"""Render check R1 — boxes with no text of their own inside an inline run (`docs/engine/rendercheck/00-PLAN.md`).

Defect 1: an `<svg>` (or `<img>`) inside an inline `<span>` vanished — `inlineSegments` made tokens from text
nodes only, so a box without text was never detached, never walked, and `emitSvgPlaceholder` never ran
(the render check's d3_s13 chevron icons). Defect 2: a paragraph with no measured text — a row of empty
inline-block rating dots — never reached `flushText`, the only painter of its inline boxes (d1_s07).
Both were silent; the silent-drop sweep (`reportSilentDrops`) now names anything visible no record carries.

The `inline-boxes` torture family holds the render check's cases; the tests below pin what its
`expect.json` counts cannot: which element carries what, the paint order, and the sweep itself.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import engine as config
from app.engine.ir import IR, Element
from app.engine.verify.fixtures import context_for
from tests.engine.helpers import extract_html

FAMILY = config.TORTURE_FIXTURE / "inline-boxes.html"

SLIDE = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;width:1280px;height:720px;overflow:hidden;background:transparent;
  font-family:Calibri,Arial,sans-serif;font-size:14px;color:#021D44}}
.dot{{display:inline-block;width:10px;height:10px;border-radius:50%;background:#021D44}}
</style></head><body>
{body}
</body></html>"""


def _extract(html: Path) -> IR:
    context = context_for(html)
    return extract_html(html, context.manifest, context.layout_id, context.assets_dir, slide_id=html.stem)


def _slide(tmp_path: Path, body: str) -> IR:
    """One slide on the `test-16x9` master, as the torture families are."""
    html = tmp_path / "slide.html"
    html.write_text(SLIDE.format(body=body), encoding="utf-8")
    (tmp_path / "slide.expect.json").write_text('{"master": "test-16x9", "layoutId": "layout-06"}',
                                               encoding="utf-8")
    return _extract(html)


def _text(element: Element) -> str:
    return " | ".join(" ".join("".join(r["text"] for r in line["runs"]) for line in p["lines"])
                      for p in element.paragraphs or [])


def _one_text(ir: IR, text: str) -> Element:
    found = [e for e in ir.elements if e.kind == "text" and _text(e) == text]
    assert len(found) == 1, f"{text!r}: {[_text(e) for e in ir.elements if e.kind == 'text']}"
    return found[0]


def _from_svg(ir: IR, near: tuple[float, float, float, float]) -> list[Element]:
    """The shapes an `<svg>` expanded into whose boxes lie inside `near` (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = near
    return [e for e in ir.elements if e.kind == "shape" and "svg" in (e.source or {}).get("path", "")
            and x0 <= e.box.x and e.box.x2 <= x1 and y0 <= e.box.y and e.box.y2 <= y1]


def _silent(ir: IR) -> list[str]:
    return [d.message for d in ir.diagnostics if d.message.startswith("not exported:")]


@pytest.fixture(scope="module")
def family() -> IR:
    assert FAMILY.exists(), f"{FAMILY} is missing"
    return _extract(FAMILY)


# ---------------------------------------------------------------------------- defect 1: icons in spans


def test_the_chevron_icons_are_exported_in_their_text_colour(family: IR):
    """d3_s13: `<span class="cico">` (inline-block, no text) holding a `currentColor` `<svg>`, after a
    circled number in the same run. Before R1 neither icon reached the export."""
    filled = _from_svg(family, (88, 195, 120, 225))
    stroked = _from_svg(family, (274, 195, 305, 225))
    assert len(filled) == 6 and all((e.fill or {}).get("color") == "FFFFFF" for e in filled)
    assert len(stroked) == 3 and all((e.stroke or {}).get("color") == "FFFFFF" for e in stroked)


def test_the_circled_number_detaches_beside_the_icon_box(family: IR):
    """The icon span is a box of the paragraph, so the number (20 px wide, its text 6) shares its
    paragraph and has geometry of its own: package B's R3 detaches it over its ring."""
    one = _one_text(family, "1")
    ring = [e for e in family.elements if e.kind == "shape" and e.name == one.name]
    assert len(ring) == 1 and ring[0].stroke["color"] == "FFFFFF"
    assert ring[0].box.x - 1 <= one.box.x and one.box.x2 <= ring[0].box.x2 + 1


def test_an_svg_alone_in_a_span_and_in_a_text_less_ring(family: IR):
    square = _from_svg(family, (470, 220, 515, 265))
    assert len(square) == 1 and square[0].fill["color"] == "FFFFFF"
    tick = _from_svg(family, (595, 225, 630, 260))
    ring = [e for e in family.elements if e.kind == "shape" and (e.fill or {}).get("color") == "E8F1FB"
            and e.box.x == pytest.approx(592)]
    assert len(tick) == 1 and len(ring) == 1
    assert ring[0].z < tick[0].z, "the ring span is walked as an element: its fill first, then its icon"


def test_an_inline_flex_pill_paints_under_its_icon_and_its_label_starts_after_it(family: IR):
    label = _one_text(family, "On track")
    [dot] = _from_svg(family, (725, 236, 750, 260))
    [pill] = [e for e in family.elements if e.kind == "shape" and (e.fill or {}).get("color") == "E8F1FB"
              and e.box.x == pytest.approx(720)]
    assert pill.z < dot.z < label.z
    assert label.paragraphs[0]["lines"][0]["box"]["x"] >= dot.box.x2 + 5
    bare = _one_text(family, "Label one")
    [square] = _from_svg(family, (715, 190, 750, 225))
    assert bare.paragraphs[0]["lines"][0]["box"]["x"] >= square.box.x2 + 5


