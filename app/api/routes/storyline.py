"""SlideForge service: api/routes/storyline. Darwin's `/api/storyline`, `/api/storyline-status`, `/api/intake`.

Same path, method, request and response as `netlify/functions/{storyline,storyline-status,intake}.ts`;
the contract is `tests/contract/data/api-{storyline,storyline-status,intake}.json`. Internal changes:

* `/api/storyline`: the `storyline-background` trigger is a Redis job (`storyline`); the 202 `{jobId}`
  is the durable job record's id.
* `/api/storyline-status`: reads the job record. An unknown id stays 200 `{"status":"pending"}`.
* `/api/intake`: one model call per turn (C14 fixed in the core) and JSON, not SSE (decision
  2026-10-09, option A: the frontend calls `postIntake` directly, so a turn is one request).
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import Response

from app.api.deps import get_runtime, get_storage, require_user
from app.api.legacy import darwin_route, json_response, legacy_router, read_json, read_json_or_none, required_query
from app.core.darwin.caps import INTAKE_CAP_MESSAGE, reserve_intake_turn
from app.core.darwin.jobs import json_job_status, read_owned_job, submit_job
from app.core.darwin.usage import KIND_INTAKE, record_usage_quietly
from app.core.errors import ApiError
from app.core.storyline.intake import (
    IntakeRequestError,
    check_intake_request,
    run_intake_turn,
    sanitize_brief,
    save_intake_transcript,
)
from app.core.storyline.job import GENERIC_FAILURE
from app.core.storyline.job import JOB_TYPE as STORYLINE_JOB
from app.core.storyline.validation import StorylineRequestError, check_storyline_request

router = legacy_router(prefix="/api", tags=["darwin-storyline"])


@darwin_route(router, "/storyline", methods=("POST",), summary="Draft a storyline: 202 {jobId}, poll storyline-status")
async def storyline(request: Request) -> Response:
    """Method not checked (any method with a JSON body acts as POST). Order: auth, body (malformed:
    500), topic, numSlides, enqueue."""
    user = await require_user(request)
    body = await read_json(request)
    try:
        check_storyline_request(body)
    except StorylineRequestError as exc:
        raise ApiError(400, str(exc)) from exc
    # The body is the job's input verbatim: Darwin echoes it, unknown fields included, as result.inputs.
    record = await submit_job(get_runtime(request).queue, user.ctx, STORYLINE_JOB, body)
    return json_response({"jobId": record.id}, 202)


@darwin_route(router, "/storyline-status", methods=("GET",), summary="Poll a storyline job")
async def storyline_status(request: Request) -> Response:
    """Unknown id: 200 pending (no 404). Someone else's job: 403 "Not your job"."""
    user = await require_user(request)
    job_id = required_query(request, "jobId", "jobId is required")
    record = await read_owned_job(get_storage(request), user.ctx, job_id, (STORYLINE_JOB,))
    body = json_job_status(record, lambda r: {"result": r.result or {}}, failure=GENERIC_FAILURE)
    return json_response(body)


@darwin_route(router, "/intake", methods=("POST",), summary="One intake interview turn: {message, brief}")
async def intake(request: Request) -> Response:
    """JSON `{message, brief}` (not SSE). Malformed JSON is a 400 here, not a 500 (Darwin catches it)."""
    user = await require_user(request)
    runtime = get_runtime(request)
    storage = get_storage(request)
    body = await read_json_or_none(request)
    try:
        messages = check_intake_request(body, max_chars=runtime.storyline.intake_max_transcript_chars)
    except IntakeRequestError as exc:
        raise ApiError(400, str(exc)) from exc
    fields = body if isinstance(body, dict) else {}  # check_intake_request accepts only an object
    # Off by default (INTAKE_CAP_ENABLED), as in Darwin; switched on, this 429 is new (D12/D33).
    if not await reserve_intake_turn(storage.users, user.ctx, runtime.darwin):
        raise ApiError(429, INTAKE_CAP_MESSAGE)
    brief = sanitize_brief(fields.get("brief"))
    turn = await run_intake_turn(runtime.storyline_model, messages, brief, settings=runtime.storyline)
    if turn.model_calls:
        await record_usage_quietly(storage.usage, user.ctx, model=runtime.storyline.model,
                                   cost_usd=turn.usage.cost_usd, kind=KIND_INTAKE)
    await save_intake_transcript(storage.transcripts, user.ctx, fields.get("intakeSessionKey"), messages, brief)
    return json_response(turn.wire())
