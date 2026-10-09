"""WP-C §2.6 — `chart_model.py` and its JS twin in `chart-preview.js` read every spec alike.

The model is the one reading of a `data-chart` spec: the validator, the emitter, the structural check
and the recogniser consume the Python module, the app's preview and the gate's reference composite
consume the JavaScript one. These tests are the proof that the two are the same function:

* every spec of `tests/engine/fixtures/charts/parity-specs.json` (≥ 40, one per rule and per case of
  12-WPC §8.2) normalises to the same model and lays out to the same geometry in both languages
  (floats to 6 dp, ints as floats, dict order ignored);
* the preview draws what the model says: the plot rect is `layout.plotArea × frame`, a waterfall's
  pieces are the plan's cells stacked as a stacked column stacks them, every label reads the model's
  text (frozen ones included), and the model's notes are on the element;
* every type name and alias — and twelve unknown spellings — resolves identically, from tables the
  test holds equal.

A rule added to `chart_model` without a parity spec fails review; a rule changed in one language
fails here.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import engine as config
from app.engine import chart_model as cm

PARITY = config.FIXTURES_DIR / "charts" / "parity-specs.json"
ENTRIES: list[dict] = json.loads(PARITY.read_text(encoding="utf-8"))["specs"]
IDS = [entry["id"] for entry in ENTRIES]

#: Unknown and near-miss spellings — each must be refused (or resolved) the same way in both.
ODD_NAMES = ["grouped_bar", "histogram", "colum", "Stacked Column Chart", "waterfallchart", "Line-Markers",
             "bar_chart", "column clustered", "donut chart", "constructor", "__proto__", "toString"]


def canon(value):
    """What deep equality means across the JSON boundary: floats to 6 dp, ints as floats."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 6)
    if isinstance(value, (list, tuple)):
        return [canon(item) for item in value]
    if isinstance(value, dict):
        return {str(key): canon(item) for key, item in value.items()}
    raise TypeError(f"not JSON: {value!r}")


def _page_html() -> str:
    blocks = []
    top = 10
    for index, entry in enumerate(ENTRIES):
        frame = entry["frame"]
        attribute = json.dumps(entry["spec"]).replace("&", "&amp;").replace("'", "&#39;")
        blocks.append(f"<div id=\"parity-{index}\" data-chart='{attribute}' style=\"position:absolute;left:10px;"
                      f"top:{top}px;width:{frame['w']}px;height:{frame['h']}px;font-size:{frame['sizePx']}px\"></div>")
        top += frame["h"] + 10
    return ('<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;background:#fff;'
            'font-family:Arial,Helvetica,sans-serif;color:#2E2E38}</style></head><body>'
            + "".join(blocks) + "</body></html>")


@pytest.fixture(scope="module")
def parity(browser, tmp_path_factory):
    """Every parity spec on one page, drawn once, with both readings taken in the browser."""
    tmp = Path(tmp_path_factory.mktemp("chart-parity"))
    page_path = tmp / "parity.html"
    page_path.write_text(_page_html(), encoding="utf-8")
    context = browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1)
    page = context.new_page()
    page.goto(page_path.resolve().as_uri(), wait_until="networkidle")
    page.add_script_tag(path=str(config.CHART_PREVIEW_JS))
    drawn = page.evaluate("() => window.ChartPreview.renderAll(document)")
    assert drawn == len(ENTRIES)
    texts = [json.dumps(entry["spec"]) for entry in ENTRIES]
    frames = [entry["frame"] for entry in ENTRIES]
    readings = page.evaluate(
        """([texts, frames]) => texts.map((text, i) => {
               const model = ChartPreview.normalise(JSON.parse(text));
               const f = frames[i];
               return {model: model, layout: ChartPreview.layout(model, f.w, f.h, f.sizePx)};
           })""",
        [texts, frames],
    )
    drawings = page.evaluate(
        """(count) => Array.from({length: count}, (_, i) => {
               const host = document.getElementById('parity-' + i);
               const svg = host.querySelector('svg.chart-preview');
               const all = (sel) => Array.from(svg ? svg.querySelectorAll(sel) : []).map(n => {
                   const out = {text: n.textContent};
                   for (const a of n.attributes) out[a.name] = a.value;
                   return out;
               });
               return {status: host.getAttribute('data-chart-preview'),
                       notes: host.getAttribute('data-chart-preview-notes'),
                       plot: svg && svg.getAttribute('data-cp-plot'),
                       bars: all('.cp-bar'), labels: all('.cp-label'), connectors: all('.cp-connector'),
                       categories: all('.cp-category'), ticks: all('.cp-tick')};
           })""",
        len(ENTRIES),
    )
    yield {"readings": readings, "drawings": drawings, "page": page}
    page.close()
    context.close()


