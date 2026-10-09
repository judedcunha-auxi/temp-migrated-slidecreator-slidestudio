"""
SlideForge service: core/storage/reference. The port semantics, written once.

The in-memory store (STORAGE_BACKEND=fake, and tests/fakes/general_service.py) and
the local dev adapter (STORAGE_BACKEND=local, core/storage/local_fs.py) are this
class over two different `RecordBackend`s: dictionaries, or JSON files under a
root. So ownership, the org-brand ACL, idempotency and append-only versions behave
the same in both, and the contract suite checks them once per backend.

This is a reference for what the General service must do, not a production
store: lists are scans, and there is one lock per process. The real adapter
(core/storage/general_service.py) replaces all of it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from app.core.storage.models import (
    ASSET_NAME_PATTERN,
    CONTENT_TYPE_PATTERN,
    DOMAIN_PATTERN,
    MAX_JSON_BYTES,
    TERMINAL_JOB_STATUSES,
    AdminAggregates,
    AnalyticsEvent,
    AnalyticsEventCreate,
    AssetInfo,
    Brand,
    BrandAccess,
    BrandCreate,
    BrandPatch,
    CallerContext,
    CounterBump,
    Deck,
    DeckCreate,
    DeckPatch,
    DeckSummary,
    DesignHistory,
    ExportCreate,
    ExportRecord,
    HealthReport,
    IdentityClaims,
    IntakeTranscript,
    IntakeTranscriptUpsert,
    JobCreate,
    JobPatch,
    JobRecord,
    JobStatus,
    Master,
    MasterCreate,
    OrgBrandDomain,
    Page,
    PageView,
    PageViewCreate,
    Profile,
    ProfilePatch,
    SignedLink,
    Slide,
    SlideEditTranscript,
    SlideSpec,
    SlideStatusPatch,
    SlideVersion,
    SpendSummary,
    StoredAsset,
    UsageCreate,
    UsageEvent,
    VersionCreate,
    is_valid_id,
)
from app.core.storage.ports import (
    Conflict,
    DailyCounter,
    Forbidden,
    InvalidInput,
    MasterFileKind,
    NotFound,
)

M = TypeVar("M", bound=BaseModel)

MAX_BLOB_BYTES = 100 * 1024 * 1024
MAX_PERSONAL_BRANDS = 20  # 0005/0011 insert policy
MAX_PAGE = 200
MAX_LINK_TTL_S = 3600
_ASSET_NAME = re.compile(ASSET_NAME_PATTERN)
_CONTENT_TYPE = re.compile(CONTENT_TYPE_PATTERN)
_DOMAIN = re.compile(DOMAIN_PATTERN)

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(UTC)


class RecordBackend(Protocol):
    """Where the reference store keeps JSON records and bytes. Synchronous: the
    reference store calls it under its own lock."""

    name: str

    def get(self, collection: str, key: str) -> dict[str, Any] | None: ...

    def put(self, collection: str, key: str, value: dict[str, Any]) -> None: ...

    def delete(self, collection: str, key: str) -> bool: ...

    def scan(self, collection: str) -> list[dict[str, Any]]: ...

    def put_bytes(self, ref: str, data: bytes) -> None: ...

    def get_bytes(self, ref: str) -> bytes | None: ...

    def delete_bytes(self, ref: str) -> None: ...

    def ping(self) -> HealthReport: ...


def _fingerprint(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _check_json_size(value: Any, what: str) -> None:
    if len(json.dumps(value, default=str)) > MAX_JSON_BYTES:
        raise InvalidInput(f"{what} is too large")


def _check_id(value: str, what: str = "id") -> str:
    if not isinstance(value, str) or not is_valid_id(value):
        # Never a valid object: indistinguishable from a missing one.
        raise NotFound(f"no such {what}")
    return value


def _check_content_type(content_type: str) -> str:
    if not isinstance(content_type, str) or not _CONTENT_TYPE.match(content_type) or len(content_type) > 100:
        raise InvalidInput("unsupported content type")
    return content_type


def _page(items: list[M], limit: int, cursor: str | None) -> Page[M]:
    if not 1 <= limit <= MAX_PAGE:
        raise InvalidInput(f"limit must be between 1 and {MAX_PAGE}")
    start = 0
    if cursor is not None:
        if not cursor.isdigit() or len(cursor) > 9:
            raise InvalidInput("bad cursor")
        start = int(cursor)
    chunk = items[start:start + limit]
    nxt = str(start + limit) if start + limit < len(items) else None
    return Page(items=chunk, next_cursor=nxt)


class _Core:
    """State and helpers shared by every port of one store."""

    def __init__(self, backend: RecordBackend, clock: Clock) -> None:
        self.backend = backend
        self.clock = clock
        self.lock = threading.RLock()
        self.link_secret = secrets.token_bytes(32)

    # -- records -------------------------------------------------------------
    def load(self, model: type[M], collection: str, key: str) -> M | None:
        raw = self.backend.get(collection, key)
        return None if raw is None else model.model_validate(raw)

    def save(self, collection: str, key: str, obj: BaseModel) -> None:
        self.backend.put(collection, key, obj.model_dump(mode="json"))

    def scan(self, model: type[M], collection: str) -> list[M]:
        return [model.model_validate(raw) for raw in self.backend.scan(collection)]

    def new_id(self) -> str:
        return str(uuid.uuid4())

    def today(self) -> date:
        return self.clock().astimezone(UTC).date()

    # -- callers -------------------------------------------------------------
    def me_or_none(self, ctx: CallerContext) -> Profile | None:
        if ctx.system:
            return None
        link = self.backend.get("subjects", ctx.subject)
        if link is None:
            return None
        return self.load(Profile, "profiles", str(link["user_id"]))

    def me(self, ctx: CallerContext) -> Profile:
        profile = self.me_or_none(ctx)
        if profile is None:
            raise Forbidden("the caller has no profile" if not ctx.system else "not allowed for a system caller")
        return profile

    def require_admin(self, ctx: CallerContext, *, allow_system: bool = False) -> Profile | None:
        if ctx.system:
            if allow_system:
                return None
            raise Forbidden("not allowed for a system caller")
        profile = self.me(ctx)
        if not profile.is_admin:
            raise Forbidden("admin only")
        return profile

    def admin(self, ctx: CallerContext) -> Profile:
        """The caller's profile, if they are an admin (never a system caller)."""
        profile = self.me(ctx)
        if not profile.is_admin:
            raise Forbidden("admin only")
        return profile

    # -- idempotency ---------------------------------------------------------
    def idem_key(self, user: str, op: str, key: str) -> str:
        return f"{user}:{op}:{key}"

    def idem_lookup(self, user: str, op: str, key: str | None, fingerprint: str) -> str | None:
        if key is None:
            return None
        rec = self.backend.get("idempotency", self.idem_key(user, op, key))
        if rec is None:
            return None
        if rec["fingerprint"] != fingerprint:
            raise Conflict("idempotency key reused with different input")
        return str(rec["result_id"])

    def idem_store(self, user: str, op: str, key: str | None, fingerprint: str, result_id: str) -> None:
        if key is None:
            return
        self.backend.put("idempotency", self.idem_key(user, op, key),
                         {"fingerprint": fingerprint, "result_id": result_id})

    def idem_forget(self, user: str, op: str, key: str) -> None:
        self.backend.delete("idempotency", self.idem_key(user, op, key))

    # -- bytes ---------------------------------------------------------------
    def put_bytes(self, owner_id: str, data: bytes, content_type: str) -> AssetInfo:
        _check_content_type(content_type)
        if not isinstance(data, bytes | bytearray):
            raise InvalidInput("data must be bytes")
        if len(data) > MAX_BLOB_BYTES:
            raise InvalidInput("too large")
        info = AssetInfo(ref=self.new_id(), owner_id=owner_id, content_type=content_type, size=len(data),
                         sha256=hashlib.sha256(data).hexdigest(), created_at=self.clock())
        self.backend.put_bytes(info.ref, bytes(data))
        self.save("blobs", info.ref, info)
        return info

    def get_bytes(self, ref: str) -> StoredAsset:
        info = self.load(AssetInfo, "blobs", ref)
        data = self.backend.get_bytes(ref) if info else None
        if info is None or data is None:
            raise NotFound("no such blob")
        return StoredAsset(info=info, data=data)

    def delete_bytes(self, ref: str | None) -> None:
        if not ref:
            return
        self.backend.delete_bytes(ref)
        self.backend.delete("blobs", ref)

    # -- org brands ----------------------------------------------------------
    def org_brand_ids(self, profile: Profile) -> list[str]:
        """0011 org_brand_ids_for: verified email only, exact domain match."""
        if not profile.email_verified or not profile.email or "@" not in profile.email:
            return []
        domain = profile.email.rsplit("@", 1)[1].strip().lower()
        if not domain:
            return []
        return [str(raw["brand_id"]) for raw in self.backend.scan("org_domains") if raw["domain"] == domain]

    def brand_access(self, profile: Profile, brand_id: str) -> BrandAccess | None:
        """Darwin's getBrandAccess, verbatim in meaning."""
        if not is_valid_id(brand_id):
            return None
        brand = self.load(Brand, "brands", brand_id)
        if brand is None:
            return None
        if not brand.is_org:
            return BrandAccess(brand=brand, can_edit=True) if brand.owner_id == profile.id else None
        if profile.is_admin:
            return BrandAccess(brand=brand, can_edit=True)
        if brand_id in self.org_brand_ids(profile):
            return BrandAccess(brand=brand, can_edit=False)
        return None

    def readable_brand(self, ctx: CallerContext, brand_id: str) -> tuple[Profile, BrandAccess]:
        profile = self.me(ctx)
        access = self.brand_access(profile, brand_id)
        if access is None:
            raise NotFound("no such brand")
        return profile, access

    def editable_brand(self, ctx: CallerContext, brand_id: str) -> tuple[Profile, BrandAccess]:
        profile, access = self.readable_brand(ctx, brand_id)
        if not access.can_edit:
            raise Forbidden("this brand is read-only for you")
        return profile, access

    # -- decks ---------------------------------------------------------------
    def owned_deck(self, ctx: CallerContext, deck_id: str) -> tuple[Profile, Deck]:
        profile = self.me(ctx)
        _check_id(deck_id, "deck")
        deck = self.load(Deck, "decks", deck_id)
        if deck is None or deck.owner_id != profile.id:
            raise NotFound("no such deck")
        return profile, deck

    def touch_deck(self, deck: Deck, **changes: Any) -> Deck:
        updated = deck.model_copy(update={**changes, "updated_at": self.clock(), "revision": deck.revision + 1})
        self.save("decks", deck.id, updated)
        return updated

    def slide_key(self, deck_id: str, number: int) -> str:
        return f"{deck_id}:{int(number)}"

    def owned_slide(self, ctx: CallerContext, deck_id: str, number: int) -> tuple[Profile, Deck, Slide]:
        profile, deck = self.owned_deck(ctx, deck_id)
        slide = self.load(Slide, "slides", self.slide_key(deck_id, number))
        if slide is None:
            raise NotFound("no such slide")
        return profile, deck, slide

    def bump_profile(self, profile: Profile, **deltas: int) -> Profile:
        changes = {name: getattr(profile, name) + delta for name, delta in deltas.items() if delta}
        updated = profile.model_copy(update=changes)
        self.save("profiles", profile.id, updated)
        return updated


