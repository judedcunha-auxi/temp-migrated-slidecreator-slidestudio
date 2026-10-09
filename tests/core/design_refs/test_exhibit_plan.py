"""`design_refs.exhibit_plan`: the comparison read from a title, and the plan for its exhibit.

Ported from Slide Studio `server/tests/test_exhibit_plan.py`. The design-chat tests (the tool offered and
answered in a turn, the review cap, the prompt wording) belong to the design loop (`tests/core/design/`).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.design_refs import exhibit_plan as ep

TITLES = {
    "ranking": "Baird's peer set outpaces on digital and operating leverage",
    "time": "AUM expanded $10.3B in 2024, with net new assets the main source of growth",
    "bridge": "EBITDA bridge: price and mix more than offset input costs from FY24 to FY25",
    "sequence": "A sequenced three-phase roadmap delivers full transformation by Q4 2027",
    "evaluation": "Acquire is the preferred option on value and speed",
    "kpi": "Baird exceeded target on six of eight KPIs in 2024",
    "market": "The HNW segment is a $4T+ addressable prize",
    "valuation": "The USD 45.00 offer implies a 24% premium to the undisturbed price",
    "deviation": "Operating expense ratio sits 700 bps above budget",
    "part_to_whole": "Retail accounts for 42% of revenue",
    "relationship": "Advisor tenure drives client retention",
    "structure": "The steering committee owns every go/no-go decision",
    "spatial": "Three regions carry 70% of the footprint",
    "distribution": "Margins range from 4% to 19% across the portfolio",
    "statement": "Customer satisfaction is 92%",
}


@pytest.mark.parametrize("kind,title", list(TITLES.items()))
def test_the_comparison_is_read_from_the_title(kind, title):
    assert ep.classify(title)[0] == kind


def test_a_declared_framework_or_slide_type_wins_over_keywords():
    assert ep.classify("Revenue grew 12% a year", framework="Bridge chart")[0] == "bridge"
    assert ep.classify("Three options, one clear winner", slide_type="process")[0] == "evaluation", \
        "a slide type only wins when the title does not contradict it"
    assert ep.classify("The next 12 months", slide_type="timeline")[0] == "sequence"


def test_every_comparison_type_has_a_complete_plan():
    for kind, plan in ep.PLANS.items():
        assert plan.exhibit and plan.insight and plan.support and plan.takeaway, kind
        assert plan.fallback, f"{kind} needs a fallback for when its component is not installed"


def test_the_plan_names_one_exhibit_one_insight_one_support_and_the_takeaway():
    text = ep.plan_exhibit(TITLES["time"], facts=["182", "204", "231", "248", "279", "312"])
    for part in ("- Comparison: change over time", "- Main exhibit: column_chart", '"cagr"',
                 "- Insight device (one, on the exhibit):", "- Supporting device (one, in the side panel):",
                 "- Takeaway (exactly one unit):", "one stated scale",
                 'data-exhibit-plan="time;'):
        assert part in text, part
    assert "trend with CAGR:" in text, "the finance recipe that left the prompt rides along"


def test_direction_and_the_missing_number_are_called_out():
    text = ep.plan_exhibit("Peers outpace us on digital and cost", lower_is_better=["Operating expense ratio"])
    assert "Direction: for Operating expense ratio lower is better" in text
    assert "colour a gap by good/bad, not by sign" in text
    assert "The title carries no number" in text
    assert "The title carries no number" not in ep.plan_exhibit(TITLES["part_to_whole"])
    # a cost metric in the facts triggers the rule without being told
    assert "lower is better" in ep.plan_exhibit("We trail peers by 27 points", facts=["78% cost ratio vs 71%"])


def test_the_recipes_that_left_the_prompt_come_back_on_demand():
    text = ep.plan_exhibit("Valuation summary", framework="football field")
    assert "football field:" in text and "`referenceLines`" in text
    assert "trading / transaction comps:" in text and "Median and Mean" in text
    assert ep.recipe("weighted scoring matrix").startswith("criteria rows")
    assert ep.recipe("bubble chart").startswith("NOT native")


def test_a_component_that_is_not_installed_is_replaced_by_its_fallback(monkeypatch):
    monkeypatch.setattr(ep, "_components", SimpleNamespace(names=lambda: ["column_chart"]))
    text = ep.plan_exhibit(TITLES["ranking"])
    assert "- Main exhibit: column_chart (orientation bar" in text, "bar_table missing → the fallback"
    assert "the `keyed_markers` component is not installed" in text
    monkeypatch.setattr(ep, "_components", SimpleNamespace(names=lambda: ["bar_table", "keyed_markers"]))
    text = ep.plan_exhibit(TITLES["ranking"])
    assert "- Main exhibit: bar_table — get_component params skeleton:" in text
    assert "not installed" not in text


def test_an_empty_title_is_an_error():
    with pytest.raises(ValueError):
        ep.plan_exhibit("  ")


def test_skeletons_come_from_the_installed_component_and_carry_no_demo_content():
    from app.core.design_refs import components

    for name in ("bar_table", "bullet_rows", "delta_badge", "keyed_markers"):
        if name not in components.names():
            pytest.skip(f"{name} is not installed yet")
    assert '"good_when": "up"' in ep.skeleton("delta_badge"), "the real parameter name, not the research draft's"
    assert '"lower_is_better"' in ep.skeleton("bar_table") and '"benchmark"' in ep.skeleton("bar_table")
    assert '"number_format": "#,##0;(#,##0)"' in ep.skeleton("waterfall"), "formats survive"
    for name in components.names():
        text = ep.skeleton(name)
        assert "Target Co" not in text and "EBITDA" not in text and "Procurement" not in text, name
    assert ep.skeleton("no_such_component", "fallback") == "fallback"
