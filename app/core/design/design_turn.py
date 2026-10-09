"""One design turn: an instruction in, the model's rounds run, the tools answered, the reply and its cost kept.

`chat_turn(ctx, text, files, selected)` is the entry point (the pipeline, Generate, HTML refine and
the later chat routes all call it). It is a generator: it yields events as the turn runs and
**returns** a `DesignTurnResult` (read it with `yield from`, or `run_design_turn` to drain it).

1. `record_request`: the message goes into the deck's transcript.
2. `new_turn`: the turn's memory (`Turn`), with the material a brief's figures may come from.
3. `user_message`: the workspace block, the attachments, the text.
4. `model_messages`: the layout pictures, then the history the turn runs on, then the message.
5. `app.core.llm.loop.run_turn` with `dispatch_tool` answering each tool call (max_tokens
   continuation and per-round pricing live there), events relayed through `ReplyText`.
6. `record_reply`: the reply and the cost (design rounds + the critic's calls) go into the transcript.

`branch` runs the turn on its own model memory instead of the deck's `history.json`: the list is the
history it starts from and is kept up to date after every round. Parallel Generate designs several
slides at once this way.

Ported from Slide Studio `server/chat/design_turn.py` (migration plan §4.1, K/R): the provider loop
moved to `app/core/llm/loop.py`, and a `DesignContext` replaces the module globals.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar, cast

from app.core.design import briefs, preview_tool, slide_tools, system_prompt
from app.core.design.context import DesignContext
from app.core.design.deck import new_id, now
from app.core.design.layouts import layout_labels
from app.core.design.reply_text import ReplyText
from app.core.design.tool_schemas import offered_tools
from app.core.design.turn_state import Turn
from app.core.design_refs import archetypes, exemplars, exhibit_plan
from app.core.llm import history as llm_history
from app.core.llm import loop, registry
from app.core.llm.types import JSON, Cancelled, Event, ProviderError, ProviderUnavailable, ToolCall, TurnResult, Usage

_log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class DesignTurnResult:
    """What one design turn did and cost."""

    touched: list[str]
    reply: str
    model: str
    cost_usd: float = 0.0
    design_cost_usd: float = 0.0
    critic_cost_usd: float = 0.0
    usage: JSON = field(default_factory=dict)
    stop_reason: str = "end_turn"
    continuations: int = 0
    rounds: int = 0
    error: str | None = None
    reviews: list[tuple[str, bool]] = field(default_factory=list)

    @property
    def last_slide(self) -> str | None:
        return self.touched[-1] if self.touched else None


# ------------------------------------------------------------------------------------- the steps
def record_request(ctx: DesignContext, shown: str, files: list[Path]) -> None:
    ctx.deck.add_transcript({"id": new_id("msg"), "role": "user", "content": shown,
                             "attachments": [f.name for f in files], "createdAt": now()})


def new_turn(ctx: DesignContext, text: str, files: list[Path], selected: str | None) -> Turn:
    turn = Turn(text, selected)
    if ctx.brief_on:
        try:
            turn.sources = briefs.brief_sources(ctx, text, files)
        except Exception:  # noqa: BLE001 - without them only this message grounds a figure
            _log.debug("could not gather brief sources", exc_info=True)
    return turn


def user_message(ctx: DesignContext, text: str, files: list[Path], selected: str | None) -> list[JSON]:
    content: list[JSON] = [{"type": "text", "text": system_prompt.workspace_block(ctx.deck.meta(), selected)}]
    content += [llm_history.attachment_block(ctx.deck.dir, f) for f in files]
    content.append({"type": "text", "text": text})
    return content


def model_messages(ctx: DesignContext, branch: list[JSON] | None, content: list[JSON]) -> list[JSON]:
    """The layout pictures first (stable, so they stay cached), then the history, then this message."""
    history = list(branch) if branch is not None else ctx.deck.history()
    history.append({"role": "user", "content": content})
    return [system_prompt.layout_context(ctx.deck), *history]


def history_saver(ctx: DesignContext, branch: list[JSON] | None) -> Callable[[list[JSON]], None]:
    """`on_round`: keep the model's memory after every round: the deck's, or the branch's own."""
    def persist(messages: list[JSON]) -> None:
        if branch is not None:
            branch[:] = messages[1:]
            return
        ctx.deck.save_history(messages[1:])
    return persist


def _result(tool_use_id: str, content: Any) -> JSON:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}


