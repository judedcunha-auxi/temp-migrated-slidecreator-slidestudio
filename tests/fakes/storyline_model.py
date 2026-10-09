"""A scriptable `StorylineModel` (app/core/storyline/ports.py): no call leaves the process, nothing is paid.

Each call to `structured()` plays the next scripted step and records the request:

* a dict: answered as structured data (`stop_reason="end_turn"`);
* a `StructuredReply`: returned as is (a refusal, a cut-off reply, a fallback model);
* an exception (instance): raised (`ModelUnavailable`, `ModelRejected`);
* a callable: called with the request; its return value is played as above.

Running past the script is a test failure (`AssertionError`), so a test that expects one call and makes two
fails loudly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.core.storyline.ports import ModelUsage, StructuredReply, StructuredRequest

Step = dict[str, Any] | StructuredReply | BaseException | Callable[[StructuredRequest], Any]

#: What one fake call costs, so cost plumbing is visible in assertions.
FAKE_COST = 0.0125


def reply(data: dict[str, Any] | None, *, stop_reason: str = "end_turn", model: str = "fake-model",
          cost: float = FAKE_COST) -> StructuredReply:
    return StructuredReply(data=data, stop_reason=stop_reason, model=model,
                           usage=ModelUsage(input_tokens=1000, output_tokens=500, cost_usd=cost))


class ScriptedStorylineModel:
    def __init__(self, *steps: Step) -> None:
        self.steps: list[Step] = list(steps)
        self.requests: list[StructuredRequest] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def structured(self, request: StructuredRequest) -> StructuredReply:
        self.requests.append(request)
        assert self.steps, f"unexpected model call #{len(self.requests)} ({request.purpose})"
        step: Any = self.steps.pop(0)
        if callable(step) and not isinstance(step, (dict, StructuredReply, BaseException)):
            step = step(request)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, StructuredReply):
            return step
        return reply(step)
