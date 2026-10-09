"""Test doubles for the engine's `Renderer` port (app/engine/renderer.py).

* `FakeRenderer`: records every call; `render_layouts` writes white PNGs named exactly as the
  deployed PptxRender names them, `render` writes one white PNG per slide, `verify` answers a clean
  report. For tests that need a renderer-shaped object and do not judge pixels.
* `pptxrender_transport(...)`: an `httpx2.MockTransport` that answers like the deployed
  PptxRender (`/render-layouts` only, PNGs in a zip, no index), for testing the HTTP client itself.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2 as httpx
from PIL import Image
from pptx import Presentation

from app.engine.renderer import BlankLayoutsRenderer, layout_identities, slide_size_emu
from app.engine.reports import GateReport, GateSlide


class FakeRenderer:
    """A `Renderer` that draws nothing worth judging, and remembers what it was asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def render_layouts(self, pptx: Path, width: int, out_dir: Path) -> dict[str, Path]:
        self.calls.append(("render_layouts", Path(pptx).name, width))
        return BlankLayoutsRenderer().render_layouts(pptx, width, out_dir)

    def render(self, pptx: Path, width: int, out_dir: Path) -> list[Path]:
        self.calls.append(("render", Path(pptx).name, width))
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        cx, cy = slide_size_emu(pptx)
        height = max(1, round(width * cy / cx))
        pngs = []
        for number in range(1, len(Presentation(str(pptx)).slides) + 1):
            png = out_dir / f"slide-{number:02d}.png"
            Image.new("RGB", (width, height), "white").save(png)
            pngs.append(png)
        return pngs

    def verify(self, pptx: Path, references: list[Path], out_dir: Path) -> GateReport:
        self.calls.append(("verify", Path(pptx).name, len(references)))
        return GateReport(
            slides=[GateSlide(index=i) for i in range(1, len(references) + 1)],
            slides_checked=len(references),
            source="fake",
        )


def layout_zip(pptx: Path, width: int, *, extra: dict[str, bytes] | None = None,
               drop: int | None = None) -> bytes:
    """The zip the deployed PptxRender returns for `pptx`: `layout-NN-<slug>.png` per layout."""
    cx, cy = slide_size_emu(pptx)
    height = max(1, round(width * cy / cx))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for identity in layout_identities(pptx):
            if drop is not None and identity.renderIndex == drop:
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", identity.name.lower()).strip("-") or "layout"
            png = io.BytesIO()
            Image.new("RGB", (width, height), "white").save(png, format="PNG")
            archive.writestr(f"layout-{identity.renderIndex:02d}-{slug}.png", png.getvalue())
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def pptxrender_transport(
    pptx: Path,
    *,
    seen: list[httpx.Request] | None = None,
    respond: Callable[[httpx.Request], httpx.Response] | None = None,
    **zip_options: Any,
) -> httpx.MockTransport:
    """A transport that answers `/render-layouts` like the deployed service and 404s anything else."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if respond is not None:
            return respond(request)
        if request.url.path.endswith("/render-layouts"):
            body = request.read().decode("latin-1")
            match = re.search(r'name="width"\r\n\r\n(\d+)', body)
            width = int(match.group(1)) if match else 1280
            return httpx.Response(200, content=layout_zip(pptx, width, **zip_options),
                                  headers={"content-type": "application/zip"})
        return httpx.Response(404, json={"title": "Not Found", "status": 404})

    return httpx.MockTransport(handler)
