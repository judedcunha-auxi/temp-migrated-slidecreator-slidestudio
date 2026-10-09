"""The storyline draft end to end against the scripted model: a standard deck, a dense deck, Arabic input,
validation failures, refusals. Darwin `anthropic.test.ts` (generateStoryline) and Slide Studio's draft tests."""

from __future__ import annotations

import pytest

from app.config.storyline import StorylineSettings
from app.core.storyline import prompts
from app.core.storyline.models import StorylineInputs
from app.core.storyline.ports import ModelUnavailable
from app.core.storyline.service import StorylineRefused, draft_storyline, revalidate
from app.core.storyline.validation import INCOMPLETE_STORYLINE, StorylineValidationError
from tests.core.storyline.conftest import answer, furniture, long_deck, slide
from tests.fakes.storyline_model import FAKE_COST, ScriptedStorylineModel, reply

WIZARD = {"topic": "Cloud consolidation", "company": "Acme", "numSlides": 9, "audience": "Board", "style": "Executive strategy"}


@pytest.mark.asyncio
async def test_a_standard_deck_is_drafted_validated_repaired_and_sectioned(settings: StorylineSettings):
    deck = long_deck()
    deck[3] = slide(4, section="Diagnosis", framework="radar chart", type="comparison")
    model = ScriptedStorylineModel(answer(*deck))
    result = await draft_storyline(model, StorylineInputs.from_wizard(WIZARD), settings=settings)

    assert model.calls == 1
    request = model.requests[0]
    assert (request.purpose, request.model, request.max_tokens, request.effort) == (
        "storyline", "claude-sonnet-5-5", 64_000, "high")
    assert request.schema == prompts.storyline_schema("standard")
    assert request.system == prompts.storyline_system("standard", "en")
    assert request.messages[0]["role"] == "user" and "Create a 9-slide" in request.messages[0]["content"]

    st = result.storyline
    assert st.presentation_title == "Consolidate to one cloud"
    assert [s.type for s in st.slides].count("divider") == 2 and len(st.slides) == 11
    assert [s.number for s in st.slides] == list(range(1, 12))
    assert st.slides[3].framework == "weighted scoring matrix"
    assert result.warnings and result.warnings[0].startswith("Slide 4:")
    assert result.usage.cost_usd == FAKE_COST and st.executive_summary is None


@pytest.mark.asyncio
async def test_a_dense_collection_keeps_six_bullets_and_its_chart_and_gets_no_dividers(settings: StorylineSettings):
    chart = {"categories": ["FY24", "FY25"], "series": [{"name": "Opex", "values": [12, "9.5"]}]}
    body = [slide(i, section="A" if i < 3 else "B", bullets=tuple("abcdef"), framework="icon card grid",
                  chartData=chart if i == 1 else None) for i in range(1, 5)]
    body = [{k: v for k, v in s.items() if v is not None} for s in body]
    model = ScriptedStorylineModel(answer(*body, executiveSummary="Costs fall; act now."))
    inputs = StorylineInputs.from_wizard({**WIZARD, "mode": "collection", "density": "dense", "numSlides": 4,
                                          "styleInstructions": "Navy and teal"})
    result = await draft_storyline(model, inputs, settings=settings)

    request = model.requests[0]
    assert request.system == prompts.storyline_system("dense", "en")
    assert "executiveSummary" in request.schema["required"]
    user = request.messages[0]["content"]
    assert "Exactly 4 body slides" in user and "Style instructions: Navy and teal" in user
    st = result.storyline
    assert len(st.slides) == 4 and all(len(s.bullets) == 6 for s in st.slides)
    assert st.slides[0].chart_data is not None and st.slides[0].chart_data.series[0].values == [12, 9.5]
    assert st.executive_summary == "Costs fall; act now."


@pytest.mark.asyncio
async def test_a_dense_deck_is_clamped_to_its_mode_and_sectioned(settings: StorylineSettings):
    model = ScriptedStorylineModel(answer(*long_deck()))
    inputs = StorylineInputs.from_wizard({**WIZARD, "mode": "deck", "density": "dense", "numSlides": 99})
    assert inputs.num_slides == 30
    result = await draft_storyline(model, inputs, settings=settings)
    assert sum(1 for s in result.storyline.slides if s.type == "divider") == 2
    assert result.storyline.slides[1].bullets == ["Diagnosis", "Options", "Plan"]  # the agenda lists the sections


