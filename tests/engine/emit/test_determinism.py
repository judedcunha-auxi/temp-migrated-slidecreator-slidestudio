"""Exports are deterministic: the same input twice gives a byte-identical `.pptx`.

Run end to end on the synthetic sample project (browser measurement, classification, emission
with a native chart, images and notes), into two different output folders and workspaces, so
nothing about where an export ran can leak into the file.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from app.engine.emit.pptx import FIXED_ZIP_DATE, emit
from app.engine.manifest import Manifest
from app.engine.pipeline import export_deck
from app.engine.reports import ExportOptions


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_same_project_exported_twice_is_byte_identical(sample_dir: Path, tmp_path: Path):
    first = export_deck(sample_dir, ExportOptions(out_dir=tmp_path / "a", workspace=tmp_path / "ws-a"))
    second = export_deck(sample_dir, ExportOptions(out_dir=tmp_path / "b", workspace=tmp_path / "ws-b"))

    assert first.pptx.name == second.pptx.name
    assert first.pptx.read_bytes() == second.pptx.read_bytes(), (
        f"{_sha256(first.pptx)[:12]} != {_sha256(second.pptx)[:12]}")
    # The deck is not trivially empty: both slides, with their notes and media, made it in.
    with zipfile.ZipFile(first.pptx) as archive:
        names = archive.namelist()
        assert sum(1 for n in names if n.startswith("ppt/slides/slide") and n.endswith(".xml")) == 2
        assert any(n.startswith("ppt/media/") for n in names)
        assert all(info.date_time == FIXED_ZIP_DATE for info in archive.infolist())


def test_rerun_hook_reproduces_the_export(sample_dir: Path, tmp_path: Path):
    result = export_deck(sample_dir, ExportOptions(out_dir=tmp_path / "first"))
    assert result.rerun is not None
    again = result.rerun(tmp_path / "again")
    assert _sha256(again.pptx) == _sha256(result.pptx)


def test_emitting_the_same_irs_twice_is_byte_identical(sample_dir: Path, tmp_path: Path):
    result = export_deck(sample_dir, ExportOptions(out_dir=tmp_path / "export"))
    manifest = Manifest.load(sample_dir / "manifest.json")
    one, two = tmp_path / "one.pptx", tmp_path / "two.pptx"
    emit(result.irs, manifest, sample_dir / "master.pptx", one)
    emit(result.irs, manifest, sample_dir / "master.pptx", two)
    assert one.read_bytes() == two.read_bytes()
