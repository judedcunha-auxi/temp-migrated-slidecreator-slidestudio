"""The text row: what the exported file's text will look like, read back from the file.

The pixel gate cannot see the defect this row exists for: a lost 6 px gap between two runs is a
negligible area and a fatal misreading ("…VALUE COMES FROMILLUSTRATIVE…"). So the row reads the
`.pptx` and judges its text geometrically, against the IR it was emitted from (16-WPG §4, plan D6):

* **`text_collisions`** — a *join* is two adjacent runs in one `a:br` segment with no whitespace
  between them (a space character in the file is a textual gap PowerPoint keeps). A PowerPoint
  paragraph has no geometry between runs, so a join whose browser gap was wider than
  `COLLISION_MIN_PX` is glued together in the export. The gap is read from the browser's own run
  boxes when the IR carries them (`extras["x-run-boxes"]`, written by the extractor), and
  otherwise predicted: the line's measured width minus its runs' advance widths is the sum of the
  line's geometric gaps. No alphabetic or digit rule: `partner|2023` and `$4.2B|+12%` are caught
  by their gap, `<b>Re</b>venue`, `CO<sub>2</sub>`, `12<sup>th</sup>` have a gap of ~0 and are not.
* **`export_overlaps`** — every unrotated, non-table text shape's line bands are rebuilt from the
  file by inverting the emitter's own placement model, and every pair of shapes whose bands share
  more ink than the same two IR elements did in the design is reported. A design-side overlap is
  the author's (a readiness finding, `text_layout.text_overlaps`), not the export's.

Everything here is a function of the file and the IR: no browser, no renderer. Owner: WP-G
(`text_collisions`, `export_overlaps`, the segment reader); B extends the module with
`text_positions`, `text_rows` and `text_seams` and their keys in `text_report` (plan D6, §7).
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pptx import Presentation
from pptx.oxml.ns import qn

from app.config import engine as config
from app.engine.emit import text as text_engine
from app.engine.ir import IR, Box, Element
from app.engine.verify import fit
from app.engine.verify.text_layout import OVERLAP_MIN_PX, ink_bands, nested_paths, worst_shared_ink

#: A join whose browser gap is wider than this is a collision: the runs will touch in the export.
#: 2 px is below a space at every size the contract allows (a 10 px Arial space is 2.8 px).
COLLISION_MIN_PX: float = 2.0

#: Without run boxes the gap is a prediction: measured line width − Σ predicted run advances. A
#: line is reported when that residual exceeds `max(PREDICTION_MIN_PX, PREDICTION_SLACK × Σ)`.
PREDICTION_MIN_PX: float = 3.0

#: Proportional slack for the predicted residual (long lines accumulate sub-pixel differences).
#: Measured 2026-09-25 on every join-free line of the client reference slides and `fixtures/torture/text.html`
#: (`line_residuals`): see the numbers in the comment below the constant's first use.
PREDICTION_SLACK: float = 0.02

#: The IR key the extractor writes the browser's per-run boxes under: per paragraph, per line, per
#: run `[left, right]` in canvas px, in run order (plan D6; written by B's `measureParagraph`).
RUN_BOXES_KEY = "x-run-boxes"

Band = tuple[float, float, float, float]


# ------------------------------------------------------------------------------------ the reader


@dataclass(slots=True)
class _Shape:
    """One text-bearing shape of the deck, paired with the IR element it came from (if any)."""

    slide: int
    shape: Any
    element: Element | None
    rotated: bool
    table: bool


@dataclass(slots=True)
class _Counts:
    unmatched: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)


def _label_of(element: Element) -> str:
    """The name the emitter gave the element's shape (`emit.shapes.name_shape`)."""
    source = element.source or {}
    return str(element.name or source.get("svg") or source.get("path") or element.id or "")[:255]


def _box_of(shape: Any) -> Box:
    return Box(shape.left / config.EMU_PER_PX, shape.top / config.EMU_PER_PX,
               (shape.width or 0) / config.EMU_PER_PX, (shape.height or 0) / config.EMU_PER_PX)


def _walk(shapes: Any) -> Iterator[Any]:
    for shape in shapes:
        if shape.shape_type is not None and str(shape.shape_type).startswith("GROUP"):
            yield from _walk(shape.shapes)
        else:
            yield shape


def _pair(shape: Any, candidates: list[Element], used: set[int]) -> Element | None:
    """The IR element a shape was emitted from: by the emitter's own label, in paint order, then IoU."""
    name = str(shape.name or "")
    for index, element in enumerate(candidates):
        if index not in used and name and name == _label_of(element):
            used.add(index)
            return element
    return fit._match_element(_box_of(shape), name, candidates, used)


