"""The brand preview behind `/api/brand-preview` (was `brand-preview-background.ts` + `_shared/brandPreview.ts`).

One fixed sample slide (`SAMPLE_SLIDE`) rendered in the brand's look by the image model, so previews are
comparable across brands and over time:

1. the brand's kit (`normalize_kit`); when the kit names a master, its PNG must load, or the job fails
   with Darwin's text (never a silent master-less preview);
2. a content hash over what changes the pixels (colours, fonts, company, style template, the master's
   bytes): an unchanged brand is served from the cached PNG, and no image slot is spent;
3. otherwise one slot of the GLOBAL daily image cap (`reserve_image_slot`; "Daily image limit reached —
   try again tomorrow." when it is spent), then one gpt-image call: an edit conditioned on the master
   (Image 1) when there is one, else a plain generation; its estimated cost goes to the ledger.

The PNG is stored as the brand asset `preview-<hash>` (Darwin: the `brand-preview` blob store, keyed by
the requesting user); `/api/brand-preview-status` returns it as base64.

Behaviour changes against Darwin (docs/darwin-api.md §6):
* no layout wireframe: a brand with extracted layouts but no master previews from the prompt alone
  (Darwin synthesised a wireframe PNG with sharp, `layoutTemplate.ts`, not ported);
* the prompt is `assembleSlidePrompt` for the sample slide, through the image-prompt module
  (`app/core/darwin/prompt.py`, `preview_prompt`);
* a brand that is no longer editable when the job runs fails it ("Brand not found"); Darwin fell back
  to the user's legacy kit;
* the image is ledgered (kind image), which Darwin never did (C10).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.config.darwin import DarwinSettings
from app.core.brand.kit import BrandKit, normalize_kit
from app.core.darwin import prompt_text
from app.core.darwin.brands import BRAND_NOT_FOUND, PNG_TYPE, get_brand_access, read_asset
from app.core.darwin.caps import reserve_image_slot
from app.core.darwin.image_gen import ImageGenerator, ImageGenError
from app.core.darwin.prompt import assemble_slide_prompt, kit_for_prompt
from app.core.darwin.usage import EST_COST_MASTER, EST_COST_PER_IMAGE, KIND_IMAGE, record_usage_quietly
from app.core.jobs.queue import JobType
from app.core.jobs.registry import JobRegistry
from app.core.jobs.worker import Handler, JobContext, JobOutcome, PermanentJobError
from app.core.redis_client import RedisStore
from app.core.storage.models import CallerContext
from app.core.storage.ports import Forbidden, NotFound, Storage

_log = logging.getLogger(__name__)

BRAND_PREVIEW_JOB = "darwin.brand_preview"
# TODO-P5 in-flight: Darwin had no per-user in-flight limit.
BRAND_PREVIEW = JobType(BRAND_PREVIEW_JOB, expensive=False, max_attempts=2, timeout_s=300.0)

MASTER_UNAVAILABLE = "Brand master could not be loaded — re-upload it, then preview again."
IMAGE_CAP = "Daily image limit reached — try again tomorrow."
PREVIEW_GONE = "Preview image is no longer available"
PREVIEW_FAILED = "Preview failed"

#: `brandPreview.ts: SAMPLE_SLIDE`.
SAMPLE_SLIDE: dict[str, Any] = {
    "number": 1,
    "title": "Three moves to widen the margin lead",
    "type": "framework",
    "framework": "3-column layout",
    "description": "A representative content slide used to preview how this brand renders.",
    "bullets": ["Consolidate vendors to cut spend 12%", "Shift routine support to self-serve",
                "Reprice the enterprise tier for value"],
}

#: `generateCore.ts: MASTER_INSTRUCTION`.
MASTER_INSTRUCTION = prompt_text.MASTER_INSTRUCTION

#: Builds the sample slide's image prompt from the brand's kit.
Prompter = Callable[[BrandKit], str]


def preview_prompt(kit: BrandKit) -> str:
    """`assembleSlidePrompt(SAMPLE_SLIDE, kit)` for the preview's sample slide (English, full slide), through
    the image-prompt module (`app/core/darwin/prompt.py`)."""
    return assemble_slide_prompt(SAMPLE_SLIDE, kit_for_prompt(kit))


def preview_content_hash(kit: BrandKit, master: bytes | None) -> str:
    """`previewContentHash`: only what changes the pixels, so the cache invalidates exactly when a
    re-render would look different (the master's BYTES, so a re-uploaded master is a new preview)."""
    h = hashlib.sha256()
    h.update(json.dumps({"primaryColor": kit.primary_color, "accentColor": kit.accent_color,
                         "headingFont": kit.heading_font, "bodyFont": kit.body_font, "company": kit.company,
                         "styleTemplate": kit.style_template, "hasMaster": master is not None,
                         "hasWireframe": False}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if master is not None:
        h.update(master)
    return h.hexdigest()[:16]


def preview_asset(content_hash: str) -> str:
    return f"preview-{content_hash}"


@dataclass
class PreviewDeps:
    storage: Storage
    redis: RedisStore
    images: ImageGenerator
    darwin: DarwinSettings
    prompter: Prompter = preview_prompt


def brand_preview_handler(deps: PreviewDeps) -> Handler:
    async def handle(job: JobContext) -> JobOutcome:
        storage, owner = deps.storage, job.owner
        brand_id = str(job.record.inputs.get("brandId") or "")
        access = await get_brand_access(storage, owner, brand_id)
        if access is None or not access.can_edit:
            raise PermanentJobError(BRAND_NOT_FOUND)
        kit = normalize_kit(access.brand.kit)
        master: bytes | None = None
        if kit.master_key:
            # SECURITY: the master is read by its fixed name under the access-checked brand, never
            # from the client-writable kit value.
            master = await read_asset(storage, owner, access.brand.id, "master")
            if master is None:
                raise PermanentJobError(MASTER_UNAVAILABLE)
        name = preview_asset(preview_content_hash(kit, master))
        result = {"brandId": access.brand.id, "asset": name}
        if await read_asset(storage, owner, access.brand.id, name) is not None:
            return JobOutcome(result=result)  # cached: no image slot spent
        if not await reserve_image_slot(deps.redis, deps.darwin):
            raise PermanentJobError(IMAGE_CAP)
        prompt = deps.prompter(kit)
        try:
            image = (await deps.images.edit(MASTER_INSTRUCTION + prompt, [master]) if master is not None
                     else await deps.images.generate(prompt))
        except ImageGenError as exc:
            raise PermanentJobError(exc.message or PREVIEW_FAILED) from exc
        cost = EST_COST_MASTER if master is not None else EST_COST_PER_IMAGE
        await record_usage_quietly(storage.usage, owner, model=image.model, cost_usd=cost, kind=KIND_IMAGE,
                                   idempotency_key=f"job:{job.record.id}")
        try:
            await storage.brands.put_asset(owner, access.brand.id, name, image.png, PNG_TYPE)
        except (NotFound, Forbidden) as exc:
            raise PermanentJobError(BRAND_NOT_FOUND) from exc
        return JobOutcome(result=result, cost_usd=cost)
    return handle


async def read_preview(storage: Storage, owner_ctx: CallerContext, result: dict[str, Any] | None) -> bytes | None:
    """The finished job's PNG, or None when it is gone (or the brand is no longer visible)."""
    if not result or not isinstance(result.get("brandId"), str) or not isinstance(result.get("asset"), str):
        return None
    try:
        return await read_asset(storage, owner_ctx, result["brandId"], result["asset"])
    except Forbidden:
        return None


def register(registry: JobRegistry, deps: PreviewDeps) -> None:
    registry.add(BRAND_PREVIEW, brand_preview_handler(deps))
