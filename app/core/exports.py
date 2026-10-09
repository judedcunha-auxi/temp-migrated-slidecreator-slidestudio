"""Exports: one deck from designed slides (`stitch_deck`), and a design deck's own export.

`stitch_deck` replaces Slide Studio's `/v1/decks` (`server/compat/orchestrate.run_deck`; migration
plan §2.1, R): N slides designed one by one (each by `design_and_export`, each in its own design
deck) become one `.pptx`. It is a native multi-slide export (`app.engine.pipeline.export_deck` over
one deck holding every slide), so there is no zip merge and no media de-duplication to get wrong.

The slides are assumed to share a brand (the first part's master seeds the deck: one brand per deck,
as Darwin decks are). A part whose master differs is still placed: its slide moves onto the deck's
layout with the same name, else the first layout, and that is reported in `moved`.

Everything is written inside the caller's work directory (a job workspace); the `.pptx` lands at
`<work_dir>/out/deck.pptx`, which the job workspace persists through the storage port.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core import engine_service
from app.core.design.deck import DesignDeck
from app.engine.renderer import BlankLayoutsRenderer, Renderer

_log = logging.getLogger(__name__)

JSON = dict[str, Any]

MAX_SLIDES = 60


class StitchError(RuntimeError):
    """The deck could not be stitched; the message is safe to show."""


@dataclass(frozen=True)
class SlidePart:
    """One designed slide: the design deck directory it lives in and its id there."""

    deck_dir: Path
    slide_id: str


@dataclass
class StitchResult:
    pptx: Path
    slide_ids: list[str]
    element_count: int
    review_flag_count: int
    moved: list[str] = field(default_factory=list)

    @property
    def slide_count(self) -> int:
        return len(self.slide_ids)


def _layout_name(deck: DesignDeck, layout_id: str | None) -> str | None:
    lay = next((x for x in deck.master().get("layouts") or [] if x.get("id") == layout_id), None)
    return str(lay.get("name")) if lay and lay.get("name") else None


def stitch_deck(parts: list[SlidePart], work_dir: Path, *, title: str = "Deck",
                renderer: Renderer | None = None) -> StitchResult:
    """Assemble `parts`, in order, into one deck and export it. See the module docstring.

    `renderer` re-renders the master's layout backgrounds for the stitched deck; the parts' own
    backgrounds are reused when it is None (no network call: the PNGs are already in the parts).
    """
    if not parts:
        raise StitchError("There are no slides to stitch.")
    if len(parts) > MAX_SLIDES:
        raise StitchError(f"A deck may hold at most {MAX_SLIDES} slides.")
    sources = []
    for part in parts:
        try:
            src = DesignDeck(part.deck_dir)
            src.find_slide(src.meta(), part.slide_id)
        except KeyError as exc:
            raise StitchError("A slide to stitch is missing.") from exc
        sources.append(src)
    first = sources[0]
    if not first.master_path.is_file():
        raise StitchError("The first slide has no master.")

    deck = DesignDeck.create(Path(work_dir) / "deck", title)
    shutil.copyfile(first.master_path, deck.master_path)
    try:
        if renderer is not None:
            engine_service.import_master(deck, deck.master_path, "master.pptx", renderer=renderer)
        else:
            _reuse_master(first, deck)
    except engine_service.MasterImportFailed as exc:
        raise StitchError(str(exc)) from exc

    layouts = {str(lay.get("name")): str(lay["id"]) for lay in deck.master().get("layouts") or [] if lay.get("id")}
    first_layout = next(iter(layouts.values()), None)
    moved: list[str] = []
    for src, part in zip(sources, parts, strict=True):
        slide = src.find_slide(src.meta(), part.slide_id)
        html = src.read_slide(part.slide_id)
        layout_id = slide.get("layoutId")
        if src.master_path.read_bytes() != first.master_path.read_bytes():
            name = _layout_name(src, layout_id)
            target = layouts.get(name or "") or first_layout
            if target != layout_id:
                moved.append(part.slide_id)
            layout_id = target
        for asset in src.assets.glob("*"):
            if asset.is_file() and not (deck.assets / asset.name).exists():
                shutil.copyfile(asset, deck.assets / asset.name)
        deck.save_slide(html, str(slide.get("title") or "Slide"), layout_id, "deck stitch", source="master")

    slide_ids = [str(s["id"]) for s in deck.slides()]
    try:
        outcome = engine_service.export_slides(deck, Path(work_dir) / "export", slide_ids=slide_ids)
    except Exception as exc:  # noqa: BLE001
        _log.warning("deck export failed", exc_info=True)
        raise StitchError(f"The deck export failed ({type(exc).__name__}).") from exc
    out = Path(work_dir) / "out"
    out.mkdir(parents=True, exist_ok=True)
    target_path = out / "deck.pptx"
    shutil.copyfile(outcome.pptx, target_path)
    return StitchResult(pptx=target_path, slide_ids=slide_ids, element_count=outcome.element_count,
                        review_flag_count=outcome.review_flag_count, moved=moved)


def _reuse_master(src: DesignDeck, deck: DesignDeck) -> None:
    """Copy the first part's imported master (manifest, backgrounds, assets) instead of re-rendering."""
    if not src.manifest_path.is_file():
        engine_service.import_master(deck, deck.master_path, "master.pptx", renderer=BlankLayoutsRenderer())
        return
    shutil.copyfile(src.manifest_path, deck.manifest_path)
    for png in src.layouts_dir.glob("*"):
        if png.is_file():
            shutil.copyfile(png, deck.layouts_dir / png.name)
    for asset in src.assets.glob("*"):
        if asset.is_file():
            shutil.copyfile(asset, deck.assets / asset.name)
    block = src.master()
    deck.update(lambda m: m.update(master=block))


def export_deck(deck: DesignDeck, work_dir: Path, *, slide_ids: list[str] | None = None) -> engine_service.ExportOutcome:
    """Export a design deck (all its slides, or `slide_ids`) to `<work_dir>/out/<name>.pptx`."""
    if not deck.slides():
        raise StitchError("There are no slides to export yet.")
    outcome = engine_service.export_slides(deck, Path(work_dir) / "export", slide_ids=slide_ids)
    out = Path(work_dir) / "out"
    out.mkdir(parents=True, exist_ok=True)
    target = out / "deck.pptx"
    shutil.copyfile(outcome.pptx, target)
    outcome.pptx = target
    return outcome