class _Users:
    def __init__(self, core: _Core) -> None:
        self._c = core

    async def get_or_create(self, ctx: CallerContext, claims: IdentityClaims) -> Profile:
        c = self._c
        with c.lock:
            if ctx.system:
                raise Forbidden("not allowed for a system caller")
            profile = c.me_or_none(ctx)
            if profile is None:
                profile = Profile(id=c.new_id(), subject=ctx.subject, email=claims.email,
                                  email_verified=claims.email_verified, signup_method=claims.signup_method,
                                  created_at=c.clock())
                c.backend.put("subjects", ctx.subject, {"user_id": profile.id})
            else:
                profile = profile.model_copy(update={
                    "email": claims.email,
                    "email_verified": claims.email_verified,
                    "signup_method": profile.signup_method or claims.signup_method,
                })
            c.save("profiles", profile.id, profile)
            return profile

    async def get_me(self, ctx: CallerContext) -> Profile:
        with self._c.lock:
            profile = self._c.me_or_none(ctx)
            if profile is None:
                raise NotFound("no profile")
            return profile

    async def update_me(self, ctx: CallerContext, patch: ProfilePatch) -> Profile:
        c = self._c
        with c.lock:
            profile = c.me(ctx)
            if "brand_kit" in patch.model_fields_set:
                if patch.brand_kit is not None:
                    _check_json_size(patch.brand_kit, "brand_kit")
                profile = profile.model_copy(update={"brand_kit": patch.brand_kit})
            c.save("profiles", profile.id, profile)
            return profile

    async def touch_last_seen(self, ctx: CallerContext) -> None:
        c = self._c
        with c.lock:
            profile = c.me(ctx)
            c.save("profiles", profile.id, profile.model_copy(update={"last_seen_at": c.clock()}))

    async def bump_counters(self, ctx: CallerContext, bump: CounterBump) -> Profile:
        c = self._c
        with c.lock:
            profile = c.me(ctx)
            return c.bump_profile(profile, lifetime_decks=bump.decks, total_slides_generated=bump.slides_generated,
                                  total_refinements=bump.refinements, total_exports=bump.exports)

    async def reserve_daily(self, ctx: CallerContext, counter: DailyCounter, cap: int) -> bool:
        c = self._c
        field = {"decks": "decks_today", "intake_turns": "intake_turns_today",
                 "brand_extracts": "brand_extracts_today"}.get(counter)
        if field is None:
            raise InvalidInput("unknown counter")
        with c.lock:
            profile = c.me(ctx)
            today = c.today()
            count = getattr(profile, field) if getattr(profile, f"{field}_date") == today else 0
            if count >= cap:
                return False
            changes: dict[str, Any] = {field: count + 1, f"{field}_date": today}
            if counter == "decks":
                changes["lifetime_decks"] = profile.lifetime_decks + 1
            c.save("profiles", profile.id, profile.model_copy(update=changes))
            return True


