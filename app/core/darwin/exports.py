"""The inputs of Darwin's export jobs: `/api/pptx-submit`, `/api/pptx-deck-submit`, `/api/image-to-slide`.

Darwin POSTed a multipart form to Slide Studio's `/v1/jobs` (`pptx-submit.ts`) or JSON to `/v1/decks`
(`pptx-deck-submit.ts`). Those hops are now internal jobs (plan §2.1): `slides.design_and_export`
(image mode: the slide's picture is rebuilt as HTML, then exported natively) and a Darwin deck job
that resolves the slide jobs and runs `stitch_deck`. This module builds their inputs from the same
data `pptx-submit.ts` read, with the same fallbacks:

| `/v1/jobs` form field (pptx-submit.ts) | job input |
|---|---|
| `file` (the slide PNG: refined version v >= 2, else the original) | `imageRef` (the version's stored image) |
| `primary_color`, `accent_color`, `heading_font`, `body_font` (normalizeKit) | `brand.*` |
| `language=ar` only for Arabic decks | `brand.language` |
| `layout_archetype`, `apply_brand_layout` (only when the brand has a master PNG or furniture) | `brand.*` |
| `furniture_json` (when the kit lists the archetype) | `furniture` (inline) |
| `layout_template` + `apply_template_bg` (the archetype's master PNG) | `templateRef` (copied to the caller) + `brand.apply_template_bg` |
| `heading` (slide title, with furniture), `date` (cover with furniture) | `brand.heading`, `brand.date` |
| `tile_is_content_only`, `workzone_bounds` (shouldTileSlide) | `brand.tile_is_content_only`, `brand.workzone` |
| `slide_context`, `slide_framework`, `chart_data` (OCR hints) | dropped: OCR retires (D0); the design turn reads the picture |

Lookups that fail degrade to defaults exactly where Darwin's `.catch()` did (deck data, brand,
slide types, furniture); the master PNG read is NOT caught (a failure is 500, as Darwin).
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from PIL import Image

from app.core.brand.kit import BrandKit, archetype_for_type, normalize_kit
from app.core.brand.workzone import should_tile
from app.core.errors import ApiError
from app.core.jobs.registry import JobRegistry
from app.core.jobs.worker import Handler, JobContext, JobOutcome, PermanentJobError
from app.core.slides import jobs as slide_jobs
from app.core.storage.models import CallerContext, Deck, ExportCreate, Profile, Slide
from app.core.storage.ports import Forbidden, NotFound, Storage

_log = logging.getLogger(__name__)

NOT_YOUR_DECK = "Not your deck"
IMAGE_NOT_FOUND = "Image not found"

#: The job the per-slide export runs (also image-to-slide's), and the Darwin deck job.
SLIDE_JOB = slide_jobs.DESIGN_AND_EXPORT.name
DECK_JOB = "darwin.pptx_deck"
DECK_JOB_TYPE = replace(slide_jobs.STITCH_DECK, name=DECK_JOB, expensive=False)

_MASTER_ASSET = {"cover": "titleMaster", "divider": "dividerMaster", "content": "master"}


async def owned_deck(storage: Storage, ctx: CallerContext, deck_id: str) -> Deck:
    """`ownsDeck`: the caller's deck, else 403 "Not your deck" (a missing deck too). The id must
    already be a uuid (`app.api.legacy.postgres_uuid`), so a malformed one stays Darwin's 500."""
    try:
        return await storage.decks.get(ctx, deck_id)
    except (NotFound, Forbidden) as exc:
        raise ApiError(403, NOT_YOUR_DECK) from exc


def _storyline_slides(deck: Deck) -> list[dict[str, Any]]:
    storyline = deck.storyline if isinstance(deck.storyline, dict) else {}
    slides = storyline.get("slides")
    return [s for s in slides if isinstance(s, dict)] if isinstance(slides, list) else []


