"""The visual vocabulary and the design notes derived from a master (`design_refs.vocabulary`, `.notes`).

Ported from Slide Studio `server/tests/test_design_vocabulary.py` (the design_refs half). The prompt-template
tests (format keys, budgets, the quality-bar wording) belong with the prompt (`tests/core/design/`).
"""
from __future__ import annotations

from tests.core.design_refs.conftest import manifest


def test_derived_notes_carry_the_design_tokens():
    from app.core.design_refs.notes import derive_notes, token_css

    synthetic = manifest()
    notes = derive_notes(synthetic)
    css = token_css(synthetic)
    assert css.startswith(":root{") and css.endswith("}") and css in notes
    for token in ("--m-left:64px", "--m-right:1216px", "--gutter:24px", "--col-w:74px", "--c4:358px",
                  "--e12:1216px", "--fs-title:29.33px", "--primary:#2F80ED", "--accent:#8E44AD", "--ink:#2B2B33"):
        assert token in css, token
    assert "column x start-end: 1 64-138, 2 162-236" in notes


def test_the_vocabulary_ports_every_slide_creator_entry():
    from app.core.design_refs import vocabulary as v

    assert len(v.SLIDE_TYPES) == 22
    canonical = {name for entries in v.FRAMEWORKS.values() for name in entries}
    assert len(canonical) == 65
    assert {v.canonical_framework(alias) for alias in v.ALIASES} <= canonical, "every alias resolves"
    assert v.canonical_framework("Bridge chart") == "waterfall chart"
    assert v.canonical_framework("Porter’s Five Forces.") == "Porter's five forces"
    assert v.canonical_framework("2×2") == "2x2 matrix"
    assert v.framework_hint("swot").startswith("four quadrant panels")
    assert v.canonical_framework("mind palace") is None


def test_the_vocabulary_names_the_framework_as_the_main_exhibit():
    from app.core.design_refs import vocabulary as v

    assert "one type and one framework per slide" not in v.VOCABULARY
    assert "its framework is the main exhibit" in v.VOCABULARY
    assert "is the main exhibit, composed with a side panel and one takeaway" in v.SLIDE_TYPES["framework"]
