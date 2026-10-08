"""scripts/verify_deploy.py and whats_deployed.py: the verdicts, without a network."""

from __future__ import annotations

import _common
import pytest
import verify_deploy as vd
import whats_deployed as wd

SHA = "c" * 40


def _all_ok(findings: list[tuple[bool, str]]) -> bool:
    return all(ok for ok, _ in findings)


def test_ready_target_passes():
    assert _all_ok(vd.readiness_findings(200, {"status": "ok", "redis": "ok", "config": "ok"}))


def test_unready_target_fails():
    assert not _all_ok(vd.readiness_findings(503, {"status": "unavailable", "redis": "unreachable"}))
    assert not _all_ok(vd.readiness_findings(200, {"redis": "ok", "config": "invalid"}))


def test_identity_matches_the_expected_commit():
    assert _all_ok(vd.identity_findings(200, {"sha": SHA, "dirty": False}, SHA))


def test_identity_fails_on_wrong_commit_dirty_build_or_no_stamp():
    assert not _all_ok(vd.identity_findings(200, {"sha": SHA, "dirty": False}, "d" * 40))
    assert not _all_ok(vd.identity_findings(200, {"sha": SHA, "dirty": True}, SHA))
    assert not _all_ok(vd.identity_findings(200, {"status": "ok"}, SHA))
    assert not _all_ok(vd.identity_findings(200, {"sha": SHA, "dirty": False}, ""))
    assert not _all_ok(vd.identity_findings(503, {}, SHA))


def test_expected_sha_prefers_the_flag_then_github_sha(monkeypatch: pytest.MonkeyPatch):
    assert vd.expected_sha(["x", "staging", "--sha", "abc"], "staging") == "abc"
    monkeypatch.setenv("GITHUB_SHA", "def")
    assert vd.expected_sha(["x", "staging"], "staging") == "def"


def test_whats_deployed_verdicts():
    assert wd.verdict({"sha": SHA, "dirty": False}, SHA)[0] is True
    assert wd.verdict({"sha": SHA, "dirty": False}, None)[0] is True
    assert wd.verdict({"sha": SHA, "dirty": False}, "e" * 40)[0] is False
    assert wd.verdict({"sha": SHA, "dirty": True}, SHA)[0] is False
    assert wd.verdict({"status": "ok"}, SHA)[0] is False
    assert wd.verdict({"sha": SHA, "dirty": False}, "")[0] is False


def test_an_unconfigured_target_exits_with_a_reason(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SLIDEFORGE_STAGING_URL", raising=False)
    with pytest.raises(SystemExit, match="SLIDEFORGE_STAGING_URL"):
        _common.target_host("staging")
    monkeypatch.setenv("SLIDEFORGE_STAGING_URL", "https://staging.example/")
    assert _common.target_host("staging") == ("https://staging.example", "staging")


def test_http_refuses_non_http_urls():
    status, body, _ = _common.http("file:///etc/hosts")
    assert status == 0 and "refusing" in body
