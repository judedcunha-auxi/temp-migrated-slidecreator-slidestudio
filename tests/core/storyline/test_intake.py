"""The intake turn: Darwin `_shared/intake.test.ts` + `_tests/intake.test.ts` (contract api-intake.json), Slide Studio's
intake tests, and C14: exactly one model call per turn, never a follow-up."""

from __future__ import annotations

from typing import Any

import pytest

from app.config.storyline import StorylineSettings
from app.core.storyline import prompts
from app.core.storyline.intake import (
    MALFORMED_MESSAGE,
    MESSAGES_REQUIRED,
    TOO_LONG,
    IntakeOptions,
    IntakeRequestError,
    check_intake_request,
    run_intake_turn,
    sanitize_brief,
    save_intake_transcript,
    transcript_upsert,
)
from app.core.storyline.models import IntakeBrief
from app.core.storyline.ports import ModelUnavailable
from tests.fakes.general_service import FakeGeneralService
from tests.fakes.storyline_model import ScriptedStorylineModel, reply

OPENER = {"role": "assistant", "content": "What deck do you need?"}


def user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


# -------------------------------------------------------------------------------------------- sanitize
def test_sanitize_keeps_valid_fields_and_drops_malformed_ones_individually():
    got = sanitize_brief({"topic": "GTM", "numSlides": "ten", "ready": "yes", "audience": 42})
    assert got.wire() == {"topic": "GTM"}


def test_sanitize_drops_oversized_fields_rather_than_trimming_them():
    got = sanitize_brief({"topic": "x" * 301, "context": "c" * 4001, "keyMessages": ["m"] * 7, "audience": "Board"})
    assert got.wire() == {"audience": "Board"}
    assert sanitize_brief({"keyMessages": ["ok", "x" * 301]}).wire() == {}


def test_sanitize_accepts_a_fully_valid_brief_and_drops_unknown_keys():
    raw = {"topic": "T", "audience": "A", "numSlides": 8, "keyMessages": ["a", "b"], "context": "c", "ready": True,
           "format": "deck", "evil": "x"}
    assert sanitize_brief(raw).wire() == {k: v for k, v in raw.items() if k not in ("format", "evil")}
    assert sanitize_brief(raw, ask_format=True).format == "deck"


@pytest.mark.parametrize("garbage", [None, "x", 3, [], {"numSlides": 0}, {"numSlides": 2.5}, {"numSlides": True}])
def test_sanitize_handles_garbage(garbage: Any):
    assert sanitize_brief(garbage).wire() == {}


def test_sanitize_keeps_large_slide_counts():
    assert sanitize_brief({"numSlides": 40}).num_slides == 40  # Darwin's schema has no max
    assert sanitize_brief({"numSlides": 9.0}).num_slides == 9


# ------------------------------------------------------------------------------------ request checks
@pytest.mark.parametrize("body", [None, {}, {"messages": []}, {"messages": "hi"}, []])
def test_missing_messages_are_rejected(body: Any):
    with pytest.raises(IntakeRequestError, match=MESSAGES_REQUIRED):
        check_intake_request(body)


@pytest.mark.parametrize("message", [
    {"role": "system", "content": "x"},
    {"role": "user", "content": 42},
    {"role": "user", "content": [{"type": "video", "data": "x"}]},
    {"role": "user", "content": [{"type": "image", "data": "x", "media_type": "image/tiff"}]},
    {"role": "user", "content": [{"type": "text"}]},
    "hello",
])
def test_malformed_messages_are_rejected(message: Any):
    with pytest.raises(IntakeRequestError, match=MALFORMED_MESSAGE):
        check_intake_request({"messages": [message]})


