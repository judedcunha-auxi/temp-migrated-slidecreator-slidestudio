"""WP1 — the HTML/CSS extractor, measured against fixtures rather than against an impression.

Every assertion here is a number someone can re-derive from the fixture HTML with a ruler: the KPI
card's border is 3 px wide at x = 64 because the CSS says `left:64px; border-left:3px`, and the
operator's line box is 60 px tall because the CSS says `line-height:60px`. That is the point — an
extractor that is "roughly right" produces a deck that is visibly wrong, and only exact numbers
catch the drift.

The families under `tests/engine/fixtures/wp1/` mirror the torture families WP0b owns (`text`, `boxes`,
`images`, `tables`, `charts`), each with an `expect.json` in the same shape, so the same assertions
move over to `fixtures/torture/` once those files land.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from app.config import engine as config
from app.engine.extract import DERIVED_SUBDIR
from app.engine.extract.html import PAGE_JS
from app.engine.ir import IR, Box, Element
from app.engine.manifest import Manifest
from tests.engine.helpers import extract_html

WP1_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "wp1"
FAMILIES = ("text", "boxes", "images", "tables", "charts", "tables-rich")


# ------------------------------------------------------------------------------------- helpers


@pytest.fixture(scope="module")
def family_assets(sample_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The sample project's assets plus the torture square logo `images.html` asks for."""
    assets = tmp_path_factory.mktemp("wp1-assets")
    for source in (sample_dir / "assets").iterdir():
        shutil.copyfile(source, assets / source.name)
    shutil.copyfile(config.TORTURE_FIXTURE / "assets" / "logo-160x160.png", assets / "logo-160x160.png")
    return assets


@pytest.fixture(scope="module")
def family_irs(sample_manifest: Manifest, family_assets: Path) -> dict[str, IR]:
    """Every WP1 family extracted once — the browser, not the walk, is the expensive part."""
    irs: dict[str, IR] = {}
    for name in FAMILIES:
        html = WP1_FIXTURES / f"{name}.html"
        assert html.exists(), f"{html} is missing — the WP1 fixture family is incomplete"
        irs[name] = extract_html(html, sample_manifest, "layout-05", family_assets,
                                 slide_id=f"wp1-{name}", title=name)
    return irs


def _texts(ir: IR) -> list[Element]:
    return [e for e in ir.elements if e.kind == "text"]


def _shapes(ir: IR) -> list[Element]:
    return [e for e in ir.elements if e.kind == "shape"]


def _named(ir: IR, name: str) -> Element:
    match = [e for e in ir.elements if e.name == name]
    assert match, f"no element named {name!r}; have {[e.name for e in ir.elements]}"
    return match[0]


def _all_runs(element: Element) -> list[dict]:
    return [run for p in element.paragraphs or [] for line in p["lines"] for run in line["runs"]]


# ----------------------------------------------------------------------------- family contracts


@pytest.mark.parametrize("family", FAMILIES)
def test_family_matches_its_expectations(family: str, family_irs: dict[str, IR]):
    """Kind counts and the raster budget, in the `expect.json` shape the torture suite uses."""
    expect = json.loads((WP1_FIXTURES / f"{family}.expect.json").read_text(encoding="utf-8"))
    ir = family_irs[family]

    assert ir.validate() == [], f"{family}: {ir.validate()}"
    assert ir.counts() == expect["kinds"]

    allowed = expect["rasterAllowed"]
    for raster in ir.of_kind("raster"):
        assert any(token in raster.reason for token in allowed), (
            f"{family}: unexpected raster {raster.id} — {raster.reason}"
        )
    authored = [c for c in ir.of_kind("chart") if c.origin == "authored"]
    assert len(authored) == expect["charts"]["authored"]


def test_every_element_carries_identity_and_paint_order(family_irs: dict[str, IR]):
    """`id`, `z` and a source path are what make a defect traceable back to a line of HTML."""
    for family, ir in family_irs.items():
        assert [e.z for e in ir.elements] == list(range(len(ir.elements))), family
        assert len({e.id for e in ir.elements}) == len(ir.elements), family
        for element in ir.elements:
            assert element.source.get("path"), f"{family}/{element.id} has no source path"
            assert element.name, f"{family}/{element.id} has no name"


# --------------------------------------------------------------------------------------- text


def test_paragraph_run_merges_consecutive_sibling_blocks(family_irs: dict[str, IR]):
    """The element-boundary rule: three inline-only blocks under one parent are one text element."""
    stack = _named(family_irs["text"], "stack")
    assert stack.kind == "text"
    assert [p["lines"][0]["runs"][0]["text"] for p in stack.paragraphs] == [
        "120k", "Partner ambition by 2030", "Raised from 90k after the first goal was met",
    ]
    # Spacing is measured from the gap the browser left, so collapsed margins cannot lie about it.
    assert [round(p["spaceBeforePx"]) for p in stack.paragraphs] == [0, 7, 4]


def test_a_long_paragraph_is_split_into_the_browsers_lines(family_irs: dict[str, IR]):
    """Three lines, each with its own box, and the bold run split at the exact wrap point."""
    long = _named(family_irs["text"], "long")
    lines = long.paragraphs[0]["lines"]
    assert len(lines) == 3
    # line-height: 1.5 on 14 px text — the boxes step by exactly that, 21 px.
    tops = [line["box"]["y"] for line in lines]
    assert all(abs((tops[i + 1] - tops[i]) - 21.0) < 0.01 for i in range(len(tops) - 1))
    assert all(abs(line["box"]["h"] - 21.0) < 0.01 for line in lines)

    joined = "".join(run["text"] for line in lines for run in line["runs"])
    assert joined.startswith("The binding constraint is shifting from volume")
    bold = [run["text"] for line in lines for run in line["runs"] if run["weight"] >= 700]
    assert bold == ["volume", "to value"], "a run spanning two lines is split at the fragment boundary"


def test_br_is_a_line_break_inside_one_paragraph(family_irs: dict[str, IR]):
    broken = _named(family_irs["text"], "broken")
    assert len(broken.paragraphs) == 1
    assert [line["runs"][0]["text"] for line in broken.paragraphs[0]["lines"]] == [
        "First line here", "second line here",
    ]


def test_line_height_sets_the_line_box_not_the_glyph_height(family_irs: dict[str, IR]):
    """`line-height: 60px` on a 60 px block: one line box filling the block, not a 23 px glyph box."""
    operator = _named(family_irs["text"], "op")
    assert operator.box.w == 40.0 and operator.box.h == 60.0
    assert operator.paragraphs[0]["align"] == "center"
    line = operator.paragraphs[0]["lines"][0]
    assert line["box"]["h"] == 60.0
    assert abs(line["box"]["y"] - 200.0) < 0.01
    assert line["runs"][0]["text"] == "×"


def test_bullets_carry_char_level_and_indent(family_irs: dict[str, IR]):
    bullets = [p for e in _texts(family_irs["text"]) for p in e.paragraphs if p["bullet"]]
    assert len(bullets) == 2
    for paragraph in bullets:
        assert paragraph["bullet"]["type"] == "char"
        assert paragraph["bullet"]["char"] == "•"
        assert paragraph["bullet"]["level"] == 0
        # `padding-left: 24px` on the <ul> is the marker gutter, so that is the indent.
        assert paragraph["bullet"]["indentPx"] == pytest.approx(24.0, abs=0.01)


def test_inline_styling_becomes_runs_with_resolved_values(family_irs: dict[str, IR]):
    marks = _named(family_irs["text"], "marks")
    runs = _all_runs(marks)
    by_text = {run["text"]: run for run in runs}

    assert by_text["SHOUTED LABEL"]["caps"] is True, "text-transform is applied to the run text"
    assert by_text["SHOUTED LABEL"]["letterSpacingPx"] == pytest.approx(1.5)
    assert by_text["underline"]["underline"] is True
    assert by_text["strike"]["strike"] is True
    assert by_text["italic"]["italic"] is True
    assert [run["baseline"] for run in runs if run["baseline"] != "normal"] == ["sub", "super"]
    assert all(len(run["color"]) == 6 for run in runs), "colours are resolved to 6 hex digits"


def test_a_detached_chip_is_its_own_text_over_its_own_shape(family_irs: dict[str, IR]):
    """`compound<span class="chip">ILLUSTRATIVE</span>`: no space separates the chip from the word
    before it — only its `margin-left` does — so it leaves the heading's paragraph. Kept inline, the
    two runs concatenate into "compoundILLUSTRATIVE" in PowerPoint (hand-off §13, P0)."""
    ir = family_irs["text"]
    head = _named(ir, "head")
    assert "".join(r["text"] for r in _all_runs(head)) == "Size and spend compound"

    chip_texts = [e for e in ir.elements if e.kind == "text"
                  and "".join(r["text"] for r in _all_runs(e)) == "ILLUSTRATIVE"]
    assert len(chip_texts) == 1
    chip_text = chip_texts[0]
    chip_shapes = [s for s in _shapes(ir) if (s.fill or {}).get("color") == "FFE600"]
    assert len(chip_shapes) == 1, "painted once — by the element walk, not also as inline decoration"
    chip = chip_shapes[0]
    assert chip.z < chip_text.z, "the chip's background is painted before its own text"
    assert chip_text.box.x >= head.box.x + head.box.w, "the chip text sits right of the heading"

    run = _all_runs(chip_text)[0]
    assert run["baselineShiftPx"] == 0, "the chip's vertical-align is in its line box, not a run shift"
    # padding: 2px 5px around the text, so the shape is wider and taller than the glyphs.
    assert chip.box.h == pytest.approx(14.0, abs=0.01)
    assert chip.box.w > 40


def test_rotation_records_the_angle_and_the_unrotated_box(family_irs: dict[str, IR]):
    """`rotate(-90deg)` on a 200x24 block: the box stays 200x24 and the angle is carried separately."""
    rotated = _named(family_irs["text"], "rot")
    assert rotated.rotation == pytest.approx(-90.0)
    assert (rotated.box.x, rotated.box.y, rotated.box.w) == (1100.0, 300.0, 200.0)
    assert rotated.clip is None, "an axis-aligned clip cannot be taken from a pre-rotation box"


def test_nowrap_is_recorded_so_the_emitter_does_not_re_wrap(family_irs: dict[str, IR]):
    assert _named(family_irs["text"], "nowrap").wrap is False


def test_clipped_text_is_an_error_diagnostic(family_irs: dict[str, IR]):
    """PowerPoint cannot clip a text frame, so this has to be said out loud, not silently cropped."""
    ir = family_irs["text"]
    clipped = [e for e in _texts(ir) if e.clip is not None]
    assert len(clipped) == 1
    assert clipped[0].clip.w == pytest.approx(120.0, abs=0.01)
    errors = [d for d in ir.diagnostics if d.level == "error" and d.elementId == clipped[0].id]
    assert errors and "clipped" in errors[0].message


def test_speaker_notes_come_from_aside_notes(family_irs: dict[str, IR]):
    assert family_irs["text"].slide.notes == "Speaker notes: the value chain has been built."
    assert not [e for e in family_irs["text"].elements if "notes" in (e.source.get("path") or "")]


# -------------------------------------------------------------------------------------- boxes


def test_uniform_border_is_a_stroke_on_a_deflated_box(family_irs: dict[str, IR]):
    """A CSS border sits inside the border box; a centred 4 px stroke covers the same pixels."""
    uniform = _named(family_irs["boxes"], "uniform")
    assert uniform.stroke["width"] == 4.0 and uniform.stroke["color"] == "2DB757"
    assert (uniform.box.x, uniform.box.y, uniform.box.w, uniform.box.h) == (582.0, 42.0, 156.0, 76.0)


