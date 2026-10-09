"""The one thing the storyline needs from a language model, as a port.

`StorylineModel.structured()` is one model call that must answer with JSON matching a schema: the storyline
draft and the intake turn are both that. It returns the parsed JSON, the stop reason, the usage and its
cost. The storyline never imports a provider SDK; `app/core/llm` (built separately) supplies the real
implementation through a thin adapter, and the tests use `tests/fakes/storyline_model.py`.

The shapes are deliberately the Anthropic Messages API's, so the adapter is a straight mapping:

* `messages` are Messages API messages: `{"role": "user"|"assistant", "content": str | [block]}` where a
  block is `{"type": "text", "text"}`, `{"type": "image", "source": {"type": "base64", "media_type", "data"}}`
  or `{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data"}}`;
* `schema` goes to `output_config.format` as `{"type": "json_schema", "schema": schema}` (structured
  outputs). It is written to the structured-output subset: every object has `additionalProperties: false`,
  no numeric, string-length or array-length constraints (those are enforced here, after the call);
* `effort` goes to `output_config.effort`; `max_tokens` as is (large values need streaming);
* `stop_reason` is the API's (`end_turn`, `max_tokens`, `refusal`, ...).

Errors: an adapter raises `ModelUnavailable` for what a retry may fix (429, 5xx, overloaded, network,
timeout) and `ModelRejected` for what it cannot (400, 401, 403, 404). A refusal is not an error: it is a
reply with `stop_reason == "refusal"` (and usually no `data`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Effort = Literal["low", "medium", "high", "xhigh", "max"]
Purpose = Literal["storyline", "intake"]

#: One Messages API message (see the module docstring for the block shapes).
Message = dict[str, Any]


@dataclass(frozen=True)
class StructuredRequest:
    purpose: Purpose
    model: str
    system: str
    messages: list[Message]
    schema_name: str
    schema: dict[str, Any]
    max_tokens: int
    effort: Effort | None = None


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    #: What the call cost, priced by the adapter (it knows the model's rates).
    cost_usd: float = 0.0


@dataclass(frozen=True)
class StructuredReply:
    #: The parsed JSON object, or None when the reply has none (a refusal, a cut-off reply).
    data: dict[str, Any] | None
    stop_reason: str
    usage: ModelUsage = field(default_factory=ModelUsage)
    #: The model that answered (may differ from the request's after a server-side fallback).
    model: str = ""
    #: The raw text of the reply, for logs only.
    text: str = ""


@runtime_checkable
class StorylineModel(Protocol):
    async def structured(self, request: StructuredRequest) -> StructuredReply: ...


class ModelError(Exception):
    """The model call failed. The message is for logs; user-facing text comes from the caller."""


class ModelUnavailable(ModelError):
    """A failure a retry may fix: rate limit, overload, a 5xx, a network error, a timeout."""


class ModelRejected(ModelError):
    """A failure a retry cannot fix: the request is invalid, or the credentials are."""


class SlidePrompter(Protocol):
    """Builds the per-slide `prompt` Darwin's `/api/storyline-status` result carries (its image-mode prompt,
    `_shared/prompt.ts: assembleSlidePrompt`, from the deck's brand kit). Image-mode prompts are not part of
    the storyline core; the Darwin route layer passes one in. One string per slide, in order."""

    async def __call__(self, owner_subject: str, inputs: dict[str, Any], slides: list[dict[str, Any]]) -> list[str]: ...
