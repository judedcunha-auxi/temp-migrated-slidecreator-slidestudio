# General service requirements (from SlideForge)

**PLACEHOLDER: confirm against General service repo.** The General service repo does not exist
yet (decision D5). This document is what the SlideForge service needs from it, written so the
.NET team can build it, or tell us where it differs. Nothing here describes an API that exists.

The source of truth is code, and tests keep this page in step with it:

- `app/core/storage/ports.py`: the operations, as Python protocols.
- `app/core/storage/models.py`: the entities and their fields.
- `app/core/storage/general_service.py`: `REQUIRED_ENDPOINTS`, one proposed endpoint per
  operation, and `CALL_CONVENTIONS`.
- `tests/core/storage/contract/`: the behaviour, as tests. Every adapter must pass them. Today
  they run against an in-memory fake and a local-files adapter. Once the General service has a
  test environment, they run against it too.

`tests/core/storage/test_fake_and_stub.py` fails the build if an operation is missing from this
page or from `REQUIRED_ENDPOINTS`.

The paths below are a **proposal**. The .NET team owns the URL design, and the adapter follows
whatever is chosen. What has to hold is the operation, its inputs and outputs, and the rules.

## 1. Calling conventions

Every call SlideForge makes carries the following:

| What | How (proposed) | Why |
|---|---|---|
| Service credential | `Authorization: Bearer <token>` for SlideForge's own identity (managed identity or client credential; D18) | Only trusted services reach durable data. |
| End user | `X-On-Behalf-Of: <identity subject>`, or the forwarded user JWT (D8) | The General service authorises **per object** and audits who did what. Absent on system calls. |
| Request id | `X-Request-ID: <gateway request id>` | One id follows a request through both services' logs (repo standards, "Operations"). |
| Idempotency | `Idempotency-Key: <key>` on the create and append operations marked below | Retries are safe. A replay with the same key and the same body returns the **first** result. The same key with a different body is **409**. Keys are scoped to the caller and the operation. |
| Optimistic concurrency | `If-Match: <revision>` on deck and job updates | A stale write is **409** (or 412), never a silent overwrite. Every write bumps `revision`. |
| Timeouts | SlideForge waits 5 s per call, or 30 s for binary uploads, and retries only idempotent calls | A slow General service must not stall a request. |

**Errors.** Each status means one thing:

- **404**: missing, or not visible to this caller. Use 404 rather than 403 wherever confirming
  that an object exists would leak information.
- **403**: visible but not allowed. Example: a member editing an org brand.
- **409**: conflict.
- **400/422**: invalid input.
- **5xx**: unavailable.

Bodies never echo another user's data. SlideForge maps these to its own errors
(`NotFound`, `Forbidden`, `Conflict`, `InvalidInput`, `Unavailable`) and then to its routes' own
contract.

**Pagination.** List operations take `limit` (1-200) and an opaque `cursor`, and return
`{items, nextCursor}`. `nextCursor` is null on the last page.

**System calls.** Some calls come from SlideForge itself with no end user: the readiness ping,
job record updates from workers, purging expired jobs and global spend. They carry the service
credential and no on-behalf-of. Only the operations that say "system" accept them.

## 2. Access rules

These are the rules the contract suite tests. They are today's Supabase RLS and Darwin
`getBrandAccess` behaviour, carried over.

- **Owner only:** decks, slides, versions, exports, transcripts, blobs and the caller's own
  profile and spend. Another user's object is **404**, as if it did not exist. A delete of
  another user's object is a silent no-op, because deletes are owner-scoped and idempotent.
- **Job records:** someone else's job is **403**, and a missing or expired job is **404**.
  Darwin's status routes need the difference: they answer 403 "Not your job" and 200
  `pending` respectively.
- **Personal brands:** owner only. At most 20 per user; the 21st create is **409**. Org brands
  do not count toward the 20.
- **Org brands:**
  - Any admin may read and edit them.
  - A user whose **verified** email domain is mapped to the brand may **read** it, its assets and
    its masters.
  - Every write by such a member is **403**.
  - For anyone else, the brand does not exist (**404**).
  - An unverified email never grants membership. This is 0011's `email_confirmed_at is not null`.
