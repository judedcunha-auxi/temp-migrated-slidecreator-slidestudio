"""Contract cases: /api/decks (data/api-decks.json)."""

from __future__ import annotations

from typing import Any

from tests.contract.cases.common import add_auth, broken
from tests.contract.harness import Probe, RouteCases
from tests.fakes.darwin import DarwinEnv
from tests.fakes.darwin_decks import UNKNOWN, generated_deck, memo

decks = RouteCases("/api/decks")
add_auth(decks, "GET", "/api/decks")


def get(path: str, subject: str = "alice", method: str = "GET") -> Any:
    return lambda env: env.client.request(method, path, headers=env.auth(subject))


def _two_decks(env: DarwinEnv) -> Any:
    first = env.seed_deck("alice")
    env.store.clock.advance(seconds=5)
    second = env.seed_deck("alice", slides=2)
    env.seed_deck("bob")  # never listed for alice
    r = env.client.get("/api/decks", headers=env.auth("alice"))
    memo(env)["ids"] = (first, second)
    return r


def _listed(env: DarwinEnv, r: Any) -> None:
    first, second = memo(env)["ids"]
    listed = r.json()["decks"]
    assert [d["id"] for d in listed] == [second, first]  # created_at desc
    assert set(listed[0]) == {"id", "title", "status", "created_at"}


def _empty_list(_env: DarwinEnv, r: Any) -> None:
    assert r.json() == {"decks": []}


def _new_user(env: DarwinEnv) -> Any:
    return env.client.get("/api/decks", headers=env.auth("carol"))


decks.add("R0",
          Probe("my decks, newest first", _two_decks, _listed),
          Probe("a new user: []", _new_user, _empty_list),
          Probe("?id= (empty) is a list", get("/api/decks?id=")),
          Probe("POST acts as GET", get("/api/decks", method="POST")),
          Probe("PATCH acts as GET", get("/api/decks", method="PATCH")))


def _one(env: DarwinEnv) -> Any:
    deck_id = generated_deck(env, "alice", slides=3)
    r = env.client.get(f"/api/decks?id={deck_id}", headers=env.auth("alice"))
    memo(env)["id"] = deck_id
    return r


def _one_check(env: DarwinEnv, r: Any) -> None:
    body = r.json()
    assert body["deck"]["id"] == memo(env)["id"] and body["deck"]["status"] == "done"
    assert body["deck"]["title"] == "Q3 2026 Financial Results" and body["deck"]["inputs"] == {"topic": "Q3"}
    assert [s["number"] for s in body["slides"]] == [1, 2, 3]
    assert [s["type"] for s in body["slides"]] == ["title", "framework", "framework"]  # folded in from the storyline
    first = body["slides"][0]
    assert first["variant_a_status"] == "done" and first["variant_b_blob_key"] is None
    assert "Non-negotiable rules: " in first["prompt"]


decks.add("R1", Probe("my deck with its slides", _one, _one_check),
          Probe("PUT ?id= acts as GET", lambda env: env.client.put(f"/api/decks?id={env.seed_deck('alice')}",
                                                                   headers=env.auth("alice"))))


def _delete(env: DarwinEnv) -> Any:
    deck_id = env.seed_deck("alice")
    r = env.client.delete(f"/api/decks?id={deck_id}", headers=env.auth("alice"))
    memo(env)["id"] = deck_id
    return r


def _deleted(env: DarwinEnv, r: Any) -> None:
    after = env.client.get(f"/api/decks?id={memo(env)['id']}", headers=env.auth("alice"))
    assert after.status_code == 404


def _foreign_delete(env: DarwinEnv) -> Any:
    deck_id = env.seed_deck("alice")
    r = env.client.delete(f"/api/decks?id={deck_id}", headers=env.auth("bob"))
    memo(env)["id"] = deck_id
    return r


def _still_there(env: DarwinEnv, r: Any) -> None:
    assert env.client.get(f"/api/decks?id={memo(env)['id']}", headers=env.auth("alice")).status_code == 200


decks.add("R2", Probe("delete my deck", _delete, _deleted),
          Probe("delete a missing deck: still ok", get(f"/api/decks?id={UNKNOWN}", method="DELETE")),
          Probe("delete someone else's deck: ok, and nothing happens", _foreign_delete, _still_there))

decks.add("E400 id required", Probe("DELETE without id", get("/api/decks", method="DELETE")),
          Probe("DELETE ?id= (empty)", get("/api/decks?id=", method="DELETE")))
decks.add("E404 Deck not found", Probe("unknown uuid", get(f"/api/decks?id={UNKNOWN}")),
          Probe("someone else's deck",
                lambda env: env.client.get(f"/api/decks?id={env.seed_deck('alice')}", headers=env.auth("bob"))))
decks.add("E500 Internal error", Probe("GET a non-uuid id", get("/api/decks?id=not-a-uuid")),
          Probe("DELETE a non-uuid id", get("/api/decks?id=not-a-uuid", method="DELETE")),
          Probe("storage down", lambda env: (broken(env, "decks", "list_mine"),
                                             env.client.get("/api/decks", headers=env.auth("alice")))[1]))

ROUTES = [decks]
