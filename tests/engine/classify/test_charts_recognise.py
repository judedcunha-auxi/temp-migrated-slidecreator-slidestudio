"""WP3c — the SVG chart recogniser.

Two bodies of evidence:

* `tests/engine/fixtures/torture/ir/svg-charts.json`, the hand-written IR of
  `tests/engine/fixtures/torture/svg-charts.html`: six figures that must become charts with exactly the
  numbers the drawing states, and two that must be refused.
* two composite slides built here (`composite_slide_one`, `composite_slide_two`): eight
  chart-shaped figures, invented for these tests, that put the recogniser's harder cases side by
  side.

The rule under test throughout: **never guess values.** Three of the eight composite figures are refused,
and that is the pass condition, not a shortfall.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.config import engine as config
from app.engine import charts_spec
from app.engine.classify.charts import ACCEPT, SUSPECT, parse_value, recognise_charts
from app.engine.ir import IR, Box, Canvas, Element, Fonts, Group, Slide, assign_ids_and_z

# ------------------------------------------------------------------------------- tiny IR builder
# Enough of WP2's output shape to exercise the recogniser: boxes in canvas px, one `source.svg`
# prefix per `<svg>` root, text as paragraphs → lines → runs. Text boxes use a coarse Arial metric
# (ascent 0.8 em, line box 1.16 em) because what the recogniser reads off a text is its box, and its
# tolerances are whole pixels.

INK, GREY, BLUE, SLATE, MIST, YELLOW, GREEN = (
    "#2E2E38", "#C4C4CD", "#1A9AFA", "#747480", "#E6E6EC", "#FFE600", "#2DB757",
)


def _advance(ch: str) -> float:
    if ch == " ":
        return 0.28
    if ch.isdigit():
        return 0.556
    if ch.isupper():
        return 0.68
    if ch.islower():
        return 0.52
    return 0.35


class Drawing:
    """One `<svg>` root, appending IR elements in paint order at a canvas offset."""

    def __init__(self, elements: list[Element], index: int, ox: float, oy: float):
        self.elements = elements
        self.key = f"svg#{index}"
        self.ox, self.oy = ox, oy
        self._child = 0

    def _source(self, tag: str) -> dict[str, Any]:
        self._child += 1
        return {"path": "body > div.card", "tag": tag, "svg": f"{self.key} > {tag}:nth-child({self._child})"}

    def rect(self, x, y, w, h, fill, opacity: float = 1.0) -> Element:
        element = Element(
            kind="shape", box=Box(self.ox + x, self.oy + y, w, h), opacity=opacity,
            source=self._source("rect"), geometry={"type": "rect"},
            fill={"type": "solid", "color": fill.lstrip("#").upper(), "alpha": 1.0},
        )
        self.elements.append(element)
        return element

    def outline(self, x, y, w, h, colour) -> Element:
        """An unfilled rect: a dashed highlight or a border, never a bar."""
        element = Element(
            kind="shape", box=Box(self.ox + x, self.oy + y, w, h), source=self._source("rect"),
            geometry={"type": "rect"}, fill={"type": "none"},
            stroke={"color": colour.lstrip("#").upper(), "alpha": 1.0, "width": 1.0, "dash": [3, 2]},
        )
        self.elements.append(element)
        return element

    def line(self, x1, y1, x2, y2, colour, width=1.0, dash="solid") -> Element:
        element = Element(
            kind="shape",
            box=Box(self.ox + min(x1, x2), self.oy + min(y1, y2), abs(x2 - x1), abs(y2 - y1)),
            source=self._source("line"),
            geometry={"type": "line",
                      "points": [[self.ox + x1, self.oy + y1], [self.ox + x2, self.oy + y2]]},
            fill={"type": "none"},
            stroke={"color": colour.lstrip("#").upper(), "alpha": 1.0, "width": width, "dash": dash},
        )
        self.elements.append(element)
        return element

    def ring(self, cx, cy, r, colour, width) -> Element:
        element = Element(
            kind="shape", box=Box(self.ox + cx - r, self.oy + cy - r, 2 * r, 2 * r),
            source=self._source("circle"), geometry={"type": "ellipse"}, fill={"type": "none"},
            stroke={"color": colour.lstrip("#").upper(), "alpha": 1.0, "width": width, "dash": "solid"},
        )
        self.elements.append(element)
        return element

    def arc(self, cx, cy, r_outer, r_inner, start, end, fill) -> Element:
        element = Element(
            kind="shape",
            box=Box(self.ox + cx - r_outer, self.oy + cy - r_outer, 2 * r_outer, 2 * r_outer),
            source=self._source("circle"),
            geometry={"type": "blockArc",
                      "arc": {"cx": self.ox + cx, "cy": self.oy + cy, "rOuter": r_outer,
                              "rInner": r_inner, "startDeg": start, "endDeg": end}},
            fill={"type": "solid", "color": fill.lstrip("#").upper(), "alpha": 1.0},
        )
        self.elements.append(element)
        return element

    def text(self, x, y, s, size=8.0, colour=INK, anchor="start", weight=400) -> Element:
        width = round(sum(_advance(c) for c in s) * size, 3)
        left = {"start": x, "middle": x - width / 2, "end": x - width}[anchor]
        box = Box(self.ox + left, self.oy + y - 0.8 * size, width, round(1.16 * size, 3))
        run = {"text": s, "font": "Arial", "sizePx": size, "weight": weight, "italic": False,
               "color": colour.lstrip("#").upper(), "alpha": 1.0}
        element = Element(
            kind="text", box=box, source=self._source("text"),
            paragraphs=[{"align": "left", "lineHeightPx": round(1.16 * size, 3),
                         "lines": [{"box": box.to_json(), "runs": [run]}]}],
            anchor="top", wrap=False,
        )
        self.elements.append(element)
        return element


def finish(elements: list[Element], slide_id: str, groups: list[Group] | None = None) -> IR:
    assign_ids_and_z(elements)
    ir = IR(canvas=Canvas(1280, 720), slide=Slide(id=slide_id, layoutId="layout-01"),
            elements=elements, groups=groups or [], fonts=Fonts(forced={"major": "Arial", "minor": "Arial"}))
    assert ir.validate(check_files=False) == [], ir.validate(check_files=False)
    return ir


def charts(ir: IR) -> list[Element]:
    return [e for e in ir.elements if e.kind == "chart"]


def values_of(chart: Element, series: int = 0) -> list[Any]:
    return chart.spec["series"][series]["values"]


# ------------------------------------------------------------------------------ torture fixture


@pytest.fixture(scope="module")
def torture_ir_path() -> Path:
    path = config.TORTURE_FIXTURE / "ir" / "svg-charts.json"
    assert path.exists(), f"{path} is missing — the svg-charts torture IR is a WP3c deliverable"
    return path


@pytest.fixture
def torture(torture_ir_path: Path) -> IR:
    return recognise_charts(IR.load(torture_ir_path))


def test_the_torture_page_yields_six_charts_and_two_refusals(torture: IR):
    kinds = torture.counts()
    assert kinds["chart"] == 6, [c.spec["type"] for c in charts(torture)]
    assert [c.spec["type"] for c in charts(torture)] == [
        "column", "column_stacked", "bar", "waterfall", "doughnut", "line_markers",
    ]
    refusals = [d for d in torture.diagnostics if d.message.startswith("possible chart")]
    assert len(refusals) == 2, [d.message for d in refusals]
    assert all(d.level == "info" for d in refusals)
    assert all("no numeric labels" in d.message for d in refusals)


def test_the_expect_file_matches_the_fixture(torture_ir_path: Path):
    """`expect.json` is what WP1/WP2 will have to reproduce from the HTML; it must match today."""
    expect = json.loads((config.TORTURE_FIXTURE / "svg-charts.expect.json").read_text(encoding="utf-8"))
    before = IR.load(torture_ir_path)
    assert before.counts() == expect["kinds"]
    after = recognise_charts(IR.load(torture_ir_path))
    assert len(charts(after)) == expect["charts"]["recognised"]
    assert expect["charts"]["authored"] == 0
    assert (config.TORTURE_FIXTURE / "svg-charts.html").exists()


def test_clustered_columns_read_every_label(torture: IR):
    chart = charts(torture)[0]
    assert chart.spec["categories"] == ["Q1", "Q2", "Q3", "Q4"]
    assert [s["name"] for s in chart.spec["series"]] == ["Plan", "Actual"], "series named from the legend"
    assert values_of(chart, 0) == [40, 60, 50, 70]
    assert values_of(chart, 1) == [55, 75, 85, 90]
    assert chart.spec["colors"] == ["#C4C4CD", "#1A9AFA"]
    assert chart.spec["options"]["valueAxis"] == {"min": 0, "max": 100, "visible": True, "majorUnit": 25}
    assert chart.spec["options"]["gapWidth"] == 73
    assert chart.spec["options"]["overlap"] == 0
    assert chart.spec["options"]["dataLabels"] is True
    assert chart.spec["options"]["labelPosition"] == "outEnd"
    assert chart.confidence == 1.0


def test_stacked_columns_keep_the_gap_slot_and_recoloured_points(torture: IR):
    chart = charts(torture)[1]
    assert chart.spec["categories"] == ["FY20", "FY21", "FY22", "FY23", "···", "FY27E"]
    assert values_of(chart, 0) == [40, 52, 57, 71, None, 88], "gap slot stays null"
    assert values_of(chart, 1) == [12, 9, 21, 18, None, 55]
    assert [s["name"] for s in chart.spec["series"]] == ["Internal", "Partner"]
    # The values came off the axis, so the design's stack totals are not data labels…
    assert chart.spec["options"]["dataLabels"] is False
    # …and they are still drawn, because PowerPoint cannot put a total over a stack.
    totals = [e for e in torture.elements if e.kind == "text" and e.id in (chart.overlay or [])]
    assert {"52", "61", "78", "89", "143"} <= {t.paragraphs[0]["lines"][0]["runs"][0]["text"] for t in totals}
    assert chart.spec["options"]["pointColors"] == {"1": {"3": "#1A9AFA", "5": "#1A9AFA"}}
    assert chart.spec["options"]["overlap"] == 100


def test_wrapped_bar_is_admitted_only_because_it_fits_the_scale(torture: IR):
    chart = charts(torture)[2]
    assert chart.spec["categories"] == ["Renewal", "Referral", "Upsell", "Business"]
    assert values_of(chart) == [38, 27, 22, 13]
    assert chart.spec["options"]["numberFormat"] == '"~"0"%"', "the design wrote ~38%, so must the chart"
    # Render check #4: the categories are listed top-down, so the chart reads top-down only with a
    # reversed category axis (a bar chart draws its first category at the bottom).
    assert chart.spec["options"]["categoryAxis"] == {"visible": True, "reverse": True}
    columns = [c for c in charts(torture) if c.spec["type"] != "bar"]
    assert all("reverse" not in (c.spec["options"].get("categoryAxis") or {}) for c in columns)


def test_waterfall_signs_its_steps_and_marks_its_totals(torture: IR):
    chart = charts(torture)[3]
    assert chart.spec["type"] == "waterfall"
    assert chart.spec["categories"] == ["2023", "Volume", "Mix", "Price", "2030"]
    assert values_of(chart) == [200, 60, -30, 50, 280]
    assert sum(values_of(chart)[:4]) == values_of(chart)[4], "the chain must reach the total"
    assert chart.extras["x-wp3c-chart"]["totalIndices"] == [0, 4]
    assert chart.spec["options"]["numberFormat"] == "0", "not every positive was signed, so no + section"


def test_doughnut_reads_the_legend_and_the_remainder_ring(torture: IR):
    chart = charts(torture)[4]
    assert chart.spec["type"] == "doughnut"
    assert values_of(chart) == [42, 58], "the ring is 100 % minus the labelled slice"
    assert chart.spec["categories"] == ["Partner (42%)", "Internal (58%)"]
    assert chart.spec["options"]["holeSize"] == 67
    assert chart.spec["options"]["legend"] is False
    assert chart.box == chart.plotRect, "a doughnut's frame is its own circle, not the legend's column"


def test_line_values_come_off_the_axis_exactly(torture: IR):
    chart = charts(torture)[5]
    assert chart.spec["type"] == "line_markers"
    assert values_of(chart) == [20, 35, 30, 55, 70]
    assert chart.spec["categories"] == ["Jan", "Feb", "Mar", "Apr", "May"]
    assert chart.spec["options"]["markers"] is True


def test_refused_panels_keep_every_shape(torture_ir_path: Path):
    before = IR.load(torture_ir_path)
    after = recognise_charts(IR.load(torture_ir_path))
    for scope in ("svg#7", "svg#8"):
        kept = {e.id for e in after.elements if ((e.source or {}).get("svg") or "").startswith(scope)}
        original = {e.id for e in before.elements if ((e.source or {}).get("svg") or "").startswith(scope)}
        assert kept == original, f"{scope} was refused, so nothing of it may disappear"


def test_every_recognised_spec_is_emittable_and_the_ir_still_validates(torture: IR):
    for chart in charts(torture):
        assert charts_spec.validate(chart.spec) == [], (chart.name, charts_spec.validate(chart.spec))
        assert chart.origin == "recognised"
        assert chart.plotRect is not None
        assert chart.confidence >= ACCEPT
    assert torture.validate(check_files=False) == []


def test_plot_rect_lands_on_the_drawing(torture: IR):
    """The acceptance gate allows 4 px between the design's plot rect and the chart's."""
    column, stacked = charts(torture)[0], charts(torture)[1]
    # Panel A: value axis 0..100 maps y=200..40, bands start at x=34 and run 4 × 60 px.
    assert column.plotRect.x == pytest.approx(16 + 34, abs=0.5)
    assert column.plotRect.y == pytest.approx(68 + 40, abs=0.5)
    assert column.plotRect.w == pytest.approx(240, abs=0.5)
    assert column.plotRect.h == pytest.approx(160, abs=0.5)
    # Panel B: six bands of 40 px including the gap slot.
    assert stacked.plotRect.w == pytest.approx(240, abs=0.5)
    assert stacked.plotRect.h == pytest.approx(160, abs=0.5)


def test_overlay_names_what_the_chart_cannot_draw(torture: IR):
    stacked = charts(torture)[1]
    overlay = {e.id for e in torture.elements if e.id in (stacked.overlay or [])}
    assert len(overlay) == 10, "axis rule + 5 stack totals + 2 legend swatches + 2 legend texts"
    assert all(e.kind != "chart" for e in torture.elements if e.id in overlay)


def test_recognition_is_deterministic(torture_ir_path: Path):
    first = recognise_charts(IR.load(torture_ir_path)).dumps()
    second = recognise_charts(IR.load(torture_ir_path)).dumps()
    assert first == second


def test_an_ir_with_no_svg_is_returned_untouched(torture_ir_path: Path):
    ir = IR.load(torture_ir_path)
    ir.elements = [e for e in ir.elements if not (e.source or {}).get("svg")]
    assign_ids_and_z(ir.elements)
    ir.groups = []
    before = ir.dumps()
    after = recognise_charts(ir)
    assert after.dumps() == before
    assert after.diagnostics == []


# ----------------------------------------------------------------------------- numeric grammar


@pytest.mark.parametrize(
    "text, number",
    [("52", 52.0), ("3.7", 3.7), ("+140", 140.0), ("-30", -30.0), ("−30", -30.0),
     ("~38%", 38.0), ("≈12 %", 12.0), ("143", 143.0), ("1,234", 1234.0), ("580", 580.0),
     ("18.6k", 18.6), ("250bn", 250.0), ("80k", 80.0), ("10%", 10.0)],
)
def test_the_grammar_accepts_written_numbers(text: str, number: float):
    parsed = parse_value(text)
    assert parsed is not None and parsed.number == pytest.approx(number)


@pytest.mark.parametrize(
    "text",
    ["FY27E", "350→420k", "Jun", "· · ·", "n/a", "~38% of total", "3.7 years", "Q1",
     "806 core (65%)", "", "2.4×", "10-12"],
)
def test_the_grammar_refuses_everything_else(text: str):
    assert parse_value(text) is None, f"{text!r} is an annotation, not a value"


def test_suffix_and_sign_survive_into_the_format():
    assert parse_value("~38%").suffix == "%"
    assert parse_value("~38%").approx is True
    assert parse_value("+150").signed is True
    assert parse_value("4.2").decimals == 1


# ----------------------------------------------------------------------- confidence thresholds


def _bar_row(widths: list[float], labels: list[str], named: bool) -> IR:
    """Four bars with labels, so the only thing under test is how well the two agree."""
    elements: list[Element] = []
    svg = Drawing(elements, 1, 0, 0)
    for index, (width, label) in enumerate(zip(widths, labels, strict=False)):
        y = 40 + 30 * index
        if named:
            svg.text(0, y + 8, f"Row {index}")
        svg.rect(70, y, width, 10, BLUE)
        svg.text(75 + width, y + 8, label, colour=SLATE)
    return finish(elements, "confidence")


def test_a_clean_bar_row_clears_the_accept_threshold():
    ir = recognise_charts(_bar_row([20, 40, 60, 80], ["10", "20", "30", "40"], named=True))
    assert len(charts(ir)) == 1
    assert charts(ir)[0].confidence >= ACCEPT


def test_a_row_whose_labels_only_roughly_match_is_kept_as_shapes_with_a_warning():
    ir = recognise_charts(_bar_row([20, 45, 30, 80], ["10", "20", "30", "40"], named=True))
    assert charts(ir) == []
    warnings = [d for d in ir.diagnostics if d.level == "warn"]
    assert len(warnings) == 1 and "possible chart" in warnings[0].message
    assert SUSPECT <= float(warnings[0].message.split("confidence ")[1].split(")")[0]) < ACCEPT


def test_a_row_whose_labels_contradict_the_drawing_says_nothing():
    ir = recognise_charts(_bar_row([60, 20, 80, 30], ["10", "20", "30", "40"], named=True))
    assert charts(ir) == []
    assert [d for d in ir.diagnostics if d.level in ("warn", "error")] == []


def test_a_bar_row_with_no_numbers_is_refused_with_an_info_diagnostic():
    ir = recognise_charts(_bar_row([20, 40, 60, 80], ["low", "mid", "high", "top"], named=True))
    assert charts(ir) == []
    infos = [d for d in ir.diagnostics if d.level == "info"]
    assert len(infos) == 1
    assert infos[0].message.startswith("possible chart, no numeric labels")


def test_identical_bars_are_not_a_chart():
    """A row of equal boxes is a legend or a band, not data; saying "possible chart" would be noise."""
    ir = recognise_charts(_bar_row([40, 40, 40, 40], ["a", "b", "c", "d"], named=False))
    assert charts(ir) == []
    assert ir.diagnostics == []


# ----------------------------------------------- composite slides (synthetic, invented for these tests)
# Two dense slides that put the recogniser's harder behaviours next to each other: a stacked column
# split by its axis with a faded forecast, a reference line and a legend; numberless lever bars;
# a waterfall that reads its own labels; doughnuts that take their percentages from the writing;
# a bar chart wrapped into a second column; and chart-shaped figures that state nothing.
# Every position, size and value below was invented for these tests.


def composite_slide_one() -> IR:
    """A stacked column (axis 0-100, 1.8 px a unit) beside four numberless lever bars."""
    elements: list[Element] = []

    growth = Drawing(elements, 1, 100, 280)
    for y in (200, 155, 110, 65, 20):
        growth.line(40, y, 640, y, MIST)
    for y, label in ((203, "0"), (158, "25"), (113, "50"), (68, "75"), (23, "100")):
        growth.text(34, y, label, 7.5, SLATE, "end")
    growth.line(40, 200, 640, 200, GREY, 1.2)
    # (x, core, growth, top colour, opacity): core and growth in units, 1.8 px each.
    for x, core, extra, colour, opacity in (
        (70, 30, 8, INK, 1.0), (170, 34, 12, INK, 1.0), (270, 41, 9, INK, 1.0),
        (370, 44, 18, BLUE, 1.0), (570, 50, 35, BLUE, 0.55),
    ):
        core_h, extra_h = core * 1.8, extra * 1.8
        growth.rect(x, 200 - core_h, 60, core_h, GREY, opacity)
        growth.rect(x, 200 - core_h - extra_h, 60, extra_h, colour, opacity)
    growth.outline(570, 47, 60, 153, BLUE)
    for x, y, label in ((100, 125.6, "38"), (200, 111.2, "46"), (300, 104, "50"),
                        (400, 82.4, "62"), (600, 41, "85")):
        growth.text(x, y, label, 8.5, INK, "middle", 700)
    growth.line(40, 65, 640, 65, SLATE, 1.0, dash=[4, 3])
    growth.text(636, 61, "Stretch goal of 75", 7.2, SLATE, "end")
    for x, label in ((100, "Q1"), (200, "Q2"), (300, "Q3"), (400, "Q4"), (600, "Plan")):
        growth.text(x, 214, label, 8.0, INK, "middle")
    growth.text(500, 214, "· · ·", 8.0, GREY, "middle")
    growth.rect(70, 222, 9, 9, BLUE)
    growth.text(83, 230, "Growth", 7.8, INK)
    growth.rect(150, 222, 9, 9, GREY)
    growth.text(163, 230, "Core", 7.8, INK)
    growth.text(640, 230, "Q2 restated after a method change", 7.4, SLATE, "end")

    levers = Drawing(elements, 2, 780, 280)
    levers.rect(0, 6, 440, 1, MIST)
    for y, width, colour, head, tag, note in (
        (20, 300, INK, "Pricing discipline", "Committed", "List prices reviewed every quarter"),
        (86, 220, BLUE, "Channel mix", "Largest lever", "More volume through partner channels"),
        (152, 150, SLATE, "Service attach", "Early", "Bundled support on every new contract"),
    ):
        levers.rect(0, y, width, 30, colour)
        levers.text(12, y + 20, head, 9.5, "#FFFFFF", weight=700)
        levers.text(width + 12, y + 20, tag, 9.5, colour, weight=700)
        levers.text(0, y + 44, note, 7.8, SLATE)
    levers.rect(0, 218, 96, 12, GREY)
    levers.text(104, 228, "Shown in order of expected effect", 8.0, INK)

    return finish(elements, "sld_composite_01")


def test_composite_slide_one_stacked_column_is_recognised_with_the_split_from_the_axis():
    ir = recognise_charts(composite_slide_one())
    found = charts(ir)
    assert len(found) == 1, "the stacked column is the only chart on slide one"
    chart = found[0]
    assert chart.spec["type"] == "column_stacked"
    assert chart.spec["categories"] == ["Q1", "Q2", "Q3", "Q4", "· · ·", "Plan"]
    assert values_of(chart, 0) == [30, 34, 41, 44, None, 50]             # core
    assert values_of(chart, 1) == [8, 12, 9, 18, None, 35]               # growth
    assert [s["name"] for s in chart.spec["series"]] == ["Core", "Growth"]
    assert chart.spec["options"]["valueAxis"]["max"] == 100
    # The growth segments are ink until Q4 and blue from Q4 on: the blue ones are point colours.
    assert chart.spec["options"]["pointColors"] == {"1": {"3": "#1A9AFA", "5": "#1A9AFA"}}
    assert chart.confidence >= ACCEPT
    # the totals the design wrote above each stack agree with the sum of the regressed segments
    for slot, total in zip((0, 1, 2, 3, 5), (38, 46, 50, 62, 85), strict=True):
        assert values_of(chart, 0)[slot] + values_of(chart, 1)[slot] == pytest.approx(total, abs=0.6)


def test_composite_slide_one_lever_bars_stay_shapes_because_nothing_is_numbered():
    ir = recognise_charts(composite_slide_one())
    refusals = [d for d in ir.diagnostics if d.message.startswith("possible chart")]
    assert len(refusals) == 1
    assert refusals[0].level == "info" and "no numeric labels" in refusals[0].message
    assert refusals[0].source.startswith("svg#2")


def test_composite_slide_one_keeps_the_reference_line_and_the_legend_on_top():
    ir = recognise_charts(composite_slide_one())
    chart = charts(ir)[0]
    overlay = [e for e in ir.elements if e.id in (chart.overlay or [])]
    texts = {t.paragraphs[0]["lines"][0]["runs"][0]["text"] for t in overlay if t.kind == "text"}
    assert "Stretch goal of 75" in texts
    assert {"Growth", "Core"} <= texts
    assert any(e.kind == "shape" and e.stroke and e.stroke.get("dash") == [4, 3] for e in overlay)


def test_composite_slide_one_reports_the_faded_forecast_bars():
    ir = recognise_charts(composite_slide_one())
    faded = [d for d in ir.diagnostics if "semi-transparent" in d.message]
    assert len(faded) == 1 and faded[0].level == "info"


def composite_slide_two() -> IR:
    """Six chart-shaped figures: four that state their numbers, two that state nothing."""
    elements: list[Element] = []

    # A waterfall on an axis of 0-800, 0.225 px a unit: 320 + 140 + 90 + 60 + 40 = 650.
    water = Drawing(elements, 1, 80, 230)
    for y in (200, 155, 110, 65, 20):
        water.line(40, y, 640, y, MIST)
    for y, label in ((203, "0"), (158, "200"), (113, "400"), (68, "600"), (23, "800")):
        water.text(34, y, label, 7.4, SLATE, "end")
    water.line(40, 200, 640, 200, GREY, 1.2)
    for x1, y, x2 in ((120, 128, 150), (220, 96.5, 250), (320, 76.25, 350),
                      (420, 62.75, 450), (520, 53.75, 550)):
        water.line(x1, y, x2, y, GREY, 1.0, dash=[3, 2])
    for x, y, h, colour in ((50, 128, 72, INK), (150, 96.5, 31.5, BLUE), (250, 76.25, 20.25, BLUE),
                            (350, 62.75, 13.5, BLUE), (450, 53.75, 9, SLATE), (550, 53.75, 146.25, YELLOW)):
        water.rect(x, y, 70, h, colour)
    water.outline(550, 53.75, 70, 146.25, INK)
    for x, y, label, colour in ((85, 122, "320", INK), (185, 90.5, "+140", BLUE), (285, 70.25, "+90", BLUE),
                                (385, 56.75, "+60", BLUE), (485, 47.75, "+40", SLATE), (585, 47.75, "650", INK)):
        water.text(x, y, label, 8.4, colour, "middle", 700)
    for x, name, note in ((85, "Opening", None), (185, "New logos", "12 signed"),
                          (285, "Expansion", "seat growth"), (385, "Pricing", "+4% list"),
                          (485, "Retention", "churn down"), (585, "Closing", None)):
        water.text(x, 214, name, 7.6, INK, "middle")
        if note:
            water.text(x, 223, note, 7.6, SLATE, "middle")
    water.text(640, 12, "New logos explain about half of the movement", 7.2, SLATE, "end")

    # A doughnut whose legend states 35 %, and bars at 2.5 px a percentage point, the fourth wrapped.
    mix = Drawing(elements, 2, 80, 490)
    mix.ring(60, 70, 40, GREY, 16)
    mix.arc(60, 70, 48, 32, 270.0, 36.0, BLUE)
    mix.text(60, 68, "1,240", 12, INK, "middle", 700)
    mix.text(60, 80, "accounts", 6.8, SLATE, "middle")
    mix.rect(120, 40, 8, 8, BLUE)
    mix.text(132, 47, "434 growth (35%)", 7.4, INK)
    mix.rect(120, 56, 8, 8, GREY)
    mix.text(132, 63, "806 core (65%)", 7.4, INK)
    mix.text(0, 130, "Deal purpose", 7.6, INK, weight=700)
    for y, name, width, colour, share in ((144, "Renewal", 90, INK, "~36%"),
                                          (158, "Referral", 70, SLATE, "~28%"),
                                          (172, "Upsell", 50, BLUE, "~20%")):
        mix.text(0, y, name, 7.2, INK)
        mix.rect(64, y - 6, width, 8, colour)
        mix.text(170, y, share, 7.2, SLATE)
    mix.text(214, 144, "Business", 7.2, INK)
    mix.rect(260, 138, 40, 8, GREY)
    mix.text(306, 144, "~16%", 7.2, SLATE)
    mix.text(214, 160, "Survey base: 240 deals", 7.2, SLATE)
    mix.text(214, 170, "Synthetic figures", 7.2, SLATE)

    # Three labelled bars, 20 px a unit.
    term = Drawing(elements, 3, 520, 500)
    for y, name, width, colour, value in ((14, "North", 60, BLUE, "3.0"),
                                          (44, "Central", 90, GREY, "4.5"),
                                          (74, "South", 120, GREY, "6.0")):
        term.text(0, y, name, 7.0, INK)
        term.rect(0, y + 4, width, 9, colour)
        term.text(width + 5, y + 12, value, 7.0, SLATE)

    # Ten unlabelled monthly columns: chart-shaped, but no number anywhere.
    season = Drawing(elements, 4, 720, 500)
    season.line(0, 80, 200, 80, GREY, 1.0)
    for index, (h, colour) in enumerate(((22, BLUE), (31, BLUE), (27, BLUE), (18, "#FF4136"), (14, "#FF4136"),
                                         (25, BLUE), (38, BLUE), (44, BLUE), (35, BLUE), (29, BLUE))):
        season.rect(4 + index * 19, 80 - h, 11, h, colour)
    for x, label in ((4, "Jan"), (80, "May"), (175, "Oct")):
        season.text(x, 91, label, 6.2, SLATE)
    season.text(200, 14, "Quiet spring", 6.4, "#FF4136", "end", 700)

    # Five unnumbered bars with names only.
    region = Drawing(elements, 5, 960, 500)
    for y, name, width, colour in ((10, "Coastal", 110, INK), (30, "Inland", 76, INK),
                                   (50, "Mountain", 52, SLATE), (70, "Islands", 31, BLUE),
                                   (90, "Remote", 18, BLUE)):
        region.text(0, y, name, 7.0, INK)
        region.rect(0, y + 3, width, 7, colour)

    # A doughnut with its percentage written in the hole.
    repeat = Drawing(elements, 6, 1130, 500)
    repeat.ring(40, 40, 28, GREY, 12)
    repeat.arc(40, 40, 34, 22, 270.0, 72.0, GREEN)
    repeat.text(40, 43, "45%", 11, INK, "middle", 700)
    repeat.text(0, 92, "Repeat buyers", 7.0, INK, weight=700)
    repeat.text(0, 102, "within a year", 7.0, SLATE)

    return finish(elements, "sld_composite_02")


def test_composite_slide_two_recognises_the_four_figures_that_state_their_numbers():
    ir = recognise_charts(composite_slide_two())
    found = {c.spec["type"]: c for c in charts(ir)}
    assert sorted(found) == ["bar", "doughnut", "waterfall"], sorted(found)
    assert len(charts(ir)) == 5, [c.name for c in charts(ir)]


def test_composite_slide_two_waterfall_reads_its_labels():
    chart = next(c for c in charts(recognise_charts(composite_slide_two())) if c.spec["type"] == "waterfall")
    assert chart.spec["categories"] == ["Opening", "New logos", "Expansion", "Pricing", "Retention", "Closing"]
    assert values_of(chart) == [320, 140, 90, 60, 40, 650]
    assert chart.extras["x-wp3c-chart"]["totalIndices"] == [0, 5]
    assert sum(values_of(chart)[1:5]) + values_of(chart)[0] == values_of(chart)[5]
    assert chart.spec["options"]["valueAxis"] == {"min": 0, "max": 800, "visible": True, "majorUnit": 200}


def test_composite_slide_two_donuts_take_their_percentages_from_the_writing():
    found = [c for c in charts(recognise_charts(composite_slide_two())) if c.spec["type"] == "doughnut"]
    assert len(found) == 2
    accounts, repeat = sorted(found, key=lambda c: c.box.x)
    assert values_of(accounts) == [35, 65], "the legend says 35 % growth; the ring is the other 65"
    assert accounts.spec["options"]["holeSize"] == 67                    # 32 / 48
    assert values_of(repeat) == [45, 55], "45 % is written in the hole; the ring is the remainder"
    assert repeat.spec["options"]["holeSize"] == 65                      # 22 / 34


def test_composite_slide_two_bar_charts_come_out_complete():
    found = sorted((c for c in charts(recognise_charts(composite_slide_two())) if c.spec["type"] == "bar"),
                   key=lambda c: c.box.x)
    assert len(found) == 2
    purpose, term = found
    assert purpose.spec["categories"] == ["Renewal", "Referral", "Upsell", "Business"]
    assert values_of(purpose) == [36, 28, 20, 16], "the wrapped fourth bar fits the scale, so it counts"
    assert term.spec["categories"] == ["North", "Central", "South"]
    assert values_of(term) == [3.0, 4.5, 6.0]
    # Render check #4: both read top-down in the file, as they do on the slide.
    assert all(c.spec["options"]["categoryAxis"]["reverse"] is True for c in found)
    # The purpose bars share their <svg> with the doughnut and its legend. A single-series chart
    # takes no name from a legend key, so the grey "core" swatch cannot label the bars.
    assert [s["name"] for s in purpose.spec["series"]] == ["series 1"]


def test_composite_slide_two_refuses_the_figures_that_state_nothing():
    ir = recognise_charts(composite_slide_two())
    refused = [d for d in ir.diagnostics if d.message.startswith("possible chart")]
    assert len(refused) == 2, [d.message for d in refused]
    scopes = sorted(d.source.split(" ")[0] for d in refused)
    assert scopes == ["svg#4", "svg#5"], "monthly columns and region bars"
    assert all(d.level == "info" and "no numeric labels" in d.message for d in refused)


def test_composite_slide_two_specs_are_emittable():
    ir = recognise_charts(composite_slide_two())
    for chart in charts(ir):
        assert charts_spec.validate(chart.spec) == [], (chart.name, charts_spec.validate(chart.spec))
    assert ir.validate(check_files=False) == []


# ------------------------------------------- a score table (render check #4: a table, not a chart)


def fit_matrix() -> IR:
    """A score table: six label cells, a grid of equal 88 x 32 cells, and a column of scores beside
    the last grid column. A table, not a bar chart (invented for this test)."""
    elements: list[Element] = []
    grid = Drawing(elements, 1, 120, 180)
    scores = [6, 9, 5, 7, 8, 3]
    for row, score in enumerate(scores):
        y = row * 36
        grid.rect(0, y, 150, 32, MIST)
        grid.text(36, y + 20, f"Area {row + 1}", 10, INK, weight=700)
        for column in range(4):
            grid.rect(156 + 92 * column, y, 88, 32, INK if (row + column) % 2 else BLUE)
        grid.text(536, y + 21, str(score), 12, BLUE, weight=700)
    return finish(elements, "sld_fitmatrix")


def test_equal_cells_with_numbers_beside_them_are_not_a_bar_chart():
    """Render check #4: six equal boxes cannot draw 6, 9, 5, 7, 8, 3; the numbers are a column of
    a table. It used to become a native bar chart (R^2 = 1.0 on zero spread)."""
    ir = recognise_charts(fit_matrix())
    assert charts(ir) == []
    assert [d.message for d in ir.diagnostics if d.message.startswith("possible chart")] == [],         "a table is not a possible chart either: nothing to say"


def test_a_flat_fit_explains_nothing_and_a_scale_never_runs_backwards():
    from app.engine.classify.charts import _linfit, _scale_r2

    slope, _intercept, r2 = _linfit([6, 9, 5, 7, 8, 3], [88.0] * 6)
    assert slope == 0 and r2 == 0.0, "extents with no spread explain none of the values"
    assert _linfit([5, 5, 5], [1, 2, 3]) is None, "values with no spread: no fit at all"
    assert _scale_r2([10, 20, 30], [30.0, 60.0, 90.0]) == pytest.approx(1.0)
    assert _scale_r2([10, 20, 30], [90.0, 60.0, 30.0]) == 0.0, "a bigger number never draws a shorter bar"


def test_bars_whose_numbers_shrink_as_the_bars_grow_are_refused():
    elements: list[Element] = []
    bars = Drawing(elements, 1, 100, 100)
    for row, (width, value) in enumerate(((60, 30), (120, 20), (180, 10))):
        bars.text(0, row * 30 + 14, f"Item {row}", 9)
        bars.rect(50, row * 30, width, 20, "#1A9AFA")
        bars.text(50 + width + 4, row * 30 + 14, str(value), 9)
    ir = recognise_charts(finish(elements, "sld_backwards"))
    assert charts(ir) == []
