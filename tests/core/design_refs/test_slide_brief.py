"""`design_refs.slide_brief`: the per-slide design brief, validated and grounded, deterministic and free.

No API is called: `draft_brief` takes the model call as an argument and these tests pass a stub. The fixture
is a utility-valuation brief whose peer figures are unknown (`[verify]`) or estimated. Names are fictional.

Ported from Slide Studio `server/tests/test_slide_brief.py` (the design_refs half). The design-chat half
(write_brief offered and answered in a turn, the nudge, the grounding check after a save, the critic's
context) belongs to the design loop and lives with it (`tests/core/design/`).
"""
from __future__ import annotations

import copy
import json

import pytest

from app.core.design_refs import slide_brief as sb

#: The storyline slide spec mode hands over (Slide Studio `test_compat_spec.SLIDE` / `DECK`), normalised the way
#: the pipeline normalises it, so the prefill tests need no pipeline.
SPEC = {
    "slide": {"title": "Net new assets beat the peer median by 340bp", "type": "data", "section": "Results",
              "framework": "waterfall chart", "description": "Shows where the growth came from.",
              "bullets": ["AUM $57.1B to $67.4B", "Net new assets $8.2B", "Market uplift $2.1B"],
              "chartData": {"categories": ["2023", "NNA", "Market", "2024"],
                            "series": [{"name": "AUM", "values": [57.1, 8.2, 2.1, 67.4]}]},
              "archetypeId": "waterfall-breakdown-analysis-01"},
    "deck": {"presentationTitle": "Performance review", "audience": "Executive committee",
             "keyMessages": ["AUM grew 18% YoY"]},
}


REQUEST = (
    "Build ONE slide for the CFO of Acme Power. ACTION TITLE (use as written) \"The market values Acme "
    "Energy below the book value of its own assets, driven by returns below the cost of equity rather than by "
    "scale\". LEAD METRIC BAND: Price / book: ~0.3x; EV / EBITDA: ~7.0x; Dividend yield: ~4.3%; P/E: ~15.8x. "
    "MULTIPLES TABLE rows: Acme Power | Country X | Integrated, regulated | ~15% | ~0.3x | ~7.0x | ~4.3%; "
    "Peer A | Country X | Developer / IPP | fill; Peer B | Country Y | Integrated | ~2% | fill; Peer C; Peer D; "
    "Peer E; Peer F; Peer G. DIAGNOSTIC SCATTER X axis ROE, Y axis P/B, fitted line, reference "
    "line at P/B = 1.0, vertical band for the assumed cost of equity. Where a cell is unknown, insert "
    "\"[verify]\". No em-dashes. Arial only."
)


COMPONENTS = {"kpi_tiles", "bar_table", "comps_table", "callout", "numbered_list", "keyed_markers", "delta_badge"}


CHARTS = {"column", "bar", "line", "scatter", "scatter_lines", "waterfall", "pie", "doughnut"}


PEERS = ["Peer A", "Peer B", "Peer C", "Peer D", "Peer E", "Peer F", "Peer G"]


