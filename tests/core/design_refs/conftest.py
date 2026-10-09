"""Fixtures for the design-reference tests: a synthetic master, and the measuring browser closed after.

The master is authored here (no client deck): one 1280x720 theme with a neutral palette, a "Title only"
layout whose title zone runs x 64-1216, y 28-90, and a layout background with footer furniture from
y 669 (a rule and a logo block) so the design review finds the footer band the way it does on a real
master. Slide Studio's tests used a client master with the same geometry; these keep the geometry and
drop the client.

The design review measures in the calling thread's browser (`app.core.browser_pool.thread_browser`);
the module-scoped fixture closes the measuring page and that browser when a test module ends, so no
Chromium outlives the tests.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

from app.core.browser_pool import close_thread_browser
from app.core.design_refs import design_lint

LAYOUT = "layout-01"  # "Title only": title zone x 64-1216, y 28-90; footer furniture from y 669

INK = "#2B2B33"
GREY = "#6F6F7A"
ACCENT = "#2F80ED"

THEME: dict[str, Any] = {
    "name": "Synthetic Widescreen",
    "colors": {"dk1": INK, "lt1": "#FFFFFF", "dk2": "#00A3A1", "lt2": "#F3F3F5", "accent1": ACCENT,
               "accent2": INK, "accent3": GREY, "accent4": "#C4C4CD", "accent5": "#8E44AD",
               "accent6": "#2DA44E", "hlink": ACCENT, "folHlink": "#800080"},
    "fonts": {"major": "Arial", "minor": "Arial"},
}

_TITLE_STYLE: dict[str, Any] = {
    "font": "+mn-lt", "sizePt": 22.0, "bold": False, "italic": False, "color": INK, "align": "left",
    "anchor": "top", "autofit": "none", "wrap": "square", "lineSpacingPct": 90.0,
    "insetsPx": {"l": 0.0, "t": 0.0, "r": 0.0, "b": 0.0}, "boxFrom": "layout",
}


def _placeholder(ptype: str, idx: int, name: str, x: float, y: float, w: float, h: float,
                 style: dict[str, Any]) -> dict[str, Any]:
    return {"type": ptype, "idx": idx, "name": name, "x": x, "y": y, "w": w, "h": h,
            "emu": {"x": round(x * 9525), "y": round(y * 9525), "w": round(w * 9525), "h": round(h * 9525)},
            "style": dict(style)}


MANIFEST: dict[str, Any] = {
    "version": 2, "importer": "deterministic", "sourceHash": "0" * 64,
    "slideWidthEmu": 12192000, "slideHeightEmu": 6858000, "canvas": {"w": 1280, "h": 720, "pxPerIn": 96},
    "masters": [{"index": 0, "name": "Synthetic", "partName": "/ppt/slideMasters/slideMaster1.xml",
                 "theme": copy.deepcopy(THEME)}],
    "theme": copy.deepcopy(THEME),
    "textStyles": {
        "title": {"levels": [{"align": "left", "font": "+mn-lt", "sizePt": 22.0, "color": INK, "level": 0}]},
        "body": {"levels": [{"align": "left", "font": "Arial", "sizePt": 11.0, "color": INK, "level": 0}]},
    },
    "bullets": [],
    "layouts": [
        {"id": "layout-01", "name": "Title only", "masterIndex": 0, "layoutIndex": 0,
         "partName": "/ppt/slideLayouts/slideLayout1.xml", "background": "layout-01.png", "usage": "title only",
         "placeholders": [_placeholder("title", 0, "Title 1", 64, 28, 1152, 62, _TITLE_STYLE)]},
        {"id": "layout-02", "name": "Blank", "masterIndex": 0, "layoutIndex": 1,
         "partName": "/ppt/slideLayouts/slideLayout2.xml", "background": "layout-02.png", "usage": "blank",
         "placeholders": []},
        {"id": "layout-03", "name": "Cover", "masterIndex": 0, "layoutIndex": 2,
         "partName": "/ppt/slideLayouts/slideLayout3.xml", "background": "layout-03.png", "usage": "title + subtitle",
         "placeholders": [
             _placeholder("ctrTitle", 0, "Title 1", 93, 283, 515, 174,
                          {**_TITLE_STYLE, "font": "+mj-lt", "sizePt": 42.0, "bold": True, "color": "#FFFFFF"}),
             _placeholder("subTitle", 1, "Subtitle 2", 93, 466, 515, 54,
                          {**_TITLE_STYLE, "font": "+mj-lt", "sizePt": 20.0, "bold": True, "color": "#FFFFFF"})]},
    ],
    "assets": ["asset-logo.png"],
}


def manifest() -> dict[str, Any]:
    """A fresh copy of the synthetic manifest (tests may change theirs)."""
    return copy.deepcopy(MANIFEST)


def _background(path: Path, *, dark: bool = False) -> None:
    """A layout background: white (or dark for the cover), with footer furniture from y 669."""
    image = Image.new("RGB", (1280, 720), "#1F3A5F" if dark else "white")
    draw = ImageDraw.Draw(image)
    if not dark:
        draw.rectangle((64, 669, 1216, 670), fill="#C4C4CD")      # the footer rule
        draw.rectangle((1150, 680, 1216, 706), fill="#1F3A5F")    # a logo block
        draw.rectangle((64, 682, 300, 690), fill="#6F6F7A")       # footer text
    image.save(path)


@pytest.fixture(scope="session")
def master_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """`layouts/` (backgrounds) and `assets/` (a logo) for the synthetic master."""
    root = tmp_path_factory.mktemp("synthetic-master")
    layouts = root / "layouts"
    layouts.mkdir()
    _background(layouts / "layout-01.png")
    _background(layouts / "layout-02.png")
    _background(layouts / "layout-03.png", dark=True)
    assets = root / "assets"
    assets.mkdir()
    Image.new("RGB", (160, 160), "#1F3A5F").save(assets / "asset-logo.png")
    return root


@pytest.fixture(scope="module", autouse=True)
def _close_browser() -> Iterator[None]:
    """Close the measuring page and this thread's browser when a module ends."""
    yield
    design_lint.close_measuring_page()
    close_thread_browser()
