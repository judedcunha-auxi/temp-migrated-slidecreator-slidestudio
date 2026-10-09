"""The shared data (app/data), the vocabulary, the archetype loader and the RTL mirroring.

Ported: Darwin `frameworks.test.ts` (registry invariants), `slideTypes` lists, `rtlText.test.ts`; Slide Studio's
catalog-size check. Plus the "no client material" rule for the archetype data."""

from __future__ import annotations

import json
import re

from app.core.storyline import archetypes, rtl, vocabulary
from app.core.storyline.vocabulary import (
    CHART_FRAMEWORKS,
    SLIDE_TYPES,
    canonical_framework,
    framework_aliases,
    framework_names,
    normalize_framework_name,
)


def test_the_library_has_65_frameworks_in_darwins_order_and_284_aliases():
    names = framework_names()
    assert len(names) == 65 and len(set(names)) == 65
    assert names[0] == "2x2 matrix" and names[-1] == "decision flowchart"
    assert len(framework_aliases()) == 284


def test_every_name_and_alias_resolves_to_a_canonical_name():
    names = set(framework_names())
    for name in names:
        assert canonical_framework(name) == name
    for alias, target in framework_aliases().items():
        assert target in names, f"alias {alias!r} -> unknown {target!r}"
        assert canonical_framework(alias) == target


def test_normalisation_is_tolerant_and_collision_free():
    assert canonical_framework("2×2") == "2x2 matrix"
    assert canonical_framework("Porter’s Five Forces.") == "Porter's five forces"
    assert canonical_framework("value-chain") == "value chain"
    assert canonical_framework("no such framework") is None
    normalised = [normalize_framework_name(n) for n in framework_names()]
    assert len(set(normalised)) == len(normalised)


def test_chart_frameworks_are_library_names():
    assert CHART_FRAMEWORKS <= set(framework_names())


def test_the_22_slide_types():
    assert len(SLIDE_TYPES) == 22
    assert vocabulary.FURNITURE <= set(SLIDE_TYPES)
    assert vocabulary.NO_SECTION_TYPES == vocabulary.FURNITURE - {"divider"}


def test_the_archetype_library_and_its_catalog():
    lib = archetypes.library()
    assert len(lib.archetypes) == 1240 and lib.category_count == 42
    catalog = archetypes.catalog_text().splitlines()
    assert len(catalog) == 126  # 3 per category
    assert re.match(r"^[a-z0-9-]+-\d{2} \| [^|]+ \| [^:]+: .+", catalog[0])


def test_the_archetype_data_carries_no_client_material():
    raw = json.loads(archetypes.ARCHETYPES_FILE.read_text(encoding="utf-8"))
    for category in raw["categories"]:
        for a in category["archetypes"]:
            assert not {"deck", "page", "referencePage"} & set(a), a["id"]
            assert re.fullmatch(r"[a-z0-9-]+-\d{2,3}", a["id"]), a["id"]
            assert "--p" not in a["id"]


def test_a_darwin_archetype_id_resolves_by_its_digest_only(monkeypatch):
    first = archetypes.library().archetypes[0]
    assert archetypes.get(first.id) is first
    assert re.fullmatch(r"[0-9a-f]{16}", first.src)
    # Darwin's source ids name client decks, so none is in this repo: a synthetic one stands in.
    legacy = "synthetic-sample-deck--p007"
    fake = archetypes.Archetype(id="demo-01", category_id="demo", category="Demo", name="demo", purpose="p",
                                directive="d", density="low", src=archetypes.source_digest(legacy))
    monkeypatch.setattr(archetypes, "library", lambda: archetypes._Library(
        archetypes=(fake,), by_id={fake.id: fake}, by_src={fake.src: fake}, category_count=1))
    assert archetypes.canonical_id(legacy) == "demo-01"
    assert archetypes.canonical_id(f"  {legacy} ") == "demo-01"
    assert archetypes.get("no-such-deck--p999") is None and archetypes.get("") is None


def test_an_archetype_directive_is_mirrored_for_arabic_only():
    a = next(x for x in archetypes.library().archetypes if re.search(r"\bleft\b", x.directive))
    assert archetypes.directive(a.id) == a.directive
    mirrored = archetypes.directive(a.id, "ar")
    assert mirrored is not None and mirrored != a.directive and "right" in mirrored
    assert archetypes.directive("nope") is None


# ------------------------------------------------------------------------------- rtl (Darwin rtlText.test.ts)
def test_rtl_flips_compound_phrases_without_corrupting_words():
    assert rtl.mirror_directional_text("left-to-right chevron stages") == "right-to-left chevron stages"


def test_rtl_swaps_bare_left_and_right_as_whole_words():
    assert rtl.mirror_directional_text("a left rail and a right panel") == "a right rail and a left panel"
    assert rtl.mirror_directional_text("leftover copyright") == "leftover copyright"


def test_rtl_does_not_double_swap():
    assert rtl.mirror_directional_text("right-hand side") == "left-hand side"
    assert rtl.mirror_directional_text("left-hand side") == "right-hand side"


def test_rtl_flips_corners_and_alignment():
    assert rtl.mirror_directional_text("bottom-left legend, top-right logo") == "bottom-right legend, top-left logo"
    assert rtl.mirror_directional_text("left-aligned labels") == "right-aligned labels"


def test_rtl_keeps_case_insensitive_matches_and_is_idempotent_on_rtl_text():
    assert rtl.mirror_directional_text("Left rail") == "right rail"
    assert rtl.mirror_directional_text("right-to-left flow") == "right-to-left flow"


def test_rtl_mirrors_a_gantt_directive_end_to_end():
    text = "Activity list on the left with a timeline on the right, originating from the bottom-left."
    assert rtl.mirror_directional_text(text) == (
        "Activity list on the right with a timeline on the left, originating from the bottom-right.")


def test_rtl_leaves_english_untouched():
    assert rtl.for_language("left rail", "en") == "left rail"
    assert rtl.for_language("left rail", None) == "left rail"
    assert rtl.for_language("left rail", "ar") == "right rail"