- **Brand assets and masters** live in the brand **owner's** namespace, never the caller's. A
  member reads an org brand's logo through the brand, not as their own blob.
- **Server-only facts:**
  - `isAdmin`, every counter, and a brand's owner and org flag cannot be changed through
    user-facing operations. This is 0006 `protect_cap_counters`, 0011 `protect_admin_flag` and
    `protect_brand_identity`.
  - Making someone an admin is an out-of-band operation and is not part of this API.
- **Slide versions are append-only.** "Revert" is `slides.set_current`. A version is never
  edited or removed. Version numbers are assigned by the server: `max + 1`, starting at 1.
- **Job records** are final once `done`, `error` or `cancelled`: a further status change is
  **409**. Each record has a TTL (default 7 days); after it, the record is gone (404).

## 3. Operations

Marked **idem** when the operation accepts an `Idempotency-Key`. "Me" is the caller named by
the on-behalf-of identity.

### Users and profiles

Replaces: Supabase `profiles` (0001, 0003, 0004, 0005, 0007, 0008) and Supabase Auth ids.

| Operation | Proposed endpoint | Input | Output | Rules |
|---|---|---|---|---|
| `users.get_or_create` | `PUT /v1/users/me` | email, emailVerified, signupMethod | Profile | Creates on first sight; refreshes email and emailVerified every call |
| `users.get_me` | `GET /v1/users/me` | | Profile | 404 if never provisioned |
| `users.update_me` | `PATCH /v1/users/me` | brandKit | Profile | Only user-writable fields |
| `users.touch_last_seen` | `POST /v1/users/me/seen` | | | |
| `users.bump_counters` | `POST /v1/users/me/counters` | decks, slidesGenerated, refinements, exports (≥ 0) | Profile | Atomic increments |
| `users.reserve_daily` | `POST /v1/users/me/daily-caps/{counter}:reserve` | counter (`decks`, `intake_turns`, `brand_extracts`), cap | granted: bool | Atomic check-and-increment; the day rolls over at 00:00 UTC (`reserve_*` RPCs) |

### Orgs and org brand domains

Replaces: `org_brand_domains` and `my_org_brand_ids` (0011).

| Operation | Proposed endpoint | Input | Output | Rules |
|---|---|---|---|---|
| `orgs.my_org_brand_ids` | `GET /v1/users/me/org-brands` | | brand ids | Verified email domain only |
| `orgs.list_domains` | `GET /v1/org-brand-domains` | | domains | Admin |
| `orgs.map_domain` | `PUT /v1/org-brand-domains/{domain}` | brandId | mapping | Admin; the brand must be an org brand; the same brand again is a no-op; another brand is 409; domain lower-cased, `^[a-z0-9-]+(\.[a-z0-9-]+)+$` |
| `orgs.unmap_domain` | `DELETE /v1/org-brand-domains/{domain}` | | | Admin; idempotent |

### Brands, kits and brand assets

Replaces: `brands` (0005, 0011) and the Netlify Blobs store `brand-assets`.

| Operation | Proposed endpoint | Input | Output | Rules |
|---|---|---|---|---|
| `brands.list_visible` | `GET /v1/brands` | | `[{brand, canEdit}]` | Readable org brands first, then the caller's own, newest first |
| `brands.get` | `GET /v1/brands/{brandId}` | | `{brand, canEdit}` | 404 when not visible |
| `brands.create` | `POST /v1/brands` | name, kit | Brand | Personal; 409 past 20 |
| `brands.create_org` | `POST /v1/org-brands` | name, kit | Brand | Admin; the creator's id namespaces its assets |
| `brands.update` | `PATCH /v1/brands/{brandId}` | name?, kit? | Brand | 403 for a member |
| `brands.delete` | `DELETE /v1/brands/{brandId}` | | | Idempotent; cascades domain mappings, masters and assets; 403 for a member |
| `brands.put_asset` | `PUT /v1/brands/{brandId}/assets/{name}` | bytes + Content-Type | AssetInfo | Needs edit. Names: `logo`, `master`, `titleMaster`, `dividerMaster`, `guidelines`, `furniture`, `furniture-all`, `layout-preview-<n>` (`^[A-Za-z][A-Za-z0-9_-]{0,63}$`) |
| `brands.get_asset` | `GET /v1/brands/{brandId}/assets/{name}` | | bytes + AssetInfo | Needs read |
| `brands.list_assets` | `GET /v1/brands/{brandId}/assets` | | name → AssetInfo | Needs read |
| `brands.delete_asset` | `DELETE /v1/brands/{brandId}/assets/{name}` | | | Needs edit; idempotent |

