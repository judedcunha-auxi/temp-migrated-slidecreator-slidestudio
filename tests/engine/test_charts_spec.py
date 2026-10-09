"""The chart spec skeleton: which types Path A emits, and which specs it refuses.

WP3a extends `validate` as it implements options. These tests pin the decisions that are **not**
WP3a's to revisit: the unsupported types, the round-2 deferral, and the fact that the type list is
read from the emitter rather than transcribed beside it.
"""
from __future__ import annotations

from app.engine import charts_spec
from app.engine.emit import charts as stageflow


def _column(**overrides) -> dict:
    spec = {
        "type": "column_stacked",
        "categories": ["FY20", "FY21", "FY22", "FY23", "", "FY27E"],
        "series": [
            {"name": "Internal", "values": [40, 52, 57, 71, None, 88]},
            {"name": "Partner", "values": [12, 9, 21, 18, None, 55]},
        ],
        "colors": ["#B9C7C9", "#1A9AFB"],
        "options": {"dataLabels": True, "labelPosition": "outEnd", "numberFormat": "0",
                    "gridlines": False, "legend": False, "gapWidth": 60,
                    "valueAxis": {"min": 0, "max": 160, "majorUnit": 40, "visible": True},
                    "plotArea": {"x": 0.08, "y": 0.05, "w": 0.9, "h": 0.8},
                    "referenceLines": [{"value": 100, "color": "#516467", "dash": [4, 3],
                                        "label": "Original 90k goal"}],
                    "pointColors": {"1": {"5": "#7FC6FF"}}},
    }
    spec.update(overrides)
    return spec


def test_the_contracts_own_example_validates():
    """The `data-chart` block in 03-AUTHORING-CONTRACT.md must pass, or the docs lie to the author."""
    assert charts_spec.validate(_column()) == []


def test_type_list_comes_from_the_emitter_not_a_transcription():
    """Every type StageFlow can draw is known here, minus the ones Path A rules out."""
    supported = set(stageflow.TYPES) | set(getattr(stageflow, "DERIVED", {}))
    assert supported - charts_spec.PATH_A_UNSUPPORTED <= charts_spec.TYPES
    assert charts_spec.TYPES - charts_spec.ENGINE_TYPES <= supported
    assert "column_3d_stacked" in charts_spec.TYPES, "StageFlow's own spelling must be accepted"


def test_unsupported_types_are_refused_with_the_reason():
    for kind in ("bubble", "bubble_3d", "radar", "radar_filled", "stock_hlc", "surface"):
        problems = charts_spec.validate({"type": kind, "categories": ["a"],
                                         "series": [{"name": "s", "values": [1]}]})
        assert problems and "not a chart object on Path A" in problems[0], kind
    assert not (charts_spec.TYPES & charts_spec.PATH_A_UNSUPPORTED)


def test_combo_is_deferred_to_round_two():
    problems = charts_spec.validate({"type": "combo", "categories": ["a"],
                                     "series": [{"name": "s", "values": [1]}]})
    assert problems and "round 2" in problems[0]

    # Also as a per-series flag on an otherwise fine chart.
    spec = _column()
    spec["series"][0]["chartType"] = "line"
    assert any("round 2" in p for p in charts_spec.validate(spec))


def test_waterfall_is_a_path_a_type():
    assert "waterfall" in charts_spec.TYPES
    assert charts_spec.validate({"type": "waterfall", "categories": ["a", "b"],
                                 "series": [{"name": "s", "values": [250, 150]}]}) == []


def test_gap_slots_are_valid():
    """An empty category with null values keeps bar positions where a design leaves a gap."""
    assert charts_spec.validate(_column()) == []


def test_series_and_category_shape_is_checked():
    assert any("same length" in p for p in charts_spec.validate(
        {"type": "scatter", "series": [{"name": "s", "x": [1, 2], "y": [1]}]}))
    assert any("entries but there are" in p for p in charts_spec.validate(
        _column(series=[{"name": "s", "values": [1]}])))
    assert any("non-empty series" in p for p in charts_spec.validate(
        {"type": "column", "categories": ["a"], "series": []}))
    assert any("needs a type" in p for p in charts_spec.validate({}))
    assert any("unknown chart type" in p for p in charts_spec.validate(
        {"type": "sunburst", "categories": ["a"], "series": [{"values": [1]}]}))


def test_xy_charts_do_not_need_categories():
    assert charts_spec.validate(
        {"type": "scatter", "series": [{"name": "s", "x": [1, 2], "y": [3, 4]}]}) == []


