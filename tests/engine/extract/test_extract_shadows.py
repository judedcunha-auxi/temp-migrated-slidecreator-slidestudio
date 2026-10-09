"""`box-shadow` layers the IR cannot carry are reported, once per element — never dropped silently.

The IR's `shadow` is one outer shadow, because that is what PowerPoint draws per shape. `page.js`
`shadowOf` keeps the first outer layer that paints (preferring one an offset or a blur makes
visible, since the IR has no spread), and `reportDroppedShadows` (called once from
`emitDecoration`) warns for every other painting layer: the outer layers after the first and any
inset layer the walk reached without rasterising the element (the body; an inset shadow anywhere
else makes the element a raster, which keeps it). A layer that paints nothing — a transparent
colour, or no offset, blur or positive spread, so it sits exactly under the box — is neither kept
nor counted: dropping it loses nothing. Plan §16 #19; `torture/boxes.expect.json` holds the same
rule for the torture gate.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.ir import IR, Element

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
html,body{{margin:0;width:1280px;height:720px;overflow:hidden;background:{body_background}}}
body{{box-shadow:{body_shadow}}}
.b{{position:absolute;box-sizing:border-box;width:160px;height:110px;background:#FFFFFF}}
</style></head><body>
{boxes}
</body></html>
"""


def _page(tmp_path: Path, boxes: dict[str, str], *, body_shadow: str = "none",
          body_background: str = "transparent") -> Path:
    """One slide with a 160 x 110 white box per `name: box-shadow`, 200 px apart."""
    divs = "\n".join(
        f'<div class="b" id="{name}" style="left:{40 + 200 * index}px;top:60px;box-shadow:{shadow}"></div>'
        for index, (name, shadow) in enumerate(boxes.items())
    )
    path = tmp_path / "shadows.html"
    path.write_text(PAGE.format(boxes=divs, body_shadow=body_shadow, body_background=body_background),
                    encoding="utf-8")
    return path


def _shape(ir: IR, name: str) -> Element:
    found = [e for e in ir.elements if e.kind == "shape" and e.source.get("path", "").endswith(f"div#{name}")]
    assert len(found) == 1, f"{name}: {[e.source for e in ir.elements]}"
    return found[0]


def _shadow_warns(ir: IR) -> list:
    """The dropped-layer warns (a raster's own `rasterised: inset box-shadow` warn is not one)."""
    return [d for d in ir.diagnostics if d.message.startswith("box-shadow:")]


def test_a_double_shadow_warns_once_and_a_single_one_does_not(ir_of, tmp_path: Path):
    """The torture `boxes` pair in isolation: two outer layers → one warn; one layer → nothing."""
    ir = ir_of(_page(tmp_path, {
        "double": "0 4px 10px rgba(46,46,56,.28), 0 12px 24px rgba(26,154,250,.22)",
        "single": "0 4px 10px rgba(46,46,56,.28)",
    }))
    double, single = _shape(ir, "double"), _shape(ir, "single")
    kept = {"color": "2E2E38", "alpha": 0.28, "dx": 0, "dy": 4, "blur": 10}
    assert double.shadow == kept, "the first (topmost) layer is the one kept"
    assert single.shadow == kept

    warns = _shadow_warns(ir)
    assert len(warns) == 1, [d.message for d in warns]
    warn = warns[0]
    assert warn.level == "warn"
    assert warn.message == ("box-shadow: 1 extra outer shadow dropped; "
                            "PowerPoint draws one outer shadow per shape")
    assert warn.source.endswith("div#double")
    assert warn.elementId == double.id, "the warn names the shape that carries the kept shadow"


def test_every_extra_layer_is_counted_in_one_warn(ir_of, tmp_path: Path):
    ir = ir_of(_page(tmp_path, {
        "triple": "0 1px 2px rgba(0,0,0,.2), 0 4px 8px rgba(0,0,0,.2), 0 12px 24px rgba(0,0,0,.2)",
    }))
    warns = _shadow_warns(ir)
    assert [d.message for d in warns] == [
        "box-shadow: 2 extra outer shadows dropped; PowerPoint draws one outer shadow per shape"
    ]
    assert _shape(ir, "triple").shadow == {"color": "000000", "alpha": 0.2, "dx": 0, "dy": 1, "blur": 2}


