"""The brand import jobs behind `/api/brand-pptx` and `/api/brand-extract` (were `-background` functions).

Darwin ran these as Netlify background functions that anyone could call with any `userId` (C12). Here
they are internal Redis jobs (D31): the route checks access and queues the job as the caller, the job
runs with the caller's identity (`job.owner`), and every storage call is the port's, so the ACL is
re-checked where it matters (as Darwin re-checked edit access in `brand-pptx-background`).

| Job | Queued by | Was | Result (`record.result`) |
|---|---|---|---|
| `darwin.brand_pptx` | `/api/brand-pptx` | `brand-pptx-background.ts` | `{"fields": BrandPptxFields}` |
| `darwin.brand_guidelines` | `/api/brand-extract` | `brand-extract-background.ts` | `{"fields": ExtractedBrandFields}` |

**`darwin.brand_pptx`** (a .pptx template): extracts the brand in-process (`app/core/brand/extract.py`,
which was Slide Studio's `/brand-extract` behind `PPTX_API_URL`) with the per-layout furniture capture,
and, when the job names a brand, renders the layouts through PptxRender (`app/core/render_layouts.py`,
was `RENDER_API_URL/render-layouts`) at the same time. With a brand it also stores the logo, the
furniture (`furniture`, and `furniture-all` with the per-layout capture) and one `layout-preview-<n>`
PNG per layout, keyed by the extractor's layout index when the names match one-to-one. Rendering,
the logo and the furniture are best-effort, as in Darwin: a failure skips them, never the import.
The extracted fields are NOT written to the kit: the client PATCHes /api/brands, as before.

**`darwin.brand_guidelines`** (a brand-guidelines PDF): one model call (`app/core/brand/guidelines.py`);
its cost goes to the ledger (`kind` brand_guidelines; Darwin recorded none, C10). The per-user daily
cap (`reserve_brand_extract`) is the route's, OFF by default.

Both types are registered NOT expensive: Darwin had no per-user in-flight limit (TODO-P5 in-flight).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PIL import Image

from app.config.storyline import StorylineSettings
from app.core import render_layouts
from app.core.brand import extract as brand_extract
from app.core.brand.guidelines import GuidelinesError, extract_guidelines
from app.core.brand.kit import (
    ARCHETYPES,
    FURNITURE_ALL_ASSET,
    FURNITURE_ASSET,
    GUIDELINES_ASSET,
    brand_asset_key,
    brand_furniture_key,
    layout_preview_asset,
    normalize_heading_placeholder,
)
from app.core.brand.legacy_mapping import (
    align_previews_to_layouts,
    default_export_furniture,
    rendered_layouts,
    slug_to_name,
)
from app.core.brand.workzone import normalize_bounds
from app.core.darwin.brands import BRAND_NOT_FOUND, JSON_TYPE, PNG_MAGIC, PNG_TYPE, get_brand_access
from app.core.darwin.usage import record_usage_quietly
from app.core.jobs.queue import JobType
from app.core.jobs.registry import JobRegistry
from app.core.jobs.worker import Handler, JobContext, JobOutcome, PermanentJobError
from app.core.storage.models import BrandAccess, CallerContext
from app.core.storage.ports import Forbidden, NotFound, Storage
from app.core.storyline.ports import ModelRejected, ModelUnavailable, StorylineModel
from app.engine.renderer import Renderer

_log = logging.getLogger(__name__)

BRAND_PPTX_JOB = "darwin.brand_pptx"
BRAND_GUIDELINES_JOB = "darwin.brand_guidelines"
# TODO-P5 in-flight: Darwin had no per-user in-flight limit, so neither is "expensive" yet.
BRAND_PPTX = JobType(BRAND_PPTX_JOB, expensive=False, max_attempts=2, timeout_s=600.0)
BRAND_GUIDELINES = JobType(BRAND_GUIDELINES_JOB, expensive=False, max_attempts=2, timeout_s=300.0)

KIND_BRAND_GUIDELINES = "brand_guidelines"
#: Darwin rendered layout previews this wide (`brand-pptx-background.ts: RENDER_LAYOUT_WIDTH`).
LAYOUT_PREVIEW_WIDTH = 1600

PPTX_NOT_FOUND = "PPTX file not found in storage"
PDF_NOT_FOUND = "Guidelines PDF not found"
EXTRACTION_FAILED = "Extraction failed"

JSON = dict[str, Any]


@dataclass
class BrandJobDeps:
    storage: Storage
    renderer_factory: Callable[[], Renderer | None]
    guidelines_model: StorylineModel
    storyline: StorylineSettings


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


async def _final_cleanup(storage: Storage, owner: CallerContext, ref: Any) -> None:
    """Delete a temporary upload once the job has its final outcome (best effort)."""
    if not isinstance(ref, str) or not ref:
        return
    try:
        await storage.blobs.delete(owner, ref)
    except Exception:  # noqa: BLE001 - cleanup must never change the job's outcome
        _log.warning("brand job: could not delete the temporary upload", exc_info=True)


# ------------------------------------------------------------------------------- PPTX template
def _render(renderer_factory: Callable[[], Renderer | None], data: bytes) -> list[tuple[int, str, bytes]]:
    """The deck's layout PNGs, or [] on any failure (Darwin: previews are best-effort)."""
    try:
        zipped = render_layouts.render_layouts(data, renderer=renderer_factory(), width=LAYOUT_PREVIEW_WIDTH)
        with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
            entries = {info.filename: archive.read(info) for info in archive.infolist() if not info.is_dir()}
        return rendered_layouts(entries)
    except render_layouts.RenderLayoutsError as exc:
        _log.warning("brand-pptx: layout previews skipped (%s): %s", exc.kind, exc)
    except Exception:  # noqa: BLE001 - previews never fail the import
        _log.warning("brand-pptx: layout rendering failed; skipping previews", exc_info=True)
    return []


