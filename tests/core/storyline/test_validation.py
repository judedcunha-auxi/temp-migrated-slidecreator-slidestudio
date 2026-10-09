"""Reading a storyline: standard (Darwin `storylineSchema.test.ts`), dense (Slide Studio `test_parse_salvages_what_it_can`),
and the `/api/storyline` request checks (Darwin `_tests/storyline.test.ts`, contract api-storyline.json)."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.storyline.validation import (
    INCOMPLETE_STORYLINE,
    StorylineRequestError,
    StorylineValidationError,
    check_storyline_request,
    parse_dense,
    parse_standard,
    parse_storyline,
)
from app.core.storyline.vocabulary import SLIDE_TYPES
from tests.core.storyline.conftest import CHART, answer, slide

VALID = answer(slide(1, title="T1", section=None))


def _issues(raw: Any) -> list[str]:
    with pytest.raises(StorylineValidationError) as caught:
        parse_standard(raw)
    assert str(caught.value) == INCOMPLETE_STORYLINE
    return caught.value.issues


# ------------------------------------------------------------------------------------------- standard
def test_a_well_formed_storyline_is_accepted():
    st = parse_standard(VALID)
    assert st.slides[0].title == "T1" and st.slides[0].section is None


def test_slides_with_fewer_than_three_bullets_are_rejected():
    assert any("3-4 bullets" in i for i in _issues(answer(slide(bullets=("only one",)))))


def test_more_than_four_bullets_is_rejected_in_standard_mode():
    assert any("at most 4" in i for i in _issues(answer(slide(bullets=("a", "b", "c", "d", "e")))))


def test_furniture_needs_no_bullets():
    st = parse_standard(answer(slide(type="title", bullets=()), slide(2, type="divider", bullets=())))
    assert [s.type for s in st.slides] == ["title", "divider"]


def test_an_empty_slide_list_and_a_missing_title_are_rejected():
    assert _issues({"presentationTitle": "x", "slides": []}) == ["slides: at least one slide"]
    assert "presentationTitle: a non-empty string" in _issues({"slides": [slide()]})
    assert _issues("not an object") == ["storyline: expected an object"]


def test_type_defaults_to_framework_and_an_unknown_type_is_rejected():
    raw = slide()
    del raw["type"]
    assert parse_standard(answer(raw)).slides[0].type == "framework"
    assert parse_standard(answer(slide(type="data"))).slides[0].type == "data"
    assert any(".type" in i for i in _issues(answer(slide(type="bogus"))))


def test_every_calibrated_type_is_accepted():
    for t in SLIDE_TYPES:
        bullets: tuple[str, ...] = () if t in ("title", "agenda", "divider", "navigator", "closing") else ("a", "b", "c")
        assert parse_standard(answer(slide(type=t, bullets=bullets))).slides[0].type == t


def test_the_slide_number_must_be_a_positive_integer():
    for bad in (0, -1, 1.5, "1", True):
        assert any(".number" in i for i in _issues(answer(slide(n=bad))))  # type: ignore[arg-type]
    assert parse_standard(answer(slide(n=2.0))).slides[0].number == 2  # type: ignore[arg-type]


def test_required_text_fields_must_be_non_empty():
    for key in ("title", "framework", "description"):
        assert any(f".{key}" in i for i in _issues(answer(slide(**{key: ""}))))


def test_chart_data_is_checked():
    assert parse_standard(answer(slide(framework="gantt chart", chartData=CHART))).slides[0].chart_data is not None
    bad_values = {"categories": ["a"], "series": [{"name": "s", "values": ["12"]}]}
    assert any("values" in i for i in _issues(answer(slide(chartData=bad_values))))
    assert any("categories" in i for i in _issues(answer(slide(chartData={"categories": [], "series": []}))))


def test_unknown_keys_are_dropped_and_wire_omits_unset_optionals():
    st = parse_standard(answer(slide(section=None, extra="ignored", prompt="client-side")))
    wire = st.slides[0].wire()
    assert "extra" not in wire and "prompt" not in wire
    assert "section" not in wire and "chartData" not in wire and "archetypeId" not in wire
    assert None not in wire.values()


def test_issues_name_every_problem_at_once():
    issues = _issues(answer(slide(1, title=""), slide(2, bullets=("x",))))
    assert any(i.startswith("slides[0].title") for i in issues) and any(i.startswith("slides[1].bullets") for i in issues)


# ---------------------------------------------------------------------------------------------- dense
def test_dense_salvages_what_it_can():
    st = parse_dense({"slides": [
        {"title": "  A  conclusion  ", "type": "nonsense", "bullets": ["x", "", 3] + ["y"] * 9},
        {"type": "data"}, "junk",
    ]})
    assert st.presentation_title == "A conclusion"
    s = st.slides[0]
    assert s.type == "framework" and s.bullets[:2] == ["x", "3"] and len(s.bullets) == 6
    assert len(st.slides) == 1 and s.number == 1
    with pytest.raises(StorylineValidationError):
        parse_dense({"slides": [{"type": "data"}]})


def test_dense_coerces_chart_values_and_keeps_the_executive_summary():
    raw = answer(slide(chartData={"categories": ["a", "b"], "series": [{"name": "s", "values": [1, "2.5", "n/a"]}]}),
                 executiveSummary="The argument.")
    st = parse_dense(raw)
    assert st.slides[0].chart_data is not None and st.slides[0].chart_data.series[0].values == [1, 2.5]
    assert st.executive_summary == "The argument."


def test_dense_accepts_up_to_six_bullets_that_standard_rejects():
    raw = answer(slide(bullets=tuple("abcdef")))
    assert len(parse_storyline(raw, "dense").slides[0].bullets) == 6
    with pytest.raises(StorylineValidationError):
        parse_storyline(raw, "standard")


# ------------------------------------------------------------------------------------ the request checks
@pytest.mark.parametrize("body", [{}, {"topic": ""}, {"topic": None}, {"topic": 0}, None, [], "x"])
def test_a_topic_is_required(body: Any):
    with pytest.raises(StorylineRequestError, match="Topic is required"):
        check_storyline_request(body)


@pytest.mark.parametrize("value", [0, -3, None, False, "", "0", 0.5, []])
def test_slides_must_be_at_least_one_as_javascript_compares(value: Any):
    with pytest.raises(StorylineRequestError, match="Slides must be at least 1"):
        check_storyline_request({"topic": "x", "numSlides": value})


@pytest.mark.parametrize("value", [1, 500, "abc", True, "12", [3]])
def test_large_missing_and_non_numeric_slide_counts_pass(value: Any):
    check_storyline_request({"topic": "x", "numSlides": value})
    check_storyline_request({"topic": "x"})
    check_storyline_request({"topic": 42})  # any truthy topic passes
