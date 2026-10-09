/* engine/extract/svg.js — the browser half of the SVG extractor (WP2).
 *
 * Principle (12-WP2 §Principle): let Chromium resolve geometry, let Python produce exact curves.
 * This file never re-implements transform maths and never reads a raw presentation attribute that
 * CSS can override — every paint property comes from getComputedStyle, so inheritance through <g>,
 * CSS classes and pt units is already resolved, and every position comes from getScreenCTM()/
 * getBBox(), so viewBox, preserveAspectRatio, nested `g transform` and nested `svg` are already
 * resolved. getScreenCTM() returns client pixels, which are canvas pixels: the measuring page is
 * exactly canvas-sized at device scale 1 and never scrolls (`scroll` is reported so Python can
 * refuse a page where that stopped being true).
 *
 * `window.__engineSvg.describe(selector)` returns a JSON-able tree for one `<svg>` root. It mutates
 * the DOM only where the platform gives no other answer — a `<use>` shadow tree is not walkable,
 * and a rotated `<text>` has no usable per-run box — and it undoes every mutation before it
 * returns, so the isolated raster captures Python takes afterwards see the original page.
 *
 * Every described element keeps a `data-engine-svg-node` attribute after the call: that is the
 * selector Python screenshots for an isolated raster. `window.__engineSvg.cleanup()` removes them.
 */
