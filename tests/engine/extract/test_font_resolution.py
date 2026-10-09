"""The measuring browser must draw the theme fonts it was forced into — checked, not assumed.

The failure this guards against: a font file on disk (so the `config.FONT_DIRS` scan calls it
installed) that the running browser session never loaded. The browser falls back, every slide is
measured in the fallback while PowerPoint sets the real face, and lines re-wrap and overlap.

These tests reproduce that state without touching the machine's fonts: a copy of an installed
sans-serif face (Verdana, DejaVu Sans, Tahoma, Arial or Liberation Sans, whichever the machine has) renamed to a
family no browser has heard of, in a temporary `FONT_DIRS`. The scan finds it; the browser cannot.
Also here: the lint rule for characters the theme font has no glyph for, exercised with a subset of
that face that lacks arrows and check marks.
"""
from __future__ import annotations

import copy
import functools
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

from app.config import engine as config
from app.engine.emit import text as text_engine
from app.engine.extract import html as html_extract
from app.engine.extract.html import (
    FONT_PROBE_PX,
    FONT_PROBE_TEXT,
    FontCheck,
    _judge,
    ensure_theme_fonts,
    font_check_diagnostics,
    font_forcing_css,
    measuring_page,
)
from app.engine.ir import Canvas
from app.engine.manifest import Manifest
from app.engine.verify.lint import lint
from tests.engine.helpers import extract_html

UNSEEN = "Engine Probe Sans"
SUBSET = "Engine Probe Subset"

#: (regular, bold) source faces, first one found under the machine's font folders wins.
#: Wide faces first: the reference test needs a face whose widths differ from the browser's own
#: sans-serif fallback (Arial, or Liberation Sans on Linux), so the fallback is visible.
SOURCE_FACES = (
    ("verdana.ttf", "verdanab.ttf"),
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
    ("tahoma.ttf", "tahomabd.ttf"),
    ("arial.ttf", "arialbd.ttf"),
    ("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf"),
)

#: Code points the glyph-subset keeps (when the source has them): Latin-1 plus the punctuation
#: the lint must stay silent on. Arrows, check marks, bullets-as-shapes and comparisons are left out.
SUBSET_KEEP = [*range(0x20, 0x7F), *range(0xA0, 0x100), 0x2022, 0x2013, 0x2014, 0x2026, 0x20AC]


@functools.cache
def _source_faces() -> tuple[Path, Path]:
    """Looked up once, before any test narrows `FONT_DIRS` to its temporary folder."""
    found = {path.name.lower(): path for path in config.font_files()}
    for regular, bold in SOURCE_FACES:
        if regular.lower() in found and bold.lower() in found:
            return found[regular.lower()], found[bold.lower()]
    pytest.skip("none of the probe source faces (regular + bold TTF) is installed")


def _probe_source_is_arial_like() -> bool:
    """Arial and Liberation Sans share Arial's metrics, which is also what the fallback draws."""
    regular, _ = _source_faces()
    return regular.name.lower() in ("arial.ttf", "liberationsans-regular.ttf")


def _style(source: Path) -> str:
    name = source.name.lower()
    return "Bold" if "bold" in name or name.endswith(("bd.ttf", "b.ttf")) else "Regular"


def _renamed(source: Path, target: Path, family: str) -> None:
    font = TTFont(str(source))
    postscript = family.replace(" ", "")
    for record in font["name"].names:
        if record.nameID in (1, 16):
            record.string = family
        elif record.nameID in (3, 4, 18):
            record.string = f"{family} {_style(source)}"
        elif record.nameID == 6:
            record.string = f"{postscript}-{_style(source)}"
    font.save(str(target))
    font.close()


def _forget_fonts() -> None:
    text_engine.forget_fonts()
    html_extract.forget_installed_families()


@pytest.fixture()
def unseen_font_dir(tmp_path: Path, monkeypatch) -> Path:
    """`FONT_DIRS` holding an installed regular/bold face renamed to `UNSEEN`: on disk, in no session."""
    regular, bold = _source_faces()
    folder = tmp_path / "fonts"
    folder.mkdir()
    _renamed(regular, folder / "EngineProbeSans-Regular.ttf", UNSEEN)
    _renamed(bold, folder / "EngineProbeSans-Bold.ttf", UNSEEN)
    monkeypatch.setattr(config, "FONT_DIRS", (folder,))
    _forget_fonts()
    assert text_engine.face_for_exact(UNSEEN) is not None, "the scan must find the renamed face"
    yield folder
    _forget_fonts()