def test_differing_border_sides_become_one_thin_shape_each(family_irs: dict[str, IR]):
    """`border: 1px …; border-top: 3px …` is four shapes, because one stroke cannot say that.

    Each is the CSS join polygon (WP-E): every side meets a painted neighbour at both corners, so
    all four are trapezoids cut on the corner diagonals, with the same boxes the rectangles had.
    """
    ir = family_irs["boxes"]
    sides = [s for s in _shapes(ir) if s.source.get("path", "").endswith("div#sides")]
    assert len(sides) == 4
    boxes = sorted((s.box.x, s.box.y, s.box.w, s.box.h) for s in sides)
    assert boxes == [
        (760.0, 40.0, 1.0, 80.0),     # left, 1 px
        (760.0, 40.0, 160.0, 3.0),    # top, 3 px and a different colour
        (760.0, 119.0, 160.0, 1.0),   # bottom
        (919.0, 40.0, 1.0, 80.0),     # right
    ]
    assert {s.geometry["type"] for s in sides} == {"custom"}
    top = next(s for s in sides if s.box.h == 3.0)
    assert top.fill == {"type": "solid", "color": "1A9AFA", "alpha": 1}
    assert [seg[1:] for seg in top.geometry["path"][:4]] == [[760, 40], [920, 40], [919, 43], [761, 43]]


def test_gradients_resolve_keywords_to_the_schema_angle(family_irs: dict[str, IR]):
    ir = family_irs["boxes"]
    angled = _named(ir, "lgrad").fill
    assert angled["kind"] == "linear" and angled["angle"] == 135
    assert [stop["pos"] for stop in angled["stops"]] == [0, 1]
    assert [stop["color"] for stop in angled["stops"]] == ["1A9AFA", "2E2E38"]
    # `to right` is 90 degrees in the schema's convention (02-IR-SCHEMA.md).
    assert _named(ir, "lgrad2").fill["angle"] == 90
    assert _named(ir, "rgrad").fill["kind"] == "radial"


def test_shadow_opacity_radius_and_clip_path(family_irs: dict[str, IR]):
    ir = family_irs["boxes"]
    assert _named(ir, "shadow").shadow == {"color": "000000", "alpha": 0.25, "dx": 0, "dy": 4, "blur": 12}
    assert _named(ir, "faded").opacity == pytest.approx(0.5)
    assert _named(ir, "radius").geometry["radius"] == {"tl": 12, "tr": 4, "br": 12, "bl": 4}
    clipped = _named(ir, "clipped")
    assert clipped.geometry["type"] == "custom"
    assert clipped.geometry["path"][0] == ["M", 940.0, 160.0]
    assert clipped.geometry["path"][-1] == ["Z"]
    assert _named(ir, "alpha").fill["alpha"] == pytest.approx(0.4)


def test_overflow_hidden_clips_the_descendant(family_irs: dict[str, IR]):
    """The clip is already intersected with every ancestor, so the emitter never walks the tree."""
    inner = next(s for s in _shapes(family_irs["boxes"]) if s.source.get("path", "").endswith("div.inner:nth-child(1)"))
    assert (inner.box.x, inner.box.y, inner.box.w, inner.box.h) == (80.0, 320.0, 200.0, 200.0)
    assert (inner.clip.x, inner.clip.y, inner.clip.w, inner.clip.h) == (80.0, 320.0, 80.0, 40.0)


def test_body_overflow_hidden_does_not_clip_everything(family_irs: dict[str, IR]):
    """`html,body{overflow:hidden}` is on every slide; a clip that does not cut is not recorded."""
    for family, ir in family_irs.items():
        clipped = [e.id for e in ir.elements if e.clip is not None]
        assert len(clipped) <= 2, f"{family} recorded a clip on {clipped}"


def test_data_pptx_group_becomes_a_group_with_members(family_irs: dict[str, IR]):
    ir = family_irs["boxes"]
    assert len(ir.groups) == 1
    group = ir.groups[0]
    assert group.name == "pair" and group.parent is None
    members = [e for e in ir.elements if e.group == group.id]
    assert len(members) == 2


def test_z_index_beats_document_order(family_irs: dict[str, IR]):
    """A real stacking walk: `z-index: 1` paints before `z-index: 2` even though it comes later."""
    ir = family_irs["boxes"]
    assert _named(ir, "zfront").z < _named(ir, "zback").z


def test_every_raster_only_construct_is_caught_and_explained(family_irs: dict[str, IR]):
    """The authoring contract's raster-only list, each one named in its own reason and diagnostic."""
    ir = family_irs["boxes"]
    reasons = {e.name: e.reason for e in ir.of_kind("raster")}
    assert "blur" in reasons["blurred"]
    assert reasons["conic"] == "conic-gradient"
    assert reasons["layers"].startswith("multiple background layers")
    assert reasons["blend"] == "mix-blend-mode: multiply"
    assert reasons["inset"] == "inset box-shadow"
    assert reasons["forced"] == 'data-pptx="raster"'
    assert reasons["scaled"].startswith("unsupported CSS transform")
    for raster in ir.of_kind("raster"):
        assert any(d.elementId == raster.id and d.level == "warn" for d in ir.diagnostics), raster.name


def test_a_raster_inside_a_rotated_ancestor_is_captured_unrotated(family_irs: dict[str, IR]):
    """The box is the pre-rotation box, so the PNG must be too — or the emitter turns it twice."""
    turned = _named(family_irs["boxes"], "turnedfx")
    assert turned.kind == "raster" and turned.rotation == pytest.approx(30.0, abs=0.01)
    assert (turned.box.w, turned.box.h) == (60.0, 40.0)
    with Image.open(turned.src) as image:
        width, height = image.size
    # Rotated by 30°, a 60x40 box has a 72x64 hull; the capture must be the 60x40 one.
    assert (width, height) == (60, 40), "the capture baked the ancestor's rotation in"


def test_scale_minus_one_is_a_flip_not_a_half_turn(family_irs: dict[str, IR]):
    """`scale(-1,1)` decomposes as rotate(180)+flipV; folded back, it is the flip the author wrote."""
    mirror = _named(family_irs["boxes"], "mirror")
    assert (mirror.flipH, mirror.flipV, mirror.rotation) == (True, False, 0.0)
    assert (mirror.box.x, mirror.box.w) == (900.0, 100.0)


def test_a_raster_is_captured_in_isolation(family_irs: dict[str, IR]):
    """The blurred box sits on top of a red sibling; its PNG must not contain one red pixel."""
    ir = family_irs["boxes"]
    raster = _named(ir, "blurred")
    assert raster.kind == "raster" and "filter" in raster.reason

    with Image.open(raster.src) as image:
        rgba = image.convert("RGBA")
        data = rgba.tobytes()
    pixels = [tuple(data[i:i + 4]) for i in range(0, len(data), 4)]
    reds = [p for p in pixels if p[3] > 8 and p[0] > 150 and p[1] < 80 and p[2] < 80]
    assert not reds, f"{len(reds)} red pixels leaked in from the sibling under the raster"
    assert any(p[3] > 8 and p[2] > 150 for p in pixels), "the raster captured nothing at all"


def test_derived_images_never_land_in_the_project_assets(family_irs: dict[str, IR], family_assets: Path):
    """Derived images go to `<workspace>/derived/<slide id>/`, never into the input assets."""
    rasters = [e for ir in family_irs.values() for e in ir.elements if e.kind == "raster"]
    assert rasters
    for element in rasters:
        src = Path(element.src)
        assert src.parent.parent.name == DERIVED_SUBDIR, src
        assert family_assets not in src.parents


# ------------------------------------------------------------------------------------- images


def test_object_fit_cover_crops_the_source_not_the_box(family_irs: dict[str, IR]):
    """A 2:1 box over a 3:2 photo keeps the box and cuts the same fraction off top and bottom."""
    cover = _named(family_irs["images"], "cover")
    assert (cover.box.w, cover.box.h) == (200.0, 100.0)
    assert cover.fit == "cover"
    assert cover.crop["l"] == 0 and cover.crop["r"] == 0
    assert cover.crop["t"] == pytest.approx(cover.crop["b"])
    # scale = 200/naturalW: the sample's 900 × 600 photo is drawn 133.33 px high from y = 23.33, and
    # Blink paints that rect on whole pixels, 23 to 157 (`ImagePainter`), so the frame's 40–140 shows
    # 17/134 of the source cut off each end — symmetric and exact.
    assert cover.crop["t"] == pytest.approx(17 / 134, abs=1e-6)


def test_object_fit_contain_shrinks_the_box_instead_of_inventing_bars(family_irs: dict[str, IR]):
    contain = _named(family_irs["images"], "contain")
    assert contain.crop == {"l": 0, "t": 0, "r": 0, "b": 0}
    assert contain.box.h == pytest.approx(100.0)
    assert contain.box.w < 200.0, "a contained image is drawn narrower than its element"
    assert contain.box.x > 280.0, "and centred by object-position"


def test_object_fit_fill_and_border_radius(family_irs: dict[str, IR]):
    ir = family_irs["images"]
    assert _named(ir, "fill").crop == {"l": 0, "t": 0, "r": 0, "b": 0}
    round_image = _named(ir, "round")
    assert round_image.radius == pytest.approx(60.0) and round_image.circle is True


def test_asset_urls_resolve_to_real_files_and_intrinsic_size_is_known(family_irs: dict[str, IR],
                                                                      family_assets: Path):
    """Routing is what makes the crop maths possible: without it the image has no intrinsic size."""
    ir = family_irs["images"]
    for image in ir.of_kind("image"):
        assert Path(image.src).is_file()
        assert Path(image.src).parent == family_assets, image.src
    # A cover crop can only be non-zero if the browser knew the photo's real pixel size.
    assert _named(ir, "cover").crop["t"] > 0


def test_a_missing_asset_is_an_error_not_a_blank_box(family_irs: dict[str, IR]):
    ir = family_irs["images"]
    assert not [e for e in ir.elements if e.name == "missing"], "a broken image is dropped, not emitted"
    sources = [d.source for d in ir.diagnostics if d.level == "error"]
    assert "asset:not-here.png" in sources


def test_background_image_is_an_image_element(family_irs: dict[str, IR]):
    background = _named(family_irs["images"], "bg")
    assert background.kind == "image" and background.fit == "cover"
    assert (background.box.w, background.box.h) == (240.0, 120.0)


# ------------------------------------------------------------------------------------- tables


def test_table_grid_spans_and_cell_styling(family_irs: dict[str, IR]):
    table = family_irs["tables"].of_kind("table")[0]
    assert (table.rows, table.cols) == (4, 4)
    assert len(table.colWidthsPx) == 4 and len(table.rowHeightsPx) == 4
    assert sum(table.colWidthsPx) == pytest.approx(table.box.w, abs=2.0)

    by_slot = {(c["r"], c["c"]): c for c in table.cells}
    assert by_slot[(0, 1)]["colSpan"] == 2, "the 2023 header spans two columns"
    assert by_slot[(1, 0)]["rowSpan"] == 2, "Partner spans two rows"
    assert (2, 0) not in by_slot, "a spanned-over slot holds no cell of its own"
    assert by_slot[(0, 0)]["fill"] == {"type": "solid", "color": "2E2E38", "alpha": 1}
    assert by_slot[(1, 2)]["valign"] == "middle"
    assert by_slot[(0, 0)]["paddingPx"] == {"t": 6, "r": 8, "b": 6, "l": 8}
    assert by_slot[(0, 0)]["borders"]["top"]["color"] == "C4C4CD"


def test_table_cells_carry_paragraphs_like_any_other_text(family_irs: dict[str, IR]):
    table = family_irs["tables"].of_kind("table")[0]
    by_slot = {(c["r"], c["c"]): c for c in table.cells}
    header = by_slot[(0, 0)]["paragraphs"][0]["lines"][0]["runs"][0]
    assert header["text"] == "Segment"
    assert header["weight"] >= 700 and header["color"] == "FFFFFF"


