"""
SlideForge service: core/jobs/queue. The Redis job queue: enqueue, lease, heartbeat, re-queue.

Redis holds the queue (decision D6); the durable job record, which the status
routes read, is written through the storage port (core/storage/ports.JobPort).
Redis can be flushed without losing what a user sees; the port can be slow
without stalling the queue.

Keys (all under `sf:jobs:`):

    ready              ZSET  every job not yet finished; score = a sequence number
                             taken when it was last queued or leased (lowest first)
    seq                STR   that sequence (INCR), so order never depends on clocks
    job:<id>           HASH  type, owner subject, request id, attempts, limits
    lease:<id>         STR   the current lease's token, with a Redis TTL

How a lease works. A worker walks the oldest entries of `ready` and takes the first
whose `lease:<id>` it can create with SET NX PX. That one command is the lock:
exactly one worker gets it. The lease key's TTL IS the lease: a worker extends it
by heartbeating; a worker that dies (killed, OOM, lost network) stops
heartbeating, the key expires, and the job, which never left `ready`, is leased by
the next worker that looks. There is no reaper to run and nothing to repair.

Attempts are counted when a lease is taken. A job leased more than `max_attempts`
times (killed each time, or failing retryably) is finished as an error on the
next lease, with the durable record saying why.

Per-job timeout: a heartbeat past `started_at + timeout_s` is refused, so a hung
attempt loses its lease and is re-queued (and counted); the worker also cancels a
handler that runs past it (core/jobs/worker.py).

Ordering of writes, so a crash at any point is safe:
* enqueue writes the durable record, then the Redis entry; a crash between them
  leaves a "queued" record nothing will run (a known gap: a sweeper for stale
  queued records is a follow-up, listed in docs/architecture.md);
* complete writes the durable record, then removes the Redis entry; a crash
  between them re-leases a finished job, and lease() sees the terminal record and
  drops it without running it again.

The compare-and-extend in heartbeat (GET then PEXPIRE) is not atomic: a lease that
expires between the two may have its successor's TTL extended once. That costs a
re-queue delay, never a double run, because the successor's token is what counts.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.jobs.limiter import InFlightLimiter, TooManyInFlight
from app.core.redis_client import RedisStore
from app.core.storage.models import TERMINAL_JOB_STATUSES, CallerContext, JobCreate, JobPatch, JobRecord
from app.core.storage.ports import Conflict, NotFound, Storage

_log = logging.getLogger(__name__)

PREFIX = "sf:jobs:"
READY_KEY = PREFIX + "ready"
SEQ_KEY = PREFIX + "seq"
SCAN_WINDOW = 50  # how many of the oldest ready jobs one lease() call considers


@dataclass(frozen=True)
class JobType:
    """How one kind of job is run. Registered by the code that enqueues it."""

    name: str
    expensive: bool = False          # counts against the per-user in-flight limit
    max_attempts: int = 3
    timeout_s: float = 600.0         # per attempt
    lease_s: float = 30.0            # heartbeat at least every lease_s / 3
    ttl_s: int = 7 * 24 * 3600       # how long the durable record is kept

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.timeout_s <= 0 or self.lease_s <= 0:
            raise ValueError("max_attempts, timeout_s and lease_s must be positive")


@dataclass
class Lease:
    job_id: str
    token: str
    attempt: int
    job_type: JobType
    record: JobRecord
    owner: CallerContext
    started_at: float
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def deadline(self) -> float:
        return self.started_at + self.job_type.timeout_s


class LeaseLost(Exception):
    """This worker no longer holds the lease (it expired, or the job timed out)."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _job_key(job_id: str) -> str:
    return f"{PREFIX}job:{job_id}"


def _lease_key(job_id: str) -> str:
    return f"{PREFIX}lease:{job_id}"


