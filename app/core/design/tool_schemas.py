"""The design chat's tools as the model is offered them: their JSON schemas, and which a turn gets.

Ported from Slide Studio `server/chat/tool_schemas.py` (migration plan §4.1, K). `delete_slide` is
not offered (the service's design turns never remove a slide; deleting is a deck operation).
`edit_slide` and `save_slide` stream their inputs eagerly (`eager_input_streaming`); the turn loop
never runs a tool call that a `max_tokens` stop cut off, and the tools validate every input.
"""

from __future__ import annotations

import logging
from types import ModuleType
from typing import Any

from app.core.design_refs import archetypes, exemplars, slide_brief

_log = logging.getLogger(__name__)

JSON = dict[str, Any]

SAVE_SLIDE: JSON = {
    "name": "save_slide",
    "description": "Create a new slide or save a new version of an existing slide. Always pass the complete HTML document.",
    "eager_input_streaming": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "slide_id": {"type": "string", "description": "Existing slide id to update. Omit to create a new slide."},
            "title": {"type": "string", "description": "Short slide title shown in the slide list."},
            "layout_id": {"type": "string", "description": "Master layout id this slide sits on."},
            "position": {"type": "integer", "description": "0-based position for a new slide. Omit to append."},
            "html": {"type": "string", "description": "Complete HTML document for the slide."},
            "summary": {"type": "string", "description": "One short line, shown in the slide's version history, "
                        "saying what this version is or what it changed, e.g. \"Three-column market overview\" "
                        "or \"Shortened the headline and added a source line\". About this slide only."},
            "source": {"type": "string", "description": "The slide's source/footnote line, e.g. "
                       "\"Source: company filings; team analysis\". Also write it into the HTML as visible "
                       "text: the HTML is what is exported, this field only records it on the version."},
        },
        "required": ["title", "layout_id", "html"],
    },
}
READ_SLIDE: JSON = {
    "name": "read_slide",
    "description": "Return the current HTML of a slide.",
    "input_schema": {"type": "object", "properties": {"slide_id": {"type": "string"}}, "required": ["slide_id"]},
}
FIND_LAYOUT_REFERENCE: JSON = {
    "name": "find_layout_reference",
    "description": "Search a library of 1,240 consulting-slide layout archetypes for structural recipes that fit "
                   "what a slide has to show: purpose, layout directive, the repeating units with typical and "
                   "tolerable counts, and density. Returns up to `limit` diverse matches as text. Recipes carry "
                   "no content or branding; adapt them to this master.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What the slide must show, in plain words, e.g. "
                      "\"revenue bridge 2023 to 2024\" or \"four-phase rollout with milestones\"."},
            "slide_type": {"type": "string", "description": "Optional slide type to search within: "
                           + ", ".join(archetypes.SLIDE_TYPE_CATEGORIES) + "."},
            "limit": {"type": "integer", "description": f"How many matches, 1-{archetypes.MAX_LIMIT} "
                      f"(default {archetypes.DEFAULT_LIMIT})."},
        },
        "required": ["query"],
    },
}
PREVIEW_SLIDE: JSON = {
    "name": "preview_slide",
    "description": "Render a slide's current version as a picture over its master layout, and return the picture "
                   "with the export lint, the design review and an independent reviewer's fix list (a separate "
                   "model that looks at the render fresh). Use it to check a slide you saved: overflow, "
                   "collisions, alignment, balance, contrast against the layout background.",
    "input_schema": {"type": "object", "properties": {"slide_id": {"type": "string"}}, "required": ["slide_id"]},
}
EDIT_SLIDE: JSON = {
    "name": "edit_slide",
    "eager_input_streaming": True,
    "description": "Change part of an existing slide without resending it: exact find/replace edits applied in "
                   "order to the slide's current HTML, saved as a new version (same version history, lint and "
                   "design review as save_slide). Each `find` must occur exactly once in the HTML as it stands "
                   "when that edit runs; include enough surrounding text to make it unique. If any edit fails, "
                   "nothing is saved. Use save_slide for a new slide or a rewrite.",
    "input_schema": {
        "type": "object",
        "properties": {
            "slide_id": {"type": "string", "description": "The slide to edit."},
            "edits": {"type": "array", "minItems": 1, "description": "Replacements, applied in order.",
                      "items": {"type": "object",
                                "properties": {
                                    "find": {"type": "string", "description": "Exact text of the current HTML, "
                                             "whitespace included, that occurs exactly once."},
                                    "replace": {"type": "string", "description": "What replaces it (may be empty)."},
                                },
                                "required": ["find", "replace"]}},
            "summary": {"type": "string", "description": "One short line for the version history, saying what "
                        "this edit changed, e.g. \"Aligned the three columns and shortened the title\"."},
            "source": {"type": "string", "description": "The slide's source/footnote line, when this edit adds or "
                       "changes it. Also edit it into the HTML: only the HTML is exported."},
        },
        "required": ["slide_id", "edits"],
    },
}
GET_COMPONENT: JSON = {
    "name": "get_component",
    "description": "Return a ready-made, export-safe HTML snippet for a consulting exhibit or structure (e.g. "
                   "Harvey balls, chevron process, KPI tiles, 2x2 matrix, football field, comps table, waterfall "
                   "callouts), with its parameters and rules. Paste it into the slide and adapt it. An unknown "
                   "name (or \"catalog\") returns the list of components.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The component's name, as the catalog lists it."},
            "params": {"type": "object", "description": "Optional parameters the component takes (see the "
                       "catalog), e.g. {\"columns\": 4}."},
        },
        "required": ["name"],
    },
}

