# Architecture

How the service is put together. Each area has its own section, written by whoever builds it.
Add a section rather than editing another area's. The plain-English tour is
[how-it-works.md](how-it-works.md); this page is the design.

## Layers

```
app/
  main.py          the FastAPI app: middleware, error handlers, the ROUTERS registry
  api/routes/      one router per area of the API (health today)
  config/          settings.py (service settings, check_config), engine.py (engine settings)
  core/            logging, telemetry, errors, request ids, Redis, storage/ (the General
                   service port), jobs/ (the Redis queue), the browser pool
  engine/          the export engine: slide HTML -> an editable .pptx
```

The rule between them: `api` calls `core` and `engine`; `engine` calls `config` and
`core.browser_pool` only, never `api`. The engine takes everything it works on (paths, a
workspace, a renderer) as arguments, so it can run in a request, a job worker or a test alike.

## The export engine

Ported from Slide Studio's `engine/` in Phase 2 (migration plan §4.2). It turns slide HTML into
native PowerPoint objects on the customer's own master: deterministic, no model calls, a few
seconds a deck.

### The pipeline

```
slide HTML ──► extract ──► IR ──► classify ──► IR ──► emit ──► .pptx
               (Chromium)        (charts,             (python-pptx,
                                 placeholders)         on the master)
                                                         │
                                       verify ◄──────────┘ (CI / staging: lint, fit,
                                                             coverage, the pixel gate)
```

1. **Extract** (`engine/extract/`). The slide is loaded in headless Chromium at the master's
   canvas size (one CSS px = 1/96 in = 9525 EMU). `page.js` walks the DOM and reports every box,
   line of text, colour, image and SVG as measured by the browser, so line breaks are the
   browser's, not a guess. The theme fonts are forced first and checked. `svg.py` expands each
   `<svg>` into shapes; what has no native equivalent becomes an isolated raster, with a reason.
2. **The IR** (`engine/ir.py`) is the seam: a flat, paint-ordered list of elements with every
   value resolved. Extract, classify, emit and verify share nothing else.
3. **Classify** (`engine/classify/`). Hand-drawn SVG bar, line and doughnut figures that state
   their numbers become native chart elements; text that sits in a layout placeholder is mapped
   to it.
4. **Emit** (`engine/emit/`). `pptx.py` opens the master, strips its slides, adds one slide per
   IR on that IR's layout and walks the elements back to front: `shapes.py` (geometry, images,
   tables), `text.py` (measured lines as editable, reflowing text), `charts.py` (native charts with
   embedded workbooks), `template.py` (placeholders, and the promise that the master's own parts
   come out byte-for-byte untouched).
5. **Verify** (`engine/verify/`): `lint` (authoring-contract checks on the HTML, cheap enough for
   every save), `fit` (does each text box still fit in PowerPoint's metrics), `coverage` (what
   became native vs raster), `text_layout`/`text_deck` (overlaps and side-by-side text), and for
   CI/staging `gate` + `suite` + `torture` (the pixel gate against PptxRender's render).

`engine/pipeline.py` runs 1-4 for a project directory (`export_deck`); `engine/importer.py`
reads a master into the **manifest** (`engine/manifest.py`, schema v2): canvas, themes, layouts,
placeholders with their resolved styles, and a background image per layout.

### Workspaces, not global folders

Every export names its own **workspace** (`ExportOptions.workspace`, default `<out_dir>/work`).
Derived images (isolated rasters, rasterised SVG images) are written under
`<workspace>/derived/<slide id>/` (`engine/extract/__init__.py: derived_dir`). Two exports running
at once never share or clean each other's files, and nothing is written to a process-wide folder.
In the service the workspace is the job's scratch folder (Phase 4 materialises it from the General
service). What remains process-wide is read-only caches of machine facts: the installed-font index
and the font scan.

### The browser pool

Playwright's sync API is bound to the thread that started it, so a browser is per thread.
`app/core/browser_pool.py` owns that: `thread_browser()` is the calling thread's Chromium, launched
on first use and reused for every slide; `BrowserPool(size)` runs engine work on N threads, each
with its own browser, so N exports measure in parallel while the event loop stays free
(`SLIDE_ENGINE_BROWSER_POOL_SIZE`, one Chromium each, sized to memory). Launches use the bundled
Chromium with `--no-sandbox --disable-dev-shm-usage` (container-safe).

### The renderer port (PptxRender)

PptxRender, the .NET renderer, stays an external HTTP service (decision D3b). The engine talks to
it only through the `Renderer` protocol in `engine/renderer.py`, passed in by the caller:

- `PptxRenderClient` is the HTTP client, built by `PptxRenderClient.from_settings()` from
  `SLIDE_ENGINE_RENDERER_URL` (or `None` when unset).
- `BlankLayoutsRenderer` renders white layout backgrounds offline.
- `tests/fakes/renderer.py` holds the test doubles.

The deployed PptxRender serves only `POST /render-layouts` (form fields `width`, `masters`,
`placeholderText`, `noPlaceholderText`); it returns the layout PNGs and no index. So the layout
PNGs are joined to the package **by filename** (`layout-NN-<slug>.png`, NN = PptxRender's
enumeration index, reconstructed from the package by `layout_identities` and keyed by
`partName`); an `index.json`, when a build returns one, is cross-checked too. `/render` and
`/verify`, which the pixel gate needs, exist only in local PptxRender builds: they are called only
when `SLIDE_ENGINE_RENDERER_ENDPOINTS` lists them, and the tests that need them skip otherwise.

### Determinism

The same input gives a byte-identical `.pptx` (`tests/engine/emit/test_determinism.py`).
`emit/pptx.py: finalize` rewrites the saved package: every zip entry dated 1980-01-01 and written
in a fixed order, `dcterms:created/modified` pinned (in the deck and in every embedded chart
workbook), PNG metadata chunks stripped from our images, and the master's own bytes restored for
its masters, layouts and theme. New slide parts get the first free `slideN.xml` names
(`_rename_new_slides`), so the names depend only on the package.

### Masters that keep slides alive: the duplicate-parts fix

`strip_slides` empties the master before the new slides go in. Two kinds of master used to make
the saved zip contain two entries with the same name (and PowerPoint offer a repair):

- a **custom show** (`p:custShowLst`) refers to slides by the same relationship id as the slide
  list, so python-pptx's `drop_rel` (which keeps a relationship referenced twice) kept the old
  slide part, and the new slide took its name;
- a **layout that links to a slide** (a "back to agenda" button) keeps the old slide part
  reachable even once the slide list is empty.

The fix: the section list and custom shows go first, each slide relationship is popped
unconditionally, and after the new slides are added each is renamed to the first
`/ppt/slides/slideN.xml` no surviving part uses. Regression tests:
`tests/engine/emit/test_strip_slides.py` (synthetic masters).

### Fonts in the container

The Linux container (and CI) installs the Microsoft core fonts (`ttf-mscorefonts-installer`:
Arial, Times New Roman, Georgia, Verdana and the rest of that set), as Slide Studio's image does, so
those faces are measured as themselves. Calibri, Calibri Light, Cambria and Segoe UI ship only with
Windows or Office and cannot be installed there, so it also installs metric-compatible stand-ins
(Carlito for Calibri, Liberation and DejaVu as fallbacks), and the engine measures a family that is
not installed in its stand-in (`emit/text.py: METRIC_ALIASES`, used by the emitter's metrics and the
fit predictor). The file still names the family the slide asked for, so PowerPoint on the client's
machine draws the real face at the same widths. Engine tests that measure a Windows-only face
itself skip on Linux with that reason (`tests/engine/helpers.py: require_faces`). See
[deployment.md](deployment.md) for the packages the image needs.

### Licence-gated modules

`engine/emit/charts.py` and `engine/emit/draw.py` are derived from StageFlow, third-party code
whose terms are not granted (decision D3). They are ported so the engine works, but must not ship
to production until D3 is resolved. See [licensing.md](licensing.md).

### Python 3.11

Slide Studio targeted 3.12. The engine has no 3.12-only syntax or APIs: every module under `app/`
and `tests/` parses with the 3.11 grammar and the suite runs on 3.11. One 3.12-only construct is
known on the Phase 3 side: Slide Studio's `server/design_refs/design_lint.py` (around line 1347)
has a backslash inside an f-string expression (PEP 701); move the `re.sub` into a helper when that
module is ported.

### Port map (Slide Studio `engine/` -> `app/engine/`)

K = kept, R = rewritten, D = dropped (migration plan §4.2).