def _manifest_with_fonts(base: Manifest, major: str, minor: str) -> Manifest:
    manifest = copy.deepcopy(base)
    manifest.theme = {**(manifest.theme or {}), "fonts": {"major": major, "minor": minor}}
    return manifest


def _forced_page(page, fonts: dict[str, str]) -> None:
    page.set_content(f"<html><body><span>{FONT_PROBE_TEXT}</span></body></html>")
    page.add_style_tag(content=font_forcing_css(fonts))
    page.evaluate("document.fonts.ready")


# ------------------------------------------------------------------------------ the rule, no browser


def test_a_family_that_measures_like_both_fallbacks_is_unresolved():
    raw = {"family": "X", "weight": 400, "mono": 1649.4, "serif": 1503.6, "asMono": 1649.4, "asSerif": 1503.6}
    check = _judge(raw, 1728.9, "x.ttf")
    assert not check.resolved and not check.ok


def test_a_resolved_family_is_judged_against_the_scanned_face():
    same = {"family": "X", "weight": 400, "mono": 1649.4, "serif": 1503.6, "asMono": 1725.3, "asSerif": 1725.3}
    assert _judge(same, 1728.9, "x.ttf").ok                     # 0.2 %: kerning, not a different face
    other = {**same, "asMono": 1623.1, "asSerif": 1623.1}       # Arial's width, under another face's name
    check = _judge(other, 1728.9, "x.ttf")
    assert check.resolved and not check.matches
    assert _judge(other, None, None).ok, "with no scanned face there is nothing to disagree with"


def test_a_theme_font_equal_to_one_fallback_is_still_resolved():
    """Times New Roman *is* the serif fallback; it still differs from monospace, so it resolved."""
    raw = {"family": "Times New Roman", "weight": 400, "mono": 1649.4, "serif": 1503.6,
           "asMono": 1503.6, "asSerif": 1503.6}
    assert _judge(raw, 1507.0, "times.ttf").ok


def test_diagnostics_are_silent_when_every_check_passed():
    ok = FontCheck("Arial", 400, True, 1623.1, 1622.7, "arial.ttf")
    assert font_check_diagnostics({"checks": [ok], "loaded": {}, "unresolved": []}) == []


# -------------------------------------------------------------------------------- in the browser


def test_installed_fonts_the_browser_sees_pass_untouched():
    with measuring_page(Canvas(1280, 720)) as page:
        _forced_page(page, {"major": "Arial", "minor": "Calibri"})
        result = ensure_theme_fonts(page, {"major": "Arial", "minor": "Calibri"})
    assert result["loaded"] == {} and result["unresolved"] == []
    assert {(c.family, c.weight) for c in result["checks"]} >= {("Arial", 400), ("Arial", 700), ("Calibri", 400)}
    assert all(c.ok for c in result["checks"]), [c.describe() for c in result["checks"]]
    assert font_check_diagnostics(result) == []


def test_a_font_on_disk_the_browser_cannot_see_is_loaded_from_its_file(unseen_font_dir: Path):
    fonts = {"major": UNSEEN, "minor": UNSEEN}
    with measuring_page(Canvas(1280, 720)) as page:
        _forced_page(page, fonts)
        before = html_extract._probe(page, [UNSEEN])
        assert before and not any(c.resolved for c in before), "the renamed face must be invisible at first"
        result = ensure_theme_fonts(page, fonts)
        # The page now lays text out in the scanned face, not in the fallback.
        drawn = page.evaluate("() => document.querySelector('span').getBoundingClientRect().width")
    assert set(result["loaded"]) == {UNSEEN}
    assert {Path(p).name for p in result["loaded"][UNSEEN]} == {
        "EngineProbeSans-Regular.ttf", "EngineProbeSans-Bold.ttf"}
    assert result["unresolved"] == []
    assert [(c.weight, c.ok) for c in result["checks"]] == [(400, True), (700, True)]
    expected = text_engine.face_width_px(text_engine.face_for_exact(UNSEEN), FONT_PROBE_TEXT, 16)
    assert drawn == pytest.approx(expected, rel=0.02)
    (level, source, message), = font_check_diagnostics(result)
    assert (level, source) == ("warn", "fonts") and "could not draw it" in message


