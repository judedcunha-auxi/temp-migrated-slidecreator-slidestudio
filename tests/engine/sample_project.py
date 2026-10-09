"""Build the synthetic sample project the engine tests export end to end.

    python -m tests.engine.sample_project <target dir>     # by hand; the tests call build()

Committed sources live in `tests/engine/fixtures/sample/` (project.json, slide HTML). Everything
binary is generated here, deterministically, so no master or image is committed for it:

* `master.pptx`: the synthetic `masters/test-16x9.pptx` (python-pptx's default template at 16:9),
  with the theme's major and minor latin fonts set to Arial;
* `manifest.json` and `layouts/`: `import_master` with `BlankLayoutsRenderer` (white backgrounds,
  joined to the layouts by the same filename rule the PptxRender client uses);
* `assets/`: a logo and a cover photo drawn with Pillow.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw
from pptx import Presentation
from pptx.opc.constants import RELATIONSHIP_TYPE as RT

from app.engine.importer import import_master, ingest
from app.engine.renderer import BlankLayoutsRenderer

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SOURCES = FIXTURES / "sample"
BASE_MASTER = FIXTURES / "masters" / "test-16x9.pptx"
THEME_FONT = "Arial"


def make_master(target: Path, font: str = THEME_FONT) -> Path:
    """The sample master: the synthetic 16:9 test master with `font` as both theme fonts."""
    prs = Presentation(str(BASE_MASTER))
    theme = prs.slide_master.part.part_related_by(RT.THEME)
    xml = theme.blob.decode("utf-8")
    for slot in ("majorFont", "minorFont"):
        xml = re.sub(
            rf"(<a:{slot}>\s*<a:latin typeface=\")[^\"]*(\")",
            rf"\g<1>{font}\g<2>",
            xml,
        )
    theme._blob = xml.encode("utf-8")
    prs.core_properties.title = "Synthetic sample master"
    prs.core_properties.author = "slideforge-service tests"
    target.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(target))
    return target


def make_assets(assets: Path) -> None:
    """A square logo and a 3:2 cover photo, drawn rather than copied."""
    assets.mkdir(parents=True, exist_ok=True)
    logo = Image.new("RGB", (128, 128), "#0B2545")
    draw = ImageDraw.Draw(logo)
    draw.ellipse((16, 16, 112, 112), fill="#1A9AFA")
    draw.rectangle((52, 36, 76, 92), fill="#FFFFFF")
    logo.save(assets / "asset-logo.png")

    photo = Image.new("RGB", (900, 600), "#FFFFFF")
    draw = ImageDraw.Draw(photo)
    for band in range(12):
        shade = 40 + band * 15
        draw.rectangle((band * 75, 0, band * 75 + 75, 600), fill=(shade // 3, shade // 2, shade))
    draw.line((0, 600, 900, 0), fill="#F5C518", width=12)
    photo.save(assets / "asset-cover-photo.jpg", quality=90)


def build(target: Path) -> Path:
    """Write the whole sample project into `target` (created if needed) and return it."""
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    for name in ("project.json", "slide-01.html", "slide-02.html"):
        shutil.copyfile(SOURCES / name, target / name)
    master = make_master(target / "master.pptx")
    work = target / ".import"
    manifest = import_master(master, work, renderer=BlankLayoutsRenderer())
    ingest(target, manifest, work)
    shutil.rmtree(work, ignore_errors=True)
    make_assets(target / "assets")
    return target


if __name__ == "__main__":
    print(build(Path(sys.argv[1])))
