"""Deterministic master import: a `.pptx` goes in, the shared manifest (master brief §6) comes out.

`import_master()` reads the package; `ingest()` lands its output (layout backgrounds, assets and the
manifest) in a project directory. Layout backgrounds come from a `Renderer` the caller passes in
(`app.engine.renderer`), normally the PptxRender HTTP client.

Three things here are worth knowing before changing anything:

**Layout identity comes from `app.engine.renderer.layout_identities`, never from position.** PptxRender
enumerates layouts in relationship order and python-pptx in `<p:sldLayoutIdLst>` order; on one client
master two of the eight masters come out reversed and five layouts share the name "Title only".
So the manifest is built in python-pptx order (that is what `masterIndex`/`layoutIndex` mean), and
every join with the renderer — the layout background, above all — goes through `partName`. The name
python-pptx reads and the name the identity carries are compared and a mismatch raises.

**Placeholder geometry is inherited, not assumed.** A layout placeholder without its own `<a:xfrm>`
takes the master placeholder's box. The match is `(type, idx)`, then `type`, then the same type
*family* — layouts routinely give the date/footer/slide-number placeholders idx 10/11/12 while the
master calls them 2/3/4, so idx alone is not enough, and `obj` idx 2 must not inherit the master's
date placeholder just because the numbers agree. Every placeholder records where its box came from
in `style.boxFrom`, so an inheritance surprise is visible rather than silent.

**Scheme colours go through the master's `<p:clrMap>`.** On one client theme `dk1` is white
and `lt1` is charcoal; reading `tx1` as "the dark one" puts white text on white. Each master carries
its own theme (a real client master has eight, with different schemes), so the palette is resolved per master and
`manifest.theme` is master 0's.

Anything the importer could not read — an image part in a format no browser shows, a layout that is
reachable by relationship but missing from the id list, a theme with no latin font — is written to
`out_dir/import-report.json` and logged. Nothing is dropped silently (master brief §10.9).
"""
from __future__ import annotations

import colorsys
import hashlib
import json
import logging
import shutil
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from lxml import etree
from pptx import Presentation

from app.config import engine as config
from app.engine.manifest import (
    VERSION,
    Layout,
    Manifest,
    ManifestError,
    Master,
    Placeholder,
    sha256_of,
)
from app.engine.renderer import Renderer, RendererError, layout_identities, slide_size_emu

log = logging.getLogger(__name__)

# ------------------------------------------------------------------------------------ namespaces
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_IMAGE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
_THEME_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme"


def _a(tag: str) -> str:
    return f"{{{_A}}}{tag}"


def _p(tag: str) -> str:
    return f"{{{_P}}}{tag}"


def _local(el: etree._Element) -> str:
    return str(etree.QName(el).localname)


# ------------------------------------------------------------------------------------- constants
#: The twelve theme colour slots, in `<a:clrScheme>` order.
THEME_SLOTS: tuple[str, ...] = (
    "dk1", "lt1", "dk2", "lt2",
    "accent1", "accent2", "accent3", "accent4", "accent5", "accent6",
    "hlink", "folHlink",
)
#: The four names `<p:clrMap>` redirects; resolved and stored beside the slots.
MAPPED_SLOTS: tuple[str, ...] = ("bg1", "tx1", "bg2", "tx2")

#: Which master `txStyles` list governs a placeholder type (ECMA-376 §19.3.1.49).
_STYLE_KIND: dict[str, str] = {
    "title": "title", "ctrTitle": "title",
    "body": "body", "subTitle": "body", "obj": "body", "pic": "body", "chart": "body",
    "tbl": "body", "clipArt": "body", "dgm": "body", "media": "body",
}

#: Placeholder types that inherit from the same *kind* of master placeholder when idx disagrees.
_FAMILIES: tuple[frozenset[str], ...] = (
    frozenset({"title", "ctrTitle"}),
    frozenset({"body", "subTitle", "obj", "pic", "chart", "tbl", "clipArt", "dgm", "media"}),
    frozenset({"dt"}), frozenset({"ftr"}), frozenset({"sldNum"}),
    frozenset({"hdr"}), frozenset({"sldImg"}),
)

_ALIGN = {"l": "left", "ctr": "center", "r": "right", "just": "justify",
          "justLow": "justify", "dist": "justify", "thaiDist": "justify"}
_ANCHOR = {"t": "top", "ctr": "middle", "b": "bottom", "just": "top", "dist": "top"}

#: Content and title-only layouts whose title placeholder sets no alignment of its own get a left
#: title, not the master's `titleStyle` (often centred for the cover). By `<p:sldLayout type>`, and
#: by name for custom layouts.
_LEFT_TITLE_TYPES = frozenset({"obj", "titleOnly"})
_LEFT_TITLE_NAMES = frozenset({"content", "title and content", "title only"})

#: Image formats a browser can show. Everything else is still extracted, but kept out of
#: `manifest.assets` — offering Claude an `.emf` to reference from slide HTML produces a broken
#: image, and the design prompt is built straight from that list.
WEB_IMAGE_SUFFIXES: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"})

#: How a placeholder type is described in the derived `usage` line.
_USAGE_WORD = {
    "title": "title", "ctrTitle": "centred title", "subTitle": "subtitle", "body": "body",
    "obj": "content", "pic": "picture", "chart": "chart", "tbl": "table", "dgm": "diagram",
    "clipArt": "clip art", "media": "media", "sldImg": "slide image", "hdr": "header",
}
#: Order the words appear in, so two layouts with the same zones get the same sentence.
_USAGE_ORDER = ("ctrTitle", "title", "subTitle", "body", "obj", "pic", "chart", "tbl",
                "dgm", "clipArt", "media", "sldImg", "hdr")
