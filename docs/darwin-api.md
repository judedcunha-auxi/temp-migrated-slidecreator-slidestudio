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
| `/api/brands`, `/api/brand-asset`, `/api/brand-pptx`, `-status`, `/api/brand-archetypes`, `/api/brand-heading`, `/api/brand-extract`, `-status`, `/api/brand-preview`, `-status` | **ported** | `app/api/routes/brands.py` |
| `/api/admin-metrics`, `/api/admin-org-brands` | **ported** | `app/api/routes/admin.py` |
| `/api/analytics-event` | **ported** | `app/api/routes/analytics.py` |
| `/api/userinfo` | **ported** | `app/api/routes/identity.py` |
| `/api/decks` | **ported** | `app/api/routes/decks.py` |
| `/api/generate`, `/api/retry`, `/api/status` | **ported** (`generate-background` is the `darwin.generate` job) | `app/api/routes/generate.py`, `app/core/darwin/generate.py` |
| `/api/refine`, `/api/revert`, `/api/slide-transcript` | **ported** (`refine-background` is the `darwin.refine` job) | `app/api/routes/refine.py`, `app/core/darwin/refine.py` |
| `/api/image`, `/api/pdf`, `/api/pdf-deck` | **ported** (image mode; sharp/pdf-lib -> Pillow) | `app/api/routes/media.py`, `app/core/darwin/{media,compose}.py` |
| `/api/quick-generate`, `/api/quick-status`, `/api/quick-image` | **ported** (`quick-generate-background` is the `darwin.quick_generate` job) | `app/api/routes/quick.py`, `app/core/darwin/quick.py` |
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
| The brand kit | `normalize_kit(raw, legacy)`, `archetype_for_type(type)`; the write side: `sanitize_kit_patch`, `merge_kit`, `brand_asset_key`, `normalize_heading_placeholder` | `app/core/brand/kit.py` |
| `getBrandAccess` / `requireBrand` | `get_brand_access(storage, ctx, id)` (None: not visible), `require_brand(..., "read" or "edit")` (400/404/403), `editable_or_404(...)` (a member gets 404) | `app/core/darwin/brands.py` |
| JavaScript's `Number()`, `Number.isInteger`, `Buffer.from(s, "base64")`, `Math.round`, `String(n)`, `trim`, `.length` | `js_number`, `js_is_integer` (= `is_js_integer`), `node_base64`, `js_round`, `js_str`, `js_trim`, `js_length` | `app/core/darwin/js.py` |
| The kit as the image prompt reads it; the brand to use | `prompt_kit(raw, legacy)`, `kit_for_prompt(kit)`, `resolve_brand_source(storage, ctx, brand_id)` | `app/core/darwin/prompt.py` |
| Darwin's image prompt | `assemble_slide_prompt(...)`, `steered_prompt(...)`, `match_layout`, `layout_hint_text` | `app/core/darwin/prompt.py` |
| A deck's job state (`/api/status`) | `status_body`, `current_version`, `version_list`, `image_ref_for` | `app/core/darwin/deck_state.py` |
| PNG -> PDF, tile composite | `pdf_from_pngs`, `composite_tile`, `knock_out_background` | `app/core/darwin/compose.py` |

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
10. **`/api/userinfo`**: no verification, Latin-1 `atob`, two 401 bodies (`detail` reproduces the
    JavaScript exception text), and Darwin set no content-type on errors, so the Fetch default
    `text/plain;charset=UTF-8` went out (the contract records that value; `app/api/routes/identity.py`
    sends it).

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
- **Deck fixtures** for the generation routes are in `tests/fakes/darwin_decks.py`: `generated_deck`
  (through `/api/generate` and its job), `empty_deck` (no job state yet), `put_version`, `tile_brand`,
  `fill_image_cap`, and `memo(env)` to hand an id from a probe's `run` to its `check`.
