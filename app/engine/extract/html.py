"""HTML → IR, measured in a real browser.

The browser half of the walk is `page.js`; this module owns the session, the filesystem and the IR.
The split is deliberate: only the layout engine knows where a line broke or what colour a cascade
resolved to, and only Python may decide where a derived PNG is written or whether an asset exists.

Four things in this file are contracts, not conveniences:

* **A page's fonts are forced before anything is measured — and then checked.** The browser must lay
  text out in the font PowerPoint will use, or every line box is a lie. Installed-font detection
  deliberately avoids `document.fonts.check()`, which answers `true` for families the browser has
  never heard of (master brief §10.2): the check is a fontTools scan of `config.FONT_DIRS`. A file on
  disk is not a font the browser can draw, though (a per-user install nobody registered with the
  session is invisible to Edge), so `ensure_theme_fonts` measures what the page actually resolved,
  loads the scanned file into the page when it did not, and reports an error when even that fails.
* **Asset URLs are routed, not fetched.** Slides reference `/api/projects/<id>/assets/<file>`, which
  404s under `file://`; `page.route` serves them from `assets_dir` so measurement sees real images
  at their real intrinsic size.
* **A raster is captured in isolation.** Everything outside the target subtree is hidden for the
  duration of the screenshot, so the picture holds that element and nothing else — a plain crop of
  the composited page bakes in the siblings and the emitter then paints them twice.
* **Derived images never touch the project's `assets/`.** That folder is an input; isolated rasters
  and rasterised `.svg` files go to `app.engine.extract.derived_dir(workspace, slide_id)`, under
  the workspace the caller names.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import logging
import mimetypes
import re
import socket
import urllib.parse
import urllib.request
import weakref
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from playwright.sync_api import Frame, Page, Route

from app.config import engine as config
from app.core.browser_pool import thread_browser
from app.engine.extract import derived_dir
from app.engine.extract.svg import expand_svg
from app.engine.ir import IR, Box, Canvas, Element, Fonts, Group, Slide, SvgPlaceholder, assign_ids_and_z
from app.engine.manifest import Manifest

#: Slide HTML asks for its images through the app's URL shape; both extraction and the reference
#: render serve them from the filesystem instead.
ASSET_URL_GLOB = "**/api/projects/*/assets/*"

#: The same shape as a path pattern, tested on *any* scheme (WP-F): under `page.goto(file://…)` a
#: slide's `/api/projects/<id>/assets/x.png` resolves to `file:///C:/api/projects/…`, and a slide
#: opened from the app's server to `http://localhost:8787/api/…` — both are project assets.
ASSET_PATH_RE = re.compile(r"/api/projects/[^/]+/assets/([^/?#]+)")

#: Schemes the measuring browser may load without asking the network policy: the document itself,
#: inline data and nothing that leaves the machine. `file:` only with an empty host or `localhost`
#: — `//cdn/x.png` under `file://` becomes `file://cdn/x.png`, a file-share request.
_LOCAL_SCHEMES = ("data", "blob", "about")

#: The browser-side walk. Injected per page load; `window.__engineExtract` is its entry point.
PAGE_JS = Path(__file__).with_name("page.js")

#: The colour normaliser both browser scripts read colours through (`window.__engineColor`).
#: Injected before `page.js`; `svg.py` evaluates it too, for pages where `page.js` never ran.
COLOR_JS = Path(__file__).with_name("color.js")

#: Rebuild `::before`/`::after` as real elements before the walk (`window.__engineMaterialisePseudo`,
#: WP-E). Each rebuild is verified not to move or repaint anything and reverted with a warning when it
#: would. Tests switch it off to prove the layout is the same either way; `render_reference` never runs it.
MATERIALISE_PSEUDO: bool = True

#: Brings the page to rest before it is measured or photographed: CSS animations finished (a
#: looping one cancelled to its base state), transitions completed, SMIL frozen at t = 0. Injected
#: by `prepare_page` and by `render_reference`'s frame loop, so export and reference agree (WP-F).
SETTLE_JS = Path(__file__).with_name("settle.js")

#: Injected for the duration of an element screenshot: hide everything, then re-show the target
#: subtree and strip the ancestors' own paint so only the target lands in the PNG. The last rule
#: undoes the rotations the walk marked, because an element's `box` and `rotation` describe the
#: *unrotated* picture and a rotated capture would be turned a second time by the emitter.
ISOLATION_CSS = """
body * { visibility: hidden !important; }
[data-engine-capture], [data-engine-capture] * { visibility: visible !important; }
[data-engine-capture-ancestor] { background: transparent !important; border-color: transparent !important;
                                 box-shadow: none !important; }
[data-engine-unrotate] { transform: none !important; }
[data-engine-capture-self-only] * { visibility: hidden !important; }
[data-engine-capture-self-only] { background-color: transparent !important; border-color: transparent !important;
                                  box-shadow: none !important; color: transparent !important; text-shadow: none !important; }