# -------------------------------------------------------------------------------- tables-rich
# WP-A (docs/archive/engine/fidelity/10-WPA-tables.md §8.5). Every number below is re-derivable from
# `wp1/tables-rich.html` with a ruler: the table sits at top 40 px under an 18 px caption line with a
# 6 px margin, so its grid starts at 64 px; the fonts are the client master's Arial.


def _rich(family_irs: dict[str, IR]) -> tuple[IR, Element, Element]:
    ir = family_irs["tables-rich"]
    return ir, _named(ir, "rich"), _named(ir, "separate")


def _slot(table: Element) -> dict:
    return {(c["r"], c["c"]): c for c in table.cells}


def _cell_x(table: Element, r: int, c: int) -> dict:
    return table.extras["x-wpa"]["cells"][f"{r}:{c}"]


def _overlays(ir: IR, table: Element, r: int | None = None, c: int | None = None,
              role: str | None = None) -> list[Element]:
    out = []
    for element in ir.elements:
        tag = (element.extras or {}).get("x-wpa") or {}
        if tag.get("table") != table.name or "role" not in tag:
            continue
        if r is not None and tag.get("cell") != [r, c]:
            continue
        if role is not None and tag.get("role") != role:
            continue
        out.append(element)
    return out


def _inside(inner: Box, outer: Box, slack: float = 0.5) -> bool:
    return (inner.x >= outer.x - slack and inner.y >= outer.y - slack and
            inner.x2 <= outer.x2 + slack and inner.y2 <= outer.y2 + slack)


def _texts_of(paragraph: dict) -> list[str]:
    return ["".join(run["text"] for run in line["runs"]) for line in paragraph["lines"]]


def test_tables_rich_grid_box_excludes_the_caption(family_irs: dict[str, IR]):
    """Chromium's table rect includes the <caption>; the native table starts where the rows do."""
    _, rich, separate = _rich(family_irs)
    assert rich.box.y == pytest.approx(40 + 18 + 6, abs=0.5)
    assert sum(rich.rowHeightsPx) == pytest.approx(rich.box.h, abs=0.01)
    assert sum(rich.colWidthsPx) == pytest.approx(rich.box.w, abs=0.01)
    assert sum(separate.rowHeightsPx) == pytest.approx(separate.box.h, abs=0.01)
    header = _slot(rich)[(0, 0)]["paragraphs"][0]["lines"][0]["box"]
    assert header["y"] == pytest.approx(rich.box.y + 5, abs=0.5), "header text at the grid top + padding"


def test_tables_rich_cells_take_their_row_fill(family_irs: dict[str, IR]):
    """Row, row group and column backgrounds paint under a transparent cell; composited, not origin."""
    _, rich, _ = _rich(family_irs)
    cells = _slot(rich)
    solid = lambda colour, alpha=1: {"type": "solid", "color": colour, "alpha": alpha}  # noqa: E731
    assert all(cells[(0, c)]["fill"] == solid("0B2545") for c in range(6)), "the header's row fill"
    assert cells[(2, 0)]["fill"] == solid("F3F5F9"), "zebra"
    assert cells[(3, 2)]["fill"] == solid("DCF5E6"), "highlighted row"
    assert cells[(1, 1)]["fill"] == solid("FFF8E1") and _cell_x(rich, 1, 1)["fillFrom"] == "col"
    assert cells[(2, 1)]["fill"] == solid("F3F5F9"), "an opaque zebra row covers the column fill"
    # rgba(11,92,173,.2) over nothing is itself; over the column's #FFF8E1 it composites to an opaque mix.
    assert cells[(5, 0)]["fill"] == solid("0B5CAD", 0.2)
    mixed = cells[(5, 1)]["fill"]
    assert mixed["alpha"] == 1 and mixed["color"] == "CED9D7"
    surface = _named(family_irs["tables-rich"], "rich surface")
    assert surface.fill == solid("FFFFFF") and surface.stroke is None, "the table's own white, behind it"
    assert cells[(3, 0)]["rowSpan"] == 2 and cells[(3, 0)]["fill"] == solid("DCF5E6"), (
        "a rowspan cell takes its first row's fill — the browser paints it under the whole cell (measured)")


def test_tables_rich_gradient_row_is_a_shape_behind_the_table(family_irs: dict[str, IR]):
    ir, rich, _ = _rich(family_irs)
    gradients = _overlays(ir, rich, role="cellGradient")
    assert [g.extras["x-wpa"]["cell"] for g in gradients] == [[7, c] for c in range(6)]
    for gradient in gradients:
        assert gradient.fill["type"] == "gradient" and gradient.z < rich.z and gradient.group is None
    assert all(_slot(rich)[(7, c)]["fill"] == {"type": "none"} for c in range(6))
    # One gradient across the row, not one per cell: the stops continue from cell to cell.
    first, second = gradients[0].fill["stops"], gradients[1].fill["stops"]
    assert first[0]["color"] == "EAF2FF" and first[-1]["color"] == second[0]["color"]


def test_tables_rich_chips_leave_the_flow(family_irs: dict[str, IR]):
    ir, rich, _ = _rich(family_irs)
    for r, c in ((1, 5), (2, 4)):
        assert _cell_x(rich, r, c)["flow"] == "none" and _slot(rich)[(r, c)]["paragraphs"] == []
        chip = _overlays(ir, rich, r, c, "chip")
        text = _overlays(ir, rich, r, c, "chipText")
        assert len(chip) == 1 and len(text) == 1
        assert _inside(text[0].box, chip[0].box), (text[0].box, chip[0].box)
        assert chip[0].z > rich.z and text[0].z > chip[0].z
    ellipse = [e for e in ir.elements if (e.source or {}).get("svg") and _inside(e.box, _overlays(ir, rich, 2, 4, "chip")[0].box)]
    assert len(ellipse) == 1 and ellipse[0].z > rich.z and ellipse[0].geometry["type"] == "ellipse"
    badge = _overlays(ir, rich, 1, 0, "chipText")
    assert [_texts_of(p) for p in badge[0].paragraphs] == [["3"]]
    assert _texts_of(_slot(rich)[(1, 0)]["paragraphs"][0]) == ["Singapore"]
    # 18 px badge + 6 px margin + the rendered space before "Singapore" (Arial 11 px: 3.06 px).
    assert _cell_x(rich, 1, 0)["indentPx"][0] == pytest.approx(18 + 6 + 11 * 0.2778, abs=0.3)


def test_tables_rich_heat_cell_is_a_block_chip(family_irs: dict[str, IR]):
    """A filled block that owns the cell's text: shape and text both go over the table."""
    ir, rich, _ = _rich(family_irs)
    assert _cell_x(rich, 3, 1)["flow"] == "none"
    shape, text = _overlays(ir, rich, 3, 1, "blockChip"), _overlays(ir, rich, 3, 1, "blockChipText")
    assert len(shape) == 1 and shape[0].fill["color"] == "FDE68A"
    assert len(text) == 1 and _texts_of(text[0].paragraphs[0]) == ["42"] and text[0].z > shape[0].z
    assert _inside(text[0].box, shape[0].box)


def test_tables_rich_padding_zero_inner_div_folds(family_irs: dict[str, IR]):
    """`<td style="padding:0"><div style="background;padding:6px">`: the div's fill is the cell's."""
    ir, rich, _ = _rich(family_irs)
    cell, extras = _slot(rich)[(3, 4)], _cell_x(rich, 3, 4)
    assert cell["fill"] == {"type": "solid", "color": "EEEEFF", "alpha": 1} and extras["fillFrom"] == "fold"
    assert _overlays(ir, rich, 3, 4) == []
    assert _texts_of(cell["paragraphs"][0]) == ["Folded"]
    # The folded div's padding is the cell's inset now: 6 px on the left, 6 px + half the 1 px rule on top.
    assert extras["slot"]["l"] == pytest.approx(6) and extras["slot"]["t"] == pytest.approx(6.5)


def test_tables_rich_mid_line_chip_splits_its_paragraph(family_irs: dict[str, IR]):
    """`Revenue <chip>+12%</chip> YoY` (master brief §11 as amended by B, read inside a cell — the
    kinds review's finding 1): the text before the chip stays in the cell, the chip is a shape with its
    own words over it, and the text after it — beside "Revenue" in one band — is a text box of its own.
    No run holds the chip's words, and no paragraph is lifted whole over the table."""
    ir, rich, _ = _rich(family_irs)
    assert _cell_x(rich, 3, 2)["flow"] == "text"
    assert [_texts_of(p) for p in _slot(rich)[(3, 2)]["paragraphs"]] == [["Revenue"]]
    chip = _overlays(ir, rich, 3, 2, "chip")
    words = _overlays(ir, rich, 3, 2, "chipText")
    after = _overlays(ir, rich, 3, 2, "bandText")
    assert len(chip) == 1 and len(words) == 1 and len(after) == 1
    assert _texts_of(words[0].paragraphs[0]) == ["+12%"] and _texts_of(after[0].paragraphs[0]) == ["YoY"]
    assert words[0].box.x >= chip[0].box.x + 3.5, "the chip's words start inside its 4 px padding"
    assert after[0].box.x >= chip[0].box.x2, "the words after the chip start after it"
    assert words[0].z > chip[0].z > rich.z
    assert not [e for e in ir.elements if ((e.extras or {}).get("x-wpa") or {}).get("role") == "flowText"]
    assert not [d for d in ir.diagnostics if "mid-line" in d.message]


def _flow(family_irs: dict[str, IR]) -> tuple[IR, Element]:
    ir = family_irs["tables-rich"]
    return ir, _named(ir, "flow")


def _content_left(table: Element, r: int, c: int) -> float:
    """Where the native cell's text frame starts: the grid line + the slot + the CSS padding."""
    cell = _slot(table)[(r, c)]
    return (table.box.x + sum(table.colWidthsPx[:c]) + _cell_x(table, r, c)["slot"]["l"]
            + cell["paddingPx"]["l"])


def test_tables_rich_text_beside_an_inline_icon_or_image_is_its_own_segment(family_irs: dict[str, IR]):
    """`Status <svg/> Done`, `Rated <img> by analysts` (review finding 2): the words after the box are a
    band text box after it, not a run the icon or the flag is drawn over."""
    ir, flow = _flow(family_irs)
    for c, before, after in ((0, "Status", "Done"), (1, "Rated", "by analysts")):
        assert [_texts_of(p) for p in _slot(flow)[(0, c)]["paragraphs"]] == [[before]]
        band = _overlays(ir, flow, 0, c, "bandText")
        assert len(band) == 1 and _texts_of(band[0].paragraphs[0]) == [after]
    circle = [e for e in ir.elements if (e.source or {}).get("svg") and e.geometry["type"] == "ellipse"
              and abs(e.box.y - flow.box.y) < 14]
    assert len(circle) == 1 and circle[0].box.x2 <= _overlays(ir, flow, 0, 0, "bandText")[0].box.x
    flag = _overlays(ir, flow, 0, 1, "image")
    assert len(flag) == 1 and flag[0].box.x2 <= _overlays(ir, flow, 0, 1, "bandText")[0].box.x


