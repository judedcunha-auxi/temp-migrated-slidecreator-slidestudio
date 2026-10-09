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
as Darwin's did), so each row lists them all; the table shows them as "every method". Ten Darwin
routes are served so far; the other 27 and the new routes arrive in Phase 7a and Phase 5.
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

### Every served route, and the row that covers it

| Route | Row |
|---|---|
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
| `GET /healthz` | health probes |
| `GET /openapi.json` | OpenAPI spec |
| `HEAD /openapi.json` | OpenAPI spec |
| `GET /readyz` | health probes |

<!-- END GENERATED: route controls -->
