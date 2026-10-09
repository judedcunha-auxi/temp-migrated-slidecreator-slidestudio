"""WP2 — `engine/extract/svg.py`.

Every row of `12-WP2-svg-extractor.md`'s mapping table gets at least one case, and the numeric
rows are asserted against numbers **derived from the fixture source**, not from a previous run:
a chevron's points are the authored `d` times the viewBox scale plus the block's offset, the
donut's radii are `r ± strokeWidth / 2`, the ring's sweep is `dash / circumference × 360`. A test
that only compares against what the code produced last time proves nothing about the mapping.

The acceptance numbers of the brief (a chevron chain: 7 custom shapes, 2 line shapes, 14 texts,
0 rasters; real slides: 0 rasters) are asserted on synthetic stand-ins: `fixtures/svg_cases/
acceptance.html` and the sample project's slide-01.

WP1 has not landed, so these tests tag the `<svg>` roots with `data-engine-svg` themselves, exactly
as WP1 will. Inputs that are missing make a test **fail**, never skip (master brief §10.7).
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from app.engine.extract.svg import (
    DIAG_KEY_PREFIX,
    ELEMENT_KEY,
    retarget_diagnostics,
)
from app.engine.ir import IR, Canvas, Element, Slide, SvgExpansion, assign_ids_and_z
from tests.engine.helpers import expand_svg

CANVAS = Canvas(1280, 720)

#: What WP1 will do to every `<svg>` root before calling `expand_svg`: give it a stable id.
TAG_ROOTS_JS = """
() => {
  const ids = [];
  document.querySelectorAll('svg').forEach((el, index) => {
    const id = 'svg#' + (index + 1);
    el.setAttribute('data-engine-svg', id);
    ids.push(id);
  });
  return ids;
}
"""


# ------------------------------------------------------------------------------------- harness


def _expand(browser, html_path: Path) -> dict[str, SvgExpansion]:
    """Load a slide, tag its `<svg>` roots and expand them all."""
    assert html_path.exists(), f"{html_path} is missing — the fixture is incomplete"
    context = browser.new_context(viewport={"width": CANVAS.w, "height": CANVAS.h}, device_scale_factor=1)
    page = context.new_page()
    try:
        page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
        svg_ids = page.evaluate(TAG_ROOTS_JS)
        assert svg_ids, f"{html_path.name} has no <svg> root to expand"
        return expand_svg(page, svg_ids, CANVAS, html_path.parent / "assets")
    finally:
        page.close()
        context.close()


def _elements(expansions: dict[str, SvgExpansion]) -> list[Element]:
    return [element for expansion in expansions.values() for element in expansion.elements]


def _diagnostics(expansions: dict[str, SvgExpansion]):
    return [d for expansion in expansions.values() for d in expansion.diagnostics]


def _geometries(elements: Iterable[Element], geometry_type: str) -> list[Element]:
    return [e for e in elements if e.kind == "shape" and e.geometry and e.geometry["type"] == geometry_type]


def _texts(elements: Iterable[Element]) -> list[Element]:
    return [e for e in elements if e.kind == "text"]


def _plain_text(element: Element) -> str:
    return "".join(
        run["text"]
        for paragraph in element.paragraphs or []
        for line in paragraph["lines"]
        for run in line["runs"]
    )


def _find_text(elements: Iterable[Element], needle: str) -> Element:
    matches = [e for e in _texts(elements) if needle in _plain_text(e)]
    assert matches, f"no text element contains {needle!r}"
    return matches[0]


def _close(actual: float, expected: float, tolerance: float = 0.05) -> bool:
    return abs(actual - expected) <= tolerance


@pytest.fixture(scope="module")
def shapes(browser, torture_dir: Path) -> dict[str, SvgExpansion]:
    return _expand(browser, torture_dir / "svg-shapes.html")


@pytest.fixture(scope="module")
def paths(browser, torture_dir: Path) -> dict[str, SvgExpansion]:
    return _expand(browser, torture_dir / "svg-paths.html")


@pytest.fixture(scope="module")
def synthetic_svg(browser, sample_dir: Path, fixtures_dir: Path) -> dict[str, dict[str, SvgExpansion]]:
    """The sample slide's SVGs, and the synthetic acceptance page (chevrons, bubbles, donuts)."""
    return {
        "sample-slide-01": _expand(browser, sample_dir / "slide-01.html"),
        "acceptance": _expand(browser, fixtures_dir / "svg_cases" / "acceptance.html"),
    }


