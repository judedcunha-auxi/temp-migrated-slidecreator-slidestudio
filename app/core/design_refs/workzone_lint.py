"""The workzone and header-band lint: content must stay inside the brand's workzone.

The known gap this closes (migration plan §3, Phase 3 fixes): a slide designed for a branded master ran
past the workzone and painted over the header band, where the brand's header and logo live. The
instruction (`app.core.brand.workzone.design_note`) asks the designer to stay inside; this module
checks that it did, on the same measured geometry the export uses.

Two rules, both errors (a partner would send the slide back):

* `workzone-overflow`: a visible element other than the title leaves the workzone rectangle by more
  than the tolerance. The message names the element, the edge and by how many px.
* `header-band-overlap`: a visible element other than the title intersects the header band, the strip
  above the workzone (`Workzone.header_band_px`).

The title is exempt from both: it belongs in the master's title placeholder, which sits in the header
band by design. Invisible elements (a transparent shape with no outline, zero area, zero opacity) are
not judged: a layout wrapper that is never painted cannot paint over anything.

`workzone_findings` works on a measured IR (`app.engine.ir.IR`, from `design_lint.design_findings_html`
or an export). `workzone_findings_static` is the browser-free fallback: it reads the inline
`left/top/width/height` px of absolutely positioned top-level blocks from the HTML and reports the same
rules where a box is fully declared. Both return findings in `design_lint`'s dict shape
(`level`, `rule`, `message`, `element`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from app.core.brand.workzone import Workzone

#: Title placeholder types (as `map_placeholders` and authored `data-placeholder` name them).
TITLE_TYPES = frozenset({"title", "ctrTitle"})
#: Default slack before an edge counts as outside (sub-pixel borders, anti-aliasing).
TOLERANCE_PX = 2.0

Rect = tuple[float, float, float, float]  # x, y, w, h


def _finding(rule: str, message: str, element: str | None) -> dict[str, Any]:
    return {"level": "error", "rule": rule, "message": message, "element": element}


def _px(value: float) -> str:
    return f"{value:.0f}"


def _paragraph_text(paragraphs: Iterable[dict[str, Any]] | None) -> str:
    words: list[str] = []
    for paragraph in paragraphs or []:
        for line in paragraph.get("lines") or []:
            for run in line.get("runs") or []:
                words.append(str(run.get("text") or ""))
        for run in paragraph.get("runs") or []:
            words.append(str(run.get("text") or ""))
    return " ".join(" ".join(words).split())


def _label(element: Any) -> str:
    """How a message names an element: its visible text (first words), else its data-name, else its kind."""
    text = _paragraph_text(getattr(element, "paragraphs", None))
    if text:
        return f'"{text[:40]}…"' if len(text) > 40 else f'"{text}"'
    name = getattr(element, "name", None)
    if name:
        return f"[{name}]"
    return f"the {getattr(element, 'kind', 'element')} at {_px(element.box.x)},{_px(element.box.y)}"


def _is_title(element: Any, raw_placeholders: dict[str, str] | None) -> bool:
    placeholder = getattr(element, "placeholder", None)
    if isinstance(placeholder, dict) and placeholder.get("type") in TITLE_TYPES:
        return True
    return bool(raw_placeholders and raw_placeholders.get(str(getattr(element, "id", ""))) in TITLE_TYPES)


def _visible(element: Any) -> bool:
    box = element.box
    if box.w <= 0 or box.h <= 0 or float(getattr(element, "opacity", 1.0) or 0.0) <= 0:
        return False
    if element.kind == "shape":
        fill = element.fill or {}
        stroke = element.stroke or {}
        painted_fill = bool(fill) and fill.get("type") != "none" and float(fill.get("alpha", 1.0) or 0.0) > 0
        painted_stroke = bool(stroke) and float(stroke.get("width", 0) or 0) > 0
        return painted_fill or painted_stroke
    if element.kind == "text":
        return bool(_paragraph_text(element.paragraphs))
    return True


def _overflow(box: Rect, zone: Rect, tolerance: float) -> list[str]:
    """Each edge by which `box` leaves `zone`, past the tolerance: ["left by 12 px", ...]."""
    x, y, w, h = box
    zx, zy, zw, zh = zone
    edges = (("left", zx - x), ("top", zy - y), ("right", (x + w) - (zx + zw)), ("bottom", (y + h) - (zy + zh)))
    return [f"{edge} by {_px(by)} px" for edge, by in edges if by > tolerance]


def _intersects(box: Rect, band: Rect, tolerance: float) -> float:
    """How far `box` reaches into `band` vertically (0 when it does not)."""
    x, y, w, h = box
    bx, by, bw, bh = band
    if x + w <= bx + tolerance or x >= bx + bw - tolerance:
        return 0.0
    depth = (by + bh) - y
    return depth if depth > tolerance and y + h > by else 0.0


def _check(label: str, box: Rect, zone: Rect, band: Rect | None, tolerance: float,
           element_name: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    outside = _overflow(box, zone, tolerance)
    if outside:
        zx, zy, zw, zh = zone
        out.append(_finding(
            "workzone-overflow",
            f"{label} leaves the brand's workzone ({_px(zx)},{_px(zy)} {_px(zw)}×{_px(zh)} px) on the "
            f"{', '.join(outside)}. Move or shrink it so it sits wholly inside the workzone; the master's "
            f"header, logo and footer own the rest of the slide.", element_name))
    if band is not None:
        depth = _intersects(box, band, tolerance)
        if depth:
            out.append(_finding(
                "header-band-overlap",
                f"{label} reaches {_px(depth)} px into the header band (y < {_px(band[3])} px), where the "
                f"brand's header and logo are drawn. Only the title may sit there: move it below "
                f"y = {_px(band[3])} px.", element_name))
    return out


def _zone_and_band(workzone: Workzone, canvas_w: int, canvas_h: int) -> tuple[Rect, Rect | None]:
    zone: Rect = tuple(float(v) for v in workzone.px_rect(canvas_w, canvas_h))  # type: ignore[assignment]
    band_px = workzone.header_band_px(canvas_w, canvas_h)
    band: Rect | None = tuple(float(v) for v in band_px) if band_px else None  # type: ignore[assignment]
    return zone, band


def workzone_findings(ir: Any, workzone: Workzone, canvas_w: int, canvas_h: int, *,
                      tolerance_px: float = TOLERANCE_PX,
                      raw_placeholders: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Workzone and header-band findings for a measured slide (`app.engine.ir.IR`).

    `raw_placeholders` (element id -> authored placeholder type, from `design_lint`) lets an authored
    `data-placeholder="title"` count as the title even when classification did not map it.
    """
    zone, band = _zone_and_band(workzone, canvas_w, canvas_h)
    out: list[dict[str, Any]] = []
    for element in getattr(ir, "elements", None) or []:
        if _is_title(element, raw_placeholders) or not _visible(element):
            continue
        box = element.box
        out.extend(_check(_label(element), (box.x, box.y, box.w, box.h), zone, band, tolerance_px,
                          getattr(element, "name", None) or _label(element)))
    return out


