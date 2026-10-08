# Deployment

> **Hosting is not decided yet.** Where the service runs is decision **D2** in the migration
> plan: App Service for Containers with a staging slot (recommended), Container Apps, or a zip
> deploy that installs the browser at startup. A **container is the likely answer**, because the
> slide engine (Phase 2) needs Chromium, which a plain zip deploy cannot carry well. How secrets
> reach the app (**D18**, recommended: Key Vault references and a managed identity) and how GitHub
> signs in to Azure (**D19**, recommended: OIDC) are open too. Until all three are settled, the
> deploy jobs in CI are disabled stubs and nothing is deployed. This page says what is fixed
> already, and what the deploy will do.

## Branches and release flow

Two long-lived branches, `staging` and `production`, each always exactly what is deployed in its
slot. Nothing is deployed from any other branch.

1. Branch from `staging`.
2. Test locally (`scripts/gate.py`).
3. Merge into `staging`.
4. CI deploys staging.
5. Test on staging.
6. Merge `staging` into `production`.
7. CI deploys production, and the release is tagged `prod-YYYY-MM-DD-<slug>`.

A branch can move; a tag cannot. The previous production tag is the rollback.

## What CI builds

Every push and pull request runs the **Backend (pytest + ruff + mypy)** job: tests, lint, type
check, dependency audit, secret scan and security scan.

On a push to `staging` or `production` that passes, the **Deploy artifact (SHA-addressed)** job
runs `scripts/package_deploy.py` on a fresh checkout of that exact commit and uploads
`staging-deploy.zip` as `deploy-<sha>`, kept 30 days. The package holds `app/`,
`requirements.txt`, `startup.txt` and `build_info.json`, which records the commit
(`sha`, `branch`, `dirty`, `builtAt`). `/healthz` serves those fields, so the running commit is
always one request away.

To build the same package locally (it refuses an uncommitted tree unless you pass
`--allow-dirty`):

```bash
python scripts/package_deploy.py
```

## What the deploy will do (once D2, D18 and D19 are decided)

- **Deploy to staging** runs on a push to `staging`, after the artifact is built, and deploys
  that artifact (never a rebuild) to the staging slot.
- **Deploy to production** runs on a push to `production`, the same way.
- Each then runs **Verify deploy**: `python scripts/verify_deploy.py <target> --sha <commit>`.

The jobs exist in `.github/workflows/ci.yml` today, marked `TODO(D2/D18/D19)` and disabled with
`if: false`. Filling them in means: the Azure sign-in step (OIDC, which needs
`permissions: id-token: write` on those jobs), the deploy command for the chosen host, and the
`SLIDEFORGE_STAGING_URL` / `SLIDEFORGE_PRODUCTION_URL` repository variables. If D2 picks a
container, the Dockerfile copies `build_info.json` in, so `/healthz` keeps reporting the commit;
`startup.txt` then documents the container's command rather than an App Service setting.

The platform's health check must point at **`/readyz`**, not `/healthz`: readiness is what says
an instance can take traffic.

## Verifying a deploy

```bash
python scripts/verify_deploy.py staging              # host from SLIDEFORGE_STAGING_URL
python scripts/verify_deploy.py production --sha <commit>
```

It passes only when `/readyz` answers 200 with Redis and configuration ok, `/healthz` reports the
expected commit from a clean tree, and `/healthz` answers quickly. It exits non-zero otherwise,
so it can gate the deploy job.

To prove what is running, at any time:

```bash
python scripts/whats_deployed.py production --against production
```

## Configuration in each environment

Every variable is listed in [`.env.example`](../.env.example). In staging and production set
`ENVIRONMENT` explicitly; `check_config()` then requires `REDIS_URL` over TLS (`rediss://`),
`APPLICATIONINSIGHTS_CONNECTION_STRING`, and an https `CORS_ALLOWED_ORIGINS`, and `/readyz`
answers 503 until they are right. Secrets are never committed and, per D18, are expected to come
from Key Vault.
