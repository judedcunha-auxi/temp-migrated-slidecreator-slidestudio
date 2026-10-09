"""SlideForge service: api/routes/exports. Darwin's PPTX export routes and `/api/image-to-slide`.

Same path, method, request and response as `netlify/functions/pptx-{submit,status,result}.ts`,
`pptx-deck-{submit,status,result}.ts` and `image-to-slide.ts` (contract: `tests/contract/data/`).
They proxied Slide Studio's `/v1/jobs` and `/v1/decks`; now the work is internal jobs (plan §2.1):

| Route | Job | Contract details kept |
|---|---|---|
| `POST /api/pptx-submit` | `slides.design_and_export` (image mode) | **200** (not 202) `{jobId}`; 405 before auth |
| `GET /api/pptx-status?jobId=` | (read) | `{status, done, failed}`; unknown -> 502 "PPTX service error" |
| `GET /api/pptx-result?jobId=` | (read) | `slide.pptx` bytes; unknown/unfinished -> 502 "PPTX service unavailable" |
| `POST /api/pptx-deck-submit` | `darwin.pptx_deck` (stitch) | **200** `{deckJobId}`; 405 before auth |
| `GET /api/pptx-deck-status?deckJobId=` | (read) | as pptx-status |
| `GET /api/pptx-deck-result?deckJobId=` | (read) | `deck.pptx` bytes, as pptx-result |
| `POST /api/image-to-slide` | `slides.design_and_export` (image mode) | multipart only, 4 MiB, **202** `{jobId}`; 405 "POST only" after auth |

The status and result routes have NO ownership check, as today (`# TODO-P5 ownership` in
app/core/darwin/jobs.py): any signed-in user can poll or fetch any job id, and the Connector reads a
status 502 as "still running". image-to-slide jobs are polled through `/api/pptx-status` and
`/api/pptx-result`, as before.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import Response
from starlette.datastructures import UploadFile

from app.api.deps import get_runtime, get_storage, require_user
from app.api.legacy import (
    check_method,
    darwin_route,
    destructure,
    js_parse_int,
    js_truthy,
    json_response,
    legacy_router,
    postgres_uuid,
    read_json,
    required_query,
)
from app.core.darwin import exports as darwin_exports
from app.core.darwin.jobs import (
    PPTX_CONTENT_TYPE,
    PPTX_RESULT_ERROR,
    PPTX_STATUS_ERROR,
    pptx_status_body,
    read_any_job,
    read_job_pptx,
    submit_job,
)
from app.core.errors import ApiError
from app.core.request_id import get_request_id

router = legacy_router(prefix="/api", tags=["darwin-exports"])

MISSING_PARAMS = "Missing params"
IMAGE_MAX_BYTES = 4 * 1024 * 1024
IMAGE_MIME = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
NOT_MULTIPART = 'Send the image as multipart/form-data with a "file" field'
MISSING_FILE = 'Missing "file" field'
BAD_MIME = "file must be image/png, image/jpeg, image/webp, or image/gif"


def _pptx(data: bytes, filename: str) -> Response:
    """The result routes' 200: bytes with a fixed content-type and filename, no cache-control."""
    return Response(content=data, status_code=200, media_type=PPTX_CONTENT_TYPE,
                    headers={"content-disposition": f'attachment; filename="{filename}"'})


# --------------------------------------------------------------------------------------- one slide
@darwin_route(router, "/pptx-submit", methods=("POST",), summary="Export one slide to PPTX: 200 {jobId}")
async def pptx_submit(request: Request) -> Response:
    check_method(request, "POST")  # BEFORE auth: an unauthenticated GET is 405, not 401
    user = await require_user(request)
    body = destructure(await read_json(request))  # malformed or null body: 500
    deck_id, slide, version = body.get("deckId"), body.get("slide"), body.get("version")
    if not js_truthy(deck_id) or not js_truthy(slide):
        raise ApiError(400, MISSING_PARAMS)
    slide_num = js_parse_int(slide)
    if slide_num is None or slide_num <= 0:
        raise ApiError(400, "Invalid slide")
    v = js_parse_int(version) if js_truthy(version) else 1
    version_num = v if v is not None and v >= 2 else 1
    apply_brand_layout = body.get("applyBrandLayout") is not False  # only a literal false turns it off

    storage = get_storage(request)
    deck = await darwin_exports.owned_deck(storage, user.ctx, postgres_uuid(deck_id))  # non-uuid: 500
    inputs = await darwin_exports.slide_export_inputs(storage, user.ctx, user.profile, deck, slide_num,
                                                      version_num, apply_brand_layout)
    record = await submit_job(get_runtime(request).queue, user.ctx, darwin_exports.SLIDE_JOB, inputs)
    await darwin_exports.count_export(storage, user.ctx, deck.id, [slide_num], record.id)
    return json_response({"jobId": record.id})  # 200, not 202


@darwin_route(router, "/pptx-status", methods=("GET",), summary="Poll a slide export (or image-to-slide) job")
async def pptx_status(request: Request) -> Response:
    await require_user(request)  # the user is not used: no ownership check (TODO-P5 ownership)
    job_id = required_query(request, "jobId", "Missing jobId")
    record = await read_any_job(get_storage(request), get_request_id(request), job_id,
                                 (darwin_exports.SLIDE_JOB,), message=PPTX_STATUS_ERROR)
    return json_response(pptx_status_body(record))


