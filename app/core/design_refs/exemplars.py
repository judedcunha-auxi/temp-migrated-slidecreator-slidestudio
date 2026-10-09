"""Visual layout exemplars: rendered slides from reference decks, shown to the designer before it designs.

Provenance. In Slide Studio, 539 slides from fifteen reference decks (consulting and banking decks and
template libraries) were rendered to 1280-wide JPEGs and tagged against this package's taxonomy
(`vocabulary.SLIDE_TYPES`, the framework names, the archetype category ids) in `exemplars.json`. Those
pictures come from client and third-party decks, so **none of them ships with the service** (decision D3,
migration plan §4.1): a set is installed beside the deploy and named by `DESIGN_EXEMPLARS_DIR`
(`exemplars.json` + one `<id>.jpg` per entry). Unset, `available()` is False and the tool is not offered.

Composition. Most sources are single-unit layouts, but strong consulting slides are composites: 2-4 units on one
page (system panels + a KPI strip; option columns + a pros/cons spine + a verdict row; a scoring table + a
recommendation panel). An optional `composition` field lists those units in order, in generic words (brief:
`out/exemplars/COMPOSITION-BRIEF.md`); single-unit slides leave it empty. The words go into the search text and
into the caption ("combines: a + b + c"), so Claude sees how the parts of a good slide fit together, not just
which framework it uses.

Ranking. BM25 decides relevance. Among the pictures the query actually matched (score > 0), a mild multiplicative
prior lifts composite slides (2+ units, x1.10) and reference-standard ones (x1.15, so x1.25 for both): enough to
reorder near-ties in their favour, never enough to beat a clearly better match, and never applied to a zero
score, so an irrelevant composite can not enter the answer. With no query words (type or framework only) the
prior is just the tie-break. The spread over decks still applies, so one reference deck contributes at most one
picture per answer until the other decks run out.

Why pictures when `archetypes` already has recipes: the recipes say *what* to draw; a picture shows the
proportions, whitespace and hierarchy that make it read (SlideCoder's layout RAG, PPTAgent's reference slides;
see docs/DESIGN-QUALITY-RESEARCH.md §4.6). They are layout references only. Many come from client decks, which
may be looked at but never mimicked, so every answer says: take the structure, never the text, names, logos,
colours or data.

The pictures are not in source control. `DESIGN_EXEMPLARS_DIR` points at them; without a manifest the
tool is simply not offered (`app.core.design.tool_schemas.offered_tools`).
"""
from __future__ import annotations

import base64
import io
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import ai as ai_config
from app.core.design_refs import archetypes

#: The widest picture sent to the model. 960x540 is ~700 image tokens: enough for the layout's gist
#: (region sizes, alignment, density), not for reading its text, which is the point.
MAX_W = 960
DEFAULT_LIMIT = 2
MAX_LIMIT = 3
K1, B = 1.2, 0.75
#: The ranking prior (see the module docstring): multiplies a query-relevant BM25 score.
COMPOSITE_BOOST = 0.10
REFERENCE_BOOST = 0.15


@dataclass(frozen=True)
class Exemplar:
    id: str
    type: str
    frameworks: tuple[str, ...]
    category: str | None
    density: str | None
    layout: str
    tokens: tuple[str, ...]
    composition: tuple[str, ...] = ()
    reference: bool = False

    @property
    def deck(self) -> str:
        return self.id.rsplit("_", 1)[0]

    @property
    def composite(self) -> bool:
        return len(self.composition) >= 2

    @property
    def prior(self) -> float:
        return 1.0 + COMPOSITE_BOOST * self.composite + REFERENCE_BOOST * self.reference


def directory() -> Path | None:
    """The installed exemplar folder (`DESIGN_EXEMPLARS_DIR`), or None when none is configured.

    Nothing ships in the repo (decision D3: the pictures come from client and third-party decks), so
    an unset variable means no exemplars and `get_exemplars` is not offered."""
    configured = ai_config.ai_settings.design_exemplars_dir.strip()
    return Path(configured) if configured else None


def _norm(name: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(name or "").lower()))


@lru_cache(maxsize=4)
def _load(folder: str) -> tuple[Exemplar, ...]:
    path = Path(folder) / "exemplars.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8")).get("exemplars") or []
    except (OSError, ValueError, AttributeError):
        return ()
    out = []
    for r in rows:
        sid = r.get("id")
        if not isinstance(sid, str) or not (Path(folder) / f"{sid}.jpg").is_file():
            continue
        frameworks = tuple(f for f in r.get("frameworks") or [] if isinstance(f, str))
        category = r.get("archetype_category")
        kind = archetypes.normalise_type(r.get("type")) or "other"
        composition = tuple(c.strip() for c in r.get("composition") or [] if isinstance(c, str) and c.strip())
        # the tagged fields weigh more than the free-text layout sentence; the units weigh like the sentence
        text = " ".join([kind] * 2 + list(frameworks) * 3 + [(category or "").replace("-", " ")] * 2
                        + [r.get("layout") or ""] + list(composition))
        out.append(Exemplar(sid, kind, frameworks, category, r.get("density"), r.get("layout") or "",
                            tuple(archetypes.tokens(text)), composition, r.get("reference") is True))
    return tuple(out)


