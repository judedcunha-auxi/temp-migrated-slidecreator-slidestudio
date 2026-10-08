"""
SlideForge service: the entry point.

    python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

create_app() builds the FastAPI app; `app` is the instance the server runs. Order
matters and is fixed here:

1. logging, then telemetry (so the Azure log handler attaches next to stdout);
2. the startup config check, logged as errors (/readyz acts on it in production);
3. the error handlers: legacy `{"error"}` for Darwin routes, Problem Details for
   everything else (decision D32, app/core/errors.py);
4. middleware: CORS for the static frontend origin, then the request id
   (outermost, so every response carries it);
5. routers, one per area of the API.

Tests call create_app() with their own Settings and a fakeredis-backed store.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.logging_config import configure_logging, register_secret_values

configure_logging()

from app.api.routes import health  # noqa: E402 - logging must be configured first
from app.config.settings import Settings, check_config, is_production, secret_values  # noqa: E402
from app.config.settings import settings as default_settings  # noqa: E402
from app.core import telemetry  # noqa: E402
from app.core.errors import install_error_handlers  # noqa: E402
from app.core.redis_client import RedisClient, RedisStore  # noqa: E402
from app.core.request_id import RequestIdMiddleware  # noqa: E402

_log = logging.getLogger(__name__)

SERVICE_NAME = "slideforge-service"

# Every router the app serves, one per area of the API. The route-controls test
# reads this tuple, so a router that is not listed here is not served either.
# Convention (tests/test_main.py holds it): a router sets its own prefix and tags
# in its APIRouter(...) constructor, and is included with no prefix or tags, so a
# route's path and feature tag are the same on the route object and on the wire.
ROUTERS = (health.router,)


def create_app(settings: Settings | None = None, redis: RedisStore | None = None) -> FastAPI:
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
        _log.info("startup: environment=%s production=%s config_problems=%d",
                  s.environment or "-", is_production(s), len(problems))
        try:
            yield
        finally:
            if owns_redis and app.state.redis is not None:
                await app.state.redis.close()
                app.state.redis = None

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
    app.state.config_problems = problems

    install_error_handlers(app)

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
