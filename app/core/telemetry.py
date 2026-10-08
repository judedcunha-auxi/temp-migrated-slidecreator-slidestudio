"""
SlideForge service: core/telemetry. Metrics and traces to Application Insights.

configure_telemetry() wires the Azure Monitor OpenTelemetry distro, but ONLY when
APPLICATIONINSIGHTS_CONNECTION_STRING is set, so local runs, tests and CI are a
clean no-op. instrument_app() adds per-request server spans with the query string
stripped (our query strings carry ids and, later, signed links).

record_request() is the per-endpoint metric the standards ask for: latency and
outcome per route, tagged by feature (the route's first tag). It is called by the
request-id middleware for every request. Every function here swallows its own
errors: telemetry must never break a request.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any

_log = logging.getLogger(__name__)

_CONN_ENV = "APPLICATIONINSIGHTS_CONNECTION_STRING"
# SLIDEFORGE_TELEMETRY_DEBUG=1 turns on the exporter's own diagnostics. Scoped to
# the OTel loggers only, NOT azure.core, whose HTTP logging dumps request headers
# (connection-string key material included) at DEBUG.
_DEBUG_ENV = "SLIDEFORGE_TELEMETRY_DEBUG"
_DEBUG_LOGGERS = ("azure.monitor.opentelemetry", "opentelemetry")

try:
    from opentelemetry import metrics as _metrics

    _meter = _metrics.get_meter("slideforge.service")
    _request_duration = _meter.create_histogram(
        "slideforge.request.duration",
        unit="s",
        description="Request latency per route, tagged by feature.",
    )
    _request_count = _meter.create_counter(
        "slideforge.request.count",
        description="Requests per route, tagged by feature and status class (2xx/4xx/5xx).",
    )
    _OTEL_AVAILABLE = True
except Exception:  # noqa: BLE001 - opentelemetry missing: telemetry is a pure no-op
    _OTEL_AVAILABLE = False

_configured = False


def is_configured() -> bool:
    return _configured


def configure_telemetry(*, force: bool = False) -> bool:
    """Enable App Insights export if the connection string is set. Idempotent.
    Returns whether export is enabled. Call after configure_logging(), so the Azure
    log handler attaches alongside the stdout one."""
    global _configured
    if _configured and not force:
        return True
    conn = os.environ.get(_CONN_ENV, "").strip()
    if not conn:
        _log.info("telemetry: %s not set; App Insights export disabled", _CONN_ENV)
        return False
    if os.environ.get(_DEBUG_ENV, "").strip().lower() in ("1", "true"):
        for name in _DEBUG_LOGGERS:
            logging.getLogger(name).setLevel(logging.DEBUG)
        _log.warning("telemetry: %s on; verbose exporter logging enabled", _DEBUG_ENV)
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        # FastAPI auto-instrumentation is off here because instrument_app() does it
        # explicitly, with the query-stripping hook.
        configure_azure_monitor(
            connection_string=conn,
            instrumentation_options={"fastapi": {"enabled": False}},
        )
        _configured = True
        _log.info("telemetry: App Insights export enabled")
    except Exception as exc:  # noqa: BLE001 - never stop the app booting over telemetry
        _log.warning("telemetry: failed to enable App Insights export: %s", type(exc).__name__)
    return _configured


def strip_query_from_span(span: Any, scope: dict[str, Any]) -> None:
    """Server-request hook: drop the query string from the span's URL attributes.
    Route, method and status are what request telemetry needs."""
    with contextlib.suppress(Exception):  # never break the request path over telemetry
        if span is None or not span.is_recording():
            return
        path = scope.get("path", "") or ""
        span.set_attribute("http.target", path)
        span.set_attribute("url.path", path)
        span.set_attribute("url.query", "")
        for key in ("http.url", "url.full"):
            span.set_attribute(key, path)


def instrument_app(app: Any) -> None:
    """Add OpenTelemetry server spans to a FastAPI app. No-op unless configured."""
    if not _OTEL_AVAILABLE or not _configured:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, server_request_hook=strip_query_from_span)
        _log.info("telemetry: FastAPI request spans enabled")
    except Exception as exc:  # noqa: BLE001
        _log.warning("telemetry: FastAPI instrumentation not enabled: %s", type(exc).__name__)


def status_class(status: int) -> str:
    return f"{status // 100}xx" if 100 <= status <= 599 else "other"


def record_request(*, route: str, method: str, status: int, duration_s: float, feature: str) -> None:
    """Record one request's latency and outcome. `route` must be the route template
    (low cardinality), never the raw path. Never raises."""
    if not _OTEL_AVAILABLE:
        return
    attrs = {
        "http.route": route,
        "http.method": method,
        "status_class": status_class(status),
        "feature": feature,
    }
    with contextlib.suppress(Exception):  # a metrics hiccup must never break a request
        _request_duration.record(duration_s, attrs)
        _request_count.add(1, attrs)
