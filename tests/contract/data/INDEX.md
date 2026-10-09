# Darwin `/api/*` contract reference (Phase 7a oracle)

This is the contract for every Darwin `/api/*` endpoint, recorded on 2026-10-08 from the TypeScript handlers in `netlify/functions/*.ts`. It exists to back the contract snapshot tests required by DETAILED-PLAN.md §2.1 and risk R12.

**Where to look:**
- One JSON file per route, `api-<name>.json`, laid out as in `_TEMPLATE.json`. Every fact cites `file:line`.
- `_common.json` covers what all routes share: routing, the `json()` helper, `errorResponse`, the 401/403/500 bodies from `requireUser` and `requireAdmin`, caps, and the test baseline.
- `api-background-functions.json` covers the 7 `*-background` functions. These are not part of the contract to reimplement (D31), but they are reachable today (C12).

**The handler code is the oracle where the sources disagree.** `public/openapi.yaml`, `docs/postman_collection.json`, `docs/api.md` and the unit tests are secondary.

## Counts

- **37** foreground routes, each with its own file. Every one is in §6.5.
- **7** background functions, all in one file.
- R12's "about 44 `/api` routes" equals 37 + 7. The new service must serve **37**.

## Route table

**Auth values:**
- **U** = `requireUser`.
- **A** = `requireAdmin`.
- **custom** = userinfo's own scheme: it decodes the JWT and does not verify it.

**Callers:**
- **FE** = Slide-Creator frontend.
- **AC** = `C:\Github\Auxi_Connector`.

