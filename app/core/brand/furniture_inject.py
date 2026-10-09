"""Inject captured brand furniture onto a python-pptx part (theme, background, shapes).

The inverse half of `app.core.brand.extract`: what extraction captured as `capturedFurniture` JSON
(raw shape XML, media by sha1, the theme part and `<p:bg>` fills) is written back onto a master here.
Only what synthesising a master needs is carried: theme install, background install, media
registration, rId remap, dangling-reference stripping, coordinate scaling and the part-level shape
injector. The ML paths of the original OCR service (furniture stripping and suppression on rendered
slides) are not: they needed numpy and the detector, and synthesis never calls them.

Depends only on `lxml` and `python-pptx`. python-pptx objects are typed `Any` here: the code reaches
into OPC parts and raw XML that python-pptx's own types do not describe.

Ported from Slide Studio `server/brand/furniture_inject.py` (migration plan §4.1, K).
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any

from lxml import etree
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.opc.package import Part
from pptx.oxml.ns import qn

logger = logging.getLogger(__name__)

# Image content-type -> media part extension. Covers the raster and vector formats PowerPoint embeds
# (SVG/EMF/WMF included, which add_picture cannot handle because PIL cannot decode them).
_EXT_BY_CT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
    "image/svg+xml": "svg",
    "image/x-emf": "emf",
    "image/emf": "emf",
    "image/x-wmf": "wmf",
    "image/wmf": "wmf",
}

_NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_THEME_RELTYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme"

#: Captured XML may come back from a caller (a stored brand), so it is parsed with no entity
#: expansion and no network access.
SAFE_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)

_REMAP_ATTRS = (
    f"{{{_NS_R}}}embed",
    f"{{{_NS_R}}}link",
    f"{{{_NS_R}}}id",
)


def apply_brand_theme(prs: Any, captured: dict[str, Any] | None) -> bool:
    """Install the captured brand theme and colour map onto the deck's master.

    Copied furniture references colours as theme slots and style-matrix indices, which resolve
    against the presentation's theme. A blank `Presentation()` carries the default Office palette,
    so without this the brand colours render wrong. Replacing the master's theme part XML (and its
    `<p:clrMap>`) makes every `schemeClr` / `fillRef` / `fontRef` resolve to the brand values.
    """
    theme = (captured or {}).get("theme")
    if not theme or not theme.get("xmlB64"):
        return False
    try:
        master = prs.slide_masters[0]
        theme_part = master.part.part_related_by(_THEME_RELTYPE)
        theme_part._blob = base64.b64decode(theme["xmlB64"])
        clr_map = theme.get("clrMap") or {}
        if clr_map:
            master_clr_map = master.element.find(f"{{{_NS_P}}}clrMap")
            if master_clr_map is not None:
                for key, value in clr_map.items():
                    master_clr_map.set(key, value)
        return True
    except Exception:  # noqa: BLE001 - a theme we cannot install costs colour fidelity, not the master
        logger.warning("apply_brand_theme failed; brand colours may be off", exc_info=True)
        return False


def _install_bg(part: Any, owner_el: Any, blob: dict[str, Any], media_registry: dict[str, Any]) -> bool:
    """Install one captured `<p:bg>` blob onto `owner_el`'s `<p:cSld>`."""
    try:
        el = etree.fromstring(base64.b64decode(blob["xmlB64"]), SAFE_PARSER)
        rid_map_local: dict[str, str] = blob.get("rIdMap") or {}
        needed = set(rid_map_local.values())
        if needed:
            sha1_to_rid = _register_media(part, media_registry, needed)
            _remap_rids_via_media(el, rid_map_local, sha1_to_rid)
            _strip_dangling_rels(el, set(part.rels.keys()))
        c_sld = owner_el.find(qn("p:cSld"))
        if c_sld is None:
            return False
        existing = c_sld.find(qn("p:bg"))
        if existing is not None:
            c_sld.remove(existing)
        c_sld.insert(0, el)  # <p:bg> must precede <p:spTree>
        return True
    except Exception:  # noqa: BLE001
        logger.warning("apply_brand_background: bg install failed", exc_info=True)
        return False


