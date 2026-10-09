"""Unit tests of the design loop's parts, ported from Slide Studio's non-UI tests.

Sources: `server/tests/test_round2_tools.py` (edit_slide, the design review riding along, the critic's
rules and parsing), `test_chat.py` (the reply text, layout labels), `test_compat_spec.py` (spec mode),
`test_design_tools.py` (save results, previews). Rewritten onto the service's deck, context and fake
provider; the UI, Files API, sandbox and `/v1` route cases are dropped with those features.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.core.brand.workzone import Workzone
from app.core.design import critic, preview_tool, slide_tools
from app.core.design.context import DesignContext, DesignServices
from app.core.design.deck import DesignDeck
from app.core.design.layouts import layout_label, speak_of_layouts
from app.core.design.reply_text import ReplyText
from app.core.design.turn_state import Turn
from app.core.slides import spec as spec_mod
from app.core.slides.spec import BrandOptions
from tests.core.design.conftest import content_layout, png_bytes, slide_html, stub_services
from tests.fakes.llm import Round, ScriptedProvider, text

# ------------------------------------------------------------------------------------------ edits


def test_edits_apply_in_order_each_to_the_text_the_last_left():
    out = slide_tools.apply_edits("a b c", [{"find": "a", "replace": "x"}, {"find": "x b", "replace": "y"}])
    assert out == "y c"


@pytest.mark.parametrize(("edits", "said"), [
    ([{"find": "zzz", "replace": "q"}], "does not occur"),
    ([{"find": "a", "replace": "q"}], "occurs 2 times"),
    ([{"find": "b", "replace": "q"}, {"find": "b", "replace": "r"}], "edit 2 of 2"),
    ([], "non-empty list"),
    ([{"find": "", "replace": "q"}], "non-empty string"),
    ([{"find": "b", "replace": 3}], "must be a string"),
    ("not a list", "non-empty list"),
    ([{"find": "b", "replace": ""}] * 41, "too many"),
])
def test_bad_edits_fail_naming_why_and_change_nothing(edits: Any, said: str):
    with pytest.raises(ValueError, match=said):
        slide_tools.apply_edits("a b a", edits)


def ctx_for(deck: DesignDeck, services: DesignServices | None = None, provider: Any = None,
            **settings: Any) -> DesignContext:
    from tests.conftest import build_ai_settings
    from tests.fakes.llm import resolver

    return DesignContext(deck=deck, settings=build_ai_settings(**settings),
                         resolve=resolver(provider or ScriptedProvider()), services=services or stub_services())


def test_save_and_edit_share_one_save_path_with_lint_and_design_review(deck: DesignDeck):
    review = [{"level": "warn", "rule": "stack-gap", "message": "a 60 px hole", "element": "Revenue"}]
    ctx = ctx_for(deck, stub_services(review))
    turn = Turn("make it")
    answer, event = slide_tools.save_slide(ctx, turn, "t1", {"title": "Revenue", "layout_id": content_layout(deck),
                                                            "html": slide_html(), "source": "Source: filings"})
    said = answer["content"]
    assert said.splitlines()[0].startswith("Saved sld_") and "Export lint" in said
    assert "Design review (measured layout" in said and "stack-gap [Revenue]" in said
    assert event and event["type"] == "slide"
    edited, _ = slide_tools.edit_slide(ctx, turn, "t2", {"slide_id": event["slideId"],
                                                         "edits": [{"find": "top:150px", "replace": "top:160px"}]})
    assert "version 2" in edited["content"] and "Design review" in edited["content"]
    assert turn.touched == [event["slideId"]], "saved twice in one turn: one slide"


def test_a_source_line_missing_from_the_html_is_pointed_out(deck: DesignDeck):
    answer, _ = slide_tools.save_slide(ctx_for(deck), Turn("x"), "t", {
        "title": "R", "layout_id": content_layout(deck), "html": slide_html(), "source": "Source: a survey of 40 firms"})
    assert "not visible text in the HTML" in answer["content"]


def test_a_throwing_reviewer_never_fails_the_save_and_none_means_absent(deck: DesignDeck):
    def boom(_d: DesignDeck, _s: str) -> list[dict[str, Any]]:
        raise RuntimeError("browser gone")

    from app.core import engine_service

    throwing = DesignServices(lint=engine_service.readiness, review=boom, render=None)
    answer, _ = slide_tools.save_slide(ctx_for(deck, throwing), Turn("x"), "t", {
        "title": "R", "layout_id": content_layout(deck), "html": slide_html()})
    assert "Design review: not checked (browser gone)" in answer["content"]
    absent = DesignServices(lint=engine_service.readiness, review=None, render=None)
    answer, _ = slide_tools.save_slide(ctx_for(deck, absent), Turn("x"), "t", {
        "title": "R", "layout_id": content_layout(deck), "html": slide_html()})
    assert "Design review" not in answer["content"]


def test_a_slide_too_large_is_refused(deck: DesignDeck):
    with pytest.raises(ValueError, match="must stay under"):
        slide_tools.save_slide(ctx_for(deck), Turn("x"), "t", {"title": "R", "layout_id": content_layout(deck),
                                                             "html": "<p>" + "x" * 400_001})


def test_get_component_answers_the_catalog_and_an_unknown_name_with_the_catalog():
    catalog = slide_tools.component_result("t", {"name": "catalog"})
    assert not catalog.get("is_error") and catalog["content"]
    unknown = slide_tools.component_result("t", {"name": "no-such-component"})
    assert unknown["is_error"] is True and "catalog" in unknown["content"]


# ---------------------------------------------------------------------------------------- preview


def test_preview_without_a_renderer_says_unavailable_and_still_lints(deck: DesignDeck):
    from app.core import engine_service

    sid = deck.save_slide(slide_html(), "R", content_layout(deck), "seed")["id"]
    ctx = ctx_for(deck, DesignServices(lint=engine_service.readiness, review=None, render=None))
    answer = preview_tool.preview_result(ctx, "t", sid, Turn("x"))
    assert answer["is_error"] is True and "Preview unavailable" in answer["content"] and "Export lint" in answer["content"]


def test_a_preview_picture_is_downscaled_jpeg():
    media, data, size = preview_tool.preview_image(png_bytes((2560, 1440)))
    assert media == "image/jpeg" and size == (1280, 720) and data


def test_the_same_version_previewed_twice_is_reviewed_once(deck: DesignDeck):
    sid = deck.save_slide(slide_html(), "R", content_layout(deck), "seed")["id"]
    provider = ScriptedProvider(completions=[Round(text("CHECKLIST\nFIXES\nnone\nVERDICT: PASS"))])
    ctx = ctx_for(deck, provider=provider)
    turn = Turn("x")
    first = preview_tool.preview_result(ctx, "t1", sid, turn)
    second = preview_tool.preview_result(ctx, "t2", sid, turn)
    assert len(provider.completion_requests) == 1
    assert first["content"][-1]["text"] == second["content"][-1]["text"]
    assert "PASS" in first["content"][-1]["text"]


def test_a_stop_before_the_critic_starts_no_critic_call(deck: DesignDeck):
    sid = deck.save_slide(slide_html(), "R", content_layout(deck), "seed")["id"]
    provider = ScriptedProvider()
    ctx = ctx_for(deck, provider=provider)
    ctx.should_stop = lambda: True
    preview_tool.preview_result(ctx, "t", sid, Turn("x"))
    assert provider.completion_requests == []


def test_a_failing_critic_leaves_the_preview_standing(deck: DesignDeck):
    sid = deck.save_slide(slide_html(), "R", content_layout(deck), "seed")["id"]

    class Broken(ScriptedProvider):
        def complete(self, request: Any) -> Any:
            raise RuntimeError("outage")

    answer = preview_tool.preview_result(ctx_for(deck, provider=Broken()), "t", sid, Turn("x"))
    assert [b["type"] for b in answer["content"]] == ["image", "text"], "no review, the picture and findings stand"


# ----------------------------------------------------------------------------------------- critic


def test_the_critic_helpers_parse_and_trim():
    said = ("CHECKLIST\n1. YES\n2. NO: overlap\n3. YES\nFIXES\n" + "\n".join(f"{n}. [must] \"x\": y -> z"
                                                                         for n in range(1, 8)) + "\nVERDICT: FIX")
    trimmed = critic._trim(said)
    assert "1. YES" not in trimmed and "2. NO: overlap" in trimmed
    assert "6. [must]" not in trimmed, "at most five fixes"
    assert critic._passed("VERDICT: PASS") and not critic._passed(said)
    assert critic._trim("CHECKLIST\n1. YES\nFIXES\nnone\nVERDICT: PASS").startswith("CHECKLIST: all YES")


def test_the_critic_prompt_keeps_its_rules():
    assert f"NO on item {critic.AI_LOOK_ITEM} is a [must]" in critic.SYSTEM
    assert "Never ask to remove a pill" in critic.SYSTEM
    assert critic.COMPOSITION_ITEM != critic.CANVAS_ITEM
    assert "furniture" in critic.CHECKLIST[-2]


def test_the_critic_is_skipped_without_a_provider_or_when_off():
    from tests.conftest import build_ai_settings

    assert critic.critique(None, "claude-sonnet-5-5", "abc", "", "", {}, build_ai_settings()) is None
    provider = ScriptedProvider()
    off = build_ai_settings(design_critic_enabled=False)
    assert critic.critique(provider, "claude-sonnet-5-5", "abc", "", "", {}, off) is None
    assert provider.completion_requests == []


# --------------------------------------------------------------------------------- reply and labels


def test_text_blocks_are_joined_with_a_blank_line_and_layout_ids_are_named():
    reply = ReplyText({"layout-05": "“Title only” (Layout 5)"})
    out = reply.start_block() + reply.feed("Put it on layout") + reply.feed("-05 now.") + reply.end_block()
    out += reply.start_block() + reply.feed("Second.") + reply.end_block()
    assert out == reply.text() == "Put it on “Title only” (Layout 5) now.\n\nSecond."
    assert speak_of_layouts("see LAYOUT-05 and layout-01.png", {"layout-05": "L5"}) == "see L5 and layout-01.png"


def test_layout_label_is_the_name_and_number():
    meta = {"master": {"layouts": [{"id": "layout-05", "name": "Title only"}]}}
    assert layout_label(meta, "layout-05") == "“Title only” (Layout 5)"
    assert layout_label(meta, "layout-99") is None
    assert layout_label({}, "layout-02") == "layout-02"


# ------------------------------------------------------------------------------------------- spec


def test_normalize_accepts_a_bare_slide_and_needs_a_title():
    spec = spec_mod.normalize_spec({"title": " Revenue  grew ", "bullets": ["a", "", 3], "type": "Data"})
    assert spec["slide"]["title"] == "Revenue grew" and spec["slide"]["bullets"] == ["a", "3"]
    assert spec["slide"]["type"] == "data" and spec["deck"]["keyMessages"] == []
    with pytest.raises(ValueError):
        spec_mod.normalize_spec({"slide": {"title": "  "}})


def test_title_and_divider_types_pick_their_layouts():
    assert spec_mod.spec_layout_archetype(spec_mod.normalize_spec({"title": "x", "type": "title"})) == "cover"
    assert spec_mod.spec_layout_archetype(spec_mod.normalize_spec({"title": "x", "type": "divider"})) == "divider"
    assert spec_mod.spec_layout_archetype(spec_mod.normalize_spec({"title": "x", "type": "kpi"})) == "content"


def test_the_content_instruction_carries_the_whole_spec_and_the_workzone():
    spec = spec_mod.normalize_spec({"slide": {"title": "Costs fell 4%", "type": "data", "framework": "2x2",
                                              "description": "show the drivers", "bullets": ["Energy -9%"]},
                                    "deck": {"audience": "board", "keyMessages": ["focus"]}})
    said = spec_mod.spec_instruction(spec, BrandOptions(primary_color="#123456", workzone=Workzone(0.05, 0.2, 0.9, 0.7)),
                                     "content", 1280, 720, brief_on=False)
    for piece in ("Costs fell 4%", "Framework: 2x2 matrix", "show the drivers", "- Energy -9%", "Audience: board",
                  "Key messages: focus", "WORKZONE CONSTRAINT", "#123456", "header band"):
        assert piece in said, piece
    assert "Pre-filled brief" not in said
    dense = spec_mod.spec_instruction(spec, BrandOptions(), "content", 1280, 720, dense=True)
    assert "estimate" in dense and "Pre-filled brief" in dense


def test_unknown_archetype_and_framework_degrade_quietly():
    spec = spec_mod.normalize_spec({"title": "x", "framework": "made-up thing", "archetypeId": "nope-99"})
    said = spec_mod.spec_instruction(spec, BrandOptions(), "content", 1280, 720, brief_on=False)
    assert "Framework: made-up thing" in said and "Layout recipe" not in said


def test_cover_is_heading_only_and_divider_uses_the_section():
    cover = spec_mod.spec_instruction(spec_mod.normalize_spec({"title": "Growth", "type": "title"}),
                                      BrandOptions(date="Oct 2026"), "cover", 1280, 720)
    assert "No bullets" in cover and "Date line: Oct 2026." in cover
    divider = spec_mod.spec_instruction(spec_mod.normalize_spec({"title": "x", "type": "divider", "section": "Costs"}),
                                        BrandOptions(), "divider", 1280, 720)
    assert "The slide's title is: Costs." in divider


# ------------------------------------------------------------------------------------- the deck


def test_the_deck_writes_only_inside_its_directory(tmp_path: Path, make_ctx: Callable[..., DesignContext]):
    deck = DesignDeck.create(tmp_path / "d", "Deck")
    with pytest.raises(KeyError):
        deck.slide_path("../../evil", 1)
    stored = deck.attach("../../evil name.png", b"x")
    assert stored.parent == deck.attachments and stored.name == "evil_name.png"
    again = deck.attach("evil name.png", b"y")
    assert again.name == "evil_name-2.png"
    with pytest.raises(FileExistsError):
        DesignDeck.create(tmp_path / "d", "Again")


def test_pdf_attachments_ground_a_brief(tmp_path: Path):
    from pypdf import PdfWriter

    from app.core.design import briefs

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    pdf = tmp_path / "facts.pdf"
    pdf.write_bytes(buf.getvalue())
    assert briefs.attachment_text(pdf) == "", "a PDF with no text reads as empty, never an error"
    notes = tmp_path / "facts.md"
    notes.write_text("Revenue grew 23%", encoding="utf-8")
    assert briefs.attachment_text(notes) == "Revenue grew 23%"
