"""api/deps (require_user / require_admin) and api/legacy (Darwin's http.ts and the quirks), on a small app.

The routes here are synthetic; the real routes' contract is tests/contract/.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from typing import Any

import fakeredis
import pytest
from fastapi import Depends, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient

from app.api.deps import ADMIN_REQUIRED, INVALID_SESSION, MISSING_BEARER, AuthedUser, require_admin, require_user
from app.api.legacy import (
    ALL_METHODS,
    LegacyModel,
    check_method,
    compact,
    darwin_route,
    destructure,
    effective_method,
    error_response,
    js_parse_int,
    js_truthy,
    json_response,
    legacy_router,
    postgres_uuid,
    read_json,
    read_json_or_none,
    required_query,
)
from app.core.auth import TokenVerifier
from app.core.redis_client import RedisClient
from app.main import create_app
from tests.conftest import build_settings
from tests.fakes.general_service import FakeGeneralService
from tests.fakes.identity import FakeIssuer

router = legacy_router(prefix="/api", tags=["synthetic-darwin"])


@darwin_route(router, "/whoami", methods=("GET",), summary="who")
async def whoami(request: Request) -> Response:
    user = await require_user(request)
    again = await require_user(request)  # cached per request
    assert again is user
    return json_response({"userId": user.user_id, "email": user.email, "admin": user.is_admin})


@darwin_route(router, "/admin-only", methods=("GET",), summary="admin")
async def admin_only(request: Request) -> Response:
    await require_admin(request)
    return json_response({"ok": True})


@darwin_route(router, "/method-first", methods=("POST",), summary="405 before auth")
async def method_first(request: Request) -> Response:
    check_method(request, "POST")
    await require_user(request)
    return json_response(await read_json(request))


@darwin_route(router, "/auth-first", methods=("POST",), summary="405 after auth")
async def auth_first(request: Request) -> Response:
    await require_user(request)
    check_method(request, "POST", "POST only")
    return json_response(destructure(await read_json(request)))


@darwin_route(router, "/as-get", methods=("GET", "DELETE"), summary="any other method is GET")
async def as_get(request: Request) -> Response:
    return json_response({"acting": effective_method(request, distinct=("DELETE",))})


@darwin_route(router, "/lenient", methods=("POST",), summary="caught parse")
async def lenient(request: Request) -> Response:
    return json_response({"body": await read_json_or_none(request)})


@darwin_route(router, "/deck", methods=("GET",), summary="uuid quirk")
async def deck(request: Request) -> Response:
    return json_response({"id": postgres_uuid(required_query(request, "id", "id is required"))})


@darwin_route(router, "/no-ct", methods=("GET",), summary="userinfo-style error")
async def no_ct(request: Request) -> Response:
    return error_response(401, {"error": "Invalid token", "detail": "x"}, content_type=False)


@router.get("/dep-style", dependencies=[Depends(require_user)])
async def dep_style() -> dict[str, bool]:
    return {"ok": True}


@pytest.fixture
def env() -> Iterator[tuple[TestClient, FakeIssuer, FakeGeneralService]]:
    issuer = FakeIssuer()
    store = FakeGeneralService()
    app = create_app(build_settings(), RedisClient(fakeredis.FakeAsyncRedis(decode_responses=True)), store)
    app.include_router(router)
    app.state.token_verifier = issuer.verifier()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, issuer, store


# ------------------------------------------------------------------------------------------ deps
def test_a_good_token_provisions_the_user_through_the_port(env: Any):
    client, issuer, store = env
    r = client.get("/api/whoami", headers=issuer.headers("sub-1", email="a@example.com"))
    assert r.status_code == 200 and r.json()["email"] == "a@example.com" and r.json()["admin"] is False
    first = r.json()["userId"]
    assert client.get("/api/whoami", headers=issuer.headers("sub-1")).json()["userId"] == first  # same user
    assert any(c.operation == "users.get_or_create" and c.subject == "sub-1" for c in store.calls)


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer", "Bearer ", "Token abc", "Bearerabc"])
def test_no_bearer_is_missing_bearer_token(env: Any, header: str | None):
    client, _, _ = env
    r = client.get("/api/whoami", headers={} if header is None else {"Authorization": header})
    assert (r.status_code, r.json(), r.headers["content-type"]) == (401, {"error": MISSING_BEARER}, "application/json")


def test_the_scheme_is_case_insensitive(env: Any):
    client, issuer, _ = env
    token = issuer.token("sub-1")
    assert client.get("/api/whoami", headers={"Authorization": f"bEaReR   {token}"}).status_code == 200


@pytest.mark.parametrize("bad", [{"expired": True}, {"audience": "x"}, {"issuer": "https://x/"},
                                 {"signed_by_stranger": True}])
def test_a_bad_token_is_invalid_or_expired_session(env: Any, bad: dict[str, Any]):
    client, issuer, _ = env
    r = client.get("/api/whoami", headers=issuer.headers("sub-1", **bad))
    assert (r.status_code, r.json()) == (401, {"error": INVALID_SESSION})


def test_an_unconfigured_issuer_is_a_500_internal_error(env: Any):
    client, issuer, _ = env
    client.app.state.token_verifier = TokenVerifier(issuer="", audience="", source=None)
    r = client.get("/api/whoami", headers=issuer.headers("sub-1"))
    assert (r.status_code, r.json()) == (500, {"error": "Internal error"})


def test_the_verifier_is_built_from_settings_when_none_is_set(env: Any):
    client, issuer, _ = env
    client.app.state.token_verifier = None
    r = client.get("/api/whoami", headers=issuer.headers("sub-1"))
    assert r.status_code == 500  # no AUTH_* configured in the test settings: cannot verify
    assert isinstance(client.app.state.token_verifier, TokenVerifier)


def test_admin_needs_is_admin(env: Any):
    client, issuer, store = env
    r = client.get("/api/admin-only", headers=issuer.headers("plain"))
    assert (r.status_code, r.json()) == (403, {"error": ADMIN_REQUIRED})
    client.portal.call(lambda: store.make_user("boss", admin=True))
    assert client.get("/api/admin-only", headers=issuer.headers("boss")).json() == {"ok": True}
    assert client.get("/api/admin-only").status_code == 401  # auth first


def test_require_user_also_works_as_a_dependency(env: Any):
    client, issuer, _ = env
    assert client.get("/api/dep-style").json() == {"error": MISSING_BEARER}
    assert client.get("/api/dep-style", headers=issuer.headers("s")).json() == {"ok": True}


def test_authed_user_carries_the_callers_context(env: Any):
    assert {"ctx", "profile", "subject", "user_id"} <= set(AuthedUser.__dataclass_fields__)


# ------------------------------------------------------------------------------- method order
def test_405_before_auth_and_after_auth(env: Any):
    client, issuer, _ = env
    assert (client.get("/api/method-first").status_code, client.get("/api/method-first").json()) == \
        (405, {"error": "Method not allowed"})
    assert client.get("/api/auth-first").json() == {"error": MISSING_BEARER}
    r = client.get("/api/auth-first", headers=issuer.headers("s"))
    assert (r.status_code, r.json()) == (405, {"error": "POST only"})


def test_every_method_reaches_the_handler_but_only_documented_ones_are_in_the_schema(env: Any):
    client, _, _ = env
    for method in ALL_METHODS:
        if method == "HEAD":
            continue
        r = client.request(method, "/api/as-get")
        assert r.json() == {"acting": "DELETE" if method == "DELETE" else "GET"}, method
    paths = client.app.openapi()["paths"]
    assert set(paths["/api/as-get"]) == {"get", "delete"}


# --------------------------------------------------------------------------------------- bodies
@pytest.mark.parametrize("raw", [b"", b"{", b"NaN", b"[1,", b"\xff\xfe"])
def test_malformed_json_is_500_internal_error(env: Any, raw: bytes):
    client, issuer, _ = env
    r = client.post("/api/method-first", content=raw, headers=issuer.headers("s"))
    assert (r.status_code, r.json()) == (500, {"error": "Internal error"})


def test_any_json_value_is_passed_through(env: Any):
    client, issuer, _ = env
    for value in ([1], 3, "x", {"a": None}):
        assert client.post("/api/method-first", json=value, headers=issuer.headers("s")).json() == value


def test_destructuring_null_is_500_and_a_non_object_has_no_fields(env: Any):
    client, issuer, _ = env
    assert client.post("/api/auth-first", content=b"null", headers=issuer.headers("s")).status_code == 500
    assert client.post("/api/auth-first", json=[1, 2], headers=issuer.headers("s")).json() == {}


def test_read_json_or_none_catches(env: Any):
    client, _, _ = env
    assert client.post("/api/lenient", content=b"{oops").json() == {"body": None}
    assert client.post("/api/lenient", json={"a": 1}).json() == {"body": {"a": 1}}


# ------------------------------------------------------------------------------------- ids, query
def test_non_uuid_is_500_and_uuid_forms_are_canonical(env: Any):
    client, _, _ = env
    assert client.get("/api/deck?id=deck-1").json() == {"error": "Internal error"}
    assert client.get("/api/deck?id=urn:uuid:3fa85f64-5717-4562-b3fc-2c963f66afa6").status_code == 500
    for form in ("3FA85F64-5717-4562-B3FC-2C963F66AFA6", "3fa85f6457174562b3fc2c963f66afa6",
                 "{3fa85f64-5717-4562-b3fc-2c963f66afa6}"):
        assert client.get(f"/api/deck?id={form}").json() == {"id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"}
    assert client.get("/api/deck?id=").json() == {"error": "id is required"}
    assert client.get("/api/deck").json() == {"error": "id is required"}


def test_userinfo_style_errors_carry_no_content_type(env: Any):
    client, _, _ = env
    r = client.get("/api/no-ct")
    assert r.status_code == 401 and "content-type" not in r.headers
    assert r.content == b'{"error":"Invalid token","detail":"x"}'


# -------------------------------------------------------------------------------- JS semantics
@pytest.mark.parametrize(("value", "expected"), [
    ("1", 1), ("1abc", 1), ("  7", 7), ("-3", -3), ("+4", 4), (1.9, 1), (12, 12), ("abc", None), ("", None),
    (True, None), (None, None), ([1], None), (math.nan, None), ("0x10", 0), ("1e3", 1), (1e20, 10**20), (1e21, 1), (2.0, 2),
])
def test_js_parse_int(value: Any, expected: int | None):
    assert js_parse_int(value) == expected


@pytest.mark.parametrize(("value", "expected"), [
    (None, False), (False, False), (0, False), (0.0, False), (math.nan, False), ("", False),
    (True, True), (1, True), ("0", True), ([], True), ({}, True),
])
def test_js_truthy(value: Any, expected: bool):
    assert js_truthy(value) is expected


def test_json_response_is_darwins_json_helper():
    r = json_response({"jobId": "j", "ünï": "x"}, 202)
    assert r.status_code == 202 and r.headers["content-type"] == "application/json"
    assert r.body == '{"jobId":"j","ünï":"x"}'.encode()
    assert "cache-control" not in r.headers


def test_optional_keys_are_omitted_never_null():
    assert compact(status="done", result={"a": None}, error=None) == {"status": "done", "result": {"a": None}}

    class Status(LegacyModel):
        status: str
        error: str | None = None

    assert Status(status="pending").body() == {"status": "pending"}