### Masters and manifests

Replaces: Slide Studio `projects/<id>/{uploads,layouts,manifest.json}` and Darwin
`furniture*.json`. The access rules follow the master's brand.

| Operation | Proposed endpoint | Input | Output | Rules |
|---|---|---|---|---|
| `masters.create` | `POST /v1/brands/{brandId}/masters` | name, manifest | Master | Needs brand edit; **idem** |
| `masters.get` | `GET /v1/masters/{masterId}` | | Master | Needs brand read |
| `masters.list_for_brand` | `GET /v1/brands/{brandId}/masters` | | Masters, oldest first | |
| `masters.update_manifest` | `PUT /v1/masters/{masterId}/manifest` | manifest (JSON ≤ 2 MB) | Master | Needs brand edit |
| `masters.put_file` | `PUT /v1/masters/{masterId}/files/{kind}[/{layoutIndex}]` | bytes + Content-Type | Master | `kind` is `pptx` or `layout`; layouts are indexed 0-999 |
| `masters.get_file` | `GET /v1/masters/{masterId}/files/{kind}[/{layoutIndex}]` | | bytes | |
| `masters.delete` | `DELETE /v1/masters/{masterId}` | | | Needs brand edit; idempotent |

### Decks

Replaces: `decks` (0001, 0008) and the per-deck flags in the Blobs `deck-jobs` JobState.

| Operation | Proposed endpoint | Input | Output | Rules |
|---|---|---|---|---|
| `decks.create` | `POST /v1/decks` | title, inputs (incl. `language`, `brandId`), storyline, status, creationMethod, **slides[]** | Deck | Deck and slide rows in **one transaction**; slide numbers unique; **idem** |
| `decks.get` | `GET /v1/decks/{deckId}` | | Deck | Owner |
| `decks.list_mine` | `GET /v1/decks?limit=&cursor=` | | `{items: [{id,title,status,createdAt}], nextCursor}` | Newest first |
| `decks.update` | `PATCH /v1/decks/{deckId}` | title, storyline, status, slideCount, generation times, masterUsed, masterSkipReason | Deck | `If-Match: revision` |
| `decks.delete` | `DELETE /v1/decks/{deckId}` | | | Idempotent; cascades slides, exports, slide-edit transcripts and design history; intake transcripts keep their row with `deckId` set to null |

### Slides and versions

Replaces: `slides` (0001, 0008) and the Blobs `deck-jobs` JobState `versions`/`current`.

A version is `{v, mode: html|image, htmlRef?, imageRef?, instruction, createdAt, cost}`.

| Operation | Proposed endpoint | Input | Output | Rules |
|---|---|---|---|---|
| `slides.list_for_deck` | `GET /v1/decks/{deckId}/slides` | | Slides by number | |
| `slides.get` | `GET /v1/decks/{deckId}/slides/{number}` | | Slide | |
| `slides.update_status` | `PATCH /v1/decks/{deckId}/slides/{number}` | status, error, prompt, masterKind, isTile, generationDurationMs | Slide | |
| `slides.append_version` | `POST /v1/decks/{deckId}/slides/{number}/versions` | mode, htmlRef or imageRef (as the mode requires), instruction, cost, makeCurrent | Version | Append-only; server assigns `v`; marks the slide done; **idem** |
| `slides.set_current` | `PUT /v1/decks/{deckId}/slides/{number}/current` | v | Slide | 404 for an unknown v |
| `slides.record_refinement` | `POST /v1/decks/{deckId}/slides/{number}/refinements` | | Slide | Slide, deck and profile counters in one transaction |

