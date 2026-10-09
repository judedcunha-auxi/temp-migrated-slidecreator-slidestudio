"""The per-slide design brief: a small, checkable plan written before a body slide's HTML.

Design: `docs/SLIDE-BRIEF-DESIGN.md`. The design chat offers it as the `write_brief` tool when
`DESIGN_BRIEF_ENABLED` is on (`app.core.design.tool_schemas.brief_on`). It is pure Python except for `draft_brief`, which takes
the model call as an argument so tests (and a later wiring) inject it.

A brief fixes the decisions the design loop otherwise finds by trial and error (message, zones, the one hero,
the exhibit and its devices) and, above all, separates **facts** from **design**. Every figure the slide will
show is a `Fact` with provenance:

    user        the figure appears in the user's text or an attachment (a verbatim `quote` is kept)
    derived     computed from other facts by a stated `formula` (e.g. a peer median, a fitted line)
    estimate    the user gave no figure: the designer's own knowledge or a reasonable estimate
    verify      (legacy, no longer offered) unknown: the slide showed "[verify]" in its place

`validate(brief, sources)` is the deterministic gate: numbers must be grounded in `sources`, every
component and chart kind must exist in this build, devices the engine cannot draw are rewritten or refused,
and the brief stays under a length cap. It returns findings; an `error` finding means "fix the brief before
designing".

    brief = Brief.from_dict(payload)
    findings = validate(brief, [user_text, attachment_text])
    text = render_for_designer(brief, findings)      # what the design model reads (compact)
    checks = critic_checks(brief)                    # what the critic verifies on the render
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

log = logging.getLogger(__name__)

try:  # the component registry is optional, like everywhere else in design_refs
    from app.core.design_refs import components as _components
except Exception:  # noqa: BLE001
    log.warning("component library could not be imported", exc_info=True)
    _components = None  # type: ignore[assignment]

try:
    from app.core.design_refs import exhibit_plan as _exhibit_plan
except Exception:  # noqa: BLE001
    log.warning("exhibit plan could not be imported", exc_info=True)
    _exhibit_plan = None  # type: ignore[assignment]

VERIFY = "[verify]"

#: The native `data-chart` kinds the design prompt advertises (`prompts.CHART_TYPES`). `combo` exists in
#: the engine's table but is refused as round 2; bubble, radar, stock and surface draw nothing.
_FALLBACK_CHART_TYPES = ("column", "column_stacked", "column_stacked_100", "bar", "bar_stacked",
                         "bar_stacked_100", "line", "line_markers", "area", "area_stacked", "pie",
                         "pie_exploded", "doughnut", "doughnut_exploded", "scatter", "scatter_lines",
                         "waterfall")

#: Chart kinds that look plausible in a brief but cannot be exported, with the feasible substitute.
REFUSED_CHARTS = {
    "combo": "two aligned charts (a column chart above a line chart on the same categories), or bar_table",
    "bubble": "scatter with the third measure in per-point labels, or matrix_2x2 with SVG circles",
    "radar": "harvey_table or bar_table (one row per criterion)",
    "stock": "football_field (low-high rows)",
    "surface": "heatmap_table",
    "treemap": "mekko or share_bar",
    "sunburst": "share_bar with a breakdown list",
    "histogram": "column chart of binned counts",
    "box": "football_field with a reference line",
}

#: Device phrases the engine cannot draw as written -> (severity, the feasible rewrite).
#: Source: docs/DESIGN-DEVICES-RESEARCH.md §2 and §4.2 (no chart-anchored annotations, no combo, no
#: secondary axis; referenceLines are value-axis lines only and need valueAxis min/max).
DEVICE_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"fitted|trend ?line|regression|line of best fit|best-fit", re.I), "info",
     "draw the fitted line as a second `scatter_lines` series of two points computed by `fit_line` from the "
     "grounded points (a derived fact); it stays anchored to the axes"),
    (re.compile(r"vertical (reference )?(line|band)|x-axis (line|band)|band for", re.I), "warn",
     "referenceLines are horizontal (value axis) only: draw a vertical marker as a two-point `scatter_lines` "
     "series at that x; a shaded band is not native, use a thin line plus a label"),
    (re.compile(r"secondary axis|dual axis|twin axis|combo", re.I), "error",
     "no secondary axis (combo is refused): split into two aligned charts or put the rate in labels"),
    (re.compile(r"(callout|arrow|annotation|leader line|bracket).{0,40}(on|at|to|pointing)\b.{0,30}"
                r"(bar|point|dot|column|chart)", re.I), "warn",
     "chart-anchored annotations are not native and drift in PowerPoint: use per-point `dataLabels` text, "
     "`pointColors`, a `referenceLines` value line, or `keyed_markers` beside the chart"),
    (re.compile(r"\bmap\b|choropleth", re.I), "warn",
     "a map only if one is in the project assets; otherwise heatmap_table or a sorted bar chart by region"),
    (re.compile(r"\bicons?\b|logos?\b", re.I), "info",
     "logos and icons only from project assets; otherwise the name in a text chip"),
]

ROLES = ("lead_metrics", "main_exhibit", "side_exhibit", "side_panel", "takeaway", "context")
TIERS = ("hero", "primary", "support", "context")
PLACES = ("top", "left", "right", "bottom", "full", "centre")
SOURCES = ("user", "derived", "estimate", "verify")
#: The provenances the model is offered (`verify` is still read, for briefs saved before `estimate`).
OFFERED_SOURCES = ("user", "derived", "estimate")
EXHIBIT_KINDS = ("component", "chart", "table", "diagram", "text")

#: Limits that keep the brief a plan and not a second slide. Body slides are dense and composite (the
#: auxi proposal bar): 4-6 zones, at least `MIN_DRAWN` of them a drawn exhibit.
MAX_ZONES = 7
MIN_ZONES = 4
MIN_DRAWN = 3
DRAWN_KINDS = ("component", "chart", "table", "diagram")
MAX_FACTS = 60
MAX_CONTENT_ITEMS = 8
MAX_TITLE_CHARS = 160        # about two lines at title size on a 1280 px canvas
MAX_BRIEF_CHARS = 9000       # about 2.2k tokens serialised; `render_for_designer` is shorter still


# ============================================================================== schema


def _decoded(value: Any) -> Any:
    """A field a model sent JSON-encoded (`"zones": "[{...}]"`, which happens) as the value it encodes."""
    if isinstance(value, str) and value.strip()[:1] in ("[", "{"):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _objects(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """`data[key]` as a list of objects, or a ValueError the designer can repair from."""
    items = _decoded(data.get(key)) or []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise ValueError(f"'{key}' must be an array of objects, not {type(items).__name__}")
    out = []
    for n, item in enumerate(items, 1):
        item = _decoded(item)
        if not isinstance(item, dict):
            raise ValueError(f"'{key}' item {n} must be an object, not {type(item).__name__}")
        out.append(item)
    return out


@dataclass
class Fact:
    """One figure (or a named placeholder) the slide may show."""

    id: str                          # "F1"; zones refer to it as {F1}
    label: str                       # "Acme Power P/B"
    value: str                       # "0.3x", "4.3%"
    source: str = "user"             # user | derived | estimate (| verify, legacy)
    quote: str = ""                  # user: the verbatim span it came from
    formula: str = ""                # derived: e.g. "median(F3,F7,F11)"
    inputs: list[str] = field(default_factory=list)   # derived: the fact ids it uses
    as_of: str = ""                  # valuation date / period, when the user gave one


@dataclass
class Exhibit:
    """What a zone draws. `kind`: component | chart | table | diagram (native shapes/SVG) | text. `name`: a component name or a
    `data-chart` type. Devices: at most one insight device and one supporting device."""

    kind: str
    name: str = ""
    insight: str = ""
    support: str = ""
    facts: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class Zone:
    id: str
    role: str                        # one of ROLES
    place: str                       # one of PLACES (a coarse region; the grid decides pixels)
    tier: str                        # one of TIERS; exactly one hero per slide
    content: list[str] = field(default_factory=list)   # short lines; figures only as {F#} references
    exhibit: Exhibit | None = None


@dataclass
class Brief:
    audience: str
    objective: str                   # what the slide must prove, one sentence
    action_title: str                # may contain {F#} references
    comparison: str = ""             # a plan_exhibit comparison type (ranking, relationship, ...)
    zones: list[Zone] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)      # the user's rules (style, banned metrics, wording)
    footnote: str = ""
    source: str = ""
    skip: str = ""                   # non-empty: no brief is needed, and why (cover, divider, edit)
    slide: str = ""                  # the title the slide is saved under (or its id): matches brief to slide

    # ---------------------------------------------------------------- (de)serialisation

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Brief:
        if not isinstance(data, dict):
            raise ValueError("a brief is a JSON object")
        zones = []
        for z in _objects(data, "zones"):
            ex = _decoded(z.get("exhibit"))
            zones.append(Zone(
                id=str(z.get("id", "")), role=str(z.get("role", "")), place=str(z.get("place", "")),
                tier=str(z.get("tier", "")), content=[str(c) for c in z.get("content") or []],
                exhibit=Exhibit(**{k: ex[k] for k in ("kind", "name", "insight", "support", "facts", "notes")
                                   if k in ex}) if isinstance(ex, dict) else None))
        facts = [Fact(**{k: f[k] for k in ("id", "label", "value", "source", "quote", "formula", "inputs",
                                           "as_of") if k in f})
                 for f in _objects(data, "facts")]
        rules = _decoded(data.get("rules")) or []
        return cls(audience=str(data.get("audience", "")), objective=str(data.get("objective", "")),
                   action_title=str(data.get("action_title", "")), comparison=str(data.get("comparison", "")),
                   zones=zones, facts=facts, rules=[str(r) for r in (rules if isinstance(rules, list) else [rules])],
                   footnote=str(data.get("footnote", "")), source=str(data.get("source", "")),
                   skip=str(data.get("skip", "")), slide=str(data.get("slide", "")))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def fact(self, fid: str) -> Fact | None:
        return next((f for f in self.facts if f.id == fid), None)


#: The same schema as a JSON Schema, for a `write_brief` tool's `input_schema` or a structured-output call.
BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["slide", "audience", "objective", "action_title", "zones", "facts"],
    "properties": {
        "slide": {"type": "string", "description": "The exact title you will pass to save_slide for this slide "
                  "(or its slide_id when redesigning one): it ties the brief to its slide when you write several "
                  "briefs before saving."},
        "skip": {"type": "string", "description": "Set (with a reason) instead of a brief for a cover, divider, "
                 "agenda or a small edit; leave the other fields empty."},
        "audience": {"type": "string", "description": "Who reads it and what they are fluent in."},
        "objective": {"type": "string", "description": "What the slide must prove, one sentence."},
        "action_title": {"type": "string", "description": "Full-sentence so-what, <= 2 lines; figures as {F#}."},
        "comparison": {"type": "string", "description": "plan_exhibit comparison type, e.g. relationship."},
        "zones": {"type": "array", "maxItems": MAX_ZONES, "items": {
            "type": "object", "required": ["id", "role", "place", "tier"],
            "properties": {
                "id": {"type": "string"}, "role": {"type": "string", "enum": list(ROLES)},
                "place": {"type": "string", "enum": list(PLACES)}, "tier": {"type": "string", "enum": list(TIERS)},
                "content": {"type": "array", "maxItems": MAX_CONTENT_ITEMS, "items": {"type": "string"},
                            "description": "Short lines; every figure as a {F#} reference, never a literal."},
                "exhibit": {"type": "object", "required": ["kind"], "properties": {
                    "kind": {"type": "string", "enum": list(EXHIBIT_KINDS)},
                    "name": {"type": "string", "description": "Installed component, data-chart type, or for a diagram "
                             "its form (process flow, hub-and-spoke, tree, timeline, matrix)."},
                    "insight": {"type": "string", "description": "ONE device that points at the so-what."},
                    "support": {"type": "string", "description": "ONE supporting device, or empty."},
                    "facts": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "string"}}}}}},
        "facts": {"type": "array", "maxItems": MAX_FACTS, "items": {
            "type": "object", "required": ["id", "label", "value", "source"],
            "properties": {
                "id": {"type": "string", "description": "F1, F2, ..."}, "label": {"type": "string"},
                "value": {"type": "string", "description": "As given by the user, derived, or your estimate."},
                "source": {"type": "string", "enum": list(OFFERED_SOURCES)},
                "quote": {"type": "string", "description": "user: the verbatim span from the request."},
                "formula": {"type": "string"}, "inputs": {"type": "array", "items": {"type": "string"}},
                "as_of": {"type": "string"}}}},
        "rules": {"type": "array", "items": {"type": "string"}},
        "footnote": {"type": "string"}, "source": {"type": "string"},
    },
}


# ============================================================================== findings


@dataclass(frozen=True)
class Finding:
    severity: str        # error | warn | info
    code: str
    where: str
    message: str

    def line(self) -> str:
        return f"[{self.severity}] {self.code} at {self.where}: {self.message}"


def errors(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == "error"]


# ============================================================================== numbers

_REF = re.compile(r"\{(F\d+)\}")
#: A figure: optional sign/currency, digits with separators, optional decimals. Letters glued to the front
#: (F12, Q3, H1, FY27, 2x2) are not figures and are skipped by the look-behind.
_NUM = re.compile(r"(?<![A-Za-z0-9_.])[-−–+]?\(?(?:USD|EUR|SAR|AED|\$|€|£)?\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?!\d|\.\d|[x×]\d)")
_YEAR = re.compile(r"^(19|20)\d\d$")


def _canon(int_part: str, frac: str | None) -> str:
    """A figure's digits as a canonical decimal string: '1,200' -> '1200', '0.30' -> '0.3', '4.' -> '4'."""
    try:
        d = Decimal(int_part.replace(",", "") + (frac or ""))
    except InvalidOperation:
        return int_part
    text = format(d.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def numbers(text: str) -> list[str]:
    """The canonical figures in a text, {F#} references removed first."""
    return [_canon(m.group(1), m.group(2)) for m in _NUM.finditer(_REF.sub(" ", text or ""))]


def _source_numbers(sources: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for s in sources:
        out.update(numbers(s))
    return out


def _normal(text: str) -> str:
    return " ".join((text or "").split()).lower()


# ============================================================================== feasibility


def installed_components() -> set[str]:
    try:
        return set(_components.names()) if _components is not None else set()
    except Exception:  # noqa: BLE001
        return set()


def chart_types() -> set[str]:
    try:
        from app.core.design_refs.charts import CHART_TYPES  # the list the design model is told
        return {t.strip() for t in CHART_TYPES.split(",") if t.strip()}
    except Exception:  # noqa: BLE001
        log.debug("prompt chart types unavailable; using the fallback list", exc_info=True)
        return set(_FALLBACK_CHART_TYPES)


# ============================================================================== validation


def validate(brief: Brief, sources: Iterable[str], *, components: set[str] | None = None,
             charts: set[str] | None = None) -> list[Finding]:
    """Every problem with a brief, deterministic and free. `sources`: the user's message(s), text from
    attachments, and a spec's bullets: the only places a figure may come from."""
    sources = [s for s in sources if s]
    comps = installed_components() if components is None else components
    kinds = chart_types() if charts is None else charts
    out: list[Finding] = []
    if brief.skip:
        return out
    grounded = _source_numbers(sources)
    corpus = _normal(" ".join(sources))

    # -- structure
    if not brief.action_title.strip():
        out.append(Finding("error", "no-title", "action_title", "write the action title"))
    rendered_title = render_text(brief.action_title, brief)
    if len(rendered_title) > MAX_TITLE_CHARS:
        out.append(Finding("warn", "title-long", "action_title",
                           f"{len(rendered_title)} chars; keep it to two lines (<= {MAX_TITLE_CHARS})"))
    if not _REF.search(brief.action_title) and not re.search(r"\d", brief.action_title) and any(
            f.source == "user" for f in brief.facts):
        out.append(Finding("warn", "title-no-number", "action_title",
                           "the title carries no figure; reference the fact that proves it"))
    if not brief.zones:
        out.append(Finding("error", "no-zones", "zones", "a body slide needs at least one zone"))
    if len(brief.zones) > MAX_ZONES:
        out.append(Finding("error", "too-many-zones", "zones", f"{len(brief.zones)} zones; at most {MAX_ZONES}"))
    drawn = [z for z in brief.zones if z.exhibit is not None and z.exhibit.kind in DRAWN_KINDS]
    if brief.zones and len(drawn) < MIN_DRAWN:
        out.append(Finding("error", "thin-slide", "zones",
                           f"{len(drawn)} drawn exhibit(s); a body slide carries at least {MIN_DRAWN} (charts, "
                           f"tables, diagrams, KPI tiles, components) plus the takeaway, filling the canvas"))
    elif brief.zones and len(brief.zones) < MIN_ZONES:
        out.append(Finding("warn", "few-zones", "zones",
                           f"{len(brief.zones)} zones; compose {MIN_ZONES}-6 (the exhibits, a side panel, a takeaway)"))
    heroes = [z.id for z in brief.zones if z.tier == "hero"]
    if len(heroes) != 1 and brief.zones:
        out.append(Finding("warn", "hero-count", "zones",
                           f"exactly one hero zone reads as a hierarchy; found {len(heroes)} ({', '.join(heroes) or 'none'})"))
    for z in brief.zones:
        where = f"zones[{z.id}]"
        for attr, allowed in (("role", ROLES), ("place", PLACES), ("tier", TIERS)):
            if getattr(z, attr) not in allowed:
                out.append(Finding("error", f"bad-{attr}", where, f"{getattr(z, attr)!r} is not one of {allowed}"))
        if len(z.content) > MAX_CONTENT_ITEMS:
            out.append(Finding("warn", "zone-dense", where, f"{len(z.content)} lines; at most {MAX_CONTENT_ITEMS}"))
        if z.exhibit is not None:
            out.extend(_check_exhibit(z, comps, kinds, brief))

    # -- facts: ids, provenance, grounding
    ids = [f.id for f in brief.facts]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        out.append(Finding("error", "duplicate-fact", dup, "fact ids must be unique"))
    if len(brief.facts) > MAX_FACTS:
        out.append(Finding("error", "too-many-facts", "facts", f"{len(brief.facts)}; at most {MAX_FACTS}"))
    for f in brief.facts:
        out.extend(_check_fact(f, brief, grounded, corpus))

    # -- references and literal figures in the fields the slide will show
    known = set(ids)
    shown = [("action_title", brief.action_title), ("footnote", brief.footnote), ("source", brief.source)]
    for z in brief.zones:
        shown += [(f"zones[{z.id}].content", c) for c in z.content]
        if z.exhibit is not None:
            shown += [(f"zones[{z.id}].exhibit.insight", z.exhibit.insight),
                      (f"zones[{z.id}].exhibit.support", z.exhibit.support)]
            for fid in z.exhibit.facts:
                if fid not in known:
                    out.append(Finding("error", "unknown-fact", f"zones[{z.id}].exhibit", f"{fid} is not a fact"))
    for where, text in shown:
        for fid in _REF.findall(text or ""):
            if fid not in known:
                out.append(Finding("error", "unknown-fact", where, f"{{{fid}}} is not a fact"))
        for num in numbers(text or ""):
            if num in grounded:
                continue
            out.append(Finding("warn", "literal-number", where,
                               f"{num} is written out; make it a fact ({{F#}}) so the slide shows one value"))

    # -- rules the user set that a validator can check
    rules = " ".join(brief.rules).lower()
    if "em-dash" in rules or "em dash" in rules:
        for where, text in shown:
            if "—" in (text or ""):
                out.append(Finding("warn", "em-dash", where, "the user's rules ban em-dashes"))

    # -- cross-check with plan_exhibit (the deterministic classifier stays the second opinion)
    out.extend(cross_check_plan(brief))

    # -- length cap
    size = len(json.dumps(brief.to_dict(), ensure_ascii=False))
    if size > MAX_BRIEF_CHARS:
        out.append(Finding("error", "brief-long", "brief",
                           f"{size} chars serialised; cap {MAX_BRIEF_CHARS}. A brief plans the slide, it is not the slide"))
    return out


def _check_fact(f: Fact, brief: Brief, grounded: set[str], corpus: str) -> list[Finding]:
    where = f"facts[{f.id}]"
    out: list[Finding] = []
    if not re.fullmatch(r"F\d+", f.id or ""):
        out.append(Finding("error", "bad-fact-id", where, "fact ids look like F1, F2, ..."))
    if f.source not in SOURCES:
        out.append(Finding("error", "bad-source", where, f"source {f.source!r} is not one of {SOURCES}"))
        return out
    nums = numbers(f.value)
    if f.source == "verify":
        if nums or VERIFY not in f.value:
            out.append(Finding("error", "verify-with-number", where,
                               f"an unknown figure is written {VERIFY}, never a guess ({f.value!r})"))
        return out
    if f.source == "user":
        missing = [n for n in nums if n not in grounded]
        if missing:
            out.append(Finding("warn", "ungrounded-fact", where,
                               f"{', '.join(missing)} does not appear in the user's material; mark it "
                               f"source=estimate"))
        if f.quote and _normal(f.quote) not in corpus:
            out.append(Finding("warn", "quote-not-found", where, "the quote is not verbatim in the user's material"))
        if not nums and VERIFY not in f.value:
            out.append(Finding("info", "non-numeric-fact", where, "a qualitative fact; fine if the user said it"))
        return out
    if f.source == "estimate":
        return out
    # derived
    if not f.formula or not f.inputs:
        out.append(Finding("error", "derived-without-formula", where, "a derived figure names its formula and inputs"))
        return out
    for fid in f.inputs:
        src = brief.fact(fid)
        if src is None:
            out.append(Finding("error", "unknown-fact", where, f"input {fid} is not a fact"))
        elif src.source == "verify":
            out.append(Finding("error", "derived-from-verify", where,
                               f"input {fid} is {VERIFY}; a figure computed from an unknown is unknown"))
    recomputed = recompute(f, brief)
    if recomputed is not None and nums and abs(recomputed - float(nums[0])) > max(0.051, abs(recomputed) * 0.01):
        out.append(Finding("error", "derived-mismatch", where,
                           f"{f.formula} gives {recomputed:.4g}, not {nums[0]}"))
    return out


def _check_exhibit(z: Zone, comps: set[str], kinds: set[str], brief: Brief) -> list[Finding]:
    ex = z.exhibit
    where = f"zones[{z.id}].exhibit"
    out: list[Finding] = []
    if ex is None:
        return out
    name = (ex.name or "").strip()
    if ex.kind == "component":
        if name not in comps:
            out.append(Finding("error", "unknown-component", where,
                               f"{name!r} is not an installed component ({len(comps)} installed)"))
    elif ex.kind == "chart":
        base = name.split("_")[0] if name not in kinds else name
        if name in kinds:
            pass
        elif base in REFUSED_CHARTS or name in REFUSED_CHARTS:
            out.append(Finding("error", "refused-chart", where,
                               f"`{name}` cannot be exported; use {REFUSED_CHARTS.get(name) or REFUSED_CHARTS[base]}"))
        else:
            out.append(Finding("error", "unknown-chart", where, f"`{name}` is not a data-chart type"))
        # a native chart plots numbers: points that are [verify] cannot be drawn
        values = [brief.fact(fid) for fid in ex.facts]
        unknown = [f for f in values if f is not None and f.source == "verify"]
        if values and len(unknown) * 2 > len(values):
            out.append(Finding("warn", "chart-needs-data", where,
                               f"{len(unknown)} of {len(values)} plotted figures are {VERIFY}; a chart cannot plot "
                               f"them. Plot only the grounded points and say which are missing, or use a table "
                               f"with {VERIFY} cells"))
    elif ex.kind not in EXHIBIT_KINDS:
        out.append(Finding("error", "bad-exhibit-kind", where, f"{ex.kind!r} is not one of {EXHIBIT_KINDS}"))
    for label, text in (("insight", ex.insight), ("support", ex.support), ("notes", ex.notes)):
        for rx, severity, fix in DEVICE_RULES:
            if text and rx.search(text):
                out.append(Finding(severity, "device-feasibility", f"{where}.{label}", fix))
    for label, text in (("insight", ex.insight), ("support", ex.support)):
        if text and len(re.split(r";|\band also\b|\bplus\b", text)) > 2:
            out.append(Finding("warn", "device-count", f"{where}.{label}",
                               "one device per slot: two devices done well read as craft, five as clutter"))
    return out


def cross_check_plan(brief: Brief) -> list[Finding]:
    """Ask `plan_exhibit.classify` what the title implies; a disagreement is advice, never an error."""
    if _exhibit_plan is None or not brief.action_title:
        return []
    kind, why = _exhibit_plan.classify(render_text(brief.action_title, brief))
    if brief.comparison and brief.comparison != kind:
        return [Finding("info", "plan-disagrees", "comparison",
                        f"plan_exhibit reads the title as '{kind}' ({why}); the brief says "
                        f"'{brief.comparison}'. Keep the brief's choice only if the content calls for it")]
    return []


# ============================================================================== derived figures


def _value(f: Fact | None) -> float | None:
    if f is None:
        return None
    nums = numbers(f.value)
    return float(nums[0]) if nums else None


def recompute(f: Fact, brief: Brief) -> float | None:
    """Recompute a derived fact for the formulas the validator knows: median, mean, sum, diff, ratio."""
    m = re.fullmatch(r"\s*(median|mean|sum|diff|ratio)\s*\(([^)]*)\)\s*", f.formula or "", re.I)
    if not m:
        return None
    found = [_value(brief.fact(x.strip())) for x in m.group(2).split(",") if x.strip()]
    vals = [v for v in found if v is not None]
    if not vals or len(vals) != len(found):
        return None
    op = m.group(1).lower()
    if op == "median":
        s = sorted(vals)
        k = len(s)
        return s[k // 2] if k % 2 else (s[k // 2 - 1] + s[k // 2]) / 2
    if op == "mean":
        return sum(vals) / len(vals)
    if op == "sum":
        return sum(vals)
    if op == "diff" and len(vals) == 2:
        return vals[0] - vals[1]
    if op == "ratio" and len(vals) == 2 and vals[1]:
        return vals[0] / vals[1]
    return None


def fit_line(points: list[tuple[float, float]]) -> dict[str, Any] | None:
    """Least-squares line through grounded (x, y) points, as the two endpoints a `scatter_lines` series
    draws (the engine has no native trendline). None under three points or with no x spread."""
    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) < 3:
        return None
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    if sxx == 0:
        return None
    slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx
    icpt = my - slope * mx
    ss_tot = sum((p[1] - my) ** 2 for p in pts)
    ss_res = sum((p[1] - (icpt + slope * p[0])) ** 2 for p in pts)
    lo, hi = min(p[0] for p in pts), max(p[0] for p in pts)
    return {"slope": slope, "intercept": icpt, "r2": (1 - ss_res / ss_tot) if ss_tot else 1.0, "n": n,
            "series": {"name": "Fitted line", "x": [lo, hi], "y": [icpt + slope * lo, icpt + slope * hi]}}


# ============================================================================== rendering


def render_text(text: str, brief: Brief) -> str:
    """{F#} references replaced by the facts' values (unknown ids left as they are)."""
    def sub(m: re.Match[str]) -> str:
        f = brief.fact(m.group(1))
        return f.value if f else m.group(0)
    return _REF.sub(sub, text or "")


def render_for_designer(brief: Brief, findings: Iterable[Finding] = ()) -> str:
    """The compact text the design model works from (a tool result, not the system prompt)."""
    if brief.skip:
        return f"No brief: {brief.skip}"
    lines = [f"Brief for: {render_text(brief.action_title, brief)}",
             f"- Audience: {brief.audience}", f"- Must prove: {brief.objective}"]
    if brief.comparison:
        lines.append(f"- Comparison: {brief.comparison}")
    for z in brief.zones:
        head = f"- Zone {z.id} ({z.role}, {z.place}, {z.tier})"
        if z.exhibit:
            ex = z.exhibit
            head += f": {ex.kind} {ex.name}".rstrip()
            if ex.insight:
                head += f"; insight: {ex.insight}"
            if ex.support:
                head += f"; support: {ex.support}"
        lines.append(head)
        lines.extend(f"    · {render_text(c, brief)}" for c in z.content)
    shown = [f for f in brief.facts if f.source != "verify"]
    unknown = [f for f in brief.facts if f.source == "verify"]
    if shown:
        lines.append("- Facts (use exactly): " + "; ".join(f"{f.id} {f.label} = {f.value}"
                                                          + (f" ({f.source})" if f.source in ("derived", "estimate") else "")
                                                          for f in shown))
    if unknown:
        lines.append(f"- Unknown, show {VERIFY}: " + "; ".join(f"{f.id} {f.label}" for f in unknown))
    if brief.rules:
        lines.append("- Rules: " + "; ".join(brief.rules))
    if brief.footnote or brief.source:
        lines.append(f"- Footnote/source: {render_text(' '.join(x for x in (brief.footnote, brief.source) if x), brief)}")
    lines.append("- Density: build every zone above, filling the body from the title to the source line (no empty "
                 "band, no hollow panel); body type 10.5-12pt; 250-450 words.")
    notes = [f.line() for f in findings if f.severity != "info"]
    if notes:
        lines.append("- Validator: " + " | ".join(notes[:8]))
    return "\n".join(lines)


def critic_items(brief: Brief, limit: int = 5) -> list[tuple[str, str]]:
    """Up to `limit` yes/no checks the critic can make against the render, each with the severity a NO
    earns: a missing hero or a figure outside the brief is a `must`; the title and devices are `nice`.
    The grounding check always comes, and comes last."""
    if brief.skip:
        return []
    out = [(f"The title reads: \"{render_text(brief.action_title, brief)}\" (or a tighter version with the "
            f"same figure).", "nice")]
    hero = next((z for z in brief.zones if z.tier == "hero"), None)
    if hero:
        what = (hero.exhibit.name if hero.exhibit and hero.exhibit.name else hero.role).replace("_", " ")
        out.append((f"The hero is the {what} ({hero.place}); it is the one solid-filled or accent unit.", "must"))
    for z in brief.zones:
        if z.exhibit and z.exhibit.insight and len(out) < limit - 1:
            out.append((f"Zone {z.id} shows its insight device: {z.exhibit.insight}.", "nice"))
    values = [f.value for f in brief.facts if f.source != "verify"]
    shown = ", ".join(values[:24]) + (", …" if len(values) > 24 else "")
    grounding = f"Every figure on the slide is one of the brief's facts ({shown or 'none'})"
    if any(f.source == "verify" for f in brief.facts):
        grounding += f"; unknown ones show {VERIFY}"
    out = out[:limit - 1] + [(grounding + ". Years, counts and page numbers do not count.", "must")]
    return out


def critic_checks(brief: Brief, limit: int = 5) -> list[str]:
    """Up to `limit` yes/no checks the critic can make against the render (passed in its context)."""
    return [line for line, _ in critic_items(brief, limit)]


# ============================================================================== skip heuristic

_SKIP_TYPES = {"cover", "title", "divider", "section", "agenda", "contents", "closing", "thank you", "quote"}
_EDIT_VERBS = re.compile(r"^\s*(please\s+)?(make|move|change|fix|shorten|lengthen|bold|align|swap|rename|"
                         r"recolou?r|resize|nudge|delete|remove|replace|tighten|reword|undo|increase|decrease)\b",
                         re.I)


def should_brief(request: str, *, slide_type: str | None = None, selected: str | None = None) -> tuple[bool, str]:
    """Whether a turn earns a brief. Covers, dividers, agendas and small edits to an existing slide do not."""
    st = (slide_type or "").strip().lower()
    if st in _SKIP_TYPES:
        return False, f"a {st} slide carries no exhibit"
    words = len((request or "").split())
    if selected and _EDIT_VERBS.search(request or "") and words < 40:
        return False, "an edit to an existing slide"
    if words < 8 and not re.search(r"\d", request or ""):
        return False, "too little content to plan"
    return True, "a body slide"


# ============================================================================== drafting (model stubbed)

BRIEF_INSTRUCTION = """Write the design brief for ONE consulting slide as JSON matching the schema. Decide the
message, the zones and the facts. The slide is DENSE and composite, like a top-tier consulting working page:
{min_zones}-6 zones (at most {max_zones}; exactly one hero) filling the canvas from title to source line, with at
least {min_drawn} drawn exhibits: charts (native data-chart types below), tables, diagrams drawn with native
shapes/SVG (process flow, hub-and-spoke, tree, timeline, value chain, matrix) and installed components (KPI tiles,
harvey balls, gantt, ...), plus a side panel and a takeaway strip. Mix exhibit types (e.g. a hero chart, a KPI
strip, a comparison table and a small process diagram). Each exhibit has ONE insight device and ONE supporting
device; each zone lists its content (3-8 short lines, a bold lead-in plus evidence). Aim for 250-450 words.

Facts: copy every figure from the user's material into `facts` with source "user" and the verbatim `quote`.
When the user gave no figure for something the slide needs, use your own knowledge or a reasonable estimate
with source "estimate", so charts and exhibits have real values to draw. A figure you compute (a gap, a share, a growth rate) is "derived", never "user". A derived figure (median, mean, sum, diff, ratio) names its formula and inputs. Everywhere else
refer to figures as {{F#}}; never write a number outside `facts`. When the user gives the title,
keep it verbatim and put the proof figure in a zone instead.

Components: {components}
Chart types: {charts}
Not available: chart-anchored callouts or arrows, combo/secondary axis, bubble, radar, shaded bands,
native trendlines (a fitted line is a second scatter_lines series of two computed points).
Keep the brief under {max_chars} characters. For a cover, divider, agenda or a small edit, return
{{"skip": "<why>"}} and nothing else.
"""


def instruction(components: set[str] | None = None, charts: set[str] | None = None) -> str:
    comps = sorted(installed_components() if components is None else components)
    kinds = sorted(chart_types() if charts is None else charts)
    return BRIEF_INSTRUCTION.format(max_zones=MAX_ZONES, min_zones=MIN_ZONES, min_drawn=MIN_DRAWN,
                                    components=", ".join(comps) or "(none installed)",
                                    charts=", ".join(kinds), max_chars=MAX_BRIEF_CHARS)


#: The `write_brief` tool's description in the design chat: the drafting instruction plus what comes back.
TOOL_PREFIX = ("Plan a NEW body slide that shows figures or an exhibit before writing its HTML (deterministic "
               "and free). ")
TOOL_SUFFIX = ("The result is the brief as you will design it, the validator's findings and the exhibit plan. "
               "If it reports errors, fix every one and call write_brief once more; design from the brief it "
               "accepts, and show only its facts.")


def tool_description(components: set[str] | None = None, charts: set[str] | None = None) -> str:
    return TOOL_PREFIX + " ".join(instruction(components, charts).split()) + " " + TOOL_SUFFIX


def _parse_json(text: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("the model returned no JSON object")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("the model returned no JSON object")
    return data


def draft_brief(request: str, call_model: Callable[[str, str], str], *, sources: Iterable[str] = (),
                components: set[str] | None = None, charts: set[str] | None = None,
                repairs: int = 1) -> tuple[Brief, list[Finding]]:
    """One model call (injected: `call_model(system, user) -> text`), then the validator; when it finds
    errors, one repair call with the findings. Returns the last brief and its findings."""
    system = instruction(components, charts)
    srcs = [request, *sources]
    user = request
    brief = Brief(audience="", objective="", action_title="")
    findings: list[Finding] = []
    for _attempt in range(repairs + 1):
        brief = Brief.from_dict(_parse_json(call_model(system, user)))
        findings = validate(brief, srcs, components=components, charts=charts)
        if not errors(findings):
            break
        user = (request + "\n\nYour previous brief failed validation. Fix every error and return the whole "
                "brief again:\n" + "\n".join(f.line() for f in errors(findings)) + "\n\nPrevious brief:\n"
                + json.dumps(brief.to_dict(), ensure_ascii=False))
    return brief, findings


# ============================================================================== post-render check


def ungrounded_in_slide(visible_text: str, brief: Brief | None, sources: Iterable[str],
                        figures: Iterable[str] = ()) -> list[str]:
    """Figures on the saved slide that are in neither the brief's facts nor the user's material: the
    after-the-fact half of the grounding guard (run on `chat.briefs.visible_text(html)` at save time, with
    `figures` = `chart_figures(html)`). Years and small counts (<= 12) are left out; they are checked, as
    warnings, in the brief. A figure that rounds a grounded one (4.33 shown as 4.3) is grounded."""
    allowed = _source_numbers(sources)
    if brief is not None:
        for f in brief.facts:
            if f.source != "verify":
                allowed.update(numbers(f.value))
    out = []
    for num in [*numbers(visible_text), *figures]:
        if num in allowed or _YEAR.match(num) or (num.isdigit() and int(num) <= 12) or _rounds_from(num, allowed):
            continue
        if num not in out:
            out.append(num)
    return out


def _rounds_from(num: str, allowed: set[str]) -> bool:
    """Is `num` a rounding of a figure in `allowed` that has more decimals?"""
    try:
        target = Decimal(num)
    except InvalidOperation:
        return False
    exponent = target.as_tuple().exponent
    places = -exponent if "." in num and isinstance(exponent, int) else 0
    step = Decimal(1).scaleb(-places)
    for a in allowed:
        if "." in a and len(a.split(".")[1]) > places:
            try:
                if Decimal(a).quantize(step, rounding=ROUND_HALF_UP) == target:
                    return True
            except InvalidOperation:
                continue
    return False


_CHART_ATTR = re.compile(r"""data-chart\s*=\s*(?:'([^']*)'|"([^"]*)")""", re.I)


def chart_figures(html: str) -> list[str]:
    """The canonical figures a slide's native charts plot: every number in their series (values, x/y, own
    data labels) and category names, and their reference lines. Axis bounds, sizes and colours are layout,
    not figures, and are left out. A chart whose JSON does not parse adds nothing."""
    import html as html_lib

    out: list[str] = []

    def leaves(node: Any) -> None:
        if isinstance(node, bool) or node is None:
            return
        if isinstance(node, (int, float)):
            text = format(Decimal(repr(abs(node))).normalize(), "f")
            out.append(text.rstrip("0").rstrip(".") if "." in text else text)
        elif isinstance(node, str):
            out.extend(numbers(node))
        elif isinstance(node, list):
            for item in node:
                leaves(item)
        elif isinstance(node, dict):
            for key, item in node.items():
                if key not in ("name", "color", "colors", "type"):
                    leaves(item)

    for m in _CHART_ATTR.finditer(html or ""):
        try:
            spec = json.loads(html_lib.unescape(m.group(1) if m.group(1) is not None else m.group(2)))
        except (ValueError, TypeError):
            continue
        if not isinstance(spec, dict):
            continue
        leaves(spec.get("series"))
        leaves(spec.get("categories"))
        leaves((spec.get("options") or {}).get("referenceLines") if isinstance(spec.get("options"), dict) else None)
    return out


# ============================================================================== spec mode: the pre-filled half

#: A unit glued after a figure ("34%", "$8.2B", "340bp", "0.3x", "12 pts"): kept in the fact's value.
_UNIT = re.compile(r"\s?(?:%|bps?\b|pts?\b|[xX×](?![A-Za-z0-9])|bn\b|[BMKbmk]\b)")


def figures_in(text: str) -> list[str]:
    """The figures in a text as written, with their currency and unit ("$57.1B", "34%"). Years and bare
    counts up to 12 are left out, as they are everywhere else in the grounding check."""
    out = []
    for m in _NUM.finditer(_REF.sub(" ", text or "")):
        canon = _canon(m.group(1), m.group(2))
        if _YEAR.match(canon) or (canon.isdigit() and int(canon) <= 12 and not text[m.end():m.end() + 1] == "%"):
            continue
        unit = _UNIT.match(text, m.end())
        out.append((m.group(0) + (unit.group(0) if unit else "")).strip())
    return out


def prefill_from_spec(title: str, bullets: Iterable[str] = (), *, audience: str = "", objective: str = "",
                      slide_type: str | None = None, framework: str | None = None,
                      chart: dict[str, Any] | None = None) -> dict[str, Any]:
    """The half of a spec-mode brief that needs no model: the title verbatim, the audience and objective,
    every figure in the bullets (and the chart data) as a `user` fact quoting its bullet, the comparison
    and the hero zone's exhibit from `plan_exhibit`. The designer completes it with `write_brief`."""
    facts: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(label: str, value: str, quote: str) -> None:
        key = "|".join(numbers(value))
        if not key or key in seen or len(facts) >= MAX_FACTS:
            return
        seen.add(key)
        facts.append({"id": f"F{len(facts) + 1}", "label": label[:80], "value": value, "source": "user",
                      "quote": quote[:200]})

    for bullet in bullets:
        for value in figures_in(bullet):
            add(bullet, value, bullet)
    if isinstance(chart, dict):
        cats = [str(c) for c in chart.get("categories") or []]
        for s in chart.get("series") or []:
            if not isinstance(s, dict):
                continue
            for i, v in enumerate(s.get("values") or []):
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    label = " ".join(x for x in (str(s.get("name") or ""), cats[i] if i < len(cats) else "") if x)
                    add(label or "chart value", format(v, "g"), "")

    comparison, exhibit = "", None
    if _exhibit_plan is not None:
        comparison, _ = _exhibit_plan.classify(title, slide_type, framework)
        plan = _exhibit_plan.PLANS.get(comparison)
        insight = plan.insight.split(";")[0].strip() if plan else ""
        kinds = chart_types()
        name = (plan.exhibit if plan else "").strip("`").split(" ")[0]
        if isinstance(chart, dict):
            ctype = str(chart.get("type") or "")
            name = ctype if ctype in kinds else (name if name in kinds else "column")
            exhibit = {"kind": "chart", "name": name, "insight": insight}
        elif plan and plan.exhibit.startswith("`") and name in kinds:
            exhibit = {"kind": "chart", "name": name, "insight": insight}
        elif name and name in installed_components():
            exhibit = {"kind": "component", "name": name, "insight": insight}
    hero: dict[str, Any] = {"id": "Z1", "role": "main_exhibit", "place": "left", "tier": "hero", "content": []}
    if exhibit:
        hero["exhibit"] = exhibit
    return {"audience": audience, "objective": objective, "action_title": title, "comparison": comparison,
            "zones": [hero], "facts": facts}