class _Orgs:
    def __init__(self, core: _Core) -> None:
        self._c = core

    async def my_org_brand_ids(self, ctx: CallerContext) -> list[str]:
        with self._c.lock:
            return self._c.org_brand_ids(self._c.me(ctx))

    async def list_domains(self, ctx: CallerContext) -> list[OrgBrandDomain]:
        with self._c.lock:
            self._c.require_admin(ctx)
            return sorted(self._c.scan(OrgBrandDomain, "org_domains"), key=lambda d: d.domain)

    async def map_domain(self, ctx: CallerContext, domain: str, brand_id: str) -> OrgBrandDomain:
        c = self._c
        with c.lock:
            admin = c.admin(ctx)
            domain = (domain or "").strip().lower()
            if not _DOMAIN.match(domain) or len(domain) > 253:
                raise InvalidInput("not a domain")
            _check_id(brand_id, "brand")
            brand = c.load(Brand, "brands", brand_id)
            if brand is None:
                raise NotFound("no such brand")
            if not brand.is_org:
                raise Conflict("only an org brand can be mapped to a domain")
            existing = c.load(OrgBrandDomain, "org_domains", domain)
            if existing is not None:
                if existing.brand_id == brand_id:
                    return existing
                raise Conflict("domain is already mapped to another brand")
            row = OrgBrandDomain(domain=domain, brand_id=brand_id, created_by=admin.id, created_at=c.clock())
            c.save("org_domains", domain, row)
            return row

    async def unmap_domain(self, ctx: CallerContext, domain: str) -> None:
        c = self._c
        with c.lock:
            c.require_admin(ctx)
            domain = (domain or "").strip().lower()
            if _DOMAIN.match(domain):
                c.backend.delete("org_domains", domain)