### Binary assets

Replaces: Netlify Blobs, Azure Files and the `i2s-*` containers.

| Operation | Proposed endpoint | Input | Output | Rules |
|---|---|---|---|---|
| `blobs.put` | `POST /v1/blobs` | bytes + Content-Type (≤ 100 MB) | `{ref, size, sha256, contentType}` | Owner is the caller |
| `blobs.get` | `GET /v1/blobs/{ref}` | | bytes | Owner only |
| `blobs.link` | `POST /v1/blobs/{ref}/links` | ttlSeconds (1-3600) | `{url, expiresAt}` | Short-lived read link, e.g. a SAS URL for PptxRender or a browser |
| `blobs.delete` | `DELETE /v1/blobs/{ref}` | | | Idempotent |

### Job records

Replaces: the Blobs `*-jobs` stores, Slide Studio SQLite and `server/jobs.py`. The queue itself
is in SlideForge's Redis; the General service only keeps the durable record.

| Operation | Proposed endpoint | Input | Output | Rules |
|---|---|---|---|---|
| `jobs.create` | `POST /v1/jobs` | type, inputs, idempotencyKey?, ttlSeconds | `{job, created}` | **idem**, scoped to caller + type; records the request id |
| `jobs.get` | `GET /v1/jobs/{jobId}` | | Job | 404 missing or expired; 403 someone else's; system allowed |
| `jobs.find_by_key` | `GET /v1/jobs?type=&idempotencyKey=` | | Job or 404 | The caller's live job for that key |
| `jobs.update` | `PATCH /v1/jobs/{jobId}` | status, progress, result, error, cost, attempts | Job | Owner or system; `If-Match`; a terminal status is final |
| `jobs.list_mine` | `GET /v1/jobs?type=&status=&limit=&cursor=` | | page of Jobs | Newest first |
| `jobs.purge_expired` | `POST /v1/jobs:purge-expired` | | count | System only. A server-side TTL sweep is just as good |

### Exports

Replaces: Slide Studio `exports[]` and the export counters (0008).

| Operation | Proposed endpoint | Input | Output | Rules |
|---|---|---|---|---|
| `exports.record` | `POST /v1/decks/{deckId}/exports` | kind (`pptx`/`pdf`), slideNumbers, fileRef, jobId, cost | Export | Bumps `deck.exportCount`, `firstExportedAt`, `profile.totalExports` in one transaction; **idem** |
| `exports.list_for_deck` | `GET /v1/decks/{deckId}/exports` | | Exports | |

### Usage and cost ledger

Replaces: `usage_events`, `global_counters` and the `reserve_image` RPC.

Atomic per-request reservation moves to Redis (D6). The ledger here is the durable record.

| Operation | Proposed endpoint | Input | Output | Rules |
|---|---|---|---|---|
| `usage.append` | `POST /v1/usage-events` | deckId?, slideNumber?, kind, model, estCostUsd | Event | Append-only; records the request id; **idem** |
| `usage.spend_for_user` | `GET /v1/users/me/spend?day=` | UTC day | `{day, events, costUsd}` | Caller's own |
| `usage.global_spend` | `GET /v1/spend?day=` | UTC day | `{day, events, costUsd}` | System or admin |

### Transcripts

Replaces: `intake_transcripts` (0008), `slide_edit_transcripts` (0009) and Slide Studio
`history.json`.

| Operation | Proposed endpoint | Input | Output | Rules |
|---|---|---|---|---|
| `transcripts.upsert_intake` | `PUT /v1/intake-transcripts/{sessionKey}` | messages, brief, turnCount, totalChars | Transcript | Unique per (user, sessionKey) |
| `transcripts.get_intake` | `GET /v1/intake-transcripts/{sessionKey}` | | Transcript | |
| `transcripts.link_intake_to_deck` | `POST /v1/intake-transcripts/{sessionKey}/deck` | deckId | Transcript | Sets `completedAt`; the deck must be the caller's |
| `transcripts.upsert_slide_edit` | `PUT /v1/decks/{deckId}/slides/{number}/edit-transcript` | messages, refineCount | Transcript | Unique per (deck, slide) |
| `transcripts.get_slide_edit` | `GET /v1/decks/{deckId}/slides/{number}/edit-transcript` | | Transcript | |
| `transcripts.put_design_history` | `PUT /v1/decks/{deckId}/design-history` | messages | History | The design chat's model history |
| `transcripts.get_design_history` | `GET /v1/decks/{deckId}/design-history` | | History | |

