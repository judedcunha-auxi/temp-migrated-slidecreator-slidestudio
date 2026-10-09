"""The provider port: what the turn loop, the critic and brand extraction need from a model vendor.

A provider does exactly one round at a time and knows nothing about tools' meaning, cost, retries of
a cut-off reply or the design chat. That all lives in `app/core/llm/loop.py`, written once against
this port, so the Anthropic provider, the Gemini provider and the scripted fake in
`tests/fakes/llm.py` behave identically above it.

* `stream_round(request)` is a generator: it yields UI events as the round streams (text deltas
  between `text_start`/`text_end`, `status`, `thinking`) and **returns** the `RoundResult` (the
  generator's value, read with `yield from`).
* `complete(request)` is one small non-streaming round (the critic, the brand role annotator).

Both take the canonical history and render it for their own wire; neither mutates the request.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Protocol, runtime_checkable

from app.core.llm.types import Event, RoundRequest, RoundResult


@runtime_checkable
class LLMProvider(Protocol):
    #: "anthropic", "gemini", or a test double's name.
    name: str

    def stream_round(self, request: RoundRequest) -> Generator[Event, None, RoundResult]: ...

    def complete(self, request: RoundRequest) -> RoundResult: ...


#: model id -> the provider that serves it. The service's is `app.core.llm.factory.resolver()`;
#: tests hand in one that returns a scripted fake.
ProviderResolver = Callable[[str], LLMProvider]