# ------------------------------------------------------------------------- mapping table: shapes


def test_rect_becomes_rect_scaled_by_the_viewbox(shapes):
    """`<rect x=10 y=10 w=60 h=30>` in a ×2 viewBox inside a block at (40, 40)."""
    rect = _geometries(shapes["svg#1"].elements, "rect")[0]
    assert (rect.box.x, rect.box.y, rect.box.w, rect.box.h) == (40 + 20, 40 + 20, 120, 60)
    assert rect.fill == {"type": "solid", "color": "1A9AFA", "alpha": 1.0}


def test_rounded_rect_radius_is_scaled_and_unequal_radii_are_reported(shapes):
    rounded = _geometries(shapes["svg#1"].elements, "roundRect")
    assert len(rounded) == 2
    assert rounded[0].geometry["radius"] == {"tl": 12.0, "tr": 12.0, "br": 12.0, "bl": 12.0}
    # rx 10 ≠ ry 4 → the smaller radius (4 × 2) wins, and the difference is a diagnostic.
    assert rounded[1].geometry["radius"]["tl"] == 8.0
    assert any("rx 10.0 ≠ ry 4.0" in d.message for d in shapes["svg#1"].diagnostics)


def test_circle_and_rotated_ellipse(shapes):
    circle, ellipse = _geometries(shapes["svg#1"].elements, "ellipse")
    assert (circle.box.w, circle.box.h) == (80, 80)          # r 20 × 2
    assert (circle.box.x, circle.box.y) == (40 + 20, 40 + 100)
    assert circle.rotation == 0.0
    assert (ellipse.box.w, ellipse.box.h) == (96, 48)        # rx 24, ry 12, × 2
    assert _close(ellipse.rotation, 30.0), ellipse.rotation


def test_line_and_polyline_keep_their_points(shapes):
    lines = _geometries(shapes["svg#1"].elements, "line")
    dashed = lines[0]
    assert dashed.geometry["points"] == [[40 + 300, 40 + 120], [40 + 500, 40 + 120]]
    assert dashed.stroke["dash"] == [12.0, 6.0]              # 6 and 3 user units × 2
    assert dashed.stroke["width"] == 4.0                     # 2 × 2
    polyline = _geometries(shapes["svg#1"].elements, "polyline")[0]
    assert polyline.geometry["points"][0] == [40 + 300, 40 + 160]
    assert polyline.geometry["points"][-1] == [40 + 500, 40 + 180]
    assert polyline.stroke["cap"] == "round" and polyline.stroke["join"] == "round"
    assert polyline.fill == {"type": "none"}


def test_polygon_becomes_a_closed_custom_path(shapes):
    polygon = _geometries(shapes["svg#1"].elements, "custom")[0]
    assert polygon.geometry["path"] == [
        ["M", 40 + 40, 40 + 220], ["L", 40 + 120, 40 + 220], ["L", 40 + 80, 40 + 280], ["Z"],
    ]


def test_stroke_inherited_from_a_g_attribute(shapes):
    """The two gridlines carry no stroke of their own: it comes from the `<g>` above them."""
    gridlines = [e for e in _geometries(shapes["svg#1"].elements, "line") if e.stroke
                 and e.stroke["color"] == "E6E6EC"]
    assert len(gridlines) == 2
    assert all(line.stroke["width"] == 2.0 for line in gridlines)   # 1 user unit × 2
    assert all(line.group == gridlines[0].group and line.group for line in gridlines)


