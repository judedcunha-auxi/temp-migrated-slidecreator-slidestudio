"""The Anthropic provider (app/core/llm/anthropic_provider.py) over a stub SDK client: no network.

What is pinned: the request parameters the `claude-api` reference prescribes (adaptive thinking,
explicit effort, top-level caching, the default refusal fallback, context editing), the stream ->
UI events mapping, and usage -> `Usage`.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.llm import loop
from app.core.llm.anthropic_provider import CONTEXT_BETA, FALLBACK_BETA, AnthropicProvider
from app.core.llm.types import ProviderUnavailable, RoundRequest
from tests.conftest import build_ai_settings


class Block(SimpleNamespace):
    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {k: v for k, v in vars(self).items() if v is not None}


class FakeStream:
    def __init__(self, events: list[Any], final: Any) -> None:
        self.events, self.final = events, final

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def __iter__(self) -> Iterator[Any]:
        return iter(self.events)

    def get_final_message(self) -> Any:
        return self.final


class FakeClient:
    """`client.beta.messages.stream/create` and `with_options`, recording what was sent."""

    def __init__(self, rounds: list[tuple[list[Any], Any]]) -> None:
        self.rounds = list(rounds)
        self.sent: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream, create=self._create))

    def with_options(self, **_: Any) -> FakeClient:
        return self

    def _stream(self, **params: Any) -> FakeStream:
        self.sent.append(params)
        events, final = self.rounds.pop(0)
        return FakeStream(events, final)

    def _create(self, **params: Any) -> Any:
        self.sent.append(params)
        return self.rounds.pop(0)[1]


def ev(kind: str, index: int = 0, **fields: Any) -> SimpleNamespace:
    return SimpleNamespace(type=kind, index=index, **fields)


def final(*blocks: Block, stop: str = "end_turn", model: str = "claude-opus-5-5") -> SimpleNamespace:
    usage = SimpleNamespace(input_tokens=1200, output_tokens=300, cache_read_input_tokens=5000,
                            cache_creation_input_tokens=800)
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage, model=model)


def request(**kw: Any) -> RoundRequest:
    values: dict[str, Any] = {"model": "claude-opus-5-5", "system": "sys",
                              "messages": [{"role": "user", "content": "hi"}], "effort": "high"}
    values.update(kw)
    return RoundRequest(**values)


def test_design_round_parameters_follow_the_reference():
    params = AnthropicProvider(build_ai_settings(), client=object()).params(
        request(tools=[{"name": "save_slide", "input_schema": {"type": "object"}}]), streaming=True)
    assert params["model"] == "claude-opus-5-5"
    assert params["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert params["output_config"] == {"effort": "high"}, "set explicitly: Opus 5.5 defaults to medium"
    assert params["cache_control"] == {"type": "ephemeral"}
    assert params["fallbacks"] == "default" and FALLBACK_BETA in params["betas"]
    assert CONTEXT_BETA in params["betas"] and params["context_management"]["edits"]
    assert params["tools"][0]["name"] == "save_slide"
    assert "budget_tokens" not in str(params), "budget_tokens is rejected on the 5.x models"


def test_the_fallback_can_be_switched_off_and_haiku_gets_no_thinking_or_effort():
    s = build_ai_settings(llm_fallbacks_enabled=False)
    params = AnthropicProvider(s, client=object()).params(request(), streaming=False)
    assert "fallbacks" not in params and "betas" not in params
    haiku = AnthropicProvider(build_ai_settings(), client=object()).params(
        request(model="claude-haiku-4-5-20251001"), streaming=False)
    assert "thinking" not in haiku and "output_config" not in haiku


def test_minimal_thinking_follows_each_model():
    provider = AnthropicProvider(build_ai_settings(), client=object())
    sonnet = provider.params(request(model="claude-sonnet-5-5", minimal_thinking=True), streaming=False)
    assert sonnet["thinking"] == {"type": "between_tools"}, "Sonnet 5.5 rejects disabled"
    opus = provider.params(request(minimal_thinking=True), streaming=False)
    assert "thinking" not in opus, "Opus 5.5 cannot disable thinking: low effort is the lever"


def test_a_streamed_round_maps_events_and_returns_canonical_blocks():
    events = [
        ev("content_block_start", 0, content_block=SimpleNamespace(type="thinking")),
        ev("content_block_delta", 0, delta=SimpleNamespace(type="thinking_delta", thinking="hmm")),
        ev("content_block_stop", 0),
        ev("content_block_start", 1, content_block=SimpleNamespace(type="text")),
        ev("content_block_delta", 1, delta=SimpleNamespace(type="text_delta", text="Hello")),
        ev("content_block_stop", 1),
        ev("content_block_start", 2, content_block=SimpleNamespace(type="tool_use", name="save_slide")),
        ev("content_block_stop", 2),
    ]
    done = final(Block(type="text", text="Hello"),
                 Block(type="tool_use", id="toolu_1", name="save_slide", input={"title": "t"}),
                 stop="tool_use", model="claude-opus-5")
    client = FakeClient([(events, done)])
    gen = AnthropicProvider(build_ai_settings(), client=client).stream_round(request())
    seen: list[dict[str, Any]] = []
    try:
        while True:
            seen.append(next(gen))
    except StopIteration as stop:
        result = stop.value
    assert [e["type"] for e in seen] == ["status", "thinking", "text_start", "text", "text_end", "status"]
    assert seen[-1]["text"] == "Designing slide…"
    assert result.stop_reason == "tool_use" and result.model == "claude-opus-5"
    assert result.tool_calls()[0].input == {"title": "t"}
    assert (result.usage.input, result.usage.output, result.usage.cache_read, result.usage.cache_write) == \
        (1200, 300, 5000, 800)


def test_a_full_turn_through_the_provider_with_a_continuation():
    """max_tokens continuation works with the real provider class over a stub client."""
    client = FakeClient([([], final(Block(type="text", text="part one"), stop="max_tokens")),
                         ([], final(Block(type="text", text="part two")))])
    provider = AnthropicProvider(build_ai_settings(), client=client)
    messages: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    gen = loop.run_turn(provider, model="claude-opus-5-5", system="s", messages=messages,
                        settings=build_ai_settings())
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        result = stop.value
    assert result.continuations == 1 and result.stop_reason == "end_turn"
    second = client.sent[1]["messages"]
    assert second[-2]["role"] == "assistant" and "model" not in second[-2], "private keys are stripped"
    assert second[-1]["content"][0]["text"] == loop.CONTINUE_TEXT


def test_no_key_means_no_client():
    with pytest.raises(ProviderUnavailable):
        _ = AnthropicProvider(build_ai_settings()).client


def test_a_json_schema_becomes_structured_output_beside_the_effort():
    schema = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"],
              "additionalProperties": False}
    params = AnthropicProvider(build_ai_settings(), client=object()).params(request(output_schema=schema),
                                                                            streaming=False)
    assert params["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": schema}}
    assert "tool_choice" not in params, "no forced tool call: the 5.5 models reject it"