def _as_png(raw: bytes) -> bytes:
    """The logo as PNG: PNG bytes as they are, anything else converted (Darwin used sharp)."""
    if raw[:4] == PNG_MAGIC:
        return raw
    with Image.open(io.BytesIO(raw)) as img:
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()


async def _store_logo(storage: Storage, owner: CallerContext, access: BrandAccess, data: JSON) -> str | None:
    images = data.get("masterImages")
    if not isinstance(images, list) or not images:
        return None
    logo = next((i for i in images if isinstance(i, dict) and i.get("role") == "logo"), None) \
        or next((i for i in images if isinstance(i, dict) and i.get("role") == "logo_mark"), None)
    if logo is None:
        return None
    try:
        png = await asyncio.to_thread(_as_png, base64.b64decode(str(logo.get("imageBase64") or ""), validate=False))
        await storage.brands.put_asset(owner, access.brand.id, "logo", png, PNG_TYPE)
    except Exception:  # noqa: BLE001 - the logo is best-effort
        _log.warning("brand-pptx: logo auto-extract failed", exc_info=True)
        return None
    return brand_asset_key(access.brand.owner_id, access.brand.id, "logo")


async def _store_furniture(storage: Storage, owner: CallerContext, access: BrandAccess,
                           captured: Any) -> list[str] | None:
    """Store the furniture when at least one archetype carries some; return those archetypes."""
    if not isinstance(captured, dict):
        return None
    layouts = captured.get("layouts")
    archetypes = [k for k in layouts if k in ARCHETYPES] if isinstance(layouts, dict) else []
    if not archetypes:
        return None
    try:
        if captured.get("layoutsByIndex"):
            await storage.brands.put_asset(owner, access.brand.id, FURNITURE_ALL_ASSET, _json_bytes(captured),
                                           JSON_TYPE)
        await storage.brands.put_asset(owner, access.brand.id, FURNITURE_ASSET,
                                       _json_bytes(default_export_furniture(captured)), JSON_TYPE)
    except Exception:  # noqa: BLE001 - furniture is best-effort
        _log.warning("brand-pptx: storing the furniture failed", exc_info=True)
        return None
    return archetypes


async def _store_previews(storage: Storage, owner: CallerContext, access: BrandAccess,
                          rendered: list[tuple[int, str, bytes]], layouts: Any) -> list[JSON]:
    """`storeLayoutPreviews`: each PNG under the extractor's layout index (renderer order when the names
    do not match one-to-one), named after the real layout."""
    extracted = layouts if isinstance(layouts, list) else []
    aligned = align_previews_to_layouts([(i, slug) for i, slug, _ in rendered], extracted)
    if aligned is None and rendered:
        _log.warning("brand-pptx: layout previews do not match the extracted layouts by name; renderer order kept")
    names = {int(entry["index"]): entry["name"] for entry in extracted
             if isinstance(entry, dict) and isinstance(entry.get("index"), int) and isinstance(entry.get("name"), str)}
    previews: list[JSON] = []
    for render_index, slug, png in rendered:
        index = aligned[render_index] if aligned is not None else render_index
        await storage.brands.put_asset(owner, access.brand.id, layout_preview_asset(index), png, PNG_TYPE)
        name = (names.get(index) if aligned is not None else None) or slug_to_name(slug) or f"Layout {index + 1}"
        previews.append({"index": index, "name": name})
    return sorted(previews, key=lambda p: int(p["index"]))


def _heading_placeholders(captured: Any, archetypes: list[str]) -> dict[str, JSON] | None:
    layouts = captured.get("layouts") if isinstance(captured, dict) else None
    out: dict[str, JSON] = {}
    for arch in archetypes:
        entry = layouts.get(arch) if isinstance(layouts, dict) else None
        placeholders = entry.get("placeholders") if isinstance(entry, dict) else None
        ph = normalize_heading_placeholder(placeholders.get("heading") if isinstance(placeholders, dict) else None)
        if ph is not None:
            out[arch] = ph
    return out or None


