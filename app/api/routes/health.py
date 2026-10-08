"""
SlideForge service: api/routes/health. Liveness and readiness probes.

GET /healthz  Liveness. No dependency checks: a failure means the process is stuck
              and should be restarted, not that a backing service is down. Reports
              the commit that is running (sha, builtAt, dirty) from build_info.json,
              which scripts/package_deploy.py stamps into the deploy package, so
              "what is deployed?" is one request.
GET /readyz   Readiness. Redis answers a PING, and in production check_config()
              found no problems. 503 when not ready. Point the platform's health
              check here. (The General service joins this check in Phase 4.)

Both are open to unauthenticated callers, because probes send no credentials, and
neither reveals internal details: which host, which variable, which exception all
stay in the logs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config.settings import PROJECT_ROOT, is_production
from app.core.redis_client import PING_TIMEOUT_S, RedisStore

_log = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

BUILD_INFO_PATH = PROJECT_ROOT / "build_info.json"
_build_info_cache: dict[str, Any] | None = None


def load_build_info(path: Path | None = None) -> dict[str, Any]:
    """build_info.json as a dict; {} when absent (a git checkout has none) or unreadable."""
    try:
        raw = json.loads((path or BUILD_INFO_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _build_info() -> dict[str, Any]:
    # Read once: the file cannot change under a running process.
    global _build_info_cache
    if _build_info_cache is None:
        _build_info_cache = load_build_info()
    return _build_info_cache


def liveness_body(info: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"status": "ok"}
    if info.get("sha"):
        body["sha"] = str(info["sha"])
        if info.get("builtAt"):
            body["builtAt"] = str(info["builtAt"])
        # Always present alongside a sha: a sha from a dirty tree names the branch
        # point, not the deployed bytes, and must not LOOK clean.
        body["dirty"] = bool(info.get("dirty"))
    return body


async def readiness(redis: RedisStore | None, config_problems: list[str], production: bool) -> tuple[bool, dict[str, str]]:
    """(ready, public detail). The detail names only which check failed."""
    detail: dict[str, str] = {}
    ready = True
    try:
        if redis is None:
            raise RuntimeError("no Redis client")
        await asyncio.wait_for(redis.ping(), timeout=PING_TIMEOUT_S)
        detail["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001 - any failure is "not ready"
        # The exception text can name the Redis host; it stays in the log.
        _log.warning("readiness: Redis unreachable: %s", exc)
        detail["redis"] = "unreachable"
        ready = False

    if config_problems:
        detail["config"] = "invalid"
        # Outside production a bad value is reported but does not take the
        # instance out of service: a dev box legitimately runs half-configured.
        if production:
            _log.error("readiness: configuration problems: %s", "; ".join(config_problems))
            ready = False
    else:
        detail["config"] = "ok"
    return ready, detail


@router.get("/healthz", summary="Liveness and build identity")
async def healthz() -> JSONResponse:
    return JSONResponse(liveness_body(_build_info()))


@router.get("/readyz", summary="Readiness: Redis and configuration")
async def readyz(request: Request) -> JSONResponse:
    state = request.app.state
    ready, detail = await readiness(
        getattr(state, "redis", None),
        list(getattr(state, "config_problems", [])),
        is_production(state.settings),
    )
    return JSONResponse({"status": "ok" if ready else "unavailable", **detail},
                        status_code=200 if ready else 503)
