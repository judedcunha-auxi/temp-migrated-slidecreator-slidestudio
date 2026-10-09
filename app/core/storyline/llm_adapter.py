"""`StorylineModel` over the provider port (`app/core/llm`): the real model behind the storyline and intake.

The storyline core asks for one structured call (`ports.StructuredRequest`); this maps it onto one
provider round with `output_schema` (structured output, `output_config.format`), streamed (a 64k
`max_tokens` needs streaming) in a worker thread, because providers are synchronous. It never
touches a vendor SDK: the provider does (`app/core/llm/anthropic_provider.py`).

* The reply's text is the JSON answer; `data` is None for a refusal, a reply cut off at
  `max_tokens`, or text that is not a JSON object (the core turns those into its own errors).
* Usage is priced with the model that actually answered (`app/core/llm/pricing.py`).
* Errors: no provider for the model (unknown id, no key) is `ModelRejected`; a failed call is
  `ModelUnavailable` (retryable: the provider's error does not say which side failed, and a retry of
  a rejected request costs one more failed call, never a paid answer).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.config.ai import AISettings
from app.core.llm.ports import ProviderResolver
from app.core.llm.pricing import priced
from app.core.llm.types import ProviderError, ProviderUnavailable, RoundRequest, RoundResult
from app.core.storyline.ports import ModelRejected, ModelUnavailable, ModelUsage, StructuredReply, StructuredRequest

_log = logging.getLogger(__name__)


class LlmStorylineModel:
    def __init__(self, resolve: ProviderResolver, settings: AISettings | None = None) -> None:
        self._resolve = resolve
        self._settings = settings

    async def structured(self, request: StructuredRequest) -> StructuredReply:
        try:
            provider = self._resolve(request.model)
        except ProviderUnavailable as exc:
            raise ModelRejected(str(exc)) from exc
        round_request = RoundRequest(model=request.model, system=request.system, messages=list(request.messages),
                                     max_tokens=request.max_tokens, effort=request.effort or "high",
                                     output_schema=request.schema)

        def run() -> RoundResult:
            stream = provider.stream_round(round_request)
            while True:
                try:
                    next(stream)
                except StopIteration as done:
                    return done.value  # type: ignore[no-any-return]

        try:
            result = await asyncio.to_thread(run)
        except ProviderUnavailable as exc:
            raise ModelRejected(str(exc)) from exc
        except ProviderError as exc:
            raise ModelUnavailable(str(exc)) from exc
        model = result.model or request.model
        try:
            usage = priced(result.usage, model, self._settings)
        except KeyError:
            usage = priced(result.usage, request.model, self._settings)
        text = result.text()
        data: dict[str, Any] | None = None
        if result.stop_reason not in ("refusal", "max_tokens"):
            try:
                parsed = json.loads(text)
            except ValueError:
                _log.warning("storyline: the structured answer is not JSON (stop_reason=%s)", result.stop_reason)
            else:
                data = parsed if isinstance(parsed, dict) else None
        return StructuredReply(
            data=data, stop_reason=result.stop_reason, model=model, text=text,
            usage=ModelUsage(input_tokens=usage.input, output_tokens=usage.output,
                             cache_read_input_tokens=usage.cache_read, cache_creation_input_tokens=usage.cache_write,
                             cost_usd=usage.cost_usd),
        )