def test_oversize_transcripts_are_rejected_counting_text_only():
    big = [user("x" * 20_000), user("y" * 10_001)]
    with pytest.raises(IntakeRequestError, match=TOO_LONG):
        check_intake_request({"messages": big})
    with_attachment = [{"role": "user", "content": [{"type": "text", "text": "see file"},
                                                     {"type": "document", "data": "Z" * 100_000}]}]
    assert check_intake_request({"messages": with_attachment}) == with_attachment
    with pytest.raises(IntakeRequestError):
        check_intake_request({"messages": [user("12345")]}, max_chars=4)


# --------------------------------------------------------------------------------------------- the turn
@pytest.mark.asyncio
async def test_a_turn_makes_one_call_and_returns_the_message_and_the_changed_fields(settings: StorylineSettings):
    model = ScriptedStorylineModel({"message": "Who is the audience?", "brief": {"topic": "GTM", "bogus": 1}})
    messages = check_intake_request({"messages": [OPENER, user("A deck on our GTM plan")]})
    turn = await run_intake_turn(model, messages, sanitize_brief({"audience": "Board"}), settings=settings)

    assert turn.wire() == {"message": "Who is the audience?", "brief": {"topic": "GTM"}}
    assert model.calls == 1 and turn.model_calls == 1
    request = model.requests[0]
    assert (request.purpose, request.max_tokens, request.effort) == ("intake", 4096, "low")
    assert request.schema == prompts.intake_schema()
    assert request.system.endswith('{"audience":"Board"}')
    assert [m["role"] for m in request.messages] == ["user"]  # the leading assistant opener is stripped


@pytest.mark.asyncio
async def test_a_turn_with_no_message_gets_the_filler_not_a_second_call(settings: StorylineSettings):
    """C14 / the old follow-up: Darwin made a second, text-only call here. Now: one call, canned filler."""
    model = ScriptedStorylineModel({"message": "   ", "brief": {"ready": True}})
    turn = await run_intake_turn(model, [user("Yes, go")], IntakeBrief(), settings=settings)
    assert turn.wire() == {"message": prompts.FALLBACK_MESSAGE, "brief": {"ready": True}}
    assert model.calls == 1


@pytest.mark.asyncio
async def test_every_turn_of_a_conversation_is_one_call(settings: StorylineSettings):
    script = [{"message": f"Question {i}?", "brief": {}} for i in range(5)]
    model = ScriptedStorylineModel(*script)
    transcript: list[dict[str, Any]] = [OPENER]
    brief = IntakeBrief()
    for i in range(5):
        transcript.append(user(f"answer {i}"))
        turn = await run_intake_turn(model, check_intake_request({"messages": transcript}), brief, settings=settings)
        transcript.append({"role": "assistant", "content": turn.message})
        assert model.calls == i + 1
    assert model.steps == []


@pytest.mark.asyncio
async def test_the_assistant_turn_cap_wraps_up_without_a_call(settings: StorylineSettings):
    model = ScriptedStorylineModel()
    transcript = [m for _ in range(12) for m in (user("x"), {"role": "assistant", "content": "y"})]
    turn = await run_intake_turn(model, transcript, IntakeBrief(topic="T"), settings=settings)
    assert turn.wire() == {"message": prompts.WRAP_UP_MESSAGE, "brief": {}}
    assert model.calls == 0 and turn.model_calls == 0


@pytest.mark.asyncio
async def test_no_user_message_gets_the_opener_without_a_call(settings: StorylineSettings):
    model = ScriptedStorylineModel()
    turn = await run_intake_turn(model, [OPENER], IntakeBrief(), settings=settings)
    assert turn.message == prompts.OPENER_MESSAGE and model.calls == 0


