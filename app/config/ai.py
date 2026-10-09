"""SlideForge service: config/ai. Settings for the AI features: providers, models, cost and fan-out.

Read from environment variables (or `.env`) by pydantic, exactly as `app.config.engine` does, and
checked at startup by `check_ai_config()` / `check_ai_production()` (called from
`app.config.settings.check_config`). Every field is listed in `.env.example`.

What lives here and why each is a setting rather than a constant:

* **Provider keys.** `ANTHROPIC_API_KEY` (the primary provider) is required in production;
  `GEMINI_API_KEY` only when `LLM_GEMINI_ENABLED` is on.
* **Model ids.** The design model, the critic model and the brand role model are ids from the
  registry in `app.core.llm.registry`; an id the registry does not know is a config problem, never a
  silent fallback, because an unpriced model would spend money the cost ledger could not account for.
* **The cost table override.** `LLM_PRICING_OVERRIDES` replaces a model's USD-per-million prices
  without a release (a price change, a negotiated rate). JSON: `{"<model id>": {"input": 4,
  "output": 20, "cache_read": 0.2, "cache_write": 5}}`.
* **Limits.** Rounds per turn, `max_tokens` continuations per turn, and how many slides parallel
  Generate designs at once (`DESIGN_GENERATE_FANOUT`, bounded: every slide is a paid turn and a
  browser preview).

Ported from Slide Studio `server/settings.py` (the models, keys and concurrency parts), migration plan
§4.1. The `SLIDE_STUDIO_*` names are dropped for names that say what they are.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config.engine import PROJECT_ROOT

#: Effort levels the Claude API accepts in `output_config.effort`.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
#: The price fields of one `LLM_PRICING_OVERRIDES` entry (USD per million tokens).
PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")


class AISettings(BaseSettings):
    """The AI features' environment-driven settings. Field name = variable name, upper-cased."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    # --- providers ------------------------------------------------------------------------------
    anthropic_api_key: SecretStr = SecretStr("")
    gemini_api_key: SecretStr = SecretStr("")
    # Gemini is behind a flag: off, no Gemini model can be chosen and no Gemini call is made.
    llm_gemini_enabled: bool = False
    # Seconds one provider request may take. A design round at high effort thinks for minutes.
    llm_request_timeout_s: float = 900.0
    # The Claude API's server-side refusal fallback (`fallbacks: "default"`), on by default.
    llm_fallbacks_enabled: bool = True

    # --- models ---------------------------------------------------------------------------------
    llm_design_model: str = "claude-opus-5-5"
    llm_critic_model: str = "claude-sonnet-5-5"
    # The model that labels brand shapes during brand extraction. Empty: no annotation (the
    # deterministic extraction stands alone, as it does in Slide Studio without a Gemini key).
    llm_brand_role_model: str = ""
    llm_design_effort: str = "high"
    llm_critic_effort: str = "low"

    # --- limits and cost ------------------------------------------------------------------------
    llm_max_tokens: int = 64000
    llm_max_rounds: int = 40
    # How many times one turn may continue after the model hit max_tokens (0 = never: the turn ends
    # with an error, as before). Each continuation is a paid round.
    llm_max_continuations: int = 3
    # JSON object: model id -> {input, output, cache_read, cache_write} in USD per million tokens.
    llm_pricing_overrides: str = ""

    # --- the design loop ------------------------------------------------------------------------
    design_critic_enabled: bool = True
    design_brief_enabled: bool = True
    # Slides parallel Generate designs at once (1-16). Each is a paid design turn with previews.
    design_generate_fanout: int = 4
    # Browsers previews and the design review measure in (1-8; one Chromium each).
    design_preview_browsers: int = 2
    # Reference data. Archetypes: empty means the shared app/data/archetypes.json; a path names another
    # library. Exemplar pictures (decision D3: none ship in the repo): empty means none installed.
    design_archetypes_path: str = ""
    design_exemplars_dir: str = ""
    # The $0 path: the pipeline saves a hand-authored slide instead of calling a model. For wiring
    # checks on staging without a paid call; refused in production.
    design_stub: bool = False

    @field_validator("llm_design_effort", "llm_critic_effort", "llm_design_model", "llm_critic_model",
                     "llm_brand_role_model")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return value.strip().lower()

    @property
    def anthropic_key(self) -> str:
        return self.anthropic_api_key.get_secret_value()

    @property
    def gemini_key(self) -> str:
        return self.gemini_api_key.get_secret_value()

    def provider_key(self, provider: str) -> str:
        """The configured key for a provider ("anthropic", "gemini"), or ""."""
        if provider == "anthropic":
            return self.anthropic_key
        if provider == "gemini":
            return self.gemini_key
        return ""

    def pricing_overrides(self) -> dict[str, dict[str, float]]:
        """`LLM_PRICING_OVERRIDES` parsed. Raises ValueError when it is not the documented shape."""
        raw = self.llm_pricing_overrides.strip()
        if not raw:
            return {}
        data: Any = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("must be a JSON object")
        out: dict[str, dict[str, float]] = {}
        for model, prices in data.items():
            if not isinstance(prices, dict) or set(prices) - set(PRICE_FIELDS):
                raise ValueError(f"entry {model!r} must hold only {list(PRICE_FIELDS)}")
            values: dict[str, float] = {}
            for name, value in prices.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                    raise ValueError(f"entry {model!r}: {name} must be a non-negative number")
                values[name] = float(value)
            out[str(model)] = values
        return out


