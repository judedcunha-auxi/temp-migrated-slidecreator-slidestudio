"""IR text → PowerPoint text. Where the fidelity of an export is won or lost.

The browser has already decided everything: which words are on which line, how wide each line is,
and where each line sits. This module's whole job is to make PowerPoint agree, while keeping the
text real, editable and re-themeable. Three mechanisms do that.

**Line locking.** A paragraph becomes one `a:p`; the browser's line breaks inside it become `a:br`,
and the frame is written `wrap="none"` (`_wraps`), so PowerPoint is never asked to break a line at
all. The box is still as wide as the widest line plus `config.TEXT_BOX_WIDTH_SLACK`, but that slack
is no longer what stops a second break: a target machine whose metrics come out wider than the
browser's (a substituted family, a heavier or synthesised weight) makes the line overhang its frame
by a few px, in the direction the paragraph is aligned to, instead of breaking it again and pushing
the paragraph into whatever sits below (fidelity F4). The fit check reports such a line as `overhang`.

**Width locking.** PowerPoint's own metrics are not the browser's (kerning, hinting, fractional
advances). Wherever a line is predicted a different width from the one the browser drew, the
difference is spent as tracking (`spc`) on that line's runs — negative to pull a too-wide line in,
positive to spread a too-narrow one out — so the text still says the same thing and still reflows if
edited, but it occupies the width the design measured.

**Vertical placement from the line boxes, never from a constant.** With exact line spacing
(`a:spcPts`) PowerPoint puts the extra leading *above* the glyphs, while CSS splits it half above
and half below. So for one line of height `L` whose font's natural height is `N = ascent + descent`:

    PowerPoint baseline = boxTop + L − descent
    browser   baseline = lineCentre + (ascent − descent) / 2
    ⇒ boxTop = browserBaseline + descent − L

Using the line box's **centre** rather than its top is deliberate: half-leading is symmetric, so the
centre is the same whether the extractor reports a rect of font height or of line height, and the
model stops depending on a browser detail. `config.TEXT_DY_BY_FONT` is the measured residual on top
of that (positive lifts the box), not the model itself — a constant standing in for the model is
what makes text drift on one master and not another.

Font metrics come from the installed font files via fontTools, and advance widths from Pillow on
the same file (master brief §10.2 — never `document.fonts.check`). The fit predictor
(`engine.verify.fit`) measures with this same index and face rule (`face_for_exact`, plan D4).
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import ImageFont
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn

from app.config import engine as config
from app.engine.emit.template import (
    EmitContext,
    emu,
    emu_length,
    gradient_fill,
    hexval,
    remove_all,
    set_alpha,
    solid_fill,
    sub,
    theme_font_token,
)
from app.engine.ir import Box, Element

_ALIGN = {
    "left": PP_ALIGN.LEFT,
    "center": PP_ALIGN.CENTER,
    "right": PP_ALIGN.RIGHT,
    "justify": PP_ALIGN.JUSTIFY,
}
_ANCHOR = {"top": MSO_ANCHOR.TOP, "middle": MSO_ANCHOR.MIDDLE, "bottom": MSO_ANCHOR.BOTTOM}

#: Pillow is asked for advance widths at this size and the result is scaled. Loading one face per
#: (family, weight, italic) instead of one per size keeps the cache small and the numbers stable.
_MEASURE_PX = 128.0

#: Never crunch a line by more than this fraction of the font size per character, whatever the
#: prediction says: beyond it the cause is a substituted font, not metric drift, and squeezing
#: makes the text unreadable instead of making it fit.
_MAX_TRACKING_RATIO = 0.12

#: 0.2 in. A text box narrower than this holding more than one character is a fit-check failure
#: (master brief §9) and, in PowerPoint, a column of single letters. The box is invisible, so
#: widening it to the floor costs nothing.
MIN_BOX_WIDTH_PX = 0.2 * 96

#: Metric-compatible stand-ins, tried before the generic fallbacks when a family is not installed.
#: Each has the same advance widths as the family it stands in for, so a line measured in it breaks
#: where PowerPoint (which has the real face on the client's machine) breaks it. The Linux container
#: has no Microsoft fonts: it installs Carlito (Calibri), Liberation (Arial, Times New Roman, Courier
#: New) and DejaVu (docs/deployment.md). Only *measurement* uses the stand-in; the file still names
#: the family the slide asked for (`emitted_face` looks the face up exactly, never through here).
METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "calibri": ("Carlito",),
    "calibri light": ("Carlito",),
    "cambria": ("Caladea",),
    "arial": ("Liberation Sans", "Arimo"),
    "helvetica": ("Liberation Sans", "Arimo"),
    "times new roman": ("Liberation Serif", "Tinos"),
    "courier new": ("Liberation Mono", "Cousine"),
}

#: Exact metric twins: faces designed to the same advance widths *and* vertical metrics as the family
#: they stand in for (Carlito for Calibri, Caladea for Cambria, Liberation for Arial / Helvetica /
#: Times New Roman / Courier New). Where the family itself is not installed but its twin is,
#: `font_index` files the twin's faces under the family's name, so every measurement, the installed
#: check (`extract.html.installed_families`) and the fit predictor treat the family as present: the
#: browser draws the twin there too (fontconfig's metric aliases), and a line measured in it breaks
#: where PowerPoint breaks it in the real face. `emitted_face` still names the family the slide asked
#: for. Calibri Light has no twin (Carlito has no light face), so it stays reported as missing.
METRIC_TWINS: dict[str, str] = {
    "calibri": "Carlito",
    "cambria": "Caladea",
    "arial": "Liberation Sans",
    "helvetica": "Liberation Sans",
    "times new roman": "Liberation Serif",
    "courier new": "Liberation Mono",
}


def twin_stands_in(face: Face | None, family: str) -> bool:
    """Whether `face` is `family`'s metric twin (filed under its name because `family` is absent)."""
    twin = METRIC_TWINS.get(family.strip().strip("'\"").strip().lower())
    return bool(face is not None and twin and face.typographic_family.lower() == twin.lower())


#: Substituted when a family (and its metric-compatible stand-in) is not installed. Everything
#: downstream still measures *something* sane, and the substitution is reported rather than hidden.
_FALLBACK_FAMILIES = ("Arial", "Helvetica", "Liberation Sans", "Segoe UI", "Calibri", "Carlito", "DejaVu Sans")


# ---------------------------------------------------------------------------------- font metrics


