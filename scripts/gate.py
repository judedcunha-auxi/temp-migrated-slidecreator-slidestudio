#!/usr/bin/env python3
"""The full local gate: the same checks CI runs, in the same order.

    python scripts/gate.py             # everything
    python scripts/gate.py --offline   # skip pip-audit (it needs the network)

RUN IT WITH THE INTERPRETER YOU MEAN: every Python step uses sys.executable, so run
it from the project's 3.11 virtual environment (the version CI and Azure use):

    .venv/Scripts/python.exe scripts/gate.py      # Windows
    .venv/bin/python scripts/gate.py              # Linux / macOS

The banner prints the interpreter and key package versions first, so a wrong
environment is visible before anything runs. What it encodes that is easy to get
wrong by hand (rationale in scripts/README.md):

1. Installed versions are checked against requirements.txt. `pip check` proves the
   installed set is consistent; it says nothing about whether it is what we pinned,
   so a green gate could otherwise be testing a library that will never deploy.
2. pip-audit is part of the gate. A green test suite says nothing about the
   dependency graph.
3. gitleaks runs when it is on PATH (CI always runs it). Without it the gate says
   SKIP rather than PASS: a check that did not run must not look clean.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time

# Also gives this script the console-encoding fix for Windows.
from _common import ROOT, banner, section


def check_pins() -> tuple[bool, str]:
    """Do the INSTALLED versions satisfy requirements.txt? In-process: gate.py runs
    under the very interpreter whose packages are in question."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        from packaging.requirements import InvalidRequirement, Requirement
    except ImportError:
        return False, "packaging is not installed (pip install -r requirements-dev.txt)"

    bad: list[str] = []
    for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].strip()
        if not line:
            continue
        try:
            req = Requirement(line)
        except InvalidRequirement:
            continue
        try:
            have = version(req.name)
        except PackageNotFoundError:
            bad.append(f"{req.name}: NOT INSTALLED (needs {req.specifier})")
            continue
        if req.specifier and not req.specifier.contains(have, prereleases=True):
            bad.append(f"{req.name}=={have} does not satisfy {req.specifier}")
    if bad:
        return False, "; ".join(bad)
    return True, "every installed version satisfies requirements.txt"


class Gate:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []  # (label, PASS|FAIL|SKIP, tail)
        self.started = time.monotonic()

    def record(self, label: str, outcome: str, tail: str) -> None:
        self.results.append((label, outcome, tail))
        print(f"  {label:<26} {outcome}  {tail[:80]}")

    def run(self, label: str, cmd: list[str]) -> bool:
        proc = subprocess.run(  # noqa: S603 - fixed argument lists, no shell
            cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
        ok = proc.returncode == 0
        self.record(label, "PASS" if ok else "FAIL", out[-1] if out else "")
        if not ok:
            for line in out[-30:]:
                print(f"      {line}")
        return ok

    def finish(self) -> int:
        failed = [label for label, outcome, _ in self.results if outcome == "FAIL"]
        skipped = [label for label, outcome, _ in self.results if outcome == "SKIP"]
        elapsed = time.monotonic() - self.started
        print()
        print("=" * 62)
        if failed:
            print(f" GATE FAILED in {elapsed:.0f}s: {', '.join(failed)}")
        else:
            print(f" GATE PASSED in {elapsed:.0f}s: {len(self.results) - len(skipped)} checks")
        if skipped:
            print(f" NOT RUN (CI still runs them): {', '.join(skipped)}")
        print("=" * 62)
        return 1 if failed else 0


def main(argv: list[str]) -> int:
    offline = "--offline" in argv
    py = sys.executable
    versions = subprocess.run(  # noqa: S603
        [py, "-c",
         "import sys,importlib.metadata as m;"
         "print(f'python {sys.version.split()[0]}', end='');"
         "[print(f'  {p}=={m.version(p)}', end='') for p in ('fastapi','redis','pydantic')]"],
        capture_output=True, text=True,
    ).stdout.strip()
    banner(f"gate: {versions or py}")
    print(f" root: {ROOT}")
    if not sys.version.startswith("3.11"):
        print(" (!) not Python 3.11: CI and Azure run 3.11, so a pass here is weaker evidence.")

    g = Gate()
    section("Backend (the CI job's steps, in order)")
    g.run("pip check", [py, "-m", "pip", "check"])
    ok, message = check_pins()
    g.record("pins match requirements", "PASS" if ok else "FAIL", message)
    if not ok:
        print("      Fix with: pip install -r requirements.txt")
    g.run("import app.main", [py, "-c", "import app.main"])
    g.run("unit tests", [py, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"])
    g.run("ruff", [py, "-m", "ruff", "check", "app/", "tests/"])
    g.run("mypy", [py, "-m", "mypy", "-p", "app"])
    if offline:
        g.record("pip-audit", "SKIP", "--offline")
    else:
        g.run("pip-audit", [py, "-m", "pip_audit", "-r", "requirements.txt"])
    gitleaks = shutil.which("gitleaks")
    if gitleaks:
        g.run("gitleaks", [gitleaks, "git", "--redact", "--no-banner", "--no-color", str(ROOT)])
    else:
        g.record("gitleaks", "SKIP", "gitleaks is not on PATH")
    g.run("bandit", [py, "-m", "bandit", "-r", "app", "-q"])
    return g.finish()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