GET_EXEMPLARS: JSON = {
    "name": "get_exemplars",
    "description": "Show 1-3 pictures of well-designed slides from real consulting decks whose layout fits what a "
                   "slide has to show, each with its type, framework, a one-line description of its "
                   "structure and, for composite slides, the units it combines (e.g. option columns + "
                   "pros/cons rows + verdict row). Layout references only: take the structure, never their "
                   "text, names, logos, colours or data.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What the slide must show, in plain words, e.g. "
                      "\"four-phase rollout with milestones\"."},
            "slide_type": {"type": "string", "description": "Optional slide type, e.g. process, table, org."},
            "framework": {"type": "string", "description": "Optional framework name from the library, e.g. "
                          "\"hub-and-spoke\" or \"gantt chart\"."},
            "limit": {"type": "integer", "description": f"How many pictures, 1-{exemplars.MAX_LIMIT} "
                      f"(default {exemplars.DEFAULT_LIMIT})."},
        },
    },
}

PLAN_EXHIBIT: JSON = {
    "name": "plan_exhibit",
    "description": "Plan a body slide's exhibit before writing HTML: from the action title (and optionally the "
                   "slide type, framework and key figures) it classifies the comparison the message makes "
                   "(ranking, deviation, time, part-to-whole, bridge, distribution, relationship, sequence, "
                   "evaluation, structure, spatial, valuation, market sizing, KPI status, single statement) and "
                   "returns the main exhibit (a component with a params skeleton, or a data-chart kind), ONE "
                   "insight device, ONE supporting device, the takeaway form, direction and scale rules, and "
                   "the matching framework and finance recipes. Deterministic and free.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "The slide's action title (with its number)."},
            "slide_type": {"type": "string", "description": "Optional slide type from the prompt's list."},
            "framework": {"type": "string", "description": "Optional framework name from the library."},
            "facts": {"type": "array", "items": {"type": "string"}, "description": "Optional: the key figures "
                      "or phrases from the brief, e.g. [\"34% vs 61% peer\", \"78% vs 71% opex ratio\"]."},
            "lower_is_better": {"type": "array", "items": {"type": "string"}, "description": "Optional: the "
                                "metrics where a lower value is the better result (cost, ratio, time)."},
        },
        "required": ["title"],
    },
}


def write_brief_tool() -> JSON:
    """The `write_brief` tool: its description lists the components and chart kinds of this build."""
    return {"name": "write_brief", "description": slide_brief.tool_description(),
            "input_schema": slide_brief.BRIEF_SCHEMA}


#: The design chat's own tools, in the order they are offered. `get_component` is offered only when
#: its library imports, `get_exemplars` only when exemplar pictures are installed, and `write_brief`
#: (just before `plan_exhibit`) only when the brief is on (`offered_tools`).
DESIGN_TOOLS: list[JSON] = [SAVE_SLIDE, READ_SLIDE, PLAN_EXHIBIT, FIND_LAYOUT_REFERENCE, PREVIEW_SLIDE,
                            EDIT_SLIDE, GET_COMPONENT, GET_EXEMPLARS]


def load_components() -> ModuleType | None:
    """`design_refs.components`, imported when it is used (None while it cannot be)."""
    try:
        from app.core.design_refs import components
    except Exception:  # noqa: BLE001 - missing or broken: the tool is simply not offered
        _log.warning("component library could not be imported; the component tool is not offered", exc_info=True)
        return None
    ok = callable(getattr(components, "get", None)) and callable(getattr(components, "catalog", None))
    return components if ok else None


def offered_tools(brief_on: bool) -> list[JSON]:
    """`DESIGN_TOOLS`, less `get_component` when the component library does not import and less
    `get_exemplars` when no exemplar pictures are installed; plus `write_brief` when the brief is on."""
    tools = [t for t in DESIGN_TOOLS
             if (t is not GET_COMPONENT or load_components() is not None)
             and (t is not GET_EXEMPLARS or exemplars.available())]
    if brief_on:
        tools.insert(tools.index(PLAN_EXHIBIT), write_brief_tool())
    return tools
