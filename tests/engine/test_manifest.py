"""The manifest round-trips, validates, and refuses ambiguous joins.

Real client masters are the adversarial case the schema was designed around: several masters,
layouts that share one name, and masters whose relationship order is the reverse of their id-list
order. Every assertion here is about not confusing one layout with another. The masters are synthetic
(the sample project, and decks built here with python-pptx); the pre-v2 legacy conversion
(`from_legacy`) was dropped in the port (decision D14).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import engine as config
from app.engine.manifest import Manifest, ManifestError, Placeholder, sha256_of


def test_fixture_manifest_round_trips_and_validates(sample_dir: Path, sample_manifest: Manifest, tmp_path: Path):
    assert sample_manifest.validate(project_dir=sample_dir) == []

    saved = sample_manifest.save(tmp_path / "manifest.json")
    reloaded = Manifest.load(saved)
    assert reloaded.to_json() == sample_manifest.to_json()
    assert reloaded.dumps() == sample_manifest.dumps()


def test_fixture_manifest_carries_part_names_for_every_layout(sample_manifest: Manifest):
    """WP0a filled `partName` from the package; without it no renderer join is safe."""
    assert sample_manifest.has_part_names
    parts = [lay.partName for lay in sample_manifest.layouts]
    assert len(set(parts)) == len(parts) == 11
    assert sample_manifest.layout("layout-05").partName == "/ppt/slideLayouts/slideLayout5.xml"


def test_layout_lookup_by_id_part_and_position(sample_manifest: Manifest):
    by_id = sample_manifest.layout("layout-11")
    assert sample_manifest.layout_by_part(by_id.partName) is by_id
    assert sample_manifest.layout_at(by_id.masterIndex, by_id.layoutIndex) is by_id

    with pytest.raises(ManifestError, match="no layout 'layout-99'"):
        sample_manifest.layout("layout-99")
    with pytest.raises(ManifestError):
        sample_manifest.layout_by_part("/ppt/slideLayouts/slideLayout99.xml")


def test_repeated_layout_names_are_distinguished_only_by_part(tmp_path: Path):
    """Layout names repeat on real masters: the reason every join goes through partName.

    A synthetic master gives four layouts exactly the same name and two more near-misses.
    """
    from pptx import Presentation

    from app.engine.importer import import_master

    prs = Presentation()
    names = {0: "Title only", 4: "Title only", 7: "Title only", 9: "Title only",
             5: "1_Title only", 6: "Title only with tracker"}
    for index, name in names.items():
        prs.slide_layouts[index].name = name
    master = tmp_path / "repeated-names.pptx"
    prs.save(str(master))
    manifest = import_master(master, tmp_path / "import", render=False)

    same_name = [lay for lay in manifest.layouts if lay.name == "Title only"]
    assert [lay.id for lay in same_name] == ["layout-01", "layout-05", "layout-08", "layout-10"]
    assert len({lay.partName for lay in same_name}) == 4
    for layout in same_name:
        assert manifest.layout_by_part(layout.partName) is layout

    similar = [lay for lay in manifest.layouts if "Title only" in lay.name]
    assert len(similar) == 6


def test_validate_reports_duplicates_and_canvas_mismatch(sample_manifest: Manifest):
    clone = Manifest.from_json(json.loads(json.dumps(sample_manifest.to_json())))
    clone.layouts[1].id = clone.layouts[0].id
    clone.layouts[2].partName = clone.layouts[0].partName
    clone.layouts[3].masterIndex, clone.layouts[3].layoutIndex = (
        clone.layouts[0].masterIndex, clone.layouts[0].layoutIndex,
    )
    problems = clone.validate()
    assert any("duplicate layout ids" in p for p in problems)
    assert any("duplicate layout partNames" in p for p in problems)
    assert any("duplicate (masterIndex, layoutIndex)" in p for p in problems)

    wrong_canvas = Manifest.from_json(json.loads(json.dumps(sample_manifest.to_json())))
    wrong_canvas.canvas["w"] = 960
    assert any("does not match the slide size" in p for p in wrong_canvas.validate())


def test_validate_requires_part_names_for_a_deterministic_import(sample_manifest: Manifest):
    clone = Manifest.from_json(json.loads(json.dumps(sample_manifest.to_json())))
    clone.importer = "deterministic"
    for layout in clone.layouts:
        layout.partName = None
    assert any("must record partName" in p for p in clone.validate())


def test_validate_reports_a_missing_background(sample_manifest: Manifest, tmp_path: Path):
    clone = Manifest.from_json(json.loads(json.dumps(sample_manifest.to_json())))
    assert any("is missing" in p for p in clone.validate(project_dir=tmp_path))


def test_placeholder_matching_follows_the_authoring_contract():
    assert Placeholder(type="ctrTitle").matches("title")
    assert Placeholder(type="subTitle").matches("subtitle")
    assert Placeholder(type="obj").matches("body")
    assert not Placeholder(type="sldNum").matches("body")


def test_masked_boxes_are_the_gate_masked_placeholder_types():
    from app.engine.manifest import Layout

    layout = Layout(
        id="l", name="l",
        placeholders=[
            Placeholder(type="sldNum", x=1000, y=690, w=60, h=20),
            Placeholder(type="title", x=64, y=28, w=1152, h=62),
        ],
    )
    boxes = layout.masked_boxes()
    assert len(boxes) == 1 and boxes[0].x == 1000
    assert config.GATE_MASK_PLACEHOLDERS == ("sldNum", "dt", "ftr")


def test_theme_of_falls_back_to_the_deck_theme(sample_manifest: Manifest):
    layout = sample_manifest.layout("layout-05")
    assert sample_manifest.theme_of(layout)["fonts"]["minor"] == "Arial"


def test_source_hash_identifies_the_master(sample_master: Path, sample_manifest: Manifest):
    assert sample_manifest.sourceHash == sha256_of(sample_master)


def test_load_refuses_a_file_that_is_not_a_v2_manifest(sample_dir: Path):
    """The legacy `project.json` master block is no longer converted (D14): loading it is an error."""
    with pytest.raises(ManifestError, match="not a v2 manifest"):
        Manifest.load(sample_dir / "project.json")
