"""SlideForge service: core/browser_pool. Where the engine's measuring browser comes from.

The engine measures slide HTML in a real Chromium (Playwright). Playwright's **sync** API is bound to
the thread that started it: a browser created on one thread cannot be touched from another. So the
unit of reuse is "one browser per thread", and this module owns both halves of that:

* `thread_browser()` / `close_thread_browser()`: the calling thread's browser, launched lazily on
  first use and reused for every slide that thread measures. The engine
  (`app.engine.extract.html.measuring_page`) asks for it here and never launches one itself.
* `BrowserPool`: N engine threads, each with its own browser. `submit(fn)` runs `fn` on one of
  them and returns a future, so up to N exports measure in parallel while the event loop stays free.
  `shutdown()` closes each thread's browser on that thread. Size N to memory (one Chromium each,
  `SLIDE_ENGINE_BROWSER_POOL_SIZE`).

Launch options come from `app.config.engine`: the channel (`chromium`, Playwright's bundled build, in
the container) and two container-safe flags, `--disable-dev-shm-usage` (Chromium uses /tmp instead of
the container's tiny /dev/shm) and `--no-sandbox` (the setuid sandbox cannot start as root in a
container). Both are harmless on a workstation.

Ported from Slide Studio `engine/extract/html.py` (`shared_browser`) and `server/engine_bridge.py`
(`_Worker`), migration plan §4.1/§4.2.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any, TypeVar, cast

from app.config import engine as engine_config

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Playwright

_log = logging.getLogger(__name__)

T = TypeVar("T")

#: Chromium flags every launch gets (see the module docstring).
LAUNCH_ARGS: tuple[str, ...] = ("--no-sandbox", "--disable-dev-shm-usage")


class _ThreadState(threading.local):
    playwright: Playwright | None = None
    browser: Browser | None = None


_state = _ThreadState()


def launch_kwargs(channel: str | None = None) -> dict[str, Any]:
    """Keyword arguments for `playwright.chromium.launch`, from the engine settings."""
    chosen = (engine_config.EDGE_CHANNEL if channel is None else channel).strip()
    bundled = not chosen or chosen.lower() == "chromium"
    kwargs: dict[str, Any] = {"args": list(LAUNCH_ARGS)}
    if not bundled:
        # An installed channel ("msedge"). Not installed fails at launch rather than falling back:
        # silently measuring in a different engine would move every line box.
        kwargs["channel"] = chosen
    return kwargs


def thread_browser() -> Browser:
    """The calling thread's browser, launched on first use and relaunched if it disconnected."""
    browser = _state.browser
    if browser is None or not browser.is_connected():
        if _state.playwright is None:
            from playwright.sync_api import sync_playwright

            _state.playwright = sync_playwright().start()
        _state.browser = _state.playwright.chromium.launch(**launch_kwargs())
        _log.info("browser: launched on thread %s", threading.current_thread().name)
    return cast("Browser", _state.browser)


def has_thread_browser() -> bool:
    """Whether the calling thread currently holds a browser."""
    return _state.browser is not None


def close_thread_browser() -> None:
    """Close the calling thread's browser and Playwright, if it has them. Safe to call twice."""
    browser, playwright = _state.browser, _state.playwright
    _state.browser, _state.playwright = None, None
    try:
        if browser is not None:
            browser.close()
    finally:
        if playwright is not None:
            playwright.stop()


_STOP = object()


class BrowserPool:
    """N engine threads, each owning one browser; engine work runs on them.

    A job is any callable; inside it, `thread_browser()` is that worker's browser, so the engine's
    own calls (`extract_html`, `export_deck`) need no browser argument. Jobs are taken in order by
    whichever thread is free. An exception in a job is set on its future and never kills a thread.
    """

    def __init__(self, size: int | None = None, *, name: str = "engine") -> None:
        count = size if size is not None else engine_config.engine_settings.browser_pool_size
        self.size = max(1, int(count))
        self._jobs: queue.Queue[Any] = queue.Queue()
        self._closed = False
        self._lock = threading.Lock()
        self._threads = [
            threading.Thread(target=self._loop, name=f"{name}-{index}", daemon=True)
            for index in range(self.size)
        ]
        for thread in self._threads:
            thread.start()

    def _loop(self) -> None:
        try:
            while True:
                item = self._jobs.get()
                if item is _STOP:
                    return
                work, future = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(work())
                except BaseException as exc:  # handed to the caller; the thread lives on
                    future.set_exception(exc)
        finally:
            try:
                close_thread_browser()
            except Exception:  # a browser that will not close must not hide the shutdown
                _log.warning("browser: close failed on %s", threading.current_thread().name, exc_info=True)

    def submit(self, work: Callable[[], T]) -> Future[T]:
        """Run `work()` on an engine thread; its result (or exception) arrives on the future."""
        with self._lock:
            if self._closed:
                raise RuntimeError("the browser pool is shut down")
            future: Future[T] = Future()
            self._jobs.put((work, future))
            return future

    def run(self, work: Callable[[], T], timeout: float | None = None) -> T:
        """`submit(work)` and wait for the result."""
        return self.submit(work).result(timeout=timeout)

    def shutdown(self, timeout: float | None = 30.0) -> None:
        """Stop taking work, let the queued jobs finish, and close every thread's browser."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for _ in self._threads:
                self._jobs.put(_STOP)
        for thread in self._threads:
            thread.join(timeout)

    def __enter__(self) -> BrowserPool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()
