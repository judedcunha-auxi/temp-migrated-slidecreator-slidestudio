"""SlideForge service: api/routes/admin. Darwin's `/api/admin-metrics` and `/api/admin-org-brands`.

Same path, method, request and response as `netlify/functions/admin-{metrics,org-brands}.ts`
(contract: `tests/contract/data/api-admin-*.json`). Both are admin-only: `require_admin` runs first,
so a non-admin gets 401/403 before anything else (and before any 405).

* `GET /api/admin-metrics?days=`: the dashboard's nine keys, aggregated in app/core/darwin/admin.py
  from the port's admin rows. `days` is `parseInt(days ?? '30') || 30`, clamped to 1..365.
* `/api/admin-org-brands`: org brands and the email domains mapped to them. GET lists them with an
  account count per domain; POST `{action: createBrand | addDomain}`; DELETE `?domain=` (wins) or
  `?brandId=`; any other method is 405 after the admin check.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request
from fastapi.responses import Response

from app.api.deps import get_storage, require_admin
from app.api.legacy import (
    darwin_route,
    js_parse_int,
    js_truthy,
    json_response,
    legacy_router,
    query_param,
    read_json_or_none,
)
from app.core.brand.kit import KitError, valid_name
from app.core.darwin.admin import DEFAULT_DAYS, DomainError, iso, metrics_body, normalize_domain
from app.core.darwin.brands import brand_lookup_id
from app.core.errors import ApiError
from app.core.storage.models import AdminActivity, BrandCreate, CallerContext
from app.core.storage.ports import NotFound, Storage

_log = logging.getLogger(__name__)

router = legacy_router(prefix="/api", tags=["darwin-admin"])

ORG_BRAND_NOT_FOUND = "Organization brand not found"


def parse_days(raw: str | None) -> int:
    """`Math.min(Math.max(parseInt(days ?? '30', 10) || 30, 1), 365)`: 0 or junk is 30, not 1."""
    days = js_parse_int(raw if raw is not None else str(DEFAULT_DAYS)) or DEFAULT_DAYS
    return min(max(days, 1), 365)


@darwin_route(router, "/admin-metrics", methods=("GET",), summary="Admin dashboard metrics (admins only)")
async def admin_metrics(request: Request) -> Response:
    """Any method. The profiles / decks / ledger reads failing is a 500; the events and transcripts
    are best-effort (empty lists), as Darwin."""
    admin = await require_admin(request)
    storage = get_storage(request)
    days = parse_days(query_param(request, "days"))
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    overview, activity = await asyncio.gather(storage.analytics.admin_overview(admin.ctx, since=since),
                                              _activity(storage, admin.ctx, since))
    return json_response(metrics_body(overview, activity, days=days, now=now))


async def _activity(storage: Storage, ctx: CallerContext, since: datetime) -> AdminActivity:
    try:
        return await storage.analytics.admin_activity(ctx, since=since)
    except Exception:  # noqa: BLE001 - best-effort, as Darwin ignored these query errors
        _log.warning("admin-metrics: events/transcripts unavailable; answering with empty lists", exc_info=True)
        return AdminActivity(events=[], intake=[], slide_edits=[])


async def _require_org_brand(storage: Storage, ctx: CallerContext, raw: Any) -> str:
    """`requireOrgBrand`: 400 unless a non-empty string; 404 unless it names an org brand (a malformed
    id is the same 404, as Darwin's Postgres cast error was)."""
    if not isinstance(raw, str) or not raw:
        raise ApiError(400, "brandId is required")
    lookup = brand_lookup_id(raw)
    if lookup is None:
        raise ApiError(404, ORG_BRAND_NOT_FOUND)
    try:
        access = await storage.brands.get(ctx, lookup)
    except NotFound as exc:
        raise ApiError(404, ORG_BRAND_NOT_FOUND) from exc
    if not access.brand.is_org:
        raise ApiError(404, ORG_BRAND_NOT_FOUND)
    return access.brand.id


async def _brand_name(storage: Storage, ctx: CallerContext, brand_id: str) -> str:
    try:
        return (await storage.brands.get(ctx, brand_id)).brand.name
    except NotFound:
        return "another brand"


@darwin_route(router, "/admin-org-brands", methods=("GET", "POST", "DELETE"),
              summary="Org brands and the email domains mapped to them (admins only)")
async def admin_org_brands(request: Request) -> Response:
    admin = await require_admin(request)
    storage = get_storage(request)
    ctx = admin.ctx
    method = request.method.upper()

    if method == "GET":
        brands, domains = await asyncio.gather(storage.brands.list_org(ctx), storage.orgs.list_domains(ctx))
        counts = await asyncio.gather(*(storage.orgs.count_accounts(ctx, d.domain) for d in domains))
        mapped = [(d, n) for d, n in zip(domains, counts, strict=True)]
        return json_response({"brands": [{
            "id": b.id, "name": b.name, "kit": b.kit, "updatedAt": iso(b.updated_at),
            "domains": [{"domain": d.domain, "accounts": n} for d, n in mapped if d.brand_id == b.id],
        } for b in brands]})

    if method == "POST":
        body = await read_json_or_none(request)
        if not js_truthy(body):
            raise ApiError(400, "Invalid JSON body")
        fields = body if isinstance(body, dict) else {}
        action = fields.get("action")
        if action == "createBrand":
            try:
                name = valid_name(fields.get("name"))
            except KitError as exc:
                raise ApiError(400, str(exc)) from exc
            brand = await storage.brands.create_org(ctx, BrandCreate(name=name, kit={}))
            return json_response({"id": brand.id, "name": brand.name, "kit": brand.kit, "isOrg": True}, 201)
        if action == "addDomain":
            raw_id = fields.get("brandId")
            brand_id = await _require_org_brand(storage, ctx, raw_id)
            try:
                domain = normalize_domain(fields.get("domain"))
            except DomainError as exc:
                raise ApiError(400, str(exc)) from exc
            existing = next((d for d in await storage.orgs.list_domains(ctx) if d.domain == domain), None)
            if existing is not None:
                if existing.brand_id == brand_id:
                    return json_response({"domain": domain, "brandId": raw_id}, 200)
                owner = await _brand_name(storage, ctx, existing.brand_id)
                raise ApiError(409, f'{domain} is already mapped to "{owner}" — remove it there first')
            await storage.orgs.map_domain(ctx, domain, brand_id)  # a racing insert's Conflict is a 500
            return json_response({"domain": domain, "brandId": raw_id}, 201)
        raise ApiError(400, "action must be createBrand or addDomain")

    if method == "DELETE":
        unmap = query_param(request, "domain")
        delete_id = query_param(request, "brandId")
        if unmap:
            # Not normalised like POST (no '@' strip, no checks); an unmapped domain is still ok.
            await storage.orgs.unmap_domain(ctx, unmap.strip().lower())
            return json_response({"ok": True})
        if delete_id:
            brand_id = await _require_org_brand(storage, ctx, delete_id)
            await storage.brands.delete(ctx, brand_id)  # its domain mappings go with it
            return json_response({"ok": True})
        raise ApiError(400, "domain or brandId required")

    raise ApiError(405, "GET, POST or DELETE only")
