"""`preview_slide`: the slide rendered over its layout, its findings, and the independent critic's review.

This is the critic loop: the designer saves, previews, gets a fix list from a separate model that
sees only the render (and the measured findings), applies the [must] fixes with `edit_slide`, and
previews again, until PASS or the review limit. The critic's cost goes on `turn.extra_usage` and is
folded into the turn's total.

Ported from Slide Studio `server/chat/preview.py` (migration plan §4.1, K/R): the render and the
critic come from the context (`DesignContext.svc.render`, the provider resolver).
"""

from __future__ import annotations

import base64
import io
import logging
import re
from typing import Any

from PIL import Image

from app.core.design import critic
from app.core.design.briefs import grounding_text
from app.core.design.context import DesignContext
from app.core.design.layouts import canvas, layout_label, zone_line
from app.core.design.slide_lint import design_text, lint_text
from app.core.design.turn_state import Turn
from app.core.design_refs import slide_brief

_log = logging.getLogger(__name__)

JSON = dict[str, Any]

#: The widest a preview sent back to the model may be.
PREVIEW_MAX_W = 1280
#: Critic reviews per slide per turn. The third runs only when the second still failed composition
#: or canvas; past that the preview comes back without a review and tells the designer to stop.
MAX_REVIEWS = 2
MAX_REVIEWS_LAYOUT = 3


def preview_image(png: bytes) -> tuple[str, str, tuple[int, int]]:
    """A rendered slide as a base64 JPEG no wider than `PREVIEW_MAX_W`: (media type, data, size)."""
    with Image.open(io.BytesIO(png)) as im:
        im.load()
        if im.mode in ("RGBA", "LA", "P", "PA"):
            rgba = im.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.split()[-1])
        else:
            flat = im.convert("RGB")
    if flat.width > PREVIEW_MAX_W:
        flat = flat.resize((PREVIEW_MAX_W, max(1, round(flat.height * PREVIEW_MAX_W / flat.width))),
                           Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    flat.save(buf, "JPEG", quality=85, optimize=True)
    return "image/jpeg", base64.b64encode(buf.getvalue()).decode(), flat.size


def critic_context(ctx: DesignContext, meta: JSON, slide: JSON, w: int, h: int) -> JSON:
    """What the critic is told besides the picture: id, version, title, canvas, the layout and its
    zones, the declared exhibit plan, and the brief's checks."""
    cw, ch = canvas(meta)
    context: JSON = {"slide_id": slide.get("id"), "version": slide.get("current"), "title": slide.get("title"),
                     "canvas": f"{cw}×{ch}" + (f" (the picture is {w}×{h})" if (w, h) != (cw, ch) else "")}
    layout_id = slide.get("layoutId")
    layout = next((lay for lay in ((meta.get("master") or {}).get("layouts") or []) if lay.get("id") == layout_id),
                  None)
    if layout:
        context["layout"] = layout_label(meta, layout_id) or layout_id
        zones = "; ".join(zone_line(z) for z in layout.get("placeholders") or [])
        if zones:
            context["zones"] = zones
    try:
        found = re.search(r'data-exhibit-plan\s*=\s*"([^"]{1,300})"', ctx.deck.read_slide(str(slide.get("id"))) or "")
        if found:
            context["exhibit_plan"] = found.group(1)
    except Exception:  # noqa: BLE001 - a missing file only means no plan to check
        _log.debug("could not read the exhibit plan for the critic", exc_info=True)
    record = slide.get("brief") if ctx.brief_on else None
    if isinstance(record, dict) and isinstance(record.get("brief"), dict):
        try:
            context["brief_checks"] = slide_brief.critic_items(slide_brief.Brief.from_dict(record["brief"]))
        except Exception:  # noqa: BLE001 - a brief that no longer parses only means no brief checks
            _log.debug("saved brief no longer parses; no brief checks for the critic", exc_info=True)
    return context


def preview_result(ctx: DesignContext, tool_use_id: str, sid: str, turn: Turn | None = None) -> JSON:
    """`preview_slide`'s tool_result: the picture, then the findings, then the critic's review."""
    meta = ctx.deck.meta()
    slide = ctx.deck.find_slide(meta, sid)  # an unknown id is a KeyError -> the dispatcher's "Error: ..."
    lint = lint_text(ctx, sid)
    design = design_text(ctx, sid)
    findings = lint + (f"\n{design}" if design else "")
    try:
        grounding = grounding_text(ctx, turn, sid, ctx.deck.read_slide(sid)) if turn is not None else None
    except Exception:  # noqa: BLE001 - the check is advice; the preview stands without it
        _log.debug("grounding check failed during preview", exc_info=True)
        grounding = None
    if grounding:
        findings += f"\n{grounding}"
    render = ctx.svc.render
    try:
        if render is None:
            raise RuntimeError("no preview renderer is configured")
        media, data, (w, h) = preview_image(render(ctx.deck, sid))
    except Exception as exc:  # noqa: BLE001 - no browser, a render that failed, bad bytes: one answer
        _log.warning("slide preview unavailable", exc_info=True)
        reason = " ".join(str(exc).split())[:200] or type(exc).__name__
        return {"type": "tool_result", "tool_use_id": tool_use_id, "is_error": True,
                "content": f"Preview unavailable ({reason}). The slide itself is saved.\n{findings}"}
    blocks: list[JSON] = [
        {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}},
        {"type": "text", "text": f"Preview of {sid} version {slide.get('current')} ({w}×{h} px, drawn over its "
                                 f"layout).\n{findings}"},
    ]
    review = critic_review(ctx, meta, slide, turn, media, data, (w, h), lint, design)
    if review:
        blocks.append({"type": "text", "text": review})
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": blocks}


