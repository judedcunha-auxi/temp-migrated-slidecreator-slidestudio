"""
SlideForge service: core/storage/models. The entities the General service stores.

PLACEHOLDER: confirm against General service repo. Every model here is derived from
data that exists today, so the migration (D29: migrate everything) has somewhere to
put each column:

* the Supabase schema, `supabase/migrations/0001`-`0011` in Slide-Creator
  (profiles, decks, slides, usage_events, global_counters, brands,
  org_brand_domains, intake_transcripts, slide_edit_transcripts,
  analytics_events, page_view_sessions);
* the Netlify Blob shapes in `netlify/functions/_shared/blobs.ts` and
  `jobState.ts` (deck JobState with append-only render versions, the five
  `*-jobs` stores, the `brand-assets` keys);
* Slide Studio's `server/store.py` (`project.json` slide versions, exports,
  `history.json`) and `server/jobs.py` (job kind/status/events).

Field names are snake_case here; the HTTP adapter maps them to whatever the
General service's wire format turns out to be. Times are timezone-aware UTC.
Money is USD as `float` (Supabase used numeric(10,4); four decimals are kept by
rounding at the ledger, not by the type).

These are the port's types, not an HTTP contract: the Darwin `/api/*` response
shapes (contract/*.json) are built from them by the route layer.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

T = TypeVar("T")

# An id the port hands out or accepts: uuid-ish, safe as a path segment and a URL
# segment. The General service will probably use GUIDs; this is wide enough for both.
ID_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"
_ID = re.compile(ID_PATTERN)
# 0011_org_domain_brands.sql's CHECK, verbatim.
DOMAIN_PATTERN = r"^[a-z0-9-]+(\.[a-z0-9-]+)+$"
# Brand asset names: today's blob basenames (logo, master, titleMaster,
# dividerMaster, guidelines, furniture, furniture-all, layout-preview-<n>).
ASSET_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"
# Content types the port stores. Anything else is refused at the boundary.
CONTENT_TYPE_PATTERN = r"^[a-z]+/[a-z0-9.+-]+$"

MAX_JSON_BYTES = 2_000_000  # a kit / storyline / manifest larger than this is a bug


def is_valid_id(value: str) -> bool:
    return bool(_ID.match(value))


class _Model(BaseModel):
    # Unknown fields are an error: a typo in a patch must not be silently dropped.
    model_config = ConfigDict(extra="forbid", frozen=False)


class Page(BaseModel, Generic[T]):
    """One page of a list. `next_cursor` is opaque; None means the last page."""

    items: list[T]
    next_cursor: str | None = None


# ----------------------------------------------------------------------- caller
class CallerContext(_Model):
    """Who is asking, for authorisation, audit and log correlation.

    `subject` is the identity provider's subject for the end user (D8: the
    forwarded end-user JWT). `request_id` is the gateway's request id, passed on to
    the General service on every call. A `system` caller is the service itself
    acting with its own credential and no end user (a reaper expiring leases);
    the ports allow it only where a docstring says so.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str = Field(min_length=1, max_length=256)
    request_id: str = Field(min_length=1, max_length=128)
    system: bool = False

    @classmethod
    def service(cls, request_id: str) -> CallerContext:
        return cls(subject="service:slideforge", request_id=request_id, system=True)


# ----------------------------------------------------------------------- users
class IdentityClaims(_Model):
    """What the verified token says about the caller, passed to get-or-create.

    `email_verified` matters: org brand membership is by verified email domain only
    (0011: `email_confirmed_at is not null`)."""

    email: str | None = Field(default=None, max_length=320)
    email_verified: bool = False
    signup_method: str | None = Field(default=None, max_length=64)


class Profile(_Model):
    """profiles (0001, 0003, 0004, 0005, 0007, 0008) plus the identity subject."""

    id: str
    subject: str
    email: str | None = None
    email_verified: bool = False
    is_admin: bool = False
    # Legacy single brand kit (0003), read when a deck names no brand.
    brand_kit: dict[str, Any] | None = None
    created_at: datetime
    last_seen_at: datetime | None = None
    signup_method: str | None = None
    # Daily caps (0001/0004/0005). Server-only: never writable through update_me.
    decks_today: int = 0
    decks_today_date: date | None = None
    intake_turns_today: int = 0
    intake_turns_today_date: date | None = None
    brand_extracts_today: int = 0
    brand_extracts_today_date: date | None = None
    # Lifetime counters (0001/0008).
    lifetime_decks: int = 0
    total_slides_generated: int = 0
    total_refinements: int = 0
    total_exports: int = 0


