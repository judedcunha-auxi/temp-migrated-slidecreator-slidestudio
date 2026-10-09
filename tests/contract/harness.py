"""The contract harness: Darwin's recorded contract (data/api-*.json) is the oracle; cases only say HOW to
provoke each documented outcome, never WHAT it must look like.

Every documented outcome of a route is one **entry**:

* `R<i>`: the i-th item of the file's `responses` (a success);
* `E<status> <error text>`: an item of its `errors`, e.g. `E400 Topic is required`.

A case module (tests/contract/cases/*.py) maps entries to **probes**: functions that take a
`DarwinEnv` (tests/fakes/darwin.py: the service on fakes) and return the HTTP response. The test then
checks the response against the ENTRY, read from the JSON:

* the status code;
* errors: the body equals the documented `{"error": "..."}` exactly, and the content-type is exactly
  `application/json` (Darwin's json() helper; `_common.json` jsonHelper), or the error's own documented
  `contentType` (userinfo's errors). A documented text with `<placeholders>` ("<key> must be a string",
  "No rendered layout at index <n> — ...") matches any text in their place; the probe's `check` pins the
  exact one. Extra documented members (userinfo's `detail`) must be present as strings;
* responses: the content-type exactly as documented, every documented header exactly, and the body
  against `bodyShape` (`check_shape`): the same keys (no extras: an optional key must be ABSENT, not
  null), literal values (`'pending'`), and the types the description starts with (string, boolean,
  number). A key whose description says it appears "only when ..." or is "omitted ..." is optional.
  A probe may add its own assertions (`check`), e.g. on a binary body.

Entries that cannot happen any more are listed in the case module's `NOT_APPLICABLE` with the reason
(e.g. the DEV_SECRET_KEY bypass, which this service does not have). An entry with neither a case nor
a reason fails the build, so a newly ported route cannot silently skip part of its contract; a route
that is not served yet is reported as xfail "not yet ported".
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import httpx2 as httpx

from tests.fakes.darwin import DarwinEnv

DATA = Path(__file__).resolve().parent / "data"
JSON_CT = "application/json"

Run = Callable[[DarwinEnv], httpx.Response]
Check = Callable[[DarwinEnv, httpx.Response], None]


@dataclass(frozen=True)
class Probe:
    """One request that must produce the entry. `name` says what it varies (for the failure message)."""

    name: str
    run: Run
    check: Check | None = None


@dataclass(frozen=True)
class Entry:
    route: str
    id: str
    status: int
    spec: dict[str, Any]

    @property
    def is_error(self) -> bool:
        return self.id.startswith("E")


@dataclass
class RouteCases:
    """What a case module exports for one route."""

    route: str
    cases: dict[str, list[Probe]] = field(default_factory=dict)
    not_applicable: dict[str, str] = field(default_factory=dict)

    def add(self, entry_id: str, *probes: Probe) -> None:
        self.cases.setdefault(entry_id, []).extend(probes)

    def na(self, entry_id: str, reason: str) -> None:
        self.not_applicable[entry_id] = reason


@cache
def contract(route: str) -> dict[str, Any]:
    name = "api-" + route.removeprefix("/api/") + ".json"
    return json.loads((DATA / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@cache
def contract_routes() -> tuple[str, ...]:
    routes = []
    for path in sorted(DATA.glob("api-*.json")):
        route = json.loads(path.read_text(encoding="utf-8")).get("route")
        if route:  # api-background-functions.json has none: not served (D31)
            routes.append(route)
    return tuple(routes)


def entry_id_of_error(error: dict[str, Any]) -> str:
    body = error.get("body") or {}
    return f"E{error['status']} {body.get('error')}" if isinstance(body, dict) and "error" in body \
        else f"E{error['status']}"


def entries(route: str) -> list[Entry]:
    c = contract(route)
    out = [Entry(route, f"R{i}", int(r["status"]), r) for i, r in enumerate(c.get("responses") or [])]
    seen: set[str] = set()
    for e in c.get("errors") or []:
        eid = entry_id_of_error(e)
        assert eid not in seen, f"{route}: two error entries share the id {eid!r}"
        seen.add(eid)
        out.append(Entry(route, eid, int(e["status"]), e))
    return out


# ------------------------------------------------------------------------------------------ checks
def _type_word(desc: str) -> str:
    return desc.strip().split(" ", 1)[0].split("(", 1)[0].lower()


_OPTIONAL = re.compile(r"\bonly when\b|\bomitted\b", re.IGNORECASE)


def _optional(key: str, description: Any) -> bool:
    return key.endswith("?") or (isinstance(description, str) and bool(_OPTIONAL.search(description)))


def check_shape(actual: Any, shape: Any, path: str = "body") -> None:
    """`actual` against a contract `bodyShape` (see the module docstring)."""
    if isinstance(shape, dict):
        assert isinstance(actual, dict), f"{path}: expected an object, got {actual!r}"
        required = {k for k, v in shape.items() if not _optional(k, v)}
        allowed = {k.rstrip("?") for k in shape}
        assert required <= set(actual), f"{path}: missing {sorted(required - set(actual))}"
        assert set(actual) <= allowed, f"{path}: undocumented keys {sorted(set(actual) - allowed)}"
        for key, sub in shape.items():
            if key.rstrip("?") in actual:
                check_shape(actual[key.rstrip("?")], sub, f"{path}.{key.rstrip('?')}")
        return
    if not isinstance(shape, str):
        return
    text = shape.strip()
    if len(text) >= 2 and text[0] == "'" and text.endswith("'") and text.count("'") == 2:
        assert actual == text[1:-1], f"{path}: expected {text}, got {actual!r}"
        return
    word = _type_word(text)
    if word == "string":
        assert isinstance(actual, str), f"{path}: expected a string, got {actual!r}"
    elif word in ("boolean",) or text.startswith("status ==="):
        assert isinstance(actual, bool), f"{path}: expected a boolean, got {actual!r}"
    elif word == "number":
        assert isinstance(actual, (int, float)) and not isinstance(actual, bool), f"{path}: expected a number"
    elif word.startswith("string[]") or word.startswith("array"):
        assert isinstance(actual, list), f"{path}: expected an array, got {actual!r}"
    elif text.startswith("{}"):
        assert actual == {}, f"{path}: expected {{}}, got {actual!r}"
    # anything else is prose: no further check


def error_text_matches(actual: Any, documented: str) -> bool:
    """The documented error text, with each `<placeholder>` standing for any non-empty text."""
    if not isinstance(actual, str):
        return False
    pattern = re.sub(r"<[^<>]+>", ".+", re.escape(documented))
    return re.fullmatch(pattern, actual, re.DOTALL) is not None


def check_error_body(actual: Any, documented: dict[str, Any], where: str) -> None:
    assert isinstance(actual, dict), f"{where}: body {actual!r}"
    assert set(actual) == set(documented), f"{where}: body keys {sorted(actual)} != {sorted(documented)}"
    assert error_text_matches(actual.get("error"), str(documented["error"])), f"{where}: body {actual!r}"
    for key in documented:
        if key != "error":
            assert isinstance(actual[key], str) and actual[key], f"{where}: {key} {actual[key]!r}"


def check_entry(entry: Entry, response: httpx.Response) -> None:
    spec = entry.spec
    assert response.status_code == entry.status, (
        f"{entry.route} {entry.id}: status {response.status_code} != {entry.status}; body {response.text[:300]}")
    content_type = response.headers.get("content-type")
    if entry.is_error:
        wanted = str(spec.get("contentType") or JSON_CT).split(" (", 1)[0]
        assert content_type == wanted, f"{entry.route} {entry.id}: content-type {content_type!r}"
        check_error_body(response.json(), spec["body"], f"{entry.route} {entry.id}")
        return
    assert content_type == spec.get("contentType"), f"{entry.route} {entry.id}: content-type {content_type!r}"
    for name, value in (spec.get("headers") or {}).items():
        assert response.headers.get(name) == value, f"{entry.route} {entry.id}: header {name}"
    if spec.get("contentType") == JSON_CT:
        check_shape(response.json(), spec.get("bodyShape"))
