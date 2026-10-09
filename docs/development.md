# Development

## Which interpreter

**Python 3.11.** CI runs 3.11 and so will Azure (`PYTHON:3.11`), so a pass on any other
version is weaker evidence: timing and library behaviour differ between versions. Keep one
virtual environment per checkout, built the way CI builds it:

```bash
py -3.11 -m venv .venv                                   # Windows
python3.11 -m venv .venv                                 # Linux / macOS
.venv/Scripts/python.exe -m pip install -r requirements.txt -r requirements-dev.txt
```

Then install the export engine's browser once per machine (Playwright's bundled Chromium, at the
version `requirements.txt` pins; CI does the same):

```bash
.venv/Scripts/python.exe -m playwright install chromium          # add --with-deps on Linux
```

Run every tool through that interpreter (`.venv/Scripts/python.exe -m pytest`, and so on).
`scripts/gate.py` prints the interpreter and key package versions first, and warns when it is
not 3.11.

## Running it locally

```bash
cp .env.example .env            # placeholders; never put a real secret in .env.example
.venv/Scripts/python.exe -m uvicorn app.main:app --reload
```

- `GET http://127.0.0.1:8000/healthz` answers 200 straight away.
- `GET /readyz` needs Redis at `REDIS_URL`. Any local Redis 6+ works (Docker:
  `docker run -p 6379:6379 redis:7-alpine`); for an older build set `REDIS_PROTOCOL=2`.
- `GET /openapi.json` is the API spec, generated from the code.

Settings come from environment variables or `.env` (see `app/config/settings.py`). Startup logs
every problem `check_config()` finds; `ENVIRONMENT=development` keeps the production-only rules
off.

## Tests

```bash
.venv/Scripts/python.exe -m pytest tests -q
```

- `tests/` mirrors `app/`: the tests for `app/core/errors.py` are in `tests/core/test_errors.py`.
- Repo-wide contracts sit at the top of `tests/`: `test_route_controls.py`,
  `test_docs_links.py`, `test_ci_workflow.py`, `test_main.py`. `tests/scripts/` covers `scripts/`.
- No test needs Redis, a network or a `.env`: `tests/conftest.py` builds `Settings` from explicit
  values and uses fakeredis behind the real `RedisClient`.
- Async tests carry `@pytest.mark.asyncio` (strict mode, set in `pyproject.toml`).

### The engine tests (`tests/engine/`)

- They drive a real headless Chromium (install it as above) and take a few minutes; run one area
  with `pytest tests/engine/emit -q`.
- **Every fixture is synthetic.** The torture families and test masters are in
  `tests/engine/fixtures/`; the end-to-end sample project is generated per session by
  `tests/engine/sample_project.py`. Never add a client master, deck or slide (risk R6); build what
  a test needs with python-pptx or Pillow in the test, or as a small synthetic HTML file.
- Tests that need **PptxRender** carry a marker and are skipped, with the reason (`pytest -rs`),
  when it is not configured: `renderer` needs `/render-layouts` (`SLIDE_ENGINE_RENDERER_URL`);
  `renderer_full` also needs `/render` and `/verify`, which only a local PptxRender build serves
  (list them in `SLIDE_ENGINE_RENDERER_ENDPOINTS`); `validator` needs the OOXML validator
  (`SLIDE_ENGINE_VALIDATE_PY`). Nothing else may skip: a missing fixture fails.
- The engine takes its workspace and renderer as arguments; in tests, `tests/engine/helpers.py`
  fills in a per-test workspace and the configured renderer, and `tests/fakes/renderer.py` has a
  `FakeRenderer` and a fake of the PptxRender HTTP service.
- Measurements were calibrated on Windows; CI runs Linux Chromium with Carlito/Liberation fonts.
  A test that needs one particular installed face skips when that face is absent, with the reason.

Before pushing, run the whole gate: `.venv/Scripts/python.exe scripts/gate.py` (see
[scripts/README.md](../scripts/README.md)).

## Adding a route

1. Put it in the router for its area, `app/api/routes/<area>.py`, one file per area. Give the
   `APIRouter(...)` its own `prefix` and `tags` (the first tag is the feature name used in
   metrics), and add the router to `ROUTERS` in `app/main.py`. `tests/test_main.py` fails if a
   router relies on an include-time prefix.
2. Choose its error format. One of Darwin's existing `/api/*` routes uses
   `legacy_router(...)` from `app/core/errors.py`, so its errors stay `{"error": "..."}` (decision
   D32). Anything new uses a plain `APIRouter` and answers Problem Details. Raise
   `ApiError(status, message)` for deliberate errors; it renders in the route's format.
3. New routes reject unknown fields (`model_config = ConfigDict(extra="forbid")`) and bound every
   input. Existing Darwin routes keep today's lenient behaviour; see D32.
4. Add its row to `RULES` in `tests/test_route_controls.py`, then regenerate the table:
   `python tests/test_route_controls.py --write`.
5. Write its tests, including the negative cases, next to the others in `tests/api/routes/`.

## Adding a setting

Engine settings live in `EngineSettings` in `app/config/engine.py` (environment prefix
`SLIDE_ENGINE_`); the steps are the same, with the rule in `check_engine_config()` (or
`check_engine_production()`) and its test in `tests/config/test_engine.py`.


1. Add the field to `Settings` in `app/config/settings.py`, with a comment saying what it is for.
2. Add it to `.env.example` with a placeholder (`tests/config/test_env_example.py` fails
   otherwise).
3. If a value can be invalid, or production requires it, add the rule to `check_config()` and a
   test in `tests/config/test_settings.py`. If it is a secret, add it to `secret_values()` so the
   log scrubber removes it.

## Dependencies

Runtime packages go in `requirements.txt`, test and lint tools in `requirements-dev.txt`. Every
entry is version-bounded (the policy is at the top of `requirements.txt`). After changing one,
reinstall, then run the gate: it fails if the installed versions do not satisfy
`requirements.txt`, and runs `pip-audit`.
