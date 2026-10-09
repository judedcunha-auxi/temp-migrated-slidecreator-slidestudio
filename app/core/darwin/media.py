"""What `/api/image`, `/api/pdf` and `/api/pdf-deck` read: a slide version's picture, the tile composite,
and the PDFs (`image.ts`, `pdf.ts`, `pdf-deck.ts`).

Darwin read `deck-images/deck/<deckId>/slide_<n>.png` (v1) or `slide_<n>_v<v>.png` (a refinement), with a
fallback to the pre-variant-removal `..._variant_A...` keys. Here every picture is a slide version's
`image_ref` (the migration, D29, carries the old keys over as versions), so `slide_picture` is a lookup of
the version: `v` >= 2 reads that version, anything else reads v1, exactly the keys Darwin derived.

`tile_preview` is `compositeIfTile`: a slide rendered as a content-only workzone tile is served composited on
the brand's Layout master, best effort (any problem serves the raw tile).
"""

from __future__ import annotations

import asyncio
import logging
import re

from app.core.brand.workzone import is_degenerate
from app.core.darwin import compose
from app.core.darwin.deck_state import image_ref_for
from app.core.darwin.generate import read_brand_asset
from app.core.darwin.js import js_trim
from app.core.darwin.prompt import prompt_kit, resolve_brand_source
from app.core.storage.models import CallerContext, Deck, ExportCreate, Slide
from app.core.storage.ports import NotFound, Storage

_log = logging.getLogger(__name__)

_UNSAFE_FILENAME = re.compile(r"[^a-zA-Z0-9\s\ufeff-]")


async def read_slide(storage: Storage, ctx: CallerContext, deck_id: str, number: int) -> Slide | None:
    try:
        return await storage.slides.get(ctx, deck_id, number)
    except NotFound:
        return None


async def slide_picture(storage: Storage, ctx: CallerContext, slide: Slide | None, version: int) -> bytes | None:
    """The stored PNG of `version` (>= 2: that refinement; else the original v1), or None."""
    if slide is None:
        return None
    ref = image_ref_for(slide, version if version >= 2 else 1)
    if ref is None:
        return None
    try:
        return (await storage.blobs.get(ctx, ref)).data
    except NotFound:
        return None


async def tile_preview(storage: Storage, ctx: CallerContext, deck: Deck, slide: Slide, tile: bytes) -> bytes:
    """`compositeIfTile`: the tile on the brand master when the slide is a tile of a brand the caller can read
    with a usable workzone and a Layout master; the raw tile otherwise, or on any failure."""
    try:
        brand_id = deck.inputs.get("brandId")
        if not slide.is_tile or not isinstance(brand_id, str) or not brand_id:
            return tile
        source = await resolve_brand_source(storage, ctx, brand_id)
        kit = prompt_kit(source.kit)
        if kit.workzone is None or is_degenerate(kit.workzone) or not kit.base.master_key:
            return tile
        master = await read_brand_asset(storage, ctx, source.brand_id, "master")
        if not master:
            return tile
        return await asyncio.to_thread(compose.composite_tile, master, tile, kit.workzone)
    except Exception:  # noqa: BLE001 - "tile composite failed, returning raw"
        _log.exception("image: the tile composite failed; serving the raw tile")
        return tile


def deck_pdf_filename(title: str | None) -> str:
    """pdf-deck.ts: the title without anything but ASCII letters, digits, whitespace and hyphens, or "deck"."""
    safe = js_trim(_UNSAFE_FILENAME.sub("", title or ""))
    return f"{safe or 'deck'}.pdf"


async def count_pdf_export(storage: Storage, ctx: CallerContext, deck_id: str, slides: list[int]) -> None:
    """`void incrementExportCounts(userId, deckId)`: never fails the download."""
    try:
        await storage.exports.record(ctx, ExportCreate(deck_id=deck_id, kind="pdf", slide_numbers=slides))
    except Exception:  # noqa: BLE001
        _log.exception("pdf: the export counter write failed")