### Analytics

Replaces: `analytics_events` and `page_view_sessions` (0008, 0010).

D27 may send these to PostHog or App Insights instead. If so, these three operations go away.

| Operation | Proposed endpoint | Input | Output | Rules |
|---|---|---|---|---|
| `analytics.record_event` | `POST /v1/analytics/events` | sessionId, eventName, properties, pagePath | Event | |
| `analytics.record_page_view` | `POST /v1/analytics/page-views` | sessionId, path, durationSeconds, scrollDepthPct, referrerPath | PageView | |
| `analytics.admin_aggregates` | `GET /v1/analytics/aggregates?since=` | | users, decks, exports, eventsByName, spendUsd | Admin |

### Health

| Operation | Proposed endpoint | Input | Output | Rules |
|---|---|---|---|---|
| `health.ping` | `GET /v1/health` | | ok | Service credential only; answers within 2 s. SlideForge's `/readyz` depends on it |

## 4. Data to migrate (D29: migrate everything)

| Today | Becomes |
|---|---|
| Supabase Auth users | Users, re-keyed to the new identity subject; the old id is kept for the mapping |
| `profiles` | Profile, including every counter and `brand_kit` |
| `brands`, `org_brand_domains` | Brand, OrgBrandDomain |
| Blobs `brand-assets` (`brand/<owner>/<brand>/<kind>.png`, `guidelines.pdf`, `layout-preview-<n>.png`, `furniture*.json`, legacy `brand/<owner>/<kind>.png`) | Brand assets under the same owner |
| `decks`, `slides` | Deck, Slide (`variant_a_*` columns become status/error) |
| Blobs `deck-jobs` `job/<deck>.json` and `deck-images` `deck/<deck>/slide_<n>[_v<v>].png` (plus legacy `_variant_A`) | Slide versions with `mode: image` and `imageRef`; `current`, `masterKind`, `isTile` and the deck's `masterUsed` / `masterSkipReason` / `brandId` |
| `usage_events`, `global_counters` | Usage events (the counters can be recomputed) |
| `intake_transcripts`, `slide_edit_transcripts` | Transcripts |
| `analytics_events`, `page_view_sessions` | Analytics (unless D27 moves them) |
| Blobs `*-jobs` stores (storyline, brand-extract, quick, brand-pptx, brand-preview) | **Dropped**: transient job records |

## 5. Assumptions to confirm

Each line is a PLACEHOLDER the General service may answer differently:

1. One service credential for SlideForge, plus the end user's subject on each call, rather than
   a token exchange per user.
2. The General service maps the identity subject to a stable user id and stores `emailVerified`.
3. Per-object authorisation happens **in** the General service, not only in SlideForge.
4. The idempotency key is honoured server-side, scoped to the caller and the operation, and kept
   at least as long as the record it created.
5. The 404-vs-403 split above, including 403 for someone else's job.
6. Optimistic concurrency by an integer `revision` with `If-Match`.
7. Atomic operations: `reserve_daily`, `bump_counters`, `record_refinement`, `exports.record`,
   and `decks.create` with its slides.
8. Server-assigned slide version numbers, append-only.
9. Short-lived signed links for blobs, at most 1 h.
10. Job record TTL enforced by the General service.
11. A blob size limit of at least 100 MB, and the content types SlideForge sends: PNG, PDF,
    PPTX, JSON and HTML.
12. Day boundaries in UTC for caps and spend.
13. Cursor pagination with `limit` ≤ 200.
14. `GET /v1/health` reachable with the service credential and answering within 2 s.
