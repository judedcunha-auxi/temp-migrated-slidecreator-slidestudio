"""The design turn end to end on the fake provider: design, HTML refine, the brief, the critic loop.

No browser: the export lint is the real static linter, the design review and the preview picture
are stubs (tests/core/design/conftest.py). The model is `ScriptedProvider`, replaying tool calls on
the ids the tools answered with. `test_e2e_browser.py` repeats the critic loop on real renders.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.core.design import design_turn, prompts, tool_schemas
from app.core.design.context import DesignContext
from app.core.design.deck import DesignDeck
from app.core.llm import pricing
from app.core.llm.types import RoundRequest, Usage
from tests.core.design.conftest import content_layout, slide_html, stub_services
from tests.fakes.llm import Round, ScriptedProvider, last_tool_results, result_text, saved_slide_id, text, tool

CRITIC_FIX = """CHECKLIST
2. NO: the title overlaps the body
FIXES
1. [must] "Revenue grew": the body starts under the title -> move the body block down to top 170px
VERDICT: FIX"""
CRITIC_PASS = """CHECKLIST
FIXES
none
VERDICT: PASS"""


def save(deck: DesignDeck, title: str = "Revenue grew 23% on three markets", **extra: Any) -> dict[str, Any]:
    return tool("save_slide", {"title": title, "layout_id": content_layout(deck), "html": slide_html(title),
                               "summary": "First version", **extra})


def test_a_design_turn_saves_a_slide_lints_it_and_prices_the_turn(deck: DesignDeck,
                                                                   make_ctx: Callable[..., DesignContext]):
    provider = ScriptedProvider([Round(save(deck, source="Source: company filings")),
                                 Round(text("I designed the revenue slide."))])
    ctx = make_ctx(deck, provider)
    events: list[dict[str, Any]] = []
    result = design_turn.run_design_turn(ctx, "A slide on revenue growth", on_event=events.append)

    assert result.error is None and result.stop_reason == "end_turn"
    assert len(result.touched) == 1
    sid = result.touched[0]
    assert deck.read_slide(sid).startswith("<!doctype html>")
    assert deck.find_slide(deck.meta(), sid)["versions"][0]["sourceLine"] == "Source: company filings"
    answer = result_text(last_tool_results(provider.requests[1])[0])
    assert answer.startswith(f"Saved {sid} version 1.") and "Export lint" in answer
    assert any(e["type"] == "slide" and e["slideId"] == sid for e in events)

    per_round = pricing.cost_of(Usage(1000, 200), "claude-opus-5-5")
    assert result.cost_usd == pytest.approx(2 * per_round) == pytest.approx(result.design_cost_usd)
    transcript = deck.transcript()
    assert transcript[-1]["role"] == "assistant" and transcript[-1]["cost"] == pytest.approx(result.cost_usd)
    assert transcript[-1]["slides"] == [sid]
    assert deck.history()[-1]["role"] == "assistant", "the model's memory is kept in the deck"


def test_the_request_carries_the_system_prompt_tools_and_layout_pictures(deck: DesignDeck,
                                                                          make_ctx: Callable[..., DesignContext]):
    provider = ScriptedProvider([Round(text("ok"))])
    design_turn.run_design_turn(make_ctx(deck, provider), "hello")
    request = provider.requests[0]
    names = [t["name"] for t in request.tools]
    assert names[:2] == ["save_slide", "read_slide"] and "write_brief" in names and "preview_slide" in names
    assert "delete_slide" not in names
    assert "get_exemplars" not in names, "no exemplar pictures ship (D3)"
    assert "1280×720" in request.system and "/api/projects/" in request.system
    first = request.messages[0]["content"]
    assert first[0]["text"].startswith("Reference: the master layouts")
    assert any(b.get("type") == "image" for b in first), "layout pictures go first, for the cache"
    assert request.model == "claude-opus-5-5" and request.effort == "high"


def test_html_refine_edits_the_existing_slide_as_a_new_version(deck: DesignDeck,
                                                               make_ctx: Callable[..., DesignContext]):
    layout = content_layout(deck)
    first = deck.save_slide(slide_html(), "Revenue", layout, "seed")
    sid = first["id"]

    def edit(_req: RoundRequest) -> Round:
        return Round(tool("edit_slide", {"slide_id": sid, "summary": "Moved the body down",
                                         "edits": [{"find": "top:150px", "replace": "top:170px"}]}))

    provider = ScriptedProvider([edit, Round(text("Moved the body down."))])
    result = design_turn.run_design_turn(make_ctx(deck, provider), "Give the body more room", selected=sid)

    slide = deck.find_slide(deck.meta(), sid)
    assert slide["current"] == 2 and result.touched == [sid]
    assert "top:170px" in deck.read_slide(sid) and "top:150px" in deck.read_slide(sid, 1), "v1 is kept"
    assert slide["versions"][1]["summary"] == "Moved the body down"
    workspace = provider.requests[0].messages[-1]["content"][0]["text"]
    assert "← selected" in workspace


def test_a_failed_edit_saves_nothing_and_the_model_is_told_why(deck: DesignDeck,
                                                               make_ctx: Callable[..., DesignContext]):
    sid = deck.save_slide(slide_html(), "Revenue", content_layout(deck), "seed")["id"]
    provider = ScriptedProvider([
        Round(tool("edit_slide", {"slide_id": sid, "edits": [{"find": "not in the slide", "replace": "x"}]})),
        Round(text("Sorry.")),
    ])
    design_turn.run_design_turn(make_ctx(deck, provider), "edit it", selected=sid)
    answer = last_tool_results(provider.requests[1])[0]
    assert answer["is_error"] is True and "does not occur" in answer["content"]
    assert deck.find_slide(deck.meta(), sid)["current"] == 1


def test_the_critic_loop_reviews_fixes_and_passes(deck: DesignDeck, make_ctx: Callable[..., DesignContext]):
    """save -> preview (critic: FIX) -> edit_slide -> preview (critic: PASS, compared with the first)."""
    provider = ScriptedProvider(
        [Round(save(deck)),
         lambda req: Round(tool("preview_slide", {"slide_id": saved_slide_id(req)})),
         lambda req: Round(tool("edit_slide", {"slide_id": saved_slide_id(req),
                                               "edits": [{"find": "top:150px", "replace": "top:170px"}]})),
         lambda req: Round(tool("preview_slide", {"slide_id": saved_slide_id(req)})),
         Round(text("Fixed what the review found; it passes now."))],
        completions=[Round(text(CRITIC_FIX), usage=Usage(3000, 300)), Round(text(CRITIC_PASS), usage=Usage(3500, 100))])
    result = design_turn.run_design_turn(make_ctx(deck, provider), "A revenue slide")

    assert [r[1] for r in result.reviews] == [False, True]
    first_review = provider.completion_requests[0]
    assert first_review.model == "claude-sonnet-5-5" and first_review.minimal_thinking
    assert first_review.effort == "low"
    second = provider.completion_requests[1].messages[0]["content"]
    assert sum(1 for b in second if b["type"] == "image") == 2, "the new render is judged against the first"
    assert "Previous review" in second[-1]["text"]
    preview_answer = last_tool_results(provider.requests[2])[0]
    kinds = [b["type"] for b in preview_answer["content"]]
    assert kinds == ["image", "text", "text"] and "Independent review" in preview_answer["content"][2]["text"]

    critic = pricing.cost_of(Usage(3000, 300), "claude-sonnet-5-5") + pricing.cost_of(Usage(3500, 100),
                                                                                       "claude-sonnet-5-5")
    assert result.critic_cost_usd == pytest.approx(critic)
    assert result.cost_usd == pytest.approx(result.design_cost_usd + critic), "the critic's calls are in the total"


def test_the_critic_is_limited_per_slide_and_skipped_when_off(deck: DesignDeck,
                                                              make_ctx: Callable[..., DesignContext]):
    def preview_again(req: RoundRequest) -> Round:
        return Round(tool("preview_slide", {"slide_id": saved_slide_id(req)}))

    def edit_again(n: int) -> Callable[[RoundRequest], Round]:
        return lambda req: Round(tool("edit_slide", {"slide_id": saved_slide_id(req),
                                                     "edits": [{"find": f"top:{150 + n}px", "replace": f"top:{151 + n}px"}]}))

    script: list[Any] = [Round(save(deck))]
    for n in range(3):
        script += [preview_again, edit_again(n)]
    script += [preview_again, Round(text("done"))]
    provider = ScriptedProvider(script, completions=[Round(text(CRITIC_FIX)), Round(text(CRITIC_FIX))])
    design_turn.run_design_turn(make_ctx(deck, provider), "go")
    assert len(provider.completion_requests) == 2, "at most two reviews per slide per turn"
    last_preview = result_text(last_tool_results(provider.requests[-1])[0])
    assert "Review limit reached" in last_preview

    off = ScriptedProvider([Round(save(deck)), preview_again, Round(text("ok"))])
    design_turn.run_design_turn(make_ctx(deck, off, design_critic_enabled=False), "go")
    assert off.completion_requests == []


def test_the_brief_is_validated_repaired_and_tied_to_the_slide(deck: DesignDeck,
                                                               make_ctx: Callable[..., DesignContext]):
    bad = {"slide": "Revenue", "action_title": "Revenue grew {f1} in 2025", "audience": "board",
           "objective": "show growth", "facts": [{"id": "f1", "value": "99%", "label": "growth", "source": "user"}],
           "zones": [{"id": "z1", "tier": "hero", "role": "proof", "exhibit": {"kind": "chart", "chart": "column"}}]}
    good = {**bad, "facts": [{"id": "f1", "value": "23%", "label": "growth", "source": "user"}]}
    provider = ScriptedProvider([Round(tool("write_brief", bad)), Round(tool("write_brief", good)),
                                 Round(save(deck, title="Revenue")), Round(text("Done."))])
    result = design_turn.run_design_turn(make_ctx(deck, provider), "Revenue grew 23% in 2025; make the slide.")

    first, second = (last_tool_results(provider.requests[i])[0] for i in (1, 2))
    assert first.get("is_error") is True and "failed validation" in first["content"]
    assert not second.get("is_error") and "data-exhibit-plan" in second["content"]
    sid = result.touched[0]
    record = deck.find_slide(deck.meta(), sid)["brief"]
    assert record["version"] == 1 and record["brief"]["action_title"].startswith("Revenue grew")
    saved_answer = result_text(last_tool_results(provider.requests[3])[0])
    assert "Grounding check" in saved_answer


def test_a_save_cut_off_by_max_tokens_is_reissued_not_run(deck: DesignDeck, make_ctx: Callable[..., DesignContext]):
    cut = tool("save_slide", {"title": "Revenue", "layout_id": content_layout(deck), "html": "<!doctype html><di"})
    provider = ScriptedProvider([Round(cut, stop="max_tokens"), Round(save(deck)), Round(text("Saved."))])
    events: list[dict[str, Any]] = []
    result = design_turn.run_design_turn(make_ctx(deck, provider), "go", on_event=events.append)
    assert result.continuations == 1 and len(deck.slides()) == 1, "only the complete save ran"
    assert any(e["type"] == "continuation" for e in events)
    assert "cut off" in last_tool_results(provider.requests[1])[0]["content"]


def test_a_tool_error_is_an_answer_and_an_unknown_layout_is_refused(deck: DesignDeck,
                                                                    make_ctx: Callable[..., DesignContext]):
    provider = ScriptedProvider([
        Round(tool("save_slide", {"title": "x", "layout_id": "layout-99", "html": "<p>x</p>"}),
              tool("read_slide", {"slide_id": "sld_0000000000"}), tool("nope", {}),
              tool("find_layout_reference", {"query": "four-phase rollout"})),
        Round(text("ok"))])
    design_turn.run_design_turn(make_ctx(deck, provider), "go")
    answers = last_tool_results(provider.requests[1])
    assert [bool(a.get("is_error")) for a in answers] == [True, True, True, False]
    assert "unknown layout_id" in answers[0]["content"]
    assert "unknown tool" in answers[2]["content"]


def test_a_provider_that_is_unavailable_ends_the_turn_with_a_reason(deck: DesignDeck):
    from app.core.llm.types import ProviderUnavailable
    from tests.conftest import build_ai_settings

    def resolve(_model: str) -> Any:
        raise ProviderUnavailable("ANTHROPIC_API_KEY is not configured.")

    ctx = DesignContext(deck=deck, settings=build_ai_settings(), resolve=resolve, services=stub_services())
    result = design_turn.run_design_turn(ctx, "go")
    assert result.error and "ANTHROPIC_API_KEY" in result.error and result.touched == []


def test_the_prompt_names_no_client_and_offers_the_brief_only_when_on():
    template = prompts.design_system_template(False)
    assert "EYP" not in template and "Slide Studio" not in template
    names_on = [t["name"] for t in tool_schemas.offered_tools(True)]
    names_off = [t["name"] for t in tool_schemas.offered_tools(False)]
    assert "write_brief" in names_on and "write_brief" not in names_off
    assert names_on.index("write_brief") == names_on.index("plan_exhibit") - 1
