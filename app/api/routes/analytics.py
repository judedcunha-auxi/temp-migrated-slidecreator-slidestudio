"""SlideForge service: api/routes/analytics. Darwin's `/api/analytics-event`.

Same path, method, request and response as `netlify/functions/analytics-event.ts` (contract:
`tests/contract/data/api-analytics-event.json`). The frontend fires it and forgets it (keepalive), so:

* auth still applies (401 before anything else);
* a malformed body, or one without an `events` array, is a silent `200 {"ok": true}` with no writes;
* otherwise the first 50 events are stored (the rest dropped silently) and the answer is
  `{"ok": true, "stored": <batch length, skipped events included>, "at": <ISO time taken before the
  writes>}`; a failed write never surfaces (app/core/darwin/analytics.py).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Request
from fastapi.responses import Response

from app.api.deps import get_storage, require_user
from app.api.legacy import darwin_route, json_response, legacy_router, read_json_or_none
from app.core.darwin.analytics import MAX_BATCH, js_iso, record_batch

router = legacy_router(prefix="/api", tags=["darwin-analytics"])


@darwin_route(router, "/analytics-event", methods=("POST",), summary="Record a batch of usage events")
async def analytics_event(request: Request) -> Response:
    """Any method (a bodyless GET is the silent `{ok:true}`)."""
    user = await require_user(request)
    body = await read_json_or_none(request)
    events = body.get("events") if isinstance(body, dict) else None
    if not isinstance(events, list):
        return json_response({"ok": True})
    batch = events[:MAX_BATCH]
    at = js_iso(datetime.now(UTC))
    await record_batch(get_storage(request), user.ctx, batch)
    return json_response({"ok": True, "stored": len(batch), "at": at})
