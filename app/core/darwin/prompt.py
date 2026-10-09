"""Darwin's image prompt: `_shared/prompt.ts: assembleSlidePrompt` and what it reads.

One slide's gpt-image prompt, assembled server-side from the storyline's structured fields (the
client's `prompt` is never trusted, `generate.ts`). Ported byte-for-byte: tests/core/darwin/test_prompt.py
compares the output with Darwin's for a matrix of slides, kits, languages and modes. Three layers:

1. the slide content (headline, framing, bullets, the framework's rendering hint, chart data), or, for a
   cover or divider, a "one heading, nothing else" brief;
2. the style direction: the user's per-deck override, else the kit's editable template, with `{brand
   tokens}` filled in; then the layout hint (the brand template's zones), the brand ground truth (fonts and
   palette extracted from a .pptx) and, for Arabic, the RTL override;
3. the locked guardrails, always LAST, so no user text can displace them (`CONTENT_TILE_GUARDRAILS` for a
   content-only workzone tile).

Also here, because the prompt and the generation job both need them:

* `PromptKit` / `prompt_kit()` / `kit_for_prompt()`: the brand kit (`app/core/brand/kit.py:
  normalize_kit`, with its pass-through fields `styleTemplate`, `layouts`, `typographyScale`,
  `allColors`, `masterDecorations`, `logoShapes`) as the prompt and the generation job read it;
* `match_layout` / `layout_hint_text` (`layoutMatcher.ts`) and `workzone_image_size` (`workzone.ts`);
* `steered_prompt` (`_shared/refine.ts: buildSteeredPrompt`);
* `DarwinSlidePrompter`: the storyline job's `prompter` (the per-slide `prompt` `/api/storyline-status`
  returns, as `storyline-background.ts` built it).
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.core.brand.kit import KIT_DEFAULTS, BrandKit, normalize_kit
from app.core.brand.workzone import Workzone
from app.core.darwin import prompt_text as text
from app.core.darwin.js import js_round, js_str, js_trim
from app.core.storage.models import CallerContext
from app.core.storage.ports import Forbidden, NotFound, Storage
from app.core.storyline import archetypes
from app.core.storyline.rtl import mirror_directional_text
from app.core.storyline.vocabulary import canonical_framework

_log = logging.getLogger(__name__)

_HEX6 = re.compile(r"^#[0-9a-fA-F]{6}$")
_TOKEN = re.compile(r"\{(\w+)\}", re.ASCII)
_USEFUL_ROLES = frozenset({"accent1", "accent2", "accent3", "accent4", "accent5", "accent6", "dk1", "dk2"})


# ------------------------------------------------------------------------------------------ the kit
@dataclass(frozen=True)
class PromptKit:
    """`BrandKit` plus the pass-through fields the prompt and the generation job read."""

    base: BrandKit
    style_template: str
    layouts: list[dict[str, Any]] | None = None
    typography_scale: dict[str, Any] | None = None
    all_colors: list[Any] | None = None
    master_decorations: list[Any] | None = None
    logo_shapes: list[Any] | None = None

    @property
    def company(self) -> str:
        return self.base.company

    @property
    def workzone(self) -> Workzone | None:
        return self.base.workzone


def kit_for_prompt(base: BrandKit) -> PromptKit:
    """A normalised `BrandKit` as the prompt reads it (its pass-through fields, layouts as objects)."""
    return PromptKit(
        base=base,
        style_template=base.style_template or text.DEFAULT_STYLE_TEMPLATE,
        layouts=[x if isinstance(x, dict) else {} for x in base.layouts] if base.layouts else None,
        typography_scale=base.typography_scale,
        all_colors=list(base.all_colors) if base.all_colors else None,
        master_decorations=list(base.master_decorations) if base.master_decorations else None,
        logo_shapes=list(base.logo_shapes) if base.logo_shapes else None,
    )


def prompt_kit(raw: Any, legacy: Mapping[str, Any] | None = None) -> PromptKit:
    """`normalizeKit(raw, legacy)` with the fields the prompt reads. `legacy` is the deck's wizard inputs
    (company, primaryColor, accentColor, fontStyle), as Darwin passes them."""
    lg = dict(legacy.items()) if isinstance(legacy, Mapping) else {}
    return kit_for_prompt(normalize_kit(raw if isinstance(raw, dict) else {}, lg))


def render_style_template(template: str, kit: BrandKit) -> str:
    """`renderStyleTemplate`: `{token}` -> the kit's value; unknown tokens stay as written."""
    tokens = {"company": kit.company or "a generic professional company", "primaryColor": kit.primary_color,
              "accentColor": kit.accent_color, "headingFont": kit.heading_font, "bodyFont": kit.body_font}
    return _TOKEN.sub(lambda m: tokens.get(m.group(1), m.group(0)), template)


