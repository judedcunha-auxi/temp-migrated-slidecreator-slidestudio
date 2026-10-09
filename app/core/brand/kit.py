"""Darwin's brand kit: the untrusted kit JSON read into the fields the service acts on.

Ported from `netlify/functions/_shared/brandKit.ts` (`normalizeKit`, `archetypeForType`,
`KIT_DEFAULTS`, the legacy font presets). PARTIAL: this holds what the export routes need (colours,
fonts, master keys, workzone, furniture archetypes). The brands routes extend it with the
pass-through fields (`layouts`, `typographyScale`, `masterDecorations`, `logoShapes`, `allColors`,
`headingPlaceholders`, `styleTemplate`); keep one `normalize_kit` for both.

The kit is client-writable JSON, so nothing in it is trusted as a key or a path: master and
furniture keys only say WHETHER an asset exists; the asset itself is always read by its fixed name
under the brand (the port re-derives the owner, as Darwin re-derived blob keys).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.core.brand.workzone import Workzone, normalize_bounds

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
    )


def archetype_for_type(slide_type: Any) -> str:
    """`archetypeForType`: title -> cover, divider -> divider, anything else -> content."""
    if slide_type == "title":
        return "cover"
    if slide_type == "divider":
        return "divider"
    return "content"