def test_a_font_that_cannot_be_loaded_is_a_loud_error(unseen_font_dir: Path, monkeypatch):
    monkeypatch.setattr(html_extract, "LOAD_UNSEEN_FONTS", False)
    fonts = {"major": UNSEEN, "minor": "Arial"}
    with measuring_page(Canvas(1280, 720)) as page:
        _forced_page(page, fonts)
        result = ensure_theme_fonts(page, fonts)
    assert result["unresolved"] == [UNSEEN] and result["loaded"] == {}
    errors = [d for d in font_check_diagnostics(result) if d[0] == "error"]
    assert len(errors) == 1 and UNSEEN in errors[0][2] and "fallback" in errors[0][2]


def _slide(tmp_path: Path, size: float = FONT_PROBE_PX) -> Path:
    slide = tmp_path / "slide.html"
    slide.write_text(
        "<!doctype html><html><head><meta charset='utf-8'></head><body style='margin:0'>"
        f"<p style='position:absolute;left:0;top:0;margin:0;font-size:{size}px;white-space:nowrap'>"
        f"{FONT_PROBE_TEXT}</p></body></html>",
        encoding="utf-8",
    )
    return slide


def test_extraction_measures_in_the_theme_font_the_session_could_not_see(
    unseen_font_dir: Path, tmp_path: Path, sample_manifest: Manifest, sample_dir: Path,
):
    manifest = _manifest_with_fonts(sample_manifest, UNSEEN, UNSEEN)
    ir = extract_html(_slide(tmp_path), manifest, "layout-05", sample_dir / "assets", slide_id="sld_unseen")
    texts = [e for e in ir.elements if e.kind == "text"]
    assert texts, "the probe paragraph must come back as text"
    expected = text_engine.face_width_px(text_engine.face_for_exact(UNSEEN), FONT_PROBE_TEXT, FONT_PROBE_PX)
    assert max(e.box.w for e in texts) == pytest.approx(expected, rel=0.02), "measured in the fallback"
    assert ir.fonts.substituted == []
    messages = [(d.level, d.message) for d in ir.diagnostics if d.source == "fonts"]
    assert any(level == "warn" and "could not draw it" in message for level, message in messages), messages


def test_extraction_reports_an_unresolved_theme_font_as_an_error_and_a_substitution(
    unseen_font_dir: Path, tmp_path: Path, sample_manifest: Manifest, sample_dir: Path, monkeypatch,
):
    monkeypatch.setattr(html_extract, "LOAD_UNSEEN_FONTS", False)
    manifest = _manifest_with_fonts(sample_manifest, UNSEEN, UNSEEN)
    ir = extract_html(_slide(tmp_path), manifest, "layout-05", sample_dir / "assets", slide_id="sld_unseen2")
    assert [d.level for d in ir.diagnostics if d.source == "fonts"] == ["error"]
    assert ir.fonts.substituted == [{"wanted": UNSEEN, "got": "(browser fallback)"}]


def test_the_reference_is_drawn_in_the_font_extraction_measured_with(
    unseen_font_dir: Path, tmp_path: Path, sample_manifest: Manifest, sample_dir: Path, monkeypatch,
):
    """`render_reference` runs the same check and rescue, or the gate compares against a fallback."""
    from PIL import Image, ImageChops

    from app.engine.extract.html import render_reference

    slide = _slide(tmp_path, 60)                 # 100 px would run off the 1280 px canvas
    layout = sample_dir / "layouts" / sample_manifest.layout("layout-05").background
    fonts = {"major": UNSEEN, "minor": UNSEEN}

    def ink_right_edge(png: Path) -> int:
        with Image.open(png) as image, Image.open(layout) as under:
            bbox = ImageChops.difference(image.convert("RGB"), under.convert("RGB")).getbbox()
        assert bbox is not None, "the probe text must be painted"
        return bbox[2]

    loaded = render_reference(slide, layout, tmp_path / "loaded.png", assets_dir=sample_dir / "assets", fonts=fonts)
    monkeypatch.setattr(html_extract, "LOAD_UNSEEN_FONTS", False)
    fallback = render_reference(slide, layout, tmp_path / "fallback.png", assets_dir=sample_dir / "assets",
                                fonts=fonts)
    expected = text_engine.face_width_px(text_engine.face_for_exact(UNSEEN), FONT_PROBE_TEXT, 60)
    if _probe_source_is_arial_like():
        pytest.skip("only an Arial-metric face was available; it measures like the browser's fallback")
    assert ink_right_edge(loaded) == pytest.approx(expected, abs=12)
    assert abs(ink_right_edge(fallback) - expected) > 40, "without the rescue the fallback is drawn"


