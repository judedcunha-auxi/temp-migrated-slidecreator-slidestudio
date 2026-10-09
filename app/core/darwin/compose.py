"""Pixel work for Darwin's image mode, on Pillow (Darwin used sharp and pdf-lib).

* `knock_out_background(png)`: `_shared/tileBackground.ts: knockOutTileBackground`. A content tile is
  rendered on white; before it is composited onto the brand master, the white connected to the tile's border
  becomes transparent, and the whitish fringe next to it gets a partial alpha with its colour un-blended from
  white. Enclosed white (text, panels) stays opaque. The flood fill is a scanline fill over a byte mask, so a
  workzone-sized tile takes a fraction of a second in pure Python.
* `composite_tile(master, tile, workzone)`: `image.ts: compositeIfTile`'s picture: the master stretched to
  2560x1440, the tile stretched to the workzone and knocked out, laid on top.
* `layout_wireframe(layout, kit)`: `_shared/layoutTemplate.ts: synthesizeLayoutTemplate`, the labelled-zone
  guide image the image model is conditioned on when a brand has extracted layouts but no master PNG. Drawn
  with ImageDraw instead of rasterising an SVG; the zones, colours and labels are Darwin's.
* `pdf_from_pngs(pngs)`: `pdf.ts` / `pdf-deck.ts`: one full-bleed page per PNG, the page in points equal to
  the picture in pixels, the pixels stored losslessly (Flate), as pdf-lib's `embedPng` did. Not a PNG: a
  ValueError (Darwin's embedPng threw, and the route answered 500).
"""

from __future__ import annotations

import io
import zlib
from collections.abc import Mapping, Sequence
from typing import Any

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFont

from app.core.brand.workzone import Workzone
from app.core.darwin.js import js_round

SLIDE_W, SLIDE_H = 2560, 1440

# tileBackground.ts thresholds
BG_MIN = 245       # min(r,g,b) at or above this = background candidate
FRINGE_MIN = 200   # whitish edge pixels down to this get partial alpha
MAX_CHROMA = 14    # max(r,g,b) - min(r,g,b): keeps pale tints opaque

_RESAMPLE = Image.Resampling.LANCZOS


def _mask(channel: Image.Image, test: Any) -> Image.Image:
    return channel.point([255 if test(v) else 0 for v in range(256)])


