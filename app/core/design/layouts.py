"""The canvas, a blank slide, and layouts named the way the person sees them.

Ported from Slide Studio `server/chat/layouts.py` (migration plan §4.1, K). Slides reference the
deck's images as `/api/projects/<deck id>/assets/<file>`: the engine's asset shape (extraction,
preview and the static linter all resolve it from the deck's `assets/` folder), so no HTTP route is
involved and the authoring contract is unchanged.
"""

from __future__ import annotations

import re
from typing import Any

JSON = dict[str, Any]


def assets_url(deck_id: str) -> str:
    """The URL prefix slides reference the deck's images by (the engine's asset shape)."""
    return f"/api/projects/{deck_id}/assets"


def blank_slide_html(w: int, h: int) -> str:
    return (f"<!doctype html><html><head><meta charset=\"utf-8\"><style>html,body{{margin:0;width:{w}px;"
            f"height:{h}px;overflow:hidden;background:transparent}}</style></head><body></body></html>")


def canvas(meta: JSON) -> tuple[int, int]:
    c = (meta.get("master") or {}).get("canvas") or {"w": 1280, "h": 720}
    return int(c["w"]), int(c["h"])


def zone_line(zone: JSON) -> str:
    """One placeholder zone: its box, and the type it is set in. The designer designs to both."""
    box = (f"{zone.get('type')} ({round(zone.get('x', 0))},{round(zone.get('y', 0))} "
           f"{round(zone.get('w', 0))}×{round(zone.get('h', 0))})")
    style = zone.get("style") or {}
    bits: list[str] = []
    if style.get("sizePt"):
        bits.append(f"{style['sizePt']:g}pt")
    font = style.get("font")
    if font and font not in ("+mj-lt", "+mn-lt"):
        bits.append(str(font))
    elif font:
        bits.append("heading font" if font == "+mj-lt" else "body font")
    if style.get("bold"):
        bits.append("bold")
    if style.get("color"):
        bits.append(str(style["color"]))
    if style.get("align") and style["align"] != "left":
        bits.append(str(style["align"]))
    return f"{box} {' '.join(bits)}".strip() if bits else box


#: A layout id in a reply's prose: `layout-05`. A file name (`layout-01.png`) is not one.
LAYOUT_ID = re.compile(r"\blayout-(\d+)\b(?!\.\w)", re.IGNORECASE)


def layout_label(meta: JSON, layout_id: str | None) -> str | None:
    """`“Title only” (Layout 5)`: how a layout is named to the person.

    None when the master has layouts and this id is not one of them; the id itself when there is no
    master. The number tells apart layouts of one master that share a name.
    """
    layouts = (meta.get("master") or {}).get("layouts") or []
    layout = next((lay for lay in layouts if lay.get("id") == layout_id), None)
    if layout is None:
        return None if layouts else layout_id
    number = re.search(r"(\d+)\s*$", layout_id or "")
    suffix = f" (Layout {int(number.group(1))})" if number else ""
    return f"“{layout.get('name') or layout_id}”{suffix}"


def layout_labels(meta: JSON) -> dict[str, str]:
    """id -> label for every layout of the master, keyed lower-case."""
    labels: dict[str, str] = {}
    for lay in (meta.get("master") or {}).get("layouts") or []:
        lid = lay.get("id")
        label = layout_label(meta, lid) if lid else None
        if lid and label:
            labels[str(lid).lower()] = label
    return labels


def speak_of_layouts(text: str, labels: dict[str, str]) -> str:
    """Replace each `layout-NN` that names a known layout with its label; leave the rest alone."""
    return LAYOUT_ID.sub(lambda m: labels.get(m.group(0).lower(), m.group(0)), text)
