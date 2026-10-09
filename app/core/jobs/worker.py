"""
SlideForge service: core/jobs/worker. Runs leased jobs: heartbeat, timeout, outcome.

A worker loop runs in the same image as the API (plan §2.1, "workers (same
image)"). For each job it:

1. leases it (core/jobs/queue.py);
2. runs the registered handler as a task, with the job id bound for logging;
3. heartbeats every lease_s / 3 while the handler runs; a lost lease or a passed
   per-job timeout cancels the handler;
4. records the outcome: done (with result and cost), a retryable failure (queued
   again while attempts remain), or a permanent failure.

If the worker itself is cancelled or killed, it records nothing and stops
heartbeating: the lease runs out and another worker takes the job. That is the
point: a graceful stop and a crash end the same way, with no half-written record.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.core.jobs.queue import JobQueue, Lease, LeaseLost
from app.core.logging_config import bind_job_id, bind_request_id, unbind_job_id, unbind_request_id
from app.core.storage.models import CallerContext, JobRecord

_log = logging.getLogger(__name__)


class PermanentJobError(Exception):
    """Raised by a handler for a failure a retry cannot fix (bad input). The
    message is stored on the job record: keep it free of internals."""


@dataclass
class JobOutcome:
    result: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0


@dataclass
class JobContext:
    """What a handler gets: the record, the owner (for port calls on their behalf),
    which attempt this is, and a way to report progress."""

    record: JobRecord
    owner: CallerContext
    attempt: int
    _progress: Callable[[dict[str, Any]], Awaitable[None]]

    async def progress(self, value: dict[str, Any]) -> None:
        await self._progress(value)


Handler = Callable[[JobContext], Coroutine[Any, Any, JobOutcome]]


class Worker:
    def __init__(
        self,
        queue: JobQueue,
        handlers: Mapping[str, Handler],
        *,
        worker_id: str | None = None,
        poll_interval_s: float = 1.0,
    ) -> None:
        self.queue = queue
        self.handlers = dict(handlers)
        self.worker_id = worker_id or f"w-{uuid.uuid4().hex[:8]}"
        self.poll_interval_s = poll_interval_s

    async def run(self, stop: asyncio.Event) -> None:
        """Lease and run jobs until `stop` is set."""
        while not stop.is_set():
            try:
                worked = await self.run_once()
            except Exception:  # noqa: BLE001 - one bad iteration must not end the loop
                _log.exception("jobs: worker %s iteration failed", self.worker_id)
                worked = False
            if not worked:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_interval_s)

    async def run_once(self) -> bool:
        """Run at most one job. True when one was leased."""
        lease = await self.queue.lease(self.worker_id)
        if lease is None:
            return False
        job_token = bind_job_id(lease.job_id)
        request_token = bind_request_id(lease.owner.request_id)
        try:
            await self._run(lease)
        finally:
            unbind_request_id(request_token)
            unbind_job_id(job_token)
        return True

    async def _run(self, lease: Lease) -> None:
        handler = self.handlers.get(lease.job_type.name)
        if handler is None:
            await self._record_failure(lease, "no handler for this job type", retryable=False)
            return
        async def report(value: dict[str, Any]) -> None:
            await self.queue.heartbeat(lease, progress=value)

        ctx = JobContext(record=lease.record, owner=lease.owner, attempt=lease.attempt, _progress=report)
        work: asyncio.Task[JobOutcome] = asyncio.create_task(handler(ctx), name=f"job-{lease.job_id}")
        lost: list[bool] = []
        beat = asyncio.create_task(self._heartbeat(lease, work, lost), name=f"beat-{lease.job_id}")
        try:
            remaining = max(lease.deadline - self.queue.now(), 0.0)
            try:
                outcome = await asyncio.wait_for(asyncio.shield(work), timeout=remaining)
            except TimeoutError:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                await self._record_failure(lease, "the job timed out", retryable=True)
                return
            except asyncio.CancelledError:
                me = asyncio.current_task()
                if not lost or (me is not None and me.cancelling()):
                    raise  # the worker itself is being stopped or killed
                # The heartbeat cancelled the handler: the lease is gone.
                _log.warning("jobs: lost the lease on %s; another worker will run it", lease.job_id)
                return
            except PermanentJobError as exc:
                await self._record_failure(lease, str(exc) or "the job failed", retryable=False)
                return
            except Exception as exc:  # noqa: BLE001 - a handler failure is a job outcome
                _log.exception("jobs: %s attempt %d failed", lease.job_id, lease.attempt)
                await self._record_failure(lease, f"the job failed ({type(exc).__name__})", retryable=True)
                return
            try:
                await self.queue.complete(lease, outcome.result, cost_usd=outcome.cost_usd)
            except LeaseLost:
                _log.warning("jobs: finished %s after losing its lease; the result was dropped", lease.job_id)
        finally:
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
            if not work.done():
                # Killed or stopped mid-job: stop the handler too, record nothing.
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)

    async def _heartbeat(self, lease: Lease, work: asyncio.Task[JobOutcome], lost: list[bool]) -> None:
        interval = max(lease.job_type.lease_s / 3.0, 0.01)
        while not work.done():
            await asyncio.sleep(interval)
            if work.done():
                return
            try:
                await self.queue.heartbeat(lease)
            except LeaseLost as exc:
                _log.warning("jobs: %s: %s", lease.job_id, exc)
                lost.append(True)
                work.cancel()
                return

    async def _record_failure(self, lease: Lease, error: str, *, retryable: bool) -> None:
        try:
            await self.queue.fail(lease, error, retryable=retryable)
        except LeaseLost:
            _log.warning("jobs: could not record the failure of %s: the lease is gone", lease.job_id)