def _model(index: int) -> dict:
    return cm.normalise(json.loads(json.dumps(ENTRIES[index]["spec"])))


def test_the_fixture_covers_the_brief():
    """≥ 40 entries, unique ids, and the §8.2 cases by name."""
    assert len(ENTRIES) >= 40 and len(set(IDS)) == len(IDS)
    for wanted in ("p05-A", "p05-B", "p05-C", "p05-D", "totals totalIndices", "totals two spellings",
                   "totals 1,0,0,1", "totals empty list", "baseValue alone", "baseValue with totals",
                   "direction words", "conflicting series words", "numeric strings", "option strings",
                   "pointColors list", "pointColors flat dict", "pointColors nested", "pointColors one-entry list",
                   "pointColors list on two series", "colour forms", "alias column_clustered", "alias bridge",
                   "refused grouped_bar", "refused histogram", "refused radar", "refused combo", "axis bools",
                   "connectorColor", "waterfall no plotArea", "column unpinned", "column negatives",
                   "bar negatives", "J stacked crossing", "E stacked non-crossing", "K entirely below zero",
                   "L zero step", "all-gap waterfall", "single-category waterfall",
                   "waterfall categoryAxis.reverse", "zoomed axis", "bar referenceLines", "General edge values",
                   "format 0;(0)", "gallery column",
                   # render check 2026-09-29: #3 percent axes, #5 waterfall roles, #6 label texts
                   "rc3 percent axis as fractions", "rc3 percent axis in percent", "rc3 percent axis crossed",
                   "rc5 one colour waterfall", "rc5 two colours", "rc5 named last total",
                   "rc6 label texts d2_s07", "rc6 label texts nulls", "rc6 label texts line",
                   "rc6 label texts doughnut", "rc6 label texts scatter", "rc6 label texts on a waterfall",
                   "rc6 label texts on an outEnd waterfall", "rc6 label texts on a stacked outEnd",
                   "rc6 label texts rejected",
                   # G24: label styles and line breaks, middle subtotals
                   "g24 labelStyle bold colour size", "g24 labelStyle inside bars", "g24 labelStyle rejected",
                   "g24 labelStyle not an object", "g24 axis bold", "g24 two-line labels on a waterfall",
                   "g24 two-line outEnd waterfall", "g24 mid subtotal named d1_s03", "g24 mid subtotal by the axis max",
                   "g24 mid equal but unnamed stays a step", "g24 mid subtotal rounded", "g24 mid subtotal below",
                   "g24 declared totals win", "g24 typed step is never a subtotal", "g24 General format tolerance"):
        assert any(i.startswith(wanted) for i in IDS), wanted
    from tests.engine.test_chart_preview import GALLERY

    assert {f"gallery {key}" for key in GALLERY} <= set(IDS), "one spec per gallery entry"


@pytest.mark.parametrize("index", range(len(ENTRIES)), ids=IDS)
def test_models_agree(parity, index):
    assert canon(parity["readings"][index]["model"]) == canon(_model(index))


@pytest.mark.parametrize("index", range(len(ENTRIES)), ids=IDS)
def test_layouts_agree(parity, index):
    frame = ENTRIES[index]["frame"]
    python = cm.layout(_model(index), frame["w"], frame["h"], frame["sizePx"])
    assert canon(parity["readings"][index]["layout"]) == canon(python)


