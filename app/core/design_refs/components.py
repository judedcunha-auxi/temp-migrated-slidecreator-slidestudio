# The component generators' signatures ARE the catalog: `inspect` reads each keyword default as the
# documented example parameter (`spec`, `catalog`, the `get_component` params skeleton), so they keep
# their plain example values (lists and dicts, never mutated: every generator copies what it reads,
# tests/core/design_refs/test_components.py holds that) and stay unannotated. Typing them would add no
# safety: the model passes JSON, and `get` validates it with `_need` before anything is built.
# mypy: disable-error-code="no-untyped-def,no-untyped-call"
# ruff: noqa: B006
"""Pre-tested, parameterised exhibit components for the design chat (research recommendation #4).

Each component is a generator that returns an HTML fragment inside the authoring contract
(`docs/engine/03-AUTHORING-CONTRACT.md`): one absolutely positioned wrapper at the box it is given
(x, y, w, h in canvas px) with absolutely positioned children, native shapes only (divs, clip-path
chevrons, tables, straight-line SVG, `data-chart`). Colours and type sizes are the design tokens
`design_refs.notes.token_css` writes into the prompt (`var(--primary)`, `var(--fs-body)`, …), each with
a fallback, so a fragment pasted into a slide that carries the token block takes on that master, and one
pasted without it still renders. `data-chart` JSON cannot read CSS variables, so chart components write
hex: pass `tokens` (the dict `notes.design_tokens(manifest)` returns, or any subset such as
{"primary": "#1A9AFA"}) to have them — and the fallbacks — follow the master.

Verified by `tests/core/design_refs/test_components.py`: every component, at its defaults, lints clean against a
real manifest, and a representative set exports through Path A as native shapes, tables and charts.

Public API (the `get_component` tool calls these):
- `catalog() -> str` — the compact list that rides in the prompt;
- `get(name, **params) -> str` — the snippet; KeyError on an unknown name, ValueError on bad params;
- `names()`, `spec(name)` for tests and tooling.
"""
from __future__ import annotations

import hashlib
import html as _html
import inspect
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# ------------------------------------------------------------------------------------ tokens
#: Fallbacks for every token a component reads, used when the slide has no token block and the caller
#: passed no `tokens`. A neutral consulting palette on the 1280x720 canvas with 64px margins. Body copy is
#: `fs-dense` (the compact size of a composed slide, notes.design_tokens), not the body zone's `fs-body`.
FALLBACK: dict[str, str] = {
    "primary": "#1F4E79", "primary-2": "#6D90AF", "primary-3": "#A0B7CB", "primary-tint": "#E4EAF0",
    "on-primary": "#FFFFFF", "accent": "#E36C0A", "accent-tint": "#FBE2CE", "on-accent": "#FFFFFF",
    "ink": "#1A1A1A", "ink-2": "#595959", "bg": "#FFFFFF",
    "grey-1": "#F2F2F2", "grey-2": "#D9D9D9", "grey-3": "#BFBFBF", "grey-4": "#A6A6A6", "grey-5": "#7F7F7F",
    "rag-r": "#D9534F", "rag-a": "#F0AD4E", "rag-g": "#5CB85C",
    "fs-title": "29.33px", "fs-subtitle": "21.33px", "fs-body": "18.67px", "fs-label": "14.67px",
    "fs-source": "12px", "fs-dense": "14.67px", "fs-head": "17.33px", "font-head": "Arial", "font-body": "Arial",
    "m-left": "64px", "m-right": "1216px", "content-top": "106px", "content-bottom": "624px",
    "source-top": "632px", "title-top": "28px", "title-bottom": "90px",
}
_HEX = re.compile(r"#[0-9A-Fa-f]{6}")


@dataclass
class Tok:
    """The token values a generator reads: `v()` for CSS (a `var()` with fallback), `hex()` for chart
    JSON and SVG presentation attributes, `px()` for layout arithmetic."""
    values: dict[str, str] = field(default_factory=dict)

    def raw(self, key: str) -> str:
        return str(self.values.get(key) or FALLBACK[key])

    def v(self, key: str) -> str:
        if key.startswith("font-"):
            return f"var(--{key})"
        return f"var(--{key},{self.raw(key)})"

    def hex(self, key: str) -> str:
        value = self.raw(key)
        return value.upper() if _HEX.fullmatch(value) else FALLBACK[key]

    def strong(self) -> str:
        """The token for a highlighted LINE, DOT or TEXT: the accent, unless it is too light to read on the
        background (a yellow accent on white), then the primary. Fills keep the accent either way."""
        a = self.hex("accent")
        lum = 0.2126 * int(a[1:3], 16) + 0.7152 * int(a[3:5], 16) + 0.0722 * int(a[5:7], 16)
        return "primary" if lum / 255 > 0.6 else "accent"

    def px(self, key: str) -> float:
        try:
            return float(self.raw(key).removesuffix("px"))
        except ValueError:
            return float(FALLBACK[key].removesuffix("px"))


def mix(a: str, b: str, share: float) -> str:
    """`share` of #b mixed into #a, as #RRGGBB (for chart JSON, which cannot use color-mix)."""
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * (1 - share) + y * share):02X}" for x, y in zip(pa, pb, strict=False))


def cmix(t: Tok, key: str, pct: float, other: str = "bg") -> str:
    """CSS colour: `pct`% of a token over another (a tint ramp that follows the token block)."""
    return f"color-mix(in srgb,{t.v(key)} {round(pct)}%,{t.v(other)})"


# ------------------------------------------------------------------------------------ helpers
def esc(text: Any) -> str:
    """Escape user text; `**lead**` becomes bold (the house style bolds lead phrases)."""
    out = _html.escape("" if text is None else str(text), quote=False)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)


def n(value: float) -> str:
    """A px number for CSS: integers stay integers, others keep 2 decimals."""
    r = round(float(value), 2)
    return f"{int(r)}" if r == int(r) else f"{r:g}"


def pos(x: float, y: float, w: float, h: float | None = None) -> str:
    s = f"position:absolute;left:{n(x)}px;top:{n(y)}px;width:{n(w)}px"
    return s + (f";height:{n(h)}px" if h is not None else "")


def box(x, y, w, h, style: str = "", content: str = "", name: str | None = None) -> str:
    attr = f' data-name="{_html.escape(name)}"' if name else ""
    return f'<div{attr} style="{pos(x, y, w, h)};box-sizing:border-box;{style}">{content}</div>'


def text(t: Tok, x, y, w, h, content: str, *, size="fs-dense", color="ink", bold=False, align="left",
         valign="top", lh=1.2, style="", name=None) -> str:
    """A text block. `valign` centre/bottom uses a flex column (normal flow inside the box)."""
    flex = ""
    if valign != "top":
        flex = (f"display:flex;flex-direction:column;justify-content:"
                f"{'center' if valign == 'middle' else 'flex-end'};")
        content = f"<div>{content}</div>"
    weight = "font-weight:700;" if bold else ""
    return box(x, y, w, h, f"{flex}font-size:{t.v(size)};line-height:{lh};color:{t.v(color)};{weight}"
               f"text-align:{align};{style}", content, name)


def bullets(items: list[Any], t: Tok, *, size="fs-dense", color="ink", gap=4) -> str:
    lis = "".join(f'<li style="margin:0 0 {gap}px 0">{esc(i)}</li>' for i in items)
    return (f'<ul style="margin:0;padding-left:1.1em;font-size:{t.v(size)};line-height:1.2;'
            f'color:{t.v(color)}">{lis}</ul>')


def chip(t: Tok, x, y, label, *, d=24, fill="primary", color="on-primary", size="fs-label") -> str:
    """A numbered chip: a filled circle with a bold centred number."""
    return box(x, y, d, d, f"border-radius:50%;background:{t.v(fill)};color:{t.v(color)};font-size:{t.v(size)};"
               f"font-weight:700;line-height:{n(d)}px;text-align:center", esc(label))


def chevron(width: float, tip: float, notch: bool = True) -> str:
    """A chevron (or, without the notch, a pentagon arrow) as a clip-path of plain percentages: the export
    reads `polygon()` points in % or px, not `calc()`."""
    t = f"{100 - 100 * tip / width:.2f}%"
    left = f",{100 * tip / width:.2f}% 50%" if notch else ""
    return f"polygon(0 0,{t} 0,100% 50%,{t} 100%,0 100%{left})"


def uid(name: str, params: dict[str, Any]) -> str:
    return name[:3] + hashlib.sha1(json.dumps(params, sort_keys=True, default=str).encode(), usedforsecurity=False).hexdigest()[:6]


def arrow_defs(mid: str, colour: str) -> str:
    return (f'<defs><marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
            f'markerHeight="7" orient="auto"><path d="M0 0 L10 5 L0 10 Z" fill="{colour}"/></marker></defs>')


def svg(x, y, w, h, inner: str) -> str:
    return (f'<svg style="{pos(x, y, w, h)};overflow:visible" width="{n(w)}" height="{n(h)}" '
            f'viewBox="0 0 {n(w)} {n(h)}">{inner}</svg>')


def harvey(t: Tok, cx, cy, score: float, r=9) -> str:
    """A Harvey ball (0-4 quarters) as native SVG: an outlined circle plus a filled wedge."""
    s = max(0, min(4, round(float(score))))
    p, bg = t.hex("primary"), t.hex("bg")
    size = 2 * r + 2
    inner = f'<circle cx="{r + 1}" cy="{r + 1}" r="{r}" fill="{p if s == 4 else bg}" stroke="{p}" stroke-width="1.5"/>'
    c = r + 1
    if s in (1, 2, 3):
        end = {1: (c + r, c), 2: (c, c + r), 3: (c - r, c)}[s]
        large = 1 if s == 3 else 0
        inner += (f'<path d="M{c} {c} L{c} {c - r} A{r} {r} 0 {large} 1 {end[0]} {end[1]} Z" fill="{p}"/>')
    return svg(cx - c, cy - c, size, size, inner)


def check(t: Tok, cx, cy, kind, s=16) -> str:
    """Presence mark: True/1/"yes" a check, 0.5/"partial" a grey check, False/0/"no" a short grey dash."""
    k = str(kind).lower()
    if k in ("1", "true", "yes", "y", "full"):
        colour, shape = t.hex("primary"), "check"
    elif k in ("0.5", "partial", "p", "half"):
        colour, shape = t.hex("grey-4"), "check"
    else:
        colour, shape = t.hex("grey-3"), "dash"
    if shape == "check":
        inner = (f'<polyline points="2,{s * .55:.1f} {s * .4:.1f},{s - 2} {s - 2},3" fill="none" stroke="{colour}" '
                 f'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>')
    else:
        inner = f'<line x1="{s * .25:.1f}" y1="{s / 2}" x2="{s * .75:.1f}" y2="{s / 2}" stroke="{colour}" stroke-width="2"/>'
    return svg(cx - s / 2, cy - s / 2, s, s, inner)


def hrule(t: Tok, x, y, w, *, colour="grey-3", weight=1) -> str:
    return box(x, y, w, weight, f"background:{t.v(colour)}")


def vrule(t: Tok, x, y, h, *, colour="grey-3", weight=1) -> str:
    return box(x, y, weight, h, f"background:{t.v(colour)}")


def chart(x, y, w, h, spec: dict[str, Any], name: str) -> str:
    data = json.dumps(spec, separators=(",", ":")).replace("'", "&#39;")
    return f'<div class="chart" data-name="{name}" style="{pos(x, y, w, h)}" data-chart=\'{data}\'></div>'


def nice_axis(lo: float, hi: float, ticks: int = 4) -> tuple[float, float, float]:
    """A value axis (min, max, majorUnit) on round numbers that contains lo..hi."""
    lo, hi = min(lo, 0.0), max(hi, 0.0)
    span = (hi - lo) or 1.0
    raw = span / ticks
    mag = 10 ** math.floor(math.log10(raw))
    unit = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    return math.floor(lo / unit) * unit, math.ceil(hi / unit) * unit, unit


def fmt(value: Any, kind: str = "0.0") -> str:
    """Banking number formats: "x" multiple (11.2x), "%" (12.5%), "$" / "0" integers with separators,
    "0.0" one decimal; negatives in parentheses; None/"" -> "n.a."; strings pass through."""
    if value is None or value == "":
        return "n.a."
    if isinstance(value, str):
        return value
    v = float(value)
    body = {"x": f"{abs(v):,.1f}x", "%": f"{abs(v):,.1f}%", "$": f"{abs(v):,.0f}", "0": f"{abs(v):,.0f}",
            "0.00": f"{abs(v):,.2f}"}.get(kind, f"{abs(v):,.1f}")
    return f"({body})" if v < 0 else body


def _signed(value: float, kind: str = "0.0") -> str:
    """A change with its sign: +4.2, -1.3; a "%" change is in points (+2.0 pts), "pct" a relative +12.5%."""
    v = float(value)
    sign = "+" if v > 0 else "-" if v < 0 else ""
    body = f"{abs(v):,.1f} pts" if kind == "%" else fmt(abs(v), "%" if kind == "pct" else kind)
    return sign + body


def _direction(value: Any, direction: str = "auto") -> str:
    """up / down / flat: as given, or (auto) read off the value's sign ("+3%", "(2.1)", -4)."""
    if direction in ("up", "down", "flat"):
        return direction
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "up" if value > 0 else "down" if value < 0 else "flat"
    s = str(value).strip()
    return "up" if s.startswith("+") else "down" if s.startswith(("-", "−", "(")) else "flat"


def _delta(t: Tok, x, y, w, h, label: str, direction: str, good_when: str, *, size="fs-label") -> str:
    """A delta badge's parts: a triangle (clip-path polygon, a native shape) and the bold value, both in
    green when the move is good, red when bad, grey when flat."""
    colour = "ink-2" if direction == "flat" else "rag-g" if direction == good_when else "rag-r"
    fs = t.px(size)
    tw = round(fs * 0.7, 1)
    th = round(tw * 0.8, 1) if direction != "flat" else 2.5
    poly = {"up": "polygon(50% 0,100% 100%,0 100%)", "down": "polygon(0 0,100% 0,50% 100%)"}.get(direction)
    mark = box(x, y + (h - th) / 2, tw, th, f"background:{t.v(colour)}" + (f";clip-path:{poly}" if poly else ""))
    return mark + text(t, x + tw + 4, y, max(8.0, w - tw - 4), h, esc(label), size=size, bold=True, color=colour,
                       valign="middle", lh=1, style="white-space:nowrap")


def _swatches(t: Tok, x, y, h, items: list[tuple[str, str]]) -> tuple[str, float]:
    """A legend row of (css fill, label) swatches in fs-source; returns the html and the row's width."""
    out, lx = [], x
    for fill, label in items:
        out.append(box(lx, y + (h - 10) / 2, 10, 10, f"background:{fill}"))
        lw = 8 + 6.8 * len(label)
        out.append(text(t, lx + 14, y, lw, h, esc(label), size="fs-source", color="ink-2", valign="middle", lh=1,
                        style="white-space:nowrap"))
        lx += 14 + lw + 14
    return "".join(out), lx - x


