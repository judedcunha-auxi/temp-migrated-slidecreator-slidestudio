"""SVG chart recogniser: hand-drawn chart shapes → native `chart` elements. **WP3c owns this file.**

`recognise_charts(ir) -> IR` looks at the shapes and texts one `<svg>` produced (WP2's expansion),
finds the families that are really a chart, and replaces them with a single `chart` element
(`origin: "recognised"`). Everything it did not consume stays exactly where it was and is listed in
`chart.overlay`, so the design's legend, annotations, dashed outlines and reference lines are still
drawn — on top of the native chart.

The rule that does not bend (`docs/archive/engine/90-CRITIQUE.md` #10): **never guess values.** A number reaches the
spec only when the slide states it — a data label on the mark, a percentage inside a legend entry, or
a numeric axis tick whose position regresses onto the mark's extent. A bar row with no numeric labels
and no numeric ticks is not a chart, however chart-shaped it looks: it stays as shapes and gets an
`info` diagnostic. Half of the chart-looking figures on the two client reference slides are refused by that rule,
and that is the intended outcome, not a shortfall.

What is consumed, and what is only read
---------------------------------------
A text is consumed **only when the native chart redraws its content**: data labels, value-axis tick
labels and category labels. A legend entry is *read* for the slice's share and name but never
consumed, because the contract pins `legend: false` — the design's own legend is what the reader
sees, and deleting it to draw PowerPoint's would lose "79.0m domestic (74%)" for a bare "74". Stack
totals, reference lines, zone rects and annotations are never consumed either: PowerPoint cannot
express them, and leaving them in place keeps them pixel-exact. The pixel gate masks chart frames
(master brief §9), so a chart is judged on its data, which is exactly what this trade optimises.

Confidence (`0.8` accept, `0.6` suspect, per `13-WP3-charts.md`) is a product of independent checks,
so one weak signal drags the whole candidate down:

    confidence = scale × structure × labels

`scale` is the R² of the value↔pixel relation (written labels against drawn extents, or ticks against
positions, or slice labels against drawn angles), `structure` is edge alignment and mark uniformity,
`labels` is category coverage. 0.6–0.8 keeps the shapes and adds a `warn`; below 0.6, nothing is said.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from app.engine.ir import IR, Box, Element

# ------------------------------------------------------------------------------------ tolerances
# All in canvas px (master brief §5: 1 px = 1/96 in on every master), so a tolerance means the same
# thing on a 4:3 deck and a 16:9 one. They are tight on purpose: a hand-drawn chart is drawn on a grid.

EDGE_TOL_PX = 1.5          #: two edges (baselines, left edges, chain joins) count as one
CENTRE_TOL_PX = 2.0        #: two arcs share a centre within this
RADIUS_TOL_PX = 2.0        #: a ring's outer/inner radius matches an arc's within this
PITCH_TOL = 0.08           #: relative tolerance when matching a repeated category pitch
MAX_GAP_SLOTS = 4          #: the widest gap (in empty category slots) a design may leave in a row
LABEL_GAP_PX = 16.0        #: how far beyond a mark's value end a data label may sit
CATEGORY_GAP_PX = 24.0     #: how far outside the plot a category label may sit
TICK_ALIGN_PX = 4.0        #: tick labels line up on one edge within this
SNAP_TOL_PX = 6.0          #: a tick label this close to a gridline is *on* it (see `_snap_to_rules`)
ROW_TOL_PX = 4.0           #: texts belong to the same row of labels within this
MIN_MARKS = 3              #: "≥ 3 axis-aligned rects" — the brief's floor for a bar/column family
MIN_R2 = 0.99              #: the brief's floor for a tick regression
EXTENSION_TOL_PX = 2.0     #: absolute slack when admitting a wrapped bar to an established scale
EXTENSION_TOL_REL = 0.05   #: …and the relative slack
PERCENT_TOL = 3.0          #: percentage points a slice label may differ from the drawn sweep
LEGEND_SWATCH_PX = 16.0    #: a filled square this small beside a text is a legend key, not a bar
ACCEPT = 0.8               #: ≥ this becomes a chart
SUSPECT = 0.6              #: ≥ this earns a "possible chart" warning
MARKER_MAX_PX = 14.0       #: an ellipse this small on a polyline vertex is a marker, not a shape

#: Namespaced `extras` key (master brief §7 escape hatch) for the one fact the frozen IR and
#: `charts_spec.OPTION_KEYS` have no room for: which waterfall points are totals rather than steps.
#: Reported to WP3a — `validate()` ignores namespaced extras, so nothing downstream breaks on it.
EXTRAS_KEY = "x-wp3c-chart"

#: Families are tried in the brief's priority order, whatever their paint order says.
_PRIORITY = {"column": 0, "bar": 1, "pie": 2, "line": 3}

__all__ = ["recognise_charts", "parse_value"]


# -------------------------------------------------------------------------------- numeric grammar
# "optional sign or ~, digits with separators, optional %, m, bn, k suffix" (13-WP3 §WP3c). The whole
# string must match, so `FY27E`, `350→420k` and `Jun` are annotations and never become values.

_NUM = r"\d{1,3}(?:[,   ]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_VALUE_RE = re.compile(
    rf"^(?P<approx>[~≈]?)\s*(?P<sign>[+\-−–])?\s*(?P<num>{_NUM})\s*(?P<suffix>%|bn|m|k)?$",
    re.IGNORECASE,
)
#: A percentage *inside* a longer label ("79.0m domestic (74%)"). Used only for pie/doughnut legend
#: entries, where the design writes the slice's share in the middle of a sentence.
_PERCENT_IN_RE = re.compile(rf"(?<![\w.])(?:{_NUM})\s*%")
_SEPARATORS = re.compile(r"[,   %]")


@dataclass(frozen=True, slots=True)
class Value:
    """One number as the slide wrote it: the value, and the decoration a number format must keep."""

    number: float
    suffix: str = ""        # "", "%", "m", "bn", "k"
    approx: bool = False    # the label said "~41%"
    signed: bool = False    # the label wrote an explicit "+"
    decimals: int = 0


def parse_value(text: str) -> Value | None:
    """The number this label states, or `None` when it is prose.

    Public because the rule it encodes — what counts as a written number — is the whole package.
    """
    match = _VALUE_RE.match(text.strip())
    if match is None:
        return None
    digits = _SEPARATORS.sub("", match.group("num"))
    number = float(digits)
    sign = match.group("sign") or ""
    if sign and sign != "+":
        number = -number
    return Value(
        number=number,
        suffix=(match.group("suffix") or "").lower(),
        approx=bool(match.group("approx")),
        signed=sign == "+",
        decimals=len(digits.split(".")[1]) if "." in digits else 0,
    )


def _percent_in(text: str) -> float | None:
    """The single percentage written inside a longer label, or `None` unless there is exactly one."""
    found = _PERCENT_IN_RE.findall(text)
    if len(found) != 1:
        return None
    return float(_SEPARATORS.sub("", found[0]))


# ------------------------------------------------------------------------------------- small math


def _linfit(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float, float] | None:
    """Least squares `y = slope·x + intercept`, with R². `None` when x has no spread.

    When x spreads and y does not, y does not depend on x at all, so R² is 0 — not the 1.0 a flat
    line "fits" with. Seven equal boxes labelled 9, 7, 9, 8, 10, 8, 4 are a table, not a scale
    (render check d1_s04: a dot-and-number column used to come back as a native bar chart).
    """
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 1e-9:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=False))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    syy = sum((y - mean_y) ** 2 for y in ys)
    if syy <= 1e-9:
        return slope, intercept, 0.0
    residual = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys, strict=False))
    return slope, intercept, max(0.0, 1.0 - residual / syy)


def _scale_r2(values: Sequence[float], extents: Sequence[float]) -> float:
    """How well the written values explain the drawn extents: the R² of `extent = a·value + b`, and
    0 unless a bigger number draws a longer mark (slope > 0) — a chart's scale never runs backwards."""
    fit = _linfit(values, extents)
    if fit is None or fit[0] <= 0:
        return 0.0
    return fit[2]


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _modal(values: Iterable[Any]) -> Any:
    """The most common value; ties broken by `repr` so two runs always agree (determinism)."""
    counts = Counter(v for v in values if v is not None)
    if not counts:
        return None
    best = max(counts.values())
    return sorted((v for v, c in counts.items() if c == best), key=repr)[0]


def _spread(values: Sequence[float]) -> float:
    """Coefficient of variation, clamped to [0, 1] — 0 for identical values."""
    if not values:
        return 1.0
    mean = sum(values) / len(values)
    if abs(mean) < 1e-9:
        return 1.0
    deviation = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
    return min(1.0, deviation / abs(mean))


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _round_series(values: Sequence[float | None]) -> list[float | None]:
    """Round a series to a precision the pixels support (≈ 3 significant digits).

    A regression against a 1 px grid produces 47.51219…; writing that into the workbook claims a
    precision the drawing never had. The quantum comes from the series' magnitude, so every point
    keeps the same number of decimals and two runs agree to the last digit.
    """
    largest = max((abs(v) for v in values if v is not None), default=0.0)
    decimals = 2 if largest < 10 else (1 if largest < 100 else 0)
    return [None if v is None else round(v, decimals) for v in values]


def _union(boxes: Sequence[Box]) -> Box:
    box = boxes[0]
    for other in boxes[1:]:
        box = box.union(other)
    return box


def _gap(box: Box, other: Box) -> float:
    """Distance between two boxes — 0 when they touch or overlap. Decides which chart owns a leftover."""
    dx = max(0.0, max(box.x - other.x2, other.x - box.x2))
    dy = max(0.0, max(box.y - other.y2, other.y - box.y2))
    return math.hypot(dx, dy)


# --------------------------------------------------------------------------------- element views