def test_tables_rich_a_chip_that_starts_a_wrapped_line(family_irs: dict[str, IR]):
    """`Revenue grew strongly this <chip>+12%</chip> year on year` in a 144 px column: the chip starts
    line 2, so the text after it is a second cell paragraph, stacked, whose first line is indented by
    the chip + a space — not a second line written under the chip."""
    ir, flow = _flow(family_irs)
    paragraphs = _slot(flow)[(0, 2)]["paragraphs"]
    assert [_texts_of(p) for p in paragraphs] == [["Revenue grew strongly this"], ["year on year"]]
    chip = _overlays(ir, flow, 0, 2, "chip")[0]
    left = _content_left(flow, 0, 2)
    assert chip.box.x == pytest.approx(left, abs=0.5)
    extras = _cell_x(flow, 0, 2)
    assert extras["indentPx"][1] == pytest.approx(chip.box.w + 11 * 0.2778, abs=0.3)
    assert extras["marginPx"][1] == 0
    assert paragraphs[1]["spaceBeforePx"] == pytest.approx(0, abs=0.5)
    assert _texts_of(_overlays(ir, flow, 0, 2, "chipText")[0].paragraphs[0]) == ["+12%"]


def test_tables_rich_a_chip_first_list_item_that_wraps(family_irs: dict[str, IR]):
    """`<li><pill>New</pill> Sandbox pilots across three regions</li>` (review finding 2, package B's
    R8 in a cell): the first line keeps the bullet with its text at the pill's end (`marL`) and the
    bullet back at the list's edge (`indent`); the wrapped line is a paragraph of its own at the item's
    content edge, where the browser put it."""
    ir, flow = _flow(family_irs)
    paragraphs = _slot(flow)[(0, 3)]["paragraphs"]
    assert [_texts_of(p) for p in paragraphs] == [["Sandbox pilots across three"], ["regions"], ["Talent visas"]]
    assert paragraphs[0]["bullet"] and paragraphs[1]["bullet"] is None and paragraphs[2]["bullet"]
    extras, left = _cell_x(flow, 0, 3), _content_left(flow, 0, 3)
    pill = _overlays(ir, flow, 0, 3, "chip")[0]
    assert extras["marginPx"][0] == pytest.approx(pill.box.x2 - left + 11 * 0.2778, abs=0.3)
    assert extras["marginPx"][0] + extras["indentPx"][0] == pytest.approx(0, abs=0.01), "the bullet at the list edge"
    assert extras["marginPx"][1] == 14 and extras["indentPx"][1] == 0
    assert extras["marginPx"][2] == 14 and extras["indentPx"][2] == -14
    assert paragraphs[1]["lines"][0]["box"]["x"] == pytest.approx(left + 14, abs=0.5)


def test_tables_rich_a_chip_with_one_border_side(family_irs: dict[str, IR]):
    """`.tag{border-left:3px solid}` (review finding 5): the chip's fill and its left side are two
    shapes, as package E's `emitInlineDecoration` draws them; its words start inside the side + padding."""
    ir, flow = _flow(family_irs)
    shapes = _overlays(ir, flow, 1, 0, "chip")
    assert len(shapes) == 2
    fill, side = shapes
    assert fill.fill["color"] == "FFF4E5" and fill.stroke is None
    assert side.fill["color"] == "D97706" and side.box.w == pytest.approx(3) and side.box.x == fill.box.x
    words = _overlays(ir, flow, 1, 0, "chipText")[0]
    assert _texts_of(words.paragraphs[0]) == ["Risk"] and words.box.x == pytest.approx(fill.box.x + 3 + 4, abs=0.3)
    assert _cell_x(flow, 1, 0)["indentPx"][0] == pytest.approx(fill.box.w + 11 * 0.2778, abs=0.3)


def test_tables_rich_stacked_text_keeps_its_left_edge(family_irs: dict[str, IR]):
    """A grid's second row that starts in column 2, and a `margin-left` block (review finding 4, the
    `marL` reading): the paragraph stays in the cell — it is stacked under the one before it — and
    carries its measured left edge on every line, so it counts nothing."""
    ir, flow = _flow(family_irs)
    for c, margin in ((1, 100), (2, 24)):
        paragraphs, extras = _slot(flow)[(1, c)]["paragraphs"], _cell_x(flow, 1, c)
        assert [len(p["lines"]) for p in paragraphs] == [1, 2] and _overlays(ir, flow, 1, c) == []
        assert extras["marginPx"][1] == margin and extras["indentPx"][1] == 0
        left = _content_left(flow, 1, c)
        assert all(line["box"]["x"] == pytest.approx(left + margin, abs=0.5) for line in paragraphs[1]["lines"])


def test_tables_rich_centred_and_right_aligned_segments(family_irs: dict[str, IR]):
    """`1,284 <svg/>` right-aligned and `Up <chip>+3</chip> pts` centred: the text the browser aligned
    together with a box after it cannot be aligned alone in the cell — it would land under the box —
    and PowerPoint's right indent (`marR`) is one PptxRender does not read, so the line is written
    left-aligned at its measured start. "pts" is centred on its own glyphs, not alone in the cell, in
    the widest box the cell allows about that centre (room for PowerPoint's wider metrics)."""
    ir, flow = _flow(family_irs)
    for c, text in ((3, "1,284"), (4, "Up")):
        paragraph = _slot(flow)[(1, c)]["paragraphs"][0]
        assert _texts_of(paragraph) == [text] and paragraph["align"] == "left", c
        extras, left = _cell_x(flow, 1, c), _content_left(flow, 1, c)
        assert extras["marginPx"] == [0]
        assert left + extras["indentPx"][0] == pytest.approx(paragraph["lines"][0]["box"]["x"], abs=0.5), c
    triangle = [e for e in ir.elements if (e.source or {}).get("svg") and e.geometry["type"] == "custom"
                and e.box.x > _content_left(flow, 1, 3)]
    line = _slot(flow)[(1, 3)]["paragraphs"][0]["lines"][0]["box"]
    assert len(triangle) == 1 and line["x"] + line["w"] <= triangle[0].box.x + 0.5
    pts = _overlays(ir, flow, 1, 4, "bandText")[0]
    chip = _overlays(ir, flow, 1, 4, "chip")[0]
    assert _texts_of(pts.paragraphs[0]) == ["pts"]
    glyphs = pts.paragraphs[0]["lines"][0]["box"]
    cell_right = _content_left(flow, 1, 4) + 210 - 16
    assert chip.box.x2 <= glyphs["x"] + 0.5
    # Package B's `glyphExtent` starts the line box at the first glyph (the space after the chip is
    # the gap's, not the text's), so the line box's centre is the glyphs' centre.
    assert pts.box.x + pts.box.w / 2 == pytest.approx(glyphs["x"] + glyphs["w"] / 2, abs=1.0)
    assert pts.box.x2 == pytest.approx(cell_right, abs=0.5), "the nearer cell edge bounds the box"


def test_tables_rich_text_around_a_block_is_three_paragraphs(family_irs: dict[str, IR]):
    """`Intro text<div>Block</div>Revenue <chip>+12%</chip> YoY` (review finding 3): only what shares
    the chip's line leaves; "Intro text", "Block" and "Revenue" stay stacked in the cell."""
    ir, flow = _flow(family_irs)
    assert [_texts_of(p) for p in _slot(flow)[(0, 4)]["paragraphs"]] == [["Intro text"], ["Block"], ["Revenue"]]
    assert [_texts_of(e.paragraphs[0]) for e in _overlays(ir, flow, 0, 4) if e.kind == "text"] == [["+12%"], ["YoY"]]


def test_tables_rich_no_inline_overlay_lies_on_a_cell_line(family_irs: dict[str, IR]):
    """The kinds review's invariant: a chip, an icon or an image that left a line never lies over the
    words that stayed on it (an out-of-flow corner badge may: the browser paints it over the text too)."""
    ir = family_irs["tables-rich"]
    svg = [e for e in ir.elements if (e.source or {}).get("svg")]
    checked = 0
    for table in ir.of_kind("table"):
        for cell in table.cells:
            lines = [Box(**line["box"]) for p in cell["paragraphs"] for line in p["lines"]]
            painted = _overlays(ir, table, cell["r"], cell["c"])
            overlays = [e.box for e in painted if e.extras["x-wpa"]["role"] in ("chip", "image", "decoration")]
            # A text box over the table is judged by its lines (its box may be wider than its words).
            overlays += [Box(**line["box"]) for e in painted
                         if e.extras["x-wpa"]["role"] in ("chipText", "bandText", "blockChipText")
                         for p in e.paragraphs for line in p["lines"]]
            overlays += [e.box for e in svg if any(_inside(e.box, line, slack=4) for line in lines)]
            for line in lines:
                for box in overlays:
                    vertical = min(line.y2, box.y2) - max(line.y, box.y)
                    horizontal = min(line.x2, box.x2) - max(line.x, box.x)
                    assert not (vertical > 0.5 * min(line.h, box.h) and horizontal > 1.0), (
                        table.name, cell["r"], cell["c"], line, box)
                    checked += 1
    assert checked > 20


def test_tables_rich_cell_lines_start_where_the_browser_drew_them(family_irs: dict[str, IR]):
    """The margins the emitter writes put every left-aligned cell line where it was measured: lines
    after a paragraph's first at `marL` (±1 px; a wrapped line has no leading space), the first at
    `marL + indent` — or, bulleted, at `marL` — within a rendered space of its line box's left."""
    ir = family_irs["tables-rich"]
    space = 12 * 0.2778 + 1.0
    checked = 0
    for table in ir.of_kind("table"):
        for cell in table.cells:
            extras = _cell_x(table, cell["r"], cell["c"])
            left = _content_left(table, cell["r"], cell["c"])
            for index, paragraph in enumerate(cell["paragraphs"]):
                if paragraph["align"] not in ("left", "justify"):
                    continue
                margin, indent = extras["marginPx"][index], extras["indentPx"][index]
                first = left + margin + (0 if paragraph["bullet"] else indent)
                box = paragraph["lines"][0]["box"]
                assert box["x"] - 1.0 <= first <= box["x"] + space, (table.name, cell["r"], cell["c"], index, first, box)
                for line in paragraph["lines"][1:]:
                    assert line["box"]["x"] == pytest.approx(left + margin, abs=1.0), (table.name, cell["r"], cell["c"])
                checked += 1
    assert checked > 60


def test_tables_rich_icon_indent_and_wrap(family_irs: dict[str, IR]):
    """An inline svg before text that wraps: the first line is indented by the icon + a space, the
    second starts at the content box's left, and the icon is a shape over the table."""
    ir, rich, _ = _rich(family_irs)
    paragraph = _slot(rich)[(2, 2)]["paragraphs"][0]
    assert len(paragraph["lines"]) == 2
    assert _cell_x(rich, 2, 2)["indentPx"][0] == pytest.approx(12 + 11 * 0.2778, abs=0.3)
    content_left = rich.box.x + 170 + 150 + 8
    assert paragraph["lines"][1]["box"]["x"] == pytest.approx(content_left, abs=0.5)
    icon = [e for e in ir.elements if (e.source or {}).get("svg") and e.box.x < content_left + 13
            and abs(e.box.y - paragraph["lines"][0]["box"]["y"]) < 14]
    assert len(icon) == 1 and icon[0].z > rich.z
    flag = _overlays(ir, rich, 2, 0, "image")
    assert len(flag) == 1 and _cell_x(rich, 2, 0)["indentPx"][0] == pytest.approx(16 + 11 * 0.2778, abs=0.3)


def test_tables_rich_bullets_are_paragraphs(family_irs: dict[str, IR]):
    _, rich, _ = _rich(family_irs)
    paragraphs = _slot(rich)[(1, 2)]["paragraphs"]
    assert [_texts_of(p) for p in paragraphs] == [["Sandbox"], ["Talent visas"], ["Compute"]]
    for paragraph in paragraphs:
        assert paragraph["bullet"]["type"] == "char" and paragraph["bullet"]["indentPx"] == pytest.approx(14)
    assert [p["spaceBeforePx"] for p in paragraphs] == [0, 0, 0]
    # Text at the item's content edge (marL 14), the bullet back at the list's edge (indent −14).
    assert _cell_x(rich, 1, 2)["marginPx"] == [14] * 3
    assert _cell_x(rich, 1, 2)["indentPx"] == [-14] * 3


