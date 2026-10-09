"""Layout previews for a master: an internal call to PptxRender through the `Renderer` port.

Replaces Slide Studio `server/compat/render.py` (migration plan §4.1, R), which proxied Darwin's
`RENDER_API_URL` at `/render-layouts`. There is no route here: the brand-setup flow calls
`render_layouts(...)` directly, and only this service talks to PptxRender (decision D3b).

The answer is the zip Darwin's brand setup already reads: `layout-NN-<slug>.png`, one per layout,
NN being PptxRender's enumeration index. The engine's renderer client joins PptxRender's PNGs to the
package by filename (it serves no `index.json`, C13); the PNG names here come from that join.

Errors keep the old status split as a kind, so a later route can map them unchanged:
`invalid` (a bad deck: the caller skips previews, no retry) and `unavailable` (PptxRender is down or
not configured: retry later).
"""

from __future__ import annotations

import io
import logging
import tempfile
import zipfile
from pathlib import Path
from typing import Literal

from app.core.masters import MasterError, check_master_bytes
from app.engine.renderer import Renderer, RendererError, layout_identities

_log = logging.getLogger(__name__)

DEFAULT_WIDTH = 1600
MIN_WIDTH = 64
MAX_WIDTH = 4096


class RenderLayoutsError(RuntimeError):
    def __init__(self, kind: Literal["invalid", "unavailable"], message: str) -> None:
        super().__init__(message)
        self.kind = kind


def render_layouts(data: bytes, *, renderer: Renderer | None, width: int = DEFAULT_WIDTH,
                   filename: str = "template.pptx") -> bytes:
    """Render a master's layouts to PNGs and return them as a zip of `layout-NN-<slug>.png`."""
    if not MIN_WIDTH <= width <= MAX_WIDTH:
        raise RenderLayoutsError("invalid", f"width must be between {MIN_WIDTH} and {MAX_WIDTH}")
    try:
        check_master_bytes(data, filename)
    except MasterError as exc:
        raise RenderLayoutsError("invalid", str(exc)) from exc
    if renderer is None:
        raise RenderLayoutsError("unavailable", "no layout renderer is configured (SLIDE_ENGINE_RENDERER_URL)")
    with tempfile.TemporaryDirectory(prefix="render-layouts-") as tmp:
        pptx = Path(tmp) / "template.pptx"
        pptx.write_bytes(data)
        try:
            identities = layout_identities(pptx)
        except Exception as exc:  # noqa: BLE001 - a zip that is not a readable deck
            raise RenderLayoutsError("invalid", "could not read the deck's layouts") from exc
        if not identities:
            raise RenderLayoutsError("invalid", "the deck has no renderable layouts")
        try:
            mapping = renderer.render_layouts(pptx, width, Path(tmp) / "out")
        except RendererError as exc:
            text = str(exc)
            if "did not answer" in text or "failed (5" in text:
                raise RenderLayoutsError("unavailable", "the layout renderer is unavailable") from exc
            raise RenderLayoutsError("invalid", "could not render layouts for this deck") from exc
        if not mapping:
            raise RenderLayoutsError("invalid", "the deck has no renderable layouts")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for png in sorted(mapping.values(), key=lambda p: p.name):
                archive.write(png, png.name)
    return buffer.getvalue()