#: Page furniture: real placeholders, but not design zones — kept out of the usage line.
_FURNITURE = frozenset({"dt", "ftr", "sldNum"})


class MasterImportError(ManifestError):
    """The master cannot be imported — a broken package, or a join the importer refuses to guess."""


# --------------------------------------------------------------------------------- XML utilities


def _child(el: etree._Element | None, *path: str) -> etree._Element | None:
    """`el` followed down a chain of direct children; `None` as soon as a step is missing."""
    node = el
    for step in path:
        if node is None:
            return None
        node = node.find(step)
    return node


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return value in ("1", "true", "on")


# ------------------------------------------------------------------------------------- colours


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02X}" for c in rgb)


def _from_hex(value: str) -> tuple[float, float, float]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


class Palette:
    """One master's colours: the theme's twelve slots seen through that master's `<p:clrMap>`."""

    __slots__ = ("scheme", "clr_map")

    def __init__(self, scheme: dict[str, str], clr_map: dict[str, str]) -> None:
        self.scheme = scheme
        self.clr_map = clr_map

    def slot(self, name: str) -> str | None:
        """`bg1`/`tx1`/… through the colour map, `dk1`/`accent2`/… straight to the theme."""
        return self.scheme.get(self.clr_map.get(name, name))

    def colors(self) -> dict[str, str]:
        """What the manifest stores: the twelve slots, then the four mapped names resolved."""
        out = {slot: self.scheme[slot] for slot in THEME_SLOTS if slot in self.scheme}
        for name in MAPPED_SLOTS:
            resolved = self.slot(name)
            if resolved:
                out[name] = resolved
        return out

    def resolve(self, color_el: etree._Element) -> str | None:
        """A DrawingML colour element (`srgbClr`, `schemeClr`, `sysClr`, …) as `#RRGGBB`.

        `phClr` is deliberately unresolved: it means "whatever colour the shape's style supplies",
        which is not knowable from the layout alone, and guessing would put a wrong default colour
        on every placeholder that uses one.
        """
        tag = _local(color_el)
        base: str | None = None
        if tag == "srgbClr":
            base = color_el.get("val")
        elif tag == "sysClr":
            base = color_el.get("lastClr")
        elif tag == "schemeClr":
            name = color_el.get("val") or ""
            if name == "phClr":
                return None
            base = self.slot(name)
        elif tag == "scrgbClr":
            try:
                base = _hex(tuple(int(color_el.get(c, "0")) / 100000 for c in ("r", "g", "b")))  # type: ignore[arg-type]
            except ValueError:
                base = None
        elif tag == "prstClr":
            base = _PRESET_COLORS.get(color_el.get("val") or "")
        if not base:
            return None
        return _hex(_transform(_from_hex(base), color_el))


def _transform(rgb: tuple[float, float, float], color_el: etree._Element) -> tuple[float, float, float]:
    """Apply the luminance/shade/tint children of a colour element, in document order.

    `lumMod`/`lumOff` are exact (PowerPoint works in HSL here). `shade`/`tint` are the sRGB
    approximation of PowerPoint's linear-gamma blend — close enough for a manifest default colour,
    and wrong by a few units only on saturated mid-tones. Transforms this does not know
    (`satMod`, `hueMod`, `alpha`, `gamma`) are left out rather than half-applied.
    """
    red, green, blue = rgb
    for child in color_el:
        tag = _local(child)
        raw = child.get("val")
        if raw is None:
            continue
        try:
            value = int(raw) / 100000
        except ValueError:
            continue
        if tag in ("lumMod", "lumOff"):
            hue, lum, sat = colorsys.rgb_to_hls(red, green, blue)
            lum = lum * value if tag == "lumMod" else min(1.0, lum + value)
            red, green, blue = colorsys.hls_to_rgb(hue, max(0.0, min(1.0, lum)), sat)
        elif tag == "shade":
            red, green, blue = red * value, green * value, blue * value
        elif tag == "tint":
            red, green, blue = (c * value + (1 - value) for c in (red, green, blue))
    return red, green, blue


#: The handful of `prstClr` names that turn up in real templates. Unknown names resolve to `None`
#: (and are reported) rather than to an invented colour.
_PRESET_COLORS: dict[str, str] = {
    "black": "000000", "white": "FFFFFF", "red": "FF0000", "green": "008000", "blue": "0000FF",
    "yellow": "FFFF00", "gray": "808080", "grey": "808080", "darkGray": "A9A9A9",
    "lightGray": "D3D3D3", "orange": "FFA500", "purple": "800080",
}


def _solid_color(parent: etree._Element | None, palette: Palette) -> str | None:
    """The `<a:solidFill>` colour of a run-properties/paragraph element, if it has one."""
    fill = _child(parent, _a("solidFill"))
    if fill is None or len(fill) == 0:
        return None
    return palette.resolve(fill[0])


# -------------------------------------------------------------------------------- text defaults


def _run_props(def_rpr: etree._Element | None, palette: Palette) -> dict[str, Any]:
    """`<a:defRPr>` as manifest fields. Missing attributes stay `None` so the chain can fill them."""
    if def_rpr is None:
        return {}
    size = _int(def_rpr.get("sz"))
    latin = def_rpr.find(_a("latin"))
    typeface = latin.get("typeface") if latin is not None else None
    return {
        "font": typeface or None,
        "sizePt": size / 100 if size else None,
        "bold": _bool(def_rpr.get("b")),
        "italic": _bool(def_rpr.get("i")),
        "color": _solid_color(def_rpr, palette),
    }


