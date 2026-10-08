"""The CI workflow keeps the exact names and commands the repo standards fix, and the
local gate runs the same checks. Text-based on purpose: no YAML dependency, and the
names are what people search for."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
GATE = (ROOT / "scripts" / "gate.py").read_text(encoding="utf-8")

BACKEND_STEPS = [
    ("Install dependencies", ["pip install -r requirements.txt", "pip install -r requirements-dev.txt"]),
    ("Verify dependency set is consistent", ["pip check"]),
    ("Import smoke test (app boots)", ['python -c "import app.main"']),
    ("Unit tests", ["pytest tests -v --junitxml=ci-reports/unit.xml"]),
    ("Lint (ruff)", ["ruff check app/ tests/"]),
    ("Type check (mypy)", ["mypy -p app"]),
    ("Dependency audit (pip-audit)", ["pip-audit -r requirements.txt -f json -o ci-reports/pip-audit.json"]),
    ("Secret scan (gitleaks)", ["gitleaks git"]),
    ("Static security scan (bandit)", ["bandit -r app"]),
    ("Job summary", ['python scripts/ci_summary.py backend >> "$GITHUB_STEP_SUMMARY"']),
]


def _job(name: str) -> str:
    """The text of one job, from its `name:` line to the next top-level job."""
    start = CI.index(f"    name: {name}\n")
    following = re.search(r"\n  [a-z][a-z-]*:\n", CI[start:])
    return CI[start:start + following.start()] if following else CI[start:]


def test_workflow_settings():
    assert CI.startswith("name: CI\n")
    assert 'branches: ["**"]' in CI and "pull_request:" in CI and "workflow_dispatch:" in CI
    assert "permissions:\n  contents: read\n" in CI
    assert "group: ci-${{ github.workflow }}-${{ github.ref }}" in CI
    assert "cancel-in-progress: true" in CI


def test_backend_job_steps_are_named_and_ordered_as_the_standard_says():
    job = _job("Backend (pytest + ruff + mypy)")
    assert "runs-on: ubuntu-latest" in job
    assert "actions/checkout@v4" in job and "persist-credentials: false" in job
    assert "actions/setup-python@v5" in job and 'python-version: "3.11"' in job
    assert "requirements.txt\n            requirements-dev.txt" in job
    names = re.findall(r"^      - name: (.+)$", job, flags=re.MULTILINE)
    assert names == [name for name, _ in BACKEND_STEPS]
    for name, commands in BACKEND_STEPS:
        block = job[job.index(f"- name: {name}"):]
        nxt = block.find("\n      - ", 1)
        block = block if nxt < 0 else block[:nxt]
        for command in commands:
            assert command in block, f"{name}: expected `{command}`"
    summary = job[job.index("- name: Job summary"):]
    assert "if: always()" in summary


def test_the_secret_scan_reads_the_full_history():
    assert "fetch-depth: 0" in _job("Backend (pytest + ruff + mypy)")


def test_deploy_artifact_job():
    job = _job("Deploy artifact (SHA-addressed)")
    assert "needs: [backend]" in job
    assert "github.event_name == 'push'" in job and '["staging","production"]' in job
    assert "python scripts/package_deploy.py" in job
    assert "actions/upload-artifact@v4" in job
    assert "name: deploy-${{ github.sha }}" in job and "path: staging-deploy.zip" in job
    assert "retention-days: 30" in job


def test_deploy_jobs_exist_as_marked_stubs_that_verify():
    for name, target in (("Deploy to staging", "staging"), ("Deploy to production", "production")):
        job = _job(name)
        assert "if: false" in job, f"{name} must stay disabled until D2/D18/D19 are decided"
        assert "TODO(D2/D18/D19)" in job
        assert f"python scripts/verify_deploy.py {target}" in job
    assert "TODO(D2, D18, D19)" in CI


def test_the_local_gate_runs_the_same_checks():
    for fragment in ('"pip", "check"', '"import app.main"', '"pytest", "tests"', '"ruff", "check", "app/", "tests/"',
                     '"mypy", "-p", "app"', '"pip_audit", "-r", "requirements.txt"', '"bandit", "-r", "app"',
                     "gitleaks"):
        assert fragment in GATE, f"gate.py does not run {fragment}"
