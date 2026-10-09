"""SlideForge service: api/routes/generate. Darwin's `/api/generate`, `/api/retry`, `/api/status`.

Same path, method, request and response as `netlify/functions/{generate,retry,status}.ts`; contracts
`tests/contract/data/api-{generate,retry,status}.json`. The `generate-background` trigger is the internal
`darwin.generate` job (D31): see `app/core/darwin/generate.py`.

| Route | Answer | Quirks kept |
|---|---|---|
| `POST /api/generate` | 202 `{deckId, warnings}` | method not checked; ANY validation failure is 500 "Internal error" (quirk 3, the Connector relies on it); the client's `prompt` is ignored |
| `POST /api/retry` | 202 `{deckId}` (echo) | `retryOnly` passed on verbatim (`[]` regenerates nothing); non-uuid deckId 500 |
| `GET /api/status?deckId=` | 200 the deck's job view | before any state: `{deckId, slides:{}, progress:"Starting…", done:false}`; optional keys omitted |
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import Response

from app.api.deps import get_runtime, get_storage, require_user
from app.api.legacy import (
    darwin_route,
    destructure,
    js_truthy,
    json_response,
    legacy_router,
    postgres_uuid,
    read_json,
    required_query,
)
from app.core.darwin import deck_state
from app.core.darwin.exports import owned_deck
from app.core.darwin.generate import GENERATE_JOB, InvalidStoryline, prepare_deck, retry_inputs
from app.core.darwin.jobs import submit_job
from app.core.errors import LEGACY_INTERNAL_ERROR, ApiError

router = legacy_router(prefix="/api", tags=["darwin-generate"])


@darwin_route(router, "/generate", methods=("POST",), summary="Generate a deck's slide images: 202 {deckId, warnings}")
async def generate(request: Request) -> Response:
    """Order: auth, body (malformed: 500), validate + repair (failure: 500), brand kit, prompts, persist, job."""
    user = await require_user(request)
    payload = await read_json(request)
    try:
        prepared = await prepare_deck(get_storage(request), user.ctx, payload)
    except InvalidStoryline as exc:
        # Darwin's ZodError is not an HttpError: 500 "Internal error" (quirk 3). Darwin's own text, never ours.
        raise ApiError(500, LEGACY_INTERNAL_ERROR) from exc
    await submit_job(get_runtime(request).queue, user.ctx, GENERATE_JOB, prepared.job_inputs)
    return json_response({"deckId": prepared.deck.id, "warnings": prepared.warnings}, 202)


@darwin_route(router, "/retry", methods=("POST",), summary="Regenerate some slides of a deck: 202 {deckId}")
async def retry(request: Request) -> Response:
    user = await require_user(request)
    body = destructure(await read_json(request))  # null: 500
    deck_id, retry_only = body.get("deckId"), body.get("retryOnly")
    if not js_truthy(deck_id) or not isinstance(retry_only, list):
        raise ApiError(400, "deckId and retryOnly required")
    storage = get_storage(request)
    deck = await owned_deck(storage, user.ctx, postgres_uuid(deck_id))  # 403 "Not your deck"; non-uuid 500
    inputs = await retry_inputs(storage, user.ctx, deck, retry_only)
    await submit_job(get_runtime(request).queue, user.ctx, GENERATE_JOB, inputs)
    return json_response({"deckId": deck_id}, 202)


@darwin_route(router, "/status", methods=("GET",), summary="Poll a deck's generation: per-slide status and image urls")
async def status(request: Request) -> Response:
    user = await require_user(request)
    deck_id = required_query(request, "deckId", "deckId is required")
    storage = get_storage(request)
    deck = await owned_deck(storage, user.ctx, postgres_uuid(deck_id))
    slides = await storage.slides.list_for_deck(user.ctx, deck.id)
    return json_response(deck_state.status_body(deck_id, deck, slides))
