"""The layout-archetype library behind the design chat's `find_layout_reference` tool.

The library is `app/data/archetypes.json`: 1,240 slide-layout archetypes in 42 categories (Darwin's
`deckArchetypes.json`, de-identified: neutral `<category>-<nn>` ids, no deck names, page numbers or
pictures). It is the one copy, shared with the storyline (`app/data/README.md`); this module owns only
its loader. `DESIGN_ARCHETYPES_PATH` points at another library (tests use a small synthetic one).
Each archetype is a structural recipe: what the layout argues (`purpose`), how it is laid out (`directive`), the repeating units it is built from
with their typical and tolerable counts, the page furniture and the density.

Retrieval is deterministic and dependency-free: BM25 over a weighted bag of words per archetype (its
name ×3, category name ×2, unit names ×2, purpose ×2, directive ×1), a few domain synonyms folded into the
query ("bridge" finds waterfalls, "2x2" finds quadrants), an optional `slide_type` that restricts the
search to the categories that type is drawn from, and a diversity pass so three results are three
different ideas rather than three near-copies from one category.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import ai as ai_config

#: The one copy of the archetype library, shared with app/core/storyline (app/data/README.md).
DATA = Path(__file__).resolve().parents[2] / "data" / "archetypes.json"


def data_path() -> Path:
    """The library in use: `DESIGN_ARCHETYPES_PATH` when set, else the shared `app/data/archetypes.json`.
    Read on every call, so a test (or a changed setting) takes effect without a restart."""
    configured = ai_config.ai_settings.design_archetypes_path.strip()
    return Path(configured) if configured else DATA

DEFAULT_LIMIT = 3
MAX_LIMIT = 5

#: Slide-Creator's 22 slide types (`slideTypes.ts`) → the archetype categories each is drawn from, most
#: typical first. An empty list means the library has no archetype of that type (covers, dividers,
#: quotes are the master's own layouts): the query alone decides.
SLIDE_TYPE_CATEGORIES: dict[str, list[str]] = {
    "title": [],
    "divider": [],
    "quote": [],
    "agenda": ["numbered-category-columns", "stacked-category-tiers", "thumbnail-process-recap"],
    "navigator": ["thumbnail-process-recap", "strategic-framework-pillars", "numbered-category-columns",
                  "stacked-category-tiers"],
    "closing": ["synthesis-logic-flow", "objective-alignment-sidebar", "thumbnail-process-recap"],
    "framework": ["strategic-framework-pillars", "quadrant-matrix-layout", "radial-circular-diagram",
                  "hub-spoke-ecosystem-map", "stacked-category-tiers", "value-prop-interaction",
                  "numbered-category-columns", "dual-entity-interaction", "input-process-output-flow",
                  "funnel-filtering-process"],
    "data": ["split-chart-narrative-dashboard", "waterfall-breakdown-analysis", "scatter-bubble-chart",
             "functional-overview-dashboard"],
    "process": ["chevron-process-flow", "swimlane-process-map", "input-process-output-flow",
                "staircase-journey-progression", "funnel-filtering-process", "stakeholder-process-summary"],
    "comparison": ["comparative-option-cards", "pros-cons-comparison-matrix", "spectrum-comparison-columns",
                   "benchmarking-comparison-table", "dual-entity-interaction"],
    "timeline": ["gantt-timeline-roadmap", "staircase-journey-progression", "project-charter-dual-timeline"],
    "kpi": ["kpi-definition-profile", "functional-overview-dashboard", "split-chart-narrative-dashboard"],
    "market": ["funnel-filtering-process", "radial-circular-diagram", "split-chart-narrative-dashboard",
               "scatter-bubble-chart", "geographic-map-callouts"],
    "org": ["hierarchical-org-chart", "governance-hierarchy-interaction", "responsibility-matrix-raci",
            "stakeholder-process-summary"],
    "table": ["structured-data-table", "hierarchical-mapping-table", "entity-detail-mapping-rows",
              "benchmarking-comparison-table", "matrix-heatmap-evaluation"],
    "case-study": ["service-profile-sidebar", "initiative-profile-bento", "entity-detail-mapping-rows",
                   "logo-mission-alignment"],
    "charter": ["project-charter-dual-timeline", "process-charter-metadata", "initiative-profile-bento"],
    "synthesis": ["synthesis-logic-flow", "input-process-output-flow", "objective-alignment-sidebar"],
    "benchmark": ["benchmarking-comparison-table", "entity-detail-mapping-rows", "logo-mission-alignment",
                  "matrix-heatmap-evaluation", "spectrum-comparison-columns"],
    "scorecard": ["weighted-scoring-matrix", "multi-criteria-assessment", "matrix-heatmap-evaluation",
                  "kpi-definition-profile"],
    "deep-dive": ["service-profile-sidebar", "initiative-profile-bento", "entity-detail-mapping-rows",
                  "objective-alignment-sidebar", "split-chart-narrative-dashboard"],
    "map": ["geographic-map-callouts", "hub-spoke-ecosystem-map"],
}

#: Query words the library says differently. Each adds terms; the word itself is kept.
SYNONYMS: dict[str, str] = {
    "bridge": "waterfall breakdown",
    "walk": "waterfall",
    "gantt": "timeline roadmap",
    "roadmap": "timeline gantt",
    "raci": "responsibility matrix",
    "2x2": "quadrant matrix",
    "swot": "quadrant matrix",
    "org": "organizational hierarchy",
    "orgchart": "organizational hierarchy",
    "kpi": "metric indicator",
    "kpis": "metric indicator",
    "metric": "kpi",
    "pillar": "pillars framework",
    "harvey": "scoring assessment",
    "heatmap": "heat matrix",
    "tam": "funnel market",
    "sam": "funnel market",
    "chevron": "process flow",
    "steps": "process",
    "phases": "phase",
    "vs": "comparison",
    "versus": "comparison",
    "options": "option comparison",
    "pros": "pro con",
    "cons": "pro con",
    "journey": "staircase progression",
    "ecosystem": "hub spoke",
    "stakeholders": "stakeholder",
    "geography": "map geographic",
    "country": "map geographic",
    "countries": "map geographic",
    "region": "map geographic",
    "bubble": "scatter bubble",
    "dashboard": "overview",
}

STOPWORDS = frozenset("""
a an and are as at be by for from has have in into is it its of on or that the their this to with
use uses used using slide slides layout layouts page each per one two three four five six via which
where while within across between over under all any more most other some such than then them they
""".split())

#: Field weights: how many times a field's words count towards the archetype's bag.
WEIGHTS = {"name": 3, "category": 2, "units": 2, "purpose": 2, "directive": 1}

K1, B = 1.2, 0.75


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ed") and not word.endswith("eed"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def tokens(text: str) -> list[str]:
    """Lower-case word stems, stopwords out. `2x2`, `raci` and numbers survive as tokens."""
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", str(text or "").lower()) if w not in STOPWORDS]


def _query_tokens(query: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", str(query or "").lower())
    expanded = list(words)
    for word in words:
        if word in SYNONYMS:
            expanded += SYNONYMS[word].split()
    seen, out = set(), []
    for token in tokens(" ".join(expanded)):
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


@dataclass(frozen=True)
class Archetype:
    id: str
    category_id: str
    category: str
    name: str
    purpose: str
    directive: str
    units: tuple[dict[str, Any], ...]
    furniture: str
    density: str
    order: int                       # position in the library: the tie-breaker, so results are stable
    src: str = ""                    # digest of the Slide-Creator source id (scripts/sync_design_refs.py)


class _Index:
    def __init__(self, archetypes: list[Archetype]) -> None:
        self.archetypes = archetypes
        self.bags: list[Counter[str]] = []
        self.shape: list[frozenset[str]] = []    # name + unit words: what "near-duplicate" compares
        df: Counter[str] = Counter()
        for a in archetypes:
            bag: Counter[str] = Counter()
            fields = {"name": a.name.replace("-", " "), "category": a.category,
                      "units": " ".join(u.get("name", "") for u in a.units),
                      "purpose": a.purpose, "directive": a.directive}
            for field, text in fields.items():
                for token in tokens(text):
                    bag[token] += WEIGHTS[field]
            self.bags.append(bag)
            self.shape.append(frozenset(tokens(fields["name"] + " " + fields["units"])))
            df.update(bag.keys())
        n = len(archetypes)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self.lengths = [sum(bag.values()) for bag in self.bags]
        self.avg = (sum(self.lengths) / n) if n else 1.0

    def score(self, i: int, query: list[str]) -> float:
        bag, length, total = self.bags[i], self.lengths[i], 0.0
        for token in query:
            tf = bag.get(token)
            if not tf:
                continue
            total += self.idf[token] * tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / self.avg))
        return total


def library() -> _Index:
    """The index of the library at `data_path()`, built once per path."""
    return _library(str(data_path()))


@lru_cache(maxsize=4)
def _library(path: str) -> _Index:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    archetypes: list[Archetype] = []
    for category in raw.get("categories") or []:
        for a in category.get("archetypes") or []:
            archetypes.append(Archetype(
                id=a["id"], category_id=category["id"], category=category.get("name") or category["id"],
                name=a.get("name") or a["id"], purpose=a.get("purpose") or "",
                directive=a.get("directive") or "", units=tuple(a.get("units") or []),
                furniture=a.get("furniture") or "", density=a.get("density") or "medium",
                order=len(archetypes), src=a.get("src") or ""))
    return _Index(archetypes)


def get(archetype_id: str | None) -> Archetype | None:
    """The archetype a storyline names: by this library's id (`<category>-<nn>`) or by Slide-Creator's
    source id (`<deck-slug>--pNNN`), which only its digest (`src`) is kept to match against."""
    key = str(archetype_id or "").strip()
    if not key:
        return None
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    for a in library().archetypes:
        if a.id == key or (a.src and a.src == digest):
            return a
    return None


def categories() -> dict[str, str]:
    """Category id → name, in library order."""
    out: dict[str, str] = {}
    for a in library().archetypes:
        out.setdefault(a.category_id, a.category)
    return out


def _diverse(ranked: list[tuple[float, int]], limit: int, index: _Index,
             taken: list[int] | None = None) -> list[int]:
    """Greedy re-rank: each pick after the first is discounted per earlier pick from its category, and a
    candidate whose name-and-units words mostly repeat a pick's is skipped as a near-duplicate."""
    chosen = list(taken or [])
    pool = list(ranked[:80])
    while pool and len(chosen) < limit:
        best, best_score = 0, -1.0
        for pos, (score, i) in enumerate(pool):
            same = sum(1 for c in chosen if index.archetypes[c].category_id == index.archetypes[i].category_id)
            adjusted = score * (0.5 ** same)
            if adjusted > best_score:
                best, best_score = pos, adjusted
        score, i = pool.pop(best)
        shape = index.shape[i]
        if any(len(shape & index.shape[c]) / max(len(shape | index.shape[c]), 1) >= 0.6 for c in chosen):
            continue
        chosen.append(i)
    return chosen[len(taken or []):]