def test_opacity_multiplies_down_the_tree(shapes):
    faded = [e for e in shapes["svg#1"].elements if e.opacity not in (None, 1.0)]
    assert len(faded) == 1
    assert faded[0].opacity == 0.25                          # 0.5 on the g × 0.5 on the rect


def test_label_styled_only_by_a_css_class(shapes):
    label = _find_text(shapes["svg#1"].elements, "CSS class only")
    run = label.paragraphs[0]["lines"][0]["runs"][0]
    # 6 pt resolves to 8 user units, and the viewBox doubles everything: the IR is canvas px.
    assert run["sizePx"] == 16.0
    assert run["weight"] == 700
    assert run["color"] == "516467"


# -------------------------------------------------------------------- mapping table: paint servers


def test_gradient_angles_follow_the_css_convention(shapes):
    rects = _geometries(shapes["svg#2"].elements, "rect")
    box_units, user_units, turned = rects[0], rects[1], rects[2]
    # default objectBoundingBox x1=0 → x2=1: left to right, which the schema calls 90°.
    assert box_units.fill["kind"] == "linear" and box_units.fill["angle"] == 90.0
    assert box_units.fill["stops"] == [
        {"pos": 0.0, "color": "1A9AFA", "alpha": 1.0},
        {"pos": 1.0, "color": "2E2E38", "alpha": 0.6},
    ]
    assert user_units.fill["angle"] == 180.0                 # userSpaceOnUse, top to bottom
    assert turned.fill["angle"] == 180.0                     # horizontal + gradientTransform rotate(90)


def test_radial_gradient_keeps_its_kind(shapes):
    radial = [e for e in _geometries(shapes["svg#2"].elements, "ellipse")
              if e.fill and e.fill.get("kind") == "radial"]
    assert len(radial) == 1
    assert [stop["color"] for stop in radial[0].fill["stops"]] == ["FFFFFF", "1A9AFA"]


def test_markers_become_line_ends(shapes):
    # svg-shapes.html: a 2 px line (viewBox scale 1) with `mDot` (6 x 6 stroke widths = a 12 px dot)
    # and `mTriangle` (8 x 8 = a 16 px head). PowerPoint draws sm / med / lg as 2 / 3 / 5 x
    # max(2 px, LINE_END_FLOOR_PX 2.65) = 5.3 / 7.95 / 13.25 px, so both are `lg` (r1b; `med` before,
    # an 8 px head where the design has 12 and 16)
    line = _geometries(shapes["svg#2"].elements, "line")[0]
    assert line.stroke["headEnd"] == {"type": "oval", "size": "lg"}
    assert line.stroke["tailEnd"] == {"type": "triangle", "size": "lg"}


def test_an_unclassifiable_marker_rasterises_its_line(shapes):
    rasters = [e for e in shapes["svg#2"].elements if e.kind == "raster"]
    assert len(rasters) == 1
    assert "markerEnd marker" in rasters[0].reason
    # a horizontal <line> has a zero-high geometry rect: the capture must still have pixels in it
    assert rasters[0].box.h >= 2 and Path(rasters[0].src).stat().st_size > 0


# ------------------------------------------------------------------ mapping table: use, image, text


def test_use_of_a_symbol_is_expanded_in_place(shapes):
    """`<use href="#badge" x=300 y=65 width=30 height=30>` on a `viewBox="0 0 10 10"` symbol."""
    badge = [e for e in _geometries(shapes["svg#2"].elements, "ellipse")
             if e.fill and e.fill.get("color") == "2DB757"]
    assert len(badge) == 1, "the symbol's circle did not come through the use expansion"
    # circle r=4 at (5,5) in a 10×10 viewBox scaled to 30×30 → 24 px across at (303, 68) + block
    assert (badge[0].box.w, badge[0].box.h) == (24, 24)
    assert (badge[0].box.x, badge[0].box.y) == (40 + 303, 370 + 68)