async def _brand(storage: Storage, ctx: CallerContext, deck: Deck, profile: Profile) -> tuple[str | None, Any]:
    """`resolveBrandSource`: (brand id, raw kit). A brand the caller can no longer read degrades to the
    legacy per-user kit, as Darwin did; any lookup error degrades too."""
    brand_id = deck.inputs.get("brandId") if isinstance(deck.inputs.get("brandId"), str) else deck.brand_id
    if brand_id:
        try:
            access = await storage.brands.get(ctx, brand_id)
            return access.brand.id, access.brand.kit
        except Exception:  # noqa: BLE001 - Darwin's .catch(): an unreadable brand is no brand
            _log.info("export: brand %s not readable; using the legacy kit", brand_id)
    return None, profile.brand_kit


async def _asset(storage: Storage, ctx: CallerContext, brand_id: str, name: str) -> bytes | None:
    try:
        return (await storage.brands.get_asset(ctx, brand_id, name)).data
    except NotFound:
        return None


def _version_image(slide: Slide | None, version: int) -> str | None:
    if slide is None:
        return None
    for v in slide.versions:
        if v.v == version and v.mode == "image" and v.image_ref:
            return v.image_ref
    return None


async def slide_export_inputs(
    storage: Storage,
    ctx: CallerContext,
    profile: Profile,
    deck: Deck,
    slide_number: int,
    version: int,
    apply_brand_layout: bool,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """The `slides.design_and_export` inputs for one Darwin slide export (see the module table).
    Raises ApiError 404 "Image not found" when the slide has no picture for that version."""
    try:
        slides = await storage.slides.list_for_deck(ctx, deck.id)
    except Exception:  # noqa: BLE001 - getDeckWithSlides(...).catch(() => null)
        slides = []
    row = next((s for s in slides if s.number == slide_number), None)
    story = next((s for s in _storyline_slides(deck) if s.get("number") == slide_number), {})
    brand_id, raw_kit = await _brand(storage, ctx, deck, profile)
    kit: BrandKit = normalize_kit(raw_kit, {"company": deck.inputs.get("company")})
    archetype = archetype_for_type(story.get("type") or (row.spec.type if row else None))

    image_ref = _version_image(row, version if version >= 2 else 1)
    if image_ref is None:
        raise ApiError(404, IMAGE_NOT_FOUND)

    brand: dict[str, Any] = {"primary_color": kit.primary_color, "accent_color": kit.accent_color,
                             "heading_font": kit.heading_font, "body_font": kit.body_font}
    if deck.inputs.get("language") == "ar":
        brand["language"] = "ar"

    master: bytes | None = None
    furniture: dict[str, Any] | None = None
    if brand_id:
        preferred = {"cover": kit.title_master_key, "divider": kit.divider_master_key,
                     "content": kit.master_key}[archetype]
        if preferred:
            master = await _asset(storage, ctx, brand_id, _MASTER_ASSET[archetype])
        if master is None and kit.master_key:
            master = await _asset(storage, ctx, brand_id, "master")
        if kit.furniture_archetypes and archetype in kit.furniture_archetypes:
            try:
                raw = await _asset(storage, ctx, brand_id, "furniture")
                parsed = json.loads(raw.decode("utf-8")) if raw else None
                furniture = parsed if isinstance(parsed, dict) else None
            except Exception:  # noqa: BLE001 - readBrandFurnitureText(...).catch(() => null)
                furniture = None

    inputs: dict[str, Any] = {"imageRef": image_ref, "imageName": "slide.png",
                              "deckId": deck.id, "slide": slide_number}
    if furniture is not None or master is not None:
        brand["layout_archetype"] = archetype
        brand["apply_brand_layout"] = apply_brand_layout
        if furniture is not None:
            inputs["furniture"] = furniture
        if master is not None:
            template = await storage.blobs.put(ctx, master, "image/png")
            inputs["templateRef"] = template.ref
            brand["apply_template_bg"] = apply_brand_layout
        title = row.spec.title if row else ""
        if furniture is not None and title:
            brand["heading"] = title
            if archetype == "cover":
                brand["date"] = clock().strftime("%B %Y")
    if kit.workzone is not None and should_tile(archetype, kit.workzone, list(kit.furniture_archetypes or [])):
        brand["tile_is_content_only"] = True
        brand["workzone"] = kit.workzone.to_json()
    inputs["brand"] = brand
    return inputs


async def count_export(storage: Storage, ctx: CallerContext, deck_id: str, slides: list[int],
                       job_id: str) -> None:
    """`void incrementExportCounts(userId, deckId)`: fire-and-forget, never fails the route."""
    try:
        await storage.exports.record(ctx, ExportCreate(deck_id=deck_id, kind="pptx", slide_numbers=slides,
                                                       job_id=job_id))
    except Exception:  # noqa: BLE001
        _log.exception("export: the export counter write failed")


# ------------------------------------------------------------------------------- image-to-slide
IMAGE_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def image_for_pipeline(data: bytes, mime: str) -> tuple[bytes, str]:
    """The uploaded picture as the pipeline accepts it (PNG, JPEG or WebP). A GIF, which Darwin
    accepted and the design pipeline does not, becomes a PNG of its first frame."""
    if mime in IMAGE_EXTENSIONS:
        return data, f"slide{IMAGE_EXTENSIONS[mime]}"
    with Image.open(io.BytesIO(data)) as img:
        img.seek(0)
        out = io.BytesIO()
        img.convert("RGBA").save(out, format="PNG")
    return out.getvalue(), "slide.png"


# ------------------------------------------------------------------------------------ deck job
def deck_handler(deps: slide_jobs.PipelineDeps, storage: Storage, stitch: Handler | None = None) -> Handler:
    """`darwin.pptx_deck`: resolve `jobIds` (the caller's finished slide exports) into stitch parts,
    then run the shared stitch handler. Darwin's upstream stitched whatever ids it was given and the
    deck job failed later when one was unknown; here an unknown, unfinished or foreign id fails the
    deck job the same way (status `failed`). `stitch` defaults to the pipeline's stitch handler."""
    stitch = stitch if stitch is not None else slide_jobs.stitch_deck_handler(deps)

    async def handle(job: JobContext) -> JobOutcome:
        inputs = dict(job.record.inputs)
        parts: list[dict[str, str]] = []
        for job_id in inputs.get("jobIds") or []:
            if not isinstance(job_id, str):
                raise PermanentJobError("A slide to stitch is missing.")
            try:
                part = await storage.jobs.get(job.owner, job_id)
            except (NotFound, Forbidden) as exc:
                raise PermanentJobError("A slide to stitch is missing.") from exc
            result = part.result or {}
            if part.type != SLIDE_JOB or part.status != "done" or not result.get("deckRef"):
                raise PermanentJobError("A slide to stitch is not finished.")
            parts.append({"deckRef": str(result["deckRef"]), "slideId": str(result.get("slideId") or "")})
        title = inputs.get("presentationTitle")
        record = job.record.model_copy(update={"inputs": {"parts": parts, "title": title if isinstance(title, str)
                                                          and title else "Deck"}})
        return await stitch(JobContext(record=record, owner=job.owner, attempt=job.attempt,
                                       _progress=job._progress))

    return handle


def register(registry: JobRegistry, deps: slide_jobs.PipelineDeps, storage: Storage) -> None:
    """The export job types: the pipeline's own (design_and_export, stitch_deck, ...) and the Darwin
    deck job. Registered NOT expensive: Darwin had no in-flight limit, so the per-user limit of 3
    would be a new 429 on its routes. TODO-P5 in-flight: turn it on with Phase 5's controls."""
    handlers = slide_jobs.handlers(deps)
    for job_type in slide_jobs.JOB_TYPES:
        registry.add(replace(job_type, expensive=False), handlers[job_type.name])
    registry.add(DECK_JOB_TYPE, deck_handler(deps, storage))
