"""
SlideForge service: core/storage/ports. The General service, as this service needs it.

PLACEHOLDER: confirm against General service repo. The General service (.NET) is
the only way to blob storage and SQL Server (repo standards, "No direct connection
to SQL Server or blob storage"). Its repo does not exist yet (decision D5), so
these protocols are the requirement we hand over (docs/general-service-requirements.md)
rather than a description of an API that exists. Every operation below is an
ASSUMED operation from the migration plan's §2.2 table.

Cross-cutting rules every adapter keeps (the contract suite in
tests/core/storage/contract/ holds them to it):

* Every operation takes a `CallerContext`: the end user's identity subject, for
  authorisation and audit, and the gateway's request id, which the adapter passes
  on so one request can be followed across both services.
* Ownership is enforced by the store, not only by the caller. Another user's
  object is `NotFound` (confirming that it exists would leak information), with
  two exceptions that the Darwin contract needs: a job record that exists but is
  someone else's is `Forbidden` (the status routes answer 403 "Not your job" but
  200 pending for an unknown id), and an object the caller may read but not
  change (an org brand, for a member) is `Forbidden` on write.
* Writes that create something accept an optional idempotency key. The same key
  with the same input returns the first result; the same key with different
  input is a `Conflict`. Keys are scoped to the caller and the operation.
* Slide versions are append-only. "Revert" is `set_current`, never a delete.

Errors belong to the port, not to HTTP: a route maps `NotFound` to its own 403 or
404 as the Darwin contract says (contract/INDEX.md).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Protocol, runtime_checkable

from app.core.storage.models import (
    AdminActivity,
    AdminAggregates,
    AdminOverview,
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
)

# --------------------------------------------------------------------------- errors


class StorageError(Exception):
    """Base of every error a port raises on purpose. Messages are safe to log and
    never carry another user's data; routes still do not echo them to callers."""


class NotFound(StorageError):
    """No such object, or one the caller may not know exists."""


class Forbidden(StorageError):
    """The caller may know the object exists but may not do this to it."""


class Conflict(StorageError):
    """The write conflicts with current state: a stale revision, an idempotency
    key reused with different input, a limit (20 personal brands), or a domain
    already mapped to another brand."""


class InvalidInput(StorageError, ValueError):
    """The input is outside what the store accepts (size, id shape, content type)."""


class Unavailable(StorageError):
    """The backend could not be reached or answered with a server error."""


DailyCounter = Literal["decks", "intake_turns", "brand_extracts"]
MasterFileKind = Literal["pptx", "layout"]

# --------------------------------------------------------------------------- ports


@runtime_checkable
class UserPort(Protocol):
    """Users and profiles. Replaces Supabase `profiles` and Supabase Auth ids.

    PLACEHOLDER: confirm against General service repo. Assumes the General service
    maps an identity subject to a stable user id (D29 re-keys today's Supabase users)
    and keeps the email-verified flag the org-domain rule needs."""

    async def get_or_create(self, ctx: CallerContext, claims: IdentityClaims) -> Profile:
        """The caller's profile, created on first sight. Refreshes email and
        email_verified from the claims every call (0011 reads auth.users, never a
        client-writable copy)."""
        ...

    async def get_me(self, ctx: CallerContext) -> Profile:
        """The caller's profile. NotFound if never provisioned."""
        ...

    async def update_me(self, ctx: CallerContext, patch: ProfilePatch) -> Profile:
        """Change the user-writable fields only (today: the legacy brand_kit)."""
        ...

    async def touch_last_seen(self, ctx: CallerContext) -> None: ...

    async def bump_counters(self, ctx: CallerContext, bump: CounterBump) -> Profile:
        """Atomic lifetime counter increments (0008's increment_* RPCs)."""
        ...

    async def reserve_daily(self, ctx: CallerContext, counter: DailyCounter, cap: int) -> bool:
        """Atomically take one unit of today's per-user cap (reserve_deck,
        reserve_intake_turn, reserve_brand_extract). False when the cap is reached.
        The day rolls over in UTC."""
        ...


@runtime_checkable
class OrgPort(Protocol):
    """Org brand domains (0011). PLACEHOLDER: confirm against General service repo."""

    async def my_org_brand_ids(self, ctx: CallerContext) -> list[str]:
        """Org brands the caller reaches through their VERIFIED email domain."""
        ...

    async def list_domains(self, ctx: CallerContext) -> list[OrgBrandDomain]:
        """Admin only (Forbidden otherwise)."""
        ...

    async def map_domain(self, ctx: CallerContext, domain: str, brand_id: str) -> OrgBrandDomain:
        """Admin only. The brand must be an org brand. Mapping a domain already on
        the same brand is a no-op; on another brand it is a Conflict."""
        ...

    async def unmap_domain(self, ctx: CallerContext, domain: str) -> None:
        """Admin only. Idempotent."""
        ...

    async def count_accounts(self, ctx: CallerContext, domain: str) -> int:
        """Admin only. How many profiles have an email address at `domain` (case-insensitive; Darwin's
        `profiles.email ILIKE '%@<domain>'`): an at-a-glance count for the admin page, not the
        membership rule (that one needs a verified email)."""
        ...