- **How the shape check reads the contract's prose:** a key whose description says it may be left out
  ("omitted", "present only") is optional, like `key?`; a `<placeholder>` key (`"<slideNumber as
  string key>"`) is a map whose every value has that shape; a literal with `{placeholders}`
  (`'Generated {doneCount} of {totalSlides} slides…'`) and a documented header with `<placeholders>`
  (`attachment; filename="slide_<slide>.pdf"`) are templates.
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
- **The storyline job result carries each slide's `prompt` again**, assembled by the port of
  `prompt.ts` (`DarwinSlidePrompter`, the runtime's default `prompter`), byte-for-byte Darwin's.
- If the brand kit cannot be read while a storyline's prompts are built (a storage outage), the
  prompts use the default kit; Darwin failed the job (here a failed job would be re-run and the
  storyline call paid again).
- **The `-background` functions are internal jobs that run as their owner** (D31, closes C12):
  `darwin.generate` (generate, retry), `darwin.refine`, `darwin.quick_generate`. Darwin's ran
  unauthenticated with a `userId` from the body. They run once (`max_attempts=1`): no paid re-run.
- **quick-generate is under the global image cap** (plan §6.5, C12): Darwin's background skipped
  `reserveImageSlot`. No slot left fails the job with "Free capacity reached, try again tomorrow.";
  the first failure cancels the images still being drawn (Darwin let them run and pay).
- **Every generated or refined image is in the cost ledger** (kind `image`, Darwin's estimates, the
  image model in `model`), and quick-generate's storyline call too; Darwin's rows were $0 (C10) and
  quick-generate recorded none. A failed ledger write no longer turns a rendered slide into an error.
- **Versions are append-only.** A retried slide gains a version and points at it; Darwin overwrote
  v1's image in place and bumped a cache-bust counter, so a retried *done* slide got an `&v=` URL
  that `/api/image` could not find (a Darwin bug, fixed by this). An errored slide keeps the versions
  it had; Darwin's job state dropped them until the next success.
- **Refine on a slide number the deck does not have** answers 202 as before but adds no phantom
  slide (Darwin added one to the job state, and `/api/status` counted it); the job fails quietly.
- **A refined slide keeps its master kind** (Darwin dropped it, so a second refine of a title or
  divider slide fell back to the Layout master). Like Darwin, it is no longer a workzone tile.
- **Refine's attachment** is stored as a blob for the job and deleted after; only the first image was
  ever used, as before.
- **Error texts without internals:** an image-provider failure still shows the provider's message
  (a content-policy refusal reads as the slide's error, as in Darwin); any other failure reads "Image
  generation failed", "Refinement failed" or "Generation failed" where Darwin showed the exception's
  text. A quick job whose storyline fails validation shows Darwin's storyline text ("Claude returned
  an incomplete storyline — ...") instead of the raw zod message.
- **Darwin's per-user legacy master** (`brand-assets/<userId>/master.png`, the pre-multi-brand
  fallback) is not read: the port keeps assets under a brand, so the migration (D29) must move it.
- **"No job state yet"** (`/api/status`'s `Starting…` body, revert's and pdf-deck's 404) is a deck with
  no slide rows; `/api/generate` writes them before it answers, as Darwin wrote the job state.
- **PDFs** are written by a small Flate writer (lossless, page size = picture size, as pdf-lib); the
  tile composite and the layout wireframe are drawn with Pillow (pixel-level differences from sharp
  and an SVG raster are expected; the shapes, colours and labels are Darwin's).
- **`/api/decks?id=`** returns the deck and slide rows built from the port's records with Darwin's
  column names (`owner`, `variant_a_status`, `variant_a_blob_key` = the current version's picture
  ref, `variant_b_*` null, ...) and a stable synthetic slide `id`. `inputs` is `{}` where Darwin
  stored `null` (no inputs posted).
- **`/api/generate`** truncates a title over 500 characters and drops a `creationMethod` over 32
  (Darwin's fire-and-forget update failed silently on them; the response is the same).
- **Model and limits** of the storyline and intake: see architecture.md, "Behaviour changes against
  Darwin today" in the storyline section.

Brand, admin, analytics and identity routes (feature/darwin-brands):

- **The brand `-background` functions are internal jobs** (`darwin.brand_pptx`, `darwin.brand_guidelines`,
  `darwin.brand_preview`) that run as the submitting user (C12 closed for them): the brand ACL is
  re-checked through the port when the job runs. A job of another user is 403 "Not your job", an
  unknown one pending, as before.
- **brand-pptx calls no external brand-extract service.** The extractor runs in-process
  (`app/core/brand/extract.py`), layout previews come from PptxRender through the `Renderer` port.
  Failures read differently: a deck the extractor cannot open is `status: error` with the
  extractor's own text ("The file could not be opened as a PowerPoint deck."), not
  "SlideForge brand-extract failed (4xx): ...". Rendering stays best-effort.
- **Logos are auto-extracted only when the extractor labels an image `logo` / `logo_mark`.** That
  label comes from the role-annotation model pass, which is off; the old upstream service labelled
  images itself. A logo can still be uploaded through `/api/brand-asset`.
- **brand-asset's pre-multi-brand fallback is gone.** Darwin served a personal brand's missing
  `logo` / `master` from a per-user legacy blob; those blobs move into the brand's own assets with
  the data migration (D29), so a missing asset is 404 "No asset uploaded".
- **POST /api/brands has the 20-personal-brand cap** of the store (0011's RLS cap, which Darwin's
  service-role API path bypassed): the 21st is 409 "Brand limit reached (20 personal brands)". New
  status and text (D33).
- **Deleting a brand deletes its assets** (the port cascades); Darwin left the blobs behind.
  `DELETE /api/admin-org-brands?brandId=` likewise.
- **The brand preview**: no layout wireframe for brands with extracted layouts but no master (Darwin
  synthesised one with sharp; the preview is then prompt-only); the prompt is `assembleSlidePrompt`
  for the sample slide, through the generation routes' prompt module (`app/core/darwin/prompt.py`);
  the cache is per brand (`preview-<hash>`), not per requesting user; a brand that is no longer
  editable when the job runs fails the job ("Brand not found") instead of falling back to the user's
  legacy kit; and the image is ledgered (`kind` image, C10).
- **The guidelines extraction is ledgered** (`kind` brand_guidelines) and uses a structured-output
  schema instead of a forced tool call (same fields and limits). Its per-user cap is wired and OFF
  (`BRAND_EXTRACT_CAP_ENABLED`); on, it is a new 429 "Daily guidelines extraction limit reached, try
  again tomorrow." checked after the PDF and brand checks.
- **admin-metrics shows real costs** (C10 is fixed in the ledger), so `totalCostUsd` and
  `dailyActivity[].costUsd` are no longer always 0.
- **analytics-event**: a `page_exited` event without a session id or path, or a value the store
  refuses (a negative duration, a 0-100 scroll depth out of range), is dropped (logged), where
  Postgres stored an empty string or failed the insert silently. The answer is unchanged.
- **userinfo's error content-type** is sent explicitly (`text/plain;charset=UTF-8`, what Netlify
  sent by default), and the `detail` text is this service's reproduction of Node's messages for
  the common failures (no '.', bad base64, bad JSON, a null payload); other JSON parse failures say
  `SyntaxError: Unexpected token ...` with Python's position.