def _flood_from_border(bg: bytes, w: int, h: int) -> bytearray:
    """4-connected scanline fill of the 0xFF pixels of `bg` reachable from the border. Returns 0/1 per pixel."""
    n = w * h
    filled = bytearray(n)
    stack: list[int] = []
    for x in range(w):
        stack.append(x)
        stack.append((h - 1) * w + x)
    for y in range(h):
        stack.append(y * w)
        stack.append(y * w + w - 1)
    while stack:
        p = stack.pop()
        if filled[p] or not bg[p]:
            continue
        row = (p // w) * w
        left = bg.rfind(b"\x00", row, p) + 1 or row
        left = max(left, row)
        right = bg.find(b"\x00", p, row + w)
        right = row + w if right == -1 else right
        filled[left:right] = b"\x01" * (right - left)
        x0, x1 = left - row, right - row  # the run, [x0, x1)
        for nrow in (row - w, row + w):
            if nrow < 0 or nrow >= n:
                continue
            x = x0
            while x < x1:
                q = bg.find(b"\xff", nrow + x, nrow + x1)
                if q == -1:
                    break
                if not filled[q]:
                    stack.append(q)
                end = bg.find(b"\x00", q, nrow + w)
                x = (nrow + w if end == -1 else end) - nrow + 1
    return filled


def knock_out_background(png: bytes) -> bytes:
    """`knockOutTileBackground`: see the module docstring."""
    with Image.open(io.BytesIO(png)) as opened:
        img = opened.convert("RGBA")
    w, h = img.size
    r, g, b, a = img.split()
    lo = ImageChops.darker(ImageChops.darker(r, g), b)
    hi = ImageChops.lighter(ImageChops.lighter(r, g), b)
    chroma = ImageChops.subtract(hi, lo)
    bg = ImageChops.darker(_mask(lo, lambda v: v >= BG_MIN), _mask(chroma, lambda v: v <= MAX_CHROMA))
    fringe_class = ImageChops.subtract(
        ImageChops.darker(_mask(lo, lambda v: v >= FRINGE_MIN), _mask(chroma, lambda v: v <= MAX_CHROMA * 2)), bg)

    filled = _flood_from_border(bg.tobytes(), w, h)
    filled_img = Image.frombytes("L", (w, h), bytes(filled.translate(bytes([0, 255]) + bytes(254))))
    # 4-neighbour dilation of the fill: the fringe is a fringe-class pixel next to a filled one.
    near = Image.new("L", (w, h), 0)
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        shifted = Image.new("L", (w, h), 0)
        shifted.paste(filled_img, (dx, dy))
        near = ImageChops.lighter(near, shifted)
    fringe = ImageChops.darker(fringe_class, near).tobytes()

    alpha = bytearray(a.tobytes())
    alpha_bytes = bytes(filled)
    i = alpha_bytes.find(b"\x01")
    while i != -1:
        alpha[i] = 0
        i = alpha_bytes.find(b"\x01", i + 1)

    rgba = bytearray(img.tobytes())
    lo_bytes = lo.tobytes()
    i = fringe.find(b"\xff")
    while i != -1:
        a_frac = (255 - lo_bytes[i]) / 255
        if a_frac <= 0:
            alpha[i] = 0
        else:
            base = i * 4
            for c in range(3):
                v = js_round((rgba[base + c] - 255 * (1 - a_frac)) / a_frac)
                rgba[base + c] = 0 if v < 0 else 255 if v > 255 else v
            alpha[i] = js_round(a_frac * 255)
        i = fringe.find(b"\xff", i + 1)

    out = Image.frombytes("RGBA", (w, h), bytes(rgba))
    out.putalpha(Image.frombytes("L", (w, h), bytes(alpha)))
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def composite_tile(master_png: bytes, tile_png: bytes, workzone: Workzone) -> bytes:
    """The tile inside the workzone of the master, at 2560x1440. Raises when the tile would not fit (sharp
    refused to composite outside the base; the caller then serves the raw tile)."""
    x, y = js_round(workzone.left * SLIDE_W), js_round(workzone.top * SLIDE_H)
    tw, th = js_round(workzone.width * SLIDE_W), js_round(workzone.height * SLIDE_H)
    if x < 0 or y < 0 or tw <= 0 or th <= 0 or x + tw > SLIDE_W or y + th > SLIDE_H:
        raise ValueError("the workzone does not fit the slide")
    with Image.open(io.BytesIO(tile_png)) as tile_img:
        resized = tile_img.convert("RGBA").resize((tw, th), _RESAMPLE)
    buf = io.BytesIO()
    resized.save(buf, format="PNG")
    with Image.open(io.BytesIO(knock_out_background(buf.getvalue()))) as knocked:
        tile = knocked.convert("RGBA")
    with Image.open(io.BytesIO(master_png)) as master_img:
        base = master_img.convert("RGBA").resize((SLIDE_W, SLIDE_H), _RESAMPLE)
    base.alpha_composite(tile, (x, y))
    out = io.BytesIO()
    base.save(out, format="PNG")
    return out.getvalue()


# ------------------------------------------------------------------------------- the wireframe
_SKIP = frozenset({"DATE", "SLIDE_NUMBER", "FOOTER", "HEADER"})
_STYLE: dict[str, tuple[tuple[int, int, int, int], str, str]] = {
    "TITLE": ((30, 100, 200, 31), "#1E64C8", "TITLE"),
    "BODY": ((20, 160, 80, 31), "#14A050", "BODY"),
    "OBJECT": ((20, 160, 80, 31), "#14A050", "CONTENT"),
    "TEXT": ((20, 160, 80, 31), "#14A050", "TEXT"),
    "SUBTITLE": ((20, 160, 80, 31), "#14A050", "SUBTITLE"),
    "PICTURE": ((200, 100, 20, 31), "#C86414", "PICTURE"),
    "MEDIA": ((200, 100, 20, 31), "#C86414", "MEDIA"),
    "CHART": ((160, 80, 0, 31), "#A05000", "CHART"),
    "TABLE": ((150, 50, 200, 31), "#9632C8", "TABLE"),
}
_DEFAULT_STYLE = ((100, 100, 100, 26), "#646464", "")


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _box(d: Mapping[str, Any]) -> tuple[int, int, int, int]:
    return (js_round(_num(d.get("left")) * SLIDE_W), js_round(_num(d.get("top")) * SLIDE_H),
            js_round(_num(d.get("width")) * SLIDE_W), js_round(_num(d.get("height")) * SLIDE_H))


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    try:
        rgb = ImageColor.getrgb(color)
    except ValueError:
        rgb = (255, 255, 255)
    return (rgb[0], rgb[1], rgb[2], alpha)


def _rect(canvas: Image.Image, box: tuple[int, int, int, int], fill: tuple[int, int, int, int] | None,
          outline: tuple[int, int, int, int] | None, width: int, radius: int) -> Image.Image:
    x, y, w, h = box
    if w <= 0 or h <= 0:
        return canvas
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle((x, y, x + w, y + h), radius=radius, fill=fill, outline=outline,
                                            width=width)
    return Image.alpha_composite(canvas, layer)


def _label(canvas: Image.Image, box: tuple[int, int, int, int], label: str, size: int,
           fill: tuple[int, int, int, int]) -> Image.Image:
    x, y, w, h = box
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((x + js_round(w / 2), y + js_round(h / 2)), label, fill=fill, font=_font(size),
                               anchor="mm")
    return Image.alpha_composite(canvas, layer)


def _logo(canvas: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    size = max(16, min(32, js_round(box[3] * 0.3)))
    canvas = _rect(canvas, box, (0, 0, 0, 15), (153, 153, 153, 255), 2, 3)
    return _label(canvas, box, "LOGO", size, (102, 102, 102, 255))


def layout_wireframe(layout: Mapping[str, Any], master_decorations: Sequence[Any] | None,
                     logo_shapes: Sequence[Any] | None) -> bytes:
    """`synthesizeLayoutTemplate(layout, kit)`: the layout's zones as labelled rectangles (a PNG)."""
    solid = layout.get("backgroundType") == "solid" and layout.get("backgroundColor") is not None
    bg = str(layout.get("backgroundColor")) if solid and layout.get("backgroundColor") else "white"
    canvas = Image.new("RGBA", (SLIDE_W, SLIDE_H), _rgba(bg))
    for d in master_decorations or []:
        if not isinstance(d, Mapping) or not d.get("fillHex") or d.get("fillType") in ("none", "picture"):
            continue
        canvas = _rect(canvas, _box(d), _rgba(str(d.get("fillHex")), 153), None, 0, 2)
    for d in layout.get("decorations") or []:
        if not isinstance(d, Mapping):
            continue
        role = d.get("role") or ""
        if role in ("logo", "logo_mark"):
            canvas = _logo(canvas, _box(d))
        elif role == "background":
            continue
        elif role == "content_picture":
            canvas = _rect(canvas, _box(d), (200, 100, 20, 20), _rgba("#C86414"), 2, 3)
        else:
            canvas = _rect(canvas, _box(d), (100, 100, 100, 18), _rgba("#cccccc"), 1, 2)
    for logo in logo_shapes or []:
        if isinstance(logo, Mapping):
            canvas = _logo(canvas, _box(logo))
    for ph in layout.get("placeholders") or []:
        if not isinstance(ph, Mapping) or ph.get("type") in _SKIP:
            continue
        ptype = str(ph.get("type"))
        fill, stroke, label = _STYLE.get(ptype, (_DEFAULT_STYLE[0], _DEFAULT_STYLE[1], ptype))
        if ph.get("role"):
            label = f"{label} · {ph.get('role')}"
        box = _box(ph)
        size = max(28, min(72, js_round(box[3] * 0.15)))
        if solid:  # on a dark background the tints vanish: white outlines and labels instead
            canvas = _rect(canvas, box, (255, 255, 255, 38), (255, 255, 255, 230), 3, 6)
            canvas = _label(canvas, box, label, size, (255, 255, 255, 255))
        else:
            canvas = _rect(canvas, box, fill, _rgba(stroke), 3, 6)
            canvas = _label(canvas, box, label, size, _rgba(stroke, 204))
    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG")
    return out.getvalue()


# ---------------------------------------------------------------------------------------- PDF
def _image_objects(png: bytes) -> tuple[int, int, bytes, bytes | None, str]:
    """(width, height, Flate-compressed pixels, compressed alpha or None, colour space) of a PNG."""
    try:
        opened = Image.open(io.BytesIO(png))
    except OSError as exc:  # not an image at all
        raise ValueError("not a PNG") from exc
    with opened as img:
        if img.format != "PNG":
            raise ValueError("not a PNG")
        img.load()
        has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
        gray = img.mode in ("L", "LA", "1", "I", "I;16")
        rgb = img.convert("RGBA" if has_alpha else ("L" if gray else "RGB"))
    alpha: bytes | None = None
    if has_alpha:
        alpha = zlib.compress(rgb.getchannel("A").tobytes(), 6)
        rgb = rgb.convert("RGB")
    space = "/DeviceGray" if rgb.mode == "L" else "/DeviceRGB"
    return rgb.width, rgb.height, zlib.compress(rgb.tobytes(), 6), alpha, space


def pdf_from_pngs(pngs: Sequence[bytes]) -> bytes:
    """A PDF with one full-bleed page per PNG (see the module docstring). Raises ValueError for no pages or
    for bytes that are not a PNG."""
    if not pngs:
        raise ValueError("no pages")
    objects: list[bytes] = []  # objects[i] is object number i + 1

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    def stream(header: str, data: bytes) -> bytes:
        return f"<< {header} /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream"

    add(b"")  # 1: the catalog, written last
    add(b"")  # 2: the page tree, written last
    pages: list[int] = []
    for png in pngs:
        w, h, pixels, alpha, space = _image_objects(png)
        smask = ""
        if alpha is not None:
            mask_id = add(stream(f"/Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceGray "
                                 "/BitsPerComponent 8 /Filter /FlateDecode", alpha))
            smask = f" /SMask {mask_id} 0 R"
        image_id = add(stream(f"/Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace {space} "
                              f"/BitsPerComponent 8 /Filter /FlateDecode{smask}", pixels))
        content_id = add(stream("", f"q {w} 0 0 {h} 0 0 cm /Im0 Do Q".encode()))
        pages.append(add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] /Resources << /XObject "
                         f"<< /Im0 {image_id} 0 R >> >> /Contents {content_id} 0 R >>".encode()))
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{p} 0 R" for p in pages)
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode()

    out = io.BytesIO()
    out.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()
