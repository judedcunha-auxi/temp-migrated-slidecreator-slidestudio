"""The shapes every provider and the turn loop share: usage and cost, a round, a turn, a tool call.

Messages and content blocks stay plain JSON dicts in the **canonical** history format (Anthropic block
shapes, plus a `model` stamp on each assistant turn and provider-private keys the other provider
strips; see `app/core/llm/history.py`). Everything else is a small dataclass so the loop, the design
chat and the tests agree on field names.

Events (what a turn yields while it runs) are dicts too, because they are relayed onward as they are:

``{"type":"text_start"}`` · ``{"type":"text","text"}`` · ``{"type":"text_end"}`` ·
``{"type":"status","text"}`` · ``{"type":"thinking","text"}`` ·
``{"type":"usage","round":{...},"cost":float}`` · ``{"type":"continuation","n":int,"reason":"max_tokens"}`` ·
``{"type":"error","text"}`` · ``{"type":"stop","reason":...}``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

JSON = dict[str, Any]
Event = dict[str, Any]


class ProviderError(RuntimeError):
    """A provider call failed (network, a rejected request, an outage). The message is safe to log
    and never carries a key; callers turn it into a short error for the person."""


class ProviderUnavailable(ProviderError):
    """No provider can serve this model here: the key is missing, or the provider is switched off."""


class Cancelled(Exception):  # noqa: N818 - a signal, not an error
    """The turn was stopped before its next paid round (the job was cancelled or timed out)."""


@dataclass
class Usage:
    """Tokens one round (or a whole turn) used, and what that cost in USD.

    `cost_usd` is priced per round with the model that ran the round and only ever **added**, never
    re-derived from a token total: a turn may span models (a refusal fallback, the critic), and one
    price table cannot price a mixed total.
    """

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0  # informational (Gemini's thought tokens, already inside `output`)
    cost_usd: float = 0.0

    def add(self, other: Usage) -> Usage:
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.reasoning += other.reasoning
        self.cost_usd = round(self.cost_usd + other.cost_usd, 6)
        return self

    def tokens(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def to_json(self) -> JSON:
        return {"input": self.input, "output": self.output, "cacheRead": self.cache_read,
                "cacheWrite": self.cache_write, "reasoning": self.reasoning, "costUsd": round(self.cost_usd, 6)}


@dataclass(frozen=True)
class ToolCall:
    """One tool call the model made: what the tool handler receives."""

    id: str
    name: str
    input: JSON


@dataclass
class RoundRequest:
    """One provider round: everything a provider needs, in canonical form."""

    model: str
    system: str
    messages: list[JSON]
    tools: list[JSON] = field(default_factory=list)
    max_tokens: int = 64000
    effort: str = "high"
    #: Directory attachment blocks' `path` is relative to (the job workspace); None: no attachments.
    attachments_root: Any = None
    #: Ask for as little reasoning as the model allows (the critic, the role annotator).
    minimal_thinking: bool = False


@dataclass
class RoundResult:
    """What one round produced: canonical content blocks, why it stopped, and what it used.

    `model` is the model that actually served the round (a server-side refusal fallback can answer
    on another model, and the round is priced with that one). `extra` is merged into the stored
    assistant message (Gemini keeps its raw steps there for replay).
    """

    content: list[JSON]
    stop_reason: str
    usage: Usage
    model: str
    extra: JSON = field(default_factory=dict)

    def tool_calls(self) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for block in self.content:
            if block.get("type") == "tool_use":
                raw = block.get("input")
                calls.append(ToolCall(str(block.get("id") or ""), str(block.get("name") or ""),
                                      raw if isinstance(raw, dict) else {}))
        return calls

    def text(self) -> str:
        return "".join(str(b.get("text") or "") for b in self.content if b.get("type") == "text")


@dataclass
class TurnResult:
    """A whole turn: why it ended, how many rounds and continuations it took, and its total cost."""

    stop_reason: str
    model: str
    rounds: int = 0
    continuations: int = 0
    usage: Usage = field(default_factory=Usage)
    error: str | None = None

    @property
    def cost_usd(self) -> float:
        return round(self.usage.cost_usd, 6)
