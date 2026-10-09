"""WP-C — the one chart model every reader of a `data-chart` spec consumes.

`engine/chart_model.py` is the only place a spec is read: the validator, the emitter, the structural
check and the app's preview (its JS twin) all read the model. These tests hold its rules down — the
totals spellings, the number grammar, the colour forms, the type names, the axis, the label formats,
and the waterfall plan in PowerPoint's stacking order (docs/archive/engine/fidelity/12-WPC-charts.md §2–§3).
"""
from __future__ import annotations

import json
import math

import pytest

from app.engine import chart_model as cm
from app.engine import charts_spec
from app.engine.emit import charts as emitter

MINUS = cm.MINUS

#: p05-A's data (probe `p05_waterfall`), the base of every totals spelling below.
P05_CATEGORIES = ["Standalone", "Cost syn.", "Revenue syn.", "Subtotal", "Integration", "Tax", "Combined"]
P05_VALUES = [8.1, 2.4, 1.6, 12.1, -0.9, 1.2, 12.4]


def _waterfall(values, *, series=None, categories=None, **options):
    series = series or [{"name": "Value", "values": list(values)}]
    count = len(series[0]["values"])
    return {"type": "waterfall", "categories": categories or [f"c{i}" for i in range(count)],
            "series": series, "options": options}


def _plan(spec):
    model = cm.normalise(spec)
    assert model["type"] == "waterfall", model["reason"]
    return model["waterfall"]


def _cells(plan):
    return {entry["name"]: entry["values"] for entry in plan["series"]}


def _kinds(model, kind):
    return [note["text"] for note in model["notes"] if note["kind"] == kind]


# ----------------------------------------------------------------------------- totals (§3.1)


def test_totals_every_spelling_gives_the_same_plan():
    wanted = [0, 3, 6]
    spellings = {
        "indices": _waterfall(P05_VALUES, totals=[0, 3, 6]),
        "bools": _waterfall(P05_VALUES, totals=[True, False, False, True, False, False, True]),
        "types": _waterfall(None, series=[{"name": "Value", "values": P05_VALUES, "types": [
            "total", "increase", "increase", "total", "decrease", "increase", "total"]}]),
        "series.totals": _waterfall(None, series=[{"name": "Value", "values": P05_VALUES,
                                                   "totals": [0, 3, 6]}]),
        "totalIndices": _waterfall(P05_VALUES, totalIndices=[0, 3, 6]),
        "two spellings": _waterfall(P05_VALUES, totals=[0, 3], totalIndices=[6]),
    }
    plans = {name: _plan(spec) for name, spec in spellings.items()}
    for name, plan in plans.items():
        assert plan["totals"] == wanted, name
        assert plan["roles"] == ["total", "increase", "increase", "total", "decrease", "increase", "total"], name
    reference = plans["indices"]
    for name, plan in plans.items():
        assert plan["series"] == reference["series"], name
        assert plan["segments"] == reference["segments"], name
    # the preview's old spellings are read, and said to be old
    assert any("totalIndices is the old spelling" in t for t in charts_spec.advisories(spellings["totalIndices"]))
    assert any("say it once: options.totals" in t for t in charts_spec.advisories(spellings["two spellings"]))
    assert charts_spec.advisories(spellings["indices"]) == []
    for spec in spellings.values():
        assert charts_spec.validate(spec) == [], charts_spec.validate(spec)


def test_bools_are_flags_ints_are_indices():
    # [1, 0, 0, 1] are indices {0, 1} — never flags — with an advisory, because that spelling meant flags.
    spec = _waterfall([10, 5, 5, 20], totals=[1, 0, 0, 1])
    plan = _plan(spec)
    assert plan["totals"] == [0, 1]
    assert any("reads as category indices [0,1]" in t for t in charts_spec.advisories(spec))
    # `int(True)` is 1: WP3a read [true, false, …] as index 1. Booleans are flags.
    flags = _plan(_waterfall([10, 5, 5, 20], totals=[True, False, False, True]))
    assert flags["totals"] == [0, 3]
    assert flags["roles"] == ["total", "increase", "increase", "total"]


def test_empty_totals_means_no_totals():
    plan = _plan(_waterfall([10, 5, 15], totals=[]))
    assert plan["totals"] == []
    assert plan["roles"] == ["increase", "increase", "increase"]
    assert plan["levels"] == [10.0, 15.0, 30.0]


def test_mixed_totals_list_is_rejected():
    spec = _waterfall([10, 5, 15], totals=[0, True])
    assert any("mixes category indices" in p for p in charts_spec.validate(spec))
    model = cm.normalise(spec)
    assert any("mixes category indices" in t for t in _kinds(model, "rejected"))
    # the list is ignored, so the model infers as if nothing were declared
    assert model["waterfall"]["totals"] == [0, 2]
    # a bool list of the wrong length and an index outside the chart are errors too
    assert charts_spec.validate(_waterfall([10, 5, 15], totals=[True, False]))
    assert any("inside 0..2" in p for p in charts_spec.validate(_waterfall([10, 5, 15], totals=[0, 7])))


def test_nothing_declared_infers_first_and_matching_last():
    plan = _plan(_waterfall([250, 150, -55, None, 345]))
    assert plan["totals"] == [0, 4], "345 = 250 + 150 − 55, so the last bar is the closing total"
    assert plan["roles"] == ["total", "increase", "decrease", "gap", "total"]
    # a last value that is not the running sum is a step
    assert _plan(_waterfall([100, 30, 12, 18, 15, 20]))["totals"] == [0]
    # the tolerance is relative: 12.4 is not 8.1 + 2.4 + 1.6 + 12.1 − 0.9 + 1.2
    assert _plan(_waterfall(P05_VALUES))["totals"] == [0]


def test_base_value_disables_inference_but_not_explicit_totals():
    plan = _plan(_waterfall([20, 10, 30], baseValue=100))
    assert plan["totals"] == []
    assert plan["baseValue"] == 100.0
    assert (plan["bottoms"], plan["tops"]) == ([100.0, 120.0, 130.0], [120.0, 130.0, 160.0])
    with_total = _plan(_waterfall([20, 10, 30], baseValue=100, totals=[2]))
    assert with_total["totals"] == [2]
    assert with_total["roles"] == ["increase", "increase", "total"]
    assert with_total["levels"] == [120.0, 130.0, 30.0]


def test_direction_words_override_the_sign_with_a_note():
    spec = _waterfall(None, series=[{"name": "v", "values": [10, 0.9, 3],
                                     "types": ["start", "decrease", "up"]}])
    model = cm.normalise(spec)
    assert model["series"][0]["values"] == [10.0, -0.9, 3.0]
    assert model["waterfall"]["roles"] == ["total", "decrease", "increase"]
    assert any("'decrease' with a value of 0.9 — read as -0.9" in t for t in _kinds(model, "advisory"))
    assert charts_spec.validate(spec) == []
    assert any("not a bar type" in p for p in charts_spec.validate(
        _waterfall(None, series=[{"name": "v", "values": [1, 2], "types": ["total", "sideways"]}])))


def test_conflicting_direction_words_across_series():
    spec = _waterfall(None, series=[
        {"name": "Internal", "values": [36, 9, 45], "types": ["total", "increase", "total"]},
        {"name": "Partner", "values": [13, 5, 18], "types": ["total", "increase", "increase"]},
    ])
    model = cm.normalise(spec)
    assert model["waterfall"]["totals"] == [0, 2], "the union wins"
    assert any("disagree about whether category 2 is a total" in t for t in _kinds(model, "advisory"))


# ------------------------------------------------------------------------- numbers and options


@pytest.mark.parametrize(("raw", "number", "how"), [
    (4, 4.0, "number"), (4.2, 4.2, "number"), ("4.2", 4.2, "string"), (" 6.2 ", 6.2, "string"),
    ("1e3", 1000.0, "string"), ("-.5", -0.5, "string"), ("+5.", 5.0, "string"),
    ("4,200", None, "invalid"), ("12%", None, "invalid"), ("$5.6m", None, "invalid"),
    ("1_000", None, "invalid"), ("", None, "invalid"), ("nan", None, "invalid"), ("1e999", None, "invalid"),
    (True, None, "invalid"), (None, None, "gap"), (float("inf"), None, "invalid"),
])
def test_numeric_strings_coerce_by_grammar(raw, number, how):
    assert cm.coerce_number(raw) == (number, how)


def test_numeric_strings_in_a_spec_are_advisories_and_the_rest_are_gaps():
    spec = {"type": "column", "categories": ["2022", "2023", "2024", "2025E"],
            "series": [{"name": "Revenue", "values": ["4.2", "4,200", " 6.2 ", 5]}]}
    model = cm.normalise(spec)
    assert model["series"][0]["values"] == [4.2, None, 6.2, 5.0]
    assert _kinds(model, "advisory") == ['series[0].values[0]: "4.2" read as 4.2',
                                         'series[0].values[2]: " 6.2 " read as 6.2']
    assert _kinds(model, "warn") == ['series[0].values[1]: "4,200" is not a number — treated as a gap slot']
    assert any('"4,200" is not a number' in p for p in charts_spec.validate(spec))
    assert not any('"4.2"' in p for p in charts_spec.validate(spec))


