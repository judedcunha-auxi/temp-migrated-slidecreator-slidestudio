"""Deterministic design review of one slide — what a vision judge misses, measured, not guessed.

The contract linter (`app/engine/verify/lint.py`, run by `app.core.engine_service.readiness`) answers "can the
export rebuild this faithfully?". This module answers "is it well made?": near-miss alignment,
uneven gutters, margin intrusion, type-scale sprawl, off-palette colour, competing accents, title
length, a missing source line and banking number formats (`docs/DESIGN-QUALITY-RESEARCH.md` §3, §4.2),
plus the tells that make a slide look AI-generated: side-stripe cards, rounded cards with drop shadows,
emoji, gradient text and an italic accent word in the title; and dead space (`empty-band`: a third of
the body left blank). Pills, numbered chips and check bullets are deliberately not flagged: they carry
meaning, and a dense composed slide needs them.

    design_findings_html(html_path, manifest_block, layout_id, *, assets_dir, layouts_dir,
                         workzone=None, run=None) -> [{"level", "rule", "message", "element"}]

Every message is written for the designing model: WHAT is wrong, WHERE (the element by its visible
text or `data-name`, canvas px), and the concrete FIX ("move it 3 px left, x 67 → 64"). `level` is
`error` only for clear defects — text off the canvas, clipped, spilling out of its card, drawn over
other text, or set below 7.5 pt (7.5–8 pt is a warn); everything else is `warn`. The list is capped (`MAX_FINDINGS`, errors
first, then the warn rules round-robin so one noisy rule cannot crowd out the others) and a final
`more-findings` entry counts what was cut.

How it measures
---------------
With the engine's own extractor, so the geometry is the export's: `extract_html` (the browser walk,
theme fonts forced, assets routed from disk, every remote URL refused — no network, no renderer),
then `recognise_charts` and `map_placeholders` as the pipeline runs them, then the engine's measured
`text_overlaps`. Rule logic (`review`) is pure Python over that IR and the master's `Grid`/palette.

The browser work runs on whichever thread `run` hands it to (the preview pool's, in the service:
`app.core.preview.run_on_preview_thread`). Playwright's sync API is bound to the thread that started
it, and that thread already owns a browser (`app.core.browser_pool.thread_browser`), so the review
reuses it with one page of its own per thread, kept warm in a thread-local. Derived images the
extractor writes go to a throwaway workspace, so a review never touches an export's captures.
With a brand workzone, the measured IR also goes through `workzone_lint.workzone_findings`: content
outside the workzone, or over the header band, is an error the designer must fix.
Warm cost is ~0.6-1 s per slide (the walk); the first call starts the browser (~2-3 s).

What is deliberately *not* reviewed: the layout background (master furniture — only used to find
where the footer band starts), the speaker-notes aside (the walk skips it), the inside of charts
(a `data-chart` is one element; a recognised SVG chart's overlay and every SVG-expanded element
stay out of the geometry rules), full-bleed elements and what sits inside them (bands the author
ran to the canvas edge on purpose), and border-side shapes (they coincide with their card).
"""
from __future__ import annotations

import colorsys
import logging
import math
import re
import statistics
import tempfile
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from app.core.brand.workzone import Workzone


# --------------------------------------------------------------------------------------- tunables
#: Findings returned at most (plus one `more-findings` entry counting the rest).
log = logging.getLogger(__name__)

MAX_FINDINGS = 10
#: Edges this close are aligned (sub-pixel borders, half-stroke insets).
ALIGN_EQUAL_PX = 1.0
#: Edges further apart than this are deliberately different, not a near miss.
ALIGN_NEAR_PX = 6.0
#: Text against a box: an offset this large is usually the box's padding, on purpose.
ALIGN_MIXED_PX = 2.5
#: Gap spread in a row/column of similar boxes that counts as uneven.
GAP_SPREAD_PX = 4.0
#: … and relative to the gap: 10 px on a 370 px gap is not visible.
GAP_SPREAD_REL = 0.08
#: Two boxes are "similar size" when width and height are each within this ratio.
SIMILAR_SIZE = 0.20
#: Tolerance before an edge counts as outside a margin / below the content bottom.
MARGIN_TOL_PX = 2.0
#: Breathing space kept above footer furniture found on the layout background.
FOOTER_CLEARANCE_PX = 8.0
#: Text spilling this far past its card's edge is a defect.
SPILL_PX = 2.0
#: Distinct font sizes (pt, to 0.5 pt) a slide may use.
MAX_FONT_SIZES = 5          # counted after rounding each size to the nearest 1 pt
#: Smallest legible size, pt (px × 0.75 on a 96 px/in canvas).
MIN_FONT_PT = 8.0
#: Below this it is an error; between it and MIN_FONT_PT a warn (10.5 px ≈ 7.9 pt is a warn).
MIN_FONT_PT_ERROR = 7.5
#: CIE76 ΔE within which a colour *is* a theme colour / a tint or shade of one / a RAG colour.
DE_THEME = 6.0
DE_TINT = 5.0
DE_RAG = 10.0
#: Lab chroma at or below which a colour is a (warm or cool) grey.
NEUTRAL_CHROMA = 9.0
#: Colours below this alpha are not judged.
MIN_ALPHA = 0.1
#: An emphasis hue: Lab chroma ≥ this and lightness in (dark, light) — very dark navies read as ink.
ACCENT_CHROMA = 30.0
ACCENT_L = (30.0, 92.0)
#: Hues closer than this (Lab hue angle, degrees) are one hue family.
HUE_FAMILY_DEG = 30.0
#: Title rules.
TITLE_MAX_LINES = 2
TOPIC_MAX_WORDS = 4
#: A one-sided border wider than this is a side stripe (the house style draws lines 1-2 px).
SIDE_STRIPE_PX = 2.0
#: … and a side stripe shorter than this is a tag's tick, not a card's.
SIDE_STRIPE_MIN_LEN_PX = 12.0
#: Corner radius above which a box counts as rounded (the house style allows ≤ 2 px).
ROUNDED_PX = 2.0
#: An empty band across (or down) the body at least this share of it is dead space — a third of the
#: body left blank reads as unfinished. Measured between the title zone and the content bottom, margin
#: to margin; the gap before the first and after the last element counts.
SPARSE_BAND = 0.30
#: … and never for a band thinner than this (a small body on an odd layout).
SPARSE_MIN_PX = 120.0
#: A body with fewer laid-out elements than this is a deliberate single statement, not a sparse slide.
SPARSE_MIN_ITEMS = 3
#: Empty space between two stacked units (one above the other, sharing most of their width) wider than
#: this is a hole in the layout, not a gutter: the house gutter is 12-24 px, a group break ~32 px.
STACK_GAP_PX = 36.0
#: Units narrower or shorter than this (rules, separators) and text at or below this size (source lines,
#: footnotes) are not units for the stack-gap rule; nor are shapes smaller than a 60×24 card (swatches, pills).
UNIT_MIN_PX = 4.0
SMALL_TEXT_PX = 12.5
#: A panel is hollow when the space under its content exceeds the space above it by this much (px) and
#: by this share of the panel's height: text in the top half, blank below.
HOLLOW_MIN_PX = 16.0
HOLLOW_SHARE = 0.15
#: A slide with at least this many figures (numbers in body text and table cells) and no visual encoding of
#: them states its evidence instead of showing it (`exhibit-plain`).
PLAIN_MIN_FIGURES = 4
#: A chart with more than this many points, one colour and no highlight or annotation has no focus.
FOCUS_MIN_POINTS = 3

#: Standard red / amber / green status colours — allowed off-theme.
RAG_COLOURS = ("C00000", "FF0000", "E03C31", "FFC000", "FFBF00", "F5B700", "00B050", "00A651", "92D050")

#: Warn rules in priority order (errors always come first). Also the order `more-findings` names them.
RULE_ORDER = (
    "text-off-canvas", "text-clipped", "text-spill", "text-overlap", "text-too-small",
    "text-overflow", "margin", "alignment", "gaps", "title-lines", "title-zone", "title-topic",
    "side-stripe", "rounded-shadow", "emoji", "gradient-text", "title-italic", "empty-band", "stack-gap", "hollow-panel", "exhibit-plain", "chart-no-focus",
    "font-sizes", "palette", "accents", "source", "chart-zero-label", "table-align",
    "table-negatives", "table-decimals",
)


class DesignLintError(RuntimeError):
    """The review could not run (unknown slide, engine or browser unavailable) — the message says why."""


class DesignLintUnavailable(DesignLintError):
    """The measuring browser (Playwright + Edge/Chromium) could not be started on this machine."""


# ------------------------------------------------------------------------------------ context


@dataclass
class Grid:
    """Where content may go, from the master: side margins, the content bottom, the title zone."""

    w: float = 1280.0
    h: float = 720.0
    left: float | None = None            # x of the left margin line
    right: float | None = None           # x of the right margin line (canvas x, not a width)
    bottom: float | None = None          # content must end above this y
    footer_top: float | None = None      # where the footer band / furniture starts
    footer_from: str = ""                # what the footer band was read from (for messages)
    title_zone: tuple[float, float, float, float] | None = None
    top_title: bool = True               # the layout's title sits at the top (not a cover/section)


@dataclass
class Context:
    grid: Grid
    palette: dict[str, str] = field(default_factory=dict)   # hex (no #, upper) -> theme slot
    primary: str | None = None                               # notes.colour_roles primary, if any
    raw_placeholders: dict[str, str] = field(default_factory=dict)  # element id -> authored type


# ------------------------------------------------------------------------------------ colours


