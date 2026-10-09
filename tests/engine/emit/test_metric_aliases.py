"""Calibri -> Carlito: a family that is not installed is measured in its metric-compatible stand-in.

The Linux container has no Microsoft fonts. It installs Carlito (metric-compatible with Calibri) and
Liberation (Arial, Times New Roman, Courier New); the engine must measure a Calibri run in Carlito,
not fall through to Arial, and must still *write* Calibri into the file. These tests build a fake
font index, so they run the same on every machine.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.engine.emit import text
from app.engine.verify import fit


def _face(family: str, weight: int = 400, italic: bool = False) -> text.Face:
    return text.Face(
        typographic_family=family, legacy_family=family, subfamily="Regular",
        path=Path(f"/fonts/{family.replace(' ', '')}.ttf"), index=0, weight=weight, italic=italic,
        bold_member=weight >= 700, width_variant=False, variable=False,
        ascent=0.75, descent=0.25, line_gap=0.0,
    )


@pytest.fixture
def linux_fonts(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[text.Face]]:
    """A font index like the container's: Carlito, Liberation and DejaVu; no Microsoft faces."""
    index: dict[str, list[text.Face]] = {}
    for family in ("Carlito", "Liberation Sans", "Liberation Serif", "Liberation Mono", "DejaVu Sans"):
        for weight in (400, 700):
            index.setdefault(family.lower(), []).append(_face(family, weight))
    text.forget_fonts()
    monkeypatch.setattr(text, "font_index", lambda: index)
    yield index
    # Before monkeypatch restores the real (cached) index: drop the lookups made against this one.
    text.face_for_exact.cache_clear()
    text.face_for.cache_clear()


def test_calibri_is_measured_in_carlito(linux_fonts: Any):
    assert text.face_for_exact("Calibri", 400, False) is None       # not installed
    face = text.face_for("Calibri", 400, False)
    assert face is not None and face.family == "Carlito"
    light = text.face_for("Calibri Light", 300, False)
    assert light is not None and light.family == "Carlito"
    bold = text.face_for("Calibri", 700, False)
    assert bold is not None and bold.family == "Carlito" and bold.weight == 700


@pytest.mark.parametrize(("family", "stand_in"), [
    ("Arial", "Liberation Sans"),
    ("Times New Roman", "Liberation Serif"),
    ("Courier New", "Liberation Mono"),
])
def test_other_microsoft_families_use_their_liberation_twins(linux_fonts: Any, family: str, stand_in: str):
    face = text.face_for(family, 400, False)
    assert face is not None and face.family == stand_in


def test_an_unknown_family_still_falls_back(linux_fonts: Any):
    face = text.face_for("Some Brand Sans", 400, False)
    assert face is not None and face.family == "Liberation Sans"   # the generic fallback order


def test_the_fit_predictor_measures_calibri_in_carlito(linux_fonts: Any):
    face = fit.resolve_face("Calibri", bold=False, italic=False)
    assert face is not None and face.family == "Carlito"
    assert fit.resolve_face("Calibri", bold=True, italic=False).weight == 700  # type: ignore[union-attr]


def test_the_file_still_names_calibri(linux_fonts: Any):
    ctx = SimpleNamespace(theme_fonts={}, options=SimpleNamespace(theme_fonts=False))
    emitted = text.emitted_face({"font": "Calibri", "weight": 400, "italic": False}, ctx)  # type: ignore[arg-type]
    assert emitted.typeface == "Calibri"
    assert emitted.face is None                                     # measured elsewhere, named as asked


def test_the_alias_table_covers_the_container_fonts():
    assert text.METRIC_ALIASES["calibri"] == ("Carlito",)
    assert "Carlito" in text._FALLBACK_FAMILIES