@dataclass(slots=True)
class _Mark:
    """A drawn datum: a bar, a slice, a ring, a line or a marker."""

    element: Element
    kind: str                       # "rect" | "arc" | "ring" | "poly" | "dot"
    box: Box
    colour: str | None              # "#RRGGBB"
    faded: bool = False             # opacity below 1 — a spec colour cannot carry it

    @property
    def id(self) -> str:
        return self.element.id or ""

    @property
    def z(self) -> int:
        return self.element.z if self.element.z is not None else 0


@dataclass(slots=True)
class _Label:
    """A text the recogniser may read, and sometimes consume."""

    element: Element
    text: str
    box: Box
    value: Value | None

    @property
    def id(self) -> str:
        return self.element.id or ""


@dataclass(slots=True)
class _Scope:
    """One `<svg>` root: its marks, its texts, its legend, and what has already been claimed."""

    key: str
    marks: list[_Mark]
    labels: list[_Label]
    legend: list[tuple[str, str, Box]]     # (swatch colour, the text beside it, the swatch's box)
    used: set[str] = field(default_factory=set)

    def available(self, taken: set[str]) -> list[_Label]:
        """Labels no accepted chart has consumed and this attempt has not already claimed."""
        return [label for label in self.labels if label.id not in self.used and label.id not in taken]


@dataclass(slots=True)
class _Values:
    """What a pattern managed to read off the drawing, and how well it hangs together."""

    series: list[list[float | None]]
    points: list[list[_Mark | None]]
    attached: list[tuple[_Mark, _Label]]   # data labels the chart will redraw (consumed)
    scale_r2: float


@dataclass(slots=True)
class _Chart:
    """An accepted candidate, ready to be spliced into the IR."""

    spec: dict[str, Any]
    confidence: float
    consumed: list[str]
    frame: Box
    plot_rect: Box | None
    style: dict[str, Any]
    scope: str
    name: str
    anchor_source: str = ""
    faded: int = 0                          # marks whose transparency a spec colour cannot carry
    extras: dict[str, Any] = field(default_factory=dict)


def _text_of(element: Element) -> str:
    """The element's text: runs joined as the browser had them, lines joined with a space."""
    lines: list[str] = []
    for paragraph in element.paragraphs or []:
        for line in paragraph.get("lines") or []:
            joined = "".join(str(run.get("text", "")) for run in line.get("runs") or [])
            if joined.strip():
                lines.append(joined.strip())
    return " ".join(lines)


def _first_run(element: Element) -> dict[str, Any] | None:
    for paragraph in element.paragraphs or []:
        for line in paragraph.get("lines") or []:
            for run in line.get("runs") or []:
                return cast(dict[str, Any], run)
    return None


def _colour_of(element: Element) -> tuple[str | None, bool]:
    """The mark's drawn colour as `#RRGGBB`, and whether transparency had to be dropped."""
    faded = element.opacity < 0.999
    fill = element.fill or {}
    if fill.get("type") == "solid":
        return "#" + str(fill["color"]).upper(), faded or float(fill.get("alpha", 1.0)) < 0.999
    if fill.get("type") == "gradient":
        stops = fill.get("stops") or []
        if stops:
            return "#" + str(stops[0]["color"]).upper(), True
    stroke = element.stroke or {}
    if stroke.get("color"):
        return "#" + str(stroke["color"]).upper(), faded or float(stroke.get("alpha", 1.0)) < 0.999
    return None, faded


def _is_filled(element: Element) -> bool:
    return (element.fill or {}).get("type") in ("solid", "gradient")


def _scope_key(element: Element) -> str | None:
    """Which `<svg>` root this element came out of, or `None` for an HTML-born element.

    The recogniser only ever looks inside an SVG: the authoring contract says a data visual that
    PowerPoint can chart *must* be `data-chart`, and this package catches the hand-drawn leftovers.
    Scoping by the svg root also stops a row of absolutely positioned HTML boxes — an equation band,
    a KPI strip — from being read as a bar chart.
    """
    svg_path = (element.source or {}).get("svg")
    if isinstance(svg_path, str) and svg_path:
        return svg_path.split(">")[0].strip()
    group = element.group
    if isinstance(group, str) and group.startswith("svg#"):
        return group.split("-")[0]
    return None


def _scopes(ir: IR) -> list[_Scope]:
    """One `_Scope` per `<svg>` root, in paint order, with its members in paint order."""
    buckets: dict[str, list[Element]] = {}
    order: list[str] = []
    for element in ir.paint_sorted():
        key = _scope_key(element)
        if key is None or element.kind not in ("shape", "text"):
            continue
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(element)

    scopes: list[_Scope] = []
    for key in order:
        marks, labels = _marks_and_labels(buckets[key])
        scopes.append(_Scope(key=key, marks=marks, labels=labels, legend=_legend_of(marks, labels)))
    return scopes


def _marks_and_labels(elements: Sequence[Element]) -> tuple[list[_Mark], list[_Label]]:
    marks: list[_Mark] = []
    labels: list[_Label] = []
    for element in elements:
        if element.kind == "text":
            text = _text_of(element)
            if text:
                labels.append(_Label(element, text, element.box, parse_value(text)))
            continue
        geometry = element.geometry or {}
        gtype = geometry.get("type")
        colour, faded = _colour_of(element)
        filled = _is_filled(element)
        if gtype in ("rect", "roundRect") and filled:
            marks.append(_Mark(element, "rect", element.box, colour, faded))
        elif gtype in ("blockArc", "pie", "chord"):
            marks.append(_Mark(element, "arc", element.box, colour, faded))
        elif gtype == "ellipse" and not filled and (element.stroke or {}).get("width"):
            marks.append(_Mark(element, "ring", element.box, colour, faded))
        elif gtype == "ellipse" and filled:
            marks.append(_Mark(element, "dot", element.box, colour, faded))
        elif gtype == "polyline":
            marks.append(_Mark(element, "poly", element.box, colour, faded))
        elif gtype == "line":
            marks.append(_Mark(element, "rule", element.box, colour, faded))
    return marks, labels


def _rule_positions(scope: _Scope, plot: Box, vertical: bool) -> list[float]:
    """Where the design drew its gridlines and axis lines across this plot, in exact geometry."""
    positions: list[float] = []
    for mark in scope.marks:
        if mark.kind != "rule":
            continue
        points = (mark.element.geometry or {}).get("points") or []
        if len(points) != 2:
            continue
        (x1, y1), (x2, y2) = (float(points[0][0]), float(points[0][1])), (float(points[1][0]), float(points[1][1]))
        if vertical and abs(y1 - y2) <= 0.5 and abs(x2 - x1) >= 0.6 * max(1.0, plot.w):
            positions.append(y1)
        elif not vertical and abs(x1 - x2) <= 0.5 and abs(y2 - y1) >= 0.6 * max(1.0, plot.h):
            positions.append(x1)
    return sorted(positions)


def _legend_of(marks: Sequence[_Mark], labels: Sequence[_Label]) -> list[tuple[str, str, Box]]:
    """Every small filled square with a text beside it — the design's own legend, with its swatch."""
    entries: list[tuple[str, str, Box]] = []
    for mark in sorted(marks, key=lambda m: (m.z, m.id)):
        if mark.kind != "rect" or mark.colour is None:
            continue
        if (mark.box.w > LEGEND_SWATCH_PX or mark.box.h > LEGEND_SWATCH_PX
                or abs(mark.box.w - mark.box.h) > 3):
            continue
        beside = [
            label for label in labels
            if 0 <= label.box.x - mark.box.x2 <= 18
            and abs(label.box.cy - mark.box.cy) <= max(8.0, mark.box.h)
        ]
        if beside:
            nearest = min(beside, key=lambda label: label.box.x - mark.box.x2)
            entries.append((mark.colour, nearest.text, mark.box))
    return entries


# ----------------------------------------------------------------------------------- label lookup


def _claim(label: _Label | None, taken: set[str]) -> _Label | None:
    """Mark a label as spoken for by this attempt, so no second mark can read the same text."""
    if label is not None:
        taken.add(label.id)
    return label


def _budget(marks: Sequence[_Mark], vertical: bool) -> float:
    """How far from its mark a data label may sit before it stops being that mark's label.

    A tight constant is wrong for real designs: the client deck right-aligns "a right-aligned label" at one x, so
    the shortest bar's label is 64 px past its end while the longest bar's is 6 px past. The budget
    therefore scales with the family's own longest mark, and the label still has to line up with the
    mark across the other axis and still has to be nearer to this mark than to any other.
    """
    span = max((mark.box.h if vertical else mark.box.w) for mark in marks) if marks else 0.0
    return max(LABEL_GAP_PX, 0.25 * span) if vertical else max(LABEL_GAP_PX + 8, 0.75 * span)


def _attach_above(mark: _Mark, scope: _Scope, taken: set[str], budget: float = LABEL_GAP_PX) -> _Label | None:
    """The data label sitting on top of a column — outside its top edge, or just inside it."""
    best: tuple[float, _Label] | None = None
    for label in scope.available(taken):
        if label.value is None:
            continue
        if not (mark.box.x - 0.3 * mark.box.w <= label.box.cx <= mark.box.x2 + 0.3 * mark.box.w):
            continue
        gap = mark.box.y - label.box.y2
        if -2.0 <= gap <= budget:
            distance = abs(gap)
        elif mark.box.y - 2 <= label.box.cy <= mark.box.y2:
            distance = LABEL_GAP_PX + (label.box.cy - mark.box.y)
        else:
            continue
        if best is None or distance < best[0]:
            best = (distance, label)
    return best[1] if best else None