def _shapes(presentation: Any, irs: Sequence[IR], counts: _Counts) -> Iterator[_Shape]:
    """Every text shape and table of the deck, paired with its IR element."""
    for index, slide in enumerate(presentation.slides, start=1):
        ir = irs[index - 1] if index - 1 < len(irs) else None
        texts = [] if ir is None else [e for e in ir.paint_sorted() if e.kind == "text"]
        tables = [] if ir is None else [e for e in ir.paint_sorted() if e.kind == "table"]
        used_texts: set[int] = set()
        used_tables: set[int] = set()
        for shape in _walk(slide.shapes):
            if shape.left is None or shape.top is None:
                continue
            if getattr(shape, "has_table", False):
                element = _pair(shape, tables, used_tables)
                if element is None:
                    counts.unmatched += 1
                yield _Shape(index, shape, element, False, True)
                continue
            if not shape.has_text_frame or not shape.text_frame.text.strip():
                continue
            element = _pair(shape, texts, used_texts)
            if element is None:
                counts.unmatched += 1
            rotation = float(getattr(shape, "rotation", 0.0) or 0.0)
            yield _Shape(index, shape, element, abs(rotation % 360.0) > 1e-6, False)


def _emitted_lines(element: Element) -> list[list[tuple[int, int, dict[str, Any]]]]:
    """`[(paragraph index, line index, line)]` per emitted `a:p` — the IR lines `plan_text` writes.

    `plan_text` drops lines without runs and paragraphs left without lines, so the deck's `a:p`
    number *i* is the *i*-th surviving IR paragraph and its `a:br` segment *j* the *j*-th surviving
    line. The indices returned are the IR's own, so a finding points at the IR.
    """
    out: list[list[tuple[int, int, dict[str, Any]]]] = []
    for p_index, paragraph in enumerate(element.paragraphs or []):
        lines = [(p_index, l_index, line) for l_index, line in enumerate(paragraph.get("lines") or [])
                 if line.get("runs")]
        if lines:
            out.append(lines)
    return out


def _segments_of(paragraph: Any) -> list[list[str]]:
    """One `a:p`'s run texts per `a:br` segment, empty runs kept so positions match the IR's runs."""
    segments: list[list[str]] = [[]]
    for child in paragraph.iterchildren():
        if child.tag == qn("a:br"):
            segments.append([])
        elif child.tag in (qn("a:r"), qn("a:fld")):
            node = child.find(qn("a:t"))
            segments[-1].append(node.text if node is not None and node.text else "")
    return segments


# ------------------------------------------------------------------------------------- collisions


def _joins(texts: Sequence[str]) -> list[tuple[int, int]]:
    """Run-index pairs `(a, b)` of adjacent non-empty runs with no whitespace between them."""
    real = [index for index, text in enumerate(texts) if text]
    return [(a, b) for a, b in zip(real, real[1:], strict=False)
            if not texts[a][-1].isspace() and not texts[b][0].isspace()]


def _run_text(run: dict[str, Any], element: Element) -> str:
    text = str(run.get("text") or "")
    return text.lower() if element.transformCase == "lower" else text


def predicted_width(runs: Sequence[dict[str, Any]], element: Element) -> float | None:
    """Σ advance widths of a line's runs in the faces the browser drew them — None if one is absent."""
    total = 0.0
    for run in runs:
        text = _run_text(run, element)
        if not text:
            continue
        weight, italic = int(run.get("weight") or 400), bool(run.get("italic"))
        face = text_engine.face_for_exact(run.get("font"), weight, italic)
        if face is None:
            return None
        total += text_engine.face_width_px(face, text, float(run.get("sizePx") or 0.0))
        total += float(run.get("letterSpacingPx") or 0.0) * len(text)
    return total


def _stretched(element: Element, p_index: int, l_index: int) -> bool:
    """A justified line other than the paragraph's last is stretched: its residual is not a gap."""
    paragraph = (element.paragraphs or [])[p_index]
    return paragraph.get("align") == "justify" and l_index < len(paragraph.get("lines") or []) - 1


def _run_boxes(element: Element, p_index: int, l_index: int, runs: int) -> list[Any] | None:
    boxes: Any = (element.extras or {}).get(RUN_BOXES_KEY)
    try:
        line = boxes[p_index][l_index]
    except (TypeError, IndexError, KeyError):
        return None
    return line if isinstance(line, list) and len(line) == runs else None


