"""
SlideForge service: the entry point.

    python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

create_app() builds the FastAPI app; `app` is the instance the server runs. Order
matters and is fixed here:

1. logging, then telemetry (so the Azure log handler attaches next to stdout);
2. the startup config check, logged as errors (/readyz acts on it in production);
3. the error handlers: legacy `{"error"}` for Darwin routes, Problem Details for
   everything else (decision D32, app/core/errors.py), plus the Darwin quirk 500s
   (app/api/legacy.py);
4. middleware: CORS for the static frontend origin, then the request id
   (outermost, so every response carries it);
5. routers, one per area of the API.

The lifespan opens Redis and the storage backend (unless a test passed its own), builds
the Darwin runtime (job queue, models, image generator; app/core/darwin/runtime.py) and,
when WORKER_CONCURRENCY > 0, starts that many job worker loops in this process.

Tests call create_app() with their own Settings, a fakeredis-backed store and,
when they need one, their own storage (tests/fakes/general_service.py).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.logging_config import configure_logging, register_secret_values

configure_logging()

from app.api.legacy import install_legacy_handlers  # noqa: E402 - logging must be configured first
from app.api.routes import decks, exports, generate, health, media, quick, refine, storyline  # noqa: E402
from app.config.settings import Settings, check_config, is_production, secret_values  # noqa: E402
from app.config.settings import settings as default_settings  # noqa: E402
from app.core import telemetry  # noqa: E402
from app.core.errors import install_error_handlers  # noqa: E402
from app.core.redis_client import RedisClient, RedisStore  # noqa: E402
from app.core.request_id import RequestIdMiddleware  # noqa: E402
from app.core.storage.factory import build_storage  # noqa: E402
from app.core.storage.ports import Storage  # noqa: E402

_log = logging.getLogger(__name__)

SERVICE_NAME = "slideforge-service"

# Every router the app serves, one per area of the API. The route-controls test
# reads this tuple, so a router that is not listed here is not served either.
# Convention (tests/test_main.py holds it): a router sets its own prefix and tags
# in its APIRouter(...) constructor, and is included with no prefix or tags, so a
# route's path and feature tag are the same on the route object and on the wire.
ROUTERS = (
    health.router, storyline.router, exports.router,
    # the generation batch (Phase 7a): decks, generate/retry/status, refine/revert/slide-transcript, media, quick
    decks.router, generate.router, refine.router, media.router, quick.router,
)


def _build_storage(s: Settings) -> Storage | None:
    """The configured storage, or None (logged) when it cannot be built: /readyz
    then answers 503 rather than the process failing to start."""
    try:
        return build_storage(s)
    except Exception as exc:  # noqa: BLE001 - e.g. the General service stub (D5)
        _log.error("storage: STORAGE_BACKEND=%s could not be built: %s", s.storage_backend, exc)
        return None


def create_app(
    settings: Settings | None = None, redis: RedisStore | None = None, storage: Storage | None = None
) -> FastAPI:
    s = settings or default_settings
    register_secret_values(secret_values(s))
    telemetry.configure_telemetry()

    problems = check_config(s)
    for problem in problems:
        _log.error("config: %s", problem)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_redis = app.state.redis is None
        if owns_redis:
            app.state.redis = RedisClient.from_settings(s)
        owns_storage = app.state.storage is None
        if owns_storage:
            app.state.storage = _build_storage(s)
        if app.state.darwin is None and app.state.storage is not None and app.state.redis is not None:
            from app.core.darwin.runtime import build_runtime

            app.state.darwin = build_runtime(s, app.state.redis, app.state.storage)
        if app.state.darwin is not None:
            app.state.darwin.start_workers(s.worker_concurrency)
        _log.info("startup: environment=%s production=%s config_problems=%d",
                  s.environment or "-", is_production(s), len(problems))
        try:
            yield
        finally:
            if app.state.darwin is not None:
                await app.state.darwin.stop_workers()
            if owns_redis and app.state.redis is not None:
                await app.state.redis.close()
                app.state.redis = None
            if owns_storage:
                app.state.storage = None

    app = FastAPI(
        title="auxi SlideForge service",
        version="0.1.0",
        lifespan=lifespan,
        # No interactive docs pages: the service is reached only through the
        # gateway, and the spec itself is enough for clients and reviewers.
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.settings = s
    app.state.redis = redis
    app.state.storage = storage
    app.state.config_problems = problems
    # The Darwin runtime (queue, models, workers) and the token verifier: built in the
    # lifespan / on first use, or set by a test before the first request.
    app.state.darwin = None
    app.state.token_verifier = None

    install_error_handlers(app)
    install_legacy_handlers(app)

    if s.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=s.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", s.request_id_header],
            expose_headers=[s.request_id_header, "Retry-After"],
            max_age=600,
        )
    # Added last, so it is the outermost of our middleware.
    app.add_middleware(RequestIdMiddleware, header_name=s.request_id_header)

    for router in ROUTERS:
        app.include_router(router)

    telemetry.instrument_app(app)
    return app


app = create_app()
