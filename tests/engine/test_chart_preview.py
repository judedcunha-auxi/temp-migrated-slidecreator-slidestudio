"""WP3b — `web/public/chart-preview.js`, measured in the browser that actually runs it.

The preview is the only thing that draws a `data-chart` element before export: the app shows it to
the designer, and `engine/extract/html.py: render_reference` injects it into the slide frame so the
gate's reference composite has a chart where the exported deck has one. So these tests do two jobs:

1. **Geometry, in pixels.** Every assertion below is a number derived from the spec — the plot rect
   from `options.plotArea`, the bar height from `valueAxis.min/max`, the slot centre from the
   category count (gap slots included), the doughnut hole from `holeSize`. A picture that "looks
   about right" proves nothing; a bar that is 81.25 px tall because 65/160 × 200 px is 81.25 does.
2. **The real injection path.** `render_reference` is what the engine calls, after load, so the test
   drives that function rather than a hand-rolled page — the auto-run on `DOMContentLoaded` is not
   what gets used there.

The fixture page is built here rather than read from `fixtures/torture/charts.html`: that file is
WP0b/WP3a's to write and is not in this branch. When it lands, `test_torture_charts_fixture`
picks it up automatically — and fails, never skips, if it exists but draws nothing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import engine as config
from app.engine.extract.html import render_reference

PREVIEW_JS = config.CHART_PREVIEW_JS

#: One frame size for every chart on the fixture page, so expected geometry is arithmetic, not magic.
FRAME_W, FRAME_H = 560, 250

#: The contract's own example (03-AUTHORING-CONTRACT.md §Charts), trimmed to one series: an explicit
#: plot area, an explicit axis range, a gap slot before FY27E and a reference line.
COLUMN_SPEC: dict = {
    "type": "column",
    "categories": ["FY20", "FY21", "FY22", "FY23", "", "FY27E"],
    "series": [{"name": "Partners", "values": [52, 61, 78, 89, None, 143]}],
    "colors": ["#1A9AFB"],
    "options": {
        "dataLabels": True,
        "numberFormat": "0",
        "legend": False,
        "gapWidth": 60,
        "valueAxis": {"min": 0, "max": 160, "majorUnit": 40, "visible": True},
        "plotArea": {"x": 0.08, "y": 0.05, "w": 0.9, "h": 0.8},
        "referenceLines": [{"value": 90, "color": "#516467", "dash": [4, 3], "label": "Original 90k goal"}],
    },
}

STACKED_SPEC: dict = {
    "type": "column_stacked",
    "categories": ["FY20", "FY21", "FY22", "FY23", "", "FY27E"],
    "series": [
        {"name": "Internal", "values": [40, 52, 57, 71, None, 88]},
        {"name": "Partner", "values": [12, 9, 21, 18, None, 55]},
    ],
    "colors": ["#B9C7C9", "#1A9AFB"],
    "options": {
        "dataLabels": True, "numberFormat": "0", "legend": False, "gapWidth": 60,
        "valueAxis": {"min": 0, "max": 160, "visible": True},
        "plotArea": {"x": 0.08, "y": 0.05, "w": 0.9, "h": 0.8},
    },
}

WATERFALL_SPEC: dict = {
    "type": "waterfall",
    "categories": ["2023", "Retail", "Ads", "Events", "Internal", "2030"],
    "series": [{"name": "Spend", "values": [300, 120, 45, 80, 35, 580]}],
    "colors": ["#2DB757", "#E5484D", "#516467"],
    "options": {
        "totals": [0, 5], "dataLabels": True, "numberFormat": "0", "gapWidth": 60,
        "valueAxis": {"min": 0, "max": 600, "visible": True},
        "plotArea": {"x": 0.1, "y": 0.05, "w": 0.85, "h": 0.8},
    },
}

DOUGHNUT_SPEC: dict = {
    "type": "doughnut",
    "categories": ["Upsell", "Business"],
    "series": [{"name": "Split", "values": [61, 39]}],
    "colors": ["#1A9AFB", "#B9C7C9"],
    "options": {"holeSize": 62, "dataLabels": True, "numberFormat": '0"%"', "legend": False},
}

SCATTER_SPEC: dict = {
    "type": "scatter",
    "series": [{"name": "Markets", "x": [2, 4, 6, 8], "y": [10, 40, 30, 80]}],
    "colors": ["#1A9AFB"],
    "options": {"dataLabels": False, "legend": False,
                "valueAxis": {"min": 0, "max": 100, "majorUnit": 25},
                "categoryAxis": {"min": 0, "max": 10, "majorUnit": 2},
                "plotArea": {"x": 0.1, "y": 0.1, "w": 0.85, "h": 0.75}},
}

#: Every family the brief asks the preview to draw, plus the flavours that reach the same code paths.
GALLERY: dict[str, dict] = {
    "column": COLUMN_SPEC,
    "column_stacked": STACKED_SPEC,
    # The 100 % flavour's axis unit is percent, so it carries no 0–160 range of its own.
    "column_stacked_100": dict(
        STACKED_SPEC, type="column_stacked_100",
        options=dict(STACKED_SPEC["options"], valueAxis={"visible": True}),
    ),
    "bar": {"type": "bar", "categories": ["A", "B", "C"],
            "series": [{"name": "v", "values": [3.7, 5.1, 6.9]}], "colors": ["#1A9AFB"],
            "options": {"numberFormat": "0.0", "legend": False,
                        "plotArea": {"x": 0.2, "y": 0.05, "w": 0.7, "h": 0.85}}},
    "bar_stacked": {"type": "bar_stacked", "categories": ["A", "B"],
                    "series": [{"name": "x", "values": [3, 4]}, {"name": "y", "values": [5, 6]}],
                    "colors": ["#1A9AFB", "#B9C7C9"], "options": {"legend": "bottom"}},
    "line": {"type": "line", "categories": ["Q1", "Q2", "Q3", "Q4"],
             "series": [{"name": "a", "values": [10, 14, 9, 18]}], "colors": ["#1A9AFB"],
             "options": {"legend": False}},
    "line_markers": {"type": "line_markers", "categories": ["Q1", "Q2", "Q3", "Q4"],
                     "series": [{"name": "a", "values": [10, 14, None, 18]}], "colors": ["#1A9AFB"],
                     "options": {"legend": False, "markerSize": 6}},
    "area": {"type": "area", "categories": ["Q1", "Q2", "Q3"],
             "series": [{"name": "a", "values": [10, 14, 9]}], "colors": ["#1A9AFB"],
             "options": {"legend": False, "dataLabels": False}},
    "area_stacked": {"type": "area_stacked", "categories": ["Q1", "Q2", "Q3"],
                     "series": [{"name": "a", "values": [10, 14, 9]},
                                {"name": "b", "values": [4, 6, 8]}],
                     "colors": ["#1A9AFB", "#B9C7C9"], "options": {"dataLabels": False}},
    "pie": {"type": "pie", "categories": ["A", "B", "C"],
            "series": [{"name": "s", "values": [50, 25, 25]}],
            "colors": ["#1A9AFB", "#B9C7C9", "#2DB757"], "options": {"legend": False}},
    "pie_exploded": {"type": "pie_exploded", "categories": ["A", "B"],
                     "series": [{"name": "s", "values": [60, 40]}],
                     "colors": ["#1A9AFB", "#B9C7C9"], "options": {"legend": False}},
    "doughnut": DOUGHNUT_SPEC,
    "doughnut_exploded": dict(DOUGHNUT_SPEC, type="doughnut_exploded"),
    "scatter": SCATTER_SPEC,
    "scatter_lines": dict(SCATTER_SPEC, type="scatter_lines"),
    "waterfall": WATERFALL_SPEC,
    # A 3-D flavour previews as its flat base: the data reads the same and the file is still a chart.
    "column_3d": dict(COLUMN_SPEC, type="column_3d"),
    # Gridlines are off unless asked for (WP-C §2.3: the emitter's default, which the preview now shares).
    "column_grid": dict(COLUMN_SPEC, options=dict(COLUMN_SPEC["options"], gridlines=True)),
}


# ------------------------------------------------------------------------------------- helpers


def _page_html(specs: dict[str, dict]) -> str:
    """One absolutely positioned chart div per spec, all the same size, laid out in a column."""
    blocks = []
    for index, (key, spec) in enumerate(specs.items()):
        attribute = json.dumps(spec).replace("&", "&amp;").replace('"', "&quot;")
        blocks.append(
            f'<div id="chart-{key}" data-chart="{attribute}" '
            f'style="position:absolute;left:20px;top:{20 + index * (FRAME_H + 20)}px;'
            f'width:{FRAME_W}px;height:{FRAME_H}px"></div>'
        )
    body = "\n".join(blocks)
    return (
        '<!doctype html><html><head><meta charset="utf-8"><style>'
        "html,body{margin:0;background:transparent;font-family:Arial,Helvetica,sans-serif;"
        "font-size:11px;color:#2E2E38}</style></head><body>\n" + body + "\n</body></html>"
    )


def _load(page, html: str, tmp_path: Path, name: str = "charts.html") -> None:
    """Load a page and inject the preview exactly the way `render_reference` does: after load."""
    path = tmp_path / name
    path.write_text(html, encoding="utf-8")
    page.goto(path.resolve().as_uri(), wait_until="networkidle")
    assert PREVIEW_JS.exists(), f"chart preview script missing at {PREVIEW_JS}"
    page.add_script_tag(path=str(PREVIEW_JS))
    page.evaluate("() => window.ChartPreview.renderAll(document)")


def _svg_info(page, key: str) -> dict:
    """Everything the tests need about one drawn chart, read straight out of the DOM."""
    return page.evaluate(
        """(key) => {
            const host = document.getElementById('chart-' + key);
            const svg = host.querySelector('svg.chart-preview');
            const nums = (s) => (s || '').split(',').map(Number);
            const collect = (selector) => Array.from(svg ? svg.querySelectorAll(selector) : []).map(n => {
                const out = {tag: n.tagName, text: n.textContent};
                for (const a of n.attributes) out[a.name] = a.value;
                return out;
            });
            return {
                status: host.getAttribute('data-chart-preview'),
                message: host.getAttribute('data-chart-preview-message'),
                children: host.children.length,
                svgCount: host.querySelectorAll('svg.chart-preview').length,
                family: svg && svg.getAttribute('data-cp-family'),
                plot: svg ? nums(svg.getAttribute('data-cp-plot')) : null,
                range: svg ? nums(svg.getAttribute('data-cp-value-range')) : null,
                bars: collect('.cp-bar'),
                labels: collect('.cp-label'),
                ticks: collect('.cp-tick'),
                categories: collect('.cp-category'),
                markers: collect('.cp-marker'),
                lines: collect('.cp-line'),
                areas: collect('.cp-area'),
                slices: collect('.cp-slice'),
                pie: collect('.cp-pie'),
                references: collect('.cp-reference'),
                gridlines: collect('.cp-gridline'),
                connectors: collect('.cp-connector'),
                legend: collect('.cp-legend-label'),
                markup: svg ? svg.outerHTML : null,
            };
        }""",
        key,
    )


@pytest.fixture(scope="module")
def gallery(browser, tmp_path_factory):
    """The whole gallery drawn once — one page load for every geometry assertion below."""
    tmp_path = Path(tmp_path_factory.mktemp("chart-preview"))
    context = browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1)
    page = context.new_page()
    _load(page, _page_html(GALLERY), tmp_path)
    yield page
    page.close()
    context.close()


# --------------------------------------------------------------------------------------- tests


def test_preview_script_is_dependency_free() -> None:
    """No imports, no network: the slide frame has neither a bundler nor a connection."""
    source = PREVIEW_JS.read_text(encoding="utf-8")
    assert "window.ChartPreview" in source
    for forbidden in ("import ", "require(", "fetch(", "XMLHttpRequest", "//cdn", "https://cdn"):
        assert forbidden not in source, f"chart-preview.js must not use {forbidden!r}"
    for nondeterministic in ("Math.random", "Date.now", "new Date"):
        assert nondeterministic not in source, f"chart-preview.js must stay deterministic ({nondeterministic})"


def test_every_supported_type_draws_something(gallery) -> None:
    """The brief's bar: every `[data-chart]` element gets children, for every family."""
    for key in GALLERY:
        info = _svg_info(gallery, key)
        assert info["status"] == "ok", f"{key}: {info['status']} {info['message']}"
        assert info["children"] >= 1, f"{key} got no children"
        assert info["svgCount"] == 1, f"{key} drew {info['svgCount']} preview roots"
        drawn = len(info["bars"]) + len(info["lines"]) + len(info["areas"]) + len(info["slices"]) + len(info["markers"])
        assert drawn > 0, f"{key} drew an empty svg"


