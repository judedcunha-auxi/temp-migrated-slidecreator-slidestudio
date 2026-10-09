"""The acceptance suite: one deck, every check in master brief §9, one `report.json` + `report.md`.

`run_suite` **never exports**: verification that exports, or export that verifies, makes both slow.
It takes either an `ExportResult` (which already carries the IRs and the emit report) or a `pptx=`
path (verify-only: an existing file, with the IRs re-derived from the slide HTML by running
extract -> classify).

The pixel gate needs a `Renderer` that serves `render` and `verify` (a local PptxRender build); with
none, or with the deployed service, the gate row reports that it did not run. CI and staging only.

The steps run in order of cost, so a cheap failure shows before the expensive checks: lint (ms) →
fit (ms) → references (a browser render per slide) → gate (two renderer passes) → coverage and
template (ms) → validator (~17 s) → determinism (two exports).

Reading the numbers:

* `gate.slides[].defect_area` is the whole defect area; `non_chart_area` is the same slide with the
  chart frames and the `sldNum`/`dt`/`ftr` boxes masked out of *both* images. The acceptance row
  reads the masked number, and both are printed side by side so a drop can be attributed.
* Rows appear in `gate_results` only when they actually ran, and `SuiteReport.passed` is false for
  an empty table on purpose: "nothing was checked" is not a pass.
"""
from __future__ import annotations

import hashlib
import json

# runs the optional OOXML validator, a fixed argument list (bandit B404)
import subprocess  # nosec B404
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from app.config import engine as config
from app.engine.ir import IR
from app.engine.manifest import Manifest
from app.engine.renderer import Renderer, RendererError
from app.engine.reports import (
    Box,
    CoverageReport,
    DeterminismReport,
    ExportResult,
    GateReport,
    SuiteReport,
    Timing,
    ValidationReport,
)
from app.engine.verify.coverage import baseline_deck, coverage
from app.engine.verify.fit import fit
from app.engine.verify.gate import gate
from app.engine.verify.lint import lint


@dataclass(slots=True)
class _Inputs:
    """Everything the suite needs to know about the deck under test, resolved once."""

    deck: Path | None
    irs: list[IR] = field(default_factory=list)
    emit_report: Any = None
    manifest: Manifest | None = None
    master: Path | None = None
    assets: Path | None = None
    project_dir: Path | None = None
    slides: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    extract_s: float = 0.0
    classify_s: float = 0.0


# ------------------------------------------------------------------------------ input resolution


def _load_source(project_dir: Path | None) -> tuple[Any, str | None]:
    """The project behind this deck, or None with the reason when there is none."""
    from app.engine.pipeline import PipelineError, load_project

    if project_dir is None:
        return None, "no project_dir: lint, references and the gate cannot run"
    try:
        return load_project(project_dir), None
    except (PipelineError, OSError) as error:
        return None, f"the project could not be loaded ({error}): lint, references and the gate cannot run"


def _extract_irs(
    source: Any, slides: Sequence[dict[str, Any]], workspace: Path
) -> tuple[list[IR], float, float]:
    """Verify-only: measure the HTML the deck was built from, so fit and coverage have an IR.

    Measurement only (extract -> classify). It never exports.
    """
    from app.engine.classify.charts import recognise_charts
    from app.engine.classify.placeholders import map_placeholders
    from app.engine.extract.html import extract_html, measuring_page
    from app.engine.ir import Canvas

    canvas = Canvas(source.manifest.canvas_w, source.manifest.canvas_h)
    irs: list[IR] = []
    extract_s = classify_s = 0.0
    with measuring_page(canvas) as page:
        for slide in slides:
            started = time.perf_counter()
            ir = extract_html(
                slide["html"], source.manifest, slide["layoutId"], source.assets,
                workspace=workspace, slide_id=slide["slideId"], title=slide["title"], page=page,
            )
            extract_s += time.perf_counter() - started
            started = time.perf_counter()
            ir = recognise_charts(ir)
            if ir.slide.layoutId:
                ir = map_placeholders(ir, source.manifest.layout(ir.slide.layoutId))
            classify_s += time.perf_counter() - started
            irs.append(ir)
    return irs, extract_s, classify_s