def test_use_of_a_path_is_expanded_with_its_own_fill(paths):
    chevron = [e for e in paths["svg#2"].elements
               if e.fill and e.fill.get("color") == "2DB757" and e.geometry["type"] == "custom"]
    assert len(chevron) == 1
    assert (chevron[0].box.x, chevron[0].box.y) == (40 + 220, 280 + 120)
    assert (chevron[0].box.w, chevron[0].box.h) == (36, 20)


def test_svg_image_data_uri_is_written_out(shapes):
    images = [e for e in shapes["svg#2"].elements if e.kind == "image"]
    assert len(images) == 1
    assert images[0].fit == "fill"                           # preserveAspectRatio="none"
    written = Path(images[0].src)
    assert written.exists() and written.suffix == ".png"
    assert written in shapes["svg#2"].assets_written


def test_text_splits_into_runs_at_a_tspan(shapes):
    element = _find_text(shapes["svg#2"].elements, "plain")
    runs = element.paragraphs[0]["lines"][0]["runs"]
    assert [run["text"] for run in runs] == ["plain ", "bold", " tail"]
    assert [run["weight"] for run in runs] == [400, 700, 400]
    assert len(element.paragraphs) == 1 and len(element.paragraphs[0]["lines"]) == 1


def test_rotated_text_is_minus_ninety_with_an_unrotated_box(shapes):
    element = _find_text(shapes["svg#2"].elements, "Rotated")
    assert element.rotation == -90.0
    # the IR box is the pre-rotation box, so it is wide and short, not tall and narrow
    assert element.box.w > element.box.h
    assert element.paragraphs[0]["align"] == "center"        # text-anchor="middle"


# ------------------------------------------------------------- mapping table: clip, shadow, rasters


def test_rectangular_clip_path_becomes_a_clip(shapes):
    clipped = [e for e in shapes["svg#3"].elements if e.kind == "shape" and e.clip is not None]
    first = clipped[0]
    assert (first.clip.x, first.clip.y, first.clip.w, first.clip.h) == (700 + 10, 370 + 10, 40, 40)


def test_the_svg_viewport_clips_what_hangs_out_of_it(shapes):
    viewport = (700.0, 370.0, 520.0, 150.0)
    overflowing = [e for e in shapes["svg#3"].elements
                   if e.clip is not None and (e.clip.x, e.clip.y, e.clip.w, e.clip.h) == viewport]
    assert len(overflowing) == 1, "exactly the rect that hangs 40 px past the viewport"
    assert overflowing[0].box.x2 > overflowing[0].clip.x2


def test_fe_drop_shadow_becomes_a_shadow_not_a_raster(shapes):
    shadowed = [e for e in shapes["svg#3"].elements if e.kind == "shape" and e.shadow]
    assert len(shadowed) == 1
    assert shadowed[0].shadow == {"color": "2E2E38", "alpha": 0.35, "blur": 4.0, "dx": 2.0, "dy": 3.0}


def test_every_raster_only_construct_rasterises_with_a_reason(shapes):
    rasters = [e for e in shapes["svg#3"].elements if e.kind == "raster"]
    reasons = sorted(e.reason for e in rasters)
    assert reasons == sorted([
        "svg filter: fegaussianblur",
        "svg mask",
        "svg fill pattern",
        "svg clipPath: userSpaceOnUse/circle",
        "svg foreignObject",
        "svg textPath",
    ])
    for raster in rasters:
        assert any(d.elementId == DIAG_KEY_PREFIX + raster.extras[ELEMENT_KEY]
                   for d in shapes["svg#3"].diagnostics), f"{raster.reason} has no diagnostic"