class ProfilePatch(_Model):
    """The only profile fields a user may change. is_admin and every counter are
    server-only facts (0006 protect_cap_counters, 0011 protect_admin_flag)."""

    brand_kit: dict[str, Any] | None = None


class CounterBump(_Model):
    """Server-side counter increments (the increment_* RPCs of 0008)."""

    decks: int = Field(default=0, ge=0, le=1000)
    slides_generated: int = Field(default=0, ge=0, le=1000)
    refinements: int = Field(default=0, ge=0, le=1000)
    exports: int = Field(default=0, ge=0, le=1000)


# ----------------------------------------------------------------------- orgs
class OrgBrandDomain(_Model):
    """org_brand_domains (0011): one brand per domain, a brand may cover several."""

    domain: str = Field(pattern=DOMAIN_PATTERN, max_length=253)
    brand_id: str
    created_by: str | None = None
    created_at: datetime


# ----------------------------------------------------------------------- brands
class Brand(_Model):
    """brands (0005, 0011). `owner_id` namespaces the brand's assets (not the caller)."""

    id: str
    owner_id: str
    name: str = Field(min_length=1, max_length=200)
    kit: dict[str, Any] = Field(default_factory=dict)
    is_org: bool = False
    created_at: datetime
    updated_at: datetime


class BrandAccess(_Model):
    """A brand as one caller sees it: Darwin's getBrandAccess (db.ts)."""

    brand: Brand
    can_edit: bool


class BrandCreate(_Model):
    name: str = Field(min_length=1, max_length=200)
    kit: dict[str, Any] = Field(default_factory=dict)


class BrandPatch(_Model):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    kit: dict[str, Any] | None = None


class AssetInfo(_Model):
    """Metadata of a stored binary. `ref` is what other records point at."""

    ref: str
    owner_id: str
    content_type: str
    size: int
    sha256: str
    created_at: datetime


class StoredAsset(_Model):
    info: AssetInfo
    data: bytes


class SignedLink(_Model):
    """A short-lived read link to a binary, for handing to a browser or PptxRender."""

    url: str
    expires_at: datetime


# ----------------------------------------------------------------------- masters
class Master(_Model):
    """An imported slide master: the `.pptx`, its manifest v2 and layout PNGs.

    Replaces Slide Studio's projects/<id>/{uploads,layouts,manifest.json} and
    Darwin's furniture*.json. Readable by whoever can read the brand; written by
    whoever can edit it."""

    id: str
    brand_id: str
    owner_id: str
    name: str = Field(min_length=1, max_length=200)
    manifest: dict[str, Any] = Field(default_factory=dict)
    pptx_ref: str | None = None
    layout_refs: dict[str, str] = Field(default_factory=dict)  # layout index -> asset ref
    created_at: datetime
    updated_at: datetime


class MasterCreate(_Model):
    name: str = Field(min_length=1, max_length=200)
    manifest: dict[str, Any] = Field(default_factory=dict)


# ----------------------------------------------------------------------- decks
DeckStatus = Literal["draft", "generating", "done", "failed"]
MasterSkipReason = Literal["no-brand", "no-master-uploaded", "blob-unavailable", "load-error"]


class Deck(_Model):
    """decks (0001, 0008) plus what jobState.ts kept per deck (masterUsed, …)."""

    id: str
    owner_id: str
    title: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)  # incl. brandId, language
    storyline: dict[str, Any] | list[Any] = Field(default_factory=list)
    status: DeckStatus = "draft"
    brand_id: str | None = None
    language: str | None = None
    creation_method: str | None = None
    slide_count: int | None = None
    refine_count: int = 0
    export_count: int = 0
    first_exported_at: datetime | None = None
    generation_started_at: datetime | None = None
    generation_completed_at: datetime | None = None
    master_used: bool | None = None
    master_skip_reason: MasterSkipReason | None = None
    created_at: datetime
    updated_at: datetime
    # Optimistic concurrency: bumps on every write. A patch that names an older
    # revision is a Conflict.
    revision: int = 1