"""
"""The two `self-only` rules come last on purpose (WP-F): `[data-engine-capture] *` and
`[data-engine-capture-self-only] *` tie on specificity, so the later rule wins. A self-only capture
is the element's own background *image* and nothing else — its children, its background colour,
border and shadow (drawn natively), and its own text nodes (not elements, so `color`) all go."""

#: Speaker notes are not part of the picture. The walk already skips `aside.notes` and reads it into
#: `slide.notes`, and the emitter puts it on the notes page — but a browser paints it like any other
#: block unless the author remembered `hidden`, which the authoring contract mentions only in
#: passing. Left unhidden it lands on top of the slide in the reference *and* scores as a large gate
#: defect against an export that correctly left it out. Hiding it here makes the reference agree with
#: the rest of the engine whatever the author wrote.
NOTES_CSS = "\naside.notes { display: none !important; }\n"

#: OpenType features PowerPoint cannot ask for. DrawingML has no run property for figure styles
#: (`lnum`/`onum`/`tnum`/`pnum`, fractions, slashed zero) or other requested features, so PowerPoint
#: always draws the font's default forms — Georgia's default digits are old-style. Measuring and
#: drawing the reference without them keeps the line breaks and the picture equal to the file;
#: `lint` says so where a slide asks for them (`type-features`). The app's preview carries the same
#: rule (`server/app.py`, a test holds the two equal), so what the author sees is what exports.
TYPE_FEATURES_CSS = (
    "\n*, *::before, *::after { font-variant-numeric: normal !important;"
    " font-feature-settings: normal !important; }\n"
)

#: Nothing narrower or shorter than this is emitted (WP1 brief, precision rules) — border lines,
#: whose height *is* the border width, are the stated exception and are let through by `_keep`.
MIN_EXTENT_PX = 0.5

_log = logging.getLogger(__name__)


# --------------------------------------------------------------------------------- browser session


@contextmanager
def measuring_page(canvas: Canvas, *, page: Page | None = None) -> Iterator[Page]:
    """A page sized to the canvas at device scale 1, on the calling thread's browser.

    The browser comes from `app.core.browser_pool.thread_browser()`: one per thread, launched on
    first use and reused for every slide (the launch is the expensive part). A caller-supplied page is resized and handed back untouched (the pipeline reuses one page for
    every slide); a page we create here is closed on the way out.
    """
    if page is not None:
        page.set_viewport_size({"width": canvas.w, "height": canvas.h})
        yield page
        return
    context = thread_browser().new_context(
        viewport={"width": canvas.w, "height": canvas.h},
        device_scale_factor=1,
        # A transparent slide layer composites over the layout; a white default would hide that.
        color_scheme="light",
    )
    own_page = context.new_page()
    try:
        yield own_page
    finally:
        own_page.close()
        context.close()


@dataclass(slots=True)
class NetworkLog:
    """What the network policy refused on one page: `{url, host, resourceType, reason}` per request.

    The list is live — requests the page makes after `route_assets` returns (the walk's own
    `new Image()` probes) are appended as they happen.
    """

    blocked: list[dict[str, Any]] = field(default_factory=list)


#: The one `**/*` handler the engine keeps on a page, so a re-route replaces only its own.
_ROUTERS: weakref.WeakKeyDictionary[Page, Callable[[Route], None]] = weakref.WeakKeyDictionary()


def route_assets(page: Page, assets_dir: Path, *, missing: list[str] | None = None) -> NetworkLog:
    """Serve project assets from `assets_dir` and apply the remote-resource policy (WP-F).

    One handler on `**/*`, in this order — the asset shape first, because under `file://` an asset
    arrives as a `file:` URL:

    1. `/api/projects/<id>/assets/<file>` on any scheme → served from `assets_dir`, or a 404 recorded
       in `missing`;
    2. `data:`, `blob:`, `about:` and `file:` with an empty host or `localhost` → loaded as usual;
    3. everything else — `http(s)`, `ws(s)`, `ftp`, a `file:` share — under `config.REMOTE_RESOURCES`
       = `block` (the default everywhere, Peter's decision #3): refused, logged "blocked by policy";
       under `raster` (an opt-in for a trusted machine): an `http(s)` URL on a public host loads,
       anything else is refused and logged ("host is not public").

    `route.fallback()`, not `continue_()`, so a handler registered *before* this one (a test faking a
    host) still runs. Both `prepare_page` and `render_reference` call this, so the export and the
    gate's reference see the same page by construction.
    """
    assets_dir = Path(assets_dir)
    # A page reused across slides would otherwise stack one handler per slide; only ours is removed.
    previous = _ROUTERS.pop(page, None)
    if previous is not None:
        page.unroute("**/*", previous)
    log = NetworkLog()
    policy = (config.REMOTE_RESOURCES or "block").strip().lower()

    def handler(route: Route) -> None:
        request = route.request
        parsed = urllib.parse.urlparse(request.url)
        asset = ASSET_PATH_RE.search(parsed.path)
        if asset:
            name = urllib.parse.unquote(asset.group(1))
            candidate = assets_dir / name
            if candidate.is_file():
                content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
                route.fulfill(status=200, body=candidate.read_bytes(), content_type=content_type)
            else:
                if missing is not None:
                    missing.append(name)
                route.fulfill(status=404, body=b"", content_type="text/plain")
            return
        scheme, host = parsed.scheme.lower(), (parsed.hostname or "").lower()
        if scheme in _LOCAL_SCHEMES or (scheme == "file" and host in ("", "localhost")):
            route.fallback()
            return
        if policy == "raster" and scheme in ("http", "https") and _host_is_public(host):
            route.fallback()
            return
        reason = "host is not public" if policy == "raster" and scheme in ("http", "https") else "blocked by policy"
        log.blocked.append({"url": request.url, "host": host, "resourceType": request.resource_type,
                            "reason": reason})
        route.abort("blockedbyclient")

    page.route("**/*", handler)
    _ROUTERS[page] = handler
    return log


#: `_host_is_public`'s answers for this process; `forget_host_checks()` drops them.
_HOST_CHECKS: dict[str, bool] = {}

#: Names that are never a public host, whatever a resolver says.
_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")


def _global(address: str) -> bool:
    """Is one resolved address globally routable? A v4-mapped v6 address is judged as its v4 — CPython
    only fixed `is_global` for those in 3.12.4."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global)


def _host_is_public(host: str) -> bool:
    """May the measuring browser fetch from this host under `REMOTE_RESOURCES=raster`?

    Only when every address it resolves to is global: never loopback, RFC 1918, link-local (the
    cloud metadata address 169.254.169.254 among them), CGNAT 100.64/10, unspecified, ULA or
    `fe80::/10`. A decimal or octal literal (`http://2130706433/`) is not an IP literal to
    `ipaddress`, so it goes through the resolver, which answers 127.0.0.1 — refused. A name that does
    not resolve is refused. DNS rebinding remains possible between this check and the browser's own
    lookup — which is why `raster` is an opt-in for trusted machines, never the default.
    """
    host = (host or "").strip().lower().strip("[]").split("%", 1)[0].rstrip(".")
    if not host:
        return False
    if host in _HOST_CHECKS:
        return _HOST_CHECKS[host]
    if host == "localhost" or host.endswith(_PRIVATE_SUFFIXES):
        answer = False
    else:
        try:
            ipaddress.ip_address(host)
            literal = True
        except ValueError:
            literal = False
        if literal:
            answer = _global(host)
        else:
            try:
                infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            except (socket.gaierror, OSError, UnicodeError):
                infos = []
            addresses = {info[4][0] for info in infos}
            answer = bool(addresses) and all(_global(str(address)) for address in addresses)
    _HOST_CHECKS[host] = answer
    return answer


def forget_host_checks() -> None:
    """Drop `_host_is_public`'s cache — for tests that monkeypatch the resolver."""
    _HOST_CHECKS.clear()


def _theme_font_names(fonts: dict[str, str]) -> tuple[str, str]:
    """`(major, minor)` with the fallbacks every forcing site shares: one missing slot takes the other."""
    major = fonts.get("major") or fonts.get("minor") or "Arial"
    minor = fonts.get("minor") or major
    return major, minor


def font_variables_css(fonts: dict[str, str]) -> str:
    """Only the `--engine-font-major/minor` variables — declared before the font pre-pass, so a slide
    that writes `font-family: var(--engine-font-major)` (the contract allows it) is read as major."""
    major, minor = _theme_font_names(fonts)
    return f"""
:root {{ --engine-font-major: "{major}"; --engine-font-minor: "{minor}"; }}
"""


def font_forcing_css(fonts: dict[str, str]) -> str:
    """CSS that pins the page to the master's theme fonts.

    `major` on headings and the title placeholder, and on every element the font pre-pass
    (`FONT_PREPASS_JS`) marked `data-engine-font="major"` because the slide set it in the theme's
    major font; `minor` everywhere else. `!important` so a slide's own `font-family` cannot win.
    The marker rule has no descendant selector: a child that declares another family was not
    marked and stays minor, while a child that inherits the major font was marked itself. The same
    two attributes carry a `::before`/`::after` drawn in the major font. Exposed as CSS variables
    because the authoring contract lets slides reference `--engine-font-major/minor` directly.
    """
    return font_variables_css(fonts) + """
*, *::before, *::after { font-family: var(--engine-font-minor), sans-serif !important; }
h1, h2, h3, h4, h5, h6, [data-placeholder="title"], [data-placeholder="title"] *,
h1 *, h2 *, h3 *, h4 *, h5 *, h6 * { font-family: var(--engine-font-major), sans-serif !important; }
[data-engine-font="major"], [data-engine-font-before="major"]::before,
[data-engine-font-after="major"]::after { font-family: var(--engine-font-major), sans-serif !important; }
"""


#: Run once per slide after load and **before** `font_forcing_css`, with `[major, titleZone]`:
#:
#: * Marks every element whose computed `font-family` names the theme's major font first
#:   (unquoted, case-insensitive) `data-engine-font="major"`, and a `::before`/`::after` with content
#:   in it `data-engine-font-before/after="major"` — the forcing CSS then gives exactly those the
#:   major font. Only the first family counts: `Lexend, Georgia` asks for Lexend, which is not a theme
#:   font, and stays minor (lint's `font-not-theme` says so).
#: * Pins a `[data-placeholder="title"]` that the slide left in normal flow at the canvas origin —
#:   static, not floated, untransformed, its margin box at (0, 0), with no positioned or transformed
#:   ancestor between it and `<body>` (the body is the canvas, positioned or not) — `position:
#:   absolute` at the layout title zone's x/y/width (`titleZone`, canvas px;
#:   null when the layout has none), margins zeroed and `box-sizing: border-box`, so its border box
#:   starts at the zone and is as wide. The browser would otherwise lay the title out at x = 0 across
#:   the whole canvas, and PowerPoint would clip it there. Only the first such title is pinned.
#:
#: Returns `{marked, pinned}`, `pinned` being `{x, y, w}` of the zone when a title was moved.
FONT_PREPASS_JS = """([major, zone]) => {
    const first = value => String(value || '').split(',')[0].trim().replace(/^["']+|["']+$/g, '').trim().toLowerCase();
    const want = first(major);
    let marked = 0;
    if (want) {
        for (const el of document.querySelectorAll('body, body *')) {
            if (first(getComputedStyle(el).fontFamily) === want) {
                el.setAttribute('data-engine-font', 'major');
                marked += 1;
            }
            for (const which of ['before', 'after']) {
                const pseudo = getComputedStyle(el, '::' + which);
                if (pseudo.content && pseudo.content !== 'none' && pseudo.content !== 'normal' &&
                    first(pseudo.fontFamily) === want) el.setAttribute('data-engine-font-' + which, 'major');
            }
        }
    }
    let pinned = null;
    const moved = cs => cs.transform !== 'none' || (cs.translate && cs.translate !== 'none') ||
                        (cs.rotate && cs.rotate !== 'none') || (cs.scale && cs.scale !== 'none');
    if (zone) {
        for (const el of document.querySelectorAll('[data-placeholder="title"]')) {
            const cs = getComputedStyle(el);
            if (cs.position !== 'static' || cs.float !== 'none' || cs.display === 'none' || moved(cs)) continue;
            let free = true;
            for (let a = el.parentElement; a && a !== document.body && a !== document.documentElement;
                 a = a.parentElement) {
                const style = getComputedStyle(a);
                if (style.position !== 'static' || moved(style)) { free = false; break; }
            }
            if (!free) continue;
            const r = el.getBoundingClientRect();
            const left = r.left - (parseFloat(cs.marginLeft) || 0), top = r.top - (parseFloat(cs.marginTop) || 0);
            if (Math.abs(left) > 1 || Math.abs(top) > 1) continue;
            const set = (name, value) => el.style.setProperty(name, value, 'important');
            set('position', 'absolute');
            set('left', zone.x + 'px');
            set('top', zone.y + 'px');
            set('right', 'auto');
            set('bottom', 'auto');
            set('width', zone.w + 'px');
            set('margin', '0');
            set('box-sizing', 'border-box');
            el.setAttribute('data-engine-pinned', 'title');
            pinned = { x: zone.x, y: zone.y, w: zone.w };
            break;
        }
    }
    return { marked: marked, pinned: pinned };
}"""


def title_zone_of(manifest: Manifest, layout_id: str | None) -> Box | None:
    """The layout's title placeholder zone (`title` or `ctrTitle`), or None — where a static title is pinned."""
    if not layout_id:
        return None
    try:
        layout = manifest.layout(layout_id)
    except Exception:  # noqa: BLE001 — an unknown layout pins nothing; the classifier reports it
        return None
    zone = layout.placeholder("title")
    if zone is None or zone.w <= 0:
        return None
    return zone.box


def force_theme_fonts(target: Page | Frame, fonts: dict[str, str], zone: Box | None = None) -> dict[str, Any]:
    """Hide the notes, run the font pre-pass (which also pins a static title to `zone`), then force.

    The one sequence `prepare_page` and `render_reference` share, so the export is measured and the
    reference drawn with the same fonts on the same elements and the title in the same place: the
    variables and `NOTES_CSS` first (an unhidden notes block in flow would push the title off the
    origin), `FONT_PREPASS_JS` while the slide's own fonts still apply, `font_forcing_css` last.
    """
    major, minor = _theme_font_names(fonts)
    # One font in both slots: every element is in it already, and marking would change nothing.
    distinct = major if major.strip().strip("'\"").lower() != minor.strip().strip("'\"").lower() else ""
    target.add_style_tag(content=font_variables_css(fonts) + NOTES_CSS + TYPE_FEATURES_CSS)
    zone_js = None if zone is None else {"x": round(zone.x, 3), "y": round(zone.y, 3), "w": round(zone.w, 3)}
    result = target.evaluate(FONT_PREPASS_JS, [distinct, zone_js]) or {}
    target.add_style_tag(content=font_forcing_css(fonts))
    return result


#: The `config.FONT_DIRS` scan, kept for the process. Reading every name table costs ~0.6 s, which
#: is most of a slide's measurement budget and buys the same answer every time — fonts are not
#: installed mid-export. `installed_families.cache_clear()` drops it if a test needs a re-scan.
_installed_scan: frozenset[str] | None = None


def _scan_installed() -> frozenset[str]:
    """Every family name the OS font files declare, lowercased."""
    from fontTools.ttLib import TTCollection, TTFont  # imported lazily: only measurement needs it

    present: set[str] = set()
    for path in config.font_files():
        try:
            fonts = (
                TTCollection(path).fonts
                if path.suffix.lower() in (".ttc", ".otc")
                else [TTFont(path, fontNumber=0, lazy=True)]
            )
            for font in fonts:
                for record in font["name"].names:
                    if record.nameID in (1, 16):  # family, typographic family
                        present.add(str(record.toUnicode()).strip().lower())
                font.close()
        except Exception:  # noqa: BLE001  # nosec B112
            # ^ a broken or exotic font file is not a run-stopper
            continue
    return frozenset(present)


def installed_families(wanted: Sequence[str]) -> dict[str, bool]:
    """Which of these font families are installed, by reading the OS font files.

    fontTools reads each font's name table, so "Arial" is only reported present when a face actually
    calls itself Arial. `document.fonts.check()` cannot be used for this: it returns `true` for any
    family name, installed or not, because it is asking "can text be rendered", not "is this font here".
    """
    global _installed_scan
    if _installed_scan is None:
        _installed_scan = _scan_installed()
    return {family: family.strip().lower() in _installed_scan for family in wanted}


def forget_installed_families() -> None:
    """Drop the cached font scan — for a caller that has just changed `config.FONT_DIRS`."""
    global _installed_scan
    _installed_scan = None
    _font_face_css.cache_clear()


# ------------------------------------------------------------------------ did the browser see it


#: Whether `ensure_theme_fonts` may load a theme face the browser cannot see from the file the
#: `config.FONT_DIRS` scan found. Tests switch it off to reach the "still unresolved" error.
LOAD_UNSEEN_FONTS: bool = True

#: The string both the browser and fontTools/Pillow measure. Mixed case, digits and wide capitals, so
#: two different faces are very unlikely to agree on its width by accident.
FONT_PROBE_TEXT = "Hamburgefonstiv 0123456789 WMQ"
FONT_PROBE_PX = 100

#: How far the browser's probe width may sit from the width of the face the scan found before it is
#: "not the same face". Kerning and hinting put the two within 0.6 % on Arial, Calibri, Segoe UI and
#: Lexend (2026-09-27); Lexend drawn as its Arial fallback is 6 % off, Crimson Pro as Times 10 %.
FONT_PROBE_TOLERANCE = 0.02

#: Canvas `measureText` on the probe, per `(family, weight)`: in the family with a monospace and with
#: a serif fallback, and in each fallback alone. A family the page cannot resolve measures exactly
#: like the fallback it names, both times — that is the whole test, and it holds for a local family
#: nobody told this browser session about, which `document.fonts.check()` would call present.
#: `document.fonts.load` first, so a face injected by `@font-face` has arrived before canvas asks.
_PROBE_FONTS_JS = """async ([pairs, text, px]) => {
    const quote = f => '"' + f.replace(/["\\\\]/g, '\\\\$&') + '"';
    for (const [family, weight] of pairs) {
        try { await document.fonts.load(`${weight} ${px}px ${quote(family)}`, text); } catch (e) {}
    }
    const ctx = document.createElement('canvas').getContext('2d');
    const width = font => { ctx.font = font; return ctx.measureText(text).width; };
    return pairs.map(([family, weight]) => ({
        family, weight,
        mono: width(`${weight} ${px}px monospace`),
        serif: width(`${weight} ${px}px serif`),
        asMono: width(`${weight} ${px}px ${quote(family)}, monospace`),
        asSerif: width(`${weight} ${px}px ${quote(family)}, serif`),
    }));
}"""


@dataclass(frozen=True, slots=True)
class FontCheck:
    """What the page drew for one theme family at one weight, against what the scan says it should."""

    family: str
    weight: int
    resolved: bool             # the page drew *some* face of this family, not its fallback
    width: float               # probe width in the browser, px
    expected: float | None     # probe width of the scanned face (fontTools/Pillow), px; None: no face
    face: str | None           # the scanned file the expectation came from

    @property
    def matches(self) -> bool:
        """The browser drew the face the emitter measures with (or there is nothing to compare to)."""
        if self.expected is None or self.expected <= 0:
            return True
        return abs(self.width - self.expected) / self.expected <= FONT_PROBE_TOLERANCE

    @property
    def ok(self) -> bool:
        return self.resolved and self.matches

    def describe(self) -> str:
        drawn = f"{self.width:.0f} px" if self.resolved else f"{self.width:.0f} px (its fallback)"
        wanted = f", {Path(cast(str, self.face)).name} is {self.expected:.0f} px" if self.expected else ""
        return f"{self.family} {self.weight}: probe {drawn}{wanted}"


def _judge(raw: dict[str, Any], expected: float | None, face: str | None) -> FontCheck:
    """One `_PROBE_FONTS_JS` record → a `FontCheck`. Pure, so the rule is testable without a browser."""
    def same(a: Any, b: Any) -> bool:
        return abs(float(a) - float(b)) < 0.01

    fallback = same(raw["asMono"], raw["mono"]) and same(raw["asSerif"], raw["serif"])
    return FontCheck(str(raw["family"]), int(raw["weight"]), not fallback, float(raw["asSerif"]),
                     expected, face)


def _probe_weights(family: str) -> list[int]:
    """400 always; 700 too when the family has a static bold face (bold titles and `<b>` use it)."""
    from app.engine.emit.text import face_for_exact

    weights = [400]
    bold = face_for_exact(family, 700)
    if bold is not None and not bold.variable and bold.weight == 700:
        weights.append(700)
    return weights


def _expectation(family: str, weight: int) -> tuple[float | None, str | None]:
    """The probe width of the face the scan says the browser draws — None for no face or a variable one."""
    from app.engine.emit.text import face_for_exact, face_width_px

    face = face_for_exact(family, weight)
    if face is None or face.variable:
        return None, (str(face.path) if face is not None else None)
    try:
        return face_width_px(face, FONT_PROBE_TEXT, FONT_PROBE_PX), str(face.path)
    except Exception:  # noqa: BLE001 — a face Pillow cannot open leaves nothing to compare against
        return None, str(face.path)


def _css_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


@lru_cache(maxsize=32)
def _font_face_css(family: str) -> tuple[str, tuple[str, ...]]:
    """`@font-face` rules that load every scanned face of `family` from its file, and those files.

    A `data:` URL rather than `file://`: the measuring page and the reference composite are both
    `file://` documents, whose origin is opaque, and Chromium fetches fonts in CORS mode — a
    `file://` font from a `file://` page is refused. Collection members (`.ttc`) are skipped: a URL
    cannot pick a face out of a collection, and the brand faces are all single `.ttf` files.
    """
    from app.engine.emit.text import font_index

    rules: list[str] = []
    paths: list[str] = []
    for face in font_index().get(family.strip().strip("'\"").strip().lower(), []):
        suffix = face.path.suffix.lower()
        if suffix not in (".ttf", ".otf"):
            continue
        try:
            payload = base64.b64encode(face.path.read_bytes()).decode("ascii")
        except OSError:
            continue
        mime, fmt = ("font/otf", "opentype") if suffix == ".otf" else ("font/ttf", "truetype")
        weight = "1 1000" if face.variable else str(face.weight)
        style = "italic" if face.italic else "normal"
        rules.append(
            f"@font-face {{ font-family: {_css_string(family)}; "
            f"src: url(data:{mime};base64,{payload}) format(\"{fmt}\"); "
            f"font-weight: {weight}; font-style: {style}; }}"
        )
        paths.append(str(face.path))
    return "\n".join(rules), tuple(paths)


def _probe(target: Page | Frame, families: Sequence[str]) -> list[FontCheck]:
    pairs = [[family, weight] for family in families for weight in _probe_weights(family)]
    raw = target.evaluate(_PROBE_FONTS_JS, [pairs, FONT_PROBE_TEXT, FONT_PROBE_PX]) or []
    return [_judge(record, *_expectation(record["family"], int(record["weight"]))) for record in raw]


def ensure_theme_fonts(target: Page | Frame, fonts: dict[str, str]) -> dict[str, Any]:
    """Prove the page draws the theme fonts it was forced into; load them when it does not.

    Run after `font_forcing_css` is on the page. For each theme family (at 400, and 700 when a static
    bold exists) canvas measures a probe string: in the family's own name against two different
    fallbacks, which tells "resolved" from "fell back", and against the fontTools/Pillow width of the
    face the `config.FONT_DIRS` scan found, which tells "the face the emitter measures with" from
    "some other face by that name".

    A family the scan found but the page did not draw (2026-09-27: Lexend copied to the per-user
    font folder and registered in HKCU, but never loaded into the Windows session, so Edge measured
    every slide in Arial while PowerPoint set Lexend ~8 % wider) is loaded from the scanned files
    with `@font-face` and probed again. Returns:

    * `checks` — the final `FontCheck` per (family, weight);
    * `loaded` — `{family: [file, …]}` for each family that had to be loaded from its files;
    * `unresolved` — families the page still does not draw although the scan found them: every
      width measured on this page is the fallback's, which `extract_html` reports as an error.

    A family the scan did not find is not this function's business: `extract_html` already warns
    that it is not installed, and a browser that draws it anyway (a font the scan cannot read, the
    blind-fonts test route) measures it as it will be drawn.
    """
    families: list[str] = []
    for slot in ("major", "minor"):
        family = str(fonts.get(slot) or "").strip()
        if family and family not in families:
            families.append(family)
    if not families:
        return {"checks": [], "loaded": {}, "unresolved": []}

    checks = _probe(target, families)
    suspect = sorted({c.family for c in checks if not c.ok and c.face is not None})
    loaded: dict[str, list[str]] = {}
    if suspect and LOAD_UNSEEN_FONTS:
        css_parts: list[str] = []
        for family in suspect:
            css, paths = _font_face_css(family)
            if css:
                css_parts.append(css)
                loaded[family] = list(paths)
        if css_parts:
            target.add_style_tag(content="\n".join(css_parts))
            again = {(c.family, c.weight): c for c in _probe(target, list(loaded))}
            checks = [again.get((c.family, c.weight), c) for c in checks]
    unresolved = sorted({c.family for c in checks if not c.resolved and c.face is not None})
    for family in unresolved:
        _log.error("theme font %r is installed (%s) but the measuring browser cannot draw it; "
                   "every width on this page is its fallback's", family,
                   next((c.face for c in checks if c.family == family), "?"))
    return {"checks": checks, "loaded": loaded, "unresolved": unresolved}


def font_check_diagnostics(result: dict[str, Any]) -> list[tuple[str, str, str]]:
    """`(level, source, message)` for what `ensure_theme_fonts` found — nothing when all was well."""
    out: list[tuple[str, str, str]] = []
    checks: list[FontCheck] = list(result.get("checks") or [])
    unresolved = set(result.get("unresolved") or [])
    for family, paths in sorted((result.get("loaded") or {}).items()):
        if family in unresolved:
            continue
        names = ", ".join(Path(p).name for p in paths)
        out.append(("warn", "fonts",
                    f"theme font {family!r} is installed but this browser session could not draw it; "
                    f"measured with {names} loaded into the page. Register the font with the "
                    f"system's font configuration (fontconfig in the container) so the browser "
                    f"resolves it by name"))
    for family in sorted(unresolved):
        detail = "; ".join(c.describe() for c in checks if c.family == family)
        out.append(("error", "fonts",
                    f"theme font {family!r} is installed but the measuring browser drew its fallback, "
                    f"so every line box on this slide is measured in the wrong font ({detail})"))
    for check in checks:
        if check.family in unresolved or check.matches:
            continue
        out.append(("warn", "fonts",
                    f"theme font {check.family!r} {check.weight}: the browser draws a different face "
                    f"than the installed file the export measures with ({check.describe()})"))
    return out


def prepare_page(
    page: Page,
    html_path: Path,
    manifest: Manifest,
    assets_dir: Path,
    *,
    missing_assets: list[str] | None = None,
    layout_id: str | None = None,
) -> dict[str, Any]:
    """Load the slide, route its assets, force the theme fonts and wait until text is measurable.

    `force_theme_fonts` hides the speaker notes as `render_reference` does, marks the elements set
    in the major font and pins a static title to `layout_id`'s title zone (no layout, no pin). Then
    the page is settled (`settle.js`): measured at rest, as a reader sees it once it has stopped
    moving. Returns `{"settle": <what __engineSettle did>, "blocked": <the network log's live
    list>, "fonts": <what ensure_theme_fonts found>, "prepass": <what the pre-pass did>}` —
    `extract_html` turns them into diagnostics; other callers may ignore them. With remote hosts
    blocked, `networkidle` no longer waits on the internet.
    """
    log = route_assets(page, assets_dir, missing=missing_assets)
    page.goto(Path(html_path).resolve().as_uri(), wait_until="networkidle")
    prepass = force_theme_fonts(page, manifest.fonts, title_zone_of(manifest, layout_id))
    page.evaluate("document.fonts.ready")
    fonts = ensure_theme_fonts(page, manifest.fonts)
    page.add_script_tag(path=str(SETTLE_JS))
    settle = page.evaluate("() => window.__engineSettle()")
    return {"settle": settle, "blocked": log.blocked, "fonts": fonts, "prepass": prepass}


def capture_isolated(
    page: Page, selector: str, out_png: Path, box: Box | None = None, *, self_only: bool = False
) -> Path:
    """Screenshot one element with every other element hidden and the ancestors' paint stripped.

    `box` (canvas px) frames the picture instead of the element's own rect, as in svg.py's twin —
    a remote image is captured at the box it was *painted* in (WP-F). `self_only` keeps only the
    element's own background image: its children, colour, border, shadow and text are hidden.
    """
    out_png.parent.mkdir(parents=True, exist_ok=True)
    handle = page.query_selector(selector)
    if handle is None:
        raise ValueError(f"nothing matches {selector!r} on this page")
    page.evaluate(
        """([sel, selfOnly]) => {
            document.querySelectorAll('[data-engine-capture], [data-engine-capture-ancestor], ' +
                                      '[data-engine-capture-self-only]')
                .forEach(el => { el.removeAttribute('data-engine-capture');
                                 el.removeAttribute('data-engine-capture-ancestor');
                                 el.removeAttribute('data-engine-capture-self-only'); });
            const target = document.querySelector(sel);
            if (!target) return;
            target.setAttribute('data-engine-capture', '');
            if (selfOnly) target.setAttribute('data-engine-capture-self-only', '');
            for (let el = target.parentElement; el; el = el.parentElement)
                el.setAttribute('data-engine-capture-ancestor', '');
        }""",
        [selector, self_only],
    )
    if box is None:
        handle.screenshot(path=str(out_png), style=ISOLATION_CSS, omit_background=True)
    else:
        page.screenshot(path=str(out_png), style=ISOLATION_CSS, omit_background=True,
                        clip={"x": box.x, "y": box.y, "width": box.w, "height": box.h})
    return out_png


# ----------------------------------------------------------------------------------- extraction


def _box(data: dict[str, Any]) -> Box:
    return Box(float(data["x"]), float(data["y"]), float(data["w"]), float(data["h"]))


def _keep(record: dict[str, Any]) -> bool:
    """Drop sub-half-pixel geometry, except the thin shapes that *are* a border side.

    A side is a rect, a stroked line (dashed, dotted) or a join polygon (`custom`, where it meets a
    painted neighbour), and a hairline side of any of the three is still a side.
    """
    b = record.get("box") or {}
    width, height = float(b.get("w", 0)), float(b.get("h", 0))
    if width >= MIN_EXTENT_PX and height >= MIN_EXTENT_PX:
        return True
    geometry = record.get("geometry") or {}
    is_border = record.get("kind") == "shape" and geometry.get("type") in ("rect", "line", "custom")
    return bool(is_border and max(width, height) >= MIN_EXTENT_PX)


def _asset_path(url: str | None, assets_dir: Path, derived: Path) -> tuple[Path | None, str | None]:
    """Resolve a measured image URL to a file on disk. Returns `(path, problem)`."""
    if not url:
        return None, "image has no src"
    parsed = urllib.parse.urlparse(url)
    name = Path(urllib.parse.unquote(parsed.path)).name
    if "/assets/" in parsed.path and "/api/projects/" in parsed.path:
        candidate = Path(assets_dir) / name
        return (candidate, None) if candidate.is_file() else (None, f"asset {name!r} is not in {assets_dir}")
    if parsed.scheme == "file":
        candidate = Path(urllib.request.url2pathname(parsed.path))
        return (candidate, None) if candidate.is_file() else (None, f"{candidate} does not exist")
    if parsed.scheme == "data":
        header, _, payload = url.partition(",")
        if ";base64" not in header:
            return None, "only base64 data: URIs are supported"
        raw = base64.b64decode(payload)
        suffix = ".png" if "image/png" in header else ".jpg" if "jpeg" in header else ".bin"
        target = derived / f"data-{hashlib.sha256(raw).hexdigest()[:12]}{suffix}"
        target.write_bytes(raw)
        return target, None
    # Defensive: the walk never hands a remote image here (it is refused, or rasterised under
    # `REMOTE_RESOURCES=raster` with `record["remote"]`), and nothing is ever fetched from Python.
    return None, "remote resources are blocked by policy"


def _settle_diagnostics(
    settle: dict[str, Any], walk_diagnostics: Sequence[dict[str, Any]]
) -> list[tuple[None, str, str, str]]:
    """What `__engineSettle` did, said out loud: nothing that moved may be exported silently.

    The "still running" error is the walk's tripwire (`page.js`) when it has fired, so one problem
    is reported once; this only adds it when the tripwire did not see what settle saw.
    """
    found: list[tuple[None, str, str, str]] = []
    settled, transitions = int(settle.get("finished") or 0), int(settle.get("transitions") or 0)
    if settled + transitions:
        found.append((None, "info", "settle",
                      f"settled {settled} CSS animation(s) and {transitions} transition(s) before measuring; "
                      f"the export shows their end state"))
    for entry in settle.get("cancelled") or []:
        name, path = entry.get("name") or "(unnamed)", entry.get("path") or "settle"
        found.append((None, "warn", path,
                      f"looping animation ‘{name}’ on {path} cannot finish; "
                      f"exported at its base (un-animated) state"))
    if settle.get("paused"):
        found.append((None, "info", "settle",
                      f"{settle['paused']} paused animation(s) measured at their paused frame"))
    running = int(settle.get("stillRunning") or 0)
    if running and not any("still running" in (d.get("message") or "") for d in walk_diagnostics):
        found.append((None, "error", "settle", f"measurement ran with {running} animation(s) still running"))
    return found


#: Blocked requests the walk reports itself (it knows which element asked); every other type —
#: stylesheet, font, script, xhr, fetch, media, websocket, other — is reported from the log.
_WALK_RESOURCE_TYPES = frozenset({"image", "document"})


def _blocked_diagnostics(blocked: Sequence[dict[str, Any]]) -> list[tuple[None, str, str, str]]:
    """One warn per blocked URL the walk cannot see: the slide was measured without it."""
    found: list[tuple[None, str, str, str]] = []
    seen: set[str] = set()
    for entry in blocked:
        url, kind = entry.get("url") or "", entry.get("resourceType") or "other"
        if kind in _WALK_RESOURCE_TYPES or url in seen:
            continue
        seen.add(url)
        tail = " — the theme fonts are forced anyway" if kind == "font" else ""
        found.append((None, "warn", "network",
                      f"blocked remote resource {url} ({kind}); the slide was measured without it{tail}"))
    return found


def _paragraphs_ok(paragraphs: list[dict[str, Any]] | None) -> bool:
    """A text element with no measurable line is not a text element."""
    return bool(paragraphs) and all(p.get("lines") for p in paragraphs or [])


def _capture_id(selector: str) -> str:
    """The `nN` the walk stamped on a capture target — the stem of its derived PNG."""
    match = re.search(r'data-engine-id="([^"]+)"', selector)
    return match.group(1) if match else re.sub(r"[^A-Za-z0-9_-]", "-", selector)


def _box_key(box: Box) -> str:
    """A capture's box in a file name: one element can own several framed captures (its tiles)."""
    return "x".join(f"{round(v * 1000):d}" for v in (box.x, box.y, box.w, box.h)).replace("-", "m")


def _element_from(
    record: dict[str, Any],
    index: int,
    *,
    assets_dir: Path,
    derived: Path,
    page: Page,
    problems: list[tuple[int | None, str, str, str]],
) -> Element | SvgPlaceholder | None:
    """One browser record → one IR element (or the marker WP2's expansion replaces)."""
    kind = record["kind"]
    common: dict[str, Any] = {
        "box": _box(record["box"]),
        "rotation": float(record.get("rotation") or 0.0),
        "opacity": float(record.get("opacity", 1.0)),
        "clip": _box(record["clip"]) if record.get("clip") else None,
        "name": record.get("name"),
        "group": record.get("group"),
        "source": record.get("source") or {},
        "extras": dict(record.get("extras") or {}),
    }

    if kind == "svgPlaceholder":
        return SvgPlaceholder(
            svg_id=record["svgId"], box=common["box"], source=common["source"],
            group=common["group"], clip=common["clip"],
        )

    if kind == "shape":
        return Element(kind="shape", geometry=record["geometry"], fill=record.get("fill"),
                       stroke=record.get("stroke"), shadow=record.get("shadow"),
                       flipH=bool(record.get("flipH")), flipV=bool(record.get("flipV")), **common)

    if kind == "text":
        if not _paragraphs_ok(record.get("paragraphs")):
            return None
        return Element(kind="text", paragraphs=record["paragraphs"], anchor=record.get("anchor", "top"),
                       writingMode=record.get("writingMode", "horizontal"),
                       wrap=bool(record.get("wrap", True)), transformCase=record.get("transformCase"),
                       **common)

    if kind == "table":
        return Element(kind="table", rows=record["rows"], cols=record["cols"],
                       colWidthsPx=record["colWidthsPx"], rowHeightsPx=record["rowHeightsPx"],
                       cells=record["cells"], **common)

    if kind == "chart":
        return Element(kind="chart", spec=record["spec"], style=record.get("style"),
                       origin=record.get("origin", "authored"), confidence=record.get("confidence", 1.0),
                       plotRect=_box(record["plotRect"]) if record.get("plotRect") else None,
                       overlay=record.get("overlay") or [], **common)

    if kind == "image":
        if record.get("remote"):
            # `REMOTE_RESOURCES=raster` (WP-F): the browser's own rendering of what it loaded, at the
            # box it was painted in — radius and crop are already in the pixels. Nothing is fetched
            # from Python; the request went through the measuring browser's network policy.
            url = str(record["remote"])
            target = derived / f"remote-{hashlib.sha256(url.encode('utf-8')).hexdigest()[:12]}.png"
            capture_isolated(page, record["selector"], target, box=common["box"],
                             self_only=bool(record.get("background")))
            common["extras"]["x-wpf-remote"] = url
            problems.append((index, "warn", (common["source"] or {}).get("path", "image"),
                             f"rasterised: remote image {url} at its displayed size; remote content may "
                             f"change between exports"))
            return Element(kind="image", src=str(target.resolve()), fit="fill", crop=None, radius=0.0,
                           circle=False, **common)
        path, problem = _asset_path(record.get("url"), assets_dir, derived)
        if problem or path is None:
            problems.append((None, "error", (common["source"] or {}).get("path", "image"),
                             problem or "image could not be resolved"))
            return None
        # r3: the browser's own pixels, framed by the record's box — a background tiled more than
        # a picture per tile should carry (`page.js` says so in its `rasterised:` warn), and an SVG,
        # which python-pptx cannot embed. The box is the part the frame shows (or the frame, under
        # rounded corners), so the crop, the letterbox and the corners are in the pixels: the element
        # is a plain `fill` picture. A rotated box is not a page rectangle, so a rotated SVG is still
        # captured whole, as before.
        pixels = bool(record.get("capture")) or path.suffix.lower() == ".svg"
        if pixels:
            framed = not common["rotation"]
            if framed:
                # Only the page can be photographed: a box past the canvas edge keeps its on-canvas part.
                viewport = page.viewport_size or {"width": 1 << 16, "height": 1 << 16}
                common["box"] = common["box"].intersect(Box(0.0, 0.0, float(viewport["width"]),
                                                             float(viewport["height"])))
                if common["box"].w <= 0 or common["box"].h <= 0:
                    problems.append((None, "info", (common["source"] or {}).get("path", "image"),
                                     "image lies outside the canvas; nothing of it shows"))
                    return None
            stem = "background" if record.get("capture") else f"svg-image-{path.stem}"
            target = derived / f"{stem}-{_capture_id(record['selector'])}-{_box_key(common['box'])}.png"
            capture_isolated(page, record["selector"], target, box=common["box"] if framed else None,
                             self_only=bool(record.get("background")))
            if path.suffix.lower() == ".svg":
                problems.append((index, "warn", (common["source"] or {}).get("path", "image"),
                                 f"{path.name} rasterised at its displayed size (PowerPoint cannot embed SVG)"))
            if framed:
                return Element(kind="image", src=str(target.resolve()), fit="fill", crop=None, radius=0.0,
                               circle=False, **common)
            path = target
        # A mirrored <img> (`scaleX(-1)`): the IR serialises flips for shapes only, so the picture's
        # travel in `extras` (r3); the crop is in the unmirrored source, as `a:srcRect` is.
        flips = {key: True for key in ("flipH", "flipV") if record.get(key)}
        if flips:
            common["extras"]["x-wpf-flip"] = flips
        return Element(kind="image", src=str(path.resolve()), fit=record.get("fit") or "fill",
                       crop=record.get("crop"), radius=float(record.get("radius") or 0.0),
                       circle=bool(record.get("circle")), **common)

    if kind == "raster":
        target = derived / f"raster-{_capture_id(record['selector'])}.png"
        capture_isolated(page, record["selector"], target)
        return Element(kind="raster", src=str(target.resolve()), reason=record["reason"], **common)

    problems.append((None, "error", "extract/html.py", f"unknown record kind {kind!r}"))
    return None


def extract_html(
    html_path: Path,
    manifest: Manifest,
    layout_id: str,
    assets_dir: Path,
    *,
    workspace: Path,
    slide_id: str | None = None,
    title: str | None = None,
    page: Page | None = None,
) -> IR:
    """Measure one slide into an IR: a paint-ordered flat element list with every value resolved.

    `workspace` is the export's own scratch folder (one per job): derived images (isolated rasters,
    rasterised `.svg` files) are written under `workspace/derived/<slide id>/`, and the IR points at
    them, so the emitter that reads them must run before the workspace is cleaned up.

    The browser walks the DOM once (`page.js`) and hands back plain records; this function turns
    them into elements, resolves image sources against `assets_dir`, captures the rasters that have
    no native equivalent, and splices each `<svg>` root's expansion in at the placeholder's paint
    position before numbering the whole list.
    """
    html_path = Path(html_path)
    canvas = Canvas(manifest.canvas_w, manifest.canvas_h, int(manifest.canvas.get("pxPerIn", config.PX_PER_IN)))
    identifier = slide_id or html_path.stem
    missing_assets: list[str] = []
    derived = derived_dir(Path(workspace), identifier, clean=True)

    elements: list[Element | SvgPlaceholder] = []
    groups: list[Group] = []
    diagnostics: list[tuple[Element | None, str, str, str]] = []
    used_fonts: list[str] = []
    notes: str | None = None
    svg_ids: list[str] = []
    expansions: dict[str, Any] = {}

    with measuring_page(canvas, page=page) as active:
        prepared = prepare_page(active, html_path, manifest, assets_dir, missing_assets=missing_assets,
                                layout_id=layout_id)
        active.add_script_tag(path=str(COLOR_JS))
        active.add_script_tag(path=str(PAGE_JS))
        pseudo: dict[str, Any] = {"materialised": [], "dropped": []}
        if MATERIALISE_PSEUDO:
            pseudo = active.evaluate("() => window.__engineMaterialisePseudo()") or pseudo
        background_sizes = active.evaluate("() => window.__engineBackgroundSizes()")
        # Read after the background probes: they issue requests of their own (WP-F). The walk is
        # the single place that reports a blocked image or document; Python reports the rest.
        blocked = list(prepared.get("blocked") or [])
        result = active.evaluate(
            "(options) => window.__engineExtract(options)",
            {"canvasW": canvas.w, "canvasH": canvas.h, "backgroundSizes": background_sizes,
             "remotePolicy": (config.REMOTE_RESOURCES or "block").strip().lower(),
             "blockedUrls": {entry["url"]: entry["reason"] for entry in blocked}},
        )
        read_before_svg = active.evaluate("() => window.__engineColor.unparsed()") or []
        # A rebuilt pseudo-element the walk could not draw was taken back and reported by the walk
        # itself (page.js `reportUndrawnPseudos`): it counts as dropped, not rebuilt.
        undrawn = set(active.evaluate("() => window.__enginePseudoUndrawn || []") or [])
        notes = result.get("notes")
        used_fonts = sorted(set(result.get("fontsUsed") or []))
        svg_ids = list(result.get("svgIds") or [])
        groups = [
            Group(id=g["id"], parent=g.get("parent"), name=g.get("name"), box=_box(g["box"]))
            for g in result.get("groups") or []
        ]

        raw_problems: list[tuple[int | None, str, str, str]] = []
        by_record: dict[int, Element] = {}
        for index, record in enumerate(result.get("records") or []):
            if not _keep(record):
                raw_problems.append((None, "info", (record.get("source") or {}).get("path", "?"),
                                     f"dropped a {record['kind']} smaller than {MIN_EXTENT_PX} px"))
                continue
            built = _element_from(record, index, assets_dir=assets_dir, derived=derived, page=active,
                                  problems=raw_problems)
            if built is None:
                continue
            if isinstance(built, Element):
                by_record[index] = built
            elements.append(built)

        rebuilt = len([path for path in pseudo.get("materialised") or [] if path not in undrawn])
        dropped = len(pseudo.get("dropped") or []) + len(undrawn)
        if rebuilt or dropped:
            raw_problems.append((None, "info", "pseudo-elements",
                                 f"{rebuilt} pseudo-elements rebuilt as shapes and text, {dropped} dropped"))
        for entry in (result.get("diagnostics") or []) + [
            {"level": level, "source": source, "message": message, "recordIndex": index}
            for index, level, source, message in raw_problems
        ]:
            index = entry.get("recordIndex")
            diagnostics.append((by_record.get(index) if index is not None else None,
                                entry["level"], entry["source"], entry["message"]))
        diagnostics.extend(_settle_diagnostics(prepared.get("settle") or {}, result.get("diagnostics") or []))
        diagnostics.extend(_blocked_diagnostics(blocked))
        pinned = (prepared.get("prepass") or {}).get("pinned")
        if pinned:
            diagnostics.append((None, "info", '[data-placeholder="title"]',
                                f"the title is not positioned; placed at {layout_id}'s title zone "
                                f"(x {pinned['x']:g}, y {pinned['y']:g}, width {pinned['w']:g} px)"))

        # Only roots that survived the walk as a placeholder are expanded: expanding one with
        # nowhere to splice its elements would drop them without a word.
        placed = [item.svg_id for item in elements if isinstance(item, SvgPlaceholder)]
        for orphan in [svg_id for svg_id in svg_ids if svg_id not in placed]:
            diagnostics.append((None, "warn", orphan, f"{orphan} was not painted; nothing to expand"))
        if placed:
            expansions = expand_svg(active, placed, canvas, Path(assets_dir), derived=derived)
            # svg.js reads its paint through the same normaliser; what it could not read is new.
            reported = set(read_before_svg)
            for text in active.evaluate("() => window.__engineColor.unparsed()") or []:
                if text not in reported:
                    diagnostics.append((None, "warn", "colours",
                                        f"colour {text} could not be read; the paint that used it is dropped"))

    # Splice each expansion in at its placeholder's paint position, then number the whole list once.
    spliced: list[Element] = []
    for item in elements:
        if isinstance(item, Element):
            spliced.append(item)
            continue
        expansion = expansions.get(item.svg_id)
        if expansion is None:
            diagnostics.append((None, "error", item.svg_id, f"no expansion came back for {item.svg_id}"))
            continue
        for element in expansion.elements:
            if item.group and not element.group:
                element.group = item.group
            if item.clip is not None:
                element.clip = item.clip if element.clip is None else element.clip.intersect(item.clip)
            spliced.append(element)
        groups.extend(expansion.groups)
        for diagnostic in expansion.diagnostics:
            diagnostics.append((None, diagnostic.level, diagnostic.source, diagnostic.message))

    for element in spliced:
        if element.clip is not None:
            element.clip = element.clip.intersect(canvas.box)

    ordered = assign_ids_and_z(spliced)
    forced = manifest.fonts
    wanted = [f for f in {forced.get("major"), forced.get("minor")} if f]
    installed = installed_families(wanted)
    font_check = prepared.get("fonts") or {}
    # Installed, but the browser still drew the fallback: substituted as surely as a missing font.
    unseen = [f for f in font_check.get("unresolved") or [] if installed.get(f)]

    ir = IR(
        canvas=canvas,
        slide=Slide(id=identifier, title=title, layoutId=layout_id, notes=notes or None),
        elements=ordered,
        groups=groups,
        fonts=Fonts(
            forced=dict(forced),
            used=used_fonts,
            substituted=[{"wanted": f, "got": "(browser fallback)"}
                         for f, ok in installed.items() if not ok or f in unseen],
        ),
    )
    for owner, level, source, message in diagnostics:
        ir.add_diagnostic(level, source, message, owner.id if owner is not None else None)
    for element in ordered:
        if element.kind == "raster" and not any(d.elementId == element.id for d in ir.diagnostics):
            ir.add_diagnostic("warn", (element.source or {}).get("path", "?"),
                              f"rasterised: {element.reason}", element.id)
    for name in sorted(set(missing_assets)):
        ir.add_diagnostic("error", f"asset:{name}", f"asset {name!r} was requested but is not in {assets_dir}")
    for family, ok in sorted(installed.items()):
        if not ok:
            ir.add_diagnostic(
                "warn", "fonts", f"theme font {family!r} is not installed here; the browser substituted"
            )
    for level, source, message in font_check_diagnostics(font_check):
        ir.add_diagnostic(level, source, message)
    return ir


# ---------------------------------------------------------------------------- reference render


#: Run in the slide's frame after settling: hide what the export never has, *after* load so layout
#: does not move. A broken `<img>` exports nothing (an `image did not load` error), but Chromium
#: paints a broken-image icon and the alt text in its box, which the gate would score as a defect.
#: Under `REMOTE_RESOURCES=block` a remote `<iframe>/<object>/<embed>` is not exported either, and
#: Chromium paints its error page there — the same `isRemoteUrl` rule as page.js, in two lines.
_HIDE_UNEXPORTED_JS = """(policy) => {
    const hide = el => el.style.setProperty('visibility', 'hidden', 'important');
    for (const img of document.images) if (!img.complete || !img.naturalWidth) hide(img);
    if (policy === 'raster') return;
    for (const el of document.querySelectorAll('iframe, object, embed')) {
        const raw = el.getAttribute('src') || el.getAttribute('data') || '', url = el.src || el.data || raw;
        if (/^\\s*\\/\\//.test(raw) || /^(https?|wss?|ftp):/i.test(url) ||
            (/^file:\\/\\/[^\\/]/i.test(url) && !/^file:\\/\\/localhost\\//i.test(url))) hide(el);
    }
}"""

_REFERENCE_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
  :root {{ --engine-font-major: "{major}"; --engine-font-minor: "{minor}"; }}
  html, body {{ margin: 0; padding: 0; width: {w}px; height: {h}px; overflow: hidden; background: #FFFFFF; }}
  .layer {{ position: absolute; left: 0; top: 0; width: {w}px; height: {h}px; border: 0; }}
</style></head>
<body>
  <img class="layer" src="{layout}" alt="">
  <iframe class="layer" src="{slide}" scrolling="no"></iframe>
</body></html>
"""


def render_reference(
    html_path: Path,
    layout_png: Path,
    out_png: Path,
    *,
    assets_dir: Path,
    fonts: dict[str, str],
    title_zone: Box | None = None,
    scripts: Sequence[Path] = (),
    page: Page | None = None,
) -> Path:
    """Composite the layout background and the slide HTML into the gate's reference image.

    This is what the candidate `.pptx` is scored against, so it has to be the same picture the app
    shows: layout PNG underneath, the slide's transparent HTML on top, at canvas size, DPR 1.

    `fonts` is the master's theme fonts (`manifest.fonts`, the dict `prepare_page` forces for
    extraction), and it is **required**: the frame is forced into exactly the fonts the extractor
    measured with. It used to be read back from the composite page, which never declared them, so
    every reference was rendered in Arial whatever the master said (fidelity finding F6) — on a
    Calibri master the gate compared the export with a picture in the wrong font. The composite
    also declares `--engine-font-major/minor`, so the page describes itself. The frame goes through
    `force_theme_fonts`, as extraction does: the same elements get the major font, and a title left
    in normal flow is pinned to `title_zone` (`title_zone_of(manifest, layoutId)`; None pins nothing,
    so pass it wherever extraction had a layout, or the two pictures disagree on where the title is).

    `scripts` are injected **into the slide's frame** after load — that is how the chart preview
    (`config.CHART_PREVIEW_JS`) draws its approximation, since stored slide HTML carries no
    JavaScript. A script that exposes `ChartPreview.renderAll` is called once after injection,
    because `DOMContentLoaded` has already been and gone by then.
    """
    from PIL import Image  # lazily: only the reference render needs Pillow

    html_path, layout_png, out_png = Path(html_path), Path(layout_png), Path(out_png)
    if not layout_png.exists():
        raise FileNotFoundError(f"layout background not found: {layout_png}")
    with Image.open(layout_png) as image:
        width, height = image.size

    major, minor = _theme_font_names(fonts)
    document = _REFERENCE_TEMPLATE.format(
        w=width, h=height, layout=layout_png.resolve().as_uri(), slide=html_path.resolve().as_uri(),
        major=major, minor=minor,
    )
    composite = out_png.parent / f".{out_png.stem}.composite.html"
    out_png.parent.mkdir(parents=True, exist_ok=True)
    composite.write_text(document, encoding="utf-8")

    canvas = Canvas(width, height)
    try:
        with measuring_page(canvas, page=page) as active:
            route_assets(active, assets_dir)
            active.goto(composite.resolve().as_uri(), wait_until="networkidle")
            for frame in active.frames:
                # The slide's own frame only: an `<iframe>` *inside* the slide is content, not a
                # slide, and forcing fonts or settling it is not the reference's business.
                if frame is active.main_frame or frame.parent_frame is not active.main_frame:
                    continue
                force_theme_fonts(frame, fonts, title_zone)
                # The same check (and the same rescue) as `prepare_page`, so a font the session
                # cannot see is drawn here exactly as it was measured.
                frame.evaluate("document.fonts.ready")
                ensure_theme_fonts(frame, fonts)
                for script in scripts:
                    script_path = Path(script)
                    if not script_path.exists():
                        continue
                    frame.add_script_tag(path=str(script_path))
                frame.evaluate(
                    "() => { if (window.ChartPreview && window.ChartPreview.renderAll) "
                    "window.ChartPreview.renderAll(document); }"
                )
                # The reference is photographed at rest, exactly as `prepare_page` measures (WP-F):
                # settled, then with what the export never has hidden after load, so nothing moves.
                frame.evaluate("document.fonts.ready")
                frame.add_script_tag(path=str(SETTLE_JS))
                frame.evaluate("() => window.__engineSettle()")
                frame.evaluate(_HIDE_UNEXPORTED_JS, (config.REMOTE_RESOURCES or "block").strip().lower())
            active.evaluate("document.fonts.ready")
            # Two animation frames, so an injected preview script and the settled styles have
            # painted before capture (bounded: a hidden page never fires rAF).
            active.evaluate(
                "() => Promise.race([new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))),"
                " new Promise(r => setTimeout(r, 1000))])"
            )
            active.screenshot(path=str(out_png))
    finally:
        composite.unlink(missing_ok=True)
    return out_png


def extract_many(
    slides: Sequence[dict[str, Any]],
    manifest: Manifest,
    assets_dir: Path,
    *,
    workspace: Path,
) -> list[IR]:
    """Measure several slides on one page — the browser launch is the expensive part, not the walk.

    Each entry is `{"html": Path, "layoutId": str, "slideId": str, "title": str}`.
    """
    canvas = Canvas(manifest.canvas_w, manifest.canvas_h)
    results: list[IR] = []
    with measuring_page(canvas) as page:
        for slide in slides:
            results.append(
                extract_html(
                    Path(slide["html"]),
                    manifest,
                    slide["layoutId"],
                    assets_dir,
                    workspace=workspace,
                    slide_id=slide.get("slideId"),
                    title=slide.get("title"),
                    page=page,
                )
            )
    return results
