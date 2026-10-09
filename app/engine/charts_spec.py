"""The `data-chart` spec: which chart types Path A emits, and what a valid spec looks like.

Two consumers must agree on this file: `engine/verify/lint.py` (a bad `data-chart` is a lint finding
before anything runs) and `engine.ir.validate()` (a chart element's spec must be emittable). The
authoring contract (`docs/engine/03-AUTHORING-CONTRACT.md` §Charts) is the human-facing spec; this
module is its executable form.

Since WP-C the rules that *read* a spec live in `engine/chart_model.py`, once, and this module judges
a spec against them: `validate` checks the raw spellings plus the model, `advisories` returns the
model's advisory notes (a lenient spelling the engine accepted and read one way — a numeric string,
an alias type name, a second totals spelling, a `#RGB` colour). The two never say the same thing:
anything `validate` reports is a `rejected`/`warn` note in the model, never an advisory.
"""
from __future__ import annotations

import math
from typing import Any, cast

from app.engine import chart_model

#: StageFlow's type table — the names the emitter can write, from `chart_model` (stdlib only), so
#: this module no longer imports the emitter. `test_type_names_have_one_source` holds
#: `emit/charts.TYPES | DERIVED` equal to it.
STAGEFLOW_TYPES: frozenset[str] = chart_model.STAGEFLOW_NAMES | chart_model.DERIVED_NAMES

#: Decided in `docs/archive/engine/90-CRITIQUE.md` #4: PptxRender draws nothing for these, so a chart object would fail
#: the gate and the "no renderer warnings" check by construction. Authoring one is a lint *error*.
PATH_A_UNSUPPORTED_PREFIXES: tuple[str, ...] = chart_model.PATH_A_UNSUPPORTED_PREFIXES
PATH_A_UNSUPPORTED: frozenset[str] = frozenset(
    t for t in STAGEFLOW_TYPES if t.startswith(PATH_A_UNSUPPORTED_PREFIXES)
)

#: Added by this engine on top of StageFlow's list.
ENGINE_TYPES: frozenset[str] = chart_model.ENGINE_NAMES

TYPES: frozenset[str] = (STAGEFLOW_TYPES - PATH_A_UNSUPPORTED) | ENGINE_TYPES

#: Deferred to round 2 — `validate` rejects them with a clear message.
ROUND_2_TYPES: frozenset[str] = chart_model.ROUND_2

#: Types whose series carry (x, y) pairs rather than a value per category.
XY_TYPES: frozenset[str] = frozenset(t for t in TYPES if t.startswith("scatter"))

#: Types that take a single series of slices. `pie_of_pie`/`bar_of_pie` are pie flavours too.
PIE_TYPES: frozenset[str] = frozenset(
    t for t in TYPES if t.startswith(("pie", "doughnut")) or t.endswith("_of_pie")
)

OPTION_KEYS: frozenset[str] = chart_model.OPTION_KEYS
SERIES_KEYS: frozenset[str] = chart_model.SERIES_KEYS
AXIS_KEYS: frozenset[str] = chart_model.AXIS_KEYS
LABEL_POSITIONS: frozenset[str] = chart_model.LABEL_POSITIONS
LEGEND_POSITIONS: frozenset[str] = chart_model.LEGEND_POSITIONS
MARKER_STYLES: frozenset[str] = chart_model.MARKER_STYLES
DASH_NAMES: frozenset[str] = chart_model.DASH_NAMES


def _is_number(value: Any) -> bool:
    """A number, or a string the model reads as one (`"4.2"`, `" 6.2 "`, `"1e3"`) — an advisory."""
    return chart_model.coerce_number(value)[1] in ("number", "string")


def _is_colour(value: Any) -> bool:
    """`#RRGGBB`/`RRGGBB`, plus the forms the model tolerates with an advisory: `#RGB`, `#RRGGBBAA`."""
    return chart_model.normalise_colour(value)[1] in ("hex", "short", "alpha")


def _is_index(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            and float(value) == math.floor(value))


