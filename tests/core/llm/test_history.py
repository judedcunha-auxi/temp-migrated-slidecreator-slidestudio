"""The canonical history and its renderings (app/core/llm/history.py).

Ported from Slide Studio `server/tests/test_history.py` (the rendering parts; Files API uploads and
the code-execution sandbox are gone, attachments are inline).
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from app.core.llm import history

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


def test_attachment_blocks_are_canonical_and_relative_to_the_workspace(tmp_path: Path):
    (tmp_path / "in").mkdir()
    image = tmp_path / "in" / "slide.png"
    image.write_bytes(PNG)
    notes = tmp_path / "in" / "notes.csv"
    notes.write_text("a,b\n1,2\n", encoding="utf-8")
    block = history.attachment_block(tmp_path, image)
    assert block == {"type": "attachment", "name": "slide.png", "path": "in/slide.png", "mime": "image/png",
                     "kind": "image", "note": "Attached image `slide.png`."}
    # always a text/* type, whatever the OS guesses for .csv (Windows says a spreadsheet)
    assert history.attachment_block(tmp_path, notes)["mime"].startswith("text/")


def test_anthropic_rendering_inlines_every_kind(tmp_path: Path):
    files = {"a.png": PNG, "b.pdf": b"%PDF-1.4 x", "c.md": b"# notes", "d.pptx": b"PK\x03\x04"}
    blocks = []
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
        blocks.append(history.attachment_block(tmp_path, tmp_path / name))
    wire = history.to_anthropic([{"role": "user", "content": blocks}], "claude-opus-5-5", tmp_path)
    kinds = [b["type"] for b in wire[0]["content"]]
    assert kinds == ["text", "image", "document", "document", "text"]
    image = wire[0]["content"][1]["source"]
    assert image == {"type": "base64", "media_type": "image/png", "data": base64.b64encode(PNG).decode()}
    assert wire[0]["content"][2]["source"]["media_type"] == "application/pdf"
    assert wire[0]["content"][3]["source"] == {"type": "text", "media_type": "text/plain", "data": "# notes"}
    assert "cannot open" in wire[0]["content"][4]["text"]


def test_a_missing_or_escaping_attachment_is_one_line_not_a_failure(tmp_path: Path):
    gone = {"type": "attachment", "name": "x.png", "path": "in/x.png", "kind": "image"}
    escape = {"type": "attachment", "name": "y.png", "path": "../y.png", "kind": "image"}
    (tmp_path.parent / "y.png").write_bytes(PNG)
    wire = history.to_anthropic([{"role": "user", "content": [gone, escape]}], "claude-opus-5-5", tmp_path)
    assert all(b["type"] == "text" and "no longer available" in b["text"] for b in wire[0]["content"])


def test_other_models_thinking_is_dropped_and_own_is_kept():
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "model": "claude-sonnet-5-5",
         "content": [{"type": "thinking", "thinking": "x", "signature": "s"}, {"type": "text", "text": "a"}]},
        {"role": "user", "content": "again"},
        {"role": "assistant", "model": "claude-opus-5-5",
         "content": [{"type": "thinking", "thinking": "y", "signature": "t"}, {"type": "text", "text": "b"}]},
    ]
    wire = history.to_anthropic(messages, "claude-opus-5-5")
    assert [b["type"] for b in wire[1]["content"]] == ["text"]
    assert [b["type"] for b in wire[3]["content"]] == ["thinking", "text"]
    assert all("model" not in m for m in wire), "private keys never reach the API"
    assert messages[1]["content"][0]["type"] == "thinking", "the stored history is not mutated"


def test_gemini_tool_calls_get_ids_for_claude():
    messages: list[dict[str, Any]] = [
        {"role": "assistant", "model": "gemini-3.8-flash", "gemini_steps": [{"type": "function_call"}],
         "content": [{"type": "tool_use", "name": "save_slide", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "content": "Saved."}]},
    ]
    wire = history.to_anthropic(messages, "claude-opus-5-5")
    assert wire[0]["content"][0]["id"] == "toolu_gm_1"
    assert wire[1]["content"][0]["tool_use_id"] == "toolu_gm_1"


def test_a_claude_turn_is_flattened_for_gemini_and_its_results_dropped():
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": "make it"}]},
        {"role": "assistant", "model": "claude-opus-5-5",
         "content": [{"type": "text", "text": "On it."},
                     {"type": "tool_use", "id": "toolu_1", "name": "save_slide", "input": {"title": "T"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "Saved."},
                                     {"type": "text", "text": "thanks"}]},
    ]
    items = history.to_gemini(messages)
    assert items[1] == {"type": "model_output",
                        "content": [{"type": "text", "text": 'On it.\nI saved the slide "T" (save_slide).'}]}
    assert items[2] == {"type": "user_input", "content": [{"type": "text", "text": "thanks"}]}
