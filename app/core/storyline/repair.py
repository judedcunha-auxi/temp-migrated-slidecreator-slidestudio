"""Coerce, don't reject: every storyline, fresh from the model or edited by a person, goes through `repair`.

Merged from Darwin `_shared/frameworkGuard.ts` + `_shared/dividers.ts` and Slide Studio
`server/storyline/repair.py` (itself a port of the same two files). Every change is reported as a warning
("Slide N: ..."); nothing here fails a storyline.

Common to both densities (the frameworkGuard core):
* a body slide's framework goes through `coerce_framework`: exact name or alias (silent), the coercion table
  (warned), a 3D/isometric prefix stripped and the lookup retried, a fuzzy match, the slide type's default;
* an `archetypeId` the library does not know is dropped with a warning; a Darwin-era id is rewritten to the
  library's id (silently: it is the same archetype);
* section dividers are inserted (Darwin `insertDividers`) when the mode has them.

`standard` keeps Darwin's rules: chart data only on a chart framework (dropped with a warning elsewhere),
furniture untouched. `dense` keeps Slide Studio's: chart data on any body slide (a composite slide carries a
chart as one exhibit of several), none on furniture; an empty framework gets the type default; fewer than 3
bullets is a warning; an archetype is not repeated within 3 slides; an empty agenda lists the sections.

Reviewed for D0 (OCR retires; see docs/architecture.md, "frameworkGuard after OCR"): the chart-framework set
no longer comes from the OCR detection metadata (`slideContext.ts: FRAMEWORK_META`), it is
`vocabulary.CHART_FRAMEWORKS`; and the chart-data strip is a standard-mode (image prompt) rule only.
"""

from __future__ import annotations

import re

from app.core.storyline import archetypes
from app.core.storyline.models import Storyline, StorylineSlide
from app.core.storyline.vocabulary import (
    CHART_FRAMEWORKS,
    FURNITURE,
    MAX_BULLETS,
    MIN_BODY_BULLETS,
    NO_SECTION_TYPES,
    canonical_framework,
    framework_aliases,
    framework_names,
    normalize_framework_name,
)

#: Asks that native PowerPoint shapes, tables and charts cannot build (normalised) -> the nearest library
#: framework. Darwin `COERCIONS`, unchanged.
COERCIONS: dict[str, str] = {
    # Radial polygon charts: the HTML engine has no native radar (chart_model PATH_A_UNSUPPORTED_PREFIXES).
    "spider chart": "weighted scoring matrix",
    "spider diagram": "weighted scoring matrix",
    "spider web chart": "weighted scoring matrix",
    "radar chart": "weighted scoring matrix",
    "radar diagram": "weighted scoring matrix",
    # Organic branch layouts -> the disciplined radial framework.
    "mind map": "hub-and-spoke",
    "mindmap": "hub-and-spoke",
    "network diagram": "hub-and-spoke",
    # Ribbon flows -> the nearest grouping/flow framework.
    "sankey": "many-to-one grouping map",
    "sankey chart": "many-to-one grouping map",
    "sankey diagram": "many-to-one grouping map",
    # Free-form keyword art -> the keyword-highlight framework.
    "word cloud": "logo rows with color-coded keyword highlighting",
    "tag cloud": "logo rows with color-coded keyword highlighting",
    # "Infographic" is a style ask, not a framework.
    "infographic": "icon card grid",
    # Gauges -> oversized-number stat tiles.
    "gauge": "KPI tiles",
    "gauge chart": "KPI tiles",
    "speedometer": "KPI tiles",
    "speedometer chart": "KPI tiles",
    "dial chart": "KPI tiles",
    # Pie/donut -> the library's composition chart (the library has no pie entry).
    "pie": "100% stacked bar chart",
    "pie chart": "100% stacked bar chart",
    "donut": "100% stacked bar chart",
    "donut chart": "100% stacked bar chart",
    "doughnut chart": "100% stacked bar chart",
}

