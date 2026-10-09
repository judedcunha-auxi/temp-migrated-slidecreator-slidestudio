"""WP-A — a `<table>` end to end: browser → IR → one native table (+ what is drawn over it) → render.

`docs/archive/engine/fidelity/10-WPA-tables.md` §8.5. Two slides: the probe that showed the defect, since
G-2 the torture family `fixtures/torture/tables-cells.html` (probe p02, on `test-16x9`: row fills,
pills, badges, bars, an svg icon, emoji flags) and the WP1 family `wp1/tables-rich.html` (on the synthetic sample master). Each is exported
with the engine, rendered with PptxRender (`tests.engine.helpers.render`), and compared with the browser's
own composite. The acceptance signal is that **no row is taller in the export than the browser drew
it**: `fit()` predicts no table growth, and the table's ink ends where the reference's does.

Inputs that are missing fail these tests; nothing here skips (master brief §10.7).
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import pytest
from PIL import Image

from app.config import engine as config
from app.engine.classify.placeholders import map_placeholders
from app.engine.emit import pptx as emitter
from app.engine.extract.html import render_reference
from app.engine.ir import IR, Box, Element
from app.engine.manifest import Manifest
from app.engine.reports import EmitOptions, EmitReport
from app.engine.verify.fit import fit
from app.engine.verify.fixtures import context_for
from app.engine.verify.text_deck import text_report
from tests.engine.fixtures.verify_cases import measure_cells
from tests.engine.helpers import extract_html, render

#: Probe p02, promoted to the `tables-cells` torture family by WP-G (G-2, plan 16 #16/#17).
PROBE = config.TORTURE_FIXTURE / "tables-cells.html"
RICH = Path(__file__).resolve().parents[1] / "fixtures" / "wp1" / "tables-rich.html"

#: Brief §8.5: the table's ink bottom in the render is within this of the reference's.
INK_TOLERANCE_PX = 2

#: Brief §10 item 5: every cell line's ink centroid in PptxRender's render within this of the browser's.
LINE_TOLERANCE_PX = 1.5


@dataclass
class Exported:
    ir: IR
    deck: Path
    report: EmitReport
    reference: Path
    out: Path
    canvas_w: int

    @cached_property
    def render(self) -> Path:
        """PptxRender's picture of the deck, drawn on first use: only the `renderer` tests need it."""
        pngs = render(self.deck, self.canvas_w, self.out / "render")
        rendered = self.out / "render.png"
        shutil.copy(pngs[0], rendered)
        return rendered


def _export(html: Path, manifest: Manifest, layout_id: str, assets_dir: Path, master: Path,
            layout_png: Path, out: Path) -> Exported:
    assert html.exists() and master.exists() and layout_png.exists(), (html, master, layout_png)
    out.mkdir(parents=True, exist_ok=True)
    ir = extract_html(html, manifest, layout_id, assets_dir, slide_id=html.stem, title=html.stem)
    ir = map_placeholders(ir, manifest.layout(layout_id))
    deck = out / f"{html.stem}.pptx"
    report = emitter.emit([ir], manifest, master, deck, EmitOptions())
    # The browser composite in the master's fonts (G-1's required `fonts=`, fidelity finding F6).
    reference = render_reference(html, layout_png, out / "reference.png", assets_dir=assets_dir,
                                 fonts=dict(manifest.fonts), scripts=(config.CHART_PREVIEW_JS,))
    return Exported(ir=ir, deck=deck, report=report, reference=reference, out=out,
                    canvas_w=manifest.canvas_w)


@pytest.fixture(scope="module")
def p02(tmp_path_factory) -> Exported:
    context = context_for(PROBE)
    return _export(PROBE, context.manifest, context.layout_id, context.assets_dir, context.master,
                   context.layout_png, tmp_path_factory.mktemp("p02"))


