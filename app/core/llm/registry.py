"""Which models exist, what they cost, and what each can be handed.

One place, so the rest of the service never hard-codes a model id or a price. Prices are USD per
million tokens, the Claude API's first-party rates (cached 2026-09-25 in the `claude-api` reference):
input, output, cache read, and cache write at the 5-minute TTL (1.25x input). They can be replaced
without a release through `LLM_PRICING_OVERRIDES` (`app/core/llm/pricing.py`).

Ported from Slide Studio `server/llm/registry.py` (migration plan §4.1, K/R): the Opus 5.x, Sonnet
5.x, Fable 5.1 and Haiku 4.5 ids are added, and the Sonnet 4.6 benchmark entry is dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float


@dataclass(frozen=True)
class Caps:
    """What a model can be handed."""

    images: bool = True
    pdf: bool = True


@dataclass(frozen=True)
class Model:
    id: str
    label: str
    provider: str  # "anthropic" | "gemini"
    pricing: Pricing
    caps: Caps = Caps()
    #: The Claude API's server-side refusal fallback (`fallbacks: "default"`) applies to this model.
    fallbacks: bool = False
    #: The `thinking` parameter for "as little reasoning as possible" (the critic, the role
    #: annotator), or None to omit the parameter and rely on low effort. Claude Opus 5.5 and Fable
    #: 5.1 reject `{"type": "disabled"}`; Claude Sonnet 5.5 takes `between_tools` instead.
    minimal_thinking: dict[str, Any] | None = None
    #: Accepts `output_config.effort`.
    effort: bool = True
    #: Accepts adaptive thinking (`thinking: {"type": "adaptive"}`).
    adaptive: bool = True


_OPUS_5_5 = Model("claude-opus-5-5", "Claude Opus 5.5", "anthropic", Pricing(4.0, 20.0, 0.20, 5.0), fallbacks=True)
_OPUS_5 = Model("claude-opus-5", "Claude Opus 5", "anthropic", Pricing(5.0, 25.0, 0.50, 6.25), fallbacks=True,
                minimal_thinking={"type": "disabled"})
_SONNET_5_5 = Model("claude-sonnet-5-5", "Claude Sonnet 5.5", "anthropic", Pricing(2.0, 10.0, 0.20, 2.5),
                    fallbacks=True, minimal_thinking={"type": "between_tools"})
_SONNET_5 = Model("claude-sonnet-5", "Claude Sonnet 5", "anthropic", Pricing(2.0, 10.0, 0.20, 2.5),
                  minimal_thinking={"type": "disabled"})
_FABLE_5_1 = Model("claude-fable-5-1", "Claude Fable 5.1", "anthropic", Pricing(10.0, 50.0, 0.25, 12.5),
                   fallbacks=True)
_HAIKU_4_5 = Model("claude-haiku-4-5-20251001", "Claude Haiku 4.5", "anthropic", Pricing(1.0, 5.0, 0.10, 1.25),
                   effort=False, adaptive=False)
#: Gemini paid-tier rates through 2026-12-31 (output includes thinking; implicit caching has no write
#: cost). They double on 2027-01-01: one LLM_PRICING_OVERRIDES entry.
_GEMINI_FLASH = Model("gemini-3.8-flash", "Gemini 3.8 Flash", "gemini", Pricing(0.75, 3.75, 0.075, 0.0))

MODELS: tuple[Model, ...] = (_OPUS_5_5, _OPUS_5, _SONNET_5_5, _SONNET_5, _FABLE_5_1, _HAIKU_4_5, _GEMINI_FLASH)

#: Other names a configuration may use for a model (the alias without a date suffix).
ALIASES: dict[str, str] = {"claude-haiku-4-5": _HAIKU_4_5.id}

#: The design model when nothing is configured.
DEFAULT_DESIGN_MODEL = _OPUS_5_5.id
#: The critic's model when nothing is configured: the current-generation cheaper second model.
DEFAULT_CRITIC_MODEL = _SONNET_5_5.id

_BY_ID: dict[str, Model] = {m.id: m for m in MODELS}

PROVIDER_LABEL = {"anthropic": "Claude", "gemini": "Gemini"}


def get(model_id: str) -> Model:
    """The model with this id (or alias). KeyError for an unknown one."""
    key = model_id.strip().lower()
    return _BY_ID[ALIASES.get(key, key)]


def known(model_id: str) -> bool:
    try:
        get(model_id)
    except KeyError:
        return False
    return True


def ids(provider: str | None = None) -> list[str]:
    return [m.id for m in MODELS if provider is None or m.provider == provider]
