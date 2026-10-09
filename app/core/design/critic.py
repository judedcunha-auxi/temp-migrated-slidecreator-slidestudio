"""The independent critic behind `preview_slide`: a second model looks at the rendered slide fresh.

Why a separate call: the model that wrote a slide tends to see what it meant rather than what
rendered ("the agent tends to rationalize defects", DeepPresenter; `docs/DESIGN-QUALITY-RESEARCH.md`
§1.3 and recommendation #3). So the critic gets a fresh context: no chat history, no design
reasoning, only the picture, the deterministic findings (export lint and design review), a few facts
about the slide, and a strict YES/NO checklist. It answers with at most five concrete fixes, each
tied to an element, or PASS.

When a slide was already previewed earlier in the same turn, the earlier picture and critique go in
too and the critic says, per item that failed, whether the new version is better, the same or worse.
No numeric scores anywhere: VLM judges rank and compare far more reliably than they score (§1.2).

The call is one small non-streaming round (`MAX_TOKENS`) on the critic's Claude model
(`LLM_CRITIC_MODEL`, Sonnet 5.5 by default) whatever model drives the design chat, with as little
thinking as the model allows and `LLM_CRITIC_EFFORT`. `critique` returns None, and the preview goes
out without a review, when the critic is switched off (`DESIGN_CRITIC_ENABLED`), when no provider
can serve its model, or when the call fails. It never raises.

What it cost is on the result (`usage`, priced with the critic's own model); the design turn adds it
to the turn's total, since it is spent outside the provider's rounds.

Ported from Slide Studio `server/critic.py` (migration plan §4.1, K); the call goes through the
provider port instead of the SDK.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.config.ai import AISettings
from app.core.llm import loop
from app.core.llm.ports import LLMProvider
from app.core.llm.types import RoundRequest

_log = logging.getLogger(__name__)

JSON = dict[str, Any]

#: A critique is a short list; anything longer is the critic over-editing. The headroom over the
#: list itself is for the little thinking a model that cannot switch it off still does.
MAX_TOKENS = 4000

#: The fixes a critique may list.
MAX_FIXES = 5

#: The recognisable "AI-generated slide" tells (`docs/architecture/design-chat.md`). Decoration only:
#: devices that encode something (pills, numbered markers, check and +/- bullets) are named as NOT tells,
#: because a critic that flags them strips a dense slide back to plain text.
AI_LOOK = ("It does not look AI-generated: no card with a coloured stripe on one side, no emoji, no "
           "decorative icon on every card or bullet, not everything centred, no eyebrow or kicker label over "
           "the title or headings, no italic accent word in the title, no gradients, glows or gradient text. "
           "Devices that carry meaning are NOT tells: status pills, numbered markers for a sequence or a key, "
           "check/cross or +/- bullets, bold lead-ins, a legend.")

#: Body slides are composed (main exhibit + side panel and/or closing strip); these slides are not.
EXEMPT = "a cover, agenda, divider, quote or closing slide, or a slide whose whole message is one number"

COMPOSITION = (f"Composed (YES for {EXEMPT}): a main exhibit plus supporting units — a side panel "
               "(verdict, recommendation, implication or detail) and/or a closing strip; not a lone list or "
               "exhibit. Exactly ONE unit states the so-what (no second box restating it or the title); every "
               "other unit adds evidence (drivers, a breakdown, figures, a verdict on one item).")

CANVAS = (f"The canvas is used (YES for {EXEMPT}): the body fills the space from the title to the source "
          "line, margin to margin; side-by-side columns share their top and bottom edges; stacked units sit "
          "one gutter apart (no hole wider than about 36 px between them); no panel is hollow (text in its "
          "top part, blank below); no empty band or dead half; no crowding (gutters and padding of about 8 px "
          "or more). Body type is compact (about 10.5-12 pt) rather than a few large lines.")

#: The exhibit item (`docs/DESIGN-DEVICES-RESEARCH.md` §3.2): evidence is drawn, not listed, and the drawing
#: points at the insight — the right way round when a lower value is the better one.
EXHIBIT = ("The exhibit shows the evidence and points at the insight (YES for a slide without figures): the "
           "quantities are drawn (bars on one stated scale, ticks, balls, heat, markers), not only listed as "
           "numbers; one focus colour on the subject with context grey, and one annotation (target or median "
           "line, delta badge, callout, numbered markers keyed to the text); the encoding reads the right way "
           "where lower is better (a cost or ratio gap coloured as bad, not as a win); and the planned device, "
           "when a plan is given, is visible.")

CHECKLIST = (
    "Every text fits its box: nothing overflows, is clipped, is cut off at the slide edge, or wraps "
    "into a box below it.",
    "No unintended overlaps or collisions: no text over text, text over shapes or lines, or labels "
    "over data marks.",
    "Elements sit on the margins and a common grid: no near-miss alignments (edges a few px apart "
    "that should line up), equal gutters between sibling blocks, nothing outside the margins.",
    "The title is an action title (a full-sentence so-what, not a topic label) and runs to at most "
    "2 lines.",
    "Colour is disciplined: the master's theme colours in at most three emphasis weights, at most one "
    "accent hue for the one thing that matters, greys for context; no rainbow. Exactly one unit is the "
    "solid-filled hero, and it is the recommendation, verdict or focal item — not the closing strip, which "
    "sits on a light tint or plain; peers are tinted, detail is white and outlined.",
    "Clear hierarchy that scans: the eye lands on the title, then the main exhibit or hero unit; parallel "
    "units share one structure, and units that carry the same kinds of content (e.g. metric, figures, why, "
    "action) put each kind on one aligned row or under shared row labels; items open with a bold lead-in.",
    COMPOSITION,
    CANVAS,
    EXHIBIT,
    "Everything is legible: no text below 8 pt (about 11 px on a 1280 px canvas), and enough contrast "
    "with what is behind it.",
    "A slide that shows data (a chart, a table, figures) carries a source line.",
    "Charts are labelled: axis or data labels, and units stated.",
    "Nothing is drawn over the master's furniture (logo, footer, page number, layout decoration), and "
    "none of it is redrawn by the slide.",
    AI_LOOK,
)

#: The checklist number of the AI-look item: a NO there is always a [must] fix — partners send these back.
AI_LOOK_ITEM = CHECKLIST.index(AI_LOOK) + 1
#: The composition and canvas items: a NO there asks for more (or larger) content, which the layout-only
#: rule below would otherwise forbid.
COMPOSITION_ITEM = CHECKLIST.index(COMPOSITION) + 1
CANVAS_ITEM = CHECKLIST.index(CANVAS) + 1
EXHIBIT_ITEM = CHECKLIST.index(EXHIBIT) + 1

SYSTEM = f"""You are an independent reviewer of consulting slides (McKinsey, BCG, Bain and \
investment-banking standard). Another model designed the slide in HTML; it is rendered over its \
PowerPoint master layout and shown to you as a picture. You did not design it and you owe it \
nothing: your job is to find what is visibly wrong, not to approve.