def _ranged_problem(value: Any, key: str, at: str | None = None) -> list[str]:
    """A number inside OOXML's range for this option (`chart_model.NUMBER_RANGES`)."""
    at = at or key
    number = chart_model.coerce_number(value)[0]
    if number is None:
        return [f"{at} must be a number"]
    low, high = chart_model.NUMBER_RANGES[key]
    if not low <= number <= high:
        return [f"{at} must be between {chart_model.js_number_string(low)} and "
                f"{chart_model.js_number_string(high)}"]
    return []


def _dash_problem(value: Any, at: str) -> str | None:
    """A dash is a DrawingML preset name or on/off lengths in px; anything else is a typo."""
    if value is None or isinstance(value, str) and value in DASH_NAMES:
        return None
    if isinstance(value, str):
        return f"{at} must be one of {sorted(DASH_NAMES)} or a list of px lengths, got {value!r}"
    if isinstance(value, list) and value and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            and 0 < v <= chart_model.DASH_MAX_PX for v in value):
        return None
    return (f"{at} must be a list of px lengths above 0 and at most "
            f"{chart_model.js_number_string(chart_model.DASH_MAX_PX)}, or a preset dash name")


def _width_problem(value: Any, at: str) -> list[str]:
    """A line width in points, inside ST_LineWidth (0–1584 pt)."""
    if value is None:
        return []
    if not _is_number(value):
        return [f"{at} must be a number (points)"]
    return _ranged_problem(value, "width", at=at)


#: Characters an XML part cannot hold (python-pptx refuses them: "All strings must be XML
#: compatible"). A category, a name or a label carrying one would roll the chart back.
_XML_UNSAFE: frozenset[str] = frozenset(
    chr(code) for code in range(0x20) if chr(code) not in "\t\n\r") | {"\ufffe", "\uffff"}


def _unsafe_strings(value: Any, at: str) -> list[str]:
    """Every string in the spec (keys included) holding a character XML cannot store."""
    found: list[str] = []
    if isinstance(value, str):
        if any(character in _XML_UNSAFE or "\ud800" <= character <= "\udfff" for character in value):
            found.append(f"{at or 'spec'}: {chart_model.show(value)} holds a control character a "
                         f"PowerPoint file cannot store")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_unsafe_strings(item, f"{at}[{index}]"))
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                found.extend(_unsafe_strings(key, f"{at} key" if at else "key"))
            found.extend(_unsafe_strings(item, f"{at}.{key}" if at else str(key)))
    return found


def advisories(spec: dict[str, Any]) -> list[str]:
    """What the engine accepted but read one way — the model's advisory notes, in order.

    Numeric strings, alias type names, deprecated keys, several totals spellings, 0/1 totals read
    as indices, direction words overriding a value's sign, `#RGB`/`#RRGGBBAA` colours, a
    label position the chart type does not allow, bridges whose steps are too small to read, a
    100 % chart's axis value given as a fraction, and `series[k].dataLabels` read leniently (a number
    as its text, a list of the wrong length).
    Never a message `validate` reports (those are the model's `rejected`/`warn` notes).
    """
    if not isinstance(spec, dict):
        return []
    return [note["text"] for note in chart_model.normalise(spec)["notes"] if note["kind"] == "advisory"]


def validate(spec: dict[str, Any]) -> list[str]:
    """Every problem with this chart spec, as readable strings (empty list = emittable).

    Returns rather than raises: the linter shows all findings at once, and `ir.validate()` folds
    them into its own list. Judges the author's own spellings (so a lint message points at what was
    written) and the model (so what is judged is what is drawn).

    The invariant lint relies on: a spec `validate` passes is read by the model with no `rejected`
    or `warn` note (C1 review #12). `rule_problems` is written to cover every such note
    (`test_validate_and_the_model_agree` fuzzes it); the model's own notes are the net under it.
    """
    problems = rule_problems(spec)
    if not problems and isinstance(spec, dict):
        problems = [note["text"] for note in chart_model.normalise(spec)["notes"]
                    if note["kind"] in ("rejected", "warn")]
    return problems