| Route | Methods (as handled) | Auth | Success | Callers |
|---|---|---|---|---|
| [`/api/userinfo`](api-userinfo.json) | any | custom | 200 JSON `{sub,email,name,given_name,family_name}` | Supabase Auth custom OIDC provider `custom:auxi` (server-side); `public/api-portal.html:415` link |
| [`/api/decks`](api-decks.json) | DELETE; **every other method = GET** | U | 200 `{decks}` / `{deck,slides}` / `{ok:true}` | FE `src/lib/api.ts:211,215,219` |
| [`/api/intake`](api-intake.json) | POST (not checked) | U | 200 JSON `{message,brief}` (**not SSE**) | FE `src/lib/api.ts:68` (postIntake), `:91` (streamIntake) via `src/pages/IntakeChat.tsx:77,101` |
| [`/api/storyline`](api-storyline.json) | POST (not checked) | U | 202 `{jobId}` | FE `src/lib/api.ts:24` |
| [`/api/storyline-status`](api-storyline-status.json) | GET (not checked) | U | 200 `{status,result?,error?}` | FE `src/lib/api.ts:42` |
| [`/api/generate`](api-generate.json) | POST (not checked) | U | 202 `{deckId,warnings}` | FE `src/lib/api.ts:145`; AC `app/api/routes/forge.py:424` |
| [`/api/retry`](api-retry.json) | POST (not checked) | U | 202 `{deckId}` | FE `src/lib/api.ts:156` |
| [`/api/status`](api-status.json) | GET (not checked) | U | 200 JSON | FE `src/lib/api.ts:168`, `src/lib/useDeckStatus.ts:5`; AC `forge.py:626` |
| [`/api/refine`](api-refine.json) | POST (not checked) | U | 202 `{deckId}` | FE `src/lib/api.ts:160`; AC `forge.py:725` |
| [`/api/revert`](api-revert.json) | POST (not checked) | U | 200 `{ok:true}` | FE `src/lib/api.ts:164`; AC `forge.py:758` |
| [`/api/slide-transcript`](api-slide-transcript.json) | POST; others get 405 **before auth** | U | 200 `{ok:true}` | FE `src/lib/api.ts:33` |
| [`/api/image`](api-image.json) | GET (not checked) | U | 200 `image/png`, `private, max-age=3600` | FE `src/components/SlideImage.tsx:10,68`, `VariantCell.tsx:14`, `src/lib/pdfDownload.ts:21,176,220`; AC `forge.py:664` |
| [`/api/pdf`](api-pdf.json) | GET (not checked) | U | 200 `application/pdf` (no cache-control) | FE `src/lib/api.ts:291-294`, `src/lib/pdfDownload.ts:170` |
| [`/api/pdf-deck`](api-pdf-deck.json) | GET (not checked) | U | 200 `application/pdf` | FE `src/lib/api.ts:299-302`; AC `app/api/routes/forge_pdf.py:94,148` |
| [`/api/pptx-submit`](api-pptx-submit.json) | POST; others get 405 **before auth** | U | **200** `{jobId}` | FE `src/lib/api.ts:233`; AC `forge.py:810` |
| [`/api/pptx-status`](api-pptx-status.json) | GET (not checked) | U | 200 `{status,done,failed}` | FE `src/lib/api.ts:237`; AC `forge.py:829` (also used by `image_slide.py` to poll image-to-slide jobs) |
| [`/api/pptx-result`](api-pptx-result.json) | GET (not checked) | U | 200 pptx bytes | FE `src/lib/api.ts:250`; AC `forge.py:972,1011` (also `image_slide.py`) |
| [`/api/pptx-deck-submit`](api-pptx-deck-submit.json) | POST; others get 405 **before auth** | U | **200** `{deckJobId}` | FE `src/lib/api.ts:256`; AC `app/api/routes/forge_deck.py:378` |
| [`/api/pptx-deck-status`](api-pptx-deck-status.json) | GET (not checked) | U | 200 `{status,done,failed}` | FE `src/lib/api.ts:260`; AC `forge_deck.py:260` |
| [`/api/pptx-deck-result`](api-pptx-deck-result.json) | GET (not checked) | U | 200 pptx bytes | FE `src/lib/api.ts:273`; AC `forge_deck.py:448,562` |
| [`/api/image-to-slide`](api-image-to-slide.json) | POST; others get 405 after auth | U | 202 `{jobId}` | AC `app/api/routes/image_slide.py:282` only |
| [`/api/quick-generate`](api-quick-generate.json) | POST (not checked) | U | 202 `{jobId}` | none in repos (external API users; openapi/postman quick-start) |
| [`/api/quick-status`](api-quick-status.json) | GET (not checked) | U | 200 `{status,slides?,warnings?,error?}` | none |
| [`/api/quick-image`](api-quick-image.json) | GET (not checked) | U | 200 `image/png`, `private, max-age=3600` | none |
| [`/api/brands`](api-brands.json) | GET, POST, PATCH, DELETE | U | 200/201 JSON | AC `app/core/forge_brands.py:208,233,253,275` only. The FE uses Supabase directly (`src/lib/brands.ts`) |
| [`/api/brand-asset`](api-brand-asset.json) | POST; **every other method = GET** | U | POST 200 `{key}`; GET `image/png` or JSON `{template}` | FE `src/lib/api.ts:173,183,207`, `src/components/ArchetypeLayoutPicker.tsx:28`, `TitlePlaceholderPicker.tsx:50`, `WorkzoneEditor.tsx:47`; AC `forge_brands.py:298` |
| [`/api/brand-pptx`](api-brand-pptx.json) | POST; others get 405 after auth | U | 202 `{jobId}` | FE `src/lib/api.ts:335`; AC `forge_brands.py:316` |
| [`/api/brand-pptx-status`](api-brand-pptx-status.json) | GET (not checked) | U | 200 `{status,fields?,error?}` | FE `src/lib/api.ts:346`; AC `forge_brands.py:358` |
| [`/api/brand-archetypes`](api-brand-archetypes.json) | POST; others get 405 **before auth** | U | 200 `{kit,furnitureRebuilt}` | FE `src/lib/api.ts:193` |
| [`/api/brand-heading`](api-brand-heading.json) | POST; others get 405 **before auth** | U | 200 `{headingPlaceholders,furnitureUpdated}` | FE `src/lib/api.ts:203` |
| [`/api/brand-extract`](api-brand-extract.json) | POST (not checked) | U | 202 `{jobId}` | FE `src/lib/api.ts:310` |
| [`/api/brand-extract-status`](api-brand-extract-status.json) | GET (not checked) | U | 200 `{status,fields?,error?}` | FE `src/lib/api.ts:316` |
| [`/api/brand-preview`](api-brand-preview.json) | POST (not checked) | U | 202 `{jobId}` | FE `src/lib/api.ts:223` |
| [`/api/brand-preview-status`](api-brand-preview-status.json) | GET (not checked) | U | 200 `{status,image?,error?}` | FE `src/lib/api.ts:229` |
| [`/api/admin-metrics`](api-admin-metrics.json) | GET (not checked) | A | 200 JSON (9 keys) | FE `src/pages/admin/AdminDashboard.tsx:77` |
| [`/api/admin-org-brands`](api-admin-org-brands.json) | GET, POST, DELETE; others get 405 after auth | A | 200/201 JSON | FE `src/lib/api.ts:384-400` |
| [`/api/analytics-event`](api-analytics-event.json) | POST (not checked) | U | 200 `{ok,stored,at}` or silent `{ok:true}` | FE `src/lib/analytics.ts:114` (fire-and-forget, keepalive) |
| `/api/*-background` ×7 ([file](api-background-functions.json)) | POST | **none** | 202, empty body | Internal triggers only (`_shared/trigger.ts`). Not served (D31) |

