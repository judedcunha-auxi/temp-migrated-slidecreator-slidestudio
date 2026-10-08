"""core/logging_config: lines actually emit, carry correlation ids, and never carry credentials."""

from __future__ import annotations

import logging

import pytest

from app.core import logging_config as lc


def _record(msg: str, *args: object, name: str = "app.test", exc_info: object = None) -> logging.LogRecord:
    return logging.LogRecord(name, logging.INFO, __file__, 1, msg, args, exc_info)  # type: ignore[arg-type]


def test_secret_query_values_are_redacted():
    assert lc.scrub("GET /api/x?token=abc123&deckId=7") == "GET /api/x?token=<redacted>&deckId=7"
    assert lc.scrub("/cb?code=xyz;state=s1") == "/cb?code=<redacted>;state=<redacted>"


def test_a_percent_encoded_secret_name_is_still_redacted():
    assert "abc" not in lc.scrub("/x?%74oken=abc")


def test_bearer_tokens_are_redacted():
    assert lc.scrub("upstream said: Authorization: Bearer eyJhbGciOi.abc.def") == (
        "upstream said: Authorization: Bearer <redacted>"
    )


def test_registered_secret_values_are_redacted_and_short_ones_ignored():
    lc.register_secret_values(["registered-secret-value", "short"])
    assert lc.scrub("pw=registered-secret-value!") == "pw=<redacted>!"
    assert lc.scrub("a short word") == "a short word"


def test_the_filter_scrubs_args_without_flattening_them():
    record = _record("%s %s", "GET /x?token=abc", 200)
    lc.RedactFilter().filter(record)
    assert record.args == ("GET /x?token=<redacted>", 200)
    assert record.getMessage() == "GET /x?token=<redacted> 200"


def test_a_secret_inside_an_exception_argument_is_scrubbed():
    lc.register_secret_values(["exception-secret-1234"])
    record = _record("failed: %s", RuntimeError("header exception-secret-1234"))
    lc.RedactFilter().filter(record)
    assert "exception-secret-1234" not in record.getMessage()


def test_a_secret_in_a_traceback_is_scrubbed_and_the_exception_dropped():
    lc.register_secret_values(["traceback-secret-5678"])
    try:
        raise ValueError("traceback-secret-5678")
    except ValueError:
        import sys

        record = _record("boom", exc_info=sys.exc_info())
    lc.RedactFilter().filter(record)
    assert record.exc_info is None
    assert "traceback-secret-5678" not in record.getMessage()


def test_every_line_carries_instance_request_and_job_ids(capsys: pytest.CaptureFixture[str]):
    lc.configure_logging(force=True)
    token = lc.bind_request_id("req-42")
    job = lc.bind_job_id("job-7")
    try:
        logging.getLogger("app.test").info("hello ?token=abc")
    finally:
        lc.unbind_job_id(job)
        lc.unbind_request_id(token)
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert "[req=req-42]" in line and "[job=job-7]" in line
    assert f"[{lc.instance_id()}]" in line
    assert "token=<redacted>" in line


def test_lines_outside_a_request_say_dash(capsys: pytest.CaptureFixture[str]):
    lc.configure_logging(force=True)
    logging.getLogger("app.test").info("no request")
    assert "[req=-] [job=-]" in capsys.readouterr().out


def test_info_is_emitted_for_app_loggers_by_default(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    lc.configure_logging(force=True)
    logging.getLogger("app.anything").info("visible")
    assert "visible" in capsys.readouterr().out


def test_an_invalid_log_level_falls_back_to_info(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    lc.configure_logging(force=True)
    assert logging.getLogger("app").level == logging.INFO
    monkeypatch.delenv("LOG_LEVEL")
    lc.configure_logging(force=True)


def test_instance_id_is_short(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WEBSITE_INSTANCE_ID", "0123456789abcdef")
    assert lc.instance_id() == "01234567"
    monkeypatch.delenv("WEBSITE_INSTANCE_ID")
    monkeypatch.delenv("HOSTNAME", raising=False)
    assert lc.instance_id() == "local"