def test_every_type_name_resolves_identically(parity):
    names = sorted(cm.STAGEFLOW_NAMES | cm.DERIVED_NAMES | cm.ENGINE_NAMES | set(cm.ALIASES)) + ODD_NAMES
    js = parity["page"].evaluate("(names) => names.map(n => ChartPreview.resolveTypeName(n))", names)
    python = [cm.resolve_type_name(name) for name in names]
    mismatched = [(n, p, j) for n, p, j in zip(names, python, js, strict=False) if p != j]
    assert mismatched == []
    assert cm.resolve_type_name("constructor")["name"] is None, "own keys only, in both"


def test_the_tables_have_one_source(parity):
    """The JS tables equal `chart_model`'s — a name added in one language only fails here."""
    tables = parity["page"].evaluate("() => ChartPreview.TABLES")
    as_sets = ("STAGEFLOW_NAMES", "ROUND_2", "OPTION_KEYS", "SERIES_KEYS", "AXIS_KEYS", "LABEL_POSITIONS",
               "LEGEND_POSITIONS", "MARKER_STYLES", "DASH_NAMES")
    for name in as_sets:
        assert set(tables[name]) == set(getattr(cm, name)), name
    for name in ("DERIVED_BASES", "ENGINE_BASES", "ALIASES", "LABEL_POSITION_ALIASES", "TYPE_WORDS",
                 "WATERFALL_COLORS", "GRIDLINES_DEFAULT", "CONNECTORS_DEFAULT", "REFERENCE_LINE_DEFAULT"):
        assert canon(tables[name]) == canon(getattr(cm, name)), name
    assert tables["PATH_A_UNSUPPORTED_PREFIXES"] == list(cm.PATH_A_UNSUPPORTED_PREFIXES)
    assert canon(tables["NUMBER_RANGES"]) == canon({k: list(v) for k, v in cm.NUMBER_RANGES.items()})
    assert tables["OPTION_ORDER"] == list(cm._OPTION_ORDER)
    for name in ("DEFAULT_GAP_WIDTH", "DEFAULT_SIZE_PX", "BASE_SERIES", "MINUS", "DASH_MAX_PX",
                 "WATERFALL_INK", "PERCENT_TICK_FORMAT", "SUBTOTAL_TOLERANCE", "LINE_PITCH_EM"):
        assert canon(tables[name]) == canon(getattr(cm, name)), name
    assert tables["TOTAL_CATEGORY"] == cm.TOTAL_CATEGORY.pattern
    assert tables["SUBTOTAL_CATEGORY"] == cm.SUBTOTAL_CATEGORY.pattern
    assert set(tables["LABEL_STYLE_KEYS"]) == set(cm.LABEL_STYLE_KEYS)


def test_format_units_and_text_lines_agree(parity):
    """The subtotal tolerance's digit unit and the line split read alike in both languages."""
    formats = ["0", "0.0", "0.0#", "#,##0", "#,##0,", "0.0,,", "0%", "0.0%", '"$"0.0"B"', "0.0;(0.0)", "General",
               '"n/a"', "", "#,##0.00_);(#,##0.00)", "0.0\"%\""]
    js = parity["page"].evaluate("(fs) => fs.map(f => ChartPreview.formatUnit(f))", formats)
    assert canon(js) == canon([cm.format_unit(fmt) for fmt in formats])
    texts = ["one", "two\nlines", "cr\r\nlf", "trailing\n", "\n", "a\n\nb", ""]
    assert parity["page"].evaluate("(ts) => ts.map(t => ChartPreview.textLines(t))", texts) == \
        [cm.text_lines(text) for text in texts]


def test_tick_texts_agree(parity):
    """Every value-axis tick prints alike — a percent axis's as the fraction the file holds."""
    for index, entry in enumerate(ENTRIES):
        model = _model(index)
        if model["type"] is None:
            continue
        ticks = cm.layout(model, entry["frame"]["w"], entry["frame"]["h"], entry["frame"]["sizePx"])["ticks"]
        js = parity["page"].evaluate("([text, ticks]) => { const m = ChartPreview.normalise(JSON.parse(text));"
                                     " return ticks.map(t => ChartPreview.tickText(m, t)); }",
                                     [json.dumps(entry["spec"]), ticks])
        assert js == [cm.tick_text(model, tick) for tick in ticks], entry["id"]