def _hex6(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", value.strip())
    return m.group(1).upper() if m else None


def _rgb(hex6: str) -> tuple[int, int, int]:
    return int(hex6[0:2], 16), int(hex6[2:4], 16), int(hex6[4:6], 16)


def _lab_of_rgb(rgb: Iterable[float]) -> tuple[float, float, float]:
    def lin(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(float(c)) for c in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


_LAB_CACHE: dict[str, tuple[float, float, float]] = {}


def _lab(hex6: str) -> tuple[float, float, float]:
    lab = _LAB_CACHE.get(hex6)
    if lab is None:
        lab = _LAB_CACHE[hex6] = _lab_of_rgb(_rgb(hex6))
    return lab


def delta_e(a: str, b: str) -> float:
    """CIE76 ΔE between two 6-hex colours."""
    return math.dist(_lab(a), _lab(b))


def chroma(hex6: str) -> float:
    _, a, b = _lab(hex6)
    return math.hypot(a, b)


def hue_deg(hex6: str) -> float:
    _, a, b = _lab(hex6)
    return math.degrees(math.atan2(b, a)) % 360


def _hue_gap(a: float, b: float) -> float:
    gap = abs(a - b) % 360
    return min(gap, 360 - gap)


def _tint_distance(colour: str, theme: str) -> float:
    """ΔE from `colour` to the nearest tint (towards white) or shade (towards black) of `theme`."""
    tr, tg, tb = _rgb(theme)
    target = _lab(colour)
    best = math.inf
    for i in range(1, 20):
        t = i / 20
        for end in (255.0, 0.0):
            mixed = (tr + (end - tr) * t, tg + (end - tg) * t, tb + (end - tb) * t)
            best = min(best, math.dist(target, _lab_of_rgb(mixed)))
    return best


def colour_name(hex6: str) -> str:
    """A plain hue word for a message ("blue", "orange"); greys by lightness."""
    r, g, b = (c / 255 for c in _rgb(hex6))
    h, light, sat = colorsys.rgb_to_hls(r, g, b)
    if sat < 0.12 or chroma(hex6) <= NEUTRAL_CHROMA:
        return "white" if light > 0.95 else "black" if light < 0.08 else "grey"
    deg = h * 360
    for limit, name in ((15, "red"), (40, "orange"), (65, "yellow"), (160, "green"), (185, "teal"),
                        (205, "cyan"), (250, "blue"), (290, "purple"), (335, "magenta"), (360, "red")):
        if deg < limit:
            if name == "blue" and light < 0.3:
                return "navy"
            return name
    return "red"


# ------------------------------------------------------------------------------------ geometry


@dataclass
class B:
    """An axis-aligned box in canvas px."""

    x: float
    y: float
    w: float
    h: float

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    def contains(self, other: B, tol: float = 1.0) -> bool:
        return (other.x >= self.x - tol and other.y >= self.y - tol
                and other.x2 <= self.x2 + tol and other.y2 <= self.y2 + tol)

    def overlap_area(self, other: B) -> float:
        w = min(self.x2, other.x2) - max(self.x, other.x)
        h = min(self.y2, other.y2) - max(self.y, other.y)
        return w * h if w > 0 and h > 0 else 0.0

    def union(self, other: B) -> B:
        x, y = min(self.x, other.x), min(self.y, other.y)
        return B(x, y, max(self.x2, other.x2) - x, max(self.y2, other.y2) - y)

    def __str__(self) -> str:
        return f"x {self.x:.0f}–{self.x2:.0f}, y {self.y:.0f}–{self.y2:.0f}"


def _b(box: Any) -> B:
    if isinstance(box, dict):
        return B(float(box["x"]), float(box["y"]), float(box["w"]), float(box["h"]))
    return B(float(box.x), float(box.y), float(box.w), float(box.h))


# ------------------------------------------------------------------------------------ elements


_AUTO_NAME = re.compile(r"^[a-z][a-z0-9-]*#\d+(\s.*)?$")
_TABLE_PART = re.compile(r"\sr\d+c\d+(\s|$)")   # WP-A names what it draws for a table cell "<table> r2c3 chip"
_BORDER_SIDE = re.compile(r"\s(top|right|bottom|left) border$")


def _border_side(name: str | None) -> str:
    """Which side a border-side shape draws (`"<card> left border"` -> "left"); "" when it names none."""
    match = _BORDER_SIDE.search(name or "")
    return match.group(1) if match else ""


@dataclass
class Item:
    """One visible element with what the rules need precomputed."""

    el: Any
    kind: str
    box: B                 # the element's own box (shapes: outer edge incl. half the stroke)
    ink: B                 # text: union of line boxes; others: box
    text: str = ""
    label: str = ""
    element: str | None = None   # what the finding's `element` field carries
    svg: bool = False
    internal: bool = False  # SVG-expanded, a chart's overlay, or drawn by a table (x-wpa): not laid out by hand
    border_side: bool = False
    card: bool = False     # a painted rect/roundRect that can contain other elements
    bleed: bool = False    # touches a canvas edge (a band the author ran off the slide)
    container: Item | None = None
    align: str | None = None     # text: left/center/right when every paragraph agrees
    size_px: float = 0.0          # text: first run size
    em_top: float = 0.0           # text: top of the first line's em box (centred in its line box)


def _runs(paragraphs: Iterable[dict[str, Any]] | None) -> Iterable[dict[str, Any]]:
    for paragraph in paragraphs or []:
        for line in paragraph.get("lines") or []:
            yield from line.get("runs") or []


def _para_text(paragraph: dict[str, Any]) -> str:
    return " ".join("".join(run.get("text") or "" for run in line.get("runs") or [])
                    for line in paragraph.get("lines") or []).strip()


def _text(paragraphs: Iterable[dict[str, Any]] | None) -> str:
    return re.sub(r"\s+", " ", " ".join(_para_text(p) for p in paragraphs or [])).strip()


def _snip(text: str, n: int = 40) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    return (cut if len(cut) >= n // 2 else text[:n]) + "…"


def _visible_paint(fill: Any) -> bool:
    if not isinstance(fill, dict):
        return False
    if fill.get("type") == "solid":
        return float(fill.get("alpha", 1)) >= 0.05
    if fill.get("type") == "gradient":
        return any(float(s.get("alpha", 1)) >= 0.05 for s in fill.get("stops") or [])
    return False


def _stroke_visible(stroke: Any) -> bool:
    return isinstance(stroke, dict) and float(stroke.get("alpha", 1)) >= 0.05 and float(stroke.get("width") or 0) > 0


def _items(ir: Any, w: float, h: float) -> list[Item]:
    overlay: set[str] = set()
    for element in ir.elements:
        if element.kind == "chart":
            overlay.update(element.overlay or [])
    items: list[Item] = []
    for element in ir.elements:
        if float(element.opacity or 0) < 0.05 or element.rotation:
            continue
        box = _b(element.box)
        svg = bool((element.source or {}).get("svg")) or element.id in overlay
        item = Item(el=element, kind=element.kind, box=box, ink=box, svg=svg,
                    internal=svg or "x-wpa" in (element.extras or {}))
        name = (element.name or "").strip()
        named = name if name and not _AUTO_NAME.match(name) and not _TABLE_PART.search(name) else None
        if element.kind == "shape":
            if not (_visible_paint(element.fill) or _stroke_visible(element.stroke)):
                continue
            if _stroke_visible(element.stroke):
                half = float(element.stroke.get("width") or 0) / 2
                item.box = item.ink = B(box.x - half, box.y - half, box.w + 2 * half, box.h + 2 * half)
            geo = (element.geometry or {}).get("type")
            item.border_side = bool(_BORDER_SIDE.search(name))
            item.card = geo in ("rect", "roundRect") and not item.border_side and box.w >= 12 and box.h >= 8
            item.label = f"“{named}”" if named else ""
        elif element.kind == "text":
            paragraphs = element.paragraphs or []
            if not any(float(r.get("alpha", 1)) >= 0.05 and (r.get("text") or "").strip() for r in _runs(paragraphs)):
                continue
            lines = [_b(line["box"]) for p in paragraphs for line in p.get("lines") or [] if line.get("box")]
            if lines:
                ink = lines[0]
                for line in lines[1:]:
                    ink = ink.union(line)
                item.ink = ink
            item.text = _text(paragraphs)
            aligns = {p.get("align") for p in paragraphs}
            item.align = aligns.pop() if len(aligns) == 1 else None
            first = next(iter(_runs(paragraphs)), {})
            item.size_px = float(first.get("sizePx") or 0)
            first_line = lines[0] if lines else item.ink
            line_size = max((float(r.get("sizePx") or 0) for r in (paragraphs[0].get("lines") or [{}])[0].get("runs") or []),
                            default=item.size_px)
            item.em_top = first_line.cy - line_size / 2
            item.label = f"“{_snip(item.text)}”"
            item.element = named or _snip(item.text)
        elif element.kind == "chart":
            kind = (element.spec or {}).get("type") or "chart"
            item.label = f"chart “{named}”" if named else f"the {kind} chart"
            item.element = named or f"{kind} chart"
        elif element.kind == "table":
            opening = next((_text(c.get("paragraphs")) for c in element.cells or [] if _text(c.get("paragraphs"))), "")
            item.label = f"table “{named}”" if named else f"the table starting “{_snip(opening, 24)}”"
            item.element = named or f"table: {_snip(opening, 24)}"
        elif element.kind in ("image", "raster"):
            stem = Path(element.src or "").stem
            item.label = f"image “{named or stem}”"
            item.element = named or stem or "image"
        else:
            continue
        item.bleed = (item.box.x <= 1 or item.box.y <= 1 or item.box.x2 >= w - 1 or item.box.y2 >= h - 1)
        items.append(item)

    # Containers: the smallest painted card that holds the element (text by its ink).
    cards = [i for i in items if i.card and not i.internal and i.box.area < 0.9 * w * h]
    for item in items:
        best = None
        probe = item.ink if item.kind == "text" else item.box
        for card in cards:
            if card is item or card.box.area <= item.box.area * 1.02 or card.box.area <= probe.area * 1.02:
                continue
            if card.box.contains(probe, 1.0) and (best is None or card.box.area < best.box.area):
                best = card
        item.container = best
    # Name the unnamed shapes by the text they hold (“card holding ‘Program Investment’”).
    for item in items:
        if item.kind == "shape" and item.border_side and item.container is not None and not item.label:
            side = _border_side(item.el.name)
            owner = item.container
            held = sorted((t for t in items if t.kind == "text" and t.container is owner), key=lambda t: (t.ink.y, t.ink.x))
            item.label = (f"the {side} border of the card holding “{_snip(held[0].text, 30)}”" if held
                          else f"the {side} border of the box at ({owner.box.x:.0f}, {owner.box.y:.0f})")
            item.element = (f"{side} border: {_snip(held[0].text, 30)}" if held else item.label)
        if item.kind == "shape" and not item.label:
            inside = [t for t in items if t.kind == "text" and t.container is item]
            inside.sort(key=lambda t: (t.ink.y, t.ink.x))
            what = "card" if item.card and item.box.w >= 60 and item.box.h >= 30 else (
                "line" if min(item.box.w, item.box.h) <= 4 else "shape")
            if inside:
                item.label = f"the {what} holding “{_snip(inside[0].text, 30)}”"
                item.element = f"{what}: {_snip(inside[0].text, 30)}"
            else:
                colour = _hex6(((item.el.fill or {}).get("color")) or "") or _hex6((item.el.stroke or {}).get("color") or "")
                item.label = f"the {colour_name(colour) + ' ' if colour else ''}{what} at ({item.box.x:.0f}, {item.box.y:.0f})"
                item.element = f"{what} at ({item.box.x:.0f}, {item.box.y:.0f})"
        if item.element is None:
            item.element = (item.label or "").strip("“”") or None
    return items


def _in_bleed(item: Item) -> bool:
    c = item.container
    while c is not None:
        if c.bleed:
            return True
        c = c.container
    return False


# ------------------------------------------------------------------------------------ findings


def _f(level: str, rule: str, message: str, element: str | None) -> dict[str, Any]:
    return {"level": level, "rule": rule, "message": message, "element": element}


def _px(v: float) -> str:
    return f"{v:.0f}"


def _sz(v: float) -> str:
    """A font size in px for a message: 9.86667 → "9.9"."""
    return f"{round(float(v), 1):g}"


# -- rule 4: text defects -------------------------------------------------------------------------


def _text_defects(ir: Any, items: list[Item], grid: Grid) -> tuple[list[dict[str, Any]], set[int]]:
    out: list[dict[str, Any]] = []
    flagged: set[int] = set()
    by_id = {i.el.id: i for i in items}
    for item in items:
        if item.kind != "text":
            continue
        clipped = (item.el.extras or {}).get("x-wpf-clipped")
        if clipped:
            clip = _b(item.el.clip) if item.el.clip is not None else None
            hidden = int(clipped.get("hiddenLines") or 0)
            chars = int(clipped.get("hiddenChars") or 0)
            what = (f"{hidden} line(s) / {chars} character(s) are hidden" if chars
                    else "part of a line is cut off")
            if clipped.get("canvas"):
                out.append(_f("error", "text-off-canvas",
                              f"{item.label} runs off the slide ({what}); its box is {item.box} on a "
                              f"{_px(grid.w)}×{_px(grid.h)} canvas. Move it inside the canvas"
                              + (f" (right edge ≤ {_px(grid.right)})" if grid.right else "") + " or shorten it.",
                              item.element))
            else:
                out.append(_f("error", "text-clipped",
                              f"{item.label} is clipped by an overflow:hidden container"
                              + (f" ({clip})" if clip else "") + f": {what}. Make the container larger, "
                              f"shorten the text or reduce its size — never rely on clipping.", item.element))
            flagged.add(id(item))
            continue
        # Glyphs that start inside a painted card and run out of it (right or bottom).
        card = _holding_card(item, items, grid.w * grid.h)
        if card is not None:
            over_r = item.ink.x2 - card.box.x2
            over_b = item.ink.y2 - card.box.y2
            if over_r > SPILL_PX or over_b > SPILL_PX:
                parts = []
                if over_r > SPILL_PX:
                    parts.append(f"{_px(over_r)} px past its right edge (text ends at x {_px(item.ink.x2)}, "
                                 f"the card at x {_px(card.box.x2)})")
                if over_b > SPILL_PX:
                    parts.append(f"{_px(over_b)} px below its bottom (text ends at y {_px(item.ink.y2)}, "
                                 f"the card at y {_px(card.box.y2)})")
                fix = ("reduce the font size, shorten the text, move it left or widen the card"
                       if over_r > SPILL_PX else "reduce the text, its size or line-height, or make the card taller")
                out.append(_f("error", "text-spill",
                              f"{item.label} spills out of {card.label}: {' and '.join(parts)}. Fix: {fix}.",
                              item.element))
                flagged.add(id(item))
    # Whole blocks the walk dropped (no element): off the canvas, or hidden by a clip.
    for d in ir.diagnostics:
        if d.elementId is not None or d.level != "error":
            continue
        message = d.message or ""
        if "is outside the canvas" in message:
            out.append(_f("error", "text-off-canvas",
                          f"A text block is entirely off the slide and is not exported: {message}. Move it onto "
                          f"the canvas (0–{_px(grid.w)} × 0–{_px(grid.h)}).", _quote_in(message)))
        elif "clipped by an overflow:hidden ancestor" in message and "whole block is hidden" in message:
            out.append(_f("error", "text-clipped",
                          f"A text block is completely hidden by an overflow:hidden container: {message}. "
                          f"Give it room or remove it.", _quote_in(message)))
    # The engine's measured overflow (below its declared box) — kept once, as a warn.
    for d in ir.diagnostics:
        if d.source != "text-overflow":
            continue
        measured = by_id.get(d.elementId)
        if measured is not None and id(measured) in flagged:
            continue
        out.append(_f("warn", "text-overflow",
                      f"{d.message}. Give the box the height its text needs, or cut the text"
                      + (f" ({item.label} at {item.ink})" if item else "") + ".",
                      item.element if item else None))
        if item is not None:
            flagged.add(id(item))
    return out, flagged


def _quote_in(message: str) -> str | None:
    m = re.search(r"“([^”]+)”", message)
    return _snip(m.group(1)) if m else None


def _holding_card(item: Item, items: list[Item], canvas_area: float) -> Item | None:
    """The smallest painted card whose box holds the text's top-left glyph corner with room to spare."""
    best = None
    for card in items:
        if not card.card or card.internal or card is item or card.box.area >= 0.9 * canvas_area:
            continue
        b = card.box
        if not (b.x + 2 <= item.ink.x <= b.x2 - 2 and b.y + 1 <= item.ink.y <= b.y2 - 2):
            continue
        if b.area <= item.ink.area:
            continue
        # A thin bar or chip the text merely starts on is not a card it lives in.
        if b.h < item.ink.h * 0.9 and b.w < item.ink.w:
            continue
        if best is None or b.area < best.box.area:
            best = card
    return best


def _overlaps(ir: Any, items: list[Item]) -> list[dict[str, Any]]:
    try:
        from app.engine.verify.text_layout import text_overlaps
    except Exception:  # noqa: BLE001
        log.debug("text overlap check unavailable", exc_info=True)
        return []
    by_id = {i.el.id: i for i in items}
    out = []
    for d in text_overlaps(ir):
        first = by_id.get(d.elementId)
        where = f" at {first.ink}" if first else ""
        out.append(_f("error", "text-overlap",
                      f"{d.message}{where}: the two texts are drawn over each other. Move one of them clear "
                      f"(leave ≥ 4 px between their lines), or shorten it.", first.element if first else None))
    return out


# -- rule 3: margins ------------------------------------------------------------------------------


def _margins(items: list[Item], grid: Grid, skip: set[int]) -> list[dict[str, Any]]:
    """Content outside the side margins or below the content bottom — one finding per side and offset.

    A slide built on its own (wrong) margin puts a dozen elements at the same x; that is one mistake,
    reported once with the elements it moves, not a dozen findings.
    """
    if grid.left is None and grid.bottom is None:
        return []
    intruding: dict[int, set[str]] = defaultdict(set)
    candidates = [i for i in items if not i.internal and not i.border_side and not i.bleed and not _in_bleed(i)
                  and i.box.area < 0.9 * grid.w * grid.h]
    # Parents first, so a child of an intruding card is not reported again.
    candidates.sort(key=lambda i: -i.box.area)
    hits: dict[tuple[str, int], list[tuple[Item, float]]] = defaultdict(list)
    for item in candidates:
        if id(item) in skip:
            continue
        box = item.ink if item.kind == "text" else item.box
        if box.x2 < 0 or box.x > grid.w or box.y2 < 0 or box.y > grid.h:
            continue
        sides: dict[str, float] = {}
        if grid.left is not None and box.x < grid.left - MARGIN_TOL_PX:
            sides["left"] = box.x
        if grid.right is not None and box.x2 > grid.right + MARGIN_TOL_PX:
            sides["right"] = box.x2
        if grid.bottom is not None and box.y2 > grid.bottom + MARGIN_TOL_PX and box.y < grid.h:
            sides["bottom"] = box.y2
        parent, inherited = item.container, set()
        while parent is not None:
            inherited |= intruding.get(id(parent), set())
            parent = parent.container
        intruding[id(item)] = set(sides)
        for side, edge in sides.items():
            if side not in inherited:
                hits[(side, 0)].append((item, edge))
    out: list[dict[str, Any]] = []
    order = {"left": 0, "right": 1, "bottom": 2}
    # A side is only ever hit when its margin is known (above), so these stand in for the known values.
    margin_left = grid.left if grid.left is not None else 0.0
    margin_right = grid.right if grid.right is not None else grid.w
    content_bottom = grid.bottom if grid.bottom is not None else grid.h
    for (side, _), group in sorted(hits.items(), key=lambda kv: (order[kv[0][0]], -len(kv[1]))):
        group.sort(key=lambda ie: -ie[0].box.area)
        item, edge = group[0]
        names = ", ".join(i.label for i, _ in group[:3]) + (f" and {len(group) - 3} more" if len(group) > 3 else "")
        many = len(group) > 1
        if side == "left":
            edge = min(e for _, e in group)
            d = margin_left - edge
            msg = (f"{names} start{'' if many else 's'} {'from' if many else 'at'} x {_px(edge)}, {_px(d)} px outside the "
                   f"{_px(margin_left)} px left margin. Move {'them' if many else 'it'} {_px(d)} px right "
                   f"(x {_px(edge)} → {_px(margin_left)}), or run {'them' if many else 'it'} to x 0 if full-bleed is meant.")
        elif side == "right":
            edge = max(e for _, e in group)
            d = edge - margin_right
            msg = (f"{names} end{'' if many else 's'} {'by' if many else 'at'} x {_px(edge)}, {_px(d)} px past the right margin at x "
                   f"{_px(margin_right)}. Narrow {'them' if many else 'it'} or move {'them' if many else 'it'} "
                   f"{_px(d)} px left so nothing ends after x {_px(margin_right)}.")
        else:
            lowest = max(e for _, e in group)
            d = lowest - content_bottom
            band = (f"the footer band (from y {_px(grid.footer_top)}, {grid.footer_from})"
                    if grid.footer_top is not None else f"the bottom margin (content ends by y {_px(content_bottom)})")
            msg = (f"{names} reach{'' if many else 'es'} down to y {_px(lowest)}, {_px(d)} px into {band}. Keep "
                   f"content above y {_px(content_bottom)}: move {'them' if many else 'it'} up or shorten "
                   f"{'them' if many else 'it'} (the lowest by {_px(d)} px).")
        out.append(_f("warn", "margin", msg, item.element))
    return out


# -- rules 1 and 2: alignment and gaps ------------------------------------------------------------


def _layout_items(items: list[Item]) -> list[Item]:
    return [i for i in items if not i.internal and not i.border_side and not (i.bleed and i.kind == "shape")
            and i.box.w >= 1 and i.box.h >= 1]


def _edges(item: Item) -> dict[str, float]:
    """The edges of `item` that alignment is judged on."""
    if item.kind == "text":
        edges: dict[str, float] = {}
        if item.align in ("left", "justify"):
            edges["left"] = item.box.x
        if item.align == "right":
            edges["right"] = item.ink.x2
        # The em box of the first line, not its line box: two texts set with different line-heights
        # but one baseline have different line-box tops and the same glyph top.
        edges["top"] = item.em_top
        return edges
    b = item.box
    edges = {"left": b.x, "right": b.x2, "top": b.y}
    if min(b.w, b.h) <= 4:                    # a rule: its top says nothing about the row it sits in
        edges.pop("top")
    if b.h <= 24 and b.w >= 3 * b.h:          # a horizontal bar: its right end is data
        edges.pop("right", None)
    if b.w <= 24 and b.h >= 3 * b.w:          # a vertical bar: its top is data
        edges.pop("top", None)
    return edges


def _groups(items: list[Item]) -> dict[int, list[Item]]:
    groups: dict[int, list[Item]] = defaultdict(list)
    for item in items:
        groups[id(item.container) if item.container else 0].append(item)
    return groups


def _comparable(a: Item, b: Item, edge: str) -> bool:
    if a.box.overlap_area(b.box) > 1.0:          # layered, not side by side
        return False
    if edge == "top":
        # Line-box tops only compare between texts of one size; text tops never against a box top.
        if (a.kind == "text") != (b.kind == "text"):
            return False
        if a.kind == "text" and abs(a.size_px - b.size_px) > 0.1:
            return False
        # A row: the two sit side by side.
        return a.box.x2 <= b.box.x + 1 or b.box.x2 <= a.box.x + 1
    # left/right: a column — one above the other.
    return a.box.y2 <= b.box.y + 1 or b.box.y2 <= a.box.y + 1


def _alignment(items: list[Item], grid: Grid) -> list[dict[str, Any]]:
    misses: list[tuple[Any, ...]] = []
    for members in _groups(_layout_items(items)).values():
        if len(members) < 2:
            continue
        reported: set[int] = set()
        edged = [(m, _edges(m)) for m in members]
        for edge in ("left", "right", "top"):
            values = [(e[edge], m) for m, e in edged if edge in e]
            for value, item in values:
                if id(item) in reported:
                    continue
                peers = [(v, o) for v, o in values if o is not item and _comparable(item, o, edge)]
                near = [(v, o) for v, o in peers if ALIGN_EQUAL_PX < abs(v - value) <= (
                    ALIGN_NEAR_PX if (o.kind == "text") == (item.kind == "text") else ALIGN_MIXED_PX)]
                if not near:
                    continue
                keys: list[float] = []
                for v, _ in sorted(near, key=lambda vo: -vo[1].box.area):
                    if not any(abs(v - k) <= ALIGN_EQUAL_PX for k in keys):
                        keys.append(v)
                clusters = [(k, [o for v, o in peers if abs(v - k) <= ALIGN_EQUAL_PX]) for k in keys]
                own = 1 + sum(1 for v, _ in peers if abs(v - value) <= ALIGN_EQUAL_PX)  # itself included
                target, anchors = max(clusters, key=lambda kv: (len(kv[1]), _on_grid(kv[0], edge, grid),
                                                                max(a.box.area for a in kv[1])))
                if _on_grid(value, edge, grid) and not _on_grid(target, edge, grid):
                    continue
                if own > len(anchors):
                    continue
                if own == len(anchors) and not (_on_grid(target, edge, grid)
                                                or max(a.box.area for a in anchors) > item.box.area):
                    continue
                anchor = max(anchors, key=lambda a: a.box.area)
                misses.append((item, edge, value, target, anchor, len(anchors)))
                reported.add(id(item))
    # One systematic slip (every card's label 2 px off its list) is one finding, not one per card.
    grouped: dict[tuple[str, int], list[tuple[Any, ...]]] = defaultdict(list)
    for miss in misses:
        grouped[(miss[1], round(miss[3] - miss[2]))].append(miss)
    out: list[dict[str, Any]] = []
    for (edge, _), group in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        item, _, value, target, anchor, n = group[0]
        value, target = round(value), round(target)
        d = target - value
        axis = "x" if edge in ("left", "right") else "y"
        direction = ("right" if d > 0 else "left") if axis == "x" else ("down" if d > 0 else "up")
        if len(group) == 1:
            others = f" and {n - 1} other(s)" if n > 1 else ""
            out.append(_f("warn", "alignment",
                          f"{item.label} {edge} edge is at {axis} {value:.0f}, {abs(d):.0f} px off {anchor.label}"
                          f"{others} at {axis} {target:.0f} — a near miss. Move it {abs(d):.0f} px {direction} "
                          f"({axis} {value:.0f} → {target:.0f}) so the {edge} edges line up.", item.element))
            continue
        cases = "; ".join(f"{m[0].label} at {axis} {m[2]:.0f} vs {m[4].label} at {m[3]:.0f}" for m in group[:3])
        more = f"; and {len(group) - 3} more" if len(group) > 3 else ""
        out.append(_f("warn", "alignment",
                      f"{len(group)} elements miss their neighbours' {edge} edge by {abs(d):.0f} px: {cases}{more}. "
                      f"Move each {abs(d):.0f} px {direction} (usually one padding/offset value in the CSS) so the "
                      f"{edge} edges line up.", item.element))
    return out


def _on_grid(value: float, edge: str, grid: Grid) -> bool:
    if edge == "left" and grid.left is not None:
        return abs(value - grid.left) <= ALIGN_EQUAL_PX
    if edge == "right" and grid.right is not None:
        return abs(value - grid.right) <= ALIGN_EQUAL_PX
    return False


def _similar(a: B, b: B) -> bool:
    return (abs(a.w - b.w) <= SIMILAR_SIZE * max(a.w, b.w) and abs(a.h - b.h) <= SIMILAR_SIZE * max(a.h, b.h))


def _gaps(items: list[Item]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for members in _groups(_layout_items(items)).values():
        boxes = [m for m in members if m.kind != "text" and m.box.w >= 20 and m.box.h >= 20]
        if len(boxes) < 3:
            continue
        for axis in ("x", "y"):
            out.extend(_uneven(boxes, members, axis))
    return out


def _uneven(boxes: list[Item], members: list[Item], axis: str) -> list[dict[str, Any]]:
    """Rows (axis x) or columns (axis y) of ≥ 3 similar boxes whose gaps differ by > GAP_SPREAD_PX."""
    out: list[dict[str, Any]] = []
    def start(b: B) -> float:
        return float(b.x if axis == "x" else b.y)

    def end(b: B) -> float:
        return float(b.x2 if axis == "x" else b.y2)

    def cross(b: B) -> tuple[float, float]:
        return (b.y, b.cy) if axis == "x" else (b.x, b.cx)

    def linked(a: Item, b: Item) -> bool:
        (a0, ac), (b0, bc) = cross(a.box), cross(b.box)
        return (abs(a0 - b0) <= ALIGN_NEAR_PX or abs(ac - bc) <= ALIGN_NEAR_PX) and _similar(a.box, b.box)

    remaining = list(boxes)
    while remaining:
        seed = remaining.pop(0)
        row = [seed]
        for other in list(remaining):
            if any(linked(other, r) for r in row):
                row.append(other)
                remaining.remove(other)
        if len(row) < 3:
            continue
        row.sort(key=lambda i: start(i.box))
        # Split where two items overlap along the axis or something else sits in the gap between them.
        runs: list[list[Item]] = [[row[0]]]
        for prev, cur in zip(row, row[1:], strict=False):
            gap_lo, gap_hi = end(prev.box), start(cur.box)
            blocked = gap_hi < gap_lo - 0.5 or any(
                m not in row and m.kind != "text" and gap_lo + 1 < start(m.box) and end(m.box) < gap_hi - 1
                and _cross_overlap(m.box, prev.box, axis) for m in members)
            if blocked:
                runs.append([cur])
            else:
                runs[-1].append(cur)
        for run in runs:
            if len(run) < 3:
                continue
            gaps = [start(b.box) - end(a.box) for a, b in zip(run, run[1:], strict=False)]
            lo, hi = min(gaps), max(gaps)
            if hi - lo <= max(GAP_SPREAD_PX, GAP_SPREAD_REL * statistics.median(gaps)) or lo <= 0 or hi > 2 * lo:
                continue
            target = statistics.median(gaps)
            target = min(gaps, key=lambda g: (abs(g - target), g))  # an existing gap, so one move fixes most
            moves = []
            pos = end(run[0].box)
            for item in run[1:]:
                want = pos + target
                have = start(item.box)
                if abs(want - have) > 1:
                    moves.append(f"{item.label} {axis} {have:.0f} → {want:.0f}")
                pos = want + (end(item.box) - start(item.box))
            what = "row" if axis == "x" else "column"
            first = run[0]
            out.append(_f("warn", "gaps",
                          f"Uneven gaps in the {what} of {len(run)} similar boxes starting with {first.label}: "
                          f"{', '.join(f'{g:.0f}' for g in gaps)} px. Make every gap {target:.0f} px: "
                          + "; ".join(moves[:4]) + ("; …" if len(moves) > 4 else "") + ".",
                          first.element))
    return out


def _cross_overlap(a: B, b: B, axis: str) -> bool:
    if axis == "x":
        return min(a.y2, b.y2) - max(a.y, b.y) > 0
    return min(a.x2, b.x2) - max(a.x, b.x) > 0


# -- rule 5: type ---------------------------------------------------------------------------------


def _text_sources(ir: Any, items: list[Item]) -> Iterable[tuple[Item, dict[str, Any], Any]]:
    """(item, run, owner) for every visible run — text elements and table cells, not SVG internals."""
    for item in items:
        if item.kind == "text" and not item.svg:
            for run in _runs(item.el.paragraphs):
                if (run.get("text") or "").strip() and float(run.get("alpha", 1)) >= 0.05:
                    yield item, run, item
        elif item.kind == "table":
            for cell in item.el.cells or []:
                for run in _runs(cell.get("paragraphs")):
                    if (run.get("text") or "").strip():
                        yield item, run, cell


def _pt(px: float) -> float:
    """A size in whole pt — 10 px and 10.5 px (7.5 / 7.9 pt) are one size to a reader."""
    return float(round(px * 0.75))


def _type(ir: Any, items: list[Item]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    sizes: dict[float, dict[str, Any]] = {}
    small: list[tuple[float, str, Item]] = []
    seen_small: set[int] = set()
    for item, run, owner in _text_sources(ir, items):
        px = float(run.get("sizePx") or 0)
        if px <= 0:
            continue
        pt = _pt(px)
        entry = sizes.setdefault(pt, {"px": px, "chars": 0, "example": None})
        entry["chars"] += len((run.get("text") or "").strip())
        if entry["example"] is None:
            entry["example"] = _snip(run.get("text") or "", 24)
        if px * 0.75 < MIN_FONT_PT - 0.01 and id(owner) not in seen_small:
            seen_small.add(id(owner))
            label = item.label if item.kind == "text" else f"cells of {item.label}"
            small.append((px, label, item))
    # Authored charts: their label font is text too.
    for item in items:
        if item.kind == "chart" and not item.svg:
            spec = item.el.spec or {}
            size = (spec.get("options") or {}).get("fontSize")
            if isinstance(size, (int, float)) and 0 < size * 1.0 * 0.75 < MIN_FONT_PT and item.el.origin == "authored":
                # `fontSize` in the chart contract is px (the preview draws it at that size).
                small.append((float(size), f"the labels of {item.label}", item))
    for level, tiny in (("error", [x for x in small if x[0] * 0.75 < MIN_FONT_PT_ERROR - 0.01]),
                        ("warn", [x for x in small if x[0] * 0.75 >= MIN_FONT_PT_ERROR - 0.01])):
        out.extend(_small_text(level, tiny))
    return out + _size_count(sizes)


def _small_text(level: str, small: list[tuple[float, str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if small:
        small.sort(key=lambda s: s[0])
        grouped_small: dict[tuple[float, str], int] = Counter((px, label) for px, label, _ in small)
        entries = [f"{n} × {label}" if n > 1 else label for (px, label), n in grouped_small.items()]
        sizes_of = [px for (px, _label) in grouped_small]
        listed = "; ".join(f"{e} at {_sz(px)} px ({px * 0.75:.1f} pt)" for e, px in list(zip(entries, sizes_of, strict=False))[:4])
        more = f" and {len(small) - sum(list(grouped_small.values())[:4])} more" if len(grouped_small) > 4 else ""
        minimum = math.ceil(MIN_FONT_PT / 0.75 * 2) / 2
        out.append(_f(level, "text-too-small",
                      f"Text below {MIN_FONT_PT:g} pt is illegible when projected: {listed}{more}. "
                      f"Set it to at least {minimum:g} px ({MIN_FONT_PT:g} pt).", small[0][2].element))
    return out


def _size_count(sizes: dict[float, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if len(sizes) > MAX_FONT_SIZES:
        ordered = sorted(sizes.items())
        listing = ", ".join(f"{_sz(v['px'])}px (“{v['example']}”)" for _, v in ordered)
        merges = []
        cluster: list[tuple[float, dict[str, Any]]] = []
        for pt, entry in ordered + [(math.inf, {"px": math.inf, "chars": 0})]:
            if cluster and entry["px"] <= cluster[0][1]["px"] * 1.2:
                cluster.append((pt, entry))
                continue
            if len(cluster) > 1:
                keep = max(cluster, key=lambda c: c[1]["chars"])[1]["px"]
                merges.append("/".join(_sz(c[1]["px"]) for c in cluster) + f" → {_sz(keep)}px")
            cluster = [(pt, entry)]
        advice = ("merge " + "; ".join(merges)) if merges else "use one size per role"
        out.append(_f("warn", "font-sizes",
                      f"{len(sizes)} different font sizes on one slide: {listing}. Keep ≤ {MAX_FONT_SIZES} "
                      f"(title, body, label, footnote): {advice}.", None))
    return out


# -- rule 8: title --------------------------------------------------------------------------------


_TOPIC_EXEMPT = re.compile(r"^(agenda|contents?|table of contents|appendix|thank you|thanks|questions|q&a|"
                           r"disclaimer|executive summary|next steps|backup|glossary|notes?)\b", re.I)
_VERB_HINT = re.compile(
    r"\b(is|are|was|were|be|been|has|have|had|will|can|could|should|must|may|might|do|does|did|"
    r"drives?|drove|grows?|grew|rises?|rose|falls?|fell|leads?|led|lags?|beats?|needs?|makes?|made|"
    r"requires?|shows?|shown|delivers?|creates?|generates?|improves?|reduces?|increases?|declines?|"
    r"outperforms?|underperforms?|doubles?|triples?|expands?|shrinks?|wins?|loses?|lost|gains?|"
    r"accelerates?|slows?|remains?|stays?|explains?|defines?|carries?|sequences?|scores?|enables?|"
    r"unlocks?|depends?|offers?|yields?|returns?)\b|\w+(ed|ing)\b", re.I)


def _title_item(items: list[Item], ctx: Context) -> Item | None:
    """The slide's title: the marked one, else the heading in the title zone, else the top heading."""
    grid = ctx.grid
    texts = [i for i in items if i.kind == "text" and not i.svg]
    title = next((i for i in texts if ctx.raw_placeholders.get(i.el.id) in ("title", "ctrTitle")), None)
    if title is None and grid.title_zone:
        # No marked title: the heading-sized text that sits most inside the master's title zone.
        zone = B(*grid.title_zone)
        inside = [(i.ink.overlap_area(zone) / max(i.ink.area, 1.0), i) for i in texts if i.size_px >= 18]
        inside = [(share, i) for share, i in inside if share >= 0.5]
        title = max(inside, key=lambda si: (si[1].size_px, si[0]), default=(0, None))[1]
    if title is None:
        # Nor a zone: the topmost heading-sized text in the top quarter.
        top = [i for i in texts if i.ink.y < 0.25 * grid.h and i.size_px >= 20]
        title = min(top, key=lambda i: (i.ink.y, -i.size_px), default=None)
    return title


def _title(ir: Any, items: list[Item], ctx: Context) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    grid = ctx.grid
    title = _title_item(items, ctx)
    if title is None:
        return out
    lines = sum(len(p.get("lines") or []) for p in title.el.paragraphs or [])
    words = len(re.findall(r"[\w$€£%.,×'’-]+", title.text))
    if lines > TITLE_MAX_LINES:
        per_line = max(1, round(words / lines))
        out.append(_f("warn", "title-lines",
                      f"The title {title.label} wraps to {lines} lines at {_sz(title.size_px)} px ({words} words). "
                      f"Cut it to ≤ {TITLE_MAX_LINES} lines — about {per_line * TITLE_MAX_LINES} words at this "
                      f"size — keeping the so-what; move detail into a subtitle.", title.element))
    # A marked title that did not fit the master's zone (map_placeholders said so).
    for d in ir.diagnostics:
        if d.elementId == title.el.id and "data-placeholder=title ignored" in (d.message or ""):
            m = re.search(r"\((\d+) px vs (\d+) px\)", d.message)
            if m:
                zone = grid.title_zone
                where = f" (x {zone[0]:.0f}–{zone[0] + zone[2]:.0f})" if zone else ""
                out.append(_f("warn", "title-zone",
                              f"The title {title.label} is {m.group(1)} px wide but the master's title zone is "
                              f"{m.group(2)} px{where}, so it exports as a loose text box, not the title "
                              f"placeholder. Set its width ≤ {m.group(2)} px (let it wrap) or shorten it.",
                              title.element))
    if grid.top_title and words <= TOPIC_MAX_WORDS and not re.search(r"\d", title.text) \
            and not _VERB_HINT.search(title.text) and not _TOPIC_EXEMPT.match(title.text):
        out.append(_f("warn", "title-topic",
                      f"The title {title.label} reads as a topic label, not a message. Write an action title — "
                      f"a full sentence with the so-what, ideally with a number (e.g. “X grew 12% on Y”).",
                      title.element))
    return out


# -- rules 6 and 7: colour ------------------------------------------------------------------------


@dataclass
class Use:
    hex: str
    role: str        # "text", "fill", "border", "chart"
    item: Item
    weight: float


def _uses(items: list[Item]) -> list[Use]:
    uses: list[Use] = []

    def add(value: Any, alpha: Any, role: str, item: Item, weight: float) -> None:
        hex6 = _hex6(value)
        if hex6 and float(alpha if alpha is not None else 1) >= MIN_ALPHA:
            uses.append(Use(hex6, role, item, weight))

    def paint(fill: Any, role: str, item: Item, weight: float) -> None:
        if not isinstance(fill, dict):
            return
        if fill.get("type") == "solid":
            add(fill.get("color"), fill.get("alpha", 1), role, item, weight)
        elif fill.get("type") == "gradient":
            for stop in fill.get("stops") or []:
                add(stop.get("color"), stop.get("alpha", 1), role, item, weight / 2)

    for item in items:
        el = item.el
        if item.kind == "shape":
            role = "border" if item.border_side or min(item.box.w, item.box.h) <= 4 else "fill"
            paint(el.fill, role, item, item.box.area)
            if _stroke_visible(el.stroke):
                add(el.stroke.get("color"), el.stroke.get("alpha", 1), "border", item,
                    2 * (item.box.w + item.box.h) * float(el.stroke.get("width") or 1))
        elif item.kind == "text":
            for run in _runs(el.paragraphs):
                if (run.get("text") or "").strip():
                    add(run.get("color"), run.get("alpha", 1), "text", item,
                        len(run["text"].strip()) * float(run.get("sizePx") or 12))
        elif item.kind == "table":
            for cell in el.cells or []:
                paint(cell.get("fill"), "fill", item, 400)
                for side in (cell.get("borders") or {}).values():
                    if isinstance(side, dict) and float(side.get("width") or 0) > 0:
                        add(side.get("color"), 1, "border", item, 40)
                for run in _runs(cell.get("paragraphs")):
                    if (run.get("text") or "").strip():
                        add(run.get("color"), run.get("alpha", 1), "text", item, len(run["text"].strip()) * 12)
        elif item.kind == "chart" and el.origin == "authored":
            spec = el.spec or {}
            options = spec.get("options") or {}
            for value in list(spec.get("colors") or []) + list(options.get("pointColors") or []):
                add(value, 1, "chart", item, 2000)
    return uses


def _theme_match(hex6: str, palette: dict[str, str]) -> tuple[str | None, float]:
    best, dist = None, math.inf
    for theme in palette:
        d = delta_e(hex6, theme)
        if d < dist:
            best, dist = theme, d
    return best, dist


def _on_palette(hex6: str, palette: dict[str, str]) -> bool:
    if chroma(hex6) <= NEUTRAL_CHROMA:
        return True
    if any(delta_e(hex6, rag) <= DE_RAG for rag in RAG_COLOURS):
        return True
    for theme in palette:
        if delta_e(hex6, theme) <= DE_THEME:
            return True
        if chroma(theme) > NEUTRAL_CHROMA and _tint_distance(hex6, theme) <= DE_TINT:
            return True
    return False


def _describe_use(uses: list[Use]) -> str:
    roles = Counter(u.role for u in uses)
    first = max(uses, key=lambda u: u.weight)
    role_words = {"text": "text", "fill": "fill", "border": "line/border", "chart": "chart series"}
    kinds = "/".join(role_words[r] for r, _ in roles.most_common())
    return f"{kinds} ×{len(uses)}, e.g. {first.item.label}"


def _palette(items: list[Item], ctx: Context) -> list[dict[str, Any]]:
    if not ctx.palette:
        return []
    by_hex: dict[str, list[Use]] = defaultdict(list)
    for use in _uses(items):
        by_hex[use.hex].append(use)
    off = [(hex6, uses) for hex6, uses in by_hex.items() if not _on_palette(hex6, ctx.palette)]
    if not off:
        return []
    off.sort(key=lambda hu: -sum(u.weight for u in hu[1]))
    parts = []
    for hex6, uses in off[:5]:
        near, dist = _theme_match(hex6, ctx.palette)
        swap = f"use {ctx.palette[near]} #{near}" if near and dist <= 25 else "use a theme colour, a tint of one or a grey"
        parts.append(f"#{hex6} ({_describe_use(uses)}) → {swap}")
    more = f"; and {len(off) - 5} more" if len(off) > 5 else ""
    theme = ", ".join(f"{slot} #{hex6}" for hex6, slot in list(ctx.palette.items())[:8])
    return [_f("warn", "palette",
               f"{len(off)} colour(s) are not in the master's theme (nor a tint/shade of one, a grey or a "
               f"standard RAG colour): " + "; ".join(parts) + more + f". Theme: {theme}.",
               max(off[0][1], key=lambda u: u.weight).item.element)]


def _accents(items: list[Item], ctx: Context) -> list[dict[str, Any]]:
    families: list[dict[str, Any]] = []
    for use in _uses(items):
        if use.role == "chart":          # a multi-series chart's hues are data, not emphasis
            continue
        hex6 = use.hex
        light = _lab(hex6)[0]
        if chroma(hex6) < ACCENT_CHROMA or not (ACCENT_L[0] <= light <= ACCENT_L[1]):
            continue
        if any(delta_e(hex6, rag) <= DE_RAG for rag in RAG_COLOURS):
            continue
        hue = hue_deg(hex6)
        family = next((f for f in families if _hue_gap(f["hue"], hue) <= HUE_FAMILY_DEG), None)
        if family is None:
            family = {"hue": hue, "weight": 0.0, "colours": Counter(), "uses": 0, "items": []}
            families.append(family)
        family["weight"] += use.weight
        family["colours"][hex6] += use.weight
        family["uses"] += 1
        family["items"].append(use.item)
    if len(families) < 3:
        return []
    primary_hex = _hex6(ctx.primary or "")
    families.sort(key=lambda f: -f["weight"])
    primary = next((f for f in families if primary_hex and primary_hex in f["colours"]), families[0])
    accents = [f for f in families if f is not primary]
    if len(accents) < 2:
        return []

    def name(f: dict[str, Any]) -> str:
        hex6 = f["colours"].most_common(1)[0][0]
        return f"{colour_name(hex6)} #{hex6} ({f['uses']} use{'s' if f['uses'] != 1 else ''}, e.g. {f['items'][0].label})"

    keep = accents[0]
    return [_f("warn", "accents",
               f"{len(accents)} accent hues compete for emphasis besides the primary {name(primary)}: "
               + "; ".join(name(f) for f in accents)
               + f". Keep one accent for the takeaway (e.g. {colour_name(keep['colours'].most_common(1)[0][0])}) and turn "
                 f"the others into the primary colour or grey.", accents[1]["items"][0].element)]


# -- rule 9: sources ------------------------------------------------------------------------------


_NUMBER = re.compile(r"^\(?[-−–]?\s*[$€£¥]?\s*\d[\d,.\s]*\s*(%|x|×|bps|pp|[kKmMbB]n?|mm|bn)?\)?\*?$")
_SOURCE = re.compile(r"^\W{0,3}(\d{1,2}\W{0,2})?(sources?|data source|source data)\b", re.I)
_SOURCE_ANYWHERE = re.compile(r"\b(sources?|data)\s*[:：]", re.I)   # "Source: …", "Actual data: …"


def _cells(table: Any) -> list[dict[str, Any]]:
    return [c for c in table.cells or [] if isinstance(c, dict)]


def _numeric_cells(table: Any) -> list[dict[str, Any]]:
    return [c for c in _cells(table) if _NUMBER.match(_text(c.get("paragraphs")) or "x")]


def _source(items: list[Item], grid: Grid) -> list[dict[str, Any]]:
    charts = [i for i in items if i.kind == "chart"]
    tables = [i for i in items if i.kind == "table" and len(_numeric_cells(i.el)) >= 3]
    if not charts and not tables:
        return []
    texts: list[str] = []
    for item in items:
        if item.kind == "text":
            texts.extend(_para_text(p) for p in item.el.paragraphs or [])
        elif item.kind == "table":
            texts.extend(_text(c.get("paragraphs")) for c in _cells(item.el))
    if any(_SOURCE.match(t) or _SOURCE_ANYWHERE.search(t) for t in texts):
        return []
    exhibit = (charts or tables)[0]
    where = ""
    if grid.left is not None and grid.bottom is not None:
        where = f" at x {_px(grid.left)}, just above y {_px(grid.bottom)} (the bottom of the content area)"
    return [_f("warn", "source",
               f"{exhibit.label} shows data but the slide has no source line. Add one small line starting "
               f"“Source:” (e.g. “Source: company filings; team analysis”){where}, at ≥ 11 px.",
               exhibit.element)]


def _chart_zero_labels(items: list[Item]) -> list[dict[str, Any]]:
    out = []
    for item in items:
        if item.kind != "chart" or item.el.origin != "authored":
            continue
        spec = item.el.spec or {}
        kind = str(spec.get("type") or "")
        options = spec.get("options") or {}
        if not options.get("dataLabels") or not re.search(r"bar|column|waterfall", kind):
            continue
        cats = list(spec.get("categories") or [])
        for series in spec.get("series") or []:
            values = series.get("values") or []
            zeros = [cats[i] if i < len(cats) else f"#{i + 1}" for i, v in enumerate(values) if v == 0]
            if zeros:
                out.append(_f("warn", "chart-zero-label",
                              f"{item.label} labels a zero value for “{zeros[0]}”"
                              + (f" (and {len(zeros) - 1} more)" if len(zeros) > 1 else "")
                              + ": a zero bar has no height, so its data label sits on the axis and collides with "
                                "the category label. Drop that category, give it its real value, or turn off its label.",
                              item.element))
                break
    return out


# -- rule 10: banking numbers ---------------------------------------------------------------------


def _unsigned(text: str) -> str:
    """A figure without its leading minus sign. A helper rather than inline in the f-string that uses
    it: a backslash inside an f-string expression is Python 3.12 syntax (PEP 701), and the service runs
    on Python 3.11 (decision D2)."""
    return re.sub(r"^[-−–]\s*", "", text)


def _decimals(text: str) -> int | None:
    m = re.search(r"\d(?:[\d,]*)(?:\.(\d+))?", text)
    if not m:
        return None
    return len(m.group(1) or "")


def _score_column(table: Any, col: int, head_cell: dict[str, Any] | None) -> bool:
    """A score/rating/status column ("4/5", "High", "✓", "3.5"): every body cell ≤ 4 characters and a
    centred header. Centring those is a convention, not a slip; financial figures are longer."""
    if head_cell is None or (head_cell.get("paragraphs") or [{}])[0].get("align") != "center":
        return False
    body = [_text(c.get("paragraphs")) for c in _cells(table)
            if int(c.get("r", 0)) > 0 and int(c.get("c", 0)) == col]
    body = [t for t in body if t]
    return bool(body) and all(len(t) <= 4 for t in body)


def _tables(items: list[Item]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        if item.kind != "table":
            continue
        table = item.el
        numeric = [c for c in _numeric_cells(table) if int(c.get("r", 0)) > 0]
        if len(numeric) < 2:
            continue
        by_col: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for cell in numeric:
            by_col[int(cell.get("c", 0))].append(cell)
        # Right alignment.
        misaligned = []
        for col, cells in sorted(by_col.items()):
            if len(cells) < 2:
                continue
            aligns = Counter((c.get("paragraphs") or [{}])[0].get("align", "left") for c in cells)
            if aligns.get("right", 0) < len(cells):
                head_cell = next((c for c in _cells(table)
                                  if int(c.get("r", 0)) == 0 and int(c.get("c", 0)) == col), None)
                header = _text(head_cell.get("paragraphs")) if head_cell else ""
                if _score_column(table, col, head_cell):
                    continue
                misaligned.append(f"column {col + 1}" + (f" “{_snip(header, 20)}”" if header else "")
                                  + f" ({', '.join(f'{n} {a}' for a, n in aligns.items() if a != 'right')})")
        if misaligned:
            out.append(_f("warn", "table-align",
                          f"Numbers in {item.label} are not right-aligned: {'; '.join(misaligned[:4])}. Set "
                          f"text-align:right on those cells (and their headers) so the digits line up.",
                          item.element))
        # Negatives: parentheses vs minus sign in one table.
        texts = [_text(c.get("paragraphs")) for c in numeric]
        parens = [t for t in texts if re.match(r"^\(\s*[$€£]?\s*\d", t)]
        minus = [t for t in texts if re.match(r"^[-−–]\s*[$€£]?\s*\d", t)]
        if parens and minus:
            out.append(_f("warn", "table-negatives",
                          f"{item.label} writes negatives both ways: {', '.join(parens[:2])} and "
                          f"{', '.join(minus[:2])}. Use parentheses throughout, e.g. "
                          f"“{minus[0]}” → “({_unsigned(minus[0])})”.", item.element))
        # Decimals per column (same unit suffix).
        for col, cells in sorted(by_col.items()):
            groups: dict[str, list[tuple[str, int]]] = defaultdict(list)
            for cell in cells:
                t = _text(cell.get("paragraphs"))
                suffix = re.sub(r"[\d,.\s()$€£¥*−–-]", "", t).lower()
                d = _decimals(t)
                if d is not None:
                    groups[suffix].append((t, d))
            for values in groups.values():
                counts = Counter(d for _, d in values)
                if len(values) >= 3 and len(counts) > 1:
                    common = counts.most_common(1)[0][0]
                    odd = [t for t, d in values if d != common]
                    out.append(_f("warn", "table-decimals",
                                  f"Column {col + 1} of {item.label} mixes decimal places: "
                                  f"{', '.join(odd[:3])} vs {common} decimal(s) elsewhere. Show every value "
                                  f"with {common} decimal(s).", item.element))
                    break
    return out


# -- rule 11: the AI-generated look ---------------------------------------------------------------

#: Emoji: the pictographic planes, the emoji presentation selector and the BMP symbols models reach for
#: (✅ ✨ ⚡ ⭐ ❌ ❗ ❓ ⚠ ➡ …). The check and cross marks ✓ ✔ ✕ ✖ ✗ ✘ (U+2713-2718) are left to the
#: glyph lint: they are table conventions, not decoration.
_EMOJI = re.compile("[\U0001F000-\U0001FAFF️☀-✒✙-➿⬅-⬇⬛⬜"
                    "⭐⭕⌚⌛⏩-⏺]")


def _radius(el: Any) -> float:
    geometry = el.geometry or {}
    if geometry.get("type") != "roundRect":
        return 0.0
    radius = geometry.get("radius")
    if isinstance(radius, dict):
        return max((float(v or 0) for v in radius.values()), default=0.0)
    try:
        return float(radius or 0)
    except (TypeError, ValueError):
        return 0.0


def _stripe_owner(side: Item, which: str) -> Item | None:
    """The card a border side belongs to: its container, when the side runs along that card's edge."""
    card = side.container
    if card is None:
        return None
    b, c = side.box, card.box
    if which in ("left", "right"):
        edge = abs(b.x - c.x) if which == "left" else abs(b.x2 - c.x2)
        return card if edge <= 1.5 and abs(b.h - c.h) <= 2.5 else None
    edge = abs(b.y - c.y) if which == "top" else abs(b.y2 - c.y2)
    return card if edge <= 1.5 and abs(b.w - c.w) <= 2.5 else None


def _ai_look(items: list[Item], ctx: Context) -> list[dict[str, Any]]:
    """The patterns that mark a slide as machine-made (research: side stripes are the most cited)."""
    out: list[dict[str, Any]] = []
    # Side stripes: a border side wider than the house style's lines, on one side only (a uniform border
    # is one stroke, not a side shape). Left, right and top always; a bottom side only on a rounded box,
    # where a thick underline under a header is the house style but the notch is still an export defect.
    stripes: list[tuple[Item, str, float, bool]] = []
    for item in items:
        if item.kind != "shape" or not item.border_side or item.internal:
            continue
        which = _border_side(item.el.name)
        thick = item.box.w if which in ("left", "right") else item.box.h
        length = item.box.h if which in ("left", "right") else item.box.w
        colour = _hex6((item.el.fill or {}).get("color") or "")
        if thick <= SIDE_STRIPE_PX + 0.01 or length < SIDE_STRIPE_MIN_LEN_PX or (colour and _lab(colour)[0] > 97):
            continue
        owner = _stripe_owner(item, which)
        rounded = owner is not None and _radius(owner.el) > 0.5
        if which == "bottom" and not rounded:
            continue
        stripes.append((item, which, thick, rounded))
    if stripes:
        stripes.sort(key=lambda s: (not s[3], s[0].box.y, s[0].box.x))
        listed = "; ".join(f"{s[0].label} ({s[2]:.0f} px{', rounded box' if s[3] else ''})" for s in stripes[:3])
        more = f"; and {len(stripes) - 3} more" if len(stripes) > 3 else ""
        notch = sum(1 for s in stripes if s[3])
        out.append(_f("warn", "side-stripe",
                      f"{len(stripes)} box(es) carry a coloured border on one side: {listed}{more}. A card with a "
                      f"side stripe is the most recognisable AI-generated-slide pattern"
                      + (f", and on a rounded box the export draws the stripe square, so it pokes out past the "
                         f"corners ({notch} here)" if notch else "")
                      + ". Remove the one-sided border; to emphasise the box use a full 1px border, a solid fill, "
                        "bold text, or a separate square accent block beside it (its own element, a gap away). "
                        "If a stripe must stay, set border-radius:0 on its box.", stripes[0][0].element))
    # Rounded cards with a soft drop shadow: a web-UI card, not a slide.
    soft = [i for i in items if i.kind == "shape" and not i.internal and not i.border_side
            and _radius(i.el) > ROUNDED_PX and isinstance(i.el.shadow, dict)
            and float(i.el.shadow.get("alpha", 1)) >= 0.05
            and (float(i.el.shadow.get("blur") or 0) or float(i.el.shadow.get("dx") or 0) or float(i.el.shadow.get("dy") or 0))]
    if soft:
        listed = "; ".join(f"{i.label} ({_radius(i.el):.0f} px radius)" for i in soft[:3])
        more = f"; and {len(soft) - 3} more" if len(soft) > 3 else ""
        out.append(_f("warn", "rounded-shadow",
                      f"{len(soft)} rounded box(es) with a drop shadow: {listed}{more}. Rounded cards with soft "
                      f"shadows read as an AI template, not a consulting slide. Remove the box-shadow and set "
                      f"border-radius ≤ {ROUNDED_PX:g} px; separate boxes with whitespace, a fill or a 1px border.",
                      soft[0].element))
    # Emoji, anywhere text is drawn (SVG labels and table cells too).
    hits: list[tuple[str, Item]] = []
    for item in items:
        if item.kind == "text":
            texts = [item.text]
        elif item.kind == "table":
            texts = [_text(c.get("paragraphs")) for c in _cells(item.el)]
        else:
            continue
        for t in texts:
            if _EMOJI.search(t or ""):
                hits.append((t, item))
    if hits:
        shown = "; ".join(f"“{_snip(t, 30)}”" for t, _ in hits[:3]) + (f"; and {len(hits) - 3} more" if len(hits) > 3 else "")
        out.append(_f("warn", "emoji",
                      f"Emoji in the text: {shown}. Emoji read as AI-generated, and PowerPoint may draw them in "
                      f"another font or not at all. Delete them; where a marker carries meaning, draw a small "
                      f"monoline SVG icon or a numbered chip.", hits[0][1].element))
    # Gradient text (background-clip:text): the extractor lifts the glyphs' gradient into the element's extras.
    graded = [i for i in items if i.kind == "text" and (i.el.extras or {}).get("x-wpe-run-fills")]
    if graded:
        out.append(_f("warn", "gradient-text",
                      f"Gradient-filled text ({', '.join(i.label for i in graded[:3])}): a signature AI-design "
                      f"flourish. Set the text in one solid colour (the theme's text colour, or the accent for "
                      f"the one number that matters) and drop background-clip:text.", graded[0].element))
    # A roman title with an italic accent word or phrase.
    title = _title_item(items, ctx)
    if title is not None:
        runs = [r for r in _runs(title.el.paragraphs) if re.search(r"\w", r.get("text") or "")]
        italic = [r for r in runs if r.get("italic")]
        if italic and len(italic) < len(runs):
            words = " ".join((r.get("text") or "").strip() for r in italic)
            out.append(_f("warn", "title-italic",
                          f"The title {title.label} sets “{_snip(words, 30)}” in italic inside roman type: the "
                          f"italic accent word is a recognisable AI-generated flourish. Set the whole title in one "
                          f"style; if a word needs weight, make it the number that proves the point.",
                          title.element))
    return out


# -- rule 12: dead space --------------------------------------------------------------------------


def _largest_gap(spans: list[tuple[float, float]], lo: float, hi: float) -> tuple[float, float]:
    """The widest stretch of [lo, hi] that no span covers, as (start, end)."""
    best, cursor = (lo, lo), lo
    for a, b in sorted(spans):
        if a > cursor and a - cursor > best[1] - best[0]:
            best = (cursor, a)
        cursor = max(cursor, b)
    if hi - cursor > best[1] - best[0]:
        best = (cursor, hi)
    return best


def _empty_band(items: list[Item], ctx: Context) -> list[dict[str, Any]]:
    """A body slide that leaves a third of its body blank — a band across it or a strip down it.

    Only on layouts with a top title (covers and dividers are meant to be spacious) and only when the body
    holds at least `SPARSE_MIN_ITEMS` elements (one statement on its own is a choice, not a gap). Text is
    measured by its ink, everything else by its box, SVG-expanded and table-drawn parts included (a diagram
    drawn in SVG fills its space too); backgrounds covering most of the canvas are ignored.
    """
    grid = ctx.grid
    if not grid.top_title:
        return []
    title = _title_item(items, ctx)
    # The master's title zone bounds the body (a title that overflows it is its own finding, not a smaller body).
    top = grid.title_zone[1] + grid.title_zone[3] if grid.title_zone else (title.ink.y2 if title else 0.15 * grid.h)
    bottom = grid.bottom if grid.bottom is not None else 0.93 * grid.h
    left = grid.left if grid.left is not None else 0.0
    right = grid.right if grid.right is not None else grid.w
    if bottom - top < 2 * SPARSE_MIN_PX or right - left < 2 * SPARSE_MIN_PX:
        return []
    body = []
    for item in items:
        if item is title or item.box.area >= 0.9 * grid.w * grid.h:   # SVG and table parts count: they are ink
            continue
        box = item.ink if item.kind == "text" else item.box
        if box.y2 <= top or box.y >= bottom or box.x2 <= left or box.x >= right:
            continue
        body.append(box)
    if len(body) < SPARSE_MIN_ITEMS:
        return []
    out: list[dict[str, Any]] = []
    for axis, lo, hi in (("y", top, bottom), ("x", left, right)):
        spans = [(max(lo, b.y), min(hi, b.y2)) if axis == "y" else (max(lo, b.x), min(hi, b.x2)) for b in body]
        a, b = _largest_gap(spans, lo, hi)
        size = b - a
        if size < max(SPARSE_MIN_PX, SPARSE_BAND * (hi - lo)):
            continue
        share = round(100 * size / (hi - lo))
        if axis == "y":
            where = f"a band across the slide from y {_px(a)} to {_px(b)}"
            extent = f"the body between the title (y {_px(top)}) and the content bottom (y {_px(bottom)})"
        else:
            where = f"a strip down the slide from x {_px(a)} to {_px(b)}"
            extent = f"the body between the margins (x {_px(left)}–{_px(right)})"
        out.append(_f("warn", "empty-band",
                      f"Nothing sits in {where} ({_px(size)} px, {share}% of {extent}): the slide reads as "
                      f"unfinished. Grow the units into it — extend the main exhibit, add a side panel with the "
                      f"implication or recommendation, or a closing takeaway strip — using content from the brief. "
                      f"Ignore this only if the slide is a deliberate single statement.", None))
    return out


def _body_bounds(ctx: Context, title: Item | None) -> tuple[float, float]:
    grid = ctx.grid
    top = grid.title_zone[1] + grid.title_zone[3] if grid.title_zone else (title.ink.y2 if title else 0.15 * grid.h)
    return top, (grid.bottom if grid.bottom is not None else 0.93 * grid.h)


def _shape(item: Item) -> B:
    return item.ink if item.kind == "text" else item.box


def _stack_gaps(items: list[Item], ctx: Context) -> list[dict[str, Any]]:
    """Holes between stacked units: a unit, then — below it, sharing at least half the narrower one's
    width — the next unit, with more than `STACK_GAP_PX` of nothing in between (nothing at all: an SVG
    diagram's parts or a rule in the gap mean it is not empty). Units are the top-level blocks of the body;
    rules, source lines and footnotes are not units."""
    if not ctx.grid.top_title:
        return []
    title = _title_item(items, ctx)
    top, bottom = _body_bounds(ctx, title)
    units = []
    for item in items:
        b = _shape(item)
        if item is title or item.container is not None or item.bleed or b.y < top - 1 or b.y2 > bottom + 2:
            continue
        if item.kind in ("table", "chart", "image", "raster"):
            pass                                   # a table's own x-wpa mark does not make it internal here
        elif item.internal or min(b.w, b.h) <= UNIT_MIN_PX:
            continue
        elif item.kind == "text" and item.size_px <= SMALL_TEXT_PX:
            continue
        elif item.kind == "shape" and not (item.card and b.w >= 60 and b.h >= 24):
            continue                               # swatches, pills, markers: parts of a unit, not units
        units.append((item, b))
    holes = []
    for upper, ub in units:
        below = [(lower, lb) for lower, lb in units if lower is not upper and lb.y >= ub.y2 - 1
                 and min(ub.x2, lb.x2) - max(ub.x, lb.x) >= 0.5 * min(ub.w, lb.w)]
        if not below:
            continue
        lower, lb = min(below, key=lambda u: u[1].y)
        gap = lb.y - ub.y2
        if gap <= STACK_GAP_PX:
            continue
        hole = B(max(ub.x, lb.x), ub.y2 + 0.5, min(ub.x2, lb.x2) - max(ub.x, lb.x), gap - 1)
        if any(_shape(o).overlap_area(hole) > 0 for o in items if o is not upper and o is not lower):
            continue
        holes.append((gap, upper, lower, ub, lb))
    if not holes:
        return []
    holes.sort(key=lambda h: -h[0])
    listed = "; ".join(f"{_px(g)} px between {u.label or 'a unit'} (bottom y {_px(ub.y2)}) and {lower.label or 'the unit below'} "
                       f"(top y {_px(lb.y)})" for g, u, lower, ub, lb in holes[:3])
    more = f"; and {len(holes) - 3} more" if len(holes) > 3 else ""
    return [_f("warn", "stack-gap",
               f"Empty gap between stacked units: {listed}{more}. A gap wider than {STACK_GAP_PX:g} px reads as a "
               f"hole, not a gutter. Re-proportion: stretch the upper unit (taller rows, the evidence behind it) "
               f"or move the lower one up to a 12-24 px gutter, so side-by-side columns share their top and bottom "
               f"edges; use freed space for evidence from the brief, never filler.", holes[0][1].element)]


def _hollow_panels(items: list[Item], ctx: Context) -> list[dict[str, Any]]:
    """Panels whose content sits in the top part and leaves the bottom blank (text-in-the-top-half cards)."""
    if not ctx.grid.top_title:
        return []
    hollow = []
    for card in items:
        if not card.card or card.internal or card.bleed or card.box.w < 60 or card.box.h < 40:
            continue
        inside = [_shape(i) for i in items if i.container is card]
        if not any(i.kind == "text" and i.container is card for i in items):
            continue
        if any(i.kind == "text" and i.container is not card and i.ink.overlap_area(card.box) > 0
               and not card.box.contains(i.ink, 1.0) for i in items):
            continue                               # text spilling out of it: that is the finding (text-spill)
        content = inside[0]
        for b in inside[1:]:
            content = content.union(b)
        above = content.y - card.box.y
        slack = card.box.y2 - content.y2 - above
        if slack >= max(HOLLOW_MIN_PX, HOLLOW_SHARE * card.box.h):
            hollow.append((slack, card, content))
    if not hollow:
        return []
    hollow.sort(key=lambda h: -h[0])
    listed = "; ".join(f"{c.label} (box y {_px(c.box.y)}–{_px(c.box.y2)}, content ends y {_px(k.y2)}, "
                       f"{_px(s)} px blank below)" for s, c, k in hollow[:3])
    more = f"; and {len(hollow) - 3} more" if len(hollow) > 3 else ""
    return [_f("warn", "hollow-panel",
               f"{len(hollow)} panel(s) are hollow — content in the top part, blank below: {listed}{more}. Size each "
               f"panel to its content with equal padding top and bottom, then give the freed height to the unit "
               f"that needs it, or fill the panel with the evidence behind its claim (the numbers, the driver, "
               f"the action) from the brief. Sibling panels keep equal heights and shared edges.", hollow[0][1].element)]


# -- rule 13: show, don't list --------------------------------------------------------------------

_FIGURE = re.compile(r"(?<![\w.])[-−–+(]?[$€£¥]?\d[\d,]*(\.\d+)?\)?\s*(%|pts?|bps|pp|x|×|bn|m|k)?(?![\w])", re.I)
_YEARISH = re.compile(r"^(19|20)\d\d$")


_TIME_BEFORE = re.compile(r"(\b(q|h|fy|cy|day|days|week|weeks|month|months|year|phase|step|stage|gate|wave|"
                          r"version|v|no|page|slide|tier|level|round|sprint)\.?\s*|[–—-])$", re.I)
_TIME_AFTER = re.compile(r"^\s*([–—-]\s*\d+\s*)?(days?|weeks?|months?|years?|yrs|hours?|hrs|mins?|quarters?|"
                         r"am|pm)\b", re.I)


def _figures(text: str) -> int:
    """Numbers a reader has to compare: not years, dates, durations or numbering ("Q1", "Week 4",
    "Day 5–10", "Phase 2", a lone "1")."""
    count = 0
    text = text or ""
    for m in _FIGURE.finditer(text):
        token = m.group(0).strip()
        digits = re.sub(r"\D", "", token)
        if _YEARISH.match(token) or (len(digits) == 1 and token == digits):
            continue
        if _TIME_BEFORE.search(text[:m.start()]) or _TIME_AFTER.match(text[m.end():]):
            continue
        count += 1
    return count


def _encodes(item: Item, items: list[Item]) -> bool:
    """Does this element draw a quantity or state (a chart, an SVG mark, a bar, ball, dot or tick)?"""
    if item.kind == "chart":
        return True
    if item.kind == "table":
        fills = {(_hex6(((c.get("fill") or {}) if isinstance(c.get("fill"), dict) else {}).get("color") or "")
                  or "") for c in _cells(item.el)}
        fills.discard("")
        fills.discard("FFFFFF")
        return len(fills) >= 3           # a heat-shaded table encodes its values
    if item.kind != "shape" or item.border_side:
        return False
    if item.svg:
        return True                      # Harvey balls, checks, markers drawn in SVG
    b = item.box
    if min(b.w, b.h) <= UNIT_MIN_PX or b.area > 0.05 * 1280 * 720:
        return False                     # rules and separators are lines, not marks
    holds_text = any(t.kind == "text" and (t.container is item or b.contains(t.ink, 2.0)) for t in items)
    return not holds_text               # a bar, a tick, a dot: painted, and not a box around words (a pill)


_PERIOD = re.compile(r"^(fy|cy|q[1-4]|h[12]|ltm|ntm)?\s*'?(19|20)?\d\d[aepf]?$|^(ltm|ntm|fy\d\d[aepf]?)$", re.I)
_STAT_ROW = re.compile(r"^(median|mean|average|total|sum|subtotal)\b", re.I)


def _financial_grid(table: Any) -> bool:
    """A table that is itself the exhibit and is read, not charted: a statement across periods (FY24,
    2025E, Q1 …), a comps grid with median/mean/total rows, or a sensitivity grid with numeric headers."""
    cells = _cells(table)
    if not cells:
        return False
    top = min(int(c.get("r", 0) or 0) for c in cells)
    left = min(int(c.get("c", 0) or 0) for c in cells)
    header = [_text(c.get("paragraphs")) for c in cells if int(c.get("r", 0) or 0) == top]
    first_col = [_text(c.get("paragraphs")) for c in cells if int(c.get("c", 0) or 0) == left]
    periods = sum(1 for t in header if _PERIOD.match(t.strip()))
    numeric_head = sum(1 for t in header[1:] if _NUMBER.match(t.strip()))
    return periods >= 2 or numeric_head >= 3 or any(_STAT_ROW.match(t.strip()) for t in first_col)


def _exhibit_plain(items: list[Item], ctx: Context) -> list[dict[str, Any]]:
    """A body slide full of figures with nothing that draws them: the text benchmark table."""
    if not ctx.grid.top_title:
        return []
    title = _title_item(items, ctx)
    figures = 0
    for item in items:
        if item is title:
            continue
        if item.kind == "text" and item.size_px > SMALL_TEXT_PX and not item.svg:
            figures += _figures(item.text)
        elif item.kind == "table" and not _financial_grid(item.el):
            figures += sum(_figures(_text(c.get("paragraphs"))) for c in _cells(item.el))
    if figures < PLAIN_MIN_FIGURES or any(_encodes(i, items) for i in items):
        return []
    return [_f("warn", "exhibit-plain",
               f"The slide states {figures} figures as text and draws none of them: no chart, bar, target "
               f"tick, Harvey ball, heat fill or marker. Show the evidence: bars on one shared scale (a bar "
               f"column or bar_table / bullet_rows), the benchmark as a tick, the gap coloured by good/bad "
               f"(mind metrics where lower is better). Call plan_exhibit with the title for the exhibit.", None)]


def _chart_no_focus(items: list[Item]) -> list[dict[str, Any]]:
    """A single-colour chart with no highlighted point, target line or annotation over it."""
    out = []
    for chart in items:
        if chart.kind != "chart":
            continue
        spec = chart.el.spec or {}
        kind = str(spec.get("type") or "")
        if kind.startswith(("waterfall", "pie", "doughnut", "scatter")):
            continue                     # rises/falls or slices already carry colour meaning
        series = spec.get("series") or []
        points = sum(len(s.get("values") or []) for s in series if isinstance(s, dict))
        options = spec.get("options") or {}
        colours = {str(c).upper() for c in (spec.get("colors") or [])}
        if points <= FOCUS_MIN_POINTS or len(series) > 1 or len(colours) > 1:
            continue
        if options.get("pointColors") or options.get("referenceLines"):
            continue
        frame = chart.box
        annotated = any(o is not chart and not (o.kind == "text" and o.size_px <= SMALL_TEXT_PX)
                        and o.kind in ("text", "shape") and frame.contains(_shape(o), 2.0)
                        for o in items)
        if annotated:
            continue
        out.append(_f("warn", "chart-no-focus",
                      f"{chart.label} draws {points} points in one colour with no highlight or annotation: the "
                      f"reader cannot see the point. Grey the context and colour the focal bar(s) with "
                      f"`pointColors`, and add one annotation — a `referenceLines` target, a CAGR or delta label, "
                      f"or a callout on the key bar.", chart.element))
    return out


# ------------------------------------------------------------------------------------ assembly


def review(ir: Any, ctx: Context, *, max_findings: int = MAX_FINDINGS,
           extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """All design findings for a measured slide, most severe first, capped (pure; no browser).

    `extra` is findings measured elsewhere on the same IR (the workzone and header-band checks); they
    are ranked and capped with the rest."""
    grid = ctx.grid
    items = _items(ir, grid.w, grid.h)
    findings: list[dict[str, Any]] = []
    defects, flagged = _text_defects(ir, items, grid)
    findings += defects
    findings += _overlaps(ir, items)
    findings += _type(ir, items)
    findings += _margins(items, grid, flagged)
    findings += _alignment(items, grid)
    findings += _gaps(items)
    findings += _title(ir, items, ctx)
    findings += _palette(items, ctx)
    findings += _accents(items, ctx)
    findings += _source(items, grid)
    findings += _chart_zero_labels(items)
    findings += _tables(items)
    findings += _ai_look(items, ctx)
    findings += _empty_band(items, ctx)
    findings += _stack_gaps(items, ctx)
    findings += _hollow_panels(items, ctx)
    findings += _exhibit_plain(items, ctx)
    findings += _chart_no_focus(items)
    findings += list(extra or [])
    return _rank(findings, max_findings)


def _rank(findings: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    order = {rule: i for i, rule in enumerate(RULE_ORDER)}
    errors = sorted((f for f in findings if f["level"] == "error"), key=lambda f: order.get(f["rule"], 99))
    by_rule: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in findings:
        if f["level"] != "error":
            by_rule[f["rule"]].append(f)
    warns: list[dict[str, Any]] = []
    rules = sorted(by_rule, key=lambda r: order.get(r, 99))
    while any(by_rule[r] for r in rules):
        for rule in rules:
            if by_rule[rule]:
                warns.append(by_rule[rule].pop(0))
    ranked = errors + warns
    if cap and len(ranked) > cap:
        rest = ranked[cap:]
        counts = Counter(f["rule"] for f in rest)
        ranked = ranked[:cap] + [_f(
            "warn", "more-findings",
            f"{len(rest)} more design finding(s) not shown ("
            + ", ".join(f"{rule} ×{n}" for rule, n in counts.most_common())
            + "). Fix the ones above, then review the slide again.", None)]
    return ranked


# ------------------------------------------------------------------------------------ the master


def _furniture_top(png: Path, w: float, h: float) -> float | None:
    """The top of the ink in the bottom 20 % of a layout background — its footer furniture — or None.

    None when the band is blank or mostly painted (a picture background says nothing about furniture).
    """
    try:
        from PIL import Image, ImageChops
    except Exception:  # noqa: BLE001
        log.debug("PIL unavailable; no image comparison", exc_info=True)
        return None
    try:
        with Image.open(png) as source:
            image = source.convert("RGB")
        if image.size != (int(w), int(h)):
            image = image.resize((int(w), int(h)))
    except Exception:  # noqa: BLE001
        log.debug("could not compare the render with the background", exc_info=True)
        return None
    top = int(h * 0.8)
    band = image.crop((0, top, int(w), int(h)))
    background = cast("tuple[int, int, int]",
                      max(band.resize((64, 16)).getcolors(64 * 16) or [(0, (255, 255, 255))])[1])
    diff = ImageChops.difference(band, Image.new("RGB", band.size, background)).convert("L")
    mask = diff.point(lambda v: 255 if v > 40 else 0)
    bbox = mask.getbbox()
    if not bbox:
        return None
    small = mask.resize((128, 32))
    inked = 1 - small.histogram()[0] / (128 * 32)
    if inked > 0.35:
        return None
    return float(top + bbox[1])


def _grid(manifest: Any, layout: Any, layouts_dir: Path | None, manifest_dict: dict[str, Any]) -> Grid:
    grid = Grid(w=float(manifest.canvas_w), h=float(manifest.canvas_h))
    w, h = grid.w, grid.h
    title = layout.placeholder("title") if layout is not None else None
    if layout is not None and layout.placeholder("ctrTitle") is not None and title is None:
        grid.top_title = False
    source_layout = layout
    if title is None or title.y > 0.25 * h:
        if title is not None:
            grid.top_title = False
        try:
            from app.core.design_refs import notes
        except Exception:  # noqa: BLE001
            log.debug("design notes module unavailable", exc_info=True)
            notes = None  # type: ignore[assignment]
        fallback = notes.content_layout(manifest_dict) if notes else None
        if fallback:
            try:
                source_layout = manifest.layout(fallback["id"])
                candidate = source_layout.placeholder("title")
                title = candidate if candidate is not None and candidate.y <= 0.25 * h else None
            except Exception:  # noqa: BLE001
                title = None
    if title is not None:
        grid.title_zone = (title.x, title.y, title.w, title.h)
    if title is not None and grid.top_title:   # a cover or section layout keeps its own side margins
        grid.left = title.x
        edges = [p.x + p.w for lay in manifest.layouts for p in lay.placeholders]
        if edges and max(edges) < 0.8 * w:
            grid.right = w - title.x         # a 4:3-authored master on a 16:9 canvas: mirror the margin
        else:
            grid.right = title.x + title.w
    footer_y = [p.y for p in (layout.placeholders if layout else []) if p.type in ("dt", "ftr", "sldNum") and p.y > 0.7 * h]
    candidates: list[tuple[float, str, float]] = []
    if footer_y:  # a placeholder box already carries its own inset: content may run to its top
        candidates.append((min(footer_y), "the master's footer placeholders", 0.0))
    if layout is not None and layout.background and layouts_dir is not None:
        png = layouts_dir / layout.background
        if png.suffix.lower() == ".png" and png.is_file():
            top = _furniture_top(png, w, h)
            if top is not None:
                candidates.append((top, "the layout's footer furniture", FOOTER_CLEARANCE_PX))
    if candidates:
        top, grid.footer_from, clearance = min(candidates, key=lambda c: c[0] - c[2])
        grid.footer_top = top
        grid.bottom = top - clearance
    else:
        grid.bottom = h - max(24.0, round((grid.left or 48) / 2))
    return grid


def _context(manifest: Any, layout: Any, layouts_dir: Path | None, manifest_dict: dict[str, Any]) -> Context:
    grid = _grid(manifest, layout, layouts_dir, manifest_dict)
    theme = manifest.theme_of(layout) if layout is not None else manifest.theme
    palette: dict[str, str] = {}
    for slot, value in ((theme or {}).get("colors") or {}).items():
        hex6 = _hex6(value)
        if hex6 and hex6 not in palette and slot not in ("hlink", "folHlink"):
            palette[hex6] = slot
    primary = None
    try:
        from app.core.design_refs import notes

        primary = notes.colour_roles(manifest_dict).get("primary") if manifest_dict else None
    except Exception:  # noqa: BLE001
        log.debug("could not read the palette's primary colour", exc_info=True)
        primary = None
    return Context(grid=grid, palette=palette, primary=primary)


# ------------------------------------------------------------------------------------ measuring


_page_local = threading.local()


def _page_state() -> dict[str, Any]:
    """This thread's measuring page and context: a page belongs to the thread that made it."""
    state = getattr(_page_local, "state", None)
    if state is None:
        state = {}
        _page_local.state = state
    return cast("dict[str, Any]", state)


def _measuring_page(w: int, h: int) -> Any:
    """This module's page on the calling thread's browser (`app.core.browser_pool.thread_browser`),
    created lazily and reused while it stays connected."""
    try:
        from app.core.browser_pool import thread_browser
    except Exception as exc:  # noqa: BLE001 — playwright is not importable here
        raise DesignLintUnavailable(f"the measuring browser is not available: {type(exc).__name__}: {exc}") from exc

    state = _page_state()
    page, context = state.get("page"), state.get("context")
    try:
        if (page is not None and context is not None and not page.is_closed() and context.browser
                and context.browser.is_connected()):
            return page
    except Exception:  # noqa: BLE001
        log.debug("the measuring page is gone; opening a new one", exc_info=True)
    close_measuring_page()
    try:
        context = thread_browser().new_context(viewport={"width": w, "height": h}, device_scale_factor=1,
                                               color_scheme="light")
        page = context.new_page()
    except Exception as exc:  # noqa: BLE001 — no browser installed, or it would not start
        raise DesignLintUnavailable(f"the measuring browser could not be started: {exc}") from exc
    state.update(page=page, context=context)
    return page


def close_measuring_page() -> None:
    """Close this thread's measuring page and context (the browser itself belongs to the pool)."""
    for key in ("page", "context"):
        thing = _page_state().pop(key, None)
        try:
            if thing is not None:
                thing.close()
        except Exception:  # noqa: BLE001 — a page that will not close is already gone
            log.debug("could not close the measuring %s", key, exc_info=True)


def _measure(html: Path, manifest: Any, layout_id: str, layout: Any, assets: Path) -> tuple[Any, dict[str, str]]:
    """Runs on a browser thread: extract, keep the authored placeholders, classify as the pipeline does.

    Derived images go to a scratch workspace of their own, removed afterwards, so a review never
    touches an export's captures."""
    from app.engine.classify.charts import recognise_charts
    from app.engine.classify.placeholders import map_placeholders
    from app.engine.extract.html import extract_html

    page = _measuring_page(manifest.canvas_w, manifest.canvas_h)
    with tempfile.TemporaryDirectory(prefix="design-lint-") as scratch:
        try:
            ir = extract_html(html, manifest, layout_id, assets, workspace=Path(scratch),
                              slide_id="design-lint", page=page)
        except Exception:
            close_measuring_page()
            raise
    raw = {str(e.id): str((e.placeholder or {}).get("type")) for e in ir.elements
           if e.kind == "text" and isinstance(e.placeholder, dict) and e.placeholder.get("type")}
    ir = recognise_charts(ir)
    ir = map_placeholders(ir, layout)
    return ir, raw


#: The canvas a slide without a master is measured on (16:9 at 96 px per inch).
_DEFAULT_MANIFEST: dict[str, Any] = {"slideWidthEmu": 12192000, "slideHeightEmu": 6858000,
                                     "canvas": {"w": 1280, "h": 720, "pxPerIn": 96}}


def design_findings_html(
    html_path: Path,
    manifest_block: dict[str, Any] | None,
    layout_id: str | None,
    *,
    assets_dir: Path,
    layouts_dir: Path | None,
    workzone: Workzone | None = None,
    run: Callable[[Callable[[], Any]], Any] | None = None,
    max_findings: int = MAX_FINDINGS,
) -> list[dict[str, Any]]:
    """Design-quality findings for one slide's HTML file.

    Each finding is `{"level": "warn"|"error", "rule": str, "message": str, "element": str | None}`,
    most severe first, at most `max_findings` of them plus one `more-findings` entry counting the rest.

    `manifest_block` is the master's v2 manifest as a dict (None: a blank 1280x720 canvas);
    `layouts_dir` holds its layout backgrounds (for the footer band); `assets_dir` the slide's assets.
    `workzone`, when the brand has one, adds the workzone and header-band checks
    (`workzone_lint.workzone_findings`). `run` executes the browser work on a browser thread
    (`app.core.preview.run_on_preview_thread`); without it, the calling thread's browser is used.

    Raises `DesignLintError` when the file is missing or the slide cannot be measured.
    """
    from app.core.design_refs.workzone_lint import workzone_findings
    from app.engine.manifest import Manifest

    html = Path(html_path)
    if not html.is_file():
        raise DesignLintError(f"no slide HTML at {html.name}")
    block = manifest_block or {}
    try:
        manifest = Manifest.from_json(block) if block else Manifest.from_json(dict(_DEFAULT_MANIFEST))
    except Exception as exc:  # noqa: BLE001
        raise DesignLintError(f"the master manifest could not be read: {exc}") from exc
    lid = layout_id or ""
    layout = next((lay for lay in manifest.layouts if lay.id == lid), None)
    ctx = _context(manifest, layout, layouts_dir, block)
    def measure() -> tuple[Any, dict[str, str]]:
        return _measure(html, manifest, lid or "none", layout, assets_dir)

    try:
        ir, raw = cast("tuple[Any, dict[str, str]]", run(measure)) if run is not None else measure()
    except DesignLintError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DesignLintError(f"the slide could not be measured: {type(exc).__name__}: {exc}") from exc
    ctx.raw_placeholders = raw
    extra = (workzone_findings(ir, workzone, manifest.canvas_w, manifest.canvas_h, raw_placeholders=raw)
             if workzone is not None else [])
    return review(ir, ctx, max_findings=max_findings, extra=extra)