def test_option_strings_are_ignored_with_a_note():
    spec = {"type": "column", "categories": ["a"], "series": [{"values": [1]}],
            "options": {"fontSize": "11px", "gapWidth": "60%", "overlap": "10"}}
    model = cm.normalise(spec)
    assert model["options"]["fontSize"] is None and model["options"]["gapWidth"] == 60.0
    assert model["options"]["overlap"] == 10.0
    rejected = _kinds(model, "rejected")
    assert 'options.fontSize: "11px" is not a number — ignored' in rejected
    assert 'options.gapWidth: "60%" is not a number — ignored' in rejected
    problems = charts_spec.validate(spec)
    assert "fontSize must be a number" in problems and "gapWidth must be a number" in problems
    assert "overlap must be a number" not in problems, "a numeric string is an advisory, not an error"


def test_point_colors_three_shapes_plus_bad_keys_and_short_lists():
    base = {"type": "column", "categories": list("abcdef"), "series": [{"name": "s", "values": [1] * 6}]}
    listed = cm.normalise(dict(base, options={"pointColors": ["#C4C4CD", "#1a9afa", None, "#1A9AFA"]}))
    assert listed["options"]["pointColors"] == {"0": {"0": "C4C4CD", "1": "1A9AFA", "3": "1A9AFA"}}
    flat = cm.normalise(dict(base, options={"pointColors": {"2": "#7FC6FF", "first": "#000000"}}))
    assert flat["options"]["pointColors"] == {"0": {"2": "7FC6FF"}}
    assert any("key \"first\" is not a point index" in t for t in _kinds(flat, "rejected"))
    nested = cm.normalise(dict(base, options={"pointColors": {"0": {"5": "#7FC6FF", "x": "#000000"}}}))
    assert nested["options"]["pointColors"] == {"0": {"5": "7FC6FF"}}
    one = cm.normalise(dict(base, options={"pointColors": ["#123456"]}))
    assert one["options"]["pointColors"] == {"0": {"0": "123456"}}, "a short list colours only its points"
    # on a two-series chart the flat shapes are an error, and still read as series 0 (a warn note)
    two = dict(base, series=[{"name": "s", "values": [1] * 6}, {"name": "t", "values": [2] * 6}],
               options={"pointColors": ["#123456", "#654321"]})
    model = cm.normalise(two)
    assert model["options"]["pointColors"] == {"0": {"0": "123456", "1": "654321"}}
    assert _kinds(model, "warn")
    assert any("only works on a single-series chart" in p for p in charts_spec.validate(two))
    assert charts_spec.validate(dict(base, options={"pointColors": ["#C4C4CD", "#1a9afa"]})) == []


def test_colour_forms():
    assert cm.normalise_colour("#1a9afb") == ("1A9AFB", "hex")
    assert cm.normalise_colour("1A9AFB") == ("1A9AFB", "hex")
    assert cm.normalise_colour("#ABC") == ("AABBCC", "short")
    assert cm.normalise_colour("#1A9AFB80") == ("1A9AFB", "alpha")
    for bad in ("rgb(26,154,251)", "navy", "oklch(0.7 0.1 250)", "#12345", 12, None):
        assert cm.normalise_colour(bad)[0] is None, bad
    spec = {"type": "column", "categories": ["a", "b", "c", "d"], "series": [{"values": [1, 2, 3, 4]}],
            "colors": ["rgb(26,154,251)", "#1A9AFB80", "#ABC", "navy"]}
    model = cm.normalise(spec)
    assert model["colors"] == [None, "1A9AFB", "AABBCC", None], "an unusable colour keeps its position"
    assert _kinds(model, "advisory") == ['colors[1]: "#1A9AFB80" read as #1A9AFB — the alpha is dropped',
                                         'colors[2]: "#ABC" read as #AABBCC']
    assert len(_kinds(model, "rejected")) == 2
    assert charts_spec.validate(dict(spec, colors=["#1A9AFB80", "#ABC"])) == [], "tolerated with advisories"
    assert charts_spec.validate(spec), "rgb() and names are not colours"


def test_type_aliases_and_unknown():
    for written, canonical in [("column_clustered", "column"), ("grouped_column", "column"),
                               ("stacked_bar", "bar_stacked"), ("donut", "doughnut"),
                               ("bridge", "waterfall"), ("Stacked Column Chart", "column_stacked"),
                               ("waterfallchart", "waterfall"), ("Line-Markers", "line_markers"),
                               ("bar_chart", "bar"), ("clustered_bar", "bar")]:
        assert cm.resolve_type_name(written)["name"] == canonical, written
    unknown = cm.resolve_type_name("grouped_bar")
    assert unknown["name"] is None
    assert unknown["reason"] == "unknown chart type 'grouped_bar' (did you mean 'bar'?)"
    assert cm.resolve_type_name("histogram")["reason"] == "unknown chart type 'histogram'"
    assert "did you mean 'column'" in cm.resolve_type_name("colum")["reason"]
    for refused in ("radar", "bubble_3d", "stock", "surface_wireframe"):
        assert "not a chart object on Path A" in cm.resolve_type_name(refused)["reason"], refused
    assert "round 2" in cm.resolve_type_name("combo")["reason"]
    # an alias is read with an advisory, and validates
    spec = {"type": "column_clustered", "categories": ["a"], "series": [{"values": [1]}]}
    assert charts_spec.validate(spec) == []
    assert charts_spec.advisories(spec) == ["chart type 'column_clustered' read as 'column' — write the type name as listed"]
    assert "grouped_bar" not in cm.ALIASES, "p08 B: an unknown name must fail visibly in both readers"


def test_type_names_have_one_source():
    """The emitter's enum tables, the validator's names and the model's lists are one list."""
    assert set(emitter.TYPES) | set(emitter.DERIVED) == cm.STAGEFLOW_NAMES | cm.DERIVED_NAMES
    assert {name: pair[0] for name, pair in emitter.DERIVED.items()} == cm.DERIVED_BASES
    assert charts_spec.STAGEFLOW_TYPES == cm.STAGEFLOW_NAMES | cm.DERIVED_NAMES
    assert set(cm.ALIASES.values()) <= cm.KNOWN_NAMES
    import inspect

    import app.engine.charts_spec as spec_module

    assert "engine.emit" not in inspect.getsource(spec_module), "charts_spec no longer imports the emitter"
    assert emitter.resolve("grouped_bar") == (None, None, None), "never a clustered column in disguise"


# ------------------------------------------------------------------------------- axis and layout


def test_nice_range_is_deterministic():
    assert cm.nice_range(0, 140) == (0.0, 150.0, 50.0)
    assert cm.nice_range(-15, 20) == (-20.0, 20.0, 10.0)
    assert cm.nice_range(0, 3780) == (0.0, 4000.0, 1000.0)
    assert cm.nice_range(0, 1) == (0.0, 1.0, 0.25)
    assert cm.nice_range(0, 0) == (0.0, 1.0, 0.25), "hi <= lo becomes lo + 1"
    assert cm.nice_range(0, 0.07) == (0.0, 0.08, 0.02)
    assert cm.nice_range(0, 1000) == (0.0, 1000.0, 250.0), "a power of ten is found by multiplying"
    assert [cm.nice_range(0, 140) for _ in range(3)] == [(0.0, 150.0, 50.0)] * 3


def test_axis_range_includes_zero_and_handles_empty_and_single_category():
    column = cm.normalise({"type": "column", "categories": ["a", "b"], "series": [{"values": [40, 60]}]})
    assert cm.axis_range(column) == {"min": 0.0, "max": 60.0, "majorUnit": 20.0}
    stacked = cm.normalise({"type": "column_stacked", "categories": ["a", "b"],
                            "series": [{"values": [40, -10]}, {"values": [30, -25]}]})
    assert cm.axis_range(stacked) == {"min": -50.0, "max": 100.0, "majorUnit": 50.0}, "summed per sign"
    all_gap = cm.normalise(_waterfall([None, None]))
    assert all_gap["axis"] == {"min": 0.0, "max": 1.0, "majorUnit": 0.25}
    single = cm.normalise(_waterfall([42]))
    assert single["waterfall"]["roles"] == ["total"]
    assert single["axis"] == {"min": 0.0, "max": 60.0, "majorUnit": 20.0}
    zoomed = cm.normalise(_waterfall([2750, 140, 95, 55, -70, 2970], totals=[0, 5],
                                     valueAxis={"min": 2650}))
    # tops reach 3,040 (2,750 + 140 + 95 + 55); span 390 / 4 = 97.5 -> a step of 100
    assert zoomed["axis"] == {"min": 2650.0, "max": 3100.0, "majorUnit": 100.0}
    assert cm.axis_range(cm.normalise({"type": "pie", "categories": ["a"], "series": [{"values": [1]}]})) is None
    # a non-waterfall pins its axis only when the author gave min and max
    assert column["axis"] is None
    pinned = cm.normalise({"type": "column", "categories": ["a"], "series": [{"values": [1]}],
                           "options": {"valueAxis": {"min": 0, "max": 10}}})
    assert pinned["axis"] == {"min": 0.0, "max": 10.0, "majorUnit": 2.5}