def _resolve(
    result: ExportResult | None,
    pptx: Path | None,
    project_dir: Path | None,
    workspace: Path,
) -> _Inputs:
    """Deck, IRs, manifest, master and slide HTML, from an `ExportResult` or from the project."""
    inputs = _Inputs(deck=Path(pptx) if pptx is not None else (result.pptx if result else None))
    source, problem = _load_source(project_dir)
    if problem:
        inputs.notes.append(problem)
    if source is None:
        if result:
            inputs.irs = list(result.irs)
            inputs.emit_report = result.emit_report
        return inputs

    inputs.manifest = source.manifest
    inputs.master = source.master
    inputs.assets = source.assets
    inputs.project_dir = source.dir
    wanted = list(result.slide_ids) if result and result.slide_ids else None
    inputs.slides = source.slides(wanted)

    if result is not None and result.irs:
        inputs.irs = list(result.irs)
        inputs.emit_report = result.emit_report
    else:
        inputs.irs, inputs.extract_s, inputs.classify_s = _extract_irs(source, inputs.slides, workspace)
        inputs.notes.append(
            "verify-only run: the IRs were measured from the slide HTML (extract -> classify), so the "
            "coverage and chart rows describe what the engine sees in this design, not what the deck "
            "under test contains"
        )
    return inputs


# ------------------------------------------------------------------------------------ references


def build_references(inputs: _Inputs, out_dir: Path) -> list[Path]:
    """One browser composite per slide: the layout PNG with the slide's HTML painted over it.

    This is what the candidate is scored against, so it has to be the picture the app shows — the
    PptxRender-rendered layout background underneath, the transparent slide on top, at canvas size.
    """
    from app.engine.extract.html import render_reference, title_zone_of

    if inputs.manifest is None or not inputs.slides:
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    scripts = (config.CHART_PREVIEW_JS,) if config.CHART_PREVIEW_JS.exists() else ()
    if not scripts:
        inputs.notes.append(
            f"no chart preview script at {config.CHART_PREVIEW_JS} (WP3b): a `data-chart` element is "
            f"blank in the reference, which is harmless only because its frame is masked in the gate"
        )
    references: list[Path] = []
    for index, slide in enumerate(inputs.slides, start=1):
        layout = (
            inputs.manifest.layout(slide["layoutId"]) if slide["layoutId"] else inputs.manifest.layouts[0]
        )
        if not layout.background or inputs.project_dir is None:
            inputs.notes.append(f"layout {layout.id} has no background render: slide {index} not scored")
            break
        layout_png = Path(inputs.project_dir) / "layouts" / layout.background
        if not layout_png.exists():
            inputs.notes.append(f"missing layout background {layout_png}: slide {index} not scored")
            break
        references.append(
            render_reference(
                slide["html"], layout_png, out_dir / f"slide-{index:02d}.png",
                assets_dir=inputs.assets or Path("."), fonts=inputs.manifest.fonts, scripts=scripts,
                title_zone=title_zone_of(inputs.manifest, slide["layoutId"]),
            )
        )
    return references


def build_masks(inputs: _Inputs) -> list[list[Box]]:
    """Per slide: the boxes painted out of both images before the gate compares them.

    Two sources, for the two reasons in master brief §9 — chart frames (two different chart painters
    will never agree pixel for pixel, so charts are checked structurally instead) and the
    `sldNum`/`dt`/`ftr` placeholder zones (the layout PNG carries the layout's own page number).

    Chart frames come from the IR *and* from the deck: a Path A export's frames are the IR's chart
    elements, while a Path B deck was built by Claude and its chart frames exist only in the file.
    """
    from pptx import Presentation

    deck_frames: list[list[Box]] = []
    if inputs.deck and Path(inputs.deck).exists():
        for slide in Presentation(str(inputs.deck)).slides:
            deck_frames.append([
                Box(shape.left / config.EMU_PER_PX, shape.top / config.EMU_PER_PX,
                    (shape.width or 0) / config.EMU_PER_PX, (shape.height or 0) / config.EMU_PER_PX)
                for shape in slide.shapes
                if getattr(shape, "has_chart", False) and shape.left is not None
            ])

    per_slide: list[list[Box]] = []
    for index in range(max(len(inputs.irs), len(deck_frames))):
        boxes: list[Box] = []
        if index < len(inputs.irs):
            boxes += masks_for(inputs.irs[index], inputs.manifest)
        if index < len(deck_frames):
            boxes += deck_frames[index]
        per_slide.append(_dedupe(boxes))
    return per_slide