def _attach_beside(mark: _Mark, scope: _Scope, taken: set[str],
                   budget: float = LABEL_GAP_PX + 8) -> _Label | None:
    """The data label at the value end of a horizontal bar — outside it, or just inside it."""
    best: tuple[float, _Label] | None = None
    for label in scope.available(taken):
        if label.value is None:
            continue
        if not (mark.box.y - 0.6 * mark.box.h <= label.box.cy <= mark.box.y2 + 0.6 * mark.box.h):
            continue
        gap = label.box.x - mark.box.x2
        if -2.0 <= gap <= budget:
            distance = abs(gap)
        elif mark.box.x <= label.box.cx <= mark.box.x2:
            distance = LABEL_GAP_PX + (mark.box.x2 - label.box.cx)
        else:
            continue
        if best is None or distance < best[0]:
            best = (distance, label)
    return best[1] if best else None


def _attach_inside(mark: _Mark, scope: _Scope, taken: set[str]) -> _Label | None:
    """A data label drawn inside the mark — how a stacked design labels its segments."""
    for label in scope.available(taken):
        if label.value is None:
            continue
        if (mark.box.x <= label.box.cx <= mark.box.x2
                and mark.box.y - 1 <= label.box.cy <= mark.box.y2 + 1):
            return label
    return None


def _label_position(mark: _Mark, label: _Label, vertical: bool) -> str:
    """Where the design put this data label, in PowerPoint's vocabulary."""
    if vertical:
        if label.box.y2 <= mark.box.y + 2:
            return "outEnd"
        return "inEnd" if label.box.cy <= mark.box.y + mark.box.h / 3 else "center"
    if label.box.x >= mark.box.x2 - 2:
        return "outEnd"
    return "inEnd" if label.box.cx >= mark.box.x2 - mark.box.w / 3 else "center"


def _category_above(mark: _Mark, scope: _Scope, taken: set[str]) -> _Label | None:
    best: tuple[float, _Label] | None = None
    for label in scope.available(taken):
        if not (mark.box.x - 2 <= label.box.cx <= mark.box.x2 + 2):
            continue
        gap = mark.box.y - label.box.y2
        if -1.0 <= gap <= CATEGORY_GAP_PX and (best is None or gap < best[0]):
            best = (gap, label)
    return best[1] if best else None


def _category_left(mark: _Mark, scope: _Scope, taken: set[str]) -> _Label | None:
    best: tuple[float, _Label] | None = None
    for label in scope.available(taken):
        if not (mark.box.y - 0.6 * mark.box.h <= label.box.cy <= mark.box.y2 + 0.6 * mark.box.h):
            continue
        gap = mark.box.x - label.box.x2
        if -1.0 <= gap <= CATEGORY_GAP_PX * 3 and (best is None or gap < best[0]):
            best = (gap, label)
    return best[1] if best else None


def _category_row(
    bands: Sequence[tuple[float, float]], baseline: float, scope: _Scope, taken: set[str]
) -> list[_Label | None]:
    """One category label per band, from the topmost row of text under the baseline.

    Designs stack a name row and a sub-note row under a column chart ("Orders" over "350→420k");
    the sub-note is an annotation that must stay drawn, so only the first row is a candidate.
    """
    under = sorted(
        (label for label in scope.available(taken) if label.box.y >= baseline - 2),
        key=lambda label: (label.box.y, label.box.x),
    )
    if not under:
        return [None] * len(bands)
    row_top = under[0].box.y
    row = [label for label in under if abs(label.box.y - row_top) <= ROW_TOL_PX]
    picked: list[_Label | None] = []
    claimed: set[str] = set()
    for start, end in bands:
        best: tuple[float, _Label] | None = None
        for label in row:
            if label.id in claimed or not (start - 2 <= label.box.cx <= end + 2):
                continue
            distance = abs(label.box.cx - (start + end) / 2)
            if best is None or distance < best[0]:
                best = (distance, label)
        if best is not None and best[1].box.y - baseline <= CATEGORY_GAP_PX:
            picked.append(best[1])
            claimed.add(best[1].id)
        else:
            picked.append(None)
    return picked


# ------------------------------------------------------------------------------------- value axis


@dataclass(slots=True)
class _Axis:
    """A numeric scale read off tick labels: `value = slope·position + intercept`."""

    slope: float
    intercept: float
    r2: float
    ticks: list[_Label]
    minimum: float
    maximum: float
    major_unit: float | None

    def value_at(self, position: float) -> float:
        return self.slope * position + self.intercept

    def position_of(self, value: float) -> float:
        return (value - self.intercept) / self.slope


def _align_groups(labels: Sequence[_Label], key: Callable[[_Label], float]) -> list[list[_Label]]:
    """Cluster labels whose `key` (an edge coordinate) agrees within `TICK_ALIGN_PX`."""
    groups: list[list[_Label]] = []
    for label in sorted(labels, key=lambda label: (key(label), label.box.y, label.box.x)):
        for group in groups:
            if abs(key(group[0]) - key(label)) <= TICK_ALIGN_PX:
                group.append(label)
                break
        else:
            groups.append([label])
    return groups


def _find_axis(scope: _Scope, plot: Box, vertical: bool, taken: set[str]) -> _Axis | None:
    """The value axis for this plot, or `None` when the design wrote no numeric ticks.

    Vertical (column charts): a column of numbers beside the plot, aligned on one edge. Horizontal
    (bar charts): a row of numbers above or below it. Both need ≥ 3 ticks and R² ≥ 0.99 — a scale
    read off two points cannot be checked, and an unchecked scale is a guess.
    """
    numeric = [label for label in scope.available(taken) if label.value is not None]
    by_suffix: dict[str, list[_Label]] = {}
    for label in numeric:
        by_suffix.setdefault(label.value.suffix, []).append(label)  # type: ignore[union-attr]

    best: _Axis | None = None
    for _, family in sorted(by_suffix.items()):
        if vertical:
            outside = [
                label for label in family
                if label.box.x2 <= plot.x + TICK_ALIGN_PX or label.box.x >= plot.x2 - TICK_ALIGN_PX
            ]
            groups = (_align_groups(outside, lambda label: label.box.x2)
                      + _align_groups(outside, lambda label: label.box.x))
            position_of: Callable[[_Label], float] = lambda label: label.box.cy  # noqa: E731
        else:
            outside = [
                label for label in family
                if label.box.y >= plot.y2 - TICK_ALIGN_PX or label.box.y2 <= plot.y + TICK_ALIGN_PX
            ]
            groups = _align_groups(outside, lambda label: label.box.cy)
            position_of = lambda label: label.box.cx  # noqa: E731

        for group in groups:
            if len(group) < MIN_MARKS:
                continue
            ordered = sorted(group, key=position_of)
            xs = [position_of(label) for label in ordered]
            ys = [label.value.number for label in ordered]  # type: ignore[union-attr]
            fit = _linfit(xs, ys)
            if fit is None:
                continue
            slope, intercept, r2 = fit
            if r2 < MIN_R2 or abs(slope) < 1e-9:
                continue
            if (slope >= 0) if vertical else (slope <= 0):
                continue  # values must grow upwards on a y axis and rightwards on an x axis
            values = sorted(ys)
            steps = [round(b - a, 6) for a, b in zip(values, values[1:], strict=False)]
            unit: float | None = _median(steps) if steps else None
            if unit is None or unit <= 0 or any(abs(s - unit) > 0.02 * unit for s in steps):
                unit = None
            axis = _Axis(slope, intercept, r2, ordered, min(values), max(values), unit)
            if best is None or (len(axis.ticks), axis.r2) > (len(best.ticks), best.r2):
                best = axis
    return _snap_to_rules(best, scope, plot, vertical) if best is not None else None


def _snap_to_rules(axis: _Axis, scope: _Scope, plot: Box, vertical: bool) -> _Axis:
    """Refit the axis on the gridlines the design drew, when every tick sits on one.

    A tick label's *box* only approximates where the tick is — its centre sits a fraction of the font
    size off the baseline, and that fraction depends on the font's metrics. A gridline is exact
    geometry. Where each tick has one, the gridlines are the better ruler; on a line chart, where
    values are read absolutely instead of as differences, that fraction of a pixel is the whole
    difference between 20 and 20.6.
    """
    rules = _rule_positions(scope, plot, vertical)
    if len(rules) < len(axis.ticks):
        return axis
    positions = [label.box.cy if vertical else label.box.cx for label in axis.ticks]
    snapped: list[float] = []
    for position in positions:
        nearest = min(rules, key=lambda rule: abs(rule - position))
        if abs(nearest - position) > SNAP_TOL_PX or nearest in snapped:
            return axis
        snapped.append(nearest)
    values = [label.value.number for label in axis.ticks]  # type: ignore[union-attr]
    fit = _linfit(snapped, values)
    if fit is None or fit[2] < MIN_R2 or abs(fit[0] - axis.slope) > 0.05 * abs(axis.slope):
        return axis
    return _Axis(fit[0], fit[1], fit[2], axis.ticks, axis.minimum, axis.maximum, axis.major_unit)


# ---------------------------------------------------------------------------------- mark families


def _stacks(rects: Sequence[_Mark]) -> list[list[_Mark]]:
    """Rects sharing an x-span, ordered bottom-up — one stack per category slot."""
    buckets: dict[tuple[int, int], list[_Mark]] = {}
    for rect in rects:
        key = (round(rect.box.x / EDGE_TOL_PX), round(rect.box.w / EDGE_TOL_PX))
        buckets.setdefault(key, []).append(rect)
    stacks = [sorted(group, key=lambda m: -m.box.y) for _, group in sorted(buckets.items())]
    return sorted(stacks, key=lambda stack: stack[0].box.x)