def _model_problems(s: AISettings) -> list[str]:
    from app.core.llm import registry  # late: the registry is core, this module is config

    problems: list[str] = []
    for variable, model_id, required in (
        ("LLM_DESIGN_MODEL", s.llm_design_model, True),
        ("LLM_CRITIC_MODEL", s.llm_critic_model, True),
        ("LLM_BRAND_ROLE_MODEL", s.llm_brand_role_model, False),
    ):
        if not model_id and not required:
            continue
        try:
            model = registry.get(model_id)
        except KeyError:
            problems.append(f"{variable} is not a model the registry knows (app/core/llm/registry.py).")
            continue
        if model.provider == "gemini" and not s.llm_gemini_enabled:
            problems.append(f"{variable} names a Gemini model but LLM_GEMINI_ENABLED is off.")
    try:
        if registry.get(s.llm_critic_model).provider != "anthropic":
            problems.append("LLM_CRITIC_MODEL must be a Claude model (the critic call is Claude-only).")
    except KeyError:
        pass
    return problems


def check_ai_config(s: AISettings) -> list[str]:
    """Problems in the AI settings (empty means all good). Messages name the variable only."""
    problems = _model_problems(s)
    for variable, effort in (("LLM_DESIGN_EFFORT", s.llm_design_effort), ("LLM_CRITIC_EFFORT", s.llm_critic_effort)):
        if effort not in EFFORT_LEVELS:
            problems.append(f"{variable} must be one of {list(EFFORT_LEVELS)}.")
    if not 1024 <= s.llm_max_tokens <= 128000:
        problems.append("LLM_MAX_TOKENS must be between 1024 and 128000.")
    if not 1 <= s.llm_max_rounds <= 100:
        problems.append("LLM_MAX_ROUNDS must be between 1 and 100.")
    if not 0 <= s.llm_max_continuations <= 10:
        problems.append("LLM_MAX_CONTINUATIONS must be between 0 and 10.")
    if s.llm_request_timeout_s <= 0:
        problems.append("LLM_REQUEST_TIMEOUT_S must be positive.")
    if not 1 <= s.design_generate_fanout <= 16:
        problems.append("DESIGN_GENERATE_FANOUT must be between 1 and 16.")
    if not 1 <= s.design_preview_browsers <= 8:
        problems.append("DESIGN_PREVIEW_BROWSERS must be between 1 and 8.")
    try:
        s.pricing_overrides()
    except ValueError as exc:
        problems.append(f"LLM_PRICING_OVERRIDES is invalid ({str(exc).splitlines()[0][:120]}).")
    return problems


def check_ai_production(s: AISettings) -> list[str]:
    """What production additionally requires of the AI settings."""
    problems: list[str] = []
    if not s.anthropic_key:
        problems.append("ANTHROPIC_API_KEY is empty in production; no slide could be designed.")
    if s.llm_gemini_enabled and not s.gemini_key:
        problems.append("GEMINI_API_KEY is empty in production while LLM_GEMINI_ENABLED is on.")
    if s.design_stub:
        problems.append("DESIGN_STUB must be off in production (it saves placeholder slides).")
    return problems


ai_settings = AISettings()
