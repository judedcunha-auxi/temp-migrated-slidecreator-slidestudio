"""SlideForge service: api/routes/brands. Darwin's brand routes.

Same path, method, request and response as `netlify/functions/brand*.ts` (contract:
`tests/contract/data/api-brand*.json`, `api-brands.json`). Who may do what is Darwin's
`getBrandAccess` (app/core/darwin/brands.py): personal brands are the owner's; an org brand is
editable by admins and readable by members of a mapped, VERIFIED email domain; assets live under the
brand owner's namespace.

| Route | Methods | Does |
|---|---|---|
| `/api/brands` | GET, POST, PATCH, DELETE (else 405 after auth) | list / create / patch (shallow kit merge) / delete a personal brand |
| `/api/brand-asset` | POST; every other method is GET | upload or copy a master/logo PNG; serve assets, layout previews, the default style template |
| `/api/brand-pptx` | POST (405 after auth) | multipart .pptx, 5 MiB: 202 `{jobId}` (`darwin.brand_pptx`) |
| `/api/brand-pptx-status` | any | `{status, fields?, error?}`; unknown id is pending |
| `/api/brand-archetypes` | POST (405 BEFORE auth) | copy the chosen layout PNGs to the masters, re-key the furniture |
| `/api/brand-heading` | POST (405 BEFORE auth) | move the title box (kit preview + furniture) |
| `/api/brand-extract` | POST (405 after auth) | guidelines PDF (base64, 4 MiB): 202 `{jobId}` (`darwin.brand_guidelines`) |
| `/api/brand-extract-status` | any | `{status, fields?, error?}` |
| `/api/brand-preview` | any (a body is required) | 202 `{jobId}` (`darwin.brand_preview`) |
| `/api/brand-preview-status` | any | `{status, image?, error?}` (the PNG as base64) |

The `-background` functions these triggered are internal jobs (D31): app/core/darwin/brand_jobs.py and
brand_preview.py.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from fastapi import Request
from fastapi.responses import Response
from starlette.datastructures import UploadFile

from app.api.deps import get_runtime, get_storage, require_user
from app.api.legacy import (
    MalformedJsonBody,
    check_method,
    darwin_route,
    destructure,
    effective_method,
    js_truthy,
    json_response,
    legacy_router,
    postgres_uuid,
    query_param,
    read_json,
    read_json_or_none,
    required_query,
)
from app.core.brand.kit import (
    ARCHETYPES,
    ASSET_KEY_KINDS,
    ASSET_KINDS,
    DEFAULT_STYLE_TEMPLATE,
    FURNITURE_ALL_ASSET,
    FURNITURE_ASSET,
    GUIDELINES_ASSET,
    KIT_KEY_FOR,
    MASTER_FOR,
    MASTER_KINDS,
    KitError,
    brand_asset_key,
    brand_furniture_key,
    layout_preview_asset,
    merge_kit,
    normalize_heading_placeholder,
    sanitize_kit_patch,
    valid_name,
)
from app.core.brand.legacy_mapping import furniture_for_archetypes, furniture_summary, parse_archetype_layouts
from app.core.darwin.brand_jobs import BRAND_GUIDELINES_JOB, BRAND_PPTX_JOB
from app.core.darwin.brand_preview import BRAND_PREVIEW_JOB, PREVIEW_FAILED, PREVIEW_GONE, read_preview
from app.core.darwin.brands import (
    BRAND_NOT_FOUND,
    JSON_TYPE,
    ORG_READ_ONLY,
    PDF_MAGIC,
    PDF_TYPE,
    PNG_MAGIC,
    PNG_TYPE,
    PPTX_MAGIC,
    PPTX_TYPE,
    editable_or_404,
    get_brand_access,
    read_asset,
    require_brand,
)
from app.core.darwin.caps import BRAND_EXTRACT_CAP_MESSAGE, reserve_brand_extract
from app.core.darwin.jobs import json_job_status, read_owned_job, submit_job
from app.core.darwin.js import js_is_integer, js_number, node_base64
from app.core.errors import ApiError
from app.core.storage.models import BrandCreate, BrandPatch
from app.core.storage.ports import Conflict

router = legacy_router(prefix="/api", tags=["darwin-brands"])

INVALID_JSON = "Invalid JSON body"
#: New: the store's 20-personal-brand cap (0011's RLS cap, which Darwin's API path bypassed). D33.
BRAND_LIMIT = "Brand limit reached (20 personal brands)"
ASSET_MAX_BYTES = 4 * 1024 * 1024
PPTX_MAX_BYTES = 5 * 1024 * 1024
PDF_MAX_BYTES = 4 * 1024 * 1024
#: A layout index past this cannot name a stored preview (and would not fit an asset name).
_MAX_LAYOUT_INDEX = 10**9


def _kit_error(exc: KitError) -> ApiError:
    return ApiError(400, str(exc))


def _brand_body(access_brand: Any, *, with_org: bool = True) -> dict[str, Any]:
    body = {"id": access_brand.id, "name": access_brand.name, "kit": access_brand.kit}
    if with_org:
        body["isOrg"] = access_brand.is_org
    return body


def _png(data: bytes, max_age: int) -> Response:
    return Response(content=data, status_code=200, media_type=PNG_TYPE,
                    headers={"cache-control": f"private, max-age={max_age}"})


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ------------------------------------------------------------------------------------- /brands
@darwin_route(router, "/brands", methods=("GET", "POST", "PATCH", "DELETE"),
              summary="List, create, patch or delete brands (org brands of the caller's domain included)")
async def brands(request: Request) -> Response:
    """Auth first for every method; any other method is 405 AFTER auth. POST/PATCH catch malformed
    JSON (400 "Invalid JSON body"). The kit is the raw stored jsonb, never normalised."""
    user = await require_user(request)
    storage = get_storage(request)
    method = request.method.upper()

    if method == "GET":
        visible = await storage.brands.list_visible(user.ctx)
        return json_response({"brands": [_brand_body(a.brand) for a in visible]})

    if method == "POST":
        body = await read_json_or_none(request)
        if not js_truthy(body):
            raise ApiError(400, INVALID_JSON)
        fields = body if isinstance(body, dict) else {}
        try:
            name = valid_name(fields.get("name"))
            kit: dict[str, Any] = {}
            if "kit" in fields:
                raw_kit = fields["kit"]
                if isinstance(raw_kit, dict):
                    for key in raw_kit:
                        if key in ASSET_KEY_KINDS or key == "furnitureKey":
                            raise ApiError(400, f"{key} cannot be set on create — upload the asset first, then PATCH")
                kit = sanitize_kit_patch(raw_kit, user.user_id, "unused")
        except KitError as exc:
            raise _kit_error(exc) from exc
        try:
            brand = await storage.brands.create(user.ctx, BrandCreate(name=name, kit=kit))
        except Conflict as exc:
            raise ApiError(409, BRAND_LIMIT) from exc
        return json_response(_brand_body(brand, with_org=False), 201)  # no isOrg on create

    if method == "PATCH":
        body = await read_json_or_none(request)
        if not js_truthy(body):
            raise ApiError(400, INVALID_JSON)
        fields = body if isinstance(body, dict) else {}
        brand_id = fields.get("brandId")
        if not isinstance(brand_id, str) or not brand_id:
            raise ApiError(400, "brandId is required")
        access = await get_brand_access(storage, user.ctx, brand_id)
        if access is None:
            raise ApiError(404, BRAND_NOT_FOUND)
        if not access.can_edit:
            raise ApiError(403, ORG_READ_ONLY)
        patch: dict[str, Any] = {}
        try:
            if "name" in fields:
                patch["name"] = valid_name(fields["name"])
            if "kit" in fields:
                # Canonical asset keys use the brand OWNER's id (an admin editing an org brand included).
                sanitized = sanitize_kit_patch(fields["kit"], access.brand.owner_id, access.brand.id)
                patch["kit"] = merge_kit(access.brand.kit, sanitized)
        except KitError as exc:
            raise _kit_error(exc) from exc
        if not patch:
            raise ApiError(400, "Nothing to update — provide name and/or kit")
        await storage.brands.update(user.ctx, access.brand.id, BrandPatch(**patch))
        updated = await get_brand_access(storage, user.ctx, access.brand.id)
        return json_response(_brand_body(updated.brand) if updated is not None else None)

    if method == "DELETE":
        raw_id = query_param(request, "id")
        if not raw_id:
            raise ApiError(400, "id required")
        brand_id = postgres_uuid(raw_id)  # quirk 2: no UUID guard on delete, a non-uuid id is a 500
        access = await get_brand_access(storage, user.ctx, brand_id)
        # Owner-scoped and idempotent; an org brand is never deleted here (the admin route does that).
        if access is not None and not access.brand.is_org:
            await storage.brands.delete(user.ctx, access.brand.id)
        return json_response({"ok": True})

    raise ApiError(405, "GET, POST, PATCH or DELETE only")


# ---------------------------------------------------------------------------------- /brand-asset
def _require_kind(value: Any) -> str:
    if not isinstance(value, str) or value not in ASSET_KINDS:
        raise ApiError(400, f"kind must be one of {', '.join(ASSET_KINDS)}")
    return value


@darwin_route(router, "/brand-asset", methods=("POST", "GET"),
              summary="Upload a brand PNG (POST) or read an asset, a layout preview or the default style template")
async def brand_asset(request: Request) -> Response:
    """Every method but POST acts as GET. POST with malformed JSON is 500 (uncaught `req.json()`)."""
    user = await require_user(request)
    storage = get_storage(request)

    if effective_method(request, distinct=("POST",)) == "POST":
        body = destructure(await read_json(request))
        kind = _require_kind(body.get("kind"))
        access = await require_brand(storage, user.ctx, body.get("brandId"), "edit")
        brand = access.brand
        if "fromLayoutIndex" in body:  # `!== undefined`: a null counts as given
            if kind not in MASTER_KINDS:
                raise ApiError(400, f"fromLayoutIndex only valid for {', '.join(MASTER_KINDS)}")
            index = body["fromLayoutIndex"]
            if not js_is_integer(index) or index < 0:
                raise ApiError(400, "fromLayoutIndex must be a non-negative integer")
            src = await read_asset(storage, user.ctx, brand.id, layout_preview_asset(int(index))) \
                if index < _MAX_LAYOUT_INDEX else None
            if src is None:
                raise ApiError(404, "No rendered layout at that index")
            if src[:4] != PNG_MAGIC:
                raise ApiError(422, "Rendered layout is not a PNG")
            await storage.brands.put_asset(user.ctx, brand.id, kind, src, PNG_TYPE)
            return json_response({"key": brand_asset_key(brand.owner_id, brand.id, kind)})
        b64 = body.get("b64")
        if not isinstance(b64, str) or not b64:
            raise ApiError(400, "b64 image data required")
        data = node_base64(b64)
        if not data or len(data) > ASSET_MAX_BYTES:
            raise ApiError(400, "Image must be a PNG up to 4MB")
        if data[:4] != PNG_MAGIC:
            raise ApiError(400, "Image must be a PNG")
        await storage.brands.put_asset(user.ctx, brand.id, kind, data, PNG_TYPE)
        return json_response({"key": brand_asset_key(brand.owner_id, brand.id, kind)})

    kind_param = query_param(request, "kind")
    if kind_param == "style-default":
        return json_response({"template": DEFAULT_STYLE_TEMPLATE})
    if kind_param == "layout-preview":
        access = await require_brand(storage, user.ctx, query_param(request, "brandId"), "read")
        index = js_number(query_param(request, "index"))  # a missing index is Number(null) = 0
        if not js_is_integer(index) or index < 0:
            raise ApiError(400, "index must be a non-negative integer")
        png = await read_asset(storage, user.ctx, access.brand.id, layout_preview_asset(int(index))) \
            if index < _MAX_LAYOUT_INDEX else None
        if png is None:
            raise ApiError(404, "No layout preview at that index")
        return _png(png, 300)
    kind = _require_kind(kind_param)
    access = await require_brand(storage, user.ctx, query_param(request, "brandId"), "read")
    # Darwin fell back to a pre-multi-brand per-user blob (logo/master of a personal brand) on a miss;
    # those blobs are migrated into the brand's own assets (D29), so a miss is a miss here.
    png = await read_asset(storage, user.ctx, access.brand.id, kind)
    if png is None:
        raise ApiError(404, "No asset uploaded")
    return _png(png, 60)


# ------------------------------------------------------------------------- /brand-pptx (+status)
@darwin_route(router, "/brand-pptx", methods=("POST",), summary="Import a .pptx brand template: 202 {jobId}")
async def brand_pptx(request: Request) -> Response:
    """multipart/form-data `file` (.pptx, at most 5 MiB) and an optional `brandId` the caller may edit
    (else 404, checked before the size). 405 "POST only" AFTER auth."""
    user = await require_user(request)
    check_method(request, "POST", "POST only")
    if "multipart/form-data" not in (request.headers.get("content-type") or ""):
        raise ApiError(415, "Expected multipart/form-data")
    try:
        form = await request.form(max_files=8, max_fields=64)
    except Exception as exc:  # noqa: BLE001 - req.formData().catch(() => null)
        raise ApiError(400, "Could not parse form data") from exc
    files = form.getlist("file")
    upload = files[0] if files else None  # FormData.get: the first value
    if not isinstance(upload, UploadFile):
        raise ApiError(400, "file is required")
    brand_values = form.getlist("brandId")
    brand_field = brand_values[0] if brand_values else None
    storage = get_storage(request)
    access = None
    if isinstance(brand_field, str) and brand_field:
        access = await editable_or_404(storage, user.ctx, brand_field)
    data = await upload.read(PPTX_MAX_BYTES + 1)
    if not data or len(data) > PPTX_MAX_BYTES:
        raise ApiError(400, "Template must be a .pptx file up to 5 MB")
    if data[:4] != PPTX_MAGIC:
        raise ApiError(400, "File must be a valid .pptx")
    stored = await storage.blobs.put(user.ctx, data, PPTX_TYPE)
    inputs: dict[str, Any] = {"pptxRef": stored.ref}
    if access is not None:
        inputs["brandId"] = access.brand.id
    record = await submit_job(get_runtime(request).queue, user.ctx, BRAND_PPTX_JOB, inputs)
    return json_response({"jobId": record.id}, 202)


@darwin_route(router, "/brand-pptx-status", methods=("GET",), summary="Poll a .pptx brand import")
async def brand_pptx_status(request: Request) -> Response:
    """Unknown id: 200 pending. Someone else's job: 403 "Not your job". Any method."""
    user = await require_user(request)
    job_id = required_query(request, "jobId", "jobId is required")
    record = await read_owned_job(get_storage(request), user.ctx, job_id, (BRAND_PPTX_JOB,))
    return json_response(json_job_status(record, lambda r: {"fields": (r.result or {}).get("fields") or {}},
                                         failure="PPTX extraction failed"))


