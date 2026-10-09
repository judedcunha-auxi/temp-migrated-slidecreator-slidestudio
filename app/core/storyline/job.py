"""The `storyline` job: Darwin's `storyline-background.ts` as a Redis job handler, and the
`/api/storyline-status` view of its record.

Enqueue (the `/api/storyline` route, Phase 7a): `queue.enqueue(ctx, "storyline", inputs)` with the request body
exactly as posted (Darwin echoes it, unknown fields included, as `result.inputs`), after
`validation.check_storyline_request`. The route answers 202 `{jobId: record.id}`.

The handler drafts the storyline (`service.draft_storyline`), asks the optional `SlidePrompter` for each
slide's `prompt`, and finishes the job with Darwin's result shape:

    {presentationTitle, inputs, slides: [{number, title, type, section?, framework, description, bullets,
                                          chartData?, archetypeId?, prompt?}], warnings}

Failures are recorded with a message the frontend can show (`{status: "error", error}`):
* an invalid or incomplete answer: Darwin's "Claude returned an incomplete storyline — ..." (permanent);
* a refusal: `service.REFUSED_MESSAGE` (permanent);
* a rejected request (bad credentials, bad model id): "Storyline generation failed" (permanent);
* a model outage: retried; on the last attempt "The model is busy — please try again in a moment."

`storyline_status(record)` is the status route's body: `{status: "pending"}` for a queued or running job (and,
in the route, for an unknown id), `{status: "done", result}`, `{status: "error", error}`. Optional keys are
omitted, never null.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config.storyline import StorylineSettings, storyline_settings
from app.core.jobs.queue import JobType
from app.core.jobs.registry import JobRegistry
from app.core.jobs.worker import Handler, JobContext, JobOutcome, PermanentJobError
from app.core.storage.models import CallerContext, JobRecord
from app.core.storage.ports import Forbidden, NotFound, Storage
from app.core.storyline.models import StorylineInputs
from app.core.storyline.ports import ModelRejected, ModelUnavailable, SlidePrompter, StorylineModel
from app.core.storyline.service import StorylineRefused, draft_storyline
from app.core.storyline.validation import StorylineValidationError

_log = logging.getLogger(__name__)

JOB_TYPE = "storyline"
GENERIC_FAILURE = "Storyline generation failed"
BUSY_FAILURE = "The model is busy — please try again in a moment."
NOT_YOUR_JOB = "Not your job"


class NotYourJob(Exception):
    """The job exists and belongs to someone else: the status route answers 403 "Not your job"."""


def job_type(settings: StorylineSettings | None = None) -> JobType:
    s = settings or storyline_settings
    # Expensive: a paid model call, so it counts against the per-user in-flight limit.
    return JobType(JOB_TYPE, expensive=True, max_attempts=s.job_max_attempts, timeout_s=s.job_timeout_s)


def make_handler(
    model: StorylineModel,
    *,
    settings: StorylineSettings | None = None,
    prompter: SlidePrompter | None = None,
) -> Handler:
    """The storyline handler, with its model (and the Darwin route's image-prompt builder) bound in."""
    s = settings or storyline_settings
    max_attempts = job_type(s).max_attempts

    async def handle(ctx: JobContext) -> JobOutcome:
        raw_inputs: dict[str, Any] = dict(ctx.record.inputs)
        inputs = StorylineInputs.from_wizard(raw_inputs, max_slides=s.max_slides)
        try:
            draft = await draft_storyline(model, inputs, settings=s)
        except StorylineValidationError as exc:
            raise PermanentJobError(str(exc)) from exc
        except StorylineRefused as exc:
            raise PermanentJobError(str(exc)) from exc
        except ModelRejected as exc:
            _log.error("storyline: the model call was rejected: %s", exc)
            raise PermanentJobError(GENERIC_FAILURE) from exc
        except ModelUnavailable as exc:
            if ctx.attempt >= max_attempts:
                raise PermanentJobError(BUSY_FAILURE) from exc
            raise  # retryable: the worker re-queues the job
        slides = [slide.wire() for slide in draft.storyline.slides]
        if prompter is not None:
            texts = await prompter(ctx.owner.subject, raw_inputs, slides)
            for slide, text in zip(slides, texts, strict=True):
                slide["prompt"] = text
        result = {
            "presentationTitle": draft.storyline.presentation_title,
            "inputs": raw_inputs,
            "slides": slides,
            "warnings": draft.warnings,
        }
        return JobOutcome(result=result, cost_usd=draft.usage.cost_usd)

    return handle


def register(
    registry: JobRegistry,
    model: StorylineModel,
    *,
    settings: StorylineSettings | None = None,
    prompter: SlidePrompter | None = None,
) -> None:
    """Register the `storyline` job type and its handler."""
    registry.add(job_type(settings), make_handler(model, settings=settings, prompter=prompter))


def storyline_status(record: JobRecord | None) -> dict[str, Any]:
    """The `/api/storyline-status` body for a job record (None: no such job, which Darwin reports as pending)."""
    if record is None or record.type != JOB_TYPE or record.status in ("queued", "running"):
        return {"status": "pending"}
    if record.status == "done":
        return {"status": "done", "result": record.result or {}}
    return {"status": "error", "error": record.error or GENERIC_FAILURE}


async def read_storyline_status(storage: Storage, ctx: CallerContext, job_id: str) -> dict[str, Any]:
    """Look the job up as the caller and build the status body. An unknown id is pending (Darwin's quirk);
    someone else's job raises `NotYourJob` (403)."""
    try:
        record = await storage.jobs.get(ctx, job_id)
    except NotFound:
        return {"status": "pending"}
    except Forbidden as exc:
        raise NotYourJob(NOT_YOUR_JOB) from exc
    return storyline_status(record)
