"""Contract cases: /api/storyline, /api/storyline-status, /api/intake (data/api-{storyline,storyline-status,intake}.json)."""

from __future__ import annotations

from typing import Any

from app.core.storyline.ports import ModelUnavailable
from tests.contract.cases.common import add_auth, broken
from tests.contract.harness import Probe, RouteCases
from tests.fakes.darwin import DarwinEnv
from tests.fakes.storyline_model import reply

STORYLINE = {"presentationTitle": "Q3 2026 Financial Results", "slides": [
    {"number": 1, "title": "Q3 beat plan by 12%", "type": "title", "framework": "title slide",
     "description": "Opening", "bullets": []}]}


def post_storyline(body: Any, *, raw: bytes | None = None) -> Any:
    def run(env: DarwinEnv) -> Any:
        headers = env.auth("alice")
        if raw is not None:
            return env.client.post("/api/storyline", content=raw, headers=headers)
        return env.client.post("/api/storyline", json=body, headers=headers)
    return run


# ---------------------------------------------------------------------------------------- storyline
storyline = RouteCases("/api/storyline")
add_auth(storyline, "POST", "/api/storyline", json={"topic": "t"})


def _queued(env: DarwinEnv, response: Any) -> None:
    record = env.job("alice", response.json()["jobId"])
    assert record.type == "storyline" and record.status == "queued"
    assert record.inputs == {"topic": "GTM", "numSlides": 3, "unknownField": 1}  # verbatim, unknown fields kept


storyline.add("R0",
              Probe("a topic and numSlides; unknown fields kept for result.inputs",
                    post_storyline({"topic": "GTM", "numSlides": 3, "unknownField": 1}), _queued),
              Probe("numSlides missing", post_storyline({"topic": "GTM"})),
              Probe("numSlides large (no cap)", post_storyline({"topic": "GTM", "numSlides": 500})),
              Probe("numSlides non-numeric string ('abc' < 1 is false in JS)", post_storyline({"topic": "x", "numSlides": "abc"})),
              Probe("any other method with a JSON body acts as POST",
                    lambda env: env.client.put("/api/storyline", json={"topic": "x"}, headers=env.auth("alice"))))
storyline.add("E400 Topic is required",
              Probe("empty object", post_storyline({})),
              Probe("empty topic", post_storyline({"topic": ""})),
              Probe("JSON null", post_storyline(None, raw=b"null")),
              Probe("JSON array", post_storyline([1, 2])))
storyline.add("E400 Slides must be at least 1",
              *(Probe(f"numSlides={v!r}", post_storyline({"topic": "x", "numSlides": v}))
                for v in (0, -3, None, False, "", "0")))
storyline.add("E500 Internal error",
              Probe("malformed JSON", post_storyline(None, raw=b"{not json")),
              Probe("empty body", post_storyline(None, raw=b"")),
              Probe("bodyless GET", lambda env: env.client.get("/api/storyline", headers=env.auth("alice"))),
              Probe("the job store is unreachable",
                    lambda env: (broken(env, "jobs", "create"), post_storyline({"topic": "x"})(env))[1]))

# --------------------------------------------------------------------------------- storyline-status
status = RouteCases("/api/storyline-status")
add_auth(status, "GET", "/api/storyline-status?jobId=x")


def submitted(env: DarwinEnv, owner: str = "alice") -> str:
    r = env.client.post("/api/storyline", json={"topic": "Q3", "numSlides": 3}, headers=env.auth(owner))
    assert r.status_code == 202
    return str(r.json()["jobId"])


def poll(job_id: str, who: str = "alice") -> Any:
    return lambda env: env.client.get(f"/api/storyline-status?jobId={job_id}", headers=env.auth(who))


def poll_after(prepare: Any, who: str = "alice") -> Any:
    def run(env: DarwinEnv) -> Any:
        job_id = prepare(env)
        return env.client.get(f"/api/storyline-status?jobId={job_id}", headers=env.auth(who))
    return run


def _exactly(expected: dict[str, Any]) -> Any:
    def check(_env: DarwinEnv, response: Any) -> None:
        assert response.json() == expected
    return check


status.add("R0",
           Probe("unknown uuid", poll("3fa85f64-5717-4562-b3fc-2c963f66afa6"), _exactly({"status": "pending"})),
           Probe("not even an id", poll("..%2F..%2Fetc"), _exactly({"status": "pending"})),
           Probe("someone ELSE's unknown id: still pending, before any ownership check",
                 poll("00000000-0000-0000-0000-000000000000", "mallory"), _exactly({"status": "pending"})))
status.add("R1", Probe("queued, not yet run", poll_after(submitted), _exactly({"status": "pending"})))


def _done(env: DarwinEnv) -> str:
    env.model.steps.append(STORYLINE)
    job_id = submitted(env)
    assert env.run_jobs() == 1
    return job_id


def _done_check(_env: DarwinEnv, response: Any) -> None:
    body = response.json()
    assert "error" not in body  # omitted, never null
    result = body["result"]
    assert result["inputs"] == {"topic": "Q3", "numSlides": 3}
    assert result["presentationTitle"] == "Q3 2026 Financial Results"
    assert result["slides"][0]["title"] == "Q3 beat plan by 12%"
    # Each slide carries Darwin's server-assembled image prompt (app/core/darwin/prompt.py, the prompter).
    for slide in result["slides"]:
        assert isinstance(slide["prompt"], str) and "Non-negotiable rules: " in slide["prompt"]
    assert 'Render ONLY the presentation title, exactly: "Q3 beat plan by 12%"' in result["slides"][0]["prompt"]


status.add("R2", Probe("done", poll_after(_done), _done_check))