def _fields(data: JSON) -> JSON:
    """The extracted fields every import returns (`fields` in brand-pptx-background.ts), absent not null."""
    out: JSON = {}
    for key in ("headingFont", "bodyFont", "primaryColor", "accentColor"):
        if data.get(key) is not None:
            out[key] = data[key]
    if isinstance(data.get("layouts"), list):
        out["layouts"] = data["layouts"]
    workzone = normalize_bounds(data.get("workzone"))
    if workzone is not None:
        out["workzone"] = workzone.to_json()
    if data.get("typographyScale") is not None:
        out["typographyScale"] = data["typographyScale"]
    for key in ("masterDecorations", "logoShapes", "allColors"):
        if isinstance(data.get(key), list):
            out[key] = data[key]
    return out


def brand_pptx_handler(deps: BrandJobDeps) -> Handler:
    async def handle(job: JobContext) -> JobOutcome:
        inputs = job.record.inputs
        ref, brand_id = inputs.get("pptxRef"), inputs.get("brandId")
        storage, owner = deps.storage, job.owner
        final = True
        try:
            access: BrandAccess | None = None
            if brand_id:
                # Re-check edit access: it may have been revoked since the route checked it.
                access = await get_brand_access(storage, owner, brand_id)
                if access is None or not access.can_edit:
                    raise PermanentJobError(BRAND_NOT_FOUND)
            try:
                pptx = (await storage.blobs.get(owner, str(ref))).data
            except (NotFound, Forbidden) as exc:
                raise PermanentJobError(PPTX_NOT_FOUND) from exc
            extracting = asyncio.to_thread(brand_extract.extract, pptx, capture_all_layouts=True)
            rendering = asyncio.to_thread(_render, deps.renderer_factory, pptx) if access is not None \
                else asyncio.sleep(0, result=[])
            try:
                data, rendered = await asyncio.gather(extracting, rendering)
            except brand_extract.BrandExtractError as exc:
                raise PermanentJobError(str(exc)) from exc
            fields = _fields(data)
            if access is not None:
                logo_key = await _store_logo(storage, owner, access, data)
                captured = data.get("capturedFurniture")
                archetypes = await _store_furniture(storage, owner, access, captured)
                headings = _heading_placeholders(captured, archetypes) if archetypes else None
                try:
                    previews = await _store_previews(storage, owner, access, rendered, data.get("layouts"))
                except Exception:  # noqa: BLE001 - previews are best-effort
                    _log.warning("brand-pptx: storing layout previews failed", exc_info=True)
                    previews = []
                if logo_key:
                    fields["logoKey"] = logo_key
                if archetypes:
                    fields["furnitureKey"] = brand_furniture_key(access.brand.owner_id, access.brand.id)
                    fields["furnitureArchetypes"] = archetypes
                if headings:
                    fields["headingPlaceholders"] = headings
                if previews:
                    fields["layoutPreviews"] = previews
            return JobOutcome(result={"fields": fields})
        except PermanentJobError:
            raise
        except Exception:
            final = job.attempt >= BRAND_PPTX.max_attempts  # a retry needs the upload
            raise
        finally:
            if final:
                await _final_cleanup(storage, owner, ref)  # Darwin always deleted the temp upload
    return handle


# ----------------------------------------------------------------------------- guidelines PDF
def brand_guidelines_handler(deps: BrandJobDeps) -> Handler:
    async def handle(job: JobContext) -> JobOutcome:
        inputs = job.record.inputs
        storage, owner = deps.storage, job.owner
        ephemeral_ref = inputs.get("pdfRef")
        final = True
        try:
            try:
                if inputs.get("brandId"):
                    pdf = (await storage.brands.get_asset(owner, str(inputs["brandId"]), GUIDELINES_ASSET)).data
                else:
                    pdf = (await storage.blobs.get(owner, str(ephemeral_ref))).data
            except (NotFound, Forbidden) as exc:
                raise PermanentJobError(PDF_NOT_FOUND) from exc
            try:
                result = await extract_guidelines(deps.guidelines_model, pdf, model_name=deps.storyline.model)
            except GuidelinesError as exc:
                raise PermanentJobError(str(exc)) from exc
            except ModelRejected as exc:
                _log.error("brand-extract: the model call was rejected: %s", exc)
                raise PermanentJobError(EXTRACTION_FAILED) from exc
            except ModelUnavailable as exc:
                if job.attempt >= BRAND_GUIDELINES.max_attempts:
                    raise PermanentJobError(EXTRACTION_FAILED) from exc
                final = False
                raise
            if result.usage.cost_usd > 0:
                await record_usage_quietly(storage.usage, owner, model=result.model, cost_usd=result.usage.cost_usd,
                                           kind=KIND_BRAND_GUIDELINES, idempotency_key=f"job:{job.record.id}")
            return JobOutcome(result={"fields": result.fields}, cost_usd=result.usage.cost_usd)
        except PermanentJobError:
            raise
        except Exception:
            final = final and job.attempt >= BRAND_GUIDELINES.max_attempts
            raise
        finally:
            if final:
                await _final_cleanup(storage, owner, ephemeral_ref)  # only the parked, brand-less upload
    return handle


def register(registry: JobRegistry, deps: BrandJobDeps) -> None:
    """Add the two brand import job types to the shared registry (app/core/darwin/runtime.py)."""
    registry.add(BRAND_PPTX, brand_pptx_handler(deps))
    registry.add(BRAND_GUIDELINES, brand_guidelines_handler(deps))
