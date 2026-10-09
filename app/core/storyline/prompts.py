"""The storyline and intake prompts, and the JSON schemas the model answers in.

Storyline system prompt, by density:
* `standard` is Darwin's `_shared/anthropic.ts: SYSTEM`, word for word, but for the last line: Darwin forced an
  `emit_storyline` tool call, which Sonnet 5.5 rejects (forced `tool_choice` is a 400 there), so the answer is
  now a structured output (`output_config.format`) and the line says so;
* `dense` is Slide Studio's `server/storyline/prompts.py: STORYLINE_SYSTEM` (composite slides, 3-6 bullets,
  estimates never placeholders, the ghost-deck test in `executiveSummary`), with the same last-line change.

Both list the 65 framework names in Darwin's order and the archetype catalog (3 per category, Darwin's line
format). For Arabic (`language == "ar"`) the Arabic content directive is appended (Darwin
`ARABIC_CONTENT_DIRECTIVE`; the dense variant also names `executiveSummary`), and the catalog's direction
words are mirrored (`rtl.py`). English prompts are unchanged by the language code path.

The user message is Darwin's (`generateStoryline`), plus Slide Studio's structure rule when a mode other than
`auto` is set, its style instructions and attached-source lines in dense mode.

Intake: Darwin's `_shared/intake.ts: SYSTEM`, with the `update_brief` tool replaced by a structured answer
`{message, brief}` (one call per turn, always with a message: C14). Slide Studio's format question is added
only when the caller asks for the `format` field.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from app.core.storyline import archetypes
from app.core.storyline.models import IntakeBrief, StorylineInputs
from app.core.storyline.vocabulary import SLIDE_TYPES, framework_names

STANDARD_SYSTEM = """You are a top-tier strategy consultant who designs executive presentation storylines.
Rules you MUST follow:
- Every slide title is a CONCLUSION (an action title), never a topic label. e.g. "Three structural gaps drive our 23% cost premium", not "Cost gaps".
- Each slide uses EXACTLY ONE visual framework. The framework MUST be a name from this library, using its exact name: {frameworks}. Never invent a bespoke framework: remap any unusual ask to its nearest native-PowerPoint equivalent (a spider/radar chart becomes a weighted scoring matrix; a mind map becomes hub-and-spoke; any 3D or isometric visual becomes its flat equivalent). Never propose a visual that cannot be built from native PowerPoint shapes, tables, and charts.
- Provide 3-4 dense, specific bullets per slide. Prefer numbers, %, $, dates, named entities.
- For slides whose framework is a chart type (waterfall chart, funnel, 100% stacked bar chart, heatmap, bubble chart, gantt chart, value chain heatmap): also output chartData with categories (X-axis labels) and series (name + numeric values + optional formatted dataLabels). Values must be raw numbers, not strings. dataLabels should be formatted display strings e.g. "$12M", "+15%", "42%". Omit chartData for all other frameworks (diagrams, tables, process flows).
- description = two sentences on what the slide argues and why it matters.
- Assign each slide a "type" (its layout archetype):
  * furniture: "title" (slide 1), "agenda" (early), "navigator" (re-show the deck's framework map with one section highlighted; longer decks only), "closing" (last slide). Do NOT emit "divider" slides yourself — the system inserts section dividers automatically from your sections.
  * body workhorses: "framework", "data", "table", "comparison", "process", "timeline".
  * evidence: "benchmark" (peer evidence in logo-anchored rows), "case-study" (one named real-world subject profiled), "map" (geography as the hero).
  * evaluation & synthesis: "scorecard" (one option judged against fixed criteria), "synthesis" (evidence flowing into numbered recommendations).
  * program detail: "charter" (one initiative as a one-pager card), "deep-dive" (drill into one element of a parent framework), "org".
  * accents, used sparingly: "kpi", "market", "quote".
  The type must match the slide's content and chosen framework.
- Sections: assign EVERY slide a "section" — the short name of the thematic part it belongs to (e.g. "Diagnosis", "Options", "Recommendation"). Consecutive slides sharing a section form one part of the deck.
  * For decks of 8+ slides, organize the body into 2-4 clean, meaningful sections. Shorter decks use a single section (still set "section" on each slide).
  * The "title"/"agenda" opening and the "closing" slide take the same "section" as their neighbour — they are not section breaks.
  * Do NOT create "divider" slides yourself. The system automatically inserts a section-divider slide at each section boundary from these "section" values — your job is just to make the sections clean and correct.
- Optionally assign archetypeId: choose the most specific matching entry from the layout catalog below. Use archetypeId ONLY when a catalog entry's purpose closely matches this slide's content and chosen framework. Omit it for furniture slides (title, agenda, divider, navigator, closing) and when no entry fits well. Never repeat an archetypeId within 3 slides. The user never sees archetype names.
  Layout catalog (id | category | name: purpose):
{catalog}
- Hard content budget: across all bullets and descriptions, aim for ≤110 words per slide. Fewer, stronger statements outperform dense text.
- No subtitles. Do not propose more than one framework per slide.
Return your answer ONLY as the storyline JSON object the response format defines."""

DENSE_SYSTEM = """You are a top-tier strategy consultant who designs executive presentation storylines.
Your storyline goes to a designer who builds every body slide as a dense, composite consulting slide: several
drawn exhibits on one page (a chart, a table, a diagram, KPI tiles), like a top-tier strategy firm's final deck.
Your job is the argument and the evidence; the designer decides the look.

Rules you MUST follow:
- Every slide title is a CONCLUSION (an action title), never a topic label. e.g. "Three structural gaps drive our 23% cost premium", not "Cost gaps". One sentence, at most about 15 words.
- The ghost-deck test: the titles alone, read in order, must tell the whole story as one argument (lead with the answer, then the pillars that prove it; or situation, complication, resolution). Write that argument in executiveSummary, 2-3 sentences built only from the titles. If it does not read as one argument, rewrite the titles before you answer.
- Each slide uses EXACTLY ONE primary visual framework. The framework MUST be a name from this library, using its exact name: {frameworks}. Never invent a bespoke framework: remap any unusual ask to its nearest native-PowerPoint equivalent (a spider/radar chart becomes a weighted scoring matrix; a mind map becomes hub-and-spoke; a pie becomes a 100% stacked bar chart; any 3D or isometric visual becomes its flat equivalent). Never propose a visual that cannot be built from native PowerPoint shapes, tables and charts.
- Give every body slide 3-6 dense, specific bullets: the evidence the slide must show, enough to fill several exhibits. Prefer numbers, %, $, dates and named entities, and name what each figure is compared against (vs last year, vs peers, vs target). When the user gave no data, use your own knowledge and give realistic figures as your best estimate; never write placeholders such as [verify], TBD, XX or "insert figure".
- chartData: when a body slide's argument rests on a series of numbers (and always when its framework is a chart: waterfall chart, funnel, 100% stacked bar chart, heatmap, bubble chart, gantt chart, value chain heatmap), output chartData with categories (X-axis labels) and series (name + numeric values + optional formatted dataLabels). Values are raw numbers, not strings; dataLabels are display strings e.g. "$12M", "+15%". Omit chartData when no series of numbers carries the slide.
- description = two sentences on what the slide argues and why it matters.
- Assign each slide a "type" (its layout archetype):
  * furniture: "title" (slide 1), "agenda" (early), "navigator" (re-show the deck's framework map with one section highlighted; longer decks only), "closing" (last slide). Do NOT emit "divider" slides yourself — the system inserts section dividers automatically from your sections.
  * body workhorses: "framework", "data", "table", "comparison", "process", "timeline".
  * evidence: "benchmark" (peer evidence in logo-anchored rows), "case-study" (one named real-world subject profiled), "map" (geography as the hero).
  * evaluation & synthesis: "scorecard" (one option judged against fixed criteria), "synthesis" (evidence flowing into numbered recommendations).
  * program detail: "charter" (one initiative as a one-pager card), "deep-dive" (drill into one element of a parent framework), "org".
  * accents, used sparingly: "kpi", "market", "quote".
  The type must match the slide's content and chosen framework. Vary the types and frameworks across the deck.
- Sections: assign EVERY slide a "section" — the short name of the thematic part it belongs to (e.g. "Diagnosis", "Options", "Recommendation"). Consecutive slides sharing a section form one part of the deck.
  * For decks of 8+ slides, organize the body into 2-4 clean, meaningful sections. Shorter decks use a single section (still set "section" on each slide).
  * The "title"/"agenda" opening and the "closing" slide take the same "section" as their neighbour — they are not section breaks.
- Optionally assign archetypeId: choose the most specific matching entry from the layout catalog below. Use archetypeId ONLY when a catalog entry's purpose closely matches this slide's content and chosen framework. Omit it for furniture slides (title, agenda, divider, navigator, closing) and when no entry fits well. Never repeat an archetypeId within 3 slides. The user never sees archetype names.
  Layout catalog (id | category | name: purpose):
{catalog}
- Do not propose more than one primary framework per slide; supporting exhibits are the designer's call.
Return your answer ONLY as the storyline JSON object the response format defines."""

ARABIC_DIRECTIVE = """

LANGUAGE — ARABIC OUTPUT:
Write ALL reader-facing copy in Modern Standard Arabic (الفصحى): the presentationTitle, {summary}every slide title, every description, all bullets, and every "section" name. For chart slides, also write the chartData categories and series names in Arabic.
- Keep Latin brand, product, and company names and standard units/acronyms (e.g. USD, EBITDA, KPI, %, $) in their original Latin script; do not transliterate them.
- Keep every numeric value as digits in Western/Latin numerals (0-9); do NOT convert to Arabic-Indic numerals (٠١٢٣). Keep dataLabels as Latin-digit display strings (e.g. "$12M", "+15%", "42%").{short}
- Do not add English translations in parentheses. The visual framework name may remain in English."""

ARABIC_SHORT_BULLETS = (
    "\n- Arabic reads longer than English — keep bullets short and punchy (fewer words than an English "
    "equivalent) so they stay legible when rendered.")


@lru_cache(maxsize=8)
def storyline_system(density: str = "standard", language: str = "en") -> str:
    """The storyline system prompt. Cached: it is stable per (density, language), which also keeps the
    provider's prompt cache warm."""
    template = DENSE_SYSTEM if density == "dense" else STANDARD_SYSTEM
    text = template.replace("{frameworks}", "; ".join(framework_names())).replace(
        "{catalog}", archetypes.catalog_text(language))
    if language == "ar":
        text += ARABIC_DIRECTIVE.replace("{summary}", "the executiveSummary, " if density == "dense" else "").replace(
            "{short}", "" if density == "dense" else ARABIC_SHORT_BULLETS)
    return text


def structure_rule(mode: str, n: int) -> str:
    """Slide Studio's per-mode structure line. `auto` (Darwin) has none: the system rules place the furniture."""
    if mode == "single":
        return ("Exactly 1 slide: a single body slide that makes the whole point on one page. No title, agenda, "
                "divider or closing slide.")
    if mode == "collection":
        return (f"Exactly {n} body slides: a self-contained set to drop into a larger deck. No title, agenda, "
                "divider, navigator or closing slides.")
    if mode == "deck":
        return (f"An entire deck of {n} slides: slide 1 is type \"title\", slide 2 is type \"agenda\", the last "
                "slide is type \"closing\", and the slides between are body slides in clean sections. Do not emit "
                "dividers; they are inserted automatically and do not count toward the total.")
    return ""


def storyline_user(inputs: StorylineInputs, sources: str = "") -> str:
    """Darwin's user message; for `auto` mode and standard density, the same text Darwin sent."""
    n = inputs.num_slides
    lines = [
        f"Create a {n}-slide presentation storyline.",
        f"Topic: {inputs.topic}",
        f"Company / brand reference: {inputs.company or 'generic professional'}",
        f"Audience: {inputs.audience or 'Executive committee'}",
        f"Style preset: {inputs.style or 'Executive strategy'}",
    ]
    if inputs.density == "dense" and inputs.style_instructions:
        lines.append(f"Style instructions: {inputs.style_instructions}")
    if inputs.context:
        lines.append(f"Alignment notes from intake: {inputs.context}")
    if inputs.key_messages:
        lines.append("Key messages that must land: " + "; ".join(inputs.key_messages))
    rule = structure_rule(inputs.mode, n)
    if rule:
        lines.append(rule)
    lines.append(f"Number the slides 1..{n}.")
    if sources:
        lines.append("Source material the user attached (use its facts and figures, quoting numbers exactly as "
                     "given; fill any gap with your own estimate):\n" + sources)
    return "\n".join(lines)


def _chart_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "description": "Structured chart data (see the rules for when to include it).",
        "properties": {
            "categories": {"type": "array", "items": {"type": "string"}, "description": "X-axis category labels"},
            "series": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "values": {"type": "array", "items": {"type": "number"},
                                   "description": "Raw numeric values, one per category"},
                        "dataLabels": {"type": "array", "items": {"type": "string"},
                                       "description": "Formatted display labels e.g. '$12M', '+15%', '42%'. "
                                                      "One per value. Optional."},
                    },
                    "required": ["name", "values"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["categories", "series"],
        "additionalProperties": False,
    }


def storyline_schema(density: str = "standard") -> dict[str, Any]:
    """The storyline's output schema: Darwin's `claudeToolInputSchema` in the structured-output subset (array
    lengths are stated in the descriptions and enforced by `validation`). Dense adds `executiveSummary`."""
    dense = density == "dense"
    bullets = ("The evidence the slide must show: 3-6 dense, specific bullets for body slides; furniture slides "
               "may have none") if dense else "3-4 dense, specific bullets (furniture slides may have none)"
    slide: dict[str, Any] = {
        "type": "object",
        "properties": {
            "number": {"type": "integer", "description": "1-based position in the deck"},
            "title": {"type": "string", "description": "Conclusion-oriented action title, not a topic label"},
            "type": {"type": "string", "enum": list(SLIDE_TYPES),
                     "description": "Slide archetype (layout family) for the slide"},
            "section": {"type": "string", "description": (
                "Short name of the thematic section this slide belongs to (e.g. \"Diagnosis\", \"Options\", "
                "\"Recommendation\"). Every slide gets one; dividers are inserted automatically at section changes.")},
            "framework": {"type": "string", "description": (
                "Exactly one visual framework — MUST be a framework name from the library given in the system "
                "rules. Remap unusual asks to the nearest library equivalent.")},
            "description": {"type": "string", "description": "Two sentences: what the slide argues and why it matters"},
            "bullets": {"type": "array", "items": {"type": "string"}, "description": bullets},
            "archetypeId": {"type": "string", "description": (
                "Optional layout catalog id. Omit for furniture slides (title, agenda, divider, navigator, closing) "
                "and when no catalog entry closely matches this slide's content.")},
            "chartData": _chart_schema(),
        },
        "required": ["number", "title", "type", "framework", "description", "bullets"],
        "additionalProperties": False,
    }
    properties: dict[str, Any] = {"presentationTitle": {"type": "string"}}
    required = ["presentationTitle", "slides"]
    if dense:
        properties["executiveSummary"] = {"type": "string", "description": (
            "The deck's argument in 2-3 sentences, built only from the slide titles read in order "
            "(the ghost-deck test).")}
        required.insert(1, "executiveSummary")
    properties["slides"] = {"type": "array", "items": slide}
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


# ------------------------------------------------------------------------------------------------ intake
INTAKE_SYSTEM = """You are a sharp, friendly strategy-consulting engagement manager running a deck-intake conversation.
Rules:
- Ask exactly ONE focused question per reply. Keep replies under 80 words.
- Cover, roughly in order: purpose/goal, audience and the decision they'll make, topic specifics, 3-6 key messages that must land, {count}, tone/emphasis.
- Every reply is a JSON object: "message" is your next conversational message to the user (always present, never empty); "brief" holds JUST the brief fields pinned down by the user's latest message ({{}} when none changed).
- When the user states points that must land, record them as keyMessages immediately — do not re-ask for what they already told you.{facts}
- Once the essentials are covered, propose a short outline in chat and ask for agreement. When the user agrees, set ready: true in the brief and tell them to hit "Draft storyline".
- Don't ask about branding, colors, or fonts — the product handles those elsewhere.
- Be concrete, use the user's vocabulary, don't pad."""

INTAKE_FACTS = ("\n- When the user shares facts, figures or files, distil the ones the deck needs into context "
                "(keep numbers exact).")

#: Darwin's fixed replies (no model call), and its filler when the model gives no message.
WRAP_UP_MESSAGE = ("We've covered a lot — let's draft with what we have. Hit “Draft storyline” "
                   "whenever you're ready.")
OPENER_MESSAGE = "Tell me about the deck you need — what's it for?"
FALLBACK_MESSAGE = "Noted — anything else before we draft?"

INTAKE_FORMATS: tuple[str, ...] = ("single", "collection", "deck")


@lru_cache(maxsize=2)
def intake_rules(ask_format: bool = False) -> str:
    count = ("format and slide count (one slide, a collection of slides, or an entire deck)"
             if ask_format else "slide count")
    return INTAKE_SYSTEM.replace("{count}", count).replace("{facts}", INTAKE_FACTS if ask_format else "").replace(
        "{{}}", "{}")


def intake_system(brief: IntakeBrief | dict[str, Any], ask_format: bool = False) -> str:
    """The rules plus the brief so far, as Darwin sends it (`JSON.stringify` of the sanitized brief)."""
    data = brief.wire() if isinstance(brief, IntakeBrief) else brief
    state = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"{intake_rules(ask_format)}\n\nCurrent brief state (already captured — do not re-ask):\n{state}"


def intake_schema(ask_format: bool = False) -> dict[str, Any]:
    """`{message, brief}`: Darwin's `update_brief` tool fields as an optional-only object."""
    brief: dict[str, Any] = {
        "topic": {"type": "string"},
        "audience": {"type": "string"},
        "numSlides": {"type": "integer", "description": "At least 1"},
        "keyMessages": {"type": "array", "items": {"type": "string"}, "description": "At most 6"},
        "context": {"type": "string", "description": "Distilled framing: goal, constraints, tone, emphasis"},
        "ready": {"type": "boolean", "description": "true once the user has agreed to the proposed outline"},
    }
    if ask_format:
        brief["format"] = {"type": "string", "enum": list(INTAKE_FORMATS), "description": (
            "single = one slide; collection = a set of body slides; deck = a full deck with title, agenda, "
            "sections and closing")}
    return {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Your next conversational message to the user"},
            "brief": {"type": "object", "properties": brief, "additionalProperties": False,
                      "description": "Only the fields pinned down this turn"},
        },
        "required": ["message", "brief"],
        "additionalProperties": False,
    }