@runtime_checkable
class BrandPort(Protocol):
    """Brands, kits and brand assets (0005, 0011; Blobs `brand-assets`).

    PLACEHOLDER: confirm against General service repo. ACL, as Darwin's
    getBrandAccess: a personal brand is its owner's alone; an org brand is
    editable by any admin and readable (only) by members of a mapped domain.
    Assets live under the brand OWNER's namespace, never the caller's."""

    async def list_visible(self, ctx: CallerContext) -> list[BrandAccess]:
        """Org brands the caller can read first, then personal brands, newest first."""
        ...

    async def get(self, ctx: CallerContext, brand_id: str) -> BrandAccess: ...

    async def create(self, ctx: CallerContext, data: BrandCreate) -> Brand:
        """A personal brand. Conflict past 20 personal brands (0011 cap)."""
        ...

    async def create_org(self, ctx: CallerContext, data: BrandCreate) -> Brand:
        """An org brand, admin only. The creating admin's id namespaces its assets."""
        ...

    async def update(self, ctx: CallerContext, brand_id: str, patch: BrandPatch) -> Brand: ...

    async def delete(self, ctx: CallerContext, brand_id: str) -> None:
        """Idempotent for the owner (a missing or foreign id is a silent no-op, as
        deleteBrandRow). An org brand: admin only, Forbidden for members. Removes
        its domain mappings, masters and asset index."""
        ...

    async def put_asset(
        self, ctx: CallerContext, brand_id: str, name: str, data: bytes, content_type: str
    ) -> AssetInfo:
        """Write a named asset (logo, master, titleMaster, dividerMaster,
        guidelines, furniture, furniture-all, layout-preview-<n>). Needs edit."""
        ...

    async def get_asset(self, ctx: CallerContext, brand_id: str, name: str) -> StoredAsset: ...

    async def list_assets(self, ctx: CallerContext, brand_id: str) -> dict[str, AssetInfo]: ...

    async def delete_asset(self, ctx: CallerContext, brand_id: str, name: str) -> None:
        """Needs edit. Idempotent."""
        ...

    async def list_org(self, ctx: CallerContext) -> list[Brand]:
        """Admin only. Every org brand, by name (the admin org-brands page)."""
        ...


@runtime_checkable
class MasterPort(Protocol):
    """Imported masters: .pptx, manifest v2, layout PNGs. ACL follows the brand.
    PLACEHOLDER: confirm against General service repo."""

    async def create(
        self, ctx: CallerContext, brand_id: str, data: MasterCreate, *, idempotency_key: str | None = None
    ) -> Master: ...

    async def get(self, ctx: CallerContext, master_id: str) -> Master: ...

    async def list_for_brand(self, ctx: CallerContext, brand_id: str) -> list[Master]: ...

    async def update_manifest(self, ctx: CallerContext, master_id: str, manifest: dict[str, object]) -> Master: ...

    async def put_file(
        self,
        ctx: CallerContext,
        master_id: str,
        kind: MasterFileKind,
        data: bytes,
        content_type: str,
        *,
        layout_index: int | None = None,
    ) -> Master: ...

    async def get_file(
        self, ctx: CallerContext, master_id: str, kind: MasterFileKind, *, layout_index: int | None = None
    ) -> StoredAsset: ...

    async def delete(self, ctx: CallerContext, master_id: str) -> None: ...


@runtime_checkable
class DeckPort(Protocol):
    """Decks (0001, 0008). Owner only. PLACEHOLDER: confirm against General service repo."""

    async def create(
        self,
        ctx: CallerContext,
        data: DeckCreate,
        slides: list[SlideSpec],
        *,
        idempotency_key: str | None = None,
    ) -> Deck:
        """The deck and its slide rows in one write (persistDeck). Slide numbers
        must be unique."""
        ...

    async def get(self, ctx: CallerContext, deck_id: str) -> Deck: ...

    async def list_mine(
        self, ctx: CallerContext, *, limit: int = 50, cursor: str | None = None
    ) -> Page[DeckSummary]:
        """The caller's decks, newest first."""
        ...

    async def update(
        self, ctx: CallerContext, deck_id: str, patch: DeckPatch, *, expected_revision: int | None = None
    ) -> Deck:
        """Conflict when `expected_revision` is given and is not the current one."""
        ...

    async def delete(self, ctx: CallerContext, deck_id: str) -> None:
        """Idempotent; a foreign id is a silent no-op (deleteDeck is owner-scoped).
        Cascades to slides, exports and slide-edit transcripts."""
        ...


