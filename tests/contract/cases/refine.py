"""Contract cases: /api/refine, /api/revert, /api/slide-transcript (data/api-{refine,revert,slide-transcript}.json)."""

from __future__ import annotations

import base64
from typing import Any

from app.core.darwin.refine import REFINE_JOB
from tests.contract.cases.common import add_auth, broken
from tests.contract.harness import Probe, RouteCases
from tests.fakes.darwin import DarwinEnv
from tests.fakes.darwin_decks import UNKNOWN, empty_deck, memo, slide
from tests.fakes.image_gen import tiny_png


def post(path: str, body: Any = None, *, raw: bytes | None = None, subject: str = "alice") -> Any:
    def run(env: DarwinEnv) -> Any:
        if raw is not None:
            return env.client.post(path, content=raw, headers=env.auth(subject))
        return env.client.post(path, json=body, headers=env.auth(subject))
    return run


def with_deck(path: str, make: Any, *, subject: str = "alice", owner: str = "alice", refined: bool = False) -> Any:
    """POST `make(deck_id)` to `path`, against a fresh seeded deck of `owner`."""
    def run(env: DarwinEnv) -> Any:
        deck_id = env.seed_deck(owner, slides=2, refined=refined)
        memo(env)["deck"] = deck_id
        return env.client.post(path, json=make(deck_id), headers=env.auth(subject))
    return run


# --------------------------------------------------------------------------------------------- refine
refine = RouteCases("/api/refine")
add_auth(refine, "POST", "/api/refine", json={"deckId": UNKNOWN, "number": 1, "instruction": "x"})

PNG_B64 = base64.b64encode(tiny_png()).decode()


def _refined(env: DarwinEnv, r: Any) -> None:
    deck_id = memo(env)["deck"]
    assert r.json() == {"deckId": deck_id}
    assert slide(env, "alice", deck_id, 2).status == "generating"  # pre-marked before the 202
    job = env.call(env.store.jobs.list_mine, env.ctx("alice"), type=REFINE_JOB).items[0]
    assert job.inputs["instruction"] == "fewer words" and job.inputs["number"] == 2
    assert "attachmentRef" in job.inputs  # the first image attachment, stored for the job
    env.run_jobs()
    after = slide(env, "alice", deck_id, 2)
    assert after.status == "done" and after.current == 2
    assert [(v.v, v.instruction) for v in after.versions] == [(1, None), (2, "fewer words")]
    call = env.images.calls[-1]
    assert call.kind == "edit" and call.images == 1 and 'Refinement requested by the user: "fewer words"' in call.prompt


refine.add("R0",
           Probe("refine slide 2 with an image attachment (and a PDF that is dropped)", with_deck(
               "/api/refine", lambda d: {"deckId": d, "number": 2, "instruction": "  fewer words  ", "attachments": [
                   {"type": "document", "media_type": "application/pdf", "data": "JVBERi0="},
                   {"type": "image", "media_type": "image/png", "data": PNG_B64}]}), _refined),
           Probe("a JSON 2.0 is an integer", with_deck("/api/refine", lambda d: {"deckId": d, "number": 2.0,
                                                                                "instruction": "x"})),
           Probe("a slide number past the deck is accepted (202)", with_deck(
               "/api/refine", lambda d: {"deckId": d, "number": 9, "instruction": "x"})),
           Probe("600 characters after trimming", with_deck(
               "/api/refine", lambda d: {"deckId": d, "number": 1, "instruction": " " + "a" * 600 + " "})))
refine.add("E400 deckId and a valid slide number are required",
           Probe("no deckId", post("/api/refine", {"number": 1, "instruction": "x"})),
           Probe("number as a string", post("/api/refine", {"deckId": UNKNOWN, "number": "2", "instruction": "x"})),
           Probe("number 0", post("/api/refine", {"deckId": UNKNOWN, "number": 0, "instruction": "x"})),
           Probe("number 1.5", post("/api/refine", {"deckId": UNKNOWN, "number": 1.5, "instruction": "x"})),
           Probe("number true", post("/api/refine", {"deckId": UNKNOWN, "number": True, "instruction": "x"})))
