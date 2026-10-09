"""`<svg>` → IR elements: browser geometry in, exact curves out.

**WP2.** `expand_svg` runs inside WP1's page session. `svg.js` measures every element of one `<svg>`
root with `getScreenCTM()` and `getComputedStyle()` — so viewBox, `preserveAspectRatio`, nested
`g transform`, CSS classes, inheritance through `<g>` and pt units are all resolved by Chromium,
which is the only implementation of those rules we trust — and this module turns that description
into IR elements. Only two things are computed here rather than read from the browser:

* **Curves.** `svgelements` parses `d`, converts arcs to cubics and applies the CTM, so a path
  arrives as exact `M L C Q Z` in canvas px. Straight-segment paths (client chevrons) stay exact
  to the last decimal; nothing is flattened except the deliberate sampling the preset detector does.
* **Presets.** A path that *is* a rectangle, a circle, a ring segment or a wedge becomes the
  matching PowerPoint preset instead of a custom geometry, because a preset is editable in
  PowerPoint and a 200-node `custGeom` is not. Detection is numeric (a circle fit with an RMS
  budget), never a guess from the `d` string's shape.

Everything the emitter cannot express natively — `filter` (other than a lone `feDropShadow`),
`mask`, `pattern` paint, a non-rectangular `clipPath`, `textPath`, `foreignObject`, an unknown
marker, a blend mode — becomes a **raster of just that element**, captured in isolation (everything
else on the page hidden), with a reason and a diagnostic. A plain crop would bake the neighbours in
and the emitter would then paint them twice (`docs/archive/engine/90-CRITIQUE.md`, majors).

Derived images (rasters, SVG `image` data URIs) go to the caller's `derived` folder, not
to `assets_dir` — `assets_dir` is an input (`04-IR-FREEZE.md` decision 4). `assets_written` reports
every file this expansion wrote.

**One call WP1 has to make.** `IR.validate()` requires every `raster` element to be named by a
diagnostic, but an expansion's elements have `id=None` until WP1 renumbers the spliced list. So a
diagnostic that points at an element here says `x-wp2:<key>`, the element carries the same key in
`extras["x-wp2-key"]`, and **`retarget_diagnostics(ir.elements, ir.diagnostics)` after
`assign_ids_and_z` rewrites them to the real ids**. Without that call a slide whose SVG contains a
raster-only construct fails validation. Nothing changes for a slide with no such construct — both
client reference slides expand with zero rasters.
"""
from __future__ import annotations

import base64
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from playwright.sync_api import Page

from app.config import engine as config
from app.engine.ir import Box, Canvas, Diagnostic, Element, Group, SvgExpansion

#: Injected for the duration of an element screenshot: hide everything, then re-show the target's
#: ancestors as transparent frames and the target subtree as-is.
ISOLATION_CSS = """
body * { visibility: hidden !important; }
[data-engine-capture], [data-engine-capture] * { visibility: visible !important; }
[data-engine-capture-ancestor] { background: transparent !important; border-color: transparent !important;
                                 box-shadow: none !important; }
defs, defs *, clipPath, clipPath *, mask, mask *, marker, marker *, pattern, pattern *,
symbol, symbol *, filter, filter * { visibility: visible !important; }
svg[data-engine-capture-ancestor], g[data-engine-capture-ancestor],
a[data-engine-capture-ancestor], switch[data-engine-capture-ancestor] { visibility: visible !important; }
"""
"""Hide the page, then show the target subtree.

The capture-ancestor rule is for `<foreignObject>` (WP-F, F-B): Chromium does not paint a
foreignObject's XHTML content under a `visibility: hidden` SVG root even when that content is
`visible !important` — p07's capture had 0 inked pixels, 3,506 with the root visible — unless a
sibling happens to use `filter` or `mask`, which is why `svg-shapes`' foreignObject still had ink.
Making the target's SVG *container* ancestors visible cannot leak anything: a container paints
nothing of its own (a bare text node under `<g>` never renders, and `<text>` is an element that
stays hidden as a non-ancestor), the root's CSS background is already stripped by the ancestor
rule above, and visibility does not touch the root's overflow clip.

The `defs, clipPath, …` rule is not a convenience. A `<pattern>` tile, a `<mask>`'s luminance shape, a
`<clipPath>`'s outline and a `<marker>`'s arrowhead are all ordinary elements that `visibility:
hidden` switches off — and the thing they define is exactly why the element is being rastered, so
hiding them produced a blank picture (a pattern-filled rect captured as nothing at all, a clipped
shape clipped to emptiness). None of these tags ever paints where it sits, so making them visible
cannot leak anything else into the capture. The names are the SVG spellings on purpose: a CSS type
selector against an SVG element is case-sensitive, and `clippath` matches nothing.
"""

#: The measurement pass that runs in the page. Read once per process; re-evaluated per page because
#: a page may have navigated since (the IIFE is idempotent).
_SVG_JS = (Path(__file__).with_suffix(".js")).read_text(encoding="utf-8")

#: Attribute `svg.js` leaves on every described element, so an isolated capture can select one.
NODE_ATTR = "data-engine-svg-node"

#: `extras` key holding an element's provisional identity, and the prefix a diagnostic uses to
#: point at it. An expansion's elements have `id=None` until WP1 renumbers the spliced list, but
#: `IR.validate()` requires every `raster` to have a diagnostic naming its id — so a diagnostic
#: here says `x-wp2:<key>` and `retarget_diagnostics` rewrites it once the real ids exist.
ELEMENT_KEY = "x-wp2-key"
DIAG_KEY_PREFIX = "x-wp2:"

# How far a measured radius may wander from the fitted circle before a path stops being a circle,
# a ring segment or a wedge. 0.5 px is the brief's budget: below a rendered pixel, above the noise
# a cubic approximation of an arc leaves behind.
RADIUS_RMS_PX = 0.5
#: Samples taken along a path when testing it against the circle/arc presets.
ARC_SAMPLES = 180
#: Below this the geometry is degenerate and the element is dropped with a diagnostic.
MIN_EXTENT_PX = 1e-4
#: `text-transform` as the IR spells it. `capitalize` has no DrawingML equivalent, so it is left
#: unset and reported — the browser has already drawn the capitalised glyphs the box was measured on.
_TRANSFORM_CASE: dict[str, str | None] = {
    "none": None, "uppercase": "upper", "lowercase": "lower", "capitalize": None,
}

#: How far outside a clip an element may reach before the clip is recorded. A glyph's side bearing
#: puts the client rect a pixel outside the advance box; that is not a clip, and reporting it as
#: one would raise an `error` diagnostic on text nobody clipped.
CLIP_TOLERANCE_PX = 1.5


# ------------------------------------------------------------------------------- isolated capture


def capture_isolated(page: Page, selector: str, out_png: Path, box: Box | None = None) -> Path:
    """Screenshot one element with every other element hidden and the page background transparent.

    With no `box`, Playwright frames the element's own bounding rect. Pass a `box` when that rect
    is not what has to be in the picture: an SVG geometry rect excludes the stroke, is *zero-high*
    for a horizontal `<line>` (which Playwright then refuses to screenshot at all), and never
    includes a marker or a blur halo. The clip is in canvas px, the same frame as the IR box.
    """
    out_png.parent.mkdir(parents=True, exist_ok=True)
    handle = page.query_selector(selector)
    if handle is None:
        raise ValueError(f"nothing matches {selector!r} on this page")
    page.evaluate(
        """(sel) => {
            document.querySelectorAll('[data-engine-capture], [data-engine-capture-ancestor]')
                .forEach(el => { el.removeAttribute('data-engine-capture');
                                 el.removeAttribute('data-engine-capture-ancestor'); });
            const target = document.querySelector(sel);
            if (!target) return;
            target.setAttribute('data-engine-capture', '');
            for (let el = target.parentElement; el; el = el.parentElement)
                el.setAttribute('data-engine-capture-ancestor', '');
        }""",
        selector,
    )
    if box is None:
        handle.screenshot(path=str(out_png), style=ISOLATION_CSS, omit_background=True)
    else:
        page.screenshot(
            path=str(out_png),
            style=ISOLATION_CSS,
            omit_background=True,
            clip={"x": box.x, "y": box.y, "width": box.w, "height": box.h},
        )
    return out_png


# --------------------------------------------------------------------------------- matrix helpers
# A CTM is the browser's [a, b, c, d, e, f]: x' = a·x + c·y + e, y' = b·x + d·y + f. svgelements'
# Matrix takes the same six numbers in the same order, so a path never needs a bespoke transform.

Matrix6 = tuple[float, float, float, float, float, float]


def _mat(values: Sequence[float]) -> Matrix6:
    a, b, c, d, e, f = (float(v) for v in values)
    return (a, b, c, d, e, f)


