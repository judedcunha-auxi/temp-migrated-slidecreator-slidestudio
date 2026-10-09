"""Contract cases: /api/admin-metrics, /api/admin-org-brands, /api/analytics-event, /api/userinfo
(data/api-admin-metrics.json, api-admin-org-brands.json, api-analytics-event.json, api-userinfo.json).

`root` is an admin; `alice` is not.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from tests.contract.cases.common import add_auth, broken
from tests.contract.harness import Probe, RouteCases
from tests.fakes.darwin import DarwinEnv

UNKNOWN = "3fa85f64-5717-4562-b3fc-2c963f66afa6"


def as_admin(method: str, path: str, **kwargs: Any) -> Any:
    def run(env: DarwinEnv) -> Any:
        env.user("root", admin=True)
        return env.client.request(method, path, headers=env.auth("root"), **kwargs)
    return run


def as_alice(method: str, path: str, **kwargs: Any) -> Any:
    return lambda env: env.client.request(method, path, headers=env.auth("alice"), **kwargs)


def exactly(expected: Any) -> Any:
    def check(_env: DarwinEnv, response: Any) -> None:
        assert response.json() == expected, response.text
    return check


def field_is(key: str, expected: Any, *, size: bool = False) -> Any:
    """The body's `key` equals `expected` (or, with `size`, has that many items)."""
    def check(_env: DarwinEnv, response: Any) -> None:
        value = response.json()[key]
        assert (len(value) if size else value) == expected, response.text
    return check


# ------------------------------------------------------------------------------ /admin-metrics
metrics = RouteCases("/api/admin-metrics")
add_auth(metrics, "GET", "/api/admin-metrics")


def _populated(env: DarwinEnv) -> Any:
    env.user("root", email="root@auxi.ai", admin=True)
    env.seed_deck("alice", slides=2)
    env.client.post("/api/analytics-event", headers=env.auth("alice"), json={"events": [
        {"event_name": "element_clicked", "properties": {"text": "Generate", "path": "/new"}},
        {"event_name": "element_clicked", "properties": {"text": "Generate"}, "page_path": "/new"}]})
    return env.client.get("/api/admin-metrics?days=7", headers=env.auth("root"))


def _metrics_check(env: DarwinEnv, response: Any) -> None:
    body = response.json()
    assert body["summary"]["totalUsers"] == 2 and body["summary"]["totalDecks30d"] == 1
    assert len(body["dailyActivity"]) == 7
    assert body["recentDecks"][0]["title"] == "Seeded"
    assert body["topClicks"] == [{"text": "Generate", "path": "/new", "count": 2}]
    assert body["perUserClicks"][0]["total"] == 2 and body["perUserClicks"][0]["topText"] == "Generate"


metrics.add("R0",
            Probe("populated", _populated, _metrics_check),
            Probe("empty tables, junk days -> 30 buckets", as_admin("GET", "/api/admin-metrics?days=abc"),
                  field_is("dailyActivity", 30, size=True)),
            Probe("any method", as_admin("POST", "/api/admin-metrics?days=400")),
            Probe("events and transcripts unavailable: empty lists, still 200", lambda env: (
                broken(env, "analytics", "admin_activity"), as_admin("GET", "/api/admin-metrics")(env))[1],
                field_is("events", [])))
metrics.add("E403 Admin access required", Probe("not an admin", as_alice("GET", "/api/admin-metrics")))
metrics.add("E500 Internal error",
            Probe("profiles / decks / ledger unavailable", lambda env: (
                broken(env, "analytics", "admin_overview"), as_admin("GET", "/api/admin-metrics")(env))[1]))

# --------------------------------------------------------------------------- /admin-org-brands
orgs = RouteCases("/api/admin-org-brands")
add_auth(orgs, "GET", "/api/admin-org-brands")


def org_brand(env: DarwinEnv, name: str = "Acme", domain: str | None = "acme.com") -> str:
    return env.seed_brand("root", name=name, org=True, domain=domain)


