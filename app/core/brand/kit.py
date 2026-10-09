"""Darwin's brand kit: the untrusted kit JSON read into the fields the service acts on.

Ported from `netlify/functions/_shared/brandKit.ts` (`normalizeKit`, `archetypeForType`,
`KIT_DEFAULTS`, the legacy font presets, `normalizeHeadingPlaceholder(s)`, `DEFAULT_STYLE_TEMPLATE`)
and the kit-writing half of `netlify/functions/brands.ts` (`sanitizeKitPatch`, `validName`, the
canonical blob keys of `_shared/blobs.ts`). One `normalize_kit` serves the export routes (colours,
fonts, master keys, workzone, furniture archetypes) and the brand routes (the pass-through fields).
The prompt-side readers (`renderStyleTemplate`, `buildBrandGroundTruth`) belong to the generation
routes and live with the image prompt, not here.

The kit is client-writable JSON, so nothing in it is trusted as a key or a path: master and
furniture keys only say WHETHER an asset exists; the asset itself is always read by its fixed name
under the brand (the port re-derives the owner, as Darwin re-derived blob keys).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from app.core.brand.workzone import Workzone, normalize_bounds
from app.core.darwin.js import UNDEFINED, js_number

KIT_DEFAULTS = {"primaryColor": "#1F3A5F", "accentColor": "#2E7D9A", "headingFont": "Inter", "bodyFont": "Inter"}
LEGACY_FONTS: dict[str, tuple[str, str]] = {
    "modern-sans": ("Inter", "Inter"),
    "serif-headline": ("Georgia", "Inter"),
    "geometric": ("Futura", "Futura"),
}
ARCHETYPES: tuple[str, ...] = ("cover", "divider", "content")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


@dataclass(frozen=True)
class BrandKit:
    company: str
    primary_color: str
    accent_color: str
    heading_font: str
    body_font: str
    master_key: str | None = None
    title_master_key: str | None = None
    divider_master_key: str | None = None
    workzone: Workzone | None = None
    furniture_archetypes: tuple[str, ...] | None = None
    # The pass-through fields (brandKit.ts BrandKit), read by the brand preview and the prompts.
    neutral_color: str | None = None
    logo_key: str | None = None
    furniture_key: str | None = None
    style_template: str = ""
    layouts: tuple[dict[str, Any], ...] | None = None
    typography_scale: dict[str, Any] | None = None
    master_decorations: tuple[Any, ...] | None = None
    logo_shapes: tuple[Any, ...] | None = None
    all_colors: tuple[dict[str, Any], ...] | None = None
    heading_placeholders: dict[str, dict[str, Any]] | None = None


def _hex(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) and _HEX.match(value) else fallback


def _text(value: Any, fallback: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _key(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def normalize_kit(raw: Any, legacy: dict[str, Any] | None = None) -> BrandKit:
    """`normalizeKit(raw, legacy)`: the kit wins for colours and fonts, the per-deck input wins for
    the company name; anything malformed falls back to the default."""
    r = raw if isinstance(raw, dict) else {}
    lg = legacy or {}
    preset = LEGACY_FONTS.get(str(lg.get("fontStyle") or ""))
    archetypes = r.get("furnitureArchetypes")
    return BrandKit(
        company=_text(lg.get("company"), _text(r.get("company"), "")),
        primary_color=_hex(r.get("primaryColor"), _hex(lg.get("primaryColor"), KIT_DEFAULTS["primaryColor"])),
        accent_color=_hex(r.get("accentColor"), _hex(lg.get("accentColor"), KIT_DEFAULTS["accentColor"])),
        heading_font=_text(r.get("headingFont"), preset[0] if preset else KIT_DEFAULTS["headingFont"]),
        body_font=_text(r.get("bodyFont"), preset[1] if preset else KIT_DEFAULTS["bodyFont"]),
        master_key=_key(r.get("masterKey")),
        title_master_key=_key(r.get("titleMasterKey")),
        divider_master_key=_key(r.get("dividerMasterKey")),
        workzone=normalize_bounds(r.get("workzone")),
        furniture_archetypes=(tuple(a for a in archetypes if a in ARCHETYPES)
                              if isinstance(archetypes, list) else None),
        neutral_color=r["neutralColor"] if isinstance(r.get("neutralColor"), str) and _HEX.match(r["neutralColor"])
        else None,
        logo_key=_key(r.get("logoKey")),
        furniture_key=_key(r.get("furnitureKey")),
        style_template=_text(r.get("styleTemplate"), DEFAULT_STYLE_TEMPLATE),
        layouts=_non_empty(r.get("layouts")),
        typography_scale=r["typographyScale"] if isinstance(r.get("typographyScale"), dict) else None,
        master_decorations=_non_empty(r.get("masterDecorations")),
        logo_shapes=_non_empty(r.get("logoShapes")),
        all_colors=_non_empty(r.get("allColors")),
        heading_placeholders=normalize_heading_placeholders(r.get("headingPlaceholders")),
    )


def _non_empty(value: Any) -> tuple[Any, ...] | None:
    """A non-empty array, verbatim (the kit is untrusted; readers re-check each element)."""
    return tuple(value) if isinstance(value, list) and value else None


def archetype_for_type(slide_type: Any) -> str:
    """`archetypeForType`: title -> cover, divider -> divider, anything else -> content."""
    if slide_type == "title":
        return "cover"
    if slide_type == "divider":
        return "divider"
    return "content"


# =====================================================================================================
# Brand routes, kit-writing side (brands.ts, brand-asset.ts, brand-heading.ts; owned by the brand routes)
# =====================================================================================================

#: `brandKit.ts: DEFAULT_STYLE_TEMPLATE`, verbatim: the editable creative defaults (GET
#: /api/brand-asset?kind=style-default serves it; `normalize_kit` falls back to it).
DEFAULT_STYLE_TEMPLATE = "\n".join((
    "Make the slide executive-ready, text-rich, and strategically structured, with dense but legible content and "
    "clear business language, in the style of a McKinsey/BCG management-consulting deck.",
    "Brand & art direction (apply identically to EVERY slide in this deck):",
    "- Palette: use {primaryColor} as the primary structural color and {accentColor} as the single accent for the "
    "key insight only. Backgrounds white or very light neutral gray. At most ONE accent hue per slide. No gradients, "
    "no drop shadows, no rainbow palettes.",
    "- Typography: headlines set in {headingFont}, body text in {bodyFont}. A left-aligned, conclusion-oriented "
    "action title sits in the top band; body text is crisp and the same size across slides.",
    "- Layout: a disciplined 12-column grid; the headline top-left with a thin horizontal rule beneath it; a small "
    "source/footnote line bottom-left; minimal, identical page furniture on every slide. Do not render page "
    "numbers.",
    "- Charts & diagrams: flat 2D only, thin 1-2px lines, restrained labels; the accent color used sparingly to "
    "mark the single most important point. No 3D, no skeuomorphism, no clip art, no stock photography.",
    "- Add supporting callouts, labels, milestones, KPIs, and decision points around the main framework where "
    "useful, but keep everything visually disciplined and anchored to the primary infographic. Leave generous "
    "whitespace for the strong action title at the top.",
))

MAX_NAME_LENGTH = 120
#: `JSON.stringify(kit).length` limit (UTF-16 code units, not bytes).
MAX_KIT_CHARS = 256 * 1024

#: Kit fields that name a brand asset, and the asset each one names (`brands.ts: ASSET_KEY_KINDS`).
ASSET_KEY_KINDS: dict[str, str] = {
    "logoKey": "logo",
    "masterKey": "master",
    "titleMasterKey": "titleMaster",
    "dividerMasterKey": "dividerMaster",
}
#: The asset kinds brand-asset uploads and serves (in the order its 400 lists them).
ASSET_KINDS: tuple[str, ...] = ("logo", "master", "titleMaster", "dividerMaster")
#: The kinds an archetype layout can be copied into (`brand-asset.ts: MASTER_KINDS`).
MASTER_KINDS: tuple[str, ...] = ("master", "titleMaster", "dividerMaster")
#: archetype -> the master asset and the kit key that names it (`brand-archetypes.ts`).
MASTER_FOR: dict[str, str] = {"cover": "titleMaster", "divider": "dividerMaster", "content": "master"}
KIT_KEY_FOR: dict[str, str] = {"cover": "titleMasterKey", "divider": "dividerMasterKey", "content": "masterKey"}

_HEX_FIELDS = frozenset({"primaryColor", "accentColor", "neutralColor"})
_STRING_FIELDS = frozenset({"company", "headingFont", "bodyFont", "styleTemplate"})
_PASSTHROUGH_FIELDS = frozenset({"layouts", "workzone", "typographyScale", "masterDecorations", "logoShapes",
                                 "allColors", "headingPlaceholders", "archetypeLayouts"})

#: Port asset names (the basenames of Darwin's `brand/<ownerId>/<brandId>/<name>` blobs).
FURNITURE_ASSET = "furniture"
FURNITURE_ALL_ASSET = "furniture-all"
GUIDELINES_ASSET = "guidelines"


class KitError(ValueError):
    """A kit or brand field Darwin answers with 400; the message is Darwin's exact text."""