## Cross-cutting quirks the reimplementation must reproduce (or consciously change under D32/D33)

1. **Malformed JSON body:**
   - Most POST routes call `req.json()` without a catch, so a bad body gives **500 `{"error":"Internal error"}`**, not 400.
   - **These routes catch it:**
     - `analytics-event` gives a silent `{ok:true}`.
     - `admin-org-brands` and `brands` give 400 `"Invalid JSON body"`.
     - `intake` gives 400 `"messages array required"`.
     - `brand-extract` treats the body as `{}`, then gives 400 `"b64 PDF data required"`.
2. **A non-UUID `deckId` gives 500, not 403/404.** `ownsDeck` has no UUID guard, so Postgres rejects the cast. Affected routes: image, pdf, pdf-deck, pptx-submit, pptx-deck-submit, status, refine, revert, retry. Brand routes do have a UUID guard (`getBrandAccess`, `db.ts:228`); brands DELETE is the exception and gives 500.
3. **Validation failure on `/api/generate` gives 500 "Internal error".** A Zod error is not an `HttpError`. The Connector knows about this (`forge.py:328`).
4. **An unknown job id on the JSON status routes gives `200 {"status":"pending"}`, forever, with no 404.** Affected routes: storyline-status, quick-status, brand-pptx-status, brand-extract-status and brand-preview-status. Ownership is only checked once the record exists.
5. **The SlideForge-backed status and result routes have different error behaviour:**
   - An unknown or unfinished id gives **502**: `"PPTX service error"` on the status routes, `"PPTX service unavailable"` on the result routes.
   - **There is no ownership check.** Any signed-in user can poll or download any job id.
   - The Connector treats a status 502 as "still running", so the 502 must be kept.
6. **Method handling varies by route:**
   - Most routes ignore the method.
   - `decks` and `brand-asset` serve any non-DELETE / non-POST method as GET.
   - The 405 check comes before auth on slide-transcript, pptx-submit, pptx-deck-submit, brand-archetypes and brand-heading.
   - The 405 check comes after auth on image-to-slide, brand-pptx and admin-org-brands.
7. **Fields left out vs null:** `JSON.stringify` drops `undefined`. Optional keys such as status `url`/`error`/`masterSkipReason` and job `error` are **absent**, not `null`. Pydantic serialisation must use `exclude_none` or equivalent per field.
8. **Caps are not enforced:**
   - `reserveBrandExtract`, `reserveIntakeTurn` and `incrementProfileDeckCount` are defined in `_shared/db.ts` but never called. The only cap enforced is the global image cap (`reserveImageSlot`).
   - quick-generate's background run skips even that cap.
   - Neither storyline nor quick-generate has an upper bound on `numSlides`.
9. **A failed background trigger is swallowed.** The foreground still returns 202 and the job stays `pending` forever (`_shared/trigger.ts:1-25`).
10. **`/api/userinfo` is an outlier:**
    - Its error responses have no content-type header.
    - The 401 body is `{error, detail}`.
    - It does no signature verification.
    - Latin-1 `atob` garbles non-ASCII names.
    - It returns `sub` as well (oid ‖ sub).
11. **The frontend calls `/api/intake` twice per turn.** `streamIntake` (`src/lib/api.ts:81-139`) expects SSE frames, but the handler returns plain JSON. The stream ends without a `done` event, `IntakeChat.tsx:97-101` falls back to `postIntake`, and so every turn makes **2 paid Claude calls**. Decide before cutover whether the Python service implements SSE (the documented contract) or keeps JSON (the actual one).

## Discrepancies: openapi.yaml / postman / api.md vs handler code

