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

import base64
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

# --- feature/darwin-brands: the brand routes and the admin routes ------------------------------------
# A brand that is not the caller's is "Brand not found" (404), never 403: Darwin cannot tell a brand
# you may not see from one that does not exist. Admin routes: a signed-in non-admin is 403.
PNG_B64 = base64.b64encode(tiny_png()).decode()
PDF_B64 = base64.b64encode(b"%PDF-1.4 x").decode()
PPTX_FILE = {"file": ("t.pptx", b"PK\x03\x04" + b"\0" * 32, "application/octet-stream")}
BOX = {"left": 0.1, "top": 0.1, "width": 0.5, "height": 0.1}


def _alice_brand(env: DarwinEnv) -> str:
    return env.seed_brand("alice")


def _alice_job(path: str, *, files: Any = None, body: Callable[[DarwinEnv], Any] | None = None) -> Callable[[DarwinEnv], str]:
    """A job alice submitted to `path` (multipart `files`, or the JSON `body(env)` builds)."""
    def submit(env: DarwinEnv) -> str:
        if files is not None:
            r = env.client.post(path, headers=env.auth("alice"), files=files)
        else:
            r = env.client.post(path, headers=env.auth("alice"), json=body(env) if body else {})
        return str(r.json()["jobId"])
    return submit


def _admin(env: DarwinEnv) -> dict[str, str]:
    env.user("root", admin=True)
    return env.auth("root")


ROUTES += [
    Route("/api/brands", lambda env, h: env.client.get("/api/brands", headers=h),
          lambda env, h: env.client.post("/api/brands", json={"kit": {}}, headers=h),
          (400, "name is required (1-120 characters)"),
          lambda env: env.client.patch("/api/brands", json={"brandId": _alice_brand(env), "name": "x"},
                                       headers=env.auth("bob")), (404, "Brand not found")),
    Route("/api/brand-asset", lambda env, h: env.client.get("/api/brand-asset?kind=style-default", headers=h),
          lambda env, h: env.client.get("/api/brand-asset?kind=favicon", headers=h),
          (400, "kind must be one of logo, master, titleMaster, dividerMaster"),
          lambda env: env.client.post("/api/brand-asset", headers=env.auth("bob"), json={
              "brandId": _alice_brand(env), "kind": "logo", "b64": PNG_B64}), (404, "Brand not found")),
    Route("/api/brand-pptx", lambda env, h: env.client.post("/api/brand-pptx", files=PPTX_FILE, headers=h),
          lambda env, h: env.client.post("/api/brand-pptx", json={"file": "x"}, headers=h),
          (415, "Expected multipart/form-data"),
          lambda env: env.client.post("/api/brand-pptx", data={"brandId": _alice_brand(env)}, files=PPTX_FILE,
                                      headers=env.auth("bob")), (404, "Brand not found")),
    Route("/api/brand-pptx-status", lambda env, h: env.client.get(f"/api/brand-pptx-status?jobId={UNKNOWN}", headers=h),
          lambda env, h: env.client.get("/api/brand-pptx-status", headers=h), (400, "jobId is required"),
          lambda env: env.client.get(f"/api/brand-pptx-status?jobId={_alice_job('/api/brand-pptx', files=PPTX_FILE)(env)}",
                                     headers=env.auth("bob")), (403, "Not your job")),
    Route("/api/brand-archetypes", lambda env, h: env.client.post("/api/brand-archetypes", json={}, headers=h),
          lambda env, h: env.client.post("/api/brand-archetypes", json={}, headers=h), (400, "brandId required"),
          lambda env: env.client.post("/api/brand-archetypes", headers=env.auth("bob"), json={
              "brandId": _alice_brand(env), "layouts": {"cover": 0, "divider": 0, "content": 0}}),
          (404, "Brand not found")),
    Route("/api/brand-heading", lambda env, h: env.client.post("/api/brand-heading", json={}, headers=h),
          lambda env, h: env.client.post("/api/brand-heading", json={"brandId": UNKNOWN, "archetype": "x"}, headers=h),
          (400, "archetype must be cover, divider or content"),
          lambda env: env.client.post("/api/brand-heading", headers=env.auth("bob"), json={
              "brandId": _alice_brand(env), "archetype": "cover", "box": BOX}), (404, "Brand not found")),
    Route("/api/brand-extract", lambda env, h: env.client.post("/api/brand-extract", json={"b64": PDF_B64}, headers=h),
          lambda env, h: env.client.post("/api/brand-extract", json={"b64": PNG_B64}, headers=h),
          (400, "Guidelines must be a PDF"),
          lambda env: env.client.post("/api/brand-extract", headers=env.auth("bob"), json={
              "b64": PDF_B64, "brandId": _alice_brand(env)}), (404, "Brand not found")),
    Route("/api/brand-extract-status",
          lambda env, h: env.client.get(f"/api/brand-extract-status?jobId={UNKNOWN}", headers=h),
          lambda env, h: env.client.get("/api/brand-extract-status?jobId=", headers=h), (400, "jobId is required"),
          lambda env: env.client.get(
              f"/api/brand-extract-status?jobId={_alice_job('/api/brand-extract', body=lambda e: {'b64': PDF_B64})(env)}",
              headers=env.auth("bob")), (403, "Not your job")),
    Route("/api/brand-preview", lambda env, h: env.client.post("/api/brand-preview", json={"brandId": UNKNOWN}, headers=h),
          lambda env, h: env.client.post("/api/brand-preview", json={}, headers=h), (400, "brandId required"),
          lambda env: env.client.post("/api/brand-preview", json={"brandId": _alice_brand(env)},
                                      headers=env.auth("bob")), (404, "Brand not found")),
    Route("/api/brand-preview-status",
          lambda env, h: env.client.get(f"/api/brand-preview-status?jobId={UNKNOWN}", headers=h),
          lambda env, h: env.client.get("/api/brand-preview-status", headers=h), (400, "jobId is required"),
          lambda env: env.client.get(
              f"/api/brand-preview-status?jobId={_alice_job('/api/brand-preview', body=lambda e: {'brandId': _alice_brand(e)})(env)}",
              headers=env.auth("bob")), (403, "Not your job")),
    # Admin routes: "another user's object" is the admin route itself, called by a non-admin.
    # admin-metrics takes no input that can be invalid (a junk ?days= is 30), so its "invalid input"
    # row is the same refusal: a non-admin never reaches the parsing.
    Route("/api/admin-metrics", lambda env, h: env.client.get("/api/admin-metrics", headers=h),
          lambda env, h: env.client.get("/api/admin-metrics?days=abc", headers=h), (403, "Admin access required"),
          lambda env: env.client.get("/api/admin-metrics", headers=env.auth("alice")), (403, "Admin access required")),
    Route("/api/admin-org-brands", lambda env, h: env.client.get("/api/admin-org-brands", headers=h),
          lambda env, h: env.client.post("/api/admin-org-brands", json={"action": "rename"}, headers=_admin(env)),
          (400, "action must be createBrand or addDomain"),
          lambda env: env.client.delete("/api/admin-org-brands?domain=acme.com", headers=env.auth("alice")),
          (403, "Admin access required")),
]

