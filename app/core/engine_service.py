"""The service's one door into the export engine: export, master import, readiness.

Replaces Slide Studio `server/engine_bridge.py` (migration plan §4.1, R). The engine (`app/engine`)
is deterministic and calls no model; this module runs it on the right threads and keeps what it
measured beside the deck:

* **Export** (`export_slides`): `app.engine.pipeline.export_deck` on an engine thread (the browser
  pool, `app/core/browser_pool.py`), into a directory the caller names (a job workspace), with the
  extraction diagnostics stored in the deck (`diagnostics.json`) for `readiness`. It also counts what
  the export built (`element_count`) and what a reviewer should look at (`review_flag_count`), so the
  pipeline can fill both instead of leaving them null (the orchestrate gap).
* **Import** (`import_master`): `app.engine.importer.import_master` + `ingest` into the deck, with
  the layout backgrounds from the `Renderer` port (PptxRender over HTTP, or a fake in tests). A
  re-import keeps each layout position's id, so slides stay on their layouts.
* **Readiness** (`readiness`): the static linter plus the last export's measured diagnostics, warn
  only (a finding never blocks an export).
* `engine_status()`: whether the engine imports and a renderer is configured (for `/readyz` later).

The browser pool here is the engine's (`SLIDE_ENGINE_BROWSER_POOL_SIZE`); previews and the design
review use their own (`app/core/preview.py`) so a long export never queues a preview.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from app.config import engine as engine_config
from app.core.browser_pool import BrowserPool
from app.core.design.deck import DesignDeck, now
from app.engine import importer, pipeline
from app.engine.manifest import Manifest
from app.engine.renderer import PptxRenderClient, Renderer
from app.engine.reports import ExportOptions, ExportResult
from app.engine.verify import lint as engine_lint

_log = logging.getLogger(__name__)

T = TypeVar("T")
JSON = dict[str, Any]

#: The measured text checks the extraction emits that keep their own rule id.
EXTRACT_RULES: frozenset[str] = frozenset({"text-overflow", "text-overlap"})

_pool: BrowserPool | None = None
_pool_lock = threading.Lock()


def engine_pool() -> BrowserPool:
    """The engine's browser threads (one Chromium each), started on first use."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = BrowserPool(name="engine")
        return _pool


def run_on_engine(work: Callable[[], T], timeout: float | None = None) -> T:
    return engine_pool().run(work, timeout=timeout)


