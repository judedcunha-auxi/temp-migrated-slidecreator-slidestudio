"""Parallel Generate on the fake provider: every storyline slide designed into one deck, with a bounded fan-out.

`ReactiveProvider` answers each round from the request itself (which slide's instruction it carries,
and whether that turn has saved yet), so turns that run at once never share a script. It also
counts how many rounds were in flight together: never more than the fan-out.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from app.core.design import generate
from app.core.design.context import DesignContext
from app.core.design.deck import DesignDeck
from app.core.llm.types import RoundRequest
from app.core.slides.spec import StorylineDeck, StorylineSlide
from tests.core.design.conftest import content_layout, slide_html
from tests.fakes.llm import ReactiveProvider, Round, first_user_text, last_tool_results, text, tool

SLIDES: list[StorylineSlide] = [
    {"id": f"s{n}", "number": n, "title": f"Market {n} grew {10 + n}% in 2025", "type": "data",
     "framework": "column chart", "bullets": [f"Market {n} grew {10 + n}%"]}
    for n in range(1, 6)
]
DECK: StorylineDeck = {"presentationTitle": "Growth review", "audience": "board"}


def designer(deck: DesignDeck, fail: set[int] | None = None) -> Callable[[RoundRequest], Round]:
    layout = content_layout(deck)

    def respond(request: RoundRequest) -> Round:
        said = first_user_text(request)
        number = int(re.search(r"This is slide (\d+) of", said).group(1))  # type: ignore[union-attr]
        if last_tool_results(request):
            return Round(text(f"Designed slide {number}."))
        if fail and number in fail:
            return Round(text("I could not design this one."))
        title = f"Market {number} grew {10 + number}% in 2025"
        return Round(tool("save_slide", {"title": title, "layout_id": layout, "html": slide_html(title)}))

    return respond


def test_generate_designs_every_slide_in_storyline_order_within_the_fanout(
        deck: DesignDeck, make_ctx: Callable[..., DesignContext]):
    provider = ReactiveProvider(designer(deck), delay_s=0.05)
    ctx = make_ctx(deck, provider, design_brief_enabled=False)
    events: list[dict[str, object]] = []
    result = generate.run_generate(ctx, SLIDES, DECK, fanout=2, on_event=events.append)

    assert result.made == 5 and result.failed == 0
    assert 1 <= provider.peak_in_flight <= 2 and result.peak_concurrency <= 2, "the fan-out is bounded"
    titles = [s["title"] for s in deck.slides()]
    assert titles == [s["title"] for s in SLIDES], "slides end up in storyline order"
    assert events[0] == {"type": "plan", "total": 5, "fanout": 2}
    assert events[-1]["type"] == "generated"
    assert result.cost_usd == pytest.approx(sum(o.cost_usd for o in result.outcomes)) and result.cost_usd > 0
    first_turns = [first_user_text(r) for r in provider.requests if not last_tool_results(r)]
    assert all("Growth review" in t for t in first_turns)
    assert deck.history() == [], "each slide runs on its own branch, not the deck's memory"


def test_later_slides_are_told_to_keep_the_look_of_a_designed_one(
        deck: DesignDeck, make_ctx: Callable[..., DesignContext]):
    provider = ReactiveProvider(designer(deck))
    generate.run_generate(make_ctx(deck, provider, design_brief_enabled=False), SLIDES[:3], DECK, fanout=1)
    instructions = [first_user_text(r) for r in provider.requests if not last_tool_results(r)]
    assert "read_slide it once" not in instructions[0]
    assert all("read_slide it once" in t for t in instructions[1:])


def test_a_failed_slide_is_reported_and_the_rest_carry_on(deck: DesignDeck, make_ctx: Callable[..., DesignContext]):
    provider = ReactiveProvider(designer(deck, fail={2}))
    result = generate.run_generate(make_ctx(deck, provider, design_brief_enabled=False), SLIDES[:3], DECK, fanout=3)
    assert result.made == 2 and result.failed == 1
    failed = next(o for o in result.outcomes if not o.slide_id)
    assert failed.story_id == "s2" and failed.error


def test_the_fanout_setting_is_the_default_and_is_capped(deck: DesignDeck, make_ctx: Callable[..., DesignContext]):
    provider = ReactiveProvider(designer(deck), delay_s=0.05)
    ctx = make_ctx(deck, provider, design_brief_enabled=False, design_generate_fanout=1)
    result = generate.run_generate(ctx, SLIDES[:3], DECK)
    assert provider.peak_in_flight == 1 and result.made == 3


def test_a_stop_ends_the_run_and_keeps_what_was_made(deck: DesignDeck, make_ctx: Callable[..., DesignContext]):
    provider = ReactiveProvider(designer(deck), delay_s=0.02)
    ctx = make_ctx(deck, provider, design_brief_enabled=False)
    ctx.should_stop = lambda: len(deck.slides()) >= 1
    result = generate.run_generate(ctx, SLIDES, DECK, fanout=1)
    assert 1 <= result.made < 5 and result.failed >= 1
    assert all(o.error == "Stopped." for o in result.outcomes if not o.slide_id)
