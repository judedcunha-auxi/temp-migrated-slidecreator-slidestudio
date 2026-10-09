"""What one design turn remembers between tool calls.

Ported from Slide Studio `server/chat/turn_state.py` (migration plan §4.1, K).
"""

from __future__ import annotations

from typing import Any

from app.core.design_refs import slide_brief

JSON = dict[str, Any]


class Turn:
    """What one turn's tool handler remembers between calls.

    `touched`: the slides saved, in order. `previews`: the last preview of each slide this turn
    (picture and critique), so a second preview is judged against the first. `extra_usage`: money
    spent outside the provider's rounds (the critic), already priced with its own model; the turn
    folds it into its total after the round (`take_extra_usage`).
    """

    def __init__(self, instruction: str, selected: str | None = None) -> None:
        self.instruction = instruction
        self.selected = selected
        self.touched: list[str] = []
        self.previews: dict[str, JSON] = {}
        self.extra_usage: list[JSON] = []
        # the design brief (`write_brief`): where figures may come from, the briefs waiting for their
        # slides, the briefs saved per slide, one repair per brief, one missing-brief nudge per turn
        self.sources: list[str] = [instruction]
        self.pending_briefs: list[tuple[slide_brief.Brief, list[Any]]] = []
        self.briefs: dict[str, slide_brief.Brief] = {}
        self.brief_rejected = False
        self.brief_declined = False
        self.nudged = False
        # every critic review this turn, in order (sid, passed): the pipeline reports them
        self.reviews: list[tuple[str, bool]] = []

    def touch(self, sid: str) -> None:
        if sid not in self.touched:
            self.touched.append(sid)

    def take_extra_usage(self) -> list[JSON]:
        taken, self.extra_usage = self.extra_usage, []
        return taken
