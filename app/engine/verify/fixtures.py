"""Where a fixture slide's master, layout and assets come from.

Two fixture shapes exist, and tools that run "a fixture slide" need both:

* **A project-shaped fixture** (`tests/engine/fixtures/sample/`, synthetic): a `manifest.json`, a
  `layouts/` folder and an `assets/` folder beside the slides, exactly like a real project.
* **The torture families** (`tests/engine/fixtures/torture/`) — one slide per construct family, each naming
  its master in `<family>.expect.json` because the families deliberately span **two** masters
  (`test-16x9` and `test-4x3`) to prove size generality. There is no single `manifest.json` here and
  there must not be: a tool that assumes one silently measures the 4:3 slides against a 16:9 canvas.

`context_for(html)` answers the question either way, so the reference renderer and the torture
runner resolve a fixture the same way. Tests and CI only: production projects are workspaces.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from app.config import engine as config
from app.engine.manifest import Layout, Manifest


class FixtureError(RuntimeError):
    """A fixture slide cannot be resolved to a master, a layout and a background."""


#: A fixture that shares another fixture's master names it here — one path, relative to the fixture
#: directory — instead of carrying a second copy of the file.
MASTER_REF = "master.ref"


def referenced_master(directory: Path) -> Path | None:
    """The master a fixture's `master.ref` points at, or None when it has no `master.ref`."""
    ref = Path(directory) / MASTER_REF
    if not ref.is_file():
        return None
    target = ref.read_text(encoding="utf-8").strip()
    if not target:
        raise FixtureError(f"{ref} is empty: it must name the shared master, relative to {directory}")
    master = (Path(directory) / target).resolve()
    if not master.exists():
        raise FixtureError(f"{ref} names {target!r}, but {master} does not exist")
    return master


@dataclass(slots=True)
class FixtureContext:
    """Everything needed to measure, emit and score one fixture slide."""

    html: Path
    manifest: Manifest
    layout_id: str
    master: Path
    assets_dir: Path
    layouts_dir: Path
    expect: dict[str, Any] | None = None

    @property
    def layout(self) -> Layout:
        return self.manifest.layout(self.layout_id)

    @property
    def layout_png(self) -> Path:
        layout = self.layout
        if not layout.background:
            raise FixtureError(f"{self.layout_id} has no background render")
        return self.layouts_dir / layout.background


def expect_for(html: Path) -> dict[str, Any] | None:
    """The `<family>.expect.json` beside a torture slide, when there is one."""
    path = Path(html).with_suffix(".expect.json")
    if not path.exists():
        return None
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def context_for(html: Path, *, layout_id: str | None = None) -> FixtureContext:
    """Resolve a fixture slide to its master, layout, assets and layout backgrounds."""
    html = Path(html)
    if not html.exists():
        raise FixtureError(f"no such fixture slide: {html}")
    directory = html.parent

    expect = expect_for(html)
    if expect and expect.get("master"):
        stem = expect["master"]
        manifest_path = config.MASTERS_FIXTURE / f"{stem}.manifest.json"
        master = config.MASTERS_FIXTURE / f"{stem}.pptx"
        layouts = config.MASTERS_FIXTURE / stem / "layouts"
        if not manifest_path.exists():
            raise FixtureError(
                f"{html.name} names master {stem!r} but {manifest_path} is missing — run "
                f"tests/engine/fixtures/masters/make_test_masters.py"
            )
        manifest = Manifest.load(manifest_path)
        return FixtureContext(
            html=html,
            manifest=manifest,
            layout_id=layout_id or expect.get("layoutId") or manifest.layouts[0].id,
            master=master,
            assets_dir=directory / "assets",
            layouts_dir=layouts,
            expect=expect,
        )

    # A project-shaped fixture: manifest, layouts/ and assets/ sit beside the slide.
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        raise FixtureError(
            f"{html.name} has neither a <family>.expect.json naming its master nor a manifest.json "
            f"beside it in {directory}"
        )
    manifest = Manifest.load(manifest_path)
    candidates = [directory / "master.pptx"]
    shared = referenced_master(directory)
    if shared is not None:
        candidates.append(shared)
    uploads = directory / "uploads"
    if uploads.is_dir():
        candidates.extend(sorted(uploads.glob("*.pptx")))
    master = next((p for p in candidates if p.exists()), candidates[0])
    return FixtureContext(
        html=html,
        manifest=manifest,
        layout_id=layout_id or _project_layout(directory, html, manifest),
        master=master,
        assets_dir=directory / "assets",
        layouts_dir=directory / "layouts",
        expect=None,
    )


def _project_layout(directory: Path, html: Path, manifest: Manifest) -> str:
    """Which layout a project-shaped fixture's slide sits on, from its `project.json`."""
    project = directory / "project.json"
    if project.exists():
        data = json.loads(project.read_text(encoding="utf-8"))
        slides = data.get("slides") or []
        if html.stem.startswith("slide-"):
            try:
                index = int(html.stem.split("-")[1]) - 1
            except ValueError:
                index = -1
            if 0 <= index < len(slides):
                return slides[index].get("layoutId") or manifest.layouts[0].id
        for slide in slides:
            if slide["id"] == html.stem:
                return slide.get("layoutId") or manifest.layouts[0].id
    return manifest.layouts[0].id