class DeckCreate(_Model):
    title: str = Field(default="", max_length=500)
    inputs: dict[str, Any] = Field(default_factory=dict)
    storyline: dict[str, Any] | list[Any] = Field(default_factory=list)
    status: DeckStatus = "draft"
    brand_id: str | None = None
    language: str | None = Field(default=None, max_length=16)
    creation_method: str | None = Field(default=None, max_length=32)


class DeckPatch(_Model):
    title: str | None = Field(default=None, max_length=500)
    storyline: dict[str, Any] | list[Any] | None = None
    status: DeckStatus | None = None
    slide_count: int | None = Field(default=None, ge=0, le=1000)
    generation_started_at: datetime | None = None
    generation_completed_at: datetime | None = None
    master_used: bool | None = None
    master_skip_reason: MasterSkipReason | None = None


class DeckSummary(_Model):
    """What /api/decks lists: id, title, status, created_at (db.ts listDecks)."""

    id: str
    title: str
    status: DeckStatus
    created_at: datetime


# ----------------------------------------------------------------------- slides
SlideStatus = Literal["idle", "generating", "done", "error"]
VersionMode = Literal["html", "image"]
MasterKind = Literal["title", "divider", "layout"]


class SlideVersion(_Model):
    """One rendered version of a slide. Append-only: never edited, never removed.

    `{v, mode, htmlRef?, imageRef?, instruction, createdAt, cost}` as the plan's
    §2.2 says. Darwin's RenderVersion was {v, key, instruction}: `key` becomes
    `image_ref` with mode "image"."""

    v: int = Field(ge=1)
    mode: VersionMode
    html_ref: str | None = None
    image_ref: str | None = None
    instruction: str | None = None
    created_at: datetime
    cost_usd: float = Field(default=0.0, ge=0)


class VersionCreate(_Model):
    mode: VersionMode
    html_ref: str | None = None
    image_ref: str | None = None
    instruction: str | None = Field(default=None, max_length=2000)
    cost_usd: float = Field(default=0.0, ge=0, le=1000)
    make_current: bool = True

    @model_validator(mode="after")
    def _ref_matches_mode(self) -> VersionCreate:
        if self.mode == "html" and not self.html_ref:
            raise ValueError("an html version needs html_ref")
        if self.mode == "image" and not self.image_ref:
            raise ValueError("an image version needs image_ref")
        return self


class SlideSpec(_Model):
    """A storyline slide as persisted at deck creation (slides table, 0001/0008)."""

    number: int = Field(ge=1, le=1000)
    title: str = ""
    type: str | None = None
    section: str | None = None
    framework: str = ""
    description: str = ""
    bullets: list[Any] = Field(default_factory=list)
    prompt: str = ""
    chart_data: dict[str, Any] | None = None
    archetype_id: str | None = None


class Slide(_Model):
    deck_id: str
    number: int
    spec: SlideSpec
    status: SlideStatus = "idle"
    error: str | None = None
    master_kind: MasterKind | None = None
    is_tile: bool = False
    current: int | None = None
    versions: list[SlideVersion] = Field(default_factory=list)
    refine_count: int = 0
    last_refined_at: datetime | None = None
    generation_duration_ms: int | None = None


class SlideStatusPatch(_Model):
    status: SlideStatus | None = None
    error: str | None = Field(default=None, max_length=2000)
    prompt: str | None = None
    master_kind: MasterKind | None = None
    is_tile: bool | None = None
    generation_duration_ms: int | None = Field(default=None, ge=0)


# ----------------------------------------------------------------------- jobs
JobStatus = Literal["queued", "running", "done", "error", "cancelled"]
TERMINAL_JOB_STATUSES: frozenset[str] = frozenset({"done", "error", "cancelled"})


class JobRecord(_Model):
    """The durable job record. Replaces the `*-jobs` Blob stores and Slide Studio's
    in-memory jobs. Redis holds the queue; this holds what a status route reads."""

    id: str
    type: str = Field(min_length=1, max_length=64)
    owner_id: str
    owner_subject: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    status: JobStatus = "queued"
    progress: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    cost_usd: float = 0.0
    attempts: int = 0
    request_id: str | None = None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    revision: int = 1


class JobCreate(_Model):
    type: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    inputs: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    ttl_seconds: int = Field(default=7 * 24 * 3600, ge=60, le=90 * 24 * 3600)


class JobPatch(_Model):
    status: JobStatus | None = None
    progress: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = Field(default=None, max_length=2000)
    cost_usd: float | None = Field(default=None, ge=0)
    attempts: int | None = Field(default=None, ge=0)


