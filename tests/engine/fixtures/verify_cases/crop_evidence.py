"""The image-crop probe: coordinate-coded sources in every CSS crop mode (tests only).

Every case draws a **coordinate-coded** source (red = x, green = y as linear ramps, blue = 128), so a
screenshot of a frame says which source pixel is shown at each screen pixel: the browser's own
geometry, not a re-implementation of CSS. `window()` finds the image's pixels in a picture, fits red
against x and green against y, and extrapolates to the edges of the visible rectangle: the window of
the source that is shown, in source px. Cases: `object-fit` and `object-position` in every syntax,
`background-size` / `-position` / `-origin` / `-clip` / `-repeat`, rounded and circular frames,
borders and shadows, tiles, a rotation, a flip, an SVG asset, SVG `<image>` with
`preserveAspectRatio`, and an image running off the canvas.

Ported from Slide Studio's fidelity tooling (its PowerPoint/PptxRender run harness is dropped) for
`tests/engine/extract/test_image_crops.py`. Synthetic: every picture is generated here. Needs numpy.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from app.config import engine as config

#: Coordinate-coded sources: `name -> (w, h)`.
SOURCES = {"L": ("coords-400x300.png", 400, 300), "P": ("coords-300x400.png", 300, 400),
           "S": ("coords-120x80.png", 120, 80), "T": ("coords-60x40.png", 60, 40),
           "V": ("bands-200x100.svg", 200, 100)}

#: An SVG asset (captured as the browser's own pixels, so scored by difference area only).
SVG_SOURCE = """<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" viewBox="0 0 200 100">
  <rect width="100" height="100" fill="#1A9AFA"/><rect x="100" width="100" height="100" fill="#F5A623"/>
  <path d="M0 100 L200 0" stroke="#2E2E38" stroke-width="6"/>