@pytest.mark.parametrize("index", range(len(ENTRIES)), ids=IDS)
def test_drawn_plot_rect_equals_the_layout(parity, index):
    model = _model(index)
    drawing = parity["drawings"][index]
    if model["type"] is None:
        assert drawing["status"] == "unsupported" and drawing["plot"] is None
        return
    frame = ENTRIES[index]["frame"]
    area = cm.layout(model, frame["w"], frame["h"], frame["sizePx"])["plotArea"]
    expected = [area["x"] * frame["w"], area["y"] * frame["h"], area["w"] * frame["w"], area["h"] * frame["h"]]
    drawn = [float(v) for v in drawing["plot"].split(",")]
    assert drawn == pytest.approx(expected, abs=0.5)


def stacked_pieces(plan: dict, count: int) -> list[tuple[int, int, float, float]]:
    """The plan's visible cells stacked as a stacked column stacks them (positives up from zero,
    negatives down from zero, each in series order): `(series, point, from, to)`."""
    positive, negative, pieces = [0.0] * count, [0.0] * count, []
    for index, entry in enumerate(plan["series"]):
        for point, cell in enumerate(entry["values"]):
            if cell is None:
                continue
            if cell >= 0:
                start, positive[point] = positive[point], positive[point] + cell
                end = positive[point]
            else:
                start, negative[point] = negative[point], negative[point] + cell
                end = negative[point]
            if entry["visible"]:
                pieces.append((index, point, start, end))
    return pieces


@pytest.mark.parametrize("index", [i for i, e in enumerate(ENTRIES) if cm.normalise(e["spec"])["family"] == "waterfall"
                                   and cm.normalise(e["spec"])["type"] is not None],
                         ids=[e["id"] for e in ENTRIES if cm.normalise(e["spec"])["family"] == "waterfall"
                              and cm.normalise(e["spec"])["type"] is not None])
def test_waterfall_cells_agree(parity, index):
    """Every drawn piece is a plan cell where PowerPoint's stacking puts it, and nothing else."""
    model = _model(index)
    expected = sorted((s, p, round(a, 6), round(b, 6))
                      for s, p, a, b in stacked_pieces(model["waterfall"], len(model["categories"])))
    drawn = sorted((int(bar["data-cp-series"]), int(bar["data-cp-point"]), round(float(bar["data-cp-from"]), 6),
                    round(float(bar["data-cp-to"]), 6)) for bar in parity["drawings"][index]["bars"])
    assert drawn == expected
    for series, point, start, end in expected:
        segments = [s for s in model["waterfall"]["segments"] if s["category"] == point
                    and any(part["series"] == series for part in s["parts"])]
        assert segments and min(s["lo"] for s in segments) <= min(start, end) + 1e-9, "a piece of a plan segment"


def expected_labels(model: dict) -> list[tuple[str, str]]:
    """What the file's labels read, from the model: `(key, text)` per drawn label."""
    options = model["options"]
    labels: list[tuple[str, str]] = []
    if model["type"] is None:
        return labels
    on = options["dataLabels"]
    if model["family"] == "waterfall":
        plan = model["waterfall"]
        if options["labelPosition"] == "outEnd":
            for point, role in enumerate(plan["roles"]):
                if role == "gap" or not any(entry["values"][point] is not None and on[k]
                                            for k, entry in enumerate(model["series"])):
                    continue
                own = plan["texts"][point]
                text = own if own is not None else cm.format_number(plan["values"][point], plan["formats"][role])
                if text != "":
                    labels.append((f"p{point}", text))
            return sorted(labels)
        for segment in plan["segments"]:
            if not on[segment["source"]]:
                continue
            label = segment["label"]
            cell = plan["series"][label["series"]]["values"][label["point"]]
            text = label["text"] if label["text"] is not None else cm.format_number(cell, label["format"])
            if text != "":
                labels.append((f"s{label['series']}p{label['point']}", text))
        return sorted(labels)
    fmt = options["labelFormat"] or options["numberFormat"]
    for k, entry in enumerate(model["series"]):
        if not on[k]:
            continue
        values = entry["y"] if model["family"] == "scatter" else entry["values"]
        xs = entry["x"] if model["family"] == "scatter" else None
        texts = entry["labels"] or []
        for point, value in enumerate(values):
            if value is None or (xs is not None and xs[point] is None):
                continue
            if model["family"] in ("pie", "doughnut") and abs(value) <= 0:
                continue
            own = texts[point] if point < len(texts) else None
            text = own if own is not None else cm.format_number(value, fmt)
            if text != "":                             # the author's "" hides that point's label
                labels.append((f"s{k}p{point}", text))
    return sorted(labels)