def clamp_limit(limit: Any) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, n))


def search(query: str, slide_type: str | None = None, limit: Any = DEFAULT_LIMIT) -> list[Archetype]:
    """The best `limit` (1–5) archetypes for `query`, restricted to `slide_type`'s categories when it
    names a type that has any — topped up from the whole library when those run out of matches."""
    index = library()
    limit = clamp_limit(limit)
    query_tokens = _query_tokens(query)
    wanted = SLIDE_TYPE_CATEGORIES.get(normalise_type(slide_type) or "", [])
    scored = [(index.score(i, query_tokens), i) for i in range(len(index.archetypes))] if query_tokens else []
    positive = sorted(((s, i) for s, i in scored if s > 0), key=lambda si: (-si[0], si[1]))
    if not wanted:
        return [index.archetypes[i] for i in _diverse(positive, limit, index)]
    rank = {cid: n for n, cid in enumerate(wanted)}
    inside = [(s, i) for s, i in positive if index.archetypes[i].category_id in rank]
    if not inside:
        # the query names nothing in these categories: the type's most typical archetypes, one per
        # category in the type's own order, medium density first (the easiest to adapt)
        density = {"medium": 0, "high": 1, "low": 2}
        seeds = sorted((i for i, a in enumerate(index.archetypes) if a.category_id in rank),
                       key=lambda i: (rank[index.archetypes[i].category_id],
                                      density.get(index.archetypes[i].density, 3), i))
        leaders, seen = [], set()
        for i in seeds:
            cid = index.archetypes[i].category_id
            leaders.append(((1.0 - 0.01 * rank[cid]) if cid not in seen else 0.01, i))
            seen.add(cid)
        inside = sorted(leaders, key=lambda si: -si[0])
    chosen = _diverse(inside, limit, index)
    if len(chosen) < limit:
        outside = [(s, i) for s, i in positive if index.archetypes[i].category_id not in rank]
        chosen += _diverse(outside, limit, index, taken=chosen)
    return [index.archetypes[i] for i in chosen]


