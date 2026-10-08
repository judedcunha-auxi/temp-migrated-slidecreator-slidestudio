"""app/main: the app boots, routers follow the include convention, CORS admits only the static origin."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import app.main as main
from app.core.redis_client import RedisClient
from tests.conftest import build_settings

ORIGIN = "https://darwin.example.com"


def test_the_module_level_app_exists():
    assert isinstance(main.app, FastAPI)


def test_routers_set_their_own_prefix_and_tags():
    """The served path and feature tag must be the ones on the route object: the
    route-controls test and the per-endpoint metric both read the route object."""
    paths = set(main.create_app(build_settings()).openapi()["paths"])
    for router in main.ROUTERS:
        for route in router.routes:
            assert isinstance(route, APIRoute)
            assert route.path in paths, f"{route.path} is not served at that path; set the prefix on the router"
            assert route.tags, f"{route.path} has no feature tag"


def test_cors_admits_the_configured_origin(make_app: Callable[..., FastAPI]):
    with TestClient(make_app(build_settings(cors_allowed_origins=ORIGIN))) as c:
        r = c.options("/readyz", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"})
        assert r.headers["access-control-allow-origin"] == ORIGIN
        assert "x-request-id" in r.headers  # the request id wraps CORS too
        simple = c.get("/healthz", headers={"Origin": ORIGIN})
        assert simple.headers["access-control-allow-origin"] == ORIGIN
        assert "X-Request-ID" in simple.headers["access-control-expose-headers"]


def test_cors_refuses_any_other_origin(make_app: Callable[..., FastAPI]):
    with TestClient(make_app(build_settings(cors_allowed_origins=ORIGIN))) as c:
        r = c.options("/readyz", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
        assert "access-control-allow-origin" not in r.headers
        assert "access-control-allow-origin" not in c.get("/healthz", headers={"Origin": "https://evil.example"}).headers


def test_no_cors_headers_when_no_origin_is_configured(client: TestClient):
    r = client.get("/healthz", headers={"Origin": ORIGIN})
    assert "access-control-allow-origin" not in r.headers


def test_the_app_creates_and_closes_its_own_redis(monkeypatch: pytest.MonkeyPatch, fake_store: Any):
    closed: list[bool] = []

    class Tracking(RedisClient):
        async def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(main.RedisClient, "from_settings",
                        classmethod(lambda cls, s: Tracking(fake_store._client)))
    app = main.create_app(build_settings())
    with TestClient(app) as c:
        assert c.get("/readyz").json()["redis"] == "ok"
    assert closed == [True]
    assert app.state.redis is None


def test_config_problems_are_logged_at_startup(caplog: pytest.LogCaptureFixture, fake_store: Any):
    with caplog.at_level("ERROR", logger="app.main"):
        main.create_app(build_settings(redis_protocol=7), fake_store)
    assert any("REDIS_PROTOCOL" in r.getMessage() for r in caplog.records)


def test_no_interactive_docs_pages(client: TestClient):
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 200
