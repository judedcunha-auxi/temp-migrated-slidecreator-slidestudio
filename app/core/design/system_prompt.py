"""What the model is told before the person's message: the system prompt, the layout pictures, the workspace.

Ported from Slide Studio `server/chat/system_prompt.py` (migration plan §4.1, K).
"""

from __future__ import annotations

import base64
import io
import json
import logging
from typing import Any

from PIL import Image

from app.core.design import prompts
from app.core.design.deck import DesignDeck
from app.core.design.layouts import assets_url, canvas, layout_label, zone_line
from app.core.design_refs import exemplars
from app.core.design_refs.notes import derive_notes, token_notes
from app.core.storage.paths import is_within

_log = logging.getLogger(__name__)

JSON = dict[str, Any]


def design_system(deck: DesignDeck, provider: str = "anthropic", *, brief_on: bool = True) -> str:
    """The design chat's system prompt, built from the deck's v2 manifest."""
    meta = deck.meta()
    w, h = canvas(meta)
    master = deck.master()
    theme = master.get("theme") or {}
    fonts = theme.get("fonts") or {}
    major = fonts.get("major") or fonts.get("heading") or ""
    minor = fonts.get("minor") or fonts.get("body") or major
    layouts = master.get("layouts") or [{"id": "blank", "name": "Blank canvas", "usage": "free design",
                                         "placeholders": []}]
    labels_meta = {"master": {"layouts": layouts}}
    lines = []
    for lay in layouts:
        zones = "; ".join(zone_line(z) for z in lay.get("placeholders", [])) or "none"
        lines.append(f"- {lay['id']} — {layout_label(labels_meta, lay['id'])} — {lay.get('usage', '')} "
                     f"— zones: {zones}")
    assets = ", ".join(master.get("assets") or sorted(f.name for f in deck.assets.glob("*"))) or "(none yet)"
    font_note = (f"headings/titles {major or 'the theme heading font'}, "
                 f"everything else {minor or 'the theme body font'}")
    system = prompts.design_system_template(exemplars.available()).format(
        w=w, h=h, fonts=font_note, major=major or "the theme heading font",
        minor=minor or "the theme body font", assets_url=assets_url(deck.id), assets=assets,
        colors=json.dumps(theme.get("colors") or {}), layouts="\n".join(lines),
        notes=design_notes(master), chart_types=prompts.CHART_TYPES,
        tools_note=prompts.TOOLS_NOTE.get(provider, prompts.TOOLS_NOTE["anthropic"]),
    )
    return prompts.with_brief(system) if brief_on else system


def design_notes(master: JSON) -> str:
    """The master's written notes (plus its design tokens); notes derived from its manifest; or
    `NO_MASTER_NOTES` when there is no master."""
    written = master.get("designNotes")
    if isinstance(written, str) and written.strip():
        try:
            tokens = token_notes(master) if master.get("layouts") else ""
        except Exception:  # noqa: BLE001 - the written notes stand on their own
            _log.debug("token notes failed; using the written design notes only", exc_info=True)
            tokens = ""
        return f"{written}\n{tokens}" if tokens else written
    if master.get("layouts") and master.get("status") != "none":
        try:
            derived = derive_notes(master)
        except Exception:  # noqa: BLE001 - notes are a help; a manifest they cannot read is not an error
            _log.debug("could not derive design notes from the master", exc_info=True)
            derived = ""
        if derived:
            return str(derived)
    return prompts.NO_MASTER_NOTES


def layout_context(deck: DesignDeck) -> JSON:
    """The first user turn: what each layout looks like. Stable across turns, so it stays cached."""
    meta = deck.meta()
    blocks: list[JSON] = [{"type": "text", "text": "Reference: the master layouts your slides are drawn on."}]
    for lay in deck.master().get("layouts") or []:
        bg = lay.get("background")
        if not isinstance(bg, str) or not bg:
            continue
        path = (deck.layouts_dir / bg).resolve()
        if not is_within(path, deck.layouts_dir.resolve()) or not path.is_file():
            continue
        if bg.endswith(".png"):
            with Image.open(path) as im:
                rgb = im.convert("RGB")
                rgb.thumbnail((960, 960))
                buf = io.BytesIO()
                rgb.save(buf, "JPEG", quality=85)
            blocks.append({"type": "text", "text": f"{layout_label(meta, lay['id'])} — id {lay['id']}:"})
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                         "data": base64.b64encode(buf.getvalue()).decode()}})
        else:
            blocks.append({"type": "text", "text": f"{layout_label(meta, lay['id'])} — id {lay['id']} (HTML "
                                                   f"background):\n" + path.read_text(encoding="utf-8")[:30000]})
    if len(blocks) == 1:
        blocks = [{"type": "text", "text": "No master layouts: design on a blank canvas."}]
    return {"role": "user", "content": blocks}


def workspace_block(meta: JSON, selected: str | None) -> str:
    rows = []
    for i, s in enumerate(meta.get("slides") or [], 1):
        mark = "  ← selected" if s["id"] == selected else ""
        rows.append(f"{i}. {s['id']} — \"{s['title']}\" — on "
                    f"{layout_label(meta, s.get('layoutId')) or 'no layout'} [{s.get('layoutId')}] "
                    f"— version {s['current']}{mark}")
    body = "\n".join(rows) or "(no slides yet)"
    sel = "" if selected else "\nNo slide is selected."
    return f"<workspace>\nDeck: {meta.get('name')}\nSlides:\n{body}{sel}\n</workspace>"