@runtime_checkable
class SlidePort(Protocol):
    """Slides and their append-only versions (slides table; Blobs deck-jobs JobState).
    Access is through the deck's owner. PLACEHOLDER: confirm against General service repo."""

    async def list_for_deck(self, ctx: CallerContext, deck_id: str) -> list[Slide]:
        """Ordered by number."""
        ...

    async def get(self, ctx: CallerContext, deck_id: str, number: int) -> Slide: ...

    async def update_status(self, ctx: CallerContext, deck_id: str, number: int, patch: SlideStatusPatch) -> Slide:
        ...

    async def append_version(
        self,
        ctx: CallerContext,
        deck_id: str,
        number: int,
        data: VersionCreate,
        *,
        idempotency_key: str | None = None,
    ) -> SlideVersion:
        """Append v = max(v)+1 (the first is v1). Sets it current unless
        data.make_current is False. Marks the slide done."""
        ...

    async def set_current(self, ctx: CallerContext, deck_id: str, number: int, v: int) -> Slide:
        """Revert/restore. NotFound if the slide has no version v."""
        ...

    async def record_refinement(self, ctx: CallerContext, deck_id: str, number: int) -> Slide:
        """increment_refinement_counts: slide, deck and profile counters together."""
        ...


@runtime_checkable
class BlobPort(Protocol):
    """Binary assets (Netlify Blobs, Azure Files, i2s-* containers). Owner only.
    PLACEHOLDER: confirm against General service repo."""

    async def put(self, ctx: CallerContext, data: bytes, content_type: str) -> AssetInfo: ...

    async def get(self, ctx: CallerContext, ref: str) -> StoredAsset: ...

    async def link(self, ctx: CallerContext, ref: str, *, ttl_seconds: int = 300) -> SignedLink:
        """A short-lived read link (1 s to 1 h), for a browser or PptxRender."""
        ...

    async def delete(self, ctx: CallerContext, ref: str) -> None:
        """Idempotent."""
        ...


