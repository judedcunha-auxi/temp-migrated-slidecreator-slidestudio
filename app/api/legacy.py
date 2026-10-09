"""SlideForge service: api/legacy. The contract helpers for Darwin's `/api/*` routes.

Darwin's routes keep their path, method, request and response exactly (plan §2.1, D32), quirks
included. The oracle is the contract reference recorded from the TypeScript handlers:
`tests/contract/data/` (a copy of `Slide-Creator/docs/slideforge-migration/contract/`), with the
cross-cutting quirks in `INDEX.md` and the shared behaviour in `_common.json`. Every helper below
reproduces one piece of `netlify/functions/_shared/http.ts` or one quirk, and says which.

How a Darwin handler is written (see docs/darwin-api.md for the full guide):

    router = legacy_router(prefix="/api", tags=["darwin-storyline"])

    @darwin_route(router, "/storyline", methods=("POST",), summary="Draft a storyline (202 {jobId})")
    async def storyline(request: Request) -> Response:
        user = await require_user(request)          # auth first: 401 before anything else
        body = await read_json(request)             # malformed JSON -> 500 "Internal error" (quirk 1)
        ...
        return json_response({"jobId": record.id}, 202)

Handlers take the raw `Request` and return a `Response`: no FastAPI body models, so FastAPI never
answers for them (its 422 / 405 would not be Darwin's). Errors are raised as `ApiError(status,
"<Darwin's exact text>")` and rendered `{"error": ...}` by app/core/errors.py; anything else is
500 `{"error": "Internal error"}`.
"""

from __future__ import annotations

import json
import logging
import math
import re
import uuid
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from app.core.errors import (
    LEGACY_INTERNAL_ERROR,
    ApiError,
    legacy_response,
    legacy_router,
    problem_response,
    uses_legacy_errors,
)

_log = logging.getLogger(__name__)

__all__ = [
    "ALL_METHODS",
    "LegacyModel",
    "MalformedJsonBody",
    "NonUuidId",
    "NullBody",
    "check_method",
    "compact",
    "darwin_route",
    "effective_method",
    "destructure",
    "error_response",
    "install_legacy_handlers",
    "js_parse_int",
    "js_truthy",
    "json_response",
    "legacy_router",
    "postgres_uuid",
    "query_param",
    "read_json",
    "read_json_or_none",
    "required_query",
]

JSON_CONTENT_TYPE = "application/json"
METHOD_NOT_ALLOWED = "Method not allowed"

#: Netlify hands a function every method; Darwin's handlers decide (quirk 6). Each Darwin route is
#: registered for all of these, so the handler, not the router, answers an unexpected method.
ALL_METHODS: tuple[str, ...] = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")

F = TypeVar("F", bound=Callable[..., Any])


# --------------------------------------------------------------------------------------- routing
def darwin_route(router: APIRouter, path: str, *, methods: tuple[str, ...], summary: str,
                 description: str | None = None) -> Callable[[F], F]:
    """Register `handler` on `path` for EVERY method.

    `methods` are the ones Darwin documents; only they appear in the OpenAPI document. The rest are
    served by the same handler but hidden, because Darwin answers them itself: most routes ignore the
    method (`_common.json` methodHandling), some answer 405 (`check_method`), and `decks` /
    `brand-asset` treat them as GET (`effective_method`). Without this, FastAPI would answer an
    unexpected method with its own 405 in the wrong format.
    """
    documented = tuple(m.upper() for m in methods)
    hidden = tuple(m for m in ALL_METHODS if m not in documented)

    def register(handler: F) -> F:
        for method in documented:  # one operation each, so every operation id is unique
            router.add_api_route(path, handler, methods=[method], summary=summary, description=description,
                                 response_model=None, operation_id=f"{handler.__name__}_{method.lower()}")
        if hidden:
            router.add_api_route(path, handler, methods=list(hidden), include_in_schema=False, response_model=None)
        return handler

    return register


