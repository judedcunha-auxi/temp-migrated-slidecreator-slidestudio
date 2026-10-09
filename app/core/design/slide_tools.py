"""The tools that write a slide (`save_slide`, `edit_slide`) and `get_component`, which hands one a snippet.

Every write goes into the deck directory inside the job workspace (`DesignDeck.save_slide`); nothing
is written anywhere else. Ported from Slide Studio `server/chat/slide_tools.py` (migration plan §4.1, K).
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.design.briefs import brief_nudge, grounding_text, record_brief, take_brief, visible_text
from app.core.design.context import DesignContext
from app.core.design.slide_lint import design_text, lint_text
from app.core.design.tool_schemas import load_components
from app.core.design.turn_state import Turn
from app.core.design_refs.notes import design_tokens

_log = logging.getLogger(__name__)

JSON = dict[str, Any]
ToolAnswer = tuple[JSON, JSON | None]

#: How many edits one `edit_slide` call may carry: a patch, not a rewrite by instalments.
MAX_EDITS = 40
#: The largest slide document a save may write (a slide is a page of HTML, not a data dump).
MAX_HTML_CHARS = 400_000


def save_version(ctx: DesignContext, turn: Turn, tool_use_id: str, html: str, title: str, layout_id: str | None,
                 sid: str | None, position: int | None, summary: Any, source: Any) -> ToolAnswer:
    """The one path a slide version is written by, for `save_slide` and `edit_slide` alike: the
    version, the source line, the export lint and the design review in the result."""
    matched = take_brief(ctx, turn, sid, title, layout_id, html)
    nudge = None if matched else brief_nudge(ctx, turn, sid, title, layout_id)
    s = ctx.deck.save_slide(html, title, layout_id, turn.instruction, sid=sid, position=position, source="chat",
                            summary=summary if isinstance(summary, str) else None)
    turn.touch(s["id"])
    said = [f"Saved {s['id']} version {s['current']}."]
    if matched is not None:
        brief, findings = matched
        turn.briefs[s["id"]] = brief
        try:
            record_brief(ctx, s["id"], int(s["current"]), brief, findings)
        except Exception:  # noqa: BLE001 - recording the brief never costs the save
            _log.debug("could not record the slide brief", exc_info=True)
    if nudge:
        said.append(nudge)
    if isinstance(source, str) and source.strip():
        line = " ".join(source.split())[:300]
        record_source_line(ctx, s["id"], int(s["current"]), line)
        if line.lower() not in visible_text(html):
            said.append("Note: that source line is not visible text in the HTML. Add it to the slide; only the "
                        "HTML is exported.")
    said.append(lint_text(ctx, s["id"]))
    grounding = grounding_text(ctx, turn, s["id"], html)
    if grounding:
        said.append(grounding)
    design = design_text(ctx, s["id"])
    if design:
        said.append(design)
    return ({"type": "tool_result", "tool_use_id": tool_use_id, "content": "\n".join(said)},
            {"type": "slide", "slideId": s["id"], "version": s["current"]})


def _check_html(html: Any) -> str:
    if not isinstance(html, str) or "<" not in html:
        raise ValueError("html must be a complete HTML document")
    if len(html) > MAX_HTML_CHARS:
        raise ValueError(f"the slide is {len(html)} characters; a slide must stay under {MAX_HTML_CHARS}")
    return html


def save_slide(ctx: DesignContext, turn: Turn, tool_use_id: str, inp: JSON) -> ToolAnswer:
    html = _check_html(inp.get("html"))
    layout_ids = {lay["id"] for lay in (ctx.deck.master().get("layouts") or []) if lay.get("id")}
    layout_id = inp.get("layout_id")
    if layout_ids and layout_id not in layout_ids:
        raise ValueError(f"unknown layout_id {layout_id!r}; use one of {sorted(layout_ids)}")
    sid = inp.get("slide_id") or None
    if sid is not None and not isinstance(sid, str):
        raise ValueError("slide_id must be a string")
    position = inp.get("position") if isinstance(inp.get("position"), int) else None
    return save_version(ctx, turn, tool_use_id, html, str(inp.get("title") or "Untitled"),
                        layout_id if isinstance(layout_id, str) else None, sid, position,
                        inp.get("summary"), inp.get("source"))


def apply_edits(html: str, edits: Any) -> str:
    """`edits` applied in order to `html`, each `find` required to occur exactly once in the text as
    it stands when that edit runs. Raises ValueError naming the edit that failed; nothing is saved."""
    if not isinstance(edits, list) or not edits:
        raise ValueError("edits must be a non-empty list of {find, replace}")
    if len(edits) > MAX_EDITS:
        raise ValueError(f"{len(edits)} edits is too many for one edit_slide (at most {MAX_EDITS}); "
                         "use save_slide to rewrite the slide")
    out = html
    total = len(edits)
    for n, edit in enumerate(edits, 1):
        find = edit.get("find") if isinstance(edit, dict) else None
        replace = edit.get("replace") if isinstance(edit, dict) else None
        if not isinstance(find, str) or not find:
            raise ValueError(f"edit {n} of {total}: `find` must be a non-empty string; nothing was saved")
        if not isinstance(replace, str):
            raise ValueError(f"edit {n} of {total}: `replace` must be a string; nothing was saved")
        count = out.count(find)
        if count != 1:
            snippet = " ".join(find.split())
            snippet = snippet if len(snippet) <= 80 else snippet[:77] + "…"
            why = ("does not occur" if count == 0
                   else f"occurs {count} times (it must be unique; include more surrounding text)")
            earlier = " after the edits before it" if n > 1 else ""
            raise ValueError(f"edit {n} of {total}: `find` {why} in the slide's current HTML{earlier} "
                             f"(\"{snippet}\"); nothing was saved. Copy the text exactly, or read_slide first.")
        out = out.replace(find, replace, 1)
    return out


def edit_slide(ctx: DesignContext, turn: Turn, tool_use_id: str, inp: JSON) -> ToolAnswer:
    sid = inp.get("slide_id")
    if not isinstance(sid, str) or not sid:
        raise ValueError("slide_id is required")
    slide = ctx.deck.find_slide(ctx.deck.meta(), sid)  # an unknown id is a KeyError -> "Error: ..."
    before = ctx.deck.read_slide(sid)
    after = _check_html(apply_edits(before, inp.get("edits")))
    if after == before:
        raise ValueError("the edits leave the slide unchanged; nothing was saved")
    return save_version(ctx, turn, tool_use_id, after, str(slide.get("title") or "Untitled"), slide.get("layoutId"),
                        sid, None, inp.get("summary"), inp.get("source"))


def record_source_line(ctx: DesignContext, sid: str, version: int, line: str) -> None:
    """Keep the slide's source line on the version it was saved with (additive; the HTML is the truth)."""
    def mark(slide: JSON) -> None:
        for v in slide.get("versions") or []:
            if v.get("n") == version:
                v["sourceLine"] = line
    try:
        ctx.deck.update_slide(sid, mark)
    except Exception:  # noqa: BLE001 - recording the line never costs the save
        _log.debug("could not record the source line", exc_info=True)