#: A subfamily that names a different *width* (Arial Narrow, Segoe UI Condensed…) is not a weight of
#: its typographic family: asked for "Arial", the browser never draws Arial Narrow.
_WIDTH_VARIANT_RE = re.compile(r"narrow|condensed|compressed|cond\b|extended|expanded|wide", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Face:
    """One installed font file: where it is, what it calls itself, and the metrics a layout needs.

    The two family names are what PowerPoint needs to name the face the browser drew: the
    typographic family (name ID 16, else 1) is the CSS family ("Calibri"), the legacy family (ID 1)
    is the name a `<a:latin typeface>` reaches this very file by ("Calibri Light"), because GDI —
    and so PowerPoint — groups faces into four-member legacy families (regular, italic, bold, bold
    italic) and nothing else.
    """

    typographic_family: str   # name ID 16, else ID 1      "Calibri", "Segoe UI", "Arial"
    legacy_family: str        # name ID 1                   "Calibri Light", "Segoe UI Semibold"
    subfamily: str            # name ID 17, else ID 2       "Light", "Semibold", "Narrow"
    path: Path
    index: int
    weight: int               # OS/2 usWeightClass
    italic: bool
    bold_member: bool         # OS/2 fsSelection bit 5, else head.macStyle bit 0: GDI's Bold member
    width_variant: bool       # the subfamily names a width, not a weight (see `_WIDTH_VARIANT_RE`)
    variable: bool            # an `fvar` table: one file, every weight — its name says nothing
    ascent: float             # em fractions, positive
    descent: float            # em fractions, positive
    line_gap: float           # em fractions

    @property
    def family(self) -> str:
        """The CSS family this face belongs to (the typographic family)."""
        return self.typographic_family

    @property
    def natural(self) -> float:
        """Ascent + descent in em fractions — the glyph box PowerPoint packs into a line."""
        return self.ascent + self.descent


def _scan_font_dirs() -> dict[str, list[Face]]:
    """Index every installed face by family name (typographic *and* legacy, both lower-cased).

    Indexing both name IDs matters: `calibril.ttf` is typographic family "Calibri" weight 300 and
    legacy family "Calibri Light" — and "Calibri Light" is exactly what a theme's `majorFont` says.
    One `Face` object is filed under both names, except that a width variant is filed under its
    legacy name only: `face_for_exact("Arial", …)` must never see Arial Narrow (the old index avoided
    it by path sort, which was luck).
    """
    from fontTools.ttLib import TTCollection, TTFont

    # fontTools narrates every odd `head.created` in the Windows font folder; not our news.
    logging.getLogger("fontTools").setLevel(logging.ERROR)

    index: dict[str, list[Face]] = {}
    for path in config.font_files():
        try:
            if path.suffix.lower() in (".ttc", ".otc"):
                fonts = list(TTCollection(str(path), lazy=True).fonts)
            else:
                fonts = [TTFont(str(path), lazy=True, fontNumber=0)]
        except Exception:  # nosec B112
            # ^ a broken or locked font file is skipped, not fatal
            continue
        for number, font in enumerate(fonts):
            try:
                names = font["name"]
                head = font["head"]
                hhea = font["hhea"]
                upem = float(head.unitsPerEm or 1000)
                ascent = abs(float(hhea.ascender)) / upem
                descent = abs(float(hhea.descender)) / upem
                gap = float(getattr(hhea, "lineGap", 0)) / upem
                os2 = font["OS/2"] if "OS/2" in font else None
                if ascent <= 0 and os2 is not None:              # some CFF faces leave hhea empty
                    ascent = abs(float(os2.usWinAscent)) / upem
                    descent = abs(float(os2.usWinDescent)) / upem
                weight = int(getattr(os2, "usWeightClass", 400) or 400)
                selection = int(getattr(os2, "fsSelection", 0) or 0)
                italic = bool(selection & 1)
                bold_member = bool(selection & 0x20) if os2 is not None else bool(int(head.macStyle) & 1)
                legacy = (names.getDebugName(1) or "").strip()
                typographic = (names.getDebugName(16) or "").strip() or legacy
                subfamily = (names.getDebugName(17) or names.getDebugName(2) or "Regular").strip()
                variable = "fvar" in font
            except Exception:  # nosec B112
                # ^ a face with unreadable tables is skipped, not fatal
                continue
            if not legacy:
                continue
            width_variant = bool(_WIDTH_VARIANT_RE.search(subfamily))
            face = Face(typographic, legacy, subfamily, path, number, weight, italic, bold_member,
                        width_variant, variable, ascent, descent, gap)
            keys = {legacy.lower()} if width_variant else {legacy.lower(), typographic.lower()}
            for key in sorted(keys):
                index.setdefault(key, []).append(face)
    return index


@lru_cache(maxsize=1)
def font_index() -> dict[str, list[Face]]:
    """The installed-font index, built once per process (~0.3 s over the Windows font folder).

    A family that is not installed but whose exact metric twin is (`METRIC_TWINS`) is filed under the
    twin's faces, so `face_for_exact("Calibri")` answers Carlito on a machine without Calibri.
    """
    index = _scan_font_dirs()
    for family, twin in METRIC_TWINS.items():
        if family not in index and twin.lower() in index:
            index[family] = list(index[twin.lower()])
    return index


@lru_cache(maxsize=1024)
def face_for_exact(family: str | None, weight: int = 400, italic: bool = False) -> Face | None:
    """The face the measuring browser draws for `(family, weight, italic)` — or `None`, no fallback.

    Italic faces first (with none, every face: the browser and PowerPoint both synthesise the slant),
    then the nearest weight; **at a tie, the heavier face when the request is ≥ 400 and the lighter
    below**; then the path name and collection index, so two machines with the same fonts choose the
    same file. The tie rule is Edge's (DirectWrite), measured 2026-09-24 with canvas `measureText` at
    40 px: Segoe UI 500 draws Semibold and Arial 800 draws Black, where a path tie-break chose Regular
    and Bold (16-WPG §2.2; `test_face_choice_matches_the_measuring_browser` re-measures it on every
    run, so the rule cannot drift from the browser silently).
    """
    if not family:
        return None
    faces = font_index().get(family.strip().strip("'\"").strip().lower())
    if not faces:
        return None
    styled = [face for face in faces if face.italic == bool(italic)] or list(faces)
    heavier_wins = int(weight) >= 400
    return min(
        styled,
        key=lambda f: (abs(f.weight - int(weight)), -f.weight if heavier_wins else f.weight,
                       str(f.path).lower(), f.index),
    )


@lru_cache(maxsize=512)
def face_for(family: str | None, weight: int = 400, italic: bool = False) -> Face | None:
    """The closest installed face, or `None` when neither the family nor a fallback is installed.

    `face_for_exact` on the family, then on its metric-compatible stand-ins (`METRIC_ALIASES`:
    Calibri -> Carlito), then on each of `_FALLBACK_FAMILIES` in turn: everything downstream still
    measures *something* sane, and `plan_text` reports the substitution.
    """
    candidates: list[str] = []
    if family:
        wanted = family.strip().strip("'\"")
        candidates.append(wanted)
        candidates.extend(METRIC_ALIASES.get(wanted.lower(), ()))
    candidates.extend(_FALLBACK_FAMILIES)
    for candidate in candidates:
        face = face_for_exact(candidate, weight, italic)
        if face is not None:
            return face
    return None


def forget_fonts() -> None:
    """Drop every cached font lookup — for a caller that has just changed `config.FONT_DIRS`."""
    font_index.cache_clear()
    face_for_exact.cache_clear()
    face_for.cache_clear()


@dataclass(frozen=True, slots=True)
class EmittedFace:
    """What `_write_run` names in the file for one run: typeface, `b`, `i` — and the face behind them."""

    typeface: str | None
    bold: bool
    italic: bool
    face: Face | None


def emitted_face(run: dict[str, Any], ctx: EmitContext) -> EmittedFace:
    """The typeface and bold/italic flags that make PowerPoint draw the face the browser measured.

    PowerPoint reaches a face through its **legacy** family and the `b` flag: `b="1"` asks for the
    family's Bold member, or — when the family has none — synthesises bold. So (16-WPG §2.3):

    (a) no installed face, or a variable one (its name says nothing about the weight): the family
        as requested (a theme token when it is a theme font) and `b` for weight ≥ 600, as before;
    (b) otherwise, with `L` = the face's legacy family: `b` when the face *is* the Bold member, or
        when the browser synthesised bold (weight ≥ 600 on a face lighter than 600 — the 700 title on
        a "Calibri Light" major); the typeface is `L`, as a theme token only when `L` is the family
        the run asked for. A distinct face (Calibri 300 → "Calibri Light", Segoe UI 600 → "Segoe UI
        Semibold") is always its literal name: the light face of the minor font is not the major
        slot, and a re-theme must not turn body text into the display face.

    Never `b="1"` on a heavy face that is not a Bold member: PowerPoint would embolden Semibold or
    Black a second time. Italic is passed through; PowerPoint synthesises oblique as the browser did.
    """
    requested = str(run.get("font") or "").strip().strip("'\"").strip()
    weight = int(run.get("weight") or 400)
    italic = bool(run.get("italic"))
    theme_fonts = getattr(ctx, "theme_fonts", None) or {}
    tokens = bool(ctx.options.theme_fonts)

    def named(family: str) -> str | None:
        return theme_font_token(family, theme_fonts) if tokens else (family or None)

    face = face_for_exact(requested, weight, italic) if requested else None
    if face is None or face.variable:
        return EmittedFace(named(requested), weight >= 600, italic, face)
    # A metric twin standing in for the requested family is written as that family: the file is for
    # PowerPoint on the client's machine, which has the real face.
    legacy = requested if twin_stands_in(face, requested) else face.legacy_family
    bold = face.bold_member or (weight >= 600 and face.weight < 600)
    typeface = named(legacy) if legacy.lower() == requested.lower() else legacy
    return EmittedFace(typeface, bold, italic, face)


def face_width_px(face: Face, text: str, size_px: float) -> float:
    """Advance width of `text` set in exactly this face — the one measurement emitter and fit share."""
    if not text:
        return 0.0
    return float(_pil_font(str(face.path), face.index).getlength(text)) * size_px / _MEASURE_PX


@lru_cache(maxsize=512)
def _pil_font(path: str, index: int) -> Any:
    return ImageFont.truetype(path, size=int(_MEASURE_PX), index=index)


def text_width_px(text: str, family: str | None, size_px: float, weight: int, italic: bool) -> float:
    """Advance width of `text` in px — the prediction the width lock compares against."""
    if not text:
        return 0.0
    face = face_for(family, weight, italic)
    if face is None:
        return 0.55 * size_px * len(text)                        # last-resort average advance
    try:
        font = _pil_font(str(face.path), face.index)
    except Exception:
        return 0.55 * size_px * len(text)
    return float(font.getlength(text)) * size_px / _MEASURE_PX


def run_metrics(run: dict[str, Any]) -> tuple[float, float, float]:
    """`(ascent px, descent px, size px)` for one run, from its own family/weight/italic."""
    size = float(run.get("sizePx") or 0.0)
    face = face_for(run.get("font"), int(run.get("weight") or 400), bool(run.get("italic")))
    if face is None:
        return 0.8 * size, 0.2 * size, size
    return face.ascent * size, face.descent * size, size


# ------------------------------------------------------------------------------------- layout


@dataclass(slots=True)
class LineLayout:
    """One browser line, ready to write: its runs, its tracking correction and its geometry."""

    runs: list[dict[str, Any]]
    ascent: float
    descent: float
    centre: float
    width: float                 # what the browser measured
    predicted: float             # what PowerPoint is predicted to draw
    tracking_px: float = 0.0     # negative: the width lock's per-character correction
    x: float = 0.0               # where the browser put the line's first glyph (canvas px)
    x2: float = 0.0              # … and its last glyph's right edge

    @property
    def cx(self) -> float:
        return (self.x + self.x2) / 2.0

    def drawn(self) -> float:
        """The width PowerPoint needs for this line: the browser's, or the prediction if wider."""
        return max(self.width, self.predicted + self.tracking_px * _chars(self))


@dataclass(slots=True)
class ParagraphLayout:
    source: dict[str, Any]
    lines: list[LineLayout]
    spacing_px: float            # exact line spacing for every line of this paragraph
    space_before_px: float = 0.0
    space_after_px: float = 0.0
    # Horizontal placement (WP-B §3.6): the paragraph's own alignment and the margins that put its
    # lines where the browser drew them inside the placed box — `marL`, `marR`, `indent` in px.
    align: str = "left"
    mar_left_px: float = 0.0
    mar_right_px: float = 0.0
    indent_px: float = 0.0


@dataclass(slots=True)
class TextLayout:
    """Everything the writer needs: where the box goes and what goes in it."""

    box: Box
    paragraphs: list[ParagraphLayout]
    align: str = "left"
    warnings: list[str] = field(default_factory=list)
    substituted: list[str] = field(default_factory=list)


def _line_geometry(line: dict[str, Any]) -> tuple[float, float, float]:
    """`(centre y, top, height)` of a line box, tolerating a line the extractor left unmeasured."""
    box = line.get("box") or {}
    top = float(box.get("y") or 0.0)
    height = float(box.get("h") or 0.0)
    return top + height / 2.0, top, height


def plan_text(element: Element, ctx: EmitContext) -> TextLayout:
    """Measure the element and decide the box, the spacing and the per-line tracking."""
    paragraphs_in = element.paragraphs or []
    align = str((paragraphs_in[0].get("align") if paragraphs_in else None) or "left")
    planned, warnings, substituted = _plan_paragraphs(paragraphs_in, ctx, element)

    if not planned:
        return TextLayout(box=element.box, paragraphs=[], align=align, warnings=["no measurable lines"])

    if ctx.options.width_lock:
        # Only the extractor's line boxes are the glyphs' own extent (it writes `x-run-boxes` beside
        # them); a hand-built IR's line box may be the whole frame, which is no width to spread onto.
        spread = "x-run-boxes" in (element.extras or {})
        for paragraph in planned:
            for line in paragraph.lines:
                line.tracking_px = _tracking_for(line, warnings, spread=spread)

    if ctx.options.line_lock:
        _fit_lone_line(planned)          # r1c: a lone line is written at its own height, where PowerPoint agrees
    box = _place_box(element, planned, align, ctx)
    _set_margins(planned, box)           # E1: against the placed box, so growth never moves a line
    _warn_unstacked(planned, warnings)   # E3
    return TextLayout(box=box, paragraphs=planned, align=align, warnings=warnings,
                      substituted=sorted(set(substituted)))


def _plan_paragraphs(
    paragraphs_in: list[dict[str, Any]], ctx: EmitContext, element: Element
) -> tuple[list[ParagraphLayout], list[str], list[str]]:
    """Per line: its metrics, the browser's width and PowerPoint's predicted one — for text boxes and cells.

    `element` places a line the extractor left unmeasured (`_line_x`); a cell passes an origin.
    Returns `(planned, warnings, substituted)`. A paragraph with no measurable line is skipped, so
    `planned[i].source` (not the index) says which input paragraph a plan belongs to.
    """
    warnings: list[str] = []
    substituted: list[str] = []
    planned: list[ParagraphLayout] = []
    for paragraph in paragraphs_in:
        lines_in = [line for line in (paragraph.get("lines") or []) if line.get("runs")]
        if not lines_in:
            continue
        lines: list[LineLayout] = []
        for line in lines_in:
            runs = list(line.get("runs") or [])
            ascent = descent = 0.0
            for run in runs:
                if run.get("font") and face_for_exact(run.get("font")) is None:
                    substituted.append(str(run.get("font")))
                run_ascent, run_descent, _ = run_metrics(run)
                ascent = max(ascent, run_ascent)
                descent = max(descent, run_descent)
            centre, _, _ = _line_geometry(line)
            measured = float((line.get("box") or {}).get("w") or 0.0)
            predicted = sum(
                text_width_px(
                    str(run.get("text") or ""),
                    run.get("font"),
                    float(run.get("sizePx") or 0.0),
                    int(run.get("weight") or 400),
                    bool(run.get("italic")),
                )
                + float(run.get("letterSpacingPx") or 0.0) * len(str(run.get("text") or ""))
                for run in runs
            )
            # Without the run's own family installed, `text_width_px` measures a fallback family (or,
            # with none at all, a last-resort average advance). That is a guess, not a prediction,
            # and squeezing a line toward it runs the words together — so the browser's own width
            # stands and the width lock leaves the line alone.
            if any(face_for_exact(run.get("font"), int(run.get("weight") or 400),
                                  bool(run.get("italic"))) is None
                   for run in runs):
                predicted = measured
            # The same reasoning for a character the measured face has no glyph for (an emoji flag,
            # a symbol): Pillow measures its `.notdef` box, the browser drew a fallback font's glyph.
            # The predicted width is a guess there too, so the lock leaves the line alone.
            missing = [
                str(run.get("text") or "") for run in runs
                if not _has_glyphs(face_for(run.get("font"), int(run.get("weight") or 400), bool(run.get("italic"))),
                                   str(run.get("text") or ""))
            ]
            if missing and predicted != measured:
                predicted = measured
                text = "".join(str(run.get("text") or "") for run in runs)
                warnings.append(
                    f"line {text[:24]!r}: characters with no glyph in the measured face — the browser drew a "
                    f"fallback font there, so the width lock leaves this line alone"
                )
            x = _line_x(line, element)
            lines.append(LineLayout(runs, ascent, descent, centre, measured, predicted, x=x, x2=x + measured))

        spacing = _paragraph_spacing(paragraph, lines)
        planned.append(
            ParagraphLayout(
                source=paragraph,
                lines=lines,
                spacing_px=spacing,
                space_before_px=float(paragraph.get("spaceBeforePx") or 0.0),
                space_after_px=float(paragraph.get("spaceAfterPx") or 0.0),
                align=str(paragraph.get("align") or "left"),
            )
        )
    return planned, warnings, substituted


def _has_glyphs(face: Face | None, text: str) -> bool:
    """Does `face` draw every visible character of `text`? Controls and joiners draw nothing anyway.

    The face's character map is read once (fontTools) and kept on this function.
    """
    import unicodedata

    if face is None or not text:
        return True
    cmaps: dict[tuple[str, int], frozenset[int]] = _has_glyphs.__dict__.setdefault("cmaps", {})
    key = (str(face.path), face.index)
    if key not in cmaps:
        from fontTools.ttLib import TTFont

        try:
            cmaps[key] = frozenset((TTFont(key[0], fontNumber=key[1], lazy=True).getBestCmap() or {}).keys())
        except Exception:                                        # a broken or locked font file
            cmaps[key] = frozenset()
    cmap = cmaps[key]
    if not cmap:
        return True
    return all(ord(ch) in cmap or unicodedata.category(ch) in ("Cc", "Cf", "Mn", "Me", "Zl", "Zp")
               for ch in text)


def _line_x(line: dict[str, Any], element: Element) -> float:
    """The line's left edge; a line the extractor left unmeasured sits at the element's edge."""
    box = line.get("box") or {}
    if "x" not in box or float(box.get("w") or 0.0) <= 0.0:
        return float(element.box.x)
    return float(box["x"])


def _set_margins(planned: list[ParagraphLayout], box: Box) -> None:
    """E1 (WP-B §3.6): each paragraph's `marL`/`marR`/`indent`, so its lines start where the browser
    put them inside the placed box `box`.

    * left / justify: `marL` from where lines 2..k start (line 1 alone for one line), `indent` the
      first line's offset from there — positive after a swatch or a `text-indent`, negative for a
      hanging first line (a hand-made bullet, B1 critique #14); `marR` 0. Justify still stretches
      its lines into the box's slack, as it always has.
    * right: `marR` = the box's right edge − the lines' right edge.
    * center: PowerPoint centres in `[x + marL, x2 − marR]`, moving the centre by `(marL − marR)/2`,
      so an offset `d` of the lines' mean centre is `marL = 2d` (d > 0) or `marR = −2d`.
    * bullet (left): `marL` = where the text starts, `indent = −marL` — the hanging indent, the
      bullet at the box's left edge (the list's), equal to `indentPx` for an ordinary item.
    """
    for paragraph in planned:
        lines = paragraph.lines
        if not lines:
            continue
        first = lines[0]
        bullet = paragraph.source.get("bullet")
        align = paragraph.align
        paragraph.mar_left_px = paragraph.mar_right_px = paragraph.indent_px = 0.0
        if align == "right":
            paragraph.mar_right_px = max(0.0, box.x2 - max(line.x2 for line in lines))
        elif align == "center":
            offset = sum(line.cx for line in lines) / len(lines) - box.cx
            if offset > 0:
                paragraph.mar_left_px = 2.0 * offset
            else:
                paragraph.mar_right_px = -2.0 * offset
        elif bullet:
            paragraph.mar_left_px = max(0.0, first.x - box.x)
            paragraph.indent_px = -paragraph.mar_left_px
        else:
            rest = [line.x for line in lines[1:]]
            base = min(rest) if rest else first.x
            paragraph.mar_left_px = max(0.0, base - box.x)
            indent = (first.x - box.x) - paragraph.mar_left_px
            paragraph.indent_px = max(indent, -paragraph.mar_left_px) if abs(indent) >= 0.5 else 0.0


def _warn_unstacked(planned: list[ParagraphLayout], warnings: list[str]) -> None:
    """E3: consecutive paragraphs of one element sit under one another, or the extractor merged a
    row (WP-B R1) — say so rather than write two side-by-side texts as a stack."""
    for index in range(1, len(planned)):
        previous, current = planned[index - 1], planned[index]
        if not previous.lines or not current.lines:
            continue
        natural = (previous.spacing_px + current.spacing_px) / 2.0
        if current.lines[0].centre - previous.lines[-1].centre < 0.5 * natural:
            text = "".join(str(run.get("text") or "") for run in current.lines[0].runs)[:24]
            warnings.append(f"paragraph {index + 1} ({text!r}) is not below paragraph {index}: side by side "
                            f"in the design, stacked in the export")


def _paragraph_spacing(paragraph: dict[str, Any], lines: list[LineLayout]) -> float:
    """Exact line spacing for a paragraph: what the browser actually advanced, not the CSS value.

    With two or more lines the measured centre-to-centre distance is the truth (it already contains
    whatever the strut, an inline image or a larger run did to the line box). A single line has no
    advance to measure, so the CSS `line-height` stands in.
    """
    if len(lines) >= 2:
        deltas = [b.centre - a.centre for a, b in zip(lines, lines[1:], strict=False)]
        positive = [d for d in deltas if d > 0]
        if positive:
            return sum(positive) / len(positive)
    declared = float(paragraph.get("lineHeightPx") or 0.0)
    if declared > 0:
        return declared
    line = lines[0]
    return max(line.ascent + line.descent, 1.0)


def _tracking_for(line: LineLayout, warnings: list[str], *, spread: bool = True) -> float:
    """Per-character tracking that pulls a line onto the width the browser measured.

    Bidirectional: PowerPoint's metrics are not the browser's in either direction, so a line the
    prediction says is *too wide* is squeezed (negative tracking) and one it says is *too narrow* is
    spread (positive tracking). Only closing the over-wide case would leave right- and centre-aligned
    text — and everything downstream of it — sitting a fraction off wherever PowerPoint draws the
    glyphs tighter than Chromium did. Both directions are bounded by the same per-character ratio:
    past it the cause is a substituted font, not metric drift, and forcing the fit makes the text
    unreadable (crushed) or gappy (spaced out) instead. `spread=False` (a line box that need not be
    the glyphs' extent) keeps only the squeeze.
    """
    if line.width <= 0 or abs(line.predicted - line.width) <= 0.25:
        return 0.0
    if not spread and line.predicted < line.width:
        return 0.0
    characters = sum(len(str(run.get("text") or "")) for run in line.runs)
    if characters <= 1:                                          # no inter-character gaps to spend
        return 0.0
    correction = (line.width - line.predicted) / characters * config.WIDTH_LOCK_FACTOR
    size = max((float(run.get("sizePx") or 0.0) for run in line.runs), default=12.0)
    limit = _MAX_TRACKING_RATIO * size
    if abs(correction) > limit:
        capped = -limit if correction < 0 else limit
        warnings.append(
            f"line {''.join(str(r.get('text') or '') for r in line.runs)[:24]!r}: predicted "
            f"{line.predicted:.1f}px vs measured {line.width:.1f}px — tracking capped at {capped:.2f}px/char"
        )
        return capped
    return correction


#: PowerPoint's single line spacing, in ems of the line's largest run, whatever the face. At or above
#: it, an exact spacing `L` (`a:spcPts`) sets the line's baseline about three quarters of `L` below
#: the line's top; PptxRender, and the model `_place_box` places a box by, set it at `L − descent`
#: (all the leading above the glyphs). Below it the two agree. Measured 2026-09-25 (r1c,
#: `engine/fixtures/fidelity/lone_line_evidence.py`, PowerPoint through `render_pptx.ps1`): one
#: top-anchored line in Arial, Arial Bold, Calibri, Calibri Bold, Calibri Light, Segoe UI, Segoe UI
#: Semibold, Segoe UI Bold and Times New Roman at 16 and 64 px, spacing swept 0.9–1.6 em in steps of
#: 0.025 em. PowerPoint minus PptxRender (ink centroid): −1 to +1 px at 16 px and −1 to +3 px at 64 px
#: up to 1.175 em, for every face; from 1.2 em on 0 to −1 px at 16 px and −2 to −3 px at 64 px, falling
#: a further ~0.24 px per px of spacing (−3 px at 1.6 em and 16 px, −9 px at 64 px; −15 px for 20 px
#: text in an 80 px line). PowerPoint also lays an `a:spcPts` spacing out at a whole number of points
#: (`lone_line_evidence.py pitch`), which is where its within-a-pixel scatter comes from. a client deck's 20 px
#: "×" in its 60 px line and a 14 px chevron label in a 56 px one: −10 px. Written
#: by `_fit_lone_line`, the eleven `lone_line_evidence.LONE_CASES` (four faces, 11–32 px, 1.2–4.1 em)
#: land 0 to 2 px below PptxRender in PowerPoint, on PptxRender's own unchanged pixels.
POWERPOINT_SINGLE_EM = 1.2

#: The most a lone line is written at, in ems: clear of `POWERPOINT_SINGLE_EM` (1.175 em still
#: measured in agreement). Of the faces measured, only Segoe UI's own height (1.33 em) is above it.
LONE_LINE_MAX_EM = 1.15


def _fit_lone_line(planned: list[ParagraphLayout]) -> None:
    """A paragraph of one line is written at its glyphs' own height, not at its line-height.

    A lone line has no pitch to keep: its exact spacing only decides how far below its line's top
    the baseline sits. For the first paragraph `_place_box` cancels that by moving the frame; for a
    later one `_exact_gap` does, by the space before it (it keeps `gap + L`, so the baseline, every
    later paragraph and the frame's bottom stay where they were). When the spacing is at or above
    PowerPoint's single spacing (`POWERPOINT_SINGLE_EM`) — a label centred by `line-height: 60px`, a
    chevron's caption, a counter in a circle, a KPI label under its number, and any one-line text at
    `line-height: 1.35` — PowerPoint and PptxRender part on where that baseline is (PowerPoint drew
    a client deck's operators 10 px above the browser and PptxRender). So the spacing becomes ascent +
    descent, capped at `LONE_LINE_MAX_EM`, where the two agree: the baseline lands where the browser
    drew it in both renderers, and a one-line frame is the line's own height rather than the CSS
    line box. (A rotated lone line turns about its frame's centre, which now sits on the glyphs, as
    the browser's line box centre does, rather than half the leading above them.)

    A later one-line paragraph that the browser set closer to the one above than its own spacing
    allows (`_exact_gap` < 0; `a:spcBef` cannot be negative) is shortened by that much as well, which
    lands it instead of leaving it low (a client slide's 13 px pillar names, 16 px lines under 9 px
    labels, sat 1 px low).

    Until r2 (plan §16 #29) only an element of a single one-line paragraph was fitted, because
    `paragraph_gap` assumed each paragraph kept the leading it was designed with. A paragraph of more
    lines keeps the browser's pitch, and with it PowerPoint's first-line offset when that pitch is
    tall (a renderer difference the file cannot remove without changing the pitch).
    """
    for index, paragraph in enumerate(planned):
        if len(paragraph.lines) != 1:
            continue
        line = paragraph.lines[0]
        size = max((float(run.get("sizePx") or 0.0) for run in line.runs), default=0.0)
        if size <= 0.0:
            continue
        target = paragraph.spacing_px
        # 0.05 px under the switch: a CSS 1.2 lands on it exactly, and PowerPoint already counts it as tall.
        if paragraph.spacing_px >= POWERPOINT_SINGLE_EM * size - 0.05:
            target = min(line.ascent + line.descent, LONE_LINE_MAX_EM * size)
        if index:
            target = min(target, paragraph.spacing_px + _exact_gap(planned, index))
        if target >= paragraph.spacing_px:
            continue
        # Shortened by whole pixels, so a first paragraph's frame moves down by whole pixels and keeps
        # every fraction the renderers snap on: PptxRender draws the line on exactly the pixels it did
        # at the CSS spacing (a fractional shortening moved a client deck's legend lines by 1 px, +76 px² at the
        # gate). A later paragraph's space before it grows by the same whole pixels.
        paragraph.spacing_px -= math.ceil(paragraph.spacing_px - target - 1e-9)


def _place_box(element: Element, planned: list[ParagraphLayout], align: str, ctx: EmitContext) -> Box:
    """The text box: wide enough never to re-wrap, positioned so the first baseline lands right."""
    first = planned[0].lines[0]
    if ctx.options.line_lock:
        dy = _first_line_dy(first)
        top = (first.centre + (first.ascent - first.descent) / 2.0) + first.descent \
            - planned[0].spacing_px - dy
    else:
        # Line locking off: PowerPoint lays the paragraph out itself, so the measured box is all
        # there is to go on. This is the A/B arm, not the one the acceptance gate is measured with.
        top = float(element.box.y)

    # E2 (WP-B §3.6): the box covers the element and, for every line, the width PowerPoint will
    # draw it at, laid out from the edge its own paragraph is aligned to — a left line grows right
    # from where it starts, a right line left from where it ends, a centred one both ways about its
    # centre. So the aligned edge stays pinned (a right-aligned element no longer grows its text
    # rightwards, off where the browser ended it), a mixed element gets room on the side each
    # paragraph needs, and `_set_margins`, which reads the placed box, never moves a line.
    lo, hi = float(element.box.x), float(element.box.x2)
    for paragraph in planned:
        for line in paragraph.lines:
            drawn = line.drawn()
            if paragraph.align == "right":
                lo, hi = min(lo, line.x2 - drawn), max(hi, line.x2)
            elif paragraph.align == "center":
                lo, hi = min(lo, line.cx - drawn / 2.0), max(hi, line.cx + drawn / 2.0)
            else:
                lo, hi = min(lo, line.x), max(hi, line.x + drawn)
    needed = hi - lo
    target = max(needed * config.TEXT_BOX_WIDTH_SLACK,
                 MIN_BOX_WIDTH_PX if _characters(planned) > 1 else 0.0)

    # The extra width is invisible and therefore free — but only if it grows away from the edge the
    # text is aligned to: widening a right-aligned box to the right would move every line. And it
    # is trimmed rather than allowed to hang off the slide, because the fit check counts a box
    # outside the canvas. Trimming the growth never moves the text; clamping the origin would.
    grown = max(0.0, target - needed)
    # Justify grows both ways, as it always has: `_set_margins` holds its lines' start with `marL`, and
    # the stretch PowerPoint gives them runs into half the slack instead of all of it.
    left_growth = grown if align == "right" else (grown / 2.0 if align in ("center", "justify") else 0.0)
    right_growth = grown - left_growth
    left_growth = min(left_growth, max(0.0, lo))
    right_growth = min(right_growth, max(0.0, float(ctx.canvas.w) - hi))

    height = _total_height(planned) if ctx.options.line_lock else float(element.box.h)
    return Box(lo - left_growth, top, needed + left_growth + right_growth, max(height, 1.0))


def _characters(planned: list[ParagraphLayout]) -> int:
    return sum(_chars(line) for paragraph in planned for line in paragraph.lines)


def _chars(line: LineLayout) -> int:
    return sum(len(str(run.get("text") or "")) for run in line.runs)


def _first_line_dy(line: LineLayout) -> float:
    """The measured residual for this face (`config.TEXT_DY_BY_FONT`), positive lifting the box.

    Keyed by the face the browser drew (`face_for_exact`): its legacy family, then its typographic
    family, then the family the run asked for, then `""`. So Calibri 300 gets Calibri Light's
    residual, "Segoe UI Semibold" gets Segoe UI's, and an absent "Lexend" keeps its own key rather
    than the fallback Arial's.
    """
    return dy_factor(line.runs[0] if line.runs else {}) * max(
        (float(run.get("sizePx") or 0.0) for run in line.runs), default=0.0)


def dy_factor(run: dict[str, Any]) -> float:
    """`config.TEXT_DY_BY_FONT` for the face this run is drawn in (see `_first_line_dy`)."""
    requested = str(run.get("font") or "").strip().strip("'\"").strip()
    face = face_for_exact(requested, int(run.get("weight") or 400), bool(run.get("italic"))) \
        if requested else None
    table = config.TEXT_DY_BY_FONT
    weight, italic = int(run.get("weight") or 400), bool(run.get("italic"))
    keys = ([face.legacy_family, face.typographic_family] if face is not None else []) + [requested]
    for key in keys:
        # A per-face key ("Calibri|700", "Arial|400|i") wins over its plain family (`_dy_factor`).
        for exact in ([f"{key}|{weight}|i"] if italic else []) + [f"{key}|{weight}"] \
                + ([f"{key}|i"] if italic else []) + [key]:
            if exact in table:
                return table[exact]
    return table.get("", 0.0)


def _dy_factor(table: dict[str, float], family: str, weight: int, italic: bool) -> float:
    """The first-baseline residual for a face, most specific key first.

    Hinting and rounding differ by weight and slant, so `TEXT_DY_BY_FONT` may carry per-face keys
    (`"Calibri|700"`, `"Arial|400|i"`) alongside the plain family key. The lookup degrades from the
    exact face to the family to the `""` fallback, so a table that only has family keys — which is all
    the WP9 pass measured — behaves exactly as it did before.
    """
    keys = []
    if family:
        if italic:
            keys.append(f"{family}|{weight}|i")
        keys.append(f"{family}|{weight}")
        if italic:
            keys.append(f"{family}|i")
        keys.append(family)
    keys.append("")
    for key in keys:
        if key in table:
            return table[key]
    return 0.0


def _total_height(planned: list[ParagraphLayout]) -> float:
    """How tall PowerPoint will make the frame: every line's exact spacing plus paragraph gaps."""
    total = 0.0
    for index, paragraph in enumerate(planned):
        gap = paragraph_gap(planned, index)
        total += gap + paragraph.spacing_px * len(paragraph.lines)
    return total


def paragraph_gap(planned: list[ParagraphLayout], index: int) -> float:
    """Extra space before paragraph `index` so its first line lands where the browser put it.

    Derived from the measured line centres, not from `spaceBeforePx`/`spaceAfterPx`: margin
    collapsing means the CSS values and the distance on screen are often different numbers, and the
    distance on screen is the one the gate compares. `a:spcBef` cannot be negative, so a paragraph
    the browser set closer than that is clamped here (`_fit_lone_line` shortens a one-line
    paragraph's spacing instead, which lands it).
    """
    return max(0.0, _exact_gap(planned, index))


def _exact_gap(planned: list[ParagraphLayout], index: int) -> float:
    """The `spcBef` (px, unclamped) that puts paragraph `index`'s first baseline the browser's
    distance below the previous paragraph's last one — under a change of leading as well.

    With `T` the top of the previous paragraph's last line and exact spacing `L`, PowerPoint and
    PptxRender set a baseline `drop` above its line's bottom (`_baseline_metrics`), so the two baselines
    are `T + L₁ − drop₁` and `T + L₁ + gap + L₂ − drop₂`; the browser drew them `offset` below their
    line boxes' centres `c`. Hence

        gap = (c₂ + offset₂) − (c₁ + offset₁) − L₂ + drop₂ − drop₁

    which needs neither the previous paragraph's spacing nor a calibration constant: both lines sit
    in one frame, so whatever the first line's placement residual is (`_first_line_dy`) it is common
    to them. The old rule, `Δc − (L₁ + L₂)/2`, is this one only when the two lines share face, size
    and leading; a 34 px KPI number at `line-height: 36px` over an 11.5 px label at 14 px put the
    label 2.6 px low in both renderers (fidelity r2, plan §16 #29; a client slide: 4,620 → 80 px²).
    A face that is not installed falls back to the same model on `run_metrics` and
    `_first_line_dy`: `Δc − L₂ + (N₂ − N₁)/2 + dy₁ − dy₂`, with `N` ascent + descent.
    """
    if index == 0:
        return 0.0
    above, below = planned[index - 1].lines[-1], planned[index].lines[0]
    spacing = planned[index].spacing_px
    first, second = _baseline_metrics(above), _baseline_metrics(below)
    if first is None or second is None:
        return (below.centre - above.centre - spacing
                + ((below.ascent + below.descent) - (above.ascent + above.descent)) / 2.0
                + _first_line_dy(above) - _first_line_dy(below))
    return (below.centre + second[0]) - (above.centre + first[0]) - spacing + second[1] - first[1]


def _baseline_metrics(line: LineLayout) -> tuple[float, float] | None:
    """`(offset, drop)` of one line, in px, from the faces its runs are drawn in — `None` when a run's
    face is not installed.

    * `offset`: how far below its line box's centre the browser drew the baseline. Edge lays text out
      on the font's ascent and descent **rounded to whole pixels** (DirectWrite's metrics: OS/2 win,
      typo under `USE_TYPO_METRICS`); the extractor's line box is centred on that content area, so
      the baseline sits `(A − D)/2` below the centre. Measured 2026-09-25 in the measuring browser:
      72 of 72 content areas (Arial, Arial Bold, Calibri, Calibri Bold, Segoe UI, Times New Roman at
      7.5–34 px) are exactly `round(ascent·size)` above the baseline and `round(descent·size)` below
      it (`test_the_browser_rounds_ascent_and_descent_to_whole_pixels`). The unrounded `(a − d)/2`
      is up to half a pixel off, and differently for every size.
    * `drop`: how far above the bottom of its exact-spaced line PptxRender sets the baseline — the
      descent plus DirectWrite's line gap (the part of hhea's line height the win metrics leave over;
      Arial 0.2446 em, Calibri 0.2686, Segoe UI 0.2510, Times New Roman 0.2588). PowerPoint agrees
      below 1.2 em (r1c's sweep; `_fit_lone_line` keeps one-line paragraphs there).

    The line's tallest runs set both, as in the browser (the union of its fragments) and in
    PptxRender (the largest run sets the line box).
    """
    ascent = descent = drop = 0.0
    for run in line.runs:
        face = face_for_exact(run.get("font"), int(run.get("weight") or 400), bool(run.get("italic")))
        if face is None:
            return None
        win_ascent, win_descent, line_gap = _directwrite_metrics(str(face.path), face.index)
        size = float(run.get("sizePx") or 0.0)
        ascent = max(ascent, math.floor(win_ascent * size + 0.5))       # Skia's round: half away up
        descent = max(descent, math.floor(win_descent * size + 0.5))
        drop = max(drop, (win_descent + line_gap) * size)
    return (ascent - descent) / 2.0, drop


@lru_cache(maxsize=256)
def _directwrite_metrics(path: str, index: int) -> tuple[float, float, float]:
    """`(ascent, descent, line gap)` in ems as DirectWrite reports a face on Windows — the metrics
    Edge lays a line out with and PptxRender places a baseline by.

    OS/2 `usWinAscent`/`usWinDescent` (`sTypoAscender`/`sTypoDescender` when `fsSelection` sets
    USE_TYPO_METRICS), and a line gap of whatever hhea's ascender + descender + lineGap adds to them
    (never negative). For Arial, Segoe UI and Times New Roman the win and hhea ascent and descent are
    the same numbers; Calibri's are 0.952 / 0.269 against hhea's 0.75 / 0.25, which is why its
    `TEXT_DY_BY_FONT` residual is the largest.
    """
    from fontTools.ttLib import TTCollection, TTFont

    try:
        if path.lower().endswith((".ttc", ".otc")):
            font = TTCollection(path, lazy=True).fonts[index]
        else:
            font = TTFont(path, lazy=True, fontNumber=0)
        upem = float(font["head"].unitsPerEm or 1000)
        hhea = font["hhea"]
        os2 = font["OS/2"]
        if int(os2.fsSelection) & 0x80:                                  # USE_TYPO_METRICS
            ascent, descent = float(os2.sTypoAscender) / upem, abs(float(os2.sTypoDescender)) / upem
            gap = float(os2.sTypoLineGap) / upem
        else:
            ascent, descent = float(os2.usWinAscent) / upem, float(os2.usWinDescent) / upem
            gap = max(0.0, (float(hhea.ascender) - float(hhea.descender) + float(hhea.lineGap)) / upem
                      - (ascent + descent))
    except Exception:                                                    # a broken or locked font file
        return 0.8, 0.2, 0.0
    return ascent, descent, gap


# -------------------------------------------------------------------------------------- writing


def _wraps(element: Element, layout: TextLayout, ctx: EmitContext) -> bool:
    """Whether PowerPoint may soft-wrap this frame. Under line locking it may not.

    Every browser line break is already an `a:br`, so a wrap PowerPoint adds can only be wrong: a
    wider metric on the target machine (a substituted family, a heavier or synthesised weight) then
    overhangs the frame by a few px, aligned the way the browser aligned the line, instead of
    breaking the line a second time and pushing the paragraph into the content below (fidelity F4:
    benchmark slide 51's "…versus balance-sheet banks" came out on three lines). One-line frames have
    worked this way since bad2f9b — a "20%" chip 0.2 in wide has room for its text at the browser's
    metrics and none at PowerPoint's, and wrapped it stacks one character per line; this is the
    same rule for every line count, placeholders included.

    Justified paragraphs included, and there it has a measured cost (Peter #7: accepted). PowerPoint
    stretches a justified `a:br` line only when the frame wraps, and then to the frame's inner width;
    under `wrap="none"` it leaves the line at its natural width. Measured 2026-09-25 with
    `wrap_evidence.py justify` on the torture `text` family's justified paragraph (browser measure
    x 40..660, frame 632 px): the browser ends line 1's ink at x 659; PowerPoint at 672 with
    `wrap="square"` (+13 px: it stretches to the 2 % box slack) and at 647 with `wrap="none"` (−12 px,
    ragged-right). PptxRender stretches either way (670). The two-line flip that would keep
    `wrap="square"` on justified paragraphs is deliberately not here: it would re-expose exactly the
    lines F4 breaks, since a justified line fills its measure by construction.
    """
    if not element.wrap:                       # white-space: nowrap | pre — the author's choice
        return False
    if not ctx.options.line_lock:
        # The A/B arm: PowerPoint lays the paragraph out itself — except a one-line frame, which a
        # narrow measure would stack one character per line (bad2f9b).
        return sum(len(paragraph.lines) for paragraph in layout.paragraphs) > 1
    return False


def write_layout(frame: Any, element: Element, layout: TextLayout, ctx: EmitContext) -> None:
    """Fill a text frame from a plan. Used for free text boxes and for layout placeholders alike."""
    frame.word_wrap = _wraps(element, layout, ctx)
    frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
    frame.vertical_anchor = _ANCHOR.get(element.anchor or "top", MSO_ANCHOR.TOP)
    _set_body(frame, element)

    body = frame._txBody
    for extra in list(body.findall(qn("a:p")))[1:]:
        body.remove(extra)
    frame.paragraphs[0].clear()          # a cloned placeholder arrives with the layout's own runs

    # `plan_text` leaves out a paragraph with no runs; `x-wpe-run-fills` keys are the IR's positions.
    planned = [i for i, p in enumerate(element.paragraphs or []) if any(ln.get("runs") for ln in p.get("lines") or [])]
    for index, paragraph in enumerate(layout.paragraphs):
        para = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        _write_paragraph(para, paragraph, element, layout, ctx, gap=paragraph_gap(layout.paragraphs, index),
                         at=planned[index] if index < len(planned) else None)


def _set_body(frame: Any, element: Element) -> None:
    """`bodyPr`: autofit off. A re-fit would undo every measurement this module just made."""
    body = frame._txBody.bodyPr
    remove_all(body, "normAutofit", "spAutoFit", "noAutofit")
    sub(body, "noAutofit")
    body.attrib.pop("vert", None)          # vertical text is a rotated box here, see `_transpose`


def _write_paragraph(
    para: Any,
    plan: ParagraphLayout,
    element: Element,
    layout: TextLayout,
    ctx: EmitContext,
    *,
    gap: float,
    at: int | None = None,
) -> None:
    """One paragraph. `at` is its index in the IR element, so a run knows its `x-wpe-run-fills` key."""
    source = plan.source
    pPr = para._p.get_or_add_pPr()
    align = plan.align or source.get("align")        # per paragraph: one element may mix them
    if align in _ALIGN:
        para.alignment = _ALIGN[align]

    bullet = source.get("bullet")
    _set_indent(pPr, plan)
    if ctx.options.line_lock:
        _set_spacing(pPr, plan.spacing_px, gap)
    _set_bullet(pPr, bullet, plan, element)

    previous: str | None = None
    kept = [i for i, line in enumerate(source.get("lines") or []) if line.get("runs")]   # as `plan_text`
    for index, line in enumerate(plan.lines):
        if index and ctx.options.line_lock:
            para._p.add_br()             # the browser's line break, locked in (never a re-wrap)
        for position, run in enumerate(line.runs):
            text = str(run.get("text") or "")
            if index and not ctx.options.line_lock and position == 0:
                # Unlocked, the paragraph must read as one flow: the browser ate the space it broke
                # at, so put it back rather than joining two words into one.
                if previous and not previous[-1].isspace() and text and not text[0].isspace():
                    run = {**run, "text": " " + text}
            _write_run(para, run, line, element, ctx,
                       at=None if at is None or index >= len(kept) else (at, kept[index], position))
            previous = text or previous


def _set_indent(pPr: Any, plan: ParagraphLayout) -> None:
    """`marL`/`marR`/`indent` from the measured placement (`_set_margins`), always all three — so a
    placeholder's list style cannot leak a margin in — and `lvl` for a bullet. For a bullet the
    margins are the hanging indent PowerPoint expects: text at `marL`, bullet at `marL + indent`."""
    pPr.set("marL", str(emu(plan.mar_left_px)))
    pPr.set("marR", str(emu(plan.mar_right_px)))
    pPr.set("indent", str(emu(plan.indent_px)))
    level = int((plan.source.get("bullet") or {}).get("level") or 0)
    if level:
        pPr.set("lvl", str(min(8, level)))


def _set_spacing(pPr: Any, spacing_px: float, gap: float) -> None:
    """Exact line spacing (`a:spcPts`) plus the measured gap before the paragraph.

    Measured, not adopted (fidelity r2, 2026-09-25): PowerPoint lays `a:spcPts` out at a whole number
    of points (13.65 pt at 14: up to two thirds of a pixel a line) and sets an exact line of 1.2 em or
    more up to 3 px higher than PptxRender; a multiple of single spacing (`a:spcPct`, 100 % = 1.2 × the
    run size for Arial, Calibri, Calibri Light, Segoe UI and Times New Roman) it lays out on
    PptxRender's pitch. On six-line paragraphs (`spacing_evidence.py sweep --powerpoint`) PowerPoint
    minus PptxRender was 1.31–1.42 px a line with points and 0.26–1.05 px with a multiple; on a client deck
    the lines of multi-line paragraphs above 1.2 em PowerPoint drew 1.5 px or more off the browser went
    from 40 to 4 with no gate number moving. It is not written because PptxRender puts a multiple
    above 100 % on a different first baseline (the extra leading split evenly about the line), so the
    frames of those paragraphs move by fractions of a pixel, and PptxRender, which scales a frame's
    text into its pixel-snapped rect where PowerPoint only snaps the top (`spacing_evidence.py snap`),
    flipped the torture `text` family's heading a pixel (`test_compare_reference_uses_the_masters_fonts`);
    `text_deck.deck_bands` and `fit._line_height_px` would also have to read a multiple as one of 1.2 em.
    """
    remove_all(pPr, "lnSpc", "spcBef", "spcAft")
    # pPr children are ordered: lnSpc, spcBef, spcAft, buClr…, so write them before the bullet.
    lnSpc = pPr.makeelement(qn("a:lnSpc"), {})
    pPr.insert(0, lnSpc)
    sub(lnSpc, "spcPts", val=max(100, int(round(spacing_px * config.PT_PER_PX * 100))))
    if gap > 0.05:
        spcBef = pPr.makeelement(qn("a:spcBef"), {})
        pPr.insert(1, spcBef)
        sub(spcBef, "spcPts", val=int(round(gap * config.PT_PER_PX * 100)))


def _set_bullet(pPr: Any, bullet: dict[str, Any] | None, plan: ParagraphLayout, element: Element) -> None:
    """A real bullet, or an explicit `buNone`.

    Explicit both ways on purpose: a body placeholder inherits the master's list style, so a
    paragraph that carries no bullet in the design would otherwise grow one in the export. The
    bullet's colour carries the paragraph's opacity (`bullet_alpha`).
    """
    remove_all(pPr, "buClr", "buSzPct", "buSzPts", "buFont", "buNone", "buChar", "buAutoNum")
    if not bullet:
        sub(pPr, "buNone")
        return
    colour = bullet.get("color")
    if colour:
        buClr = pPr.makeelement(qn("a:buClr"), {})                # CT_Color: the colour sits inside
        pPr.append(buClr)
        set_alpha(sub(buClr, "srgbClr", val=hexval(str(colour))), bullet_alpha(plan, element))
    first_run = plan.lines[0].runs[0] if plan.lines and plan.lines[0].runs else {}
    family = str(first_run.get("font") or "Arial")
    sub(pPr, "buFont", typeface=family)
    if bullet.get("type") == "num":
        sub(pPr, "buAutoNum", type="arabicPeriod", startAt=int(bullet.get("start") or 1))
    else:
        sub(pPr, "buChar", char=str(bullet.get("char") or "•"))


def bullet_alpha(plan: ParagraphLayout, element: Element) -> float:
    """How opaque a paragraph's bullet is: its element's `opacity` times its most opaque run's `alpha` (r1e).

    The browser paints a list marker in its item's colour under its item's opacity, and the IR's
    bullet has no alpha: the item's opacity is on the element (a positioned list) or on its runs
    (r1a's fold, an item in flow), and so is a transparent item colour. An inline box can only fade
    a run further, so the paragraph's most opaque run is the item's own paint. A list at opacity 0,
    an item at opacity 0 in flow and an item in `color: transparent` get an invisible bullet, a list
    at 0.5 a half-tone one, and `<li><span style="opacity:.5">Faded</span> word</li>` an opaque one.
    Measured before choosing (`fidelity-reports/r1e-evidence/bullets`): PowerPoint and PptxRender
    both draw `a:alpha` inside `a:buClr` — 50000 as a half-tone bullet, 0 as none — where `buNone`
    would move the first line to the bullet's column (`marL + indent`); so the bullet stays in the
    file at alpha 0, invisible and editable, as its text does (r1d). A bullet with no colour of its
    own follows its first run's colour and alpha in both renderers (measured too), so needs nothing.
    """
    opacity = 1.0 if element.opacity is None else float(element.opacity)
    alphas = [float(run.get("alpha", 1.0)) for line in plan.lines for run in line.runs]
    return opacity * (max(alphas) if alphas else 1.0)


def _write_run(para: Any, run: dict[str, Any], line: LineLayout, element: Element, ctx: EmitContext,
               at: tuple[int, int, int] | None = None) -> None:
    """One run. `at` = (paragraph, line, run) in the IR element, where a gradient-text fill is keyed."""
    text = str(run.get("text") or "")
    if element.transformCase == "lower":
        # PowerPoint has `cap="all"` and `cap="small"` but no lowercase: the only way to honour
        # `text-transform: lower` is to write the lowered text, so the export says what the eye saw.
        text = text.lower()
    r = para.add_run()
    r.text = text

    rPr = r._r.get_or_add_rPr()
    size_px = float(run.get("sizePx") or 0.0)
    if size_px > 0:
        rPr.set("sz", str(max(100, int(round(size_px * config.FONT_SZ_PER_PX)))))
    # Explicit both ways, like `_set_bullet`'s `buNone`: a regular run written into a title
    # placeholder whose layout style is bold (a client deck's is) must not inherit bold — and the same for
    # every other run property a master's title or list style can set: a style with `cap="all"`
    # turns a title the browser drew in mixed case into capitals (the render check's divider
    # titles, d0_s05/d0_s08), and `u`, `strike` and `spc` are the same trap. So all are written.
    face = emitted_face(run, ctx)
    rPr.set("b", "1" if face.bold else "0")
    rPr.set("i", "1" if face.italic else "0")
    rPr.set("u", "sng" if run.get("underline") else "none")
    rPr.set("strike", "sngStrike" if run.get("strike") else "noStrike")
    rPr.set("cap", "all" if run.get("caps") or element.transformCase == "upper" else "none")

    tracking = float(run.get("letterSpacingPx") or 0.0) + (line.tracking_px if ctx.options.width_lock else 0.0)
    rPr.set("spc", str(int(round(tracking * config.PT_PER_PX * 100))) if abs(tracking) > 0.005 else "0")

    baseline = _baseline_percent(run, size_px)
    if baseline:
        rPr.set("baseline", str(baseline))

    # An opacity of 0 is 0 (r1d): `or 1.0` read it as 1 and wrote an opacity-0 element's text opaque.
    # Invisible text stays in the file at alpha 0, as an opacity-0 box keeps its fill (`_apply_fill`).
    opacity = 1.0 if element.opacity is None else float(element.opacity)
    alpha = float(run.get("alpha", 1.0)) * opacity
    gradient = run_fill(element, at)
    if gradient is not None:
        # Gradient text: the run's own `a:gradFill`, in the fill's place (before `latin`/`cs`). The
        # run's `color` is the gradient's middle, kept for consumers that read one colour.
        gradient_fill(rPr, gradient, opacity)
        ctx.gap("gradient text fill: PptxRender draws the first stop (renderer gap, the file is correct)")
    else:
        solid_fill(rPr, hexval(run.get("color"), "000000"), alpha)

    if face.typeface:
        sub(rPr, "latin", typeface=face.typeface)
        sub(rPr, "cs", typeface=face.typeface)

    href = run.get("href")
    if href:
        r.hyperlink.address = str(href)


def run_fill(element: Element, at: tuple[int, int, int] | None) -> dict[str, Any] | None:
    """The gradient a run is filled with (`extras["x-wpe-run-fills"]["p/l/r"]`), or None for a solid run."""
    if at is None:
        return None
    fill = ((element.extras or {}).get("x-wpe-run-fills") or {}).get(f"{at[0]}/{at[1]}/{at[2]}")
    if isinstance(fill, dict) and fill.get("type") == "gradient" and len(fill.get("stops") or []) >= 2:
        return fill
    return None


#: `rPr@baseline` for `<sup>`/`<sub>`, in 1000ths of a percent. `verify.coverage.text_collisions`
#: exempts exactly this value, so a measured `vertical-align` shift is still checked.
SCRIPT_BASELINE: int = 30000


def _baseline_percent(run: dict[str, Any], size_px: float) -> int:
    """`baseline` in 1000ths of a percent: an explicit shift when measured, ±30 % for sup/sub."""
    shift = float(run.get("baselineShiftPx") or 0.0)
    if abs(shift) > 0.01 and size_px > 0:
        return int(round(shift / size_px * 100000))
    kind = run.get("baseline")
    if kind == "super":
        return SCRIPT_BASELINE
    if kind == "sub":
        return -SCRIPT_BASELINE
    return 0


# ------------------------------------------------------------------------------------- entry point


def _transpose(element: Element) -> Element:
    """A vertical text element as the equivalent horizontal one (x ↔ y on every box).

    Vertical text is emitted as a horizontal text box turned a quarter turn, rather than as
    `bodyPr vert="vert270"`. Both read bottom-to-top, but a rotated box keeps every line-spacing
    and baseline number this module already measures and calibrates, while `vert270` swaps the
    meaning of the frame's width and height and of its anchor, and would need a second calibration
    of its own for a construct the authoring contract does not even allow (`writing-mode` is
    disallowed; vertical text arrives as an SVG or CSS rotation).
    """
    def flip(box: dict[str, Any]) -> dict[str, Any]:
        return {"x": box.get("y", 0.0), "y": box.get("x", 0.0),
                "w": box.get("h", 0.0), "h": box.get("w", 0.0)}

    paragraphs = []
    for paragraph in element.paragraphs or []:
        lines = [{**line, "box": flip(line.get("box") or {})} for line in paragraph.get("lines") or []]
        paragraphs.append({**paragraph, "lines": lines})
    return element.replace(
        box=Box(element.box.y, element.box.x, element.box.h, element.box.w),
        paragraphs=paragraphs,
        writingMode="horizontal",
    )


def add_text(container: Any, element: Element, ctx: EmitContext, shape: Any | None = None) -> Any:
    """Emit one IR text element — into `shape` when it maps to a placeholder, else a new text box."""
    vertical = (element.writingMode or "horizontal") == "vertical"
    layout = plan_text(_transpose(element) if vertical else element, ctx)
    if not layout.paragraphs:
        ctx.warn(element.id, "text element has no measurable lines — nothing emitted")
        return None
    if vertical:
        # Rotation is about the centre, and rotation preserves the centre: place the un-rotated
        # box centred where the text really is, then turn it.
        box = layout.box
        layout.box = Box(box.cy - box.w / 2.0, box.cx - box.h / 2.0, box.w, box.h)

    if shape is None:
        shape = container.add_textbox(
            emu_length(layout.box.x), emu_length(layout.box.y),
            emu_length(layout.box.w), emu_length(layout.box.h),
        )
    else:
        shape.left, shape.top = emu_length(layout.box.x), emu_length(layout.box.y)
        shape.width, shape.height = emu_length(layout.box.w), emu_length(layout.box.h)

    write_layout(shape.text_frame, element, layout, ctx)
    rotation = float(element.rotation or 0.0) + (270.0 if vertical else 0.0)
    if rotation:
        shape.rotation = rotation % 360.0
    for warning in layout.warnings:
        ctx.warn(element.id, warning)
    for family in layout.substituted:
        ctx.warn(element.id, f"font {family!r} is not installed — measured with a fallback")
    return shape


@dataclass(slots=True)
class CellLayout:
    """A table cell's plan: its paragraphs, where each one's lines go, and what went wrong.

    Per paragraph, `indents_px` is the first-line `indent` and `margins_px` the `marL` the extractor
    measured (`x-wpa` `indentPx` / `marginPx`, px from the cell's content box); a margin is `None`
    for an IR that carries none (a hand-built fixture), which keeps the old reading: a bullet's own
    hanging indent, else a positive first-line indent at `marL = 0`.
    """

    paragraphs: list[ParagraphLayout]
    indents_px: list[float]
    margins_px: list[float | None] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def plan_cell(cell: dict[str, Any], extras: dict[str, Any], ctx: EmitContext) -> CellLayout:
    """The same per-line plan a text box gets (metrics, predicted width, width-lock tracking).

    `extras` is the cell's `x-wpa` entry; its `indentPx` and `marginPx` are indexed by the IR
    paragraph, so a paragraph `_plan_paragraphs` skipped for having no lines does not shift the others.
    """
    paragraphs_in = list(cell.get("paragraphs") or [])
    planned, warnings, substituted = _plan_paragraphs(paragraphs_in, ctx, Element(kind="text", box=Box(0, 0, 0, 0)))
    if ctx.options.width_lock:
        for paragraph in planned:
            for line in paragraph.lines:
                line.tracking_px = _tracking_for(line, warnings)
    indents = list((extras or {}).get("indentPx") or [])
    margins = list((extras or {}).get("marginPx") or [])
    position = {id(paragraph): index for index, paragraph in enumerate(paragraphs_in)}
    indents_px: list[float] = []
    margins_px: list[float | None] = []
    for paragraph in planned:
        index = position.get(id(paragraph.source), -1)
        indents_px.append(float(indents[index]) if 0 <= index < len(indents) else 0.0)
        margins_px.append(float(margins[index]) if 0 <= index < len(margins) else None)
    warnings.extend(f"font {family!r} is not installed — measured with a fallback"
                    for family in sorted(set(substituted)))
    return CellLayout(paragraphs=planned, indents_px=indents_px, margins_px=margins_px, warnings=warnings)


def _cell_line_left_px(layout: CellLayout, index: int, number: int) -> float:
    """How far in from the text frame's left inset PowerPoint starts line `number` of cell paragraph
    `index` (px): a bulleted paragraph's lines at `marL` (the bullet hangs in the indent), any other
    first line at `marL + indent`, later lines at `marL`."""
    bullet = layout.paragraphs[index].source.get("bullet")
    indent = layout.indents_px[index]
    margin = layout.margins_px[index] if index < len(layout.margins_px) else None
    if margin is None:
        return float(bullet.get("indentPx") or 0.0) if bullet else (indent if number == 0 and indent > 0.5 else 0.0)
    return margin + (indent if number == 0 and not bullet else 0.0)


def _set_cell_indent(pPr: Any, bullet: dict[str, Any] | None, margin: float | None, indent_px: float) -> None:
    """A cell paragraph's `marL` and first-line `indent`, written over what `_set_indent` wrote for
    it: a cell cannot move its frame the way `_place_box` moves a text box, so where the browser put
    the lines — after an icon, in a grid column, under a `margin-left`, beside a bullet — is carried
    by the paragraph's own margin. No `marR` beyond `_set_indent`'s 0: PptxRender does not read it
    (the extractor writes a line that would need one left-aligned at its measured start instead).

    `_set_indent` (package B's) writes the margins `_set_margins` measured for a text box, which a
    cell plan never has (all 0), so a bullet without a measured margin — an IR that carries none —
    gets its own hanging indent here: text at `indentPx`, the bullet at the cell's left inset.
    """
    if margin is None:
        if bullet:
            hanging = float(bullet.get("indentPx") or 0.0)
            pPr.set("marL", str(emu(hanging)))
            pPr.set("indent", str(-emu(hanging)))
            return
        pPr.set("marL", "0")
        pPr.set("indent", str(emu(indent_px)) if indent_px > 0.5 else "0")
        return
    pPr.set("marL", str(emu(max(0.0, margin))))
    pPr.set("indent", str(emu(indent_px)) if abs(indent_px) > 0.5 else "0")


def cell_margin_shift_px(
    layout: CellLayout,
    *,
    slot_top_px: float | None = None,
    slot_height_px: float = 0.0,
    top_px: float = 0.0,
    bottom_px: float = 0.0,
    anchor: str = "top",
) -> float:
    """Δ: how far a cell's text must move up so its first baseline lands on the browser's.

    A text box is placed so its first baseline lands where the browser drew it (`_place_box`); a
    cell's frame cannot move, so the same model moves the top inset instead. With `L` the first
    paragraph's exact spacing and `N` its first line's ascent + descent:

        browser baseline    = top + L/2 + (ascent − descent)/2       (half-leading centres the glyphs)
        PowerPoint baseline = marT + L − descent + dy                (exact spacing puts the leading above)
        ⇒ marT = top − Δ,   Δ = (L − N)/2 + dy

    `dy` is the measured residual `_first_line_dy` gives a text box's first line, taken the same way.

    Given the cell's grid slot (`slot_top_px`, `slot_height_px`), its insets and its anchor, the
    browser's first line is taken where it was *measured* rather than at `top + L/2`: a line that an
    inline-block chip or an emoji's fallback font made taller sits lower than its CSS line-height
    says (2 px on p02's country cells), and a middle-anchored cell's block is placed by the browser's
    own centring. The difference between PowerPoint's first-line centre under the same anchor and
    the measured centre is added to Δ, so every anchor lands the baseline.
    """
    if not layout.paragraphs or not layout.paragraphs[0].lines:
        return 0.0
    first = layout.paragraphs[0].lines[0]
    spacing = layout.paragraphs[0].spacing_px
    delta = (spacing - (first.ascent + first.descent)) / 2.0 + _first_line_dy(first)
    # A first line outside its own slot is no browser measurement (a hand-built IR): the model stands.
    if slot_top_px is None or not slot_top_px <= first.centre <= slot_top_px + slot_height_px:
        return delta
    block = _total_height(layout.paragraphs)
    inner = slot_height_px - top_px - bottom_px
    if anchor == "middle":
        block_top = slot_top_px + top_px + (inner - block) / 2.0
    elif anchor == "bottom":
        block_top = slot_top_px + slot_height_px - bottom_px - block
    else:
        block_top = slot_top_px + top_px
    return delta + (block_top + spacing / 2.0 - first.centre)


def cell_margins_px(
    layout: CellLayout | None,
    *,
    slot_top_px: float,
    slot_height_px: float,
    top_px: float,
    bottom_px: float,
    anchor: str = "top",
) -> tuple[float, float, float]:
    """`(marT, marB, residual)` for a cell whose browser insets are `top_px`/`bottom_px`.

    The text must move up by Δ (`cell_margin_shift_px`). PowerPoint places a top-anchored block by
    `marT` alone, a bottom-anchored one by `marB` alone and a centred one by `marT − marB`, inside a
    row whose declared height is a minimum: `marT + block + marB` must not exceed the slot, or the
    row grows. So the wanted `top − Δ` / `bottom + Δ` are kept while both are ≥ 0; when one is not,
    the side (or the difference) that places the block keeps its value as long as the row has the
    room, and otherwise gives way — the signed residual says how far (positive: the text sits low).
    """
    delta = 0.0 if layout is None else cell_margin_shift_px(
        layout, slot_top_px=slot_top_px, slot_height_px=slot_height_px, top_px=top_px, bottom_px=bottom_px,
        anchor=anchor)
    want_top, want_bottom = top_px - delta, bottom_px + delta
    if want_top >= 0.0 and want_bottom >= 0.0:
        return want_top, want_bottom, 0.0
    room = max(0.0, slot_height_px - (_total_height(layout.paragraphs) if layout is not None else 0.0))
    if anchor == "top":
        margin_top = min(max(0.0, want_top), room)
        return margin_top, max(0.0, min(top_px + bottom_px - margin_top, room - margin_top)), margin_top - want_top
    if anchor == "bottom":
        margin_bottom = min(max(0.0, want_bottom), room)
        return max(0.0, min(top_px + bottom_px - margin_bottom, room - margin_bottom)), margin_bottom,             want_bottom - margin_bottom
    difference = max(-room, min(room, want_top - want_bottom))
    spare = max(0.0, (min(top_px + bottom_px, room) - abs(difference)) / 2.0)
    margin_top, margin_bottom = max(0.0, difference) + spare, max(0.0, -difference) + spare
    return margin_top, margin_bottom, (difference - (want_top - want_bottom)) / 2.0


#: The least room a cell line leaves before the frame's right inset (px). A text box is made
#: `config.TEXT_BOX_WIDTH_SLACK` wider than its widest line; a cell cannot be, and the browser fills
#: a column to its last fraction: a client slide's "a column label" measured 103.609 px
#: in a 103.609 px content box and wrapped in PptxRender, where a frame 1 px wider kept it on one line.
CELL_LINE_SLACK_PX = 1.0


def _leave_cell_slack(layout: CellLayout, inner_width_px: float) -> None:
    """The width lock for a cell's lines, against the width PowerPoint has rather than the browser's.

    `_tracking_for` pulls a line back to the width the browser measured; a text box then has
    `TEXT_BOX_WIDTH_SLACK` of room, a cell has none, so a line the browser set flush with its column
    is tracked in until it leaves `max(CELL_LINE_SLACK_PX, the text-box allowance)` before the right
    inset — the same allowance a text box gets, spent as tracking because the frame cannot grow —
    within the width lock's usual floor.
    """
    for index, paragraph in enumerate(layout.paragraphs):
        for number, line in enumerate(paragraph.lines):
            available = inner_width_px - _cell_line_left_px(layout, index, number)
            limit = available - max(CELL_LINE_SLACK_PX, available * (1.0 - 1.0 / config.TEXT_BOX_WIDTH_SLACK))
            # The renderers set a line about as wide as the browser did (their ink matched it to the
            # pixel on that slide), or as Pillow predicts when that is wider.
            characters, width = _chars(line), max(line.predicted, line.width)
            if characters <= 0 or width + line.tracking_px * characters <= limit:
                continue
            size = max((float(run.get("sizePx") or 0.0) for run in line.runs), default=12.0)
            line.tracking_px = max(-_MAX_TRACKING_RATIO * size, (limit - width) / characters)


#: A one-line cell whose exact line spacing is more than this many times its glyphs' own height is
#: written at the glyphs' height (`fit_cell_spacing`). Measured 2026-09-25 (tables-rich, PowerPoint
#: against the browser, rows 2 and 3 of the `flow` table): PowerPoint sets an exact line's baseline
#: about 0.8 of the line below its top, the model (and PptxRender) at the line's height less the
#: descent; the two part by about 0.2·L − descent — 2 to 3 px for a 21 px line of 10 px Arial, under
#: 0.5 px for the 14 px lines of 11 px text — and agree where L is the glyphs' height. Beyond 1.5× the
#: gap passes 1 px.
CELL_TALL_LINE_RATIO = 1.5


def fit_cell_spacing(
    layout: CellLayout,
    *,
    slot_top_px: float,
    slot_height_px: float,
    top_px: float,
    bottom_px: float,
    anchor: str = "top",
) -> float:
    """A one-line cell with a tall line-height is written at its glyphs' own height.

    `height:38px; line-height:38px; padding:0` is how a design centres a label or a heat-map value
    in a cell. The browser splits the leading above and below the glyphs; PowerPoint puts an exact
    line's leading above them, so the glyphs land at the bottom of the cell unless the text moves up
    by Δ ≈ 14.5 px — past a zero inset (an app slide's heat map sat 14 px low, 2026-09-25) — and
    PowerPoint and PptxRender do not even agree where a line that tall puts its baseline
    (`CELL_TALL_LINE_RATIO`). For a cell of one paragraph of one line whose spacing leaves a residual
    or exceeds that ratio, the spacing becomes the glyphs' height (ascent + descent), where the two
    renderers agree, provided `cell_margins_px` then places it with no residual: the baseline lands
    where the browser drew it, and the shorter block leaves the row its height. Anything else keeps
    the design's spacing (a cell of more lines keeps its pitch, and `add_table` warns of a residual).
    Returns the spacing now in `layout` (px).
    """
    if len(layout.paragraphs) != 1 or len(layout.paragraphs[0].lines) != 1:
        return layout.paragraphs[0].spacing_px if layout.paragraphs else 0.0
    paragraph, line = layout.paragraphs[0], layout.paragraphs[0].lines[0]
    design, natural = paragraph.spacing_px, line.ascent + line.descent

    def residual(spacing: float) -> float:
        paragraph.spacing_px = spacing
        return abs(cell_margins_px(layout, slot_top_px=slot_top_px, slot_height_px=slot_height_px,
                                   top_px=top_px, bottom_px=bottom_px, anchor=anchor)[2])

    tall = design > CELL_TALL_LINE_RATIO * natural
    if natural < design and (tall or residual(design) > 0.5) and residual(natural) <= 0.5:
        paragraph.spacing_px = natural
        return natural
    paragraph.spacing_px = design
    return design


def write_cell(
    frame: Any,
    cell: dict[str, Any],
    element: Element,
    ctx: EmitContext,
    *,
    inner_width_px: float,
    extras: dict[str, Any],
    placement: dict[str, Any] | None = None,
) -> CellLayout:
    """Table-cell text, written the way a text box's is (`_write_paragraph`): the browser's lines
    locked with `a:br`, exact line spacing, the measured gap before each paragraph, bullets,
    width-lock tracking, and the table's opacity on every run — then each paragraph's measured
    `marL` and first-line `indent` (`_set_cell_indent`). `placement` is the cell's grid slot and
    insets as `fit_cell_spacing` takes them; given, a one-line cell's spacing is fitted first.

    PowerPoint ignores `bodyPr wrap="none"` inside a table cell (probe (d), 2026-09-25: a one-line
    cell with wrap off came out wrapped exactly like the control), so wrapping stays on and the
    width lock is what keeps each locked line on one line. A line predicted wider than the width
    its margins leave even after tracking is reported: PowerPoint will wrap it and the row will grow.
    """
    layout = plan_cell(cell, extras, ctx)
    if placement is not None:
        fit_cell_spacing(layout, **placement)
    if ctx.options.width_lock:
        _leave_cell_slack(layout, inner_width_px)
    body = frame._txBody
    for extra in list(body.findall(qn("a:p")))[1:]:
        body.remove(extra)
    frame.paragraphs[0].clear()

    text_layout = TextLayout(box=element.box, paragraphs=layout.paragraphs,
                             align=str((layout.paragraphs[0].source.get("align") if layout.paragraphs else None)
                                       or "left"))
    for index, paragraph in enumerate(layout.paragraphs):
        para = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        _write_paragraph(para, paragraph, element, text_layout, ctx, gap=paragraph_gap(layout.paragraphs, index),
                         at=None)   # cells never carry run fills
        _set_cell_indent(para._p.get_or_add_pPr(), paragraph.source.get("bullet"), layout.margins_px[index],
                         layout.indents_px[index])

    where = f"cell {cell.get('r')},{cell.get('c')}"
    for index, paragraph in enumerate(layout.paragraphs):
        for number, line in enumerate(paragraph.lines):
            available = inner_width_px - _cell_line_left_px(layout, index, number)
            width = line.predicted + (line.tracking_px * _chars(line) if ctx.options.width_lock else 0.0)
            if width > available + 0.5:
                text = "".join(str(run.get("text") or "") for run in line.runs)
                layout.warnings.append(
                    f"{where}: {text[:24]!r} predicted {width:.1f} px in an inner width of {available:.1f} px "
                    f"— PowerPoint will wrap it"
                )
    return layout