def admin_call(method: str, path: str, body: Any = None, *, prepare: Any = None, raw: bytes | None = None) -> Any:
    """`prepare(env)` returns a brand id the path / body may use as {b}."""
    def run(env: DarwinEnv) -> Any:
        env.user("root", admin=True)
        b = prepare(env) if prepare else ""
        kwargs: dict[str, Any] = {}
        if raw is not None:
            kwargs["content"] = raw
        elif body is not None:
            kwargs["json"] = json.loads(json.dumps(body).replace("{b}", b))
        return env.client.request(method, path.replace("{b}", b), headers=env.auth("root"), **kwargs)
    return run


def _listed(env: DarwinEnv, response: Any) -> None:
    brands = response.json()["brands"]
    assert [b["name"] for b in brands] == ["Acme", "Zeta"]  # by name
    acme = brands[0]
    assert acme["domains"] == [{"domain": "acme.co.uk", "accounts": 0}, {"domain": "acme.com", "accounts": 2}]
    assert brands[1]["domains"] == [] and isinstance(acme["updatedAt"], str)


def _list(env: DarwinEnv) -> Any:
    env.user("root", admin=True)
    b = org_brand(env)
    org_brand(env, "Zeta", None)
    env.call(env.store.orgs.map_domain, env.ctx("root"), "acme.co.uk", b)
    env.user("u1", email="one@acme.com")
    env.user("u2", email="Two@ACME.com")
    return env.client.get("/api/admin-org-brands", headers=env.auth("root"))


orgs.add("R0",
         Probe("brands by name, domains by name, account counts", _list, _listed),
         Probe("none", as_admin("GET", "/api/admin-org-brands"), exactly({"brands": []})))


def _created(env: DarwinEnv, response: Any) -> None:
    body = response.json()
    assert body["name"] == "Acme" and body["isOrg"] is True
    assert env.brand("root", body["id"]).owner_id == env.user("root")  # the creating admin namespaces it


orgs.add("R1", Probe("createBrand", admin_call("POST", "/api/admin-org-brands",
                                               {"action": "createBrand", "name": "  Acme "}), _created))
orgs.add("R2", Probe("addDomain, '@' stripped and lower-cased", admin_call(
    "POST", "/api/admin-org-brands", {"action": "addDomain", "brandId": "{b}", "domain": " @Acme.IO "},
    prepare=lambda env: org_brand(env, domain=None)), field_is("domain", "acme.io")))
orgs.add("R3", Probe("addDomain again, same brand", admin_call(
    "POST", "/api/admin-org-brands", {"action": "addDomain", "brandId": "{b}", "domain": "acme.com"},
    prepare=lambda env: org_brand(env))))


def _gone(env: DarwinEnv, response: Any) -> None:
    assert response.json() == {"ok": True}
    assert env.client.get("/api/admin-org-brands", headers=env.auth("root")).json()["brands"][0]["domains"] == []


orgs.add("R4",
         Probe("DELETE ?domain=", admin_call("DELETE", "/api/admin-org-brands?domain=ACME.com ",
                                             prepare=lambda env: org_brand(env)), _gone),
         Probe("DELETE ?domain= never mapped", as_admin("DELETE", "/api/admin-org-brands?domain=nobody.org")),
         Probe("DELETE ?brandId=", admin_call("DELETE", "/api/admin-org-brands?brandId={b}",
                                              prepare=lambda env: org_brand(env))),
         Probe("domain wins over brandId", admin_call("DELETE", "/api/admin-org-brands?domain=acme.com&brandId={b}",
                                                      prepare=lambda env: org_brand(env)), _gone))
orgs.add("E403 Admin access required",
         Probe("not an admin, before any check or 405", as_alice("PUT", "/api/admin-org-brands")))
orgs.add("E400 Invalid JSON body",
         *(Probe(name, admin_call("POST", "/api/admin-org-brands", raw=raw))
           for name, raw in (("malformed", b"{"), ("empty", b""), ("null", b"null"), ("0", b"0"))))
orgs.add("E400 name is required (1-120 characters)",
         Probe("blank", admin_call("POST", "/api/admin-org-brands", {"action": "createBrand", "name": " "})),
         Probe("too long", admin_call("POST", "/api/admin-org-brands", {"action": "createBrand", "name": "n" * 121})))
