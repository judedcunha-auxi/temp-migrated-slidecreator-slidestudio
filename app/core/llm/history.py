"""The canonical history, and how each provider renders it for its wire.

The history is kept in Anthropic block format; provider-specific material rides along in keys the
other provider strips. Two things make a history readable by whichever model reads it next:

* every assistant turn says which model wrote it (`"model"`), and a Gemini turn carries its raw steps
  (`"gemini_steps"`) so they can be replayed verbatim;
* an attachment is stored as what it *is*, a file in the job workspace, rather than as one
  provider's handle on it::

      {"type": "attachment", "name": "slide.png", "path": "in/slide.png",
       "mime": "image/png", "kind": "image", "note": "Attached image `slide.png`."}

  `path` is relative to the request's `attachments_root` (the job workspace). Both renderers send
  attachments inline (base64 or text): the service uploads nothing to a provider's file store.

Neither renderer mutates the stored history: both build a new list. Ported from Slide Studio
`server/llm/history.py` (migration plan §4.1, K/R); the Files API uploads and the code-execution
sandbox are dropped (the service's attachments are images, PDFs and text, sent inline).
"""

from __future__ import annotations

import base64
import copy
import mimetypes
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core.llm import registry
from app.core.llm.types import JSON
from app.core.storage.paths import is_within

#: Which model wrote a turn that was stored before turns said so.
LEGACY_MODEL = registry.DEFAULT_DESIGN_MODEL
#: Keys that live in the stored history and in no provider's API.
PRIVATE_KEYS = ("model", "gemini_steps")
#: Blocks one model signed and another must never be shown.
THINKING_BLOCKS = ("thinking", "redacted_thinking")

TEXT_TYPES = frozenset({".txt", ".md", ".csv", ".tsv", ".json", ".html", ".htm", ".xml", ".yaml", ".yml", ".css"})
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}
#: The most an inline text attachment carries; past it the model is told the file is too large.
MAX_INLINE_TEXT = 2_000_000
#: The largest PDF sent inline (base64); past it the model is told the file is too large.
MAX_INLINE_PDF = 20_000_000
FOREIGN_ATTACHMENT = "[An earlier attachment is not available to this model.]"


# ------------------------------------------------------------------ writing canonical history
def attachment_block(root: Path, path: Path, mime: str | None = None) -> JSON:
    """One attachment as the canonical block both providers render. `path` must be under `root`
    (the job workspace); the block stores it relative to `root`."""
    name = path.name
    ext = path.suffix.lower()
    if ext in IMAGE_TYPES:
        kind, mime = "image", mime or IMAGE_TYPES[ext]
        note = f"Attached image `{name}`."
    elif ext == ".pdf":
        kind, mime = "pdf", mime or "application/pdf"
        note = f"Attached file `{name}`."
    elif ext in TEXT_TYPES:
        kind = "text"
        guess = mime or mimetypes.guess_type(name)[0] or ""
        mime = guess if guess.startswith("text/") else "text/plain"
        note = f"Attached file `{name}`."
    else:
        kind, mime = "file", mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
        note = f"Attached file `{name}`."
    rel = path.resolve().relative_to(root.resolve()).as_posix()
    return {"type": "attachment", "name": name, "path": rel, "mime": mime, "kind": kind, "note": note}


def model_of_message(msg: JSON) -> str:
    """Which model wrote this turn."""
    model = msg.get("model")
    return model if isinstance(model, str) and model else LEGACY_MODEL


def _attachment_file(root: Any, block: JSON) -> Path | None:
    """The file an attachment names, only if it is a regular file inside `root`."""
    if root is None:
        return None
    base = Path(root).resolve()
    rel = str(block.get("path") or "")
    if not rel or Path(rel).is_absolute():
        return None
    candidate = (base / rel).resolve()
    if not is_within(candidate, base) or not candidate.is_file():
        return None
    return candidate


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


# ------------------------------------------------------------------------- rendering: Anthropic
def to_anthropic(messages: list[JSON], model: str, root: Any = None) -> list[JSON]:
    """The canonical history as the Claude API wants it: a **new** list.

    `model` and `gemini_steps` are dropped; thinking blocks written by a *different* model are
    dropped (their signatures are another model's); `attachment` blocks become inline blocks (image
    -> base64 image, pdf -> base64 document, text -> text document). A Gemini `tool_use` without an
    id is given `toolu_gm_<n>`, and the matching `tool_result` is renamed to match.
    """
    wire: list[JSON] = []
    mint = _minter()
    awaiting: list[str] = []
    for msg in messages:
        rendered = {k: v for k, v in msg.items() if k not in PRIVATE_KEYS}
        content = msg.get("content")
        if not isinstance(content, list):
            wire.append(rendered)
            awaiting = []
            continue
        if msg.get("role") == "assistant":
            blocks, awaiting = _assistant_blocks(msg, content, model, mint)
            if not blocks:
                continue
        else:
            blocks = _user_blocks(content, awaiting, root)
            awaiting = []
        rendered["content"] = blocks
        wire.append(rendered)
    return wire


