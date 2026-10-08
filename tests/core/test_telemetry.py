"""core/telemetry: a clean no-op without App Insights, and never able to break a request."""

from __future__ import annotations

from typing import Any

import pytest

from app.core import telemetry


def test_without_a_connection_string_export_stays_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    monkeypatch.setattr(telemetry, "_configured", False)
    assert telemetry.configure_telemetry() is False
    assert telemetry.is_configured() is False


def test_a_failing_exporter_does_not_stop_the_app(monkeypatch: pytest.MonkeyPatch):
    import azure.monitor.opentelemetry as distro

    def boom(**_: Any) -> None:
        raise RuntimeError("exporter exploded")

    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=00000000-0000-0000-0000-000000000000")
    monkeypatch.setattr(telemetry, "_configured", False)
    monkeypatch.setattr(distro, "configure_azure_monitor", boom)
    assert telemetry.configure_telemetry() is False


def test_instrument_app_is_a_no_op_when_not_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(telemetry, "_configured", False)
    sentinel = object()
    telemetry.instrument_app(sentinel)  # must not raise on a non-app


def test_record_request_never_raises(monkeypatch: pytest.MonkeyPatch):
    class Broken:
        def record(self, *_: Any) -> None:
            raise RuntimeError("metrics down")

        def add(self, *_: Any) -> None:
            raise RuntimeError("metrics down")

    monkeypatch.setattr(telemetry, "_request_duration", Broken())
    monkeypatch.setattr(telemetry, "_request_count", Broken())
    telemetry.record_request(route="/healthz", method="GET", status=200, duration_s=0.01, feature="health")


def test_status_classes():
    assert [telemetry.status_class(s) for s in (200, 404, 503, 42)] == ["2xx", "4xx", "5xx", "other"]


def test_the_span_hook_drops_the_query_string():
    class Span:
        attrs: dict[str, Any] = {}

        def is_recording(self) -> bool:
            return True

        def set_attribute(self, key: str, value: Any) -> None:
            self.attrs[key] = value

    span = Span()
    telemetry.strip_query_from_span(span, {"path": "/api/status", "query_string": b"deckId=1&token=x"})
    assert span.attrs["url.query"] == ""
    assert all("token" not in str(v) for v in span.attrs.values())


def test_the_span_hook_never_raises():
    telemetry.strip_query_from_span(object(), {})