def _regular(lefts: Sequence[float]) -> bool:
    """Do these x positions sit on one pitch, allowing a design to skip a slot or two?

    "Skip a slot" is the gap a client deck left between two year columns: the spacing there is exactly twice
    the pitch, which is a gap slot, not a different chart.
    """
    gaps = [b - a for a, b in zip(lefts, lefts[1:], strict=False)]
    pitch = _median(gaps)
    if pitch <= 0:
        return False
    for gap in gaps:
        slots = gap / pitch
        if abs(slots - round(slots)) > PITCH_TOL * max(1.0, round(slots)) or round(slots) > MAX_GAP_SLOTS:
            return False
    return True


def _column_families(rects: Sequence[_Mark]) -> list[list[_Mark]]:
    """Filled rects of one width whose x positions repeat on a pitch — a column/waterfall family.

    The pitch is checked on the *category* positions, because a clustered chart repeats bar-gap-bar
    then a wider gap: measured bar to bar that looks irregular, measured cluster to cluster it is not.
    """
    by_width: dict[int, list[_Mark]] = {}
    for rect in rects:
        by_width.setdefault(round(rect.box.w / EDGE_TOL_PX), []).append(rect)
    families: list[list[_Mark]] = []
    for _, group in sorted(by_width.items()):
        stacks = _stacks(group)
        if len(stacks) < MIN_MARKS:
            continue
        lefts = [stack[0].box.x for stack in stacks]
        if not _regular(lefts[:: _cluster_split(stacks)]):
            continue
        families.append([mark for stack in stacks for mark in stack])
    return families


def _bar_families(rects: Sequence[_Mark]) -> list[list[_Mark]]:
    """Filled rects of one height sharing a left edge — a horizontal bar family."""
    by_height: dict[int, list[_Mark]] = {}
    for rect in rects:
        by_height.setdefault(round(rect.box.h / EDGE_TOL_PX), []).append(rect)
    families: list[list[_Mark]] = []
    for _, group in sorted(by_height.items()):
        by_left: dict[int, list[_Mark]] = {}
        for rect in group:
            by_left.setdefault(round(rect.box.x / EDGE_TOL_PX), []).append(rect)
        for _, column in sorted(by_left.items()):
            if len(column) >= MIN_MARKS:
                families.append(sorted(column, key=lambda m: m.box.y))
    return families


def _arc_families(marks: Sequence[_Mark]) -> list[list[_Mark]]:
    """Arcs — and the full ring behind them — sharing a centre: a pie/doughnut family."""
    families: list[list[_Mark]] = []
    for mark in sorted((m for m in marks if m.kind in ("arc", "ring")), key=lambda m: (m.z, m.id)):
        for family in families:
            if (abs(family[0].box.cx - mark.box.cx) <= CENTRE_TOL_PX
                    and abs(family[0].box.cy - mark.box.cy) <= CENTRE_TOL_PX):
                family.append(mark)
                break
        else:
            families.append([mark])
    return [family for family in families if any(m.kind == "arc" for m in family)]


def _cluster_split(stacks: Sequence[list[_Mark]]) -> int:
    """How many bars sit side by side in one category: 1 for a single series, k when clustered."""
    if len(stacks) < 4 or any(len(stack) > 1 for stack in stacks):
        return 1
    gaps = [b[0].box.x - (a[0].box.x + a[0].box.w) for a, b in zip(stacks, stacks[1:], strict=False)]
    low, high = min(gaps), max(gaps)
    if high - low <= max(2.0, 0.2 * stacks[0][0].box.w):
        return 1
    threshold = (low + high) / 2
    sizes: list[int] = []
    run = 1
    for gap in gaps:
        if gap > threshold:
            sizes.append(run)
            run = 1
        else:
            run += 1
    sizes.append(run)
    if len(sizes) < 2 or len(set(sizes)) != 1 or sizes[0] < 2:
        return 1
    return sizes[0]


def _bands(
    lefts: Sequence[float], width: float, pitch: float
) -> tuple[list[tuple[float, float]], list[int]]:
    """Category bands — including the empty slots a design leaves as a gap — and each mark's slot."""
    start = lefts[0] - (pitch - width) / 2
    slots = [round((left - lefts[0]) / pitch) for left in lefts]
    return ([(start + i * pitch, start + (i + 1) * pitch) for i in range(slots[-1] + 1)], slots)


# --------------------------------------------------------------------------------- reading values


def _values_single(
    marks: Sequence[_Mark], axis: _Axis | None, scope: _Scope, vertical: bool, taken: set[str]
) -> _Values | None:
    """One series: every mark's own label, else the axis regression applied to its extent."""
    attach = _attach_above if vertical else _attach_beside
    budget = _budget(marks, vertical)
    found: list[_Label | None] = []
    for mark in marks:
        label = attach(mark, scope, taken, budget)
        if label is not None:
            taken.add(label.id)     # one text is one mark's label, never two
        found.append(label)
    if all(label is not None for label in found):
        numbers = [label.value.number for label in found]  # type: ignore[union-attr]
        extents = [mark.box.h if vertical else mark.box.w for mark in marks]
        pairs = cast(list[tuple[_Mark, _Label]], list(zip(marks, found, strict=False)))
        return _Values([numbers], [list(marks)], pairs, _scale_r2(numbers, extents))
    if axis is not None:
        if vertical:
            origin = axis.value_at(max(mark.box.y2 for mark in marks))
            numbers = [axis.value_at(mark.box.y) - origin for mark in marks]
        else:
            origin = axis.value_at(min(mark.box.x for mark in marks))
            numbers = [axis.value_at(mark.box.x2) - origin for mark in marks]
        return _Values([numbers], [list(marks)], [], axis.r2)
    return None


def _values_clustered(
    stacks: Sequence[list[_Mark]], per_category: int, axis: _Axis | None, scope: _Scope, taken: set[str]
) -> _Values | None:
    """`per_category` bars side by side: series k is the k-th bar from the left of every category."""
    bars = [stack[0] for stack in stacks]
    grouped = [bars[i:i + per_category] for i in range(0, len(bars), per_category)]
    points: list[list[_Mark | None]] = [[group[k] for group in grouped] for k in range(per_category)]
    budget = _budget(bars, True)
    found = [[_claim(_attach_above(mark, scope, taken, budget), taken) for mark in series]  # type: ignore[arg-type]
             for series in points]
    if all(label is not None for series in found for label in series):
        values = [[label.value.number for label in series] for series in found]  # type: ignore[union-attr]
        flat_values = [v for series in values for v in series]
        flat_extents = [mark.box.h for series in points for mark in series if mark]
        pairs = [
            (mark, label)
            for series, labels in zip(points, found, strict=False)
            for mark, label in zip(series, labels, strict=False) if mark and label
        ]
        return _Values(values, points, pairs, _scale_r2(flat_values, flat_extents))
    if axis is not None:
        origin = axis.value_at(max(mark.box.y2 for mark in bars))
        values = [[axis.value_at(mark.box.y) - origin for mark in series if mark] for series in points]
        return _Values(values, points, [], axis.r2)
    return None


def _values_stacked(
    stacks: Sequence[list[_Mark]], depth: int, axis: _Axis | None, scope: _Scope, baseline: float,
    taken: set[str],
) -> _Values | None:
    """Segment values: a label inside every segment, else the axis regression on each segment's height.

    A number written *above* a stack is its total, not the top segment's value. It is never consumed
    (PowerPoint cannot draw a stack total) but, when it agrees with the sum of the regressed segments,
    it is strong evidence that the scale is right — so it feeds the confidence instead.
    """
    points: list[list[_Mark | None]] = [[stack[k] for stack in stacks] for k in range(depth)]
    found = [[_claim(_attach_inside(mark, scope, taken), taken) for mark in series]  # type: ignore[arg-type]
             for series in points]
    if all(label is not None for series in found for label in series):
        values = [[label.value.number for label in series] for series in found]  # type: ignore[union-attr]
        pairs = [
            (mark, label)
            for series, labels in zip(points, found, strict=False)
            for mark, label in zip(series, labels, strict=False) if mark and label
        ]
        return _Values(values, points, pairs, 1.0)
    if axis is None:
        return None

    unit = abs(axis.slope)
    values = [[mark.box.h * unit for mark in series if mark] for series in points]
    errors: list[float] = []
    for index, stack in enumerate(stacks):
        top = min(stack, key=lambda mark: mark.box.y)
        # Read, never claimed: a stack total is not a data label, it is a cross-check.
        label = _attach_above(top, scope, taken, _budget([top], True))
        if label is None or label.value is None:
            continue
        total = sum(values[series][index] for series in range(depth))
        if total > 0:
            errors.append(abs(label.value.number - total) / total)
    agreement = _clamp01(1.0 - 4.0 * (sum(errors) / len(errors))) if errors else 1.0
    return _Values(values, points, [], axis.r2 * agreement)


def _extend_to_scale(
    core: Sequence[_Mark], others: Sequence[_Mark], values: Sequence[float], scope: _Scope,
    vertical: bool, taken: set[str],
) -> list[tuple[_Mark, _Label]]:
    """Admit wrapped marks — a bar laid out in a second column — that fit the established scale.

    A client slide set one category as three bars in one column and a fourth bar in another.
    Dropping the fourth would ship an incomplete chart; taking it on faith would be a guess. It is
    admitted only when it carries its own numeric label **and** its own extent matches the scale the
    other bars fixed, so the drawing itself vouches for it.
    """
    extents = [mark.box.h if vertical else mark.box.w for mark in core]
    fit = _linfit(list(values), extents)
    if fit is None or fit[0] <= 0 or fit[2] < MIN_R2:
        return []
    slope, intercept, _ = fit
    budget = _budget(list(core), vertical)
    admitted: list[tuple[_Mark, _Label]] = []
    for mark in sorted(others, key=lambda m: (m.box.x, m.box.y)):
        label = (_attach_above(mark, scope, taken, budget) if vertical
                 else _attach_beside(mark, scope, taken, budget))
        if label is None or label.value is None:
            continue
        predicted = slope * label.value.number + intercept
        actual = mark.box.h if vertical else mark.box.w
        if abs(predicted - actual) > max(EXTENSION_TOL_PX, EXTENSION_TOL_REL * max(1.0, abs(predicted))):
            continue
        taken.add(label.id)
        admitted.append((mark, label))
    return admitted