orgs.add("E400 brandId is required",
         Probe("missing", admin_call("POST", "/api/admin-org-brands", {"action": "addDomain", "domain": "acme.com"})))
orgs.add("E404 Organization brand not found",
         Probe("unknown (checked before the domain)", admin_call(
             "POST", "/api/admin-org-brands", {"action": "addDomain", "brandId": UNKNOWN, "domain": "gmail.com"})),
         Probe("a personal brand", admin_call("POST", "/api/admin-org-brands",
                                              {"action": "addDomain", "brandId": "{b}", "domain": "acme.com"},
                                              prepare=lambda env: env.seed_brand("alice"))),
         Probe("a malformed id", admin_call("DELETE", "/api/admin-org-brands?brandId=nope")))
orgs.add("E400 domain is required",
         Probe("domain a number", admin_call("POST", "/api/admin-org-brands",
                                             {"action": "addDomain", "brandId": "{b}", "domain": 5},
                                             prepare=lambda env: org_brand(env, domain=None))))
orgs.add("E400 Enter a bare domain like acme.com",
         *(Probe(d, admin_call("POST", "/api/admin-org-brands", {"action": "addDomain", "brandId": "{b}", "domain": d},
                               prepare=lambda env: org_brand(env, domain=None))) for d in ("jane@acme.com", "acme", "")))
orgs.add("E400 gmail.com is a public email provider and can't be mapped to a brand",
         Probe("gmail", admin_call("POST", "/api/admin-org-brands",
                                   {"action": "addDomain", "brandId": "{b}", "domain": "@GMAIL.com"},
                                   prepare=lambda env: org_brand(env, domain=None))))
orgs.add('E409 acme.com is already mapped to "Other Brand" — remove it there first',
         Probe("mapped to another brand", admin_call(
             "POST", "/api/admin-org-brands", {"action": "addDomain", "brandId": "{b}", "domain": "acme.com"},
             prepare=lambda env: (org_brand(env, "Other Brand"), org_brand(env, "Mine", None))[1])))
orgs.add("E400 action must be createBrand or addDomain",
         Probe("unknown action", admin_call("POST", "/api/admin-org-brands", {"action": "rename"})),
         Probe("an array body", admin_call("POST", "/api/admin-org-brands", [1])))
orgs.add("E400 domain or brandId required", Probe("neither", as_admin("DELETE", "/api/admin-org-brands")))
orgs.add("E405 GET, POST or DELETE only", Probe("PATCH (after the admin check)", as_admin("PATCH", "/api/admin-org-brands")))
orgs.add("E500 Internal error",
         Probe("the store is unreachable", lambda env: (broken(env, "brands", "list_org"),
                                                        as_admin("GET", "/api/admin-org-brands")(env))[1]))

# ---------------------------------------------------------------------------- /analytics-event
events = RouteCases("/api/analytics-event")
add_auth(events, "POST", "/api/analytics-event", json={"events": []})


def _silent_no_write(env: DarwinEnv, response: Any) -> None:
    assert response.json() == {"ok": True}
    assert not [c for c in env.store.calls if c.operation in ("users.touch_last_seen", "analytics.record_event")]


events.add("R0",
           *(Probe(name, as_alice("POST", "/api/analytics-event", content=raw), _silent_no_write)
             for name, raw in (("malformed", b"{"), ("empty", b""), ("no events", b"{}"),
                               ("events not an array", b'{"events": {"a": 1}}'), ("null", b"null"))),
           Probe("bodyless GET", as_alice("GET", "/api/analytics-event"), _silent_no_write))


def _stored(expected: int, writes: int) -> Any:
    def check(env: DarwinEnv, response: Any) -> None:
        body = response.json()
        assert body["ok"] is True and body["stored"] == expected
        assert body["at"].endswith("Z") and len(body["at"]) == 24  # toISOString()
        recorded = [c for c in env.store.calls if c.operation in ("analytics.record_event",
                                                                  "analytics.record_page_view")]
        assert len(recorded) == writes
    return check


