"""Contract cases: /api/quick-generate, /api/quick-status, /api/quick-image (data/api-quick-*.json)."""

from __future__ import annotations

import base64
from typing import Any

from app.core.darwin.image_gen import ImageGenError
from app.core.darwin.quick import QUICK_JOB
from tests.contract.cases.common import add_auth, broken
from tests.contract.harness import Probe, RouteCases
from tests.fakes.darwin import DarwinEnv
from tests.fakes.darwin_decks import UNKNOWN, fill_image_cap, memo, storyline_reply
from tests.fakes.image_gen import tiny_png

PNG_B64 = base64.b64encode(tiny_png()).decode()


def post(body: Any = None, *, raw: bytes | None = None, subject: str = "alice", method: str = "POST") -> Any:
    def run(env: DarwinEnv) -> Any:
        if raw is not None:
            return env.client.request(method, "/api/quick-generate", content=raw, headers=env.auth(subject))
        return env.client.request(method, "/api/quick-generate", json=body, headers=env.auth(subject))
    return run


def submit(env: DarwinEnv, *, slides: int = 2, run: bool = True, subject: str = "alice", **body: Any) -> str:
    env.user(subject)
    if run:
        env.model.steps.append(storyline_reply(slides))
    r = env.client.post("/api/quick-generate", json={"topic": "Q3", **body}, headers=env.auth(subject))
    assert r.status_code == 202, r.text
    if run:
        env.run_jobs()
    return str(r.json()["jobId"])


# ------------------------------------------------------------------------------------- quick-generate
generate = RouteCases("/api/quick-generate")
add_auth(generate, "POST", "/api/quick-generate", json={"topic": "x"})


def _queued(env: DarwinEnv, r: Any) -> None:
    record = env.job("alice", r.json()["jobId"])
    assert record.type == QUICK_JOB and record.status == "queued"
    assert record.inputs["inputs"] == {"topic": "GTM", "numSlides": 3, "unknown": 1}  # layoutImageB64 stripped
    assert isinstance(record.inputs["layoutRef"], str)


generate.add("R0", Probe("with an inline layout PNG", post({"topic": "GTM", "numSlides": 3, "unknown": 1,
                                                            "layoutImageB64": PNG_B64}), _queued),
             Probe("topic only (no numSlides, no upper bound)", post({"topic": "GTM"})),
             Probe("numSlides 500", post({"topic": "GTM", "numSlides": 500})),
             Probe("numSlides 'abc' passes ('abc' < 1 is false)", post({"topic": "GTM", "numSlides": "abc"})))
generate.add("E400 Topic is required", Probe("no topic", post({"numSlides": 3})),
             Probe("empty topic", post({"topic": ""})), Probe("an array body", post([1])))
generate.add("E400 Slides must be at least 1", Probe("numSlides 0", post({"topic": "x", "numSlides": 0})),
             Probe("numSlides '0'", post({"topic": "x", "numSlides": "0"})),
             Probe("numSlides null", post({"topic": "x", "numSlides": None})))
generate.add("E400 layoutImageB64 must be a base64 string",
             Probe("a number", post({"topic": "x", "layoutImageB64": 5})),
             Probe("empty", post({"topic": "x", "layoutImageB64": ""})),
             Probe("null counts as present", post({"topic": "x", "layoutImageB64": None})))
generate.add("E400 Layout image must be a PNG up to 4MB",
             Probe("decodes to nothing", post({"topic": "x", "layoutImageB64": "!!!!"})),
             Probe("over 4 MiB", post({"topic": "x", "layoutImageB64": base64.b64encode(
                 b"\x89PNG" + b"\x00" * (4 * 1024 * 1024)).decode()})))
generate.add("E400 Layout image must be a PNG", Probe("a JPEG", post({"topic": "x", "layoutImageB64": base64.b64encode(
    b"\xff\xd8\xff\xe0 jpeg").decode()})))
generate.add("E500 Internal error", Probe("malformed JSON", post(raw=b"{")), Probe("a JSON null body", post(raw=b"null")),
             Probe("GET has no body", post(method="GET")))

# ------------------------------------------------------------------------------------------- quick-status
status = RouteCases("/api/quick-status")
add_auth(status, "GET", f"/api/quick-status?jobId={UNKNOWN}")


def poll(job_id: str, subject: str = "alice") -> Any:
    return lambda env: env.client.get(f"/api/quick-status?jobId={job_id}", headers=env.auth(subject))


def _exactly(expected: dict[str, Any]) -> Any:
    def check(_env: DarwinEnv, r: Any) -> None:
        assert r.json() == expected
    return check


