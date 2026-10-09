"""`get_exemplars`: layout pictures searched by type, framework and query.

No picture ships with the service (decision D3), so every test builds a small synthetic library of its
own in a temp folder (`DESIGN_EXEMPLARS_DIR`, patched on the settings object) and never depends on what
the machine has installed.

Ported from Slide Studio `server/tests/test_exemplars.py`. The two design-chat tests (the tool offered
only when pictures are installed, a turn returning them) belong to the design loop (`tests/core/design/`).
"""
from __future__ import annotations

import base64
import io
import json

import pytest
from PIL import Image

from app.config import ai as ai_config


def test_unset_means_no_exemplars_and_no_tool(monkeypatch):
    from app.core.design_refs import exemplars

    monkeypatch.setattr(ai_config.ai_settings, "design_exemplars_dir", "")
    assert exemplars.directory() is None
    assert not exemplars.available() and exemplars.library() == ()


ROWS = [
    {"id": "deck_001", "type": "timeline", "frameworks": ["gantt chart"], "archetype_category": "gantt-timeline-roadmap",
     "density": "high", "layout": "Left activity rail, monthly columns, bars ending in milestone diamonds."},
    {"id": "deck_002", "type": "timeline", "frameworks": ["gantt chart"], "archetype_category": "gantt-timeline-roadmap",
     "density": "high", "layout": "Same gantt again with shaded quarters."},
    {"id": "other_001", "type": "timeline", "frameworks": ["milestone timeline"],
     "archetype_category": "project-charter-dual-timeline", "density": "medium",
     "layout": "Dated milestones alternating above and below one axis line."},
    {"id": "other_002", "type": "framework", "frameworks": ["hub-and-spoke"], "archetype_category": "hub-spoke-ecosystem-map",
     "density": "medium", "layout": "Central hub circle with six stakeholder spokes."},
    {"id": "third_001", "type": "table", "frameworks": ["weighted scoring matrix"],
     "archetype_category": "weighted-scoring-matrix", "density": "high",
     "layout": "Options as columns scored against weighted criteria rows with a total row."},
    {"id": "missing_001", "type": "table", "frameworks": [], "archetype_category": None, "density": "low",
     "layout": "Listed but its picture is missing."},
]


def _install(rows, tmp_path, monkeypatch):
    from app.core.design_refs import exemplars

    folder = tmp_path / "exemplars"
    folder.mkdir()
    for r in rows:
        if r["id"] != "missing_001":
            Image.new("RGB", (1280, 720), (20, 60, 120)).save(folder / f"{r['id']}.jpg", "JPEG")
    (folder / "exemplars.json").write_text(json.dumps({"exemplars": rows}), encoding="utf-8")
    monkeypatch.setattr(ai_config.ai_settings, "design_exemplars_dir", str(folder))
    exemplars._load.cache_clear()
    exemplars._image.cache_clear()
    return exemplars


@pytest.fixture
def library(tmp_path, monkeypatch):
    yield _install(ROWS, tmp_path, monkeypatch)
    library_ = __import__("app.core.design_refs.exemplars", fromlist=["x"])
    library_._load.cache_clear()
    library_._image.cache_clear()


def _option(sid, layout, composition=None, reference=False, type_="comparison"):
    row = {"id": sid, "type": type_, "frameworks": ["options evaluation matrix"],
           "archetype_category": "comparative-option-cards", "density": "high", "layout": layout}
    if composition is not None:
        row["composition"] = composition
    if reference:
        row["reference"] = True
    return row


COMPOSITE_ROWS = [
    _option("aaa_001", "Two option columns side by side."),
    _option("bbb_001", "Two option columns side by side.", ["two option columns", "verdict row"]),
    _option("ccc_001", "Two option columns side by side.", ["two option columns", "verdict row"], reference=True),
    {"id": "ddd_001", "type": "table", "frameworks": ["checkmark presence matrix"], "archetype_category": None,
     "density": "high", "layout": "Option columns with tick marks in every cell, the tick marks totalled."},
    {"id": "eee_001", "type": "framework", "frameworks": ["hub-and-spoke"], "archetype_category": None,
     "density": "medium", "layout": "Central hub with spokes.", "composition": ["hub diagram", "legend", "KPI strip"],
     "reference": True},
]


@pytest.fixture
def composite(tmp_path, monkeypatch):
    yield _install(COMPOSITE_ROWS, tmp_path, monkeypatch)
    library_ = __import__("app.core.design_refs.exemplars", fromlist=["x"])
    library_._load.cache_clear()
    library_._image.cache_clear()


def test_the_library_loads_only_rows_whose_picture_exists(library):
    assert library.available()
    assert [e.id for e in library.library()] == ["deck_001", "deck_002", "other_001", "other_002", "third_001"]


