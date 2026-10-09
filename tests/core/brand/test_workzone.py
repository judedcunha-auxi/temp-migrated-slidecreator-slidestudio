"""core/brand/workzone: the content rectangle, its header band, and the instruction that confines a design."""

from __future__ import annotations

import math

import pytest

from app.core.brand.workzone import (
    Workzone,
    container_style,
    design_note,
    is_degenerate,
    normalize_bounds,
    should_tile,
)


def test_normalize_accepts_width_height() -> None:
    assert normalize_bounds({"left": 0.1, "top": 0.2, "width": 0.8, "height": 0.6}) == Workzone(0.1, 0.2, 0.8, 0.6)


def test_normalize_converts_right_bottom() -> None:
    z = normalize_bounds({"left": 0.1, "top": 0.2, "right": 0.9, "bottom": 0.8})
    assert z is not None
    assert math.isclose(z.width, 0.8) and math.isclose(z.height, 0.6)


def test_normalize_is_idempotent_on_its_own_output() -> None:
    z = Workzone(0.05, 0.15, 0.9, 0.7)
    assert normalize_bounds(z.to_json()) == z


@pytest.mark.parametrize("raw", [
    None, "0.1", [], {}, {"left": 0.1}, {"left": 0.1, "top": 0.2},
    {"left": 0.1, "top": 0.2, "width": 0, "height": 0.5},
    {"left": 0.1, "top": 0.2, "right": 0.05, "bottom": 0.8},
    {"left": "x", "top": 0.2, "width": 0.5, "height": 0.5},
    {"left": float("nan"), "top": 0.2, "width": 0.5, "height": 0.5},
    {"left": 0.1, "top": 0.2, "width": float("inf"), "height": 0.5},
    {"left": True, "top": 0.2, "width": 0.5, "height": 0.5},
])
def test_normalize_rejects_unusable_bounds(raw: object) -> None:
    assert normalize_bounds(raw) is None


def test_degenerate_zones() -> None:
    assert not is_degenerate(Workzone(0.05, 0.15, 0.9, 0.7))
    assert is_degenerate(Workzone(0.0, 0.0, 0.9, 0.05))  # a sliver: aspect past 3:1
    assert is_degenerate(Workzone(0.0, 0.0, 0.05, 0.9))  # a pillar
    assert is_degenerate(Workzone(0.4, 0.4, 0.15, 0.15))  # under 3% of the slide
    assert is_degenerate(Workzone(0.1, 0.1, 0.0, 0.5))


def test_should_tile_needs_content_a_usable_zone_and_furniture() -> None:
    good = Workzone(0.05, 0.15, 0.9, 0.7)
    assert should_tile("content", good, ["cover", "content"])
    assert not should_tile("cover", good, ["cover", "content"])
    assert not should_tile("divider", good, ["divider"])
    assert not should_tile("content", None, ["content"])
    assert not should_tile("content", Workzone(0.4, 0.4, 0.1, 0.1), ["content"])
    assert not should_tile("content", good, [])
    assert not should_tile("content", good, None)


def test_px_rect_and_header_band() -> None:
    z = Workzone(0.05, 0.2, 0.9, 0.7)
    assert z.px_rect(1280, 720) == (64, 144, 1152, 504)
    assert z.header_band_px(1280, 720) == (0, 0, 1280, 144)
    assert Workzone(0.05, 0.0, 0.9, 0.7).header_band_px(1280, 720) is None


def test_design_note_names_the_rectangle_and_the_header_band() -> None:
    note = design_note(Workzone(0.05, 0.2, 0.9, 0.7), 1280, 720)
    assert "left=64px, top=144px, width=1152px, height=504px" in note
    assert "1280x720" in note
    assert "down to 144px" in note and "header" in note


def test_design_note_without_a_band_says_nothing_about_one() -> None:
    note = design_note(Workzone(0.05, 0.0, 0.9, 0.9), 1280, 720)
    assert "down to" not in note


def test_container_style_pins_the_workzone() -> None:
    assert container_style(Workzone(0.05, 0.2, 0.9, 0.7), 1280, 720) == \
        "position:absolute;left:64px;top:144px;width:1152px;height:504px"