def test_isolated_capture_contains_only_the_target_and_is_not_blank(shapes):
    """Every raster PNG has ink in it, and none of them is the whole slide."""
    from PIL import Image

    rasters = [e for e in shapes["svg#3"].elements if e.kind == "raster"]
    for raster in rasters:
        with Image.open(raster.src) as image:
            # Within a pixel of the box: the capture clip is snapped to whole device pixels, and a box
            # whose extent comes from text (textPath) ends on a fraction that differs between FreeType
            # (Linux) and DirectWrite (Windows) text rasterisation, so the snap can fall either way.
            assert abs(image.size[0] - raster.box.w) < 1.0 and abs(image.size[1] - raster.box.h) < 1.0, (
                raster.reason, image.size, raster.box)
            alpha = image.convert("RGBA").getchannel("A").histogram()
            inked = sum(alpha[9:])
        assert inked > 0, f"{raster.reason} captured a blank image"


# ------------------------------------------------------------------------ mapping table: the paths


def test_chevron_points_survive_the_viewbox_scale_exactly(paths):
    """`M0,20 H140 L156,44 L140,68 H0 L16,44 Z` at ×2 in a block at (40, 40)."""
    chevron = paths["svg#1"].elements[0]
    assert chevron.geometry["type"] == "custom"
    assert chevron.geometry["path"] == [
        ["M", 40.0, 80.0], ["L", 320.0, 80.0], ["L", 352.0, 128.0],
        ["L", 320.0, 176.0], ["L", 40.0, 176.0], ["L", 72.0, 128.0], ["Z"],
    ]


def test_a_quadratic_stays_a_quadratic_and_an_arc_becomes_cubics(paths):
    quadratic = [e for e in paths["svg#2"].elements
                 if any(segment[0] == "Q" for segment in e.geometry.get("path", []))]
    assert len(quadratic) == 1
    arc = [e for e in paths["svg#2"].elements if e.stroke and e.stroke["color"] == "FF4136"]
    assert len(arc) == 1
    commands = {segment[0] for segment in arc[0].geometry["path"]}
    assert commands == {"M", "C"}, "an elliptical arc must arrive as cubics only"


def test_nested_transforms_are_resolved_by_the_ctm(paths):
    rotated = [e for e in paths["svg#2"].elements if e.fill and e.fill.get("color") == "C4C4CD"]
    assert len(rotated) == 1
    # 40×24 scaled 1.5 then rotated 20°: the axis-aligned span is the rotated rectangle's
    width, height = 40 * 1.5, 24 * 1.5
    angle = math.radians(20)
    assert _close(rotated[0].box.w, width * math.cos(angle) + height * math.sin(angle), 0.01)
    assert _close(rotated[0].box.h, width * math.sin(angle) + height * math.cos(angle), 0.01)


def test_dash_pairs_are_scaled_by_the_ctm(paths):
    dashed = [e for e in paths["svg#1"].elements if e.stroke and e.stroke["dash"] != "solid"]
    assert len(dashed) == 1
    assert dashed[0].stroke["dash"] == [8.0, 6.0]            # 4 and 3 user units × 2
    assert dashed[0].stroke["width"] == 3.0                  # 1.5 × 2