def test_layout_column_and_bar_gutters():
    """The gutters PowerPoint needs around a pinned plot, measured in C3 (`AXIS_LINE_EM` and its
    neighbours): a line of axis labels 2.05 em + 1 px, half a tick label 0.7 em above the plot, the
    widest tick label plus 1 em + 2 px, an end category label's overhang."""
    spec = {"type": "column", "categories": ["2022", "2023"], "series": [{"name": "Revenue", "values": [40, 60]}],
            "options": {"numberFormat": "0"}}
    geometry = cm.layout(cm.normalise(spec), 400, 200, 10)
    ticks = geometry["ticks"]
    assert ticks == [0.0, 20.0, 40.0, 60.0]
    widest = max(0.55 * 10 * len(cm.format_number(t, "0")) for t in ticks)
    gutters = geometry["gutters"]
    assert gutters["left"] == pytest.approx(4 + widest + 10 * 1.0 + 2)
    assert gutters["bottom"] == pytest.approx(4 + 10 * 2.05 + 1)
    assert gutters["top"] == pytest.approx(4 + 10 * 1.25), "clustered labels sit outside the end"
    assert gutters["right"] == pytest.approx(4)
    assert geometry["plotArea"] == pytest.approx({
        "x": gutters["left"] / 400, "y": gutters["top"] / 200, "w": (400 - gutters["left"] - 4) / 400,
        "h": (200 - gutters["top"] - gutters["bottom"]) / 200}, abs=5e-5)
    inside = cm.layout(cm.normalise(dict(spec, options={"numberFormat": "0", "labelPosition": "center"})), 400, 200, 10)
    assert inside["gutters"]["top"] == pytest.approx(4 + 10 * 0.7), "half the top tick label above the plot"
    bar = cm.layout(cm.normalise(dict(spec, type="bar")), 400, 200, 10)
    assert bar["gutters"]["left"] == pytest.approx(4 + 0.55 * 10 * 4 + 10 + 2), "the widest category"
    assert bar["gutters"]["bottom"] == pytest.approx(4 + 10 * 2.05 + 1), "the value axis runs along the bottom"
    assert bar["gutters"]["right"] == pytest.approx(4 + 10 * 2.2)
    two = cm.layout(cm.normalise(dict(spec, series=[{"name": "A", "values": [1, 2]},
                                                    {"name": "B", "values": [3, 4]}])), 400, 200, 10)
    assert two["gutters"]["bottom"] == pytest.approx(4 + 10 * 1.9 + 10 * 0.3 + 10 * 2.05 + 1), \
        "a bottom legend row, a gap, then the category labels"
    assert two["legend"] == {"x": cm._round_to((400 - 2 * (13 + 5.5 + 6)) / 2 / 400, 4),
                             "y": cm._round_to((200 - 4 - 19) / 200, 4),
                             "w": cm._round_to(2 * (13 + 5.5 + 6) / 400, 4), "h": cm._round_to(19 / 200, 4)}
    long_end = cm.layout(cm.normalise(dict(spec, categories=["a", "b", "c", "An extremely long last label"])),
                         400, 200, 10)
    slot = (400 - long_end["gutters"]["left"] - 4) / 4
    assert long_end["gutters"]["right"] == pytest.approx(4 + (0.55 * 10 * 28 - slot) / 2), "the overhang"
    assert cm.layout(cm.normalise(spec), 400, 200, 100)["sizePx"] == 48.0, "the size is clamped to 48"
    sized = cm.normalise(dict(spec, options={"fontSize": 12}))
    assert cm.layout(sized, 400, 200, 30)["sizePx"] == 12.0, "options.fontSize wins"


def test_legend_box_sits_where_the_gutter_is():
    """The pinned legend box, per position: bottom on the pad, top under the title, sides centred on
    the plot; a horizontal legend too wide for the frame wraps into rows."""
    two = {"type": "column", "categories": ["a", "b"], "title": "T",
           "series": [{"name": "Alpha", "values": [1, 2]}, {"name": "Beta", "values": [3, 4]}]}
    for position in ("bottom", "top", "left", "right", "topRight"):
        model = cm.normalise(dict(two, options={"legend": position}))
        geometry = cm.layout(model, 400, 200, 10)
        box, area = geometry["legend"], geometry["plotArea"]
        assert box is not None and 0 <= box["x"] and box["x"] + box["w"] <= 1.0001 and box["y"] + box["h"] <= 1.0001
        if position == "bottom":
            assert box["y"] >= area["y"] + area["h"], "below the plot and its labels"
        elif position == "top":
            assert box["y"] + box["h"] <= area["y"] + 1e-4
        elif position == "left":
            assert box["x"] + box["w"] <= area["x"] + 1e-4
        else:
            assert box["x"] >= area["x"] + area["w"] - 1e-4
    assert cm.layout(cm.normalise(dict(two, options={"legend": False})), 400, 200, 10)["legend"] is None
    many = dict(two, series=[{"name": f"Series with a long name {k}", "values": [1, 2]} for k in range(6)])
    wrapped = cm.layout(cm.normalise(many), 400, 200, 10)["legend"]
    assert wrapped["h"] * 200 > 10 * 1.9 + 1, "several rows"


def test_layout_pinned_flag():
    column = {"type": "column", "categories": ["a", "b"], "series": [{"values": [1, 2]}]}
    assert cm.layout(cm.normalise(column), 300, 200, 11)["pinned"] is False
    authored = dict(column, options={"plotArea": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}})
    geometry = cm.layout(cm.normalise(authored), 300, 200, 11)
    assert geometry["pinned"] is True and geometry["plotArea"] == {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}
    lines = dict(column, options={"valueAxis": {"min": 0, "max": 4}, "referenceLines": [{"value": 2}]})
    assert cm.layout(cm.normalise(lines), 300, 200, 11)["pinned"] is True
    unplaceable = dict(column, options={"referenceLines": [{"value": 2}]})
    assert cm.layout(cm.normalise(unplaceable), 300, 200, 11)["pinned"] is False, "no axis, nothing drawn"
    # Connectors are a series of the chart itself (C3's fallback): they pin nothing.
    assert cm.layout(cm.normalise(_waterfall([10, 5, 15])), 300, 200, 11)["pinned"] is False
    lonely = cm.normalise(_waterfall([10], connectors=False))
    assert cm.layout(lonely, 300, 200, 11)["pinned"] is False
    assert cm.layout(cm.normalise(_waterfall([10], connectors=False, labelPosition="outEnd")),
                     300, 200, 11)["pinned"] is True, "outEnd labels are text boxes placed on the plot"


# ------------------------------------------------------------------------------- number formats


@pytest.mark.parametrize(("fmt", "value", "text"), [
    ("General", 4.0, "4"), ("General", 0.1 + 0.2, "0.3"), ("General", 1e-7, "0"), ("General", 1e21, "1e+21"),
    ("General", -0.0, "0"), ("General", 12345678.9, "12345678.9"), ("General", 0.00001234, "0.000012"),
    ("General", -4.25, "-4.25"), (None, 3, "3"),
    ("0", 3.5, "4"), ("0", 2.5, "3"), ("0.0", 3.25, "3.3"), ("0.00", 2.675, "2.68"),
    ("0.0#", 4, "4.0"), ("0.0#", 4.25, "4.25"), ("0.##", 4, "4"), ("#.##", 0.5, ".5"),
    ("#,##0", 2750, "2,750"), ("#,##0", -1234567, "-1,234,567"), ("#,##0,\"k\"", 1234567, "1,235k"),
    ("0%", 0.742, "74%"), ('0"%"', 74, "74%"), ('0.0"%"', -2, "-2.0%"), ('"$"#,##0.0"m"', 5.6, "$5.6m"),
    ('"+"0;"+"0', -10, "+10"), ('"-"0;"-"0', 15, "-15"), ('"-"0;"-"0', -35, "-35"),
    ("0;(0)", -5, "(5)"), ("0;(0)", 5, "5"), ('0;(0);"-"', 0, "-"), ("00", 7, "07"),
    ("#,##0_);(#,##0)", -1200, "(1,200)"), ("[Red]0.0", 2.25, "2.3"), ("0", -0.2, "0"),
])
def test_format_number_table(fmt, value, text):
    assert cm.format_number(value, fmt) == text


def test_general_format_for_waterfall():
    assert cm.normalise(_waterfall([10, 5, 15]))["waterfall"]["formats"]["total"] == "0"
    plan = cm.normalise(_waterfall(P05_VALUES))["waterfall"]
    assert plan["formats"] == {"increase": '"+"0.0#;"+"0.0#', "decrease": '"-"0.0#;"-"0.0#', "total": "0.0#"}
    assert cm.format_number(4.0, plan["formats"]["increase"]) == "+4.0", "never '+4.' as 0.## printed"
    authored = cm.normalise(_waterfall([10, 5], numberFormat="#,##0"))["waterfall"]["formats"]
    assert authored["increase"] == '"+"#,##0;"+"#,##0'
    owned = cm.normalise(_waterfall([10, 5], numberFormat="0;(0)"))["waterfall"]["formats"]
    assert owned == {"increase": "0;0", "decrease": "(0);(0)", "total": "0;(0)"}, \
        "two sections: the role picks the author's section (C1 review #5)"