# ------------------------------------------------------------------------------ pattern: columns


def _columns(family: Sequence[_Mark], scope: _Scope) -> tuple[_Chart | None, float, str]:
    """Clustered or stacked columns standing on a shared baseline."""
    taken: set[str] = set()
    stacks = _stacks(family)
    baseline = max(mark.box.y2 for mark in family)
    if any(abs(stack[0].box.y2 - baseline) > EDGE_TOL_PX for stack in stacks):
        return None, 0.0, "not a column family"

    width = stacks[0][0].box.w
    lefts = [stack[0].box.x for stack in stacks]
    per_category = _cluster_split(stacks)
    category_lefts = lefts[::per_category]
    cluster_width = width * per_category
    # The pitch is the distance between *categories*, which on a clustered chart is not the distance
    # between neighbouring bars — measuring it on the bars would put every band in the wrong place.
    category_pitch = _median(
        [b - a for a, b in zip(category_lefts, category_lefts[1:], strict=False)]
    ) or cluster_width
    depth = _modal(len(stack) for stack in stacks) or 1
    stacked = per_category == 1 and depth > 1
    if stacked and any(len(stack) != depth for stack in stacks):
        return None, 0.0, "the stacks do not all have the same number of segments"
    if not stacked and any(len(stack) > 1 for stack in stacks):
        return None, 0.0, "overlapping columns of the same width"

    top = min(mark.box.y for mark in family)
    axis = _find_axis(
        scope, Box(lefts[0], top, lefts[-1] + width - lefts[0], baseline - top), True, taken
    )
    if axis is not None:
        taken.update(label.id for label in axis.ticks)   # ticks are the chart's, not a category's

    if stacked:
        values = _values_stacked(stacks, depth, axis, scope, baseline, taken)
    elif per_category > 1:
        values = _values_clustered(stacks, per_category, axis, scope, taken)
    else:
        values = _values_single([stack[0] for stack in stacks], axis, scope, True, taken)
    extents = [baseline - min(mark.box.y for mark in stack) for stack in stacks]
    uniform = (max(extents) - min(extents)) <= max(1.0, 0.02 * max(extents))
    if values is None or (uniform and values.attached and not stacked):
        # A row of identical boxes with nothing written on it is a band or a legend, not a chart the
        # engine failed to read — say nothing rather than raise a "possible chart" on every design.
        # Numbers written beside identical boxes (a score column, a grid of cells) are a table:
        # the boxes do not draw them, so they are not a chart either. (Stacks of one height are a
        # 100 % chart; their labels sit inside the segments and are judged there.)
        return None, 0.0, "uniform marks" if uniform else "no numeric labels and no numeric axis"

    bands, slots = _bands(category_lefts, cluster_width, category_pitch)
    picked = _category_row(bands, baseline, scope, taken)
    coverage = sum(1 for slot in slots if picked[slot] is not None) / len(slots)

    series_values: list[list[float | None]] = []
    series_points: list[list[_Mark | None]] = []
    for row, points in zip(values.series, values.points, strict=False):
        slotted: list[float | None] = [None] * len(bands)
        slotted_marks: list[_Mark | None] = [None] * len(bands)
        for index, value in enumerate(row):
            slotted[slots[index]] = value
            slotted_marks[slots[index]] = points[index]
        series_values.append(slotted)
        series_points.append(slotted_marks)

    residual = _median([abs(stack[0].box.y2 - baseline) for stack in stacks])
    structure = _clamp01(0.5 + 0.5 * _clamp01(1.0 - residual / EDGE_TOL_PX)) * _clamp01(
        1.0 - _spread([mark.box.w for mark in family])
    )
    confidence = _clamp01(values.scale_r2) * structure * (0.9 + 0.1 * coverage)
    if confidence < ACCEPT:
        return None, confidence, "confidence below the accept threshold"

    chart_type = "column_stacked" if stacked else "column"
    chart = _assemble(
        scope=scope,
        chart_type=chart_type,
        categories=[label.text if label else "" for label in picked],
        series_values=series_values,
        series_points=series_points,
        marks=list(family),
        values=values,
        category_labels=[label for label in picked if label is not None],
        axis=axis,
        vertical=True,
        bands=bands,
        series_width=cluster_width,
        bar_width=width,
        pitch=category_pitch,
        stacked=stacked,
        confidence=confidence,
    )
    return chart, confidence, ""


# --------------------------------------------------------------------------------- pattern: bars


def _bars(family: Sequence[_Mark], scope: _Scope) -> tuple[_Chart | None, float, str]:
    """Horizontal bars sharing a left edge, plus any wrapped bar that fits the same scale."""
    taken: set[str] = set()
    marks = sorted(family, key=lambda mark: (mark.box.y, mark.box.x))
    left = marks[0].box.x
    height = _median([mark.box.h for mark in marks])
    plot = Box(left, marks[0].box.y, max(mark.box.w for mark in marks), marks[-1].box.y2 - marks[0].box.y)
    axis = _find_axis(scope, plot, False, taken)
    if axis is not None:
        taken.update(label.id for label in axis.ticks)   # ticks are the chart's, not a category's
    values = _values_single(marks, axis, scope, False, taken)
    extents = [mark.box.w for mark in marks]
    uniform = (max(extents) - min(extents)) <= max(1.0, 0.02 * max(extents))
    if values is None or (uniform and values.attached):
        # Identical bars with numbers beside them (render check d1_s04: seven equal cells and a
        # score column) are a table, not a chart — see `_columns`.
        return None, 0.0, "uniform marks" if uniform else "no numeric labels and no numeric axis"

    if values.attached:  # the scale is known from the labels, so a wrapped bar can be checked
        claimed = {mark.id for mark in marks}
        others = [
            mark for mark in scope.marks
            if mark.kind == "rect" and mark.id not in claimed
            and abs(mark.box.h - height) <= EDGE_TOL_PX and abs(mark.box.x - left) > EDGE_TOL_PX
        ]
        row = [v for v in values.series[0] if v is not None]
        for mark, label in _extend_to_scale(marks, others, row, scope, False, taken):
            marks.append(mark)
            row.append(label.value.number)  # type: ignore[union-attr]
            values.attached.append((mark, label))
        order = sorted(range(len(marks)), key=lambda i: (marks[i].box.x, marks[i].box.y))
        marks = [marks[i] for i in order]
        values.series = [[row[i] for i in order]]
        values.points = [list(marks)]

    categories: list[str] = []
    category_labels: list[_Label] = []
    for mark in marks:
        name_label = _category_left(mark, scope, taken) or _category_above(mark, scope, taken)
        if name_label is None:
            categories.append("")
            continue
        taken.add(name_label.id)               # one text names one bar, never two
        category_labels.append(name_label)
        categories.append(name_label.text)
    coverage = sum(1 for name in categories if name) / len(categories)

    structure = _clamp01(1.0 - _spread([mark.box.h for mark in marks]))
    confidence = _clamp01(values.scale_r2) * _clamp01(0.5 + 0.5 * structure) * (0.9 + 0.1 * coverage)
    if confidence < ACCEPT:
        return None, confidence, "confidence below the accept threshold"

    pitch = _median([b.box.y - a.box.y for a, b in zip(marks, marks[1:], strict=False)]) or height * 1.6
    bands = [(mark.box.y - (pitch - height) / 2, mark.box.y + height + (pitch - height) / 2)
             for mark in marks]
    chart = _assemble(
        scope=scope,
        chart_type="bar",
        categories=categories,
        series_values=[list(values.series[0])],
        series_points=[list(marks)],
        marks=marks,
        values=values,
        category_labels=category_labels,
        axis=axis,
        vertical=False,
        bands=bands,
        series_width=height,
        bar_width=height,
        pitch=pitch,
        stacked=False,
        confidence=confidence,
    )
    return chart, confidence, ""


# ---------------------------------------------------------------------------- pattern: waterfall


