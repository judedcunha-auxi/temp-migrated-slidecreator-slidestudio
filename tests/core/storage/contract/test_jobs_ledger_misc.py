"""Contract: job records, the usage ledger, transcripts, analytics and health."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core.storage.models import (
    AnalyticsEventCreate,
    CallerContext,
    DeckCreate,
    ExportCreate,
    IntakeTranscriptUpsert,
    JobCreate,
    JobPatch,
    PageViewCreate,
    UsageCreate,
)
from app.core.storage.ports import Conflict, Forbidden, NotFound
from tests.core.storage.contract.conftest import Harness

pytestmark = pytest.mark.asyncio

SYSTEM = CallerContext.service("req-sys")


# ------------------------------------------------------------------ job records
async def test_job_create_get_update(harness: Harness):
    s = harness.storage
    ctx, profile = await harness.user("alice")
    job, created = await s.jobs.create(ctx, JobCreate(type="storyline", inputs={"topic": "x"}))
    assert created and (job.status, job.owner_id, job.owner_subject) == ("queued", profile.id, "alice")
    assert job.request_id == ctx.request_id
    running = await s.jobs.update(ctx, job.id, JobPatch(status="running", progress={"done": 1, "total": 3}))
    done = await s.jobs.update(SYSTEM, job.id, JobPatch(status="done", result={"slides": []}, cost_usd=0.02),
                               expected_revision=running.revision)
    got = await s.jobs.get(ctx, job.id)
    assert (got.status, got.result, got.cost_usd, got.progress) == ("done", {"slides": []}, 0.02,
                                                                    {"done": 1, "total": 3})
    assert done.revision == job.revision + 2


async def test_another_users_job_is_forbidden_and_a_missing_one_not_found(harness: Harness):
    """The Darwin status routes need the difference: 403 'Not your job' vs 200 pending."""
    s = harness.storage
    alice, _ = await harness.user("alice")
    bob, _ = await harness.user("bob")
    job, _ = await s.jobs.create(alice, JobCreate(type="storyline"))
    with pytest.raises(Forbidden):
        await s.jobs.get(bob, job.id)
    with pytest.raises(Forbidden):
        await s.jobs.update(bob, job.id, JobPatch(status="cancelled"))
    for missing in ("00000000-0000-0000-0000-000000000000", "../job", ""):
        with pytest.raises(NotFound):
            await s.jobs.get(alice, missing)
    assert (await s.jobs.list_mine(bob)).items == []


async def test_job_idempotency_is_per_caller_and_type(harness: Harness):
    s = harness.storage
    alice, _ = await harness.user("alice")
    bob, _ = await harness.user("bob")
    data = JobCreate(type="brand-extract", inputs={"pdf": "ref1"}, idempotency_key="k")
    first, created = await s.jobs.create(alice, data)
    again, created_again = await s.jobs.create(alice, data)
    assert created and not created_again and again.id == first.id
    assert (await s.jobs.find_by_key(alice, "brand-extract", "k")) == again
    assert await s.jobs.find_by_key(alice, "storyline", "k") is None
    assert await s.jobs.find_by_key(bob, "brand-extract", "k") is None
    with pytest.raises(Conflict):
        await s.jobs.create(alice, JobCreate(type="brand-extract", inputs={"pdf": "ref2"}, idempotency_key="k"))
    other_type, created_other = await s.jobs.create(alice, JobCreate(type="storyline", idempotency_key="k"))
    bobs, created_bob = await s.jobs.create(bob, data)
    assert created_other and created_bob and len({first.id, other_type.id, bobs.id}) == 3


async def test_a_finished_job_cannot_change_status(harness: Harness):
    s = harness.storage
    ctx, _ = await harness.user("alice")
    job, _ = await s.jobs.create(ctx, JobCreate(type="t"))
    await s.jobs.update(ctx, job.id, JobPatch(status="error", error="boom"))
    with pytest.raises(Conflict):
        await s.jobs.update(ctx, job.id, JobPatch(status="running"))
    with pytest.raises(Conflict):
        await s.jobs.update(ctx, job.id, JobPatch(status="queued"), expected_revision=1)


async def test_job_ttl_expiry_and_purge(harness: Harness):
    s = harness.storage
    ctx, _ = await harness.user("alice")
    short, _ = await s.jobs.create(ctx, JobCreate(type="t", ttl_seconds=60, idempotency_key="k"))
    long, _ = await s.jobs.create(ctx, JobCreate(type="t", ttl_seconds=3600))
    harness.clock.advance(seconds=61)
    with pytest.raises(NotFound):
        await s.jobs.get(ctx, short.id)
    assert await s.jobs.find_by_key(ctx, "t", "k") is None
    assert [j.id for j in (await s.jobs.list_mine(ctx)).items] == [long.id]
    # After expiry the key is free again.
    fresh, created = await s.jobs.create(ctx, JobCreate(type="t", ttl_seconds=60, idempotency_key="k"))
    assert created and fresh.id != short.id
    with pytest.raises(Forbidden):
        await s.jobs.purge_expired(ctx)
    harness.clock.advance(hours=2)
    assert await s.jobs.purge_expired(SYSTEM) == 2


async def test_job_list_filters(harness: Harness):
    s = harness.storage
    ctx, _ = await harness.user("alice")
    a, _ = await s.jobs.create(ctx, JobCreate(type="storyline"))
    harness.clock.advance(seconds=1)
    b, _ = await s.jobs.create(ctx, JobCreate(type="export"))
    await s.jobs.update(ctx, b.id, JobPatch(status="running"))
    assert [j.id for j in (await s.jobs.list_mine(ctx)).items] == [b.id, a.id]
    assert [j.id for j in (await s.jobs.list_mine(ctx, type="storyline")).items] == [a.id]
    assert [j.id for j in (await s.jobs.list_mine(ctx, status="running")).items] == [b.id]


# ------------------------------------------------------------------ usage ledger
async def test_usage_ledger_spend_and_idempotency(harness: Harness):
    s = harness.storage
    alice, _ = await harness.user("alice")
    bob, _ = await harness.user("bob")
    admin, _ = await harness.user("admin", admin=True)
    await s.usage.append(alice, UsageCreate(model="gpt-image", est_cost_usd=0.04), idempotency_key="u1")
    await s.usage.append(alice, UsageCreate(model="gpt-image", est_cost_usd=0.04), idempotency_key="u1")
    await s.usage.append(alice, UsageCreate(kind="claude", est_cost_usd=0.0125))
    await s.usage.append(bob, UsageCreate(est_cost_usd=1))
    today = harness.clock.now.date()
    mine = await s.usage.spend_for_user(alice, today)
    assert (mine.events, mine.cost_usd) == (2, 0.0525)
    assert (await s.usage.spend_for_user(alice, today + timedelta(days=1))).events == 0
    with pytest.raises(Forbidden):
        await s.usage.global_spend(alice, today)
    assert (await s.usage.global_spend(admin, today)).cost_usd == 1.0525
    assert (await s.usage.global_spend(SYSTEM, date(2000, 1, 1))).events == 0


# ------------------------------------------------------------------ transcripts
async def test_intake_transcripts_upsert_and_link(harness: Harness):
    s = harness.storage
    alice, _ = await harness.user("alice")
    bob, _ = await harness.user("bob")
    first = await s.transcripts.upsert_intake(alice, IntakeTranscriptUpsert(session_key="s1", messages=[1],
                                                                            turn_count=1))
    harness.clock.advance(seconds=10)
    second = await s.transcripts.upsert_intake(alice, IntakeTranscriptUpsert(session_key="s1", messages=[1, 2],
                                                                             turn_count=2))
    assert second.created_at == first.created_at and second.messages == [1, 2]
    with pytest.raises(NotFound):  # transcripts are per user
        await s.transcripts.get_intake(bob, "s1")
    deck = await s.decks.create(alice, DeckCreate(title="d"), [])
    linked = await s.transcripts.link_intake_to_deck(alice, "s1", deck.id)
    assert (linked.deck_id, linked.completed_at) == (deck.id, harness.clock.now)
    with pytest.raises(NotFound):
        await s.transcripts.link_intake_to_deck(bob, "s1", deck.id)
    await s.decks.delete(alice, deck.id)  # on delete set null
    assert (await s.transcripts.get_intake(alice, "s1")).deck_id is None


async def test_slide_edit_and_design_history(harness: Harness):
    s = harness.storage
    alice, _ = await harness.user("alice")
    bob, _ = await harness.user("bob")
    deck = await s.decks.create(alice, DeckCreate(title="d"), [])
    await s.transcripts.upsert_slide_edit(alice, deck.id, 3, [{"role": "user", "content": "bigger"}], 1)
    row = await s.transcripts.upsert_slide_edit(alice, deck.id, 3, [{"a": 1}, {"b": 2}], 2)
    assert (row.refine_count, len(row.messages)) == (2, 2)
    assert (await s.transcripts.get_slide_edit(alice, deck.id, 3)).messages == [{"a": 1}, {"b": 2}]
    with pytest.raises(NotFound):
        await s.transcripts.get_slide_edit(alice, deck.id, 4)
    with pytest.raises(NotFound):
        await s.transcripts.get_design_history(alice, deck.id)
    await s.transcripts.put_design_history(alice, deck.id, [{"role": "assistant"}])
    assert (await s.transcripts.get_design_history(alice, deck.id)).messages == [{"role": "assistant"}]
    with pytest.raises(NotFound):
        await s.transcripts.get_design_history(bob, deck.id)


# ------------------------------------------------------------------ analytics
async def test_analytics_events_and_admin_aggregates(harness: Harness):
    s = harness.storage
    alice, _ = await harness.user("alice")
    admin, _ = await harness.user("admin", admin=True)
    await s.analytics.record_event(alice, AnalyticsEventCreate(event_name="deck_created", properties={"n": 3}))
    await s.analytics.record_event(alice, AnalyticsEventCreate(event_name="deck_created"))
    await s.analytics.record_page_view(alice, PageViewCreate(session_id="s", path="/decks", duration_seconds=5))
    deck = await s.decks.create(alice, DeckCreate(title="d"), [])
    await s.exports.record(alice, ExportCreate(deck_id=deck.id, kind="pdf", slide_numbers=[]))
    await s.usage.append(alice, UsageCreate(est_cost_usd=0.5))
    with pytest.raises(Forbidden):
        await s.analytics.admin_aggregates(alice)
    agg = await s.analytics.admin_aggregates(admin)
    assert (agg.users, agg.decks, agg.exports, agg.events_by_name, agg.spend_usd) == (2, 1, 1, {"deck_created": 2},
                                                                                       0.5)
    later = await s.analytics.admin_aggregates(admin, since=harness.clock.now + timedelta(seconds=1))
    assert later.events_by_name == {} and later.spend_usd == 0


# ------------------------------------------------------------------ health
async def test_health_ping(harness: Harness):
    report = await harness.storage.health.ping(SYSTEM)
    assert report.status in ("ok", "skipped")
    assert report.backend == harness.storage.backend