Judge only what you can see in the picture. The deterministic findings you are given (export lint \
and design review) were measured by code: treat them as facts, do not argue with them, and repeat \
one only when you can see its effect or it needs a concrete fix.

Answer the checklist below strictly YES (the slide meets it) or NO (it does not), one line each, \
with at most a few words of evidence on a NO. An item that does not apply (no chart on the slide) \
is YES.

{chr(10).join(f"{n}. {item}" for n, item in enumerate(CHECKLIST, 1))}

Then list at most {MAX_FIXES} fixes, most important first. Each fix:
- names the element it is about: its visible text in quotes (the first few words) or its data-name;
- says what is wrong and what to change, concretely (px, pt, which edge to align to which), so it \
can be applied as a small edit to the HTML;
- is tagged [must] (a defect a partner would send back) or [nice] (polish).
Every AI-look tell behind a NO on item {AI_LOOK_ITEM} is a [must] fix: replace a side stripe with a full \
1px border, a solid fill or bold text; delete emoji; keep icons only where they carry meaning; \
left-align; drop the eyebrow label, the italic accent and the gradient. Never ask to remove a pill, \
a numbered marker, a check or +/- bullet or a bold lead-in that encodes something.
A NO on item {COMPOSITION_ITEM} or {CANVAS_ITEM} is a [must] when it is a duplicate takeaway, a hole \
between units, a hollow panel, an empty band or a lone exhibit (so is a stack-gap, hollow-panel or \
empty-band finding); only a minor unevenness is [nice]. The fix re-proportions: stretch or shrink units \
so columns share top and bottom edges and panels fit their content, merge or cut the duplicate \
takeaway, and spend freed space on evidence (e.g. "shrink the side panels to their text and \
align their tops with the table rows; turn the second takeaway box into a breakdown of the drivers"). \
Never ask for filler; the designer writes any new text from the brief, so do not write facts yourself.
A NO on item {EXHIBIT_ITEM} is a [must] on a slide that carries data (so is an exhibit-plain or \
chart-no-focus finding): name the encoding to add (a bar column on one scale, the benchmark as a tick, \
`pointColors` on the focal bar, a target line) or the direction to fix, using the slide's own numbers.
Prioritise: at most {MAX_FIXES} fixes, [must] first, and batch related ones into one fix so the designer \
can apply them in a single edit. On a re-review, list only [must] fixes that still fail or that the last \
edit broke; add no new [nice] polish then. A [nice] never blocks PASS.
Otherwise fix layout and presentation only. Never change the numbers, the facts or the wording, \
except to shorten a title or a text that does not fit. Do not propose a redesign or a different \
exhibit unless a checklist item fails because of it.

