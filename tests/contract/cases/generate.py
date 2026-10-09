"""Contract cases: /api/generate, /api/retry, /api/status (data/api-{generate,retry,status}.json)."""

from __future__ import annotations

from typing import Any

from app.core.darwin.caps import CAP_REACHED
from app.core.darwin.generate import GENERATE_JOB
from app.core.darwin.image_gen import ImageGenError
from tests.contract.cases.common import add_auth, broken
from tests.contract.harness import Probe, RouteCases
from tests.fakes.darwin import DarwinEnv
from tests.fakes.darwin_decks import (
    UNKNOWN,
    empty_deck,
    fill_image_cap,
    generated_deck,
    memo,
    slide,
    storyline_payload,
)


def post(path: str, body: Any = None, *, raw: bytes | None = None, subject: str = "alice",
         method: str = "POST") -> Any:
    def run(env: DarwinEnv) -> Any:
        if raw is not None:
            return env.client.request(method, path, content=raw, headers=env.auth(subject))
        return env.client.request(method, path, json=body, headers=env.auth(subject))
    return run


def queued_jobs(env: DarwinEnv, subject: str = "alice") -> list[Any]:
    page = env.call(env.store.jobs.list_mine, env.ctx(subject), type=GENERATE_JOB)
    return list(page.items)


# ------------------------------------------------------------------------------------------- generate
generate = RouteCases("/api/generate")
add_auth(generate, "POST", "/api/generate", json=storyline_payload())


def _generated(env: DarwinEnv, r: Any) -> None:
    body = r.json()
    jobs = queued_jobs(env)
    assert len(jobs) == 1 and jobs[0].inputs["deckId"] == body["deckId"]
    slides = jobs[0].inputs["slides"]
    assert [s["number"] for s in slides] == [1, 2, 3]
    assert all("Non-negotiable rules: " in s["prompt"] for s in slides)
    assert "IGNORED CLIENT PROMPT" not in str(jobs[0].inputs)  # the client's prompt is rebuilt server-side
    deck = env.call(env.store.decks.get, env.ctx("alice"), body["deckId"])
    assert deck.status == "generating" and deck.title == "Q3 2026 Financial Results"
    assert [s.status for s in env.call(env.store.slides.list_for_deck, env.ctx("alice"), deck.id)] == ["idle"] * 3


def _client_prompt(env: DarwinEnv) -> Any:
    payload = storyline_payload(3)
    payload["slides"][1]["prompt"] = "IGNORED CLIENT PROMPT"
    payload["warnings"] = ["from storyline-status: ignored"]
    return env.client.post("/api/generate", json=payload, headers=env.auth("alice"))


def _coerced(_env: DarwinEnv, r: Any) -> None:
    assert len(r.json()["warnings"]) == 1 and "Slide 2" in r.json()["warnings"][0]


def _spider(env: DarwinEnv) -> Any:
    payload = storyline_payload(2)
    payload["slides"][1]["framework"] = "spider chart"
    return env.client.post("/api/generate", json=payload, headers=env.auth("alice"))


generate.add("R0", Probe("a valid storyline: 202 {deckId, warnings: []}", _client_prompt, _generated),
             Probe("an infeasible framework is coerced with a warning", _spider, _coerced),
             Probe("PUT acts as POST", post("/api/generate", storyline_payload(), method="PUT")))


def _invalid(mutate: Any) -> Any:
    def run(env: DarwinEnv) -> Any:
        payload = storyline_payload(2)
        mutate(payload)
        return env.client.post("/api/generate", json=payload, headers=env.auth("alice"))
    return run


generate.add("E500 Internal error",
             Probe("two bullets on a content slide (the openapi example)",
                   _invalid(lambda p: p["slides"][1].update(bullets=["a", "b"]))),
             Probe("an unknown slide type", _invalid(lambda p: p["slides"][1].update(type="spider"))),
             Probe("no slides", _invalid(lambda p: p.update(slides=[]))),
             Probe("an empty title", _invalid(lambda p: p.update(presentationTitle=""))),
             Probe("a slide number of 0", _invalid(lambda p: p["slides"][0].update(number=0))),
             Probe("duplicate slide numbers", _invalid(lambda p: p["slides"][1].update(number=1))),
             Probe("malformed JSON", post("/api/generate", raw=b"{not json")),
             Probe("a JSON null body", post("/api/generate", raw=b"null")),
             Probe("GET has no body", post("/api/generate", method="GET")))

# --------------------------------------------------------------------------------------------- retry
retry = RouteCases("/api/retry")
add_auth(retry, "POST", "/api/retry", json={"deckId": UNKNOWN, "retryOnly": [1]})


def _retry(env: DarwinEnv) -> Any:
    deck_id = generated_deck(env, "alice")
    memo(env)["deck"] = deck_id
    return env.client.post("/api/retry", json={"deckId": deck_id, "retryOnly": [2]}, headers=env.auth("alice"))


def _retried(env: DarwinEnv, r: Any) -> None:
    deck_id = memo(env)["deck"]
    assert r.json() == {"deckId": deck_id}
    job = next(j for j in queued_jobs(env) if "retryOnly" in j.inputs)  # not the generate job
    assert job.inputs["retryOnly"] == [2] and [s["number"] for s in job.inputs["slides"]] == [1, 2]
    assert [s["type"] for s in job.inputs["slides"]] == ["title", "framework"]  # re-attached from the storyline
    assert env.call(env.store.decks.get, env.ctx("alice"), deck_id).status == "generating"
    env.run_jobs()
    assert len(slide(env, "alice", deck_id, 2).versions) == 2  # a new version, current
    assert len(slide(env, "alice", deck_id, 1).versions) == 1  # untouched


