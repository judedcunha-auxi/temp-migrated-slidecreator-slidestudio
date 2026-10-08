"""
SlideForge service: core/request_id. The request id on every request.

The gateway puts a request id on every call (repo standards, "The gateway does").
RequestIdMiddleware:

1. reads it from the configured header (REQUEST_ID_HEADER, default X-Request-ID),
   or generates one when it is missing or malformed;
2. binds it for logging, so every log line of the request carries it;
3. returns it on the response in the same header;
4. writes one access line per request and records the per-endpoint metric.

An incoming id is accepted only if it is short and made of safe characters: it is
written into every log line, so a caller-supplied value must not be able to forge
or break lines. Pure ASGI (not BaseHTTPMiddleware), so streaming responses and
background tasks are untouched.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import telemetry
from app.core.logging_config import bind_request_id, current_request_id, unbind_request_id

_log = logging.getLogger("app.access")

SCOPE_KEY = "slideforge.request_id"
HEADER_SCOPE_KEY = "slideforge.request_id_header"
DEFAULT_HEADER = "X-Request-ID"
_VALID = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")


def new_request_id() -> str:
    return str(uuid.uuid4())


def accept_request_id(raw: str | None) -> str:
    """The incoming id if it is safe to log, else a fresh one."""
    if raw and _VALID.match(raw):
        return raw
    return new_request_id()


def get_request_id(request: Request) -> str:
    """The id of `request`, as bound by the middleware."""
    value = request.scope.get(SCOPE_KEY)
    return value if isinstance(value, str) else current_request_id()


def request_id_header_name(request: Request) -> str:
    value = request.scope.get(HEADER_SCOPE_KEY)
    return value if isinstance(value, str) else DEFAULT_HEADER


def _feature(scope: Scope) -> str:
    """The feature a route belongs to: its first tag, else 'unrouted'."""
    tags = getattr(scope.get("route"), "tags", None) or []
    return str(tags[0]) if tags else "unrouted"


def _route_template(scope: Scope) -> str:
    """The route template (e.g. /api/status), never the raw path: raw paths would
    make the metric's cardinality unbounded."""
    return str(getattr(scope.get("route"), "path", "") or "unmatched")


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp, header_name: str = DEFAULT_HEADER) -> None:
        self.app = app
        self.header_name = header_name
        self._header_bytes = header_name.lower().encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw = None
        for name, value in scope.get("headers", []):
            if name == self._header_bytes:
                raw = value.decode("latin-1")
                break
        request_id = accept_request_id(raw)
        scope[SCOPE_KEY] = request_id
        scope[HEADER_SCOPE_KEY] = self.header_name
        token = bind_request_id(request_id)
        status = 500
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers: list[Any] = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != self._header_bytes
                ]
                headers.append((self._header_bytes, request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            elapsed = time.perf_counter() - started
            # The raw path only, never the query string: it can carry ids and links.
            _log.info("%s %s -> %s in %.0fms", scope.get("method", "-"), scope.get("path", "-"),
                      status, elapsed * 1000)
            telemetry.record_request(route=_route_template(scope), method=str(scope.get("method", "-")),
                                     status=status, duration_s=elapsed, feature=_feature(scope))
            unbind_request_id(token)