def test_plot_area_option_places_the_plot(gallery) -> None:
    """`options.plotArea` is the same rect the emitter writes as `c:manualLayout` — honour it exactly."""
    info = _svg_info(gallery, "column")
    expected = [FRAME_W * 0.08, FRAME_H * 0.05, FRAME_W * 0.9, FRAME_H * 0.8]
    assert info["plot"] == pytest.approx(expected, abs=0.01), f"plot {info['plot']} != {expected}"
    assert info["range"] == pytest.approx([0, 160], abs=0.001)


def test_column_bars_match_the_axis_range_and_the_slots(gallery) -> None:
    """Bar height = value/axis-span × plot height; bar centre = its category slot centre."""
    info = _svg_info(gallery, "column")
    plot_x, plot_y, plot_w, plot_h = info["plot"]
    values = [52, 61, 78, 89, None, 143]
    slot = plot_w / len(values)

    bars = {int(bar["data-cp-point"]): bar for bar in info["bars"]}
    assert sorted(bars) == [0, 1, 2, 3, 5], "a null value must leave its slot empty, not shift the rest"

    for index, value in enumerate(values):
        if value is None:
            continue
        bar = bars[index]
        height = float(bar["height"])
        top = float(bar["y"])
        centre = float(bar["x"]) + float(bar["width"]) / 2
        assert height == pytest.approx(value / 160 * plot_h, abs=0.05), f"bar {index} height"
        assert top == pytest.approx(plot_y + plot_h - value / 160 * plot_h, abs=0.05), f"bar {index} top"
        assert centre == pytest.approx(plot_x + (index + 0.5) * slot, abs=0.05), f"bar {index} centre"

    # gapWidth 60 on a single series: bar = slot / (1 + 0.6).
    assert float(bars[0]["width"]) == pytest.approx(slot / 1.6, abs=0.05)
    assert [label["text"] for label in info["labels"]] == ["52", "61", "78", "89", "143"]


