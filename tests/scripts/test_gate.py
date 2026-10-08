"""scripts/gate.py: the pin check catches drift between installed and pinned versions."""

from __future__ import annotations

from pathlib import Path

import gate
import pytest


def test_pins_pass_when_installed_versions_satisfy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "requirements.txt").write_text("# comment\npytest>=1.0\n\n", encoding="utf-8")
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    assert gate.check_pins()[0] is True


def test_pins_fail_on_drift_or_absence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "requirements.txt").write_text("pytest<1.0\nno-such-package-xyz==1.0\n", encoding="utf-8")
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    ok, message = gate.check_pins()
    assert ok is False
    assert "does not satisfy" in message and "NOT INSTALLED" in message
