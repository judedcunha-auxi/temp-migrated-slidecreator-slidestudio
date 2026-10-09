/*
 * engine/extract/color.js — one normaliser for every CSS <color> the browser can hand us.
 *
 * Injected by `engine/extract/html.py` before `page.js`, and evaluated by `svg.py: install` before
 * `svg.js`, so both browser scripts read colours the same way (`window.__engineColor`).
 *
 * Why it works (measured on Edge 153 and Chromium 153, 2026-09-25 — 14-WPE-paint §"Measured before
 * designing"): Chromium keeps `oklch()`, `oklab()`, `lab()`, `lch()` and `color(display-p3 …)` in
 * their own syntax in computed values *and* in a 2D-context `fillStyle` round-trip, so neither is
 * a normaliser. The computed value of `color-mix(in srgb, X 100%, transparent)` is different: a
 * colour-mix result is in its interpolation space (CSS Color 5 §2.1), so it is always serialised as
 * `color(srgb r g b [/ a])` — for every X the browser can resolve, `none` channels already zeroed.
 * `fast` parses that form (and the legacy forms) directly; everything else goes through the probe.
 *
 * The one approximation: a wide-gamut colour comes back with channels outside [0, 1]
 * (`color(display-p3 1 0 0)` → `color(srgb 1.09302 -0.22669 -0.150073)`) and is clamped here,
 * while the page gamut-maps it. The `colour-gamut` lint reports that statically.
 */
