"""
SlideForge service: core/logging_config. One place that makes application logging
actually emit.

Without a config, `app.*` falls back to Python's WARNING-only last-resort handler
and every INFO line is silently discarded. configure_logging() sends `app.*` at
INFO (LOG_LEVEL overrides) to stdout, which the platform captures. Every line
carries:

- the instance id, so interleaved scale-out logs stay readable;
- the request id (the gateway's, or one we generated), so one request's story can
  be grepped out of interleaved logs;
- the job id, for following one background job across coroutines and workers.

Credentials are redacted from every record: secret query-string values, bearer
tokens, and the exact values of this process's own secrets
(register_secret_values). Call once, early; idempotent; stdlib only, so it can run
before settings are loaded.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import logging.config
import os
import re
import sys
from collections.abc import Iterable
from typing import Any
from urllib.parse import unquote_plus

_LOG_LEVEL_ENV = "LOG_LEVEL"
_DEFAULT_LEVEL = "INFO"
_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_configured = False

# ---------------------------------------------------------------------------
# correlation ids
# ---------------------------------------------------------------------------

_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("sf_request_id", default="-")
_job_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("sf_job_id", default="-")


def bind_request_id(request_id: str) -> contextvars.Token[str]:
    """Bind the request id for the current task. Returns a token for unbind_request_id."""
    return _request_id_var.set(request_id or "-")


def unbind_request_id(token: contextvars.Token[str]) -> None:
    _request_id_var.reset(token)


def current_request_id() -> str:
    return _request_id_var.get()


def bind_job_id(job_id: str) -> contextvars.Token[str]:
    """Bind the job id for the duration of a job. contextvars are task-local, so
    this is safe across concurrent jobs."""
    return _job_id_var.set(job_id or "-")


def unbind_job_id(token: contextvars.Token[str]) -> None:
    _job_id_var.reset(token)


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------

# Query parameters whose value is a credential. uvicorn's access log prints the raw
# request line, query string included, so these are scrubbed in every record.
_SECRET_QUERY_PARAMS = frozenset((
    "code", "state", "token", "access_token", "refresh_token", "id_token",
    "client_secret", "code_verifier", "client_assertion", "sig", "signature",
    "api_key", "apikey", "key", "password",
))

# Every name=value pair, whatever the separator. The name is percent-decoded before
# the lookup: "%74oken=..." reaches a handler as "token".
_QUERY_PAIR_RE = re.compile(r"([?&;])([^=&;\s\"']+)=([^&;\s\"']+)")

# "Bearer <token>" anywhere in a line (an upstream error echoing our header).
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/]+=*")

# Exact values of this process's own secrets. Short values are skipped so a
# coincidental substring is never redacted.
_SECRET_VALUES: tuple[str, ...] = ()
_MIN_SECRET_CHARS = 8
REDACTED = "<redacted>"


def register_secret_values(values: Iterable[str]) -> None:
    """Add secrets to the set removed from every log record. Idempotent; longest
    first, so a secret that contains another is replaced whole."""
    global _SECRET_VALUES
    added = {v for v in values if isinstance(v, str) and len(v) >= _MIN_SECRET_CHARS}
    _SECRET_VALUES = tuple(sorted(added | set(_SECRET_VALUES), key=len, reverse=True))


def _redact_pair(match: re.Match[str]) -> str:
    sep, name, _value = match.groups()
    if unquote_plus(name).lower() in _SECRET_QUERY_PARAMS:
        return f"{sep}{name}={REDACTED}"
    return match.group(0)


def scrub(text: str) -> str:
    """Return `text` with credentials redacted."""
    text = _QUERY_PAIR_RE.sub(_redact_pair, text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    for secret in _SECRET_VALUES:
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


def _scrub_value(value: Any) -> Any:
    return scrub(value) if isinstance(value, str) else value


class RedactFilter(logging.Filter):
    """Strip credentials from a record. A filter (not a formatter) so it covers
    every logger reaching the handler. msg and args are scrubbed separately:
    uvicorn's access formatter unpacks record.args into exactly five fields, so the
    record must not be flattened when it has args."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _scrub_value(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(_scrub_value(a) for a in record.args)
        elif isinstance(record.msg, str):
            record.msg = scrub(record.msg)
        if record.name != "uvicorn.access" and record.args:
            # A secret that only appears once formatted (an exception passed as %s).
            try:
                message = record.getMessage()
            except Exception:  # noqa: BLE001 - a malformed record is not ours to fix
                message = ""
            if message and (clean := scrub(message)) != message:
                record.msg, record.args = clean, None
        if record.exc_info and not getattr(record, "_sf_exc_scrubbed", False):
            record._sf_exc_scrubbed = True  # type: ignore[attr-defined]
            try:
                trace = logging.Formatter().formatException(record.exc_info)
            except Exception:  # noqa: BLE001
                return True
            if (clean := scrub(trace)) != trace:
                # Flatten: the OpenTelemetry handler rebuilds the exception from
                # exc_info, so scrubbing only the text would leak to telemetry.
                record.msg = scrub(record.getMessage()) + "\n" + clean
                record.args = None
                record.exc_info = None
                record.exc_text = None
        return True


