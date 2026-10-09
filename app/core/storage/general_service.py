"""
SlideForge service: core/storage/general_service. The HTTP adapter to the General service: STUB.

PLACEHOLDER: confirm against General service repo. The General service (.NET) repo
does not exist yet (decision D5), so this adapter is not written: constructing it
raises NotImplementedError, and STORAGE_BACKEND=general leaves /readyz at 503.

What is here is the REQUIREMENT, as data: `REQUIRED_ENDPOINTS` names one endpoint
per port operation (core/storage/ports.py), and `CALL_CONVENTIONS` lists what every
call carries. docs/general-service-requirements.md is the prose version for the
.NET team; tests/core/storage/test_general_service.py fails the build if a port
operation is missing from either. The paths are a proposal: the .NET team owns
them, and the adapter will follow whatever they choose. What must hold is the
operation, its inputs and outputs, and the semantics the contract suite tests.

When the repo arrives:
1. replace the stub with an httpx client implementing `ports.Storage`;
2. map 404 -> NotFound, 403 -> Forbidden, 409/412 -> Conflict, 400/422 ->
   InvalidInput, 5xx and timeouts -> Unavailable;
3. add `general` to the contract suite's adapter list and run it against staging.
"""

from __future__ import annotations

from dataclasses import dataclass

NOT_IMPLEMENTED_MESSAGE = (
    "The General service adapter is not written yet: the General service repo does not exist "
    "(decision D5). Use STORAGE_BACKEND=fake or local outside production. The expected endpoints "
    "are listed in app/core/storage/general_service.py (REQUIRED_ENDPOINTS) and "
    "docs/general-service-requirements.md."
)


@dataclass(frozen=True)
class Endpoint:
    operation: str  # "<port>.<method>", as in ports.PORTS
    method: str
    path: str
    notes: str = ""


# What every call carries. PLACEHOLDER: confirm against General service repo.
CALL_CONVENTIONS: tuple[str, ...] = (
    "Authorization: a service credential for SlideForge (managed identity / client credential, D18), never the "
    "end user's token alone.",
    "The end user's identity subject (CallerContext.subject), e.g. an X-On-Behalf-Of header or the forwarded JWT "
    "(D8), used by the General service for authorisation and its audit log. Absent for system calls.",
    "The gateway request id (CallerContext.request_id) in X-Request-ID, logged by the General service.",
    "Idempotency-Key on the create/append operations that accept one; same key + same body returns the first "
    "result, same key + different body is 409.",
    "If-Match with the record revision on deck and job updates; a stale revision is 409 (or 412).",
    "Errors: 404 not found or not yours, 403 visible but not allowed, 409 conflict, 400/422 invalid, 5xx "
    "unavailable. Bodies never echo another user's data.",
    "Timeouts: the adapter waits at most 5 s per call (30 s for binary uploads) and retries only idempotent "
    "calls.",
)

