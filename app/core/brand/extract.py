"""Brand extraction from a `.pptx` template: everything needed to apply a brand to generated slides.

`extract(data)` reads a master deck and returns its theme colours and fonts, slide layouts (with
placeholder bounds as 0-1 fractions), the typography scale, master decorations and logo shapes, the
workzone, and `capturedFurniture`: the raw shape XML, media, theme and backgrounds that
`app.core.brand.synth_master.synthesize_master` turns back into a master (this module is its
inverse). Works on any platform through python-pptx + lxml; no rendering, no PowerPoint.

There is no HTTP route here (rev 3: Slide Studio's `/v1/brand-extract` is gone). The brand-pptx and
master-import flows call `extract` internally, off the event loop: it is synchronous, CPU-bound work.

The optional role-annotation pass labels every placeholder and decoration (title, body, logo, source
line, ...) with one model call. It goes through a `RoleAnnotator` the caller passes in (built on the
LLM provider port, so tests hand in a fake); without one, the pass is skipped and roles stay `None`,
which is the production behaviour today.

Ported from Slide Studio `server/brand/brand_extract.py`, itself vendored from the retiring OCR
service (migration plan §4.1, K). FastAPI, the settings lookup and the direct provider SDK call are
gone; the size cap, magic-byte check and output shape are unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from collections.abc import Callable, Iterator
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from typing import Any

from lxml import etree
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER

logger = logging.getLogger(__name__)

_PPTX_MAGIC = b'PK\x03\x04'  # PPTX / ZIP magic bytes
#: The largest master accepted, in bytes (20 MB).
MAX_BYTES = 20 * 1024 * 1024

#: Given the full role-annotation prompt, return the model's raw reply text (JSON:
#: `{"roles": [{"id", "role"}, ...]}`). Built by the caller on the LLM provider port.
RoleAnnotator = Callable[[str], str]

#: Parser for theme XML read out of an uploaded file: no entity expansion, no network.
_SAFE_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)


class BrandExtractError(ValueError):
    """The file cannot be read as a master. The message is short and safe to show a caller."""


_A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
_P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
_THEME_RELTYPE = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme'

# All 12 theme color slots in display order.
_COLOR_ROLES = [
    ('dk1', 'Dark 1'),
    ('lt1', 'Light 1'),
    ('dk2', 'Dark 2'),
    ('lt2', 'Light 2'),
    ('accent1', 'Accent 1'),
    ('accent2', 'Accent 2'),
    ('accent3', 'Accent 3'),
    ('accent4', 'Accent 4'),
    ('accent5', 'Accent 5'),
    ('accent6', 'Accent 6'),
    ('hlink', 'Hyperlink'),
    ('folHlink', 'Followed Link'),
]

# OOXML logical aliases: these slot names resolve to the canonical slot.
_SLOT_ALIASES: dict[str, str] = {
    'tx1': 'dk1',
    'tx2': 'dk2',
    'bg1': 'lt1',
    'bg2': 'lt2',
}

# EMU per point — duplicated here to avoid import from config (no circular dep).
_EMU_PER_PT = 12_700


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _theme_element(prs: Any) -> Any:
    """Return the parsed <a:theme> lxml element from the slide master's theme part."""
    try:
        theme_part = prs.slide_master.part.part_related_by(_THEME_RELTYPE)
        return etree.fromstring(theme_part.blob, _SAFE_PARSER)
    except Exception:
        logger.debug("theme part could not be read", exc_info=True)
        return None


def _emu_frac(emu: int | None, dim: int) -> float:
    """Convert an EMU value to a 0.0–1.0 fraction of slide dimension."""
    if emu is None or dim == 0:
        return 0.0
    return round(max(0.0, min(1.0, emu / dim)), 4)


def _hex_from_clr_el(el: Any) -> str | None:
    """Extract #RRGGBB from an <a:srgbClr> or <a:sysClr> child element."""
    if el is None:
        return None
    srgb = el.find(f'{{{_A}}}srgbClr')
    if srgb is not None:
        val = srgb.get('val', '')
        if len(val) == 6:
            return f'#{val.upper()}'
    sysclr = el.find(f'{{{_A}}}sysClr')
    if sysclr is not None:
        val = sysclr.get('lastClr', '')
        if len(val) == 6:
            return f'#{val.upper()}'
    return None


# ---------------------------------------------------------------------------
# Slot map — foundation for all color resolution
# ---------------------------------------------------------------------------

def _build_slot_map(theme_el: Any) -> dict[str, str]:
    """Build a dict mapping all theme color slot names → #RRGGBB hex.

    Includes the 4 OOXML logical aliases (tx1=dk1, tx2=dk2, bg1=lt1, bg2=lt2).
    Returns {} when theme_el is None or the clrScheme is absent.
    """
    if theme_el is None:
        return {}
    clr_scheme = theme_el.find(f'.//{{{_A}}}clrScheme')
    if clr_scheme is None:
        return {}

    result: dict[str, str] = {}
    for slot, _ in _COLOR_ROLES:
        el = clr_scheme.find(f'{{{_A}}}{slot}')
        hex_val = _hex_from_clr_el(el)
        if hex_val:
            result[slot] = hex_val

    for alias, canonical in _SLOT_ALIASES.items():
        if canonical in result:
            result[alias] = result[canonical]

    return result


def _resolve_scheme_color(slot_val: str | None, slot_map: dict[str, str]) -> str | None:
    """Resolve a scheme color slot name (e.g. 'accent1') to #RRGGBB."""
    if not slot_val:
        return None
    return slot_map.get(slot_val)


# ---------------------------------------------------------------------------
# Extraction functions
# ---------------------------------------------------------------------------

def _is_near_white(hex_color: str) -> bool:
    """True when a color is too light to serve as the brand's primary
    structural color (relative luminance > 0.85)."""
    h = hex_color.lstrip('#')
    if len(h) != 6:
        return False
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) > 0.85


def _extract_colors(
    prs: Any,
    theme_el: Any,
    slot_map: dict[str, str],
) -> tuple[list[dict[str, Any]], str, str]:
    """Return (all_colors, primary_hex, accent_hex).

    Primary heuristic: prefer dk2 over dk1 because dk1 is the default text
    color (near-black) in the OOXML spec, while dk2 is almost always the
    brand's primary structural color in real templates.
    """
    all_colors: list[dict[str, Any]] = []
    by_slot: dict[str, str] = {}

    if theme_el is not None:
        clr_scheme = theme_el.find(f'.//{{{_A}}}clrScheme')
        if clr_scheme is not None:
            for slot, role in _COLOR_ROLES:
                el = clr_scheme.find(f'{{{_A}}}{slot}')
                hex_val = _hex_from_clr_el(el)
                if hex_val:
                    all_colors.append({'role': role, 'hex': hex_val.lstrip('#')})
                    by_slot[slot] = hex_val

    # dk2 is preferred over dk1: dk1 is always near-black (default text color),
    # dk2 is typically the brand's primary non-black color. But themes designed
    # for dark covers map dk2/lt2 to WHITE (e.g. dk2=lt2=#FFFFFF,
    # navy in accent1) — a near-white "primary" flips generated covers to
    # white-on-white, so skip candidates that can't serve as a structural color.
    primary = next(
        (c for c in (by_slot.get('dk2'), by_slot.get('accent1'), by_slot.get('dk1'))
         if c and not _is_near_white(c)),
        '#1F3A5F',
    )
    accent = by_slot.get('accent1') or by_slot.get('accent2') or '#2E7D9A'
    return all_colors, primary, accent


def _extract_fonts(prs: Any) -> tuple[str | None, str | None]:
    """Return (heading_font, body_font) from the theme font scheme."""
    theme_el = _theme_element(prs)
    if theme_el is None:
        return None, None

    font_scheme = theme_el.find(f'.//{{{_A}}}fontScheme')
    if font_scheme is None:
        return None, None

    def _latin(parent_tag: str) -> str | None:
        parent = font_scheme.find(f'{{{_A}}}{parent_tag}')
        if parent is None:
            return None
        latin = parent.find(f'{{{_A}}}latin')
        if latin is None:
            return None
        tf = latin.get('typeface', '')
        # +mj-lt / +mn-lt are theme slot references, not real font names
        return tf if tf and not tf.startswith('+') else None

    return _latin('majorFont'), _latin('minorFont')


