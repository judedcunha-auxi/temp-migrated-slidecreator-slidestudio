"""api/routes/health: /healthz reports the build, /readyz gates traffic, neither leaks internals."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import health
from tests.conftest import build_settings
from tests.fakes.general_service import FakeGeneralService


class DownRedis:
    async def ping(self) -> bool:
        raise ConnectionError("Error 111 connecting to secret-host.internal:6380. Connection refused.")

    async def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_cached_build_info(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(health, "_build_info_cache", None)
    monkeypatch.setattr(health, "BUILD_INFO_PATH", Path("does-not-exist.json"))


def test_healthz_without_build_info_is_just_ok(client: TestClient):
    r = client.get("/healthz")
    assert (r.status_code, r.json()) == (200, {"status": "ok"})


def test_healthz_reports_the_stamped_build(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    info = {"sha": "a" * 40, "branch": "staging", "dirty": False, "builtAt": "2026-10-08T10:00:00+00:00"}
    path = tmp_path / "build_info.json"
    path.write_text(json.dumps(info), encoding="utf-8")
    monkeypatch.setattr(health, "BUILD_INFO_PATH", path)
    body = client.get("/healthz").json()
    assert body == {"status": "ok", "sha": "a" * 40, "builtAt": "2026-10-08T10:00:00+00:00", "dirty": False}


def test_healthz_flags_a_dirty_build():
    assert health.liveness_body({"sha": "b" * 40, "builtAt": "t", "dirty": True})["dirty"] is True


def test_load_build_info_tolerates_garbage(tmp_path: Path):
    bad = tmp_path / "build_info.json"
    bad.write_text("[1, 2", encoding="utf-8")
    assert health.load_build_info(bad) == {}
    bad.write_text("[1, 2]", encoding="utf-8")
    assert health.load_build_info(bad) == {}


def test_healthz_does_not_touch_dependencies(make_app: Callable[..., FastAPI]):
    with TestClient(make_app(redis=DownRedis())) as c:
        assert c.get("/healthz").status_code == 200


def test_readyz_ok_when_redis_answers(client: TestClient):
    r = client.get("/readyz")
    assert (r.status_code, r.json()) == (200, {"status": "ok", "redis": "ok", "storage": "skipped", "config": "ok"})


def test_readyz_503_when_redis_is_down_without_naming_the_host(make_app: Callable[..., FastAPI]):
    with TestClient(make_app(redis=DownRedis())) as c:
        r = c.get("/readyz")
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "redis": "unreachable", "storage": "skipped", "config": "ok"}
    assert "secret-host" not in r.text and "6380" not in r.text


def test_readyz_503_on_bad_production_config_without_naming_the_variable(make_app: Callable[..., FastAPI]):
    with TestClient(make_app(build_settings(environment="production"))) as c:
        r = c.get("/readyz")
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "redis": "ok", "storage": "skipped", "config": "invalid"}
    assert "APPLICATIONINSIGHTS" not in r.text and "REDIS_URL" not in r.text


def test_readyz_reports_but_tolerates_bad_config_outside_production(make_app: Callable[..., FastAPI]):
    with TestClient(make_app(build_settings(environment="development", redis_protocol=9))) as c:
        r = c.get("/readyz")
    assert (r.status_code, r.json()["config"]) == (200, "invalid")


def test_health_endpoints_need_no_credentials(client: TestClient):
    for path in ("/healthz", "/readyz"):
        assert client.get(path).status_code == 200


# ------------------------------------------------------------------ storage (Phase 4)
def test_readyz_storage_is_neutral_for_the_fake(make_app: Callable[..., FastAPI]):
    with TestClient(make_app(storage=FakeGeneralService())) as c:
        r = c.get("/readyz")
    assert (r.status_code, r.json()["storage"]) == (200, "skipped")


def test_readyz_storage_ok_for_the_local_adapter(make_app: Callable[..., FastAPI], tmp_path: Path):
    settings = build_settings(storage_backend="local", scratch_root=str(tmp_path))
    with TestClient(make_app(settings)) as c:
        r = c.get("/readyz")
    assert (r.status_code, r.json()["storage"]) == (200, "ok")
    assert all(p.resolve().is_relative_to(tmp_path.resolve()) for p in tmp_path.rglob("*"))


def test_readyz_503_when_storage_is_unreachable_without_naming_the_host(make_app: Callable[..., FastAPI]):
    fake = FakeGeneralService()
    fake.fail_health = True
    with TestClient(make_app(storage=fake)) as c:
        r = c.get("/readyz")
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "redis": "ok", "storage": "unreachable", "config": "ok"}
    assert "general-service.internal" not in r.text


def test_readyz_503_while_the_general_adapter_is_a_stub(make_app: Callable[..., FastAPI]):
    settings = build_settings(storage_backend="general", general_service_url="https://general.example")
    with TestClient(make_app(settings)) as c:
        r = c.get("/readyz")
    assert r.status_code == 503
    assert r.json()["storage"] == "unavailable"
    assert "D5" not in r.text and "NotImplemented" not in r.text
