"""A design deck packed as one blob and restored (app/core/design/bundle.py), defensively."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app.core.design import bundle
from app.core.design.deck import DesignDeck
from tests.core.design.conftest import content_layout, slide_html


def test_a_deck_round_trips(deck: DesignDeck, tmp_path: Path):
    sid = deck.save_slide(slide_html(), "Revenue", content_layout(deck), "seed")["id"]
    (deck.dir / ".import").mkdir(exist_ok=True)
    (deck.dir / ".import" / "scratch.bin").write_bytes(b"x")
    restored = bundle.unpack(bundle.pack(deck), tmp_path / "restored")
    assert restored.read_slide(sid) == deck.read_slide(sid)
    assert restored.master() == deck.master() and restored.master_path.read_bytes() == deck.master_path.read_bytes()
    assert not (restored.dir / ".import").exists(), "scratch folders stay out"


@pytest.mark.parametrize("name", ["../evil.txt", "/abs.txt", "C:/x.txt", "slides/../../x", "unexpected.txt"])
def test_unsafe_or_unexpected_entries_are_refused(tmp_path: Path, name: str):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("project.json", '{"id": "prj_1", "slides": []}')
        archive.writestr(name, "x")
    with pytest.raises(bundle.BundleError):
        bundle.unpack(buf.getvalue(), tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()


def test_a_blob_that_is_not_a_deck_is_refused(tmp_path: Path):
    with pytest.raises(bundle.BundleError):
        bundle.unpack(b"nope", tmp_path / "a")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("slides/x.html", "x")
    with pytest.raises(bundle.BundleError):
        bundle.unpack(buf.getvalue(), tmp_path / "b")
