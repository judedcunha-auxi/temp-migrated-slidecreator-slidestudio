"""A small loader over the layout-archetype library (`app/data/archetypes.json`).

The storyline needs three things from the 1,240 archetypes: the compact catalog its prompt lists
(Darwin `deckArchetypeCatalog(3)`), a check that an `archetypeId` the model or the browser names is
real, and the directive for a named archetype (mirrored for Arabic). Retrieval (BM25 search for the
design chat) belongs to `app/core/design_refs`, which reads the same file.

Ids. This library's ids are neutral (`<category>-<nn>`). A storyline saved by Darwin names an archetype by
its old id (`<deck-slug>--pNNN`); only a SHA-256 digest of that id is kept (`src`), so `get` resolves an
old id without the deck slug being stored. `canonical_id` turns either form into the library id.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.core.storyline import rtl
from app.core.storyline.vocabulary import DATA_DIR

ARCHETYPES_FILE = DATA_DIR / "archetypes.json"

#: Archetypes per category in the storyline prompt's catalog (Darwin `deckArchetypeCatalog(3)`).
CATALOG_PER_CATEGORY = 3


@dataclass(frozen=True)
class Archetype:
    id: str
    category_id: str
    category: str
    name: str
    purpose: str
    directive: str
    density: str
    src: str


@dataclass(frozen=True)
class _Library:
    archetypes: tuple[Archetype, ...]
    by_id: dict[str, Archetype]
    by_src: dict[str, Archetype]
    category_count: int


def source_digest(source_id: str) -> str:
    """The digest `src` holds for a Darwin archetype id (Slide Studio `scripts/sync_design_refs.py`)."""
    return hashlib.sha256(source_id.strip().encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=1)
def library() -> _Library:
    raw: dict[str, Any] = json.loads(ARCHETYPES_FILE.read_text(encoding="utf-8"))
    items: list[Archetype] = []
    categories = raw.get("categories") or []
    for category in categories:
        for a in category.get("archetypes") or []:
            items.append(Archetype(
                id=str(a["id"]), category_id=str(category["id"]), category=str(category.get("name") or category["id"]),
                name=str(a.get("name") or a["id"]), purpose=str(a.get("purpose") or ""),
                directive=str(a.get("directive") or ""), density=str(a.get("density") or "medium"),
                src=str(a.get("src") or "")))
    return _Library(
        archetypes=tuple(items),
        by_id={a.id: a for a in items},
        by_src={a.src: a for a in items if a.src},
        category_count=len(categories),
    )


def get(archetype_id: str | None) -> Archetype | None:
    """The archetype named by a library id or by a Darwin source id; None when neither matches."""
    key = (archetype_id or "").strip()
    if not key:
        return None
    lib = library()
    return lib.by_id.get(key) or lib.by_src.get(source_digest(key))


def canonical_id(archetype_id: str | None) -> str | None:
    found = get(archetype_id)
    return found.id if found else None


@lru_cache(maxsize=2)
def catalog(per_category: int = CATALOG_PER_CATEGORY) -> tuple[Archetype, ...]:
    """The first `per_category` archetypes of each category, in library order."""
    seen: dict[str, int] = {}
    out: list[Archetype] = []
    for a in library().archetypes:
        if seen.get(a.category_id, 0) >= per_category:
            continue
        seen[a.category_id] = seen.get(a.category_id, 0) + 1
        out.append(a)
    return tuple(out)


def catalog_text(language: str | None = None, per_category: int = CATALOG_PER_CATEGORY) -> str:
    """The catalog as the storyline prompt lists it: `id | category | name: purpose`, one per line.

    Darwin's format, purposes in full. Mirrored for an RTL language (`rtl.for_language`)."""
    lines = (f"{a.id} | {a.category} | {a.name}: {' '.join(a.purpose.split())}" for a in catalog(per_category))
    return rtl.for_language("\n".join(lines), language)


def directive(archetype_id: str | None, language: str | None = None) -> str | None:
    """The layout directive of a named archetype, mirrored for an RTL language; None when unknown.

    The hook the downstream prompts (the design turn, Darwin's image prompt) use for an `archetypeId`."""
    found = get(archetype_id)
    return rtl.for_language(found.directive, language) if found else None
