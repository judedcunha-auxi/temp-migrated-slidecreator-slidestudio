"""The Gemini provider (app/core/llm/gemini_provider.py), replayed from recorded API events.

Nothing here reaches Google: a stub client's `interactions.create` returns the events of real turns
recorded by Slide Studio's spike (tests/fixtures/llm/gemini/*.json, synthetic prompts only). Ported
from Slide Studio `server/tests/test_gemini.py` (the provider parts; the UI, key-check and Files API
parts are dropped with those features).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.core.llm import factory, loop, pricing
from app.core.llm.gemini_provider import GeminiProvider, usage_of, wire_tools
from app.core.llm.types import ProviderUnavailable, ToolCall
from tests.conftest import build_ai_settings

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "llm" / "gemini"
TOOLS = [{"name": "save_slide", "description": "Create a slide.", "eager_input_streaming": True,
          "input_schema": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}}]


def events(name: str) -> list[dict[str, Any]]:
    recorded = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return [e for e in recorded if e.get("event_type") != "__spike_outcome__"]


class Obj:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return copy.deepcopy(self._data)


class FakeStream:
    def __init__(self, recorded: list[dict[str, Any]]) -> None:
        self.recorded, self.closed = recorded, False

    def __iter__(self) -> Iterator[Obj]:
        for data in self.recorded:
            if data.get("event_type") == "error":
                raise RuntimeError("400 invalid_request")
            yield Obj(data)

    def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self, *rounds: list[dict[str, Any]]) -> None:
        self.rounds = list(rounds)
        self.calls: list[dict[str, Any]] = []
        self.streams: list[FakeStream] = []
        self.interactions = self

    def create(self, **params: Any) -> FakeStream:
        self.calls.append(copy.deepcopy(params))
        stream = FakeStream(self.rounds.pop(0))
        self.streams.append(stream)
        return stream


def run(client: FakeClient, messages: list[dict[str, Any]], handler: Any) -> tuple[list[dict[str, Any]], Any]:
    s = build_ai_settings(llm_gemini_enabled=True)
    provider = GeminiProvider(s, client=client)
    gen = loop.run_turn(provider, model="gemini-3.8-flash", system="design", messages=messages, tools=TOOLS,
                        tool_handler=handler, settings=s)
    seen: list[dict[str, Any]] = []
    try:
        while True:
            seen.append(next(gen))
    except StopIteration as stop:
        return seen, stop.value


def saved(call: ToolCall) -> tuple[dict[str, Any], dict[str, Any] | None]:
    return ({"type": "tool_result", "tool_use_id": call.id, "content": "Saved sld_1 version 1."},
            {"type": "slide", "slideId": "sld_1", "version": 1})


def test_a_recorded_turn_streams_thinking_a_tool_call_and_a_reply():
    client = FakeClient(events("turn1_tool_call"), events("turn2_after_result"))
    calls: list[ToolCall] = []

    def handler(call: ToolCall) -> Any:
        calls.append(call)
        return saved(call)

    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text": "A title slide."}]}]
    seen, result = run(client, messages, handler)

    assert seen[0] == {"type": "status", "text": "Thinking…"}
    assert {"type": "status", "text": "Designing slide…"} in seen
    assert [c.name for c in calls] == ["save_slide"] and calls[0].id == "call_747730"
    assert calls[0].input["title"] == "Market outlook 2027", "the streamed arguments JSON is parsed"
    assert result.stop_reason == "end_turn" and result.rounds == 2
    wrote = messages[1]
    assert wrote["model"] == "gemini-3.8-flash"
    assert [s["type"] for s in wrote["gemini_steps"]] == ["thought", "function_call"]
    assert wrote["gemini_steps"][0]["signature"], "the thought signature is kept for replay"
    assert result.cost_usd > 0


def test_the_second_request_resends_the_steps_verbatim_and_pairs_the_result():
    client = FakeClient(events("turn1_tool_call"), events("turn2_after_result"))
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text": "A title slide."}]}]
    run(client, messages, saved)
    resent = client.calls[1]["input"]
    assert json.dumps(resent[1:3], sort_keys=True) == json.dumps(messages[1]["gemini_steps"], sort_keys=True)
    assert resent[3] == {"type": "function_result", "call_id": "call_747730", "name": "save_slide",
                         "result": [{"type": "text", "text": "Saved sld_1 version 1."}]}
    assert client.calls[0]["store"] is False and client.calls[0]["stream"] is True
    assert client.calls[0]["tools"] == wire_tools(TOOLS)
    assert "eager_input_streaming" not in json.dumps(client.calls[0]["tools"])


def test_parallel_calls_run_in_order_and_are_answered_together():
    client = FakeClient(events("turn3_parallel"), events("turn2_after_result"))
    seen_ids: list[str] = []

    def handler(call: ToolCall) -> Any:
        seen_ids.append(call.id)
        return ({"type": "tool_result", "tool_use_id": call.id, "content": f"Saved {call.id}."}, None)

    messages: list[dict[str, Any]] = [{"role": "user", "content": "Two slides."}]
    run(client, messages, handler)
    assert seen_ids == ["call_1294958", "call_1294974"]
    assert len(messages[2]["content"]) == 2


def test_usage_maps_cached_and_thought_tokens():
    completed = json.loads((FIXTURES / "usage_turn2.json").read_text(encoding="utf-8"))["interaction"]
    usage = usage_of(completed)
    api = completed["usage"]
    assert usage.input + usage.cache_read == api["total_input_tokens"]
    assert usage.output == api["total_output_tokens"] + api["total_thought_tokens"]
    assert usage.cache_write == 0
    assert round(pricing.cost_of(usage, "gemini-3.8-flash", build_ai_settings()), 4) == 0.0281
    assert usage_of(None).tokens() == 0


def test_an_incomplete_round_continues_and_a_failed_one_is_a_refusal():
    cut = copy.deepcopy(events("turn2_after_result"))
    cut[-1]["interaction"]["status"] = "incomplete"
    client = FakeClient(cut, events("turn2_after_result"))
    _seen, result = run(client, [{"role": "user", "content": "hi"}], saved)
    assert result.continuations == 1 and result.stop_reason == "end_turn"

    failed = copy.deepcopy(events("turn2_after_result"))
    failed[-1]["interaction"]["status"] = "failed"
    _seen, result = run(FakeClient(failed), [{"role": "user", "content": "hi"}], saved)
    assert result.stop_reason == "refusal"


def test_gemini_is_off_unless_flagged():
    with pytest.raises(ProviderUnavailable):
        factory.provider_for("gemini-3.8-flash", build_ai_settings())
    with pytest.raises(ProviderUnavailable):
        _ = GeminiProvider(build_ai_settings(llm_gemini_enabled=True)).client  # no key
    flagged = build_ai_settings(llm_gemini_enabled=True)
    assert factory.provider_for("gemini-3.8-flash", flagged).name == "gemini"
    factory.reset()
