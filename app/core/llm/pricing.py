"""Tokens -> USD. Every turn result carries what it cost, priced here.

`cost_of(usage, model)` prices one round with the model that ran it: regular input, output, cache
reads and cache writes, each at its own rate (`registry.Pricing`, USD per million tokens). A model's
prices can be replaced without a release through `LLM_PRICING_OVERRIDES`
(`app.config.ai.AISettings.pricing_overrides`), for a price change or a negotiated rate; an override
may name some of the four rates and keep the rest.

An unknown model is a `KeyError`, never a zero: an unpriced call would spend money the ledger could
not account for, so the registry and `check_config` keep unknown ids out before any call is made.
"""

from __future__ import annotations

from dataclasses import replace

from app.config.ai import AISettings, ai_settings
from app.core.llm import registry
from app.core.llm.types import Usage

_PER_MILLION = 1_000_000


def pricing_for(model_id: str, settings: AISettings | None = None) -> registry.Pricing:
    """The model's prices, with any `LLM_PRICING_OVERRIDES` entry applied."""
    model = registry.get(model_id)
    s = settings if settings is not None else ai_settings
    try:
        overrides = s.pricing_overrides()
    except ValueError:
        overrides = {}  # check_config reports it; the registry's prices stand meanwhile
    override = overrides.get(model.id) or overrides.get(model_id)
    if not override:
        return model.pricing
    return replace(model.pricing, **override)


def cost_of(usage: Usage, model_id: str, settings: AISettings | None = None) -> float:
    """What one round's tokens cost on `model_id`, in USD (rounded to 1e-6)."""
    p = pricing_for(model_id, settings)
    total = (usage.input * p.input + usage.output * p.output
             + usage.cache_read * p.cache_read + usage.cache_write * p.cache_write) / _PER_MILLION
    return round(total, 6)


def priced(usage: Usage, model_id: str, settings: AISettings | None = None) -> Usage:
    """`usage` with its `cost_usd` set from `model_id`'s prices (the same object, returned)."""
    usage.cost_usd = cost_of(usage, model_id, settings)
    return usage