def _extract_typography_scale(
    prs: Any, slot_map: dict[str, str]
) -> dict[str, Any]:
    """Extract default font sizes, weights, and colors from p:txStyles.

    p:txStyles is exclusive to the slide master — it is absent from layouts.
    Font size is stored in hundredths of a point (sz='2400' = 24pt).
    Returns {} on any failure so the response degrades gracefully.
    """
    try:
        master_el = prs.slide_master.element
        tx_styles = master_el.find(f'{{{_P}}}txStyles')
        if tx_styles is None:
            return {}

        result: dict[str, Any] = {}
        for style_tag, key in [
            (f'{{{_P}}}titleStyle', 'title'),
            (f'{{{_P}}}bodyStyle', 'body'),
            (f'{{{_P}}}otherStyle', 'other'),
        ]:
            style_el = tx_styles.find(style_tag)
            entry: dict[str, Any] = {
                'sizePt': None, 'bold': False,
                'colorSchemeSlot': None, 'colorHex': None,
            }
            if style_el is not None:
                lvl1 = style_el.find(f'.//{{{_A}}}lvl1pPr')
                if lvl1 is not None:
                    def_rpr = lvl1.find(f'{{{_A}}}defRPr')
                    if def_rpr is not None:
                        sz_raw = def_rpr.get('sz')
                        if sz_raw:
                            entry['sizePt'] = round(int(sz_raw) / 100, 1)
                        entry['bold'] = def_rpr.get('b') == '1'
                        solid = def_rpr.find(f'{{{_A}}}solidFill')
                        if solid is not None:
                            scheme = solid.find(f'{{{_A}}}schemeClr')
                            if scheme is not None:
                                slot = scheme.get('val')
                                entry['colorSchemeSlot'] = slot
                                entry['colorHex'] = _resolve_scheme_color(slot, slot_map)
                            else:
                                srgb = solid.find(f'{{{_A}}}srgbClr')
                                if srgb is not None:
                                    val = srgb.get('val', '')
                                    if len(val) == 6:
                                        entry['colorHex'] = f'#{val.upper()}'
            result[key] = entry

        return result
    except Exception:
        logger.debug('Typography scale extraction failed', exc_info=True)
        return {}


def _detect_layout_background(
    layout: Any, slot_map: dict[str, str]
) -> tuple[str, str | None]:
    """Detect a layout's background type and resolved color.

    Returns:
        ("inherit", None)       — no p:bg; inherits from master (standard content)
        ("solid", "#RRGGBB"|None) — explicit solid fill; hex when resolvable
        ("theme-ref", None)     — references theme style matrix (not resolved here)
    """
    try:
        csld = layout.element.find(f'{{{_P}}}cSld')
        if csld is None:
            return 'inherit', None
        bg = csld.find(f'{{{_P}}}bg')
        if bg is None:
            return 'inherit', None

        bg_pr = bg.find(f'{{{_P}}}bgPr')
        if bg_pr is not None:
            solid = bg_pr.find(f'{{{_A}}}solidFill')
            if solid is not None:
                srgb = solid.find(f'{{{_A}}}srgbClr')
                if srgb is not None:
                    val = srgb.get('val', '')
                    if len(val) == 6:
                        return 'solid', f'#{val.upper()}'
                scheme = solid.find(f'{{{_A}}}schemeClr')
                if scheme is not None:
                    slot = scheme.get('val')
                    return 'solid', _resolve_scheme_color(slot, slot_map)
                return 'solid', None

        if bg.find(f'{{{_P}}}bgRef') is not None:
            return 'theme-ref', None

        return 'inherit', None
    except Exception:
        logger.debug('Layout background detection failed', exc_info=True)
        return 'inherit', None


def _detect_layout_color_map_override(layout: Any) -> str:
    """Return 'override' if the layout remaps accent slots, else 'passthrough'."""
    try:
        clr_map_ovr = layout.element.find(f'{{{_P}}}clrMapOvr')
        if clr_map_ovr is None:
            return 'passthrough'
        if clr_map_ovr.find(f'{{{_A}}}overrideClrMapping') is not None:
            return 'override'
        return 'passthrough'
    except Exception:
        return 'passthrough'


def _picture_blob_b64(shape: Any) -> tuple[str | None, str | None]:
    """Return (base64_string, content_type) for a PICTURE shape, or (None, None)."""
    try:
        img = shape.image
        return base64.b64encode(img.blob).decode('ascii'), img.content_type
    except Exception:
        logger.debug("picture shape has no readable image", exc_info=True)
        return None, None


def _collect_pictures_from_shapes(shapes: Any, slide_w: int, slide_h: int,
                                   layout_index: int | None, layout_name: str | None,
                                   _depth: int = 0) -> list[dict[str, Any]]:
    """Recursively walk shapes (including GROUP children) and collect PICTURE blobs.

    Returns a list of image dicts with position, size, base64 data, and content type.
    Recurses one level into GROUP shapes only to avoid pathological nesting.
    """
    results: list[dict[str, Any]] = []
    for sh in shapes:
        try:
            is_placeholder = False
            try:
                is_placeholder = sh.placeholder_format is not None
            except Exception:
                logger.debug('brand extract: an optional read failed', exc_info=True)
            if is_placeholder:
                continue

            if sh.shape_type == MSO_SHAPE_TYPE.PICTURE:
                b64, ctype = _picture_blob_b64(sh)
                if b64 is None:
                    continue
                if sh.left is None or sh.width is None:
                    continue
                results.append({
                    'layoutIndex': layout_index,
                    'layoutName': layout_name,
                    'left': _emu_frac(sh.left, slide_w),
                    'top': _emu_frac(sh.top, slide_h),
                    'width': _emu_frac(sh.width, slide_w),
                    'height': _emu_frac(sh.height, slide_h),
                    'contentType': ctype,
                    'imageBase64': b64,
                    'role': None,
                })
            elif sh.shape_type == MSO_SHAPE_TYPE.GROUP and _depth == 0:
                try:
                    results.extend(_collect_pictures_from_shapes(
                        sh.shapes, slide_w, slide_h, layout_index, layout_name, _depth + 1
                    ))
                except Exception:
                    logger.debug('brand extract: an optional read failed', exc_info=True)
        except Exception:
            logger.debug("skipping a layout shape", exc_info=True)
            continue
    return results


def _extract_master_images(prs: Any) -> list[dict[str, Any]]:
    """Collect all PICTURE blobs from the slide master and every layout.

    Returns a flat list ordered master-first, then layouts in index order.
    Deduplicates by image blob hash so the same graphic shared across many
    layouts is only returned once (keeping the first occurrence).
    """
    slide_w = prs.slide_width
    slide_h = prs.slide_height
    seen_hashes: set[int] = set()
    results: list[dict[str, Any]] = []

    def _add(entries: list[dict[str, Any]]) -> None:
        for entry in entries:
            h = hash(entry['imageBase64'])
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            results.append(entry)

    # Master-level shapes
    _add(_collect_pictures_from_shapes(
        prs.slide_master.shapes, slide_w, slide_h, None, None
    ))

    # Layout-level shapes
    for idx, layout in enumerate(prs.slide_master.slide_layouts):
        _add(_collect_pictures_from_shapes(
            layout.shapes, slide_w, slide_h, idx, layout.name
        ))

    return results


