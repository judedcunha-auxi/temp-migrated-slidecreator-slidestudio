"""The engine's tests give the thread's Playwright back when they end (conftest.py,
`pytest_runtest_teardown`).

Sync Playwright allows one instance per thread. When the shared browser outlived the engine's
tests, `pytest engine/tests server/tests` errored in all 76 UI tests: their harness could not start
its own. This runs one engine test that uses the browser, followed by a test from outside this
directory that starts a second sync Playwright, in a separate pytest — the combined run, in
miniature, in about ten seconds.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

PROBE = '''
def test_a_second_sync_playwright_starts():
    from playwright.sync_api import sync_playwright

    sync_playwright().start().stop()
'''


def test_the_shared_browser_is_connected(browser):
    """What the check below runs first: a test that launches the shared browser."""
    assert browser.is_connected()


def test_the_shared_browser_is_closed_when_the_engine_tests_end(tmp_path: Path):
    probe = tmp_path / "test_probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    engine_test = f"{Path(__file__).resolve()}::test_the_shared_browser_is_connected"
    done = subprocess.run(
        [sys.executable, "-m", "pytest", engine_test, str(probe), "-q", "-p", "no:cacheprovider",
         f"--basetemp={tmp_path / 'inner'}"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180,
    )
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert "2 passed" in done.stdout, done.stdout
