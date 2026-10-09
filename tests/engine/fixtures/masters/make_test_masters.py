"""Build the generated test masters the torture suite runs on.

    python -m tests.engine.fixtures.masters.make_test_masters               # test masters (renderer) + extra/
    python -m tests.engine.fixtures.masters.make_test_masters --extra-only  # extra/ only, offline

Two decks from python-pptx's default template, one 16:9 and one 4:3, each with a v2 `manifest.json`
and PptxRender-rendered layout PNGs. They exist to prove **size generality**: the same IR, emitted
onto a 1280×720 canvas and a 960×720 one, must both land correctly (master brief §9, last row).

Seeded by WP0a so that WP3a, WP4 and WP5 — which run in the same wave as WP0b — are not blocked on a
sibling agent's output. **WP0b owns these files from here on** and may regenerate or extend them;
re-running this script is deterministic apart from the PNGs, which come from the renderer.

It also writes the four synthetic brand masters in `extra/` (see the section at the end): invented
stand-ins for real templates, byte-identical on every run.

The manifest is built here rather than hand-written because a hand-written one would be wrong in the
interesting cases: most default-template layouts inherit their placeholder geometry from the master
and declare no `xfrm` of their own. WP7's real importer replaces these manifests at WP9 step 2.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from pptx import Presentation
from pptx.util import Emu

from app.config import engine as config
from app.engine.manifest import Layout, Manifest, Master, Placeholder, sha256_of
from app.engine.renderer import BlankLayoutsRenderer, PptxRenderClient

HERE = Path(__file__).resolve().parent

#: (stem, width in inches, height in inches). 13⅓×7.5 is PowerPoint's own widescreen; 10×7.5 its 4:3.
SIZES: tuple[tuple[str, float, float], ...] = (
    ("test-16x9", 13 + 1 / 3, 7.5),
    ("test-4x3", 10.0, 7.5),
)


def _placeholder_box(shape, master, slide_w: int, slide_h: int) -> tuple[int, int, int, int]:
    """A placeholder's EMU box, inherited from the master placeholder when the layout omits `xfrm`.

    This inheritance is the whole reason the manifest is generated: on the default template most
    layout placeholders carry no geometry at all, and a manifest full of zeros would make every
    placeholder test meaningless.
    """
    if shape.element.spPr.xfrm is not None:
        return shape.left, shape.top, shape.width, shape.height

    wanted_idx = shape.placeholder_format.idx
    wanted_type = shape.placeholder_format.type
    for candidate in master.placeholders:
        if candidate.placeholder_format.idx == wanted_idx:
            return candidate.left, candidate.top, candidate.width, candidate.height
    for candidate in master.placeholders:
        if candidate.placeholder_format.type == wanted_type:
            return candidate.left, candidate.top, candidate.width, candidate.height
    # Nothing to inherit from: the full slide, which is what PowerPoint falls back to.
    return 0, 0, slide_w, slide_h


def build_manifest(pptx: Path) -> Manifest:
    """A v2 manifest for a generated master: real `idx`, `partName`, px and EMU boxes, theme fonts."""
    presentation = Presentation(str(pptx))
    slide_w, slide_h = presentation.slide_width, presentation.slide_height
    canvas = {
        "w": round(slide_w / config.EMU_PER_PX),
        "h": round(slide_h / config.EMU_PER_PX),
        "pxPerIn": config.PX_PER_IN,
    }

    theme = {
        "colors": {},  # the default template's scheme; WP7's importer resolves these properly
        "fonts": {"major": "Calibri Light", "minor": "Calibri"},
    }

    masters: list[Master] = []
    layouts: list[Layout] = []
    number = 0
    for master_index, master in enumerate(presentation.slide_masters):
        masters.append(
            Master(index=master_index, name=f"master-{master_index + 1}",
                   partName=str(master.part.partname), theme=theme)
        )
        for layout_index, layout in enumerate(master.slide_layouts):
            number += 1
            placeholders: list[Placeholder] = []
            for shape in layout.placeholders:
                x, y, w, h = _placeholder_box(shape, master, slide_w, slide_h)
                placeholders.append(
                    Placeholder(
                        # Always from the XML: python-pptx's enum spells `ctrTitle` CENTER_TITLE and
                        # `obj` OBJECT, and a manifest has to carry the OOXML names or the join with
                        # `<p:ph type>` at emit time fails. `_ph_type` reads the attribute itself.
                        type=_ph_type(shape),
                        idx=shape.placeholder_format.idx,
                        name=shape.name,
                        x=round(x / config.EMU_PER_PX, 3),
                        y=round(y / config.EMU_PER_PX, 3),
                        w=round(w / config.EMU_PER_PX, 3),
                        h=round(h / config.EMU_PER_PX, 3),
                        emu={"x": int(x), "y": int(y), "w": int(w), "h": int(h)},
                        style={"font": "+mn-lt", "sizePt": 18, "bold": False,
                               "color": "000000", "align": "left", "anchor": "top", "autofit": "none"},
                    )
                )
            layouts.append(
                Layout(
                    id=f"layout-{number:02d}",
                    name=layout.name,
                    masterIndex=master_index,
                    layoutIndex=layout_index,
                    partName=str(layout.part.partname),
                    background=f"layout-{number:02d}.png",
                    usage=_usage(placeholders),
                    placeholders=placeholders,
                )
            )

    return Manifest(
        slideWidthEmu=int(slide_w),
        slideHeightEmu=int(slide_h),
        canvas=canvas,
        masters=masters,
        theme=theme,
        textStyles={},
        bullets=[],
        layouts=layouts,
        assets=[],
        designNotes="Generated from python-pptx's default template for the torture suite.",
        importer="deterministic",
        sourceHash=sha256_of(pptx),
    )


def _ph_type(shape) -> str:
    """OOXML `<p:ph type>` as the manifest spells it (`title`, `body`, `ctrTitle`, `sldNum`, …)."""
    element = shape.element.find(
        ".//{http://schemas.openxmlformats.org/presentationml/2006/main}ph"
    )
    if element is not None and element.get("type"):
        return element.get("type")
    # python-pptx reports a bodyless default placeholder as OBJECT; OOXML calls it `obj`.
    return "obj"


def _usage(placeholders: list[Placeholder]) -> str:
    """A one-line description of what a layout is for, derived from its placeholder set (no LLM)."""
    kinds = {p.type for p in placeholders}
    if not kinds - {"dt", "ftr", "sldNum"}:
        return "blank"
    parts = []
    if kinds & {"title", "ctrTitle"}:
        parts.append("title")
    if "subTitle" in kinds:
        parts.append("subtitle")
    if kinds & {"body", "obj"}:
        parts.append("body")
    if "pic" in kinds:
        parts.append("picture")
    if "tbl" in kinds:
        parts.append("table")
    if "chart" in kinds:
        parts.append("chart")
    return " + ".join(parts) or "blank"


def build(stem: str, width_in: float, height_in: float) -> Path:
    """Write `<stem>.pptx`, its manifest and its layout PNGs; return the deck path."""
    presentation = Presentation()
    presentation.slide_width = Emu(round(width_in * config.EMU_PER_IN))
    presentation.slide_height = Emu(round(height_in * config.EMU_PER_IN))
    deck = HERE / f"{stem}.pptx"
    presentation.save(str(deck))

    manifest = build_manifest(deck)
    manifest.save(HERE / f"{stem}.manifest.json")

    layouts_dir = HERE / stem / "layouts"
    if layouts_dir.exists():
        shutil.rmtree(layouts_dir)
    renderer = PptxRenderClient.from_settings() or BlankLayoutsRenderer()
    mapping = renderer.render_layouts(deck, manifest.canvas_w, layouts_dir)
    for layout in manifest.layouts:
        mapping[layout.partName].replace(layouts_dir / f"{layout.id}.png")

    problems = manifest.validate()
    if problems:
        raise SystemExit(f"{stem}: generated manifest is invalid: {problems}")
    print(
        f"{stem}: {manifest.canvas_w}×{manifest.canvas_h} px, "
        f"{len(manifest.layouts)} layouts, "
        f"{sum(len(lay.placeholders) for lay in manifest.layouts)} placeholders -> {deck.name}"
    )
    return deck


# ---------------------------------------------------------------- synthetic brand masters (extra/)
#
# `extra/` holds four masters that stand in for real brand templates: "it works on any master" is the
# claim `test_import.py::test_extra_master_imports_and_validates` defends, so each one carries a
# property a default-template deck does not — 89 layouts on one master, a 14 × 8.5 in page, a portrait
# A4 page, layouts whose placeholders inherit their box from the master, theme fonts that are not
# installed, pictures on the master and on layouts, dark layout backgrounds, a picture placeholder with
# its own custom-geometry fill. Everything here is invented: no brand, no content, neutral metadata.
# They need no renderer and no manifest (the importer writes both), so `--extra-only` runs offline.

EXTRA_DIR = HERE / "extra"

_NS = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
       'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"')
_P_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
#: python-pptx's default template is 10 × 7.5 in; its master placeholders are scaled from that.
_DEFAULT_W, _DEFAULT_H = 9144000, 6858000
#: Every zip member is stamped with this, so the same script writes the same bytes.
_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
_AUTHOR = "Slide Studio test fixtures"


@dataclass(frozen=True)
class _Ph:
    """A layout placeholder: OOXML type (`obj` writes none), idx, box as slide fractions or None
    (no `<a:xfrm>`: the importer must inherit it from the master), and an optional text style."""
    type: str
    idx: int
    box: tuple[float, float, float, float] | None
    size: int | None = None          # hundredths of a point
    bold: bool = False
    color: str | None = None         # scheme colour name
    anchor: str | None = None
    fill: str | None = None          # scheme colour: the placeholder's own fill
    angled: bool = False             # a custom-geometry (angled) outline, as cover panels draw


@dataclass(frozen=True)
class _LayoutSpec:
    name: str
    placeholders: tuple[_Ph, ...]
    decorations: int = 0
    dark: bool = False
    logo: bool = False
    pattern: bool = False            # a grid of ~90 small dots instead of edge bars


@dataclass(frozen=True)
class _ExtraSpec:
    stem: str
    width_emu: int
    height_emu: int
    fonts: tuple[str, str]                       # (major, minor)
    master_types: tuple[str, ...]                # master placeholders kept, of title/body/dt/ftr/sldNum
    layouts: tuple[_LayoutSpec, ...]
    title: str
    master_logo: bool = False
    colours: tuple[str, ...] = ("1B2A41", "E8ECF1", "2A6F97", "E07A5F", "3D405B", "81B29A", "F2CC8F",
                                "5C80BC", "2A6F97", "6D597A")


def _title(box=(0.04, 0.05, 0.92, 0.10), **style) -> _Ph:
    return _Ph("title", 0, box, **style)


def _row(first_idx: int, n: int, y: float, h: float, x0: float = 0.04, x1: float = 0.96,
         kind: str = "body", gap: float = 0.02, **style) -> tuple[_Ph, ...]:
    """`n` placeholders side by side between `x0` and `x1`, numbered from `first_idx`."""
    w = (x1 - x0 - gap * (n - 1)) / n
    return tuple(_Ph(kind, first_idx + i, (x0 + i * (w + gap), y, w, h), **style) for i in range(n))


def _grid(first_idx: int, cols: int, rows: int, y0: float = 0.24, y1: float = 0.92, **style) -> tuple[_Ph, ...]:
    h = (y1 - y0 - 0.02 * (rows - 1)) / rows
    out: list[_Ph] = []
    for r in range(rows):
        out.extend(_row(first_idx + r * cols, cols, y0 + r * (h + 0.02), h, **style))
    return tuple(out)


def _many_layouts() -> tuple[_LayoutSpec, ...]:
    """89 layouts on one master: covers, agendas, dividers, n-box content, tables, charts, profiles,
    big numbers, diagrams, blanks — each in a light and a dark variant, plus numbered copies."""
    sub = dict(size=1600, color="tx2")

    def cover(k: int) -> tuple[_Ph, ...]:
        return (_Ph("body", 15, (0.06, 0.52, 0.50, 0.08), **sub),
                _Ph("body", 16, (0.06, 0.62, 0.50, 0.05), size=1200),
                _Ph("pic", 17, (0.60 + 0.02 * k, 0.0, 0.40 - 0.02 * k, 1.0), fill="accent6", angled=True),
                _title((0.06, 0.28, 0.50, 0.22), size=3600, bold=True, anchor="b"))

    def agenda(items: int) -> tuple[_Ph, ...]:
        rows = (items + 1) // 2
        return (_title(), _Ph("body", 10, (0.04, 0.16, 0.60, 0.05), **sub),
                *_grid(11, 2, rows, x1=0.66), _Ph("pic", 10 + items + 1, (0.70, 0.0, 0.30, 1.0)))

    def divider(pic: bool) -> tuple[_Ph, ...]:
        main = (_title((0.08, 0.36, 0.58, 0.18), size=4000, bold=True, anchor="b"),
                _Ph("body", 15, (0.08, 0.56, 0.58, 0.08), **sub))
        return main + ((_Ph("pic", 10, (0.70, 0.0, 0.30, 1.0)),) if pic else ())

    def content(n: int) -> tuple[_Ph, ...]:
        cols, rows = (n, 1) if n <= 4 else (4, 2)
        boxes = _grid(17, cols, rows)[:n]
        return (_title(), _Ph("body", 12, (0.04, 0.16, 0.92, 0.05), **sub), *boxes)

    def big_numbers(n: int) -> tuple[_Ph, ...]:
        numbers = _row(13, n, 0.28, 0.16, kind="body", size=4000, bold=True, color="accent1")
        texts = _row(13 + n, n, 0.46, 0.40)
        return (_title(), _Ph("body", 12, (0.04, 0.16, 0.92, 0.05), **sub), *numbers, *texts)

    def profiles(n: int) -> tuple[_Ph, ...]:
        pics = _row(15, n, 0.24, 0.30, kind="pic")
        names = _row(15 + n, n, 0.56, 0.06, bold=True)
        bios = _row(15 + 2 * n, n, 0.64, 0.28)
        return (_title(), _Ph("body", 12, (0.04, 0.16, 0.92, 0.05), **sub), *pics, *names, *bios)

    def graphic(n: int) -> tuple[_Ph, ...]:
        import math
        spots = []
        for i in range(n):
            angle = 2 * math.pi * i / n
            spots.append(_Ph("body", 20 + i, (0.42 + 0.30 * math.cos(angle), 0.54 + 0.30 * math.sin(angle) * 0.9,
                                              0.16, 0.08), size=1100))
        clipped = tuple(_Ph(s.type, s.idx, (min(max(s.box[0], 0.0), 0.84), min(max(s.box[1], 0.18), 0.90),
                                            s.box[2], s.box[3]), size=s.size) for s in spots)
        return (_title(), _Ph("body", 12, (0.04, 0.16, 0.92, 0.05), **sub), *clipped)

    base: list[_LayoutSpec] = []
    for k in range(1, 5):
        base.append(_LayoutSpec(f"Cover - Option {k}", cover(k), decorations=4, logo=k == 1))
    base.append(_LayoutSpec("Agenda - Ten Items", agenda(10), decorations=3))
    base.append(_LayoutSpec("Agenda - Six Items", agenda(6), decorations=3))
    for k in range(1, 7):
        base.append(_LayoutSpec(f"Section Divider - Option {k}", divider(k <= 2), decorations=6 + k))
    for n in (1, 2, 3, 4, 7):
        base.append(_LayoutSpec(f"Content - {n} Box{'es' if n > 1 else ''}", content(n), decorations=8))
    base.append(_LayoutSpec("Content - Text and Object", (
        _title(), _Ph("body", 12, (0.04, 0.16, 0.92, 0.05), **sub),
        _Ph("body", 17, (0.04, 0.24, 0.44, 0.66)), _Ph("obj", 21, (0.52, 0.24, 0.44, 0.58)),
        _Ph("body", 22, (0.52, 0.84, 0.44, 0.06), size=900)), decorations=8))
    base.append(_LayoutSpec("Content - Table", (
        _Ph("tbl", 13, (0.04, 0.24, 0.92, 0.66)), _title(),
        _Ph("body", 12, (0.04, 0.16, 0.92, 0.05), **sub)), decorations=8))
    base.append(_LayoutSpec("Content - Chart", (
        _title(), _Ph("body", 12, (0.04, 0.16, 0.92, 0.05), **sub),
        _Ph("chart", 14, (0.04, 0.24, 0.92, 0.66))), decorations=8))
    base.append(_LayoutSpec("Title Only", (_title(),), decorations=8))
    base.append(_LayoutSpec("Title and Subtitle", (_title(), _Ph("body", 15, (0.04, 0.16, 0.92, 0.05), **sub)),
                            decorations=8))
    base.append(_LayoutSpec("Blank - Corners", (), decorations=8))
    base.append(_LayoutSpec("Blank - Plain", (), decorations=2))
    base.append(_LayoutSpec("From and To", (_title(), _Ph("body", 12, (0.04, 0.16, 0.92, 0.05), **sub),
                                            *_grid(15, 2, 4, y0=0.26)), decorations=8))
    for n in (3, 4, 5):
        base.append(_LayoutSpec(f"Big Numbers - {n}", big_numbers(n), decorations=8))
    base.append(_LayoutSpec("Profiles - Four", profiles(4), decorations=8))
    base.append(_LayoutSpec("Profile - One", (
        _Ph("pic", 15, (0.04, 0.24, 0.28, 0.60)), _title(), _Ph("body", 12, (0.04, 0.16, 0.92, 0.05), **sub),
        *_grid(29, 2, 3, y0=0.24, x0=0.36)), decorations=12))
    base.append(_LayoutSpec("Quotation", (_title((0.10, 0.28, 0.80, 0.28), size=2800, anchor="ctr"),
                                          _Ph("body", 10, (0.10, 0.60, 0.80, 0.06), **sub)), decorations=5))
    base.append(_LayoutSpec("Quotation with Portrait", (
        _title((0.34, 0.28, 0.60, 0.28), size=2800, anchor="ctr"),
        _Ph("body", 10, (0.34, 0.60, 0.60, 0.06), **sub), _Ph("pic", 15, (0.06, 0.26, 0.24, 0.42))),
        decorations=5))
    base.append(_LayoutSpec("Diagram - Six Around", graphic(6), decorations=8))
    base.append(_LayoutSpec("Diagram - Twelve Around", graphic(12), decorations=8))
    base.append(_LayoutSpec("Images and Text", (
        *_row(15, 4, 0.24, 0.30, kind="pic"), _title(), _Ph("body", 12, (0.04, 0.16, 0.92, 0.05), **sub),
        *_row(19, 4, 0.56, 0.06, bold=True), *_row(23, 4, 0.64, 0.28)), decorations=8))
    base.append(_LayoutSpec("Case Study", (
        _Ph("pic", 10, (0.0, 0.0, 0.40, 1.0)), _title((0.44, 0.06, 0.52, 0.12)),
        _Ph("body", 12, (0.44, 0.20, 0.52, 0.05), **sub), _Ph("body", 16, (0.44, 0.28, 0.52, 0.62))),
        decorations=6))
    for k in range(1, 5):
        base.append(_LayoutSpec(f"Closing - Option {k}", (
            _title((0.08, 0.40, 0.50, 0.16), size=4400, bold=True),
            _Ph("pic", 17, (0.62, 0.0, 0.38, 1.0), fill="accent1", angled=k % 2 == 1)), decorations=k))
    base.append(_LayoutSpec("Content - Dot Pattern", (
        _Ph("body", 13, (0.04, 0.24, 0.50, 0.66)), _Ph("body", 12, (0.04, 0.16, 0.50, 0.05), **sub),
        _title((0.04, 0.05, 0.50, 0.10)), _Ph("pic", 15, (0.58, 0.24, 0.38, 0.40))), pattern=True))

    # Dark variants: the tx2 background, and text in bg1 (bg2 where the light one was coloured).
    dark = [_LayoutSpec(f"{spec.name} (Dark)",
                        tuple(replace(p, color="bg2" if p.color else "bg1")
                              if p.type in ("title", "body", "obj") else p for p in spec.placeholders),
                        spec.decorations, dark=True, pattern=spec.pattern) for spec in base]
    # PowerPoint numbers a copied layout "1_<name>"; the importer joins on partName, so a name that
    # repeats exactly ("Title Only" twice) must import as two layouts.
    copies = [_LayoutSpec("1_" + base[0].name, base[0].placeholders, 3),
              _LayoutSpec("1_Title Only", (_title(),), 4),
              _LayoutSpec("2_Title Only", (_title(size=2000),), 12),
              _LayoutSpec("1_Blank - Plain", (), 4),
              _LayoutSpec("1_" + base[6].name, base[6].placeholders, 9),
              _LayoutSpec("1_" + base[13].name, base[13].placeholders, 7),
              _LayoutSpec("Title Only", (_title(),), 0)]
    layouts = tuple(base + dark + copies)
    assert len(layouts) == 89, len(layouts)
    return layouts


#: The four synthetic masters `engine/tests/test_import.py::COMMITTED_EXTRA_MASTERS` expects.
EXTRA_SPECS: tuple[_ExtraSpec, ...] = (
    _ExtraSpec(
        stem="synthetic-89-layouts", width_emu=12192000, height_emu=6858000,
        fonts=("Georgia", "Arial"), master_types=("title", "body", "sldNum"),
        layouts=_many_layouts(), title="Synthetic master: 89 layouts on one master",
        colours=("1F2A44", "EEF1F5", "1F6FB2", "E8664A", "12233B", "4FA3A5", "F2B544", "2B4C7E",
                 "1F6FB2", "7A5C99"),
    ),
    _ExtraSpec(
        stem="synthetic-14x8.5in", width_emu=12801600, height_emu=7772400,
        # Not installed anywhere: exercises the importer's font-substitution reporting.
        fonts=("Fixture Sans", "Fixture Sans"), master_types=("title", "body"), master_logo=True,
        title="Synthetic master: 14 x 8.5 in page",
        colours=("1C2B39", "F3F1EC", "6F8FAF", "A3485A", "2F3E46", "8E9AAF", "CBB89D", "52796F",
                 "6F8FAF", "8E7DBE"),
        layouts=(
            _LayoutSpec("Cover Page", (
                _Ph("body", 13, (0.06, 0.08, 0.40, 0.05), size=1200),
                _Ph("subTitle", 1, (0.06, 0.36, 0.70, 0.16), size=3200, bold=True),
                _Ph("body", 10, (0.06, 0.56, 0.70, 0.06), size=1600),
                _Ph("body", 11, (0.06, 0.64, 0.40, 0.05)), _Ph("body", 12, (0.06, 0.70, 0.40, 0.05)),
                _Ph("body", 14, (0.06, 0.90, 0.40, 0.04), size=900)), decorations=4),
            _LayoutSpec("1_Cover Page", (
                _Ph("body", 13, (0.06, 0.08, 0.40, 0.05), size=1200),
                _Ph("subTitle", 1, (0.06, 0.40, 0.70, 0.16), size=3200, bold=True),
                _Ph("body", 10, (0.06, 0.60, 0.70, 0.06), size=1600),
                _Ph("body", 11, (0.06, 0.68, 0.40, 0.05)), _Ph("body", 12, (0.06, 0.74, 0.40, 0.05)),
                _Ph("body", 14, (0.06, 0.90, 0.40, 0.04), size=900)), decorations=3, dark=True),
            _LayoutSpec("Cover Page with Logo", (
                _Ph("pic", 14, (0.70, 0.08, 0.24, 0.12)),
                _Ph("body", 13, (0.06, 0.08, 0.40, 0.05), size=1200),
                _Ph("body", 10, (0.06, 0.40, 0.70, 0.12), size=3200, bold=True),
                _Ph("body", 11, (0.06, 0.56, 0.40, 0.05)), _Ph("body", 12, (0.06, 0.62, 0.40, 0.05)),
                _Ph("body", 15, (0.06, 0.90, 0.40, 0.04), size=900)), decorations=4, logo=True),
            _LayoutSpec("Content Page", (
                _title((0.05, 0.04, 0.90, 0.09)), _Ph("body", 14, (0.05, 0.14, 0.90, 0.05), size=1400),
                _Ph("obj", 12, (0.05, 0.21, 0.90, 0.68)), _Ph("body", 15, (0.05, 0.91, 0.70, 0.04), size=800),
                _Ph("body", 11, (0.80, 0.91, 0.15, 0.04), size=800))),
            _LayoutSpec("Section Page", (_title((0.08, 0.40, 0.80, 0.14), size=3600),
                                         _Ph("body", 1, (0.08, 0.56, 0.80, 0.08))), decorations=4),
            _LayoutSpec("1_Appendix", (_title((0.08, 0.40, 0.80, 0.14), size=3600),
                                       _Ph("body", 1, (0.08, 0.56, 0.80, 0.08))), decorations=4, dark=True),
            # No <a:xfrm>: the title's box comes from the master's title placeholder.
            _LayoutSpec("Custom Layout", (_Ph("title", 0, None),)),
            _LayoutSpec("2_Content Page", (
                _title((0.05, 0.04, 0.90, 0.09)), _Ph("body", 14, (0.05, 0.14, 0.90, 0.05), size=1400),
                _Ph("obj", 12, (0.05, 0.21, 0.44, 0.33)), _Ph("body", 15, (0.05, 0.91, 0.70, 0.04), size=800),
                _Ph("body", 11, (0.80, 0.91, 0.15, 0.04), size=800), _Ph("obj", 16, (0.51, 0.21, 0.44, 0.33)),
                _Ph("obj", 17, (0.05, 0.56, 0.44, 0.33)), _Ph("obj", 18, (0.51, 0.56, 0.44, 0.33)),
                _Ph("obj", 19, (0.05, 0.56, 0.90, 0.01)))),
        ),
    ),
    _ExtraSpec(
        stem="synthetic-portrait-a4", width_emu=6858000, height_emu=9906000,
        fonts=("Calibri", "Calibri"), master_types=("title", "body"), master_logo=True,
        title="Synthetic master: portrait A4",
        layouts=(_LayoutSpec("Full Page", (_Ph("title", 0, None),), decorations=2),),
    ),
    _ExtraSpec(
        stem="synthetic-widescreen", width_emu=12192000, height_emu=6858000,
        fonts=("Calibri", "Calibri"), master_types=("title", "body"),
        title="Synthetic master: widescreen",
        colours=("22313F", "F4F6F8", "C0392B", "2C7FB8", "34495E", "7FB3D5", "F5B041", "58D68D",
                 "2C7FB8", "AF7AC5"),
        layouts=(
            _LayoutSpec("Title", (_Ph("ctrTitle", 0, (0.08, 0.30, 0.84, 0.20), size=4000, bold=True, anchor="b"),
                                  _Ph("subTitle", 1, (0.08, 0.52, 0.84, 0.10), size=2000)), decorations=7,
                        logo=True),
            _LayoutSpec("Prepared For", (_title((0.08, 0.20, 0.60, 0.14)),), decorations=10),
            _LayoutSpec("Contents", (_title(), _Ph("obj", 1, (0.04, 0.18, 0.60, 0.72)),
                                     _Ph("body", 10, (0.68, 0.18, 0.28, 0.72), size=1200)), decorations=5),
            _LayoutSpec("Divider", (_title((0.08, 0.38, 0.84, 0.14), size=3600),
                                    _Ph("body", 1, (0.08, 0.54, 0.84, 0.08))), decorations=4, dark=True),
            _LayoutSpec("Appendix", (_title((0.08, 0.40, 0.84, 0.14), size=3600),), decorations=3),
            _LayoutSpec("Full", (_Ph("title", 0, None),), decorations=2),
            _LayoutSpec("1_Full", (_Ph("title", 0, None),)),
            _LayoutSpec("Banner", (_title((0.04, 0.04, 0.92, 0.10), color="bg1"),), decorations=5),
            _LayoutSpec("Disclaimer", (), decorations=6),
            _LayoutSpec("Style Guide", (), decorations=16),
        ),
    ),
)


def _logo_png() -> bytes:
    """A plain two-tone mark, drawn here so no image file has to be committed."""
    import io

    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (240, 96), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 95, 95), fill=(42, 111, 151, 255))
    draw.ellipse((24, 24, 71, 71), fill=(255, 255, 255, 255))
    draw.rectangle((112, 30, 239, 66), fill=(61, 64, 91, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def _emu_box(box: tuple[float, float, float, float], w: int, h: int) -> tuple[int, int, int, int]:
    x, y, bw, bh = box
    return round(x * w), round(y * h), round(bw * w), round(bh * h)


def _xfrm_xml(x: int, y: int, cx: int, cy: int) -> str:
    return f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'


_ANGLED = ('<a:custGeom><a:avLst/><a:gdLst/><a:ahLst/><a:cxnLst/><a:rect l="0" t="0" r="r" b="b"/>'
           '<a:pathLst><a:path w="1000" h="1000"><a:moveTo><a:pt x="300" y="0"/></a:moveTo>'
           '<a:lnTo><a:pt x="1000" y="0"/></a:lnTo><a:lnTo><a:pt x="1000" y="1000"/></a:lnTo>'
           '<a:lnTo><a:pt x="0" y="1000"/></a:lnTo><a:close/></a:path></a:pathLst></a:custGeom>')

_PH_NAMES = {"title": "Title", "ctrTitle": "Title", "subTitle": "Subtitle", "body": "Text Placeholder",
             "obj": "Content Placeholder", "pic": "Picture Placeholder", "tbl": "Table Placeholder",
             "chart": "Chart Placeholder"}


def _placeholder_xml(ph: _Ph, shape_id: int, w: int, h: int) -> str:
    type_attr = "" if ph.type == "obj" else f' type="{ph.type}"'
    idx_attr = "" if ph.idx == 0 else f' idx="{ph.idx}"'
    size_attr = ' sz="quarter"' if ph.type in ("body", "obj", "pic", "tbl", "chart") and ph.idx else ""
    geometry = ""
    if ph.box is not None:
        geometry = _xfrm_xml(*_emu_box(ph.box, w, h))
        if ph.angled:
            geometry += _ANGLED
        if ph.fill:
            geometry += f'<a:solidFill><a:schemeClr val="{ph.fill}"/></a:solidFill>'
    rpr = ""
    if ph.size or ph.bold or ph.color:
        size = f' sz="{ph.size}"' if ph.size else ""
        bold = ' b="1"' if ph.bold else ""
        colour = f'<a:solidFill><a:schemeClr val="{ph.color}"/></a:solidFill>' if ph.color else ""
        rpr = f"<a:defRPr{size}{bold}>{colour}</a:defRPr>"
    lst = f'<a:lstStyle><a:lvl1pPr marL="0" indent="0"><a:buNone/>{rpr}</a:lvl1pPr></a:lstStyle>' if rpr else "<a:lstStyle/>"
    anchor = f' anchor="{ph.anchor}"' if ph.anchor else ""
    prompt = "Click to edit" if ph.type != "pic" else "Picture"
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="{_PH_NAMES[ph.type]} {shape_id - 1}"/>'
            f'<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
            f'<p:nvPr><p:ph{type_attr}{size_attr}{idx_attr}/></p:nvPr></p:nvSpPr>'
            f'<p:spPr>{geometry}</p:spPr>'
            f'<p:txBody><a:bodyPr{anchor}/>{lst}<a:p><a:r><a:rPr lang="en-US"/><a:t>{prompt}</a:t></a:r>'
            f'</a:p></p:txBody></p:sp>')


def _decoration_xml(i: int, count: int, shape_id: int, w: int, h: int, pattern: bool) -> str:
    """Non-placeholder art: bars along the edges, or (pattern) a grid of dots, in theme colours."""
    if pattern:
        col, row = i % 10, i // 10
        x, y, cx, cy = round(w * (0.60 + col * 0.036)), round(h * (0.70 + row * 0.03)), round(w * 0.012), round(w * 0.012)
        geometry, colour = "ellipse", ("accent1", "accent2", "accent4")[i % 3]
    else:
        edge = i % 4
        step = i // 4
        thin = 0.012 + 0.004 * step
        span = 0.30 + 0.10 * (step % 3)
        x, y, cx, cy = {
            0: (0.0, 0.0, span, thin),                    # top-left bar
            1: (1.0 - thin, 1.0 - span, thin, span),      # right edge, bottom
            2: (1.0 - span, 1.0 - thin, span, thin),      # bottom-right bar
            3: (0.0, 0.0 + 0.05 * step, thin, span / 2),  # left edge
        }[edge]
        x, y, cx, cy = round(x * w), round(y * h), round(cx * w), round(cy * h)
        geometry, colour = "rect", ("accent1", "accent2", "tx2", "accent3")[i % 4]
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="Decoration {i + 1}"/><p:cNvSpPr/>'
            f'<p:nvPr userDrawn="1"/></p:nvSpPr><p:spPr>{_xfrm_xml(x, y, cx, cy)}'
            f'<a:prstGeom prst="{geometry}"><a:avLst/></a:prstGeom>'
            f'<a:solidFill><a:schemeClr val="{colour}"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr>'
            f'<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="en-US"/></a:p></p:txBody></p:sp>')


def _picture_xml(shape_id: int, rid: str, box: tuple[int, int, int, int]) -> str:
    return (f'<p:pic {_NS}><p:nvPicPr><p:cNvPr id="{shape_id}" name="Logo {shape_id - 1}"/>'
            f'<p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr userDrawn="1"/></p:nvPicPr>'
            f'<p:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>'
            f'<p:spPr>{_xfrm_xml(*box)}<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>')


def _layout_xml(spec: _LayoutSpec, w: int, h: int) -> str:
    shapes, shape_id = [], 2
    for i in range(90 if spec.pattern else spec.decorations):
        shapes.append(_decoration_xml(i, spec.decorations, shape_id, w, h, spec.pattern))
        shape_id += 1
    for ph in spec.placeholders:
        shapes.append(_placeholder_xml(ph, shape_id, w, h))
        shape_id += 1
    background = ('<p:bg><p:bgPr><a:solidFill><a:schemeClr val="tx2"/></a:solidFill><a:effectLst/></p:bgPr></p:bg>'
                  if spec.dark else "")
    name = spec.name.replace("&", "&amp;")
    return (f'<p:sldLayout {_NS} preserve="1"><p:cSld name="{name}">{background}<p:spTree>'
            '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/>'
            f'<a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>{"".join(shapes)}</p:spTree></p:cSld>'
            '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>')


def _retheme(theme_part, spec: _ExtraSpec) -> None:
    """Swap the default theme's fonts and colours for the spec's (plain text edits on the part blob)."""
    import re

    xml = theme_part.blob.decode("utf-8")
    major, minor = spec.fonts
    xml = re.sub(r'(<a:majorFont><a:latin typeface=")[^"]*', rf"\g<1>{major}", xml)
    xml = re.sub(r'(<a:minorFont><a:latin typeface=")[^"]*', rf"\g<1>{minor}", xml)
    slots = ("dk2", "lt2", "accent1", "accent2", "accent3", "accent4", "accent5", "accent6", "hlink", "folHlink")
    for slot, colour in zip(slots, spec.colours, strict=True):
        xml = re.sub(rf'(<a:{slot}><a:srgbClr val=")[0-9A-Fa-f]{{6}}', rf"\g<1>{colour}", xml)
    xml = xml.replace('name="Office Theme"', 'name="Fixture Theme"').replace(
        '<a:clrScheme name="Office">', '<a:clrScheme name="Fixture">').replace(
        '<a:fontScheme name="Office">', '<a:fontScheme name="Fixture">')
    theme_part._blob = xml.encode("utf-8")


def _shape_master(master, spec: _ExtraSpec) -> None:
    """Keep only the spec's master placeholders, scaled from the 10 × 7.5 in template to the page."""
    tree = master.shapes._spTree
    for shape in list(master.placeholders):
        ph = shape.element.find(f".//{_P_NS}ph")
        kind = ph.get("type") if ph is not None else "obj"
        if kind not in spec.master_types:
            tree.remove(shape.element)
            continue
        shape.left = round(shape.left * spec.width_emu / _DEFAULT_W)
        shape.width = round(shape.width * spec.width_emu / _DEFAULT_W)
        shape.top = round(shape.top * spec.height_emu / _DEFAULT_H)
        shape.height = round(shape.height * spec.height_emu / _DEFAULT_H)


