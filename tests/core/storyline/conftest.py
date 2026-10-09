"""Storyline tests: builders for slides and model answers, settings, and the job plumbing (fakeredis + the
General service fake). Every model call goes to `tests/fakes/storyline_model.py`; nothing is paid."""

from __future__ import annotations

from typing import Any

import fakeredis
import pytest

from app.config.storyline import StorylineSettings
from app.core.jobs.queue import JobQueue
from app.core.redis_client import RedisClient
from app.core.storyline import job as storyline_job
from tests.fakes.general_service import FakeGeneralService

CHART = {"categories": ["2024", "2025"], "series": [{"name": "Revenue", "values": [1, 2]}]}


def slide(n: int = 1, title: str = "Revenue grew 21% on three new markets", type: str = "framework",
          section: str | None = "Diagnosis", framework: str = "2x2 matrix",
          bullets: tuple[str, ...] = ("a 1", "b 2", "c 3"), **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"number": n, "title": title, "type": type, "framework": framework,
                           "description": "It argues. It matters.", "bullets": list(bullets), **extra}
    if section is not None:
        out["section"] = section
    return out


def furniture(n: int, type: str, section: str) -> dict[str, Any]:
    return slide(n, title=f"{type.title()} slide", type=type, section=section, framework=type, bullets=())


def long_deck() -> list[dict[str, Any]]:
    """Nine slides, three body sections: Diagnosis, Options, Plan (Slide Studio's fixture)."""
    return [furniture(1, "title", "Diagnosis"), furniture(2, "agenda", "Diagnosis"),
            slide(3, section="Diagnosis"), slide(4, section="Diagnosis"),
            slide(5, section="Options"), slide(6, section="Options"),
            slide(7, section="Plan"), slide(8, section="Plan"),
            furniture(9, "closing", "Plan")]


def answer(*slides: dict[str, Any], title: str = "Consolidate to one cloud", **extra: Any) -> dict[str, Any]:
    return {"presentationTitle": title, "slides": list(slides), **extra}


@pytest.fixture
def settings() -> StorylineSettings:
    """Explicit values, independent of the environment and any .env."""
    return StorylineSettings(
        model="claude-sonnet-5-5", max_tokens=64_000, effort="high", intake_max_tokens=4096, intake_effort="low",
        intake_max_assistant_turns=12, intake_max_transcript_chars=30_000, max_slides=0, job_timeout_s=30,
        job_max_attempts=2,
    )


@pytest.fixture
def redis() -> RedisClient:
    return RedisClient(fakeredis.FakeAsyncRedis(decode_responses=True))


@pytest.fixture
def storage() -> FakeGeneralService:
    return FakeGeneralService()


@pytest.fixture
def queue(redis: RedisClient, storage: FakeGeneralService, settings: StorylineSettings) -> JobQueue:
    """A queue that knows the `storyline` type, as the enqueueing process's would."""
    return JobQueue(redis, storage, types={"storyline": storyline_job.job_type(settings)})
