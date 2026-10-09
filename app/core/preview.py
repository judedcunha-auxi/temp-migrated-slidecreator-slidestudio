"""A picture of a slide as it will look, and its measured design review: what the designer and the critic see.

`render_slide_png(deck, slide_id)` returns a PNG of one slide version: its HTML composited over its
master layout background, at the master canvas size, theme fonts forced, `data-chart` charts drawn
by the engine's `chart-preview.js`, then downscaled. It is not a second rendering stack: the picture
is the engine's own gate reference (`app.engine.extract.html.render_reference`), the picture the
export is scored against. Assets are served from the deck's `assets/`; every remote URL is refused
under the engine's network policy, so a preview never touches the network.

`design_review(deck, slide_id)` measures the slide in the same browser and runs the design lint
(`app.core.design_refs.design_lint`), including the workzone and header-band rules when the deck
carries a workzone (`project.json: workzone`).

Both run on the **preview pool** (`DESIGN_PREVIEW_BROWSERS` threads, one Chromium each), separate
from the engine's, so a long export never queues a preview. Playwright's sync API is bound to the
thread that started it, so each preview thread keeps its own page and reuses it.

Ported from Slide Studio `server/slide_preview.py` (migration plan §4.1, K/R): the hand-rolled
preview threads are replaced by `app.core.browser_pool.BrowserPool`.
"""

from __future__ import annotations

import io
import logging
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from PIL import Image

from app.config import engine as engine_config
from app.config.ai import AISettings, ai_settings
from app.core.brand.workzone import normalize_bounds
from app.core.browser_pool import BrowserPool, thread_browser
from app.core.design.deck import DesignDeck

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page

_log = logging.getLogger(__name__)

T = TypeVar("T")
JSON = dict[str, Any]

DEFAULT_CANVAS = (1280, 720)
#: How long a caller waits for one preview (seconds). A cold start is ~2 s, a warm render well under 1.
TIMEOUT_S = 90.0


class SlidePreviewError(RuntimeError):
    """The preview could not be made; the message says why, in words a tool result can carry."""


_pool: BrowserPool | None = None
_pool_lock = threading.Lock()


def preview_pool(settings: AISettings | None = None) -> BrowserPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            s = settings if settings is not None else ai_settings
            _pool = BrowserPool(size=s.design_preview_browsers, name="preview")
        return _pool


def run_on_preview(work: Callable[[], T], timeout: float | None = TIMEOUT_S) -> T:
    """Run `work` on a preview thread (it may use `thread_browser()`) and return its result."""
    return preview_pool().run(work, timeout=timeout)


def shutdown() -> None:
    """Close the preview browsers (tests, process shutdown)."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown()


class _ThreadPage(threading.local):
    context: BrowserContext | None = None
    page: Page | None = None
    size: tuple[int, int] | None = None
    scratch: Path | None = None


_local = _ThreadPage()


def _page(w: int, h: int) -> Page:
    """This preview thread's page at the canvas size, made on first use and reused while it lives."""
    page = _local.page
    if page is not None and not page.is_closed() and _local.size == (w, h):
        return page
    _drop_page()
    context = thread_browser().new_context(viewport={"width": w, "height": h}, device_scale_factor=1,
                                           color_scheme="light")
    _local.context, _local.page, _local.size = context, context.new_page(), (w, h)
    return _local.page


def _drop_page() -> None:
    for thing in (_local.page, _local.context):
        try:
            if thing is not None:
                thing.close()
        except Exception:  # noqa: BLE001 - a page that will not close is already gone
            _log.debug("preview: close failed", exc_info=True)
    _local.page = _local.context = None
    _local.size = None


def _scratch() -> Path:
    if _local.scratch is None or not _local.scratch.exists():
        _local.scratch = Path(tempfile.mkdtemp(prefix="slide-preview-"))
    return _local.scratch


def _fonts(master: JSON) -> dict[str, str]:
    fonts = (master.get("theme") or {}).get("fonts") or {}
    major = fonts.get("major") or fonts.get("heading") or ""
    minor = fonts.get("minor") or fonts.get("body") or ""
    return {k: v for k, v in (("major", major), ("minor", minor)) if v}