def test_gap_slot_keeps_the_category_axis_intact(gallery) -> None:
    """The empty category is a slot with no label and no bar — the point of a gap slot."""
    info = _svg_info(gallery, "column")
    drawn = [category["text"] for category in info["categories"]]
    assert drawn == ["FY20", "FY21", "FY22", "FY23", "FY27E"]
    plot_x, _, plot_w, _ = info["plot"]
    slot = plot_w / 6
    positions = {int(c["data-cp-point"]): float(c["x"]) for c in info["categories"]}
    assert positions[5] == pytest.approx(plot_x + 5.5 * slot, abs=0.05), "FY27E sits after the gap"


def test_reference_line_lands_on_its_value(gallery) -> None:
    info = _svg_info(gallery, "column")
    _, plot_y, _, plot_h = info["plot"]
    assert len(info["references"]) == 1
    line = info["references"][0]
    assert float(line["y1"]) == pytest.approx(plot_y + plot_h * (1 - 90 / 160), abs=0.05)
    assert line["stroke-dasharray"] == "4 3"
    assert line["stroke"] == "#516467"


def test_stacked_columns_sit_on_each_other(gallery) -> None:
    info = _svg_info(gallery, "column_stacked")
    _, plot_y, _, plot_h = info["plot"]
    per_point: dict[int, dict[int, dict]] = {}
    for bar in info["bars"]:
        per_point.setdefault(int(bar["data-cp-point"]), {})[int(bar["data-cp-series"])] = bar

    for point, (internal, partner) in enumerate([(40, 12), (52, 9), (57, 21), (71, 18)]):
        bottom, top = per_point[point][0], per_point[point][1]
        assert float(bottom["height"]) == pytest.approx(internal / 160 * plot_h, abs=0.05)
        assert float(top["height"]) == pytest.approx(partner / 160 * plot_h, abs=0.05)
        # The second series starts exactly where the first one ends.
        assert float(top["y"]) + float(top["height"]) == pytest.approx(float(bottom["y"]), abs=0.05)
        assert float(bottom["y"]) + float(bottom["height"]) == pytest.approx(plot_y + plot_h, abs=0.05)
        assert float(top["x"]) == pytest.approx(float(bottom["x"]), abs=0.001), "stacked bars share a slot"
    assert 4 not in per_point, "the gap slot carries no bar"