# --- the generation batch (Phase 7a): decks, generate/retry/status, refine/revert/slide-transcript, media, quick
def _alice_quick_job(env: DarwinEnv, *, done: bool = False) -> str:
    from tests.fakes.darwin_decks import storyline_reply

    env.user("alice")
    if done:
        env.model.steps.append(storyline_reply(1))
    job = str(env.client.post("/api/quick-generate", json={"topic": "x"}, headers=env.auth("alice")).json()["jobId"])
    if done:
        env.run_jobs()
    return job


def _bob_posts(path: str, body: Callable[[str], dict[str, Any]]) -> Callable[[DarwinEnv], Any]:
    return lambda env: env.client.post(path, json=body(env.seed_deck("alice")), headers=env.auth("bob"))


def _bob_gets(template: str) -> Callable[[DarwinEnv], Any]:
    return lambda env: env.client.get(template.format(deck=env.seed_deck("alice")), headers=env.auth("bob"))


ROUTES += [
    Route("/api/decks", lambda env, h: env.client.get("/api/decks", headers=h),
          lambda env, h: env.client.delete("/api/decks", headers=h), (400, "id required"),
          _bob_gets("/api/decks?id={deck}"), (404, "Deck not found")),
    Route("/api/generate", lambda env, h: env.client.post("/api/generate", json={"slides": []}, headers=h),
          lambda env, h: env.client.post("/api/generate", json={"presentationTitle": "T", "slides": []}, headers=h),
          (500, "Internal error"), None),  # quirk 3: validation failures are a 500
    Route("/api/retry", lambda env, h: env.client.post("/api/retry", json={"deckId": UNKNOWN, "retryOnly": [1]},
                                                       headers=h),
          lambda env, h: env.client.post("/api/retry", json={"deckId": UNKNOWN}, headers=h),
          (400, "deckId and retryOnly required"),
          _bob_posts("/api/retry", lambda d: {"deckId": d, "retryOnly": [1]}), (403, "Not your deck")),
    Route("/api/status", lambda env, h: env.client.get(f"/api/status?deckId={UNKNOWN}", headers=h),
          lambda env, h: env.client.get("/api/status", headers=h), (400, "deckId is required"),
          _bob_gets("/api/status?deckId={deck}"), (403, "Not your deck")),
    Route("/api/refine", lambda env, h: env.client.post("/api/refine", json={"deckId": UNKNOWN, "number": 1,
                                                                             "instruction": "x"}, headers=h),
          lambda env, h: env.client.post("/api/refine", json={"deckId": UNKNOWN, "number": 1, "instruction": " "},
                                         headers=h), (400, "instruction is required"),
          _bob_posts("/api/refine", lambda d: {"deckId": d, "number": 1, "instruction": "x"}), (403, "Not your deck")),
    Route("/api/revert", lambda env, h: env.client.post("/api/revert", json={"deckId": UNKNOWN, "number": 1,
                                                                             "version": 1}, headers=h),
          lambda env, h: env.client.post("/api/revert", json={"deckId": UNKNOWN, "number": 1}, headers=h),
          (400, "deckId, number, version required"),
          _bob_posts("/api/revert", lambda d: {"deckId": d, "number": 1, "version": 1}), (403, "Not your deck")),
    Route("/api/slide-transcript",
          lambda env, h: env.client.post("/api/slide-transcript", json={"deckId": UNKNOWN, "slideNumber": 1,
                                                                        "messages": []}, headers=h),
          lambda env, h: env.client.post("/api/slide-transcript", json={"deckId": UNKNOWN}, headers=h),
          (400, "Missing deckId, slideNumber, or messages"),
          _bob_posts("/api/slide-transcript", lambda d: {"deckId": d, "slideNumber": 1, "messages": []}),
          (403, "Not your deck")),
    Route("/api/image", lambda env, h: env.client.get(f"/api/image?deckId={UNKNOWN}&slide=1", headers=h),
          lambda env, h: env.client.get(f"/api/image?deckId={UNKNOWN}&slide=0", headers=h), (400, "Bad params"),
          _bob_gets("/api/image?deckId={deck}&slide=1"), (403, "Not your deck")),
    Route("/api/pdf", lambda env, h: env.client.get(f"/api/pdf?deckId={UNKNOWN}&slide=1", headers=h),
          lambda env, h: env.client.get("/api/pdf?slide=1", headers=h), (400, "Bad params"),
          _bob_gets("/api/pdf?deckId={deck}&slide=1"), (403, "Not your deck")),
    Route("/api/pdf-deck", lambda env, h: env.client.get(f"/api/pdf-deck?deckId={UNKNOWN}", headers=h),
          lambda env, h: env.client.get("/api/pdf-deck", headers=h), (400, "Missing deckId"),
          _bob_gets("/api/pdf-deck?deckId={deck}"), (403, "Not your deck")),
    Route("/api/quick-generate", lambda env, h: env.client.post("/api/quick-generate", json={"topic": "x"}, headers=h),
          lambda env, h: env.client.post("/api/quick-generate", json={"topic": ""}, headers=h),
          (400, "Topic is required"), None),
    Route("/api/quick-status", lambda env, h: env.client.get(f"/api/quick-status?jobId={UNKNOWN}", headers=h),
          lambda env, h: env.client.get("/api/quick-status", headers=h), (400, "jobId is required"),
          lambda env: env.client.get(f"/api/quick-status?jobId={_alice_quick_job(env)}", headers=env.auth("bob")),
          (403, "Not your job")),
    Route("/api/quick-image", lambda env, h: env.client.get(f"/api/quick-image?jobId={UNKNOWN}&slide=1", headers=h),
          lambda env, h: env.client.get(f"/api/quick-image?jobId={UNKNOWN}&slide=x", headers=h), (400, "Bad params"),
          lambda env: env.client.get(f"/api/quick-image?jobId={_alice_quick_job(env, done=True)}&slide=1",
                                     headers=env.auth("bob")), (403, "Not your job")),
]
# --- end of the generation batch ---
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