def masks_for(ir: IR, manifest: Manifest | None) -> list[Box]:
    """The masks one slide's IR implies: its chart frames and its layout's `sldNum`/`dt`/`ftr` zones.

    Factored out of `build_masks` so the torture runner and the bench mask a single slide exactly as
    the suite does; `build_masks` adds the deck's own chart frames on top (a Path B deck's charts
    exist only in the file).
    """
    boxes = [Box(e.box.x, e.box.y, e.box.w, e.box.h) for e in ir.elements if e.kind == "chart"]
    if manifest is not None and ir.slide.layoutId:
        layout = next((lay for lay in manifest.layouts if lay.id == ir.slide.layoutId), None)
        if layout is not None:
            boxes += [Box(b.x, b.y, b.w, b.h) for b in layout.masked_boxes()]
    return _dedupe(boxes)


def _dedupe(boxes: Sequence[Box]) -> list[Box]:
    """Drop boxes that are the same rectangle twice — the IR and the deck agree on a chart frame."""
    kept: list[Box] = []
    for box in boxes:
        if box.w <= 0 or box.h <= 0:
            continue
        if any(
            abs(box.x - other.x) < 2 and abs(box.y - other.y) < 2
            and abs(box.w - other.w) < 2 and abs(box.h - other.h) < 2
            for other in kept
        ):
            continue
        kept.append(box)
    return kept


# ------------------------------------------------------------------------------------- validator


def run_validator(pptx: Path, master: Path | None, *, timeout_s: float = 300.0) -> ValidationReport:
    """`validate.py deck.pptx --original master.pptx` — the pptx skill's OOXML validator.

    Slow (~17 s on an 18 MB client master) and worth it: it is the check that catches the package
    defects PowerPoint answers with a silent repair, and a silent repair drops shapes.
    """
    report = ValidationReport()
    script = config.VALIDATE_PY
    if not script.exists():
        report.skipped = f"the validator was not found at {script}"
        return report
    command = [sys.executable, str(script), str(Path(pptx).resolve())]
    if master is not None and Path(master).exists():
        command += ["--original", str(Path(master).resolve())]
    started = time.perf_counter()
    try:
        # cwd is the script's own directory: it imports `helpers` and `validators` from beside it.
        # no shell; the configured validator and two paths (bandit B603)
        completed = subprocess.run(  # nosec B603
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout_s, cwd=str(script.parent),
        )
    except subprocess.TimeoutExpired:
        report.ok = False
        report.elapsed_s = round(time.perf_counter() - started, 3)
        report.errors.append(f"the validator did not finish within {timeout_s:.0f}s")
        return report

    report.elapsed_s = round(time.perf_counter() - started, 3)
    report.ok = completed.returncode == 0
    lines = [
        line.strip()
        for line in ((completed.stdout or "") + (completed.stderr or "")).splitlines()
        if line.strip()
    ]
    if report.ok:
        report.warnings = [line for line in lines if line.lower().startswith(("note:", "warning"))]
    else:
        report.errors = [line for line in lines if not line.startswith("All validations")]
    return report


# ----------------------------------------------------------------------------------- determinism


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_determinism(result: ExportResult | None, out_dir: Path) -> DeterminismReport:
    """Export the same input twice and compare sha256 (master brief §9, "Determinism")."""
    report = DeterminismReport()
    if result is None or result.rerun is None:
        report.detail = "the caller supplied no rerun hook, so the export could not be repeated"
        return report
    report.checked = True
    first = result.rerun(out_dir / "a").pptx
    second = result.rerun(out_dir / "b").pptx
    report.sha256 = [_sha256(first), _sha256(second)]
    report.identical = report.sha256[0] == report.sha256[1]
    report.detail = (
        "two exports of the same input are byte-identical"
        if report.identical
        else f"the two exports differ ({first.name}: {report.sha256[0][:12]}…, "
             f"{second.name}: {report.sha256[1][:12]}…)"
    )
    return report


# ------------------------------------------------------------------------------ renderer warnings


def _warnings_from(out_dir: Path) -> list[str]:
    """The warning list `Renderer.render` wrote beside the PNGs."""
    path = Path(out_dir) / "warnings.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    warnings = list(data.get("warnings") or [])
    if data.get("truncated"):
        warnings.append(
            f"(the renderer hid {data.get('hidden')} further warnings)"
        )
    return warnings