#: Last resort per slide type when a framework matches nothing (Darwin `TYPE_DEFAULT_FRAMEWORK`). Furniture and
#: `quote` have none, so an unmatched name passes through there.
TYPE_DEFAULT_FRAMEWORK: dict[str, str] = {
    "framework": "icon card grid",
    "data": "100% stacked bar chart",
    "process": "chevron process flow",
    "comparison": "mirrored two-panel comparison",
    "timeline": "milestone timeline",
    "kpi": "KPI tiles",
    "market": "TAM/SAM/SOM concentric circles",
    "org": "org chart",
    "table": "attribute comparison table",
    "case-study": "left identity panel + numbered list profile",
    "charter": "initiative charter card",
    "synthesis": "best-practices-to-recommendations panels",
    "benchmark": "benchmark comparison table",
    "scorecard": "single-option criteria scorecard",
    "deep-dive": "stage deep-dive breadcrumb",
    "map": "annotated geographic map with callouts",
}

#: Tokens too generic to establish a fuzzy match on their own ("line chart" must not become "gantt chart").
GENERIC_TOKENS: frozenset[str] = frozenset(
    {"chart", "diagram", "matrix", "table", "map", "grid", "graph", "with", "and", "the", "of", "a"})

#: Below this many slides a deck is one implicit section: no dividers (Darwin `MIN_SLIDES_FOR_DIVIDERS`).
MIN_SLIDES_FOR_DIVIDERS = 8
#: Dense: an archetype may not repeat within this many slides (the prompt's rule, enforced).
ARCHETYPE_GAP = 3
#: The pseudo-framework a divider carries (Darwin `makeDivider`).
DIVIDER_FRAMEWORK = "section divider"

_FUZZY: list[tuple[frozenset[str], str]] | None = None


def _fuzzy_candidates() -> list[tuple[frozenset[str], str]]:
    global _FUZZY
    if _FUZZY is None:
        _FUZZY = [(frozenset(normalize_framework_name(n).split()), n) for n in framework_names()]
        _FUZZY += [(frozenset(normalize_framework_name(a).split()), c) for a, c in framework_aliases().items()]
    return _FUZZY


def fuzzy_match(norm: str) -> str | None:
    """Nearest library entry by token overlap (Jaccard >= 0.5) sharing at least one non-generic token.
    Ties keep the first candidate, in Darwin's order."""
    tokens = set(norm.split())
    best: str | None = None
    best_score = 0.0
    for cand, canonical in _fuzzy_candidates():
        shared = cand & tokens
        if not any(t not in GENERIC_TOKENS for t in shared):
            continue
        score = len(shared) / len(tokens | cand)
        if score > best_score:
            best, best_score = canonical, score
    return best if best_score >= 0.5 else None


def coerce_framework(raw: str, slide_type: str, *, fill_empty: bool = False) -> tuple[str, str | None]:
    """(framework, warning or None). An empty framework stays empty unless `fill_empty` (dense), which uses the
    type default. A name that survives every step passes through unchanged."""
    trimmed = (raw or "").strip()
    fallback = TYPE_DEFAULT_FRAMEWORK.get(slide_type)
    if not trimmed:
        if fill_empty and fallback:
            return fallback, f"no framework given — using the {slide_type}-slide default \"{fallback}\""
        return trimmed, None
    exact = canonical_framework(trimmed)
    if exact:
        return exact, None
    norm = normalize_framework_name(trimmed)
    mapped = COERCIONS.get(norm)
    if mapped:
        return mapped, f"\"{trimmed}\" isn't buildable from native PowerPoint shapes — using \"{mapped}\" instead"
    stripped = re.sub(r"^(3d|isometric)\s+", "", norm)
    if stripped != norm:
        flat = canonical_framework(stripped) or COERCIONS.get(stripped) or fuzzy_match(stripped)
        if flat:
            return flat, f"\"{trimmed}\" asked for a 3D/isometric visual — using the flat \"{flat}\" instead"
    fuzzy = fuzzy_match(norm)
    if fuzzy:
        return fuzzy, f"framework \"{trimmed}\" isn't in the library — using the closest match \"{fuzzy}\""
    if fallback:
        return fallback, (f"framework \"{trimmed}\" isn't renderable — falling back to the "
                          f"{slide_type}-slide default \"{fallback}\"")
    return trimmed, None