refine.add("E400 instruction is required",
           Probe("missing", post("/api/refine", {"deckId": UNKNOWN, "number": 1})),
           Probe("blank", post("/api/refine", {"deckId": UNKNOWN, "number": 1, "instruction": " \n\t "})))
refine.add("E400 instruction is too long (max 600 chars)",
           Probe("601 characters", post("/api/refine", {"deckId": UNKNOWN, "number": 1, "instruction": "a" * 601})),
           Probe("600 emoji are 1200 UTF-16 units", post("/api/refine", {"deckId": UNKNOWN, "number": 1,
                                                                         "instruction": "\U0001F600" * 300 + "a"})))
refine.add("E403 Not your deck", Probe("unknown uuid", post("/api/refine", {"deckId": UNKNOWN, "number": 1,
                                                                             "instruction": "x"})),
           Probe("someone else's deck", with_deck("/api/refine", lambda d: {"deckId": d, "number": 1,
                                                                            "instruction": "x"}, subject="bob")))
refine.add("E500 Internal error",
           Probe("a non-string instruction", post("/api/refine", {"deckId": UNKNOWN, "number": 1, "instruction": 5})),
           Probe("attachments not an array", with_deck("/api/refine", lambda d: {
               "deckId": d, "number": 1, "instruction": "x", "attachments": {"type": "image"}})),
           Probe("attachments false", with_deck("/api/refine", lambda d: {
               "deckId": d, "number": 1, "instruction": "x", "attachments": False})),
           Probe("non-uuid deckId", post("/api/refine", {"deckId": "deck-1", "number": 1, "instruction": "x"})),
           Probe("a JSON null body", post("/api/refine", raw=b"null")),
           Probe("malformed JSON", post("/api/refine", raw=b"{")))

# --------------------------------------------------------------------------------------------- revert
revert = RouteCases("/api/revert")
add_auth(revert, "POST", "/api/revert", json={"deckId": UNKNOWN, "number": 1, "version": 1})


def _reverted(env: DarwinEnv, r: Any) -> None:
    after = slide(env, "alice", memo(env)["deck"], 1)
    assert after.current == 1 and after.status == "done"


revert.add("R0", Probe("back to v1 of a refined slide", with_deck(
    "/api/revert", lambda d: {"deckId": d, "number": 1, "version": 1}, refined=True), _reverted),
    Probe("the current version again", with_deck("/api/revert", lambda d: {"deckId": d, "number": 2, "version": 1})))
revert.add("E400 deckId, number, version required",
           Probe("no version", post("/api/revert", {"deckId": UNKNOWN, "number": 1})),
           Probe("version 0", post("/api/revert", {"deckId": UNKNOWN, "number": 1, "version": 0})),
           Probe("number -1", post("/api/revert", {"deckId": UNKNOWN, "number": -1, "version": 1})),
           Probe("no deckId", post("/api/revert", {"number": 1, "version": 1})))
revert.add("E403 Not your deck", Probe("unknown uuid", post("/api/revert", {"deckId": UNKNOWN, "number": 1,
                                                                             "version": 1})),
           Probe("someone else's deck", with_deck("/api/revert", lambda d: {"deckId": d, "number": 1, "version": 1},
                                                  subject="bob")))


def _empty(env: DarwinEnv) -> Any:
    deck_id = empty_deck(env, "alice")
    return env.client.post("/api/revert", json={"deckId": deck_id, "number": 1, "version": 1},
                           headers=env.auth("alice"))


revert.add("E404 No such slide", Probe("a slide past the deck", with_deck(
    "/api/revert", lambda d: {"deckId": d, "number": 9, "version": 1})),
    Probe("a deck with no job state", _empty))
