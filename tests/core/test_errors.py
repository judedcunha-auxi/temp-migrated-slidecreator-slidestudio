"""core/errors: the two error formats of decision D32.

Legacy `{"error": "<message>"}` on Darwin routes (route class or tag marker);
RFC 9457 Problem Details on everything else. Neither leaks internals.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import (
    GENERIC_5XX_DETAIL,
    LEGACY_ERRORS_TAG,
    LEGACY_INTERNAL_ERROR,
    LEGACY_VALIDATION_MESSAGE,
    PROBLEM_JSON,
    ApiError,
    legacy_router,
    problem_title,
    problem_type,
)

SECRET = "internal-host.redis.cache.windows.net:6380 password=hunter2"


class NewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")  # new routes reject unknown fields
    language: str = Field(max_length=5)
    slides: list[int] = Field(max_length=3)


def _routers() -> list[APIRouter]:
    legacy = legacy_router(prefix="/api", tags=["darwin"])

    @legacy.get("/decks")
    async def decks() -> dict[str, str]:
        raise ApiError(401, "Missing bearer token")

    @legacy.post("/refine")
    async def refine(body: NewBody) -> dict[str, str]:
        return {"ok": "yes"}

    @legacy.get("/crash")
    async def legacy_crash() -> None:
        raise RuntimeError(SECRET)

    @legacy.get("/busy")
    async def legacy_busy() -> None:
        raise ApiError(429, "Too many requests", headers={"Retry-After": "30"})

    tagged = APIRouter(prefix="/api", tags=["darwin-tagged", LEGACY_ERRORS_TAG])

    @tagged.get("/tagged")
    async def tagged_route() -> None:
        raise HTTPException(status_code=403, detail="Forbidden")

    new = APIRouter(prefix="/api", tags=["new"])

    @new.post("/master-import")
    async def master_import(body: NewBody) -> dict[str, str]:
        return {"ok": "yes"}

    @new.get("/job-status")
    async def job_status() -> None:
        raise ApiError(404, "No such job.")

    @new.get("/crash-new")
    async def new_crash() -> None:
        raise RuntimeError(SECRET)

    @new.get("/busy-new")
    async def new_busy() -> None:
        raise ApiError(429, "Slow down.", headers={"Retry-After": "12"})

    @new.get("/upstream")
    async def upstream() -> None:
        raise ApiError(502, f"PptxRender at {SECRET} failed")

    return [legacy, tagged, new]


@pytest.fixture
def api(make_app: Callable[..., FastAPI]) -> Iterator[TestClient]:
    app = make_app()
    for router in _routers():
        app.include_router(router)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---- legacy format ---------------------------------------------------------------

def test_legacy_route_keeps_the_darwin_error_body(api: TestClient):
    r = api.get("/api/decks")
    assert r.status_code == 401
    assert r.json() == {"error": "Missing bearer token"}
    assert r.headers["content-type"].startswith("application/json")


def test_legacy_route_validation_failure_is_a_400_error_body(api: TestClient):
    r = api.post("/api/refine", json={"language": "far-too-long", "slides": [1]})
    assert r.status_code == 400
    assert r.json() == {"error": LEGACY_VALIDATION_MESSAGE}


def test_legacy_route_crash_is_generic(api: TestClient):
    """Darwin's errorResponse: anything that is not an HttpError is 500 "Internal error"."""
    r = api.get("/api/crash")
    assert r.status_code == 500
    assert r.json() == {"error": LEGACY_INTERNAL_ERROR}
    assert "hunter2" not in r.text and "redis" not in r.text


def test_legacy_429_carries_retry_after(api: TestClient):
    r = api.get("/api/busy")
    assert (r.status_code, r.json(), r.headers["retry-after"]) == (429, {"error": "Too many requests"}, "30")


def test_the_tag_marker_also_selects_the_legacy_body(api: TestClient):
    r = api.get("/api/tagged")
    assert (r.status_code, r.json()) == (403, {"error": "Forbidden"})


