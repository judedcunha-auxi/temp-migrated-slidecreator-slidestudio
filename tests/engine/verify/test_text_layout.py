"""WP3 — the two measured text checks: is the text taller than its box, and is it drawn on other text?

The case these exist for came from a real client slide; here it is rebuilt synthetically, with the same
geometry, in `tests/engine/fixtures/svg_cases/title-v1-overlaps.html`: a headline (22pt bold, line-height
1.18, in a 62 px box at top 28) that wraps to three lines, runs 41 px below its box and paints through
the subtitle at top 104. `title-v2-fixed.html` is the repair (one `<br>` moved), and it *still* exceeds
its declared box by about 6.4 px — which is why every tolerance here is a
fraction of a line rather than zero, and why both files are in the suite. A rule that fires on the
defect and on the repair is worse than no rule: it teaches people to ignore readiness.

The synthetic half of this module is deliberately browser-free. `text_overlaps` is a function of the
classified IR alone, so its edge cases (ink versus line boxes, shared width, ancestor and
descendant, rotation, paint order, shapes and charts) are written as IRs and read in milliseconds.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.engine.classify.charts import recognise_charts
from app.engine.classify.placeholders import map_placeholders
from app.engine.ir import IR, Box, Canvas, Element, Slide, assign_ids_and_z
from app.engine.manifest import Manifest
from app.engine.pipeline import export_deck
from app.engine.reports import ExportOptions
from app.engine.verify.text_layout import annotate_text_layout, text_overlaps
from tests.engine.helpers import extract_html

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
OVERLAP_DIR = FIXTURES / "svg_cases"
WP1_FIXTURES = FIXTURES / "wp1"
#: The headline's opening words, which every assertion uses to find it.
HEADLINE = "Every channel grew"
IDS = ("text-overflow", "text-overlap")

#: The one defective title, measured: 3 lines of 34.61 px end at 130.99 px, 40.99 px below the box
#: (top 28 + 62); its third line's em box (98.99-128.35) shares 14.0 px with `.sub`'s first (106-120).
OVERFLOW_MESSAGE = (
    "“Every channel grew and the partner programme now carries a…” runs 41 px below its box: "
    "3 lines need 104 px, the box is 62 px tall"
)
OVERLAP_MESSAGE = (
    "“Every channel grew and the partner programme now carries a…” and "
    "“Partner revenue rose in every region, with the fastest…” overlap by 14 px"
)


# ---------------------------------------------------------------------------------------- helpers


def _classify(ir: IR, manifest: Manifest) -> IR:
    """The classified IR the app's export produces, which is what overlap is measured on."""
    ir = recognise_charts(ir)
    ir = map_placeholders(ir, manifest.layout(ir.slide.layoutId))
    return annotate_text_layout(ir)


def _sources(ir: IR) -> list[str]:
    return [d.source for d in ir.diagnostics if d.source in IDS]


def _only(ir: IR, source: str):
    found = [d for d in ir.diagnostics if d.source == source]
    assert len(found) == 1, f"expected one {source}, got {[(d.source, d.message) for d in found]}"
    return found[0]


def _by_path(ir: IR, path: str) -> Element:
    """The text element the extractor traced to `path` — `cssPath` stops at the first `id`."""
    found = [e for e in ir.of_kind("text") if (e.source or {}).get("path") == path]
    assert len(found) == 1, f"no single text at {path!r}; have {[e.source for e in ir.of_kind('text')]}"
    return found[0]


def _slide(tmp_path: Path, name: str, style: str, body: str) -> Path:
    """A one-off slide on a 1280x720 canvas — the shape `test_pipeline.py` uses for its own fixtures."""
    path = tmp_path / name
    path.write_text(
        "<!doctype html><html><head><meta charset=\"utf-8\"><style>\n"
        "html,body{margin:0;width:1280px;height:720px;overflow:hidden;background:transparent;"
        "font-family:Arial,Helvetica,sans-serif;color:#2E2E38}\n"
        f"{style}\n</style></head><body>\n{body}\n</body></html>\n",
        encoding="utf-8",
    )
    return path


