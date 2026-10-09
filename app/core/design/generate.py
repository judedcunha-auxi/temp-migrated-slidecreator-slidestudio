"""Parallel Generate: a storyline's slides designed into one deck, several at once, with a bounded fan-out.

Each slide is one design turn with the spec-mode instruction (`app.core.slides.spec`), on its own
branch of model memory starting empty, so the tenth slide does not pay for the first nine and no slide
sees another's turn. At most `DESIGN_GENERATE_FANOUT` turns run at once (every turn is a paid model
call with browser previews, so the fan-out is bounded, unlike Slide Studio's "every slide at once"
default). A slide that starts after a content slide is designed is told to read it and keep its look.
A failed slide is reported and the rest carry on; a stop ends the run between rounds and keeps what
was made.

Ported from Slide Studio `server/storyline/service.generate` (migration plan §4.1, K/R). The storyline
is owned by `app/core/storyline`; this module needs only `spec.StorylineSlide` / `StorylineDeck`.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, cast

from app.core import masters
from app.core.design.context import DesignContext
from app.core.design.design_turn import run_design_turn
from app.core.llm.types import Cancelled
from app.core.slides import spec as spec_mod
from app.core.slides.spec import BrandOptions, StorylineDeck, StorylineSlide

_log = logging.getLogger(__name__)

JSON = dict[str, Any]

#: Storyline slide types that are master furniture, not content (they set no style reference).
FURNITURE = frozenset({"title", "divider", "agenda"})


@dataclass
class SlideOutcome:
    story_id: str
    number: int
    slide_id: str | None
    error: str | None
    cost_usd: float


@dataclass
class GenerateResult:
    outcomes: list[SlideOutcome] = field(default_factory=list)
    peak_concurrency: int = 0

    @property
    def made(self) -> int:
        return sum(1 for o in self.outcomes if o.slide_id)

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if not o.slide_id)

    @property
    def cost_usd(self) -> float:
        return round(sum(o.cost_usd for o in self.outcomes), 6)


def instruction_for(ctx: DesignContext, slide: StorylineSlide, deck: StorylineDeck, total: int,
                    brand: BrandOptions, style_ref: str | None) -> str:
    raw: JSON = {"slide": dict(slide), "deck": dict(deck)}
    spec = spec_mod.normalize_spec(raw)
    archetype = spec_mod.spec_layout_archetype(spec)
    w, h = ctx.deck.canvas()
    parts = [spec_mod.spec_instruction(spec, brand, archetype, w, h, dense=True, brief_on=ctx.brief_on)]
    try:
        layout_id = masters.layout_for(ctx.deck, archetype)
        parts.append(f"Save it on the layout with layout_id \"{layout_id}\".")
    except masters.MasterError:
        pass
    line = f"This is slide {slide.get('number')} of {total} in the deck \"{deck.get('presentationTitle') or ''}\"."
    if style_ref and archetype == "content":
        line += (f" Keep the deck's visual language consistent: slide {style_ref} is already designed — read_slide it "
                 "once and reuse its colours, type sizes and component styles.")
    parts.append(line)
    return "\n\n".join(parts)


def generate(ctx: DesignContext, slides: list[StorylineSlide], deck: StorylineDeck, *,
             brand: BrandOptions | None = None, fanout: int | None = None) -> Generator[JSON, None, GenerateResult]:
    """Design `slides` into `ctx.deck`, at most `fanout` (default `DESIGN_GENERATE_FANOUT`) at once.

    A generator of progress events (`progress`, and each turn's `status`/`slide` events tagged with
    `storyId`); returns the `GenerateResult`. Slides end up in storyline order.
    """
    brand = brand or BrandOptions()
    cap = max(1, min(int(fanout or ctx.settings.design_generate_fanout), 16))
    total = len(slides)
    result = GenerateResult()
    events: queue.Queue[tuple[str, JSON]] = queue.Queue()
    style_lock = threading.Lock()
    style_ref: list[str | None] = [None]
    running = [0]
    stop = threading.Event()

    def stopped() -> bool:
        return stop.is_set() or ctx.stopping()

    def design_one(slide: StorylineSlide) -> None:
        story_id = str(slide.get("id") or slide.get("number"))
        sid: str | None = None
        error: str | None = None
        cost = 0.0
        saved: list[str] = []
        with style_lock:
            running[0] += 1
            result.peak_concurrency = max(result.peak_concurrency, running[0])
            ref = style_ref[0]
        try:
            if stopped():
                raise Cancelled()
            turn_ctx = DesignContext(deck=ctx.deck, settings=ctx.settings, resolve=ctx.resolve, services=ctx.services,
                                     model=ctx.model, should_stop=stopped)
            text = instruction_for(turn_ctx, slide, deck, total, brand, ref)
            before = {s["id"] for s in ctx.deck.slides()}

            def seen(ev: JSON) -> None:
                # this turn's own saves, so a stop that ends the turn still keeps its slide
                if ev.get("type") == "slide" and ev.get("slideId") not in before and ev["slideId"] not in saved:
                    saved.append(str(ev["slideId"]))
                events.put((story_id, ev))

            outcome = run_design_turn(turn_ctx, text, [], None, branch=[],
                                      shown=f"Design slide {slide.get('number')}: {slide.get('title')}",
                                      on_event=seen)
            cost = outcome.cost_usd
            sid = saved[0] if saved else None
            error = None if sid else (outcome.error or "The design turn saved no slide.")
            if sid and str(slide.get("type") or "").lower() not in FURNITURE:
                with style_lock:
                    style_ref[0] = style_ref[0] or sid
        except Cancelled:
            sid = saved[0] if saved else None
            error = None if sid else "Stopped."
        except Exception as exc:  # noqa: BLE001 - one slide's failure never ends the run
            _log.warning("generate: slide %s failed", story_id, exc_info=True)
            error = f"The slide could not be designed ({type(exc).__name__})."
        finally:
            with style_lock:
                running[0] -= 1
            events.put((story_id, {"type": "end", "slideId": sid, "error": error, "cost": cost,
                                   "number": slide.get("number")}))

    yield {"type": "plan", "total": total, "fanout": cap}
    pending = len(slides)
    pool = ThreadPoolExecutor(max_workers=cap, thread_name_prefix="generate")
    try:
        for slide in slides:
            pool.submit(design_one, slide)
        while pending:
            try:
                story_id, ev = events.get(timeout=0.25)
            except queue.Empty:
                if ctx.stopping():
                    stop.set()
                continue
            if ev["type"] != "end":
                if ev["type"] in ("status", "slide", "error"):
                    yield {**ev, "storyId": story_id}
                continue
            pending -= 1
            outcome = SlideOutcome(story_id=story_id, number=int(ev.get("number") or 0), slide_id=ev["slideId"],
                                   error=ev["error"], cost_usd=float(ev["cost"] or 0.0))
            result.outcomes.append(outcome)
            yield {"type": "progress", "storyId": story_id, "state": "done" if outcome.slide_id else "failed",
                   "slideId": outcome.slide_id, "error": outcome.error, "cost": result.cost_usd}
    finally:
        if pending:
            stop.set()
        pool.shutdown(wait=True)
        order_slides(ctx, slides, result)
    yield {"type": "generated", "designed": result.made, "failed": result.failed, "cost": result.cost_usd}
    return result


def order_slides(ctx: DesignContext, slides: list[StorylineSlide], result: GenerateResult) -> None:
    """Deck slides in storyline order; slides the storyline does not know keep their place after them."""
    by_story = {o.story_id: o.slide_id for o in result.outcomes if o.slide_id}
    wanted = [by_story.get(str(s.get("id") or s.get("number"))) for s in slides]
    ids = [w for w in wanted if w]

    def fn(meta: JSON) -> None:
        by_id = {s["id"]: s for s in meta.get("slides") or []}
        meta["slides"] = [by_id.pop(i) for i in ids if i in by_id] + list(by_id.values())
    ctx.deck.update(fn)


def run_generate(ctx: DesignContext, slides: list[StorylineSlide], deck: StorylineDeck, *,
                 brand: BrandOptions | None = None, fanout: int | None = None,
                 on_event: Callable[[JSON], None] | None = None) -> GenerateResult:
    gen = generate(ctx, slides, deck, brand=brand, fanout=fanout)
    while True:
        try:
            ev = next(gen)
        except StopIteration as done:
            return cast(GenerateResult, done.value)
        if on_event is not None:
            on_event(ev)