If you have no [must] fix, the verdict is PASS.

Answer in exactly this format and nothing else:
CHECKLIST
1. YES
2. NO: <evidence>
...
FIXES
1. [must] "<element>": <what is wrong> -> <concrete fix>
...
VERDICT: PASS or VERDICT: FIX
(Write "FIXES" followed by "none" when there are none.)"""

COMPARE = """This slide was reviewed earlier in this session. The FIRST picture is the new version, \
the SECOND picture is the previous one, and the previous review is below. After the checklist and \
fixes for the new version, add a section:
COMPARED WITH THE PREVIOUS VERSION
- <item number or fix>: better | same | worse (a few words)
one line for each item that failed or each fix listed in the previous review. Do not score."""


@dataclass
class CritiqueResult:
    """What the critic said, and what saying it cost."""

    text: str                       # the critique as the critic wrote it (trimmed)
    passed: bool                    # the critic's verdict was PASS
    model: str                      # the model that wrote it
    usage: JSON = field(default_factory=dict)   # priced usage JSON (costUsd, model, ...)
    compared: bool = False          # a previous picture was compared against

    def for_designer(self) -> str:
        """The critique as the tail of a `preview_slide` result."""
        head = ("Independent review (a separate model looked at this render with fresh eyes; it has not "
                "seen your reasoning). Apply the [must] fixes, with edit_slide where you can; then "
                "preview again (the prompt says how many times):")
        if self.passed:
            head = "Independent review (a separate model looked at this render with fresh eyes): PASS."
        return f"{head}\n{self.text}"


def critique(provider: LLMProvider | None, model: str, image_b64: str, lint_text: str, design_text: str,
             context: JSON | None, settings: AISettings) -> CritiqueResult | None:
    """Ask the critic about one rendered slide. None when it is off, unavailable, or failed.

    `image_b64` is the picture as base64 (`context["media_type"]`, default image/jpeg). `context`
    may carry: `slide_id`, `version`, `title`, `layout` (its name), `zones`, `canvas`, `exhibit_plan`,
    `brief_checks`, and for a before/after judgement `previous_image` and `previous_critique`.
    """
    if not image_b64 or provider is None or not settings.design_critic_enabled:
        return None
    context = context or {}
    media = context.get("media_type") or "image/jpeg"
    previous = context.get("previous_image")
    content: list[JSON] = [_image(image_b64, media)]
    if previous:
        content.append(_image(previous, media))
    content.append({"type": "text", "text": _brief(lint_text, design_text, context, bool(previous))})
    request = RoundRequest(model=model, system=SYSTEM, messages=[{"role": "user", "content": content}],
                           max_tokens=MAX_TOKENS, effort=settings.llm_critic_effort, minimal_thinking=True)
    try:
        text, usage = loop.complete(provider, request, settings)
    except Exception as exc:  # noqa: BLE001 - no key, outage, a rejected request: the preview stands alone
        _log.warning("critic call failed: %s", " ".join(str(exc).split())[:300])
        return None
    if not text:
        # a refusal or an empty reply still cost what it cost: report it, with nothing to say
        return CritiqueResult(text="(The reviewer returned nothing.)", passed=False, model=model, usage=usage,
                              compared=bool(previous))
    return CritiqueResult(text=_trim(text), passed=_passed(text), model=model, usage=usage, compared=bool(previous))


# ------------------------------------------------------------------------------------ helpers
def _image(data: str, media: str) -> JSON:
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}


def _brief(lint_text: str, design_text: str, context: JSON, compare: bool) -> str:
    """The user message's text: what the slide is, what code measured, and (maybe) the comparison."""
    facts = []
    if context.get("slide_id"):
        version = f" version {context['version']}" if context.get("version") is not None else ""
        facts.append(f"Slide {context['slide_id']}{version}.")
    if context.get("title"):
        facts.append(f"Its title in the slide list: {context['title']}.")
    if context.get("canvas"):
        facts.append(f"Canvas: {context['canvas']} px (16:9 at 96 px per inch unless stated).")
    if context.get("layout"):
        facts.append(f"Master layout: {context['layout']}.")
    if context.get("zones"):
        facts.append(f"Layout zones (the master's own placeholders and furniture): {context['zones']}.")
    if context.get("exhibit_plan"):
        facts.append(f"The designer's exhibit plan: {context['exhibit_plan']} — check that device is visible.")
    parts = [" ".join(facts) or "A slide."]
    checks = [c for c in context.get("brief_checks") or [] if isinstance(c, (list, tuple)) and len(c) == 2]
    if checks:
        # the designer's accepted brief (`slide_brief.critic_items`): a NO on a [must] line is a [must] fix
        parts.append("The designer's brief. Check each line against the picture too; a NO on a [must] line is a "
                     "[must] fix, on a [nice] line a [nice] fix. Put these after the checklist, before FIXES, as "
                     "\"B1. YES\" or \"B1. NO: <evidence>\":\n"
                     + "\n".join(f"B{n}. [{sev}] {line}" for n, (line, sev) in enumerate(checks, 1)))
    parts += ["Deterministic findings:",
             (lint_text or "Export lint: not run.").strip(),
             (design_text or "Design review: not run.").strip()]
    if compare:
        parts += [COMPARE, "Previous review:", (context.get("previous_critique") or "(none recorded)").strip()]
    parts.append("Review the slide in the (first) picture now.")
    return "\n\n".join(parts)


