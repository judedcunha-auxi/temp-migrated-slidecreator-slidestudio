"""core/jobs/worker: outcomes, timeouts, and the Phase 4 exit criterion that a killed
worker's job is re-queued and completed by another worker."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.core.jobs.queue import JobQueue, JobType
from app.core.jobs.worker import JobContext, JobOutcome, PermanentJobError, Worker
from app.core.redis_client import RedisClient
from tests.fakes.general_service import FakeGeneralService

pytestmark = pytest.mark.asyncio


async def test_a_job_runs_reports_progress_and_completes(queue: JobQueue, storage: FakeGeneralService):
    ctx, _ = await storage.make_user("alice")
    seen: list[JobContext] = []

    async def handler(job: JobContext) -> JobOutcome:
        seen.append(job)
        await job.progress({"done": 1, "total": 2})
        return JobOutcome(result={"slides": 2}, cost_usd=0.08)

    record = await queue.enqueue(ctx, "render", {"deck": "d1"})
    assert await Worker(queue, {"render": handler}).run_once() is True
    final = await storage.jobs.get(ctx, record.id)
    assert (final.status, final.result, final.cost_usd, final.progress) == ("done", {"slides": 2}, 0.08,
                                                                            {"done": 1, "total": 2})
    assert seen[0].owner.subject == "alice" and seen[0].record.inputs == {"deck": "d1"}
    assert await Worker(queue, {"render": handler}).run_once() is False  # nothing left


async def test_a_handler_error_is_retried_then_succeeds(queue: JobQueue, storage: FakeGeneralService):
    ctx, _ = await storage.make_user("alice")

    async def flaky(job: JobContext) -> JobOutcome:
        if job.attempt == 1:
            raise RuntimeError("upstream 503 at secret-host.internal")
        return JobOutcome(result={"ok": True})

    record = await queue.enqueue(ctx, "render", {})
    worker = Worker(queue, {"render": flaky})
    await worker.run_once()
    mid = await storage.jobs.get(ctx, record.id)
    assert mid.status == "queued" and "secret-host" not in (mid.error or "")
    await worker.run_once()
    final = await storage.jobs.get(ctx, record.id)
    assert (final.status, final.attempts) == ("done", 2)


async def test_a_permanent_error_is_not_retried(queue: JobQueue, storage: FakeGeneralService):
    ctx, _ = await storage.make_user("alice")

    async def bad(job: JobContext) -> JobOutcome:
        raise PermanentJobError("The PDF could not be read.")

    record = await queue.enqueue(ctx, "render", {})
    await Worker(queue, {"render": bad}).run_once()
    final = await storage.jobs.get(ctx, record.id)
    assert (final.status, final.error, final.attempts) == ("error", "The PDF could not be read.", 1)


async def test_a_job_with_no_handler_fails(queue: JobQueue, storage: FakeGeneralService):
    ctx, _ = await storage.make_user("alice")
    record = await queue.enqueue(ctx, "render", {})
    await Worker(queue, {}).run_once()
    assert (await storage.jobs.get(ctx, record.id)).status == "error"


async def test_a_job_past_its_timeout_is_cancelled_and_retried(redis: RedisClient, storage: FakeGeneralService):
    q = JobQueue(redis, storage, types={"slow": JobType("slow", timeout_s=0.2, lease_s=5, max_attempts=2)})
    ctx, _ = await storage.make_user("alice")
    cancelled: list[int] = []

    async def slow(job: JobContext) -> JobOutcome:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(job.attempt)
            raise
        return JobOutcome()

    record = await q.enqueue(ctx, "slow", {})
    worker = Worker(q, {"slow": slow})
    await worker.run_once()
    assert cancelled == [1]
    mid = await storage.jobs.get(ctx, record.id)
    assert (mid.status, mid.error) == ("queued", "the job timed out")
    await worker.run_once()
    final = await storage.jobs.get(ctx, record.id)
    assert (final.status, final.attempts) == ("error", 2)


async def test_a_killed_workers_job_is_requeued_and_completed_by_another_worker(
    queue: JobQueue, storage: FakeGeneralService
):
    """Phase 4 exit criterion. Worker A takes the job and is killed mid-run (its task
    is cancelled: it records nothing and stops heartbeating). Worker B cannot take
    the job while A's lease lives, then takes it once the lease runs out and
    completes it. The durable record shows one job, two attempts, done by B."""
    ctx, _ = await storage.make_user("alice")
    a_started = asyncio.Event()
    runs: list[tuple[str, int]] = []

    def handler_for(worker_name: str):
        async def handler(job: JobContext) -> JobOutcome:
            runs.append((worker_name, job.attempt))
            if worker_name == "A":
                a_started.set()
                await asyncio.Event().wait()  # hangs until the worker is killed
            return JobOutcome(result={"by": worker_name}, cost_usd=0.04)
        return handler

    record = await queue.enqueue(ctx, "render", {"deck": "d1"})
    worker_a = Worker(queue, {"render": handler_for("A")}, worker_id="A")
    worker_b = Worker(queue, {"render": handler_for("B")}, worker_id="B")

    task_a = asyncio.create_task(worker_a.run_once())
    await asyncio.wait_for(a_started.wait(), timeout=5)
    assert (await storage.jobs.get(ctx, record.id)).status == "running"

    task_a.cancel()  # the kill
    with contextlib.suppress(asyncio.CancelledError):
        await task_a
    assert (await storage.jobs.get(ctx, record.id)).status == "running"  # A recorded nothing

    assert await worker_b.run_once() is False  # A's lease has not run out yet
    await asyncio.sleep(0.45)                  # lease_s = 0.3: it has now
    assert await worker_b.run_once() is True

    final = await storage.jobs.get(ctx, record.id)
    assert (final.status, final.result, final.attempts, final.cost_usd) == ("done", {"by": "B"}, 2, 0.04)
    assert runs == [("A", 1), ("B", 2)]
    assert await queue.pending() == 0
    assert await queue._limiter.in_flight("alice") == 0  # the slot came back


async def test_a_worker_that_vanishes_without_cleanup_is_also_recovered(queue: JobQueue, storage: FakeGeneralService):
    """The harsher kill: the lease is taken and then nothing at all runs (SIGKILL
    between lease and handler)."""
    ctx, _ = await storage.make_user("alice")
    record = await queue.enqueue(ctx, "render", {})
    assert await queue.lease("ghost") is not None

    async def ok(job: JobContext) -> JobOutcome:
        return JobOutcome(result={"recovered": True})

    await asyncio.sleep(0.45)
    assert await Worker(queue, {"render": ok}).run_once() is True
    assert (await storage.jobs.get(ctx, record.id)).result == {"recovered": True}


async def test_concurrent_workers_never_run_a_job_twice(queue: JobQueue, storage: FakeGeneralService):
    ctx, _ = await storage.make_user("alice")
    runs: list[str] = []

    async def handler(job: JobContext) -> JobOutcome:
        runs.append(job.record.id)
        await asyncio.sleep(0.05)
        return JobOutcome()

    ids = {(await queue.enqueue(ctx, "storyline", {"n": i})).id for i in range(6)}
    workers = [Worker(queue, {"storyline": handler}, worker_id=f"w{i}") for i in range(4)]
    while await queue.pending():
        await asyncio.gather(*(w.run_once() for w in workers))
    assert sorted(runs) == sorted(ids)


async def test_run_loop_stops_when_asked(queue: JobQueue, storage: FakeGeneralService):
    ctx, _ = await storage.make_user("alice")
    done = asyncio.Event()

    async def handler(job: JobContext) -> JobOutcome:
        done.set()
        return JobOutcome()

    await queue.enqueue(ctx, "storyline", {})
    stop = asyncio.Event()
    loop = asyncio.create_task(Worker(queue, {"storyline": handler}, poll_interval_s=0.01).run(stop))
    await asyncio.wait_for(done.wait(), timeout=5)
    stop.set()
    await asyncio.wait_for(loop, timeout=5)