def _whole_text(node: dict) -> str:
    """A drawn text as the model wrote it: a label broken by `\\n` is one `tspan` per line and
    carries its whole text as `data-cp-text` (its textContent runs the lines together)."""
    return node.get("data-cp-text", node["text"])


@pytest.mark.parametrize("index", range(len(ENTRIES)), ids=IDS)
def test_label_texts_agree(parity, index):
    model = _model(index)
    drawn = []
    for node in parity["drawings"][index]["labels"]:
        key = f"s{node['data-cp-series']}p{node['data-cp-point']}" if "data-cp-series" in node \
            else f"p{node['data-cp-point']}"
        drawn.append((key, _whole_text(node)))
    assert sorted(drawn) == expected_labels(model)


@pytest.mark.parametrize("index", range(len(ENTRIES)), ids=IDS)
def test_label_style_and_line_breaks_are_drawn(parity, index):
    """G24: `options.labelStyle` is on every drawn label (weight, ink, size) and nowhere else; a
    label or category broken by `\\n` is drawn as that many lines; an axis's `bold` is on its text."""
    model = _model(index)
    drawing = parity["drawings"][index]
    if model["type"] is None:
        return
    style = model["options"]["labelStyle"]
    for node in drawing["labels"]:
        assert (node.get("font-weight") == "700") == style["bold"], node
        if style["color"] is not None:
            assert node["fill"] == "#" + style["color"], node
        if style["fontSize"] is not None:
            size = cm.label_size(model, cm.layout(model, 1, 1, ENTRIES[index]["frame"]["sizePx"])["sizePx"])
            assert float(node["font-size"]) == pytest.approx(size, abs=1e-3), node
        assert node["text"] == "".join(cm.text_lines(_whole_text(node)))
    drawn_categories = [_whole_text(node) for node in drawing["categories"]]
    if model["family"] not in ("pie", "doughnut", "scatter") and model["options"]["categoryAxis"]["visible"]:
        assert drawn_categories == [c for c in model["categories"] if c]
    for node in drawing["categories"]:
        assert (node.get("font-weight") == "700") == model["options"]["categoryAxis"]["bold"], node
    if model["family"] != "scatter":
        for node in drawing["ticks"]:
            assert (node.get("font-weight") == "700") == model["options"]["valueAxis"]["bold"], node


@pytest.mark.parametrize("index", range(len(ENTRIES)), ids=IDS)
def test_notes_agree(parity, index):
    notes = parity["drawings"][index]["notes"]
    assert notes is not None, "every drawn spec says what the model made of it"
    assert json.loads(notes) == [note["text"] for note in _model(index)["notes"]]


def test_connectors_follow_the_plan(parity):
    """One dashed line per plan connector, at its level — none where the model omitted one."""
    for index, entry in enumerate(ENTRIES):
        model = _model(index)
        if model["family"] != "waterfall" or model["type"] is None:
            continue
        drawn = [(int(c["data-cp-index"]), float(c["data-cp-value"])) for c in parity["drawings"][index]["connectors"]]
        plan = [(c["index"], c["level"]) for c in model["waterfall"]["connectors"]]
        assert drawn == plan, entry["id"]