def dispatch_tool(ctx: DesignContext, turn: Turn, call: ToolCall) -> tuple[JSON, Event | None]:
    """Answer one tool call: the tool_result for the model, and the event (if any) for the caller.
    A bad call is an answer, never a crash: the model gets the error and repairs its call."""
    name, inp = call.name, call.input if isinstance(call.input, dict) else {}
    try:
        if name == "save_slide":
            return slide_tools.save_slide(ctx, turn, call.id, inp)
        if name == "edit_slide":
            return slide_tools.edit_slide(ctx, turn, call.id, inp)
        if name == "get_component":
            return slide_tools.component_result(call.id, inp, ctx.deck.master()), None
        if name == "read_slide":
            return _result(call.id, ctx.deck.read_slide(str(inp.get("slide_id")))), None
        if name == "write_brief":
            if not ctx.brief_on:
                raise ValueError("write_brief is not available; use plan_exhibit")
            return briefs.write_brief(turn, call.id, inp), None
        if name == "plan_exhibit":
            return _result(call.id, exhibit_plan.plan_exhibit(
                str(inp.get("title") or ""), inp.get("slide_type"), inp.get("framework"), inp.get("facts"),
                inp.get("lower_is_better"))), None
        if name == "find_layout_reference":
            return _result(call.id, archetypes.find_layout_reference(
                str(inp.get("query") or ""), inp.get("slide_type"), inp.get("limit", archetypes.DEFAULT_LIMIT))), None
        if name == "preview_slide":
            return preview_tool.preview_result(ctx, call.id, str(inp.get("slide_id")), turn), None
        if name == "get_exemplars":
            if not exemplars.available():
                raise ValueError("no layout exemplars are installed; use find_layout_reference")
            return _result(call.id, exemplars.get_exemplars(
                inp.get("query"), inp.get("slide_type"), inp.get("framework"),
                inp.get("limit", exemplars.DEFAULT_LIMIT))), None
        raise ValueError(f"unknown tool {name}")
    except (KeyError, ValueError) as exc:
        return {"type": "tool_result", "tool_use_id": call.id, "is_error": True, "content": f"Error: {exc}"}, None
    except Exception as exc:  # noqa: BLE001 - malformed tool input must not end the turn
        _log.debug("tool input could not be used", exc_info=True)
        return ({"type": "tool_result", "tool_use_id": call.id, "is_error": True,
                 "content": f"Error: {name} could not use that input ({type(exc).__name__}: {exc}). Check the "
                            "arguments against the tool's schema and call it again."}, None)


class Tally:
    """What the turn has cost: design rounds (priced in the loop) plus the critic's calls."""

    def __init__(self, turn: Turn) -> None:
        self.turn = turn
        self.design = Usage()
        self.critic_cost = 0.0

    @property
    def cost(self) -> float:
        return round(self.design.cost_usd + self.critic_cost, 6)

    def event(self) -> Event:
        return {"type": "usage", "total": {**self.design.to_json(), "designCostUsd": round(self.design.cost_usd, 6),
                                           "criticCostUsd": round(self.critic_cost, 6), "cost": self.cost}}

    def fold_extra(self) -> bool:
        extra = self.turn.take_extra_usage()
        for usage in extra:
            self.critic_cost = round(self.critic_cost + float(usage.get("costUsd") or 0.0), 6)
        return bool(extra)


#: Provider events the caller gets as they are.
PASSED_THROUGH = ("status", "thinking", "slide", "error", "continuation")


def relay(ev: Event, reply: ReplyText, tally: Tally) -> Generator[Event, None, None]:
    if tally.fold_extra():  # the critic ran inside the last round's tool handling
        yield tally.event()
    piece = ""
    if ev["type"] == "text_start":
        piece = reply.start_block()
    elif ev["type"] == "text_end":
        piece = reply.end_block()
    elif ev["type"] == "text":
        piece = reply.feed(ev["text"])
    if piece:
        yield {"type": "text", "text": piece}
    if ev["type"] == "usage":
        round_usage = ev.get("round") or {}
        tally.design.add(Usage(int(round_usage.get("input") or 0), int(round_usage.get("output") or 0),
                               int(round_usage.get("cacheRead") or 0), int(round_usage.get("cacheWrite") or 0),
                               int(round_usage.get("reasoning") or 0), float(ev.get("cost") or 0.0)))
        yield tally.event()
    if ev["type"] in PASSED_THROUGH:
        yield ev


