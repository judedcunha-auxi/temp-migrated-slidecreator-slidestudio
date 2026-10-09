"""`write_brief`: the per-slide design brief, matching a brief to the slide it plans, and the grounding check.

A brief plans one body slide before its HTML: the action title, the facts (every figure the slide
may show, each with where it came from), the zones and their exhibits. It is validated against the
person's own material (`turn.sources`): errors come back for ONE repair, then the brief is accepted
with its findings shown. After the slide is saved, the grounding check lists any figure on it that
is neither a brief fact nor in the material.

Ported from Slide Studio `server/chat/briefs.py` (migration plan §4.1, K). PDF attachments are read
with `pypdf` (now a pinned dependency) so their figures can ground a brief.
"""

from __future__ import annotations

import html as html_lib
import logging
import re
import zipfile
from pathlib import Path
from typing import Any

from app.core.design.context import DesignContext
from app.core.design.slide_lint import MAX_FINDINGS
from app.core.design.turn_state import Turn
from app.core.design_refs import exemplars, exhibit_plan, slide_brief

_log = logging.getLogger(__name__)

JSON = dict[str, Any]

#: Words in a layout's name or usage that mark a slide with no exhibit (no brief expected).
NO_BRIEF_LAYOUT = re.compile(r"\b(cover|title slide|divider|section|agenda|contents|closing|thank you|quote)\b",
                             re.I)
#: Words too common to tie a brief's title to a slide.
STOP_WORDS = {"with", "from", "that", "this", "than", "into", "their", "they", "have", "over", "across", "while",
              "which", "would", "will", "each", "every", "only", "more", "most", "less"}
#: The most text one attachment adds to a brief's sources.
MAX_SOURCE_CHARS = 200_000
TEXT_TYPES = frozenset({".txt", ".md", ".csv", ".tsv", ".json", ".html", ".htm", ".xml", ".yaml", ".yml"})


def write_brief(turn: Turn, tool_use_id: str, inp: JSON) -> JSON:
    """`write_brief`'s tool_result. The accepted brief waits on the turn for the next saved slide."""
    brief = slide_brief.Brief.from_dict(inp)
    if brief.skip:
        turn.brief_declined = True
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": f"No brief: {brief.skip}"}
    findings = slide_brief.validate(brief, turn.sources)
    errors = slide_brief.errors(findings)
    if errors and not turn.brief_rejected:
        turn.brief_rejected = True
        lines = [f.line() for f in errors[:MAX_FINDINGS]]
        if len(errors) > MAX_FINDINGS:
            lines.append(f"… and {len(errors) - MAX_FINDINGS} more")
        return {"type": "tool_result", "tool_use_id": tool_use_id, "is_error": True,
                "content": "The brief failed validation. Fix every error and call write_brief again with the "
                           "whole brief:\n" + "\n".join(lines)}
    turn.brief_rejected = False
    key = norm(brief.slide)
    turn.pending_briefs = [b for b in turn.pending_briefs if not key or norm(b[0].slide) != key]
    turn.pending_briefs.append((brief, findings))
    text = slide_brief.render_for_designer(brief, findings)
    if errors:
        text += "\nThe brief still has errors after its repair: design from it, fixing what the validator lists."
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": text + brief_plan(brief)}