def apply_brand_background(
    prs: Any, captured: dict[str, Any] | None, slide_layout: Any, layout_archetype: str | None
) -> bool:
    """Install the captured master and layout `<p:bg>` fills onto the deck."""
    bgs: dict[str, Any] = (captured or {}).get("background") or {}
    media: dict[str, Any] = (captured or {}).get("media") or {}
    applied = False
    try:
        master = prs.slide_masters[0]
        if bgs.get("master") and _install_bg(master.part, master.element, bgs["master"], media):
            applied = True
    except Exception:  # noqa: BLE001
        logger.warning("apply_brand_background: master bg failed", exc_info=True)

    lay_bgs: dict[str, Any] = bgs.get("layouts") or {}
    blob = lay_bgs.get(layout_archetype) if layout_archetype else None
    if blob is None and layout_archetype == "content":
        blob = lay_bgs.get("divider")  # content falls back to the divider layout
    if blob and _install_bg(slide_layout.part, slide_layout.element, blob, media):
        applied = True
    return applied


def _register_media(part: Any, sha1_to_blob: dict[str, Any], needed_sha1: set[str]) -> dict[str, str]:
    """Register each needed media blob on `part`; return sha1 -> new rId.

    Registers the blob directly at the OPC layer with its captured content type (via `Part` +
    `relate_to`), so the vector formats PowerPoint embeds (SVG, EMF, WMF) are supported. `part` is
    any OPC part (slide, slide layout, or slide master).
    """
    sha1_to_rid: dict[str, str] = {}
    package = part.package
    for sha1 in sorted(needed_sha1):
        media = sha1_to_blob.get(sha1)
        if media is None:
            logger.warning("inject_furniture: media %s missing from registry", sha1[:12])
            continue
        try:
            blob = base64.b64decode(media["dataB64"])
        except Exception:  # noqa: BLE001
            logger.warning("inject_furniture: media %s failed to decode", sha1[:12])
            continue
        content_type = str(media.get("contentType", "image/png"))
        ext = _EXT_BY_CT.get(content_type, "bin")
        try:
            partname = package.next_partname(f"/ppt/media/image%d.{ext}")
            media_part = Part(partname, content_type, package, blob)
            sha1_to_rid[sha1] = str(part.relate_to(media_part, RT.IMAGE))
        except Exception:  # noqa: BLE001
            logger.warning("inject_furniture: media %s (%s) failed to register", sha1[:12], content_type,
                           exc_info=True)
    return sha1_to_rid


def _remap_rids_via_media(element: Any, rid_map_local: dict[str, str], sha1_to_rid: dict[str, str]) -> None:
    """Rewrite r:embed / r:link / r:id from the captured (source) rId to the new rId."""
    for attr in _REMAP_ATTRS:
        src_rid = element.get(attr)
        if src_rid and src_rid in rid_map_local:
            new_rid = sha1_to_rid.get(rid_map_local[src_rid])
            if new_rid is not None:
                element.set(attr, new_rid)
    for child in element:
        _remap_rids_via_media(child, rid_map_local, sha1_to_rid)


def _strip_dangling_rels(element: Any, valid_rids: set[str]) -> None:
    """Remove any element carrying a relationship attribute not resolvable on the target part."""
    for child in list(element):
        dangling = any(child.get(attr) and child.get(attr) not in valid_rids for attr in _REMAP_ATTRS)
        if dangling:
            element.remove(child)
        else:
            _strip_dangling_rels(child, valid_rids)