events.add("R1",
           Probe("a mixed batch: skipped events still counted", as_alice("POST", "/api/analytics-event", json={"events": [
               {"event_name": "page_viewed", "page_path": "/", "properties": {"session_id": "s1"}},
               {"event_name": "page_exited", "properties": {"path": "/", "session_id": "s1", "duration_seconds": 12,
                                                            "scroll_depth_pct": 80}},
               {"properties": {}}, None, "junk"]}), _stored(5, 2)),
           Probe("over 50: truncated silently", as_alice("POST", "/api/analytics-event", json={
               "events": [{"event_name": "x"}] * 60}), _stored(50, 50)),
           Probe("an empty array", as_alice("POST", "/api/analytics-event", json={"events": []}), _stored(0, 0)),
           Probe("a failed insert never surfaces", lambda env: (broken(env, "analytics", "record_event"), as_alice(
               "POST", "/api/analytics-event", json={"events": [{"event_name": "x"}]})(env))[1]))
events.add("E500 Internal error",
           Probe("the profile store is unreachable (outside the swallowed writes)", lambda env: (
               broken(env, "users", "get_or_create"), as_alice("POST", "/api/analytics-event", json={"events": []})(env))[1]))

# ------------------------------------------------------------------------------------ /userinfo
userinfo = RouteCases("/api/userinfo")


def jwt(payload: Any, *, raw_segment: str | None = None) -> str:
    segment = raw_segment if raw_segment is not None else base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{segment}.c2ln"


def info(header: str | None, method: str = "GET") -> Any:
    return lambda env: env.client.request(method, "/api/userinfo",
                                          headers={"Authorization": header} if header is not None else {})


userinfo.add("R0",
             Probe("B2C claims: emails[0], oid", info("Bearer " + jwt({
                 "oid": "9f1c", "sub": "other", "emails": ["jane@acme.com"], "name": "Jane Doe", "given_name": "Jane",
                 "family_name": "Doe"})), exactly({"sub": "9f1c", "email": "jane@acme.com", "name": "Jane Doe",
                                                   "given_name": "Jane", "family_name": "Doe"})),
             Probe("absent claims are omitted; Latin-1 atob garbles UTF-8", info("Bearer " + jwt({
                 "sub": "s1", "email": "j@acme.com", "name": "José"})), exactly({
                     "sub": "s1", "email": "j@acme.com", "name": "JosÃ©"})),
             Probe("an expired, unsigned token is still decoded (no verification)", info("bearer " + jwt({
                 "upn": "u@corp.com", "exp": 1})), exactly({"email": "u@corp.com"})),
             Probe("a non-Bearer scheme is decoded whole", info("Basic " + jwt({"email": "b@x.io", "sub": None}).replace(
                 "eyJhbGciOiJSUzI1NiJ9", "x")), exactly({"email": "b@x.io", "sub": None})),
             Probe("any method", info("Bearer " + jwt({"email": "a@b.co"}), method="DELETE")))
userinfo.add("E401 Unauthorized",
             Probe("no header", info(None)), Probe("Bearer and spaces only", info("Bearer   ")))
userinfo.add("E400 No email in token",
             Probe("no email claim", info("Bearer " + jwt({"sub": "x", "email": ""}))),
             Probe("a payload that is a JSON number", info("Bearer " + jwt(5))))


def _detail(text: str) -> Any:
    def check(_env: DarwinEnv, response: Any) -> None:
        assert response.json() == {"error": "Invalid token", "detail": text}, response.text
    return check


userinfo.add("E401 Invalid token",
             Probe("no '.' (a dev key)", info("Bearer sk-dev-123"), _detail(
                 "TypeError: Cannot read properties of undefined (reading 'replace')")),
             Probe("a payload of bad length", info("Bearer " + jwt(None, raw_segment="abcde")), _detail(
                 "InvalidCharacterError: The string to be decoded is not correctly encoded.")),
             Probe("a character outside base64", info("Bearer " + jwt(None, raw_segment="ab$d")), _detail(
                 "InvalidCharacterError: Invalid character")),
             Probe("not JSON", info("Bearer " + jwt(None, raw_segment=base64.b64encode(b"hello").decode().rstrip("=")))),
             Probe("a JSON null payload", info("Bearer " + jwt(None)), _detail(
                 "TypeError: Cannot read properties of null (reading 'emails')")))

ROUTES = [metrics, orgs, events, userinfo]
