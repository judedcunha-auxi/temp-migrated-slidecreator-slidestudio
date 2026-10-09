"""Workzone: the brand's clean content rectangle on the content layout.

Brand extraction returns a `workzone` beside `capturedFurniture`: the region of the content layout
free of header, logo and footer chrome. There is no compositing step here: the design turn lays the
slide's content **inside** the workzone on the branded content layout, and the master's furniture
supplies everything around it. The **header band** is the strip above the workzone, where the brand's
header and the title live; content must not paint over it.

Two places hold a slide to the workzone:

* the instruction (`design_note`), so the designer aims for it;
* the lint (`app/core/design_refs/workzone_lint.py`), which measures the saved slide and reports any
  element that leaves the workzone or covers the header band, so the turn fixes it and the pipeline
  counts what is left as review flags.

Semantics mirror Darwin's `netlify/functions/_shared/workzone.ts`. Ported from Slide Studio
`server/brand/workzone.py` (migration plan §4.1, K), plus the header band.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

SLIDE_ASPECT_16_9 = 16 / 9
_MAX_RATIO = 3.0  # the rendered aspect must sit within [1:3, 3:1]
_MIN_AREA_FRAC = 0.03  # zones under ~3% of the slide cannot hold real content


@dataclass(frozen=True)
class Workzone:
    """A content rectangle as fractions of the slide (origin top-left)."""

    left: float
    top: float
    width: float
    height: float

    def px_rect(self, canvas_w: int, canvas_h: int) -> tuple[int, int, int, int]:
        """(x, y, w, h) in canvas pixels."""
        return (
            round(self.left * canvas_w),
            round(self.top * canvas_h),
            round(self.width * canvas_w),
            round(self.height * canvas_h),
        )

    def header_band_px(self, canvas_w: int, canvas_h: int) -> tuple[int, int, int, int] | None:
        """The band above the workzone, full width: (0, 0, w, top). None when the workzone starts at
        the top edge (there is no band to protect)."""
        top = round(self.top * canvas_h)
        return (0, 0, canvas_w, top) if top > 0 else None

    def to_json(self) -> dict[str, float]:
        return {"left": self.left, "top": self.top, "width": self.width, "height": self.height}


def _finite(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)


def normalize_bounds(raw: Any) -> Workzone | None:
    """Accept `{left,top,right,bottom}` or `{left,top,width,height}`; return a Workzone or None.

    Mirrors `normalizeWorkzoneBounds`: idempotent for the w/h shape, converts right/bottom, and
    returns None for anything unusable so downstream math never sees an undefined size.
    """
    if not isinstance(raw, dict) or not _finite(raw.get("left")) or not _finite(raw.get("top")):
        return None
    left, top = float(raw["left"]), float(raw["top"])
    if _finite(raw.get("width")) and _finite(raw.get("height")):
        width, height = float(raw["width"]), float(raw["height"])
    elif _finite(raw.get("right")) and _finite(raw.get("bottom")):
        width, height = float(raw["right"]) - left, float(raw["bottom"]) - top
    else:
        return None
    if not (width > 0 and height > 0):
        return None
    return Workzone(left, top, width, height)


def is_degenerate(z: Workzone, slide_aspect: float = SLIDE_ASPECT_16_9) -> bool:
    """A workzone whose rendered aspect is outside [1:3, 3:1] or area < ~3% is not usable."""
    if not (z.width > 0 and z.height > 0):
        return True
    rendered_aspect = (z.width / z.height) * slide_aspect
    if rendered_aspect > _MAX_RATIO or rendered_aspect < 1 / _MAX_RATIO:
        return True
    return z.width * z.height < _MIN_AREA_FRAC


def should_tile(archetype: str, workzone: Workzone | None, furniture_archetypes: list[str] | None) -> bool:
    """Gate matching `shouldTileSlide`: content archetype + usable workzone + furniture present."""
    if archetype in ("cover", "divider"):
        return False
    if workzone is None or is_degenerate(workzone):
        return False
    return bool(furniture_archetypes and archetype in furniture_archetypes)


def design_note(z: Workzone, canvas_w: int, canvas_h: int) -> str:
    """The instruction fragment that confines a design to the workzone rectangle."""
    x, y, w, h = z.px_rect(canvas_w, canvas_h)
    band = z.header_band_px(canvas_w, canvas_h)
    header = (f" The band from the top edge down to {band[3]}px is the brand's header: only the title "
              f"(in its placeholder) may sit there, and no content may paint over it.") if band else ""
    return (
        f"WORKZONE CONSTRAINT: place ALL of your content inside the rectangle at left={x}px, "
        f"top={y}px, width={w}px, height={h}px (a {w}x{h}px region on the {canvas_w}x{canvas_h} "
        f"canvas). Nothing may fall outside it: the brand template's header band, logo and footer "
        f"occupy the rest of the slide and are drawn behind your HTML. Position every top-level "
        f"block within that box and do not redraw any chrome.{header}"
    )


def container_style(z: Workzone, canvas_w: int, canvas_h: int) -> str:
    """CSS for an absolutely positioned container pinned to the workzone (hand-authored slides)."""
    x, y, w, h = z.px_rect(canvas_w, canvas_h)
    return f"position:absolute;left:{x}px;top:{y}px;width:{w}px;height:{h}px"