def _waterfall(family: Sequence[_Mark], scope: _Scope) -> tuple[_Chart | None, float, str]:
    """A column family whose bars float: every step starts where the previous one stopped."""
    taken: set[str] = set()
    stacks = _stacks(family)
    if any(len(stack) != 1 for stack in stacks):
        return None, 0.0, "not a waterfall"
    bars = [stack[0] for stack in stacks]
    baseline = max(bar.box.y2 for bar in bars)
    if abs(bars[0].box.y2 - baseline) > EDGE_TOL_PX:
        return None, 0.0, "not a waterfall"

    steps = ["total"]
    level = bars[0].box.y
    for bar in bars[1:]:
        if abs(bar.box.y2 - baseline) <= EDGE_TOL_PX and abs(bar.box.y - level) <= EDGE_TOL_PX:
            steps.append("total")
        elif abs(bar.box.y2 - level) <= EDGE_TOL_PX:
            steps.append("rise")
            level = bar.box.y
        elif abs(bar.box.y - level) <= EDGE_TOL_PX:
            steps.append("fall")
            level = bar.box.y2
        else:
            return None, 0.0, "not a waterfall"
    if "rise" not in steps and "fall" not in steps:
        return None, 0.0, "not a waterfall"

    top = min(bar.box.y for bar in bars)
    plot = Box(bars[0].box.x, top, bars[-1].box.x2 - bars[0].box.x, baseline - top)
    axis = _find_axis(scope, plot, True, taken)
    if axis is not None:
        taken.update(label.id for label in axis.ticks)   # ticks are the chart's, not a category's
    attached = [_claim(_attach_above(bar, scope, taken, _budget(bars, True)), taken) for bar in bars]
    heights = [bar.box.h for bar in bars]

    pairs: list[tuple[_Mark, _Label]] = []
    if all(label is not None for label in attached):
        numbers = [label.value.number for label in attached]  # type: ignore[union-attr]
        for index, (step, number) in enumerate(zip(steps, numbers, strict=False)):
            if step == "fall" and number > 0:
                numbers[index] = -number
            elif step == "rise" and number < 0:
                return None, 0.0, "a rising step is labelled negative"
        scale_r2 = _scale_r2([abs(n) for n in numbers], heights)
        pairs = list(zip(bars, attached, strict=False))  # type: ignore[arg-type]
    elif axis is not None:
        unit = abs(axis.slope)
        numbers = [h * unit * (-1 if step == "fall" else 1) for h, step in zip(heights, steps, strict=False)]
        scale_r2 = axis.r2
    else:
        return None, 0.0, "no numeric labels and no numeric axis"

    # The chain must add up: a waterfall whose steps miss its own total has not been understood.
    running = 0.0
    agreement = 1.0
    for index, (step, number) in enumerate(zip(steps, numbers, strict=False)):
        if step == "total":
            if index and abs(running) > 1e-9:
                agreement = min(agreement, _clamp01(1.0 - 4.0 * abs(number - running) / abs(running)))
            running = number
        else:
            running += number

    width = bars[0].box.w
    pitch = _median([b.box.x - a.box.x for a, b in zip(bars, bars[1:], strict=False)]) or width
    bands, slots = _bands([bar.box.x for bar in bars], width, pitch)
    picked = _category_row(bands, baseline, scope, taken)
    coverage = sum(1 for slot in slots if picked[slot] is not None) / len(slots)

    slotted: list[float | None] = [None] * len(bands)
    slotted_marks: list[_Mark | None] = [None] * len(bands)
    for index, slot in enumerate(slots):
        slotted[slot] = numbers[index]
        slotted_marks[slot] = bars[index]

    confidence = _clamp01(scale_r2) * agreement * (0.9 + 0.1 * coverage)
    if confidence < ACCEPT:
        return None, confidence, "confidence below the accept threshold"

    values = _Values([list(slotted)], [slotted_marks], pairs, scale_r2)
    chart = _assemble(
        scope=scope,
        chart_type="waterfall",
        categories=[label.text if label else "" for label in picked],
        series_values=[slotted],
        series_points=[slotted_marks],
        marks=list(bars),
        values=values,
        category_labels=[label for label in picked if label is not None],
        axis=axis,
        vertical=True,
        bands=bands,
        series_width=width,
        bar_width=width,
        pitch=pitch,
        stacked=False,
        confidence=confidence,
        extras={EXTRAS_KEY: {"totalIndices": [slots[i] for i, s in enumerate(steps) if s == "total"]}},
        totals=[slots[i] for i, s in enumerate(steps) if s == "total"],
    )
    _role_colours(chart, bars, steps, slots)
    return chart, confidence, ""


def _role_colours(chart: _Chart, bars: Sequence[_Mark], steps: Sequence[str], slots: Sequence[int]) -> None:
    """A recognised waterfall's colours in the order the chart model reads them.

    A single-series waterfall's `colors` is `[increase, decrease, total]` and a role it does not
    name takes that role's default (`chart_model.role_colours`), so the series' one modal colour
    `_assemble` wrote would paint the other roles in the defaults. Each role gets the modal colour
    of its own bars, and only a bar that differs from its role keeps a point colour.
    """
    role_of = {"rise": "increase", "fall": "decrease", "total": "total"}
    fallback = _modal(bar.colour for bar in bars if bar.colour) or "#808080"
    by_role = {
        role: _modal(bar.colour for bar, step in zip(bars, steps, strict=False) if role_of[step] == role and bar.colour)
        or fallback
        for role in ("increase", "decrease", "total")
    }
    overrides = {
        str(slots[index]): bar.colour
        for index, (bar, step) in enumerate(zip(bars, steps, strict=False))
        if bar.colour and bar.colour != by_role[role_of[step]]
    }
    chart.spec["colors"] = [by_role["increase"], by_role["decrease"], by_role["total"]]
    options = chart.spec["options"]
    if overrides:
        options["pointColors"] = {"0": dict(sorted(overrides.items(), key=lambda item: int(item[0])))}
    else:
        options.pop("pointColors", None)


# ----------------------------------------------------------------------- pattern: pie / doughnut


def _arc_of(mark: _Mark) -> dict[str, float]:
    return dict((mark.element.geometry or {}).get("arc") or {})


def _sweep(arc: dict[str, float]) -> float:
    span = (float(arc.get("endDeg", 0.0)) - float(arc.get("startDeg", 0.0))) % 360.0
    return span or 360.0


def _in_sector(point: tuple[float, float], arc: dict[str, float]) -> bool:
    """Is this point inside the arc's annulus sector? (A donut's centre text is not on any slice.)"""
    dx = point[0] - float(arc.get("cx", 0.0))
    dy = point[1] - float(arc.get("cy", 0.0))
    radius = math.hypot(dx, dy)
    r_outer = float(arc.get("rOuter", 0.0))
    r_inner = float(arc.get("rInner", 0.0) or 0.0)
    if not (r_inner - 1.0 <= radius <= r_outer + 1.0):
        return False
    angle = math.degrees(math.atan2(dy, dx)) % 360.0     # clockwise from 3 o'clock, y grows downwards
    return ((angle - float(arc.get("startDeg", 0.0))) % 360.0) <= _sweep(arc)


def _pie(family: Sequence[_Mark], scope: _Scope) -> tuple[_Chart | None, float, str]:
    """Arcs round one centre, plus the full ring that stands for the remainder slice."""
    taken: set[str] = set()
    arcs = [mark for mark in family if mark.kind == "arc"]
    geometry = _arc_of(arcs[0])
    r_outer = float(geometry.get("rOuter", 0.0))
    r_inner = float(geometry.get("rInner", 0.0) or 0.0)
    if r_outer <= 0:
        return None, 0.0, "the arc has no radius"

    ring = None
    for candidate in (mark for mark in family if mark.kind == "ring"):
        stroke_width = float((candidate.element.stroke or {}).get("width") or 0.0)
        mid = candidate.box.w / 2
        if (abs(mid + stroke_width / 2 - r_outer) <= RADIUS_TOL_PX
                and abs(mid - stroke_width / 2 - r_inner) <= RADIUS_TOL_PX):
            ring = candidate
            break

    sweeps = {mark.id: _sweep(_arc_of(mark)) for mark in arcs}
    total_sweep = sum(sweeps.values())
    if total_sweep > 360.0 + PERCENT_TOL * 3.6:
        return None, 0.0, "the slices overlap"
    if ring is None and abs(total_sweep - 360.0) > PERCENT_TOL * 3.6:
        return None, 0.0, "the slices do not close the circle and there is no remainder ring"

    # PowerPoint starts a pie at twelve o'clock and runs clockwise; the IR measures from three.
    def from_twelve(mark: _Mark) -> float:
        return (float(_arc_of(mark).get("startDeg", 0.0)) + 90.0) % 360.0

    ordered = sorted(arcs, key=from_twelve)
    shares = [sweeps[mark.id] / 3.6 for mark in ordered]
    offset = min(from_twelve(ordered[0]), 360.0 - from_twelve(ordered[0]))
    outer = Box(arcs[0].box.cx - r_outer, arcs[0].box.cy - r_outer, 2 * r_outer, 2 * r_outer)

    readings: list[float | None] = []
    names: list[str] = []
    for arc in ordered:
        value, name = _slice_value(arc, scope, taken)
        readings.append(value)
        names.append(name)

    if ring is not None and len(ordered) == 1 and readings[0] is None:
        # One arc, one ring, one percentage written anywhere inside the circle (a "39%" inside the ring).
        lone = [
            label for label in scope.available(taken)
            if label.value is not None and label.value.suffix == "%"
            and outer.x <= label.box.cx <= outer.x2 and outer.y <= label.box.cy <= outer.y2
        ]
        if len(lone) == 1:
            readings[0] = lone[0].value.number  # type: ignore[union-attr]
            names[0] = lone[0].text

    if any(value is None for value in readings):
        return None, 0.0, "a slice has no numeric label"
    values: list[float] = [float(value) for value in readings]  # type: ignore[arg-type]

    if ring is not None:
        if any(label.suffix != "%" for label in _slice_suffixes(ordered, scope, taken)):
            return None, 0.0, "a remainder ring needs percentage labels"
        remainder = 100.0 - sum(values)
        ring_share = 100.0 - sum(shares)
        if remainder <= 0 or abs(remainder - ring_share) > PERCENT_TOL:
            return None, 0.0, "the remainder ring does not match 100 % minus the labelled slices"
        values.append(remainder)
        shares.append(ring_share)
        names.append(_ring_name(ring, scope))

    scale = sum(values)
    if scale <= 0:
        return None, 0.0, "the labelled slices sum to nothing"
    errors = [abs(value / scale * 100.0 - share) for value, share in zip(values, shares, strict=False)]
    if max(errors) > PERCENT_TOL:
        return None, 0.0, "the labels disagree with the drawn angles"

    coverage = sum(1 for name in names if name) / len(names)
    confidence = (
        _clamp01(1.0 - max(errors) / PERCENT_TOL) * _clamp01(1.0 - offset / 30.0) * (0.9 + 0.1 * coverage)
    )
    if confidence < ACCEPT:
        return None, confidence, "confidence below the accept threshold"

    hole = round(100.0 * r_inner / r_outer) if r_inner > 0 else 0
    chart_type = "doughnut" if hole else "pie"
    colours = [mark.colour or "#808080" for mark in ordered]
    if ring is not None:
        colours.append(ring.colour or "#808080")
    spec: dict[str, Any] = {
        "type": chart_type,
        "categories": [name or f"slice {i + 1}" for i, name in enumerate(names)],
        "series": [{"name": "series 1", "values": _round_series(values)}],
        "colors": colours,
        "options": {"legend": False, "dataLabels": False},
    }
    if hole:
        spec["options"]["holeSize"] = hole
    consumed = sorted({mark.id for mark in ordered} | ({ring.id} if ring else set()))
    scope.used.update(consumed)
    return (
        _Chart(spec=spec, confidence=round(confidence, 3), consumed=consumed, frame=outer,
               plot_rect=outer, style={}, scope=scope.key, name=f"{scope.key} {chart_type}",
               anchor_source=str((ordered[0].element.source or {}).get("svg") or scope.key),
               faded=sum(1 for mark in ordered if mark.faded) + (1 if ring and ring.faded else 0)),
        confidence,
        "",
    )


