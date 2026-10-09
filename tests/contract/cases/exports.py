"""Contract cases: the PPTX export routes and /api/image-to-slide (data/api-pptx-*.json, api-image-to-slide.json)."""

from __future__ import annotations

from typing import Any

from app.core.darwin.exports import SLIDE_JOB
from tests.contract.cases.common import add_auth, broken
from tests.contract.harness import Probe, RouteCases
from tests.fakes.darwin import DarwinEnv
from tests.fakes.image_gen import tiny_png

NO_UPSTREAM = ("Slide Studio's /v1/jobs is an internal job now (plan §2.1): there is no upstream HTTP answer at "
               "submit time. A design or export failure surfaces later as status \"failed\" on the status route.")

UNKNOWN = "3fa85f64-5717-4562-b3fc-2c963f66afa6"


def post(path: str, body: Any = None, *, raw: bytes | None = None, who: str = "alice") -> Any:
    def run(env: DarwinEnv) -> Any:
        if raw is not None:
            return env.client.post(path, content=raw, headers=env.auth(who))
        return env.client.post(path, json=body, headers=env.auth(who))
    return run


def with_deck(build: Any, *, refined: bool = False, slides: int = 2) -> Any:
    """A probe that seeds alice's deck first; `build(env, deck_id)` sends the request."""
    def run(env: DarwinEnv) -> Any:
        return build(env, env.seed_deck("alice", slides=slides, refined=refined))
    return run


def submit_slide(env: DarwinEnv, deck_id: str, who: str = "alice", **extra: Any) -> Any:
    return env.client.post("/api/pptx-submit", json={"deckId": deck_id, "slide": "1", **extra}, headers=env.auth(who))


def finished_slide_job(env: DarwinEnv, *, run: bool = True) -> str:
    deck_id = env.seed_deck("alice", slides=1)
    r = submit_slide(env, deck_id)
    assert r.status_code == 200, r.text
    if run:
        assert env.run_jobs() >= 1
    return str(r.json()["jobId"])


def finished_deck_job(env: DarwinEnv, *, run: bool = True, job_ids: list[str] | None = None) -> str:
    deck_id = env.seed_deck("alice", slides=1)
    ids = job_ids if job_ids is not None else [finished_slide_job(env)]
    r = env.client.post("/api/pptx-deck-submit", json={"deckId": deck_id, "jobIds": ids, "presentationTitle": "Q3"},
                        headers=env.auth("alice"))
    assert r.status_code == 200, r.text
    if run:
        env.run_jobs()
    return str(r.json()["deckJobId"])


def get(path: str, who: str = "alice") -> Any:
    return lambda env: env.client.get(path, headers=env.auth(who))


def get_after(prepare: Any, template: str, who: str = "alice") -> Any:
    def run(env: DarwinEnv) -> Any:
        return env.client.get(template.format(prepare(env)), headers=env.auth(who))
    return run


def _exactly(expected: Any) -> Any:
    def check(_env: DarwinEnv, response: Any) -> None:
        assert response.json() == expected
    return check


def _is_pptx(_env: DarwinEnv, response: Any) -> None:
    assert response.content[:2] == b"PK" and len(response.content) > 100
    assert "cache-control" not in response.headers


# -------------------------------------------------------------------------------------- pptx-submit
submit = RouteCases("/api/pptx-submit")
add_auth(submit, "POST", "/api/pptx-submit", json={"deckId": UNKNOWN, "slide": "1"})


def _job_queued(env: DarwinEnv, response: Any) -> None:
    record = env.job("alice", response.json()["jobId"])
    assert record.type == SLIDE_JOB and record.inputs["imageRef"] and record.inputs["slide"] == 1


submit.add("R0",
           Probe("slide as a string; 200 not 202", with_deck(lambda env, d: submit_slide(env, d)), _job_queued),
           Probe("slide as a number, '1abc' parses to 1",
                 with_deck(lambda env, d: env.client.post("/api/pptx-submit", json={"deckId": d, "slide": "1abc"},
                                                          headers=env.auth("alice")))),
           Probe("a refined version", with_deck(lambda env, d: submit_slide(env, d, version="2"), refined=True)),
           Probe("legacy 'variant' and the Connector's 'heading' are ignored",
                 with_deck(lambda env, d: submit_slide(env, d, variant="A", heading="ignored",
                                                       applyBrandLayout=False))))
