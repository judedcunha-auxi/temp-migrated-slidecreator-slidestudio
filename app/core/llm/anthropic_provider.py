"""The Anthropic provider (the primary one): one Claude round, streamed, behind the provider port.

Everything Claude-specific lives here: the official SDK, the request parameters and the betas. What
a model costs is `registry`/`pricing`; tool dispatch, continuation and cost totals are `loop.py`.

Request shape (from the `claude-api` reference, 2026-09):

* `client.beta.messages.stream(...)`, `max_tokens` from `LLM_MAX_TOKENS` (64k by default: streaming,
  so no HTTP timeout concern), the result read with `get_final_message()`;
* adaptive thinking with summarised display (`thinking: {"type": "adaptive", "display": "summarized"}`)
  on every model that takes it, and `output_config.effort` set explicitly (Claude Opus 5.5 defaults
  to `medium`, one level below what the design chat wants);
* top-level automatic prompt caching (`cache_control: {"type": "ephemeral"}`): the system prompt and
  the layout pictures are a stable prefix, so every round after the first reads them from cache;
* the server-side refusal fallback (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`)
  on the models that support it, unless `LLM_FALLBACKS_ENABLED` is off; the round is then priced
  with the model that served it (`final.model`);
* context editing (`clear_tool_uses_20250919`, beta `context-management-2025-06-27`): previews pile
  up large image tool results, and a long turn must not run out of context.

Dropped from Slide Studio's client: the code-execution sandbox, its Office skills and the Files API
(the service sends attachments inline and has no Office attachments), and the per-process key
storage (the key comes from settings).
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from typing import Any

import anthropic

from app.config.ai import AISettings, ai_settings
from app.core.llm import history, registry
from app.core.llm.types import Event, ProviderError, ProviderUnavailable, RoundRequest, RoundResult, Usage

_log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
CONTEXT_BETA = "context-management-2025-06-27"
CONTEXT_MANAGEMENT: dict[str, Any] = {
    "edits": [{
        "type": "clear_tool_uses_20250919",
        "trigger": {"type": "input_tokens", "value": 250000},
        "keep": {"type": "tool_uses", "value": 4},
        "clear_at_least": {"type": "input_tokens", "value": 50000},
    }]
}

#: What the person sees while a tool call streams in.
TOOL_LABELS = {
    "save_slide": "Designing slide…", "read_slide": "Reading slide…", "edit_slide": "Editing slide…",
    "find_layout_reference": "Looking up layout references…", "plan_exhibit": "Planning the exhibit…",
    "write_brief": "Writing the brief…", "preview_slide": "Previewing slide…",
    "get_component": "Fetching a component…", "get_exemplars": "Looking at example slides…",
}


def usage_of(raw: Any) -> Usage:
    """A Claude response's usage block as `Usage` (unpriced)."""
    return Usage(
        input=int(getattr(raw, "input_tokens", 0) or 0),
        output=int(getattr(raw, "output_tokens", 0) or 0),
        cache_read=int(getattr(raw, "cache_read_input_tokens", 0) or 0),
        cache_write=int(getattr(raw, "cache_creation_input_tokens", 0) or 0),
    )


def _dump(content: Any) -> list[dict[str, Any]]:
    return [b.model_dump(mode="json", exclude_none=True) for b in content or []]


class AnthropicProvider:
    """`LLMProvider` over the official SDK. One instance per process (it holds the HTTP client)."""

    name = "anthropic"

    def __init__(self, settings: AISettings | None = None, client: Any = None) -> None:
        self.settings = settings if settings is not None else ai_settings
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            key = self.settings.anthropic_key
            if not key:
                raise ProviderUnavailable("ANTHROPIC_API_KEY is not configured.")
            self._client = anthropic.Anthropic(api_key=key, timeout=self.settings.llm_request_timeout_s)
        return self._client

    # ------------------------------------------------------------------------------ params
    def params(self, request: RoundRequest, *, streaming: bool) -> dict[str, Any]:
        """The request body for one round (also what the tests assert on)."""
        model = registry.get(request.model)
        params: dict[str, Any] = {
            "model": model.id,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": history.to_anthropic(request.messages, model.id, request.attachments_root),
            "cache_control": {"type": "ephemeral"},
        }
        betas: list[str] = []
        if request.minimal_thinking:
            if model.minimal_thinking is not None:
                params["thinking"] = dict(model.minimal_thinking)
        elif model.adaptive:
            params["thinking"] = {"type": "adaptive", "display": "summarized"}
        output: dict[str, Any] = {}
        if model.effort:
            output["effort"] = request.effort
        if request.output_schema is not None:
            output["format"] = {"type": "json_schema", "schema": request.output_schema}
        if output:
            params["output_config"] = output
        if request.tools:
            params["tools"] = request.tools
        if streaming:
            betas.append(CONTEXT_BETA)
            params["context_management"] = CONTEXT_MANAGEMENT
        if model.fallbacks and self.settings.llm_fallbacks_enabled:
            betas.append(FALLBACK_BETA)
            params["fallbacks"] = "default"
        if betas:
            params["betas"] = betas
        return params

    # ------------------------------------------------------------------------------ rounds
    def stream_round(self, request: RoundRequest) -> Generator[Event, None, RoundResult]:
        params = self.params(request, streaming=True)
        kinds: dict[int, str] = {}
        try:
            with self.client.beta.messages.stream(**params) as stream:
                for event in stream:
                    yield from self._event(event, kinds)
                final = stream.get_final_message()
        except anthropic.APIError as exc:
            raise ProviderError(_describe(exc)) from exc
        return RoundResult(content=_dump(final.content), stop_reason=str(final.stop_reason or "end_turn"),
                           usage=usage_of(final.usage), model=str(getattr(final, "model", "") or request.model))

    @staticmethod
    def _event(event: Any, kinds: dict[int, str]) -> Generator[Event, None, None]:
        et = getattr(event, "type", "")
        if et == "content_block_start":
            block = event.content_block
            kinds[event.index] = block.type
            if block.type == "text":
                yield {"type": "text_start"}
            elif block.type == "thinking":
                yield {"type": "status", "text": "Thinking…"}
            elif block.type == "tool_use":
                yield {"type": "status", "text": TOOL_LABELS.get(block.name, f"Using {block.name}…")}
        elif et == "content_block_delta":
            delta = event.delta
            if delta.type == "text_delta":
                yield {"type": "text", "text": delta.text}
            elif delta.type == "thinking_delta" and delta.thinking:
                yield {"type": "thinking", "text": delta.thinking}
        elif et == "content_block_stop" and kinds.get(event.index) == "text":
            yield {"type": "text_end"}

    def complete(self, request: RoundRequest) -> RoundResult:
        params = self.params(request, streaming=False)
        try:
            response = self.client.with_options(max_retries=1).beta.messages.create(**params)
        except anthropic.APIError as exc:
            raise ProviderError(_describe(exc)) from exc
        return RoundResult(content=_dump(response.content), stop_reason=str(response.stop_reason or "end_turn"),
                           usage=usage_of(response.usage), model=str(getattr(response, "model", "") or request.model))


def _describe(exc: anthropic.APIError) -> str:
    """A short, key-free description of an API failure, for logs and job errors."""
    status = getattr(exc, "status_code", None)
    kind = type(exc).__name__
    return f"Claude API call failed ({kind}{f' {status}' if status else ''})."
