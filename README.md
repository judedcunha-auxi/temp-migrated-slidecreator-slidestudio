# auxi SlideForge service

The Python service that designs slides, exports them to PowerPoint, and serves Darwin's
`/api/*` routes. It replaces three things: Slide Studio's `/v1` backend, the retired OCR
backend, and Darwin's Netlify functions. The migration plan lives in the Slide-Creator repo
(`docs/slideforge-migration/DETAILED-PLAN.md`).

**Status: Phase 1, the skeleton.** The service boots, reports its health and build, logs and
traces requests, and has both error formats and the CI gate in place. It serves no feature
routes yet: the engine arrives in Phase 2, the AI features in Phase 3, and Darwin's routes in
Phase 7a.

## Run it

Python 3.11, the version CI and Azure use.

```bash
py -3.11 -m venv .venv                        # Windows; python3.11 -m venv .venv elsewhere
.venv/Scripts/python.exe -m pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env                          # placeholders only; adjust as needed
.venv/Scripts/python.exe -m uvicorn app.main:app --reload
curl http://127.0.0.1:8000/healthz
```

`/readyz` needs a Redis at `REDIS_URL` (default `redis://127.0.0.1:6379/0`); without one it
answers 503, which is correct. The tests need no Redis.

Before pushing, run the same checks CI runs:

```bash
.venv/Scripts/python.exe scripts/gate.py
```

## Documentation

| Read | When |
|---|---|
| [docs/README.md](docs/README.md) | How the docs are organized. |
| [docs/how-it-works.md](docs/how-it-works.md) | **Start here.** A plain-English tour. |
| [docs/development.md](docs/development.md) | Running it locally, the interpreter, adding a route. |
| [docs/deployment.md](docs/deployment.md) | Deploying to staging and production, and verifying a deploy. |
| [docs/route-controls.md](docs/route-controls.md) | Every route and what protects it (generated, test-enforced). |
| [scripts/README.md](scripts/README.md) | The gate, packaging and deploy-verification scripts. |

## Layout

```
app/
  main.py              entry point: create_app() and the app the server runs
  api/routes/          one file per area of the API (health.py today)
  config/settings.py   settings and check_config(), the startup config check
  core/                logging, telemetry, errors, request ids, Redis
tests/                 mirrors app/, plus the repo-wide contract tests
docs/  scripts/  .github/workflows/
```