def _bullet(p_pr: etree._Element, palette: Palette) -> dict[str, Any] | None:
    """The bullet declared on a paragraph-properties element, or `None` when it declares none."""
    kind: str | None = None
    detail: dict[str, Any] = {}
    if p_pr.find(_a("buNone")) is not None:
        kind = "none"
    elif (auto := p_pr.find(_a("buAutoNum"))) is not None:
        kind = "autoNum"
        detail["autoNumType"] = auto.get("type")
        detail["startAt"] = _int(auto.get("startAt"))
    elif (char := p_pr.find(_a("buChar"))) is not None:
        kind = "char"
        detail["char"] = char.get("char")
    if kind is None:
        return None
    font = p_pr.find(_a("buFont"))
    size = p_pr.find(_a("buSzPct"))
    color = p_pr.find(_a("buClr"))
    detail["kind"] = kind
    detail["font"] = font.get("typeface") if font is not None else None
    detail["sizePct"] = (_int(size.get("val")) or 0) / 1000 if size is not None else None
    detail["color"] = palette.resolve(color[0]) if color is not None and len(color) else None
    return detail


def _para_props(p_pr: etree._Element | None, palette: Palette) -> dict[str, Any]:
    """One `<a:lvlNpPr>`/`<a:pPr>` as manifest fields, with `None` wherever it says nothing."""
    if p_pr is None:
        return {}
    props: dict[str, Any] = {
        "align": _ALIGN.get(p_pr.get("algn") or "", p_pr.get("algn")),
        "marLEmu": _int(p_pr.get("marL")),
        "indentEmu": _int(p_pr.get("indent")),
        "lineSpacingPct": None,
        "lineSpacingPt": None,
        "spaceBeforePt": None,
        "spaceAfterPt": None,
        "bullet": _bullet(p_pr, palette),
    }
    for tag, pct_key, pt_key in (
        ("lnSpc", "lineSpacingPct", "lineSpacingPt"),
        ("spcBef", None, "spaceBeforePt"),
        ("spcAft", None, "spaceAfterPt"),
    ):
        node = p_pr.find(_a(tag))
        if node is None:
            continue
        pct, pts = node.find(_a("spcPct")), node.find(_a("spcPts"))
        if pct is not None and pct_key:
            props[pct_key] = (_int(pct.get("val")) or 0) / 1000
        elif pct is not None and tag == "spcBef":
            # spcBef as a percentage of the font size has no pt value; record it as a percentage
            # under its own key so a consumer does not read it as points.
            props["spaceBeforePct"] = (_int(pct.get("val")) or 0) / 1000
        elif pts is not None:
            props[pt_key] = (_int(pts.get("val")) or 0) / 100
    props.update(_run_props(p_pr.find(_a("defRPr")), palette))
    return props