def utility() -> dict:
    facts = [
        {"id": "F1", "label": "Acme price / book", "value": "~0.3x", "source": "user", "quote": "Price / book: ~0.3x"},
        {"id": "F2", "label": "Acme EV / EBITDA", "value": "~7.0x", "source": "user", "quote": "EV / EBITDA: ~7.0x"},
        {"id": "F3", "label": "Acme dividend yield", "value": "~4.3%", "source": "user", "quote": "Dividend yield: ~4.3%"},
        {"id": "F4", "label": "Acme P/E", "value": "~15.8x", "source": "user", "quote": "P/E: ~15.8x"},
        {"id": "F5", "label": "Acme free float", "value": "~15%", "source": "user"},
        {"id": "F6", "label": "Peer B free float", "value": "~2%", "source": "user"},
        {"id": "F7", "label": "Acme ROE", "value": sb.VERIFY, "source": "verify"},
        {"id": "F8", "label": "Cost of equity assumption", "value": sb.VERIFY, "source": "verify"},
    ]
    for i, name in enumerate(PEERS):
        facts.append({"id": f"F{10 + i}", "label": f"{name} P/B", "value": sb.VERIFY, "source": "verify"})
    return {
        "audience": "Acme Power CFO, valuation-literate",
        "objective": "Prove Acme trades below book because implied returns sit below the cost of equity",
        "action_title": "The market values Acme Power below the book value of its own assets, driven by "
                        "returns below the cost of equity rather than by scale",
        "comparison": "relationship",
        "zones": [
            {"id": "Z1", "role": "lead_metrics", "place": "top", "tier": "primary",
             "content": ["{F1} price / book: market values equity below book", "{F2} EV / EBITDA",
                         "{F3} dividend yield", "{F4} P/E"],
             "exhibit": {"kind": "component", "name": "kpi_tiles", "insight": "P/B tile in the accent colour",
                         "facts": ["F1", "F2", "F3", "F4"]}},
            {"id": "Z2", "role": "side_exhibit", "place": "left", "tier": "support",
             "content": ["Listed peers, one row each, Acme row shaded, peer median row"],
             "exhibit": {"kind": "component", "name": "comps_table", "insight": "Acme row shaded",
                         "facts": ["F1", "F2", "F3", "F5", "F6"]}},
            {"id": "Z3", "role": "main_exhibit", "place": "right", "tier": "hero",
             "content": ["Utilities are priced off returns, not size"],
             "exhibit": {"kind": "chart", "name": "scatter", "insight": "Acme point in accent, labelled",
                         "support": "fitted line across the peers",
                         "facts": ["F1", "F7", "F10", "F11", "F12"]}},
            {"id": "Z4", "role": "takeaway", "place": "bottom", "tier": "context",
             "content": ["Lift ROE toward the cost of equity through the value levers"]},
        ],
        "facts": facts,
        "rules": ["No em-dashes", "Do not use GW or customer count as valuation metrics", "Arial only"],
        "footnote": "Listed peers only. Market data as of [verify]. Cost of equity assumption: {F8}.",
        "source": "Source: [verify]",
    }


def run(data: dict, request: str = REQUEST):
    return sb.validate(sb.Brief.from_dict(data), [request], components=COMPONENTS, charts=CHARTS)


def codes(findings, severity=None):
    return {f.code for f in findings if severity is None or f.severity == severity}


@pytest.mark.parametrize("text,expected", [
    ("~0.3x", ["0.3"]), ("4.30%", ["4.3"]), ("USD 1,200m", ["1200"]), ("(12.5)", ["12.5"]),
    ("FY27 Q3 {F12} 2x2", []), ("P/B = 1.0", ["1"]), ("from 7 to 9", ["7", "9"]),
])
def test_numbers_are_read_canonically(text, expected):
    assert sb.numbers(text) == expected


def test_the_utility_brief_validates_with_verify_placeholders():
    findings = run(utility())
    assert not sb.errors(findings), [f.line() for f in findings]


def test_the_scatter_is_flagged_when_most_points_are_unknown():
    findings = run(utility())
    assert "chart-needs-data" in codes(findings, "warn")


def test_the_fitted_line_is_rewritten_into_a_feasible_series():
    lines = [f for f in run(utility()) if f.code == "device-feasibility"]
    assert any("scatter_lines" in f.message for f in lines)


def test_plan_exhibit_is_a_second_opinion_not_a_veto():
    findings = run(utility())
    disagree = [f for f in findings if f.code == "plan-disagrees"]
    assert disagree and disagree[0].severity == "info"


def test_a_peer_figure_the_user_did_not_give_is_an_estimate_not_a_user_fact():
    data = utility()
    data["facts"][8] = {"id": "F10", "label": "Peer A P/B", "value": "1.4x", "source": "user"}
    assert "ungrounded-fact" in codes(run(data), "warn") and not sb.errors(run(data))
    data["facts"][8]["source"] = "estimate"
    assert not [f for f in run(data) if f.where == "facts[F10]"]


def test_a_brief_built_on_estimates_alone_is_accepted_and_its_figures_may_be_shown():
    data = growth()
    for fact in data["facts"]:
        fact.update(source="estimate", quote="")
    brief = sb.Brief.from_dict(data)
    assert not sb.errors(sb.validate(brief, ["Make a revenue growth slide for the board"]))
    assert sb.ungrounded_in_slide("Revenue rose 21% to 510", brief, ["Make a revenue growth slide"]) == []
    assert "(estimate)" in sb.render_for_designer(brief)


