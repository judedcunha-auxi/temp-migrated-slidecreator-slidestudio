# Darwin's `/api`: the legacy layer, its quirks, and how to port a route

This service answers every Darwin `/api/*` route with the **same path, method, request and
response** as `netlify/functions/*.ts` (plan §2.1, decision D24: Darwin's backend lives in this
service). This page is the guide for porting the remaining routes. Read it with:

- the **contract**, which is the test oracle: [`tests/contract/data/`](../tests/contract/data/), a
  copy of `Slide-Creator/docs/slideforge-migration/contract/` (`INDEX.md`, `_common.json`, one
  `api-<name>.json` per route). Where the contract disagrees with `openapi.yaml`, Postman or
  `api.md`, the contract wins;
- [route-controls.md](route-controls.md), because every route you add needs a row;
- [architecture.md](architecture.md#darwins-api-phase-7a), for where the pieces live.

## 1. What is ported

| Route | Status | Module |
|---|---|---|
| `/api/storyline`, `/api/storyline-status`, `/api/intake` | **ported** | `app/api/routes/storyline.py` |
| `/api/pptx-submit`, `-status`, `-result`, `/api/pptx-deck-submit`, `-status`, `-result` | **ported** | `app/api/routes/exports.py` |
| `/api/image-to-slide` | **ported** | `app/api/routes/exports.py` |
| the other 27 routes | pending (xfail "not yet ported" in the harness) | |
| the 7 `*-background` paths | **not served**, by decision (D31); the harness checks they 404 | |

## 2. The pieces you build on

| Need | Use | Where |
|---|---|---|
| A router whose errors are `{"error": "..."}` | `legacy_router(prefix="/api", tags=[...])` | `app/core/errors.py` (re-exported by `app/api/legacy.py`) |
| Register a handler for every method | `@darwin_route(router, "/name", methods=("POST",), summary=...)` | `app/api/legacy.py` |
| The signed-in caller | `user = await require_user(request)` (`user.ctx`, `user.user_id`, `user.profile`) | `app/api/deps.py` |
| An admin | `await require_admin(request)` (403 "Admin access required") | `app/api/deps.py` |
| Storage, the job queue, the models | `get_storage(request)`, `get_runtime(request)` (`.queue`, `.storyline_model`, `.images`, `.darwin` settings) | `app/api/deps.py` |
| A deliberate error | `raise ApiError(status, "<Darwin's exact text>")` | `app/core/errors.py` |
| A JSON answer | `json_response(body, status)` | `app/api/legacy.py` |
| The body | `await read_json(request)` (malformed: 500) or `await read_json_or_none(request)` (caught) | `app/api/legacy.py` |
| `const {a, b} = body` | `destructure(body)` (null: 500; non-object: no fields) | `app/api/legacy.py` |
| A required query parameter | `required_query(request, "jobId", "jobId is required")` | `app/api/legacy.py` |
| An id Darwin cast to uuid in Postgres | `postgres_uuid(value)` (non-uuid: 500) | `app/api/legacy.py` |
| JavaScript's `!x`, `parseInt` | `js_truthy(x)`, `js_parse_int(x)` | `app/api/legacy.py` |
| A 405 | `check_method(request, "POST", "Method not allowed")`, called before or after auth as the contract says | `app/api/legacy.py` |
| "Any other method is GET" | `effective_method(request, distinct=("DELETE",))` | `app/api/legacy.py` |
| Optional keys, omitted not null | `compact(**fields)` or a `LegacyModel` with `.body()` | `app/api/legacy.py` |
| An error with no content-type (userinfo) | `return error_response(401, {...}, content_type=False)` | `app/api/legacy.py` |
| Enqueue a job | `await submit_job(runtime.queue, user.ctx, "<type>", inputs)`; register the type in `app/core/darwin/runtime.py` | `app/core/darwin/jobs.py` |
| A JSON status route | `read_owned_job(...)` + `json_job_status(record, done=...)` | `app/core/darwin/jobs.py` |
| A pptx-style status/result route | `read_any_job(...)`, `pptx_status_body(...)`, `read_job_pptx(...)` | `app/core/darwin/jobs.py` |
| `ownsDeck` | `await owned_deck(storage, user.ctx, deck_id)` (403 "Not your deck") | `app/core/darwin/exports.py` |
| Record what a paid call cost | `record_usage(storage.usage, ctx, model=..., cost_usd=..., kind=...)` | `app/core/darwin/usage.py` |
| The image cap / per-user caps | `reserve_image_slot(redis)`, `reserve_intake_turn(...)`, `reserve_brand_extract(...)` | `app/core/darwin/caps.py` |
| Generate an image | `runtime.images.generate(prompt, size=..., transparent=...)`, `.edit(prompt, [png])` | `app/core/darwin/image_gen.py` |
| The brand kit | `normalize_kit(raw, legacy)`, `archetype_for_type(type)` (partial port of `brandKit.ts`) | `app/core/brand/kit.py` |

## 3. The quirks, and the helper for each

Each is in `INDEX.md` "Cross-cutting quirks"; the helper's docstring cites it.

1. **Malformed JSON is 500 "Internal error"** on most POST routes (`req.json()` uncaught):
   `read_json`. The five that catch it: intake (400 "messages array required"), brand-extract
   (treated as `{}`), analytics-event (silent `{ok:true}`), brands and admin-org-brands (400
   "Invalid JSON body"): `read_json_or_none`, then answer as the route does.
2. **A non-UUID `deckId` is 500**, not 403/404 (no guard before the Postgres cast): call
   `postgres_uuid(deck_id)` before `owned_deck`. Brand routes DO have a UUID guard (`getBrandAccess`),
   except brands DELETE.
3. **`/api/generate` validation failures are 500 "Internal error"** (the Connector relies on it).
4. **An unknown job id on a JSON status route is `200 {"status":"pending"}`**, forever:
   `read_owned_job` returns None and `json_job_status(None, ...)` is pending. Someone else's job is
   403 "Not your job".
5. **The pptx status and result routes answer 502** for an unknown or unfinished id ("PPTX service
   error" / "PPTX service unavailable") and have **no ownership check**: `read_any_job` reads with the
   service's identity. Kept, marked `# TODO-P5 ownership` (adding it is a D33 decision; the
   Connector reads a status 502 as "still running").
6. **Method handling varies:** most routes ignore the method (any method acts as the documented
   one); `check_method` before `require_user` on slide-transcript, pptx-submit, pptx-deck-submit,
   brand-archetypes, brand-heading; after it on image-to-slide ("POST only"), brand-pptx,
   admin-org-brands; `effective_method` on decks and brand-asset.
7. **Optional keys are absent, never null** (`JSON.stringify` drops undefined): `compact` /
   `LegacyModel`. A null INSIDE stored data stays null.
8. **Caps:** only the global image cap is enforced (`reserve_image_slot`); the per-user caps are
   wired and OFF (`INTAKE_CAP_ENABLED`, `BRAND_EXTRACT_CAP_ENABLED`). Turning one on adds a 429:
   a D12/D33 behaviour change.
9. **A failed background trigger was swallowed** (job pending forever): nothing to reproduce; the
   queue keeps a job until a worker runs it.
10. **`/api/userinfo`**: no verification, Latin-1 `atob`, two 401 bodies, no content-type on errors
    (`error_response(..., content_type=False)`).

Also: Darwin's `errorResponse` turns any non-`HttpError` into 500 `{"error":"Internal error"}`; on a
legacy route an unhandled exception does exactly that. A deliberate `ApiError` keeps its message at
any status (Darwin's fixed 5xx texts), so **never put exception text in an `ApiError`**.

## 4. Auth

`require_user` verifies a bearer JWT against a JWKS (`AUTH_ISSUER`, `AUTH_AUDIENCE`,
`AUTH_JWKS_URL`; the real issuer is decided later, D7/D8) and maps its `sub` to a profile through
the storage port (get-or-create). The bodies are Darwin's: 401 "Missing bearer token" (no `Bearer
<token>` header), 401 "Invalid or expired session" (any token problem), 403 "Admin access required";
an unreachable issuer is 500 "Internal error". **There is no DEV_SECRET_KEY bypass**: tests mint
tokens with `tests/fakes/identity.py`, and development uses a local issuer.

## 5. How to port a route

1. **Read its contract file** (`tests/contract/data/api-<name>.json`): `methodHandling`, `auth`,
   `request.parse`, `checkOrder`, `responses`, `errors`, `quirks`. The `checkOrder` is the order of
   your handler's statements.
2. **Write the handler** in the area's module (`app/api/routes/<area>.py`, plan §4.3), with
   `@darwin_route` on a `legacy_router(prefix="/api", tags=[...])`; add the router to `ROUTERS` in
   `app/main.py` if it is new. Keep logic in `app/core/darwin/*` (or the core module §4.3 names), not
   in the handler.
3. **Add its contract cases** (below). Every documented entry needs a probe or a `na(...)` reason.
4. **Add its negative tests** to `tests/api/routes/test_darwin_negative.py` (a `Route(...)` row).
5. **Add or extend its route-controls row** in `tests/test_route_controls.py` (`_darwin("<name>")`
   lists every method; `guard="route"`, `must_call=("require_user", ...)`, `errors="legacy"`, the
   rate class from §6.5, and the `todo_p5` markers), then regenerate:
   `python tests/test_route_controls.py --write`.
6. **Update the docs in the same commit**: this page's table (section 1), and anything in
   architecture.md that changes.

### Adding contract cases

A case module (`tests/contract/cases/<group>.py`) builds one `RouteCases` per route and lists them in
`ROUTES`; `tests/contract/cases/__init__.py` gathers the modules. Cases say HOW to provoke an entry;
the expected status, body, content-type and headers are read from the JSON:

```python
from tests.contract.cases.common import add_auth, broken
from tests.contract.harness import Probe, RouteCases

decks = RouteCases("/api/decks")
add_auth(decks, "GET", "/api/decks")      # both 401s, and the DEV_SECRET_KEY 500 marked not applicable
decks.add("R0", Probe("my decks", lambda env: env.client.get("/api/decks", headers=env.auth("alice"))))
decks.add("E500 Internal error", Probe("storage down",
          lambda env: (broken(env, "decks", "list_mine"), env.client.get("/api/decks", headers=env.auth("alice")))[1]))
decks.na("E4xx something", "why this cannot happen any more")
ROUTES = [decks]
```

- **Entry ids:** `R<i>` is the i-th `responses` item; `E<status> <error text>` is an `errors` item.
- **A probe** is `Probe(name, run, check=None)`: `run(env)` returns the response; `check(env, r)` adds
  assertions (e.g. that the job was queued with the right inputs, or the exact body of a response
  whose `bodyShape` is prose).
- **The environment** (`tests/fakes/darwin.py`, `DarwinEnv`): `env.client`, `env.auth("alice")`
  (a real signed token), `env.user(...)`, `env.seed_deck(...)`, `env.run_jobs()`, `env.job(...)`,
  `env.store` (the General service fake), `env.model` (scripted storyline model: append steps),
  `env.images` (fake image generator), `env.call(async_fn, ...)` (run on the app's loop).
- The harness fails an entry with neither a case nor a reason, and a case for an entry the contract
  does not document. Run it: `pytest tests/contract -q`.

## 6. Behaviour changes against Netlify Darwin

Deliberate, and to go in the release notes:

- **No DEV_SECRET_KEY bypass** (plan §4.3). Tokens come from the configured issuer; the 500
  "DEV_SECRET_KEY set but DEV_USER_ID missing" cannot happen.
- **Intake is one model call per turn** (C14, in the core) and stays JSON (decision 2026-10-09,
  option A): the frontend calls `postIntake` directly, so a turn is one request and one paid call.
- **The cost ledger records the cost** (C10): `model` holds the model, `est_cost_usd` the cost.
  Storyline jobs, intake turns and slide designs are now ledgered too (`kind` storyline / intake /
  design); Darwin only recorded images.
- **pptx-submit / image-to-slide no longer call Slide Studio's `/v1/jobs`.** The slide picture is
  rebuilt by the internal design pipeline (image -> HTML -> PPTX, D0). The OCR hints Darwin sent
  (`slide_context`, `slide_framework`, `chart_data`) are dropped with OCR. The upstream 413/422/502 at
  submit time cannot happen any more; a design failure shows as status `failed`.
- **pptx-deck-submit stitches only the caller's finished slide jobs.** Darwin's upstream stitched any
  ids; an id that is unknown, unfinished or someone else's now fails the deck job (status `failed`),
  where upstream failed it for unknown ids only.
- **image-to-slide accepts GIF as before, converted to PNG** (its first frame) for the pipeline.
- **Job ids are the durable record's UUIDs** (Slide Studio's were `j_xxxxxxxxxxxx`); the Connector
  only checks `[A-Za-z0-9_-]{1,128}`, which they meet.
- **The storyline job result has no per-slide `prompt` yet.** Darwin's image prompt (`prompt.ts`)
  is ported with the generation routes; it plugs in as the runtime's `prompter`.
- **Model and limits** of the storyline and intake: see architecture.md, "Behaviour changes against
  Darwin today" in the storyline section.
