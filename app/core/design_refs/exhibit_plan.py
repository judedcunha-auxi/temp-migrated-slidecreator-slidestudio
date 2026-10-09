"""`plan_exhibit`: choose the exhibit and its devices from the message, before any HTML is written.

Pure Python and deterministic, with no model call and no cost. The design chat calls it once per body slide
(`docs/DESIGN-DEVICES-RESEARCH.md` §3). The slides it replaces *stated* evidence (numbers in a text table)
where a consultant would *show* it. The step that was missing is the one Zelazny teaches: the message implies
a comparison, and the comparison picks the chart. The keywords sit in the action title, so the choice can be
made by lookup:

    message (action title) ─► comparison type ─► main exhibit (component or data-chart + skeleton)
                                              ├► ONE insight device (points at the so-what)
                                              ├► ONE supporting device (side panel)
                                              └► the takeaway form (the one unit with the so-what)

The comparison types are Zelazny's five (component, item, time series, frequency, correlation), widened with
the FT Visual Vocabulary (deviation, flow, spatial, ...) and the consulting and finance shapes the vocabulary
already knows (bridge, evaluation, sequence, structure, valuation, market sizing, KPI status). A slide type or
framework the caller passes wins over keywords. The framework recipes and finance exhibits that used to ride
in the system prompt (`vocabulary.FRAMEWORKS`, `FINANCE_EXHIBITS`) are delivered here, on demand.

    plan_exhibit(title, slide_type=None, framework=None, facts=None, lower_is_better=None) -> str

Component names are checked against the installed library at call time (`components.names()`): a planned
component that is not installed yet (`bar_table`, `bullet_rows`, `delta_badge`, `keyed_markers` arrive with
their own work package) is replaced by the fallback the plan names, so the tool never sends Claude to a
missing component.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.core.design_refs import vocabulary

log = logging.getLogger(__name__)

try:  # optional, like the prompt: without the library every exhibit is described as native HTML/data-chart
    from app.core.design_refs import components as _components
except Exception:  # noqa: BLE001
    log.warning("component library could not be imported", exc_info=True)
    _components = None  # type: ignore[assignment]


@dataclass(frozen=True)
class Plan:
    """What one comparison type calls for. `exhibit` names a component (or a data-chart kind in backticks);
    `fallback` is used when that component is not installed."""

    label: str
    exhibit: str
    skeleton: str
    insight: str
    support: str
    takeaway: str
    fallback: str = ""


#: Comparison type -> plan. Devices are one insight device and one supporting device each: two devices done
#: well read as craft, five read as clutter.
PLANS: dict[str, Plan] = {
    "ranking": Plan(
        "item comparison / ranking (who is larger, the subject against peers)",
        "bar_table", '{"rows": [{"label": "…", "value": 0, "benchmark": 0, "lower_is_better": false}], '
        '"subject": "…", "benchmark": "…", "headings": ["Metric", "Gap"], "scale": "row", "highlight": "worse"}',
        "focus colour on the subject's bars, peers grey, the peer median or best-in-class as a tick or "
        "`referenceLines` on the same scale; the gap labelled where it matters",
        "keyed_markers: numbered markers 1, 2, 3 on the rows that matter, the same numbers heading the side-panel points",
        "a closing strip: what closing the largest gap is worth",
        fallback="column_chart (orientation bar, sorted, `highlight` the subject) or a <table> with native bar "
                 "divs in a bar column"),
    "deviation": Plan(
        "deviation / gap to target (actual against plan, budget, benchmark)",
        "bullet_rows", '{"rows": [{"label": "…", "actual": 0, "target": 0, "bands": [0, 0, 0], "format": "0"}], '
        '"band_labels": ["Poor", "Satisfactory", "Good"], "flag_misses": true}',
        "the target as a tick on every bar (grey qualitative bands behind); misses coloured by good/bad "
        "direction, not by sign",
        "delta_badge per row (value, direction, good_when: up|down) so a fall in a cost reads as good",
        "a verdict line in the side panel: which misses matter and the action",
        fallback="column_chart with `referenceLines` for one target, or a <table> with bar divs and a target "
                 "tick div per row"),
    "time": Plan(
        "change over time (grew, fell, trend, since, by 20xx)",
        "column_chart", '{"categories": ["2021", "…"], "values": [0], "highlight": [5], "cagr": '
        '{"from": 0, "to": 5, "label": "+x% p.a."}, "header": "Metric, unit, period"}',
        "a CAGR arrow (column_chart `cagr`) or the latest/forecast bars in focus colour with the rest grey",
        "delta_badge for the headline change, or a 2-3 point driver list keyed to the periods",
        "a closing strip: what the trend means for the decision",
        fallback="line_chart for 2+ series (the subject in focus colour, others grey, labels at line ends)"),
    "part_to_whole": Plan(
        "part-to-whole (share, mix, split, % of)",
        "share_bar", '{"categories": ["…"], "series": [{"name": "…", "values": [0]}], "highlight": 0, '
        '"header": "Share, % of …"}',
        "the focal segment in focus colour, the rest in greys; its share labelled on the bar",
        "a 3-row breakdown of what sits inside the focal segment (bold lead-in + number)",
        "the side panel's verdict on the focal segment",
        fallback="`bar_stacked_100` data-chart (pie only for <= 4 parts); mekko when two dimensions matter"),
    "bridge": Plan(
        "bridge / walk (from X to Y, what moved it)",
        "waterfall", '{"categories": ["Start", "…", "End"], "values": [0, 0, 0], "totals": [0, 2], '
        '"number_format": "#,##0;(#,##0)", "header": "Metric bridge, unit"}',
        "a callout on the biggest step (leader line + one line of why); rises and falls in two tones",
        "keyed_markers on the 2-3 steps that matter, explained in the side panel",
        "a closing strip: net effect and what it implies",
        fallback="`waterfall` data-chart with `totals` declared"),
    "distribution": Plan(
        "distribution / range (spread, range, concentration, quartiles)",
        "football_field", '{"rows": [{"label": "…", "low": 0, "high": 0}], "reference": {"value": 0, '
        '"label": "…"}, "highlight": 0}',
        "a reference line for the subject or the median; the subject's range in focus colour",
        "a small table of the statistics behind the range (n, median, IQR)",
        "the side panel's verdict: where the subject sits and why it matters",
        fallback="`bar_stacked` floating bars (first series invisible) or `column` histogram"),
    "relationship": Plan(
        "relationship / correlation (drives, relates to, the more … the more)",
        "`scatter` data-chart", '{"type": "scatter", "series": [{"name": "…", "values": [[x, y]]}]}',
        "the subject's point in focus colour and labelled, a `referenceLines` average or a shaded target "
        "zone; axes named with units",
        "keyed_markers on the 2-3 outliers, explained in the side panel",
        "a closing strip: the rule the relationship implies",
        fallback="matrix_2x2 (dots) when the axes are judgements, not measures"),
    "sequence": Plan(
        "sequence / flow (phases, roadmap, steps, by Qx)",
        "chevron_process", '{"steps": [{"title": "…", "bullets": ["…"]}], "ramp": true}',
        "the current or critical phase in focus colour (others on a ramp or grey); a 'today' marker on a "
        "gantt",
        "a grid spine under the steps: owner / duration / deliverable rows aligned to each step",
        "a closing strip: the gate or decision that unlocks the next phase",
        fallback="gantt (bars on period columns, milestone diamonds) when dates matter"),
    "evaluation": Plan(
        "evaluation of options against criteria (choose, options, recommend, trade-off)",
        "harvey_table", '{"criteria": ["…"], "rows": [{"label": "Option A", "scores": [0, 1, 2, 3, 4]}], '
        '"legend": true}',
        "the recommended option as the hero (solid fill column or row) with a 'Recommended' tag",
        "a legend that decodes the balls, or a +/- row per option",
        "the hero unit itself states the verdict; no second takeaway",
        fallback="comparison_columns (parallel option columns on one row spine, `recommended`)"),
    "structure": Plan(
        "structure / roles (who owns, governance, organisation, operating model)",
        "org_tiers", '{"tiers": [{"label": "…", "boxes": ["…"]}], "highlight": 0}',
        "the tier or box that changes in focus colour; every arrow labelled with what flows",
        "keyed_markers pinned to the changes, explained in the side panel",
        "a closing strip: the decision rights that change",
        fallback="rag_table as a RACI grid (one owner per row)"),
    "spatial": Plan(
        "spatial / geographic (regions, footprint, countries)",
        "heatmap_table", '{"columns": ["…"], "rows": [{"label": "…", "values": [0]}], "unit": "…"}',
        "the focal region in focus colour; a map only if one is in the assets (never invent one)",
        "keyed_markers on the regions discussed in the side panel",
        "the side panel's verdict on where to act",
        fallback="a regional grid or a sorted bar chart by region"),
    "valuation": Plan(
        "valuation (offer, premium, per share, multiples)",
        "football_field", '{"rows": [{"label": "…", "low": 0, "high": 0}], "reference": {"value": 0, '
        '"label": "Offer …"}, "number_format": "0.00", "highlight": 0}',
        "the offer or current price as a reference line across every methodology",
        "a comps or sensitivity table (comps_table / sensitivity_table) as the side exhibit",
        "a closing strip: where the offer sits against the ranges",
        fallback="`bar_stacked` floating bars with `referenceLines`"),
    "market": Plan(
        "market sizing (TAM/SAM/SOM, addressable, pool, prize)",
        "funnel", '{"stages": [{"label": "…", "value": "…", "note": "filter criterion"}]}',
        "the obtainable slice in focus colour, the filter criterion labelled at each stage",
        "a 3-row growth-driver list with one number each",
        "a closing strip: the size of the prize and the share it implies",
        fallback="mekko for market × player; concentric circles (SVG) for TAM/SAM/SOM"),
    "kpi": Plan(
        "KPI status (targets met, scorecard, dashboard)",
        "bullet_rows", '{"rows": [{"label": "KPI", "actual": 0, "target": 0, "bands": [0, 0, 0]}]}',
        "misses in focus colour against a target tick; hits grey",
        "status pills (on track / at risk / off track) decoded by a legend",
        "a closing strip: the 1-2 KPIs that need action",
        fallback="kpi_tiles (3-5 tiles, the headline one in accent, each with a delta) or rag_table"),
    "statement": Plan(
        "a single statement or number (the message is one fact)",
        "kpi_tiles", '{"tiles": [{"value": "…", "label": "…", "delta": "…"}], "highlight": 0}',
        "the one number large, in focus colour, with its qualifier",
        "the 2-3 facts that prove it, as bold lead-in rows",
        "none beyond the number itself: restraint is right here",
        fallback="callout (panel)"),
}

#: Keyword patterns per comparison type, scored by hits. Order breaks ties (earlier wins).
KEYWORDS: list[tuple[str, str]] = [
    ("bridge", r"\bbridge\b|\bwalk\b|\bbuild[- ]up\b|(?<!range )(?<!ranges )(?<!ranging )(?<!varies )(?<!vary )\bfrom\b.{1,40}\bto\b|\breconcil|\bmoved\b|\bexplain(s|ed)? the"),
    ("valuation", r"valuation|\boffer\b|\bpremium\b|per share|\bmultiples?\b|ev/|football field|\bdcf\b|\blbo\b"),
    ("market", r"\btam\b|\bsam\b|\bsom\b|addressable|market size|\bprize\b|profit pool|\bpool\b|whitespace"),
    ("kpi", r"\bkpis?\b|scorecard|dashboard|targets? (met|missed)|(six|five|four|three|\d+) of (the )?\w+ "
            r"(kpis|targets|metrics)|on track|off track"),
    ("evaluation", r"\boptions?\b|\bchoose\b|\bcriteria\b|\brecommend|\btrade-?offs?\b|\bevaluat|\bpreferred\b|"
                   r"\bscored\b|\bshortlist|\bbuild,? buy|\bbest fit\b"),
    ("sequence", r"\bphases?\b|\broadmap\b|\bsteps?\b|\bstages?\b|\bjourney\b|\broll-?out\b|\bmilestones?\b|"
                 r"\bwaves?\b|\bsequenc|\bby q[1-4]\b|\bgates?\b|\bplan to\b|\bweeks?\b|\bmonths? \d"),
    ("structure", r"\bgovernance\b|\bowns?\b|\borg(anisation|anization|anisational)?\b|\broles?\b|\braci\b|"
                  r"reports? to|operating model|decision rights|\bcommittee"),
    ("spatial", r"\bregions?\b|\bcountr(y|ies)\b|geograph|footprint|\bmap\b|\bcities\b|\bemirates?\b|\bstates\b"),
    ("relationship", r"correlat|relates? to|\bdriven by\b|\bdrives?\b|associated with|the more|increases? with|"
                     r"elasticit|\bexplains?\b"),
    ("distribution", r"\brange\b|distribution|\bspread\b|dispersion|concentrat|\bquartiles?\b|\bvaries\b|"
                     r"\bvariance across\b|\boutliers?\b"),
    ("part_to_whole", r"\bshare\b|% of|\bmix\b|\bsplit\b|breakdown|composition|accounts? for|makes? up|"
                      r"two-thirds|one-third|\bhalf of\b|\bmajority\b|\bportion\b"),
    ("deviation", r"\btarget\b|\bbudget\b|\bplan\b|\bgoal\b|\bvs\.? plan\b|\bshortfall\b|\bmiss(ed|es)?\b|"
                  r"\bahead of\b|\bbehind\b|\bover(shoot|run)|\babove\b|\bbelow\b"),
    ("ranking", r"\bpeers?\b|benchmark|\btrails?\b|\blags?\b|\bleads?\b|outpaces?|outperform|underperform|"
                r"\bgaps?\b|\blargest\b|\bhighest\b|\blowest\b|\branks?\b|\btop\b|\bbottom\b|\bvs\.?\b|"
                r"\bversus\b|\bcompetitors?\b|best-in-class"),
    ("time", r"\bgr(ew|ow|owth|ows)\b|\bsince\b|\btrend|over time|\bby 20\d\d\b|\bcagr\b|\bdeclin|\brose\b|"
             r"\bfell\b|year[- ]on[- ]year|\byoy\b|\bdoubled\b|\btripled\b|20\d\d\s*[-–]\s*(20)?\d\d|"
             r"\bin 20\d\d\b|\bexpand(ed|s)?\b|\bshr(ank|inks)\b"),
]
_COMPILED = [(kind, re.compile(pattern, re.I)) for kind, pattern in KEYWORDS]

#: A slide type from the vocabulary -> comparison type (the caller's declared type wins over keywords).
SLIDE_TYPE_TO_KIND = {
    "data": None, "table": "ranking", "comparison": "evaluation", "process": "sequence", "timeline": "sequence",
    "benchmark": "ranking", "scorecard": "evaluation", "org": "structure", "kpi": "kpi", "market": "market",
    "map": "spatial", "synthesis": "statement", "quote": "statement", "charter": "structure",
    "case-study": "statement", "deep-dive": "sequence", "framework": None,
}

#: Framework (canonical name) -> comparison type, where the framework settles it.
FRAMEWORK_TO_KIND = {
    "waterfall chart": "bridge", "100% stacked bar chart": "part_to_whole", "TAM/SAM/SOM concentric circles":
    "market", "funnel": "market", "gantt chart": "sequence", "milestone timeline": "sequence",
    "phased roadmap": "sequence", "chevron process flow": "sequence", "numbered step process": "sequence",
    "swimlane diagram": "sequence", "value chain": "sequence", "customer journey map": "sequence",
    "options evaluation matrix": "evaluation", "weighted scoring matrix": "evaluation",
    "single-option criteria scorecard": "evaluation", "gated evaluation scorecard": "evaluation",
    "benchmark comparison table": "ranking", "attribute comparison table": "ranking",
    "checkmark presence matrix": "ranking", "heatmap": "distribution", "bubble chart": "relationship",
    "2x2 matrix": "relationship", "BCG growth-share matrix": "relationship", "org chart": "structure",
    "three-tier governance stack": "structure", "responsibility assignment table": "structure",
    "KPI tiles": "kpi", "KPI inventory table": "kpi", "annotated geographic map with callouts": "spatial",
}

#: Finance exhibits by comparison type (their recipes ride along in the result).
FINANCE_FOR_KIND = {
    "valuation": ["football field", "trading / transaction comps", "sensitivity table"],
    "bridge": ["bridge (EBITDA, revenue, valuation)"],
    "time": ["trend with CAGR"], "part_to_whole": ["market share / mix", "sources & uses / cap table"],
    "ranking": ["market share / mix"],
}

_NUMBER = re.compile(r"[-−–+]?\(?\$?€?£?\d[\d,.]*\)?\s*(%|pts?|bps|x|×|bn|m|k)?", re.I)
_LOWER_BETTER = re.compile(r"cost|expense|ratio|churn|attrition|time to|lead time|cycle time|defect|error|"
                           r"days|delay|backlog|leakage|loss|risk|emission|debt|leverage", re.I)


def classify(title: str, slide_type: str | None = None, framework: str | None = None) -> tuple[str, str]:
    """(comparison type, why): the declared framework, then the declared slide type, then title keywords."""
    canonical = vocabulary.canonical_framework(framework) if framework else None
    if canonical and canonical in FRAMEWORK_TO_KIND:
        return FRAMEWORK_TO_KIND[canonical], f"framework “{canonical}”"
    declared = SLIDE_TYPE_TO_KIND.get((slide_type or "").strip().lower())
    scores: dict[str, int] = {}
    hits: dict[str, list[str]] = {}
    for kind, rx in _COMPILED:
        found = [m.group(0) for m in rx.finditer(title or "")]
        if found:
            scores[kind] = len(found)
            hits[kind] = found
    if declared and (not scores or scores.get(declared)):
        return declared, f"slide type “{slide_type}”"
    if not scores:
        if declared:
            return declared, f"slide type “{slide_type}”"
        digits = len(_NUMBER.findall(title or ""))
        return ("statement" if digits <= 1 else "ranking"), "no comparison keyword in the title"
    order = [kind for kind, _ in KEYWORDS]
    best = max(scores, key=lambda k: (scores[k], -order.index(k)))
    return best, "title keywords: " + ", ".join(f"“{h.strip()}”" for h in hits[best][:3])


def _installed(name: str) -> bool:
    if name.startswith("`"):
        return True
    try:
        return _components is not None and name in _components.names()
    except Exception:  # noqa: BLE001
        return False


def _device(text: str) -> str:
    """A device line naming a component that is not installed says to draw it natively instead."""
    head = text.split(":", 1)[0].split(" ", 1)[0]
    if re.fullmatch(r"[a-z_]+", head) and "_" in head and not _installed(head):
        return f"{text} (draw it with native shapes: the `{head}` component is not installed)"
    return text


def _placeholder(value: Any) -> Any:
    """A default param reduced to its shape: strings become "…", numbers 0, lists keep one element."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return 0
    if isinstance(value, str):
        # enum values ("row", "worse", "auto") and number formats ("0.0", "#,##0;(#,##0)") are real options;
        # anything else is demo content and must not be copied
        return value if re.fullmatch(r"[a-z_]{1,12}|[#0.,;%()x\-]{1,24}", value) else "…"
    if isinstance(value, list):
        return [_placeholder(value[0])] if value else []
    if isinstance(value, dict):
        return {k: _placeholder(v) for k, v in value.items()}
    return value


