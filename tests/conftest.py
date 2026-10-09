"""Shared fixtures. No test needs a real Redis, a network or a .env file.

- `make_settings(**overrides)` builds Settings from explicit values only
  (`_env_file=None`), so a developer's .env can never change a test's result.
- `fake_store` is the real RedisClient over fakeredis: the same code path as
  production, minus the server.
- `make_app` / `client` build the app through create_app(), exactly as main.py does.
  Storage defaults to STORAGE_BACKEND (fake); pass `storage=` for a
  tests/fakes/general_service.FakeGeneralService you want to steer.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import fakeredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.core.redis_client import RedisClient, RedisStore
from app.main import create_app


def build_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"environment": "test"}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    return build_settings


@pytest.fixture
def fake_store() -> RedisStore:
    return RedisClient(fakeredis.FakeAsyncRedis(decode_responses=True))


@pytest.fixture
def make_app(fake_store: RedisStore) -> Callable[..., FastAPI]:
    def _make(settings: Settings | None = None, redis: RedisStore | None = None, storage: Any = None) -> FastAPI:
        return create_app(settings or build_settings(), redis if redis is not None else fake_store, storage)

    return _make


@pytest.fixture
def client(make_app: Callable[..., FastAPI]) -> Iterator[TestClient]:
    with TestClient(make_app(), raise_server_exceptions=False) as c:
        yield c