def test_stacked_100_normalises_each_category(gallery) -> None:
    info = _svg_info(gallery, "column_stacked_100")
    _, plot_y, _, plot_h = info["plot"]
    assert info["range"] == pytest.approx([0, 100], abs=0.001)
    per_point: dict[int, list[dict]] = {}
    for bar in info["bars"]:
        per_point.setdefault(int(bar["data-cp-point"]), []).append(bar)
    for point, bars in per_point.items():
        total = sum(float(bar["height"]) for bar in bars)
        assert total == pytest.approx(plot_h, abs=0.1), f"category {point} does not fill the axis"
    # 40 of 52 in FY20 is 76.9 % of the stack.
    first = [b for b in per_point[0] if b["data-cp-series"] == "0"][0]
    assert float(first["height"]) == pytest.approx(40 / 52 * plot_h, abs=0.05)
    assert first["data-cp-value"] == "40", "the label data keeps the authored value, not the percentage"


def test_bar_family_runs_horizontally_from_the_first_category_at_the_bottom(gallery) -> None:
    """PowerPoint puts category 1 at the bottom of a bar chart; the preview shows what will export."""
    info = _svg_info(gallery, "bar")
    plot_x, plot_y, plot_w, plot_h = info["plot"]
    bars = {int(bar["data-cp-point"]): bar for bar in info["bars"]}
    assert float(bars[0]["y"]) > float(bars[2]["y"]), "first category is lowest on the page"
    for bar in bars.values():
        assert float(bar["x"]) == pytest.approx(plot_x, abs=0.05), "bars start at the value-axis origin"
    widest = bars[2]
    assert float(widest["width"]) == pytest.approx(6.9 / float(info["range"][1]) * plot_w, abs=0.05)
    assert [label["text"] for label in info["labels"]] == ["3.7", "5.1", "6.9"]


def test_waterfall_steps_float_on_the_running_total(gallery) -> None:
    """Totals are absolute and reset the run; every other step is a delta on top of the last."""
    info = _svg_info(gallery, "waterfall")
    steps = {int(bar["data-cp-point"]): bar for bar in info["bars"]}
    expected = {0: (0, 300, True), 1: (300, 420, False), 2: (420, 465, False),
                3: (465, 545, False), 4: (545, 580, False), 5: (0, 580, True)}
    for point, (start, end, is_total) in expected.items():
        bar = steps[point]
        assert float(bar["data-cp-from"]) == pytest.approx(start), f"step {point} starts at {start}"
        assert float(bar["data-cp-to"]) == pytest.approx(end), f"step {point} ends at {end}"
        assert bar["data-cp-total"] == ("1" if is_total else "0")
    _, plot_y, _, plot_h = info["plot"]
    assert float(steps[1]["height"]) == pytest.approx(120 / 600 * plot_h, abs=0.05)
    assert float(steps[1]["y"]) == pytest.approx(plot_y + plot_h * (1 - 420 / 600), abs=0.05)
    # Rises, falls and totals are told apart by colour, and the deltas are signed.
    assert steps[1]["fill"] == "#2DB757" and steps[0]["fill"] == "#516467"
    assert "+120" in [label["text"] for label in info["labels"]]
    # The plan's connectors: one between every pair of neighbouring bars, the closing total included —
    # exactly the lines the emitter draws on top of the chart (WP-C §3.6).
    assert len(info["connectors"]) == 5


def test_doughnut_hole_and_slice_sweeps(gallery) -> None:
    info = _svg_info(gallery, "doughnut")
    ring = info["pie"][0]
    assert float(ring["data-cp-inner-radius"]) == pytest.approx(float(ring["data-cp-radius"]) * 0.62, abs=0.01)
    sweeps = {int(s["data-cp-point"]): float(s["data-cp-sweep"]) for s in info["slices"]}
    assert sweeps[0] == pytest.approx(61 / 100 * 360, abs=0.01)
    assert sweeps[1] == pytest.approx(39 / 100 * 360, abs=0.01)
    assert float(info["slices"][0]["data-cp-start"]) == pytest.approx(-90), "the first slice starts at 12 o'clock"
    assert [label["text"] for label in info["labels"]] == ["61%", "39%"]


