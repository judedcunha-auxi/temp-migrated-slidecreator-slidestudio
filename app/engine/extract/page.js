/*
 * engine/extract/page.js — the browser half of WP1's extractor.
 *
 * Injected into the measuring page by `engine/extract/html.py`, which then calls
 * `window.__engineExtract(options)` once and converts the records it returns into IR elements.
 * Everything that needs the layout engine lives here; Python owns file paths, screenshots and the
 * IR dataclasses. The split matters: a measurement is only true if the browser makes it, and a
 * value is only reproducible if nothing in Python guesses at what the browser would have done.
 *
 * Three rules this file exists to hold:
 *
 *  1. **Resolved values, never source CSS.** Everything comes from `getComputedStyle` and
 *     `getClientRects`, so a colour is a colour, a length is a pixel, and a line is where the
 *     browser actually broke it.
 *  2. **Lines come from `Range` rects, one character at a time.** `Range.getClientRects()` over a
 *     whole element returns a rect per inline box and duplicates nested ones, so it cannot say
 *     which characters are on which line. Per-character ranges can, and they are what make
 *     "a run spanning two lines is split with the exact text of each fragment" true rather than
 *     approximately true.
 *  3. **Paint order is a stacking walk.** Negative z-index first, then everything in flow in DOM
 *     order, then positioned/auto, then positive z-index — because the emitter stacks shapes in
 *     list order and a wrong order is a wrong slide.
 */
(function () {
  "use strict";

  /** Fragments whose tops differ by less than this are the same line (WP1 brief, precision rules). */
  var LINE_EPS = 0.5;
  /**
   * A rebuilt `::before`/`::after` may move no element or text rect by more than this (px), or it is
   * reverted. Chromium lays out in 1/64 px units and serialises used lengths to ~4 decimals, so an
   * exact rebuild moves nothing measurable; 0.25 px is well under anything a gate or a reader sees.
   */
  var PSEUDO_SHIFT_EPS = 0.25;
  /** Nothing narrower or shorter than this is emitted, except border lines. */
  var MIN_EXTENT = 0.5;
  /**
   * How far text may run below the box its author sized before `text-overflow` is reported: this
   * many px, or half a line, whichever is larger. A line box is taller than the glyphs in it and a
   * declared height is usually a round number, so a healthy heading clears its box by a few px.
   */
  var OVERFLOW_MIN_PX = 4;
  var OVERFLOW_HALF_LINE = 0.5;
  /** A quoted first line longer than this is cut at the last word boundary after QUOTE_MIN. */
  var QUOTE_MAX = 60;
  var QUOTE_MIN = 30;

  // Side-by-side text (docs/archive/engine/fidelity/11-WPB-side-by-side.md §3). A paragraph run is an
  // element only while its paragraphs sit *under* one another; the browser lays text out in two
  // dimensions and every constant below is a measured tolerance on that geometry.
  /**
   * R1: the next paragraph stacks under the previous one when its first line box starts no higher
   * than the previous one's last line box ends, less this fraction of the shorter of the two line
   * boxes. Python twin: `config.TEXT_ROW_SHARE` (`text_deck.text_rows` flags strictly more overlap).
   * Measured (B1, 2,850 decisions on 225 project slides, 99 on 29 fixture slides): every
   * x-overlapping pair that belongs together has r = gap / shorter line ≥ −0.129, every pair that
   * does not has r ≤ −1.5625, so any value in [0.13, 1.56) decides the same. 0.15, not 0.5: the
   * emitter cannot write a negative gap (`text.paragraph_gap` clamps at 0), so a merged overlap
   * pushes every later line of the element down by it — 0.15 bounds that to 15 % of a line
   * (B1 critique #8: a −7 px margin under a 28 px heading merged at 0.5 and came out 7 px low).
   */
  var STACK_TOL = 0.15;
  /** R4: a line within this many px of the element box's edge is flush with it (`config.TEXT_ALIGN_TOL_PX`). */
  var ALIGN_TOL = 1.5;
  /**
   * R4: a flex or grid box whose first line has no more than twice this much vertical slack (px)
   * says nothing about its anchor: `top`.
   */
  var ANCHOR_TOL = 1.5;
  /**
   * R3: an atomic inline box with more horizontal geometry than this (px) is detached from its line.
   * R8 reuses it: a list item whose first line starts this far right of its content box starts after
   * an inline box.
   */
  var DETACH_GEOMETRY_PX = 0.5;
  /** R6: two fragments of one line at least this far apart (px), with no space between, are glued. */
  var SEAM_GAP_PX = 1.0;

  // ------------------------------------------------------------------ small helpers

  function px(value) {
    var n = parseFloat(value);
    return isFinite(n) ? n : 0;
  }

  function box(rect) {
    return { x: rect.left, y: rect.top, w: rect.width, h: rect.height };
  }

  function boxOf(x, y, w, h) {
    return { x: x, y: y, w: w, h: h };
  }

  function unionBox(a, b) {
    if (!a) return b;
    if (!b) return a;
    var x = Math.min(a.x, b.x), y = Math.min(a.y, b.y);
    return boxOf(x, y, Math.max(a.x + a.w, b.x + b.w) - x, Math.max(a.y + a.h, b.y + b.h) - y);
  }

  function intersectBox(a, b) {
    if (!a) return b;
    if (!b) return a;
    var x = Math.max(a.x, b.x), y = Math.max(a.y, b.y);
    return boxOf(x, y, Math.max(0, Math.min(a.x + a.w, b.x + b.w) - x),
                 Math.max(0, Math.min(a.y + a.h, b.y + b.h) - y));
  }

  /** Does `outer` fully contain `inner` (within a pixel)? A clip that does not cut is not a clip. */
  function contains(outer, inner) {
    return outer.x <= inner.x + 1 && outer.y <= inner.y + 1 &&
           outer.x + outer.w >= inner.x + inner.w - 1 &&
           outer.y + outer.h >= inner.y + inner.h - 1;
  }

  /** Split on a separator that is not inside brackets — `rgba(0,0,0,.2) 0 4px, …` needs this. */
  function splitTop(text, separator) {
    var parts = [], depth = 0, current = "";
    for (var i = 0; i < text.length; i++) {
      var c = text[i];
      if (c === "(") depth++;
      else if (c === ")") depth--;
      if (c === separator && depth === 0) { parts.push(current.trim()); current = ""; continue; }
      current += c;
    }
    if (current.trim()) parts.push(current.trim());
    return parts;
  }

  function hex2(n) {
    var s = Math.max(0, Math.min(255, Math.round(n))).toString(16).toUpperCase();
    return s.length === 1 ? "0" + s : s;
  }

  /**
   * Any CSS `<color>` → `{color: "RRGGBB", alpha: 0..1}`; null when absent or unreadable.
   *
   * One normaliser for both browser scripts (`color.js`, `window.__engineColor`): `color-mix()`,
   * `oklch()`, `lab()`, `color(display-p3 …)`, named colours and 4-digit hex all resolve, and a
   * string the browser cannot read is recorded once and reported after the walk. `el` is the
   * element the value was read from, so a `currentcolor` still in the string resolves against it.
   */
  function parseColor(value, el) {
    if (value === undefined || value === null || value === "") return null;
    return window.__engineColor.normalise(value, el);
  }

  /** A `<length>` token as the browser serialises one (a bare `0` is a length too). */
  var LENGTH_TOKEN = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?(?:px|em|rem)?$/i;

  /**
   * `<color>? <dx> <dy> [<blur>] [<spread>] [inset]` → its parts, in px. Null when unparseable.
   *
   * Tokenised at parenthesis depth 0, so `oklch(0.2 0.1 240 / 0.3) 0px 4px 12px 0px` — Chromium
   * serialises colour first — is one colour and four lengths. With no colour the shadow is drawn in
   * `currentcolor`; with a colour nobody can read it is not drawn at all (and the colour is reported).
   */
  function parseShadow(text, el) {
    var lengths = [], colourText = null, inset = false;
    window.__engineColor.tokens(text).forEach(function (token) {
      if (/^inset$/i.test(token)) inset = true;
      else if (LENGTH_TOKEN.test(token)) lengths.push(px(token));
      else if (colourText === null) colourText = token;
    });
    if (lengths.length < 2) return null;
    var colour = colourText === null
      ? (parseColor(el ? getComputedStyle(el).color : "rgb(0, 0, 0)", el) || { color: "000000", alpha: 1 })
      : parseColor(colourText, el);
    if (!colour) return null;
    return {
      color: colour.color, alpha: colour.alpha,
      dx: lengths[0], dy: lengths[1],
      blur: lengths[2] || 0, spread: lengths[3] || 0,
      inset: inset,
    };
  }

  /**
   * A stable CSS-ish path, used for traceability and for shape names. A rebuilt pseudo-element
   * (`[data-engine-pseudo]`) is `<host path>::before|::after`, and is not counted as a sibling, so
   * every other path is the one the author's own document has.
   */
  function cssPath(el) {
    if (el.nodeType === 1 && el.hasAttribute("data-engine-pseudo") && el.parentElement) {
      return cssPath(el.parentElement) + "::" + el.getAttribute("data-engine-pseudo");
    }
    var parts = [];
    for (var node = el; node && node.nodeType === 1 && node !== document.documentElement;
         node = node.parentElement) {
      var piece = node.tagName.toLowerCase();
      if (node.id) { piece += "#" + node.id; parts.unshift(piece); break; }
      if (node.classList.length) piece += "." + node.classList[0];
      var index = 1, sibling = node;
      while ((sibling = sibling.previousElementSibling)) {
        if (!sibling.hasAttribute("data-engine-pseudo")) index++;
      }
      parts.unshift(piece + ":nth-child(" + index + ")");
    }
    return parts.join(" > ");
  }

  // ------------------------------------------------------------------ rotation frames
  //
  // A rotated element is measured with its own transform switched **off**. Transforms do not affect
  // layout, so with `transform: none` every rect in the subtree — including `Range` rects, which
  // otherwise come back as axis-aligned hulls of rotated text and lose their real width — is the
  // true unrotated geometry. The paint position is recovered separately: the centre the browser
  // painted the element at, minus the centre it occupies unrotated, is a translation that applies
  // to the whole subtree. PowerPoint rotates a shape about its own centre, so
  // "unrotated box + painted centre + angle" reproduces the paint exactly, with no nested groups.

  var IDENTITY_FRAME = { angle: 0, dx: 0, dy: 0 };

  /** Measurement happens with transforms neutralised, so a local box is just the rect. */
  function localBox(frame, rect) {
    return box(rect);
  }

  /** Where an unrotated box ends up once the frame's paint offset is applied. */
  function paintedBox(frame, b) {
    if (!frame.dx && !frame.dy) return b;
    return boxOf(b.x + frame.dx, b.y + frame.dy, b.w, b.h);
  }

  /** Decompose a computed `transform` matrix into rotation, scale signs and skew-ness. */
  function decomposeTransform(value) {
    if (!value || value === "none") return null;
    var m = value.match(/^matrix\(([^)]+)\)$/);
    var nums;
    if (m) {
      nums = m[1].split(",").map(parseFloat);
    } else {
      var m3 = value.match(/^matrix3d\(([^)]+)\)$/);
      if (!m3) return { unsupported: true, angle: 0, scaleX: 1, scaleY: 1 };
      var all = m3[1].split(",").map(parseFloat);
      nums = [all[0], all[1], all[4], all[5], all[12], all[13]];
      if (all[2] || all[3] || all[6] || all[7] || all[8] || all[9] || all[11] || all[14]) {
        return { unsupported: true, angle: 0, scaleX: 1, scaleY: 1 };
      }
    }
    var a = nums[0], b = nums[1], c = nums[2], d = nums[3];
    var scaleX = Math.sqrt(a * a + b * b) || 1;
    var determinant = a * d - b * c;
    var scaleY = determinant / scaleX || 1;
    var shear = (a * c + b * d) / (scaleX * scaleX);
    var angle = (Math.atan2(b, a) * 180) / Math.PI;
    var flipX = scaleX < 0, flipY = scaleY < 0;
    // A mirror decomposes as "turn 180° and flip the other axis", which is the same picture with a
    // rotation nobody wrote. `scale(-1)` — the one transform the contract allows besides rotate —
    // should come out as a plain flip, so fold the half turn back into the flips.
    if (Math.abs(Math.abs(angle) - 180) < 1e-6 && (flipX !== flipY)) {
      angle = 0;
      flipX = !flipX;
      flipY = !flipY;
    }
    return {
      unsupported: Math.abs(shear) > 1e-6 ||
                   Math.abs(Math.abs(scaleX) - 1) > 1e-6 || Math.abs(Math.abs(scaleY) - 1) > 1e-6,
      angle: angle,
      scaleX: flipX ? -1 : 1,
      scaleY: flipY ? -1 : 1,
      translated: Math.abs(nums[4]) > 1e-6 || Math.abs(nums[5]) > 1e-6,
    };
  }

  // ------------------------------------------------------------------ element classification

  var SPECIAL_TAGS = { img: 1, svg: 1, table: 1, canvas: 1, video: 1, iframe: 1, object: 1, embed: 1 };

  function isInlineDisplay(display) {
    return display === "inline" || display === "inline-block" || display === "inline-flex" ||
           display === "inline-grid" || display === "contents" || display === "ruby";
  }

  /** An inline-level box with its own formatting context: laid out in the line as one unit. */
  function isAtomicInline(display) {
    return display === "inline-block" || display === "inline-flex" || display === "inline-grid";
  }

  function isOutOfFlow(cs) {
    return cs.position === "absolute" || cs.position === "fixed" || cs.float !== "none";
  }

  function isSpecial(el) {
    var tag = el.tagName.toLowerCase();
    if (SPECIAL_TAGS[tag]) return true;
    if (el.hasAttribute("data-chart")) return true;
    if (el.getAttribute("data-pptx") === "raster") return true;
    return false;
  }

  /** A block whose content is inline-only: one paragraph in the element-boundary rule. */
  function isTextBlock(el, cs) {
    if (isSpecial(el)) return false;
    if (!textBesidePseudos(el).trim()) return false;
    for (var i = 0; i < el.children.length; i++) {
      var child = el.children[i];
      var childStyle = getComputedStyle(child);
      if (childStyle.display === "none") continue;
      // The grouping rule (WP-E): an out-of-flow pseudo-element copy (a bullet dot, a counter
      // circle, a colour bar) sits beside the paragraph, not in it. It is walked after the text
      // (`walkChildren`), and the paragraph stays a paragraph of its run.
      if (isDeferredPseudo(child, childStyle)) continue;
      if (isSpecial(child)) return false;
      // R5: a `display: contents` child has no box; its own children are this block's content.
      if (childStyle.display === "contents") { if (!contentsInline(child)) return false; continue; }
      if (!isInlineDisplay(childStyle.display)) return false;
      if (isOutOfFlow(childStyle)) return false;
    }
    return true;
  }

  /** R5: is everything a `display: contents` element splices into its parent inline content? */
  function contentsInline(el) {
    for (var i = 0; i < el.children.length; i++) {
      var child = el.children[i];
      var childStyle = getComputedStyle(child);
      if (childStyle.display === "none") continue;
      if (isSpecial(child)) return false;
      if (childStyle.display === "contents") { if (!contentsInline(child)) return false; continue; }
      if (!isInlineDisplay(childStyle.display) || isOutOfFlow(childStyle)) return false;
    }
    return true;
  }

  // ------------------------------------------------------------------ raster detection

  /** Why this element cannot be drawn natively — null when it can. Authoring contract §Boxes. */
  function rasterReason(el, cs) {
    if (el.getAttribute("data-pptx") === "raster") return 'data-pptx="raster"';
    if (cs.filter && cs.filter !== "none") return "CSS filter: " + cs.filter;
    if (cs.backdropFilter && cs.backdropFilter !== "none") return "backdrop-filter";
    if (cs.mixBlendMode && cs.mixBlendMode !== "normal") return "mix-blend-mode: " + cs.mixBlendMode;
    if (cs.maskImage && cs.maskImage !== "none") return "mask-image";
    if (cs.webkitTextStrokeWidth && px(cs.webkitTextStrokeWidth) > 0) return "-webkit-text-stroke";
    var background = cs.backgroundImage;
    // A picture seen through the glyphs has no native form; a gradient seen through them does (the
    // run's own `a:gradFill`, see `runStyle`), and neither is ever a slab behind the text.
    if (clipsToText(cs) && /url\(/i.test(background || "")) return "background-clip:text over an image";
    if (background && background !== "none") {
      var layers = splitTop(background, ",");
      if (layers.length > 1) return "multiple background layers (" + layers.length + ")";
      if (/conic-gradient/i.test(background)) return "conic-gradient";
      if (/-webkit-|image-set|cross-fade|element\(/i.test(background)) return "unsupported background: " + background;
    }
    if (cs.boxShadow && cs.boxShadow !== "none") {
      var shadows = splitTop(cs.boxShadow, ",");
      for (var i = 0; i < shadows.length; i++) {
        if (/\binset\b/.test(shadows[i])) return "inset box-shadow";
      }
    }
    if (cs.textShadow && cs.textShadow !== "none") {
      var text = parseShadow(splitTop(cs.textShadow, ",")[0], el);
      if (text && text.blur > 0) return "text-shadow with blur";
    }
    var transform = decomposeTransform(cs.transform);
    if (transform && transform.unsupported) return "unsupported CSS transform: " + cs.transform;
    return null;
  }

  // ------------------------------------------------------------------ geometry of a box

  function borderBox(el, frame) {
    return localBox(frame, el.getBoundingClientRect());
  }

  function insetBox(b, top, right, bottom, left) {
    return boxOf(b.x + left, b.y + top, Math.max(0, b.w - left - right), Math.max(0, b.h - top - bottom));
  }

  function paddingBox(b, cs) {
    return insetBox(b, px(cs.borderTopWidth), px(cs.borderRightWidth),
                    px(cs.borderBottomWidth), px(cs.borderLeftWidth));
  }

  function contentBox(b, cs) {
    return insetBox(b,
      px(cs.borderTopWidth) + px(cs.paddingTop), px(cs.borderRightWidth) + px(cs.paddingRight),
      px(cs.borderBottomWidth) + px(cs.paddingBottom), px(cs.borderLeftWidth) + px(cs.paddingLeft));
  }

  // ------------------------------------------------------------------ fills, borders, shadows

  /** `in <space> [<hue-method> hue]` inside a gradient's head (CSS Images 4 / Color 4 §12). */
  var INTERPOLATION = /(?:^|\s)in\s+([a-z0-9-]+)(?:\s+(?:shorter|longer|increasing|decreasing)\s+hue)?(?=\s|$)/i;
  /** A colour-stop position as the browser serialises one; `calc()` positions are read as unpositioned. */
  var STOP_POSITION = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?(?:%|px|em|rem)?$/i;

  /** One comma part of a gradient split into its colour text and its position tokens. */
  function stopParts(part) {
    var colour = [], positions = [];
    window.__engineColor.tokens(part).forEach(function (token) {
      if (STOP_POSITION.test(token)) positions.push(token);
      else if (!/^calc\(/i.test(token)) colour.push(token);
    });
    return { colour: colour.join(" "), positions: positions };
  }

  /**
   * CSS gradient string → the IR's gradient fill, with `angle` per 02-IR-SCHEMA conventions.
   *
   * The head is `[direction] [in <space> [<hue-method> hue]]`: the interpolation clause is stripped
   * first (a computed value keeps it — `90deg in oklab` — and the direction regexes then fail), and
   * the head is dropped whenever it is not a colour stop. The space the browser interpolated in rides
   * along as `space` for `takeSpace`: the named one, else `oklab` when any stop is not a legacy sRGB
   * colour (Chromium serialises those as `rgb(`/`#`), else `srgb` (CSS Color 4 §12.1).
   */
  function parseGradient(value, b, el, state) {
    var linear = value.match(/^(repeating-)?linear-gradient\((.*)\)$/i);
    var radial = value.match(/^(repeating-)?radial-gradient\((.*)\)$/i);
    if (!linear && !radial) return null;
    var kind = linear ? "linear" : "radial";
    var parts = splitTop((linear || radial)[2], ",");
    var angle = kind === "linear" ? 180 : 0;   // CSS default is `to bottom`
    var space = null;
    var first = parts.length ? stopParts(parts[0]) : null;
    if (first && !(first.colour && window.__engineColor.normalise(first.colour, el, true))) {
      var head = parts[0];
      var named = head.match(INTERPOLATION);
      if (named) {
        space = named[1].toLowerCase();
        head = head.replace(INTERPOLATION, " ").trim();
      }
      var deg = head.match(/^(-?[\d.]+)deg$/i);
      var turn = head.match(/^(-?[\d.]+)turn$/i);
      var rad = head.match(/^(-?[\d.]+)rad$/i);
      var grad = head.match(/^(-?[\d.]+)grad$/i);
      if (deg) angle = parseFloat(deg[1]);
      else if (turn) angle = parseFloat(turn[1]) * 360;
      else if (rad) angle = (parseFloat(rad[1]) * 180) / Math.PI;
      else if (grad) angle = parseFloat(grad[1]) * 0.9;
      else if (/^to\s+/i.test(head)) {
        var sides = head.toLowerCase().replace(/^to\s+/, "").split(/\s+/).sort().join(" ");
        var table = { top: 0, right: 90, bottom: 180, left: 270,
                      "right top": 45, "bottom right": 135, "bottom left": 225, "left top": 315 };
        angle = table[sides] !== undefined ? table[sides] : 180;
      }
      // Whatever else the head says (radial shape and position) is not a stop either.
      parts = parts.slice(1);
    }
    // The gradient line's length decides what a px stop position means as a fraction.
    var rads = (angle * Math.PI) / 180;
    var lineLength = Math.abs(b.w * Math.sin(rads)) + Math.abs(b.h * Math.cos(rads));
    if (kind === "radial") lineLength = Math.max(b.w, b.h) / 2;

    var stops = [], legacy = true;
    parts.forEach(function (part) {
      var split = stopParts(part);
      // A lone position is a colour hint (it moves the midpoint); PowerPoint has no such thing.
      if (!split.colour) return;
      var colour = parseColor(split.colour, el);
      if (!colour) return;
      if (!/^(rgba?\(|#)/i.test(split.colour)) legacy = false;
      if (!split.positions.length) { stops.push({ pos: null, color: colour.color, alpha: colour.alpha }); return; }
      split.positions.forEach(function (position) {
        var pos = /%$/.test(position) ? parseFloat(position) / 100
                                      : (lineLength ? px(position) / lineLength : 0);
        stops.push({ pos: pos, color: colour.color, alpha: colour.alpha });
      });
    });
    if (stops.length < 2) return null;
    // Unpositioned stops spread evenly between their positioned neighbours (CSS Images §3.4.2).
    if (stops[0].pos === null) stops[0].pos = 0;
    if (stops[stops.length - 1].pos === null) stops[stops.length - 1].pos = 1;
    for (var i = 1; i < stops.length - 1; i++) {
      if (stops[i].pos !== null) continue;
      var next = i;
      while (next < stops.length && stops[next].pos === null) next++;
      var span = (stops[next].pos - stops[i - 1].pos) / (next - i + 1);
      for (var j = i; j < next; j++) stops[j].pos = stops[i - 1].pos + span * (j - i + 1);
    }
    return { type: "gradient", kind: kind, angle: ((angle % 360) + 360) % 360, stops: stops,
             space: space || (legacy ? "srgb" : "oklab") };
  }

  /** Is the (first) background layer clipped to the text? `-webkit-background-clip: text` reads the same. */
  function clipsToText(cs) {
    return splitTop(String(cs.backgroundClip || ""), ",")[0] === "text";
  }

  /**
   * Take the browser's interpolation space off a parsed gradient (it is not an IR field) and say
   * once per element when PowerPoint's sRGB interpolation will not match it.
   */
  function takeSpace(gradient, el, state) {
    var space = gradient.space;
    delete gradient.space;
    if (space && space !== "srgb" && el && state && !el.__engineSpaceNoted) {
      el.__engineSpaceNoted = true;
      diagnose(state, "info", cssPath(el),
               "gradient interpolated in " + space + " by the browser and in sRGB by PowerPoint, approximated");
    }
    return gradient;
  }

  /**
   * The box's own background as an IR fill, or null. `el` and `state` are the element it was read
   * from and the walk's state: `el` resolves `currentcolor`, `state` receives the one interpolation
   * note (callers that only ask "is there a fill?" pass no state and record nothing).
   */
  function fillOf(cs, b, el, state) {
    // A background clipped to the text paints inside the glyphs only, never as a box behind them:
    // gradient text is the run's own fill (`runStyle`), and the slab is not drawn.
    if (clipsToText(cs)) return null;
    var image = cs.backgroundImage;
    if (image && image !== "none") {
      var gradient = parseGradient(splitTop(image, ",")[0], b, el, state);
      if (gradient) return takeSpace(gradient, el, state);
    }
    var colour = parseColor(cs.backgroundColor, el);
    if (colour && colour.alpha > 0) return { type: "solid", color: colour.color, alpha: colour.alpha };
    return null;
  }

  /** `dashed`/`dotted` → on/off pairs in px, scaled with the line width as browsers do. */
  function dashFor(style, width) {
    if (style === "dashed") return [Math.max(1, width * 2), Math.max(1, width)];
    if (style === "dotted") return [Math.max(1, width), Math.max(1, width)];
    return "solid";
  }

  function sideBorders(cs) {
    var sides = {};
    ["Top", "Right", "Bottom", "Left"].forEach(function (side) {
      var width = px(cs["border" + side + "Width"]);
      var style = cs["border" + side + "Style"];
      var colour = parseColor(cs["border" + side + "Color"]);
      sides[side.toLowerCase()] = (width > 0 && style !== "none" && style !== "hidden" &&
                                   colour && colour.alpha > 0)
        ? { width: width, style: style, color: colour.color, alpha: colour.alpha }
        : null;
    });
    return sides;
  }

  function uniformBorder(sides) {
    var first = sides.top;
    if (!first) return null;
    var keys = ["right", "bottom", "left"];
    for (var i = 0; i < keys.length; i++) {
      var side = sides[keys[i]];
      if (!side || side.width !== first.width || side.color !== first.color ||
          side.style !== first.style || side.alpha !== first.alpha) return null;
    }
    return first;
  }

  function radiusOf(cs, b) {
    function corner(name) {
      var parts = String(cs[name] || "0px").split(/\s+/);
      var value = /%$/.test(parts[0]) ? (parseFloat(parts[0]) / 100) * b.w : px(parts[0]);
      return Math.max(0, value);
    }
    return { tl: corner("borderTopLeftRadius"), tr: corner("borderTopRightRadius"),
             br: corner("borderBottomRightRadius"), bl: corner("borderBottomLeftRadius") };
  }

  /**
   * The `box-shadow` layers that paint something, parsed, first (topmost) layer first. A layer that
   * paints nothing is left out: a transparent colour, or no offset, no blur and no positive spread,
   * which puts it exactly under the box (Tailwind's `0 0 #0000` ring layers), so keeping it would
   * draw nothing and dropping it loses nothing. A layer `parseShadow` cannot read is left out too.
   */
  function shadowLayers(cs) {
    if (!cs.boxShadow || cs.boxShadow === "none") return [];
    return splitTop(cs.boxShadow, ",").map(function (text) { return parseShadow(text); })
      .filter(function (layer) {
        return layer && layer.alpha > 0 && !!(layer.dx || layer.dy || layer.blur || layer.spread > 0);
      });
  }

  /**
   * The one shadow the IR carries: the first outer layer that paints. PowerPoint draws one outer
   * shadow per shape, so the other layers are dropped — `reportDroppedShadows` says so. The IR's
   * shadow has no spread, so a layer an offset or a blur makes visible is preferred to a spread-only
   * ring (`0 0 0 3px`), which would come out exactly under the shape.
   */
  function shadowOf(cs) {
    var outer = shadowLayers(cs).filter(function (layer) { return !layer.inset; });
    var shadow = outer.filter(function (layer) { return layer.dx || layer.dy || layer.blur; })[0] || outer[0];
    if (!shadow) return null;
    return { color: shadow.color, alpha: shadow.alpha, dx: shadow.dx, dy: shadow.dy, blur: shadow.blur };
  }

  /**
   * One warn per decorated element for the painting layers `shadowOf` did not keep: every outer
   * layer after the first and every inset layer. A single shadow says nothing. `index` is the record
   * that carries the kept shadow, or null when there is none.
   */
  function reportDroppedShadows(el, cs, state, index) {
    var layers = shadowLayers(cs);
    var inset = layers.filter(function (layer) { return layer.inset; }).length;
    var outer = layers.length - inset;
    var extra = outer > 1 ? outer - 1 : 0;
    if (!extra && !inset) return;
    var parts = [];
    if (extra) parts.push(extra + " extra outer shadow" + (extra > 1 ? "s" : ""));
    if (inset) parts.push(inset + " inset shadow" + (inset > 1 ? "s" : ""));
    var what = parts.join(" and ");
    diagnose(state, "warn", cssPath(el),
             "box-shadow: " + what + " dropped; PowerPoint draws one outer shadow per shape", index);
  }

  /** `clip-path: polygon(…)` → an absolute path in canvas px. Percentages resolve against the box. */
  function clipPathGeometry(cs, b) {
    var value = cs.clipPath;
    if (!value || value === "none") return null;
    var polygon = value.match(/^polygon\(([^)]*)\)$/i);
    if (!polygon) return null;
    var points = splitTop(polygon[1], ",").map(function (pair) {
      var parts = pair.trim().split(/\s+/);
      var x = /%$/.test(parts[0]) ? b.x + (parseFloat(parts[0]) / 100) * b.w : b.x + px(parts[0]);
      var y = /%$/.test(parts[1]) ? b.y + (parseFloat(parts[1]) / 100) * b.h : b.y + px(parts[1]);
      return [x, y];
    });
    if (points.length < 3) return null;
    var path = [["M", points[0][0], points[0][1]]];
    for (var i = 1; i < points.length; i++) path.push(["L", points[i][0], points[i][1]]);
    path.push(["Z"]);
    return { type: "custom", path: path, fillRule: "nonzero" };
  }

  // ------------------------------------------------------------------ line-height for `normal`

  var normalCache = {};
  var probeHost = null;

  /** `line-height: normal` is not a number; measure it once per font signature and cache. */
  function normalLineHeight(cs) {
    var key = cs.fontFamily + "|" + cs.fontSize + "|" + cs.fontWeight + "|" + cs.fontStyle;
    if (normalCache[key] !== undefined) return normalCache[key];
    if (!probeHost) {
      probeHost = document.createElement("div");
      probeHost.setAttribute("data-engine-probe", "");
      probeHost.style.cssText = "position:absolute;left:-10000px;top:-10000px;visibility:hidden;" +
                                "white-space:pre;line-height:normal;margin:0;padding:0";
      document.body.appendChild(probeHost);
    }
    probeHost.style.font = cs.fontStyle + " " + cs.fontWeight + " " + cs.fontSize + " " + cs.fontFamily;
    probeHost.textContent = "A\nA";
    var rects = probeHost.getClientRects();
    var height = probeHost.getBoundingClientRect().height / 2;
    normalCache[key] = rects.length ? height : px(cs.fontSize) * 1.2;
    return normalCache[key];
  }

  function lineHeightPx(cs) {
    if (!cs.lineHeight || cs.lineHeight === "normal") return normalLineHeight(cs);
    return px(cs.lineHeight);
  }

  // ------------------------------------------------------------------ text

  function resolveAlign(cs) {
    var align = String(cs.textAlign || "left").replace(/^-(webkit|moz|internal)-/, "");
    var rtl = cs.direction === "rtl";
    if (align === "start") return rtl ? "right" : "left";
    if (align === "end") return rtl ? "left" : "right";
    if (align === "justify" || align === "center" || align === "left" || align === "right") return align;
    return "left";
  }

  function applyCase(text, transform) {
    if (transform === "uppercase") return text.toUpperCase();
    if (transform === "lowercase") return text.toLowerCase();
    if (transform === "capitalize") {
      return text.replace(/(^|\s)(\S)/g, function (_, space, letter) { return space + letter.toUpperCase(); });
    }
    return text;
  }

  /** Does any ancestor up to `stop` decorate its text? Chrome does not propagate this to descendants. */
  function inheritedDecoration(el, stop) {
    var underline = false, strike = false;
    for (var node = el; node && node !== stop.parentElement; node = node.parentElement) {
      var line = getComputedStyle(node).textDecorationLine || "";
      if (line.indexOf("underline") >= 0) underline = true;
      if (line.indexOf("line-through") >= 0) strike = true;
      if (node === stop) break;
    }
    return { underline: underline, strike: strike };
  }

  function runStyle(textNode, paragraphEl) {
    var el = textNode.parentElement;
    var cs = getComputedStyle(el);
    // The glyphs are painted in `-webkit-text-fill-color` (it defaults to `color`); when that is
    // transparent, what shows through them is a text-clipped background (gradient text).
    var colour = parseColor(cs.webkitTextFillColor, el) || parseColor(cs.color, el) || { color: "000000", alpha: 1 };
    if (el.hasAttribute("data-engine-pseudo")) el.__engineTextDrawn = true;   // WP-E: the sweep's evidence
    var glyphs = colour.alpha === 0 ? glyphPaint(el) : null;
    if (glyphs) colour = glyphs.colour;
    var decoration = inheritedDecoration(el, paragraphEl);
    // `vertical-align` places a box in its *parent's* line. On the paragraph element itself (a
    // detached chip with `vertical-align:1px`) that offset is already in the measured line boxes;
    // turning it into a run baseline shift as well would move the text twice.
    var verticalAlign = el === paragraphEl ? "baseline" : cs.verticalAlign;
    var baseline = "normal", shift = 0;
    if (verticalAlign === "super" || verticalAlign === "sub") baseline = verticalAlign;
    else if (/^-?[\d.]+px$/.test(verticalAlign)) shift = px(verticalAlign);
    var anchor = el.closest ? el.closest("a") : null;
    return {
      font: String(cs.fontFamily).split(",")[0].replace(/["']/g, "").trim(),
      sizePx: px(cs.fontSize),
      weight: parseInt(cs.fontWeight, 10) || 400,
      italic: cs.fontStyle !== "normal",
      color: colour.color,
      alpha: colour.alpha,
      underline: decoration.underline,
      strike: decoration.strike,
      letterSpacingPx: cs.letterSpacing === "normal" ? 0 : px(cs.letterSpacing),
      baseline: baseline,
      baselineShiftPx: shift,
      caps: cs.textTransform === "uppercase",
      href: anchor ? anchor.href : null,
      _transform: cs.textTransform,
      _whiteSpace: cs.whiteSpace,
      _fill: glyphs ? glyphs.fill : null,
    };
  }

  /**
   * What shows through transparent glyphs: the background of the nearest element, from `el` up to
   * `<body>`, that clips its background to its text (and so to its descendants' text) and has one.
   * `{colour, fill}` — a solid background is the run's colour; a gradient is the run's own fill, with
   * the gradient's colour at its middle as the run colour every consumer that ignores fills still
   * reads. Null when nothing shows through. Cached per element.
   */
  function glyphPaint(el) {
    if (el.__engineGlyphPaint !== undefined) return el.__engineGlyphPaint;
    var found = null;
    for (var node = el; node && node.nodeType === 1 && !found; node = node.parentElement) {
      var cs = getComputedStyle(node);
      if (clipsToText(cs)) {
        var image = cs.backgroundImage;
        var gradient = image && image !== "none"
          ? parseGradient(splitTop(image, ",")[0], box(node.getBoundingClientRect()), node) : null;
        if (gradient) {
          takeSpace(gradient, node, null);
          found = { colour: colourAt(gradient.stops, 0.5), fill: gradient };
        } else {
          var solid = parseColor(cs.backgroundColor, node);
          if (solid && solid.alpha > 0) found = { colour: solid, fill: null };
        }
      }
      if (node === document.body) break;
    }
    el.__engineGlyphPaint = found;
    return found;
  }

  /** A gradient's colour at `t` (0..1): linear in sRGB between the neighbouring stops, alpha likewise. */
  function colourAt(stops, t) {
    var before = stops[0], after = stops[stops.length - 1];
    for (var i = 0; i < stops.length; i++) {
      if (stops[i].pos <= t) before = stops[i];
      if (stops[i].pos >= t) { after = stops[i]; break; }
    }
    var span = after.pos - before.pos;
    var f = span > 0 ? (t - before.pos) / span : 0;
    function mix(a, b) { return a + (b - a) * f; }
    var colour = "";
    for (var c = 0; c < 6; c += 2) {
      colour += hex2(mix(parseInt(before.color.slice(c, c + 2), 16), parseInt(after.color.slice(c, c + 2), 16)));
    }
    return { color: colour, alpha: mix(before.alpha, after.alpha) };
  }

  /**
   * Gradient text: every run's own fill leaves the run — a run-level key would be a change to the
   * frozen IR — for the element's escape hatch, `extras["x-wpe-run-fills"]`, keyed by the run's
   * position `"<paragraph>/<line>/<run>"` (02-IR-SCHEMA addendum). Null when no run has one.
   */
  function liftRunFills(paragraphs) {
    var fills = null;
    paragraphs.forEach(function (paragraph, p) {
      paragraph.lines.forEach(function (line, l) {
        line.runs.forEach(function (run, r) {
          if (run._fill) {
            fills = fills || {};
            fills[p + "/" + l + "/" + r] = JSON.parse(JSON.stringify(run._fill));
          }
          delete run._fill;
        });
      });
    });
    return fills;
  }

  function styleKey(style) {
    // A run's own fill is part of its identity: two gradient spans with one mid-colour stay two runs.
    return [style.font, style.sizePx, style.weight, style.italic, style.color, style.alpha,
            style.underline, style.strike, style.letterSpacingPx, style.baseline,
            style.baselineShiftPx, style.caps, style.href, JSON.stringify(style._fill || null)].join("|");
  }

  /** Every text node under `root`, in document order, skipping hidden and nested block content. */
  function textNodesOf(root) {
    var nodes = [];
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        if (!node.data) return NodeFilter.FILTER_REJECT;
        var parent = node.parentElement;
        if (!parent) return NodeFilter.FILTER_REJECT;
        var cs = getComputedStyle(parent);
        if (cs.display === "none" || cs.visibility === "hidden") return NodeFilter.FILTER_REJECT;
        // The counter inside an absolute `li::before` belongs to the dot, not to the paragraph: the
        // text of an out-of-flow pseudo-element copy below `root` is walked with that copy.
        var copy = parent.closest("[data-engine-pseudo]");
        if (copy && copy !== root && root.contains(copy) && isOutOfFlow(getComputedStyle(copy))) {
          return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    var node;
    while ((node = walker.nextNode())) nodes.push(node);
    return nodes;
  }

  /**
   * A fragment's text as the browser drew it. Only CSS's *document white space* collapses — space,
   * tab, line feed, carriage return (CSS Text 3 §4.1.1) — never JavaScript's `\s`, which also
   * matches U+00A0 and every other space separator. Measured in Edge (fidelity r1b, 2026-09-25,
   * Calibri 40 px, advances in spaces for 'a' + X + 'b' / 'a ' + X + ' b' / 'a' + XXX + 'b'):
   * space, tab, LF and CR draw 1 / 1 / 1 in `normal`, `nowrap` and `pre-line`; U+00A0, U+202F,
   * U+2028 and U+2029 draw 1 / 3.00 / 3.00, U+2002 2.2 / 4.2 / 6.6, U+2003 and U+3000
   * 4.4 / 6.4 / 13.3, U+2009 0.9 / 2.9 / 2.7, U+FEFF 0 / 2 / 0 — never collapsed. With `\s`,
   * `A &nbsp; B` (three advances) exported as one breakable space and every word after it moved
   * left (the media family's flags, 10–21 px in PowerPoint). A non-breaking space is written into
   * the run as U+00A0. PowerPoint and PptxRender draw U+00A0, U+202F, U+2002, U+2003, U+2009 and
   * U+FEFF at the browser's advances (within 0.3 px, r1b's one COM render); U+2028 5.4 px wider
   * than the space the browser draws, so the line and paragraph separators go in as one space
   * each, uncollapsed. Form feed and vertical tab (a 2–4-space box in Edge) still collapse as
   * before: an XML part cannot hold them, and a box is not what an author typed them for.
   */
  function normaliseText(raw, whiteSpace) {
    if (whiteSpace === "pre" || whiteSpace === "pre-wrap" || whiteSpace === "break-spaces") {
      return raw.replace(/[\r\n]+/g, "");
    }
    return raw.replace(/[ \t\n\r\f\v]+/g, " ").replace(/[\u2028\u2029]/g, " ");
  }

  /**
   * `runs` without the white space at the line's two ends: every character `\s` matches — the set
   * `glyphExtent` skips when it moves the line's box to its first and last glyph — so the text and
   * the box keep agreeing now that `normaliseText` lets U+00A0 and the other space separators
   * through (r1b). Before, they arrived here as a plain space and this trim removed them; a line's
   * ends are unchanged, only the spaces inside a line are kept as drawn. A run the trim empties is
   * dropped, and the trim goes on into the next one, unless it is the line's only run.
   */
  function trimLineEnds(runs) {
    for (var i = 0; i < runs.length; i++) {
      runs[i].text = runs[i].text.replace(/^\s+/, "");
      if (runs[i].text) break;
    }
    for (var j = runs.length - 1; j >= 0; j--) {
      runs[j].text = runs[j].text.replace(/\s+$/, "");
      if (runs[j].text) break;
    }
    return runs.filter(function (run) { return run.text.length > 0 || runs.length === 1; });
  }

  /**
   * Break one paragraph's text nodes into the browser's lines, and each line into runs.
   *
   * Per-character `Range` rects are the only way to learn *which characters* the browser put on
   * each line; a range over the whole node only says how many lines there are. Whitespace that the
   * browser collapsed (a space at a wrap point) measures zero-wide and is skipped, so the slice
   * between the first and last measured character of a fragment is exactly the rendered text.
   *
   * Returns `{lines, hidden}` (WP-F). With a `clip` (the overflow clip the text sits in; null for a
   * rotated frame and for table cells), each character the reader cannot see is left out
   * (`clipCharacter`) and counted in `hidden`, and the browser's own ellipsis is kept
   * (`keepEllipsis`). Inside a CSS multi-column box each character also carries its column, so a
   * fragment never spans a column gap and a line never mixes columns (`columnBandsOf`).
   */
  function measureParagraph(textNodes, paragraphEl, frame, clip) {
    var fragments = [];
    var range = document.createRange();
    var bands = columnBandsOf(paragraphEl);
    var multicol = bands && !bands.spanner ? bands : null;
    var hidden = hiddenTally(clip, paragraphEl);
    textNodes.forEach(function (node, order) {
      var style = runStyle(node, paragraphEl);
      var data = node.data;
      var open = null;
      for (var i = 0; i < data.length; i++) {
        range.setStart(node, i);
        range.setEnd(node, i + 1);
        var rects = range.getClientRects();
        var rect = null;
        for (var r = 0; r < rects.length; r++) {
          if (rects[r].width > 0 || (rects[r].height > 0 && !/\s/.test(data[i]))) { rect = rects[r]; break; }
        }
        if (!rect || (/\s/.test(data[i]) && rect.width === 0)) continue;
        var local = localBox(frame, rect);
        var column = multicol ? columnOf(multicol, local) : 0;
        if (clip && clipCharacter(hidden, paintedBox(frame, local), clip, order, i, column)) { open = null; continue; }
        if (open && Math.abs(local.y - open.top) < LINE_EPS && open.column === column) {
          open.to = i;
          open.left = Math.min(open.left, local.x);
          open.right = Math.max(open.right, local.x + local.w);
          open.bottom = Math.max(open.bottom, local.y + local.h);
          continue;
        }
        open = { node: node, order: order, style: style, from: i, to: i, top: local.y,
                 bottom: local.y + local.h, left: local.x, right: local.x + local.w,
                 seq: fragments.length, column: column };
        fragments.push(open);
      }
    });
    settleHidden(hidden, textNodes);
    if (!fragments.length) return { lines: [], hidden: hidden };

    // Group fragments into the browser's lines. Equal tops are not the test: a 22 px number and an
    // 8 px unit sitting on one baseline have very different tops, while two wrapped lines of one
    // size have the same height and barely touch. What identifies a line is that every fragment on
    // it shares a baseline, which shows up as a *large vertical overlap* — a short fragment is
    // contained in the tall one's band, whereas the next line down clears it almost entirely.
    // Across CSS columns, lines at the same height in different columns are different lines: a
    // fragment only joins a line of its own column, and lines read column by column.
    var lines = [];
    fragments.forEach(function (fragment) {
      var best = null, bestShare = 0;
      for (var i = 0; i < lines.length; i++) {
        var line = lines[i];
        if (line.column !== fragment.column) continue;
        var overlap = Math.min(line.bottom, fragment.bottom) - Math.max(line.top, fragment.top);
        var shortest = Math.max(LINE_EPS, Math.min(line.bottom - line.top, fragment.bottom - fragment.top));
        var share = overlap / shortest;
        if (share > bestShare) { bestShare = share; best = line; }
      }
      if (!best || bestShare <= 0.5) {
        best = { top: fragment.top, bottom: fragment.bottom, fragments: [], column: fragment.column };
        lines.push(best);
      }
      best.top = Math.min(best.top, fragment.top);
      best.bottom = Math.max(best.bottom, fragment.bottom);
      best.fragments.push(fragment);
    });
    lines.sort(function (a, b) { return a.column - b.column || a.top - b.top; });
    if (clip) keepEllipsis(lines, paragraphEl, frame, hidden);

    var height = lineHeightPx(getComputedStyle(paragraphEl));
    var built = lines.map(function (line) {
      line.fragments.sort(function (a, b) { return a.seq - b.seq; });
      var left = Infinity, right = -Infinity, runs = [], seams = [], before = null;
      line.fragments.forEach(function (fragment) {
        left = Math.min(left, fragment.left);
        right = Math.max(right, fragment.right);
        var text = applyCase(
          normaliseText(fragment.node.data.slice(fragment.from, fragment.to + 1), fragment.style._whiteSpace),
          fragment.style._transform);
        if (!text) return;
        // r1a: what an inline box between the paragraph and this text does to it — its `opacity`.
        var style = seenStyle(fragment.style, fragment.node.parentElement, paragraphEl);
        var from = fragment.left;
        // R6: the browser separated these two texts by layout — a margin, padding, an empty box —
        // which a run cannot carry. The gap is carried by a space instead (`carryGap`): the one
        // beside it, or one inserted and sized to it. Only a gap nothing can carry is a glued seam.
        var gap = before === null ? 0 : fragment.left - before.right;
        var said = text;
        if (gap >= SEAM_GAP_PX) {
          var carried = carryGap(runs, before, text, style, fragment.node.parentElement, fragment.left, gap);
          if (!carried) seams.push({ left: before.text, right: text, gapPx: gap });
          else if (carried.rest !== undefined) { text = carried.rest; from = carried.from; }
        }
        before = { right: fragment.right, text: said };
        if (!text) return;
        var previous = runs[runs.length - 1];
        if (previous && gap < SEAM_GAP_PX && previous._key === styleKey(style)) {
          previous.text += text;
          previous._left = Math.min(previous._left, from);
          previous._right = Math.max(previous._right, fragment.right);
          return;
        }
        var run = {};
        Object.keys(style).forEach(function (key) {
          if (key[0] !== "_") run[key] = style[key];
        });
        // Gradient text: the run's own fill, lifted into the element's extras by `flushText`.
        if (style._fill) run._fill = style._fill;
        run.text = text;
        run._key = styleKey(style);
        run._left = from;
        run._right = fragment.right;
        run._el = fragment.node.parentElement;
        runs.push(run);
      });
      runs = trimLineEnds(runs);
      // The trim above drops the spaces at the line's ends from its text; drop them from its
      // geometry too. A space the browser did not collapse — after a chip, a badge, an inline image
      // — belongs to the gap between two things, not to the text beside it: kept, it put a hugged
      // segment's first line one space (3–10 px) toward the chip, where E1 would then write it.
      var ink = glyphExtent(line.fragments, frame);
      if (ink && runs.length) {
        left = ink.left;
        right = ink.right;
        runs[0]._left = Math.max(runs[0]._left, left);
        runs[runs.length - 1]._right = Math.min(runs[runs.length - 1]._right, right);
      }
      // `x-run-boxes`: each run's [left, right] from its own fragments, in run order.
      var runBoxes = runs.map(function (run) { return [run._left, run._right]; });
      runs.forEach(function (run) { delete run._key; delete run._left; delete run._right; delete run._el; });
      // The measured rects are the font's content area, centred in the line box by half the
      // leading. The leading is *signed*: `line-height: 1` on a font whose ascent+descent exceeds
      // one em gives a content area taller than its line box, and clamping it to zero would put
      // every such line half a pixel too high.
      var textHeight = line.bottom - line.top;
      var leading = (height - textHeight) / 2;
      return {
        box: boxOf(left, line.top - leading, Math.max(0, right - left), height),
        runs: runs.length ? runs : [{ text: "", font: "Arial", sizePx: 12, weight: 400, italic: false,
                                      color: "000000", alpha: 1 }],
        _seams: seams,
        _runBoxes: runs.length ? runBoxes : [[left, right]],
        column: line.column,
        _ellipsis: line.ellipsis || null,
      };
    }).filter(function (line) { return line.runs.length && line.runs.some(function (r) { return r.text; }); });
    built.forEach(keepEllipsisBox);
    return { lines: built, hidden: hidden };
  }

  /**
   * `{left, right}` of one line's first and last glyph that is not whitespace, in local px — or null
   * when the line has none. `fragments` are the line's, in source order. One `Range` per end: the
   * per-character rects of the fragment loop are gone by the time a line is assembled.
   */
  function glyphExtent(fragments, frame) {
    var range = document.createRange();
    function rectAt(node, i) {
      range.setStart(node, i);
      range.setEnd(node, i + 1);
      var rects = range.getClientRects();
      for (var r = 0; r < rects.length; r++) {
        if (rects[r].width > 0) return localBox(frame, rects[r]);
      }
      return null;
    }
    var left = null, right = null, f, i, rect;
    for (f = 0; f < fragments.length && left === null; f++) {
      for (i = fragments[f].from; i <= fragments[f].to && left === null; i++) {
        if (/\s/.test(fragments[f].node.data[i])) continue;
        rect = rectAt(fragments[f].node, i);
        if (rect) left = rect.x;
      }
    }
    for (f = fragments.length - 1; f >= 0 && right === null; f--) {
      for (i = fragments[f].to; i >= fragments[f].from && right === null; i--) {
        if (/\s/.test(fragments[f].node.data[i])) continue;
        rect = rectAt(fragments[f].node, i);
        if (rect) right = rect.x + rect.w;
      }
    }
    return left === null || right === null || right < left ? null : { left: left, right: right };
  }

  // ------------------------------------------------------------------ as seen: columns (WP-F, F-C)
  //
  // Inside a CSS multi-column box the browser lays one flow out in column boxes side by side, so
  // lines of different columns share tops. Grouping fragments by height alone interleaved them
  // (p10 B: each exported line was a left-column line glued to a right-column one). The column of
  // a character is decided *per character*, from its x-centre: a sentence balanced to one line per
  // column puts both halves on one top, and only the column tells them apart.

  /** Per-extraction caches (reset by `__engineExtract`): multicol geometry, ellipsis widths. */
  var bandCache = new WeakMap();
  var ellipsisCache = {};

  /**
   * The column boxes `el`'s text is laid out in: `{el, x, gap, colW, count}` for the nearest
   * multicol ancestor-or-self, `{spanner: true, el}` when a `column-span: all` block lies between
   * (it is laid out across the whole box, not in a column), null otherwise. The walk stops at an
   * out-of-flow element: it is not fragmented into the columns around it.
   */
  function columnBandsOf(el) {
    if (!el || el.nodeType !== 1) return null;
    if (bandCache.has(el)) return bandCache.get(el);
    var result = null, spanner = false;
    for (var node = el; node && node.nodeType === 1 && node !== document.body &&
         node !== document.documentElement; node = node.parentElement) {
      var cs = getComputedStyle(node);
      if (cs.columnCount !== "auto" || cs.columnWidth !== "auto") {
        result = spanner ? { spanner: true, el: node } : multicolBands(node, cs);
        break;
      }
      if (cs.columnSpan === "all") spanner = true;
      if (isOutOfFlow(cs)) break;
    }
    bandCache.set(el, result);
    return result;
  }

  /** CSS Multi-column §3.4's column count and width, from the used content box. */
  function multicolBands(m, cs) {
    var cb = contentBox(box(m.getBoundingClientRect()), cs);
    var gap = cs.columnGap === "normal" ? px(cs.fontSize) : px(cs.columnGap);
    var count = cs.columnCount !== "auto" ? parseInt(cs.columnCount, 10) : 0;
    var width = cs.columnWidth !== "auto" ? px(cs.columnWidth) : 0;
    var n = count;
    if (width > 0) {
      n = Math.max(1, Math.floor((cb.w + gap) / (width + gap)));
      if (count > 0) n = Math.min(n, count);
    }
    if (!n || (n <= 1 && !(width > 0))) return null;
    return { el: m, x: cb.x, gap: gap, colW: Math.max(0, (cb.w - (n - 1) * gap) / n), count: n };
  }

  /** Which column box a character is in — any index ≥ 0: overflow columns sit past the last one. */
  function columnOf(bands, rect) {
    var cx = rect.x + rect.w / 2;
    return Math.max(0, Math.floor((cx - bands.x + bands.gap / 2) / (bands.colW + bands.gap)));
  }

  function columnBox(bands, c) {
    return { x: bands.x + c * (bands.colW + bands.gap), w: bands.colW };
  }

  /**
   * A run of pending paragraphs inside a multicol box becomes one text element per column box that
   * received a line, in column order (the counting rule in `fixtures/torture/README.md`). Returns
   * false — and does nothing — when no pending paragraph is in columns; otherwise it emits
   * everything, handing the paragraphs outside columns (a spanner included) back to `flushText`.
   *
   * It is also where paragraphs the clip hid entirely are said (`keepHidden` holds them on the run,
   * as `pending.hidden`, since a source with no line never enters it): with nothing else pending,
   * one error names the hidden block; otherwise they ride on the record of their own flow.
   *
   * A native `<a:bodyPr numCol>` was not used: PowerPoint fills column 1 to the frame height by its
   * own metrics, so the break moves whenever a line's predicted width differs. Locked lines per
   * column box are the only faithful choice, and the emitter needs no change.
   */
  function emitColumnRecords(pending, container, ctx, state) {
    var stash = pending.hidden || [];
    if (!pending.length) {
      if (stash.length) trimClipped(null, pending, container, ctx, state);
      return true;
    }
    var modes = pending.map(function (source) {
      if (source.column !== undefined) return null;   // already one column box's piece (`columnPiece`)
      var bands = columnBandsOf(source.el);
      return bands && !bands.spanner ? bands : null;
    });
    if (!modes.some(Boolean)) return false;
    var runs = [];
    pending.forEach(function (source, index) {
      var last = runs[runs.length - 1];
      var mode = modes[index];
      if (last && (last.bands ? last.bands.el : null) === (mode ? mode.el : null)) last.sources.push(source);
      else runs.push({ bands: mode, sources: [source] });
    });
    // A wholly hidden paragraph joins the last run of its own flow (else the last run).
    stash.forEach(function (entry) {
      var bands = columnBandsOf(entry.el), key = bands && !bands.spanner ? bands.el : null, home = null;
      runs.forEach(function (run) { if ((run.bands ? run.bands.el : null) === key) home = run; });
      home = home || runs[runs.length - 1];
      (home.sources.hidden = home.sources.hidden || []).push(entry);
    });
    runs.forEach(function (run) {
      if (run.bands) emitColumnRun(run.sources, run.bands, container, ctx, state);
      else flushText(run.sources, container, ctx, state);
    });
    return true;
  }

  /**
   * One multicol flow, already measured by `pushParagraph` (each line carries its column): each
   * column box's lines go through `flushText` as a run of their own, so a column record is built,
   * aligned and checked exactly as any other text (WP-B's alignment, `x-run-boxes`, seams, overflow),
   * then named `… col N` with `extras["x-wpf-column"]`. The flow's clip finding is one, on its first
   * column box: the author clipped the multicol element, not each column. Its detached boxes and
   * pseudo copies are walked once, after its last column.
   */
  function emitColumnRun(sources, bands, container, ctx, state) {
    var columns = [];
    sources.forEach(function (source) {
      source.lines.forEach(function (line) { if (columns.indexOf(line.column) < 0) columns.push(line.column); });
    });
    columns.sort(function (a, b) { return a - b; });

    var path = cssPath(bands.el);
    if (!state.multicolSeen) state.multicolSeen = [];
    if (state.multicolSeen.indexOf(bands.el) < 0) {
      state.multicolSeen.push(bands.el);
      diagnose(state, "info", path, "CSS columns: " + path + " exported as " + columns.length +
               " text boxes, one per column", null);
      var mcs = getComputedStyle(bands.el);
      if (mcs.columnRuleStyle !== "none" && px(mcs.columnRuleWidth) > 0) {
        diagnose(state, "warn", path, "column-rule on " + path + " is not drawn; dropped", null);
      }
    }
    var overflowing = columns.filter(function (c) { return c >= bands.count; }).length;
    if (overflowing) {
      diagnose(state, "warn", path, "text overflowed into " + overflowing + " column(s) outside " + path +
               "; they are exported beside the box", null);
    }

    columns.forEach(function (c, k) {
      var band = columnBox(bands, c), pieces = [];
      sources.forEach(function (source) {
        var lines = source.lines.filter(function (line) { return line.column === c; });
        if (lines.length) pieces.push(columnPiece(source, lines, band, c));
      });
      if (k === 0) {
        pieces.hidden = sources.filter(function (source) { return source.hidden; }).concat(sources.hidden || []);
      }
      var before = state.records.length;
      flushText(pieces, container, ctx, state);
      for (var i = before; i < state.records.length; i++) {
        var record = state.records[i];
        if (record.kind !== "text") continue;
        record.name = record.name + " col " + (c + 1);
        record.extras = record.extras || {};
        record.extras["x-wpf-column"] = { index: c, count: bands.count };
        break;
      }
    });
    // As `flushText` does once a record is out: the detached boxes after the text (R3), then the
    // out-of-flow pseudo copies (WP-E).
    sources.forEach(function (source) {
      (source.then || []).forEach(function (queued) { walkDetached(queued, ctx, state); });
    });
    walkDeferred(sources.reduce(function (acc, source) { return acc.concat(source.after || []); }, []), ctx, state);
  }

  /**
   * The part of a pending entry that lies in column box `c`: the entry's own fields, its lines in
   * that column, a box as wide as the column box, and space before measured from the lines (a
   * fragmented block's border box spans every column it reaches). A list item continued from the
   * previous column has no marker of its own there.
   */
  function columnPiece(source, lines, band, c) {
    var span = lines.reduce(function (acc, line) { return unionBox(acc, line.box); }, null);
    var starts = source.lines[0] === lines[0];
    var left = Math.min(band.x, span.x), right = Math.max(band.x + band.w, span.x + span.w);
    return {
      el: source.el, cs: source.cs, nodes: source.nodes, item: source.item, hug: source.hug,
      segment: source.segment, scope: source.scope, lines: lines, span: span,
      box: boxOf(left, span.y, right - left, span.h), outer: span,
      bullet: starts ? source.bullet : null, textLeft: starts ? source.textLeft : null,
      split: starts ? source.split : null, tracks: null, continues: !starts, column: c,
    };
  }

  /**
   * Does `next` follow `previous` down one CSS multi-column flow — its first line in a later column
   * than `previous`'s last, or below it in the same column? Then the two stack in that flow whatever
   * their fragments' union boxes say (a paragraph continued into the next column spans the whole
   * flow's height, so `stacked()` sees it beside the one before), and `pushParagraph` keeps them in
   * one run (WP-F). Text beside a detached box shares its line with the text before: not this rule.
   */
  function sameColumnFlow(previous, next) {
    var a = columnBandsOf(previous.el), b = columnBandsOf(next.el);
    if (!a || !b || a.spanner || b.spanner || a.el !== b.el) return false;
    var above = previous.lines[previous.lines.length - 1], first = next.lines[0];
    if (first.column !== above.column) return first.column > above.column;
    return first.box.y >= above.box.y + above.box.h - STACK_TOL * Math.min(above.box.h, first.box.h);
  }

  /**
   * Is `el`, a decorated block, split across column boxes? Its background and border are drawn as
   * one box around every fragment — per-fragment decoration is a follow-up, so say so.
   */
  function warnFragmented(el, state) {
    var bands = columnBandsOf(el);
    if (!bands || bands.spanner || el.getClientRects().length <= 1) return;
    var path = cssPath(el);
    diagnose(state, "warn", path, path + " is fragmented across columns; its background/border is drawn as one box",
             null);
  }

  // ------------------------------------------------------------------ as seen: clipped text (WP-F, F-D)
  //
  // An `overflow:hidden` box, `-webkit-line-clamp` and `text-overflow: ellipsis` hide text the
  // reader never sees; PowerPoint cannot clip a text frame, so exporting every line put the hidden
  // words on the slide (p09 D). The rule is per character, inside `measureParagraph`, because only
  // the `Range` rects say which characters the clip hides: a character is hidden when less than half
  // of its height is inside the clip or its x-centre is outside it. A partly visible glyph is
  // dropped, not kept — a whole glyph where the reader saw half is the worse lie, and the error
  // diagnostic says what happened.

  /**
   * The clip a paragraph's text is measured against: the walk's, plus the paragraph's own when it
   * is not in the walk's yet (`own`: a text block; an anonymous run's block is the walk's container,
   * whose clip `walkTransformed` already applied).
   */
  function paragraphClip(el, own, ctx) {
    if (ctx.frame.angle) return null;   // a rotated box's clip is not axis-aligned (see `push`)
    var clip = ctx.clip;
    if (own && el && el.nodeType === 1) {
      var cs = getComputedStyle(el);
      if (/hidden|clip|scroll|auto/.test(cs.overflow + " " + cs.overflowX + " " + cs.overflowY)) {
        clip = intersectBox(clip, paintedBox(ctx.frame, paddingBox(borderBox(el, ctx.frame), cs)));
      }
    }
    return clip || null;
  }

  /**
   * A paragraph the clip hid entirely has no line, so `pushParagraph` does not add it to the run;
   * what it hid is kept on the run (`pending.hidden`) for `trimClipped`, which says so once the run
   * is flushed — folded into the finding of the text beside it, or as a hidden block of its own.
   */
  function keepHidden(run, source, clip, hidden) {
    if (!clip || !hidden.chars) return;
    var list = run.pending.hidden || (run.pending.hidden = []);
    list.push({ el: source.el, hug: !!source.hug, clip: clip, hidden: hidden });
  }

  /** The `hidden` half of `measureParagraph`'s answer, before any character is seen. */
  function hiddenTally(clip, paragraphEl) {
    return { chars: 0, lines: 0, firstText: null, fullExtent: null, truncatedTops: [], ellipsis: false,
             _rtl: !!clip && getComputedStyle(paragraphEl).direction === "rtl", _tops: {}, _first: null,
             _last: null };
  }

  /** A row of characters: one line of one column (overflow columns share their tops with the rest). */
  function rowKey(column, top) {
    return column + "|" + Math.round(top);
  }

  /** Is this character (its painted rect) hidden by `clip`? Counts it either way. */
  function clipCharacter(hidden, rect, clip, order, index, column) {
    hidden.fullExtent = unionBox(hidden.fullExtent, rect);
    var overlap = Math.min(rect.y + rect.h, clip.y + clip.h) - Math.max(rect.y, clip.y);
    var cx = rect.x + rect.w / 2;
    var gone = overlap < 0.5 * rect.h || cx < clip.x || cx > clip.x + clip.w;
    var key = rowKey(column, rect.y);
    var row = hidden._tops[key] || (hidden._tops[key] = {
      kept: 0, gone: 0, keptMin: Infinity, keptMax: -Infinity, goneMin: Infinity, goneMax: -Infinity });
    if (gone) {
      row.gone++;
      row.goneMin = Math.min(row.goneMin, cx);
      row.goneMax = Math.max(row.goneMax, cx);
      hidden.chars++;
      if (!hidden._first) hidden._first = { order: order, index: index };
      hidden._last = { order: order, index: index };
    } else {
      row.kept++;
      row.keptMin = Math.min(row.keptMin, cx);
      row.keptMax = Math.max(row.keptMax, cx);
    }
    return gone;
  }

  /** Lines wholly hidden, lines cut at their end edge, and the hidden words, once per paragraph. */
  function settleHidden(hidden, textNodes) {
    Object.keys(hidden._tops).forEach(function (key) {
      var row = hidden._tops[key];
      if (row.gone && !row.kept) hidden.lines++;
      // Cut at the end edge: hidden text past the kept text in reading direction (LTR: to the
      // right; RTL: to the left). Text cut at its start edge gets no ellipsis from the browser.
      if (row.gone && row.kept && (hidden._rtl ? row.goneMin < row.keptMin : row.goneMax > row.keptMax)) {
        hidden.truncatedTops.push(key);
      }
    });
    if (!hidden._first) return;
    // The quote is the hidden text itself, from the first hidden character to the last: in RTL the
    // hidden part is the start of the text, so "everything after the first hidden one" would quote
    // what the reader does see.
    var text = "", first = hidden._first, last = hidden._last;
    for (var order = first.order; order <= last.order && text.length < 80; order++) {
      var node = textNodes[order];
      var parent = node.parentElement;
      var cs = parent ? getComputedStyle(parent) : null;
      var piece = node.data.slice(order === first.order ? first.index : 0,
                                  order === last.order ? last.index + 1 : node.data.length);
      text += applyCase(normaliseText(piece, cs ? cs.whiteSpace : "normal"), cs ? cs.textTransform : "none");
    }
    hidden.firstText = quoteText(text.slice(0, 80));
  }

  /** The nearest `line-clamp` box from `el` up to (and including) the first box that clips. */
  function lineClampOf(el) {
    for (var node = el; node && node.nodeType === 1 && node !== document.body; node = node.parentElement) {
      var cs = getComputedStyle(node);
      var clamp = cs.webkitLineClamp || cs.lineClamp || "none";
      if (clamp && clamp !== "none") return node;
      if (/hidden|clip|scroll|auto/.test(cs.overflowX + " " + cs.overflowY)) return null;
    }
    return null;
  }

  /**
   * Keep the ellipsis the browser drew: on the last visible line of a `line-clamp` box that hid
   * lines, and on every line a `text-overflow: ellipsis` block cut at its end edge. The line is
   * trimmed to the characters that end before `content edge − ellipsis width`, and an ellipsis
   * fragment is appended (prepended for RTL) so the runs, the line box and every per-run geometry
   * built from the fragments include it. Verified on p09 D: 340 − 8.98 = 331.0 → "…is nev…",
   * pixel-identical to the browser.
   *
   * The ellipsis is styled by the **block** that holds the line, not by the last run it follows:
   * measured in Edge (a bold red last run gets a regular black "…", for both `line-clamp` and
   * `text-overflow`), which is what CSS Overflow 3 says.
   */
  function keepEllipsis(lines, paragraphEl, frame, hidden) {
    if (!lines.length) return;
    var cs = getComputedStyle(paragraphEl);
    var targets = [];
    if (hidden.lines > 0 && lineClampOf(paragraphEl)) targets.push(lines[lines.length - 1]);
    if (cs.textOverflow === "ellipsis" && cs.overflowX !== "visible" && hidden.truncatedTops.length) {
      lines.forEach(function (line) {
        var cut = line.fragments.some(function (f) {
          return hidden.truncatedTops.indexOf(rowKey(f.column, f.top + (frame.dy || 0))) >= 0;
        });
        if (cut && targets.indexOf(line) < 0) targets.push(line);
      });
    }
    if (!targets.length) return;
    var style = runStyle({ parentElement: paragraphEl }, paragraphEl);
    var width = ellipsisWidth(paragraphEl, style);
    var content = contentBox(borderBox(paragraphEl, frame), cs);
    targets.forEach(function (line) {
      if (ellipsize(line, content, style, width, frame, hidden._rtl)) hidden.ellipsis = true;
    });
  }

  function ellipsize(line, content, style, width, frame, rtl) {
    var ordered = line.fragments.slice().sort(function (a, b) { return a.seq - b.seq; });
    var range = document.createRange();
    function charBox(fragment, i) {
      range.setStart(fragment.node, i);
      range.setEnd(fragment.node, i + 1);
      var rects = range.getClientRects();
      for (var r = 0; r < rects.length; r++) if (rects[r].width > 0) return localBox(frame, rects[r]);
      return null;
    }
    var limit = rtl ? content.x + width : content.x + content.w - width;
    var edge = null, keeper = null;
    while (ordered.length) {
      var fragment = rtl ? ordered[0] : ordered[ordered.length - 1];
      while (fragment.from <= fragment.to) {
        var b = charBox(fragment, rtl ? fragment.from : fragment.to);
        if (b && (rtl ? b.x >= limit - 0.01 : b.x + b.w <= limit + 0.01)) { edge = rtl ? b.x : b.x + b.w; break; }
        if (rtl) fragment.from++; else fragment.to--;
      }
      if (edge !== null) {
        if (rtl) fragment.left = edge; else fragment.right = edge;
        keeper = fragment;
        break;
      }
      line.fragments.splice(line.fragments.indexOf(fragment), 1);
      ordered.splice(ordered.indexOf(fragment), 1);
    }
    var anchor = keeper || ordered[0];
    if (!anchor) {
      // Nothing fits before the ellipsis: the line is the ellipsis alone, at the start edge.
      edge = rtl ? content.x + content.w : content.x;
      anchor = { order: 0, top: line.top, bottom: line.bottom, seq: 0, column: line.column };
    }
    line.fragments.push({
      node: document.createTextNode("\u2026"), order: anchor.order, style: style, from: 0, to: 0,
      top: anchor.top, bottom: anchor.bottom,
      left: rtl ? edge - width : edge, right: rtl ? edge : edge + width,
      seq: rtl ? anchor.seq - 0.5 : anchor.seq + 0.5, column: anchor.column, ellipsis: true,
    });
    // The synthetic "…" is in no document, so it has no `Range` rect: its box is carried to
    // `keepEllipsisBox`, which puts it back into the line box and the run boxes (plan §16 #10).
    line.ellipsis = { left: rtl ? edge - width : edge, right: rtl ? edge : edge + width, first: rtl };
    return true;
  }

  /**
   * A built line whose ellipsis `ellipsize` appended: its line box and the `x-run-boxes` entry of
   * the run that holds the "…" (the last run, or the first where the ellipsis leads, RTL) are
   * widened to it. They were measured from glyph rects (`glyphExtent`), which the synthetic
   * fragment has none of, so without this the ellipsis would sit outside its own run's box.
   */
  function keepEllipsisBox(line) {
    var e = line._ellipsis;
    delete line._ellipsis;
    if (!e) return;
    var boxes = line._runBoxes, k = e.first ? 0 : boxes.length - 1;
    boxes[k] = [Math.min(boxes[k][0], e.left), Math.max(boxes[k][1], e.right)];
    var left = Math.min(line.box.x, e.left), right = Math.max(line.box.x + line.box.w, e.right);
    line.box = boxOf(left, line.box.y, right - left, line.box.h);
  }

  /**
   * The width of "…" in the block's font, letter-spacing and features — measured by the browser on
   * a probe span, because canvas `measureText` ignores letter-spacing and features.
   */
  function ellipsisWidth(el, style) {
    var key = styleKey(style);
    if (ellipsisCache[key] !== undefined) return ellipsisCache[key];
    var source = getComputedStyle(el);
    var probe = document.createElement("span");
    probe.setAttribute("data-engine-probe", "");
    probe.style.cssText = "position:absolute;left:-10000px;top:-10000px;visibility:hidden;white-space:pre";
    ["fontStyle", "fontVariant", "fontWeight", "fontStretch", "fontSize", "fontFamily", "letterSpacing",
     "fontKerning", "fontFeatureSettings", "fontVariationSettings", "fontVariantLigatures",
     "fontVariantCaps", "fontVariantNumeric", "fontVariantEastAsian", "textRendering"].forEach(function (name) {
      if (source[name]) probe.style[name] = source[name];
    });
    probe.textContent = "\u2026";
    document.body.appendChild(probe);
    var width = probe.getBoundingClientRect().width;
    document.body.removeChild(probe);
    ellipsisCache[key] = width;
    return width;
  }

  /** Where a paragraph's glyphs were before trimming, widened like its text box (painted px). */
  function untrimmedBox(entry, ctx) {
    var extent = entry.hidden.fullExtent;
    if (!extent) return null;
    var el = entry.el, cs = getComputedStyle(el);
    var block = entry.hug ? extent
      : paintedBox(ctx.frame, contentBox(borderBox(el, ctx.frame), cs));
    var bullet = el.tagName === "LI" ? bulletOf(el, cs, ctx.frame) : null;
    var left = Math.min(bullet ? bullet._listLeft + (ctx.frame.dx || 0) : block.x, extent.x);
    var right = Math.max(block.x + block.w, extent.x + extent.w);
    return boxOf(left, extent.y, right - left, extent.h);
  }

  /**
   * After a text record is pushed (or, with `record` null, instead of pushing one): record what the
   * clip hid. The record's `clip` is the clip against the *untrimmed* text — the IR still says "this
   * was clipped" — with `extras["x-wpf-clipped"]`, and one error names what is gone. When every
   * paragraph is hidden there is no record, and the error still says so (master brief §10.9).
   * Returns true when the record was clipped, so the caller skips the overflow check: one defect,
   * one finding.
   */
  function trimClipped(record, entries, container, ctx, state) {
    var chars = 0, lines = 0, ellipsis = false, quote = null, glyphs = null, clip = null, untrimmed = null;
    entries = entries.concat(entries.hidden || []);   // with the paragraphs the clip hid entirely
    entries.forEach(function (entry) {
      var hidden = entry.hidden;
      if (!entry.clip || !hidden || !hidden.fullExtent) return;
      chars += hidden.chars;
      lines += hidden.lines;
      ellipsis = ellipsis || hidden.ellipsis;
      if (!quote && hidden.firstText) quote = hidden.firstText;
      glyphs = unionBox(glyphs, hidden.fullExtent);
      clip = unionBox(clip, entry.clip);
      untrimmed = unionBox(untrimmed, untrimmedBox(entry, ctx));
    });
    var clipped = !!(record && record.clip);
    if (!glyphs || !clip) return clipped;
    if (!chars) {
      // Nothing is hidden, so only a *line box* the clip cuts is worth an error: glyph rects overhang
      // their line boxes (`line-height: 1` on Calibri), and a chip fitted to its text must not count
      // as cut. Vertically the line boxes, horizontally the glyphs.
      var lineBox = !record ? null : record.paragraphs.reduce(function (acc, paragraph) {
        return paragraph.lines.reduce(function (inner, line) { return unionBox(inner, line.box); }, acc);
      }, null);
      if (!lineBox || contains(clip, boxOf(glyphs.x, lineBox.y, glyphs.w, lineBox.h))) return clipped;
    }
    // Only the canvas cuts when the clip is no tighter than the slide itself: then the text is not
    // "clipped by an ancestor", it runs off the slide (Peter's decision #12).
    var canvas = !!state.canvasBox && contains(clip, state.canvasBox);
    if (!record) {
      if (!chars) return true;
      var where = cssPath(entries[0].el);
      if (canvas) {
        diagnose(state, "error", where, "\u201C" + quote + "\u201D is outside the canvas: the whole block is " +
                 "off the slide and is not exported", null);
      } else {
        diagnose(state, "error", where, "text is clipped by an overflow:hidden ancestor: the whole block is " +
                 "hidden and is not exported \u2014 \u201C" + quote + "\u201D", null);
      }
      return true;
    }
    record.clip = intersectBox(clip, untrimmed || record.box);
    if (!record.extras) record.extras = {};
    record.extras["x-wpf-clipped"] = { hiddenLines: lines, hiddenChars: chars, ellipsis: ellipsis, canvas: canvas };
    var index = state.records.indexOf(record);
    var path = record.source.path;
    if (!quote) quote = quoteOf(record.paragraphs);
    if (!chars && canvas) {
      diagnose(state, "error", path, "\u201C" + quote + "\u201D runs outside the canvas: part of a line is " +
               "off the slide, and the export shows it whole", index);
    } else if (!chars) {
      diagnose(state, "error", path, "text is clipped by an overflow:hidden ancestor: part of a line is " +
               "cut off, and the export shows that line whole \u2014 \u201C" + quote + "\u201D", index);
    } else if (canvas) {
      diagnose(state, "error", path, "\u201C" + quote + "\u201D runs outside the canvas: " + lines +
               " line(s) and " + chars + " character(s) are off the slide and are not exported", index);
    } else {
      diagnose(state, "error", path, "text is clipped by an overflow:hidden ancestor: " + lines +
               " line(s) and " + chars + " character(s) are hidden and are not exported \u2014 \u201C" +
               quote + "\u201D" + (ellipsis ? " (the browser's ellipsis was kept)" : ""), index);
    }
    return true;
  }

  // ------------------------------------------------------------------ as seen: remote resources (WP-F, F-E)
  //
  // The measuring browser used to fetch anything a slide named — a remote map, an internal address,
  // a file share behind `//host/x.png` — and the export then dropped the image with two errors while
  // the reference showed it. The network policy now lives in `html.py: route_assets`
  // (`REMOTE_RESOURCES`: `block` by default everywhere, `raster` as an opt-in), and the walk knows
  // it, so this is the one place a remote image or frame is reported, once.

  var ASSET_PATH = /\/api\/projects\/[^\/]+\/assets\/[^\/?#]+/;

  /** Does this URL leave the machine? `raw` is the attribute as written (a `//host/x` is remote). */
  function isRemoteUrl(resolved, raw) {
    var url = String(resolved || raw || "");
    if (ASSET_PATH.test(url.replace(/[?#].*$/, ""))) return false;   // served from the project's assets
    if (raw && /^\s*\/\//.test(raw)) return true;
    var m = /^([a-z][a-z0-9+.\-]*):\/\/([^\/?#]*)/i.exec(url);
    if (!m) return false;
    var scheme = m[1].toLowerCase(), host = m[2].toLowerCase();
    if (/^(https?|wss?|ftp)$/.test(scheme)) return true;
    return scheme === "file" && host !== "" && host !== "localhost";
  }

  /**
   * A remote `<img>` or `background-image`: under `block` nothing is pushed and one error says why;
   * under `raster` a loaded one is pushed with `remote: url` (Python captures the browser's picture),
   * a failed one is an error naming what the network log saw. True when the record should be pushed.
   */
  function remoteImage(record, background, el, state) {
    var url = record.url, path = cssPath(el);
    var why = state.remotePolicy !== "raster"
      ? "remote resources are blocked by policy (SLIDE_ENGINE_REMOTE_RESOURCES=block); upload the image " +
        "as a project asset and reference /api/projects/<id>/assets/<file>"
      : (record.complete ? null : (state.blockedUrls[url] || "the request failed"));
    if (why === null) {
      record.remote = url;
      if (background) record.background = true;
      return true;
    }
    if (background) diagnose(state, "error", path, "background image did not load: " + url + " \u2014 " + why, null);
    else diagnose(state, "error", path, "image did not load: " + url + " \u2014 " + why, null);
    return false;
  }

  /** A remote `<iframe>/<object>/<embed>` under `block`: no raster of Chromium's error page. */
  function remoteFrameBlocked(el, tag, state) {
    if (state.remotePolicy === "raster" || (tag !== "iframe" && tag !== "object" && tag !== "embed")) return false;
    var raw = el.getAttribute("src") || el.getAttribute("data") || "";
    var url = el.src || el.data || raw;
    if (!isRemoteUrl(url, raw)) return false;
    diagnose(state, "error", cssPath(el), "remote resource blocked by policy: <" + tag + " src=" + url +
             "> is not exported", null);
    return true;
  }

  // ------------------------------------------------------------------ detached inline boxes
  //
  // A PowerPoint paragraph has no geometry between runs. When the browser gives an atomic inline
  // box horizontal geometry of its own — a chip's padding or `margin-left`, a fixed-width label
  // column beside a text column — keeping its text in the paragraph either glues it to its
  // neighbour ("…COMES FROMILLUSTRATIVE…") or, with spaces around it, shifts every word after it
  // left by the padding the run cannot carry, under the next chip's shape. Such a box is
  // **detached** (R3): it leaves the paragraph and is walked as an element of its own, painted once
  // by `emitDecoration`; the text before and after it becomes hugged segments that the stacking
  // rule (R1) alone groups into elements. A flush atomic box with no geometry
  // (`Auxi<span style="display:inline-block">Studio</span>`) stays inline: its runs abut and read right.

  /** The outermost atomic inline box between a text node and its paragraph element, or null. */
  function atomicAncestor(textNode, paragraphEl) {
    var outer = null;
    for (var node = textNode.parentElement; node && node !== paragraphEl; node = node.parentElement) {
      if (isAtomicInline(getComputedStyle(node).display)) outer = node;
    }
    return outer;
  }

  /** Does this atomic box wrap its own content to more than one line? Then it is a block, laid inline. */
  function wrapsOwnLines(el) {
    var cs = getComputedStyle(el);
    var content = contentBox(box(el.getBoundingClientRect()), cs);
    return content.h > lineHeightPx(cs) * 1.5;
  }

  /**
   * The union of the client rects of `el`'s own text: one `Range.selectNodeContents` per text node,
   * zero-width rects (whitespace collapsed at an edge) left out. Null when no text is painted.
   */
  function textSpanOf(el) {
    var range = document.createRange(), union = null;
    textNodesOf(el).forEach(function (node) {
      range.selectNodeContents(node);
      var rects = range.getClientRects();
      for (var i = 0; i < rects.length; i++) {
        if (rects[i].width > 0) union = unionBox(union, box(rects[i]));
      }
    });
    return union;
  }

  /**
   * R3 (a): horizontal geometry no run can carry — padding, border or margin on either side, or a
   * content box whose width is not its own text's (a fixed-width label column, `min-width`, centred
   * content — or a fixed width its text overflows: what follows starts at the box's edge, not at
   * the text's end). Measured, not declared: `getComputedStyle().width` is always the used width.
   */
  function hasOwnGeometry(el) {
    var cs = getComputedStyle(el);
    var sides = px(cs.paddingLeft) + px(cs.paddingRight) + px(cs.borderLeftWidth) +
                px(cs.borderRightWidth) + Math.abs(px(cs.marginLeft)) + Math.abs(px(cs.marginRight));
    if (sides > DETACH_GEOMETRY_PX) return true;
    if (!textSpanOf(el)) return false;
    // The *sum* of the text's widths, not their union: a gap inside the box (a nested inline-block's
    // margin, an inline-flex `gap`) lies inside the union and is geometry all the same (B1 critique #6).
    return Math.abs(contentBox(box(el.getBoundingClientRect()), cs).w - textWidthOf(el)) > DETACH_GEOMETRY_PX;
  }

  /** The summed width of `el`'s own painted text rects — its text with every gap between them removed. */
  function textWidthOf(el) {
    var range = document.createRange(), total = 0;
    textNodesOf(el).forEach(function (node) {
      range.selectNodeContents(node);
      var rects = range.getClientRects();
      for (var i = 0; i < rects.length; i++) total += rects[i].width > 0 ? rects[i].width : 0;
    });
    return total;
  }

  /**
   * An `inline-flex`/`inline-grid` box that lays two or more in-flow items with text out beside one
   * another is a layout, not a word: walked as an element whether or not anything else shares its
   * paragraph, so its items become elements of their own (B1 critique #6).
   */
  function laysOutItems(el) {
    var display = getComputedStyle(el).display;
    if (display !== "inline-flex" && display !== "inline-grid") return false;
    var items = 0;
    for (var i = 0; i < el.childNodes.length; i++) {
      var node = el.childNodes[i];
      if (node.nodeType === 3) { if (/\S/.test(node.data)) items++; continue; }
      if (node.nodeType !== 1) continue;
      var cs = getComputedStyle(node);
      if (cs.display === "none" || isOutOfFlow(cs)) continue;
      if (node.textContent && /\S/.test(node.textContent)) items++;
    }
    return items >= 2;
  }

  /**
   * Does a special element inside an inline run paint anything? One that is hidden or has no area
   * (an `<svg>` holding only `<defs>`) is left in place: detaching it would split the text for nothing.
   */
  function paintsInline(el) {
    var cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") return false;
    var r = el.getBoundingClientRect();
    return r.width >= MIN_EXTENT && r.height >= MIN_EXTENT;
  }

  /**
   * A run's leaves in document order (render check R1): its text nodes and the special elements
   * (`isSpecial`: an `<svg>` icon, an `<img>`, a chart host, a forced raster) below `roots`. Text
   * inside a special is the special's — a raster's pixels, an SVG's own `<text>` — never the
   * paragraph's. Without `roots` the leaves are the text nodes, as before R1.
   */
  function inlineLeaves(textNodes, roots) {
    if (!roots) return textNodes.slice();
    var wanted = new Set(textNodes), leaves = [];
    function visit(node) {
      if (node.nodeType === 3) { if (wanted.has(node)) leaves.push(node); return; }
      if (node.nodeType !== 1 || node.hasAttribute("data-engine-probe")) return;
      if (node.tagName === "ASIDE" && node.classList.contains("notes")) return;
      if (isSpecial(node)) { if (paintsInline(node)) leaves.push(node); return; }
      for (var child = node.firstChild; child; child = child.nextSibling) visit(child);
    }
    roots.forEach(visit);
    return leaves;
  }

  /**
   * The box a special leaf is walked through (R1): the outermost atomic inline box around it, below
   * `stop`, that holds no text of the paragraph (`hasText`) — a `.cico` icon span, a badge — so the
   * box's own paint goes with its icon; else the special element itself.
   */
  function specialOwner(special, stop, hasText) {
    var owner = special;
    for (var node = special.parentElement; node && node !== stop; node = node.parentElement) {
      if (hasText(node)) break;
      if (isAtomicInline(getComputedStyle(node).display)) owner = node;
    }
    return owner;
  }

  /**
   * A paragraph's text nodes split at its detached boxes: `[{nodes: [...]} | {box: el}]`, in order.
   * Detached boxes are marked so the paragraph's `emitInlineDecoration` leaves their paint to the
   * element walk — one painter per box, or every chip is drawn twice.
   *
   * R1 (render check, defect 1): the leaves are the run's text nodes *and* its special descendants
   * (`inlineLeaves`, given the run's `roots`). A box with no text — an `<svg>` in an inline-block icon
   * span, an `<img>` in a `<span>` — used to make no token at all, so it was never detached, never
   * walked, and vanished. A special's token is its `specialOwner`; it always detaches (the "shares
   * the paragraph" rule is for text, which a lone icon is not), so `walkDetached` → `walkElement`
   * reaches it. A special inside an atomic box that has text rides with that box: walked with it
   * when the box detaches, detached on its own (through its own `specialOwner`) when the box stays.
   */
  function inlineSegments(paragraphEl, textNodes, roots) {
    var texty = textNodes.filter(function (node) { return /\S/.test(node.data); });
    function hasText(el) {
      for (var i = 0; i < texty.length; i++) if (el.contains(texty[i])) return true;
      return false;
    }
    var tokens = [];
    inlineLeaves(textNodes, roots).forEach(function (leaf) {
      var text = leaf.nodeType === 3;
      var outer = atomicAncestor(leaf, paragraphEl), key = outer;
      // A special with no text-holding atomic box around it is a token of its own, keyed by its owner.
      if (!text && (!outer || !hasText(outer))) key = specialOwner(leaf, paragraphEl, hasText);
      var last = tokens[tokens.length - 1];
      if (!last || last.box !== key) tokens.push(last = { box: key, nodes: [], leaves: [], special: false });
      last.leaves.push(leaf);
      if (text) last.nodes.push(leaf);
      else if (key !== outer || !hasText(outer)) last.special = true;
      else last.inner = true;
    });
    function said(token) {
      return !!token.box || token.nodes.some(function (node) { return /\S/.test(node.data); });
    }
    var segments = [];
    function detach(el) {
      el.__engineDetached = true;
      segments.push({ box: el });
    }
    function keep(nodes) {
      if (!nodes.length) return;
      var last = segments[segments.length - 1];
      if (last && last.nodes) last.nodes = last.nodes.concat(nodes);
      else segments.push({ nodes: nodes.slice() });
    }
    tokens.forEach(function (token) {
      if (token.special) { detach(token.box); return; }
      // R3: detached when the box has geometry of its own (a) or wraps its own lines (b) — and only
      // when it shares the paragraph with other text or another box. Alone, it is glued to nothing,
      // and taking it out would only cost the paragraph (a list item's bullet, say) its text.
      var detached = !!token.box && (laysOutItems(token.box) ||
        (tokens.some(function (other) { return other !== token && said(other); }) &&
         (hasOwnGeometry(token.box) || wrapsOwnLines(token.box))));
      if (detached) { detach(token.box); return; }
      if (!token.inner) { keep(token.nodes); return; }
      // R1: the box stays in the paragraph, so each special inside it leaves on its own, where it sits.
      var pending = [], walked = null;
      token.leaves.forEach(function (leaf) {
        if (leaf.nodeType === 3) { pending.push(leaf); return; }
        var owner = specialOwner(leaf, token.box, hasText);
        if (owner === walked) return;          // two icons in one text-less span: one walk
        keep(pending);
        pending = [];
        walked = owner;
        detach(owner);
      });
      keep(pending);
    });
    return segments;
  }

  /** Is `el` a detached box, or inside one, below `paragraphEl`? */
  function insideDetached(el, paragraphEl) {
    for (var node = el; node && node !== paragraphEl; node = node.parentElement) {
      if (node.__engineDetached) return true;
    }
    return false;
  }

  // ------------------------------------------------------------------ bullets

  var BULLET_CHARS = { disc: "•", circle: "◦", square: "▪", "-": "-" };

  function bulletOf(li, cs, frame) {
    // The export draws a marker only where the browser draws one. `list-style: none` draws none
    // (measured: `::marker` content is `normal`); neither does an `li` that is not a list item
    // (`display: flex|block` — the legend and checklist idioms), nor one whose `::marker` content is
    // `none` or empty. Each would otherwise get a `•` beside its own `li::before` mark.
    if (cs.listStyleType === "none" || cs.display !== "list-item") return null;
    var marker = null;
    try { marker = getComputedStyle(li, "::marker"); } catch (error) { marker = null; }
    if (marker && /^(none|""|'')$/.test(String(marker.content || "").trim())) return null;
    var list = li.parentElement;
    if (!list || (list.tagName !== "UL" && list.tagName !== "OL")) return null;
    var level = -1;
    for (var node = li; node; node = node.parentElement) {
      if (node.tagName === "UL" || node.tagName === "OL") level++;
    }
    var listStyle = getComputedStyle(list);
    var ordered = list.tagName === "OL";
    var colour = parseColor((marker && marker.color) || cs.color, li) || { color: "000000" };
    var listBox = borderBox(list, frame);
    var listPadding = boxOf(listBox.x + px(listStyle.borderLeftWidth), listBox.y, listBox.w, listBox.h);
    var liContent = contentBox(borderBox(li, frame), cs);
    return {
      type: ordered ? "num" : "char",
      char: ordered ? null : (BULLET_CHARS[cs.listStyleType] || "•"),
      level: Math.max(0, level),
      indentPx: Math.max(0, liContent.x - listPadding.x),
      color: colour.color,
      start: ordered ? parseInt(list.getAttribute("start") || "1", 10) : 1,
      _listLeft: listPadding.x,
    };
  }

  // ------------------------------------------------------------------ images
  //
  // r3 (plan §16 #30): a picture shows exactly the part of its source the browser shows. The browser
  // gives, per image, the frame it paints in (an <img>'s content box — replaced content is clipped
  // there, measured; a background's painting area), the source's natural size, and the sizing and
  // position values it computed: `object-position` / `background-position` arrive as two
  // `<length-percentage>`s with every keyword and the offset syntax already resolved (`right bottom`
  // → `100% 100%`, `left 10px bottom 20px` → `10px calc(100% - 20px)`), and `lengthAgainst` lets the
  // browser itself do the arithmetic of whatever it computed. The whole source is drawn into one
  // rectangle (`drawn`); the record's `box` is the part of it the frame lets through and `crop` the
  // fractions of the source that cut hides — exactly a PowerPoint `a:srcRect`, so the emitter only
  // writes it (`shapes.add_image` fits nothing again once the crop is given).
  //
  // A frame with rounded corners keeps its own box instead: the corners clip the frame, not the
  // picture, and a side of the frame the source does not reach becomes a *negative* crop — the empty
  // band PowerPoint's own Crop → Fit leaves (both renderers draw nothing there, measured).

  /**
   * A rectangle on whole pixels, as Blink paints it: each edge rounded half up (`ToPixelSnappedRect`).
   * An <img>'s destination and content rects are both snapped before the source rect is mapped
   * (`ImagePainter::PaintIntoRect`) — measured on the coordinate-coded probe: a 400 px source covering
   * 180 × 140 shows 6.48–393.50 px of its width (snapped: 6.45–393.55; unsnapped 7.14–392.86).
   */
  function snappedBox(r) {
    var x = Math.floor(r.x + 0.5), y = Math.floor(r.y + 0.5);
    return boxOf(x, y, Math.floor(r.x + r.w + 0.5) - x, Math.floor(r.y + r.h + 0.5) - y);
  }

  /** A drawn rectangle cut by a frame: what shows, and the fractions of the source cut away. */
  function cutImage(drawn, frame, keepFrame) {
    var box = keepFrame ? frame : intersectBox(drawn, frame);
    function fraction(amount, extent) { return extent ? amount / extent : 0; }
    return {
      box: box,
      crop: {
        l: fraction(box.x - drawn.x, drawn.w),
        t: fraction(box.y - drawn.y, drawn.h),
        r: fraction(drawn.x + drawn.w - box.x - box.w, drawn.w),
        b: fraction(drawn.y + drawn.h - box.y - box.h, drawn.h),
      },
    };
  }

  var lengthProbe = null;

  /**
   * A computed `<length-percentage>` (`37%`, `12.5px`, `calc(100% - 20px)`) resolved against `basis`
   * px — by the browser: the probe's containing block is so many px wide and `left: value` is where the
   * browser puts its child. A position's basis is `frame − image`, negative when the image is larger
   * (cover), and a containing block cannot be; a length-percentage is affine in its basis, so the
   * browser resolves it at 0 and at 6400 px (whole LayoutUnits, so both are exact) and the line through
   * the two gives any basis. (A `min()`/`max()` inside a position is not affine; none is authored.)
   */
  function lengthAgainst(value, basis) {
    var text = String(value || "0px").trim();
    var plain = /^(-?\d*\.?\d+(?:e[-+]?\d+)?)(px|%)$/i.exec(text);
    if (plain) return plain[2] === "%" ? parseFloat(plain[1]) / 100 * basis : parseFloat(plain[1]);
    if (!lengthProbe) {
      lengthProbe = document.createElement("div");
      lengthProbe.setAttribute("data-engine-probe", "");
      lengthProbe.style.cssText = "position:absolute;left:0;top:0;height:0;margin:0;padding:0;border:0;" +
                                  "visibility:hidden;transform:none";
      lengthProbe.appendChild(document.createElement("div"));
      lengthProbe.firstChild.style.cssText = "position:absolute;top:0;width:0;height:0;margin:0;padding:0;border:0";
    }
    function at(width) {
      lengthProbe.style.width = width + "px";
      lengthProbe.firstChild.style.left = "0px";
      lengthProbe.firstChild.style.left = text;          // a value the browser refuses leaves 0
      document.body.appendChild(lengthProbe);
      var left = lengthProbe.firstChild.getBoundingClientRect().left - lengthProbe.getBoundingClientRect().left;
      document.body.removeChild(lengthProbe);
      return left;
    }
    var zero = at(0);
    return zero + (at(6400) - zero) * basis / 6400;
  }

  /** The two components of a computed `<position>` or `background-size` (first layer). */
  function pairOf(value, fallback) {
    var parts = splitTop(splitTop(String(value || fallback), ",")[0] || fallback, " ");
    return [parts[0] || fallback, parts[1] || parts[0] || fallback];
  }

  /** How big the browser draws a natural w × h source for `object-fit` in an area. */
  function objectFitSize(fit, area, w, h) {
    var scale;
    if (fit === "cover") scale = Math.max(area.w / w, area.h / h);
    else if (fit === "contain") scale = Math.min(area.w / w, area.h / h);
    else if (fit === "none") scale = 1;
    else if (fit === "scale-down") scale = Math.min(1, area.w / w, area.h / h);
    else return { w: area.w, h: area.h };                     // `fill`: stretched to the area
    return { w: w * scale, h: h * scale };
  }

  /** `background-size` of a natural source over a positioning area: cover, contain, lengths, auto. */
  function backgroundDrawnSize(value, area, natural) {
    var text = splitTop(String(value || "auto"), ",")[0] || "auto";
    if (text === "cover" || text === "contain") {
      return natural && natural.w && natural.h ? objectFitSize(text, area, natural.w, natural.h)
                                               : { w: area.w, h: area.h };
    }
    var parts = pairOf(text, "auto");
    if (splitTop(text, " ").length === 1) parts[1] = "auto";   // `120px` is `120px auto`
    var w = parts[0] === "auto" ? null : lengthAgainst(parts[0], area.w);
    var h = parts[1] === "auto" ? null : lengthAgainst(parts[1], area.h);
    if (!natural || !natural.w || !natural.h) return { w: w === null ? area.w : w, h: h === null ? area.h : h };
    if (w === null && h === null) return { w: natural.w, h: natural.h };
    if (w === null) return { w: h * natural.w / natural.h, h: h };
    if (h === null) return { w: w, h: w * natural.h / natural.w };
    return { w: w, h: h };
  }

  /** Where `object-position` / `background-position` puts a drawn size inside its area. */
  function positionedRect(value, area, size) {
    var parts = pairOf(value, "50%");
    return boxOf(area.x + lengthAgainst(parts[0], area.w - size.w),
                 area.y + lengthAgainst(parts[1], area.h - size.h), size.w, size.h);
  }

  /** The one radius the IR carries for a frame inset from the border box by `inset` px. */
  function frameRadius(cs, b, inset) {
    var radius = radiusOf(cs, b);
    return Math.max(0, Math.max(radius.tl, radius.tr, radius.br, radius.bl) - inset);
  }

  function imageRecord(el, cs, ctx, state) {
    var b = borderBox(el, ctx.frame);
    var area = contentBox(b, cs);
    var fit = cs.objectFit || "fill";
    var w = el.naturalWidth, h = el.naturalHeight;
    var drawn = snappedBox(w && h && area.w && area.h && fit !== "fill"
      ? positionedRect(cs.objectPosition, area, objectFitSize(fit, area, w, h)) : area);
    // The replaced content is clipped to the content box, whose corners follow the border box's
    // radius less the border and padding (measured); the IR has one radius for the frame.
    var radius = frameRadius(cs, b, Math.min(area.x - b.x, area.y - b.y));
    var circle = radius > 0 && radius >= Math.min(area.w, area.h) / 2 - 0.5;
    var cut = cutImage(drawn, snappedBox(area), radius > 0);
    return {
      kind: "image",
      box: paintedBox(ctx.frame, cut.box),
      url: el.currentSrc || el.getAttribute("src") || null,
      naturalW: w,
      naturalH: h,
      fit: fit === "none" || fit === "scale-down" ? "contain" : fit,
      crop: cut.crop,
      radius: radius,
      circle: circle,
      complete: !!el.complete && w > 0,
      selector: markCapture(el, state),
    };
  }

  /** `background-repeat` of the first layer, per axis: repeat | no-repeat | space | round. */
  function repeatModes(value) {
    var parts = splitTop(splitTop(String(value || "repeat"), ",")[0] || "repeat", " ");
    if (parts.length > 1) return parts;
    if (parts[0] === "repeat-x") return ["repeat", "no-repeat"];
    if (parts[0] === "repeat-y") return ["no-repeat", "repeat"];
    return [parts[0], parts[0]];
  }

  /**
   * The tiles one axis draws across the painting area [paintStart, paintEnd), as `origin + k·period`
   * for `count` values of k from `first`: one tile where it is `no-repeat`; a whole-tile period
   * anchored at the positioned tile for `repeat` (and `round`, whose tile size was already rounded);
   * for `space` as many whole tiles as fit the positioning area, the first and last touching its edges
   * — or one positioned tile when fewer than two fit. Counted, not listed: a 1 px texture is a million.
   */
  function tileAxis(mode, anchor, size, areaStart, areaSize, paintStart, paintEnd) {
    var one = { origin: anchor, period: 0, first: 0, count: 1 };
    if (mode === "no-repeat" || !(size > 0)) return one;
    var origin = anchor, period = size;
    if (mode === "space") {
      var fits = Math.floor(areaSize / size + 1e-6);
      if (fits < 2) return one;
      origin = areaStart;
      period = size + (areaSize - fits * size) / (fits - 1);
    }
    var first = Math.floor((paintStart - origin) / period + 1e-6);
    var last = Math.ceil((paintEnd - origin) / period - 1e-6) - 1;
    return { origin: origin, period: period, first: first, count: Math.max(1, last - first + 1) };
  }

  function tileStarts(axis) {
    var starts = [];
    for (var k = 0; k < axis.count; k++) starts.push(axis.origin + (axis.first + k) * axis.period);
    return starts;
  }

  // More tiles than this are one captured picture of the painted background, not one picture each
  // (a 4 px dot texture over a slide would otherwise be tens of thousands of shapes).
  var MAX_BACKGROUND_TILES = 64;

  /**
   * A `background-image: url(...)` layer: the picture(s) behind the box's own decoration. One record
   * for the layer; `tiles` holds one record per visible tile when `background-repeat` draws more than
   * one, and `capture` marks a layer the export takes as the browser's own pixels instead (too many
   * tiles, or tiles under rounded corners), with its diagnostic.
   */
  function backgroundImageRecord(el, cs, ctx, state) {
    var image = cs.backgroundImage;
    if (!image || image === "none") return null;
    var layer = splitTop(image, ",")[0];
    var match = layer.match(/^url\((['"]?)(.*?)\1\)$/i);
    if (!match) return null;
    var b = borderBox(el, ctx.frame);
    var natural = state.backgroundSizes[match[2]];
    var origin = splitTop(String(cs.backgroundOrigin || "padding-box"), ",")[0];
    var area = origin === "border-box" ? b : origin === "content-box" ? contentBox(b, cs) : paddingBox(b, cs);
    if (splitTop(String(cs.backgroundAttachment || "scroll"), ",")[0] === "fixed" && !ctx.frame.angle) {
      area = boxOf(-(ctx.frame.dx || 0), -(ctx.frame.dy || 0), window.innerWidth, window.innerHeight);
    }
    // The painting area. `border-box` paints under the border too, but the export draws the border
    // (the decoration shape) *before* this picture, so a picture under a side that paints would cover
    // it: the picture stops at the padding edge of every side that paints anything (an opaque side
    // hides what is under it anyway; a translucent or dashed one keeps its own look and loses the strip
    // of picture it shows through), and runs under a side that paints nothing (`transparent`, `none`).
    var clipTo = splitTop(String(cs.backgroundClip || "border-box"), ",")[0];
    var paint = clipTo === "content-box" ? contentBox(b, cs) : clipTo === "padding-box" ? paddingBox(b, cs) : b;
    if (clipTo !== "content-box" && clipTo !== "padding-box") {
      var sides = sideBorders(cs);
      var painted = function (side) { return side ? side.width : 0; };
      paint = insetBox(b, painted(sides.top), painted(sides.right), painted(sides.bottom), painted(sides.left));
    }
    var inset = Math.min(paint.x - b.x, paint.y - b.y);
    var radius = frameRadius(cs, b, inset);
    // Blink clips a background to its pixel-snapped destination rect; the tiles keep their fractional
    // size and phase (measured: a cover tile 1.667 px left of its frame shows from source px 3.57,
    // unsnapped), but a no-repeat tile that starts inside the painting area is that rect's start.
    paint = snappedBox(paint);

    var size = backgroundDrawnSize(cs.backgroundSize, area, natural);
    var modes = repeatModes(cs.backgroundRepeat);
    var sizeParts = splitTop(splitTop(String(cs.backgroundSize || "auto"), ",")[0] || "auto", " ");
    if (modes[0] === "round" && size.w > 0) {
      var roundW = area.w / Math.max(1, Math.round(area.w / size.w));
      if (modes[1] !== "round" && (sizeParts[1] || "auto") === "auto" && sizeParts[0] !== "cover" &&
          sizeParts[0] !== "contain") size.h *= roundW / size.w;
      size.w = roundW;
    }
    if (modes[1] === "round" && size.h > 0) {
      var roundH = area.h / Math.max(1, Math.round(area.h / size.h));
      if (modes[0] !== "round" && sizeParts[0] === "auto") size.w *= roundH / size.h;
      size.h = roundH;
    }
    var anchor = positionedRect(cs.backgroundPosition, area, size);
    var ax = tileAxis(modes[0], anchor.x, size.w, area.x, area.w, paint.x, paint.x + paint.w);
    var ay = tileAxis(modes[1], anchor.y, size.h, area.y, area.h, paint.y, paint.y + paint.h);
    var many = ax.count * ay.count > MAX_BACKGROUND_TILES;
    var tiles = [];
    if (!many) {
      tileStarts(ay).forEach(function (y) {
        tileStarts(ax).forEach(function (x) {
          if (modes[0] === "no-repeat" && x > paint.x) x = Math.floor(x + 0.5);
          if (modes[1] === "no-repeat" && y > paint.y) y = Math.floor(y + 0.5);
          var tile = boxOf(x, y, size.w, size.h);
          var shown = intersectBox(tile, paint);
          if (shown.w > 0 && shown.h > 0) tiles.push(tile);
        });
      });
      if (!tiles.length) tiles.push(anchor);         // nothing shows: dropped as smaller than 0.5 px
    }
    var single = !many && tiles.length === 1;
    var cut = cutImage(single ? tiles[0] : anchor, paint, single && radius > 0);
    var covered = many
      ? intersectBox(boxOf(ax.origin + ax.first * ax.period, ay.origin + ay.first * ay.period,
                           (ax.count - 1) * ax.period + size.w, (ay.count - 1) * ay.period + size.h), paint)
      : tiles.reduce(function (all, tile) {
        var shown = intersectBox(tile, paint);
        return all ? boxOf(Math.min(all.x, shown.x), Math.min(all.y, shown.y),
                           Math.max(all.x + all.w, shown.x + shown.w) - Math.min(all.x, shown.x),
                           Math.max(all.y + all.h, shown.y + shown.h) - Math.min(all.y, shown.y)) : shown;
      }, null);
    var record = {
      kind: "image",
      box: paintedBox(ctx.frame, single ? cut.box : covered),
      url: match[2],
      naturalW: natural ? natural.w : 0,
      naturalH: natural ? natural.h : 0,
      fit: sizeParts[0] === "cover" ? "cover" : sizeParts[0] === "contain" ? "contain" : "fill",
      crop: single ? cut.crop : { l: 0, t: 0, r: 0, b: 0 },
      radius: single ? radius : 0,
      circle: single && radius > 0 && radius >= Math.min(paint.w, paint.h) / 2 - 0.5,
      complete: !!natural,
      background: true,
      selector: markCapture(el, state),
    };
    if (single) return record;
    if (many || radius > 0) {
      record.capture = many ? "background-repeat draws " + ax.count * ay.count + " tiles"
                            : "background-repeat draws " + tiles.length + " tiles under rounded corners";
      return record;
    }
    record.tiles = tiles.map(function (tile) {
      var piece = cutImage(tile, paint, false);
      var copy = {};
      for (var key in record) if (key !== "tiles") copy[key] = record[key];
      copy.box = paintedBox(ctx.frame, piece.box);
      copy.crop = piece.crop;
      return copy;
    });
    return record;
  }

  /** Push a background layer: its one picture, one picture per tile, or one captured picture. */
  function pushBackground(state, record, el, ctx) {
    if (record.tiles && !record.remote) {
      record.tiles.forEach(function (tile) { push(state, tile, el, ctx); });
      return;
    }
    push(state, record, el, ctx);
    if (record.capture && !record.remote) {
      diagnose(state, "warn", record.source.path, "rasterised: " + record.capture + " on " +
               record.source.path + "; exported as one picture of the painted background",
               state.records.length - 1);
    }
  }

  // ------------------------------------------------------------------ tables
  //
  // A <table> becomes one native, editable `a:tbl` that looks like the design (WP-A,
  // docs/archive/engine/fidelity/10-WPA-tables.md). The rule:
  //
  //   every glyph the cell owns lives in the cell's text frame, unless a painted box owns it;
  //   every non-glyph paint the cell owns is an ordinary IR element drawn above the native table;
  //   every fill the CSS paints under a cell is the cell's fill, and a fill the native cell cannot
  //   express (a gradient) is a shape behind the table.
  //
  // What the frozen cell fields cannot say — where the grid line is relative to the padding box,
  // first-line indents, which overlay belongs to which cell — travels in `extras["x-wpa"]`.
  //
  // Measured on Edge before this was written (probes (a)–(c) of the brief, 2026-09-25):
  //  (a) Chromium's table rect is the wrapper box and includes the <caption> and its margin; the
  //      first row starts at captionBottom + marginBottom.
  //  (b) With `border-collapse: collapse`, `getComputedStyle(td)` reports the cell's *own* border
  //      (0 px under a `tr{border-bottom}`), never the collapsed winner — so the winner is resolved
  //      here (CSS 2.1 §17.6.2.1). A collapsed cell's rect runs from the centre of one grid line to
  //      the next, and its content starts half the winning border further in: a 5 px row rule puts
  //      the next row's text 2.5 px + padding below the cell's top.
  //  (c) `getComputedStyle(col).backgroundColor` resolves, and a <col> reports its column's rect.
  // Also measured: a `tr` gradient is painted across the whole row (it does not restart per cell),
  // and a row's background covers the full height of a rowspan cell originating in it.

  /** A block coincides with the cell's padding box when every edge is within this (px). */
  var TABLE_FOLD_EPS = 1;
  /** Two paragraphs with lines that overlap by more than this share of the shorter sit side by side. */
  var TABLE_BAND_SHARE = 0.5;
  /** A first-line indent smaller than this is rounding, not an inline box's doing (px). */
  var TABLE_INDENT_MIN_PX = 0.5;

  /** Paint tiers inside a cell, after CSS 2.1 Appendix E: block backgrounds, floats, inline
   *  content (an atomic inline box paints as one unit), text, then positioned descendants. */
  var TIER_FIRST = 0, TIER_BLOCK = 1, TIER_FLOAT = 2, TIER_INLINE = 3, TIER_TEXT = 4, TIER_POSITIONED = 5;

  /** Lines of one cell paragraph whose anchors (start, centre or end) agree within this share one
   *  `marL`/`marR`/`indent`; a line that does not starts a paragraph of its own (px). */
  var TABLE_PLACE_TOL_PX = 1;

  /**
   * The `<table>` branch of the walk (brief §4.1), in paint order: the table's own surface behind
   * it, the gradient cells behind it, the caption, the native table, then everything its cells
   * paint on top of it. Every record but the table itself carries `group: null` — a
   * `graphicFrame` cannot live in a group, so the emitter always puts the table on the slide, and
   * an overlay left in the table's group would land in a `grpSp` *under* it.
   */
  function emitTable(el, cs, ctx, state) {
    var model = tableModel(el, cs, ctx, state);
    var overlayCtx = { opacity: ctx.opacity, clip: ctx.clip, group: null, frame: ctx.frame };
    emitTableSurface(model, overlayCtx, state);
    emitCellGradients(model, overlayCtx, state);
    if (model.caption) {
      var from = state.records.length;
      walkElement(model.caption, overlayCtx, state);
      stampOverlays(state, from, model, null, "caption", false);
    }
    var record = push(state, tableRecord(model), el, ctx);
    var index = state.records.length - 1;
    model.cells.forEach(function (entry) { reportCellText(model, entry, record, index, ctx, state); });
    walkCellOverlays(model, record, overlayCtx, state);
    model.infos.forEach(function (message) { diagnose(state, "info", record.source.path, message, index); });
    model.warns.forEach(function (message) { diagnose(state, "warn", record.source.path, message, index); });
  }

  // ---- the grid

  /** The table's own rows, minus hidden ones, in the order the browser drew them (brief §4.2). */
  function tableRows(el, frame) {
    var rows = [], all = el.querySelectorAll("tr");
    for (var i = 0; i < all.length; i++) {
      var tr = all[i];
      if (tr.closest("table") !== el) continue;
      var trs = getComputedStyle(tr);
      if (trs.display === "none" || trs.visibility === "collapse" || !tr.getClientRects().length) continue;
      rows.push({ el: tr, order: rows.length, top: borderBox(tr, frame).y });
    }
    // A <tfoot> written before <tbody> is rendered last: order by where the row is, not by source.
    rows.sort(function (a, b) { return a.top - b.top || a.order - b.order; });
    return rows.map(function (row) { return row.el; });
  }

  /** A rowspan is clipped to its row group; `rowspan="0"` runs to the group's end (HTML table model). */
  function spannedRows(cell, rows, r) {
    var group = rows[r].parentElement, left = 0;
    for (var i = r; i < rows.length && rows[i].parentElement === group; i++) left++;
    if (cell.rowSpan === 0) return left;
    return Math.max(1, Math.min(cell.rowSpan || 1, left));
  }

  /** Occupancy grid: a spanning cell reserves the slots below and to the right of itself. */
  function tableCells(rows) {
    var occupant = {}, cells = [], columnCount = 0;
    rows.forEach(function (row, r) {
      var c = 0;
      for (var i = 0; i < row.children.length; i++) {
        var cell = row.children[i];
        if (cell.tagName !== "TD" && cell.tagName !== "TH") continue;
        if (getComputedStyle(cell).display === "none") continue;
        while (occupant[r + ":" + c]) c++;
        var entry = { element: cell, r: r, c: c, rowSpan: spannedRows(cell, rows, r),
                      colSpan: Math.max(1, cell.colSpan || 1) };
        for (var dr = 0; dr < entry.rowSpan; dr++) {
          for (var dc = 0; dc < entry.colSpan; dc++) occupant[(r + dr) + ":" + (c + dc)] = entry;
        }
        cells.push(entry);
        c += entry.colSpan;
        columnCount = Math.max(columnCount, c);
      }
    });
    return { cells: cells, occupant: occupant, cols: columnCount };
  }

  /** Per grid column, the `<col>` and `<colgroup>` that cover it (a `span` covers several). */
  function tableColumns(el) {
    var columns = [], groups = el.querySelectorAll("colgroup");
    for (var g = 0; g < groups.length; g++) {
      var group = groups[g];
      if (group.closest("table") !== el) continue;
      var cols = [];
      for (var i = 0; i < group.children.length; i++) {
        if (group.children[i].tagName === "COL") cols.push(group.children[i]);
      }
      if (!cols.length) {
        for (var s = 0; s < Math.max(1, group.span || 1); s++) columns.push({ col: null, group: group, first: s === 0 });
        continue;
      }
      cols.forEach(function (col) {
        for (var k = 0; k < Math.max(1, col.span || 1); k++) columns.push({ col: col, group: group, first: k === 0 });
      });
    }
    return columns;
  }

  /**
   * Where the native table goes. The table rect includes the caption (probe (a)), so the caption's
   * margin box comes off on its side. In collapse mode the grid is the cells' union: the rect adds
   * half of each outer border beyond the grid lines, and PowerPoint strokes a cell border centred on
   * the cell edge — so the union puts every rule, the outer ones included, where the browser drew it.
   */
  function gridBoxOf(el, cs, frame, cells, collapse) {
    var table = borderBox(el, frame);
    var caption = el.caption && getComputedStyle(el.caption).display !== "none" &&
                  el.caption.getClientRects().length ? el.caption : null;
    if (caption) {
      var ccs = getComputedStyle(caption), cb = borderBox(caption, frame);
      var above = cb.y + cb.h / 2 < table.y + table.h / 2;
      if (above) {
        var bottom = cb.y + cb.h + px(ccs.marginBottom);
        table = boxOf(table.x, bottom, table.w, Math.max(0, table.y + table.h - bottom));
      } else {
        var top = cb.y - px(ccs.marginTop);
        table = boxOf(table.x, table.y, table.w, Math.max(0, top - table.y));
      }
    }
    var grid = table;
    if (collapse && cells.length) {
      grid = null;
      cells.forEach(function (entry) { grid = unionBox(grid, entry.box); });
    }
    return { table: table, grid: grid, caption: caption };
  }

  /** Linear fill between measured neighbours, for grid lines no single-span cell or <col> measured. */
  function fillGridLines(lines) {
    var last = lines.length - 1;
    for (var i = 1; i < last; i++) {
      if (lines[i] !== undefined) continue;
      var next = i;
      while (lines[next] === undefined) next++;
      var step = (lines[next] - lines[i - 1]) / (next - i + 1);
      for (var j = i; j < next; j++) lines[j] = lines[i - 1] + step * (j - i + 1);
    }
    for (var k = 1; k <= last; k++) lines[k] = Math.max(lines[k], lines[k - 1]);
    return lines;
  }

  /**
   * Grid lines, row heights, column widths and each cell's slot (brief §4.2). `rowTop` / `colLeft`
   * come from the single-span cells, pinned to the grid box at both ends, so the heights and widths
   * add up to the box exactly. A cell's `slot` is everything between its grid lines and its padding
   * box — `border-spacing`, the table's own border and padding on the outer cells, and the cell's
   * used border (half the collapsed winner, probe (b)) — which the emitter adds to the margins.
   */
  function slotGeometry(model) {
    var rowTop = new Array(model.rows.length + 1), colLeft = new Array(model.cols + 1);
    model.cells.forEach(function (entry) {
      if (entry.rowSpan === 1 && (rowTop[entry.r] === undefined || entry.box.y < rowTop[entry.r])) rowTop[entry.r] = entry.box.y;
      if (entry.colSpan === 1 && (colLeft[entry.c] === undefined || entry.box.x < colLeft[entry.c])) colLeft[entry.c] = entry.box.x;
    });
    model.rows.forEach(function (row, r) { if (rowTop[r] === undefined) rowTop[r] = borderBox(row, model.frame).y; });
    model.columns.forEach(function (column, c) {
      if (c < model.cols && colLeft[c] === undefined && column.col && column.first && (column.col.span || 1) === 1) {
        colLeft[c] = borderBox(column.col, model.frame).x;
      }
    });
    rowTop[0] = model.grid.y;
    rowTop[model.rows.length] = model.grid.y + model.grid.h;
    colLeft[0] = model.grid.x;
    colLeft[model.cols] = model.grid.x + model.grid.w;
    model.rowTop = fillGridLines(rowTop);
    model.colLeft = fillGridLines(colLeft);
    model.rowHeightsPx = model.rows.map(function (row, r) { return model.rowTop[r + 1] - model.rowTop[r]; });
    model.colWidthsPx = [];
    for (var c = 0; c < model.cols; c++) model.colWidthsPx.push(model.colLeft[c + 1] - model.colLeft[c]);
    model.cells.forEach(function (entry) {
      var b = entry.box, used = entry.used;
      entry.slotBox = boxOf(model.colLeft[entry.c], model.rowTop[entry.r],
                            model.colLeft[entry.c + entry.colSpan] - model.colLeft[entry.c],
                            model.rowTop[entry.r + entry.rowSpan] - model.rowTop[entry.r]);
      entry.slot = {
        t: Math.max(0, b.y - entry.slotBox.y + used.t),
        r: Math.max(0, entry.slotBox.x + entry.slotBox.w - (b.x + b.w) + used.r),
        b: Math.max(0, entry.slotBox.y + entry.slotBox.h - (b.y + b.h) + used.b),
        l: Math.max(0, b.x - entry.slotBox.x + used.l),
      };
      entry.paddingBox = insetBox(b, used.t, used.r, used.b, used.l);
      entry.contentBox = insetBox(entry.paddingBox, px(entry.cs.paddingTop), px(entry.cs.paddingRight),
                                  px(entry.cs.paddingBottom), px(entry.cs.paddingLeft));
    });
  }

  // ---- border-spacing: spacer rows and columns

  /** PowerPoint's floor for a row height and a column width: 2 pt. Measured through COM on
   *  2026-09-29 (Office 16): an empty row or column declared 0.5, 1, 1.5 or 2 px is laid out
   *  2.667 px, one declared 3 or 4 px keeps its size — whatever the empty cell's font size (1 pt),
   *  line spacing or margins (0). */
  var TABLE_MIN_TRACK_PX = 8 / 3;

  /** Does a cell paint anything a gap beside it would show — a fill, a gradient or a border? */
  function cellPaints(entry) {
    var b = entry.borders || {};
    return !!((entry.fill && entry.fill.type === "solid" && entry.fill.alpha > 0) || entry.gradient ||
              b.top || b.right || b.bottom || b.left);
  }

  /**
   * One axis of the spacer grid. `axis` says how to read it: `count` tracks, `lines` the logical grid
   * lines (`slotGeometry`'s), `start`/`span` of a cell, `lo`/`hi` of a box, the `near`/`far` border
   * sides. A band is where the browser painted a track's cells; between two bands lies the spacing.
   *
   * Returns the physical lines, which physical tracks are spacers, where each logical track went
   * (`map`), and the gaps it widened to PowerPoint's floor or had to close.
   */
  function planAxis(model, axis) {
    var n = axis.count, cells = model.cells, bands = [];
    for (var i = 0; i < n; i++) bands.push({ lo: undefined, hi: undefined });
    cells.forEach(function (entry) {
      var first = bands[axis.start(entry)], last = bands[axis.start(entry) + axis.span(entry) - 1];
      if (first.lo === undefined || axis.lo(entry.box) < first.lo) first.lo = axis.lo(entry.box);
      if (last.hi === undefined || axis.hi(entry.box) > last.hi) last.hi = axis.hi(entry.box);
    });
    bands.forEach(function (band, k) {
      if (band.lo === undefined) band.lo = axis.fallbackLo(k);
      if (band.hi === undefined) band.hi = axis.fallbackHi(k);
    });
    function edgeCells(k, side) {
      return cells.filter(function (entry) {
        return side === "lo" ? axis.start(entry) === k : axis.start(entry) + axis.span(entry) - 1 === k;
      });
    }
    function paints(k, side) { return edgeCells(k, side).some(cellPaints); }
    // PowerPoint strokes a cell border centred on its grid line: the line goes half the widest
    // border on that edge inside the band, so the stroke covers the pixels the browser's did.
    function inset(k, side) {
      var width = 0;
      edgeCells(k, side).forEach(function (entry) {
        var border = entry.borders[side === "lo" ? axis.near : axis.far];
        if (border) width = Math.max(width, border.width);
      });
      return width / 2;
    }
    var out = { lines: [], spacer: [], map: [], widened: [], closed: [], changed: false };
    var start = paints(0, "lo") ? bands[0].lo + inset(0, "lo") : axis.lines[0];
    out.lines.push(start);
    for (var k = 0; k < n; k++) {
      out.map.push(out.spacer.length);
      out.spacer.push(false);
      var previous = out.lines[out.lines.length - 1];
      if (k === n - 1) {
        out.lines.push(Math.max(previous, paints(k, "hi") ? bands[k].hi - inset(k, "hi") : axis.lines[n]));
        break;
      }
      var gap = bands[k + 1].lo - bands[k].hi, visible = paints(k, "hi") || paints(k + 1, "lo");
      var a = bands[k].hi - inset(k, "hi"), b = bands[k + 1].lo + inset(k + 1, "lo");
      // Widening the spacer track to the floor misdraws (floor − track) px of the gap, closing it
      // `gap` px: the smaller error wins (with unbordered cells, a gap of 1 pt or more is kept).
      if (visible && gap > 0 && TABLE_MIN_TRACK_PX - (b - a) <= gap) {
        if (b - a < TABLE_MIN_TRACK_PX) {
          var middle = (a + b) / 2;
          a = middle - TABLE_MIN_TRACK_PX / 2;
          b = middle + TABLE_MIN_TRACK_PX / 2;
          out.widened.push(gap);
        }
        a = Math.max(previous, a);
        out.lines.push(a);
        out.spacer.push(true);
        out.lines.push(Math.max(a, b));
      } else {
        if (visible && gap > 0.01) out.closed.push(gap);
        out.lines.push(Math.max(previous, axis.lines[k + 1]));
      }
    }
    out.changed = out.spacer.length !== n || out.lines[0] !== axis.lines[0] || out.lines[out.lines.length - 1] !== axis.lines[n];
    return out;
  }

  /** "2 px", "2 and 3 px" — the distinct gaps, rounded to 0.01 px. */
  function gapWords(gaps) {
    var seen = [];
    gaps.forEach(function (gap) {
      var g = Math.round(gap * 100) / 100;
      if (seen.indexOf(g) < 0) seen.push(g);
    });
    seen.sort(function (a, b) { return a - b; });
    return (seen.length > 1 ? seen.slice(0, -1).join(", ") + " and " + seen[seen.length - 1] : String(seen[0])) + " px";
  }

  /**
   * `border-collapse: separate` with a `border-spacing`: a PowerPoint table has no cell spacing, so
   * every gap between cells that paint (a fill, a gradient, a border) becomes an empty, unfilled,
   * borderless **spacer** row or column of the native table, and the table's edge moves to the
   * painted cells' edge. Each cell's rect then lands where the browser drew it, and what shows
   * through the gaps is what the browser shows there: the table's surface (its background, drawn
   * behind the table), or the slide. A merged cell spans the spacers between its parts.
   *
   * `entry.pr`/`pc`/`prSpan`/`pcSpan` are the cell's physical place in the native table (the logical
   * `r`/`c` stay the HTML's, for the model and its messages), `model.slots` the physical occupancy.
   * A table without spacing, with nothing painted, or in collapse mode keeps the identity — byte for
   * byte what it was. Gaps between cells that paint nothing are invisible and stay absorbed in the
   * cells' insets, as before. A visible gap whose track is under PowerPoint's 2 pt floor is widened
   * to it, taken evenly from the two cells beside it (info) — unless that would misdraw more than
   * closing it does (under 1 pt between unbordered cells): then it is closed (warn).
   */
  function spacerGrid(model) {
    var R = model.rows.length, C = model.cols;
    model.cells.forEach(function (entry) {
      entry.pr = entry.r; entry.pc = entry.c; entry.prSpan = entry.rowSpan; entry.pcSpan = entry.colSpan;
    });
    model.slots = model.occupant;
    model.spacerCells = [];
    model.spacers = null;
    if (model.collapse || !(model.spacing.h > 0 || model.spacing.v > 0) || !R || !C) return;
    if (!model.cells.some(cellPaints)) return;
    var rows = planAxis(model, {
      count: R, lines: model.rowTop, near: "top", far: "bottom",
      start: function (entry) { return entry.r; }, span: function (entry) { return entry.rowSpan; },
      lo: function (b) { return b.y; }, hi: function (b) { return b.y + b.h; },
      fallbackLo: function (k) { return borderBox(model.rows[k], model.frame).y; },
      fallbackHi: function (k) { var b = borderBox(model.rows[k], model.frame); return b.y + b.h; },
    });
    var cols = planAxis(model, {
      count: C, lines: model.colLeft, near: "left", far: "right",
      start: function (entry) { return entry.c; }, span: function (entry) { return entry.colSpan; },
      lo: function (b) { return b.x; }, hi: function (b) { return b.x + b.w; },
      fallbackLo: function (k) { return model.colLeft[k]; },
      fallbackHi: function (k) { return k + 1 < C ? model.colLeft[k + 1] - model.spacing.h : model.colLeft[C]; },
    });
    var closed = rows.closed.concat(cols.closed), widened = rows.widened.concat(cols.widened);
    if (closed.length) {
      model.warns.push("border-spacing " + gapWords(closed) + " between painted cells is narrower than 1 pt: " +
                       "PowerPoint draws no row or column under 2 pt, so the cell fills close the gap " +
                       "(use a spacing of 2 px or more, or border-collapse with a border in the gap's colour)");
    }
    if (!rows.changed && !cols.changed) return;

    model.rowTop = rows.lines;
    model.colLeft = cols.lines;
    model.rowHeightsPx = [];
    model.colWidthsPx = [];
    for (var i = 0; i + 1 < rows.lines.length; i++) model.rowHeightsPx.push(rows.lines[i + 1] - rows.lines[i]);
    for (var j = 0; j + 1 < cols.lines.length; j++) model.colWidthsPx.push(cols.lines[j + 1] - cols.lines[j]);
    model.grid = boxOf(cols.lines[0], rows.lines[0], cols.lines[cols.lines.length - 1] - cols.lines[0],
                       rows.lines[rows.lines.length - 1] - rows.lines[0]);
    model.slots = {};
    model.cells.forEach(function (entry) {
      entry.pr = rows.map[entry.r];
      entry.prSpan = rows.map[entry.r + entry.rowSpan - 1] - entry.pr + 1;
      entry.pc = cols.map[entry.c];
      entry.pcSpan = cols.map[entry.c + entry.colSpan - 1] - entry.pc + 1;
      for (var dr = 0; dr < entry.prSpan; dr++) {
        for (var dc = 0; dc < entry.pcSpan; dc++) model.slots[(entry.pr + dr) + ":" + (entry.pc + dc)] = entry;
      }
      var s = boxOf(cols.lines[entry.pc], rows.lines[entry.pr],
                    cols.lines[entry.pc + entry.pcSpan] - cols.lines[entry.pc],
                    rows.lines[entry.pr + entry.prSpan] - rows.lines[entry.pr]);
      var content = entry.contentBox, cs = entry.cs;
      entry.slotBox = s;
      // What lies between the physical grid lines and the padding box (a folded block's included).
      entry.slot = {
        t: Math.max(0, content.y - s.y - px(cs.paddingTop)),
        r: Math.max(0, s.x + s.w - (content.x + content.w) - px(cs.paddingRight)),
        b: Math.max(0, s.y + s.h - (content.y + content.h) - px(cs.paddingBottom)),
        l: Math.max(0, content.x - s.x - px(cs.paddingLeft)),
      };
      if (entry.gradientSource) {
        entry.gradient = remapGradient(entry.gradientSource.fill, entry.gradientSource.box, entry.slotBox);
      }
    });
    model.spacers = { rows: [], cols: [] };
    rows.spacer.forEach(function (spacer, k) { if (spacer) model.spacers.rows.push(k); });
    cols.spacer.forEach(function (spacer, k) { if (spacer) model.spacers.cols.push(k); });
    for (i = 0; i < rows.spacer.length; i++) {
      for (j = 0; j < cols.spacer.length; j++) {
        if (!(rows.spacer[i] || cols.spacer[j]) || model.slots[i + ":" + j]) continue;
        var cell = { spacer: true, pr: i, pc: j, prSpan: 1, pcSpan: 1,
                     borders: { top: null, right: null, bottom: null, left: null } };
        model.slots[i + ":" + j] = cell;
        model.spacerCells.push(cell);
      }
    }
    if (model.spacers.rows.length || model.spacers.cols.length) {
      model.infos.push("border-spacing " + model.spacing.h + (model.spacing.v !== model.spacing.h ? " " + model.spacing.v : "") +
                       " px: the gaps between painted cells are " + model.spacers.rows.length + " spacer row(s) and " +
                       model.spacers.cols.length + " spacer column(s) of the native table (empty, unfilled)");
    }
    if (widened.length) {
      model.infos.push("border-spacing " + gapWords(widened) + " is drawn " + gapWords([TABLE_MIN_TRACK_PX]) +
                       " wide: PowerPoint draws no row or column under 2 pt, and the cells beside the gap give up the difference");
    }
  }

  // ---- borders

  var BORDER_STYLE_RANK = { double: 8, solid: 7, dashed: 6, dotted: 5, ridge: 4, outset: 3, groove: 2, inset: 1 };
  var SIDE_NAMES = { top: "Top", right: "Right", bottom: "Bottom", left: "Left" };

  /** One candidate for a collapsed edge: the side of `el`, ranked by origin (cell 0 … table 5). */
  function edgeCandidate(el, side, origin, position) {
    if (!el) return null;
    var ecs = getComputedStyle(el), name = SIDE_NAMES[side];
    var style = ecs["border" + name + "Style"];
    if (!style || style === "none") return null;
    return { style: style, width: px(ecs["border" + name + "Width"]),
             colour: parseColor(ecs["border" + name + "Color"], el), origin: origin, position: position };
  }

  /** CSS 2.1 §17.6.2.1: hidden wins; then the widest; then the style rank; then the nearer origin
   *  (cell, row, row group, column, column group, table); then the one further up / left. */
  function strongerEdge(a, b) {
    if (!a) return b;
    if (!b) return a;
    if (a.style === "hidden") return a;
    if (b.style === "hidden") return b;
    if (a.width !== b.width) return a.width > b.width ? a : b;
    var ra = BORDER_STYLE_RANK[a.style] || 0, rb = BORDER_STYLE_RANK[b.style] || 0;
    if (ra !== rb) return ra > rb ? a : b;
    if (a.origin !== b.origin) return a.origin < b.origin ? a : b;
    return a.position <= b.position ? a : b;
  }

  /** The IR's border for a winning candidate — null when it paints nothing. */
  function edgeBorder(winner) {
    if (!winner || winner.style === "hidden" || winner.width <= 0 || !winner.colour || winner.colour.alpha <= 0) return null;
    return { width: winner.width, color: winner.colour.color, dash: dashFor(winner.style, winner.width) };
  }

  /**
   * Every candidate for one side of a cell in collapse mode (brief §4.4 branch B — probe (b)
   * measured that Chromium does not report the collapsed border on the cell). A spanning cell's
   * side pools the candidates of every grid segment it covers.
   */
  function collapsedSide(entry, side, model) {
    var rows = model.rows, last = rows.length - 1, lastCol = model.cols - 1;
    var r0 = entry.r, r1 = entry.r + entry.rowSpan - 1, c0 = entry.c, c1 = entry.c + entry.colSpan - 1;
    var pool = [edgeCandidate(entry.element, side, 0, r0 * 1e4 + c0)];
    function neighbour(r, c, opposite) {
      var other = model.occupant[r + ":" + c];
      if (other && other !== entry) pool.push(edgeCandidate(other.element, opposite, 0, other.r * 1e4 + other.c));
    }
    function group(r) { var g = rows[r].parentElement; return /^(THEAD|TBODY|TFOOT)$/.test(g.tagName) ? g : null; }
    function column(c) { return model.columns[c] || { col: null, group: null }; }
    var c, r;
    if (side === "top" || side === "bottom") {
      var rr = side === "top" ? r0 : r1, across = side === "top" ? rr - 1 : rr + 1;
      var opposite = side === "top" ? "bottom" : "top";
      for (c = c0; c <= c1; c++) if (across >= 0 && across <= last) neighbour(across, c, opposite);
      pool.push(edgeCandidate(rows[rr], side, 1, rr));
      if (across >= 0 && across <= last) pool.push(edgeCandidate(rows[across], opposite, 1, across));
      var own = group(rr), next = across >= 0 && across <= last ? group(across) : null;
      if (own && own !== next) pool.push(edgeCandidate(own, side, 2, rr));
      if (next && next !== own) pool.push(edgeCandidate(next, opposite, 2, across));
      if (across < 0 || across > last) {
        for (c = c0; c <= c1; c++) {
          pool.push(edgeCandidate(column(c).col, side, 3, c));
          pool.push(edgeCandidate(column(c).group, side, 4, c));
        }
        pool.push(edgeCandidate(model.el, side, 5, 0));
      }
    } else {
      var cc = side === "left" ? c0 : c1, beside = side === "left" ? cc - 1 : cc + 1;
      var facing = side === "left" ? "right" : "left";
      for (r = r0; r <= r1; r++) if (beside >= 0 && beside <= lastCol) neighbour(r, beside, facing);
      pool.push(edgeCandidate(column(cc).col, side, 3, cc));
      if (beside >= 0 && beside <= lastCol) pool.push(edgeCandidate(column(beside).col, facing, 3, beside));
      var ownGroup = column(cc).group, nextGroup = beside >= 0 && beside <= lastCol ? column(beside).group : null;
      if (ownGroup && ownGroup !== nextGroup) pool.push(edgeCandidate(ownGroup, side, 4, cc));
      if (nextGroup && nextGroup !== ownGroup) pool.push(edgeCandidate(nextGroup, facing, 4, beside));
      if (beside < 0 || beside > lastCol) {
        // A row (and a row group) has a left and right border only at the table's edges.
        for (r = r0; r <= r1; r++) {
          pool.push(edgeCandidate(rows[r], side, 1, r));
          if (group(r)) pool.push(edgeCandidate(group(r), side, 2, r));
        }
        pool.push(edgeCandidate(model.el, side, 5, 0));
      }
    }
    var winner = null;
    pool.forEach(function (candidate) { winner = strongerEdge(winner, candidate); });
    return winner;
  }

  /**
   * Each cell's four borders and its used border widths. Collapse mode resolves every edge; separate
   * mode keeps the cell's own sides (rows, groups and columns have no borders there, by spec).
   */
  function resolveCellBorders(entry, model) {
    var borders = {}, used = {};
    ["top", "right", "bottom", "left"].forEach(function (side) {
      if (model.collapse) {
        var winner = collapsedSide(entry, side, model);
        borders[side] = edgeBorder(winner);
        used[side[0]] = winner && winner.style !== "hidden" ? winner.width / 2 : 0;
      } else {
        borders[side] = edgeBorder(edgeCandidate(entry.element, side, 0, 0));
        used[side[0]] = px(entry.cs["border" + SIDE_NAMES[side] + "Width"]);
      }
    });
    entry.borders = borders;
    entry.used = used;
  }

  /**
   * An interior grid line is written once: as the upper cell's `lnB` and the left cell's `lnR`.
   * Measured in PowerPoint (probe (d), 2026-09-25): a lower cell's `lnT` with no `lnB` above it is
   * not drawn at all, while an upper `lnB` alone is — so the line has to live on the upper / left
   * cell, and the neighbour's side is cleared. PptxRender draws either and dedupes equal segments.
   * The same holds for `lnL` (measured 2026-09-29), so on the spacer grid a cell's top and left
   * rules move onto the spacer cells above it and to its left — their `lnB` / `lnR`, on the same
   * grid line. The walk is over the physical grid (`spacerGrid`), the logical one when there is none.
   * Under a merged cell's continuation columns / beside its continuation rows the cleared side comes
   * back (`mergedEdges`): there PowerPoint draws the neighbour's `lnT` / `lnL`.
   */
  function dedupeSharedEdges(model) {
    model.cells.forEach(function (entry) {
      var seen = [], k, other;
      if (entry.pr > 0) {
        for (k = entry.pc; k < entry.pc + entry.pcSpan; k++) {
          other = model.slots[(entry.pr - 1) + ":" + k];
          if (!other || other === entry || seen.indexOf(other) >= 0) continue;
          seen.push(other);
          if (entry.borders.top && !other.borders.bottom) other.borders.bottom = entry.borders.top;
          else if (entry.borders.top && !sameBorder(other.borders.bottom, entry.borders.top)) {
            model.infos.push("cells r" + other.r + "c" + other.c + " and r" + entry.r + "c" + entry.c +
                             " draw different rules on one grid line; the upper cell's is kept");
          }
        }
        if (seen.length) entry.borders.top = null;
      }
      seen = [];
      if (entry.pc > 0) {
        for (k = entry.pr; k < entry.pr + entry.prSpan; k++) {
          other = model.slots[k + ":" + (entry.pc - 1)];
          if (!other || other === entry || seen.indexOf(other) >= 0) continue;
          seen.push(other);
          if (entry.borders.left && !other.borders.right) other.borders.right = entry.borders.left;
          else if (entry.borders.left && !sameBorder(other.borders.right, entry.borders.left)) {
            model.infos.push("cells r" + other.r + "c" + other.c + " and r" + entry.r + "c" + entry.c +
                             " draw different rules on one grid line; the left cell's is kept");
          }
        }
        if (seen.length) entry.borders.left = null;
      }
    });
  }

  /**
   * A merged cell's rule along an interior edge, as PowerPoint wants it (measured 2026-09-29, and
   * what PowerPoint itself writes when a merged cell gets a bottom or right border through its UI):
   * the origin's `lnB` / `lnR` is drawn along its first column / row only; under an `hMerge`
   * continuation the segment is the lower cell's `lnT`, beside a `vMerge` one the right cell's
   * `lnL` — a merged neighbour's origin included (PowerPoint writes it there too). So the cells
   * below / to the right of the continuations carry the rule as well, in collapse mode (where
   * `dedupeSharedEdges` had cleared their side for the upper / left cell's) and on the spacer grid
   * alike. A merged cell on the table's edge needs nothing: there the origin's rule runs its whole
   * length. A table without a merged cell that has an interior bottom or right rule is unchanged.
   */
  function mergedEdges(model) {
    model.cells.forEach(function (entry) {
      var k, other, below = entry.pr + entry.prSpan, right = entry.pc + entry.pcSpan;
      if (entry.borders.bottom && entry.pcSpan > 1) {
        for (k = entry.pc + 1; k < right; k++) {
          other = model.slots[below + ":" + k];
          if (other && other !== entry && !other.borders.top) other.borders.top = entry.borders.bottom;
        }
      }
      if (entry.borders.right && entry.prSpan > 1) {
        for (k = entry.pr + 1; k < below; k++) {
          other = model.slots[k + ":" + right];
          if (other && other !== entry && !other.borders.left) other.borders.left = entry.borders.right;
        }
      }
    });
  }

  function sameBorder(a, b) {
    if (!a || !b) return !a && !b;
    return a.width === b.width && a.color === b.color && JSON.stringify(a.dash) === JSON.stringify(b.dash);
  }

  // ---- fills

  /** `over` compositing of one solid layer onto what is under it (premultiplied, then back). */
  function overColour(top, under) {
    if (!under || under.alpha <= 0) return { color: top.color, alpha: top.alpha };
    var alpha = top.alpha + under.alpha * (1 - top.alpha);
    if (alpha <= 0) return { color: top.color, alpha: 0 };
    var out = "";
    for (var i = 0; i < 6; i += 2) {
      var t = parseInt(top.color.slice(i, i + 2), 16), u = parseInt(under.color.slice(i, i + 2), 16);
      out += hex2((t * top.alpha + u * under.alpha * (1 - top.alpha)) / alpha);
    }
    return { color: out, alpha: Math.round(alpha * 10000) / 10000 };
  }

  /** The colour of a gradient at `t` (0..1 along its line), interpolated in sRGB like the stops. */
  function gradientAt(stops, t) {
    if (t <= stops[0].pos) return stops[0];
    var last = stops[stops.length - 1];
    if (t >= last.pos) return last;
    for (var i = 1; i < stops.length; i++) {
      var a = stops[i - 1], b = stops[i];
      if (t > b.pos) continue;
      var f = b.pos > a.pos ? (t - a.pos) / (b.pos - a.pos) : 0, colour = "";
      for (var k = 0; k < 6; k += 2) {
        var ca = parseInt(a.color.slice(k, k + 2), 16), cb = parseInt(b.color.slice(k, k + 2), 16);
        colour += hex2(ca + (cb - ca) * f);
      }
      return { pos: t, color: colour, alpha: a.alpha + (b.alpha - a.alpha) * f };
    }
    return last;
  }

  /**
   * A linear gradient declared on `from` (a row, a row group, a column), restated on the cell's
   * `to` box so it continues across the row instead of restarting in every cell (measured: the
   * browser paints a `tr` gradient across the whole row). CSS places position t on the line through
   * the box centre at the gradient's angle, scaled by the box's gradient-line length, so the same
   * point is affine in both boxes' t and every stop maps exactly; the ends are re-sampled.
   */
  function remapGradient(fill, from, to) {
    if (fill.kind !== "linear" || !from || !to) return fill;
    var rad = (fill.angle * Math.PI) / 180, sx = Math.sin(rad), sy = -Math.cos(rad);
    var length = Math.abs(from.w * sx) + Math.abs(from.h * sy), own = Math.abs(to.w * sx) + Math.abs(to.h * sy);
    if (!length || !own) return fill;
    var shift = (to.x + to.w / 2 - (from.x + from.w / 2)) * sx + (to.y + to.h / 2 - (from.y + from.h / 2)) * sy;
    var scale = own / length, offset = 0.5 - 0.5 * scale + shift / length;   // t_from = scale · t_to + offset
    var head = gradientAt(fill.stops, offset), tail = gradientAt(fill.stops, scale + offset);
    var stops = [{ pos: 0, color: head.color, alpha: head.alpha }];
    fill.stops.forEach(function (stop) {
      var t = (stop.pos - offset) / scale;
      if (t > 0 && t < 1) stops.push({ pos: t, color: stop.color, alpha: stop.alpha });
    });
    stops.push({ pos: 1, color: tail.color, alpha: tail.alpha });
    return { type: "gradient", kind: fill.kind, angle: fill.angle, stops: stops };
  }

  /**
   * Every fill CSS paints under the cell (brief §4.3), composited bottom to top: column group,
   * column, row group, row, cell, then the blocks folded into the cell (§4.5 rule 4). The table's
   * own background is not a layer — it is the surface shape behind the whole table. A gradient
   * layer cannot live in a cell: it becomes a shape behind the table, the solid layers above it are
   * the cell's fill, and the ones below it are covered by it and dropped.
   */
  function effectiveCellFill(entry, model, state) {
    var row = model.rows[entry.r], group = row.parentElement, column = model.columns[entry.c] || {};
    var layers = [
      { name: "col", el: column.group }, { name: "col", el: column.col },
      { name: "rowgroup", el: /^(THEAD|TBODY|TFOOT)$/.test(group.tagName) ? group : null },
      { name: "row", el: row }, { name: "cell", el: entry.element },
    ].concat(entry.folds.map(function (fold) { return { name: "fold", el: fold.el }; }));
    var solid = null, gradient = null, from = "none";
    layers.forEach(function (layer) {
      if (!layer.el) return;
      var lcs = getComputedStyle(layer.el), lb = borderBox(layer.el, model.frame);
      var fill = fillOf(lcs, lb, layer.el, state);
      if (!fill) return;
      if (fill.type === "gradient") {
        if (gradient) model.infos.push("cell r" + entry.r + "c" + entry.c + " has two gradient layers; the upper one is drawn");
        gradient = { fill: fill, box: lb, layer: layer.name };
        solid = null;
        from = "none";
        return;
      }
      solid = overColour({ color: fill.color, alpha: fill.alpha }, solid);
      from = layer.name;
    });
    entry.fill = solid && solid.alpha > 0 ? { type: "solid", color: solid.color, alpha: solid.alpha } : { type: "none" };
    entry.fillFrom = entry.fill.type === "solid" ? from : "none";
    if (gradient) {
      entry.gradientSource = gradient;   // restated again on the spacer grid's slot (`spacerGrid`)
      entry.gradient = remapGradient(gradient.fill, gradient.box, entry.slotBox);
      if (gradient.fill.kind !== "linear" && gradient.layer !== "cell") {
        model.infos.push("cell r" + entry.r + "c" + entry.c + ": a radial " + gradient.layer +
                         " gradient restarts in every cell; the browser spreads it across the " + gradient.layer);
      }
    }
  }

  // ---- the cell walk

  function newSink(root, mode, items) {
    return { root: root, mode: mode, excluded: [], chips: [], blockChips: [], items: items };
  }

  /** Is `node` under one of `elements`, looking no further up than `root`? */
  function underAny(node, elements, root) {
    if (!elements.length) return false;
    for (var el = node.nodeType === 1 ? node : node.parentElement; el && el !== root; el = el.parentElement) {
      if (elements.indexOf(el) >= 0) return true;
    }
    return false;
  }

  /** Text nodes under a sink's root that stay in its flow. */
  function flowNodesOf(sink) {
    return textNodesOf(sink.root).filter(function (node) { return !underAny(node, sink.excluded, sink.root); });
  }

  /** Does a block own text that would stay in the flow — text not inside an overlay or a painted inline? */
  function ownsFlowText(el) {
    var nodes = textNodesOf(el);
    for (var i = 0; i < nodes.length; i++) {
      if (!nodes[i].data.trim()) continue;
      var owned = true;
      for (var node = nodes[i].parentElement; node && node !== el; node = node.parentElement) {
        var ncs = getComputedStyle(node);
        if (isOutOfFlow(ncs) || isSpecial(node) || rasterReason(node, ncs)) { owned = false; break; }
        if (isInlineDisplay(ncs.display) && node.getClientRects().length &&
            hasDecoration(node, ncs, localBox(IDENTITY_FRAME, node.getClientRects()[0]))) { owned = false; break; }
      }
      if (owned) return true;
    }
    return false;
  }

  /** Rule 4: a block that exactly fills the cell's padding box paints the cell, nothing more. */
  function foldsIntoCell(n, ncs, nb, entry) {
    var p = entry.paddingBox;
    if (Math.abs(nb.x - p.x) > TABLE_FOLD_EPS || Math.abs(nb.y - p.y) > TABLE_FOLD_EPS ||
        Math.abs(nb.x + nb.w - p.x - p.w) > TABLE_FOLD_EPS || Math.abs(nb.y + nb.h - p.y - p.h) > TABLE_FOLD_EPS) return false;
    var radius = radiusOf(ncs, nb);
    if (radius.tl || radius.tr || radius.br || radius.bl || shadowOf(ncs) || clipPathGeometry(ncs, nb)) return false;
    var sides = sideBorders(ncs);
    return !(sides.top || sides.right || sides.bottom || sides.left) || !!uniformBorder(sides);
  }

  /**
   * Pre-order walk of a cell (brief §4.5): what stays in the cell's flow, and what is queued to be
   * drawn over the table. `sink.mode` is "cell" (the td itself), "block" (a block chip: its text is
   * one text element) or "chip" (inside an atomic inline chip: a painted inline there is a shape only,
   * its text stays in the chip's text).
   */
  function partition(root, sink, entry, model, state) {
    for (var i = 0; i < root.children.length; i++) {
      var n = root.children[i];
      if (n.hasAttribute("data-engine-probe")) continue;
      var ncs = getComputedStyle(n);
      if (ncs.display === "none") continue;
      if (isOutOfFlow(ncs) || isSpecial(n) || rasterReason(n, ncs)) {
        // An absolutely positioned badge, a flag <img>, an inline <svg> icon, a chart, a nested table:
        // walked as elements of their own, over the table. Their text leaves the flow.
        sink.excluded.push(n);
        var positioned = ncs.position === "absolute" || ncs.position === "fixed";
        sink.items.push({ tier: positioned ? TIER_POSITIONED : isOutOfFlow(ncs) ? TIER_FLOAT : TIER_INLINE,
                          seq: entry.seq++, walk: n, outOfFlow: isOutOfFlow(ncs) });
        if (n.tagName === "TABLE") {
          model.infos.push("a table inside cell r" + entry.r + "c" + entry.c +
                           " is emitted as a second native table over the outer one");
        }
        continue;
      }
      var rects = n.getClientRects();
      if (ncs.visibility === "hidden" || !rects.length) { partition(n, sink, entry, model, state); continue; }
      var inline = isInlineDisplay(ncs.display);
      var nb = inline ? localBox(model.frame, rects[0]) : borderBox(n, model.frame);
      var painted = hasDecoration(n, ncs, nb);
      if (!painted && !(sink.mode !== "chip" && detachesInCell(n, ncs, sink.root))) {
        partition(n, sink, entry, model, state);
        continue;
      }
      if (inline) {
        if (sink.mode === "chip") {
          // A painted inline inside a chip: its shape joins the chip's unit, its words the chip's text.
          sink.items.push({ tier: TIER_INLINE, seq: entry.seq++, chipShapes: n, cs: ncs, role: "chip" });
          partition(n, sink, entry, model, state);
          continue;
        }
        var chip = { el: n, cs: ncs, seq: entry.seq++, sink: null, painted: painted };
        chip.sink = newSink(n, "chip", []);
        sink.chips.push(chip);
        partition(n, chip.sink, entry, model, state);
        continue;
      }
      if (sink.mode === "cell" && foldsIntoCell(n, ncs, nb, entry)) {
        entry.folds.push({ el: n, cs: ncs, box: nb });
        partition(n, sink, entry, model, state);
        continue;
      }
      if ((fillOf(ncs, nb, n, state) || shadowOf(ncs)) && ownsFlowText(n)) {
        // A filled box owns its text (the heat-map cell): drawing the fill over the table while the
        // text stayed in the cell would bury the text, so both go over the table.
        sink.excluded.push(n);
        sink.items.push({ tier: TIER_BLOCK, seq: entry.seq++, decorate: n, cs: ncs, role: "blockChip" });
        var inner = newSink(n, "block", sink.items);
        sink.blockChips.push({ el: n, cs: ncs, sink: inner, seq: entry.seq - 1 });
        partition(n, inner, entry, model, state);
        continue;
      }
      // Border-only, or filled with no text of its own (a bar track): it covers no glyph.
      sink.items.push({ tier: TIER_BLOCK, seq: entry.seq++, decorate: n, cs: ncs, role: "decoration" });
      partition(n, sink, entry, model, state);
    }
  }

  /**
   * Package B's R3 read inside a cell (master brief §11 as amended by B): an atomic inline box with
   * horizontal geometry of its own (`hasOwnGeometry`, `wrapsOwnLines`) that shares its paragraph
   * with other text is an element of its own, painted or not — an arrow glyph with a margin before a
   * target, a fixed-width label. It leaves the cell as a chip does (its words a chipText, no shape
   * when it paints nothing), and the text after it is a segment of its own; kept, its run would be
   * glued across a gap no run can carry. A box that is flush (no geometry) or alone stays inline.
   */
  function detachesInCell(n, ncs, root) {
    if (!isAtomicInline(ncs.display) || !textSpanOf(n)) return false;
    if (!hasOwnGeometry(n) && !wrapsOwnLines(n)) return false;
    return textNodesOf(hostOf(n, root)).some(function (node) { return /\S/.test(node.data) && !n.contains(node); });
  }

  /** The paragraph host of a text node: its nearest non-inline ancestor at or below `root`. */
  function hostOf(node, root) {
    for (var el = node.parentElement; el && el !== root; el = el.parentElement) {
      if (!isInlineDisplay(getComputedStyle(el).display)) return el;
    }
    return root;
  }

  /**
   * What left a sink's flow but still holds a place in its lines: chips, inline icons and images,
   * block chips. Out-of-flow boxes (absolute, fixed, floats) take no inline space and split nothing.
   */
  function inlineSplitters(sink) {
    return sink.excluded.filter(function (el) { return !isOutOfFlow(getComputedStyle(el)); });
  }

  /** Does element `el` lie after node `a` and before node `b` in document order? */
  function liesBetween(el, a, b) {
    return !!(a.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) &&
           !!(el.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) &&
           !(el.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_CONTAINED_BY);
  }

  /**
   * Consecutive flow nodes that share a host are one paragraph (`td: a <div>b</div> c` is three),
   * and a paragraph is also split at every box that left the flow between two of its nodes (a chip,
   * an inline icon, a block chip): master brief §11 as amended by package B, read inside a cell —
   * the text on either side of a detached box is a segment of its own. Left joined, the words after
   * the box would be written where the box is drawn, under its paint.
   */
  function groupByHost(nodes, root, splitters) {
    var groups = [];
    nodes.forEach(function (node) {
      var host = hostOf(node, root), last = groups[groups.length - 1];
      var previous = last ? last.nodes[last.nodes.length - 1] : null;
      if (last && last.host === host &&
          !splitters.some(function (el) { return liesBetween(el, previous, node); })) last.nodes.push(node);
      else groups.push({ host: host, nodes: [node] });
    });
    return groups;
  }

  /** The rect of the first rendered non-space character of `nodes`. */
  function glyphRect(nodes) {
    var range = document.createRange();
    for (var n = 0; n < nodes.length; n++) {
      var node = nodes[n], data = node.data;
      for (var i = 0; i < data.length; i++) {
        if (/\s/.test(data[i])) continue;
        range.setStart(node, i);
        range.setEnd(node, i + 1);
        var rects = range.getClientRects();
        for (var r = 0; r < rects.length; r++) if (rects[r].width > 0 || rects[r].height > 0) return rects[r];
      }
    }
    return null;
  }

  /** Do two line boxes share a band (vertical overlap above half the shorter)? Package B's
   *  `sameBand` is the same test on its runs; this one is the table's, under its own name — two
   *  declarations of one name in the walk's scope would leave only the last in force. */
  function cellLinesShareBand(a, b) {
    var overlap = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
    return overlap > TABLE_BAND_SHARE * Math.max(LINE_EPS, Math.min(a.h, b.h));
  }

  /** The line of `lines` whose box holds the centre of `b` (else the nearest). */
  function lineIndexOf(lines, b) {
    var centre = b.y + b.h / 2, best = -1, distance = Infinity;
    lines.forEach(function (line, k) {
      var top = line.box.y, bottom = line.box.y + line.box.h;
      var d = centre < top ? top - centre : centre > bottom ? centre - bottom : 0;
      if (d < distance) { distance = d; best = k; }
    });
    return best;
  }

  /**
   * Per line of a measured paragraph, the painted x of its first and last glyph `{s, e}` — spaces
   * left out: a line box keeps a rendered space before or after an inline box, the runs do not.
   */
  function lineSpans(item, model) {
    var lines = item.paragraph.lines, spans = lines.map(function () { return null; });
    var range = document.createRange();
    item.nodes.forEach(function (node) {
      var data = node.data;
      for (var i = 0; i < data.length; i++) {
        if (/\s/.test(data[i])) continue;
        range.setStart(node, i);
        range.setEnd(node, i + 1);
        var rects = range.getClientRects();
        for (var r = 0; r < rects.length; r++) {
          if (!(rects[r].width > 0 || rects[r].height > 0)) continue;
          var b = paintedBox(model.frame, localBox(model.frame, rects[r]));
          var k = lineIndexOf(lines, b);
          if (k >= 0) {
            spans[k] = spans[k] ? { s: Math.min(spans[k].s, b.x), e: Math.max(spans[k].e, b.x + b.w) }
                                : { s: b.x, e: b.x + b.w };
          }
          break;
        }
      }
    });
    return spans.map(function (span, k) {
      return span || { s: lines[k].box.x, e: lines[k].box.x + lines[k].box.w };
    });
  }

  /**
   * Measure one group of flow nodes as a paragraph of the IR's text grammar, with package F's
   * `measureParagraph` under the cell's clip (plan §16 #11). What the clip hid is kept as a
   * `trimClipped` entry — on the item, or on the cell when the whole paragraph is hidden — and
   * reported once its record exists (`emitCellText`, `reportCellText`).
   */
  function measureGroup(group, entry, model, state, clip) {
    var hcs = getComputedStyle(group.host);
    var measured = measureParagraph(group.nodes, group.host, model.frame, clip);
    var tally = clipEntry(group.host, false, clip, measured.hidden);
    if (!measured.lines.length) {
      if (tally) entry.hiddenAway.push(tally);
      return null;
    }
    return {
      host: group.host, cs: hcs, nodes: group.nodes, clipEntry: tally,
      paragraph: {
        align: resolveAlign(hcs), lineHeightPx: lineHeightPx(hcs), spaceBeforePx: 0, spaceAfterPx: 0, bullet: null,
        lines: cellLines(measured.lines, model.frame),
      },
    };
  }

  /** A `trimClipped` entry `{el, hug, clip, hidden}` (package F's shape), or null when nothing clips. */
  function clipEntry(el, hug, clip, hidden) {
    return clip && hidden && hidden.fullExtent ? { el: el, hug: hug, clip: clip, hidden: hidden } : null;
  }

  /**
   * Package B's run geometry for the IR lines of a cell's text, kept beside them (not in them: the
   * IR line is `{box, runs}`): per line, each run's `[left, right]` as `flushText` writes
   * `x-run-boxes` (R6 keeps glued runs apart, so the text row can see the join), and the line's R6
   * seams. Keyed by the IR line object, which `placeParagraph`'s pieces share.
   */
  var CELL_LINE_GEOMETRY = new WeakMap();

  function cellLines(lines, frame) {
    return lines.map(function (line) {
      var out = { box: paintedBox(frame, line.box), runs: line.runs };
      CELL_LINE_GEOMETRY.set(out, {
        runBoxes: (line._runBoxes || []).map(function (b) {
          return [round2(b[0] + (frame.dx || 0)), round2(b[1] + (frame.dx || 0))];
        }),
        seams: line._seams || [],
      });
      return out;
    });
  }

  /** `x-run-boxes` for IR paragraphs built by `cellLines` (paragraph × line × run). */
  function cellRunBoxes(paragraphs) {
    return paragraphs.map(function (paragraph) {
      return paragraph.lines.map(function (line) {
        var geometry = CELL_LINE_GEOMETRY.get(line);
        return geometry ? geometry.runBoxes : [];
      });
    });
  }

  /**
   * Package B's R6 finding for text of a table (a cell's own, or a text record over the table):
   * one warn per element, in `flushText`'s words, quoting its first glued join.
   */
  function reportCellSeams(paragraphs, path, index, state) {
    var seams = [];
    paragraphs.forEach(function (paragraph) {
      paragraph.lines.forEach(function (line) {
        var geometry = CELL_LINE_GEOMETRY.get(line);
        if (geometry) seams = seams.concat(geometry.seams);
      });
    });
    if (!seams.length) return;
    var more = seams.length > 1 ? " (" + seams.length + " joins in this text)" : "";
    diagnose(state, "warn", path,
             "glued runs: '" + seams[0].left.slice(-24) + "' + '" + seams[0].right.slice(0, 24) +
             "' are separated by layout, not by a space — the export cannot keep that gap" + more, index);
  }

  /**
   * A text record over the table (chip text, band text, block chip text), pushed the way `flushText`
   * finishes one: package E's run fills lifted into `x-wpe-run-fills`, package B's `x-run-boxes`
   * and seam finding, and package F's `trimClipped` for what the cell's clip hid.
   */
  function emitCellText(item, ctx, state) {
    var record = push(state, item.text, item.host, ctx), index = state.records.length - 1;
    record.extras = record.extras || {};
    record.extras["x-run-boxes"] = cellRunBoxes(record.paragraphs);
    var fills = liftRunFills(record.paragraphs);
    if (fills) {
      record.extras["x-wpe-run-fills"] = fills;
      diagnose(state, "info", cssPath(item.host),
               "gradient text: the gradient is stretched over the text box, approximated", index);
    }
    reportCellSeams(record.paragraphs, record.source.path, index, state);
    trimClipped(record, (item.clipEntries || []).filter(Boolean), null, ctx, state);
  }

  /**
   * After the native table's record is pushed, what its cells' own text needs said. A cell run
   * carries no gradient (package E's `write_cell` writes none), so a `_fill` is dropped with an info;
   * the runs' boxes go to the cell's `x-wpa` entry; seams are package B's warn; what the clip hid
   * is package F's `trimClipped` finding — against a stand-in text record of the cell's paragraphs,
   * as the table has none — attached to the table record.
   */
  function reportCellText(model, entry, record, index, ctx, state) {
    var cell = record.extras["x-wpa"].cells[entry.pr + ":" + entry.pc];
    if (liftRunFills(entry.paragraphs)) {
      model.infos.push("cell r" + entry.r + "c" + entry.c +
                       ": gradient text is written in one colour (a cell run carries no gradient)");
    }
    if (entry.paragraphs.length) cell.runBoxes = cellRunBoxes(entry.paragraphs);
    reportCellSeams(entry.paragraphs, cssPath(entry.element), index, state);
    var entries = entry.clipEntries.slice();
    entries.hidden = entry.hiddenAway;
    var stand = entry.paragraphs.length
      ? { kind: "text", box: paintedBox(model.frame, entry.contentBox), paragraphs: entry.paragraphs, clip: null,
          source: sourceOf(entry.element) }
      : null;
    var from = state.diagnostics.length;
    trimClipped(stand, entries, null, ctx, state);
    for (var i = from; i < state.diagnostics.length; i++) state.diagnostics[i].recordIndex = index;
    if (stand && stand.extras && stand.extras["x-wpf-clipped"]) cell.clipped = stand.extras["x-wpf-clipped"];
  }

  /**
   * Measured paragraphs for a sink's flow — the cell's own, or a block chip's inside it — with the
   * gap before each and the bullet of an `li` (on its first paragraph only; `listLeft` keeps where
   * the list's bullets are drawn). The successor of the one-paragraph `cellParagraphs` plan §7 names;
   * its `measureParagraph` call (in `measureGroup`) is package F's, under the cell's clip.
   */
  function cellParagraphs(sink, entry, model, state, clip) {
    var measured = [], bulleted = [];
    groupByHost(flowNodesOf(sink), sink.root, inlineSplitters(sink)).forEach(function (group) {
      var item = measureGroup(group, entry, model, state, clip);
      if (!item) return;
      item.listLeft = null;
      if (group.host.tagName === "LI" && bulleted.indexOf(group.host) < 0) {
        bulleted.push(group.host);
        var bullet = bulletOf(group.host, item.cs, model.frame);
        if (bullet) { item.listLeft = bullet._listLeft; delete bullet._listLeft; item.paragraph.bullet = bullet; }
      }
      var previous = measured[measured.length - 1];
      if (previous) {
        var last = previous.paragraph.lines[previous.paragraph.lines.length - 1].box;
        item.paragraph.spaceBeforePx = Math.max(0, item.paragraph.lines[0].box.y - (last.y + last.h));
      }
      measured.push(item);
    });
    return measured;
  }

  /**
   * The table half of F2 (brief §4.6; master brief §11 as amended by package B, read inside a cell):
   * two flow paragraphs with a line in the same band sit side by side — `label | value` in a flex
   * row, or the segments either side of a chip. PowerPoint stacks a cell's paragraphs, so per band
   * only the leftmost stays in the flow; each of the others is returned to become a text box over
   * the table.
   */
  function bandParagraphs(measured) {
    var band = measured.map(function (item, index) { return index; });
    function find(i) { while (band[i] !== i) i = band[i]; return i; }
    function beside(a, b) {
      return a.paragraph.lines.some(function (above) {
        return b.paragraph.lines.some(function (below) { return cellLinesShareBand(above.box, below.box); });
      });
    }
    for (var i = 0; i < measured.length; i++) {
      for (var j = i + 1; j < measured.length; j++) {
        if (beside(measured[i], measured[j])) band[find(j)] = find(i);
      }
    }
    var keep = {}, out = [];
    measured.forEach(function (item, index) {
      var root = find(index), x = item.paragraph.lines[0].box.x;
      if (keep[root] === undefined || x < measured[keep[root]].paragraph.lines[0].box.x) keep[root] = index;
    });
    measured.forEach(function (item, index) { if (keep[find(index)] !== index) out.push(item); });
    return out;
  }

  /** Does `next` start below `previous` ends (package B's `stacked`, vertical half)? */
  function stackedUnder(previous, next) {
    var lines = previous.paragraph.lines, above = lines[lines.length - 1].box, first = next.paragraph.lines[0].box;
    return first.y >= above.y + above.h - TABLE_BAND_SHARE * Math.min(above.h, first.h);
  }

  /**
   * Where a kept paragraph's lines go in the native cell (brief §4.6's leading offset, generalised
   * to every line). PowerPoint places a paragraph with a `marL` and a first-line `indent`, from the
   * cell's content box: left-aligned lines after the first start at `marL` and the first at
   * `marL + indent` (an icon or a chip before the first word); centred lines centre on
   * `(marL + width) / 2`, the first moved by `indent`; right-aligned lines end at the right inset; a
   * bulleted first line starts at `marL`, its bullet at `marL + indent` — where the browser drew the
   * list's bullets. Measured on the glyphs (`lineSpans`), so a `margin-left` or a grid column puts
   * the text where the browser did. `marR` is never written: PptxRender ignores it (TextStyles.cs
   * reads `marL` and `indent` only), so the gate would disagree with PowerPoint. A centred line left
   * of the centre or a right-aligned line short of the right edge — one that shares its line with a
   * box after it (`1,284 ▲`, `Up [+3] pts`) — is therefore written left-aligned at its measured start.
   * Lines that do not share their anchor — text beside a float, a list item whose first line starts
   * after a chip and wraps (package B's R8), a centred line shared with a chip — are split where they
   * part; a later piece has no bullet. Returns `[{paragraph, marL, indent}]` in px.
   */
  function placeParagraph(item, entry, model) {
    var p = item.paragraph, lines = p.lines, spans = lineSpans(item, model);
    var cell = paintedBox(model.frame, entry.contentBox), width = cell.w;
    var host = item.host === entry.element ? cell
             : paintedBox(model.frame, contentBox(borderBox(item.host, model.frame), item.cs));
    var hostLeft = host.x - cell.x;
    var s = spans.map(function (span) { return span.s - cell.x; });
    var e = spans.map(function (span) { return span.e - cell.x; });
    var align = p.align === "center" || p.align === "right" ? p.align : "left";
    var bulletAt = p.bullet && item.listLeft !== null
      ? paintedBox(model.frame, boxOf(item.listLeft, 0, 0, 0)).x - cell.x : null;
    function anchor(k) { return align === "right" ? e[k] : align === "center" ? (s[k] + e[k]) / 2 : s[k]; }
    function agree(a, b) { return Math.abs(anchor(a) - anchor(b)) <= TABLE_PLACE_TOL_PX; }

    // A piece is a run of lines that share the anchor of its second line; its first line may differ
    // through `indent`, unless it carries the bullet (its text starts at `marL`) or is right-aligned.
    var cuts = [0];
    for (var k = 1; k < lines.length; k++) {
      var from = cuts[cuts.length - 1];
      var fixedFirst = align === "right" || (from === 0 && !!p.bullet);
      if (k === from + 1 ? fixedFirst && !agree(k, from) : !agree(k, from + 1)) cuts.push(k);
    }
    cuts.push(lines.length);
    var pieces = [];
    for (var n = 0; n + 1 < cuts.length; n++) place(cuts[n], cuts[n + 1], align);
    return pieces;

    function place(from, to, as) {
      var bulleted = from === 0 && !!p.bullet, multi = to - from >= 2;
      var marL = 0, indent = 0;
      if (as === "center") {
        var centre = multi ? (s[from + 1] + e[from + 1]) / 2 : (s[from] + e[from]) / 2;
        if (centre < width / 2 - TABLE_PLACE_TOL_PX / 2) as = "left";
        else {
          marL = 2 * centre - width;
          if (multi && !bulleted) {
            indent = s[from] + e[from] - marL - width;
            if (marL + indent < -TABLE_PLACE_TOL_PX) { place(from, from + 1, as); place(from + 1, to, as); return; }
          }
        }
      } else if (as === "right" && e[from] < width - TABLE_PLACE_TOL_PX) {
        as = "left";
      }
      if (as === "left" && align !== "left" && multi) {
        // Centred or right-aligned lines of different widths start at different x: one per line.
        for (var line = from; line < to; line++) place(line, line + 1, "left");
        return;
      }
      if (as === "left") {
        marL = multi ? s[from + 1] : bulleted ? s[from] : Math.min(hostLeft, s[from]);
        if (!bulleted) indent = s[from] - marL;
      }
      if (bulleted) indent = (bulletAt !== null ? bulletAt : marL - (p.bullet.indentPx || 0)) - marL;
      if (marL < 0) { indent += marL; marL = 0; }
      if (marL + indent < 0) indent = -marL;
      var paragraph = {};
      Object.keys(p).forEach(function (key) { paragraph[key] = p[key]; });
      paragraph.lines = lines.slice(from, to);
      paragraph.bullet = bulleted ? p.bullet : null;
      if (as !== align) paragraph.align = "left";
      pieces.push({ paragraph: paragraph, marL: marL, indent: indent });
    }
  }

  /** `text-transform` of a text element, when all its paragraphs agree (as flushText derives it). */
  function caseOf(items) {
    var transforms = {};
    items.forEach(function (item) { transforms[item.cs.textTransform] = true; });
    var keys = Object.keys(transforms);
    return keys.length === 1 && keys[0] === "uppercase" ? "upper" : keys.length === 1 && keys[0] === "lowercase" ? "lower" : null;
  }

  /**
   * A rendered space before the first glyph (after an icon or a chip) is in the measured line box
   * but not in the run, so a left-aligned box starting at the line box writes the first glyph one
   * space too far left. Start the box at the glyph instead, when the first line is the leftmost.
   */
  function trimLeadingSpace(span, item, model) {
    if (item.paragraph.align !== "left" || Math.abs(item.paragraph.lines[0].box.x - span.x) > 0.01) return span;
    var glyph = glyphRect(item.nodes);
    if (!glyph) return span;
    var x = paintedBox(model.frame, localBox(model.frame, glyph)).x;
    return x > span.x && x < span.x + span.w ? boxOf(x, span.y, span.x + span.w - x, span.h) : span;
  }

  /**
   * A text record over the table (a band's other paragraph, a block chip's text): its lines' union
   * widened to the host's content box as flushText does — except that left-aligned text keeps its own
   * left edge, where its first glyph is, and that a centred or right-aligned band paragraph (`hug`)
   * keeps its own glyphs' anchor: it may be a segment beside a chip, which the browser centred or
   * right-aligned together with the rest of its line, not alone in the host (package B's hug rule).
   * Its box is then the widest the cell allows around that anchor — centred on the glyphs' centre,
   * or ending at their end — so PowerPoint's wider metrics grow the text about the anchor instead of
   * moving it (a box that hugs the glyphs is widened to the right by `_place_box`).
   */
  function overlayTextRecord(items, host, entry, model, anchor, hug) {
    var span = null, wrap = true, align = items[0].paragraph.align;
    items.forEach(function (item) {
      item.paragraph.lines.forEach(function (line) { span = unionBox(span, line.box); });
      if (item.cs.whiteSpace === "nowrap" || item.cs.whiteSpace === "pre") wrap = false;
    });
    span = trimLeadingSpace(span, items[0], model);
    var block = paintedBox(model.frame, host === entry.element ? entry.contentBox
                                                            : contentBox(borderBox(host, model.frame), getComputedStyle(host)));
    var left = align === "left" ? span.x : Math.min(block.x, span.x);
    var right = Math.max(block.x + block.w, span.x + span.w);
    if (hug && (align === "center" || align === "right")) {
      var cell = paintedBox(model.frame, entry.contentBox), s = Infinity, e = -Infinity;
      items.forEach(function (item) {
        lineSpans(item, model).forEach(function (glyphs) { s = Math.min(s, glyphs.s); e = Math.max(e, glyphs.e); });
      });
      if (align === "right") {
        left = Math.min(cell.x, s);
        right = e;
      } else {
        var centre = (s + e) / 2, reach = Math.max((e - s) / 2, Math.min(centre - cell.x, cell.x + cell.w - centre));
        left = centre - reach;
        right = centre + reach;
      }
    }
    return {
      kind: "text", box: boxOf(left, span.y, right - left, span.h),
      paragraphs: items.map(function (item) { return item.paragraph; }),
      anchor: anchor || "top", writingMode: "horizontal", wrap: wrap, transformCase: caseOf(items),
    };
  }

  /** A chip's own text: the union of its line boxes, one line, laid over the chip's shape. */
  function chipTextRecord(item, model) {
    var span = null;
    item.paragraph.lines.forEach(function (line) { span = unionBox(span, line.box); });
    return {
      kind: "text", box: trimLeadingSpace(span, item, model), paragraphs: [item.paragraph], anchor: "top",
      writingMode: "horizontal", wrap: false, transformCase: caseOf([item]),
    };
  }

  /** The anchor a flex or grid parent gives its item, as flushText derives it. */
  function anchorFromParent(host) {
    var parent = host.parentElement ? getComputedStyle(host.parentElement) : null;
    if (parent && /flex|grid/.test(parent.display)) {
      if (parent.alignItems === "center") return "middle";
      if (parent.alignItems === "flex-end" || parent.alignItems === "end") return "bottom";
    }
    return "top";
  }

  /**
   * A chip's paint: one shape per client rect — the per-rect body of `emitInlineDecoration`,
   * duplicated on purpose (that function is B's; B may merge a general helper this becomes a call
   * to) — plus, where the border sides differ, one shape per painted side as package E's
   * `emitInlineDecoration` draws them (`border-left: 3px solid` on a tag): the start side on the
   * first fragment only, the end side on the last (box-decoration-break: slice).
   */
  function emitCellChip(el, cs, ctx, state) {
    state.decorated.push(el);
    var rects = el.getClientRects(), first = state.records.length;
    for (var r = 0; r < rects.length; r++) {
      var b = localBox(ctx.frame, rects[r]);
      if (b.w < MIN_EXTENT || b.h < MIN_EXTENT) continue;
      var fill = fillOf(cs, b, el, state);
      var sides = sideBorders(cs);
      var uniform = uniformBorder(sides);
      var radius = radiusOf(cs, b);
      push(state, {
        kind: "shape", box: paintedBox(ctx.frame, b),
        geometry: radius.tl || radius.tr || radius.br || radius.bl ? { type: "roundRect", radius: radius } : { type: "rect" },
        fill: fill || { type: "none" },
        stroke: uniform ? { color: uniform.color, alpha: uniform.alpha, width: uniform.width,
                            dash: dashFor(uniform.style, uniform.width), cap: "butt", join: "miter",
                            headEnd: null, tailEnd: null } : null,
        shadow: shadowOf(cs),
      }, el, ctx);
      if (!uniform && (sides.top || sides.right || sides.bottom || sides.left)) {
        var rtl = cs.direction === "rtl";
        emitSideBorders(el, cs, b, ctx, state, nameOf(el, state), {
          left: r > 0 && !rtl || r < rects.length - 1 && rtl,
          right: r < rects.length - 1 && !rtl || r > 0 && rtl,
        });
      }
    }
    // G-1's warn for the shadow layers the one IR shadow cannot keep (plan §16 #19), once per chip.
    reportDroppedShadows(el, cs, state, state.records.length > first && shadowOf(cs) ? first : null);
  }

  /**
   * Settle a sink's chips and block chips into queued items; returns the sink's flow paragraphs.
   * Every chip's words leave with it (Peter #4: chip text is a text box over the native table): its
   * paint is drawn above the table, so words left in the cell would lie under it. The text on either
   * side of a chip is split into segments by `groupByHost`.
   */
  function settleSink(sink, entry, model, state, clip) {
    sink.chips.forEach(function (chip) {
      var unit = chip.painted ? [{ tier: TIER_FIRST, seq: chip.seq, chipShapes: chip.el, cs: chip.cs, role: "chip" }] : [];
      sink.excluded.push(chip.el);
      var own = flowNodesOf(chip.sink);
      var text = own.length ? measureParagraph(own, chip.el, model.frame, clip) : { lines: [], hidden: null };
      var tally = clipEntry(chip.el, true, clip, text.hidden);
      if (text.lines.length) {
        var item = { host: chip.el, cs: chip.cs, nodes: own,
                     paragraph: { align: resolveAlign(chip.cs), lineHeightPx: lineHeightPx(chip.cs), spaceBeforePx: 0,
                                  spaceAfterPx: 0, bullet: null, lines: cellLines(text.lines, model.frame) } };
        unit.push({ tier: TIER_TEXT, seq: chip.seq, text: chipTextRecord(item, model), host: chip.el, role: "chipText",
                    clipEntries: [tally] });
      } else if (tally) {
        entry.hiddenAway.push(tally);
      }
      settleBlockChips(chip.sink, entry, model, state, clip);
      sink.items.push({ tier: TIER_INLINE, seq: chip.seq, unit: unit.concat(chip.sink.items) });
    });
    settleBlockChips(sink, entry, model, state, clip);
    return cellParagraphs(sink, entry, model, state, clip);
  }

  /** Each block chip's own paragraphs become one text element over its fill (brief §4.7). */
  function settleBlockChips(sink, entry, model, state, clip) {
    sink.blockChips.forEach(function (blockChip) {
      var measured = settleSink(blockChip.sink, entry, model, state, clip);
      var beside = bandParagraphs(measured);
      beside.forEach(function (item) { queueBandText(item, sink.items, entry, model); });
      var stacked = measured.filter(function (item) { return beside.indexOf(item) < 0; });
      if (!stacked.length) return;
      var record = overlayTextRecord(stacked, blockChip.el, entry, model, "top");
      sink.items.push({ tier: TIER_TEXT, seq: blockChip.seq, text: record, host: blockChip.el, role: "blockChipText",
                        clipEntries: stacked.map(function (item) { return item.clipEntry; }) });
    });
  }

  function queueBandText(item, items, entry, model) {
    var record = overlayTextRecord([item], item.host, entry, model, anchorFromParent(item.host), true);
    items.push({ tier: TIER_TEXT, seq: entry.seq++, text: record, host: item.host, role: "bandText",
                 clipEntries: [item.clipEntry] });
    entry.bands++;
  }

  /**
   * One cell, end to end: partition, settle, the flow paragraphs that stay in the native cell and
   * where their lines go (`placeParagraph`), and the paragraphs that leave it (a band's other
   * paragraphs — the text beside a chip, a flex row's second item — and anything that does not
   * start below the paragraph kept before it).
   */
  function analyseCell(entry, model, state) {
    entry.items = [];
    entry.seq = 0;
    entry.bands = 0;
    entry.folds = [];
    entry.clipEntries = [];
    entry.hiddenAway = [];
    var sink = newSink(entry.element, "cell", entry.items);
    partition(entry.element, sink, entry, model, state);
    if (entry.folds.length) {
      // A folded block's border and padding are the cell's inset now: the text starts inside them.
      var fold = entry.folds[entry.folds.length - 1], slotBox = entry.slotBox, cs = entry.cs;
      entry.contentBox = contentBox(fold.box, fold.cs);
      entry.slot = {
        t: Math.max(0, entry.contentBox.y - slotBox.y - px(cs.paddingTop)),
        r: Math.max(0, slotBox.x + slotBox.w - (entry.contentBox.x + entry.contentBox.w) - px(cs.paddingRight)),
        b: Math.max(0, slotBox.y + slotBox.h - (entry.contentBox.y + entry.contentBox.h) - px(cs.paddingBottom)),
        l: Math.max(0, entry.contentBox.x - slotBox.x - px(cs.paddingLeft)),
      };
    }
    // Plan §16 #11: the cell's content box is the clip its paragraphs are measured under.
    var clip = intersectBox(model.overlayClip, paintedBox(model.frame, entry.contentBox));
    var measured = settleSink(sink, entry, model, state, clip);

    // Per band the leftmost paragraph stays; PowerPoint stacks a cell's paragraphs in order, so a
    // paragraph that does not start below the one kept before it (a reordered flex column) leaves too.
    var beside = bandParagraphs(measured), kept = [];
    measured.forEach(function (item) {
      if (beside.indexOf(item) >= 0) return;
      if (kept.length && !stackedUnder(kept[kept.length - 1], item)) { beside.push(item); return; }
      kept.push(item);
    });
    measured.forEach(function (item) { if (beside.indexOf(item) >= 0) queueBandText(item, entry.items, entry, model); });

    entry.indentPx = [];
    entry.marginPx = [];
    entry.paragraphs = [];
    entry.wrap = true;
    kept.forEach(function (item) {
      if (item.cs.whiteSpace === "nowrap" || item.cs.whiteSpace === "pre") entry.wrap = false;
      if (item.clipEntry) entry.clipEntries.push(item.clipEntry);
      placeParagraph(item, entry, model).forEach(function (piece) {
        var previous = entry.paragraphs[entry.paragraphs.length - 1];
        var last = previous ? previous.lines[previous.lines.length - 1].box : null;
        piece.paragraph.spaceBeforePx = last ? Math.max(0, piece.paragraph.lines[0].box.y - (last.y + last.h)) : 0;
        entry.paragraphs.push(piece.paragraph);
        entry.indentPx.push(Math.abs(piece.indent) > TABLE_INDENT_MIN_PX ? piece.indent : 0);
        entry.marginPx.push(piece.marL);
      });
    });
  }

  // ---- the model, the record, the paint around it

  /** Measure everything about one table; nothing is pushed here. */
  function tableModel(el, cs, ctx, state) {
    var frame = ctx.frame, name = nameOf(el, state);
    var rows = tableRows(el, frame), occupancy = tableCells(rows);
    var model = {
      el: el, cs: cs, name: name, frame: frame, ctx: ctx, rows: rows, cells: occupancy.cells,
      occupant: occupancy.occupant, cols: occupancy.cols, columns: tableColumns(el), infos: [], warns: [],
      collapse: cs.borderCollapse === "collapse", overlayClip: ctx.clip,
    };
    var spacing = String(cs.borderSpacing || "0px").split(/\s+/);
    model.spacing = model.collapse ? { h: 0, v: 0 }
                                   : { h: px(spacing[0]), v: px(spacing.length > 1 ? spacing[1] : spacing[0]) };
    model.cells.forEach(function (entry) {
      entry.cs = getComputedStyle(entry.element);
      entry.box = borderBox(entry.element, frame);
    });
    var boxes = gridBoxOf(el, cs, frame, model.cells, model.collapse);
    model.grid = boxes.grid;
    model.tableBox = boxes.table;
    model.caption = boxes.caption;
    model.cells.forEach(function (entry) { resolveCellBorders(entry, model); });
    slotGeometry(model);
    model.cells.forEach(function (entry) {
      analyseCell(entry, model, state);
      if (entry.folds.length) {
        var fold = entry.folds[entry.folds.length - 1], sides = sideBorders(fold.cs), uniform = uniformBorder(sides);
        if (uniform && !(entry.borders.top || entry.borders.right || entry.borders.bottom || entry.borders.left)) {
          var border = { width: uniform.width, color: uniform.color, dash: dashFor(uniform.style, uniform.width) };
          entry.borders = { top: border, right: border, bottom: border, left: border };
        }
      }
      effectiveCellFill(entry, model, state);
    });
    spacerGrid(model);
    dedupeSharedEdges(model);
    mergedEdges(model);

    if (ctx.group) model.infos.push("table overlays leave the group; the emitter places the table on the slide");
    if (frame.angle) model.infos.push("a rotated table is exported unrotated (a deferred follow-up); its cell overlays follow the rotation");
    rows.forEach(function (row) {
      [row, row.parentElement].forEach(function (box) {
        if (!box || box === el || box.__engineRowPaintNoted) return;
        var bcs = getComputedStyle(box), bb = borderBox(box, frame), radius = radiusOf(bcs, bb);
        if (radius.tl || radius.tr || radius.br || radius.bl || shadowOf(bcs)) {
          box.__engineRowPaintNoted = true;
          model.infos.push("border-radius or box-shadow on a table " + box.tagName.toLowerCase() + " is not drawn");
        }
      });
    });
    return model;
  }

  /** The native table's record (brief §4.8): the frozen cell fields, plus `extras["x-wpa"]`. */
  function tableRecord(model) {
    var cells = {}, decorated = false;
    var records = model.cells.map(function (entry) {
      var cs = entry.cs;
      cells[entry.pr + ":" + entry.pc] = {
        fillFrom: entry.fillFrom, flow: entry.paragraphs.length ? "text" : "none", indentPx: entry.indentPx,
        marginPx: entry.marginPx, slot: entry.slot, wrap: entry.wrap, overlays: 0, bands: entry.bands,
      };
      if (entry.items.length || entry.gradient) decorated = true;
      return {
        r: entry.pr, c: entry.pc, rowSpan: entry.prSpan, colSpan: entry.pcSpan,
        fill: entry.fill, borders: entry.borders,
        paddingPx: { t: px(cs.paddingTop), r: px(cs.paddingRight), b: px(cs.paddingBottom), l: px(cs.paddingLeft) },
        valign: cs.verticalAlign === "middle" ? "middle" : cs.verticalAlign === "bottom" ? "bottom" : "top",
        paragraphs: entry.paragraphs,
      };
    });
    // A spacer cell (`spacerGrid`) is empty, unfilled and has no inset; it carries only the rule of
    // the cell below it (`lnB`) or to its right (`lnR`) that `dedupeSharedEdges` moved onto it.
    model.spacerCells.forEach(function (spacer) {
      cells[spacer.pr + ":" + spacer.pc] = {
        fillFrom: "none", flow: "none", indentPx: [], marginPx: [], slot: { t: 0, r: 0, b: 0, l: 0 }, wrap: true,
        overlays: 0, bands: 0, spacer: true,
      };
      records.push({ r: spacer.pr, c: spacer.pc, rowSpan: 1, colSpan: 1, fill: { type: "none" }, borders: spacer.borders,
                     paddingPx: { t: 0, r: 0, b: 0, l: 0 }, valign: "top", paragraphs: [] });
    });
    if (model.spacerCells.length) records.sort(function (a, b) { return a.r - b.r || a.c - b.c; });
    var wpa = { v: 1, collapse: model.collapse, spacingPx: model.spacing,
                decorated: decorated || !!model.surface, grouped: !!model.ctx.group, cells: cells };
    if (model.spacers) wpa.spacers = model.spacers;
    return {
      kind: "table", box: paintedBox(model.frame, model.grid), name: model.name,
      rows: model.rowHeightsPx.length, cols: model.colWidthsPx.length,
      colWidthsPx: model.colWidthsPx, rowHeightsPx: model.rowHeightsPx, cells: records,
      extras: { "x-wpa": wpa },
    };
  }

  /**
   * `extras["x-wpa"]` and a name on every record pushed since `from` that has none yet (a nested
   * table and its own overlays keep theirs; an svg placeholder has no extras field — `ir.py` is
   * frozen — so its expansion is not tagged).
   */
  function stampOverlays(state, from, model, entry, role, rename) {
    for (var i = from; i < state.records.length; i++) {
      var record = state.records[i];
      if (record.kind === "svgPlaceholder") continue;
      record.extras = record.extras || {};
      if (record.extras["x-wpa"]) continue;
      var own = typeof role === "function" ? role(record) : role;
      record.extras["x-wpa"] = { cell: entry ? [entry.pr, entry.pc] : null, table: model.name, role: own };
      if (!rename) continue;
      if (entry) {
        entry.roles = entry.roles || {};
        var k = entry.roles[own] = (entry.roles[own] || 0) + 1;
        record.name = model.name + " r" + entry.pr + "c" + entry.pc + " " + own + (k > 1 ? " " + k : "");
      } else {
        record.name = model.name + " " + own;
      }
    }
  }

  /**
   * The table's own background, shadow and radius, behind it on the table box (brief §4.1 step 2).
   * Its border is drawn here only in `separate` mode: in `collapse` mode it is part of the collapsed
   * grid and is already on the outer cells — drawing it here too would paint it twice, half a pixel
   * apart. Radius applies only in `separate` mode (CSS ignores it on a collapsed table).
   */
  function emitTableSurface(model, ctx, state) {
    var cs = model.cs, b = model.tableBox, from = state.records.length;
    if (b.w < MIN_EXTENT || b.h < MIN_EXTENT) return;
    var fill = fillOf(cs, b, model.el, state), shadow = shadowOf(cs);
    var sides = model.collapse ? {} : sideBorders(cs), uniform = model.collapse ? null : uniformBorder(sides);
    var radius = model.collapse ? { tl: 0, tr: 0, br: 0, bl: 0 } : radiusOf(cs, b);
    var rounded = radius.tl || radius.tr || radius.br || radius.bl;
    if (fill || shadow || uniform) {
      var box = b, geometry = rounded ? { type: "roundRect", radius: radius } : { type: "rect" };
      if (uniform) {
        var half = uniform.width / 2;
        box = boxOf(b.x + half, b.y + half, Math.max(0, b.w - uniform.width), Math.max(0, b.h - uniform.width));
        if (rounded) {
          geometry = { type: "roundRect", radius: { tl: Math.max(0, radius.tl - half), tr: Math.max(0, radius.tr - half),
                                                    br: Math.max(0, radius.br - half), bl: Math.max(0, radius.bl - half) } };
        }
      }
      push(state, {
        kind: "shape", box: paintedBox(ctx.frame, box), geometry: geometry, fill: fill || { type: "none" },
        stroke: uniform ? { color: uniform.color, alpha: uniform.alpha, width: uniform.width,
                            dash: dashFor(uniform.style, uniform.width), cap: "butt", join: "miter",
                            headEnd: null, tailEnd: null } : null,
        shadow: shadow, flipH: false, flipV: false,
      }, model.el, ctx);
    }
    reportDroppedShadows(model.el, cs, state, shadow && state.records.length > from ? from : null);
    if (!model.collapse && !uniform && (sides.top || sides.right || sides.bottom || sides.left)) {
      emitSideBorders(model.el, cs, b, ctx, state, model.name);
    }
    if (state.records.length > from) model.surface = true;
    stampOverlays(state, from, model, null, "surface", true);
  }

  /** One shape per cell whose fill stack holds a gradient, on its grid slot, behind the table. */
  function emitCellGradients(model, ctx, state) {
    model.cells.forEach(function (entry) {
      if (!entry.gradient || entry.slotBox.w < MIN_EXTENT || entry.slotBox.h < MIN_EXTENT) return;
      var from = state.records.length;
      push(state, { kind: "shape", box: paintedBox(ctx.frame, entry.slotBox), geometry: { type: "rect" },
                    fill: entry.gradient, stroke: null, shadow: null, flipH: false, flipV: false }, entry.element, ctx);
      stampOverlays(state, from, model, entry, "cellGradient", true);
    });
  }

  /** Paint order inside a cell: by tier, then in document order (a chip is one unit). */
  function orderedItems(items) {
    return items.slice().sort(function (a, b) { return a.tier - b.tier || a.seq - b.seq; });
  }

  /** Every overlay the partition queued, cells row-major, so they follow the table in paint order. */
  function walkCellOverlays(model, record, ctx, state) {
    var cells = model.cells.slice().sort(function (a, b) { return a.r - b.r || a.c - b.c; });
    cells.forEach(function (entry) {
      var cellCtx = ctx, overflow = entry.cs.overflow + " " + entry.cs.overflowX + " " + entry.cs.overflowY;
      if (/hidden|clip/.test(overflow)) {
        cellCtx = { opacity: ctx.opacity, clip: intersectBox(ctx.clip, paintedBox(ctx.frame, entry.paddingBox)),
                    group: null, frame: ctx.frame };
      }
      var start = state.records.length;
      function run(items) {
        orderedItems(items).forEach(function (item) {
          var from = state.records.length;
          if (item.unit) { run(item.unit); return; }
          if (item.walk) {
            walkElement(item.walk, cellCtx, state);
            stampOverlays(state, from, model, entry, function (pushed) {
              if (pushed.kind === "image" || pushed.kind === "raster" || pushed.kind === "chart") return pushed.kind;
              return item.outOfFlow ? "outOfFlow" : "decoration";
            }, true);
            return;
          }
          if (item.chipShapes) emitCellChip(item.chipShapes, item.cs, cellCtx, state);
          else if (item.decorate) emitDecoration(item.decorate, item.cs, cellCtx, state);
          else if (item.text) emitCellText(item, cellCtx, state);
          stampOverlays(state, from, model, entry, item.role, true);
        });
      }
      run(entry.items);
      record.extras["x-wpa"].cells[entry.pr + ":" + entry.pc].overlays = state.records.length - start;
    });
  }

  // ------------------------------------------------------------------ record plumbing

  function markCapture(el, state) {
    if (!el.hasAttribute("data-engine-id")) {
      el.setAttribute("data-engine-id", "n" + ++state.counter);
    }
    return '[data-engine-id="' + el.getAttribute("data-engine-id") + '"]';
  }

  /** One name per element, whoever asks: a box and its four border sides must agree on it. */
  function nameOf(el, state) {
    // A rebuilt pseudo-element is named after its host — `card-3::before` — but never *allocates* a
    // name for it: `tag#N` numbers are handed out in walk order, and a host numbered for its copy's
    // sake would shift the name of every later anonymous element (E3 review m3). A host with no
    // name of its own yet lends its path instead.
    if (el.hasAttribute("data-engine-pseudo") && el.parentElement) {
      var host = el.parentElement;
      return (host.getAttribute("data-name") || host.id || host.__engineName || cssPath(host)) +
             "::" + el.getAttribute("data-engine-pseudo");
    }
    var explicit = el.getAttribute("data-name");
    if (explicit) return explicit;
    if (el.id) return el.id;
    if (el.__engineName) return el.__engineName;
    var tag = el.tagName.toLowerCase();
    state.tagCounts[tag] = (state.tagCounts[tag] || 0) + 1;
    el.__engineName = tag + "#" + state.tagCounts[tag];
    return el.__engineName;
  }

  function sourceOf(el) {
    if (el.hasAttribute("data-engine-pseudo") && el.parentElement) {
      return { path: cssPath(el), tag: el.parentElement.tagName.toLowerCase() + "::" +
               el.getAttribute("data-engine-pseudo"), svg: null };
    }
    return { path: cssPath(el), tag: el.tagName.toLowerCase(), svg: null };
  }

  function push(state, record, el, ctx) {
    record.opacity = ctx.opacity;
    record.rotation = ctx.frame.angle || 0;
    record.group = ctx.group;
    if (!record.source) record.source = sourceOf(el);
    if (!record.name) record.name = nameOf(el, state);
    // R3: the author's `data-placeholder` (the element's own or its nearest ancestor's), for
    // `classify/placeholders.py` pass 1, with the host it was written on, so the texts one title
    // holds (its words and an inline-block chip) are known to be one request, not two.
    var host = record.kind === "text" && el && el.closest ? el.closest("[data-placeholder]") : null;
    if (host && String(host.getAttribute("data-placeholder") || "").trim()) {
      record.extras = record.extras || {};
      record.extras["x-wp4-placeholder"] = String(host.getAttribute("data-placeholder")).trim();
      record.extras["x-wp4-placeholder-host"] = cssPath(host);
    }
    // A clip that does not actually cut is noise — `html,body{overflow:hidden}` is on every slide
    // and would otherwise stamp the canvas rectangle onto every element. A rotated element's box is
    // the box *before* rotation, so an axis-aligned clip taken from it would cut the wrong edge.
    if (ctx.clip && record.box && !ctx.frame.angle && !contains(ctx.clip, record.box)) {
      record.clip = intersectBox(ctx.clip, record.box);
    }
    state.records.push(record);
    return record;
  }

  function diagnose(state, level, source, message, index) {
    state.diagnostics.push({ level: level, source: source, message: message,
                             recordIndex: index === undefined ? null : index });
  }

  // ------------------------------------------------------------------ decoration

  function hasDecoration(el, cs, b) {
    if (fillOf(cs, b, el)) return true;
    if (shadowOf(cs)) return true;
    if (clipPathGeometry(cs, b)) return true;
    var sides = sideBorders(cs);
    return !!(sides.top || sides.right || sides.bottom || sides.left);
  }

  /**
   * The box's own paint: one shape for fill (+ uniform border as a stroke), then one shape per
   * border side when the sides differ. A CSS border sits inside the border box, so a uniform
   * border becomes a centred stroke on a box deflated by half its width — same pixels, one shape.
   */
  function emitDecoration(el, cs, ctx, state, flipH, flipV) {
    walkUnderPaint(el, ctx, state);      // WP-E: z-index < 0 pseudo copies that paint under this box
    var b = borderBox(el, ctx.frame);
    if (b.w < MIN_EXTENT || b.h < MIN_EXTENT) return;
    var fill = fillOf(cs, b, el, state);
    var shadow = shadowOf(cs);
    var sides = sideBorders(cs);
    var uniform = uniformBorder(sides);
    var radius = radiusOf(cs, b);
    var rounded = radius.tl || radius.tr || radius.br || radius.bl;
    var custom = clipPathGeometry(cs, b);

    if (fill || shadow || uniform || custom) {
      var geometry;
      if (custom) geometry = custom;
      else if (uniform) {
        var half = uniform.width / 2;
        var inner = boxOf(b.x + half, b.y + half, Math.max(0, b.w - uniform.width),
                          Math.max(0, b.h - uniform.width));
        geometry = rounded
          ? { type: "roundRect", radius: { tl: Math.max(0, radius.tl - half), tr: Math.max(0, radius.tr - half),
                                           br: Math.max(0, radius.br - half), bl: Math.max(0, radius.bl - half) } }
          : { type: "rect" };
        b = inner;
      } else {
        geometry = rounded ? { type: "roundRect", radius: radius } : { type: "rect" };
      }
      push(state, {
        kind: "shape",
        box: paintedBox(ctx.frame, b),
        geometry: geometry,
        fill: fill || { type: "none" },
        stroke: uniform ? { color: uniform.color, alpha: uniform.alpha, width: uniform.width,
                            dash: dashFor(uniform.style, uniform.width), cap: "butt", join: "miter",
                            headEnd: null, tailEnd: null } : null,
        shadow: shadow,
        flipH: !!flipH,
        flipV: !!flipV,
      }, el, ctx);
    }
    reportDroppedShadows(el, cs, state, shadow ? state.records.length - 1 : null);
    if (uniform) return;
    emitSideBorders(el, cs, borderBox(el, ctx.frame), ctx, state, nameOf(el, state));
  }

  /** The four border widths in px, whatever they paint: a join is shaped by the invisible sides too. */
  function borderWidths(cs) {
    return { top: px(cs.borderTopWidth), right: px(cs.borderRightWidth),
             bottom: px(cs.borderBottomWidth), left: px(cs.borderLeftWidth) };
  }

  /**
   * One shape per painted border side of the box `full` (canvas px, unpainted), where the sides differ.
   *
   * A solid side is the CSS join polygon between the border box and the padding box: the corner is
   * cut on the diagonal from the outer corner to the inner one, so where two painted sides meet each
   * is a trapezoid, and in the zero-size-box idiom (`width:0;height:0`, transparent neighbours) the
   * inner rectangle degenerates to a point and the side is an exact triangle. A side whose two
   * neighbours are both 0 px wide *is* a rectangle and stays `rect`. A dashed or dotted side is a
   * stroked centre line, as before (the mitre is a few px off). Rounded corners are ignored.
   * `skip` names sides not to draw (an inline box split over lines paints its left side on the first
   * fragment only and its right side on the last).
   */
  function emitSideBorders(el, cs, full, ctx, state, base, skip) {
    var JOIN_EPS = 1e-6;   // two points closer than this are one vertex of a join polygon
    var sides = sideBorders(cs);
    var w = borderWidths(cs);
    var x = full.x, y = full.y, W = full.w, H = full.h;
    // The order the points are written in is the order of the path: outer edge first, then inward.
    var outline = {
      top: [[x, y], [x + W, y], [x + W - w.right, y + w.top], [x + w.left, y + w.top]],
      right: [[x + W, y], [x + W, y + H], [x + W - w.right, y + H - w.bottom], [x + W - w.right, y + w.top]],
      bottom: [[x + W, y + H], [x, y + H], [x + w.left, y + H - w.bottom], [x + W - w.right, y + H - w.bottom]],
      left: [[x, y + H], [x, y], [x + w.left, y + w.top], [x + w.left, y + H - w.bottom]],
    };
    var neighbours = { top: ["left", "right"], bottom: ["left", "right"], left: ["top", "bottom"],
                       right: ["top", "bottom"] };
    ["top", "right", "bottom", "left"].forEach(function (which) {
      var side = sides[which];
      if (!side || (skip && skip[which])) return;
      var rect;
      if (which === "top") rect = boxOf(full.x, full.y, full.w, side.width);
      else if (which === "bottom") rect = boxOf(full.x, full.y + full.h - side.width, full.w, side.width);
      else if (which === "left") rect = boxOf(full.x, full.y, side.width, full.h);
      else rect = boxOf(full.x + full.w - side.width, full.y, side.width, full.h);
      var dash = dashFor(side.style, side.width);
      if (dash === "solid") {
        var record = {
          kind: "shape", box: paintedBox(ctx.frame, rect), geometry: { type: "rect" },
          fill: { type: "solid", color: side.color, alpha: side.alpha }, stroke: null, shadow: null,
          name: base + " " + which + " border",
        };
        if (w[neighbours[which][0]] > 0 || w[neighbours[which][1]] > 0) {
          var points = [];
          outline[which].forEach(function (point) {
            var moved = [point[0] + (ctx.frame.dx || 0), point[1] + (ctx.frame.dy || 0)];
            var last = points[points.length - 1];
            if (!last || Math.abs(last[0] - moved[0]) > JOIN_EPS || Math.abs(last[1] - moved[1]) > JOIN_EPS) {
              points.push(moved);
            }
          });
          var first = points[0], end = points[points.length - 1];
          if (points.length > 1 && Math.abs(first[0] - end[0]) <= JOIN_EPS && Math.abs(first[1] - end[1]) <= JOIN_EPS) {
            points.pop();
          }
          if (points.length < 3) return;   // no area: nothing is painted
          var xs = points.map(function (p) { return p[0]; }), ys = points.map(function (p) { return p[1]; });
          var left = Math.min.apply(null, xs), top = Math.min.apply(null, ys);
          record.box = boxOf(left, top, Math.max.apply(null, xs) - left, Math.max.apply(null, ys) - top);
          record.geometry = {
            type: "custom",
            path: [["M", points[0][0], points[0][1]]]
              .concat(points.slice(1).map(function (p) { return ["L", p[0], p[1]]; }), [["Z"]]),
            fillRule: "nonzero",
          };
        }
        push(state, record, el, ctx);
        return;
      }
      // A dashed side cannot be a filled rectangle: draw it as a stroked line down its centre.
      var horizontal = which === "top" || which === "bottom";
      var moved = paintedBox(ctx.frame, rect);
      var line = horizontal
        ? [[moved.x, moved.y + moved.h / 2], [moved.x + moved.w, moved.y + moved.h / 2]]
        : [[moved.x + moved.w / 2, moved.y], [moved.x + moved.w / 2, moved.y + moved.h]];
      push(state, {
        kind: "shape", box: moved, geometry: { type: "line", points: line },
        fill: { type: "none" },
        stroke: { color: side.color, alpha: side.alpha, width: side.width, dash: dash,
                  cap: "butt", join: "miter", headEnd: null, tailEnd: null },
        shadow: null,
        name: base + " " + which + " border",
      }, el, ctx);
    });
  }

  /**
   * The elements whose inline paint belongs to one paragraph source: everything under a text block,
   * but only the run's own elements for an anonymous run — its container also holds the sibling
   * blocks, whose inline boxes (a chip not yet detached) are theirs to paint.
   */
  function inlineScope(paragraphEl, scope) {
    if (!scope) return paragraphEl.querySelectorAll("*");
    var found = [];
    scope.forEach(function (root) {
      found.push(root);
      var below = root.querySelectorAll("*");
      for (var i = 0; i < below.length; i++) found.push(below[i]);
    });
    return found;
  }

  /** Inline boxes that paint something of their own (`mark`, inline-block chips), one per fragment. */
  function emitInlineDecoration(paragraphEl, ctx, state, scope) {
    var inlines = inlineScope(paragraphEl, scope);
    for (var i = 0; i < inlines.length; i++) {
      var el = inlines[i];
      // Two anonymous inline runs under one parent both name that parent as their paragraph
      // element, so without this the chip between them would be painted twice.
      if (state.decorated.indexOf(el) >= 0) continue;
      // A detached box is walked as an element and paints itself through `emitDecoration`.
      if (insideDetached(el, paragraphEl)) continue;
      var cs = getComputedStyle(el);
      if (cs.display === "none" || !isInlineDisplay(cs.display)) continue;
      state.decorated.push(el);
      var rects = el.getClientRects();
      if (!rects.length) continue;
      if (!hasDecoration(el, cs, localBox(ctx.frame, rects[0]))) continue;
      // r1d: the box paints under its own opacity and its inline ancestors' below the paragraph —
      // r1a's rule for the text inside it (`opacityBelow`, contents boxes skipped); `ctx` is the
      // paragraph's (`<span style="opacity:.5;background:…">` was an opaque slab under faded text).
      var own = ownContext(el, paragraphEl, ctx);
      for (var r = 0; r < rects.length; r++) {
        var b = localBox(ctx.frame, rects[r]);
        if (b.w < MIN_EXTENT || b.h < MIN_EXTENT) continue;
        var fill = fillOf(cs, b, el, state);
        var sides = sideBorders(cs);
        var uniform = uniformBorder(sides);
        var radius = radiusOf(cs, b);
        var rounded = radius.tl || radius.tr || radius.br || radius.bl;
        push(state, {
          kind: "shape", box: paintedBox(ctx.frame, b),
          geometry: rounded ? { type: "roundRect", radius: radius } : { type: "rect" },
          fill: fill || { type: "none" },
          stroke: uniform ? { color: uniform.color, alpha: uniform.alpha, width: uniform.width,
                              dash: dashFor(uniform.style, uniform.width), cap: "butt", join: "miter",
                              headEnd: null, tailEnd: null } : null,
          shadow: shadowOf(cs),
        }, el, own);
        // Sides that differ are shapes of their own, as on a block (`border-left: 3px solid` on a chip).
        // An inline box broken over lines draws its start side on its first fragment only, its end
        // side on its last (box-decoration-break: slice).
        if (!uniform) {
          var rtl = cs.direction === "rtl";
          emitSideBorders(el, cs, b, own, state, nameOf(el, state), {
            left: r > 0 && !rtl || r < rects.length - 1 && rtl,
            right: r < rects.length - 1 && !rtl || r > 0 && rtl,
          });
        }
      }
    }
  }

  // ------------------------------------------------------------------ the walk

  function stackingOrder(items) {
    var negative = [], flow = [], auto = [], positive = [];
    items.forEach(function (item, index) {
      if (item.type !== "element") { flow.push([0, index, item]); return; }
      var cs = item.cs;
      if (cs.position === "static") { flow.push([0, index, item]); return; }
      var z = cs.zIndex === "auto" ? null : parseInt(cs.zIndex, 10);
      if (z === null || isNaN(z) || z === 0) { auto.push([0, index, item]); return; }
      (z < 0 ? negative : positive).push([z, index, item]);
    });
    function ordered(list) {
      return list.sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; })
                 .map(function (entry) { return entry[2]; });
    }
    return ordered(negative).concat(ordered(flow), ordered(auto), ordered(positive));
  }

  /**
   * `el`'s children as walk items: runs of inline content (`{type: "inline", nodes}`) and everything
   * else (`{type: "element", node, cs}`), in DOM order. R5: a `display: contents` child generates no
   * box — its children are laid out as `el`'s own (flex items of a flex row, say) — so they are
   * spliced in its place, and nothing paints the wrapper itself.
   */
  function childItems(el) {
    var items = [];
    collectItems(el, items, { run: null });
    return items;
  }

  function collectItems(el, items, open) {
    for (var i = 0; i < el.childNodes.length; i++) {
      var node = el.childNodes[i];
      if (node.nodeType === 3) {
        if (!node.data) continue;
        if (!node.data.trim()) { if (open.run) open.run.nodes.push(node); continue; }
        if (!open.run) { open.run = { type: "inline", nodes: [] }; items.push(open.run); }
        open.run.nodes.push(node);
        continue;
      }
      if (node.nodeType !== 1) continue;
      var cs = getComputedStyle(node);
      if (cs.display === "none") continue;
      if (node.tagName === "ASIDE" && node.classList.contains("notes")) continue;
      if (node.hasAttribute("data-engine-probe")) continue;
      if (cs.display === "contents" && !isSpecial(node)) { collectItems(node, items, open); continue; }
      var inline = isInlineDisplay(cs.display) && !isSpecial(node) && !isOutOfFlow(cs);
      if (inline) {
        if (!open.run) { open.run = { type: "inline", nodes: [] }; items.push(open.run); }
        open.run.nodes.push(node);
        continue;
      }
      open.run = null;
      items.push({ type: "element", node: node, cs: cs });
    }
  }

  /**
   * A text record's first line, quoted the way a person would quote it.
   *
   * `engine/verify/text_layout.py: quote_of` is the same rule in Python, because the two measured
   * text diagnostics are computed in different places — overflow here, where the author's declared
   * box exists, and overlap there, once chart recognition has taken the chart labels away — and
   * their messages must read as one voice.
   */
  function quoteOf(paragraphs) {
    var first = paragraphs.length ? paragraphs[0] : null;
    var line = first && first.lines.length ? first.lines[0] : null;
    return quoteText((line ? line.runs : []).map(function (run) { return run.text || ""; }).join(""));
  }

  /** Any text, quoted by `quoteOf`'s rule: whitespace collapsed, cut at a word boundary (WP-F). */
  function quoteText(text) {
    text = String(text || "").replace(/\s+/g, " ").replace(/^ | $/g, "");
    if (text.length <= QUOTE_MAX) return text;
    var cut = text.lastIndexOf(" ", QUOTE_MAX);
    if (cut < QUOTE_MIN) cut = QUOTE_MAX;
    return text.slice(0, cut) + "\u2026";
  }

  /**
   * Does this text run below the box its author sized? Two kinds of host are asked, innermost first.
   *
   * A card is one text record made of several paragraphs under a container the author gave a
   * height, and those paragraphs are themselves `height: auto`, so they cannot overflow anything.
   * Each paragraph is therefore checked against *its own* line boxes, and the container — plus any
   * static ancestor up to and including the nearest out-of-flow one — against the union of all of
   * them. Comparing the union with each paragraph's element instead reports every KPI value as
   * overflowing its own one-line `div`, which is the false positive this shape exists to avoid.
   *
   * Only the bottom edge is measured. A `white-space: nowrap` line wider than its box is an
   * authoring choice the emitter preserves with `wrap: false`, and text in normal flow never
   * starts above the box it is in.
   *
   * A `display: contents` ancestor generates no box (its rect is all zeros), so it is no box the
   * text could run out of: the walk up skips it and judges the nearest ancestor that has one
   * (`layoutParentOf`). r1e: a detached chip under a contents wrapper "ran 231 px below its box …
   * the box is 0 px tall".
   */
  function checkTextOverflow(spans, container, ctx, state, union, paragraphs) {
    if (!spans.length) return;
    var hosts = [], lines = 0;
    spans.forEach(function (entry) {
      lines += entry.lines;
      // An anonymous inline run names the container as its "paragraph"; that is the union's host.
      if (entry.el !== container) {
        hosts.push({ el: entry.el, span: entry.box, lineHeightPx: entry.lineHeightPx,
                     lines: entry.lines });
      }
    });
    var spilling = spans[spans.length - 1].lineHeightPx;
    for (var host = container; host && host.nodeType === 1; host = layoutParentOf(host)) {
      var tag = host.tagName.toLowerCase();
      // `html,body{height:720px;overflow:hidden}` is on every slide: the canvas is not a box an
      // author sized for this text, and reporting it would fire on every long slide.
      if (tag === "body" || tag === "html") break;
      hosts.push({ el: host, span: union, lineHeightPx: spilling, lines: lines });
      if (isOutOfFlow(getComputedStyle(host))) break;
    }
    for (var i = 0; i < hosts.length; i++) {
      var candidate = hosts[i];
      var b = borderBox(candidate.el, ctx.frame);
      var over = candidate.span.y + candidate.span.h - (b.y + b.h);
      if (over <= Math.max(OVERFLOW_MIN_PX, OVERFLOW_HALF_LINE * candidate.lineHeightPx)) continue;
      diagnose(state, "warn", "text-overflow",
               "\u201C" + quoteOf(paragraphs) + "\u201D runs " + Math.round(over) +
               " px below its box: " + candidate.lines +
               (candidate.lines === 1 ? " line needs " : " lines need ") +
               Math.round(candidate.span.h) + " px, the box is " + Math.round(b.h) + " px tall",
               state.records.length - 1);
      return;
    }
  }

  // ------------------------------------------------------------------ paragraph runs (WP-B)
  //
  // Master brief §11 as amended: a text element is a maximal run of consecutive, vertically stacked,
  // horizontally overlapping in-flow sibling blocks and anonymous inline runs, whose content is
  // inline-only, under one parent, with no intervening non-text sibling; each block or run is one
  // paragraph; atomic inline boxes with their own horizontal geometry are elements of their own.
  // A paragraph source is `{el, nodes?, item, hug?, peers?, segment?, boxBefore?, boxAfter?, scope?,
  // noBullet?, bulletHost?, after?}`: `el` the block (or, for an anonymous run, its container),
  // `nodes` its text nodes when it is not the whole block, `item` the layout item it belongs to (the
  // block, or the anonymous run), `hug` when its box is its own line boxes rather than the block's
  // content box (R2) — or `peers`, the container's other in-flow items, to decide that once its lines
  // are measured —, `segment` when it is text beside a detached box (R3) and `boxBefore`/`boxAfter`
  // the boxes it sits beside, `scope` the elements whose inline paint it owns, `noBullet` for a list
  // item's later segments, `bulletHost` a list item whose marker it carries although it is not the
  // item (R8), `after` WP-E's queue. `pushParagraph` measures it once and adds `cs`, `lines`, `span`,
  // `box`, `outer`, `bullet`, `textLeft`, `split`, `tracks`, `continues`; `flushText` reuses them and
  // walks each entry's `then` — the detached boxes that sit after its text — once its record is out.

  // ---- r1a: layout gaps carried by a space; an element's own opacity folded into what it emits
  //
  // A PowerPoint paragraph has no geometry between runs, so a gap the browser made with layout — a
  // hand-made bullet's `margin-right`, a label's `margin-left`, a legend swatch, a padded span —
  // either closes (glued: `•Online portal:`, 66 survey collisions at r1) or, with a space beside
  // it, shrinks to that space (`padded-inline`: 12 px). A space *can* carry it: its advance plus
  // `letterSpacingPx` (written as `spc`) is exactly the gap, in PowerPoint and PptxRender alike.
  // Measured before choosing (r1a.md): 46 seams, Arial and Calibri, three candidates — separately
  // placed boxes, `spc` on the glyph before the gap, a sized space — rendered by both. The two
  // `spc` forms agree with the browser within 1.3 px in PowerPoint on every seam; separate boxes
  // do too but cannot place a seam in a wrapping line (up to 27 px off). The sized space wins on
  // what the file then says: `• Online portal:`, `Target: Business`, text a reader can copy,
  // search and edit, where the glyph form writes `•Online portal:`.

  /**
   * R6: carry the gap between the last run of a line (`runs`, `before` = the previous fragment)
   * and the text of the next fragment (`text`, `style`, its element `el`, starting at `left`),
   * `gap` px after it, on a space: the space before the gap, the text's own leading space, or —
   * glued — a space inserted and sized to the gap. Returns false when it cannot (no run before it,
   * a font the canvas cannot measure: the seam stays glued and R6 reports it), true when the gap
   * went onto a run it pushed or widened, `{rest, from}` when the text's leading space took it —
   * the text after that space and where it starts.
   */
  function carryGap(runs, before, text, style, el, left, gap) {
    var previous = runs[runs.length - 1];
    if (!previous) return false;
    var space, width;
    if (/\s$/.test(before.text) && /\s$/.test(previous.text)) {
      // The space before the gap takes it (`and <span style="padding:0 12px">spaced</span>`).
      if (previous.text.length === 1) {
        previous.letterSpacingPx = (previous.letterSpacingPx || 0) + gap;
        previous._right = left;
        previous._key = null;
        return true;
      }
      width = spaceAdvance(previous._el);
      if (width === null) return false;
      width += previous.letterSpacingPx || 0;
      space = gapRun(previous, previous.text.slice(-1), previous._el);
      previous.text = previous.text.slice(0, -1);
      previous._right = Math.max(previous._left, before.right - width);
      space.letterSpacingPx = (previous.letterSpacingPx || 0) + gap;
      space._left = previous._right;
      space._right = left;
      runs.push(space);
      return true;
    }
    if (/^\s/.test(text)) {
      // The text's own leading space takes it (`<span style="padding:0 12px">spaced</span> and`).
      width = spaceAdvance(el);
      if (width === null) return false;
      width += style.letterSpacingPx || 0;
      space = gapRun(style, text[0], el);
      space.letterSpacingPx = (style.letterSpacingPx || 0) + gap;
      space._left = before.right;
      space._right = left + width;
      runs.push(space);
      return { rest: text.slice(1), from: left + width };
    }
    // Glued: a space inserted between the two, in the smaller of their styles (the line's metrics
    // are untouched and the correction is the smaller), sized to the gap by its letter spacing —
    // negative where the gap is narrower than a space. Nothing of it paints: no underline, no
    // strike, no baseline shift (PowerPoint shrinks a superscript's advance too), and a link only
    // where both sides are the same link.
    var smaller = Number(style.sizePx) < Number(previous.sizePx);
    var host = smaller ? el : previous._el;
    width = spaceAdvance(host);
    if (width === null) return false;
    space = gapRun(smaller ? style : previous, " ", host);
    space.underline = false;
    space.strike = false;
    space.baseline = "normal";
    space.baselineShiftPx = 0;
    space.href = previous.href && previous.href === style.href ? previous.href : null;
    space.letterSpacingPx = gap - width;
    space._left = before.right;
    space._right = left;
    runs.push(space);
    return true;
  }

  /** A run of `text` (one space) in the style of `source` (a run or a `runStyle`), no fill, merging with nothing. */
  function gapRun(source, text, el) {
    var run = {};
    Object.keys(source).forEach(function (key) {
      if (key[0] !== "_" && key !== "text") run[key] = source[key];
    });
    run.text = text;
    run._key = null;
    run._el = el;
    return run;
  }

  var SPACE_CONTEXT = null;

  /**
   * The advance of one space in `el`'s font, as the browser draws it without word or letter spacing
   * (`measureText`, the same shaper as layout) — what PowerPoint draws for a space in the face the
   * file names. Null when there is no canvas to measure with.
   */
  function spaceAdvance(el) {
    if (!el) return null;
    if (!SPACE_CONTEXT) {
      var canvas = document.createElement("canvas");
      SPACE_CONTEXT = canvas.getContext ? canvas.getContext("2d") : null;
      if (!SPACE_CONTEXT) return null;
    }
    var cs = getComputedStyle(el);
    SPACE_CONTEXT.font = [cs.fontStyle, cs.fontWeight, cs.fontSize, cs.fontFamily].join(" ");
    var width = SPACE_CONTEXT.measureText(" ").width;
    return isFinite(width) && width > 0 ? width : null;
  }

  /**
   * The product of the `opacity` of `el` and its ancestors below `stop` — what walking them as
   * elements would have multiplied into the context (`walkTransformed`), the same chain E's
   * `pseudoContext` folds for a pseudo copy. A `display: contents` element generates no box, so
   * its `opacity` paints nothing (measured in Edge: text under a contents wrapper at 0.2 is fully
   * opaque) and is skipped.
   */
  function opacityBelow(el, stop) {
    var product = 1;
    for (var node = el; node && node !== stop && node.nodeType === 1 && node !== document.body; node = node.parentElement) {
      var cs = getComputedStyle(node);
      if (cs.display === "contents") continue;
      var value = parseFloat(cs.opacity);
      if (!isNaN(value)) product *= value;
    }
    return product;
  }

  /** `ctx` with the opacity of `el` and its ancestors below `stop` folded in (`ctx` itself when 1). */
  function ownContext(el, stop, ctx) {
    var factor = el === stop ? 1 : opacityBelow(el, stop);
    if (factor === 1) return ctx;
    return { opacity: ctx.opacity * factor, clip: ctx.clip, group: ctx.group, frame: ctx.frame };
  }

  /**
   * A run style (or a run) with `factor` folded into its alpha — and into its gradient fill's
   * stops, which the emitter reads instead of the colour. `style` itself when `factor` is 1.
   */
  function fadedStyle(style, factor) {
    if (factor === 1) return style;
    var faded = {};
    Object.keys(style).forEach(function (key) { faded[key] = style[key]; });
    faded.alpha = (style.alpha === undefined ? 1 : style.alpha) * factor;
    if (style._fill) {
      faded._fill = JSON.parse(JSON.stringify(style._fill));
      (faded._fill.stops || []).forEach(function (stop) {
        stop.alpha = (stop.alpha === undefined ? 1 : stop.alpha) * factor;
      });
    }
    return faded;
  }

  /** A text node's style with the opacity of the inline boxes between it and its paragraph folded in. */
  function seenStyle(style, el, paragraphEl) {
    return fadedStyle(style, el === paragraphEl ? 1 : opacityBelow(el, paragraphEl));
  }

  /**
   * R1: measure a paragraph source once and add it to the pending run — after flushing the run when
   * the source does not stack under its last paragraph. A source with no measurable line never
   * enters the run (a whitespace-only segment between two chips, an empty block). Returns the last
   * entry it pushed, or null.
   *
   * A source can enter as several paragraphs of one element (pieces), cut between two of its lines:
   * - R8: a bulleted, left-aligned item whose first line starts right of the item's content edge (a
   *   swatch or chip before the text, or a `text-indent`) and wraps. One bulleted paragraph would put
   *   every continuation line where the first line's text starts; the browser put them at the edge.
   * - R3: a segment whose first line continues a browser line after a detached box, or whose last
   *   line ends one before a box. Such a part-line starts (or ends) at the box, while the segment's
   *   other lines are whole lines of the paragraph: one alignment cannot place both (a centred
   *   paragraph's part-line is not centred on the paragraph; a right-aligned one's before a box does
   *   not end at its edge; a left-aligned one's after a box would need a first-line indent, which
   *   the text row's band model does not read — B1 critique #2 and #5), so such a part-line is a
   *   paragraph of its own, placed by its own margins.
   */
  function pushParagraph(run, source, ctx) {
    var el = source.el;
    var cs = getComputedStyle(el);
    var nodes = source.nodes || textNodesOf(el);
    if (!nodes.length) return null;
    // WP-F: only what the reader sees is measured; what the clip hid reaches `trimClipped`.
    var clip = paragraphClip(el, source.item === el, ctx), measured = measureParagraph(nodes, el, ctx.frame, clip);
    var lines = measured.lines;
    if (!lines.length) { keepHidden(run, source, clip, measured.hidden); return null; }
    // r1a: a text block's own opacity (flexgrid's `.lbl{opacity:.85}`) is on its runs: the element
    // takes its container's context, and one element may hold paragraphs of different opacity.
    var faded = run.container ? opacityBelow(el, run.container) : 1;
    if (faded !== 1) {
      lines.forEach(function (line) {
        line.runs = line.runs.map(function (r) { return fadedStyle(r, faded); });
      });
    }
    // The list item whose marker this paragraph carries: its own `li`, or — for an item whose only
    // text is inside detached boxes — the item the first box belongs to (B1 critique #11).
    var host = source.bulletHost || (el.tagName === "LI" && !source.noBullet ? el : null);
    var bullet = host ? bulletOf(host, getComputedStyle(host), ctx.frame) : null;
    var border = borderBox(el, ctx.frame);
    var content = contentBox(border, cs);
    // R2: a run shares its container's width only with what sits beside it (B1 critique #10).
    var hug = !!source.hug || sharesLineBand(source.peers, lines, ctx);

    var cuts = [], cause = null, align = resolveAlign(cs);
    var offset = lines[0].box.x - content.x;
    if (bullet && lines.length >= 2 && (align === "left" || align === "justify") &&
        offset > DETACH_GEOMETRY_PX) {
      cuts.push(1);
      var indent = /px$/.test(cs.textIndent) ? px(cs.textIndent) : 0;
      cause = indent > DETACH_GEOMETRY_PX && Math.abs(offset - indent) <= ALIGN_TOL ? "text-indent" : "inline box";
    }
    if (source.segment && lines.length >= 2) {
      // Only where the paragraph's alignment cannot place the part-line with the whole lines: after
      // a box unless right-aligned (its start is the box's edge, not the paragraph's), before one
      // unless left-aligned or justified (its end is the box's edge).
      var last = lines[lines.length - 1].box;
      if (align !== "right" && source.boxBefore &&
          sameBand(borderBox(source.boxBefore, ctx.frame), lines[0].box)) cuts.push(1);
      if (align !== "left" && align !== "justify" && source.boxAfter &&
          sameBand(borderBox(source.boxAfter, ctx.frame), last)) cuts.push(lines.length - 1);
    }
    var bounds = [0];
    cuts.sort(function (a, b) { return a - b; }).forEach(function (cut) {
      if (cut > bounds[bounds.length - 1] && cut < lines.length) bounds.push(cut);
    });
    bounds.push(lines.length);

    var pushed = null;
    for (var k = 0; k + 1 < bounds.length; k++) {
      var slice = lines.slice(bounds[k], bounds[k + 1]);
      var lastLine = slice[slice.length - 1].box;
      var first = k === 0, final = k + 2 === bounds.length;
      var pieceBullet = first ? bullet : null;
      var pieceHug = first ? hug : true;
      var span = slice.reduce(function (acc, line) { return unionBox(acc, line.box); }, null);
      // The box is the block's content width with the lines' own vertical extent: hugging the glyphs
      // would re-wrap the text in PowerPoint and lose its alignment, while the block's own height
      // says nothing about where the text sits. A hugged entry (R2) shared its block with a box or
      // with other text, so the block's width is not its own: it hugs its lines. A bulleted entry
      // keeps the list's left edge, where the bullet is painted.
      var block = pieceHug ? span : content;
      var left = Math.min(pieceBullet ? pieceBullet._listLeft : block.x, span.x);
      var right = Math.max(block.x + block.w, span.x + span.w);
      var outer = source.nodes ? span : border;
      var top = first ? outer.y : slice[0].box.y;
      var bottom = final ? outer.y + outer.h : lastLine.y + lastLine.h;
      var entry = {
        el: el, cs: cs, nodes: source.nodes, item: source.item, hug: pieceHug, segment: !!source.segment,
        scope: source.scope, lines: slice,
        span: span, box: boxOf(left, span.y, right - left, span.h), bullet: pieceBullet,
        outer: boxOf(outer.x, top, outer.w, Math.max(0, bottom - top)),
        split: k === 1 && cause !== null && bounds[1] === 1 ? cause : null,
        textLeft: pieceBullet ? content.x : null,
        // A block is its own grid item; an anonymous run's item is its text.
        tracks: run.layout ? run.layout.tracksOf(source.item === el ? border : span) : null,
        // A later piece continues the lines of the one before it: one browser paragraph, stacked by
        // construction, whatever its hugged boxes say.
        continues: !first,
        // WP-E's queue of positioned pseudo children, walked once after the record: first piece only.
        after: first ? source.after : undefined,
      };
      if (pieceBullet) delete pieceBullet._listLeft;
      var previous = run.pending[run.pending.length - 1];
      if (previous && !run.layout && sameColumnFlow(previous, entry)) entry.continues = true;   // WP-F
      if (previous && !entry.continues && !stacked(previous, entry, run.layout)) run.flush();
      run.pending.push(entry);
      pushed = entry;
    }
    if (pushed) { pushed.clip = clip; pushed.hidden = measured.hidden; }   // WP-F: one tally per source
    return pushed;
  }

  /** Do two boxes share a line band — a vertical overlap of more than half the shorter? */
  function sameBand(a, b) {
    var overlap = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
    return overlap > 0.5 * Math.min(a.h, b.h);
  }

  /** R2: does any of `peers` (the container's other in-flow items) sit beside one of these lines? */
  function sharesLineBand(peers, lines, ctx) {
    return (peers || []).some(function (peer) {
      var b = borderBox(peer, ctx.frame);
      if (b.w < MIN_EXTENT && b.h < MIN_EXTENT) return false;
      return lines.some(function (line) { return sameBand(b, line.box); });
    });
  }

  /**
   * R1: does `next` sit under `previous`? Its first line box starts no higher than `previous`'s
   * last line box ends (less `STACK_TOL` of the shorter of the two), and the two paragraph boxes
   * overlap horizontally. A flex/grid row, a number beside its unit (the unit's line box sits inside
   * the number's), a grid's row wrap (below, but in another column) all fail it.
   *
   * Two items of one flex or grid container stack only where that layout stacks them (B1 critique
   * #9): never in a flex row (a wrapped flex line is a row, not a stack), never where `order` moves
   * items away from the DOM order this walk follows, and in a grid only within the same column tracks
   * (a header spanning three columns does not stack onto the first cell under it).
   */
  function stacked(previous, next, layout) {
    if (layout && previous.item !== next.item) {
      if (layout.kind === "row" || layout.ordered) return false;
      if (layout.kind === "grid" && previous.tracks !== next.tracks) return false;
    }
    var above = previous.lines[previous.lines.length - 1].box, first = next.lines[0].box;
    var tolerance = STACK_TOL * Math.min(above.h, first.h);
    var below = next.span.y >= previous.span.y + previous.span.h - tolerance;
    var overlap = Math.min(previous.box.x + previous.box.w, next.box.x + next.box.w) -
                  Math.max(previous.box.x, next.box.x);
    return below && overlap > 0;
  }

  /**
   * How `el` lays its children out, for `stacked()`: `{kind: "row" | "column" | "grid", ordered,
   * tracksOf(box)}`, or null for block flow. `tracksOf` names the grid column tracks a box lies in
   * (`"1-2"`), from the resolved `grid-template-columns`; null when they cannot be read.
   */
  function containerLayout(el, ctx) {
    var cs = getComputedStyle(el);
    var flex = cs.display === "flex" || cs.display === "inline-flex";
    var grid = cs.display === "grid" || cs.display === "inline-grid";
    if (!flex && !grid) return null;
    var ordered = false;
    for (var i = 0; i < el.children.length; i++) {
      var child = getComputedStyle(el.children[i]);
      if (child.display !== "none" && !isOutOfFlow(child) && child.order && child.order !== "0") ordered = true;
    }
    var kind = flex ? (/column/.test(cs.flexDirection) ? "column" : "row") : "grid";
    var tracks = grid ? gridTracks(el, cs, ctx) : null;
    return {
      kind: kind, ordered: ordered,
      tracksOf: function (b) {
        if (!tracks) return null;
        var hit = [];
        tracks.forEach(function (track, index) {
          if (Math.min(b.x + b.w, track[1]) - Math.max(b.x, track[0]) > DETACH_GEOMETRY_PX) hit.push(index);
        });
        return hit.length ? hit[0] + "-" + hit[hit.length - 1] : null;
      },
    };
  }

  /** A grid's column tracks as `[left, right]` in local px, or null when the template is not all px. */
  function gridTracks(el, cs, ctx) {
    var tokens = String(cs.gridTemplateColumns || "").replace(/\[[^\]]*\]/g, " ").trim().split(/\s+/);
    if (!tokens.length || tokens.some(function (token) { return !/^-?[\d.]+px$/.test(token); })) return null;
    var sizes = tokens.map(px), gap = px(cs.columnGap) || 0;
    var content = contentBox(borderBox(el, ctx.frame), cs);
    var total = sizes.reduce(function (a, b) { return a + b; }, 0) + gap * (sizes.length - 1);
    var free = Math.max(0, content.w - total), justify = String(cs.justifyContent || "");
    var shift = /center/.test(justify) ? free / 2 : /(^|\s)(end|flex-end|right)$/.test(justify) ? free : 0;
    var rtl = cs.direction === "rtl", cursor = rtl ? content.x + content.w - shift : content.x + shift;
    return sizes.map(function (size) {
      var track = rtl ? [cursor - size, cursor] : [cursor, cursor + size];
      cursor += rtl ? -(size + gap) : size + gap;
      return track;
    });
  }

  /**
   * R3: does a detached box sit under the pending run (by `stacked()`'s vertical test) rather than
   * beside its last paragraph? Then it lies between that run and anything after it.
   */
  function belowPending(run, el, ctx) {
    var last = run.pending[run.pending.length - 1];
    if (!last) return false;
    var b = borderBox(el, ctx.frame);
    var above = last.lines[last.lines.length - 1].box;
    return b.y >= last.span.y + last.span.h - STACK_TOL * Math.min(above.h, b.h);
  }

  /** The box whose layout placed `el`: its parent, skipping `display: contents` wrappers (R5). */
  function layoutParentOf(el) {
    var parent = el.parentElement;
    while (parent && getComputedStyle(parent).display === "contents") parent = parent.parentElement;
    return parent;
  }

  /**
   * A CSS alignment keyword as the paragraph alignment it imposes, or null (stretch, space-*, and
   * the plain left start, where `text-align` keeps its say). `rtl` is the container's inline
   * direction; `flipped` whether the axis the keyword aligns on starts at the right (a flex row's
   * main axis under `row-reverse` xor `rtl`, a flex column's cross axis under `rtl`) —
   * `flex-start`/`flex-end` follow the axis, `start`/`end` the inline direction. An end that the
   * flip puts on the left is `left`, whatever the paragraph's own (rtl) `text-align` says (B1
   * critique #13).
   */
  function alignKeyword(value, flipped, rtl) {
    var keyword = String(value || "").replace(/^(legacy|safe|unsafe)\s+/, "").replace(/\s+legacy$/, "");
    if (keyword === "center") return "center";
    if (keyword === "right") return "right";
    if (keyword === "end" || keyword === "self-end") return rtl ? "left" : "right";
    if (keyword === "start" || keyword === "self-start") return rtl ? "right" : null;
    if (keyword === "flex-end") return flipped ? "left" : "right";
    if (keyword === "flex-start") return flipped ? "right" : null;
    return null;
  }

  /**
   * R4: what the container imposes on this paragraph's box, horizontally — or null. An anonymous
   * run is an anonymous item of its own container; a block is an item of its layout parent. Under
   * `justify-content: space-between` the last of two or more items sits at the main end.
   */
  function layoutAlignOf(source, container) {
    var item = source.el === container ? null : source.el;
    var parent = item ? layoutParentOf(item) : container;
    if (!parent) return null;
    var ps = getComputedStyle(parent), own = item ? getComputedStyle(item) : null;
    var rtl = ps.direction === "rtl";
    if (ps.display === "flex" || ps.display === "inline-flex") {
      if (/column/.test(ps.flexDirection)) {
        var cross = own && own.alignSelf !== "auto" ? own.alignSelf : ps.alignItems;
        return alignKeyword(cross, rtl, rtl);
      }
      var flipped = (ps.flexDirection === "row-reverse") !== rtl;
      if (/space-between/.test(ps.justifyContent) && lastItemOf(parent, source)) return flipped ? "left" : "right";
      return alignKeyword(ps.justifyContent, flipped, rtl);
    }
    if (ps.display === "grid" || ps.display === "inline-grid") {
      var self = own && own.justifySelf !== "auto" ? own.justifySelf : ps.justifyItems;
      return alignKeyword(self, rtl, rtl);
    }
    return null;
  }

  /** Is `source` the last of two or more in-flow items of `parent` (a block, or an anonymous run)? */
  function lastItemOf(parent, source) {
    var items = childItems(parent).filter(function (item) {
      return item.type === "inline" || !isOutOfFlow(item.cs);
    });
    if (items.length < 2) return false;
    var last = items[items.length - 1];
    if (last.type === "element") return last.node === source.el || last.node.contains(source.el);
    var probe = source.nodes && source.nodes[0];
    return !!probe && last.nodes.some(function (node) { return node === probe || node.contains(probe); });
  }

  /**
   * R4: each line's extent as the browser laid it out — its text, widened by the inline boxes the
   * browser put on the same line inside this paragraph's box (a swatch, a padded span) and, on a
   * block's first line, by a positive `text-indent`. Text after a leading swatch in a shrink-wrapped
   * block ends where the block ends; it is not right-aligned for that.
   */
  function lineExtents(source) {
    var own = source.box, rects = [];
    var inlines = inlineScope(source.el, source.scope);
    for (var i = 0; i < inlines.length; i++) {
      var el = inlines[i];
      if (insideDetached(el, source.el)) continue;
      var display = getComputedStyle(el).display;
      if (!isInlineDisplay(display) || display === "contents") continue;
      var list = el.getClientRects();
      for (var r = 0; r < list.length; r++) {
        var rect = box(list[r]);
        if (rect.w > 0 && rect.x >= own.x - ALIGN_TOL && rect.x + rect.w <= own.x + own.w + ALIGN_TOL) {
          rects.push(rect);
        }
      }
    }
    var indent = !source.nodes && /px$/.test(source.cs.textIndent) ? Math.max(0, px(source.cs.textIndent)) : 0;
    return source.lines.map(function (line, index) {
      var left = line.box.x - (index === 0 && !source.split ? indent : 0), right = line.box.x + line.box.w;
      rects.forEach(function (rect) {
        var overlap = Math.min(rect.y + rect.h, line.box.y + line.box.h) - Math.max(rect.y, line.box.y);
        if (overlap > 0.5 * Math.min(rect.h, line.box.h)) {
          left = Math.min(left, rect.x);
          right = Math.max(right, rect.x + rect.w);
        }
      });
      return { left: left, right: right };
    });
  }

  /**
   * R4: a paragraph's alignment from where its lines sit in the element's union box, with the
   * container's layout as the tie-break for a paragraph that fills the box (the wider of a centred
   * flex column's number and label). `text-align` alone misses every flex- or grid-centred text.
   * A bulleted paragraph is measured from where its text starts, not from the bullet. A segment
   * beside a detached box keeps its block's `text-align`: its offset in the element is the box
   * before it, not an alignment (a heading after a number badge is not right-aligned because its
   * text happens to reach the element's right edge).
   */
  function deriveAlign(source, union, container) {
    var declared = resolveAlign(source.cs);
    if (source.segment) return declared;
    var x = source.textLeft !== null ? Math.max(union.x, source.textLeft) : union.x;
    var x2 = union.x + union.w;
    var leftFlush = true, rightFlush = true, centred = true;
    lineExtents(source).forEach(function (extent) {
      var before = extent.left - x, after = x2 - extent.right;
      if (before > ALIGN_TOL) leftFlush = false;
      if (after > ALIGN_TOL) rightFlush = false;
      if (Math.abs(before - after) > ALIGN_TOL) centred = false;
    });
    if (leftFlush && rightFlush) return layoutAlignOf(source, container) || declared;
    if (leftFlush) return declared === "justify" ? "justify" : "left";
    if (rightFlush) return "right";
    if (centred) return "center";
    return declared;   // ragged: the emitter's margins carry the residual
  }

  /**
   * R4: the element's vertical anchor, from its first paragraph's lines inside the block that holds
   * them (the container, for an anonymous run). Only a flex or grid box (or a table cell, by its
   * `vertical-align`) can place its text anywhere but the top — block flow always starts at the
   * top, and there the gap between the line boxes and the block is measurement residue (a `sup`
   * stretching a line box the IR line does not), not alignment. A box with no slack says nothing
   * either, and a hugged entry's box is its lines: `top`.
   */
  function deriveAnchor(source, ctx) {
    if (source.hug) return "top";
    if (!/^(inline-)?(flex|grid)$/.test(source.cs.display)) {
      // `vertical-align` on a table cell is the other common way a slide centres a label in a box.
      // Unlike a flex item, the text usually *is* the cell's content, so read the cell's own style
      // as well as the parent's.
      var parentStyle = source.el.parentElement ? getComputedStyle(source.el.parentElement) : null;
      var cell = source.cs.display === "table-cell" ? source.cs
               : parentStyle && parentStyle.display === "table-cell" ? parentStyle : null;
      if (cell && cell.verticalAlign === "middle") return "middle";
      if (cell && cell.verticalAlign === "bottom") return "bottom";
      return "top";
    }
    var content = contentBox(borderBox(source.el, ctx.frame), source.cs);
    var topGap = source.span.y - content.y;
    var slack = content.h - source.span.h;
    if (slack <= 2 * ANCHOR_TOL) return "top";
    // The nearest of the three places the browser puts a line box: 0, half the slack, all of it.
    // The IR's line box is rebuilt from the glyphs' content area and the declared line-height, so
    // a flex-centred 14 px line can sit a pixel off true centre; the nearest position is robust to that.
    var toTop = Math.abs(topGap), toMiddle = Math.abs(topGap - slack / 2), toBottom = Math.abs(slack - topGap);
    if (toTop <= toMiddle && toTop <= toBottom) return "top";
    return toMiddle <= toBottom ? "middle" : "bottom";
  }

  function round2(value) {
    return Math.round(value * 100) / 100;
  }

  /**
   * The paragraph sources gathered so far become one `text` element (element-boundary rule).
   *
   * `container` is the parent the run of paragraphs sits under, and it — not the first paragraph —
   * names the element and is its `source`: an author writes `data-name` on the card, not on each
   * line inside it, and "which card did this text come from" is the question a defect asks.
   * Every source arrives measured by `pushParagraph`; nothing here opens a second Range pass.
   */
  function flushText(pending, container, ctx, state) {
    if (emitColumnRecords(pending, container, ctx, state)) return;   // WP-F: CSS columns; wholly hidden text
    if (!pending.length) return;
    var paragraphs = [], spans = [], runBoxes = [], seams = [], transforms = {}, wrap = true, split = false;
    var previousBottom = null;
    // The out-of-flow pseudo-element copies of these paragraphs (WP-E grouping rule) paint after
    // the text — positioned descendants after in-flow content, as CSS paints them.
    var after = pending.reduce(function (acc, source) { return acc.concat(source.after || []); }, []);
    var union = pending.reduce(function (acc, source) { return unionBox(acc, source.box); }, null);

    pending.forEach(function (source, index) {
      var el = source.el, cs = source.cs, lines = source.lines;
      spans.push({ el: el, box: source.box, lineHeightPx: lineHeightPx(cs), lines: lines.length });

      var spaceBefore = previousBottom === null ? 0 : Math.max(0, source.outer.y - previousBottom);
      previousBottom = source.outer.y + source.outer.h;

      transforms[cs.textTransform] = true;
      if (cs.whiteSpace === "nowrap" || cs.whiteSpace === "pre") wrap = false;
      if (source.split) split = source.split;

      paragraphs.push({
        align: deriveAlign(source, union, container),
        lineHeightPx: lineHeightPx(cs),
        spaceBeforePx: spaceBefore,
        spaceAfterPx: index === pending.length - 1 ? px(cs.marginBottom) : 0,
        bullet: source.bullet,
        lines: lines.map(function (line) { return { box: paintedBox(ctx.frame, line.box), runs: line.runs }; }),
      });
      runBoxes.push(lines.map(function (line) {
        return line._runBoxes.map(function (b) {
          return [round2(b[0] + (ctx.frame.dx || 0)), round2(b[1] + (ctx.frame.dx || 0))];
        });
      }));
      lines.forEach(function (line) { seams = seams.concat(line._seams); });
      // r1a: a text block's inline paint is under its own opacity; `ctx` is its container's.
      emitInlineDecoration(el, ownContext(el, container, ctx), state, source.scope);
    });

    if (!paragraphs.length) { walkDeferred(after, ctx, state); return; }
    var caseKeys = Object.keys(transforms);
    var record = push(state, {
      kind: "text",
      box: paintedBox(ctx.frame, union),
      paragraphs: paragraphs,
      anchor: deriveAnchor(pending[0], ctx),
      writingMode: "horizontal",
      wrap: wrap,
      transformCase: caseKeys.length === 1 && caseKeys[0] === "uppercase" ? "upper"
                   : caseKeys.length === 1 && caseKeys[0] === "lowercase" ? "lower" : null,
      extras: { "x-run-boxes": runBoxes },
    }, container, ctx);
    var runFills = liftRunFills(paragraphs);
    if (runFills) {
      record.extras = record.extras || {};
      record.extras["x-wpe-run-fills"] = runFills;
      diagnose(state, "info", cssPath(container),
               "gradient text: the gradient is stretched over the text box, approximated", state.records.length - 1);
    }
    var recordIndex = state.records.length - 1;
    if (seams.length) {
      // R6: one finding per element, quoting its first glued join.
      var glueLeft = seams[0].left.slice(-24), glueRight = seams[0].right.slice(0, 24);
      var more = seams.length > 1 ? " (" + seams.length + " joins in this text)" : "";
      diagnose(state, "warn", record.source.path,
               "glued runs: '" + glueLeft + "' + '" + glueRight + "' are separated by layout, not by a space — the export cannot keep that gap" + more,
               recordIndex);
    }
    if (split) {
      // R8, worded after its cause (B1 critique #12).
      diagnose(state, "info", record.source.path,
               split === "text-indent"
                 ? "list item split after its first line so its text-indent lands where the browser put it"
                 : "list item split after its first line so the text after the inline box lands where the browser put it",
               recordIndex);
    }
    // Clipped text is already reported as cut off (WP-F: `trimClipped`); saying it also runs past
    // its box twice over would give the readiness panel two findings for one defect.
    if (!trimClipped(record, pending, container, ctx, state)) {
      checkTextOverflow(spans, container, ctx, state, union, paragraphs);
    }
    // R3: the detached boxes that sit after this text, in the browser's paint and reading order —
    // after the text before them (and the inline paint around them), before the text after them
    // (B1 critique #3).
    pending.forEach(function (source) {
      (source.then || []).forEach(function (queued) { walkDetached(queued, ctx, state); });
    });
    // WP-E: the out-of-flow pseudo copies last — positioned boxes paint after the in-flow content,
    // the detached boxes above included.
    walkDeferred(after, ctx, state);
  }

  /**
   * Walk a detached box as an element of its own (R3). Its inline ancestors inside the paragraph —
   * a `mark` or a tag tray around the chip — paint first, as the browser paints them under it.
   * `ctx` is the context of `queued.stop`'s children (the walk's container): the box takes the
   * opacity and overflow clip of every ancestor between it and the container, as E's
   * `pseudoContext` gives a pseudo copy (r1a: a `.lbl{opacity:.85}` paragraph's chip is at 0.85).
   */
  function walkDetached(queued, ctx, state) {
    if (queued.inline) {
      // R1: the inline paint of a paragraph with no measured text (`addParagraph`'s `paintInline`).
      emitInlineDecoration(queued.paragraphEl, ownContext(queued.paragraphEl, queued.stop, ctx), state, queued.scope);
      return;
    }
    var ancestors = [];
    for (var node = queued.el.parentElement; node && node !== queued.paragraphEl; node = node.parentElement) {
      ancestors.unshift(node);
    }
    var stop = queued.stop || queued.paragraphEl;
    if (ancestors.length) emitInlineDecoration(queued.paragraphEl, ownContext(queued.paragraphEl, stop, ctx), state, ancestors);
    walkElement(queued.el, pseudoContext(queued.el, stop, ctx), state);
  }

  function walkElement(el, ctx, state) {
    var cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") return;
    if (el.tagName === "ASIDE" && el.classList.contains("notes")) return;
    if (el.hasAttribute("data-engine-probe")) return;

    var frame = ctx.frame;
    var transform = decomposeTransform(cs.transform);
    var flipH = false, flipV = false, restoreTransform = null;
    if (transform && !transform.unsupported) {
      flipH = transform.scaleX < 0;
      flipV = transform.scaleY < 0;
      if (Math.abs(transform.angle) > 1e-6) {
        var painted = el.getBoundingClientRect();
        restoreTransform = el.style.transform;
        el.style.transform = "none";
        // An isolated raster is taken later, from Python, against the *live* page. Marking the
        // rotated element lets the capture CSS switch its transform off too, so the PNG holds the
        // unrotated picture that the recorded box and rotation describe — not a double rotation.
        el.setAttribute("data-engine-unrotate", "");
        var plain = el.getBoundingClientRect();
        frame = {
          angle: ctx.frame.angle + transform.angle,
          dx: ctx.frame.dx + (painted.left + painted.width / 2) - (plain.left + plain.width / 2),
          dy: ctx.frame.dy + (painted.top + painted.height / 2) - (plain.top + plain.height / 2),
        };
        if (ctx.frame.angle) {
          diagnose(state, "info", cssPath(el),
                   "rotation inside a rotated ancestor: the position is a translation approximation", null);
        }
      }
    }
    try {
      walkTransformed(el, cs, ctx, frame, flipH, flipV, state);
    } finally {
      if (restoreTransform !== null) el.style.transform = restoreTransform;
    }
  }

  function walkTransformed(el, cs, ctx, frame, flipH, flipV, state) {
    var opacity = ctx.opacity * (parseFloat(cs.opacity) || 0);
    var b = localBox(frame, el.getBoundingClientRect());
    var clip = ctx.clip;
    var overflow = cs.overflow + " " + cs.overflowX + " " + cs.overflowY;
    if (/hidden|clip|scroll|auto/.test(overflow)) {
      clip = intersectBox(clip, paintedBox(frame, paddingBox(b, cs)));
    }
    var group = ctx.group;
    if (el.getAttribute("data-pptx") === "group") {
      group = "g" + (state.groups.length + 1);
      state.groups.push({ id: group, parent: ctx.group, name: el.getAttribute("data-name") || el.id || group,
                          box: paintedBox(frame, b) });
    }
    var next = { opacity: opacity, clip: clip, group: group, frame: frame };
    // An element's own `overflow` clips what it contains — its children, an <img>'s picture (the UA's
    // `overflow: clip`) — never its own background, border or shadow (r3: the outer half of the border
    // of an overflow-hidden card, or of any <img>, was cut away with it).
    var own = { opacity: opacity, clip: ctx.clip, group: group, frame: frame };

    var reason = rasterReason(el, cs);
    if (reason) {
      var record = push(state, {
        kind: "raster", box: paintedBox(frame, b), reason: reason, selector: markCapture(el, state),
      }, el, next);
      diagnose(state, "warn", record.source.path, "rasterised: " + reason, state.records.length - 1);
      return;
    }

    var tag = el.tagName.toLowerCase();
    if (el.hasAttribute("data-chart")) { emitChart(el, cs, next, state); return; }
    if (tag === "svg") { emitSvgPlaceholder(el, next, state); return; }
    if (tag === "img") {
      // r3: an <img>'s own background, border and shadow paint around its content box, as a box's do.
      if (hasDecoration(el, cs, b)) emitDecoration(el, cs, own, state, flipH, flipV);
      var image = imageRecord(el, cs, next, state);
      if (isRemoteUrl(image.url, el.getAttribute("src"))) {
        if (!remoteImage(image, false, el, state)) return;          // WP-F: the network policy
      } else if (!image.complete) {
        diagnose(state, "error", cssPath(el), "image did not load: " + image.url, null);
      }
      var placed = push(state, image, el, next);
      placed.flipH = flipH;
      placed.flipV = flipV;
      return;
    }
    if (tag === "table") { emitTable(el, cs, next, state); return; }
    if (tag === "canvas" || tag === "video" || tag === "iframe" || tag === "object" || tag === "embed") {
      if (remoteFrameBlocked(el, tag, state)) return;               // WP-F: the network policy
      var generic = push(state, { kind: "raster", box: paintedBox(frame, b),
                                  reason: "<" + tag + "> has no native PowerPoint equivalent",
                                  selector: markCapture(el, state) }, el, next);
      diagnose(state, "warn", generic.source.path, "rasterised: <" + tag + ">", state.records.length - 1);
      return;
    }

    if (hasDecoration(el, cs, b)) { warnFragmented(el, state); emitDecoration(el, cs, own, state, flipH, flipV); }
    var background = backgroundImageRecord(el, cs, next, state);
    if (background && isRemoteUrl(background.url, background.url)) {
      if (!remoteImage(background, true, el, state)) background = null;   // WP-F: the network policy
    } else if (background && !background.complete) {
      diagnose(state, "error", cssPath(el), "background image did not load: " + background.url, null);
    }
    if (background) pushBackground(state, background, el, own);
    walkChildren(el, next, state);
  }

  function walkChildren(el, ctx, state) {
    var inOrder = childItems(el);
    var items = stackingOrder(inOrder);
    var run = { pending: [], layout: containerLayout(el, ctx), container: el };
    function flush() { flushText(run.pending, el, ctx, state); run.pending = []; }
    run.flush = flush;
    // R2: the container's items that are not text and share its lines — what an anonymous run may sit
    // beside: in-flow boxes and floats (the run's lines flow around a float), not positioned ones.
    var elements = inOrder.filter(function (item) {
      return item.type === "element" && item.cs.position !== "absolute" && item.cs.position !== "fixed";
    }).map(function (item) { return item.node; });
    // R8: a detached box whose list item has no text of its own carries the item's marker, on the
    // first paragraph this walk measures (B1 critique #11).
    var bulletHost = el.__engineBulletHost || null;
    el.__engineBulletHost = null;
    function push(source) {
      if (bulletHost && !source.noBullet) source.bulletHost = bulletHost;
      var entry = pushParagraph(run, source, ctx);
      if (entry && source.bulletHost) bulletHost = null;
      return entry;
    }

    /**
     * One paragraph's inline content, split at its detached boxes (see `inlineSegments`). The text
     * on either side of a box goes through `pushParagraph` as a hugged segment, with no flush around
     * a box *beside* the pending text: `stacked()` alone decides where elements end (R3). The box is
     * queued on the pending text before it and walked once that text's record is out, so the two
     * paint and read in the browser's order; with nothing pending it is walked at once. A box *below*
     * the pending run intervenes between it and whatever follows — a non-text sibling in §11's words —
     * so the run ends before it. Only the first text segment of a list item carries its bullet; an
     * item with no text outside its boxes gives it to the first box.
     */
    function addParagraph(paragraphEl, nodes, whole) {
      var segments = inlineSegments(paragraphEl, nodes, whole.roots);
      if (segments.length === 1 && segments[0].nodes) {
        if (!push(whole)) { paintInline(paragraphEl, whole); deferAfterPending(whole.after); }
        return;
      }
      function said(segment) {
        return !!segment.nodes && segment.nodes.some(function (node) { return /\S/.test(node.data); });
      }
      /** The detached box a segment sits against, looking past whitespace-only segments. */
      function boxBeside(index, step) {
        for (var j = index + step; j >= 0 && j < segments.length; j += step) {
          if (!segments[j].nodes) return segments[j].box;
          if (said(segments[j])) return null;
        }
        return null;
      }
      var lastText = -1;
      segments.forEach(function (segment, index) { if (said(segment)) lastText = index; });
      if (paragraphEl.tagName === "LI" && lastText < 0 && !whole.noBullet) {
        var first = segments.filter(function (segment) { return !segment.nodes; })[0];
        if (first) first.box.__engineBulletHost = paragraphEl;
      }
      var texts = 0, orphans = whole.after, measured = false;
      segments.forEach(function (segment, index) {
        if (!segment.nodes) {
          if (belowPending(run, segment.box, ctx)) flush();
          queueAfterPending({ el: segment.box, paragraphEl: paragraphEl, stop: el });
          return;
        }
        var entry = push({
          el: paragraphEl, nodes: segment.nodes, item: whole.item, hug: true, segment: true,
          boxBefore: boxBeside(index, -1), boxAfter: boxBeside(index, 1),
          scope: whole.scope, noBullet: texts > 0 || !!whole.noBullet,
          // WP-E's queue rides on the paragraph's last text (B1 critique #4).
          after: index === lastText ? whole.after : undefined,
        });
        if (entry) measured = true;
        if (entry && index === lastText) orphans = null;
        if (said(segment)) texts++;
      });
      // R1: with no text measured — only whitespace around its detached boxes, or none — nothing
      // carries the paragraph's inline paint through `flushText`.
      if (!measured) paintInline(paragraphEl, whole);
      // With no text of the paragraph measured, its pseudo-element copies still paint, in order.
      deferAfterPending(orphans);
    }

    /**
     * R1 (render check, defect 2): a paragraph none of whose text is measured — a row of empty
     * inline-block rating dots, the whitespace between them — still paints its inline boxes, which
     * `flushText` paints only for a paragraph that reached `pending`. They are painted at once, or,
     * with text pending, queued after it as a detached box is, so they keep the browser's paint
     * order. `state.decorated` keeps each box painted once, whoever gets to it first.
     */
    function paintInline(paragraphEl, whole) {
      queueAfterPending({ inline: true, paragraphEl: paragraphEl, stop: el, scope: whole.scope });
    }

    function queueAfterPending(queued) {
      var holder = run.pending[run.pending.length - 1];
      if (holder) (holder.then = holder.then || []).push(queued);
      else walkDetached(queued, ctx, state);
    }

    /**
     * WP-E's pseudo copies of a paragraph none of whose text entered the run (no line measured) still
     * paint, through `walkDeferred` like every other copy (once, in its host's context). They are
     * positioned boxes, which paint after the in-flow content: they join the last pending paragraph's
     * `after`, walked once its element's record and detached boxes are out — or at once, with
     * nothing pending.
     */
    function deferAfterPending(list) {
      if (!list || !list.length) return;
      var holder = run.pending[run.pending.length - 1];
      if (holder) holder.after = (holder.after || []).concat(list);
      else walkDeferred(list, ctx, state);
    }

    var previousIndex = null;
    items.forEach(function (item) {
      // The walk follows the stacking order, not the DOM's: an in-flow positioned block walked
      // later, or out of turn, still sat between its DOM neighbours (B1 critique #9).
      var index = inOrder.indexOf(item);
      if (previousIndex !== null && interrupted(inOrder, previousIndex, index)) flush();
      previousIndex = index;
      if (item.type === "inline") {
        var nodes = item.nodes.reduce(function (acc, node) {
          return acc.concat(node.nodeType === 3 ? [node] : textNodesOf(node));
        }, []);
        // WP-E grouping rule: pseudo copies inside the run's inline boxes paint beside its text.
        var inRun = deferredPseudos(el, item.nodes, nodes, el, null);
        walkDeferred(inRun.below, ctx, state);
        addParagraph(el, nodes, {
          el: el, nodes: nodes, item: item, peers: elements, roots: item.nodes,
          scope: item.nodes.filter(function (node) { return node.nodeType === 1; }),
          after: inRun.above,
        });
        return;
      }
      var node = item.node, cs = item.cs;
      if (pseudoHandled(node, cs)) return;          // WP-E: drawn under its host's paint, or reported
      if (isOutOfFlow(cs) || isSpecial(node) || rasterReason(node, cs)) { flush(); walkElement(node, ctx, state); return; }
      if (isTextBlock(node, cs)) {
        var b = borderBox(node, ctx.frame);
        // WP-E grouping rule: its pseudo copies never split the paragraph run. Those under the
        // block's own paint go first, those under its text next, the rest after the text.
        var own = textNodesOf(node);
        var deferred = deferredPseudos(node, [node], own, node.parentElement, node);
        walkDeferred(deferred.under, ctx, state);
        if (hasDecoration(node, cs, b)) { flush(); warnFragmented(node, state); emitDecoration(node, cs, ownContext(node, el, ctx), state); }
        walkDeferred(deferred.below, ctx, state);
        addParagraph(node, own, { el: node, item: node, roots: [node], after: deferred.above });
        return;
      }
      flush();
      walkElement(node, ctx, state);
    });
    flush();
  }

  /**
   * Does the walk skip an in-flow sibling between the items at DOM positions `from` and `to` — a
   * relatively positioned block, walked in the positioned layer — or go back to an earlier one?
   * Either way the two are not neighbours on the page.
   */
  function interrupted(items, from, to) {
    if (to < from) return true;
    for (var k = from + 1; k < to; k++) {
      var item = items[k];
      if (item.type === "element" && (item.cs.position === "relative" || item.cs.position === "sticky")) return true;
    }
    return false;
  }

  function emitChart(el, cs, ctx, state) {
    var b = borderBox(el, ctx.frame);
    var spec = null, error = null;
    try { spec = JSON.parse(el.getAttribute("data-chart")); }
    catch (parseError) { error = String(parseError && parseError.message || parseError); }
    if (!spec) {
      var record = push(state, { kind: "raster", box: paintedBox(ctx.frame, b),
                                 reason: "data-chart is not valid JSON: " + error,
                                 selector: markCapture(el, state) }, el, ctx);
      diagnose(state, "error", record.source.path, "data-chart is not valid JSON: " + error,
               state.records.length - 1);
      return;
    }
    var area = (spec.options && spec.options.plotArea) || null;
    var plotRect = area
      ? boxOf(b.x + b.w * (area.x || 0), b.y + b.h * (area.y || 0),
              b.w * (area.w !== undefined ? area.w : 1), b.h * (area.h !== undefined ? area.h : 1))
      : b;
    var colour = parseColor(cs.color) || { color: "000000" };
    push(state, {
      kind: "chart", box: paintedBox(ctx.frame, b), spec: spec, origin: "authored", confidence: 1.0,
      plotRect: area ? paintedBox(ctx.frame, plotRect) : null, overlay: [],
      style: { font: String(cs.fontFamily).split(",")[0].replace(/["']/g, "").trim(),
               sizePx: px(cs.fontSize), color: colour.color },
    }, el, ctx);
  }

  function emitSvgPlaceholder(el, ctx, state) {
    var svgId = el.getAttribute("data-engine-svg");
    var b = borderBox(el, ctx.frame);
    push(state, { kind: "svgPlaceholder", box: paintedBox(ctx.frame, b), svgId: svgId,
                  source: { path: cssPath(el), tag: "svg", svg: svgId } }, el, ctx);
  }

  // ------------------------------------------------------------------ pseudo-elements (WP-E)
  //
  // `::before` and `::after` are not in the DOM, so a walk of the DOM never meets them: timeline
  // axes and dots, chevron tips, colour edges, custom bullets and counters used to vanish without a
  // word. `window.__engineMaterialisePseudo` (run by html.py after the scripts are injected and
  // before `__engineBackgroundSizes`; never by the reference render) rebuilds each one as a real
  // element — `<engine-pseudo data-engine-pseudo="before|after">` — and then proves that the
  // rebuild moved and repainted nothing:
  //
  //  1. One layered stylesheet suppresses a pseudo as soon as its host carries the token in
  //     `data-engine-pseudo-host`. A layered `!important` beats every unlayered author
  //     `!important` whatever its specificity (CSS Cascade 5 §6.3, measured); only an author layer
  //     declared earlier can beat it, and check (0) catches that. The same layer switches off the
  //     pseudo-elements of the copies themselves (`.card *::before` would otherwise match them).
  //  2. Snapshot every element's and text node's rect, and every element's paint signature.
  //  3. Walk the document in order carrying CSS counters (CSS Lists 3 scoping) and quote depth, and
  //     record — before any mutation — every generated pseudo's computed longhands and its content
  //     resolved to text (`counter()`, `counters()`, `attr()`, quotes, strings).
  //  4. Rebuild them all at once and verify once (`rebuildAll`); if that fails, take everything back
  //     and rebuild them one at a time, in *reverse* document order: counters and quotes flow
  //     forwards, so suppressing a later pseudo never changes the number or quote an earlier,
  //     still-live one shows. Each copy is inserted where the pseudo was (first child for `::before`, last for
  //     `::after`) and then given, inline and `!important`, every computed longhand of the pseudo it
  //     does not already compute to (inline beats stylesheet `!important`, measured — so author
  //     rules for real elements cannot restyle it, and it keeps the font the forcing CSS gave the
  //     pseudo), except `content`, `counter-*`, `animation-*`, `transition-*` and `will-change`
  //     (`settleCopy` says why not all of them). Then verify (0) the pseudo is suppressed,
  //     (i) no snapshotted rect moved by more than PSEUDO_SHIFT_EPS, (ii) the copy computes to the
  //     pseudo's own value for every longhand it took, (iii) no element's paint changed (`:first-child`, `:nth-child`,
  //     `:empty`, `+`/`~` and `:has()` rules the new child flips without moving anything). Any
  //     violation removes the copy and the token and says so: a reported drop, never a moved line
  //     or a repainted sibling.
  //
  // The copies then enter the walk as ordinary children, with one grouping rule (master brief §11,
  // cited by the side-by-side package too): an *out-of-flow* copy never splits a paragraph run
  // (`isTextBlock`, `textNodesOf`, `walkChildren`, `flushText`). An in-flow inline copy joins the
  // run as text; an in-flow block copy splits the flow exactly as a real block child does.

  /** Hosts that never generate `::before`/`::after` (replaced elements and form controls). */
  var PSEUDO_SKIP_HOSTS = { img: 1, svg: 1, canvas: 1, video: 1, iframe: 1, object: 1, embed: 1, br: 1,
                            input: 1, textarea: 1, select: 1 };
  /** Longhands a copy does not take: its text replaces `content`, and the rest animate or count. */
  var PSEUDO_NOT_COPIED = /^(content$|counter-|animation|transition|will-change$)/;
  var COUNTER_STYLES = { decimal: 1, "decimal-leading-zero": 1, "lower-alpha": 1, "lower-latin": 1,
                         "upper-alpha": 1, "upper-latin": 1, "lower-roman": 1, "upper-roman": 1, none: 1,
                         disc: 1, circle: 1, square: 1, "lower-greek": 1 };

  // -- the grouping rule's helpers, used by the walk

  /** Is this child a rebuilt pseudo-element that sits beside the text rather than in it? */
  function isDeferredPseudo(el, cs) {
    return el.hasAttribute("data-engine-pseudo") && cs.display !== "none" && isOutOfFlow(cs);
  }

  /** The text a block holds outside its out-of-flow pseudo copies (theirs is walked with them). */
  function textBesidePseudos(el) {
    var text = "";
    for (var node = el.firstChild; node; node = node.nextSibling) {
      if (node.nodeType === 3) text += node.data;
      else if (node.nodeType === 1 && !isDeferredPseudo(node, getComputedStyle(node))) text += node.textContent;
    }
    return text;
  }

  /**
   * The rebuilt pseudo-elements a paragraph is responsible for — the grouping rule, extended by the
   * E3 review (B1): every out-of-flow copy below the paragraph element, or below the inline nodes
   * of an anonymous run, *at any depth* (a highlight under a word, a badge on a `<span>`), and every
   * in-flow block-level copy that paints inside its inline content (a bar in a block-in-inline).
   * Copies inside a detached box are that box's: it is walked as an element (`inlineSegments`
   * decides which boxes detach, so it is asked here too — it only marks, and marks the same boxes
   * every time).
   *
   * `under`: z-index < 0 copies drawn before `root`'s own paint, `root` being the paragraph element
   * whose decoration the walk has not emitted yet (null for an anonymous run: its container's paint
   * is already out) — M1; `below`: the other z-index < 0 copies and the in-flow boxes, drawn before
   * the text; `above`: the rest, drawn after it. A copy that paints under a box already emitted is
   * in no list: the sweep after the walk reports it (`reportUndrawnPseudos`).
   * `stop`: the element whose children `ctx` describes; the copy's own context adds the opacity and
   * overflow clip of every ancestor below it (`pseudoContext`, M2).
   */
  function deferredPseudos(paragraphEl, roots, textNodes, stop, root) {
    var out = { under: [], below: [], above: [] }, copies = [];
    roots.forEach(function (node) {
      if (node.nodeType !== 1) return;
      var found = node.querySelectorAll("[data-engine-pseudo]");
      for (var i = 0; i < found.length; i++) copies.push(found[i]);
    });
    if (!copies.length) return out;
    var nested = copies.some(function (copy) {
      for (var node = copy.parentElement; node && node !== paragraphEl; node = node.parentElement) {
        if (isAtomicInline(getComputedStyle(node).display)) return true;
      }
      return false;
    });
    if (nested) inlineSegments(paragraphEl, textNodes, roots);
    copies.forEach(function (copy) {
      if (copy.__engineWalked || insideDetached(copy.parentElement, paragraphEl)) return;
      var cs = getComputedStyle(copy);
      if (cs.display === "none") return;
      copy.__engineStop = stop;
      if (!isOutOfFlow(cs)) {
        // In flow: its text is the paragraph's; a block-level copy's own box is drawn before it.
        if (!isInlineDisplay(cs.display) && hasDecoration(copy, cs, borderBox(copy, IDENTITY_FRAME))) {
          out.below.push(copy);
        }
        return;
      }
      if (!(parseInt(cs.zIndex, 10) < 0)) { out.above.push(copy); return; }
      var place = negativePlacement(copy, root);
      if (place === "blocked") copy.__engineWalked = true;
      else (place === "under" ? out.under : out.below).push(copy);
    });
    return out;
  }

  /**
   * Draw deferred copies, each once, in the context its host would give its children (M2): an
   * out-of-flow copy as an element, an in-flow block copy by its own paint only (its text is
   * already in the paragraph), under its own opacity too — `walkElement` folds that for an
   * out-of-flow copy, `pseudoContext` stops at the copy's parent (r1e: a
   * `::before{display:block;opacity:.4}` bar in a block-in-inline was drawn opaque).
   */
  function walkDeferred(list, ctx, state) {
    (list || []).forEach(function (copy) {
      if (copy.__engineWalked) return;
      copy.__engineWalked = true;
      var own = pseudoContext(copy, copy.__engineStop, ctx);
      var cs = getComputedStyle(copy);
      if (isOutOfFlow(cs)) walkElement(copy, own, state);
      else emitDecoration(copy, cs, ownContext(copy, copy.parentElement, own), state);
    });
  }

  /**
   * `ctx` (the context of `stop`'s children) with the opacity and the overflow clip of every
   * ancestor of `copy` below `stop` — what walking those ancestors as elements would have given it.
   * The E3 review (M2) found a KPI card's `overflow:hidden` circle spilling out of the card.
   * A `display: contents` ancestor generates no box, so it neither fades nor clips anything
   * (r1d, measured in Edge: a chip or a `::after` bar under a contents wrapper at 0.2 is drawn at
   * full colour; r1a's `opacityBelow` skips it for text the same way).
   */
  function pseudoContext(copy, stop, ctx) {
    var chain = [];
    for (var node = copy.parentElement; node && node !== stop && node !== document.body; node = node.parentElement) {
      chain.unshift(node);
    }
    var next = { opacity: ctx.opacity, clip: ctx.clip, group: ctx.group, frame: ctx.frame };
    chain.forEach(function (node) {
      var cs = getComputedStyle(node);
      if (cs.display === "contents") return;
      next.opacity *= isNaN(parseFloat(cs.opacity)) ? 1 : parseFloat(cs.opacity);
      // `overflow` clips only a block container (an inline box ignores it).
      if (isInlineDisplay(cs.display) && !isAtomicInline(cs.display)) return;
      if (/hidden|clip|scroll|auto/.test(cs.overflow + " " + cs.overflowX + " " + cs.overflowY)) {
        next.clip = intersectBox(next.clip,
                                 paintedBox(next.frame, paddingBox(localBox(next.frame, node.getBoundingClientRect()), cs)));
      }
    });
    return next;
  }

  /** Does this element start a stacking context of its own (CSS 2.1 Appendix E and its successors)? */
  function stackingContext(el, cs) {
    if (el === document.documentElement) return true;
    if (cs.position === "fixed" || cs.position === "sticky") return true;
    if (cs.zIndex !== "auto") {
      if (cs.position !== "static") return true;
      var parent = el.parentElement ? getComputedStyle(el.parentElement).display : "";
      if (/flex|grid/.test(parent)) return true;
    }
    if (parseFloat(cs.opacity) < 1) return true;
    if (cs.transform !== "none" || (cs.rotate || "none") !== "none" || (cs.scale || "none") !== "none" ||
        (cs.translate || "none") !== "none") return true;
    if (cs.filter !== "none" || (cs.backdropFilter || "none") !== "none" || cs.clipPath !== "none") return true;
    if ((cs.maskImage || cs.webkitMaskImage || "none") !== "none" || cs.perspective !== "none") return true;
    if (cs.isolation === "isolate" || cs.mixBlendMode !== "normal") return true;
    if (/paint|layout|strict|content/.test(cs.contain || "") || /size/.test(cs.containerType || "")) return true;
    return /transform|opacity|filter|perspective|z-index|isolation|mix-blend-mode|mask|clip-path/.test(cs.willChange || "");
  }

  /** Does this box paint something of its own under its content (a fill, a side, a shadow, a picture)? */
  function paintsBox(el, cs) {
    return hasDecoration(el, cs, box(el.getBoundingClientRect())) || /url\(/i.test(cs.backgroundImage || "");
  }

  /**
   * Where a z-index < 0 copy paints (E3 review M1). Until an ancestor starts a stacking context, a
   * negative z-index paints under the backgrounds of *every* ancestor on the way (the gradient-border
   * and offset-shadow idioms rely on it). "under": under `root`'s paint, which the walk has not
   * emitted yet, so the copy is drawn first; "blocked": under a box whose paint is already out —
   * never drawn over it, reported by the sweep; "normal": its stacking context is below every
   * painted ancestor, so it is drawn after that paint and before the text, as it always was. Inline
   * boxes inside `root` paint with the paragraph, after the copy, so they never block it.
   */
  function negativePlacement(copy, root) {
    var place = "normal";
    for (var node = copy.parentElement; node && node !== document.body; node = node.parentElement) {
      var cs = getComputedStyle(node);
      if (stackingContext(node, cs)) return place;
      if (!paintsBox(node, cs)) continue;
      if (node === root) { place = "under"; continue; }
      if (root && root !== node && root.contains(node) && isInlineDisplay(cs.display)) continue;
      return "blocked";
    }
    return place;
  }

  /**
   * A child the walk must not draw again (`walkChildren`): a copy already drawn under its host's
   * paint, or a z-index < 0 copy that paints under a box already emitted (reported by the sweep).
   */
  function pseudoHandled(el, cs) {
    if (!el.hasAttribute("data-engine-pseudo")) return false;
    if (el.__engineWalked) return true;
    if (isOutOfFlow(cs) && parseInt(cs.zIndex, 10) < 0 && negativePlacement(el, null) === "blocked") {
      el.__engineWalked = true;
      return true;
    }
    return false;
  }

  /**
   * Before a box's own paint (`emitDecoration`), the copies of its z-index < 0 pseudo-elements that
   * paint under it. `ctx` is what its children are walked with.
   */
  function walkUnderPaint(el, ctx, state) {
    for (var child = el.firstElementChild; child; child = child.nextElementSibling) {
      if (!child.hasAttribute("data-engine-pseudo") || child.__engineWalked) continue;
      var cs = getComputedStyle(child);
      if (!isOutOfFlow(cs) || !(parseInt(cs.zIndex, 10) < 0)) continue;
      if (negativePlacement(child, el) !== "under") continue;
      child.__engineWalked = true;
      walkElement(child, ctx, state);
    }
  }

  /**
   * After the walk: every copy the export does not carry is taken back and said so (E3 review B1).
   * A copy is carried when each part of it that paints was drawn — its text in a paragraph
   * (`runStyle` marks it), its box as an element with its path — or when a raster holds its pixels.
   * Otherwise (a chart host's label, a copy under a hidden host, a paint the export has no shape for,
   * one blocked under a box already drawn) the pseudo-element paints again on the live page, as in
   * the reference, and the warn names it; `window.__enginePseudoUndrawn` lists them for the count.
   */
  function reportUndrawnPseudos(state) {
    var drawn = {}, undrawn = [];
    state.records.forEach(function (record) {
      if (record.kind !== "text" && record.source) drawn[record.source.path] = true;
    });
    var copies = document.querySelectorAll("[data-engine-pseudo]");
    for (var i = 0; i < copies.length; i++) {
      var copy = copies[i], host = copy.parentElement, kind = copy.getAttribute("data-engine-pseudo");
      if (!host || copy.closest("[data-engine-id]")) continue;
      var cs = getComputedStyle(copy);
      if (cs.visibility === "hidden" || cs.display === "none") continue;
      var where = cssPath(copy);
      var textOk = !/\S/.test(copy.textContent) || !!copy.__engineTextDrawn;
      var paintOk = !pseudoPaints(cs) || !!drawn[where];
      if (textOk && paintOk) continue;
      pseudoWarning(state, where, kind, "undrawn");
      undrawn.push(where);
      host.removeChild(copy);
      setHostToken(host, kind, false);
    }
    window.__enginePseudoUndrawn = undrawn;
  }

  // -- content: tokens, strings, counters, quotes

  /** A CSS string starting at `start` (a quote) → `{value, end}`, with CSS escapes decoded. */
  function readCssString(text, start) {
    var quote = text[start], value = "", i = start + 1;
    while (i < text.length && text[i] !== quote) {
      if (text[i] === "\\") {
        var hex = /^[0-9a-fA-F]{1,6}/.exec(text.slice(i + 1, i + 7));
        if (hex) {
          var code = parseInt(hex[0], 16);
          if (!code || code > 0x10FFFF || (code >= 0xD800 && code <= 0xDFFF)) code = 0xFFFD;
          value += String.fromCodePoint(code);
          i += 1 + hex[0].length;
          if (/\s/.test(text[i] || "")) i++;        // one whitespace ends a hex escape
          continue;
        }
        if (text[i + 1] === "\n") { i += 2; continue; }   // an escaped newline continues the line
        value += text[i + 1] || "";
        i += 2;
        continue;
      }
      value += text[i++];
    }
    return { value: value, end: i + 1 };
  }

  /** A computed `content` value → strings, functions and keywords, up to an alternative-text `/`. */
  function contentParts(value) {
    var parts = [], text = String(value || ""), i = 0;
    while (i < text.length) {
      var c = text[i];
      if (/\s/.test(c) || c === ",") { i++; continue; }
      if (c === "/") break;                           // alternative text is not painted
      if (c === '"' || c === "'") {
        var read = readCssString(text, i);
        parts.push({ type: "string", value: read.value });
        i = read.end;
        continue;
      }
      var ident = /^-?[A-Za-z_][\w-]*/.exec(text.slice(i));
      if (!ident) { i++; continue; }
      var name = ident[0].toLowerCase();
      i += ident[0].length;
      if (text[i] !== "(") { parts.push({ type: "keyword", name: name }); continue; }
      var depth = 0, j = i, quote = null;
      for (; j < text.length; j++) {
        var d = text[j];
        if (quote) {
          if (d === "\\") j++;
          else if (d === quote) quote = null;
          continue;
        }
        if (d === '"' || d === "'") quote = d;
        else if (d === "(") depth++;
        else if (d === ")" && --depth === 0) break;
      }
      parts.push({ type: "function", name: name, args: text.slice(i + 1, j) });
      i = j + 1;
    }
    return parts;
  }

  /** Function arguments split at top-level commas, outside quotes (`counters(item, ", ")`). */
  function splitArgs(text) {
    var out = [], depth = 0, quote = null, current = "";
    for (var i = 0; i < text.length; i++) {
      var c = text[i];
      if (quote) {
        current += c;
        if (c === "\\" && i + 1 < text.length) current += text[++i];
        else if (c === quote) quote = null;
        continue;
      }
      if (c === '"' || c === "'") quote = c;
      else if (c === "(") depth++;
      else if (c === ")") depth--;
      if (c === "," && depth === 0) { out.push(current.trim()); current = ""; continue; }
      current += c;
    }
    if (current.trim()) out.push(current.trim());
    return out;
  }

  /** An argument that is a CSS string → its value; anything else as written. */
  function stringArg(text) {
    text = String(text || "").trim();
    return /^["']/.test(text) ? readCssString(text, 0).value : text;
  }

  /** CSS Counter Styles' alphabetic system: 1 → a, 26 → z, 27 → aa (`letters` defaults to a–z). */
  function alphabetic(n, letters) {
    letters = letters || "abcdefghijklmnopqrstuvwxyz";
    var out = "";
    for (; n > 0; n = Math.floor((n - 1) / letters.length)) out = letters[(n - 1) % letters.length] + out;
    return out;
  }

  function roman(n) {
    var table = [[1000, "M"], [900, "CM"], [500, "D"], [400, "CD"], [100, "C"], [90, "XC"], [50, "L"],
                 [40, "XL"], [10, "X"], [9, "IX"], [5, "V"], [4, "IV"], [1, "I"]];
    var out = "";
    table.forEach(function (pair) { while (n >= pair[0]) { out += pair[1]; n -= pair[0]; } });
    return out;
  }

  /** One counter value in one of the styles the export supports (the caller warns about the rest). */
  function counterText(value, style) {
    switch (style) {
      case "none": return "";
      case "disc": return "•";
      case "circle": return "◦";
      case "square": return "▪";
      case "decimal-leading-zero":
        return (value < 0 ? "-" : "") + (Math.abs(value) < 10 ? "0" : "") + Math.abs(value);
      case "lower-alpha": case "lower-latin": return value > 0 ? alphabetic(value) : String(value);
      case "upper-alpha": case "upper-latin": return value > 0 ? alphabetic(value).toUpperCase() : String(value);
      case "lower-roman": return value > 0 && value < 4000 ? roman(value).toLowerCase() : String(value);
      case "upper-roman": return value > 0 && value < 4000 ? roman(value) : String(value);
      case "lower-greek": return value > 0 ? alphabetic(value, "αβγδεζηθικλμνξοπρστυφχψω") : String(value);
      default: return String(value);
    }
  }

  /** `counter-reset`/`-set`/`-increment` computed text → `[[name, amount], …]`. */
  function counterOps(value, fallback) {
    var ops = [];
    if (!value || value === "none") return ops;
    var tokens = String(value).trim().split(/\s+/);
    for (var i = 0; i < tokens.length; i++) {
      var name = tokens[i].replace(/^reversed\((.*)\)$/, "$1");
      var amount = fallback;
      if (i + 1 < tokens.length && /^[+-]?\d+$/.test(tokens[i + 1])) amount = parseInt(tokens[++i], 10);
      ops.push([name, amount]);
    }
    return ops;
  }

  // Counters follow CSS Lists 3 §4.4: an element inherits its parent's counter instances and then
  // those of its preceding sibling it does not already have; a reset instantiates a new counter
  // (replacing the innermost of that name when it came from the element itself or a previous
  // sibling). Instances are shared objects, so an increment anywhere is seen by every later element
  // holding the same instance — which is exactly "values flow in tree order".

  function inheritCounters(parentSet, siblingSet) {
    var set = parentSet.slice();
    (siblingSet || []).forEach(function (counter) { if (set.indexOf(counter) < 0) set.push(counter); });
    return set;
  }

  function innermostCounter(set, name) {
    for (var i = set.length - 1; i >= 0; i--) if (set[i].name === name) return set[i];
    return null;
  }

  function instantiateCounter(set, name, value, origin) {
    var inner = innermostCounter(set, name);
    if (inner && inner.origin.parent === origin.parent && inner.origin.order <= origin.order) {
      set.splice(set.indexOf(inner), 1);
    }
    var counter = { name: name, origin: origin, value: value };
    set.push(counter);
    return counter;
  }

  /**
   * Resets, then sets, then increments (CSS Lists 3 §4.3). `el` is null for a pseudo-element.
   * Chromium keeps the `list-item` counter out of computed styles, so its implicit reset on lists
   * and increment on list items are applied here, as the browser applies them.
   */
  function applyCounters(set, el, cs, origin) {
    var resets = counterOps(cs.counterReset, 0);
    var tag = el ? el.tagName : "";
    if ((tag === "OL" || tag === "UL" || tag === "MENU") && !resets.some(function (op) { return op[0] === "list-item"; })) {
      resets.push(["list-item", tag === "OL" ? (parseInt(el.getAttribute("start") || "1", 10) || 1) - 1 : 0]);
    }
    resets.forEach(function (op) { instantiateCounter(set, op[0], op[1], origin); });
    counterOps(cs.counterSet, 0).forEach(function (op) {
      (innermostCounter(set, op[0]) || instantiateCounter(set, op[0], 0, origin)).value = op[1];
    });
    var increments = counterOps(cs.counterIncrement, 1);
    if (el && cs.display === "list-item" && !increments.some(function (op) { return op[0] === "list-item"; })) {
      increments.push(["list-item", 1]);
    }
    increments.forEach(function (op) {
      (innermostCounter(set, op[0]) || instantiateCounter(set, op[0], 0, origin)).value += op[1];
    });
  }

  /** The pseudo's `quotes` → `[[open, close], …]` (`auto` is the English pair). */
  function quotePairs(ps) {
    var value = String(ps.quotes || "auto");
    if (value === "none") return [];
    if (value === "auto") return [["“", "”"], ["‘", "’"]];
    var strings = contentParts(value).filter(function (p) { return p.type === "string"; })
                                     .map(function (p) { return p.value; });
    var pairs = [];
    for (var i = 0; i + 1 < strings.length; i += 2) pairs.push([strings[i], strings[i + 1]]);
    return pairs;
  }

  /**
   * A pseudo's computed `content` → `{text, image}`. Chromium substitutes `attr()` in the computed
   * value and leaves `counter()`/`counters()` as written (measured); both forms are handled.
   */
  function resolveContent(host, kind, ps, set, origin, quotes, state, warned) {
    var text = "", image = false;
    var where = cssPath(host) + "::" + kind;
    function counterStyle(name) {
      var style = String(name || "decimal").trim().toLowerCase();
      if (COUNTER_STYLES[style]) return style;
      if (!warned[style]) {
        warned[style] = true;
        diagnose(state, "warn", where, "counter style " + style + " is not supported; decimal used, approximated");
      }
      return "decimal";
    }
    function quoteLanguage() {
      // `quotes: auto` follows the content language; only the English pairs are known here.
      var lang = String((host.closest("[lang]") || {}).lang || document.documentElement.lang || "").toLowerCase();
      if (!/^(|en)(-|$)/.test(lang) && String(ps.quotes || "auto") === "auto" && !warned["quotes " + lang]) {
        warned["quotes " + lang] = true;
        diagnose(state, "warn", where, "quotes for lang " + lang + " are drawn as English quotes, approximated");
      }
    }
    function listItemCounter(name) {
      // Chromium numbers `counter(list-item)` in a reversed list, or after an `li value`, one way on
      // the first layout and another after any change to the page; the walk here counts in document
      // order. Said so, rather than a silently different number in a fixed-size circle.
      if (name !== "list-item" || warned["list-item"]) return;
      var list = host.closest("ol, ul, menu");
      if (list && (list.hasAttribute("reversed") || list.querySelector("li[value]"))) {
        warned["list-item"] = true;
        diagnose(state, "warn", where, "counter(list-item) in a reversed or renumbered list is resolved in document order, approximated");
      }
    }
    contentParts(ps.content).forEach(function (part) {
      if (part.type === "string") { text += part.value; return; }
      if (part.type === "keyword") {
        if (/^(open|close)-quote$/.test(part.name)) quoteLanguage();
        var pairs = quotePairs(ps), level = Math.min(quotes.depth, pairs.length - 1);
        if (part.name === "open-quote") {
          if (pairs.length) text += pairs[Math.max(0, level)][0];
          quotes.depth++;
        } else if (part.name === "close-quote") {
          if (quotes.depth > 0) {
            quotes.depth--;
            if (pairs.length) text += pairs[Math.min(quotes.depth, pairs.length - 1)][1];
          }
        } else if (part.name === "no-open-quote") {
          quotes.depth++;
        } else if (part.name === "no-close-quote") {
          if (quotes.depth > 0) quotes.depth--;
        }
        return;
      }
      var args = splitArgs(part.args);
      if (part.name === "attr") {
        var attrName = String(args[0] || "").trim().split(/\s+/)[0];
        var attrValue = attrName ? host.getAttribute(attrName) : null;
        text += attrValue !== null ? attrValue : (args.length > 1 ? stringArg(args[1]) : "");
      } else if (part.name === "counter") {
        var name = String(args[0] || "").trim();
        listItemCounter(name);
        var counter = innermostCounter(set, name) || instantiateCounter(set, name, 0, origin);
        text += counterText(counter.value, counterStyle(args[1]));
      } else if (part.name === "counters") {
        var many = String(args[0] || "").trim();
        listItemCounter(many);
        if (!innermostCounter(set, many)) instantiateCounter(set, many, 0, origin);
        var style = counterStyle(args[2]);
        text += set.filter(function (c) { return c.name === many; })
                   .map(function (c) { return counterText(c.value, style); })
                   .join(stringArg(args[1]));
      } else if (/^(url|image-set|-webkit-image-set|image|cross-fade|element|(repeating-)?(linear|radial|conic)-gradient)$/.test(part.name)) {
        image = true;
      }
    });
    return { text: text, image: image };
  }

  // -- the pre-pass

  /**
   * Does this computed style paint anything by itself? Broad on purpose: a fill, a painted side, any
   * shadow layer (an inset ring too), a picture, an outline, a backdrop filter. What the export cannot
   * draw of it is then reported by the walk's sweep (`reportUndrawnPseudos`), never skipped in silence.
   */
  function pseudoPaints(ps) {
    if (fillOf(ps, boxOf(0, 0, 1, 1))) return true;
    if (shadowLayers(ps).length) return true;
    if (/url\(/i.test(ps.backgroundImage || "")) return true;
    var sides = sideBorders(ps);
    if (sides.top || sides.right || sides.bottom || sides.left) return true;
    var outline = ps.outlineStyle && ps.outlineStyle !== "none" && px(ps.outlineWidth) > 0
      ? parseColor(ps.outlineColor) : null;
    if (outline && outline.alpha > 0) return true;
    return !!ps.backdropFilter && ps.backdropFilter !== "none";
  }

  function pseudoWarning(state, where, kind, reason) {
    // Literal messages, one call each, so `server/tests/test_words.py` finds every one in the source.
    if (kind === "before") {
      if (reason === "moved") diagnose(state, "warn", where, "a ::before could not be rebuilt without moving the layout; dropped");
      else if (reason === "row") diagnose(state, "warn", where, "a ::before on a table row is not drawn by the table export; dropped");
      else if (reason === "table") diagnose(state, "warn", where, "a ::before on a table is not drawn by the table export; dropped");
      else if (reason === "image") diagnose(state, "warn", where, "::before image content dropped");
      else if (reason === "root") diagnose(state, "warn", where, "a ::before on the page itself is not drawn; dropped");
      else if (reason === "control") diagnose(state, "warn", where, "a ::before on a form control is not drawn; dropped");
      else if (reason === "undrawn") diagnose(state, "warn", where, "a ::before was rebuilt but the export cannot draw it in its place; dropped");
    } else {
      if (reason === "moved") diagnose(state, "warn", where, "a ::after could not be rebuilt without moving the layout; dropped");
      else if (reason === "row") diagnose(state, "warn", where, "a ::after on a table row is not drawn by the table export; dropped");
      else if (reason === "table") diagnose(state, "warn", where, "a ::after on a table is not drawn by the table export; dropped");
      else if (reason === "image") diagnose(state, "warn", where, "::after image content dropped");
      else if (reason === "root") diagnose(state, "warn", where, "a ::after on the page itself is not drawn; dropped");
      else if (reason === "control") diagnose(state, "warn", where, "a ::after on a form control is not drawn; dropped");
      else if (reason === "undrawn") diagnose(state, "warn", where, "a ::after was rebuilt but the export cannot draw it in its place; dropped");
    }
  }

  function rectOf(target) {
    var r = target.getBoundingClientRect();
    return [r.left, r.top, r.width, r.height];
  }

  /** Every computed longhand, in the order the browser lists them. */
  function styleSignature(cs) {
    var values = [];
    for (var i = 0; i < cs.length; i++) values.push(cs.getPropertyValue(cs.item(i)));
    return values;
  }

  /**
   * Two signatures that are the same style: value by value, with `sameComputed`'s px tolerance —
   * used lengths (`width`, `inline-size`, `transform-origin` …) are layout, and a shrink-to-fit box
   * reads 71.5781px before and 71.5625px after an exact rebuild (1/64 px, measured on the E3 review's
   * counter case), which is no change at all.
   */
  function sameSignature(a, b) {
    if (a.length !== b.length) return false;
    for (var i = 0; i < a.length; i++) if (a[i] !== b[i] && !sameComputed(a[i], b[i])) return false;
    return true;
  }

  /**
   * Check (iii)'s signature of one element: **every** computed longhand, plus its `::marker`'s when
   * it is a list item. A list of "paint" properties is never complete — a structural rule the new
   * child flips can change a radius, a transform, a z-index, a background position or a marker
   * colour without moving a rect (found by the E3 review: a segmented control's last radius, a
   * mirrored arrow, a z order, `li:first-child::marker`) — so nothing is left out. Measured on the
   * 15 surveyed app slides: no original element changes at all when every copy is kept.
   */
  function paintOf(el, cs) {
    var signature = styleSignature(cs);
    if (cs.display === "list-item") {
      try { signature = signature.concat(styleSignature(getComputedStyle(el, "::marker"))); } catch (error) { /* none */ }
    }
    return signature;
  }

  /** A live pseudo-element's signature: every longhand, `content` included. */
  function pseudoSignature(record) {
    return styleSignature(getComputedStyle(record.host, "::" + record.kind));
  }

  /**
   * Every element under <body> (probes excepted) with its rect and paint, every text node's rect,
   * and the signature of every pseudo-element found — a pseudo that stays live (skipped, dropped,
   * reverted or not rebuilt yet) must not be restyled by a copy either.
   */
  function layoutSnapshot(records) {
    var snapshot = { elements: [], texts: [], pseudos: [], range: document.createRange() };
    var all = [document.body].concat(Array.prototype.slice.call(document.body.querySelectorAll("*")));
    all.forEach(function (el) {
      if (el.closest("[data-engine-probe]")) return;
      var cs = getComputedStyle(el);
      snapshot.elements.push({ el: el, cs: cs, rect: rectOf(el), paint: paintOf(el, cs) });
    });
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    var node;
    while ((node = walker.nextNode())) {
      if (node.parentElement && node.parentElement.closest("[data-engine-probe]")) continue;
      snapshot.range.selectNodeContents(node);
      snapshot.texts.push({ node: node, rect: rectOf(snapshot.range) });
    }
    (records || []).forEach(function (record) {
      snapshot.pseudos.push({ record: record, signature: pseudoSignature(record) });
    });
    return snapshot;
  }

  function sameRect(a, b) {
    for (var i = 0; i < 4; i++) if (Math.abs(a[i] - b[i]) > PSEUDO_SHIFT_EPS) return false;
    return true;
  }

  /**
   * Checks (i) and (iii): nothing snapshotted moved, nothing repainted, no live pseudo restyled.
   * Returns the first thing that changed (for the report), or null when the snapshot holds.
   */
  function snapshotBreak(snapshot) {
    for (var i = 0; i < snapshot.elements.length; i++) {
      var item = snapshot.elements[i];
      if (!sameRect(rectOf(item.el), item.rect)) return item.el;
    }
    for (var t = 0; t < snapshot.texts.length; t++) {
      snapshot.range.selectNodeContents(snapshot.texts[t].node);
      if (!sameRect(rectOf(snapshot.range), snapshot.texts[t].rect)) return snapshot.texts[t].node.parentElement;
    }
    for (var p = 0; p < snapshot.elements.length; p++) {
      var entry = snapshot.elements[p];
      if (!sameSignature(paintOf(entry.el, entry.cs), entry.paint)) return entry.el;
    }
    for (var q = 0; q < snapshot.pseudos.length; q++) {
      var pseudo = snapshot.pseudos[q];
      if (pseudo.record.live && !sameSignature(pseudoSignature(pseudo.record), pseudo.signature)) return pseudo.record.host;
    }
    return null;
  }

  function snapshotHolds(snapshot) {
    return snapshotBreak(snapshot) === null;
  }

  function hostTokens(host) {
    return String(host.getAttribute("data-engine-pseudo-host") || "").split(/\s+/).filter(Boolean);
  }

  function setHostToken(host, kind, on) {
    var tokens = hostTokens(host).filter(function (t) { return t !== kind; });
    if (on) tokens.push(kind);
    if (tokens.length) host.setAttribute("data-engine-pseudo-host", tokens.sort().join(" "));
    else host.removeAttribute("data-engine-pseudo-host");
  }

  /** Phase 3: every generated pseudo in document order, with its counters, quotes and styles read. */
  function collectPseudos(state) {
    var found = [], quotes = { depth: 0 }, warned = {};

    function pseudoOf(host, kind, hostSet, siblingSet) {
      var ps = getComputedStyle(host, "::" + kind);
      var content = ps.content;
      if (!content || content === "none" || content === "normal" || ps.display === "none") return null;
      // Generated, so it counts and quotes even when it is invisible or has nothing to show.
      var set = inheritCounters(hostSet, siblingSet);
      var origin = { parent: host, order: kind === "before" ? -1 : Infinity };
      applyCounters(set, null, ps, origin);
      var resolved = resolveContent(host, kind, ps, set, origin, quotes, state, warned);
      var record = { host: host, kind: kind, where: (host === document.documentElement ? "html" : cssPath(host)) + "::" + kind,
                     text: resolved.text, image: resolved.image, action: "build", reason: null, values: [], live: true };
      if (ps.visibility === "hidden" || ps.visibility === "collapse" || ps.opacity === "0") {
        record.action = "skip";
        return { record: record, set: set };
      }
      var paints = pseudoPaints(ps);
      var hostDisplay = getComputedStyle(host).display;
      // Dropped here: hosts the walk never draws children of. A pseudo on a table cell or inside
      // one is not among them (plan §16 #22): the cell walk (WP-A's partition) draws a rebuilt copy
      // like any child — a swatch or a badge as a shape over the table, its text in the cell's
      // paragraph or in a text box over it — and the sweep after the walk (`reportUndrawnPseudos`)
      // takes back and reports whatever it did not draw.
      if (host === document.documentElement || host.tagName === "INPUT") {
        // The page's own pseudo-elements sit outside <body>, where the walk never goes, and a form
        // control holds no children: neither can take a copy. Chromium draws a checkbox's or a
        // radio's only under `appearance: none` (the custom tick); said so rather than skipped.
        record.action = record.text || paints ? "drop" : "skip";
        record.reason = host === document.documentElement ? "root" : "control";
      } else if (/^table-/.test(hostDisplay) && hostDisplay !== "table-cell") {
        record.action = record.text || paints ? "drop" : "skip";
        record.reason = "row";
      } else if (hostDisplay === "table" || hostDisplay === "inline-table") {
        record.action = record.text || paints ? "drop" : "skip";
        record.reason = "table";
      } else if (!record.text && !paints) {
        // An empty pseudo that paints nothing (a clearfix) stays where it is: nothing to draw,
        // and leaving it means nothing can move. An image-only one is reported below.
        record.action = "skip";
      }
      if (record.action === "build") {
        for (var i = 0; i < ps.length; i++) {
          var name = ps.item(i);
          if (!PSEUDO_NOT_COPIED.test(name)) record.values.push([name, ps.getPropertyValue(name)]);
        }
      }
      return { record: record, set: set };
    }

    function visit(el, parentSet, siblingSet, order) {
      var set = inheritCounters(parentSet, siblingSet);
      if (el.hasAttribute("data-engine-probe") || el.hasAttribute("data-engine-pseudo")) return set;
      var cs = getComputedStyle(el);
      // No box, no counters and no pseudo-elements (CSS Lists 3 §4; CSS Pseudo 4 §2).
      if (cs.display === "none") return set;
      var tag = el.tagName.toLowerCase();
      if (cs.display === "contents" && !PSEUDO_SKIP_HOSTS[tag]) return visitContents(el, parentSet, siblingSet);
      applyCounters(set, el, cs, { parent: el.parentNode, order: order });
      if (tag === "input") {
        // No children and no copy; its pseudo-elements are reported when Chromium draws them.
        if (/^(checkbox|radio)$/i.test(el.type) && /^none$/.test(cs.appearance || cs.webkitAppearance || "")) {
          ["before", "after"].forEach(function (kind) {
            var own = pseudoOf(el, kind, set, null);
            if (own && own.record.action !== "skip") found.push(own.record);
          });
        }
        return set;
      }
      if (PSEUDO_SKIP_HOSTS[tag] || (tag === "aside" && el.classList.contains("notes"))) return set;
      var previous = null;
      var before = pseudoOf(el, "before", set, null);
      if (before) { found.push(before.record); previous = before.set; }
      var index = 0;
      for (var child = el.firstElementChild; child; child = child.nextElementSibling) {
        previous = visit(child, set, previous, index++);
      }
      var after = pseudoOf(el, "after", set, previous);
      if (after) found.push(after.record);
      return set;
    }

    /**
     * A `display: contents` element has no box, so Chromium — which keeps counters on the layout
     * tree — ignores its own counter properties, and its pseudo-elements and children count as its
     * parent's children (E3 review m1: `.dc{display:contents;counter-reset:k}` wrappers number 1, 2,
     * 3 across the wrappers, not 1, 2, 1). Returns what its next sibling inherits: the counters of the
     * last box it contributed.
     */
    function visitContents(el, parentSet, siblingSet) {
      var chain = siblingSet, index = 0;
      var before = pseudoOf(el, "before", parentSet, chain);
      if (before) { found.push(before.record); chain = before.set; }
      for (var child = el.firstElementChild; child; child = child.nextElementSibling) {
        chain = visit(child, parentSet, chain, index++);
      }
      var after = pseudoOf(el, "after", parentSet, chain);
      if (after) { found.push(after.record); chain = after.set; }
      return chain || inheritCounters(parentSet, siblingSet);
    }

    // The page's own `html::before`/`::after` generate outside <body>: reported, never copied.
    var root = document.documentElement, rootBefore = pseudoOf(root, "before", [], null);
    if (rootBefore && rootBefore.record.action !== "skip") found.push(rootBefore.record);
    visit(document.body, [], null, 0);
    var rootAfter = pseudoOf(root, "after", [], null);
    if (rootAfter && rootAfter.record.action !== "skip") found.push(rootAfter.record);
    return found;
  }

  /**
   * Two computed values that are the same value: equal strings, or the same text with px lengths
   * within PSEUDO_SHIFT_EPS. Used lengths are 1/64 px units serialised to 6 digits, so a pseudo's
   * shrink-to-fit width of 269/64 px reads `4.20312px` and, set back, lays out as 268/64 = 4.1875 —
   * a 0.016 px difference the exact comparison turned into a dropped bullet (measured on six real
   * slides, 2026-09-25).
   */
  function sameComputed(a, b) {
    if (a === b) return true;
    var length = /-?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?px/g;
    if (String(a).replace(length, "L") !== String(b).replace(length, "L")) return false;
    var x = String(a).match(length) || [], y = String(b).match(length) || [];
    for (var i = 0; i < x.length; i++) {
      if (Math.abs(parseFloat(x[i]) - parseFloat(y[i])) > PSEUDO_SHIFT_EPS) return false;
    }
    return true;
  }

  /**
   * Give the inserted copy the pseudo's computed values: every longhand that differs from what the
   * copy already computes is set inline and `!important` (inline beats stylesheet `!important`, so
   * an author rule that matches real elements — `.card * { margin: 4px }` — cannot restyle it; and
   * a forced font the pseudo had is kept). A longhand that already agrees is left inherited, as the
   * pseudo's own was: setting every one explicitly (the first design) made Chromium stop shaping
   * the copy's text together with its neighbours, and the lost kerning moved the text beside a
   * quote or an arrow by 0.7–1.7 px — measured on Edge 153, 2026-09-25 — so the rebuild was
   * reverted. Three passes, because setting one longhand can change another's computed value.
   */
  function settleCopy(copy, values) {
    for (var pass = 0; pass < 3; pass++) {
      var computed = getComputedStyle(copy), changed = false;
      values.forEach(function (pair) {
        if (sameComputed(computed.getPropertyValue(pair[0]), pair[1])) return;
        changed = true;
        try { copy.style.setProperty(pair[0], pair[1], "important"); } catch (error) { /* internal longhand */ }
      });
      if (!changed) return;
    }
  }

  /**
   * Suppress one pseudo and insert its copy where it was, settled to the pseudo's values. Null when
   * (0) fails — an author layer declared before ours out-ranks the suppression — with nothing left
   * changed.
   */
  function insertCopy(record) {
    var host = record.host, kind = record.kind;
    setHostToken(host, kind, true);
    if (getComputedStyle(host, "::" + kind).content !== "none") {                  // (0)
      setHostToken(host, kind, false);
      return null;
    }
    record.live = false;
    var copy = document.createElement("engine-pseudo");
    copy.setAttribute("data-engine-pseudo", kind);
    if (record.text) copy.textContent = record.text;
    if (kind === "before") host.insertBefore(copy, host.firstChild);
    else host.appendChild(copy);
    settleCopy(copy, record.values);
    return copy;
  }

  /**
   * (ii) the copy computes to the pseudo's own value for every longhand it took — the colours,
   * borders, radii, shadow, opacity, font, line height, spacing, alignment, size, insets, display,
   * position, z-index, transform and clip-path among them.
   */
  function copyMatches(copy, record) {
    var computed = getComputedStyle(copy);
    return record.values.every(function (pair) { return sameComputed(computed.getPropertyValue(pair[0]), pair[1]); });
  }

  function removeCopy(record, copy) {
    if (copy && copy.parentNode) copy.parentNode.removeChild(copy);
    setHostToken(record.host, record.kind, false);
    record.live = true;
  }

  /** A kept copy joins the snapshot: later rebuilds must not move it either. */
  function snapshotCopy(snapshot, copy) {
    var cs = getComputedStyle(copy);
    snapshot.elements.push({ el: copy, cs: cs, rect: rectOf(copy), paint: paintOf(copy, cs) });
    for (var node = copy.firstChild; node; node = node.nextSibling) {
      if (node.nodeType !== 3) continue;
      snapshot.range.selectNodeContents(node);
      snapshot.texts.push({ node: node, rect: rectOf(snapshot.range) });
    }
  }

  /** Phase 4 for one pseudo: rebuild, verify (0)–(iii), keep or revert. True when kept. */
  function rebuildPseudo(record, snapshot) {
    var copy = insertCopy(record);
    var ok = !!copy && snapshotHolds(snapshot) && copyMatches(copy, record);       // (i), (iii), (ii)
    if (!ok) {
      removeCopy(record, copy);
      return false;
    }
    snapshotCopy(snapshot, copy);
    return true;
  }

  /**
   * Phase 4 for all of them, `indices` in reverse document order. First all at once, verified once:
   * the walk measures the final page, and a page where every copy computes to its pseudo and no
   * rect or paint changed is exactly the page the reference shows — one layout check instead of one
   * per pseudo (a 34-pseudo slide went from 0.6 s to a fifth of that). If that fails, everything is
   * taken back and rebuilt one at a time, in the same order, so each failure is attributed to its
   * own pseudo and reverted alone. Returns `{index: kept}`.
   */
  function rebuildAll(records, indices, snapshot, state) {
    var kept = {}, batch = [];
    indices.forEach(function (index) {
      var copy = insertCopy(records[index]);
      if (copy) batch.push([index, copy]);
      else kept[index] = false;
    });
    var together = batch.length > 0 && snapshotHolds(snapshot) &&
                   batch.every(function (entry) { return copyMatches(entry[1], records[entry[0]]); });
    if (together) {
      batch.forEach(function (entry) { kept[entry[0]] = true; snapshotCopy(snapshot, entry[1]); });
      return kept;
    }
    batch.forEach(function (entry) { removeCopy(records[entry[0]], entry[1]); });
    // Taking the batch back should give the first layout again. When it does not — Chromium numbers
    // `counter(list-item)` in a reversed list one way on the first layout and another after any
    // mutation; a running animation moves on — every later check would fail against a layout that no
    // longer exists and every pseudo would be dropped (E3 review m2). Say so once, and judge each
    // rebuild against the page as it now is: the walk measures that page too.
    if (batch.length && !snapshotHolds(snapshot)) {
      var changed = snapshot.elements.filter(function (item) {
        return !sameRect(rectOf(item.el), item.rect) || !sameSignature(paintOf(item.el, item.cs), item.paint);
      }).length + snapshot.texts.filter(function (item) {
        snapshot.range.selectNodeContents(item.node);
        return !sameRect(rectOf(snapshot.range), item.rect);
      }).length;
      diagnose(state, "info", "pseudo-elements",
               "the page did not return to its first layout when its rebuilt pseudo-elements were taken back (" +
               changed + " elements or texts moved or restyled); each rebuild was checked against the new layout");
      var fresh = layoutSnapshot(records);
      snapshot.elements = fresh.elements;
      snapshot.texts = fresh.texts;
      snapshot.pseudos = fresh.pseudos;
    }
    indices.forEach(function (index) {
      if (kept[index] === false) return;
      kept[index] = rebuildPseudo(records[index], snapshot);
    });
    return kept;
  }

  /**
   * Rebuild every `::before`/`::after` as a real element, verified not to move or repaint anything.
   * Returns `{materialised: ["<path>::before", …], dropped: […]}`; its diagnostics wait on
   * `window.__enginePrepass` for `__engineExtract`, which reports them with the walk's own.
   */
  window.__engineMaterialisePseudo = function () {
    var state = { diagnostics: [] };
    window.__enginePrepass = state.diagnostics;
    var result = { materialised: [], dropped: [] };
    if (!document.body) return result;
    var style = document.createElement("style");
    style.setAttribute("data-engine-probe", "");
    style.textContent =
      "@layer engine-pseudo {" +
      " [data-engine-pseudo-host~=\"before\"]::before { content: none !important; }" +
      " [data-engine-pseudo-host~=\"after\"]::after { content: none !important; }" +
      " [data-engine-pseudo]::before, [data-engine-pseudo]::after { content: none !important; }" +
      " }";
    (document.head || document.documentElement).appendChild(style);
    // While the rebuild runs, a style the inserted child flips must change at once, not start a
    // transition: at t = 0 a transition still computes to the old value, so check (iii) would pass
    // and the walk would later read a different colour. Removed again before the walk.
    var still = document.createElement("style");
    still.setAttribute("data-engine-probe", "");
    still.textContent = "@layer engine-pseudo { *, *::before, *::after { transition-property: none !important; } }";
    (document.head || document.documentElement).appendChild(still);

    var records = collectPseudos(state);
    var snapshot = records.some(function (r) { return r.action === "build"; }) ? layoutSnapshot(records) : null;
    var indices = [];
    for (var i = records.length - 1; i >= 0; i--) if (records[i].action === "build") indices.push(i);
    var kept = snapshot ? rebuildAll(records, indices, snapshot, state) : {};
    // Settle every style the rebuilds and reverts touched while transitions are still frozen (a
    // style is resolved lazily: left to the walk, a reverted sibling would transition back).
    document.body.getBoundingClientRect();
    still.parentNode.removeChild(still);
    // Report in document order, whatever order the rebuild ran in.
    records.forEach(function (record, index) {
      if (record.action === "skip") {
        if (record.image) { pseudoWarning(state, record.where, record.kind, "image"); result.dropped.push(record.where); }
        return;
      }
      if (record.action === "drop") {
        pseudoWarning(state, record.where, record.kind, record.reason);
        result.dropped.push(record.where);
        return;
      }
      if (record.image) pseudoWarning(state, record.where, record.kind, "image");
      if (!kept[index]) {
        pseudoWarning(state, record.where, record.kind, "moved");
        result.dropped.push(record.where);
        return;
      }
      result.materialised.push(record.where);
    });
    return result;
  };

  // ------------------------------------------------------------------ entry point

  /**
   * The walk reads `opacity` and rects at one instant; if anything is still animating, that instant
   * is arbitrary and the IR differs from run to run. `settle.js` (run by `prepare_page`) brings the
   * page to rest first — this is the tripwire for a caller that forgot, or for an animation that
   * would not settle, so a moving page can never produce a quiet IR (WP-F, F-A).
   */
  function settleTripwire(state) {
    if (typeof document.getAnimations !== "function") return;
    var moving = document.getAnimations().filter(function (a) { return a.playState === "running"; }).length;
    if (!moving) return;
    diagnose(state, "error", "settle",
             "measurement ran with " + moving + " animation(s) still running" +
             (window.__engineSettled ? "" : "; the page was not settled before it was measured"), null);
  }

  /**
   * The silent-drop sweep (render check R1). Defects 1 and 2 left nothing behind — an icon in an
   * inline `<span>`, a row of empty rating dots — so the readiness panel had nothing to show. After
   * the walk, every visible thing that paints is checked against what the walk pushed, and a warn
   * names each one no record carries:
   *
   *  - an `<svg>` root with no `svgPlaceholder`, and any other special element (`<img>`, `<canvas>`,
   *    a chart host, a forced raster, …) with no record of its own and no finding already about it;
   *  - an inline box (`inline`, `inline-block`, `inline-flex`, …) that paints a fill, border, shadow
   *    or clip shape, and was neither decorated (`emitInlineDecoration`, `emitCellChip`) nor walked
   *    as an element (a record with its path).
   *
   * Anything inside a raster or a chart is carried by that record's pixels or spec. Rebuilt
   * pseudo-elements have their own sweep (`reportUndrawnPseudos`), and HTML inside an `<svg>` is the
   * SVG expansion's.
   */
  function reportSilentDrops(state) {
    var drawn = {}, placed = {}, said = {}, rasters = [];
    state.records.forEach(function (record) {
      if (record.kind === "svgPlaceholder") placed[record.svgId] = true;
      else if (record.kind === "raster" && record.selector) {
        var captured = document.querySelector(record.selector);
        if (captured) rasters.push(captured);
      } else if (record.kind !== "text" && record.source && record.source.path) drawn[record.source.path] = true;
    });
    state.diagnostics.forEach(function (d) { said[d.source] = true; });
    var decorated = new Set(state.decorated);
    /** Inside a chart host (its record is the chart) or a captured raster (its pixels hold it)? */
    function inCover(el) {
      if (el.closest("[data-chart]")) return true;
      for (var r = 0; r < rasters.length; r++) if (rasters[r].contains(el)) return true;
      return false;
    }
    function shows(el, cs) {
      if (cs.display === "none" || cs.visibility === "hidden") return false;
      if (el.closest("aside.notes, [data-engine-probe]")) return false;
      var rects = el.getClientRects();
      for (var i = 0; i < rects.length; i++) {
        if (rects[i].width >= MIN_EXTENT && rects[i].height >= MIN_EXTENT) return true;
      }
      return false;
    }
    var all = document.body.querySelectorAll("*");
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (el.hasAttribute("data-engine-pseudo")) continue;
      var svgRoot = el.hasAttribute("data-engine-svg");
      if (!svgRoot && el.namespaceURI === "http://www.w3.org/2000/svg") continue;   // the expansion's
      var cs = getComputedStyle(el), path;
      if (svgRoot || isSpecial(el)) {
        if (svgRoot && placed[el.getAttribute("data-engine-svg")]) continue;
        if (!svgRoot && el.parentElement && el.parentElement.closest("svg")) continue;   // in a foreignObject
        if (!shows(el, cs) || inCover(el)) continue;
        path = cssPath(el);
        if (drawn[path] || said[path]) continue;
        diagnose(state, "warn", path,
                 "not exported: this <" + el.tagName.toLowerCase() + ">" +
                 (svgRoot ? " (" + el.getAttribute("data-engine-svg") + ")" : "") +
                 " is visible, but the walk never reached it");
        continue;
      }
      if (!isInlineDisplay(cs.display) || cs.display === "contents" || decorated.has(el)) continue;
      var first = el.getClientRects()[0];
      if (!first || !hasDecoration(el, cs, localBox(IDENTITY_FRAME, first)) || !shows(el, cs)) continue;
      if (el.parentElement && el.parentElement.closest("svg")) continue;   // HTML in a foreignObject
      if (inCover(el) || drawn[path = cssPath(el)]) continue;
      diagnose(state, "warn", path,
               "not exported: this inline <" + el.tagName.toLowerCase() + "> paints a box (fill, border or " +
               "shadow) that the walk neither decorated nor walked");
    }
  }

  /** Tag every `<svg>` root before the walk so WP2 can find them by a stable id. */
  function tagSvgRoots(state) {
    var roots = document.querySelectorAll("svg");
    var index = 0;
    for (var i = 0; i < roots.length; i++) {
      if (roots[i].parentElement && roots[i].parentElement.closest("svg")) continue;   // nested
      var id = "svg#" + ++index;
      roots[i].setAttribute("data-engine-svg", id);
      state.svgIds.push(id);
    }
  }

  /** Intrinsic sizes for `background-image: url(...)`, needed before any of them can be measured. */
  function backgroundSizes() {
    var sizes = {};
    var all = document.querySelectorAll("*");
    for (var i = 0; i < all.length; i++) {
      var image = getComputedStyle(all[i]).backgroundImage;
      if (!image || image === "none") continue;
      var match = splitTop(image, ",")[0].match(/^url\((['"]?)(.*?)\1\)$/i);
      if (!match || sizes[match[2]]) continue;
      sizes[match[2]] = null;
    }
    return sizes;
  }

  window.__engineExtract = function (options) {
    options = options || {};
    var state = {
      records: [], groups: [], diagnostics: [], svgIds: [], counter: 0, tagCounts: {},
      decorated: [], backgroundSizes: options.backgroundSizes || {},
    };
    // The pseudo-element pre-pass ran first, on its own `state`: its findings open the walk's.
    if (window.__enginePrepass) state.diagnostics = window.__enginePrepass.slice();
    settleTripwire(state);
    tagSvgRoots(state);
    bandCache = new WeakMap();
    ellipsisCache = {};
    var bodyStyle = getComputedStyle(document.body);
    var canvasClip = boxOf(0, 0, options.canvasW || window.innerWidth, options.canvasH || window.innerHeight);
    state.canvasBox = canvasClip;
    state.remotePolicy = options.remotePolicy === "raster" ? "raster" : "block";
    state.blockedUrls = options.blockedUrls || {};
    var ctx = {
      opacity: parseFloat(bodyStyle.opacity) || 1,
      clip: /hidden|clip/.test(bodyStyle.overflow + bodyStyle.overflowX + bodyStyle.overflowY)
        ? canvasClip : null,
      group: null,
      frame: IDENTITY_FRAME,
    };
    if (hasDecoration(document.body, bodyStyle, box(document.body.getBoundingClientRect()))) {
      emitDecoration(document.body, bodyStyle, ctx, state);
    }
    walkChildren(document.body, ctx, state);
    reportSilentDrops(state);              // R1: a visible box or icon no record carries is said so
    reportUndrawnPseudos(state);           // WP-E: a rebuilt pseudo the walk did not draw is said so
    if (probeHost && probeHost.parentNode) { probeHost.parentNode.removeChild(probeHost); probeHost = null; }
    // One finding per colour string nobody could read, however many elements used it.
    window.__engineColor.unparsed().forEach(function (text) {
      diagnose(state, "warn", "colours", "colour " + text + " could not be read; the paint that used it is dropped");
    });

    var notes = document.querySelector("aside.notes");
    var used = {};
    var all = document.querySelectorAll("*");
    for (var i = 0; i < all.length; i++) {
      var family = String(getComputedStyle(all[i]).fontFamily).split(",")[0].replace(/["']/g, "").trim();
      if (family) used[family] = true;
    }
    return {
      records: state.records,
      groups: state.groups,
      diagnostics: state.diagnostics,
      svgIds: state.svgIds,
      notes: notes ? notes.textContent.trim() : null,
      fontsUsed: Object.keys(used).sort(),
    };
  };

  /** Intrinsic sizes of `background-image` URLs; awaited by Python before the walk. */
  window.__engineBackgroundSizes = function () {
    var urls = Object.keys(backgroundSizes());
    return Promise.all(urls.map(function (url) {
      return new Promise(function (resolve) {
        var image = new Image();
        image.onload = function () { resolve([url, { w: image.naturalWidth, h: image.naturalHeight }]); };
        image.onerror = function () { resolve([url, null]); };
        image.src = url;
      });
    })).then(function (pairs) {
      var sizes = {};
      pairs.forEach(function (pair) { if (pair[1]) sizes[pair[0]] = pair[1]; });
      return sizes;
    });
  };
})();