def check_method(request: Request, allowed: str | tuple[str, ...], message: str = METHOD_NOT_ALLOWED) -> None:
    """405 `{"error": message}` unless the method is allowed.

    WHERE you call it is the contract (quirk 6): before `require_user` on slide-transcript,
    pptx-submit, pptx-deck-submit, brand-archetypes and brand-heading (an unauthenticated GET gets
    405, not 401); after it on image-to-slide ("POST only"), brand-pptx and admin-org-brands. Every
    other route never checks. Each route's contract file says which (`methodHandling`)."""
    wanted = (allowed,) if isinstance(allowed, str) else allowed
    if request.method.upper() not in wanted:
        raise ApiError(405, message)


def effective_method(request: Request, *, distinct: tuple[str, ...]) -> str:
    """The method a "treat anything else as GET" route acts on (quirk 6): the request's method if it is
    one of `distinct`, else "GET". `decks` passes ("DELETE",); `brand-asset` passes ("POST",).
    (`api-decks.json`, `api-brand-asset.json` methodHandling.)"""
    method = request.method.upper()
    return method if method in distinct else "GET"


# ----------------------------------------------------------------------------------------- bodies
class MalformedJsonBody(ValueError):
    """`await req.json()` threw. Deliberately NOT an ApiError: Darwin answers it 500 "Internal error"."""


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")  # JSON.parse refuses NaN / Infinity; Python's json would not


async def read_json(request: Request) -> Any:
    """Darwin's bare `await req.json()` (quirk 1; `_common.json` errorBody.consequence).

    Any JSON value comes back as is: null, a list, a number (the handler's own checks then answer,
    e.g. 400 "Missing params", or crash into a 500 just as the TypeScript did). An empty or malformed
    body raises `MalformedJsonBody`, which the error handler turns into 500 `{"error":"Internal
    error"}`, NOT a 400. The content-type header is not checked (Darwin never checked it).

    The routes that catch the parse error instead use `read_json_or_none`: intake (-> 400 "messages
    array required"), brand-extract (-> treated as {}), analytics-event (-> silent {ok:true}),
    brands and admin-org-brands (-> 400 "Invalid JSON body").
    """
    raw = await request.body()
    try:
        return json.loads(raw.decode("utf-8-sig") if raw else "", parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError) as exc:
        raise MalformedJsonBody("the request body is not JSON") from exc


async def read_json_or_none(request: Request) -> Any:
    """`await req.json().catch(() => null)`: the parsed body, or None when it is empty or malformed."""
    try:
        return await read_json(request)
    except MalformedJsonBody:
        return None


# ----------------------------------------------------------------------------------------- query
def query_param(request: Request, name: str) -> str | None:
    """`new URL(req.url).searchParams.get(name)`: the first value, "" when present but empty, else None."""
    return request.query_params.get(name)


def required_query(request: Request, name: str, message: str) -> str:
    """`if (!x) throw new HttpError(400, message)`: missing OR empty is a 400 with the route's text."""
    value = query_param(request, name)
    if not value:
        raise ApiError(400, message)
    return value


# -------------------------------------------------------------------------------------------- ids
class NonUuidId(ValueError):
    """An id Postgres would refuse to cast to uuid. Deliberately NOT an ApiError (quirk 2)."""


class NullBody(TypeError):
    """`const {a} = null` threw a TypeError in Darwin: a JSON `null` body is a 500 on the routes that
    destructure it (pptx-submit, pptx-deck-submit). Deliberately NOT an ApiError."""


def destructure(body: Any) -> dict[str, Any]:
    """`const {a, b} = body` as JavaScript does it: an object gives its fields, null throws (500), and
    any other value (an array, a number, a string) gives every field undefined (so the handler's own
    400 answers)."""
    if body is None:
        raise NullBody("cannot destructure null")
    return body if isinstance(body, dict) else {}