def rule_problems(spec: dict[str, Any]) -> list[str]:
    """`validate`'s own rules, in the author's spellings — everything but the model's net."""
    problems: list[str] = []
    if not isinstance(spec, dict):
        return ["chart spec must be an object"]

    chart_type = spec.get("type")
    if not isinstance(chart_type, str) or not chart_type:
        problems.append("chart spec needs a type")
        return problems
    resolved = chart_model.resolve_type_name(chart_type)
    if resolved["name"] is None:
        problems.append(resolved["reason"])
        return problems
    kind = chart_model.family_of(resolved["name"])
    name = resolved["name"]

    series = spec.get("series")
    if not isinstance(series, list) or not series:
        problems.append("chart spec needs a non-empty series list")
        return problems

    is_xy = kind["family"] == "scatter"
    categories = spec.get("categories")
    category_count = len(categories) if isinstance(categories, list) else 0
    if not is_xy:
        if not isinstance(categories, list) or not categories:
            problems.append("chart spec needs a non-empty categories list")
        elif any(not isinstance(c, (str, int, float)) or isinstance(c, bool) for c in categories):
            problems.append("categories must be strings or numbers (an empty string is a gap slot)")

    waterfall = kind["family"] == "waterfall"
    for index, entry in enumerate(series):
        at = f"series[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{at} must be an object")
            continue
        for key in entry:
            if key not in SERIES_KEYS:
                problems.append(f"unknown {at} key {key!r}")
        problem = _dash_problem(entry.get("dash"), f"{at}.dash")
        if problem:
            problems.append(problem)
        for key in ("lineWidth", "markerSize", "explosion"):
            if entry.get(key) is not None:
                problems.extend(_ranged_problem(entry[key], key, at=f"{at}.{key}"))
        marker = entry.get("marker", entry.get("markers"))
        if marker is not None and not isinstance(marker, bool) and not (
                isinstance(marker, str) and marker.lower() in MARKER_STYLES):
            problems.append(f"unknown {at} marker {marker!r}")
        for key in ("smooth", "line"):
            if entry.get(key) is not None and not isinstance(entry[key], bool):
                problems.append(f"{at}.{key} must be true or false")
        if "name" in entry and entry["name"] is not None and (
                isinstance(entry["name"], bool) or not isinstance(entry["name"], (str, int, float))):
            problems.append(f"{at}.name must be a string")
        if is_xy:
            xs, ys = entry.get("x"), entry.get("y", entry.get("values"))
            if not isinstance(xs, list) or not isinstance(ys, list):
                problems.append(f"{at} needs x and y lists for an XY chart")
            elif len(xs) != len(ys):
                problems.append(f"{at} x ({len(xs)}) and y ({len(ys)}) must be the same length")
            else:
                for label, values in (("x", xs), ("y", ys)):
                    if any(v is not None and not _is_number(v) for v in values):
                        problems.append(f"{at}.{label} must hold numbers or null")
        else:
            values = cast(Any, entry.get("values"))
            if not isinstance(values, list):
                problems.append(f"{at} needs a values list")
            else:
                for point, value in enumerate(values):
                    if value is not None and not _is_number(value):
                        problems.append(f"{at}.values[{point}]: {chart_model.show(value)} is not a number "
                                        f"(null = a gap slot)")
                if isinstance(categories, list) and len(values) != len(categories):
                    problems.append(
                        f"{at}.values has {len(values)} entries but there are {len(categories)} categories"
                    )
        problems.extend(_label_texts_problems(entry.get("dataLabels"), f"{at}.dataLabels"))
        if "chartType" in entry or entry.get("axis") == "secondary":
            problems.append(f"{at}: combo series (chartType/secondary axis) is deferred to round 2")
        for key in ("totals", "types"):
            if entry.get(key) is not None and not waterfall:
                problems.append(f"{at}.{key} only has meaning on a waterfall chart, not on {name}")
        if waterfall:
            problems.extend(_totals_problems(entry.get("totals"), f"{at}.totals", category_count))
            problems.extend(_types_problems(entry.get("types"), f"{at}.types", category_count))

    if name in PIE_TYPES and len(series) > 1:
        problems.append(f"{name} takes a single series, got {len(series)}")

    colors = spec.get("colors")
    if colors is not None:
        if not isinstance(colors, list) or any(not _is_colour(c) for c in colors):
            problems.append("colors must be a list of #RRGGBB strings")

    if spec.get("title") is not None and not isinstance(spec["title"], str):
        problems.append("title must be a string")

    problems.extend(_validate_options(spec.get("options"), name, kind, category_count, len(series)))
    if waterfall:
        problems.extend(_mixed_signs(spec))
    problems.extend(_unsafe_strings(spec, ""))
    return problems


