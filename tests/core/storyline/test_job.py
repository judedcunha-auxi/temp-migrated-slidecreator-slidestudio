"""The `storyline` job on the real queue and worker (fakeredis + the General service fake), and the
`/api/storyline-status` bodies (contract api-storyline-status.json; Darwin `storyline-background.test.ts`,
`storyline-status.test.ts`)."""

from __future__ import annotations

from typing import Any

import pytest

from app.config.storyline import StorylineSettings
from app.core.jobs.queue import JobQueue
from app.core.jobs.registry import JobRegistry
from app.core.jobs.worker import Worker
from app.core.storyline import job as storyline_job
from app.core.storyline.ports import ModelRejected, ModelUnavailable
from app.core.storyline.validation import INCOMPLETE_STORYLINE
from tests.core.storyline.conftest import answer, long_deck, slide
from tests.fakes.general_service import FakeGeneralService
from tests.fakes.storyline_model import FAKE_COST, ScriptedStorylineModel, reply

pytestmark = pytest.mark.asyncio

BODY = {"topic": "Q3", "numSlides": 9, "company": "Acme", "audience": "Board", "style": "Executive strategy",
        "brandId": "brand-1", "clientOnlyField": {"kept": True}}


async def _run(queue: JobQueue, model: ScriptedStorylineModel, settings: StorylineSettings, *,
               prompter: Any = None, runs: int = 1) -> None:
    registry = JobRegistry()
    storyline_job.register(registry, model, settings=settings, prompter=prompter)
    worker = Worker(queue, registry.install(queue))
    for _ in range(runs):
        await worker.run_once()


async def test_a_done_job_has_darwins_result_shape(queue: JobQueue, storage: FakeGeneralService,
                                                   settings: StorylineSettings):
    ctx, _ = await storage.make_user("alice")
    record = await queue.enqueue(ctx, "storyline", BODY)
    assert storyline_job.storyline_status(record) == {"status": "pending"}

    await _run(queue, ScriptedStorylineModel(answer(*long_deck(), title="Q3 beat plan")), settings)
    body = await storyline_job.read_storyline_status(storage, ctx, record.id)

    assert set(body) == {"status", "result"} and body["status"] == "done"
    result = body["result"]
    assert set(result) == {"presentationTitle", "inputs", "slides", "warnings"}
    assert result["presentationTitle"] == "Q3 beat plan"
    assert result["inputs"] == BODY  # echoed verbatim, unknown fields included
    assert len(result["slides"]) == 11 and result["warnings"] == []
    first = result["slides"][0]
    assert set(first) <= {"number", "title", "type", "section", "framework", "description", "bullets", "chartData",
                          "archetypeId", "prompt"}
    assert None not in first.values() and "prompt" not in first
    final = await storage.jobs.get(ctx, record.id)
    assert final.cost_usd == pytest.approx(FAKE_COST)


async def test_the_route_layers_prompter_adds_one_prompt_per_slide(queue: JobQueue, storage: FakeGeneralService,
                                                                    settings: StorylineSettings):
    ctx, _ = await storage.make_user("alice")
    seen: list[tuple[str, dict[str, Any], int]] = []

    async def prompter(owner: str, inputs: dict[str, Any], slides: list[dict[str, Any]]) -> list[str]:
        seen.append((owner, inputs, len(slides)))
        return [f"image prompt {s['number']}" for s in slides]

    record = await queue.enqueue(ctx, "storyline", BODY)
    await _run(queue, ScriptedStorylineModel(answer(slide(1), slide(2))), settings, prompter=prompter)
    body = await storyline_job.read_storyline_status(storage, ctx, record.id)
    assert [s["prompt"] for s in body["result"]["slides"]] == ["image prompt 1", "image prompt 2"]
    assert seen == [("alice", BODY, 2)]


async def test_an_invalid_storyline_is_an_error_job_with_darwins_message(queue: JobQueue, storage: FakeGeneralService,
                                                                          settings: StorylineSettings):
    ctx, _ = await storage.make_user("alice")
    record = await queue.enqueue(ctx, "storyline", BODY)
    model = ScriptedStorylineModel(answer(slide(bullets=("one",))))
    await _run(queue, model, settings)
    assert await storyline_job.read_storyline_status(storage, ctx, record.id) == {
        "status": "error", "error": INCOMPLETE_STORYLINE}
    assert model.calls == 1  # permanent: not retried


@pytest.mark.parametrize("step, message", [
    (reply(None, stop_reason="refusal"), "The model declined to draft this storyline"),
    (ModelRejected("401 invalid x-api-key sk-ant-..."), "Storyline generation failed"),
])
async def test_permanent_failures_carry_a_safe_message(queue: JobQueue, storage: FakeGeneralService,
                                                       settings: StorylineSettings, step: Any, message: str):
    ctx, _ = await storage.make_user("alice")
    record = await queue.enqueue(ctx, "storyline", BODY)
    await _run(queue, ScriptedStorylineModel(step), settings)
    body = await storyline_job.read_storyline_status(storage, ctx, record.id)
    assert body["status"] == "error" and body["error"].startswith(message) and "sk-ant" not in body["error"]


async def test_an_outage_is_retried_then_succeeds(queue: JobQueue, storage: FakeGeneralService,
                                                  settings: StorylineSettings):
    ctx, _ = await storage.make_user("alice")
    record = await queue.enqueue(ctx, "storyline", BODY)
    model = ScriptedStorylineModel(ModelUnavailable("529 overloaded"), answer(slide()))
    await _run(queue, model, settings, runs=1)
    assert await storyline_job.read_storyline_status(storage, ctx, record.id) == {"status": "pending"}
    await _run(queue, model, settings, runs=1)
    assert (await storyline_job.read_storyline_status(storage, ctx, record.id))["status"] == "done"
    assert model.calls == 2


async def test_an_outage_on_the_last_attempt_says_the_model_is_busy(queue: JobQueue, storage: FakeGeneralService,
                                                                    settings: StorylineSettings):
    ctx, _ = await storage.make_user("alice")
    record = await queue.enqueue(ctx, "storyline", BODY)
    model = ScriptedStorylineModel(ModelUnavailable("529"), ModelUnavailable("529"))
    await _run(queue, model, settings, runs=2)
    assert await storyline_job.read_storyline_status(storage, ctx, record.id) == {
        "status": "error", "error": storyline_job.BUSY_FAILURE}


async def test_status_for_unknown_and_foreign_jobs(queue: JobQueue, storage: FakeGeneralService):
    alice, _ = await storage.make_user("alice")
    bob, _ = await storage.make_user("bob")
    assert await storyline_job.read_storyline_status(storage, alice, "no-such-job") == {"status": "pending"}
    record = await queue.enqueue(alice, "storyline", BODY)
    with pytest.raises(storyline_job.NotYourJob, match="Not your job"):
        await storyline_job.read_storyline_status(storage, bob, record.id)


async def test_the_job_type_is_expensive_and_takes_its_limits_from_settings(settings: StorylineSettings):
    jt = storyline_job.job_type(settings)
    assert (jt.name, jt.expensive, jt.max_attempts, jt.timeout_s) == ("storyline", True, 2, 30)
    registry = JobRegistry()
    storyline_job.register(registry, ScriptedStorylineModel(), settings=settings)
    with pytest.raises(ValueError, match="already registered"):
        storyline_job.register(registry, ScriptedStorylineModel(), settings=settings)
