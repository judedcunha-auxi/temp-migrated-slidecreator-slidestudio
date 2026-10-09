"""`design_refs.notes`: colour roles and design notes derived from a master's manifest.

Ported from the notes tests in Slide Studio `server/tests/test_design_tools.py`. The client master's
inverted-theme test is replaced by one on the synthetic master (`conftest.py`) and one on a synthetic
inverted theme; the Office-default test masters are the repo's own synthetic ones.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.design_refs.notes import colour_roles, derive_notes
from tests.core.design_refs.conftest import manifest

MASTERS = Path(__file__).resolve().parents[2] / "engine" / "fixtures" / "masters"


def _fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((MASTERS / name).read_text(encoding="utf-8"))
    return data


def test_notes_for_the_synthetic_master():
    synthetic = manifest()
    roles = colour_roles(synthetic)
    assert roles["text"] == "#2B2B33" and roles["background"] == "#FFFFFF"
    assert roles["primary"] == "#2F80ED" and not roles["inverted"]
    notes = derive_notes(synthetic)
    assert "Canvas 1280×720 px (16:9)" in notes and "Arial throughout" in notes
    assert "margins 64px left, 64px right" in notes and "title zone y 28–90" in notes
    assert "titles 22pt Arial #2B2B33" in notes
    assert "(4:3)" not in notes and "ends by x" not in notes, "no narrow-zone warning on a 16:9 master"


def test_an_inverted_theme_is_read_from_the_title_colour_not_the_slot_names():
    inverted = manifest()
    for block in (inverted["theme"], inverted["masters"][0]["theme"]):
        block["colors"]["dk1"], block["colors"]["lt1"] = "#FFFFFF", "#2B2B33"
    roles = colour_roles(inverted)
    assert roles["text"] == "#2B2B33", "the colour the titles are set in, not dk1 (white on this theme)"
    assert roles["background"] == "#FFFFFF" and roles["inverted"]
    assert "slot names are inverted" in derive_notes(inverted)


def test_notes_for_a_default_template_master_find_the_footer_and_the_narrow_zones():
    notes = derive_notes(_fixture("test-16x9.manifest.json"))
    assert "Fonts: Calibri Light for titles, Calibri for text." in notes
    assert "Grid (from Title and Content): margins 48px left" in notes
    assert "Footer band from y 667" in notes
    assert "ends by x 912 of the 1280px canvas" in notes

    square = derive_notes(_fixture("test-4x3.manifest.json"))
    assert "(4:3)" in square and "ends by x" not in square