# --- feature/darwin-brands: the two routes the six-test pattern does not fit -------------------------
def test_analytics_event_negatives(tmp_path: Path) -> None:
    """Auth as everywhere; invalid input is Darwin's SILENT 200 (fire-and-forget), never a 400; no
    per-user object (every batch is the caller's own)."""
    with darwin_env(tmp_path) as env:
        def post(headers: dict[str, str], **kwargs: Any) -> Any:
            return env.client.post("/api/analytics-event", headers=headers, **kwargs)

        assert (post({}, json={"events": []}).json()) == {"error": "Missing bearer token"}
        assert post(env.auth("alice", expired=True), json={"events": []}).status_code == 401
        assert post(env.auth("alice", audience="another-service"), json={"events": []}).status_code == 401
        r = post(env.auth("alice"), content=b"{not json")
        assert (r.status_code, r.json()) == (200, {"ok": True})


def test_userinfo_does_not_verify_by_design(tmp_path: Path) -> None:
    """`/api/userinfo` is Supabase Auth's OIDC userinfo shim (C8): it decodes, it does not verify. So a
    missing token is its own 401, and an expired or foreign-audience token still decodes (200)."""
    with darwin_env(tmp_path) as env:
        r = env.client.get("/api/userinfo")
        assert (r.status_code, r.json()) == (401, {"error": "Unauthorized"})
        for token in (env.issuer.token("alice", email="a@acme.com", expired=True),
                      env.issuer.token("alice", email="a@acme.com", audience="another-service")):
            r = env.client.get("/api/userinfo", headers={"Authorization": f"Bearer {token}"})
            assert (r.status_code, r.json()["email"]) == (200, "a@acme.com")
        r = env.client.get("/api/userinfo", headers={"Authorization": "Bearer not-a-jwt"})
        assert r.status_code == 401 and r.json()["error"] == "Invalid token"