def library() -> tuple[Exemplar, ...]:
    folder = directory()
    return _load(str(folder)) if folder is not None else ()


def available() -> bool:
    return bool(library())


def _bm25(query: list[str], docs: tuple[Exemplar, ...]) -> dict[str, float]:
    if not query or not docs:
        return {}
    avg = sum(len(d.tokens) for d in docs) / len(docs)
    df = Counter(t for d in docs for t in set(d.tokens))
    scores = {}
    for d in docs:
        tf = Counter(d.tokens)
        s = 0.0
        for t in query:
            if tf[t]:
                idf = math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * (K1 + 1) / (tf[t] + K1 * (1 - B + B * len(d.tokens) / avg))
        scores[d.id] = s * d.prior  # the prior only ever scales a match; 0 stays 0
    return scores


def search(query: str | None = None, slide_type: str | None = None, framework: str | None = None,
           limit: Any = DEFAULT_LIMIT) -> list[Exemplar]:
    """The best `limit` (1-3) exemplars. A known `slide_type` or `framework` narrows the pool when it has
    matches; the query ranks within it. Picks are spread over layout categories and decks, so two
    near-identical pages from one deck never fill the answer."""
    try:
        limit = max(1, min(MAX_LIMIT, int(limit)))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    pool = library()
    kind = archetypes.normalise_type(slide_type)
    if kind and any(e.type == kind for e in pool):
        pool = tuple(e for e in pool if e.type == kind)
    if framework:
        want = _norm(framework)
        hits = tuple(e for e in pool if any(_norm(f) == want for f in e.frameworks)) or \
            tuple(e for e in pool if any(want in _norm(f) or _norm(f) in want for f in e.frameworks))
        pool = hits or pool
    words = archetypes.tokens(" ".join(filter(None, [query, framework])))
    scores = _bm25(words, pool)
    ranked = sorted(pool, key=lambda e: (-scores.get(e.id, 0.0), -e.prior, e.id))
    if any(scores.values()):  # the query matched something: never pad the answer with unrelated pictures
        ranked = [e for e in ranked if scores.get(e.id, 0.0) > 0]
    picked: list[Exemplar] = []
    for strict in (True, False):
        for e in ranked:
            if len(picked) == limit:
                break
            if e in picked:
                continue
            if strict and any(e.category == p.category or e.deck == p.deck for p in picked):
                continue
            picked.append(e)
    if len(picked) < limit and len(pool) < len(library()) and words:
        # the type or framework left too few matches: top up with the best query matches from everywhere
        wider = _bm25(words, library())
        for e in sorted(library(), key=lambda e: (-wider.get(e.id, 0.0), -e.prior, e.id)):
            if len(picked) == limit or wider.get(e.id, 0.0) <= 0:
                break
            if e not in picked and all(e.deck != p.deck or e.category != p.category for p in picked):
                picked.append(e)
    return picked


@lru_cache(maxsize=64)
def _image(folder: str, sid: str) -> str:
    from PIL import Image  # noqa: PLC0415 — only a tool call pays for the import

    with Image.open(Path(folder) / f"{sid}.jpg") as source:
        im = source.convert("RGB")
    if im.width > MAX_W:
        im = im.resize((MAX_W, round(im.height * MAX_W / im.width)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80, optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


RULE = ("Layout references only, from real decks: take the structure (regions, proportions, alignment, "
        "hierarchy, how the parts connect) and build it in this master's grid, fonts and palette. Never copy "
        "their text, names, logos, colours or data, and never embed the pictures.")


def get_exemplars(query: str | None = None, slide_type: str | None = None, framework: str | None = None,
                  limit: Any = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """The tool's answer as content blocks: the pictures first, then what each one is and the rule."""
    if not any(isinstance(v, str) and v.strip() for v in (query, slide_type, framework)):
        raise ValueError("say what the slide must show (query), or give a slide_type or framework")
    picks = search(query, slide_type, framework, limit)
    if not picks:
        return [{"type": "text", "text": "No exemplars matched; use find_layout_reference instead."}]
    folder = str(directory())  # picks exist, so a folder is configured
    blocks: list[dict[str, Any]] = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                         "data": _image(folder, e.id)}} for e in picks]
    lines = []
    for n, e in enumerate(picks, 1):
        tags = ", ".join(e.frameworks) or (e.category or "").replace("-", " ")
        line = f"{n}. {e.type}{' - ' + tags if tags else ''} ({e.density or '?'} density): {e.layout}"
        if e.composite:
            line += f" Combines: {' + '.join(e.composition)}."
        if e.reference:
            line += " House quality bar: match its density and emphasis tiers, not its content."
        lines.append(line)
    blocks.append({"type": "text", "text": "\n".join(lines) + "\n" + RULE})
    return blocks
