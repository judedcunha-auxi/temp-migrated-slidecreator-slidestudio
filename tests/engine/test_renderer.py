"""The renderer port: the PptxRender HTTP client against a fake of the deployed service, the
offline renderer, and the package reading the layout join depends on. No network."""

from __future__ import annotations

import json
from pathlib import Path

import httpx2 as httpx
import pytest

from app.config.engine import EngineSettings
from app.engine.renderer import (
    BlankLayoutsRenderer,
    PptxRenderClient,
    Renderer,
    RendererError,
    layout_identities,
    map_layouts_by_filename,
    slide_size_emu,
)
from tests.fakes.renderer import FakeRenderer, layout_zip, pptxrender_transport

MASTERS = Path(__file__).resolve().parent / "fixtures" / "masters"
MASTER_16X9 = MASTERS / "test-16x9.pptx"


def client_for(pptx: Path, **options: object) -> tuple[PptxRenderClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    transport = pptxrender_transport(pptx, seen=seen, **options)  # type: ignore[arg-type]
    return PptxRenderClient("https://render.example/", transport=transport), seen


def test_every_implementation_satisfies_the_protocol():
    assert isinstance(BlankLayoutsRenderer(), Renderer)
    assert isinstance(FakeRenderer(), Renderer)
    assert isinstance(PptxRenderClient("https://render.example"), Renderer)


def test_from_settings_is_none_without_a_url():
    assert PptxRenderClient.from_settings(EngineSettings(_env_file=None)) is None  # type: ignore[call-arg]
    client = PptxRenderClient.from_settings(EngineSettings(  # type: ignore[call-arg]
        _env_file=None, renderer_url="https://render.example", renderer_endpoints="render-layouts,render"))
    assert client is not None
    assert client.supports("render") and not client.supports("verify")


def test_a_non_http_url_is_refused():
    with pytest.raises(RendererError):
        PptxRenderClient("file:///etc/passwd")


def test_render_layouts_sends_width_only_and_joins_by_filename(tmp_path: Path):
    client, seen = client_for(MASTER_16X9)
    mapping = client.render_layouts(MASTER_16X9, 1280, tmp_path)

    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == "/render-layouts"
    body = request.read().decode("latin-1")
    assert 'name="width"' in body and "1280" in body
    assert 'name="names"' not in body                     # the deployed service has no names=true

    identities = layout_identities(MASTER_16X9)
    assert set(mapping) == {i.partName for i in identities}
    for identity in identities:
        assert mapping[identity.partName].name.startswith(f"layout-{identity.renderIndex:02d}-")
    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert [entry["partName"] for entry in index] == [i.partName for i in identities]
    assert json.loads((tmp_path / "warnings.json").read_text(encoding="utf-8"))["source"] == "pptxrender"


def test_a_missing_layout_png_is_refused_rather_than_joined_by_position(tmp_path: Path):
    client, _ = client_for(MASTER_16X9, drop=3)
    with pytest.raises(RendererError, match="cannot join them safely"):
        client.render_layouts(MASTER_16X9, 1280, tmp_path)


def test_an_index_json_when_present_is_cross_checked(tmp_path: Path):
    identities = layout_identities(MASTER_16X9)
    good = [{"file": f"layout-{i.renderIndex:02d}.png", **i.to_json()} for i in identities]
    client, _ = client_for(MASTER_16X9, extra={"index.json": json.dumps(good).encode()})
    assert len(client.render_layouts(MASTER_16X9, 1280, tmp_path / "ok")) == len(identities)

    bad = [dict(entry) for entry in good]
    bad[0]["partName"] = "/ppt/slideLayouts/slideLayout99.xml"
    client, _ = client_for(MASTER_16X9, extra={"index.json": json.dumps(bad).encode()})
    with pytest.raises(RendererError, match="identity mismatch"):
        client.render_layouts(MASTER_16X9, 1280, tmp_path / "bad")


def test_zip_members_cannot_escape_the_output_folder(tmp_path: Path):
    out = tmp_path / "out"
    client, _ = client_for(MASTER_16X9, extra={"../../evil.png": b"\x89PNG"})
    mapping = client.render_layouts(MASTER_16X9, 1280, out)
    assert all(png.parent == out for png in mapping.values())
    assert (out / "evil.png").exists()                    # flattened into out/, and not a layout
    assert not (tmp_path / "evil.png").exists()
    assert not (tmp_path.parent / "evil.png").exists()


def test_render_and_verify_are_refused_unless_the_service_lists_them(tmp_path: Path):
    client, seen = client_for(MASTER_16X9)
    with pytest.raises(RendererError, match="does not serve /render"):
        client.render(MASTER_16X9, 1280, tmp_path)
    reference = tmp_path / "ref.png"
    reference.write_bytes(b"")
    with pytest.raises(RendererError, match="does not serve /verify"):
        client.verify(MASTER_16X9, [reference], tmp_path)
    assert seen == []                                     # nothing was sent


def test_service_errors_and_timeouts_become_renderer_errors(tmp_path: Path):
    def fail(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"title": "Internal Server Error", "detail": "boom"})

    client, _ = client_for(MASTER_16X9, respond=fail)
    with pytest.raises(RendererError, match=r"failed \(500\): boom"):
        client.render_layouts(MASTER_16X9, 1280, tmp_path)

    def hang(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client, _ = client_for(MASTER_16X9, respond=hang)
    with pytest.raises(RendererError, match="did not answer"):
        client.render_layouts(MASTER_16X9, 1280, tmp_path)


def test_a_missing_deck_is_an_error(tmp_path: Path):
    client, seen = client_for(MASTER_16X9)
    with pytest.raises(RendererError, match="deck not found"):
        client.render_layouts(tmp_path / "missing.pptx", 1280, tmp_path)
    assert seen == []


def test_blank_layouts_renderer_matches_the_service_naming(tmp_path: Path):
    mapping = BlankLayoutsRenderer().render_layouts(MASTER_16X9, 640, tmp_path)
    identities = layout_identities(MASTER_16X9)
    assert set(mapping) == {i.partName for i in identities}
    from PIL import Image

    cx, cy = slide_size_emu(MASTER_16X9)
    with Image.open(next(iter(mapping.values()))) as image:
        assert image.size == (640, round(640 * cy / cx))
    with pytest.raises(RendererError):
        BlankLayoutsRenderer().render(MASTER_16X9, 640, tmp_path)


def test_map_layouts_by_filename_rejects_an_unexpected_name(tmp_path: Path):
    identities = layout_identities(MASTER_16X9)
    pngs = [tmp_path / f"slide-{n:02d}.png" for n in range(1, len(identities) + 1)]
    with pytest.raises(RendererError, match="unexpected layout filename"):
        map_layouts_by_filename(MASTER_16X9, pngs, identities, tmp_path)


def test_layout_zip_fake_matches_the_identities():
    import io
    import zipfile

    names = zipfile.ZipFile(io.BytesIO(layout_zip(MASTER_16X9, 320))).namelist()
    assert len(names) == len(layout_identities(MASTER_16X9))