def brief_plan(brief: slide_brief.Brief) -> str:
    """What `plan_exhibit` adds to an accepted brief: the classifier's reading of the title, each
    component's params skeleton, and the declaration line the reviewer looks for."""
    title = slide_brief.render_text(brief.action_title, brief)
    lines: list[str] = []
    kind = ""
    try:
        kind, why = exhibit_plan.classify(title)
        lines.append(f"- Exhibit plan: the title reads as '{kind}' ({why}).")
    except Exception:  # noqa: BLE001 - the classifier is a second opinion
        _log.debug("exhibit-plan classifier failed", exc_info=True)
    for z in brief.zones:
        ex = z.exhibit
        if ex and ex.kind == "component" and ex.name:
            shape = exhibit_plan.skeleton(ex.name)
            if shape:
                lines.append(f"- get_component params skeleton for {ex.name}: {shape}")
    hero = next((z for z in brief.zones if z.tier == "hero"), None)
    insight = (hero.exhibit.insight if hero and hero.exhibit else "") or next(
        (z.exhibit.insight for z in brief.zones if z.exhibit and z.exhibit.insight), "")
    declared = (brief.comparison or kind or "statement") + (f"; insight: {insight}" if insight else "")
    declared = " ".join(declared.replace('"', "'").split())[:200]
    lines.append(f'- Declare it: put data-exhibit-plan="{declared}" on the body wrapper.')
    if exemplars.available():
        units = ", ".join(dict.fromkeys((z.exhibit.name or z.exhibit.kind).replace("_", " ")
                                        for z in brief.zones if z.exhibit))
        lines.append(f"- Before the HTML, call get_exemplars once (query: \"{units}\") and borrow the composition "
                     f"of a dense real slide that combines these units.")
    if brief.slide:
        lines.append(f"- Save this slide as \"{brief.slide}\" so the brief stays tied to it.")
    return "\n" + "\n".join(lines)


def norm(text: str | None) -> str:
    return " ".join(re.sub(r"[^\w%$.]+", " ", str(text or "").lower()).split())


def no_brief_kind(ctx: DesignContext, title: str | None, layout_id: str | None) -> str | None:
    """'cover', 'agenda', ... when the slide's title or layout marks it as one with no exhibit."""
    layout = next((lay for lay in (ctx.deck.master().get("layouts") or []) if lay.get("id") == layout_id), None) or {}
    found = (NO_BRIEF_LAYOUT.search(str(title or ""))
             or NO_BRIEF_LAYOUT.search(f"{layout.get('name') or ''} {layout.get('usage') or ''}"))
    kind = found.group(1).lower() if found else None
    return "cover" if kind == "title slide" else kind


def take_brief(ctx: DesignContext, turn: Turn, sid: str | None, title: str, layout_id: str | None,
               html: str) -> tuple[slide_brief.Brief, list[Any]] | None:
    """The waiting brief this save belongs to, removed from the queue (see Slide Studio's rules: by
    the brief's `slide` name, else by 60%+ of its title's words, else the only one for a new body
    slide). None when none fits."""
    if not turn.pending_briefs:
        return None
    pick: int | None = None
    keys = {norm(title), norm(sid)} - {""}
    for i, (brief, _) in enumerate(turn.pending_briefs):
        if brief.slide and norm(brief.slide) in keys:
            pick = i
            break
    if pick is None:
        text = visible_text(html)
        best = 0.0
        for i, (brief, _) in enumerate(turn.pending_briefs):
            words = [w for w in norm(slide_brief.render_text(brief.action_title, brief)).split()
                     if len(w) >= 4 and w not in STOP_WORDS]
            score = sum(w in text for w in words) / len(words) if words else 0.0
            if score >= 0.6 and score > best:
                pick, best = i, score
    if pick is None and len(turn.pending_briefs) == 1 and not sid and not no_brief_kind(ctx, title, layout_id):
        pick = 0
    return turn.pending_briefs.pop(pick) if pick is not None else None


def brief_nudge(ctx: DesignContext, turn: Turn, sid: str | None, title: str | None, layout_id: str | None) -> str | None:
    """One line, once a turn, when a NEW body slide is saved with no brief although the turn earns one."""
    if sid or not ctx.brief_on or turn.nudged or turn.pending_briefs or turn.brief_declined:
        return None
    needed, _ = slide_brief.should_brief(turn.instruction, slide_type=no_brief_kind(ctx, title, layout_id),
                                         selected=turn.selected)
    if not needed:
        return None
    turn.nudged = True
    return ("Note: this new body slide was designed without a brief. Before the next one with figures or an "
            "exhibit, call write_brief.")


def record_brief(ctx: DesignContext, sid: str, version: int, brief: slide_brief.Brief, findings: list[Any]) -> None:
    """Keep the accepted brief on the slide (for later edits and the critic)."""
    record = {"version": version, "brief": brief.to_dict(),
              "findings": [f.line() for f in findings if f.severity != "info"][:MAX_FINDINGS]}
    ctx.deck.update_slide(sid, lambda slide: slide.__setitem__("brief", record))