window.__engineColor = window.__engineColor || (function () {
  "use strict";

  var cache = {}, unparsed = {}, probe = null;
  var NUMBER = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i;

  function clamp(n, low, high) { return Math.max(low, Math.min(high, n)); }

  function hex2(n) {
    var s = clamp(Math.round(n), 0, 255).toString(16).toUpperCase();
    return s.length === 1 ? "0" + s : s;
  }

  /** One channel or alpha token: a number, a percentage of `full`, or `none` (→ 0). */
  function channel(token, full) {
    if (token === undefined) return null;
    token = String(token).trim().toLowerCase();
    if (token === "none") return 0;
    if (/%$/.test(token)) {
      var p = token.slice(0, -1);
      return NUMBER.test(p) ? (parseFloat(p) / 100) * full : null;
    }
    return NUMBER.test(token) ? parseFloat(token) : null;
  }

  /** `r g b [/ a]` or `r, g, b[, a]` → [r, g, b, a-token]; null when the shape is wrong. */
  function args(body) {
    var slash = body.split("/");
    if (slash.length > 2) return null;
    var main = slash[0].trim().split(/[\s,]+/).filter(function (p) { return p !== ""; });
    var alpha = slash.length === 2 ? slash[1].trim() : main.length === 4 ? main.pop() : undefined;
    if (main.length !== 3) return null;
    return [main[0], main[1], main[2], alpha];
  }

  /** The forms we parse without the browser: transparent/none, hex, rgb()/rgba(), color(srgb …). */
  function fast(text) {
    var t = String(text).trim().toLowerCase();
    if (t === "transparent" || t === "none") return { color: "000000", alpha: 0 };
    var m;
    if (t[0] === "#") {
      var body = t.slice(1);
      if (!/^[0-9a-f]+$/.test(body)) return null;
      if (body.length === 3 || body.length === 4) {
        body = body.split("").map(function (c) { return c + c; }).join("");
      }
      if (body.length === 6) return { color: body.toUpperCase(), alpha: 1 };
      if (body.length === 8) return { color: body.slice(0, 6).toUpperCase(), alpha: parseInt(body.slice(6), 16) / 255 };
      return null;
    }
    if ((m = t.match(/^rgba?\((.*)\)$/))) {
      var parts = args(m[1]);
      if (!parts) return null;
      var rgb = [channel(parts[0], 255), channel(parts[1], 255), channel(parts[2], 255)];
      var a = parts[3] === undefined ? 1 : channel(parts[3], 1);
      if (rgb.indexOf(null) >= 0 || a === null) return null;
      return { color: hex2(rgb[0]) + hex2(rgb[1]) + hex2(rgb[2]), alpha: clamp(a, 0, 1) };
    }
    if ((m = t.match(/^color\(\s*srgb\s+(.*)\)$/))) {
      var srgb = args(m[1]);
      if (!srgb) return null;
      var c = [channel(srgb[0], 1), channel(srgb[1], 1), channel(srgb[2], 1)];
      var alpha = srgb[3] === undefined ? 1 : channel(srgb[3], 1);
      if (c.indexOf(null) >= 0 || alpha === null) return null;
      return { color: hex2(clamp(c[0], 0, 1) * 255) + hex2(clamp(c[1], 0, 1) * 255) + hex2(clamp(c[2], 0, 1) * 255),
               alpha: clamp(alpha, 0, 1) };
    }
    return null;
  }

  /** Ask the browser: the computed value of a 100 % srgb colour-mix is always `color(srgb …)`. */
  function viaProbe(text) {
    if (!probe || !probe.isConnected) {
      // One span, outside <body>, never measured by anything: absolutely placed off-screen and
      // hidden, so being in the document cannot move a single rect the walk reads.
      probe = document.createElement("span");
      probe.setAttribute("data-engine-probe", "");
      probe.style.cssText = "position:absolute;left:-10000px;top:-10000px;visibility:hidden";
      document.documentElement.appendChild(probe);
    }
    probe.style.color = "";
    probe.style.color = "color-mix(in srgb, " + text + " 100%, transparent)";
    // An invalid colour leaves the declaration unset, which is detectable before any style read.
    if (!probe.style.color) return null;
    return fast(getComputedStyle(probe).color);
  }

  /**
   * Any CSS <color> → `{color: "RRGGBB", alpha: 0..1}`, or null when the browser cannot read it.
   * `el` resolves `currentcolor` (Chromium resolves it inside functions at computed-value time for
   * every property but `color`; this is for the strings that still carry it). `quiet` asks "is this
   * a colour?" without recording a failure — a gradient's direction or a colour hint is not one.
   */
  function normalise(text, el, quiet) {
    text = String(text === undefined || text === null ? "" : text).trim();
    if (!text) return null;
    if (/\bcurrentcolor\b/i.test(text)) {
      var current = el && el.nodeType === 1 ? getComputedStyle(el).color : "rgb(0, 0, 0)";
      text = text.replace(/\bcurrentcolor\b/ig, current);
    }
    if (!Object.prototype.hasOwnProperty.call(cache, text)) cache[text] = fast(text) || viaProbe(text);
    var out = cache[text];
    if (!out) {
      if (!quiet) unparsed[text] = true;
      return null;
    }
    return { color: out.color, alpha: out.alpha };
  }

  /** Split on whitespace at parenthesis depth 0, outside quotes: `oklch(0.2 0.1 240) 0px 4px`. */
  function tokens(text) {
    var out = [], depth = 0, quote = null, current = "";
    text = String(text || "");
    for (var i = 0; i < text.length; i++) {
      var c = text[i];
      if (quote) {
        current += c;
        if (c === "\\" && i + 1 < text.length) { current += text[++i]; continue; }
        if (c === quote) quote = null;
        continue;
      }
      if (c === '"' || c === "'") { quote = c; current += c; continue; }
      if (c === "(") depth++;
      else if (c === ")") depth = Math.max(0, depth - 1);
      if (/\s/.test(c) && depth === 0) {
        if (current) out.push(current);
        current = "";
        continue;
      }
      current += c;
    }
    if (current) out.push(current);
    return out;
  }

  return {
    normalise: normalise,
    tokens: tokens,
    fast: fast,
    /** Every string that could not be read since this page loaded, sorted: one warning each. */
    unparsed: function () { return Object.keys(unparsed).sort(); },
  };
})();
