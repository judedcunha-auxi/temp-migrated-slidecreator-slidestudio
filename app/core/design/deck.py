"""A deck being designed: one directory inside a job workspace, laid out as the engine reads it.

Replaces Slide Studio's `server/store.Project`. The service keeps no lasting data of its own, so a
design deck lives only for one job attempt, under that job's scratch workspace
(`app/core/storage/workspace.job_workspace`): it is materialised from the job's inputs, the design
turn and the export write into it, and what must last leaves through the storage port (the
workspace's `out/` folder, or `app/core/design/persist.py`). Nothing here writes outside the
directory it was given.

    <dir>/project.json          name, model, the master block (manifest v2), slides + versions
    <dir>/manifest.json         the imported master's manifest (the engine reads it)
    <dir>/master.pptx           the master
    <dir>/layouts/              layout backgrounds
    <dir>/assets/               images slides may reference
    <dir>/slides/<sid>/vNNN.html
    <dir>/attachments/          what the person (or the pipeline) handed the design turn
    <dir>/history.json          the model's memory of the design chat (canonical history)
    <dir>/transcript.json       what the chat said, turn by turn
    <dir>/diagnostics.json      per slide: what the last export measured

The layout is exactly the one `app.engine.pipeline.load_project` reads, so an export needs no copy.
Every method is safe to call from several threads at once (parallel Generate designs several
slides into one deck): writes to `project.json` go through one lock and an atomic replace.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.storage.paths import safe_join

JSON = dict[str, Any]

SLIDE_ID = re.compile(r"sld_[0-9a-f]{10}")
#: A slide id in a reply or a tool argument.
DEFAULT_CANVAS = {"w": 1280, "h": 720, "pxPerIn": 96}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


#: On Windows a replace fails while another thread has the target open for a moment (a parallel
#: Generate turn reading project.json), and a read fails while a replace is in flight. Both are
#: retried briefly; a file that stays locked still raises.
_ATTEMPTS = 20


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    for attempt in range(_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == _ATTEMPTS - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.01 * (attempt + 1))


def _read_json(path: Path, default: Any) -> Any:
    for attempt in range(_ATTEMPTS):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except PermissionError:
            if attempt == _ATTEMPTS - 1:
                raise
            time.sleep(0.01 * (attempt + 1))
    return default


class DesignDeck:
    """One deck's working directory. See the module docstring."""

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory).resolve()
        if not (self.dir / "project.json").is_file():
            raise KeyError("not a design deck directory")
        self._lock = threading.RLock()

    # --------------------------------------------------------------------------- creation
    @classmethod
    def create(cls, directory: Path, name: str, *, model: str | None = None, deck_id: str | None = None) -> DesignDeck:
        """A new, empty deck in `directory` (created; must be empty or missing)."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        if any(directory.iterdir()):
            raise FileExistsError("the deck directory is not empty")
        meta: JSON = {"id": deck_id or new_id("prj"), "name": (name or "").strip() or "Untitled deck",
                      "createdAt": now(), "updatedAt": now(), "model": model, "master": None, "slides": []}
        for sub in ("layouts", "assets", "slides", "attachments"):
            (directory / sub).mkdir()
        _write_json(directory / "project.json", meta)
        return cls(directory)

    # ------------------------------------------------------------------------------ paths
    @property
    def id(self) -> str:
        return str(self.meta().get("id") or self.dir.name)

    @property
    def layouts_dir(self) -> Path:
        return self.dir / "layouts"

    @property
    def assets(self) -> Path:
        return self.dir / "assets"

    @property
    def attachments(self) -> Path:
        return self.dir / "attachments"

    @property
    def master_path(self) -> Path:
        return self.dir / "master.pptx"

    @property
    def manifest_path(self) -> Path:
        return self.dir / "manifest.json"

    # ------------------------------------------------------------------------- documents
    def meta(self) -> JSON:
        data = _read_json(self.dir / "project.json", {})
        return data if isinstance(data, dict) else {}

    def update(self, fn: Callable[[JSON], None]) -> JSON:
        with self._lock:
            meta = self.meta()
            fn(meta)
            meta["updatedAt"] = now()
            _write_json(self.dir / "project.json", meta)
            return meta

    def history(self) -> list[JSON]:
        data = _read_json(self.dir / "history.json", [])
        return data if isinstance(data, list) else []

    def save_history(self, history: list[JSON]) -> None:
        with self._lock:
            _write_json(self.dir / "history.json", history)

    def transcript(self) -> list[JSON]:
        data = _read_json(self.dir / "transcript.json", [])
        return data if isinstance(data, list) else []

    def add_transcript(self, entry: JSON) -> None:
        with self._lock:
            entries = self.transcript()
            entries.append(entry)
            _write_json(self.dir / "transcript.json", entries)

    def master(self) -> JSON:
        block = self.meta().get("master")
        return block if isinstance(block, dict) else {}

    def canvas(self) -> tuple[int, int]:
        c = self.master().get("canvas") or DEFAULT_CANVAS
        return int(c.get("w") or 1280), int(c.get("h") or 720)

    # ---------------------------------------------------------------------------- slides
    def slide_path(self, sid: str, version: int) -> Path:
        if not SLIDE_ID.fullmatch(sid):
            raise KeyError(sid)
        return safe_join(self.dir, "slides", sid, f"v{int(version):03d}.html")

    @staticmethod
    def find_slide(meta: JSON, sid: str) -> JSON:
        for slide in meta.get("slides") or []:
            if slide.get("id") == sid:
                return dict(slide) if not isinstance(slide, dict) else slide
        raise KeyError(sid)

    def slides(self) -> list[JSON]:
        return list(self.meta().get("slides") or [])

    def read_slide(self, sid: str, version: int | None = None) -> str:
        slide = self.find_slide(self.meta(), sid)
        return self.slide_path(sid, version or int(slide["current"])).read_text(encoding="utf-8")

    def save_slide(self, html: str, title: str, layout_id: str | None, instruction: str, *,
                   sid: str | None = None, position: int | None = None, source: str | None = None,
                   summary: str | None = None) -> JSON:
        """Write a new version of a slide (a new slide when `sid` is None); returns the slide record."""
        with self._lock:
            meta = self.meta()
            if sid:
                slide = self.find_slide(meta, sid)
            else:
                slide = {"id": new_id("sld"), "title": title, "layoutId": layout_id, "current": 0, "versions": []}
                slides = meta.setdefault("slides", [])
                if position is None or position >= len(slides):
                    slides.append(slide)
                else:
                    slides.insert(max(position, 0), slide)
            n = max((int(v["n"]) for v in slide["versions"]), default=0) + 1
            path = self.slide_path(slide["id"], n)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(html, encoding="utf-8")
            version: JSON = {"n": n, "createdAt": now(), "instruction": instruction[:300], "title": title,
                             "layoutId": layout_id}
            if source:
                version["source"] = source
            if isinstance(summary, str) and summary.strip():
                version["summary"] = " ".join(summary.split())[:200]
            slide["versions"].append(version)
            slide["current"] = n
            slide["title"] = title or slide["title"]
            if layout_id is not None:
                slide["layoutId"] = layout_id
            meta["updatedAt"] = now()
            _write_json(self.dir / "project.json", meta)
            return dict(slide)

    def update_slide(self, sid: str, fn: Callable[[JSON], None]) -> None:
        """Change one slide's record in place (its brief, a version's source line)."""
        def apply(meta: JSON) -> None:
            for slide in meta.get("slides") or []:
                if slide.get("id") == sid:
                    fn(slide)
        self.update(apply)

    def attach(self, name: str, data: bytes) -> Path:
        """Store a file the design turn is handed; returns its path (a safe, unique name)."""
        base = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._") or "file"
        stem, suffix = Path(base).stem or "file", Path(base).suffix
        candidate, n = f"{stem}{suffix}", 2
        while (self.attachments / candidate).exists():
            candidate, n = f"{stem}-{n}{suffix}", n + 1
        path = safe_join(self.attachments, candidate)
        path.write_bytes(data)
        return path