def _slice_value(arc: _Mark, scope: _Scope, taken: set[str]) -> tuple[float | None, str]:
    """The number written for this slice: on the slice itself, or in the legend entry of its colour."""
    geometry = _arc_of(arc)
    for label in scope.available(taken):
        if label.value is None or label.value.suffix not in ("%", ""):
            continue
        if _in_sector((label.box.cx, label.box.cy), geometry):
            return label.value.number, label.text
    for colour, text, _ in scope.legend:
        if arc.colour and colour == arc.colour:
            percent = _percent_in(text)
            if percent is not None:
                return percent, text
            value = parse_value(text)
            if value is not None:
                return value.number, text
    return None, ""


def _slice_suffixes(arcs: Sequence[_Mark], scope: _Scope, taken: set[str]) -> list[Value]:
    """The parsed labels behind the slice values — used to insist on percentages before a remainder."""
    found: list[Value] = []
    for arc in arcs:
        geometry = _arc_of(arc)
        for label in scope.available(taken):
            if label.value is not None and _in_sector((label.box.cx, label.box.cy), geometry):
                found.append(label.value)
                break
        else:
            for colour, text, _ in scope.legend:
                if arc.colour and colour == arc.colour and _percent_in(text) is not None:
                    found.append(Value(number=_percent_in(text) or 0.0, suffix="%"))
                    break
    return found


def _ring_name(ring: _Mark, scope: _Scope) -> str:
    for colour, text, _ in scope.legend:
        if ring.colour and colour == ring.colour:
            return text
    return "remainder"


# --------------------------------------------------------------------------- pattern: line / area


def _line(polys: Sequence[_Mark], scope: _Scope) -> tuple[_Chart | None, float, str]:
    """Polylines in an axes frame with numeric ticks: one series each, values read off the axis."""
    taken: set[str] = set()
    series: list[tuple[_Mark, list[tuple[float, float]]]] = []
    for poly in polys:
        points = [(float(p[0]), float(p[1])) for p in (poly.element.geometry or {}).get("points") or []]
        if len(points) >= MIN_MARKS:
            series.append((poly, points))
    if not series:
        return None, 0.0, "no polyline with enough vertices"
    xs = [round(x, 1) for x, _ in series[0][1]]
    series = [entry for entry in series if [round(x, 1) for x, _ in entry[1]] == xs]

    all_points = [point for _, points in series for point in points]
    plot = Box(min(p[0] for p in all_points), min(p[1] for p in all_points), 0.0, 0.0)
    plot = Box(plot.x, plot.y,
               max(p[0] for p in all_points) - plot.x, max(p[1] for p in all_points) - plot.y)
    axis = _find_axis(scope, plot, True, taken)
    if axis is None:
        return None, 0.0, "no numeric axis"
    taken.update(label.id for label in axis.ticks)

    values = [[axis.value_at(y) for _, y in points] for _, points in series]
    half = (_median([b - a for a, b in zip(xs, xs[1:], strict=False)]) / 2) if len(xs) > 1 else 10.0
    bands = [(x - half, x + half) for x in xs]
    # Categories sit under the *axis*, which on a line chart is below the lowest vertex.
    picked = _category_row(bands, max(plot.y2, axis.position_of(axis.minimum)), scope, taken)
    coverage = sum(1 for label in picked if label is not None) / len(picked)

    markers = [
        mark for mark in scope.marks
        if mark.kind == "dot" and mark.box.w <= MARKER_MAX_PX and mark.box.h <= MARKER_MAX_PX
        and any(abs(mark.box.cx - x) <= 2.0 and abs(mark.box.cy - y) <= 2.0 for x, y in all_points)
    ]
    confidence = _clamp01(axis.r2) * (0.9 + 0.1 * coverage)
    if confidence < ACCEPT:
        return None, confidence, "confidence below the accept threshold"

    chart_type = "line_markers" if markers else "line"
    marks = [poly for poly, _ in series] + markers
    chart = _assemble(
        scope=scope,
        chart_type=chart_type,
        categories=[label.text if label else "" for label in picked],
        series_values=[list(row) for row in values],
        series_points=[[poly] * len(xs) for poly, _ in series],
        marks=marks,
        values=_Values(cast(list[list[float | None]], values), [[poly] * len(xs) for poly, _ in series], [],
                       axis.r2),
        category_labels=[label for label in picked if label is not None],
        axis=axis,
        vertical=True,
        bands=bands,
        series_width=0.0,
        bar_width=0.0,
        pitch=half * 2,
        stacked=False,
        confidence=confidence,
    )
    if chart is not None and markers:
        chart.spec["options"]["markers"] = True
    return chart, confidence, ""


# ----------------------------------------------------------------------------------- spec assembly


def _assemble(
    *,
    scope: _Scope,
    chart_type: str,
    categories: Sequence[str],
    series_values: Sequence[Sequence[float | None]],
    series_points: Sequence[Sequence[_Mark | None]],
    marks: Sequence[_Mark],
    values: _Values,
    category_labels: Sequence[_Label],
    axis: _Axis | None,
    vertical: bool,
    bands: Sequence[tuple[float, float]],
    series_width: float,
    bar_width: float,
    pitch: float,
    stacked: bool,
    confidence: float,
    extras: dict[str, Any] | None = None,
    totals: list[int] | None = None,
) -> _Chart:
    """Turn a recognised family into a spec, and record which elements the chart now owns.

    `totals` (a waterfall's total bars, as category indices) becomes `options.totals`, so the
    emitter and the preview read the bars the drawing showed rather than inferring them (WP-C §6).
    """
    colours, point_colours = _series_colours(series_points)
    names = _series_names(series_points, scope)

    series: list[dict[str, Any]] = [
        {"name": names[index] or f"series {index + 1}", "values": _round_series(list(row))}
        for index, row in enumerate(series_values)
    ]

    options: dict[str, Any] = {"legend": False, "gridlines": False}
    if values.attached:
        options["dataLabels"] = True
        options["labelPosition"] = _modal(
            _label_position(mark, label, vertical) for mark, label in values.attached
        ) or "outEnd"
        number_format = _number_format([label.value for _, label in values.attached if label.value])
        if number_format:
            options["numberFormat"] = number_format
    else:
        options["dataLabels"] = False

    if bar_width > 0 and pitch > series_width:
        options["gapWidth"] = round(100.0 * (pitch - series_width) / bar_width)
    if bar_width > 0:
        options["overlap"] = 100 if stacked else 0
    if point_colours:
        options["pointColors"] = point_colours
    options["categoryAxis"] = {"visible": bool(category_labels)}
    if not vertical:
        # The categories are listed top-down, as the design reads; a bar chart draws its first
        # category at the bottom unless the axis is reversed (the emitter then crosses at max).
        options["categoryAxis"]["reverse"] = True
    if totals is not None:
        options["totals"] = sorted(int(index) for index in totals)

    consumed = {mark.id for mark in marks}
    consumed |= {label.id for _, label in values.attached}
    consumed |= {label.id for label in category_labels}

    plot_rect: Box | None
    if axis is not None:
        consumed |= {label.id for label in axis.ticks}
        value_axis: dict[str, Any] = {
            "min": round(axis.minimum, 3), "max": round(axis.maximum, 3), "visible": True,
        }
        if axis.major_unit:
            value_axis["majorUnit"] = round(axis.major_unit, 3)
        options["valueAxis"] = value_axis
        low, high = axis.position_of(axis.minimum), axis.position_of(axis.maximum)
        if vertical:
            plot_rect = Box(bands[0][0], min(low, high), bands[-1][1] - bands[0][0], abs(high - low))
        else:
            plot_rect = Box(min(low, high), bands[0][0], abs(high - low), bands[-1][1] - bands[0][0])
    else:
        # No axis: the brief's fallback — the marks' own extent box.
        plot_rect = _union([mark.box for mark in marks])

    frame_boxes = [mark.box for mark in marks]
    frame_boxes += [label.box for _, label in values.attached]
    frame_boxes += [label.box for label in category_labels]
    if axis is not None:
        frame_boxes += [label.box for label in axis.ticks]
    frame = _union(frame_boxes)
    if plot_rect is not None:
        frame = frame.union(plot_rect)

    scope.used.update(consumed)
    style = _style_of(list(category_labels) + [label for _, label in values.attached])
    return _Chart(
        spec={
            "type": chart_type,
            "categories": list(categories),
            "series": series,
            "colors": colours,
            "options": options,
        },
        confidence=round(confidence, 3),
        consumed=sorted(consumed),
        frame=frame,
        plot_rect=plot_rect,
        style=style,
        scope=scope.key,
        name=f"{scope.key} {chart_type}",
        anchor_source=str((marks[0].element.source or {}).get("svg") or scope.key),
        faded=sum(1 for mark in marks if mark.faded),
        extras=dict(extras or {}),
    )