def _baseline_warnings(
    inputs: _Inputs, out_dir: Path, renderer: Renderer | None
) -> tuple[list[str], list[str]]:
    """What the master warns about on its own: one empty slide per layout the deck actually uses."""
    if renderer is None:
        return [], ["no renderer: renderer warnings could not be attributed"]
    if inputs.manifest is None or inputs.master is None or not inputs.slides:
        return [], ["no master or manifest: renderer warnings could not be attributed"]
    layout_ids = [s["layoutId"] for s in inputs.slides if s.get("layoutId")]
    if not layout_ids:
        return [], ["no layout ids: renderer warnings could not be attributed"]
    try:
        deck = baseline_deck(inputs.master, layout_ids, inputs.manifest, out_dir / "baseline.pptx")
        renderer.render(deck, inputs.manifest.canvas_w, out_dir)
    except Exception as error:  # noqa: BLE001 — the baseline is a comparison aid, not the product
        return [], [f"the baseline deck could not be rendered ({error}): warnings are unattributed"]
    return _warnings_from(out_dir), []


# -------------------------------------------------------------------------------------- the suite


def run_suite(
    result: ExportResult | None,
    out_dir: Path,
    *,
    pptx: Path | None = None,
    project_dir: Path | None = None,
    renderer: Renderer | None = None,
    baseline: Path | None = None,
    validate: bool = True,
    determinism: bool = False,
) -> SuiteReport:
    """Verify one deck and write `report.json`, `report.md` and `compare-NN.png` into `out_dir`.

    The four keyword arguments of master brief §8 behave exactly as specified. `validate` and
    `determinism` are additive switches for the two steps that cost real time. `renderer` is needed
    for the pixel gate and the renderer-warning rows; without it they report that they did not run.
    Measuring scratch files go under `out_dir/work`.
    """
    started = time.perf_counter()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    inputs = _resolve(result, pptx, project_dir, out_dir / "work")
    report = SuiteReport(
        project_id=project_dir.name if project_dir is not None else None,
        pptx=inputs.deck,
        slide_ids=[ir.slide.id for ir in inputs.irs] or (list(result.slide_ids) if result else []),
        cost=result.cost if result else 0.0,
    )
    report.notes.extend(inputs.notes)

    if inputs.deck is None or not Path(inputs.deck).exists():
        report.notes.append("no deck to verify: pass an ExportResult or pptx=")
        report.timing = Timing(total=round(time.perf_counter() - started, 3))
        write_reports(report, out_dir)
        return report

    # -- lint ---------------------------------------------------------------------------------------
    for slide in inputs.slides:
        html = Path(slide["html"]).read_text(encoding="utf-8", errors="replace")
        lint_report = lint(html, inputs.manifest, layout_id=slide.get("layoutId") or None)
        lint_report.slide_id = slide["slideId"]
        report.lint.append(lint_report)

    # -- fit ----------------------------------------------------------------------------------------
    report.fit = fit(inputs.deck, inputs.irs)

    # -- references and the pixel gate ----------------------------------------------------------------
    gate_started = time.perf_counter()
    references = build_references(inputs, out_dir / "reference")
    masks = build_masks(inputs)
    if references and renderer is None:
        report.gate = GateReport(source="none", warnings=["no renderer: the pixel gate did not run"])
        report.notes.append("no renderer: the pixel gate did not run")
    elif references and renderer is not None:
        try:
            report.gate = gate(inputs.deck, references, masks, out_dir, renderer=renderer)
        except RendererError as error:
            report.gate = GateReport(source="none", warnings=[str(error)])
            report.notes.append(f"the gate did not run: {error}")
    else:
        report.notes.append("no reference images could be built: the pixel gate did not run")

    # -- coverage, charts and the template --------------------------------------------------------------
    rendered_warnings = _warnings_from(out_dir / "render")
    if result is not None and result.renderer_warnings:
        rendered_warnings = sorted(set(rendered_warnings) | set(result.renderer_warnings))
    baseline_warnings, warning_notes = _baseline_warnings(inputs, out_dir / "baseline", renderer)
    report.notes.extend(warning_notes)
    report.coverage = coverage(
        inputs.irs, inputs.emit_report, rendered_warnings, baseline_warnings,
        pptx=inputs.deck, master=inputs.master, manifest=inputs.manifest,
    )

    # -- the validator -----------------------------------------------------------------------------
    report.validation = (
        run_validator(inputs.deck, inputs.master) if validate else ValidationReport(skipped="--no-validate")
    )

    # -- determinism -------------------------------------------------------------------------------
    if determinism:
        report.determinism = check_determinism(result, out_dir / "determinism")

    # Steps after `_resolve` (the reference build, mostly) add notes of their own to `inputs`.
    report.notes.extend(note for note in inputs.notes if note not in report.notes)

    # -- the acceptance table ------------------------------------------------------------------------
    report.timing = Timing(
        extract=round(inputs.extract_s, 3),
        classify=round(inputs.classify_s, 3),
        emit=0.0,
        verify=round(time.perf_counter() - gate_started, 3),
        per_slide_export=(result.elapsed_s / max(1, len(result.slide_ids))) if result else 0.0,
        total=round(time.perf_counter() - started, 3),
    )
    report.gate_results = _acceptance(report, inputs)
    if baseline is not None:
        report.baseline_delta = _against_baseline(report, baseline)

    write_reports(report, out_dir)
    return report


