"""Fixtures for the design loop: a deck on the synthetic master, measuring stubs, and a context.

A deck is a real `DesignDeck` with the synthetic 16:9 master really imported (python-pptx + the
engine importer; the layout backgrounds come from the offline `BlankLayoutsRenderer`, no network).
The model is always a fake from `tests/fakes/llm.py`. The measuring services are stubs by default
(the static export lint is real, the design review is empty, the preview is a flat PNG) so these
tests need no browser; the end-to-end tests in `test_e2e_*.py` use the real browser services.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from app.core import engine_service, preview
from app.core.design.context import DesignContext, DesignServices
from app.core.design.deck import DesignDeck
from app.engine.renderer import BlankLayoutsRenderer
from tests.conftest import build_ai_settings
from tests.fakes.llm import resolver

MASTERS = Path(__file__).resolve().parents[2] / "engine" / "fixtures" / "masters"
SYNTHETIC_MASTER = MASTERS / "test-16x9.pptx"


def png_bytes(size: tuple[int, int] = (1280, 720), colour: str = "white") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "PNG")
    return buf.getvalue()


def stub_services(review: list[dict[str, Any]] | None = None) -> DesignServices:
    return DesignServices(lint=engine_service.readiness, review=lambda _deck, _sid: list(review or []),
                          render=lambda _deck, _sid: png_bytes())


def make_deck(root: Path, name: str = "Test deck") -> DesignDeck:
    deck = DesignDeck.create(root / "deck", name)
    engine_service.import_master(deck, SYNTHETIC_MASTER, "test-16x9.pptx", renderer=BlankLayoutsRenderer())
    return deck


@pytest.fixture
def deck(tmp_path: Path) -> DesignDeck:
    return make_deck(tmp_path)


@pytest.fixture
def make_ctx() -> Callable[..., DesignContext]:
    def _make(deck: DesignDeck, provider: Any, services: DesignServices | None = None, **settings: Any) -> DesignContext:
        return DesignContext(deck=deck, settings=build_ai_settings(**settings), resolve=resolver(provider),
                             services=services or stub_services())
    return _make


def content_layout(deck: DesignDeck) -> str:
    layouts = deck.master().get("layouts") or []
    named = next((lay for lay in layouts if "content" in str(lay.get("name") or "").lower()), None)
    return str((named or layouts[0])["id"])


SLIDE_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;width:1280px;height:720px;overflow:hidden;background:transparent;font-family:Arial,sans-serif}}
h1{{position:absolute;left:64px;top:28px;width:1100px;margin:0;font:700 28px Arial;color:#1F2A44}}
.body{{position:absolute;left:64px;top:{top}px;width:1100px;height:300px}}
p{{margin:0 0 8px;font-size:16px;color:#222}}
</style></head><body><h1 data-placeholder="title">{title}</h1>
<div class="body"><p><b>Revenue grew 23%</b> in 2025 on volume.</p><p>Source: company filings</p></div>
</body></html>"""


def slide_html(title: str = "Revenue grew 23% on three markets", top: int = 150) -> str:
    return SLIDE_HTML.format(title=title, top=top)


@pytest.fixture(scope="module")
def browsers() -> Iterator[None]:
    """For browser tests: shut the preview and engine browsers down when the module is done."""
    yield
    preview.shutdown()
    engine_service.shutdown()
