"""The design loop on real renders: the preview picture, the measured design review and the critic loop.

Everything but the model is real here: Chromium renders the slide over its layout (`app.core.preview`),
the design lint measures it, the workzone lint checks it, and the critic (a fake provider) is handed
the real picture. One module, so the browsers start once.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator

import pytest
from PIL import Image

from app.core import engine_service, preview
from app.core.design import design_turn
from app.core.design.context import DesignContext, default_services
from app.core.design.deck import DesignDeck
from tests.core.design.conftest import content_layout, slide_html
from tests.core.design.test_design_turn import CRITIC_FIX, CRITIC_PASS
from tests.fakes.llm import Round, ScriptedProvider, last_tool_results, result_text, saved_slide_id, text, tool


@pytest.fixture(scope="module", autouse=True)
def close_browsers() -> Iterator[None]:
    yield
    preview.shutdown()
    engine_service.shutdown()


def test_a_slide_renders_over_its_layout_at_canvas_size(deck: DesignDeck):
    sid = deck.save_slide(slide_html(), "Revenue", content_layout(deck), "seed")["id"]
    png = preview.render_slide_png(deck, sid)
    with Image.open(io.BytesIO(png)) as image:
        assert image.size == (1280, 720)
        assert image.convert("L").getextrema()[0] < 128, "the title's dark text is in the picture"
    with pytest.raises(preview.SlidePreviewError):
        preview.render_slide_png(deck, "sld_0000000000")


def test_the_design_review_measures_the_workzone_and_header_band(deck: DesignDeck):
    sid = deck.save_slide(slide_html(top=40), "Revenue", content_layout(deck), "seed")["id"]
    deck.update(lambda m: m.update(workzone={"left": 0.05, "top": 0.2, "width": 0.9, "height": 0.7}))
    rules = {f["rule"] for f in preview.design_review(deck, sid)}
    assert "header-band-overlap" in rules, "the body painted over the header band"
    fixed = deck.save_slide(slide_html(top=170), "Revenue", content_layout(deck), "fix", sid=sid)
    assert fixed["current"] == 2
    rules = {f["rule"] for f in preview.design_review(deck, sid)}
    assert "header-band-overlap" not in rules and "workzone-overflow" not in rules


def test_the_critic_loop_on_real_renders(deck: DesignDeck, make_ctx: Callable[..., DesignContext]):
    layout = content_layout(deck)
    provider = ScriptedProvider(
        [Round(tool("save_slide", {"title": "Revenue", "layout_id": layout, "html": slide_html()})),
         lambda req: Round(tool("preview_slide", {"slide_id": saved_slide_id(req)})),
         lambda req: Round(tool("edit_slide", {"slide_id": saved_slide_id(req),
                                               "edits": [{"find": "top:150px", "replace": "top:170px"}]})),
         lambda req: Round(tool("preview_slide", {"slide_id": saved_slide_id(req)})),
         Round(text("Done."))],
        completions=[Round(text(CRITIC_FIX)), Round(text(CRITIC_PASS))])
    ctx = make_ctx(deck, provider, services=default_services())
    result = design_turn.run_design_turn(ctx, "A revenue slide")

    assert [passed for _sid, passed in result.reviews] == [False, True]
    preview_answer = last_tool_results(provider.requests[2])[0]
    image = preview_answer["content"][0]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/jpeg"
    assert "Design review" in preview_answer["content"][1]["text"], "the measured review rides along"
    critic_picture = provider.completion_requests[0].messages[0]["content"][0]
    assert critic_picture["source"]["data"] == image["source"]["data"], "the critic sees the real render"
    saved_answer = result_text(last_tool_results(provider.requests[1])[0])
    assert "Export lint" in saved_answer and "Design review" in saved_answer