def test_option_validation_covers_the_contracts_options():
    assert any("unknown option" in p for p in charts_spec.validate(
        _column(options={"notAnOption": 1})))
    assert any("labelPosition" in p for p in charts_spec.validate(
        _column(options={"labelPosition": "sideways"})))
    assert any("legend must be" in p for p in charts_spec.validate(
        _column(options={"legend": "diagonally"})))
    assert any("plotArea" in p for p in charts_spec.validate(
        _column(options={"plotArea": {"x": 0, "y": 0, "w": 2, "h": 1}})))
    assert any(
        "holeSize" in p for p in charts_spec.validate(_column(options={"holeSize": 62}))
    ), "holeSize on a column chart is meaningless"
    assert any("referenceLines" in p for p in charts_spec.validate(
        _column(options={"referenceLines": [{"color": "#000000"}]})))
    assert any("pointColors" in p for p in charts_spec.validate(
        _column(options={"pointColors": {"1": {"5": "not-a-colour"}}})))
    assert any("valueAxis.min" in p for p in charts_spec.validate(
        _column(options={"valueAxis": {"min": "zero"}})))
    assert any("unknown valueAxis key" in p for p in charts_spec.validate(
        _column(options={"valueAxis": {"minimum": 0}})))


def test_colours_accept_both_spellings():
    assert charts_spec.validate(_column(colors=["#B9C7C9", "1A9AFB"])) == []
    assert any("#RRGGBB" in p for p in charts_spec.validate(_column(colors=["blue"])))


def test_pie_types_take_one_series():
    one = {"type": "doughnut", "categories": ["a", "b"], "series": [{"name": "s", "values": [61, 39]}],
           "options": {"holeSize": 62}}
    assert charts_spec.validate(one) == []
    two = dict(one, series=[{"name": "s", "values": [1, 1]}, {"name": "t", "values": [1, 1]}])
    assert any("single series" in p for p in charts_spec.validate(two))



# ======================================================================= WP-C: one model, leniency
#
# `validate` judges the raw spelling plus `chart_model.normalise`; `advisories` returns what the
# model accepted but read one way. The two never say the same thing (docs/archive/engine/fidelity/12-WPC-
# charts.md §4.5; Peter #8: lenient specs are accepted with advisories).

from app.engine import chart_model  # noqa: E402

_P05 = {"type": "waterfall",
        "categories": ["Standalone", "Cost syn.", "Revenue syn.", "Subtotal", "Integration", "Tax", "Combined"],
        "series": [{"name": "Value", "values": [8.1, 2.4, 1.6, 12.1, -0.9, 1.2, 12.4]}],
        "options": {"numberFormat": "0.0"}}


def _p05(**options):
    return dict(_P05, options=dict(_P05["options"], **options))


def _p05_series(**keys):
    return dict(_P05, series=[dict(_P05["series"][0], **keys)])


def _column(**extra):
    spec = {"type": "column", "categories": ["a", "b"], "series": [{"values": [1, 2]}]}
    spec.update(extra)
    return spec


#: Every spelling WP-C §2–§3 accepts, each with the advisory it earns (None: no advisory at all).
SPELLINGS = [
    ("totals as indices", _p05(totals=[0, 3, 6]), None),
    ("totals as flags", _p05(totals=[True, False, False, True, False, False, True]), None),
    ("totals as types", _p05_series(types=["total", "up", "gain", "subtotal", "down", "rise", "end"]), None),
    ("series.totals", _p05_series(totals=[0, 3, 6]), None),
    ("empty totals", _p05(totals=[]), None),
    ("totalIndices", _p05(totalIndices=[0, 3, 6]), "old spelling of options.totals"),
    ("two spellings", _p05(totals=[0], totalIndices=[6]), "say it once"),
    ("0/1 as indices", _p05(totals=[1, 0, 0, 1, 0, 0, 1]), "reads as category indices"),
    ("baseValue", _p05(baseValue=100), None),
    ("baseValue as a string", _p05(baseValue="100"), "read as 100"),
    ("connectorColor", _p05(connectorColor="#8A9699"), "connectors.color"),
    ("numeric strings", _column(series=[{"values": ["4.2", " 6.2 "]}]), "read as 4.2"),
    ("alias", dict(_column(), type="column_clustered"), "read as 'column'"),
    ("#RGB", _column(colors=["#ABC"]), "read as #AABBCC"),
    ("#RRGGBBAA", _column(colors=["#1A9AFB80"]), "alpha is dropped"),
    ("axis bool", _column(options={"valueAxis": False}), "{visible: false}"),
    ("list pointColors", _column(options={"pointColors": ["#C4C4CD", "#1A9AFA"]}), "pointColors is a list"),
    ("flat pointColors", _column(options={"pointColors": {"1": "#1A9AFA"}}), "maps points to colours"),
    ("snake_case position", _column(options={"labelPosition": "inside_end"}), 'read as "inEnd"'),
    ("stacked waterfall", {"type": "waterfall", "categories": ["FY23", "Q1", "FY24"],
                           "series": [{"name": "D", "values": [30, -25, -10]}, {"name": "I", "values": [0, -15, 0]}],
                           "options": {"totals": [0, 2]}}, None),
    ("direction word overrides", _p05_series(values=[8.1, 2.4, 1.6, 12.1, 0.9, 1.2, 12.4],
                                             types=["total", None, None, "total", "decrease", None, "total"]),
     "read as -0.9"),
]