def _passed(text: str) -> bool:
    verdict = re.search(r"VERDICT:\s*(PASS|FIX)", text, re.IGNORECASE)
    if verdict:
        return verdict.group(1).upper() == "PASS"
    return text.strip().upper() == "PASS"


def _trim(text: str) -> str:
    """Keep what the designer needs: drop the YES lines of the checklist (a NO says what failed) and
    any fix past `MAX_FIXES`."""
    out: list[str] = []
    section, fixes = "", 0
    for line in text.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if upper in ("CHECKLIST", "FIXES") or upper.startswith("COMPARED WITH") or upper.startswith("VERDICT"):
            section = upper
            out.append(stripped)
            continue
        if section == "CHECKLIST" and re.match(r"^\d+\.\s*YES\b", stripped, re.IGNORECASE):
            continue
        if section == "FIXES" and re.match(r"^\d+\.", stripped):
            fixes += 1
            if fixes > MAX_FIXES:
                continue
        if stripped:
            out.append(stripped)
    # a checklist with every item YES reads better said once than as an empty heading
    cleaned: list[str] = []
    for i, line in enumerate(out):
        if line.upper() == "CHECKLIST" and (i + 1 == len(out) or out[i + 1].upper() in ("FIXES", "VERDICT: PASS",
                                                                                         "VERDICT: FIX")):
            cleaned.append("CHECKLIST: all YES")
            continue
        cleaned.append(line)
    return "\n".join(cleaned)
