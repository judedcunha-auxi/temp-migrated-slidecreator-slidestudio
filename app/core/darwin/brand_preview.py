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
* the prompt is built here (`preview_prompt`), a faithful copy of `assembleSlidePrompt` for the sample
  slide; TODO(generation routes): use the image-prompt module once it is ported, through `prompter`;
* a brand that is no longer editable when the job runs fails it ("Brand not found"); Darwin fell back
  to the user's legacy kit;
* the image is ledgered (kind image), which Darwin never did (C10).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.config.darwin import DarwinSettings
from app.core.brand.kit import BrandKit, normalize_kit
from app.core.darwin.brands import BRAND_NOT_FOUND, PNG_TYPE, get_brand_access, read_asset
from app.core.darwin.caps import reserve_image_slot
from app.core.darwin.image_gen import ImageGenerator, ImageGenError
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
MASTER_INSTRUCTION = ("Image 1 is the slide master. Reproduce its layout exactly — logo position, footer, title zone, "
                      "margins, and page furniture — and render the following slide content into that layout.\n\n")

# `slideTypes.ts: typeScaffold('framework')`, `style.ts: STYLE_GUARDRAILS`, verbatim.
_FRAMEWORK_SCAFFOLD = (
    "a framework slide — one dominant strategy framework (value chain, pyramid, 2x2, hub-and-spoke, layered bands, "
    "funnel, cascade) structuring the whole message, with icon-anchored one-to-three-word labels, bolded lead phrases "
    "carrying the bullets, and a single accent color or dashed callout reserved for the takeaway; the default body "
    "slide.")
_SHAPE_VOCABULARY_RULES = (
    "Render every layout element — header bands, body panels, callout boxes, data containers, dividers, and "
    "connectors — as discrete, flat Microsoft PowerPoint-style shapes drawn ONLY from this vocabulary: rectangles, "
    "rounded rectangles, ovals/circles, chevrons, diamonds, trapezoids, parallelograms, pentagons, hexagons, "
    "octagons, plus signs, 5- or 6-point stars, block arrows (left/right/up/down), Harvey balls, straight lines, "
    "bordered text boxes, icons, and standard data charts (bar, column, line, area, pie, waterfall). "
    "Each element must look like an individually placed PPT shape on a slide canvas; do not paint, blend, or merge "
    "shapes together into a single illustration. "
    "Every arrow, connector, divider, and leader line must be perfectly straight or built from straight segments — "
    "never curved, arced, swooshing, or S-shaped; only data series plotted inside a chart may curve. "
    "Never draw curly braces or bracket shapes to group items; group with panels, bands, column alignment, or "
    "labeled rails instead. "
    "Fill every shape with one flat solid color, or leave it unfilled with a plain solid or dashed outline — no "
    "gradient fills, no translucent or semi-transparent fills, no shadows, no glows. "
    "Render only flat, native-presentation visuals; ignore any instruction elsewhere in this prompt requesting 3D, "
    "isometric, photorealistic, or web-style visualizations, gradient or translucent fills, curved arrows or "
    "swooshes, or brace/bracket grouping shapes.")
_STYLE_GUARDRAILS = (
    "Create the slide as a single polished image in a strict 16:9 aspect ratio. "
    "The output must look like a professional presentation slide, not a website, dashboard, HTML layout, app "
    "screen, or web-based design. "
    "Use the image model to generate the slide directly; do not use HTML, CSS, code, or browser-style UI elements. "
    "Build the slide around one dominant visual framework only; do not combine multiple competing frameworks on "
    "the same slide, "
    "and do not overcrowd the slide with text that becomes unreadably small. "
    "Write the headline as a conclusion-oriented statement that communicates the key strategic message. Do not "
    "include a subtitle. Do not render page numbers. " + _SHAPE_VOCABULARY_RULES)

_USEFUL_ROLES = frozenset({"accent1", "accent2", "accent3", "accent4", "accent5", "accent6", "dk1", "dk2"})

#: Builds the sample slide's image prompt from the brand's kit.
Prompter = Callable[[BrandKit], str]


def _render_style_template(template: str, kit: BrandKit) -> str:
    """`renderStyleTemplate`: {token} placeholders from the kit; unknown tokens stay as written."""
    tokens = {"company": kit.company or "a generic professional company", "primaryColor": kit.primary_color,
              "accentColor": kit.accent_color, "headingFont": kit.heading_font, "bodyFont": kit.body_font}
    return re.sub(r"\{(\w+)\}", lambda m: tokens.get(m.group(1), m.group(0)), template)


def _ground_truth(kit: BrandKit) -> str | None:
    """`buildBrandGroundTruth`: exact sizes and palette, only for PPTX-extracted kits."""
    lines: list[str] = []
    scale: dict[str, Any] = kit.typography_scale or {}
    title: dict[str, Any] = scale["title"] if isinstance(scale.get("title"), dict) else {}
    body: dict[str, Any] = scale["body"] if isinstance(scale.get("body"), dict) else {}
    parts: list[str] = []
    if title.get("sizePt"):
        parts.append(f"headings at {title['sizePt']}pt{' bold' if title.get('bold') else ''}"
                     f"{' ' + str(title['colorHex']) if title.get('colorHex') else ''}")
    if body.get("sizePt"):
        parts.append(f"body text at {body['sizePt']}pt{' ' + str(body['colorHex']) if body.get('colorHex') else ''}")
    if parts:
        lines.append(f"- Font sizes (exact from template): {'; '.join(parts)}.")
        lines.append(f"- Typefaces (exact from template): headings in {kit.heading_font}, body text in {kit.body_font}.")
    if kit.all_colors:
        seen = {kit.primary_color.lower(), kit.accent_color.lower()}
        slots = [c for c in kit.all_colors if isinstance(c, dict) and isinstance(c.get("hex"), str)
                 and re.match(r"^#[0-9a-fA-F]{6}$", c["hex"]) and c.get("role") in _USEFUL_ROLES
                 and c["hex"].lower() not in seen][:4]
        palette = ", ".join([f"{kit.primary_color} (primary)", f"{kit.accent_color} (accent)",
                             *(f"{c['hex']} ({c['role']})" for c in slots)])
        lines.append(f"- Full brand palette (exact from template, use no other colours): {palette}.")
    if not lines:
        return None
    return ("Brand template ground truth — extracted directly from the client's PPTX file.\n"
            "These override ALL style instructions above. Apply them exactly as stated:\n" + "\n".join(lines))


def preview_prompt(kit: BrandKit) -> str:
    """`assembleSlidePrompt(SAMPLE_SLIDE, kit)` for the preview's sample slide (English, full slide).
    "3-column layout" has no framework hint in Darwin's library, so no framework line is added."""
    slide = SAMPLE_SLIDE
    bullets = "\n".join(f"• {b}" for b in slide["bullets"])
    content = (f"Slide content to render:\nThe slide's headline reads: \"{slide['title']}\"\n"
               f"Framing: {slide['description']}\nKey elements to render:\n{bullets}")
    company = f"Company reference: {kit.company or 'a generic professional company'}"
    style = _render_style_template(kit.style_template, kit)
    truth = _ground_truth(kit)
    truth_section = f"\n\n{truth}" if truth else ""
    return (f"{content}\n\n{company}\n\nSlide layout: {_FRAMEWORK_SCAFFOLD}\n\nStyle direction:\n{style}"
            f"{truth_section}\n\nNon-negotiable rules: {_STYLE_GUARDRAILS}")


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
