"""Layout previews (app/core/render_layouts.py): an internal call to PptxRender through the Renderer port.

The HTTP client runs over a mock transport that answers like the deployed PptxRender, so the real
client code is exercised with no network.
"""

from __future__ import annotations

import io
import zipfile

import httpx2 as httpx
import pytest

from app.core.render_layouts import RenderLayoutsError, render_layouts
from app.engine.renderer import PptxRenderClient, layout_identities
from tests.core.design.conftest import SYNTHETIC_MASTER
from tests.fakes.renderer import pptxrender_transport


def client(transport: httpx.BaseTransport) -> PptxRenderClient:
    return PptxRenderClient("https://render.example", transport=transport)


def test_the_answer_is_the_zip_brand_setup_reads():
    seen: list[httpx.Request] = []
    data = render_layouts(SYNTHETIC_MASTER.read_bytes(), width=800,
                          renderer=client(pptxrender_transport(SYNTHETIC_MASTER, seen=seen)))
    names = sorted(zipfile.ZipFile(io.BytesIO(data)).namelist())
    assert len(names) == len(layout_identities(SYNTHETIC_MASTER)) == 11
    assert all(n.startswith("layout-") and n.endswith(".png") for n in names)
    assert seen and seen[0].url.path.endswith("/render-layouts")


def test_an_outage_is_unavailable_and_a_bad_deck_is_invalid():
    down = client(httpx.MockTransport(lambda _req: httpx.Response(503, text="busy")))
    with pytest.raises(RenderLayoutsError) as unavailable:
        render_layouts(SYNTHETIC_MASTER.read_bytes(), renderer=down)
    assert unavailable.value.kind == "unavailable"

    def refuse(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(RenderLayoutsError) as unreachable:
        render_layouts(SYNTHETIC_MASTER.read_bytes(), renderer=client(httpx.MockTransport(refuse)))
    assert unreachable.value.kind == "unavailable"

    with pytest.raises(RenderLayoutsError) as unconfigured:
        render_layouts(SYNTHETIC_MASTER.read_bytes(), renderer=None)
    assert unconfigured.value.kind == "unavailable"

    for data, width in ((b"not a zip", 1600), (b"PK\x03\x04junk", 1600), (SYNTHETIC_MASTER.read_bytes(), 10)):
        with pytest.raises(RenderLayoutsError) as invalid:
            render_layouts(data, renderer=client(pptxrender_transport(SYNTHETIC_MASTER)), width=width)
        assert invalid.value.kind == "invalid"


def test_a_count_mismatch_is_refused_not_mapped_silently():
    short = client(pptxrender_transport(SYNTHETIC_MASTER, drop=1))
    with pytest.raises(RenderLayoutsError) as err:
        render_layouts(SYNTHETIC_MASTER.read_bytes(), renderer=short)
    assert err.value.kind == "invalid"