def test_layers_that_paint_nothing_are_neither_kept_nor_reported(ir_of, tmp_path: Path):
    """Tailwind writes `0 0 #0000` ring layers in front of its shadow: the first layer that paints is
    the one PowerPoint should draw, and the invisible ones are not a loss worth a warn."""
    ir = ir_of(_page(tmp_path, {
        # shadow-md: two transparent rings, then two real layers — one of those is really dropped
        "tw-md": "0 0 #0000, 0 0 #0000, 0 4px 6px -1px rgba(0,0,0,.1), 0 2px 4px -2px rgba(0,0,0,.1)",
        # a transparent ring and one real layer — nothing that paints is dropped
        "tw-one": "0 0 #0000, 0 1px 3px rgba(0,0,0,.1)",
        # opaque, but exactly under the box: no offset, no blur, no spread
        "under": "0 0 0 0 #FF0000, 0 1px 3px rgba(0,0,0,.1)",
    }))
    assert _shape(ir, "tw-md").shadow == {"color": "000000", "alpha": 0.1, "dx": 0, "dy": 4, "blur": 6}
    assert _shape(ir, "tw-one").shadow == {"color": "000000", "alpha": 0.1, "dx": 0, "dy": 1, "blur": 3}
    assert _shape(ir, "under").shadow == {"color": "000000", "alpha": 0.1, "dx": 0, "dy": 1, "blur": 3}
    warns = _shadow_warns(ir)
    assert [(d.source.split(" > ")[-1], d.message) for d in warns] == [
        ("div#tw-md", "box-shadow: 1 extra outer shadow dropped; PowerPoint draws one outer shadow per shape"),
    ]


def test_a_spread_ring_paints_and_counts_but_the_drawable_layer_is_kept(ir_of, tmp_path: Path):
    """`0 0 0 3px` has no offset and no blur, but its spread paints a ring outside the box. The IR
    has no spread, so kept it would sit exactly under the shape: the layer with an offset is kept,
    and the ring is the layer reported as dropped."""
    ir = ir_of(_page(tmp_path, {"ring": "0 0 0 3px #1A9AFA, 0 4px 10px rgba(0,0,0,.2)"}))
    assert _shape(ir, "ring").shadow == {"color": "000000", "alpha": 0.2, "dx": 0, "dy": 4, "blur": 10}
    assert [d.message for d in _shadow_warns(ir)] == [
        "box-shadow: 1 extra outer shadow dropped; PowerPoint draws one outer shadow per shape"
    ]


def test_an_inset_shadow_on_a_box_is_a_raster_not_a_dropped_layer(ir_of, tmp_path: Path):
    """The raster keeps the inset layer's pixels, so nothing is dropped and nothing is said about it."""
    ir = ir_of(_page(tmp_path, {"inset": "inset 0 2px 4px rgba(0,0,0,.4), 0 4px 10px rgba(0,0,0,.2)"}))
    rasters = [e for e in ir.elements if e.kind == "raster"]
    assert [r.reason for r in rasters] == ["inset box-shadow"]
    assert [d.message for d in ir.diagnostics] == ["rasterised: inset box-shadow"]
    assert _shadow_warns(ir) == []


def test_an_inset_layer_the_walk_drops_is_reported(ir_of, tmp_path: Path):
    """The body is decorated without the raster check: its outer layer is kept, the inset one warned."""
    ir = ir_of(_page(tmp_path, {}, body_background="#F5F5F7",
                     body_shadow="inset 0 0 0 6px #1A9AFA, 0 4px 10px rgba(0,0,0,.2)"))
    body = [e for e in ir.elements if e.kind == "shape" and e.source.get("path", "").startswith("body")
            and " > " not in e.source.get("path", "")]
    assert len(body) == 1
    assert body[0].shadow == {"color": "000000", "alpha": 0.2, "dx": 0, "dy": 4, "blur": 10}
    assert [d.message for d in _shadow_warns(ir)] == [
        "box-shadow: 1 inset shadow dropped; PowerPoint draws one outer shadow per shape"
    ]
    assert _shadow_warns(ir)[0].elementId == body[0].id


@pytest.mark.parametrize("shadow", ["none", "0 0 #0000", "0 0 0 0 #000000"])
def test_a_box_with_no_painting_shadow_carries_none(ir_of, tmp_path: Path, shadow: str):
    ir = ir_of(_page(tmp_path, {"plain": shadow}))
    assert _shape(ir, "plain").shadow is None
    assert _shadow_warns(ir) == []