def _retry_nothing(env: DarwinEnv) -> Any:
    deck_id = generated_deck(env, "alice")
    return env.client.post("/api/retry", json={"deckId": deck_id, "retryOnly": []}, headers=env.auth("alice"))


retry.add("R0", Probe("retry one slide", _retry, _retried),
          Probe("retryOnly [] is accepted (regenerates nothing)", _retry_nothing))
retry.add("E400 deckId and retryOnly required",
          Probe("no deckId", post("/api/retry", {"retryOnly": [1]})),
          Probe("retryOnly not an array", post("/api/retry", {"deckId": UNKNOWN, "retryOnly": 1})),
          Probe("retryOnly missing", post("/api/retry", {"deckId": UNKNOWN})),
          Probe("an array body", post("/api/retry", [1, 2])))
retry.add("E403 Not your deck", Probe("unknown uuid", post("/api/retry", {"deckId": UNKNOWN, "retryOnly": [1]})),
          Probe("someone else's deck", lambda env: env.client.post(
              "/api/retry", json={"deckId": env.seed_deck("alice"), "retryOnly": [1]}, headers=env.auth("bob"))))
retry.add("E500 Internal error", Probe("non-uuid deckId", post("/api/retry", {"deckId": "deck-1", "retryOnly": [1]})),
          Probe("a JSON null body", post("/api/retry", raw=b"null")),
          Probe("malformed JSON", post("/api/retry", raw=b"{")))

# -------------------------------------------------------------------------------------------- status
status = RouteCases("/api/status")
add_auth(status, "GET", f"/api/status?deckId={UNKNOWN}")


def poll(deck_id: str, subject: str = "alice") -> Any:
    return lambda env: env.client.get(f"/api/status?deckId={deck_id}", headers=env.auth(subject))


def _before_state(env: DarwinEnv) -> Any:
    deck_id = empty_deck(env, "alice")
    memo(env)["deck"] = deck_id
    return poll(deck_id)(env)


def _starting(env: DarwinEnv, r: Any) -> None:
    assert r.json() == {"deckId": memo(env)["deck"], "slides": {}, "progress": "Starting…", "done": False}


status.add("R0", Probe("an owned deck with no job state yet", _before_state, _starting))


def _done_deck(env: DarwinEnv) -> Any:
    deck_id = generated_deck(env, "alice", slides=2)
    memo(env)["deck"] = deck_id
    return poll(deck_id)(env)


def _done_body(env: DarwinEnv, r: Any) -> None:
    deck_id = memo(env)["deck"]
    assert r.json() == {
        "deckId": deck_id,
        "slides": {str(n): {"status": "done", "url": f"/api/image?deckId={deck_id}&slide={n}&v=1", "current": 1,
                            "versions": [{"v": 1, "instruction": None}]} for n in (1, 2)},
        "progress": "Generated 2 of 2 slides…", "done": True, "masterApplied": False, "masterSkipReason": "no-brand"}


def _mixed(env: DarwinEnv) -> Any:
    deck_id = generated_deck(env, "alice", slides=3, run=False)
    env.images.fail_with = ImageGenError(400, "Your request was rejected by the safety system.")
    env.run_jobs()
    memo(env)["deck"] = deck_id
    return poll(deck_id)(env)


def _mixed_body(env: DarwinEnv, r: Any) -> None:
    body = r.json()
    assert body["done"] is True and body["progress"] == "Generated 0 of 3 slides…"
    first = body["slides"]["1"]
    assert first == {"status": "error", "error": "Your request was rejected by the safety system.", "current": 1,
                     "versions": []}  # no url (omitted, never null)


def _capped(env: DarwinEnv) -> Any:
    deck_id = generated_deck(env, "alice", slides=2, run=False)
    fill_image_cap(env)
    env.run_jobs()
    return poll(deck_id)(env)


def _capped_body(_env: DarwinEnv, r: Any) -> None:
    assert {s["error"] for s in r.json()["slides"].values()} == {CAP_REACHED}


def _generating(env: DarwinEnv) -> Any:
    deck_id = generated_deck(env, "alice", slides=2, run=False)
    return poll(deck_id)(env)


def _generating_body(_env: DarwinEnv, r: Any) -> None:
    body = r.json()
    assert body["done"] is False and body["slides"]["1"] == {"status": "idle", "current": 1, "versions": []}
    assert body["masterApplied"] is False and "masterSkipReason" not in body  # not decided yet: omitted


status.add("R1", Probe("every slide done", _done_deck, _done_body),
           Probe("slides failed with the provider's message", _mixed, _mixed_body),
           Probe("the global image cap reached", _capped, _capped_body),
           Probe("queued, not yet run", _generating, _generating_body))
status.add("E400 deckId is required", Probe("no deckId", lambda env: env.client.get(
    "/api/status", headers=env.auth("alice"))), Probe("empty deckId", poll("")))
status.add("E403 Not your deck", Probe("unknown uuid", poll(UNKNOWN)),
           Probe("someone else's deck", lambda env: poll(env.seed_deck("alice"), "bob")(env)))
status.add("E500 Internal error", Probe("non-uuid deckId", poll("deck-1")),
           Probe("storage down", lambda env: (broken(env, "slides", "list_for_deck"),
                                              poll(env.seed_deck("alice"))(env))[1]))

ROUTES = [generate, retry, status]