def test_legacy_errors_still_carry_the_request_id_header(api: TestClient):
    r = api.get("/api/decks", headers={"X-Request-ID": "gw-123"})
    assert r.headers["x-request-id"] == "gw-123"


# ---- Problem Details -------------------------------------------------------------

def test_new_route_error_is_problem_details(api: TestClient):
    r = api.get("/api/job-status", headers={"X-Request-ID": "gw-456"})
    assert r.status_code == 404
    assert r.headers["content-type"] == PROBLEM_JSON
    assert r.json() == {
        "type": "/errors/not-found",
        "title": "Not Found",
        "status": 404,
        "detail": "No such job.",
        "requestId": "gw-456",
    }


def test_validation_failure_is_422_with_an_errors_list_by_field(api: TestClient):
    r = api.post("/api/master-import", json={"language": "far-too-long", "slides": [1, 2, 3, 4], "extra": 1})
    body = r.json()
    assert r.status_code == 422
    assert r.headers["content-type"] == PROBLEM_JSON
    assert body["type"] == "/errors/validation-failed"
    assert body["title"] == "Validation failed"
    assert body["requestId"]
    fields = {e["field"] for e in body["errors"]}
    assert {"language", "slides", "extra"} <= fields
    assert all(set(e) == {"field", "message"} for e in body["errors"])


def test_validation_errors_do_not_echo_the_input_back(api: TestClient):
    r = api.post("/api/master-import", json={"language": "hunter2-is-secret", "slides": []})
    assert "hunter2" not in r.text


def test_unhandled_error_is_a_generic_500_problem(api: TestClient):
    r = api.get("/api/crash-new", headers={"X-Request-ID": "gw-789"})
    body = r.json()
    assert r.status_code == 500
    assert body["detail"] == GENERIC_5XX_DETAIL
    assert body["requestId"] == "gw-789"
    assert r.headers["x-request-id"] == "gw-789"
    assert "hunter2" not in r.text and "Traceback" not in r.text and "RuntimeError" not in r.text


def test_a_deliberate_5xx_never_shows_its_internal_detail(api: TestClient):
    r = api.get("/api/upstream")
    assert r.status_code == 502
    assert r.json()["detail"] == GENERIC_5XX_DETAIL
    assert "hunter2" not in r.text


def test_problem_429_carries_retry_after(api: TestClient):
    r = api.get("/api/busy-new")
    assert r.status_code == 429
    assert r.headers["retry-after"] == "12"
    assert r.json()["type"] == "/errors/rate-limited"


def test_an_unmatched_path_gets_problem_details(api: TestClient):
    r = api.get("/api/does-not-exist")
    assert r.status_code == 404
    assert r.headers["content-type"] == PROBLEM_JSON


def test_a_wrong_method_gets_problem_details_405(api: TestClient):
    r = api.delete("/api/job-status")
    assert r.status_code == 405
    assert r.json()["type"] == "/errors/method-not-allowed"


def test_problem_types_and_titles():
    assert problem_type(422) == "/errors/validation-failed"
    assert problem_type(418) == "/errors/error"
    assert problem_type(599) == "/errors/internal-error"
    assert problem_title(404) == "Not Found"
    assert problem_title(799) == "Error"


def test_a_deliberate_legacy_5xx_keeps_darwins_fixed_text(make_app):
    """The Connector reads Darwin's 502 "PPTX service error" as "still running": it must pass through."""
    from fastapi.testclient import TestClient

    app = make_app()
    legacy = legacy_router(prefix="/api", tags=["darwin-test"])

    @legacy.get("/pptx-like")
    async def pptx_like() -> None:
        raise ApiError(502, "PPTX service error")

    app.include_router(legacy)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/api/pptx-like")
    assert (r.status_code, r.json()) == (502, {"error": "PPTX service error"})