def _apply(m: Matrix6, x: float, y: float) -> tuple[float, float]:
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def _mean_scale(m: Matrix6) -> float:
    """√|det| — the isotropic scale a stroke width and a dash pattern are multiplied by."""
    return math.sqrt(abs(m[0] * m[3] - m[1] * m[2])) or 1.0


@dataclass(frozen=True, slots=True)
class _Decomposed:
    """A CTM split into the parts DrawingML can express: rotate, scale, flip — and leftover skew."""

    sx: float
    sy: float
    rotation: float
    skew: float
    flip_v: bool

    @property
    def skewed(self) -> bool:
        return abs(self.skew) > 1e-3


def _decompose(m: Matrix6) -> _Decomposed:
    """QR-style split: rotate ∘ scale ∘ shear, with a negative determinant read as a flip."""
    a, b, c, d = m[0], m[1], m[2], m[3]
    sx = math.hypot(a, b)
    if sx < 1e-12:
        return _Decomposed(1.0, 1.0, 0.0, 0.0, False)
    rotation = math.degrees(math.atan2(b, a))
    shear = (a * c + b * d) / (sx * sx)
    sy = math.hypot(c - shear * a, d - shear * b)
    return _Decomposed(sx, sy, rotation, shear, a * d - b * c < 0)


def _mapped_box(local: Sequence[float], m: Matrix6) -> tuple[Box, _Decomposed]:
    """Canvas box (before rotation) of an axis-aligned local rectangle under `m`."""
    x, y, w, h = (float(v) for v in local)
    parts = _decompose(m)
    cx, cy = _apply(m, x + w / 2, y + h / 2)
    width, height = abs(w * parts.sx), abs(h * parts.sy)
    return Box(cx - width / 2, cy - height / 2, width, height), parts


