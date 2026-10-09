"""Coerce, don't reject. Golden tests for every rule: Darwin `frameworkGuard.test.ts` and `_tests/dividers.test.ts`,
Slide Studio `test_storyline.py` (repair part), per density."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.storyline import archetypes
from app.core.storyline.repair import (
    COERCIONS,
    TYPE_DEFAULT_FRAMEWORK,
    coerce_framework,
    insert_dividers,
    repair,
)
from app.core.storyline.validation import parse_dense, parse_standard
from app.core.storyline.vocabulary import CHART_FRAMEWORKS, canonical_framework
from tests.core.storyline.conftest import CHART, answer, furniture, long_deck, slide


def standard(*slides: dict[str, Any]) -> Any:
    return parse_standard(answer(*slides))


def dense(*slides: dict[str, Any]) -> Any:
    return parse_dense(answer(*slides))


# --------------------------------------------------------------------------------------- coerce_framework
@pytest.mark.parametrize("asked, slide_type, framework, warned", [
    ("2x2 matrix", "framework", "2x2 matrix", False),                      # exact
    ("gantt", "timeline", "gantt chart", False),                            # alias: same visual, silent
    ("2×2", "framework", "2x2 matrix", False),
    ("swot", "framework", "SWOT analysis", False),
    ("Spider Chart", "comparison", "weighted scoring matrix", True),         # coercion table
    ("radar chart", "comparison", "weighted scoring matrix", True),
    ("Mind Map", "framework", "hub-and-spoke", True),
    ("pie chart", "data", "100% stacked bar chart", True),
    ("speedometer", "kpi", "KPI tiles", True),
    ("3D exploded pyramid", "framework", "pyramid", True),                   # 3D stripped, then fuzzy
    ("3D value chain", "framework", "value chain", True),                    # 3D stripped, then exact
    ("isometric funnel", "market", "funnel", True),
    ("3D pie chart", "data", "100% stacked bar chart", True),                # stripped, then coercion
    ("exploded pyramid", "framework", "pyramid", True),                      # fuzzy (Jaccard >= 0.5)
    ("chevron process", "framework", "chevron process flow", True),
    ("zzz plorp", "data", "100% stacked bar chart", True),                   # nothing matches: the type default
    ("Kano model", "framework", "icon card grid", True),
    ("line chart", "data", "100% stacked bar chart", True),                  # generic token alone: no fuzzy
    ("line chart", "process", "chevron process flow", True),
])
def test_frameworks_are_coerced_onto_the_library(asked: str, slide_type: str, framework: str, warned: bool):
    got, note = coerce_framework(asked, slide_type)
    assert got == framework
    assert bool(note) is warned
    if note:
        assert asked in note


def test_an_empty_framework_stays_empty_unless_filled_dense():
    assert coerce_framework("", "framework") == ("", None)
    assert coerce_framework("  ", "framework") == ("", None)
    got, note = coerce_framework("", "framework", fill_empty=True)
    assert got == "icon card grid" and note


def test_an_unmatched_name_passes_through_for_a_type_with_no_default():
    assert coerce_framework("zzz plorp", "quote") == ("zzz plorp", None)


def test_every_table_value_is_a_real_library_framework():
    for name in [*COERCIONS.values(), *TYPE_DEFAULT_FRAMEWORK.values(), *CHART_FRAMEWORKS]:
        assert canonical_framework(name) == name


# ---------------------------------------------------------------------------------------- repair: standard
def test_repair_coerces_frameworks_with_slide_numbered_warnings():
    st, warnings = repair(standard(slide(1, framework="gantt chart", type="timeline"),
                                   slide(2, framework="radar chart", type="comparison")))
    assert [s.framework for s in st.slides] == ["gantt chart", "weighted scoring matrix"]
    assert len(warnings) == 1 and warnings[0].startswith("Slide 2:")


def test_standard_strips_chart_data_from_a_framework_that_is_not_a_chart():
    st, warnings = repair(standard(slide(1, framework="spider chart", type="comparison", chartData=CHART)))
    assert st.slides[0].framework == "weighted scoring matrix" and st.slides[0].chart_data is None
    assert any("chartData" in w for w in warnings)


def test_standard_keeps_chart_data_on_a_chart_framework():
    st, warnings = repair(standard(slide(1, framework="gantt chart", type="timeline", chartData=CHART)))
    assert st.slides[0].chart_data is not None and warnings == []


def test_unknown_archetype_ids_are_dropped_and_real_ones_kept():
    real = archetypes.catalog(1)[0].id
    st, warnings = repair(standard(slide(1, archetypeId="no-such-deck--p999"), slide(2, archetypeId=real)))
    assert st.slides[0].archetype_id is None and st.slides[1].archetype_id == real
    assert len(warnings) == 1 and "no-such-deck--p999" in warnings[0]


def test_furniture_and_its_pseudo_framework_are_left_alone():
    st, warnings = repair(standard(slide(3, type="divider", framework="section divider", bullets=())))
    assert st.slides[0].framework == "section divider" and warnings == []


# ------------------------------------------------------------------------------------------ repair: dense
def test_dense_keeps_chart_data_on_any_body_slide():
    st, _ = repair(dense(slide(framework="icon card grid", chartData=CHART)), density="dense")
    assert st.slides[0].chart_data is not None


def test_dense_furniture_keeps_no_chart_and_no_archetype():
    real = archetypes.catalog(1)[0].id
    st, warnings = repair(dense(slide(type="title", framework="", bullets=(), chartData=CHART, archetypeId=real)),
                          density="dense")
    s = st.slides[0]
    assert s.chart_data is None and s.archetype_id is None and s.framework == ""
    assert any("dropped chart data" in w for w in warnings)


def test_dense_drops_unknown_and_repeated_archetypes():
    real = archetypes.library().archetypes[0].id
    st, warnings = repair(dense(slide(1, archetypeId="nope--p001"), slide(2, archetypeId=real),
                                slide(3, archetypeId=real), slide(4), slide(5), slide(6, archetypeId=real)),
                          density="dense", dividers=False)
    assert [s.archetype_id for s in st.slides] == [None, real, None, None, None, real]
    assert any("unknown archetypeId" in w for w in warnings)


def test_dense_thin_body_slide_is_a_warning_not_a_rejection():
    st, warnings = repair(dense(slide(bullets=("only one",))), density="dense")
    assert len(st.slides) == 1 and any("only 1 bullet" in w for w in warnings)


def test_dense_fills_an_empty_framework_with_the_type_default():
    st, warnings = repair(dense(slide(type="kpi", framework="")), density="dense")
    assert st.slides[0].framework == "KPI tiles" and warnings


def test_dense_agenda_without_items_lists_the_sections():
    st, _ = repair(dense(*long_deck()), density="dense")
    assert st.slides[1].type == "agenda" and st.slides[1].bullets == ["Diagnosis", "Options", "Plan"]


def test_max_slides_keeps_the_first_n_with_a_warning():
    st, warnings = repair(standard(*(slide(i) for i in range(1, 6))), max_slides=3)
    assert len(st.slides) == 3 and "kept the first 3" in warnings[0]


# --------------------------------------------------------------------------------------------- dividers
def _deck(*specs: tuple[int, str, str]) -> list[Any]:
    out = []
    for n, t, section in specs:
        raw = furniture(n, t, section) if t in ("title", "agenda", "closing", "navigator", "divider") \
            else slide(n, type=t, section=section, title=f"S{n}")
        out.append(raw)
    return standard(*out).slides


def test_a_divider_opens_each_section_after_the_first():
    out = insert_dividers(_deck((1, "title", "Opening"), (2, "agenda", "Opening"), (3, "market", "Market"),
                                (4, "data", "Market"), (5, "framework", "Moat"), (6, "benchmark", "Moat"),
                                (7, "data", "Valuation"), (8, "process", "Deal"), (9, "scorecard", "Deal"),
                                (10, "closing", "Recommendation")))
    dividers = [s for s in out if s.type == "divider"]
    assert [d.title for d in dividers] == ["Moat", "Valuation", "Deal"]
    assert [s.number for s in out] == list(range(1, len(out) + 1))
    assert dividers[0].section == "Moat" and dividers[0].bullets == []
    assert dividers[0].framework == "section divider"


def test_no_dividers_under_eight_slides_or_in_one_section():
    short = _deck((1, "title", "A"), (2, "data", "A"), (3, "data", "B"), (4, "framework", "B"), (5, "closing", "C"))
    assert len(insert_dividers(short)) == 5
    one = _deck(*((i, "framework", "Only") for i in range(1, 10)))
    assert len(insert_dividers(one)) == 9


def test_never_a_divider_before_the_first_content_section():
    out = insert_dividers(_deck((1, "title", "Intro"), *((i, "framework", "Intro") for i in range(2, 6)),
                                *((i, "data", "Next") for i in range(6, 10))))
    dividers = [s for s in out if s.type == "divider"]
    assert [d.title for d in dividers] == ["Next"]
    assert out[0].type == "title" and out[1].type == "framework"


def test_dividers_are_idempotent():
    out = insert_dividers(_deck((1, "title", "A"), *((i, "framework", "A") for i in range(2, 6)),
                                (6, "divider", "B"), *((i, "data", "B") for i in range(7, 10))))
    assert sum(1 for s in out if s.type == "divider") == 1
    again = insert_dividers(insert_dividers(standard(*long_deck()).slides))
    assert len(again) == 11


def test_repair_inserts_dividers_unless_told_not_to():
    st, _ = repair(standard(*long_deck()))
    kinds = [(s.type, s.section) for s in st.slides]
    assert kinds[4] == ("divider", "Options") and kinds[7] == ("divider", "Plan")
    assert len(repair(standard(*long_deck()), dividers=False)[0].slides) == 9