def test_pie_slices_cover_the_circle(gallery) -> None:
    info = _svg_info(gallery, "pie")
    sweeps = [float(s["data-cp-sweep"]) for s in info["slices"]]
    assert sum(sweeps) == pytest.approx(360, abs=0.01)
    assert sweeps[0] == pytest.approx(180, abs=0.01)
    assert float(info["pie"][0]["data-cp-inner-radius"]) == 0.0, "a pie has no hole"


def test_scatter_places_points_on_both_numeric_axes(gallery) -> None:
    info = _svg_info(gallery, "scatter")
    plot_x, plot_y, plot_w, plot_h = info["plot"]
    points = {int(m["data-cp-point"]): m for m in info["markers"]}
    assert len(points) == 4
    for index, (x_value, y_value) in enumerate([(2, 10), (4, 40), (6, 30), (8, 80)]):
        marker = points[index]
        assert float(marker["cx"]) == pytest.approx(plot_x + x_value / 10 * plot_w, abs=0.05)
        assert float(marker["cy"]) == pytest.approx(plot_y + plot_h * (1 - y_value / 100), abs=0.05)
    assert not info["lines"], "a plain scatter draws no connecting line"
    assert _svg_info(gallery, "scatter_lines")["lines"], "scatter_lines does"


def test_line_chart_breaks_at_a_gap_and_marks_its_points(gallery) -> None:
    info = _svg_info(gallery, "line_markers")
    assert len(info["markers"]) == 3, "the null point is not drawn"
    assert len(info["lines"]) == 2, "the null point breaks the line into two runs"
    plot_x, _, plot_w, _ = info["plot"]
    slot = plot_w / 4
    first = [m for m in info["markers"] if m["data-cp-point"] == "0"][0]
    assert float(first["cx"]) == pytest.approx(plot_x + 0.5 * slot, abs=0.05)


def test_area_is_filled_down_to_the_baseline(gallery) -> None:
    import re

    info = _svg_info(gallery, "area")
    assert len(info["areas"]) == 1
    _, plot_y, _, plot_h = info["plot"]
    numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", info["areas"][0]["d"])]
    ys = numbers[1::2]
    assert max(ys) == pytest.approx(plot_y + plot_h, abs=0.05), "the fill closes on the value-0 baseline"


def test_legend_and_axis_labels_follow_the_emitter_defaults(gallery) -> None:
    """Legend only when more than one series; value ticks in the spec's number format; gridlines off
    unless asked for (WP-C §8.3 #2: the emitter never drew them by default, the preview now agrees)."""
    assert not _svg_info(gallery, "column")["legend"], "one series, legend:false — nothing to label"
    assert [entry["text"] for entry in _svg_info(gallery, "bar_stacked")["legend"]] == ["x", "y"]
    ticks = [tick["text"] for tick in _svg_info(gallery, "column")["ticks"]]
    assert ticks == ["0", "40", "80", "120", "160"], "majorUnit 40 over 0–160"
    assert len(_svg_info(gallery, "column")["gridlines"]) == 0
    assert len(_svg_info(gallery, "column_grid")["gridlines"]) == 5


def test_unsupported_and_broken_specs_are_reported_never_silent(browser, tmp_path) -> None:
    """A chart the export will not make must say so on the element, and still draw a notice."""
    html = _page_html({"radar": {"type": "radar", "categories": ["a"], "series": [{"values": [1]}]},
                       "combo": {"type": "combo", "categories": ["a"], "series": [{"values": [1]}]},
                       "bubble": {"type": "bubble", "series": [{"x": [1], "y": [2]}]}})
    html = html.replace("</body>", '<div id="chart-broken" data-chart="{oops" '
                                   'style="position:absolute;left:620px;top:20px;width:200px;height:120px"></div>'
                                   '<div id="chart-zero" data-chart=\'{"type":"column","categories":["a"],'
                                   '"series":[{"values":[1]}]}\' style="position:absolute;width:0;height:0"></div>'
                                   "</body>")
    context = browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1)
    page = context.new_page()
    try:
        _load(page, html, tmp_path, name="unsupported.html")
        for key, reason in (("radar", "Path A"), ("combo", "round 2"), ("bubble", "Path A")):
            info = _svg_info(page, key)
            assert info["status"] == "unsupported", f"{key} reported {info['status']}"
            assert reason in info["message"], f"{key}: {info['message']}"
            assert info["children"] == 1, f"{key} drew no notice"
        broken = _svg_info(page, "broken")
        assert broken["status"] == "error" and "JSON" in broken["message"]
        assert broken["children"] == 1
        zero = _svg_info(page, "zero")
        assert zero["status"] == "skipped" and "no size" in zero["message"]
    finally:
        page.close()
        context.close()


def test_render_all_is_idempotent_and_deterministic(browser, tmp_path) -> None:
    """`render_reference` calls `renderAll` after the auto-run has already fired. Twice must equal once."""
    context = browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1)
    page = context.new_page()
    try:
        _load(page, _page_html({"column": COLUMN_SPEC, "waterfall": WATERFALL_SPEC}), tmp_path, name="twice.html")
        first = {key: _svg_info(page, key)["markup"] for key in ("column", "waterfall")}
        drawn = page.evaluate("() => window.ChartPreview.renderAll(document)")
        assert drawn == 2
        for key, markup in first.items():
            after = _svg_info(page, key)
            assert after["svgCount"] == 1, f"{key} was drawn twice"
            assert after["markup"] == markup, f"{key} redrew differently"
    finally:
        page.close()
        context.close()


