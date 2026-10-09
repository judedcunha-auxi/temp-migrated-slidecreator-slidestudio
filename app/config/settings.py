"""
SlideForge service: config/settings. All environment-driven configuration.

Values come from environment variables, or from a local `.env` in development. Every
variable the service reads is listed in `.env.example` with a placeholder, and
`tests/config/test_env_example.py` fails the build if one is missing there.

`check_config()` is the startup config check (repo standards, "Core standards"): it
returns a list of problems. Values that are always invalid are flagged in every
environment; values that production requires are flagged only in a production-like
config. Problems are logged as errors at startup, and `/readyz` keeps an instance
with problems out of service in production.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config.ai import AISettings, ai_settings, check_ai_config, check_ai_production
from app.config.engine import EngineSettings, check_engine_config, check_engine_production, engine_settings

# Project root is three levels above this file:
#   app/config/settings.py -> app/config/ -> app/ -> project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Environments check_config() knows. An unknown value is a problem, not a guess.
PRODUCTION_ENVIRONMENTS = frozenset({"production", "prod", "staging"})
NON_PRODUCTION_ENVIRONMENTS = frozenset({"development", "dev", "local", "test"})

STORAGE_BACKENDS = ("fake", "local", "general")

# An HTTP header name (RFC 9110 token), kept to the characters a gateway would use.
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        # A validation error otherwise prints the offending input, which for a
        # secret puts it in the boot traceback and the platform log.
        hide_input_in_errors=True,
    )

    # Which rules check_config() applies. Set it explicitly in every deployed
    # environment ("staging" or "production"); empty falls back to a heuristic
    # (see is_production) that errs towards production.
    environment: str = ""

    # Redis: queue, in-flight limiter and counters (decision D6). 127.0.0.1, not
    # "localhost": on Windows the IPv6 attempt goes first and stalls about 2s per
    # connection against an IPv4-only Redis.
    redis_url: str = "redis://127.0.0.1:6379/0"
    # RESP protocol. RESP3 needs Redis 6+; set 2 for older local builds.
    redis_protocol: int = 3

    # Browser origins allowed to call the API directly, comma separated. Until the
    # gateway exists (decision D9) the Darwin static frontend calls the service
    # cross-origin; this is that one static origin. Never "*".
    cors_allowed_origins: str = ""

    # The header the gateway puts the request id in. Read on every request, echoed
    # on every response, and logged on every line.
    request_id_header: str = "X-Request-ID"

    # Azure App Service sets this. Used for the instance id in log lines and as a
    # production signal when ENVIRONMENT is unset.
    website_instance_id: str = ""

    # App Insights. Also read directly from os.environ by app/core/telemetry.py so
    # telemetry can start before settings; listed here so check_config() can
    # require it in production and so the scrubber knows it is a secret.
    applicationinsights_connection_string: str = ""

    # Durable storage (Phase 4). "general" is the General service (the only
    # backend allowed in production; its adapter is a stub until the repo exists,
    # D5). "local" keeps data in files under SCRATCH_ROOT, "fake" in memory; both
    # are for development and tests only.
    storage_backend: str = "fake"
    # The General service base URL. Required (https) when STORAGE_BACKEND=general.
    general_service_url: str = ""
    # Where job workspaces and the local storage adapter write. Nothing is written
    # outside it. Empty: <system temp>/slideforge-scratch. Must be absolute if set.
    scratch_root: str = ""

    @field_validator("environment", "storage_backend")
    @classmethod
    def _normalise_environment(cls, value: str) -> str:
        return value.strip().lower()

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.cors_allowed_origins.split(",") if o.strip()]


settings = Settings()


def is_production(s: Settings) -> bool:
    """Whether to hold `s` to the production requirements.

    An explicit ENVIRONMENT is authoritative. When it is unset, running on Azure
    (WEBSITE_INSTANCE_ID present) counts as production, so a deployment that forgot
    ENVIRONMENT still gets the checks instead of silently skipping them.
    """
    if s.environment in PRODUCTION_ENVIRONMENTS:
        return True
    if s.environment in NON_PRODUCTION_ENVIRONMENTS:
        return False
    return bool(s.website_instance_id)


def secret_values(s: Settings, ai: AISettings | None = None) -> list[str]:
    """Every secret this process holds, for the log scrubber (core/logging_config).
    Nothing here is logged or returned anywhere else. `ai` defaults to the process's
    AI settings (the provider keys)."""
    ai = ai if ai is not None else ai_settings
    values: list[str] = [ai.anthropic_key, ai.gemini_key]
    try:
        values.append(urlparse(s.redis_url).password or "")
    except ValueError:
        pass
    try:
        values.append(urlparse(s.general_service_url).password or "")
    except ValueError:
        pass
    conn = s.applicationinsights_connection_string
    match = re.search(r"InstrumentationKey=([^;]+)", conn)
    if match:
        values.append(match.group(1))
    return [v for v in values if v]


def _origin_problem(origin: str, production: bool) -> str | None:
    if origin == "*":
        return "CORS_ALLOWED_ORIGINS must not contain '*'; list the static origin explicitly."
    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return f"CORS_ALLOWED_ORIGINS entry {origin!r} is not an http(s) origin."
    if parsed.path or parsed.query or parsed.fragment:
        return f"CORS_ALLOWED_ORIGINS entry {origin!r} must be an origin only (no path or query)."
    if production and parsed.scheme != "https":
        return f"CORS_ALLOWED_ORIGINS entry {origin!r} must use https in production."
    return None


def check_config(s: Settings, engine: EngineSettings | None = None, ai: AISettings | None = None) -> list[str]:
    """Return the configuration problems in `s`, the engine and the AI settings (empty means all good).

    `engine` defaults to the process's `SLIDE_ENGINE_*` settings (app/config/engine.py) and `ai` to
    its AI settings (app/config/ai.py: provider keys, models, cost, fan-out); the production-only
    rules of both apply when `s` is production-like. Pure function of its
    arguments, so tests construct inputs directly. Advisory: main.py
    logs the problems as errors and /readyz refuses traffic in production, but the
    process still boots, so a borderline rule cannot take a running service down.
    Messages name the variable, never its value.
    """
    problems: list[str] = []
    production = is_production(s)
    engine = engine if engine is not None else engine_settings
    ai = ai if ai is not None else ai_settings
    problems.extend(check_engine_config(engine))
    problems.extend(check_ai_config(ai))

    # --- always, any environment ------------------------------------------------
    if s.environment and s.environment not in PRODUCTION_ENVIRONMENTS | NON_PRODUCTION_ENVIRONMENTS:
        known = sorted(PRODUCTION_ENVIRONMENTS | NON_PRODUCTION_ENVIRONMENTS)
        problems.append(f"ENVIRONMENT is not one of {known}.")
    if s.redis_protocol not in (2, 3):
        problems.append("REDIS_PROTOCOL must be 2 or 3.")
    try:
        redis_scheme = urlparse(s.redis_url).scheme
    except ValueError:
        redis_scheme = ""
    if redis_scheme not in ("redis", "rediss", "unix"):
        problems.append("REDIS_URL must be a redis://, rediss:// or unix:// URL.")
    if not _HEADER_NAME.match(s.request_id_header):
        problems.append("REQUEST_ID_HEADER must be a plain header name (letters, digits, '-').")
    for origin in s.cors_origins:
        problem = _origin_problem(origin, production)
        if problem:
            problems.append(problem)

    if s.storage_backend not in STORAGE_BACKENDS:
        problems.append(f"STORAGE_BACKEND must be one of {list(STORAGE_BACKENDS)}.")
    if s.storage_backend == "general":
        # PLACEHOLDER until the General service repo exists (D5).
        problems.append("STORAGE_BACKEND=general: the General service adapter is not written yet (D5).")
        try:
            gs = urlparse(s.general_service_url)
        except ValueError:
            gs = urlparse("")
        if gs.scheme not in ("http", "https") or not gs.netloc:
            problems.append("GENERAL_SERVICE_URL must be an http(s) URL when STORAGE_BACKEND=general.")
        elif production and gs.scheme != "https":
            problems.append("GENERAL_SERVICE_URL must use https in production.")
    if s.scratch_root and not Path(s.scratch_root).is_absolute():
        problems.append("SCRATCH_ROOT must be an absolute path.")

    # --- production only --------------------------------------------------------
    if production:
        if s.storage_backend in ("fake", "local"):
            problems.append(
                f"STORAGE_BACKEND={s.storage_backend} is for development only; production "
                "stores data through the General service (STORAGE_BACKEND=general)."
            )
        if redis_scheme != "rediss":
            problems.append("REDIS_URL must use rediss:// (TLS) in production.")
        if not s.applicationinsights_connection_string:
            problems.append(
                "APPLICATIONINSIGHTS_CONNECTION_STRING is empty in production; "
                "there would be no metrics or traces."
            )
        if not s.cors_origins:
            problems.append(
                "CORS_ALLOWED_ORIGINS is empty in production; the static frontend "
                "could not call the API (decision D9)."
            )
        problems.extend(check_engine_production(engine))
        problems.extend(check_ai_production(ai))
    return problems