| Slide Studio | Here | | Notes |
|---|---|---|---|
| `__init__.py` | `app/engine/__init__.py` | R | Path B gone. |
| `__main__.py` (CLI) | none | D | The CLI was a dev tool (export, torture, reference). The torture judging and reference writing stay in `verify/torture.py`; there is no command line. |
| `config.py` | `app/config/engine.py` | R | Typed `EngineSettings` (`SLIDE_ENGINE_*`), wired into `check_config()` and `.env.example`; per-machine paths dropped. |
| `ir.py`, `reports.py`, `chart_model.py`, `charts_spec.py` | same | K | `path: "A"/"B"` fields dropped from the reports. |
| `manifest.py` | same | K | `from_legacy` dropped (D14). |
| `importer.py` | same | K/R | `IMPORT_INSTRUCTIONS` (Path B) dropped; takes a `Renderer`. |
| `pipeline.py` | same | K/R | Reads a project directory the caller names (no projects folder); per-export workspace. |
| `renderer.py` | same | R | `Renderer` protocol + HTTP client + offline renderer; `info` and the global stand-in dropped; filename join is the real path, `index.json` optional. |
| `classify/*` | same | K | |
| `extract/*` (incl. `page.js`) | same | K/R | Derived dir per workspace; browser from `app/core/browser_pool.py`. |
| `emit/pptx.py`, `template.py`, `text.py`, `shapes.py` | same | K + fixes | Duplicate-parts fix; Calibri -> Carlito metrics. |
| `emit/charts.py`, `emit/draw.py` | same | K (licence-gated) | D3. |
| `verify/lint.py`, `text_layout.py`, `fit.py`, `text_deck.py`, `coverage.py` | same | K | |
| `verify/gate.py`, `suite.py`, `torture.py`, `torture_expect.py` | same | K (CI/staging) | Take a `Renderer`; need `/render` + `/verify`. |
| `fixtures/__init__.py` | `app/engine/verify/fixtures.py` | K | Resolves torture/sample fixtures; tests and CI only. |
| `web/public/chart-preview.js` | `app/engine/chart-preview.js` | K | The browser twin of `chart_model.py` (gate references, previews). |
| `verify/compare.py`, `tools/*`, `fixtures/fidelity/*`, `vendor/stageflow/*` | none | D | Dev-only tools and the reference-only StageFlow copy. |
| `fixtures/eyp/`, `fixtures/deck10/`, `tests/overlap/*` | none | D | Client material (R6). Replaced by the synthetic sample project. |
| `tests/*` | `tests/engine/**` | K/R | Re-baselined on synthetic fixtures. |

## Storage and jobs (Phase 4)

### The rule

The service keeps no lasting data of its own. Repo standards: "No direct connection to SQL
Server or blob storage. All data goes through the General service."

- **Redis** holds only what can be lost and rebuilt: the job queue, the in-flight limiter and
  the counters (D6).
- **Decks, brands, files and job records** go through one port to the General service.

The General service does not exist yet (D5). So the port is defined and tested now, and its
real adapter is written when the .NET repo arrives.
[general-service-requirements.md](general-service-requirements.md) is the hand-over spec.

### The storage port

```
app/core/storage/
  ports.py            the protocols (one per area of the plan's §2.2) + the errors
  models.py           the entities, from the Supabase schema + the Netlify Blob shapes
  reference.py        the port semantics, written once, over a RecordBackend
  memory.py           STORAGE_BACKEND=fake   reference over dicts
  local_fs.py         STORAGE_BACKEND=local  reference over files under SCRATCH_ROOT
  general_service.py  STORAGE_BACKEND=general  STUB + the endpoint requirements as data
  factory.py          settings -> Storage
  paths.py            safe_join: the only way a path is built
  workspace.py        a scratch directory per job, backed by the port
```

**Ports.** `Storage` bundles thirteen ports:

- users;
- orgs;
- brands;
- masters;
- decks;
- slides;
- blobs;
- jobs;
- exports;
- usage;
- transcripts;
- analytics;
- health.

Every operation takes a `CallerContext` (identity subject, request id, and whether it is a
system call). An adapter passes these on to the General service for authorisation, audit and
log correlation.

**Errors.** The errors belong to the port: `NotFound`, `Forbidden`, `Conflict`, `InvalidInput`
and `Unavailable`. A route maps them to its own contract. For example, Darwin answers 403 "Not
your deck" for a `NotFound` deck.

**One set of semantics, three backends.** The in-memory store and the local adapter are the
same `ReferenceStorage` over two `RecordBackend`s. So the following behave identically in
both:

- ownership;
- the org-brand ACL;
- idempotency;
- append-only versions.

The contract suite (`tests/core/storage/contract/`) runs every test once per adapter. The
General service adapter joins it when it exists.

**The test fake.** `tests/fakes/general_service.py` is the in-memory store plus test controls:

- a settable clock;
- one-line users and admins;
- a call log, to assert that the request id reaches the General service;
- failure injection for the readiness ping.

**Selection.** The backend is chosen by `STORAGE_BACKEND`:

- `fake` is the default and is for development.
- `local` survives restarts.
- `general` is the only backend production accepts.