# ------------------------------------------------------------------------- /brand-archetypes
@darwin_route(router, "/brand-archetypes", methods=("POST",),
              summary="Apply the cover/divider/content layout choice: masters, furniture, kit")
async def brand_archetypes(request: Request) -> Response:
    """405 BEFORE auth. All three previews are read before anything is written; not atomic after that
    (masters first, then furniture, then the kit), as Darwin."""
    check_method(request, "POST", "POST only")
    user = await require_user(request)
    storage = get_storage(request)
    body = await read_json_or_none(request)
    fields = body if isinstance(body, dict) else {}
    brand_id = fields.get("brandId")
    if not js_truthy(body) or not isinstance(brand_id, str) or not brand_id:
        raise ApiError(400, "brandId required")
    mapping = parse_archetype_layouts(fields.get("layouts"))
    if mapping is None:
        raise ApiError(400, "layouts must map cover, divider and content to layout indices")
    access = await get_brand_access(storage, user.ctx, brand_id)
    if access is None:
        raise ApiError(404, BRAND_NOT_FOUND)
    if not access.can_edit:
        raise ApiError(403, ORG_READ_ONLY)
    brand = access.brand

    pngs: dict[str, bytes] = {}
    for arch in ARCHETYPES:
        index = mapping[arch]
        src = await read_asset(storage, user.ctx, brand.id, layout_preview_asset(index)) \
            if index < _MAX_LAYOUT_INDEX else None
        if src is None:
            raise ApiError(404, f"No rendered layout at index {index} — re-import the template")
        if src[:4] != PNG_MAGIC:
            raise ApiError(422, "Rendered layout is not a PNG")
        pngs[arch] = src
    patch: dict[str, Any] = {"archetypeLayouts": mapping}
    for arch, png in pngs.items():
        await storage.brands.put_asset(user.ctx, brand.id, MASTER_FOR[arch], png, PNG_TYPE)
        patch[KIT_KEY_FOR[arch]] = brand_asset_key(brand.owner_id, brand.id, MASTER_FOR[arch])

    # Brands imported before the per-layout capture have no furniture-all: their furniture stays.
    all_text = await read_asset(storage, user.ctx, brand.id, FURNITURE_ALL_ASSET)
    furniture = furniture_for_archetypes(json.loads(all_text), mapping) if all_text is not None else None
    if furniture is not None:
        archetypes, headings = furniture_summary(furniture)
        if archetypes:
            await storage.brands.put_asset(user.ctx, brand.id, FURNITURE_ASSET, _json_bytes(furniture), JSON_TYPE)
            patch["furnitureKey"] = brand_furniture_key(brand.owner_id, brand.id)
            patch["furnitureArchetypes"] = archetypes
            patch["headingPlaceholders"] = headings
    await storage.brands.update(user.ctx, brand.id, BrandPatch(kit={**brand.kit, **patch}))
    return json_response({"kit": patch, "furnitureRebuilt": furniture is not None})