@runtime_checkable
class JobPort(Protocol):
    """Durable job records (Blobs `*-jobs`, Slide Studio jobs). Redis holds the queue.
    PLACEHOLDER: confirm against General service repo."""

    async def create(self, ctx: CallerContext, data: JobCreate) -> tuple[JobRecord, bool]:
        """(record, created). With an idempotency key already used by this caller
        for this job type, the existing record and False."""
        ...

    async def get(self, ctx: CallerContext, job_id: str) -> JobRecord:
        """NotFound when missing or past its TTL; Forbidden when someone else's."""
        ...

    async def find_by_key(self, ctx: CallerContext, type: str, idempotency_key: str) -> JobRecord | None:
        """The caller's live job of this type created with this idempotency key, if
        any. Lets the queue answer a replay before taking an in-flight slot."""
        ...

    async def update(
        self, ctx: CallerContext, job_id: str, patch: JobPatch, *, expected_revision: int | None = None
    ) -> JobRecord:
        """The owner or a system caller. A terminal record (done, error, cancelled)
        cannot change status again: Conflict."""
        ...

    async def list_mine(
        self,
        ctx: CallerContext,
        *,
        type: str | None = None,
        status: JobStatus | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[JobRecord]: ...

    async def purge_expired(self, ctx: CallerContext) -> int:
        """System caller only. Removes records past their TTL; returns how many."""
        ...


@runtime_checkable
class ExportPort(Protocol):
    """Exports (Slide Studio exports[]; 0008 export counters).
    PLACEHOLDER: confirm against General service repo."""

    async def record(
        self, ctx: CallerContext, data: ExportCreate, *, idempotency_key: str | None = None
    ) -> ExportRecord:
        """Deck owner only. Also bumps deck.export_count, first_exported_at and
        the profile's total_exports (increment_export_counts)."""
        ...

    async def list_for_deck(self, ctx: CallerContext, deck_id: str) -> list[ExportRecord]: ...


@runtime_checkable
class UsagePort(Protocol):
    """The cost ledger (usage_events). The atomic per-request reserve is in Redis
    (D6); this is the durable record. PLACEHOLDER: confirm against General service repo."""

    async def append(
        self, ctx: CallerContext, data: UsageCreate, *, idempotency_key: str | None = None
    ) -> UsageEvent: ...

    async def spend_for_user(self, ctx: CallerContext, day: date) -> SpendSummary:
        """The caller's own spend on a UTC day."""
        ...

    async def global_spend(self, ctx: CallerContext, day: date) -> SpendSummary:
        """Everyone's spend on a UTC day. System caller or admin."""
        ...


@runtime_checkable
class TranscriptPort(Protocol):
    """Intake, slide-edit and design history (0008, 0009; Slide Studio history.json).
    PLACEHOLDER: confirm against General service repo."""

    async def upsert_intake(self, ctx: CallerContext, data: IntakeTranscriptUpsert) -> IntakeTranscript: ...

    async def get_intake(self, ctx: CallerContext, session_key: str) -> IntakeTranscript: ...

    async def link_intake_to_deck(self, ctx: CallerContext, session_key: str, deck_id: str) -> IntakeTranscript:
        """Sets deck_id and completed_at. The deck must be the caller's."""
        ...

    async def upsert_slide_edit(
        self, ctx: CallerContext, deck_id: str, slide_number: int, messages: list[object], refine_count: int
    ) -> SlideEditTranscript: ...

    async def get_slide_edit(self, ctx: CallerContext, deck_id: str, slide_number: int) -> SlideEditTranscript: ...

    async def put_design_history(self, ctx: CallerContext, deck_id: str, messages: list[object]) -> DesignHistory: ...

    async def get_design_history(self, ctx: CallerContext, deck_id: str) -> DesignHistory: ...


@runtime_checkable
class AnalyticsPort(Protocol):
    """Analytics events and page views (0008, 0010). D27 may move these to
    PostHog/App Insights; /api/analytics-event keeps its shape either way.
    PLACEHOLDER: confirm against General service repo."""

    async def record_event(self, ctx: CallerContext, data: AnalyticsEventCreate) -> AnalyticsEvent: ...

    async def record_page_view(self, ctx: CallerContext, data: PageViewCreate) -> PageView: ...

    async def admin_aggregates(self, ctx: CallerContext, *, since: datetime | None = None) -> AdminAggregates:
        """Admin only."""
        ...

    async def admin_overview(self, ctx: CallerContext, *, since: datetime, recent_decks: int = 20) -> AdminOverview:
        """Admin only. Every profile (newest first), the decks and ledger rows created at or after
        `since`, and the `recent_decks` newest decks overall (/api/admin-metrics)."""
        ...

    async def admin_activity(
        self,
        ctx: CallerContext,
        *,
        since: datetime,
        events: int = 2000,
        intake: int = 50,
        slide_edits: int = 100,
    ) -> AdminActivity:
        """Admin only. Analytics events at or after `since` (newest first, at most `events`), the
        `intake` newest intake transcripts and the `slide_edits` most recently updated slide-edit
        transcripts, regardless of `since` (/api/admin-metrics)."""
        ...


@runtime_checkable
class HealthPort(Protocol):
    """Readiness. PLACEHOLDER: confirm against General service repo."""

    async def ping(self, ctx: CallerContext) -> HealthReport:
        """Raise (any exception) when the backend is unreachable."""
        ...


@runtime_checkable
class Storage(Protocol):
    """Every port together: what `app.state.storage` holds and what the factory
    (core/storage/factory.py) builds from STORAGE_BACKEND."""

    @property
    def backend(self) -> str: ...

    @property
    def users(self) -> UserPort: ...

    @property
    def orgs(self) -> OrgPort: ...

    @property
    def brands(self) -> BrandPort: ...

    @property
    def masters(self) -> MasterPort: ...

    @property
    def decks(self) -> DeckPort: ...

    @property
    def slides(self) -> SlidePort: ...

    @property
    def blobs(self) -> BlobPort: ...

    @property
    def jobs(self) -> JobPort: ...

    @property
    def exports(self) -> ExportPort: ...

    @property
    def usage(self) -> UsagePort: ...

    @property
    def transcripts(self) -> TranscriptPort: ...

    @property
    def analytics(self) -> AnalyticsPort: ...

    @property
    def health(self) -> HealthPort: ...


# The port areas, in the order of the plan's §2.2 table. The requirements test
# walks these to check every operation is documented for the .NET team.
PORTS: tuple[tuple[str, type], ...] = (
    ("users", UserPort),
    ("orgs", OrgPort),
    ("brands", BrandPort),
    ("masters", MasterPort),
    ("decks", DeckPort),
    ("slides", SlidePort),
    ("blobs", BlobPort),
    ("jobs", JobPort),
    ("exports", ExportPort),
    ("usage", UsagePort),
    ("transcripts", TranscriptPort),
    ("analytics", AnalyticsPort),
    ("health", HealthPort),
)