**Coverage gaps:**
- **Absent from `public/openapi.yaml`:** pdf, pdf-deck, slide-transcript, brand-archetypes, brand-heading, admin-metrics, admin-org-brands, analytics-event.
- **Absent from `docs/postman_collection.json`:** slide-transcript, brands, brand-pptx, brand-pptx-status, brand-archetypes, brand-heading, admin-metrics, admin-org-brands, analytics-event, `DELETE /api/decks`.
- **Error responses** (400/403/404/405/415/422/500) are almost entirely undocumented in openapi. openapi also gives `deckId`/`jobId` `format: uuid`, which nothing enforces.

**Per route:**
- **intake:** openapi (`1606-1707`), postman 3.1 and `api.md:156` describe an SSE `text/event-stream` response. The handler returns `application/json` `{message, brief}`. `intakeSessionKey` is undocumented.
- **WizardInputs:** openapi requires topic, company, numSlides, audience and style, with numSlides at most 12. The handlers require only `topic` (plus `numSlides >= 1`) and set no maximum.
- **generate:**
  - The openapi and postman 2.3 example bodies are invalid: they have 2 bullets on kpi/data/market slides, which the schema rejects, so they return 500 against the real handler.
  - `StorylineSlide.prompt` is marked required but is ignored and rebuilt on the server.
  - `chartData` and `archetypeId` are missing.
- **status:**
  - openapi and api.md show `url`/`masterSkipReason` as `null`; in fact they are absent.
  - The progress example drops the suffix "slides…".
  - The image URL example has no `&v=`.
  - The body returned before any state exists is undocumented.
- **storyline-status:** the done example breaks the bullet rule and has no `warnings`.
- **retry and refine:** `DeckJobResponse` advertises `warnings`, which these routes never return. Refine's "max 4 MB per attachment" is not enforced.
- **decks:** openapi types `deck.storyline` as `StorylineResponse`, but the stored storyline has no `inputs` or `warnings`.
- **quick-status:** the `warnings` array (always present on done) is undocumented.
- **PptxJobStatus** (`openapi.yaml:375-384`, also in api.md): the enum is `[pending, done, failed]`, but the real upstream values are `queued | running | done | failed`.
- **pdf:** `api.md:1388` claims `cache-control: private, max-age=3600`. The handler sends none, deliberately, so that every download is counted.
- **image-to-slide:** the 502 body schema is undocumented (it is `{error}`).
- **brand-asset:**
  - openapi omits `fromLayoutIndex`, `kind=layout-preview` + `index`, and the cache headers (60s assets, 300s previews).
  - openapi and postman say assets are stored under the caller's id. They are stored under the brand owner's id.
  - api.md says a missing `index` gives 400. The code serves index 0.
- **brands:**
  - The `Brand` schema lacks `isOrg`, and GET is described as "owned by the caller", although org brands are included.
  - api.md claims a 20-brand cap. It exists only in RLS, which the API's service-role client bypasses.
- **BrandKitData:** the pass-through fields are missing (`layouts`, `workzone`, `typographyScale`, `masterDecorations`, `logoShapes`, `allColors`, `headingPlaceholders`, `archetypeLayouts`). The schema is also reused for brand-pptx-status `fields`, which carries `layoutPreviews`.
- **userinfo:** openapi says the dev bypass key returns `dev@sales.invalid`. It does not: userinfo never checks `DEV_SECRET_KEY`, and a dev key without dots gives 401 `{"error":"Invalid token","detail":...}`. Postman 7.1 therefore fails with the collection's own key.
- **Postman secret:** the collection-level auth hard-codes a live-looking dev bypass key in the `dev_key` variable of `docs/postman_collection.json`. If production has `DEV_SECRET_KEY` set to it, anyone with the repo can impersonate `DEV_USER_ID`. Rotate it, and remove it from the doc.

## Plan §6.5 vs code

**All 37 routes in §6.5 exist in the code, and the code has no extra routes.** The 7 `*-background` paths match the last row.

**Row corrections:**

