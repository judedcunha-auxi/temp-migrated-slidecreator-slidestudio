"""Shared fixtures. No test needs a real Redis, a network or a .env file.

- `make_settings(**overrides)` builds Settings from explicit values only
  (`_env_file=None`), so a developer's .env can never change a test's result;
  `build_engine_settings(**overrides)` and `build_ai_settings(**overrides)` do the same for the
  engine's and the AI features' settings (the latter never carries a real provider key).
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

from app.config.ai import AISettings
from app.config.darwin import DarwinSettings
from app.config.engine import EngineSettings
from app.config.settings import Settings
from app.core.redis_client import RedisClient, RedisStore
from app.main import create_app


def build_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"environment": "test"}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def build_engine_settings(**overrides: Any) -> EngineSettings:
    """`SLIDE_ENGINE_*` settings from explicit values only (no environment, no .env)."""
    return EngineSettings(_env_file=None, **overrides)  # type: ignore[call-arg]


def build_ai_settings(**overrides: Any) -> AISettings:
    """AI settings from explicit values only: no environment, no .env, no provider key unless given.
    Every AI test builds its settings here, so a developer's real key can never reach a test."""
    values: dict[str, Any] = {"anthropic_api_key": "", "gemini_api_key": ""}
    values.update(overrides)
    return AISettings(_env_file=None, **values)  # type: ignore[call-arg]


def build_darwin_settings(**overrides: Any) -> DarwinSettings:
    """Darwin's settings from explicit values only: no environment, no .env, no OpenAI key unless given."""
    values: dict[str, Any] = {"openai_api_key": ""}
    values.update(overrides)
    return DarwinSettings(_env_file=None, **values)  # type: ignore[call-arg]


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


class _RealProviderForbidden(AssertionError):
    pass


@pytest.fixture(autouse=True)
def no_paid_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may build a real provider client, so no test can make a paid model call.

    The Anthropic, Gemini and OpenAI SDK clients refuse to be constructed for the whole run, whatever
    keys the environment holds. Provider tests hand their provider a fake client object instead
    (tests/core/llm, tests/core/darwin), and everything above the providers runs on tests/fakes/llm.py,
    tests/fakes/storyline_model.py and tests/fakes/image_gen.py.
    """
    import anthropic
    import openai
    from google import genai

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise _RealProviderForbidden("a test tried to build a real model client (paid call)")

    monkeypatch.setattr(anthropic, "Anthropic", refuse)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", refuse)
    monkeypatch.setattr(genai, "Client", refuse)
    monkeypatch.setattr(openai, "OpenAI", refuse)
    monkeypatch.setattr(openai, "AsyncOpenAI", refuse)