def _text(
    path: str,
    lines: list[tuple[float, float, float, float]],
    *,
    size_px: float = 20.0,
    text: str = "Some words on a line",
    rotation: float = 0.0,
) -> Element:
    """A `text` element with explicit per-line boxes — only what `text_overlaps` reads."""
    top = min(y for _, y, _, _ in lines)
    span = Box(min(x for x, _, _, _ in lines), top,
               max(x + w for x, _, w, _ in lines) - min(x for x, _, _, _ in lines),
               max(y + h for _, y, _, h in lines) - top)
    return Element(
        kind="text",
        box=span,
        rotation=rotation,
        source={"path": path},
        paragraphs=[{
            "align": "left",
            "lineHeightPx": lines[0][3],
            "lines": [
                {"box": {"x": x, "y": y, "w": w, "h": h},
                 "runs": [{"text": text, "font": "Arial", "sizePx": size_px, "weight": 400,
                           "italic": False, "color": "2E2E38"}]}
                for x, y, w, h in lines
            ],
        }],
        anchor="top",
    )


def _ir_with(elements: list[Element]) -> IR:
    return IR(canvas=Canvas(1280, 720), slide=Slide(id="sld_test", layoutId="layout-01"),
              elements=assign_ids_and_z(elements))


# ------------------------------------------------------------------------- the slide that is wrong


@pytest.fixture(scope="module")
def defective(sample_manifest: Manifest, sample_dir: Path) -> IR:
    return extract_html(OVERLAP_DIR / "title-v1-overlaps.html", sample_manifest, "layout-05",
                        sample_dir / "assets", slide_id="sld_title", title="v1")


def test_the_defective_title_reports_text_overflow(defective: IR) -> None:
    """Extraction alone says the headline does not fit: only the browser knows the declared box."""
    found = _only(defective, "text-overflow")
    assert found.level == "warn"
    assert found.message == OVERFLOW_MESSAGE
    title = next(e for e in defective.of_kind("text")
                 if e.paragraphs[0]["lines"][0]["runs"][0]["text"].startswith(HEADLINE))
    assert found.elementId == title.id
    assert defective.validate() == []


def test_the_defective_title_reports_text_overlap(sample_manifest: Manifest, sample_dir: Path) -> None:
    """…and classification says it is drawn through the subtitle, once, on the one painted first."""
    ir = _classify(
        extract_html(OVERLAP_DIR / "title-v1-overlaps.html", sample_manifest, "layout-05",
                     sample_dir / "assets", slide_id="sld_title", title="v1"),
        sample_manifest,
    )
    found = _only(ir, "text-overlap")
    assert found.level == "warn"
    assert found.message == OVERLAP_MESSAGE
    title = next(e for e in ir.of_kind("text")
                 if e.paragraphs[0]["lines"][0]["runs"][0]["text"].startswith(HEADLINE))
    assert found.elementId == title.id

    before = list(ir.diagnostics)
    again = text_overlaps(ir)
    assert [(d.source, d.message, d.elementId) for d in again] == [
        (found.source, found.message, found.elementId)
    ]
    assert ir.diagnostics == before, "text_overlaps must not mutate the IR it is given"


def test_the_repaired_title_reports_neither(sample_manifest: Manifest, sample_dir: Path) -> None:
    """The repair moved one `<br>`; both rules must go quiet, or readiness cries wolf."""
    ir = _classify(
        extract_html(OVERLAP_DIR / "title-v2-fixed.html", sample_manifest, "layout-05",
                     sample_dir / "assets", slide_id="sld_title", title="v2"),
        sample_manifest,
    )
    assert _sources(ir) == []


@pytest.mark.parametrize(("name", "layout_id"), [("slide-01", "layout-06"), ("slide-02", "layout-01")])
def test_the_sample_slides_report_neither(
    name: str, layout_id: str, sample_manifest: Manifest, sample_dir: Path
) -> None:
    ir = _classify(
        extract_html(sample_dir / f"{name}.html", sample_manifest, layout_id, sample_dir / "assets",
                     slide_id=name, title=name),
        sample_manifest,
    )
    assert _sources(ir) == []


# The torture families' own check — no unlisted text-overflow/text-overlap diagnostic, and every one
# a family lists must appear — is `test_torture.py::test_family_meets_its_expectations` (WP-G).


@pytest.mark.parametrize("family", ("text", "boxes", "images", "tables", "charts"))
def test_no_wp1_family_reports_overflow_or_overlap(
    family: str, sample_manifest: Manifest, sample_dir: Path
) -> None:
    ir = _classify(
        extract_html(WP1_FIXTURES / f"{family}.html", sample_manifest, "layout-05",
                     sample_dir / "assets", slide_id=f"wp1-{family}", title=family),
        sample_manifest,
    )
    assert _sources(ir) == [], f"wp1/{family} reported {[(d.source, d.message) for d in ir.diagnostics]}"


# -------------------------------------------------------------------------- text-overflow, measured