def test_a_framework_narrows_the_pool_and_picks_spread_over_decks(library):
    picks = library.search(framework="gantt chart", limit=3)
    assert all("gantt chart" in e.frameworks for e in picks)
    # the pool is two gantts from one deck: the second is only used because nothing else is left
    assert {e.id for e in picks} == {"deck_001", "deck_002"}


def test_a_slide_type_narrows_and_the_query_ranks_within_it(library):
    picks = library.search("milestones above and below an axis", slide_type="timeline", limit=2)
    assert picks[0].id == "other_001" and all(e.type == "timeline" for e in picks)
    assert picks[1].deck != picks[0].deck


def test_a_query_that_matches_is_not_padded_with_unrelated_pictures(library):
    assert [e.id for e in library.search("hub with stakeholder spokes", limit=3)] == ["other_002"]


def test_a_type_with_too_few_matches_is_topped_up_from_query_matches_elsewhere(library):
    picks = library.search("hub with stakeholder spokes and weighted criteria", slide_type="table", limit=3)
    assert picks[0].type == "table" and "other_002" in [e.id for e in picks]
    assert len(picks) == 2, "only pictures the query matches, never padding"


def test_the_answer_is_pictures_first_then_the_captions_and_the_rule(library):
    blocks = library.get_exemplars("weighted criteria scoring", limit=1)
    assert [b["type"] for b in blocks] == ["image", "text"]
    source = blocks[0]["source"]
    assert source["type"] == "base64" and source["media_type"] == "image/jpeg"
    with Image.open(io.BytesIO(base64.b64decode(source["data"]))) as im:
        assert im.width == library.MAX_W
    text = blocks[1]["text"]
    assert text.startswith("1. table - weighted scoring matrix (high density): Options as columns")
    assert "never copy" in text.lower() and "never embed" in text.lower()


def test_limits_are_clamped_and_an_empty_ask_is_an_error(library):
    assert len(library.search("timeline", limit=99)) <= library.MAX_LIMIT
    assert len(library.search("timeline", limit="lots")) == library.DEFAULT_LIMIT
    with pytest.raises(ValueError):
        library.get_exemplars(None, "  ", None)


def test_without_a_manifest_the_library_is_empty(tmp_path, monkeypatch):
    from app.core.design_refs import exemplars

    monkeypatch.setattr(ai_config.ai_settings, "design_exemplars_dir", str(tmp_path / "nowhere"))
    exemplars._load.cache_clear()
    try:
        assert not exemplars.available() and exemplars.library() == ()
    finally:
        exemplars._load.cache_clear()


def test_composition_and_reference_are_optional_fields(composite, library=None):
    by_id = {e.id: e for e in composite.library()}
    assert by_id["aaa_001"].composition == () and not by_id["aaa_001"].composite
    assert by_id["bbb_001"].composition == ("two option columns", "verdict row") and by_id["bbb_001"].composite
    assert by_id["ccc_001"].reference and not by_id["bbb_001"].reference
    assert by_id["aaa_001"].prior == 1.0 < by_id["bbb_001"].prior < by_id["ccc_001"].prior


def test_the_composition_words_are_searchable(composite):
    # "verdict" appears only in the composition lists, never in a layout sentence
    assert {e.id for e in composite.search("verdict row", limit=3)} == {"bbb_001", "ccc_001"}


def test_the_caption_says_what_the_slide_combines(composite):
    text = composite.get_exemplars("verdict row", limit=3)[-1]["text"]
    assert "Combines: two option columns + verdict row." in text
    assert "House quality bar" in text and text.count("House quality bar") == 1  # only the reference row
    assert "never copy" in text.lower()


def test_a_single_unit_caption_has_no_combines_clause(library):
    assert "Combines" not in library.get_exemplars("weighted criteria scoring", limit=1)[-1]["text"]


def test_the_boost_breaks_ties_towards_composite_and_reference_slides(composite):
    words = composite.archetypes.tokens("two option columns side by side")
    scores = composite._bm25(words, tuple(e for e in composite.library() if e.type == "comparison"))
    assert scores["ccc_001"] > scores["bbb_001"] > scores["aaa_001"] > 0
    picks = composite.search("two option columns side by side", slide_type="comparison", limit=3)
    assert [e.id for e in picks] == ["ccc_001", "bbb_001", "aaa_001"]


def test_the_boost_never_beats_a_clearly_better_match_nor_admits_an_irrelevant_one(composite):
    picks = composite.search("option columns with tick marks", limit=3)
    assert picks[0].id == "ddd_001", "a much stronger single-unit match still wins"
    assert "eee_001" not in [e.id for e in picks], "a reference composite the query misses never enters"


def test_with_no_query_words_the_prior_is_the_tie_break(composite):
    picks = composite.search(slide_type="comparison", limit=1)
    assert picks[0].id == "ccc_001"