def _extract_layouts(prs: Any, slot_map: dict[str, str]) -> list[dict[str, Any]]:
    """Return a catalog of slide layouts with placeholder bounds and background info.

    Each layout dict now also contains a ``decorations`` list of non-placeholder
    shapes (e.g. fixed logo images, accent bands) positioned in fractions of the
    slide. Roles for both placeholders and decorations are filled in later by
    ``_annotate_layout_roles``; until then they are ``None``.
    """
    slide_w = prs.slide_width
    slide_h = prs.slide_height
    result = []

    for idx, layout in enumerate(prs.slide_master.slide_layouts):
        placeholders = []
        for ph in layout.placeholders:
            try:
                fmt = ph.placeholder_format
                if fmt is None:
                    continue
                ph_type = fmt.type.name if fmt.type is not None else 'OBJECT'
                if ph.left is None or ph.top is None or ph.width is None or ph.height is None:
                    continue
                ph_dict = {
                    'type': ph_type,
                    'left': _emu_frac(ph.left, slide_w),
                    'top': _emu_frac(ph.top, slide_h),
                    'width': _emu_frac(ph.width, slide_w),
                    'height': _emu_frac(ph.height, slide_h),
                    'role': None,
                }
                try:
                    text = ph.text_frame.text.strip() if ph.has_text_frame else ''
                    if text:
                        ph_dict['text'] = text
                except Exception:
                    logger.debug('brand extract: an optional read failed', exc_info=True)
                placeholders.append(ph_dict)
            except Exception:
                logger.debug("skipping a placeholder", exc_info=True)
                continue

        # Non-placeholder shapes fixed into the layout (logos, decorative bands, etc.)
        decorations = []
        for sh in layout.shapes:
            try:
                fmt = sh.placeholder_format
                if fmt is not None:
                    continue
            except Exception:  # noqa: BLE001
                fmt = None  # python-pptx raises for a shape that is not a placeholder: include it
            try:
                if sh.left is None or sh.width is None or sh.height is None:
                    continue
                l_f = _emu_frac(sh.left, slide_w)
                t_f = _emu_frac(sh.top, slide_h)
                w_f = _emu_frac(sh.width, slide_w)
                h_f = _emu_frac(sh.height, slide_h)
                if w_f < 0.001 and h_f < 0.001:
                    continue  # zero-size OLE / DRM artefacts
                try:
                    stype = sh.shape_type.name
                except Exception:
                    stype = 'UNKNOWN'
                dec_dict = {
                    'type': stype,
                    'left': l_f,
                    'top': t_f,
                    'width': w_f,
                    'height': h_f,
                    'role': None,
                }
                try:
                    text = sh.text_frame.text.strip() if sh.has_text_frame else ''
                    if text:
                        dec_dict['text'] = text
                except Exception:
                    logger.debug('brand extract: an optional read failed', exc_info=True)
                # For PICTURE shapes store a short content hash so logo
                # deduplication can group identical images that have slightly
                # different coordinates across layouts.
                if stype == 'PICTURE':
                    try:
                        dec_dict['_imageHash'] = hashlib.sha1(sh.image.blob, usedforsecurity=False).hexdigest()[:12]
                    except Exception:
                        logger.debug('brand extract: an optional read failed', exc_info=True)
                decorations.append(dec_dict)
            except Exception:
                logger.debug("skipping a decoration shape", exc_info=True)
                continue

        bg_type, bg_color = _detect_layout_background(layout, slot_map)
        color_map_override = _detect_layout_color_map_override(layout)

        result.append({
            'name': layout.name,
            'index': idx,
            'placeholders': placeholders,
            'decorations': decorations,
            'backgroundType': bg_type,
            'backgroundColor': bg_color,
            'colorMapOverride': color_map_override,
        })

    return result


def _resolve_shape_fill(
    shape: Any, slot_map: dict[str, str]
) -> tuple[str, str | None, str | None]:
    """Resolve a shape's fill via raw lxml, since FillFormat.type returns None
    for inherited fills.

    Resolution chain:
        p:spPr → a:noFill          → ("none", None, None)
        p:spPr → a:solidFill       → ("solid"|"scheme", slot, hex)
        p:spPr → a:gradFill        → ("gradient", None, None)
        p:spPr → a:blipFill        → ("picture", None, None)
        p:style → a:fillRef        → ("theme-ref", slot, hex)
        fallback                   → ("none", None, None)

    Returns (fill_type, fill_scheme_slot, fill_hex).
    """
    try:
        sp_el = shape.element
        sp_pr = sp_el.find(f'{{{_P}}}spPr')

        if sp_pr is not None:
            if sp_pr.find(f'{{{_A}}}noFill') is not None:
                return 'none', None, None

            solid = sp_pr.find(f'{{{_A}}}solidFill')
            if solid is not None:
                srgb = solid.find(f'{{{_A}}}srgbClr')
                if srgb is not None:
                    val = srgb.get('val', '')
                    if len(val) == 6:
                        return 'solid', None, f'#{val.upper()}'
                scheme = solid.find(f'{{{_A}}}schemeClr')
                if scheme is not None:
                    slot = scheme.get('val')
                    return 'scheme', slot, _resolve_scheme_color(slot, slot_map)
                return 'solid', None, None

            if sp_pr.find(f'{{{_A}}}gradFill') is not None:
                return 'gradient', None, None

            if sp_pr.find(f'{{{_A}}}blipFill') is not None:
                return 'picture', None, None

        # Fall through to style/fillRef for theme-inherited fills.
        style_el = sp_el.find(f'{{{_P}}}style')
        if style_el is not None:
            fill_ref = style_el.find(f'{{{_A}}}fillRef')
            if fill_ref is not None:
                idx = int(fill_ref.get('idx', '0'))
                if idx == 0:
                    return 'none', None, None
                scheme = fill_ref.find(f'{{{_A}}}schemeClr')
                if scheme is not None:
                    slot = scheme.get('val')
                    return 'theme-ref', slot, _resolve_scheme_color(slot, slot_map)
                return 'theme-ref', None, None

        return 'none', None, None
    except Exception:
        logger.debug('Shape fill resolution failed', exc_info=True)
        return 'none', None, None