def test_overflow_checks_the_paragraphs_own_box_then_the_absolute_container(
    tmp_path: Path, sample_manifest: Manifest, sample_dir: Path
) -> None:
    """A card is what the author sized; the lines inside it are `height: auto` and cannot overflow."""
    slide = _slide(
        tmp_path, "cards.html",
        ".card{position:absolute;left:64px;width:300px;font-size:14px;line-height:1.5}\n"
        ".fixed{top:100px;height:40px}\n"
        ".auto{top:300px;height:auto}",
        '<div class="card fixed" id="fixed"><div>First line</div><div>Second line</div>'
        "<div>Third line</div></div>\n"
        '<div class="card auto" id="auto"><div>First line</div><div>Second line</div>'
        "<div>Third line</div></div>",
    )
    ir = extract_html(slide, sample_manifest, "layout-05", sample_dir / "assets", slide_id="sld_cards")
    found = _only(ir, "text-overflow")
    assert "3 lines need 63 px, the box is 40 px tall" in found.message
    assert found.elementId == _by_path(ir, "div#fixed").id


def test_overflow_tolerates_less_than_half_a_line(
    tmp_path: Path, sample_manifest: Manifest, sample_dir: Path
) -> None:
    """Two lines of 34.6 px in a 62 px box is a healthy heading; three is the fixture's defect."""
    slide = _slide(
        tmp_path, "heads.html",
        ".head{position:absolute;left:64px;width:1152px;height:62px;"
        "font-size:22pt;font-weight:bold;line-height:1.18}\n"
        ".two{top:28px}\n.three{top:300px}",
        '<div class="head two" id="two">A<br>B</div>\n'
        '<div class="head three" id="three">A<br>B<br>C</div>',
    )
    ir = extract_html(slide, sample_manifest, "layout-05", sample_dir / "assets", slide_id="sld_heads")
    found = _only(ir, "text-overflow")
    assert "3 lines need 104 px, the box is 62 px tall" in found.message
    assert found.elementId == _by_path(ir, "div#three").id


def test_overflow_is_not_reported_on_clipped_text(
    tmp_path: Path, sample_manifest: Manifest, sample_dir: Path
) -> None:
    """Cut-off text is already an error; saying it also runs past its box is one defect twice.

    The `clipbox` shape is `tests/engine/fixtures/wp1/text.html`, with the inner `width:400px` dropped
    so the sentence wraps and the box really would overflow — otherwise the test would pass for the
    wrong reason.
    """
    slide = _slide(
        tmp_path, "clipped.html",
        ".clipbox{position:absolute;left:64px;top:600px;width:120px;height:20px;overflow:hidden}\n"
        ".clipbox div{font-size:14px;line-height:1.2}",
        '<div class="clipbox"><div>This sentence is much taller than its clipping parent box</div></div>',
    )
    ir = extract_html(slide, sample_manifest, "layout-05", sample_dir / "assets", slide_id="sld_clip")
    clipped = [e for e in ir.of_kind("text") if e.clip is not None]
    assert len(clipped) == 1
    errors = [d for d in ir.diagnostics if d.level == "error" and d.elementId == clipped[0].id]
    assert errors and "clipped" in errors[0].message
    assert [d for d in ir.diagnostics if d.source == "text-overflow"] == []


def test_overflow_ignores_a_nowrap_line_wider_than_its_box(
    tmp_path: Path, sample_manifest: Manifest, sample_dir: Path
) -> None:
    """`white-space: nowrap` wider than its box is an authoring choice the emitter keeps."""
    slide = _slide(
        tmp_path, "nowrap.html",
        ".nowrap{position:absolute;left:64px;top:520px;width:80px;font-size:12px;white-space:nowrap}",
        '<div class="nowrap">This never wraps and is far wider than eighty pixels</div>',
    )
    ir = extract_html(slide, sample_manifest, "layout-05", sample_dir / "assets", slide_id="sld_nowrap")
    assert [d for d in ir.diagnostics if d.source == "text-overflow"] == []


# -------------------------------------------------------------------------- text-overlap, synthetic


def test_overlap_measures_shared_ink_not_line_boxes() -> None:
    """Line boxes are `line-height` tall; the glyphs are the em box centred in them."""
    apart = _ir_with([
        _text("body > div.a:nth-child(1)", [(64.0, 100.0, 400.0, 40.0)]),
        _text("body > div.b:nth-child(2)", [(64.0, 128.0, 400.0, 40.0)]),
    ])
    assert text_overlaps(apart) == [], "line boxes sharing 12 px of leading is not a collision"

    together = _ir_with([
        _text("body > div.a:nth-child(1)", [(64.0, 100.0, 400.0, 40.0)]),
        _text("body > div.b:nth-child(2)", [(64.0, 112.0, 400.0, 40.0)]),
    ])
    found = text_overlaps(together)
    assert len(found) == 1 and found[0].message.endswith("overlap by 8 px")


