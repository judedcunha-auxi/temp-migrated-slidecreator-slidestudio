"""Text → layout placeholder mapping.

`map_placeholders(ir, layout) -> IR` decides which text elements belong *in* the master's own
placeholders rather than in free text boxes on top of them. That matters beyond tidiness: text in a
real placeholder shows up in Outline view, inherits the master's type styling, survives a re-theme,
and is what a consultant expects to find when they click the title.

The rule is the authoring contract's, in two passes:

1. **An explicit `data-placeholder` wins.** The extractor records the author's intent (see
   "What the extractor must pass" below); this module only resolves it to a real placeholder and
   fills in the `idx`.
2. **Otherwise, geometry decides**: a text box whose area lies ≥ 60 % inside a placeholder zone of a
   compatible type (`title ↔ title/ctrTitle`, `subtitle ↔ subTitle`, `body ↔ body/obj`) maps to it.
   Claims are resolved best-overlap-first across the whole slide, so two candidates for one title
   cannot depend on the order the extractor happened to walk the DOM in.

One placeholder holds at most one element — the runner-up stays a text box, which is honest: the
layout has one title and the design drew two things over it.

**What the extractor must pass (WP1).** Either `element.placeholder = {"type": "title"}` (the raw
`data-placeholder` value, `idx` unresolved) or `element.extras["x-wp4-placeholder"] = "title"`.
Both are read here; nothing else is. `idx` is always (re)resolved from the layout, because only the
manifest knows it. Since R3 (render check, 2026-09-29) page.js records the second form on every
text element inside a `data-placeholder` element, with `x-wp4-placeholder-host` naming that element
— before, it recorded nothing, and pass 1 never fired.
"""
from __future__ import annotations

from typing import Any

from app.engine.ir import IR, Element
from app.engine.manifest import PLACEHOLDER_ALIASES, Layout, Placeholder

#: Placeholder types a design's text may be written into. `pic`/`chart`/`tbl` are content frames for
#: objects, and `dt`/`ftr`/`sldNum` belong to the layout, not to the slide.
MAPPABLE_TYPES: tuple[str, ...] = ("title", "ctrTitle", "subTitle", "body", "obj")

#: Share of the text box that must lie inside the zone (authoring contract §Placeholders).
MIN_OVERLAP = 0.60

#: The extras key WP1 may use instead of a half-filled `placeholder` dict.
EXTRAS_KEY = "x-wp4-placeholder"  # gitleaks:allow (an IR extras key name, not a secret)

#: The CSS path of the element that carries the `data-placeholder` (page.js `push`, R3): texts with
#: the same host are one request. An element without it is a request of its own.
HOST_KEY = "x-wp4-placeholder-host"


def map_placeholders(ir: IR, layout: Layout | None) -> IR:
    """Return the IR with `placeholder` set on the text elements that map to the layout's own."""
    if layout is None:
        return ir
    zones = [p for p in layout.placeholders if p.type in MAPPABLE_TYPES]
    if not zones:
        return ir

    texts = [(index, element) for index, element in enumerate(ir.elements) if element.kind == "text"]
    if not texts:
        return ir

    assignment: dict[int, Placeholder] = {}
    claimed: set[int] = set()

    # -- pass 1: the author said so ---------------------------------------------------------------
    # One `data-placeholder` element can hold several text elements (its words and an inline-block
    # chip beside them; page.js records the host on each). That is one request: the most prominent
    # text of the host takes the placeholder — largest type, then area, then order, as in pass 2 —
    # and the rest stay text boxes without a warning, since nothing the author asked for was refused.
    requests: dict[tuple[str, str], list[tuple[int, Element]]] = {}
    for index, element in texts:
        wanted = _explicit_type(element)
        if wanted:
            host = str((element.extras or {}).get(HOST_KEY) or f"#{index}")
            requests.setdefault((wanted, host), []).append((index, element))
    for (wanted, _), members in requests.items():
        index, element = min(members, key=lambda item: (-_type_size(item[1]), -item[1].box.area, item[0]))
        allowed = PLACEHOLDER_ALIASES.get(wanted, (wanted,))
        candidates = [(zi, zone) for zi, zone in enumerate(zones)
                      if zone.type in allowed and zi not in claimed]
        if not candidates:
            ir.add_diagnostic(
                "warn", _source(element),
                f"data-placeholder={wanted!r} but {layout.id} has no free {wanted} placeholder",
                element.id,
            )
            continue
        zi, zone = max(candidates, key=lambda item: (element.box.overlap_fraction(item[1].box), -item[0]))
        claimed.add(zi)
        assignment[index] = zone

    # -- pass 2: geometry ------------------------------------------------------------------------
    # A candidate covers at least MIN_OVERLAP of its own box with the zone; among candidates the one
    # set in the largest type wins, then the one sharing the most area with the zone. Ranking by the
    # *fraction* let a chip that sits wholly inside the title zone (100 %) take the title from the
    # heading beside it (~95 %) — since WP-B a chip is its own text element (plan §7); ranking by
    # area alone let a long standfirst 75 % inside the zone take it from a short title wholly inside
    # (B1 critique #7). The zone's text is the prominent one: a title beats its chip and its standfirst.
    scored: list[tuple[float, float, int, int]] = []
    for index, element in texts:
        if index in assignment:
            continue
        for zi, zone in enumerate(zones):
            if element.box.overlap_fraction(zone.box) >= MIN_OVERLAP:
                scored.append((_type_size(element), element.box.intersect(zone.box).area, index, zi))
    # Largest type, then shared area, then element order — deterministic whatever the DOM walk did.
    for _, _, index, zi in sorted(scored, key=lambda item: (-item[0], -item[1], item[2], item[3])):
        if index in assignment or zi in claimed:
            continue
        claimed.add(zi)
        assignment[index] = zones[zi]

    if not assignment:
        return ir

    elements = list(ir.elements)
    for index, zone in assignment.items():
        element = elements[index]
        if zone.idx is None:
            same_type = [z for z in zones if z.type == zone.type]
            if len(same_type) > 1:
                ir.add_diagnostic(
                    "error", _source(element),
                    f"{layout.id} has {len(same_type)} {zone.type} placeholders and no idx (legacy "
                    f"manifest) — cannot say which one; emitted as a text box",
                    element.id,
                )
                continue
        elements[index] = element.replace(placeholder={"type": zone.type, "idx": zone.idx})

    return IR(
        canvas=ir.canvas,
        slide=ir.slide,
        elements=elements,
        groups=ir.groups,
        diagnostics=ir.diagnostics,
        fonts=ir.fonts,
        version=ir.version,
    )


def _type_size(element: Element) -> float:
    """The largest font size (px) on the element's first line: how prominent its text is."""
    for paragraph in element.paragraphs or []:
        for line in paragraph.get("lines") or []:
            sizes = [float(run.get("sizePx") or 0.0) for run in line.get("runs") or [] if run.get("text")]
            if sizes:
                return max(sizes)
    return 0.0


def _explicit_type(element: Element) -> str | None:
    """The author's `data-placeholder`, wherever the extractor put it."""
    for candidate in (
        (element.placeholder or {}).get("type") if isinstance(element.placeholder, dict) else None,
        (element.extras or {}).get(EXTRAS_KEY),
        (element.source or {}).get("placeholder"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _source(element: Element) -> str:
    source: dict[str, Any] = element.source or {}
    return str(source.get("path") or source.get("svg") or element.name or element.id or "text")
