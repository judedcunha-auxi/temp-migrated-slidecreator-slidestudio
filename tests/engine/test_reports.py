"""Every report type named in master brief §8 exists here, carries its fields, and serialises.

This is the IR-freeze check in test form: if a package brief names `GateReport.slides[].defect_area`
and it is not here, the mismatch shows up now rather than at 4 a.m. when two agents have each
invented their own version.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.engine.ir import IR, Canvas, Slide
from app.engine.ir import Box as IRBox
from app.engine.reports import (
    Box,
    CoverageReport,
    DeterminismReport,
    EmitOptions,
    EmitReport,
    ExportOptions,
    ExportResult,
    FitIssue,
    FitReport,
    GateReport,
    GateSlide,
    LintFinding,
    LintReport,
    SuiteReport,
    Timing,
    ValidationReport,
)


def test_export_options_carries_every_field_the_briefs_name(tmp_path: Path):
    seen: list[dict] = []
    options = ExportOptions(
        out_dir=tmp_path, slide_ids=["sld_a"], verify=True, on_event=seen.append,
        emit=EmitOptions(), workspace=tmp_path / "ws",
    )
    assert options.work_dir == tmp_path / "ws"
    assert ExportOptions(out_dir=tmp_path).work_dir == tmp_path / "work"
    options.event(type="status", message="hello")
    assert seen == [{"type": "status", "message": "hello"}]

    # A missing listener must be a no-op, not a crash: the CLI passes none.
    ExportOptions(out_dir=tmp_path).event(type="status")


def test_emit_options_defaults_are_the_measured_configuration():
    options = EmitOptions()
    assert (options.line_lock, options.width_lock, options.theme_fonts) == (True, True, True)
    assert (options.group_svg, options.name_shapes, options.finalize) == (True, True, True)


def test_emit_report_fields_and_json():
    report = EmitReport(
        shapes_by_kind={"shape": 9, "text": 12},
        rasters=[{"id": "e77", "reason": "svg filter"}],
        charts=[{"id": "e41", "origin": "recognised"}],
        placeholders_used=[{"slide": "sld_a", "type": "title", "idx": 0}],
        warnings=["something"],
        renderer_gaps=["custDash drawn solid"],
    )
    assert report.to_json()["shapes_by_kind"]["text"] == 12
    assert json.dumps(report.to_json())


def test_export_result_json_is_serialisable_despite_holding_irs_and_a_callable(tmp_path: Path):
    ir = IR(canvas=Canvas(1280, 720), slide=Slide(id="sld_a", layoutId="layout-01"))
    result = ExportResult(
        pptx=tmp_path / "deck.pptx", irs=[ir], emit_report=EmitReport(), slide_ids=["sld_a"],
        elapsed_s=3.14159, per_slide_s={"sld_a": 1.5}, cost=0.0, renderer_warnings=["w"],
        rerun=lambda target: result,
    )
    data = result.to_json()
    assert data["elapsedS"] == 3.142 and data["perSlideS"] == {"sld_a": 1.5}
    assert json.dumps(data)


def test_lint_report_separates_errors_from_warnings():
    report = LintReport(
        slide_id="sld_a",
        findings=[
            LintFinding(level="error", rule="remote-resource", message="https://example.com/x.png"),
            LintFinding(level="warn", rule="google-fonts", message="@import"),
        ],
    )
    assert len(report.errors) == 1 and len(report.warnings) == 1
    assert report.clean is False


def test_fit_report_fields():
    report = FitReport(
        issues=[FitIssue(slide=1, shape="Title 1", kind="zero-width", detail="w=0")],
        slides_checked=2,
        details=[{"slide": 1, "predicted": 3, "ir": 2}],
    )
    assert report.clean is False and report.slides_checked == 2
    assert json.dumps(report.to_json())


def test_gate_report_shape_matches_the_briefs(tmp_path: Path):
    report = GateReport(
        slides=[
            GateSlide(index=1, defect_area=39584, clean=False,
                      components=[Box(10, 20, 30, 40)], diff_png=tmp_path / "d.png",
                      non_chart_area=12000),
            GateSlide(index=2, defect_area=0, clean=True),
        ],
        slides_checked=2, total_area=39584, settings={"blur": 2.0}, source="cli",
    )
    assert report.clean_slides == 1
    data = report.to_json()
    assert data["slides"][0]["defect_area"] == 39584
    assert data["slides"][0]["components"][0]["w"] == 30
    assert data["slides"][0]["diff_png"].endswith("d.png")
    assert json.dumps(data)


def test_coverage_report_clean_requires_no_rasters_and_no_warnings():
    assert CoverageReport().clean is True
    assert CoverageReport(rasters=[{"id": "e1"}]).clean is False
    assert CoverageReport(renderer_warnings_ours=["w"]).clean is False
    assert CoverageReport(template={"ok": False}).clean is False


def test_coverage_report_clean_requires_an_empty_text_row():
    """Every list in `text` is a check's findings; any finding makes coverage unclean."""
    empty = {"checked": True, "collisions": [], "exportOverlaps": [], "unmatched": 2, "skipped": 1}
    assert CoverageReport(text=empty).clean is True, "counters are not findings"
    collision = {"slide": 1, "shape": "chip", "paragraph": 0, "line": 0,
                 "joins": [{"left": "partner", "right": "2023"}], "gapPx": 6.0, "method": "boxes"}
    assert CoverageReport(text={**empty, "collisions": [collision]}).clean is False
    overlap = {"slide": 1, "a": "title", "b": "standfirst", "sharedPx": 9.0, "designSharedPx": 0.0}
    assert CoverageReport(text={**empty, "exportOverlaps": [overlap]}).clean is False
    # A check added to the dict later (B's rows/positions/seams) counts with no edit to reports.py.
    assert CoverageReport(text={**empty, "rows": [{"slide": 1}]}).clean is False


