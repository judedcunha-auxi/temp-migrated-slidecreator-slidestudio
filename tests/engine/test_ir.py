"""The IR round-trips, and rejects every invalid case `02-IR-SCHEMA.md` names.

The negative tests matter more than the positive one: the IR is the contract five packages build
against, and a validator that misses a violation lets a malformed IR reach the emitter, where the
symptom is a corrupt `.pptx` rather than a clear message.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine.ir import (
    IR,
    Box,
    Canvas,
    Diagnostic,
    Element,
    Group,
    IRValidationError,
    Slide,
    SvgExpansion,
    SvgPlaceholder,
    assign_ids_and_z,
)


def _text_element(**overrides) -> Element:
    element = Element(
        kind="text",
        box=Box(64, 28, 400, 60),
        paragraphs=[
            {
                "align": "left",
                "lineHeightPx": 24.0,
                "spaceBeforePx": 0,
                "spaceAfterPx": 8,
                "bullet": None,
                "lines": [
                    {
                        "box": {"x": 64, "y": 28, "w": 380, "h": 24},
                        "runs": [
                            {
                                "text": "Present faster.",
                                "font": "Arial", "sizePx": 29.33, "weight": 700, "italic": False,
                                "color": "2E2E38", "alpha": 1,
                                "underline": False, "strike": False, "letterSpacingPx": 0,
                                "baseline": "normal", "baselineShiftPx": 0, "caps": False, "href": None,
                            }
                        ],
                    }
                ],
            }
        ],
        anchor="top",
        wrap=True,
        source={"path": "body > h1", "tag": "h1", "svg": None},
    )
    for key, value in overrides.items():
        setattr(element, key, value)
    return element


def _shape_element(**overrides) -> Element:
    element = Element(
        kind="shape",
        box=Box(0, 0, 100, 50),
        geometry={"type": "roundRect", "radius": {"tl": 8, "tr": 8, "br": 8, "bl": 8}},
        fill={"type": "solid", "color": "1A9AFA", "alpha": 1},
        stroke={"color": "2E2E38", "alpha": 1, "width": 1.5, "dash": "solid",
                "cap": "butt", "join": "miter", "headEnd": None, "tailEnd": None},
        source={"path": "body > div.card", "tag": "div", "svg": None},
    )
    for key, value in overrides.items():
        setattr(element, key, value)
    return element


def _ir(*elements: Element, groups: list[Group] | None = None) -> IR:
    ir = IR(
        canvas=Canvas(1280, 720),
        slide=Slide(id="sld_test", title="Test", layoutId="layout-01"),
        elements=assign_ids_and_z(list(elements)),
        groups=groups or [],
    )
    return ir


# --------------------------------------------------------------------------------- happy path


def test_hand_written_ir_validates_and_round_trips(tmp_path: Path):
    ir = _ir(
        _shape_element(),
        _text_element(),
        Element(
            kind="chart",
            box=Box(96, 330, 560, 250),
            spec={
                "type": "column_stacked",
                "categories": ["2019", "2021", ""],
                "series": [{"name": "Internal", "values": [48, 42, None]}],
                "colors": ["#B9C7C9"],
                "options": {"dataLabels": True, "legend": False, "plotArea": {"x": .08, "y": .05, "w": .9, "h": .8}},
            },
            style={"font": "Arial", "sizePx": 11, "color": "516467"},
            origin="authored",
            confidence=1.0,
            plotRect=Box(100, 340, 500, 200),
            overlay=[],
        ),
    )
    assert ir.validate(check_files=False) == []

    restored = IR.from_json(json.loads(json.dumps(ir.to_json())))
    assert restored.to_json() == ir.to_json()
    assert restored.dumps() == ir.dumps()

    path = ir.save(tmp_path / "ir.json")
    assert IR.load(path).to_json() == ir.to_json()


def test_to_json_rounds_floats_so_two_runs_agree():
    ir = _ir(_shape_element(box=Box(0.1 + 0.2, 1 / 3, 10.00049, 5.0)))
    box = ir.to_json()["elements"][0]["box"]
    assert box == {"x": 0.3, "y": 0.333, "w": 10.0, "h": 5.0}


def test_kind_specific_fields_are_not_written_for_other_kinds():
    data = _ir(_shape_element()).to_json()["elements"][0]
    assert "paragraphs" not in data and "spec" not in data
    assert data["geometry"]["type"] == "roundRect"


def test_assign_ids_and_z_numbers_in_paint_order():
    elements = [_shape_element(), _text_element(), _shape_element()]
    assign_ids_and_z(elements)
    assert [e.id for e in elements] == ["e1", "e2", "e3"]
    assert [e.z for e in elements] == [0, 1, 2]


def test_paint_sorted_orders_by_z_and_tolerates_unnumbered():
    first, second = _shape_element(), _text_element()
    assign_ids_and_z([first, second])
    unnumbered = _shape_element()
    ir = IR(canvas=Canvas(1280, 720), slide=Slide(id="s"), elements=[second, unnumbered, first])
    assert [e.id for e in ir.paint_sorted()] == ["e1", "e2", None]


def test_counts_and_queries():
    ir = _ir(_shape_element(), _text_element(), _shape_element())
    assert ir.counts() == {"shape": 2, "text": 1}
    assert ir.by_id("e2").kind == "text"
    assert len(ir.of_kind("shape")) == 2


def test_box_geometry_helpers():
    box = Box(10, 20, 100, 50)
    assert (box.x2, box.y2, box.cx, box.cy, box.area) == (110, 70, 60, 45, 5000)
    assert box.intersect(Box(60, 20, 100, 50)) == Box(60, 20, 50, 50)
    assert box.intersect(Box(500, 500, 10, 10)).area == 0
    assert box.overlap_fraction(Box(10, 20, 50, 50)) == 0.5
    assert box.union(Box(0, 0, 10, 10)) == Box(0, 0, 110, 70)


# ---------------------------------------------------------------------------- rejection cases


def test_rejects_duplicate_ids():
    ir = _ir(_shape_element(), _text_element())
    ir.elements[1].id = ir.elements[0].id
    assert any("duplicate id" in p for p in ir.validate(check_files=False))


def test_rejects_non_increasing_z():
    ir = _ir(_shape_element(), _text_element())
    ir.elements[1].z = 0
    assert any("z must strictly increase" in p for p in ir.validate(check_files=False))


def test_rejects_missing_id_or_z():
    ir = _ir(_shape_element())
    ir.elements[0].id = None
    ir.elements[0].z = None
    problems = ir.validate(check_files=False)
    assert any("id is required" in p for p in problems)
    assert any("z is required" in p for p in problems)


def test_rejects_non_finite_and_negative_boxes():
    assert any("non-finite" in p for p in _ir(_shape_element(box=Box(float("nan"), 0, 1, 1))).validate(check_files=False))
    assert any("negative extent" in p for p in _ir(_shape_element(box=Box(0, 0, -1, 1))).validate(check_files=False))


def test_rejects_unknown_group_and_cyclic_groups():
    ir = _ir(_shape_element(group="g-missing"))
    assert any("does not exist" in p for p in ir.validate(check_files=False))

    cyclic = _ir(_shape_element(), groups=[Group(id="g1", parent="g2"), Group(id="g2", parent="g1")])
    assert any("cycle" in p for p in cyclic.validate(check_files=False))


def test_rejects_bad_custom_paths():
    bad_start = _ir(_shape_element(geometry={"type": "custom", "path": [["L", 1, 2]]}))
    assert any("must start with M" in p for p in bad_start.validate(check_files=False))

    bad_command = _ir(_shape_element(geometry={"type": "custom", "path": [["M", 0, 0], ["A", 1, 2, 3]]}))
    assert any("only M L C Q Z" in p for p in bad_command.validate(check_files=False))

    bad_arity = _ir(_shape_element(geometry={"type": "custom", "path": [["M", 0, 0], ["C", 1, 2]]}))
    assert any("takes 6 numbers" in p for p in bad_arity.validate(check_files=False))


def test_rejects_bad_colours_and_alphas():
    assert any("6 hex digits" in p for p in _ir(
        _shape_element(fill={"type": "solid", "color": "#1A9AFA"})
    ).validate(check_files=False))
    assert any("alpha out of range" in p for p in _ir(
        _shape_element(fill={"type": "solid", "color": "1A9AFA", "alpha": 4})
    ).validate(check_files=False))
    assert any("opacity out of range" in p for p in _ir(_shape_element(opacity=2.0)).validate(check_files=False))


def test_rejects_empty_text_structures():
    assert any("non-empty paragraphs" in p for p in _ir(_text_element(paragraphs=[])).validate(check_files=False))

    no_runs = _text_element()
    no_runs.paragraphs[0]["lines"][0]["runs"] = []
    assert any("non-empty runs" in p for p in _ir(no_runs).validate(check_files=False))


def test_rejects_chart_with_an_unsupported_type():
    ir = _ir(Element(
        kind="chart", box=Box(0, 0, 100, 100), origin="authored",
        spec={"type": "bubble", "categories": ["a"], "series": [{"name": "s", "values": [1]}]},
    ))
    assert any("not a chart object on Path A" in p for p in ir.validate(check_files=False))


def test_rejects_raster_without_reason_or_diagnostic(tmp_path: Path):
    png = tmp_path / "e1.png"
    png.write_bytes(b"not really a png, but it exists")

    ir = _ir(Element(kind="raster", box=Box(0, 0, 10, 10), src=str(png), reason="svg filter"))
    assert any("no matching diagnostic" in p for p in ir.validate())

    ir.diagnostics.append(Diagnostic("warn", "svg#1", "filter rasterised", "e1"))
    assert ir.validate() == []

    ir.elements[0].reason = None
    assert any("needs a reason" in p for p in ir.validate())


def test_rejects_missing_image_file_only_when_checking_files():
    ir = _ir(Element(kind="image", box=Box(0, 0, 10, 10), src="C:/nope/missing.png", fit="cover"))
    assert any("does not exist" in p for p in ir.validate())
    assert ir.validate(check_files=False) == []


def test_rejects_clip_outside_the_canvas():
    ir = _ir(_shape_element(clip=Box(0, 0, 5000, 5000)))
    assert any("outside the canvas" in p for p in ir.validate(check_files=False))


def test_rejects_bad_table_geometry():
    ir = _ir(Element(
        kind="table", box=Box(0, 0, 100, 100),
        rows=2, cols=2, colWidthsPx=[50.0], rowHeightsPx=[50.0, 50.0],
        cells=[{"r": 0, "c": 0}, {"r": 5, "c": 0}],
    ))
    problems = ir.validate(check_files=False)
    assert any("colWidthsPx must hold 2" in p for p in problems)
    assert any("outside the 2×2 grid" in p for p in problems)


def test_strict_validation_raises():
    with pytest.raises(IRValidationError):
        _ir(_shape_element(opacity=9)).validate(check_files=False, strict=True)


def test_from_json_rejects_a_future_version():
    data = _ir(_shape_element()).to_json()
    data["version"] = 99
    with pytest.raises(IRValidationError):
        IR.from_json(data)


def test_extras_survive_a_round_trip_untouched():
    ir = _ir(_shape_element(extras={"x-wp2-ctm": [1, 0, 0, 1, 0, 0]}))
    assert ir.validate(check_files=False) == []
    assert IR.from_json(ir.to_json()).elements[0].extras == {"x-wp2-ctm": [1, 0, 0, 1, 0, 0]}


# ------------------------------------------------------------------- internal (unserialised) types


def test_svg_placeholder_and_expansion_are_not_part_of_the_schema():
    placeholder = SvgPlaceholder(svg_id="svg#3", box=Box(0, 0, 10, 10))
    assert placeholder.svg_id == "svg#3"

    expansion = SvgExpansion(elements=[_shape_element()], diagnostics=[Diagnostic("info", "svg#3", "ok")])
    assert expansion.elements[0].id is None and expansion.elements[0].z is None
    # Splicing is WP1's job; ids only exist once the whole list is renumbered.
    assign_ids_and_z(expansion.elements)
    assert expansion.elements[0].id == "e1"
