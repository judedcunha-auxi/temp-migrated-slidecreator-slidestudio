/*!
 * chart-preview.js — the browser's picture of a `data-chart` spec.   WP3b, WP-C
 *
 * Why this file exists
 * --------------------
 * Slide HTML carries charts as data, not as drawing: `<div data-chart='{...}'></div>` (authoring
 * contract §Charts). The export emits a **native** PowerPoint chart from that spec; nothing in the
 * stored HTML draws it. So without this script the designer sees an empty box, and — worse — the
 * gate's reference composite (`engine/extract/html.py: render_reference`) would show a hole where
 * the exported deck has a chart.
 *
 * One model, two languages (WP-C)
 * -------------------------------
 * The first half of this file is a **line-for-line transliteration of `engine/chart_model.py`** —
 * the one reading of a spec that the validator, the emitter, the structural check and this preview
 * share. `ChartPreview.normalise(spec)` and `ChartPreview.layout(model, w, h, sizePx)` return
 * exactly what the Python functions of the same names return; `engine/fixtures/charts/
 * parity-specs.json` and `engine/tests/test_chart_parity.py` hold the two equal. The rules of the
 * transliteration are the Python module's: no `toFixed` (rounding is done on the shortest decimal
 * string, half up, as Excel displays), powers of ten by multiplying, an ASCII-only trim, objects
 * enumerated in JS key order on both sides. A rule changed in one language and not in the other
 * fails the parity test; a rule added without a parity spec fails review.
 *
 * The second half draws **only** from the model and the layout: the plot rect is
 * `layout.plotArea × frame`, the value axis is the model's, a waterfall is the plan's pieces
 * stacked exactly as PowerPoint stacks a stacked column (so a series split across zero shows two
 * pieces), labels read what the file's labels read (frozen text included), connectors are dashed in
 * the connector colour between the same bar edges the emitter uses. Where the chart is not pinned
 * (no `plotArea`, nothing drawn on top of it) PowerPoint lays the chart out itself and this picture
 * is an approximation — the one known difference, stated in the contract.
 *
 * Contract with the engine
 * ------------------------
 *   window.ChartPreview.renderAll(root = document) -> number of [data-chart] elements drawn
 *   window.ChartPreview.render(element)            -> 'ok' | 'unsupported' | 'error' | 'skipped'
 *   window.ChartPreview.normalise(spec)            -> the model (chart_model.normalise)
 *   window.ChartPreview.layout(model, w, h, px)    -> the layout (chart_model.layout)
 *
 * `render_reference` injects this file into the slide frame **after load** and then calls
 * `renderAll(document)` itself, because `DOMContentLoaded` has already been and gone by then. The
 * auto-run below therefore also covers the "loaded normally in the app" case, and `renderAll` is
 * idempotent: it removes the `<svg class="chart-preview">` it made last time and redraws, leaving
 * every other child of the chart element (the design's own annotations) untouched and on top.
 * Every coercion the model made is on the element as `data-chart-preview-notes` (a JSON list).
 *
 * Everything drawn is tagged (`class="cp-bar"`, `data-cp-value`, `data-cp-plot`, …) so the tests in
 * `engine/tests/test_chart_preview.py` and `test_chart_parity.py` assert geometry in pixels.
 *
 * Deterministic by construction: no clock, no randomness, no animation, coordinates rounded to
 * 3 decimals — two runs of the same spec produce byte-identical markup.
 */
