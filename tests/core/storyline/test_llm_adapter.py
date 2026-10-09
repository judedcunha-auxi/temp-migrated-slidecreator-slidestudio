"""core/storyline/llm_adapter: StructuredRequest -> one provider round (scripted provider; nothing is paid)."""

from __future__ import annotations

import json

import pytest

from app.core.llm.types import ProviderError, ProviderUnavailable, Usage
from app.core.storyline.llm_adapter import LlmStorylineModel
from app.core.storyline.ports import ModelRejected, ModelUnavailable, StructuredRequest
from tests.fakes.llm import Round, ScriptedProvider, text

pytestmark = pytest.mark.asyncio

REQUEST = StructuredRequest(purpose="intake", model="claude-sonnet-5-5", system="sys",
                            messages=[{"role": "user", "content": "hi"}], schema_name="intake_turn",
                            schema={"type": "object", "additionalProperties": False}, max_tokens=4096, effort="low")


async def test_one_round_with_the_schema_and_a_priced_reply():
    provider = ScriptedProvider([Round(text(json.dumps({"message": "Who?", "brief": {}})),
                                       usage=Usage(input=1_000_000, output=0))])
    reply = await LlmStorylineModel(lambda _m: provider).structured(REQUEST)
    assert reply.data == {"message": "Who?", "brief": {}} and reply.stop_reason == "end_turn"
    assert reply.usage.cost_usd == pytest.approx(2.0)  # Sonnet 5.5 input: $2 per million
    sent = provider.requests[0]
    assert sent.output_schema == REQUEST.schema and sent.effort == "low" and sent.max_tokens == 4096
    assert sent.system == "sys" and sent.messages == REQUEST.messages


@pytest.mark.parametrize(("blocks", "stop"), [([text("not json")], "end_turn"), ([text("[1]")], "end_turn"),
                                              ([text('{"a":')], "max_tokens"), ([], "refusal")])
async def test_no_usable_json_is_data_none(blocks: list[dict[str, object]], stop: str):
    provider = ScriptedProvider([Round(*blocks, stop=stop)])
    reply = await LlmStorylineModel(lambda _m: provider).structured(REQUEST)
    assert reply.data is None and reply.stop_reason == stop


async def test_errors_map_to_the_storyline_port():
    def unavailable(_m: str) -> ScriptedProvider:
        raise ProviderUnavailable("ANTHROPIC_API_KEY is not configured.")

    with pytest.raises(ModelRejected):
        await LlmStorylineModel(unavailable).structured(REQUEST)

    class Failing(ScriptedProvider):
        def stream_round(self, request):  # type: ignore[no-untyped-def]
            raise ProviderError("Claude API call failed (InternalServerError 529).")
            yield  # pragma: no cover

    with pytest.raises(ModelUnavailable):
        await LlmStorylineModel(lambda _m: Failing([])).structured(REQUEST)
