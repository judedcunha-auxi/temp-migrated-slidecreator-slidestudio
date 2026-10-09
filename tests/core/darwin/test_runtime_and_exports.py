"""core/darwin/runtime (wiring, the ledger wrapper, workers), core/darwin/jobs (status shapes) and
core/darwin/exports (the pptx-submit inputs, brand fallbacks, the deck job)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import fakeredis
import pytest

from app.config.storyline import StorylineSettings
from app.core.darwin import exports
from app.core.darwin.jobs import json_job_status, pptx_status_body, read_any_job, read_job_pptx, read_owned_job
from app.core.darwin.runtime import build_runtime
from app.core.errors import ApiError
from app.core.jobs.worker import JobContext, PermanentJobError
from app.core.redis_client import RedisClient
from app.core.storage.models import BrandCreate, CallerContext, DeckCreate, JobCreate, SlideSpec, VersionCreate
from tests.conftest import build_ai_settings, build_darwin_settings, build_settings
from tests.fakes.general_service import FakeGeneralService
from tests.fakes.image_gen import FakeImageGenerator, tiny_png
from tests.fakes.storyline_model import ScriptedStorylineModel

pytestmark = pytest.mark.asyncio

STORY = {"presentationTitle": "T", "slides": [{"number": 1, "title": "Hello there", "type": "title",
                                                "framework": "title slide", "description": "d", "bullets": []}]}


def runtime(store: FakeGeneralService, tmp_path: Path, *steps: Any) -> Any:
    redis = RedisClient(fakeredis.FakeAsyncRedis(decode_responses=True))
    return build_runtime(build_settings(), redis, store, storyline_model=ScriptedStorylineModel(*steps),
                         images=FakeImageGenerator(), ai=build_ai_settings(), darwin=build_darwin_settings(),
                         storyline=StorylineSettings(_env_file=None), scratch=tmp_path)  # type: ignore[call-arg]


async def test_every_darwin_job_type_is_registered_and_none_counts_against_the_in_flight_limit(tmp_path: Path):
    rt = runtime(FakeGeneralService(), tmp_path)
    assert {"storyline", "slides.design_and_export", "exports.stitch_deck", "darwin.pptx_deck"} <= set(rt.registry.types)
    assert not any(t.expensive for t in rt.registry.types.values())  # TODO-P5 in-flight


async def test_a_storyline_job_writes_its_cost_to_the_ledger(tmp_path: Path):
    store = FakeGeneralService()
    ctx, _ = await store.make_user("alice")
    rt = runtime(store, tmp_path, STORY)
    record = await rt.queue.enqueue(ctx, "storyline", {"topic": "x"})
    assert await rt.run_pending() == 1
    assert (await store.jobs.get(ctx, record.id)).status == "done"
    spend = await store.usage.spend_for_user(ctx, datetime.now(UTC).date())
    assert spend.events == 1 and spend.cost_usd == pytest.approx(0.0125)
    events = [e for e in store.memory.scan("usage")]
    assert events[0]["model"] == rt.storyline.model and events[0]["kind"] == "storyline"


async def test_workers_start_and_stop_in_the_api_process(tmp_path: Path):
    store = FakeGeneralService()
    ctx, _ = await store.make_user("alice")
    rt = runtime(store, tmp_path, STORY)
    rt.start_workers(2)
    record = await rt.queue.enqueue(ctx, "storyline", {"topic": "x"})
    for _ in range(100):
        if (await store.jobs.get(ctx, record.id)).status == "done":
            break
        await asyncio.sleep(0.05)
    await rt.stop_workers()
    assert (await store.jobs.get(ctx, record.id)).status == "done"
    assert not rt._tasks


# ------------------------------------------------------------------------------------- jobs.py
async def test_json_status_shapes():
    store = FakeGeneralService()
    ctx, _ = await store.make_user("alice")
    other, _ = await store.make_user("bob")
    record, _ = await store.jobs.create(ctx, JobCreate(type="storyline"))
    assert await read_owned_job(store, ctx, "no-such", ("storyline",)) is None
    assert await read_owned_job(store, ctx, record.id, ("brand.extract",)) is None  # another kind: unknown
    with pytest.raises(ApiError) as err:
        await read_owned_job(store, other, record.id, ("storyline",))
    assert (err.value.status, err.value.detail) == (403, "Not your job")
    assert json_job_status(None, lambda r: {}) == {"status": "pending"}
    assert json_job_status(record, lambda r: {"x": 1}) == {"status": "pending"}
    done = record.model_copy(update={"status": "done", "result": {"a": 1}})
    assert json_job_status(done, lambda r: {"result": r.result}) == {"status": "done", "result": {"a": 1}}
    failed = record.model_copy(update={"status": "error", "error": None})
    assert json_job_status(failed, lambda r: {}, failure="F") == {"status": "error", "error": "F"}


async def test_pptx_status_vocabulary_and_502s():
    store = FakeGeneralService()
    ctx, _ = await store.make_user("alice")
    record, _ = await store.jobs.create(ctx, JobCreate(type="slides.design_and_export"))
    for status, word in (("queued", "queued"), ("running", "running"), ("done", "done"), ("error", "failed"),
                         ("cancelled", "failed")):
        body = pptx_status_body(record.model_copy(update={"status": status}))
        assert body == {"status": word, "done": word == "done", "failed": word == "failed"}
    with pytest.raises(ApiError) as err:
        await read_any_job(store, "r", "unknown", ("slides.design_and_export",), message="PPTX service error")
    assert err.value.status == 502
    with pytest.raises(ApiError) as err:
        await read_job_pptx(store, record, "r")
    assert (err.value.status, err.value.detail) == (502, "PPTX service unavailable")
    ref = (await store.blobs.put(ctx, b"PK..", "application/zip")).ref
    done = record.model_copy(update={"status": "done", "result": {"pptxRef": ref}})
    assert await read_job_pptx(store, done, "r") == b"PK.."
    gone = record.model_copy(update={"status": "done", "result": {"pptxRef": "missing"}})
    with pytest.raises(ApiError):
        await read_job_pptx(store, gone, "r")


# ---------------------------------------------------------------------------------- exports.py
async def _deck(store: FakeGeneralService, ctx: CallerContext, *, kind: str = "content",
                inputs: dict[str, Any] | None = None) -> Any:
    story = {"slides": [{"number": 1, "title": "Revenue up", "type": kind}]}
    deck = await store.decks.create(ctx, DeckCreate(title="D", inputs=inputs or {}, storyline=story),
                                    [SlideSpec(number=1, title="Revenue up", type=kind)])
    for _ in range(2):
        ref = (await store.blobs.put(ctx, tiny_png(), "image/png")).ref
        await store.slides.append_version(ctx, deck.id, 1, VersionCreate(mode="image", image_ref=ref))
    return deck


async def test_an_unbranded_deck_sends_only_colours_and_fonts():
    store = FakeGeneralService()
    ctx, profile = await store.make_user("alice")
    deck = await _deck(store, ctx)
    inputs = await exports.slide_export_inputs(store, ctx, profile, deck, 1, 1, True)
    assert inputs["brand"] == {"primary_color": "#1F3A5F", "accent_color": "#2E7D9A", "heading_font": "Inter",
                               "body_font": "Inter"}
    assert "templateRef" not in inputs and "furniture" not in inputs
    v1 = (await store.slides.get(ctx, deck.id, 1)).versions[0].image_ref
    assert inputs["imageRef"] == v1


async def test_version_2_is_the_refined_image_and_a_missing_version_is_404():
    store = FakeGeneralService()
    ctx, profile = await store.make_user("alice")
    deck = await _deck(store, ctx)
    v2 = (await store.slides.get(ctx, deck.id, 1)).versions[1].image_ref
    assert (await exports.slide_export_inputs(store, ctx, profile, deck, 1, 2, True))["imageRef"] == v2
    with pytest.raises(ApiError) as err:
        await exports.slide_export_inputs(store, ctx, profile, deck, 1, 7, True)
    assert (err.value.status, err.value.detail) == (404, "Image not found")


async def test_a_branded_cover_gets_master_furniture_heading_and_date():
    store = FakeGeneralService()
    ctx, profile = await store.make_user("alice")
    kit = {"primaryColor": "#112233", "headingFont": "Georgia", "titleMasterKey": "x", "masterKey": "y",
           "furnitureArchetypes": ["cover", "bogus"]}
    brand = await store.brands.create(ctx, BrandCreate(name="B", kit=kit))
    await store.brands.put_asset(ctx, brand.id, "titleMaster", tiny_png(color=(1, 2, 3)), "image/png")
    await store.brands.put_asset(ctx, brand.id, "furniture", json.dumps({"layouts": {}}).encode(), "application/json")
    deck = await _deck(store, ctx, kind="title", inputs={"brandId": brand.id, "language": "ar"})
    inputs = await exports.slide_export_inputs(store, ctx, profile, deck, 1, 1, False,
                                               clock=lambda: datetime(2026, 10, 9, tzinfo=UTC))
    b = inputs["brand"]
    assert (b["primary_color"], b["heading_font"], b["language"]) == ("#112233", "Georgia", "ar")
    assert (b["layout_archetype"], b["apply_brand_layout"], b["apply_template_bg"]) == ("cover", False, False)
    assert (b["heading"], b["date"]) == ("Revenue up", "October 2026")
    assert inputs["furniture"] == {"layouts": {}}
    template = await store.blobs.get(ctx, inputs["templateRef"])  # copied to the caller, readable by the job
    assert template.data == tiny_png(color=(1, 2, 3))


async def test_an_unreadable_brand_degrades_to_the_legacy_kit():
    store = FakeGeneralService()
    ctx, profile = await store.make_user("alice")
    bob, _ = await store.make_user("bob")
    brand = await store.brands.create(bob, BrandCreate(name="B", kit={"primaryColor": "#000000"}))
    deck = await _deck(store, ctx, inputs={"brandId": brand.id})
    inputs = await exports.slide_export_inputs(store, ctx, profile, deck, 1, 1, True)
    assert inputs["brand"]["primary_color"] == "#1F3A5F"


async def test_a_gif_becomes_a_png_for_the_pipeline():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("P", (4, 4)).save(buf, format="GIF")
    data, name = exports.image_for_pipeline(buf.getvalue(), "image/gif")
    assert name == "slide.png" and data[:8] == b"\x89PNG\r\n\x1a\n"
    assert exports.image_for_pipeline(b"jpg", "image/jpeg") == (b"jpg", "slide.jpg")


async def test_the_deck_job_resolves_only_the_callers_finished_slide_jobs(tmp_path: Path):
    from app.core.slides.jobs import PipelineDeps
    from app.core.storage.models import JobPatch

    store = FakeGeneralService()
    ctx, _ = await store.make_user("alice")
    bob, _ = await store.make_user("bob")
    seen: list[Any] = []

    async def stitch(job: JobContext) -> Any:
        seen.append(job.record.inputs)
        from app.core.jobs.worker import JobOutcome

        return JobOutcome(result={"pptxRef": "r"})

    handler = exports.deck_handler(PipelineDeps(blobs=store.blobs, scratch_root=tmp_path,
                                                settings=build_ai_settings()), store, stitch=stitch)
    mine, _ = await store.jobs.create(ctx, JobCreate(type="slides.design_and_export"))
    system = CallerContext.service("r")
    await store.jobs.update(system, mine.id, JobPatch(status="done", result={"deckRef": "d1", "slideId": "s1"}))
    theirs, _ = await store.jobs.create(bob, JobCreate(type="slides.design_and_export"))

    async def progress(_v: dict[str, Any]) -> None:
        return None

    def job(ids: list[Any], title: Any = "Q3") -> JobContext:
        from app.core.storage.models import JobRecord

        rec = JobRecord(id="deck", type="darwin.pptx_deck", owner_id="o", owner_subject="alice",
                        inputs={"jobIds": ids, "presentationTitle": title}, created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC), expires_at=datetime.now(UTC))
        return JobContext(record=rec, owner=ctx, attempt=1, _progress=progress)

    await handler(job([mine.id], ""))
    assert seen[-1] == {"parts": [{"deckRef": "d1", "slideId": "s1"}], "title": "Deck"}
    for ids in ([theirs.id], ["unknown"], [1]):
        with pytest.raises(PermanentJobError):
            await handler(job(ids))
