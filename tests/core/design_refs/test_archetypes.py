"""`design_refs.archetypes`: the layout-archetype library behind `find_layout_reference`.

The library is the shared `app/data/archetypes.json` (one copy, also read by the storyline). Most tests
run on a small synthetic library (`tests/fixtures/design_refs/archetypes.synthetic.json`, 24 archetypes in
12 categories, named by `DESIGN_ARCHETYPES_PATH`) so their expectations do not move when the shared data
does; the first two check the shared library itself. Ported from the archetype tests in Slide Studio
`server/tests/test_design_tools.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import ai as ai_config
from app.core.design_refs import archetypes

SYNTHETIC = Path(__file__).resolve().parents[2] / "fixtures" / "design_refs" / "archetypes.synthetic.json"


@pytest.fixture(autouse=True)
def _synthetic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_config.ai_settings, "design_archetypes_path", str(SYNTHETIC))


def test_the_default_is_the_shared_library(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ai_config.ai_settings, "design_archetypes_path", "")
    assert archetypes.data_path() == archetypes.DATA
    assert archetypes.DATA.parent.name == "data" and archetypes.DATA.parent.parent.name == "app"
    raw = json.loads(archetypes.DATA.read_text(encoding="utf-8"))
    assert len(archetypes.library().archetypes) == raw["archetypeCount"] == 1240
    assert len(archetypes.categories()) == raw["categoryCount"] == 42


def test_the_shared_library_carries_no_client_deck_references(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ai_config.ai_settings, "design_archetypes_path", "")
    raw = archetypes.DATA.read_text(encoding="utf-8")
    for leak in ('"deck"', '"page"', '"referencePage"', "reference-decks", "--p0"):
        assert leak not in raw, leak
    assert all(len(a.src) == 16 for a in archetypes.library().archetypes if a.src), "src is a one-way digest"


def test_every_synthetic_category_is_one_the_slide_types_draw_from():
    mapped = {c for cats in archetypes.SLIDE_TYPE_CATEGORIES.values() for c in cats}
    assert set(archetypes.categories()) <= mapped
    assert len(archetypes.SLIDE_TYPE_CATEGORIES) == 22


def test_a_waterfall_bridge_query_finds_waterfall_archetypes():
    found = archetypes.search("waterfall bridge revenue", limit=3)
    assert found[0].category_id == "waterfall-breakdown-analysis"
    assert sum(a.category_id == "waterfall-breakdown-analysis" for a in found) == 2   # both in the library


def test_a_bridge_alone_finds_a_waterfall_through_the_synonyms():
    assert archetypes.search("EBITDA bridge", limit=1)[0].category_id == "waterfall-breakdown-analysis"


def test_slide_type_puts_that_types_categories_first():
    wanted = set(archetypes.SLIDE_TYPE_CATEGORIES["timeline"])
    found = archetypes.search("plan with phases and milestones", "timeline", limit=2)
    assert len(found) == 2 and {a.category_id for a in found} <= wanted
    org = archetypes.search("who decides", "org", limit=1)
    assert org and org[0].category_id in set(archetypes.SLIDE_TYPE_CATEGORIES["org"])


def test_limit_is_clamped_to_one_to_five_and_defaults_to_three():
    assert len(archetypes.search("process flow", limit=50)) == 4     # every match, never padding
    assert len(archetypes.search("process flow steps table chart columns", limit=50)) == archetypes.MAX_LIMIT
    assert len(archetypes.search("process flow", limit=0)) == 1
    assert len(archetypes.search("process flow", limit="lots")) == 3
    assert len(archetypes.search("process flow")) == 3


def test_results_are_diverse_rather_than_near_copies():
    found = archetypes.search("chevron process steps", limit=5)
    assert len({a.name for a in found}) == len(found)
    assert max(sum(a.category_id == c for a in found) for c in {a.category_id for a in found}) <= 3


def test_get_finds_an_archetype_by_its_id():
    a = archetypes.get("waterfall-breakdown-analysis-01")
    assert a is not None and a.name == "value-bridge-with-drivers"
    assert archetypes.get("no-such-archetype") is None and archetypes.get(None) is None


def test_the_tool_text_is_compact_and_names_what_it_found():
    text = archetypes.find_layout_reference("waterfall bridge revenue")
    assert text.count("\n1. ") == 1 and "\n2. " in text and "\n4. " not in text
    assert "Waterfall Breakdown Analysis" in text and "Purpose:" in text and "Units:" in text
    assert "×" in text and "density:" in text
    assert len(text) < 4000
    described = archetypes.describe(archetypes.search("gantt", limit=1)[0], 1)
    assert described.startswith("1. ")

    odd = archetypes.find_layout_reference("process flow", slide_type="spaceship")
    assert "not one of" in odd and "\n1. " in odd
    with pytest.raises(ValueError):
        archetypes.find_layout_reference("  ")


def test_the_setting_switches_the_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    library = {"categories": [{"id": "chevron-process-flow", "name": "Chevron Process Flow", "archetypes": [
        {"id": "chevron-process-flow-01", "name": "installed-only-recipe", "purpose": "A recipe only the installed "
         "library has.", "directive": "Five chevrons.", "units": [], "furniture": "", "density": "low"}]}]}
    path = tmp_path / "library.json"
    path.write_text(json.dumps(library), encoding="utf-8")
    monkeypatch.setattr(ai_config.ai_settings, "design_archetypes_path", str(path))
    assert archetypes.data_path() == path
    assert [a.name for a in archetypes.library().archetypes] == ["installed-only-recipe"]
    monkeypatch.setattr(ai_config.ai_settings, "design_archetypes_path", str(SYNTHETIC))
    assert len(archetypes.library().archetypes) == 24
