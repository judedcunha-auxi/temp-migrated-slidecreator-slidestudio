"""Test-side wrappers over the engine's entry points.

The engine takes its workspace and its renderer as arguments (nothing global). Most tests do not
care where derived images go, so these wrappers fill in the current test's workspace, set by the
autouse fixture `_engine_workspace` in `tests/engine/conftest.py`. A test that cares passes its own.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from app.engine.extract import derived_dir
from app.engine.extract import html as _html
from app.engine.extract import svg as _svg
from app.engine.ir import IR
from app.engine.renderer import PptxRenderClient, RendererError
from app.engine.reports import GateReport

_workspace: Path | None = None


def set_workspace(path: Path | None) -> None:
    global _workspace
    _workspace = path


_fallback: Path | None = None


def current_workspace() -> Path:
    """The current test's workspace; session-scoped fixtures (built before any test's) get a shared one."""
    global _fallback
    if _workspace is not None:
        _workspace.mkdir(parents=True, exist_ok=True)
        return _workspace
    if _fallback is None:
        _fallback = Path(tempfile.mkdtemp(prefix="engine-session-ws-"))
    return _fallback


def extract_html(*args: Any, workspace: Path | None = None, **kwargs: Any) -> IR:
    """`app.engine.extract.html.extract_html`, in the current test's workspace by default."""
    return _html.extract_html(*args, workspace=workspace or current_workspace(), **kwargs)


def extract_many(*args: Any, workspace: Path | None = None, **kwargs: Any) -> list[IR]:
    return _html.extract_many(*args, workspace=workspace or current_workspace(), **kwargs)


def expand_svg(page: Any, svg_ids: list[str], canvas: Any, assets_dir: Path, *,
               derived: Path | None = None, slide_id: str = "slide") -> Any:
    """`app.engine.extract.svg.expand_svg`, writing into the current workspace by default."""
    out = derived or derived_dir(current_workspace(), slide_id)
    return _svg.expand_svg(page, svg_ids, canvas, assets_dir, derived=out)


def renderer() -> PptxRenderClient:
    """The configured PptxRender client; the calling test carries a `renderer*` marker."""
    client = PptxRenderClient.from_settings()
    if client is None:
        raise RendererError("no renderer configured (SLIDE_ENGINE_RENDERER_URL)")
    return client


def render(pptx: Path, width: int, out_dir: Path) -> list[Path]:
    return renderer().render(pptx, width, out_dir)


def render_layouts(pptx: Path, width: int, out_dir: Path) -> dict[str, Path]:
    return renderer().render_layouts(pptx, width, out_dir)


def verify(pptx: Path, references: list[Path], out_dir: Path) -> GateReport:
    return renderer().verify(pptx, references, out_dir)