def _as_percent(value: float) -> float:
    """A 100 % axis value as the model reads it: at most 1 in size (0 aside) is a fraction."""
    return value * 100.0 if value != 0 and abs(value) <= 1 else value


def _label_texts_problems(texts: Any, at: str) -> list[str]:
    """`series[k].dataLabels`: a list of label texts, one per point — a string, or null to keep the
    point's value (a number is read as its text, with an advisory)."""
    if texts is None:
        return []
    if not isinstance(texts, list):
        return [f"{at} must be a list of label texts, one per point (null keeps the value)"]
    return [f"{at}[{index}]: {chart_model.show(text)} is not a label text"
            for index, text in enumerate(texts)
            if not (text is None or isinstance(text, str) or chart_model._is_real(text))]


def _totals_problems(totals: Any, at: str, category_count: int) -> list[str]:
    """`totals` is a list of category indices, or a list of true/false flags one per bar — not both.

    A list of bar-type words, one per bar (`["total", "step", …]`), is read too, with an advisory.
    """
    if totals is None:
        return []
    if not isinstance(totals, list):
        return [f"{at} must be a list of category indices or of true/false flags, one per bar"]
    flags = [isinstance(v, bool) for v in totals]
    words = [isinstance(v, str) for v in totals]
    if totals and all(flags):
        if category_count and len(totals) != category_count:
            return [f"{at} has {len(totals)} flags but there are {category_count} categories"]
        return []
    if totals and all(words):
        return _types_problems(totals, at, category_count)
    if any(flags) or any(words):
        return [f"{at} mixes category indices, true/false flags and words — use one or the other"]
    if any(not _is_index(v) for v in totals):
        return [f"{at} must be a list of category indices"]
    if category_count and any(v < 0 or v >= category_count for v in totals):
        return [
            f"{at} indices must be inside 0..{category_count - 1} (they name the bars that stand on "
            f"the axis rather than on the running total)"
        ]
    return []


def _types_problems(types: Any, at: str, category_count: int) -> list[str]:
    if types is None:
        return []
    if not isinstance(types, list):
        return [f"{at} must be a list of words, one per bar"]
    problems: list[str] = []
    for index, word in enumerate(types):
        if word is None:
            continue
        if not isinstance(word, str) or chart_model._trim(word).lower() not in chart_model.TYPE_WORDS:
            problems.append(f"{at}[{index}]: {chart_model.show(word)} is not a bar type — use total, "
                            f"increase, decrease or step")
    if category_count and len(types) != category_count:
        problems.append(f"{at} has {len(types)} entries but there are {category_count} categories")
    return problems


def _mixed_signs(spec: dict[str, Any]) -> list[str]:
    """A stacked waterfall bar is the stack of its series: every value in one bar shares a sign."""
    model = chart_model.normalise(spec)
    if model["family"] != "waterfall" or len(model["series"]) < 2:
        return []
    problems: list[str] = []
    for i, category in enumerate(model["categories"]):
        present = [entry["values"][i] for entry in model["series"] if entry["values"][i] is not None]
        if any(v > 0 for v in present) and any(v < 0 for v in present):
            problems.append(f"category {i} ('{category}') mixes rises and falls across series — "
                            f"all values in one bar must share a sign")
    return problems