def _merge(sources: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """First non-`None` value per key, most specific source first."""
    merged: dict[str, Any] = {}
    for source in sources:
        for key, value in source.items():
            if value is not None and merged.get(key) is None:
                merged[key] = value
    return merged


#: OOXML's `<a:bodyPr>` inset defaults, in px: 0.1 in left/right, 0.05 in top/bottom.
_DEFAULT_INSETS_PX: dict[str, float] = {"l": 9.6, "t": 4.8, "r": 9.6, "b": 4.8}


def _body_props(txbody: etree._Element | None) -> dict[str, Any]:
    """`<a:bodyPr>`: anchoring, wrapping, autofit and the text insets, in px.

    Every value is `None` when the element does not state it, so the layout's `<a:bodyPr/>` does not
    reset an inset the master set. a client deck's placeholders run at zero insets; treating an omitted `lIns`
    as the 0.1 in default would shift every title nine pixels right of where the master puts it.
    """
    body_pr = _child(txbody, _a("bodyPr"))
    if body_pr is None:
        return {}
    if body_pr.find(_a("normAutofit")) is not None:
        autofit = "shrink"
    elif body_pr.find(_a("spAutoFit")) is not None:
        autofit = "resize"
    elif body_pr.find(_a("noAutofit")) is not None:
        autofit = "none"
    else:
        autofit = None
    props: dict[str, Any] = {
        "anchor": _ANCHOR.get(body_pr.get("anchor") or "", body_pr.get("anchor")),
        "autofit": autofit,
        "wrap": body_pr.get("wrap"),
    }
    for side, attr in (("l", "lIns"), ("t", "tIns"), ("r", "rIns"), ("b", "bIns")):
        raw = _int(body_pr.get(attr))
        props[f"inset-{side}"] = round(raw / config.EMU_PER_PX, 3) if raw is not None else None
    return props


# --------------------------------------------------------------------------------- package model


def _placeholder_shapes(slide_el: etree._Element) -> list[tuple[str, int, str, etree._Element]]:
    """Every placeholder shape of a master/layout as `(type, idx, name, element)`, in paint order.

    Walks `<p:spTree>` directly instead of `python-pptx`'s `.placeholders`: that property maps a
    layout placeholder onto a master one through a fixed type table which raises `KeyError` on the
    types it does not list (`hdr`, `sldImg`, the vertical variants), and it hides whether the box
    was declared or inherited — which is exactly what this importer has to report.
    """
    tree = _child(slide_el, _p("cSld"), _p("spTree"))
    if tree is None:
        return []
    found: list[tuple[str, int, str, etree._Element]] = []
    for shape in tree:
        ph = None
        for nv_tag in ("nvSpPr", "nvPicPr", "nvGraphicFramePr", "nvCxnSpPr", "nvGrpSpPr"):
            ph = _child(shape, _p(nv_tag), _p("nvPr"), _p("ph"))
            if ph is not None:
                break
        if ph is None:
            continue
        nv = ph.getparent().getparent()
        c_nv_pr = nv.find(_p("cNvPr"))
        name = (c_nv_pr.get("name") if c_nv_pr is not None else None) or ""
        # ECMA-376 leaves `type` optional; python-pptx reports the omission as `obj`, and the
        # manifest's PLACEHOLDER_TYPES follows it.
        found.append((ph.get("type") or "obj", _int(ph.get("idx")) or 0, name, shape))
    return found


def _xfrm(shape: etree._Element) -> tuple[int, int, int, int] | None:
    """`(x, y, cx, cy)` in EMU from a shape's own `<a:xfrm>`, or `None` when it declares none."""
    # `p:sp`/`p:pic` carry the transform inside `p:spPr`; `p:graphicFrame` carries `p:xfrm` itself.
    xfrm = _child(shape, _p("spPr"), _a("xfrm"))
    if xfrm is None:
        xfrm = _child(shape, _p("xfrm"))
    if xfrm is None:
        return None
    off, ext = xfrm.find(_a("off")), xfrm.find(_a("ext"))
    if off is None or ext is None:
        return None
    return (_int(off.get("x")) or 0, _int(off.get("y")) or 0,
            _int(ext.get("cx")) or 0, _int(ext.get("cy")) or 0)


def _family(ph_type: str) -> frozenset[str]:
    for family in _FAMILIES:
        if ph_type in family:
            return family
    return frozenset({ph_type})


def _inherit_from(
    ph_type: str, idx: int, master_phs: list[tuple[str, int, str, etree._Element]]
) -> tuple[etree._Element, str] | None:
    """The master placeholder a layout placeholder inherits from, and why it was chosen.

    Order matters. `(type, idx)` first; then `type` alone, because a layout habitually numbers the
    date/footer/slide-number placeholders 10/11/12 where the master numbered them 2/3/4; then the
    type *family*, so a layout `obj` falls back to the master's body placeholder. Matching on bare
    `idx` is never attempted: a layout `obj` at idx 2 would inherit the master's date placeholder.
    """
    for candidate_type, candidate_idx, _name, shape in master_phs:
        if candidate_type == ph_type and candidate_idx == idx:
            return shape, f"master:{candidate_type}[{candidate_idx}]"
    for candidate_type, candidate_idx, _name, shape in master_phs:
        if candidate_type == ph_type:
            return shape, f"master:{candidate_type}[{candidate_idx}]"
    family = _family(ph_type)
    for candidate_type, candidate_idx, _name, shape in master_phs:
        if candidate_type in family:
            return shape, f"master:{candidate_type}[{candidate_idx}]"
    return None


def _cSld_name(slide_el: etree._Element) -> str:
    """`<p:cSld name>` of a master or layout part, stripped; `""` when it declares none."""
    common = _child(slide_el, _p("cSld"))
    return (common.get("name") or "").strip() if common is not None else ""


def _theme_root(master_part: Any) -> etree._Element | None:
    for rel in master_part.rels.values():
        if rel.reltype == _THEME_REL and not rel.is_external:
            return etree.fromstring(rel.target_part.blob)
    return None


def _palette_of(master_el: etree._Element, theme_root: etree._Element | None) -> Palette:
    scheme: dict[str, str] = {}
    clr_scheme = _child(theme_root, _a("themeElements"), _a("clrScheme")) if theme_root is not None else None
    if clr_scheme is not None:
        for child in clr_scheme:
            if len(child) == 0:
                continue
            value = child[0]
            tag = _local(value)
            raw = value.get("val") if tag == "srgbClr" else value.get("lastClr") if tag == "sysClr" else None
            if raw:
                scheme[_local(child)] = "#" + raw.upper()
    clr_map_el = master_el.find(_p("clrMap"))
    clr_map = dict(clr_map_el.attrib) if clr_map_el is not None else {}
    return Palette(scheme, clr_map)


def _theme_fonts(theme_root: etree._Element | None, warnings: list[str], master_index: int) -> dict[str, str]:
    font_scheme = _child(theme_root, _a("themeElements"), _a("fontScheme")) if theme_root is not None else None
    faces: dict[str, str] = {}
    for key, tag in (("major", "majorFont"), ("minor", "minorFont")):
        latin = _child(font_scheme, _a(tag), _a("latin")) if font_scheme is not None else None
        faces[key] = (latin.get("typeface") or "").strip() if latin is not None else ""
    if not faces["major"] and faces["minor"]:
        faces["major"] = faces["minor"]
    if not faces["minor"] and faces["major"]:
        faces["minor"] = faces["major"]
    if not faces["major"]:
        # Measurement forces these two families onto the page; an empty family would produce
        # `font-family: ;` and silently measure in the browser default instead.
        faces = {"major": "Arial", "minor": "Arial"}
        warnings.append(f"master {master_index}: theme declares no latin font; using Arial for both")
    return faces


def _usage_of(placeholders: Iterable[Placeholder]) -> str:
    """A one-line description of a layout, derived from its zones only — no LLM, no guessing."""
    counts: dict[str, int] = {}
    for placeholder in placeholders:
        if placeholder.type in _FURNITURE:
            continue
        counts[placeholder.type] = counts.get(placeholder.type, 0) + 1
    if not counts:
        return "blank"
    parts: list[str] = []
    for ph_type in _USAGE_ORDER:
        if ph_type not in counts:
            continue
        word = _USAGE_WORD.get(ph_type, ph_type)
        parts.append(word if counts[ph_type] == 1 else f"{counts[ph_type]} {word}")
    for ph_type in sorted(set(counts) - set(_USAGE_ORDER)):
        parts.append(ph_type if counts[ph_type] == 1 else f"{counts[ph_type]} {ph_type}")
    if len(parts) == 1 and set(counts) <= {"title", "ctrTitle"}:
        return f"{parts[0]} only"
    return " + ".join(parts)


# ------------------------------------------------------------------------------- text style maps


def _levels_of(style_el: etree._Element | None, palette: Palette) -> list[dict[str, Any]]:
    """The nine `lvlNpPr` of one `txStyles` list, resolved (colours to hex, fonts left symbolic)."""
    if style_el is None:
        return []
    levels: list[dict[str, Any]] = []
    for level in range(1, 10):
        p_pr = style_el.find(_a(f"lvl{level}pPr"))
        if p_pr is None:
            continue
        props = _para_props(p_pr, palette)
        props["level"] = level - 1
        levels.append(props)
    return levels


def _text_styles(master_el: etree._Element, palette: Palette) -> dict[str, Any]:
    tx_styles = master_el.find(_p("txStyles"))
    return {
        kind: {"levels": _levels_of(_child(tx_styles, _p(f"{kind}Style")), palette)}
        for kind in ("title", "body", "other")
    }


def _bullets(master_el: etree._Element, palette: Palette) -> list[dict[str, Any]]:
    """The body list's bullet per level — what an authored `<ul>` has to be turned into."""
    body_style = _child(master_el.find(_p("txStyles")), _p("bodyStyle"))
    bullets: list[dict[str, Any]] = []
    for level, props in enumerate(_levels_of(body_style, palette)):
        bullet = props.get("bullet") or {}
        bullets.append({
            "level": level,
            "char": bullet.get("char"),
            "font": bullet.get("font"),
            "kind": bullet.get("kind") or "none",
            "sizePct": bullet.get("sizePct"),
            "color": bullet.get("color"),
            "indentEmu": props.get("indentEmu") or 0,
            "marLEmu": props.get("marLEmu") or 0,
        })
    return bullets


# ------------------------------------------------------------------------------------- assets


def _slug(text: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in text.strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "asset"


def _extract_assets(prs: Any, out_dir: Path) -> tuple[list[str], list[dict[str, Any]]]:
    """Write every image part the masters, their themes and their layouts reference.

    Deduplicated by content hash, so the logo shared by eight masters is written once. The returned
    list is the browser-usable subset (`manifest.assets`, which the design prompt reads); the report
    entries cover everything, including the EMF/WMF vector logos no browser can show.
    """
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    parts: dict[str, Any] = {}

    def collect(part: Any) -> None:
        for rel in part.rels.values():
            if rel.is_external:
                continue
            if rel.reltype == _IMAGE_REL:
                parts.setdefault(str(rel.target_part.partname), rel.target_part)
            elif rel.reltype == _THEME_REL:
                for theme_rel in rel.target_part.rels.values():
                    if theme_rel.reltype == _IMAGE_REL and not theme_rel.is_external:
                        parts.setdefault(str(theme_rel.target_part.partname), theme_rel.target_part)

    for master in prs.slide_masters:
        collect(master.part)
        for layout in master.slide_layouts:
            collect(layout.part)

    by_hash: dict[str, str] = {}
    used_names: set[str] = set()
    report: list[dict[str, Any]] = []
    for part_name in sorted(parts):
        blob = parts[part_name].blob
        digest = hashlib.sha256(blob).hexdigest()
        if digest in by_hash:
            report.append({"part": part_name, "file": by_hash[digest], "bytes": len(blob),
                           "duplicateOf": by_hash[digest]})
            continue
        source = Path(part_name)
        suffix = source.suffix.lower()
        name = f"asset-{_slug(source.stem)}{suffix}"
        if name in used_names:
            name = f"asset-{_slug(source.stem)}-{digest[:8]}{suffix}"
        used_names.add(name)
        (assets_dir / name).write_bytes(blob)
        by_hash[digest] = name
        report.append({
            "part": part_name, "file": name, "bytes": len(blob),
            "webRenderable": suffix in WEB_IMAGE_SUFFIXES,
        })
    assets = sorted({entry["file"] for entry in report if entry.get("webRenderable")})
    return assets, report


# ------------------------------------------------------------------------------- the import


def _assign_layout_ids(
    layouts: list[Layout], adopted: dict[tuple[int, int], str] | None, warnings: list[str]
) -> None:
    """Give every layout its id: the one a previous import used for that position, else the next free.

    Ids are `layout-NN` numbered through the whole deck in python-pptx order, which is what a fresh
    import produces and what the legacy Claude import produced too. When a project is re-imported,
    its existing ids are adopted so `slide.layoutId` keeps pointing at the same layout — an id that
    is claimed twice, or that is not of the `layout-NN` shape, is refused and reported rather than
    quietly shifting one slide onto another layout.
    """
    claimed: dict[str, tuple[int, int]] = {}
    for layout in layouts:
        wanted = (adopted or {}).get((layout.masterIndex, layout.layoutIndex))
        if not wanted:
            continue
        if wanted in claimed:
            warnings.append(
                f"layout id {wanted!r} is claimed by both position {claimed[wanted]} and "
                f"({layout.masterIndex}, {layout.layoutIndex}); the second keeps a fresh id"
            )
            continue
        claimed[wanted] = (layout.masterIndex, layout.layoutIndex)
        layout.id = wanted

    taken = set(claimed)
    number = 0
    for layout in layouts:
        if layout.id:
            continue
        number += 1
        while f"layout-{number:02d}" in taken:
            number += 1
        layout.id = f"layout-{number:02d}"
        taken.add(layout.id)


def import_master(
    pptx: Path,
    out_dir: Path,
    *,
    render: bool = True,
    renderer: Renderer | None = None,
    layout_ids: dict[tuple[int, int], str] | None = None,
) -> Manifest:
    """Read a master deck into the shared manifest, with a rendered background per layout.

    `out_dir` receives `layouts/<layout id>.png` (PptxRender, joined on `partName`),
    `assets/asset-*.<ext>` and `import-report.json`. The manifest itself is returned, not written —
    the CLI and `engine.tools.migrate_projects` decide where it belongs.

    `render=False` skips the renderer (and leaves `background` unset) for callers that only need
    the structure; every acceptance path runs with backgrounds. With `render=True`, `renderer` is
    required (`PptxRenderClient.from_settings()`, or `BlankLayoutsRenderer` offline): there is no
    process-wide renderer to fall back on.

    `layout_ids` maps `(masterIndex, layoutIndex)` to the id that position must keep. Re-importing
    the master of an existing project goes through it: the slides already store a `layoutId`, and
    renumbering the layouts would silently move every slide onto a different one.
    """
    started = time.perf_counter()
    pptx, out_dir = Path(pptx), Path(out_dir)
    if not pptx.exists():
        raise MasterImportError(f"master not found: {pptx.name}")
    if render and renderer is None:
        raise RendererError("import_master(render=True) needs a renderer; none is configured "
                            "(SLIDE_ENGINE_RENDERER_URL)")
    out_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    prs = Presentation(str(pptx))
    width_emu, height_emu = slide_size_emu(pptx)
    if width_emu <= 0 or height_emu <= 0:
        raise MasterImportError(f"{pptx.name}: <p:sldSz> is {width_emu}x{height_emu}")
    canvas = {
        "w": round(width_emu / config.EMU_PER_IN * config.PX_PER_IN),
        "h": round(height_emu / config.EMU_PER_IN * config.PX_PER_IN),
        "pxPerIn": config.PX_PER_IN,
    }

    identities = {identity.partName: identity for identity in layout_identities(pptx)}
    default_text_style = _child(prs.part._element, _p("defaultTextStyle"))

    masters: list[Master] = []
    layouts: list[Layout] = []
    palettes: list[Palette] = []
    seen_parts: set[str] = set()

    for master_index, master in enumerate(prs.slide_masters):
        master_el = master.part._element
        theme_root = _theme_root(master.part)
        palette = _palette_of(master_el, theme_root)
        palettes.append(palette)
        theme_name = (theme_root.get("name") or "").strip() if theme_root is not None else ""
        theme = {
            "name": theme_name,
            "colors": palette.colors(),
            "fonts": _theme_fonts(theme_root, warnings, master_index),
        }
        masters.append(Master(
            index=master_index,
            # Real masters almost never set `<p:cSld name>`, so the theme name is the only human
            # label available; the index stays the identity either way.
            name=_cSld_name(master_el) or theme_name or f"master-{master_index + 1}",
            partName=str(master.part.partname),
            theme=theme,
        ))
        master_phs = _placeholder_shapes(master_el)
        master_tx_styles = master_el.find(_p("txStyles"))

        for layout_index, layout in enumerate(master.slide_layouts):
            part_name = str(layout.part.partname)
            seen_parts.add(part_name)
            layout_el = layout.part._element
            name = _cSld_name(layout_el) or Path(part_name).stem
            identity = identities.get(part_name)
            if identity is None:
                raise MasterImportError(
                    f"{pptx.name}: layout part {part_name} is not among the relationships "
                    f"engine.renderer.layout_identities found — the package is inconsistent."
                )
            if (identity.masterIndex, identity.layoutIndex) != (master_index, layout_index):
                raise MasterImportError(
                    f"{pptx.name}: {part_name} is python-pptx ({master_index}, {layout_index}) but "
                    f"the package's id lists say ({identity.masterIndex}, {identity.layoutIndex})."
                )
            if identity.name != name:
                raise MasterImportError(
                    f"{pptx.name}: {part_name} is named {name!r} by python-pptx and "
                    f"{identity.name!r} by the package reader — the partName join is unsafe."
                )
            layouts.append(Layout(
                id="",  # assigned below, once every position is known
                name=name,
                masterIndex=master_index,
                layoutIndex=layout_index,
                partName=part_name,
                placeholders=_layout_placeholders(
                    layout_el, master_phs, master_tx_styles, default_text_style, palette, warnings, part_name
                ),
            ))
            layouts[-1].usage = _usage_of(layouts[-1].placeholders)

    _assign_layout_ids(layouts, layout_ids, warnings)

    orphans = sorted(set(identities) - seen_parts)
    for part_name in orphans:
        warnings.append(
            f"layout part {part_name} ({identities[part_name].name!r}) is reachable by relationship "
            f"but absent from <p:sldLayoutIdLst>; PowerPoint does not offer it, so it is not in the "
            f"manifest"
        )

    assets, asset_report = _extract_assets(prs, out_dir)

    manifest = Manifest(
        slideWidthEmu=width_emu,
        slideHeightEmu=height_emu,
        canvas=canvas,
        masters=masters,
        theme=masters[0].theme if masters else {},
        textStyles=_text_styles(prs.slide_masters[0].part._element, palettes[0]) if masters else {},
        bullets=_bullets(prs.slide_masters[0].part._element, palettes[0]) if masters else [],
        layouts=layouts,
        assets=assets,
        designNotes="",
        importer="deterministic",
        sourceHash=sha256_of(pptx),
        version=VERSION,
    )

    if render and renderer is not None:
        warnings += _render_backgrounds(renderer, pptx, manifest, out_dir, orphans)

    problems = manifest.validate(project_dir=out_dir if render else None)
    elapsed = time.perf_counter() - started
    report = {
        "master": str(pptx),
        "sourceHash": manifest.sourceHash,
        "elapsedS": round(elapsed, 3),
        "canvas": canvas,
        "masters": len(masters),
        "layouts": len(layouts),
        "placeholders": sum(len(lay.placeholders) for lay in layouts),
        "inheritedBoxes": sum(
            1 for lay in layouts for ph in lay.placeholders
            if (ph.style or {}).get("boxFrom", "").startswith("master")
        ),
        "assets": asset_report,
        "assetsInManifest": assets,
        "orphanLayouts": orphans,
        "warnings": warnings,
        "validation": problems,
    }
    (out_dir / "import-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    for warning in warnings:
        log.warning("%s: %s", pptx.name, warning)
    for problem in problems:
        log.error("%s: manifest problem: %s", pptx.name, problem)
    log.info("imported %s: %d layouts, %d masters in %.2fs", pptx.name, len(layouts), len(masters), elapsed)
    return manifest


def _layout_placeholders(
    layout_el: etree._Element,
    master_phs: list[tuple[str, int, str, etree._Element]],
    master_tx_styles: etree._Element | None,
    default_text_style: etree._Element | None,
    palette: Palette,
    warnings: list[str],
    part_name: str,
) -> list[Placeholder]:
    placeholders: list[Placeholder] = []
    left_title = (layout_el.get("type") in _LEFT_TITLE_TYPES
                  or _cSld_name(layout_el).strip().lower() in _LEFT_TITLE_NAMES)
    for ph_type, idx, name, shape in _placeholder_shapes(layout_el):
        box = _xfrm(shape)
        box_from = "layout"
        inherited = _inherit_from(ph_type, idx, master_phs)
        if box is None:
            if inherited is None:
                warnings.append(
                    f"{part_name}: placeholder {ph_type}[{idx}] {name!r} declares no <a:xfrm> and "
                    f"no master placeholder matches; its box is 0x0"
                )
                box, box_from = (0, 0, 0, 0), "missing"
            else:
                master_box = _xfrm(inherited[0])
                if master_box is None:
                    warnings.append(
                        f"{part_name}: placeholder {ph_type}[{idx}] {name!r} inherits from "
                        f"{inherited[1]}, which has no <a:xfrm> either; its box is 0x0"
                    )
                    box, box_from = (0, 0, 0, 0), "missing"
                else:
                    box, box_from = master_box, inherited[1]

        layout_txbody = _child(shape, _p("txBody"))
        master_txbody = _child(inherited[0], _p("txBody")) if inherited else None
        kind = _STYLE_KIND.get(ph_type, "other")
        # The inheritance chain of master brief §17-WP7: the layout's own overrides, then the
        # master placeholder's, then that master's txStyles list for this kind of zone, then the
        # presentation-wide default text style.
        layout_para = _para_props(_child(layout_txbody, _a("lstStyle"), _a("lvl1pPr")), palette)
        merged = _merge([
            _body_props(layout_txbody),
            _body_props(master_txbody),
            layout_para,
            _para_props(_child(master_txbody, _a("lstStyle"), _a("lvl1pPr")), palette),
            _para_props(_child(master_tx_styles, _p(f"{kind}Style"), _a("lvl1pPr")), palette),
            _para_props(_child(default_text_style, _a("lvl1pPr")), palette),
        ])
        if left_title and kind == "title" and not layout_para.get("align"):
            merged["align"] = "left"
        style = {
            "font": merged.get("font") or ("+mj-lt" if kind == "title" else "+mn-lt"),
            "sizePt": merged.get("sizePt"),
            "bold": bool(merged.get("bold") or False),
            "italic": bool(merged.get("italic") or False),
            "color": merged.get("color"),
            "align": merged.get("align") or "left",
            "anchor": merged.get("anchor") or "top",
            "autofit": merged.get("autofit") or "none",
            "wrap": merged.get("wrap") or "square",
            "lineSpacingPct": merged.get("lineSpacingPct"),
            "lineSpacingPt": merged.get("lineSpacingPt"),
            "spaceBeforePt": merged.get("spaceBeforePt"),
            "spaceAfterPt": merged.get("spaceAfterPt"),
            "marLEmu": merged.get("marLEmu"),
            "indentEmu": merged.get("indentEmu"),
            "insetsPx": {side: merged.get(f"inset-{side}", default)
                         for side, default in _DEFAULT_INSETS_PX.items()},
            "boxFrom": box_from,
        }

        x_emu, y_emu, w_emu, h_emu = box
        placeholders.append(Placeholder(
            type=ph_type,
            idx=idx,
            name=name or None,
            x=round(x_emu / config.EMU_PER_PX, 3),
            y=round(y_emu / config.EMU_PER_PX, 3),
            w=round(w_emu / config.EMU_PER_PX, 3),
            h=round(h_emu / config.EMU_PER_PX, 3),
            emu={"x": x_emu, "y": y_emu, "w": w_emu, "h": h_emu},
            style=style,
        ))
    return placeholders


def _render_backgrounds(
    renderer: Renderer, pptx: Path, manifest: Manifest, out_dir: Path, orphans: list[str]
) -> list[str]:
    """Render one background per layout and name it after the layout id, joining on `partName`."""
    warnings: list[str] = []
    layouts_dir = out_dir / "layouts"
    rendered = renderer.render_layouts(pptx, manifest.canvas_w, layouts_dir)

    index: list[dict[str, Any]] = []
    for layout in manifest.layouts:
        source = rendered.get(layout.partName or "")
        if source is None:
            raise MasterImportError(
                f"{pptx.name}: the renderer produced no background for {layout.id} "
                f"({layout.partName}); do not fall back to position — the orders differ."
            )
        target = layouts_dir / f"{layout.id}.png"
        if target.exists():
            target.unlink()
        shutil.move(str(source), str(target))
        layout.background = target.name
        index.append({
            "id": layout.id, "file": target.name, "renderFile": source.name,
            "masterIndex": layout.masterIndex, "layoutIndex": layout.layoutIndex,
            "name": layout.name, "partName": layout.partName,
        })

    for part_name in orphans:
        stray = rendered.get(part_name)
        if stray is not None and stray.exists():
            stray.unlink()
            warnings.append(f"discarded the background rendered for orphan layout {part_name}")

    (layouts_dir / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    return warnings


# ------------------------------------------------------------------------------------- ingest


def ingest(
    project_dir: Path,
    manifest: Manifest | dict[str, Any],
    files: Path | Iterable[Path] | None = None,
    *,
    clear: bool = True,
    importer: str | None = None,
) -> Manifest:
    """Land an importer's output in a project: write `layouts/`, `assets/` and `manifest.json`.

    Hands in what `import_master` produced together with its `out_dir` (or a manifest dict and the
    directory its files are in). The manifest is validated before anything is declared ready, so an
    import that lost a layout background fails here rather than three screens later.

    Backgrounds are normalised to `<layout id>.png` at canvas width — the reference composite and
    the candidate render have to share layout pixels, so a background at the wrong scale is a defect
    the gate would chase for ever.
    """
    project_dir = Path(project_dir)
    if isinstance(manifest, dict):
        manifest = Manifest.from_json(manifest)
    if importer:
        manifest.importer = importer

    # A flat directory of files, or what `import_master` wrote (`layouts/` and `assets/` beside its
    # report). Accepting both shapes means no caller has to list files by hand.
    candidates: dict[str, Path] = {}
    if files is not None:
        paths = [Path(files)] if isinstance(files, (str, Path)) else [Path(f) for f in files]
        for path in paths:
            if path.is_dir():
                for child in sorted(path.iterdir()):
                    if child.is_file():
                        candidates.setdefault(child.name, child)
                    elif child.name in ("layouts", "assets"):
                        for grandchild in sorted(child.iterdir()):
                            if grandchild.is_file():
                                candidates.setdefault(grandchild.name, grandchild)
            elif path.is_file():
                candidates.setdefault(path.name, path)

    layouts_dir, assets_dir = project_dir / "layouts", project_dir / "assets"
    for directory in (layouts_dir, assets_dir):
        if clear and directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True, exist_ok=True)

    missing: list[str] = []
    for layout in manifest.layouts:
        if not layout.background:
            continue  # a layout that genuinely draws nothing is allowed to have no background
        source = candidates.get(layout.background)
        if source is None:
            # Not tolerated: a named-but-absent background means the importer lost a file, and the
            # symptom otherwise shows up as a blank backdrop behind a finished slide.
            missing.append(f"{layout.id} -> {layout.background}")
            continue
        layout.background = _write_background(source, layouts_dir, layout.id, manifest.canvas_w, manifest.canvas_h)
    if missing:
        raise ManifestError(f"the import did not supply these layout backgrounds: {'; '.join(missing)}")

    # Everything the manifest lists, plus any stray `asset-*` an importer forgot to list. Only the
    # browser-usable ones go back into `manifest.assets`: that list is read straight into the design
    # prompt, and a slide that references an `.emf` shows a broken image.
    wanted = set(manifest.assets)
    wanted |= {name for name in candidates
               if name.startswith("asset-") and Path(name).suffix.lower() in WEB_IMAGE_SUFFIXES}
    written: list[str] = []
    for name in sorted(wanted):
        source = candidates.get(name)
        if source is None:
            log.warning("ingest: asset %r was listed but not supplied", name)
            continue
        safe = _slug(Path(name).stem) + Path(name).suffix.lower()
        shutil.copy2(source, assets_dir / safe)
        written.append(safe)
    manifest.assets = sorted({name for name in written
                              if Path(name).suffix.lower() in WEB_IMAGE_SUFFIXES})

    problems = manifest.validate(project_dir=project_dir)
    if problems:
        raise ManifestError(f"the imported manifest is not usable: {'; '.join(problems)}")
    manifest.save(project_dir / "manifest.json")
    return manifest


def _write_background(source: Path, layouts_dir: Path, layout_id: str, canvas_w: int, canvas_h: int) -> str:
    """Copy one layout background into the project, as `<layout id>.png` at exactly canvas size."""
    if source.suffix.lower() in (".html", ".htm"):
        target = layouts_dir / f"{layout_id}.html"
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        return target.name
    from PIL import Image  # imported here: only an ingest of raster backgrounds needs Pillow

    target = layouts_dir / f"{layout_id}.png"
    with Image.open(source) as opened:
        image = opened.convert("RGB")
        if image.size != (canvas_w, canvas_h):
            image = image.resize((canvas_w, canvas_h))
        image.save(target)
    return target.name
