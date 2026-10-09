"""SlideForge service: api/routes/decks. Darwin's `/api/decks` (`netlify/functions/decks.ts`).

Same path, methods, request and response; contract `tests/contract/data/api-decks.json`. Only DELETE is
told apart; every other method is a GET (`effective_method`). The body is never read.

* `GET` -> `{decks: [{id, title, status, created_at}]}`, newest first (`[]` for a new user);
* `GET ?id=` -> `{deck, slides}`: the deck and its slides as Darwin's rows (`deck_row`, `slide_row`); a deck
  that is missing or someone else's is 404 "Deck not found"; a non-uuid id is 500 (quirk 2);
* `DELETE ?id=` -> `{ok: true}`, also for a missing or foreign deck (idempotent, owner-scoped); no id: 400.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import Request
from fastapi.responses import Response

from app.api.deps import get_storage, require_user
from app.api.legacy import darwin_route, effective_method, json_response, legacy_router, postgres_uuid, query_param
from app.core.darwin.deck_state import current_version, image_ref_for
from app.core.darwin.generate import storyline_types
from app.core.errors import ApiError
from app.core.storage.models import Deck, Slide
from app.core.storage.ports import Forbidden, NotFound

router = legacy_router(prefix="/api", tags=["darwin-decks"])

_SLIDE_ROW_NS = uuid.UUID("6d1f3c2a-5b7e-4f0a-9c1d-2e3f4a5b6c7d")


def _ts(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def deck_row(deck: Deck) -> dict[str, Any]:
    """The `decks` row (`select *`) Darwin returned: its columns, snake_case, null where unset."""
    return {
        "id": deck.id, "owner": deck.owner_id, "title": deck.title, "inputs": deck.inputs,
        "storyline": deck.storyline, "status": deck.status, "created_at": _ts(deck.created_at),
        "updated_at": _ts(deck.updated_at), "slide_count": deck.slide_count, "brand_id": deck.brand_id,
        "creation_method": deck.creation_method, "generation_started_at": _ts(deck.generation_started_at),
        "generation_completed_at": _ts(deck.generation_completed_at), "refine_count": deck.refine_count,
        "export_count": deck.export_count, "first_exported_at": _ts(deck.first_exported_at),
    }


def slide_row(slide: Slide, types: dict[int, str]) -> dict[str, Any]:
    """The `slides` row Darwin returned, plus `type` folded in from the storyline when it has one. The
    single image's columns keep their legacy `variant_a_*` names; `variant_b_*` are always null."""
    spec = slide.spec
    row: dict[str, Any] = {
        "id": str(uuid.uuid5(_SLIDE_ROW_NS, f"{slide.deck_id}:{slide.number}")), "deck_id": slide.deck_id,
        "number": slide.number, "title": spec.title, "framework": spec.framework, "description": spec.description,
        "bullets": spec.bullets, "prompt": spec.prompt,
        "variant_a_blob_key": image_ref_for(slide, current_version(slide)), "variant_b_blob_key": None,
        "variant_a_status": slide.status, "variant_b_status": None,
        "variant_a_error": slide.error, "variant_b_error": None,
        "refine_count": slide.refine_count, "last_refined_at": _ts(slide.last_refined_at),
        "generation_duration_ms": slide.generation_duration_ms,
    }
    if slide.number in types:
        row["type"] = types[slide.number]
    return row


@darwin_route(router, "/decks", methods=("GET", "DELETE"),
              summary="List my decks, read one with its slides (?id=), or delete one (DELETE ?id=)")
async def decks(request: Request) -> Response:
    user = await require_user(request)
    storage = get_storage(request)
    deck_id = query_param(request, "id")
    if effective_method(request, distinct=("DELETE",)) == "DELETE":
        if not deck_id:
            raise ApiError(400, "id required")
        await storage.decks.delete(user.ctx, postgres_uuid(deck_id))
        return json_response({"ok": True})
    if not deck_id:
        listed: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await storage.decks.list_mine(user.ctx, limit=200, cursor=cursor)
            listed.extend({"id": d.id, "title": d.title, "status": d.status, "created_at": _ts(d.created_at)}
                          for d in page.items)
            if not page.next_cursor:
                break
            cursor = page.next_cursor
        return json_response({"decks": listed})
    try:
        deck = await storage.decks.get(user.ctx, postgres_uuid(deck_id))
    except (NotFound, Forbidden) as exc:
        raise ApiError(404, "Deck not found") from exc
    slides = await storage.slides.list_for_deck(user.ctx, deck.id)
    types = storyline_types(deck)
    return json_response({"deck": deck_row(deck), "slides": [slide_row(s, types) for s in slides]})
