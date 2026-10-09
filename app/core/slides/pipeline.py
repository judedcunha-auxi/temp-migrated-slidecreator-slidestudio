"""`design_and_export`: one slide from a storyline spec or a picture, designed as HTML and exported as PPTX.

The internal replacement for Slide Studio's `/v1/jobs` (`server/compat/orchestrate.run_job`;
migration plan §2.1 and §4.1, R). There is no HTTP route: `/api/pptx-submit` (Phase 7) calls this,
in a job, through `app/core/slides/jobs.py`.

    1. master  -> the brand's master for the slide's archetype (synthesised from captured furniture,
                  debranded, with the template background, or plain; `app.core.masters`), imported
                  into a fresh deck in the job workspace
    2. design  -> one design turn (`app.core.design.design_turn`): spec mode (the storyline slide is
                  the brief) or image mode (rebuild the attached picture natively); the turn
                  previews, takes the critic's review and fixes, as in the chat
    3. workzone-> when the brand has a workzone, the saved slide is measured against it; content that
                  leaves it or covers the header band gets ONE repair turn (`edit_slide`), and what is
                  left is counted as review flags (the known gap: content overran the workzone)
    4. export  -> the engine rebuilds the slide as native PowerPoint (`app.core.engine_service`)

The result fills what the old adapter left null: `element_count` (what the export built) and
`review_flag_count` (lint + measured diagnostics + design-review/workzone findings still open), and
carries the turn's cost (design rounds + critic calls).

`DESIGN_STUB=true` saves a hand-authored slide instead of step 2 ($0; never in production).
"""

from __future__ import annotations

import html as html_lib
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config.ai import AISettings, ai_settings
from app.core import engine_service, masters
from app.core.brand import workzone as wz
from app.core.design import bundle
from app.core.design.context import DesignContext, DesignServices
from app.core.design.deck import DesignDeck
from app.core.design.design_turn import DesignTurnResult, run_design_turn
from app.core.design_refs.workzone_lint import workzone_findings_static
from app.core.llm.ports import ProviderResolver
from app.core.slides import spec as spec_mod
from app.core.slides.spec import BrandOptions
from app.engine.renderer import Renderer

_log = logging.getLogger(__name__)

JSON = dict[str, Any]

#: Image types the image route accepts (what Darwin sends).
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024


class PipelineError(RuntimeError):
    """The slide could not be designed or exported; the message is safe to show."""


@dataclass
class DesignRequest:
    """One slide to make. Exactly one of `spec` (a storyline slide, bare or `{"slide", "deck"}`) and
    `image` (a finished slide picture to rebuild) is set."""

    spec: JSON | None = None
    image: bytes | None = None
    image_name: str = "slide.png"
    furniture: JSON | None = None
    brand: BrandOptions = field(default_factory=BrandOptions)
    model: str | None = None
    #: Spec mode only: the storyline page's dense slides (composite exhibits, labelled estimates).
    dense: bool = False
    deck_name: str = "Slide"


@dataclass
class PipelineResult:
    pptx: Path
    deck_dir: Path
    slide_id: str
    archetype: str
    master_mode: str
    element_count: int
    review_flag_count: int
    review_flags: list[JSON]
    cost_usd: float
    design_cost_usd: float = 0.0
    critic_cost_usd: float = 0.0
    template_background: bool = False
    turns: list[DesignTurnResult] = field(default_factory=list)
    reviews: list[tuple[str, bool]] = field(default_factory=list)

    def to_json(self) -> JSON:
        return {"slideId": self.slide_id, "archetype": self.archetype, "masterMode": self.master_mode,
                "templateBackground": self.template_background,
                "elementCount": self.element_count, "reviewFlagCount": self.review_flag_count,
                "costUsd": round(self.cost_usd, 6), "designCostUsd": round(self.design_cost_usd, 6),
                "criticCostUsd": round(self.critic_cost_usd, 6),
                "continuations": sum(t.continuations for t in self.turns),
                "reviews": [{"slideId": s, "passed": p} for s, p in self.reviews]}


def _validate(req: DesignRequest) -> JSON | None:
    if (req.spec is None) == (req.image is None):
        raise PipelineError("Give exactly one of a spec or an image.")
    if req.image is not None:
        if Path(req.image_name or "").suffix.lower() not in IMAGE_TYPES:
            raise PipelineError("The image must be a PNG, JPEG or WebP.")
        if not req.image or len(req.image) > MAX_IMAGE_BYTES:
            raise PipelineError("The image is empty or too large.")
        return None
    try:
        return spec_mod.normalize_spec(req.spec or {})
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc


