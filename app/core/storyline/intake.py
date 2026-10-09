"""The intake turn: one interviewer reply and the brief fields it pinned down. ONE model call per turn.

Ported from Darwin `_shared/intake.ts` (`sanitizeBrief`, `runIntakeTurn`) and `intake.ts` (the request checks and
the transcript upsert), with Slide Studio's optional `format` field.

C14, fixed here at the core: Darwin made two paid calls on a turn whose answer had a tool call but no text (a
second, text-only follow-up). The turn is now one structured call whose schema requires `message` and `brief`,
so the reply always has both; an empty message gets Darwin's canned filler, never a second call. (The other
half of C14, the frontend calling `/api/intake` twice per turn, is a route decision; see docs/architecture.md,
"The intake turn and C14".)

Stateless like Darwin's: the client sends the whole transcript and the brief so far; the returned brief holds
only the fields changed this turn, which the client merges.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.config.storyline import StorylineSettings, storyline_settings
from app.core.storage.models import CallerContext, IntakeTranscriptUpsert
from app.core.storage.ports import TranscriptPort
from app.core.storyline import prompts
from app.core.storyline.models import IntakeBrief
from app.core.storyline.ports import Message, ModelUsage, StorylineModel, StructuredRequest

_log = logging.getLogger(__name__)

IMAGE_MEDIA_TYPES: frozenset[str] = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
#: Darwin's caps on the brief fields (`FIELD_SCHEMAS`): a field outside them is dropped, not trimmed.
TEXT_CAP = 300
CONTEXT_CAP = 4000
MAX_KEY_MESSAGES = 6

# The request errors (Darwin `intake.ts`), answered 400 with these messages by the route.
MESSAGES_REQUIRED = "messages array required"
MALFORMED_MESSAGE = "malformed message"
TOO_LONG = "Conversation too long — draft with what you have"


class IntakeRequestError(ValueError):
    """An `/api/intake` body the route answers with 400 and this message."""


@dataclass(frozen=True)
class IntakeOptions:
    #: Ask for Slide Studio's `format` (single / collection / deck) and return it in the brief. Off for Darwin's
    #: `/api/intake`, whose brief has no `format` field.
    ask_format: bool = False


@dataclass(frozen=True)
class IntakeTurn:
    message: str
    brief: IntakeBrief
    usage: ModelUsage = field(default_factory=ModelUsage)
    #: How many model calls the turn made: 0 (wrap-up, opener) or 1. Never 2 (C14).
    model_calls: int = 0

    def wire(self) -> dict[str, Any]:
        """Darwin's `/api/intake` 200 body: `{message, brief}` (brief = changed fields only)."""
        return {"message": self.message, "brief": self.brief.wire()}


# ------------------------------------------------------------------------------------------ the brief
def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if float(value).is_integer() else None


def sanitize_brief(raw: Any, *, ask_format: bool = False) -> IntakeBrief:
    """Per-field salvage (Darwin `sanitizeBrief`): keep every valid field, drop the rest, never raise."""
    r = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    for key, cap in (("topic", TEXT_CAP), ("audience", TEXT_CAP), ("context", CONTEXT_CAP)):
        value = r.get(key)
        if isinstance(value, str) and 1 <= len(value) <= cap:
            out[key] = value
    n = _int(r.get("numSlides"))
    if n is not None and n >= 1:
        out["num_slides"] = n
    msgs = r.get("keyMessages")
    if (isinstance(msgs, list) and 1 <= len(msgs) <= MAX_KEY_MESSAGES
            and all(isinstance(m, str) and 1 <= len(m) <= TEXT_CAP for m in msgs)):
        out["key_messages"] = list(msgs)
    if isinstance(r.get("ready"), bool):
        out["ready"] = r["ready"]
    if ask_format and r.get("format") in prompts.INTAKE_FORMATS:
        out["format"] = r["format"]
    return IntakeBrief(**out)


# --------------------------------------------------------------------------------------- the request
def _block_ok(block: Any) -> bool:
    if not isinstance(block, dict) or not isinstance(block.get("type"), str):
        return False
    kind = block["type"]
    if kind == "text":
        return isinstance(block.get("text"), str)
    if kind == "image":
        return isinstance(block.get("data"), str) and block.get("media_type") in IMAGE_MEDIA_TYPES
    if kind == "document":
        return isinstance(block.get("data"), str)
    return False


def _message_ok(m: Any) -> bool:
    if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
        return False
    content = m.get("content")
    return isinstance(content, str) or (isinstance(content, list) and all(_block_ok(b) for b in content))


def text_chars(messages: list[dict[str, Any]]) -> int:
    """Characters of text in a transcript: string contents and text blocks. Attachment bytes are not counted."""
    total = 0
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            total += len(content)
        else:
            total += sum(len(b.get("text") or "") for b in content if b.get("type") == "text")
    return total


def check_intake_request(body: Any, *, max_chars: int | None = None) -> list[dict[str, Any]]:
    """Darwin's checks in its order; returns the messages. Raises `IntakeRequestError` with Darwin's text."""
    limit = max_chars if max_chars is not None else storyline_settings.intake_max_transcript_chars
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list) or not messages:
        raise IntakeRequestError(MESSAGES_REQUIRED)
    if not all(_message_ok(m) for m in messages):
        raise IntakeRequestError(MALFORMED_MESSAGE)
    if text_chars(messages) > limit:
        raise IntakeRequestError(TOO_LONG)
    return messages


