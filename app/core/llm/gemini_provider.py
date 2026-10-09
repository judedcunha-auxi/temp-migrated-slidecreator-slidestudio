"""The Gemini provider, behind `LLM_GEMINI_ENABLED`: the Interactions API, stateless and streamed.

Off by default. `app.core.llm.factory` refuses a Gemini model while the flag is off, and
`check_config` refuses a Gemini model in any `LLM_*_MODEL` setting then too.

What the API's measured behaviour shaped here (Slide Studio's WP0 notes, kept):

* `interaction.completed` carries no `steps` in stateless streaming, so the assistant turn is
  reassembled from `step.start` / `step.delta` / `step.stop`, and what is reassembled is what is
  stored (in `gemini_steps`) and resent next round, thought signatures included;
* "the model wants a tool result" is the *status* on `interaction.completed` (`requires_action`);
* errors are raised out of the iterator, not out of `create()`, so the whole loop is wrapped;
* usage: `total_output_tokens` excludes thinking, so output = output + thought tokens; cached tokens
  sit inside `total_input_tokens` and are subtracted back out; caching is implicit (no write cost).

Ported from Slide Studio `server/llm/gemini_client.py` (migration plan §4.1, K/R): Files API uploads
are dropped (attachments go inline, `history.to_gemini`), and the client is built from settings.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Generator
from typing import Any

from app.config.ai import AISettings, ai_settings
from app.core.llm import history
from app.core.llm.anthropic_provider import TOOL_LABELS
from app.core.llm.types import JSON, Event, ProviderError, ProviderUnavailable, RoundRequest, RoundResult, Usage

_log = logging.getLogger(__name__)

#: The SDK's HTTP timeout is in milliseconds.
MAX_OUTPUT_TOKENS = 65_536
#: `effort` -> `thinking_level`.
THINKING_LEVEL = {"minimal": "minimal", "none": "minimal", "low": "low", "medium": "medium",
                  "high": "high", "xhigh": "high", "max": "high"}


class GeminiProvider:
    """`LLMProvider` over google-genai's Interactions API."""

    name = "gemini"

    def __init__(self, settings: AISettings | None = None, client: Any = None) -> None:
        self.settings = settings if settings is not None else ai_settings
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            if not self.settings.llm_gemini_enabled:
                raise ProviderUnavailable("Gemini is switched off (LLM_GEMINI_ENABLED).")
            key = self.settings.gemini_key
            if not key:
                raise ProviderUnavailable("GEMINI_API_KEY is not configured.")
            from google import genai  # late: only a Gemini call needs the SDK loaded

            self._client = genai.Client(api_key=key,
                                        http_options={"timeout": int(self.settings.llm_request_timeout_s * 1000)})
        return self._client

    def params(self, request: RoundRequest, *, stream: bool) -> JSON:
        if request.output_schema is not None:
            raise ProviderError("structured output is not wired for Gemini; use a Claude model")
        params: JSON = {
            "model": request.model,
            "input": history.to_gemini(request.messages, request.attachments_root),
            "system_instruction": request.system,
            "generation_config": {
                "thinking_level": "minimal" if request.minimal_thinking else THINKING_LEVEL.get(request.effort, "high"),
                "thinking_summaries": "auto",
                "max_output_tokens": min(request.max_tokens, MAX_OUTPUT_TOKENS),
            },
            "store": False,
            "stream": stream,
        }
        tools = wire_tools(request.tools)
        if tools:
            params["tools"] = tools
        return params

    def stream_round(self, request: RoundRequest) -> Generator[Event, None, RoundResult]:
        steps: dict[int, JSON] = {}
        order: list[int] = []
        open_text: set[int] = set()
        completed: JSON | None = None
        stream: Any = None
        try:
            stream = self.client.interactions.create(**self.params(request, stream=True))
            for event in stream:
                data = _dump(event)
                if data.get("event_type") == "interaction.completed":
                    raw = data.get("interaction")
                    completed = raw if isinstance(raw, dict) else {}
                    continue
                yield from consume(data, steps, order, open_text)
            for _ in sorted(open_text):
                yield {"type": "text_end"}
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - the SDK raises its own errors out of the iterator
            raise ProviderError(f"Gemini call failed ({type(exc).__name__}).") from exc
        finally:
            if stream is not None and callable(getattr(stream, "close", None)):
                try:
                    stream.close()
                except Exception:  # noqa: BLE001 - a connection already gone needs no closing
                    _log.debug("gemini: stream close failed", exc_info=True)
        finished = [finish(steps[i]) for i in order]
        return round_from(finished, completed, request.model)

    def complete(self, request: RoundRequest) -> RoundResult:
        try:
            response = self.client.interactions.create(**self.params(request, stream=False))
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"Gemini call failed ({type(exc).__name__}).") from exc
        data = _dump(response)
        raw_steps = data.get("steps")
        steps = [s for s in raw_steps if isinstance(s, dict)] if isinstance(raw_steps, list) else []
        return round_from(steps, data, request.model)