def test_path_presets(paths):
    """A path that really is a rect / circle / ring segment / wedge becomes that preset."""
    elements = paths["svg#3"].elements
    rect, circle, ring, wedge, annulus, squiggle = elements

    assert rect.geometry == {"type": "rect"}
    assert (rect.box.x, rect.box.y, rect.box.w, rect.box.h) == (700 + 20, 40 + 20, 100, 60)

    # The radii are fitted to arcs that svgelements turned into cubics, so they land a ten-
    # thousandth of a pixel inside the true circle — far below the 0.5 px detection budget.
    assert circle.geometry == {"type": "ellipse"}
    assert _close(circle.box.w, 80, 0.01) and _close(circle.box.h, 80, 0.01)

    assert ring.geometry["type"] == "blockArc"
    arc = ring.geometry["arc"]
    assert _close(arc["cx"], 700 + 320, 0.01) and _close(arc["cy"], 40 + 150, 0.01)
    assert _close(arc["rOuter"], 80.0, 0.01) and _close(arc["rInner"], 30.0, 0.01)
    assert _close(arc["startDeg"], 0.0, 0.01) and _close(arc["endDeg"], 90.0, 0.01)
    assert _close(ring.box.x, 700 + 320 - 80, 0.01) and _close(ring.box.y, 40 + 150 - 80, 0.01)
    assert _close(ring.box.w, 160, 0.02) and _close(ring.box.h, 160, 0.02)

    assert wedge.geometry["type"] == "pie"
    assert wedge.geometry["arc"]["rInner"] == 0.0
    assert _close(wedge.geometry["arc"]["startDeg"], 0.0, 0.01)
    assert _close(wedge.geometry["arc"]["endDeg"], 90.0, 0.01)

    # a closed annulus is *not* a ring segment: a blockArc would sweep 0°, so the curves stay
    assert annulus.geometry["type"] == "custom"
    assert annulus.geometry["fillRule"] == "evenodd"
    assert squiggle.geometry["type"] == "custom"
    assert any(segment[0] == "C" for segment in squiggle.geometry["path"])


def test_fill_rule_comes_from_the_css_class(paths):
    evenodd = [e for e in paths["svg#3"].elements if e.geometry.get("fillRule") == "evenodd"]
    assert len(evenodd) == 1


# --------------------------------------------------------------------------- the dashed-ring rule


def test_dashed_circle_is_read_as_a_ring_segment(shapes):
    """r=100, stroke-width 24, dasharray 157.08/471.24 on a 628.32 px circumference, rotate(-90)."""
    ring = _geometries(shapes["svg#4"].elements, "blockArc")
    assert len(ring) == 1
    arc = ring[0].geometry["arc"]
    assert (arc["cx"], arc["cy"]) == (700 + 150, 40 + 150)
    assert (arc["rOuter"], arc["rInner"]) == (100 + 12, 100 - 12)
    assert arc["startDeg"] == 270.0                          # rotate(-90) from 3 o'clock
    assert _close((arc["endDeg"] - arc["startDeg"]) % 360, 157.08 / (2 * math.pi * 100) * 360, 0.01)
    # the box is the *outer* circle's box, not the geometry circle's
    assert (ring[0].box.w, ring[0].box.h) == (224, 224)
    assert ring[0].fill == {"type": "solid", "color": "1A9AFA", "alpha": 1.0}
    assert ring[0].stroke is None


def test_the_full_ring_behind_a_ring_segment_stays_an_ellipse(shapes):
    behind = _geometries(shapes["svg#4"].elements, "ellipse")
    assert len(behind) == 1
    assert behind[0].fill == {"type": "none"}
    assert behind[0].stroke["width"] == 24.0


# ------------------------------------------------------------------------------- groups and ids


def test_every_g_becomes_a_group_with_a_prefixed_id_and_a_union_box(shapes):
    expansion = shapes["svg#1"]
    assert [g.id for g in expansion.groups] == ["svg#1-g1", "svg#1-g2"]
    members = [e for e in expansion.elements if e.group == "svg#1-g1"]
    assert len(members) == 2
    box = expansion.groups[0].box
    assert box is not None
    assert _close(box.x, min(m.box.x for m in members)) and _close(box.y, min(m.box.y for m in members))


def test_nested_groups_form_a_tree(paths):
    groups = {g.id: g for g in paths["svg#2"].groups}
    assert len(groups) == 2
    children = [g for g in groups.values() if g.parent is not None]
    assert len(children) == 1 and children[0].parent in groups


def test_expansion_elements_arrive_unnumbered(shapes, paths):
    for element in _elements(shapes) + _elements(paths):
        assert element.id is None and element.z is None