# ------------------------------------------------------------------------- the plan (§3.2)


def test_crossing_bar_splits_into_four_series():
    """p05-D: a bridge that crosses zero draws in PowerPoint because each bar is cut at zero."""
    spec = _waterfall([20, -35, 10, -8, 25, 12], totals=[0, 5], numberFormat="0",
                      categories=["FY23", "Price", "Volume", "Mix", "Cost out", "FY24"])
    spec["series"][0]["name"] = "EBITDA"
    plan = _plan(spec)
    assert plan["negative"] is True
    assert [s["name"] for s in plan["series"]] == ["Base", "EBITDA", f"Base{MINUS}", f"EBITDA{MINUS}"]
    cells = _cells(plan)
    assert cells["Base"] == [0.0, 0.0, None, None, 0.0, 0.0]
    assert cells["EBITDA"] == [20.0, 20.0, None, None, 12.0, 12.0]
    assert cells[f"Base{MINUS}"] == [None, 0.0, -5.0, -5.0, 0.0, None]
    assert cells[f"EBITDA{MINUS}"] == [None, -15.0, -10.0, -8.0, -13.0, None]
    price = next(s for s in plan["segments"] if s["category"] == 1)
    assert price["label"] == {"series": 3, "point": 1, "text": "-35", "sign": "-", "format": '"-"0;"-"0'}
    assert price["hide"] == [[1, 1]]
    cost = next(s for s in plan["segments"] if s["category"] == 4)
    assert cost["label"]["text"] == "+25" and cost["label"]["series"] == 1 and cost["hide"] == [[3, 4]]
    assert _plan(_waterfall([20, 5, 25]))["negative"] is False


def test_stacked_pieces_follow_powerpoints_order():
    """The worked example of WP-C §3.2 (fixture J, parity spec `stacked-crossing`), cell by cell."""
    spec = _waterfall(None, series=[{"name": "Internal", "values": [30, -25, -10]},
                                    {"name": "Partner", "values": [0, -15, 0]}],
                      categories=["FY23", "Q1", "FY24"], totals=[0, 2], numberFormat="0")
    plan = _plan(spec)
    cells = _cells(plan)
    assert cells == {
        "Base": [0.0, 0.0, None],
        "Internal": [30.0, 15.0, None],
        "Partner": [None, 15.0, None],
        f"Base{MINUS}": [None, 0.0, 0.0],
        f"Internal{MINUS}": [None, -10.0, -10.0],
        f"Partner{MINUS}": [None, None, None],
    }
    by_key = {(s["category"], s["source"]): s for s in plan["segments"]}
    assert (by_key[(1, 0)]["lo"], by_key[(1, 0)]["hi"]) == (-10.0, 15.0), "Internal straddles zero"
    assert (by_key[(1, 1)]["lo"], by_key[(1, 1)]["hi"]) == (15.0, 30.0)
    assert by_key[(1, 0)]["label"]["text"] == "-25" and by_key[(1, 0)]["label"]["series"] == 4
    assert by_key[(1, 0)]["hide"] == [[1, 1]]
    assert by_key[(1, 1)]["label"] == {"series": 2, "point": 1, "text": None, "sign": "-",
                                       "format": '"-"0;"-"0'}
    assert cm.format_number(15.0, by_key[(1, 1)]["label"]["format"]) == "-15", "Partner reads -15 natively"
    assert by_key[(2, 0)]["label"]["series"] == 4 and by_key[(2, 0)]["label"]["text"] is None
    assert (0, 1) not in by_key and (2, 1) not in by_key, "a zero piece is blank, never 0"


def test_stacked_non_crossing_bars_keep_series_zero_nearest_the_axis():
    """Fixture E: in a rising and a falling bar alike, Internal is always the bottom colour."""
    spec = _waterfall(None, series=[{"name": "Internal", "values": [36, 9, 14, -11, 48]},
                                    {"name": "Partner", "values": [13, 5, 6, -4, 20]}], totals=[0, 4])
    plan = _plan(spec)
    assert plan["negative"] is False
    assert len(plan["series"]) == 3
    by_key = {(s["category"], s["source"]): s for s in plan["segments"]}
    for category in range(5):
        internal, partner = by_key[(category, 0)], by_key[(category, 1)]
        assert internal["hi"] == pytest.approx(partner["lo"]), f"category {category}: Internal below Partner"
    falling = by_key[(3, 0)]
    assert falling["role"] == "decrease"
    # 49 -> 63 -> 83, then -15 down to 68: Internal's 11 sits on 68, Partner's 4 above it
    assert (falling["lo"], falling["hi"]) == (68.0, 79.0), "a falling bar still stacks Internal lowest"
    assert _cells(plan)["Base"] == [0.0, 49.0, 63.0, 68.0, 0.0]
    assert charts_spec.validate(spec) == []


def test_entirely_below_zero_has_blank_plus_side():
    plan = _plan(_waterfall([-20, -10, -30], totals=[0, 2], numberFormat="0"))
    cells = _cells(plan)
    assert cells["Base"] == [None, None, None] and cells[list(cells)[1]] == [None, None, None]
    assert cells[f"Base{MINUS}"] == [0.0, -20.0, 0.0]
    assert cells[f"Value{MINUS}"] == [-20.0, -10.0, -30.0]
    texts = [cm.format_number(p["cell"], s["label"]["format"]) for s in plan["segments"] for p in s["parts"]]
    assert texts == ["-20", "-10", "-30"]


def test_zero_step_keeps_its_cell_and_label():
    plan = _plan(_waterfall([20, 0, -20, 5], totals=[0]))
    assert plan["negative"] is False
    assert _cells(plan) == {"Base": [0.0, 20.0, 0.0, 0.0], "Value": [20.0, 0.0, 20.0, 5.0]}
    assert plan["roles"] == ["total", "increase", "decrease", "increase"]
    zero = next(s for s in plan["segments"] if s["category"] == 1)
    assert zero["parts"] == [{"series": 1, "cell": 0.0}]
    assert cm.format_number(0.0, zero["label"]["format"]) == "+0"
    assert plan["connectors"] == [{"index": 0, "level": 20.0}, {"index": 1, "level": 20.0},
                                  {"index": 2, "level": 0.0}]


def test_mixed_signs_are_rejected():
    spec = _waterfall(None, series=[{"name": "a", "values": [10, 20, 30]},
                                    {"name": "b", "values": [1, -2, 3]}], totals=[0])
    assert any("mixes rises and falls" in p for p in charts_spec.validate(spec))
    model = cm.normalise(spec)
    assert any("mixes rises and falls" in t for t in _kinds(model, "warn")), "drawn anyway, and said so"
    assert model["waterfall"]["roles"][1] == "increase", "the net direction"


def test_gap_slots_and_all_gap_waterfall():
    plan = _plan(_waterfall([250, 150, None, -55, 345]))
    assert plan["roles"] == ["total", "increase", "gap", "decrease", "total"]
    assert plan["levels"][2] == 400.0, "a gap keeps the running total"
    assert [c["index"] for c in plan["connectors"]] == [0, 3], "no connector touches the gap"
    empty = cm.normalise(_waterfall([None, None, None]))
    assert empty["type"] == "waterfall"
    assert empty["waterfall"]["roles"] == ["gap"] * 3 and empty["waterfall"]["connectors"] == []
    assert empty["overlay"] is False


def test_reverse_category_axis_mirrors_connectors():
    spec = _waterfall([100, 40, 140], gapWidth=50, valueAxis={"min": 0, "max": 200},
                      plotArea={"x": 0, "y": 0, "w": 1, "h": 1})
    mirrored = dict(spec, options=dict(spec["options"], categoryAxis={"reverse": True}))
    assert _plan(spec) == _plan(mirrored), "the plan is unchanged"
    box = (0.0, 0.0, 6.0, 2.0)
    forward = emitter.overlay_geometry(spec, box)
    backward = emitter.overlay_geometry(mirrored, box)
    assert [line["index"] for line in forward] == [0, 1] == [line["index"] for line in backward]
    for a, b in zip(forward, backward, strict=False):
        assert b["x1"] == pytest.approx(6.0 - a["x1"]) and b["x2"] == pytest.approx(6.0 - a["x2"])
        assert b["y1"] == pytest.approx(a["y1"])


