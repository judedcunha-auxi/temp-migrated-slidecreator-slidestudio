# Architecture

The system design, for someone about to change it. [how-it-works.md](how-it-works.md) is the
plain-English version; this page names the modules and the rules between them.

## Layers

```
app/
  main.py          the FastAPI app: middleware, error handlers, the ROUTERS registry
  api/routes/      one router per area of the API (health today)
  config/          settings.py (service settings, check_config), engine.py (engine settings)
  core/            logging, telemetry, errors, request ids, Redis, the browser pool
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

The same input gives a byte-identical `.pptx`.
`emit/pptx.py: finalize` rewrites the saved package: every zip entry dated 1980-01-01 and written
in a fixed order, `dcterms:created/modified` pinned (in the deck and in every embedded chart
workbook), PNG metadata chunks stripped from our images, and the master's own bytes restored for
its masters, layouts and theme.

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
| `emit/pptx.py`, `template.py`, `text.py`, `shapes.py` | same | K + fixes | Fixes follow (plan: duplicate parts, RTL). |
| `emit/charts.py`, `emit/draw.py` | same | K (licence-gated) | D3. |
| `verify/lint.py`, `text_layout.py`, `fit.py`, `text_deck.py`, `coverage.py` | same | K | |
| `verify/gate.py`, `suite.py`, `torture.py`, `torture_expect.py` | same | K (CI/staging) | Take a `Renderer`; need `/render` + `/verify`. |
| `fixtures/__init__.py` | `app/engine/verify/fixtures.py` | K | Resolves torture/sample fixtures; tests and CI only. |
| `web/public/chart-preview.js` | `app/engine/chart-preview.js` | K | The browser twin of `chart_model.py` (gate references, previews). |
| `verify/compare.py`, `tools/*`, `fixtures/fidelity/*`, `vendor/stageflow/*` | none | D | Dev-only tools and the reference-only StageFlow copy. |
| `fixtures/eyp/`, `fixtures/deck10/`, `tests/overlap/*` | none | D | Client material (R6). Replaced by the synthetic sample project. |
| `tests/*` | `tests/engine/**` | K/R | Re-baselined on synthetic fixtures. |
