"""scripts/package_deploy.py: the package carries its commit, and nothing it must not."""

from __future__ import annotations

import zipfile
from pathlib import Path

import package_deploy as pd


def test_a_complete_package_has_no_problems():
    assert pd.package_problems(["app/main.py", "requirements.txt", "startup.txt", "build_info.json"]) == []


def test_missing_build_info_is_refused():
    assert "missing build_info.json" in pd.package_problems(["app/main.py", "requirements.txt", "startup.txt"])


def test_secrets_files_and_tests_are_refused():
    names = ["app/main.py", "requirements.txt", "startup.txt", "build_info.json", "app/.env", "tests/x.py"]
    problems = " ".join(pd.package_problems(names))
    assert "secrets file: app/.env" in problems
    assert "tests/x.py" in problems


def test_a_dirty_tree_is_refused_unless_allowed():
    assert pd.dirty_refusal([], {"dirty": True}) is not None
    assert pd.dirty_refusal(["--allow-dirty"], {"dirty": True}) is None
    assert pd.dirty_refusal([], {"dirty": False}) is None


def test_package_zips_only_the_include_list(tmp_path: Path):
    (tmp_path / "app" / "__pycache__").mkdir(parents=True)
    (tmp_path / "app" / "main.py").write_text("x = 1\n")
    (tmp_path / "app" / "__pycache__" / "main.cpython-311.pyc").write_bytes(b"\0")
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "startup.txt").write_text("run\n")
    (tmp_path / "build_info.json").write_text("{}\n")
    (tmp_path / ".env").write_text("PLACEHOLDER=1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("")
    out = tmp_path / "pkg.zip"
    names = pd.package(tmp_path, out)
    assert sorted(names) == ["app/main.py", "build_info.json", "requirements.txt", "startup.txt"]
    with zipfile.ZipFile(out) as zf:
        assert sorted(zf.namelist()) == sorted(names)


def test_build_info_has_the_fields_healthz_serves(tmp_path: Path):
    info = pd.build_info()
    assert set(info) == {"sha", "branch", "dirty", "builtAt"}
    info["sha"] = info["sha"] or "0" * 40  # a fresh repo has no commit yet
    pd.write_build_info(info, tmp_path / "build_info.json")
    assert (tmp_path / "build_info.json").read_text().strip().startswith("{")


def test_deploy_is_not_offered_until_hosting_is_decided():
    assert pd.main(["package_deploy.py", "--deploy"]) == 2
