# Route controls: every route, and what protects it

This page answers one question for every way into the service: who may call it, where the
protection lives, and what limits abuse. It is **enforced**: the tables below are generated from
`RULES` in [`tests/test_route_controls.py`](../tests/test_route_controls.py), and tests in that file
fail the build when the table and the code disagree. Read it before adding a route.

## How to read it

Each row covers one or more routes.

- **Enforced in** says where the protection lives:
  - `public`: deliberately open. The route may take no credentials and declare no dependencies.
  - `dependency`: a FastAPI dependency (`Depends(...)`) on the route. The row names the
    dependencies, and the test checks the route really declares them.
  - `route`: the handler protects itself. The row names the functions it must call, and the test
    checks the handler calls them (following calls within its module).
- **Rate class** is the standards' class: `standard` (the gateway default, 120 requests per minute
  per user), `expensive` (AI, GPU or large files: 10 per minute per user and at most 3 jobs in
  flight), or `none` (the health probes, which the gateway does not route).
- **Licence** says whether licensing is checked and, where it is, whether the route fails open or
  closed when the licensing service is down (decision D10, set per feature).
- **Errors** is the error format (decision D32): `legacy` routes are today's Darwin `/api/*` routes
  and answer `{"error": "<message>"}`; `problem` routes answer RFC 9457 Problem Details. The test
  checks the route class agrees.
- **TODO-P5** is what Phase 5 still owes the row: `rate-limit` (Redis per-user rate limit),
  `in-flight` (the cap of 3 expensive jobs per user), `licence` (the D10 check), `ownership` (a
  per-object check behind a decision; the handler carries a `TODO-P5 ownership` comment where it
  goes), `over-the-limit-test` (the sixth negative test). An `expensive` row must mark
  `rate-limit` and `in-flight` until they are enforced. Remove a marker in the same change that
  enforces it.

## What the tests refuse

| If someone... | ...this fails |
|---|---|
| adds a route no row covers, or that two rows cover | `test_every_route_has_exactly_one_row_and_every_row_holds` |
| leaves a row that covers no served route | the same test |
| writes a `dependency` row whose route lacks the promised dependency | the same test |
| writes a `route` row whose handler does not call the promised protections | the same test |
| marks a route `public` while it takes credentials or dependencies | the same test |
| writes a row whose error format disagrees with the route class | the same test |
| uses an unknown TODO-P5 marker, marks `ownership` without the handler comment, or leaves an `expensive` row's limits unmarked | the same test |
| changes the table without regenerating this page | `test_the_generated_documentation_matches_the_checklist` |

`test_the_robots_catch_what_they_should` runs the same checks against a synthetic app, so the
checker is proven to catch each failure even while the service has only public routes.

## Adding a route

1. Write the handler in the router for its area (`app/api/routes/<area>.py`). Set the prefix and
   tags on the `APIRouter(...)` itself, and add the router to `ROUTERS` in `app/main.py`.
2. Add a row to `RULES`, or add the route to an existing row.
3. Regenerate this page: `python tests/test_route_controls.py --write`.
4. Commit the code, the row and this page together.

Darwin's `/api/*` routes are registered for every method (the handler answers an unexpected one,
as Darwin's did), so each row lists them all; the table shows them as "every method". All 37 Darwin
routes are served (Phase 7a); the new routes arrive with Phase 5 and 7c.
[darwin-api.md](darwin-api.md) is the porting guide.

## The table

<!-- BEGIN GENERATED: route controls (python tests/test_route_controls.py --write) -->

### The checklist