status.add("R0", Probe("an unknown id", poll(UNKNOWN), _exactly({"status": "pending"})),
           Probe("someone else's unknown id", poll("not-an-id", "mallory"), _exactly({"status": "pending"})))
status.add("R1", Probe("queued", lambda env: poll(submit(env, run=False))(env), _exactly({"status": "pending"})))


def _failed(env: DarwinEnv) -> Any:
    env.images.fail_with = ImageGenError(400, "Your request was rejected by the safety system.")
    return poll(submit(env))(env)


def _capped(env: DarwinEnv) -> Any:
    fill_image_cap(env)
    return poll(submit(env))(env)


def _storyline_failed(env: DarwinEnv) -> Any:
    env.user("alice")
    env.model.steps.append({"presentationTitle": "", "slides": []})
    job = env.client.post("/api/quick-generate", json={"topic": "x"}, headers=env.auth("alice")).json()["jobId"]
    env.run_jobs()
    return poll(job)(env)


status.add("R2", Probe("an image failure fails the job with the provider's message", _failed,
                       _exactly({"status": "error", "error": "Your request was rejected by the safety system."})),
           Probe("the global image cap applies (C12)", _capped,
                 _exactly({"status": "error", "error": "Free capacity reached, try again tomorrow."})),
           Probe("an incomplete storyline", _storyline_failed))


def _done(env: DarwinEnv) -> Any:
    job_id = submit(env, slides=3)
    memo(env)["job"] = job_id
    return poll(job_id)(env)


def _done_body(env: DarwinEnv, r: Any) -> None:
    job_id = memo(env)["job"]
    assert r.json() == {"status": "done", "warnings": [], "slides": [
        {"slideNumber": n, "url": f"/api/quick-image?jobId={job_id}&slide={n}"} for n in (1, 2, 3)]}
    assert len(env.images.calls) == 3


status.add("R3", Probe("done", _done, _done_body))
status.add("E400 jobId is required", Probe("no jobId", lambda env: env.client.get("/api/quick-status",
                                                                                   headers=env.auth("alice"))),
           Probe("empty jobId", poll("")))
status.add("E403 Not your job", Probe("bob polls alice's job", lambda env: poll(submit(env, run=False), "bob")(env)))
status.add("E500 Internal error", Probe("storage down", lambda env: (broken(env, "jobs", "get"),
                                                                     poll(UNKNOWN)(env))[1]))

# -------------------------------------------------------------------------------------------- quick-image
image = RouteCases("/api/quick-image")
add_auth(image, "GET", f"/api/quick-image?jobId={UNKNOWN}&slide=1")


def fetch(job_id: str, slide: str = "1", subject: str = "alice") -> Any:
    return lambda env: env.client.get(f"/api/quick-image?jobId={job_id}&slide={slide}", headers=env.auth(subject))


def _png(_env: DarwinEnv, r: Any) -> None:
    assert r.content.startswith(b"\x89PNG")


image.add("R0", Probe("slide 2 of a done job", lambda env: fetch(submit(env), "2")(env), _png),
          Probe("slide=1.0 is slide 1", lambda env: fetch(submit(env), "1.0")(env), _png))
image.add("E400 Bad params", Probe("no jobId", lambda env: env.client.get("/api/quick-image?slide=1",
                                                                          headers=env.auth("alice"))),
          Probe("slide=0", fetch(UNKNOWN, "0")), Probe("slide missing", lambda env: env.client.get(
              f"/api/quick-image?jobId={UNKNOWN}", headers=env.auth("alice"))))
image.add("E404 Job not found", Probe("unknown id", fetch(UNKNOWN)),
          Probe("a job of another kind", lambda env: fetch(
              env.client.post("/api/storyline", json={"topic": "x"}, headers=env.auth("alice")).json()["jobId"])(env)))
image.add("E403 Not your job", Probe("bob fetches alice's image", lambda env: fetch(submit(env), "1", "bob")(env)))
image.add("E404 Images not ready", Probe("queued", lambda env: fetch(submit(env, run=False))(env)),
          Probe("failed", lambda env: (setattr(env.images, "fail_with", ImageGenError(500, "boom")),
                                       fetch(submit(env))(env))[1]))
image.add("E404 Image not found", Probe("a slide past the deck", lambda env: fetch(submit(env), "9")(env)))
image.add("E500 Internal error", Probe("storage down", lambda env: (broken(env, "jobs", "get"),
                                                                   fetch(UNKNOWN)(env))[1]))

ROUTES = [generate, status, image]
