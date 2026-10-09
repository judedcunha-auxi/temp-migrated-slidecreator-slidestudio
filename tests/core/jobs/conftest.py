"""Job tests run on fakeredis (the skeleton's RedisClient over it) and the General service fake."""

from __future__ import annotations

import fakeredis
import pytest

from app.core.jobs.queue import JobQueue, JobType
from app.core.redis_client import RedisClient
from tests.fakes.general_service import FakeGeneralService


class ManualClock:
    """Seconds since the epoch, moved by hand. Redis TTLs (the lease) stay real."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def redis() -> RedisClient:
    return RedisClient(fakeredis.FakeAsyncRedis(decode_responses=True))


@pytest.fixture
def storage() -> FakeGeneralService:
    return FakeGeneralService()


@pytest.fixture
def types() -> dict[str, JobType]:
    return {
        "render": JobType("render", expensive=True, max_attempts=3, timeout_s=30, lease_s=0.3),
        "storyline": JobType("storyline", expensive=False, max_attempts=2, timeout_s=30, lease_s=0.3),
    }


@pytest.fixture
def queue(redis: RedisClient, storage: FakeGeneralService, types: dict[str, JobType]) -> JobQueue:
    return JobQueue(redis, storage, types=types)