def test_verify_must_not_carry_a_guess():
    data = utility()
    data["facts"][6] = {"id": "F7", "label": "Acme ROE", "value": "~6%", "source": "verify"}
    assert "verify-with-number" in codes(run(data), "error")


def test_a_literal_figure_outside_facts_is_a_warning():
    data = utility()
    data["zones"][3]["content"].append("Closing the gap is worth SAR 40bn")
    assert "literal-number" in codes(run(data), "warn") and not sb.errors(run(data))
    data = utility()
    data["zones"][3]["content"].append("Acme trades at 0.3x book")          # in the request: allowed
    assert "literal-number" not in codes(run(data))


def test_small_counts_and_years_warn_rather_than_fail():
    data = utility()
    data["zones"][3]["content"].append("Five levers, delivered by 2030")
    found = [f for f in run(data) if f.code == "literal-number"]
    assert found and all(f.severity == "warn" for f in found)


def test_an_unknown_fact_reference_is_an_error():
    data = utility()
    data["zones"][0]["content"].append("{F99} market cap")
    assert "unknown-fact" in codes(run(data), "error")


def test_derived_figures_are_recomputed():
    request = "Peer P/B: A 1.2x, B 0.8x, C 1.0x, Acme 0.3x."
    data = utility()
    data["facts"] = [
        {"id": "F1", "label": "A", "value": "1.2x", "source": "user"},
        {"id": "F2", "label": "B", "value": "0.8x", "source": "user"},
        {"id": "F3", "label": "C", "value": "1.0x", "source": "user"},
        {"id": "F4", "label": "Peer median", "value": "1.0x", "source": "derived", "formula": "median(F1,F2,F3)",
         "inputs": ["F1", "F2", "F3"]},
        {"id": "F5", "label": "Acme", "value": "0.3x", "source": "user"},
        {"id": "F7", "label": "x", "value": sb.VERIFY, "source": "verify"},
        {"id": "F8", "label": "CoE", "value": sb.VERIFY, "source": "verify"},
        {"id": "F10", "label": "p", "value": sb.VERIFY, "source": "verify"},
        {"id": "F11", "label": "p", "value": sb.VERIFY, "source": "verify"},
        {"id": "F12", "label": "p", "value": sb.VERIFY, "source": "verify"},
    ]
    data["zones"][0]["exhibit"]["facts"] = ["F1"]
    data["zones"][1]["exhibit"]["facts"] = ["F1"]
    data["zones"][0]["content"] = ["{F5} price / book"]
    assert "derived-mismatch" not in codes(run(data, request))
    data["facts"][3]["value"] = "0.9x"
    assert "derived-mismatch" in codes(run(data, request), "error")
    data["facts"][3].update(value="1.0x", formula="median(F1,F7)", inputs=["F1", "F7"])
    assert "derived-from-verify" in codes(run(data, request), "error")


def test_components_and_chart_kinds_must_exist():
    data = utility()
    data["zones"][1]["exhibit"]["name"] = "peer_matrix"
    assert "unknown-component" in codes(run(data), "error")
    data = utility()
    data["zones"][2]["exhibit"]["name"] = "combo"
    assert "refused-chart" in codes(run(data), "error")
    data["zones"][2]["exhibit"]["name"] = "bubble"
    assert "refused-chart" in codes(run(data), "error")
    data["zones"][2]["exhibit"]["name"] = "sankey"
    assert "unknown-chart" in codes(run(data), "error")


@pytest.mark.parametrize("device,severity", [
    ("revenue bars with margin on a secondary axis", "error"),
    ("a callout arrow pointing at the Acme dot", "warn"),
    ("a vertical band for the cost of equity", "warn"),
])
def test_devices_the_engine_cannot_draw(device, severity):
    data = utility()
    data["zones"][2]["exhibit"]["support"] = device
    found = [f for f in run(data) if f.code == "device-feasibility" and f.where.endswith("support")]
    assert found and found[0].severity == severity


def test_structure_limits():
    data = utility()
    data["zones"][0]["tier"] = "hero"
    assert "hero-count" in codes(run(data), "warn")
    data = utility()
    data["zones"] += [copy.deepcopy(data["zones"][3]) for _ in range(4)]
    assert "too-many-zones" in codes(run(data), "error")
    data = utility()
    data["zones"][0]["role"] = "hero_banner"
    assert "bad-role" in codes(run(data), "error")