def test_tables_rich_elements_match_the_expect_file(family_irs: dict[str, IR]):
    """The kinds review's finding 6: the `elements` block is asserted, not only the kinds. Every width
    derives from the CSS (fixed layout; the separate table's spacing and border sit in its outer columns)."""
    expect = json.loads((WP1_FIXTURES / "tables-rich.expect.json").read_text(encoding="utf-8"))
    ir = family_irs["tables-rich"]
    for wanted in expect["elements"]:
        table = _named(ir, wanted["name"])
        assert table.kind == wanted["kind"]
        assert (table.rows, table.cols, len(table.cells)) == (wanted["rows"], wanted["cols"], wanted["cells"])
        assert table.colWidthsPx == pytest.approx(wanted["colWidthsPx"], abs=0.01), wanted["name"]
    assert sorted(t.name for t in ir.of_kind("table")) == sorted(w["name"] for w in expect["elements"])


def test_tables_rich_band_keeps_leftmost_in_flow(family_irs: dict[str, IR]):
    """`label | value` in a flex row: the label stays in the cell, the value is placed over it."""
    ir, rich, _ = _rich(family_irs)
    assert [_texts_of(p) for p in _slot(rich)[(2, 3)]["paragraphs"]] == [["Label"]]
    band = _overlays(ir, rich, 2, 3, "bandText")
    assert len(band) == 1 and _texts_of(band[0].paragraphs[0]) == ["Value"]
    assert band[0].box.x2 == pytest.approx(rich.box.x + 170 + 150 + 260 + 210 - 8, abs=1.0)
    assert _cell_x(rich, 2, 3)["bands"] == 1


def test_tables_rich_overlays_follow_the_table_in_paint_order_and_leave_the_group(
        family_irs: dict[str, IR], sample_manifest: Manifest, sample_dir: Path, tmp_path: Path):
    ir, rich, separate = _rich(family_irs)
    for table in (rich, _named(ir, "flow"), separate):
        for element in _overlays(ir, table):
            tag = element.extras["x-wpa"]
            assert element.group is None
            if tag["role"] in ("surface", "cellGradient", "caption"):
                assert element.z < table.z, (element.name, tag)
            else:
                assert tag["cell"] is not None and element.z > table.z, (element.name, tag)
    grouped = tmp_path / "grouped.html"
    grouped.write_text(
        '<!doctype html><html><body style="margin:0;font-family:Arial">'
        '<div data-pptx="group" data-name="g" style="position:absolute;left:40px;top:40px">'
        '<table style="border-collapse:collapse"><tr><td style="padding:4px">'
        '<span style="display:inline-block;padding:2px 6px;background:#1E9E5A;color:#fff">Chip</span>'
        '</td></tr></table></div></body></html>', encoding="utf-8")
    small = extract_html(grouped, sample_manifest, "layout-05", sample_dir / "assets", slide_id="grouped")
    table = small.of_kind("table")[0]
    assert table.group is not None and table.extras["x-wpa"]["grouped"] is True
    assert all(e.group is None for e in small.elements if (e.extras or {}).get("x-wpa", {}).get("role"))
    assert [d for d in small.diagnostics if "leave the group" in d.message and d.level == "info"]


def test_tables_rich_collapsed_row_border(family_irs: dict[str, IR]):
    """Row rules reach the cells (Chromium reports 0 px on the cell under a `tr` border — branch B of
    brief §4.4), each shared grid line is written once, on the upper / left cell."""
    _, rich, _ = _rich(family_irs)
    cells = _slot(rich)
    for c in range(6):
        assert cells[(0, c)]["borders"]["bottom"] == {"width": 2, "color": "1A9AFB", "dash": "solid"}
    dashed = [cells[(4, c)]["borders"]["bottom"] for c in range(1, 6)]
    assert all(b["width"] == 2 and b["color"] == "999999" and isinstance(b["dash"], list) for b in dashed)
    assert cells[(3, 0)]["borders"]["bottom"]["dash"] == dashed[0]["dash"], "the rowspan cell ends on row 4"
    assert all(cells[(8, c)]["borders"]["bottom"] == {"width": 2, "color": "0B2545", "dash": "solid"}
               for c in range(6)), "the tfoot's top rule, on the last body row's bottom"
    for (r, c), cell in cells.items():
        if r > 0:
            assert cell["borders"]["top"] is None, (r, c)
        if c > 0:
            assert cell["borders"]["left"] is None, (r, c)


def test_tables_rich_separate_mode_slots(family_irs: dict[str, IR]):
    """border-spacing 2 px, a 1 px table border and 1 px cell borders: the gaps between the bordered
    cells are spacer rows and columns (`spacerGrid`, rendercheck 2026-09-29), each grid line half a
    border inside its cell, so the slot is the other half; the table's own border, padding and outer
    spacing belong to its surface, which carries radius, fill and stroke."""
    ir, _, separate = _rich(family_irs)
    extras = separate.extras["x-wpa"]
    assert extras["collapse"] is False and extras["spacingPx"] == {"h": 2, "v": 2}
    assert extras["spacers"] == {"rows": [1, 3], "cols": [1, 3]}
    for r, c in ((0, 0), (2, 2), (4, 4)):
        assert _cell_x(separate, r, c)["slot"] == {"t": 0.5, "r": 0.5, "b": 0.5, "l": 0.5}
    assert _cell_x(separate, 1, 1)["spacer"] is True and _cell_x(separate, 1, 1)["slot"] == {"t": 0, "r": 0, "b": 0, "l": 0}
    assert separate.rowHeightsPx[1::2] == [3, 3] and separate.colWidthsPx[1::2] == [3, 3]
    surface = _named(ir, "separate surface")
    assert surface.geometry["type"] == "roundRect" and surface.fill["color"] == "F7F9FC"
    assert surface.stroke["color"] == "C9D2DF" and surface.stroke["width"] == 1
    assert separate.box.x == pytest.approx(surface.box.x + 0.5 + 2 + 0.5), "table border centre + spacing + half a cell border"
    assert [d for d in ir.diagnostics if d.message.startswith("border-spacing 2 px: the gaps between painted cells")]


def test_tables_rich_tfoot_first_is_rendered_last(family_irs: dict[str, IR]):
    _, rich, _ = _rich(family_irs)
    cells = _slot(rich)
    assert rich.rows == 10
    assert _texts_of(cells[(9, 0)]["paragraphs"][0]) == ["Total"]
    assert _texts_of(cells[(1, 0)]["paragraphs"][0]) == ["Singapore"]


def test_tables_rich_caption_is_text(family_irs: dict[str, IR]):
    ir, rich, _ = _rich(family_irs)
    caption = [e for e in _overlays(ir, rich) if e.extras["x-wpa"]["role"] == "caption"]
    assert len(caption) == 1 and caption[0].kind == "text" and caption[0].z < rich.z
    assert _texts_of(caption[0].paragraphs[0]) == ["Programme scorecard"]
    assert caption[0].box.y2 <= rich.box.y


def test_tables_say_which_shadow_layers_they_drop(sample_manifest: Manifest, sample_dir: Path, tmp_path: Path):
    """A chip in a cell and the table's own surface keep one outer shadow, as every shape does, and
    say what they dropped with G-1's warn (plan §16 #19) — never silently."""
    html = tmp_path / "shadows.html"
    html.write_text(
        '<!doctype html><html><body style="margin:0;font-family:Arial">'
        '<table data-name="t" style="position:absolute;left:40px;top:40px;border-collapse:separate;'
        'background:#fff;box-shadow:0 1px 2px #000,0 4px 8px #000,0 8px 16px #000"><tr><td style="padding:6px">'
        '<span style="display:inline-block;padding:2px 6px;background:#eee;'
        'box-shadow:0 1px 2px #000,0 2px 4px #000">Chip</span> after'
        '</td></tr></table></body></html>', encoding="utf-8")
    ir = extract_html(html, sample_manifest, "layout-05", sample_dir / "assets", slide_id="shadows")
    warns = sorted(d.message for d in ir.diagnostics if d.level == "warn" and d.message.startswith("box-shadow:"))
    assert warns == ["box-shadow: 1 extra outer shadow dropped; PowerPoint draws one outer shadow per shape",
                     "box-shadow: 2 extra outer shadows dropped; PowerPoint draws one outer shadow per shape"], warns
    chip = next(e for e in ir.elements if e.name == "t r0c0 chip")
    surface = next(e for e in ir.elements if e.name == "t surface")
    assert chip.shadow and surface.shadow


#: One table for the rebase onto E, B and F: a cell that clips with an ellipsis, glued runs in a cell
#: and in a band text, gradient text in a cell, a line an inner box hides, and a chip cell whose tail
#: the clip cuts.
CELL_WAVE2_TABLE = (
    '<table data-name="t" style="position:absolute;left:40px;top:40px;border-collapse:collapse;'
    'table-layout:fixed;width:420px"><tr>'
    '<td style="width:140px;padding:4px 8px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">'
    'A very long label that cannot fit in the column</td>'
    '<td style="width:140px;padding:4px 8px"><span style="margin-right:10px">Label</span><span>Value</span></td>'
    '<td style="width:140px;padding:4px 8px"><div style="display:flex;justify-content:space-between">'
    '<span>Name</span><span><i style="margin-right:6px">x</i>Right</span></div></td></tr><tr>'
    '<td style="padding:4px 8px"><span style="background:linear-gradient(90deg,#f00,#00f);'
    '-webkit-background-clip:text;background-clip:text;color:transparent">Gradient words</span></td>'
    '<td style="padding:4px 8px;overflow:hidden;height:20px"><div style="height:20px;overflow:hidden">'
    'First line<br>Second line hidden</div></td>'
    '<td style="padding:4px 8px;overflow:hidden;white-space:nowrap"><span style="display:inline-block;'
    'padding:1px 4px;background:#eee">Chip</span> then a long tail of words that is cut</td></tr></table>'
)