def _assistant_blocks(msg: JSON, content: list[Any], model: str,
                      mint: Callable[[], str]) -> tuple[list[JSON], list[str]]:
    keep_thinking = model_of_message(msg) == model
    from_gemini = "gemini_steps" in msg
    blocks: list[JSON] = []
    minted: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in THINKING_BLOCKS and not keep_thinking:
            continue
        if from_gemini and kind == "tool_use" and not block.get("id"):
            block = dict(block, id=mint())
            minted.append(block["id"])
        blocks.append(block)
    return blocks, minted


def _user_blocks(content: list[Any], awaiting: list[str], root: Any) -> list[JSON]:
    blocks: list[JSON] = []
    pending = list(awaiting)
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "attachment":
            blocks.extend(_anthropic_attachment(block, root))
            continue
        if kind == "tool_result" and not block.get("tool_use_id") and pending:
            block = dict(block, tool_use_id=pending.pop(0))
        blocks.append(block)
    return blocks


def _anthropic_attachment(block: JSON, root: Any) -> list[JSON]:
    name = str(block.get("name") or "attachment")
    path = _attachment_file(root, block)
    if path is None:
        return [{"type": "text", "text": f"[The attachment `{name}` is no longer available.]"}]
    note = str(block.get("note") or f"Attached file `{name}`.")
    kind = block.get("kind") or "file"
    mime = str(block.get("mime") or "")
    size = path.stat().st_size
    if kind == "image":
        return [{"type": "text", "text": note},
                {"type": "image", "source": {"type": "base64", "media_type": mime or "image/png", "data": _b64(path)}}]
    if kind == "pdf" and size <= MAX_INLINE_PDF:
        return [{"type": "document", "title": name,
                 "source": {"type": "base64", "media_type": "application/pdf", "data": _b64(path)}}]
    if kind == "text" and size < MAX_INLINE_TEXT:
        return [{"type": "document", "title": name,
                 "source": {"type": "text", "media_type": "text/plain",
                            "data": path.read_text(encoding="utf-8", errors="replace")}}]
    return [{"type": "text", "text": f"[The attachment `{name}` is a file this model cannot open here.]"}]


# ---------------------------------------------------------------------------- rendering: Gemini
def to_gemini(messages: list[JSON], root: Any = None) -> list[JSON]:
    """The canonical history as Gemini Interactions input items: a **new** list.

    Gemini turns (with `gemini_steps`) are replayed verbatim (Google requires the signed thought
    steps back exactly as received). A Claude turn becomes one `model_output` text step with its
    tool calls folded into sentences, and the matching tool results are dropped (a `function_call`
    step without its own signed thought is a 400). A `tool_result` that answers a replayed Gemini
    `function_call` is a `function_result` item. Attachments go inline.
    """
    items: list[JSON] = []
    open_calls: list[tuple[str, str]] = []
    for msg in messages:
        if msg.get("role") == "assistant":
            steps, open_calls = _gemini_assistant(msg)
            items.extend(steps)
            continue
        items.extend(_gemini_user(msg.get("content"), open_calls, root))
        open_calls = []
    return _merge_user_inputs(items)


def _merge_user_inputs(items: list[JSON]) -> list[JSON]:
    merged: list[JSON] = []
    for item in items:
        last = merged[-1] if merged else None
        if item.get("type") == "user_input" and last is not None and last.get("type") == "user_input":
            last["content"] = list(last["content"]) + list(item["content"])
            continue
        merged.append(item)
    return merged


def _is_gemini(model_id: str) -> bool:
    try:
        return registry.get(model_id).provider == "gemini"
    except KeyError:
        return model_id.startswith("gemini")


def _gemini_assistant(msg: JSON) -> tuple[list[JSON], list[tuple[str, str]]]:
    steps = msg.get("gemini_steps")
    if isinstance(steps, list) and steps and _is_gemini(model_of_message(msg)):
        replayed: list[JSON] = copy.deepcopy([s for s in steps if isinstance(s, dict)])
        calls = [(str(s.get("id") or ""), str(s.get("name") or ""))
                 for s in replayed if s.get("type") == "function_call"]
        return replayed, calls
    said = _gemini_said(msg.get("content"))
    return ([{"type": "model_output", "content": [{"type": "text", "text": said}]}] if said else []), []


