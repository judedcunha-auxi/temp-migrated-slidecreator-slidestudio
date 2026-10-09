"""What the design turn is asked to do: a storyline slide spec, or a picture to rebuild, as an instruction.

Darwin's storyline already holds every design decision as text (action title, slide type, framework,
bullets, chart data, archetype id). Spec mode hands that text to the design turn directly; image
mode hands it a finished picture to rebuild natively. Both instructions, the spec normaliser and the
pre-filled brief are ported from Slide Studio `server/compat/orchestrate.py` (migration plan §4.1, R).

**The storyline interface.** The storyline itself is owned by `app/core/storyline` (another work
stream). This module depends only on the minimal shape below (`StorylineSlide`, `StorylineDeck`):
the fields Darwin's storyline slide and deck already carry. Nothing here imports the storyline code.

Arabic/RTL is Phase 6: `BrandOptions.language` is carried through untouched so it can be honoured
there without changing these signatures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, TypedDict

from app.core.brand import workzone as wz
from app.core.design_refs import archetypes as arch
from app.core.design_refs import slide_brief
from app.core.design_refs import vocabulary as vocab

JSON = dict[str, Any]

#: Storyline slide types that are master furniture rather than content (Darwin `brandKit.ts`).
SPEC_LAYOUT = {"title": "cover", "divider": "divider"}
MAX_BULLETS = 8


class StorylineSlide(TypedDict, total=False):
    """The storyline slide fields design reads (minimal interface; the storyline owns the type)."""

    number: int
    id: str
    title: str
    type: str
    section: str
    framework: str
    description: str
    bullets: list[str]
    chartData: JSON
    archetypeId: str


class StorylineDeck(TypedDict, total=False):
    presentationTitle: str
    topic: str
    audience: str
    style: str
    company: str
    context: str
    keyMessages: list[str]


@dataclass
class BrandOptions:
    """What the old `/v1/jobs` form carried about the brand, as typed fields."""

    primary_color: str | None = None
    accent_color: str | None = None
    heading_font: str | None = None
    body_font: str | None = None
    heading: str | None = None
    date: str | None = None
    chart_data: str | None = None
    layout_archetype: str | None = None
    #: False is Darwin's debrand mode: the output carries no brand furniture or theme.
    apply_brand_layout: bool = True
    #: The brand's master PNG for this archetype (the raster template), and whether to paint it as
    #: the layout background.
    layout_template: bytes | None = None
    apply_template_bg: bool = True
    tile_is_content_only: bool = False
    workzone: wz.Workzone | None = None
    #: Phase 6 (Arabic/RTL). Carried, not acted on yet.
    language: str | None = None
    extra: JSON = field(default_factory=dict)


def _clean(value: Any) -> str:
    return " ".join(str(value).split()) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def normalize_spec(raw: JSON) -> JSON:
    """`spec_json` -> `{"slide": {...}, "deck": {...}}`. Accepts the wrapped form or a bare storyline
    slide. Raises ValueError when there is no slide title to design from."""
    raw_slide, raw_deck = raw.get("slide"), raw.get("deck")
    slide: JSON = raw_slide if isinstance(raw_slide, dict) else raw
    deck: JSON = raw_deck if isinstance(raw_deck, dict) else {}
    title = _clean(slide.get("title"))
    if not title:
        raise ValueError("the spec needs slide.title")
    raw_bullets, raw_chart = slide.get("bullets"), slide.get("chartData")
    bullets: list[Any] = raw_bullets if isinstance(raw_bullets, list) else []
    chart: JSON | None = raw_chart if isinstance(raw_chart, dict) else None
    out_slide = {
        "title": title,
        "type": (_clean(slide.get("type")) or "framework").lower(),
        "section": _clean(slide.get("section")),
        "framework": _clean(slide.get("framework")),
        "description": _clean(slide.get("description")),
        "bullets": [_clean(b) for b in bullets if _clean(b)][:MAX_BULLETS],
        "chartData": chart,
        "archetypeId": _clean(slide.get("archetypeId")),
    }
    raw_messages = deck.get("keyMessages")
    key_messages: list[Any] = raw_messages if isinstance(raw_messages, list) else []
    out_deck: JSON = {k: _clean(deck.get(k)) for k in ("presentationTitle", "topic", "audience", "style", "company",
                                                      "context")}
    out_deck["keyMessages"] = [_clean(m) for m in key_messages if _clean(m)][:6]
    return {"slide": out_slide, "deck": out_deck}


def spec_layout_archetype(spec: JSON) -> str:
    return SPEC_LAYOUT.get(str(spec["slide"]["type"]), "content")


def title_rule(title: str) -> str:
    return (f"The slide's title is: {title}. Emit it as its own top-level element tagged "
            f"`data-placeholder=\"title\"` (e.g. `<h1 data-placeholder=\"title\">…</h1>` as a direct child of "
            f"<body>, not inside a content container) so it fills the master's title placeholder.")


KEEP_TO_BULLETS = ("The bullets are the slide's content: restructure them into the framework's units and tighten the "
                   "wording, but keep every number and claim exactly as given and invent no new facts or figures.")
DENSE = ("The bullets are the evidence the slide must show: keep every number and claim in them exactly as given. "
         "Build a dense, composite consulting slide around them — several drawn exhibits (a chart, a table, a "
         "diagram, KPI tiles) around the one framework — and where an exhibit needs a figure the spec does not give, "
         "use your own knowledge for a realistic estimate and label it as one (e.g. \"est.\"); never write a "
         "placeholder such as [verify], TBD or XX.")


def _brand_bits(brand: BrandOptions) -> list[str]:
    bits: list[str] = []
    colours = [c for c in (brand.primary_color, brand.accent_color) if c]
    if colours:
        bits.append("Brand colours: " + ", ".join(colours) + ".")
    fonts = [f for f in (brand.heading_font, brand.body_font) if f]
    if fonts:
        bits.append("Brand fonts (heading, body): " + ", ".join(fonts) + ".")
    return bits


def _workzone_note(brand: BrandOptions, canvas_w: int, canvas_h: int) -> str | None:
    if brand.workzone is not None and not wz.is_degenerate(brand.workzone):
        return wz.design_note(brand.workzone, canvas_w, canvas_h)
    return None


def spec_instruction(spec: JSON, brand: BrandOptions, archetype: str, canvas_w: int, canvas_h: int, *,
                     dense: bool = False, brief_on: bool = True) -> str:
    """The design turn's instruction for one storyline slide (spec mode)."""
    slide, deck = spec["slide"], spec["deck"]
    bits = _brand_bits(brand)
    if archetype in ("cover", "divider"):
        cover = archetype == "cover"
        parts = [
            f"Design this deck's {'cover' if cover else 'section divider'} slide as ONE HTML slide on this "
            f"master's layout. It is nearly empty by design: the master draws the brand furniture, you add only the "
            f"heading{' and a short date line' if cover else ''}. No bullets, charts, tables, icons or extra shapes. "
            f"Call save_slide once.",
            title_rule(slide["title"] if cover or not slide["section"] else slide["section"]),
        ]
        if cover and brand.date:
            parts.append(f"Date line: {brand.date}.")
        if bits:
            parts.append(" ".join(bits))
        return "\n\n".join(parts)

    parts = [
        "Design ONE editable consulting slide from the storyline spec below, on this master's layout. There is no "
        "picture to copy: the spec is the brief, and the design decisions in it (the action title, the framework, "
        "the layout recipe) are already made — follow them. The master draws the brand header, logo and footer "
        "behind your HTML; do not redraw those. Build every element natively (text, tables, SVG, `data-chart`). "
        f"{DENSE if dense else KEEP_TO_BULLETS} Save the slide with save_slide, then preview and fix it as usual.",
        title_rule(slide["title"]),
    ]
    lines = [f"Slide type: {slide['type']}"]
    if slide["framework"]:
        canonical = vocab.canonical_framework(slide["framework"]) or slide["framework"]
        hint = vocab.framework_hint(slide["framework"])
        lines.append(f"Framework: {canonical}" + (f" — {hint}" if hint else ""))
    if slide["description"]:
        lines.append(f"What the slide must do: {slide['description']}")
    if slide["bullets"]:
        lines.append("Content:\n" + "\n".join(f"- {b}" for b in slide["bullets"]))
    parts.append("\n".join(lines))
    recipe = arch.get(slide["archetypeId"]) if slide["archetypeId"] else None
    if recipe:
        parts.append("Layout recipe (structure only; flex the unit counts to the content):\n" + arch.describe(recipe, 1))
    chart = json.dumps(slide["chartData"], ensure_ascii=False) if slide["chartData"] else brand.chart_data
    if chart:
        parts.append(f"Use this exact chart data as a native `data-chart`; never approximate it with shapes: {chart}")
    if brief_on:
        parts.append(prefilled_brief(spec))
    context = [f"{label}: {deck[key]}" for key, label in
               (("presentationTitle", "Deck"), ("topic", "Topic"), ("audience", "Audience"), ("style", "Style"),
                ("company", "Company"), ("context", "Brief")) if deck.get(key)]
    if deck.get("keyMessages"):
        context.append("Key messages: " + "; ".join(deck["keyMessages"]))
    if context:
        parts.append("Deck context (for tone and emphasis; do not put it on the slide):\n" + "\n".join(context))
    note = _workzone_note(brand, canvas_w, canvas_h)
    if note:
        parts.append(note)
    if bits:
        parts.append(" ".join(bits))
    return "\n\n".join(parts)