@pytest.mark.asyncio
async def test_arabic_input_sends_the_arabic_prompt_and_keeps_the_arabic_copy(settings: StorylineSettings):
    arabic = answer(furniture(1, "title", "التشخيص"),
                    slide(2, title="ثلاث فجوات هيكلية ترفع تكلفتنا بنسبة 23%", section="التشخيص",
                          bullets=("الإيرادات 12 مليون $", "النمو 15%", "EBITDA 4.8x")),
                    title="توحيد السحابة")
    model = ScriptedStorylineModel(arabic)
    result = await draft_storyline(model, StorylineInputs.from_wizard({**WIZARD, "language": "ar"}), settings=settings)

    system = model.requests[0].system
    assert system == prompts.storyline_system("standard", "ar") and "ARABIC OUTPUT" in system
    assert result.storyline.presentation_title == "توحيد السحابة"
    assert result.storyline.slides[1].bullets[2] == "EBITDA 4.8x"
    # English decks never see the directive.
    english = ScriptedStorylineModel(answer(slide()))
    await draft_storyline(english, StorylineInputs.from_wizard(WIZARD), settings=settings)
    assert "ARABIC" not in english.requests[0].system


@pytest.mark.asyncio
async def test_an_invalid_answer_is_a_validation_error_with_darwins_message(settings: StorylineSettings):
    model = ScriptedStorylineModel(answer(slide(bullets=("one",))))
    with pytest.raises(StorylineValidationError) as caught:
        await draft_storyline(model, StorylineInputs.from_wizard(WIZARD), settings=settings)
    assert str(caught.value) == INCOMPLETE_STORYLINE and caught.value.issues


@pytest.mark.asyncio
async def test_a_reply_cut_off_at_max_tokens_is_incomplete(settings: StorylineSettings):
    model = ScriptedStorylineModel(reply(None, stop_reason="max_tokens"))
    with pytest.raises(StorylineValidationError, match="incomplete storyline"):
        await draft_storyline(model, StorylineInputs.from_wizard(WIZARD), settings=settings)


@pytest.mark.asyncio
async def test_a_refusal_is_its_own_error(settings: StorylineSettings):
    model = ScriptedStorylineModel(reply(None, stop_reason="refusal"))
    with pytest.raises(StorylineRefused):
        await draft_storyline(model, StorylineInputs.from_wizard(WIZARD), settings=settings)


@pytest.mark.asyncio
async def test_a_model_outage_propagates(settings: StorylineSettings):
    model = ScriptedStorylineModel(ModelUnavailable("overloaded"))
    with pytest.raises(ModelUnavailable):
        await draft_storyline(model, StorylineInputs.from_wizard(WIZARD), settings=settings)


@pytest.mark.asyncio
async def test_settings_pick_the_model_budget_and_cap(settings: StorylineSettings):
    custom = settings.model_copy(update={"model": "claude-opus-5-5", "max_tokens": 32_000, "effort": "medium",
                                         "max_slides": 3})
    model = ScriptedStorylineModel(answer(*(slide(i) for i in range(1, 6))))
    result = await draft_storyline(model, StorylineInputs.from_wizard({**WIZARD, "numSlides": 5}, max_slides=3),
                                   settings=custom)
    r = model.requests[0]
    assert (r.model, r.max_tokens, r.effort) == ("claude-opus-5-5", 32_000, "medium")
    assert "Create a 3-slide" in r.messages[0]["content"] and len(result.storyline.slides) == 3


def test_an_edited_storyline_is_revalidated_without_dividers_coming_back():
    st, _ = revalidate(answer(*long_deck()))
    assert len(st.slides) == 9
    with pytest.raises(StorylineValidationError):
        revalidate(answer(slide(bullets=())))


def test_inputs_from_the_wizard_are_lenient():
    inputs = StorylineInputs.from_wizard({"topic": "x", "numSlides": "abc", "language": "fr", "mode": "nope",
                                          "keyMessages": ["a", 3, " "], "brandId": "b1"})
    assert (inputs.num_slides, inputs.language, inputs.mode, inputs.density) == (12, "en", "auto", "standard")
    assert inputs.key_messages == ["a"] and inputs.brand_id == "b1"
    assert StorylineInputs.from_wizard({"topic": "x", "numSlides": 500}).num_slides == 500  # Darwin: no cap
    assert StorylineInputs.from_wizard({"topic": "x", "mode": "single", "numSlides": 9}).num_slides == 1
