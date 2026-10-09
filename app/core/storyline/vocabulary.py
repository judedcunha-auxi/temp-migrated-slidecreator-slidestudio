"""The storyline's vocabulary: slide types, modes, densities, languages and the framework names.

Merged from Darwin (`_shared/slideTypes.ts`, `frameworks.ts`) and Slide Studio (`server/storyline/schema.py`,
`design_refs/vocabulary.py`):

* the 22 slide types and the furniture sets are Darwin's (Slide Studio ported the same list);
* the framework names and aliases are read from `app/data/frameworks.json`, the one copy shared with
  `app/core/design_refs` (the per-framework hints are prompt material and stay with their prompts);
* the modes (`deck`, `collection`, `single`) are Slide Studio's, plus `auto`: Darwin's behaviour, where
  the model chooses the furniture and the slide count is not clamped;
* the densities: `standard` is Darwin's (3-4 bullets, about 110 words), `dense` is Slide Studio's composite
  slide (3-6 bullets, no word cap; Slide Studio ADR 0012).
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, get_args

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
FRAMEWORKS_FILE = DATA_DIR / "frameworks.json"

SlideType = Literal[
    "title", "agenda", "framework", "data", "process", "quote", "closing",
    "comparison", "timeline", "kpi", "market", "org", "divider", "table",
    "case-study", "charter", "synthesis", "navigator", "benchmark",
    "scorecard", "deep-dive", "map",
]
#: Darwin's 22 slide types, in its order (the order the model's enum lists them).
SLIDE_TYPES: tuple[str, ...] = get_args(SlideType)

#: Near-empty deck furniture: no framework coercion and no bullet floor (storylineSchema.ts, frameworkGuard.ts).
FURNITURE: frozenset[str] = frozenset({"title", "agenda", "divider", "navigator", "closing"})
#: Furniture that never opens a section, so never gets a divider before it (dividers.ts).
NO_SECTION_TYPES: frozenset[str] = frozenset({"title", "agenda", "closing", "navigator"})

Mode = Literal["auto", "deck", "collection", "single"]
MODES: tuple[str, ...] = get_args(Mode)
#: mode -> (min slides, max slides, default). `auto` has no clamp (Darwin); its default applies only when
#: no slide count was given.
MODE_BOUNDS: dict[str, tuple[int, int | None, int]] = {
    "auto": (1, None, 12),
    "deck": (6, 30, 12),
    "collection": (3, 20, 5),
    "single": (1, 1, 1),
}
#: Modes whose storyline gets section dividers inserted (Darwin always inserts; Slide Studio only for decks).
DIVIDER_MODES: frozenset[str] = frozenset({"auto", "deck"})

Density = Literal["standard", "dense"]
DENSITIES: tuple[str, ...] = get_args(Density)

Language = Literal["en", "ar"]
LANGUAGES: dict[str, str] = {"en": "English", "ar": "Arabic"}

#: Bullet limits per density. Standard rejects outside them (Darwin's zod schema); dense trims to the
#: maximum and warns below the minimum (Slide Studio).
MIN_BODY_BULLETS = 3
MAX_BULLETS: dict[str, int] = {"standard": 4, "dense": 6}

#: Frameworks that render as a chart. Darwin read this from the OCR detection metadata
#: (`slideContext.ts: FRAMEWORK_META[...].primary == 'chart'`), which retires with OCR (D0); the set is the same.
CHART_FRAMEWORKS: frozenset[str] = frozenset({
    "funnel", "waterfall chart", "heatmap", "bubble chart", "gantt chart", "100% stacked bar chart",
    "value chain heatmap",
})


def normalize_framework_name(name: str) -> str:
    """Lowercase, drop apostrophes, fold the multiplication sign, collapse other punctuation (frameworks.ts)."""
    name = name.lower().replace("'", "").replace("’", "").replace("×", "x")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9/+]+", " ", name)).strip()


@lru_cache(maxsize=1)
def _library() -> tuple[tuple[str, ...], dict[str, str], dict[str, str]]:
    raw = json.loads(FRAMEWORKS_FILE.read_text(encoding="utf-8"))
    names = tuple(str(n) for n in raw["frameworks"])
    aliases = {str(a): str(c) for a, c in raw["aliases"].items()}
    lookup = {normalize_framework_name(n): n for n in names}
    lookup.update({normalize_framework_name(a): c for a, c in aliases.items()})
    return names, aliases, lookup


def framework_names() -> tuple[str, ...]:
    """The 65 canonical framework names, in Darwin's order (the order the storyline prompt lists them)."""
    return _library()[0]


def framework_aliases() -> dict[str, str]:
    """Observed variant spelling -> canonical name."""
    return dict(_library()[1])


def canonical_framework(name: str) -> str | None:
    """The canonical name for a name, alias or variant spelling, or None when it is unknown."""
    return _library()[2].get(normalize_framework_name(name))
