"""The archetype -> layout mapping of Darwin's PPTX brand import: furniture re-keying and layout previews.

Ported from `netlify/functions/_shared/furniture.ts` and `_shared/layoutPreviews.ts`. Used by the
brand-pptx import job (which stores the furniture and the layout PNGs), `/api/brand-archetypes`
(which re-keys the furniture to the user's cover/divider/content choice) and `/api/brand-heading`.

`capturedFurniture` is the brand extractor's opaque blob (app/core/brand/extract.py). Only its
`layouts`, `layoutsByIndex`, `background.layouts(ByIndex)`, `media` and the heading placeholders are
read here; everything else passes through untouched.

The routes that use this are kept for the contract and are deprecation candidates once the UI stops
calling them (plan §6.5, D33).
"""

from __future__ import annotations

import re
from typing import Any

from app.core.brand.kit import ARCHETYPES, normalize_heading_placeholder
from app.core.darwin.js import js_is_integer

JSON = dict[str, Any]


def _obj(value: Any) -> JSON:
    """`value` if it is a JSON object, else {} (the `?? {}` / optional-chaining reads)."""
    return value if isinstance(value, dict) else {}


# ------------------------------------------------------------------------------------- furniture
def _media_refs(value: Any, out: set[str]) -> set[str]:
    """Every media sha1 an `rIdMap` anywhere inside `value` points at (`mediaRefs`)."""
    if isinstance(value, list):
        for item in value:
            _media_refs(item, out)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key == "rIdMap" and isinstance(item, dict):
                out.update(v for v in item.values() if isinstance(v, str))
            else:
                _media_refs(item, out)
    return out


def _finalize(furniture: JSON) -> JSON:
    """Drop the per-layout capture and keep only the media still referenced (`finalize`): this is the
    furniture sent inline with every export, so it stays small."""
    rest = {k: v for k, v in furniture.items() if k != "layoutsByIndex"}
    background = rest.get("background")
    if isinstance(background, dict):
        rest["background"] = {k: v for k, v in background.items() if k != "layoutsByIndex"}
    media = rest.get("media")
    if isinstance(media, dict):
        used = _media_refs({k: v for k, v in rest.items() if k != "media"}, set())
        rest["media"] = {sha1: blob for sha1, blob in media.items() if sha1 in used}
    return rest


def default_export_furniture(captured: JSON) -> JSON:
    """`defaultExportFurniture`: the extractor's own archetype picks, without the per-layout capture.
    A capture without `layoutsByIndex` is returned as is."""
    return _finalize(captured) if captured.get("layoutsByIndex") else captured


def furniture_for_archetypes(captured: Any, mapping: dict[str, int]) -> JSON | None:
    """`furnitureForArchetypes`: the export furniture for the user's own cover/divider/content choice,
    re-keyed out of the per-layout capture. None when there is no per-layout capture (the caller keeps
    the furniture it has). An index the capture skipped leaves that archetype out.

    Raises TypeError for a capture that is not an object (`JSON.parse` gave null or a primitive): the
    route answers 500, as Darwin's `null.layoutsByIndex` did."""
    if not isinstance(captured, dict):
        if captured is None:
            raise TypeError("the stored per-layout furniture is not an object")
        return None
    by_index = captured.get("layoutsByIndex")
    if not by_index:
        return None
    background = _obj(captured.get("background"))
    bg_by_index = _obj(background.get("layoutsByIndex"))
    layouts: JSON = {}
    bg_layouts: JSON = {}
    for arch in ARCHETYPES:
        entry = by_index.get(str(mapping[arch])) if isinstance(by_index, dict) else None
        if not entry:
            continue
        placeholders = entry.get("placeholders") if isinstance(entry, dict) else None
        # The per-layout capture records cover rules (heading + date) for every layout; only a cover
        # uses the date slot.
        if arch != "cover" and isinstance(placeholders, dict):
            placeholders = {"heading": placeholders.get("heading")}
        layouts[arch] = {**entry, "placeholders": placeholders} if isinstance(entry, dict) else entry
        bg = bg_by_index.get(str(mapping[arch]))
        if bg:
            bg_layouts[arch] = bg
    return _finalize({**captured, "layouts": layouts, "background": {**background, "layouts": bg_layouts}})