def _errored(env: DarwinEnv) -> str:
    env.model.steps.extend([ModelUnavailable("overloaded"), ModelUnavailable("overloaded")])
    job_id = submitted(env)
    env.run_jobs()
    return job_id


def _error_check(_env: DarwinEnv, response: Any) -> None:
    body = response.json()
    assert "result" not in body and isinstance(body["error"], str) and body["error"]


def _incomplete(env: DarwinEnv) -> str:
    env.model.steps.append({"presentationTitle": "", "slides": []})
    job_id = submitted(env)
    env.run_jobs()
    return job_id


status.add("R3",
           Probe("the model stayed unavailable", poll_after(_errored), _error_check),
           Probe("an incomplete storyline: Darwin's message", poll_after(_incomplete), _exactly({
               "status": "error",
               "error": "Claude returned an incomplete storyline — please try again or reduce the number of slides"})))
status.add("E400 jobId is required",
           Probe("no jobId", lambda env: env.client.get("/api/storyline-status", headers=env.auth("alice"))),
           Probe("empty jobId", lambda env: env.client.get("/api/storyline-status?jobId=", headers=env.auth("alice"))))
status.add("E403 Not your job",
           Probe("bob polls alice's job", poll_after(submitted, "bob")),
           Probe("bob polls alice's finished job", poll_after(_done, "bob")))
status.add("E500 Internal error",
           Probe("the job store is unreachable",
                 lambda env: (broken(env, "jobs", "get"), poll("3fa85f64-5717-4562-b3fc-2c963f66afa6")(env))[1]))

# ------------------------------------------------------------------------------------------- intake
intake = RouteCases("/api/intake")
add_auth(intake, "POST", "/api/intake", json={"messages": [{"role": "user", "content": "hi"}]})


def post_intake(body: Any = None, *, raw: bytes | None = None, steps: list[Any] | None = None) -> Any:
    def run(env: DarwinEnv) -> Any:
        env.model.steps.extend(steps or [])
        if raw is not None:
            return env.client.post("/api/intake", content=raw, headers=env.auth("alice"))
        return env.client.post("/api/intake", json=body, headers=env.auth("alice"))
    return run


USER = {"role": "user", "content": "A GTM deck for the board"}


def _one_call(expected: dict[str, Any]) -> Any:
    def check(env: DarwinEnv, response: Any) -> None:
        assert response.json() == expected
        assert env.model.calls == 1  # ONE model call per turn (C14)
    return check


def _no_call(expected: dict[str, Any]) -> Any:
    def check(env: DarwinEnv, response: Any) -> None:
        assert response.json() == expected
        assert env.model.calls == 0
    return check


intake.add("R0",
           Probe("a turn: only the changed brief fields come back",
                 post_intake({"messages": [USER], "brief": {"topic": "GTM", "bogus": 1}},
                             steps=[{"message": "Who is the audience?", "brief": {"topic": "GTM"}}]),
                 _one_call({"message": "Who is the audience?", "brief": {"topic": "GTM"}})),
           Probe("an image attachment block", post_intake(
               {"messages": [{"role": "user", "content": [{"type": "text", "text": "this"},
                                                          {"type": "image", "data": "aGk=", "media_type": "image/png"}]}]},
               steps=[{"message": "Nice.", "brief": {}}]), _one_call({"message": "Nice.", "brief": {}})))
WRAP_UP = ("We've covered a lot — let's draft with what we have. Hit “Draft storyline” whenever you're ready.")
intake.add("R1", Probe("12 assistant turns: wrap-up, no model call", post_intake(
    {"messages": [m for _ in range(12) for m in (USER, {"role": "assistant", "content": "ok"})]}),
    _no_call({"message": WRAP_UP, "brief": {}})))
intake.add("R2", Probe("no user message: the opener, no model call", post_intake(
    {"messages": [{"role": "assistant", "content": "Hi!"}]}),
    _no_call({"message": "Tell me about the deck you need — what's it for?", "brief": {}})))
intake.add("R3", Probe("an empty message: the filler, never a second call", post_intake(
    {"messages": [USER]}, steps=[reply({"message": "", "brief": {"ready": True}})]),
    _one_call({"message": "Noted — anything else before we draft?", "brief": {"ready": True}})))
intake.add("E400 messages array required",
           Probe("malformed JSON (caught here: 400, not 500)", post_intake(raw=b"{oops")),
           Probe("empty body", post_intake(raw=b"")),
           Probe("JSON null", post_intake(raw=b"null")),
           Probe("{}", post_intake({})),
           Probe("messages: []", post_intake({"messages": []})),
           Probe("messages not an array", post_intake({"messages": "hi"})),
           Probe("bodyless GET", lambda env: env.client.get("/api/intake", headers=env.auth("alice"))))
intake.add("E400 malformed message",
           Probe("role system", post_intake({"messages": [{"role": "system", "content": "x"}]})),
           Probe("content a number", post_intake({"messages": [{"role": "user", "content": 42}]})),
           Probe("image with a bad media type", post_intake({"messages": [{"role": "user", "content": [
               {"type": "image", "data": "x", "media_type": "image/tiff"}]}]})),
           Probe("unknown block type", post_intake({"messages": [{"role": "user", "content": [{"type": "audio"}]}]})))
def _not_called(env: DarwinEnv, _response: Any) -> None:
    assert env.model.calls == 0, "an oversize transcript must never reach the paid model"


intake.add("E400 Conversation too long — draft with what you have",
           Probe("30001 text characters", post_intake({"messages": [{"role": "user", "content": "x" * 30001}]}),
                 _not_called))
intake.add("E500 Internal error",
           Probe("the model call failed", post_intake({"messages": [USER]}, steps=[ModelUnavailable("boom")])))

ROUTES = [storyline, status, intake]