def test_overlap_needs_shared_width() -> None:
    """Two columns at the same height are side by side, not on top of each other."""
    disjoint = _ir_with([
        _text("body > div.a:nth-child(1)", [(64.0, 100.0, 100.0, 20.0)]),
        _text("body > div.b:nth-child(2)", [(200.0, 105.0, 100.0, 20.0)]),
    ])
    assert text_overlaps(disjoint) == []

    touching = _ir_with([
        _text("body > div.a:nth-child(1)", [(64.0, 100.0, 100.0, 20.0)]),
        _text("body > div.b:nth-child(2)", [(163.0, 105.0, 100.0, 20.0)]),
    ])
    assert text_overlaps(touching) == [], "1 px of shared width is a rounded edge, not an overlap"


def test_overlap_skips_ancestor_and_descendant_paths() -> None:
    """A card's own paragraph is inside the card by construction — the same ink at two levels."""
    nested = _ir_with([
        _text("body > div.card:nth-child(1)", [(64.0, 100.0, 300.0, 20.0)]),
        _text("body > div.card:nth-child(1) > div.badge:nth-child(2)", [(64.0, 105.0, 100.0, 20.0)]),
    ])
    assert text_overlaps(nested) == []

    siblings = _ir_with([
        _text("body > div.card:nth-child(1)", [(64.0, 100.0, 300.0, 20.0)]),
        _text("body > div.other:nth-child(2) > div.badge:nth-child(2)", [(64.0, 105.0, 100.0, 20.0)]),
    ])
    assert len(text_overlaps(siblings)) == 1


def test_overlap_skips_rotated_text() -> None:
    """A rotated element's box is the box before rotation, so a level band tests the wrong shape."""
    ir = _ir_with([
        _text("body > div.a:nth-child(1)", [(64.0, 100.0, 300.0, 20.0)]),
        _text("body > div.b:nth-child(2)", [(64.0, 105.0, 300.0, 20.0)], rotation=-8.0),
    ])
    assert text_overlaps(ir) == []


def test_overlap_reports_each_pair_once_on_the_first_painted() -> None:
    """One pair, one finding, on the element underneath — and the worst of its line pairs."""
    ir = _ir_with([
        _text("body > div.a:nth-child(1)", [(64.0, 100.0, 400.0, 20.0), (64.0, 200.0, 400.0, 20.0)]),
        _text("body > div.b:nth-child(2)", [(64.0, 115.0, 400.0, 20.0), (64.0, 211.0, 400.0, 20.0)]),
    ])
    found = text_overlaps(ir)
    assert len(found) == 1
    assert found[0].elementId == ir.elements[0].id
    assert ir.elements[0].z == 0 and ir.elements[1].z == 1
    assert found[0].message.endswith("overlap by 9 px")


def test_overlap_leaves_shapes_and_charts_alone() -> None:
    """A badge over a card's fill, a label inside a box, a title over a band: that is design."""
    ir = _ir_with([
        Element(kind="shape", box=Box(64.0, 90.0, 400.0, 60.0), geometry={"type": "rect"},
                fill={"type": "solid", "color": "1A9AFA"}, source={"path": "body > div.band:nth-child(1)"}),
        Element(kind="chart", box=Box(64.0, 90.0, 400.0, 60.0), spec={"type": "bar", "series": []},
                origin="recognised", confidence=0.9, source={"path": "body > svg:nth-child(2)"}),
        _text("body > div.label:nth-child(3)", [(64.0, 100.0, 300.0, 20.0)]),
    ])
    assert text_overlaps(ir) == []


# ------------------------------------------------------------------------------- the whole pipeline


def test_export_deck_carries_both_text_diagnostics(tmp_path: Path, sample_dir: Path) -> None:
    """The app only ever reaches the engine through `export_deck`, so that is what must carry them."""
    copy = tmp_path / "sample"
    shutil.copytree(sample_dir, copy)
    shutil.copyfile(OVERLAP_DIR / "title-v1-overlaps.html", copy / "slide-01.html")

    result = export_deck(copy, ExportOptions(
        out_dir=tmp_path / "out", slide_ids=["sld_sample_01"],
    ))
    assert len(result.irs) == 1
    assert {d.source for d in result.irs[0].diagnostics} >= {"text-overflow", "text-overlap"}
    assert result.irs[0].validate() == []
