# Models and cost

Which AI models the service calls, how each call is priced, and the settings that control both.
The code is `app/core/llm/` and the settings are `app/config/ai.py`; every variable below is in
[`.env.example`](../.env.example) and is checked at startup by `check_config()`.

## Providers

| Provider | Role | Switch |
|---|---|---|
| Anthropic (Claude) | The primary provider: the design chat, the critic. | Always on. `ANTHROPIC_API_KEY` is required in production. |
| Google (Gemini) | An alternative design model, and the optional brand role annotation. | Off unless `LLM_GEMINI_ENABLED=true`; then `GEMINI_API_KEY` is required in production. |

Every call goes through one port (`app/core/llm/ports.py`), so the design loop, the critic and
brand extraction work the same on either provider. The tests run on a scripted fake provider
(`tests/fakes/llm.py`), and a test fixture refuses to build a real SDK client, so **no test makes a
paid call**.

## Models

Model ids come from the registry in `app/core/llm/registry.py`. An id the registry does not know is
a startup config problem, never a silent fallback: an unpriced model would spend money the cost
ledger could not account for.

| Setting | Default | What it is |
|---|---|---|
| `LLM_DESIGN_MODEL` | `claude-opus-5-5` | Writes and edits the slides (the design chat, Generate, image -> HTML). |
| `LLM_CRITIC_MODEL` | `claude-sonnet-5-5` | The independent critic behind `preview_slide`. Must be a Claude model. |
| `LLM_BRAND_ROLE_MODEL` | empty (off) | Labels brand shapes during brand extraction. |
| `LLM_DESIGN_EFFORT` / `LLM_CRITIC_EFFORT` | `high` / `low` | `output_config.effort`. Set explicitly because Opus 5.5 defaults to `medium`. |

Registered: Claude Opus 5.5 and 5, Sonnet 5.5 and 5, Fable 5.1, Haiku 4.5
(`claude-haiku-4-5-20251001`, alias `claude-haiku-4-5`), and Gemini 3.8 Flash.

Every Claude round uses adaptive thinking with a summarised display, top-level prompt caching, and
context editing for long turns. On the models that support it, the Claude API's server-side refusal
fallback is on (`fallbacks: "default"`, `LLM_FALLBACKS_ENABLED`): a declined request is retried on
another model inside the same call, and that round is priced with the model that answered.

## What a call costs

Prices are USD per million tokens, the first-party Claude API rates (cache write is the 5-minute
TTL rate, 1.25x input):

| Model | Input | Output | Cache read | Cache write |
|---|---|---|---|---|
| `claude-opus-5-5` | 4.00 | 20.00 | 0.20 | 5.00 |
| `claude-opus-5` | 5.00 | 25.00 | 0.50 | 6.25 |
| `claude-sonnet-5-5` | 2.00 | 10.00 | 0.20 | 2.50 |
| `claude-sonnet-5` | 2.00 | 10.00 | 0.20 | 2.50 |
| `claude-fable-5-1` | 10.00 | 50.00 | 0.25 | 12.50 |
| `claude-haiku-4-5-20251001` | 1.00 | 5.00 | 0.10 | 1.25 |
| `gemini-3.8-flash` | 0.75 | 3.75 | 0.075 | 0 |

**Per-turn accounting.** Each round's tokens (input, output, cache reads, cache writes) are priced
with the model that served that round, and added. A turn's result (`TurnResult.cost_usd`) is that
sum; the design turn adds the critic's calls to it, and the pipeline returns the total with its
result. Totals are always sums of per-round prices, never re-priced from a token total, because one
turn can span models.

**Changing a price** needs no release: `LLM_PRICING_OVERRIDES` is a JSON object of model id to the
rates to replace, and an entry may name only some of the four:

```
LLM_PRICING_OVERRIDES={"claude-opus-5-5": {"input": 4, "output": 20}}
```

## Limits

| Setting | Default | Bounds | What it limits |
|---|---|---|---|
| `LLM_MAX_TOKENS` | 64000 | 1024-128000 | Output per round. |
| `LLM_MAX_ROUNDS` | 40 | 1-100 | Rounds in one turn. |
| `LLM_MAX_CONTINUATIONS` | 3 | 0-10 | Times a turn may continue after hitting `max_tokens`. |
| `LLM_REQUEST_TIMEOUT_S` | 900 | > 0 | One provider request. |
| `DESIGN_GENERATE_FANOUT` | 4 | 1-16 | Slides parallel Generate designs at once. |
| `DESIGN_PREVIEW_BROWSERS` | 2 | 1-8 | Browsers for previews and the design review. |

**`max_tokens` continuation.** When a round stops because it hit `max_tokens`, the turn goes on
instead of failing: the cut-off reply is kept as it is and the model is asked to continue where it
stopped. A tool call that was cut off is never run (its input may parse but be incomplete); the
model is told it was cut off and asked to call it again, smaller. Each continuation is a paid round,
so they are capped by `LLM_MAX_CONTINUATIONS`; past the cap the turn ends with an error, as it did
before. `LLM_MAX_CONTINUATIONS=0` restores the old behaviour.

## The $0 path

`DESIGN_STUB=true` makes the pipeline save a hand-authored slide instead of calling a model, so the
whole design -> export path can be exercised on staging without a paid call. It is refused in
production.