@pytest.mark.asyncio
async def test_attachments_reach_the_model_as_messages_api_blocks(settings: StorylineSettings):
    model = ScriptedStorylineModel({"message": "Got it.", "brief": {}})
    messages = check_intake_request({"messages": [{"role": "user", "content": [
        {"type": "text", "text": "Here are the numbers", "extra": "dropped"},
        {"type": "image", "media_type": "image/png", "data": "iVBOR"},
        {"type": "document", "data": "JVBER"},
    ]}]})
    await run_intake_turn(model, messages, IntakeBrief(), settings=settings)
    assert model.requests[0].messages[0]["content"] == [
        {"type": "text", "text": "Here are the numbers"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBOR"}},
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBER"}},
    ]


@pytest.mark.asyncio
async def test_a_refusal_or_a_cut_off_reply_gets_the_filler(settings: StorylineSettings):
    for step in (reply(None, stop_reason="refusal"), reply(None, stop_reason="max_tokens")):
        turn = await run_intake_turn(ScriptedStorylineModel(step), [user("hi")], IntakeBrief(), settings=settings)
        assert turn.wire() == {"message": prompts.FALLBACK_MESSAGE, "brief": {}}


@pytest.mark.asyncio
async def test_a_model_error_propagates_for_the_route_to_answer_500(settings: StorylineSettings):
    with pytest.raises(ModelUnavailable):
        await run_intake_turn(ScriptedStorylineModel(ModelUnavailable("429")), [user("hi")], IntakeBrief(),
                              settings=settings)


@pytest.mark.asyncio
async def test_format_is_asked_for_and_returned_only_on_request(settings: StorylineSettings):
    model = ScriptedStorylineModel({"message": "How many slides?", "brief": {"format": "collection"}})
    turn = await run_intake_turn(model, [user("A few slides on pricing")], IntakeBrief(), settings=settings,
                                 options=IntakeOptions(ask_format=True))
    assert turn.brief.format == "collection" and "format" in model.requests[0].schema["properties"]["brief"]["properties"]
    plain = ScriptedStorylineModel({"message": "Ok?", "brief": {"format": "deck"}})
    assert (await run_intake_turn(plain, [user("x")], IntakeBrief(), settings=settings)).brief.wire() == {}


# ---------------------------------------------------------------------------------------- the transcript
def test_the_transcript_strips_attachment_bytes():
    messages = [OPENER, {"role": "user", "content": [{"type": "text", "text": "file:"},
                                                     {"type": "image", "media_type": "image/png", "data": "BYTES"},
                                                     {"type": "document", "data": "PDF"}]}]
    up = transcript_upsert("sess-1", messages, IntakeBrief(topic="T"))
    assert up.messages[1]["content"] == [{"type": "text", "text": "file:"},
                                         {"type": "image", "data": "[attachment]", "media_type": "image/png"},
                                         {"type": "document", "data": "[attachment]", "media_type": None}]
    assert (up.turn_count, up.total_chars, up.brief) == (1, len("What deck do you need?") + 5, {"topic": "T"})


@pytest.mark.asyncio
async def test_the_transcript_is_stored_through_the_storage_port():
    storage = FakeGeneralService()
    ctx, _ = await storage.make_user("alice")
    messages = [OPENER, user("A GTM deck")]
    assert await save_intake_transcript(storage.transcripts, ctx, "sess-1", messages, IntakeBrief(topic="GTM"))
    saved = await storage.transcripts.get_intake(ctx, "sess-1")
    assert saved.turn_count == 1 and saved.brief == {"topic": "GTM"} and saved.messages[1]["content"] == "A GTM deck"
    assert ("transcripts.upsert_intake", "alice", "req-test") in [(c.operation, c.subject, c.request_id)
                                                                  for c in storage.calls]


@pytest.mark.asyncio
async def test_no_session_key_stores_nothing_and_a_storage_failure_never_raises():
    storage = FakeGeneralService()
    ctx, _ = await storage.make_user("alice")
    assert not await save_intake_transcript(storage.transcripts, ctx, None, [user("x")], IntakeBrief())
    assert not await save_intake_transcript(storage.transcripts, ctx, {"not": "a string"}, [user("x")], IntakeBrief())

    class Broken:
        async def upsert_intake(self, *_: Any) -> Any:
            raise ConnectionError("general service down")

    assert not await save_intake_transcript(Broken(), ctx, "sess", [user("x")], IntakeBrief())  # type: ignore[arg-type]
