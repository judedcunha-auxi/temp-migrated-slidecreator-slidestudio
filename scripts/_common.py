"""Shared plumbing for scripts/. Standard library plus the Azure CLI, so no virtual
environment is needed to run any script here.

Python, not bash, for the reasons learned in Auxi_Connector: JSON and query quoting
that breaks silently in shell, Git Bash rewriting leading-slash Azure resource ids
(az() sets MSYS_NO_PATHCONV once, here), and console encoding on Windows.

Hosting is not decided yet (decision D2: App Service for Containers, Container Apps,
or zip deploy), so the deploy targets' hosts come from the environment rather than
being hard-coded:

    SLIDEFORGE_STAGING_URL     e.g. https://<staging host>
    SLIDEFORGE_PRODUCTION_URL  e.g. https://<production host>

`local` always means http://127.0.0.1:8000. Once D2 is settled, put the real hosts
in TARGETS and drop the environment lookup.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# The Windows console defaults to cp1252, which cannot encode the box-drawing
# characters these reports use; the failure is a hard UnicodeEncodeError mid-report.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
SERVICE = "slideforge-service"

# The platform's health check must point here (repo standards, "Health endpoints").
EXPECTED_HEALTH_PATH = "/readyz"

# target -> (environment variable holding the host, default host, git branch it deploys)
TARGETS: dict[str, tuple[str, str, str]] = {
    "staging": ("SLIDEFORGE_STAGING_URL", "", "staging"),
    "production": ("SLIDEFORGE_PRODUCTION_URL", "", "production"),
    "local": ("SLIDEFORGE_LOCAL_URL", "http://127.0.0.1:8000", "HEAD"),
}


def target_host(target: str) -> tuple[str, str]:
    """(host, git ref) for a deploy target. Exits with a clear message when the host
    is not known yet."""
    try:
        env_name, default, ref = TARGETS[target]
    except KeyError:
        sys.exit(f"Unknown target {target!r}; use one of: {', '.join(TARGETS)}.")
    host = os.environ.get(env_name, "").strip().rstrip("/") or default
    if not host:
        sys.exit(f"No host for {target!r}: set {env_name} (hosting is undecided, decision D2).")
    return host, ref


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------

class Report:
    """Pass/fail tally with one shape, so a script's exit code and its printed
    summary can never disagree."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def ok(self, message: str) -> None:
        self.passed += 1
        print(f"  PASS  {message}")

    def bad(self, message: str) -> None:
        self.failed += 1
        print(f"  FAIL  {message}")

    def note(self, message: str) -> None:
        print(f"        {message}")

    def finish(self, title: str) -> int:
        print()
        print("=" * 62)
        print(f" {title}: {self.passed} passed, {self.failed} failed")
        print("=" * 62)
        return 1 if self.failed else 0


def section(title: str) -> None:
    print()
    print(f"── {title} " + "─" * max(0, 58 - len(title)))


def banner(title: str) -> None:
    print("=" * 62)
    print(f" {title}")
    print("=" * 62)


# ---------------------------------------------------------------------------
# git, az
# ---------------------------------------------------------------------------

def git(*args: str) -> str:
    """`git` in the repo root; stdout stripped, '' on failure."""
    proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)  # noqa: S603,S607
    return proc.stdout.strip() if proc.returncode == 0 else ""


def az(*args: str, check: bool = False) -> str:
    """Run `az` and return stdout, stripped. MSYS_NO_PATHCONV stops Git Bash from
    rewriting leading-slash resource ids. Unused until hosting is decided (D2)."""
    exe = shutil.which("az") or shutil.which("az.cmd")
    if not exe:
        sys.exit("az CLI not found on PATH; install it and run `az login` first.")
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    proc = subprocess.run(  # noqa: S603
        [exe, *args], capture_output=True, text=True, env=env, encoding="utf-8", errors="replace"
    )
    if check and proc.returncode != 0:
        sys.exit(f"az {' '.join(args)} failed:\n{proc.stderr.strip()}")
    return (proc.stdout or "").strip()


# ---------------------------------------------------------------------------
# http
# ---------------------------------------------------------------------------

def http(url: str, *, timeout: float = 30.0, retries: int = 0,
         headers: dict[str, str] | None = None) -> tuple[int, str, dict[str, str]]:
    """One GET. Returns (status, body, headers) and never raises for an HTTP error
    status: a 503 is data here. Status 0 means no response at all. `retries` covers
    the minutes after a deploy, when the platform may still be warming."""
    if not url.startswith(("http://", "https://")):
        return 0, f"refusing a non-http URL: {url!r}", {}
    req = urllib.request.Request(url, headers=headers or {})  # noqa: S310 - scheme checked above
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310  # nosec B310
                return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers)
        except Exception as exc:  # noqa: BLE001 - refused or reset while warming
            last = exc
            if attempt < retries:
                time.sleep(3)
    return 0, f"{type(last).__name__}: {last}", {}


def http_json(url: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    """GET and parse a JSON object; {} when the body is not one."""
    status, body, _ = http(url, **kwargs)
    try:
        parsed = json.loads(body)
    except ValueError:
        return status, {}
    return status, parsed if isinstance(parsed, dict) else {}


def timed(url: str, timeout: float = 30.0) -> float:
    """Wall-clock seconds for one GET, or -1 if it got no response."""
    start = time.monotonic()
    status, _, _ = http(url, timeout=timeout)
    return (time.monotonic() - start) if status else -1.0