`check_config` refuses `fake` and `local` in production. `general` is still a stub, so
`check_config` reports it, and `/readyz` answers 503 with `storage: unavailable`. That is
deliberate: an instance that cannot store data must not take traffic.

**Readiness.** `/readyz` pings the storage backend:

- `ok`: the backend answered.
- `skipped`: the fake has nothing to reach. This is neutral and does not affect readiness.
- `unreachable` or `unavailable`: 503.

The body never says which host or why. That stays in the logs.

### No writes outside the scratch root

The local adapter and the job workspaces write only under `SCRATCH_ROOT`. If it is unset, they
use `<system temp>/slideforge-scratch`. `paths.safe_join` builds every path and keeps every
write inside that root:

- Each part must be one plain segment: no separators, no `..`, no drive letters, no NUL, no
  leading dot and no Windows device names.
- The joined path is resolved, and it must still be inside the root. This also catches a
  symlink or junction planted inside the root.
- Record keys that are not plain segments are stored under their SHA-256, never as raw text.
- Temporary files are created next to their target, never in the system temp directory.

`tests/core/storage/test_local_fs.py` tests this. It drives every port and a workspace with
hostile names, then checks that every new file under the test directory is inside the root.

### Job workspaces

`job_workspace(blobs, ctx, root, job_id, inputs=...)` handles one job attempt in four steps:

1. Create `<root>/jobs/<job id>-<random>/`.
2. Fetch the named input blobs through the port, so the caller's ownership applies, into
   `in/`.
3. When the block ends cleanly, store every file under `out/` through the port. The results
   are in `ws.persisted`.
4. Remove the directory, however the block ends.

Two attempts of one job never share a directory.

### Jobs

```
app/core/jobs/
  queue.py     JobQueue: enqueue, lease, heartbeat, complete, fail
  limiter.py   InFlightLimiter: at most 3 expensive jobs per user
  worker.py    Worker: runs leased jobs with heartbeat and timeout
```

**Redis keys**, all under `sf:jobs:`:

- `ready` (ZSET): every unfinished job, scored by a Redis sequence. The oldest is tried first.
- `seq`: that sequence.
- `job:<id>` (HASH): type, owner subject, request id, attempts and the limiter slot.
- `lease:<id>`: the lease token, with a TTL.
- `inflight:<sha(subject)>` (ZSET): the limiter.

**Leases.** A worker takes a job with `SET lease:<id> <token> NX PX <lease>`. That one atomic
command is the lock. The TTL **is** the lease: heartbeats extend it.

A worker that is killed stops heartbeating. The key expires, and the job, which never left
`ready`, goes to the next worker that looks. No reaper is needed.

**Attempts.** Attempts are counted when a job is leased. Past `max_attempts`, the job is
finished as an error.

**Per-job timeout.** It is enforced twice:

- The worker cancels a handler that runs too long.
- A heartbeat past the deadline is refused, so a hung attempt loses its lease.

**Durable records.** Each record is written through the port at every step: queued, running
(with the attempt), progress, then done, error or queued again. The status routes read the
record, never Redis.

**Order of writes.** The durable record is written first, then Redis is cleaned up. If a
finished job comes back after a crash, `lease` sees the terminal record and drops it without
running it again.

**Idempotent enqueue.** A replayed `Idempotency-Key` returns the existing job. This happens
before an in-flight slot is taken, so a retry never gets a 429 for a job it already has, and a
429 never uses up the key. The same key with different input is a `Conflict`.

**The in-flight limit.** At most 3 expensive jobs per user (repo standards, "Rate limits"). The
limiter adds the job first, then counts, so two racing requests can both be refused but never
both admitted past the limit. Each slot carries its own expiry, so a slot whose job died frees
itself.

**The Phase 4 exit tests:**

- No writes outside the scratch root: `tests/core/storage/test_local_fs.py`.
- A killed worker's job is re-queued and finished by another worker:
  `tests/core/jobs/test_worker.py::test_a_killed_workers_job_is_requeued_and_completed_by_another_worker`.
- The contract suite is green for the fake and the local adapter.

### Not done yet

- **The real General service adapter.** It is blocked on D5.
- **A sweeper for stale `queued` records.** If the process dies between writing the durable
  record and adding the job to Redis, that record waits forever.
- **Wiring workers into the app process.** Starting worker loops in the lifespan, or as a
  separate entry point in the same image, comes when the first route enqueues a job (Phase 5
  and 7).
- **Two replicas on staging.** This Phase 4 exit item needs hosting (D2).
- **The local adapter is single-process.** It has one lock per process and lists by scanning
  directories. It is for development only.
