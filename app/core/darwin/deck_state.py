"""Darwin's deck job state (`_shared/jobState.ts`), read from the slides port.

Darwin kept a per-deck `JobState` blob (`deck-jobs/job/<deckId>.json`): each slide's status, error, render
versions and current version, plus the deck's master decision. The service keeps the same facts in the
General service: the slide rows (`Slide.status`, `error`, `versions`, `current`, `master_kind`, `is_tile`) and
the deck (`master_used`, `master_skip_reason`). This module turns them back into what Darwin's routes read:

| Darwin (`jobState.ts`) | Here |
|---|---|
| no job state yet | the deck has no slide rows (`/api/generate` writes them before it answers, as Darwin wrote the state) |
| `currentVersion(vs)`: current, else max(v), else version, else 1 | `current_version`: `Slide.current`, else max(v), else 1 |
| `slideVersions`: stored versions, or a synthesised v1 for a done slide | `version_list`: the stored versions (append-only), or the same synthesised v1 |
| `imageUrlFor`: `/api/image?deckId=..&slide=n&v=current` when done | `image_url` |
| `progressText`, `isTerminal`, `masterApplied` | `progress_text`, `is_terminal`, `status_body` |

Versions are append-only (the port's rule): a regenerated slide (retry) gains a version instead of
overwriting v1's image as Darwin did (docs/darwin-api.md §6).
"""

from __future__ import annotations

from typing import Any

from app.core.storage.models import Deck, Slide

STARTING = "Starting…"


def current_version(slide: Slide) -> int:
    if slide.current:
        return slide.current
    if slide.versions:
        return max(v.v for v in slide.versions)
    return 1


def version_list(slide: Slide) -> list[dict[str, Any]]:
    """`slideVersions(...).map(r => ({v, instruction}))`."""
    if slide.versions:
        return [{"v": v.v, "instruction": v.instruction} for v in slide.versions]
    if slide.status == "done":
        return [{"v": 1, "instruction": None}]
    return []


def image_url(deck_id: str, number: int, slide: Slide) -> str | None:
    if slide.status != "done":
        return None
    return f"/api/image?deckId={deck_id}&slide={number}&v={current_version(slide)}"


def is_terminal(slides: list[Slide]) -> bool:
    return all(s.status in ("done", "error") for s in slides)


def progress_text(slides: list[Slide]) -> str:
    done = sum(1 for s in slides if s.status == "done")
    return f"Generated {done} of {len(slides)} slides…"


def status_body(deck_id: str, deck: Deck, slides: list[Slide]) -> dict[str, Any]:
    """The `/api/status` body. `deck_id` is the id as the caller wrote it (Darwin echoed the query value)."""
    if not slides:
        return {"deckId": deck_id, "slides": {}, "progress": STARTING, "done": False}
    view: dict[str, Any] = {}
    for slide in sorted(slides, key=lambda s: s.number):
        entry: dict[str, Any] = {"status": slide.status}
        if slide.error is not None:
            entry["error"] = slide.error
        url = image_url(deck_id, slide.number, slide)
        if url is not None:
            entry["url"] = url
        entry["current"] = current_version(slide)
        entry["versions"] = version_list(slide)
        view[str(slide.number)] = entry
    body: dict[str, Any] = {"deckId": deck_id, "slides": view, "progress": progress_text(slides),
                            "done": is_terminal(slides), "masterApplied": bool(deck.master_used)}
    if deck.master_skip_reason is not None:
        body["masterSkipReason"] = deck.master_skip_reason
    return body


def image_ref_for(slide: Slide, v: int) -> str | None:
    """The stored picture of version `v` (an image-mode version), or None."""
    for version in slide.versions:
        if version.v == v and version.mode == "image" and version.image_ref:
            return version.image_ref
    return None
