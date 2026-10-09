"""Design notes derived from a master's manifest, for a master that came without written ones.

The deterministic importer (`engine/importer.py`) never writes `designNotes` — it has no model to
write prose with — so the design chat used to fall back to "No master deck was provided… a white
background you draw yourself", contradicting the layouts listed a few lines above it. `derive_notes`
says what the manifest itself knows, and nothing it does not: canvas, theme fonts, the colours'
likely roles, the type scale the placeholders are set in, the margins and title zone of the content
layout, and where the footer band starts.

Colour roles are inferred, not read: a theme's slot names are not reliable. Some themes swap
dk1/lt1 (dk1 is white, lt1 is charcoal) and their bg1/tx1 follow, so "tx1" there is white. The text
colour is therefore taken from what the title placeholder is actually set in, and the background is
whichever of dk1/lt1 contrasts with it.
"""
from __future__ import annotations

import colorsys
import re
from typing import Any

FOOTER_TYPES = ("dt", "ftr", "sldNum")
TITLE_TYPES = ("title",)
COVER_TITLE_TYPES = ("ctrTitle",)
BODY_TYPES = ("body", "obj")


# ------------------------------------------------------------------------------------ colours
def _hex(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", value.strip())
    return f"#{m.group(1).upper()}" if m else None


def _rgb(hex_: str) -> tuple[float, float, float]:
    return tuple(int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))  # type: ignore[return-value]


def luminance(hex_: str) -> float:
    """WCAG relative luminance, 0 (black) … 1 (white)."""
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in _rgb(hex_))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _hls(hex_: str) -> tuple[float, float, float]:
    return colorsys.rgb_to_hls(*_rgb(hex_))


def _saturated(hex_: str) -> bool:
    _, light, sat = _hls(hex_)
    return sat >= 0.2 and 0.12 <= light <= 0.9


def _hue_gap(a: str, b: str) -> float:
    gap = abs(_hls(a)[0] - _hls(b)[0]) * 360
    return min(gap, 360 - gap)


def _close(a: str, b: str) -> bool:
    return sum(abs(x - y) for x, y in zip(_rgb(a), _rgb(b), strict=False)) < 0.12


