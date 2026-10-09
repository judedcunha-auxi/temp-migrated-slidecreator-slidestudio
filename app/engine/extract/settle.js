/*
 * engine/extract/settle.js — bring the page to rest before anything is measured (WP-F, F-A).
 *
 * A slide is exported as a reader sees it once it has stopped moving. `prepare_page` and the gate's
 * `render_reference` inject this file and call `window.__engineSettle()` once; after that, CSS
 * animations are at their end state (a looping one at its base, un-animated state), transitions
 * are complete and SMIL is frozen at t = 0. Without it the walk reads `opacity` and rects mid-flight:
 * a staggered fade-in exported its cards at opacity 0.91 / 0.16 / 0 / 0, differently on every run.
 *
 * The order is measured, not a preference (Edge, 2026-09-25): once the zero-duration stylesheet
 * below is in the document, the finite animations report `finished` and the infinite ones are *no
 * longer returned* by `getAnimations()` (their active duration is 0 × ∞ = 0). Enumerating after the
 * stylesheet would therefore count nothing and cancel nothing, and the "looping animation" warning
 * could never fire — so the snapshot is taken first.
 */
(function () {
  "use strict";

  /** Zero every duration and delay, so anything Blink starts (or re-creates) later is instant. */
  var ZERO_CSS =
    "*, *::before, *::after { animation-duration: 0s !important; animation-delay: 0s !important;" +
    " transition-duration: 0s !important; transition-delay: 0s !important; }";

  /** The same stable path shape as page.js's `cssPath`, so a diagnostic points where page.js would. */
  function cssPath(el) {
    var parts = [];
    for (var node = el; node && node.nodeType === 1 && node !== document.documentElement;
         node = node.parentElement) {
      var piece = node.tagName.toLowerCase();
      if (node.id) { piece += "#" + node.id; parts.unshift(piece); break; }
      if (node.classList && node.classList.length) piece += "." + node.classList[0];
      var index = 1, sibling = node;
      while ((sibling = sibling.previousElementSibling)) index++;
      parts.unshift(piece + ":nth-child(" + index + ")");
    }
    return parts.join(" > ");
  }

  function isTransition(a) {
    return typeof CSSTransition !== "undefined" && a instanceof CSSTransition;
  }

  function running() {
    return document.getAnimations().filter(function (a) { return a.playState === "running"; }).length;
  }

  window.__engineSettle = function () {
    if (window.__engineSettled) {
      return { repeat: true, animations: 0, finished: 0, cancelled: [], transitions: 0, paused: 0,
               smilPaused: 0, stillRunning: running() };
    }
    // 1. Snapshot first — see the header for why this cannot come after the stylesheet.
    var snapshot = document.getAnimations();
    var finished = 0, transitions = 0, paused = 0, smilPaused = 0, cancelled = [];

    // 2. Finish what can finish; cancel what never would.
    snapshot.forEach(function (a) {
      var effect = a.effect;
      var timing = effect && effect.getTiming ? effect.getTiming() : {};
      var el = effect && effect.target;
      var name = a.animationName || a.transitionProperty || "";
      var path = el && el.nodeType === 1 ? cssPath(el) : "";
      if (a.playState === "paused") { paused++; return; }        // the reader sees the paused frame
      if (a.playState === "finished") {
        // Already at its end state: nothing to do, but it is counted, because whether a 0.6 s
        // fade finished before or after `networkidle` is a race, and the diagnostic this report
        // becomes must not change from run to run. (A `fill: none` animation that ended before the
        // snapshot is gone from `getAnimations()` and back at its base style — nothing to report.)
        if (isTransition(a)) transitions++; else finished++;
        return;
      }
      if (timing.iterations === Infinity) {
        a.cancel();                                               // back to the base style
        cancelled.push({ name: name, path: path });
        return;
      }
      try {
        a.finish();
        if (isTransition(a)) transitions++; else finished++;
      } catch (error) {
        // `finish()` throws `InvalidStateError` on an infinite effect; any other refusal is treated
        // the same way: the element is measured at its base state, and the caller says so.
        a.cancel();
        cancelled.push({ name: name, path: path });
      }
    });

    // 3. Then the stylesheet. It stays: an isolated capture later toggles `visibility` on every
    //    element, which would start a `transition: all`, and Blink may re-create a cancelled CSS
    //    animation on the next style change — both must be instant. Not `animation: none`: that
    //    would also remove a `fill: forwards` reveal and leave its element at a base `opacity: 0`.
    var style = document.createElement("style");
    style.setAttribute("data-engine-probe", "");
    style.textContent = ZERO_CSS;
    (document.head || document.documentElement).appendChild(style);

    // 4. SMIL is measured at t = 0 (the linter already warns on `<animate>`). Every root is paused;
    //    only the ones that actually carry SMIL are counted.
    var roots = document.querySelectorAll("svg");
    for (var i = 0; i < roots.length; i++) {
      if (typeof roots[i].pauseAnimations !== "function") continue;
      if (roots[i].parentElement && roots[i].parentElement.closest("svg")) continue;   // nested
      roots[i].pauseAnimations();
      roots[i].setCurrentTime(0);
      if (roots[i].querySelector("animate, animateTransform, animateMotion, set")) smilPaused++;
    }

    window.__engineSettled = true;
    return {
      animations: snapshot.length,
      finished: finished,
      cancelled: cancelled,
      transitions: transitions,
      paused: paused,
      smilPaused: smilPaused,
      stillRunning: running(),
    };
  };
})();