def _extract_master_decorations(
    prs: Any, slot_map: dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Walk master shapes and extract non-placeholder decorations and logo images.

    Returns (master_decorations, logo_shapes).
    logo_shapes is the subset of master_decorations where shapeType == 'PICTURE'.
    """
    slide_w = prs.slide_width
    slide_h = prs.slide_height
    master_decorations: list[dict[str, Any]] = []
    logo_shapes: list[dict[str, Any]] = []

    for shape in prs.slide_master.shapes:
        try:
            if shape.placeholder_format is not None:
                continue

            left_f = _emu_frac(shape.left, slide_w)
            top_f = _emu_frac(shape.top, slide_h)
            width_f = _emu_frac(shape.width, slide_w)
            height_f = _emu_frac(shape.height, slide_h)

            is_picture = shape.shape_type == MSO_SHAPE_TYPE.PICTURE

            if is_picture:
                logo_entry = {
                    'name': shape.name,
                    'left': left_f,
                    'top': top_f,
                    'width': width_f,
                    'height': height_f,
                }
                logo_shapes.append(logo_entry)
                master_decorations.append({
                    **logo_entry,
                    'shapeType': 'PICTURE',
                    'fillType': 'picture',
                    'fillSchemeSlot': None,
                    'fillHex': None,
                    'lineWidthPt': 0.0,
                })
                continue

            try:
                shape_type_name = shape.shape_type.name
            except Exception:
                shape_type_name = 'UNKNOWN'

            fill_type, fill_scheme_slot, fill_hex = _resolve_shape_fill(shape, slot_map)

            line_width_pt = 0.0
            try:
                w = shape.line.width
                if w:
                    line_width_pt = round(w / _EMU_PER_PT, 2)
            except Exception:
                logger.debug('brand extract: an optional read failed', exc_info=True)

            master_decorations.append({
                'name': shape.name,
                'shapeType': shape_type_name,
                'left': left_f,
                'top': top_f,
                'width': width_f,
                'height': height_f,
                'fillType': fill_type,
                'fillSchemeSlot': fill_scheme_slot,
                'fillHex': fill_hex,
                'lineWidthPt': line_width_pt,
            })

        except Exception:
            logger.debug('Skipping master shape due to error', exc_info=True)
            continue

    return master_decorations, logo_shapes


# ---------------------------------------------------------------------------
# Layout role annotation (one text-only model call, no image rendering)
# ---------------------------------------------------------------------------

_LAYOUT_ROLE_PROMPT = """\
You are analyzing the slide master/layout structure of a professional consulting \
or business presentation (PowerPoint).

Below is a JSON array of every shape found across all layouts — both placeholder
shapes (typed by PPTX) and non-placeholder decoration shapes (fixed images,
accent bands, etc.).  Each item has:

  id              – unique identifier for this shape
  layout          – "index:name" of the layout it belongs to
  pptxType        – PPTX placeholder type (TITLE, BODY, PICTURE, SLIDE_NUMBER …)
                    or the MSO shape type for non-placeholders (e.g. PICTURE, AUTO_SHAPE)
  isDecoration    – true = non-placeholder (fixed in layout); false = placeholder
  left/top        – position as fraction of slide width/height (0.0 = left/top edge)
  width/height    – size as fraction of slide
  appearsInCount  – how many layouts contain a shape at this exact position
  text            – (optional) the text content of the shape, if any

Assign a single semantic role to each shape from this list:
  title            – primary slide heading (large, typically near top)
  slide_heading    – secondary heading / subtitle
  header_label     – small decorative label strip (project/chapter name, top corner, very thin)
  body             – main content area (bullets, paragraphs, tables)
  source           – footnote, source citation, or disclaimer (thin strip, very bottom, height < 5%;
                     text often starts with "Source:", "Note:", or a footnote number like "1)")
  slide_number     – slide/page number indicator (SLIDE_NUMBER type, or small box in bottom corner
                     containing a number)
  logo             – primary company/brand logo image (non-placeholder PICTURE in a corner,
                     MUST appear in many layouts — appearsInCount ≥ 3)
  logo_mark        – small secondary brand mark (tiny non-placeholder PICTURE, bottom area,
                     width < 0.08, MUST appear in multiple layouts — appearsInCount ≥ 2)
  background       – full-bleed background image (PICTURE with left≈0, top≈0, width≈1, height≈1)
  content_picture  – picture placeholder for user-inserted photos/images in a content zone
  decoration       – purely decorative shape (accent line, colored band, watermark)

Rules (apply in order, first match wins):
  - SLIDE_NUMBER type → always "slide_number"
  - PICTURE with left < 0.05 and top < 0.05 and width > 0.90 and height > 0.90 → "background"
  - Non-placeholder PICTURE, appearsInCount ≥ 3, in a corner (left > 0.70 or top > 0.70) → "logo"
  - Non-placeholder PICTURE, appearsInCount ≥ 2, very small (width < 0.08 and height < 0.08) → "logo_mark"
  - Non-placeholder PICTURE, appearsInCount < 2 → "content_picture" or "decoration" (not a logo)
  - BODY with top > 0.88 and height < 0.06, or text starts with "Source"/"Note"/"1)" → "source"
  - BODY with top < 0.10 and left > 0.70 and width < 0.25 → "header_label"
  - BODY with top < 0.20 and width > 0.40 → "slide_heading"
  - PICTURE placeholder covering a partial area (not full-bleed) → "content_picture"

Return ONLY valid JSON: {"roles": [{"id": "<id>", "role": "<role>"}, ...]}\
"""


def _build_annotation_payload(
    layouts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[tuple[int, str, int]]]]:
    """Flatten all shapes from all layouts into a **deduplicated** list for the model.

    Shapes with identical (type, left, top, width, height) across multiple layouts
    are merged into one entry so the model is sent far fewer items.

    Returns:
        shapes_for_model   – deduplicated list, each with a stable id
        reverse_index      – maps shape_id → list of (layout_index, kind, item_index)
                             so one id can fan out to many instances on write-back
    """
    # sig → {"entry": model_shape_dict, "instances": [(lidx, kind, item_idx)]}
    sig_map: dict[str, dict[str, Any]] = {}

    for layout in layouts:
        lidx = layout['index']
        lname = layout['name']
        for i, ph in enumerate(layout['placeholders']):
            sig = f"{ph['type']}:{ph['left']:.3f},{ph['top']:.3f},{ph['width']:.3f},{ph['height']:.3f}"
            if sig not in sig_map:
                sig_map[sig] = {
                    'entry': {
                        'id': f"s{len(sig_map)}",
                        'exampleLayout': f"{lidx}:{lname}",
                        'pptxType': ph['type'],
                        'isDecoration': False,
                        'left': ph['left'],
                        'top': ph['top'],
                        'width': ph['width'],
                        'height': ph['height'],
                        'appearsInCount': 0,
                    },
                    'instances': [],
                }
            sig_map[sig]['entry']['appearsInCount'] += 1
            sig_map[sig]['instances'].append((lidx, 'placeholder', i))

        for i, dec in enumerate(layout.get('decorations', [])):
            sig = f"DEC:{dec['type']}:{dec['left']:.3f},{dec['top']:.3f},{dec['width']:.3f},{dec['height']:.3f}"
            if sig not in sig_map:
                sig_map[sig] = {
                    'entry': {
                        'id': f"s{len(sig_map)}",
                        'exampleLayout': f"{lidx}:{lname}",
                        'pptxType': dec['type'],
                        'isDecoration': True,
                        'left': dec['left'],
                        'top': dec['top'],
                        'width': dec['width'],
                        'height': dec['height'],
                        'appearsInCount': 0,
                    },
                    'instances': [],
                }
            sig_map[sig]['entry']['appearsInCount'] += 1
            sig_map[sig]['instances'].append((lidx, 'decoration', i))

    shapes_out = [v['entry'] for v in sig_map.values()]
    reverse_index = {v['entry']['id']: v['instances'] for v in sig_map.values()}
    return shapes_out, reverse_index


def role_prompt(shapes: list[dict[str, Any]]) -> str:
    """The full role-annotation prompt for these (deduplicated) shapes."""
    return _LAYOUT_ROLE_PROMPT + "\n\nShapes:\n" + json.dumps(shapes, separators=(',', ':'))


def _call_annotator_for_roles(shapes: list[dict[str, Any]], annotate: RoleAnnotator) -> dict[str, str]:
    """One model call -> {shape_id: role}. Any failure or unreadable reply gives {} (logged)."""
    try:
        raw = (annotate(role_prompt(shapes)) or "").strip()
        data = json.loads(raw)
        roles = data.get("roles", []) if isinstance(data, dict) else []
        return {
            str(item["id"]): str(item["role"])
            for item in roles
            if isinstance(item, dict) and "id" in item and "role" in item
        }
    except Exception:  # noqa: BLE001 - the annotation is optional; the extraction stands without it
        logger.warning("layout role annotation failed; roles stay unannotated", exc_info=True)
        return {}


def _annotate_layout_roles(
    layouts: list[dict[str, Any]],
    annotate: RoleAnnotator,
) -> list[dict[str, Any]]:
    """Use a single model call (`annotate`) to assign semantic roles to every placeholder
    and decoration across all layouts.  Roles are written in-place onto each
    shape dict.  Returns ``logo_shapes`` — deduplicated list of non-placeholder
    PICTURE shapes that the model identified as logo or logo_mark.
    """
    shapes, reverse_index = _build_annotation_payload(layouts)
    if not shapes:
        return []

    role_map = _call_annotator_for_roles(shapes, annotate)
    if not role_map:
        return []

    # Write roles back onto every instance of each deduplicated shape
    layout_by_idx = {lay['index']: lay for lay in layouts}
    for sid, instances in reverse_index.items():
        role = role_map.get(sid)
        if role is None:
            continue
        for (lidx, kind, item_idx) in instances:
            layout = layout_by_idx.get(lidx)
            if layout is None:
                continue
            bucket = layout['placeholders'] if kind == 'placeholder' else layout.get('decorations', [])
            if item_idx < len(bucket):
                bucket[item_idx]['role'] = role

    # Collect deduplicated logo shapes from layout decorations.
    # Primary key: image content hash (_imageHash) — identical images that
    # sit at slightly different pixel positions across layouts collapse to one.
    # Fallback key: rounded position, for shapes where the blob wasn't readable.
    # Also requires appearsInCount ≥ 2 to exclude single-layout content pictures.
    sig_counts: dict[str, int] = {}
    for layout in layouts:
        for dec in layout.get('decorations', []):
            if dec['type'] != 'PICTURE':
                continue
            sig3 = f"DEC:PICTURE:{dec['left']:.3f},{dec['top']:.3f},{dec['width']:.3f},{dec['height']:.3f}"
            sig_counts[sig3] = sig_counts.get(sig3, 0) + 1

    # Collect all logo/logo_mark candidates, tracking how often each image appears.
    # Key: image content hash (or rounded position as fallback).
    candidates: dict[str, dict[str, Any]] = {}  # dedup_key → {shape_info + count}
    for layout in layouts:
        for dec in layout.get('decorations', []):
            if dec.get('role') not in ('logo', 'logo_mark'):
                continue
            if dec['type'] != 'PICTURE':
                continue
            sig3 = f"DEC:PICTURE:{dec['left']:.3f},{dec['top']:.3f},{dec['width']:.3f},{dec['height']:.3f}"
            if sig_counts.get(sig3, 1) < 2:
                continue

            dedup_key = dec.get('_imageHash') or \
                f"{dec['left']:.2f},{dec['top']:.2f},{dec['width']:.2f},{dec['height']:.2f}"

            count = sig_counts.get(sig3, 1)
            if dedup_key not in candidates or count > candidates[dedup_key]['_count']:
                # Post-correct: a shape the model called "logo" but is tiny → logo_mark
                role = dec['role']
                if role == 'logo' and dec['width'] < 0.08 and dec['height'] < 0.08:
                    role = 'logo_mark'
                candidates[dedup_key] = {
                    'left': dec['left'],
                    'top': dec['top'],
                    'width': dec['width'],
                    'height': dec['height'],
                    'role': role,
                    '_count': count,
                }

    # For each role keep only the single most-common image (highest appearsInCount).
    # Consulting decks have one primary logo and at most one logo_mark.
    best: dict[str, dict[str, Any]] = {}
    for entry in candidates.values():
        role = entry['role']
        if role not in best or entry['_count'] > best[role]['_count']:
            best[role] = entry

    logo_shapes = [
        {k: v for k, v in entry.items() if not k.startswith('_')}
        for entry in best.values()
    ]

    return logo_shapes


def _workzone_from_placeholders(
    prs: Any, slot_map: dict[str, str]
) -> dict[str, Any] | None:
    """Derive workzone from placeholder bounds on the first 'inherit' layout.

    Returns a conservative approximation: the union bounding box of all
    placeholders on the first layout that inherits its background from the
    master (i.e. a standard content layout, not a section divider).
    """
    slide_w = prs.slide_width
    slide_h = prs.slide_height

    for layout in prs.slide_master.slide_layouts:
        try:
            bg_type, _ = _detect_layout_background(layout, slot_map)
            if bg_type != 'inherit':
                continue

            coords: list[tuple[int, int, int, int]] = []
            for ph in layout.placeholders:
                try:
                    if ph.left is None or ph.top is None or ph.width is None or ph.height is None:
                        continue
                    coords.append((ph.left, ph.top, ph.left + ph.width, ph.top + ph.height))
                except Exception:
                    logger.debug('brand extract: skipped a layout', exc_info=True)
                    continue

            if not coords:
                continue

            return {
                'left': _emu_frac(min(c[0] for c in coords), slide_w),
                'top': _emu_frac(min(c[1] for c in coords), slide_h),
                'right': _emu_frac(max(c[2] for c in coords), slide_w),
                'bottom': _emu_frac(max(c[3] for c in coords), slide_h),
            }
        except Exception:
            logger.debug("skipping a layout while deriving the workzone", exc_info=True)
            continue

    return None


def _extract_workzone(prs: Any, slot_map: dict[str, str]) -> dict[str, Any] | None:
    """Detect the safe content zone as 0–1 slide fractions.

    Tries PowerPoint guide rails first (the designer's explicit intent), then
    falls back to deriving the zone from placeholder bounds on the first
    'inherit' layout. Returns None only if both attempts fail.
    """
    slide_w = prs.slide_width
    slide_h = prs.slide_height
    center_x = slide_w / 2
    center_y = slide_h / 2

    def _read_guides(el: Any) -> tuple[list[int], list[int]] | None:
        guides_el = el.find(f'{{{_P}}}guides')
        if guides_el is None:
            return None
        v: list[int] = []
        h: list[int] = []
        for g in guides_el.findall(f'{{{_P}}}guide'):
            orient = g.get('orient', 'vert')
            try:
                pos = int(g.get('pos', '0'))
            except ValueError:
                continue
            (v if orient == 'vert' else h).append(pos)
        return (v, h) if v and h else None

    def _straddles(positions: list[int], center: float) -> bool:
        return any(p < center - 1 for p in positions) and any(p > center + 1 for p in positions)

    for el in (prs.slide_master.element, prs.element):
        guides = _read_guides(el)
        if guides is None:
            continue
        v_pos, h_pos = guides

        if _straddles(v_pos, 0) and _straddles(h_pos, 0):
            left_emu = center_x + min(v_pos)
            right_emu = center_x + max(v_pos)
            top_emu = center_y + min(h_pos)
            bottom_emu = center_y + max(h_pos)
        elif _straddles(v_pos, center_x) and _straddles(h_pos, center_y):
            left_emu = min(v_pos)
            right_emu = max(v_pos)
            top_emu = min(h_pos)
            bottom_emu = max(h_pos)
        else:
            continue

        return {
            'left': _emu_frac(int(left_emu), slide_w),
            'top': _emu_frac(int(top_emu), slide_h),
            'right': _emu_frac(int(right_emu), slide_w),
            'bottom': _emu_frac(int(bottom_emu), slide_h),
        }

    return _workzone_from_placeholders(prs, slot_map)


# ---------------------------------------------------------------------------
# Furniture capture — raw shape XML + media for vector reproduction
# ---------------------------------------------------------------------------

_R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
_REMAP_RID_ATTRS = (
    f'{{{_R}}}embed',
    f'{{{_R}}}link',
    f'{{{_R}}}id',
)

# Per-layout byte budget for captured shape XML + media (base64 inflates ~1.33x).
# Layouts exceeding this are skipped with a warning so a huge full-bleed image
# doesn't balloon the brand JSON.
_FURNITURE_LAYOUT_BUDGET_BYTES = 8 * 1024 * 1024  # 8 MB raw per layout

# Local shape tags we know how to reproduce by copying raw XML.
_SHAPE_TAGS = {
    f'{{{_P}}}sp':    'p:sp',
    f'{{{_P}}}pic':   'p:pic',
    f'{{{_P}}}grpSp': 'p:grpSp',
    f'{{{_P}}}cxnSp': 'p:cxnSp',
}


def _iter_rid_attrs(element: Any) -> Iterator[tuple[Any, str, str]]:
    """Yield (element, attr, rId) for every r:embed / r:link / r:id in the tree."""
    for attr in _REMAP_RID_ATTRS:
        val = element.get(attr)
        if val:
            yield element, attr, val
    for child in element:
        yield from _iter_rid_attrs(child)


def _shape_role_hint(shape: Any) -> str | None:
    """Return a role hint for a shape if one has already been computed, else None.

    ``_extract_layouts`` and the role-annotation pass annotate roles on the fraction-based
    decoration dicts, not on the live shapes, so there is no per-shape role
    available on the python-pptx object.  We return ``None`` here and let the
    injection z-order fall back to its 'unknown' bucket; callers may override
    ``roleHint`` after the fact when they have a role map.
    """
    return None


# Fill tags that mark a picture/object placeholder as baked-in furniture (a
# coloured/angled panel or full-bleed image) rather than an empty drop-zone.
_DECOR_FILL_TAGS = ('solidFill', 'blipFill', 'gradFill', 'pattFill')


def _is_decorative_placeholder(shape: Any) -> bool:
    """True when a placeholder is really visual furniture, not a content slot.

    Cover / section templates frequently implement a coloured or angled panel —
    or a full-bleed image — as a *picture* (or object) placeholder carrying a
    custom geometry and a baked fill on the layout. Those are furniture and must
    be reproduced. An empty picture placeholder awaiting a user photo (geometry
    but no fill) must not. Title / body / date / footer / slide-number
    placeholders are recreated elsewhere and are never rescued here.
    """
    try:
        pf = shape.placeholder_format
        if pf is None or pf.type not in (PP_PLACEHOLDER.PICTURE, PP_PLACEHOLDER.OBJECT):
            return False
    except Exception:
        return False
    sp_pr = shape._element.find(f'{{{_P}}}spPr')
    if sp_pr is None:
        return False
    has_geom = (sp_pr.find(f'{{{_A}}}custGeom') is not None
                or sp_pr.find(f'{{{_A}}}prstGeom') is not None)
    has_fill = any(sp_pr.find(f'{{{_A}}}{t}') is not None for t in _DECOR_FILL_TAGS)
    return has_geom and has_fill


def _serialize_shape(shape: Any, media_registry: dict[str, Any]) -> dict[str, Any] | None:
    """Serialize one non-placeholder shape into a JSON-safe shape blob.

    Returns ``None`` for content placeholders, zero-size shapes, and unrecognised
    tags. Decorative picture/object placeholders (see
    :func:`_is_decorative_placeholder`) ARE captured as furniture with their
    ``<p:ph>`` tag stripped so they inject as plain shapes. Every embedded/linked
    media part is added to ``media_registry`` (keyed by sha1) and recorded in
    ``rIdMap`` so injection can remap relationship IDs.
    """
    try:
        # Skip content placeholders — those are re-created by compose. A
        # decorative placeholder (a filled panel) is kept, but its <p:ph> tag is
        # dropped below so it reproduces as ordinary furniture.
        strip_ph = False
        try:
            if shape.placeholder_format is not None:
                if _is_decorative_placeholder(shape):
                    strip_ph = True
                else:
                    return None
        except Exception:  # noqa: BLE001
            strip_ph = False  # python-pptx raises for a shape that is not a placeholder: keep going

        el = shape._element
        tag = _SHAPE_TAGS.get(el.tag)
        if tag is None:
            return None

        # Skip zero-size shapes (OLE / DRM artefacts).
        try:
            if (shape.width is None or shape.height is None
                    or (shape.width <= 0 and shape.height <= 0)):
                return None
        except Exception:
            logger.debug('brand extract: an optional read failed', exc_info=True)

        # Copy the element so we can strip non-image relationship references
        # without mutating the live presentation tree.
        el = deepcopy(el)

        # A rescued decorative placeholder must lose its <p:ph> tag so it copies
        # in as plain furniture (not a placeholder PowerPoint prompts to fill),
        # and its <p:txBody> — which holds the "Click icon to add picture" prompt
        # text — so that prompt does not render as body text once <p:ph> is gone.
        if strip_ph:
            nv_pr = el.find(f'.//{{{_P}}}nvPr')
            if nv_pr is not None:
                ph_el = nv_pr.find(f'{{{_P}}}ph')
                if ph_el is not None:
                    nv_pr.remove(ph_el)
            tx_body = el.find(f'{{{_P}}}txBody')
            if tx_body is not None:
                el.remove(tx_body)

        # Classify every relationship the shape references. Only *image* parts
        # (blips / svgBlips) are copyable furniture media; everything else —
        # PowerPoint customer-data <p:tags>, hyperlinks, OLE objects — carries a
        # relationship that would dangle on the target slide, so those bearing
        # elements are stripped out entirely.
        rid_map: dict[str, str] = {}
        strip_nodes: list[Any] = []
        for owner, _attr, rId in list(_iter_rid_attrs(el)):
            is_image = False
            try:
                rel = shape.part.rels[rId]
                if not rel.is_external:
                    content_type = rel.target_part.content_type
                    if content_type.startswith('image/'):
                        blob = rel.target_part.blob
                        sha1 = hashlib.sha1(blob, usedforsecurity=False).hexdigest()
                        if sha1 not in media_registry:
                            media_registry[sha1] = {
                                'contentType': content_type,
                                'dataB64': base64.b64encode(blob).decode('ascii'),
                            }
                        rid_map[rId] = sha1
                        is_image = True
            except Exception:
                logger.debug('brand extract: an optional read failed', exc_info=True)
            if not is_image:
                # Drop the whole element that carries the non-image reference
                # (e.g. <p:tags>, <a:hlinkClick>) so no relationship dangles.
                strip_nodes.append(owner)

        for node in strip_nodes:
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
        # Remove now-empty <p:custDataLst> wrappers left behind by tag stripping.
        for cdl in el.findall(f'.//{{{_P}}}custDataLst'):
            if len(cdl) == 0:
                p = cdl.getparent()
                if p is not None:
                    p.remove(cdl)

        xml_bytes = etree.tostring(el)
        return {
            'tag': tag,
            'xmlB64': base64.b64encode(xml_bytes).decode('ascii'),
            'rIdMap': rid_map,
            'roleHint': _shape_role_hint(shape),
        }
    except Exception:
        logger.debug('Furniture shape serialization failed', exc_info=True)
        return None


def _map_layouts_to_archetypes(prs: Any) -> dict[str, int]:
    """Map cover/divider/content archetypes to slide-layout indices.

    Name match first (case-insensitive substring): cover/title -> cover,
    divider/section/agenda -> divider, content/body/text -> content.  When an
    archetype has no name match, falls back to a positional guess (index 0 for
    cover, a middle index for divider, and the first plain 'inherit' layout —
    or index 1 — for content) with a logged warning.  Returns only archetypes
    that could be resolved to a real layout index.
    """
    layouts = list(prs.slide_master.slide_layouts)
    n = len(layouts)
    result: dict[str, int] = {}

    cover_hints = ('cover', 'title')
    divider_hints = ('divider', 'section', 'agenda')
    content_hints = ('content', 'body', 'text')

    used: set[int] = set()
    for idx, layout in enumerate(layouts):
        name = (layout.name or '').lower()
        if 'cover' not in result and any(h in name for h in cover_hints):
            result['cover'] = idx
            used.add(idx)
        elif 'divider' not in result and any(h in name for h in divider_hints):
            result['divider'] = idx
            used.add(idx)
        elif 'content' not in result and any(h in name for h in content_hints):
            result['content'] = idx
            used.add(idx)

    # Positional fallbacks for any archetype not resolved by name.
    if n == 0:
        return result

    if 'cover' not in result:
        fallback = 0
        result['cover'] = fallback
        used.add(fallback)
        logger.warning(
            'capturedFurniture: no cover layout by name; falling back to index %d (%r)',
            fallback, layouts[fallback].name,
        )
    if 'content' not in result:
        # Content must resolve to a layout DISTINCT from the divider — reusing the
        # divider gives content slides the wrong furniture and anchors the heading
        # at the divider's title slot. Prefer an as-yet-unclaimed layout (favouring
        # one that inherits its background, the hallmark of a plain content
        # layout); only reuse a claimed index when every layout is spoken for.
        unused = [idx for idx in range(n) if idx not in used]

        def _inherits_bg(idx: int) -> bool:
            try:
                bg_type, _ = _detect_layout_background(layouts[idx], {})
            except Exception:
                return True
            return bg_type == 'inherit'

        content_idx = next((idx for idx in unused if _inherits_bg(idx)), None)
        if content_idx is None:
            content_idx = unused[0] if unused else min(1, n - 1)
        result['content'] = content_idx
        used.add(content_idx)
        logger.warning(
            'capturedFurniture: no content layout by name; falling back to index %d (%r)',
            content_idx, layouts[content_idx].name,
        )
    if 'divider' not in result:
        # Pick a middle-ish layout not already claimed, else the middle index.
        divider_idx = next((idx for idx in range(n) if idx not in used), n // 2)
        result['divider'] = divider_idx
        logger.warning(
            'capturedFurniture: no divider layout by name; falling back to index %d (%r)',
            divider_idx, layouts[divider_idx].name,
        )

    return result


def _capture_theme(prs: Any) -> dict[str, Any] | None:
    """Capture the master's theme part + colour map for exact colour fidelity.

    Copied furniture references brand colours as theme slots (``schemeClr``) and
    style-matrix indices (``fillRef``/``lnRef``/``effectRef``). Those resolve
    against the *output* deck's theme, so unless we carry the source theme along,
    every branded colour renders as the default Office palette. Returns the theme
    part XML (base64) and the master ``<p:clrMap>`` attributes, or ``None`` on
    failure.
    """
    try:
        theme_part = prs.slide_master.part.part_related_by(_THEME_RELTYPE)
        theme_b64 = base64.b64encode(theme_part.blob).decode('ascii')
        clr_map_el = prs.slide_master.element.find(f'{{{_P}}}clrMap')
        clr_map = dict(clr_map_el.attrib) if clr_map_el is not None else {}
        return {'xmlB64': theme_b64, 'clrMap': clr_map}
    except Exception:
        logger.debug('Theme capture failed', exc_info=True)
        return None


def _serialize_bg(bg_el: Any, part: Any, media_registry: dict[str, Any]) -> dict[str, Any] | None:
    """Serialize a ``<p:bg>`` element, registering any image fill it references.

    Deep-copies the element (so the live tree is untouched), records every image
    part it references — a ``blipFill`` background — into ``media_registry`` by
    sha1, and strips any element bearing a non-image relationship so nothing
    dangles on inject. Returns ``{xmlB64, rIdMap}`` or ``None`` on failure.
    """
    try:
        el = deepcopy(bg_el)
        rid_map: dict[str, str] = {}
        strip_nodes: list[Any] = []
        for owner, _attr, rId in list(_iter_rid_attrs(el)):
            is_image = False
            try:
                rel = part.rels[rId]
                if not rel.is_external and rel.target_part.content_type.startswith('image/'):
                    blob = rel.target_part.blob
                    sha1 = hashlib.sha1(blob, usedforsecurity=False).hexdigest()
                    if sha1 not in media_registry:
                        media_registry[sha1] = {
                            'contentType': rel.target_part.content_type,
                            'dataB64': base64.b64encode(blob).decode('ascii'),
                        }
                    rid_map[rId] = sha1
                    is_image = True
            except Exception:
                logger.debug('brand extract: an optional read failed', exc_info=True)
            if not is_image:
                strip_nodes.append(owner)
        for node in strip_nodes:
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
        return {
            'xmlB64': base64.b64encode(etree.tostring(el)).decode('ascii'),
            'rIdMap': rid_map,
        }
    except Exception:
        logger.debug('Background serialization failed', exc_info=True)
        return None


def _capture_backgrounds(
    prs: Any,
    layout_archetype_map: dict[str, int],
    media_registry: dict[str, Any],
) -> dict[str, Any]:
    """Capture the master + per-archetype layout ``<p:bg>`` fills.

    The ``<p:bg>`` background property (solid / gradient / image fill) is separate
    from the shape tree, so furniture capture alone misses it. A blank output
    deck carries only the default Office background, so any brand background —
    especially a gradient or full-bleed image not implemented as a picture shape
    — is lost unless captured here. Returns ``{'master': blob|None, 'layouts':
    {archetype: blob|None}}`` where each blob is ``{xmlB64, rIdMap}``.
    """
    def _bg_of(el: Any) -> Any:
        cSld = el.find(f'{{{_P}}}cSld')
        return cSld.find(f'{{{_P}}}bg') if cSld is not None else None

    out: dict[str, Any] = {'master': None, 'layouts': {}}
    try:
        master_bg = _bg_of(prs.slide_master.element)
        if master_bg is not None:
            out['master'] = _serialize_bg(master_bg, prs.slide_master.part, media_registry)
    except Exception:
        logger.debug('Master background capture failed', exc_info=True)

    layouts_by_idx = list(prs.slide_master.slide_layouts)
    for archetype, idx in layout_archetype_map.items():
        if idx < 0 or idx >= len(layouts_by_idx):
            continue
        layout = layouts_by_idx[idx]
        try:
            bg = _bg_of(layout.element)  # only present when the layout overrides
            if bg is not None:
                out['layouts'][archetype] = _serialize_bg(bg, layout.part, media_registry)
        except Exception:
            logger.debug('Layout background capture failed (%s)', archetype, exc_info=True)
    return out


# Placeholder types that can hold the slide heading (vs page furniture).
_HEADING_PH_TYPES = {'TITLE', 'CENTER_TITLE', 'VERTICAL_TITLE', 'SUBTITLE', 'BODY', 'OBJECT'}
# The XML-declared title types (<p:ph type="title"/> / "ctrTitle" / "vertTitle").
_TITLE_PH_TYPES = {'TITLE', 'CENTER_TITLE', 'VERTICAL_TITLE'}
# Positional heading fallback (no declared title, no "title" hint): the top-most
# box at least this wide/tall, as slide fractions. Excludes kicker/tracker strips
# (narrow) and hairline label rows (short).
_HEADING_MIN_WIDTH = 0.5
_HEADING_MIN_HEIGHT = 0.03


def _placeholder_geom_frac(ph: Any, slide_w: int, slide_h: int) -> dict[str, Any] | None:
    """0-1 fractional {left,top,width,height} for a placeholder, or None."""
    if ph.left is None or ph.top is None or ph.width is None or ph.height is None:
        return None
    return {
        'left': _emu_frac(ph.left, slide_w),
        'top': _emu_frac(ph.top, slide_h),
        'width': _emu_frac(ph.width, slide_w),
        'height': _emu_frac(ph.height, slide_h),
    }


def _placeholder_run_style(
    ph: Any, slot_map: dict[str, str]
) -> tuple[str | None, str | None, float | None, bool | None]:
    """Resolve a placeholder's OWN default run styling from its ``defRPr`` (the
    template designer's inline choice for that box): (font_name, color_hex,
    size_pt, bold). Any facet the placeholder inherits rather than declares
    inline comes back ``None`` — the caller fills those from the master
    typography scale + theme font scheme, so the emitted heading matches the
    template's real title styling instead of a flat fallback."""
    font: str | None = None
    color: str | None = None
    size: float | None = None
    bold: bool | None = None
    try:
        el = ph._element
        # Collect the first declared value per facet across the placeholder's
        # defRPr elements (lvl1 appears first, so it wins in practice).
        for def_rpr in el.iter(f'{{{_A}}}defRPr'):
            if font is None:
                latin = def_rpr.find(f'{{{_A}}}latin')
                if latin is not None:
                    tf = latin.get('typeface', '')
                    if tf and not tf.startswith('+'):
                        font = tf
            if size is None:
                sz_raw = def_rpr.get('sz')
                if sz_raw:
                    try:
                        size = round(int(sz_raw) / 100, 1)
                    except ValueError:
                        pass
            if bold is None:
                b = def_rpr.get('b')
                if b is not None:
                    bold = b == '1'
            if color is None:
                solid = def_rpr.find(f'{{{_A}}}solidFill')
                if solid is not None:
                    color = _hex_from_clr_el(solid)
                    if color is None:
                        scheme = solid.find(f'{{{_A}}}schemeClr')
                        if scheme is not None:
                            color = _resolve_scheme_color(scheme.get('val'), slot_map)
            if font and color and size is not None and bold is not None:
                break
    except Exception:
        logger.debug('placeholder run-style resolution failed', exc_info=True)
    return font, color, size, bold


def _styled_placeholder_box(
    cand: dict[str, Any],
    typo_scale: dict[str, Any],
    major_font: str | None,
    minor_font: str | None,
) -> dict[str, Any]:
    """Merge a candidate's geometry with fully-resolved run styling.

    Font/colour/size the placeholder declares inline (``cand``) win; anything it
    inherits is filled from the master typography scale + theme font scheme so
    the box carries the template's REAL title styling rather than ``None``.
    Title-family placeholders resolve against the major font + titleStyle; body
    ones against the minor font + bodyStyle.
    """
    is_title = cand['ptype'] in _TITLE_PH_TYPES
    typo = (typo_scale.get('title' if is_title else 'body')) or {}
    theme_font = major_font if is_title else minor_font
    return {
        **cand['geom'],
        'font': cand['font'] or theme_font,
        'color': cand['color'] or typo.get('colorHex'),
        'size': cand['size'] if cand['size'] is not None else typo.get('sizePt'),
    }


def _capture_heading_placeholders(
    layout: Any, slide_w: int, slide_h: int, archetype: str, slot_map: dict[str, str],
    typo_scale: dict[str, Any] | None = None, major_font: str | None = None,
    minor_font: str | None = None,
) -> dict[str, Any]:
    """Pick the heading box (and, for the cover, the date box) among a layout's
    placeholders and return them with geometry + fully-resolved font/colour/size.

    Uniform across archetypes so cover/divider/content all get their action
    title placed natively at export. Selection is type-first: the XML-declared
    title placeholder (<p:ph type="title"/>) wins outright — prompt text like
    "Edit date or title/role" on an unrelated body strip must not. Only layouts
    with no declared title fall back to the text hint, then the top-most wide
    text box (templates that type every placeholder BODY still put the title
    strip across the top; the largest box there is usually the body), then the
    largest text box.
    The cover date has no dedicated placeholder type (the DATE type is the
    auto-date footer field, not the cover's date slot), so it stays heuristic:
    a "date" text hint, else the lowest remaining text box.

    Styling is resolved through the OOXML inheritance chain: the placeholder's
    own ``defRPr`` first, then the master's ``p:txStyles`` + theme font scheme
    (via ``typo_scale``/``major_font``/``minor_font``). Real templates almost
    always inherit their title styling, so without the fallback font/colour/size
    would be ``None`` and the export would drop to a flat default.
    """
    typo_scale = typo_scale or {}
    cands: list[dict[str, Any]] = []
    for ph in layout.placeholders:
        try:
            fmt = ph.placeholder_format
            ptype = fmt.type.name if (fmt is not None and fmt.type is not None) else 'OBJECT'
        except Exception:
            ptype = 'OBJECT'
        if ptype not in _HEADING_PH_TYPES:
            continue
        geom = _placeholder_geom_frac(ph, slide_w, slide_h)
        if geom is None:
            continue
        text = ''
        try:
            text = ph.text_frame.text.strip() if ph.has_text_frame else ''
        except Exception:
            logger.debug('brand extract: an optional read failed', exc_info=True)
        font, color, size, _bold = _placeholder_run_style(ph, slot_map)
        cands.append({
            'text': text, 'geom': geom, 'font': font, 'color': color,
            'size': size, 'ptype': ptype,
        })

    if not cands:
        return {}

    def _area(c: dict[str, Any]) -> float:
        return float(c['geom']['width']) * float(c['geom']['height'])

    by_type = next((c for c in cands if c['ptype'] in _TITLE_PH_TYPES), None)
    by_title = next((c for c in cands if 'title' in c['text'].lower()), None)
    wide = [c for c in cands
            if c['geom']['width'] >= _HEADING_MIN_WIDTH and c['geom']['height'] >= _HEADING_MIN_HEIGHT]
    by_position = min(wide, key=lambda c: c['geom']['top']) if wide else None
    heading = by_type or by_title or by_position or max(cands, key=_area)
    out: dict[str, Any] = {'heading': _styled_placeholder_box(heading, typo_scale, major_font, minor_font)}

    if archetype == 'cover':
        rest = [c for c in cands if c is not heading]
        if rest:
            by_date = next((c for c in rest if 'date' in c['text'].lower()), None)
            date_c = by_date or max(rest, key=lambda c: c['geom']['top'])
            out['date'] = _styled_placeholder_box(date_c, typo_scale, major_font, minor_font)
    return out


def _capture_master_shapes(prs: Any, media_registry: dict[str, Any]) -> list[dict[str, Any]]:
    """Serialize the master's furniture once, tagged ``origin='master'`` so
    injection can dedupe shared furniture by content hash."""
    master_shapes: list[dict[str, Any]] = []
    for shape in prs.slide_master.shapes:
        blob = _serialize_shape(shape, media_registry)
        if blob is not None:
            blob['origin'] = 'master'
            master_shapes.append(blob)
    return master_shapes


def _capture_one_layout(
    prs: Any,
    layout: Any,
    idx: int,
    archetype: str,
    master_shapes: list[dict[str, Any]],
    media_registry: dict[str, Any],
    slot_map: dict[str, str],
    typo_scale: dict[str, Any] | None,
    major_font: str | None,
    minor_font: str | None,
) -> dict[str, Any] | None:
    """One layout's furniture entry (master + layout shapes in z-order, plus its
    heading placeholder), or None when it exceeds the per-layout byte budget."""
    layout_shapes: list[dict[str, Any]] = []
    for shape in layout.shapes:
        blob = _serialize_shape(shape, media_registry)
        if blob is not None:
            blob['origin'] = 'layout'
            layout_shapes.append(blob)

    # Master furniture sits behind the layout's own furniture in z-order.
    shapes = master_shapes + layout_shapes

    # Byte budget: approximate raw XML+media size for this layout.
    approx = 0
    for b in shapes:
        approx += len(b.get('xmlB64', '')) * 3 // 4
        for sha1 in b.get('rIdMap', {}).values():
            media = media_registry.get(sha1)
            if media:
                approx += len(media.get('dataB64', '')) * 3 // 4
    if approx > _FURNITURE_LAYOUT_BUDGET_BYTES:
        logger.warning(
            'capturedFurniture: layout %r (%s) is %d bytes > budget %d; skipping',
            layout.name, archetype, approx, _FURNITURE_LAYOUT_BUDGET_BYTES,
        )
        return None

    return {
        'sourceName': layout.name,
        'sourceIndex': idx,
        'shapes': shapes,
        # Heading (and cover date) placeholder for native text placement at
        # export — same mechanism for all three archetypes.
        'placeholders': _capture_heading_placeholders(
            layout, prs.slide_width, prs.slide_height, archetype, slot_map,
            typo_scale, major_font, minor_font,
        ),
    }


def _capture_all_layouts(
    prs: Any,
    media_registry: dict[str, Any],
    slot_map: dict[str, str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any] | None]]:
    """Furniture + background for EVERY layout, keyed by layout index (as str).

    Lets the client honour the user's own cover/divider/content choice instead of
    the name-matched guess in ``_map_layouts_to_archetypes``: it re-keys the
    chosen indices into ``layouts`` before export. Placeholders are captured with
    the cover rules (heading + date) since the archetype isn't known yet — the
    heading pick is archetype-independent; ``date`` is only read for covers.
    Shares ``media_registry`` with the archetype capture so media is stored once.
    """
    layouts = list(prs.slide_master.slide_layouts)
    typo_scale = _extract_typography_scale(prs, slot_map or {})
    major_font, minor_font = _extract_fonts(prs)
    master_shapes = _capture_master_shapes(prs, media_registry)
    index_map = {str(i): i for i in range(len(layouts))}

    by_index: dict[str, dict[str, Any]] = {}
    for key, idx in index_map.items():
        entry = _capture_one_layout(
            prs, layouts[idx], idx, 'cover', master_shapes, media_registry,
            slot_map or {}, typo_scale, major_font, minor_font,
        )
        if entry is not None:
            by_index[key] = entry
    backgrounds = _capture_backgrounds(prs, index_map, media_registry)['layouts']
    return by_index, backgrounds