| Route | §6.5 says | Code |
|---|---|---|
| userinfo | `{email,name,given_name,family_name}`; 400; 401 | Also returns `sub`. There are two 401 bodies, and `Invalid token` adds `detail`. Error responses have no content-type. |
| decks | GET `?id=` → deck | Returns `{deck, slides}` (raw DB rows plus `type`). DELETE of a missing deck still returns `{ok:true}`. Any non-DELETE method is treated as GET. |
| intake | → turn JSON | Returns `{message, brief}` as JSON. The frontend expects SSE first (double call; see quirk 11). There is no daily cap. |
| storyline-status | `{status,result?,error?}` / pending | Add 403 `"Not your job"`. An unknown id returns pending. |
| generate | 202 `{deckId, warnings}` | Invalid input returns **500** "Internal error", not 400. No deck cap. |
| status | `{deckId, slides, progress, done, masterApplied, masterSkipReason}` | Before any state exists the body is `{deckId, slides:{}, progress:"Starting…", done:false}`, with no `masterApplied`. Optional keys are omitted. |
| revert | → `{ok:true}` | Add 404 `"No such slide"` and 404 `"No such version"`. |
| slide-transcript | → `{ok:true}` | 405 `"Method not allowed"` comes before auth. DB errors are swallowed. |
| image | png, cache 3600 | Correct. Tile slides are composited at 2560×1440 on a best-effort basis. |
| pdf | `application/pdf` | No cache-control. |
| pptx-submit / pptx-deck-submit | → `{jobId}` / `{deckJobId}` | Status is **200**, not 202. Upstream 413 and 422 are mapped on pptx-submit only; all other upstream errors become 502. |
| pptx-status / -result / deck-status / deck-result | `{status, done, failed}` / bytes | Unknown or unfinished id returns **502**. No ownership check. Result filenames are fixed: `slide.pptx` and `deck.pptx`. |
| image-to-slide | multipart `file` ≤4 MB, **or JSON** | **JSON is rejected** with 400 `Send the image as multipart/form-data with a "file" field`. Oversize returns 400, not 413. |
| quick-status | `{status, slides?, warnings?, error?}` | `warnings` is always present ([]) when done. |
| quick-image | `image/png` | Add `cache-control: private, max-age=3600`. |
| brands | DELETE; PATCH `{brandId,name?,kit?}` | DELETE takes `?id=` and always returns `{ok:true}`; it is a no-op for org brands. PATCH returns `{id,name,kit,isOrg}` and merges the kit shallowly (null deletes a key). POST 201 has no `isOrg`. Blob-key fields are rejected on create. No 20-brand cap. The frontend is not a caller. |
| brand-asset | GET → PNG or `{template}` | Every non-POST method acts as GET. Cache is `private, max-age=60` for assets, 300 for layout previews, none for `style-default`. A POST with bad JSON returns 500. |
| brand-pptx | multipart `file`, `brandId` | `brandId` is optional. Non-multipart returns 415. The limit is 5 MiB. A non-editable brand returns 404, checked before size and type. |
| brand-extract | **cap 10 per user per day** | **No cap is enforced today** (`reserveBrandExtract` is never called). Adding one is a behaviour change (new 429). |
| admin-org-brands | DELETE `?domain=&brandId=` | DELETE takes `?domain=` **or** `?brandId=`, and domain wins if both are given. addDomain returns 200 when already mapped to the same brand. createBrand 201 returns `{id,name,kit:{},isOrg:true}`. Other methods return 405. |
| analytics-event | `{events[] ≤50}` → `{ok,stored,at}`; silent `{ok:true}` | More than 50 events are truncated, not rejected. `stored` counts skipped events. 401 still applies. |
| admin-metrics | 9 keys | Correct. `days` is clamped to 1..365, and 0 or an invalid value means 30. The `summary.*30d` key names don't change with `days`. |
| `*-background` | unauthenticated (C12) | Confirmed. In addition, `brand-extract-background` reads **any** brand-assets key named in its body (no ownership check). |

## Test baseline

Run offline with `npx vitest run netlify/functions` on Node v24.15.0: 391 tests, **6 failing**. None of the failures is a handler bug.

- **`_tests/status.test.ts:22-34`** is stale. It still expects the old `{A,B}` variant shape.
- **`_tests/image.test.ts`** (2 tests) has a stale mock. The blobs mock lacks `legacyImageKey`, so the handler returns 500 under test.
- **`_tests/brand-preview.test.ts`** does not compile. Line 69 has an unescaped apostrophe in `'forbids reading another user's job'`, so none of its tests run.
- **`_tests/brand-pptx-background.test.ts`** (3 tests) time out. The `fetch` stub answers the layout-render call with JSON, `renderLayouts` waits 5 s before retrying, and that exceeds vitest's 5 s limit.

**Routes with no unit test:** userinfo, analytics-event, admin-metrics, slide-transcript, brand-pptx, brand-pptx-status, pdf, pdf-deck, all pptx-status/result/deck routes, image-to-slide.
