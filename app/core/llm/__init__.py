"""The model layer: which models exist, what they cost, and one provider-agnostic agentic turn.

    from app.core.llm import factory, loop

    provider = factory.provider_for(model)              # Anthropic (primary) or Gemini (flagged)
    result = yield from loop.run_turn(provider, model=model, system=..., messages=..., tools=...,
                                      tool_handler=...)
    result.cost_usd                                     # every turn result carries its cost

Modules, leaves first:

* `types`              Usage (tokens -> cost), ToolCall, RoundRequest/RoundResult, TurnResult, errors.
* `registry`           the models, their prices and capabilities (Opus 5.x, Sonnet 5.x, Fable 5.1,
                       Haiku 4.5, Gemini Flash).
* `pricing`            usage -> USD, with `LLM_PRICING_OVERRIDES` applied.
* `ports`              the provider port (`stream_round`, `complete`) and `ProviderResolver`.
* `history`            the canonical history and its Anthropic and Gemini renderings.
* `anthropic_provider` / `gemini_provider`   the two providers.
* `loop`               the turn: rounds, tools, `max_tokens` continuation, per-turn cost.
* `factory`            model id -> provider, from settings.

Tests drive everything above the providers with `tests/fakes/llm.py` (a scripted provider); no test
calls a real API. Ported from Slide Studio `server/llm/*` (migration plan §4.1, K/R).
"""
