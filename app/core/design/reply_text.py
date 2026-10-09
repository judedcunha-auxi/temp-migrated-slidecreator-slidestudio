"""The model's streamed reply, turned into the words the person reads (live and in the transcript).

Ported from Slide Studio `server/chat/reply_text.py` (migration plan §4.1, K).
"""

from __future__ import annotations

import re

from app.core.design.layouts import speak_of_layouts


class ReplyText:
    """Text blocks separated by a blank line, layouts named as the app names them. The trailing word
    of a block is held back until the block ends, so an id split across deltas is still recognised."""

    def __init__(self, labels: dict[str, str]) -> None:
        self.labels, self.done, self.pending = labels, "", ""

    def start_block(self) -> str:
        out = self.end_block()
        if self.done.strip() and not self.done.endswith("\n\n"):
            sep = "\n" if self.done.endswith("\n") else "\n\n"
            self.done += sep
            out += sep
        return out

    def feed(self, delta: str) -> str:
        self.pending += delta
        m = re.search(r"\s(?=\S*$)", self.pending)
        if not m:
            return ""
        out, self.pending = speak_of_layouts(self.pending[:m.end()], self.labels), self.pending[m.end():]
        self.done += out
        return out

    def end_block(self) -> str:
        out, self.pending = speak_of_layouts(self.pending, self.labels), ""
        self.done += out
        return out

    def text(self) -> str:
        return self.done + speak_of_layouts(self.pending, self.labels)