def critic_review(ctx: DesignContext, meta: JSON, slide: JSON, turn: Turn | None, media: str, data: str,
                  size: tuple[int, int], lint: str, design: str | None) -> str | None:
    """Ask the critic about this render; record its cost and the render for a later comparison."""
    if not ctx.settings.design_critic_enabled:
        return None
    sid = str(slide["id"])
    earlier: JSON = (turn.previews.get(sid) if turn else None) or {}
    if earlier.get("review") and earlier.get("version") == slide.get("current"):
        return str(earlier["review"])  # the same version again: the same review, not a second bill
    reviews = int(earlier.get("reviews") or 0)
    if reviews >= MAX_REVIEWS and not (reviews < MAX_REVIEWS_LAYOUT and layout_must(earlier.get("critique"))):
        return (f"Review limit reached for this slide in this turn ({reviews} reviews): no further review. Stop "
                f"editing it; in your reply, name what the last review left open.")
    context = critic_context(ctx, meta, slide, *size)
    context["media_type"] = media
    if earlier.get("image") and earlier.get("version") != slide.get("current"):
        context["previous_image"] = earlier["image"]
        context["previous_critique"] = earlier.get("critique") or ""
    if ctx.stopping():
        return None  # a job being stopped starts no critic bill
    model = ctx.settings.llm_critic_model
    result = critic.critique(ctx.provider_or_none(model), model, data, lint,
                             design or "Design review: not available.", context, ctx.settings)
    if result is None:
        return None
    if turn is not None:
        if result.usage:
            turn.extra_usage.append(dict(result.usage))
        turn.reviews.append((sid, result.passed))
        turn.previews[sid] = {"image": data, "version": slide.get("current"), "critique": result.text,
                              "review": result.for_designer(), "reviews": reviews + 1}
    return result.for_designer()


def layout_must(critique: Any) -> bool:
    """Did a review fail the composition or canvas item? Those earn the one extra cycle."""
    numbers = f"{critic.COMPOSITION_ITEM}|{critic.CANVAS_ITEM}"
    return bool(isinstance(critique, str) and re.search(rf"^\s*({numbers})\.\s*NO\b", critique, re.M | re.I))
