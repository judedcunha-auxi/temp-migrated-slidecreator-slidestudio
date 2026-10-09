"""One chart model: every rule that reads a `data-chart` spec, once.

A `data-chart` spec used to be read by three readers with three vocabularies — the validator
(`engine/charts_spec.py`), the emitter (`engine/emit/charts.py`) and the app's preview
(`web/public/chart-preview.js`) — so a spec that drew one way in the app drew another way in the file.
Now all three, plus the structural check (`engine/verify/coverage.py`) and the recogniser's hand-over,
consume **this model** and never the raw spec. `chart-preview.js` carries a line-for-line
transliteration of this module (`ChartPreview.normalise` / `ChartPreview.layout`), and
`engine/fixtures/charts/parity-specs.json` with `engine/tests/test_chart_parity.py` is the proof the
two agree (WP-C §2, `docs/archive/engine/fidelity/12-WPC-charts.md`).

Two steps, both pure:

* `normalise(spec) -> model` — no geometry. Type, numbers, series, colours, options, the pinned
  axis and the waterfall plan, plus a `notes` list recording every coercion in order.
* `layout(model, w_px, h_px, size_px) -> layout` — the frame-dependent part: the default plot
  rectangle (gutters from a deterministic text estimate, their constants measured in PowerPoint in
  WP-C C3), the axis ticks, the legend's box and whether the chart is pinned (`c:manualLayout` is
  written only for pinned charts).

Rules for the transliteration (they are why some code below looks more careful than Python needs):

* **Standard library only**, and nothing whose result depends on the language: no `round()` (half-
  even in Python, half-up in JS) — `js_round` is JS `Math.round`; no `math.log10`/`**` for powers
  of ten — `_pow10` multiplies; no `str.strip()` (Unicode whitespace differs) — `_trim` strips
  ASCII whitespace only; no `repr` of a value in a note — `show()` prints what JS would; no
  `toFixed` in JS — `_fixed` rounds the shortest decimal string half-up, as Excel does.
* The model is JSON-round-trip stable: dict keys are strings, numbers are finite floats (indices
  stay ints), no tuples. `-0.0` never appears.
* Sorting: every sorted list here holds ints or ASCII strings; JS sorts ints with `(a, b) => a - b`.

Note kinds (`model["notes"]`): **advisory** — a lenient spelling `charts_spec.validate` accepts (lint
shows it as a warning, `charts_spec.advisories` returns it); **rejected** — `validate` errors on it
and the model ignores the item (the default applies); **warn** — `validate` errors on it and the
model still applies a reading, so the emitter warns; **info** — a fact about the drawing (an
out-of-range connector omitted), neither a spelling nor an error.

The frozen API (end of WP-C C1, 2026-09-25) — C2's twin and C3's measurement build on exactly this:

    normalise(spec) -> {
      "type": str | None, "requested": str | None, "reason": str | None,
      "family": "column"|"bar"|"line"|"area"|"pie"|"doughnut"|"scatter"|"waterfall"|None,
      "base": str | None, "stacked": "" | "stacked" | "stacked100",
      "categories": [str],
      "series": [{"name", "values", "x", "y", "types", "dash", "lineWidth", "smooth", "marker",
                  "markerSize", "explosion", "line"}],
      "colors": [hex | None],
      "options": {every key of _OPTION_ORDER},              # normalise_options
      "notes": [{"kind": "advisory"|"rejected"|"warn"|"info", "text": str}],
      "axis": {"min", "max", "majorUnit"} | None,           # pinned: waterfalls; others given min+max
      "waterfall": plan | None,                             # waterfall_plan
      "overlay": bool,                                      # outEnd labels / reference lines (C3)
    }
    plan = {"roles", "values", "levels", "tops", "bottoms", "totals", "baseValue", "negative",
            "series": [{"name", "values", "visible", "side": "+"|"-", "source": k | None}],
            "segments": [{"category", "source", "lo", "hi", "role", "parts": [{"series", "cell"}],
                          "label": {"series", "point", "text": str | None, "sign", "format"},
                          "hide": [[series, point]]}],
            "connectors": [{"index", "level"}], "colors": {role: hex} | None,
            "formats": {"increase", "decrease", "total"}}
    layout(model, w_px, h_px, size_px) -> {"sizePx", "ticks", "plotArea": {"x","y","w","h"},
                                           "pinned": bool, "gutters": {"left","top","right","bottom"},
                                           "legend": {"x","y","w","h"} | None}          # C3

and the helpers `resolve_type_name`, `family_of`, `label_position_rule`, `coerce_number`,
`normalise_colour`, `point_color_table`, `normalise_options`, `totals_of`, `waterfall_plan`,
`role_colours`, `nice_range`, `axis_range`, `axis_ticks`, `format_number`, `general_number`,
`general_format_for(series)`, `signed_format`, `default_plot_area`, `js_number_string`, `js_round`,
`show`, with the tables in `__all__`. Additive to WP-C §2: `requested`, `family`, `base`, `stacked`;
the plan's `values`, `formats` and each label's `format` (a native label's text is
`format_number(cell, label.format)`); the `info` note kind; `NUMBER_RANGES`.

Changed by C3 (PowerPoint measurement, `docs/archive/engine/fidelity/12-WPC-charts.md` §9): `overlay` no
longer counts a waterfall's connectors — the emitter draws them as a series of the chart, so they
pin nothing; `layout` gained `legend` (`legend_box`, in `__all__`), and the gutter rules take the
measured constants (`AXIS_LINE_EM` and its neighbours below).

Changed by the render check (`docs/engine/rendercheck/00-PLAN.md` §2, 2026-09-29): each series
carries `labels` (`series[k].dataLabels`, a text per point or None — #6), and a waterfall plan
carries `texts` (the single series' texts for `outEnd` labels) and `inkRoles` (the roles painted in
the chart's ink — #5); a 100 % chart's value axis is read in percent (`is_percent_axis`, #3) and
its ticks print as the fractions the file holds (`tick_text`); a waterfall's colours follow
`role_colours`' one/two/three-colour rules and its last bar is also a total when its category or
the authored axis says so (`_inferred_totals`, #5).

Changed by the render check's follow-up (G24, 2026-09-29, `rendercheck/00-PLAN.md` §8–§9): the
options carry `labelStyle` (`{bold, color, fontSize}`, `_label_style`) and each axis a `bold`
flag; a `\\n` in a label text or a category name breaks it onto a new line (`text_lines`; the
gutters grow by `LINE_PITCH_EM` per extra line); and a waterfall's *middle* bar is inferred to be a
subtotal when it equals the running sum (`subtotal_tolerance`) and its category or the authored
axis says so (`SUBTOTAL_CATEGORY`, `_inferred_totals`).
"""
from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any, cast

# ============================================================================== the type tables

#: The 29 chart types python-pptx writes directly — the keys of `engine.emit.charts.TYPES`
#: (StageFlow's table, which needs `pptx` enums and therefore stays in the emitter). A test asserts
#: the two agree; this is the source every other reader imports.
STAGEFLOW_NAMES: frozenset[str] = frozenset({
    "column", "column_stacked", "column_stacked_100",
    "bar", "bar_stacked", "bar_stacked_100",
    "line", "line_markers", "line_stacked", "line_stacked_100",
    "line_markers_stacked", "line_markers_stacked_100",
    "area", "area_stacked", "area_stacked_100",
    "pie", "pie_exploded", "doughnut", "doughnut_exploded",
    "radar", "radar_markers", "radar_filled",
    "scatter", "scatter_lines", "scatter_lines_no_markers", "scatter_smooth",
    "scatter_smooth_no_markers",
    "bubble", "bubble_3d",
})

#: Types reached by building a supported chart and rewriting its XML: name -> the base type it is
#: built from. Mirrors `engine.emit.charts.DERIVED` (name -> (base, converter kwargs)).
DERIVED_BASES: dict[str, str] = {
    "column_3d": "column", "column_3d_clustered": "column",
    "column_3d_stacked": "column_stacked", "column_3d_stacked_100": "column_stacked_100",
    "bar_3d_clustered": "bar", "bar_3d_stacked": "bar_stacked", "bar_3d_stacked_100": "bar_stacked_100",
    "cone_column": "column", "cone_column_clustered": "column",
    "cone_column_stacked": "column_stacked", "cone_column_stacked_100": "column_stacked_100",
    "cone_bar_clustered": "bar", "cone_bar_stacked": "bar_stacked", "cone_bar_stacked_100": "bar_stacked_100",
    "cylinder_column": "column", "cylinder_column_clustered": "column",
    "cylinder_column_stacked": "column_stacked", "cylinder_column_stacked_100": "column_stacked_100",
    "cylinder_bar_clustered": "bar", "cylinder_bar_stacked": "bar_stacked",
    "cylinder_bar_stacked_100": "bar_stacked_100",
    "pyramid_column": "column", "pyramid_column_clustered": "column",
    "pyramid_column_stacked": "column_stacked", "pyramid_column_stacked_100": "column_stacked_100",
    "pyramid_bar_clustered": "bar", "pyramid_bar_stacked": "bar_stacked",
    "pyramid_bar_stacked_100": "bar_stacked_100",
    "line_3d": "line", "area_3d": "area", "area_3d_stacked": "area_stacked",
    "area_3d_stacked_100": "area_stacked_100", "pie_3d": "pie", "pie_3d_exploded": "pie_exploded",
    "surface": "column", "surface_wireframe": "column", "surface_top_view": "column",
    "surface_top_view_wireframe": "column",
    "pie_of_pie": "pie", "bar_of_pie": "pie",
    "stock_hlc": "line", "stock_ohlc": "line", "stock_vhlc": "line", "stock_vohlc": "line",
}
DERIVED_NAMES: frozenset[str] = frozenset(DERIVED_BASES)

#: Types this engine adds on top of StageFlow's list, and the python-pptx type each is built from.
ENGINE_BASES: dict[str, str] = {"waterfall": "column_stacked", "combo": "column"}
ENGINE_NAMES: frozenset[str] = frozenset(ENGINE_BASES)

#: Every name the resolver accepts as written.
KNOWN_NAMES: frozenset[str] = STAGEFLOW_NAMES | DERIVED_NAMES | ENGINE_NAMES

#: Other spellings of a known name — StageFlow's own table plus the ones models write. The whole
#: leniency: anything outside `KNOWN_NAMES` and this table is refused, in the file and the preview.
#: `grouped_bar` is deliberately **not** here (probe p08 B: an unknown name fails visibly in both;
#: the hint names `bar`).
ALIASES: dict[str, str] = {
    # StageFlow
    "col": "column", "bars": "bar", "donut": "doughnut", "donut_exploded": "doughnut_exploded",
    "xy": "scatter", "column_100": "column_stacked_100", "bar_100": "bar_stacked_100",
    "3d_column": "column_3d", "3d_bar": "bar_3d_clustered", "3d_pie": "pie_3d",
    "3d_line": "line_3d", "3d_area": "area_3d", "stock": "stock_hlc",
    "waterfall_column": "waterfall",
    # WP-C §2.1
    "clustered_bar": "bar", "horizontal_bar": "bar", "hbar": "bar",
    "grouped_column": "column", "clustered_column": "column", "vertical_bar": "column",
    "columns": "column",
    "stacked_column": "column_stacked", "column_stack": "column_stacked",
    "stacked_bar": "bar_stacked", "bar_stack": "bar_stacked",
    "stacked_column_100": "column_stacked_100", "column_percent": "column_stacked_100",
    "stacked_bar_100": "bar_stacked_100",
    "ring": "doughnut", "lines": "line", "scatter_plot": "scatter",
    "bridge": "waterfall", "waterfall_chart": "waterfall",
}

#: Decided (handoff §4 #7): PptxRender draws nothing for these, so they are not chart objects on
#: Path A. Matched by prefix so a StageFlow type added later is blocked too.
PATH_A_UNSUPPORTED_PREFIXES: tuple[str, ...] = ("bubble", "radar", "stock", "surface")
#: Deferred to round 2.
ROUND_2: frozenset[str] = frozenset({"combo"})

# ============================================================================= the option tables

#: Option keys the contract names. `totalIndices` and `connectorColor` are deprecated spellings the
#: preview used to read; they are mapped with an advisory.
OPTION_KEYS: frozenset[str] = frozenset({
    "dataLabels", "labelPosition", "labelFormat", "labelStyle", "numberFormat",
    "gridlines", "legend", "gapWidth", "overlap",
    "valueAxis", "categoryAxis", "secondaryValueAxis",
    "plotArea", "referenceLines", "pointColors",
    "holeSize", "smooth", "markers", "markerSize", "lineWidth", "dash",
    "font", "fontSize", "fontColor", "title", "varyColors", "explosion",
    "totals", "connectors", "baseValue",
    "totalIndices", "connectorColor",
})

#: Keys a `series[]` entry may carry.
SERIES_KEYS: frozenset[str] = frozenset({
    "name", "values", "x", "y", "sizes", "size",
    "dash", "lineWidth", "smooth", "marker", "markers", "markerSize", "explosion", "line",
    "totals", "types",
    "dataLabels",                   # per-point label texts (render check #6)
    "chartType", "axis",            # combo — refused (round 2), but a known key, not a typo
})

AXIS_KEYS: frozenset[str] = frozenset({"min", "max", "majorUnit", "minorUnit", "visible", "format",
                                       "reverse", "title", "bold"})

#: Keys of `options.labelStyle` — the data labels' own text style (render-check follow-up, G24):
#: `bold`, `color` (the labels' ink, in place of the contrast pick) and `fontSize` (px).
LABEL_STYLE_KEYS: frozenset[str] = frozenset({"bold", "color", "fontSize"})

LABEL_POSITIONS: frozenset[str] = frozenset({
    "center", "inEnd", "inBase", "outEnd", "bestFit", "left", "right", "above", "below",
})
#: StageFlow's snake_case spellings, read with an advisory.
LABEL_POSITION_ALIASES: dict[str, str] = {
    "inside_end": "inEnd", "inside_base": "inBase", "outside_end": "outEnd", "best_fit": "bestFit",
}

LEGEND_POSITIONS: frozenset[str] = frozenset({"bottom", "top", "left", "right", "topRight"})

MARKER_STYLES: frozenset[str] = frozenset({
    "none", "auto", "circle", "square", "diamond", "triangle", "x", "star", "dash", "dot", "plus",
})

DASH_NAMES: frozenset[str] = frozenset({
    "solid", "dot", "dash", "lgDash", "dashDot", "lgDashDot", "lgDashDotDot",
    "sysDash", "sysDot", "sysDashDot", "sysDashDotDot",
})