def _validate_options(options: Any, chart_type: str, kind: dict[str, Any], category_count: int = 0,
                      series_count: int = 1) -> list[str]:
    if options is None:
        return []
    if not isinstance(options, dict):
        return ["options must be an object"]

    problems: list[str] = []
    for key in options:
        if key not in OPTION_KEYS:
            problems.append(f"unknown option {key!r}")

    position = options.get("labelPosition")
    if position is not None and not (isinstance(position, str) and (
            position in LABEL_POSITIONS or position in chart_model.LABEL_POSITION_ALIASES)):
        problems.append(f"unknown labelPosition {position!r}")

    legend = options.get("legend")
    if legend is not None and not isinstance(legend, bool) and not (
            isinstance(legend, str) and legend in LEGEND_POSITIONS):
        problems.append(f"legend must be false or one of {sorted(LEGEND_POSITIONS)}, got {legend!r}")

    if options.get("secondaryValueAxis") is not None:
        problems.append("secondaryValueAxis belongs to a combo chart and is deferred to round 2")

    for axis_key in ("valueAxis", "categoryAxis", "secondaryValueAxis"):
        axis = options.get(axis_key)
        if axis is None or isinstance(axis, bool):
            continue                               # a bool is StageFlow's {visible: b} — an advisory
        if not isinstance(axis, dict):
            problems.append(f"{axis_key} must be an object")
            continue
        for key in axis:
            if key not in AXIS_KEYS:
                problems.append(f"unknown {axis_key} key {key!r}")
        for key in ("min", "max", "majorUnit", "minorUnit"):
            if key in axis and axis[key] is not None and not _is_number(axis[key]):
                problems.append(f"{axis_key}.{key} must be a number")
        low = chart_model.coerce_number(axis.get("min"))[0]
        high = chart_model.coerce_number(axis.get("max"))[0]
        if low is not None and high is not None and low >= high:
            problems.append(f"{axis_key}.min must be below {axis_key}.max")
        elif (axis_key == "valueAxis" and low is not None and high is not None
              and chart_model.is_percent_axis(kind["family"], kind["stacked"])
              and _as_percent(low) >= _as_percent(high)):
            # A 100 % axis reads a value of at most 1 as a fraction (`chart_model._percent_axis`).
            problems.append(f"{axis_key}.min must be below {axis_key}.max (a 100 % chart's axis is in "
                            f"percent; a value of at most 1 is read as a fraction)")
        for key in ("majorUnit", "minorUnit"):
            unit = chart_model.coerce_number(axis.get(key))[0]
            if unit is not None and unit <= 0:
                problems.append(f"{axis_key}.{key} must be above 0")
        for key in ("format", "title"):
            if axis.get(key) is not None and not isinstance(axis[key], str):
                problems.append(f"{axis_key}.{key} must be a string")
        for key in ("visible", "reverse", "bold"):
            if axis.get(key) is not None and not isinstance(axis[key], bool):
                problems.append(f"{axis_key}.{key} must be true or false")

    plot_area = options.get("plotArea")
    if plot_area is not None:
        if not isinstance(plot_area, dict) or any(k not in plot_area for k in ("x", "y", "w", "h")):
            problems.append("plotArea must be an object with x, y, w and h")
        elif any(not _is_number(plot_area[k]) for k in ("x", "y", "w", "h")):
            problems.append("plotArea x/y/w/h must be numbers")
        else:
            x, y, w, h = (cast(float, chart_model.coerce_number(plot_area[k])[0]) for k in ("x", "y", "w", "h"))
            if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < w <= 1 and 0 < h <= 1):
                problems.append("plotArea values are fractions of the frame, so they must lie in [0, 1]")
            elif x + w > 1.001 or y + h > 1.001:
                problems.append("plotArea runs outside the chart frame (x + w and y + h must be ≤ 1)")

    reference_lines = options.get("referenceLines")
    if reference_lines is not None:
        if not isinstance(reference_lines, list):
            problems.append("referenceLines must be a list")
        else:
            if reference_lines and kind["family"] in ("pie", "doughnut"):
                problems.append(f"referenceLines need a value axis, and a {chart_type} chart has none")
            for index, line in enumerate(reference_lines):
                at = f"referenceLines[{index}]"
                if not isinstance(line, dict) or not _is_number(line.get("value")):
                    problems.append(f"{at} needs a numeric value")
                    continue
                for key in line:
                    if key not in ("value", "color", "dash", "label", "width"):
                        problems.append(f"unknown {at} key {key!r}")
                if line.get("color") is not None and not _is_colour(line["color"]):
                    problems.append(f"{at}.color must be #RRGGBB")
                if line.get("label") is not None and (
                        isinstance(line["label"], bool) or not isinstance(line["label"], (str, int, float))):
                    problems.append(f"{at}.label must be a string")
                problems.extend(_width_problem(line.get("width"), f"{at}.width"))
                problem = _dash_problem(line.get("dash"), f"{at}.dash")
                if problem:
                    problems.append(problem)

    problems.extend(_point_colour_problems(options.get("pointColors"), series_count,
                                           None if kind["family"] == "scatter" else category_count))

    hole = options.get("holeSize")
    if hole is not None:
        size = chart_model.coerce_number(hole)[0]
        if size is None or not (1 <= size <= 90):
            problems.append("holeSize is a percentage in [1, 90]")
        elif chart_type not in PIE_TYPES:
            problems.append(f"holeSize has no meaning on a {chart_type} chart")

    for key in ("gapWidth", "overlap", "lineWidth", "fontSize", "markerSize"):
        if key in options and options[key] is not None:
            problems.extend(_ranged_problem(options[key], key))

    if options.get("fontColor") is not None and not _is_colour(options["fontColor"]):
        problems.append("fontColor must be #RRGGBB")

    gridlines = options.get("gridlines")
    if gridlines is not None and not isinstance(gridlines, bool):
        if not isinstance(gridlines, dict):
            problems.append("gridlines must be a bool or {color, width, dash}")
        else:
            for key in gridlines:
                if key not in ("color", "width", "dash"):
                    problems.append(f"unknown gridlines key {key!r}")
            if gridlines.get("color") is not None and not _is_colour(gridlines["color"]):
                problems.append("gridlines.color must be #RRGGBB")
            problems.extend(_width_problem(gridlines.get("width"), "gridlines.width"))
            problem = _dash_problem(gridlines.get("dash"), "gridlines.dash")
            if problem:
                problems.append(problem)

    labels = options.get("dataLabels")
    if labels is not None and not isinstance(labels, bool):
        if not isinstance(labels, list) or any(not isinstance(v, bool) for v in labels):
            problems.append("dataLabels must be a bool or a list of bools, one per series")
    problems.extend(_label_style_problems(options.get("labelStyle")))

    problem = _dash_problem(options.get("dash"), "dash")
    if problem:
        problems.append(problem)

    markers = options.get("markers")
    if markers is not None and not isinstance(markers, bool) and str(markers).lower() not in MARKER_STYLES:
        problems.append(f"unknown markers {markers!r} — one of {sorted(MARKER_STYLES)}, or a bool")

    for key in ("smooth", "varyColors"):
        if options.get(key) is not None and not isinstance(options[key], bool):
            problems.append(f"{key} must be true or false")

    for key in ("numberFormat", "labelFormat", "font", "title"):
        if options.get(key) is not None and not isinstance(options[key], str):
            problems.append(f"{key} must be a string")

    if options.get("explosion") is not None:
        if not _is_number(options["explosion"]):
            problems.append("explosion must be a number (percent of the radius)")
        else:
            problems.extend(_ranged_problem(options["explosion"], "explosion"))

    problems.extend(_validate_waterfall(options, chart_type, category_count))
    return problems


