"""model id -> the provider that serves it, built from settings.

`resolver(settings)` returns the `ProviderResolver` the design loop, the critic and brand extraction
take. Providers are built once per settings object and reused (each holds an HTTP client). Asking for
a Gemini model while `LLM_GEMINI_ENABLED` is off, or for a model the registry does not know, raises
`ProviderUnavailable` before any call is made.
"""

from __future__ import annotations

import threading

from app.config.ai import AISettings, ai_settings
from app.core.llm import registry
from app.core.llm.anthropic_provider import AnthropicProvider
from app.core.llm.gemini_provider import GeminiProvider
from app.core.llm.ports import LLMProvider, ProviderResolver
from app.core.llm.types import ProviderUnavailable

_lock = threading.Lock()
_cache: dict[tuple[int, str], LLMProvider] = {}


def provider_for(model_id: str, settings: AISettings | None = None) -> LLMProvider:
    s = settings if settings is not None else ai_settings
    try:
        model = registry.get(model_id)
    except KeyError:
        raise ProviderUnavailable(f"unknown model {model_id!r}") from None
    if model.provider == "gemini" and not s.llm_gemini_enabled:
        raise ProviderUnavailable("Gemini is switched off (LLM_GEMINI_ENABLED).")
    key = (id(s), model.provider)
    with _lock:
        provider = _cache.get(key)
        if provider is None:
            provider = AnthropicProvider(s) if model.provider == "anthropic" else GeminiProvider(s)
            _cache[key] = provider
        return provider


def resolver(settings: AISettings | None = None) -> ProviderResolver:
    s = settings if settings is not None else ai_settings
    return lambda model_id: provider_for(model_id, s)


def reset() -> None:
    """Forget the built providers (tests)."""
    with _lock:
        _cache.clear()