def test_auto_run_covers_a_normally_loaded_page(browser, tmp_path) -> None:
    """The other half of the contract: a page that loads the script itself needs no call at all.

    That is how the app shows a slide; `render_reference` is the after-load case the test above
    covers. Both have to work, and the console has to stay clean either way.
    """
    (tmp_path / "chart-preview.js").write_text(PREVIEW_JS.read_text(encoding="utf-8"), encoding="utf-8")
    html = _page_html({"column": COLUMN_SPEC, "doughnut": DOUGHNUT_SPEC}).replace(
        "</head>", '<script src="chart-preview.js"></script></head>'
    )
    (tmp_path / "auto.html").write_text(html, encoding="utf-8")
    context = browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1)
    page = context.new_page()
    problems: list[str] = []
    page.on("console", lambda message: problems.append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda error: problems.append(str(error)))
    try:
        page.goto((tmp_path / "auto.html").resolve().as_uri(), wait_until="networkidle")
        for key in ("column", "doughnut"):
            info = _svg_info(page, key)
            assert info["status"] == "ok", f"{key} was not drawn by the DOMContentLoaded run"
            assert info["svgCount"] == 1
        assert not problems, f"the preview logged errors: {problems}"
    finally:
        page.close()
        context.close()


def test_number_formats_match_the_workbook_the_emitter_writes(gallery) -> None:
    """Labels and ticks read the same as the native chart's, so the two pictures agree."""
    cases = [
        (65, "0", "65"), (4.25, "0.0", "4.3"), (1234567, "#,##0", "1,234,567"),
        (0.41, "0%", "41%"), (0.4123, "0.0%", "41.2%"), (-12.5, "0.0", "-12.5"),
        (120, '#,##0"m"', "120m"), (3.7, "General", "3.7"), (7, None, "7"),
    ]
    for value, fmt, expected in cases:
        got = gallery.evaluate("([v, f]) => window.ChartPreview.formatNumber(v, f)", [value, fmt])
        assert got == expected, f"formatNumber({value!r}, {fmt!r}) = {got!r}, expected {expected!r}"


def test_type_resolution_covers_the_flavours_the_contract_allows(gallery) -> None:
    """3-D/cone/cylinder flavours preview as their flat base; Path A's refusals stay refused.

    Unchanged assertions (WP-C §8.3 #3): the exported `resolveType` keeps its return shape
    `{name, family, stacked, supported, reason, markers, lines, smooth, exploded}`, now computed on
    top of the chart model's `resolveTypeName`/`familyOf` twin rather than its own tables."""
    resolved = gallery.evaluate(
        "(types) => types.map(t => window.ChartPreview.resolveType(t))",
        ["column_3d_stacked_100", "cone_bar_stacked", "donut", "xy", "scatter_smooth_no_markers",
         "line_markers_stacked", "pie_3d_exploded", "bubble_3d", "surface_wireframe"],
    )
    families = [(item["family"], item["stacked"], item["supported"]) for item in resolved]
    assert families == [
        ("column", "stacked100", True),
        ("bar", "stacked", True),
        ("doughnut", "", True),
        ("scatter", "", True),
        ("scatter", "", True),
        ("line", "stacked", True),
        ("pie", "", True),
        (None, "", False),
        (None, "", False),
    ]
    assert resolved[4]["markers"] is False and resolved[4]["smooth"] is True
    assert resolved[5]["markers"] is True
    assert resolved[6]["exploded"] is True


# ------------------------------------------------------------------ the path the engine really uses


def _chart_slide_html(canvas: tuple[int, int], box: tuple[int, int, int, int]) -> str:
    left, top, width, height = box
    spec = json.dumps(COLUMN_SPEC).replace('"', "&quot;")
    return (
        '<!doctype html><html><head><meta charset="utf-8"><style>'
        f"html,body{{margin:0;width:{canvas[0]}px;height:{canvas[1]}px;overflow:hidden;"
        "background:transparent;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#2E2E38}"
        "</style></head><body>"
        f'<div class="chart" data-chart="{spec}" style="position:absolute;left:{left}px;top:{top}px;'
        f'width:{width}px;height:{height}px"></div>'
        "</body></html>"
    )