def _judge_line(
    slide: int, shape_name: str, element: Element, p_index: int, l_index: int,
    line: dict[str, Any], texts: list[str], counts: _Counts, *, boxes_allowed: bool = True,
) -> dict[str, Any] | None:
    """One `a:br` segment against its IR line: a collision entry, or None."""
    runs = list(line.get("runs") or [])
    if len(texts) != len(runs):
        counts.skipped += 1
        return None
    joins = _joins(texts)
    if not joins:
        return None

    def entry(pairs: list[tuple[int, int]], gap: float, method: str) -> dict[str, Any]:
        return {"slide": slide, "shape": shape_name, "paragraph": p_index, "line": l_index,
                "joins": [{"left": texts[a][-24:], "right": texts[b][:24]} for a, b in pairs],
                "gapPx": round(gap, 2), "method": method}

    boxes = _run_boxes(element, p_index, l_index, len(runs)) if boxes_allowed else None
    if boxes is not None:
        gaps = [(a, b, float(boxes[b][0]) - float(boxes[a][1])) for a, b in joins]
        colliding = [(a, b, gap) for a, b, gap in gaps if gap > COLLISION_MIN_PX]
        if not colliding:
            return None
        return entry([(a, b) for a, b, _ in colliding], max(g for _, _, g in colliding), "boxes")

    if _stretched(element, p_index, l_index):
        counts.skipped += 1
        return None
    predicted = predicted_width(runs, element)
    measured = float((line.get("box") or {}).get("w") or 0.0)
    if predicted is None or measured <= 0:
        counts.skipped += 1
        return None
    residual = measured - predicted
    if residual > max(PREDICTION_MIN_PX, PREDICTION_SLACK * predicted):
        return entry(joins, residual, "predicted")
    return None


def _collisions(shapes: Sequence[_Shape], counts: _Counts) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for item in shapes:
        element = item.element
        if element is None:
            continue
        name = str(item.shape.name or "")
        if item.table:
            found.extend(_cell_collisions(item, element, counts))
            continue
        paragraphs = list(item.shape.text_frame._txBody.iterchildren(qn("a:p")))
        emitted = _emitted_lines(element)
        for position, paragraph in enumerate(paragraphs):
            segments = _segments_of(paragraph)
            ir_lines = emitted[position] if position < len(emitted) else []
            for number, texts in enumerate(segments):
                if number >= len(ir_lines):
                    if any(texts):
                        counts.skipped += 1          # a segment with no IR line to judge it by
                    continue
                p_index, l_index, line = ir_lines[number]
                hit = _judge_line(item.slide, name, element, p_index, l_index, line, texts, counts)
                if hit is not None:
                    found.append(hit)
    return found


def _cell_run_boxes(element: Element, r: int, c: int) -> list[Any] | None:
    """A table cell's browser run boxes, in the `x-run-boxes` shape (per IR paragraph, per line, per
    run `[left, right]`): WP-A's cell walk keeps them in `extras["x-wpa"].cells["r:c"].runBoxes`
    (plan §16 #11) — None when the cell carries none (a hand-built IR, Path B, an older IR)."""
    cells = ((element.extras or {}).get("x-wpa") or {}).get("cells")
    entry = cells.get(f"{r}:{c}") if isinstance(cells, dict) else None
    boxes = entry.get("runBoxes") if isinstance(entry, dict) else None
    return boxes if isinstance(boxes, list) else None


def _cell_collisions(item: _Shape, element: Element, counts: _Counts) -> list[dict[str, Any]]:
    """Table cells: `write_cell` writes every IR paragraph and every line, so indices are direct.

    A cell is judged by its own run boxes, as a text shape is (plan §16 #22): its IR paragraphs
    stand in for the element's, its `runBoxes` for `x-run-boxes`. A line whose boxes are absent or
    do not match its runs falls back to the predicted widths, exactly as `_judge_line` does.
    """
    found: list[dict[str, Any]] = []
    table = item.shape.table
    for cell in element.cells or []:
        r, c = int(cell.get("r") or 0), int(cell.get("c") or 0)
        try:
            frame = table.cell(r, c).text_frame
        except IndexError:
            counts.skipped += 1
            continue
        paragraphs = list(frame._txBody.iterchildren(qn("a:p")))
        boxes = _cell_run_boxes(element, r, c)
        cell_element = element.replace(paragraphs=cell.get("paragraphs") or [],
                                       extras={} if boxes is None else {RUN_BOXES_KEY: boxes})
        for p_index, paragraph in enumerate(cell.get("paragraphs") or []):
            if p_index >= len(paragraphs):
                break
            segments = _segments_of(paragraphs[p_index])
            for l_index, line in enumerate(paragraph.get("lines") or []):
                if l_index >= len(segments) or not line.get("runs"):
                    continue
                hit = _judge_line(item.slide, f"{item.shape.name} r{r}c{c}", cell_element, p_index,
                                  l_index, line, segments[l_index], counts)
                if hit is not None:
                    found.append(hit)
    return found


