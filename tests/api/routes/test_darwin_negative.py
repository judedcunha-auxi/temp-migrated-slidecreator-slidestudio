"""The six negative tests per Darwin route (plan Phase 5 "Proof"), for the routes ported so far.

1. no token -> 401 "Missing bearer token"
2. an expired token -> 401 "Invalid or expired session"
3. a token for another audience -> 401 "Invalid or expired session"
4. another user's object -> the route's own answer (403, or Darwin's quirk where there is no check)
5. invalid input -> the route's 400
6. over the limit -> TODO-P5 (Redis rate limits and in-flight caps arrive in Phase 5)

Expected codes and texts are today's (contract/INDEX.md), not the standard's.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.fakes.darwin import DarwinEnv, darwin_env
from tests.fakes.image_gen import tiny_png

UNKNOWN = "3fa85f64-5717-4562-b3fc-2c963f66afa6"


def _alice_storyline_job(env: DarwinEnv) -> str:
    return str(env.client.post("/api/storyline", json={"topic": "x"}, headers=env.auth("alice")).json()["jobId"])


def _alice_slide_job(env: DarwinEnv) -> str:
    deck = env.seed_deck("alice")
    return str(env.client.post("/api/pptx-submit", json={"deckId": deck, "slide": "1"},
                               headers=env.auth("alice")).json()["jobId"])


def _alice_deck_job(env: DarwinEnv) -> str:
    deck = env.seed_deck("alice")
    return str(env.client.post("/api/pptx-deck-submit", json={"deckId": deck, "jobIds": ["j"]},
                               headers=env.auth("alice")).json()["deckJobId"])


Send = Callable[[DarwinEnv, dict[str, str]], Any]


@dataclass(frozen=True)
class Route:
    path: str
    send: Send                                   # a valid request, with these headers
    invalid: Send                                # an invalid one, as an authenticated caller
    invalid_answer: tuple[int, str]
    others: Callable[[DarwinEnv], Any] | None    # bob touches alice's object (None: the route has no object)
    others_answer: tuple[int, str] | int | None = None


ROUTES = [
    Route("/api/storyline", lambda env, h: env.client.post("/api/storyline", json={"topic": "x"}, headers=h),
          lambda env, h: env.client.post("/api/storyline", json={}, headers=h), (400, "Topic is required"), None),
    Route("/api/storyline-status", lambda env, h: env.client.get(f"/api/storyline-status?jobId={UNKNOWN}", headers=h),
          lambda env, h: env.client.get("/api/storyline-status", headers=h), (400, "jobId is required"),
          lambda env: env.client.get(f"/api/storyline-status?jobId={_alice_storyline_job(env)}", headers=env.auth("bob")),
          (403, "Not your job")),
    Route("/api/intake", lambda env, h: env.client.post("/api/intake", json={"messages": []}, headers=h),
          lambda env, h: env.client.post("/api/intake", json={"messages": []}, headers=h),
          (400, "messages array required"), None),
    Route("/api/pptx-submit",
          lambda env, h: env.client.post("/api/pptx-submit", json={"deckId": UNKNOWN, "slide": "1"}, headers=h),
          lambda env, h: env.client.post("/api/pptx-submit", json={"deckId": UNKNOWN, "slide": "abc"}, headers=h),
          (400, "Invalid slide"),
          lambda env: env.client.post("/api/pptx-submit", json={"deckId": env.seed_deck("alice"), "slide": "1"},
                                      headers=env.auth("bob")), (403, "Not your deck")),
    # No ownership check today (quirk 5; TODO-P5 ownership): bob CAN poll and fetch alice's jobs.
    Route("/api/pptx-status", lambda env, h: env.client.get(f"/api/pptx-status?jobId={UNKNOWN}", headers=h),
          lambda env, h: env.client.get("/api/pptx-status", headers=h), (400, "Missing jobId"),
          lambda env: env.client.get(f"/api/pptx-status?jobId={_alice_slide_job(env)}", headers=env.auth("bob")), 200),
    Route("/api/pptx-result", lambda env, h: env.client.get(f"/api/pptx-result?jobId={UNKNOWN}", headers=h),
          lambda env, h: env.client.get("/api/pptx-result?jobId=", headers=h), (400, "Missing jobId"),
          lambda env: env.client.get(f"/api/pptx-result?jobId={_alice_slide_job(env)}", headers=env.auth("bob")),
          (502, "PPTX service unavailable")),  # not finished: the same 502 alice would get
    Route("/api/pptx-deck-submit",
          lambda env, h: env.client.post("/api/pptx-deck-submit", json={"deckId": UNKNOWN, "jobIds": ["j"]}, headers=h),
          lambda env, h: env.client.post("/api/pptx-deck-submit", json={"deckId": UNKNOWN, "jobIds": []}, headers=h),
          (400, "Missing params"),
          lambda env: env.client.post("/api/pptx-deck-submit", json={"deckId": env.seed_deck("alice"), "jobIds": ["j"]},
                                      headers=env.auth("bob")), (403, "Not your deck")),
    Route("/api/pptx-deck-status",
          lambda env, h: env.client.get(f"/api/pptx-deck-status?deckJobId={UNKNOWN}", headers=h),
          lambda env, h: env.client.get("/api/pptx-deck-status", headers=h), (400, "Missing deckJobId"),
          lambda env: env.client.get(f"/api/pptx-deck-status?deckJobId={_alice_deck_job(env)}",
                                     headers=env.auth("bob")), 200),
    Route("/api/pptx-deck-result",
          lambda env, h: env.client.get(f"/api/pptx-deck-result?deckJobId={UNKNOWN}", headers=h),
          lambda env, h: env.client.get("/api/pptx-deck-result", headers=h), (400, "Missing deckJobId"),
          lambda env: env.client.get(f"/api/pptx-deck-result?deckJobId={_alice_deck_job(env)}",
                                     headers=env.auth("bob")), (502, "PPTX service unavailable")),
    Route("/api/image-to-slide",
          lambda env, h: env.client.post("/api/image-to-slide", files={"file": ("s.png", tiny_png(), "image/png")},
                                         headers=h),
          lambda env, h: env.client.post("/api/image-to-slide", json={"file": "x"}, headers=h),
          (400, 'Send the image as multipart/form-data with a "file" field'), None),
]
IDS = [r.path for r in ROUTES]


@pytest.mark.parametrize("route", ROUTES, ids=IDS)
def test_1_no_token(route: Route, tmp_path: Path) -> None:
    with darwin_env(tmp_path) as env:
        r = route.send(env, {})
        assert (r.status_code, r.json()) == (401, {"error": "Missing bearer token"})


@pytest.mark.parametrize("route", ROUTES, ids=IDS)
def test_2_expired_token(route: Route, tmp_path: Path) -> None:
    with darwin_env(tmp_path) as env:
        r = route.send(env, env.auth("alice", expired=True))
        assert (r.status_code, r.json()) == (401, {"error": "Invalid or expired session"})


@pytest.mark.parametrize("route", ROUTES, ids=IDS)
def test_3_wrong_audience(route: Route, tmp_path: Path) -> None:
    with darwin_env(tmp_path) as env:
        r = route.send(env, env.auth("alice", audience="another-service"))
        assert (r.status_code, r.json()) == (401, {"error": "Invalid or expired session"})


@pytest.mark.parametrize("route", ROUTES, ids=IDS)
def test_4_another_users_object(route: Route, tmp_path: Path) -> None:
    if route.others is None:
        pytest.skip("the route has no per-user object: every call creates the caller's own job")
    with darwin_env(tmp_path) as env:
        r = route.others(env)
        if isinstance(route.others_answer, int):
            assert r.status_code == route.others_answer  # Darwin's missing check, kept (TODO-P5 ownership)
        else:
            assert route.others_answer is not None
            assert (r.status_code, r.json()) == (route.others_answer[0], {"error": route.others_answer[1]})


@pytest.mark.parametrize("route", ROUTES, ids=IDS)
def test_5_invalid_input(route: Route, tmp_path: Path) -> None:
    with darwin_env(tmp_path) as env:
        r = route.invalid(env, env.auth("alice"))
        assert (r.status_code, r.json()) == (route.invalid_answer[0], {"error": route.invalid_answer[1]})


@pytest.mark.parametrize("route", ROUTES, ids=IDS)
@pytest.mark.xfail(reason="TODO-P5 over the limit: Redis rate limits and in-flight caps arrive in Phase 5", run=False)
def test_6_over_the_limit(route: Route, tmp_path: Path) -> None:
    raise NotImplementedError