submit.na("R1", NO_UPSTREAM + " The job id is always present, so `{}` cannot happen.")
submit.add("E405 Method not allowed",
           Probe("GET without a token: 405 BEFORE auth", lambda env: env.client.get("/api/pptx-submit")),
           Probe("PUT with a token", lambda env: env.client.put("/api/pptx-submit", json={}, headers=env.auth("alice"))))
submit.add("E400 Missing params",
           *(Probe(f"body {b!r}", post("/api/pptx-submit", b)) for b in (
               {}, {"deckId": UNKNOWN}, {"slide": "1"}, {"deckId": "", "slide": "1"}, {"deckId": UNKNOWN, "slide": 0},
               {"deckId": UNKNOWN, "slide": ""}, [1, 2], "a string", 7)))
submit.add("E400 Invalid slide",
           *(Probe(f"slide {v!r}", post("/api/pptx-submit", {"deckId": UNKNOWN, "slide": v})) for v in ("0", "-1", "abc", True)))
submit.add("E403 Not your deck",
           Probe("an unknown deck", post("/api/pptx-submit", {"deckId": UNKNOWN, "slide": "1"})),
           Probe("bob's request for alice's deck", with_deck(lambda env, d: submit_slide(env, d, who="bob"))))
submit.add("E404 Image not found",
           Probe("no slide 5", with_deck(lambda env, d: env.client.post(
               "/api/pptx-submit", json={"deckId": d, "slide": "5"}, headers=env.auth("alice")))),
           Probe("version 3 was never rendered", with_deck(lambda env, d: submit_slide(env, d, version="3"))))
submit.na("E422 Brand layout rejected by the PPTX service (invalid archetype or furniture).", NO_UPSTREAM)
submit.na("E413 Brand layout assets are too large for the PPTX service.", NO_UPSTREAM)
submit.na("E502 PPTX service error", NO_UPSTREAM)
submit.add("E500 Internal error",
           Probe("malformed JSON", post("/api/pptx-submit", raw=b"{nope")),
           Probe("JSON null (destructuring null threw)", post("/api/pptx-submit", raw=b"null")),
           Probe("a non-UUID deckId: 500, not 403 (quirk 2)", post("/api/pptx-submit", {"deckId": "deck-1", "slide": "1"})),
           Probe("the deck store is unreachable",
                 lambda env: (broken(env, "decks", "get"), post("/api/pptx-submit", {"deckId": UNKNOWN, "slide": "1"})(env))[1]))

# -------------------------------------------------------------------------------------- pptx-status
status = RouteCases("/api/pptx-status")
add_auth(status, "GET", f"/api/pptx-status?jobId={UNKNOWN}")
status.add("R0",
           Probe("queued", get_after(lambda env: finished_slide_job(env, run=False), "/api/pptx-status?jobId={}"),
                 _exactly({"status": "queued", "done": False, "failed": False})),
           Probe("done", get_after(finished_slide_job, "/api/pptx-status?jobId={}"),
                 _exactly({"status": "done", "done": True, "failed": False})),
           Probe("NO ownership check: bob polls alice's job (TODO-P5 ownership)",
                 get_after(finished_slide_job, "/api/pptx-status?jobId={}", who="bob"),
                 _exactly({"status": "done", "done": True, "failed": False})),
           Probe("an image-to-slide job, queued", lambda env: env.client.get(
               f"/api/pptx-status?jobId={upload(tiny_png())(env).json()['jobId']}", headers=env.auth("alice")),
               _exactly({"status": "queued", "done": False, "failed": False})),
           Probe("failed: an image-to-slide job that failed",
                 lambda env: _failed_status(env), _exactly({"status": "failed", "done": False, "failed": True})))


