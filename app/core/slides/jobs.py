"""The AI pipeline's background jobs: handlers for the Redis job worker (`app/core/jobs`).

Nothing here starts a worker: `register(registry, deps)` adds the job types and their handlers to the
shared `JobRegistry` (`app/core/jobs/registry.py`, the same registry the storyline job uses), and whoever
runs the worker loop (Phase 5/7) installs it on the queue. No route enqueues these yet.

Each handler runs one attempt in a fresh job workspace (`app.core.storage.workspace.job_workspace`):
its input blobs are fetched through the storage port into `in/`, the work runs in a thread (the
design turn and the engine are synchronous), and everything left in `out/` is stored through the
port when the attempt ends cleanly. The record's result names those stored refs; its cost is the
job's model spend.

| Job type | Inputs | Out |
|---|---|---|
| `slides.design_and_export` | `spec` or `imageRef` (+`imageName`), `furniture` or `furnitureRef`, `brand`, `templateRef`, `model`, `dense` | `slide.pptx`, `deck.zip` |
| `exports.stitch_deck` | `parts`: `[{"deckRef", "slideId"}]`, `title` | `deck.pptx` |
| `brand.extract` | `pptxRef`, `captureAllLayouts` | `brand.json` |
| `masters.render_layouts` | `pptxRef`, `width` | `layouts.zip` |
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config.ai import AISettings
from app.core import exports, render_layouts
from app.core.brand import extract as brand_extract
from app.core.brand.workzone import normalize_bounds
from app.core.design import bundle
from app.core.design.context import DesignServices
from app.core.jobs.queue import JobType
from app.core.jobs.registry import JobRegistry
from app.core.jobs.worker import Handler, JobContext, JobOutcome, PermanentJobError
from app.core.llm.ports import ProviderResolver
from app.core.slides import pipeline
from app.core.slides.spec import BrandOptions
from app.core.storage.ports import BlobPort
from app.core.storage.workspace import job_workspace
from app.engine.renderer import Renderer

JSON = dict[str, Any]

DESIGN_AND_EXPORT = JobType("slides.design_and_export", expensive=True, max_attempts=2, timeout_s=900.0)
STITCH_DECK = JobType("exports.stitch_deck", max_attempts=3, timeout_s=600.0)
BRAND_EXTRACT = JobType("brand.extract", max_attempts=2, timeout_s=300.0)
RENDER_LAYOUTS = JobType("masters.render_layouts", max_attempts=3, timeout_s=600.0)
JOB_TYPES = (DESIGN_AND_EXPORT, STITCH_DECK, BRAND_EXTRACT, RENDER_LAYOUTS)

#: Brand option fields a job's `brand` input may set (anything else is refused, not ignored).
BRAND_FIELDS = ("primary_color", "accent_color", "heading_font", "body_font", "heading", "date", "chart_data",
                "layout_archetype", "apply_brand_layout", "apply_template_bg", "tile_is_content_only", "language")


@dataclass
class PipelineDeps:
    """What the handlers need besides the job: where to write, which storage, models and renderer."""

    blobs: BlobPort
    scratch_root: Path
    settings: AISettings
    resolve: ProviderResolver | None = None
    services: DesignServices | None = None
    renderer_factory: Callable[[], Renderer | None] = lambda: None


def _brand(raw: Any) -> BrandOptions:
    if raw is None:
        return BrandOptions()
    if not isinstance(raw, dict):
        raise PermanentJobError("brand must be an object")
    unknown = sorted(set(raw) - set(BRAND_FIELDS) - {"workzone"})
    if unknown:
        raise PermanentJobError(f"unknown brand fields: {', '.join(unknown)}")
    options = BrandOptions(**{k: raw[k] for k in BRAND_FIELDS if k in raw})
    options.workzone = normalize_bounds(raw.get("workzone"))
    return options


def _ref(inputs: JSON, key: str) -> str | None:
    value = inputs.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise PermanentJobError(f"{key} must be a stored blob reference")
    return value


def design_and_export_handler(deps: PipelineDeps) -> Handler:
    async def handle(job: JobContext) -> JobOutcome:
        inputs = dict(job.record.inputs)
        wanted: dict[str, str] = {}
        for key, name in (("imageRef", "image"), ("furnitureRef", "furniture.json"), ("templateRef", "template.png")):
            ref = _ref(inputs, key)
            if ref:
                wanted[name] = ref
        async with job_workspace(deps.blobs, job.owner, deps.scratch_root, job.record.id, inputs=wanted) as ws:
            brand = _brand(inputs.get("brand"))
            if "template.png" in ws.materialised:
                brand.layout_template = ws.materialised["template.png"].read_bytes()
            furniture = inputs.get("furniture")
            if "furniture.json" in ws.materialised:
                furniture = json.loads(ws.materialised["furniture.json"].read_text(encoding="utf-8"))
            request = pipeline.DesignRequest(
                spec=inputs.get("spec") if isinstance(inputs.get("spec"), dict) else None,
                image=ws.materialised["image"].read_bytes() if "image" in ws.materialised else None,
                image_name=str(inputs.get("imageName") or "slide.png"),
                furniture=furniture if isinstance(furniture, dict) else None, brand=brand,
                model=inputs.get("model") if isinstance(inputs.get("model"), str) else None,
                dense=bool(inputs.get("dense")))
            await job.progress({"stage": "designing"})
            try:
                result = await asyncio.to_thread(
                    pipeline.design_and_export, request, ws.root, settings=deps.settings, resolve=deps.resolve,
                    services=deps.services, renderer=deps.renderer_factory())
            except pipeline.PipelineError as exc:
                raise PermanentJobError(str(exc)) from exc
        return JobOutcome(result={**result.to_json(), "pptxRef": ws.persisted["slide.pptx"].ref,
                                  "deckRef": ws.persisted["deck.zip"].ref}, cost_usd=result.cost_usd)
    return handle


def stitch_deck_handler(deps: PipelineDeps) -> Handler:
    async def handle(job: JobContext) -> JobOutcome:
        inputs = dict(job.record.inputs)
        parts = inputs.get("parts")
        if not isinstance(parts, list) or not parts:
            raise PermanentJobError("parts must be a non-empty list")
        wanted: dict[str, str] = {}
        for i, part in enumerate(parts):
            if not isinstance(part, dict) or not isinstance(part.get("slideId"), str):
                raise PermanentJobError("each part needs a deckRef and a slideId")
            ref = _ref(part, "deckRef")
            if not ref:
                raise PermanentJobError("each part needs a deckRef and a slideId")
            wanted[f"part-{i:03d}.zip"] = ref
        async with job_workspace(deps.blobs, job.owner, deps.scratch_root, job.record.id, inputs=wanted) as ws:
            def work() -> exports.StitchResult:
                slide_parts = []
                for i, part in enumerate(parts):
                    deck = bundle.unpack(ws.materialised[f"part-{i:03d}.zip"].read_bytes(), ws.root / f"part-{i:03d}")
                    slide_parts.append(exports.SlidePart(deck.dir, str(part["slideId"])))
                return exports.stitch_deck(slide_parts, ws.root, title=str(inputs.get("title") or "Deck"))
            try:
                result = await asyncio.to_thread(work)
            except (exports.StitchError, bundle.BundleError) as exc:
                raise PermanentJobError(str(exc)) from exc
        return JobOutcome(result={"pptxRef": ws.persisted["deck.pptx"].ref, "slideCount": result.slide_count,
                                  "elementCount": result.element_count, "reviewFlagCount": result.review_flag_count,
                                  "moved": result.moved})
    return handle


def brand_extract_handler(deps: PipelineDeps) -> Handler:
    async def handle(job: JobContext) -> JobOutcome:
        inputs = dict(job.record.inputs)
        ref = _ref(inputs, "pptxRef")
        if not ref:
            raise PermanentJobError("pptxRef is required")
        async with job_workspace(deps.blobs, job.owner, deps.scratch_root, job.record.id,
                                 inputs={"master.pptx": ref}) as ws:
            data = ws.materialised["master.pptx"].read_bytes()
            try:
                brand = await asyncio.to_thread(brand_extract.extract, data,
                                                capture_all_layouts=bool(inputs.get("captureAllLayouts")))
            except brand_extract.BrandExtractError as exc:
                raise PermanentJobError(str(exc)) from exc
            ws.output_path("brand.json").write_text(json.dumps(brand), encoding="utf-8")
        return JobOutcome(result={"brandRef": ws.persisted["brand.json"].ref})
    return handle


def render_layouts_handler(deps: PipelineDeps) -> Handler:
    async def handle(job: JobContext) -> JobOutcome:
        inputs = dict(job.record.inputs)
        ref = _ref(inputs, "pptxRef")
        if not ref:
            raise PermanentJobError("pptxRef is required")
        width = inputs.get("width", render_layouts.DEFAULT_WIDTH)
        if not isinstance(width, int) or isinstance(width, bool):
            raise PermanentJobError("width must be an integer")
        async with job_workspace(deps.blobs, job.owner, deps.scratch_root, job.record.id,
                                 inputs={"master.pptx": ref}) as ws:
            data = ws.materialised["master.pptx"].read_bytes()
            try:
                zipped = await asyncio.to_thread(render_layouts.render_layouts, data,
                                                 renderer=deps.renderer_factory(), width=width)
            except render_layouts.RenderLayoutsError as exc:
                if exc.kind == "invalid":
                    raise PermanentJobError(str(exc)) from exc
                raise  # unavailable: retried
            ws.output_path("layouts.zip").write_bytes(zipped)
        return JobOutcome(result={"layoutsRef": ws.persisted["layouts.zip"].ref})
    return handle


def handlers(deps: PipelineDeps) -> dict[str, Handler]:
    return {DESIGN_AND_EXPORT.name: design_and_export_handler(deps), STITCH_DECK.name: stitch_deck_handler(deps),
            BRAND_EXTRACT.name: brand_extract_handler(deps), RENDER_LAYOUTS.name: render_layouts_handler(deps)}


def register(registry: JobRegistry, deps: PipelineDeps) -> None:
    """Add the pipeline's job types and handlers to the shared registry (`app/core/jobs/registry.py`).
    Starts nothing: the process that runs workers calls `registry.install(queue)`."""
    for job_type, handler in ((DESIGN_AND_EXPORT, design_and_export_handler(deps)),
                              (STITCH_DECK, stitch_deck_handler(deps)),
                              (BRAND_EXTRACT, brand_extract_handler(deps)),
                              (RENDER_LAYOUTS, render_layouts_handler(deps))):
        registry.add(job_type, handler)