def round_from(steps: list[JSON], completed: JSON | None, model: str) -> RoundResult:
    """A finished Gemini round as a canonical `RoundResult` (thoughts stay in `gemini_steps` only)."""
    blocks, has_calls = canonical(steps, history._minter())
    status = (completed or {}).get("status")
    if completed is None:
        stop = "end_turn"
    elif status == "incomplete":
        stop = "max_tokens"
    elif status in ("completed", "requires_action"):
        stop = "tool_use" if has_calls else "end_turn"
    else:
        stop = "refusal"
    return RoundResult(content=blocks, stop_reason=stop, usage=usage_of(completed), model=model,
                       extra={"gemini_steps": steps})


def consume(data: JSON, steps: dict[int, JSON], order: list[int], open_text: set[int]) -> Generator[Event, None, None]:
    """One streamed event: into the step being built, and out as what the person sees."""
    event_type = data.get("event_type")
    raw_index = data.get("index")
    index = int(raw_index) if isinstance(raw_index, int) else -1
    if event_type == "error":
        raise ProviderError("Gemini could not finish this request.")
    if event_type == "step.start":
        raw = data.get("step")
        step: JSON = dict(raw) if isinstance(raw, dict) else {}
        steps[index] = step
        order.append(index)
        kind = step.get("type")
        if kind == "thought":
            yield {"type": "status", "text": "Thinking…"}
        elif kind == "function_call":
            name = str(step.get("name") or "")
            yield {"type": "status", "text": TOOL_LABELS.get(name, f"Using {name}…")}
        return
    if event_type == "step.stop":
        if index in open_text:
            open_text.discard(index)
            yield {"type": "text_end"}
        return
    if event_type != "step.delta":
        return
    current = steps.get(index)
    if current is None:
        current = steps[index] = {}
        order.append(index)
    raw_delta = data.get("delta")
    delta: JSON = raw_delta if isinstance(raw_delta, dict) else {}
    kind = delta.get("type")
    if kind == "thought_summary":
        content = delta.get("content")
        text = str((content or {}).get("text") or "") if isinstance(content, dict) else ""
        current.setdefault("summary", [{"type": "text", "text": ""}])[0]["text"] += text
        if text:
            yield {"type": "thinking", "text": text}
    elif kind == "thought_signature":
        current["signature"] = str(current.get("signature", "")) + str(delta.get("signature") or "")
    elif kind == "arguments_delta":
        current["_json"] = str(current.get("_json", "")) + str(delta.get("arguments") or "")
    elif kind == "text":
        text = str(delta.get("text") or "")
        current.setdefault("content", [{"type": "text", "text": ""}])[0]["text"] += text
        if text:
            if index not in open_text:
                open_text.add(index)
                yield {"type": "text_start"}
            yield {"type": "text", "text": text}


def finish(step: JSON) -> JSON:
    """A reassembled step, with the arguments JSON that streamed in parsed into the step."""
    raw = step.pop("_json", None)
    if raw is None:
        return step
    try:
        step["arguments"] = json.loads(raw)
    except ValueError:
        step["arguments"] = {}  # the tool says what was wrong; the model tries again
    return step


def canonical(steps: list[JSON], mint: Callable[[], str]) -> tuple[list[JSON], bool]:
    """The turn as canonical blocks every model can read, and whether it called a tool."""
    blocks: list[JSON] = []
    calls = False
    for step in steps:
        kind = step.get("type")
        if kind == "model_output":
            text = "".join(str(c.get("text") or "") for c in step.get("content") or [] if isinstance(c, dict))
            if text:
                blocks.append({"type": "text", "text": text})
        elif kind == "function_call":
            call_id = str(step.get("id") or "") or mint()
            args = step.get("arguments")
            blocks.append({"type": "tool_use", "id": call_id, "name": str(step.get("name") or ""),
                           "input": args if isinstance(args, dict) else {}})
            calls = True
    return blocks, calls


def usage_of(completed: JSON | None) -> Usage:
    raw = (completed or {}).get("usage")
    usage: JSON = raw if isinstance(raw, dict) else {}
    cached = _count(usage.get("total_cached_tokens"))
    thoughts = _count(usage.get("total_thought_tokens"))
    return Usage(input=max(_count(usage.get("total_input_tokens")) - cached, 0),
                 output=_count(usage.get("total_output_tokens")) + thoughts,
                 cache_read=cached, cache_write=0, reasoning=thoughts)


def _count(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _dump(event: Any) -> JSON:
    dump = getattr(event, "model_dump", None)
    if callable(dump):
        data = dump(mode="json")
        return data if isinstance(data, dict) else {}
    return dict(event) if isinstance(event, dict) else {}


def wire_tools(tools: list[JSON]) -> list[JSON]:
    """Anthropic tool schemas as Gemini function declarations (provider hints dropped)."""
    wire: list[JSON] = []
    for tool in tools:
        schema = tool.get("input_schema") or tool.get("parameters")
        name = tool.get("name")
        if not name or not isinstance(schema, dict):
            continue
        wire.append({"type": "function", "name": name, "description": tool.get("description") or "",
                     "parameters": schema})
    return wire