class CorrelationFilter(logging.Filter):
    """Stamp every record with the request and job ids, so the format's fields are
    always present: correlated, or '-'."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get()
        record.job_id = _job_id_var.get()
        return True


def instance_id() -> str:
    """Short, stable id for this instance. App Service sets WEBSITE_INSTANCE_ID (a
    long hash); a container sets HOSTNAME. Falls back to 'local'."""
    raw = os.environ.get("WEBSITE_INSTANCE_ID", "") or os.environ.get("HOSTNAME", "")
    return raw[:8] if raw else "local"


def _make_stdout_unicode_safe() -> None:
    """On Windows' cp1252 console, logging drops a whole record that holds one
    unencodable character. UTF-8 with errors='replace' costs one '?' instead."""
    # Best effort: pytest's capture buffers cannot be reconfigured.
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def _install_record_scrubber() -> None:
    """Scrub every record at creation, so handlers added later (Azure Monitor adds
    one to the root logger, with no filter) receive redacted text too."""
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_sf_scrubbing", False):
        return
    scrubber = RedactFilter()
    correlate = CorrelationFilter()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        correlate.filter(record)
        scrubber.filter(record)
        return record

    factory._sf_scrubbing = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


def configure_logging(*, force: bool = False) -> None:
    """Install the logging configuration. Idempotent.

    - `app.*` logs at LOG_LEVEL (default INFO); everything else at WARNING.
    - One stdout handler with an instance-, request- and job-tagged format.
    - disable_existing_loggers=False, so module-level loggers created at import
      keep working.
    """
    global _configured
    if _configured and not force:
        return
    _make_stdout_unicode_safe()

    level = os.environ.get(_LOG_LEVEL_ENV, _DEFAULT_LEVEL).upper()
    if level not in _LEVELS:
        level = _DEFAULT_LEVEL
    inst = instance_id()

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "slideforge": {
                "format": (
                    "%(asctime)s %(levelname)-8s [" + inst + "] "
                    "[req=%(request_id)s] [job=%(job_id)s] %(name)s: %(message)s"
                ),
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "filters": {
            "correlation": {"()": "app.core.logging_config.CorrelationFilter"},
            "redact": {"()": "app.core.logging_config.RedactFilter"},
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "slideforge",
                "filters": ["correlation", "redact"],
            },
        },
        "root": {"level": "WARNING", "handlers": ["console"]},
        "loggers": {
            "app": {"level": level, "propagate": True},
        },
    })
    # uvicorn owns its loggers (own handlers, propagate False), so the handler
    # filter above never sees them; put the redactor on the loggers themselves.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactFilter) for f in logger.filters):
            logger.addFilter(RedactFilter())
    _install_record_scrubber()
    _configured = True
