"""SlideForge service: api/routes/refine. Darwin's `/api/refine`, `/api/revert`, `/api/slide-transcript`.

Same path, method, request and response as `netlify/functions/{refine,revert,slide-transcript}.ts`;
contracts `tests/contract/data/api-{refine,revert,slide-transcript}.json`. The `refine-background` trigger is
the internal `darwin.refine` job (D31): see `app/core/darwin/refine.py`.

| Route | Answer | Quirks kept |
|---|---|---|
| `POST /api/refine` | 202 `{deckId}` (echo) | number must be a positive integer (`"2"` is a 400); instruction trimmed, 1..600 UTF-16 units; a non-string instruction or a non-array `attachments` is 500; non-image attachments silently dropped |
| `POST /api/revert` | 200 `{ok: true}` | 404 "No such slide" / "No such version"; the slide's status is not changed |
| `POST /api/slide-transcript` | 200 `{ok: true}` | 405 BEFORE auth; storage errors swallowed; attachment payloads stripped to "[attachment]" |
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.responses import Response

from app.api.deps import get_runtime, get_storage, require_user
from app.api.legacy import (
    check_method,
    darwin_route,
    destructure,
    js_truthy,
    json_response,
    legacy_router,
    postgres_uuid,
    read_json,
)
from app.core.darwin import refine as darwin_refine
from app.core.darwin.exports import owned_deck
from app.core.darwin.jobs import submit_job
from app.core.darwin.js import is_js_integer, js_length, js_trim
from app.core.errors import ApiError

router = legacy_router(prefix="/api", tags=["darwin-refine"])
_log = logging.getLogger(__name__)

REFINE_PARAMS = "deckId and a valid slide number are required"
REVERT_PARAMS = "deckId, number, version required"
TRANSCRIPT_PARAMS = "Missing deckId, slideNumber, or messages"
MAX_INSTRUCTION = 600

_MISSING = object()  # JavaScript's undefined, for the transcript sanitiser


def _integer_at_least(value: Any, minimum: int) -> int | None:
    """`Number.isInteger(value) && value >= minimum` (a JSON 2.0 counts), as an int; else None."""
    return int(value) if is_js_integer(value) and value >= minimum else None


@darwin_route(router, "/refine", methods=("POST",), summary="Refine one slide's image with an instruction: 202 {deckId}")
async def refine(request: Request) -> Response:
    user = await require_user(request)
    body = destructure(await read_json(request))
    deck_id, number, instruction = body.get("deckId"), body.get("number"), body.get("instruction")
    slide_number = _integer_at_least(number, 1)
    if not js_truthy(deck_id) or slide_number is None:
        raise ApiError(400, REFINE_PARAMS)
    if instruction is not None and not isinstance(instruction, str):
        raise TypeError("instruction.trim is not a function")  # Darwin: 500
    text = js_trim(instruction or "")
    if not text:
        raise ApiError(400, "instruction is required")
    if js_length(text) > MAX_INSTRUCTION:
        raise ApiError(400, f"instruction is too long (max {MAX_INSTRUCTION} chars)")
    storage = get_storage(request)
    deck = await owned_deck(storage, user.ctx, postgres_uuid(deck_id))
    attachments = darwin_refine.valid_attachments(body.get("attachments"))  # a non-array: 500
    inputs: dict[str, Any] = {"deckId": deck.id, "number": slide_number, "instruction": text}
    if attachments:  # only the first image is used (refine-background.ts); stored for the job, not inlined
        try:
            data = darwin_refine.decode_attachment(str(attachments[0]["data"]))
            stored = await storage.blobs.put(user.ctx, data, str(attachments[0]["media_type"]))
            inputs["attachmentRef"] = stored.ref
        except Exception:  # noqa: BLE001 - the refine goes on without the reference image
            _log.info("refine: the attachment could not be stored; refining without it", exc_info=True)
    await darwin_refine.mark_generating(storage, user.ctx, deck.id, slide_number)
    await submit_job(get_runtime(request).queue, user.ctx, darwin_refine.REFINE_JOB, inputs)
    return json_response({"deckId": deck_id}, 202)


@darwin_route(router, "/revert", methods=("POST",), summary="Make an earlier version of a slide current")
async def revert(request: Request) -> Response:
    user = await require_user(request)
    body = destructure(await read_json(request))
    deck_id, number, version = body.get("deckId"), body.get("number"), body.get("version")
    slide_number, wanted = _integer_at_least(number, 1), _integer_at_least(version, 1)
    if not js_truthy(deck_id) or slide_number is None or wanted is None:
        raise ApiError(400, REVERT_PARAMS)
    storage = get_storage(request)
    deck = await owned_deck(storage, user.ctx, postgres_uuid(deck_id))
    await darwin_refine.revert_slide(storage, user.ctx, deck.id, slide_number, wanted)
    return json_response({"ok": True})


def _prop(value: Any, key: str) -> Any:
    """`value.key` in JavaScript: undefined for a primitive or an array, a TypeError for null/undefined."""
    if value is None:
        raise TypeError(f"cannot read {key!r} of null")
    return value.get(key, _MISSING) if isinstance(value, dict) else _MISSING


def _sanitize_block(block: Any) -> Any:
    kind = _prop(block, "type")
    if kind == "text":
        return block
    media = _prop(block, "media_type")
    out: dict[str, Any] = {}
    if kind is not _MISSING:
        out["type"] = kind
    out["data"] = "[attachment]"
    out["media_type"] = None if media is _MISSING or media is None else media
    return out


def _sanitize_message(message: Any) -> dict[str, Any]:
    role, content = _prop(message, "role"), _prop(message, "content")
    out: dict[str, Any] = {}
    if role is not _MISSING:
        out["role"] = role
    if isinstance(content, list):
        out["content"] = [_sanitize_block(b) for b in content]
    elif content is not _MISSING:
        out["content"] = content
    return out


@darwin_route(router, "/slide-transcript", methods=("POST",), summary="Save a slide's edit conversation")
async def slide_transcript(request: Request) -> Response:
    check_method(request, "POST")  # BEFORE auth
    user = await require_user(request)
    body = await read_json(request)
    fields = body if isinstance(body, dict) else {}  # body?.deckId: undefined on null, arrays and primitives
    deck_id, slide_number, messages = fields.get("deckId"), fields.get("slideNumber"), fields.get("messages")
    if not js_truthy(deck_id) or not isinstance(slide_number, (int, float)) or isinstance(slide_number, bool) \
            or not isinstance(messages, list):
        raise ApiError(400, TRANSCRIPT_PARAMS)
    storage = get_storage(request)
    deck = await owned_deck(storage, user.ctx, postgres_uuid(deck_id))
    sanitized: list[object] = [_sanitize_message(m) for m in messages]  # a null message: 500, as Darwin
    count = fields.get("refineCount")
    refine_count = count if count is not None else sum(1 for m in sanitized if isinstance(m, dict) and m.get("role") == "user")
    try:
        if not is_js_integer(slide_number) or not is_js_integer(refine_count):
            raise ValueError("not an integer column value")  # Postgres refused it; Darwin logged it
        await storage.transcripts.upsert_slide_edit(user.ctx, deck.id, int(slide_number), sanitized, int(refine_count))
    except Exception:  # noqa: BLE001 - upsertSlideEditTranscript logs and swallows every error
        _log.info("slide-transcript: the transcript was not stored", exc_info=True)
    return json_response({"ok": True})