def text_collisions(pptx: Path, irs: Sequence[IR]) -> list[dict[str, Any]]:
    """Joins the browser separated by more than `COLLISION_MIN_PX` of layout — glued in the file.

    Entry: `{slide, shape, paragraph, line, joins: [{left, right}], gapPx, method}`, with the IR's
    paragraph and line indices and `method` "boxes" (the browser's run boxes) or "predicted".
    """
    return cast(list[dict[str, Any]], text_report(pptx, irs)["collisions"])


# ------------------------------------------------------------------------------ export overlaps


def _margin_left_px(paragraph: Any) -> float:
    properties = paragraph._p.find(qn("a:pPr"))
    if properties is None or properties.get("marL") is None:
        return 0.0
    return int(properties.get("marL")) / config.EMU_PER_PX


def _alignment(paragraph: Any) -> str:
    value = str(paragraph.alignment or "").split(".")[-1].split(" ")[0].upper()
    return {"CENTER": "center", "RIGHT": "right"}.get(value, "left")


def deck_bands(shape: Any, fonts: dict[str, str]) -> list[Band]:
    """The line bands of one exported text shape, rebuilt by inverting the emitter's placement.

    `text.plan_text` put the box top where the first baseline lands at the browser's baseline:
    `top = baseline + descent − L − dy`. Read backwards, line *k* of a paragraph with exact spacing
    `L` has its baseline at `y_k + L − descent + dy`, and its em box centred `(ascent − descent) / 2`
    above that — the same em box `text_layout.ink_bands` builds from the IR. `dy` is the first
    line's `TEXT_DY_BY_FONT` residual, which shifted the whole box. Widths are PowerPoint's
    predicted ones (fit's advances, tracking included), so a line PowerPoint draws wider is wider here.
    """
    frame = shape.text_frame
    box = _box_of(shape)
    inset_l, inset_t, inset_r, _ = fit._insets_px(shape)
    inner = max(1.0, box.w - inset_l - inset_r)
    wrap = frame.word_wrap is not False
    defaults = fit._shape_defaults(shape, fonts)
    y = box.y + inset_t
    dy_px: float | None = None
    bands: list[Band] = []
    for paragraph in frame.paragraphs:
        y += fit._space_px(paragraph.space_before)
        segments = fit._segments(paragraph, fit._paragraph_defaults(paragraph, defaults, fonts), fonts)
        pieces = [piece for segment in segments for piece in segment]
        spacing = fit._line_height_px(paragraph, pieces)
        align = _alignment(paragraph)
        indent = _margin_left_px(paragraph)
        for segment in segments:
            count = fit._predict_lines(segment, inner - indent, wrap)[0]
            real = [piece for piece in segment if piece.text]
            if not real:
                y += spacing * count
                continue
            biggest = max(real, key=lambda piece: piece.size_px)
            size = biggest.size_px
            face = text_engine.face_for(biggest.family, 700 if biggest.bold else 400, biggest.italic)
            ascent, descent = (face.ascent, face.descent) if face is not None else (0.8, 0.2)
            if dy_px is None:
                dy_px = text_engine.dy_factor({"font": biggest.family, "weight": 700 if biggest.bold else 400,
                                               "italic": biggest.italic}) * size
            widths = [piece.width for piece in real]
            width = sum(w for w in widths if w is not None)
            start = box.x + inset_l + indent
            room = inner - indent
            x1 = start + {"left": 0.0, "center": (room - width) / 2.0, "right": room - width}[align]
            for _ in range(count):
                baseline = y + spacing - descent * size + dy_px
                centre = baseline - (ascent - descent) / 2.0 * size
                bands.append((x1, x1 + width, centre - size / 2.0, centre + size / 2.0))
                y += spacing
        y += fit._space_px(paragraph.space_after)
    return bands