class _Brands:
    def __init__(self, core: _Core) -> None:
        self._c = core

    async def list_visible(self, ctx: CallerContext) -> list[BrandAccess]:
        c = self._c
        with c.lock:
            profile = c.me(ctx)
            org_ids = set(c.org_brand_ids(profile))
            org: list[BrandAccess] = []
            own: list[BrandAccess] = []
            for brand in c.scan(Brand, "brands"):
                if brand.is_org and brand.id in org_ids:
                    org.append(BrandAccess(brand=brand, can_edit=profile.is_admin))
                elif not brand.is_org and brand.owner_id == profile.id:
                    own.append(BrandAccess(brand=brand, can_edit=True))
            own.sort(key=lambda a: a.brand.updated_at, reverse=True)
            org.sort(key=lambda a: a.brand.name)
            return org + own

    async def get(self, ctx: CallerContext, brand_id: str) -> BrandAccess:
        with self._c.lock:
            return self._c.readable_brand(ctx, brand_id)[1]

    def _new(self, owner_id: str, data: BrandCreate, *, is_org: bool) -> Brand:
        c = self._c
        _check_json_size(data.kit, "kit")
        now = c.clock()
        brand = Brand(id=c.new_id(), owner_id=owner_id, name=data.name, kit=data.kit, is_org=is_org,
                      created_at=now, updated_at=now)
        c.save("brands", brand.id, brand)
        return brand

    async def create(self, ctx: CallerContext, data: BrandCreate) -> Brand:
        c = self._c
        with c.lock:
            profile = c.me(ctx)
            mine = [b for b in c.scan(Brand, "brands") if b.owner_id == profile.id and not b.is_org]
            if len(mine) >= MAX_PERSONAL_BRANDS:
                raise Conflict("personal brand limit reached")
            return self._new(profile.id, data, is_org=False)

    async def create_org(self, ctx: CallerContext, data: BrandCreate) -> Brand:
        c = self._c
        with c.lock:
            admin = c.admin(ctx)
            return self._new(admin.id, data, is_org=True)

    async def update(self, ctx: CallerContext, brand_id: str, patch: BrandPatch) -> Brand:
        c = self._c
        with c.lock:
            _, access = c.editable_brand(ctx, brand_id)
            changes: dict[str, Any] = {"updated_at": c.clock()}
            if patch.name is not None:
                changes["name"] = patch.name
            if patch.kit is not None:
                _check_json_size(patch.kit, "kit")
                changes["kit"] = patch.kit
            brand = access.brand.model_copy(update=changes)
            c.save("brands", brand.id, brand)
            return brand

    async def delete(self, ctx: CallerContext, brand_id: str) -> None:
        c = self._c
        with c.lock:
            profile = c.me(ctx)
            access = c.brand_access(profile, brand_id)
            if access is None:
                return  # missing or someone else's: an owner-scoped, idempotent delete
            if not access.can_edit:
                raise Forbidden("this brand is read-only for you")
            for row in c.scan(OrgBrandDomain, "org_domains"):
                if row.brand_id == brand_id:
                    c.backend.delete("org_domains", row.domain)
            for master in c.scan(Master, "masters"):
                if master.brand_id == brand_id:
                    _Masters.drop(c, master)
            for raw in c.backend.scan("brand_assets"):
                if raw["brand_id"] == brand_id:
                    c.delete_bytes(str(raw["info"]["ref"]))
                    c.backend.delete("brand_assets", f"{brand_id}:{raw['name']}")
            c.backend.delete("brands", brand_id)

    @staticmethod
    def _check_name(name: str) -> str:
        if not isinstance(name, str) or not _ASSET_NAME.match(name):
            raise InvalidInput("not a valid asset name")
        return name

    async def put_asset(
        self, ctx: CallerContext, brand_id: str, name: str, data: bytes, content_type: str
    ) -> AssetInfo:
        c = self._c
        with c.lock:
            self._check_name(name)
            _, access = c.editable_brand(ctx, brand_id)
            key = f"{brand_id}:{name}"
            old = c.backend.get("brand_assets", key)
            info = c.put_bytes(access.brand.owner_id, data, content_type)
            c.backend.put("brand_assets", key, {"brand_id": brand_id, "name": name,
                                                "info": info.model_dump(mode="json")})
            if old is not None:
                c.delete_bytes(str(old["info"]["ref"]))
            return info

    async def get_asset(self, ctx: CallerContext, brand_id: str, name: str) -> StoredAsset:
        c = self._c
        with c.lock:
            self._check_name(name)
            c.readable_brand(ctx, brand_id)
            raw = c.backend.get("brand_assets", f"{brand_id}:{name}")
            if raw is None:
                raise NotFound("no such asset")
            return c.get_bytes(str(raw["info"]["ref"]))

    async def list_assets(self, ctx: CallerContext, brand_id: str) -> dict[str, AssetInfo]:
        c = self._c
        with c.lock:
            c.readable_brand(ctx, brand_id)
            return {str(raw["name"]): AssetInfo.model_validate(raw["info"])
                    for raw in c.backend.scan("brand_assets") if raw["brand_id"] == brand_id}

    async def delete_asset(self, ctx: CallerContext, brand_id: str, name: str) -> None:
        c = self._c
        with c.lock:
            self._check_name(name)
            c.editable_brand(ctx, brand_id)
            key = f"{brand_id}:{name}"
            raw = c.backend.get("brand_assets", key)
            if raw is not None:
                c.delete_bytes(str(raw["info"]["ref"]))
                c.backend.delete("brand_assets", key)


class _Masters:
    def __init__(self, core: _Core) -> None:
        self._c = core

    @staticmethod
    def drop(c: _Core, master: Master) -> None:
        c.delete_bytes(master.pptx_ref)
        for ref in master.layout_refs.values():
            c.delete_bytes(ref)
        c.backend.delete("masters", master.id)

    def _load(self, ctx: CallerContext, master_id: str, *, edit: bool) -> tuple[Master, BrandAccess]:
        c = self._c
        _check_id(master_id, "master")
        master = c.load(Master, "masters", master_id)
        if master is None:
            raise NotFound("no such master")
        try:
            _, access = (c.editable_brand if edit else c.readable_brand)(ctx, master.brand_id)
        except NotFound:
            raise NotFound("no such master") from None
        return master, access

    async def create(
        self, ctx: CallerContext, brand_id: str, data: MasterCreate, *, idempotency_key: str | None = None
    ) -> Master:
        c = self._c
        with c.lock:
            profile, access = c.editable_brand(ctx, brand_id)
            _check_json_size(data.manifest, "manifest")
            fp = _fingerprint({"brand": brand_id, **data.model_dump(mode="json")})
            existing_id = c.idem_lookup(profile.id, "masters.create", idempotency_key, fp)
            if existing_id is not None:
                existing = c.load(Master, "masters", existing_id)
                if existing is not None:
                    return existing
            now = c.clock()
            master = Master(id=c.new_id(), brand_id=brand_id, owner_id=access.brand.owner_id, name=data.name,
                            manifest=data.manifest, created_at=now, updated_at=now)
            c.save("masters", master.id, master)
            c.idem_store(profile.id, "masters.create", idempotency_key, fp, master.id)
            return master

    async def get(self, ctx: CallerContext, master_id: str) -> Master:
        with self._c.lock:
            return self._load(ctx, master_id, edit=False)[0]

    async def list_for_brand(self, ctx: CallerContext, brand_id: str) -> list[Master]:
        c = self._c
        with c.lock:
            c.readable_brand(ctx, brand_id)
            return sorted((m for m in c.scan(Master, "masters") if m.brand_id == brand_id),
                          key=lambda m: m.created_at)

    async def update_manifest(self, ctx: CallerContext, master_id: str, manifest: dict[str, object]) -> Master:
        c = self._c
        with c.lock:
            master, _ = self._load(ctx, master_id, edit=True)
            _check_json_size(manifest, "manifest")
            master = master.model_copy(update={"manifest": dict(manifest), "updated_at": c.clock()})
            c.save("masters", master.id, master)
            return master

    @staticmethod
    def _layout_key(kind: MasterFileKind, layout_index: int | None) -> str | None:
        if kind == "pptx":
            return None
        if kind != "layout" or layout_index is None or not 0 <= layout_index < 1000:
            raise InvalidInput("a layout file needs a layout_index from 0 to 999")
        return str(layout_index)

    async def put_file(
        self,
        ctx: CallerContext,
        master_id: str,
        kind: MasterFileKind,
        data: bytes,
        content_type: str,
        *,
        layout_index: int | None = None,
    ) -> Master:
        c = self._c
        with c.lock:
            layout = self._layout_key(kind, layout_index)
            master, access = self._load(ctx, master_id, edit=True)
            info = c.put_bytes(access.brand.owner_id, data, content_type)
            if layout is None:
                old = master.pptx_ref
                master = master.model_copy(update={"pptx_ref": info.ref, "updated_at": c.clock()})
            else:
                old = master.layout_refs.get(layout)
                master = master.model_copy(update={"layout_refs": {**master.layout_refs, layout: info.ref},
                                                   "updated_at": c.clock()})
            c.save("masters", master.id, master)
            c.delete_bytes(old)
            return master

    async def get_file(
        self, ctx: CallerContext, master_id: str, kind: MasterFileKind, *, layout_index: int | None = None
    ) -> StoredAsset:
        c = self._c
        with c.lock:
            layout = self._layout_key(kind, layout_index)
            master, _ = self._load(ctx, master_id, edit=False)
            ref = master.pptx_ref if layout is None else master.layout_refs.get(layout)
            if not ref:
                raise NotFound("no such file")
            return c.get_bytes(ref)

    async def delete(self, ctx: CallerContext, master_id: str) -> None:
        c = self._c
        with c.lock:
            try:
                master, _ = self._load(ctx, master_id, edit=True)
            except NotFound:
                return
            self.drop(c, master)