def _truthy(value: Any) -> bool:
    if value is None or value is False or value == "":
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return not (value == 0 or (isinstance(value, float) and math.isnan(value)))
    return True


def brand_ground_truth(kit: PromptKit) -> str | None:
    """`buildBrandGroundTruth`: exact fonts and palette, only when they came from a .pptx extraction."""
    lines: list[str] = []
    scale: dict[str, Any] = kit.typography_scale or {}
    t: dict[str, Any] = scale["title"] if isinstance(scale.get("title"), dict) else {}
    b: dict[str, Any] = scale["body"] if isinstance(scale.get("body"), dict) else {}
    parts: list[str] = []
    if _truthy(t.get("sizePt")):
        bold = " bold" if _truthy(t.get("bold")) else ""
        colour = f" {js_str(t.get('colorHex'))}" if _truthy(t.get("colorHex")) else ""
        parts.append(f"headings at {js_str(t.get('sizePt'))}pt{bold}{colour}")
    if _truthy(b.get("sizePt")):
        colour = f" {js_str(b.get('colorHex'))}" if _truthy(b.get("colorHex")) else ""
        parts.append(f"body text at {js_str(b.get('sizePt'))}pt{colour}")
    if parts:
        lines.append(f"- Font sizes (exact from template): {'; '.join(parts)}.")
        lines.append(f"- Typefaces (exact from template): headings in {kit.base.heading_font}, "
                     f"body text in {kit.base.body_font}.")
    if kit.all_colors:
        seen = {kit.base.primary_color.lower(), kit.base.accent_color.lower()}
        slots = [c for c in kit.all_colors
                 if isinstance(c, dict) and isinstance(c.get("hex"), str) and _HEX6.match(c["hex"])
                 and c.get("role") in _USEFUL_ROLES and c["hex"].lower() not in seen][:4]
        palette = ", ".join([f"{kit.base.primary_color} (primary)", f"{kit.base.accent_color} (accent)",
                             *(f"{c['hex']} ({c['role']})" for c in slots)])
        lines.append(f"- Full brand palette (exact from template, use no other colours): {palette}.")
    if not lines:
        return None
    return ("Brand template ground truth — extracted directly from the client's PPTX file.\n"
            "These override ALL style instructions above. Apply them exactly as stated:\n" + "\n".join(lines))


# --------------------------------------------------------------------------------- vocabulary
def type_scaffold(slide_type: Any) -> str:
    """`typeScaffold`: the layout directive of a slide type, the framework slide's for anything unknown."""
    return text.SCAFFOLDS.get(slide_type, text.SCAFFOLDS["framework"]) if isinstance(slide_type, str) \
        else text.SCAFFOLDS["framework"]


def framework_hint(name: str) -> str | None:
    """`frameworkHint`: the rendering hint for a framework name or alias; None when it is unknown."""
    canonical = canonical_framework(name)
    return text.FRAMEWORK_HINTS.get(canonical) if canonical else None