def test_render_reference_draws_the_chart_into_the_gate_reference(sample_dir, tmp_path) -> None:
    """The real injection path: `render_reference` + `config.CHART_PREVIEW_JS`.

    Without the script the reference composite would show an empty box where the exported deck has a
    chart, and every chart would read as a defect. The assertion is a pixel count inside the chart
    box, and — just as important — zero changed pixels outside it.
    """
    from PIL import Image, ImageChops

    layout = sample_dir / "layouts" / "layout-05.png"
    assert layout.exists(), f"{layout} is missing — the client fixture is incomplete"
    with Image.open(layout) as image:
        canvas = image.size

    box = (96, 330, 560, 250)
    slide = tmp_path / "slide-chart.html"
    slide.write_text(_chart_slide_html(canvas, box), encoding="utf-8")

    eyp_fonts = {"major": "Arial", "minor": "Arial"}     # the client master's theme fonts
    with_preview = render_reference(
        slide, layout, tmp_path / "with.png", assets_dir=sample_dir / "assets", fonts=eyp_fonts,
        scripts=(PREVIEW_JS,),
    )
    without_preview = render_reference(
        slide, layout, tmp_path / "without.png", assets_dir=sample_dir / "assets", fonts=eyp_fonts,
        scripts=(),
    )

    import numpy as np

    with Image.open(with_preview) as drawn, Image.open(without_preview) as blank:
        assert drawn.size == canvas == blank.size
        difference = ImageChops.difference(drawn.convert("RGB"), blank.convert("RGB"))
        changed = np.asarray(difference, dtype=np.int16).max(axis=2) > 8
    painted = int(changed[box[1]:box[1] + box[3], box[0]:box[0] + box[2]].sum())
    total = int(changed.sum())

    assert painted > 2000, f"the preview only painted {painted} px inside the chart frame"
    assert total == painted, f"{total - painted} px changed outside the chart frame"


def test_charts_fixture_page_draws_every_chart(browser, tmp_path) -> None:
    """Every `[data-chart]` on the charts fixture page draws — the brief's own acceptance test.

    The page is WP0b/WP3a's `fixtures/torture/charts.html` as soon as that file exists; until then
    it is the gallery above, written to disk and loaded the same way. Either way the test runs real
    assertions against a real page — it never skips, and it upgrades itself at the merge.
    """
    fixture = config.TORTURE_FIXTURE / "charts.html"
    if not fixture.exists():
        fixture = tmp_path / "charts.html"
        fixture.write_text(_page_html(GALLERY), encoding="utf-8")
    context = browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1)
    page = context.new_page()
    try:
        page.goto(fixture.resolve().as_uri(), wait_until="networkidle")
        page.add_script_tag(path=str(PREVIEW_JS))
        drawn = page.evaluate("() => window.ChartPreview.renderAll(document)")
        report = page.evaluate(
            """() => Array.from(document.querySelectorAll('[data-chart]')).map(el => {
                   let type = null;
                   try { type = (JSON.parse(el.getAttribute('data-chart')) || {}).type; } catch (e) {}
                   return {type: type,
                           status: el.getAttribute('data-chart-preview'),
                           message: el.getAttribute('data-chart-preview-message'),
                           children: el.children.length};
               })"""
        )
        assert drawn == len(report) and report, f"{fixture} has no [data-chart] elements"
        empty = [entry for entry in report if entry["children"] < 1]
        assert not empty, f"chart elements the preview left empty: {empty}"
        # A fixture may carry a deliberately refused type (the contract makes those a lint error);
        # everything else has to draw.
        refused = ("bubble", "radar", "stock", "surface", "combo")
        undrawn = [
            entry for entry in report
            if entry["status"] != "ok" and not str(entry["type"] or "").startswith(refused)
        ]
        assert not undrawn, f"charts the preview could not draw: {undrawn}"
    finally:
        page.close()
        context.close()


# ------------------------------------------------------------------- WP-C: the preview reads the model

#: The waterfalls and coercions of 12-WPC §8.4, drawn on a page of their own (the gallery above is
#: WP3b's geometry suite, one frame size for every chart).
WPC_GALLERY: dict[str, dict] = {
    "bridge": {"type": "waterfall", "categories": ["FY23", "Price", "Volume", "Mix", "Cost out", "FY24"],
               "series": [{"name": "EBITDA", "values": [20, -35, 10, -8, 25, 12]}],
               "colors": ["#1E9E5A", "#D64545", "#0B2545"],
               "options": {"numberFormat": "0", "legend": False, "totals": [0, 5]}},
    "bridge_out": {"type": "waterfall", "categories": ["FY23", "Up", "Down", "FY24"],
                   "series": [{"name": "s", "values": [100, 40, -15, 125]}],
                   "options": {"numberFormat": "0", "totals": [0, 3], "labelPosition": "outEnd"}},
    "stacked_crossing": {"type": "waterfall", "categories": ["FY23", "Q1", "FY24"],
                         "series": [{"name": "Internal", "values": [30, -25, -10]},
                                    {"name": "Partner", "values": [0, -15, 0]}],
                         "colors": ["#0B2545", "#1A9AFA"], "options": {"numberFormat": "0", "totals": [0, 2]}},
    "below_zero": {"type": "waterfall", "categories": ["FY23", "Opex", "FY24"],
                   "series": [{"name": "EBIT", "values": [-20, -10, -30]}],
                   "options": {"numberFormat": "0", "legend": False, "totals": [0, 2]}},
    "strings": {"type": "column_clustered", "categories": ["2022", "2023", "2024", "2025E"],
                "series": [{"name": "Revenue", "values": ["4.2", "4.9", "5.6", "6.2"]}], "colors": ["#0B5CAD"],
                "options": {"legend": False}},
}


@pytest.fixture(scope="module")
def wpc(browser, tmp_path_factory):
    tmp_path = Path(tmp_path_factory.mktemp("chart-preview-wpc"))
    context = browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1)
    page = context.new_page()
    _load(page, _page_html(WPC_GALLERY), tmp_path, name="wpc.html")
    yield page
    page.close()
    context.close()