def test_a_missing_svg_root_is_an_error_not_a_crash(browser, torture_dir: Path):
    html_path = torture_dir / "svg-paths.html"
    assert html_path.exists(), f"{html_path} is missing"
    context = browser.new_context(viewport={"width": CANVAS.w, "height": CANVAS.h}, device_scale_factor=1)
    page = context.new_page()
    try:
        page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
        result = expand_svg(page, ["svg#404"], CANVAS, html_path.parent / "assets")
    finally:
        page.close()
        context.close()
    expansion = result["svg#404"]
    assert expansion.elements == []
    assert [d.level for d in expansion.diagnostics] == ["error"]


# ----------------------------------------------------------------------- assembly and validation


def test_a_spliced_expansion_validates(shapes):
    """What WP1 will do: splice, renumber, relink the diagnostics, validate."""
    elements = assign_ids_and_z(_elements(shapes))
    diagnostics = _diagnostics(shapes)
    relinked = retarget_diagnostics(elements, diagnostics)
    assert relinked == sum(1 for d in diagnostics if d.elementId)
    ir = IR(
        canvas=CANVAS,
        slide=Slide(id="svg-shapes", layoutId="layout-06"),
        elements=elements,
        groups=[group for expansion in shapes.values() for group in expansion.groups],
        diagnostics=diagnostics,
    )
    assert ir.validate() == []


def test_retarget_clears_a_key_whose_element_did_not_survive():
    """A raster whose capture failed is dropped, and its diagnostic must not point at a ghost."""
    from app.engine.ir import Box, Diagnostic

    kept = Element(kind="shape", box=Box(0, 0, 1, 1), id="e1", extras={ELEMENT_KEY: "svg#1/2"})
    diagnostics = [
        Diagnostic("warn", "svg#1", "kept", DIAG_KEY_PREFIX + "svg#1/2"),
        Diagnostic("warn", "svg#1", "gone", DIAG_KEY_PREFIX + "svg#1/9"),
        Diagnostic("info", "svg#1", "untouched", None),
    ]
    assert retarget_diagnostics([kept], diagnostics) == 1
    assert [d.elementId for d in diagnostics] == ["e1", None, None]


def test_the_expect_file_matches_the_expansion(shapes, paths, torture_dir):
    import json

    for name, expansions in (("svg-shapes", shapes), ("svg-paths", paths)):
        expect_path = torture_dir / f"{name}.expect.json"
        assert expect_path.exists(), f"{expect_path} is missing"
        expect = json.loads(expect_path.read_text(encoding="utf-8"))
        counts: dict[str, int] = {}
        for element in _elements(expansions):
            counts[element.kind] = counts.get(element.kind, 0) + 1
        assert counts == expect["kinds"], name
        allowed = tuple(expect["rasterAllowed"])
        for element in _elements(expansions):
            if element.kind == "raster":
                assert element.reason.startswith(allowed), f"{name}: undeclared raster {element.reason!r}"


# ------------------------------------------------------------------------------------ determinism


def test_two_expansions_of_one_page_are_identical(browser, torture_dir: Path):
    html_path = torture_dir / "svg-shapes.html"
    assert html_path.exists(), f"{html_path} is missing"
    context = browser.new_context(viewport={"width": CANVAS.w, "height": CANVAS.h}, device_scale_factor=1)
    page = context.new_page()
    try:
        page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
        svg_ids = page.evaluate(TAG_ROOTS_JS)
        before = page.evaluate("() => document.body.innerHTML")
        first = expand_svg(page, svg_ids, CANVAS, html_path.parent / "assets")
        after = page.evaluate("() => document.body.innerHTML")
        second = expand_svg(page, svg_ids, CANVAS, html_path.parent / "assets")
    finally:
        page.close()
        context.close()

    assert after == before, "describe() left the DOM changed (a use expansion or a tspan wrapper)"
    assert _json(first) == _json(second)
    for element in _elements(first):
        if element.kind in ("raster", "image"):
            assert Path(element.src).exists()
    assert _hashes(first) == _hashes(second)


def _json(expansions: dict[str, SvgExpansion]) -> list[Any]:
    return [element.to_json() for element in _elements(expansions)]