def to_model_content(content: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    """A transcript message's content as Messages API content (Darwin `toAnthropicContent`). Unknown keys are
    not forwarded; a document is always sent as a PDF."""
    if isinstance(content, str):
        return content
    out: list[dict[str, Any]] = []
    for b in content:
        if b["type"] == "text":
            out.append({"type": "text", "text": b["text"]})
        elif b["type"] == "image":
            out.append({"type": "image", "source": {"type": "base64", "media_type": b["media_type"], "data": b["data"]}})
        else:
            out.append({"type": "document",
                        "source": {"type": "base64", "media_type": "application/pdf", "data": b["data"]}})
    return out


# ------------------------------------------------------------------------------------------ the turn
def intake_request(messages: list[Message], brief: IntakeBrief, settings: StorylineSettings,
                   options: IntakeOptions) -> StructuredRequest:
    return StructuredRequest(
        purpose="intake",
        model=settings.model,
        system=prompts.intake_system(brief, options.ask_format),
        messages=messages,
        schema_name="intake_turn",
        schema=prompts.intake_schema(options.ask_format),
        max_tokens=settings.intake_max_tokens,
        effort=settings.intake_effort,  # type: ignore[arg-type]  # validated by check_storyline_config
    )


async def run_intake_turn(
    model: StorylineModel,
    messages: list[dict[str, Any]],
    current_brief: IntakeBrief,
    *,
    settings: StorylineSettings | None = None,
    options: IntakeOptions | None = None,
) -> IntakeTurn:
    """One interviewer turn over a checked transcript (`check_intake_request`) and a sanitized brief.

    No model call at the assistant-turn cap (wrap-up) or without a user message (the opener). Otherwise exactly
    one call. Model errors propagate (Darwin answers them 500)."""
    s = settings or storyline_settings
    opts = options or IntakeOptions()
    assistant_turns = sum(1 for m in messages if m["role"] == "assistant")
    # No forced ready: at the cap the brief may still lack a topic.
    if assistant_turns >= s.intake_max_assistant_turns:
        return IntakeTurn(message=prompts.WRAP_UP_MESSAGE, brief=IntakeBrief())
    # The client seeds a static assistant opener; the Messages API wants a user-first transcript.
    first_user = next((i for i, m in enumerate(messages) if m["role"] == "user"), None)
    if first_user is None:
        return IntakeTurn(message=prompts.OPENER_MESSAGE, brief=IntakeBrief())
    api_messages: list[Message] = [{"role": m["role"], "content": to_model_content(m["content"])}
                                   for m in messages[first_user:]]
    reply = await model.structured(intake_request(api_messages, current_brief, s, opts))
    message = ""
    brief = IntakeBrief()
    if reply.stop_reason == "refusal":
        _log.warning("intake: the model refused (model=%s)", reply.model or s.model)
    elif reply.data is not None:
        raw_message = reply.data.get("message")
        message = raw_message.strip() if isinstance(raw_message, str) else ""
        brief = sanitize_brief(reply.data.get("brief"), ask_format=opts.ask_format)
    else:
        _log.warning("intake: no JSON answer (stop_reason=%s)", reply.stop_reason)
    return IntakeTurn(message=message or prompts.FALLBACK_MESSAGE, brief=brief, usage=reply.usage, model_calls=1)


# ----------------------------------------------------------------------------------- the transcript
def transcript_upsert(session_key: str, messages: list[dict[str, Any]], brief: IntakeBrief) -> IntakeTranscriptUpsert:
    """What Darwin stores per turn (`intake.ts`): the inbound transcript with attachment bytes replaced by
    `[attachment]`, the inbound (sanitized) brief, the user-turn count and the text size."""
    stored: list[Any] = []
    for m in messages:
        content = m["content"]
        if not isinstance(content, str):
            content = [b if b["type"] == "text"
                       else {"type": b["type"], "data": "[attachment]", "media_type": b.get("media_type")}
                       for b in content]
        stored.append({"role": m["role"], "content": content})
    return IntakeTranscriptUpsert(
        session_key=session_key, messages=stored, brief=brief.wire(),
        turn_count=sum(1 for m in messages if m["role"] == "user"), total_chars=text_chars(messages),
    )


async def save_intake_transcript(
    transcripts: TranscriptPort, ctx: CallerContext, session_key: Any, messages: list[dict[str, Any]],
    brief: IntakeBrief,
) -> bool:
    """Upsert the transcript through the storage port when the client sent a session key. Never raises: Darwin
    upserts fire-and-forget and logs failures. True when it was stored."""
    if not session_key:
        return False
    if not isinstance(session_key, str) or len(session_key) > 200:
        _log.warning("intake: transcript not stored: the session key is not a string of at most 200 characters")
        return False
    try:
        await transcripts.upsert_intake(ctx, transcript_upsert(session_key, messages, brief))
    except Exception:  # noqa: BLE001 - a transcript is best-effort; the turn already succeeded
        _log.exception("intake: transcript upsert failed")
        return False
    return True
