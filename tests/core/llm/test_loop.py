"""The provider-agnostic turn (app/core/llm/loop.py), driven by the scripted fake provider.

The behaviours that matter most: `max_tokens` continues the turn instead of failing (text and a
cut-off tool call), continuations are bounded, every round is priced with the model that served it,
and the turn's result carries the total cost.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any, TypeVar

import pytest

from app.core.llm import loop, pricing
from app.core.llm.types import Cancelled, ToolCall, TurnResult, Usage
from tests.conftest import build_ai_settings
from tests.fakes.llm import Round, ScriptedProvider, ScriptExhausted, text, tool

T = TypeVar("T")


def drain(gen: Generator[dict[str, Any], None, T]) -> tuple[list[dict[str, Any]], T]:
    events: list[dict[str, Any]] = []
    while True:
        try:
            events.append(next(gen))
        except StopIteration as stop:
            return events, stop.value


def of(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [e for e in events if e["type"] == kind]


def turn(provider: ScriptedProvider, messages: list[dict[str, Any]], handler: Any = None,
         **settings: Any) -> tuple[list[dict[str, Any]], TurnResult]:
    s = build_ai_settings(**settings)
    return drain(loop.run_turn(provider, model="claude-opus-5-5", system="sys", messages=messages,
                               tools=[{"name": "save_slide", "input_schema": {"type": "object"}}],
                               tool_handler=handler, settings=s))


def saved(call: ToolCall) -> tuple[dict[str, Any], dict[str, Any] | None]:
    return ({"type": "tool_result", "tool_use_id": call.id, "content": "Saved sld_0000000001 version 1."},
            {"type": "slide", "slideId": "sld_0000000001", "version": 1})


def test_a_tool_round_then_a_reply():
    provider = ScriptedProvider([Round(tool("save_slide", {"title": "x"})), Round(text("Done."))])
    messages: list[dict[str, Any]] = [{"role": "user", "content": "Make a slide"}]
    events, result = turn(provider, messages, saved)

    assert result.stop_reason == "end_turn" and result.rounds == 2 and result.continuations == 0
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[1]["model"] == "claude-opus-5-5", "assistant turns are stamped with their model"
    assert messages[2]["content"][0]["type"] == "tool_result"
    assert of(events, "slide"), "the handler's event reaches the caller"
    assert events[-1] == {"type": "stop", "reason": "end_turn"}


def test_every_turn_result_carries_its_cost():
    usage = Usage(input=10_000, output=2_000, cache_read=50_000, cache_write=4_000)
    provider = ScriptedProvider([Round(tool("save_slide", {}), usage=usage), Round(text("ok"), usage=usage)])
    events, result = turn(provider, [{"role": "user", "content": "go"}], saved)

    one = pricing.cost_of(Usage(10_000, 2_000, 50_000, 4_000), "claude-opus-5-5")
    # Opus 5.5: $4 in, $20 out, $0.20 cache read, $5 cache write per million
    assert one == pytest.approx((10_000 * 4 + 2_000 * 20 + 50_000 * 0.2 + 4_000 * 5) / 1e6)
    assert result.cost_usd == pytest.approx(2 * one)
    assert [e["cost"] for e in of(events, "usage")] == [one, one]
    assert result.usage.input == 20_000 and result.usage.cache_read == 100_000


def test_a_round_is_priced_with_the_model_that_served_it():
    """A server-side refusal fallback answers on another model; the round is priced with that one."""
    usage = Usage(input=1_000_000, output=0)
    provider = ScriptedProvider([Round(text("ok"), usage=usage, model="claude-opus-5")])
    events, result = turn(provider, [{"role": "user", "content": "go"}])
    assert of(events, "usage")[0]["model"] == "claude-opus-5"
    assert result.cost_usd == pytest.approx(5.0), "Opus 5's $5 input, not Opus 5.5's $4"


def test_max_tokens_on_text_continues_the_turn():
    provider = ScriptedProvider([
        Round(text("The first half"), stop="max_tokens"),
        Round(text(" and the second half.")),
    ])
    messages: list[dict[str, Any]] = [{"role": "user", "content": "Write it"}]
    events, result = turn(provider, messages)

    assert result.stop_reason == "end_turn" and result.error is None
    assert result.continuations == 1 and result.rounds == 2
    assert of(events, "continuation") == [{"type": "continuation", "n": 1, "reason": "max_tokens"}]
    assert of(events, "error") == []
    # the cut-off turn is kept as it is (append-only), then a user turn asks to continue
    assert messages[1]["content"] == [{"type": "text", "text": "The first half"}]
    assert messages[2] == {"role": "user", "content": [{"type": "text", "text": loop.CONTINUE_TEXT}]}
    sent = provider.requests[1].messages
    assert sent[-1]["content"][0]["text"] == loop.CONTINUE_TEXT


def test_max_tokens_inside_a_tool_call_never_runs_it_and_asks_again():
    provider = ScriptedProvider([
        Round(tool("save_slide", {"title": "half", "html": "<div"}), stop="max_tokens"),
        Round(tool("save_slide", {"title": "whole", "html": "<p>x</p>"})),
        Round(text("Saved.")),
    ])
    ran: list[str] = []

    def handler(call: ToolCall) -> tuple[dict[str, Any], dict[str, Any] | None]:
        ran.append(call.input["title"])
        return saved(call)

    messages: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    events, result = turn(provider, messages, handler)

    assert ran == ["whole"], "the truncated input is never executed"
    cut = messages[2]["content"][0]
    assert cut["type"] == "tool_result" and cut["is_error"] is True
    assert cut["tool_use_id"] == messages[1]["content"][0]["id"]
    assert "cut off" in cut["content"]
    assert result.continuations == 1 and result.stop_reason == "end_turn"


def test_continuations_are_bounded_and_then_the_turn_fails_as_before():
    provider = ScriptedProvider([Round(text("a"), stop="max_tokens") for _ in range(3)])
    events, result = turn(provider, [{"role": "user", "content": "go"}], llm_max_continuations=2)

    assert result.continuations == 2 and result.rounds == 3
    assert result.stop_reason == "max_tokens" and result.error == loop.LENGTH_ERROR
    assert of(events, "error") == [{"type": "error", "text": loop.LENGTH_ERROR}]
    assert provider.remaining == 0


def test_continuation_off_keeps_the_old_failure():
    provider = ScriptedProvider([Round(text("a"), stop="max_tokens")])
    _events, result = turn(provider, [{"role": "user", "content": "go"}], llm_max_continuations=0)
    assert result.stop_reason == "max_tokens" and result.continuations == 0


def test_a_refusal_is_said_in_words():
    provider = ScriptedProvider([Round(text(""), stop="refusal")])
    events, result = turn(provider, [{"role": "user", "content": "go"}])
    assert result.stop_reason == "refusal" and result.error == loop.REFUSAL_ERROR
    assert of(events, "error")[0]["text"] == loop.REFUSAL_ERROR


def test_pause_turn_resumes_without_a_user_message():
    provider = ScriptedProvider([Round(text("thinking"), stop="pause_turn"), Round(text("done"))])
    messages: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    _events, result = turn(provider, messages)
    assert [m["role"] for m in messages] == ["user", "assistant", "assistant"]
    assert result.rounds == 2


def test_a_turn_that_never_ends_stops_after_max_rounds():
    provider = ScriptedProvider([Round(tool("save_slide", {})) for _ in range(3)])
    events, result = turn(provider, [{"role": "user", "content": "go"}], saved, llm_max_rounds=3)
    assert result.stop_reason == "max_rounds" and of(events, "error")


def test_a_stopped_turn_starts_no_round():
    provider = ScriptedProvider([Round(text("never"))])
    gen = loop.run_turn(provider, model="claude-opus-5-5", system="s", messages=[{"role": "user", "content": "x"}],
                        should_stop=lambda: True, settings=build_ai_settings())
    with pytest.raises(Cancelled):
        next(gen)
    assert provider.requests == []


def test_parallel_tool_calls_are_answered_in_one_user_message():
    provider = ScriptedProvider([Round(tool("save_slide", {"n": 1}), tool("save_slide", {"n": 2})), Round(text("ok"))])
    messages: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    turn(provider, messages, saved)
    assert len(messages[2]["content"]) == 2


def test_the_fake_fails_loudly_on_an_unplanned_round():
    provider = ScriptedProvider([Round(tool("save_slide", {}))])
    with pytest.raises(ScriptExhausted):
        turn(provider, [{"role": "user", "content": "go"}], saved)


def test_complete_prices_one_small_round():
    provider = ScriptedProvider(completions=[Round(text(" PASS "), usage=Usage(input=2_000, output=100))])
    from app.core.llm.types import RoundRequest

    said, usage = loop.complete(provider, RoundRequest(model="claude-sonnet-5-5", system="s", messages=[]),
                                build_ai_settings())
    assert said == "PASS"
    assert usage["costUsd"] == pytest.approx((2_000 * 2 + 100 * 10) / 1e6)
    assert usage["model"] == "claude-sonnet-5-5"
