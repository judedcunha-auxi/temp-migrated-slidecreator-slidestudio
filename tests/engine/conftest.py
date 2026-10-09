"""Shared engine-test fixtures.

* `browser` / `page`: the calling thread's Chromium (`app.core.browser_pool`). Launching costs about
  2 s, so one browser serves the session; it is closed when the engine tests end.
* `workspace`: a scratch folder for one test (derived images go under it; nothing is global).
* The synthetic sample project (`sample_dir`, `sample_manifest`, ...), generated once per session
  by `tests/engine/sample_project.py`. It stands in for Slide Studio's client fixture, which is not
  ported (plan R6): every master and image here is synthetic.
* `ir_of(html)`: HTML -> IR in one call, with the sample project as the default context.
* `pptx_renderer`: the configured PptxRender client, or a skip.

Markers (registered in pyproject.toml):

* `renderer`: needs PptxRender's `/render-layouts` (`SLIDE_ENGINE_RENDERER_URL`); skipped unset.
* `renderer_full`: needs `/render` and `/verify` too (a local PptxRender build listed in
  `SLIDE_ENGINE_RENDERER_ENDPOINTS`); the deployed service has neither, so these skip there.
* `validator`: needs the OOXML validator (`SLIDE_ENGINE_VALIDATE_PY`).

A skipped test is reported with its reason (`pytest -rs`). Repo inputs (fixtures, masters) are never
skipped: a missing one fails.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from app.config import engine as config
from app.core.browser_pool import close_thread_browser, thread_browser
from app.engine.extract.html import extract_html
from app.engine.ir import IR
from app.engine.manifest import Manifest
from app.engine.renderer import PptxRenderClient
from tests.engine import helpers, sample_project

ENGINE_TESTS = Path(__file__).resolve().parent
FIXTURES = ENGINE_TESTS / "fixtures"


def _endpoints() -> frozenset[str]:
    return config.engine_settings.endpoints if config.RENDERER_URL else frozenset()


_EXTERNAL_TOOLS: dict[str, tuple[Callable[[], bool], str]] = {
    "renderer": (
        lambda: "render-layouts" not in _endpoints(),
        "needs PptxRender: set SLIDE_ENGINE_RENDERER_URL to run it",
    ),
    "renderer_full": (
        lambda: not {"render", "verify"} <= _endpoints(),
        "needs a PptxRender build with /render and /verify: set SLIDE_ENGINE_RENDERER_URL and list "
        "them in SLIDE_ENGINE_RENDERER_ENDPOINTS (the deployed service has neither)",
    ),
    "validator": (
        lambda: not config.validate_py().exists(),
        "needs the OOXML validator: set SLIDE_ENGINE_VALIDATE_PY to run it",
    ),
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip `renderer` / `renderer_full` / `validator` tests when their tool is not configured."""
    for marker, (missing, reason) in _EXTERNAL_TOOLS.items():
        if not missing():
            continue
        skip = pytest.mark.skip(reason=reason)
        for item in items:
            if marker in item.keywords:
                item.add_marker(skip)


# ------------------------------------------------------------------------------------ paths


@pytest.fixture(scope="session")
def torture_dir() -> Path:
    return config.TORTURE_FIXTURE


@pytest.fixture(scope="session")
def masters_dir() -> Path:
    return config.MASTERS_FIXTURE


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    """A scratch directory for one test."""
    return tmp_path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """The per-test engine workspace (derived images land under it)."""
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture(scope="session")
def session_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A workspace for session-scoped extractions."""
    return tmp_path_factory.mktemp("workspace")


@pytest.fixture(autouse=True)
def _engine_workspace(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Point `tests.engine.helpers` at a workspace for this test (session fixtures share one)."""
    helpers.set_workspace(tmp_path_factory.mktemp("ws", numbered=True))
    yield
    helpers.set_workspace(None)


# --------------------------------------------------------------------------- sample project


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The synthetic sample project, generated once per session."""
    return sample_project.build(tmp_path_factory.mktemp("sample"))


@pytest.fixture(scope="session")
def sample_master(sample_dir: Path) -> Path:
    return sample_dir / "master.pptx"


@pytest.fixture(scope="session")
def sample_manifest(sample_dir: Path) -> Manifest:
    return Manifest.load(sample_dir / "manifest.json")


@pytest.fixture(scope="session")
def sample_project_data(sample_dir: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((sample_dir / "project.json").read_text(encoding="utf-8"))
    return data


@pytest.fixture(scope="session")
def sample_slides(sample_dir: Path) -> list[Path]:
    slides = sorted(sample_dir.glob("slide-*.html"))
    assert slides, f"no slide HTML in {sample_dir}"
    return slides


@pytest.fixture(scope="session")
def sample_assets(sample_dir: Path) -> Path:
    return sample_dir / "assets"


# ---------------------------------------------------------------------------------- browser


@pytest.fixture(scope="session")
def browser() -> Iterator[Any]:
    yield thread_browser()
    close_thread_browser()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> Any:
    """Close this thread's browser when the engine tests end, not when the session does.

    Playwright's sync API allows one instance per thread; while one is alive it leaves its event
    loop set as the thread's running loop, which breaks any later async test on the same thread.
    """
    try:
        return (yield)
    finally:
        if nextitem is None or ENGINE_TESTS not in Path(nextitem.path).parents:
            close_thread_browser()


@pytest.fixture
def page(browser: Any) -> Iterator[Any]:
    """A fresh page per test, at 1280x720 and device scale 1 (override the viewport in the test)."""
    context = browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1)
    new_page = context.new_page()
    yield new_page
    new_page.close()
    context.close()


# --------------------------------------------------------------------------------------- IR


@pytest.fixture
def ir_of(sample_manifest: Manifest, sample_dir: Path, workspace: Path) -> Callable[..., IR]:
    """`ir_of(html, manifest=None, layout_id=None)` -> IR, with the sample project as the context."""

    def build(html: Path, manifest: Manifest | None = None, layout_id: str | None = None) -> IR:
        html = Path(html)
        used = manifest or sample_manifest
        return extract_html(
            html,
            used,
            layout_id or used.layouts[0].id,
            sample_dir / "assets",
            workspace=workspace,
            slide_id=html.stem,
            title=html.stem,
        )

    return build


# ---------------------------------------------------------------------------------- renderer


@pytest.fixture(scope="session")
def pptx_renderer() -> PptxRenderClient:
    """The configured PptxRender client (tests that use it carry a `renderer*` marker)."""
    client = PptxRenderClient.from_settings()
    if client is None:
        pytest.skip("needs PptxRender: set SLIDE_ENGINE_RENDERER_URL to run it")
    return client