def grounding_text(ctx: DesignContext, turn: Turn, sid: str, html: str) -> str | None:
    """The post-save grounding check for a slide with a brief: figures in its visible text or native
    charts that are neither brief facts nor in the person's material. None when there is nothing to
    check; a line saying it is clean when it passes."""
    if not ctx.brief_on:
        return None
    slide = next((s for s in ctx.deck.slides() if s.get("id") == sid), None) or {}
    record = slide.get("brief") if isinstance(slide.get("brief"), dict) else None
    brief = turn.briefs.get(sid)
    if brief is None and record and isinstance(record.get("brief"), dict):
        brief = slide_brief.Brief.from_dict(record["brief"])
    if brief is None:
        return None
    found = slide_brief.ungrounded_in_slide(visible_text(html), brief, turn.sources, slide_brief.chart_figures(html))
    if record is not None:
        version = slide.get("current")

        def mark(s: JSON) -> None:
            if isinstance(s.get("brief"), dict):
                s["brief"].update(ungrounded=found, checkedVersion=version)
        try:
            ctx.deck.update_slide(sid, mark)
        except Exception:  # noqa: BLE001 - recording never costs the save
            _log.debug("could not record the grounding check", exc_info=True)
    if not found:
        return "Grounding check: clean (every figure is a brief fact or in the user's material)."
    shown = ", ".join(found[:MAX_FINDINGS]) + (f" and {len(found) - MAX_FINDINGS} more" if len(found) > MAX_FINDINGS
                                               else "")
    return (f"Grounding check found figures that are neither brief facts nor in the user's material: {shown}. "
            f"Fix each before you reply: use the brief's fact, or add it to the brief (call write_brief again, "
            f"as an estimate or a derived fact with its formula and inputs).")


def attachment_text(path: Path) -> str:
    """An attachment's text, for grounding a brief's figures: text files as they are, PDFs through
    `pypdf`, Office files by their XML text. Anything else, or anything that fails, is ''."""
    ext = path.suffix.lower()
    try:
        if ext in TEXT_TYPES:
            return path.read_text(encoding="utf-8", errors="replace")[:MAX_SOURCE_CHARS]
        if ext == ".pdf":
            from pypdf import PdfReader

            out: list[str] = []
            for page in PdfReader(str(path)).pages:
                out.append(page.extract_text() or "")
                if sum(map(len, out)) > MAX_SOURCE_CHARS:
                    break
            return "\n".join(out)[:MAX_SOURCE_CHARS]
        if ext in (".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm"):
            parts: list[str] = []
            with zipfile.ZipFile(path) as z:
                for name in z.namelist():
                    if (name.endswith(".xml") and "_rels/" not in name and "theme" not in name
                            and not name.startswith(("[Content_Types]", "docProps/"))):
                        xml = z.read(name).decode("utf-8", errors="replace")
                        parts.append(html_lib.unescape(re.sub(r"<[^>]+>", " ", xml)))
                    if sum(map(len, parts)) > MAX_SOURCE_CHARS:
                        break
            return " ".join(" ".join(parts).split())[:MAX_SOURCE_CHARS]
    except Exception:  # noqa: BLE001 - an unreadable file only means fewer grounded figures
        _log.debug("could not read an attachment for grounding", exc_info=True)
    return ""


def brief_sources(ctx: DesignContext, text: str, files: list[Path]) -> list[str]:
    """Where a brief's figures may come from: this message, earlier messages, and every attachment."""
    sources = [text]
    seen = {f.name for f in files}
    for entry in ctx.deck.transcript():
        if entry.get("role") != "user":
            continue
        if isinstance(entry.get("content"), str) and entry["content"] != text:
            sources.append(entry["content"])
        for name in entry.get("attachments") or []:
            if name not in seen:
                seen.add(name)
                path = ctx.deck.attachments / Path(str(name)).name
                if path.is_file():
                    sources.append(attachment_text(path))
    sources += [attachment_text(f) for f in files]
    return [s for s in sources if s]


def visible_text(html: str) -> str:
    """A slide's visible text, lower-cased and whitespace-collapsed (styles, scripts, notes out)."""
    body = re.sub(r"(?is)<(script|style|aside)\b.*?</\1>", " ", html)
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", body)).split()).lower()