def _inside(label: dict, bar: dict) -> bool:
    y = float(label["y"])
    return float(bar["y"]) - 1 <= y <= float(bar["y"]) + float(bar["height"]) + 1


def test_waterfall_labels_inside_by_default_and_outside_on_request(gallery, wpc) -> None:
    """Native labels sit inside the bars (`center`); `outEnd` puts text above a rise and below a fall."""
    info = _svg_info(gallery, "waterfall")
    bars = {(bar["data-cp-series"], bar["data-cp-point"]): bar for bar in info["bars"]}
    for label in info["labels"]:
        assert _inside(label, bars[(label["data-cp-series"], label["data-cp-point"])]), label
    out = _svg_info(wpc, "bridge_out")
    by_point = {int(label["data-cp-point"]): label for label in out["labels"]}
    assert [by_point[i]["text"] for i in sorted(by_point)] == ["100", "+40", "-15", "125"]
    tops = {int(bar["data-cp-point"]): float(bar["y"]) for bar in out["bars"]}
    bottoms = {int(bar["data-cp-point"]): float(bar["y"]) + float(bar["height"]) for bar in out["bars"]}
    assert float(by_point[1]["y"]) < tops[1], "a rise's label sits above its bar"
    assert float(by_point[2]["y"]) > bottoms[2], "a fall's label sits below its bar"


def test_gridlines_off_by_default_and_on_when_asked(gallery) -> None:
    assert _svg_info(gallery, "column")["gridlines"] == []
    grid = _svg_info(gallery, "column_grid")["gridlines"]
    assert len(grid) == 5 and {line["stroke"] for line in grid} == {"#E4E9F0"}


def test_bridge_draws_below_zero(wpc) -> None:
    """p05-D: the bars below zero are drawn, and the crossing fall reads its step as frozen text."""
    info = _svg_info(wpc, "bridge")
    below = [bar for bar in info["bars"] if min(float(bar["data-cp-from"]), float(bar["data-cp-to"])) < 0]
    assert below, "nothing drawn below the axis"
    price = [bar for bar in info["bars"] if bar["data-cp-point"] == "1"]
    assert sorted((float(b["data-cp-from"]), float(b["data-cp-to"])) for b in price) == [(0.0, -15.0), (0.0, 20.0)]
    frozen = [label for label in info["labels"] if label.get("data-cp-frozen") == "1"]
    assert sorted(label["text"] for label in frozen) == ["+25", "-35"]
    assert info["range"] == pytest.approx([-20, 20])


def test_stacked_waterfall_draws_the_plans_pieces(wpc) -> None:
    """The §3.2 worked example: Internal straddles zero in Q1 (two pieces), Partner sits on top."""
    info = _svg_info(wpc, "stacked_crossing")
    q1 = sorted((int(b["data-cp-series"]), float(b["data-cp-from"]), float(b["data-cp-to"]))
                for b in info["bars"] if b["data-cp-point"] == "1")
    assert q1 == [(1, 0.0, 15.0), (2, 15.0, 30.0), (4, 0.0, -10.0)]
    fills = {int(b["data-cp-series"]): b["fill"] for b in info["bars"] if b["data-cp-point"] == "1"}
    assert fills[1] == fills[4] == "#0B2545" and fills[2] == "#1A9AFA", "a split series keeps its colour"


def test_entirely_negative_waterfall(wpc) -> None:
    info = _svg_info(wpc, "below_zero")
    zero_line = [float(axis["y1"]) for axis in [a for a in _all(wpc, "below_zero", ".cp-axis")]][0]
    for bar in info["bars"]:
        assert float(bar["y"]) >= zero_line - 0.5, "every bar hangs below the axis line"
    assert sorted(label["text"] for label in info["labels"]) == ["-10", "-20", "-30"]


def test_connectors_are_dashed_in_the_connector_colour(gallery) -> None:
    connectors = _svg_info(gallery, "waterfall")["connectors"]
    assert connectors and {c["stroke"] for c in connectors} == {"#8A9699"}
    assert {c["stroke-dasharray"] for c in connectors} == {"3 2"}
    assert {c["stroke-width"] for c in connectors} == {"1"}, "0.75 pt"


def test_coercions_are_noted_on_the_element(wpc) -> None:
    notes = json.loads(wpc.evaluate("() => document.getElementById('chart-strings')"
                                    ".getAttribute('data-chart-preview-notes')"))
    assert notes[0] == "chart type 'column_clustered' read as 'column' — write the type name as listed"
    assert 'series[0].values[0]: "4.2" read as 4.2' in notes
    info = _svg_info(wpc, "strings")
    assert info["status"] == "ok" and [label["text"] for label in info["labels"]] == ["4.2", "4.9", "5.6", "6.2"]


def test_legend_lists_authored_series_only(wpc) -> None:
    """Six series in the file (Base, the authored two, and their − copies); two names in the legend."""
    assert [entry["text"] for entry in _svg_info(wpc, "stacked_crossing")["legend"]] == ["Internal", "Partner"]


def _all(page, key: str, selector: str) -> list[dict]:
    return page.evaluate(
        """([key, selector]) => Array.from(document.getElementById('chart-' + key)
               .querySelectorAll('svg.chart-preview ' + selector)).map(n => {
                   const out = {}; for (const a of n.attributes) out[a.name] = a.value; return out; })""",
        [key, selector],
    )