# --------------------------------------------------------------------------------------- the turn
def chat_turn(ctx: DesignContext, text: str, files: list[Path] | None = None, selected: str | None = None, *,
              shown: str | None = None, branch: list[JSON] | None = None) -> Generator[Event, None, DesignTurnResult]:
    """One design turn. `files` are attachments already in the deck (`DesignDeck.attach`); `shown` is
    what the transcript records when that is not `text` itself."""
    files = list(files or [])
    model = ctx.design_model
    record_request(ctx, shown or text, files)
    turn = new_turn(ctx, text, files, selected)
    reply = ReplyText(layout_labels(ctx.deck.meta()))
    tally = Tally(turn)
    result = DesignTurnResult(touched=turn.touched, reply="", model=model)
    turn_result: TurnResult | None = None
    try:
        yield {"type": "status", "text": "Reading your message…"}
        provider = ctx.provider(model)
        content = user_message(ctx, text, files, selected)
        messages = model_messages(ctx, branch, content)
        system = system_prompt.design_system(ctx.deck, registry.get(model).provider, brief_on=ctx.brief_on)
        events = loop.run_turn(provider, model=model, system=system, messages=messages,
                               tools=offered_tools(ctx.brief_on),
                               tool_handler=lambda call: dispatch_tool(ctx, turn, call),
                               effort=ctx.settings.llm_design_effort, attachments_root=ctx.deck.dir,
                               on_round=history_saver(ctx, branch), should_stop=ctx.should_stop,
                               settings=ctx.settings)
        while True:
            try:
                ev = next(events)
            except StopIteration as stop:
                turn_result = stop.value
                break
            yield from relay(ev, reply, tally)
        piece = reply.end_block()
        if piece:
            yield {"type": "text", "text": piece}
        if tally.fold_extra():
            yield tally.event()
    except Cancelled:
        tally.fold_extra()
        result.error = "Stopped."
        record_reply(ctx, reply.text(), turn, tally, model, stopped=True)
        raise
    except (ProviderError, ProviderUnavailable) as exc:
        tally.fold_extra()
        result.error = str(exc)
        yield {"type": "error", "text": str(exc)}
    except Exception as exc:  # noqa: BLE001 - any failure ends the turn with a reason, never a hang
        tally.fold_extra()
        _log.exception("design turn failed")
        result.error = f"The design turn failed ({type(exc).__name__})."
        yield {"type": "error", "text": result.error}
    if turn_result is not None:
        result.stop_reason, result.rounds = turn_result.stop_reason, turn_result.rounds
        result.continuations = turn_result.continuations
        result.error = result.error or turn_result.error
    result.reply = record_reply(ctx, reply.text(), turn, tally, model)
    result.design_cost_usd = round(tally.design.cost_usd, 6)
    result.critic_cost_usd = tally.critic_cost
    result.cost_usd = tally.cost
    result.usage = tally.event()["total"]
    result.reviews = list(turn.reviews)
    yield {"type": "done", "cost": result.cost_usd}
    return result


def record_reply(ctx: DesignContext, content: str, turn: Turn, tally: Tally, model: str, stopped: bool = False) -> str:
    """The reply as the transcript keeps it, with what the turn cost and the model that wrote it."""
    content = content.strip()
    if stopped:
        note = "(Stopped before finishing. Slides saved before the stop are kept.)"
        content = f"{content}\n\n{note}" if content else note
    entry: JSON = {"id": new_id("msg"), "role": "assistant", "content": content or "Done.",
                   "slides": list(turn.touched), "cost": tally.cost, "model": model, "createdAt": now()}
    if stopped:
        entry["stopped"] = True
    ctx.deck.add_transcript(entry)
    return str(entry["content"])


def run_design_turn(ctx: DesignContext, text: str, files: list[Path] | None = None, selected: str | None = None, *,
                    shown: str | None = None, branch: list[JSON] | None = None,
                    on_event: Callable[[Event], None] | None = None) -> DesignTurnResult:
    """`chat_turn` drained: the events go to `on_event` (if any) and the result is returned."""
    gen = chat_turn(ctx, text, files, selected, shown=shown, branch=branch)
    return drain(gen, on_event)


def drain(gen: Generator[Event, None, T], on_event: Callable[[Event], None] | None = None) -> T:
    while True:
        try:
            ev = next(gen)
        except StopIteration as stop:
            return cast(T, stop.value)
        if on_event is not None:
            on_event(ev)