| Entry | Who may call it | Enforced in | Rate class | Abuse budget | Licence | Errors | TODO-P5 | Notes |
|---|---|---|---|---|---|---|---|---|
| **health probes**<br>`GET /healthz`<br>`GET /readyz` | anyone (the platform's health check, verify scripts) | public | none | none; not routed through the gateway, so not rate limited by it | no | problem | - | No credentials by necessity; bodies reveal no hosts, variables or exceptions. |
| **OpenAPI spec**<br>`GET /openapi.json`<br>`HEAD /openapi.json` | anyone who can reach the service (behind the gateway once it exists) | public | none | none; static document generated from the code | no | problem | - | Interactive /docs and /redoc pages are disabled. |
| **Darwin storyline + intake**<br>`/api/storyline` (every method)<br>`/api/intake` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py) | route | expensive | paid model calls: storyline (one structured call per job), intake (one call per turn); intake text <= 30000 chars, 12-reply wrap-up; per-user intake cap wired, OFF (D12) | not checked yet (D10) | legacy | rate-limit, in-flight, licence, over-the-limit-test | Method not checked (any method acts as POST). Malformed JSON: 500 on storyline, 400 on intake. |
| **Darwin storyline status**<br>`/api/storyline-status` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py); the job's owner (403 'Not your job'); an unknown id is 200 pending | route | standard | a read of one job record | no | legacy | rate-limit, over-the-limit-test |  |
| **Darwin PPTX submit**<br>`/api/pptx-submit` (every method)<br>`/api/pptx-deck-submit` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py); the deck's owner (403 'Not your deck') | route | expensive | one design_and_export / stitch job per call (paid design turn + export) | not checked yet (D10) | legacy | rate-limit, in-flight, licence, over-the-limit-test | 405 is checked BEFORE auth (Darwin's order). Success is 200, not 202. |
| **Darwin PPTX status + result**<br>`/api/pptx-status` (every method)<br>`/api/pptx-result` (every method)<br>`/api/pptx-deck-status` (every method)<br>`/api/pptx-deck-result` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py); NO ownership check today (any job id), kept for the Connector | route | standard | a read of one job record (and one .pptx) | no | legacy | rate-limit, ownership, over-the-limit-test | Unknown or unfinished id: 502 (the Connector reads it as still running). Adding the ownership check is a D33 behaviour change; someone else's job must then answer the same 502. |
| **Darwin image-to-slide**<br>`/api/image-to-slide` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py) (the Auxi Connector) | route | expensive | multipart image <= 4 MiB; one design_and_export job (paid design turn) per call | not checked yet (D10) | legacy | rate-limit, in-flight, licence, over-the-limit-test | 405 'POST only' AFTER auth. JSON bodies are a 400. |
| **Darwin decks**<br>`/api/decks` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py); the deck's owner (another user's deck is 404 'Deck not found'; DELETE is a no-op) | route | standard | reads of the caller's decks; DELETE of one owned deck | no | legacy | rate-limit, over-the-limit-test | Only DELETE is told apart; every other method is a GET. Non-uuid id: 500. |
| **Darwin generate**<br>`/api/generate` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py) | route | expensive | one darwin.generate job: one paid image per slide (global image cap; no slide or deck cap, as Darwin) | not checked yet (D10) | legacy | rate-limit, in-flight, licence, over-the-limit-test | Method not checked. Any storyline validation failure is 500 'Internal error' (the Connector relies on it). |
| **Darwin retry + refine**<br>`/api/retry` (every method)<br>`/api/refine` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py); the deck's owner (403 'Not your deck') | route | expensive | one darwin.generate (listed slides) or darwin.refine (one slide) job: paid images under the global image cap; refine instruction <= 600 chars | not checked yet (D10) | legacy | rate-limit, in-flight, licence, over-the-limit-test | Method not checked. The jobs run as the job's owner: the -background bodies' userId is gone (C12). |
| **Darwin status + revert**<br>`/api/status` (every method)<br>`/api/revert` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py); the deck's owner (403 'Not your deck') | route | standard | reads of one deck's slides; revert moves a version pointer (no paid call) | no | legacy | rate-limit, over-the-limit-test |  |
| **Darwin slide-transcript**<br>`/api/slide-transcript` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py); the deck's owner (403 'Not your deck') | route | standard | one transcript upsert per call (attachment payloads stripped) | no | legacy | rate-limit, over-the-limit-test | 405 is checked BEFORE auth (Darwin's order). Storage errors are swallowed. |
| **Darwin image + pdf**<br>`/api/image` (every method)<br>`/api/pdf` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py); the deck's owner (403 'Not your deck') | route | standard | one stored picture per call (a tile is composited at 2560x1440; the PDF wraps one PNG) | no | legacy | rate-limit, over-the-limit-test | No method check. image: cache-control private, max-age=3600; pdf: none (every download counted). |
| **Darwin pdf-deck**<br>`/api/pdf-deck` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py); the deck's owner (403 'Not your deck') | route | expensive | one PDF of every done slide (CPU-bound, no paid call) | no | legacy | rate-limit, in-flight, licence, over-the-limit-test | The Connector reads 5xx/429 as 'still running' and any other 4xx as terminal. |
| **Darwin quick-generate**<br>`/api/quick-generate` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py) (external API users; no caller in Darwin or the Connector) | route | expensive | one darwin.quick_generate job: one paid storyline call and one paid image per slide; the global image cap now applies (C12); inline layout PNG <= 4 MiB | not checked yet (D10) | legacy | rate-limit, in-flight, licence, over-the-limit-test | No upper bound on numSlides, as Darwin. |
| **Darwin quick-status**<br>`/api/quick-status` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py); the job's owner (403 'Not your job'); an unknown id is 200 pending | route | standard | a read of one job record | no | legacy | rate-limit, over-the-limit-test |  |
| **Darwin quick-image**<br>`/api/quick-image` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py); the job's owner (403 'Not your job'; unknown id 404) | route | standard | one stored picture per call | no | legacy | rate-limit, over-the-limit-test |  |
| **Darwin brands**<br>`/api/brands` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py); brand access is Darwin's getBrandAccess: owner, admins for org brands, read-only members of a mapped VERIFIED email domain; an unreachable brand is 404 (403 for a member's PATCH) | route | standard | kit JSON <= 256K characters; 20 personal brands (the store's cap: 409, new); list/patch/delete | no | legacy | rate-limit, over-the-limit-test | Other methods 405 AFTER auth. DELETE never removes an org brand (admin-org-brands does). |
| **Darwin brand assets**<br>`/api/brand-asset` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py); brand access is Darwin's getBrandAccess: owner, admins for org brands, read-only members of a mapped VERIFIED email domain; an unreachable brand is 404; uploads need edit (403 for a member) | route | standard | one PNG <= 4 MiB decoded per upload; reads of one asset | no | legacy | rate-limit, over-the-limit-test | Every non-POST method acts as GET. style-default needs no brand. |
| **Darwin brand template import + guidelines extraction**<br>`/api/brand-pptx` (every method)<br>`/api/brand-extract` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py); a named brand must be editable (404 otherwise, members included) | route | expensive | brand-pptx: multipart .pptx <= 5 MiB, one extract + layout render job; brand-extract: PDF <= 4 MiB, one paid model call per job; per-user daily extraction cap wired, OFF (D12) | not checked yet (D10) | legacy | rate-limit, in-flight, licence, over-the-limit-test | 405 'POST only' AFTER auth. The -background functions they triggered are internal jobs (D31). |
| **Darwin brand preview**<br>`/api/brand-preview` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py); the brand must be editable (404 otherwise, members included) | route | expensive | one gpt-image call per job unless the content-hash cache hits; global daily image cap (enforced) | not checked yet (D10) | legacy | rate-limit, in-flight, licence, over-the-limit-test | No method check; a bodyless request is 500 (uncaught req.json()). |
| **Darwin brand job status**<br>`/api/brand-pptx-status` (every method)<br>`/api/brand-extract-status` (every method)<br>`/api/brand-preview-status` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py); the job's owner (403 'Not your job'); an unknown id is 200 pending | route | standard | a read of one job record (brand-preview-status: and one PNG) | no | legacy | rate-limit, over-the-limit-test |  |
| **Darwin brand archetypes + heading**<br>`/api/brand-archetypes` (every method)<br>`/api/brand-heading` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py); brand access is Darwin's getBrandAccess: owner, admins for org brands, read-only members of a mapped VERIFIED email domain; an unreachable brand is 404; needs edit (403 for a member) | route | standard | copies three stored layout PNGs / rewrites one furniture JSON and the kit | no | legacy | rate-limit, over-the-limit-test | 405 'POST only' BEFORE auth. Kept for the contract; deprecation candidates (D33). |
| **Darwin admin**<br>`/api/admin-metrics` (every method)<br>`/api/admin-org-brands` (every method) | an admin only (profiles.is_admin, server-only): 401, then 403 'Admin access required' | route | standard | admin-metrics: one overview + one activity read (<= 2000 events); admin-org-brands: one write | no | legacy | rate-limit, over-the-limit-test | admin-org-brands: other methods 405 AFTER the admin check. |
| **Darwin analytics events**<br>`/api/analytics-event` (every method) | any signed-in user (bearer JWT, verified in-service: app/core/auth.py) | route | standard | <= 50 events per call (the rest dropped silently); writes swallowed | no | legacy | rate-limit, over-the-limit-test | A malformed body is a silent 200 {ok:true}. |
| **Darwin userinfo (OIDC shim)**<br>`/api/userinfo` (every method) | anyone presenting a JWT-shaped bearer: it is DECODED, NOT VERIFIED (C8; Supabase Auth's custom:auxi provider calls it with a fresh IdP token) | public | standard | decodes one header; no storage, no model | no | legacy | rate-limit, over-the-limit-test | Reveals only what the presented token says. Kept while the identity flow needs it (D25); errors are text/plain (Fetch default), as Darwin. |

### Every served route, and the row that covers it

| Route | Row |
|---|---|
| `DELETE /api/admin-metrics` | Darwin admin |
| `GET /api/admin-metrics` | Darwin admin |
| `HEAD /api/admin-metrics` | Darwin admin |
| `OPTIONS /api/admin-metrics` | Darwin admin |
| `PATCH /api/admin-metrics` | Darwin admin |
| `POST /api/admin-metrics` | Darwin admin |
| `PUT /api/admin-metrics` | Darwin admin |
| `DELETE /api/admin-org-brands` | Darwin admin |
| `GET /api/admin-org-brands` | Darwin admin |
| `HEAD /api/admin-org-brands` | Darwin admin |
| `OPTIONS /api/admin-org-brands` | Darwin admin |
| `PATCH /api/admin-org-brands` | Darwin admin |
| `POST /api/admin-org-brands` | Darwin admin |
| `PUT /api/admin-org-brands` | Darwin admin |
| `DELETE /api/analytics-event` | Darwin analytics events |
| `GET /api/analytics-event` | Darwin analytics events |
| `HEAD /api/analytics-event` | Darwin analytics events |
| `OPTIONS /api/analytics-event` | Darwin analytics events |
| `PATCH /api/analytics-event` | Darwin analytics events |
| `POST /api/analytics-event` | Darwin analytics events |
| `PUT /api/analytics-event` | Darwin analytics events |
| `DELETE /api/brand-archetypes` | Darwin brand archetypes + heading |
| `GET /api/brand-archetypes` | Darwin brand archetypes + heading |
| `HEAD /api/brand-archetypes` | Darwin brand archetypes + heading |
| `OPTIONS /api/brand-archetypes` | Darwin brand archetypes + heading |
| `PATCH /api/brand-archetypes` | Darwin brand archetypes + heading |
| `POST /api/brand-archetypes` | Darwin brand archetypes + heading |
| `PUT /api/brand-archetypes` | Darwin brand archetypes + heading |
| `DELETE /api/brand-asset` | Darwin brand assets |
| `GET /api/brand-asset` | Darwin brand assets |
| `HEAD /api/brand-asset` | Darwin brand assets |
| `OPTIONS /api/brand-asset` | Darwin brand assets |
| `PATCH /api/brand-asset` | Darwin brand assets |
| `POST /api/brand-asset` | Darwin brand assets |
| `PUT /api/brand-asset` | Darwin brand assets |
| `DELETE /api/brand-extract` | Darwin brand template import + guidelines extraction |
| `GET /api/brand-extract` | Darwin brand template import + guidelines extraction |
| `HEAD /api/brand-extract` | Darwin brand template import + guidelines extraction |
| `OPTIONS /api/brand-extract` | Darwin brand template import + guidelines extraction |
| `PATCH /api/brand-extract` | Darwin brand template import + guidelines extraction |
| `POST /api/brand-extract` | Darwin brand template import + guidelines extraction |
| `PUT /api/brand-extract` | Darwin brand template import + guidelines extraction |
| `DELETE /api/brand-extract-status` | Darwin brand job status |
| `GET /api/brand-extract-status` | Darwin brand job status |
| `HEAD /api/brand-extract-status` | Darwin brand job status |
| `OPTIONS /api/brand-extract-status` | Darwin brand job status |
| `PATCH /api/brand-extract-status` | Darwin brand job status |
| `POST /api/brand-extract-status` | Darwin brand job status |
| `PUT /api/brand-extract-status` | Darwin brand job status |
| `DELETE /api/brand-heading` | Darwin brand archetypes + heading |
| `GET /api/brand-heading` | Darwin brand archetypes + heading |
| `HEAD /api/brand-heading` | Darwin brand archetypes + heading |
| `OPTIONS /api/brand-heading` | Darwin brand archetypes + heading |
| `PATCH /api/brand-heading` | Darwin brand archetypes + heading |
| `POST /api/brand-heading` | Darwin brand archetypes + heading |
| `PUT /api/brand-heading` | Darwin brand archetypes + heading |
| `DELETE /api/brand-pptx` | Darwin brand template import + guidelines extraction |
| `GET /api/brand-pptx` | Darwin brand template import + guidelines extraction |
| `HEAD /api/brand-pptx` | Darwin brand template import + guidelines extraction |
| `OPTIONS /api/brand-pptx` | Darwin brand template import + guidelines extraction |
| `PATCH /api/brand-pptx` | Darwin brand template import + guidelines extraction |
| `POST /api/brand-pptx` | Darwin brand template import + guidelines extraction |
| `PUT /api/brand-pptx` | Darwin brand template import + guidelines extraction |
| `DELETE /api/brand-pptx-status` | Darwin brand job status |
| `GET /api/brand-pptx-status` | Darwin brand job status |
| `HEAD /api/brand-pptx-status` | Darwin brand job status |
| `OPTIONS /api/brand-pptx-status` | Darwin brand job status |
| `PATCH /api/brand-pptx-status` | Darwin brand job status |
| `POST /api/brand-pptx-status` | Darwin brand job status |
| `PUT /api/brand-pptx-status` | Darwin brand job status |
| `DELETE /api/brand-preview` | Darwin brand preview |
| `GET /api/brand-preview` | Darwin brand preview |
| `HEAD /api/brand-preview` | Darwin brand preview |
| `OPTIONS /api/brand-preview` | Darwin brand preview |
| `PATCH /api/brand-preview` | Darwin brand preview |
| `POST /api/brand-preview` | Darwin brand preview |
| `PUT /api/brand-preview` | Darwin brand preview |
| `DELETE /api/brand-preview-status` | Darwin brand job status |
| `GET /api/brand-preview-status` | Darwin brand job status |
| `HEAD /api/brand-preview-status` | Darwin brand job status |
| `OPTIONS /api/brand-preview-status` | Darwin brand job status |
| `PATCH /api/brand-preview-status` | Darwin brand job status |
| `POST /api/brand-preview-status` | Darwin brand job status |
| `PUT /api/brand-preview-status` | Darwin brand job status |
| `DELETE /api/brands` | Darwin brands |
| `GET /api/brands` | Darwin brands |
| `HEAD /api/brands` | Darwin brands |
| `OPTIONS /api/brands` | Darwin brands |
| `PATCH /api/brands` | Darwin brands |
| `POST /api/brands` | Darwin brands |
| `PUT /api/brands` | Darwin brands |
| `DELETE /api/decks` | Darwin decks |
| `GET /api/decks` | Darwin decks |
| `HEAD /api/decks` | Darwin decks |
| `OPTIONS /api/decks` | Darwin decks |
| `PATCH /api/decks` | Darwin decks |
| `POST /api/decks` | Darwin decks |
| `PUT /api/decks` | Darwin decks |
| `DELETE /api/generate` | Darwin generate |
| `GET /api/generate` | Darwin generate |
| `HEAD /api/generate` | Darwin generate |
| `OPTIONS /api/generate` | Darwin generate |
| `PATCH /api/generate` | Darwin generate |
| `POST /api/generate` | Darwin generate |
| `PUT /api/generate` | Darwin generate |
| `DELETE /api/image` | Darwin image + pdf |
| `GET /api/image` | Darwin image + pdf |
| `HEAD /api/image` | Darwin image + pdf |
| `OPTIONS /api/image` | Darwin image + pdf |
| `PATCH /api/image` | Darwin image + pdf |
| `POST /api/image` | Darwin image + pdf |
| `PUT /api/image` | Darwin image + pdf |
| `DELETE /api/image-to-slide` | Darwin image-to-slide |
| `GET /api/image-to-slide` | Darwin image-to-slide |
| `HEAD /api/image-to-slide` | Darwin image-to-slide |
| `OPTIONS /api/image-to-slide` | Darwin image-to-slide |
| `PATCH /api/image-to-slide` | Darwin image-to-slide |
| `POST /api/image-to-slide` | Darwin image-to-slide |
| `PUT /api/image-to-slide` | Darwin image-to-slide |
| `DELETE /api/intake` | Darwin storyline + intake |
| `GET /api/intake` | Darwin storyline + intake |
| `HEAD /api/intake` | Darwin storyline + intake |
| `OPTIONS /api/intake` | Darwin storyline + intake |
| `PATCH /api/intake` | Darwin storyline + intake |
| `POST /api/intake` | Darwin storyline + intake |
| `PUT /api/intake` | Darwin storyline + intake |
| `DELETE /api/pdf` | Darwin image + pdf |
| `GET /api/pdf` | Darwin image + pdf |
| `HEAD /api/pdf` | Darwin image + pdf |
| `OPTIONS /api/pdf` | Darwin image + pdf |
| `PATCH /api/pdf` | Darwin image + pdf |
| `POST /api/pdf` | Darwin image + pdf |
| `PUT /api/pdf` | Darwin image + pdf |
| `DELETE /api/pdf-deck` | Darwin pdf-deck |
| `GET /api/pdf-deck` | Darwin pdf-deck |
| `HEAD /api/pdf-deck` | Darwin pdf-deck |
| `OPTIONS /api/pdf-deck` | Darwin pdf-deck |
| `PATCH /api/pdf-deck` | Darwin pdf-deck |
| `POST /api/pdf-deck` | Darwin pdf-deck |
| `PUT /api/pdf-deck` | Darwin pdf-deck |
| `DELETE /api/pptx-deck-result` | Darwin PPTX status + result |
| `GET /api/pptx-deck-result` | Darwin PPTX status + result |
| `HEAD /api/pptx-deck-result` | Darwin PPTX status + result |
| `OPTIONS /api/pptx-deck-result` | Darwin PPTX status + result |
| `PATCH /api/pptx-deck-result` | Darwin PPTX status + result |
| `POST /api/pptx-deck-result` | Darwin PPTX status + result |
| `PUT /api/pptx-deck-result` | Darwin PPTX status + result |
| `DELETE /api/pptx-deck-status` | Darwin PPTX status + result |
| `GET /api/pptx-deck-status` | Darwin PPTX status + result |
| `HEAD /api/pptx-deck-status` | Darwin PPTX status + result |
| `OPTIONS /api/pptx-deck-status` | Darwin PPTX status + result |
| `PATCH /api/pptx-deck-status` | Darwin PPTX status + result |
| `POST /api/pptx-deck-status` | Darwin PPTX status + result |
| `PUT /api/pptx-deck-status` | Darwin PPTX status + result |
| `DELETE /api/pptx-deck-submit` | Darwin PPTX submit |
| `GET /api/pptx-deck-submit` | Darwin PPTX submit |
| `HEAD /api/pptx-deck-submit` | Darwin PPTX submit |
| `OPTIONS /api/pptx-deck-submit` | Darwin PPTX submit |
| `PATCH /api/pptx-deck-submit` | Darwin PPTX submit |
| `POST /api/pptx-deck-submit` | Darwin PPTX submit |
| `PUT /api/pptx-deck-submit` | Darwin PPTX submit |
| `DELETE /api/pptx-result` | Darwin PPTX status + result |
| `GET /api/pptx-result` | Darwin PPTX status + result |
| `HEAD /api/pptx-result` | Darwin PPTX status + result |
| `OPTIONS /api/pptx-result` | Darwin PPTX status + result |
| `PATCH /api/pptx-result` | Darwin PPTX status + result |
| `POST /api/pptx-result` | Darwin PPTX status + result |
| `PUT /api/pptx-result` | Darwin PPTX status + result |
| `DELETE /api/pptx-status` | Darwin PPTX status + result |
| `GET /api/pptx-status` | Darwin PPTX status + result |
| `HEAD /api/pptx-status` | Darwin PPTX status + result |
| `OPTIONS /api/pptx-status` | Darwin PPTX status + result |
| `PATCH /api/pptx-status` | Darwin PPTX status + result |
| `POST /api/pptx-status` | Darwin PPTX status + result |
| `PUT /api/pptx-status` | Darwin PPTX status + result |
| `DELETE /api/pptx-submit` | Darwin PPTX submit |
| `GET /api/pptx-submit` | Darwin PPTX submit |
| `HEAD /api/pptx-submit` | Darwin PPTX submit |
| `OPTIONS /api/pptx-submit` | Darwin PPTX submit |
| `PATCH /api/pptx-submit` | Darwin PPTX submit |
| `POST /api/pptx-submit` | Darwin PPTX submit |
| `PUT /api/pptx-submit` | Darwin PPTX submit |
| `DELETE /api/quick-generate` | Darwin quick-generate |
| `GET /api/quick-generate` | Darwin quick-generate |
| `HEAD /api/quick-generate` | Darwin quick-generate |
| `OPTIONS /api/quick-generate` | Darwin quick-generate |
| `PATCH /api/quick-generate` | Darwin quick-generate |
| `POST /api/quick-generate` | Darwin quick-generate |
| `PUT /api/quick-generate` | Darwin quick-generate |
| `DELETE /api/quick-image` | Darwin quick-image |
| `GET /api/quick-image` | Darwin quick-image |
| `HEAD /api/quick-image` | Darwin quick-image |
| `OPTIONS /api/quick-image` | Darwin quick-image |
| `PATCH /api/quick-image` | Darwin quick-image |
| `POST /api/quick-image` | Darwin quick-image |
| `PUT /api/quick-image` | Darwin quick-image |
| `DELETE /api/quick-status` | Darwin quick-status |
| `GET /api/quick-status` | Darwin quick-status |
| `HEAD /api/quick-status` | Darwin quick-status |
| `OPTIONS /api/quick-status` | Darwin quick-status |
| `PATCH /api/quick-status` | Darwin quick-status |
| `POST /api/quick-status` | Darwin quick-status |
| `PUT /api/quick-status` | Darwin quick-status |
| `DELETE /api/refine` | Darwin retry + refine |
| `GET /api/refine` | Darwin retry + refine |
| `HEAD /api/refine` | Darwin retry + refine |
| `OPTIONS /api/refine` | Darwin retry + refine |
| `PATCH /api/refine` | Darwin retry + refine |
| `POST /api/refine` | Darwin retry + refine |
| `PUT /api/refine` | Darwin retry + refine |
| `DELETE /api/retry` | Darwin retry + refine |
| `GET /api/retry` | Darwin retry + refine |
| `HEAD /api/retry` | Darwin retry + refine |
| `OPTIONS /api/retry` | Darwin retry + refine |
| `PATCH /api/retry` | Darwin retry + refine |
| `POST /api/retry` | Darwin retry + refine |
| `PUT /api/retry` | Darwin retry + refine |
| `DELETE /api/revert` | Darwin status + revert |
| `GET /api/revert` | Darwin status + revert |
| `HEAD /api/revert` | Darwin status + revert |
| `OPTIONS /api/revert` | Darwin status + revert |
| `PATCH /api/revert` | Darwin status + revert |
| `POST /api/revert` | Darwin status + revert |
| `PUT /api/revert` | Darwin status + revert |
| `DELETE /api/slide-transcript` | Darwin slide-transcript |
| `GET /api/slide-transcript` | Darwin slide-transcript |
| `HEAD /api/slide-transcript` | Darwin slide-transcript |
| `OPTIONS /api/slide-transcript` | Darwin slide-transcript |
| `PATCH /api/slide-transcript` | Darwin slide-transcript |
| `POST /api/slide-transcript` | Darwin slide-transcript |
| `PUT /api/slide-transcript` | Darwin slide-transcript |
| `DELETE /api/status` | Darwin status + revert |
| `GET /api/status` | Darwin status + revert |
| `HEAD /api/status` | Darwin status + revert |
| `OPTIONS /api/status` | Darwin status + revert |
| `PATCH /api/status` | Darwin status + revert |
| `POST /api/status` | Darwin status + revert |
| `PUT /api/status` | Darwin status + revert |
| `DELETE /api/storyline` | Darwin storyline + intake |
| `GET /api/storyline` | Darwin storyline + intake |
| `HEAD /api/storyline` | Darwin storyline + intake |
| `OPTIONS /api/storyline` | Darwin storyline + intake |
| `PATCH /api/storyline` | Darwin storyline + intake |
| `POST /api/storyline` | Darwin storyline + intake |
| `PUT /api/storyline` | Darwin storyline + intake |
| `DELETE /api/storyline-status` | Darwin storyline status |
| `GET /api/storyline-status` | Darwin storyline status |
| `HEAD /api/storyline-status` | Darwin storyline status |
| `OPTIONS /api/storyline-status` | Darwin storyline status |
| `PATCH /api/storyline-status` | Darwin storyline status |
| `POST /api/storyline-status` | Darwin storyline status |
| `PUT /api/storyline-status` | Darwin storyline status |
| `DELETE /api/userinfo` | Darwin userinfo (OIDC shim) |
| `GET /api/userinfo` | Darwin userinfo (OIDC shim) |
| `HEAD /api/userinfo` | Darwin userinfo (OIDC shim) |
| `OPTIONS /api/userinfo` | Darwin userinfo (OIDC shim) |
| `PATCH /api/userinfo` | Darwin userinfo (OIDC shim) |
| `POST /api/userinfo` | Darwin userinfo (OIDC shim) |
| `PUT /api/userinfo` | Darwin userinfo (OIDC shim) |
| `GET /healthz` | health probes |
| `GET /openapi.json` | OpenAPI spec |
| `HEAD /openapi.json` | OpenAPI spec |
| `GET /readyz` | health probes |

<!-- END GENERATED: route controls -->