REQUIRED_ENDPOINTS: tuple[Endpoint, ...] = (
    # users / profiles
    Endpoint("users.get_or_create", "PUT", "/v1/users/me", "body: email, emailVerified, signupMethod -> Profile"),
    Endpoint("users.get_me", "GET", "/v1/users/me", "-> Profile; 404 if never provisioned"),
    Endpoint("users.update_me", "PATCH", "/v1/users/me", "body: brandKit only; counters and isAdmin are server-only"),
    Endpoint("users.touch_last_seen", "POST", "/v1/users/me/seen"),
    Endpoint("users.bump_counters", "POST", "/v1/users/me/counters",
             "body: decks, slidesGenerated, refinements, exports (atomic increments)"),
    Endpoint("users.reserve_daily", "POST", "/v1/users/me/daily-caps/{counter}:reserve",
             "counter: decks | intake_turns | brand_extracts; body: cap -> {granted}; atomic, UTC day rollover"),
    # orgs
    Endpoint("orgs.my_org_brand_ids", "GET", "/v1/users/me/org-brands", "verified email domain only"),
    Endpoint("orgs.list_domains", "GET", "/v1/org-brand-domains", "admin"),
    Endpoint("orgs.map_domain", "PUT", "/v1/org-brand-domains/{domain}", "admin; body: brandId; 409 if mapped elsewhere"),
    Endpoint("orgs.unmap_domain", "DELETE", "/v1/org-brand-domains/{domain}", "admin; idempotent"),
    # brands
    Endpoint("brands.list_visible", "GET", "/v1/brands", "org brands readable by the caller, then own, newest first"),
    Endpoint("brands.get", "GET", "/v1/brands/{brandId}", "-> {brand, canEdit}"),
    Endpoint("brands.create", "POST", "/v1/brands", "personal; 409 past 20"),
    Endpoint("brands.create_org", "POST", "/v1/org-brands", "admin"),
    Endpoint("brands.update", "PATCH", "/v1/brands/{brandId}", "403 for an org member"),
    Endpoint("brands.delete", "DELETE", "/v1/brands/{brandId}", "idempotent; cascades domains, masters, assets"),
    Endpoint("brands.put_asset", "PUT", "/v1/brands/{brandId}/assets/{name}", "raw bytes + Content-Type"),
    Endpoint("brands.get_asset", "GET", "/v1/brands/{brandId}/assets/{name}"),
    Endpoint("brands.list_assets", "GET", "/v1/brands/{brandId}/assets"),
    Endpoint("brands.delete_asset", "DELETE", "/v1/brands/{brandId}/assets/{name}", "idempotent"),
    # masters
    Endpoint("masters.create", "POST", "/v1/brands/{brandId}/masters", "Idempotency-Key"),
    Endpoint("masters.get", "GET", "/v1/masters/{masterId}"),
    Endpoint("masters.list_for_brand", "GET", "/v1/brands/{brandId}/masters"),
    Endpoint("masters.update_manifest", "PUT", "/v1/masters/{masterId}/manifest"),
    Endpoint("masters.put_file", "PUT", "/v1/masters/{masterId}/files/{kind}[/{layoutIndex}]", "kind: pptx | layout"),
    Endpoint("masters.get_file", "GET", "/v1/masters/{masterId}/files/{kind}[/{layoutIndex}]"),
    Endpoint("masters.delete", "DELETE", "/v1/masters/{masterId}", "idempotent"),
    # decks
    Endpoint("decks.create", "POST", "/v1/decks", "deck + slide rows in one transaction; Idempotency-Key"),
    Endpoint("decks.get", "GET", "/v1/decks/{deckId}"),
    Endpoint("decks.list_mine", "GET", "/v1/decks?limit=&cursor=", "newest first"),
    Endpoint("decks.update", "PATCH", "/v1/decks/{deckId}", "If-Match: revision"),
    Endpoint("decks.delete", "DELETE", "/v1/decks/{deckId}", "idempotent; cascades slides, exports, transcripts"),
    # slides + versions
    Endpoint("slides.list_for_deck", "GET", "/v1/decks/{deckId}/slides"),
    Endpoint("slides.get", "GET", "/v1/decks/{deckId}/slides/{number}"),
    Endpoint("slides.update_status", "PATCH", "/v1/decks/{deckId}/slides/{number}"),
    Endpoint("slides.append_version", "POST", "/v1/decks/{deckId}/slides/{number}/versions",
             "append-only; server assigns v; Idempotency-Key"),
    Endpoint("slides.set_current", "PUT", "/v1/decks/{deckId}/slides/{number}/current", "body: v"),
    Endpoint("slides.record_refinement", "POST", "/v1/decks/{deckId}/slides/{number}/refinements",
             "slide, deck and profile counters in one transaction"),
    # binary assets
    Endpoint("blobs.put", "POST", "/v1/blobs", "raw bytes + Content-Type -> {ref, size, sha256}"),
    Endpoint("blobs.get", "GET", "/v1/blobs/{ref}"),
    Endpoint("blobs.link", "POST", "/v1/blobs/{ref}/links", "body: ttlSeconds (1-3600) -> {url, expiresAt}"),
    Endpoint("blobs.delete", "DELETE", "/v1/blobs/{ref}", "idempotent"),
    # job records
    Endpoint("jobs.create", "POST", "/v1/jobs", "Idempotency-Key scoped to caller + type; -> {job, created}"),
    Endpoint("jobs.get", "GET", "/v1/jobs/{jobId}", "404 missing/expired, 403 someone else's"),
    Endpoint("jobs.find_by_key", "GET", "/v1/jobs?type=&idempotencyKey=", "the caller's live job for that key, or 404"),
    Endpoint("jobs.update", "PATCH", "/v1/jobs/{jobId}", "If-Match: revision; terminal status is final"),
    Endpoint("jobs.list_mine", "GET", "/v1/jobs?type=&status=&limit=&cursor="),
    Endpoint("jobs.purge_expired", "POST", "/v1/jobs:purge-expired", "system caller only"),
    # exports
    Endpoint("exports.record", "POST", "/v1/decks/{deckId}/exports", "bumps export counters; Idempotency-Key"),
    Endpoint("exports.list_for_deck", "GET", "/v1/decks/{deckId}/exports"),
    # usage / cost ledger
    Endpoint("usage.append", "POST", "/v1/usage-events", "append-only; Idempotency-Key"),
    Endpoint("usage.spend_for_user", "GET", "/v1/users/me/spend?day="),
    Endpoint("usage.global_spend", "GET", "/v1/spend?day=", "system caller or admin"),
    # transcripts
    Endpoint("transcripts.upsert_intake", "PUT", "/v1/intake-transcripts/{sessionKey}"),
    Endpoint("transcripts.get_intake", "GET", "/v1/intake-transcripts/{sessionKey}"),
    Endpoint("transcripts.link_intake_to_deck", "POST", "/v1/intake-transcripts/{sessionKey}/deck"),
    Endpoint("transcripts.upsert_slide_edit", "PUT", "/v1/decks/{deckId}/slides/{number}/edit-transcript"),
    Endpoint("transcripts.get_slide_edit", "GET", "/v1/decks/{deckId}/slides/{number}/edit-transcript"),
    Endpoint("transcripts.put_design_history", "PUT", "/v1/decks/{deckId}/design-history"),
    Endpoint("transcripts.get_design_history", "GET", "/v1/decks/{deckId}/design-history"),
    # analytics
    Endpoint("analytics.record_event", "POST", "/v1/analytics/events"),
    Endpoint("analytics.record_page_view", "POST", "/v1/analytics/page-views"),
    Endpoint("analytics.admin_aggregates", "GET", "/v1/analytics/aggregates?since=", "admin"),
    # health
    Endpoint("health.ping", "GET", "/v1/health", "service credential only; answers within 2 s"),
)


class GeneralServiceStorage:
    """The real adapter, once the General service exists. Not implemented."""

    def __init__(self, base_url: str) -> None:
        raise NotImplementedError(NOT_IMPLEMENTED_MESSAGE)
