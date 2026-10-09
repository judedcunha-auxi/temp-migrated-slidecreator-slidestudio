"""SlideForge service: api/routes/quick. Darwin's `/api/quick-generate`, `/api/quick-status`, `/api/quick-image`.

Same path, method, request and response as `netlify/functions/quick-{generate,status,image}.ts`; contracts
`tests/contract/data/api-quick-{generate,status,image}.json`. No caller in Darwin's frontend or the Connector:
kept for external API users (plan §6.5). The `quick-generate-background` trigger is the internal
`darwin.quick_generate` job (D31), now under the global image cap (C12): see `app/core/darwin/quick.py`.

| Route | Answer | Quirks kept |
|---|---|---|
| `POST /api/quick-generate` | 202 `{jobId}` | topic truthy; `numSlides < 1` as JavaScript compares it, no upper bound; `layoutImageB64` (optional) a base64 PNG of 1 byte..4 MiB, stripped from the inputs |
| `GET /api/quick-status?jobId=` | `{status}` / `{status:"error", error}` / `{status:"done", slides:[{slideNumber,url}], warnings}` | unknown id: pending forever; someone else's: 403 |
| `GET /api/quick-image?jobId=&slide=` | `image/png`, `private, max-age=3600` | unknown id 404 "Job not found" (unlike quick-status); not done (pending OR error) 404 "Images not ready" |
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import Response

from app.api.deps import get_runtime, get_storage, require_user
from app.api.legacy import (
    darwin_route,
    destructure,
    json_response,
    legacy_router,
    query_param,
    read_json,
    required_query,
)
from app.core.darwin import quick as darwin_quick
from app.core.darwin.jobs import NOT_YOUR_JOB, json_job_status, read_owned_job, submit_job
from app.core.darwin.js import is_js_integer, js_number
from app.core.darwin.refine import decode_attachment
from app.core.errors import ApiError
from app.core.storage.ports import Forbidden, NotFound
from app.core.storyline.validation import StorylineRequestError, check_storyline_request

router = legacy_router(prefix="/api", tags=["darwin-quick"])

_NOT_GIVEN = object()
#: encodeURIComponent leaves A-Z a-z 0-9 - _ . ! ~ * ' ( ) unescaped.
_URI_SAFE = "-_.!~*'()"


def _decode_layout(value: Any) -> bytes:
    """quick-generate.ts' checks, in order: a non-empty string; 1 byte..4 MiB decoded; the PNG magic."""
    if not isinstance(value, str) or not value:
        raise ApiError(400, darwin_quick.LAYOUT_NOT_STRING)
    data = decode_attachment(value)
    if not data or len(data) > darwin_quick.MAX_LAYOUT_BYTES:
        raise ApiError(400, darwin_quick.LAYOUT_TOO_BIG)
    if data[:4] != darwin_quick.PNG_MAGIC:
        raise ApiError(400, darwin_quick.LAYOUT_NOT_PNG)
    return data


@darwin_route(router, "/quick-generate", methods=("POST",), summary="Storyline and slide images in one job: 202 {jobId}")
async def quick_generate(request: Request) -> Response:
    """Order: auth, body (malformed or null: 500), topic, numSlides, layoutImageB64, then the job."""
    user = await require_user(request)
    body = destructure(await read_json(request))  # `const {layoutImageB64, ...inputs} = null` throws: 500
    inputs = {k: v for k, v in body.items() if k != "layoutImageB64"}
    try:
        check_storyline_request(inputs)  # "Topic is required", "Slides must be at least 1": storyline.ts' rules
    except StorylineRequestError as exc:
        raise ApiError(400, str(exc)) from exc
    layout = body.get("layoutImageB64", _NOT_GIVEN)
    job_inputs: dict[str, Any] = {"inputs": inputs}
    storage = get_storage(request)
    if layout is not _NOT_GIVEN:
        png = _decode_layout(layout)
        job_inputs["layoutRef"] = (await storage.blobs.put(user.ctx, png, "image/png")).ref
    record = await submit_job(get_runtime(request).queue, user.ctx, darwin_quick.QUICK_JOB, job_inputs)
    return json_response({"jobId": record.id}, 202)


@darwin_route(router, "/quick-status", methods=("GET",), summary="Poll a quick-generate job")
async def quick_status(request: Request) -> Response:
    user = await require_user(request)
    job_id = required_query(request, "jobId", "jobId is required")
    record = await read_owned_job(get_storage(request), user.ctx, job_id, (darwin_quick.QUICK_JOB,))

    def done(r: Any) -> dict[str, Any]:
        slides = [{"slideNumber": s.get("slideNumber"),
                   "url": f"/api/quick-image?jobId={quote(job_id, safe=_URI_SAFE)}&slide={s.get('slideNumber')}"}
                  for s in darwin_quick.quick_slides(r)]
        warnings = (r.result or {}).get("warnings")
        return {"slides": slides, "warnings": warnings if isinstance(warnings, list) else []}

    return json_response(json_job_status(record, done, failure=darwin_quick.GENERATION_FAILED))



@darwin_route(router, "/quick-image", methods=("GET",), summary="One slide image (PNG) of a finished quick job")
async def quick_image(request: Request) -> Response:
    user = await require_user(request)
    job_id = query_param(request, "jobId")
    slide = js_number(query_param(request, "slide"))
    if not job_id or not is_js_integer(slide) or slide <= 0:
        raise ApiError(400, "Bad params")
    storage = get_storage(request)
    try:
        record = await storage.jobs.get(user.ctx, job_id)
    except NotFound as exc:
        raise ApiError(404, "Job not found") from exc
    except Forbidden as exc:
        raise ApiError(403, NOT_YOUR_JOB) from exc
    except ValueError as exc:  # an id the store refuses outright: no such job
        raise ApiError(404, "Job not found") from exc
    if record.type != darwin_quick.QUICK_JOB:
        raise ApiError(404, "Job not found")
    if record.status != "done":
        raise ApiError(404, "Images not ready")
    ref = next((s.get("imageRef") for s in darwin_quick.quick_slides(record) if s.get("slideNumber") == int(slide)),
               None)
    if not isinstance(ref, str):
        raise ApiError(404, "Image not found")
    try:
        data = (await storage.blobs.get(user.ctx, ref)).data
    except NotFound as exc:
        raise ApiError(404, "Image not found") from exc
    return Response(content=data, status_code=200, media_type="image/png",
                    headers={"cache-control": "private, max-age=3600"})