# ---------------------------------------------------------------------------- /brand-heading
def _parse_box(raw: Any) -> dict[str, Any] | None:
    """`parseBox`: four finite numbers in [0, 1], positive size, right/bottom within 1.001."""
    if not isinstance(raw, dict):
        return None
    values: list[float] = []
    for key in ("left", "top", "width", "height"):
        v = raw.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= 1:
            return None
        values.append(v)
    left, top, width, height = values
    if width <= 0 or height <= 0 or left + width > 1.001 or top + height > 1.001:
        return None
    return {"left": left, "top": top, "width": width, "height": height}


def _js_spread(value: Any) -> dict[str, Any]:
    """`{...value}` for a JSON value: an object's entries, an array's or a string's by index, else {}."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (list, str)):
        return {str(i): v for i, v in enumerate(value)}
    return {}


@darwin_route(router, "/brand-heading", methods=("POST",),
              summary="Move an archetype's title box (in-app preview and export furniture)")
async def brand_heading(request: Request) -> Response:
    """405 BEFORE auth. Keeps the captured title styling (font, colour, size); only the geometry moves."""
    check_method(request, "POST", "POST only")
    user = await require_user(request)
    storage = get_storage(request)
    body = await read_json_or_none(request)
    fields = body if isinstance(body, dict) else {}
    brand_id = fields.get("brandId")
    if not js_truthy(body) or not isinstance(brand_id, str) or not brand_id:
        raise ApiError(400, "brandId required")
    archetype = fields.get("archetype")
    if not isinstance(archetype, str) or archetype not in ARCHETYPES:
        raise ApiError(400, "archetype must be cover, divider or content")
    box = _parse_box(fields.get("box"))
    if box is None:
        raise ApiError(400, "box must be {left, top, width, height} as slide fractions")
    access = await get_brand_access(storage, user.ctx, brand_id)
    if access is None:
        raise ApiError(404, BRAND_NOT_FOUND)
    if not access.can_edit:
        raise ApiError(403, ORG_READ_ONLY)
    brand = access.brand
    kit = brand.kit

    stored = kit.get("headingPlaceholders")
    current = _js_spread(stored) if stored is not None else {}
    prev = normalize_heading_placeholder(current.get(archetype))
    heading = dict(box)
    for key in ("font", "color", "sizePt"):
        if prev is not None and prev.get(key):
            heading[key] = prev[key]
    placeholders = {**current, archetype: heading}

    furniture_updated = False
    text = await read_asset(storage, user.ctx, brand.id, FURNITURE_ASSET)
    if text is not None:
        furniture = json.loads(text)
        if furniture is None:
            raise TypeError("the stored furniture is null")  # `null.layouts`: Darwin's 500
        layouts = furniture.get("layouts") if isinstance(furniture, dict) else None
        layout = layouts.get(archetype) if isinstance(layouts, dict) else None
        if js_truthy(layout):
            if isinstance(layout, dict):
                current_ph = layout.get("placeholders")
                prev_heading = current_ph.get("heading") if isinstance(current_ph, dict) else None
                layout["placeholders"] = {**_js_spread(current_ph), "heading": {**_js_spread(prev_heading), **box}}
            elif not isinstance(layout, list):  # an array takes the property and serialises without it
                raise TypeError("the stored furniture layout is not an object")  # strict-mode assignment: 500
            await storage.brands.put_asset(user.ctx, brand.id, FURNITURE_ASSET, _json_bytes(furniture), JSON_TYPE)
            furniture_updated = True

    await storage.brands.update(user.ctx, brand.id, BrandPatch(kit={**kit, "headingPlaceholders": placeholders}))
    return json_response({"headingPlaceholders": placeholders, "furnitureUpdated": furniture_updated})


# ------------------------------------------------------------------- /brand-extract (+status)
@darwin_route(router, "/brand-extract", methods=("POST",),
              summary="Extract brand fields from a guidelines PDF: 202 {jobId}")
async def brand_extract(request: Request) -> Response:
    """405 "POST only" AFTER auth. Malformed JSON is treated as {} (400 b64), but a JSON null is 500.
    The PDF checks come BEFORE the brand check. The per-user daily cap is wired and OFF (D12)."""
    user = await require_user(request)
    check_method(request, "POST", "POST only")
    try:
        raw = await read_json(request)
    except MalformedJsonBody:
        raw = {}  # `.catch(() => ({}))`
    body = destructure(raw)
    b64 = body.get("b64")
    if not isinstance(b64, str) or not b64:
        raise ApiError(400, "b64 PDF data required")
    data = node_base64(b64)
    if not data or len(data) > PDF_MAX_BYTES:
        raise ApiError(400, "Guidelines must be a PDF up to 4MB")
    if data[:4] != PDF_MAGIC:
        raise ApiError(400, "Guidelines must be a PDF")
    storage = get_storage(request)
    runtime = get_runtime(request)
    access = None
    if "brandId" in body:  # `brandId !== undefined`: null, "" and numbers are a 404 too
        access = await editable_or_404(storage, user.ctx, body["brandId"])
    if not await reserve_brand_extract(storage.users, user.ctx, runtime.darwin):
        raise ApiError(429, BRAND_EXTRACT_CAP_MESSAGE)  # only when BRAND_EXTRACT_CAP_ENABLED (D12/D33)
    inputs: dict[str, Any]
    if access is not None:
        # Kept at the brand's guidelines slot for a later re-extraction.
        await storage.brands.put_asset(user.ctx, access.brand.id, GUIDELINES_ASSET, data, PDF_TYPE)
        inputs = {"brandId": access.brand.id}
    else:
        stored = await storage.blobs.put(user.ctx, data, PDF_TYPE)  # parked; the job deletes it
        inputs = {"pdfRef": stored.ref}
    record = await submit_job(runtime.queue, user.ctx, BRAND_GUIDELINES_JOB, inputs)
    return json_response({"jobId": record.id}, 202)


@darwin_route(router, "/brand-extract-status", methods=("GET",), summary="Poll a guidelines extraction")
async def brand_extract_status(request: Request) -> Response:
    user = await require_user(request)
    job_id = required_query(request, "jobId", "jobId is required")
    record = await read_owned_job(get_storage(request), user.ctx, job_id, (BRAND_GUIDELINES_JOB,))
    return json_response(json_job_status(record, lambda r: {"fields": (r.result or {}).get("fields") or {}},
                                         failure="Extraction failed"))


# ------------------------------------------------------------------- /brand-preview (+status)
@darwin_route(router, "/brand-preview", methods=("POST",), summary="Render a sample slide in the brand: 202 {jobId}")
async def brand_preview(request: Request) -> Response:
    """No method check; the body is required (a bodyless GET is 500). A member of an org brand gets 404."""
    user = await require_user(request)
    body = destructure(await read_json(request))
    brand_id = body.get("brandId")
    if not isinstance(brand_id, str) or not brand_id:
        raise ApiError(400, "brandId required")
    storage = get_storage(request)
    access = await editable_or_404(storage, user.ctx, brand_id)
    record = await submit_job(get_runtime(request).queue, user.ctx, BRAND_PREVIEW_JOB, {"brandId": access.brand.id})
    return json_response({"jobId": record.id}, 202)


@darwin_route(router, "/brand-preview-status", methods=("GET",), summary="Poll a brand preview: the PNG as base64")
async def brand_preview_status(request: Request) -> Response:
    user = await require_user(request)
    job_id = required_query(request, "jobId", "jobId is required")
    storage = get_storage(request)
    record = await read_owned_job(storage, user.ctx, job_id, (BRAND_PREVIEW_JOB,))
    if record is not None and record.status == "done" and (record.result or {}).get("asset"):
        png = await read_preview(storage, user.ctx, record.result)
        if png is None:
            return json_response({"status": "error", "error": PREVIEW_GONE})
        return json_response({"status": "done", "image": base64.b64encode(png).decode("ascii")})
    return json_response(json_job_status(record, lambda r: {}, failure=PREVIEW_FAILED))