#: The words `series[k].types` may use, and what each means on a waterfall bar.
TYPE_WORDS: dict[str, str] = {
    "total": "total", "subtotal": "total", "sum": "total", "end": "total", "start": "total",
    "increase": "increase", "up": "increase", "rise": "increase", "gain": "increase",
    "decrease": "decrease", "down": "decrease", "fall": "decrease", "loss": "decrease",
    "delta": "step", "step": "step", "change": "step",
}

# ================================================================================== the defaults

#: A waterfall bar's colour by role when `colors` does not name it (`role_colours`: with no colours,
#: or three with a gap, a role takes the default *for that role*). The preview's palette, which the
#: emitter shares.
WATERFALL_COLORS: dict[str, str] = {"increase": "2DB757", "decrease": "E5484D", "total": "516467"}
#: The chart's ink: what a one-colour waterfall paints its totals in (render check #5a). The emitter
#: swaps in the deck theme's second text colour (`tx2`, normally `dk2`) when that colour is dark;
#: the preview cannot see the theme and draws this.
WATERFALL_INK = "1F2937"
#: A last bar whose category reads as one of these words is a total when nothing is declared
#: (render check #5c). ASCII word boundaries and ASCII case folding, so JS reads it alike.
TOTAL_CATEGORY = re.compile(r"\b(total|net|pro[- ]?forma|ending|closing|final|combined)\b", re.IGNORECASE | re.ASCII)
#: A *middle* bar whose category reads as one of these words — `TOTAL_CATEGORY`'s words plus the
#: names a bridge gives its subtotals ("2024 EBITDA", "Gross profit", "Base revenue") — is a total
#: when nothing is declared **and** its value equals the running sum (`_inferred_totals`). The
#: words are broader than the last bar's because the running-sum check is required here, not an
#: alternative: "Revenue synergies" is still a step unless it equals the bridge so far.
SUBTOTAL_CATEGORY = re.compile(
    r"\b(total|net|pro[- ]?forma|ending|closing|final|combined|sub[- ]?totals?|totals|ebitda|ebita|ebit"
    r"|gross[- ]profit|gross[- ]margin|operating[- ]income|operating[- ]profit|revenues?|sales|value"
    r"|base|baseline|opening|starting)\b", re.IGNORECASE | re.ASCII)
#: How close a middle bar must be to the running sum to be read as a subtotal: 0.5 % of it, or half
#: a unit of the label format's last digit, whichever is larger (render check, d1_s03).
SUBTOTAL_TOLERANCE = 0.005
GRIDLINES_DEFAULT: dict[str, Any] = {"color": "E4E9F0", "width": 0.75, "dash": None}
CONNECTORS_DEFAULT: dict[str, Any] = {"color": "8A9699", "width": 0.75, "dash": [3.0, 2.0]}
REFERENCE_LINE_DEFAULT: dict[str, Any] = {"color": "516467", "width": 1.0, "dash": [4.0, 3.0]}
DEFAULT_GAP_WIDTH = 60.0
#: The chart text size when the element carries none: 11 pt, PowerPoint's own default.
DEFAULT_SIZE_PX = 14.667
#: The invisible series a waterfall floats on, and the suffix of the below-zero copies.
BASE_SERIES = "Base"
MINUS = "−"

#: Every option the model carries, in this order, with `None` where the author gave nothing.
_OPTION_ORDER: tuple[str, ...] = (
    "dataLabels", "labelPosition", "numberFormat", "labelFormat", "labelStyle", "gridlines", "legend",
    "gapWidth", "overlap", "valueAxis", "categoryAxis", "plotArea", "pointColors", "totals",
    "baseValue", "connectors", "referenceLines", "fontSize", "font", "fontColor", "title",
    "holeSize", "smooth", "markers", "markerSize", "lineWidth", "dash", "varyColors", "explosion",
)

# ================================================================================ small helpers

_WHITESPACE = " \t\n\r\f\v"
#: The whole numeric grammar, in both languages: `"4,200"`, `"12%"`, `"$5.6m"`, `"1_000"`, `""`,
#: `"nan"` are not numbers; `" 6.2 "` and `"1e3"` are.
_NUMBER_RE = re.compile(r"[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?")
_HEX6 = re.compile(r"#?([0-9A-Fa-f]{6})")
_HEX3 = re.compile(r"#([0-9A-Fa-f]{3})")
_HEX8 = re.compile(r"#([0-9A-Fa-f]{6})[0-9A-Fa-f]{2}")
_INDEX_KEY = re.compile(r"[0-9]+")
_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")


def _trim(text: str) -> str:
    """ASCII-whitespace trim — `str.strip()` and JS `trim()` disagree on Unicode spaces."""
    return text.strip(_WHITESPACE)


def _js_keys(mapping: dict[Any, Any]) -> list[Any]:
    """A JSON object's keys in the order JS enumerates them: array-index keys (`"0"`, `"12"`, below
    2^32 − 1) ascending, then the rest in insertion order. Every loop over a spec's object uses it,
    so notes come out in the same order in both languages (`{"5": …, "2": …}` is 2, 5 in JS)."""
    index_keys = [key for key in mapping
                  if isinstance(key, str) and _ARRAY_INDEX.fullmatch(key) and int(key) < 4294967295]
    chosen = set(index_keys)
    return sorted(index_keys, key=int) + [key for key in mapping if key not in chosen]


def _is_real(value: Any) -> bool:
    """A JSON number: int or float, not bool, finite."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _js_len(text: str) -> int:
    """`text.length` as JS counts it (UTF-16 code units)."""
    return len(text.encode("utf-16-le")) // 2


_LINE_BREAK = re.compile(r"\r?\n")


def text_lines(text: str) -> list[str]:
    """A label or category text's lines: `\\n` (or `\\r\\n`) breaks it. The file writes one `a:p`
    per line in a label's rich text and keeps the break in a category's cached text; the preview
    draws one `tspan` per line."""
    return _LINE_BREAK.split(text)


def js_round(value: float) -> float:
    """JS `Math.round`: halves go up (towards +∞). Python's `round` rounds half to even."""
    if not math.isfinite(value):
        return value
    floor = math.floor(value)
    return float(floor + 1) if value - floor >= 0.5 else float(floor)


def _round_to(value: float, places: int) -> float:
    """`Math.round(value * 10^places) / 10^places`, and never `-0.0`."""
    factor = _pow10(places)
    result = js_round(value * factor) / factor
    return 0.0 if result == 0 else result


def _pow10(exponent: int) -> float:
    """10^exponent by repeated multiplication — identical in both languages (no `pow`/`log10`)."""
    result = 1.0
    for _ in range(abs(exponent)):
        result *= 10.0
    return result if exponent >= 0 else 1.0 / result


def _float(value: Any) -> float:
    """A finite float with `-0.0` folded to `0.0` (JSON and JS cannot tell them apart)."""
    result = float(value)
    return 0.0 if result == 0 else result


def _shortest(value: float) -> tuple[str, int]:
    """(digits, n) with value = 0.digits × 10^n — the shortest round-trip digits, as JS prints them.

    `value` is positive and finite. Python's `repr` and JS `String` both produce the shortest
    decimal that round-trips, so the digits agree; only the layout of the string differs.
    """
    text = repr(float(value))
    mantissa, _, exponent = text.partition("e")
    shift = int(exponent) if exponent else 0
    whole, _, fraction = mantissa.partition(".")
    digits = whole + fraction
    stripped = digits.lstrip("0")
    leading = len(digits) - len(stripped)
    n = len(whole) + shift - leading
    return (stripped.rstrip("0") or "0"), n


def js_number_string(value: float) -> str:
    """JS `String(number)` for a finite number: `4.0` -> `"4"`, `1e21` -> `"1e+21"`, `-0` -> `"0"`."""
    if value != value:
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    if value < 0:
        return "-" + js_number_string(-value)
    digits, n = _shortest(value)
    k = len(digits)
    if k <= n <= 21:
        return digits + "0" * (n - k)
    if 0 < n <= 21:
        return digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return "0." + "0" * (-n) + digits
    exponent = n - 1
    sign = "+" if exponent >= 0 else "-"
    head = digits if k == 1 else digits[0] + "." + digits[1:]
    return f"{head}e{sign}{abs(exponent)}"


