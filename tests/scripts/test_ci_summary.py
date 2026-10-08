"""scripts/ci_summary.py: a red run still reports, and report text is escaped."""

from __future__ import annotations

import json
from pathlib import Path

import ci_summary as cs

JUNIT = """<?xml version="1.0"?>
<testsuites><testsuite name="pytest" time="1.5">
  <testcase classname="tests.test_a" name="test_ok" time="0.2"/>
  <testcase classname="tests.test_a" name="test_bad|`pipe`" time="0.1">
    <failure message="assert 1 == 2 &lt;b&gt;bold&lt;/b&gt;">trace</failure>
  </testcase>
  <testcase classname="tests.test_a" name="test_skip" time="0"><skipped message="later"/></testcase>
</testsuite></testsuites>"""


def test_the_summary_reports_failures_and_escapes_them(tmp_path: Path):
    (tmp_path / "unit.xml").write_text(JUNIT, encoding="utf-8")
    (tmp_path / "pip-audit.json").write_text(json.dumps({"dependencies": [
        {"name": "pkg", "version": "1.0", "vulns": [{"id": "PYSEC-1", "fix_versions": ["1.1"]}]},
        {"name": "ok", "version": "2.0", "vulns": []},
    ]}), encoding="utf-8")
    env = {"GITHUB_SHA": "abcdef1234", "STEPS_JSON": json.dumps({
        "unit-tests": {"outcome": "failure"}, "lint": {"outcome": "skipped"}})}
    text = cs.backend(env, tmp_path)
    assert "**3 tests** · 1 passed · 1 failed · 1 skipped" in text
    assert "<b>" not in text and "&lt;b&gt;" in text
    assert "1 of 2 steps failed" in text
    assert "PYSEC-1" in text and "1 known vulnerabilities in 2 Python packages" in text


def test_missing_reports_are_said_not_hidden(tmp_path: Path):
    text = cs.backend({}, tmp_path / "nothing")
    assert "No test report was written." in text
    assert "No pip-audit report was written." in text


def test_an_unreadable_report_is_named(tmp_path: Path):
    (tmp_path / "broken.xml").write_text("<testsuite", encoding="utf-8")
    assert "report could not be read" in cs.backend({}, tmp_path)


def test_step_names_keep_tool_names():
    assert cs.step_name("dependency-audit") == "Dependency audit"
    assert cs.step_name("gitleaks-secret-scan") == "gitleaks secret scan"


def test_usage_error_for_unknown_kind():
    assert cs.main(["ci_summary.py", "frontend"]) == 2