# ----------------------------------------------------------------------- exports
ExportKind = Literal["pptx", "pdf"]


class ExportRecord(_Model):
    """One export: Slide Studio's exports[] plus Darwin's export counters (0008)."""

    id: str
    deck_id: str
    owner_id: str
    kind: ExportKind
    slide_numbers: list[int]
    file_ref: str | None = None
    job_id: str | None = None
    cost_usd: float = 0.0
    created_at: datetime


class ExportCreate(_Model):
    deck_id: str
    kind: ExportKind
    slide_numbers: list[int] = Field(max_length=1000)
    file_ref: str | None = None
    job_id: str | None = None
    cost_usd: float = Field(default=0.0, ge=0, le=1000)


# ----------------------------------------------------------------------- usage
class UsageEvent(_Model):
    """usage_events (0001): the append-only cost ledger."""

    id: str
    user_id: str
    deck_id: str | None = None
    slide_number: int | None = None
    kind: str = "image"
    model: str | None = None
    est_cost_usd: float = 0.0
    request_id: str | None = None
    created_at: datetime


class UsageCreate(_Model):
    deck_id: str | None = None
    slide_number: int | None = Field(default=None, ge=1)
    kind: str = Field(default="image", max_length=32)
    model: str | None = Field(default=None, max_length=100)
    est_cost_usd: float = Field(default=0.0, ge=0, le=1000)


class SpendSummary(_Model):
    day: date
    events: int
    cost_usd: float


# ----------------------------------------------------------------------- transcripts
class IntakeTranscript(_Model):
    """intake_transcripts (0008), unique per (user, session_key)."""

    user_id: str
    session_key: str = Field(min_length=1, max_length=200)
    deck_id: str | None = None
    messages: list[Any] = Field(default_factory=list)
    brief: dict[str, Any] = Field(default_factory=dict)
    turn_count: int = 0
    total_chars: int = 0
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class IntakeTranscriptUpsert(_Model):
    session_key: str = Field(min_length=1, max_length=200)
    messages: list[Any] = Field(default_factory=list, max_length=1000)
    brief: dict[str, Any] = Field(default_factory=dict)
    turn_count: int = Field(default=0, ge=0)
    total_chars: int = Field(default=0, ge=0)


class SlideEditTranscript(_Model):
    """slide_edit_transcripts (0009), unique per (deck, slide_number)."""

    user_id: str
    deck_id: str
    slide_number: int
    messages: list[Any] = Field(default_factory=list)
    refine_count: int = 0
    created_at: datetime
    updated_at: datetime


class DesignHistory(_Model):
    """Slide Studio's history.json: the design chat's model message history per deck."""

    deck_id: str
    owner_id: str
    messages: list[Any] = Field(default_factory=list)
    updated_at: datetime


# ----------------------------------------------------------------------- analytics
class AnalyticsEventCreate(_Model):
    session_id: str | None = Field(default=None, max_length=200)
    event_name: str = Field(min_length=1, max_length=200)
    properties: dict[str, Any] = Field(default_factory=dict)
    page_path: str | None = Field(default=None, max_length=2000)


class AnalyticsEvent(AnalyticsEventCreate):
    """analytics_events (0008)."""

    id: str
    user_id: str
    created_at: datetime


class PageViewCreate(_Model):
    session_id: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=2000)
    duration_seconds: int | None = Field(default=None, ge=0)
    scroll_depth_pct: int | None = Field(default=None, ge=0, le=100)
    referrer_path: str | None = Field(default=None, max_length=2000)


class PageView(PageViewCreate):
    """page_view_sessions (0008)."""

    id: str
    user_id: str
    entered_at: datetime
    exited_at: datetime | None = None


class AdminAggregates(_Model):
    """The raw counts /api/admin-metrics is built from. The route keeps its shape."""

    users: int
    decks: int
    exports: int
    events_by_name: dict[str, int]
    spend_usd: float


# ----------------------------------------------------------------------- health
class HealthReport(_Model):
    """`ok`: the backend answered. `skipped`: there is nothing to reach (the fake);
    /readyz treats that as neutral."""

    status: Literal["ok", "skipped"]
    backend: str


def check_id(value: str, what: str = "id") -> str:
    """Raise ValueError unless `value` is a well-formed id."""
    if not is_valid_id(value):
        raise ValueError(f"{what} is not a valid id")
    return value