class _Decks:
    def __init__(self, core: _Core) -> None:
        self._c = core

    async def create(
        self,
        ctx: CallerContext,
        data: DeckCreate,
        slides: list[SlideSpec],
        *,
        idempotency_key: str | None = None,
    ) -> Deck:
        c = self._c
        with c.lock:
            profile = c.me(ctx)
            numbers = [s.number for s in slides]
            if len(numbers) != len(set(numbers)):
                raise InvalidInput("slide numbers must be unique")
            if len(slides) > 1000:
                raise InvalidInput("too many slides")
            _check_json_size(data.inputs, "inputs")
            _check_json_size(data.storyline, "storyline")
            if data.brand_id is not None:
                _check_id(data.brand_id, "brand")
            fp = _fingerprint({"deck": data.model_dump(mode="json"), "slides": [s.model_dump(mode="json")
                                                                                 for s in slides]})
            existing_id = c.idem_lookup(profile.id, "decks.create", idempotency_key, fp)
            if existing_id is not None:
                existing = c.load(Deck, "decks", existing_id)
                if existing is not None:
                    return existing
            now = c.clock()
            language = data.language
            if language is None and isinstance(data.inputs.get("language"), str):
                language = str(data.inputs["language"])
            deck = Deck(id=c.new_id(), owner_id=profile.id, title=data.title, inputs=data.inputs,
                        storyline=data.storyline, status=data.status, brand_id=data.brand_id, language=language,
                        creation_method=data.creation_method, slide_count=len(slides), created_at=now,
                        updated_at=now)
            c.save("decks", deck.id, deck)
            for spec in slides:
                c.save("slides", c.slide_key(deck.id, spec.number),
                       Slide(deck_id=deck.id, number=spec.number, spec=spec))
            c.idem_store(profile.id, "decks.create", idempotency_key, fp, deck.id)
            return deck

    async def get(self, ctx: CallerContext, deck_id: str) -> Deck:
        with self._c.lock:
            return self._c.owned_deck(ctx, deck_id)[1]

    async def list_mine(
        self, ctx: CallerContext, *, limit: int = 50, cursor: str | None = None
    ) -> Page[DeckSummary]:
        c = self._c
        with c.lock:
            profile = c.me(ctx)
            decks = sorted((d for d in c.scan(Deck, "decks") if d.owner_id == profile.id),
                           key=lambda d: (d.created_at, d.id), reverse=True)
            summaries = [DeckSummary(id=d.id, title=d.title, status=d.status, created_at=d.created_at)
                         for d in decks]
            return _page(summaries, limit, cursor)

    async def update(
        self, ctx: CallerContext, deck_id: str, patch: DeckPatch, *, expected_revision: int | None = None
    ) -> Deck:
        c = self._c
        with c.lock:
            _, deck = c.owned_deck(ctx, deck_id)
            if expected_revision is not None and expected_revision != deck.revision:
                raise Conflict("the deck has changed since it was read")
            changes = {k: getattr(patch, k) for k in patch.model_fields_set}
            if "storyline" in changes:
                _check_json_size(changes["storyline"], "storyline")
            return c.touch_deck(deck, **changes)

    async def delete(self, ctx: CallerContext, deck_id: str) -> None:
        c = self._c
        with c.lock:
            try:
                _, deck = c.owned_deck(ctx, deck_id)
            except NotFound:
                return
            for raw in c.backend.scan("slides"):
                if raw["deck_id"] == deck.id:
                    c.backend.delete("slides", c.slide_key(deck.id, int(raw["number"])))
            for export in c.scan(ExportRecord, "exports"):
                if export.deck_id == deck.id:
                    c.backend.delete("exports", export.id)
            for raw in c.backend.scan("slide_edits"):
                if raw["deck_id"] == deck.id:
                    c.backend.delete("slide_edits", c.slide_key(deck.id, int(raw["slide_number"])))
            c.backend.delete("design_history", deck.id)
            for transcript in c.scan(IntakeTranscript, "intake"):
                if transcript.deck_id == deck.id:  # on delete set null (0008)
                    c.save("intake", f"{transcript.user_id}:{transcript.session_key}",
                           transcript.model_copy(update={"deck_id": None}))
            c.backend.delete("decks", deck.id)