def _job(deck: DesignDeck, slide_id: str, version: int | None) -> JSON:
    """Everything the browser thread needs, read and checked on the caller's thread."""
    meta = deck.meta()
    try:
        slide = deck.find_slide(meta, slide_id)
        n = int(version or slide.get("current") or 1)
        html = deck.slide_path(slide_id, n)
    except KeyError:
        raise SlidePreviewError(f"no slide {slide_id!r} in this deck") from None
    if not html.is_file():
        raise SlidePreviewError(f"slide {slide_id} has no version {n}")
    master = deck.master()
    canvas = master.get("canvas") or {}
    w, h = int(canvas.get("w") or DEFAULT_CANVAS[0]), int(canvas.get("h") or DEFAULT_CANVAS[1])
    layout = next((lay for lay in master.get("layouts") or [] if lay.get("id") == slide.get("layoutId")), None)
    background: Path | None = None
    if layout and isinstance(layout.get("background"), str):
        candidate = deck.layouts_dir / Path(layout["background"]).name
        if candidate.is_file() and candidate.suffix.lower() == ".png":
            background = candidate
    return {"html": html, "background": background, "w": w, "h": h, "assets": deck.assets, "fonts": _fonts(master),
            "master": master, "layout_id": slide.get("layoutId")}


def _background(job: JSON) -> Path:
    """A PNG at canvas size for the layout layer: the layout's own (resized), or white."""
    out = _scratch() / f"bg-{uuid.uuid4().hex}.png"
    w, h, src = job["w"], job["h"], job["background"]
    if src is None:
        Image.new("RGB", (w, h), "white").save(out)
    else:
        with Image.open(src) as image:
            if image.size == (w, h):
                shutil.copyfile(src, out)
            else:
                image.convert("RGBA").resize((w, h), Image.Resampling.LANCZOS).save(out)
    return out


def _render(job: JSON) -> bytes:
    from app.engine.extract.html import render_reference, title_zone_of
    from app.engine.manifest import Manifest

    page = _page(job["w"], job["h"])
    background = _background(job)
    out = _scratch() / f"slide-{uuid.uuid4().hex}.png"
    scripts = (engine_config.CHART_PREVIEW_JS,) if engine_config.CHART_PREVIEW_JS.exists() else ()
    zone = None
    if job["master"].get("layouts"):
        try:
            zone = title_zone_of(Manifest.from_json(job["master"]), job["layout_id"])
        except Exception:  # noqa: BLE001 - no title pinning: the picture is still the slide
            zone = None
    try:
        render_reference(job["html"], background, out, assets_dir=job["assets"], fonts=job["fonts"],
                         title_zone=zone, scripts=scripts, page=page)
        return out.read_bytes()
    except Exception as exc:  # noqa: BLE001 - a crashed browser: forget the page, say why
        _drop_page()
        raise SlidePreviewError(f"the slide could not be rendered: {type(exc).__name__}: {exc}") from exc
    finally:
        out.unlink(missing_ok=True)
        background.unlink(missing_ok=True)


def render_slide_png(deck: DesignDeck, slide_id: str, *, version: int | None = None, max_width: int = 1280) -> bytes:
    """PNG bytes of `slide_id` as it will look; scaled down (never up) to at most `max_width` wide."""
    job = _job(deck, slide_id, version)
    png = run_on_preview(lambda: _render(job))
    if max_width and job["w"] > max_width:
        with Image.open(io.BytesIO(png)) as image:
            height = max(1, round(image.height * max_width / image.width))
            small = image.convert("RGB").resize((int(max_width), height), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            small.save(buf, "PNG")
            png = buf.getvalue()
    return png


def design_review(deck: DesignDeck, slide_id: str) -> list[JSON]:
    """The design lint's findings for the slide's current version (measured on a preview thread)."""
    from app.core.design_refs import design_lint

    meta = deck.meta()
    slide = deck.find_slide(meta, slide_id)
    html = deck.slide_path(slide_id, int(slide.get("current") or 1))
    workzone = normalize_bounds(meta.get("workzone"))
    return design_lint.design_findings_html(html, deck.master() or None, slide.get("layoutId"),
                                            assets_dir=deck.assets, layouts_dir=deck.layouts_dir,
                                            workzone=workzone, run=run_on_preview)