@darwin_route(router, "/pptx-result", methods=("GET",), summary="Download a finished slide export (slide.pptx)")
async def pptx_result(request: Request) -> Response:
    await require_user(request)  # no ownership check (TODO-P5 ownership)
    job_id = required_query(request, "jobId", "Missing jobId")
    storage = get_storage(request)
    record = await read_any_job(storage, get_request_id(request), job_id, (darwin_exports.SLIDE_JOB,),
                                message=PPTX_RESULT_ERROR)
    return _pptx(await read_job_pptx(storage, record, get_request_id(request)), "slide.pptx")


# -------------------------------------------------------------------------------------- the deck
@darwin_route(router, "/pptx-deck-submit", methods=("POST",), summary="Stitch exported slides into a deck: 200 {deckJobId}")
async def pptx_deck_submit(request: Request) -> Response:
    check_method(request, "POST")  # BEFORE auth
    user = await require_user(request)
    body = destructure(await read_json(request))
    deck_id, job_ids, title = body.get("deckId"), body.get("jobIds"), body.get("presentationTitle")
    if not js_truthy(deck_id) or not isinstance(job_ids, list) or not job_ids:
        raise ApiError(400, MISSING_PARAMS)
    storage = get_storage(request)
    deck = await darwin_exports.owned_deck(storage, user.ctx, postgres_uuid(deck_id))
    # Slide Studio's /v1/decks refused (422) ids that are not strings and a non-string title, and the
    # proxy turned that into 502 "PPTX service error" at submit time; kept.
    if not all(isinstance(j, str) for j in job_ids) or (title is not None and not isinstance(title, str)):
        raise ApiError(502, PPTX_STATUS_ERROR)
    inputs = {"deckId": deck.id, "jobIds": job_ids, "presentationTitle": title if title is not None else ""}
    record = await submit_job(get_runtime(request).queue, user.ctx, darwin_exports.DECK_JOB, inputs)
    await darwin_exports.count_export(storage, user.ctx, deck.id, [], record.id)
    return json_response({"deckJobId": record.id})  # 200, not 202


@darwin_route(router, "/pptx-deck-status", methods=("GET",), summary="Poll a deck stitch job")
async def pptx_deck_status(request: Request) -> Response:
    await require_user(request)  # no ownership check (TODO-P5 ownership)
    job_id = required_query(request, "deckJobId", "Missing deckJobId")
    record = await read_any_job(get_storage(request), get_request_id(request), job_id,
                                (darwin_exports.DECK_JOB,), message=PPTX_STATUS_ERROR)
    return json_response(pptx_status_body(record))


@darwin_route(router, "/pptx-deck-result", methods=("GET",), summary="Download a stitched deck (deck.pptx)")
async def pptx_deck_result(request: Request) -> Response:
    await require_user(request)  # no ownership check (TODO-P5 ownership)
    job_id = required_query(request, "deckJobId", "Missing deckJobId")
    storage = get_storage(request)
    record = await read_any_job(storage, get_request_id(request), job_id, (darwin_exports.DECK_JOB,),
                                message=PPTX_RESULT_ERROR)
    return _pptx(await read_job_pptx(storage, record, get_request_id(request)), "deck.pptx")


# -------------------------------------------------------------------------------- image-to-slide
@darwin_route(router, "/image-to-slide", methods=("POST",), summary="Rebuild a slide picture as native PPTX: 202 {jobId}")
async def image_to_slide(request: Request) -> Response:
    """Multipart only (a JSON body is a 400), one `file` part, PNG/JPEG/WebP/GIF, at most 4 MiB (400,
    not 413). The MIME type is the part's header, as Darwin (bytes are not sniffed)."""
    user = await require_user(request)
    check_method(request, "POST", "POST only")  # AFTER auth, with its own text
    if "multipart/form-data" not in (request.headers.get("content-type") or ""):
        raise ApiError(400, NOT_MULTIPART)
    try:
        form = await request.form(max_files=8, max_fields=64)
    except Exception:  # noqa: BLE001 - req.formData().catch(() => null)
        form = None
    upload = form.get("file") if form is not None else None
    if not isinstance(upload, UploadFile):
        raise ApiError(400, MISSING_FILE)
    mime = (upload.content_type or "").lower()
    if mime not in IMAGE_MIME:
        raise ApiError(400, BAD_MIME)
    data = await upload.read(IMAGE_MAX_BYTES + 1)
    if not data:
        raise ApiError(400, "Empty image")
    if len(data) > IMAGE_MAX_BYTES:
        raise ApiError(400, "Image must be 4 MB or smaller")
    try:
        data, name = darwin_exports.image_for_pipeline(data, mime)
    except Exception:  # noqa: BLE001 - an unreadable GIF: the job fails, as Slide Studio's job would have
        name = "slide.gif"
    stored = await get_storage(request).blobs.put(user.ctx, data, "image/png" if name.endswith(".png") else mime)
    record = await submit_job(get_runtime(request).queue, user.ctx, darwin_exports.SLIDE_JOB,
                              {"imageRef": stored.ref, "imageName": name})
    return json_response({"jobId": record.id}, 202)