class _Slides:
    def __init__(self, core: _Core) -> None:
        self._c = core

    async def list_for_deck(self, ctx: CallerContext, deck_id: str) -> list[Slide]:
        c = self._c
        with c.lock:
            _, deck = c.owned_deck(ctx, deck_id)
            return sorted((s for s in c.scan(Slide, "slides") if s.deck_id == deck.id), key=lambda s: s.number)

    async def get(self, ctx: CallerContext, deck_id: str, number: int) -> Slide:
        with self._c.lock:
            return self._c.owned_slide(ctx, deck_id, number)[2]

    async def update_status(self, ctx: CallerContext, deck_id: str, number: int, patch: SlideStatusPatch) -> Slide:
        c = self._c
        with c.lock:
            _, _, slide = c.owned_slide(ctx, deck_id, number)
            changes: dict[str, Any] = {}
            for name in patch.model_fields_set:
                value = getattr(patch, name)
                if name == "prompt":
                    changes["spec"] = slide.spec.model_copy(update={"prompt": value or ""})
                else:
                    changes[name] = value
            slide = slide.model_copy(update=changes)
            c.save("slides", c.slide_key(deck_id, number), slide)
            return slide

    async def append_version(
        self,
        ctx: CallerContext,
        deck_id: str,
        number: int,
        data: VersionCreate,
        *,
        idempotency_key: str | None = None,
    ) -> SlideVersion:
        c = self._c
        with c.lock:
            profile, _, slide = c.owned_slide(ctx, deck_id, number)
            op = f"slides.append_version:{deck_id}:{int(number)}"
            fp = _fingerprint(data.model_dump(mode="json"))
            existing_v = c.idem_lookup(profile.id, op, idempotency_key, fp)
            if existing_v is not None:
                for version in slide.versions:
                    if version.v == int(existing_v):
                        return version
            v = max((x.v for x in slide.versions), default=0) + 1
            version = SlideVersion(v=v, mode=data.mode, html_ref=data.html_ref, image_ref=data.image_ref,
                                   instruction=data.instruction, created_at=c.clock(), cost_usd=data.cost_usd)
            current = v if data.make_current or slide.current is None else slide.current
            slide = slide.model_copy(update={"versions": [*slide.versions, version], "current": current,
                                             "status": "done", "error": None})
            c.save("slides", c.slide_key(deck_id, number), slide)
            c.idem_store(profile.id, op, idempotency_key, fp, str(v))
            return version

    async def set_current(self, ctx: CallerContext, deck_id: str, number: int, v: int) -> Slide:
        c = self._c
        with c.lock:
            _, _, slide = c.owned_slide(ctx, deck_id, number)
            if not any(x.v == v for x in slide.versions):
                raise NotFound("no such version")
            slide = slide.model_copy(update={"current": v})
            c.save("slides", c.slide_key(deck_id, number), slide)
            return slide

    async def record_refinement(self, ctx: CallerContext, deck_id: str, number: int) -> Slide:
        c = self._c
        with c.lock:
            profile, deck, slide = c.owned_slide(ctx, deck_id, number)
            slide = slide.model_copy(update={"refine_count": slide.refine_count + 1,
                                             "last_refined_at": c.clock()})
            c.save("slides", c.slide_key(deck_id, number), slide)
            c.touch_deck(deck, refine_count=deck.refine_count + 1)
            c.bump_profile(profile, total_refinements=1)
            return slide


class _Blobs:
    def __init__(self, core: _Core) -> None:
        self._c = core

    def _owned(self, ctx: CallerContext, ref: str) -> AssetInfo:
        c = self._c
        profile = c.me(ctx)
        _check_id(ref, "blob")
        info = c.load(AssetInfo, "blobs", ref)
        if info is None or info.owner_id != profile.id:
            raise NotFound("no such blob")
        return info

    async def put(self, ctx: CallerContext, data: bytes, content_type: str) -> AssetInfo:
        c = self._c
        with c.lock:
            return c.put_bytes(c.me(ctx).id, data, content_type)

    async def get(self, ctx: CallerContext, ref: str) -> StoredAsset:
        with self._c.lock:
            self._owned(ctx, ref)
            return self._c.get_bytes(ref)

    async def link(self, ctx: CallerContext, ref: str, *, ttl_seconds: int = 300) -> SignedLink:
        c = self._c
        with c.lock:
            if not 1 <= ttl_seconds <= MAX_LINK_TTL_S:
                raise InvalidInput(f"ttl_seconds must be between 1 and {MAX_LINK_TTL_S}")
            self._owned(ctx, ref)
            expires = c.clock() + timedelta(seconds=ttl_seconds)
            stamp = str(int(expires.timestamp()))
            sig = hmac.new(c.link_secret, f"{ref}:{stamp}".encode(), hashlib.sha256).hexdigest()
            return SignedLink(url=f"{c.backend.name}://blobs/{ref}?expires={stamp}&sig={sig}", expires_at=expires)

    async def delete(self, ctx: CallerContext, ref: str) -> None:
        c = self._c
        with c.lock:
            try:
                self._owned(ctx, ref)
            except NotFound:
                return
            c.delete_bytes(ref)