def _overlaps(presentation: Any, shapes: Sequence[_Shape], irs: Sequence[IR],
              counts: _Counts) -> list[dict[str, Any]]:
    slides = list(presentation.slides)
    by_slide: dict[int, list[_Shape]] = {}
    for item in shapes:
        if item.table:
            continue                                   # row growth is fit's `_check_table`
        if item.element is None:
            continue                                   # counted as unmatched already
        if item.rotated or item.element.rotation:
            counts.skipped += 1
            continue
        by_slide.setdefault(item.slide, []).append(item)

    found: list[dict[str, Any]] = []
    for slide, items in sorted(by_slide.items()):
        ir = irs[slide - 1] if slide - 1 < len(irs) else None
        fonts = fit._theme_fonts_of(slides[slide - 1], ir.fonts.forced if ir is not None else None)
        bands = {id(item): deck_bands(item.shape, fonts) for item in items}
        design = {id(item): ink_bands(item.element) for item in items}   # type: ignore[arg-type]
        for position, first in enumerate(items):
            for second in items[position + 1:]:
                if nested_paths(first.element, second.element):   # type: ignore[arg-type]
                    continue
                shared = worst_shared_ink(bands[id(first)], bands[id(second)])
                if shared is None:
                    continue
                designed = worst_shared_ink(design[id(first)], design[id(second)]) or 0.0
                if shared - designed > OVERLAP_MIN_PX:
                    found.append({"slide": slide, "a": str(first.shape.name), "b": str(second.shape.name),
                                  "sharedPx": round(shared, 2), "designSharedPx": round(designed, 2)})
    return found


def export_overlaps(pptx: Path, irs: Sequence[IR]) -> list[dict[str, Any]]:
    """Text-box pairs whose ink overlaps more in the export than in the design.

    Entry: `{slide, a, b, sharedPx, designSharedPx}`. Rotated shapes and shapes with no IR element
    (a Path B deck, an unnamed placeholder) are skipped and counted; table cells are excluded.
    """
    return cast(list[dict[str, Any]], text_report(pptx, irs)["exportOverlaps"])


# ------------------------------------------------------------------------------------- the report


def text_report(pptx: Path, irs: Sequence[IR]) -> dict[str, Any]:
    """`CoverageReport.text`: every text check on the file, one dict.

    List-valued keys are findings (`collisions`, `exportOverlaps`; B adds `rows`, `positions`,
    `seams`): the row passes when every list is empty, and `reports.py`, the suite and the torture
    runner iterate them generically. `unmatched` counts text shapes with no IR element; `skipped`
    counts segments and shapes the rules could not judge (no IR line, a run-count mismatch, an
    absent face, a stretched justified line, a rotated shape).
    """
    pptx = Path(pptx)
    if not pptx.exists():
        return {"checked": False, "collisions": [], "exportOverlaps": [], "unmatched": 0, "skipped": 0}
    presentation = Presentation(str(pptx))
    counts = _Counts()
    shapes = list(_shapes(presentation, list(irs), counts))
    collisions = _collisions(shapes, counts)
    overlaps = _overlaps(presentation, shapes, list(irs), counts)
    return {"checked": True, "collisions": collisions, "exportOverlaps": overlaps,
            "unmatched": counts.unmatched, "skipped": counts.skipped, **side_by_side_report(pptx, irs)}


def line_residuals(irs: Sequence[IR]) -> list[dict[str, Any]]:
    """`measured − predicted` for every judgeable IR line — how `PREDICTION_SLACK` is measured.

    `joins` is the number of joins on the line; the slack is read off the join-free ones, whose
    residual is pure prediction error.
    """
    out: list[dict[str, Any]] = []
    for ir in irs:
        for element in ir.elements:
            if element.kind != "text":
                continue
            for p_index, paragraph in enumerate(element.paragraphs or []):
                for l_index, line in enumerate(paragraph.get("lines") or []):
                    runs = list(line.get("runs") or [])
                    if not runs or _stretched(element, p_index, l_index):
                        continue
                    predicted = predicted_width(runs, element)
                    measured = float((line.get("box") or {}).get("w") or 0.0)
                    if predicted is None or predicted <= 0 or measured <= 0:
                        continue
                    texts = [_run_text(run, element) for run in runs]
                    out.append({"slide": ir.slide.id, "element": element.id, "paragraph": p_index,
                                "line": l_index, "text": "".join(texts)[:40], "joins": len(_joins(texts)),
                                "measured": round(measured, 3), "predicted": round(predicted, 3),
                                "residual": round(measured - predicted, 3),
                                "ratio": round((measured - predicted) / predicted, 5)})
    return out


# ------------------------------------------------------------------------------ WP-B: side by side