@pytest.fixture(scope="module")
def cell_wave2_ir(sample_manifest: Manifest, sample_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> IR:
    html = tmp_path_factory.mktemp("cells") / "cells.html"
    html.write_text('<!doctype html><html><head><meta charset="utf-8"></head>'
                    '<body style="margin:0;font-family:Arial;font-size:14px">' + CELL_WAVE2_TABLE + '</body></html>',
                    encoding="utf-8")
    return extract_html(html, sample_manifest, "layout-05", sample_dir / "assets", slide_id="cells")


def test_tables_cell_text_the_clip_hides_is_reported_in_fs_words(cell_wave2_ir: IR):
    """Plan §16 #11: a cell's paragraphs are measured under its content box, and what that hides is
    package F's `trimClipped` finding, attached to the table: the ellipsis the browser drew is kept
    (`text-overflow` on the cell), a line an inner box hides is gone, a chip cell's cut tail too; the
    cell's `x-wpa` entry says what was hidden, as `x-wpf-clipped` does on a text element."""
    [table] = cell_wave2_ir.of_kind("table")
    runs = {(c["r"], c["c"]): ["".join(r["text"] for r in line["runs"]) for p in c["paragraphs"] for line in p["lines"]]
            for c in table.cells}
    assert runs[(0, 0)] == ["A very long label th…"]
    assert runs[(1, 1)] == ["First line"]
    assert runs[(1, 2)][0].startswith("then a long tail") and not runs[(1, 2)][0].endswith("cut")
    cells = table.extras["x-wpa"]["cells"]
    assert cells["0:0"]["clipped"]["ellipsis"] is True and cells["0:0"]["clipped"]["hiddenChars"] > 0
    assert cells["1:1"]["clipped"]["hiddenLines"] == 1
    assert cells["1:2"]["clipped"]["hiddenChars"] > 0 and "clipped" not in cells["0:1"]
    errors = [d for d in cell_wave2_ir.diagnostics if d.level == "error"]
    assert len(errors) == 3 and {d.elementId for d in errors} == {table.id}, errors
    assert all(d.message.startswith("text is clipped by an overflow:hidden ancestor: ") for d in errors)
    assert "(the browser's ellipsis was kept)" in errors[0].message
    chip_text = next(e for e in cell_wave2_ir.elements if e.name == "t r1c2 chipText")
    assert chip_text.clip is None and "x-wpf-clipped" not in chip_text.extras, "the chip itself is not cut"


def test_tables_cell_runs_keep_bs_run_boxes_and_carried_gaps(cell_wave2_ir: IR):
    """Plan §16 #11, #25 (r1a): package B's run building reaches the cell walk — a gap a margin
    leaves between a cell's texts is carried by a space sized to it (its advance + letter spacing =
    the gap), whose `[left, right]` is the gap itself (in the cell's `x-wpa` entry); a text record
    over the table (a band text) carries it the same way in `x-run-boxes`. Nothing is glued, so no
    `glued runs:` warn."""
    [table] = cell_wave2_ir.of_kind("table")
    cell = next(c for c in table.cells if (c["r"], c["c"]) == (0, 1))
    [[[label, space, value]]] = table.extras["x-wpa"]["cells"]["0:1"]["runBoxes"]
    assert value[0] - label[1] == pytest.approx(10.0, abs=0.5)
    assert (space[0], space[1]) == (label[1], value[0]), "the space run spans the gap"
    runs = cell["paragraphs"][0]["lines"][0]["runs"]
    assert [r["text"] for r in runs] == ["Label", " ", "Value"]
    # 14 px Arial: a space advances 3.89 px, so it is written 10 - 3.89 px wider.
    assert runs[1]["letterSpacingPx"] == pytest.approx(10.0 - 14 * 569 / 2048, abs=0.1)
    band = next(e for e in cell_wave2_ir.elements if e.name == "t r0c2 bandText")
    [[[x, gap, right]]] = band.extras["x-run-boxes"]
    assert right[0] - x[1] == pytest.approx(6.0, abs=0.5) and band.box.x == pytest.approx(x[0], abs=0.5)
    assert [r["text"] for r in _all_runs(band)] == ["x", " ", "Right"]
    assert not [d for d in cell_wave2_ir.diagnostics if d.message.startswith("glued runs:")]
    chip_text = next(e for e in cell_wave2_ir.elements if e.name == "t r1c2 chipText")
    assert len(chip_text.extras["x-run-boxes"][0][0]) == 1


def test_tables_an_unpainted_inline_box_with_geometry_leaves_the_cell(sample_manifest: Manifest, sample_dir: Path,
                                                                      tmp_path: Path):
    """Package B's R3 read inside a cell (master brief §11 as amended by B): an atomic inline box
    with horizontal geometry of its own that shares its paragraph with other text is an element of
    its own, painted or not — `<span style="display:inline-block;margin-right:3px">▼</span>≤ 10
    days` (an app slide's target column, a survey collision at F's merge) or a fixed-width label.
    Its words leave as a chipText with no shape, the text after it stays in the cell where the
    browser drew it, and nothing is glued. A flush box, or one alone in its cell, stays inline."""
    html = tmp_path / "boxes.html"
    html.write_text(
        '<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;font-family:Arial;'
        'font-size:14px"><table data-name="t" style="position:absolute;left:40px;top:40px;'
        'border-collapse:collapse"><tr>'
        '<td style="padding:4px 8px"><span style="display:inline-block;margin-right:3px;color:#2DB757;'
        'font-weight:700;font-size:12px">&#9660;</span>&le; 10 days</td>'
        '<td style="padding:4px 8px"><span style="display:inline-block;width:60px">Label</span>value</td>'
        '<td style="padding:4px 8px">Auxi<span style="display:inline-block">Studio</span></td>'
        '<td style="padding:4px 8px"><span style="display:inline-block;padding:0 6px">Alone</span></td>'
        '</tr></table></body></html>', encoding="utf-8")
    ir = extract_html(html, sample_manifest, "layout-05", sample_dir / "assets", slide_id="boxes")
    [table] = ir.of_kind("table")
    texts = {c["c"]: ["".join(r["text"] for r in line["runs"]) for p in c["paragraphs"] for line in p["lines"]]
             for c in table.cells}
    assert texts == {0: ["≤ 10 days"], 1: ["value"], 2: ["AuxiStudio"], 3: ["Alone"]}, texts
    boxes = {e.name: _all_runs(e)[0]["text"] for e in _texts(ir)}
    assert boxes == {"t r0c0 chipText": "▼", "t r0c1 chipText": "Label"}, boxes
    assert not _shapes(ir), "an unpainted box draws nothing"
    for c in (0, 1):
        line = _slot(table)[(0, c)]["paragraphs"][0]["lines"][0]["box"]
        assert _content_left(table, 0, c) + _cell_x(table, 0, c)["indentPx"][0] == pytest.approx(line["x"], abs=0.5)
        assert _named(ir, f"t r0c{c} chipText").box.x2 <= line["x"] + 0.5, "the words after the box follow it"
    assert not [d for d in ir.diagnostics if d.message.startswith("glued runs:")]


def test_tables_gradient_text_in_a_cell_is_one_colour(cell_wave2_ir: IR):
    """Package E's run fills are lifted by `flushText`; a cell run carries none (`write_cell` writes
    no `a:gradFill`), so the cell walk drops `_fill` and says so — no private key reaches the IR."""
    [table] = cell_wave2_ir.of_kind("table")
    keys = {key for cell in table.cells for p in cell["paragraphs"] for line in p["lines"] for run in line["runs"]
            for key in run}
    assert not [key for key in keys if key.startswith("_")], keys
    infos = [d.message for d in cell_wave2_ir.diagnostics if d.level == "info" and "gradient text" in d.message]
    assert infos == ["cell r1c0: gradient text is written in one colour (a cell run carries no gradient)"]


def test_tables_rich_extraction_is_deterministic(sample_manifest: Manifest, sample_dir: Path):
    html = WP1_FIXTURES / "tables-rich.html"
    first = extract_html(html, sample_manifest, "layout-05", sample_dir / "assets", slide_id="determinism")
    second = extract_html(html, sample_manifest, "layout-05", sample_dir / "assets", slide_id="determinism")
    assert first.dumps() == second.dumps()


# ------------------------------------------------------------------------------------- charts


def test_authored_charts_keep_their_spec_and_plot_rect(family_irs: dict[str, IR]):
    charts = family_irs["charts"].of_kind("chart")
    assert [c.origin for c in charts] == ["authored", "authored"]
    stacked = charts[0]
    assert stacked.spec["type"] == "column_stacked"
    assert stacked.plotRect is not None
    # plotArea is a fraction of the frame: x 0.1, w 0.8 of a 560 px box at x = 96.
    assert stacked.plotRect.x == pytest.approx(96 + 56)
    assert stacked.plotRect.w == pytest.approx(448)
    assert charts[1].plotRect is None, "no plotArea means no measured plot rect"
    assert not [e for e in family_irs["charts"].elements if e.kind == "text"], "chart children are ignored"


# ------------------------------------------------------------------------------- sample slides

CASES = Path(__file__).resolve().parents[1] / "fixtures" / "extract_cases"


@pytest.fixture(scope="module")
def sample_irs(sample_manifest: Manifest, sample_dir: Path) -> list[IR]:
    """The synthetic sample project's two slides, on the layouts its project.json names."""
    return [
        extract_html(sample_dir / f"slide-{n:02d}.html", sample_manifest, layout, sample_dir / "assets",
                     slide_id=f"sample-{n}", title=f"slide {n}")
        for n, layout in ((1, "layout-06"), (2, "layout-01"))
    ]


@pytest.fixture(scope="module")
def cards_ir(sample_manifest: Manifest, sample_dir: Path) -> IR:
    """`extract_cases/cards.html`: cards around an svg, headings with chips, tiles with mixed borders."""
    return extract_html(CASES / "cards.html", sample_manifest, "layout-07", sample_dir / "assets",
                        slide_id="cards", title="cards")


def _by_path(ir: IR, fragment: str) -> list[Element]:
    return [e for e in _texts(ir) if fragment in e.source.get("path", "")]


def test_sample_slides_are_native_except_for_svg(sample_irs: list[IR]):
    """Coverage row of the acceptance gate: nothing on these slides is a picture but the SVGs."""
    for ir in sample_irs:
        assert ir.validate() == []
        for raster in ir.of_kind("raster"):
            assert raster.source.get("svg") or raster.source.get("tag") == "svg", (
                f"{ir.slide.id}/{raster.id} is a non-SVG raster: {raster.reason}"
            )
        assert ir.counts().get("text", 0) > 0 and ir.counts().get("shape", 0) > 0


def test_a_kpi_card_is_one_text_element_of_three_paragraphs(sample_irs: list[IR]):
    """Master brief §11's counting rule, as amended by WP-B (fidelity plan D14): the three stacked
    blocks of a `.kpi` card (figure, label, note) are one element of three paragraphs."""
    ir = sample_irs[0]
    kpis = [e for e in _texts(ir) if e.source.get("path", "").endswith(tuple(
        f"div.kpi:nth-child({n})" for n in (3, 4, 5)))]

    assert len(kpis) == 3, [e.source.get("path") for e in _texts(ir)]
    for kpi in kpis:
        assert len(kpi.paragraphs) == 3
        assert len(kpi.paragraphs[0]["lines"][0]["runs"]) == 1


def test_a_kpi_left_border_is_a_three_pixel_shape_at_x_48(sample_irs: list[IR]):
    """`.kpi{left:48px; top:200px; height:96px; border-left:3px solid #1A9AFA}`: the named geometry case."""
    ir = sample_irs[0]
    border = next(s for s in _shapes(ir) if s.box.x == 48.0 and s.box.w == 3.0)
    assert (border.box.y, border.box.h) == (200.0, 96.0)
    assert border.fill == {"type": "solid", "color": "1A9AFA", "alpha": 1}
    assert border.stroke is None, "a single-side border is a filled rectangle, not a stroke"
    assert len([s for s in _shapes(ir) if s.box.w == 3.0]) == 3, "one per KPI card"


def test_a_card_is_two_paragraphs_around_its_svg(cards_ir: IR):
    """`.card` -> one text element of two paragraphs, then the svg: the run stops at a non-text
    sibling (master brief §11 as amended by WP-B, D14)."""
    cards = [e for e in _texts(cards_ir) if e.source.get("path", "").endswith(("div.card:nth-child(1)",
                                                                               "div.card:nth-child(2)"))]
    assert len(cards) == 2
    for card in cards:
        assert len(card.paragraphs) == 2


def test_chips_are_shapes_and_tiles_with_differing_sides_are_four_rectangles(cards_ir: IR):
    """Three inline chips become three shapes; each `.tile` border is four thin rectangles (master
    brief §11 as amended by WP-B, D14: each chip is an element over its own shape)."""
    chips = [s for s in _shapes(cards_ir) if (s.fill or {}).get("color") == "F5C518"
             and 10 < s.box.h < 20 and not (s.source or {}).get("svg")]
    assert len(chips) == 3

    tiles = [s for s in _shapes(cards_ir) if "div.tile" in s.source.get("path", "")
             and not (s.source or {}).get("svg")]
    assert len(tiles) == 16, "four tiles x four border sides"
    # `.tile` has a 2 px blue top over a 1 px grey box: the sides differ, so they cannot be a stroke.
    assert {round(s.box.h, 1) for s in tiles if s.box.w > 100} == {2.0, 1.0}


def test_tiles_are_two_text_elements_around_the_svg(cards_ir: IR):
    """`.tile` -> two text elements around the in-flow svg, of 2 and 1 paragraphs (master brief §11
    as amended by WP-B, D14: the svg is an intervening non-text sibling)."""
    for index in range(6, 10):
        tile_texts = _by_path(cards_ir, f"div.tile:nth-child({index})")
        assert len(tile_texts) == 2, f"tile {index}: {[t.source for t in tile_texts]}"
        assert [len(t.paragraphs) for t in tile_texts] == [2, 1]


# ------------------------------------------------------------------ WP-B: side by side, seams


def test_heading_and_standfirst_stay_one_element_beside_their_chip(cards_ir: IR):
    """R1/R3: the chip leaves `.ph` (its margin glues it to the last word) but, sitting *beside* the
    heading, it does not come between the heading and the `.psub` under it: they still stack, one
    element of two paragraphs; the chip is a third."""
    heading = [e for e in _texts(cards_ir)
               if _all_runs(e) and _all_runs(e)[0]["text"].startswith("Partner revenue grew")]
    assert len(heading) == 1 and len(heading[0].paragraphs) == 2
    assert "".join(r["text"] for r in heading[0].paragraphs[1]["lines"][0]["runs"]).startswith("Partners now")
    last = heading[0].paragraphs[0]["lines"][-1]["box"]
    chip = [e for e in _texts(cards_ir) if "".join(r["text"] for r in _all_runs(e)) == "SAMPLE"
            and last["y"] <= e.box.cy <= last["y"] + last["h"]]
    assert len(chip) == 1 and chip[0].box.x >= last["x"] + last["w"], "beside the heading's last line"


def test_a_chip_wrapped_onto_its_own_line_ends_the_heading_s_element(cards_ir: IR):
    """R3: a chip too wide for the heading's last line wraps onto a line of its own, between `.ph` and
    `.psub`. A detached box *below* the pending text is an intervening sibling (§11), so the heading,
    the chip and the `.psub` are three elements, in that vertical order."""
    heading = [e for e in _texts(cards_ir)
               if _all_runs(e) and _all_runs(e)[0]["text"].startswith("Growth compounds")]
    standfirst = [e for e in _texts(cards_ir)
                  if _all_runs(e) and _all_runs(e)[0]["text"].startswith("A chip too wide")]
    chip = [e for e in _texts(cards_ir) if "".join(r["text"] for r in _all_runs(e)) == "SYNTHETIC PREVIEW ONLY"]
    assert len(heading) == len(standfirst) == len(chip) == 1
    assert len(heading[0].paragraphs) == 1 and len(standfirst[0].paragraphs) == 1
    assert heading[0].box.y2 <= chip[0].box.y + 1 and chip[0].box.y2 <= standfirst[0].box.y + 1


def _extract_snippet(tmp_path: Path, body: str, sample_manifest: Manifest, sample_dir: Path) -> IR:
    html = tmp_path / "snippet.html"
    html.write_text(
        "<!doctype html><html><head><meta charset='utf-8'><style>html,body{margin:0;width:1280px;"
        "height:720px;overflow:hidden;font-family:Arial;font-size:16px}.accent{color:#1A9AFA;"
        "font-weight:bold}</style></head><body>" + body + "</body></html>", encoding="utf-8")
    return extract_html(html, sample_manifest, "layout-05", sample_dir / "assets", slide_id="snippet")


def _glued_runs(ir: IR) -> list[str]:
    return [d.message for d in ir.diagnostics if d.level == "warn" and d.message.startswith("glued runs:")]


#: A 16 px Arial space advances 0.2778 em (the `hmtx` of arial.ttf: 569 / 2048).
ARIAL_16_SPACE = 16 * 569 / 2048


def test_a_padded_inline_span_flush_against_its_words_keeps_its_gaps_on_sized_spaces(
        tmp_path: Path, sample_manifest: Manifest, sample_dir: Path):
    """R6 as changed by r1a (plan §16 #25): a non-atomic span stays inline, and the padding the
    browser put between its text and the words around it — geometry no run can carry — is carried
    by a space inserted at each seam, sized to the gap by its letter spacing (`spc` in the file).
    Its run box is the gap itself, so the text row reads a textual gap, not a join; nothing is glued."""
    ir = _extract_snippet(tmp_path, '<p style="position:absolute;left:40px;top:40px">and<span '
                          'style="padding:0 6px;background:#eee">tight</span>and</p>', sample_manifest, sample_dir)
    [text] = _texts(ir)
    runs = _all_runs(text)
    assert [r["text"] for r in runs] == ["and", " ", "tight", " ", "and"]
    boxes = text.extras["x-run-boxes"][0][0]
    for k in (1, 3):
        gap = boxes[k + 1][0] - boxes[k - 1][1]
        assert gap == pytest.approx(6.0, abs=0.6), "the padding is the gap between the texts"
        assert (boxes[k][0], boxes[k][1]) == (boxes[k - 1][1], boxes[k + 1][0])
        assert ARIAL_16_SPACE + runs[k]["letterSpacingPx"] == pytest.approx(gap, abs=0.05)
        assert runs[k]["underline"] is False and runs[k]["baseline"] == "normal"
    assert _glued_runs(ir) == []


def test_an_empty_box_with_a_margin_between_two_letters_is_carried_by_a_space(tmp_path: Path, sample_manifest: Manifest,
                                                                              sample_dir: Path):
    ir = _extract_snippet(tmp_path, '<p style="position:absolute;left:40px;top:40px">a<span '
                          'style="margin:0 8px"></span>b</p>', sample_manifest, sample_dir)
    [text] = _texts(ir)
    runs = _all_runs(text)
    assert [r["text"] for r in runs] == ["a", " ", "b"]
    assert ARIAL_16_SPACE + runs[1]["letterSpacingPx"] == pytest.approx(16.0, abs=0.1), "two 8 px margins"
    assert _glued_runs(ir) == []


def test_a_gap_beside_a_space_goes_onto_that_space(tmp_path: Path, sample_manifest: Manifest, sample_dir: Path):
    """r1a: with a space beside the padding (rows `padded-inline`) the space itself takes the gap —
    split off its run, letter-spaced by the padding — rather than a second space being inserted:
    the text still reads `and spaced and`, and the words after the span start where the browser
    put them instead of 12 px early."""
    ir = _extract_snippet(tmp_path, '<p style="position:absolute;left:40px;top:40px">and <span '
                          'style="padding:0 12px;background:#eee">spaced</span> and</p>', sample_manifest, sample_dir)
    [text] = _texts(ir)
    runs = _all_runs(text)
    assert [r["text"] for r in runs] == ["and", " ", "spaced", " ", "and"]
    assert [round(runs[k]["letterSpacingPx"], 1) for k in (1, 3)] == [12.0, 12.0]
    boxes = text.extras["x-run-boxes"][0][0]
    assert boxes[1][1] == boxes[2][0] and boxes[3][0] == boxes[2][1], "each space's box ends where the next text starts"
    assert _glued_runs(ir) == []


def test_an_in_flow_blocks_opacity_is_on_its_fill_its_text_and_its_chip(tmp_path: Path, sample_manifest: Manifest,
                                                                         sample_dir: Path):
    """r1a (plan §16 #25): a text block walked as a paragraph of its container gets the container's
    context, so its own `opacity` was lost on everything it paints. Its fill now carries it (element
    opacity), its text (run alpha — the element may hold other blocks' paragraphs) and the chip it
    detaches (element opacity, through E's `pseudoContext`); the block beside it keeps full alpha.
    Per-shape alpha is the walk's approximation of group opacity (as for any element with opacity):
    exact where nothing of the block paints over its own fill."""
    ir = _extract_snippet(tmp_path, '<div style="position:absolute;left:40px;top:40px;width:400px">'
                          '<div data-name="half" style="opacity:.5;background:#0B2545;color:#fff;padding:4px">'
                          'Half <span style="display:inline-block;padding:0 6px;background:#FFE600;color:#000">'
                          'CHIP</span> text</div><div>Whole</div></div>', sample_manifest, sample_dir)
    fill = next(e for e in ir.elements if e.kind == "shape" and e.name == "half")
    chip_fill = next(e for e in ir.elements if e.kind == "shape" and e.name != "half")
    assert fill.opacity == pytest.approx(0.5) and chip_fill.opacity == pytest.approx(0.5)
    texts = {"".join(r["text"] for r in _all_runs(e)).strip(): e for e in _texts(ir)}
    assert texts["CHIP"].opacity == pytest.approx(0.5)
    assert [r["alpha"] for r in _all_runs(texts["Half"])] == [pytest.approx(0.5)]
    whole = next(e for e in _texts(ir) if "Whole" in "".join(r["text"] for r in _all_runs(e)))
    assert [r["alpha"] for r in _all_runs(whole) if r["text"].strip() == "Whole"] == [pytest.approx(1.0)]


def test_an_inline_boxs_opacity_is_on_its_runs(tmp_path: Path, sample_manifest: Manifest, sample_dir: Path):
    """r1a: `opacity` on an inline box inside a paragraph fades its text as the browser does (the
    alpha of its runs), and not the text beside it; a `display: contents` box's opacity paints
    nothing (measured in Edge), so it fades nothing."""
    ir = _extract_snippet(tmp_path, '<p style="position:absolute;left:40px;top:40px">plain <span '
                          'style="opacity:.5">half <b style="opacity:.5">quarter</b></span> <span '
                          'style="display:contents;opacity:.2">whole</span></p>', sample_manifest, sample_dir)
    [text] = _texts(ir)
    alphas = [(r["text"].strip(), round(r["alpha"], 3)) for r in _all_runs(text) if r["text"].strip()]
    assert alphas == [("plain", 1.0), ("half", 0.5), ("quarter", 0.25), ("whole", 1.0)], alphas


def test_a_style_change_inside_a_word_is_not_a_seam(tmp_path: Path, sample_manifest: Manifest, sample_dir: Path):
    ir = _extract_snippet(tmp_path, '<p style="position:absolute;left:40px;top:40px">Auxi<span '
                          'class="accent">Studio</span></p>', sample_manifest, sample_dir)
    [text] = _texts(ir)
    assert [r["text"] for r in _all_runs(text)] == ["Auxi", "Studio"]
    assert [d for d in ir.diagnostics if d.level in ("warn", "error")] == []


def test_a_fixed_width_label_its_text_overflows_still_leaves_its_line(tmp_path: Path, sample_manifest: Manifest,
                                                                       sample_dir: Path):
    """R3 (a): an 80 px label column whose text is wider than 80 px has geometry of its own — the
    column beside it starts at the label's edge, not at the end of its text."""
    ir = _extract_snippet(tmp_path, '<div style="position:absolute;left:40px;top:40px;width:400px">'
                          '<span style="display:inline-block;width:80px;font-size:12px;letter-spacing:1px">'
                          'RECOMMENDATION</span><span style="display:inline-block;width:300px;font-size:12px">'
                          'Prioritise segments in the upper-right quadrant and convert volume.</span></div>',
                          sample_manifest, sample_dir)
    texts = sorted(_texts(ir), key=lambda e: e.box.x)
    assert len(texts) == 2 and "".join(r["text"] for r in _all_runs(texts[0])) == "RECOMMENDATION"
    assert texts[0].source["path"].endswith("> span:nth-child(1)"), "walked as its own box, not a segment of the row"


def test_a_heading_does_not_merge_across_a_box_below_it(tmp_path: Path, sample_manifest: Manifest, sample_dir: Path):
    """R3 with §11's 'no intervening non-text sibling': a header, then rows that each start with a
    padded chip. The first chip lies below the header, so the header's run ends there — otherwise
    the header would merge with the first row's text across the chip, and each row's text with the
    next across the next chip."""
    rows = "".join(
        f'<div style="margin-top:6px"><span style="display:inline-block;padding:0 4px;background:#EEEEEE">'
        f'{chip}</span> {text}</div>'
        for chip, text in (("NEW", "Launch the programme"), ("NOW", "Scale what works"), ("NEXT", "Retire the rest")))
    ir = _extract_snippet(tmp_path, '<div style="position:absolute;left:40px;top:40px;width:400px;font-size:12px">'
                          '<div style="font-weight:bold">From volume to value</div>' + rows + '</div>',
                          sample_manifest, sample_dir)
    texts = {"".join(r["text"] for r in _all_runs(e)): e for e in _texts(ir)}
    assert set(texts) == {"From volume to value", "NEW", "Launch the programme", "NOW", "Scale what works",
                          "NEXT", "Retire the rest"}
    assert all(len(e.paragraphs) == 1 for e in texts.values())


def _texts_by_text(ir: IR) -> dict[str, Element]:
    return {" | ".join("".join(r["text"] for line in p["lines"] for r in line["runs"]) for p in e.paragraphs): e
            for e in _texts(ir)}


def test_a_relatively_positioned_block_stays_between_its_neighbours(tmp_path: Path, sample_manifest: Manifest,
                                                                     sample_dir: Path):
    """B1 critique #9: a `position: relative` block is walked in the positioned layer, after the
    flow, but it sits between its DOM neighbours on the page — the two around it do not merge."""
    ir = _extract_snippet(tmp_path, '<div style="position:absolute;left:40px;top:40px;width:300px">'
                          '<div>First block</div><div style="position:relative;left:4px">Second, relative</div>'
                          '<div>Third block</div></div>', sample_manifest, sample_dir)
    assert set(_texts_by_text(ir)) == {"First block", "Second, relative", "Third block"}


def test_flex_order_and_grid_spans_do_not_stack_items_across_the_layout(tmp_path: Path, sample_manifest: Manifest,
                                                                         sample_dir: Path):
    """B1 critique #9: DOM neighbours that `order` separates on the page, and a header spanning
    three grid columns over the first cell, are not stacks; a grid column's cells still are."""
    ir = _extract_snippet(
        tmp_path,
        '<div style="position:absolute;left:40px;top:40px;width:300px;display:flex;flex-direction:column">'
        '<div>Alpha</div><div style="order:2">Beta</div><div style="order:1">Gamma</div></div>'
        '<div style="position:absolute;left:40px;top:200px;width:600px;display:grid;'
        'grid-template-columns:repeat(3,1fr)"><div style="grid-column:1/4">Spanning header</div>'
        '<div>Cell one</div><div>Cell two</div><div>Cell three</div></div>'
        '<div style="position:absolute;left:40px;top:400px;width:600px;display:grid;'
        'grid-template-columns:200px 200px;grid-auto-flow:column;grid-template-rows:auto auto">'
        '<div>Label one</div><div>Value one</div><div>Label two</div><div>Value two</div></div>',
        sample_manifest, sample_dir)
    texts = set(_texts_by_text(ir))
    assert {"Alpha", "Beta", "Gamma", "Spanning header", "Cell one"} <= texts
    assert {"Label one | Value one", "Label two | Value two"} <= texts, "a grid column's cells stack"


def test_a_run_hugs_only_what_sits_beside_it(tmp_path: Path, sample_manifest: Manifest, sample_dir: Path):
    """B1 critique #10: a centred title whose container also holds an absolutely positioned accent
    bar keeps the container's width (the bar is out of flow); a run beside an icon hugs its line."""
    ir = _extract_snippet(
        tmp_path,
        '<div style="position:absolute;left:40px;top:40px;width:1200px;text-align:center;font-size:28px">'
        'Market entry strategy<span style="position:absolute;left:560px;top:44px;width:80px;height:3px;'
        'background:#F59E0B"></span></div>'
        '<div style="position:absolute;left:40px;top:200px;width:400px;display:flex;gap:8px;align-items:center">'
        '<span style="display:block;width:16px;height:16px;background:#0B5CAD"></span>Beside an icon</div>'
        '<div style="position:absolute;left:40px;top:300px;width:200px"><span style="float:right;'
        'font-weight:bold">20%</span>Beside a float</div>',
        sample_manifest, sample_dir)
    texts = _texts_by_text(ir)
    title, label = texts["Market entry strategy"], texts["Beside an icon"]
    assert title.box.w == pytest.approx(1200, abs=0.5) and title.paragraphs[0]["align"] == "center"
    assert label.box.x >= 40 + 16 + 8 - 0.5 and label.box.w < 200
    # A float is out of flow but shares the run's line (a client deck's weighted criteria): the run hugs,
    # so its box does not reach over the float's text.
    floated, run = texts["20%"], texts["Beside a float"]
    assert run.box.x2 < floated.box.x


def test_r8_splits_only_left_aligned_items_and_says_why(tmp_path: Path, sample_manifest: Manifest, sample_dir: Path):
    """B1 critique #12: a centred item that wraps is not split (centring places its lines); an
    indented one is, with the message naming the text-indent, not an inline box."""
    words = "A list item long enough to wrap onto a second line of its own"
    ir = _extract_snippet(
        tmp_path,
        f'<ul style="position:absolute;left:40px;top:40px;width:300px;text-align:center"><li>{words}</li></ul>'
        f'<ul style="position:absolute;left:400px;top:40px;width:300px"><li style="text-indent:20px">{words}</li></ul>',
        sample_manifest, sample_dir)
    centred = [e for e in _texts(ir) if e.box.x < 380]
    indented = [e for e in _texts(ir) if e.box.x >= 380]
    assert len(centred) == 1 and len(centred[0].paragraphs) == 1
    assert len(indented) == 1 and len(indented[0].paragraphs) == 2
    split = [d.message for d in ir.diagnostics if "split after its first line" in d.message]
    assert split == ["list item split after its first line so its text-indent lands where the browser put it"]


def test_layout_alignment_reads_direction_and_space_between(tmp_path: Path, sample_manifest: Manifest,
                                                            sample_dir: Path):
    """B1 critique #13: in an rtl flex column `flex-end` is the left edge; under `space-between` the
    last item is pinned to the right. Only the edge E2 keeps fixed depends on it."""
    ir = _extract_snippet(
        tmp_path,
        '<div dir="rtl" style="position:absolute;left:40px;top:40px;width:300px;display:flex;'
        'flex-direction:column;align-items:flex-end"><div>Alpha item</div><div>Beta</div></div>'
        '<div style="position:absolute;left:400px;top:40px;width:300px;display:flex;'
        'justify-content:space-between"><span>Revenue</span><span>$15.2B</span></div>',
        sample_manifest, sample_dir)
    texts = _texts_by_text(ir)
    assert [p["align"] for p in texts["Alpha item | Beta"].paragraphs] == ["left", "left"]
    assert texts["Revenue"].paragraphs[0]["align"] == "left"
    assert texts["$15.2B"].paragraphs[0]["align"] == "right"


# ------------------------------------------------------------------------------- cross-cutting


def test_extraction_is_deterministic(sample_manifest: Manifest, sample_dir: Path):
    """Two runs of one slide produce byte-identical JSON — the base of the determinism row."""
    first = extract_html(sample_dir / "slide-02.html", sample_manifest, "layout-01", sample_dir / "assets",
                         slide_id="determinism")
    second = extract_html(sample_dir / "slide-02.html", sample_manifest, "layout-01", sample_dir / "assets",
                          slide_id="determinism")
    assert first.dumps() == second.dumps()


def test_theme_fonts_are_forced_and_substitutions_are_reported(sample_irs: list[IR]):
    for ir in sample_irs:
        assert ir.fonts.forced == {"major": "Arial", "minor": "Arial"}
        assert ir.fonts.used == ["Arial"], "the forcing CSS wins over the slide's own font-family"
        assert ir.fonts.substituted == []


def test_svg_roots_are_tagged_and_expanded_in_place(sample_irs: list[IR]):
    """The placeholder never reaches a saved IR: every svg id comes back spliced at its paint position."""
    ir = sample_irs[0]
    from_svg = [e for e in ir.elements if (e.source or {}).get("svg")]
    # `source.svg` is the element's full path *inside* its root ("svg#3 > g:nth-child(2) > path"),
    # per 02-IR-SCHEMA.md — so the root is the part before the first separator.
    roots = {e.source["svg"].split(" > ")[0] for e in from_svg}
    assert roots == {"svg#1", "svg#2"}, f"slide 1 has two <svg> roots, got {sorted(roots)}"
    for element in ir.elements:
        assert element.kind in ("shape", "text", "image", "table", "chart", "raster")


def test_page_js_ships_with_the_module():
    """The walk is a file, not a string in Python — it must travel with the package."""
    assert PAGE_JS.exists() and PAGE_JS.name == "page.js"
    assert "__engineExtract" in PAGE_JS.read_text(encoding="utf-8")


# --------------------------------------------------------------------- alignment and anchor


def _extract_inline(tmp_path: Path, body: str, manifest: Manifest, assets: Path) -> IR:
    """Extract one ad-hoc slide body — the browser is the thing under test, not a fixture file."""
    html = ("<!doctype html><html><head><meta charset='utf-8'>"
            "<style>html,body{margin:0;padding:0}</style></head>"
            f"<body>{body}</body></html>")
    path = tmp_path / "inline.html"
    path.write_text(html, encoding="utf-8")
    return extract_html(path, manifest, "layout-05", assets, slide_id="inline", title="inline")


def test_alignment_is_read_from_where_the_line_was_drawn(tmp_path, sample_manifest, sample_dir):
    """`justify-content:flex-end` pushes text right while `text-align` stays left — geometry wins.

    A box that trusted `text-align` here would draw the run at the left of the full-width element,
    which is not where the browser painted it. The measured line sits flush against the right edge,
    so the paragraph is recorded as right-aligned.
    """
    body = ("<div style='position:absolute;left:40px;top:40px;width:600px;"
            "display:flex;justify-content:flex-end'>Pushed right</div>")
    ir = _extract_inline(tmp_path, body, sample_manifest, sample_dir / "assets")
    text = next(e for e in _texts(ir) if "Pushed right" in _all_runs(e)[0]["text"])
    assert text.paragraphs[0]["align"] == "right"


def test_a_plain_left_paragraph_is_still_left(tmp_path, sample_manifest, sample_dir):
    """The geometry read must not turn ordinary left-aligned text into anything else."""
    body = ("<div style='position:absolute;left:40px;top:40px;width:600px'>"
            "Ordinary flush-left copy</div>")
    ir = _extract_inline(tmp_path, body, sample_manifest, sample_dir / "assets")
    text = next(e for e in _texts(ir) if "Ordinary" in _all_runs(e)[0]["text"])
    assert text.paragraphs[0]["align"] == "left"


def test_table_cell_vertical_align_becomes_the_anchor(tmp_path, sample_manifest, sample_dir):
    """`display:table-cell; vertical-align:middle` on the cell anchors the text in the middle.

    The cell reads its *own* style, not just its parent's (the row): a flex item is centred by the
    container, but a table cell is centred by the cell the text lives in.
    """
    body = ("<div style='position:absolute;left:40px;top:40px;width:300px;height:200px;display:table'>"
            "<div style='display:table-row'>"
            "<div style='display:table-cell;vertical-align:middle'>Centred label</div>"
            "</div></div>")
    ir = _extract_inline(tmp_path, body, sample_manifest, sample_dir / "assets")
    text = next(e for e in _texts(ir) if "Centred" in _all_runs(e)[0]["text"])
    assert text.anchor == "middle"