# ------------------------------------------------------------------------------------- static


_PX = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(?:px)?\s*$")


def _style(value: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (value or "").split(";"):
        if ":" in part:
            key, _, val = part.partition(":")
            out[key.strip().lower()] = val.strip().lower()
    return out


def _number(value: str | None) -> float | None:
    match = _PX.match(value or "")
    return float(match.group(1)) if match else None


def _static_text(node: Any) -> str:
    return " ".join("".join(node.itertext()).split())


def workzone_findings_static(html: str, workzone: Workzone, canvas_w: int, canvas_h: int, *,
                             tolerance_px: float = TOLERANCE_PX) -> list[dict[str, Any]]:
    """The same rules without a browser, from inline styles of absolutely positioned top-level blocks.

    Only boxes whose `left`, `top`, `width` and `height` are all declared in px are judged; anything
    else needs the measured check (`workzone_findings`). The title (`data-placeholder="title"`, or an
    `h1` that names no other placeholder) is exempt, as is a block with no text and no background.
    """
    import html5lib  # noqa: PLC0415 — a dependency of the engine's linter already

    root = html5lib.parse(html, treebuilder="etree", namespaceHTMLElements=False)
    body = root.find(".//body")
    if body is None:
        return []
    zone, band = _zone_and_band(workzone, canvas_w, canvas_h)
    out: list[dict[str, Any]] = []
    for node in list(body):
        tag = str(node.tag).lower()
        if tag in ("script", "style", "aside"):
            continue
        placeholder = (node.get("data-placeholder") or "").strip()
        if placeholder in TITLE_TYPES or (tag == "h1" and not placeholder):
            continue
        style = _style(node.get("style"))
        if style.get("position") not in ("absolute", "fixed"):
            continue
        values = [_number(style.get(k)) for k in ("left", "top", "width", "height")]
        if any(v is None for v in values):
            continue
        x, y, w, h = (float(v) for v in values if v is not None)
        text = _static_text(node)
        painted = any(k in style for k in ("background", "background-color", "border")) or node.find(".//img") is not None
        if not text and not painted and node.find(".//svg") is None:
            continue
        label = f'"{text[:40]}"' if text else f"the {tag} block at {_px(x)},{_px(y)}"
        out.extend(_check(label, (x, y, w, h), zone, band, tolerance_px, node.get("data-name") or label))
    return out