def test_the_length_cap():
    data = utility()
    data["objective"] = "x " * 4000
    assert "brief-long" in codes(run(data), "error")


def test_em_dash_rule_is_checked_when_the_user_sets_it():
    data = utility()
    data["zones"][3]["content"] = ["Returns — not size — drive value"]
    assert "em-dash" in codes(run(data), "warn")


def test_skip_briefs_are_not_validated():
    assert run({"skip": "cover slide", "audience": "", "objective": "", "action_title": ""}) == []


def test_fit_line_gives_a_two_point_series():
    fit = sb.fit_line([(5, 0.5), (10, 1.0), (15, 1.5), (20, 2.0)])
    assert fit["slope"] == pytest.approx(0.1) and fit["intercept"] == pytest.approx(0.0)
    assert fit["r2"] == pytest.approx(1.0)
    assert fit["series"]["x"] == [5, 20] and fit["series"]["y"] == pytest.approx([0.5, 2.0])
    assert sb.fit_line([(1, 1), (2, 2)]) is None


def test_render_for_designer_lists_facts_and_unknowns():
    brief = sb.Brief.from_dict(utility())
    text = sb.render_for_designer(brief, run(utility()))
    assert "F1 Acme price / book = ~0.3x" in text
    assert "Unknown, show [verify]" in text and "Peer A P/B" in text
    assert "{F" not in text.split("- Facts")[0]          # references are resolved in the zones
    assert len(text) < sb.MAX_BRIEF_CHARS


def test_critic_checks_are_few_and_name_the_hero():
    checks = sb.critic_checks(sb.Brief.from_dict(utility()))
    assert 1 < len(checks) <= 5
    assert any("hero" in c and "scatter" in c for c in checks)


@pytest.mark.parametrize("request_text,slide_type,selected,expected", [
    ("Design the cover", "cover", None, False),
    ("make the title shorter", None, "s1", False),
    ("Create a slide showing our cost base is 23% above peers", None, None, True),
    ("hello", None, None, False),
    (REQUEST, None, "s1", True),
])
def test_should_brief(request_text, slide_type, selected, expected):
    assert sb.should_brief(request_text, slide_type=slide_type, selected=selected)[0] is expected


def test_schema_enums_match_the_constants():
    zone = sb.BRIEF_SCHEMA["properties"]["zones"]["items"]["properties"]
    assert zone["role"]["enum"] == list(sb.ROLES) and zone["tier"]["enum"] == list(sb.TIERS)
    assert sb.BRIEF_SCHEMA["properties"]["facts"]["items"]["properties"]["source"]["enum"] == list(sb.OFFERED_SOURCES)
    assert "verify" not in sb.OFFERED_SOURCES and "estimate" in sb.OFFERED_SOURCES
    assert zone["exhibit"]["properties"]["kind"]["enum"] == list(sb.EXHIBIT_KINDS) and "diagram" in sb.EXHIBIT_KINDS


def test_chart_types_follow_the_prompt_and_exclude_refused_kinds():
    kinds = sb.chart_types()
    assert "scatter" in kinds and "waterfall" in kinds
    assert not kinds & set(sb.REFUSED_CHARTS)


def test_draft_brief_repairs_once_with_the_validator_findings():
    bad = utility()
    bad["zones"][2]["exhibit"]["name"] = "radar"
    replies = [json.dumps(bad), "Here it is:\n" + json.dumps(utility())]
    calls = []

    def model(system, user):
        calls.append((system, user))
        return replies[len(calls) - 1]

    brief, findings = sb.draft_brief(REQUEST, model, components=COMPONENTS, charts=CHARTS)
    assert len(calls) == 2
    assert "refused-chart" in calls[1][1] and "estimate" in calls[0][0]
    assert not sb.errors(findings) and brief.zones[2].exhibit.name == "scatter"


def test_draft_brief_returns_the_findings_when_the_repair_also_fails():
    bad = utility()
    bad["zones"][2]["exhibit"]["name"] = "combo"
    brief, findings = sb.draft_brief(REQUEST, lambda s, u: json.dumps(bad), components=COMPONENTS, charts=CHARTS)
    assert "refused-chart" in codes(findings, "error")