def shutdown() -> None:
    """Close the engine's browsers (tests, process shutdown)."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown()


def default_renderer() -> Renderer | None:
    """PptxRender from `SLIDE_ENGINE_RENDERER_URL`, or None when it is not configured."""
    return PptxRenderClient.from_settings()


def engine_status() -> JSON:
    return {"engine": True, "renderer": default_renderer() is not None}


# ---------------------------------------------------------------------------------------- export
@dataclass
class ExportOutcome:
    pptx: Path
    slide_ids: list[str]
    element_count: int
    #: Per slide: the extraction diagnostics, plus lint errors and warnings at export time.
    review_flags: dict[str, list[JSON]] = field(default_factory=dict)
    elapsed_s: float = 0.0

    @property
    def review_flag_count(self) -> int:
        return sum(len(flags) for flags in self.review_flags.values())


def export_slides(deck: DesignDeck, out_dir: Path, *, slide_ids: list[str] | None = None,
                  on_event: Callable[[JSON], None] | None = None) -> ExportOutcome:
    """Export the deck (or `slide_ids`) to one `.pptx` under `out_dir`. Runs on an engine thread."""
    started = time.perf_counter()
    out_dir = Path(out_dir)
    options = ExportOptions(out_dir=out_dir, slide_ids=slide_ids, verify=False, on_event=on_event,
                            workspace=out_dir / "work")
    result: ExportResult = run_on_engine(lambda: pipeline.export_deck(deck.dir, options))
    _store_diagnostics(deck, result)
    flags: dict[str, list[JSON]] = {}
    for sid in result.slide_ids:
        report = readiness(deck, sid)
        flags[sid] = [f for f in report["findings"] if f.get("level") in ("error", "warn")]
    count = sum(len(ir.elements) for ir in result.irs)
    return ExportOutcome(pptx=result.pptx, slide_ids=list(result.slide_ids), element_count=count, review_flags=flags,
                         elapsed_s=round(time.perf_counter() - started, 3))


def _diagnostics(deck: DesignDeck) -> JSON:
    try:
        data = json.loads((deck.dir / "diagnostics.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _store_diagnostics(deck: DesignDeck, result: ExportResult) -> None:
    versions = {s["id"]: int(s.get("current") or 1) for s in deck.slides()}
    stored = _diagnostics(deck)
    for ir in result.irs:
        sid = str(ir.slide.id)
        stored[sid] = {"version": versions.get(sid), "at": now(), "diagnostics": [d.to_json() for d in ir.diagnostics]}
    (deck.dir / "diagnostics.json").write_text(json.dumps(stored, indent=2), encoding="utf-8")


# ------------------------------------------------------------------------------------- readiness
def readiness(deck: DesignDeck, slide_id: str) -> JSON:
    """Lint one slide and merge in the diagnostics the last export measured for it. Warn only."""
    meta = deck.meta()
    slide = deck.find_slide(meta, slide_id)
    version = int(slide.get("current") or 1)
    html = deck.read_slide(slide_id, version)
    findings: list[JSON] = []
    notes: list[str] = []
    manifest = None
    block = deck.master()
    if block.get("layouts"):
        try:
            manifest = Manifest.from_json(block)
        except Exception as exc:  # noqa: BLE001 - a manifest we cannot read: fonts are not checked
            notes.append(f"the master manifest could not be read ({exc}); fonts are not checked")
    report = engine_lint.lint(html, manifest, layout_id=slide.get("layoutId") or None)
    findings += [{"level": f.level, "rule": f.rule, "message": f.message, "line": f.line, "snippet": f.snippet,
                  "source": "lint"} for f in report.findings]
    stored = _diagnostics(deck).get(slide_id)
    if isinstance(stored, dict):
        if stored.get("version") == version:
            findings += [_measured(d) for d in stored.get("diagnostics") or [] if isinstance(d, dict)]
        else:
            notes.append(f"the measured diagnostics are from version {stored.get('version')}; export again to "
                         "refresh them")
    errors = [f for f in findings if f["level"] == "error"]
    warnings = [f for f in findings if f["level"] == "warn"]
    return {"slideId": slide_id, "version": version, "findings": findings, "errors": len(errors),
            "warnings": len(warnings), "ready": not errors, "blocking": False, "notes": notes}


def _measured(d: JSON) -> JSON:
    source = str(d.get("source") or "")
    named = source in EXTRACT_RULES
    return {"level": d.get("level", "warn"), "rule": source if named else "extract", "message": d.get("message", ""),
            "line": None, "snippet": d.get("elementId"), "source": "extract", "where": None if named else source or None}


# ---------------------------------------------------------------------------------------- import
class MasterImportFailed(RuntimeError):
    """The master could not be imported; the message is safe to show."""


def import_master(deck: DesignDeck, master: Path, filename: str, *, renderer: Renderer | None,
                  render: bool = True) -> JSON:
    """Import `master` into the deck: manifest, layout backgrounds, assets. Returns the master block.

    With `render=True` a renderer is required (PptxRender, or a fake in tests); without one the
    import says so and nothing falls back silently.
    """
    existing = {(int(lay.get("masterIndex", 0)), int(lay.get("layoutIndex", 0))): str(lay["id"])
                for lay in (deck.master().get("layouts") or []) if lay.get("id")}
    work = deck.dir / ".import"
    try:
        manifest = importer.import_master(master, work, render=render, renderer=renderer,
                                          layout_ids=existing or None)
        manifest = importer.ingest(deck.dir, manifest, work, importer="deterministic")
    except Exception as exc:  # noqa: BLE001 - the deck stays usable; the caller reports it
        _log.warning("master import failed", exc_info=True)
        raise MasterImportFailed(f"The master could not be imported ({type(exc).__name__}: {exc}).") from exc
    if Path(master).resolve() != deck.master_path.resolve():
        deck.master_path.write_bytes(Path(master).read_bytes())
    block = manifest.to_json()
    block.update(filename=filename, status="ready", importedAt=now(), importer="deterministic",
                 summary=f"{len(manifest.layouts)} layouts across {len(manifest.masters)} master(s), canvas "
                         f"{manifest.canvas_w}×{manifest.canvas_h} px.")
    deck.update(lambda m: m.update(master=block))
    return block


def rehome_slides(deck: DesignDeck) -> list[str]:
    """Move every slide whose layout the (new) master does not have onto its first layout, as a new
    version. Returns the ids moved. Ported from Slide Studio `services/slides.rehome_slides`."""
    layouts = [lay for lay in deck.master().get("layouts") or [] if lay.get("id")]
    if not layouts:
        return []
    known = {lay["id"] for lay in layouts}
    first = str(layouts[0]["id"])
    moved: list[str] = []
    for s in deck.slides():
        if s.get("layoutId") in known:
            continue
        html = deck.read_slide(str(s["id"]))
        why = "the new master does not have the layout it was on" if s.get("layoutId") else "it had no layout"
        deck.save_slide(html, str(s.get("title") or ""), first, f"Moved when the master changed: {why}",
                        sid=str(s["id"]), source="master")
        moved.append(str(s["id"]))
    return moved


# keep `engine_config` imported for callers that read engine constants through this module
__all__ = ["engine_config", "export_slides", "import_master", "readiness", "rehome_slides", "engine_status"]