def component_result(tool_use_id: str, inp: JSON, master: JSON | None = None) -> JSON:
    """`get_component`'s tool_result. The deck's tokens are passed in unless the model passed its own,
    so a chart's hex colours follow the master without being copied."""
    module = load_components()
    if module is None:
        return {"type": "tool_result", "tool_use_id": tool_use_id, "is_error": True,
                "content": "Error: the component library is not available; write the HTML yourself."}
    name = inp.get("name")
    raw = inp.get("params")
    params: JSON = dict(raw) if isinstance(raw, dict) else {}
    if not isinstance(name, str) or not name.strip() or name.strip().lower() in ("catalog", "list"):
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": str(module.catalog())}
    if "tokens" not in params and master and master.get("layouts"):
        try:
            params = {**params, "tokens": design_tokens(master)}
        except Exception:  # noqa: BLE001 - the component falls back to its own defaults
            _log.debug("could not read design tokens for a component", exc_info=True)
    try:
        text = module.get(name.strip(), **params)
    except KeyError:
        return {"type": "tool_result", "tool_use_id": tool_use_id, "is_error": True,
                "content": f"Error: no component named {name!r}. The catalog:\n{module.catalog()}"}
    except Exception as exc:  # noqa: BLE001 - bad params: an answer, not a crash
        _log.debug("component could not be built", exc_info=True)
        reason = " ".join(str(exc).split())[:200] or type(exc).__name__
        return {"type": "tool_result", "tool_use_id": tool_use_id, "is_error": True,
                "content": f"Error: component {name!r} could not be built with those params ({reason})."}
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": str(text)}