def postgres_uuid(value: Any) -> str:
    """Quirk 2: `ownsDeck` has no UUID guard, so a non-UUID `deckId` made Postgres throw and the route
    answer 500 "Internal error" (not 403/404). Call this where Darwin queried Postgres with the id,
    BEFORE the ownership check, to keep that 500. Accepts what Postgres' uuid input accepts (with or
    without hyphens, in braces, any case); returns the canonical lower-case form."""
    if not isinstance(value, str):
        raise NonUuidId("not a string")
    text = value.strip()
    if text.lower().startswith("urn:"):
        raise NonUuidId("not a uuid")
    try:
        return str(uuid.UUID(text))
    except ValueError as exc:
        raise NonUuidId("not a uuid") from exc


# -------------------------------------------------------------------------------- JS semantics
def js_truthy(value: Any) -> bool:
    """JavaScript truthiness: `!x` in Darwin's checks. Falsy: undefined/null, false, 0, NaN, "".
    Empty lists and objects are truthy."""
    if value is None or value is False:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return not (value == 0 or (isinstance(value, float) and math.isnan(value)))
    if isinstance(value, str):
        return value != ""
    return True


_LEADING_INT = re.compile(r"^\s*([+-]?\d+)")


def js_parse_int(value: Any) -> int | None:
    """`parseInt(value, 10)`, None for NaN. JavaScript first turns the value into a string, so "1abc"
    is 1, 1.9 is 1, "  7" is 7, and "abc", "", true and null are NaN."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        # String(n) as JavaScript writes it: whole numbers below 1e21 in full ("100000000000000000000"),
        # larger ones in exponent form ("1e+21" parses to 1)
        text = f"{value:.0f}" if value.is_integer() and abs(value) < 1e21 else repr(value)
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        return None
    match = _LEADING_INT.match(text)
    return int(match.group(1)) if match else None


# -------------------------------------------------------------------------------------- responses
def json_response(body: Any, status: int = 200, *, headers: Mapping[str, str] | None = None) -> Response:
    """Darwin's `json(body, status)`: `JSON.stringify(body)` with content-type `application/json` and
    nothing else (no charset, no cache-control). The body is serialised as given: build optional
    keys with `compact()` or a `LegacyModel` so they are omitted rather than null (quirk 7)."""
    content = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return Response(content=content, status_code=status, media_type=JSON_CONTENT_TYPE, headers=dict(headers or {}))


def error_response(status: int, body: Mapping[str, Any], *, content_type: bool = True) -> Response:
    """An error body sent directly (not raised). `content_type=False` reproduces `/api/userinfo`,
    whose error responses carry NO content-type header (`api-userinfo.json`; INDEX quirk 10).
    Every other route raises `ApiError` instead."""
    content = json.dumps(dict(body), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return Response(content=content, status_code=status, media_type=JSON_CONTENT_TYPE if content_type else None)


def compact(**fields: Any) -> dict[str, Any]:
    """An object with every None-valued key LEFT OUT (quirk 7: `JSON.stringify` drops undefined, so
    Darwin's optional keys are absent, never null). Only the top level: a null INSIDE stored data (a
    kit field, a slide's chartData) is real data and stays null."""
    return {key: value for key, value in fields.items() if value is not None}


class LegacyModel(BaseModel):
    """A typed Darwin response body. `body()` omits unset optional fields (quirk 7) and uses the
    camelCase aliases Darwin's JSON has."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    def body(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


# ------------------------------------------------------------------------------- error handlers
async def _darwin_500(request: Request, exc: Exception) -> JSONResponse:
    """MalformedJsonBody / NonUuidId / NullBody: an expected client mistake that Darwin answered 500. Logged at
    INFO (no traceback): it is the contract, not a fault."""
    _log.info("legacy 500 (Darwin quirk) on %s %s: %s", request.method, request.url.path, exc)
    if uses_legacy_errors(request):
        return legacy_response(request, 500, LEGACY_INTERNAL_ERROR)
    return problem_response(request, 500, "Internal error")


def install_legacy_handlers(app: FastAPI) -> None:
    """Register the quirk exceptions' handlers (main.py calls this after install_error_handlers)."""
    app.add_exception_handler(MalformedJsonBody, _darwin_500)
    app.add_exception_handler(NonUuidId, _darwin_500)
    app.add_exception_handler(NullBody, _darwin_500)
