"""app/core/browser_pool.py: one browser per thread, and a pool of engine threads.

The real-Chromium checks run on threads of their own (never the test runner's main thread), so a
sync Playwright left over here cannot get in the way of the async tests that run on that thread.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest

from app.config import engine as engine_config
from app.core import browser_pool
from app.core.browser_pool import (
    LAUNCH_ARGS,
    BrowserPool,
    close_thread_browser,
    has_thread_browser,
    launch_kwargs,
    thread_browser,
)


@pytest.fixture
def pool() -> Iterator[BrowserPool]:
    created = BrowserPool(2, name="test-pool")
    yield created
    created.shutdown()


def test_the_bundled_chromium_takes_no_channel_and_the_container_flags():
    for channel in ("chromium", "Chromium", "", "  "):
        assert launch_kwargs(channel) == {"args": list(LAUNCH_ARGS)}
    assert "--no-sandbox" in LAUNCH_ARGS and "--disable-dev-shm-usage" in LAUNCH_ARGS


def test_an_installed_channel_is_named_so_it_fails_rather_than_falls_back():
    assert launch_kwargs("msedge") == {"args": list(LAUNCH_ARGS), "channel": "msedge"}


def test_the_channel_comes_from_the_engine_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(engine_config, "EDGE_CHANNEL", "msedge")
    assert launch_kwargs()["channel"] == "msedge"
    monkeypatch.setattr(engine_config, "EDGE_CHANNEL", "chromium")
    assert "channel" not in launch_kwargs()


def test_the_pool_size_defaults_to_the_setting(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(engine_config.engine_settings, "browser_pool_size", 3)
    with BrowserPool(name="sized") as sized:
        assert sized.size == 3
    with BrowserPool(0, name="floor") as floor:
        assert floor.size == 1


def test_a_thread_reuses_its_browser_and_relaunches_after_close():
    seen: dict[str, Any] = {}

    def on_its_own_thread() -> None:
        assert not has_thread_browser()
        first = thread_browser()
        seen["same"] = thread_browser() is first
        seen["connected"] = first.is_connected()
        close_thread_browser()
        seen["closed"] = not has_thread_browser() and not first.is_connected()
        second = thread_browser()
        seen["relaunched"] = second is not first and second.is_connected()
        close_thread_browser()
        close_thread_browser()                           # twice is safe
        seen["done"] = not has_thread_browser()

    worker = threading.Thread(target=on_its_own_thread)
    worker.start()
    worker.join(120)
    assert seen == {"same": True, "connected": True, "closed": True, "relaunched": True, "done": True}


def test_the_pool_runs_jobs_in_parallel_with_one_browser_per_thread(pool: BrowserPool):
    # Both jobs must be inside `job` at once to pass the barrier: proof the pool runs them in parallel.
    barrier = threading.Barrier(2, timeout=60)

    def job() -> tuple[str, int, bool]:
        browser = thread_browser()
        barrier.wait()
        return threading.current_thread().name, id(browser), browser is thread_browser()

    results = [future.result(timeout=120) for future in [pool.submit(job), pool.submit(job)]]
    names = {name for name, _, _ in results}
    assert names == {"test-pool-0", "test-pool-1"}
    assert len({browser for _, browser, _ in results}) == 2
    assert all(reused for _, _, reused in results)
    assert threading.current_thread().name not in names


def test_an_exception_reaches_the_future_and_the_thread_lives_on():
    with BrowserPool(1, name="solo") as solo:
        failed = solo.submit(lambda: 1 / 0)
        assert isinstance(failed.exception(timeout=30), ZeroDivisionError)
        assert solo.run(lambda: threading.current_thread().name, timeout=30) == "solo-0"


def test_shutdown_closes_every_threads_browser_and_refuses_new_work(monkeypatch: pytest.MonkeyPatch):
    closed: dict[str, tuple[bool, bool]] = {}
    original = browser_pool.close_thread_browser

    def recording_close() -> None:
        had = has_thread_browser()
        original()
        closed[threading.current_thread().name] = (had, has_thread_browser())

    monkeypatch.setattr(browser_pool, "close_thread_browser", recording_close)
    pool = BrowserPool(2, name="closing")
    barrier = threading.Barrier(2, timeout=60)

    def launch() -> bool:
        connected = thread_browser().is_connected()
        barrier.wait()                                   # so each thread launches its own
        return connected

    assert all(future.result(timeout=120) for future in [pool.submit(launch), pool.submit(launch)])
    pool.shutdown()
    assert closed == {"closing-0": (True, False), "closing-1": (True, False)}
    assert not any(thread.is_alive() for thread in pool._threads)
    with pytest.raises(RuntimeError, match="shut down"):
        pool.submit(lambda: None)
    pool.shutdown()                                      # idempotent


def test_queued_jobs_finish_before_shutdown_returns():
    pool = BrowserPool(1, name="drain")
    gate = threading.Event()
    first = pool.submit(lambda: gate.wait(30))
    later = [pool.submit(lambda n=n: n) for n in range(3)]
    gate.set()
    pool.shutdown()
    assert first.result(timeout=1) is True
    assert [future.result(timeout=1) for future in later] == [0, 1, 2]