def normalise_type(slide_type: str | None) -> str | None:
    if not isinstance(slide_type, str) or not slide_type.strip():
        return None
    key = re.sub(r"[\s_]+", "-", slide_type.strip().lower())
    return {"casestudy": "case-study", "deepdive": "deep-dive"}.get(key, key)


def _units_line(units: tuple[dict[str, Any], ...]) -> str:
    parts = []
    for unit in units[:6]:
        count = unit.get("count")
        bit = unit.get("name") or "unit"
        if count:
            bit += f" ×{count}"
        if unit.get("elastic"):
            bit += f" (fits {unit['elastic']})"
        anatomy = unit.get("anatomy") or ""
        if anatomy:
            bit += f": {anatomy if len(anatomy) <= 110 else anatomy[:107].rstrip() + '…'}"
        parts.append(bit)
    return "\n     ".join(parts)


def describe(a: Archetype, n: int) -> str:
    lines = [f"{n}. {a.name} — {a.category} (density: {a.density})",
             f"   Purpose: {a.purpose}",
             f"   Layout: {a.directive}"]
    units = _units_line(a.units)
    if units:
        lines.append(f"   Units: {units}")
    # `furniture` (the source deck's logos, page numbers, footers) is deliberately not shown: on this
    # app the master draws the furniture, and a recipe that says "logo bottom-right" invites a redraw.
    return "\n".join(lines)


