"""Two texts drawn over each other — the measured check the static linter cannot make.

This is the second half of the pair `13-WP3` adds. The first, `text-overflow`, lives in
`engine/extract/page.js`, because only the browser holds both the lines it broke and the box the
author declared; the IR deliberately records a text element's own vertical extent, not the block it
was written into, so nothing downstream could recover the overflow. Overlap is the opposite: it
needs no declared box, only line boxes and font sizes, and it must run **after** classification —
`recognise_charts` turns a hand-drawn SVG chart's axis and value labels into one chart element, and
those labels sit a couple of px apart by design. Reporting them would bury the one real finding
under thirty-five false ones.

What the rule deliberately does not report (each was a decision, not an oversight):

* **text over a shape, an image, a table or a chart.** A badge over a card's fill, a heading over a
  tinted band, a label inside a rounded box: that is how slides are drawn. Only text over *text* is
  a defect nobody chose.
* **rotated text.** Its box is the box before rotation, so an axis-aligned band would test a
  rectangle that is not on the slide.
* **an ancestor over its own descendant.** A card's own paragraph sits inside the card by
  construction; the two records are the same ink seen at two levels of the DOM.
* **line boxes that merely touch.** The band compared is the em box, not the line box: consecutive
  lines of a tight `line-height` share line-box ink by design, and a descender brushing the cap
  height of the line below is not a collision. `OVERLAP_MIN_PX` is the width of that judgement.

Pure Python, no browser, no file access: `text_overlaps` is a function of the IR alone, which is
what keeps `test_extraction_is_deterministic` true with these diagnostics in the list.
"""
from __future__ import annotations

from typing import cast

from app.engine.ir import IR, Box, Diagnostic, Element

#: Shared ink narrower than this is leading, not a collision (see the module docstring).
OVERLAP_MIN_PX = 2.0

#: A quoted first line longer than this is cut at the last word boundary after `QUOTE_MIN`.
QUOTE_MAX = 60
QUOTE_MIN = 30


def annotate_text_layout(ir: IR) -> IR:
    """Add the measured text-layout diagnostics to `ir` and return it.

    Called from `engine.pipeline.export_deck` after `map_placeholders`, so every export stores these
    with the slide's version and the readiness panel can show them without measuring anything again.
    """
    for diagnostic in text_overlaps(ir):
        ir.add_diagnostic(diagnostic.level, diagnostic.source, diagnostic.message, diagnostic.elementId)
    return ir


def text_overlaps(ir: IR) -> list[Diagnostic]:
    """Every pair of text elements whose glyphs are drawn over each other, once per pair.

    Does not mutate `ir`. The order is paint order of the first element, then of the second, so two
    runs of the same export produce the same list.
    """
    texts = [
        element
        for element in ir.paint_sorted()
        if element.kind == "text" and not element.rotation and element.paragraphs
    ]
    bands = {id(element): ink_bands(element) for element in texts}
    found: list[Diagnostic] = []
    for index, first in enumerate(texts):
        for second in texts[index + 1:]:
            if nested_paths(first, second):
                continue
            shared = worst_shared_ink(bands[id(first)], bands[id(second)])
            if shared is None:
                continue
            found.append(Diagnostic(
                level="warn",
                source="text-overlap",
                message=f"{_quoted(first)} and {_quoted(second)} overlap by {round(shared)} px",
                elementId=first.id,
            ))
    return found


# ------------------------------------------------------------------------------------ the geometry


def ink_bands(element: Element) -> list[tuple[float, float, float, float]]:
    """One `(x1, x2, top, bottom)` per line: its x-extent and the em box centred in its line box.

    The em box, not the line box, is the ink: a line box is `line-height` tall and says nothing
    about where the glyphs are inside it, so two lines set 1.0 apart would "overlap" on every slide
    ever designed. The em size is the largest run on the line, because that run is what makes the
    line tall.
    """
    bands: list[tuple[float, float, float, float]] = []
    for paragraph in element.paragraphs or []:
        for line in paragraph.get("lines") or []:
            raw = line.get("box")
            if not raw:
                continue
            box = cast(Box, Box.from_json(raw))  # raw is a non-empty box dict
            sizes = [float(run.get("sizePx") or 0.0) for run in line.get("runs") or []]
            size = max(sizes) if sizes else 0.0
            if size <= 0.0:
                continue
            bands.append((box.x, box.x2, box.cy - size / 2, box.cy + size / 2))
    return bands


def worst_shared_ink(
    first: list[tuple[float, float, float, float]],
    second: list[tuple[float, float, float, float]],
) -> float | None:
    """The largest vertical ink two lines share where they also share width — `None` if none do."""
    worst: float | None = None
    for ax1, ax2, atop, abottom in first:
        for bx1, bx2, btop, bbottom in second:
            if min(ax2, bx2) - max(ax1, bx1) <= OVERLAP_MIN_PX:
                continue
            shared = min(abottom, bbottom) - max(atop, btop)
            if shared <= OVERLAP_MIN_PX:
                continue
            if worst is None or shared > worst:
                worst = shared
    return worst


def nested_paths(first: Element, second: Element) -> bool:
    """True when one element's source path is inside the other's — the same ink, twice."""
    one = str((first.source or {}).get("path") or "")
    other = str((second.source or {}).get("path") or "")
    if not one or not other:
        return False
    return other.startswith(f"{one} > ") or one.startswith(f"{other} > ")


# The geometry is shared with the deck-side text row (`engine/verify/text_deck.py`), which rebuilds
# the same bands from the exported file; the old private names stay as aliases.
_ink_bands = ink_bands
_worst_shared_ink = worst_shared_ink
_nested = nested_paths


# ------------------------------------------------------------------------------------- the wording


def _quoted(element: Element) -> str:
    return f"“{quote_of(element)}”"


def quote_of(element: Element) -> str:
    """A text element's first line, quoted for a person.

    The same rule as `quoteOf` in `engine/extract/page.js`: the two measured text diagnostics are
    written in different languages and must read as one voice.
    """
    paragraphs = element.paragraphs or []
    lines = (paragraphs[0].get("lines") or []) if paragraphs else []
    runs = (lines[0].get("runs") or []) if lines else []
    text = " ".join("".join(str(run.get("text") or "") for run in runs).split())
    if len(text) <= QUOTE_MAX:
        return text
    cut = text.rfind(" ", 0, QUOTE_MAX + 1)
    if cut < QUOTE_MIN:
        cut = QUOTE_MAX
    return f"{text[:cut]}…"
