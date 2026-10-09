"""core/jobs/queue and core/jobs/limiter: enqueue, lease, heartbeat, re-queue, limits."""

from __future__ import annotations

import asyncio

import pytest

from app.core.jobs.limiter import InFlightLimiter, TooManyInFlight
from app.core.jobs.queue import READY_KEY, JobQueue, JobType, LeaseLost
from app.core.redis_client import RedisClient
from app.core.storage.models import CallerContext, JobPatch
from app.core.storage.ports import Conflict
from tests.core.jobs.conftest import ManualClock
from tests.fakes.general_service import FakeGeneralService

pytestmark = pytest.mark.asyncio
SYSTEM = CallerContext.service("sys")


async def test_enqueue_writes_the_durable_record_then_queues(queue: JobQueue, storage: FakeGeneralService,
                                                             redis: RedisClient):
    ctx, profile = await storage.make_user("alice", request_id="req-enq")
    record = await queue.enqueue(ctx, "storyline", {"topic": "Q3"})
    stored = await storage.jobs.get(ctx, record.id)
    assert (stored.status, stored.owner_id, stored.inputs, stored.request_id) == ("queued", profile.id,
                                                                                  {"topic": "Q3"}, "req-enq")
    assert await redis.zrange(READY_KEY, 0, -1) == [record.id]
    assert ("jobs.create", "alice", "req-enq") in [(c.operation, c.subject, c.request_id) for c in storage.calls]


async def test_unknown_type_is_refused(queue: JobQueue, storage: FakeGeneralService):
    ctx, _ = await storage.make_user("alice")
    with pytest.raises(ValueError):
        await queue.enqueue(ctx, "nope", {})


async def test_enqueue_is_idempotent_by_key(queue: JobQueue, storage: FakeGeneralService):
    ctx, _ = await storage.make_user("alice")
    first = await queue.enqueue(ctx, "render", {"slide": 1}, idempotency_key="k")
    again = await queue.enqueue(ctx, "render", {"slide": 1}, idempotency_key="k")
    assert again.id == first.id and await queue.pending() == 1
    with pytest.raises(Conflict):
        await queue.enqueue(ctx, "render", {"slide": 2}, idempotency_key="k")


async def test_three_expensive_jobs_in_flight_per_user(queue: JobQueue, storage: FakeGeneralService):
    alice, _ = await storage.make_user("alice")
    bob, _ = await storage.make_user("bob")
    jobs = [await queue.enqueue(alice, "render", {"n": i}, idempotency_key=f"k{i}") for i in range(3)]
    with pytest.raises(TooManyInFlight):
        await queue.enqueue(alice, "render", {"n": 3}, idempotency_key="k3")
    # A replay of a job already in flight is answered, not refused; the 429 consumed no key.
    assert (await queue.enqueue(alice, "render", {"n": 0}, idempotency_key="k0")).id == jobs[0].id
    assert await storage.jobs.find_by_key(alice, "render", "k3") is None
    await queue.enqueue(alice, "storyline", {})  # cheap jobs are not limited
    await queue.enqueue(bob, "render", {})       # the limit is per user
    # Finishing one frees a slot.
    lease = await queue.lease("w1")
    assert lease is not None and lease.job_id == jobs[0].id
    await queue.complete(lease, {"ok": True})
    await queue.enqueue(alice, "render", {"n": 3}, idempotency_key="k3")


async def test_a_lease_is_exclusive(queue: JobQueue, storage: FakeGeneralService):
    ctx, _ = await storage.make_user("alice")
    record = await queue.enqueue(ctx, "storyline", {})
    leases = await asyncio.gather(*(queue.lease(f"w{i}") for i in range(5)))
    taken = [lease for lease in leases if lease is not None]
    assert len(taken) == 1 and taken[0].job_id == record.id and taken[0].attempt == 1
    running = await storage.jobs.get(ctx, record.id)
    assert (running.status, running.attempts) == ("running", 1)
    assert taken[0].owner == CallerContext(subject="alice", request_id="req-test")


async def test_heartbeat_keeps_the_lease_and_reports_progress(queue: JobQueue, storage: FakeGeneralService):
    ctx, _ = await storage.make_user("alice")
    record = await queue.enqueue(ctx, "storyline", {})
    lease = await queue.lease("w1")
    assert lease is not None
    for _ in range(4):  # 0.4 s in total, past the 0.3 s lease, kept alive by heartbeats
        await asyncio.sleep(0.1)
        await queue.heartbeat(lease, progress={"done": 1})
    assert await queue.lease("w2") is None
    assert (await storage.jobs.get(ctx, record.id)).progress == {"done": 1}


