"""Job submit and status helpers for Darwin's routes: the shapes and quirks of its two status families.

Darwin's async routes are submit + poll pairs. The `-background` trigger and the Netlify Blob job
record become a Redis job and a durable record (`app/core/jobs`, the storage port's JobPort); the
shapes the callers poll stay exactly as they were. There are two families:

**1. The JSON status routes** (storyline-status, quick-status, brand-pptx-status,
brand-extract-status, brand-preview-status; contract INDEX quirk 4):

* an unknown job id is `200 {"status": "pending"}`, forever, never a 404 (`json_job_status`);
* a job that exists but is someone else's is `403 {"error": "Not your job"}`; ownership is checked
  only once the record exists (`read_owned_job`);
* queued or running is `{"status": "pending"}`; finished is `{"status": "done", ...route fields}`
  or `{"status": "error", "error": "..."}`. Optional keys are omitted, never null.

**2. The SlideForge-backed routes** (pptx-status / pptx-result, pptx-deck-status /
pptx-deck-result; contract INDEX quirk 5), which were proxies to Slide Studio's `/v1`:

* an unknown id, a job of another kind, or (for a result) an unfinished job is a **502**:
  "PPTX service error" on the status routes, "PPTX service unavailable" on the result routes. The
  Connector reads a status 502 as "still running" (forge.py:828-869), so it must stay a 502;
* **there is no ownership check**: any signed-in user can poll or download any job id. Kept on
  purpose (`# TODO-P5 ownership`): adding the check is a behaviour change behind decision D33, and
  someone else's job must then answer 502 too, not 403/404, or the Connector changes behaviour;
* the status body is `{status, done, failed}` with Slide Studio's vocabulary
  (queued | running | done | failed), mapped from the record (`pptx_status_body`).

**Submitting.** `submit_job` enqueues through the queue (durable record first, then Redis) and
returns the record; its `id` is the jobId the route answers with. Darwin swallowed a failed trigger
and left the job pending forever (quirk 9); here a queued job is never lost (the queue keeps it until
a worker runs it), so that quirk has nothing left to reproduce.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from app.core.errors import ApiError
from app.core.jobs.queue import JobQueue
from app.core.storage.models import CallerContext, JobRecord
from app.core.storage.ports import Forbidden, NotFound, Storage

NOT_YOUR_JOB = "Not your job"
PPTX_STATUS_ERROR = "PPTX service error"
PPTX_RESULT_ERROR = "PPTX service unavailable"
PPTX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

#: The durable record's status -> Slide Studio's (`server/compat/store.py`) on the pptx status routes.
_PPTX_STATUS = {"queued": "queued", "running": "running", "done": "done", "error": "failed", "cancelled": "failed"}


async def submit_job(queue: JobQueue, ctx: CallerContext, type_name: str, inputs: dict[str, Any]) -> JobRecord:
    """Queue a job for the caller; the record's id is the jobId the route returns."""
    return await queue.enqueue(ctx, type_name, inputs)


# ------------------------------------------------------------------------- family 1: JSON status
async def read_owned_job(storage: Storage, ctx: CallerContext, job_id: str, types: Iterable[str]) -> JobRecord | None:
    """The caller's job of one of `types`, or None for an unknown id (or a job of another kind, which
    Darwin's per-kind blob stores could not have found either). Someone else's job: 403 "Not your job"."""
    try:
        record = await storage.jobs.get(ctx, job_id)
    except NotFound:
        return None
    except Forbidden as exc:
        raise ApiError(403, NOT_YOUR_JOB) from exc
    return record if record.type in set(types) else None


def json_job_status(record: JobRecord | None, done: Callable[[JobRecord], dict[str, Any]],
                    *, failure: str = "Job failed") -> dict[str, Any]:
    """The JSON status routes' body. `done(record)` gives the route's fields for a finished job (its
    `result`, `fields`, `image`...); `failure` is the error text when the record carries none."""
    if record is None or record.status in ("queued", "running"):
        return {"status": "pending"}
    if record.status == "done":
        return {"status": "done", **done(record)}
    return {"status": "error", "error": record.error or failure}


# --------------------------------------------------------------------- family 2: pptx proxy routes
async def read_any_job(storage: Storage, request_id: str, job_id: str, types: Iterable[str], *,
                       message: str) -> JobRecord:
    """Any user's job of one of `types`, read with the service's own identity: Darwin's pptx routes
    never checked ownership. Unknown, expired, or another kind of job: 502 `message`."""
    # TODO-P5 ownership: reading as the service reproduces today's missing ownership check (quirk 5).
    # Adding the check is a D33 behaviour change; someone else's job must then answer this same 502.
    try:
        record = await storage.jobs.get(CallerContext.service(request_id), job_id)
    except NotFound as exc:
        raise ApiError(502, message) from exc
    if record.type not in set(types):
        raise ApiError(502, message)
    return record


def pptx_status_body(record: JobRecord) -> dict[str, Any]:
    status = _PPTX_STATUS.get(record.status, record.status)
    return {"status": status, "done": status == "done", "failed": status == "failed"}


async def read_job_pptx(storage: Storage, record: JobRecord, request_id: str, *, message: str = PPTX_RESULT_ERROR,
                        ref_key: str = "pptxRef") -> bytes:
    """A finished job's .pptx bytes. Not finished, or the file is gone: 502 `message` (Darwin: Slide
    Studio answered 404 before done, and the proxy turned every non-2xx into this 502)."""
    if record.status != "done" or not isinstance((record.result or {}).get(ref_key), str):
        raise ApiError(502, message)
    # TODO-P5 ownership: blobs are owner-only in the port, so the file is read as the job's owner.
    owner = CallerContext(subject=record.owner_subject, request_id=request_id)
    try:
        stored = await storage.blobs.get(owner, str((record.result or {})[ref_key]))
    except (NotFound, Forbidden) as exc:
        raise ApiError(502, message) from exc
    return stored.data
