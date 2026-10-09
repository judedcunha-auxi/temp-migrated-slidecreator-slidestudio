"""`/api/analytics-event`'s batch: the frontend's fire-and-forget usage events (Darwin `analytics-event.ts`).

Each batch touches the caller's `last_seen_at` and stores every event through the storage port
(`AnalyticsPort`; D27 may route these to PostHog / App Insights later, and the route keeps its shape
either way). Nothing here can fail the request: Darwin logged and swallowed every write error, so an
event the store refuses (a missing name, a value outside the store's limits) is dropped and logged.

* `page_exited` goes to the page-view sessions (path, duration, scroll depth, referrer);
* every other event to the events table, with `session_id` from the event or its properties.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from app.core.storage.models import AnalyticsEventCreate, CallerContext, PageViewCreate
from app.core.storage.ports import Storage

_log = logging.getLogger(__name__)

MAX_BATCH = 50


def js_iso(now: datetime) -> str:
    """`new Date().toISOString()`: UTC, milliseconds, a trailing Z."""
    utc = now.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _number_or_none(value: Any) -> Any:
    """`typeof v === 'number' ? v : null`."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _whole(value: Any) -> int | None:
    """A number for an integer column: whole values only (anything else is an insert Postgres refuses)."""
    if value is None:
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError("not an integer")
        return int(value)
    return int(value)


def _pick(*values: Any) -> Any:
    """`a ?? b ?? c`."""
    for value in values:
        if value is not None:
            return value
    return None


def _write(storage: Storage, ctx: CallerContext, event: Any) -> Awaitable[Any] | None:
    """The store call for one event, or None when Darwin skipped it (no truthy `event_name`)."""
    if not isinstance(event, dict):
        return None
    name = event.get("event_name")
    if name is None or name is False or name == "" or name == 0:
        return None
    raw_props = event.get("properties")
    props: dict[str, Any] = raw_props if isinstance(raw_props, dict) else {}
    if name == "page_exited":
        try:
            view = PageViewCreate(
                session_id=str(_pick(event.get("session_id"), props.get("session_id"), "")),
                path=str(_pick(props.get("path"), event.get("page_path"), "")),
                duration_seconds=_whole(_number_or_none(props.get("duration_seconds"))),
                scroll_depth_pct=_whole(_number_or_none(props.get("scroll_depth_pct"))),
                referrer_path=_pick(props.get("referrer_path")),
            )
        except (ValidationError, ValueError, TypeError):
            _log.info("analytics: a page_exited event the store cannot take was dropped")
            return None
        return storage.analytics.record_page_view(ctx, view)
    try:
        data = AnalyticsEventCreate(
            session_id=_pick(event.get("session_id"), props.get("session_id")),
            event_name=name if isinstance(name, str) else str(name),
            properties=raw_props if isinstance(raw_props, dict) else {},
            page_path=_pick(event.get("page_path")),
        )
    except (ValidationError, ValueError, TypeError):
        _log.info("analytics: an event the store cannot take was dropped")
        return None
    return storage.analytics.record_event(ctx, data)


async def record_batch(storage: Storage, ctx: CallerContext, events: list[Any]) -> None:
    """Touch last-seen and store each event, all at once; every failure is logged and swallowed."""
    writes: list[Awaitable[Any]] = [storage.users.touch_last_seen(ctx)]
    writes.extend(w for e in events if (w := _write(storage, ctx, e)) is not None)
    for outcome in await asyncio.gather(*writes, return_exceptions=True):
        if isinstance(outcome, BaseException):
            _log.warning("analytics: a write failed and was ignored: %s", type(outcome).__name__)