def _failed_status(env: DarwinEnv) -> Any:
    r = env.client.post("/api/image-to-slide", files={"file": ("x.png", tiny_png(), "image/png")},
                        headers=env.auth("alice"))
    job_id = r.json()["jobId"]
    env.runtime.handlers[SLIDE_JOB] = _failing
    env.run_jobs()
    return env.client.get(f"/api/pptx-status?jobId={job_id}", headers=env.auth("alice"))


async def _failing(_job: Any) -> Any:
    from app.core.jobs.worker import PermanentJobError

    raise PermanentJobError("The image is empty or too large.")


status.add("E400 Missing jobId", Probe("absent", get("/api/pptx-status")), Probe("empty", get("/api/pptx-status?jobId=")))
status.add("E502 PPTX service error",
           Probe("unknown id", get(f"/api/pptx-status?jobId={UNKNOWN}")),
           Probe("a storyline job's id is not a pptx job",
                 get_after(lambda env: env.client.post("/api/storyline", json={"topic": "x"},
                                                       headers=env.auth("alice")).json()["jobId"],
                           "/api/pptx-status?jobId={}")),
           Probe("a deck job's id", get_after(lambda env: finished_deck_job(env, run=False), "/api/pptx-status?jobId={}")))
status.add("E500 Internal error", Probe("the job store is unreachable",
                                        lambda env: (broken(env, "jobs", "get"), get(f"/api/pptx-status?jobId={UNKNOWN}")(env))[1]))

# -------------------------------------------------------------------------------------- pptx-result
result = RouteCases("/api/pptx-result")
add_auth(result, "GET", f"/api/pptx-result?jobId={UNKNOWN}")
result.add("R0",
           Probe("done", get_after(finished_slide_job, "/api/pptx-result?jobId={}"), _is_pptx),
           Probe("NO ownership check: bob downloads alice's (TODO-P5 ownership)",
                 get_after(finished_slide_job, "/api/pptx-result?jobId={}", who="bob"), _is_pptx),
           Probe("repeatable", lambda env: _twice(env), _is_pptx))


def _twice(env: DarwinEnv) -> Any:
    job_id = finished_slide_job(env)
    assert env.client.get(f"/api/pptx-result?jobId={job_id}", headers=env.auth("alice")).status_code == 200
    return env.client.get(f"/api/pptx-result?jobId={job_id}", headers=env.auth("alice"))


result.add("E400 Missing jobId", Probe("absent", get("/api/pptx-result")), Probe("empty", get("/api/pptx-result?jobId=")))
result.add("E502 PPTX service unavailable",
           Probe("unknown id", get(f"/api/pptx-result?jobId={UNKNOWN}")),
           Probe("not finished yet", get_after(lambda env: finished_slide_job(env, run=False), "/api/pptx-result?jobId={}")))
result.add("E500 Internal error", Probe("the job store is unreachable",
                                        lambda env: (broken(env, "jobs", "get"), get(f"/api/pptx-result?jobId={UNKNOWN}")(env))[1]))

# --------------------------------------------------------------------------------- pptx-deck-submit
deck_submit = RouteCases("/api/pptx-deck-submit")
add_auth(deck_submit, "POST", "/api/pptx-deck-submit", json={"deckId": UNKNOWN, "jobIds": ["j"]})


def _deck_queued(env: DarwinEnv, response: Any) -> None:
    record = env.job("alice", response.json()["deckJobId"])
    assert record.type == "darwin.pptx_deck" and record.inputs["presentationTitle"] == "Q3"


deck_submit.add("R0",
                Probe("200 not 202", lambda env: env.client.post(
                    "/api/pptx-deck-submit", json={"deckId": env.seed_deck("alice"), "jobIds": ["any-id"],
                                                   "presentationTitle": "Q3"}, headers=env.auth("alice")), _deck_queued),
                Probe("no title: ''", lambda env: env.client.post(
                    "/api/pptx-deck-submit", json={"deckId": env.seed_deck("alice"), "jobIds": ["a", "b"]},
                    headers=env.auth("alice"))))
