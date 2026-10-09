"""Darwin's /api contract, entry by entry (tests/contract/harness.py explains the oracle and the cases).

* every documented entry of every served route must pass its probes, or be listed NOT_APPLICABLE
  with a reason; an entry with neither fails;
* a route the service does not serve yet is xfail "not yet ported";
* the 7 `*-background` paths are not served at all (D31);
* cases may only name entries the contract documents.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.api.legacy import ALL_METHODS
from app.main import ROUTERS
from tests.contract.cases import ALL
from tests.contract.harness import DATA, Entry, check_entry, contract_routes, entries
from tests.fakes.darwin import darwin_env


def served_paths() -> set[str]:
    return {getattr(route, "path", "") for router in ROUTERS for route in router.routes}


SERVED = served_paths()
ENTRIES = [e for route in contract_routes() for e in entries(route)]


@pytest.mark.parametrize("entry", ENTRIES, ids=[f"{e.route} {e.id}" for e in ENTRIES])
def test_contract_entry(entry: Entry, tmp_path: Path) -> None:
    if entry.route not in SERVED:
        pytest.xfail("not yet ported")
    cases = ALL.get(entry.route)
    assert cases is not None, f"{entry.route} is served but has no contract cases (tests/contract/cases)"
    probes = cases.cases.get(entry.id)
    if not probes:
        reason = cases.not_applicable.get(entry.id)
        assert reason, f"{entry.route}: no case and no NOT_APPLICABLE reason for documented entry {entry.id!r}"
        pytest.skip(f"not reproducible: {reason}")
    for probe in probes:
        with darwin_env(tmp_path / probe.name.replace(" ", "_")[:40]) as env:
            response = probe.run(env)
            try:
                check_entry(entry, response)
                if probe.check is not None:
                    probe.check(env, response)
            except AssertionError as exc:
                raise AssertionError(f"probe {probe.name!r}: {exc}") from exc


def test_cases_only_name_documented_entries() -> None:
    for route, cases in ALL.items():
        documented = {e.id for e in entries(route)}
        unknown = (set(cases.cases) | set(cases.not_applicable)) - documented
        assert not unknown, f"{route}: cases for undocumented entries {sorted(unknown)}"
        both = set(cases.cases) & set(cases.not_applicable)
        assert not both, f"{route}: entries both covered and marked not applicable: {sorted(both)}"


def test_every_served_darwin_route_has_cases() -> None:
    darwin = {p for p in SERVED if p.startswith("/api/")}
    assert darwin <= set(contract_routes()), f"served /api routes with no contract file: {darwin - set(contract_routes())}"
    assert darwin <= set(ALL), f"served /api routes with no cases: {sorted(darwin - set(ALL))}"


BACKGROUND = [f["name"] for f in json.loads((DATA / "api-background-functions.json").read_text(encoding="utf-8"))["functions"]]


@pytest.mark.parametrize("name", BACKGROUND)
def test_background_functions_are_not_served(name: str, tmp_path: Path) -> None:
    """D31: the `-background` triggers were public and unauthenticated (C12); here they do not exist."""
    with darwin_env(tmp_path) as env:
        for method in ALL_METHODS:
            r = env.client.request(method, f"/api/{name}", json={"userId": "u", "jobId": "j"})
            assert r.status_code in (404, 405), (method, r.status_code)
            assert r.status_code == 404 or method == "OPTIONS"