def skeleton(name: str, fallback: str = "") -> str:
    """The params skeleton for an installed component, derived from its own defaults, so the plan can never
    drift from the component's real parameter names. `fallback` when the component is not installed."""
    import json

    try:
        spec = _components.spec(name) if _components is not None else None
    except Exception:  # noqa: BLE001
        log.debug("component spec unavailable", exc_info=True)
        spec = None
    if not spec or not getattr(spec, "params", None):
        return fallback
    return json.dumps(_placeholder(dict(spec.params)), ensure_ascii=False)


def plan_exhibit(title: str, slide_type: str | None = None, framework: str | None = None,
                 facts: list[str] | str | None = None, lower_is_better: list[str] | bool | None = None) -> str:
    """The exhibit plan for one body slide, as text for the tool result."""
    title = " ".join(str(title or "").split())
    if not title:
        raise ValueError("pass the slide's action title as `title`")
    kind, why = classify(title, slide_type, framework)
    plan = PLANS[kind]
    exhibit, shape = plan.exhibit, plan.skeleton
    if not _installed(exhibit):
        exhibit, shape = plan.fallback, ""
    elif not exhibit.startswith("`"):
        shape = skeleton(exhibit, shape)
    skeleton_text = shape
    facts_list = [facts] if isinstance(facts, str) else [str(f) for f in (facts or [])]
    figures = sum(len(_NUMBER.findall(f)) for f in facts_list + [title])

    lines = [f"Exhibit plan for: {title}",
             f"- Comparison: {plan.label} ({why}).",
             f"- Main exhibit: {exhibit}" + (f" — get_component params skeleton: {skeleton_text}" if skeleton_text else "")
             + (f"; else {plan.fallback}" if skeleton_text and plan.fallback else "") + ".",
             f"- Insight device (one, on the exhibit): {_device(plan.insight)}.",
             f"- Supporting device (one, in the side panel): {_device(plan.support)}.",
             f"- Takeaway (exactly one unit): {plan.takeaway}."]
    rules = ["Every quantity gets a visual encoding (bar, tick, ball, heat fill, marker), not only a number "
             "in a cell; bars that compare share one stated scale (axis ticks, the max in the header, or the "
             "benchmark as a tick on the same bar)."]
    lower = lower_is_better
    if lower is True or (isinstance(lower, list) and lower) or _LOWER_BETTER.search(title + " " + " ".join(facts_list)):
        named = ", ".join(lower) if isinstance(lower, list) else "cost, ratio, time and risk metrics"
        rules.append(f"Direction: for {named} lower is better — colour a gap by good/bad, not by sign, say "
                     f"'lower is better' in the row label, and never let a longer bar read as a win when it "
                     f"is a loss.")
    if not re.search(r"\d", title) and kind != "structure":
        rules.append("The title carries no number: add the figure that proves it (e.g. the gap, the share, "
                     "the growth).")
    if figures >= 4 and kind == "statement":
        rules.append("Four or more figures: encode them (bar_table / column_chart), a statement layout would "
                     "only list them.")
    lines.append("- Rules: " + " ".join(rules))
    lines.append('- Declare it: put data-exhibit-plan="' + kind + '; insight: ' + plan.insight.split(";")[0].split(",")[0]
                 + '" on the body wrapper (the export ignores it; the reviewer checks the device is there).')
    recipes = _recipes(kind, framework)
    if recipes:
        lines.append("Recipes:")
        lines.extend(f"  - {r}" for r in recipes)
    lines.append("Override the plan when the content calls for it, and say so in one line; keep one insight "
                 "and one supporting device.")
    return "\n".join(lines)


def _recipes(kind: str, framework: str | None) -> list[str]:
    """The framework recipe(s) and finance exhibits that fit, from the vocabulary (moved out of the prompt)."""
    out: list[str] = []
    canonical = vocabulary.canonical_framework(framework) if framework else None
    if canonical:
        out.append(f"{canonical}: {vocabulary.framework_hint(canonical)}")
    for name, k in FRAMEWORK_TO_KIND.items():
        if k == kind and name != canonical and len(out) < 3:
            hint = vocabulary.framework_hint(name)
            if hint:
                out.append(f"{name}: {hint}")
    for name in FINANCE_FOR_KIND.get(kind, []):
        hint = vocabulary.FINANCE_EXHIBITS.get(name)
        if hint:
            out.append(f"{name}: {hint}")
    return out


def recipe(name: str) -> str | None:
    """One framework's or finance exhibit's recipe by any known name or alias (for tests and tools)."""
    canonical = vocabulary.canonical_framework(name)
    if canonical:
        return vocabulary.framework_hint(canonical)
    return vocabulary.FINANCE_EXHIBITS.get(name)
