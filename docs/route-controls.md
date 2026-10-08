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

## What the tests refuse

| If someone... | ...this fails |
|---|---|
| adds a route no row covers, or that two rows cover | `test_every_route_has_exactly_one_row_and_every_row_holds` |
| leaves a row that covers no served route | the same test |
| writes a `dependency` row whose route lacks the promised dependency | the same test |
| writes a `route` row whose handler does not call the promised protections | the same test |
| marks a route `public` while it takes credentials or dependencies | the same test |
| writes a row whose error format disagrees with the route class | the same test |
| changes the table without regenerating this page | `test_the_generated_documentation_matches_the_checklist` |

`test_the_robots_catch_what_they_should` runs the same checks against a synthetic app, so the
checker is proven to catch each failure even while the service has only public routes.

## Adding a route

1. Write the handler in the router for its area (`app/api/routes/<area>.py`). Set the prefix and
   tags on the `APIRouter(...)` itself, and add the router to `ROUTERS` in `app/main.py`.
2. Add a row to `RULES`, or add the route to an existing row.
3. Regenerate this page: `python tests/test_route_controls.py --write`.
4. Commit the code, the row and this page together.

Phase 1 serves only the health probes and the OpenAPI document. Darwin's existing `/api/*` routes
(about 44, all `legacy`) and the new routes arrive in Phases 5 and 7a; see the migration plan.

## The table

<!-- BEGIN GENERATED: route controls (python tests/test_route_controls.py --write) -->

### The checklist

| Entry | Who may call it | Enforced in | Rate class | Abuse budget | Licence | Errors | Notes |
|---|---|---|---|---|---|---|---|
| **health probes**<br>`GET /healthz`<br>`GET /readyz` | anyone (the platform's health check, verify scripts) | public | none | none; not routed through the gateway, so not rate limited by it | no | problem | No credentials by necessity; bodies reveal no hosts, variables or exceptions. |
| **OpenAPI spec**<br>`GET /openapi.json`<br>`HEAD /openapi.json` | anyone who can reach the service (behind the gateway once it exists) | public | none | none; static document generated from the code | no | problem | Interactive /docs and /redoc pages are disabled. |

### Every served route, and the row that covers it

| Route | Row |
|---|---|
| `GET /healthz` | health probes |
| `GET /openapi.json` | OpenAPI spec |
| `HEAD /openapi.json` | OpenAPI spec |
| `GET /readyz` | health probes |

<!-- END GENERATED: route controls -->