def _line_text(line: dict[str, Any]) -> str:
    return "".join(str(run.get("text") or "") for run in line.get("runs") or [])


def _paragraph_quote(paragraph: dict[str, Any], limit: int = 40) -> str:
    lines = paragraph.get("lines") or []
    text = _line_text(lines[0]).strip() if lines else ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def text_rows(irs: Iterable[IR]) -> list[dict[str, Any]]:
    """Text elements with two paragraphs side by side — a row the export would stack (V2).

    Two paragraphs of one element sit side by side when their first line boxes share a vertical band
    — an overlap of more than `config.TEXT_ROW_SHARE` of the shorter line box — and are x-disjoint
    (`x2 <= other.x + 1`). This is the survey's rule (`survey_slides.side_by_side`); H's
    `metrics.side_by_side` delegates here, so the two agree by construction. It is consistent with
    the extractor's stacking rule (`page.js` R1, `STACK_TOL`): R1 lets adjacent line boxes overlap by
    at most that share, this flags strictly more.

    One entry per element, naming its first offending pair: `{slide, element, name, left, right,
    pairs}`.
    """
    found: list[dict[str, Any]] = []
    for ir in irs:
        for element in ir.elements:
            if element.kind != "text":
                continue
            firsts = [p for p in element.paragraphs or [] if p.get("lines")]
            pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for i, a in enumerate(firsts):
                for b in firsts[i + 1:]:
                    la, lb = a["lines"][0]["box"], b["lines"][0]["box"]
                    overlap = min(la["y"] + la["h"], lb["y"] + lb["h"]) - max(la["y"], lb["y"])
                    shorter = min(la["h"], lb["h"])
                    disjoint = la["x"] + la["w"] <= lb["x"] + 1 or lb["x"] + lb["w"] <= la["x"] + 1
                    if shorter > 0 and overlap > config.TEXT_ROW_SHARE * shorter and disjoint:
                        pairs.append((a, b) if la["x"] <= lb["x"] else (b, a))
            if pairs:
                left, right = pairs[0]
                found.append({
                    "slide": ir.slide.id, "element": element.id, "name": element.name,
                    "left": _paragraph_quote(left), "right": _paragraph_quote(right), "pairs": len(pairs),
                })
    return found


#: The message family `page.js` R6 writes, one per text element with a glued join.
GLUED_RUNS_PREFIX = "glued runs:"


def text_seams(irs: Iterable[IR]) -> list[dict[str, Any]]:
    """`glued runs:` warnings on text elements — joins the browser made by layout, not by a space.

    The extractor records them where the fragment rects are still at hand (`page.js` R6: two
    fragments of one line at least `SEAM_GAP_PX` apart with no whitespace between); a PowerPoint
    paragraph has no geometry between runs, so the export cannot keep that gap.
    """
    found: list[dict[str, Any]] = []
    for ir in irs:
        texts = {element.id for element in ir.elements if element.kind == "text"}
        for diagnostic in ir.diagnostics:
            if diagnostic.level != "warn" or not diagnostic.message.startswith(GLUED_RUNS_PREFIX):
                continue
            if diagnostic.elementId not in texts:
                continue
            found.append({"slide": ir.slide.id, "element": diagnostic.elementId, "message": diagnostic.message})
    return found


# ------------------------------------------------------------------ V3: where each paragraph starts

_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_EMU = float(config.EMU_PER_PX)

_ALGN = {"l": "left", "ctr": "center", "r": "right", "just": "justify", "dist": "justify"}


def _emu_px(value: Any) -> float:
    return (float(value) if value is not None else 0.0) / _EMU


def _xfrm(node: Any) -> Any:
    """The `a:xfrm` of a `p:sp` (`p:spPr/a:xfrm`) or a `p:grpSp` (`p:grpSpPr/a:xfrm`)."""
    for props in (f"{{{_P}}}spPr", f"{{{_P}}}grpSpPr"):
        holder = node.find(props)
        if holder is not None:
            found = holder.find(f"{{{_A}}}xfrm")
            if found is not None:
                return found
    return None


def _placed(node: Any, mapping: tuple[float, float, float, float]) -> tuple[Box, float] | None:
    """`(box, rotation°)` of a shape on the slide, its child coordinates mapped through its groups."""
    xfrm = _xfrm(node)
    if xfrm is None:
        return None
    off, ext = xfrm.find(f"{{{_A}}}off"), xfrm.find(f"{{{_A}}}ext")
    if off is None or ext is None:
        return None
    sx, sy, dx, dy = mapping
    x, y = _emu_px(off.get("x")) * sx + dx, _emu_px(off.get("y")) * sy + dy
    w, h = _emu_px(ext.get("cx")) * sx, _emu_px(ext.get("cy")) * sy
    return Box(x, y, w, h), float(xfrm.get("rot") or 0) / 60000.0


