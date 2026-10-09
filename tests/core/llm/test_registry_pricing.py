"""The model registry and the cost table (app/core/llm/registry.py, pricing.py) and their settings."""

from __future__ import annotations

import pytest

from app.config.settings import check_config
from app.core.llm import pricing, registry
from app.core.llm.types import Usage
from tests.conftest import build_ai_settings, build_engine_settings, build_settings


def test_the_current_claude_ids_are_registered():
    for model_id in ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-sonnet-5",
                     "claude-fable-5-1", "claude-haiku-4-5-20251001"):
        assert registry.get(model_id).provider == "anthropic"
    assert registry.get("claude-haiku-4-5").id == "claude-haiku-4-5-20251001", "the undated alias"
    assert registry.get("gemini-3.8-flash").provider == "gemini"
    with pytest.raises(KeyError):
        registry.get("claude-sonnet-4-6")


def test_defaults_are_opus_5_5_for_design_and_sonnet_5_5_for_the_critic():
    s = build_ai_settings()
    assert s.llm_design_model == registry.DEFAULT_DESIGN_MODEL == "claude-opus-5-5"
    assert s.llm_critic_model == registry.DEFAULT_CRITIC_MODEL == "claude-sonnet-5-5"


@pytest.mark.parametrize(("model_id", "expected"), [
    ("claude-opus-5-5", (4.0, 20.0, 0.20, 5.0)),
    ("claude-opus-5", (5.0, 25.0, 0.50, 6.25)),
    ("claude-sonnet-5-5", (2.0, 10.0, 0.20, 2.5)),
    ("claude-fable-5-1", (10.0, 50.0, 0.25, 12.5)),
    ("claude-haiku-4-5-20251001", (1.0, 5.0, 0.10, 1.25)),
])
def test_prices_per_million(model_id: str, expected: tuple[float, float, float, float]):
    p = registry.get(model_id).pricing
    assert (p.input, p.output, p.cache_read, p.cache_write) == expected
    # one million of each kind costs the four rates added up
    usage = Usage(input=1_000_000, output=1_000_000, cache_read=1_000_000, cache_write=1_000_000)
    assert pricing.cost_of(usage, model_id, build_ai_settings()) == pytest.approx(sum(expected))


def test_gemini_usage_prices_like_slide_studio():
    usage = Usage(input=37021, output=101)
    assert round(pricing.cost_of(usage, "gemini-3.8-flash", build_ai_settings()), 4) == 0.0281


def test_the_cost_table_can_be_overridden_per_model_and_per_rate():
    s = build_ai_settings(llm_pricing_overrides='{"claude-opus-5-5": {"input": 1, "output": 2}}')
    p = pricing.pricing_for("claude-opus-5-5", s)
    assert (p.input, p.output, p.cache_read, p.cache_write) == (1.0, 2.0, 0.20, 5.0)
    assert pricing.cost_of(Usage(input=1_000_000), "claude-opus-5-5", s) == pytest.approx(1.0)
    assert pricing.pricing_for("claude-sonnet-5-5", s) == registry.get("claude-sonnet-5-5").pricing


@pytest.mark.parametrize("raw", ["[1]", '{"m": 3}', '{"m": {"input": -1}}', '{"m": {"bogus": 1}}', "{not json"])
def test_a_bad_override_is_a_config_problem(raw: str):
    problems = check_config(build_settings(), build_engine_settings(), build_ai_settings(llm_pricing_overrides=raw))
    assert any("LLM_PRICING_OVERRIDES" in p for p in problems)


def test_unknown_models_and_gemini_without_the_flag_are_config_problems():
    s = build_ai_settings(llm_design_model="claude-nope", llm_critic_model="gemini-3.8-flash")
    problems = check_config(build_settings(), build_engine_settings(), s)
    assert any("LLM_DESIGN_MODEL" in p and "registry" in p for p in problems)
    assert any("LLM_CRITIC_MODEL" in p and "LLM_GEMINI_ENABLED" in p for p in problems)
    assert any("LLM_CRITIC_MODEL must be a Claude model" in p for p in problems)


def test_bounds_on_limits_and_fanout():
    s = build_ai_settings(design_generate_fanout=0, llm_max_continuations=11, llm_design_effort="huge")
    problems = check_config(build_settings(), build_engine_settings(), s)
    for variable in ("DESIGN_GENERATE_FANOUT", "LLM_MAX_CONTINUATIONS", "LLM_DESIGN_EFFORT"):
        assert any(variable in p for p in problems), variable


def test_the_default_ai_settings_are_clean():
    assert check_config(build_settings(), build_engine_settings(), build_ai_settings()) == []


def test_production_requires_the_provider_keys():
    prod = build_settings(environment="production")
    problems = check_config(prod, build_engine_settings(), build_ai_settings(llm_gemini_enabled=True, design_stub=True))
    assert any("ANTHROPIC_API_KEY" in p for p in problems)
    assert any("GEMINI_API_KEY" in p for p in problems)
    assert any("DESIGN_STUB" in p for p in problems)
    keyed = build_ai_settings(anthropic_api_key="placeholder-not-a-key")
    assert not any("ANTHROPIC_API_KEY" in p for p in check_config(prod, build_engine_settings(), keyed))


def test_messages_never_carry_a_key_value():
    s = build_ai_settings(anthropic_api_key="sk-ant-should-not-appear", llm_design_model="nope")
    problems = check_config(build_settings(environment="production"), build_engine_settings(), s)
    assert all("sk-ant-should-not-appear" not in p for p in problems)