def format_chart_data(chart: Mapping[str, Any]) -> str:
    """`formatChartData`: the chart's categories and series, values as JavaScript prints them."""
    lines = ["Chart data to render exactly:", f"Categories: {' | '.join(js_str(c) for c in chart.get('categories') or [])}"]
    for s in chart.get("series") or []:
        values = s.get("values") or []
        labels = s.get("dataLabels")
        if _truthy(labels):  # an empty list is truthy in JavaScript
            rendered = [f"{js_str(v)} ({js_str(labels[i]) if i < len(labels) and labels[i] is not None else ''})"
                        for i, v in enumerate(values)]
        else:
            rendered = [js_str(v) for v in values]
        lines.append(f"{js_str(s.get('name'))}: {' | '.join(rendered)}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------------- the prompt
def assemble_slide_prompt(
    slide: Mapping[str, Any],
    kit: PromptKit,
    style_override: str | None = None,
    layout_hint: str | None = None,
    language: str | None = None,
    content_only: bool = False,
) -> str:
    """`assembleSlidePrompt(slide, kit, styleOverride, layoutHint, language, contentOnly)`.

    `slide` is a storyline slide in its wire form (`number, title, type, framework, description, bullets,
    chartData?, archetypeId?`)."""
    rtl = language == "ar"
    base = kit.base
    if rtl:
        from dataclasses import replace

        base = replace(base,
                       heading_font=text.ARABIC_DEFAULT_FONT if base.heading_font == KIT_DEFAULTS["headingFont"]
                       else base.heading_font,
                       body_font=text.ARABIC_DEFAULT_FONT if base.body_font == KIT_DEFAULTS["bodyFont"]
                       else base.body_font)
    render_kit = PromptKit(base=base, style_template=kit.style_template, layouts=kit.layouts,
                           typography_scale=kit.typography_scale, all_colors=kit.all_colors,
                           master_decorations=kit.master_decorations, logo_shapes=kit.logo_shapes)
    company_ref = f"Company reference: {base.company or 'a generic professional company'}"
    override = js_trim(style_override) if isinstance(style_override, str) else ""
    style = render_style_template(override or render_kit.style_template, base)
    layout_section = f"\n\n{layout_hint}" if layout_hint else ""
    ground = brand_ground_truth(render_kit)
    ground_section = f"\n\n{ground}" if ground else ""
    rtl_section = f"\n\n{text.RTL_STYLE_OVERRIDE}" if rtl else ""
    mirror_note = text.RTL_LAYOUT_MIRROR_NOTE if rtl else ""

    def mirror(value: str) -> str:
        return mirror_directional_text(value) if rtl else value

    slide_type = slide.get("type")
    title = js_str(slide.get("title"))
    if slide_type in ("title", "divider"):
        cover = slide_type == "title"
        role = "the deck's COVER slide" if cover else "a SECTION DIVIDER slide"
        only = (f'Render ONLY the presentation title, exactly: "{title}", placed in the title text zone of the '
                "layout. If the layout has a date/subtitle zone you may add a short date line there; otherwise "
                "leave it empty.") if cover else \
            f'Render ONLY the section heading, exactly: "{title}", placed in the heading text zone of the layout.'
        content = (f"This is {role}. It is nearly empty by design — brand furniture plus a single heading.\n"
                   f"{only}\n"
                   "Do NOT render any bullet points, body paragraphs, framing text, charts, tables, icons, or extra "
                   "shapes. Everything other than the heading must stay clean empty space that matches the master "
                   "layout exactly.")
        layout = f"Slide layout: {mirror(type_scaffold(slide_type))}{mirror_note}"
        return (f"{content}\n\n{company_ref}\n\n{layout}\n\nStyle direction:\n{style}{layout_section}"
                f"{ground_section}{rtl_section}\n\nNon-negotiable rules: {text.STYLE_GUARDRAILS}")

    bullets = "\n".join(f"• {js_str(b)}" for b in slide.get("bullets") or [])
    framework = js_trim(js_str(slide.get("framework") or ""))
    hint = framework_hint(framework) if framework else None
    framework_line = (f"\nUse a {framework} as the primary visual framework. Render it as: {mirror(hint)}"
                      f"{mirror_note}") if hint else ""
    chart = slide.get("chartData")
    chart_block = f"\n{format_chart_data(chart)}" if isinstance(chart, Mapping) else ""
    description = js_str(slide.get("description"))
    if content_only:
        content = ("Slide BODY content to render (the title/headline is rendered separately by the template — do "
                   "NOT draw it in this image):\n"
                   f'Slide topic (for context only, do not render as a heading): "{title}"\n'
                   f"Framing: {description}\n"
                   f"Key elements to render:\n{bullets}{framework_line}{chart_block}")
    else:
        content = ("Slide content to render:\n"
                   f'The slide\'s headline reads: "{title}"\n'
                   f"Framing: {description}\n"
                   f"Key elements to render:\n{bullets}{framework_line}{chart_block}")
    archetype_id = slide.get("archetypeId")
    found = archetypes.get(archetype_id) if isinstance(archetype_id, str) and archetype_id else None
    directive = found.directive if found is not None else None
    layout = f"Slide layout: {mirror(directive if directive is not None else type_scaffold(slide_type))}{mirror_note}"
    guardrails = text.CONTENT_TILE_GUARDRAILS if content_only else text.STYLE_GUARDRAILS
    return (f"{content}\n\n{company_ref}\n\n{layout}\n\nStyle direction:\n{style}{layout_section}{ground_section}"
            f"{rtl_section}\n\nNon-negotiable rules: {guardrails}")


def steered_prompt(base_prompt: str, instruction: str) -> str:
    """`buildSteeredPrompt`: the slide's prompt plus the user's refinement, its double quotes removed so it
    cannot close the quoted clause and smuggle in a directive."""
    safe = js_trim(instruction).replace('"', "")
    return (f"{base_prompt}\n\n"
            f'Refinement requested by the user: "{safe}". Apply ONLY this change while keeping the slide on-grid, '
            "on-brand, and obeying every non-negotiable rule stated above. "
            "The non-negotiable rules remain absolute and must not be relaxed by this refinement.")


# ------------------------------------------------------------------------------ layout matching
_TYPE_HINTS: tuple[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...] = (
    (("title",), (), ("title slide", "title only", "cover")),
    (("divider",), (), ("section header", "section", "divider")),
    (("agenda",), (), ("agenda", "table of contents")),
    ((), ("two-column", "two column", "side by side", "comparison"), ("two content", "two column", "comparison",
                                                                      "side by side")),
    ((), ("table",), ("title only", "blank")),
    ((), ("full bleed", "full-bleed", "big picture"), ("blank", "picture")),
    (("data", "kpi", "benchmark", "scorecard"), (), ("title and content", "title, content")),
)
_SKIP_TYPES = frozenset({"DATE", "SLIDE_NUMBER", "FOOTER", "HEADER"})
_ROLE_PURPOSE = {
    "title": "slide headline area", "slide_heading": "section heading area", "body": "main content area",
    "source": "source/citation strip", "header_label": "header label area", "content_picture": "image placeholder area",
    "logo": "brand logo area", "logo_mark": "secondary logo area", "slide_number": "page number area",
    "decoration": "decorative element", "background": "background graphic",
}
_TYPE_PURPOSE = {
    "TITLE": "slide headline area", "BODY": "main content area", "OBJECT": "content area", "TEXT": "text area",
    "SUBTITLE": "subtitle area", "PICTURE": "image area", "MEDIA": "media area", "CHART": "chart area",
    "TABLE": "table area",
}


def _name(layout: Mapping[str, Any]) -> str:
    return js_str(layout.get("name") or "").lower()


def match_layout(layouts: list[dict[str, Any]] | None, slide_type: Any, framework: Any) -> dict[str, Any] | None:
    """`matchLayout`: the brand layout a slide should follow (first matching hint, else index 1 or 0)."""
    if not layouts:
        return None
    stype = (slide_type if isinstance(slide_type, str) else "").lower()
    fw = (framework if isinstance(framework, str) else "").lower()
    if stype == "divider":
        solid = [lay for lay in layouts if lay.get("backgroundType") == "solid"]
        if solid:
            named = next((lay for lay in solid if any(n in _name(lay) for n in ("section header", "section",
                                                                                 "divider"))), None)
            return named or solid[0]
    for types, frameworks, names in _TYPE_HINTS:
        if not (stype in types or any(f in fw for f in frameworks)):
            continue
        for fragment in names:
            found = next((lay for lay in layouts if fragment in _name(lay)), None)
            if found is not None:
                return found
    return layouts[min(1, len(layouts) - 1)]


def _num(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def _describe_zone(ph: Mapping[str, Any]) -> str:
    left, top, width, height = (_num(ph.get(k)) for k in ("left", "top", "width", "height"))
    h_pos = "on the right side" if left > 0.5 else "at the left edge" if left < 0.1 else "spanning the center"
    v_pos = "at the very top" if top < 0.15 else "near the bottom" if top > 0.6 else "in the body area"
    span = "full-width" if width > 0.7 else "wide" if width > 0.45 else "narrow"
    depth = ", tall panel" if height > 0.4 else ", shallow band" if height < 0.15 else ""
    return f"{span} zone {h_pos}, {v_pos}{depth}"


def _describe_deco(d: Mapping[str, Any]) -> str:
    left, top, width, height = (_num(d.get(k)) for k in ("left", "top", "width", "height"))
    h = "left" if left < 0.25 else "right" if left + width > 0.75 else "center"
    v = "top" if top < 0.25 else "bottom" if top + height > 0.75 else "middle"
    return f"{v}-{h} ({js_round(width * 100)}%×{js_round(height * 100)}% of slide)"


def _zone_purpose(ph: Mapping[str, Any]) -> str:
    role = ph.get("role")
    purpose = _ROLE_PURPOSE.get(role) if isinstance(role, str) and role else None
    ptype = js_str(ph.get("type"))
    return purpose or _TYPE_PURPOSE.get(ptype) or ptype.lower() + " area"


def layout_hint_text(layout: Mapping[str, Any], workzone: Workzone | None = None) -> str:
    """`layoutHintText`: the brand layout's zones (and the safe content area) as prompt text."""
    lines: list[str] = []
    if workzone is not None:
        def pct(n: float) -> str:
            return f"{js_round(n * 100)}%"

        lines.append("Safe content area (HARD CONSTRAINT from brand template): "
                     f"left {pct(workzone.left)}, top {pct(workzone.top)}, "
                     f"width {pct(workzone.width)}, height {pct(workzone.height)}. "
                     "Every text block, shape, and graphic must stay inside this zone — place nothing outside it.")
    lines.append(f'Slide zone positions for "{js_str(layout.get("name"))}" (from brand template — follow exactly):')
    for ph in layout.get("placeholders") or []:
        if not isinstance(ph, Mapping) or ph.get("type") in _SKIP_TYPES:
            continue
        lines.append(f"  {_zone_purpose(ph)}: {_describe_zone(ph)}")
    reserved = [d for d in layout.get("decorations") or []
                if isinstance(d, Mapping) and d.get("role") in ("logo", "logo_mark", "background")]
    if reserved:
        labels = {"background": "full-bleed background graphic", "logo_mark": "logo mark"}
        descs = [f"{labels.get(str(d.get('role')), 'brand logo')} at {_describe_deco(d)}" for d in reserved]
        lines.append(f"  RESERVED — do not draw content over these zones: {'; '.join(descs)}.")
    lines.append("This spatial layout is a hard constraint from the brand template. Follow it exactly.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------- workzone tiles
FULL_SLIDE_PX = (2560, 1440)
_MIN_PX, _MAX_PX, _MAX_EDGE, _MAX_RATIO = 655360, 8294400, 3840, 3


def _r16(n: float) -> int:
    return max(16, js_round(n / 16) * 16)


def _ceil16(n: float) -> int:
    return max(16, math.ceil(n / 16) * 16)


def workzone_px_to_image_size(w: float, h: float) -> tuple[int, int, bool]:
    """`workzonePxToImageSize`: a gpt-image-2 size near (w, h): edges divisible by 16, aspect at most 3:1,
    total pixels within the model's window, longest edge at most 3840. Returns (width, height, padded)."""
    width, height, padded = _r16(w), _r16(h), False
    if width / height > _MAX_RATIO:
        height, padded = _ceil16(width / _MAX_RATIO), True
    elif height / width > _MAX_RATIO:
        width, padded = _ceil16(height / _MAX_RATIO), True
    if width * height < _MIN_PX:
        s = math.sqrt((_MIN_PX * 1.02) / (width * height))
        width, height = _ceil16(width * s), _ceil16(height * s)
    if width * height > _MAX_PX:
        s = math.sqrt((_MAX_PX * 0.98) / (width * height))
        width, height = _r16(width * s), _r16(height * s)
    if max(width, height) > _MAX_EDGE:
        s = _MAX_EDGE / max(width, height)
        width, height = _r16(width * s), _r16(height * s)
    while max(width, height) / min(width, height) > _MAX_RATIO:
        if width > height:
            height += 16
        else:
            width += 16
    return width, height, padded


def workzone_image_size(z: Workzone) -> str:
    """`workzoneImageSize`, as the image API's size string ("WxH")."""
    w, h, _ = workzone_px_to_image_size(z.width * FULL_SLIDE_PX[0], z.height * FULL_SLIDE_PX[1])
    return f"{w}x{h}"


# ---------------------------------------------------------------------------- brand resolution
@dataclass(frozen=True)
class BrandSource:
    """`resolveBrandSource`: the kit to use, and the brand whose assets may be read (None: the legacy
    per-user kit, which has no brand assets here)."""

    kit: Any
    brand_id: str | None


async def resolve_brand_source(storage: Storage, ctx: CallerContext, brand_id: Any) -> BrandSource:
    """The named brand when the caller can read it, else the caller's legacy profile kit (Darwin's
    fallback for a missing, foreign or malformed brand id). A storage outage propagates."""
    if isinstance(brand_id, str) and brand_id:
        try:
            access = await storage.brands.get(ctx, brand_id)
            return BrandSource(kit=access.brand.kit, brand_id=access.brand.id)
        except (NotFound, Forbidden, ValueError):
            _log.info("brand %s not readable by the caller; using the profile kit", brand_id)
    profile = await storage.users.get_me(ctx)
    return BrandSource(kit=profile.brand_kit, brand_id=None)


# --------------------------------------------------------------------------- the storyline prompter
class DarwinSlidePrompter:
    """The storyline job's `SlidePrompter` (`app/core/storyline/ports.py`): each slide's image prompt as
    `storyline-background.ts` built it (the caller's brand kit, the matched layout's hint, the deck's style
    instructions and language; never the content-only tile variant)."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    async def __call__(self, owner_subject: str, inputs: dict[str, Any], slides: list[dict[str, Any]]) -> list[str]:
        ctx = CallerContext(subject=owner_subject, request_id="job:storyline")
        try:
            raw_kit = (await resolve_brand_source(self._storage, ctx, inputs.get("brandId"))).kit
        except Exception:  # noqa: BLE001 - a storage outage must not re-run (and re-pay) the storyline call
            _log.exception("storyline prompts: the brand kit could not be read; using the default kit")
            raw_kit = None
        kit = prompt_kit(raw_kit, inputs)
        style = inputs.get("styleInstructions")
        language = inputs.get("language") if isinstance(inputs.get("language"), str) else None
        out: list[str] = []
        for slide in slides:
            matched = match_layout(kit.layouts, slide.get("type"), slide.get("framework")) if kit.layouts else None
            hint = layout_hint_text(matched, kit.workzone) if matched is not None else None
            out.append(assemble_slide_prompt(slide, kit, style if isinstance(style, str) else None, hint, language))
        return out