def _label_style_problems(style: Any) -> list[str]:
    """`labelStyle`: `{bold, color, fontSize}` for the data labels — each optional."""
    if style is None:
        return []
    if not isinstance(style, dict):
        return ["labelStyle must be an object {bold, color, fontSize}"]
    problems = [f"unknown labelStyle key {key!r}" for key in style if key not in chart_model.LABEL_STYLE_KEYS]
    if style.get("bold") is not None and not isinstance(style["bold"], bool):
        problems.append("labelStyle.bold must be true or false")
    if style.get("color") is not None and not _is_colour(style["color"]):
        problems.append("labelStyle.color must be #RRGGBB")
    if style.get("fontSize") is not None:
        problems.extend(_ranged_problem(style["fontSize"], "fontSize", at="labelStyle.fontSize"))
    return problems


def _point_colour_problems(point_colors: Any, series_count: int, category_count: int | None) -> list[str]:
    """`pointColors`: `{series: {point: colour}}`, or — on a single-series chart — a list of colours
    or `{point: colour}`, both read as series 0."""
    if point_colors is None:
        return []
    problems: list[str] = []
    flat = isinstance(point_colors, list) or (
        isinstance(point_colors, dict) and not any(isinstance(v, dict) for v in point_colors.values()))
    if flat and series_count > 1:
        problems.append("pointColors as a list or {point: colour} only works on a single-series chart — "
                        "write {series: {point: colour}}")
    if isinstance(point_colors, list):
        for index, colour in enumerate(point_colors):
            if colour is not None and not _is_colour(colour):
                problems.append(f"pointColors[{index}] must be #RRGGBB")
        if category_count is not None and len(point_colors) > category_count:
            problems.append(f"pointColors has {len(point_colors)} colours for {category_count} points")
        return problems
    if not isinstance(point_colors, dict):
        return ["pointColors must be an object keyed by series index"]

    def bad_key(key: Any) -> bool:
        return not (isinstance(key, str) and key.isascii() and key.isdigit()) and not (
            isinstance(key, int) and not isinstance(key, bool) and key >= 0)

    if flat:
        for point_key, colour in point_colors.items():
            if bad_key(point_key):
                problems.append(f"pointColors key {point_key!r} is not a point index")
            elif category_count is not None and int(point_key) >= category_count:
                problems.append(f"pointColors[{point_key!r}] is outside the chart")
            if not _is_colour(colour):
                problems.append(f"pointColors[{point_key!r}] must be #RRGGBB")
        return problems
    for series_key, points in point_colors.items():
        if bad_key(series_key):
            problems.append(f"pointColors key {series_key!r} is not a series index")
            continue
        if not isinstance(points, dict):
            problems.append(f"pointColors[{series_key!r}] must be an object keyed by point index")
            continue
        if int(series_key) >= max(series_count, 1):
            problems.append(f"pointColors[{series_key!r}] is outside the chart")
        for point_key, colour in points.items():
            if bad_key(point_key):
                problems.append(f"pointColors[{series_key!r}] key {point_key!r} is not a point index")
                continue
            if category_count is not None and int(point_key) >= category_count:
                problems.append(f"pointColors[{series_key!r}][{point_key!r}] is outside the chart")
            if not _is_colour(colour):
                problems.append(f"pointColors[{series_key!r}][{point_key!r}] must be #RRGGBB")
    return problems


