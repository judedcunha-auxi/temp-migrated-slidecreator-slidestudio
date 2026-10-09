"""Masters (app/core/masters.py): uploaded master import, the brand master, and keeping a master through the port.

No browser: import is python-pptx plus the `Renderer` port, here the fake renderer (white PNGs named
as PptxRender names them).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pptx import Presentation

from app.core import engine_service, masters
from app.core.brand import extract as brand_extract
from app.core.design.deck import DesignDeck
from app.core.slides.spec import BrandOptions
from app.core.storage.models import BrandCreate
from tests.core.design.conftest import SYNTHETIC_MASTER, png_bytes, slide_html
from tests.fakes.general_service import FakeGeneralService
from tests.fakes.renderer import FakeRenderer

FOUR_THREE = SYNTHETIC_MASTER.with_name("test-4x3.pptx")


def new_deck(tmp_path: Path) -> DesignDeck:
    return DesignDeck.create(tmp_path / "deck", "Deck")


def test_an_uploaded_master_is_imported_with_rendered_layouts(tmp_path: Path):
    deck = new_deck(tmp_path)
    renderer = FakeRenderer()
    block = masters.import_uploaded_master(deck, SYNTHETIC_MASTER.read_bytes(), "brand.pptx", renderer=renderer)
    assert block["status"] == "ready" and block["filename"] == "brand.pptx"
    assert len(block["layouts"]) == 11 and renderer.calls[0][0] == "render_layouts"
    for layout in block["layouts"]:
        assert (deck.layouts_dir / layout["background"]).is_file()
    assert deck.manifest_path.is_file() and deck.master_path.is_file()
    assert deck.canvas() == (1280, 720)


def test_a_reimport_rehomes_slides_whose_layout_is_gone_and_a_failure_keeps_the_old_master(tmp_path: Path):
    deck = new_deck(tmp_path)
    masters.import_uploaded_master(deck, SYNTHETIC_MASTER.read_bytes(), "a.pptx", renderer=FakeRenderer())
    sid = deck.save_slide(slide_html(), "Revenue", "layout-99", "seed")["id"]
    block = masters.import_uploaded_master(deck, FOUR_THREE.read_bytes(), "b.pptx", renderer=FakeRenderer())
    assert block["rehomed"] == [sid]
    slide = deck.find_slide(deck.meta(), sid)
    assert slide["current"] == 2 and slide["layoutId"] == deck.master()["layouts"][0]["id"]

    before = deck.master()
    broken = tmp_path / "broken.pptx"
    broken.write_bytes(b"PK\x03\x04")
    with pytest.raises(masters.MasterError):
        masters.import_uploaded_master(deck, broken.read_bytes(), "broken.pptx", renderer=FakeRenderer())
    assert deck.master() == before

    class Failing(FakeRenderer):
        def render_layouts(self, pptx: Path, width: int, out_dir: Path) -> dict[str, Path]:
            raise RuntimeError("renderer down")

    with pytest.raises(engine_service.MasterImportFailed):
        masters.import_uploaded_master(deck, SYNTHETIC_MASTER.read_bytes(), "c.pptx", renderer=Failing())
    assert deck.master()["filename"] == "b.pptx", "a failed import keeps the master the deck had"


@pytest.mark.parametrize(("data", "name"), [(b"", "m.pptx"), (b"not a zip", "m.pptx"), (b"PK\x03\x04", "m.pdf")])
def test_bad_uploads_are_refused(data: bytes, name: str):
    with pytest.raises(masters.MasterError):
        masters.check_master_bytes(data, name)


def furniture() -> dict[str, Any]:
    return dict(brand_extract.extract(SYNTHETIC_MASTER.read_bytes())["capturedFurniture"])


def test_the_brand_master_is_synthesised_debranded_or_plain(tmp_path: Path):
    branded = new_deck(tmp_path / "a")
    out = masters.prepare_brand_master(branded, furniture(), BrandOptions(), "content", renderer=FakeRenderer())
    assert out["mode"] == "branded"
    assert Presentation(str(branded.master_path)).slide_layouts.get_by_name("Content") is not None
    assert out["layoutId"] == masters.layout_for(branded, "content")

    debranded = new_deck(tmp_path / "b")
    out = masters.prepare_brand_master(debranded, furniture(), BrandOptions(apply_brand_layout=False), "cover",
                                       renderer=FakeRenderer())
    assert out["mode"] == "debranded"
    capture = masters.debrand_capture(furniture())
    assert all(blob["shapes"] == [] for blob in capture["layouts"].values())
    assert "theme" not in capture and "media" not in capture

    plain = new_deck(tmp_path / "c")
    assert masters.prepare_brand_master(plain, None, BrandOptions(), "content", renderer=None)["mode"] == "plain"
    assert plain.master()["status"] == "ready", "no renderer: imported without layout backgrounds"


def test_a_layout_template_that_is_not_an_image_is_refused(tmp_path: Path):
    deck = new_deck(tmp_path)
    with pytest.raises(masters.MasterError):
        masters.prepare_brand_master(deck, furniture(), BrandOptions(layout_template=b"nope"), "content",
                                     renderer=FakeRenderer())
    applied = masters.apply_template_background(
        masters.synthesize_master(masters.plain_capture(), tmp_path / "p.pptx"), png_bytes(), None)
    assert applied


@pytest.mark.asyncio
async def test_a_master_is_kept_through_the_storage_port(tmp_path: Path):
    store = FakeGeneralService()
    ctx, _ = await store.make_user("alice")
    brand = await store.brands.create(ctx, BrandCreate(name="Brand"))
    deck = new_deck(tmp_path)
    masters.import_uploaded_master(deck, SYNTHETIC_MASTER.read_bytes(), "brand.pptx", renderer=FakeRenderer())
    kept = await masters.persist_master(store, ctx, brand.id, deck, name="Brand master")
    assert kept.pptx_ref and len(kept.layout_refs) == 11
    assert kept.manifest["layouts"][0]["id"] == deck.master()["layouts"][0]["id"]
    stored = await store.masters.get_file(ctx, kept.id, "pptx")
    assert stored.data == deck.master_path.read_bytes()
    with pytest.raises(masters.MasterError):
        await masters.persist_master(store, ctx, brand.id, new_deck(tmp_path / "empty"), name="x")
