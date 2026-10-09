"""WP5 — the verification suite: lint, fit, gate, coverage, the template check and the harness.

Tests fail when an input is missing; they never skip (master brief §10.7). The lint fixtures live
in this file as strings on purpose: WP0b owns `fixtures/torture/`, and a rule-by-rule lint case is
one contract violation in four lines, not a slide.

Everything is synthetic: the sample project (`tests/engine/sample_project.py`), the test masters,
and decks python-pptx builds here. The gate tests (which need a PptxRender build with `/render` and
`/verify`, marker `renderer_full`) build their own two-slide deck on `fixtures/masters/test-16x9.pptx` and score it
against its own render, so they measure the gate rather than the emitter: a deck versus its own
pixels must be 0 px², the same deck with one rectangle moved must not be, and masking that
rectangle must bring it back to 0.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import MSO_SHAPE
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

from app.config import engine as config
from app.engine.emit.text import face_for as emitter_face_for
from app.engine.emit.text import face_for_exact, font_index
from app.engine.emit.text import text_width_px as emitter_width_px
from app.engine.ir import IR, Box, Canvas, Diagnostic, Element, Slide, assign_ids_and_z
from app.engine.manifest import Manifest
from app.engine.reports import Box as ReportBox
from app.engine.reports import EmitReport
from app.engine.verify.coverage import chart_checks, coverage, template_checks
from app.engine.verify.fit import fit, resolve_face, text_width_px
from app.engine.verify.gate import gate, masked_deck
from app.engine.verify.lint import lint
from app.engine.verify.suite import build_masks, run_validator
from app.engine.verify.text_deck import text_collisions, text_report
from tests.engine import helpers as renderer

PX = config.EMU_PER_PX


def _installed(*families: str) -> bool:
    """Every family installed as itself (not a metric-compatible stand-in)."""
    return all(face_for_exact(family) is not None for family in families)


def _ink_extent(image: object, box: Box, *, pad: int = 2) -> Box | None:
    """The bounding box of the ink inside one box, in page px (None when there is none).

    "Ink" is darker than the crop's own brightest decile, counted where it is at least a quarter of
    the crop's darkest ink, so anti-aliased fringes do not widen it. Pillow only.
    """
    from PIL import Image

    assert isinstance(image, Image.Image)
    left, top = max(0, int(box.x) - pad), max(0, int(box.y) - pad)
    right = min(image.width, int(box.x + box.w) + pad + 1)
    bottom = min(image.height, int(box.y + box.h) + pad + 1)
    if right <= left or bottom <= top:
        return None
    crop = image.convert("L").crop((left, top, right, bottom))
    histogram = crop.histogram()
    total, seen, background = sum(histogram), 0, 255
    for level in range(256):                       # the 90th percentile of the crop's lightness
        seen += histogram[level]
        if seen >= 0.9 * total:
            background = level
            break
    darkest = min(level for level in range(256) if histogram[level])
    deepest = background - darkest
    if deepest < 1:
        return None
    threshold = background - deepest / 4.0
    bbox = crop.point(lambda value: 255 if value <= threshold else 0).getbbox()
    if bbox is None:
        return None
    return Box(left + bbox[0], top + bbox[1], bbox[2] - bbox[0], bbox[3] - bbox[1])


def _plain_python_pptx_deck(master: Path, path: Path, titles: Sequence[str]) -> Path:
    """What a plain python-pptx export looks like: the master opened, one titled slide per entry,
    saved. python-pptx re-serialises every part it opens (unlike the engine, whose `finalize`
    restores the master's bytes), which is what the template check has to see through."""
    presentation = Presentation(str(master))
    layout = next(layout for layout in presentation.slide_layouts if layout.name == "Title Only")
    for title in titles:
        slide = presentation.slides.add_slide(layout)
        slide.shapes.title.text = title
    path.parent.mkdir(parents=True, exist_ok=True)
    presentation.save(str(path))
    return path


def _as_saved_elsewhere(master: Path, path: Path) -> Path:
    """`master` as another tool would have saved it: template XML pretty-printed and the master's and
    layouts' relationships listed in reverse (PowerPoint writes rId3, rId2, rId1 on real masters).
    Same content, other bytes, which is what python-pptx's round trip rewrites."""
    import re
    import zipfile

    from lxml import etree

    with zipfile.ZipFile(master) as source, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            template = re.match(r"ppt/(slideMasters|slideLayouts|theme)/", item.filename)
            if template and item.filename.endswith(".rels"):
                root = etree.fromstring(data)
                children = list(root)
                for child in children:
                    root.remove(child)
                for child in reversed(children):
                    root.append(child)
                data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            elif template and item.filename.endswith(".xml"):
                data = etree.tostring(etree.fromstring(data), xml_declaration=True, encoding="UTF-8",
                                      standalone=True, pretty_print=True)
            target.writestr(item, data)
    return path


@pytest.fixture(scope="module")
def foreign_master(tmp_path_factory: pytest.TempPathFactory, sample_master: Path) -> Path:
    return _as_saved_elsewhere(sample_master, tmp_path_factory.mktemp("foreign") / "master.pptx")


@pytest.fixture(scope="module")
def plain_export(tmp_path_factory: pytest.TempPathFactory, foreign_master: Path) -> Path:
    return _plain_python_pptx_deck(foreign_master, tmp_path_factory.mktemp("plain") / "plain.pptx",
                                   ["Growth by channel", "Synthetic sample deck"])


@pytest.fixture(scope="module")
def sample_export(tmp_path_factory: pytest.TempPathFactory, sample_dir: Path):
    """The engine's own export of the synthetic sample project."""
    from app.engine.pipeline import export_deck
    from app.engine.reports import ExportOptions

    return export_deck(sample_dir, ExportOptions(out_dir=tmp_path_factory.mktemp("sample-export")))


# ------------------------------------------------------------------------------------------ lint

CLEAN_SLIDE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
html,body{margin:0;width:1280px;height:720px;overflow:hidden;background:transparent;
  font-family:Arial,Helvetica,sans-serif;color:#2E2E38}
.title{position:absolute;left:64px;top:28px;width:1152px;height:62px;font-size:22pt;font-weight:bold}
.card{position:absolute;left:64px;top:140px;width:520px;height:200px;background:#F5F5F7;
  border-left:3px solid #1A9AFA;border-radius:4px;box-shadow:0 2px 8px rgba(0,0,0,.12)}
</style></head><body>
<div class="title">A perfectly ordinary slide</div>
<div class="card"><p>Some body copy with <b>bold</b>, <i>italic</i> and a <mark>chip</mark>.</p></div>
<svg width="200" height="80" viewBox="0 0 200 80" style="position:absolute;left:700px;top:140px">
  <rect x="0" y="0" width="60" height="40" fill="#1A9AFA"/>
  <text x="0" y="60" font-family="Arial" font-size="9">Legend</text>
</svg>
<aside class="notes" style="display:none">Speaker notes are exempt.</aside>
</body></html>
"""


def _rules(html: str, manifest: Manifest | None = None) -> set[str]:
    return {finding.rule for finding in lint(html, manifest).findings}


def _levels(html: str, manifest: Manifest | None = None) -> dict[str, str]:
    return {finding.rule: finding.level for finding in lint(html, manifest).findings}


def test_lint_is_silent_on_a_contract_abiding_slide(sample_manifest: Manifest) -> None:
    report = lint(CLEAN_SLIDE, sample_manifest)
    assert report.findings == [], f"false positives: {[f.rule + ': ' + f.message for f in report.findings]}"
    assert report.clean


def test_lint_finds_no_errors_in_the_sample_slides(sample_slides: list[Path], sample_manifest: Manifest) -> None:
    """The sample project's slides are a zero-false-positive fixture."""
    for slide in sample_slides:
        report = lint(slide.read_text(encoding="utf-8"), sample_manifest)
        assert report.errors == [], f"{slide.name}: {[f.message for f in report.errors]}"
        # Slide 1 genuinely holds hand-drawn SVG charts, so that one warning is expected; nothing
        # else may fire.
        assert {f.rule for f in report.findings} <= {"hand-drawn-chart"}, \
            f"{slide.name}: {[f.rule for f in report.findings]}"


def test_lint_is_fast_enough_for_every_save(sample_slides: list[Path], sample_manifest: Manifest) -> None:
    import time

    html = sample_slides[0].read_text(encoding="utf-8")
    lint(html, sample_manifest)                                   # warm the parser
    started = time.perf_counter()
    lint(html, sample_manifest)
    assert (time.perf_counter() - started) < 0.2               # budget 50 ms, 4× head-room on CI


@pytest.mark.parametrize(
    ("rule", "html"),
    [
        ("remote-resource", '<img src="https://example.com/logo.png">'),
        ("remote-resource", '<div style="background-image:url(https://cdn.example.com/x.png)"></div>'),
        ("script", "<script>alert(1)</script>"),
        ("script", '<div onclick="go()">x</div>'),
        ("disallowed-tag", "<video src=\"/api/projects/p1/assets/a.mp4\"></video>"),
        ("img-not-asset", '<img src="../../etc/logo.png">'),
        ("chart-spec", '<div data-chart=\'{"type":"bubble","series":[{"values":[1]}]}\'></div>'),
        ("chart-spec", "<div data-chart='{not json}'></div>"),
        ("google-fonts", '<style>@import url("https://fonts.googleapis.com/css2?family=Lexend");</style>'),
        ("text-overflow", '<div style="text-overflow:ellipsis;white-space:nowrap">x</div>'),
        ("raster-css", '<div style="filter:blur(2px)">x</div>'),
        ("raster-css", '<div style="background:conic-gradient(#fff,#000)">x</div>'),
        ("raster-css", '<div style="box-shadow:inset 0 2px 4px #000">x</div>'),
        ("raster-css", '<div style="transform:skewX(12deg)">x</div>'),
        ("raster-css", '<div style="text-shadow:0 1px 3px rgba(0,0,0,.4)">x</div>'),
        ("raster-css", '<div style="mix-blend-mode:multiply">x</div>'),
        ("raster-svg", '<svg><filter id="f"><feGaussianBlur stdDeviation="2"/></filter></svg>'),
        ("raster-svg", '<img src="/api/projects/p1/assets/icon.svg">'),
        ("unsupported-svg", '<svg><animate attributeName="x" to="10"/></svg>'),
        ("icon-font", '<i style="font-family:\'Font Awesome 6 Free\'"></i>'),
        ("icon-font", '<style>.mi{font-family:"Material Symbols Outlined"}</style><span class="mi">home</span>'),
        ("colour-gamut", '<div style="background:oklch(0.9 0.1 150)">x</div>'),
        ("gradient-text",
         '<div style="background:linear-gradient(red,blue);-webkit-background-clip:text;color:transparent">x</div>'),
        ("pseudo-content-image",
         '<style>.x::before{content:url(/api/projects/p/assets/a.png)}</style><div class="x">x</div>'),
        ("animation", '<div style="animation: spin 1s infinite">x</div>'),
        ("animation", '<style>.p{animation-name:pulse;animation-iteration-count:infinite}</style><div class="p">x</div>'),
        ("text-overflow", '<div style="display:-webkit-box;-webkit-line-clamp:2;overflow:hidden">x</div>'),
    ],
)
def test_lint_each_rule_fires(rule: str, html: str) -> None:
    document = f"<!doctype html><html><body>{html}</body></html>"
    assert rule in _rules(document), f"{rule} did not fire on {html!r}: {_rules(document)}"


SYNTHETIC_FAMILY = "Engine Test Sans"


def _ascii_only_font(path: Path) -> Path:
    """A synthetic TrueType font with glyphs for printable ASCII and nothing else (no arrows)."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    characters = {code: f"uni{code:04X}" for code in range(0x20, 0x7F)}
    order = [".notdef", *characters.values()]
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap(characters)
    glyphs = {}
    for name in order:
        pen = TTGlyphPen(None)
        pen.moveTo((50, 0))
        pen.lineTo((50, 600))
        pen.lineTo((450, 600))
        pen.lineTo((450, 0))
        pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (500, 50) for name in order})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": SYNTHETIC_FAMILY, "styleName": "Regular"})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    builder.save(str(path))
    return path


@pytest.fixture()
def lexend_only_manifest(sample_manifest: Manifest, monkeypatch, tmp_path: Path) -> Manifest:
    """A synthetic ASCII-only font (no arrows) as the only installed font, and as both theme fonts.

    (Slide Studio used its brand Lexend build, which also lacks arrows; it is not in this repo.)"""
    import copy

    from app.engine.emit.text import forget_fonts
    from app.engine.extract.html import forget_installed_families

    fonts = tmp_path / "fonts"
    fonts.mkdir()
    _ascii_only_font(fonts / "EngineTestSans-Regular.ttf")
    monkeypatch.setattr(config, "FONT_DIRS", (fonts,))
    forget_fonts()
    forget_installed_families()
    manifest = copy.deepcopy(sample_manifest)
    manifest.theme = {**(manifest.theme or {}),
                      "fonts": {"major": SYNTHETIC_FAMILY, "minor": SYNTHETIC_FAMILY}}
    yield manifest
    forget_fonts()
    forget_installed_families()


@pytest.mark.parametrize(
    "html",
    [
        # a Private Use Area glyph inline, in an element set in the icon font
        '<p style="font-family:\'Font Awesome 6 Free\'">\uf015</p>',
        # the usual icon-font pattern: the family on one class, the glyph drawn by another's ::before
        '<style>.fa{font-family:"Font Awesome 6 Free"}.fa-user::before{content:"\\f007"}</style>'
        '<p>Profile <i class="fa fa-user"></i></p>',
    ],
    ids=["inline", "pseudo"],
)
def test_lint_icon_font_glyphs_are_not_also_glyph_missing(html: str, lexend_only_manifest: Manifest) -> None:
    """One icon, one finding: the Private Use Area glyph of an icon font is `icon-font`'s to report."""
    rules = _rules(f"<!doctype html><html><body>{html}</body></html>", lexend_only_manifest)
    assert "icon-font" in rules and "glyph-missing" not in rules, rules


def test_lint_glyph_missing_still_reports_ordinary_characters(lexend_only_manifest: Manifest) -> None:
    """An arrow the theme font lacks is still `glyph-missing` — in plain text and inside an icon-font
    element alike; only the icon font's Private Use Area glyphs are left to `icon-font`."""
    plain = lint("<!doctype html><html><body><p>Next \u2192 step</p></body></html>", lexend_only_manifest)
    assert [f.rule for f in plain.findings if f.rule == "glyph-missing"] == ["glyph-missing"]

    mixed = lint('<!doctype html><html><body><p style="font-family:\'Font Awesome 6 Free\'">'
                 '\uf015 \u2192</p></body></html>', lexend_only_manifest)
    (finding,) = [f for f in mixed.findings if f.rule == "glyph-missing"]
    assert "U+2192" in finding.message and "U+F015" not in finding.message
    assert any(f.rule == "icon-font" for f in mixed.findings)


def test_lint_levels_follow_the_contract() -> None:
    """Errors mean "Path A will approximate or drop something"; everything else warns (hand-off §10.3)."""
    document = (
        '<!doctype html><html><body><img src="https://example.com/a.png">'
        '<div style="filter:blur(2px)">x</div></body></html>'
    )
    levels = _levels(document)
    assert levels["remote-resource"] == "error"
    assert levels["raster-css"] == "warn"


def test_lint_never_raises_on_broken_html() -> None:
    assert lint("<html><body><div style='color:'><p>unclosed").findings is not None


def test_lint_icon_font_is_a_warning_that_points_at_inline_svg() -> None:
    """An icon font is a warning (Path A still exports, just not the glyph) with the real fix in it."""
    document = ('<!doctype html><html><body>'
                '<i style="font-family:\'Font Awesome 6 Free\'"></i></body></html>')
    findings = [f for f in lint(document).findings if f.rule == "icon-font"]
    assert len(findings) == 1
    assert findings[0].level == "warn"
    assert "inline <svg>" in findings[0].message


def test_lint_does_not_call_a_theme_font_an_icon_font(sample_manifest: Manifest) -> None:
    """The hint list matches icon families, not ordinary text families like the theme's own."""
    document = ('<!doctype html><html><body>'
                '<p style="font-family:Arial">plain text</p></body></html>')
    assert "icon-font" not in _rules(document, sample_manifest)


def test_lint_flags_a_font_outside_the_theme(sample_manifest: Manifest) -> None:
    document = '<!doctype html><html><body><div style="font-family:Comic Sans MS">x</div></body></html>'
    assert "font-not-theme" in _rules(document, sample_manifest)
    assert "font-not-theme" not in _rules(document)            # no manifest, no guess


def test_lint_flags_a_weight_the_theme_fonts_lack(sample_manifest: Manifest, masters_dir: Path) -> None:
    """`font-weight` warns when neither theme family has a static face within 50 of the weight."""
    def weight(value: str) -> str:
        return f'<!doctype html><html><body><p style="font-weight:{value}">x</p></body></html>'

    calibri = Manifest.load(masters_dir / "test-16x9.manifest.json")     # Calibri Light / Calibri
    assert "font-weight" in _rules(weight("300"), sample_manifest)          # Arial has no 300 face
    assert _levels(weight("300"), sample_manifest)["font-weight"] == "warn"
    from tests.engine.helpers import installed

    # Calibri's static faces are 300 (calibril.ttf), 400 and 700 where Calibri is installed; on Linux
    # Calibri is measured in Carlito (400, 700) and Calibri Light is absent, so 300 has no face there.
    if installed("Calibri") and installed("Calibri Light"):
        assert "font-weight" not in _rules(weight("300"), calibri)       # calibril.ttf is 300
    else:
        assert "font-weight" in _rules(weight("300"), calibri)
    assert "font-weight" in _rules(weight("600"), calibri)               # no 600 face either way
    message = next(f.message for f in lint(weight("600"), calibri).findings if f.rule == "font-weight")
    assert "neither Calibri Light nor Calibri" in message
    if installed("Calibri"):
        assert "(Calibri Bold)" in message
    for fine in ("400", "700", "bold", "normal", "lighter", "bolder"):
        assert "font-weight" not in _rules(weight(fine), sample_manifest), fine
    assert "font-weight" not in _rules(weight("300")), "no manifest, no guess"


def test_lint_flags_an_element_off_the_canvas(sample_manifest: Manifest) -> None:
    document = (
        '<!doctype html><html><body>'
        '<div style="position:absolute;left:1200px;top:10px;width:300px;height:40px">x</div>'
        "</body></html>"
    )
    assert "outside-canvas" in _rules(document, sample_manifest)


def test_lint_reads_the_cascade_not_just_inline_styles(sample_manifest: Manifest) -> None:
    """Half the geometry is in a class rule and half in the style attribute: both have to be seen."""
    document = (
        "<!doctype html><html><head><style>.k{position:absolute;top:10px;height:40px}</style></head>"
        '<body><div class="k" style="left:1260px;width:300px">x</div></body></html>'
    )
    assert "outside-canvas" in _rules(document, sample_manifest)


def test_lint_accepts_a_rectangular_clip_path_and_warns_on_any_other() -> None:
    rectangular = (
        '<svg><defs><clipPath id="c"><rect x="0" y="0" width="10" height="10"/></clipPath></defs>'
        '<rect clip-path="url(#c)" x="0" y="0" width="5" height="5"/></svg>'
    )
    assert "raster-svg" not in _rules(f"<html><body>{rectangular}</body></html>")
    circular = (
        '<svg><defs><clipPath id="c"><circle cx="5" cy="5" r="5"/></clipPath></defs>'
        '<rect clip-path="url(#c)" x="0" y="0" width="5" height="5"/></svg>'
    )
    assert "raster-svg" in _rules(f"<html><body>{circular}</body></html>")


def test_lint_hand_drawn_chart_heuristic_ignores_a_decorative_row() -> None:
    """Three aligned rects are not a chart without numbers — the contract's own qualifier."""
    decorative = (
        '<svg viewBox="0 0 100 40">'
        '<rect x="0" y="10" width="8" height="20"/><rect x="12" y="10" width="8" height="20"/>'
        '<rect x="24" y="10" width="8" height="20"/><text x="0" y="38">Phases of work</text></svg>'
    )
    assert "hand-drawn-chart" not in _rules(f"<html><body>{decorative}</body></html>")

    charty = (
        '<svg viewBox="0 0 100 40">'
        '<rect x="0" y="10" width="8" height="20"/><rect x="12" y="4" width="8" height="26"/>'
        '<rect x="24" y="16" width="8" height="14"/>'
        '<text x="0" y="38">12</text><text x="12" y="38">18</text><text x="24" y="38">7</text></svg>'
    )
    assert "hand-drawn-chart" in _rules(f"<html><body>{charty}</body></html>")


# ------------------------------------------------------------------------------------------- fit


def _text_element(box: Box, text: str, size_px: float, lines: int, **kwargs) -> Element:
    return Element(
        kind="text",
        box=box,
        paragraphs=[{
            "align": "left",
            "lineHeightPx": size_px * 1.2,
            "lines": [
                {"box": {"x": box.x, "y": box.y + i * size_px * 1.2, "w": box.w, "h": size_px * 1.2},
                 "runs": [{"text": text, "font": "Arial", "sizePx": size_px, "weight": 400,
                           "italic": False, "color": "2E2E38"}]}
                for i in range(lines)
            ],
        }],
        anchor="top",
        **kwargs,
    )


def _ir_with(elements: list[Element], slide_id: str = "sld_test") -> IR:
    return IR(
        canvas=Canvas(1280, 720),
        slide=Slide(id=slide_id, layoutId="layout-01"),
        elements=assign_ids_and_z(elements),
    )


def _deck_with_text(path: Path, master: Path, boxes: list[tuple[Box, str, float]], *,
                    wrap: bool = True, align: str = "left", rotation: float = 0.0,
                    mar_l_px: float = 0.0) -> Path:
    """One slide of Arial text boxes. `mar_l_px` makes each paragraph a hanging bullet at that `marL`."""
    from pptx.enum.text import MSO_AUTO_SIZE, PP_ALIGN

    presentation = Presentation(str(master))
    slide = presentation.slides.add_slide(presentation.slide_masters[0].slide_layouts[6])
    for box, text, size_px in boxes:
        shape = slide.shapes.add_textbox(
            Emu(round(box.x * PX)), Emu(round(box.y * PX)),
            Emu(round(box.w * PX)), Emu(round(box.h * PX)),
        )
        frame = shape.text_frame
        # python-pptx's textbox template carries `<a:spAutoFit/>`; a box that grows to fit its text
        # cannot overflow, and the predictor correctly says nothing about one. The emitter writes
        # fixed boxes, so the fixtures do too.
        frame.auto_size = MSO_AUTO_SIZE.NONE
        frame.word_wrap = wrap
        frame.paragraphs[0].text = text
        frame.paragraphs[0].runs[0].font.size = Pt(size_px * config.PT_PER_PX)
        frame.paragraphs[0].runs[0].font.name = "Arial"
        if align != "left":
            frame.paragraphs[0].alignment = {"center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT,
                                             "justify": PP_ALIGN.JUSTIFY}[align]
        if mar_l_px:
            properties = frame.paragraphs[0]._p.get_or_add_pPr()
            properties.set("marL", str(round(mar_l_px * PX)))
            properties.set("indent", str(-round(mar_l_px * PX)))
            properties.append(properties.makeelement(qn("a:buChar"), {"char": "•"}))
        if rotation:
            shape.rotation = rotation
    path.parent.mkdir(parents=True, exist_ok=True)
    presentation.save(str(path))
    return path


def test_font_index_matches_by_name_table_not_by_filename() -> None:
    """The file behind each name is the one PowerPoint and the browser draw, not the shortest name.

    The private index this test used to check filed `ARIALN.TTF` under "Arial" and returned it for
    Arial (its `family` was "Arial", name ID 16, so the old assertion passed on the Narrow file).
    """
    index = font_index()
    assert index, f"no fonts found in {config.FONT_DIRS}"
    if not _installed("Arial", "Arial Narrow", "Calibri", "Calibri Light", "Segoe UI", "Segoe UI Semibold"):
        pytest.skip("names the Microsoft font files (arial.ttf, calibri.ttf, segoeui...): a Windows font folder")
    arial = resolve_face("Arial", bold=False, italic=False)
    assert arial is not None, "Arial is not installed; the fit predictor cannot measure it"
    assert arial.family.lower() == "arial" and arial.path.name.lower() == "arial.ttf"
    assert not arial.width_variant
    bold = resolve_face("Arial", bold=True, italic=False)
    assert bold is not None and bold.bold_member and bold.path.name.lower() == "arialbd.ttf"
    assert resolve_face("Calibri", bold=False, italic=False).path.name.lower() == "calibri.ttf"
    light = resolve_face("Calibri Light", bold=False, italic=False)
    assert light is not None and light.path.name.lower() == "calibril.ttf"
    assert light.legacy_family == "Calibri Light" and light.typographic_family == "Calibri"
    semibold = resolve_face("Segoe UI Semibold", bold=False, italic=False)
    assert semibold is not None and semibold.path.name.lower() == "seguisb.ttf"
    assert not semibold.bold_member, "Semibold is not GDI's Bold member: b=1 would embolden it again"
    # WP-D's list, on the weight (fit's old width class went with its private `Face`, plan D4):
    # b="1" on Segoe UI is its Bold file, not the 600 Semibold, and every regular is the 400 face.
    assert (arial.weight, bold.weight, light.weight) == (400, 700, 300)
    assert resolve_face("Calibri", bold=False, italic=False).weight == 400
    segoe_bold = resolve_face("Segoe UI", bold=True, italic=False)
    assert segoe_bold is not None and segoe_bold.path.name.lower() == "segoeuib.ttf"
    assert segoe_bold.weight == 700
    # A width variant is filed under its own legacy name only, never under "Arial".
    assert all(not face.width_variant for face in index["arial"])
    assert resolve_face("Arial Narrow", bold=False, italic=False).path.name.lower() == "arialn.ttf"
    # A family that exists only as a file name must not be matched.
    assert resolve_face("ARIALN", bold=False, italic=False) is None


@pytest.mark.parametrize("family", ["Arial", "Segoe UI", "Times New Roman", "Calibri"])
@pytest.mark.parametrize("bold", [False, True])
def test_emitter_and_fit_measure_with_the_same_file(family: str, bold: bool) -> None:
    """One index, one rule (plan D4): what the emitter width-locks to is what fit predicts with."""
    weight = 700 if bold else 400
    predictor = resolve_face(family, bold=bold, italic=False)
    emitter = emitter_face_for(family, weight, False)
    if predictor is None:
        pytest.skip(f"{family} is not installed here, nor a metric-compatible stand-in")
    assert emitter is not None
    assert predictor.path == emitter.path and predictor.index == emitter.index
    text = "Revenue grew 12% to $4.2B across the northern markets"
    for size in (10.0, 13.5, 21.0, 40.0):
        fit_width = text_width_px(text, family, size, bold=bold)
        emit_width = emitter_width_px(text, family, size, weight, False)
        assert fit_width == pytest.approx(emit_width, rel=0.005)


def test_text_width_grows_with_size_and_reports_an_absent_family() -> None:
    small = text_width_px("Hamburgefonstiv", "Arial", 10.0)
    large = text_width_px("Hamburgefonstiv", "Arial", 20.0)
    assert small and large and large > small * 1.9
    assert text_width_px("x", "No Such Family At All", 12.0) is None


def _with_theme_major(deck: Path, family: str) -> Path:
    """The same deck with its theme part's major latin font replaced — what a re-theme does."""
    import re
    import zipfile

    rewritten = deck.with_name(f"{deck.stem}-themed.pptx")
    with zipfile.ZipFile(deck) as source, zipfile.ZipFile(rewritten, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename.startswith("ppt/theme/") and item.filename.endswith(".xml"):
                data = re.sub(rb'(<a:majorFont><a:latin typeface=")[^"]*(")',
                              rb"\g<1>" + family.encode() + rb"\g<2>", data)
            target.writestr(item, data)
    return rewritten


def test_fit_measures_titles_in_the_major_font(tmp_path: Path, masters_dir: Path) -> None:
    """`+mj-lt` is measured in the deck's own theme major font, `+mn-lt` in its minor (plan D4).

    Before, both tokens fell back to the minor font — so every title on a master whose major and
    minor differ was predicted in the body face. The fonts come from the deck's theme part, which is
    what PowerPoint resolves the tokens to: on `test-16x9` that part says Calibri for both slots,
    not the manifest's Calibri Light (the fixture inconsistency D4 reports).
    """
    from app.engine.verify.fit import _paragraph_defaults, _segments, _shape_defaults, _theme_fonts_of

    title = "Why balance-sheet banks are losing share of lending"
    deck = _deck_with_text(tmp_path / "title.pptx", masters_dir / "test-16x9.pptx",
                           [(Box(64, 40, 700, 80), title, 30.0), (Box(64, 200, 700, 80), title, 30.0)])
    assert _theme_fonts_of(Presentation(str(deck)).slides[0]) == {"major": "Calibri", "minor": "Calibri"}

    presentation = Presentation(str(deck))
    for shape, token in zip(presentation.slides[0].shapes, ("+mj-lt", "+mn-lt"), strict=False):
        rPr = shape.text_frame.paragraphs[0].runs[0]._r.get_or_add_rPr()
        rPr.find(qn("a:latin")).set("typeface", token)
    presentation.save(str(deck))
    themed = _with_theme_major(deck, "Arial Black")

    slide = Presentation(str(themed)).slides[0]
    fonts = _theme_fonts_of(slide)
    assert fonts == {"major": "Arial Black", "minor": "Calibri"}
    families = []
    for shape in slide.shapes:
        paragraph = shape.text_frame.paragraphs[0]
        defaults = _shape_defaults(shape, fonts)
        [pieces] = _segments(paragraph, _paragraph_defaults(paragraph, defaults, fonts), fonts)
        families.append(pieces[0].family)
        # `_check_table`'s call passes no fonts: a token then keeps the caller's default family
        [unresolved] = _segments(paragraph, {**defaults, "family": "Arial"})
        assert unresolved[0].family == "Arial"
    assert families == ["Arial Black", "Calibri"]

    # and the prediction follows: in the wide major face the same title needs a second line
    report = fit(themed, [])
    assert [detail["predictedLines"] for detail in report.details] == [2, 1]
    # a fallback for a deck whose theme part names no fonts: the IR's forced fonts, then Arial
    assert _theme_fonts_of(None, {"major": "Calibri Light"}) == {"major": "Calibri Light", "minor": "Arial"}


def test_fit_reports_the_zero_width_title(tmp_path: Path, sample_master: Path) -> None:
    """A title placeholder squeezed to zero width (a defect real decks carry) is reported, once."""
    deck = _plain_python_pptx_deck(sample_master, tmp_path / "zero-width.pptx",
                                   ["A title that has no room at all", "A title with room"])
    presentation = Presentation(str(deck))
    presentation.slides[0].shapes.title.width = Emu(0)
    presentation.save(str(deck))
    report = fit(deck, [])
    assert report.slides_checked == 2
    zero_width = [i for i in report.issues if i.kind == "zero-width"]
    assert len(zero_width) == 1
    assert "Title 1" in zero_width[0].shape and zero_width[0].slide == 1


def test_fit_is_clean_on_the_engines_own_export(sample_export) -> None:
    """The sample project as the engine exports it: every text box predicted to fit, nothing off the
    slide, and every wrap-off frame measured with room to spare."""
    report = fit(sample_export.pptx, sample_export.irs)
    assert report.slides_checked == 2
    assert report.issues == [], [(i.slide, i.kind, i.shape, i.detail) for i in report.issues]
    wrap_off = [d for d in report.details if d.get("wrap") is False]
    assert wrap_off and all(d["measured"] and d["headroomPx"] >= 0 for d in wrap_off), \
        [d for d in wrap_off if d["headroomPx"] < 0]


def test_fit_predicts_an_overflow(tmp_path: Path, masters_dir: Path) -> None:
    master = masters_dir / "test-16x9.pptx"
    assert master.exists(), f"{master} is missing (WP0b deliverable)"
    long_text = "PowerPoint will need several lines for this sentence because the box is narrow. " * 3
    deck = _deck_with_text(tmp_path / "overflow.pptx", master, [(Box(64, 64, 300, 24), long_text, 16.0)])
    report = fit(deck, [])
    assert [i.kind for i in report.issues] == ["overflow"]
    assert "needs" in report.issues[0].detail


def test_fit_compares_the_predicted_line_count_with_the_ir(tmp_path: Path, masters_dir: Path) -> None:
    master = masters_dir / "test-16x9.pptx"
    text = "One two three four five six seven eight nine ten eleven twelve thirteen fourteen"
    deck = _deck_with_text(tmp_path / "lines.pptx", master, [(Box(64, 64, 300, 200), text, 16.0)])
    # The IR claims one line for text that needs several in a 300 px box.
    ir = _ir_with([_text_element(Box(64, 64, 300, 200), text, 16.0, lines=1)])
    report = fit(deck, [ir])
    kinds = [i.kind for i in report.issues]
    assert "line-count" in kinds, report.issues
    detail = next(d for d in report.details if d["shape"].startswith("TextBox"))
    assert detail["irLines"] == 1 and detail["predictedLines"] > 1


def test_fit_is_quiet_when_the_prediction_matches_the_ir(tmp_path: Path, masters_dir: Path) -> None:
    master = masters_dir / "test-16x9.pptx"
    text = "Short line"
    deck = _deck_with_text(tmp_path / "match.pptx", master, [(Box(64, 64, 400, 40), text, 16.0)])
    ir = _ir_with([_text_element(Box(64, 64, 400, 40), text, 16.0, lines=1)])
    report = fit(deck, [ir])
    assert report.issues == [], report.issues
    assert report.clean


def test_fit_flags_an_off_slide_text_box_but_not_a_right_aligned_overhang(
    tmp_path: Path, masters_dir: Path
) -> None:
    """Off-slide is judged by where the *text* lands, not by the box (the server's own rule).

    A right-aligned label often sits in a wide box that starts past the left edge; its text is on
    the slide and nothing is wrong. A box whose anchor is past the right edge really is lost.
    """
    from pptx.enum.text import MSO_AUTO_SIZE, PP_ALIGN

    master = masters_dir / "test-16x9.pptx"
    presentation = Presentation(str(master))
    slide = presentation.slides.add_slide(presentation.slide_masters[0].slide_layouts[6])

    overhang = slide.shapes.add_textbox(Emu(round(-200 * PX)), Emu(round(100 * PX)),
                                        Emu(round(600 * PX)), Emu(round(40 * PX)))
    overhang.text_frame.auto_size = MSO_AUTO_SIZE.NONE
    overhang.text_frame.paragraphs[0].text = "right aligned label"
    overhang.text_frame.paragraphs[0].alignment = PP_ALIGN.RIGHT

    off = slide.shapes.add_textbox(Emu(round(1400 * PX)), Emu(round(200 * PX)),
                                   Emu(round(200 * PX)), Emu(round(40 * PX)))
    off.text_frame.auto_size = MSO_AUTO_SIZE.NONE
    off.text_frame.paragraphs[0].text = "this one really is off the slide"

    deck = tmp_path / "offslide.pptx"
    presentation.save(str(deck))

    report = fit(deck, [])
    off_slide = [i for i in report.issues if i.kind == "off-slide"]
    assert len(off_slide) == 1, [i.detail for i in report.issues]
    assert "really is off" in off_slide[0].shape


# -------------------------------------------------------------- fit: lines that cannot wrap (WP-D)

#: About 400 px of Arial 16 px (fit's own measure is taken in each test, never assumed).
LONG_LINE = "Revenue grew twelve per cent to four point two billion dollars"
#: About 300 px of Arial 16 px.
MID_LINE = "Three hundred pixels of text in Arial at sixteen"


def _issues_by_shape(report) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for issue in report.issues:
        found.setdefault(issue.shape.split(" holding ")[0].strip("'"), []).append(issue.kind)
    return found


def test_fit_measures_theme_tokens_with_the_decks_own_theme(tmp_path: Path, masters_dir: Path) -> None:
    """A `+mj-lt` run is measured in the deck's own theme major font (G-1's `_theme_fonts_of`).

    On `test-16x9` the theme part says Calibri (its manifest says Calibri Light — the fixture
    inconsistency plan D4 reports). The width D's detail carries is Calibri's, far from Arial's.
    """
    text = "Why balance-sheet banks are losing share of lending"
    deck = _deck_with_text(tmp_path / "token.pptx", masters_dir / "test-16x9.pptx",
                           [(Box(64, 40, 1100, 60), text, 30.0)], wrap=False)
    presentation = Presentation(str(deck))
    run = presentation.slides[0].shapes[0].text_frame.paragraphs[0].runs[0]
    run._r.get_or_add_rPr().find(qn("a:latin")).set("typeface", "+mj-lt")
    presentation.save(str(deck))

    report = fit(deck, [])
    [detail] = report.details
    calibri, arial = text_width_px(text, "Calibri", 30.0), text_width_px(text, "Arial", 30.0)
    assert detail["widestLinePx"] == pytest.approx(calibri, abs=1.0)
    assert abs(detail["widestLinePx"] - arial) > 0.05 * arial
    assert report.issues == []


def test_fit_reports_an_overhang_on_a_wrap_off_box(tmp_path: Path, masters_dir: Path) -> None:
    """A line in a frame that does not wrap cannot break again: predicted wider than its box, it overhangs."""
    master = masters_dir / "test-16x9.pptx"
    width = text_width_px(LONG_LINE, "Arial", 16.0)
    assert 300 < width < 560, width
    narrow = _deck_with_text(tmp_path / "narrow.pptx", master, [(Box(64, 64, 300, 40), LONG_LINE, 16.0)],
                             wrap=False)
    report = fit(narrow, [])
    assert [i.kind for i in report.issues] == ["overhang"], report.issues
    assert "past its edge" in report.issues[0].detail
    [detail] = report.details
    inner = 300 - 2 * 0.1 * config.PX_PER_IN                # python-pptx's default 0.1 in insets
    assert detail["headroomPx"] == pytest.approx(inner - width, abs=0.05)
    assert detail["widestLine"] == 1 and detail["widestLinePx"] == pytest.approx(width, abs=0.05)

    roomy = _deck_with_text(tmp_path / "roomy.pptx", master, [(Box(64, 64, 600, 40), LONG_LINE, 16.0)],
                            wrap=False)
    report = fit(roomy, [])
    assert report.issues == [], report.issues
    [detail] = report.details
    assert detail["headroomPx"] > 0 and detail["wrap"] is False and detail["measured"] is True
    assert detail["missingFonts"] == [] and detail["syntheticBold"] == []


def test_fit_calls_a_wrap_off_line_off_the_slide_once(tmp_path: Path, masters_dir: Path) -> None:
    """Past the canvas the overhang is an `off-slide`; a box whose anchor is already off gets one issue."""
    width = text_width_px(MID_LINE, "Arial", 16.0)
    assert 250 < width < 400, width
    deck = _deck_with_text(tmp_path / "edge.pptx", masters_dir / "test-16x9.pptx",
                           [(Box(1100, 200, 150, 40), MID_LINE, 16.0),
                            (Box(1400, 300, 150, 40), MID_LINE, 16.0)], wrap=False)
    report = fit(deck, [])
    by_shape = _issues_by_shape(report)
    assert sorted(by_shape.values()) == [["off-slide"], ["off-slide"]], report.issues
    line_rule, anchor_rule = report.issues
    assert "past the right edge" in line_rule.detail
    assert f"runs {1100 + 9.6 + width - 1280:.0f}px past" in line_rule.detail
    assert "its text lands off" in anchor_rule.detail and "past the" not in anchor_rule.detail


def test_fit_accounts_for_the_bullet_indent_in_the_line_extent(tmp_path: Path, masters_dir: Path) -> None:
    """With a hanging bullet every line starts at `marL`: 18 px less room on the line."""
    master = masters_dir / "test-16x9.pptx"
    text = "A bulleted line that nearly fills its box"
    width = text_width_px(text, "Arial", 16.0)
    box = Box(64, 64, width + 2 * 0.1 * config.PX_PER_IN + 8.0, 40)    # 8 px to spare without the bullet
    bulleted = _deck_with_text(tmp_path / "bullet.pptx", master, [(box, text, 16.0)], wrap=False,
                               mar_l_px=18.0)
    report = fit(bulleted, [])
    assert [i.kind for i in report.issues] == ["overhang"], report.issues
    assert report.details[0]["headroomPx"] == pytest.approx(8.0 - 18.0, abs=0.1)

    plain = _deck_with_text(tmp_path / "plain.pptx", master, [(box, text, 16.0)], wrap=False)
    report = fit(plain, [])
    assert report.issues == [], report.issues
    assert report.details[0]["headroomPx"] == pytest.approx(8.0, abs=0.1)


def test_fit_skips_the_line_extent_rule_for_rotated_text(tmp_path: Path, masters_dir: Path) -> None:
    """A rotated box's extent is not where its un-rotated box says: `overhang`, never a line `off-slide`."""
    deck = _deck_with_text(tmp_path / "rotated.pptx", masters_dir / "test-16x9.pptx",
                           [(Box(1200, 200, 60, 300), MID_LINE, 16.0)], wrap=False, rotation=270.0)
    kinds = [i.kind for i in fit(deck, []).issues]
    assert "overhang" in kinds and "off-slide" not in kinds, kinds


def test_fit_line_extent_follows_the_paragraph_alignment(tmp_path: Path, masters_dir: Path) -> None:
    """A wrap-off line grows the way its paragraph is aligned: right-aligned leftwards, centred both ways."""
    master = masters_dir / "test-16x9.pptx"
    width = text_width_px(MID_LINE, "Arial", 16.0)
    right = _deck_with_text(tmp_path / "right.pptx", master, [(Box(10, 100, 150, 40), MID_LINE, 16.0)],
                            wrap=False, align="right")
    [issue] = fit(right, []).issues                     # its anchor (the right edge, x 160) is on the slide
    assert issue.kind == "off-slide" and "past the left edge" in issue.detail
    assert f"runs {width - (160 - 9.6):.0f}px past" in issue.detail

    centred = _deck_with_text(tmp_path / "centred.pptx", master,
                              [(Box(1150, 100, 120, 40), MID_LINE, 16.0),
                               (Box(500, 300, 120, 40), MID_LINE, 16.0)],
                              wrap=False, align="center")
    first, second = fit(centred, []).issues
    assert first.kind == "off-slide" and "past the right edge" in first.detail
    assert second.kind == "overhang", "a centred line well inside the canvas only overhangs its box"


def test_fit_is_clean_on_every_emitter_fixture(tmp_path: Path) -> None:
    """The emitter's own output never overhangs where the fonts match: 0 issues, every line measured."""
    from tests.engine.emit.test_emit import ALL_FIXTURES, emit_fixture, load_ir

    checked = 0
    for name in ALL_FIXTURES:
        out, report, ir, _, _ = emit_fixture(name, tmp_path)
        assert report.warnings == [], (name, report.warnings)
        fitted = fit(out, [ir])
        assert fitted.issues == [], (name, fitted.issues)
        for detail in (d for d in fitted.details if "wrap" in d):
            assert detail["wrap"] is False and detail["measured"] is True, (name, detail)
            assert detail["headroomPx"] >= 0, (name, detail)
            checked += 1
    assert checked == sum(1 for name in ALL_FIXTURES
                          for element in load_ir(name).elements if element.kind == "text")


def test_fit_on_emit_wrap_reports_the_title_running_off_the_slide(tmp_path: Path, monkeypatch) -> None:
    """Exported blind (fidelity F4) and checked with the fonts back: the title's line 1 runs off the slide.

    The export could not see Arial, so it boxed line 1 at the browser's 948 px; drawn in Arial Bold it
    is 1316 px from x 24 — 60 px past the canvas. Exactly that is reported, once; the other three
    elements are clean.
    """
    from tests.engine.emit.test_emit import _blind_fonts, emit_fixture

    with _blind_fonts(monkeypatch, tmp_path):
        out, _, ir, _, _ = emit_fixture("emit-wrap", tmp_path)
    report = fit(out, [ir])
    assert _issues_by_shape(report) == {"title-edge": ["off-slide"]}, report.issues
    assert "past the right edge" in report.issues[0].detail
    details = {detail["shape"]: detail for detail in report.details}
    title = details["title-edge"]
    line_one = ir.elements[0].paragraphs[0]["lines"][0]["runs"][0]["text"]
    assert title["headroomPx"] < 0 and title["widestLine"] == 1
    assert title["widestLinePx"] == pytest.approx(text_width_px(line_one, "Arial", 31.0, bold=True), rel=0.01)
    for name in ("body-column", "centred-card", "chip"):
        detail = details[name]
        assert detail["measured"] and detail["wrap"] is False and detail["headroomPx"] >= 0, detail


def test_fit_details_name_missing_and_synthetic_fonts(tmp_path: Path, masters_dir: Path, monkeypatch) -> None:
    """With only `arial.ttf` installed: a bold Arial run is a synthesised bold, an unknown family missing.

    Unmeasured widths never produce an overhang — a guess is not a prediction.
    """
    from tests.engine.emit.test_emit import _forget_fonts

    if not _installed("Arial"):
        pytest.skip("Arial is not installed here (its metric-compatible stand-in has other bold faces)")
    arial = emitter_face_for("Arial")
    assert arial is not None and arial.weight == 400
    deck = _deck_with_text(tmp_path / "fonts.pptx", masters_dir / "test-16x9.pptx",
                           [(Box(64, 64, 100, 40), "Bold Arial words that run long", 16.0)], wrap=False)
    presentation = Presentation(str(deck))
    paragraph = presentation.slides[0].shapes[0].text_frame.paragraphs[0]
    paragraph.runs[0].font.bold = True
    extra = paragraph.add_run()
    extra.text = " and more"
    extra.font.name = "No Such Family"
    extra.font.size = Pt(16.0 * config.PT_PER_PX)
    presentation.save(str(deck))

    fonts = tmp_path / "only-arial"
    fonts.mkdir()
    (fonts / arial.path.name).write_bytes(arial.path.read_bytes())
    try:
        with monkeypatch.context() as patch:
            patch.setattr(config, "FONT_DIRS", (fonts,))
            _forget_fonts()
            report = fit(deck, [])
    finally:
        _forget_fonts()
    [detail] = report.details
    assert detail["syntheticBold"] == ["Arial"]
    assert detail["missingFonts"] == ["No Such Family"]
    assert detail["measured"] is False
    assert report.issues == [], report.issues


def test_fit_measures_a_semibold_run_in_the_face_the_file_names(tmp_path: Path, masters_dir: Path) -> None:
    """Segoe UI 600 is written `Segoe UI Semibold`, `b="0"` (G-1's `emitted_face`, Peter #9), and fit
    measures that very file: a line that fills its box stays clean.

    Before G-1 the run was written `b="1"`, PowerPoint drew Segoe UI Bold — 3.5 % wider than the
    Semibold the browser measured — and this rule would have called it an overhang: F5's weight
    defect. Re-taken on WP-D's base: no issue survives G-1.
    """
    from app.engine.emit import pptx as emit_module
    from app.engine.reports import EmitOptions
    from tests.engine.helpers import require_faces

    require_faces("Segoe UI")                          # Windows-only; its Semibold face is the subject
    semibold = emitter_face_for("Segoe UI", 600)
    assert semibold is not None and semibold.weight == 600, "Segoe UI Semibold is not installed"
    text = "Semibold is narrower than Bold by a few per cent here"
    width = emitter_width_px(text, "Segoe UI", 20.0, 600, False)
    element = Element(
        kind="text", box=Box(64, 64, width, 28), name="semibold", anchor="top",
        paragraphs=[{"align": "left", "lineHeightPx": 28, "spaceBeforePx": 0, "spaceAfterPx": 0,
                     "bullet": None,
                     "lines": [{"box": {"x": 64, "y": 64, "w": width, "h": 28},
                                "runs": [{"text": text, "font": "Segoe UI", "sizePx": 20.0, "weight": 600,
                                          "italic": False, "color": "2E2E38"}]}]}],
    )
    ir = IR(canvas=Canvas(1280, 720), slide=Slide(id="semibold", layoutId="layout-07"),
            elements=assign_ids_and_z([element]))
    manifest = Manifest.load(masters_dir / "test-16x9.manifest.json")
    out = tmp_path / "semibold.pptx"
    emit_module.emit([ir], manifest, masters_dir / "test-16x9.pptx", out, EmitOptions())

    rPr = Presentation(str(out)).slides[0].shapes[0].text_frame.paragraphs[0].runs[0]._r.get_or_add_rPr()
    assert (rPr.find(qn("a:latin")).get("typeface"), rPr.get("b")) == ("Segoe UI Semibold", "0")
    report = fit(out, [ir])
    assert report.issues == [], report.issues
    [detail] = report.details
    assert detail["widestLinePx"] == pytest.approx(width, rel=0.005) and detail["headroomPx"] >= 0
    bold = text_width_px(text, "Segoe UI", 20.0, bold=True)
    assert bold > width * 1.02, "Bold would overhang the 2 % slack: the case F5 was"


def _table_deck(path: Path, master: Path, *, indent_px: float, first_before_px: float,
                second_before_px: float, last_after_px: float, column_px: float, row_px: float,
                margin_px: float = 0.0) -> Path:
    """One 1×1 table: a paragraph that fits the column only without its first-line indent (and its
    `marL`), then a short paragraph; exact 20 px lines, 4 px top and bottom margins, spacing as given."""
    from lxml import etree

    presentation = Presentation(str(master))
    slide = presentation.slides.add_slide(presentation.slide_masters[0].slide_layouts[6])
    frame = slide.shapes.add_table(1, 1, Emu(64 * PX), Emu(64 * PX), Emu(round(column_px * PX)),
                                   Emu(round(row_px * PX)))
    table = frame.table
    table.columns[0].width = Emu(round(column_px * PX))
    table.rows[0].height = Emu(round(row_px * PX))
    cell = table.cell(0, 0)
    cell.margin_left = cell.margin_right = Emu(0)
    cell.margin_top = cell.margin_bottom = Emu(4 * PX)
    body = cell.text_frame._txBody
    for paragraph in body.findall(qn("a:p")):
        body.remove(paragraph)
    for text, margin, indent, before, after in (
            ("Indented first line of words", margin_px, indent_px, first_before_px, 0.0),
            ("x", 0.0, 0.0, second_before_px, last_after_px)):
        p = etree.SubElement(body, qn("a:p"))
        pPr = etree.SubElement(p, qn("a:pPr"), marL=str(round(margin * PX)), indent=str(round(indent * PX)))
        etree.SubElement(etree.SubElement(pPr, qn("a:lnSpc")), qn("a:spcPts"), val="1500")
        for tag, px in (("a:spcBef", before), ("a:spcAft", after)):
            if px:
                etree.SubElement(etree.SubElement(pPr, qn(tag)), qn("a:spcPts"), val=str(round(px * 75)))
        etree.SubElement(pPr, qn("a:buNone"))
        r = etree.SubElement(p, qn("a:r"))
        rPr = etree.SubElement(r, qn("a:rPr"), sz="1200")
        etree.SubElement(rPr, qn("a:latin"), typeface="Arial")
        etree.SubElement(r, qn("a:t")).text = text
    presentation.save(str(path))
    return path


def _row_needs(report) -> float | None:
    import re

    issue = next((i for i in report.issues if i.kind == "overflow" and "row 1" in i.shape), None)
    return float(re.search(r"needs (\d+)px", issue.detail).group(1)) if issue else None


def test_check_table_honours_first_line_indent_and_paragraph_spacing(tmp_path: Path, masters_dir: Path) -> None:
    """WP-A §7: a cell's first line wraps at `inner − indent`; `spcBef` counts for every paragraph but
    the first and `spcAft` for every one but the last (PowerPoint drops the frame's outer two)."""
    master = masters_dir / "test-16x9.pptx"
    width = text_width_px("Indented first line of words", "Arial", 16.0)
    assert width is not None
    column = width + 10.0                               # fits as it is, not after a 40 px indent

    plain = _table_deck(tmp_path / "plain.pptx", master, indent_px=0, first_before_px=0,
                        second_before_px=0, last_after_px=0, column_px=column, row_px=30)
    assert _row_needs(fit(plain, [])) == round(20 + 20 + 8)          # one line each + margins

    indented = _table_deck(tmp_path / "indented.pptx", master, indent_px=40, first_before_px=0,
                           second_before_px=0, last_after_px=0, column_px=column, row_px=30)
    assert _row_needs(fit(indented, [])) == round(2 * 20 + 20 + 8), "the indent wraps the first line"

    spaced = _table_deck(tmp_path / "spaced.pptx", master, indent_px=0, first_before_px=12,
                         second_before_px=10, last_after_px=14, column_px=column, row_px=30)
    assert _row_needs(fit(spaced, [])) == round(20 + 10 + 20 + 8), "only the inner spcBef counts"

    roomy = _table_deck(tmp_path / "roomy.pptx", master, indent_px=40, first_before_px=0,
                        second_before_px=10, last_after_px=0, column_px=column, row_px=80)
    report = fit(roomy, [])
    assert not [i for i in report.issues if i.kind == "overflow"], report.issues
    assert not [d for d in report.details if "tableGrowthPx" in d]

    # The kinds review's finding 4: a cell paragraph's own `marL` (a grid column, a margin) narrows
    # every line of it, and a negative first-line indent under it gives the first line room back.
    margined = _table_deck(tmp_path / "margined.pptx", master, indent_px=0, margin_px=40, first_before_px=0,
                           second_before_px=0, last_after_px=0, column_px=column, row_px=30)
    assert _row_needs(fit(margined, [])) == round(2 * 20 + 20 + 8), "marL wraps the paragraph"
    hanging = _table_deck(tmp_path / "hanging.pptx", master, indent_px=-40, margin_px=40, first_before_px=0,
                          second_before_px=0, last_after_px=0, column_px=column, row_px=30)
    assert _row_needs(fit(hanging, [])) == round(20 + 20 + 8), "marL + indent = 0: the first line fits"


def test_check_table_measures_a_theme_token_cell_in_its_theme_face(tmp_path: Path, masters_dir: Path) -> None:
    """Plan §16 #11: a cell run written `+mj-lt` (the emitter's token for the master's heading font)
    is measured in the deck's major face, as a text box's is (G-1's `fonts=`) — not in the minor
    face `_check_table` defaults to. On `test-16x9` both theme slots say Calibri; with the major
    slot rewritten to Arial Black the same cell needs a second line."""
    master = masters_dir / "test-16x9.pptx"
    text = "Indented first line of words"
    calibri, black = text_width_px(text, "Calibri", 16.0), text_width_px(text, "Arial Black", 16.0)
    assert calibri is not None and black is not None and black > calibri * 1.3, (calibri, black)
    column = calibri + 10.0
    deck = _table_deck(tmp_path / "token.pptx", master, indent_px=0, first_before_px=0, second_before_px=0,
                       last_after_px=0, column_px=column, row_px=30)
    presentation = Presentation(str(deck))
    [frame] = [shape for shape in presentation.slides[0].shapes if shape.has_table]
    frame.table.cell(0, 0)._tc.find(".//" + qn("a:latin")).set("typeface", "+mj-lt")
    presentation.save(str(deck))
    assert _row_needs(fit(deck, [])) == round(20 + 20 + 8), "Calibri: one line each + margins"
    themed = _with_theme_major(deck, "Arial Black")
    assert _row_needs(fit(themed, [])) == round(2 * 20 + 20 + 8), "Arial Black: the first cell line wraps"


# -------------------------------------------------------------------------------------- coverage


def test_coverage_counts_a_mixed_ir(sample_dir: Path) -> None:
    asset = sample_dir / "assets" / "asset-logo.png"
    assert asset.exists()
    elements = [
        Element(kind="shape", box=Box(0, 0, 10, 10), geometry={"type": "rect"},
                fill={"type": "solid", "color": "FF0000"}),
        _text_element(Box(10, 10, 100, 20), "hello", 12.0, lines=1),
        Element(kind="image", box=Box(0, 0, 50, 50), src=str(asset), fit="contain"),
        Element(kind="table", box=Box(0, 0, 100, 40), rows=1, cols=1, colWidthsPx=[100],
                rowHeightsPx=[40], cells=[{"r": 0, "c": 0}]),
        Element(kind="chart", box=Box(0, 0, 200, 100), spec={"type": "column", "categories": ["a"],
                                                             "series": [{"name": "s", "values": [1]}]},
                origin="authored", confidence=1.0),
        Element(kind="raster", box=Box(0, 0, 20, 20), src=str(asset), reason="svg filter: feTurbulence"),
    ]
    ir = _ir_with(elements)
    ir.diagnostics.append(Diagnostic(level="warn", source="svg#1", message="filter rasterised",
                                     elementId="e6"))

    report = coverage(
        [ir],
        EmitReport(renderer_gaps=["custDash drawn solid"]),
        ["warn: font 'Foo' substituted", "warn: our own shape is odd"],
        ["warn: font 'Foo' substituted"],
    )
    counts = report.per_slide[0]
    assert (counts["shape"], counts["text"], counts["image"], counts["table"], counts["chart"],
            counts["raster"]) == (1, 1, 1, 1, 1, 1)
    assert counts["chartsAuthored"] == 1 and counts["chartsRecognised"] == 0
    assert report.raster_count == 1
    assert report.rasters[0]["reason"].startswith("svg filter")
    assert report.renderer_warnings_ours == ["warn: our own shape is odd"]
    assert "custDash drawn solid" in report.unsupported
    assert len(report.diagnostics) == 1
    assert not report.clean                                   # a raster is never "clean coverage"


def test_template_check_passes_on_an_untouched_master(plain_export: Path, foreign_master: Path) -> None:
    """python-pptx re-serialises every part it opens, so the verdict is canonical XML, not bytes."""
    result = template_checks(plain_export, foreign_master, [])
    assert result["checked"] and result["ok"], result
    assert result["changed"] == [] and result["missing"] == []
    assert result["byteIdentical"] < result["parts"]           # the round trip really does rewrite
    assert result["relsReordered"], "relationship order is expected to move on a python-pptx round trip"


def test_template_check_catches_an_edited_layout(tmp_path: Path, plain_export: Path,
                                                 foreign_master: Path) -> None:
    import shutil
    import zipfile

    tampered = tmp_path / "tampered.pptx"
    shutil.copyfile(plain_export, tampered)
    with zipfile.ZipFile(tampered) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    target = "ppt/slideLayouts/slideLayout1.xml"
    entries[target] = entries[target].replace(b'name="Title Slide"', b'name="Tampered"', 1)
    with zipfile.ZipFile(tampered, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, blob in entries.items():
            archive.writestr(name, blob)

    result = template_checks(tampered, foreign_master, [])
    assert not result["ok"]
    assert target in result["changed"]


def test_template_check_pairs_the_title_placeholder_with_the_ir(plain_export: Path,
                                                                foreign_master: Path) -> None:
    mapped = _ir_with([_text_element(Box(64, 28, 1152, 62), "A title", 22.0, lines=1,
                                     placeholder={"type": "title", "idx": 0})])
    unmapped = _ir_with([_text_element(Box(64, 28, 1152, 62), "A title", 22.0, lines=1)],
                        slide_id="sld_two")

    both_mapped = template_checks(plain_export, foreign_master, [mapped, mapped])
    assert both_mapped["placeholderProblems"] == [], both_mapped["placeholderProblems"]

    one_unmapped = template_checks(plain_export, foreign_master, [mapped, unmapped])
    assert any("title placeholder" in problem for problem in one_unmapped["placeholderProblems"])


# ---------------------------------------------------------------------------- chart structure


def _chart_deck(path: Path, master: Path, box: Box, categories: list[str], values: list[float],
                plot: tuple[float, float, float, float] | None = None) -> Path:
    from pptx.oxml import parse_xml

    presentation = Presentation(str(master))
    slide = presentation.slides.add_slide(presentation.slide_masters[0].slide_layouts[6])
    data = CategoryChartData()
    data.categories = categories
    data.add_series("Series 1", values)
    frame = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED,
        Emu(round(box.x * PX)), Emu(round(box.y * PX)),
        Emu(round(box.w * PX)), Emu(round(box.h * PX)),
        data,
    )
    if plot is not None:
        namespace = "http://schemas.openxmlformats.org/drawingml/2006/chart"
        plot_area = frame.chart._chartSpace.find(f".//{{{namespace}}}plotArea")
        x, y, w, h = plot
        plot_area.insert(0, parse_xml(
            f'<c:layout xmlns:c="{namespace}"><c:manualLayout>'
            f'<c:layoutTarget val="inner"/>'
            f'<c:x val="{x}"/><c:y val="{y}"/><c:w val="{w}"/><c:h val="{h}"/>'
            f"</c:manualLayout></c:layout>"
        ))
    path.parent.mkdir(parents=True, exist_ok=True)
    presentation.save(str(path))
    return path


def _chart_ir(box: Box, categories: list[str], values: list[float], plot_rect: Box | None) -> IR:
    return _ir_with([
        Element(
            kind="chart", box=box, origin="authored", confidence=1.0, plotRect=plot_rect,
            spec={"type": "column", "categories": categories,
                  "series": [{"name": "Series 1", "values": values}]},
        )
    ])


def test_chart_structure_matches_the_spec(tmp_path: Path, masters_dir: Path) -> None:
    box = Box(64, 100, 600, 300)
    categories, values = ["FY21", "FY22", "FY23"], [40.0, 52.0, 57.0]
    deck = _chart_deck(tmp_path / "chart.pptx", masters_dir / "test-16x9.pptx", box, categories,
                       values, plot=(0.08, 0.05, 0.9, 0.8))
    plot_rect = Box(box.x + 0.08 * box.w, box.y + 0.05 * box.h, 0.9 * box.w, 0.8 * box.h)

    [entry] = chart_checks(deck, [_chart_ir(box, categories, values, plot_rect)])
    assert entry["found"] and entry["ok"], entry["problems"]
    assert entry["plotRectDeltaPx"] < 0.01
    assert entry["workbookColumns"] >= 2


def test_chart_structure_catches_a_wrong_value_and_a_displaced_plot_rect(
    tmp_path: Path, masters_dir: Path
) -> None:
    box = Box(64, 100, 600, 300)
    deck = _chart_deck(tmp_path / "chart2.pptx", masters_dir / "test-16x9.pptx", box,
                       ["a", "b"], [1.0, 2.0], plot=(0.08, 0.05, 0.9, 0.8))

    [wrong_value] = chart_checks(deck, [_chart_ir(box, ["a", "b"], [1.0, 99.0], None)])
    assert not wrong_value["ok"]
    assert any("point 1" in problem for problem in wrong_value["problems"])

    far = Box(box.x, box.y, 0.9 * box.w, 0.8 * box.h)          # 0.05 × 300 px = 15 px too high
    [displaced] = chart_checks(deck, [_chart_ir(box, ["a", "b"], [1.0, 2.0], far)])
    assert not displaced["ok"]
    assert displaced["plotRectDeltaPx"] > 4.0


# ------------------------------------------------------------------------------------------ gate


def _two_slide_deck(path: Path, master: Path, offset: float = 0.0) -> Path:
    """Two slides, one rectangle each. `offset` moves the first slide's rectangle."""
    presentation = Presentation(str(master))
    for x, y in [(200.0 + offset, 150.0), (500.0, 300.0)]:
        slide = presentation.slides.add_slide(presentation.slide_masters[0].slide_layouts[6])
        shape = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE, Emu(round(x * PX)), Emu(round(y * PX)),
            Emu(round(300 * PX)), Emu(round(200 * PX)),
        )
        shape.fill.solid()
        shape.fill.fore_color.rgb = RGBColor(0x1A, 0x9A, 0xFA)
        shape.line.fill.background()
        style = shape._element.find(qn("p:style"))
        if style is not None:
            shape._element.remove(style)
    path.parent.mkdir(parents=True, exist_ok=True)
    presentation.save(str(path))
    return path


@pytest.fixture(scope="module")
def gate_decks(tmp_path_factory, masters_dir: Path) -> dict[str, object]:
    """A deck, its own render (used as the reference) and a copy with one rectangle moved."""
    master = masters_dir / "test-16x9.pptx"
    assert master.exists(), f"{master} is missing (WP0b deliverable)"
    directory = tmp_path_factory.mktemp("gate")
    original = _two_slide_deck(directory / "original.pptx", master)
    shifted = _two_slide_deck(directory / "shifted.pptx", master, offset=40.0)
    references = renderer.render(original, 1280, directory / "reference")
    assert len(references) == 2
    return {"dir": directory, "original": original, "shifted": shifted, "references": references}


@pytest.mark.renderer_full
def test_gate_scores_a_deck_against_its_own_render_as_clean(gate_decks: dict) -> None:
    report = gate(gate_decks["original"], gate_decks["references"], [], gate_decks["dir"] / "self",
                  renderer=renderer.renderer())
    assert report.slides_checked == 2
    assert [slide.defect_area for slide in report.slides] == [0, 0]
    assert all(slide.clean for slide in report.slides)
    assert report.total_area == 0


@pytest.mark.renderer_full
def test_gate_sees_a_shifted_shape(gate_decks: dict) -> None:
    report = gate(gate_decks["shifted"], gate_decks["references"], [], gate_decks["dir"] / "shift",
                  renderer=renderer.renderer())
    assert report.slides[0].defect_area > 1000, report.slides[0]
    assert not report.slides[0].clean
    assert report.slides[1].defect_area == 0                   # slide 2 was not touched
    assert report.slides[0].diff_png is not None and Path(report.slides[0].diff_png).exists()


@pytest.mark.renderer_full
def test_gate_masks_are_painted_on_both_images(gate_decks: dict) -> None:
    """Masking the region that moved must take it out of the score — on both pictures, or the mask
    itself would be the defect."""
    mask = [[ReportBox(190, 140, 380, 220)], []]
    report = gate(gate_decks["shifted"], gate_decks["references"], mask, gate_decks["dir"] / "masked",
                  renderer=renderer.renderer())
    assert report.slides[0].defect_area > 1000                 # unmasked: still reported
    assert report.slides[0].non_chart_area == 0, report.slides[0]
    assert report.settings["masks"] == 1


@pytest.mark.renderer_full
def test_gate_asserts_every_reference_was_checked(gate_decks: dict, tmp_path: Path) -> None:
    """The CLI silently skips a slide whose render size differs from its reference (04-IR-FREEZE §3)."""
    from PIL import Image

    half = tmp_path / "half.png"
    with Image.open(gate_decks["references"][1]) as image:
        image.resize((image.width // 2, image.height // 2)).save(half)

    with pytest.raises(renderer.RendererError, match="checked 1 slides"):
        gate(gate_decks["original"], [gate_decks["references"][0], half], [], tmp_path / "mixed",
             renderer=renderer.renderer())


def test_gate_masks_accept_both_a_flat_list_and_one_list_per_slide() -> None:
    """Master brief §8 types `masks` as `list[Box]`; the suite needs per-slide masks, so both work."""
    from app.engine.verify.gate import _per_slide_masks

    box = ReportBox(0, 0, 10, 10)
    assert _per_slide_masks([box], 3) == [[box], [box], [box]]
    assert _per_slide_masks([[box], []], 2) == [[box], []]
    assert _per_slide_masks([[box]], 3) == [[box], [], []]
    assert _per_slide_masks([], 2) == [[], []]


@pytest.mark.renderer_full
def test_masked_deck_adds_one_rectangle_per_mask(gate_decks: dict, tmp_path: Path) -> None:
    deck = masked_deck(gate_decks["original"], [[ReportBox(10, 10, 100, 50)], []],
                       tmp_path / "masked.pptx")
    presentation = Presentation(str(deck))
    names = [[shape.name for shape in slide.shapes] for slide in presentation.slides]
    assert names[0].count("engine-gate-mask") == 1
    assert names[1].count("engine-gate-mask") == 0
    # The rectangle must carry no theme effects: PptxRender draws the theme's soft shadow otherwise
    # and the gate reports the shadow as a defect along every mask edge.
    mask = [s for s in presentation.slides[0].shapes if s.name == "engine-gate-mask"][0]
    assert mask._element.find(qn("p:style")) is None


# -------------------------------------------------------------------------------------- the suite


def test_build_masks_takes_chart_frames_from_the_ir_and_from_the_deck(
    tmp_path: Path, masters_dir: Path, sample_manifest: Manifest
) -> None:
    from app.engine.verify.suite import _Inputs

    box = Box(64, 100, 600, 300)
    deck = _chart_deck(tmp_path / "masks.pptx", masters_dir / "test-16x9.pptx", box, ["a"], [1.0])
    inputs = _Inputs(deck=deck, irs=[_chart_ir(box, ["a"], [1.0], None)], manifest=sample_manifest)
    masks = build_masks(inputs)
    assert len(masks) == 1
    # The IR chart and the deck's chart frame are the same rectangle; it is masked once, not twice.
    # (Plus the layout's own sldNum/dt/ftr zones, which every slide on it is masked by.)
    layout_masks = sample_manifest.layout("layout-01").masked_boxes()
    assert len(masks[0]) == 1 + len(layout_masks)
    charts = [mask for mask in masks[0] if mask.w == pytest.approx(600, abs=1)]
    assert len(charts) == 1 and charts[0].x == pytest.approx(64, abs=1)


def test_masks_for_matches_build_masks(masters_dir: Path) -> None:
    """The torture runner and the bench mask one slide exactly as the suite does (plan D7)."""
    from app.engine.verify.suite import _Inputs, masks_for

    manifest = Manifest.load(masters_dir / "test-16x9.manifest.json")
    masked_layout = next(layout for layout in manifest.layouts if layout.masked_boxes())
    box = Box(64, 100, 600, 300)
    chart = _chart_ir(box, ["a"], [1.0], None)
    chart.slide.layoutId = masked_layout.id
    plain = IR(canvas=Canvas(1280, 720), slide=Slide(id="plain", layoutId=manifest.layouts[0].id), elements=[])
    irs = [chart, plain]
    per_slide = build_masks(_Inputs(deck=None, irs=irs, manifest=manifest))
    assert per_slide == [masks_for(ir, manifest) for ir in irs]
    # the chart frame plus the layout's sldNum/dt/ftr zones — and nothing without a manifest
    assert len(per_slide[0]) == 1 + len(masked_layout.masked_boxes())
    assert masks_for(chart, None) == per_slide[0][:1]


def test_determinism_compares_two_exports(tmp_path: Path, masters_dir: Path) -> None:
    """`--determinism` re-runs the caller's own export twice and compares sha256 (master brief §9)."""
    import shutil

    from app.engine.reports import ExportResult
    from app.engine.verify.suite import check_determinism

    master = masters_dir / "test-16x9.pptx"
    stable = _deck_with_text(tmp_path / "stable.pptx", master, [(Box(0, 0, 100, 20), "x", 12.0)])
    drifting = _deck_with_text(tmp_path / "drifting.pptx", master, [(Box(0, 0, 100, 20), "y", 12.0)])

    def rerun_same(target: Path) -> ExportResult:
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(stable, target / "deck.pptx")
        return ExportResult(pptx=target / "deck.pptx")

    def rerun_different(target: Path) -> ExportResult:
        target.mkdir(parents=True, exist_ok=True)
        source = stable if target.name == "a" else drifting
        shutil.copyfile(source, target / "deck.pptx")
        return ExportResult(pptx=target / "deck.pptx")

    identical = check_determinism(ExportResult(pptx=stable, rerun=rerun_same), tmp_path / "same")
    assert identical.checked and identical.identical
    assert identical.sha256[0] == identical.sha256[1]

    differs = check_determinism(ExportResult(pptx=stable, rerun=rerun_different), tmp_path / "differs")
    assert differs.checked and differs.identical is False

    not_checked = check_determinism(ExportResult(pptx=stable), tmp_path / "none")
    assert not not_checked.checked and not_checked.identical is None


@pytest.mark.validator
def test_run_validator_reports_a_clean_deck(sample_export, sample_master: Path) -> None:
    report = run_validator(sample_export.pptx, sample_master)
    assert report.skipped is None, report.skipped
    assert report.ok, report.errors
    assert report.elapsed_s > 0


@pytest.mark.renderer_full
def test_suite_verifies_a_deck_end_to_end(tmp_path: Path, sample_dir: Path, sample_export) -> None:
    """The acceptance run, verify-only, on the engine's export of the sample project: full report set.

    (Slide Studio ran this on a client's Path B export against recorded areas; that baseline is not
    ported. Here the gate must run on both slides, and masking can only lower a slide's number.)
    """
    from app.engine.verify.suite import run_suite

    report = run_suite(
        None, tmp_path / "suite", pptx=sample_export.pptx, project_dir=sample_dir,
        renderer=renderer.renderer(), validate=False,
    )
    assert report.project_id == sample_dir.name
    assert report.fit is not None and report.fit.slides_checked == 2
    assert report.gate is not None and report.gate.slides_checked == 2
    assert report.gate.source == "pptxrender"
    assert len(report.lint) == 2
    assert report.coverage is not None and report.coverage.template["checked"]
    for slide in report.gate.slides:
        assert (slide.non_chart_area or 0) <= slide.defect_area

    for name in ("report.json", "report.md", "compare-01.png", "compare-02.png"):
        assert (tmp_path / "suite" / name).exists(), name
    data = json.loads((tmp_path / "suite" / "report.json").read_text(encoding="utf-8"))
    assert data["gate"]["slides_checked"] == 2
    assert data["validation"]["skipped"] == "--no-validate"


def test_suite_without_a_renderer_runs_every_other_row(tmp_path: Path, sample_dir: Path, sample_export) -> None:
    """No renderer (the deployed PptxRender cannot score): the gate says it did not run, and lint,
    fit, coverage and the template check still run on the deck."""
    from app.engine.verify.suite import run_suite

    report = run_suite(None, tmp_path / "suite", pptx=sample_export.pptx, project_dir=sample_dir,
                       validate=False)
    assert report.gate is not None and report.gate.slides_checked == 0
    assert report.gate.source == "none" and "did not run" in " ".join(report.gate.warnings)
    assert any("pixel gate did not run" in note for note in report.notes)
    assert len(report.lint) == 2 and all(lint_report.errors == [] for lint_report in report.lint)
    assert report.fit is not None and report.fit.slides_checked == 2
    assert report.coverage is not None and report.coverage.template["checked"]
    assert "gate" not in report.gate_results            # a row that did not run is absent, not passed
    assert (tmp_path / "suite" / "report.json").exists() and (tmp_path / "suite" / "report.md").exists()
    # The verify-only run measured the slide HTML in the suite's own workspace, not a global folder.
    assert (tmp_path / "suite" / "work").is_dir()


def test_suite_with_no_project_reports_why_nothing_ran(tmp_path: Path) -> None:
    from app.engine.verify.suite import run_suite

    report = run_suite(None, tmp_path / "suite", pptx=tmp_path / "missing.pptx", validate=False)
    assert any("no project_dir" in note for note in report.notes)
    assert any("no deck to verify" in note for note in report.notes)
    assert not report.passed


# ------------------------------------------------------------------- the reference composite


def test_reference_is_rendered_in_the_masters_fonts(tmp_path: Path) -> None:
    """F6: the gate's reference is drawn in the fonts the extractor measured with, not in Arial.

    The `nowrap` line of `text.html` on test-16x9 (Calibri): its ink in the reference covers its IR
    line box to within 2 px. The same slide forced to Arial — what every reference was before —
    comes out more than 4 % wider, which is how wrong every Calibri gate number used to be.
    """
    from PIL import Image

    from app.engine.extract.html import render_reference
    from app.engine.verify.fixtures import context_for
    from tests.engine.helpers import extract_html

    html = config.TORTURE_FIXTURE / "text.html"
    context = context_for(html)
    assert context.manifest.fonts == {"major": "Calibri Light", "minor": "Calibri"}
    ir = extract_html(html, context.manifest, context.layout_id, context.assets_dir, slide_id="text")
    line = next(e for e in ir.elements if e.name == "nowrap").paragraphs[0]["lines"][0]
    box = Box.from_json(line["box"])
    search = Box(box.x - 8, box.y, box.w + 80, box.h)

    widths = {}
    for label, fonts in (("masters", context.manifest.fonts), ("arial", {"major": "Arial", "minor": "Arial"})):
        reference = render_reference(html, context.layout_png, tmp_path / f"{label}.png",
                                     assets_dir=context.assets_dir, fonts=fonts)
        with Image.open(reference) as image:
            extent = _ink_extent(image, search)
        assert extent is not None, f"no ink for the nowrap line in the {label} reference"
        widths[label] = extent.w
    assert abs(widths["masters"] - box.w) <= 2.0, (widths, box.w)
    assert widths["arial"] > box.w * 1.04, (widths, box.w)


# ------------------------------------------------------------------------ the text row (WP-G, D6)
#
# The row reads the exported file against the IR it came from, so every case here is a synthetic IR
# emitted through the real emitter on test-16x9: the deck is what Path A writes, and the IR carries
# the browser's geometry (line boxes and, where given, the per-run boxes `x-run-boxes`).


def _row_run(text: str, *, size: float = 16.0, weight: int = 400, font: str = "Calibri",
             baseline: str = "normal") -> dict:
    return {"text": text, "font": font, "sizePx": size, "weight": weight, "italic": False,
            "color": "2E2E38", "alpha": 1, "underline": False, "strike": False, "letterSpacingPx": 0,
            "baseline": baseline, "baselineShiftPx": 0, "caps": False, "href": None}


def _row_line(runs: list[dict], x: float, y: float, gaps: Sequence[float] = (),
              height: float = 22.0) -> tuple[dict, list[list[float]]]:
    """A browser line: the runs side by side, `gaps[i]` px of layout after run *i*; plus run boxes."""
    boxes, cursor = [], x
    for index, run in enumerate(runs):
        width = emitter_width_px(run["text"], run["font"], run["sizePx"], run["weight"], False)
        boxes.append([cursor, cursor + width])
        cursor += width + (gaps[index] if index < len(gaps) else 0.0)
    return {"box": {"x": x, "y": y, "w": cursor - x, "h": height}, "runs": runs}, boxes


def _row_element(name: str, paragraphs: list[list[tuple[dict, list]]], *, boxes: bool = True,
                 align: str = "left", path: str | None = None) -> Element:
    lines = [line for paragraph in paragraphs for line, _ in paragraph]
    x = min(line["box"]["x"] for line in lines)
    y = min(line["box"]["y"] for line in lines)
    right = max(line["box"]["x"] + line["box"]["w"] for line in lines)
    bottom = max(line["box"]["y"] + line["box"]["h"] for line in lines)
    return Element(
        kind="text", box=Box(x, y, right - x, bottom - y), name=name,
        source={"path": path or f"body > div.{name}", "tag": "div"},
        paragraphs=[{"align": align, "lineHeightPx": 22.0, "spaceBeforePx": 0, "spaceAfterPx": 0,
                     "bullet": None, "lines": [line for line, _ in paragraph]}
                    for paragraph in paragraphs],
        extras={"x-run-boxes": [[run_boxes for _, run_boxes in paragraph] for paragraph in paragraphs]}
        if boxes else {},
        anchor="top", writingMode="horizontal", wrap=True)


def _row_deck(tmp_path: Path, elements: list[Element], name: str = "row") -> tuple[Path, list[IR]]:
    from app.engine.emit import pptx as emitter
    from app.engine.reports import EmitOptions

    assign_ids_and_z(elements)
    ir = IR(canvas=Canvas(1280, 720), slide=Slide(id=name, layoutId="layout-07"), elements=elements)
    assert ir.validate() == [], ir.validate()
    manifest = Manifest.load(config.MASTERS_FIXTURE / "test-16x9.manifest.json")
    deck = tmp_path / f"{name}.pptx"
    emitter.emit([ir], manifest, config.MASTERS_FIXTURE / "test-16x9.pptx", deck, EmitOptions())
    return deck, [ir]


def _joins_found(found: list[dict]) -> list[tuple[str, str]]:
    return [(j["left"], j["right"]) for entry in found for j in entry["joins"]]


def test_text_collisions_find_runs_that_read_as_one_word(tmp_path: Path) -> None:
    """The hand-off's P0: a chip beside a heading, a label column beside its text — by their gap."""
    heading = _row_line([_row_run("WHERE VALUE COMES FROM", weight=700), _row_run("ILLUSTRATIVE", size=12)],
                        60, 100, gaps=[8.0])
    label = _row_line([_row_run("INSIGHT", size=11, weight=700), _row_run("Revenue value is a product of size")],
                      60, 160, gaps=[40.0])
    deck, irs = _row_deck(tmp_path, [_row_element("heading", [[heading]]), _row_element("label", [[label]])])
    found = text_collisions(deck, irs)
    # each side of a join is quoted to 24 characters, like the alnum rule's `reads`
    assert _joins_found(found) == [("WHERE VALUE COMES FROM", "ILLUSTRATIVE"),
                                   ("INSIGHT", "Revenue value is a produ")]
    assert {entry["method"] for entry in found} == {"boxes"}
    assert found[0]["slide"] == 1 and found[0]["gapPx"] == pytest.approx(8.0, abs=0.01)


def test_text_collisions_are_silent_on_correct_output(tmp_path: Path) -> None:
    spaced = _row_line([_row_run("Inline chips "), _row_run("Baseline"), _row_run(" sit in the line")], 60, 100)
    first = _row_line([_row_run("first line")], 60, 140)
    second = _row_line([_row_run("second line")], 60, 162)
    scripts = _row_line([_row_run("base"), _row_run("sup", size=10, baseline="super"), _row_run(" base"),
                         _row_run("sub", size=10, baseline="sub")], 60, 200)
    seam = _row_line([_row_run("Revenue"), _row_run(""), _row_run("(SAR bn)")], 60, 240)
    deck, irs = _row_deck(tmp_path, [
        _row_element("spaced", [[spaced]]), _row_element("broken", [[first, second]]),
        _row_element("scripts", [[scripts]]), _row_element("seam", [[seam]]),
    ])
    assert text_collisions(deck, irs) == []


def _table_with_cell(runs: list[dict], width: float) -> Element:
    return Element(
        kind="table", box=Box(60, 300, 400, 40), name="tax-table",
        source={"path": "body > table.tax", "tag": "table"}, rows=1, cols=1,
        colWidthsPx=[400.0], rowHeightsPx=[40.0],
        cells=[{"r": 0, "c": 0, "rowSpan": 1, "colSpan": 1, "fill": None,
                "borders": {"top": None, "right": None, "bottom": None, "left": None},
                "paddingPx": {"t": 7, "r": 10, "b": 7, "l": 10}, "valign": "top",
                "paragraphs": [{"align": "left", "lineHeightPx": 20.0, "spaceBeforePx": 0.0,
                                "spaceAfterPx": 0.0, "bullet": None,
                                "lines": [{"box": {"x": 70, "y": 307, "w": width, "h": 20},
                                           "runs": runs}]}]}])


def test_text_collisions_look_inside_table_cells_and_skip_empty_runs(tmp_path: Path) -> None:
    runs = [_row_run("TAX", weight=700), _row_run(""), _row_run("Incentive design")]
    advances = sum(emitter_width_px(r["text"], r["font"], r["sizePx"], r["weight"], False) for r in runs)
    deck, irs = _row_deck(tmp_path, [_table_with_cell(runs, advances + 24.0)])
    found = text_collisions(deck, irs)
    assert _joins_found(found) == [("TAX", "Incentive design")]
    assert found[0]["method"] == "predicted" and found[0]["shape"].endswith("r0c0")


def _cell_table(name: str, y: float, runs: list[dict], gaps: Sequence[float], *, wider: float = 0.0,
                boxes: bool = True) -> Element:
    """A one-cell table whose line is `_row_line`'s (the runs, `gaps[i]` px of layout after run *i*),
    `wider` px of it inside the last run (word spacing: width that is not a gap between runs), with
    the run boxes where WP-A's cell walk keeps them — `x-wpa.cells["0:0"].runBoxes` — when `boxes`."""
    line, run_boxes = _row_line(runs, 70, y + 7, gaps=gaps, height=20.0)
    line["box"]["w"] += wider
    run_boxes[-1][1] += wider
    table = _table_with_cell(runs, line["box"]["w"])
    cell = table.cells[0]
    cell["paragraphs"][0]["lines"] = [line]
    extras = {"x-wpa": {"v": 1, "cells": {"0:0": {"flow": "text", "runBoxes": [[run_boxes]]}}}} if boxes else {}
    return table.replace(name=name, box=Box(60, y, 400, 40), source={"path": f"body > table.{name}", "tag": "table"},
                         extras=extras)


def test_text_collisions_judge_a_cell_by_its_run_boxes(tmp_path: Path) -> None:
    """Plan §16 #22: a cell carries its browser run boxes (WP-A, `x-wpa` `runBoxes`), so its joins are
    judged by their own gap, as a text shape's are, and predicted widths are only the fallback.

    Both ways the two methods part: a 2.5 px layout gap between `Revenue` and `2023` is a collision
    (above `COLLISION_MIN_PX`) the prediction cannot see (below `PREDICTION_MIN_PX`); a styled word
    `Re|venue` in a word-spaced line (12 px of spacing inside its second run) touches, while the
    predicted residual blames the join for the spacing."""
    revenue = [_row_run("Revenue"), _row_run("2023")]
    styled = [_row_run("Re", weight=700), _row_run("venue grew by a third")]
    with_boxes = [_cell_table("gap", 100, revenue, [2.5]), _cell_table("spaced", 200, styled, [0.0], wider=12.0)]
    deck, irs = _row_deck(tmp_path, with_boxes, name="cells-boxes")
    found = text_collisions(deck, irs)
    assert _joins_found(found) == [("Revenue", "2023")]
    assert found[0]["method"] == "boxes" and found[0]["shape"].endswith("r0c0")
    assert found[0]["gapPx"] == pytest.approx(2.5, abs=0.01)

    predicted = [_cell_table("gap", 100, revenue, [2.5], boxes=False),
                 _cell_table("spaced", 200, styled, [0.0], wider=12.0, boxes=False)]
    deck, irs = _row_deck(tmp_path, predicted, name="cells-predicted")
    found = text_collisions(deck, irs)
    assert _joins_found(found) == [("Re", "venue grew by a third")]
    assert found[0]["method"] == "predicted" and found[0]["gapPx"] == pytest.approx(12.0, abs=0.5)


def test_a_cell_line_without_matching_boxes_falls_back_to_predicted_widths(tmp_path: Path) -> None:
    """Boxes that do not describe the line (a run count that differs: a trimmed or appended run) are
    no boxes: that line is predicted, as a text shape's would be."""
    runs = [_row_run("TAX", weight=700), _row_run("Incentive design")]
    table = _cell_table("stale", 100, runs, [24.0])
    table.extras["x-wpa"]["cells"]["0:0"]["runBoxes"] = [[[[70.0, 100.0]]]]
    deck, irs = _row_deck(tmp_path, [table])
    found = text_collisions(deck, irs)
    assert _joins_found(found) == [("TAX", "Incentive design")] and found[0]["method"] == "predicted"


def test_an_extracted_cell_join_is_found_by_its_run_boxes(tmp_path: Path) -> None:
    """End to end: WP-A's cell walk writes the run boxes, the emitter writes the cell, and the text
    row reads the one through the other. `Revenue<span style="margin-left:2.5px">2023</span>` in a
    cell is split by WP-B's R6 at the seam. Since r1a (plan §16 #25) R6 carries the 2.5 px on a
    space sized to it, so the file keeps the gap and the row finds no join; with that space taken
    back out — the glued shape R6 wrote before — the gap is found by the boxes and missed by the
    prediction (the same IR with the boxes taken out finds nothing)."""
    from app.engine.emit import pptx as emitter
    from app.engine.reports import EmitOptions
    from app.engine.verify.fixtures import context_for
    from tests.engine.helpers import extract_html

    html = tmp_path / "cell-gap.html"
    html.write_text(
        '<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;width:1280px;height:720px;'
        'overflow:hidden;font-family:Arial,sans-serif;color:#1d2433} td{padding:6px 10px}</style></head><body>'
        '<table style="position:absolute;left:40px;top:40px;border-collapse:collapse;font-size:14px">'
        '<tr><td>Revenue<span style="margin-left:2.5px">2023</span></td><td>Plain words</td></tr></table>'
        '</body></html>', encoding="utf-8")
    (tmp_path / "cell-gap.expect.json").write_text('{"master": "test-16x9", "layoutId": "layout-06"}',
                                                   encoding="utf-8")
    context = context_for(html)
    ir = extract_html(html, context.manifest, context.layout_id, context.assets_dir, slide_id="cell-gap")
    (table,) = ir.of_kind("table")
    [[[left, space, right]]] = table.extras["x-wpa"]["cells"]["0:0"]["runBoxes"]
    assert right[0] - left[1] == pytest.approx(2.5, abs=0.05)
    assert (space[0], space[1]) == (left[1], right[0])
    cell = next(c for c in table.cells if (c["r"], c["c"]) == (0, 0))
    line = cell["paragraphs"][0]["lines"][0]
    assert [r["text"] for r in line["runs"]] == ["Revenue", " ", "2023"]

    def collisions(slide: IR, name: str) -> list[dict]:
        deck = tmp_path / f"{name}.pptx"
        emitter.emit([slide], context.manifest, context.master, deck, EmitOptions())
        return text_collisions(deck, [slide])

    assert collisions(ir, "carried") == []
    del line["runs"][1]
    del table.extras["x-wpa"]["cells"]["0:0"]["runBoxes"][0][0][1]
    found = collisions(ir, "with-boxes")
    assert _joins_found(found) == [("Revenue", "2023")]
    assert found[0]["method"] == "boxes" and found[0]["shape"].endswith("r0c0")
    assert found[0]["gapPx"] == pytest.approx(2.5, abs=0.05)
    for cell in table.extras["x-wpa"]["cells"].values():
        cell.pop("runBoxes", None)
    assert collisions(ir, "predicted") == []


@pytest.mark.parametrize("case, runs, gaps, expected", [
    ("found", ["WHERE VALUE COMES FROM", "ILLUSTRATIVE"], [6.0], [("WHERE VALUE COMES FROM", "ILLUSTRATIVE")]),
    ("space between", ["COMES FROM ", "ILLUSTRATIVE"], [6.0], []),
    ("digit-led chip", ["marketing-led partner", "2023"], [8.0], [("marketing-led partner", "2023")]),
    ("money then change", ["$4.2B", "+12%"], [10.0], [("$4.2B", "+12%")]),
    ("styled word", ["Re", "venue"], [0.0], []),
    ("superscript", ["12", "th"], [0.2], []),
])
def test_text_row_finds_a_collision(tmp_path: Path, case: str, runs: list[str], gaps: list[float],
                                    expected: list[tuple[str, str]]) -> None:
    """The boxes path: a join is judged by its own browser gap, never by what its characters are."""
    line = _row_line([_row_run(text) for text in runs], 60, 100, gaps=gaps)
    deck, irs = _row_deck(tmp_path, [_row_element("case", [[line]])], name=case.replace(" ", "-"))
    found = text_collisions(deck, irs)
    assert _joins_found(found) == expected
    assert all(entry["method"] == "boxes" for entry in found)


def test_text_row_does_not_join_across_a_break(tmp_path: Path) -> None:
    """Two lines of one paragraph meet at an `a:br`: the break is the gap, whatever the geometry."""
    first = _row_line([_row_run("…marketing-led"), _row_run("partner")], 60, 100, gaps=[0.0])
    second = _row_line([_row_run("2023")], 60, 122)
    deck, irs = _row_deck(tmp_path, [_row_element("wrapped", [[first, second]])])
    assert text_collisions(deck, irs) == []


def test_text_row_falls_back_to_predicted_widths(tmp_path: Path) -> None:
    """Without run boxes the gap is the line's residual: measured width − predicted advances."""
    glued = _row_line([_row_run("…marketing-led partner"), _row_run("2023")], 60, 100, gaps=[12.0])
    tight = _row_line([_row_run("Re", weight=700), _row_run("venue grew")], 60, 140, gaps=[0.5])
    stretched = _row_line([_row_run("Justified"), _row_run("line")], 60, 180, gaps=[30.0])
    last = _row_line([_row_run("last line")], 60, 202)
    absent = _row_line([_row_run("Lexend", font="Lexend"), _row_run("text", font="Lexend")], 60, 240, gaps=[20.0])
    deck, irs = _row_deck(tmp_path, [
        _row_element("glued", [[glued]], boxes=False),
        _row_element("tight", [[tight]], boxes=False),
        _row_element("justified", [[stretched, last]], boxes=False, align="justify"),
        _row_element("absent", [[absent]], boxes=False),
    ])
    report = text_report(deck, irs)
    assert _joins_found(report["collisions"]) == [("…marketing-led partner", "2023")]
    assert report["collisions"][0]["method"] == "predicted"
    assert report["collisions"][0]["gapPx"] == pytest.approx(12.0, abs=0.5)
    # the stretched justified line and the absent face cannot be predicted: counted, not guessed
    assert report["skipped"] >= 2


def _move_shape(deck: Path, name: str, *, dy_px: float = 0.0, rotation: float | None = None) -> None:
    presentation = Presentation(str(deck))
    for shape in presentation.slides[0].shapes:
        if shape.name == name:
            shape.top = int(shape.top + dy_px * PX)
            if rotation is not None:
                shape.rotation = rotation
    presentation.save(str(deck))


def test_text_row_finds_an_export_introduced_overlap(tmp_path: Path) -> None:
    """Deck bands rebuilt from the file against the design's bands; only the export's excess counts."""
    title = _row_element("title", [[_row_line([_row_run("A title that sits on top", size=28)], 60, 60,
                                              height=34.0)]])
    body = _row_element("body", [[_row_line([_row_run("Body copy one line below the title")], 60, 100)]])
    # designed over each other: an author's overlap, reported by text_layout, not by the export row
    over_a = _row_element("over-a", [[_row_line([_row_run("Designed overlap one")], 600, 300)]])
    over_b = _row_element("over-b", [[_row_line([_row_run("Designed overlap two")], 600, 306)]])
    # nested: the inner element's source path is inside the outer's
    outer = _row_element("outer", [[_row_line([_row_run("Outer card text")], 60, 400)]],
                         path="body > div.card")
    inner = _row_element("inner", [[_row_line([_row_run("Inner card text")], 60, 404)]],
                         path="body > div.card > p")
    deck, irs = _row_deck(tmp_path, [title, body, over_a, over_b, outer, inner])

    clean = text_report(deck, irs)
    assert clean["exportOverlaps"] == [] and clean["unmatched"] == 0

    # A faithful export rebuilds the design's bands: every shape's band within a pixel of the IR's.
    from app.engine.verify.fit import _theme_fonts_of
    from app.engine.verify.text_deck import deck_bands
    from app.engine.verify.text_layout import ink_bands

    slide = Presentation(str(deck)).slides[0]
    fonts = _theme_fonts_of(slide, irs[0].fonts.forced)
    for shape in slide.shapes:
        element = next(e for e in irs[0].elements if e.name == shape.name)
        for rebuilt, designed in zip(deck_bands(shape, fonts), ink_bands(element), strict=False):
            assert rebuilt[2] == pytest.approx(designed[2], abs=1.0), shape.name
            assert rebuilt[0] == pytest.approx(designed[0], abs=1.0), shape.name

    _move_shape(deck, "body", dy_px=-22.0)          # the export now draws the body into the title
    found = text_report(deck, irs)["exportOverlaps"]
    assert [(entry["a"], entry["b"]) for entry in found] == [("title", "body")]
    assert found[0]["sharedPx"] > 2.0 and found[0]["designSharedPx"] == 0.0

    _move_shape(deck, "body", rotation=15.0)         # rotated: its band is not on the slide — skipped
    rotated = text_report(deck, irs)
    assert rotated["exportOverlaps"] == [] and rotated["skipped"] >= 1

    presentation = Presentation(str(deck))              # a box with no IR element: counted, not paired
    extra = presentation.slides[0].shapes.add_textbox(PX * 60, PX * 60, PX * 300, PX * 40)
    extra.name, extra.text_frame.text = "stray", "A stray box over the title"
    presentation.save(str(deck))
    assert text_report(deck, irs)["unmatched"] == 1


def test_text_row_is_clean_on_the_text_family(tmp_path: Path) -> None:
    """`fixtures/torture/text.html` measured, classified and emitted: no joins glued, no overlap added."""
    from app.engine.classify.charts import recognise_charts
    from app.engine.classify.placeholders import map_placeholders
    from app.engine.emit import pptx as emitter
    from app.engine.reports import EmitOptions
    from app.engine.verify.fixtures import context_for
    from tests.engine.helpers import extract_html

    html = config.TORTURE_FIXTURE / "text.html"
    context = context_for(html)
    ir = extract_html(html, context.manifest, context.layout_id, context.assets_dir, slide_id="text")
    ir = map_placeholders(recognise_charts(ir), context.layout)
    deck = tmp_path / "text.pptx"
    emitter.emit([ir], context.manifest, context.master, deck, EmitOptions())
    report = text_report(deck, [ir])
    assert report["checked"] and report["unmatched"] == 0
    assert report["collisions"] == [] and report["exportOverlaps"] == [], report


def test_text_row_is_clean_on_the_sample_project(sample_export) -> None:
    """The engine's export of the sample project: no join glued, no overlap the export introduced.

    Every text shape pairs with an IR text element except a chart's own overlay text (a recognised
    chart's labels drawn as text boxes named "<chart> label i"), which belongs to the chart element.
    """
    from app.engine.verify.text_deck import _label_of

    result = sample_export
    report = text_report(result.pptx, result.irs)
    charts = {_label_of(e) for ir in result.irs for e in ir.of_kind("chart")}
    overlays = [shape.name for slide in Presentation(str(result.pptx)).slides for shape in slide.shapes
                if shape.has_text_frame and shape.text_frame.text.strip()
                and any(shape.name.startswith(f"{chart} label ") for chart in charts)]
    assert report["checked"] and report["unmatched"] == len(overlays)
    assert report["collisions"] == [] and report["exportOverlaps"] == [], report


def test_suite_has_a_text_row() -> None:
    """The row runs whenever coverage read the file — not only when the template was checked."""
    from app.engine.reports import CoverageReport, SuiteReport
    from app.engine.verify.suite import _acceptance, _Inputs

    text = {"checked": True, "collisions": [], "exportOverlaps": [], "unmatched": 0, "skipped": 0}
    report = SuiteReport(coverage=CoverageReport(template={"ok": True, "checked": False}, text=text))
    assert _acceptance(report, _Inputs(deck=None))["text"] is True
    report.coverage.text = {**text, "exportOverlaps": [{"slide": 1, "a": "t", "b": "s"}]}
    assert _acceptance(report, _Inputs(deck=None))["text"] is False
    report.coverage.text = {}
    assert "text" not in _acceptance(report, _Inputs(deck=None)), "a row that did not run is absent"


# ------------------------------------------------------------------------ WP-C: charts row and lint


def _wpc_chart_ir(spec: dict, box: Box) -> IR:
    return _ir_with([Element(kind="chart", box=box, origin="authored", confidence=1.0, spec=spec,
                             style={"font": "Calibri", "sizePx": 12.0, "color": "333333"})])


def test_a_failed_chart_is_named_in_the_coverage_row(tmp_path: Path, masters_dir: Path) -> None:
    """A chart the emitter could not build is a placeholder, and the charts row says why (WP-C §4.1)."""
    from app.engine.emit import pptx as emitter

    manifest = Manifest.load(masters_dir / "test-16x9.manifest.json")
    ir = _wpc_chart_ir({"type": "grouped_bar", "categories": ["a"], "series": [{"values": [1]}]},
                       Box(64, 100, 600, 300))
    ir.slide.layoutId = "layout-07"
    deck = tmp_path / "failed.pptx"
    report = emitter.emit([ir], manifest, masters_dir / "test-16x9.pptx", deck)
    [entry] = report.charts
    assert entry["failed"] and entry["error"] == "unknown chart type 'grouped_bar' (did you mean 'bar'?)"
    cover = coverage([ir], report, [], [], pptx=deck)
    [check] = cover.charts
    assert check["ok"] is False and check["failed"] is True
    assert check["problems"][0] == "chart not built: unknown chart type 'grouped_bar' (did you mean 'bar'?)"
    # the suite's charts row is "every entry ok" (suite._acceptance), so this one fails it
    assert not all(entry["ok"] for entry in cover.charts)


def test_coverage_flags_a_manual_layout_on_an_unpinned_chart(tmp_path: Path, masters_dir: Path) -> None:
    """No plot rect, no overlay, no plotArea: PowerPoint lays the chart out — a manual layout is a guess."""
    box = Box(64, 100, 600, 300)
    deck = _chart_deck(tmp_path / "guessed.pptx", masters_dir / "test-16x9.pptx", box, ["a", "b"], [1.0, 2.0],
                       plot=(0.0, 0.0, 1.0, 1.0))
    [entry] = chart_checks(deck, [_chart_ir(box, ["a", "b"], [1.0, 2.0], None)])
    assert "unpinned chart carries a manualLayout (PowerPoint should lay it out)" in entry["problems"]
    clean = _chart_deck(tmp_path / "auto.pptx", masters_dir / "test-16x9.pptx", box, ["a", "b"], [1.0, 2.0])
    [entry] = chart_checks(clean, [_chart_ir(box, ["a", "b"], [1.0, 2.0], None)])
    assert entry["ok"], entry["problems"]


def test_coverage_checks_a_pinned_default_rect(tmp_path: Path, masters_dir: Path) -> None:
    """An overlay pins `chart_model.layout`'s rect; the charts row compares the file's with it."""
    from app.engine import chart_model

    spec = {"type": "column", "categories": ["a", "b"], "series": [{"name": "Series 1", "values": [1.0, 2.0]}],
            "options": {"valueAxis": {"min": 0, "max": 4}, "referenceLines": [{"value": 2}]}}
    box = Box(64, 100, 600, 300)
    area = chart_model.layout(chart_model.normalise(spec), box.w, box.h, 12.0)["plotArea"]
    right = _chart_deck(tmp_path / "pinned.pptx", masters_dir / "test-16x9.pptx", box, ["a", "b"], [1.0, 2.0],
                        plot=(area["x"], area["y"], area["w"], area["h"]))
    [entry] = chart_checks(right, [_wpc_chart_ir(spec, box)])
    assert entry["ok"], entry["problems"]
    assert entry["plotRectDeltaPx"] < 0.5
    wrong = _chart_deck(tmp_path / "shifted.pptx", masters_dir / "test-16x9.pptx", box, ["a", "b"], [1.0, 2.0],
                        plot=(area["x"] + 0.05, area["y"], area["w"], area["h"]))
    [entry] = chart_checks(wrong, [_wpc_chart_ir(spec, box)])
    assert not entry["ok"] and entry["plotRectDeltaPx"] > 4.0
    missing = _chart_deck(tmp_path / "unpinned.pptx", masters_dir / "test-16x9.pptx", box, ["a", "b"], [1.0, 2.0])
    [entry] = chart_checks(missing, [_wpc_chart_ir(spec, box)])
    assert any("pinned" in problem and "no c:manualLayout" in problem for problem in entry["problems"])


def test_lint_says_how_a_lenient_chart_was_read() -> None:
    """A numeric string, an alias name, a `#RGB` colour: `chart-advisory` warnings, never errors."""
    document = ('<!doctype html><html><body><div data-chart=\'{"type":"column_clustered",'
                '"categories":["a","b"],"series":[{"values":["4.2",5]}],"colors":["#ABC"]}\'></div>'
                '</body></html>')
    report = lint(document)
    advisories = [f for f in report.findings if f.rule == "chart-advisory"]
    assert {f.level for f in advisories} == {"warn"}
    assert len(advisories) == 3, [f.message for f in advisories]
    assert not [f for f in report.findings if f.level == "error"]


# ------------------------------------------------------------ WP-B: side by side, positions, seams


def _para(lines: list[Box], *, align: str = "left", bullet: dict | None = None, text: str = "Text") -> dict:
    run = {"text": text, "font": "Arial", "sizePx": 14, "weight": 400, "italic": False,
           "color": "000000", "alpha": 1}
    return {"align": align, "lineHeightPx": lines[0].h, "spaceBeforePx": 0, "spaceAfterPx": 0,
            "bullet": bullet, "lines": [{"box": line.to_json(), "runs": [dict(run)]} for line in lines]}


def _text_ir(elements: list[Element]) -> IR:
    return IR(canvas=Canvas(1280, 720), slide=Slide(id="s1", layoutId="layout-07"),
              elements=assign_ids_and_z(elements))


def _element(name: str, box: Box, paragraphs: list[dict]) -> Element:
    boxes = [[[[line["box"]["x"], line["box"]["x"] + line["box"]["w"]]] for line in p["lines"]]
             for p in paragraphs]
    return Element(kind="text", box=box, name=name, source={"path": name}, paragraphs=paragraphs,
                   extras={"x-run-boxes": boxes})


def test_text_rows_flags_two_paragraphs_on_one_line_and_not_a_stack() -> None:
    from app.engine.verify.text_deck import text_rows

    row = _element("row", Box(40, 100, 400, 20), [
        _para([Box(40, 100, 90, 20)], text="Revenue"), _para([Box(360, 100, 80, 20)], text="$15.2B")])
    stack = _element("stack", Box(40, 200, 400, 42), [
        _para([Box(40, 200, 90, 20)], text="Revenue"), _para([Box(40, 222, 80, 20)], text="$15.2B")])
    found = text_rows([_text_ir([row, stack])])
    assert [(f["name"], f["left"], f["right"], f["pairs"]) for f in found] == [("row", "Revenue", "$15.2B", 1)]


def test_text_rows_needs_more_than_the_shared_band_and_real_disjointness() -> None:
    """R1 lets adjacent line boxes overlap by up to half the shorter one; V2 flags strictly more."""
    from app.engine.verify.text_deck import text_rows

    half = _element("half", Box(0, 0, 400, 30), [
        _para([Box(0, 0, 90, 20)]), _para([Box(200, 10, 90, 20)])])        # 10 of 20 px: exactly half
    more = _element("more", Box(0, 100, 400, 30), [
        _para([Box(0, 100, 90, 20)]), _para([Box(200, 108, 90, 20)])])     # 12 of 20 px
    touching = _element("touching", Box(0, 200, 400, 20), [
        _para([Box(0, 200, 90, 20)]), _para([Box(89.5, 200, 90, 20)])])    # x-overlap within 1 px
    overlapping = _element("overlapping", Box(0, 300, 400, 20), [
        _para([Box(0, 300, 90, 20)]), _para([Box(80, 300, 90, 20)])])      # not x-disjoint
    found = text_rows([_text_ir([half, more, touching, overlapping])])
    assert [f["name"] for f in found] == ["more", "touching"]


def test_text_seams_reads_the_glued_runs_warnings_on_text_elements() -> None:
    from app.engine.verify.text_deck import text_seams

    text = _element("t", Box(0, 0, 100, 20), [_para([Box(0, 0, 100, 20)])])
    shape = Element(kind="shape", box=Box(0, 50, 10, 10), geometry={"type": "rect"}, fill={"type": "none"})
    ir = _text_ir([text, shape])
    message = ("glued runs: 'and' + 'tight' are separated by layout, not by a space — the export "
               "cannot keep that gap")
    ir.diagnostics.extend([
        Diagnostic(level="warn", source="p", message=message, elementId=text.id),
        Diagnostic(level="warn", source="p", message=message, elementId=shape.id),   # not a text element
        Diagnostic(level="info", source="p", message=message, elementId=text.id),    # not a warning
        Diagnostic(level="warn", source="p", message="text is clipped", elementId=text.id),
    ])
    assert text_seams([ir]) == [{"slide": "s1", "element": text.id, "message": message}]


def _positions_deck(path: Path) -> Path:
    """Five text shapes with known pPr margins on the test master: left (marL + indent), right
    (marR), centre (marL only), a bullet (hanging indent) and a group child whose group's `off` is
    50 px right of its `chOff`. Insets are zero except on the left box, which keeps PowerPoint's
    default 0.1 in."""
    presentation = Presentation(str(config.MASTERS_FIXTURE / "test-16x9.pptx"))
    for slide_id in list(presentation.slides._sldIdLst):
        presentation.part.drop_rel(slide_id.rId)
        presentation.slides._sldIdLst.remove(slide_id)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])

    def box(container, name: str, x: float, y: float, w: float, *, algn: str, marL: float = 0.0,
            marR: float = 0.0, indent: float = 0.0, bullet: bool = False, insets: bool = False):
        shape = container.add_textbox(Emu(int(x * PX)), Emu(int(y * PX)), Emu(int(w * PX)), Emu(int(20 * PX)))
        shape.name = name
        frame = shape.text_frame
        if not insets:
            frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
        paragraph = frame.paragraphs[0]
        paragraph.add_run().text = "Text"
        pPr = paragraph._p.get_or_add_pPr()
        pPr.set("algn", algn)
        for key, value in (("marL", marL), ("marR", marR), ("indent", indent)):
            if value:
                pPr.set(key, str(int(round(value * PX))))
        if bullet:
            pPr.append(pPr.makeelement(qn("a:buChar"), {"char": "•"}))
        return shape

    box(slide.shapes, "left", 100, 100, 300, algn="l", marL=10, indent=5, insets=True)
    box(slide.shapes, "right", 500, 100, 200, algn="r", marR=20)
    box(slide.shapes, "centre", 800, 100, 200, algn="ctr", marL=20)
    box(slide.shapes, "bullet", 100, 300, 300, algn="l", marL=24, indent=-24, bullet=True)
    group = slide.shapes.add_group_shape()
    box(group.shapes, "child", 600, 300, 200, algn="l")
    off = group._element.grpSpPr.find(qn("a:xfrm")).find(qn("a:off"))
    off.set("x", str(int(off.get("x")) + 50 * PX))
    presentation.save(str(path))
    return path


def _positions_ir(shift: dict[str, float]) -> IR:
    """IR elements matching `_positions_deck`, each first line where the deck puts it plus `shift`."""
    inset = 0.1 * config.PX_PER_IN
    marker = {"type": "char", "char": "•", "level": 0, "indentPx": 24, "color": "000000", "start": 1}

    def element(name: str, box: Box, x: float, align: str, *, bullet: bool = False) -> Element:
        line = Box(x + shift.get(name, 0.0), box.y, 40, 20)
        return _element(name, box, [_para([line], align=align, bullet=marker if bullet else None)])

    return _text_ir([
        element("left", Box(100, 100, 300, 20), 100 + inset + 10 + 5, "left"),
        element("right", Box(500, 100, 200, 20), 680 - 40, "right"),
        element("centre", Box(800, 100, 200, 20), (820 + 1000) / 2 - 20, "center"),
        element("bullet", Box(100, 300, 300, 20), 124, "left", bullet=True),
        element("child", Box(650, 300, 200, 20), 650, "left"),
    ])


def test_text_positions_is_silent_when_every_first_line_starts_where_the_browser_put_it(tmp_path: Path) -> None:
    from app.engine.verify.text_deck import text_positions

    deck = _positions_deck(tmp_path / "positions.pptx")
    assert text_positions(deck, [_positions_ir({})]) == []
    assert text_positions(deck, [_positions_ir({"left": 1.4, "right": -1.4})]) == [], "within tolerance"


def test_text_positions_names_each_paragraph_that_moved(tmp_path: Path) -> None:
    from app.engine.verify.text_deck import text_positions

    deck = _positions_deck(tmp_path / "positions.pptx")
    moved = {"left": 3.0, "right": -6.0, "centre": 2.0, "bullet": 4.0, "child": -50.0}
    found = {f["shape"]: f for f in text_positions(deck, [_positions_ir(moved)])}
    assert set(found) == set(moved)
    assert (found["left"]["align"], found["left"]["expected"]) == ("left", pytest.approx(124.6, abs=0.01))
    assert (found["right"]["align"], found["right"]["expected"]) == ("right", pytest.approx(680.0, abs=0.01))
    assert (found["centre"]["align"], found["centre"]["expected"]) == ("center", pytest.approx(910.0, abs=0.01))
    assert (found["bullet"]["align"], found["bullet"]["expected"]) == ("bullet", pytest.approx(124.0, abs=0.01))
    assert found["child"]["expected"] == pytest.approx(650.0, abs=0.01), "child coordinates map through the group"
    for name, delta in moved.items():
        assert found[name]["measured"] - found[name]["expected"] == pytest.approx(delta, abs=0.02)


def test_text_positions_skips_an_ir_without_run_boxes(tmp_path: Path) -> None:
    """V3 is Path A's: an element the extractor did not measure (no `x-run-boxes`) is not judged."""
    from app.engine.verify.text_deck import text_positions

    deck = _positions_deck(tmp_path / "positions.pptx")
    ir = _positions_ir({"left": 30.0})
    for element in ir.elements:
        element.extras = {}
    assert text_positions(deck, [ir]) == []


def test_the_text_report_carries_wpb_lists_and_the_row_reads_them(tmp_path: Path) -> None:
    """Plan D6, §16 #8: `text_report` (the one dict `coverage()` and the torture runner read) holds
    WP-B's `rows`, `positions` and `seams`, and `rows["text"]` fails on any of them."""
    from app.engine.reports import CoverageReport, SuiteReport
    from app.engine.verify.suite import _acceptance, _Inputs
    from app.engine.verify.text_deck import text_report

    deck = _positions_deck(tmp_path / "positions.pptx")
    ir = _positions_ir({"left": 3.0})
    report = text_report(deck, [ir])
    assert {"rows", "positions", "seams"} <= set(report)
    assert [f["shape"] for f in report["positions"]] == ["left"]
    assert report["rows"] == [] and report["seams"] == []

    clean = {"checked": True, "collisions": [], "exportOverlaps": [], "unmatched": 0, "skipped": 0,
             "rows": [], "positions": [], "seams": []}
    suite = SuiteReport(coverage=CoverageReport(template={"ok": True, "checked": False}, text=clean))
    assert _acceptance(suite, _Inputs(deck=None))["text"] is True
    for key in ("rows", "positions", "seams"):
        suite.coverage.text = {**clean, key: [{"slide": 1}]}
        assert _acceptance(suite, _Inputs(deck=None))["text"] is False, key