def _box_of_points(points: Iterable[tuple[float, float]]) -> Box:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return Box(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def _norm_deg(value: float) -> float:
    return value % 360.0


# ---------------------------------------------------------------------------------- colour / paint

_NUMBER = re.compile(r"-?\d*\.?\d+(?:e-?\d+)?", re.IGNORECASE)


def _colour(value: str | None) -> tuple[str, float] | None:
    """`rgb(46, 46, 56)` / `rgba(…)` / `#2e2e38` → `("2E2E38", alpha)`; `None` when not a colour."""
    if not value:
        return None
    text = value.strip()
    if text in ("none", "transparent"):
        return None
    if text.startswith("#"):
        digits = text[1:]
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        if len(digits) == 6:
            return digits.upper(), 1.0
        if len(digits) == 8:
            return digits[:6].upper(), int(digits[6:], 16) / 255
        return None
    if text.startswith(("rgb", "color(")):
        numbers = [float(n) for n in _NUMBER.findall(text)]
        # `color(srgb 0 0 0 / .5)` gives 0–1 channels; rgb()/rgba() give 0–255.
        if text.startswith("color("):
            channels = numbers[:3]
            alpha = numbers[3] if len(numbers) > 3 else 1.0
            rgb = [max(0, min(255, round(c * 255))) for c in channels]
        else:
            rgb = [max(0, min(255, round(c))) for c in numbers[:3]]
            alpha = numbers[3] if len(numbers) > 3 else 1.0
        if len(rgb) != 3:
            return None
        return "".join(f"{c:02X}" for c in rgb), max(0.0, min(1.0, alpha))
    return None


def _dash_list(value: str | None) -> list[float]:
    """`"3px, 2px"` → `[3.0, 2.0]`; `none` → `[]`. Values are local user units.

    An odd-length list is doubled, which is what SVG does with it (`stroke-dasharray: 5` draws
    5 on, 5 off) — so the ring-segment rule below can always read one on/off pair.
    """
    if not value or value.strip() in ("none", "0px", "0"):
        return []
    numbers = [float(n) for n in _NUMBER.findall(value)]
    if not any(n > 0 for n in numbers):
        return []
    return numbers * 2 if len(numbers) % 2 else numbers


def _first_family(font_family: str | None) -> str:
    if not font_family:
        return "Arial"
    return font_family.split(",")[0].strip().strip("'\"") or "Arial"


# ------------------------------------------------------------------------------- path <-> IR path


def _path_commands(path: Any) -> list[list[Any]]:
    """An `svgelements.Path` (already transformed and reified) → IR `M L C Q Z` segments."""
    from svgelements import Close, CubicBezier, Line, Move, QuadraticBezier

    out: list[list[Any]] = []
    for segment in path:
        if isinstance(segment, Move):
            out.append(["M", float(segment.end.x), float(segment.end.y)])
        elif isinstance(segment, Line):
            out.append(["L", float(segment.end.x), float(segment.end.y)])
        elif isinstance(segment, CubicBezier):
            out.append([
                "C",
                float(segment.control1.x), float(segment.control1.y),
                float(segment.control2.x), float(segment.control2.y),
                float(segment.end.x), float(segment.end.y),
            ])
        elif isinstance(segment, QuadraticBezier):
            out.append([
                "Q", float(segment.control.x), float(segment.control.y),
                float(segment.end.x), float(segment.end.y),
            ])
        elif isinstance(segment, Close):
            out.append(["Z"])
    return out


def _fit_circle(points: Sequence[tuple[float, float]]) -> tuple[float, float, float, float]:
    """Kåsa algebraic circle fit → `(cx, cy, r, rms)`. `rms` is infinite when it cannot fit."""
    n = len(points)
    if n < 3:
        return 0.0, 0.0, 0.0, math.inf
    sum_x = sum(p[0] for p in points)
    sum_y = sum(p[1] for p in points)
    mean_x, mean_y = sum_x / n, sum_y / n
    us = [p[0] - mean_x for p in points]
    vs = [p[1] - mean_y for p in points]
    suu = sum(u * u for u in us)
    svv = sum(v * v for v in vs)
    suv = sum(u * v for u, v in zip(us, vs, strict=False))
    suuu = sum(u ** 3 for u in us)
    svvv = sum(v ** 3 for v in vs)
    suvv = sum(u * v * v for u, v in zip(us, vs, strict=False))
    svuu = sum(v * u * u for u, v in zip(us, vs, strict=False))
    determinant = 2 * (suu * svv - suv * suv)
    if abs(determinant) < 1e-9:
        return 0.0, 0.0, 0.0, math.inf
    uc = (svv * (suuu + suvv) - suv * (svvv + svuu)) / determinant
    vc = (suu * (svvv + svuu) - suv * (suuu + suvv)) / determinant
    cx, cy = uc + mean_x, vc + mean_y
    radii = [math.hypot(p[0] - cx, p[1] - cy) for p in points]
    radius = sum(radii) / n
    rms = math.sqrt(sum((r - radius) ** 2 for r in radii) / n)
    return cx, cy, radius, rms


def _straight_points(commands: Sequence[Sequence[Any]]) -> list[tuple[float, float]] | None:
    """The corner list of a single *closed* straight-segment subpath, else `None`."""
    points: list[tuple[float, float]] = []
    moves = 0
    closed = False
    for command in commands:
        head = command[0]
        if head == "M":
            moves += 1
            if moves > 1:
                return None
            points.append((float(command[1]), float(command[2])))
        elif head == "L":
            points.append((float(command[1]), float(command[2])))
        elif head == "Z":
            closed = True
        else:
            return None
    if not closed:
        return None
    if len(points) > 1 and math.isclose(points[0][0], points[-1][0], abs_tol=1e-6) \
            and math.isclose(points[0][1], points[-1][1], abs_tol=1e-6):
        points.pop()
    return points


def _axis_aligned_rect(points: Sequence[tuple[float, float]]) -> Box | None:
    """A 4-corner axis-aligned rectangle → its box; anything else → `None`."""
    if len(points) != 4:
        return None
    xs = sorted({round(p[0], 3) for p in points})
    ys = sorted({round(p[1], 3) for p in points})
    if len(xs) != 2 or len(ys) != 2:
        return None
    for index, point in enumerate(points):
        nxt = points[(index + 1) % 4]
        horizontal = math.isclose(point[1], nxt[1], abs_tol=1e-3)
        vertical = math.isclose(point[0], nxt[0], abs_tol=1e-3)
        if not (horizontal or vertical):
            return None
    return Box(xs[0], ys[0], xs[1] - xs[0], ys[1] - ys[0])


def _arc_angles(points: Sequence[tuple[float, float]], cx: float, cy: float) -> tuple[float, float]:
    """`(startDeg, endDeg)` clockwise from 3 o'clock for a sampled arc, following its direction."""
    def angle(point: tuple[float, float]) -> float:
        return math.degrees(math.atan2(point[1] - cy, point[0] - cx))

    first, middle, last = angle(points[0]), angle(points[len(points) // 2]), angle(points[-1])
    clockwise = _norm_deg(middle - first) + _norm_deg(last - middle) <= _norm_deg(last - first) + 1e-6
    return (_norm_deg(first), _norm_deg(last)) if clockwise else (_norm_deg(last), _norm_deg(first))


@dataclass(slots=True)
class _Preset:
    """What the path really is: a preset geometry plus the box it needs."""

    geometry: dict[str, Any]
    box: Box
    note: str


def _detect_preset(path: Any, commands: Sequence[Sequence[Any]]) -> _Preset | None:
    """A path that is really a rect / circle / ring segment / wedge → that preset, else `None`.

    The arcs are the evidence, not the `d` string: a ring segment or a wedge is drawn as runs of
    curve segments separated by the straight radial edges, so each run is fitted to a circle on its
    own and only the fit decides. Two runs on one centre at two radii are a ring segment; one run
    spanning the full turn is a circle; one run whose straight vertices include the centre is a
    wedge. Anything else keeps its exact curves — a wrong preset is worse than a faithful custGeom.
    """
    if not any(command[0] in ("C", "Q") for command in commands):
        straight = _straight_points(commands)
        if straight is None:
            return None
        box = _axis_aligned_rect(straight)
        if box is not None and box.w > MIN_EXTENT_PX and box.h > MIN_EXTENT_PX:
            return _Preset({"type": "rect"}, box, "4-point axis-aligned closed path")
        return None

    runs = _curve_runs(path)
    if not runs or len(runs) > 2:
        return None
    fits: list[tuple[float, float, float, list[tuple[float, float]]]] = []
    for run in runs:
        samples = _run_samples(run)
        cx, cy, radius, rms = _fit_circle(samples)
        if radius <= MIN_EXTENT_PX or rms >= RADIUS_RMS_PX:
            return None
        fits.append((cx, cy, radius, samples))

    if len(fits) == 2:
        (x1, y1, r1, p1), (x2, y2, r2, p2) = fits
        if math.hypot(x1 - x2, y1 - y2) >= RADIUS_RMS_PX:
            return None
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        if abs(r1 - r2) < RADIUS_RMS_PX:
            radius = (r1 + r2) / 2
            return _Preset({"type": "ellipse"}, Box(cx - radius, cy - radius, 2 * radius, 2 * radius),
                           "two arcs on one circle")
        r_outer, r_inner = max(r1, r2), min(r1, r2)
        outer = p1 if r1 > r2 else p2
        if _angular_span(outer, cx, cy) >= 359.0:
            # A closed annulus, not a ring *segment*: a blockArc would sweep 0°. Two subpaths with
            # `fill-rule: evenodd` already say it exactly, so keep the curves.
            return None
        start, end = _arc_angles(outer, cx, cy)
        return _Preset(
            {"type": "blockArc", "arc": {"cx": cx, "cy": cy, "rOuter": r_outer, "rInner": r_inner,
                                         "startDeg": start, "endDeg": end}},
            Box(cx - r_outer, cy - r_outer, 2 * r_outer, 2 * r_outer),
            f"ring segment rOuter {r_outer:.2f} rInner {r_inner:.2f}",
        )

    cx, cy, radius, samples = fits[0]
    box = Box(cx - radius, cy - radius, 2 * radius, 2 * radius)
    span = _angular_span(samples, cx, cy)
    if span >= 359.0:
        return _Preset({"type": "ellipse"}, box, f"one arc run spanning {span:.1f}°")
    if any(math.hypot(x - cx, y - cy) < RADIUS_RMS_PX for x, y in _straight_vertices(commands)):
        start, end = _arc_angles(samples, cx, cy)
        return _Preset(
            {"type": "pie", "arc": {"cx": cx, "cy": cy, "rOuter": radius, "rInner": 0.0,
                                    "startDeg": start, "endDeg": end}},
            box, f"wedge spanning {span:.1f}° with a vertex on the centre",
        )
    return None


def _curve_runs(path: Any) -> list[list[Any]]:
    """Runs of consecutive curve segments. A ring segment has two; a full circle has one."""
    from svgelements import CubicBezier, QuadraticBezier

    runs: list[list[Any]] = []
    current: list[Any] = []
    for segment in path:
        if isinstance(segment, (CubicBezier, QuadraticBezier)):
            current.append(segment)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _run_samples(run: Sequence[Any], total: int = ARC_SAMPLES) -> list[tuple[float, float]]:
    per_segment = max(4, total // len(run))
    points: list[tuple[float, float]] = []
    for segment in run:
        for index in range(per_segment + 1):
            point = segment.point(index / per_segment)
            points.append((float(point.x), float(point.y)))
    return points


def _straight_vertices(commands: Sequence[Sequence[Any]]) -> list[tuple[float, float]]:
    return [(float(c[1]), float(c[2])) for c in commands if c[0] in ("M", "L")]


def _angular_span(points: Sequence[tuple[float, float]], cx: float, cy: float) -> float:
    """Degrees swept along the samples, signed steps summed so a full turn reads as 360."""
    total = 0.0
    for first, second in zip(points, points[1:], strict=False):
        step = math.degrees(math.atan2(second[1] - cy, second[0] - cx)
                            - math.atan2(first[1] - cy, first[0] - cx))
        total += (step + 180.0) % 360.0 - 180.0
    return abs(total)


# ------------------------------------------------------------------------------------- the walker


@dataclass(slots=True)
class _Context:
    """What flows down the tree: accumulated opacity, clip, group, and the raster anchor."""

    opacity: float = 1.0
    clip: Box | None = None
    group: str | None = None
    raster_key: str | None = None


class _Expander:
    """Turns one `svg.js` description into IR elements, groups and diagnostics."""

    def __init__(self, page: Page, svg_id: str, canvas: Canvas, output: Path) -> None:
        self.page = page
        self.svg_id = svg_id
        self.canvas = canvas
        self.output = output
        self.elements: list[Element] = []
        self.groups: list[Group] = []
        self.diagnostics: list[Diagnostic] = []
        self.assets: list[Path] = []
        self._tag_counts: dict[str, int] = {}
        self._group_count = 0
        self._css_path = ""
        self._rasters: list[tuple[str, Element]] = []   # (selector, element) captured after the walk

    # -- entry -------------------------------------------------------------------------------
    def run(self, data: dict[str, Any]) -> SvgExpansion:
        self._css_path = data.get("cssPath") or f'[data-engine-svg="{self.svg_id}"]'
        for warning in data.get("warnings") or []:
            self._warn(warning.get("source", self.svg_id), warning.get("message", ""))
        scroll = data.get("scroll") or [0, 0]
        if abs(scroll[0]) > 0.5 or abs(scroll[1]) > 0.5:
            self._diag("error", self.svg_id,
                       f"the measuring page is scrolled to {scroll}; canvas coordinates would be shifted")
        viewport = Box(*[float(v) for v in data["viewport"]])
        clip = viewport if (data.get("overflow") or "hidden") != "visible" else None
        context = _Context(opacity=float(data.get("opacity", 1.0)), clip=clip)
        for node in data.get("nodes") or []:
            self._node(node, context)
        self._capture_rasters()
        return SvgExpansion(
            elements=self.elements,
            groups=self.groups,
            diagnostics=self.diagnostics,
            assets_written=self.assets,
        )

    # -- diagnostics -------------------------------------------------------------------------
    def _diag(self, level: str, source: str, message: str, element_id: str | None = None) -> None:
        self.diagnostics.append(Diagnostic(level, source, message, element_id))  # type: ignore[arg-type]

    def _warn(self, source: str, message: str) -> None:
        self._diag("warn", source, message)

    @staticmethod
    def _mark(element: Element, node: dict[str, Any]) -> str:
        """Stamp an element with its provisional identity and return the id a diagnostic uses."""
        element.extras[ELEMENT_KEY] = node["key"]
        return DIAG_KEY_PREFIX + str(node["key"])

    def _source_of(self, node: dict[str, Any]) -> str:
        return f"{self.svg_id} > {node['spath']}"

    # -- naming ------------------------------------------------------------------------------
    def _name(self, node: dict[str, Any]) -> str:
        if node.get("name"):
            return str(node["name"])
        tag = node["tag"]
        self._tag_counts[tag] = self._tag_counts.get(tag, 0) + 1
        return f"{tag}#{self._tag_counts[tag]}"

    # -- the walk ----------------------------------------------------------------------------
    def _node(self, node: dict[str, Any], context: _Context) -> None:
        style = node["style"]
        opacity = context.opacity * float(style.get("opacity", 1.0))
        clip = self._clip_of(node, context)
        raster_key = context.raster_key if node.get("viaUse") else node["key"]
        reason = self._raster_reason(node)
        if reason is None and node["tag"] == "use":
            # The expanded copy is deleted before Python runs, so a raster inside it can no longer
            # be screenshotted: if any part of the subtree needs one, the whole `use` becomes it.
            reason = self._subtree_raster_reason(node)
            if reason is not None:
                reason = f"{reason} (inside a <use>)"
        if reason is not None:
            self._raster(node, _Context(opacity, clip, context.group, raster_key), reason)
            return

        kind = node.get("kind")
        if kind == "container":
            group = context.group
            if node.get("group"):
                group = self._new_group(node, context.group)
            child_context = _Context(opacity, clip, group, raster_key)
            for child in node.get("children") or []:
                self._node(child, child_context)
            if node.get("group"):
                self._close_group(cast(str, group))
            return
        inner = _Context(opacity, clip, context.group, raster_key)
        if kind == "shape":
            self._shape(node, inner)
        elif kind == "text":
            self._text(node, inner)
        elif kind == "image":
            self._image(node, inner)
        elif kind == "raster":
            self._raster(node, inner, str(node.get("rasterReason") or "svg feature"))

    # -- groups ------------------------------------------------------------------------------
    def _new_group(self, node: dict[str, Any], parent: str | None) -> str:
        self._group_count += 1
        group_id = f"{self.svg_id}-g{self._group_count}"
        self.groups.append(Group(id=group_id, parent=parent, name=self._name(node), box=None))
        return group_id

    def _close_group(self, group_id: str) -> None:
        """Give the group the union box of its members, and drop it when it never got any."""
        members = [e.box for e in self.elements if e.group == group_id]
        members += [g.box for g in self.groups if g.parent == group_id and g.box is not None]
        group = next(g for g in self.groups if g.id == group_id)
        if not members:
            self.groups.remove(group)
            for child in self.groups:
                if child.parent == group_id:
                    child.parent = group.parent
            return
        box = members[0]
        for other in members[1:]:
            box = box.union(other)
        group.box = box

    # -- clipping ----------------------------------------------------------------------------
    def _clip_of(self, node: dict[str, Any], context: _Context) -> Box | None:
        clip = context.clip
        info = node.get("clip")
        if info and info.get("kind") == "rect":
            matrix = _mat(info["ctm"])
            box, parts = _mapped_box(info["rect"], matrix)
            if abs(parts.rotation) > 0.01:
                # The IR clip is axis-aligned, so a rotated clip rect can only be approximated.
                # Its *bounding* box is the safe side of the approximation: it clips less than the
                # author asked for, never more, so nothing visible is thrown away silently.
                x, y, w, h = (float(v) for v in info["rect"])
                corners = [_apply(matrix, px, py)
                           for px, py in ((x, y), (x + w, y), (x + w, y + h), (x, y + h))]
                box = _box_of_points(corners)
                self._warn(self._source_of(node),
                           "rotated clipPath rect widened to its bounding box (the IR clip is axis-aligned)")
            clip = box if clip is None else clip.intersect(box)
        if node.get("kind") == "container" and node.get("viewportClip"):
            viewport = Box(*[float(v) for v in node["viewportClip"]])
            clip = viewport if clip is None else clip.intersect(viewport)
        return clip

    def _effective_clip(self, box: Box, clip: Box | None, drawn: Box | None = None) -> Box | None:
        """Only record a clip that actually cuts the element, and never one outside the canvas.

        `drawn` — the element's bounding client rect — is passed for **rotated** elements only,
        whose IR box is by definition the pre-rotation box: a vertical axis label's unrotated box
        sticks far out of the viewport while the label itself sits comfortably inside it, and
        clipping that would be a defect we invented. `CLIP_TOLERANCE_PX` absorbs the other side of
        the same coin: a glyph's left side bearing makes the client rect a pixel wider than the
        text's advance box, which is not a clip either.
        """
        if clip is None:
            return None
        test = drawn if drawn is not None else box
        if (clip.x <= test.x + CLIP_TOLERANCE_PX and clip.y <= test.y + CLIP_TOLERANCE_PX
                and clip.x2 >= test.x2 - CLIP_TOLERANCE_PX and clip.y2 >= test.y2 - CLIP_TOLERANCE_PX):
            return None
        return clip.intersect(self.canvas.box)

    # -- rasters -----------------------------------------------------------------------------
    def _raster_reason(self, node: dict[str, Any]) -> str | None:
        """The feature that stops this element being native, or `None` when it can be native."""
        style = node["style"]
        filter_info = node.get("filter")
        if filter_info and filter_info.get("kind") == "unsupported":
            return f"svg filter: {filter_info.get('detail')}"
        if node.get("hasMask"):
            return "svg mask"
        if node.get("blend"):
            return f"svg mix-blend-mode: {node['blend']}"
        clip_info = node.get("clip")
        if clip_info and clip_info.get("kind") == "unsupported":
            return f"svg clipPath: {clip_info.get('detail')}"
        for paint in ("fillPaint", "strokePaint"):
            info = node.get(paint) or {}
            if info.get("type") == "pattern":
                return f"svg {paint[:-5]} pattern"
            if info.get("type") == "unsupported":
                return f"svg {paint[:-5]} {info.get('detail')}"
        if node.get("kind") == "shape":
            for slot in ("markerStart", "markerMid", "markerEnd"):
                marker = node.get(slot)
                if marker and marker.get("type") == "unknown":
                    return f"svg {slot} marker: {marker.get('detail')}"
            if node.get("markerMid"):
                return "svg marker-mid (PowerPoint has no mid-line marker)"
        if style.get("strokeLinejoin") == "arcs":
            return "svg stroke-linejoin: arcs"
        return None

    def _subtree_raster_reason(self, node: dict[str, Any]) -> str | None:
        for child in node.get("children") or []:
            reason = self._raster_reason(child) or self._subtree_raster_reason(child)
            if reason:
                return reason
        return None

    def _raster_box(self, node: dict[str, Any]) -> Box:
        """What the capture has to cover: the geometry rect plus everything painted outside it.

        `getBoundingClientRect()` on an SVG shape is the *geometry* box — no stroke, no marker, no
        filter halo, and zero-high for a horizontal `<line>`. Rastering that would cut the picture
        down to the skeleton of the shape (or, for the line, fail outright).
        """
        box = Box(*[float(v) for v in node["rect"]])
        style = node.get("style") or {}
        scale = _mean_scale(_mat(node["ctm"]))
        pad = 0.5
        stroke_width = 0.0
        if (node.get("strokePaint") or {}).get("type") == "color":
            stroke_width = float(style.get("strokeWidth", 0.0)) * scale
            pad = max(pad, stroke_width / 2)
        for slot in ("markerStart", "markerMid", "markerEnd"):
            marker = node.get(slot)
            if not marker:
                continue
            extent = max(float(marker.get("width", 3)), float(marker.get("height", 3)))
            pad = max(pad, extent * (stroke_width if marker.get("units") == "strokeWidth" else scale))
        info = node.get("filter")
        if info and info.get("kind") == "unsupported":
            # A blur reaches roughly 3σ; σ is unknown for an arbitrary filter chain, so take the
            # renderer's own limit of the default filter region: 10 % of the box on every side.
            pad = max(pad, 0.1 * max(box.w, box.h), 8.0)
        return Box(box.x - pad, box.y - pad, box.w + 2 * pad, box.h + 2 * pad).intersect(self.canvas.box)

    def _raster(self, node: dict[str, Any], context: _Context, reason: str) -> None:
        """Queue an isolated capture of one element; the screenshots happen after the walk."""
        key = context.raster_key or node["key"]
        box = self._raster_box(node)
        if box.w <= 0 or box.h <= 0:
            self._warn(self._source_of(node), f"{reason}: element is outside the canvas; dropped")
            return
        safe = re.sub(r"[^A-Za-z0-9_.-]", "-", f"{key}")
        element = Element(
            kind="raster",
            box=box,
            src=str(self.output / f"{safe}.png"),
            reason=reason,
            name=self._name(node),
            group=context.group,
            opacity=round(context.opacity, 6),
            clip=self._effective_clip(box, context.clip),
            source=self._source(node),
        )
        self.elements.append(element)
        self._rasters.append((f'[{NODE_ATTR}="{key}"]', element))
        self._diag("warn", self._source_of(node), f"rasterised: {reason}", self._mark(element, node))

    def _capture_rasters(self) -> None:
        for selector, element in self._rasters:
            try:
                self.assets.append(
                    capture_isolated(self.page, selector, Path(element.src or ""), element.box)
                )
            except ValueError as error:      # the element is gone: drop it rather than ship a
                self.elements.remove(element)  # raster whose file does not exist
                self._diag("error", self.svg_id, f"isolated capture failed, element dropped: {error}")
                continue
            href = element.extras.get("x-wpf-remote")
            if href and not _has_ink(Path(element.src or "")):
                # `REMOTE_RESOURCES=raster` and the browser drew nothing: the request was refused or
                # failed. A blank picture is not an image; say so instead (WP-F).
                self.elements.remove(element)
                key = DIAG_KEY_PREFIX + str(element.extras.get(ELEMENT_KEY))
                self.diagnostics[:] = [d for d in self.diagnostics if d.elementId != key]
                self._diag("error", element.source.get("svg") or self.svg_id,
                           f"image did not load: {href} — the remote host is not public or did not respond")

    # -- shared element bits -------------------------------------------------------------------
    def _source(self, node: dict[str, Any]) -> dict[str, Any]:
        return {"path": self._css_path, "tag": node["tag"], "svg": self._source_of(node)}

    def _fill(self, node: dict[str, Any], ctm: Matrix6, bbox: Sequence[float] | None) -> dict[str, Any]:
        info = node.get("fillPaint") or {"type": "none"}
        style = node["style"]
        alpha = float(style.get("fillOpacity", 1.0))
        if info.get("type") == "color":
            parsed = _colour(info.get("value"))
            if parsed is None:
                return {"type": "none"}
            colour, colour_alpha = parsed
            return {"type": "solid", "color": colour, "alpha": round(alpha * colour_alpha, 6)}
        if info.get("type") == "gradient":
            return self._gradient(node, info, ctm, bbox, alpha)
        return {"type": "none"}

    def _stroke(self, node: dict[str, Any], ctm: Matrix6) -> dict[str, Any] | None:
        style = node["style"]
        info = node.get("strokePaint") or {"type": "none"}
        if info.get("type") != "color":
            return None
        parsed = _colour(info.get("value"))
        width = float(style.get("strokeWidth", 1.0)) * _mean_scale(ctm)
        if parsed is None or width <= 0:
            return None
        colour, colour_alpha = parsed
        alpha = float(style.get("strokeOpacity", 1.0)) * colour_alpha
        dashes = [round(d * _mean_scale(ctm), 3) for d in _dash_list(style.get("strokeDasharray"))]
        stroke: dict[str, Any] = {
            "color": colour,
            "alpha": round(alpha, 6),
            "width": round(width, 3),
            "dash": dashes if dashes else "solid",
            "cap": style.get("strokeLinecap", "butt"),
            "join": style.get("strokeLinejoin", "miter"),
            "headEnd": _line_end(node.get("markerStart"), width, _mean_scale(ctm), self._warn,
                                 self._source_of(node)),
            "tailEnd": _line_end(node.get("markerEnd"), width, _mean_scale(ctm), self._warn,
                                 self._source_of(node)),
        }
        return stroke

    def _shadow(self, node: dict[str, Any], ctm: Matrix6) -> dict[str, Any] | None:
        info = node.get("filter")
        if not info or info.get("kind") != "dropShadow":
            return None
        parsed = _colour(info.get("color")) or ("000000", 1.0)
        scale = _mean_scale(ctm)
        return {
            "color": parsed[0],
            "alpha": round(float(info.get("opacity", 1.0)) * parsed[1], 6),
            # DrawingML's blur radius is about twice a Gaussian sigma.
            "blur": round(float(info.get("stdDeviation", 0.0)) * 2 * scale, 3),
            "dx": round(float(info.get("dx", 0.0)) * scale, 3),
            "dy": round(float(info.get("dy", 0.0)) * scale, 3),
        }

    def _gradient(
        self,
        node: dict[str, Any],
        info: dict[str, Any],
        ctm: Matrix6,
        bbox: Sequence[float] | None,
        alpha: float,
    ) -> dict[str, Any]:
        from svgelements import Matrix as SvgMatrix

        stops = []
        for stop in info.get("stops") or []:
            parsed = _colour(stop.get("color"))
            if parsed is None:
                continue
            stops.append({
                "pos": round(float(stop.get("offset", 0.0)), 6),
                "color": parsed[0],
                "alpha": round(alpha * parsed[1] * float(stop.get("opacity", 1.0)), 6),
            })
        if len(stops) < 2:
            self._warn(self._source_of(node), "gradient with fewer than two stops treated as its first colour")
            return {"type": "solid", "color": stops[0]["color"], "alpha": stops[0]["alpha"]} if stops \
                else {"type": "none"}
        if (info.get("spread") or "pad") != "pad":
            self._warn(self._source_of(node), f"gradient spreadMethod {info['spread']} approximated as pad")

        matrix = ctm
        if info.get("transform"):
            local = SvgMatrix(str(info["transform"]))
            matrix = _compose(ctm, (local.a, local.b, local.c, local.d, local.e, local.f))
        kind = info.get("kind", "linear")
        angle = 180.0
        if kind == "linear":
            x1, y1, x2, y2 = self._gradient_vector(info, bbox)
            start = _apply(matrix, x1, y1)
            end = _apply(matrix, x2, y2)
            dx, dy = end[0] - start[0], end[1] - start[1]
            if abs(dx) > 1e-9 or abs(dy) > 1e-9:
                # 02-IR-SCHEMA: the CSS angle convention — 0 = to top, 90 = to right, 180 = to bottom.
                angle = _norm_deg(math.degrees(math.atan2(dx, -dy)))
        else:
            angle = 0.0
        return {"type": "gradient", "kind": kind, "angle": round(angle, 3), "stops": stops}

    def _gradient_vector(
        self, info: dict[str, Any], bbox: Sequence[float] | None
    ) -> tuple[float, float, float, float]:
        """The gradient's start/end in the element's user space, both unit systems resolved."""
        def value(raw: Any, default: float, span: float, origin: float) -> float:
            if raw is None:
                fraction = default
            else:
                text = str(raw).strip()
                fraction = float(_NUMBER.findall(text)[0]) / 100 if text.endswith("%") \
                    else float(_NUMBER.findall(text)[0])
            if info.get("units", "objectBoundingBox") == "objectBoundingBox":
                return origin + fraction * span
            return fraction

        if info.get("units", "objectBoundingBox") == "objectBoundingBox" and bbox:
            bx, by, bw, bh = (float(v) for v in bbox)
        else:
            bx = by = 0.0
            bw = bh = 1.0
        return (
            value(info.get("x1"), 0.0, bw, bx),
            value(info.get("y1"), 0.0, bh, by),
            value(info.get("x2"), 1.0, bw, bx),
            value(info.get("y2"), 0.0, bh, by),
        )

    # -- shapes ---------------------------------------------------------------------------------
    def _shape(self, node: dict[str, Any], context: _Context) -> None:
        geom = node.get("geom") or {}
        ctm = _mat(node["ctm"])
        kind = geom.get("kind")
        if kind == "rect":
            self._emit_rect(node, geom, ctm, context)
        elif kind == "ellipse":
            self._emit_ellipse(node, geom, ctm, context)
        elif kind in ("line", "polyline"):
            self._emit_polyline(node, geom, ctm, context)
        elif kind == "polygon":
            self._emit_polygon(node, geom, ctm, context)
        elif kind == "path":
            self._emit_path(node, geom, ctm, context)

    def _add_shape(
        self,
        node: dict[str, Any],
        context: _Context,
        *,
        geometry: dict[str, Any],
        box: Box,
        rotation: float = 0.0,
        fill: dict[str, Any] | None = None,
        stroke: dict[str, Any] | None | bool = False,
        flip_h: bool = False,
        flip_v: bool = False,
    ) -> Element:
        ctm = _mat(node["ctm"])
        element = Element(
            kind="shape",
            box=box,
            rotation=round(rotation, 6) if abs(rotation) > 1e-9 else 0.0,
            opacity=round(context.opacity, 6),
            clip=self._effective_clip(box, context.clip,
                                      _drawn_box(node) if abs(rotation) > 1e-9 else None),
            name=self._name(node),
            group=context.group,
            source=self._source(node),
            geometry=geometry,
            fill=fill if fill is not None else self._fill(node, ctm, node.get("bbox")),
            stroke=self._stroke(node, ctm) if stroke is False else cast("dict[str, Any] | None", stroke or None),
            shadow=self._shadow(node, ctm),
            flipH=flip_h,
            flipV=flip_v,
        )
        self.elements.append(element)
        return element

    def _emit_rect(self, node: dict[str, Any], geom: dict[str, Any], ctm: Matrix6, context: _Context) -> None:
        box, parts = _mapped_box((geom["x"], geom["y"], geom["w"], geom["h"]), ctm)
        if box.w <= MIN_EXTENT_PX or box.h <= MIN_EXTENT_PX:
            self._warn(self._source_of(node), "rect has no area; dropped")
            return
        if parts.skewed:
            self._warn(self._source_of(node), "skewed rect emitted as a custom path")
            self._emit_corner_path(node, ctm, context, [
                (geom["x"], geom["y"]), (geom["x"] + geom["w"], geom["y"]),
                (geom["x"] + geom["w"], geom["y"] + geom["h"]), (geom["x"], geom["y"] + geom["h"]),
            ], closed=True)
            return
        rx, ry = float(geom.get("rx", 0.0)), float(geom.get("ry", 0.0))
        if rx > 0 or ry > 0:
            if abs(rx - ry) > 1e-6:
                self._warn(self._source_of(node),
                           f"rect rx {rx} ≠ ry {ry}; PowerPoint roundRect uses the smaller radius")
            radius = min(rx * parts.sx, ry * parts.sy) if ry > 0 else rx * parts.sx
            geometry = {"type": "roundRect",
                        "radius": {k: round(radius, 3) for k in ("tl", "tr", "br", "bl")}}
        else:
            geometry = {"type": "rect"}
        self._add_shape(node, context, geometry=geometry, box=box, rotation=parts.rotation,
                        flip_v=parts.flip_v)

    def _emit_ellipse(self, node: dict[str, Any], geom: dict[str, Any], ctm: Matrix6, context: _Context) -> None:
        arc = self._ring_segment(node, geom, ctm, context)
        if arc is not None:
            return
        rx, ry = float(geom["rx"]), float(geom["ry"])
        box, parts = _mapped_box((geom["cx"] - rx, geom["cy"] - ry, 2 * rx, 2 * ry), ctm)
        if box.w <= MIN_EXTENT_PX or box.h <= MIN_EXTENT_PX:
            self._warn(self._source_of(node), "ellipse has no area; dropped")
            return
        self._add_shape(node, context, geometry={"type": "ellipse"}, box=box,
                        rotation=parts.rotation, flip_v=parts.flip_v)

    def _ring_segment(self, node: dict[str, Any], geom: dict[str, Any], ctm: Matrix6, context: _Context) -> Element | None:
        """A dashed circle used as a ring segment → `blockArc` (12-WP2's donut rule).

        The test is the dash pattern, not the author's intent: one on/off pair whose on-length is
        shorter than the circumference and whose period covers it (within 1 px, because designers
        round the numbers they type). rOuter/rInner come from the stroke width, the start angle
        from the CTM's rotation and `stroke-dashoffset`, and the box is the *outer* circle's box.
        """
        style = node["style"]
        if not geom.get("circle"):
            return None
        if (node.get("fillPaint") or {}).get("type") != "none":
            return None
        stroke_paint = node.get("strokePaint") or {}
        if stroke_paint.get("type") != "color":
            return None
        dashes = _dash_list(style.get("strokeDasharray"))
        if len(dashes) != 2:
            return None
        radius = float(geom["rx"])
        width = float(style.get("strokeWidth", 0.0))
        if radius <= 0 or width <= 0:
            return None
        circumference = 2 * math.pi * radius
        on, off = dashes
        if not (on < circumference and on + off >= circumference - 1.0):
            return None
        sweep = on / circumference * 360.0
        if sweep >= 359.5:
            return None
        parts = _decompose(ctm)
        scale = _mean_scale(ctm)
        offset = float(style.get("strokeDashoffset", 0.0))
        start = _norm_deg(parts.rotation - offset / circumference * 360.0)
        cx, cy = _apply(ctm, float(geom["cx"]), float(geom["cy"]))
        r_outer = (radius + width / 2) * scale
        r_inner = max(0.0, (radius - width / 2) * scale)
        if style.get("strokeLinecap", "butt") != "butt":
            self._warn(self._source_of(node),
                       f"ring segment drawn with stroke-linecap {style['strokeLinecap']}; emitted with flat ends")
        parsed = _colour(stroke_paint.get("value")) or ("000000", 1.0)
        alpha = float(style.get("strokeOpacity", 1.0)) * parsed[1]
        box = Box(cx - r_outer, cy - r_outer, 2 * r_outer, 2 * r_outer)
        element = self._add_shape(
            node, context,
            geometry={"type": "blockArc", "arc": {
                "cx": round(cx, 3), "cy": round(cy, 3),
                "rOuter": round(r_outer, 3), "rInner": round(r_inner, 3),
                "startDeg": round(start, 3), "endDeg": round(_norm_deg(start + sweep), 3),
            }},
            box=box,
            fill={"type": "solid", "color": parsed[0], "alpha": round(alpha, 6)},
            stroke=None,
        )
        self._diag("info", self._source_of(node),
                   f"dashed circle read as a ring segment: {sweep:.2f}° from {start:.2f}°",
                   self._mark(element, node))
        return element

    def _emit_polyline(self, node: dict[str, Any], geom: dict[str, Any], ctm: Matrix6, context: _Context) -> None:
        points = [_apply(ctm, float(x), float(y)) for x, y in geom.get("points") or []]
        if len(points) < 2:
            self._warn(self._source_of(node), f"{geom['kind']} with fewer than two points; dropped")
            return
        box = _box_of_points(points)
        is_line = geom["kind"] == "line"
        geometry = {"type": "line" if is_line else "polyline",
                    "points": [[round(x, 3), round(y, 3)] for x, y in points]}
        # A `<line>` encloses no area, so SVG never fills it — but a `<polyline>` *is* filled as if
        # it were closed, and the default fill is black, so dropping it would silently erase paint.
        self._add_shape(node, context, geometry=geometry, box=box,
                        fill={"type": "none"} if is_line else None)

    def _emit_polygon(self, node: dict[str, Any], geom: dict[str, Any], ctm: Matrix6, context: _Context) -> None:
        self._emit_corner_path(node, ctm, context, geom.get("points") or [], closed=True)

    def _emit_corner_path(
        self,
        node: dict[str, Any],
        ctm: Matrix6,
        context: _Context,
        local_points: Sequence[Sequence[float]],
        *,
        closed: bool,
    ) -> None:
        points = [_apply(ctm, float(x), float(y)) for x, y in local_points]
        if len(points) < 2:
            self._warn(self._source_of(node), "polygon with fewer than two points; dropped")
            return
        commands: list[list[Any]] = [["M", round(points[0][0], 3), round(points[0][1], 3)]]
        commands += [["L", round(x, 3), round(y, 3)] for x, y in points[1:]]
        if closed:
            commands.append(["Z"])
        box = _box_of_points(points)
        rect = _axis_aligned_rect(points)
        if rect is not None and closed:
            self._add_shape(node, context, geometry={"type": "rect"}, box=rect)
            return
        self._add_shape(node, context,
                        geometry={"type": "custom", "path": commands, "fillRule": self._fill_rule(node)},
                        box=box)

    @staticmethod
    def _fill_rule(node: dict[str, Any]) -> str:
        rule = (node.get("style") or {}).get("fillRule")
        return "evenodd" if rule == "evenodd" else "nonzero"

    def _emit_path(self, node: dict[str, Any], geom: dict[str, Any], ctm: Matrix6, context: _Context) -> None:
        from svgelements import Matrix as SvgMatrix
        from svgelements import Path as SvgPath

        d = (geom.get("d") or "").strip()
        if not d:
            self._warn(self._source_of(node), "path with an empty d; dropped")
            return
        path = SvgPath(d)
        path.approximate_arcs_with_cubics()
        path *= SvgMatrix(*ctm)
        path.reify()
        commands = _path_commands(path)
        if not commands or commands[0][0] != "M":
            self._warn(self._source_of(node), "path does not start with a move; dropped")
            return
        bounds = path.bbox()
        box = Box(float(bounds[0]), float(bounds[1]),
                  float(bounds[2]) - float(bounds[0]), float(bounds[3]) - float(bounds[1])) \
            if bounds else Box(*[float(v) for v in node["rect"]])

        preset = _detect_preset(path, commands)
        if preset is not None:
            geometry = {k: v for k, v in preset.geometry.items()}
            if "arc" in geometry:
                geometry["arc"] = {k: round(float(v), 3) for k, v in geometry["arc"].items()}
            element = self._add_shape(node, context, geometry=geometry, box=preset.box)
            self._diag("info", self._source_of(node),
                       f"path emitted as {preset.geometry['type']} ({preset.note})",
                       self._mark(element, node))
            return
        rounded = [[c[0]] + [round(float(v), 3) for v in c[1:]] for c in commands]
        self._add_shape(node, context,
                        geometry={"type": "custom", "path": rounded, "fillRule": self._fill_rule(node)},
                        box=box)

    # -- text -----------------------------------------------------------------------------------
    def _text(self, node: dict[str, Any], context: _Context) -> None:
        runs = node.get("runs") or []
        if not runs:
            return
        ctm = _mat(node["ctm"])
        parts = _decompose(ctm)
        rotation = parts.rotation if node.get("runFrame") == "local" else 0.0
        # Font sizes and letter spacing inside an <svg> are in *user units*: a 7-unit label in a
        # viewBox that doubles everything renders at 14 canvas px, and the IR is canvas px.
        scale = _mean_scale(ctm)

        if node.get("runFrame") == "local":
            boxes = [self._local_to_canvas(run["box"], node, ctm, parts) for run in runs]
        else:
            boxes = [Box(*[float(v) for v in run["box"]]) for run in runs]

        # Lines are the browser's: fragments whose tops agree within half a pixel are one line
        # (02-IR-SCHEMA §text), which is also what a `tspan` with its own `x`/`dy` produces.
        lines: list[dict[str, Any]] = []
        for run, box in zip(runs, boxes, strict=False):
            if not (run.get("text") or ""):
                continue
            if lines and abs(lines[-1]["box"].y - box.y) < 0.5:
                lines[-1]["box"] = lines[-1]["box"].union(box)
                lines[-1]["runs"].append(self._run(run, scale))
            else:
                lines.append({"box": box, "runs": [self._run(run, scale)]})
        if not lines:
            return
        element_box = lines[0]["box"]
        for line in lines[1:]:
            element_box = element_box.union(line["box"])

        line_height = lines[0]["box"].h
        if len(lines) > 1:
            line_height = lines[1]["box"].y - lines[0]["box"].y
        style = node["style"]
        align = {"start": "left", "middle": "center", "end": "right"}.get(
            node.get("textStyle", {}).get("textAnchor", "start"), "left"
        )
        paragraph = {
            "align": align,
            "lineHeightPx": round(line_height, 3),
            "spaceBeforePx": 0.0,
            "spaceAfterPx": 0.0,
            "bullet": None,
            "lines": [{"box": line["box"].to_json(), "runs": line["runs"]} for line in lines],
        }
        transform_case = node.get("textStyle", {}).get("textTransform")
        element = Element(
            kind="text",
            box=element_box,
            rotation=round(rotation, 6) if abs(rotation) > 1e-9 else 0.0,
            opacity=round(context.opacity, 6),
            clip=None,
            name=self._name(node),
            group=context.group,
            source=self._source(node),
            paragraphs=[paragraph],
            anchor="top",
            writingMode="horizontal",
            wrap=False,
            transformCase=_TRANSFORM_CASE.get(transform_case or "none"),
        )
        self.elements.append(element)
        drawn = _drawn_box(node) if abs(rotation) > 1e-9 else None
        if self._effective_clip(element_box, context.clip, drawn) is not None:
            self._diag("error", self._source_of(node),
                       "svg text is clipped; PowerPoint cannot clip a text frame",
                       self._mark(element, node))
        if (style.get("stroke") or "none") != "none":
            self._warn(self._source_of(node), "svg text stroke dropped (PowerPoint text has no outline here)")
        if transform_case == "capitalize":
            self._warn(self._source_of(node),
                       "text-transform: capitalize has no PowerPoint equivalent; the run keeps the source text")

    def _local_to_canvas(self, local: Sequence[float], node: dict[str, Any], ctm: Matrix6, parts: _Decomposed) -> Box:
        """A local-units box → the canvas box it occupies in the element's *unrotated* frame."""
        element_local = node.get("bbox") or local
        ex, ey, ew, eh = (float(v) for v in element_local)
        cx, cy = _apply(ctm, ex + ew / 2, ey + eh / 2)
        width, height = abs(ew * parts.sx), abs(eh * parts.sy)
        origin_x, origin_y = cx - width / 2, cy - height / 2
        lx, ly, lw, lh = (float(v) for v in local)
        return Box(origin_x + (lx - ex) * abs(parts.sx), origin_y + (ly - ey) * abs(parts.sy),
                   lw * abs(parts.sx), lh * abs(parts.sy))

    def _run(self, run: dict[str, Any], scale: float) -> dict[str, Any]:
        parsed = _colour(run.get("fill")) or ("000000", 1.0)
        decoration = run.get("textDecorationLine") or "none"
        spacing = run.get("letterSpacing") or "normal"
        weight = run.get("fontWeight") or "400"
        return {
            "text": run.get("text") or "",
            "font": _first_family(run.get("fontFamily")),
            "sizePx": round(float(run.get("fontSize", 16.0)) * scale, 3),
            "weight": int(float(weight)) if str(weight).replace(".", "").isdigit() else (
                700 if weight == "bold" else 400
            ),
            "italic": (run.get("fontStyle") or "normal") != "normal",
            "color": parsed[0],
            "alpha": round(float(run.get("fillOpacity", 1.0)) * parsed[1], 6),
            "underline": "underline" in decoration,
            "strike": "line-through" in decoration,
            "letterSpacingPx": 0.0 if spacing == "normal"
                               else round(float(_NUMBER.findall(spacing)[0]) * scale, 3),
            "baseline": "normal",
            "baselineShiftPx": 0.0,
            "caps": False,
            "href": None,
        }

    # -- images ---------------------------------------------------------------------------------
    def _image(self, node: dict[str, Any], context: _Context) -> None:
        href = str(node.get("href") or "")
        box = Box(*[float(v) for v in node["rect"]])
        if box.w <= 0 or box.h <= 0:
            self._warn(self._source_of(node), "svg image has no area; dropped")
            return
        if _is_remote_href(href):
            # WP-F: the measuring browser's network policy (`config.REMOTE_RESOURCES`). Under `block`
            # the image never loaded; under `raster` the browser's picture of it is captured.
            if config.REMOTE_RESOURCES != "raster":
                self._diag("error", self._source_of(node),
                           f"image did not load: {href} — remote resources are blocked by policy "
                           f"(SLIDE_ENGINE_REMOTE_RESOURCES=block); upload the image as a project asset "
                           f"and reference /api/projects/<id>/assets/<file>")
                return
            before = len(self.elements)
            self._raster(node, context, "remote svg image")
            if len(self.elements) > before:
                self.elements[-1].extras["x-wpf-remote"] = href
            return
        written = self._write_data_uri(href, node)
        if written is None:
            self._raster(node, context, "svg image: source is not embeddable raster data")
            return
        preserve = str(node.get("preserveAspectRatio") or "xMidYMid meet")
        fit = "fill" if preserve.startswith("none") else ("cover" if "slice" in preserve else "contain")
        rotation = 0.0
        if node.get("bbox"):
            box, parts = _mapped_box(node["bbox"], _mat(node["ctm"]))
            rotation = parts.rotation
        local = [float(v) for v in (node.get("bbox") or (box.x, box.y, box.w, box.h))]
        crop = _aspect_crop(written, preserve, local[2], local[3])
        self.elements.append(Element(
            kind="image",
            box=box,
            rotation=round(rotation, 6) if abs(rotation) > 1e-9 else 0.0,
            opacity=round(context.opacity, 6),
            clip=self._effective_clip(box, context.clip,
                                      _drawn_box(node) if abs(rotation) > 1e-9 else None),
            name=self._name(node),
            group=context.group,
            source=self._source(node),
            src=str(written),
            fit=fit,
            crop=crop,
        ))

    def _write_data_uri(self, href: str, node: dict[str, Any]) -> Path | None:
        """Decode a `data:` image into `derived_dir`. Anything else (or SVG) → `None` → raster."""
        match = re.match(r"data:image/(png|jpeg|jpg|gif|webp);base64,(.*)$", href, re.DOTALL)
        if not match:
            return None
        suffix = {"jpeg": "jpg"}.get(match.group(1), match.group(1))
        safe = re.sub(r"[^A-Za-z0-9_.-]", "-", node["key"])
        target = self.output / f"{safe}.{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(match.group(2)))
        self.assets.append(target)
        return target


def _aspect_crop(path: Path, preserve: str, frame_w: float, frame_h: float) -> dict[str, float] | None:
    """An SVG `<image>`'s `preserveAspectRatio` as the crop of a picture framed by its viewport (r3).

    `meet` letterboxes and `slice` crops, each at the alignment it names (`xMinYMax` …) — the
    emitter's own fit could only centre. The frame is the viewport, so a side the source does not reach
    is a negative crop: the empty band PowerPoint's Crop → Fit leaves, and a rotation still turns the
    viewport about its own centre. Fractions of the source are the same in user units and on the
    canvas, so the viewport's local size is enough. `None` for `none` (stretched, no crop) or a source
    with no readable size, which the emitter then fits as before.
    """
    words = [w for w in preserve.split() if w != "defer"]
    if not words or words[0] == "none" or frame_w <= 0 or frame_h <= 0:
        return None
    try:
        from PIL import Image

        with Image.open(path) as image:
            natural_w, natural_h = image.size
    except Exception:  # noqa: BLE001 — an unreadable source keeps the emitter's centred fit
        return None
    if natural_w <= 0 or natural_h <= 0:
        return None
    where = {"Min": 0.0, "Mid": 0.5, "Max": 1.0}
    align = re.match(r"x(Min|Mid|Max)Y(Min|Mid|Max)$", words[0])
    fx, fy = (where[align.group(1)], where[align.group(2)]) if align else (0.5, 0.5)
    pick = max if "slice" in words[1:] else min
    scale = pick(frame_w / natural_w, frame_h / natural_h)
    drawn_w, drawn_h = natural_w * scale, natural_h * scale
    left, top = (frame_w - drawn_w) * fx, (frame_h - drawn_h) * fy
    return {"l": -left / drawn_w, "t": -top / drawn_h,
            "r": (left + drawn_w - frame_w) / drawn_w, "b": (top + drawn_h - frame_h) / drawn_h}


def _is_remote_href(href: str) -> bool:
    """page.js's `isRemoteUrl` for an SVG `href` as written: it leaves the machine (WP-F)."""
    text = (href or "").strip()
    if re.search(r"/api/projects/[^/]+/assets/[^/?#]+", text.split("?", 1)[0].split("#", 1)[0]):
        return False
    if text.startswith("//"):
        return True
    match = re.match(r"([a-z][a-z0-9+.\-]*)://([^/?#]*)", text, re.IGNORECASE)
    if not match:
        return False
    scheme, host = match.group(1).lower(), match.group(2).lower()
    return scheme in ("http", "https", "ws", "wss", "ftp") or (scheme == "file" and host not in ("", "localhost"))


def _has_ink(png: Path) -> bool:
    """Does a captured PNG hold any visible pixel?"""
    from PIL import Image  # lazily, like the rest of the engine's image reads

    try:
        with Image.open(png) as image:
            return image.convert("RGBA").getchannel("A").getbbox() is not None
    except OSError:
        return False


def _drawn_box(node: dict[str, Any]) -> Box | None:
    """The element's bounding client rect — what the browser actually painted, rotation included."""
    rect = node.get("rect")
    if not rect:
        return None
    return Box(*[float(v) for v in rect])


def _compose(outer: Matrix6, inner: Matrix6) -> Matrix6:
    """`outer ∘ inner` in the browser's [a,b,c,d,e,f] convention."""
    a1, b1, c1, d1, e1, f1 = outer
    a2, b2, c2, d2, e2, f2 = inner
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


#: The head PowerPoint draws for each DrawingML line-end size, as a multiple of the line's width on
#: both axes (`w` across the line, `len` along it) — of a line at least `LINE_END_FLOOR_PX` wide: a
#: thinner line gets the head of a line that wide. Measured r1b, 2026-09-25, PowerPoint (COM, once)
#: and PptxRender at 4x, lines 0.75/1/1.5/2/3/4/6 px, every type and (w, len) in sm/med/lg
#: (docs/engine/fidelity/evidence/r1b-measurements.json): a triangle, stealth, oval or diamond head is
#: 2.0 / 3.0 / 5.0 widths wide and long (±0.1) from 3 px up, and 5.0-5.3 / 8.0 / 13.0-13.3 px on
#: every line 2 px or thinner; `w` sets only the width and `len` only the length. PptxRender draws
#: the same sizes (it draws `arrow` as a triangle and does not centre a diamond: renderer gaps);
#: PowerPoint's open `arrow` adds about 1.5 widths of its own stroke to both.
LINE_END_SIZES: dict[str, float] = {"sm": 2.0, "med": 3.0, "lg": 5.0}
LINE_END_FLOOR_PX = 2.65

#: A head the nearest size misses by more than this factor on either axis, either way, is drawn
#: visibly smaller or larger than the design; that is said, not left silent.
LINE_END_MISFIT = 1.5


def _line_end(marker: dict[str, Any] | None, width_px: float = 1.0, unit_px: float = 1.0,
              _warn: Any = None, source: str = "") -> dict[str, Any] | None:
    """A classified `<marker>` → the IR's line-end record, sized as the browser drew the head.

    The browser draws the marker's viewport, `markerWidth` × `markerHeight`, in stroke widths
    (`markerUnits="strokeWidth"`, the default: `width_px` each) or in user units (`unit_px` each);
    with `orient="auto"` the first runs along the line (the head's length), the second across it.
    Authored markers fill their viewport (every fixture and survey marker does), so that is the
    head's size. The IR has one `size` for both axes (`w` = `len`), so the size whose PowerPoint
    head (`LINE_END_SIZES`) misses the design's length and width by the fewest px wins; a tie goes
    to the smaller. Before r1b every marker was `med`: a 14 px head on a 2 px line came out 8 px
    and disappeared under the pin it pointed at (the media family). A zero-sized marker draws
    nothing (SVG 2 §11.6.2), and unknown markers never reach here.
    """
    if not marker or marker.get("type") in (None, "unknown"):
        return None
    per = width_px if (marker.get("units") or "strokeWidth") == "strokeWidth" else unit_px
    length = float(marker.get("width", 3)) * per
    across = float(marker.get("height", 3)) * per
    if length <= 0 or across <= 0:
        return None
    base = max(width_px, LINE_END_FLOOR_PX)

    def miss(size: str) -> tuple[float, float]:
        drawn = LINE_END_SIZES[size] * base
        return abs(drawn - length) + abs(drawn - across), LINE_END_SIZES[size]

    size = min(LINE_END_SIZES, key=miss)
    drawn = LINE_END_SIZES[size] * base
    worst = max(drawn / length, length / drawn, drawn / across, across / drawn)
    if _warn is not None and worst > LINE_END_MISFIT:
        _warn(source, f"an arrowhead of {length:.0f} × {across:.0f} px is drawn at the nearest line-end "
                      f"size PowerPoint has, {drawn:.0f} × {drawn:.0f} px")
    return {"type": marker["type"], "size": size}


# ------------------------------------------------------------------------------------- public API


def install(page: Page) -> None:
    """Define `window.__engineSvg` on this page.

    Evaluated as a function body rather than added as a `<script>` tag: WP1 walks this same DOM,
    and a script element it did not write would show up as an element in the slide. Idempotent.

    `svg.js` reads every paint through the colour normaliser `color.js` defines
    (`window.__engineColor`). WP1 injects it before `page.js`; it is evaluated here too, guarded, so
    `expand_svg` also works on a page where `page.js` never ran.
    """
    color_js = Path(__file__).with_name("color.js").read_text(encoding="utf-8")
    page.evaluate("() => { if (!window.__engineColor) {\n" + color_js + "\n} }")
    page.evaluate("() => {\n" + _SVG_JS + "\n}")


def expand_svg(
    page: Page,
    svg_ids: list[str],
    canvas: Canvas,
    assets_dir: Path,
    *,
    derived: Path,
) -> dict[str, SvgExpansion]:
    """Expand each tagged `<svg>` root into IR elements, in paint order.

    `svg_ids` are the values WP1 wrote into `data-engine-svg` (e.g. `"svg#3"`). Elements come back
    with `id=None, z=None` for WP1 to renumber on splice, and group ids prefixed `<svg id>-`.

    `assets_dir` is the slide's *input* asset folder and is never written to; derived images go to
    `derived`, the slide's folder in the caller's workspace (`app.engine.extract.derived_dir`).
    """
    install(page)
    output = Path(derived)
    output.mkdir(parents=True, exist_ok=True)
    expansions: dict[str, SvgExpansion] = {}
    for svg_id in svg_ids:
        selector = f'[data-engine-svg="{svg_id}"]'
        data = page.evaluate("(sel) => window.__engineSvg.describe(sel)", selector)
        if data is None:
            expansions[svg_id] = SvgExpansion(
                diagnostics=[Diagnostic("error", svg_id, f"no element matches {selector}")]
            )
            continue
        expansions[svg_id] = _Expander(page, svg_id, canvas, output).run(data)
    page.evaluate("() => window.__engineSvg.cleanup()")
    return expansions


def retarget_diagnostics(elements: Iterable[Element], diagnostics: Iterable[Diagnostic]) -> int:
    """Point this expansion's diagnostics at the ids WP1 assigned. Returns how many were relinked.

    Call it once per slide, right after `assign_ids_and_z`, with the assembled IR's element and
    diagnostic lists. Diagnostics that do not use a provisional key are left alone, and a key with
    no surviving element (a raster whose capture failed) is cleared rather than left dangling.
    """
    by_key = {
        element.extras[ELEMENT_KEY]: element.id
        for element in elements
        if element.id and element.extras.get(ELEMENT_KEY)
    }
    relinked = 0
    for diagnostic in diagnostics:
        target = diagnostic.elementId
        if not target or not target.startswith(DIAG_KEY_PREFIX):
            continue
        diagnostic.elementId = by_key.get(target[len(DIAG_KEY_PREFIX):])
        relinked += diagnostic.elementId is not None
    return relinked
