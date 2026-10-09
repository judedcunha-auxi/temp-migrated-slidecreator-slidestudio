"""Measurement: slide HTML in a real browser -> IR.

`html.py` walks the DOM; `svg.py` expands every `<svg>` root. Both write derived image assets
(isolated rasters, rasterised `.svg` images, SVG `image` data URIs) and both put them in the same
place, `derived_dir(workspace, slide_id)`, so that:

* the project's own `assets/` folder stays an *input* (it may be a read-only copy, and a live
  project should not collect engine by-products), and
* everything one export produced lives under that export's own workspace (one per job), so two
  exports running at once never share or clean each other's files. There is no process-wide
  derived folder.
"""

from __future__ import annotations

from pathlib import Path

#: The workspace subfolder derived images go in.
DERIVED_SUBDIR = "derived"


def derived_dir(workspace: Path, slide_id: str, *, clean: bool = False) -> Path:
    """Where this slide's derived images go: `<workspace>/derived/<slide id>/`.

    Stable per (workspace, slide id), so two runs in the same workspace write the same paths.
    `clean=True` empties it first (a re-measure must not see the previous run's files).
    """
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in (slide_id or "slide")).strip(".") or "slide"
    path = Path(workspace) / DERIVED_SUBDIR / safe
    if clean and path.exists():
        for child in path.iterdir():
            if child.is_file():
                child.unlink()
    path.mkdir(parents=True, exist_ok=True)
    return path
