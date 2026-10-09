"""SlideForge service: api/routes/media. Darwin's `/api/image`, `/api/pdf`, `/api/pdf-deck`.

Same path, method, request and response as `netlify/functions/{image,pdf,pdf-deck}.ts`; contracts
`tests/contract/data/api-{image,pdf,pdf-deck}.json`. No method is checked (any method acts as GET) and the
body is never read. sharp and pdf-lib are replaced by Pillow and a small PDF writer
(`app/core/darwin/compose.py`).

| Route | Answer | Quirks kept |
|---|---|---|
| `GET /api/image?deckId=&slide=&v=` | `image/png`, `cache-control: private, max-age=3600` | `slide` is `Number(...)` (`"1.0"`, `" 2"` pass); `v` >= 2 reads that refinement, anything else the original; a tile slide is composited on the brand master (best effort, 2560x1440) |
| `GET /api/pdf?deckId=&slide=&v=` | `application/pdf`, `attachment; filename="slide_<n>.pdf"`, NO cache-control (every download is counted) | the raw PNG, never composited |
| `GET /api/pdf-deck?deckId=` | `application/pdf`, `attachment; filename="<title>.pdf"` | one page per done slide at its current version; a missing picture is skipped silently |
"""

from __future__ import annotations

import asyncio

from fastapi import Request
from fastapi.responses import Response

from app.api.deps import get_storage, require_user
from app.api.legacy import darwin_route, legacy_router, postgres_uuid, query_param, required_query
from app.core.darwin import compose, media
from app.core.darwin.deck_state import current_version
from app.core.darwin.exports import owned_deck
from app.core.darwin.js import is_js_integer, js_number
from app.core.errors import ApiError

router = legacy_router(prefix="/api", tags=["darwin-media"])

BAD_PARAMS = "Bad params"
IMAGE_NOT_FOUND = "Image not found"
PNG = "image/png"
PDF = "application/pdf"
IMAGE_CACHE = "private, max-age=3600"


def _params(request: Request) -> tuple[str, int]:
    """`deckId` and `Number(p.get('slide'))`: a deck id and a positive integer, else 400 "Bad params"."""
    deck_id = query_param(request, "deckId")
    slide = js_number(query_param(request, "slide"))
    if not deck_id or not is_js_integer(slide) or slide <= 0:
        raise ApiError(400, BAD_PARAMS)
    return deck_id, int(slide)


@darwin_route(router, "/image", methods=("GET",), summary="A slide's picture (PNG), at a version")
async def image(request: Request) -> Response:
    user = await require_user(request)
    deck_id, slide_number = _params(request)
    storage = get_storage(request)
    deck = await owned_deck(storage, user.ctx, postgres_uuid(deck_id))
    v = js_number(query_param(request, "v"))
    version = int(v) if is_js_integer(v) and v >= 2 else 1
    slide = await media.read_slide(storage, user.ctx, deck.id, slide_number)
    data = await media.slide_picture(storage, user.ctx, slide, version)
    if data is None or slide is None:
        raise ApiError(404, IMAGE_NOT_FOUND)
    data = await media.tile_preview(storage, user.ctx, deck, slide, data)
    return Response(content=data, status_code=200, media_type=PNG, headers={"cache-control": IMAGE_CACHE})


@darwin_route(router, "/pdf", methods=("GET",), summary="One slide as a single-page PDF")
async def pdf(request: Request) -> Response:
    user = await require_user(request)
    deck_id, slide_number = _params(request)
    storage = get_storage(request)
    deck = await owned_deck(storage, user.ctx, postgres_uuid(deck_id))
    v = js_number(query_param(request, "v"))
    version = int(v) if is_js_integer(v) and v >= 1 else 1
    slide = await media.read_slide(storage, user.ctx, deck.id, slide_number)
    data = await media.slide_picture(storage, user.ctx, slide, version)
    if data is None:
        raise ApiError(404, IMAGE_NOT_FOUND)
    document = await asyncio.to_thread(compose.pdf_from_pngs, [data])  # not a PNG: 500, as embedPng
    await media.count_pdf_export(storage, user.ctx, deck.id, [slide_number])
    return Response(content=document, status_code=200, media_type=PDF,
                    headers={"content-disposition": f'attachment; filename="slide_{slide_number}.pdf"'})


@darwin_route(router, "/pdf-deck", methods=("GET",), summary="Every finished slide as one PDF")
async def pdf_deck(request: Request) -> Response:
    user = await require_user(request)
    deck_id = required_query(request, "deckId", "Missing deckId")
    storage = get_storage(request)
    deck = await owned_deck(storage, user.ctx, postgres_uuid(deck_id))
    slides = await storage.slides.list_for_deck(user.ctx, deck.id)
    if not slides:
        raise ApiError(404, "Deck not found")  # Darwin: no job state for the deck yet
    done = sorted((s for s in slides if s.status == "done"), key=lambda s: s.number)
    if not done:
        raise ApiError(400, "No finished slides to export")
    pages: list[bytes] = []
    for slide in done:
        data = await media.slide_picture(storage, user.ctx, slide, current_version(slide))
        if data is not None:  # skip a slide whose picture is unavailable
            pages.append(data)
    if not pages:
        raise ApiError(404, "No slide images found")
    document = await asyncio.to_thread(compose.pdf_from_pngs, pages)
    await media.count_pdf_export(storage, user.ctx, deck.id, [s.number for s in done])
    return Response(content=document, status_code=200, media_type=PDF,
                    headers={"content-disposition": f'attachment; filename="{media.deck_pdf_filename(deck.title)}"'})