def show(value: Any) -> str:
    """A value as a note prints it — the same text in both languages (`JSON.stringify` for scalars)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return js_number_string(float(value))
    if isinstance(value, str):
        out = ['"']
        for character in value:
            code = ord(character)
            if character == '"':
                out.append('\\"')
            elif character == "\\":
                out.append("\\\\")
            elif character == "\n":
                out.append("\\n")
            elif character == "\r":
                out.append("\\r")
            elif character == "\t":
                out.append("\\t")
            elif character == "\b":
                out.append("\\b")
            elif character == "\f":
                out.append("\\f")
            elif code < 0x20:
                out.append(f"\\u{code:04x}")
            else:
                out.append(character)
        out.append('"')
        return "".join(out)
    if isinstance(value, list):
        return "a list"
    return "an object"


def _show_list(values: list[Any]) -> str:
    return "[" + ",".join(show(v) for v in values) + "]"


def _note(notes: list[dict[str, str]], kind: str, text: str) -> None:
    notes.append({"kind": kind, "text": text})


def _as_text(value: Any) -> str | None:
    """A category or series name: a string as written, a number as JS prints it, else None."""
    if isinstance(value, str):
        return value
    if _is_real(value):
        return js_number_string(float(value))
    return None


# ======================================================================= rule 1: the type name


def _edit_distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def _hint(name: str) -> str | None:
    """The canonical name an unknown spelling probably meant, or None.

    The closest known name or alias within edit distance 2 (ties: the first in sorted order), else
    the last `_`-separated word of the name that is itself a known name (`grouped_bar` -> `bar`).
    """
    best, best_distance = None, 3
    for candidate in sorted(KNOWN_NAMES | frozenset(ALIASES)):
        distance = _edit_distance(name, candidate)
        if distance < best_distance:
            best, best_distance = candidate, distance
    if best is not None:
        return ALIASES.get(best, best)
    words = name.split("_")
    for index in range(len(words) - 1, -1, -1):
        if words[index] in KNOWN_NAMES and words[index] != name:
            return words[index]
    return None


def resolve_type_name(raw: Any) -> dict[str, Any]:
    """`spec.type` -> `{"name", "requested", "reason"}`; `name` is None when it cannot be drawn.

    `requested` is the author's own spelling (for messages). Order of the tries: exact name; the
    name without a trailing `_chart`/`chart`; that without `_clustered`; then the alias table for
    the same three spellings.
    """
    requested = _as_text(raw) if raw is not None else "column"
    if requested is None:
        requested = show(raw)
    name = requested.lower().replace("-", "_").replace(" ", "_")
    candidates = [name]
    stripped = name
    if stripped.endswith("_chart"):
        stripped = stripped[: -len("_chart")]
    elif stripped.endswith("chart") and len(stripped) > len("chart"):
        stripped = stripped[: -len("chart")]
    if stripped not in candidates:
        candidates.append(stripped)
    unclustered = stripped.replace("_clustered", "")
    if unclustered not in candidates:
        candidates.append(unclustered)

    resolved = None
    for candidate in candidates:
        if candidate in KNOWN_NAMES:
            resolved = candidate
            break
    if resolved is None:
        for candidate in candidates:
            if candidate in ALIASES:
                resolved = ALIASES[candidate]
                break
    if resolved is None:
        hint = _hint(name)
        reason = f"unknown chart type '{requested}'" + (f" (did you mean '{hint}'?)" if hint else "")
        return {"name": None, "requested": requested, "reason": reason}
    if resolved.startswith(PATH_A_UNSUPPORTED_PREFIXES):
        return {"name": None, "requested": requested, "reason": (
            f"chart type '{requested}' is not a chart object on Path A (the renderer draws nothing "
            f"for it); draw it as SVG shapes and text instead — see 03-AUTHORING-CONTRACT.md §Charts")}
    if resolved in ROUND_2:
        return {"name": None, "requested": requested,
                "reason": f"chart type '{requested}' is deferred to round 2 and cannot be emitted yet"}
    return {"name": resolved, "requested": requested, "reason": None}


def family_of(name: str | None) -> dict[str, Any]:
    """`{"family", "base", "stacked"}` for a canonical name (all None/"" for None).

    `family` is one of column, bar, line, area, pie, doughnut, scatter, waterfall; `base` is the
    python-pptx type the chart is built from; `stacked` is "", "stacked" or "stacked100".
    """
    if name is None:
        return {"family": None, "base": None, "stacked": ""}
    base = ENGINE_BASES.get(name) or DERIVED_BASES.get(name) or name
    if name == "waterfall":
        return {"family": "waterfall", "base": base, "stacked": "stacked"}
    stacked = "stacked100" if base.endswith("_stacked_100") else ("stacked" if "_stacked" in base else "")
    for prefix in ("column", "bar", "line", "area", "pie", "doughnut", "scatter"):
        if base.startswith(prefix):
            return {"family": prefix, "base": base, "stacked": stacked}
    return {"family": None, "base": base, "stacked": stacked}


def label_position_rule(family: str | None, stacked: str) -> tuple[frozenset[str], str | None]:
    """Which `labelPosition` values a family accepts, and its default.

    `c:dLblPos` is constrained per chart type in ECMA-376 (a line chart has no `outEnd`, a stacked
    bar no `outEnd`, an area or doughnut chart no position at all), and PowerPoint answers an illegal
    one with a repair that drops content. A waterfall is a stacked column; its `outEnd` is honoured
    as text boxes above/below the bars (WP-C §3.4). Stacked labels are centred (WP-C §0.9).
    """
    if family == "waterfall":
        return frozenset({"center", "inEnd", "inBase", "outEnd"}), "center"
    if family in ("column", "bar"):
        if stacked:
            return frozenset({"center", "inEnd", "inBase"}), "center"
        return frozenset({"center", "inEnd", "inBase", "outEnd"}), "outEnd"
    if family == "line":
        return frozenset({"center", "left", "right", "above", "below"}), "above"
    if family == "scatter":
        return frozenset({"center", "left", "right", "above", "below"}), "right"
    if family == "pie":
        return frozenset({"bestFit", "center", "inEnd", "outEnd"}), "bestFit"
    return frozenset(), None


# ============================================================================== rule 2: numbers


def coerce_number(value: Any) -> tuple[float | None, str]:
    """`(number, how)`: how is "number", "string" (a numeric string, coerced), "gap" (None), "invalid".

    int/float (not bool, finite) -> float; a string whose ASCII-trimmed form matches the grammar ->
    float; None -> None; anything else -> None, "invalid".
    """
    if value is None:
        return None, "gap"
    if _is_real(value):
        return _float(value), "number"
    if isinstance(value, str):
        text = _trim(value)
        if _NUMBER_RE.fullmatch(text):
            number = float(text)
            if math.isfinite(number):
                return _float(number), "string"
    return None, "invalid"


def _number_option(raw: Any, where: str, notes: list[Any], *, kind_bad: str = "rejected",
                   what: str = "ignored") -> float | None:
    """An option that must be a number: coerced with an advisory, ignored (default) with a note."""
    number, how = coerce_number(raw)
    if how == "string":
        _note(notes, "advisory", f"{where}: {show(raw)} read as {js_number_string(cast(float, number))}")
    elif how == "invalid":
        _note(notes, kind_bad, f"{where}: {show(raw)} is not a number — {what}")
    return number


#: What a numeric option may be, in OOXML's own ranges (ST_GapAmount 0–500 %, ST_Overlap −100–100 %,
#: ST_HoleSize 1–90 %, ST_MarkerSize 2–72, ST_LineWidth ≤ 1584 pt — `lineWidth` and every line's
#: `width`; explosion a non-negative %). A value outside is ignored with a note: written as is,
#: python-pptx raises or PowerPoint repairs the file (C1 review #6).
NUMBER_RANGES: dict[str, tuple[float, float]] = {
    "gapWidth": (0.0, 500.0), "overlap": (-100.0, 100.0), "holeSize": (1.0, 90.0),
    "markerSize": (2.0, 72.0), "lineWidth": (0.0, 1584.0), "width": (0.0, 1584.0),
    "explosion": (0.0, 400.0), "fontSize": (0.0, 4000.0),
}

#: The longest dash or gap a `dash` list may give, in px. `a:ds@d`/`@sp` are thousandths of a
#: percent of the line width (an `xs:int`): 1,000 px on the thinnest line the emitter writes stays
#: inside it; a longer one would not (C1 review #6).
DASH_MAX_PX = 1000.0


def _ranged(raw: Any, where: str, notes: list[Any]) -> float | None:
    """A numeric option inside `NUMBER_RANGES` (keyed by the last part of `where`), else None."""
    if raw is None:
        return None
    number = _number_option(raw, where, notes)
    if number is None:
        return None
    low, high = NUMBER_RANGES[where.rsplit(".", 1)[-1]]
    if not low <= number <= high:
        _note(notes, "rejected", f"{where}: {js_number_string(number)} is outside "
                                 f"{js_number_string(low)}..{js_number_string(high)} — ignored")
        return None
    return number


def _number_list(raw: Any, where: str, notes: list[Any]) -> list[float | None] | None:
    """A list of data values: numbers, numeric strings (advisory), gaps; anything else a gap (warn)."""
    if not isinstance(raw, list):
        return None
    out: list[float | None] = []
    for index, value in enumerate(raw):
        number, how = coerce_number(value)
        if how == "string":
            _note(notes, "advisory", f"{where}[{index}]: {show(value)} read as {js_number_string(cast(float, number))}")
        elif how == "invalid":
            _note(notes, "warn", f"{where}[{index}]: {show(value)} is not a number — treated as a gap slot")
        out.append(number)
    return out


# ============================================================================== rule 4: colours


def normalise_colour(value: Any) -> tuple[str | None, str]:
    """`(RRGGBB, how)`: how is "hex", "short" (#RGB expanded), "alpha" (#RRGGBBAA, alpha dropped),
    "absent" (None) or "invalid" (`rgb(…)`, `navy`, `oklch(…)` — no colour)."""
    if value is None:
        return None, "absent"
    if not isinstance(value, str):
        return None, "invalid"
    text = _trim(value)
    found = _HEX6.fullmatch(text)
    if found:
        return found.group(1).upper(), "hex"
    found = _HEX3.fullmatch(text)
    if found:
        short = found.group(1).upper()
        return "".join(c + c for c in short), "short"
    found = _HEX8.fullmatch(text)
    if found:
        return found.group(1).upper(), "alpha"
    return None, "invalid"


def _colour(raw: Any, where: str, notes: list[Any], *, fallback: str = "the default colour is used") -> str | None:
    colour, how = normalise_colour(raw)
    if how == "short":
        _note(notes, "advisory", f"{where}: {show(raw)} read as #{colour}")
    elif how == "alpha":
        _note(notes, "advisory", f"{where}: {show(raw)} read as #{colour} — the alpha is dropped")
    elif how == "invalid":
        _note(notes, "rejected", f"{where}: {show(raw)} is not a #RRGGBB colour — {fallback}")
    return colour


# ========================================================================= rule 3: series, cats


def _series_style(entry: dict[str, Any], where: str, notes: list[Any]) -> dict[str, Any]:
    """The per-series styling keys, canonical (None when absent)."""
    out: dict[str, Any] = {}
    out["dash"] = _dash(entry.get("dash"), f"{where}.dash", notes)
    out["lineWidth"] = _ranged(entry.get("lineWidth"), f"{where}.lineWidth", notes)
    out["smooth"] = _bool_option(entry.get("smooth"), f"{where}.smooth", notes)
    marker = entry.get("marker", entry.get("markers"))
    out["marker"] = _marker(marker, f"{where}.marker", notes)
    out["markerSize"] = _ranged(entry.get("markerSize"), f"{where}.markerSize", notes)
    out["explosion"] = _ranged(entry.get("explosion"), f"{where}.explosion", notes)
    out["line"] = _bool_option(entry.get("line"), f"{where}.line", notes)
    return out


def _bool_option(raw: Any, where: str, notes: list[Any]) -> bool | None:
    if raw is None or isinstance(raw, bool):
        return raw
    _note(notes, "rejected", f"{where}: {show(raw)} is not true or false — ignored")
    return None


def _marker(raw: Any, where: str, notes: list[Any]) -> str | bool | None:
    if raw is None or isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.lower() in MARKER_STYLES:
        return raw.lower()
    _note(notes, "rejected", f"{where}: {show(raw)} is not a marker style — ignored")
    return None


def _dash(raw: Any, where: str, notes: list[Any]) -> str | list[float] | None:
    """A DrawingML preset dash name, or on/off lengths in px on the design."""
    if raw is None:
        return None
    if isinstance(raw, str) and raw in DASH_NAMES:
        return raw
    if isinstance(raw, list) and raw and all(_is_real(v) and 0 < float(v) <= DASH_MAX_PX for v in raw):
        return [_float(v) for v in raw]
    _note(notes, "rejected", f"{where}: {show(raw) if not isinstance(raw, list) else 'that list'} is not a "
                             f"dash (a preset name or px lengths above 0 and at most "
                             f"{js_number_string(DASH_MAX_PX)}) — ignored")
    return None


def _read_series(spec: dict[str, Any], family: str | None, count: int, notes: list[Any]) -> list[dict[str, Any]]:
    raw_series = spec.get("series")
    if not isinstance(raw_series, list):
        if raw_series is not None:
            _note(notes, "rejected", "series must be a list — ignored")
        return []
    xy = family == "scatter"
    series: list[dict[str, Any]] = []
    for index, entry in enumerate(raw_series):
        where = f"series[{index}]"
        if not isinstance(entry, dict):
            _note(notes, "warn", f"{where} is not an object — drawn as an empty series")
            entry = {}
        for key in _js_keys(entry):
            if key not in SERIES_KEYS:
                _note(notes, "rejected", f"{where}: unknown key '{key}' ignored")
        for key in ("chartType", "axis"):
            if key in entry:
                _note(notes, "rejected", f"{where}.{key}: combo series are deferred to round 2 — ignored")

        name = _as_text(entry.get("name")) if entry.get("name") is not None else None
        if name is None:
            if entry.get("name") is not None:
                _note(notes, "warn", f"{where}.name: {show(entry.get('name'))} is not a string — "
                                     f"named 'Series {index + 1}'")
            name = f"Series {index + 1}"

        out: dict[str, Any] = {"name": name, "values": [], "x": None, "y": None, "types": None}
        if xy:
            xs = _number_list(entry.get("x"), f"{where}.x", notes)
            raw_y = entry.get("y") if "y" in entry else entry.get("values")
            ys = _number_list(raw_y, f"{where}.y", notes)
            if xs is None or ys is None:
                _note(notes, "warn", f"{where} needs x and y lists — drawn empty")
                xs, ys = xs or [], ys or []
            if len(xs) != len(ys):
                _note(notes, "warn", f"{where}: x has {len(xs)} entries and y {len(ys)} — the extra "
                                     f"points are dropped")
                length = min(len(xs), len(ys))
                xs, ys = xs[:length], ys[:length]
            out["x"], out["y"], out["values"] = xs, ys, list(ys)
        else:
            values = _number_list(entry.get("values"), f"{where}.values", notes)
            if values is None:
                _note(notes, "warn", f"{where} needs a values list — drawn as gap slots")
                values = []
            if len(values) < count:
                if entry.get("values") is not None or values:
                    _note(notes, "warn", f"{where}.values has {len(values)} entries for {count} "
                                         f"categories — the rest are gap slots")
                values = values + [None] * (count - len(values))
            elif len(values) > count:
                _note(notes, "warn", f"{where}.values has {len(values)} entries for {count} "
                                     f"categories — the extra values are dropped")
                values = values[:count]
            out["values"] = values
        out.update(_series_style(entry, where, notes))
        out["labels"] = _label_texts(entry.get("dataLabels"), f"{where}.dataLabels", len(out["values"]), notes)
        series.append(out)
    return series


def _label_texts(raw: Any, where: str, count: int, notes: list[Any]) -> list[str | None] | None:
    """`series[k].dataLabels`: the text each point's label shows, in place of its formatted value.

    A list, one entry per point: a string is shown as written (`""` shows no label on that point),
    `null` keeps the point's own formatted value, a number is shown as JS prints it (advisory).
    A shorter list leaves the remaining points their values; a longer one is cut (advisory).
    Anything but a list is ignored. Whether a series shows labels at all is still
    `options.dataLabels`. None when the series gives no texts.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        _note(notes, "rejected", f"{where} must be a list of label texts, one per point (null keeps the "
                                 f"value) — ignored")
        return None
    out: list[str | None] = []
    for index, value in enumerate(raw[:count]):
        if value is None or isinstance(value, str):
            out.append(value)
        elif _is_real(value):
            text = js_number_string(float(value))
            _note(notes, "advisory", f"{where}[{index}]: {show(value)} read as the text {show(text)}")
            out.append(text)
        else:
            _note(notes, "rejected", f"{where}[{index}]: {show(value)} is not a label text — the point "
                                     f"keeps its value")
            out.append(None)
    if len(raw) != count:
        tail = "the rest keep their values" if len(raw) < count else "the extra texts are dropped"
        _note(notes, "advisory", f"{where} has {len(raw)} entries for {count} points — {tail}")
    out = out + [None] * (count - len(out))
    return out if any(text is not None for text in out) else None


def _read_categories(spec: dict[str, Any], notes: list[Any]) -> list[str]:
    raw = spec.get("categories")
    if raw is None:
        return []
    if not isinstance(raw, list):
        _note(notes, "rejected", "categories must be a list — ignored")
        return []
    out: list[str] = []
    for index, value in enumerate(raw):
        text = _as_text(value)
        if text is None:
            _note(notes, "warn", f"categories[{index}]: {show(value)} is not a string — read as a gap slot")
            text = ""
        out.append(text)
    return out


# ============================================================================= rule 5: options


def _axis(raw: Any, where: str, notes: list[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"visible": True, "min": None, "max": None, "majorUnit": None,
                           "minorUnit": None, "format": None, "reverse": False, "title": None,
                           "bold": False}
    if raw is None:
        return out
    if isinstance(raw, bool):
        _note(notes, "advisory", f"{where}: {show(raw)} read as {{visible: {show(raw)}}}")
        out["visible"] = raw
        return out
    if not isinstance(raw, dict):
        _note(notes, "rejected", f"{where} must be an object — ignored")
        return out
    for key in _js_keys(raw):
        if key not in AXIS_KEYS:
            _note(notes, "rejected", f"{where}: unknown key '{key}' ignored")
    for key in ("min", "max", "majorUnit", "minorUnit"):
        if raw.get(key) is not None:
            out[key] = _number_option(raw.get(key), f"{where}.{key}", notes)
    for key in ("majorUnit", "minorUnit"):
        if out[key] is not None and out[key] <= 0:
            _note(notes, "rejected", f"{where}.{key} must be above 0 — ignored")
            out[key] = None
    if out["min"] is not None and out["max"] is not None and out["min"] >= out["max"]:
        _note(notes, "rejected", f"{where}.min must be below {where}.max — both ignored")
        out["min"] = out["max"] = None
    for key in ("visible", "reverse", "bold"):
        value = raw.get(key)
        if value is not None:
            if isinstance(value, bool):
                out[key] = value
            else:
                _note(notes, "rejected", f"{where}.{key}: {show(value)} is not true or false — ignored")
    for key in ("format", "title"):
        value = raw.get(key)
        if value is not None:
            if isinstance(value, str):
                out[key] = value
            else:
                _note(notes, "rejected", f"{where}.{key}: {show(value)} is not a string — ignored")
    return out


def is_percent_axis(family: str | None, stacked: str) -> bool:
    """A 100 % stacked chart's value axis: 0–100 in the spec and the model, 0–1 in the file."""
    return stacked == "stacked100" and family in ("column", "bar", "line", "area")


def _percent_axis(axis: dict[str, Any], where: str, notes: list[Any]) -> None:
    """A percent axis is stated in percent (render check #3): `max: 100` is the whole bar. A value
    of at most 1 in size (0 aside) is already a fraction — `max: 1` — and is read as that percent,
    with an advisory; the emitter divides every value by 100 for PowerPoint's 0–1 scale."""
    for key in ("min", "max", "majorUnit", "minorUnit"):
        value = axis[key]
        if value is None or value == 0 or abs(value) > 1:
            continue
        percent = _round_to(value * 100.0, 9)
        _note(notes, "advisory", f"{where}.{key}: {js_number_string(value)} is read as a fraction, "
                                 f"{js_number_string(percent)} % — a 100 % chart's axis is stated in "
                                 f"percent (0–100)")
        axis[key] = percent
    if axis["min"] is not None and axis["max"] is not None and axis["min"] >= axis["max"]:
        _note(notes, "rejected", f"{where}.min must be below {where}.max — both ignored")
        axis["min"] = axis["max"] = None


def _plot_area(raw: Any, notes: list[Any]) -> dict[str, float] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or any(key not in raw for key in ("x", "y", "w", "h")):
        _note(notes, "rejected", "options.plotArea must be an object with x, y, w and h — ignored")
        return None
    values: dict[str, float] = {}
    for key in ("x", "y", "w", "h"):
        number, how = coerce_number(raw[key])
        if number is None:
            _note(notes, "rejected", f"options.plotArea.{key}: {show(raw[key])} is not a number — "
                                     f"plotArea ignored")
            return None
        if how == "string":
            _note(notes, "advisory", f"options.plotArea.{key}: {show(raw[key])} read as "
                                     f"{js_number_string(number)}")
        values[key] = number
    if not (0 <= values["x"] <= 1 and 0 <= values["y"] <= 1 and 0 < values["w"] <= 1 and 0 < values["h"] <= 1):
        _note(notes, "rejected", "options.plotArea values are fractions of the frame in [0, 1] — ignored")
        return None
    if values["x"] + values["w"] > 1.001 or values["y"] + values["h"] > 1.001:
        _note(notes, "rejected", "options.plotArea runs outside the chart frame — ignored")
        return None
    return {key: _round_to(values[key], 4) for key in ("x", "y", "w", "h")}


def _gridlines(raw: Any, notes: list[Any]) -> Any:
    if raw is None or raw is False:
        return False
    if raw is True:
        return dict(GRIDLINES_DEFAULT)
    if not isinstance(raw, dict):
        _note(notes, "rejected", f"options.gridlines: {show(raw)} is not a bool or {{color, width, dash}} — ignored")
        return False
    out = dict(GRIDLINES_DEFAULT)
    for key in _js_keys(raw):
        if key not in ("color", "width", "dash"):
            _note(notes, "rejected", f"options.gridlines: unknown key '{key}' ignored")
    if raw.get("color") is not None:
        out["color"] = _colour(raw["color"], "options.gridlines.color", notes) or GRIDLINES_DEFAULT["color"]
    if raw.get("width") is not None:
        width = _ranged(raw["width"], "options.gridlines.width", notes)
        if width is not None:
            out["width"] = width
    out["dash"] = _dash(raw.get("dash"), "options.gridlines.dash", notes)
    return out


def _legend(raw: Any, authored_series: int, notes: list[Any]) -> Any:
    if raw is None:
        return "bottom" if authored_series > 1 else False
    if raw is False:
        return False
    if raw is True:
        return "bottom"
    if isinstance(raw, str) and raw in LEGEND_POSITIONS:
        return raw
    _note(notes, "rejected", f"options.legend: {show(raw)} is not false or one of bottom, top, left, "
                             f"right, topRight — ignored")
    return "bottom" if authored_series > 1 else False


def _data_labels(raw: Any, count: int, notes: list[Any]) -> list[bool]:
    if raw is None:
        return [True] * count
    if isinstance(raw, bool):
        return [raw] * count
    if isinstance(raw, list) and all(isinstance(v, bool) for v in raw):
        if len(raw) != count:
            _note(notes, "advisory", f"options.dataLabels has {len(raw)} entries for {count} series — "
                                     f"a series without an entry has no labels")
        return [bool(raw[i]) if i < len(raw) else False for i in range(count)]
    _note(notes, "rejected", "options.dataLabels must be a bool or a list of bools, one per series — ignored")
    return [True] * count


def _label_style(raw: Any, notes: list[Any]) -> dict[str, Any]:
    """`options.labelStyle`: `{bold, color, fontSize}` for every data label of the chart.

    `bold` writes `b="1"` on the labels' run properties; `color` is the labels' ink everywhere (in
    place of the contrast pick against each bar); `fontSize` is px, like `options.fontSize`, and
    applies to the labels only. Each key is optional; a bad one is ignored with a note.
    """
    out: dict[str, Any] = {"bold": False, "color": None, "fontSize": None}
    if raw is None:
        return out
    if not isinstance(raw, dict):
        _note(notes, "rejected", "options.labelStyle must be an object {bold, color, fontSize} — ignored")
        return out
    for key in _js_keys(raw):
        if key not in LABEL_STYLE_KEYS:
            _note(notes, "rejected", f"options.labelStyle: unknown key '{key}' ignored")
    bold = raw.get("bold")
    if bold is not None:
        if isinstance(bold, bool):
            out["bold"] = bold
        else:
            _note(notes, "rejected", f"options.labelStyle.bold: {show(bold)} is not true or false — ignored")
    if raw.get("color") is not None:
        out["color"] = _colour(raw["color"], "options.labelStyle.color", notes,
                               fallback="the labels keep their own ink")
    out["fontSize"] = _ranged(raw.get("fontSize"), "options.labelStyle.fontSize", notes)
    return out


def label_size(model: dict[str, Any], size: float) -> float:
    """The data labels' text size in px: `labelStyle.fontSize` (clamped to 5–48, as the chart's own
    size is) when given, else `size`."""
    wanted = model["options"]["labelStyle"]["fontSize"] if model["options"].get("labelStyle") else None
    if wanted is None:
        return size
    return cast(float, min(48.0, max(5.0, wanted)))


def _connectors(raw: Any, legacy_colour: Any, notes: list[Any]) -> Any:
    out = {"color": CONNECTORS_DEFAULT["color"], "width": CONNECTORS_DEFAULT["width"],
           "dash": list(CONNECTORS_DEFAULT["dash"])}
    if raw is False:
        return False
    if raw is not None and raw is not True:
        if not isinstance(raw, dict):
            _note(notes, "rejected", f"options.connectors: {show(raw)} is not false or {{color, width, dash}} — ignored")
        else:
            for key in _js_keys(raw):
                if key not in ("color", "width", "dash"):
                    _note(notes, "rejected", f"options.connectors: unknown key '{key}' ignored")
            if raw.get("color") is not None:
                out["color"] = _colour(raw["color"], "options.connectors.color", notes) or out["color"]
            if raw.get("width") is not None:
                width = _ranged(raw["width"], "options.connectors.width", notes)
                if width is not None:
                    out["width"] = width
            if raw.get("dash") is not None:
                dash = _dash(raw["dash"], "options.connectors.dash", notes)
                if dash is not None:
                    out["dash"] = dash
    if legacy_colour is not None:
        _note(notes, "advisory", "options.connectorColor is the old spelling of options.connectors.color — read as that")
        colour = _colour(legacy_colour, "options.connectorColor", notes)
        if colour is not None and not (isinstance(raw, dict) and raw.get("color") is not None):
            out["color"] = colour
    return out


def _reference_lines(raw: Any, notes: list[Any]) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        _note(notes, "rejected", "options.referenceLines must be a list — ignored")
        return []
    out: list[dict[str, Any]] = []
    for index, line in enumerate(raw):
        where = f"options.referenceLines[{index}]"
        if not isinstance(line, dict):
            _note(notes, "rejected", f"{where} is not an object — left out")
            continue
        for key in _js_keys(line):
            if key not in ("value", "color", "dash", "label", "width"):
                _note(notes, "rejected", f"{where}: unknown key '{key}' ignored")
        value = _number_option(line.get("value"), f"{where}.value", notes, what="the line is left out")
        if value is None:
            if line.get("value") is None:
                _note(notes, "rejected", f"{where} has no value — the line is left out")
            continue
        entry: dict[str, Any] = {
            "value": value,
            "color": REFERENCE_LINE_DEFAULT["color"],
            "width": REFERENCE_LINE_DEFAULT["width"],
            "dash": list(REFERENCE_LINE_DEFAULT["dash"]),
            "label": None,
        }
        if line.get("color") is not None:
            entry["color"] = _colour(line["color"], f"{where}.color", notes) or entry["color"]
        if line.get("width") is not None:
            width = _ranged(line["width"], f"{where}.width", notes)
            if width is not None:
                entry["width"] = width
        if "dash" in line:
            if line["dash"] is None:
                entry["dash"] = None
            else:
                dash = _dash(line["dash"], f"{where}.dash", notes)
                if dash is not None:
                    entry["dash"] = dash
        label = line.get("label")
        if label is not None:
            text = _as_text(label)
            if text is None:
                _note(notes, "rejected", f"{where}.label: {show(label)} is not a string — ignored")
            else:
                entry["label"] = text
        out.append(entry)
    return out


# ========================================================================== rule 6: pointColors


def point_color_table(raw: Any, authored_series: int, category_count: int | None,
                      notes: list[Any]) -> dict[str, dict[str, str]]:
    """`options.pointColors` in its three shapes -> `{"series": {"point": "RRGGBB"}}` (string keys).

    * a list of colours: one colour per point of authored series 0 (a shorter list colours only
      those points);
    * an object whose values are colours: `{point: colour}` for series 0;
    * an object of objects: `{series: {point: colour}}`.

    Keys must be integer-like (`"0"`, `"12"`); others are dropped with a note. The first two shapes
    on a chart with more than one authored series are a validation error, and are still applied to
    series 0 (a `warn` note) so the preview and the file agree.
    """
    if raw is None:
        return {}
    table: dict[str, dict[str, str]] = {}

    def put(series_index: int, point_index: int, value: Any, where: str) -> None:
        if series_index >= max(authored_series, 1) or (category_count is not None and point_index >= category_count):
            _note(notes, "rejected", f"{where} is outside the chart — ignored")
            return
        colour = _colour(value, where, notes, fallback="ignored")
        if colour is not None:
            table.setdefault(str(series_index), {})[str(point_index)] = colour

    def index_of(key: Any) -> int | None:
        if isinstance(key, int) and not isinstance(key, bool) and key >= 0:
            return key
        if isinstance(key, str) and _INDEX_KEY.fullmatch(key):
            return int(key)
        return None

    flat_kind = "warn" if authored_series > 1 else "advisory"
    if isinstance(raw, list):
        _note(notes, flat_kind, "options.pointColors is a list: read as one colour per point of "
                                "series 0" + (" (the chart has several series)" if authored_series > 1 else ""))
        for point, value in enumerate(raw):
            if value is None:
                continue
            put(0, point, value, f"options.pointColors[{point}]")
        return _sorted_table(table)
    if not isinstance(raw, dict):
        _note(notes, "rejected", "options.pointColors must be an object keyed by series index — ignored")
        return {}
    nested = any(isinstance(raw[key], dict) for key in _js_keys(raw))
    if not nested:
        _note(notes, flat_kind, "options.pointColors maps points to colours: read as series 0"
                                + (" (the chart has several series)" if authored_series > 1 else ""))
        for key in _js_keys(raw):
            value = raw[key]
            point_index = index_of(key)
            if point_index is None:
                _note(notes, "rejected", f"options.pointColors: key {show(key)} is not a point index — ignored")
                continue
            put(0, point_index, value, f"options.pointColors[{show(str(key))}]")
        return _sorted_table(table)
    for key in _js_keys(raw):
        points = raw[key]
        series_index = index_of(key)
        if series_index is None:
            _note(notes, "rejected", f"options.pointColors: key {show(key)} is not a series index — ignored")
            continue
        if not isinstance(points, dict):
            _note(notes, "rejected", f"options.pointColors[{show(str(key))}] must be an object keyed by "
                                     f"point index — ignored")
            continue
        for point_key in _js_keys(points):
            value = points[point_key]
            point_index = index_of(point_key)
            if point_index is None:
                _note(notes, "rejected", f"options.pointColors[{show(str(key))}]: key {show(point_key)} is "
                                         f"not a point index — ignored")
                continue
            put(series_index, point_index, value, f"options.pointColors[{show(str(key))}][{show(str(point_key))}]")
    return _sorted_table(table)


def _sorted_table(table: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    return {s: {p: table[s][p] for p in sorted(table[s], key=int)} for s in sorted(table, key=int)}


# ====================================================================== options, all together


def normalise_options(spec: dict[str, Any], kind: dict[str, Any], categories: list[str],
                      series: list[dict[str, Any]], notes: list[Any]) -> dict[str, Any]:
    """The canonical options (WP-C §2.3): every key present, `None` where the author gave nothing."""
    raw = spec.get("options")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        _note(notes, "rejected", "options must be an object — ignored")
        raw = {}
    family, stacked = kind["family"], kind["stacked"]
    waterfall = family == "waterfall"
    for key in _js_keys(raw):
        if key not in OPTION_KEYS:
            _note(notes, "rejected", f"unknown chart option '{key}' ignored")
    if raw.get("secondaryValueAxis") is not None:
        _note(notes, "rejected", "options.secondaryValueAxis belongs to a combo chart (round 2) — ignored")

    out: dict[str, Any] = {key: None for key in _OPTION_ORDER}
    count = len(series)
    out["dataLabels"] = _data_labels(raw.get("dataLabels"), count, notes)

    allowed, default_position = label_position_rule(family, stacked)
    position = raw.get("labelPosition")
    if isinstance(position, str) and position in LABEL_POSITION_ALIASES:
        _note(notes, "advisory", f"options.labelPosition: {show(position)} read as "
                                 f"{show(LABEL_POSITION_ALIASES[position])}")
        position = LABEL_POSITION_ALIASES[position]
    if position is not None and not (isinstance(position, str) and position in LABEL_POSITIONS):
        _note(notes, "rejected", f"options.labelPosition: {show(position)} is not a label position — ignored")
        position = None
    if position is not None and position not in allowed:
        tail = f"; using '{default_position}' instead" if default_position else "; omitted"
        _note(notes, "advisory", f"labelPosition '{position}' is not available on a {kind['name']} chart"
                                 f"{tail} (PowerPoint reports the file as corrupt otherwise)")
        position = default_position
    out["labelPosition"] = position if position is not None else default_position

    for key in ("numberFormat", "labelFormat", "font", "title"):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            _note(notes, "rejected", f"options.{key}: {show(value)} is not a string — ignored")
            value = None
        out[key] = value
    if out["numberFormat"] is None:
        out["numberFormat"] = "General"
    title = spec.get("title")
    if title is not None:
        if isinstance(title, str):
            out["title"] = title
        else:
            _note(notes, "rejected", f"title: {show(title)} is not a string — ignored")
    out["labelStyle"] = _label_style(raw.get("labelStyle"), notes)

    out["gridlines"] = _gridlines(raw.get("gridlines"), notes)
    out["legend"] = _legend(raw.get("legend"), count, notes)

    gap = _ranged(raw.get("gapWidth"), "options.gapWidth", notes)
    out["gapWidth"] = gap if gap is not None else DEFAULT_GAP_WIDTH
    overlap = _ranged(raw.get("overlap"), "options.overlap", notes)
    if overlap is not None and overlap != 100 and stacked and family in ("column", "bar", "waterfall"):
        # A stacked bar's series share one bar: at any other overlap PowerPoint draws them side by
        # side in part-width bars, and a waterfall's connectors cut into them (C1 review #8).
        _note(notes, "advisory", f"options.overlap: the bars of a {kind['name']} chart stack on each "
                                 f"other — {js_number_string(overlap)} read as 100")
        overlap = 100.0
    out["overlap"] = overlap if overlap is not None else (100.0 if stacked else 0.0)

    out["valueAxis"] = _axis(raw.get("valueAxis"), "options.valueAxis", notes)
    if is_percent_axis(family, stacked):
        _percent_axis(out["valueAxis"], "options.valueAxis", notes)
    out["categoryAxis"] = _axis(raw.get("categoryAxis"), "options.categoryAxis", notes)
    out["plotArea"] = _plot_area(raw.get("plotArea"), notes)
    out["pointColors"] = point_color_table(raw.get("pointColors"), count,
                                           None if family == "scatter" else len(categories), notes)

    for key in ("fontSize", "holeSize", "markerSize", "lineWidth", "explosion"):
        out[key] = _ranged(raw.get(key), f"options.{key}", notes)
    if out["holeSize"] is not None and family not in ("pie", "doughnut"):
        _note(notes, "rejected", "options.holeSize has meaning on a pie or doughnut chart only — ignored")
        out["holeSize"] = None
    if raw.get("fontColor") is not None:
        out["fontColor"] = _colour(raw["fontColor"], "options.fontColor", notes)
    for key in ("smooth", "varyColors"):
        out[key] = _bool_option(raw.get(key), f"options.{key}", notes)
    out["markers"] = _marker(raw.get("markers"), "options.markers", notes)
    out["dash"] = _dash(raw.get("dash"), "options.dash", notes)
    out["referenceLines"] = _reference_lines(raw.get("referenceLines"), notes)

    if waterfall:
        base = raw.get("baseValue")
        out["baseValue"] = (_number_option(base, "options.baseValue", notes) if base is not None else None)
        out["connectors"] = _connectors(raw.get("connectors"), raw.get("connectorColor"), notes)
    else:
        for key in ("totals", "totalIndices", "connectors", "connectorColor", "baseValue"):
            if raw.get(key) is not None:
                _note(notes, "rejected", f"options.{key} only has meaning on a waterfall — ignored")
        raw_series: Any = spec.get("series") if isinstance(spec.get("series"), list) else []
        for index, entry in enumerate(raw_series):
            if isinstance(entry, dict):
                for key in ("totals", "types"):
                    if entry.get(key) is not None:
                        _note(notes, "rejected", f"series[{index}].{key} only has meaning on a waterfall — ignored")
    return out


# ================================================================= the waterfall: totals (§3.1)


def _totals_list(raw: Any, where: str, count: int, notes: list[Any]) -> list[int] | None:
    """One totals spelling: ints are category indices, bools are flags, words are bar types.

    `None` when the spelling is unusable — not a list, a list mixing kinds, or a list none of whose
    entries names a bar — so it is not a declaration and the totals are inferred (C1 review #11).
    `[]` is the explicit "no totals".
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        _note(notes, "rejected", f"{where} must be a list of category indices or of true/false flags — ignored")
        return None
    if not raw:
        return []
    flags = [isinstance(v, bool) for v in raw]
    words = [isinstance(v, str) for v in raw]
    if all(flags):
        if len(raw) != count:
            _note(notes, "warn", f"{where} has {len(raw)} flags for {count} categories — read position by position")
        return [index for index, flag in enumerate(raw) if flag and index < count]
    if all(words):
        return _totals_words(raw, where, count, notes)
    if any(flags) or any(words):
        _note(notes, "rejected", f"{where} mixes category indices, true/false flags and words — ignored")
        return None
    indices: list[int] = []
    for position, value in enumerate(raw):
        if _is_real(value) and float(value) == math.floor(float(value)):
            index = int(value)
            if 0 <= index < count:
                indices.append(index)
                continue
        _note(notes, "rejected", f"{where}[{position}]: {show(value)} is not a category index "
                                 f"(0..{count - 1}) — ignored")
    if not indices:
        return None
    if (len(raw) == count and all(_is_real(v) and float(v) in (0.0, 1.0) for v in raw)):
        _note(notes, "advisory", f"{where} {_show_list(raw)} reads as category indices "
                                 f"{_show_list(sorted(set(indices)))}; to mark bars by position write true/false")
    return sorted(set(indices))


def _totals_words(raw: list[Any], where: str, count: int, notes: list[Any]) -> list[int] | None:
    """`options.totals: ["total", "step", …]` — one bar-type word per bar, read like `series.types`
    (an advisory: the contract spells totals as indices). None when no word is a bar type."""
    indices: list[int] = []
    usable = 0
    for position, word in enumerate(raw):
        key = _trim(word).lower()
        if key not in TYPE_WORDS:
            _note(notes, "rejected", f"{where}[{position}]: {show(word)} is not a bar type (total, increase, "
                                     f"decrease, step and their synonyms) — ignored")
            continue
        usable += 1
        if TYPE_WORDS[key] == "total" and position < count:
            indices.append(position)
    if not usable:
        return None
    if len(raw) != count:
        _note(notes, "warn", f"{where} has {len(raw)} words for {count} categories — read position by position")
    _note(notes, "advisory", f"{where} gives a word per bar: read as category indices "
                             f"{_show_list(indices)} — write {where}: {_show_list(indices)}")
    return indices


def _types_list(raw: Any, where: str, count: int, notes: list[Any]) -> list[str | None] | None:
    """`series[k].types`: one word per point -> total/increase/decrease/step/None per category."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        _note(notes, "rejected", f"{where} must be a list of words, one per bar — ignored")
        return None
    out: list[str | None] = []
    for index, word in enumerate(raw):
        if word is None:
            out.append(None)
            continue
        key = _trim(word).lower() if isinstance(word, str) else None
        if key not in TYPE_WORDS:
            _note(notes, "rejected", f"{where}[{index}]: {show(word)} is not a bar type (total, increase, "
                                     f"decrease, step and their synonyms) — ignored")
            out.append(None)
            continue
        out.append(TYPE_WORDS[key])
    if len(out) != count:
        if len(out) > count:
            _note(notes, "warn", f"{where} has {len(out)} entries for {count} categories — the extra entries are ignored")
            out = out[:count]
        else:
            _note(notes, "warn", f"{where} has {len(out)} entries for {count} categories — the rest are undeclared")
            out = out + [None] * (count - len(out))
    return out


def totals_of(spec: dict[str, Any], series: list[dict[str, Any]], count: int,
              base_value: float | None, notes: list[Any], categories: list[str] | None = None,
              value_axis: dict[str, Any] | None = None, label_format: str | None = None) -> dict[str, Any]:
    """Which bars are totals (WP-C §3.1): the union of every declared spelling, else inferred
    (`_inferred_totals`, which reads the categories, the authored value axis and the labels' number
    format). Any declaration switches inference off: `options.totals: [0]` keeps only the first bar
    on the axis, `[]` none.

    Returns `{"totals": [i…], "declared": bool, "types": [[word|None]*count per series]}` and re-signs
    each series' values in place where a direction word contradicts the value's sign.
    """
    raw_options: Any = spec.get("options") if isinstance(spec.get("options"), dict) else {}
    raw_series: Any = spec.get("series") if isinstance(spec.get("series"), list) else []
    union: set[int] = set()
    declared = False
    spellings: list[str] = []

    from_options = _totals_list(raw_options.get("totals"), "options.totals", count, notes)
    if from_options is not None:
        declared = True
        spellings.append("options.totals")
        union.update(from_options)
    if raw_options.get("totalIndices") is not None:
        _note(notes, "advisory", "options.totalIndices is the old spelling of options.totals — read as totals")
        legacy = _totals_list(raw_options.get("totalIndices"), "options.totalIndices", count, notes)
        if legacy is not None:
            declared = True
            spellings.append("options.totalIndices")
            union.update(legacy)

    types: list[list[str | None]] = []
    typed_total_by: dict[int, list[int]] = {}
    typed_step_by: dict[int, list[int]] = {}
    for k, entry in enumerate(raw_series):
        entry = entry if isinstance(entry, dict) else {}
        own = _totals_list(entry.get("totals"), f"series[{k}].totals", count, notes)
        if own is not None:
            declared = True
            if "series.totals" not in spellings:
                spellings.append("series.totals")
            union.update(own)
        words = _types_list(entry.get("types"), f"series[{k}].types", count, notes)
        if words is None:
            words = [None] * count
        types.append(words)
        if k < len(series):
            values = series[k]["values"]
            for i, word in enumerate(words):
                if word == "total":
                    typed_total_by.setdefault(i, []).append(k)
                elif word is not None:
                    typed_step_by.setdefault(i, []).append(k)
                if word in ("increase", "decrease") and i < len(values) and values[i] is not None:
                    value = values[i]
                    signed = abs(value) if word == "increase" else -abs(value)
                    signed = _float(signed)
                    if signed != value:
                        _note(notes, "advisory", f"series[{k}].types[{i}]: '{word}' with a value of "
                                                 f"{js_number_string(value)} — read as {js_number_string(signed)}")
                        values[i] = signed
    if typed_total_by:
        declared = True
        if "series.types" not in spellings:
            spellings.append("series.types")
        union.update(typed_total_by)
    for i in sorted(typed_total_by):
        if i in typed_step_by:
            _note(notes, "advisory", f"series disagree about whether category {i} is a total — read as a total")
    if len(spellings) > 1:
        _note(notes, "advisory", f"totals are given in {len(spellings)} ways ({', '.join(spellings)}) — "
                                 f"say it once: options.totals")

    if declared:
        return {"totals": sorted(union), "declared": True, "types": types}
    if base_value is not None:
        return {"totals": [], "declared": False, "types": types}
    return {"totals": _inferred_totals(series, count, typed_step_by, categories or [], value_axis, label_format),
            "declared": False, "types": types}


def _category_sum(series: list[dict[str, Any]], index: int) -> float | None:
    total, present = 0.0, False
    for entry in series:
        values = entry["values"]
        if index < len(values) and values[index] is not None:
            total += values[index]
            present = True
    return total if present else None


def _inferred_totals(series: list[dict[str, Any]], count: int, typed_steps: dict[int, list[int]],
                     categories: list[str], value_axis: dict[str, Any] | None,
                     label_format: str | None = None) -> list[int]:
    """Nothing declared: the first present bar is a total, and then

    * a **middle** bar is a total (a subtotal: "2024 EBITDA", d1_s03) when its value equals the
      running sum before it — within `subtotal_tolerance` (0.5 % of the sum, or half a unit of the
      label format's last digit) — **and** either its category reads as a subtotal
      (`SUBTOTAL_CATEGORY`) or, drawn as a step, it would end outside the authored value axis
      (above `valueAxis.max` or below `valueAxis.min`) where standing on the axis it stays inside.
      Both are required: a step that happens to equal the bridge so far stays a step. The running
      sum restarts at an inferred subtotal, as it does at any total;
    * the **last** bar is a total when it equals the running sum (within max(1e-9, |sum| × 1e-6)),
      or its category reads as one (`TOTAL_CATEGORY`: "Pro-forma EV", "Net total", "Closing"), or
      drawn as a step it would end above the authored `valueAxis.max` — the author sized the axis
      for it standing on the axis (render check #5c; d1_s06's 255 after a running 101, max 280).

    A bar typed as a step is never inferred to be a total."""
    present = [i for i in range(count) if _category_sum(series, i) is not None]
    if not present:
        return []
    totals: list[int] = []
    if present[0] not in typed_steps:
        totals.append(present[0])
    if len(present) > 1:
        ceiling = value_axis.get("max") if value_axis else None
        floor = value_axis.get("min") if value_axis else None
        half_unit = _half_unit(label_format, series)
        running = _category_sum(series, present[0]) or 0.0
        for middle in present[1:-1]:
            value = _category_sum(series, middle) or 0.0
            if middle not in typed_steps and abs(value - running) <= subtotal_tolerance(running, half_unit):
                named = middle < len(categories) and SUBTOTAL_CATEGORY.search(categories[middle]) is not None
                end = running + value
                leaves = ((ceiling is not None and end > ceiling and value <= ceiling)
                          or (floor is not None and end < floor and value >= floor))
                if named or leaves:
                    totals.append(middle)
                    running = value
                    continue
            running += value
        index = present[-1]
        if index not in typed_steps:
            last = _category_sum(series, index) or 0.0
            equal = abs(last - running) <= max(1e-9, abs(running) * 1e-6)
            named = index < len(categories) and TOTAL_CATEGORY.search(categories[index]) is not None
            overflows = ceiling is not None and running + last > ceiling and last <= ceiling
            if equal or named or overflows:
                totals.append(index)
    return totals


def subtotal_tolerance(running: float, half_unit: float | None) -> float:
    """How far a middle bar may be from the running sum and still read as a subtotal of it:
    `SUBTOTAL_TOLERANCE` (0.5 %) of the sum, or half a unit of the labels' last digit (a bridge
    whose parts are rounded for display, 8.1 against a running 8.08), whichever is larger."""
    return max(1e-9, abs(running) * SUBTOTAL_TOLERANCE, half_unit or 0.0)


def _half_unit(label_format: str | None, series: list[dict[str, Any]]) -> float | None:
    """Half a unit of the last digit the waterfall's labels show — `labelFormat`, else
    `numberFormat`, with `General` read as the plan reads it (`general_format_for`)."""
    fmt = label_format
    if fmt is None or _trim(fmt).lower() == "general":
        fmt = general_format_for(series)
    unit = format_unit(fmt)
    return None if unit is None else unit / 2.0


# =================================================================== the waterfall: plan (§3.2)


def waterfall_plan(model: dict[str, Any], totals: list[int], notes: list[Any]) -> dict[str, Any]:
    """Bars, pieces, cells and labels of a waterfall — PowerPoint's stacking order, not ours.

    A stacked column stacks positive cells upward from zero in series order and negative cells
    downward from zero in series order. So each bar is cut at zero into a positive and a negative
    extent, and the authored series fill those extents outward from zero in authored order, each
    series taking first what remains of the negative side, then of the positive side. Cells on a
    side a bar does not occupy are blank (`None`), never 0 — PowerPoint labels a 0 point. The one
    0 is a zero-height bar's own cell, on the side of its level, so its `+0` label is placed there.

    `connectors` is filled in by `normalise` once the axis is known (`_plan_connectors`).
    """
    categories = model["categories"]
    series = model["series"]
    options = model["options"]
    count = len(categories)
    authored = len(series)
    base_value = options["baseValue"] if options["baseValue"] is not None else 0.0
    total_set = set(totals)

    roles: list[str] = []
    tops: list[float | None] = []
    bottoms: list[float | None] = []
    levels: list[float] = []
    sums: list[float | None] = []
    running = base_value
    for i in range(count):
        present = [series[k]["values"][i] for k in range(authored) if series[k]["values"][i] is not None]
        if not present:
            roles.append("gap")
            tops.append(None)
            bottoms.append(None)
            levels.append(_float(running))
            sums.append(None)
            continue
        value = 0.0
        for number in present:
            value += number
        value = _float(value)
        if any(v > 0 for v in present) and any(v < 0 for v in present):
            _note(notes, "warn", f"category {i} ('{categories[i]}') mixes rises and falls across series — "
                                 f"drawn as the net step from the magnitudes")
        if i in total_set:
            low, high, role = min(0.0, value), max(0.0, value), "total"
            running = value
        else:
            start, end = running, _float(running + value)
            low, high = min(start, end), max(start, end)
            role = "increase" if value >= 0 else "decrease"
            running = end
        roles.append(role)
        bottoms.append(_float(low))
        tops.append(_float(high))
        levels.append(_float(running))
        sums.append(value)
    negative = any(b is not None and b < 0 for b in bottoms)

    # ---- the series layout: [Base, S_0 … S_n−1] (+ [Base−, S_0− … S_n−1−] once any bar dips below 0)
    plan_series: list[dict[str, Any]] = [{"name": BASE_SERIES, "values": [None] * count, "visible": False,
                                          "side": "+", "source": None}]
    for k in range(authored):
        plan_series.append({"name": series[k]["name"], "values": [None] * count, "visible": True,
                            "side": "+", "source": k})
    if negative:
        plan_series.append({"name": BASE_SERIES + MINUS, "values": [None] * count, "visible": False,
                            "side": "-", "source": None})
        for k in range(authored):
            plan_series.append({"name": series[k]["name"] + MINUS, "values": [None] * count, "visible": True,
                                "side": "-", "source": k})
    plus_index = {k: 1 + k for k in range(authored)}
    minus_base = 1 + authored
    minus_index = {k: minus_base + 1 + k for k in range(authored)}

    fmt = options["labelFormat"] or options["numberFormat"]
    if fmt is None or _trim(fmt).lower() == "general":
        fmt = general_format_for(series)
    formats = {"increase": signed_format(fmt, "+"), "decrease": signed_format(fmt, "-"), "total": fmt}
    segments: list[dict[str, Any]] = []

    def custom(k: int, i: int) -> str | None:
        """The author's own text for series k's label at bar i (`series[k].dataLabels`), or None."""
        texts = series[k]["labels"]
        return texts[i] if texts is not None and i < len(texts) else None

    # `outEnd` labels are one per bar, not per series: a single series' texts apply to them.
    out_texts: list[str | None] = [None] * count
    if authored == 1:
        out_texts = [custom(0, i) for i in range(count)]
    elif options["labelPosition"] == "outEnd":
        for k in range(authored):
            if series[k]["labels"] is not None:
                _note(notes, "advisory", f"series[{k}].dataLabels: an outEnd label on a stacked waterfall "
                                         f"shows the whole bar — the per-point texts are not used")

    for i in range(count):
        role = roles[i]
        if role == "gap":
            continue
        low, high = bottoms[i], tops[i]  # type: ignore[assignment]
        sign = "+" if role == "increase" else ("-" if role == "decrease" else "")
        if low == high:
            # A zero-height bar. At or above zero: one 0 cell on the first present series, stacked on
            # `Base` at the level, so its `+0` label sits there. Below zero PowerPoint stacks a 0
            # with the *positive* cells — at the zero line, not at the level (C1 review #9) — so
            # there is no 0 cell: the label is frozen text on `Base−`'s point, whose end is the level.
            level = high
            first = next(k for k in range(authored) if series[k]["values"][i] is not None)
            if level >= 0:
                plan_series[0]["values"][i] = _float(level)
                target = plus_index[first]
                plan_series[target]["values"][i] = 0.0
                segments.append({"category": i, "source": first, "lo": _float(level), "hi": _float(level),
                                 "role": role, "parts": [{"series": target, "cell": 0.0}],
                                 "label": {"series": target, "point": i, "text": None, "sign": sign,
                                           "format": formats[role]},
                                 "hide": []})
            else:
                plan_series[minus_base]["values"][i] = _float(level)
                segments.append({"category": i, "source": first, "lo": _float(level), "hi": _float(level),
                                 "role": role, "parts": [],
                                 "label": {"series": minus_base, "point": i,
                                           "text": format_number(0.0, formats[role]), "sign": sign,
                                           "format": formats[role]},
                                 "hide": []})
            continue
        ext_plus = max(high, 0.0) - max(low, 0.0)
        ext_minus = min(high, 0.0) - min(low, 0.0)
        if high > 0:
            plan_series[0]["values"][i] = _float(max(low, 0.0))
        if low < 0:
            plan_series[minus_base]["values"][i] = _float(min(high, 0.0))
        remaining_minus, remaining_plus = ext_minus, ext_plus
        cursor_plus, cursor_minus = max(low, 0.0), min(high, 0.0)
        for k in range(authored):
            value = series[k]["values"][i]
            if value is None:
                continue
            # A piece that fits takes its value exactly (so a series wholly on one side of zero
            # writes |v| itself, not the difference of two running totals); only a piece cut by
            # the extent takes what remains of it.
            magnitude = abs(value)
            below = magnitude if _fits(magnitude, remaining_minus) else remaining_minus
            remaining_minus = max(0.0, remaining_minus - below)
            rest = magnitude - below
            above = rest if _fits(rest, remaining_plus) else remaining_plus
            remaining_plus = max(0.0, remaining_plus - above)
            parts: list[dict[str, Any]] = []
            lo_k: float | None = None
            hi_k: float | None = None
            if above > 0:
                plan_series[plus_index[k]]["values"][i] = _float(above)
                parts.append({"series": plus_index[k], "cell": _float(above)})
                lo_k, hi_k = cursor_plus, cursor_plus + above
                cursor_plus = cursor_plus + above
            if below > 0:
                plan_series[minus_index[k]]["values"][i] = _float(-below)
                parts.append({"series": minus_index[k], "cell": _float(-below)})
                piece_low, piece_high = cursor_minus - below, cursor_minus
                cursor_minus = cursor_minus - below
                lo_k = piece_low if lo_k is None else min(lo_k, piece_low)
                hi_k = piece_high if hi_k is None else max(hi_k, piece_high)
            if not parts:
                continue
            if len(parts) == 2:
                # Neither part is the step: the part holding the segment's end carries a frozen
                # label (a rise ends above zero, a fall below it); the other part's label goes.
                ending = parts[0] if role != "decrease" else parts[1]
                other = parts[1] if ending is parts[0] else parts[0]
                label = {"series": ending["series"], "point": i,
                         "text": format_number(value, formats[role]), "sign": sign, "format": formats[role]}
                hide = [[other["series"], i]]
            else:
                label = {"series": parts[0]["series"], "point": i, "text": None, "sign": sign,
                         "format": formats[role]}
                hide = []
            segments.append({"category": i, "source": k, "lo": _float(lo_k), "hi": _float(hi_k), "role": role,
                             "parts": parts, "label": label, "hide": hide})

    # The author's own texts replace the formatted step: frozen, like a zero-crossing label.
    for segment in segments:
        text = custom(segment["source"], segment["category"])
        if text is not None:
            segment["label"]["text"] = text

    colours = None
    inked: list[str] = []
    if authored == 1:
        colours = role_colours(model["colors"])
        inked = ink_roles(model["colors"])
    return {
        "roles": roles, "values": sums, "levels": levels, "tops": tops, "bottoms": bottoms,
        "totals": sorted(total_set), "baseValue": _float(base_value), "negative": negative, "series": plan_series,
        "segments": segments, "connectors": [], "colors": colours, "inkRoles": inked, "formats": formats,
        "texts": out_texts,
    }


def _fits(piece: float, room: float) -> bool:
    """`piece <= room`, forgiving the last bits in which two running totals disagree."""
    return piece <= room + 1e-9 * max(1.0, abs(piece), abs(room))


def role_colours(colors: list[str | None]) -> dict[str, str]:
    """A single-series waterfall's colour per role (render check #5, decided 2026-09-29).

    * one colour: rises and falls both take it, and totals take the chart's ink (`WATERFALL_INK`;
      `ink_roles` names them, so a renderer that knows the theme can use its dark `tx2`);
    * two colours: [increase, decrease], and the totals take the first;
    * three: [increase, decrease, total].
    A role still not covered (no colours, or a `None` in its place) takes its own default.
    """
    if len(colors) == 1 and colors[0] is not None:
        return {"increase": colors[0], "decrease": colors[0], "total": WATERFALL_INK}
    out: dict[str, str] = {}
    for position, role in enumerate(("increase", "decrease", "total")):
        chosen = colors[position] if position < len(colors) else None
        if chosen is None and role == "total" and len(colors) == 2:
            chosen = colors[0]
        out[role] = chosen if chosen is not None else WATERFALL_COLORS[role]
    return out


def ink_roles(colors: list[str | None]) -> list[str]:
    """The roles `role_colours` paints in the chart's ink rather than in a colour of the spec."""
    return ["total"] if len(colors) == 1 and colors[0] is not None else []


def _plan_connectors(model: dict[str, Any], plan: dict[str, Any], notes: list[Any]) -> list[dict[str, Any]]:
    """Consecutive non-gap pairs whose level lies inside the pinned value axis."""
    if model["options"]["connectors"] is False:
        return []
    axis = model["axis"]
    roles, levels = plan["roles"], plan["levels"]
    categories = model["categories"]
    out: list[dict[str, Any]] = []
    for i in range(len(roles) - 1):
        if roles[i] == "gap" or roles[i + 1] == "gap":
            continue
        level = levels[i]
        if axis is not None and (level < axis["min"] or level > axis["max"]):
            _note(notes, "info", f"the connector after category {i} ('{categories[i]}') at "
                                 f"{js_number_string(level)} lies outside the value axis "
                                 f"({js_number_string(axis['min'])}..{js_number_string(axis['max'])}) — omitted")
            continue
        out.append({"index": i, "level": level})
    return out


# ======================================================================= axis and number formats


def nice_range(low: float, high: float) -> tuple[float, float, float]:
    """A human axis range covering [low, high]: (min, max, majorUnit), about four steps.

    span/4 rounded up to 1, 2, 2.5, 5 or 10 × a power of ten; min and max floored/ceiled to the
    step. `high <= low` becomes `high = low + 1`. Deterministic, and identical in the JS twin
    (powers of ten by multiplication, not `log10`).
    """
    if high <= low:
        high = low + 1.0
    raw = (high - low) / 4.0
    exponent = 0
    while _pow10(exponent + 1) <= raw:
        exponent += 1
    while _pow10(exponent) > raw:
        exponent -= 1
    magnitude = _pow10(exponent)
    step = magnitude
    for factor in (1.0, 2.0, 2.5, 5.0, 10.0):
        step = magnitude * factor
        if raw <= step:
            break
    return _float(math.floor(low / step) * step), _float(math.ceil(high / step) * step), step


def _data_bounds(model: dict[str, Any]) -> tuple[float, float] | None:
    """(min, max) of what the value axis must show, before 0 is added; None when there is no data."""
    family, stacked = model["family"], model["stacked"]
    series = model["series"]
    if family == "waterfall":
        plan = model["waterfall"]
        lows = [b for b in plan["bottoms"] if b is not None]
        highs = [t for t in plan["tops"] if t is not None]
        if not lows:
            return None
        return min(lows), max(highs)
    values_of: Callable[[dict[str, Any]], Any] = (
        (lambda entry: entry["y"] or []) if family == "scatter" else (lambda entry: entry["values"])
    )
    if stacked and family in ("column", "bar", "line", "area"):
        low = high = None
        for i in range(len(model["categories"])):
            positive = negative = 0.0
            any_positive = any_negative = False
            for entry in series:
                value = entry["values"][i]
                if value is None:
                    continue
                if value >= 0:
                    positive += value
                    any_positive = True
                else:
                    negative += value
                    any_negative = True
            if stacked == "stacked100":
                positive, negative = (100.0 if any_positive else 0.0), (-100.0 if any_negative else 0.0)
            if any_positive or any_negative:
                low = negative if low is None else min(low, negative)
                high = positive if high is None else max(high, positive)
        return None if low is None else cast(tuple[float, float], (low, high))
    numbers = [v for entry in series for v in values_of(entry) if v is not None]
    if not numbers:
        return None
    return min(numbers), max(numbers)


def axis_range(model: dict[str, Any]) -> dict[str, float] | None:
    """The value axis the chart shows: the author's min/max/majorUnit where given, else a nice range
    over the data and 0 (stacked charts sum per sign; waterfalls use the plan's bars). None for the
    families without a value axis (pie, doughnut)."""
    if model["family"] in (None, "pie", "doughnut"):
        return None
    axis = model["options"]["valueAxis"]
    bounds = _data_bounds(model)
    low, high = (0.0, 1.0) if bounds is None else (min(0.0, bounds[0]), max(0.0, bounds[1]))
    given_min, given_max, given_step = axis["min"], axis["max"], axis["majorUnit"]
    low_in = given_min if given_min is not None else low
    high_in = given_max if given_max is not None else high
    if given_step is not None:
        step = given_step
        lo = given_min if given_min is not None else _float(math.floor(low_in / step) * step)
        hi = given_max if given_max is not None else _float(math.ceil(high_in / step) * step)
    else:
        nice_low, nice_high, step = nice_range(low_in, high_in)
        lo = given_min if given_min is not None else nice_low
        hi = given_max if given_max is not None else nice_high
    if hi <= lo:
        hi = _float(lo + step)
    return {"min": _float(lo), "max": _float(hi), "majorUnit": _float(step)}


def axis_ticks(axis: dict[str, float] | None) -> list[float]:
    """min, min + step, … up to max (at most 200), each rounded to 10 decimals."""
    if axis is None:
        return []
    ticks: list[float] = []
    step = axis["majorUnit"]
    k = 0
    while k < 200:
        value = axis["min"] + k * step
        if value > axis["max"] + step * 1e-6:
            break
        ticks.append(_round_to(value, 10))
        k += 1
    return ticks


def general_number(value: float) -> str:
    """Excel's `General`, as the preview has always printed it: 6 decimals at most, JS digits."""
    rounded = js_round(value * 1e6) / 1e6
    return js_number_string(0.0 if rounded == 0 else rounded)


def _split_sections(fmt: str) -> list[str]:
    """`;`-separated sections, respecting quotes and `\\` escapes."""
    sections, current, quoted, index = [], [], False, 0
    while index < len(fmt):
        character = fmt[index]
        if character == "\\" and not quoted and index + 1 < len(fmt):
            current.append(fmt[index:index + 2])
            index += 2
            continue
        if character == '"':
            quoted = not quoted
        if character == ";" and not quoted:
            sections.append("".join(current))
            current = []
        else:
            current.append(character)
        index += 1
    sections.append("".join(current))
    return sections


def _masked(section: str) -> str:
    """The section with every literal character (quoted, escaped, `_x`, `*x`, `[..]`) replaced by
    NUL, position for position — so the digit pattern is only found in real placeholders."""
    out: list[str] = []
    index, quoted = 0, False
    while index < len(section):
        character = section[index]
        if quoted:
            out.append("\0")
            if character == '"':
                quoted = False
            index += 1
            continue
        if character == '"':
            quoted = True
            out.append("\0")
            index += 1
            continue
        if character in "\\_*" and index + 1 < len(section):
            out.append("\0\0")
            index += 2
            continue
        if character == "[":
            close = section.find("]", index)
            if close > index:
                out.append("\0" * (close - index + 1))
                index = close + 1
                continue
        out.append(character)
        index += 1
    return "".join(out)


def _literal(part: str) -> str:
    """Literal text of a format fragment: quotes removed, `\\x` -> x, `_x` -> space, `*x` and
    `[..]` dropped."""
    out: list[str] = []
    index, quoted = 0, False
    while index < len(part):
        character = part[index]
        if quoted:
            if character == '"':
                quoted = False
            else:
                out.append(character)
            index += 1
            continue
        if character == '"':
            quoted = True
            index += 1
            continue
        if character == "\\" and index + 1 < len(part):
            out.append(part[index + 1])
            index += 2
            continue
        if character == "_" and index + 1 < len(part):
            out.append(" ")
            index += 2
            continue
        if character == "*" and index + 1 < len(part):
            index += 2
            continue
        if character == "[":
            close = part.find("]", index)
            if close > index:
                index = close + 1
                continue
        out.append(character)
        index += 1
    return "".join(out)


def _increment(digits: str) -> str:
    """"199" -> "200": a decimal digit string plus one, without big integers."""
    chars = list(digits)
    index = len(chars) - 1
    while index >= 0:
        if chars[index] == "9":
            chars[index] = "0"
            index -= 1
        else:
            chars[index] = str(int(chars[index]) + 1)
            return "".join(chars)
    return "1" + "".join(chars)


def _fixed(value: float, places: int) -> tuple[str, str]:
    """(integer digits, fraction digits) of a non-negative number rounded half-up to `places`
    decimals — on its shortest decimal form, as Excel rounds what it displays (2.675 -> 2.68)."""
    if value == 0:
        return "0", "0" * places
    digits, n = _shortest(value)
    keep = n + places
    if keep < 0:
        scaled = "0"
    elif keep >= len(digits):
        scaled = digits + "0" * (keep - len(digits))
    else:
        scaled = digits[:keep] or "0"
        if digits[keep] >= "5":
            scaled = _increment(scaled)
    scaled = scaled.lstrip("0") or "0"
    scaled = scaled.rjust(places + 1, "0")
    if places:
        return scaled[:-places], scaled[-places:]
    return scaled, ""


_BODY = re.compile(r"[#0][#0,]*(\.[#0]+)?")


def format_number(value: float | None, fmt: str | None) -> str:
    """An Excel number format applied the way a PowerPoint label shows it (WP-C §2.4).

    Section-aware: `positive;negative;zero` — the negative section formats `abs(value)` with no
    automatic minus, the zero section applies to exactly 0; with one section a negative number gets
    a leading `-` (unless it rounds to zero). Body `[#0][#0,]*(\\.[#0]+)?`: `0` digits are
    mandatory, `#` decimals optional (trailing zeros beyond the `0`s are dropped, never a trailing
    dot), a `,` between digits groups thousands, trailing `,`s divide by 1000 each. A bare `%`
    (outside quotes) multiplies by 100. Quoted text, `\\x`, `_x` (a space) pass through as literals;
    `*x` and `[..]` are dropped. `General` (or a section with no digit pattern) is `general_number`.
    """
    if value is None or not math.isfinite(value):
        return ""
    fmt = fmt if isinstance(fmt, str) and fmt else "General"
    sections = _split_sections(fmt)
    minus = False
    if value < 0 and len(sections) >= 2:
        section, magnitude = sections[1], -value
    elif value == 0 and len(sections) >= 3:
        section, magnitude = sections[2], 0.0
    else:
        section, magnitude = sections[0], abs(value)
        minus = value < 0
    masked = _masked(section)
    found = _BODY.search(masked)
    if found is None:
        bare = _trim(masked.replace("\0", ""))
        if bare == "" and section:
            # Only literal text (`"-"`, `"n/a"`): Excel shows the text itself.
            return ("-" if minus else "") + _literal(section)
        text = general_number(magnitude)          # `General`, or a pattern this reader does not know
        return ("-" if minus and text != "0" else "") + text
    body = found.group(0)
    prefix, suffix = section[: found.start()], section[found.end():]
    suffix_masked = masked[found.end():]
    whole, _, decimals = body.partition(".")
    scale = 0
    if not decimals:
        scale += len(whole) - len(whole.rstrip(","))
        whole = whole.rstrip(",")
    lead = len(suffix_masked) - len(suffix_masked.lstrip(","))
    scale += lead
    suffix = suffix[lead:]
    if "%" in masked.replace("\0", ""):
        magnitude = magnitude * 100.0
    for _ in range(scale):
        magnitude = magnitude / 1000.0
    if magnitude >= 1e21:
        number = general_number(magnitude)
    else:
        mandatory = decimals.count("0")
        integer, fraction = _fixed(magnitude, len(decimals))
        while len(fraction) > mandatory and fraction.endswith("0"):
            fraction = fraction[:-1]
        minimum = whole.count("0")
        if len(integer) < minimum:
            integer = integer.rjust(minimum, "0")
        if minimum == 0 and integer == "0":
            integer = ""
        if "," in whole and integer:
            groups: list[str] = []
            while len(integer) > 3:
                groups.insert(0, integer[-3:])
                integer = integer[:-3]
            groups.insert(0, integer)
            integer = ",".join(groups)
        number = integer + ("." + fraction if fraction else "")
    nonzero = any(c in "123456789" for c in number)
    return ("-" if minus and nonzero else "") + _literal(prefix) + number + _literal(suffix)


def format_unit(fmt: str | None) -> float | None:
    """What one unit of the last digit a format shows is worth, read as `format_number` reads the
    first section: `0.0` → 0.1, `0` → 1, `#,##0,` → 1000 (a trailing `,` divides by 1000), `0.0%`
    → 0.001 (a `%` multiplies by 100). None for `General` or a section with no digit pattern."""
    if not isinstance(fmt, str) or not fmt:
        return None
    section = _split_sections(fmt)[0]
    masked = _masked(section)
    found = _BODY.search(masked)
    if found is None:
        return None
    whole, _, decimals = found.group(0).partition(".")
    scale = 0
    if not decimals:
        scale += len(whole) - len(whole.rstrip(","))
    suffix_masked = masked[found.end():]
    scale += len(suffix_masked) - len(suffix_masked.lstrip(","))
    unit = _pow10(-len(decimals))
    for _ in range(scale):
        unit = unit * 1000.0
    if "%" in masked.replace("\0", ""):
        unit = unit / 100.0
    return unit


#: The tick format PowerPoint gives a percent axis, and the one the emitter writes there when the
#: author states none: the author's `numberFormat` is for the data (27.6 → "27.6%"), and on a
#: 0–1 axis it would print "0.3%" for 30 %.
PERCENT_TICK_FORMAT = "0%"


def value_axis_format(model: dict[str, Any]) -> str | None:
    """The value axis's tick format as the file carries it: `valueAxis.format`, else the chart's
    `numberFormat` — or, on a percent axis (whose ticks are fractions in the file), `0%`."""
    options = model["options"]
    given = options["valueAxis"]["format"]
    if is_percent_axis(model["family"], model["stacked"]):
        return given or PERCENT_TICK_FORMAT
    return cast("str | None", given or options["numberFormat"])


def tick_text(model: dict[str, Any], value: float) -> str:
    """A value-axis tick label as PowerPoint prints it. A percent axis's ticks run 0–100 in the
    model and 0–1 in the file, so they are formatted as the fraction."""
    fmt = value_axis_format(model)
    if is_percent_axis(model["family"], model["stacked"]):
        return format_number(_float(value / 100.0), fmt)
    return format_number(value, fmt)


def general_format_for(series: list[dict[str, Any]]) -> str:
    """The waterfall label format when the author gave none: `0` when every authored value (the
    model's `series[k]["values"]`) is whole, else `0.0#` (the old `0.##` printed `"4."` for 4 in
    PowerPoint)."""
    for entry in series:
        for value in entry["values"]:
            if value is not None and value != math.floor(value):
                return "0.0#"
    return "0"


def _literal_plus(section: str) -> tuple[int, int] | None:
    """(start, end) of the first literal `+` in a format section — bare, quoted or `\\+` — else None.

    A `+` straight after `E`/`e` is scientific notation's exponent sign, not a literal.
    """
    index, quoted = 0, False
    while index < len(section):
        character = section[index]
        if quoted:
            if character == '"':
                quoted = False
            elif character == "+":
                return index, index + 1
            index += 1
            continue
        if character == '"':
            quoted = True
            index += 1
            continue
        if character == "\\" and index + 1 < len(section):
            if section[index + 1] == "+":
                return index, index + 2
            index += 2
            continue
        if character == "+" and not (index > 0 and section[index - 1] in "Ee"):
            return index, index + 1
        index += 1
    return None


def signed_format(fmt: str, sign: str) -> str:
    """The format a waterfall step's label is written in, chosen by the step's direction (`sign`).

    A step's cell is not the step: it is a magnitude on whichever side of zero the piece is drawn
    (a fall above zero is a positive cell, a rise below zero a negative one), so the sign comes from
    the role, never from the cell, and the format prints the same text on either side of zero:

    * one section `S`: `"+"S;"+"S` for a rise, `"-"S;"-"S` for a fall — Excel's negative section
      prints the absolute value, so a falling cell of +15 above zero and one of −15 below it both
      read `-15`. A literal `+` in `S` is the author's own rise sign: a rise keeps `S;S`, and a
      fall drops the `+` and takes `"-"`.
    * two or more sections `S1;S2[;S3]`: a rise `S1;S1[;S3]`, a fall `S2;S2[;S3]` — the author's
      own positive and negative presentations (`#,##0;(#,##0)` reads `(30)` for every fall).
    * no sign (a total): the author's format as written.

    The frozen text of a zero-crossing segment is `format_number(step, signed_format(fmt, sign))`,
    so a native label and a frozen one on the same chart always print alike (C1 review #5).
    """
    if not sign:
        return fmt
    sections = _split_sections(fmt)
    if len(sections) >= 2:
        chosen = sections[0] if sign == "+" else sections[1]
        tail = ";" + sections[2] if len(sections) >= 3 else ""
        return f"{chosen};{chosen}{tail}"
    section = sections[0]
    plus = _literal_plus(section)
    if plus is not None:
        if sign == "+":
            return f"{section};{section}"
        body = section[: plus[0]] + section[plus[1]:]
        return f'"-"{body};"-"{body}'
    return f'"{sign}"{section};"{sign}"{section}'


# ================================================================================== normalise


def _blank_model(reason: str, notes: list[Any]) -> dict[str, Any]:
    return {
        "type": None, "requested": None, "reason": reason, "family": None, "base": None, "stacked": "",
        "categories": [], "series": [], "colors": [], "options": {key: None for key in _OPTION_ORDER},
        "notes": notes, "axis": None, "waterfall": None, "overlay": False,
    }


def normalise(spec: Any) -> dict[str, Any]:
    """The one reading of a `data-chart` spec (WP-C §2.1). Never raises; never carries geometry.

    Rules in order, each adding a note when it changes anything: 1 the type; 2 numbers (inside
    3–6); 3 series and categories; 4 colours; 5 options; 6 pointColors; 7 the defaults, the pinned
    axis, the waterfall plan and whether anything is drawn on top of the frame (`overlay`).
    """
    notes: list[dict[str, str]] = []
    if not isinstance(spec, dict):
        return _blank_model("chart spec must be an object", notes)

    # 1 -- the type
    if spec.get("type") is None:
        _note(notes, "warn", "the chart has no type — drawn as a column chart")
    resolved = resolve_type_name(spec.get("type"))
    kind = family_of(resolved["name"])
    kind["name"] = resolved["name"]
    if resolved["name"] is not None and resolved["requested"] != resolved["name"] and spec.get("type") is not None:
        _note(notes, "advisory", f"chart type '{resolved['requested']}' read as '{resolved['name']}' — "
                                 f"write the type name as listed")

    # 3 -- series and categories (2, numbers, inside)
    categories = _read_categories(spec, notes) if kind["family"] != "scatter" else []
    series = _read_series(spec, kind["family"], len(categories), notes)
    if kind["family"] in ("pie", "doughnut") and len(series) > 1:
        _note(notes, "warn", f"a {resolved['name']} chart takes a single series — only series[0] is drawn")
        series = series[:1]

    # 4 -- colours, keeping positions
    colors: list[str | None] = []
    raw_colors = spec.get("colors")
    if raw_colors is not None:
        if isinstance(raw_colors, list):
            colors = [_colour(value, f"colors[{index}]", notes) for index, value in enumerate(raw_colors)]
        else:
            _note(notes, "rejected", "colors must be a list — ignored")

    model: dict[str, Any] = {
        "type": resolved["name"], "requested": resolved["requested"], "reason": resolved["reason"],
        "family": kind["family"], "base": kind["base"], "stacked": kind["stacked"],
        "categories": categories, "series": series, "colors": colors,
        "options": {key: None for key in _OPTION_ORDER}, "notes": notes,
        "axis": None, "waterfall": None, "overlay": False,
    }
    if resolved["name"] is None:
        return model

    # 5, 6 -- options, pointColors, defaults
    options = normalise_options(spec, kind, categories, series, notes)
    model["options"] = options

    if not series:
        model["type"] = None
        model["reason"] = "chart spec needs a non-empty series list"
        return model
    if kind["family"] != "scatter" and not categories:
        model["type"] = None
        model["reason"] = "chart spec needs a non-empty categories list"
        return model

    # 7 -- the waterfall plan, the axis, the overlay
    if kind["family"] == "waterfall":
        found = totals_of(spec, series, len(categories), options["baseValue"], notes, categories,
                          options["valueAxis"], options["labelFormat"] or options["numberFormat"])
        options["totals"] = found["totals"]
        model["waterfall"] = waterfall_plan(model, found["totals"], notes)
    axis = axis_range(model)
    if kind["family"] == "waterfall":
        model["axis"] = axis
        model["waterfall"]["connectors"] = _plan_connectors(model, model["waterfall"], notes)
        _tiny_steps(model, notes)
    elif axis is not None and options["valueAxis"]["min"] is not None and options["valueAxis"]["max"] is not None:
        model["axis"] = axis
    model["overlay"] = _overlay(model, notes)
    return model


def _tiny_steps(model: dict[str, Any], notes: list[Any]) -> None:
    """Every step below 2 % of the axis span: the bridge reads as a flat line."""
    plan, axis = model["waterfall"], model["axis"]
    if axis is None:
        return
    span = axis["max"] - axis["min"]
    steps = [abs((t or 0.0) - (b or 0.0)) for r, t, b in zip(plan["roles"], plan["tops"], plan["bottoms"], strict=False)
             if r in ("increase", "decrease")]
    if steps and span > 0 and all(step < 0.02 * span for step in steps):
        _note(notes, "advisory", "every step is below 2 % of the value axis — set valueAxis.min to zoom the bridge")


def _overlay(model: dict[str, Any], notes: list[Any]) -> bool:
    """Whether shapes are drawn on top of the frame: a waterfall's `outEnd` labels, reference lines.

    A waterfall's connectors are not an overlay: the file draws them as a series of the chart
    itself (WP-C §9's fallback, taken on C3's PowerPoint measurement), so they follow the plot
    wherever PowerPoint lays it out and pin nothing."""
    options = model["options"]
    family = model["family"]
    drawn = False
    if family == "waterfall":
        if options["labelPosition"] == "outEnd" and any(options["dataLabels"]):
            drawn = True
    lines = options["referenceLines"]
    if lines:
        if family in ("pie", "doughnut"):
            _note(notes, "rejected", "options.referenceLines need a value axis — a pie or doughnut chart has none")
        elif model["axis"] is None:
            _note(notes, "advisory", "options.referenceLines need options.valueAxis.min and max to be placed — "
                                     "the lines are left out")
        else:
            drawn = True
    return drawn


# ===================================================================================== layout


def _estimate(text: str, size: float) -> float:
    """Deterministic text width in px — `0.55 em` per character, in place of canvas measureText; a
    text broken by `\\n` is as wide as its widest line."""
    widest = 0.0
    for line in text_lines(text):
        widest = max(widest, 0.55 * size * _js_len(line))
    return widest


#: What PowerPoint needs around a pinned inner plot rect before it moves or shrinks it — measured
#: 2026-09-25 with PowerPoint COM on `out/c3/calib.pptx` (WP-C C3; PlotArea.Inside* against the
#: written c:manualLayout, 32 charts): a line of axis labels below the plot takes 17.5 px or less at
#: 9 px text, 22.4 at 11, 26.2 at 13, 31.7 at 16 (≈ 2.0 em + 0.5 px); the top tick label needs
#: 6.7 px above the plot at 11 px (0.61 em); the value-axis labels take their own width plus 12.2 /
#: 14.1 / 16.9 px at 11 / 13 / 16 px (≈ 0.95 em + 1.7 px); an end category label wider than its slot
#: needs half the excess beside the plot. A pinned rect that leaves that room is kept to 0.1 px; one
#: that does not is moved or shrunk by PowerPoint and not by PptxRender (C1 review #7). The
#: constants round each measurement up — a gutter too wide costs a few px of plot, too narrow costs
#: the overlay its alignment.
AXIS_LINE_EM = 2.05
AXIS_LINE_PX = 1.0
HALF_LABEL_EM = 0.7
TICK_GAP_EM = 1.0
TICK_GAP_PX = 2.0
#: A legend row at the chart's text size: 21 px at 11 px text (PowerPoint, the same run), 1.9 em.
LEGEND_ROW_EM = 1.9
#: A vertical legend's entry pitch, and the space between a legend and what it sits beside.
LEGEND_PITCH_EM = 1.6
LEGEND_GAP_EM = 0.3
#: The pitch of a second (third, …) line of chart text — a category or a label broken by `\n` —
#: which the gutter holding that text grows by.
LINE_PITCH_EM = 1.2


def _labels_outside(model: dict[str, Any]) -> bool:
    options = model["options"]
    if not any(options["dataLabels"]):
        return False
    return options["labelPosition"] in ("outEnd", "above")


def _label_lines(model: dict[str, Any]) -> int:
    """The most lines any drawn label text runs to (`series[k].dataLabels` broken by `\\n`); 1 when
    every label is one line. A waterfall's `outEnd` labels read the plan's texts."""
    options = model["options"]
    labelled = options["dataLabels"]
    texts: list[str | None] = []
    if model["family"] == "waterfall" and model["waterfall"] is not None:
        if any(labelled):
            texts = list(model["waterfall"]["texts"])
    else:
        for k, entry in enumerate(model["series"]):
            if k < len(labelled) and labelled[k] and entry["labels"] is not None:
                texts.extend(entry["labels"])
    lines = 1
    for text in texts:
        if text is not None:
            lines = max(lines, len(text_lines(text)))
    return lines


def _category_lines(model: dict[str, Any]) -> int:
    """The most lines a category name runs to (broken by `\\n`); 1 for one-line names."""
    lines = 1
    for category in model["categories"]:
        lines = max(lines, len(text_lines(category)))
    return lines


def _legend_names(model: dict[str, Any]) -> list[str]:
    """What a legend lists: a pie's categories, else the authored series' names."""
    if model["family"] in ("pie", "doughnut"):
        return list(model["categories"])
    return [entry["name"] for entry in model["series"]]


def _legend_size(model: dict[str, Any], w: float, h: float, size: float) -> tuple[float, float, int]:
    """(width, height, rows) of the legend box, in px. Entries sit in equal columns, as PowerPoint
    spreads them across a pinned legend; a horizontal legend wider than the frame wraps into rows."""
    names = _legend_names(model)
    widest = 0.0
    for name in names:
        widest = max(widest, _estimate(name, size))
    entry = size * 1.3 + widest + 6.0
    count = max(1, len(names))
    if model["options"]["legend"] in ("left", "right", "topRight"):
        return entry, size * (LEGEND_PITCH_EM * count + LEGEND_GAP_EM), count
    room = max(entry, w - 8.0)
    per_row = max(1, int(math.floor(room / entry)))
    rows = (count + per_row - 1) // per_row
    return min(room, entry * min(count, per_row)), size * (LEGEND_ROW_EM + (rows - 1) * LEGEND_PITCH_EM), rows


def _end_overflow(label: str, slot: float, size: float) -> float:
    """How far a category label centred on an end slot reaches past the plot's edge."""
    return max(0.0, (_estimate(label, size) - slot) / 2.0)


def default_plot_area(model: dict[str, Any], w: float, h: float, size: float, ticks: list[float]) -> dict[str, Any]:
    """The plot rect the preview draws and a pinned chart writes, from gutters sized to the text the
    chart will draw and to the room PowerPoint needs around it (WP-C §2.2; the constants above are
    measured). Returns `{"plotArea", "gutters"}`."""
    options = model["options"]
    family = model["family"]
    pad = 4.0
    legend = options["legend"]
    gap = size * LEGEND_GAP_EM
    legend_w, legend_h, _rows = _legend_size(model, w, h, size) if legend else (0.0, 0.0, 0)
    top = pad + (size * 1.15 * 1.6 if options["title"] else 0.0) + (legend_h + gap if legend == "top" else 0.0)
    bottom = pad + (legend_h + gap if legend == "bottom" else 0.0)
    left = pad + (legend_w + gap if legend == "left" else 0.0)
    right = pad + (legend_w + gap if legend in ("right", "topRight") else 0.0)
    value_visible = options["valueAxis"]["visible"]
    category_visible = options["categoryAxis"]["visible"]
    outside = _labels_outside(model)
    line = size * AXIS_LINE_EM + AXIS_LINE_PX
    tick_gap = size * TICK_GAP_EM + TICK_GAP_PX
    tick_texts = [tick_text(model, tick) for tick in ticks]
    widest_tick = 0.0
    for text in tick_texts:
        widest_tick = max(widest_tick, _estimate(text, size))
    categories = model["categories"]
    labels_px = label_size(model, size)

    if family in ("column", "waterfall", "line", "area", "scatter"):
        if value_visible:
            left += widest_tick + tick_gap
        if category_visible and (family == "scatter" or categories):
            bottom += line + (_category_lines(model) - 1) * size * LINE_PITCH_EM
            if family == "scatter":
                last = tick_texts[-1] if tick_texts else ""
                right = max(right, pad + _estimate(last, size) / 2.0)
            else:
                slot = max(1.0, w - left - right) / max(1, len(categories))
                left = max(left, pad + _end_overflow(categories[0], slot, size))
                right = max(right, pad + _end_overflow(categories[-1], slot, size))
        if outside:
            top += labels_px * 1.25 + (_label_lines(model) - 1) * labels_px * LINE_PITCH_EM
        if value_visible:
            top = max(top, pad + size * HALF_LABEL_EM)
    elif family == "bar":
        if outside:
            right += labels_px * 2.2
        if value_visible:
            bottom += line
            right = max(right, pad + (_estimate(tick_texts[-1], size) / 2.0 if tick_texts else 0.0))
        if category_visible:
            widest = 0.0
            for category in categories:
                widest = max(widest, _estimate(category, size))
            left += widest + tick_gap
    left = min(left, 0.6 * w)
    bottom = min(bottom, 0.6 * h)
    area = {
        "x": _round_to(left / w, 4), "y": _round_to(top / h, 4),
        "w": _round_to(max(1.0, w - left - right) / w, 4), "h": _round_to(max(1.0, h - top - bottom) / h, 4),
    }
    return {"plotArea": area, "gutters": {"left": left, "top": top, "right": right, "bottom": bottom}}


def legend_box(model: dict[str, Any], w: float, h: float, size: float, area: dict[str, float]) -> dict[str, float] | None:
    """Where the legend of a chart laid out at `area` goes — fractions of the frame, 4 dp — or None.

    A pinned chart writes this box as `c:legend/c:layout/c:manualLayout` (PowerPoint keeps it to the
    px, measured with the plot rect), so its legend sits where the preview draws it: a bottom legend
    on the frame's bottom pad, a top one under the title, a side one centred on the plot."""
    legend = model["options"]["legend"]
    if not legend or model["type"] is None or w <= 0 or h <= 0:
        return None
    pad = 4.0
    legend_w, legend_h, _rows = _legend_size(model, w, h, size)
    title = size * 1.15 * 1.6 if model["options"]["title"] else 0.0
    if legend == "bottom":
        x, y = (w - legend_w) / 2.0, h - pad - legend_h
    elif legend == "top":
        x, y = (w - legend_w) / 2.0, pad + title
    else:
        x = pad if legend == "left" else w - pad - legend_w
        if legend == "topRight":
            y = pad + title
        else:
            y = (area["y"] + area["h"] / 2.0) * h - legend_h / 2.0
    x, y = max(0.0, x), max(0.0, y)
    return {"x": _round_to(x / w, 4), "y": _round_to(y / h, 4),
            "w": _round_to(min(legend_w, w - x) / w, 4), "h": _round_to(min(legend_h, h - y) / h, 4)}


def layout(model: dict[str, Any], w_px: float, h_px: float, size_px: float | None) -> dict[str, Any]:
    """The geometry of a model in a frame of `w_px` × `h_px` at text size `size_px` (WP-C §2.2).

    `sizePx` is `options.fontSize` when given, else `size_px` (the element's computed font size in
    the browser, `style.sizePx` in the emitter), clamped to [5, 48]. `plotArea` is the author's when
    given (4 dp fractions), else the default rect; `pinned` says whether the file carries it as a
    `c:manualLayout` (the author gave one, or something is drawn on top of the frame).
    """
    options = model["options"]
    size = options["fontSize"] if options.get("fontSize") is not None else size_px
    size = DEFAULT_SIZE_PX if size is None else float(size)
    size = min(48.0, max(5.0, size))
    w, h = float(w_px), float(h_px)
    ticks = axis_ticks(axis_range(model)) if model["type"] is not None else []
    pinned = model["type"] is not None and (options.get("plotArea") is not None or bool(model["overlay"]))
    if model["type"] is None or w <= 0 or h <= 0:
        return {"sizePx": size, "ticks": ticks, "plotArea": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
                "pinned": False, "gutters": {"left": 0.0, "top": 0.0, "right": 0.0, "bottom": 0.0},
                "legend": None}
    if options.get("plotArea") is not None:
        area = dict(options["plotArea"])
        gutters = {"left": area["x"] * w, "top": area["y"] * h,
                   "right": w - (area["x"] + area["w"]) * w, "bottom": h - (area["y"] + area["h"]) * h}
    else:
        default = default_plot_area(model, w, h, size, ticks)
        area, gutters = default["plotArea"], default["gutters"]
    return {"sizePx": size, "ticks": ticks, "plotArea": area, "pinned": pinned, "gutters": gutters,
            "legend": legend_box(model, w, h, size, area)}


# ================================================================================= public list

__all__ = [
    "NUMBER_RANGES", "normalise", "layout", "resolve_type_name", "family_of", "label_position_rule", "coerce_number",
    "normalise_colour", "point_color_table", "normalise_options", "totals_of", "waterfall_plan",
    "role_colours", "ink_roles", "is_percent_axis", "value_axis_format", "tick_text",
    "WATERFALL_INK", "TOTAL_CATEGORY", "PERCENT_TICK_FORMAT",
    "SUBTOTAL_CATEGORY", "SUBTOTAL_TOLERANCE", "LABEL_STYLE_KEYS", "LINE_PITCH_EM", "subtotal_tolerance",
    "format_unit", "text_lines", "label_size",
    "nice_range", "axis_range", "axis_ticks", "format_number", "general_number",
    "general_format_for", "signed_format", "default_plot_area", "legend_box", "js_number_string", "js_round", "show",
    "STAGEFLOW_NAMES", "DERIVED_BASES", "DERIVED_NAMES", "ENGINE_BASES", "ENGINE_NAMES", "KNOWN_NAMES",
    "ALIASES", "PATH_A_UNSUPPORTED_PREFIXES", "ROUND_2", "OPTION_KEYS", "SERIES_KEYS", "AXIS_KEYS",
    "LABEL_POSITIONS", "LABEL_POSITION_ALIASES", "LEGEND_POSITIONS", "MARKER_STYLES", "DASH_NAMES",
    "TYPE_WORDS", "WATERFALL_COLORS", "GRIDLINES_DEFAULT", "CONNECTORS_DEFAULT", "REFERENCE_LINE_DEFAULT",
    "DEFAULT_GAP_WIDTH", "DEFAULT_SIZE_PX", "BASE_SERIES", "MINUS",
]
