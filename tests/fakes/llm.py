"""A scriptable fake model provider: every AI test runs on this, never on a real API.

`ScriptedProvider` implements the provider port (`app/core/llm/ports.py`). It is given a **script**:
a list of rounds, each either a fixed `Round` or a function of the request (so a step can read the
last tool result, e.g. the id `save_slide` answered with, and replay a tool call on it). Each
`stream_round` takes the next step, streams its text blocks as the real providers do (`text_start`,
`text`, `text_end`; a `status` for each tool call), and returns the `RoundResult`. `complete`
answers from a separate `completions` script (the critic, the brand role annotator).

Every request is recorded (`provider.requests`, `provider.completion_requests`) so a test can assert
what was sent: the system prompt, the tools offered, the history after a continuation.

A script that runs out raises `ScriptExhausted`: a test that makes one more paid round than it
expected fails loudly instead of looping.

Builders:

    text("Done.")                                   a text block
    tool("save_slide", {...})                       a tool_use block (id minted: toolu_fake_1, ...)
    Round(text("hi"), stop="end_turn", usage=...)   one scripted round
    last_tool_results(request)                      the tool_result blocks the request ends with
    saved_slide_id(request)                         "sld_..." from the last "Saved sld_... version N."
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from typing import Any

from app.core.llm.types import JSON, Event, RoundRequest, RoundResult, Usage

_ids = itertools.count(1)


class ScriptExhausted(AssertionError):
    """The code under test asked for a round the script did not plan for."""


def text(value: str) -> JSON:
    return {"type": "text", "text": value}


def tool(name: str, input: JSON, id: str | None = None) -> JSON:  # noqa: A002 - mirrors the block's field
    return {"type": "tool_use", "id": id or f"toolu_fake_{next(_ids)}", "name": name, "input": input}


def thinking(value: str, signature: str = "sig") -> JSON:
    return {"type": "thinking", "thinking": value, "signature": signature}


@dataclass
class Round:
    """One scripted round. `stop` defaults to `tool_use` when a block is a tool call, else `end_turn`."""

    blocks: list[JSON] = field(default_factory=list)
    stop: str | None = None
    usage: Usage = field(default_factory=lambda: Usage(input=1000, output=200))
    model: str | None = None  # the model that "served" it (a refusal fallback); default: the requested one

    def __init__(self, *blocks: JSON, stop: str | None = None, usage: Usage | None = None,
                 model: str | None = None) -> None:
        self.blocks = list(blocks)
        self.stop = stop
        self.usage = usage if usage is not None else Usage(input=1000, output=200)
        self.model = model

    def result(self, request: RoundRequest) -> RoundResult:
        stop = self.stop or ("tool_use" if any(b.get("type") == "tool_use" for b in self.blocks) else "end_turn")
        usage = Usage(self.usage.input, self.usage.output, self.usage.cache_read, self.usage.cache_write)
        return RoundResult(content=[dict(b) for b in self.blocks], stop_reason=stop, usage=usage,
                           model=self.model or request.model)


Step = Round | Callable[[RoundRequest], Round]


class ScriptedProvider:
    """The fake provider. See the module docstring."""

    def __init__(self, script: list[Step] | None = None, *, completions: list[Step] | None = None,
                 name: str = "fake") -> None:
        self.name = name
        self.script: list[Step] = list(script or [])
        self.completions: list[Step] = list(completions or [])
        self.requests: list[RoundRequest] = []
        self.completion_requests: list[RoundRequest] = []

    def add(self, *steps: Step) -> ScriptedProvider:
        self.script.extend(steps)
        return self

    @staticmethod
    def _snapshot(request: RoundRequest) -> RoundRequest:
        # the loop mutates the message list after the round: keep what was actually sent
        return RoundRequest(model=request.model, system=request.system,
                            messages=[dict(m) for m in request.messages], tools=list(request.tools),
                            max_tokens=request.max_tokens, effort=request.effort,
                            attachments_root=request.attachments_root, minimal_thinking=request.minimal_thinking)

    def stream_round(self, request: RoundRequest) -> Generator[Event, None, RoundResult]:
        self.requests.append(self._snapshot(request))
        if not self.script:
            raise ScriptExhausted(f"no scripted round left (round {len(self.requests)})")
        step = self.script.pop(0)
        scripted = step(request) if callable(step) else step
        result = scripted.result(request)
        for block in result.content:
            if block.get("type") == "text":
                yield {"type": "text_start"}
                yield {"type": "text", "text": block.get("text", "")}
                yield {"type": "text_end"}
            elif block.get("type") == "tool_use":
                yield {"type": "status", "text": f"Using {block.get('name')}…"}
        return result

    def complete(self, request: RoundRequest) -> RoundResult:
        self.completion_requests.append(self._snapshot(request))
        if not self.completions:
            raise ScriptExhausted("no scripted completion left")
        step = self.completions.pop(0)
        scripted = step(request) if callable(step) else step
        return scripted.result(request)

    @property
    def remaining(self) -> int:
        return len(self.script)


def resolver(provider: ScriptedProvider) -> Callable[[str], ScriptedProvider]:
    """A `ProviderResolver` that answers every model with `provider`."""
    return lambda _model: provider


def last_tool_results(request: RoundRequest) -> list[JSON]:
    last = request.messages[-1] if request.messages else {}
    content = last.get("content") if isinstance(last, dict) else None
    return [b for b in content or [] if isinstance(b, dict) and b.get("type") == "tool_result"]


def result_text(block: JSON) -> str:
    content: Any = block.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(str(c.get("text") or "") for c in content or [] if isinstance(c, dict))


_SAVED = re.compile(r"Saved (sld_[0-9a-f]+) version (\d+)")


def saved_slide_id(request: RoundRequest) -> str:
    """The slide id the most recent save/edit answered with (for replaying edit_slide/preview_slide on it)."""
    for message in reversed(request.messages):
        for block in reversed(message.get("content") or []):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                found = _SAVED.search(result_text(block))
                if found:
                    return found.group(1)
    raise AssertionError("no saved slide in the history yet")