def _series_colours(
    series_points: Sequence[Sequence[_Mark | None]],
) -> tuple[list[str], dict[str, dict[str, str]]]:
    """A colour per series, plus `pointColors` for the points that break the pattern."""
    colours: list[str] = []
    point_colours: dict[str, dict[str, str]] = {}
    for index, points in enumerate(series_points):
        base = _modal(mark.colour for mark in points if mark is not None) or "#808080"
        colours.append(base)
        overrides = {
            str(position): mark.colour
            for position, mark in enumerate(points)
            if mark is not None and mark.colour and mark.colour != base
        }
        if overrides:
            point_colours[str(index)] = overrides
    return colours, point_colours


def _series_names(series_points: Sequence[Sequence[_Mark | None]], scope: _Scope) -> list[str | None]:
    """Name each series from the legend key whose colour belongs to that series and no other.

    "Belongs to this series alone" survives designs that recolour a forecast point: a client slide paints
    the forecast segments blue and the earlier ones charcoal, so the series' modal colour
    is not the one in the legend, but blue still appears in no other series.

    Two guards keep a key from naming the wrong thing. A single-series chart is never named from a
    legend — a key exists to tell series apart, so with one series it is someone else's. And the key
    has to sit next to this figure: a client slide draws a doughnut, its legend, and a separate bar row
    inside one `<svg>`, and the doughnut's grey key must not label the bars.
    """
    names: list[str | None] = [None] * len(series_points)
    if len(series_points) < 2:
        return names
    region = _union([mark.box for points in series_points for mark in points if mark is not None])
    near = [
        (colour, text) for colour, text, swatch in scope.legend
        if (region.x - 0.5 * region.w <= swatch.cx <= region.x2 + 0.5 * region.w
            and region.y - 0.5 * region.h <= swatch.cy <= region.y2 + 0.5 * region.h)
    ]
    colour_sets = [
        {mark.colour for mark in points if mark is not None and mark.colour} for points in series_points
    ]
    for index, colours in enumerate(colour_sets):
        owned = {c for c in colours if sum(1 for other in colour_sets if c in other) == 1}
        for colour, text in near:
            if colour in owned:
                names[index] = text
                break
    return names


def _number_format(values: Sequence[Value]) -> str | None:
    """Reproduce the way the design wrote its numbers ("~41%", "+150") as an Excel format code."""
    if not values:
        return None
    suffixes = {value.suffix for value in values}
    prefix = '"~"' if all(value.approx for value in values) else ""
    decimals = max(value.decimals for value in values)
    body = "0" + ("." + "0" * decimals if decimals else "")
    suffix = f'"{sorted(suffixes)[0]}"' if len(suffixes) == 1 and sorted(suffixes)[0] else ""
    positives = [value for value in values if value.number > 0]
    if positives and all(value.signed for value in positives):
        # A waterfall writes "+150" for a rise and "-30" for a fall; three sections say exactly that.
        # Only when *every* positive was signed — otherwise a "250" base would come back as "+250".
        return (f'{prefix}"+"{body}{suffix};{prefix}"-"{body}{suffix};{prefix}{body}{suffix}')
    return f"{prefix}{body}{suffix}"


def _style_of(labels: Sequence[_Label]) -> dict[str, Any]:
    """The design's text style for the labels the chart will redraw."""
    runs = [run for run in (_first_run(label.element) for label in labels) if run]
    if not runs:
        return {}
    style: dict[str, Any] = {}
    font = _modal(run.get("font") for run in runs)
    size = _modal(run.get("sizePx") for run in runs)
    colour = _modal(run.get("color") for run in runs)
    if font:
        style["font"] = str(font)
    if size:
        style["sizePx"] = float(size)
    if colour:
        style["color"] = str(colour).upper()
    return style


# ---------------------------------------------------------------------------------- scope walking


def _recognise_scope(ir: IR, scope: _Scope) -> list[_Chart]:
    rects = [mark for mark in scope.marks if mark.kind == "rect"]
    polys = [mark for mark in scope.marks if mark.kind == "poly"]

    families: list[tuple[int, int, str, list[_Mark]]] = []
    for family in _column_families(rects):
        families.append((_PRIORITY["column"], min(m.z for m in family), "column", family))
    for family in _bar_families(rects):
        families.append((_PRIORITY["bar"], min(m.z for m in family), "bar", family))
    for family in _arc_families(scope.marks):
        families.append((_PRIORITY["pie"], min(m.z for m in family), "pie", family))
    if polys:
        families.append((_PRIORITY["line"], min(m.z for m in polys), "line", polys))

    charts: list[_Chart] = []
    for _, _, kind, family in sorted(families, key=lambda entry: (entry[0], entry[1])):
        if any(mark.id in scope.used for mark in family):
            continue
        if kind == "column":
            chart, confidence, reason = _columns(family, scope)
            if chart is None and reason == "not a column family":
                chart, confidence, reason = _waterfall(family, scope)
        elif kind == "bar":
            chart, confidence, reason = _bars(family, scope)
        elif kind == "pie":
            chart, confidence, reason = _pie(family, scope)
        else:
            chart, confidence, reason = _line(family, scope)

        if chart is not None:
            charts.append(chart)
            if chart.faded:
                # A spec colour is #RRGGBB: the design's 55 %-opacity forecast bars come back solid.
                ir.add_diagnostic(
                    "info", chart.anchor_source,
                    f"{chart.faded} chart point(s) were drawn semi-transparent; "
                    f"the chart uses their solid colour",
                )
        else:
            _refuse(ir, family, kind, confidence, reason)
    return charts


#: Structural mismatches: the family was never chart-shaped, so saying so would be noise.
_SILENT = {
    "not a waterfall", "not a column family", "uniform marks", "no polyline with enough vertices",
    "the arc has no radius", "the slices overlap", "overlapping columns of the same width",
}


def _refuse(ir: IR, family: Sequence[_Mark], kind: str, confidence: float, reason: str) -> None:
    """Say why a chart-shaped figure stayed as shapes. Never silent about a real refusal (§10.9)."""
    if reason in _SILENT:
        return
    first = min(family, key=lambda mark: mark.z)
    source = (first.element.source or {}).get("svg") or (first.element.source or {}).get("path") or "svg"
    if confidence >= SUSPECT:
        ir.add_diagnostic(
            "warn", str(source),
            f"possible chart ({kind}, confidence {confidence:.2f}) kept as shapes: {reason}",
            first.id,
        )
    elif "no numeric" in reason:
        ir.add_diagnostic(
            "info", str(source),
            f"possible chart, no numeric labels: {len(family)} {kind} marks kept as shapes",
            first.id,
        )
    else:
        ir.add_diagnostic(
            "info", str(source), f"possible chart, not recognised: {reason}", first.id,
        )


# ------------------------------------------------------------------------------------- the splice


def _splice(ir: IR, charts: Sequence[_Chart]) -> IR:
    """Replace each chart's consumed elements with one `chart`; leave everything else where it was."""
    ordered = ir.paint_sorted()
    position = {element.id: index for index, element in enumerate(ordered)}
    consumed: set[str] = set()
    anchors: dict[str, _Chart] = {}
    for chart in charts:
        consumed.update(chart.consumed)
        anchors[min(chart.consumed, key=lambda eid: position.get(eid, 0))] = chart

    scope_of = {element.id: _scope_key(element) for element in ordered}
    made: dict[str, Element] = {}

    elements: list[Element] = []
    for element in ordered:
        anchored = anchors.get(element.id or "")
        if anchored is not None:
            # The chart takes the paint slot of the first element it swallowed, so everything the
            # design drew later — legend, annotations, reference lines — still lands on top of it.
            new = Element(
                kind="chart",
                box=anchored.frame,
                id=element.id,
                name=anchored.name,
                group=element.group,
                source=dict(element.source or {}),
                spec=anchored.spec,
                style=anchored.style,
                origin="recognised",
                confidence=anchored.confidence,
                plotRect=anchored.plot_rect,
                overlay=[],
                extras=dict(anchored.extras),
            )
            made[element.id or ""] = new
            elements.append(new)
        if element.id in consumed:
            continue
        elements.append(element)

    for index, element in enumerate(elements):
        element.z = index

    # Overlay: whatever the design still draws *after* a chart, out of the same svg — the legend, the
    # annotations, the reference lines, the stack totals. The chart cannot express them, so they stay
    # exactly where they were and are named here so the emitter and the report know they belong to it.
    # When one svg holds two charts, each leftover goes to the nearer of the two.
    by_scope: dict[str | None, list[Element]] = {}
    for anchor_id, new in made.items():
        by_scope.setdefault(scope_of.get(anchor_id), []).append(new)
    for element in elements:
        if element.kind == "chart":
            continue
        nearby = by_scope.get(scope_of.get(element.id))
        if not nearby:
            continue
        owner = min(nearby, key=lambda chart: (_gap(element.box, chart.box), chart.z or 0))
        if (element.z or 0) > (owner.z or 0) and owner.overlay is not None:
            owner.overlay.append(element.id or "")

    ir.elements = elements
    _prune_groups(ir)
    return ir


def _prune_groups(ir: IR) -> None:
    """Drop groups that lost every member — an empty `grpSp` is not something to emit."""
    while True:
        occupied = {element.group for element in ir.elements if element.group}
        parents = {group.parent for group in ir.groups if group.parent}
        alive = [group for group in ir.groups if group.id in occupied or group.id in parents]
        if len(alive) == len(ir.groups):
            return
        ir.groups = alive


# ------------------------------------------------------------------------------------ entry point


def recognise_charts(ir: IR) -> IR:
    """Return the IR with recognised SVG charts replaced by `chart` elements."""
    charts: list[_Chart] = []
    for scope in _scopes(ir):
        charts.extend(_recognise_scope(ir, scope))
    if not charts:
        return ir
    return _splice(ir, charts)