# ------------------------------------------------------------------------------------ registry
@dataclass
class Spec:
    name: str
    purpose: str
    group: str
    fn: Callable[..., str]
    box: tuple[float, float, float, float]
    params: dict[str, Any]
    hint: str = ""


_REGISTRY: dict[str, Spec] = {}
_COMMON = ("x", "y", "w", "h", "tokens")


def component(name: str, purpose: str, group: str, box=(64, 120, 1152, 480), hint: str = ""):
    def register(fn):
        sig = inspect.signature(fn)
        params = {k: p.default for k, p in sig.parameters.items() if p.kind is p.KEYWORD_ONLY}
        _REGISTRY[name] = Spec(name, purpose, group, fn, tuple(box), params, hint)
        return fn
    return register


def names() -> list[str]:
    return list(_REGISTRY)


def spec(name: str) -> Spec:
    return _REGISTRY[name]


def _check_type(key: str, value: Any, default: Any) -> Any:
    if default is None or value is None:
        return value
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return value
    if isinstance(default, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
        return value
    if isinstance(default, str) and not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    if isinstance(default, list) and not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    if isinstance(default, dict) and not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _shape(default: Any) -> str:
    """A parameter's expected shape, read off its default: `list of {title, bullets}`, `number`, …"""
    if isinstance(default, bool):
        return "true/false"
    if isinstance(default, (int, float)):
        return "number"
    if isinstance(default, str):
        return "text"
    if isinstance(default, dict):
        return "{" + ", ".join(default) + "}"
    if isinstance(default, list):
        first = default[0] if default else None
        if isinstance(first, dict):
            keys = list(dict.fromkeys(k for item in default if isinstance(item, dict) for k in item))
            return f"list of {{{', '.join(keys)}}}"
        if isinstance(first, list):
            return "list of lists"
        return "list of " + ("numbers" if isinstance(first, (int, float)) else "text")
    return "optional"


def get(name: str, **params: Any) -> str:
    """The component's HTML fragment for these params (defaults fill the rest).

    Raises KeyError for an unknown component and ValueError for an unknown or ill-typed parameter or a
    value the component cannot draw (too many steps, mismatched row lengths, …)."""
    if name not in _REGISTRY:
        raise KeyError(f"unknown component {name!r}; known: {', '.join(_REGISTRY)}")
    s = _REGISTRY[name]
    unknown = sorted(set(params) - set(s.params) - set(_COMMON))
    if unknown:
        raise ValueError(f"{name}: unknown parameter(s) {', '.join(unknown)}; "
                         f"accepted: {', '.join(list(_COMMON[:4]) + list(s.params))}")
    x, y, w, h = (params.pop(k, d) for k, d in zip(("x", "y", "w", "h"), s.box, strict=False))
    for k, v in (("x", x), ("y", y), ("w", w), ("h", h)):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{k} must be a number (px)")
    if w <= 0 or h <= 0:
        raise ValueError("w and h must be positive")
    tokens = params.pop("tokens", None) or {}
    if not isinstance(tokens, dict):
        raise ValueError("tokens must be an object of token name -> value")
    tok = Tok({str(k).removeprefix("--"): str(v) for k, v in tokens.items()})
    kwargs = _resolve(name, params)
    body = s.fn(tok, float(x), float(y), float(w), float(h), **kwargs)
    shapes = "; ".join(f"{k}: {_shape(d)}" for k, d in s.params.items())
    header = (f"<!-- component {name}: {s.purpose} Box {n(x)},{n(y)} {n(w)}x{n(h)}. Params — {shapes}."
              f"{' ' + s.hint if s.hint else ''} Adapt the copy; keep the structure. -->")
    return header + "\n" + body


def catalog() -> str:
    """The compact list for the prompt: one line per component, grouped."""
    lines = ["Each takes x, y, w, h (its box, px; default the body area) plus the params listed. No content "
             "params draws a demo; with your own content, anything you leave out is simply not drawn."]
    group = None
    for s in _REGISTRY.values():
        if s.group != group:
            group = s.group
            lines.append(f"{group}:")
        lines.append(f"- {s.name}({', '.join(s.params)}) — {s.purpose}")
    return "\n".join(lines)


def _need(cond: bool, message: str) -> None:
    if not cond:
        raise ValueError(message)


def _wrap(name: str, x, y, w, h, inner: str, group: bool = True) -> str:
    g = ' data-pptx="group"' if group else ""
    return f'<div data-name="{name}"{g} style="{pos(x, y, w, h)}">{inner}</div>'


# ==================================================================================== furniture
@component("action_title", "action title in the title placeholder, optional rule under it.", "Furniture",
           box=(64, 28, 1152, 62), hint="Use the title zone's box; rule only if the master draws none.")
def action_title(t, x, y, w, h, *, text="Three structural gaps drive our 23% cost premium over peers",
                 bold=False, rule=False):
    weight = "700" if bold else "400"
    out = (f'<h1 data-placeholder="title" style="{pos(x, y, w, h)};margin:0;font-family:{t.v("font-head")};'
           f'font-size:{t.v("fs-title")};font-weight:{weight};line-height:1.1;color:{t.v("ink")}">{esc(text)}</h1>')
    if rule:
        out += "\n" + box(x, y + h + 4, w, 2, f"background:{t.v('primary')}", name="Title rule")
    return out


@component("source_note", "source line with optional numbered notes above it, bottom-left.", "Furniture",
           box=(64, 632, 1152, 24), hint="Pass the same source text to save_slide.")
def source_note(t, x, y, w, h, *, source="Source: Company filings; Capital IQ (as of 30 Jun 2026); team analysis",
                notes=[]):
    lines = [f"({i + 1}) {esc(s)}" if not str(s).startswith(("Note", "(")) else esc(s) for i, s in enumerate(notes)]
    lines.append(esc(source))
    lh = t.px("fs-source") * 1.25
    top = y + h - lh * len(lines)
    return text(t, x, top, w, lh * len(lines), "<br>".join(lines), size="fs-source", color="ink-2", lh=1.25,
                name="Source")


@component("tracker", "breadcrumb chips naming the deck's sections, active one filled.", "Furniture",
           box=(816, 4, 400, 20))
def tracker(t, x, y, w, h, *, sections=["Context", "Diagnosis", "Options", "Plan"], active=1):
    _need(1 <= len(sections) <= 7, "tracker takes 1-7 sections")
    gap = 4
    cw = (w - gap * (len(sections) - 1)) / len(sections)
    out = []
    for i, s in enumerate(sections):
        on = i == active
        out.append(text(t, i * (cw + gap), 0, cw, h, esc(s), size="fs-source", bold=on, align="center",
                        valign="middle", lh=1, color="on-primary" if on else "ink-2",
                        style=f"background:{t.v('primary') if on else t.v('grey-1')};border-radius:2px"))
    return _wrap("Tracker", x, y, w, h, "".join(out))


@component("sticker", "status tag such as Preliminary, Illustrative or Draft.", "Furniture", box=(1096, 4, 120, 22))
def sticker(t, x, y, w, h, *, label="Preliminary"):
    return text(t, x, y, w, h, esc(label).upper(), size="fs-source", bold=True, color="primary",
                align="center", valign="middle", lh=1, name="Sticker",
                style=f"border:1.5px solid {t.v('primary')};border-radius:2px;letter-spacing:0.5px")


@component("callout", "takeaway box: grey panel (or primary fill), bold lead, one or two lines.",
           "Text blocks", box=(64, 520, 1152, 88))
def callout(t, x, y, w, h, *, title="So what", body="Closing the procurement gap alone is worth "
            "**USD 14-18m** a year, two-thirds of the premium.", style="panel"):
    # No flush accent stripe on the box: a one-sided border is the most recognisable "AI slide" tell, and
    # the export draws it square under rounded corners. "bar" keeps its name but draws a separate
    # square accent block with a gap, beside the panel.
    _need(style in ("panel", "bar", "fill"), "style is 'panel', 'bar' or 'fill'")
    fill = t.v("primary") if style == "fill" else t.v("grey-1")
    ink = "on-primary" if style == "fill" else "ink"
    px0 = 0.0
    inner = ""
    if style == "bar":
        inner += box(0, 0, 8, h, f"background:{t.v('accent')}")
        px0 = 16.0
    inner += box(px0, 0, w - px0, h, f"background:{fill}")
    inner += text(t, px0 + 16, 0, w - px0 - 32, h, f"<b>{esc(title)}</b><br>{esc(body)}" if title else esc(body),
                  color=ink, valign="middle")
    return _wrap("Callout", x, y, w, h, inner)


@component("numbered_list", "numbered takeaways: chip, bold lead-in, one or two lines each, rules between.",
           "Text blocks")
def numbered_list(t, x, y, w, h, *, items=[
        {"title": "Procurement", "text": "Unit costs 9% above peers on fragmented supplier base (420 vs 180)"},
        {"title": "Footprint", "text": "Three sub-scale plants run at 58% utilisation versus 80% benchmark"},
        {"title": "Overhead", "text": "SG&A at 14% of revenue, 4 pts above the peer median"}]):
    _need(1 <= len(items) <= 8, "numbered_list takes 1-8 items")
    rh = h / len(items)
    out = []
    for i, it in enumerate(items):
        top = i * rh
        if i:
            out.append(hrule(t, 0, top, w, colour="grey-2"))
        out.append(chip(t, 0, top + (rh - 28) / 2, i + 1, d=28))
        title, body = (it.get("title"), it.get("text")) if isinstance(it, dict) else (None, it)
        content = (f"<b>{esc(title)}</b><br>" if title else "") + esc(body or "")
        out.append(text(t, 44, top, w - 44, rh, content, valign="middle"))
    return _wrap("Numbered list", x, y, w, h, "".join(out))


@component("three_column", "parallel columns (e.g. Situation / Implication / Action), arrows between.",
           "Text blocks")
def three_column(t, x, y, w, h, *, columns=[
        {"title": "Situation", "bullets": ["Volumes flat since 2023", "Price pressure from two new entrants"]},
        {"title": "Implication", "bullets": ["EBITDA margin falls 3 pts by FY27E", "Cash to fund capex shrinks"]},
        {"title": "Action", "bullets": ["Cut cost-to-serve 12%", "Reprice the long tail of SKUs"]}],
        arrows=True, highlight=2):
    k = len(columns)
    _need(2 <= k <= 5, "three_column takes 2-5 columns")
    gap = 40 if arrows else 24
    cw = (w - gap * (k - 1)) / k
    out = []
    for i, c in enumerate(columns):
        cx = i * (cw + gap)
        on = i == highlight
        out.append(box(cx, 0, cw, 44, f"background:{t.v('primary') if on else t.v('grey-1')}"))
        out.append(text(t, cx + 12, 0, cw - 24, 44, esc(c.get("title", "")), bold=True, valign="middle",
                        color="on-primary" if on else "ink"))
        out.append(box(cx, 56, cw, h - 56, "", bullets(c.get("bullets", []), t)))
        if arrows and i < k - 1:
            ax = cx + cw + 8
            out.append(box(ax, 10, gap - 16, 24, f"background:{t.v('grey-4')};clip-path:polygon(0 30%,55% 30%,"
                           f"55% 0,100% 50%,55% 100%,55% 70%,0 70%)"))
    return _wrap("Columns", x, y, w, h, "".join(out))


@component("pros_cons", "two columns of advantages and risks with plus / minus markers.", "Text blocks")
def pros_cons(t, x, y, w, h, *, pros_title="Advantages", cons_title="Risks",
              pros=["Adds USD 40m EBITDA from year 2", "Fills the mid-market gap", "Low integration effort"],
              cons=["Premium of 35% to undisturbed price", "Two overlapping plants to close"]):
    _need(bool(pros or cons), "pros_cons: pass `pros` and/or `cons`")
    gap = 32
    cw = (w - gap) / 2
    out = []
    for i, (title, items, colour, sign) in enumerate(((pros_title, pros, "rag-g", "+"), (cons_title, cons, "rag-r", "-"))):
        if not items and not title:
            continue
        cx = i * (cw + gap)
        out.append(box(cx, 0, 24, 24, f"border-radius:50%;background:{t.v(colour)}"))
        bar = box(cx + 6, 11, 12, 2, "background:#FFFFFF")
        if sign == "+":
            bar += box(cx + 11, 6, 2, 12, "background:#FFFFFF")
        out.append(bar)
        out.append(text(t, cx + 34, 0, cw - 34, 24, esc(title), bold=True, valign="middle", lh=1))
        out.append(hrule(t, cx, 34, cw, colour="grey-4"))
        rows = len(items) or 1
        rh = min(64, (h - 46) / rows)
        for j, it in enumerate(items):
            top = 46 + j * rh
            out.append(text(t, cx, top, cw, rh - 6, esc(it)))
            if j < len(items) - 1:
                out.append(hrule(t, cx, top + rh - 4, cw, colour="grey-2"))
    return _wrap("Pros and cons", x, y, w, h, "".join(out))


@component("keyed_markers", "numbered markers on an exhibit, the same numbers on side-panel notes.",
           "Text blocks", hint="Box = the exhibit plus the notes panel; marker x, y are canvas px (the chip "
           "centre) inside the exhibit part; panel is the notes' width (side right) or height (below), 0 = auto.")
def keyed_markers(t, x, y, w, h, *, markers=[
        {"n": 1, "x": 250, "y": 250}, {"n": 2, "x": 520, "y": 330}, {"n": 3, "x": 700, "y": 450}],
        notes=[{"n": 1, "lead": "Procurement", "text": "Unit costs 9% above peers on a fragmented supplier base"},
               {"n": 2, "lead": "Footprint", "text": "Three sub-scale plants run at 58% utilisation"},
               {"n": 3, "lead": "Overhead", "text": "SG&A 4 pts above the peer median"}],
        side="right", panel=0, color="primary"):
    _need(1 <= len(markers) <= 12, "keyed_markers takes 1-12 markers")
    _need(len(notes) <= 12, "keyed_markers takes up to 12 notes")
    _need(side in ("right", "below"), "side is right or below")
    _need(color in ("primary", "accent", "ink"), "color is primary, accent or ink")
    on = {"primary": "on-primary", "accent": "on-accent", "ink": "bg"}[color]
    keys = [str(nt.get("n", i + 1)) for i, nt in enumerate(notes)]
    if notes:
        pw = panel or (min(380.0, w * 0.34) if side == "right" else min(150.0, h * 0.34))
        _need(40 <= pw <= (w if side == "right" else h) - 40, "panel leaves no room for the exhibit")
    else:
        pw = 0.0
    ex_w, ex_h = (w - pw - (24 if pw else 0), h) if side == "right" else (w, h - pw - (16 if pw else 0))
    d = 24

    def marker(cx, cy, label, size=d):
        return box(cx - size / 2, cy - size / 2, size, size,
                   f"border-radius:50%;background:{t.v(color)};border:2px solid {t.v('bg')};color:{t.v(on)};"
                   f"font-size:{t.v('fs-source')};font-weight:700;line-height:{n(size - 4)}px;text-align:center",
                   esc(label))

    out = []
    for i, m in enumerate(markers):
        _need(isinstance(m, dict) and isinstance(m.get("x"), (int, float)) and isinstance(m.get("y"), (int, float)),
              "every marker is {n, x, y} with x, y in canvas px")
        label = str(m.get("n", i + 1))
        _need(not notes or label in keys, f"marker {label} has no note with that number")
        mx, my = float(m["x"]) - x, float(m["y"]) - y
        _need(0 <= mx <= ex_w and 0 <= my <= ex_h,
              f"marker {label} at {m['x']},{m['y']} lies outside the exhibit part of the box")
        out.append(marker(mx, my, label))
    k = len(notes)
    for i, nt in enumerate(notes):
        body = (f"<b>{esc(nt['lead'])}</b><br>" if nt.get("lead") else "") + esc(nt.get("text", ""))
        if side == "right":
            slot = h / k
            nx, ny, nw, nh = w - pw, i * slot, pw, slot
            if i:
                out.append(hrule(t, nx, ny, nw, colour="grey-2"))
        else:
            slot = (w - 16 * (k - 1)) / k
            nx, ny, nw, nh = i * (slot + 16), h - pw, slot, pw
        out.append(marker(nx + d / 2, ny + 8 + d / 2, keys[i], d - 2))
        out.append(text(t, nx + d + 10, ny + 8, nw - d - 10, nh - 12, body, lh=1.2))
    return _wrap("Keyed markers", x, y, w, h, "".join(out), group=False)


# ==================================================================================== numbers
@component("kpi_tiles", "3-5 tiles: big number, one-line qualifier, optional delta; one in accent.",
           "Numbers", box=(64, 120, 1152, 150))
def kpi_tiles(t, x, y, w, h, *, tiles=[
        {"value": "USD 1.2bn", "label": "Revenue FY25", "delta": "+8% YoY"},
        {"value": "18.4%", "label": "EBITDA margin", "delta": "+1.2 pts"},
        {"value": "3.1x", "label": "Net debt / EBITDA", "delta": "-0.4x"},
        {"value": "42", "label": "Markets served", "delta": ""}], highlight=1, gap=24):
    k = len(tiles)
    _need(1 <= k <= 6, "kpi_tiles takes 1-6 tiles")
    tw = (w - gap * (k - 1)) / k
    longest = max((len(str(tl.get("value", ""))) for tl in tiles), default=4)
    big = min(48.0, h * 0.34, (tw - 32) / (0.62 * max(longest, 3)))
    out = []
    for i, tile in enumerate(tiles):
        tx = i * (tw + gap)
        on = i == highlight
        # Emphasis by a solid fill, not a coloured top stripe on the tile (an "AI card" tell).
        out.append(box(tx, 0, tw, h, f"background:{t.v('accent') if on else t.v('grey-1')}"))
        out.append(text(t, tx + 16, 16, tw - 32, big * 1.15, esc(tile.get("value", "")), bold=True, lh=1.1,
                        color="on-accent" if on else "primary", style=f"font-size:{n(big)}px"))
        out.append(text(t, tx + 16, 20 + big * 1.15, tw - 32, h - 36 - big * 1.15 - 22, esc(tile.get("label", "")),
                        color="on-accent" if on else "ink"))
        delta = str(tile.get("delta") or "")
        if delta:
            colour = "on-accent" if on else "rag-g" if delta.startswith("+") else "rag-r" if delta.startswith(
                ("-", "(")) else "ink-2"
            out.append(text(t, tx + 16, h - 30, tw - 32, 20, esc(delta), size="fs-label", bold=True,
                            color=colour, lh=1.2))
    return _wrap("KPI tiles", x, y, w, h, "".join(out))


@component("delta_badge", "change badge: triangle plus value, green if good, red if bad.",
           "Numbers", box=(64, 120, 120, 24), hint="value is text (-12%, +3.1 pts); direction auto reads its "
           "sign; good_when down makes a fall (costs, churn, days) read green.")
def delta_badge(t, x, y, w, h, *, value="+8.4%", direction="auto", good_when="up"):
    _need(direction in ("auto", "up", "down", "flat"), "direction is auto, up, down or flat")
    _need(good_when in ("up", "down"), "good_when is up or down")
    way = _direction(value, direction)
    return _wrap("Delta", x, y, w, h, _delta(t, 0, 0, w, h, value, way, good_when))


# ==================================================================================== flows
def _ramp(k: int, i: int) -> float:
    """Share of primary for step i of k: dark to light, 100% down to 45%."""
    return 100 - (55 * i / (k - 1) if k > 1 else 0)


@component("chevron_process", "3-7 left-to-right chevrons, dark-to-light, with 2-3 bullets under each.",
           "Flows and structures")
def chevron_process(t, x, y, w, h, *, steps=[
        {"title": "Diagnose", "bullets": ["Baseline cost by line", "Benchmark vs 6 peers"]},
        {"title": "Design", "bullets": ["Target operating model", "Business case per lever"]},
        {"title": "Pilot", "bullets": ["Two plants, 12 weeks", "Track weekly KPIs"]},
        {"title": "Scale", "bullets": ["Roll out to network", "Embed in budgeting"]}], ramp=True):
    k = len(steps)
    _need(3 <= k <= 7, "chevron_process takes 3-7 steps")
    ch = 52
    tip = 18
    overlap = tip - 6
    sw = (w + overlap * (k - 1)) / k
    size = "fs-dense" if k <= 4 else "fs-label"
    out = []
    for i, s in enumerate(steps):
        sx = i * (sw - overlap)
        share = _ramp(k, i) if ramp else 100
        poly = chevron(sw, tip, notch=bool(i))
        ink = "on-primary" if share >= 75 else "ink"
        out.append(box(sx, 0, sw, ch, f"background:{cmix(t, 'primary', share)};clip-path:{poly}"))
        pad = tip + 6 if i else 14
        out.append(text(t, sx + pad, 0, sw - pad - tip - 4, ch, esc(s.get("title", "")), bold=True,
                        valign="middle", color=ink, size=size, lh=1.1))
        bx = sx + (tip if i else 0)
        out.append(box(bx, ch + 16, sw - tip - (tip if i else 0) + (tip - overlap) + 2, h - ch - 16, "",
                       bullets(s.get("bullets", []), t, size=size)))
    return _wrap("Process", x, y, w, h, "".join(out))


@component("value_chain", "pentagon-arrow stages, activities under each, optional support band.",
           "Flows and structures")
def value_chain(t, x, y, w, h, *, stages=[
        {"title": "Source", "items": ["Supplier base", "Contracts"]},
        {"title": "Make", "items": ["6 plants", "Quality"]},
        {"title": "Move", "items": ["3 DCs", "Fleet"]},
        {"title": "Sell", "items": ["Key accounts", "E-commerce"]},
        {"title": "Service", "items": ["Warranty", "Parts"]}],
        support=["Finance, HR and IT"], highlight=2):
    k = len(stages)
    _need(3 <= k <= 8, "value_chain takes 3-8 stages")
    out = []
    top = 0
    for s in support:
        out.append(text(t, 0, top, w - 20, 32, esc(s), bold=True, valign="middle", align="center",
                        style=f"background:{t.v('grey-1')}", size="fs-label"))
        top += 40
    tip, gap = 16, 4
    sw = (w - gap * (k - 1)) / k
    ch = 48
    for i, s in enumerate(stages):
        sx = i * (sw + gap)
        on = i == highlight
        poly = chevron(sw, tip, notch=False)
        out.append(box(sx, top, sw, ch, f"background:{t.v('accent') if on else t.v('primary')};clip-path:{poly}"))
        out.append(text(t, sx + 10, top, sw - tip - 14, ch, esc(s.get("title", "")), bold=True, valign="middle",
                        color="on-accent" if on else "on-primary", lh=1.1))
        out.append(box(sx, top + ch + 12, sw - tip, h - top - ch - 12, "",
                       bullets(s.get("items", []), t, size="fs-label")))
    return _wrap("Value chain", x, y, w, h, "".join(out))


@component("pyramid", "3-5 stacked levels, narrow top to wide base, description to the right.",
           "Flows and structures")
def pyramid(t, x, y, w, h, *, levels=[
        {"title": "Vision", "text": "Top-quartile returns by FY28"},
        {"title": "Strategy", "text": "Three growth bets, one cost program"},
        {"title": "Capabilities", "text": "Pricing, digital sales, lean operations"},
        {"title": "Foundations", "text": "Data platform, talent model, governance"}]):
    k = len(levels)
    _need(2 <= k <= 6, "pyramid takes 2-6 levels")
    pw = min(w * 0.46, h * 1.3)
    gap = 6
    lh_ = (h - gap * (k - 1)) / k
    out = []
    for i, lv in enumerate(levels):
        ly = i * (lh_ + gap)
        # the pyramid's outline x at the top and bottom of this band (apex at the centre)
        half_top = pw / 2 * (ly / h)
        half_bot = pw / 2 * ((ly + lh_) / h)
        inset_top = (pw / 2 - half_top) / pw * 100
        inset_bot = (pw / 2 - half_bot) / pw * 100
        poly = (f"polygon({inset_top:.2f}% 0,{100 - inset_top:.2f}% 0,{100 - inset_bot:.2f}% 100%,"
                f"{inset_bot:.2f}% 100%)")
        share = _ramp(k, i)
        out.append(box(0, ly, pw, lh_, f"background:{cmix(t, 'primary', share)};clip-path:{poly}"))
        out.append(text(t, pw * 0.2, ly, pw * 0.6, lh_, esc(lv.get("title", "")), bold=True, align="center",
                        valign="middle", color="on-primary" if share >= 75 else "ink", size="fs-label", lh=1.1))
        out.append(box(pw / 2 + (half_top + half_bot) / 2 + 12, ly + lh_ / 2, pw + 24 - (pw / 2 + (half_top + half_bot) / 2 + 12), 1,
                       f"border-top:1px dashed {t.v('grey-4')}"))
        out.append(text(t, pw + 32, ly, w - pw - 32, lh_, esc(lv.get("text", "")), valign="middle"))
    return _wrap("Pyramid", x, y, w, h, "".join(out))


@component("funnel", "3-6 narrowing stages with the value inside and a note to the right.", "Flows and structures")
def funnel(t, x, y, w, h, *, stages=[
        {"label": "Addressable market", "value": "USD 48bn", "note": "All segments, 12 countries"},
        {"label": "Serviceable market", "value": "USD 19bn", "note": "Mid-market and enterprise"},
        {"label": "Target share", "value": "USD 4.2bn", "note": "Where we win on price and service"},
        {"label": "FY28 revenue", "value": "USD 0.9bn", "note": "22% of target share"}]):
    k = len(stages)
    _need(2 <= k <= 6, "funnel takes 2-6 stages")
    fw = w * 0.56
    gap = 6
    sh = (h - gap * (k - 1)) / k
    shrink = fw * 0.5 / k
    out = []
    for i, s in enumerate(stages):
        sy = i * (sh + gap)
        l0, l1 = shrink * i / 2, shrink * (i + 1) / 2
        poly = f"polygon({l0 / fw * 100:.2f}% 0,{100 - l0 / fw * 100:.2f}% 0,{100 - l1 / fw * 100:.2f}% 100%,{l1 / fw * 100:.2f}% 100%)"
        share = _ramp(k, i)
        ink = "on-primary" if share >= 75 else "ink"
        out.append(box(0, sy, fw, sh, f"background:{cmix(t, 'primary', share)};clip-path:{poly}"))
        out.append(text(t, l1 + 16, sy, fw - 2 * l1 - 32, sh,
                        f"{esc(s.get('label', ''))}<br><b>{esc(s.get('value', ''))}</b>", align="center",
                        valign="middle", color=ink, size="fs-label", lh=1.15))
        out.append(text(t, fw + 24, sy, w - fw - 24, sh, esc(s.get("note", "")), valign="middle", color="ink-2",
                        style=f"border-left:2px solid {t.v('grey-2')};padding-left:12px"))
    return _wrap("Funnel", x, y, w, h, "".join(out))


@component("hub_spoke", "a centre circle linked to 3-8 surrounding boxes.", "Flows and structures")
def hub_spoke(t, x, y, w, h, *, center="Customer", nodes=[
        {"title": "Pricing", "text": "Value-based tiers"}, {"title": "Channels", "text": "Direct + 3 partners"},
        {"title": "Service", "text": "24h response SLA"}, {"title": "Product", "text": "Modular platform"},
        {"title": "Data", "text": "One customer record"}, {"title": "People", "text": "Pod-based teams"}]):
    k = len(nodes)
    _need(3 <= k <= 8, "hub_spoke takes 3-8 nodes")
    cx, cy = w / 2, h / 2
    d = min(150.0, h * 0.34)
    bw, bh = min(240.0, w * 0.22), 70.0
    rx, ry = (w - bw) / 2, (h - bh) / 2
    lines, boxes = [], []
    for i, nd in enumerate(nodes):
        a = -math.pi / 2 + 2 * math.pi * i / k
        nx, ny = cx + rx * math.cos(a), cy + ry * math.sin(a)
        lines.append(f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{nx:.1f}" y2="{ny:.1f}" stroke="{t.hex("grey-3")}" '
                     f'stroke-width="1.5"/>')
        boxes.append(box(nx - bw / 2, ny - bh / 2, bw, bh, f"background:{t.v('bg')};border:1px solid "
                         f"{t.v('primary')}"))
        boxes.append(text(t, nx - bw / 2 + 10, ny - bh / 2, bw - 20, bh,
                          f"<b>{esc(nd.get('title', ''))}</b><br>{esc(nd.get('text', ''))}", size="fs-label",
                          valign="middle", align="center", lh=1.2))
    hub = text(t, cx - d / 2, cy - d / 2, d, d, esc(center), bold=True, align="center", valign="middle",
               color="on-primary", style=f"background:{t.v('primary')};border-radius:50%;padding:12px")
    return _wrap("Hub and spoke", x, y, w, h, svg(0, 0, w, h, "".join(lines)) + "".join(boxes) + hub)


@component("issue_tree", "root -> 2-4 branches -> leaves, elbow connectors (driver/issue tree).",
           "Flows and structures")
def issue_tree(t, x, y, w, h, *, root="How do we lift EBIT by USD 50m by FY28?", branches=[
        {"title": "Grow revenue", "leaves": ["Price realisation", "Share in mid-market"]},
        {"title": "Cut direct cost", "leaves": ["Procurement", "Plant footprint", "Yield"]},
        {"title": "Cut overhead", "leaves": ["Shared services", "Spans and layers"]}]):
    k = len(branches)
    _need(2 <= k <= 5, "issue_tree takes 2-5 branches")
    total = sum(max(1, len(b.get("leaves", []))) for b in branches)
    rw, bw = w * 0.24, w * 0.24
    gap = 48
    lx = rw + gap + bw + gap
    lw = w - lx
    slot = h / total
    lh_ = min(40.0, slot - 8)
    out, segs = [], []
    col = t.hex("grey-4")
    rtop = h / 2 - 50
    out.append(text(t, 0, rtop, rw, 100, esc(root), bold=True, color="on-primary", valign="middle",
                    style=f"background:{t.v('primary')};padding:12px"))
    idx = 0
    bus1 = rw + gap / 2
    ys = []
    for b in branches:
        leaves = b.get("leaves", []) or [""]
        first, last = idx, idx + len(leaves) - 1
        by = (first + last + 1) / 2 * slot
        ys.append(by)
        out.append(text(t, rw + gap, by - 26, bw, 52, esc(b.get("title", "")), bold=True, valign="middle",
                        style=f"background:{t.v('primary-tint')};padding-left:12px"))
        bus2 = rw + gap + bw + gap / 2
        lys = []
        for leaf in leaves:
            ly = (idx + 0.5) * slot
            lys.append(ly)
            out.append(text(t, lx, ly - lh_ / 2, lw, lh_, esc(leaf), valign="middle", size="fs-label",
                            style=f"background:{t.v('grey-1')};padding-left:10px"))
            segs.append(f'<line x1="{bus2}" y1="{ly:.1f}" x2="{lx}" y2="{ly:.1f}" stroke="{col}" stroke-width="1.5"/>')
            idx += 1
        segs.append(f'<line x1="{rw + gap + bw}" y1="{by:.1f}" x2="{bus2}" y2="{by:.1f}" stroke="{col}" stroke-width="1.5"/>')
        if len(lys) > 1:
            segs.append(f'<line x1="{bus2}" y1="{lys[0]:.1f}" x2="{bus2}" y2="{lys[-1]:.1f}" stroke="{col}" stroke-width="1.5"/>')
    segs.append(f'<line x1="{rw}" y1="{h / 2:.1f}" x2="{bus1}" y2="{h / 2:.1f}" stroke="{col}" stroke-width="1.5"/>')
    segs.append(f'<line x1="{bus1}" y1="{min(ys + [h / 2]):.1f}" x2="{bus1}" y2="{max(ys + [h / 2]):.1f}" stroke="{col}" stroke-width="1.5"/>')
    for by in ys:
        segs.append(f'<line x1="{bus1}" y1="{by:.1f}" x2="{rw + gap}" y2="{by:.1f}" stroke="{col}" stroke-width="1.5"/>')
    return _wrap("Issue tree", x, y, w, h, svg(0, 0, w, h, "".join(segs)) + "".join(out))


@component("org_tiers", "governance / org tiers: left rail naming tiers, boxes per tier, connectors.",
           "Flows and structures")
def org_tiers(t, x, y, w, h, *, tiers=[
        {"label": "Steering", "boxes": ["Steering committee (CEO, CFO, COO)"]},
        {"label": "Program", "boxes": ["Transformation office"]},
        {"label": "Delivery", "boxes": ["Procurement", "Operations", "Commercial", "Support functions"]}],
        highlight=1):
    k = len(tiers)
    _need(2 <= k <= 5, "org_tiers takes 2-5 tiers")
    rail = 150
    gap_y = 36
    th = (h - gap_y * (k - 1)) / k
    bh = min(64.0, th)
    area = w - rail - 24
    out, segs = [], []
    col = t.hex("grey-4")
    centres: list[list[float]] = []
    for i, tier in enumerate(tiers):
        ty = i * (th + gap_y) + (th - bh) / 2
        out.append(text(t, 0, ty, rail, bh, esc(tier.get("label", "")), bold=True, valign="middle",
                        size="fs-label", color="ink-2", style=f"border-right:2px solid {t.v('grey-2')};padding-right:8px"))
        bs = tier.get("boxes", []) or [""]
        gap = 16
        bw = min(260.0, (area - gap * (len(bs) - 1)) / len(bs))
        span = bw * len(bs) + gap * (len(bs) - 1)
        x0 = rail + 24 + (area - span) / 2
        cs = []
        for j, label in enumerate(bs):
            bx = x0 + j * (bw + gap)
            cs.append(bx + bw / 2)
            on = i == highlight
            style = (f"background:{t.v('primary')}" if i == 0 else
                     f"background:{t.v('bg')};border:{'2px' if on else '1px'} solid {t.v('accent') if on else t.v('primary')}")
            out.append(text(t, bx, ty, bw, bh, esc(label), bold=i == 0, align="center", valign="middle",
                            size="fs-label", color="on-primary" if i == 0 else "ink", style=style + ";padding:4px 8px"))
        centres.append([ty, ty + bh, *cs])
    for i in range(k - 1):
        top, below = centres[i], centres[i + 1]
        mid = (top[1] + below[0]) / 2
        src = (top[2] + top[-1]) / 2
        segs.append(f'<line x1="{src:.1f}" y1="{top[1]:.1f}" x2="{src:.1f}" y2="{mid:.1f}" stroke="{col}" stroke-width="1.5"/>')
        xs = below[2:]
        segs.append(f'<line x1="{min(xs + [src]):.1f}" y1="{mid:.1f}" x2="{max(xs + [src]):.1f}" y2="{mid:.1f}" stroke="{col}" stroke-width="1.5"/>')
        for cx in xs:
            segs.append(f'<line x1="{cx:.1f}" y1="{mid:.1f}" x2="{cx:.1f}" y2="{below[0]:.1f}" stroke="{col}" stroke-width="1.5"/>')
    return _wrap("Org tiers", x, y, w, h, svg(0, 0, w, h, "".join(segs)) + "".join(out))


# ==================================================================================== time
@component("gantt", "roadmap: activity rail, period bars, milestone diamonds, optional today line.",
           "Time", hint="start/end/milestone/today are period positions (0 = start of the first period; "
           "fractions allowed); group labels a phase above the row.")
def gantt(t, x, y, w, h, *, periods=["Q1 26", "Q2 26", "Q3 26", "Q4 26", "Q1 27", "Q2 27"], rows=[
        {"label": "Diagnose and baseline", "start": 0, "end": 1, "group": "Phase 1"},
        {"label": "Design target model", "start": 1, "end": 2.5, "milestone": 2.5, "note": "SteerCo sign-off"},
        {"label": "Pilot in two plants", "start": 2, "end": 4, "group": "Phase 2"},
        {"label": "Network roll-out", "start": 3.5, "end": 6, "milestone": 6, "note": "Run-rate", "highlight": True}],
        today=None):
    kp = len(periods)
    _need(1 <= kp <= 24 and 1 <= len(rows) <= 14, "gantt takes 1-24 periods and 1-14 rows")
    rail = min(300.0, w * 0.3)
    head = 32
    pw = (w - rail) / kp
    rh = min(52.0, (h - head) / len(rows))
    out = []
    for i, p in enumerate(periods):
        px = rail + i * pw
        if i % 2 == 0:
            out.append(box(px, head, pw, rh * len(rows), f"background:{t.v('grey-1')}"))
        out.append(text(t, px, 0, pw, head - 6, esc(p), bold=True, size="fs-label", align="center", valign="bottom",
                        color="ink-2", lh=1))
    out.append(hrule(t, 0, head - 1, w, colour="ink"))
    for j, r in enumerate(rows):
        ry = head + j * rh
        label = esc(r.get("label", ""))
        if r.get("group"):
            label = f'<span style="color:{t.v("ink-2")};font-size:{t.v("fs-source")}">{esc(r["group"])}</span><br>' + label
        out.append(text(t, 0, ry, rail - 12, rh, label, valign="middle", size="fs-label", lh=1.15))
        out.append(hrule(t, 0, ry + rh - 1, w, colour="grey-2"))
        s, e = float(r.get("start", 0)), float(r.get("end", 1))
        bar_h = 14
        colour = "accent" if r.get("highlight") else "primary"
        out.append(box(rail + s * pw + 2, ry + (rh - bar_h) / 2, max(4.0, (e - s) * pw - 4), bar_h,
                       f"background:{t.v(colour)};border-radius:2px"))
        if r.get("milestone") is not None:
            dsz = 14
            mx = min(w - dsz / 2 - 3, rail + float(r["milestone"]) * pw)
            out.append(box(mx - dsz / 2 - 2, ry + (rh - dsz) / 2, dsz, dsz,
                           f"background:{t.v('ink')};transform:rotate(45deg)"))
            if r.get("note"):
                right = mx + 12 + 150 <= w
                out.append(text(t, mx + 12 if right else mx - 162, ry, 150, (rh - bar_h) / 2 - 1, esc(r["note"]),
                                size="fs-source", color="ink-2", valign="bottom", align="left" if right else "right",
                                lh=1.1))
    if today is not None:
        tx = rail + float(today) * pw
        out.append(box(tx, head, 0, rh * len(rows), f"border-left:1.5px dashed {t.v('accent')}"))
        out.append(text(t, tx - 40, rh * len(rows) + head + 2, 80, 16, "Today", size="fs-source", bold=True,
                        align="center", color="ink-2", lh=1))
    return _wrap("Roadmap", x, y, w, h, "".join(out))


@component("timeline", "dated milestones on one axis, labels alternating above and below.", "Time")
def timeline(t, x, y, w, h, *, milestones=[
        {"date": "Jan 2026", "title": "Kick-off", "text": "Baseline agreed"},
        {"date": "Apr 2026", "title": "Design", "text": "Target model approved"},
        {"date": "Jul 2026", "title": "Pilot", "text": "Two plants live"},
        {"date": "Oct 2026", "title": "Scale", "text": "Network roll-out"},
        {"date": "Mar 2027", "title": "Run-rate", "text": "USD 50m EBIT"}], highlight=4):
    k = len(milestones)
    _need(2 <= k <= 9, "timeline takes 2-9 milestones")
    axis = h / 2
    slot = w / k
    out = [hrule(t, 0, axis - 1, w, colour="grey-4", weight=2)]
    for i, m in enumerate(milestones):
        mx = slot * (i + 0.5)
        on = i == highlight
        d = 16
        out.append(box(mx - d / 2, axis - d / 2, d, d, f"border-radius:50%;background:{t.v(t.strong()) if on else t.v('primary')};"
                       f"border:3px solid {t.v('bg')}"))
        above = i % 2 == 0
        lw = slot - 12
        stem_h = 28
        out.append(vrule(t, mx, axis - stem_h - d / 2 if above else axis + d / 2, stem_h, colour="grey-3"))
        content = (f'<b style="color:{t.v("primary")}">{esc(m.get("date", ""))}</b><br>'
                   f'<b>{esc(m.get("title", ""))}</b><br>{esc(m.get("text", ""))}')
        ty = 0 if above else axis + d / 2 + stem_h + 6
        th = axis - d / 2 - stem_h - 6 if above else h - ty
        out.append(text(t, mx - lw / 2, ty, lw, th, content, size="fs-label", align="center",
                        valign="bottom" if above else "top", lh=1.2))
    return _wrap("Timeline", x, y, w, h, "".join(out))


# ==================================================================================== matrices
@component("matrix_2x2", "2x2 with axis arrows, quadrant labels, target quadrant tinted, dots.",
           "Matrices and tables")
def matrix_2x2(t, x, y, w, h, *, x_label="Market attractiveness", y_label="Ability to win",
               quadrants=["Build capability", "Invest to lead", "Deprioritise", "Harvest or partner"],
               target="tr", dots=[
                   {"label": "Segment A", "x": 0.78, "y": 0.8, "highlight": True},
                   {"label": "Segment B", "x": 0.62, "y": 0.35}, {"label": "Segment C", "x": 0.3, "y": 0.66},
                   {"label": "Segment D", "x": 0.2, "y": 0.22}]):
    _need(len(quadrants) == 4, "quadrants is [top-left, top-right, bottom-left, bottom-right]")
    _need(target in ("tl", "tr", "bl", "br", ""), "target is tl, tr, bl, br or ''")
    ax = 36
    gx, gy, gw, gh = ax, 0, w - ax, h - ax
    qw, qh = (gw - 4) / 2, (gh - 4) / 2
    out = []
    for i, (key, q) in enumerate(zip(("tl", "tr", "bl", "br"), quadrants, strict=False)):
        qx = gx + (i % 2) * (qw + 4)
        qy = gy + (i // 2) * (qh + 4)
        on = key == target
        out.append(box(qx, qy, qw, qh, f"background:{t.v('primary-tint') if on else t.v('grey-1')}"))
        if q:
            out.append(text(t, qx + 12, qy + 10, qw - 24, 24, esc(q), bold=True, size="fs-label",
                            color="primary" if on else "ink-2", align="right" if i % 2 else "left", lh=1.1))
    mid = uid("mx", {"x": x_label, "y": y_label})
    ink = t.hex("ink-2")
    axes = (arrow_defs(mid, ink)
            + f'<line x1="{gx - 8}" y1="{gh + 8}" x2="{gx - 8}" y2="4" stroke="{ink}" stroke-width="1.5" marker-end="url(#{mid})"/>'
            + f'<line x1="{gx - 8}" y1="{gh + 8}" x2="{w - 4}" y2="{gh + 8}" stroke="{ink}" stroke-width="1.5" marker-end="url(#{mid})"/>')
    out.append(svg(0, 0, w, h, axes))
    if x_label:
        out.append(text(t, gx, gh + 14, gw, 20, esc(x_label), size="fs-label",
                        bold=True, align="center", color="ink-2", lh=1))
    if y_label:
        out.append(svg(0, 0, ax, gh, f'<text x="{-gh / 2:.1f}" y="14" transform="rotate(-90)" text-anchor="middle" '
                   f'style="font-size:{t.v("fs-label")};font-weight:700;fill:{t.v("ink-2")}">{esc(y_label)}</text>'))
    for dt in dots:
        px = gx + float(dt.get("x", 0.5)) * gw
        py = gy + (1 - float(dt.get("y", 0.5))) * gh
        on = bool(dt.get("highlight"))
        d = 18 if on else 14
        out.append(box(px - d / 2, py - d / 2, d, d, f"border-radius:50%;background:{t.v(t.strong()) if on else t.v('primary')};"
                       f"border:2px solid {t.v('bg')}"))
        out.append(text(t, px + d / 2 + 6, py - 10, 180, 20, esc(dt.get("label", "")), size="fs-label", bold=on,
                        valign="middle", lh=1))
    return _wrap("2x2 matrix", x, y, w, h, "".join(out))


def _grid_head(t, cols, x0, cw, head, *, align="center"):
    return "".join(text(t, x0 + i * cw, 0, cw, head, esc(c), bold=True, size="fs-label", align=align,
                        valign="bottom", lh=1.15, style="padding:0 4px") for i, c in enumerate(cols))


@component("harvey_table", "options x criteria scored with Harvey balls (0-4), a legend, a highlighted row.",
           "Matrices and tables")
def harvey_table(t, x, y, w, h, *, criteria=["Cost", "Speed", "Risk", "Scalability", "Capability fit"], rows=[
        {"label": "Build in-house", "scores": [2, 1, 3, 2, 4]},
        {"label": "Acquire target", "scores": [1, 4, 2, 4, 3], "highlight": True},
        {"label": "Partner / JV", "scores": [3, 3, 2, 2, 2]},
        {"label": "License", "scores": [4, 3, 1, 1, 1]}], legend=True):
    kc = len(criteria)
    _need(1 <= kc <= 10 and 1 <= len(rows) <= 12, "harvey_table takes 1-10 criteria and 1-12 rows")
    for r in rows:
        _need(len(r.get("scores", [])) == kc, "every row needs one score per criterion")
    rail = min(280.0, w * 0.3)
    head = 44
    foot = 28 if legend else 0
    cw = (w - rail) / kc
    rh = min(56.0, (h - head - foot) / len(rows))
    out = [_grid_head(t, criteria, rail, cw, head - 6), hrule(t, 0, head - 1, w, colour="ink")]
    for j, r in enumerate(rows):
        ry = head + j * rh
        if r.get("highlight"):
            out.append(box(0, ry, w, rh, f"background:{t.v('primary-tint')}"))
        out.append(text(t, 8, ry, rail - 16, rh, esc(r.get("label", "")), bold=bool(r.get("highlight")),
                        valign="middle"))
        for i, s in enumerate(r["scores"]):
            out.append(harvey(t, rail + (i + 0.5) * cw, ry + rh / 2, s))
        out.append(hrule(t, 0, ry + rh - 1, w, colour="grey-2"))
    if legend:
        ly = head + rh * len(rows) + 10
        items = [(0, "None"), (1, "Low"), (2, "Medium"), (3, "High"), (4, "Full")]
        lx = w - 5 * 96
        for i, (s, label) in enumerate(items):
            out.append(harvey(t, lx + i * 96 + 8, ly + 8, s, r=7))
            out.append(text(t, lx + i * 96 + 22, ly, 70, 16, label, size="fs-source", color="ink-2", lh=1.3))
    return _wrap("Harvey matrix", x, y, w, h, "".join(out))


@component("check_matrix", "presence matrix: rows x columns of checks (1), partial (0.5) or dashes (0).",
           "Matrices and tables")
def check_matrix(t, x, y, w, h, *, columns=["Us", "Peer A", "Peer B", "Peer C", "Peer D"], rows=[
        {"label": "Same-day delivery", "marks": [1, 1, 0, 0.5, 0]},
        {"label": "Self-service portal", "marks": [0.5, 1, 1, 1, 0]},
        {"label": "Usage-based pricing", "marks": [0, 1, 0, 0, 1]},
        {"label": "24/7 support", "marks": [1, 1, 1, 0, 0.5]},
        {"label": "Open API", "marks": [0, 1, 0.5, 1, 0]}], highlight_column=0):
    kc = len(columns)
    _need(1 <= kc <= 12 and 1 <= len(rows) <= 14, "check_matrix takes 1-12 columns and 1-14 rows")
    for r in rows:
        _need(len(r.get("marks", [])) == kc, "every row needs one mark per column")
    rail = min(300.0, w * 0.32)
    head = 40
    cw = (w - rail) / kc
    rh = min(48.0, (h - head - 24) / len(rows))
    out = []
    if highlight_column is not None and 0 <= highlight_column < kc:
        out.append(box(rail + highlight_column * cw, 0, cw, head + rh * len(rows), f"background:{t.v('primary-tint')}"))
    out += [_grid_head(t, columns, rail, cw, head - 6), hrule(t, 0, head - 1, w, colour="ink")]
    for j, r in enumerate(rows):
        ry = head + j * rh
        out.append(text(t, 8, ry, rail - 16, rh, esc(r.get("label", "")), valign="middle"))
        for i, m in enumerate(r["marks"]):
            out.append(check(t, rail + (i + 0.5) * cw, ry + rh / 2, m))
        out.append(hrule(t, 0, ry + rh - 1, w, colour="grey-2"))
    ly = head + rh * len(rows) + 8
    out.append(check(t, w - 300, ly + 8, 1, s=14))
    out.append(text(t, w - 288, ly, 80, 16, "Offered", size="fs-source", color="ink-2", lh=1.3))
    out.append(check(t, w - 200, ly + 8, 0.5, s=14))
    out.append(text(t, w - 188, ly, 80, 16, "Partial", size="fs-source", color="ink-2", lh=1.3))
    out.append(check(t, w - 100, ly + 8, 0, s=14))
    out.append(text(t, w - 88, ly, 88, 16, "Not offered", size="fs-source", color="ink-2", lh=1.3))
    return _wrap("Check matrix", x, y, w, h, "".join(out))


def _table(t, x, y, w, rows_html: str, name: str, cols: list[float] | None = None) -> str:
    colgroup = ""
    if cols:
        colgroup = "<colgroup>" + "".join(f'<col style="width:{n(c)}px">' for c in cols) + "</colgroup>"
    return (f'<table data-name="{name}" style="{pos(x, y, w)};border-collapse:collapse;table-layout:fixed;'
            f'font-size:{t.v("fs-label")};line-height:1.2;color:{t.v("ink")}">{colgroup}{rows_html}</table>')


def _cell(content, *, tag="td", align="left", bold=False, bg=None, color=None, top=None, bottom=None,
          pad="6px 8px", extra="") -> str:
    style = [f"text-align:{align}", f"padding:{pad}", "vertical-align:middle"]
    if bold:
        style.append("font-weight:700")
    if bg:
        style.append(f"background:{bg}")
    if color:
        style.append(f"color:{color}")
    if top:
        style.append(f"border-top:{top}")
    if bottom:
        style.append(f"border-bottom:{bottom}")
    if extra:
        style.append(extra)
    return f'<{tag} style="{";".join(style)}">{content}</{tag}>'


@component("rag_table", "status table: text columns plus a RAG status cell (R/A/G) per row.",
           "Matrices and tables")
def rag_table(t, x, y, w, h, *, columns=["Workstream", "Owner", "Status", "Next milestone"],
              status_column=2, rows=[
                  {"cells": ["Procurement", "J. Smith", "G", "Tender awarded, 15 Oct"]},
                  {"cells": ["Plant footprint", "A. Rossi", "A", "Works council, 2 Nov"]},
                  {"cells": ["Shared services", "M. Chen", "R", "Vendor re-selection, 30 Nov"]},
                  {"cells": ["Pricing", "K. Patel", "G", "New list prices, 1 Jan"]}]):
    kc = len(columns)
    _need(1 <= kc <= 8, "rag_table takes 1-8 columns")
    if status_column is None:   # the column headed "Status", if any; otherwise a plain table
        status_column = next((i for i, c in enumerate(columns) if "status" in str(c).lower()), None)
    words ={"R": "Off track", "A": "At risk", "G": "On track"}
    colours = {"R": t.v("rag-r"), "A": t.v("rag-a"), "G": t.v("rag-g")}
    rule = f"1px solid {t.v('grey-2')}"
    head = "<tr>" + "".join(_cell(esc(c), tag="th", bold=True, bottom=f"1px solid {t.v('ink')}",
                                  align="center" if i == status_column else "left") for i, c in enumerate(columns)) + "</tr>"
    body = ""
    for r in rows:
        cells = r.get("cells", [])
        _need(len(cells) == kc, "every row needs one cell per column")
        tds = []
        for i, c in enumerate(cells):
            if i == status_column:
                key = str(c).upper()[:1]
                _need(key in words, "status cells are R, A or G")
                tds.append(_cell(words[key], align="center", bold=True, bg=colours[key], color="#FFFFFF",
                                 bottom=f"3px solid {t.v('bg')}"))
            else:
                tds.append(_cell(esc(c), bottom=rule, bold=i == 0))
        body += "<tr>" + "".join(tds) + "</tr>"
    cols = [w / kc] * kc
    if status_column is not None and 0 <= status_column < kc:
        cols = [(w - 130) / (kc - 1) if i != status_column else 130 for i in range(kc)] if kc > 1 else [w]
    return _table(t, x, y, w, head + body, "RAG table", cols)


@component("heatmap_table", "numeric grid, cells shaded by value (darker = higher).",
           "Matrices and tables")
def heatmap_table(t, x, y, w, h, *, columns=["North", "South", "East", "West", "Central"], rows=[
        {"label": "Retail", "values": [12.1, 8.4, 15.3, 6.2, 9.9]},
        {"label": "Wholesale", "values": [4.3, 11.8, 7.7, 13.2, 5.1]},
        {"label": "Online", "values": [18.6, 14.2, 9.5, 11.1, 16.4]},
        {"label": "Services", "values": [3.2, 5.6, 4.1, 2.8, 7.3]}], number="0.0", unit="Growth, % p.a."):
    kc = len(columns)
    _need(1 <= kc <= 10, "heatmap_table takes 1-10 columns")
    vals = [float(v) for r in rows for v in r.get("values", []) if isinstance(v, (int, float))]
    lo, hi = (min(vals), max(vals)) if vals else (0.0, 1.0)
    head = "<tr>" + _cell(esc(unit), tag="th", bold=True, bottom=f"1px solid {t.v('ink')}", color=t.v("ink-2")) + "".join(
        _cell(esc(c), tag="th", bold=True, align="center", bottom=f"1px solid {t.v('ink')}") for c in columns) + "</tr>"
    body = ""
    for r in rows:
        values = r.get("values", [])
        _need(len(values) == kc, "every row needs one value per column")
        tds = [_cell(esc(r.get("label", "")), bold=True, bottom=f"2px solid {t.v('bg')}")]
        for v in values:
            share = 10 + 80 * ((float(v) - lo) / (hi - lo) if hi > lo and isinstance(v, (int, float)) else 0)
            ink = t.v("on-primary") if share >= 55 else t.v("ink")
            tds.append(_cell(fmt(v, number), align="center", bg=cmix(t, "primary", share), color=ink,
                             extra=f"border:2px solid {t.v('bg')}"))
        body += "<tr>" + "".join(tds) + "</tr>"
    first = min(260.0, w * 0.25)
    return _table(t, x, y, w, head + body, "Heatmap", [first] + [(w - first) / kc] * kc)


def _num(row: dict[str, Any], key: str, name: str) -> float:
    v = row.get(key)
    _need(isinstance(v, (int, float)) and not isinstance(v, bool), f"{name}: every row needs a numeric `{key}`")
    return float(v) if isinstance(v, (int, float)) else 0.0


def _row_geometry(t: Tok, avail: float, k: int, name: str, *, most: float = 40.0) -> float:
    """A compact row height: at most `most` px, never stretched to fill the box; ValueError when the rows
    cannot fit at a legible height (a row carries two labelled bars, one fs-source label per bar)."""
    lab = math.ceil(t.px("fs-source"))
    least = max(30.0, (lab - 4) / 0.29 + 1)
    rh = min(most, avail / k)
    _need(rh >= least, f"{name}: {k} rows do not fit in {n(avail)}px at {n(least)}px a row; raise h or cut rows")
    return rh


@component("bar_table", "benchmark table: subject vs benchmark bars per metric, end labels, gap badges.", "Matrices and tables",
           hint="rows: value (subject), benchmark, lower_is_better, format (0, 0.0, %, x, $), gap (text to "
           "override). scale row|column; highlight worse|better|none colours the subject bar; gap abs|pct|none.")
def bar_table(t, x, y, w, h, *, rows=[
        {"label": "Cost per order, USD", "value": 14.2, "benchmark": 11.8, "lower_is_better": True},
        {"label": "On-time delivery", "value": 91.0, "benchmark": 95.0, "format": "%"},
        {"label": "Inventory days", "value": 38, "benchmark": 52, "lower_is_better": True, "format": "0"},
        {"label": "Revenue per FTE, USD k", "value": 212, "benchmark": 186, "format": "0"},
        {"label": "Customer churn", "value": 7.4, "benchmark": 5.1, "lower_is_better": True, "format": "%"}],
        subject="Target Co", benchmark="Peer median", headings=["Metric", "Gap to median"], scale="row",
        highlight="worse", gap="abs", number="0.0"):
    k = len(rows)
    _need(1 <= k <= 14, "bar_table takes 1-14 rows")
    _need(scale in ("row", "column"), "scale is row or column")
    _need(highlight in ("worse", "better", "none"), "highlight is worse, better or none")
    _need(gap in ("abs", "pct", "none"), "gap is abs, pct or none")
    _need(len(headings) <= 2, "headings is [label column, gap column]")
    vals = [(_num(r, "value", "bar_table"), _num(r, "benchmark", "bar_table")) for r in rows]
    _need(all(v >= 0 and b >= 0 for v, b in vals), "bar_table draws values >= 0 (bars start at zero)")
    rail = min(280.0, w * 0.28)
    gw = 124.0 if gap != "none" else 0.0
    lab_w = 72.0
    bx0 = rail + 12
    bar_w = w - bx0 - gw - lab_w
    _need(bar_w >= 80, "bar_table: the box is too narrow for the bars")
    flagged = []
    for r, (v, b) in zip(rows, vals, strict=False):
        low = bool(r.get("lower_is_better"))
        worse = v > b if low else v < b
        better = v < b if low else v > b
        flagged.append((highlight == "worse" and worse) or (highlight == "better" and better))
    # The flagged bars take the gap badges' colour for the same reading (red worse, green better), so one
    # meaning has one colour and the master's accent stays free for the slide's takeaway.
    flag = t.v("rag-r" if highlight == "worse" else "rag-g")
    legend_items = [(t.v("primary"), subject)] if subject else []
    if benchmark:
        legend_items.append((t.v("grey-3"), benchmark))
    if highlight != "none" and any(flagged):
        legend_items.append((flag, f"{highlight.capitalize()} than {benchmark or 'benchmark'}"))
    head = 30.0 if (headings or legend_items) else 0.0
    rh = _row_geometry(t, h - head, k, "bar_table")
    lab_h = math.ceil(t.px("fs-source"))
    b1, b2, bgap = round(rh * 0.34), round(rh * 0.24), 4
    pad = (rh - b1 - b2 - bgap) / 2
    col_max = max(max(v, b) for v, b in vals) or 1.0
    out = []
    if head:
        if headings and headings[0]:
            out.append(text(t, 8, 0, rail - 16, head - 6, esc(headings[0]), bold=True, size="fs-label",
                            valign="bottom", lh=1.15))
        if gw and len(headings) > 1 and headings[1]:
            out.append(text(t, w - gw + 8, 0, gw - 8, head - 6, esc(headings[1]), bold=True, size="fs-label",
                            valign="bottom", lh=1.15))
        if legend_items:
            out.append(_swatches(t, bx0, head - 22, 16, legend_items)[0])
        out.append(hrule(t, 0, head - 1, w, colour="ink"))
    for j, (r, (v, b), on) in enumerate(zip(rows, vals, flagged, strict=False)):
        ry = head + j * rh
        kind = str(r.get("format") or number)
        scale_max = col_max if scale == "column" else (max(v, b) or 1.0)
        out.append(text(t, 8, ry, rail - 16, rh, esc(r.get("label", "")), valign="middle", lh=1.15))
        for top, bh, val, fill, strong in ((pad, b1, v, flag if on else t.v("primary"), True),
                                           (pad + b1 + bgap, b2, b, t.v("grey-3"), False)):
            length = max(1.5, bar_w * val / scale_max) if val else 0
            if length:
                out.append(box(bx0, ry + top, length, bh, f"background:{fill}"))
            out.append(text(t, bx0 + length + 4, ry + top + bh / 2 - lab_h / 2, lab_w - 4, lab_h, fmt(val, kind),
                            size="fs-source", bold=strong, color="ink" if strong else "ink-2", lh=1,
                            style="white-space:nowrap"))
        if gw:
            delta = v - b
            if r.get("gap"):
                label = str(r["gap"])
            elif gap == "pct":
                label = _signed(100 * delta / b, "pct") if b else "n.a."
            else:
                label = _signed(delta, kind)
            good = "down" if r.get("lower_is_better") else "up"
            out.append(_delta(t, w - gw + 8, ry, gw - 8, rh, label, _direction(delta), good, size="fs-dense"))
        out.append(hrule(t, 0, ry + rh - 1, w, colour="grey-2"))
    return _wrap("Bar table", x, y, w, h, "".join(out))


@component("comparison_columns","2-4 options side by side on shared rows, winner flagged, verdict band.",
           "Matrices and tables")
def comparison_columns(t, x, y, w, h, *, options=["Option A: Organic", "Option B: Acquire", "Option C: Partner"],
                       row_labels=["Value, NPV", "Time to impact", "Execution risk"],
                       cells=[["USD 120m", "USD 210m", "USD 90m"], ["36 months", "12 months", "18 months"],
                              ["Low", "Medium", "Medium"]],
                       recommended=1, verdict="Acquire: highest value and fastest impact; risk is manageable with a "
                                             "100-day integration plan"):
    k = len(options)
    _need(2 <= k <= 5, "comparison_columns takes 2-5 options")
    _need((not row_labels or len(cells) == len(row_labels)) and all(len(r) == k for r in cells),
          "cells is one list per row (per row label, when given), one entry per option")
    row_labels = list(row_labels) or [""] * len(cells)
    rail = min(200.0, w * 0.2) if row_labels else 0
    gap = 12
    cw = (w - rail - gap * (k - 1)) / k
    top = 14 if recommended is not None and 0 <= recommended < k else 0
    head = 52
    band = 56 if verdict else 0
    rh = (h - top - head - band - (16 if verdict else 0)) / max(1, len(row_labels))
    out = []
    tags: list[str] = []
    for j, label in enumerate(row_labels):
        ry = head + j * rh
        if label:
            out.append(text(t, 0, ry, rail - 12, rh, esc(label), bold=True, size="fs-label", valign="middle", color="ink-2"))
    for i, opt in enumerate(options):
        cx = rail + i * (cw + gap)
        on = i == recommended
        out.append(box(cx, 0, cw, head + rh * len(row_labels),
                       f"border:{'2px solid ' + t.v('accent') if on else '1px solid ' + t.v('grey-2')}"))
        out.append(text(t, cx, 0, cw, head, esc(opt), bold=True, align="center", valign="middle",
                        color="on-primary" if on else "ink", style=f"background:{t.v('primary') if on else t.v('grey-1')};padding:0 8px"))
        if on:
            tags.append(text(t, cx + cw - 118, top - 12, 110, 22, "Recommended", bold=True, size="fs-source", align="center",
                            valign="middle", color="on-accent", lh=1, style=f"background:{t.v('accent')}"))
        for j in range(len(row_labels)):
            ry = head + j * rh
            if j:
                out.append(hrule(t, cx + 12, ry, cw - 24, colour="grey-2"))
            out.append(text(t, cx + 12, ry, cw - 24, rh, esc(cells[j][i]), align="center", valign="middle", bold=on))
    inner = "".join(out)
    out = [f'<div style="{pos(0, top, w, h - top)}">{inner}</div>', *tags]
    if verdict:
        out.append(text(t, 0, h - band, w, band, esc(verdict), bold=True, valign="middle", color="on-primary",
                        style=f"background:{t.v('primary')};padding:0 16px"))
    return _wrap("Comparison", x, y, w, h, "".join(out))


# ==================================================================================== finance tables
@component("comps_table", "trading/transaction comps: subject row tinted, median/mean rows.",
           "Finance tables", hint="Column fmt: text, 0, 0.0, 0.00, x (multiple), % or $; negatives print in "
           "parentheses, None prints n.a.; statistics exclude the subject row.")
def comps_table(t, x, y, w, h, *, columns=[
        {"label": "Company", "fmt": "text"}, {"label": "Mkt cap (USD bn)", "fmt": "0.0"},
        {"label": "EV (USD bn)", "fmt": "0.0"}, {"label": "EV / EBITDA LTM", "fmt": "x"},
        {"label": "EV / EBITDA NTM", "fmt": "x"}, {"label": "P / E NTM", "fmt": "x"},
        {"label": "Revenue growth", "fmt": "%"}, {"label": "EBITDA margin", "fmt": "%"}], rows=[
        {"cells": ["Peer One", 42.1, 48.3, 11.2, 10.1, 18.4, 6.2, 21.5]},
        {"cells": ["Peer Two", 28.7, 33.0, 9.8, 9.1, 15.2, 4.1, 18.9]},
        {"cells": ["Peer Three", 15.2, 19.9, 8.4, 7.9, 12.7, -1.3, 14.2]},
        {"cells": ["Peer Four", 9.6, 12.4, 12.9, 11.3, 22.1, 9.8, 24.0]},
        {"cells": ["Peer Five", 6.3, 8.8, 7.1, 6.8, None, -3.5, 11.6]},
        {"cells": ["Target Co", 4.9, 6.1, 8.9, 8.2, 14.0, 5.5, 17.3], "subject": True}],
        stats=["Median", "Mean"], unit="USD in billions, except multiples and %"):
    kc = len(columns)
    _need(2 <= kc <= 12 and 1 <= len(rows) <= 16, "comps_table takes 2-12 columns and 1-16 rows")
    cols = [c if isinstance(c, dict) else {"label": str(c), "fmt": "text" if i == 0 else "0.0"} for i, c in enumerate(columns)]
    for r in rows:
        _need(len(r.get("cells", [])) == kc, "every row needs one cell per column")
    ink = f"1px solid {t.v('ink')}"
    thin = f"1px solid {t.v('grey-2')}"
    head = "<tr>" + "".join(_cell(esc(c["label"]), tag="th", bold=True, bottom=ink, align="left" if i == 0 else "right",
                                  extra="vertical-align:bottom") for i, c in enumerate(cols)) + "</tr>"
    body = ""
    peers = [r for r in rows if not r.get("subject")]
    for r in rows:
        sub = bool(r.get("subject"))
        tds = []
        for i, (c, v) in enumerate(zip(cols, r["cells"], strict=False)):
            txt = esc(v) if c.get("fmt") == "text" else fmt(v, c.get("fmt", "0.0"))
            tds.append(_cell(txt, align="left" if i == 0 else "right", bold=sub, bottom=thin,
                             bg=t.v("primary-tint") if sub else None, pad="5px 8px"))
        body += "<tr>" + "".join(tds) + "</tr>"
    for si, stat in enumerate(stats):
        tds = [_cell(esc(stat), bold=True, top=ink if si == 0 else None, pad="5px 8px")]
        for c_i, c in enumerate(cols[1:], start=1):
            vals = [float(r["cells"][c_i]) for r in peers if isinstance(r["cells"][c_i], (int, float))]
            if c.get("fmt") == "text" or not vals:
                value = "" if c.get("fmt") == "text" else "n.a."
            elif stat.lower().startswith("med"):
                s_ = sorted(vals)
                m = len(s_) // 2
                value = fmt(s_[m] if len(s_) % 2 else (s_[m - 1] + s_[m]) / 2, c.get("fmt", "0.0"))
            elif stat.lower().startswith("mean") or stat.lower().startswith("average"):
                value = fmt(sum(vals) / len(vals), c.get("fmt", "0.0"))
            elif stat.lower().startswith("high") or stat.lower().startswith("max"):
                value = fmt(max(vals), c.get("fmt", "0.0"))
            elif stat.lower().startswith("low") or stat.lower().startswith("min"):
                value = fmt(min(vals), c.get("fmt", "0.0"))
            else:
                raise ValueError("stats are Median, Mean, High or Low")
            tds.append(_cell(value, align="right", bold=True, top=ink if si == 0 else None, pad="5px 8px"))
        body += "<tr>" + "".join(tds) + "</tr>"
    first = min(220.0, w * 0.22)
    table = _table(t, 0, 22 if unit else 0, w, head + body, "Comps table", [first] + [(w - first) / (kc - 1)] * (kc - 1))
    note = text(t, 0, 0, w, 18, f"({esc(unit)})", size="fs-source", color="ink-2", lh=1.2) if unit else ""
    return _wrap("Comps", x, y, w, h, note + table, group=False)


@component("sensitivity_table", "two-way sensitivity (e.g. WACC x growth), base case highlighted.",
           "Finance tables")
def sensitivity_table(t, x, y, w, h, *, row_label="WACC", col_label="Terminal growth rate",
                      row_values=["8.0%", "8.5%", "9.0%", "9.5%", "10.0%"], col_values=["1.5%", "2.0%", "2.5%", "3.0%"],
                      values=[[48.2, 51.0, 54.3, 58.1], [44.6, 47.0, 49.8, 53.0], [41.5, 43.6, 46.0, 48.7],
                              [38.8, 40.6, 42.7, 45.0], [36.4, 38.0, 39.8, 41.8]],
                      base=[2, 2], number="0.0", caption="Implied share price, USD"):
    _need(len(values) == len(row_values) and all(len(r) == len(col_values) for r in values),
          "values is one list per row value, one entry per column value")
    _need(base is None or len(base) == 2, "base is [row index, column index] or null")
    br, bc = base if base is not None else (-1, -1)
    ink = f"1px solid {t.v('ink')}"
    thin = f"1px solid {t.v('grey-2')}"
    kc = len(col_values)
    top = ("<tr>" + _cell("", tag="th") + _cell(esc(col_label), tag="th", bold=True, align="center",
                                                  extra=f"border-bottom:{thin}", color=t.v("ink-2"))
           .replace("<th ", f'<th colspan="{kc}" ', 1) + "</tr>")
    head = "<tr>" + _cell(esc(row_label), tag="th", bold=True, align="left", bottom=ink, color=t.v("ink-2")) + "".join(
        _cell(esc(c), tag="th", bold=True, align="right", bottom=ink, bg=t.v("grey-1") if i == bc else None)
        for i, c in enumerate(col_values)) + "</tr>"
    body = ""
    for i, (rv, row) in enumerate(zip(row_values, values, strict=False)):
        tds = [_cell(esc(rv), bold=True, bottom=thin, bg=t.v("grey-1") if i == br else None)]
        for j, v in enumerate(row):
            on = i == br and j == bc
            tds.append(_cell(fmt(v, number), align="right", bold=on, bottom=thin,
                             bg=t.v("accent") if on else None, color=t.v("on-accent") if on else None))
        body += "<tr>" + "".join(tds) + "</tr>"
    first = min(200.0, w * 0.28)
    table = _table(t, 0, 24, w, top + head + body, "Sensitivity", [first] + [(w - first) / kc] * kc)
    cap = text(t, 0, 0, w, 20, esc(caption), bold=True, size="fs-label", lh=1.2) if caption else ""
    return _wrap("Sensitivity", x, y, w, h, cap + table, group=False)


@component("sources_uses", "sources and uses side by side with % of total and tying totals.", "Finance tables")
def sources_uses(t, x, y, w, h, *, sources=[
        {"label": "Term loan B", "value": 1200}, {"label": "Senior notes", "value": 600},
        {"label": "Sponsor equity", "value": 1450}, {"label": "Rollover equity", "value": 150}],
        uses=[{"label": "Purchase of equity", "value": 2700}, {"label": "Refinance existing debt", "value": 580},
              {"label": "Fees and expenses", "value": 90}, {"label": "Cash to balance sheet", "value": 30}],
        unit="USD m"):
    gap = 40
    tw = (w - gap) / 2
    ink = f"1px solid {t.v('ink')}"
    thin = f"1px solid {t.v('grey-2')}"

    def one(title, items):
        total = sum(float(i.get("value") or 0) for i in items)
        rows = "<tr>" + _cell(esc(title), tag="th", bold=True, bottom=ink) + _cell(esc(unit), tag="th", bold=True, align="right", bottom=ink) \
               + _cell("% of total", tag="th", bold=True, align="right", bottom=ink) + "</tr>"
        for i in items:
            v = float(i.get("value") or 0)
            rows += ("<tr>" + _cell(esc(i.get("label", "")), bottom=thin) + _cell(fmt(v, "0"), align="right", bottom=thin)
                     + _cell(fmt(100 * v / total if total else None, "%"), align="right", bottom=thin) + "</tr>")
        rows += ("<tr>" + _cell("Total", bold=True, top=ink) + _cell(fmt(total, "0"), align="right", bold=True, top=ink)
                 + _cell("100.0%", align="right", bold=True, top=ink) + "</tr>")
        return rows, total

    s_rows, s_tot = one("Sources", sources)
    u_rows, u_tot = one("Uses", uses)
    _need(abs(s_tot - u_tot) < 0.5, f"sources ({s_tot:g}) and uses ({u_tot:g}) must tie")
    cols = [tw * 0.56, tw * 0.22, tw * 0.22]
    return _wrap("Sources and uses", x, y, w, h,
                 _table(t, 0, 0, tw, s_rows, "Sources", cols) + _table(t, tw + gap, 0, tw, u_rows, "Uses", cols),
                 group=False)


# ==================================================================================== charts
def _plot(x, y, w, h, pa):
    return x + pa["x"] * w, y + pa["y"] * h, pa["w"] * w, pa["h"] * h


@component("column_chart", "native column/bar chart, grey context, highlights in accent, optional CAGR arrow.", "Charts",
           hint="Pinned plotArea: the CAGR arrow is computed on the bars. Pass tokens for theme colours.")
def column_chart(t, x, y, w, h, *, categories=["2021", "2022", "2023", "2024", "2025", "2026E"],
                 values=[182, 204, 231, 248, 279, 312], highlight=[5], series_name="Revenue", number_format="#,##0",
                 orientation="column", cagr={"from": 0, "to": 5, "label": "+11% p.a."}, header="Revenue, USD m"):
    k = len(categories)
    _need(1 <= k <= 24 and len(values) == k, "column_chart takes 1-24 categories and one value per category")
    _need(orientation in ("column", "bar"), "orientation is column or bar")
    nums = [v for v in values if isinstance(v, (int, float))]
    lo, hi, unit = nice_axis(min(nums or [0]), max(nums or [1]) * 1.18)
    base = t.hex("grey-4") if highlight else t.hex("primary")
    pc = {str(i): t.hex("accent") for i in highlight if 0 <= i < k}
    cats, vals = [str(c) for c in categories], list(values)
    if orientation == "bar":    # a bar chart draws its first category at the bottom: reverse, so it reads top-down
        cats, vals = cats[::-1], vals[::-1]
        pc = {str(k - 1 - int(i)): c for i, c in pc.items()}
    top = 28 if header else 0
    if orientation == "bar":
        pa = {"x": 0.22, "y": 0.02, "w": 0.7, "h": 0.96}
    else:
        pa = {"x": 0.02, "y": 0.14 if cagr else 0.06, "w": 0.96, "h": (0.72 if cagr else 0.8)}
    spec_ = {"type": orientation, "categories": cats,
             "series": [{"name": series_name, "values": vals}], "colors": [base],
             "options": {"dataLabels": True, "labelPosition": "outEnd", "numberFormat": number_format,
                         "gridlines": False, "legend": False, "gapWidth": 60,
                         "valueAxis": {"min": lo, "max": hi, "visible": False},
                         "plotArea": pa, "fontSize": round(t.px("fs-label") * 0.75, 1), "fontColor": t.hex("ink"),
                         **({"pointColors": {"0": pc}} if pc else {})}}
    out = []
    if header:
        out.append(text(t, 0, 0, w, 22, esc(header), bold=True, size="fs-label", lh=1.2))
    ch = h - top
    out.append(chart(0, top, w, ch, spec_, "Chart"))
    if cagr and orientation == "column":
        i, j = int(cagr.get("from", 0)), int(cagr.get("to", k - 1))
        _need(0 <= i < j < k, "cagr from/to are category indices, from < to")
        px, py, pw, ph = _plot(0, top, w, ch, pa)
        slot = pw / k
        def top_of(idx):
            v = float(values[idx] or 0)
            return py + ph * (1 - (v - lo) / (hi - lo))
        x1, x2 = px + (i + 0.5) * slot, px + (j + 0.5) * slot
        ya = min(top_of(i), top_of(j)) - 34
        yl = min(top_of(idx) for idx in range(i, j + 1)) - 34
        ya = min(ya, yl)
        mid = uid("cg", {"f": i, "t": j, "l": cagr.get("label")})
        col = t.hex("ink")
        lines = (arrow_defs(mid, col) +
                 f'<polyline points="{x1:.1f},{top_of(i) - 26:.1f} {x1:.1f},{ya:.1f} {x2:.1f},{ya:.1f} {x2:.1f},{top_of(j) - 28:.1f}" '
                 f'fill="none" stroke="{col}" stroke-width="1.5" marker-end="url(#{mid})"/>')
        out.append(svg(0, 0, w, h, lines))
        lw = 120
        out.append(text(t, (x1 + x2) / 2 - lw / 2, ya - 13, lw, 26, esc(cagr.get("label", "")), bold=True,
                        size="fs-label", align="center", valign="middle", lh=1,
                        style=f"background:{t.v('bg')};border:1.5px solid {t.v('ink')};border-radius:13px"))
    return _wrap("Column chart", x, y, w, h, "".join(out), group=False)


@component("cagr_arrow", "stand-alone growth annotation: elbow arrow between two points plus a rounded label.",
           "Charts", box=(100, 200, 600, 60), hint="Box = the span; the arrow runs from the box's bottom-left up "
           "to the top and down to the bottom-right.")
def cagr_arrow(t, x, y, w, h, *, label="+12% p.a.", rise=True):
    mid = uid("ca", {"l": label, "w": w, "h": h})
    col = t.hex("ink")
    y0 = h if rise else h * 0.6
    y1 = h * 0.6 if rise else h
    lines = (arrow_defs(mid, col) +
             f'<polyline points="0,{y0:.1f} 0,13 {w:.1f},13 {w:.1f},{y1:.1f}" fill="none" stroke="{col}" '
             f'stroke-width="1.5" marker-end="url(#{mid})"/>')
    lw = 120
    return _wrap("CAGR", x, y, w, h, svg(0, 0, w, h, lines) + text(
        t, w / 2 - lw / 2, 0, lw, 26, esc(label), bold=True, size="fs-label", align="center", valign="middle", lh=1,
        style=f"background:{t.v('bg')};border:1.5px solid {t.v('ink')};border-radius:13px"))


@component("waterfall", "native bridge: start and end totals, signed steps, labels above, dashed connectors.",
           "Charts", hint="Values are signed steps; totals are indices of bars standing on the axis.")
def waterfall(t, x, y, w, h, *, categories=["FY24 EBITDA", "Volume", "Price", "Mix", "Input costs", "Opex", "FY25 EBITDA"],
              values=[420, 38, 55, 12, -64, -21, 440], totals=[0, 6], number_format="#,##0;(#,##0)",
              header="EBITDA bridge, USD m"):
    k = len(categories)
    _need(2 <= k <= 20 and len(values) == k, "waterfall takes 2-20 categories and one value per category")
    if totals is None:          # the first bar and the last stand on the axis
        totals = [0, k - 1]
    _need(all(isinstance(i, int) and 0 <= i < k for i in totals), "totals are category indices")
    run, hi, lo = 0.0, 0.0, 0.0
    for i, v in enumerate(values):
        run = float(v) if i in totals else run + float(v)
        hi, lo = max(hi, run), min(lo, run)
    amin, amax, _ = nice_axis(lo, hi * 1.12)
    spec_ = {"type": "waterfall", "categories": [str(c) for c in categories],
             "series": [{"name": "Change", "values": values}],
             "colors": [t.hex("grey-4"), t.hex("rag-r"), t.hex("primary")],
             "options": {"totals": totals, "dataLabels": True, "labelPosition": "outEnd", "numberFormat": number_format,
                         "gridlines": False, "legend": False, "gapWidth": 50,
                         "valueAxis": {"min": amin, "max": amax, "visible": False},
                         "connectors": {"color": t.hex("grey-4"), "width": 1, "dash": [3, 3]},
                         "fontSize": round(t.px("fs-label") * 0.75, 1), "fontColor": t.hex("ink")}}
    top = 28 if header else 0
    head = text(t, 0, 0, w, 22, esc(header), bold=True, size="fs-label", lh=1.2) if header else ""
    return _wrap("Waterfall", x, y, w, h, head + chart(0, top, w, h - top, spec_, "Bridge"), group=False)


@component("football_field", "valuation ranges per method on one axis, low/high labels, offer line.", "Charts",
           hint="bar_stacked with a base series in the background colour; labels are placed on the pinned plot.")
def football_field(t, x, y, w, h, *, rows=[
        {"label": "52-week trading range", "low": 31.2, "high": 44.8, "note": "As of 30 Jun 2026"},
        {"label": "Analyst price targets", "low": 38.0, "high": 52.0, "note": "12 brokers"},
        {"label": "Trading comparables", "low": 35.5, "high": 49.0, "note": "9.0-11.5x NTM EBITDA"},
        {"label": "Precedent transactions", "low": 41.0, "high": 57.5, "note": "10.5-13.0x LTM EBITDA"},
        {"label": "DCF", "low": 39.5, "high": 55.0, "note": "WACC 8.5-9.5%, TGR 2.0-3.0%"},
        {"label": "LBO", "low": 34.0, "high": 45.5, "note": "20-25% IRR, 5.5x leverage"}],
        reference={"value": 47.0, "label": "Offer USD 47.00"}, number_format="0.00", axis_title="USD per share",
        highlight=3):
    k = len(rows)
    _need(1 <= k <= 10, "football_field takes 1-10 rows")
    lows = [float(r["low"]) for r in rows]
    highs = [float(r["high"]) for r in rows]
    extra = [float(reference["value"])] if reference else []
    span = max(highs + extra) - min(lows + extra)
    step = nice_axis(0, span, 6)[2]
    amin = math.floor((min(lows + extra) - span * 0.08) / step) * step
    amax = math.ceil((max(highs + extra) + span * 0.08) / step) * step
    rail = min(320.0, w * 0.34)
    pa = {"x": 0.0, "y": 0.02, "w": 1.0, "h": 0.88}
    cw = w - rail
    colours = [t.hex("bg"), t.hex("primary")]
    ordered = list(reversed(rows))   # a bar chart draws its first category at the bottom
    pc = {str(k - 1 - highlight): t.hex("accent")} if highlight is not None and 0 <= highlight < k else {}
    spec_ = {"type": "bar_stacked", "categories": [str(r["label"]) for r in ordered],
             "series": [{"name": "Base (hidden)", "values": [round(float(r["low"]) - amin, 4) for r in ordered]},
                        {"name": "Range", "values": [round(float(r["high"]) - float(r["low"]), 4) for r in ordered]}],
             "colors": colours,
             "options": {"dataLabels": False, "gridlines": False, "legend": False, "gapWidth": 70,
                         "valueAxis": {"min": 0, "max": round(amax - amin, 4), "visible": False},
                         "categoryAxis": {"visible": False}, "plotArea": pa,
                         **({"pointColors": {"1": pc}} if pc else {}),
                         **({"referenceLines": [{"value": round(float(reference["value"]) - amin, 4),
                                                 "color": t.hex("ink"), "dash": [4, 3]}]} if reference else {})}}
    top = 24 if reference else 0
    ch = h - 26 - top
    px, py, pw, ph = _plot(rail, top, cw, ch, pa)
    slot = ph / k
    scale = pw / (amax - amin)
    out = [chart(rail, top, cw, ch, spec_, "Football field")]
    for i, r in enumerate(rows):
        cy = py + (i + 0.5) * slot
        on = i == highlight
        out.append(text(t, 0, cy - slot / 2, rail - 16, slot,
                        f"<b>{esc(r['label'])}</b>" + (f'<br><span style="color:{t.v("ink-2")};font-size:{t.v("fs-source")}">'
                                                       f'{esc(r.get("note", ""))}</span>' if r.get("note") else ""),
                        size="fs-label", valign="middle", lh=1.2))
        x_lo = px + (float(r["low"]) - amin) * scale
        x_hi = px + (float(r["high"]) - amin) * scale
        kind = "0.00" if number_format == "0.00" else "0.0"
        mask = f"background:{t.v('bg')}"   # the offer line passes behind the labels, not through them
        out.append(text(t, x_lo - 50, cy - 9, 46, 18, fmt(float(r["low"]), kind), size="fs-label", align="right",
                        lh=1.2, bold=on, style=mask))
        out.append(text(t, x_hi + 4, cy - 9, 46, 18, fmt(float(r["high"]), kind), size="fs-label", lh=1.2, bold=on,
                        style=mask))
        if i:
            out.append(hrule(t, 0, cy - slot / 2, w, colour="grey-2"))
    # a light axis under the plot: ticks every `step`
    ax_y = py + ph + 4
    out.append(hrule(t, px, ax_y, pw, colour="grey-4"))
    v = amin
    while v <= amax + 1e-9:
        tx = px + (v - amin) * scale
        out.append(vrule(t, tx, ax_y, 4, colour="grey-4"))
        out.append(text(t, tx - 30, ax_y + 6, 60, 14, f"{v:g}", size="fs-source", align="center", color="ink-2", lh=1))
        v += step
    if axis_title:
        out.append(text(t, px, h - 14, pw, 14, esc(axis_title), size="fs-source", align="center", color="ink-2", lh=1))
    if reference:
        rx = px + (float(reference["value"]) - amin) * scale
        out.append(text(t, rx - 80, 0, 160, 18, esc(reference.get("label", "")), bold=True, size="fs-label",
                        align="center", lh=1.2))
    return _wrap("Football field", x, y, w, h, "".join(out), group=False)


@component("share_bar", "100% stacked bars (market share / mix) with a legend above; one series in accent.",
           "Charts")
def share_bar(t, x, y, w, h, *, categories=["2022", "2023", "2024", "2025"], series=[
        {"name": "Us", "values": [18, 21, 24, 27]}, {"name": "Competitor A", "values": [31, 30, 28, 27]},
        {"name": "Competitor B", "values": [22, 21, 21, 20]}, {"name": "Others", "values": [29, 28, 27, 26]}],
        highlight=0, orientation="column", header="Market share, % of revenue"):
    k = len(series)
    _need(1 <= k <= 6, "share_bar takes 1-6 series")
    _need(all(len(s.get("values", [])) == len(categories) for s in series), "one value per category in every series")
    _need(orientation in ("column", "bar"), "orientation is column or bar")
    colours = []
    gi = 0
    for i in range(k):
        if i == highlight:
            colours.append(t.hex("accent"))
        else:
            colours.append([t.hex("grey-5"), t.hex("grey-4"), t.hex("grey-3"), t.hex("grey-2"), t.hex("grey-1")][gi % 5])
            gi += 1
    top = 28 if header else 0
    legend_h = 24
    spec_ = {"type": f"{orientation}_stacked_100", "categories": [str(c) for c in categories],
             "series": [{"name": s["name"], "values": s["values"]} for s in series], "colors": colours,
             "options": {"dataLabels": True, "numberFormat": "0", "gridlines": False, "legend": False, "gapWidth": 50,
                         "valueAxis": {"visible": False}, "fontSize": round(t.px("fs-label") * 0.75, 1)}}
    out = []
    if header:
        out.append(text(t, 0, 0, w, 22, esc(header), bold=True, size="fs-label", lh=1.2))
    lx = 0.0
    for s, c in zip(series, colours, strict=False):
        out.append(box(lx, top + 5, 12, 12, f"background:{c}"))
        label_w = 16 + 8 * len(str(s["name"]))
        out.append(text(t, lx + 18, top, label_w, 22, esc(s["name"]), size="fs-label", valign="middle", lh=1))
        lx += 18 + label_w + 16
    out.append(chart(0, top + legend_h + 4, w, h - top - legend_h - 4, spec_, "Share chart"))
    return _wrap("Share bar", x, y, w, h, "".join(out), group=False)


@component("line_chart", "native line chart, one series in accent, rest grey, names at line ends.",
           "Charts", hint="Pinned plotArea so the end labels sit on the last points.")
def line_chart(t, x, y, w, h, *, categories=["2019", "2020", "2021", "2022", "2023", "2024", "2025"], series=[
        {"name": "Us", "values": [100, 96, 108, 121, 133, 147, 162]},
        {"name": "Peer median", "values": [100, 91, 99, 106, 110, 115, 119]},
        {"name": "Market", "values": [100, 94, 101, 104, 107, 109, 112]}], highlight=0, number_format="0",
        header="Revenue indexed, 2019 = 100"):
    k = len(series)
    _need(1 <= k <= 6, "line_chart takes 1-6 series")
    _need(all(len(s.get("values", [])) == len(categories) for s in series), "one value per category in every series")
    vals = [float(v) for s in series for v in s["values"] if isinstance(v, (int, float))]
    lo_, hi_ = min(vals), max(vals)
    pad = (hi_ - lo_) * 0.1 or 1
    _, _, unit = nice_axis(0, hi_ - lo_ + 2 * pad, 4)
    amin = math.floor((lo_ - pad) / unit) * unit
    amax = math.ceil((hi_ + pad) / unit) * unit
    colours = [t.hex(t.strong()) if i == highlight else [t.hex("grey-5"), t.hex("grey-3"), t.hex("grey-4")][i % 3]
               for i in range(k)]
    top = 28 if header else 0
    label_w = 150
    cw = w - label_w
    pa = {"x": 0.06, "y": 0.04, "w": 0.92, "h": 0.84}
    spec_ = {"type": "line_markers", "categories": [str(c) for c in categories],
             "series": [{"name": s["name"], "values": s["values"], "lineWidth": 3 if i == highlight else 2}
                        for i, s in enumerate(series)], "colors": colours,
             "options": {"dataLabels": False, "gridlines": {"color": t.hex("grey-2"), "width": 0.75}, "legend": False,
                         "valueAxis": {"min": amin, "max": amax, "majorUnit": unit, "visible": True,
                                       "format": "0.0" if unit < 1 and number_format == "0" else number_format},
                         "plotArea": pa, "fontSize": round(t.px("fs-label") * 0.75, 1), "fontColor": t.hex("ink-2")}}
    out = []
    if header:
        out.append(text(t, 0, 0, w, 22, esc(header), bold=True, size="fs-label", lh=1.2))
    ch = h - top
    out.append(chart(0, top, cw, ch, spec_, "Line chart"))
    px, py, pw, ph = _plot(0, top, cw, ch, pa)
    ends = []
    for i, s in enumerate(series):
        v = float(s["values"][-1])
        ends.append([py + ph * (1 - (v - amin) / (amax - amin)), i, v])
    ends.sort()
    for a in range(1, len(ends)):          # keep end labels at least 20px apart
        ends[a][0] = max(ends[a][0], ends[a - 1][0] + 20)
    for ly, i, v in ends:
        on = i == highlight
        out.append(text(t, px + pw + 10, ly - 10, label_w + (cw - px - pw) - 10, 20,
                        f"{esc(series[i]['name'])} {fmt(v, '0' if number_format == '0' else '0.0')}", size="fs-label",
                        bold=on, valign="middle", lh=1, style=f"color:{colours[i] if on else t.v('ink-2')}"))
    return _wrap("Line chart", x, y, w, h, "".join(out), group=False)


@component("mekko", "Marimekko: column widths = segment size, stacks = shares, labels inside.",
           "Charts")
def mekko(t, x, y, w, h, *, segments=[
        {"name": "Retail", "size": 42, "parts": [{"label": "Us", "share": 28}, {"label": "A", "share": 34}, {"label": "Others", "share": 38}]},
        {"name": "Wholesale", "size": 31, "parts": [{"label": "Us", "share": 12}, {"label": "A", "share": 46}, {"label": "Others", "share": 42}]},
        {"name": "Online", "size": 17, "parts": [{"label": "Us", "share": 35}, {"label": "A", "share": 20}, {"label": "Others", "share": 45}]},
        {"name": "Services", "size": 10, "parts": [{"label": "Us", "share": 8}, {"label": "A", "share": 30}, {"label": "Others", "share": 62}]}],
        unit="USD bn", highlight_label="Us"):
    _need(1 <= len(segments) <= 10, "mekko takes 1-10 segments")
    total = sum(float(s.get("size") or 0) for s in segments) or 1
    foot = 44
    ph = h - foot
    gap = 3
    cw_total = w - gap * (len(segments) - 1)
    out = []
    cx = 0.0
    greys = ["grey-4", "grey-3", "grey-2", "grey-1"]
    for s in segments:
        sw = cw_total * float(s.get("size") or 0) / total
        parts = s.get("parts", [])
        psum = sum(float(p.get("share") or 0) for p in parts) or 1
        cy = 0.0
        gi = 0
        for p in parts:
            hh = ph * float(p.get("share") or 0) / psum
            on = p.get("label") == highlight_label
            fill = t.v("accent") if on else t.v(greys[gi % 4])
            if not on:
                gi += 1
            out.append(box(cx, cy, sw, hh - gap if hh > gap else hh, f"background:{fill}"))
            if hh >= 30 and sw >= 44:
                out.append(text(t, cx + 4, cy, sw - 8, hh - gap, f"{esc(p.get('label', ''))}<br>{fmt(float(p.get('share') or 0), '0')}%",
                                size="fs-source", align="center", valign="middle", lh=1.15, bold=on,
                                color="on-accent" if on else "ink"))
            cy += hh
        out.append(text(t, cx, ph + 4, sw, foot - 4, f"<b>{esc(s.get('name', ''))}</b><br>{fmt(float(s.get('size') or 0), '0')} {esc(unit)}",
                        size="fs-source", align="center", lh=1.2))
        cx += sw + gap
    return _wrap("Mekko", x, y, w, h, "".join(out))


@component("bullet_rows", "per KPI: grey bands, actual bar, target tick, value; misses in red.", "Charts",
           hint="rows: actual, target, bands (ascending thresholds, the last the scale end), lower_is_better, "
           "format. scale row (each KPI its own) or column (one shared).")
def bullet_rows(t, x, y, w, h, *, rows=[
        {"label": "Revenue, USD m", "actual": 272, "target": 300, "bands": [200, 260, 340], "format": "0"},
        {"label": "EBITDA margin", "actual": 18.4, "target": 17.5, "bands": [12, 16, 22], "format": "%"},
        {"label": "Cost to serve, USD per order", "actual": 6.1, "target": 5.5, "bands": [5, 6.5, 8],
         "lower_is_better": True},
        {"label": "Net promoter score", "actual": 41, "target": 45, "bands": [20, 40, 60], "format": "0"}],
        band_labels=["Poor", "Satisfactory", "Good"], scale="row", flag_misses=True, number="0.0"):
    k = len(rows)
    _need(1 <= k <= 14, "bullet_rows takes 1-14 rows")
    _need(scale in ("row", "column"), "scale is row or column")
    parsed = []
    for r in rows:
        a, g = _num(r, "actual", "bullet_rows"), _num(r, "target", "bullet_rows")
        bands = r.get("bands") or []
        _need(isinstance(bands, list) and len(bands) <= 5
              and all(isinstance(b, (int, float)) and not isinstance(b, bool) for b in bands)
              and all(p < q for p, q in zip(bands, bands[1:], strict=False)), "bands are up to 5 ascending numbers")
        _need(a >= 0 and g >= 0 and all(b > 0 for b in bands), "bullet_rows draws values >= 0")
        parsed.append((a, g, [float(b) for b in bands]))
    rail = min(260.0, w * 0.26)
    val_w = 150.0
    px0 = rail + 12
    pw = w - px0 - val_w - 12
    _need(pw >= 80, "bullet_rows: the box is too narrow for the bars")
    greys = {1: ["grey-2"], 2: ["grey-3", "grey-1"], 3: ["grey-3", "grey-2", "grey-1"],
             4: ["grey-4", "grey-3", "grey-2", "grey-1"], 5: ["grey-5", "grey-4", "grey-3", "grey-2", "grey-1"]}
    nb = max((len(b) for _, _, b in parsed), default=0)
    legend_items = [(t.v("primary"), "Actual"), (t.v("ink"), "Target")]
    if flag_misses and any((a > g if r.get("lower_is_better") else a < g) for r, (a, g, _) in zip(rows, parsed, strict=False)):
        legend_items.append((t.v("rag-r"), "Misses target"))
    if band_labels and nb:
        legend_items += [(t.v(c), str(lbl)) for c, lbl in zip(greys.get(len(band_labels), greys[3]), band_labels, strict=False)]
    head = 30.0
    rh = _row_geometry(t, h - head, k, "bullet_rows", most=38.0)
    col_max = max(max([a, g] + b) for a, g, b in parsed) or 1.0
    out = [_swatches(t, px0, head - 22, 16, legend_items)[0],
           text(t, w - val_w, 0, val_w, head - 6, "Actual vs target", bold=True, size="fs-label", valign="bottom",
                align="right", lh=1.15),
           hrule(t, 0, head - 1, w, colour="ink")]
    band_h, bar_h, tick_h = round(rh * 0.56), max(6, round(rh * 0.2)), round(rh * 0.72)
    for j, (r, (a, g, bands)) in enumerate(zip(rows, parsed, strict=False)):
        ry = head + j * rh
        low = bool(r.get("lower_is_better"))
        kind = str(r.get("format") or number)
        peak = max([a, g] + bands) or 1.0
        scale_max = col_max if scale == "column" else (bands[-1] if bands and bands[-1] >= max(a, g) else peak * 1.05)
        sx = lambda v, m=scale_max: pw * min(v, m) / m  # noqa: E731
        out.append(text(t, 8, ry, rail - 16, rh, esc(r.get("label", "")), valign="middle", lh=1.15))
        cols = (greys[len(bands)] if bands else ["grey-1"])
        cols = cols[::-1] if low else cols
        edges = [0.0] + (bands or [scale_max])
        for c, lo_, hi_ in zip(cols, edges, edges[1:], strict=False):
            out.append(box(px0 + sx(lo_), ry + (rh - band_h) / 2, sx(hi_) - sx(lo_), band_h, f"background:{t.v(c)}"))
        miss = a > g if low else a < g
        fill = t.v("rag-r") if (miss and flag_misses) else t.v("primary")   # red, as bar_table and delta_badge
        if a:
            out.append(box(px0, ry + (rh - bar_h) / 2, max(1.5, sx(a)), bar_h, f"background:{fill}"))
        out.append(box(px0 + sx(g) - 1.5, ry + (rh - tick_h) / 2, 3, tick_h, f"background:{t.v('ink')}"))
        out.append(text(t, w - val_w, ry, val_w, rh, f"<b>{fmt(a, kind)}</b> "
                        f'<span style="color:{t.v("ink-2")}">vs {fmt(g, kind)}</span>', align="right",
                        valign="middle", lh=1.15, style="white-space:nowrap"))
        out.append(hrule(t, 0, ry + rh - 1, w, colour="grey-2"))
    return _wrap("Bullet rows", x, y, w, h, "".join(out))


@component("legend","a legend row of colour swatches and labels.", "Charts",
           box=(64, 120, 600, 22))
def legend(t, x, y, w, h, *, items=[{"label": "Us", "color": "accent"}, {"label": "Peers", "color": "grey-4"}]):
    out = []
    lx = 0.0
    for it in items:
        c = str(it.get("color", "primary"))
        fill = c if c.startswith("#") else t.v(c) if c in FALLBACK else t.v("primary")
        out.append(box(lx, (h - 12) / 2, 12, 12, f"background:{fill}"))
        lw = 14 + 8.5 * len(str(it.get("label", "")))
        out.append(text(t, lx + 18, 0, lw, h, esc(it.get("label", "")), size="fs-label", valign="middle", lh=1))
        lx += 18 + lw + 16
    return _wrap("Legend", x, y, w, h, "".join(out))


# ==================================================================================== content vs settings
#: Per component: (required content params, neutral values of the optional content params, index params).
#: The defaults in each signature are a SHOWCASE: `get(name)` with no content params draws the demo. As soon
#: as a caller passes any content param, every content param it leaves out takes its neutral value here —
#: empty, so nothing invented is drawn — the required ones must be given, and index params (highlight,
#: recommended, …) must point inside the caller's own data. Params not listed are settings (orientation,
#: number formats, style, ramp, arrows, legend, gap, stats, bold, rule, target) and keep their defaults.
CONTENT: dict[str, tuple[tuple[str, ...], dict[str, Any], dict[str, str]]] = {
    "action_title": (("text",), {}, {}),
    "source_note": (("source",), {"notes": []}, {}),
    "tracker": (("sections",), {"active": None}, {"active": "sections"}),
    "sticker": (("label",), {}, {}),
    "callout": (("body",), {"title": ""}, {}),
    "numbered_list": (("items",), {}, {}),
    "three_column": (("columns",), {"highlight": None}, {"highlight": "columns"}),
    "pros_cons": ((), {"pros": [], "cons": [], "pros_title": "", "cons_title": ""}, {}),
    "kpi_tiles": (("tiles",), {"highlight": None}, {"highlight": "tiles"}),
    "chevron_process": (("steps",), {}, {}),
    "value_chain": (("stages",), {"support": [], "highlight": None}, {"highlight": "stages"}),
    "pyramid": (("levels",), {}, {}),
    "funnel": (("stages",), {}, {}),
    "hub_spoke": (("center", "nodes"), {}, {}),
    "issue_tree": (("root", "branches"), {}, {}),
    "org_tiers": (("tiers",), {"highlight": None}, {"highlight": "tiers"}),
    "gantt": (("periods", "rows"), {"today": None}, {}),
    "timeline": (("milestones",), {"highlight": None}, {"highlight": "milestones"}),
    "matrix_2x2": ((), {"x_label": "", "y_label": "", "quadrants": ["", "", "", ""], "dots": []}, {}),
    "harvey_table": (("criteria", "rows"), {}, {}),
    "check_matrix": (("columns", "rows"), {"highlight_column": None}, {"highlight_column": "columns"}),
    "rag_table": (("columns", "rows"), {"status_column": None}, {"status_column": "columns"}),
    "heatmap_table": (("columns", "rows"), {"unit": ""}, {}),
    "comparison_columns": (("options", "cells"), {"row_labels": [], "recommended": None, "verdict": ""},
                           {"recommended": "options"}),
    "comps_table": (("columns", "rows"), {"unit": ""}, {}),
    "sensitivity_table": (("row_values", "col_values", "values"),
                          {"row_label": "", "col_label": "", "base": None, "caption": ""}, {}),
    "sources_uses": (("sources", "uses"), {"unit": ""}, {}),
    "column_chart": (("categories", "values"), {"highlight": [], "series_name": "Value", "cagr": None, "header": ""},
                     {"highlight": "categories"}),
    "cagr_arrow": (("label",), {}, {}),
    "waterfall": (("categories", "values"), {"totals": None, "header": ""}, {}),
    "football_field": (("rows",), {"reference": None, "axis_title": "", "highlight": None}, {"highlight": "rows"}),
    "share_bar": (("categories", "series"), {"highlight": None, "header": ""}, {"highlight": "series"}),
    "line_chart": (("categories", "series"), {"highlight": None, "header": ""}, {"highlight": "series"}),
    "mekko": (("segments",), {"unit": "", "highlight_label": None}, {}),
    "legend": (("items",), {}, {}),
    "keyed_markers": (("markers",), {"notes": []}, {}),
    "delta_badge": (("value",), {}, {}),
    "bar_table": (("rows",), {"subject": "", "benchmark": "", "headings": []}, {}),
    "bullet_rows": (("rows",), {"band_labels": []}, {}),
}


def content_params(name: str) -> set[str]:
    required, neutral, _ = CONTENT[name]
    return set(required) | set(neutral)


def _resolve(name: str, params: dict[str, Any]) -> dict[str, Any]:
    """The generator's kwargs: the showcase when no content param is passed, else the caller's content with
    neutral values for what it left out (see CONTENT)."""
    s = _REGISTRY[name]
    required, neutral, indices = CONTENT[name]
    custom = any(k in params for k in content_params(name))
    if custom:
        missing = [r for r in required if r not in params]
        if missing:
            raise ValueError(f"{name}: pass {', '.join(f'`{m}`' for m in missing)} — with your own content, "
                             f"omitted content draws nothing (no demo values)")
    kwargs = {}
    for k, d in s.params.items():
        if k in params:
            kwargs[k] = _check_type(k, params[k], d)
        else:
            kwargs[k] = neutral[k] if custom and k in neutral else d
    for k, seq in indices.items():
        v, count = kwargs.get(k), len(kwargs.get(seq) or [])
        for i in (v if isinstance(v, list) else [] if v is None else [v]):
            if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < count:
                raise ValueError(f"{name}: `{k}` {i!r} is not an index into `{seq}` (0-{count - 1})")
    return kwargs
