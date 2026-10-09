"""Build the image assets the torture slides reference.

    python tests/engine/fixtures/torture/make_assets.py

Every asset is drawn from code rather than copied from a photo, for three reasons: the bytes are
deterministic (the same run produces the same file, so a re-generation is not a spurious diff), the
patterns are *legible under cropping* (a quadrant grid with corner markers shows immediately whether
`object-fit: cover` cropped the right edges), and nothing here carries a licence.

Sizes are chosen so the fit maths is checkable by hand: 400x300 is 4:3 landscape, 300x400 is 3:4
portrait, 160x160 is square. Dropping any of them into a 220x140 box therefore exercises a different
crop direction (`images.html`).

Owned by WP0b together with the rest of `fixtures/torture/`.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"

#: Quadrant colours, clockwise from top-left. Distinct hues so a crop is identifiable at a glance.
QUADRANTS: tuple[str, str, str, str] = ("#1A9AFA", "#2DB757", "#F5A623", "#E2445C")
INK = "#2E2E38"
PAPER = "#FFFFFF"


def _quadrant_image(width: int, height: int) -> Image.Image:
    """A four-quadrant card with corner ticks, a centre cross and a border.

    The corner ticks are what make a crop readable: `object-fit: cover` on a landscape source in a
    portrait box eats the left and right ticks and keeps the top and bottom ones.
    """
    image = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(image)
    half_w, half_h = width // 2, height // 2
    draw.rectangle((0, 0, half_w, half_h), fill=QUADRANTS[0])
    draw.rectangle((half_w, 0, width, half_h), fill=QUADRANTS[1])
    draw.rectangle((half_w, half_h, width, height), fill=QUADRANTS[2])
    draw.rectangle((0, half_h, half_w, height), fill=QUADRANTS[3])

    tick = max(8, min(width, height) // 12)
    for x0, y0 in ((0, 0), (width - tick, 0), (0, height - tick), (width - tick, height - tick)):
        draw.rectangle((x0, y0, x0 + tick - 1, y0 + tick - 1), fill=INK)
    draw.line((0, half_h, width, half_h), fill=PAPER, width=3)
    draw.line((half_w, 0, half_w, height), fill=PAPER, width=3)
    draw.rectangle((0, 0, width - 1, height - 1), outline=INK, width=2)
    return image


def _logo(size: int) -> Image.Image:
    """A square RGBA mark with real transparency, for the circle/radius cases."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, size - 5, size - 5), fill=QUADRANTS[0])
    draw.ellipse((size * 0.28, size * 0.28, size * 0.72, size * 0.72), fill=PAPER)
    draw.rectangle((size * 0.46, 0, size * 0.54, size), fill=QUADRANTS[3])
    return image


ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96">
  <rect width="96" height="96" rx="12" fill="#2E2E38"/>
  <path d="M24 60 L44 32 L60 52 L72 36" fill="none" stroke="#1A9AFA" stroke-width="6"
        stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="72" cy="36" r="7" fill="#2DB757"/>
</svg>
"""


def build() -> list[Path]:
    """Write every asset and return the paths, sorted (a stable list for the manifest)."""
    ASSETS.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    landscape = ASSETS / "photo-400x300.png"
    _quadrant_image(400, 300).save(landscape, format="PNG", optimize=True)
    written.append(landscape)

    portrait = ASSETS / "photo-300x400.jpg"
    _quadrant_image(300, 400).save(portrait, format="JPEG", quality=92, optimize=True)
    written.append(portrait)

    logo = ASSETS / "logo-160x160.png"
    _logo(160).save(logo, format="PNG", optimize=True)
    written.append(logo)

    icon = ASSETS / "icon.svg"
    icon.write_text(ICON_SVG, encoding="utf-8")
    written.append(icon)

    return sorted(written)


if __name__ == "__main__":
    for path in build():
        print(f"{path.relative_to(HERE.parents[2])}  {path.stat().st_size} bytes")