</svg>
"""

FRAME_W, FRAME_H = 180, 140
COLUMNS = (40, 245, 450, 655, 860, 1065)
ROWS = (30, 200, 370, 540)

#: `(name, source, element, frame css, element css)`. `element` is `img` (inside a frame div), `bg`
#: (the frame's own background) or `bleed` (an absolutely positioned img in an overflow-hidden frame).
IMG_CASES: list[tuple[str, str, str, str, str]] = [
    ("cover-L", "L", "img", "", "object-fit:cover"),
    ("cover-P", "P", "img", "", "object-fit:cover"),
    ("cover-L-left-top", "L", "img", "", "object-fit:cover;object-position:left top"),
    ("cover-P-right-bottom", "P", "img", "", "object-fit:cover;object-position:right bottom"),
    ("cover-P-50-20", "P", "img", "", "object-fit:cover;object-position:50% 20%"),
    ("cover-P-lengths", "P", "img", "", "object-fit:cover;object-position:0 -10px"),
    ("cover-P-offsets", "P", "img", "", "object-fit:cover;object-position:left 10px bottom 20px"),
    ("cover-L-calc", "L", "img", "", "object-fit:cover;object-position:calc(100% - 5px) 50%"),
    ("cover-L-120pct", "L", "img", "", "object-fit:cover;object-position:120% 50%"),
    ("contain-L", "L", "img", "", "object-fit:contain"),
    ("contain-P-left-top", "P", "img", "", "object-fit:contain;object-position:left top"),
    ("contain-P-offsets", "P", "img", "", "object-fit:contain;object-position:right 10px bottom 0"),
    ("contain-L-30-80", "L", "img", "", "object-fit:contain;object-position:30% 80%"),
    ("none-L", "L", "img", "", "object-fit:none"),
    ("none-L-lengths", "L", "img", "", "object-fit:none;object-position:-20px -30px"),
    ("none-S", "S", "img", "", "object-fit:none"),
    ("none-S-offsets", "S", "img", "", "object-fit:none;object-position:right 5px bottom 5px"),
    ("scale-down-L", "L", "img", "", "object-fit:scale-down"),
    ("scale-down-S", "S", "img", "", "object-fit:scale-down;object-position:left bottom"),
    ("fill-P", "P", "img", "", "object-fit:fill"),
    ("cover-L-padded", "L", "img", "",
     "object-fit:cover;padding:10px;border:4px solid #2E2E38;box-sizing:border-box;background:#fff"),
    ("cover-P-radius", "P", "img", "", "object-fit:cover;border-radius:24px"),
    ("cover-P-circle", "P", "img", "width:140px", "object-fit:cover;border-radius:50%"),
    ("bleed-P", "P", "bleed", "overflow:hidden",
     "position:absolute;left:-40px;top:-30px;width:300px;height:225px;object-fit:cover;"
     "object-position:right bottom"),
]

BG_CASES: list[tuple[str, str, str, str, str]] = [
    ("bg-cover-P-right-bottom", "P", "bg", "", "background-size:cover;background-position:right bottom"),
    ("bg-cover-L-25-75", "L", "bg", "", "background-size:cover;background-position:25% 75%"),
    ("bg-cover-P-offsets", "P", "bg", "", "background-size:cover;background-position:left 10px bottom 20px"),
    ("bg-cover-P-lengths", "P", "bg", "", "background-size:cover;background-position:0 -30px"),
    ("bg-cover-L-calc", "L", "bg", "",
     "background-size:cover;background-position:calc(100% - 10px) calc(50% + 10px)"),
    ("bg-contain-L-right", "L", "bg", "", "background-size:contain;background-position:right center"),
    ("bg-contain-P-offsets", "P", "bg", "", "background-size:contain;background-position:left 10px top"),
    ("bg-100pct", "P", "bg", "", "background-size:100% 100%"),
    ("bg-120px-auto", "L", "bg", "", "background-size:120px auto;background-position:right 10px bottom 10px"),
    ("bg-auto-200px", "P", "bg", "", "background-size:auto 200px;background-position:center"),
    ("bg-natural-negative", "L", "bg", "", "background-position:-50px -40px"),
    ("bg-50pct", "L", "bg", "", "background-size:50% 50%;background-position:center"),
    ("bg-origin-content", "P", "bg", "",
     "background-size:cover;padding:20px;box-sizing:border-box;background-origin:content-box;"
     "background-position:left top"),
    ("bg-clip-padding", "L", "bg", "",
     "background-size:cover;border:10px solid #2E2E38;box-sizing:border-box;background-clip:padding-box;"
     "background-position:left top"),
    ("bg-clip-content", "L", "bg", "",
     "background-size:cover;padding:15px;box-sizing:border-box;background-clip:content-box"),
    ("bg-radius", "P", "bg", "", "background-size:cover;border-radius:24px"),
    ("tile-contain-P", "P", "bg", "repeat", "background-size:contain;background-position:center"),
    ("tile-repeat-x", "S", "bg", "repeat", "background-repeat:repeat-x;background-position:left top"),
    ("tile-T", "T", "bg", "repeat", ""),
    ("tile-round", "T", "bg", "repeat", "background-repeat:round"),
    ("tile-space", "T", "bg", "repeat", "background-repeat:space"),
    ("tile-offset", "T", "bg", "repeat", "background-position:-10px 15px"),
]

#: Frames with rounded corners around a letterboxed source (the frame keeps its box; a negative crop is
#: the empty band), an `<img>`'s own border and shadow, tiles the export takes as one captured picture,
#: transforms, an SVG asset, an image running off the canvas and SVG `<image>`s aligned by
#: `preserveAspectRatio`.
EDGE_CASES: list[tuple[str, str, str, str, str]] = [
    ("contain-L-radius", "L", "img", "", "object-fit:contain;border-radius:24px"),
    ("contain-P-circle", "P", "img", "width:140px", "object-fit:contain;border-radius:50%"),
    ("none-S-radius", "S", "img", "", "object-fit:none;border-radius:30px"),
    ("bg-contain-radius", "P", "bg", "", "background-size:contain;background-position:center;border-radius:24px"),
    ("bg-small-circle", "S", "bg", "width:140px", "background-position:center;border-radius:50%"),
    ("img-bordered", "L", "img", "",
     "object-fit:cover;border:4px solid #2E2E38;border-radius:12px;box-sizing:border-box;"
     "box-shadow:0 4px 10px rgba(0,0,0,.35)"),
    ("tile-radius", "T", "bg", "repeat", "border-radius:20px"),
    ("tile-many", "T", "bg", "repeat", "background-size:6px 4px"),
    ("rot-cover", "P", "img", "", "object-fit:cover;object-position:right bottom;transform:rotate(8deg)"),
    ("flip-cover", "L", "img", "", "object-fit:cover;object-position:left top;transform:scaleX(-1)"),
    ("svg-contain-radius", "V", "img", "", "object-fit:contain;border-radius:16px"),
    ("bleed-canvas", "P", "bleed", "",
     "position:absolute;left:0;top:0;width:300px;height:140px;object-fit:cover;object-position:0 70%"),
    ("svgimg-slice-xMinYMax", "P", "svgimg", "", "xMinYMax slice"),
    ("svgimg-meet-xMaxYMid", "P", "svgimg", "", "xMaxYMid meet"),
    ("svgimg-meet-xMinYMin", "L", "svgimg", "", "xMinYMin meet"),
    ("svgimg-slice-xMidYMin", "L", "svgimg", "", "xMidYMin slice"),
    ("tile-dust", "T", "bg", "repeat", "background-size:1px 1px"),
    ("under-transparent", "P", "bg", "", "background-size:cover;border:10px solid transparent;box-sizing:border-box"),
    ("under-dashed", "P", "bg", "", "background-size:cover;border:6px dashed #2E2E38;box-sizing:border-box"),
]

PAGES = (("crops-a", IMG_CASES), ("crops-b", BG_CASES), ("crops-c", EDGE_CASES))

#: Cases with no single window to fit (tiles repeat the ramp; a rotated frame is not a rectangle; an SVG
#: is not coordinate-coded): scored by the masked difference area only.
DIFF_ONLY = ("tile-", "rot-", "svg-", "under-dashed")


# --------------------------------------------------------------------------------------------- build


def write_sources(assets: Path) -> None:
    """Red = x, green = y (full-range linear ramps over the source), blue = 128; PNG, so lossless."""
    assets.mkdir(parents=True, exist_ok=True)
    (assets / SOURCES["V"][0]).write_text(SVG_SOURCE, encoding="utf-8")
    for name, w, h in SOURCES.values():
        if name.endswith(".svg"):
            continue
        xs = np.round(np.arange(w) * 255.0 / (w - 1)).astype(np.uint8)
        ys = np.round(np.arange(h) * 255.0 / (h - 1)).astype(np.uint8)
        pixels = np.zeros((h, w, 3), dtype=np.uint8)
        pixels[:, :, 0] = xs[None, :]
        pixels[:, :, 1] = ys[:, None]
        pixels[:, :, 2] = 128
        Image.fromarray(pixels, "RGB").save(assets / name, format="PNG")


def slide_html(cases: list[tuple[str, str, str, str, str]], assets: Path) -> str:
    cells = []
    for index, (name, source, element, frame_css, element_css) in enumerate(cases):
        x, y = COLUMNS[index % len(COLUMNS)], ROWS[index // len(COLUMNS)]
        url = f"/api/projects/prj_crops/assets/{SOURCES[source][0]}"
        repeat = "" if frame_css == "repeat" else "background-repeat:no-repeat;"
        frame = "" if frame_css == "repeat" else frame_css
        if element == "bg":
            cells.append(f'<div class="f" data-name="{name}" style="left:{x}px;top:{y}px;{frame};'
                         f'background-image:url({url});{repeat}{element_css}"></div>')
        elif element == "svgimg":
            data = base64.b64encode((assets / SOURCES[source][0]).read_bytes()).decode("ascii")
            cells.append(f'<svg data-name="{name}-svg" style="position:absolute;left:{x}px;top:{y}px" '
                         f'width="{FRAME_W}" height="{FRAME_H}"><image data-name="{name}" '
                         f'href="data:image/png;base64,{data}" width="{FRAME_W}" height="{FRAME_H}" '
                         f'preserveAspectRatio="{element_css}"/></svg>')
        else:
            inner = "" if element == "bleed" else "width:100%;height:100%;display:block;"
            cells.append(f'<div class="f" data-name="{name}-frame" style="left:{x}px;top:{y}px;{frame}">'
                         f'<img data-name="{name}" src="{url}" alt="" style="{inner}{element_css}"></div>')
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\"><title>crops</title>\n<style>\n"
            "html,body{margin:0;width:1280px;height:720px;overflow:hidden;background:transparent}\n"
            f".f{{position:absolute;width:{FRAME_W}px;height:{FRAME_H}px}}\n"
            "</style></head><body>\n" + "\n".join(cells) + "\n</body></html>\n")


def build(root: Path) -> list[Path]:
    """Write the sources and the three probe slides (with an expect.json naming `test-16x9`) under `root`."""
    root.mkdir(parents=True, exist_ok=True)
    write_sources(root / "assets")
    pages = []
    for stem, cases in PAGES:
        html = root / f"{stem}.html"
        html.write_text(slide_html(cases, root / "assets"), encoding="utf-8")
        (root / f"{stem}.expect.json").write_text(
            json.dumps({"master": "test-16x9", "layoutId": "layout-07"}), encoding="utf-8")
        pages.append(html)
    return pages


# ------------------------------------------------------------------------------------------ measure


def cell_of(index: int) -> tuple[int, int, int, int]:
    """The search region of a case: its frame plus a margin (a bleed or a tile may sit outside)."""
    x, y = COLUMNS[index % len(COLUMNS)], ROWS[index // len(COLUMNS)]
    return x - 12, y - 12, x + FRAME_W + 12, y + FRAME_H + 12


def image_mask(rgb: np.ndarray) -> np.ndarray:
    """Pixels of a coordinate-coded source: blue ≈ 128 (white paper and dark borders are not)."""
    blue = rgb[:, :, 2].astype(np.int32)
    return np.abs(blue - 128) <= 6


def window(png: Path, index: int, source: str) -> dict[str, Any] | None:
    """The visible rectangle (screen px) and the source window it shows (source px) for one case."""
    x0, y0, x1, y1 = cell_of(index)
    rgb = np.asarray(Image.open(png).convert("RGB"))[y0:y1, x0:x1]
    mask = image_mask(rgb)
    if mask.sum() < 20:
        return None
    rows = np.where(mask.mean(axis=1) >= 0.5 * mask.mean(axis=1).max())[0]
    cols = np.where(mask.mean(axis=0) >= 0.5 * mask.mean(axis=0).max())[0]
    top, bottom, left, right = rows.min(), rows.max() + 1, cols.min(), cols.max() + 1
    _, w, h = SOURCES[source]
    inner = mask[top + 2:bottom - 2, left + 2:right - 2]
    red = rgb[top + 2:bottom - 2, left + 2:right - 2, 0].astype(np.float64)
    green = rgb[top + 2:bottom - 2, left + 2:right - 2, 1].astype(np.float64)
    if inner.size == 0 or inner.shape[0] < 3 or inner.shape[1] < 3:
        return None
    # Column medians of red against the pixel centre, row medians of green: robust to the rounded corners.
    xs = np.arange(left + 2, right - 2) + 0.5
    ys = np.arange(top + 2, bottom - 2) + 0.5
    red_cols = np.array([np.median(red[:, i][inner[:, i]]) if inner[:, i].any() else np.nan
                         for i in range(inner.shape[1])])
    green_rows = np.array([np.median(green[j, :][inner[j, :]]) if inner[j, :].any() else np.nan
                           for j in range(inner.shape[0])])
    ok_x, ok_y = ~np.isnan(red_cols), ~np.isnan(green_rows)
    ax, bx = np.polyfit(xs[ok_x], red_cols[ok_x], 1)
    ay, by = np.polyfit(ys[ok_y], green_rows[ok_y], 1)

    def source_x(u: float) -> float:
        return (ax * u + bx) * (w - 1) / 255.0 + 0.5

    def source_y(v: float) -> float:
        return (ay * v + by) * (h - 1) / 255.0 + 0.5

    kx, ky = ax * (w - 1) / 255.0, ay * (h - 1) / 255.0
    return {
        "rect": [float(x0 + left), float(y0 + top), float(right - left), float(bottom - top)],
        "window": [source_x(left), source_y(top), source_x(right), source_y(bottom)],
        "scale": [abs(kx), abs(ky)],
        # source px as an affine function of page px: x_src = map[0]·x + map[1], y_src = map[2]·y + map[3]
        "map": [kx, source_x(0) - kx * x0, ky, source_y(0) - ky * y0],
    }


def ink_difference(a: Path, b: Path, index: int) -> int:
    """Pixels of one case's region that differ by more than 24 levels in any channel."""
    x0, y0, x1, y1 = cell_of(index)
    first = np.asarray(Image.open(a).convert("RGB"))[y0:y1, x0:x1].astype(np.int32)
    second = np.asarray(Image.open(b).convert("RGB"))[y0:y1, x0:x1].astype(np.int32)
    return int((np.abs(first - second).max(axis=2) > 24).sum())


def file_pictures(deck: Path) -> list[dict[str, dict[str, Any]]]:
    """Per slide, per picture name: the frame (px) and the srcRect fractions the file carries."""
    from pptx import Presentation

    slides = []
    for slide in Presentation(str(deck)).slides:
        pictures: dict[str, dict[str, Any]] = {}
        for shape in slide.shapes:
            if shape.shape_type != 13:
                continue
            entry = {"frame": [round(v / config.EMU_PER_PX, 3) for v in (shape.left, shape.top, shape.width,
                                                                          shape.height)],
                     "srcRect": [round(v, 5) for v in (shape.crop_left, shape.crop_top, shape.crop_right,
                                                       shape.crop_bottom)]}
            key = shape.name if shape.name not in pictures else f"{shape.name}#{len(pictures)}"
            pictures[key] = entry
        slides.append(pictures)
    return slides