def test_the_instruction_lists_real_components_and_asks_for_estimates_when_there_is_no_data():
    text = sb.instruction(COMPONENTS, CHARTS)
    assert "comps_table" in text and "scatter_lines" in text and "combo" in text
    assert 'source "estimate"' in text and "never estimate" not in text and "[verify]" not in text
    assert "diagram" in text and "DENSE" in text and f"at least {sb.MIN_DRAWN} drawn exhibits" in " ".join(text.split())


def test_figures_on_the_saved_slide_must_come_from_the_brief_or_the_user():
    brief = sb.Brief.from_dict(utility())
    text = "Price / book 0.3x | EV / EBITDA 7.0x | Peer A 1.4x | ROE 6.2% | 2025 | 5 levers"
    assert sb.ungrounded_in_slide(text, brief, [REQUEST]) == ["1.4", "6.2"]
    assert sb.ungrounded_in_slide("P/B 0.3x", None, [REQUEST]) == []


ASK = ("For the board, build a slide showing revenue grew from 420 to 510 over two years, a 21% rise, "
       "driven by the new enterprise tier.")


def growth(**extra) -> dict:
    brief = {
        "audience": "Board", "objective": "Show revenue growth came from the enterprise tier",
        "action_title": "Revenue rose {F3} to {F2} in two years on the enterprise tier",
        "comparison": "time",
        "zones": [{"id": "Z1", "role": "main_exhibit", "place": "left", "tier": "hero",
                   "content": ["{F1} to {F2}"],
                   "exhibit": {"kind": "chart", "name": "column", "insight": "the latest column in accent",
                               "facts": ["F1", "F2"]}},
                  {"id": "Z2", "role": "lead_metrics", "place": "top", "tier": "primary",
                   "content": ["{F2} revenue now", "{F3} growth"],
                   "exhibit": {"kind": "component", "name": "kpi_tiles", "facts": ["F2", "F3"]}},
                  {"id": "Z3", "role": "side_exhibit", "place": "right", "tier": "support",
                   "content": ["Enterprise tier: new logos and upsell"],
                   "exhibit": {"kind": "diagram", "name": "process flow", "insight": "the enterprise step in accent"}},
                  {"id": "Z4", "role": "takeaway", "place": "bottom", "tier": "context",
                   "content": ["Enterprise tier drove the rise"]}],
        "facts": [{"id": "F1", "label": "Revenue, start", "value": "420", "source": "user", "quote": "from 420"},
                  {"id": "F2", "label": "Revenue, now", "value": "510", "source": "user", "quote": "to 510"},
                  {"id": "F3", "label": "Growth", "value": "21%", "source": "user", "quote": "a 21% rise"}],
    }
    brief.update(extra)
    return brief


def test_the_brief_schema_types_every_enum_for_gemini():
    def walk(node):
        if isinstance(node, dict):
            if "enum" in node:
                assert node.get("type") == "string", node
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(sb.BRIEF_SCHEMA)
    assert "pattern" not in json.dumps(sb.BRIEF_SCHEMA)


def test_critic_items_make_the_hero_and_grounding_musts_and_grounding_comes_last():
    items = sb.critic_items(sb.Brief.from_dict(utility()))
    assert 2 < len(items) <= 5
    hero = [sev for line, sev in items if line.startswith("The hero is")]
    assert hero == ["must"]
    last, sev = items[-1]
    assert sev == "must" and "one of the brief's facts" in last and "~0.3x" in last and sb.VERIFY in last
    assert all(sev == "nice" for line, sev in items if line.startswith(("The title", "Zone ")))
    assert len(sb.critic_items(sb.Brief.from_dict(utility()), limit=2)) == 2
    assert sb.critic_items(sb.Brief(audience="", objective="", action_title="", skip="cover")) == []


def _html(body: str) -> str:
    return f"<!doctype html><html><body>{body}</body></html>"


CHART = ("<div data-chart='{\"type\":\"column\",\"categories\":[\"2023\",\"2025\"],"
         "\"series\":[{\"name\":\"Revenue\",\"values\":[420,%s]}],\"options\":{\"valueAxis\":{\"max\":600}}}'></div>")


