"""design_refs reads the shared reference data in app/data (one copy, also read by the storyline)."""

from __future__ import annotations

import json

from app.core.design_refs import archetypes, vocabulary


def test_aliases_come_from_the_shared_frameworks_file():
    shared = json.loads(vocabulary.SHARED_FRAMEWORKS.read_text(encoding="utf-8"))
    assert vocabulary.ALIASES == shared["aliases"]
    assert vocabulary.SHARED_FRAMEWORKS.parent == archetypes.DATA.parent


def test_every_shared_name_and_alias_resolves_to_a_framework_the_prompt_knows():
    shared = json.loads(vocabulary.SHARED_FRAMEWORKS.read_text(encoding="utf-8"))
    known = {name for entries in vocabulary.FRAMEWORKS.values() for name in entries}
    assert set(shared["frameworks"]) <= known
    assert set(shared["aliases"].values()) <= known
    assert vocabulary.canonical_framework("two-by-two") == "2x2 matrix"


def test_design_refs_ships_no_data_of_its_own():
    package = archetypes.DATA.parents[1] / "core" / "design_refs"
    assert not list(package.rglob("*.json")), "reference data lives in app/data only"