def test_a_span_wrapped_svg_mid_sentence_splits_the_sentence_around_it(family: IR):
    before, after = _one_text(family, "Revenue grew"), _one_text(family, "twelve per cent on the year")
    [triangle] = _from_svg(family, (100, 345, 160, 380))
    assert before.box.x2 <= triangle.box.x + 1 and after.box.x >= triangle.box.x2 - 1


def test_an_image_in_a_span_is_exported_between_its_texts(family: IR):
    [image] = [e for e in family.elements if e.kind == "image"]
    before, after = _one_text(family, "Rated"), _one_text(family, "by analysts")
    assert (image.box.w, image.box.h) == (18, 18)
    assert before.box.x2 <= image.box.x + 0.5 and after.box.x >= image.box.x2 - 0.5


# ---------------------------------------------------------------------------- defect 2: dots-only rows


def test_every_dot_of_the_dots_only_rows_is_painted(family: IR):
    dots = [e for e in family.elements if e.kind == "shape" and 430 <= e.box.y <= 450
            and e.box.w == pytest.approx(10) and e.box.h == pytest.approx(10)]
    filled = [d for d in dots if d.fill["color"] == "021D44"]
    outlined = [d for d in dots if d.fill["color"] == "FFFFFF" and d.stroke and d.stroke["color"] == "021D44"]
    assert (len(filled), len(outlined)) == (5, 5)
    centred = sorted((d for d in dots if 360 <= d.box.x <= 520), key=lambda d: d.box.x)
    assert len(centred) == 4 and centred[0].box.x - 360 == pytest.approx(520 - centred[-1].box.x2, abs=1)


def test_the_table_cell_dots_stay_painted(family: IR):
    chips = [e for e in family.elements if e.kind == "shape" and (e.name or "").startswith("table#1 r0c1 chip")]
    assert len(chips) == 3


def test_the_family_reports_nothing_dropped(family: IR):
    assert not _silent(family)
    assert not [d for d in family.diagnostics if "was not painted" in d.message]


def test_dots_after_pending_text_paint_after_it(tmp_path):
    """A dots-only run after a text block of the same walk: queued on the pending text, as a detached
    box is, so the dots paint after that text's record, once each."""
    ir = _slide(tmp_path, '<div style="position:absolute;left:40px;top:200px"><div>Title</div> '
                          '<span class="dot"></span><span class="dot"></span></div>')
    title = _one_text(ir, "Title")
    dots = [e for e in ir.elements if e.kind == "shape"]
    assert len(dots) == 2 and all(dot.z > title.z for dot in dots)
    assert not _silent(ir)


def test_an_icon_and_a_dot_with_no_text_both_paint(tmp_path):
    """A paragraph of detached boxes and whitespace only: no segment measures a line, so the fallback
    paints the inline boxes the segments left (the dot) — the icon is walked through its span."""
    ir = _slide(tmp_path, '<div style="position:absolute;left:40px;top:200px">'
                          '<span style="display:inline-block;width:24px;height:24px"><svg viewBox="0 0 24 24" '
                          'width="24" height="24"><circle cx="12" cy="12" r="10" fill="#1E9E5A"/></svg></span> '
                          '<span style="display:inline-block;width:10px;height:10px;background:#F59E0B"></span></div>')
    colours = sorted((e.fill or {}).get("color") for e in ir.elements if e.kind == "shape")
    assert colours == ["1E9E5A", "F59E0B"]
    assert not _silent(ir)


def test_an_svg_that_paints_nothing_does_not_split_its_sentence(tmp_path):
    """A zero-size `<svg>` of `<defs>` in a span is no box: the sentence stays one paragraph."""
    ir = _slide(tmp_path, '<p style="position:absolute;left:40px;top:200px;margin:0">Alpha <span><svg width="0" '
                          'height="0"><defs><linearGradient id="g"><stop offset="0" stop-color="red"/>'
                          '</linearGradient></defs></svg></span>beta</p>')
    assert [_text(e) for e in ir.elements if e.kind == "text"] == ["Alpha beta"]


# ------------------------------------------------------------------------------ the silent-drop sweep


def test_the_sweep_names_a_visible_icon_and_box_the_walk_never_reached(tmp_path):
    """`visibility: visible` inside a `visibility: hidden` block paints in the browser, but the walk
    stops at the hidden block: the sweep says so, once per box, with its path."""
    ir = _slide(tmp_path, '<div style="position:absolute;left:40px;top:200px;width:300px;visibility:hidden">'
                          '<span style="visibility:visible;display:inline-block;width:12px;height:12px;'
                          'background:#D64545"></span><svg style="visibility:visible" viewBox="0 0 24 24" '
                          'width="24" height="24"><rect x="1" y="1" width="22" height="22" fill="#0B5CAD"/></svg></div>')
    found = [d for d in ir.diagnostics if d.message.startswith("not exported:")]
    assert [d.level for d in found] == ["warn", "warn"]
    messages = " ".join(d.message for d in found)
    assert "inline <span> paints a box" in messages and "<svg> (svg#1) is visible" in messages
    assert {d.source for d in found} == {"body:nth-child(2) > div:nth-child(1) > span:nth-child(1)",
                                         "body:nth-child(2) > div:nth-child(1) > svg:nth-child(2)"}


def test_the_sweep_is_quiet_for_hidden_and_rasterised_boxes(tmp_path):
    ir = _slide(tmp_path, '<div style="position:absolute;left:40px;top:200px;width:300px">'
                          '<svg style="display:none" viewBox="0 0 24 24" width="24" height="24"><rect width="24" '
                          'height="24"/></svg><div data-pptx="raster"><span class="dot"></span> <svg width="12" '
                          'height="12"><rect width="12" height="12"/></svg></div></div>')
    assert [e.kind for e in ir.elements] == ["raster"]
    assert not _silent(ir)