deck_submit.add("E405 Method not allowed",
                Probe("GET without a token: 405 BEFORE auth", lambda env: env.client.get("/api/pptx-deck-submit")))
deck_submit.add("E400 Missing params",
                *(Probe(f"body {b!r}", post("/api/pptx-deck-submit", b)) for b in (
                    {}, {"deckId": UNKNOWN}, {"deckId": UNKNOWN, "jobIds": []}, {"deckId": UNKNOWN, "jobIds": "j1"},
                    {"jobIds": ["j1"]}, [1])))
deck_submit.add("E403 Not your deck",
                Probe("unknown deck", post("/api/pptx-deck-submit", {"deckId": UNKNOWN, "jobIds": ["j1"]})),
                Probe("bob's request for alice's deck", lambda env: env.client.post(
                    "/api/pptx-deck-submit", json={"deckId": env.seed_deck("alice"), "jobIds": ["j1"]},
                    headers=env.auth("bob"))))
deck_submit.add("E502 PPTX service error",
                Probe("a job id that is not a string (upstream 422)", lambda env: env.client.post(
                    "/api/pptx-deck-submit", json={"deckId": env.seed_deck("alice"), "jobIds": [1]},
                    headers=env.auth("alice"))),
                Probe("a title that is not a string (upstream 422)", lambda env: env.client.post(
                    "/api/pptx-deck-submit", json={"deckId": env.seed_deck("alice"), "jobIds": ["j"],
                                                   "presentationTitle": 5}, headers=env.auth("alice"))))
deck_submit.add("E500 Internal error",
                Probe("malformed JSON", post("/api/pptx-deck-submit", raw=b"[")),
                Probe("JSON null", post("/api/pptx-deck-submit", raw=b"null")),
                Probe("non-UUID deckId", post("/api/pptx-deck-submit", {"deckId": "nope", "jobIds": ["j"]})))

# --------------------------------------------------------------------------------- pptx-deck-status
deck_status = RouteCases("/api/pptx-deck-status")
add_auth(deck_status, "GET", f"/api/pptx-deck-status?deckJobId={UNKNOWN}")
deck_status.add("R0",
                Probe("queued", get_after(lambda env: finished_deck_job(env, run=False), "/api/pptx-deck-status?deckJobId={}"),
                      _exactly({"status": "queued", "done": False, "failed": False})),
                Probe("done", get_after(finished_deck_job, "/api/pptx-deck-status?deckJobId={}"),
                      _exactly({"status": "done", "done": True, "failed": False})),
                Probe("failed: an unknown slide job id is stitched and fails later, as upstream did",
                      get_after(lambda env: finished_deck_job(env, job_ids=[UNKNOWN]), "/api/pptx-deck-status?deckJobId={}"),
                      _exactly({"status": "failed", "done": False, "failed": True})),
                Probe("NO ownership check (TODO-P5 ownership)",
                      get_after(finished_deck_job, "/api/pptx-deck-status?deckJobId={}", who="bob")))
deck_status.add("E400 Missing deckJobId", Probe("absent", get("/api/pptx-deck-status")),
                Probe("jobId is not the parameter", get(f"/api/pptx-deck-status?jobId={UNKNOWN}")))
deck_status.add("E502 PPTX service error",
                Probe("unknown id", get(f"/api/pptx-deck-status?deckJobId={UNKNOWN}")),
                Probe("a slide job's id", get_after(lambda env: finished_slide_job(env, run=False),
                                                    "/api/pptx-deck-status?deckJobId={}")))
deck_status.add("E500 Internal error", Probe("the job store is unreachable", lambda env: (
    broken(env, "jobs", "get"), get(f"/api/pptx-deck-status?deckJobId={UNKNOWN}")(env))[1]))