def _normalised_zip(raw: bytes, spec: _ExtraSpec) -> bytes:
    """The saved package with fixed timestamps and neutral app properties, so a rerun is byte-identical."""
    import io
    import zipfile

    source = zipfile.ZipFile(io.BytesIO(raw))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "docProps/app.xml":
                text = data.decode("utf-8")
                text = text.replace("Office Theme", "Fixture Theme").replace(
                    "On-screen Show (4:3)", "Custom").replace("Microsoft Macintosh PowerPoint", "python-pptx")
                data = text.encode("utf-8")
            target.writestr(zipfile.ZipInfo(info.filename, date_time=_ZIP_TIME), data,
                            compress_type=zipfile.ZIP_DEFLATED)
    return out.getvalue()


def build_extra(spec: _ExtraSpec) -> Path:
    """Write `extra/<stem>.pptx` from python-pptx's default template; return its path."""
    import datetime as dt
    import io

    from pptx.opc.constants import CONTENT_TYPE as CT
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT
    from pptx.oxml import parse_xml
    from pptx.parts.slide import SlideLayoutPart

    presentation = Presentation()
    presentation.slide_width = Emu(spec.width_emu)
    presentation.slide_height = Emu(spec.height_emu)
    master = presentation.slide_master
    package = presentation.part.package

    _retheme(master.part.part_related_by(RT.THEME), spec)
    _shape_master(master, spec)
    for layout in list(presentation.slide_layouts):
        presentation.slide_layouts.remove(layout)

    logo = _logo_png()
    if spec.master_logo:
        _image, rid = master.part.get_or_add_image_part(io.BytesIO(logo))
        box = (round(spec.width_emu * 0.84), round(spec.height_emu * 0.03),
               round(spec.width_emu * 0.12), round(spec.width_emu * 0.048))
        master.shapes._spTree.append(parse_xml(_picture_xml(90, rid, box)))

    id_list = master.element.find(f"{_P_NS}sldLayoutIdLst")
    for number, layout_spec in enumerate(spec.layouts):
        partname = package.next_partname("/ppt/slideLayouts/slideLayout%d.xml")
        element = parse_xml(_layout_xml(layout_spec, spec.width_emu, spec.height_emu))
        part = SlideLayoutPart(partname, CT.PML_SLIDE_LAYOUT, package, element)
        part.relate_to(master.part, RT.SLIDE_MASTER)
        rid = master.part.relate_to(part, RT.SLIDE_LAYOUT)
        id_list.append(parse_xml(f'<p:sldLayoutId {_NS} id="{2147483649 + number}" r:id="{rid}"/>'))
        if layout_spec.logo:
            _image, image_rid = part.get_or_add_image_part(io.BytesIO(logo))
            tree = element.find(f"{_P_NS}cSld/{_P_NS}spTree")
            box = (round(spec.width_emu * 0.04), round(spec.height_emu * 0.88),
                   round(spec.width_emu * 0.10), round(spec.width_emu * 0.04))
            tree.append(parse_xml(_picture_xml(len(tree) + 100, image_rid, box)))

    core = presentation.core_properties
    core.author = core.last_modified_by = _AUTHOR
    core.title = spec.title
    core.subject = core.keywords = core.category = ""
    core.comments = "Generated by engine/fixtures/masters/make_test_masters.py"
    core.revision = 1
    core.created = core.modified = dt.datetime(2026, 1, 1)

    buffer = io.BytesIO()
    presentation.save(buffer)
    EXTRA_DIR.mkdir(exist_ok=True)
    deck = EXTRA_DIR / f"{spec.stem}.pptx"
    deck.write_bytes(_normalised_zip(buffer.getvalue(), spec))
    print(f"{spec.stem}: {spec.width_emu}x{spec.height_emu} EMU, {len(spec.layouts)} layouts -> {deck.name}"
          f" ({deck.stat().st_size:,} bytes)")
    return deck


if __name__ == "__main__":
    import sys

    if "--extra-only" not in sys.argv:
        for stem, width, height in SIZES:
            build(stem, width, height)
    for extra in EXTRA_SPECS:
        build_extra(extra)