async def test_an_expired_lease_is_requeued_and_counted(queue: JobQueue, storage: FakeGeneralService):
    ctx, _ = await storage.make_user("alice")
    record = await queue.enqueue(ctx, "storyline", {})
    first = await queue.lease("w1")
    assert first is not None
    await asyncio.sleep(0.4)  # w1 "dies": no heartbeat, the lease key expires
    second = await queue.lease("w2")
    assert second is not None and (second.job_id, second.attempt) == (record.id, 2)
    with pytest.raises(LeaseLost):
        await queue.heartbeat(first)
    with pytest.raises(LeaseLost):
        await queue.complete(first, {"late": True})
    done = await queue.complete(second, {"ok": True})
    assert (done.status, done.result, done.attempts) == ("done", {"ok": True}, 2)


async def test_max_attempts_then_gives_up(queue: JobQueue, storage: FakeGeneralService):
    ctx, _ = await storage.make_user("alice")
    record = await queue.enqueue(ctx, "storyline", {})  # max_attempts=2
    for _ in range(2):
        lease = await queue.lease("w")
        assert lease is not None
        await asyncio.sleep(0.4)
    assert await queue.lease("w") is None
    final = await storage.jobs.get(ctx, record.id)
    assert (final.status, final.attempts, final.error) == ("error", 2, "gave up after 2 attempts")
    assert await queue.pending() == 0


async def test_retryable_failure_requeues_until_attempts_run_out(queue: JobQueue, storage: FakeGeneralService):
    ctx, _ = await storage.make_user("alice")
    record = await queue.enqueue(ctx, "storyline", {})
    lease = await queue.lease("w")
    assert lease is not None
    queued = await queue.fail(lease, "flaky upstream")
    assert (queued.status, queued.error) == ("queued", "flaky upstream")
    lease = await queue.lease("w")  # immediately available again
    assert lease is not None and lease.attempt == 2
    final = await queue.fail(lease, "flaky again")
    assert final.status == "error" and await queue.pending() == 0
    assert (await storage.jobs.get(ctx, record.id)).error == "flaky again"


async def test_permanent_failure_ends_at_once_and_frees_the_slot(queue: JobQueue, storage: FakeGeneralService):
    ctx, _ = await storage.make_user("alice")
    await queue.enqueue(ctx, "render", {})
    lease = await queue.lease("w")
    assert lease is not None
    assert (await queue.fail(lease, "bad input", retryable=False)).status == "error"
    assert await queue._limiter.in_flight("alice") == 0


async def test_a_heartbeat_past_the_timeout_is_refused(redis: RedisClient, storage: FakeGeneralService):
    clock = ManualClock()
    q = JobQueue(redis, storage, types={"t": JobType("t", timeout_s=10, lease_s=30)}, clock=clock)
    ctx, _ = await storage.make_user("alice")
    await q.enqueue(ctx, "t", {})
    lease = await q.lease("w")
    assert lease is not None
    clock.advance(11)
    with pytest.raises(LeaseLost):
        await q.heartbeat(lease)


async def test_a_job_finished_before_a_crash_is_not_run_again(queue: JobQueue, storage: FakeGeneralService,
                                                              redis: RedisClient):
    ctx, _ = await storage.make_user("alice")
    record = await queue.enqueue(ctx, "storyline", {})
    lease = await queue.lease("w1")
    assert lease is not None
    # Durable record written, then the worker died before cleaning Redis up.
    await storage.jobs.update(SYSTEM, record.id, JobPatch(status="done", result={"ok": 1}))
    await redis.delete(f"sf:jobs:lease:{record.id}")
    assert await queue.lease("w2") is None
    assert await queue.pending() == 0
    assert (await storage.jobs.get(ctx, record.id)).result == {"ok": 1}


async def test_a_job_whose_record_expired_is_dropped(queue: JobQueue, storage: FakeGeneralService):
    ctx, _ = await storage.make_user("alice")
    await queue.enqueue(ctx, "storyline", {})
    storage.clock.advance(days=8)  # past the 7-day record TTL
    assert await queue.lease("w") is None
    assert await queue.pending() == 0


# ------------------------------------------------------------------ limiter
async def test_limiter_add_then_count(redis: RedisClient):
    clock = ManualClock()
    limiter = InFlightLimiter(redis, limit=2, clock=clock)
    assert await limiter.acquire("u", "a", ttl_seconds=60)
    assert await limiter.acquire("u", "b", ttl_seconds=60)
    assert not await limiter.acquire("u", "c", ttl_seconds=60)
    assert await limiter.in_flight("u") == 2
    await limiter.release("u", "a")
    assert await limiter.acquire("u", "c", ttl_seconds=60)
    clock.advance(61)  # slots of dead jobs free themselves
    assert await limiter.in_flight("u") == 0
    with pytest.raises(ValueError):
        InFlightLimiter(redis, limit=0)


async def test_limiter_never_admits_past_the_limit_under_a_race(redis: RedisClient):
    limiter = InFlightLimiter(redis, limit=3)
    results = await asyncio.gather(*(limiter.acquire("u", f"s{i}", ttl_seconds=60) for i in range(10)))
    assert sum(results) <= 3 and await limiter.in_flight("u") <= 3