# --------------------------------------------------------------------------------- pptx-deck-result
deck_result = RouteCases("/api/pptx-deck-result")
add_auth(deck_result, "GET", f"/api/pptx-deck-result?deckJobId={UNKNOWN}")
deck_result.add("R0", Probe("done", get_after(finished_deck_job, "/api/pptx-deck-result?deckJobId={}"), _is_pptx))
deck_result.add("E400 Missing deckJobId", Probe("absent", get("/api/pptx-deck-result")))
deck_result.add("E502 PPTX service unavailable",
                Probe("unknown", get(f"/api/pptx-deck-result?deckJobId={UNKNOWN}")),
                Probe("not done", get_after(lambda env: finished_deck_job(env, run=False),
                                            "/api/pptx-deck-result?deckJobId={}")),
                Probe("failed", get_after(lambda env: finished_deck_job(env, job_ids=[UNKNOWN]),
                                          "/api/pptx-deck-result?deckJobId={}")))
deck_result.add("E500 Internal error", Probe("the job store is unreachable", lambda env: (
    broken(env, "jobs", "get"), get(f"/api/pptx-deck-result?deckJobId={UNKNOWN}")(env))[1]))

# ----------------------------------------------------------------------------------- image-to-slide
image = RouteCases("/api/image-to-slide")
add_auth(image, "POST", "/api/image-to-slide", files={"file": ("s.png", b"x", "image/png")})


def upload(content: bytes, mime: str | None = "image/png", *, name: str = "slide.png", field: str = "file") -> Any:
    def run(env: DarwinEnv) -> Any:
        part = (name, content, mime) if mime is not None else (name, content)
        return env.client.post("/api/image-to-slide", files={field: part}, headers=env.auth("alice"))
    return run


def _image_queued(env: DarwinEnv, response: Any) -> None:
    record = env.job("alice", response.json()["jobId"])
    assert record.type == SLIDE_JOB and record.inputs["imageName"].startswith("slide.")


image.add("R0",
          Probe("a PNG: 202", upload(tiny_png()), _image_queued),
          Probe("a JPEG", upload(b"\xff\xd8\xff" + b"0" * 100, "image/jpeg")),
          Probe("a GIF (converted to PNG for the pipeline)", lambda env: upload(_gif(), "image/gif")(env), _image_queued),
          Probe("exactly 4 MiB", upload(b"0" * (4 * 1024 * 1024), "image/webp")))


def _gif() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("P", (8, 8)).save(buf, format="GIF")
    return buf.getvalue()


image.add("E405 POST only",
          Probe("GET with a token (after auth)", lambda env: env.client.get("/api/image-to-slide", headers=env.auth("alice"))))
image.add('E400 Send the image as multipart/form-data with a "file" field',
          Probe("JSON is rejected", lambda env: env.client.post("/api/image-to-slide", json={"file": "aGk="},
                                                                headers=env.auth("alice"))),
          Probe("no body", lambda env: env.client.post("/api/image-to-slide", headers=env.auth("alice"))))
image.add('E400 Missing "file" field',
          Probe("another field name", upload(tiny_png(), field="image")),
          Probe("file as a text field", lambda env: env.client.post(
              "/api/image-to-slide", data={"file": "not a file"}, files={"other": ("o.txt", b"o", "text/plain")},
              headers=env.auth("alice"))),
          Probe("a broken multipart body", lambda env: env.client.post(
              "/api/image-to-slide", content=b"garbage", headers={**env.auth("alice"),
                                                                 "content-type": "multipart/form-data; boundary=zzz"})))
image.add("E400 file must be image/png, image/jpeg, image/webp, or image/gif",
          Probe("a PDF", upload(b"%PDF-1.7", "application/pdf")),
          Probe("image/svg+xml", upload(b"<svg/>", "image/svg+xml")))
image.add("E400 Empty image", Probe("zero bytes", upload(b"")))
image.add("E400 Image must be 4 MB or smaller", Probe("4 MiB + 1", upload(b"0" * (4 * 1024 * 1024 + 1))))
image.na("E502 PPTX service error", NO_UPSTREAM)
image.add("E500 Internal error", Probe("the blob store is unreachable",
                                       lambda env: (broken(env, "blobs", "put"), upload(tiny_png())(env))[1]))

ROUTES = [submit, status, result, deck_submit, deck_status, deck_result, image]