def prefilled_brief(spec: JSON) -> str:
    """The spec's half of the design brief, filled without a model; the designer completes it."""
    slide, deck = spec["slide"], spec["deck"]
    chart = slide["chartData"] if isinstance(slide["chartData"], dict) else None
    draft = slide_brief.prefill_from_spec(slide["title"], slide["bullets"], audience=deck.get("audience") or "",
                                          objective=slide["description"], slide_type=slide["type"],
                                          framework=slide["framework"] or None, chart=chart)
    return ("Pre-filled brief (built from the spec). Before any HTML, call write_brief with it completed: keep "
            "action_title and every fact exactly as given, add the other zones (at most 5, one hero), the devices "
            "and any derived figures (with formula and inputs):\n"
            + json.dumps(draft, ensure_ascii=False, separators=(",", ":")))


def image_instruction(brand: BrandOptions, canvas_w: int, canvas_h: int) -> str:
    """The design turn's instruction for rebuilding an attached picture (image mode)."""
    parts = [
        "The attached image is a finished consulting slide. Recreate it as ONE editable HTML slide on this master's "
        "layout. The master already draws the brand header, logo and footer behind your HTML — do not redraw those. "
        "Rebuild every element natively (text, tables, SVG, `data-chart`); never embed the screenshot. Match the "
        "source's layout, wording, numbers and colours. Call save_slide once with the complete HTML document, then "
        "preview it and fix what the review finds."
    ]
    if not brand.apply_brand_layout:
        parts.append("The picture may carry brand chrome (a header band, a logo, a footer): leave it out. Rebuild "
                     "only the slide's content; this export carries no brand furniture.")
    note = _workzone_note(brand, canvas_w, canvas_h)
    if note:
        parts.append(note)
    if brand.heading:
        parts.append(title_rule(brand.heading))
    if brand.chart_data:
        parts.append("Use this exact chart data for the chart (as a native `data-chart`), do not read values off the "
                     f"image: {brand.chart_data}")
    bits = _brand_bits(brand)
    if bits and brand.apply_brand_layout:
        parts.append(" ".join(bits))
    return "\n\n".join(parts)


def workzone_repair_instruction(sid: str, findings: list[JSON]) -> str:
    """A follow-up turn's instruction when a saved slide still leaves the workzone."""
    lines = "\n".join(f"- {f.get('rule')}: {f.get('message')}" for f in findings[:8])
    return (f"The slide you saved ({sid}) still breaks the brand's workzone:\n{lines}\n"
            "Fix it with edit_slide: move or shrink every listed block so all content sits inside the workzone and "
            "nothing covers the header band. Change positions and sizes only, not the content.")