def _validate_waterfall(options: dict[str, Any], chart_type: str, category_count: int) -> list[str]:
    """`totals`, `connectors` and `baseValue` steer what a waterfall cannot infer from its numbers."""
    problems: list[str] = []
    waterfall_keys = ("totals", "totalIndices", "connectors", "connectorColor", "baseValue")
    if chart_type != "waterfall":
        for key in waterfall_keys:
            if options.get(key) is not None:
                problems.append(f"{key} only has meaning on a waterfall chart, not on {chart_type}")
        return problems

    problems.extend(_totals_problems(options.get("totals"), "totals", category_count))
    problems.extend(_totals_problems(options.get("totalIndices"), "totalIndices", category_count))
    base = options.get("baseValue")
    if base is not None and not _is_number(base):
        problems.append("baseValue must be a number")
    if options.get("connectorColor") is not None and not _is_colour(options["connectorColor"]):
        problems.append("connectorColor must be #RRGGBB")
    connectors = options.get("connectors")
    if connectors is not None and not isinstance(connectors, bool):
        if not isinstance(connectors, dict):
            problems.append("connectors must be false or {color, width, dash}")
        else:
            for key in connectors:
                if key not in ("color", "width", "dash"):
                    problems.append(f"unknown connectors key {key!r}")
            if connectors.get("color") is not None and not _is_colour(connectors["color"]):
                problems.append("connectors.color must be #RRGGBB")
            problems.extend(_width_problem(connectors.get("width"), "connectors.width"))
            problem = _dash_problem(connectors.get("dash"), "connectors.dash")
            if problem:
                problems.append(problem)
    return problems