@pytest.fixture(scope="module")
def rich(tmp_path_factory, sample_manifest: Manifest, sample_dir: Path) -> Exported:
    # `context_for` does not resolve `"master": "sample"`; the WP1 harness's own context, by hand.
    layout = sample_manifest.layout("layout-05")
    return _export(RICH, sample_manifest, "layout-05", sample_dir / "assets", sample_dir / "master.pptx",
                   sample_dir / "layouts" / layout.background, tmp_path_factory.mktemp("rich"))


def _tables(ir: IR) -> list[Element]:
    return [e for e in ir.elements if e.kind == "table"]


def _roles(ir: IR) -> dict[str, int]:
    counts: dict[str, int] = {}
    for element in ir.elements:
        role = ((element.extras or {}).get("x-wpa") or {}).get("role")
        if role:
            counts[role] = counts.get(role, 0) + 1
    return counts


def _lum(hex6: str) -> float:
    """Relative luminance, as the fidelity survey counted near-white text."""
    r, g, b = (int(hex6[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ink_bottom(png: Path, box: Box) -> int | None:
    """Brief §8.5: the first scan row from `box.bottom + 6` upward where ≥ 30 % of the pixels across
    the table's width differ from the slide background by more than 10/255 in any channel. The
    background is the commonest colour of the starting row, which lies below the table."""
    np = pytest.importorskip("numpy")
    image = np.asarray(Image.open(png).convert("RGB"), dtype=np.int16)
    left, right = int(round(box.x)), int(round(box.x2))
    y = min(image.shape[0] - 1, int(round(box.y2)) + 6)
    colours, counts = np.unique(image[y, left:right].reshape(-1, 3), axis=0, return_counts=True)
    background = colours[int(np.argmax(counts))]
    while y > box.y:
        if (np.abs(image[y, left:right] - background) > 10).any(axis=1).mean() >= 0.30:
            return y
        y -= 1
    return None


def _assert_rows_keep_their_height(exported: Exported) -> None:
    report = fit(exported.deck, [exported.ir])
    grown = [i for i in report.issues if i.kind == "overflow" and " row " in i.shape]
    assert grown == [], [i.detail for i in grown]
    assert not [d for d in report.details if "tableGrowthPx" in d]
    for table in _tables(exported.ir):
        reference, rendered = ink_bottom(exported.reference, table.box), ink_bottom(exported.render, table.box)
        assert reference is not None and rendered is not None, table.name
        assert abs(rendered - reference) <= INK_TOLERANCE_PX, (table.name, reference, rendered)
    tables = {table.id for table in _tables(exported.ir)}
    ours = [w for w in exported.report.warnings
            if w.split(": ", 1)[0].rsplit("/", 1)[-1] in tables and "no glyph" not in w]
    assert ours == [], ours


# ------------------------------------------------------------------------------------------- p02


def test_p02_no_silent_drops(p02: Exported):
    """The defect the probe showed: white text on a row fill the export dropped, an icon never placed."""
    assert not [d for d in p02.ir.diagnostics if "was not painted" in d.message]
    for table in _tables(p02.ir):
        for cell in table.cells:
            runs = [run for p in cell["paragraphs"] for line in p["lines"] for run in line["runs"]
                    if run["text"].strip()]
            if runs and all(_lum(run["color"]) > 0.85 for run in runs):
                assert cell["fill"]["type"] == "solid", (table.name, cell["r"], cell["c"])
    first = _tables(p02.ir)[0]
    extras = first.extras["x-wpa"]["cells"]
    zebra = {2, 4, 6}
    for cell in first.cells:
        # The header row and the zebra / highlighted body rows (tr backgrounds), cell by cell. The
        # `.shade` cell of the second table (`color-mix()`) is package E's normaliser: not asserted here.
        if cell["r"] == 0 or cell["r"] in zebra or cell["r"] == 3:
            assert cell["fill"]["type"] == "solid", (cell["r"], cell["c"])
            assert extras[f"{cell['r']}:{cell['c']}"]["fillFrom"] in ("row", "rowgroup")


def test_p02_overlay_inventory(p02: Exported):
    """Six number badges and six pills (a shape and a text each), six bars with their fills, the icon."""
    roles = _roles(p02.ir)
    assert (roles.get("chip"), roles.get("chipText"), roles.get("decoration")) == (12, 12, 12), roles
    side = _tables(p02.ir)[1]
    top = side.box.y + sum(side.rowHeightsPx[:3])
    compute = Box(side.box.x + side.colWidthsPx[0], top, side.colWidthsPx[1], side.rowHeightsPx[3])
    ellipses = [e for e in p02.ir.elements if (e.source or {}).get("svg") and e.kind == "shape"
                and compute.x <= e.box.x and e.box.x2 <= compute.x2 and compute.y <= e.box.y and e.box.y2 <= compute.y2]
    assert len(ellipses) == 1 and ellipses[0].geometry["type"] == "ellipse" and ellipses[0].z > side.z
    assert side.extras["x-wpa"]["cells"]["3:1"]["indentPx"][0] > 14


@pytest.mark.renderer_full
def test_p02_rows_keep_their_height(p02: Exported):
    _assert_rows_keep_their_height(p02)


@pytest.mark.renderer_full
def test_tables_rich_rows_keep_their_height(rich: Exported):
    _assert_rows_keep_their_height(rich)
    assert rich.report.warnings == [], rich.report.warnings


@pytest.mark.renderer_full
def test_tables_rich_every_line_lands_where_the_browser_drew_it(rich: Exported):
    """Every line of every cell, and every line WP-A lays over a table (chip words, band texts, block
    chips, the caption): the render's ink centroid within 1.5 px of the browser's, both ways. This is
    what the kinds review's finding 1 and 2 constructs (a chip or an icon mid-line, a chip starting a
    wrapped line, a chip-first list item, a grid column, a margin, a trailing icon) are held to."""
    pytest.importorskip("numpy")
    samples = measure_cells.measure(rich.ir, rich.reference, rich.render)
    assert len(samples) > 90
    off = [(s["table"], s["cell"], s["role"], s["text"], s["dx"], s["dy"]) for s in samples
           if s["dx"] is None or abs(s["dx"]) > LINE_TOLERANCE_PX or abs(s["dy"]) > LINE_TOLERANCE_PX]
    assert off == [], off


@pytest.mark.renderer_full
def test_tables_rich_the_words_after_a_chip_are_not_drawn_on_it(rich: Exported):
    """The kinds review's check on `Revenue <chip>+12%</chip> YoY`, in pixels: the chip's red words
    start inside its padding, and the first dark neutral ink after them starts after the chip."""
    np = pytest.importorskip("numpy")
    chip = next(e for e in rich.ir.elements if e.name == "rich r3c2 chip")
    for png in (rich.reference, rich.render):
        image = np.asarray(Image.open(png).convert("RGB"), dtype=np.int16)
        band = image[int(chip.box.y) + 2:int(chip.box.y2) - 2]
        red = (band[..., 0] > 120) & (band[..., 1] < 90) & (band[..., 2] < 90)
        columns = np.flatnonzero(red.any(axis=0))
        first_red = columns[columns >= int(chip.box.x) - 4][0]
        assert first_red >= chip.box.x + 3, (png.name, first_red, chip.box)
        luminance = band @ np.array([0.2126, 0.7152, 0.0722])
        neutral = (luminance < 110) & (np.abs(band[..., 0] - band[..., 1]) < 40)
        start = int(chip.box.x2) - 8
        after = np.flatnonzero(neutral[:, start:start + 30].any(axis=0))
        assert after.size and start + after[0] >= chip.box.x2, (png.name, start + after[0], chip.box)


@pytest.mark.parametrize("name", ["p02", "rich"])
def test_the_text_row_is_clean_on_both_decks(name: str, request):
    """G-1's text row (plan D6; its joins include table cells, plan §16 #11): no run the browser kept
    apart is glued in a cell or a text box over the table, and no text box's ink lands on another's."""
    exported: Exported = request.getfixturevalue(name)
    report = text_report(exported.deck, [exported.ir])
    assert report["checked"] and report["collisions"] == [] and report["exportOverlaps"] == [], report