# ------------------------------------------------------------------------ glyphs the font lacks


@pytest.fixture()
def subset_manifest(sample_manifest: Manifest, tmp_path: Path, monkeypatch) -> Manifest:
    """A glyph subset of an installed face (no arrows, no check marks) as the only installed font,
    and as both theme fonts."""
    from fontTools import subset

    regular, _ = _source_faces()
    folder = tmp_path / "subset-fonts"
    folder.mkdir()
    renamed = folder / "renamed.ttf"
    _renamed(regular, renamed, SUBSET)
    font = TTFont(str(renamed))
    keep = [code for code in SUBSET_KEEP if code in font.getBestCmap()]
    options = subset.Options()
    options.name_IDs = ["*"]
    options.notdef_outline = True
    subsetter = subset.Subsetter(options)
    subsetter.populate(unicodes=keep)
    subsetter.subset(font)
    font.save(str(folder / "EngineProbeSubset-Regular.ttf"))
    font.close()
    renamed.unlink()
    monkeypatch.setattr(config, "FONT_DIRS", (folder,))
    _forget_fonts()
    yield _manifest_with_fonts(sample_manifest, SUBSET, SUBSET)
    _forget_fonts()


def _glyph_findings(html: str, manifest: Manifest):
    return [f for f in lint(html, manifest).findings if f.rule == "glyph-missing"]


def test_the_subset_face_has_no_arrows_or_check_marks(subset_manifest: Manifest):
    """The gap the lint rule exists for: the fixture face lacks these glyphs and keeps the rest."""
    face = text_engine.face_for_exact(SUBSET)
    assert face is not None
    cmap = TTFont(str(face.path)).getBestCmap()
    for code in (0x2190, 0x2191, 0x2192, 0x2193, 0x2713, 0x2714, 0x25CF, 0x2264, 0x2265, 0x25B6, 0x25BA):
        assert code not in cmap, f"U+{code:04X} survived the subset"
    for code in (0x2022, 0x2013, 0x2014, 0x2026, 0x20AC):
        assert code in cmap


def test_lint_flags_characters_the_theme_font_cannot_draw(subset_manifest: Manifest):
    html = ("<html><body><h1>Growth \u2191 12%</h1><p>Done \u2713 \u2192 next \u25CF item \u2264 5</p>"
            "<p>Plain text \u2022 \u2014 fine</p></body></html>")
    findings = _glyph_findings(html, subset_manifest)
    assert len(findings) == 1 and findings[0].level == "warn"
    message = findings[0].message
    for code in ("U+2191", "U+2713", "U+2192", "U+25CF", "U+2264"):
        assert code in message
    assert "U+2022" not in message and "U+2014" not in message
    assert findings[0].line == 1


def test_lint_reads_pseudo_content_and_skips_the_notes(subset_manifest: Manifest):
    html = ("<html><head><style>li::before { content: \"\\2714\"; }</style></head><body>"
            "<ul><li>Item</li></ul><aside class=\"notes\">Notes may say \u2192</aside></body></html>")
    (finding,) = _glyph_findings(html, subset_manifest)
    assert "U+2714" in finding.message and "U+2192" not in finding.message


def test_lint_is_silent_on_plain_text_and_on_fonts_it_cannot_see(subset_manifest: Manifest, sample_manifest: Manifest):
    assert _glyph_findings("<html><body><p>Café — 12 € …</p></body></html>", subset_manifest) == []
    ghost = _manifest_with_fonts(sample_manifest, "No Such Font 7781", "No Such Font 7781")
    assert _glyph_findings("<html><body><p>\u2713</p></body></html>", ghost) == []
    assert _glyph_findings("<html><body><p>\u2713</p></body></html>", None) == []