class JobQueue:
    def __init__(
        self,
        redis: RedisStore,
        storage: Storage,
        *,
        types: dict[str, JobType] | None = None,
        limiter: InFlightLimiter | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis
        self._storage = storage
        self._types: dict[str, JobType] = dict(types or {})
        self._limiter = limiter or InFlightLimiter(redis, clock=clock)
        self._clock = clock

    def now(self) -> float:
        return self._clock()

    def register(self, job_type: JobType) -> None:
        self._types[job_type.name] = job_type

    def job_type(self, name: str) -> JobType:
        try:
            return self._types[name]
        except KeyError:
            raise ValueError(f"unknown job type {name!r}") from None

    # ----------------------------------------------------------------- enqueue
    async def enqueue(
        self,
        ctx: CallerContext,
        type_name: str,
        inputs: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> JobRecord:
        """Create the durable record and queue it. With an idempotency key already
        used for this type by this caller, the existing record, not queued again.
        Raises TooManyInFlight for an expensive type past the per-user limit."""
        jt = self.job_type(type_name)
        if idempotency_key is not None:
            # A replay is answered before an in-flight slot is taken, so a retried
            # request never gets a 429 for a job it already has, and a 429 never
            # consumes the key.
            existing = await self._storage.jobs.find_by_key(ctx, jt.name, idempotency_key)
            if existing is not None:
                if _canonical(existing.inputs) != _canonical(inputs):
                    raise Conflict("idempotency key reused with different input")
                return existing
        slot = ""
        if jt.expensive:
            slot = uuid.uuid4().hex
            if not await self._limiter.acquire(ctx.subject, slot, ttl_seconds=self._slot_ttl(jt)):
                raise TooManyInFlight(self._limiter.limit)
        try:
            record, created = await self._storage.jobs.create(
                ctx, JobCreate(type=jt.name, inputs=inputs, idempotency_key=idempotency_key, ttl_seconds=jt.ttl_s))
        except BaseException:
            if slot:
                await self._limiter.release(ctx.subject, slot)
            raise
        if not created:  # lost a race with the same key
            if slot:
                await self._limiter.release(ctx.subject, slot)
            return record
        await self._redis.hset(_job_key(record.id), {
            "type": jt.name,
            "owner_subject": ctx.subject,
            "request_id": ctx.request_id,
            "slot": slot,
            "attempts": "0",
        })
        await self._redis.zadd(READY_KEY, {record.id: await self._redis.incr(SEQ_KEY)})
        _log.info("jobs: queued %s %s", jt.name, record.id)
        return record

    @staticmethod
    def _slot_ttl(jt: JobType) -> int:
        return int(jt.max_attempts * (jt.timeout_s + jt.lease_s)) + 60

    # ------------------------------------------------------------------- lease
    async def lease(self, worker_id: str) -> Lease | None:
        """Take the oldest job nobody holds, or None when there is none."""
        for job_id in await self._redis.zrange(READY_KEY, 0, SCAN_WINDOW - 1):
            token = f"{worker_id}:{uuid.uuid4().hex}"
            jt_name = (await self._redis.hgetall(_job_key(job_id))).get("type")
            jt = self._types.get(jt_name or "")
            lease_ms = int((jt.lease_s if jt else 30.0) * 1000)
            if not await self._redis.set_if_absent(_lease_key(job_id), token, ttl_ms=lease_ms):
                continue  # someone holds it
            lease = await self._take(job_id, token, worker_id)
            if lease is not None:
                return lease
        return None

    async def _take(self, job_id: str, token: str, worker_id: str) -> Lease | None:
        """We hold the lease key; decide whether this job runs."""
        attempts = await self._redis.hincrby(_job_key(job_id), "attempts", 1)
        meta = await self._redis.hgetall(_job_key(job_id))
        jt = self._types.get(meta.get("type", ""))
        if "owner_subject" not in meta or jt is None:
            # Finished and cleaned up under us, or a type this process cannot run.
            if "owner_subject" not in meta:
                await self._forget(job_id, meta)
            else:
                _log.error("jobs: no handler registered for type %r; left for another worker", meta.get("type"))
                await self._redis.hincrby(_job_key(job_id), "attempts", -1)
                await self._redis.delete(_lease_key(job_id))
            return None
        system = CallerContext.service(meta.get("request_id") or "-")
        try:
            record = await self._storage.jobs.get(system, job_id)
        except NotFound:
            await self._forget(job_id, meta)  # the durable record is gone (TTL): nothing to run
            return None
        if record.status in TERMINAL_JOB_STATUSES:
            await self._forget(job_id, meta)  # finished before a crash cleaned Redis up
            return None
        if attempts > jt.max_attempts:
            await self._finish(job_id, meta, JobPatch(
                status="error", error=f"gave up after {jt.max_attempts} attempts", attempts=attempts - 1))
            _log.warning("jobs: %s %s exhausted its %d attempts", jt.name, job_id, jt.max_attempts)
            return None
        now = self._clock()
        # Move it behind the others, so a held job does not sit at the head of every scan.
        await self._redis.zadd(READY_KEY, {job_id: await self._redis.incr(SEQ_KEY)}, only_existing=True)
        record = await self._storage.jobs.update(system, job_id, JobPatch(status="running", attempts=attempts))
        owner = CallerContext(subject=meta["owner_subject"], request_id=meta.get("request_id") or "-")
        _log.info("jobs: %s leased %s %s (attempt %d)", worker_id, jt.name, job_id, attempts)
        return Lease(job_id=job_id, token=token, attempt=attempts, job_type=jt, record=record, owner=owner,
                     started_at=now, meta=meta)

    # --------------------------------------------------------------- heartbeat
    async def _holds(self, lease: Lease) -> bool:
        return await self._redis.get(_lease_key(lease.job_id)) == lease.token

    async def heartbeat(self, lease: Lease, *, progress: dict[str, Any] | None = None) -> None:
        """Extend the lease. LeaseLost when it is no longer ours or the attempt has
        run past its timeout."""
        if self._clock() > lease.deadline:
            raise LeaseLost("the attempt ran past its timeout")
        if not await self._holds(lease):
            raise LeaseLost("the lease expired or was taken")
        await self._redis.pexpire(_lease_key(lease.job_id), int(lease.job_type.lease_s * 1000))
        if progress is not None:
            await self._storage.jobs.update(CallerContext.service(lease.owner.request_id), lease.job_id,
                                            JobPatch(progress=progress))

    # ------------------------------------------------------------------ finish
    async def complete(self, lease: Lease, result: dict[str, Any], *, cost_usd: float = 0.0) -> JobRecord:
        if not await self._holds(lease):
            raise LeaseLost("the lease expired or was taken; the result was not recorded")
        return await self._finish(lease.job_id, lease.meta,
                                  JobPatch(status="done", result=result, cost_usd=cost_usd, error=None))

    async def fail(self, lease: Lease, error: str, *, retryable: bool = True) -> JobRecord:
        """Record a failed attempt. Retryable with attempts left: queued again at
        once. Otherwise: finished as an error."""
        if not await self._holds(lease):
            raise LeaseLost("the lease expired or was taken; the failure was not recorded")
        system = CallerContext.service(lease.owner.request_id)
        if retryable and lease.attempt < lease.job_type.max_attempts:
            record = await self._storage.jobs.update(system, lease.job_id,
                                                     JobPatch(status="queued", error=error[:2000]))
            await self._redis.delete(_lease_key(lease.job_id))
            return record
        return await self._finish(lease.job_id, lease.meta, JobPatch(status="error", error=error[:2000]))

    async def _finish(self, job_id: str, meta: dict[str, str], patch: JobPatch) -> JobRecord:
        system = CallerContext.service(meta.get("request_id") or "-")
        record = await self._storage.jobs.update(system, job_id, patch)  # durable first
        await self._forget(job_id, meta)
        return record

    async def _forget(self, job_id: str, meta: dict[str, str]) -> None:
        await self._redis.zrem(READY_KEY, job_id)
        await self._redis.delete(_job_key(job_id))
        await self._redis.delete(_lease_key(job_id))
        if meta.get("slot") and meta.get("owner_subject"):
            await self._limiter.release(meta["owner_subject"], meta["slot"])

    # ------------------------------------------------------------------- query
    async def pending(self) -> int:
        return await self._redis.zcard(READY_KEY)