def _gemini_said(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"].strip())
        elif kind == "tool_use":
            parts.append(_tool_use_sentence(block))
    return "\n".join(part for part in parts if part).strip()


def _tool_use_sentence(block: JSON) -> str:
    name = str(block.get("name") or "a tool")
    raw = block.get("input")
    args: JSON = raw if isinstance(raw, dict) else {}
    if name == "save_slide":
        title = args.get("title")
        return f'I saved the slide "{title}" (save_slide).' if title else "I saved a slide (save_slide)."
    if name in ("read_slide", "edit_slide", "preview_slide"):
        return f"I used {name} on the slide {args.get('slide_id')}."
    return f"I used the {name} tool."


def _gemini_user(content: Any, open_calls: list[tuple[str, str]], root: Any) -> list[JSON]:
    if isinstance(content, str):
        text = content.strip()
        return [{"type": "user_input", "content": [{"type": "text", "text": text}]}] if text else []
    steps: list[JSON] = []
    items: list[JSON] = []
    pending = list(open_calls)
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            result = _function_result(block, pending)
            if result is not None:
                steps.append(result)
            continue
        items.extend(_gemini_items(block, root))
    if items:
        steps.append({"type": "user_input", "content": items})
    return steps


def _function_result(block: JSON, pending: list[tuple[str, str]]) -> JSON | None:
    wanted = block.get("tool_use_id")
    call = next((c for c in pending if c[0] and c[0] == wanted), None)
    if call is None:
        if not pending:
            return None
        call = pending[0]
    pending.remove(call)
    step: JSON = {"type": "function_result", "call_id": call[0] or str(wanted or "")}
    if call[1]:
        step["name"] = call[1]
    step["result"] = [{"type": "text", "text": _result_text(block)}, *_result_images(block)]
    if block.get("is_error"):
        step["is_error"] = True
    return step


def _result_text(block: JSON) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    return "".join(str(c.get("text") or "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text")


def _result_images(block: JSON) -> list[JSON]:
    content = block.get("content")
    if not isinstance(content, list):
        return []
    items: list[JSON] = []
    for c in content:
        source = c.get("source") if isinstance(c, dict) and c.get("type") == "image" else None
        if isinstance(source, dict) and source.get("type") == "base64" and source.get("data"):
            items.append({"type": "image", "mime_type": source.get("media_type") or "image/png",
                          "data": source["data"]})
    return items


def _gemini_items(block: JSON, root: Any) -> list[JSON]:
    kind = block.get("type")
    if kind == "text":
        text = block.get("text")
        return [{"type": "text", "text": text}] if isinstance(text, str) and text else []
    if kind == "attachment":
        return _gemini_attachment(block, root)
    if kind in ("image", "document"):
        raw = block.get("source")
        source: JSON = raw if isinstance(raw, dict) else {}
        if source.get("type") == "base64" and source.get("data"):
            return [{"type": kind, "mime_type": source.get("media_type") or "application/octet-stream",
                     "data": source["data"]}]
        if source.get("type") == "text" and isinstance(source.get("data"), str):
            return [{"type": "text", "text": source["data"]}]
        return [{"type": "text", "text": FOREIGN_ATTACHMENT}]
    return []


def _gemini_attachment(block: JSON, root: Any) -> list[JSON]:
    name = str(block.get("name") or "attachment")
    path = _attachment_file(root, block)
    if path is None:
        return [{"type": "text", "text": f"[The attachment `{name}` is no longer available.]"}]
    note: JSON = {"type": "text", "text": str(block.get("note") or f"Attached file `{name}`.")}
    kind = block.get("kind") or "file"
    mime = str(block.get("mime") or "")
    size = path.stat().st_size
    if kind == "image":
        return [note, {"type": "image", "mime_type": mime or "image/png", "data": _b64(path)}]
    if kind == "pdf" and size <= MAX_INLINE_PDF:
        return [note, {"type": "document", "mime_type": "application/pdf", "data": _b64(path)}]
    if kind == "text" and size < MAX_INLINE_TEXT:
        return [{"type": "text", "text": f"{note['text']}\n\n{path.read_text(encoding='utf-8', errors='replace')}"}]
    return [{"type": "text", "text": f"[The attachment `{name}` is a file this model cannot open here.]"}]


def _minter() -> Callable[[], str]:
    """`toolu_gm_1`, `toolu_gm_2`, ...: ids for Gemini tool calls that arrived without one."""
    minted = 0

    def mint() -> str:
        nonlocal minted
        minted += 1
        return f"toolu_gm_{minted}"

    return mint