def layout_preview_asset(index: int) -> str:
    """The asset holding the rendered PNG of layout `index` (`brandLayoutPreviewKey`)."""
    return f"layout-preview-{index}"


def brand_asset_key(owner_id: str, brand_id: str, kind: str) -> str:
    """`brandAssetKey`: the canonical key a kit stores for an asset. Only says the asset EXISTS: every
    read re-derives the asset from the access-checked brand, never from this string."""
    return f"brand/{owner_id}/{brand_id}/{kind}.png"


def brand_furniture_key(owner_id: str, brand_id: str) -> str:
    """`brandFurnitureKey`."""
    return f"brand/{owner_id}/{brand_id}/furniture.json"


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def valid_name(value: Any) -> str:
    """`validName`: a string of 1-120 characters (UTF-16 units) after trimming, returned trimmed."""
    if not isinstance(value, str) or not value.strip() or _utf16_length(value.strip()) > MAX_NAME_LENGTH:
        raise KitError(f"name is required (1-{MAX_NAME_LENGTH} characters)")
    return value.strip()


def js_stringify_length(value: Any) -> int:
    """`JSON.stringify(value).length`: the JSON text's length in UTF-16 code units."""
    return _utf16_length(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False))


def sanitize_kit_patch(raw: Any, owner_id: str, brand_id: str) -> dict[str, Any]:
    """`brands.ts: sanitizeKitPatch`: validate a kit (patch) and canonicalise its asset keys.

    A null value means "clear this field" and is kept as is, for ANY key (unknown keys included);
    asset-key fields take any truthy value and store the canonical key (the asset need not exist);
    unknown non-null keys are a 400, the first one in the object's order."""
    if not isinstance(raw, dict):
        raise KitError("kit must be an object")
    if js_stringify_length(raw) > MAX_KIT_CHARS:
        raise KitError("kit too large (max 256KB)")
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if value is None:
            out[key] = None
        elif key in ASSET_KEY_KINDS:
            if _falsy(value):
                raise KitError(f"{key} must be a truthy value or null")
            out[key] = brand_asset_key(owner_id, brand_id, ASSET_KEY_KINDS[key])
        elif key == "furnitureKey":
            if _falsy(value):
                raise KitError("furnitureKey must be a truthy value or null")
            out[key] = brand_furniture_key(owner_id, brand_id)
        elif key == "furnitureArchetypes":
            if not isinstance(value, list) or not all(isinstance(a, str) and a in ARCHETYPES for a in value):
                raise KitError('furnitureArchetypes must be an array of "cover" | "divider" | "content"')
            out[key] = value
        elif key in _HEX_FIELDS:
            if not isinstance(value, str) or not _HEX.match(value):
                raise KitError(f"{key} must be a hex color like #1A2B3C")
            out[key] = value
        elif key in _STRING_FIELDS:
            if not isinstance(value, str):
                raise KitError(f"{key} must be a string")
            out[key] = value
        elif key in _PASSTHROUGH_FIELDS:
            out[key] = value
        else:
            raise KitError(f"Unknown kit field: {key}")
    return out


