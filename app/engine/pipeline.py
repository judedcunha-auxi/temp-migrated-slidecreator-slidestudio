"""The export pipeline, end to end: a project directory of slide HTML -> an editable `.pptx`.

Two rules this module exists to hold:

* **`export_deck` never runs the suite.** It exports and returns an `ExportResult`; a caller that
  wants verification chains `app.engine.verify.suite.run_suite` afterwards. Verification that
  exports, or export that verifies, makes both slow.
* **The engine never imports the API layer.** The project is read off disk from the directory the
  caller names (a per-job workspace in the service), and progress reaches the caller through
  `ExportOptions.on_event` alone.

A project directory holds `project.json`, `manifest.json`, the master (`master.pptx`, or the one
`.pptx` in `uploads/`), `assets/`, and each slide's HTML as `slides/<id>/vNNN.html` (or, for flat
fixtures, `slide-NN.html` in project order).

The browser used for measuring is the calling thread's (`app.core.browser_pool`); run exports on a
`BrowserPool` thread to measure several decks in parallel.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from app.engine.classify.charts import recognise_charts
from app.engine.classify.placeholders import map_placeholders
from app.engine.emit import pptx as emitter
from app.engine.extract.html import extract_html, measuring_page
from app.engine.ir import IR, Canvas
from app.engine.manifest import Manifest
from app.engine.reports import EmitOptions, ExportOptions, ExportResult
from app.engine.verify.text_layout import annotate_text_layout


class PipelineError(RuntimeError):
    """The project cannot be exported: a missing master, an unknown slide, a broken manifest."""


@dataclass(slots=True)
class ProjectSource:
    """Where one deck's inputs live."""

    id: str
    dir: Path
    data: dict[str, Any]
    manifest: Manifest
    master: Path
    assets: Path

    def slides(self, slide_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """The slides to export, in project order, each with its current version's HTML path."""
        wanted = set(slide_ids) if slide_ids else None
        chosen: list[dict[str, Any]] = []
        for index, slide in enumerate(self.data.get("slides") or [], start=1):
            if wanted is not None and slide["id"] not in wanted:
                continue
            version = int(slide.get("current") or 1)
            html = self._slide_html(slide["id"], version, index)
            chosen.append(
                {
                    "slideId": slide["id"],
                    "title": slide.get("title"),
                    "layoutId": slide.get("layoutId"),
                    "html": html,
                    "version": version,
                }
            )
        if wanted is not None:
            missing = wanted - {s["slideId"] for s in chosen}
            if missing:
                raise PipelineError(f"no such slide(s) in {self.id}: {sorted(missing)}")
        if not chosen:
            raise PipelineError(f"{self.id} has no slides to export")
        return chosen

    def _slide_html(self, slide_id: str, version: int, index: int) -> Path:
        """A slide's HTML: `slides/<id>/vNNN.html`, or flat `slide-NN.html` (numbered in project order)."""
        candidates = [
            self.dir / "slides" / slide_id / f"v{version:03d}.html",
            self.dir / f"slide-{index:02d}.html",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise PipelineError(f"slide {slide_id} v{version} has no HTML in {self.dir.name}")


def load_project(directory: Path) -> ProjectSource:
    """Read `project.json` and `manifest.json` from a project directory."""
    directory = Path(directory)
    project_json = directory / "project.json"
    if not project_json.exists():
        raise PipelineError(f"no project.json in {directory.name}")
    data = json.loads(project_json.read_text(encoding="utf-8"))

    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        raise PipelineError(f"{directory.name} has no manifest.json (import the master first)")
    manifest = Manifest.load(manifest_path)

    return ProjectSource(
        id=str(data.get("id") or directory.name),
        dir=directory,
        data=data,
        manifest=manifest,
        master=_find_master(directory, data),
        assets=directory / "assets",
    )


def _find_master(directory: Path, data: dict[str, Any]) -> Path:
    """The uploaded master deck: `master.pptx` beside `project.json`, else the one in `uploads/`."""
    master_block = data.get("master")
    named = master_block.get("filename") if isinstance(master_block, dict) else None
    uploads = directory / "uploads"
    candidates = [directory / "master.pptx"]
    if named:
        candidates.append(uploads / Path(str(named)).name)
    if uploads.is_dir():
        candidates.extend(sorted(uploads.glob("*.pptx")))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise PipelineError(f"no master .pptx found in {directory.name}")


def export_deck(project_dir: Path, options: ExportOptions) -> ExportResult:
    """Export a project's slides to one `.pptx`. Never runs the suite."""
    started = time.perf_counter()
    source = load_project(project_dir)
    slides = source.slides(options.slide_ids)
    out_dir = Path(options.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    workspace = options.work_dir
    workspace.mkdir(parents=True, exist_ok=True)

    canvas = Canvas(source.manifest.canvas_w, source.manifest.canvas_h)
    irs: list[IR] = []
    per_slide: dict[str, float] = {}

    options.event(type="status", message=f"measuring {len(slides)} slide(s)")
    with measuring_page(canvas) as page:
        for slide in slides:
            slide_started = time.perf_counter()
            options.event(type="status", message=f"extract {slide['slideId']}", slide=slide["slideId"])
            ir = extract_html(
                slide["html"],
                source.manifest,
                slide["layoutId"],
                source.assets,
                workspace=workspace,
                slide_id=slide["slideId"],
                title=slide["title"],
                page=page,
            )
            options.event(type="status", message=f"classify {slide['slideId']}", slide=slide["slideId"])
            ir = recognise_charts(ir)
            ir = map_placeholders(ir, source.manifest.layout(cast(str, ir.slide.layoutId)))
            # After classification, so a recognised chart's axis and value labels (which sit a few
            # px apart by design) cannot be read as two texts drawn over each other.
            ir = annotate_text_layout(ir)
            irs.append(ir)
            per_slide[slide["slideId"]] = round(time.perf_counter() - slide_started, 3)
            options.event(type="usage", elapsed=round(time.perf_counter() - started, 3), cost=0.0)

    options.event(type="status", message="emitting")
    out_path = out_dir / f"{_safe_stem(str(source.data.get('name') or source.id))}.pptx"
    emit_report = emitter.emit(irs, source.manifest, source.master, out_path, options.emit or EmitOptions())

    elapsed = round(time.perf_counter() - started, 3)
    options.event(type="export", file=out_path.name, elapsedS=elapsed)
    options.event(type="done", file=out_path.name)

    def rerun(target_dir: Path) -> ExportResult:
        """Export the same inputs again into `target_dir` (determinism checks compare the two)."""
        repeat = ExportOptions(
            out_dir=Path(target_dir),
            slide_ids=options.slide_ids,
            verify=False,
            on_event=None,
            emit=options.emit,
            workspace=Path(target_dir) / "work",
        )
        return export_deck(project_dir, repeat)

    return ExportResult(
        pptx=out_path,
        irs=irs,
        emit_report=emit_report,
        slide_ids=[slide["slideId"] for slide in slides],
        elapsed_s=elapsed,
        per_slide_s=per_slide,
        cost=0.0,
        renderer_warnings=[],
        rerun=rerun,
    )


def _safe_stem(name: str) -> str:
    """A filename stem from a project name: deterministic, no timestamp."""
    cleaned = "".join(c if c.isalnum() or c in " -_" else "-" for c in name).strip()
    return "-".join(cleaned.split()) or "export"
