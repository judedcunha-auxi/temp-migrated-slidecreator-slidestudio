"""app/core/darwin/compose.py: the tile knock-out, the composite, the wireframe and the PDF writer."""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader

from app.core.brand.workzone import Workzone
from app.core.darwin import compose


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _tile() -> bytes:
    """White background; a dark panel holding an enclosed white box; a light-grey panel touching the edge;
    an anti-aliased-looking whitish ring around the dark panel."""
    img = Image.new("RGB", (60, 40), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle((9, 9, 31, 31), fill=(225, 225, 225))     # fringe ring (whitish, next to the background)
    draw.rectangle((10, 10, 30, 30), fill=(20, 40, 90))      # content
    draw.rectangle((16, 16, 24, 24), fill=(255, 255, 255))   # enclosed white: must stay opaque
    draw.rectangle((45, 0, 59, 39), fill=(242, 242, 242))    # light-grey panel at the edge: not background
    return _png(img)


def test_knock_out_clears_the_border_white_only() -> None:
    with Image.open(io.BytesIO(compose.knock_out_background(_tile()))) as out:
        px = out.convert("RGBA").load()
        assert px is not None
        assert px[0, 0][3] == 0 and px[40, 35][3] == 0          # background: transparent
        assert px[20, 20] == (255, 255, 255, 255)               # enclosed white: opaque
        assert px[15, 15][3] == 255                             # content: opaque
        assert px[50, 20][3] == 255                             # #F2F2F2 panel at the edge survives
        ring = px[9, 20]
        assert ring[3] == 255 - 225 and ring[:3] == (0, 0, 0)   # fringe: alpha from its lightness, un-blended


def test_knock_out_of_a_full_workzone_tile_is_quick_enough() -> None:
    img = Image.new("RGB", (2304, 1008), (255, 255, 255))
    ImageDraw.Draw(img).rectangle((200, 200, 2000, 800), fill=(10, 10, 10))
    out = compose.knock_out_background(_png(img))
    with Image.open(io.BytesIO(out)) as result:
        assert result.size == (2304, 1008) and result.getpixel((5, 5))[3] == 0


def test_composite_tile_is_full_slide_and_refuses_a_workzone_off_the_slide() -> None:
    master = _png(Image.new("RGB", (32, 18), (200, 0, 0)))
    tile = _png(Image.new("RGB", (100, 50), (0, 0, 200)))
    out = compose.composite_tile(master, tile, Workzone(0.1, 0.2, 0.5, 0.5))
    with Image.open(io.BytesIO(out)) as img:
        assert img.size == (2560, 1440)
        assert img.getpixel((10, 10))[:3] == (200, 0, 0)       # the master around the workzone
        assert img.getpixel((600, 600))[:3] == (0, 0, 200)     # the tile inside it
    with pytest.raises(ValueError):
        compose.composite_tile(master, tile, Workzone(0.6, 0.2, 0.5, 0.5))


def test_layout_wireframe() -> None:
    layout = {"name": "Title and Content", "placeholders": [
        {"type": "TITLE", "role": "title", "left": 0.05, "top": 0.05, "width": 0.9, "height": 0.12},
        {"type": "BODY", "left": 0.05, "top": 0.2, "width": 0.9, "height": 0.7},
        {"type": "DATE", "left": 0.05, "top": 0.9, "width": 0.2, "height": 0.05}],
        "decorations": [{"role": "logo", "left": 0.85, "top": 0.02, "width": 0.1, "height": 0.06},
                        {"role": "content_picture", "left": 0.6, "top": 0.6, "width": 0.2, "height": 0.2},
                        {"role": "background", "left": 0, "top": 0, "width": 1, "height": 1}]}
    decorations = [{"fillHex": "#336699", "fillType": "solid", "left": 0, "top": 0.95, "width": 1, "height": 0.05}]
    with Image.open(io.BytesIO(compose.layout_wireframe(layout, decorations, [{"left": 0.0, "top": 0.0,
                                                                              "width": 0.1, "height": 0.1}]))) as img:
        assert img.size == (2560, 1440) and img.getpixel((5, 1430)) != (255, 255, 255)
    dark = {"name": "Section", "backgroundType": "solid", "backgroundColor": "#112233",
            "placeholders": [{"type": "TITLE", "left": 0.1, "top": 0.4, "width": 0.8, "height": 0.2}]}
    with Image.open(io.BytesIO(compose.layout_wireframe(dark, None, None))) as img:
        assert img.getpixel((5, 5)) == (17, 34, 51)


def test_pdf_pages_are_the_pictures() -> None:
    rgb = _png(Image.new("RGB", (40, 30), (1, 2, 3)))
    rgba = _png(Image.new("RGBA", (20, 10), (1, 2, 3, 128)))
    gray = _png(Image.new("L", (8, 4), 77))
    reader = PdfReader(io.BytesIO(compose.pdf_from_pngs([rgb, rgba, gray])))
    assert [(float(p.mediabox.width), float(p.mediabox.height)) for p in reader.pages] == [(40, 30), (20, 10), (8, 4)]
    image = reader.pages[0].images[0].image
    assert image is not None and image.getpixel((0, 0)) == (1, 2, 3)  # lossless
    with pytest.raises(ValueError):
        compose.pdf_from_pngs([b"GIF89a"])
    with pytest.raises(ValueError):
        compose.pdf_from_pngs([])