def _hashes(expansions: dict[str, SvgExpansion]) -> dict[str, str]:
    return {
        Path(path).name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        for expansion in expansions.values()
        for path in expansion.assets_written
    }


# ------------------------------------------------------------------------------------- acceptance


def test_chevron_chain_acceptance(synthetic_svg):
    """12-WP2 acceptance: a chevron chain -> 7 custom shapes, 2 line shapes, 14 texts, 0 rasters."""
    expansion = synthetic_svg["acceptance"]["svg#1"]
    assert len(_geometries(expansion.elements, "custom")) == 7
    assert len(_geometries(expansion.elements, "line")) == 2
    assert len(_texts(expansion.elements)) == 14
    assert [e for e in expansion.elements if e.kind == "raster"] == []


def test_donuts_and_bubbles(synthetic_svg):
    """The donuts become ring segments with a full ring behind; the bubbles stay ellipses."""
    donut = synthetic_svg["acceptance"]["svg#3"]
    ring = _geometries(donut.elements, "blockArc")
    assert len(ring) == 1
    arc = ring[0].geometry["arc"]
    # r 44, stroke-width 17, dasharray 70.8 205.6 on a 276.46 px circumference, rotate(-90)
    assert (arc["rOuter"], arc["rInner"]) == (52.5, 35.5)
    assert arc["startDeg"] == 270.0
    assert _close((arc["endDeg"] - arc["startDeg"]) % 360, 70.8 / (2 * math.pi * 44) * 360, 0.01)
    assert len(_geometries(donut.elements, "ellipse")) == 1

    second = synthetic_svg["acceptance"]["svg#4"]
    assert len(_geometries(second.elements, "blockArc")) == 1
    assert _close(
        (second.elements[1].geometry["arc"]["endDeg"]
         - second.elements[1].geometry["arc"]["startDeg"]) % 360,
        73.5 / (2 * math.pi * 30) * 360, 0.01,
    )

    bubbles = _geometries(synthetic_svg["acceptance"]["svg#2"].elements, "ellipse")
    assert len(bubbles) == 8, "the bubble matrix must stay native ellipses, not a chart"

    # The sample slide's donut: r 90, stroke 36, dash 237.5, rotate(-90).
    sample = synthetic_svg["sample-slide-01"]["svg#2"]
    arc = _geometries(sample.elements, "blockArc")[0].geometry["arc"]
    assert (arc["rOuter"], arc["rInner"]) == (108.0, 72.0)
    assert arc["startDeg"] == 270.0
    assert _close((arc["endDeg"] - arc["startDeg"]) % 360, 237.5 / (2 * math.pi * 90) * 360, 0.01)


def test_synthetic_slides_expand_with_no_rasters(synthetic_svg):
    """12-WP2 acceptance: 0 rasters across real-shaped slides, and nothing dropped silently."""
    for name, expansions in synthetic_svg.items():
        rasters = [e for e in _elements(expansions) if e.kind == "raster"]
        assert rasters == [], f"{name}: {[r.reason for r in rasters]}"
        errors = [d for d in _diagnostics(expansions) if d.level == "error"]
        assert errors == [], f"{name}: {[d.message for d in errors]}"
        warnings = [d for d in _diagnostics(expansions) if d.level == "warn"]
        assert warnings == [], f"{name}: {[d.message for d in warnings]}"


def test_every_synthetic_element_is_inside_the_canvas_and_valid(synthetic_svg):
    for name, expansions in synthetic_svg.items():
        elements = assign_ids_and_z(_elements(expansions))
        ir = IR(
            canvas=CANVAS,
            slide=Slide(id=name, layoutId="layout-05"),
            elements=elements,
            groups=[g for expansion in expansions.values() for g in expansion.groups],
            diagnostics=_diagnostics(expansions),
        )
        retarget_diagnostics(elements, ir.diagnostics)
        assert ir.validate() == [], name