@pytest.mark.parametrize("text,expected", [
    ("USD 45m", ["45"]), ("(0.5)", ["0.5"]), ("27 pts", ["27"]), ("SAR 1,200.50", ["1200.5"]), ("~0.3x", ["0.3"]),
    ("+36%", ["36"]), ("2x2 in Q3 FY27", []),
])
def test_numbers_reads_the_formats_slides_use(text, expected):
    assert sb.numbers(text) == expected


def test_chart_figures_are_the_plotted_values_not_the_layout():
    html = CHART % "510.0"
    assert sb.chart_figures(html) == ["420", "510", "2023", "2025"]
    assert sb.chart_figures("<div data-chart='not json'></div>") == []
    escaped = '<div data-chart="{&quot;series&quot;:[{&quot;values&quot;:[1.25,-3]}]}"></div>'
    assert sb.chart_figures(escaped) == ["1.25", "3"]


def test_a_rounded_figure_counts_as_grounded():
    assert sb.ungrounded_in_slide("4.3% and 4.4% and 46", None, ["4.33% and 45.6"]) == ["4.4"]


def test_figures_in_keeps_currency_and_unit_and_drops_years_and_counts():
    assert sb.figures_in("AUM $57.1B to $67.4B, up 18% in 2024; 3 levers; 340bp; 0.3x P/B; 5%") == [
        "$57.1B", "$67.4B", "18%", "340bp", "0.3x", "5%"]


def test_the_prefill_takes_the_title_verbatim_and_every_figure_as_a_quoted_fact():
    spec = copy.deepcopy(SPEC)
    s = spec["slide"]
    draft = sb.prefill_from_spec(s["title"], s["bullets"], audience="Executive committee", objective=s["description"],
                                 slide_type=s["type"], framework=s["framework"], chart=s["chartData"])
    assert draft["action_title"] == s["title"] and draft["audience"] == "Executive committee"
    assert [f["value"] for f in draft["facts"]] == ["$57.1B", "$67.4B", "$8.2B", "$2.1B"]   # chart dupes merged
    assert draft["facts"][2]["quote"] == "Net new assets $8.2B" and draft["facts"][2]["source"] == "user"
    assert draft["comparison"] == "bridge"
    assert draft["zones"][0]["tier"] == "hero" and draft["zones"][0]["exhibit"]["name"] == "waterfall"
    only_chart = sb.prefill_from_spec("Revenue grew", [], chart={"categories": ["a", "b"],
                                                                 "series": [{"name": "Rev", "values": [420, 510]}]})
    assert [(f["label"], f["value"], f["quote"]) for f in only_chart["facts"]] == [("Rev a", "420", ""),
                                                                                    ("Rev b", "510", "")]


def test_a_thin_brief_is_sent_back_for_more_exhibits():
    data = growth()
    data["zones"] = data["zones"][:1] + data["zones"][-1:]
    brief = sb.Brief.from_dict(data)
    assert "thin-slide" in codes(sb.validate(brief, [ASK]), "error")
    three = growth()
    three["zones"] = three["zones"][:3]
    assert "few-zones" in codes(sb.validate(sb.Brief.from_dict(three), [ASK]), "warn")
    assert "- Density:" in sb.render_for_designer(sb.Brief.from_dict(growth()))


def test_a_brief_with_json_encoded_arrays_is_read():
    """A model sometimes sends an array argument as a JSON string; the brief must still parse."""
    from app.core.design_refs import slide_brief

    zones = [{"id": "Z1", "role": "main_exhibit", "place": "left", "tier": "hero",
              "exhibit": {"kind": "chart", "name": "bar"}}]
    b = slide_brief.Brief.from_dict({"audience": "Board", "objective": "o", "action_title": "t",
                                     "zones": json.dumps(zones), "facts": "[]", "rules": '["no pies"]'})
    assert b.zones[0].exhibit.name == "bar" and b.rules == ["no pies"]


def test_a_malformed_brief_is_a_repairable_error_not_a_crash():
    from app.core.design_refs import slide_brief

    with pytest.raises(ValueError, match="zones"):
        slide_brief.Brief.from_dict({"zones": ["just a string"]})
    with pytest.raises(ValueError, match="zones"):
        slide_brief.Brief.from_dict({"zones": "not json"})