def _capture_layout_furniture(
    prs: Any,
    layout_archetype_map: dict[str, int],
    media_registry: dict[str, Any],
    slot_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Capture non-placeholder furniture from each mapped layout + the master.

    Returns the ``layouts`` sub-dict of ``capturedFurniture``: one entry per
    archetype (cover/divider/content) whose ``shapes`` list holds serialized
    shape blobs in source z-order.  Master-level furniture is appended to every
    layout tagged with ``origin='master'`` so injection can dedupe shared
    furniture by content hash.  A per-layout byte budget is enforced; oversize
    layouts are skipped with a warning.
    """
    layouts_by_idx = list(prs.slide_master.slide_layouts)

    # Resolve the master's typography scale + theme font scheme once, so each
    # heading placeholder can inherit real font/colour/size (the common case —
    # templates rarely declare title styling inline on the layout placeholder).
    typo_scale = _extract_typography_scale(prs, slot_map or {})
    major_font, minor_font = _extract_fonts(prs)

    master_shapes = _capture_master_shapes(prs, media_registry)

    out: dict[str, dict[str, Any]] = {}
    for archetype, idx in layout_archetype_map.items():
        if idx < 0 or idx >= len(layouts_by_idx):
            continue
        entry = _capture_one_layout(
            prs, layouts_by_idx[idx], idx, archetype, master_shapes, media_registry,
            slot_map or {}, typo_scale, major_font, minor_font,
        )
        if entry is not None:
            out[archetype] = entry

    return {
        'slideWidthEmu': int(prs.slide_width),
        'slideHeightEmu': int(prs.slide_height),
        'media': media_registry,
        'layouts': out,
        'theme': _capture_theme(prs),
        'background': _capture_backgrounds(prs, layout_archetype_map, media_registry),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract(
    data: bytes,
    *,
    capture_all_layouts: bool = False,
    annotate_roles: RoleAnnotator | None = None,
) -> dict[str, Any]:
    """Validate a master's bytes, extract the brand, and return the brand dict.

    Raises `BrandExtractError` for an empty file, one over `MAX_BYTES`, one that is not a zip, or one
    python-pptx cannot open. `capture_all_layouts` also returns `capturedFurniture.layoutsByIndex` and
    `capturedFurniture.background.layoutsByIndex` (see `_capture_all_layouts`). `annotate_roles`, when
    given, labels placeholders and decorations with one model call; otherwise roles stay `None`.

    Synchronous and CPU-bound (python-pptx parsing): call it off the event loop.
    """
    content = data
    if not content:
        raise BrandExtractError("The file is empty.")
    if len(content) > MAX_BYTES:
        raise BrandExtractError(f'The file is too large (max {MAX_BYTES // (1024 * 1024)} MB).')
    if content[:4] != _PPTX_MAGIC:
        raise BrandExtractError('The file is not a .pptx.')

    try:
        prs: Any = Presentation(BytesIO(content))
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same answer to the caller
        logger.info('brand extract: python-pptx could not open the file (%s)', type(exc).__name__)
        raise BrandExtractError('The file could not be opened as a PowerPoint deck.') from exc

    try:
        theme_el = _theme_element(prs)
        slot_map = _build_slot_map(theme_el)
        all_colors, primary_color, accent_color = _extract_colors(prs, theme_el, slot_map)
        heading_font, body_font = _extract_fonts(prs)
        layouts = _extract_layouts(prs, slot_map)
        workzone = _extract_workzone(prs, slot_map)
        typography_scale = _extract_typography_scale(prs, slot_map)
        master_decorations, logo_shapes = _extract_master_decorations(prs, slot_map)
        master_images = _extract_master_images(prs)
    except Exception as exc:  # noqa: BLE001
        logger.exception('Brand field extraction failed')
        raise BrandExtractError('The brand could not be read from this deck.') from exc

    # Model pass: annotate semantic roles on every placeholder and decoration. Skipped when the
    # caller passes no annotator.
    if annotate_roles is not None:
        try:
            layout_logo_shapes = _annotate_layout_roles(layouts, annotate_roles)
            # Prefer layout-derived logos over master-derived ones when master found none.
            if not logo_shapes and layout_logo_shapes:
                logo_shapes = layout_logo_shapes
        except Exception:
            logger.warning('layout role annotation failed; roles will be null', exc_info=True)

    # Vector furniture capture (raw shape XML + media) for per-role reproduction.
    # Guarded so a capture failure degrades to capturedFurniture=None rather than
    # failing the whole brand extraction.
    captured_furniture: dict[str, Any] | None = None
    try:
        layout_archetype_map = _map_layouts_to_archetypes(prs)
        captured_furniture = _capture_layout_furniture(prs, layout_archetype_map, {}, slot_map)
    except Exception:
        logger.warning('capturedFurniture capture failed; returning None', exc_info=True)
        captured_furniture = None

    # Opt-in per-layout capture. Guarded separately: a failure here leaves the
    # archetype capture above intact.
    if capture_all_layouts and captured_furniture is not None:
        try:
            by_index, bg_by_index = _capture_all_layouts(prs, captured_furniture['media'], slot_map)
            captured_furniture['layoutsByIndex'] = by_index
            captured_furniture['background']['layoutsByIndex'] = bg_by_index
        except Exception:
            logger.warning('capturedFurniture per-layout capture failed; omitting layoutsByIndex', exc_info=True)

    return {
        'headingFont': heading_font,
        'bodyFont': body_font,
        'primaryColor': primary_color,
        'accentColor': accent_color,
        'allColors': all_colors,
        'typographyScale': typography_scale,
        'layouts': layouts,
        'masterDecorations': master_decorations,
        'logoShapes': logo_shapes,
        'masterImages': master_images,
        'workzone': workzone,
        'masterPng': None,
        'capturedFurniture': captured_furniture,
    }


def extract_file(path: Path, **kwargs: Any) -> dict[str, Any]:
    """`extract` on a file's bytes (same keyword arguments)."""
    return extract(Path(path).read_bytes(), **kwargs)