(() => {
  "use strict";

  const SVGNS = "http://www.w3.org/2000/svg";
  const XLINK = "http://www.w3.org/1999/xlink";
  const NODE_ATTR = "data-engine-svg-node";
  const SYNTHETIC_ATTR = "data-engine-synthetic";

  /** Elements that define paint or content but are never painted where they sit. */
  const NON_RENDERED = new Set([
    "defs", "symbol", "marker", "clippath", "mask", "pattern", "lineargradient", "radialgradient",
    "meshgradient", "solidcolor", "title", "desc", "metadata", "style", "script", "filter",
    "animate", "animatetransform", "animatemotion", "animatecolor", "set", "mpath", "view",
    "font", "font-face", "hatch", "hatchpath",
  ]);
  const SHAPE_TAGS = new Set(["rect", "circle", "ellipse", "line", "polyline", "polygon", "path"]);
  const CONTAINER_TAGS = new Set(["g", "a", "svg", "switch"]);

  // ------------------------------------------------------------------ small helpers

  const tagOf = (el) => el.tagName.toLowerCase();
  const num = (value, fallback) => {
    const parsed = parseFloat(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  };
  const attrNum = (el, name, fallback) => num(el.getAttribute(name), fallback);
  const hrefOf = (el) => el.getAttribute("href") || el.getAttributeNS(XLINK, "href") || "";

  function matrixOf(el) {
    try {
      const m = el.getScreenCTM();
      return m ? [m.a, m.b, m.c, m.d, m.e, m.f] : null;
    } catch (e) {
      return null;
    }
  }

  function bboxOf(el) {
    try {
      const b = el.getBBox();
      return [b.x, b.y, b.width, b.height];
    } catch (e) {
      return null;
    }
  }

  function rectOf(el) {
    const r = el.getBoundingClientRect();
    return [r.x, r.y, r.width, r.height];
  }

  /** The element a `url(#id)` paint/clip/filter/marker reference points at. */
  function referenced(value) {
    if (!value || value === "none") return null;
    const match = /url\(\s*["']?#([^"')\s]+)["']?\s*\)/.exec(value);
    return match ? document.getElementById(match[1]) : null;
  }

  const elementChildren = (el) => Array.from(el.childNodes).filter((n) => n.nodeType === 1);

  /**
   * A CSS path from `body` to `el` — the IR's `source.path` for the svg root. Elements the
   * pseudo-element pre-pass inserted (`[data-engine-pseudo]`, page.js) are not counted, so every
   * path is the one the author's own document has.
   */
  function cssPath(el) {
    const parts = [];
    for (let node = el; node && node.nodeType === 1 && tagOf(node) !== "body"; node = node.parentElement) {
      if (node.id) {
        parts.unshift(tagOf(node) + "#" + node.id);
        break;
      }
      const siblings = (node.parentNode ? elementChildren(node.parentNode) : [node])
        .filter((sibling) => sibling === node || !sibling.hasAttribute("data-engine-pseudo"));
      parts.unshift(tagOf(node) + ":nth-child(" + (siblings.indexOf(node) + 1) + ")");
    }
    return ["body"].concat(parts).join(" > ");
  }

  // ------------------------------------------------------------------ colours

  /**
   * Any CSS colour → `"rgba(r, g, b, a)"` through the shared normaliser (`color.js`), which is what
   * `svg.py: _colour` reads. An unreadable string is handed on unchanged — `_colour` then finds no
   * colour, as before — and `color.js` has recorded it for the page's warning.
   */
  function rgbaOf(value) {
    const colour = window.__engineColor.normalise(value);
    if (!colour) return value;
    const hex = colour.color;
    return "rgba(" + parseInt(hex.slice(0, 2), 16) + ", " + parseInt(hex.slice(2, 4), 16) + ", " +
           parseInt(hex.slice(4, 6), 16) + ", " + colour.alpha + ")";
  }

  // ------------------------------------------------------------------ computed style

  function styleOf(el) {
    const cs = getComputedStyle(el);
    const maskValue = cs.maskImage && cs.maskImage !== "none" ? cs.maskImage : cs.mask;
    return {
      fill: cs.fill,
      fillRule: cs.fillRule || "nonzero",
      stroke: cs.stroke,
      strokeWidth: num(cs.strokeWidth, 1),
      strokeDasharray: cs.strokeDasharray,
      strokeDashoffset: num(cs.strokeDashoffset, 0),
      strokeLinecap: cs.strokeLinecap,
      strokeLinejoin: cs.strokeLinejoin,
      opacity: num(cs.opacity, 1),
      fillOpacity: num(cs.fillOpacity, 1),
      strokeOpacity: num(cs.strokeOpacity, 1),
      fontFamily: cs.fontFamily,
      fontSize: num(cs.fontSize, 16),
      fontWeight: cs.fontWeight,
      fontStyle: cs.fontStyle,
      letterSpacing: cs.letterSpacing,
      textAnchor: cs.textAnchor,
      dominantBaseline: cs.dominantBaseline,
      textDecorationLine: cs.textDecorationLine,
      textTransform: cs.textTransform,
      markerStart: cs.markerStart,
      markerMid: cs.markerMid,
      markerEnd: cs.markerEnd,
      clipPath: cs.clipPath,
      mask: maskValue || "none",
      filter: cs.filter,
      mixBlendMode: cs.mixBlendMode,
      display: cs.display,
      visibility: cs.visibility,
      overflow: cs.overflow,
    };
  }

  // ------------------------------------------------------------------ paint servers

  /** Walk `href` inheritance so `<linearGradient id="b" href="#a">` inherits a's stops and coords. */
  function gradientChain(el) {
    const chain = [];
    const seen = new Set();
    let cursor = el;
    while (cursor && !seen.has(cursor)) {
      seen.add(cursor);
      chain.push(cursor);
      const href = hrefOf(cursor);
      cursor = href.startsWith("#") ? document.getElementById(href.slice(1)) : null;
    }
    return chain;
  }

  function inherited(chain, name) {
    for (const el of chain) {
      const value = el.getAttribute(name);
      if (value !== null && value !== "") return value;
    }
    return null;
  }

  function gradientStops(chain) {
    for (const el of chain) {
      const stops = elementChildren(el).filter((c) => tagOf(c) === "stop");
      if (stops.length) {
        return stops.map((stop) => {
          const cs = getComputedStyle(stop);
          const raw = (stop.getAttribute("offset") || "0").trim();
          const offset = raw.endsWith("%") ? num(raw, 0) / 100 : num(raw, 0);
          return { offset, color: rgbaOf(cs.stopColor), opacity: num(cs.stopOpacity, 1) };
        });
      }
    }
    return [];
  }

  function describeGradient(el) {
    const chain = gradientChain(el);
    const kind = tagOf(el) === "radialgradient" ? "radial" : "linear";
    const info = {
      type: "gradient",
      kind,
      units: inherited(chain, "gradientUnits") || "objectBoundingBox",
      spread: inherited(chain, "spreadMethod") || "pad",
      transform: inherited(chain, "gradientTransform"),
      stops: gradientStops(chain),
    };
    if (kind === "linear") {
      info.x1 = inherited(chain, "x1");
      info.y1 = inherited(chain, "y1");
      info.x2 = inherited(chain, "x2");
      info.y2 = inherited(chain, "y2");
    } else {
      info.cx = inherited(chain, "cx");
      info.cy = inherited(chain, "cy");
      info.r = inherited(chain, "r");
    }
    return info;
  }

  /** `getComputedStyle().fill|stroke` → a paint description Python turns into an IR fill. */
  function describePaint(value) {
    if (!value || value === "none") return { type: "none" };
    const target = referenced(value);
    if (target) {
      const t = tagOf(target);
      if (t === "lineargradient" || t === "radialgradient") return describeGradient(target);
      if (t === "pattern") return { type: "pattern" };
      return { type: "unsupported", detail: t };
    }
    if (value.indexOf("url(") === 0) return { type: "unsupported", detail: value };
    return { type: "color", value: rgbaOf(value) };
  }

  // ------------------------------------------------------------------ clip / filter / marker

  /** `A ∘ B` for two [a,b,c,d,e,f] matrices, via DOMMatrix so the maths stays the platform's. */
  function compose(a, b) {
    const m = new DOMMatrix([a[0], a[1], a[2], a[3], a[4], a[5]])
      .multiply(new DOMMatrix([b[0], b[1], b[2], b[3], b[4], b[5]]));
    return [m.a, m.b, m.c, m.d, m.e, m.f];
  }

  function invert(a) {
    const m = new DOMMatrix([a[0], a[1], a[2], a[3], a[4], a[5]]).inverse();
    return [m.a, m.b, m.c, m.d, m.e, m.f];
  }

  /**
   * A `clip-path` that is one axis-aligned `rect` in the referencing element's user space is a
   * `clip` box in the IR; anything else has to be rasterised.
   *
   * The rect's own screen CTM is (root ∘ clipPath transform ∘ rect transform) — it does *not*
   * carry the referencing element's ancestors, because the clipPath lives in `<defs>`. The clip
   * is defined in the referencing element's user space, so the matrix that belongs to it is
   * `el.CTM ∘ root.CTM⁻¹ ∘ rect.CTM`, which collapses to `el.CTM` in the usual untransformed case.
   */
  function describeClip(el, style, rootCtm) {
    if (!style.clipPath || style.clipPath === "none") return null;
    const target = referenced(style.clipPath);
    if (!target || tagOf(target) !== "clippath") return { kind: "unsupported", detail: style.clipPath };
    const units = target.getAttribute("clipPathUnits") || "userSpaceOnUse";
    const kids = elementChildren(target).filter((c) => tagOf(c) !== "title" && tagOf(c) !== "desc");
    if (units !== "userSpaceOnUse" || kids.length !== 1 || tagOf(kids[0]) !== "rect") {
      return { kind: "unsupported", detail: units + "/" + kids.map(tagOf).join(",") };
    }
    const rect = kids[0];
    if (attrNum(rect, "rx", 0) > 0 || attrNum(rect, "ry", 0) > 0) {
      return { kind: "unsupported", detail: "rounded clip rect" };
    }
    const elCtm = matrixOf(el) || rootCtm;
    const rectCtm = matrixOf(rect);
    const ctm = rectCtm ? compose(elCtm, compose(invert(rootCtm), rectCtm)) : elCtm;
    return {
      kind: "rect",
      rect: [attrNum(rect, "x", 0), attrNum(rect, "y", 0),
             attrNum(rect, "width", 0), attrNum(rect, "height", 0)],
      ctm,
    };
  }

  /** `feDropShadow` (or CSS `drop-shadow()`) is a real shadow; every other filter is a raster. */
  function describeFilter(el, style) {
    if (!style.filter || style.filter === "none") return null;
    // One `drop-shadow(...)` and nothing else. Tokenised at parenthesis depth 0 *before* the
    // lengths are told from the colour: `drop-shadow(oklch(0.2 0.1 240) 0px 2px 4px)` is one colour.
    const whole = window.__engineColor.tokens(style.filter.trim());
    const css = whole.length === 1 ? /^drop-shadow\((.*)\)$/i.exec(whole[0]) : null;
    if (css) {
      const parts = window.__engineColor.tokens(css[1]);
      const isLength = (p) => /^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?(?:px)?$/i.test(p);
      const lengths = parts.filter(isLength).map((p) => num(p, 0));
      const colors = parts.filter((p) => !isLength(p));
      return {
        kind: "dropShadow",
        dx: lengths[0] || 0,
        dy: lengths[1] || 0,
        // CSS drop-shadow's third length is a blur *radius*; SVG's feDropShadow takes a sigma.
        stdDeviation: (lengths[2] || 0) / 2,
        color: rgbaOf(colors[0] || "rgb(0, 0, 0)"),
        opacity: 1,
      };
    }
    const target = referenced(style.filter);
    if (!target || tagOf(target) !== "filter") return { kind: "unsupported", detail: style.filter };
    const primitives = elementChildren(target).filter((c) => tagOf(c).indexOf("fe") === 0);
    if (primitives.length === 1 && tagOf(primitives[0]) === "fedropshadow") {
      const fe = primitives[0];
      const cs = getComputedStyle(fe);
      return {
        kind: "dropShadow",
        dx: attrNum(fe, "dx", 2),
        dy: attrNum(fe, "dy", 2),
        stdDeviation: attrNum(fe, "stdDeviation", 2),
        color: rgbaOf(cs.floodColor || fe.getAttribute("flood-color") || "rgb(0, 0, 0)"),
        opacity: num(cs.floodOpacity, num(fe.getAttribute("flood-opacity"), 1)),
      };
    }
    return { kind: "unsupported", detail: primitives.map(tagOf).join("+") || tagOf(target) };
  }

  /**
   * Classify a `<marker>` by the shape it draws so the IR can ask for a DrawingML line end
   * instead of a picture. Unknown markers come back as `unknown` and Python rasters the line.
   */
  function describeMarker(value) {
    const target = referenced(value);
    if (!target || tagOf(target) !== "marker") return null;
    // Size travels with the classification: a rastered line has to be captured wide enough to
    // include its marker, which the element's own bounding rect does not cover.
    const size = {
      width: attrNum(target, "markerWidth", 3),
      height: attrNum(target, "markerHeight", 3),
      units: target.getAttribute("markerUnits") || "strokeWidth",
    };
    const kids = elementChildren(target).filter((c) => !NON_RENDERED.has(tagOf(c)));
    const out = (type, detail) => Object.assign({ type }, size, detail ? { detail } : {});
    if (kids.length !== 1) return out("unknown", kids.length + " marker children");
    const child = kids[0];
    const t = tagOf(child);
    if (t === "circle" || t === "ellipse") return out("oval");
    if (t === "rect") return out("diamond");
    if (t === "line" || t === "polyline") return out("arrow");
    if (t === "polygon" || t === "path") {
      const corners = countCorners(child);
      if (corners === 3) return out("triangle");
      if (corners === 4) return out("diamond");
      return out("unknown", t + " with " + corners + " corners");
    }
    return out("unknown", t);
  }

  /** Corner count of a straight-segment outline; -1 when it curves (then it is not a line end). */
  function countCorners(el) {
    const t = tagOf(el);
    if (t === "polygon" || t === "polyline") {
      const values = (el.getAttribute("points") || "").trim().split(/[\s,]+/).filter(Boolean);
      return Math.floor(values.length / 2);
    }
    const d = el.getAttribute("d") || "";
    if (/[CcSsQqTtAa]/.test(d)) return -1;
    const commands = d.match(/[MLHVmlhv]/g);
    return commands ? commands.length : -1;
  }

  // ------------------------------------------------------------------ geometry attributes

  function geometryOf(el) {
    const t = tagOf(el);
    if (t === "rect") {
      const rxAttr = el.getAttribute("rx");
      const ryAttr = el.getAttribute("ry");
      const rx = rxAttr !== null ? num(rxAttr, 0) : (ryAttr !== null ? num(ryAttr, 0) : 0);
      const ry = ryAttr !== null ? num(ryAttr, 0) : (rxAttr !== null ? num(rxAttr, 0) : 0);
      return {
        kind: "rect",
        x: attrNum(el, "x", 0), y: attrNum(el, "y", 0),
        w: attrNum(el, "width", 0), h: attrNum(el, "height", 0),
        rx, ry,
      };
    }
    if (t === "circle") {
      const r = attrNum(el, "r", 0);
      return { kind: "ellipse", cx: attrNum(el, "cx", 0), cy: attrNum(el, "cy", 0), rx: r, ry: r, circle: true };
    }
    if (t === "ellipse") {
      return {
        kind: "ellipse", cx: attrNum(el, "cx", 0), cy: attrNum(el, "cy", 0),
        rx: attrNum(el, "rx", 0), ry: attrNum(el, "ry", 0), circle: false,
      };
    }
    if (t === "line") {
      return {
        kind: "line",
        points: [[attrNum(el, "x1", 0), attrNum(el, "y1", 0)],
                 [attrNum(el, "x2", 0), attrNum(el, "y2", 0)]],
      };
    }
    if (t === "polyline" || t === "polygon") {
      const raw = (el.getAttribute("points") || "").trim().split(/[\s,]+/).filter(Boolean).map(Number);
      const points = [];
      for (let i = 0; i + 1 < raw.length; i += 2) points.push([raw[i], raw[i + 1]]);
      return { kind: t, points };
    }
    if (t === "path") return { kind: "path", d: el.getAttribute("d") || "" };
    return null;
  }

  // ------------------------------------------------------------------ text

  function textNodesOf(el) {
    const found = [];
    const walk = (parent) => {
      for (const node of Array.from(parent.childNodes)) {
        if (node.nodeType === 3) {
          if (node.nodeValue && node.nodeValue.length) found.push(node);
        } else if (node.nodeType === 1 && tagOf(node) === "tspan") {
          walk(node);
        }
      }
    };
    walk(el);
    return found;
  }

  function runStyle(node) {
    const style = styleOf(node.parentElement);
    return {
      text: node.nodeValue,
      fill: style.fill,
      fillOpacity: style.fillOpacity,
      stroke: style.stroke,
      fontFamily: style.fontFamily,
      fontSize: style.fontSize,
      fontWeight: style.fontWeight,
      fontStyle: style.fontStyle,
      letterSpacing: style.letterSpacing,
      textDecorationLine: style.textDecorationLine,
    };
  }

  /**
   * The runs of one `<text>`, each with a box.
   *
   * Unrotated text is measured with `Range.getClientRects()` — already canvas px, and the same
   * line boxes WP1 records for HTML text. Rotated text has no usable client rect (the browser
   * returns the axis-aligned bounds of rotated glyphs), so each text node is wrapped in an inert
   * `<tspan>` for the length of the measurement and `getBBox()` reports its box in local units,
   * which Python maps through the CTM. Every wrapper is removed before this returns.
   */
  function textRuns(el, rotated, warnings, spath) {
    const nodes = textNodesOf(el);
    const runs = [];
    if (!rotated) {
      for (const node of nodes) {
        const range = document.createRange();
        range.selectNodeContents(node);
        const rects = Array.from(range.getClientRects()).filter((r) => r.width > 0 && r.height > 0);
        if (!rects.length) continue;
        if (rects.length > 1) {
          warnings.push({ source: spath, message: "text node drawn as " + rects.length + " fragments; merged" });
        }
        const x = Math.min.apply(null, rects.map((r) => r.x));
        const y = Math.min.apply(null, rects.map((r) => r.y));
        const x2 = Math.max.apply(null, rects.map((r) => r.x + r.width));
        const y2 = Math.max.apply(null, rects.map((r) => r.y + r.height));
        runs.push(Object.assign(runStyle(node), { box: [x, y, x2 - x, y2 - y] }));
      }
      return { runs, frame: "canvas" };
    }

    const wrappers = [];
    try {
      for (const node of nodes) {
        const wrapper = document.createElementNS(SVGNS, "tspan");
        wrapper.setAttribute(SYNTHETIC_ATTR, "");
        node.parentNode.replaceChild(wrapper, node);
        wrapper.appendChild(node);
        wrappers.push(wrapper);
      }
      for (let i = 0; i < nodes.length; i += 1) {
        const box = bboxOf(wrappers[i]);
        if (!box || (box[2] <= 0 && box[3] <= 0)) continue;
        runs.push(Object.assign(runStyle(nodes[i]), { box }));
      }
    } finally {
      for (const wrapper of wrappers) {
        const node = wrapper.firstChild;
        if (node) wrapper.parentNode.replaceChild(node, wrapper);
        else if (wrapper.parentNode) wrapper.parentNode.removeChild(wrapper);
      }
    }
    return { runs, frame: "local" };
  }

  // ------------------------------------------------------------------ `use` expansion

  /**
   * `<use>` renders a shadow copy the DOM will not let us walk, so build the copy the spec
   * describes — a `<g>` carrying the use element's own attributes plus `translate(x, y)`, with a
   * `<symbol>` turned into the `<svg>` it is defined to become — measure it, and delete it again.
   */
  function expandUse(el, context, spath) {
    const href = hrefOf(el);
    if (!href.startsWith("#")) {
      context.warnings.push({ source: spath, message: "use without a local href is not expanded" });
      return null;
    }
    const target = document.getElementById(href.slice(1));
    if (!target) {
      context.warnings.push({ source: spath, message: "use references missing id " + href });
      return null;
    }
    const skip = ["href", "x", "y", "width", "height", "xlink:href", NODE_ATTR];
    const holder = document.createElementNS(SVGNS, "g");
    holder.setAttribute(SYNTHETIC_ATTR, "");
    for (const attr of Array.from(el.attributes)) {
      if (skip.indexOf(attr.name) === -1) holder.setAttribute(attr.name, attr.value);
    }
    const inner = document.createElementNS(SVGNS, "g");
    inner.setAttribute(SYNTHETIC_ATTR, "");
    inner.setAttribute("transform", "translate(" + attrNum(el, "x", 0) + "," + attrNum(el, "y", 0) + ")");
    holder.appendChild(inner);

    if (tagOf(target) === "symbol") {
      // A referenced <symbol> renders as an <svg> that takes width/height from the <use>.
      const shell = document.createElementNS(SVGNS, "svg");
      for (const attr of Array.from(target.attributes)) {
        if (attr.name !== "id") shell.setAttribute(attr.name, attr.value);
      }
      shell.setAttribute("width", el.getAttribute("width") || "100%");
      shell.setAttribute("height", el.getAttribute("height") || "100%");
      for (const child of Array.from(target.childNodes)) shell.appendChild(child.cloneNode(true));
      inner.appendChild(shell);
    } else {
      const clone = target.cloneNode(true);
      clone.removeAttribute("id");
      if (tagOf(target) === "svg") {
        if (el.getAttribute("width")) clone.setAttribute("width", el.getAttribute("width"));
        if (el.getAttribute("height")) clone.setAttribute("height", el.getAttribute("height"));
      }
      inner.appendChild(clone);
    }
    el.parentNode.insertBefore(holder, el);
    context.created.push(holder);
    return holder;
  }

  // ------------------------------------------------------------------ the walk

  function baseNode(el, spath, style, context) {
    context.counter += 1;
    const key = context.svgId + "/" + context.counter;
    el.setAttribute(NODE_ATTR, key);
    return {
      key,
      tag: tagOf(el),
      id: el.id || null,
      name: el.getAttribute("data-name") || el.id || null,
      spath,
      ctm: matrixOf(el) || context.rootCtm,
      bbox: bboxOf(el),
      rect: rectOf(el),
      style,
      fillPaint: describePaint(style.fill),
      strokePaint: describePaint(style.stroke),
      clip: describeClip(el, style, context.rootCtm),
      filter: describeFilter(el, style),
      hasMask: !!(style.mask && style.mask !== "none"),
      blend: style.mixBlendMode && style.mixBlendMode !== "normal" ? style.mixBlendMode : null,
      viaUse: context.viaUse,
      children: [],
    };
  }

  function describeNode(el, spath, context) {
    const t = tagOf(el);
    if (NON_RENDERED.has(t)) return null;
    const style = styleOf(el);
    if (style.display === "none" || style.visibility === "hidden") return null;

    if (t === "use") {
      const holder = expandUse(el, context, spath);
      if (!holder) return null;
      const node = baseNode(el, spath, style, context);
      node.kind = "container";
      node.group = false;            // a <use> is not a <g>, so it adds no PowerPoint group
      const wasViaUse = context.viaUse;
      context.viaUse = true;
      node.children = walkChildren(holder, spath + " > #shadow", context);
      context.viaUse = wasViaUse;
      return node;
    }

    const node = baseNode(el, spath, style, context);

    if (CONTAINER_TAGS.has(t)) {
      node.kind = "container";
      node.group = t === "g" && !el.hasAttribute(SYNTHETIC_ATTR);
      if (t === "svg") node.viewportClip = rectOf(el);
      if (t === "switch") {
        context.warnings.push({ source: spath, message: "<switch> walked as a plain container" });
      }
      node.children = walkChildren(el, spath, context);
      return node;
    }
    if (SHAPE_TAGS.has(t)) {
      node.kind = "shape";
      node.geom = geometryOf(el);
      node.markerStart = describeMarker(style.markerStart);
      node.markerMid = describeMarker(style.markerMid);
      node.markerEnd = describeMarker(style.markerEnd);
      return node;
    }
    if (t === "text") {
      if (el.querySelector("textPath")) {
        node.kind = "raster";
        node.rasterReason = "svg textPath";
        return node;
      }
      node.kind = "text";
      const measured = textRuns(el, Math.abs(Math.atan2(node.ctm[1], node.ctm[0])) > 1e-4,
                                context.warnings, spath);
      node.runs = measured.runs;
      node.runFrame = measured.frame;
      node.textStyle = {
        textAnchor: style.textAnchor,
        dominantBaseline: style.dominantBaseline,
        textTransform: style.textTransform,
      };
      return node;
    }
    if (t === "image") {
      node.kind = "image";
      node.href = hrefOf(el);
      node.preserveAspectRatio = el.getAttribute("preserveAspectRatio") || "xMidYMid meet";
      return node;
    }
    if (t === "foreignobject") {
      node.kind = "raster";
      node.rasterReason = "svg foreignObject";
      return node;
    }
    context.warnings.push({ source: spath, message: "unsupported svg element <" + t + "> dropped" });
    return null;
  }

  function walkChildren(parent, prefix, context) {
    const nodes = [];
    const children = elementChildren(parent);   // snapshot: `use` inserts its expansion in here
    children.forEach((child, index) => {
      const spath = (prefix ? prefix + " > " : "") + tagOf(child) + ":nth-child(" + (index + 1) + ")";
      const node = describeNode(child, spath, context);
      if (node) nodes.push(node);
    });
    return nodes;
  }

  // ------------------------------------------------------------------ entry points

  function describe(selector) {
    const root = document.querySelector(selector);
    if (!root) return null;
    const context = {
      svgId: root.getAttribute("data-engine-svg") || selector,
      rootCtm: matrixOf(root) || [1, 0, 0, 1, 0, 0],
      created: [],
      warnings: [],
      counter: 0,
      viaUse: false,
    };
    const rootStyle = styleOf(root);
    let nodes = [];
    try {
      nodes = walkChildren(root, "", context);
    } finally {
      for (let i = context.created.length - 1; i >= 0; i -= 1) {
        const holder = context.created[i];
        if (holder.parentNode) holder.parentNode.removeChild(holder);
      }
    }
    return {
      svgId: context.svgId,
      cssPath: cssPath(root),
      ctm: context.rootCtm,
      viewport: rectOf(root),
      overflow: rootStyle.overflow || "hidden",
      opacity: rootStyle.opacity,
      scroll: [window.scrollX, window.scrollY],
      nodes,
      warnings: context.warnings,
    };
  }

  /** Remove every attribute the measurement and the isolated captures left behind. */
  function cleanup() {
    const marks = [NODE_ATTR, "data-engine-capture", "data-engine-capture-ancestor", SYNTHETIC_ATTR];
    for (const mark of marks) {
      Array.from(document.querySelectorAll("[" + mark + "]")).forEach((el) => el.removeAttribute(mark));
    }
  }

  window.__engineSvg = { describe, cleanup, NODE_ATTR };
})();