def test_out_of_range_connector_is_omitted_with_a_note():
    # A bridge zoomed to 2,650 with a step down to 2,590: that level is off the axis.
    spec = _waterfall([2750, -160, 140, 95, 55, 2880], totals=[0, 5], valueAxis={"min": 2650})
    model = cm.normalise(spec)
    assert model["axis"] == {"min": 2650.0, "max": 2900.0, "majorUnit": 100.0}
    assert model["waterfall"]["levels"][1] == 2590.0
    assert [c["index"] for c in model["waterfall"]["connectors"]] == [0, 2, 3, 4]
    info = _kinds(model, "info")
    assert len(info) == 1 and "category 1" in info[0] and "lies outside the value axis" in info[0]
    lines = emitter.overlay_geometry(dict(spec, options=dict(spec["options"], plotArea={
        "x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8})), (0.0, 0.0, 6.0, 3.0))
    assert [line["index"] for line in lines] == [0, 2, 3, 4], "omitted, never clamped to the plot edge"


def test_model_is_json_stable():
    """What the JS twin returns through JSON must equal the Python model: no tuples, no int keys."""
    specs = [
        _waterfall(None, series=[{"name": "Internal", "values": [30, -25, -10]},
                                 {"name": "Partner", "values": [0, -15, 0]}], totals=[0, 2]),
        {"type": "column", "categories": ["a", "b"], "series": [{"values": ["1.5", 2]}],
         "options": {"pointColors": {"0": {"1": "#ABC"}}, "valueAxis": True}},
    ]
    for spec in specs:
        model = cm.normalise(spec)
        assert json.loads(json.dumps(model)) == model
        text = json.dumps(model)
        assert "NaN" not in text and "Infinity" not in text


def test_js_number_string_matches_javascript():
    cases = {4.0: "4", 0.1: "0.1", 1e21: "1e+21", 1e-7: "1e-7", 1.5e-7: "1.5e-7", 123.456: "123.456",
             1e-6: "0.000001", 100.0: "100", -0.0: "0", -2.5: "-2.5", 2.0 ** 60: "1152921504606847000",
             1e16: "10000000000000000"}
    for value, text in cases.items():
        assert cm.js_number_string(value) == text, value
    assert cm.js_round(2.5) == 3.0 and cm.js_round(-2.5) == -2.0 and cm.js_round(0.49999999999999994) == 0.0
    assert math.isinf(cm.js_round(float("inf")))


#: Specs a fuzz sweep of 3,000 malformed specs found to crash the first draft of the model or the
#: validator (unhashable option values, a non-list series) or to reach python-pptx out of range.
MALFORMED = [
    {"type": "column", "categories": ["a"], "series": [{"values": [1]}], "options": {"labelPosition": ["outEnd"]}},
    {"type": "column", "categories": ["a"], "series": [{"values": [1]}], "options": {"legend": {"x": 1}}},
    {"type": "column_stacked", "categories": ["a"], "series": float("nan"), "options": {}},
    {"type": "bar", "categories": [], "series": float("inf"), "options": {"baseValue": "pie"}},
    {"type": "bar", "categories": ["a", "b"], "series": [{"values": [1, -2]}], "options": {"gapWidth": -1}},
    {"type": "waterfall", "categories": ["a", "b"], "series": [{"values": [1, 2]}], "options": {"gapWidth": 1e300}},
    {"type": "column", "categories": ["a"], "series": [{"values": [1]}], "options": {"overlap": -600}},
    {"type": "pie", "categories": ["a"], "series": [{"values": [1], "explosion": -5}]},
    {"type": "line", "categories": ["a"], "series": [{"values": [1], "lineWidth": 9999, "markerSize": 1}]},
    {"type": [], "categories": {"a": 1}, "series": {"values": [1]}, "options": [], "colors": 3},
    {"type": "bar\x01chart", "categories": ["a"], "series": [{"values": [1]}]},
    {"type": "column", "categories": ["a", "b"], "series": [{"values": [1, 2]}],
     "options": {"valueAxis": {"minorUnit": 0}, "gridlines": {"width": -1}}},
]


@pytest.mark.parametrize("spec", MALFORMED)
def test_normalise_and_validate_never_raise(spec):
    model = cm.normalise(spec)
    json.dumps(model, allow_nan=False)
    cm.layout(model, 300, 200, 11)
    assert isinstance(charts_spec.validate(spec), list)
    assert isinstance(charts_spec.advisories(spec), list)
    if model["type"] is not None:
        for key in ("gapWidth", "overlap"):
            low, high = cm.NUMBER_RANGES[key]
            assert low <= model["options"][key] <= high, "an out-of-range value never reaches the file"


# ------------------------------------------------------------------ the C1 review's findings (C2)


@pytest.mark.parametrize("fmt", ["0", "#,##0;(#,##0)", "+0;-0", "0;(0)", "+0", '"+"0;"-"0', "0.0#", '0.0"%"'])
def test_role_formats_print_the_step_on_either_side_of_zero(fmt):
    """C1 review #5: a step's cell is a magnitude on the side of zero its piece is drawn, so the
    role picks the format; the native text of every one-part segment equals the text the same step
    gets as frozen text, and a fall never reads like a rise (above or below zero)."""
    spec = _waterfall([100, -30, 20, -150, 25, -35], totals=[0], numberFormat=fmt,
                      categories=["FY23", "Price", "Volume", "Crash", "Up below", "Down below"])
    plan = _plan(spec)
    assert plan["negative"] is True
    steps = plan["values"]
    for segment in plan["segments"]:
        label = segment["label"]
        frozen = cm.format_number(steps[segment["category"]], label["format"])
        if label["text"] is not None:
            assert label["text"] == frozen
            continue
        for part in segment["parts"]:
            if part["series"] == label["series"]:
                assert cm.format_number(part["cell"], label["format"]) == frozen, (fmt, segment)
        if segment["role"] == "decrease":
            assert cm.format_number(steps[segment["category"]], label["format"]) != cm.format_number(
                -steps[segment["category"]], plan["formats"]["increase"]), (fmt, "a fall reads as a fall")


def test_signed_format_by_section():
    assert cm.signed_format("#,##0;(#,##0)", "+") == "#,##0;#,##0"
    assert cm.signed_format("#,##0;(#,##0)", "-") == "(#,##0);(#,##0)"
    assert cm.signed_format('0;(0);"-"', "-") == '(0);(0);"-"', "the zero section stays"
    assert cm.signed_format("+0", "+") == "+0;+0"
    assert cm.signed_format("+0", "-") == '"-"0;"-"0', "the author's + is a rise sign; a fall drops it"
    assert cm.signed_format('"+"0.0', "-") == '"-"""0.0;"-"""0.0'
    assert cm.format_number(-3, cm.signed_format('"+"0.0', "-")) == "-3.0"
    assert cm.signed_format("0.00E+00", "-") == '"-"0.00E+00;"-"0.00E+00', "E+ is not a literal +"
    assert cm.signed_format("0", "") == "0", "a total keeps the author's format"


def test_line_widths_dashes_and_units_keep_to_ooxml_ranges():
    """C1 review #6: every number the emitter hands to python-pptx has a range, in the model and
    in `validate` alike, so a spec that validates always builds."""
    column = {"type": "column", "categories": ["a", "b"], "series": [{"values": [1, 2]}]}
    waterfall = _waterfall([100, -30, 70], totals=[0, 2])
    cases = [
        (dict(column, options={"valueAxis": {"minorUnit": 0}}), "valueAxis.minorUnit must be above 0"),
        (dict(column, options={"valueAxis": {"minorUnit": -1}}), "valueAxis.minorUnit must be above 0"),
        (dict(column, options={"gridlines": {"width": -1}}), "gridlines.width must be between 0 and 1584"),
        (dict(column, options={"gridlines": {"width": 5000}}), "gridlines.width must be between 0 and 1584"),
        (dict(column, options={"valueAxis": {"min": 0, "max": 5},
                               "referenceLines": [{"value": 2, "width": -2}]}),
         "referenceLines[0].width must be between 0 and 1584"),
        (dict(waterfall, options=dict(waterfall["options"], connectors={"width": 2000})),
         "connectors.width must be between 0 and 1584"),
        (dict(column, options={"dash": [1e9, 1]}), "dash must be a list of px lengths above 0 and at most 1000, or a preset dash name"),
    ]
    for spec, message in cases:
        assert message in charts_spec.validate(spec), (spec["options"], charts_spec.validate(spec))
        model = cm.normalise(spec)
        assert _kinds(model, "rejected"), "the model ignores the value and says so"
    ignored = cm.normalise(dict(column, options={"gridlines": {"width": 5000}}))
    assert ignored["options"]["gridlines"]["width"] == 0.75
    assert cm.normalise(dict(column, options={"valueAxis": {"minorUnit": 0}}))["options"]["valueAxis"]["minorUnit"] is None


def test_stacked_bars_always_overlap_fully():
    """C1 review #8: at overlap 0 PowerPoint draws a stacked chart's series side by side."""
    spec = _waterfall([100, -30, 70], totals=[0, 2], overlap=0)
    model = cm.normalise(spec)
    assert model["options"]["overlap"] == 100.0
    assert any("options.overlap" in t and "read as 100" in t for t in _kinds(model, "advisory"))
    assert charts_spec.validate(spec) == []
    stacked = {"type": "column_stacked", "categories": ["a"], "series": [{"values": [1]}, {"values": [2]}],
               "options": {"overlap": 20}}
    assert cm.normalise(stacked)["options"]["overlap"] == 100.0
    clustered = dict(stacked, type="column")
    assert cm.normalise(clustered)["options"]["overlap"] == 20.0, "a clustered chart keeps its overlap"


def test_zero_step_below_zero_is_frozen_text_on_the_base():
    """C1 review #9: PowerPoint stacks a 0 cell with the positives, at the zero line — so below
    zero a flat step has no cell, and its `+0` is frozen text on `Base−`, whose end is the level."""
    spec = _waterfall([-20, 0, 5, -15], totals=[0, 3], numberFormat="0",
                      categories=["Start", "Flat", "Up", "End"])
    plan = _plan(spec)
    cells = _cells(plan)
    assert all(cells[name][1] is None for name in cells if name != f"Base{MINUS}"), "no 0 cell anywhere"
    assert cells[f"Base{MINUS}"][1] == -20.0
    flat = next(s for s in plan["segments"] if s["category"] == 1)
    assert flat["parts"] == [] and (flat["lo"], flat["hi"]) == (-20.0, -20.0)
    assert flat["label"] == {"series": 2, "point": 1, "text": "+0", "sign": "+", "format": '"+"0;"+"0'}
    assert [c["level"] for c in plan["connectors"]] == [-20.0, -20.0, -15.0]
    above = _plan(_waterfall([20, 0, -20, 5], totals=[0]))
    assert next(s for s in above["segments"] if s["category"] == 1)["parts"] == [{"series": 1, "cell": 0.0}]


def test_totals_as_words_and_unusable_lists():
    """C1 review #11: a list with no usable entry is no declaration (inference runs); a list of bar
    words is read like `series.types`, with an advisory."""
    values = [100, -30, 20, 90]
    words = _waterfall(values, totals=["total", "step", "step", "total"])
    plan = _plan(words)
    assert plan["totals"] == [0, 3] and plan["roles"] == ["total", "decrease", "increase", "total"]
    assert any("gives a word per bar" in t for t in charts_spec.advisories(words))
    assert charts_spec.validate(words) == []
    assert _plan(_waterfall(values, totals=[99]))["totals"] == [0, 3], "no usable index: inferred"
    assert any("inside 0..3" in p for p in charts_spec.validate(_waterfall(values, totals=[99])))
    junk = _waterfall(values, totals=["sideways", "nope", "x", "y"])
    assert _plan(junk)["totals"] == [0, 3] and charts_spec.validate(junk)
    assert _plan(_waterfall(values, totals=[]))["totals"] == [], "[] still means no totals"
    assert any("mixes category indices" in p for p in charts_spec.validate(_waterfall(values, totals=[0, "total"])))


# The agreement fuzz (C1 review #12): the reviewer's generator, kept deterministic.
_FUZZ_VALUES = [None, True, False, 0, -1, 1, 2.5, 1e9, -1e9, "", "x", "4", " 6.2 ", "#ABC", "#1A9AFA", "navy",
                [], [1], [0, 1], [True], ["a"], {}, {"a": 1}, {"0": "#1A9AFA"}, {"0": {"0": "#1A9AFA"}}, "outEnd",
                "center", "bottom", "right", "dash", [3, 2], [-1], 0.5, 90, 500, 501, -100, 100, 101,
                " total", ["total", " end"], [1e9, 1], 1585, -5, 5000, ["total", "step"], 0.0001]
_FUZZ_TYPES = ["column", "bar", "line", "area", "pie", "doughnut", "scatter", "waterfall", "column_stacked",
               "bar_stacked_100", "line_markers", "Column", "stacked_column", "donut", "bridge"]


def _fuzz_value(rng, depth=0):
    if depth < 2 and rng.random() < 0.25:
        if rng.random() < 0.5:
            keys = sorted(cm.AXIS_KEYS) + ["color", "width", "dash", "value", "label"]
            return {rng.choice(keys): _fuzz_value(rng, depth + 1) for _ in range(rng.randint(1, 3))}
        return [_fuzz_value(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return rng.choice(_FUZZ_VALUES)


def fuzz_spec(rng) -> dict:
    """One random, often malformed, `data-chart` spec (also used by the emitter's build fuzz)."""
    kind = rng.choice(_FUZZ_TYPES)
    n = rng.randint(2, 4)
    spec: dict = {"type": kind, "categories": [f"c{i}" for i in range(n)]}
    series = []
    for k in range(rng.randint(1, 2)):
        if kind == "scatter":
            series.append({"name": f"S{k}", "x": [rng.randint(0, 9) for _ in range(n)],
                           "y": [rng.randint(0, 9) for _ in range(n)]})
        else:
            series.append({"name": f"S{k}", "values": [rng.choice([rng.randint(-9, 9), rng.randint(1, 9)])
                                                       for _ in range(n)]})
        if rng.random() < 0.2:
            series[-1][rng.choice(["totals", "types", "dash", "lineWidth", "marker", "smooth", "line",
                                   "dataLabels"])] = _fuzz_value(rng)
    spec["series"] = series
    options = {}
    for _ in range(rng.randint(0, 3)):
        options[rng.choice(sorted(cm.OPTION_KEYS))] = _fuzz_value(rng)
    if options:
        spec["options"] = options
    if rng.random() < 0.2:
        spec["colors"] = [rng.choice(["#1A9AFA", "#ABC", "navy", None, "#1A9AFA80"])]
    return spec


def test_validate_and_the_model_agree():
    """`validate`'s own rules report every spec the model reads with a `rejected` or `warn` note —
    so lint shows what the model dropped or re-read (C1 review #12). 4,000 specs, fixed seed."""
    import random

    rng = random.Random(20260925)
    silent = []
    for _ in range(4000):
        spec = fuzz_spec(rng)
        loud = [n["text"] for n in cm.normalise(spec)["notes"] if n["kind"] in ("rejected", "warn")]
        if loud and not charts_spec.rule_problems(spec):
            silent.append((spec, loud[0]))
    assert silent == [], silent[:3]
    # the words `validate` and the model trim alike (ASCII only)
    nbsp = _waterfall(None, series=[{"name": "v", "values": [10, 2, 12], "types": [" total", "up", "end"]}])
    assert any("is not a bar type" in p for p in charts_spec.validate(nbsp))
    smooth = {"type": "line", "categories": ["a"], "series": [{"values": [1], "smooth": "yes"}]}
    assert "series[0].smooth must be true or false" in charts_spec.validate(smooth)


def test_control_characters_are_a_validation_error():
    """C1 review #13: a string a PowerPoint file cannot hold is named by `validate`."""
    spec = {"type": "column", "categories": ["a\x01", "b"], "series": [{"name": "Rev\x0benue", "values": [1, 2]}]}
    problems = charts_spec.validate(spec)
    assert any(p.startswith("categories[0]:") and "control character" in p for p in problems), problems
    assert any(p.startswith("series[0].name:") and "control character" in p for p in problems), problems
    assert charts_spec.validate({"type": "column", "categories": ["a\tb"], "series": [{"values": [1]}]}) == []


# ------------------------------------------------ render check 2026-09-29 (rendercheck/00-PLAN.md §2)

#: d2_s08: one colour, no totals declared, a final "Pro-Forma Net" that equals the running sum.
D2_S08 = {"type": "waterfall",
          "categories": ["Base EBITDA", "Sourcing", "Overhead", "Systems Consolidation", "Pricing Lift",
                         "Tax Planning", "One-off Charges", "Pro-Forma Net"],
          "series": [{"name": "Savings", "values": [11.6, 3.2, 2.4, 1.7, 5.9, 1.3, -4.5, 21.6]}],
          "colors": ["#00AECF"], "options": {"valueAxis": {"min": 0, "max": 35, "majorUnit": 5}}}
#: d1_s06: two colours, a final 214 that is not the running sum (83) and would leave `max: 240`.
D1_S06 = {"type": "waterfall",
          "categories": ["Equity Value at Entry", "Debt Financing", "Partner Capital", "Cost Savings",
                         "Growth Initiatives", "Working Capital Release", "Pro-forma Enterprise Value"],
          "series": [{"name": "Value Bridge", "values": [180, -97, -28, 10, 11, 7, 214]}],
          "colors": ["#021D44", "#00AECF"], "options": {"valueAxis": {"visible": False, "min": 0, "max": 240}}}


def test_one_colour_waterfall_paints_rises_and_falls_alike_and_totals_in_the_ink():
    """Render check #5a: a single colour is the bars' colour, not the rise colour — and the totals
    take the chart's ink rather than a grey the author never asked for."""
    plan = _plan(D2_S08)
    assert plan["colors"] == {"increase": "00AECF", "decrease": "00AECF", "total": cm.WATERFALL_INK}
    assert plan["inkRoles"] == ["total"]
    assert plan["roles"][6] == "decrease" and plan["roles"][0] == plan["roles"][7] == "total"
    # an unusable single colour is no colour: every role takes its own default
    fallback = _plan(dict(D2_S08, colors=["navy"]))
    assert fallback["colors"] == cm.WATERFALL_COLORS and fallback["inkRoles"] == []


def test_two_colour_waterfall_totals_take_the_first_colour():
    """Render check #5b: `[increase, decrease]`, and the totals in `colors[0]`."""
    plan = _plan(D1_S06)
    assert plan["colors"] == {"increase": "021D44", "decrease": "00AECF", "total": "021D44"}
    assert plan["inkRoles"] == []
    three = _plan(dict(D1_S06, colors=["#021D44", "#00AECF", "#516467"]))
    assert three["colors"] == {"increase": "021D44", "decrease": "00AECF", "total": "516467"}
    assert _plan(dict(D1_S06, colors=[None, "#00AECF"]))["colors"]["total"] == cm.WATERFALL_COLORS["total"]
    assert _plan(dict(D1_S06, colors=[]))["colors"] == cm.WATERFALL_COLORS


def test_last_bar_is_a_total_when_its_category_says_so():
    """Render check #5c: 'Pro-forma …', 'Net …', 'Total', 'Closing …' end a bridge on the axis."""
    values = [100, 30, -10, 150]                       # 150 is not 120: by the sum alone, a step
    assert _plan(_waterfall(values))["totals"] == [0]
    for name in ("Total", "Net result", "Pro-forma EV", "Pro forma", "PROFORMA value", "Ending cash",
                 "Closing", "Final", "Combined entity"):
        assert _plan(_waterfall(values, categories=["a", "b", "c", name]))["totals"] == [0, 3], name
    for name in ("Subtotals", "Netherlands", "Totally new", "Networking"):
        assert _plan(_waterfall(values, categories=["a", "b", "c", name]))["totals"] == [0], name
    # a bar typed as a step stays one, and declared totals are never second-guessed
    typed = _waterfall(None, categories=["a", "b", "c", "Total"],
                       series=[{"name": "v", "values": values, "types": [None, None, None, "step"]}])
    assert _plan(typed)["totals"] == [0]
    assert _plan(_waterfall(values, categories=["a", "b", "c", "Total"], totals=[0]))["totals"] == [0]


def test_last_bar_is_a_total_when_a_step_would_leave_the_authored_axis():
    """Render check #5c, d1_s06: 83 + 214 = 297 runs off `max: 240`; standing on the axis, 214 fits."""
    anonymous = dict(D1_S06, categories=[f"c{i}" for i in range(7)])
    plan = _plan(anonymous)
    assert plan["totals"] == [0, 6]
    assert plan["tops"][6] == 214.0 and plan["bottoms"][6] == 0.0
    assert _plan(dict(anonymous, options={}))["totals"] == [0], "no authored max: nothing to leave"
    roomy = dict(anonymous, options={"valueAxis": {"min": 0, "max": 400}})
    assert _plan(roomy)["totals"] == [0], "the step fits the axis, so it stays a step"
    tiny = dict(anonymous, options={"valueAxis": {"min": 0, "max": 200}})
    assert _plan(tiny)["totals"] == [0], "neither reading fits: no evidence either way"
    assert _plan(D1_S06)["totals"] == [0, 6]


def test_percent_axis_is_stated_in_percent():
    """Render check #3: a 100 % chart's axis is 0–100 in the spec and the model (the emitter
    divides by 100); a value of at most 1 is already a fraction, read with an advisory."""
    spec = {"type": "bar_stacked_100", "categories": ["Mix"],
            "series": [{"name": "a", "values": [27.6]}, {"name": "b", "values": [72.4]}],
            "options": {"valueAxis": {"min": 0, "max": 100, "majorUnit": 25}}}
    model = cm.normalise(spec)
    assert model["notes"] == []
    assert (model["options"]["valueAxis"]["min"], model["options"]["valueAxis"]["max"]) == (0.0, 100.0)
    assert model["axis"] == {"min": 0.0, "max": 100.0, "majorUnit": 25.0}
    fractions = cm.normalise(dict(spec, options={"valueAxis": {"min": 0, "max": 1, "majorUnit": 0.25,
                                                                "minorUnit": 0.05}}))
    axis = fractions["options"]["valueAxis"]
    assert (axis["max"], axis["majorUnit"], axis["minorUnit"]) == (100.0, 25.0, 5.0)
    assert len(_kinds(fractions, "advisory")) == 3 and "read as a fraction" in _kinds(fractions, "advisory")[0]
    assert charts_spec.validate(dict(spec, options={"valueAxis": {"max": 1}})) == []
    # the ticks print as PowerPoint prints the file's 0–1 axis
    assert [cm.tick_text(model, t) for t in cm.axis_ticks(model["axis"])] == ["0%", "25%", "50%", "75%", "100%"]
    labelled = cm.normalise(dict(spec, options={"numberFormat": '0.0"%"', "valueAxis": {"format": "0.0%"}}))
    assert cm.tick_text(labelled, 50.0) == "50.0%"
    assert cm.value_axis_format(cm.normalise(dict(spec, options={"numberFormat": '0.0"%"'}))) == "0%"
    # a mixed spelling that crosses once read: min 0.5 (50 %) above max 40
    crossed = dict(spec, options={"valueAxis": {"min": 0.5, "max": 40}})
    assert cm.normalise(crossed)["options"]["valueAxis"]["min"] is None
    assert charts_spec.validate(crossed)
    # other charts keep their units and their tick format
    column = cm.normalise({"type": "column_stacked", "categories": ["a"], "series": [{"values": [1]}],
                           "options": {"numberFormat": "0.0", "valueAxis": {"max": 1}}})
    assert column["options"]["valueAxis"]["max"] == 1.0 and cm.tick_text(column, 0.5) == "0.5"


def test_series_label_texts_replace_the_formatted_value():
    """Render check #6: `series[k].dataLabels` — a text per point, `null` keeps the value."""
    spec = {"type": "bar_stacked_100", "categories": ["Mix", "Other"],
            "series": [{"name": "Equity", "values": [31.4, 30], "dataLabels": ["$48.2B (31.4%)", None]},
                       {"name": "Cash", "values": [8, 70]}]}
    model = cm.normalise(spec)
    assert model["notes"] == [] and charts_spec.validate(spec) == [] and charts_spec.advisories(spec) == []
    assert model["series"][0]["labels"] == ["$48.2B (31.4%)", None]
    assert model["series"][1]["labels"] is None
    lenient = cm.normalise({"type": "column", "categories": ["a", "b", "c"],
                            "series": [{"values": [1, 2, 3], "dataLabels": [12, ""]}]})
    assert lenient["series"][0]["labels"] == ["12", "", None]
    assert [n["kind"] for n in lenient["notes"]] == ["advisory", "advisory"]
    long = cm.normalise({"type": "pie", "categories": ["a"], "series": [{"values": [1], "dataLabels": ["x", "y"]}]})
    assert long["series"][0]["labels"] == ["x"] and "extra texts are dropped" in long["notes"][0]["text"]
    for bad in ("text", {"0": "x"}, [True], [["x"]]):
        broken = {"type": "column", "categories": ["a"], "series": [{"values": [1], "dataLabels": bad}]}
        assert charts_spec.validate(broken), bad
        assert cm.normalise(broken)["series"][0]["labels"] is None
    xy = cm.normalise({"type": "scatter", "series": [{"x": [1, 2], "y": [3, 4], "dataLabels": ["A", "B"]}]})
    assert xy["series"][0]["labels"] == ["A", "B"]


def test_waterfall_label_texts_are_frozen_on_their_bars():
    spec = dict(D2_S08, series=[dict(D2_S08["series"][0], dataLabels=["$11.6B", None, None, None, None,
                                                                         None, "", "$21.6B"])])
    plan = _plan(spec)
    texts = {segment["category"]: segment["label"]["text"] for segment in plan["segments"]}
    assert texts[0] == "$11.6B" and texts[7] == "$21.6B" and texts[6] == "" and texts[1] is None
    assert plan["texts"] == ["$11.6B", None, None, None, None, None, "", "$21.6B"]
    stacked = _waterfall(None, series=[{"name": "a", "values": [10, 5, 15], "dataLabels": ["x", "y", "z"]},
                                       {"name": "b", "values": [1, 1, 2]}], labelPosition="outEnd")
    model = cm.normalise(stacked)
    assert model["waterfall"]["texts"] == [None, None, None]
    assert any("per-point texts are not used" in t for t in _kinds(model, "advisory"))


# ------------------------------------------ G24: middle subtotals, label styles, line breaks (§8–§9)

#: d1_s03 (render check): "2025 EBITDA" in the middle is the bridge so far — 7.0.
D1_S03 = {"type": "waterfall",
          "categories": ["2020 EBITDA", "Volume Pressure", "Input Cost Inflation", "Overhead Drift",
                         "Pricing Leakage", "2025 EBITDA", "Uplift Potential", "Pro-forma EBITDA"],
          "series": [{"name": "EBITDA ($B)", "values": [12.6, -0.8, -2.1, -1.5, -1.2, 7.0, 8.4, 15.4]}],
          "options": {"numberFormat": "0.0", "valueAxis": {"min": 0, "max": 20, "majorUnit": 4}}}


def test_a_middle_bar_is_a_subtotal_when_it_equals_the_sum_and_says_so():
    """A middle bar equal to the running sum is a total when its category names a subtotal — with
    nothing declared it used to float as a step (7.0 → 14.0) and every later bar ran off the axis."""
    plan = _plan(D1_S03)
    assert plan["totals"] == [0, 5, 7]
    assert plan["roles"][5] == "total" and (plan["bottoms"][5], plan["tops"][5]) == (0.0, 7.0)
    assert plan["levels"][6] == pytest.approx(15.4), "the uplift climbs from the subtotal, the end equals it"
    for name in ("2025 EBITDA", "Subtotal", "Sub-total", "Gross profit", "Operating income", "Base revenue",
                 "EBIT", "Net sales", "Opening value", "Starting point", "Total"):
        categories = list(D1_S03["categories"])
        categories[5] = name
        assert _plan(dict(D1_S03, categories=categories))["totals"][:2] == [0, 5], name
    for name in ("FY24", "Uplift", "Prerevenue", "Subtotally", "Networking"):
        categories = list(D1_S03["categories"])
        categories[5] = name
        assert 5 not in _plan(dict(D1_S03, categories=categories))["totals"], name


def test_a_middle_bar_needs_both_the_sum_and_a_reason():
    """Equal to the running sum alone is not enough (a step may happen to double the bridge), and a
    subtotal's name alone is not enough ("Revenue synergies" is a step) — but leaving the authored
    axis counts as the reason: the author sized the axis for the bar standing on it."""
    anonymous = dict(D1_S03, categories=[f"c{i}" for i in range(8)])
    assert 5 not in _plan(anonymous)["totals"], "no name, the step fits max 20: a step"
    assert 5 not in _plan(dict(anonymous, options={"numberFormat": "0.0"}))["totals"], "no axis: a step"
    squeezed = dict(anonymous, options={"numberFormat": "0.0", "valueAxis": {"min": 0, "max": 13}})
    assert _plan(squeezed)["totals"] == [0, 5, 7], "7.0 + 7.0 = 14.0 leaves max 13; 7.0 standing fits"
    named_step = _waterfall([10, 2, 3, 4], categories=["Base", "Revenue synergies", "Cost", "Total"])
    assert _plan(named_step)["totals"] == [0, 3], "a name without the sum is a step"
    below = _waterfall([-40, -10, -50, 5, -45], valueAxis={"min": -60, "max": 0})
    assert _plan(below)["totals"] == [0, 2, 4], "-50 as a step ends at -100, below min -60"


def test_the_subtotal_tolerance_reads_the_label_format():
    """Within 0.5 % of the running sum or half a unit of the labels' last digit: a bridge whose
    parts are rounded for display still finds its subtotal; a real difference does not."""
    rounded = _waterfall([10.04, 1.5, 0.53, 12.1, 3, 15.1],
                         categories=["Base", "Price", "Volume", "Subtotal", "Mix", "Total"], numberFormat="0.0")
    assert _plan(rounded)["totals"] == [0, 3, 5], "12.1 against 12.07: under 0.05 in a 0.0 format"
    off = _waterfall([10, 1.5, 0.5, 12.3, 3, 15.3], categories=["Base", "a", "b", "Subtotal", "c", "Total"],
                     numberFormat="0.0")
    assert _plan(off)["totals"] == [0, 5], "12.3 against 12: 0.3 is a real difference"
    assert cm.subtotal_tolerance(100.0, 0.05) == 0.5 and cm.subtotal_tolerance(1.0, 0.05) == 0.05
    assert cm.subtotal_tolerance(0.0, None) == 1e-9
    general = _waterfall([5.25, 1.5, -0.75, 6.004, -1, 5.004],
                         categories=["Opening", "In", "Out", "Gross profit", "Tax", "Net"])
    assert _plan(general)["totals"] == [0, 3, 5], "General reads as 0.0#; 0.5 % of 6 is 0.03"


@pytest.mark.parametrize("fmt, unit", [
    ("0", 1.0), ("0.0", 0.1), ("0.0#", 0.01), ("#,##0", 1.0), ("#,##0,", 1000.0), ("0.0,,", 100000.0),
    ("0%", 0.01), ("0.0%", 0.001), ('"$"0.0"B"', 0.1), ("0.0;(0.0)", 0.1), ('0.0"%"', 0.1),
    ("General", None), ('"n/a"', None), ("", None), (None, None)])
def test_format_unit(fmt, unit):
    assert cm.format_unit(fmt) == (pytest.approx(unit) if unit is not None else None)


def test_declared_totals_and_base_value_switch_middle_inference_off():
    assert _plan(dict(D1_S03, options=dict(D1_S03["options"], totals=[0, 7])))["totals"] == [0, 7]
    assert _plan(dict(D1_S03, options=dict(D1_S03["options"], totals=[0])))["totals"] == [0], \
        "[0]: only the first bar stands on the axis"
    assert _plan(dict(D1_S03, options=dict(D1_S03["options"], totals=[])))["totals"] == []
    assert _plan(dict(D1_S03, options=dict(D1_S03["options"], baseValue=0)))["totals"] == []
    typed = dict(D1_S03, series=[dict(D1_S03["series"][0], types=[None] * 5 + ["step", None, None])])
    assert _plan(typed)["totals"] == [0, 7], "a bar typed as a step is never a subtotal"


def test_existing_bridges_keep_their_totals():
    """The rule only adds totals where a middle bar equals the bridge and says so: the render
    check's d2_s08 / d1_s06 and p05's declared bridges read as before, and p05 undeclared now
    finds its "Subtotal" (it floated as a step, and "Combined" was only a total by its name)."""
    assert _plan(D2_S08)["totals"] == [0, 7] and _plan(D1_S06)["totals"] == [0, 6]
    assert _plan(_waterfall(P05_VALUES, categories=P05_CATEGORIES, numberFormat="0.0"))["totals"] == [0, 3, 6]


def test_label_style_and_axis_bold_are_read_with_notes():
    spec = {"type": "column", "categories": ["a"], "series": [{"values": [1]}],
            "options": {"labelStyle": {"bold": True, "color": "#c00", "fontSize": 13},
                        "valueAxis": {"bold": True}}}
    model = cm.normalise(spec)
    assert model["options"]["labelStyle"] == {"bold": True, "color": "CC0000", "fontSize": 13.0}
    assert model["options"]["valueAxis"]["bold"] is True and model["options"]["categoryAxis"]["bold"] is False
    assert _kinds(model, "advisory") == ['options.labelStyle.color: "#c00" read as #CC0000']
    assert charts_spec.validate(spec) == []
    plain = cm.normalise({"type": "column", "categories": ["a"], "series": [{"values": [1]}]})
    assert plain["options"]["labelStyle"] == {"bold": False, "color": None, "fontSize": None}
    bad = dict(spec, options={"labelStyle": {"bold": 1, "color": "red", "fontSize": 5000, "italic": True},
                              "categoryAxis": {"bold": "yes"}})
    model = cm.normalise(bad)
    assert model["options"]["labelStyle"] == {"bold": False, "color": None, "fontSize": None}
    assert len(_kinds(model, "rejected")) == 5
    problems = charts_spec.validate(bad)
    for wanted in ("unknown labelStyle key 'italic'", "labelStyle.bold must be true or false",
                   "labelStyle.color must be #RRGGBB", "labelStyle.fontSize must be between",
                   "categoryAxis.bold must be true or false"):
        assert any(wanted in p for p in problems), (wanted, problems)
    assert charts_spec.validate(dict(spec, options={"labelStyle": "bold"})) == \
        ["labelStyle must be an object {bold, color, fontSize}"]
    assert cm.label_size(model, 11.0) == 11.0
    assert cm.label_size(cm.normalise(dict(spec, options={"labelStyle": {"fontSize": 60}})), 11.0) == 48.0


def test_line_breaks_grow_the_gutters_that_hold_them():
    """A `\\n` breaks a category or label onto another line; the default plot rect leaves
    `LINE_PITCH_EM` for each extra line (and a text is as wide as its widest line)."""
    one = {"type": "column", "categories": ["FY23 Actual", "FY24"], "series": [{"values": [1, 2]}],
           "options": {"labelPosition": "outEnd"}}
    two = dict(one, categories=["FY23\nActual", "FY24"])
    size = 11.0
    g1, g2 = (cm.layout(cm.normalise(s), 560, 250, size)["gutters"] for s in (one, two))
    assert g2["bottom"] - g1["bottom"] == pytest.approx(size * cm.LINE_PITCH_EM)
    labelled = dict(one, series=[{"values": [1, 2], "dataLabels": ["$1B\n(33%)", None]}])
    g3 = cm.layout(cm.normalise(labelled), 560, 250, size)["gutters"]
    assert g3["top"] - g1["top"] == pytest.approx(size * cm.LINE_PITCH_EM)
    bigger = dict(one, options=dict(one["options"], labelStyle={"fontSize": 22}))
    g4 = cm.layout(cm.normalise(bigger), 560, 250, size)["gutters"]
    assert g4["top"] - g1["top"] == pytest.approx((22 - size) * 1.25)
    assert cm._estimate("ab\nabcd", 10.0) == cm._estimate("abcd", 10.0)
    assert cm.text_lines("a\r\nb\nc") == ["a", "b", "c"]
    outend = _waterfall([100, 30, -10, 120], labelPosition="outEnd")
    broken = dict(outend, series=[dict(outend["series"][0], dataLabels=["$100m\n(base)", None, None, None])])
    top = [cm.layout(cm.normalise(s), 560, 250, size)["gutters"]["top"] for s in (outend, broken)]
    assert top[1] - top[0] == pytest.approx(size * cm.LINE_PITCH_EM), "a pinned bridge leaves room for it"