def design_and_export(req: DesignRequest, work_dir: Path, *, settings: AISettings | None = None,
                      resolve: ProviderResolver | None = None, services: DesignServices | None = None,
                      renderer: Renderer | None = None, should_stop: Callable[[], bool] | None = None,
                      on_event: Callable[[JSON], None] | None = None) -> PipelineResult:
    """Design one slide and export it. `work_dir` is the job workspace's scratch (created, must be
    empty); the `.pptx` is written to `work_dir/out/slide.pptx` and the design deck, packed, to
    `work_dir/out/deck.zip` (both persisted through the storage port by the job workspace)."""
    s = settings if settings is not None else ai_settings
    spec = _validate(req)
    brand = req.brand
    if spec is not None and not brand.heading:
        brand.heading = spec["slide"]["title"]
    archetype = ((brand.layout_archetype or (spec_mod.spec_layout_archetype(spec) if spec else None) or "content")
                 .strip().lower())
    if archetype not in ("cover", "divider", "content"):
        raise PipelineError("The layout archetype must be cover, divider or content.")

    deck = DesignDeck.create(Path(work_dir) / "deck", req.deck_name, model=req.model or s.llm_design_model)
    if brand.workzone is not None and archetype == "content" and not wz.is_degenerate(brand.workzone):
        deck.update(lambda m: m.update(workzone=brand.workzone.to_json() if brand.workzone else None))
    else:
        brand.workzone = None
    try:
        prepared = masters.prepare_brand_master(deck, req.furniture, brand, archetype, renderer=renderer)
    except (masters.MasterError, engine_service.MasterImportFailed) as exc:
        raise PipelineError(str(exc)) from exc
    layout_id = str(prepared["layoutId"])
    canvas_w, canvas_h = deck.canvas()

    ctx = DesignContext(deck=deck, settings=s, resolve=resolve, services=services, model=req.model,
                        should_stop=should_stop)
    turns: list[DesignTurnResult] = []
    if s.design_stub:
        sid = stub_slide(deck, brand, layout_id, canvas_w, canvas_h)
    else:
        if spec is not None:
            instruction = spec_mod.spec_instruction(spec, brand, archetype, canvas_w, canvas_h, dense=req.dense,
                                                    brief_on=ctx.brief_on)
            files: list[Path] = []
        else:
            instruction = spec_mod.image_instruction(brand, canvas_w, canvas_h)
            files = [deck.attach(req.image_name or "slide.png", req.image or b"")]
        instruction += f"\n\nSave it on the layout with layout_id \"{layout_id}\"."
        first = run_design_turn(ctx, instruction, files, on_event=on_event)
        turns.append(first)
        if not first.touched:
            raise PipelineError(first.error or "The design turn saved no slide.")
        sid = first.touched[-1]
        if brand.workzone is not None:
            open_findings = workzone_check(ctx, sid)
            if open_findings:
                repair = run_design_turn(ctx, spec_mod.workzone_repair_instruction(sid, open_findings), [], sid,
                                         on_event=on_event)
                turns.append(repair)

    try:
        outcome = engine_service.export_slides(deck, Path(work_dir) / "export", slide_ids=[sid])
    except Exception as exc:  # noqa: BLE001 - the export failed: the job fails with a safe message
        _log.warning("export failed", exc_info=True)
        raise PipelineError(f"The export failed ({type(exc).__name__}).") from exc
    out = Path(work_dir) / "out"
    out.mkdir(parents=True, exist_ok=True)
    target = out / "slide.pptx"
    shutil.copyfile(outcome.pptx, target)
    # the designed deck itself, so a later job can stitch or refine it (app/core/design/bundle.py)
    (out / "deck.zip").write_bytes(bundle.pack(deck))

    flags = list(outcome.review_flags.get(sid, []))
    flags += review_findings(ctx, sid)
    cost = sum(t.cost_usd for t in turns)
    return PipelineResult(pptx=target, deck_dir=deck.dir, slide_id=sid, archetype=archetype,
                          master_mode=str(prepared["mode"]), element_count=outcome.element_count,
                          review_flag_count=len(flags), review_flags=flags, cost_usd=round(cost, 6),
                          design_cost_usd=round(sum(t.design_cost_usd for t in turns), 6),
                          critic_cost_usd=round(sum(t.critic_cost_usd for t in turns), 6),
                          template_background=bool(prepared["templateBackground"]), turns=turns,
                          reviews=[r for t in turns for r in t.reviews])


def workzone_check(ctx: DesignContext, sid: str) -> list[JSON]:
    """The workzone and header-band findings still open on the slide: measured when a reviewer is
    available, else from the HTML's declared boxes."""
    return [f for f in review_findings(ctx, sid) if f.get("rule") in ("workzone-overflow", "header-band-overlap")]


def review_findings(ctx: DesignContext, sid: str) -> list[JSON]:
    """The design review's open findings (measured), or the static workzone check without a browser."""
    review = ctx.svc.review
    if review is not None:
        try:
            return [f for f in review(ctx.deck, sid) or [] if f.get("level") in ("error", "warn")]
        except Exception:  # noqa: BLE001 - a reviewer that fails falls back to the static check
            _log.warning("design review failed; using the static workzone check", exc_info=True)
    zone = wz.normalize_bounds(ctx.deck.meta().get("workzone"))
    if zone is None:
        return []
    w, h = ctx.deck.canvas()
    return workzone_findings_static(ctx.deck.read_slide(sid), zone, w, h)


def stub_slide(deck: DesignDeck, brand: BrandOptions, layout_id: str, canvas_w: int, canvas_h: int) -> str:
    """A $0 hand-authored slide, so the whole path can be exercised without a paid call."""
    zone = brand.workzone
    box = (wz.container_style(zone, canvas_w, canvas_h) if zone is not None
           else f"position:absolute;left:64px;top:150px;width:{canvas_w - 128}px")
    title = html_lib.escape(brand.heading or "Stub slide")
    html = (f"<!doctype html><html><head><meta charset=\"utf-8\"><style>"
            f"html,body{{margin:0;width:{canvas_w}px;height:{canvas_h}px;overflow:hidden;background:transparent;"
            f"font-family:Arial,sans-serif}}"
            f"h1{{position:absolute;left:64px;top:40px;width:{canvas_w - 128}px;margin:0;font-size:26px;"
            f"color:#1F2A44;font-weight:700}}"
            f".z{{{box};box-sizing:border-box}}p{{margin:0;font-size:15px;color:#222}}"
            f"</style></head><body><h1 data-placeholder=\"title\">{title}</h1>"
            f"<div class=\"z\"><p>Designed by the $0 stub path (DESIGN_STUB): no model was called.</p></div>"
            f"</body></html>")
    return str(deck.save_slide(html, brand.heading or "Stub slide", layout_id, "stub design", source="blank")["id"])