revert.add("E404 No such version", Probe("v5 of a one-version slide", with_deck(
    "/api/revert", lambda d: {"deckId": d, "number": 1, "version": 5})),
    Probe("v3 of a refined slide", with_deck("/api/revert", lambda d: {"deckId": d, "number": 1, "version": 3},
                                             refined=True)))
revert.add("E500 Internal error", Probe("non-uuid deckId", post("/api/revert", {"deckId": "x", "number": 1,
                                                                                 "version": 1})),
           Probe("a JSON null body", post("/api/revert", raw=b"null")))

# ----------------------------------------------------------------------------------- slide-transcript
transcript = RouteCases("/api/slide-transcript")
add_auth(transcript, "POST", "/api/slide-transcript", json={"deckId": UNKNOWN, "slideNumber": 1, "messages": []})

MESSAGES = [{"role": "user", "content": [{"type": "text", "text": "bolder"},
                                         {"type": "image", "media_type": "image/png", "data": "iVBORw0KGgo="}],
             "extra": "dropped"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "thanks"}]


def _stored(env: DarwinEnv, r: Any) -> None:
    row = env.call(env.store.transcripts.get_slide_edit, env.ctx("alice"), memo(env)["deck"], 2)
    assert row.refine_count == 2  # the user messages
    assert row.messages[0] == {"role": "user", "content": [
        {"type": "text", "text": "bolder"}, {"type": "image", "data": "[attachment]", "media_type": "image/png"}]}
    assert row.messages[1:] == [{"role": "assistant", "content": "done"}, {"role": "user", "content": "thanks"}]


transcript.add("R0",
               Probe("store a slide's edit thread", with_deck(
                   "/api/slide-transcript", lambda d: {"deckId": d, "slideNumber": 2, "messages": MESSAGES}), _stored),
               Probe("a storage failure is swallowed", lambda env: (broken(env, "transcripts", "upsert_slide_edit"),
                     with_deck("/api/slide-transcript",
                               lambda d: {"deckId": d, "slideNumber": 1, "messages": []})(env))[1]),
               Probe("a non-integer slide number: ok, not stored", with_deck(
                   "/api/slide-transcript", lambda d: {"deckId": d, "slideNumber": 1.5, "messages": []})))
transcript.add("E405 Method not allowed",
               Probe("GET without a token: 405, not 401", lambda env: env.client.get("/api/slide-transcript")),
               Probe("PUT with a token", lambda env: env.client.put("/api/slide-transcript", json={},
                                                                    headers=env.auth("alice"))))
transcript.add("E400 Missing deckId, slideNumber, or messages",
               Probe("no slideNumber", post("/api/slide-transcript", {"deckId": UNKNOWN, "messages": []})),
               Probe("slideNumber as a string", post("/api/slide-transcript", {"deckId": UNKNOWN, "slideNumber": "1",
                                                                               "messages": []})),
               Probe("messages not an array", post("/api/slide-transcript", {"deckId": UNKNOWN, "slideNumber": 1,
                                                                             "messages": {}})),
               Probe("a JSON null body", post("/api/slide-transcript", raw=b"null")))
transcript.add("E403 Not your deck", Probe("unknown uuid", post("/api/slide-transcript", {
    "deckId": UNKNOWN, "slideNumber": 1, "messages": []})),
    Probe("someone else's deck", with_deck("/api/slide-transcript",
                                           lambda d: {"deckId": d, "slideNumber": 1, "messages": []}, subject="bob")))
transcript.add("E500 Internal error",
               Probe("malformed JSON", post("/api/slide-transcript", raw=b"{")),
               Probe("a null message", with_deck("/api/slide-transcript",
                                                 lambda d: {"deckId": d, "slideNumber": 1, "messages": [None]})),
               Probe("non-uuid deckId", post("/api/slide-transcript", {"deckId": "x", "slideNumber": 1,
                                                                       "messages": []})))

ROUTES = [refine, revert, transcript]