class _Jobs:
    def __init__(self, core: _Core) -> None:
        self._c = core

    def _live(self, job_id: str) -> JobRecord | None:
        c = self._c
        if not is_valid_id(job_id):
            return None
        job = c.load(JobRecord, "jobs", job_id)
        if job is not None and job.expires_at <= c.clock():
            c.backend.delete("jobs", job_id)
            return None
        return job

    async def create(self, ctx: CallerContext, data: JobCreate) -> tuple[JobRecord, bool]:
        c = self._c
        with c.lock:
            profile = c.me(ctx)
            _check_json_size(data.inputs, "inputs")
            op = f"jobs.create:{data.type}"
            fp = _fingerprint(data.inputs)
            existing_id = c.idem_lookup(profile.id, op, data.idempotency_key, fp)
            if existing_id is not None:
                existing = self._live(existing_id)
                if existing is not None:
                    return existing, False
            now = c.clock()
            job = JobRecord(id=c.new_id(), type=data.type, owner_id=profile.id, owner_subject=profile.subject,
                            inputs=data.inputs, idempotency_key=data.idempotency_key, request_id=ctx.request_id,
                            created_at=now, updated_at=now, expires_at=now + timedelta(seconds=data.ttl_seconds))
            c.save("jobs", job.id, job)
            c.idem_store(profile.id, op, data.idempotency_key, fp, job.id)
            return job, True

    def _authorised(self, ctx: CallerContext, job_id: str) -> JobRecord:
        job = self._live(job_id)
        if job is None:
            raise NotFound("no such job")
        if not ctx.system and job.owner_id != self._c.me(ctx).id:
            raise Forbidden("not your job")
        return job

    async def get(self, ctx: CallerContext, job_id: str) -> JobRecord:
        with self._c.lock:
            return self._authorised(ctx, job_id)

    async def find_by_key(self, ctx: CallerContext, type: str, idempotency_key: str) -> JobRecord | None:
        c = self._c
        with c.lock:
            profile = c.me(ctx)
            rec = c.backend.get("idempotency", c.idem_key(profile.id, f"jobs.create:{type}", idempotency_key))
            return None if rec is None else self._live(str(rec["result_id"]))

    async def update(
        self, ctx: CallerContext, job_id: str, patch: JobPatch, *, expected_revision: int | None = None
    ) -> JobRecord:
        c = self._c
        with c.lock:
            job = self._authorised(ctx, job_id)
            if expected_revision is not None and expected_revision != job.revision:
                raise Conflict("the job has changed since it was read")
            changes = {k: getattr(patch, k) for k in patch.model_fields_set}
            if job.status in TERMINAL_JOB_STATUSES and changes.get("status", job.status) != job.status:
                raise Conflict("the job has already finished")
            for name in ("progress", "result"):
                if changes.get(name) is not None:
                    _check_json_size(changes[name], name)
            job = job.model_copy(update={**changes, "updated_at": c.clock(), "revision": job.revision + 1})
            c.save("jobs", job.id, job)
            return job

    async def list_mine(
        self,
        ctx: CallerContext,
        *,
        type: str | None = None,
        status: JobStatus | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[JobRecord]:
        c = self._c
        with c.lock:
            profile = c.me(ctx)
            now = c.clock()
            jobs = [j for j in c.scan(JobRecord, "jobs")
                    if j.owner_id == profile.id and j.expires_at > now
                    and (type is None or j.type == type) and (status is None or j.status == status)]
            jobs.sort(key=lambda j: (j.created_at, j.id), reverse=True)
            return _page(jobs, limit, cursor)

    async def purge_expired(self, ctx: CallerContext) -> int:
        c = self._c
        with c.lock:
            if not ctx.system:
                raise Forbidden("system caller only")
            now = c.clock()
            expired = [j.id for j in c.scan(JobRecord, "jobs") if j.expires_at <= now]
            for job_id in expired:
                c.backend.delete("jobs", job_id)
            return len(expired)


class _Exports:
    def __init__(self, core: _Core) -> None:
        self._c = core

    async def record(
        self, ctx: CallerContext, data: ExportCreate, *, idempotency_key: str | None = None
    ) -> ExportRecord:
        c = self._c
        with c.lock:
            profile, deck = c.owned_deck(ctx, data.deck_id)
            fp = _fingerprint(data.model_dump(mode="json"))
            existing_id = c.idem_lookup(profile.id, "exports.record", idempotency_key, fp)
            if existing_id is not None:
                existing = c.load(ExportRecord, "exports", existing_id)
                if existing is not None:
                    return existing
            now = c.clock()
            record = ExportRecord(id=c.new_id(), deck_id=deck.id, owner_id=profile.id, kind=data.kind,
                                  slide_numbers=data.slide_numbers, file_ref=data.file_ref, job_id=data.job_id,
                                  cost_usd=data.cost_usd, created_at=now)
            c.save("exports", record.id, record)
            c.touch_deck(deck, export_count=deck.export_count + 1,
                         first_exported_at=deck.first_exported_at or now)
            c.bump_profile(profile, total_exports=1)
            c.idem_store(profile.id, "exports.record", idempotency_key, fp, record.id)
            return record

    async def list_for_deck(self, ctx: CallerContext, deck_id: str) -> list[ExportRecord]:
        c = self._c
        with c.lock:
            _, deck = c.owned_deck(ctx, deck_id)
            return sorted((e for e in c.scan(ExportRecord, "exports") if e.deck_id == deck.id),
                          key=lambda e: e.created_at)


def _summarise(events: Iterable[UsageEvent], day: date) -> SpendSummary:
    picked = [e for e in events if e.created_at.astimezone(UTC).date() == day]
    return SpendSummary(day=day, events=len(picked), cost_usd=round(sum(e.est_cost_usd for e in picked), 4))


class _Usage:
    def __init__(self, core: _Core) -> None:
        self._c = core

    async def append(
        self, ctx: CallerContext, data: UsageCreate, *, idempotency_key: str | None = None
    ) -> UsageEvent:
        c = self._c
        with c.lock:
            profile = c.me(ctx)
            fp = _fingerprint(data.model_dump(mode="json"))
            existing_id = c.idem_lookup(profile.id, "usage.append", idempotency_key, fp)
            if existing_id is not None:
                existing = c.load(UsageEvent, "usage", existing_id)
                if existing is not None:
                    return existing
            event = UsageEvent(id=c.new_id(), user_id=profile.id, deck_id=data.deck_id,
                               slide_number=data.slide_number, kind=data.kind, model=data.model,
                               est_cost_usd=round(data.est_cost_usd, 4), request_id=ctx.request_id,
                               created_at=c.clock())
            c.save("usage", event.id, event)
            c.idem_store(profile.id, "usage.append", idempotency_key, fp, event.id)
            return event

    async def spend_for_user(self, ctx: CallerContext, day: date) -> SpendSummary:
        c = self._c
        with c.lock:
            profile = c.me(ctx)
            return _summarise((e for e in c.scan(UsageEvent, "usage") if e.user_id == profile.id), day)

    async def global_spend(self, ctx: CallerContext, day: date) -> SpendSummary:
        c = self._c
        with c.lock:
            c.require_admin(ctx, allow_system=True)
            return _summarise(c.scan(UsageEvent, "usage"), day)


class _Transcripts:
    def __init__(self, core: _Core) -> None:
        self._c = core

    async def upsert_intake(self, ctx: CallerContext, data: IntakeTranscriptUpsert) -> IntakeTranscript:
        c = self._c
        with c.lock:
            profile = c.me(ctx)
            _check_json_size(data.messages, "messages")
            key = f"{profile.id}:{data.session_key}"
            now = c.clock()
            existing = c.load(IntakeTranscript, "intake", key)
            row = IntakeTranscript(user_id=profile.id, session_key=data.session_key,
                                   deck_id=existing.deck_id if existing else None, messages=data.messages,
                                   brief=data.brief, turn_count=data.turn_count, total_chars=data.total_chars,
                                   created_at=existing.created_at if existing else now, updated_at=now,
                                   completed_at=existing.completed_at if existing else None)
            c.save("intake", key, row)
            return row

    async def get_intake(self, ctx: CallerContext, session_key: str) -> IntakeTranscript:
        c = self._c
        with c.lock:
            row = c.load(IntakeTranscript, "intake", f"{c.me(ctx).id}:{session_key}")
            if row is None:
                raise NotFound("no such transcript")
            return row

    async def link_intake_to_deck(self, ctx: CallerContext, session_key: str, deck_id: str) -> IntakeTranscript:
        c = self._c
        with c.lock:
            profile, deck = c.owned_deck(ctx, deck_id)
            key = f"{profile.id}:{session_key}"
            row = c.load(IntakeTranscript, "intake", key)
            if row is None:
                raise NotFound("no such transcript")
            row = row.model_copy(update={"deck_id": deck.id, "completed_at": c.clock()})
            c.save("intake", key, row)
            return row

    async def upsert_slide_edit(
        self, ctx: CallerContext, deck_id: str, slide_number: int, messages: list[object], refine_count: int
    ) -> SlideEditTranscript:
        c = self._c
        with c.lock:
            profile, deck = c.owned_deck(ctx, deck_id)
            if not 1 <= slide_number <= 1000 or refine_count < 0:
                raise InvalidInput("slide_number or refine_count out of range")
            _check_json_size(messages, "messages")
            key = c.slide_key(deck.id, slide_number)
            existing = c.load(SlideEditTranscript, "slide_edits", key)
            now = c.clock()
            row = SlideEditTranscript(user_id=profile.id, deck_id=deck.id, slide_number=slide_number,
                                      messages=list(messages), refine_count=refine_count,
                                      created_at=existing.created_at if existing else now, updated_at=now)
            c.save("slide_edits", key, row)
            return row

    async def get_slide_edit(self, ctx: CallerContext, deck_id: str, slide_number: int) -> SlideEditTranscript:
        c = self._c
        with c.lock:
            _, deck = c.owned_deck(ctx, deck_id)
            row = c.load(SlideEditTranscript, "slide_edits", c.slide_key(deck.id, slide_number))
            if row is None:
                raise NotFound("no such transcript")
            return row

    async def put_design_history(self, ctx: CallerContext, deck_id: str, messages: list[object]) -> DesignHistory:
        c = self._c
        with c.lock:
            profile, deck = c.owned_deck(ctx, deck_id)
            _check_json_size(messages, "messages")
            row = DesignHistory(deck_id=deck.id, owner_id=profile.id, messages=list(messages), updated_at=c.clock())
            c.save("design_history", deck.id, row)
            return row

    async def get_design_history(self, ctx: CallerContext, deck_id: str) -> DesignHistory:
        c = self._c
        with c.lock:
            _, deck = c.owned_deck(ctx, deck_id)
            row = c.load(DesignHistory, "design_history", deck.id)
            if row is None:
                raise NotFound("no design history")
            return row


class _Analytics:
    def __init__(self, core: _Core) -> None:
        self._c = core

    async def record_event(self, ctx: CallerContext, data: AnalyticsEventCreate) -> AnalyticsEvent:
        c = self._c
        with c.lock:
            profile = c.me(ctx)
            _check_json_size(data.properties, "properties")
            event = AnalyticsEvent(id=c.new_id(), user_id=profile.id, created_at=c.clock(),
                                   **data.model_dump())
            c.save("analytics", event.id, event)
            return event

    async def record_page_view(self, ctx: CallerContext, data: PageViewCreate) -> PageView:
        c = self._c
        with c.lock:
            profile = c.me(ctx)
            now = c.clock()
            view = PageView(id=c.new_id(), user_id=profile.id, entered_at=now, exited_at=now, **data.model_dump())
            c.save("page_views", view.id, view)
            return view

    async def admin_aggregates(self, ctx: CallerContext, *, since: datetime | None = None) -> AdminAggregates:
        c = self._c
        with c.lock:
            c.require_admin(ctx)
            events: dict[str, int] = {}
            for event in c.scan(AnalyticsEvent, "analytics"):
                if since is None or event.created_at >= since:
                    events[event.event_name] = events.get(event.event_name, 0) + 1
            spend = sum(e.est_cost_usd for e in c.scan(UsageEvent, "usage") if since is None or e.created_at >= since)
            return AdminAggregates(users=len(c.backend.scan("profiles")), decks=len(c.backend.scan("decks")),
                                   exports=len(c.backend.scan("exports")), events_by_name=events,
                                   spend_usd=round(spend, 4))


class _Health:
    def __init__(self, core: _Core) -> None:
        self._c = core

    async def ping(self, ctx: CallerContext) -> HealthReport:
        with self._c.lock:
            return self._c.backend.ping()


class ReferenceStorage:
    """Every port over one `RecordBackend`. Satisfies `ports.Storage`."""

    def __init__(self, backend: RecordBackend, *, clock: Clock = utcnow) -> None:
        self._core = _Core(backend, clock)
        self.users = _Users(self._core)
        self.orgs = _Orgs(self._core)
        self.brands = _Brands(self._core)
        self.masters = _Masters(self._core)
        self.decks = _Decks(self._core)
        self.slides = _Slides(self._core)
        self.blobs = _Blobs(self._core)
        self.jobs = _Jobs(self._core)
        self.exports = _Exports(self._core)
        self.usage = _Usage(self._core)
        self.transcripts = _Transcripts(self._core)
        self.analytics = _Analytics(self._core)
        self.health = _Health(self._core)

    @property
    def backend(self) -> str:
        return self._core.backend.name