def merge_kit(stored: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """PATCH /api/brands' shallow merge: the patch's keys win; a null in the patch deletes the key."""
    merged = dict(stored)
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def normalize_heading_placeholder(raw: Any) -> dict[str, Any] | None:
    """`normalizeHeadingPlaceholder`: finite left/top and positive width/height (JavaScript `Number()`
    of each: a null is 0, a missing key is NaN), plus an optional trimmed font, #RRGGBB colour and a
    positive sizePt (SlideForge's `size` alias)."""
    if not isinstance(raw, dict):
        return None

    def num(key: str) -> float | None:
        n = js_number(raw.get(key, UNDEFINED))
        return n if math.isfinite(n) else None

    left, top, width, height = num("left"), num("top"), num("width"), num("height")
    if left is None or top is None or width is None or height is None or not width > 0 or not height > 0:
        return None
    out: dict[str, Any] = {"left": js_plain(left), "top": js_plain(top), "width": js_plain(width),
                           "height": js_plain(height)}
    font = raw.get("font")
    if isinstance(font, str) and font.strip():
        out["font"] = font.strip()
    color = raw.get("color")
    if isinstance(color, str) and _HEX.match(color):
        out["color"] = color
    # `Number(r.sizePt ?? r.size)`: sizePt unless it is null or missing.
    size = js_number(raw["sizePt"] if raw.get("sizePt") is not None else raw.get("size", UNDEFINED))
    if math.isfinite(size) and size > 0:
        out["sizePt"] = js_plain(size)
    return out


def normalize_heading_placeholders(raw: Any) -> dict[str, dict[str, Any]] | None:
    """`normalizeHeadingPlaceholders`: the known archetypes with usable geometry; None when none survive."""
    if not isinstance(raw, dict):
        return None
    out = {arch: ph for arch in ARCHETYPES if (ph := normalize_heading_placeholder(raw.get(arch))) is not None}
    return out or None


def js_plain(value: float) -> int | float:
    """A whole float as an int, so JSON writes `28` where JavaScript writes 28 (not `28.0`)."""
    return int(value) if value.is_integer() and abs(value) < 2**53 else value


def _falsy(value: Any) -> bool:
    """JavaScript `!value` for a non-null JSON value: false, 0 and "" (arrays and objects are truthy)."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value == 0
    return isinstance(value, str) and not value
