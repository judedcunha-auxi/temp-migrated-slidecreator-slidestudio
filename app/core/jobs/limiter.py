"""
SlideForge service: core/jobs/limiter. At most N expensive jobs in flight per user.

Repo standards, "Rate limits": expensive endpoints allow at most 3 jobs in flight
per user. One Redis sorted set per user: member = a slot id, score = when the slot
expires on its own (so a slot whose job died with its worker frees itself).

Acquire is add-then-count, not count-then-add: two racing requests may both be
refused (both see 4), never both admitted past the limit. Expired slots are
pruned on every acquire.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable

from app.core.redis_client import RedisStore

DEFAULT_MAX_IN_FLIGHT = 3
KEY_PREFIX = "sf:jobs:inflight:"


class TooManyInFlight(Exception):
    """The user already has the maximum number of expensive jobs in flight (429)."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"at most {limit} jobs may be in flight per user")
        self.limit = limit


def _key(subject: str) -> str:
    # The subject comes from a token: hash it rather than trust it as a key.
    return KEY_PREFIX + hashlib.sha256(subject.encode("utf-8")).hexdigest()[:32]


class InFlightLimiter:
    def __init__(
        self,
        redis: RedisStore,
        *,
        limit: int = DEFAULT_MAX_IN_FLIGHT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._redis = redis
        self.limit = limit
        self._clock = clock

    async def acquire(self, subject: str, slot: str, *, ttl_seconds: int) -> bool:
        key = _key(subject)
        now = self._clock()
        await self._redis.zremrangebyscore(key, float("-inf"), now)
        await self._redis.zadd(key, {slot: now + ttl_seconds})
        if await self._redis.zcard(key) > self.limit:
            await self._redis.zrem(key, slot)
            return False
        await self._redis.expire(key, ttl_seconds)
        return True

    async def release(self, subject: str, slot: str) -> None:
        await self._redis.zrem(_key(subject), slot)

    async def in_flight(self, subject: str) -> int:
        key = _key(subject)
        await self._redis.zremrangebyscore(key, float("-inf"), self._clock())
        return await self._redis.zcard(key)