(function (global) {
  'use strict';

  var NS = 'http://www.w3.org/2000/svg';
  var VERSION = '2.1.0';

  // =================================================================================================
  // THE CHART MODEL — a line-for-line twin of engine/chart_model.py. Same names (camelCase), same
  // order. Change both or neither.
  // =================================================================================================

  function setOf(names) {
    var out = {};
    for (var i = 0; i < names.length; i++) out[names[i]] = true;
    return out;
  }
  /** `key in mapping` for a JSON object — own properties only ("constructor" is not a chart type). */
  function has(mapping, key) {
    return mapping !== null && typeof mapping === 'object' && Object.prototype.hasOwnProperty.call(mapping, key);
  }
  /** `mapping.get(key)`: the value, or null when absent. */
  function get(mapping, key) {
    if (!has(mapping, key)) return null;
    var value = mapping[key];
    return value === undefined ? null : value;
  }
  function isDict(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
  function isList(value) { return Array.isArray(value); }
  function isStr(value) { return typeof value === 'string'; }
  function isBool(value) { return typeof value === 'boolean'; }
  function keysOf(set) { return Object.keys(set); }
  function numericSort(values) { return values.slice().sort(function (a, b) { return a - b; }); }
  function codeSort(values) {
    return values.slice().sort(function (a, b) { return a < b ? -1 : (a > b ? 1 : 0); });
  }
  function copyObject(source) {
    var out = {};
    for (var key in source) if (Object.prototype.hasOwnProperty.call(source, key)) out[key] = source[key];
    return out;
  }
  function repeat(text, count) {
    var out = '';
    for (var i = 0; i < count; i++) out += text;
    return out;
  }
  function padLeft(text, width, fill) { return text.length >= width ? text : repeat(fill, width - text.length) + text; }
  function stripLeft(text, chars) {
    var start = 0;
    while (start < text.length && chars.indexOf(text.charAt(start)) >= 0) start++;
    return text.slice(start);
  }
  function stripRight(text, chars) {
    var end = text.length;
    while (end > 0 && chars.indexOf(text.charAt(end - 1)) >= 0) end--;
    return text.slice(0, end);
  }
  function countOf(text, character) { return text.split(character).length - 1; }

  // ------------------------------------------------------------------------------ the type tables

  var STAGEFLOW_NAMES = setOf([
    'column', 'column_stacked', 'column_stacked_100',
    'bar', 'bar_stacked', 'bar_stacked_100',
    'line', 'line_markers', 'line_stacked', 'line_stacked_100',
    'line_markers_stacked', 'line_markers_stacked_100',
    'area', 'area_stacked', 'area_stacked_100',
    'pie', 'pie_exploded', 'doughnut', 'doughnut_exploded',
    'radar', 'radar_markers', 'radar_filled',
    'scatter', 'scatter_lines', 'scatter_lines_no_markers', 'scatter_smooth',
    'scatter_smooth_no_markers',
    'bubble', 'bubble_3d'
  ]);

  var DERIVED_BASES = {
    column_3d: 'column', column_3d_clustered: 'column',
    column_3d_stacked: 'column_stacked', column_3d_stacked_100: 'column_stacked_100',
    bar_3d_clustered: 'bar', bar_3d_stacked: 'bar_stacked', bar_3d_stacked_100: 'bar_stacked_100',
    cone_column: 'column', cone_column_clustered: 'column',
    cone_column_stacked: 'column_stacked', cone_column_stacked_100: 'column_stacked_100',
    cone_bar_clustered: 'bar', cone_bar_stacked: 'bar_stacked', cone_bar_stacked_100: 'bar_stacked_100',
    cylinder_column: 'column', cylinder_column_clustered: 'column',
    cylinder_column_stacked: 'column_stacked', cylinder_column_stacked_100: 'column_stacked_100',
    cylinder_bar_clustered: 'bar', cylinder_bar_stacked: 'bar_stacked',
    cylinder_bar_stacked_100: 'bar_stacked_100',
    pyramid_column: 'column', pyramid_column_clustered: 'column',
    pyramid_column_stacked: 'column_stacked', pyramid_column_stacked_100: 'column_stacked_100',
    pyramid_bar_clustered: 'bar', pyramid_bar_stacked: 'bar_stacked',
    pyramid_bar_stacked_100: 'bar_stacked_100',
    line_3d: 'line', area_3d: 'area', area_3d_stacked: 'area_stacked',
    area_3d_stacked_100: 'area_stacked_100', pie_3d: 'pie', pie_3d_exploded: 'pie_exploded',
    surface: 'column', surface_wireframe: 'column', surface_top_view: 'column',
    surface_top_view_wireframe: 'column',
    pie_of_pie: 'pie', bar_of_pie: 'pie',
    stock_hlc: 'line', stock_ohlc: 'line', stock_vhlc: 'line', stock_vohlc: 'line'
  };
  var DERIVED_NAMES = setOf(keysOf(DERIVED_BASES));

  var ENGINE_BASES = { waterfall: 'column_stacked', combo: 'column' };
  var ENGINE_NAMES = setOf(keysOf(ENGINE_BASES));

  var KNOWN_NAMES = setOf(keysOf(STAGEFLOW_NAMES).concat(keysOf(DERIVED_NAMES), keysOf(ENGINE_NAMES)));

  var ALIASES = {
    // StageFlow
    col: 'column', bars: 'bar', donut: 'doughnut', donut_exploded: 'doughnut_exploded',
    xy: 'scatter', column_100: 'column_stacked_100', bar_100: 'bar_stacked_100',
    '3d_column': 'column_3d', '3d_bar': 'bar_3d_clustered', '3d_pie': 'pie_3d',
    '3d_line': 'line_3d', '3d_area': 'area_3d', stock: 'stock_hlc',
    waterfall_column: 'waterfall',
    // WP-C §2.1
    clustered_bar: 'bar', horizontal_bar: 'bar', hbar: 'bar',
    grouped_column: 'column', clustered_column: 'column', vertical_bar: 'column',
    columns: 'column',
    stacked_column: 'column_stacked', column_stack: 'column_stacked',
    stacked_bar: 'bar_stacked', bar_stack: 'bar_stacked',
    stacked_column_100: 'column_stacked_100', column_percent: 'column_stacked_100',
    stacked_bar_100: 'bar_stacked_100',
    ring: 'doughnut', lines: 'line', scatter_plot: 'scatter',
    bridge: 'waterfall', waterfall_chart: 'waterfall'
  };

  var PATH_A_UNSUPPORTED_PREFIXES = ['bubble', 'radar', 'stock', 'surface'];
  var ROUND_2 = setOf(['combo']);

  // ---------------------------------------------------------------------------- the option tables

  var OPTION_KEYS = setOf([
    'dataLabels', 'labelPosition', 'labelFormat', 'labelStyle', 'numberFormat',
    'gridlines', 'legend', 'gapWidth', 'overlap',
    'valueAxis', 'categoryAxis', 'secondaryValueAxis',
    'plotArea', 'referenceLines', 'pointColors',
    'holeSize', 'smooth', 'markers', 'markerSize', 'lineWidth', 'dash',
    'font', 'fontSize', 'fontColor', 'title', 'varyColors', 'explosion',
    'totals', 'connectors', 'baseValue',
    'totalIndices', 'connectorColor'
  ]);

  var SERIES_KEYS = setOf([
    'name', 'values', 'x', 'y', 'sizes', 'size',
    'dash', 'lineWidth', 'smooth', 'marker', 'markers', 'markerSize', 'explosion', 'line',
    'totals', 'types',
    'dataLabels',
    'chartType', 'axis'
  ]);

  var AXIS_KEYS = setOf(['min', 'max', 'majorUnit', 'minorUnit', 'visible', 'format', 'reverse', 'title', 'bold']);

  /* `LABEL_STYLE_KEYS`: `options.labelStyle` — the data labels' own bold, colour and size (px). */
  var LABEL_STYLE_KEYS = setOf(['bold', 'color', 'fontSize']);

  var LABEL_POSITIONS = setOf(['center', 'inEnd', 'inBase', 'outEnd', 'bestFit', 'left', 'right', 'above', 'below']);
  var LABEL_POSITION_ALIASES = {
    inside_end: 'inEnd', inside_base: 'inBase', outside_end: 'outEnd', best_fit: 'bestFit'
  };

  var LEGEND_POSITIONS = setOf(['bottom', 'top', 'left', 'right', 'topRight']);

  var MARKER_STYLES = setOf(['none', 'auto', 'circle', 'square', 'diamond', 'triangle', 'x', 'star', 'dash', 'dot', 'plus']);

  var DASH_NAMES = setOf([
    'solid', 'dot', 'dash', 'lgDash', 'dashDot', 'lgDashDot', 'lgDashDotDot',
    'sysDash', 'sysDot', 'sysDashDot', 'sysDashDotDot'
  ]);

  var TYPE_WORDS = {
    total: 'total', subtotal: 'total', sum: 'total', end: 'total', start: 'total',
    increase: 'increase', up: 'increase', rise: 'increase', gain: 'increase',
    decrease: 'decrease', down: 'decrease', fall: 'decrease', loss: 'decrease',
    delta: 'step', step: 'step', change: 'step'
  };

  // --------------------------------------------------------------------------------- the defaults

  var WATERFALL_COLORS = { increase: '2DB757', decrease: 'E5484D', total: '516467' };
  /* The chart's ink: a one-colour waterfall's totals (`WATERFALL_INK`). The emitter swaps in the
     theme's dark tx2; the preview cannot see the theme and draws this. */
  var WATERFALL_INK = '1F2937';
  /* `TOTAL_CATEGORY`: no `u` flag, so `\b` and `i` are ASCII-only, as `re.ASCII` makes them there. */
  var TOTAL_CATEGORY = /\b(total|net|pro[- ]?forma|ending|closing|final|combined)\b/i;
  /* `SUBTOTAL_CATEGORY`: a middle bar named as a subtotal — read with the running-sum check
     (`inferredTotals`); ASCII `\b` and `i`, as above. `SUBTOTAL_TOLERANCE`: 0.5 % of the running sum. */
  var SUBTOTAL_CATEGORY = /\b(total|net|pro[- ]?forma|ending|closing|final|combined|sub[- ]?totals?|totals|ebitda|ebita|ebit|gross[- ]profit|gross[- ]margin|operating[- ]income|operating[- ]profit|revenues?|sales|value|base|baseline|opening|starting)\b/i;
  var SUBTOTAL_TOLERANCE = 0.005;
  var PERCENT_TICK_FORMAT = '0%';
  var GRIDLINES_DEFAULT = { color: 'E4E9F0', width: 0.75, dash: null };
  var CONNECTORS_DEFAULT = { color: '8A9699', width: 0.75, dash: [3.0, 2.0] };
  var REFERENCE_LINE_DEFAULT = { color: '516467', width: 1.0, dash: [4.0, 3.0] };
  var DEFAULT_GAP_WIDTH = 60.0;
  var DEFAULT_SIZE_PX = 14.667;
  var BASE_SERIES = 'Base';
  var MINUS = '−';

  var OPTION_ORDER = [
    'dataLabels', 'labelPosition', 'numberFormat', 'labelFormat', 'labelStyle', 'gridlines', 'legend',
    'gapWidth', 'overlap', 'valueAxis', 'categoryAxis', 'plotArea', 'pointColors', 'totals',
    'baseValue', 'connectors', 'referenceLines', 'fontSize', 'font', 'fontColor', 'title',
    'holeSize', 'smooth', 'markers', 'markerSize', 'lineWidth', 'dash', 'varyColors', 'explosion'
  ];

  // ------------------------------------------------------------------------------- small helpers

  var WHITESPACE = ' \t\n\r\f\v';
  var NUMBER_RE = /^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$/;
  var HEX6 = /^#?([0-9A-Fa-f]{6})$/;
  var HEX3 = /^#([0-9A-Fa-f]{3})$/;
  var HEX8 = /^#([0-9A-Fa-f]{6})[0-9A-Fa-f]{2}$/;
  var INDEX_KEY = /^[0-9]+$/;

  /** ASCII-whitespace trim — JS `trim()` and Python's `strip()` disagree on Unicode spaces. */
  function trim(text) { return stripRight(stripLeft(text, WHITESPACE), WHITESPACE); }

  /** `text_lines`: a label or category text's lines — `\n` (or `\r\n`) breaks it. */
  function textLines(text) { return String(text).split(/\r?\n/); }

  /** A JSON number: a finite number (JSON has no other kind; `true` is not a number). */
  function isReal(value) { return typeof value === 'number' && isFinite(value); }

  function jsRound(value) { return isFinite(value) ? Math.round(value) : value; }

  /** `Math.round(value * 10^places) / 10^places`, and never `-0`. */
  function roundTo(value, places) {
    var factor = pow10(places);
    var result = jsRound(value * factor) / factor;
    return result === 0 ? 0 : result;
  }

  /** 10^exponent by repeated multiplication — identical in both languages (no `pow`/`log10`). */
  function pow10(exponent) {
    var result = 1.0;
    for (var i = 0; i < Math.abs(exponent); i++) result *= 10.0;
    return exponent >= 0 ? result : 1.0 / result;
  }

  /** A finite number with `-0` folded to `0`. */
  function toFloat(value) {
    var result = Number(value);
    return result === 0 ? 0 : result;
  }

  /** [digits, n] with value = 0.digits × 10^n — the shortest round-trip digits (`_shortest`). */
  function shortest(value) {
    var text = value.toExponential();
    var at = text.indexOf('e');
    var mantissa = text.slice(0, at).replace('.', '');
    var exponent = parseInt(text.slice(at + 1), 10);
    var digits = stripRight(stripLeft(mantissa, '0'), '0') || '0';
    return [digits, exponent + 1];
  }

  /** JS `String(number)`, with `-0` as "0" — the Python module reproduces exactly this. */
  function jsNumberString(value) {
    if (value !== value) return 'NaN';
    if (value === Infinity) return 'Infinity';
    if (value === -Infinity) return '-Infinity';
    if (value === 0) return '0';
    return String(value);
  }

  /** A value as a note prints it — the same text in both languages. */
  function show(value) {
    if (value === null || value === undefined) return 'null';
    if (isBool(value)) return value ? 'true' : 'false';
    if (typeof value === 'number') return jsNumberString(value);
    if (isStr(value)) {
      var out = '"';
      for (var i = 0; i < value.length; i++) {
        var character = value.charAt(i);
        var code = value.charCodeAt(i);
        if (character === '"') out += '\\"';
        else if (character === '\\') out += '\\\\';
        else if (character === '\n') out += '\\n';
        else if (character === '\r') out += '\\r';
        else if (character === '\t') out += '\\t';
        else if (character === '\b') out += '\\b';
        else if (character === '\f') out += '\\f';
        else if (code < 0x20) out += '\\u' + padLeft(code.toString(16), 4, '0');
        else out += character;
      }
      return out + '"';
    }
    if (isList(value)) return 'a list';
    return 'an object';
  }

  function showList(values) { return '[' + values.map(show).join(',') + ']'; }

  function note(notes, kind, text) { notes.push({ kind: kind, text: text }); }

  /** A category or series name: a string as written, a number as JS prints it, else null. */
  function asText(value) {
    if (isStr(value)) return value;
    if (isReal(value)) return jsNumberString(value);
    return null;
  }

  // ======================================================================= rule 1: the type name

  function editDistance(a, b) {
    var previous = [];
    for (var j = 0; j <= b.length; j++) previous.push(j);
    for (var i = 1; i <= a.length; i++) {
      var current = [i];
      for (var k = 1; k <= b.length; k++) {
        current.push(Math.min(previous[k] + 1, current[k - 1] + 1,
          previous[k - 1] + (a.charAt(i - 1) !== b.charAt(k - 1) ? 1 : 0)));
      }
      previous = current;
    }
    return previous[previous.length - 1];
  }

  function hint(name) {
    var best = null, bestDistance = 3;
    var candidates = codeSort(keysOf(setOf(keysOf(KNOWN_NAMES).concat(keysOf(ALIASES)))));
    for (var i = 0; i < candidates.length; i++) {
      var distance = editDistance(name, candidates[i]);
      if (distance < bestDistance) { best = candidates[i]; bestDistance = distance; }
    }
    if (best !== null) return has(ALIASES, best) ? ALIASES[best] : best;
    var words = name.split('_');
    for (var index = words.length - 1; index >= 0; index--) {
      if (has(KNOWN_NAMES, words[index]) && words[index] !== name) return words[index];
    }
    return null;
  }

  function resolveTypeName(raw) {
    var requested = raw !== null && raw !== undefined ? asText(raw) : 'column';
    if (requested === null) requested = show(raw);
    var name = requested.toLowerCase().split('-').join('_').split(' ').join('_');
    var candidates = [name];
    var stripped = name;
    if (stripped.length >= 6 && stripped.slice(-6) === '_chart') stripped = stripped.slice(0, -6);
    else if (stripped.length > 5 && stripped.slice(-5) === 'chart') stripped = stripped.slice(0, -5);
    if (candidates.indexOf(stripped) < 0) candidates.push(stripped);
    var unclustered = stripped.split('_clustered').join('');
    if (candidates.indexOf(unclustered) < 0) candidates.push(unclustered);

    var resolved = null, i;
    for (i = 0; i < candidates.length; i++) {
      if (has(KNOWN_NAMES, candidates[i])) { resolved = candidates[i]; break; }
    }
    if (resolved === null) {
      for (i = 0; i < candidates.length; i++) {
        if (has(ALIASES, candidates[i])) { resolved = ALIASES[candidates[i]]; break; }
      }
    }
    if (resolved === null) {
      var guess = hint(name);
      var reason = "unknown chart type '" + requested + "'" + (guess ? " (did you mean '" + guess + "'?)" : '');
      return { name: null, requested: requested, reason: reason };
    }
    for (i = 0; i < PATH_A_UNSUPPORTED_PREFIXES.length; i++) {
      if (resolved.indexOf(PATH_A_UNSUPPORTED_PREFIXES[i]) === 0) {
        return {
          name: null, requested: requested, reason:
            "chart type '" + requested + "' is not a chart object on Path A (the renderer draws nothing " +
            'for it); draw it as SVG shapes and text instead — see 03-AUTHORING-CONTRACT.md §Charts'
        };
      }
    }
    if (has(ROUND_2, resolved)) {
      return { name: null, requested: requested,
        reason: "chart type '" + requested + "' is deferred to round 2 and cannot be emitted yet" };
    }
    return { name: resolved, requested: requested, reason: null };
  }

  function familyOf(name) {
    if (name === null) return { family: null, base: null, stacked: '' };
    var base = get(ENGINE_BASES, name) || get(DERIVED_BASES, name) || name;
    if (name === 'waterfall') return { family: 'waterfall', base: base, stacked: 'stacked' };
    var stacked = /_stacked_100$/.test(base) ? 'stacked100' : (base.indexOf('_stacked') >= 0 ? 'stacked' : '');
    var prefixes = ['column', 'bar', 'line', 'area', 'pie', 'doughnut', 'scatter'];
    for (var i = 0; i < prefixes.length; i++) {
      if (base.indexOf(prefixes[i]) === 0) return { family: prefixes[i], base: base, stacked: stacked };
    }
    return { family: null, base: base, stacked: stacked };
  }

  /** [allowed positions (a set), the family's default] — `label_position_rule`. */
  function labelPositionRule(family, stacked) {
    if (family === 'waterfall') return [setOf(['center', 'inEnd', 'inBase', 'outEnd']), 'center'];
    if (family === 'column' || family === 'bar') {
      if (stacked) return [setOf(['center', 'inEnd', 'inBase']), 'center'];
      return [setOf(['center', 'inEnd', 'inBase', 'outEnd']), 'outEnd'];
    }
    if (family === 'line') return [setOf(['center', 'left', 'right', 'above', 'below']), 'above'];
    if (family === 'scatter') return [setOf(['center', 'left', 'right', 'above', 'below']), 'right'];
    if (family === 'pie') return [setOf(['bestFit', 'center', 'inEnd', 'outEnd']), 'bestFit'];
    return [{}, null];
  }

  // ======================================================================== rule 2: numbers

  /** [number, how]: how is 'number', 'string' (coerced), 'gap' (null) or 'invalid'. */
  function coerceNumber(value) {
    if (value === null || value === undefined) return [null, 'gap'];
    if (isReal(value)) return [toFloat(value), 'number'];
    if (isStr(value)) {
      var text = trim(value);
      if (NUMBER_RE.test(text)) {
        var number = Number(text);
        if (isFinite(number)) return [toFloat(number), 'string'];
      }
    }
    return [null, 'invalid'];
  }

  function numberOption(raw, where, notes, kindBad, what) {
    kindBad = kindBad || 'rejected';
    what = what || 'ignored';
    var coerced = coerceNumber(raw), number = coerced[0], how = coerced[1];
    if (how === 'string') note(notes, 'advisory', where + ': ' + show(raw) + ' read as ' + jsNumberString(number));
    else if (how === 'invalid') note(notes, kindBad, where + ': ' + show(raw) + ' is not a number — ' + what);
    return number;
  }

  var NUMBER_RANGES = {
    gapWidth: [0.0, 500.0], overlap: [-100.0, 100.0], holeSize: [1.0, 90.0],
    markerSize: [2.0, 72.0], lineWidth: [0.0, 1584.0], width: [0.0, 1584.0],
    explosion: [0.0, 400.0], fontSize: [0.0, 4000.0]
  };

  var DASH_MAX_PX = 1000.0;

  function ranged(raw, where, notes) {
    if (raw === null || raw === undefined) return null;
    var number = numberOption(raw, where, notes);
    if (number === null) return null;
    var range = NUMBER_RANGES[where.slice(where.lastIndexOf('.') + 1)];
    if (!(range[0] <= number && number <= range[1])) {
      note(notes, 'rejected', where + ': ' + jsNumberString(number) + ' is outside ' +
        jsNumberString(range[0]) + '..' + jsNumberString(range[1]) + ' — ignored');
      return null;
    }
    return number;
  }

  function numberList(raw, where, notes) {
    if (!isList(raw)) return null;
    var out = [];
    for (var index = 0; index < raw.length; index++) {
      var value = raw[index];
      var coerced = coerceNumber(value), number = coerced[0], how = coerced[1];
      if (how === 'string') {
        note(notes, 'advisory', where + '[' + index + ']: ' + show(value) + ' read as ' + jsNumberString(number));
      } else if (how === 'invalid') {
        note(notes, 'warn', where + '[' + index + ']: ' + show(value) + ' is not a number — treated as a gap slot');
      }
      out.push(number);
    }
    return out;
  }

  // ======================================================================== rule 4: colours

  /** [RRGGBB, how]: how is 'hex', 'short', 'alpha', 'absent' or 'invalid'. */
  function normaliseColour(value) {
    if (value === null || value === undefined) return [null, 'absent'];
    if (!isStr(value)) return [null, 'invalid'];
    var text = trim(value);
    var found = HEX6.exec(text);
    if (found) return [found[1].toUpperCase(), 'hex'];
    found = HEX3.exec(text);
    if (found) {
      var short = found[1].toUpperCase();
      return [short.charAt(0) + short.charAt(0) + short.charAt(1) + short.charAt(1) + short.charAt(2) + short.charAt(2), 'short'];
    }
    found = HEX8.exec(text);
    if (found) return [found[1].toUpperCase(), 'alpha'];
    return [null, 'invalid'];
  }

  function colour(raw, where, notes, fallback) {
    fallback = fallback || 'the default colour is used';
    var normalised = normaliseColour(raw), value = normalised[0], how = normalised[1];
    if (how === 'short') note(notes, 'advisory', where + ': ' + show(raw) + ' read as #' + value);
    else if (how === 'alpha') note(notes, 'advisory', where + ': ' + show(raw) + ' read as #' + value + ' — the alpha is dropped');
    else if (how === 'invalid') note(notes, 'rejected', where + ': ' + show(raw) + ' is not a #RRGGBB colour — ' + fallback);
    return value;
  }

  // ================================================================== rule 3: series, categories

  function seriesStyle(entry, where, notes) {
    var out = {};
    out.dash = dash(get(entry, 'dash'), where + '.dash', notes);
    out.lineWidth = ranged(get(entry, 'lineWidth'), where + '.lineWidth', notes);
    out.smooth = boolOption(get(entry, 'smooth'), where + '.smooth', notes);
    var markerValue = has(entry, 'marker') ? get(entry, 'marker') : get(entry, 'markers');
    out.marker = marker(markerValue, where + '.marker', notes);
    out.markerSize = ranged(get(entry, 'markerSize'), where + '.markerSize', notes);
    out.explosion = ranged(get(entry, 'explosion'), where + '.explosion', notes);
    out.line = boolOption(get(entry, 'line'), where + '.line', notes);
    return out;
  }

  function boolOption(raw, where, notes) {
    if (raw === null || isBool(raw)) return raw;
    note(notes, 'rejected', where + ': ' + show(raw) + ' is not true or false — ignored');
    return null;
  }

  function marker(raw, where, notes) {
    if (raw === null || isBool(raw)) return raw;
    if (isStr(raw) && has(MARKER_STYLES, raw.toLowerCase())) return raw.toLowerCase();
    note(notes, 'rejected', where + ': ' + show(raw) + ' is not a marker style — ignored');
    return null;
  }

  function dash(raw, where, notes) {
    if (raw === null) return null;
    if (isStr(raw) && has(DASH_NAMES, raw)) return raw;
    if (isList(raw) && raw.length && raw.every(function (v) { return isReal(v) && v > 0 && v <= DASH_MAX_PX; })) {
      return raw.map(toFloat);
    }
    note(notes, 'rejected', where + ': ' + (!isList(raw) ? show(raw) : 'that list') + ' is not a ' +
      'dash (a preset name or px lengths above 0 and at most ' + jsNumberString(DASH_MAX_PX) + ') — ignored');
    return null;
  }

  function readSeries(spec, family, count, notes) {
    var rawSeries = get(spec, 'series');
    if (!isList(rawSeries)) {
      if (rawSeries !== null) note(notes, 'rejected', 'series must be a list — ignored');
      return [];
    }
    var xy = family === 'scatter';
    var series = [];
    for (var index = 0; index < rawSeries.length; index++) {
      var entry = rawSeries[index];
      var where = 'series[' + index + ']';
      if (!isDict(entry)) {
        note(notes, 'warn', where + ' is not an object — drawn as an empty series');
        entry = {};
      }
      var keys = Object.keys(entry), k;
      for (k = 0; k < keys.length; k++) {
        if (!has(SERIES_KEYS, keys[k])) note(notes, 'rejected', where + ": unknown key '" + keys[k] + "' ignored");
      }
      var comboKeys = ['chartType', 'axis'];
      for (k = 0; k < comboKeys.length; k++) {
        if (has(entry, comboKeys[k])) {
          note(notes, 'rejected', where + '.' + comboKeys[k] + ': combo series are deferred to round 2 — ignored');
        }
      }

      var name = get(entry, 'name') !== null ? asText(get(entry, 'name')) : null;
      if (name === null) {
        if (get(entry, 'name') !== null) {
          note(notes, 'warn', where + '.name: ' + show(get(entry, 'name')) + ' is not a string — ' +
            "named 'Series " + (index + 1) + "'");
        }
        name = 'Series ' + (index + 1);
      }

      var out = { name: name, values: [], x: null, y: null, types: null };
      if (xy) {
        var xs = numberList(get(entry, 'x'), where + '.x', notes);
        var rawY = has(entry, 'y') ? get(entry, 'y') : get(entry, 'values');
        var ys = numberList(rawY, where + '.y', notes);
        if (xs === null || ys === null) {
          note(notes, 'warn', where + ' needs x and y lists — drawn empty');
          xs = xs && xs.length ? xs : [];
          ys = ys && ys.length ? ys : [];
        }
        if (xs.length !== ys.length) {
          note(notes, 'warn', where + ': x has ' + xs.length + ' entries and y ' + ys.length + ' — the extra ' +
            'points are dropped');
          var length = Math.min(xs.length, ys.length);
          xs = xs.slice(0, length);
          ys = ys.slice(0, length);
        }
        out.x = xs;
        out.y = ys;
        out.values = ys.slice();
      } else {
        var values = numberList(get(entry, 'values'), where + '.values', notes);
        if (values === null) {
          note(notes, 'warn', where + ' needs a values list — drawn as gap slots');
          values = [];
        }
        if (values.length < count) {
          if (get(entry, 'values') !== null || values.length) {
            note(notes, 'warn', where + '.values has ' + values.length + ' entries for ' + count + ' ' +
              'categories — the rest are gap slots');
          }
          while (values.length < count) values.push(null);
        } else if (values.length > count) {
          note(notes, 'warn', where + '.values has ' + values.length + ' entries for ' + count + ' ' +
            'categories — the extra values are dropped');
          values = values.slice(0, count);
        }
        out.values = values;
      }
      var styled = seriesStyle(entry, where, notes);
      for (var key in styled) if (Object.prototype.hasOwnProperty.call(styled, key)) out[key] = styled[key];
      out.labels = labelTexts(get(entry, 'dataLabels'), where + '.dataLabels', out.values.length, notes);
      series.push(out);
    }
    return series;
  }

  /** `series[k].dataLabels`: each point's label text in place of its formatted value (`_label_texts`). */
  function labelTexts(raw, where, count, notes) {
    if (raw === null) return null;
    if (!isList(raw)) {
      note(notes, 'rejected', where + ' must be a list of label texts, one per point (null keeps the ' +
        'value) — ignored');
      return null;
    }
    var out = [];
    for (var index = 0; index < raw.length && index < count; index++) {
      var value = raw[index];
      if (value === null || isStr(value)) {
        out.push(value);
      } else if (isReal(value)) {
        var text = jsNumberString(value);
        note(notes, 'advisory', where + '[' + index + ']: ' + show(value) + ' read as the text ' + show(text));
        out.push(text);
      } else {
        note(notes, 'rejected', where + '[' + index + ']: ' + show(value) + ' is not a label text — the point ' +
          'keeps its value');
        out.push(null);
      }
    }
    if (raw.length !== count) {
      var tail = raw.length < count ? 'the rest keep their values' : 'the extra texts are dropped';
      note(notes, 'advisory', where + ' has ' + raw.length + ' entries for ' + count + ' points — ' + tail);
    }
    while (out.length < count) out.push(null);
    return out.some(function (t) { return t !== null; }) ? out : null;
  }

  function readCategories(spec, notes) {
    var raw = get(spec, 'categories');
    if (raw === null) return [];
    if (!isList(raw)) {
      note(notes, 'rejected', 'categories must be a list — ignored');
      return [];
    }
    var out = [];
    for (var index = 0; index < raw.length; index++) {
      var text = asText(raw[index]);
      if (text === null) {
        note(notes, 'warn', 'categories[' + index + ']: ' + show(raw[index]) + ' is not a string — read as a gap slot');
        text = '';
      }
      out.push(text);
    }
    return out;
  }

  // ======================================================================== rule 5: options

  /** A 100 % stacked chart's value axis: 0–100 in the spec and the model, 0–1 in the file. */
  function isPercentAxis(family, stacked) {
    return stacked === 'stacked100' && (family === 'column' || family === 'bar' || family === 'line' ||
      family === 'area');
  }

  /** `_percent_axis`: a value of at most 1 in size (0 aside) is a fraction, read as that percent. */
  function percentAxis(axisOut, where, notes) {
    var keys = ['min', 'max', 'majorUnit', 'minorUnit'];
    for (var i = 0; i < keys.length; i++) {
      var value = axisOut[keys[i]];
      if (value === null || value === 0 || Math.abs(value) > 1) continue;
      var percent = roundTo(value * 100.0, 9);
      note(notes, 'advisory', where + '.' + keys[i] + ': ' + jsNumberString(value) + ' is read as a fraction, ' +
        jsNumberString(percent) + ' % — a 100 % chart\'s axis is stated in percent (0–100)');
      axisOut[keys[i]] = percent;
    }
    if (axisOut.min !== null && axisOut.max !== null && axisOut.min >= axisOut.max) {
      note(notes, 'rejected', where + '.min must be below ' + where + '.max — both ignored');
      axisOut.min = axisOut.max = null;
    }
  }

  function axis(raw, where, notes) {
    var out = { visible: true, min: null, max: null, majorUnit: null, minorUnit: null, format: null,
      reverse: false, title: null, bold: false };
    if (raw === null) return out;
    if (isBool(raw)) {
      note(notes, 'advisory', where + ': ' + show(raw) + ' read as {visible: ' + show(raw) + '}');
      out.visible = raw;
      return out;
    }
    if (!isDict(raw)) {
      note(notes, 'rejected', where + ' must be an object — ignored');
      return out;
    }
    var keys = Object.keys(raw), i;
    for (i = 0; i < keys.length; i++) {
      if (!has(AXIS_KEYS, keys[i])) note(notes, 'rejected', where + ": unknown key '" + keys[i] + "' ignored");
    }
    var numeric = ['min', 'max', 'majorUnit', 'minorUnit'];
    for (i = 0; i < numeric.length; i++) {
      if (get(raw, numeric[i]) !== null) out[numeric[i]] = numberOption(get(raw, numeric[i]), where + '.' + numeric[i], notes);
    }
    var units = ['majorUnit', 'minorUnit'];
    for (i = 0; i < units.length; i++) {
      if (out[units[i]] !== null && out[units[i]] <= 0) {
        note(notes, 'rejected', where + '.' + units[i] + ' must be above 0 — ignored');
        out[units[i]] = null;
      }
    }
    if (out.min !== null && out.max !== null && out.min >= out.max) {
      note(notes, 'rejected', where + '.min must be below ' + where + '.max — both ignored');
      out.min = out.max = null;
    }
    var flags = ['visible', 'reverse', 'bold'];
    for (i = 0; i < flags.length; i++) {
      var value = get(raw, flags[i]);
      if (value !== null) {
        if (isBool(value)) out[flags[i]] = value;
        else note(notes, 'rejected', where + '.' + flags[i] + ': ' + show(value) + ' is not true or false — ignored');
      }
    }
    var texts = ['format', 'title'];
    for (i = 0; i < texts.length; i++) {
      var text = get(raw, texts[i]);
      if (text !== null) {
        if (isStr(text)) out[texts[i]] = text;
        else note(notes, 'rejected', where + '.' + texts[i] + ': ' + show(text) + ' is not a string — ignored');
      }
    }
    return out;
  }

  function plotArea(raw, notes) {
    if (raw === null) return null;
    var keys = ['x', 'y', 'w', 'h'], i;
    if (!isDict(raw) || keys.some(function (key) { return !has(raw, key); })) {
      note(notes, 'rejected', 'options.plotArea must be an object with x, y, w and h — ignored');
      return null;
    }
    var values = {};
    for (i = 0; i < keys.length; i++) {
      var coerced = coerceNumber(raw[keys[i]]);
      if (coerced[0] === null) {
        note(notes, 'rejected', 'options.plotArea.' + keys[i] + ': ' + show(raw[keys[i]]) + ' is not a number — ' +
          'plotArea ignored');
        return null;
      }
      if (coerced[1] === 'string') {
        note(notes, 'advisory', 'options.plotArea.' + keys[i] + ': ' + show(raw[keys[i]]) + ' read as ' +
          jsNumberString(coerced[0]));
      }
      values[keys[i]] = coerced[0];
    }
    if (!(values.x >= 0 && values.x <= 1 && values.y >= 0 && values.y <= 1 && values.w > 0 && values.w <= 1 &&
          values.h > 0 && values.h <= 1)) {
      note(notes, 'rejected', 'options.plotArea values are fractions of the frame in [0, 1] — ignored');
      return null;
    }
    if (values.x + values.w > 1.001 || values.y + values.h > 1.001) {
      note(notes, 'rejected', 'options.plotArea runs outside the chart frame — ignored');
      return null;
    }
    return { x: roundTo(values.x, 4), y: roundTo(values.y, 4), w: roundTo(values.w, 4), h: roundTo(values.h, 4) };
  }

  function gridlines(raw, notes) {
    if (raw === null || raw === false) return false;
    if (raw === true) return copyObject(GRIDLINES_DEFAULT);
    if (!isDict(raw)) {
      note(notes, 'rejected', 'options.gridlines: ' + show(raw) + ' is not a bool or {color, width, dash} — ignored');
      return false;
    }
    var out = copyObject(GRIDLINES_DEFAULT);
    var keys = Object.keys(raw);
    for (var i = 0; i < keys.length; i++) {
      if (['color', 'width', 'dash'].indexOf(keys[i]) < 0) {
        note(notes, 'rejected', "options.gridlines: unknown key '" + keys[i] + "' ignored");
      }
    }
    if (get(raw, 'color') !== null) {
      out.color = colour(raw.color, 'options.gridlines.color', notes) || GRIDLINES_DEFAULT.color;
    }
    if (get(raw, 'width') !== null) {
      var width = ranged(raw.width, 'options.gridlines.width', notes);
      if (width !== null) out.width = width;
    }
    out.dash = dash(get(raw, 'dash'), 'options.gridlines.dash', notes);
    return out;
  }

  function legend(raw, authoredSeries, notes) {
    if (raw === null) return authoredSeries > 1 ? 'bottom' : false;
    if (raw === false) return false;
    if (raw === true) return 'bottom';
    if (isStr(raw) && has(LEGEND_POSITIONS, raw)) return raw;
    note(notes, 'rejected', 'options.legend: ' + show(raw) + ' is not false or one of bottom, top, left, ' +
      'right, topRight — ignored');
    return authoredSeries > 1 ? 'bottom' : false;
  }

  function dataLabels(raw, count, notes) {
    var out = [], i;
    if (raw === null) { for (i = 0; i < count; i++) out.push(true); return out; }
    if (isBool(raw)) { for (i = 0; i < count; i++) out.push(raw); return out; }
    if (isList(raw) && raw.every(isBool)) {
      if (raw.length !== count) {
        note(notes, 'advisory', 'options.dataLabels has ' + raw.length + ' entries for ' + count + ' series — ' +
          'a series without an entry has no labels');
      }
      for (i = 0; i < count; i++) out.push(i < raw.length ? Boolean(raw[i]) : false);
      return out;
    }
    note(notes, 'rejected', 'options.dataLabels must be a bool or a list of bools, one per series — ignored');
    for (i = 0; i < count; i++) out.push(true);
    return out;
  }

  /** `_label_style`: `options.labelStyle` — `{bold, color, fontSize}` for every data label. */
  function labelStyle(raw, notes) {
    var out = { bold: false, color: null, fontSize: null };
    if (raw === null) return out;
    if (!isDict(raw)) {
      note(notes, 'rejected', 'options.labelStyle must be an object {bold, color, fontSize} — ignored');
      return out;
    }
    var keys = Object.keys(raw);
    for (var i = 0; i < keys.length; i++) {
      if (!has(LABEL_STYLE_KEYS, keys[i])) note(notes, 'rejected', "options.labelStyle: unknown key '" + keys[i] + "' ignored");
    }
    var bold = get(raw, 'bold');
    if (bold !== null) {
      if (isBool(bold)) out.bold = bold;
      else note(notes, 'rejected', 'options.labelStyle.bold: ' + show(bold) + ' is not true or false — ignored');
    }
    if (get(raw, 'color') !== null) {
      out.color = colour(raw.color, 'options.labelStyle.color', notes, 'the labels keep their own ink');
    }
    out.fontSize = ranged(get(raw, 'fontSize'), 'options.labelStyle.fontSize', notes);
    return out;
  }

  /** `label_size`: the labels' size in px — `labelStyle.fontSize` (clamped 5–48) when given. */
  function labelSize(model, size) {
    var style = get(model.options, 'labelStyle');
    var wanted = style ? style.fontSize : null;
    if (wanted === null || wanted === undefined) return size;
    return Math.min(48.0, Math.max(5.0, wanted));
  }

  function connectors(raw, legacyColour, notes) {
    var out = { color: CONNECTORS_DEFAULT.color, width: CONNECTORS_DEFAULT.width, dash: CONNECTORS_DEFAULT.dash.slice() };
    if (raw === false) return false;
    if (raw !== null && raw !== true) {
      if (!isDict(raw)) {
        note(notes, 'rejected', 'options.connectors: ' + show(raw) + ' is not false or {color, width, dash} — ignored');
      } else {
        var keys = Object.keys(raw);
        for (var i = 0; i < keys.length; i++) {
          if (['color', 'width', 'dash'].indexOf(keys[i]) < 0) {
            note(notes, 'rejected', "options.connectors: unknown key '" + keys[i] + "' ignored");
          }
        }
        if (get(raw, 'color') !== null) out.color = colour(raw.color, 'options.connectors.color', notes) || out.color;
        if (get(raw, 'width') !== null) {
          var width = ranged(raw.width, 'options.connectors.width', notes);
          if (width !== null) out.width = width;
        }
        if (get(raw, 'dash') !== null) {
          var dashValue = dash(raw.dash, 'options.connectors.dash', notes);
          if (dashValue !== null) out.dash = dashValue;
        }
      }
    }
    if (legacyColour !== null) {
      note(notes, 'advisory', 'options.connectorColor is the old spelling of options.connectors.color — read as that');
      var legacy = colour(legacyColour, 'options.connectorColor', notes);
      if (legacy !== null && !(isDict(raw) && get(raw, 'color') !== null)) out.color = legacy;
    }
    return out;
  }

  function referenceLines(raw, notes) {
    if (raw === null) return [];
    if (!isList(raw)) {
      note(notes, 'rejected', 'options.referenceLines must be a list — ignored');
      return [];
    }
    var out = [];
    for (var index = 0; index < raw.length; index++) {
      var line = raw[index];
      var where = 'options.referenceLines[' + index + ']';
      if (!isDict(line)) {
        note(notes, 'rejected', where + ' is not an object — left out');
        continue;
      }
      var keys = Object.keys(line);
      for (var i = 0; i < keys.length; i++) {
        if (['value', 'color', 'dash', 'label', 'width'].indexOf(keys[i]) < 0) {
          note(notes, 'rejected', where + ": unknown key '" + keys[i] + "' ignored");
        }
      }
      var value = numberOption(get(line, 'value'), where + '.value', notes, 'rejected', 'the line is left out');
      if (value === null) {
        if (get(line, 'value') === null) note(notes, 'rejected', where + ' has no value — the line is left out');
        continue;
      }
      var entry = {
        value: value,
        color: REFERENCE_LINE_DEFAULT.color,
        width: REFERENCE_LINE_DEFAULT.width,
        dash: REFERENCE_LINE_DEFAULT.dash.slice(),
        label: null
      };
      if (get(line, 'color') !== null) entry.color = colour(line.color, where + '.color', notes) || entry.color;
      if (get(line, 'width') !== null) {
        var width = ranged(line.width, where + '.width', notes);
        if (width !== null) entry.width = width;
      }
      if (has(line, 'dash')) {
        if (get(line, 'dash') === null) {
          entry.dash = null;
        } else {
          var dashValue = dash(line.dash, where + '.dash', notes);
          if (dashValue !== null) entry.dash = dashValue;
        }
      }
      var label = get(line, 'label');
      if (label !== null) {
        var text = asText(label);
        if (text === null) note(notes, 'rejected', where + '.label: ' + show(label) + ' is not a string — ignored');
        else entry.label = text;
      }
      out.push(entry);
    }
    return out;
  }

  // ===================================================================== rule 6: pointColors

  function pointColorTable(raw, authoredSeries, categoryCount, notes) {
    if (raw === null) return {};
    var table = {};

    function put(seriesIndex, pointIndex, value, where) {
      if (seriesIndex >= Math.max(authoredSeries, 1) || (categoryCount !== null && pointIndex >= categoryCount)) {
        note(notes, 'rejected', where + ' is outside the chart — ignored');
        return;
      }
      var chosen = colour(value, where, notes, 'ignored');
      if (chosen !== null) {
        if (!has(table, String(seriesIndex))) table[String(seriesIndex)] = {};
        table[String(seriesIndex)][String(pointIndex)] = chosen;
      }
    }

    function indexOf(key) {
      if (isStr(key) && INDEX_KEY.test(key)) return parseInt(key, 10);
      return null;
    }

    var flatKind = authoredSeries > 1 ? 'warn' : 'advisory';
    var keys, i;
    if (isList(raw)) {
      note(notes, flatKind, 'options.pointColors is a list: read as one colour per point of ' +
        'series 0' + (authoredSeries > 1 ? ' (the chart has several series)' : ''));
      for (var point = 0; point < raw.length; point++) {
        if (raw[point] === null) continue;
        put(0, point, raw[point], 'options.pointColors[' + point + ']');
      }
      return sortedTable(table);
    }
    if (!isDict(raw)) {
      note(notes, 'rejected', 'options.pointColors must be an object keyed by series index — ignored');
      return {};
    }
    keys = Object.keys(raw);
    var nested = keys.some(function (key) { return isDict(raw[key]); });
    if (!nested) {
      note(notes, flatKind, 'options.pointColors maps points to colours: read as series 0' +
        (authoredSeries > 1 ? ' (the chart has several series)' : ''));
      for (i = 0; i < keys.length; i++) {
        var pointIndex = indexOf(keys[i]);
        if (pointIndex === null) {
          note(notes, 'rejected', 'options.pointColors: key ' + show(keys[i]) + ' is not a point index — ignored');
          continue;
        }
        put(0, pointIndex, raw[keys[i]], 'options.pointColors[' + show(String(keys[i])) + ']');
      }
      return sortedTable(table);
    }
    for (i = 0; i < keys.length; i++) {
      var key = keys[i], points = raw[key];
      var seriesIndex = indexOf(key);
      if (seriesIndex === null) {
        note(notes, 'rejected', 'options.pointColors: key ' + show(key) + ' is not a series index — ignored');
        continue;
      }
      if (!isDict(points)) {
        note(notes, 'rejected', 'options.pointColors[' + show(String(key)) + '] must be an object keyed by ' +
          'point index — ignored');
        continue;
      }
      var pointKeys = Object.keys(points);
      for (var p = 0; p < pointKeys.length; p++) {
        var pointAt = indexOf(pointKeys[p]);
        if (pointAt === null) {
          note(notes, 'rejected', 'options.pointColors[' + show(String(key)) + ']: key ' + show(pointKeys[p]) + ' is ' +
            'not a point index — ignored');
          continue;
        }
        put(seriesIndex, pointAt, points[pointKeys[p]],
          'options.pointColors[' + show(String(key)) + '][' + show(String(pointKeys[p])) + ']');
      }
    }
    return sortedTable(table);
  }

  function sortedTable(table) {
    var out = {};
    var series = numericSort(Object.keys(table).map(Number));
    for (var s = 0; s < series.length; s++) {
      var row = table[String(series[s])], sorted = {};
      var points = numericSort(Object.keys(row).map(Number));
      for (var p = 0; p < points.length; p++) sorted[String(points[p])] = row[String(points[p])];
      out[String(series[s])] = sorted;
    }
    return out;
  }

  // ================================================================== options, all together

  function normaliseOptions(spec, kind, categories, series, notes) {
    var raw = get(spec, 'options');
    if (raw === null) raw = {};
    if (!isDict(raw)) {
      note(notes, 'rejected', 'options must be an object — ignored');
      raw = {};
    }
    var family = kind.family, stacked = kind.stacked;
    var waterfall = family === 'waterfall';
    var keys = Object.keys(raw), i;
    for (i = 0; i < keys.length; i++) {
      if (!has(OPTION_KEYS, keys[i])) note(notes, 'rejected', "unknown chart option '" + keys[i] + "' ignored");
    }
    if (get(raw, 'secondaryValueAxis') !== null) {
      note(notes, 'rejected', 'options.secondaryValueAxis belongs to a combo chart (round 2) — ignored');
    }

    var out = {};
    for (i = 0; i < OPTION_ORDER.length; i++) out[OPTION_ORDER[i]] = null;
    var count = series.length;
    out.dataLabels = dataLabels(get(raw, 'dataLabels'), count, notes);

    var rule = labelPositionRule(family, stacked), allowed = rule[0], defaultPosition = rule[1];
    var position = get(raw, 'labelPosition');
    if (isStr(position) && has(LABEL_POSITION_ALIASES, position)) {
      note(notes, 'advisory', 'options.labelPosition: ' + show(position) + ' read as ' + show(LABEL_POSITION_ALIASES[position]));
      position = LABEL_POSITION_ALIASES[position];
    }
    if (position !== null && !(isStr(position) && has(LABEL_POSITIONS, position))) {
      note(notes, 'rejected', 'options.labelPosition: ' + show(position) + ' is not a label position — ignored');
      position = null;
    }
    if (position !== null && !has(allowed, position)) {
      var tail = defaultPosition ? "; using '" + defaultPosition + "' instead" : '; omitted';
      note(notes, 'advisory', "labelPosition '" + position + "' is not available on a " + kind.name + ' chart' +
        tail + ' (PowerPoint reports the file as corrupt otherwise)');
      position = defaultPosition;
    }
    out.labelPosition = position !== null ? position : defaultPosition;

    var textKeys = ['numberFormat', 'labelFormat', 'font', 'title'];
    for (i = 0; i < textKeys.length; i++) {
      var value = get(raw, textKeys[i]);
      if (value !== null && !isStr(value)) {
        note(notes, 'rejected', 'options.' + textKeys[i] + ': ' + show(value) + ' is not a string — ignored');
        value = null;
      }
      out[textKeys[i]] = value;
    }
    if (out.numberFormat === null) out.numberFormat = 'General';
    var title = get(spec, 'title');
    if (title !== null) {
      if (isStr(title)) out.title = title;
      else note(notes, 'rejected', 'title: ' + show(title) + ' is not a string — ignored');
    }
    out.labelStyle = labelStyle(get(raw, 'labelStyle'), notes);

    out.gridlines = gridlines(get(raw, 'gridlines'), notes);
    out.legend = legend(get(raw, 'legend'), count, notes);

    var gap = ranged(get(raw, 'gapWidth'), 'options.gapWidth', notes);
    out.gapWidth = gap !== null ? gap : DEFAULT_GAP_WIDTH;
    var overlap = ranged(get(raw, 'overlap'), 'options.overlap', notes);
    if (overlap !== null && overlap !== 100 && stacked && (family === 'column' || family === 'bar' || family === 'waterfall')) {
      note(notes, 'advisory', 'options.overlap: the bars of a ' + kind.name + ' chart stack on each ' +
        'other — ' + jsNumberString(overlap) + ' read as 100');
      overlap = 100.0;
    }
    out.overlap = overlap !== null ? overlap : (stacked ? 100.0 : 0.0);

    out.valueAxis = axis(get(raw, 'valueAxis'), 'options.valueAxis', notes);
    if (isPercentAxis(family, stacked)) percentAxis(out.valueAxis, 'options.valueAxis', notes);
    out.categoryAxis = axis(get(raw, 'categoryAxis'), 'options.categoryAxis', notes);
    out.plotArea = plotArea(get(raw, 'plotArea'), notes);
    out.pointColors = pointColorTable(get(raw, 'pointColors'), count,
      family === 'scatter' ? null : categories.length, notes);

    var rangedKeys = ['fontSize', 'holeSize', 'markerSize', 'lineWidth', 'explosion'];
    for (i = 0; i < rangedKeys.length; i++) out[rangedKeys[i]] = ranged(get(raw, rangedKeys[i]), 'options.' + rangedKeys[i], notes);
    if (out.holeSize !== null && family !== 'pie' && family !== 'doughnut') {
      note(notes, 'rejected', 'options.holeSize has meaning on a pie or doughnut chart only — ignored');
      out.holeSize = null;
    }
    if (get(raw, 'fontColor') !== null) out.fontColor = colour(raw.fontColor, 'options.fontColor', notes);
    var flagKeys = ['smooth', 'varyColors'];
    for (i = 0; i < flagKeys.length; i++) out[flagKeys[i]] = boolOption(get(raw, flagKeys[i]), 'options.' + flagKeys[i], notes);
    out.markers = marker(get(raw, 'markers'), 'options.markers', notes);
    out.dash = dash(get(raw, 'dash'), 'options.dash', notes);
    out.referenceLines = referenceLines(get(raw, 'referenceLines'), notes);

    if (waterfall) {
      var base = get(raw, 'baseValue');
      out.baseValue = base !== null ? numberOption(base, 'options.baseValue', notes) : null;
      out.connectors = connectors(get(raw, 'connectors'), get(raw, 'connectorColor'), notes);
    } else {
      var waterfallKeys = ['totals', 'totalIndices', 'connectors', 'connectorColor', 'baseValue'];
      for (i = 0; i < waterfallKeys.length; i++) {
        if (get(raw, waterfallKeys[i]) !== null) {
          note(notes, 'rejected', 'options.' + waterfallKeys[i] + ' only has meaning on a waterfall — ignored');
        }
      }
      var rawSeries = isList(get(spec, 'series')) ? spec.series : [];
      for (var index = 0; index < rawSeries.length; index++) {
        if (isDict(rawSeries[index])) {
          var seriesKeys = ['totals', 'types'];
          for (var k = 0; k < seriesKeys.length; k++) {
            if (get(rawSeries[index], seriesKeys[k]) !== null) {
              note(notes, 'rejected', 'series[' + index + '].' + seriesKeys[k] + ' only has meaning on a waterfall — ignored');
            }
          }
        }
      }
    }
    return out;
  }

  // ============================================================== the waterfall: totals (§3.1)

  function totalsList(raw, where, count, notes) {
    if (raw === null) return null;
    if (!isList(raw)) {
      note(notes, 'rejected', where + ' must be a list of category indices or of true/false flags — ignored');
      return null;
    }
    if (!raw.length) return [];
    var flags = raw.map(isBool);
    var words = raw.map(isStr);
    var allFlags = flags.every(Boolean), anyFlags = flags.some(Boolean);
    var allWords = words.every(Boolean), anyWords = words.some(Boolean);
    var index, position;
    if (allFlags) {
      if (raw.length !== count) {
        note(notes, 'warn', where + ' has ' + raw.length + ' flags for ' + count + ' categories — read position by position');
      }
      var flagged = [];
      for (index = 0; index < raw.length; index++) if (raw[index] && index < count) flagged.push(index);
      return flagged;
    }
    if (allWords) return totalsWords(raw, where, count, notes);
    if (anyFlags || anyWords) {
      note(notes, 'rejected', where + ' mixes category indices, true/false flags and words — ignored');
      return null;
    }
    var indices = [];
    for (position = 0; position < raw.length; position++) {
      var value = raw[position];
      if (isReal(value) && value === Math.floor(value)) {
        index = value;
        if (index >= 0 && index < count) {
          indices.push(index);
          continue;
        }
      }
      note(notes, 'rejected', where + '[' + position + ']: ' + show(value) + ' is not a category index ' +
        '(0..' + (count - 1) + ') — ignored');
    }
    if (!indices.length) return null;
    var unique = numericSort(keysOf(setOf(indices)).map(Number));
    if (raw.length === count && raw.every(function (v) { return isReal(v) && (v === 0 || v === 1); })) {
      note(notes, 'advisory', where + ' ' + showList(raw) + ' reads as category indices ' +
        showList(unique) + '; to mark bars by position write true/false');
    }
    return unique;
  }

  function totalsWords(raw, where, count, notes) {
    var indices = [];
    var usable = 0;
    for (var position = 0; position < raw.length; position++) {
      var key = trim(raw[position]).toLowerCase();
      if (!has(TYPE_WORDS, key)) {
        note(notes, 'rejected', where + '[' + position + ']: ' + show(raw[position]) + ' is not a bar type (total, increase, ' +
          'decrease, step and their synonyms) — ignored');
        continue;
      }
      usable += 1;
      if (TYPE_WORDS[key] === 'total' && position < count) indices.push(position);
    }
    if (!usable) return null;
    if (raw.length !== count) {
      note(notes, 'warn', where + ' has ' + raw.length + ' words for ' + count + ' categories — read position by position');
    }
    note(notes, 'advisory', where + ' gives a word per bar: read as category indices ' +
      showList(indices) + ' — write ' + where + ': ' + showList(indices));
    return indices;
  }

  function typesList(raw, where, count, notes) {
    if (raw === null) return null;
    if (!isList(raw)) {
      note(notes, 'rejected', where + ' must be a list of words, one per bar — ignored');
      return null;
    }
    var out = [];
    for (var index = 0; index < raw.length; index++) {
      var word = raw[index];
      if (word === null) {
        out.push(null);
        continue;
      }
      var key = isStr(word) ? trim(word).toLowerCase() : null;
      if (key === null || !has(TYPE_WORDS, key)) {
        note(notes, 'rejected', where + '[' + index + ']: ' + show(word) + ' is not a bar type (total, increase, ' +
          'decrease, step and their synonyms) — ignored');
        out.push(null);
        continue;
      }
      out.push(TYPE_WORDS[key]);
    }
    if (out.length !== count) {
      if (out.length > count) {
        note(notes, 'warn', where + ' has ' + out.length + ' entries for ' + count + ' categories — the extra entries are ignored');
        out = out.slice(0, count);
      } else {
        note(notes, 'warn', where + ' has ' + out.length + ' entries for ' + count + ' categories — the rest are undeclared');
        while (out.length < count) out.push(null);
      }
    }
    return out;
  }

  function totalsOf(spec, series, count, baseValue, notes, categories, valueAxis, labelFormat) {
    var rawOptions = isDict(get(spec, 'options')) ? spec.options : {};
    var rawSeries = isList(get(spec, 'series')) ? spec.series : [];
    var union = {};
    var declared = false;
    var spellings = [];
    var i;
    function add(list) { for (var n = 0; n < list.length; n++) union[list[n]] = true; }

    var fromOptions = totalsList(get(rawOptions, 'totals'), 'options.totals', count, notes);
    if (fromOptions !== null) {
      declared = true;
      spellings.push('options.totals');
      add(fromOptions);
    }
    if (get(rawOptions, 'totalIndices') !== null) {
      note(notes, 'advisory', 'options.totalIndices is the old spelling of options.totals — read as totals');
      var legacy = totalsList(get(rawOptions, 'totalIndices'), 'options.totalIndices', count, notes);
      if (legacy !== null) {
        declared = true;
        spellings.push('options.totalIndices');
        add(legacy);
      }
    }

    var types = [];
    var typedTotalBy = {};
    var typedStepBy = {};
    for (var k = 0; k < rawSeries.length; k++) {
      var entry = isDict(rawSeries[k]) ? rawSeries[k] : {};
      var own = totalsList(get(entry, 'totals'), 'series[' + k + '].totals', count, notes);
      if (own !== null) {
        declared = true;
        if (spellings.indexOf('series.totals') < 0) spellings.push('series.totals');
        add(own);
      }
      var words = typesList(get(entry, 'types'), 'series[' + k + '].types', count, notes);
      if (words === null) {
        words = [];
        for (i = 0; i < count; i++) words.push(null);
      }
      types.push(words);
      if (k < series.length) {
        var values = series[k].values;
        for (i = 0; i < words.length; i++) {
          var word = words[i];
          if (word === 'total') {
            if (!has(typedTotalBy, String(i))) typedTotalBy[String(i)] = [];
            typedTotalBy[String(i)].push(k);
          } else if (word !== null) {
            if (!has(typedStepBy, String(i))) typedStepBy[String(i)] = [];
            typedStepBy[String(i)].push(k);
          }
          if ((word === 'increase' || word === 'decrease') && i < values.length && values[i] !== null) {
            var value = values[i];
            var signed = word === 'increase' ? Math.abs(value) : -Math.abs(value);
            signed = toFloat(signed);
            if (signed !== value) {
              note(notes, 'advisory', 'series[' + k + '].types[' + i + "]: '" + word + "' with a value of " +
                jsNumberString(value) + ' — read as ' + jsNumberString(signed));
              values[i] = signed;
            }
          }
        }
      }
    }
    var typedTotals = numericSort(Object.keys(typedTotalBy).map(Number));
    if (typedTotals.length) {
      declared = true;
      if (spellings.indexOf('series.types') < 0) spellings.push('series.types');
      add(typedTotals);
    }
    for (i = 0; i < typedTotals.length; i++) {
      if (has(typedStepBy, String(typedTotals[i]))) {
        note(notes, 'advisory', 'series disagree about whether category ' + typedTotals[i] + ' is a total — read as a total');
      }
    }
    if (spellings.length > 1) {
      note(notes, 'advisory', 'totals are given in ' + spellings.length + ' ways (' + spellings.join(', ') + ') — ' +
        'say it once: options.totals');
    }

    if (declared) return { totals: numericSort(Object.keys(union).map(Number)), declared: true, types: types };
    if (baseValue !== null) return { totals: [], declared: false, types: types };
    return { totals: inferredTotals(series, count, typedStepBy, categories || [], valueAxis || null,
      labelFormat === undefined ? null : labelFormat), declared: false, types: types };
  }

  function categorySum(series, index) {
    var total = 0.0, present = false;
    for (var k = 0; k < series.length; k++) {
      var values = series[k].values;
      if (index < values.length && values[index] !== null) {
        total += values[index];
        present = true;
      }
    }
    return present ? total : null;
  }

  /** `_inferred_totals`: the first bar; a middle bar that equals the running sum (`subtotalTolerance`)
      and whose category reads as a subtotal (`SUBTOTAL_CATEGORY`) or which, as a step, would leave
      the authored value axis; and the last one when it equals the running sum, when its category
      reads as a total (`TOTAL_CATEGORY`), or when as a step it would end above an authored
      `valueAxis.max` that it fits under standing on the axis. */
  function inferredTotals(series, count, typedSteps, categories, valueAxis, labelFormat) {
    var present = [];
    for (var i = 0; i < count; i++) if (categorySum(series, i) !== null) present.push(i);
    if (!present.length) return [];
    var totals = [];
    if (!has(typedSteps, String(present[0]))) totals.push(present[0]);
    if (present.length > 1) {
      var ceiling = valueAxis && valueAxis.max !== undefined ? valueAxis.max : null;
      var floor = valueAxis && valueAxis.min !== undefined ? valueAxis.min : null;
      var half = halfUnit(labelFormat, series);
      var running = categorySum(series, present[0]) || 0.0;
      for (var p = 1; p < present.length - 1; p++) {
        var middle = present[p];
        var value = categorySum(series, middle) || 0.0;
        if (!has(typedSteps, String(middle)) && Math.abs(value - running) <= subtotalTolerance(running, half)) {
          var called = middle < categories.length && SUBTOTAL_CATEGORY.test(categories[middle]);
          var end = running + value;
          var leaves = (ceiling !== null && end > ceiling && value <= ceiling) ||
            (floor !== null && end < floor && value >= floor);
          if (called || leaves) {
            totals.push(middle);
            running = value;
            continue;
          }
        }
        running += value;
      }
      var index = present[present.length - 1];
      if (!has(typedSteps, String(index))) {
        var last = categorySum(series, index) || 0.0;
        var equal = Math.abs(last - running) <= Math.max(1e-9, Math.abs(running) * 1e-6);
        var named = index < categories.length && TOTAL_CATEGORY.test(categories[index]);
        var overflows = ceiling !== null && running + last > ceiling && last <= ceiling;
        if (equal || named || overflows) totals.push(index);
      }
    }
    return totals;
  }

  /** `subtotal_tolerance`: 0.5 % of the running sum, or half a unit of the labels' last digit. */
  function subtotalTolerance(running, half) {
    return Math.max(1e-9, Math.abs(running) * SUBTOTAL_TOLERANCE, half || 0.0);
  }

  /** `_half_unit`: half a unit of the last digit the waterfall's labels show. */
  function halfUnit(labelFormat, series) {
    var fmt = labelFormat;
    if (fmt === null || fmt === undefined || trim(fmt).toLowerCase() === 'general') fmt = generalFormatFor(series);
    var unit = formatUnit(fmt);
    return unit === null ? null : unit / 2.0;
  }

  // ================================================================ the waterfall: plan (§3.2)

  function waterfallPlan(model, totals, notes) {
    var categories = model.categories;
    var series = model.series;
    var options = model.options;
    var count = categories.length;
    var authored = series.length;
    var baseValue = options.baseValue !== null ? options.baseValue : 0.0;
    var totalSet = setOf(totals.map(String));
    var i, k;

    var roles = [], tops = [], bottoms = [], levels = [], sums = [];
    var running = baseValue;
    for (i = 0; i < count; i++) {
      var present = [];
      for (k = 0; k < authored; k++) if (series[k].values[i] !== null) present.push(series[k].values[i]);
      if (!present.length) {
        roles.push('gap');
        tops.push(null);
        bottoms.push(null);
        levels.push(toFloat(running));
        sums.push(null);
        continue;
      }
      var value = 0.0;
      for (var n = 0; n < present.length; n++) value += present[n];
      value = toFloat(value);
      if (present.some(function (v) { return v > 0; }) && present.some(function (v) { return v < 0; })) {
        note(notes, 'warn', 'category ' + i + " ('" + categories[i] + "') mixes rises and falls across series — " +
          'drawn as the net step from the magnitudes');
      }
      var low, high, role;
      if (has(totalSet, String(i))) {
        low = Math.min(0.0, value);
        high = Math.max(0.0, value);
        role = 'total';
        running = value;
      } else {
        var start = running, end = toFloat(running + value);
        low = Math.min(start, end);
        high = Math.max(start, end);
        role = value >= 0 ? 'increase' : 'decrease';
        running = end;
      }
      roles.push(role);
      bottoms.push(toFloat(low));
      tops.push(toFloat(high));
      levels.push(toFloat(running));
      sums.push(value);
    }
    var negative = bottoms.some(function (b) { return b !== null && b < 0; });

    // ---- the series layout: [Base, S_0 … S_n−1] (+ [Base−, S_0− … S_n−1−] once any bar dips below 0)
    function blank() { var out = []; for (var c = 0; c < count; c++) out.push(null); return out; }
    var planSeries = [{ name: BASE_SERIES, values: blank(), visible: false, side: '+', source: null }];
    for (k = 0; k < authored; k++) {
      planSeries.push({ name: series[k].name, values: blank(), visible: true, side: '+', source: k });
    }
    if (negative) {
      planSeries.push({ name: BASE_SERIES + MINUS, values: blank(), visible: false, side: '-', source: null });
      for (k = 0; k < authored; k++) {
        planSeries.push({ name: series[k].name + MINUS, values: blank(), visible: true, side: '-', source: k });
      }
    }
    var plusIndex = {}, minusIndex = {};
    for (k = 0; k < authored; k++) plusIndex[k] = 1 + k;
    var minusBase = 1 + authored;
    for (k = 0; k < authored; k++) minusIndex[k] = minusBase + 1 + k;

    var fmt = options.labelFormat || options.numberFormat;
    if (fmt === null || trim(fmt).toLowerCase() === 'general') fmt = generalFormatFor(series);
    var formats = { increase: signedFormat(fmt, '+'), decrease: signedFormat(fmt, '-'), total: fmt };
    var segments = [];

    /* The author's own text for series k's label at bar i (`series[k].dataLabels`), or null. */
    function custom(kk, ii) {
      var texts = series[kk].labels;
      return texts !== null && texts !== undefined && ii < texts.length ? texts[ii] : null;
    }
    /* `outEnd` labels are one per bar, not per series: a single series' texts apply to them. */
    var outTexts = [];
    for (i = 0; i < count; i++) outTexts.push(null);
    if (authored === 1) {
      for (i = 0; i < count; i++) outTexts[i] = custom(0, i);
    } else if (options.labelPosition === 'outEnd') {
      for (k = 0; k < authored; k++) {
        if (series[k].labels !== null && series[k].labels !== undefined) {
          note(notes, 'advisory', 'series[' + k + '].dataLabels: an outEnd label on a stacked waterfall ' +
            'shows the whole bar — the per-point texts are not used');
        }
      }
    }

    for (i = 0; i < count; i++) {
      var roleAt = roles[i];
      if (roleAt === 'gap') continue;
      var lowAt = bottoms[i], highAt = tops[i];
      var sign = roleAt === 'increase' ? '+' : (roleAt === 'decrease' ? '-' : '');
      var first;
      if (lowAt === highAt) {
        var level = highAt;
        for (first = 0; first < authored; first++) if (series[first].values[i] !== null) break;
        if (level >= 0) {
          planSeries[0].values[i] = toFloat(level);
          var target = plusIndex[first];
          planSeries[target].values[i] = 0.0;
          segments.push({ category: i, source: first, lo: toFloat(level), hi: toFloat(level),
            role: roleAt, parts: [{ series: target, cell: 0.0 }],
            label: { series: target, point: i, text: null, sign: sign, format: formats[roleAt] },
            hide: [] });
        } else {
          planSeries[minusBase].values[i] = toFloat(level);
          segments.push({ category: i, source: first, lo: toFloat(level), hi: toFloat(level),
            role: roleAt, parts: [],
            label: { series: minusBase, point: i, text: formatNumber(0.0, formats[roleAt]), sign: sign,
              format: formats[roleAt] },
            hide: [] });
        }
        continue;
      }
      var extPlus = Math.max(highAt, 0.0) - Math.max(lowAt, 0.0);
      var extMinus = Math.min(highAt, 0.0) - Math.min(lowAt, 0.0);
      if (highAt > 0) planSeries[0].values[i] = toFloat(Math.max(lowAt, 0.0));
      if (lowAt < 0) planSeries[minusBase].values[i] = toFloat(Math.min(highAt, 0.0));
      var remainingMinus = extMinus, remainingPlus = extPlus;
      var cursorPlus = Math.max(lowAt, 0.0), cursorMinus = Math.min(highAt, 0.0);
      for (k = 0; k < authored; k++) {
        var cellValue = series[k].values[i];
        if (cellValue === null) continue;
        var magnitude = Math.abs(cellValue);
        var below = fits(magnitude, remainingMinus) ? magnitude : remainingMinus;
        remainingMinus = Math.max(0.0, remainingMinus - below);
        var rest = magnitude - below;
        var above = fits(rest, remainingPlus) ? rest : remainingPlus;
        remainingPlus = Math.max(0.0, remainingPlus - above);
        var parts = [];
        var loK = null, hiK = null;
        if (above > 0) {
          planSeries[plusIndex[k]].values[i] = toFloat(above);
          parts.push({ series: plusIndex[k], cell: toFloat(above) });
          loK = cursorPlus;
          hiK = cursorPlus + above;
          cursorPlus = cursorPlus + above;
        }
        if (below > 0) {
          planSeries[minusIndex[k]].values[i] = toFloat(-below);
          parts.push({ series: minusIndex[k], cell: toFloat(-below) });
          var pieceLow = cursorMinus - below, pieceHigh = cursorMinus;
          cursorMinus = cursorMinus - below;
          loK = loK === null ? pieceLow : Math.min(loK, pieceLow);
          hiK = hiK === null ? pieceHigh : Math.max(hiK, pieceHigh);
        }
        if (!parts.length) continue;
        var label, hide;
        if (parts.length === 2) {
          var endPart = roleAt !== 'decrease' ? parts[0] : parts[1];
          var other = endPart === parts[0] ? parts[1] : parts[0];
          label = { series: endPart.series, point: i, text: formatNumber(cellValue, formats[roleAt]), sign: sign,
            format: formats[roleAt] };
          hide = [[other.series, i]];
        } else {
          label = { series: parts[0].series, point: i, text: null, sign: sign, format: formats[roleAt] };
          hide = [];
        }
        segments.push({ category: i, source: k, lo: toFloat(loK), hi: toFloat(hiK), role: roleAt,
          parts: parts, label: label, hide: hide });
      }
    }

    /* The author's own texts replace the formatted step: frozen, like a zero-crossing label. */
    for (var g = 0; g < segments.length; g++) {
      var own = custom(segments[g].source, segments[g].category);
      if (own !== null) segments[g].label.text = own;
    }

    var colours = null, inked = [];
    if (authored === 1) {
      colours = roleColours(model.colors);
      inked = inkRoles(model.colors);
    }
    return {
      roles: roles, values: sums, levels: levels, tops: tops, bottoms: bottoms,
      totals: numericSort(Object.keys(totalSet).map(Number)), baseValue: toFloat(baseValue), negative: negative,
      series: planSeries, segments: segments, connectors: [], colors: colours, inkRoles: inked, formats: formats,
      texts: outTexts
    };
  }

  function fits(piece, room) { return piece <= room + 1e-9 * Math.max(1.0, Math.abs(piece), Math.abs(room)); }

  /** `role_colours`: one colour → rises and falls take it, totals the ink; two → the totals take
      the first; three → [increase, decrease, total]; a role still uncovered takes its default. */
  function roleColours(colors) {
    if (colors.length === 1 && colors[0] !== null) {
      return { increase: colors[0], decrease: colors[0], total: WATERFALL_INK };
    }
    var out = {};
    var roles = ['increase', 'decrease', 'total'];
    for (var position = 0; position < roles.length; position++) {
      var chosen = position < colors.length ? colors[position] : null;
      if (chosen === null && roles[position] === 'total' && colors.length === 2) chosen = colors[0];
      out[roles[position]] = chosen !== null ? chosen : WATERFALL_COLORS[roles[position]];
    }
    return out;
  }

  function inkRoles(colors) { return colors.length === 1 && colors[0] !== null ? ['total'] : []; }

  function planConnectors(model, plan, notes) {
    if (model.options.connectors === false) return [];
    var axisRange = model.axis;
    var roles = plan.roles, levels = plan.levels;
    var categories = model.categories;
    var out = [];
    for (var i = 0; i < roles.length - 1; i++) {
      if (roles[i] === 'gap' || roles[i + 1] === 'gap') continue;
      var level = levels[i];
      if (axisRange !== null && (level < axisRange.min || level > axisRange.max)) {
        note(notes, 'info', 'the connector after category ' + i + " ('" + categories[i] + "') at " +
          jsNumberString(level) + ' lies outside the value axis ' +
          '(' + jsNumberString(axisRange.min) + '..' + jsNumberString(axisRange.max) + ') — omitted');
        continue;
      }
      out.push({ index: i, level: level });
    }
    return out;
  }

  // ================================================================= axis and number formats

  /** [min, max, majorUnit] — `nice_range`. */
  function niceRange(low, high) {
    if (high <= low) high = low + 1.0;
    var raw = (high - low) / 4.0;
    var exponent = 0;
    while (pow10(exponent + 1) <= raw) exponent += 1;
    while (pow10(exponent) > raw) exponent -= 1;
    var magnitude = pow10(exponent);
    var step = magnitude;
    var factors = [1.0, 2.0, 2.5, 5.0, 10.0];
    for (var i = 0; i < factors.length; i++) {
      step = magnitude * factors[i];
      if (raw <= step) break;
    }
    return [toFloat(Math.floor(low / step) * step), toFloat(Math.ceil(high / step) * step), step];
  }

  function dataBounds(model) {
    var family = model.family, stacked = model.stacked;
    var series = model.series;
    var i, k;
    if (family === 'waterfall') {
      var plan = model.waterfall;
      var lows = plan.bottoms.filter(function (b) { return b !== null; });
      var highs = plan.tops.filter(function (t) { return t !== null; });
      if (!lows.length) return null;
      return [Math.min.apply(null, lows), Math.max.apply(null, highs)];
    }
    var valuesOf = family === 'scatter'
      ? function (entry) { return entry.y || []; }
      : function (entry) { return entry.values; };
    if (stacked && (family === 'column' || family === 'bar' || family === 'line' || family === 'area')) {
      var low = null, high = null;
      for (i = 0; i < model.categories.length; i++) {
        var positive = 0.0, negative = 0.0;
        var anyPositive = false, anyNegative = false;
        for (k = 0; k < series.length; k++) {
          var value = series[k].values[i];
          if (value === null) continue;
          if (value >= 0) { positive += value; anyPositive = true; } else { negative += value; anyNegative = true; }
        }
        if (stacked === 'stacked100') {
          positive = anyPositive ? 100.0 : 0.0;
          negative = anyNegative ? -100.0 : 0.0;
        }
        if (anyPositive || anyNegative) {
          low = low === null ? negative : Math.min(low, negative);
          high = high === null ? positive : Math.max(high, positive);
        }
      }
      return low === null ? null : [low, high];
    }
    var numbers = [];
    for (k = 0; k < series.length; k++) {
      var list = valuesOf(series[k]);
      for (i = 0; i < list.length; i++) if (list[i] !== null) numbers.push(list[i]);
    }
    if (!numbers.length) return null;
    return [Math.min.apply(null, numbers), Math.max.apply(null, numbers)];
  }

  function axisRange(model) {
    if (model.family === null || model.family === 'pie' || model.family === 'doughnut') return null;
    var valueAxis = model.options.valueAxis;
    var bounds = dataBounds(model);
    var low = bounds === null ? 0.0 : Math.min(0.0, bounds[0]);
    var high = bounds === null ? 1.0 : Math.max(0.0, bounds[1]);
    var givenMin = valueAxis.min, givenMax = valueAxis.max, givenStep = valueAxis.majorUnit;
    var lowIn = givenMin !== null ? givenMin : low;
    var highIn = givenMax !== null ? givenMax : high;
    var lo, hi, step;
    if (givenStep !== null) {
      step = givenStep;
      lo = givenMin !== null ? givenMin : toFloat(Math.floor(lowIn / step) * step);
      hi = givenMax !== null ? givenMax : toFloat(Math.ceil(highIn / step) * step);
    } else {
      var nice = niceRange(lowIn, highIn);
      step = nice[2];
      lo = givenMin !== null ? givenMin : nice[0];
      hi = givenMax !== null ? givenMax : nice[1];
    }
    if (hi <= lo) hi = toFloat(lo + step);
    return { min: toFloat(lo), max: toFloat(hi), majorUnit: toFloat(step) };
  }

  function axisTicks(range) {
    if (range === null) return [];
    var ticks = [];
    var step = range.majorUnit;
    for (var k = 0; k < 200; k++) {
      var value = range.min + k * step;
      if (value > range.max + step * 1e-6) break;
      ticks.push(roundTo(value, 10));
    }
    return ticks;
  }

  function generalNumber(value) {
    var rounded = jsRound(value * 1e6) / 1e6;
    return jsNumberString(rounded === 0 ? 0.0 : rounded);
  }

  function splitSections(fmt) {
    var sections = [], current = '', quoted = false, index = 0;
    while (index < fmt.length) {
      var character = fmt.charAt(index);
      if (character === '\\' && !quoted && index + 1 < fmt.length) {
        current += fmt.slice(index, index + 2);
        index += 2;
        continue;
      }
      if (character === '"') quoted = !quoted;
      if (character === ';' && !quoted) {
        sections.push(current);
        current = '';
      } else {
        current += character;
      }
      index += 1;
    }
    sections.push(current);
    return sections;
  }

  function masked(section) {
    var out = '', index = 0, quoted = false;
    while (index < section.length) {
      var character = section.charAt(index);
      if (quoted) {
        out += '\0';
        if (character === '"') quoted = false;
        index += 1;
        continue;
      }
      if (character === '"') {
        quoted = true;
        out += '\0';
        index += 1;
        continue;
      }
      if ('\\_*'.indexOf(character) >= 0 && index + 1 < section.length) {
        out += '\0\0';
        index += 2;
        continue;
      }
      if (character === '[') {
        var close = section.indexOf(']', index);
        if (close > index) {
          out += repeat('\0', close - index + 1);
          index = close + 1;
          continue;
        }
      }
      out += character;
      index += 1;
    }
    return out;
  }

  function literal(part) {
    var out = '', index = 0, quoted = false;
    while (index < part.length) {
      var character = part.charAt(index);
      if (quoted) {
        if (character === '"') quoted = false;
        else out += character;
        index += 1;
        continue;
      }
      if (character === '"') {
        quoted = true;
        index += 1;
        continue;
      }
      if (character === '\\' && index + 1 < part.length) {
        out += part.charAt(index + 1);
        index += 2;
        continue;
      }
      if (character === '_' && index + 1 < part.length) {
        out += ' ';
        index += 2;
        continue;
      }
      if (character === '*' && index + 1 < part.length) {
        index += 2;
        continue;
      }
      if (character === '[') {
        var close = part.indexOf(']', index);
        if (close > index) {
          index = close + 1;
          continue;
        }
      }
      out += character;
      index += 1;
    }
    return out;
  }

  function increment(digits) {
    var chars = digits.split('');
    var index = chars.length - 1;
    while (index >= 0) {
      if (chars[index] === '9') {
        chars[index] = '0';
        index -= 1;
      } else {
        chars[index] = String(parseInt(chars[index], 10) + 1);
        return chars.join('');
      }
    }
    return '1' + chars.join('');
  }

  /** [integer digits, fraction digits] rounded half-up on the shortest decimal form (`_fixed`). */
  function fixed(value, places) {
    if (value === 0) return ['0', repeat('0', places)];
    var parsed = shortest(value), digits = parsed[0], n = parsed[1];
    var keep = n + places;
    var scaled;
    if (keep < 0) {
      scaled = '0';
    } else if (keep >= digits.length) {
      scaled = digits + repeat('0', keep - digits.length);
    } else {
      scaled = digits.slice(0, keep) || '0';
      if (digits.charAt(keep) >= '5') scaled = increment(scaled);
    }
    scaled = stripLeft(scaled, '0') || '0';
    scaled = padLeft(scaled, places + 1, '0');
    if (places) return [scaled.slice(0, scaled.length - places), scaled.slice(scaled.length - places)];
    return [scaled, ''];
  }

  var BODY = /[#0][#0,]*(\.[#0]+)?/;

  /** An Excel number format applied the way a PowerPoint label shows it (`format_number`). */
  function formatNumber(value, fmt) {
    if (value === null || value === undefined || typeof value !== 'number' || !isFinite(value)) return '';
    fmt = isStr(fmt) && fmt ? fmt : 'General';
    var sections = splitSections(fmt);
    var minus = false;
    var section, magnitude;
    if (value < 0 && sections.length >= 2) {
      section = sections[1];
      magnitude = -value;
    } else if (value === 0 && sections.length >= 3) {
      section = sections[2];
      magnitude = 0.0;
    } else {
      section = sections[0];
      magnitude = Math.abs(value);
      minus = value < 0;
    }
    var maskedSection = masked(section);
    var found = BODY.exec(maskedSection);
    var text;
    if (found === null) {
      var bare = trim(maskedSection.split('\0').join(''));
      if (bare === '' && section) return (minus ? '-' : '') + literal(section);
      text = generalNumber(magnitude);
      return (minus && text !== '0' ? '-' : '') + text;
    }
    var body = found[0];
    var start = found.index, end = found.index + body.length;
    var prefix = section.slice(0, start), suffix = section.slice(end);
    var suffixMasked = maskedSection.slice(end);
    var dot = body.indexOf('.');
    var whole = dot >= 0 ? body.slice(0, dot) : body;
    var decimals = dot >= 0 ? body.slice(dot + 1) : '';
    var scale = 0;
    if (!decimals) {
      scale += whole.length - stripRight(whole, ',').length;
      whole = stripRight(whole, ',');
    }
    var lead = suffixMasked.length - stripLeft(suffixMasked, ',').length;
    scale += lead;
    suffix = suffix.slice(lead);
    if (maskedSection.split('\0').join('').indexOf('%') >= 0) magnitude = magnitude * 100.0;
    for (var s = 0; s < scale; s++) magnitude = magnitude / 1000.0;
    var number;
    if (magnitude >= 1e21) {
      number = generalNumber(magnitude);
    } else {
      var mandatory = countOf(decimals, '0');
      var parts = fixed(magnitude, decimals.length), integer = parts[0], fraction = parts[1];
      while (fraction.length > mandatory && fraction.charAt(fraction.length - 1) === '0') fraction = fraction.slice(0, -1);
      var minimum = countOf(whole, '0');
      if (integer.length < minimum) integer = padLeft(integer, minimum, '0');
      if (minimum === 0 && integer === '0') integer = '';
      if (whole.indexOf(',') >= 0 && integer) {
        var groups = [];
        while (integer.length > 3) {
          groups.unshift(integer.slice(-3));
          integer = integer.slice(0, -3);
        }
        groups.unshift(integer);
        integer = groups.join(',');
      }
      number = integer + (fraction ? '.' + fraction : '');
    }
    var nonzero = /[1-9]/.test(number);
    return (minus && nonzero ? '-' : '') + literal(prefix) + number + literal(suffix);
  }

  /** `format_unit`: what one unit of the last digit a format shows is worth (`0.0` → 0.1), or null. */
  function formatUnit(fmt) {
    if (!isStr(fmt) || !fmt) return null;
    var section = splitSections(fmt)[0];
    var maskedSection = masked(section);
    var found = BODY.exec(maskedSection);
    if (found === null) return null;
    var body = found[0];
    var dot = body.indexOf('.');
    var whole = dot >= 0 ? body.slice(0, dot) : body;
    var decimals = dot >= 0 ? body.slice(dot + 1) : '';
    var scale = 0;
    if (!decimals) scale += whole.length - stripRight(whole, ',').length;
    var suffixMasked = maskedSection.slice(found.index + body.length);
    scale += suffixMasked.length - stripLeft(suffixMasked, ',').length;
    var unit = pow10(-decimals.length);
    for (var s = 0; s < scale; s++) unit = unit * 1000.0;
    if (maskedSection.split('\0').join('').indexOf('%') >= 0) unit = unit / 100.0;
    return unit;
  }

  /** `value_axis_format`: the tick format the file carries — `0%` on a percent axis by default. */
  function valueAxisFormat(model) {
    var given = model.options.valueAxis.format;
    if (isPercentAxis(model.family, model.stacked)) return given || PERCENT_TICK_FORMAT;
    return given || model.options.numberFormat;
  }

  /** `tick_text`: a percent axis's ticks run 0–100 in the model and 0–1 in the file. */
  function tickText(model, value) {
    var fmt = valueAxisFormat(model);
    if (isPercentAxis(model.family, model.stacked)) return formatNumber(toFloat(value / 100.0), fmt);
    return formatNumber(value, fmt);
  }

  function generalFormatFor(series) {
    for (var k = 0; k < series.length; k++) {
      var values = series[k].values;
      for (var i = 0; i < values.length; i++) {
        if (values[i] !== null && values[i] !== Math.floor(values[i])) return '0.0#';
      }
    }
    return '0';
  }

  function literalPlus(section) {
    var index = 0, quoted = false;
    while (index < section.length) {
      var character = section.charAt(index);
      if (quoted) {
        if (character === '"') quoted = false;
        else if (character === '+') return [index, index + 1];
        index += 1;
        continue;
      }
      if (character === '"') {
        quoted = true;
        index += 1;
        continue;
      }
      if (character === '\\' && index + 1 < section.length) {
        if (section.charAt(index + 1) === '+') return [index, index + 2];
        index += 2;
        continue;
      }
      if (character === '+' && !(index > 0 && 'Ee'.indexOf(section.charAt(index - 1)) >= 0)) return [index, index + 1];
      index += 1;
    }
    return null;
  }

  function signedFormat(fmt, sign) {
    if (!sign) return fmt;
    var sections = splitSections(fmt);
    if (sections.length >= 2) {
      var chosen = sign === '+' ? sections[0] : sections[1];
      var tail = sections.length >= 3 ? ';' + sections[2] : '';
      return chosen + ';' + chosen + tail;
    }
    var section = sections[0];
    var plus = literalPlus(section);
    if (plus !== null) {
      if (sign === '+') return section + ';' + section;
      var body = section.slice(0, plus[0]) + section.slice(plus[1]);
      return '"-"' + body + ';"-"' + body;
    }
    return '"' + sign + '"' + section + ';"' + sign + '"' + section;
  }

  // ================================================================================ normalise

  function blankOptions() {
    var out = {};
    for (var i = 0; i < OPTION_ORDER.length; i++) out[OPTION_ORDER[i]] = null;
    return out;
  }

  function blankModel(reason, notes) {
    return {
      type: null, requested: null, reason: reason, family: null, base: null, stacked: '',
      categories: [], series: [], colors: [], options: blankOptions(),
      notes: notes, axis: null, waterfall: null, overlay: false
    };
  }

  /** The one reading of a `data-chart` spec (`chart_model.normalise`). Never throws. */
  function normalise(spec) {
    var notes = [];
    if (!isDict(spec)) return blankModel('chart spec must be an object', notes);

    // 1 -- the type
    if (get(spec, 'type') === null) note(notes, 'warn', 'the chart has no type — drawn as a column chart');
    var resolved = resolveTypeName(get(spec, 'type'));
    var kind = familyOf(resolved.name);
    kind.name = resolved.name;
    if (resolved.name !== null && resolved.requested !== resolved.name && get(spec, 'type') !== null) {
      note(notes, 'advisory', "chart type '" + resolved.requested + "' read as '" + resolved.name + "' — " +
        'write the type name as listed');
    }

    // 3 -- series and categories (2, numbers, inside)
    var categories = kind.family !== 'scatter' ? readCategories(spec, notes) : [];
    var series = readSeries(spec, kind.family, categories.length, notes);
    if ((kind.family === 'pie' || kind.family === 'doughnut') && series.length > 1) {
      note(notes, 'warn', 'a ' + resolved.name + ' chart takes a single series — only series[0] is drawn');
      series = series.slice(0, 1);
    }

    // 4 -- colours, keeping positions
    var colors = [];
    var rawColors = get(spec, 'colors');
    if (rawColors !== null) {
      if (isList(rawColors)) {
        for (var index = 0; index < rawColors.length; index++) {
          colors.push(colour(rawColors[index], 'colors[' + index + ']', notes));
        }
      } else {
        note(notes, 'rejected', 'colors must be a list — ignored');
      }
    }

    var model = {
      type: resolved.name, requested: resolved.requested, reason: resolved.reason,
      family: kind.family, base: kind.base, stacked: kind.stacked,
      categories: categories, series: series, colors: colors,
      options: blankOptions(), notes: notes,
      axis: null, waterfall: null, overlay: false
    };
    if (resolved.name === null) return model;

    // 5, 6 -- options, pointColors, defaults
    var options = normaliseOptions(spec, kind, categories, series, notes);
    model.options = options;

    if (!series.length) {
      model.type = null;
      model.reason = 'chart spec needs a non-empty series list';
      return model;
    }
    if (kind.family !== 'scatter' && !categories.length) {
      model.type = null;
      model.reason = 'chart spec needs a non-empty categories list';
      return model;
    }

    // 7 -- the waterfall plan, the axis, the overlay
    if (kind.family === 'waterfall') {
      var found = totalsOf(spec, series, categories.length, options.baseValue, notes, categories,
        options.valueAxis, options.labelFormat || options.numberFormat);
      options.totals = found.totals;
      model.waterfall = waterfallPlan(model, found.totals, notes);
    }
    var range = axisRange(model);
    if (kind.family === 'waterfall') {
      model.axis = range;
      model.waterfall.connectors = planConnectors(model, model.waterfall, notes);
      tinySteps(model, notes);
    } else if (range !== null && options.valueAxis.min !== null && options.valueAxis.max !== null) {
      model.axis = range;
    }
    model.overlay = overlay(model, notes);
    return model;
  }

  function tinySteps(model, notes) {
    var plan = model.waterfall, range = model.axis;
    if (range === null) return;
    var span = range.max - range.min;
    var steps = [];
    for (var i = 0; i < plan.roles.length; i++) {
      if (plan.roles[i] === 'increase' || plan.roles[i] === 'decrease') {
        steps.push(Math.abs((plan.tops[i] || 0.0) - (plan.bottoms[i] || 0.0)));
      }
    }
    if (steps.length && span > 0 && steps.every(function (step) { return step < 0.02 * span; })) {
      note(notes, 'advisory', 'every step is below 2 % of the value axis — set valueAxis.min to zoom the bridge');
    }
  }

  function overlay(model, notes) {
    var options = model.options;
    var family = model.family;
    var drawn = false;
    if (family === 'waterfall') {
      if (options.labelPosition === 'outEnd' && options.dataLabels.some(Boolean)) drawn = true;
    }
    var lines = options.referenceLines;
    if (lines.length) {
      if (family === 'pie' || family === 'doughnut') {
        note(notes, 'rejected', 'options.referenceLines need a value axis — a pie or doughnut chart has none');
      } else if (model.axis === null) {
        note(notes, 'advisory', 'options.referenceLines need options.valueAxis.min and max to be placed — ' +
          'the lines are left out');
      } else {
        drawn = true;
      }
    }
    return drawn;
  }

  // =================================================================================== layout

  /** Deterministic text width in px — `0.55 em` per character, in place of canvas measureText; a
      text broken by `\n` is as wide as its widest line. */
  function estimate(text, size) {
    var lines = textLines(text), widest = 0.0;
    for (var i = 0; i < lines.length; i++) widest = Math.max(widest, 0.55 * size * lines[i].length);
    return widest;
  }

  /* What PowerPoint needs around a pinned inner plot rect before it moves or shrinks it — measured
     with PowerPoint COM in WP-C C3 (see chart_model.py for the numbers). */
  var AXIS_LINE_EM = 2.05;
  var AXIS_LINE_PX = 1.0;
  var HALF_LABEL_EM = 0.7;
  var TICK_GAP_EM = 1.0;
  var TICK_GAP_PX = 2.0;
  var LEGEND_ROW_EM = 1.9;
  var LEGEND_PITCH_EM = 1.6;
  var LEGEND_GAP_EM = 0.3;
  var LINE_PITCH_EM = 1.2;

  function labelsOutside(model) {
    var options = model.options;
    if (!options.dataLabels.some(Boolean)) return false;
    return options.labelPosition === 'outEnd' || options.labelPosition === 'above';
  }

  /** `_label_lines`: the most lines any drawn label text runs to. */
  function labelLines(model) {
    var labelled = model.options.dataLabels;
    var texts = [], k;
    if (model.family === 'waterfall' && model.waterfall !== null) {
      if (labelled.some(Boolean)) texts = model.waterfall.texts.slice();
    } else {
      for (k = 0; k < model.series.length; k++) {
        if (k < labelled.length && labelled[k] && model.series[k].labels !== null) {
          texts = texts.concat(model.series[k].labels);
        }
      }
    }
    var lines = 1;
    for (k = 0; k < texts.length; k++) {
      if (texts[k] !== null) lines = Math.max(lines, textLines(texts[k]).length);
    }
    return lines;
  }

  /** `_category_lines`: the most lines a category name runs to. */
  function categoryLines(model) {
    var lines = 1;
    for (var c = 0; c < model.categories.length; c++) lines = Math.max(lines, textLines(model.categories[c]).length);
    return lines;
  }

  function legendNames(model) {
    if (model.family === 'pie' || model.family === 'doughnut') return model.categories.slice();
    return model.series.map(function (entry) { return entry.name; });
  }

  /** [width, height, rows] of the legend box, in px (`_legend_size`). */
  function legendSize(model, w, h, size) {
    var names = legendNames(model);
    var widest = 0.0;
    for (var i = 0; i < names.length; i++) widest = Math.max(widest, estimate(names[i], size));
    var entry = size * 1.3 + widest + 6.0;
    var count = Math.max(1, names.length);
    var position = model.options.legend;
    if (position === 'left' || position === 'right' || position === 'topRight') {
      return [entry, size * (LEGEND_PITCH_EM * count + LEGEND_GAP_EM), count];
    }
    var room = Math.max(entry, w - 8.0);
    var perRow = Math.max(1, Math.floor(room / entry));
    var rows = Math.floor((count + perRow - 1) / perRow);
    return [Math.min(room, entry * Math.min(count, perRow)), size * (LEGEND_ROW_EM + (rows - 1) * LEGEND_PITCH_EM), rows];
  }

  function endOverflow(label, slot, size) { return Math.max(0.0, (estimate(label, size) - slot) / 2.0); }

  function defaultPlotArea(model, w, h, size, ticks) {
    var options = model.options;
    var family = model.family;
    var pad = 4.0;
    var legendAt = options.legend;
    var gap = size * LEGEND_GAP_EM;
    var box = legendAt ? legendSize(model, w, h, size) : [0.0, 0.0, 0];
    var legendW = box[0], legendH = box[1];
    var top = pad + (options.title ? size * 1.15 * 1.6 : 0.0) + (legendAt === 'top' ? legendH + gap : 0.0);
    var bottom = pad + (legendAt === 'bottom' ? legendH + gap : 0.0);
    var left = pad + (legendAt === 'left' ? legendW + gap : 0.0);
    var right = pad + (legendAt === 'right' || legendAt === 'topRight' ? legendW + gap : 0.0);
    var valueVisible = options.valueAxis.visible;
    var categoryVisible = options.categoryAxis.visible;
    var outside = labelsOutside(model);
    var line = size * AXIS_LINE_EM + AXIS_LINE_PX;
    var tickGap = size * TICK_GAP_EM + TICK_GAP_PX;
    var tickTexts = ticks.map(function (tick) { return tickText(model, tick); });
    var widestTick = 0.0, i;
    for (i = 0; i < tickTexts.length; i++) widestTick = Math.max(widestTick, estimate(tickTexts[i], size));
    var categories = model.categories;
    var labelsPx = labelSize(model, size);

    if (family === 'column' || family === 'waterfall' || family === 'line' || family === 'area' || family === 'scatter') {
      if (valueVisible) left += widestTick + tickGap;
      if (categoryVisible && (family === 'scatter' || categories.length)) {
        bottom += line + (categoryLines(model) - 1) * size * LINE_PITCH_EM;
        if (family === 'scatter') {
          var last = tickTexts.length ? tickTexts[tickTexts.length - 1] : '';
          right = Math.max(right, pad + estimate(last, size) / 2.0);
        } else {
          var slot = Math.max(1.0, w - left - right) / Math.max(1, categories.length);
          left = Math.max(left, pad + endOverflow(categories[0], slot, size));
          right = Math.max(right, pad + endOverflow(categories[categories.length - 1], slot, size));
        }
      }
      if (outside) top += labelsPx * 1.25 + (labelLines(model) - 1) * labelsPx * LINE_PITCH_EM;
      if (valueVisible) top = Math.max(top, pad + size * HALF_LABEL_EM);
    } else if (family === 'bar') {
      if (outside) right += labelsPx * 2.2;
      if (valueVisible) {
        bottom += line;
        right = Math.max(right, pad + (tickTexts.length ? estimate(tickTexts[tickTexts.length - 1], size) / 2.0 : 0.0));
      }
      if (categoryVisible) {
        var widest = 0.0;
        for (i = 0; i < categories.length; i++) widest = Math.max(widest, estimate(categories[i], size));
        left += widest + tickGap;
      }
    }
    left = Math.min(left, 0.6 * w);
    bottom = Math.min(bottom, 0.6 * h);
    var area = {
      x: roundTo(left / w, 4), y: roundTo(top / h, 4),
      w: roundTo(Math.max(1.0, w - left - right) / w, 4), h: roundTo(Math.max(1.0, h - top - bottom) / h, 4)
    };
    return { plotArea: area, gutters: { left: left, top: top, right: right, bottom: bottom } };
  }

  /** Where the legend of a chart laid out at `area` goes, fractions of the frame (`legend_box`). */
  function legendBox(model, w, h, size, area) {
    var position = model.options.legend;
    if (!position || model.type === null || w <= 0 || h <= 0) return null;
    var pad = 4.0;
    var box = legendSize(model, w, h, size), legendW = box[0], legendH = box[1];
    var title = model.options.title ? size * 1.15 * 1.6 : 0.0;
    var x, y;
    if (position === 'bottom') {
      x = (w - legendW) / 2.0;
      y = h - pad - legendH;
    } else if (position === 'top') {
      x = (w - legendW) / 2.0;
      y = pad + title;
    } else {
      x = position === 'left' ? pad : w - pad - legendW;
      if (position === 'topRight') y = pad + title;
      else y = (area.y + area.h / 2.0) * h - legendH / 2.0;
    }
    x = Math.max(0.0, x);
    y = Math.max(0.0, y);
    return { x: roundTo(x / w, 4), y: roundTo(y / h, 4), w: roundTo(Math.min(legendW, w - x) / w, 4),
      h: roundTo(Math.min(legendH, h - y) / h, 4) };
  }

  /** The geometry of a model in a frame of w × h px at text size sizePx (`chart_model.layout`). */
  function layout(model, wPx, hPx, sizePx) {
    var options = model.options;
    var size = get(options, 'fontSize') !== null ? options.fontSize : sizePx;
    size = size === null || size === undefined ? DEFAULT_SIZE_PX : Number(size);
    size = Math.min(48.0, Math.max(5.0, size));
    var w = Number(wPx), h = Number(hPx);
    var ticks = model.type !== null ? axisTicks(axisRange(model)) : [];
    var pinned = model.type !== null && (get(options, 'plotArea') !== null || Boolean(model.overlay));
    if (model.type === null || w <= 0 || h <= 0) {
      return { sizePx: size, ticks: ticks, plotArea: { x: 0.0, y: 0.0, w: 1.0, h: 1.0 },
        pinned: false, gutters: { left: 0.0, top: 0.0, right: 0.0, bottom: 0.0 }, legend: null };
    }
    var area, gutters;
    if (get(options, 'plotArea') !== null) {
      area = copyObject(options.plotArea);
      gutters = { left: area.x * w, top: area.y * h,
        right: w - (area.x + area.w) * w, bottom: h - (area.y + area.h) * h };
    } else {
      var fallback = defaultPlotArea(model, w, h, size, ticks);
      area = fallback.plotArea;
      gutters = fallback.gutters;
    }
    return { sizePx: size, ticks: ticks, plotArea: area, pinned: pinned, gutters: gutters,
      legend: legendBox(model, w, h, size, area) };
  }

  /** The tables above, for the parity test's "one source" check. */
  var TABLES = {
    STAGEFLOW_NAMES: keysOf(STAGEFLOW_NAMES), DERIVED_BASES: DERIVED_BASES, ENGINE_BASES: ENGINE_BASES,
    ALIASES: ALIASES, PATH_A_UNSUPPORTED_PREFIXES: PATH_A_UNSUPPORTED_PREFIXES, ROUND_2: keysOf(ROUND_2),
    OPTION_KEYS: keysOf(OPTION_KEYS), SERIES_KEYS: keysOf(SERIES_KEYS), AXIS_KEYS: keysOf(AXIS_KEYS),
    LABEL_POSITIONS: keysOf(LABEL_POSITIONS), LABEL_POSITION_ALIASES: LABEL_POSITION_ALIASES,
    LEGEND_POSITIONS: keysOf(LEGEND_POSITIONS), MARKER_STYLES: keysOf(MARKER_STYLES), DASH_NAMES: keysOf(DASH_NAMES),
    TYPE_WORDS: TYPE_WORDS, WATERFALL_COLORS: WATERFALL_COLORS, GRIDLINES_DEFAULT: GRIDLINES_DEFAULT,
    CONNECTORS_DEFAULT: CONNECTORS_DEFAULT, REFERENCE_LINE_DEFAULT: REFERENCE_LINE_DEFAULT,
    DEFAULT_GAP_WIDTH: DEFAULT_GAP_WIDTH, DEFAULT_SIZE_PX: DEFAULT_SIZE_PX, BASE_SERIES: BASE_SERIES, MINUS: MINUS,
    OPTION_ORDER: OPTION_ORDER, NUMBER_RANGES: NUMBER_RANGES, DASH_MAX_PX: DASH_MAX_PX,
    WATERFALL_INK: WATERFALL_INK, PERCENT_TICK_FORMAT: PERCENT_TICK_FORMAT, TOTAL_CATEGORY: TOTAL_CATEGORY.source,
    SUBTOTAL_CATEGORY: SUBTOTAL_CATEGORY.source, SUBTOTAL_TOLERANCE: SUBTOTAL_TOLERANCE,
    LABEL_STYLE_KEYS: keysOf(LABEL_STYLE_KEYS), LINE_PITCH_EM: LINE_PITCH_EM
  };

  // =================================================================================================
  // DRAWING — from the model and the layout only.
  // =================================================================================================

  /* A spec without `colors` is emitted with the deck's theme accents, which the browser cannot know;
     Office's default accent ramp is the closest fixed stand-in. */
  var DEFAULT_COLORS = ['#4472C4', '#ED7D31', '#A5A5A5', '#FFC000', '#5B9BD5', '#70AD47'];
  var AXIS_COLOR = '#C9D3E0';      // engine/emit/charts.py: _apply_axis
  var DARK_INK = '#12233B';        // idem (_contrast)
  /* DrawingML preset dashes as SVG dash patterns, in multiples of the line width. */
  var PRESET_DASH = {
    solid: null, dot: [1, 1], dash: [4, 3], lgDash: [8, 3], dashDot: [4, 3, 1, 3], lgDashDot: [8, 3, 1, 3],
    lgDashDotDot: [8, 3, 1, 3, 1, 3], sysDash: [3, 1], sysDot: [1, 1], sysDashDot: [3, 1, 1, 1],
    sysDashDotDot: [3, 1, 1, 1, 1, 1]
  };

  function r3(v) { return Math.round(v * 1000) / 1000; }
  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }
  function hash(hex) { return hex ? '#' + hex : null; }

  function node(name, attrs) {
    var element = document.createElementNS(NS, name);
    if (attrs) {
      for (var key in attrs) {
        if (!Object.prototype.hasOwnProperty.call(attrs, key)) continue;
        var value = attrs[key];
        if (value === null || value === undefined) continue;
        element.setAttribute(key, typeof value === 'number' ? String(r3(value)) : String(value));
      }
    }
    return element;
  }

  /** An SVG text. A `\n` in `content` breaks it into one `tspan` per line, `LINE_PITCH_EM` apart, as
      the file writes one paragraph per line; `align` says which line sits on `attrs.y` — 'top' (the
      first; the rest below), 'bottom' (the last; the rest above) or 'middle' (the block centred on
      it). A broken text carries its whole content as `data-cp-text`. */
  function label(content, attrs, align) {
    var text = String(content);
    var lines = textLines(text);
    var element = node('text', attrs);
    if (lines.length === 1) {
      element.textContent = text;
      return element;
    }
    var pitch = (Number(attrs['font-size']) || DEFAULT_SIZE_PX) * LINE_PITCH_EM;
    var shift = align === 'bottom' ? -(lines.length - 1) * pitch : (align === 'middle' ? -(lines.length - 1) * pitch / 2 : 0);
    element.setAttribute('y', String(r3(Number(attrs.y) + shift)));
    element.setAttribute('data-cp-text', text);
    for (var i = 0; i < lines.length; i++) {
      var span = node('tspan', { x: attrs.x, dy: i === 0 ? 0 : pitch });
      span.textContent = lines[i];
      element.appendChild(span);
    }
    return element;
  }

  function rgbOf(color) {
    var found = /^#?([0-9a-f]{6})$/i.exec(String(color || ''));
    if (found) {
      var h = found[1];
      return [parseInt(h.substr(0, 2), 16), parseInt(h.substr(2, 2), 16), parseInt(h.substr(4, 2), 16)];
    }
    var match = /rgba?\(([^)]+)\)/.exec(String(color));
    if (!match) return null;
    var parts = match[1].split(',');
    return [parseFloat(parts[0]), parseFloat(parts[1]), parseFloat(parts[2])];
  }

  function luminance(color) {
    var rgb = rgbOf(color);
    if (!rgb) return 1;
    function channel(value) {
      var c = value / 255;
      return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    }
    return 0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2]);
  }

  /** Readable ink for text sitting ON this fill — the same WCAG pick the emitter makes. */
  function contrastInk(background, dark) {
    var ink = dark || DARK_INK;
    var bg = luminance(background);
    function ratio(a, b) { return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05); }
    return ratio(bg, luminance(ink)) >= ratio(bg, luminance('#FFFFFF')) ? ink : '#FFFFFF';
  }

  var measureCanvas = null;

  /** Text width in px — for a reference line's label only (it has to stop short of the text). */
  function textWidth(text, sizePx, family) {
    var value = String(text === null || text === undefined ? '' : text);
    try {
      if (!measureCanvas) measureCanvas = document.createElement('canvas');
      var context = measureCanvas.getContext('2d');
      context.font = '400 ' + sizePx + 'px ' + family;
      return context.measureText(value).width;
    } catch (error) {
      return estimate(value, sizePx);
    }
  }

  /** An SVG dash array (px) for a model dash: px on/off lengths, or a preset name × the line width. */
  function dashArray(value, widthPx) {
    if (value === null || value === undefined) return null;
    if (isList(value)) return value.join(' ');
    var preset = PRESET_DASH[value];
    if (!preset) return null;
    return preset.map(function (v) { return r3(v * Math.max(widthPx, 0.75)); }).join(' ');
  }

  /** `resolveType` keeps the shape the preview has always exported, on top of the model's resolver. */
  function resolveType(raw) {
    var resolved = resolveTypeName(raw === undefined ? null : raw);
    var kind = familyOf(resolved.name);
    var base = kind.base || '';
    var out = {
      name: resolved.name, family: kind.family, stacked: kind.stacked, supported: resolved.name !== null,
      reason: resolved.reason || '', markers: false, lines: true, smooth: false, exploded: false
    };
    if (resolved.name === null) {
      out.family = null;
      out.stacked = '';
      return out;
    }
    if (kind.family === 'line') out.markers = base.indexOf('line_markers') === 0;
    if (kind.family === 'scatter') {
      out.markers = !/_no_markers/.test(base);
      out.lines = /_lines|_smooth/.test(base);
      out.smooth = /_smooth/.test(base);
    }
    if (kind.family === 'pie' || kind.family === 'doughnut') out.exploded = /_exploded/.test(base);
    return out;
  }

  // ------------------------------------------------------------------------------ frame + style

  /** The element's content box in CSS px, and where inside the element it starts. */
  function frameOf(element) {
    var style = global.getComputedStyle ? getComputedStyle(element) : null;
    function side(name) { return style ? (parseFloat(style.getPropertyValue(name)) || 0) : 0; }
    var padLeft = side('padding-left'), padTop = side('padding-top');
    var width = element.clientWidth - padLeft - side('padding-right');
    var height = element.clientHeight - padTop - side('padding-bottom');
    return { x: padLeft, y: padTop, w: width, h: height, position: style ? style.position : 'static',
      sizePx: style ? parseFloat(style.fontSize) : null,
      fontFamily: style ? style.fontFamily : null, color: style ? style.color : null };
  }

  function seriesColour(model, index) {
    var colors = model.colors;
    var chosen = colors.length ? colors[index % colors.length] : null;
    return chosen ? '#' + chosen : DEFAULT_COLORS[index % DEFAULT_COLORS.length];
  }

  function pointColour(model, seriesIndex, pointIndex) {
    var row = get(model.options.pointColors, String(seriesIndex));
    var chosen = row ? get(row, String(pointIndex)) : null;
    return chosen ? '#' + chosen : null;
  }

  function labelsOn(model, seriesIndex) {
    var flags = model.options.dataLabels;
    return seriesIndex < flags.length ? flags[seriesIndex] : false;
  }

  /** A point's label text: the author's own (`series[k].dataLabels`), else its formatted value.
      `''` means no label on that point. */
  function pointText(model, seriesIndex, pointIndex, value, fmt) {
    var entry = model.series[seriesIndex];
    var texts = entry ? entry.labels : null;
    if (texts && pointIndex < texts.length && texts[pointIndex] !== null) return texts[pointIndex];
    return formatNumber(value, fmt);
  }

  // ---------------------------------------------------------------------------------- axes

  function valueScale(context) {
    var model = context.model;
    var range = model.axis || axisRange(model) || { min: 0, max: 1, majorUnit: 0.25 };
    return { min: range.min, max: range.max, ticks: context.geometry.ticks,
      reverse: model.options.valueAxis.reverse === true };
  }

  function drawAxes(root, context) {
    var plot = context.plot, style = context.style, model = context.model;
    var horizontal = model.family === 'bar';
    var gridlines = model.options.gridlines;
    var ticks = context.scale.ticks;
    for (var i = 0; i < ticks.length; i++) {
      var position = context.valuePos(ticks[i]);
      if (gridlines) {
        var line = node('line', horizontal
          ? { x1: position, y1: plot.y, x2: position, y2: plot.y + plot.h }
          : { x1: plot.x, y1: position, x2: plot.x + plot.w, y2: position });
        line.setAttribute('class', 'cp-gridline');
        line.setAttribute('stroke', '#' + gridlines.color);
        line.setAttribute('stroke-width', String(r3(gridlines.width / 0.75)));
        var gridDash = dashArray(gridlines.dash, gridlines.width / 0.75);
        if (gridDash) line.setAttribute('stroke-dasharray', gridDash);
        root.appendChild(line);
      }
      if (model.options.valueAxis.visible) {
        var text = tickText(model, ticks[i]);
        var tickWeight = model.options.valueAxis.bold ? 700 : null;
        root.appendChild(label(text, horizontal
          ? { x: position, y: plot.y + plot.h + style.size * 1.15, 'text-anchor': 'middle', class: 'cp-tick',
              fill: style.color, 'font-family': style.family, 'font-size': style.size, 'font-weight': tickWeight,
              'data-cp-value': ticks[i] }
          : { x: plot.x - 6, y: position + style.size * 0.35, 'text-anchor': 'end', class: 'cp-tick',
              fill: style.color, 'font-family': style.family, 'font-size': style.size, 'font-weight': tickWeight,
              'data-cp-value': ticks[i] }));
      }
    }

    /* The axis line sits at the category axis, i.e. at value 0 when 0 is in range. */
    var baseline = context.valuePos(clamp(0, context.scale.min, context.scale.max));
    var axisLine = node('line', horizontal
      ? { x1: baseline, y1: plot.y, x2: baseline, y2: plot.y + plot.h }
      : { x1: plot.x, y1: baseline, x2: plot.x + plot.w, y2: baseline });
    axisLine.setAttribute('class', 'cp-axis');
    axisLine.setAttribute('stroke', AXIS_COLOR);
    axisLine.setAttribute('stroke-width', '0.75');
    root.appendChild(axisLine);

    /* Category labels at the plot's edge — `tickLblPos low` — never on a zero line inside it. */
    if (model.options.categoryAxis.visible) {
      var categoryWeight = model.options.categoryAxis.bold ? 700 : null;
      for (var c = 0; c < model.categories.length; c++) {
        var name = model.categories[c];
        if (!name) continue;                       // a gap slot has no label, but keeps its slot
        var centre = context.slotCentre(c);
        /* A name broken by `\n` is centred on its slot beside a bar, and hangs from the axis below a column. */
        root.appendChild(label(name, horizontal
          ? { x: plot.x - 6, y: centre + style.size * 0.35, 'text-anchor': 'end', class: 'cp-category',
              fill: style.color, 'font-family': style.family, 'font-size': style.size, 'font-weight': categoryWeight,
              'data-cp-point': c }
          : { x: centre, y: plot.y + plot.h + style.size * 1.15, 'text-anchor': 'middle', class: 'cp-category',
              fill: style.color, 'font-family': style.family, 'font-size': style.size, 'font-weight': categoryWeight,
              'data-cp-point': c }, horizontal ? 'middle' : 'top'));
      }
    }
  }

  /** `options.referenceLines`, placed where the emitter places its overlay: only on a chart whose
      value axis is pinned (the model says so), label at the line's right end. */
  function drawReferenceLines(root, context) {
    var model = context.model;
    if (model.axis === null || model.family === 'pie' || model.family === 'doughnut') return;
    var lines = model.options.referenceLines;
    var plot = context.plot, style = context.style;
    var horizontal = model.family === 'bar';
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (line.value < model.axis.min || line.value > model.axis.max) continue;
      var position = context.valuePos(line.value);
      var stroke = '#' + line.color;
      var widthPx = line.width / 0.75;
      var textSize = style.size * 0.95;
      var width = line.label ? textWidth(line.label, textSize, style.family) : 0;
      var room = line.label ? width + 8 : 0;
      var geometry = horizontal
        ? { x1: position, y1: plot.y + room, x2: position, y2: plot.y + plot.h }
        : { x1: plot.x, y1: position, x2: plot.x + plot.w - room, y2: position };
      var shape = node('line', geometry);
      shape.setAttribute('class', 'cp-reference');
      shape.setAttribute('stroke', stroke);
      shape.setAttribute('stroke-width', String(r3(widthPx)));
      var pattern = dashArray(line.dash, widthPx);
      if (pattern) shape.setAttribute('stroke-dasharray', pattern);
      shape.setAttribute('data-cp-value', String(line.value));
      root.appendChild(shape);
      if (line.label) {
        root.appendChild(label(line.label, horizontal
          ? { x: position + 3, y: plot.y + style.size, 'text-anchor': 'start', class: 'cp-reference-label',
              fill: stroke, 'font-family': style.family, 'font-size': textSize }
          : { x: plot.x + plot.w, y: position + style.size * 0.32, 'text-anchor': 'end', class: 'cp-reference-label',
              fill: stroke, 'font-family': style.family, 'font-size': textSize }));
      }
    }
  }

  function drawTitle(root, context) {
    if (!context.style.title) return;
    root.appendChild(label(context.style.title, {
      x: context.w / 2, y: context.style.size * 1.5, 'text-anchor': 'middle', class: 'cp-title',
      fill: context.style.color, 'font-family': context.style.family,
      'font-size': context.style.size * 1.15, 'font-weight': 700
    }));
  }

  /** The legend, in the box `layout()` gives it (`legend_box` — the box a pinned chart writes as
      its legend's manual layout): entries in equal columns, or one per row at the side. Entries are
      authored names only. */
  function drawLegend(root, context, entries) {
    var position = context.model.options.legend;
    var box = context.geometry.legend;
    if (!position || !box || !entries.length) return;
    var style = context.style, size = style.size;
    var swatch = size * 0.8, gap = size * 0.5;
    var group = node('g', { class: 'cp-legend', 'data-cp-legend': position });
    var bx = box.x * context.w, by = box.y * context.h, bw = box.w * context.w, bh = box.h * context.h;
    group.setAttribute('data-cp-box', r3(bx) + ',' + r3(by) + ',' + r3(bw) + ',' + r3(bh));
    var vertical = position === 'left' || position === 'right' || position === 'topRight';
    var columns = vertical ? 1 : Math.max(1, Math.round(bw / (size * 1.3 + 6.0 + Math.max.apply(null,
      entries.map(function (entry) { return estimate(entry.name, size); })))));
    var pitch = vertical ? size * 1.6 : size * 1.6;
    var columnWidth = bw / columns;
    for (var e = 0; e < entries.length; e++) {
      var entry = entries[e];
      var column = e % columns, row = Math.floor(e / columns);
      var x = bx + column * columnWidth + (vertical ? 0 : Math.max(0, (columnWidth - swatch - gap - estimate(entry.name, size)) / 2));
      var y = by + size * 1.25 + row * pitch;
      group.appendChild(node('rect', {
        x: x, y: y - swatch * 0.85, width: swatch, height: swatch, rx: 1,
        fill: entry.color, class: 'cp-legend-swatch'
      }));
      group.appendChild(label(entry.name, {
        x: x + swatch + gap, y: y, class: 'cp-legend-label',
        fill: style.color, 'font-family': style.family, 'font-size': size
      }));
    }
    root.appendChild(group);
  }

  function legendEntries(context) {
    var model = context.model, entries = [], k;
    if (model.family === 'pie' || model.family === 'doughnut') {
      for (k = 0; k < model.categories.length; k++) {
        entries.push({ name: model.categories[k], color: pointColour(model, 0, k) || seriesColour(model, k) });
      }
      return entries;
    }
    if (model.family === 'waterfall' && model.series.length === 1) {
      return [{ name: model.series[0].name, color: '#' + model.waterfall.colors.increase }];
    }
    for (k = 0; k < model.series.length; k++) entries.push({ name: model.series[k].name, color: seriesColour(model, k) });
    return entries;
  }

  // ------------------------------------------------------------------------------ category core

  /** The slot geometry (a gap slot keeps its space) and the value → position mapping. */
  function categoryContext(context) {
    var model = context.model;
    var horizontal = model.family === 'bar';
    var count = Math.max(1, model.categories.length);
    context.valuePos = function (value) {
      var scale = context.scale, plot = context.plot;
      var fraction = (value - scale.min) / (scale.max - scale.min);
      if (scale.reverse) fraction = 1 - fraction;
      /* A value outside the axis range is clipped to the plot, which is what PowerPoint draws. */
      fraction = clamp(fraction, 0, 1);
      return horizontal ? plot.x + fraction * plot.w : plot.y + plot.h - fraction * plot.h;
    };
    context.slotSize = function () { return (horizontal ? context.plot.h : context.plot.w) / count; };
    /* A bar chart's first category is at the bottom, a column chart's on the left; reverse flips it. */
    context.slotStart = function (index) {
      var slot = context.slotSize();
      var reverse = model.options.categoryAxis.reverse === true;
      if (horizontal) return context.plot.y + (reverse ? index : count - 1 - index) * slot;
      return context.plot.x + (reverse ? count - 1 - index : index) * slot;
    };
    context.slotCentre = function (index) { return context.slotStart(index) + context.slotSize() / 2; };
    context.horizontal = horizontal;
    context.gapWidth = model.options.gapWidth;
    context.overlap = model.options.overlap;
    return context;
  }

  /** Bar thickness and the offset of series `index` inside a category slot (OOXML's own formula). */
  function barGeometry(context, seriesCount) {
    var slot = context.slotSize();
    var count = Math.max(1, seriesCount);
    var cluster = count - (count - 1) * context.overlap / 100;
    var thickness = slot / Math.max(0.1, cluster + context.gapWidth / 100);
    var gap = thickness * context.gapWidth / 100;
    return {
      thickness: thickness,
      offsetOf: function (index) { return gap / 2 + index * thickness * (1 - context.overlap / 100); }
    };
  }

  function rectFor(context, start, size, from, to) {
    var a = context.valuePos(from), b = context.valuePos(to);
    var lo = Math.min(a, b), extent = Math.max(0.5, Math.abs(b - a));
    return context.horizontal
      ? { x: lo, y: start, width: extent, height: size }
      : { x: start, y: lo, width: size, height: extent };
  }

  /** A data label at `position` (the model's canonical one) against its piece. */
  function placeLabel(root, context, text, rect, positive, position, ink, extra) {
    var style = context.style, size = style.labelSize;
    var attrs = { class: 'cp-label', fill: style.labelColor || ink, 'font-family': style.family, 'font-size': size,
      'font-weight': style.labelWeight };
    for (var key in extra) if (Object.prototype.hasOwnProperty.call(extra, key)) attrs[key] = extra[key];
    /* Which line of a label broken by `\n` sits where a one-line label would. */
    var align = 'middle';
    if (context.horizontal) {
      attrs.y = rect.y + rect.height / 2 + size * 0.35;
      var farX = positive ? rect.x + rect.width : rect.x, nearX = positive ? rect.x : rect.x + rect.width;
      if (position === 'center') { attrs.x = rect.x + rect.width / 2; attrs['text-anchor'] = 'middle'; }
      else if (position === 'inEnd') { attrs.x = farX + (positive ? -4 : 4); attrs['text-anchor'] = positive ? 'end' : 'start'; }
      else if (position === 'inBase') { attrs.x = nearX + (positive ? 4 : -4); attrs['text-anchor'] = positive ? 'start' : 'end'; }
      else { attrs.x = farX + (positive ? 4 : -4); attrs['text-anchor'] = positive ? 'start' : 'end'; }
    } else {
      attrs.x = rect.x + rect.width / 2;
      attrs['text-anchor'] = 'middle';
      var top = rect.y, bottom = rect.y + rect.height;
      var far = positive ? top : bottom, near = positive ? bottom : top;
      if (position === 'center') attrs.y = rect.y + rect.height / 2 + size * 0.35;
      else if (position === 'inEnd') { attrs.y = positive ? far + size * 1.05 : far - size * 0.35; align = positive ? 'top' : 'bottom'; }
      else if (position === 'inBase') { attrs.y = positive ? near - size * 0.35 : near + size * 1.05; align = positive ? 'bottom' : 'top'; }
      else { attrs.y = positive ? far - 4 : far + size; align = positive ? 'bottom' : 'top'; }
    }
    root.appendChild(label(text, attrs, align));
  }

  function drawBars(root, context) {
    var model = context.model, options = model.options;
    var series = model.series;
    var stacked = model.stacked !== '';
    var geometry = barGeometry(context, stacked ? 1 : series.length);
    var labelFormat = options.labelFormat || options.numberFormat;
    var position = options.labelPosition;
    var inside = position === 'center' || position === 'inEnd' || position === 'inBase';
    var count = model.categories.length;
    var totals = [], positiveTop = [], negativeTop = [], p, s;
    for (p = 0; p < count; p++) {
      var total = 0;
      for (s = 0; s < series.length; s++) if (series[s].values[p] !== null) total += Math.abs(series[s].values[p]);
      totals.push(total);
      positiveTop.push(0);
      negativeTop.push(0);
    }
    var ink = context.style.color;
    for (s = 0; s < series.length; s++) {
      var group = node('g', { class: 'cp-series', 'data-cp-series': s, 'data-cp-name': series[s].name });
      for (p = 0; p < count; p++) {
        var raw = series[s].values[p];
        if (raw === null) continue;                     // gap slot / missing point: no bar, slot kept
        var value = raw;
        if (model.stacked === 'stacked100') value = totals[p] ? (raw / totals[p]) * 100 : 0;
        var from = 0, to = value;
        if (stacked) {
          if (value >= 0) { from = positiveTop[p]; to = from + value; positiveTop[p] = to; }
          else { from = negativeTop[p]; to = from + value; negativeTop[p] = to; }
        }
        var start = context.slotStart(p) + geometry.offsetOf(stacked ? 0 : s);
        var rect = rectFor(context, start, geometry.thickness, from, to);
        var fill = pointColour(model, s, p) || seriesColour(model, s);
        group.appendChild(node('rect', {
          x: rect.x, y: rect.y, width: rect.width, height: rect.height, fill: fill,
          class: 'cp-bar', 'data-cp-series': s, 'data-cp-point': p, 'data-cp-value': raw
        }));
        var barText = pointText(model, s, p, raw, labelFormat);
        if (labelsOn(model, s) && barText !== '') {
          placeLabel(group, context, barText, rect, to >= from, position,
            inside ? contrastInk(fill, ink) : ink, { 'data-cp-value': raw, 'data-cp-series': s, 'data-cp-point': p });
        }
      }
      root.appendChild(group);
    }
  }

  /** The plan's cells stacked exactly as a stacked column stacks them: positive cells upward from
      zero and negative cells downward from zero, each in series order. Returns the drawn pieces. */
  function stackedPieces(plan, count) {
    var positive = [], negative = [], pieces = [];
    for (var i = 0; i < count; i++) { positive.push(0); negative.push(0); }
    for (var s = 0; s < plan.series.length; s++) {
      var entry = plan.series[s];
      for (var p = 0; p < count; p++) {
        var cell = entry.values[p];
        if (cell === null) continue;
        var from, to;
        if (cell >= 0) { from = positive[p]; to = from + cell; positive[p] = to; }
        else { from = negative[p]; to = from + cell; negative[p] = to; }
        if (entry.visible) pieces.push({ series: s, point: p, from: from, to: to, cell: cell });
      }
    }
    return pieces;
  }

  function drawWaterfall(root, context) {
    var model = context.model, options = model.options, plan = model.waterfall;
    var count = model.categories.length;
    var single = model.series.length === 1;
    var geometry = barGeometry(context, 1);
    var ink = context.style.color;
    var pieces = stackedPieces(plan, count);
    var byCell = {};
    var group = node('g', { class: 'cp-series cp-waterfall', 'data-cp-name': 'waterfall' });

    for (var n = 0; n < pieces.length; n++) {
      var piece = pieces[n];
      var entry = plan.series[piece.series];
      var start = context.slotStart(piece.point) + geometry.offsetOf(0);
      var rect = rectFor(context, start, geometry.thickness, piece.from, piece.to);
      var fill = pointColour(model, entry.source, piece.point) ||
        (single ? '#' + plan.colors[plan.roles[piece.point]] : seriesColour(model, entry.source));
      byCell[piece.series + ',' + piece.point] = { rect: rect, fill: fill, piece: piece };
      group.appendChild(node('rect', {
        x: rect.x, y: rect.y, width: rect.width, height: rect.height, fill: fill, class: 'cp-bar',
        'data-cp-series': piece.series, 'data-cp-source': entry.source, 'data-cp-point': piece.point,
        'data-cp-value': piece.cell, 'data-cp-from': piece.from, 'data-cp-to': piece.to,
        'data-cp-total': plan.roles[piece.point] === 'total' ? '1' : '0'
      }));
    }

    // Connectors: from the edge of bar i facing bar i+1 to the edge of bar i+1 facing i, at the level.
    var style = options.connectors;
    if (style !== false) {
      for (var c = 0; c < plan.connectors.length; c++) {
        var connector = plan.connectors[c];
        var y = context.valuePos(connector.level);
        var a0 = context.slotStart(connector.index) + geometry.offsetOf(0), a1 = a0 + geometry.thickness;
        var b0 = context.slotStart(connector.index + 1) + geometry.offsetOf(0), b1 = b0 + geometry.thickness;
        var ends = b0 >= a1 ? [a1, b0] : [a0, b1];
        var widthPx = style.width / 0.75;
        var line = node('line', context.horizontal
          ? { x1: y, y1: ends[0], x2: y, y2: ends[1] }
          : { x1: ends[0], y1: y, x2: ends[1], y2: y });
        line.setAttribute('class', 'cp-connector');
        line.setAttribute('stroke', '#' + style.color);
        line.setAttribute('stroke-width', String(r3(widthPx)));
        var pattern = dashArray(style.dash, widthPx);
        if (pattern) line.setAttribute('stroke-dasharray', pattern);
        line.setAttribute('data-cp-index', String(connector.index));
        line.setAttribute('data-cp-value', String(connector.level));
        group.appendChild(line);
      }
    }

    // Labels: the plan's, native (inside, the role's format) or frozen; outEnd as text above/below.
    var position = options.labelPosition || 'center';
    var outside = position === 'outEnd';
    if (outside) {
      for (var i = 0; i < plan.roles.length; i++) {
        var role = plan.roles[i];
        if (role === 'gap') continue;
        var labelled = false;
        for (var k = 0; k < model.series.length; k++) {
          if (model.series[k].values[i] !== null && labelsOn(model, k)) labelled = true;
        }
        var outText = plan.texts[i] !== null ? plan.texts[i] : formatNumber(plan.values[i], plan.formats[role]);
        if (!labelled || outText === '') continue;
        var value = plan.values[i];
        var above = role === 'increase' || (role === 'total' && value >= 0);
        var anchor = clamp(above ? plan.tops[i] : plan.bottoms[i], context.scale.min, context.scale.max);
        var at = context.valuePos(anchor);
        var centre = context.slotStart(i) + geometry.offsetOf(0) + geometry.thickness / 2;
        var size = context.style.labelSize;
        group.appendChild(label(outText, {
          x: centre, y: above ? at - 2 - size * 0.3 : at + 2 + size * 0.95, 'text-anchor': 'middle',
          class: 'cp-label cp-label-out', fill: context.style.labelColor || ink, 'font-family': context.style.family,
          'font-size': size, 'font-weight': context.style.labelWeight, 'data-cp-point': i, 'data-cp-value': value
        }, above ? 'bottom' : 'top'));
      }
    } else {
      for (var g = 0; g < plan.segments.length; g++) {
        var segment = plan.segments[g];
        if (!labelsOn(model, segment.source)) continue;
        var target = segment.label;
        var cell = plan.series[target.series].values[target.point];
        var text = target.text !== null ? target.text : formatNumber(cell, target.format);
        if (text === '') continue;
        var drawn = byCell[target.series + ',' + target.point];
        var flat = segment.lo === segment.hi;
        var rect2;
        if (drawn && !flat) {
          rect2 = drawn.rect;
        } else {
          var startAt = context.slotStart(target.point) + geometry.offsetOf(0);
          rect2 = rectFor(context, startAt, geometry.thickness, segment.lo, segment.hi);
        }
        var textInk = drawn && !flat ? contrastInk(drawn.fill, ink) : ink;
        var positive = drawn ? drawn.piece.to >= drawn.piece.from : true;
        placeLabel(group, context, text, rect2, positive, flat ? 'center' : position, textInk, {
          'data-cp-series': target.series, 'data-cp-point': target.point, 'data-cp-value': segment.hi - segment.lo,
          'data-cp-frozen': target.text !== null ? '1' : '0'
        });
      }
    }
    root.appendChild(group);
  }

  function smoothPath(points) {
    /* Catmull-Rom → cubic Bézier: the same shape PowerPoint's `smooth` flag gives, near enough. */
    var d = 'M ' + r3(points[0][0]) + ' ' + r3(points[0][1]);
    for (var i = 0; i < points.length - 1; i++) {
      var p0 = points[i === 0 ? 0 : i - 1], p1 = points[i], p2 = points[i + 1];
      var p3 = points[i + 2 < points.length ? i + 2 : i + 1];
      var c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
      var c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
      d += ' C ' + r3(c1[0]) + ' ' + r3(c1[1]) + ', ' + r3(c2[0]) + ' ' + r3(c2[1]) + ', ' + r3(p2[0]) + ' ' + r3(p2[1]);
    }
    return d;
  }

  function linePath(points, smooth) {
    if (!points.length) return '';
    if (smooth && points.length > 2) return smoothPath(points);
    var d = 'M ' + r3(points[0][0]) + ' ' + r3(points[0][1]);
    for (var i = 1; i < points.length; i++) d += ' L ' + r3(points[i][0]) + ' ' + r3(points[i][1]);
    return d;
  }

  function markersFor(model, entry) {
    var base = model.base || '';
    var wanted = entry.marker !== null ? entry.marker : model.options.markers;
    if (wanted === false || wanted === 'none') return false;
    if (wanted === true || (isStr(wanted) && wanted !== 'auto')) return true;
    if (model.family === 'scatter') return !/_no_markers$/.test(base);
    return base.indexOf('line_markers') === 0;
  }

  function drawLines(root, context) {
    var model = context.model, options = model.options, series = model.series;
    var area = model.family === 'area';
    var stacked = model.stacked !== '';
    var labelFormat = options.labelFormat || options.numberFormat;
    var baseline = context.valuePos(clamp(0, context.scale.min, context.scale.max));
    var count = model.categories.length;
    var cumulative = [], lower = [], totals = [], c;
    for (c = 0; c < count; c++) {
      cumulative.push(0);
      lower.push(baseline);
      var total = 0;
      for (var t = 0; t < series.length; t++) if (series[t].values[c] !== null) total += Math.abs(series[t].values[c]);
      totals.push(total);
    }
    for (var s = 0; s < series.length; s++) {
      var entry = series[s];
      var group = node('g', { class: 'cp-series', 'data-cp-series': s, 'data-cp-name': entry.name });
      var color = seriesColour(model, s);
      var widthPt = entry.lineWidth !== null ? entry.lineWidth : (options.lineWidth !== null ? options.lineWidth : 2.25);
      var markerPx = (entry.markerSize !== null ? entry.markerSize : (options.markerSize !== null ? options.markerSize : 7)) / 0.75;
      var smooth = (entry.smooth !== null ? entry.smooth : options.smooth) === true;
      var marks = markersFor(model, entry);
      var runs = [], current = [], values = [];
      for (var p = 0; p < count; p++) {
        var raw = entry.values[p];
        if (raw === null) {                              // a gap slot breaks the line, as it should
          if (current.length) { runs.push({ points: current, values: values }); current = []; values = []; }
          continue;
        }
        var value = raw;
        if (model.stacked === 'stacked100') value = totals[p] ? (raw / totals[p]) * 100 : 0;
        var plotted = stacked ? cumulative[p] + value : value;
        if (stacked) cumulative[p] = plotted;
        current.push([context.slotCentre(p), context.valuePos(plotted)]);
        values.push({ raw: raw, point: p });
      }
      if (current.length) runs.push({ points: current, values: values });
      for (var rIndex = 0; rIndex < runs.length; rIndex++) {
        var run = runs[rIndex];
        if (area && run.points.length) {
          var d = linePath(run.points, smooth);
          for (var back = run.points.length - 1; back >= 0; back--) {
            d += ' L ' + r3(run.points[back][0]) + ' ' + r3(lower[run.values[back].point]);
          }
          d += ' Z';
          group.appendChild(node('path', { d: d, fill: color, 'fill-opacity': stacked ? 1 : 0.65, class: 'cp-area' }));
        }
        if (run.points.length > 1 || !area) {
          var stroke = node('path', {
            d: linePath(run.points, smooth), fill: 'none', stroke: color, 'stroke-width': area ? 1.5 : widthPt / 0.75,
            'stroke-linejoin': 'round', 'stroke-linecap': 'round', class: 'cp-line'
          });
          var pattern = dashArray(entry.dash !== null ? entry.dash : options.dash, widthPt / 0.75);
          if (pattern) stroke.setAttribute('stroke-dasharray', pattern);
          group.appendChild(stroke);
        }
        for (var m = 0; m < run.points.length; m++) {
          var point = run.points[m], record = run.values[m];
          if (marks && !area) {
            group.appendChild(node('circle', {
              cx: point[0], cy: point[1], r: markerPx / 2, fill: color, class: 'cp-marker',
              'data-cp-series': s, 'data-cp-point': record.point, 'data-cp-value': record.raw
            }));
          }
          var lineText = pointText(model, s, record.point, record.raw, labelFormat);
          if (labelsOn(model, s) && lineText !== '') {
            group.appendChild(label(lineText, {
              x: point[0], y: point[1] - markerPx * 0.6 - 2, 'text-anchor': 'middle', class: 'cp-label',
              fill: context.style.labelColor || context.style.color, 'font-family': context.style.family,
              'font-size': context.style.labelSize, 'font-weight': context.style.labelWeight,
              'data-cp-value': record.raw, 'data-cp-series': s, 'data-cp-point': record.point
            }, 'bottom'));
          }
        }
      }
      if (stacked) {
        for (var band = 0; band < runs.length; band++) {
          for (var at = 0; at < runs[band].points.length; at++) lower[runs[band].values[at].point] = runs[band].points[at][1];
        }
      }
      root.appendChild(group);
    }
  }

  // ------------------------------------------------------------------------------------- pie

  function polar(cx, cy, radius, degrees) {
    var radians = degrees * Math.PI / 180;
    return [cx + radius * Math.cos(radians), cy + radius * Math.sin(radians)];
  }

  function slicePath(cx, cy, outer, inner, startDeg, endDeg) {
    var large = (endDeg - startDeg) > 180 ? 1 : 0;
    var a = polar(cx, cy, outer, startDeg), b = polar(cx, cy, outer, endDeg);
    if (inner <= 0.01) {
      return 'M ' + r3(cx) + ' ' + r3(cy) + ' L ' + r3(a[0]) + ' ' + r3(a[1]) +
        ' A ' + r3(outer) + ' ' + r3(outer) + ' 0 ' + large + ' 1 ' + r3(b[0]) + ' ' + r3(b[1]) + ' Z';
    }
    var c = polar(cx, cy, inner, endDeg), d = polar(cx, cy, inner, startDeg);
    return 'M ' + r3(a[0]) + ' ' + r3(a[1]) +
      ' A ' + r3(outer) + ' ' + r3(outer) + ' 0 ' + large + ' 1 ' + r3(b[0]) + ' ' + r3(b[1]) +
      ' L ' + r3(c[0]) + ' ' + r3(c[1]) +
      ' A ' + r3(inner) + ' ' + r3(inner) + ' 0 ' + large + ' 0 ' + r3(d[0]) + ' ' + r3(d[1]) + ' Z';
  }

  function drawPie(root, context) {
    var model = context.model, options = model.options, style = context.style;
    var entry = model.series[0] || { values: [], explosion: null };
    var values = entry.values;
    var plot = context.plot;
    var exploded = /_exploded/.test(model.base || '');
    var explosionPct = entry.explosion !== null ? entry.explosion : (options.explosion !== null ? options.explosion : (exploded ? 25 : 0));
    var maxRadius = Math.max(4, Math.min(plot.w, plot.h) / 2 - style.size * 0.6);
    var radius = Math.max(4, maxRadius / (1 + explosionPct / 100));
    var explode = radius * explosionPct / 100;
    var cx = plot.x + plot.w / 2, cy = plot.y + plot.h / 2;
    /* PowerPoint's doughnut hole is 50 % of the radius unless the spec says otherwise. */
    var hole = model.family === 'doughnut' ? clamp(options.holeSize !== null ? options.holeSize : 50, 0, 90) : 0;
    var inner = radius * hole / 100;
    var labelFormat = options.labelFormat || options.numberFormat;

    var total = 0, i;
    for (i = 0; i < values.length; i++) if (values[i] !== null) total += Math.abs(values[i]);
    if (!(total > 0)) total = 1;

    var group = node('g', {
      class: 'cp-pie', 'data-cp-radius': radius, 'data-cp-inner-radius': inner,
      'data-cp-centre': r3(cx) + ',' + r3(cy)
    });
    var angle = -90;                                   // PowerPoint starts the first slice at 12 o'clock
    for (var p = 0; p < values.length; p++) {
      var value = values[p] !== null ? Math.abs(values[p]) : 0;
      if (value <= 0) continue;
      var sweep = value / total * 360;
      var mid = angle + sweep / 2;
      var offset = explode ? polar(0, 0, explode, mid) : [0, 0];
      var colors = model.colors;
      var chosen = colors.length ? colors[p % colors.length] : null;
      var fill = pointColour(model, 0, p) || (chosen ? '#' + chosen : DEFAULT_COLORS[p % DEFAULT_COLORS.length]);
      group.appendChild(node('path', {
        d: slicePath(cx + offset[0], cy + offset[1], radius, inner, angle, angle + sweep),
        fill: fill, class: 'cp-slice', 'data-cp-point': p, 'data-cp-value': values[p],
        'data-cp-start': angle, 'data-cp-sweep': sweep
      }));
      var sliceText = pointText(model, 0, p, values[p], labelFormat);
      if (labelsOn(model, 0) && sliceText !== '') {
        var at = polar(cx + offset[0], cy + offset[1], inner > 0 ? (inner + radius) / 2 : radius * 0.68, mid);
        group.appendChild(label(sliceText, {
          x: at[0], y: at[1] + style.size * 0.35, 'text-anchor': 'middle', class: 'cp-label',
          fill: style.labelColor || contrastInk(fill, style.color), 'font-family': style.family,
          'font-size': style.labelSize, 'font-weight': style.labelWeight,
          'data-cp-value': values[p], 'data-cp-series': 0, 'data-cp-point': p
        }, 'middle'));
      }
      angle += sweep;
    }
    root.appendChild(group);
  }

  // ---------------------------------------------------------------------------------- scatter

  /** The x axis of a scatter chart: the author's min/max/majorUnit, else a nice range over x and 0.
      Drawing only: the file keeps PowerPoint's automatic x axis unless the author gave min and max. */
  function xRange(model) {
    var axisOptions = model.options.categoryAxis;
    var xs = [];
    for (var k = 0; k < model.series.length; k++) {
      var list = model.series[k].x || [];
      for (var i = 0; i < list.length; i++) if (list[i] !== null) xs.push(list[i]);
    }
    var low = xs.length ? Math.min(0, Math.min.apply(null, xs)) : 0;
    var high = xs.length ? Math.max(0, Math.max.apply(null, xs)) : 1;
    var lo = axisOptions.min !== null ? axisOptions.min : low;
    var hi = axisOptions.max !== null ? axisOptions.max : high;
    var nice = niceRange(lo, hi);
    var step = axisOptions.majorUnit !== null ? axisOptions.majorUnit : nice[2];
    lo = axisOptions.min !== null ? axisOptions.min : Math.floor(lo / step) * step;
    hi = axisOptions.max !== null ? axisOptions.max : Math.ceil(hi / step) * step;
    if (hi <= lo) hi = lo + step;
    return { min: lo, max: hi, ticks: axisTicks({ min: lo, max: hi, majorUnit: step }), reverse: axisOptions.reverse === true };
  }

  function drawScatter(root, context) {
    var model = context.model, options = model.options, style = context.style;
    var plot = context.plot;
    var xScale = context.xScale;
    var labelFormat = options.labelFormat || options.numberFormat;
    var base = model.base || '';
    var joined = base !== 'scatter';

    function xPos(value) {
      var fraction = (value - xScale.min) / (xScale.max - xScale.min);
      if (xScale.reverse) fraction = 1 - fraction;
      return plot.x + clamp(fraction, 0, 1) * plot.w;
    }

    for (var s = 0; s < model.series.length; s++) {
      var entry = model.series[s];
      var color = seriesColour(model, s);
      var group = node('g', { class: 'cp-series', 'data-cp-series': s, 'data-cp-name': entry.name });
      var xs = entry.x || [], ys = entry.y || [];
      var widthPt = entry.lineWidth !== null ? entry.lineWidth : (options.lineWidth !== null ? options.lineWidth : 2.25);
      var markerPx = (entry.markerSize !== null ? entry.markerSize : (options.markerSize !== null ? options.markerSize : 7)) / 0.75;
      var points = [];
      for (var p = 0; p < Math.min(xs.length, ys.length); p++) {
        if (xs[p] === null || ys[p] === null) continue;
        points.push([xPos(xs[p]), context.valuePos(ys[p]), p, xs[p], ys[p]]);
      }
      if ((joined || entry.line === true) && points.length > 1) {
        group.appendChild(node('path', {
          d: linePath(points, /_smooth/.test(base)), fill: 'none', stroke: color, 'stroke-width': widthPt / 0.75,
          'stroke-linejoin': 'round', class: 'cp-line'
        }));
      }
      var marks = markersFor(model, entry);
      for (var m = 0; m < points.length; m++) {
        if (marks) {
          group.appendChild(node('circle', {
            cx: points[m][0], cy: points[m][1], r: markerPx / 2, fill: color, class: 'cp-marker',
            'data-cp-series': s, 'data-cp-point': points[m][2], 'data-cp-x': points[m][3], 'data-cp-value': points[m][4]
          }));
        }
        var xyText = pointText(model, s, points[m][2], points[m][4], labelFormat);
        if (labelsOn(model, s) && xyText !== '') {
          group.appendChild(label(xyText, {
            x: points[m][0], y: points[m][1] - markerPx * 0.6 - 2, 'text-anchor': 'middle',
            class: 'cp-label', fill: style.labelColor || style.color, 'font-family': style.family,
            'font-size': style.labelSize, 'font-weight': style.labelWeight,
            'data-cp-value': points[m][4], 'data-cp-series': s, 'data-cp-point': points[m][2]
          }, 'bottom'));
        }
      }
      root.appendChild(group);
    }
  }

  function drawScatterAxes(root, context) {
    var model = context.model, plot = context.plot, style = context.style;
    var gridlines = model.options.gridlines;
    var tickFormat = model.options.valueAxis.format || model.options.numberFormat;
    var i, position;
    for (i = 0; i < context.scale.ticks.length; i++) {
      position = context.valuePos(context.scale.ticks[i]);
      if (gridlines) {
        var grid = node('line', { x1: plot.x, y1: position, x2: plot.x + plot.w, y2: position, class: 'cp-gridline' });
        grid.setAttribute('stroke', '#' + gridlines.color);
        grid.setAttribute('stroke-width', String(r3(gridlines.width / 0.75)));
        root.appendChild(grid);
      }
      if (model.options.valueAxis.visible) {
        root.appendChild(label(formatNumber(context.scale.ticks[i], tickFormat), {
          x: plot.x - 6, y: position + style.size * 0.35, 'text-anchor': 'end', class: 'cp-tick',
          fill: style.color, 'font-family': style.family, 'font-size': style.size,
          'font-weight': model.options.valueAxis.bold ? 700 : null, 'data-cp-value': context.scale.ticks[i]
        }));
      }
    }
    var xFormat = model.options.categoryAxis.format || model.options.numberFormat;
    for (i = 0; i < context.xScale.ticks.length; i++) {
      var value = context.xScale.ticks[i];
      var fraction = (value - context.xScale.min) / (context.xScale.max - context.xScale.min);
      if (context.xScale.reverse) fraction = 1 - fraction;
      position = plot.x + fraction * plot.w;
      if (model.options.categoryAxis.visible) {
        root.appendChild(label(formatNumber(value, xFormat), {
          x: position, y: plot.y + plot.h + style.size * 1.15, 'text-anchor': 'middle', class: 'cp-tick',
          fill: style.color, 'font-family': style.family, 'font-size': style.size,
          'font-weight': model.options.categoryAxis.bold ? 700 : null, 'data-cp-value': value
        }));
      }
    }
    var axisLine = node('line', { x1: plot.x, y1: plot.y + plot.h, x2: plot.x + plot.w, y2: plot.y + plot.h, class: 'cp-axis' });
    axisLine.setAttribute('stroke', AXIS_COLOR);
    axisLine.setAttribute('stroke-width', '0.75');
    root.appendChild(axisLine);
  }

  // ------------------------------------------------------------------------------ notices

  function drawNotice(root, context, headline, detail) {
    root.appendChild(node('rect', {
      x: 0.5, y: 0.5, width: Math.max(1, context.w - 1), height: Math.max(1, context.h - 1),
      fill: 'none', stroke: '#E5484D', 'stroke-width': 1, 'stroke-dasharray': '4 3', class: 'cp-notice-box'
    }));
    var style = context.style;
    root.appendChild(label(headline, {
      x: context.w / 2, y: context.h / 2 - (detail ? style.size * 0.4 : -style.size * 0.35),
      'text-anchor': 'middle', class: 'cp-notice', fill: '#E5484D',
      'font-family': style.family, 'font-size': Math.min(style.size, 12)
    }));
    if (detail) {
      root.appendChild(label(detail, {
        x: context.w / 2, y: context.h / 2 + style.size * 1.1, 'text-anchor': 'middle',
        class: 'cp-notice-detail', fill: '#9AA5AD', 'font-family': style.family,
        'font-size': Math.min(style.size, 12) * 0.9
      }));
    }
  }

  // ------------------------------------------------------------------------------ render one

  function clearPrevious(element) {
    var children = element.childNodes, index = children.length - 1;
    for (; index >= 0; index--) {
      var child = children[index];
      if (child.nodeType === 1 && child.getAttribute && child.getAttribute('data-chart-preview-root') !== null) {
        element.removeChild(child);
      }
    }
  }

  function finish(element, status, message) {
    element.setAttribute('data-chart-preview', status);
    if (message) element.setAttribute('data-chart-preview-message', message);
    else element.removeAttribute('data-chart-preview-message');
    return status;
  }

  /** Draw one `[data-chart]` element. Returns 'ok' | 'unsupported' | 'error' | 'skipped'. */
  function render(element) {
    clearPrevious(element);
    element.removeAttribute('data-chart-preview-notes');
    var spec;
    try {
      spec = JSON.parse(element.getAttribute('data-chart'));
    } catch (error) {
      spec = null;
    }
    var frame = frameOf(element);
    var broken = !isDict(spec);

    if (frame.w < 2 || frame.h < 2) {
      /* Nothing can be drawn into no space, and guessing a size would draw a chart the export will
         not place. Say so on the element rather than leaving a silent blank. */
      return finish(element, 'skipped', 'chart element has no size (' + r3(frame.w) + '×' + r3(frame.h) + ')');
    }

    var model = normalise(broken ? {} : spec);
    var geometry = layout(model, frame.w, frame.h, frame.sizePx);
    var options = model.options;
    var ownLabels = options.labelStyle || { bold: false, color: null, fontSize: null };
    var style = {
      family: options.font || frame.fontFamily || 'Arial, sans-serif',
      size: geometry.sizePx,
      color: options.fontColor ? '#' + options.fontColor : (frame.color || DARK_INK),
      title: options.title || null,
      /* `options.labelStyle`: the labels' size (px), ink (in place of the contrast pick) and weight. */
      labelSize: ownLabels.fontSize !== null ? labelSize(model, geometry.sizePx) : geometry.sizePx * 0.95,
      labelColor: ownLabels.color ? '#' + ownLabels.color : null,
      labelWeight: ownLabels.bold ? 700 : null
    };
    if (!broken) {
      element.setAttribute('data-chart-preview-notes', JSON.stringify(model.notes.map(function (n) { return n.text; })));
    }

    var root = node('svg', {
      width: frame.w, height: frame.h, viewBox: '0 0 ' + r3(frame.w) + ' ' + r3(frame.h),
      xmlns: NS, class: 'chart-preview', 'data-chart-preview-root': '',
      'data-cp-version': VERSION
    });
    /* The outermost SVG clips to its viewport by default, which is what a PowerPoint graphic frame
       does too: a label that does not fit is cut off, it does not paint over the neighbouring text. */
    root.setAttribute('style',
      'position:absolute;left:' + r3(frame.x) + 'px;top:' + r3(frame.y) + 'px;' +
      'width:' + r3(frame.w) + 'px;height:' + r3(frame.h) + 'px;pointer-events:none');
    if (frame.position === 'static') element.style.position = 'relative';

    var context = { model: model, geometry: geometry, style: style, w: frame.w, h: frame.h };

    if (broken) {
      drawNotice(root, context, 'chart spec is not valid JSON', 'fix the data-chart attribute');
      element.insertBefore(root, element.firstChild);
      return finish(element, 'error', 'data-chart is not valid JSON');
    }
    if (model.type === null) {
      drawNotice(root, context, 'chart not built', model.reason);
      element.insertBefore(root, element.firstChild);
      return finish(element, 'unsupported', model.reason);
    }

    var area = geometry.plotArea;
    context.plot = { x: area.x * frame.w, y: area.y * frame.h, w: Math.max(1, area.w * frame.w), h: Math.max(1, area.h * frame.h) };

    var family = model.family;
    if (family === 'pie' || family === 'doughnut') {
      drawTitle(root, context);
      drawPie(root, context);
      drawLegend(root, context, legendEntries(context));
    } else if (family === 'scatter') {
      context.scale = valueScale(context);
      context.xScale = xRange(model);
      context.valuePos = function (value) {
        var fraction = (value - context.scale.min) / (context.scale.max - context.scale.min);
        if (context.scale.reverse) fraction = 1 - fraction;
        return context.plot.y + context.plot.h - clamp(fraction, 0, 1) * context.plot.h;
      };
      context.horizontal = false;
      drawTitle(root, context);
      drawScatterAxes(root, context);
      drawScatter(root, context);
      drawReferenceLines(root, context);        // an overlay: on top of the data, like the emitter's
      drawLegend(root, context, legendEntries(context));
    } else {
      context.scale = valueScale(context);
      categoryContext(context);
      drawTitle(root, context);
      drawAxes(root, context);
      if (family === 'column' || family === 'bar') drawBars(root, context);
      else if (family === 'waterfall') drawWaterfall(root, context);
      else drawLines(root, context);
      drawReferenceLines(root, context);        // an overlay: on top of the data, like the emitter's
      drawLegend(root, context, legendEntries(context));
    }

    root.setAttribute('data-cp-family', family);
    root.setAttribute('data-cp-stacked', model.stacked || 'none');
    root.setAttribute('data-cp-pinned', geometry.pinned ? '1' : '0');
    root.setAttribute('data-cp-plot',
      r3(context.plot.x) + ',' + r3(context.plot.y) + ',' + r3(context.plot.w) + ',' + r3(context.plot.h));
    if (context.scale) root.setAttribute('data-cp-value-range', r3(context.scale.min) + ',' + r3(context.scale.max));
    element.insertBefore(root, element.firstChild);
    return finish(element, 'ok', null);
  }

  // ------------------------------------------------------------------------------ render all

  /**
   * Draw every `[data-chart]` under `root` (a Document or an Element, the root itself included).
   * Idempotent: call it as often as you like. Returns how many elements it handled — each one's
   * outcome is on the element itself as `data-chart-preview`.
   */
  function renderAll(root) {
    var scope = root || document;
    var elements = [];
    if (scope.nodeType === 1 && scope.hasAttribute && scope.hasAttribute('data-chart')) elements.push(scope);
    var found = scope.querySelectorAll ? scope.querySelectorAll('[data-chart]') : [];
    for (var i = 0; i < found.length; i++) elements.push(found[i]);
    var drawn = 0;
    for (var e = 0; e < elements.length; e++) {
      try {
        render(elements[e]);
        drawn++;
      } catch (error) {
        /* One broken spec must not stop the rest of the deck from drawing. */
        try {
          finish(elements[e], 'error', String((error && error.message) || error));
        } catch (ignored) { /* the element is beyond help */ }
      }
    }
    return drawn;
  }

  var ChartPreview = {
    VERSION: VERSION,
    renderAll: renderAll,
    render: render,
    normalise: normalise,
    layout: layout,
    resolveType: resolveType,
    resolveTypeName: resolveTypeName,
    familyOf: familyOf,
    formatNumber: formatNumber,
    formatUnit: formatUnit,
    textLines: textLines,
    signedFormat: signedFormat,
    tickText: tickText,
    niceRange: niceRange,
    axisRange: axisRange,
    stackedPieces: stackedPieces,
    TABLES: TABLES,
    FAMILIES: ['column', 'bar', 'line', 'area', 'pie', 'doughnut', 'scatter', 'waterfall']
  };

  global.ChartPreview = ChartPreview;

  /* Auto-run for the normal page load; `render_reference` injects this file after load and calls
     renderAll itself, which is why the readyState branch matters. */
  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', function () { renderAll(document); });
    } else {
      renderAll(document);
    }
  }
})(typeof window !== 'undefined' ? window : this);