def test_suite_report_passed_requires_rows_to_exist():
    report = SuiteReport()
    assert report.passed is False, "a suite with no checks has not passed anything"
    report.gate_results = {"coverage": True, "fit": True}
    assert report.passed is True
    report.gate_results["gate"] = False
    assert report.passed is False


def test_suite_report_markdown_covers_every_section(tmp_path: Path):
    report = SuiteReport(
        project_id="prj_test", pptx=tmp_path / "deck.pptx", slide_ids=["a", "b"],
        lint=[LintReport(findings=[LintFinding(level="error", rule="script", message="no js")])],
        fit=FitReport(issues=[FitIssue(1, "Title 1", "overflow", "2 lines over")], slides_checked=2),
        gate=GateReport(slides=[GateSlide(index=1, defect_area=1234, clean=False, non_chart_area=1000)],
                        slides_checked=1, total_area=1234),
        coverage=CoverageReport(rasters=[{"id": "e7", "reason": "svg filter"}], text={
            "checked": True, "unmatched": 0, "skipped": 0,
            "collisions": [{"slide": 2, "shape": "note", "paragraph": 0, "line": 0,
                            "joins": [{"left": "partner", "right": "2023"}], "gapPx": 6.5,
                            "method": "predicted"}],
            "exportOverlaps": [{"slide": 1, "a": "title", "b": "standfirst", "sharedPx": 8.0,
                                "designSharedPx": 0.0}],
            "seams": [{"slide": 1, "element": "e4"}]}),
        validation=ValidationReport(ok=False, errors=["E1 bad rel"], elapsed_s=17.2),
        determinism=DeterminismReport(checked=True, identical=False, detail="sha differs"),
        timing=Timing(extract=1.0, classify=0.1, emit=2.0, verify=20.0, per_slide_export=1.5, total=23.1),
        cost=0.0,
        gate_results={"coverage": False, "fit": False},
        notes=["a note"],
    )
    markdown = report.to_markdown()
    for expected in ["# Export report", "## Gate", "## Fit", "## Coverage", "## Lint",
                     "## Validation", "## Determinism", "## Timing", "## Notes",
                     "1,234", "E1 bad rel", "FAIL",
                     "## Text — 1 collision(s), 1 export-introduced overlap(s), 1 seams",
                     "'partner' + '2023'", "'title' and 'standfirst'"]:
        assert expected in markdown, f"{expected!r} missing from report.md"


def test_suite_report_json_round_trips_through_json_dumps():
    report = SuiteReport(gate=GateReport(slides=[GateSlide(index=1)]), timing=Timing(total=1.0))
    assert json.loads(json.dumps(report.to_json()))["timing"]["total"] == 1.0


def test_ir_box_and_report_box_are_deliberately_separate():
    """`ir.Box` is canvas geometry; `reports.Box` is a render-pixel rectangle in a report."""
    assert IRBox(0, 0, 1, 1).to_json() == {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
    assert Box(0, 0, 1, 1).w == 1