def test_every_spelling_validates():
    for label, spec, _advice in SPELLINGS:
        assert charts_spec.validate(spec) == [], (label, charts_spec.validate(spec))


def test_advisories_name_each_coercion_and_never_duplicate_validate():
    for label, spec, advice in SPELLINGS:
        said = charts_spec.advisories(spec)
        if advice is None:
            assert said == [], (label, said)
        else:
            assert any(advice in text for text in said), (label, said)
    # a spec with errors and coercions: the two lists never share a message
    messy = {"type": "column_clustered", "categories": ["a", "b"],
             "series": [{"values": ["4.2", "4,200"]}], "colors": ["navy", "#ABC"],
             "options": {"fontSize": "11px", "gapWidth": "60", "gapWith": 60}}
    problems, advice = charts_spec.validate(messy), charts_spec.advisories(messy)
    assert problems and advice
    assert not set(problems) & set(advice)
    assert not any(word in text for text in advice for word in ("gapWith", "11px", "navy", "4,200")), (
        "what validate reports is a rejected/warn note, never an advisory")


def test_alias_names_resolve_in_the_validator():
    for written in ("column_clustered", "grouped_column", "stacked_bar", "donut", "bridge", "Stacked Column Chart"):
        spec = {"type": written, "categories": ["a", "b"], "series": [{"name": "s", "values": [1, 2]}]}
        assert charts_spec.validate(spec) == [], written
        assert chart_model.resolve_type_name(written)["name"] is not None, written
    problems = charts_spec.validate({"type": "grouped_bar", "categories": ["a"], "series": [{"values": [1]}]})
    assert problems == ["unknown chart type 'grouped_bar' (did you mean 'bar'?)"]


def test_flat_point_colors_need_a_single_series():
    two = {"type": "column", "categories": ["a", "b"],
           "series": [{"name": "s", "values": [1, 2]}, {"name": "t", "values": [3, 4]}]}
    for flat in (["#C4C4CD", "#1A9AFA"], {"1": "#1A9AFA"}):
        assert any("only works on a single-series chart" in p
                   for p in charts_spec.validate(dict(two, options={"pointColors": flat})))
    assert charts_spec.validate(dict(two, options={"pointColors": {"1": {"0": "#1A9AFA"}}})) == []
    assert any("outside the chart" in p for p in charts_spec.validate(
        dict(two, options={"pointColors": {"2": {"0": "#1A9AFA"}}})))


def test_multi_series_waterfall_validates_unless_signs_mix():
    base = {"type": "waterfall", "categories": ["a", "b", "c"], "options": {"totals": [0]}}
    assert charts_spec.validate(dict(base, series=[{"values": [10, 20, 30]}, {"values": [1, 2, 3]}])) == []
    assert charts_spec.validate(dict(base, series=[{"values": [10, -20, 30]}, {"values": [1, -2, 3]}])) == []
    problems = charts_spec.validate(dict(base, series=[{"values": [10, 20, 30]}, {"values": [1, -2, 3]}]))
    assert problems == ["category 1 ('b') mixes rises and falls across series — all values in one bar must share a sign"]


def test_mixed_totals_list_is_an_error():
    base = {"type": "waterfall", "categories": ["a", "b", "c"], "series": [{"values": [10, 5, 15]}]}
    assert any("mixes category indices" in p
               for p in charts_spec.validate(dict(base, options={"totals": [0, True]})))
    assert any("2 flags but there are 3 categories" in p
               for p in charts_spec.validate(dict(base, options={"totals": [True, False]})))
    assert any("must be a list of category indices" in p
               for p in charts_spec.validate(dict(base, options={"totals": [0.5]})))