def insert_dividers(slides: list[StorylineSlide]) -> list[StorylineSlide]:
    """A divider opens each body section after the first, from 8 slides and 2+ sections; the deck is renumbered
    1..N. Unchanged (and not renumbered) when sectioning is not meaningful. Idempotent: a section already opened
    by a divider gets no second one. Darwin `insertDividers`."""
    if len(slides) < MIN_SLIDES_FOR_DIVIDERS:
        return slides
    body_sections = {s.section.strip() for s in slides
                     if s.type not in NO_SECTION_TYPES and s.section and s.section.strip()}
    if len(body_sections) <= 1:
        return slides
    out: list[StorylineSlide] = []
    prev: str | None = None
    for s in slides:
        section = s.section.strip() if s.section else ""
        if s.type not in NO_SECTION_TYPES and section:
            if s.type != "divider" and prev is not None and section != prev:
                out.append(StorylineSlide(number=0, title=section, type="divider", section=section,
                                          framework=DIVIDER_FRAMEWORK,
                                          description=f"Section break introducing {section}.", bullets=[]))
            prev = section
        out.append(s)
    return [s.model_copy(update={"number": i + 1}) for i, s in enumerate(out)]


def _body_sections(slides: list[StorylineSlide]) -> list[str]:
    seen: list[str] = []
    for s in slides:
        if s.type not in FURNITURE and s.section and s.section not in seen:
            seen.append(s.section)
    return seen


def repair(storyline: Storyline, *, density: str = "standard", dividers: bool = True,
           max_slides: int = 0) -> tuple[Storyline, list[str]]:
    """The storyline with every repairable problem repaired, and one warning per repair.

    `dividers=False` for an edit a person saved (a divider they removed stays removed). `max_slides` > 0 keeps
    the first N slides (before dividers)."""
    dense = density == "dense"
    warnings: list[str] = []
    slides = list(storyline.slides)
    if max_slides > 0 and len(slides) > max_slides:
        warnings.append(f"The storyline had {len(slides)} slides — kept the first {max_slides}")
        slides = slides[:max_slides]
    recent: list[str | None] = []
    out: list[StorylineSlide] = []
    for s in slides:
        update: dict[str, object] = {}
        n = s.number
        framework = s.framework
        if s.type not in FURNITURE:
            framework, note = coerce_framework(s.framework, s.type, fill_empty=dense)
            update["framework"] = framework
            if note:
                warnings.append(f"Slide {n}: {note}")
            if dense and len(s.bullets) < MIN_BODY_BULLETS:
                count = len(s.bullets)
                warnings.append(f"Slide {n}: only {count} bullet{'' if count == 1 else 's'} — a body slide "
                                f"needs at least {MIN_BODY_BULLETS} to fill its exhibits")
        if s.chart_data is not None:
            if dense and s.type in FURNITURE:
                update["chart_data"] = None
                warnings.append(f"Slide {n}: dropped chart data — a {s.type} slide has no chart")
            elif not dense and framework not in CHART_FRAMEWORKS:
                update["chart_data"] = None
                warnings.append(f"Slide {n}: dropped chartData — \"{framework}\" doesn't render as a chart")
        arch: str | None = s.archetype_id
        if arch:
            if dense and s.type in FURNITURE:
                arch = None
            else:
                known = archetypes.canonical_id(arch)
                if known is None:
                    warnings.append(f"Slide {n}: unknown archetypeId \"{arch}\" ignored")
                    arch = None
                elif dense and known in recent[-ARCHETYPE_GAP:]:
                    arch = None
                else:
                    arch = known
            update["archetype_id"] = arch
        recent.append(arch)
        out.append(s.model_copy(update=update) if update else s)
    if dividers:
        out = insert_dividers(out)
    if dense:
        sections = _body_sections(out)
        out = [s.model_copy(update={"bullets": sections[:MAX_BULLETS["dense"]]})
               if s.type == "agenda" and len(s.bullets) < 2 and len(sections) >= 2 else s for s in out]
        out = [s.model_copy(update={"number": i + 1}) for i, s in enumerate(out)]
    return storyline.model_copy(update={"slides": out}), warnings
