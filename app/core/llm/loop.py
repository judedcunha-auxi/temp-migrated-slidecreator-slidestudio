"""One agentic turn over any provider: rounds, tool calls, `max_tokens` continuation, cost.

`run_turn(...)` is a generator. It yields the provider's UI events plus its own (`usage`,
`continuation`, `error`, `stop`) and **returns** a `TurnResult` (read it with `yield from`). It
mutates `messages` in place, appending canonical turns: each assistant turn stamped with the model
that wrote it, then the user turn carrying tool results.

What happens after each round, by `stop_reason`:

* `tool_use`: every tool call is answered by `tool_handler` (in order; their results go back in ONE
  user message, as the API wants), then the next round runs.
* `pause_turn`: a server tool paused the turn; the assistant turn is appended and the next round
  resumes it.
* **`max_tokens`: the turn continues instead of failing** (the Slide Studio ROADMAP gap). The cut-off
  assistant turn is kept as it is (models from 4.6 on accept no prefill, and preserved thinking needs
  an append-only history), and a user turn asks the model to carry on:
  - a cut-off **tool call** is never run (a truncated input can parse as a valid partial object):
    each gets an error tool_result saying it was cut off and nothing ran, so the model re-issues it,
    smaller;
  - cut-off **text** gets a plain "continue exactly where you stopped".
  At most `max_continuations` times per turn; past that the turn ends with an error and
  `stop_reason == "max_tokens"`, as it always did.
* `refusal`: the turn ends with an error event (the provider's server-side fallback, when on, has
  already tried another model inside the same call).
* anything else (`end_turn`, `stop_sequence`): the turn ends.

Cost: each round's usage is priced with the model that **served** that round (`RoundResult.model`)
and added to the turn's total; `TurnResult.cost_usd` is that total. Ported from the loops in Slide
Studio `server/llm/anthropic_client.run` and `gemini_client.run` (migration plan §4.1, K/R).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Generator

from app.config.ai import AISettings, ai_settings
from app.core.llm import pricing
from app.core.llm.ports import LLMProvider
from app.core.llm.types import JSON, Cancelled, Event, RoundRequest, ToolCall, TurnResult

_log = logging.getLogger(__name__)

#: What a tool handler returns: the tool_result block for the model, and an event for the caller.
ToolHandler = Callable[[ToolCall], tuple[JSON, Event | None]]

CONTINUE_TEXT = ("Your previous reply was cut off by the output limit. Continue exactly where you "
                 "stopped, without repeating what you already wrote.")
CUT_OFF_TOOL = ("Error: this {name} call was cut off by the output limit before its input was complete, "
                "so nothing ran. Call it again with a smaller input (for a slide, prefer edit_slide over "
                "resending the whole document).")
LENGTH_ERROR = "The response hit the length limit before finishing."
REFUSAL_ERROR = "The model declined this request. Try rephrasing it."
ROUNDS_ERROR = "Stopped after too many steps."


def _assistant_message(content: list[JSON], model: str, extra: JSON) -> JSON:
    message: JSON = {"role": "assistant", "content": content, "model": model}
    message.update(extra)
    return message


def run_turn(
    provider: LLMProvider,
    *,
    model: str,
    system: str,
    messages: list[JSON],
    tools: list[JSON] | None = None,
    tool_handler: ToolHandler | None = None,
    effort: str | None = None,
    attachments_root: object = None,
    on_round: Callable[[list[JSON]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    settings: AISettings | None = None,
) -> Generator[Event, None, TurnResult]:
    """Stream a full agentic turn. See the module docstring."""
    s = settings if settings is not None else ai_settings
    result = TurnResult(stop_reason="end_turn", model=model)
    for _ in range(s.llm_max_rounds):
        if should_stop is not None and should_stop():
            raise Cancelled()  # never start a round (and its bill) for a turn being stopped
        request = RoundRequest(model=model, system=system, messages=messages, tools=list(tools or []),
                               max_tokens=s.llm_max_tokens, effort=effort or s.llm_design_effort,
                               attachments_root=attachments_root)
        round_result = yield from provider.stream_round(request)
        result.rounds += 1
        served = round_result.model or model
        usage = pricing.priced(round_result.usage, served, s)
        result.usage.add(usage)
        yield {"type": "usage", "round": usage.to_json(), "cost": usage.cost_usd, "model": served}

        messages.append(_assistant_message(round_result.content, served, round_result.extra))
        stop = round_result.stop_reason
        calls = round_result.tool_calls()

        if stop == "max_tokens":
            if result.continuations >= s.llm_max_continuations:
                if on_round:
                    on_round(messages)
                yield {"type": "error", "text": LENGTH_ERROR}
                result.stop_reason, result.error = "max_tokens", LENGTH_ERROR
                yield {"type": "stop", "reason": "max_tokens"}
                return result
            result.continuations += 1
            if calls:
                answer: list[JSON] = [{"type": "tool_result", "tool_use_id": c.id, "is_error": True,
                                       "content": CUT_OFF_TOOL.format(name=c.name or "tool")} for c in calls]
            else:
                answer = [{"type": "text", "text": CONTINUE_TEXT}]
            messages.append({"role": "user", "content": answer})
            if on_round:
                on_round(messages)
            _log.info("llm: %s hit max_tokens; continuing (%d of %d)", served, result.continuations,
                      s.llm_max_continuations)
            yield {"type": "continuation", "n": result.continuations, "reason": "max_tokens"}
            continue

        if calls and tool_handler is not None and stop in ("tool_use", "end_turn"):
            results: list[JSON] = []
            events: list[Event] = []
            for call in calls:
                block, event = tool_handler(call)
                results.append(block)
                if event:
                    events.append(event)
            messages.append({"role": "user", "content": results})
            if on_round:
                on_round(messages)
            yield from events
            continue
        if on_round:
            on_round(messages)
        if stop == "pause_turn":
            continue
        if stop == "refusal":
            yield {"type": "error", "text": REFUSAL_ERROR}
            result.error = REFUSAL_ERROR
        result.stop_reason = stop
        yield {"type": "stop", "reason": stop}
        return result
    yield {"type": "error", "text": ROUNDS_ERROR}
    result.stop_reason, result.error = "max_rounds", ROUNDS_ERROR
    yield {"type": "stop", "reason": "max_rounds"}
    return result


def complete(provider: LLMProvider, request: RoundRequest, settings: AISettings | None = None) -> tuple[str, JSON]:
    """One small non-streaming round: (its text, its priced usage as JSON). For the critic and the
    brand role annotator, whose cost the caller adds to its own total."""
    round_result = provider.complete(request)
    usage = pricing.priced(round_result.usage, round_result.model or request.model, settings)
    return round_result.text().strip(), {**usage.to_json(), "model": round_result.model or request.model,
                                         "stopReason": round_result.stop_reason}
