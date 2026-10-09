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


# ------------------------------------------------------------------------------- installed fonts

#: Why a test that needs one of these faces skips on Linux: they ship only with Windows or Office.
#: They are not in `ttf-mscorefonts-installer` (which CI installs, as Slide Studio's CI and image do:
#: Arial, Arial Black, Times New Roman, Georgia, Verdana, Courier New, Trebuchet MS, Comic Sans MS,
#: Impact, Webdings, Andale Mono), and their licences forbid redistributing them, so no Linux runner
#: can have them. The measurements those tests make are of the faces themselves, so a metric-compatible
#: stand-in (Carlito for Calibri) cannot replace them.
WINDOWS_ONLY_REASON = ("needs {missing}, which ship only with Windows or Office (not in "
                       "ttf-mscorefonts-installer, not redistributable): platform-dependent")


def installed(family: str, weight: int = 400, italic: bool = False) -> bool:
    """Whether `family` itself (not a stand-in) is installed in the font folders the engine scans."""
    from app.engine.emit.text import face_for_exact

    return face_for_exact(family, weight, italic) is not None


def available(families: Any) -> list[str]:
    """The subset of `families` installed here, in order."""
    return [family for family in families if installed(family)]


def require_faces(*families: str) -> None:
    """Skip the calling test, with the platform reason, unless every one of `families` is installed."""
    import pytest

    missing = [family for family in families if not installed(family)]
    if missing:
        pytest.skip(WINDOWS_ONLY_REASON.format(missing=", ".join(missing)))


def measuring_face(family: str, weight: int = 400, italic: bool = False) -> Any:
    """The face a run in `family` is measured in here: the family itself, else its metric-compatible
    stand-in (`METRIC_ALIASES`: Calibri -> Carlito on Linux), else None. No generic fallback: a test
    that measures advances must measure the face the browser drew or one with identical metrics."""
    from app.engine.emit.text import METRIC_ALIASES, face_for_exact

    face = face_for_exact(family, weight, italic)
    for alias in METRIC_ALIASES.get(family.strip().lower(), ()) if face is None else ():
        face = face_for_exact(alias, weight, italic)
        if face is not None:
            break
    return face


#: Faces that ship only with Windows or Office (see `WINDOWS_ONLY_REASON`), lower-cased.
WINDOWS_ONLY_FACES = frozenset({"calibri", "calibri light", "cambria", "segoe ui", "segoe ui semibold",
                                "bahnschrift", "arial rounded mt bold"})


def is_platform_font_diagnostic(diagnostic: Any) -> bool:
    """A "theme font ... is not installed here" report about a Windows-only face this machine lacks.

    True on Linux for the synthetic test masters' Calibri Light (no metric twin exists), never on a
    machine that has the face. Tests about something else (pseudo-elements, paint) set it aside; the
    tests about font substitution itself keep it.
    """
    import re

    if getattr(diagnostic, "source", None) != "fonts":
        return False
    match = re.match(r"theme font '([^']+)' is not installed here", str(getattr(diagnostic, "message", "")))
    return bool(match and match.group(1).lower() in WINDOWS_ONLY_FACES and not installed(match.group(1)))


def without_platform_font_diagnostics(ir: IR) -> IR:
    """`ir` with `is_platform_font_diagnostic` reports removed (in place; returned for chaining)."""
    ir.diagnostics = [d for d in ir.diagnostics if not is_platform_font_diagnostic(d)]
    return ir
