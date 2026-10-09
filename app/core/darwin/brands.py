"""Brand access for Darwin's brand routes and jobs: `getBrandAccess`, `requireBrand`, asset reads.

Darwin's rule (`_shared/db.ts: getBrandAccess`), kept by the storage port (`BrandPort.get`):

* a personal brand is its owner's alone (read and edit);
* an org brand is editable by any admin and readable (only) by members whose VERIFIED email domain is
  mapped to it (`org_brand_domains`, 0011);
* anything else, including an id that is not a UUID, is "not found": the caller cannot tell a brand
  they may not see from one that does not exist.

Assets live under the brand OWNER's namespace (for an org brand, the admin who created it), never the
caller's: the port re-derives that from the brand, so nothing here builds a storage path from a
client-supplied string. The canonical `brand/<ownerId>/<brandId>/<kind>.png` keys a kit stores
(`app/core/brand/kit.py: brand_asset_key`) only say whether an asset exists.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from app.core.errors import ApiError
from app.core.storage.models import BrandAccess, CallerContext
from app.core.storage.ports import NotFound, Storage

BRAND_ID_REQUIRED = "brandId required"
BRAND_NOT_FOUND = "Brand not found"
ORG_READ_ONLY = "Organization brands can only be edited by an admin"

PNG_MAGIC = b"\x89PNG"
PPTX_MAGIC = b"PK\x03\x04"
PDF_MAGIC = b"%PDF"
PNG_TYPE = "image/png"
JSON_TYPE = "application/json"
PDF_TYPE = "application/pdf"
PPTX_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

# `db.ts: UUID_RE`, case-insensitive. getBrandAccess answers null for anything else, before any query.
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def brand_lookup_id(value: Any) -> str | None:
    """The id to look a brand up by (lower case, as Postgres compares uuids), or None when Darwin's
    UUID guard would have answered "not found" without a query."""
    return value.lower() if isinstance(value, str) and _UUID.match(value) else None


async def get_brand_access(storage: Storage, ctx: CallerContext, brand_id: Any) -> BrandAccess | None:
    """`getBrandAccess(userId, brandId)`: the brand as this caller sees it, or None."""
    lookup = brand_lookup_id(brand_id)
    if lookup is None:
        return None
    try:
        return await storage.brands.get(ctx, lookup)
    except NotFound:
        return None


async def require_brand(storage: Storage, ctx: CallerContext, brand_id: Any,
                        mode: Literal["read", "edit"]) -> BrandAccess:
    """`brand-asset.ts: requireBrand`: 400 "brandId required" (not a non-empty string), 404 "Brand not
    found" (not visible), 403 for an org brand a member may only read, when `mode` is "edit"."""
    if not isinstance(brand_id, str) or not brand_id:
        raise ApiError(400, BRAND_ID_REQUIRED)
    access = await get_brand_access(storage, ctx, brand_id)
    if access is None:
        raise ApiError(404, BRAND_NOT_FOUND)
    if mode == "edit" and not access.can_edit:
        raise ApiError(403, ORG_READ_ONLY)
    return access


async def editable_or_404(storage: Storage, ctx: CallerContext, brand_id: Any) -> BrandAccess:
    """`!(await getBrandAccess(..))?.canEdit -> 404 "Brand not found"` (brand-pptx, brand-extract,
    brand-preview): a member of an org brand gets 404 here, not 403."""
    access = await get_brand_access(storage, ctx, brand_id)
    if access is None or not access.can_edit:
        raise ApiError(404, BRAND_NOT_FOUND)
    return access


async def read_asset(storage: Storage, ctx: CallerContext, brand_id: str, name: str) -> bytes | None:
    """A brand asset's bytes, or None when it was never stored (`readBrandAsset` -> null)."""
    try:
        return (await storage.brands.get_asset(ctx, brand_id, name)).data
    except NotFound:
        return None