# --------------------------------------------------------------------------------- manifest bits
def _layouts(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [lo for lo in manifest.get("layouts") or [] if isinstance(lo, dict)]


def _placeholders(layout: dict[str, Any], types: tuple[str, ...]) -> list[dict[str, Any]]:
    return [z for z in layout.get("placeholders") or [] if isinstance(z, dict) and z.get("type") in types]


def content_layout(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """The layout body slides sit on: a top title + body/content zone first, then a top title alone, then
    any layout with a title (a cover's title sits low on the slide, so it is the last resort)."""
    layouts = _layouts(manifest)
    h = float((manifest.get("canvas") or {}).get("h") or 720)

    def top_title(lo: dict[str, Any]) -> bool:          # a body slide's title sits at the top, a cover's does not
        return any(float(z.get("y") or 0) < 0.25 * h for z in _placeholders(lo, TITLE_TYPES))

    for test in (lambda lo: top_title(lo) and _placeholders(lo, BODY_TYPES),
                 lambda lo: top_title(lo),
                 lambda lo: _placeholders(lo, TITLE_TYPES)):
        found = next((lo for lo in layouts if test(lo)), None)
        if found:
            return found
    return None


def _style(zone: dict[str, Any] | None) -> dict[str, Any]:
    return (zone or {}).get("style") or {}


def _level0(manifest: dict[str, Any], kind: str) -> dict[str, Any]:
    levels = ((manifest.get("textStyles") or {}).get(kind) or {}).get("levels") or []
    return levels[0] if levels and isinstance(levels[0], dict) else {}


def _pt(value: Any) -> str | None:
    return f"{round(float(value), 1):g}pt" if isinstance(value, (int, float)) and value > 0 else None


def _font(value: Any, major: str, minor: str) -> str | None:
    if value == "+mj-lt":
        return major or "the heading font"
    if value == "+mn-lt":
        return minor or "the body font"
    return value if isinstance(value, str) and value else None


def _type_line(label: str, style: dict[str, Any], major: str, minor: str) -> str | None:
    size = _pt(style.get("sizePt"))
    if not size:
        return None
    bits = [size]
    font = _font(style.get("font"), major, minor)
    if font:
        bits.append(font)
    if style.get("bold"):
        bits.append("bold")
    color = _hex(style.get("color"))
    if color:
        bits.append(color)
    return f"{label} {' '.join(bits)}"


# ------------------------------------------------------------------------------------ the notes
def colour_roles(manifest: dict[str, Any]) -> dict[str, str | None]:
    """Likely `text`, `background`, `primary`, `accent` colours, plus `inverted` ("yes"/None)."""
    colors = {k: _hex(v) for k, v in ((manifest.get("theme") or {}).get("colors") or {}).items()}
    colors = {k: v for k, v in colors.items() if v}
    lay = content_layout(manifest)
    title = next(iter(_placeholders(lay, TITLE_TYPES)), None) if lay else None
    dk1, lt1 = colors.get("dk1"), colors.get("lt1")
    text = (_hex(_style(title).get("color")) or _hex(_level0(manifest, "title").get("color"))
            or _hex(_level0(manifest, "body").get("color")))
    pair = [c for c in (dk1, lt1) if c]
    if not text and pair:
        text = min(pair, key=luminance)
    text = text or "#000000"
    dark_text = luminance(text) < 0.4
    if pair:
        background = max(pair, key=luminance) if dark_text else min(pair, key=luminance)
        if _close(background, text):
            background = "#FFFFFF" if dark_text else "#000000"
    else:
        background = "#FFFFFF" if dark_text else "#000000"
    inverted = "yes" if dk1 and lt1 and luminance(dk1) > luminance(lt1) else None

    def usable(c: str | None) -> bool:
        return c is not None and _saturated(c) and not _close(c, text) and not _close(c, background)

    order = [f"accent{i}" for i in range(1, 7)]
    primary = next((colors[k] for k in order if usable(colors.get(k))), None)
    accent = None
    for key in ("dk2", "tx2", *order):
        c = colors.get(key)
        if c is not None and usable(c) and c != primary and (primary is None or _hue_gap(c, primary) >= 45):
            accent = c
            break
    return {"text": text, "background": background, "primary": primary, "accent": accent, "inverted": inverted}


def _slot_of(colors: dict[str, Any], hex_: str | None) -> str:
    if not hex_:
        return ""
    slots = [k for k, v in colors.items() if _hex(v) == hex_ and k in
             ("dk1", "lt1", "dk2", "lt2", *(f"accent{i}" for i in range(1, 7)))]
    return f" ({'/'.join(slots)})" if slots else ""


# ------------------------------------------------------------------------------------ design tokens
#: Gutters the 12-column grid may use, in order of preference (16-24px at 1280px, per the quality bar).
GUTTERS = (20, 24, 16, 18, 22)
#: Muted RAG defaults, used when the theme has no accent of that hue.
RAG_DEFAULTS = {"r": "#D9534F", "a": "#F0AD4E", "g": "#5CB85C"}
#: Office's default theme roles, for a master whose theme carries no colours at all.
OFFICE_PRIMARY, OFFICE_ACCENT = "#4472C4", "#ED7D31"


def _px(pt: float) -> float:
    return round(pt * 4 / 3, 2)


def _mix(hex_: str, other: str, share: float) -> str:
    """`share` of `other` mixed into `hex_` (sRGB), as #RRGGBB."""
    a, b = _rgb(hex_), _rgb(other)
    return "#" + "".join(f"{round((x * (1 - share) + y * share) * 255):02X}" for x, y in zip(a, b, strict=False))


def _on(fill: str, ink: str) -> str:
    """Readable text on `fill`: the deck's ink on a light fill, white on a dark one."""
    return ink if luminance(fill) > 0.4 else "#FFFFFF"


def _grid(left: int, right: int) -> tuple[int, int, int]:
    """(gutter, column width, right edge) for 12 columns from `left` whose edges land on integer px.
    The right edge may come in by up to 11px so that the arithmetic closes exactly."""
    width = right - left
    for trim in range(12):
        for g in GUTTERS:
            col = (width - trim - 11 * g) / 12
            if col == int(col) and col > 0:
                return g, int(col), right - trim
    g = GUTTERS[0]
    return g, max(1, (width - 11 * g) // 12), right


def _rag(colors: dict[str, Any]) -> dict[str, str]:
    """RAG from the theme's own saturated reds/ambers/greens where it has them (e.g. accent5/6), else muted
    defaults."""
    found: dict[str, str] = {}
    for key in (f"accent{i}" for i in range(1, 7)):
        c = _hex(colors.get(key))
        if not c or not _saturated(c):
            continue
        hue = _hls(c)[0] * 360
        slot = "r" if hue < 12 or hue >= 340 else "a" if 28 <= hue <= 50 else "g" if 90 <= hue <= 160 else None
        if slot and slot not in found:
            found[slot] = c
    return {k: found.get(k, v) for k, v in RAG_DEFAULTS.items()}


def design_tokens(manifest: dict[str, Any]) -> dict[str, str]:
    """The deck's design tokens as CSS custom properties (name without `--` -> value), in a stable order.

    Everything is derived from the manifest by fixed rules, so the same master always gives the same block:
    grid from the content layout's title zone (the quality bar's margins), type scale from its placeholders
    (px = pt × 4/3), colour roles from `colour_roles`. Empty when the manifest has no layouts."""
    if not isinstance(manifest, dict) or not _layouts(manifest):
        return {}
    canvas = manifest.get("canvas") or {}
    w, h = int(canvas.get("w") or 1280), int(canvas.get("h") or 720)
    theme = manifest.get("theme") or {}
    fonts = theme.get("fonts") or {}
    major = fonts.get("major") or fonts.get("heading") or "Arial"
    minor = fonts.get("minor") or fonts.get("body") or major
    colors = theme.get("colors") or {}
    lay = content_layout(manifest)
    title = next(iter(_placeholders(lay, TITLE_TYPES)), None) if lay else None
    body = next(iter(_placeholders(lay, BODY_TYPES)), None) if lay else None
    footer = next((z for lo in ([lay] if lay else []) + _layouts(manifest)
                   for z in _placeholders(lo, FOOTER_TYPES)), None)

    # Grid. A master authored for 4:3 but set on a wider canvas gets the full width with the same side margin.
    tx, ty, tw, th = ((float(title.get(k) or 0) for k in ("x", "y", "w", "h")) if title
                      else (round(w * 0.05), round(h * 0.04), w - 2 * round(w * 0.05), round(h * 0.1)))
    left, right = round(tx), round(tx + tw)
    edges = [float(z.get("x") or 0) + float(z.get("w") or 0) for lo in _layouts(manifest)
             for z in lo.get("placeholders") or [] if isinstance(z, dict)]
    if edges and max(edges) < 0.8 * w:
        right = w - left
    gutter, col, right = _grid(left, right)
    floor = round(float(footer.get("y") or h)) - 8 if footer else h - max(left, 24)
    source_top = floor - 24
    tokens: dict[str, str] = {
        "canvas-w": f"{w}px", "canvas-h": f"{h}px",
        "m-left": f"{left}px", "m-right": f"{right}px", "content-w": f"{right - left}px",
        "title-top": f"{round(ty)}px", "title-bottom": f"{round(ty + th)}px",
        "content-top": f"{round(ty + th + 16)}px", "content-bottom": f"{source_top - 8}px",
        "source-top": f"{source_top}px", "floor": f"{floor}px",
        "gutter": f"{gutter}px", "col-w": f"{col}px",
    }
    for i in range(12):
        tokens[f"c{i + 1}"] = f"{left + i * (col + gutter)}px"
    for i in range(12):
        tokens[f"e{i + 1}"] = f"{left + i * (col + gutter) + col}px"

    # Type scale (quality bar: body 12-16pt from the body zone, else 14pt; labels 10-12pt; sources 8-9pt).
    # A composed body slide (exhibit + side panel + takeaway strip) sets its copy smaller: body 10.5-12pt
    # (`fs-dense`) under 12-14pt bold unit headers (`fs-head`); the body-zone size is for sparse slides.
    title_pt = round(2 * float(_style(title).get("sizePt") or _level0(manifest, "title").get("sizePt") or 24)) / 2
    body_pt = float(_style(body).get("sizePt") or _level0(manifest, "body").get("sizePt") or 14)
    body_pt = body_pt if 12 <= body_pt <= 16 else 14.0
    label_pt = min(12.0, max(10.0, body_pt - 3))
    source_pt = 9.0 if body_pt >= 13 else 8.0
    subtitle_pt = min(max(title_pt - 4, body_pt), body_pt + 2)
    dense_pt = min(12.0, max(10.5, body_pt - 3))
    head_pt = min(14.0, dense_pt + 2)
    tokens.update({
        "font-head": f"'{major}'", "font-body": f"'{minor}'",
        "fs-title": f"{_px(title_pt):g}px", "fs-subtitle": f"{_px(subtitle_pt):g}px",
        "fs-body": f"{_px(body_pt):g}px", "fs-label": f"{_px(label_pt):g}px", "fs-source": f"{_px(source_pt):g}px",
        "fs-head": f"{_px(head_pt):g}px", "fs-dense": f"{_px(dense_pt):g}px",
    })

    # Colour roles.
    roles = colour_roles(manifest)
    ink, bg = roles["text"] or "#000000", roles["background"] or "#FFFFFF"
    primary = roles["primary"] or (OFFICE_PRIMARY if not colors else _hex(colors.get("accent1")) or OFFICE_PRIMARY)
    accent = roles["accent"] or OFFICE_ACCENT
    light = luminance(bg) > 0.4
    greys = (["#F2F2F2", "#D9D9D9", "#BFBFBF", "#A6A6A6", "#7F7F7F"] if light
             else ["#3A3A3A", "#4D4D4D", "#666666", "#808080", "#A6A6A6"])
    rag = _rag(colors)
    tokens.update({
        "primary": primary, "primary-2": _mix(primary, bg, 0.35), "primary-3": _mix(primary, bg, 0.6),
        "primary-tint": _mix(primary, bg, 0.88), "on-primary": _on(primary, ink),
        "accent": accent, "accent-tint": _mix(accent, bg, 0.8), "on-accent": _on(accent, ink),
        "ink": ink, "ink-2": "#595959" if light else "#BFBFBF", "bg": bg,
        **{f"grey-{i + 1}": g for i, g in enumerate(greys)},
        "rag-r": rag["r"], "rag-a": rag["a"], "rag-g": rag["g"],
        "s1": "4px", "s2": "8px", "s3": "16px", "s4": "24px", "s5": "32px",
    })
    return tokens


def token_css(manifest: dict[str, Any]) -> str:
    """The `:root{…}` block a slide pastes into its `<style>`: a few compact lines, grouped by kind."""
    tokens = design_tokens(manifest)
    if not tokens:
        return ""

    def line(keys: Any) -> str:
        return ";".join(f"--{k}:{tokens[k]}" for k in keys if k in tokens) + ";"

    groups = [
        [k for k in tokens if k in ("canvas-w", "canvas-h", "m-left", "m-right", "content-w", "title-top",
                                     "title-bottom", "content-top", "content-bottom", "source-top", "floor",
                                     "gutter", "col-w")],
        [f"c{i}" for i in range(1, 13)],
        [f"e{i}" for i in range(1, 13)],
        [k for k in tokens if k.startswith(("font-", "fs-"))],
        [k for k in tokens if k.startswith(("primary", "on-", "accent", "ink", "bg"))],
        [k for k in tokens if k.startswith(("grey-", "rag-")) or k in ("s1", "s2", "s3", "s4", "s5")],
    ]
    return ":root{\n" + "\n".join(line(g) for g in groups) + "\n}"


def derive_notes(manifest: dict[str, Any]) -> str:
    """A few lines of design notes from the manifest alone, then the design tokens. Empty when there is
    nothing to say."""
    if not isinstance(manifest, dict) or not _layouts(manifest):
        return ""
    return "\n".join(s for s in (_prose_notes(manifest), token_notes(manifest)) if s)


def token_notes(manifest: dict[str, Any]) -> str:
    """The token block plus the grid in words, for the prompt. Usable on its own after written notes."""
    tokens = design_tokens(manifest)
    if not tokens:
        return ""
    def px(k: str) -> int:
        return int(float(tokens[k][:-2]))

    cols = ", ".join(f"{i} {px(f'c{i}')}-{px(f'e{i}')}" for i in range(1, 13))
    return (
        "Design tokens — paste this block into every slide's <style> and use the variables "
        "(`left:var(--c4)`, `color:var(--ink)`, `font-size:var(--fs-body)`); data-chart JSON cannot read "
        "variables, so write the hex values there:\n"
        f"{token_css(manifest)}\n"
        f"- 12-column grid, gutter {tokens['gutter']}, column {tokens['col-w']}; column x start-end: {cols}. "
        f"A block spanning columns a..b is left:var(--ca); width = e_b - c_a. Body y {tokens['content-top']}-"
        f"{tokens['content-bottom']}; source line top {tokens['source-top']}.\n"
        f"- Type scale: title {tokens['fs-title']}, subtitle/lead-in {tokens['fs-subtitle']}; composed body "
        f"slides: unit headers {tokens['fs-head']} bold, body {tokens['fs-dense']}, pills and source "
        f"{tokens['fs-source']}; sparse slides: body and box headers {tokens['fs-body']}; chart/table labels "
        f"{tokens['fs-label']}."
    )


def _prose_notes(manifest: dict[str, Any]) -> str:
    canvas = manifest.get("canvas") or {}
    w, h = int(canvas.get("w") or 1280), int(canvas.get("h") or 720)
    theme = manifest.get("theme") or {}
    fonts = theme.get("fonts") or {}
    major = fonts.get("major") or fonts.get("heading") or ""
    minor = fonts.get("minor") or fonts.get("body") or major
    colors = theme.get("colors") or {}
    lines = ["Derived from the master itself (it carries no written design notes):"]

    ratio = "16:9" if abs(w / h - 16 / 9) < 0.02 else "4:3" if abs(w / h - 4 / 3) < 0.02 else f"{w}:{h}"
    lines.append(f"- Canvas {w}×{h} px ({ratio}). The layout backgrounds shown with each layout are the "
                 f"master's own: design on top of them, not over them.")
    if major or minor:
        same = major == minor
        lines.append(f"- Fonts: {major} throughout." if same and major
                     else f"- Fonts: {major or 'theme heading font'} for titles, {minor or 'theme body font'} for text.")

    roles = colour_roles(manifest)
    colour = [f"text {roles['text']}{_slot_of(colors, roles['text'])}",
              f"background {roles['background']}{_slot_of(colors, roles['background'])}"]
    if roles["primary"]:
        colour.append(f"primary/structural {roles['primary']}{_slot_of(colors, roles['primary'])}")
    if roles["accent"]:
        colour.append(f"accent (sparingly, for the takeaway) {roles['accent']}{_slot_of(colors, roles['accent'])}")
    lines.append("- Colours (inferred from the theme and the placeholders): " + "; ".join(colour) + ".")
    series = [c for c in (_hex(colors.get(f"accent{i}")) for i in range(1, 7)) if c]
    neutrals = [c for c in series if _hls(c)[2] < 0.15 and c not in (roles["text"], roles["background"])]
    if series:
        lines.append(f"- Chart series in theme order: {', '.join(dict.fromkeys(series))}"
                     + (f"; greys {', '.join(dict.fromkeys(neutrals))} for secondary data." if neutrals else "."))
    if roles["inverted"]:
        lines.append(f"- The theme's slot names are inverted (dk1 is {_hex(colors.get('dk1'))}, lt1 is "
                     f"{_hex(colors.get('lt1'))}; bg1/tx1 follow them): use the hex values above, not the "
                     f"slot names, and never assume \"tx1\" is dark.")

    lay = content_layout(manifest)
    title = next(iter(_placeholders(lay, TITLE_TYPES)), None) if lay else None
    body = next(iter(_placeholders(lay, BODY_TYPES)), None) if lay else None
    footer = next((z for lo in ([lay] if lay else []) + _layouts(manifest)
                   for z in _placeholders(lo, FOOTER_TYPES)), None)
    cover = next((z for lo in _layouts(manifest) for z in _placeholders(lo, COVER_TITLE_TYPES)), None)
    if cover is None:
        cover = next((z for lo in _layouts(manifest) if lo is not lay for z in _placeholders(lo, TITLE_TYPES)
                      if _pt(_style(z).get("sizePt"))
                      and _style(z)["sizePt"] > 1.2 * float(_style(title).get("sizePt") or 0)),
                     None)
    scale = [
        _type_line("titles", _style(title) or _level0(manifest, "title"), major, minor),
        _type_line("body", _style(body) if _style(body).get("sizePt") else _level0(manifest, "body"), major, minor),
        _type_line("footer", _style(footer), major, minor) if footer else None,
        _type_line("cover title", _style(cover), major, minor) if cover else None,
    ]
    shown = [s for s in scale if s]
    if shown:
        lines.append("- Type scale from the placeholders: " + "; ".join(shown) + ".")

    if title and lay:
        x, y, tw, th = (float(title.get(k) or 0) for k in ("x", "y", "w", "h"))
        left, right = round(x), round(w - (x + tw))
        tokens = design_tokens(manifest)
        lines.append(f"- Grid (from {lay.get('name') or lay.get('id')}): margins {left}px left, {right}px right; "
                     f"title zone y {round(y)}–{round(y + th)}; content y {tokens['content-top'][:-2]}–"
                     f"{tokens['content-bottom'][:-2]}, the source line below it (tokens below).")
    if footer:
        lines.append(f"- Footer band from y {round(float(footer.get('y') or 0))}: the master's date/footer/"
                     f"slide-number placeholders live there — keep content above it.")
    else:
        lines.append("- No footer placeholders: any footer, logo or page number is part of the layout "
                     "background — leave the area it occupies clear.")

    edges = [float(z.get("x") or 0) + float(z.get("w") or 0) for lo in _layouts(manifest)
             for z in lo.get("placeholders") or [] if isinstance(z, dict)]
    if edges and max(edges) < 0.8 * w:
        lines.append(f"- Note: every placeholder zone ends by x {round(max(edges))} of the {w}px canvas — the "
                     f"master's layouts were authored for a narrower (4:3) slide. Keep titles in their zone; "
                     f"the token grid below spans the full width with the same "
                     f"{round(float((title or {}).get('x') or 0))}px side margin.")
    return "\n".join(lines)
