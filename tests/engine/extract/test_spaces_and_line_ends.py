"""r1b — the two engine losses G-2 found in the `media` family (fidelity plan §16 #25).

1. **`&nbsp;` collapsed.** `page.js normaliseText` replaced `/\\s+/` with one space, and JavaScript's
   `\\s` matches U+00A0 and every other space separator, so `A &nbsp; B` — three advances in the
   browser — exported as one breakable space and every word after it moved left. Only CSS's document
   white space (space, tab, LF, CR) collapses now; a non-breaking space reaches the run as U+00A0,
   the export writes it, fit measures it as part of its word and never breaks there, and the text row
   reads it as a textual gap.
2. **Every marker was `med`.** `svg.py _line_end` sized each DrawingML line end `med`, an 8 px head on
   a 2 px line where the design drew 14 px, hidden under the pin it pointed at. The size is now the
   one whose PowerPoint head is nearest the marker's own, by PowerPoint's measured model
   (`LINE_END_SIZES` × max(stroke, `LINE_END_FLOOR_PX`)), and a head no size comes near is said.

Numbers are derived from the fixture source or from the measured evidence file
(`tests/engine/fixtures/extract_cases/line-end-measurements.json`, PowerPoint and PptxRender
measurements taken in Slide Studio on the synthetic test master), never from a previous run.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app.config import engine as config
from app.engine.classify.charts import recognise_charts
from app.engine.classify.placeholders import map_placeholders
from app.engine.emit import pptx as emitter
from app.engine.extract.svg import LINE_END_FLOOR_PX, LINE_END_MISFIT, LINE_END_SIZES, _line_end
from app.engine.ir import IR, Element
from app.engine.manifest import Manifest
from app.engine.reports import EmitOptions
from app.engine.verify import fit
from app.engine.verify.fixtures import context_for
from app.engine.verify.text_deck import _joins
from app.engine.verify.text_layout import annotate_text_layout
from tests.engine.helpers import extract_html

EVIDENCE = Path(__file__).resolve().parents[1] / "fixtures" / "extract_cases" / "line-end-measurements.json"

NBSP = "\u00a0"


# ------------------------------------------------------------------------------------- helpers


def _extract(tmp_path: Path, body: str, sample_manifest: Manifest, sample_dir: Path) -> IR:
    html = tmp_path / "spaces.html"
    html.write_text(
        "<!doctype html><html><head><meta charset='utf-8'><style>html,body{margin:0;width:1280px;"
        "height:720px;overflow:hidden;font-family:Arial;font-size:20px}"
        "div{position:absolute;left:40px;white-space:nowrap}</style></head><body>" + body + "</body></html>",
        encoding="utf-8")
    return extract_html(html, sample_manifest, "layout-05", sample_dir / "assets", slide_id="spaces")


def _named(ir: IR, name: str) -> Element:
    found = [e for e in ir.elements if e.name == name]
    assert found, f"no element named {name!r}; have {[e.name for e in ir.elements]}"
    return found[0]


def _lines(element: Element) -> list[dict]:
    return [line for paragraph in element.paragraphs or [] for line in paragraph["lines"]]


def _line_texts(element: Element) -> list[str]:
    return ["".join(run["text"] for run in line["runs"]) for line in _lines(element)]


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict:
    """The `media` torture family extracted, classified and emitted as `engine torture` does."""
    context = context_for(config.TORTURE_FIXTURE / "media.html")
    extracted = extract_html(context.html, context.manifest, context.layout_id, context.assets_dir,
                             slide_id="media", title="media")
    ir = annotate_text_layout(map_placeholders(recognise_charts(IR.from_json(extracted.to_json())),
                                               context.layout))
    deck = tmp_path_factory.mktemp("media") / "media.pptx"
    emitter.emit([ir], context.manifest, context.master, deck, EmitOptions())
    return {"extracted": extracted, "ir": ir, "deck": deck}


# --------------------------------------------------------------------- 1. the no-break space


def test_a_no_break_space_survives_extraction(tmp_path, sample_manifest, sample_dir):
    """G-2's case: `Alpha &nbsp; Beta` is three advances in the browser and three characters in
    the run — not one breakable space."""
    ir = _extract(tmp_path, '<div data-name="t" style="top:40px">Alpha &nbsp; Beta</div>', sample_manifest, sample_dir)
    assert _line_texts(_named(ir, "t")) == [f"Alpha {NBSP} Beta"]


def test_only_document_white_space_collapses(tmp_path, sample_manifest, sample_dir):
    """CSS Text 3 §4.1.1 collapses space, tab and segment breaks only (measured in Edge: every other
    space separator draws its own advance). En, em, thin and narrow no-break spaces and the zero-width
    U+FEFF are kept as drawn; the line and paragraph separators (one space in the browser, 1.6 in
    PowerPoint) go in as one space each; form feed and vertical tab, which an XML part cannot hold,
    collapse as before."""
    body = ('<div data-name="t" style="top:40px">A&ensp;B&emsp;C&thinsp;D&#x202F;E \t\n  F&#x2028;G'
            '&#xFEFF;H&#x0C;I&#x0B;J&#x2029;K</div>')
    ir = _extract(tmp_path, body, sample_manifest, sample_dir)
    assert _line_texts(_named(ir, "t")) == ["A\u2002B\u2003C\u2009D\u202fE F G\ufeffH I J K"]


def test_the_spaces_at_a_lines_ends_go_with_its_box(tmp_path, sample_manifest, sample_dir):
    """A wrapped line that starts with no-break spaces — in two differently styled runs — loses them
    from its text *and* its box (`glyphExtent` starts the box at the first glyph), exactly as the
    plain spaces there always did: a line's ends are unchanged, only the spaces inside it are kept.
    The browser draws the `Z` after both spaces; the box starts there."""
    body = ('<div data-name="t" style="top:40px;width:160px;white-space:normal;font:40px Arial">'
            'wwwww <b>&nbsp;</b>&nbsp;Z &nbsp;y</div>')
    ir = _extract(tmp_path, body, sample_manifest, sample_dir)
    element = _named(ir, "t")
    lines = _lines(element)
    assert _line_texts(element)[0] == "wwwww"
    second = lines[1]
    assert "".join(run["text"] for run in second["runs"]) == f"Z {NBSP}y"
    assert all(run["text"] for run in second["runs"])
    advance = fit.text_width_px(NBSP, "Arial", 40.0)
    bold = fit.text_width_px(NBSP, "Arial", 40.0, bold=True)
    assert second["box"]["x"] == pytest.approx(40 + bold + advance, abs=0.6)
    boxes = (element.extras.get("x-run-boxes") or [[[]]])[0][1]
    assert len(boxes) == len(second["runs"]) and boxes[0][0] == pytest.approx(second["box"]["x"], abs=0.01)


def test_a_styled_number_before_no_break_spaces_is_no_seam(tmp_path, sample_manifest, sample_dir):
    """The survey's `<span class="num">5</span>&nbsp;&nbsp;Broker Routing`: two runs, the gap between
    them is text in the file (the no-break spaces), so no glued-runs warn and no join."""
    body = '<div data-name="t" style="top:40px"><b style="color:#1A9AFA">5</b>&nbsp;&nbsp;Broker Routing</div>'
    ir = _extract(tmp_path, body, sample_manifest, sample_dir)
    runs = _lines(_named(ir, "t"))[0]["runs"]
    assert [run["text"] for run in runs] == ["5", f"{NBSP}{NBSP}Broker Routing"]
    assert not [d for d in ir.diagnostics if d.message.startswith("glued runs:")]
    assert _joins([run["text"] for run in runs]) == []


def test_the_media_flags_keep_their_no_break_spaces(media):
    """The media family's two emoji lines, as authored (`&nbsp;` between items), reach the IR and
    the file with U+00A0 — G-2 measured the flags band fall from 1,126 to 487 px² with it."""
    flags = next(e for e in media["ir"].elements if e.kind == "text" and "United States" in "".join(_line_texts(e)))
    icons = next(e for e in media["ir"].elements if e.kind == "text" and "Approved" in "".join(_line_texts(e)))
    assert _line_texts(flags) == [
        f"\U0001F1FA\U0001F1F8 United States {NBSP} \U0001F1EC\U0001F1E7 United Kingdom",
        f"\U0001F1F8\U0001F1EC Singapore {NBSP} \U0001F1E6\U0001F1EA UAE {NBSP} \U0001F1EE\U0001F1F3 India",
    ]
    assert _line_texts(icons)[1].count(NBSP) == 3
    with zipfile.ZipFile(media["deck"]) as package:
        slide = package.read("ppt/slides/slide1.xml").decode("utf-8")
    assert f"United States {NBSP} \U0001F1EC" in slide and f"UAE {NBSP} " in slide


def test_fit_never_breaks_at_a_no_break_space():
    """PowerPoint does not break a line at U+00A0 (UAX #14 class GL), so fit measures `Alpha\\u00a0Beta`
    as one word: one line in a box narrower than it, where `Alpha Beta` takes two."""
    def piece(text: str) -> fit._Piece:
        return fit._Piece(text, "Arial", 20.0, False, False, 0.0)

    whole = fit.text_width_px(f"Alpha{NBSP}Beta", "Arial", 20.0)
    assert fit._predict_lines([piece(f"Alpha{NBSP}Beta")], whole - 5, True) == (1, True)
    assert fit._predict_lines([piece("Alpha Beta")], whole - 5, True) == (2, True)
    tokens = fit._tokens([piece(f"a {NBSP} b"), piece(f"c{NBSP}\u202f\u2007d")])
    assert [(gap, [p.text for p in token]) for gap, token in tokens] == [
        ("", ["a"]), (" ", [NBSP]), (" ", ["b", f"c{NBSP}\u202f\u2007d"])]


def test_fit_measures_a_wide_space_as_itself():
    """An em space the extractor now keeps is a break as wide as the character, not one space."""
    def piece(text: str) -> fit._Piece:
        return fit._Piece(text, "Arial", 20.0, False, False, 0.0)

    narrow = fit.text_width_px("Q1 Q2", "Arial", 20.0)
    assert fit._predict_lines([piece("Q1 Q2")], narrow + 1, True) == (1, True)
    assert fit._predict_lines([piece("Q1\u2003Q2")], narrow + 1, True) == (2, True)
    assert fit._space_width(piece("x"), "\u2003") == pytest.approx(fit.text_width_px("\u2003", "Arial", 20.0))


def test_the_text_row_reads_a_no_break_space_as_a_gap():
    """A join is two runs with no white space between them; U+00A0 at either side is white space."""
    assert _joins([f"Moderate{NBSP}", "Critical"]) == []
    assert _joins(["5", f"{NBSP}{NBSP}Broker"]) == []
    assert _joins(["partner", "2023"]) == [(0, 1)]


# ------------------------------------------------------------------------ 2. marker sizes


def _head(size: str, stroke_px: float) -> float:
    return LINE_END_SIZES[size] * max(stroke_px, LINE_END_FLOOR_PX)


def test_line_end_sizes_match_what_powerpoint_draws():
    """The model the mapping uses is PowerPoint's own, measured (r1b's one COM render): every
    triangle head within 0.6 px on both axes and every closed head's width within 1.25 px, at seven
    stroke widths and five (w, len) pairs — `w` sets the width only, `len` the length only."""
    rows = json.loads(EVIDENCE.read_text(encoding="utf-8"))["lineEnds"]
    assert len(rows) == 5 * 7 * 5
    for row in rows:
        measured = row["powerpoint"]
        where = f"{row['type']} {row['strokePx']} px w={row['w']} len={row['len']}: {measured}"
        if row["type"] == "arrow":          # PowerPoint strokes the open arrow: wider by its line
            continue
        assert measured["wPx"] == pytest.approx(_head(row["w"], row["strokePx"]), abs=1.25), where
        if row["type"] == "triangle":
            assert measured["wPx"] == pytest.approx(_head(row["w"], row["strokePx"]), abs=0.6), where
            assert measured["lenPx"] == pytest.approx(_head(row["len"], row["strokePx"]), abs=0.6), where
        # PptxRender, the runtime renderer, draws the same widths
        assert row["pptxrender"]["wPx"] == pytest.approx(_head(row["w"], row["strokePx"]), abs=1.25), where


@pytest.mark.parametrize(("marker", "stroke_px", "unit_px", "size", "said"), [
    # media: viewBox 0 0 10 10, 7 x 7 stroke widths on a 2 px line = a 14 px head -> lg (13.25 px)
    ({"type": "triangle", "width": 7, "height": 7, "units": "strokeWidth"}, 2.0, 1.0, "lg", False),
    # the SVG default, 3 x 3 stroke widths, on a 4 px line = 12 px = med exactly (3 x 4)
    ({"type": "triangle", "width": 3, "height": 3}, 4.0, 1.0, "med", False),
    # a survey arrow: 7 x 5 on 1.8 px = 12.6 long, 9 wide; lg misses by 0.65 + 4.25, med by 4.65 + 1.05
    ({"type": "triangle", "width": 7, "height": 5, "units": "strokeWidth"}, 1.8, 1.0, "lg", False),
    # another: 6 x 6 on 1.5 px = 9 px -> med (7.95)
    ({"type": "triangle", "width": 6, "height": 6, "units": "strokeWidth"}, 1.5, 1.0, "med", False),
    # userSpaceOnUse: 8 user units at scale 2 = 16 px on a 2 px line -> lg
    ({"type": "oval", "width": 8, "height": 8, "units": "userSpaceOnUse"}, 2.0, 2.0, "lg", False),
    # a 2 px head on a 1 px line: even sm draws 5.3 px, more than 1.5 x -> said
    ({"type": "triangle", "width": 2, "height": 2, "units": "strokeWidth"}, 1.0, 1.0, "sm", True),
    # a 30 px head on a 3 px line: lg draws 15 px, half of it -> said
    ({"type": "diamond", "width": 10, "height": 10, "units": "strokeWidth"}, 3.0, 1.0, "lg", True),
])
def test_a_marker_gets_the_line_end_nearest_its_head(marker, stroke_px, unit_px, size, said):
    heard: list[tuple[str, str]] = []
    end = _line_end(marker, stroke_px, unit_px, lambda source, message: heard.append((source, message)), "svg#1 > line")
    assert end == {"type": marker["type"], "size": size}
    assert bool(heard) is said
    if said:
        source, message = heard[0]
        assert source == "svg#1 > line" and "arrowhead of" in message and "line-end size" in message


def test_a_marker_nobody_can_see_gets_no_line_end():
    assert _line_end(None, 2.0) is None
    assert _line_end({"type": "unknown", "width": 7, "height": 7}, 2.0) is None
    assert _line_end({"type": "triangle", "width": 0, "height": 7}, 2.0) is None     # SVG: zero disables it
    assert LINE_END_MISFIT > 1


def test_the_media_pins_get_heads_they_can_show(media):
    """The media family's two connectors (2 px, marker 7 x 7 stroke widths) carry `lg` tail ends:
    a 13 px head where the design has 14, instead of the 8 px `med` that vanished under the 18 px pin."""
    lines = [e for e in media["ir"].elements if e.kind == "shape" and (e.geometry or {}).get("type") == "line"]
    assert len(lines) == 2
    assert [line.stroke["tailEnd"] for line in lines] == [{"type": "triangle", "size": "lg"}] * 2
    assert not [d for d in media["ir"].diagnostics if "arrowhead of" in d.message]
    with zipfile.ZipFile(media["deck"]) as package:
        slide = package.read("ppt/slides/slide1.xml").decode("utf-8")
    assert slide.count('<a:tailEnd type="triangle" w="lg" len="lg"/>') == 2
