"""
SlideForge service: core/errors. Two error formats, chosen per route (decision D32).

1. **Legacy** `{"error": "<message>"}`, for today's Darwin `/api/*` routes. Their
   path, method and bodies stay exactly as Darwin serves them from
   `netlify/functions/_shared/http.ts`, so their errors do too. A route opts in by
   being registered with `route_class=LegacyErrorRoute` (see `legacy_router()`),
   or by carrying the `LEGACY_ERRORS_TAG` tag.
2. **Problem Details** (RFC 9457, `application/problem+json`), for every other
   route. Each body has `type`, `title`, `status`, `detail` and the `requestId`;
   validation failures add an `errors` list by field.

The choice is made from the MATCHED route, so a path that matched nothing (a 404
or 405 from the router) gets Problem Details. Neither format ever carries a stack
trace, exception text, SQL, host names or keys, and the cause is logged under the
request id:

* Problem Details: 5xx detail is always generic.
* Legacy: an unhandled error is Darwin's `{"error": "Internal error"}` (`errorResponse`
  in `_shared/http.ts`). A deliberate `ApiError` keeps its message at any status,
  because Darwin's contract has fixed 5xx texts the callers read (502 "PPTX service
  error", which the Connector treats as "still running"). So on a legacy route an
  ApiError message must be one of Darwin's fixed strings, never exception text.

Raise `ApiError` from a handler for any deliberate error; it renders in whichever
format the route uses.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from http import HTTPStatus
from typing import Any, cast

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.request_id import get_request_id, request_id_header_name

_log = logging.getLogger(__name__)

PROBLEM_JSON = "application/problem+json"
LEGACY_ERRORS_TAG = "legacy-errors"

# The message a legacy route returns when FastAPI's own body/query validation
# rejects a request before the handler runs. Darwin's handlers answer bad input
# with 400 and their own message; P5's contract snapshots replace this per route
# by validating inside the handler and raising ApiError with today's text.
LEGACY_VALIDATION_MESSAGE = "Invalid request"
GENERIC_5XX_DETAIL = "Something went wrong on our side. Quote the request id if you report it."
# Darwin's body for any error that is not an HttpError (`_shared/http.ts: errorResponse`).
LEGACY_INTERNAL_ERROR = "Internal error"

_TYPES: dict[int, str] = {
    400: "bad-request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not-found",
    405: "method-not-allowed",
    409: "conflict",
    413: "payload-too-large",
    415: "unsupported-media-type",
    422: "validation-failed",
    429: "rate-limited",
    500: "internal-error",
    502: "bad-gateway",
    503: "service-unavailable",
    504: "gateway-timeout",
}
_TITLES: dict[int, str] = {422: "Validation failed", 429: "Rate limited"}


class LegacyErrorRoute(APIRoute):
    """Marker route class: errors on this route use the legacy `{"error"}` body."""


def legacy_router(**kwargs: Any) -> APIRouter:
    """An APIRouter whose routes all use the legacy error body."""
    return APIRouter(route_class=LegacyErrorRoute, **kwargs)


class ApiError(Exception):
    """A deliberate error from a handler.

    `detail` is the human message: the `error` string on a legacy route, the
    `detail` member in Problem Details. `errors` is the per-field list for a 422.
    `headers` carries e.g. Retry-After on a 429.
    """

    def __init__(
        self,
        status: int,
        detail: str,
        *,
        title: str | None = None,
        type_: str | None = None,
        errors: Sequence[dict[str, str]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.title = title
        self.type_ = type_
        self.errors = list(errors) if errors else None
        self.headers = headers or {}


def uses_legacy_errors(request: Request) -> bool:
    route = request.scope.get("route")
    if isinstance(route, LegacyErrorRoute):
        return True
    return LEGACY_ERRORS_TAG in (getattr(route, "tags", None) or [])


def problem_type(status: int) -> str:
    return f"/errors/{_TYPES.get(status, 'error' if status < 500 else 'internal-error')}"


def problem_title(status: int) -> str:
    if status in _TITLES:
        return _TITLES[status]
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


def _headers(request: Request, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = dict(extra or {})
    headers[request_id_header_name(request)] = get_request_id(request)
    return headers


def problem_response(
    request: Request,
    status: int,
    detail: str,
    *,
    title: str | None = None,
    type_: str | None = None,
    errors: Sequence[dict[str, str]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": type_ or problem_type(status),
        "title": title or problem_title(status),
        "status": status,
        "detail": GENERIC_5XX_DETAIL if status >= 500 else detail,
        "requestId": get_request_id(request),
    }
    if errors:
        body["errors"] = list(errors)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=_headers(request, headers))


def legacy_response(
    request: Request, status: int, message: str, *, headers: dict[str, str] | None = None
) -> JSONResponse:
    """Darwin's `json({error: message}, status)`: content-type application/json, nothing else
    (plus our request id header). The message is shown as is: see the module docstring."""
    return JSONResponse({"error": message}, status_code=status, headers=_headers(request, headers))


def _render(
    request: Request,
    status: int,
    detail: str,
    *,
    title: str | None = None,
    type_: str | None = None,
    errors: Sequence[dict[str, str]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    if uses_legacy_errors(request):
        return legacy_response(request, status, detail, headers=headers)
    return problem_response(
        request, status, detail, title=title, type_=type_, errors=errors, headers=headers
    )


def _field_name(loc: Sequence[Any]) -> str:
    # ("body", "slides", 0, "title") -> "slides[0].title"; the source segment is dropped.
    parts = list(loc[1:]) if loc and loc[0] in ("body", "query", "path", "header", "cookie") else list(loc)
    name = ""
    for part in parts:
        name += f"[{part}]" if isinstance(part, int) else (f".{part}" if name else str(part))
    return name or "request"


def validation_errors(exc: RequestValidationError) -> list[dict[str, str]]:
    """Per-field messages. Built from `loc` and `msg` only: pydantic's `input` and
    `ctx` would echo the caller's (possibly secret) values back."""
    return [{"field": _field_name(e.get("loc", ())), "message": str(e.get("msg", "Invalid value."))}
            for e in exc.errors()]


async def _api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    exc = cast(ApiError, exc)  # registered for ApiError only
    if exc.status >= 500:
        _log.error("api error %s: %s", exc.status, exc.detail)
    return _render(request, exc.status, exc.detail, title=exc.title, type_=exc.type_,
                   errors=exc.errors, headers=exc.headers)


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    exc = cast(StarletteHTTPException, exc)
    status = exc.status_code
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail else problem_title(status)
    return _render(request, status, detail, headers=dict(exc.headers or {}))


async def _validation_handler(request: Request, exc: Exception) -> JSONResponse:
    exc = cast(RequestValidationError, exc)
    if uses_legacy_errors(request):
        return legacy_response(request, 400, LEGACY_VALIDATION_MESSAGE)
    return problem_response(request, 422, "One or more fields are invalid.", errors=validation_errors(exc))


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    # The cause goes to the log under the request id; the caller gets nothing internal.
    _log.exception("unhandled error on %s %s", request.method, request.url.path)
    if uses_legacy_errors(request):
        return legacy_response(request, 500, LEGACY_INTERNAL_ERROR)
    return _render(request, 500, GENERIC_5XX_DETAIL)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(Exception, _unhandled_handler)