def furniture_summary(furniture: JSON) -> tuple[list[str], dict[str, JSON]]:
    """`furnitureSummary`: the archetypes that carry furniture, and their heading placeholders (what the
    kit keeps as furnitureArchetypes / headingPlaceholders for the in-app preview)."""
    layouts = _obj(furniture.get("layouts"))
    archetypes = [a for a in ARCHETYPES if layouts.get(a)]
    headings: dict[str, JSON] = {}
    for arch in archetypes:
        entry = layouts.get(arch)
        placeholders = entry.get("placeholders") if isinstance(entry, dict) else None
        ph = normalize_heading_placeholder(placeholders.get("heading") if isinstance(placeholders, dict) else None)
        if ph is not None:
            headings[arch] = ph
    return archetypes, headings


def parse_archetype_layouts(raw: Any) -> dict[str, int] | None:
    """`parseArchetypeLayouts`: `{cover, divider, content}` each a non-negative integer; extra keys
    ignored; None for anything else."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, int] = {}
    for arch in ARCHETYPES:
        value = raw.get(arch)
        if not isinstance(value, (int, float)) or not js_is_integer(value) or value < 0:
            return None
        out[arch] = int(value)
    return out


# -------------------------------------------------------------------------------- layout previews
_LAYOUT_PNG = re.compile(r"^layout-(\d+)[-_.]?(.*)\.png$", re.IGNORECASE)


def layout_slug(name: str) -> str:
    """`layoutSlug`: lower case, runs of anything but [a-z0-9] to one '-', trimmed; 'layout' if empty."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "layout"


def slug_to_name(slug: str) -> str:
    """`slugToName`: a filename slug as a title ("title-slide" -> "Title Slide")."""
    text = re.sub(r"[-_]+", " ", re.sub(r"\.png$", "", slug, flags=re.IGNORECASE)).strip()
    return re.sub(r"\b\w", lambda m: m.group(0).upper(), text) if text else ""


def rendered_layouts(entries: dict[str, bytes]) -> list[tuple[int, str, bytes]]:
    """The renderer's `layout-NN-<slug>.png` files as (renderIndex 0-based, slug, png), in file order.
    Filenames are 1-based; anything that is not a layout PNG is ignored (`renderLayouts`)."""
    out: list[tuple[int, str, bytes]] = []
    for name in sorted(entries, key=_natural):
        match = _LAYOUT_PNG.match(name.rsplit("/", 1)[-1])
        if not match:
            continue
        index = int(match.group(1)) - 1
        if index < 0:
            continue
        out.append((index, match.group(2), entries[name]))
    return out


def _natural(name: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def align_previews_to_layouts(render_indices: list[tuple[int, str]],
                              layouts: list[Any]) -> dict[int, int] | None:
    """`alignPreviewsToLayouts`: renderIndex -> the extractor's layout index, matched by slug (same-named
    layouts pair up in order). None unless EVERY rendered layout matches: a partial remap could put two
    previews on one index, so the caller keeps the renderer's numbering instead."""
    by_slug: dict[str, list[int]] = {}
    for layout in layouts:
        if not isinstance(layout, dict) or not isinstance(layout.get("name"), str) \
                or not js_is_integer(layout.get("index")):
            return None
        by_slug.setdefault(layout_slug(layout["name"]), []).append(int(layout["index"]))
    out: dict[int, int] = {}
    for render_index, slug in sorted(render_indices):
        candidates = by_slug.get(layout_slug(slug))
        if not candidates:
            return None
        out[render_index] = candidates.pop(0)
    return out