def find_layout_reference(query: str, slide_type: str | None = None, limit: Any = DEFAULT_LIMIT) -> str:
    """The tool's answer, as compact text."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must say what the slide has to show, e.g. \"revenue bridge 2023 to 2024\"")
    kind = normalise_type(slide_type)
    notes = []
    if kind and kind not in SLIDE_TYPE_CATEGORIES:
        notes.append(f"(slide_type {slide_type!r} is not one of: {', '.join(SLIDE_TYPE_CATEGORIES)}; ignored.)")
        kind = None
    elif kind and not SLIDE_TYPE_CATEGORIES[kind]:
        notes.append(f"(The library has no {kind} archetypes — that slide is the master's own layout; "
                     f"these are the closest matches to the query.)")
    matches = search(query, kind, limit)
    if not matches:
        return "\n".join(notes + [f"No layout archetype matched {query!r}. Try the structure you need in plain "
                                  f"words (e.g. \"three pillars\", \"phased roadmap\", \"option comparison\")."])
    head = ("Layout references — structural recipes from a library of consulting slides. Adapt the structure "
            "to this master's zones, palette and fonts, flex the unit counts to the content; the recipes carry "
            "no content of their own.")
    return "\n".join([head] + notes + [describe(a, n) for n, a in enumerate(matches, 1)])
