"""core/request_id: read the gateway's id, else generate one; log it; return it."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.core import telemetry
from app.core.logging_config import current_request_id
from app.core.request_id import accept_request_id
from tests.conftest import build_settings


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def test_the_gateway_id_is_echoed(client: TestClient):
    assert client.get("/healthz", headers={"X-Request-ID": "gw-abc.123:x"}).headers["x-request-id"] == "gw-abc.123:x"


def test_a_missing_id_is_generated(client: TestClient):
    assert _is_uuid(client.get("/healthz").headers["x-request-id"])


@pytest.mark.parametrize("bad", ["has space", "x" * 129, "line\\nbreak", "quote\"d", "<script>"])
def test_an_unsafe_id_is_replaced_not_logged(bad: str):
    assert _is_uuid(accept_request_id(bad))


def test_each_request_gets_its_own_generated_id(client: TestClient):
    ids = {client.get("/healthz").headers["x-request-id"] for _ in range(3)}
    assert len(ids) == 3


def test_the_header_name_is_configurable(make_app: Callable[..., FastAPI]):
    app = make_app(build_settings(request_id_header="X-Correlation-ID"))
    with TestClient(app) as c:
        r = c.get("/healthz", headers={"X-Correlation-ID": "corr-1"})
    assert r.headers["x-correlation-id"] == "corr-1"


def test_the_id_is_bound_for_handlers_and_logged(make_app: Callable[..., FastAPI], caplog: pytest.LogCaptureFixture):
    app = make_app()
    router = APIRouter(tags=["probe"])
    seen: dict[str, str] = {}

    @router.get("/probe")
    async def probe() -> dict[str, str]:
        seen["id"] = current_request_id()
        logging.getLogger("app.probe").info("inside the handler")
        return {}

    app.include_router(router)
    with caplog.at_level(logging.INFO, logger="app"), TestClient(app) as c:
        c.get("/probe", headers={"X-Request-ID": "gw-log-1"})
    assert seen["id"] == "gw-log-1"
    handler_lines = [r for r in caplog.records if r.name == "app.probe"]
    access = [r for r in caplog.records if r.name == "app.access" and "/probe" in r.getMessage()]
    assert handler_lines and getattr(handler_lines[0], "request_id", None) == "gw-log-1"
    assert access and getattr(access[0], "request_id", None) == "gw-log-1"
    assert current_request_id() == "-"  # unbound after the request


def test_the_access_line_never_includes_the_query_string(client: TestClient, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.INFO, logger="app.access"):
        client.get("/healthz?deckId=secret-deck")
    assert all("secret-deck" not in r.getMessage() for r in caplog.records)


def test_the_metric_gets_the_route_template_and_feature(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(telemetry, "record_request", lambda **kw: calls.append(kw))
    client.get("/healthz")
    client.get("/no/such/path")
    assert calls[0] == {"route": "/healthz", "method": "GET", "status": 200,
                        "duration_s": calls[0]["duration_s"], "feature": "health"}
    assert calls[1]["route"] == "unmatched" and calls[1]["status"] == 404