def _acceptance(report: SuiteReport, inputs: _Inputs) -> dict[str, bool]:
    """Master brief §9, row by row — and only the rows that actually ran."""
    rows: dict[str, bool] = {}
    coverage_report: CoverageReport | None = report.coverage

    if coverage_report is not None:
        rows["coverage"] = not coverage_report.rasters
        if coverage_report.charts:
            rows["charts"] = all(bool(entry.get("ok")) for entry in coverage_report.charts)
        if coverage_report.template.get("checked"):
            rows["template"] = bool(coverage_report.template.get("ok"))
        # The text row reads the file against the IRs (`text_deck`); it runs whenever coverage was
        # given the deck, master or not.
        text = coverage_report.text
        if text.get("checked"):
            rows["text"] = (bool(text.get("checked"))
                            and not text.get("collisions")
                            and not text.get("exportOverlaps"))
            rows["text"] = rows["text"] and not (text.get("rows") or text.get("positions") or text.get("seams"))

    if report.fit is not None:
        rows["fit"] = report.fit.clean

    if report.gate is not None and report.gate.slides:
        canvas_w = inputs.manifest.canvas_w if inputs.manifest else 1280
        canvas_h = inputs.manifest.canvas_h if inputs.manifest else 720
        target = config.gate_target_px2(canvas_w, canvas_h)
        worst = max((slide.non_chart_area or 0) for slide in report.gate.slides)
        if target is None:
            report.notes.append(
                f"the gate target is not set (config.GATE_TARGET_PX2_1280 is None), so the gate row "
                f"is measured but not judged: the worst slide is {worst:,} px² masked"
            )
        else:
            rows["gate"] = worst <= target
            report.notes.append(f"gate target {target:,.0f} px² per slide; worst slide {worst:,} px²")

    file_ok, ran_file_row = True, False
    if report.validation is not None and report.validation.skipped is None:
        file_ok, ran_file_row = file_ok and report.validation.ok, True
    if coverage_report is not None:
        file_ok, ran_file_row = file_ok and not coverage_report.renderer_warnings_ours, True
    if ran_file_row:
        rows["file"] = file_ok

    if report.timing.per_slide_export:
        rows["speed"] = report.timing.per_slide_export <= 10.0

    if report.determinism is not None and report.determinism.checked:
        rows["determinism"] = bool(report.determinism.identical)

    return rows


def _against_baseline(report: SuiteReport, baseline: Path) -> dict[str, Any]:
    """Per-slide deltas against a previous `report.json`: is this round better than the last one?"""
    try:
        previous = load_report(baseline)
    except (OSError, ValueError) as error:
        return {"error": f"the baseline {baseline} could not be read: {error}"}

    previous_slides = (previous.get("gate") or {}).get("slides") or []
    slides = []
    for index, slide in enumerate(report.gate.slides if report.gate else []):
        before = previous_slides[index] if index < len(previous_slides) else {}
        slides.append({
            "slide": slide.index,
            "defectArea": slide.defect_area,
            "wasDefectArea": before.get("defect_area"),
            "nonChartArea": slide.non_chart_area,
            "wasNonChartArea": before.get("non_chart_area"),
            "delta": ((slide.non_chart_area or 0) - (before.get("non_chart_area") or 0))
            if before else None,
        })
    return {
        "baseline": str(baseline),
        "slides": slides,
        "fitIssues": len(report.fit.issues) if report.fit else None,
        "wasFitIssues": len((previous.get("fit") or {}).get("issues") or []),
    }


def write_reports(report: SuiteReport, out_dir: Path) -> dict[str, Path]:
    """Write `report.json` and `report.md`; return where they went."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "report.json"
    markdown_path = out_dir / "report.md"
    json_path.write_text(json.dumps(report.to_json(), indent=2, ensure_ascii=False), encoding="utf-8")
    markdown_path.write_text(report.to_markdown(), encoding="utf-8")
    return {"json": json_path, "markdown": markdown_path}


def load_report(path: Path) -> dict[str, Any]:
    """Read a previous `report.json` — how `baseline=` compares two runs."""
    path = Path(path)
    if path.is_dir():
        path = path / "report.json"
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