def _group_mapping(group: Any, mapping: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Child → slide coordinates of a `p:grpSp`: `off/ext` against `chOff/chExt`, then the parent's."""
    xfrm = _xfrm(group)
    if xfrm is None:
        return mapping
    parts = {tag: xfrm.find(f"{{{_A}}}{tag}") for tag in ("off", "ext", "chOff", "chExt")}
    if any(part is None for part in parts.values()):
        return mapping
    ext_w, ext_h = _emu_px(parts["ext"].get("cx")), _emu_px(parts["ext"].get("cy"))
    ch_w, ch_h = _emu_px(parts["chExt"].get("cx")), _emu_px(parts["chExt"].get("cy"))
    scale_x = ext_w / ch_w if ch_w else 1.0
    scale_y = ext_h / ch_h if ch_h else 1.0
    offset_x = _emu_px(parts["off"].get("x")) - _emu_px(parts["chOff"].get("x")) * scale_x
    offset_y = _emu_px(parts["off"].get("y")) - _emu_px(parts["chOff"].get("y")) * scale_y
    sx, sy, dx, dy = mapping
    return (scale_x * sx, scale_y * sy, offset_x * sx + dx, offset_y * sy + dy)


def _text_shapes(
    tree: Any, mapping: tuple[float, float, float, float] = (1.0, 1.0, 0.0, 0.0)
) -> Iterator[tuple[Any, Any, Any, float]]:
    """Every `p:sp` with a text body under a shape tree, as `(sp, box, rotation, scale_x)`."""
    for child in tree:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "grpSp":
            yield from _text_shapes(child, _group_mapping(child, mapping))
        elif tag == "sp" and child.find(f"{{{_P}}}txBody") is not None:
            placed = _placed(child, mapping)
            if placed is not None:
                yield child, placed[0], placed[1], mapping[0]


def _shape_name(sp: Any) -> str:
    props = sp.find(f"{{{_P}}}nvSpPr/{{{_P}}}cNvPr")
    return "" if props is None else str(props.get("name") or "")


def _insets(sp: Any, scale_x: float) -> tuple[float, float]:
    """`(left, right)` text insets in slide px — `bodyPr@lIns/rIns`, PowerPoint's 0.1 in when absent."""
    body = sp.find(f"{{{_P}}}txBody/{{{_A}}}bodyPr")
    default = 0.1 * config.PX_PER_IN * _EMU
    left = float(body.get("lIns")) if body is not None and body.get("lIns") is not None else default
    right = float(body.get("rIns")) if body is not None and body.get("rIns") is not None else default
    return left / _EMU * scale_x, right / _EMU * scale_x


def _has_text(sp: Any) -> bool:
    return any((t.text or "").strip() for t in sp.iter(f"{{{_A}}}t"))


def _iou(a: Box, b: Box) -> float:
    overlap = a.intersect(b).area
    union = a.area + b.area - overlap
    return overlap / union if union > 0 else 0.0


def _match(box: Box, name: str, candidates: list[Element], used: set[int]) -> Element | None:
    """The IR element a text shape came from: by name — the best IoU among same-name elements, since a
    chip's shape and text, or the two segments of one paragraph, share a name — then by IoU ≥ 0.4."""
    named = [(i, e) for i, e in enumerate(candidates) if i not in used and name and name in {e.name, e.id}]
    pool = named or [(i, e) for i, e in enumerate(candidates) if i not in used]
    best_index, best_score = -1, -1.0
    for index, element in pool:
        score = _iou(box, element.box)
        if score > best_score:
            best_index, best_score = index, score
    if best_index < 0 or (not named and best_score < 0.4):
        return None
    used.add(best_index)
    return candidates[best_index]


def _emitted_paragraphs(element: Element) -> list[dict[str, Any]]:
    """The IR paragraphs the emitter writes (`text.plan_text` skips one with no line carrying runs)."""
    return [p for p in element.paragraphs or [] if any(line.get("runs") for line in p.get("lines") or [])]


def text_positions(pptx: Path, irs: list[IR]) -> list[dict[str, Any]]:
    """Paragraphs whose first line would not start where the browser put it (V3, Path A only).

    For every unrotated text shape matched to an unrotated, horizontal IR text element that carries
    `extras["x-run-boxes"]` (i.e. was measured by Path A's extractor — a Path B deck has no IR to
    compare with), each written `a:p` is paired, in order, with the IR paragraph the emitter wrote it
    from. The start PowerPoint will give its first line follows from the shape box, the `bodyPr`
    insets and the paragraph's `marL`/`marR`/`indent` (absent → 0):

    * left / justify: `left + inset + marL + indent`
    * right: `right − inset − marR`
    * center: the centre of `[left + inset + marL, right − inset − marR]`
    * bullet: `left + inset + marL` (the text; the bullet hangs at `marL + indent`)

    — compared with the IR's first line box (`x`, `x2` or its centre) within
    `config.TEXT_POSITION_TOL_PX`. Entry: `{slide, shape, element, paragraph, align, expected,
    measured}`. Table cells are not text shapes and are not checked (package A's).
    """
    import zipfile

    from defusedxml.ElementTree import fromstring

    found: list[dict[str, Any]] = []
    pptx = Path(pptx)
    if not pptx.exists():
        return found
    tolerance = config.TEXT_POSITION_TOL_PX
    with zipfile.ZipFile(pptx) as archive:
        names = sorted(
            (n for n in archive.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")),
            key=lambda n: int("".join(c for c in n.rsplit("/", 1)[-1] if c.isdigit()) or 0),
        )
        for index, name in enumerate(names):
            if index >= len(irs):
                break
            ir = irs[index]
            candidates = [
                e for e in ir.elements
                if e.kind == "text" and not e.rotation and (e.writingMode or "horizontal") == "horizontal"
                and "x-run-boxes" in (e.extras or {})
            ]
            if not candidates:
                continue
            tree = fromstring(archive.read(name)).find(f"{{{_P}}}cSld/{{{_P}}}spTree")
            if tree is None:
                continue
            used: set[int] = set()
            for sp, box, rotation, scale_x in _text_shapes(tree):
                if rotation or not _has_text(sp):
                    continue
                shape_name = _shape_name(sp)
                element = _match(box, shape_name, candidates, used)
                if element is None:
                    continue
                inset_l, inset_r = _insets(sp, scale_x)
                written = sp.findall(f"{{{_P}}}txBody/{{{_A}}}p")
                source = _emitted_paragraphs(element)
                for number, (paragraph, ir_paragraph) in enumerate(zip(written, source, strict=False)):
                    first = next((line for line in ir_paragraph.get("lines") or [] if line.get("runs")), None)
                    if first is None:
                        continue
                    pPr = paragraph.find(f"{{{_A}}}pPr")
                    def get(key: str, pPr: Any = pPr) -> Any:
                        return pPr.get(key) if pPr is not None else None

                    mar_l, mar_r = _emu_px(get("marL")) * scale_x, _emu_px(get("marR")) * scale_x
                    indent = _emu_px(get("indent")) * scale_x
                    align = _ALGN.get(str(get("algn") or ""), str(ir_paragraph.get("align") or "left"))
                    bulleted = pPr is not None and any(
                        pPr.find(f"{{{_A}}}{tag}") is not None for tag in ("buChar", "buAutoNum", "buBlip")
                    )
                    line = first["box"]
                    left_edge, right_edge = box.x + inset_l + mar_l, box.x2 - inset_r - mar_r
                    if bulleted and align in ("left", "justify"):
                        align, expected, measured = "bullet", left_edge, line["x"]
                    elif align == "right":
                        expected, measured = right_edge, line["x"] + line["w"]
                    elif align == "center":
                        expected, measured = (left_edge + right_edge) / 2.0, line["x"] + line["w"] / 2.0
                    else:
                        expected, measured = left_edge + indent, line["x"]
                    if abs(expected - measured) > tolerance:
                        found.append({
                            "slide": ir.slide.id, "shape": shape_name, "element": element.id,
                            "paragraph": number, "align": align,
                            "expected": round(expected, 2), "measured": round(measured, 2),
                        })
    return found


def side_by_side_report(pptx: Path, irs: Sequence[IR]) -> dict[str, list[dict[str, Any]]]:
    """WP-B's three keys of `text_report` (plan D6, §16 #8): `rows` (V2), `positions` (V3) and
    `seams`. `positions` judges only elements that carry `extras["x-run-boxes"]` — Path A's
    extraction — so the row needs no `path` argument; a Path B deck has nothing it can judge."""
    return {"rows": text_rows(irs), "positions": text_positions(Path(pptx), list(irs)), "seams": text_seams(irs)}