def _scale_top_level_xfrm(el: Any, sx: float, sy: float) -> None:
    """Scale a top-level shape's `<a:off>` / `<a:ext>` in place (outermost xfrm only)."""
    xfrm = None
    for pr_tag in (qn("p:spPr"), qn("p:grpSpPr")):
        pr = el.find(pr_tag)
        if pr is not None:
            candidate = pr.find(qn("a:xfrm"))
            if candidate is not None:
                xfrm = candidate
                break
    if xfrm is None:
        return
    off = xfrm.find(qn("a:off"))
    ext = xfrm.find(qn("a:ext"))
    if off is not None:
        try:
            off.set("x", str(int(round(int(off.get("x", "0")) * sx))))
            off.set("y", str(int(round(int(off.get("y", "0")) * sy))))
        except (TypeError, ValueError):
            pass
    if ext is not None:
        try:
            ext.set("cx", str(int(round(int(ext.get("cx", "0")) * sx))))
            ext.set("cy", str(int(round(int(ext.get("cy", "0")) * sy))))
        except (TypeError, ValueError):
            pass


def shape_identity(blob: dict[str, Any]) -> str:
    """Content hash of a shape blob (xml + referenced media sha1s), for dedupe."""
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(str(blob.get("xmlB64", "")).encode("ascii", "ignore"))
    for sha1 in sorted((blob.get("rIdMap") or {}).values()):
        digest.update(str(sha1).encode("ascii", "ignore"))
    return digest.hexdigest()


def resolve_layout(captured: dict[str, Any], layout_archetype: str | None) -> dict[str, Any] | None:
    """The layout blob for `layout_archetype`, with content -> any fallbacks."""
    layouts: dict[str, Any] = captured.get("layouts") or {}
    if not layouts:
        return None
    if layout_archetype and layout_archetype in layouts:
        return dict(layouts[layout_archetype])
    if "content" in layouts:
        logger.info("resolve_layout: kind=%r absent; falling back to 'content'", layout_archetype)
        return dict(layouts["content"])
    first_key = next(iter(layouts))
    logger.info("resolve_layout: kind=%r absent; falling back to %r", layout_archetype, first_key)
    return dict(layouts[first_key])


def inject_furniture_into_part(
    part: Any,
    sp_tree: Any,
    blobs: list[dict[str, Any]],
    media_registry: dict[str, Any],
    cap_w: int,
    cap_h: int,
    canvas_w: int,
    canvas_h: int,
) -> int:
    """Copy a pre-filtered list of furniture shape blobs onto an arbitrary part.

    `part` owns `sp_tree` (media relationships attach here); `blobs` is an already-filtered list of
    shape blob dicts. `cap_w`/`cap_h` is the captured slide size in EMU; `canvas_w`/`canvas_h` the
    target master's size; shapes are scaled when they differ. Returns the count appended.
    """
    if not blobs:
        return 0

    needed: set[str] = set()
    for blob in blobs:
        needed.update((blob.get("rIdMap") or {}).values())
    sha1_to_rid = _register_media(part, media_registry, needed) if needed else {}

    sx = sy = 1.0
    if cap_w and cap_h and (cap_w != canvas_w or cap_h != canvas_h):
        sx = canvas_w / cap_w
        sy = canvas_h / cap_h

    valid_rids = set(part.rels)
    appended = 0
    for blob in blobs:
        try:
            el = etree.fromstring(base64.b64decode(blob["xmlB64"]), SAFE_PARSER)
        except Exception:  # noqa: BLE001
            logger.warning("inject_furniture: shape XML failed to parse; skipping", exc_info=True)
            continue
        rid_map_local: dict[str, str] = blob.get("rIdMap") or {}
        if rid_map_local:
            _remap_rids_via_media(el, rid_map_local, sha1_to_rid)
        _strip_dangling_rels(el, valid_rids)
        if sx != 1.0 or sy != 1.0:
            _scale_top_level_xfrm(el, sx, sy)
        sp_tree.append(el)
        appended += 1
    return appended
