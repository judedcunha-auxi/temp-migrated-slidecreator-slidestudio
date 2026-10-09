"""`design_refs.workzone_lint`: content stays inside the brand's workzone and off the header band.

The known gap from Slide Studio (migration plan, Phase 3): a slide designed on a branded master ran past
the workzone and painted over the header band. These tests hold the two rules on hand-built IRs, on the
browser-free static fallback, and once end to end through `design_findings_html` in a real browser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.core.brand.workzone import Workzone
from app.core.design_refs import design_lint
from app.core.design_refs.workzone_lint import workzone_findings, workzone_findings_static
from app.engine.ir import IR, Box, Canvas, Element, Slide
from tests.core.design_refs.conftest import LAYOUT, manifest

#: x 64-1216, y 144-648 on the 1280x720 canvas; the header band is y 0-144.
ZONE = Workzone(left=0.05, top=0.2, width=0.9, height=0.7)


def _text(text: str, x: float, y: float, w: float, h: float, *, placeholder: str | None = None,
          eid: str = "e") -> Element:
    return Element(kind="text", box=Box(x, y, w, h), id=eid,
                   placeholder={"type": placeholder} if placeholder else None,
                   paragraphs=[{"lines": [{"runs": [{"text": text}]}]}])


def _shape(x: float, y: float, w: float, h: float, *, fill: dict[str, Any] | None = None,
           name: str | None = None, eid: str = "s") -> Element:
    return Element(kind="shape", box=Box(x, y, w, h), id=eid, name=name, fill=fill)


def _ir(*elements: Element) -> IR:
    return IR(canvas=Canvas(1280, 720), slide=Slide(id="s1", layoutId=LAYOUT), elements=list(elements))


SOLID = {"type": "solid", "color": "2F80ED", "alpha": 1.0}


def rules(findings: list[dict[str, Any]]) -> list[str]:
    return [f["rule"] for f in findings]


def test_content_inside_the_workzone_and_a_title_in_the_band_are_clean():
    ir = _ir(_text("Revenue grew 12% in 2025", 64, 40, 1152, 60, placeholder="title", eid="t"),
             _shape(64, 160, 560, 400, fill=SOLID, eid="card"),
             _text("Pricing drove half the growth", 80, 176, 500, 40, eid="body"))
    assert workzone_findings(ir, ZONE, 1280, 720) == []


def test_an_element_past_the_workzone_edge_is_an_overflow_error():
    ir = _ir(_shape(64, 160, 1200, 300, fill=SOLID, name="Wide panel", eid="wide"))
    found = workzone_findings(ir, ZONE, 1280, 720)
    assert rules(found) == ["workzone-overflow"]
    f = found[0]
    assert f["level"] == "error" and f["element"] == "Wide panel"
    assert "right by 48 px" in f["message"] and "64,144 1152×504" in f["message"]


def test_an_element_over_the_header_band_is_flagged_twice():
    ir = _ir(_text("Key insight", 64, 100, 400, 80, eid="kicker"))
    found = workzone_findings(ir, ZONE, 1280, 720)
    assert sorted(rules(found)) == ["header-band-overlap", "workzone-overflow"]
    band = next(f for f in found if f["rule"] == "header-band-overlap")
    assert "44 px into the header band" in band["message"] and "y = 144" in band["message"]
    assert '"Key insight"' in band["message"]


def test_the_tolerance_absorbs_sub_pixel_edges():
    ir = _ir(_shape(63, 143, 1154, 506, fill=SOLID, eid="edge"))
    assert workzone_findings(ir, ZONE, 1280, 720) == []
    assert rules(workzone_findings(ir, ZONE, 1280, 720, tolerance_px=0.5)) == ["workzone-overflow",
                                                                             "header-band-overlap"]


def test_invisible_wrappers_and_authored_titles_are_not_judged():
    ir = _ir(_shape(0, 0, 1280, 720, eid="wrapper"),                       # never painted
             _shape(10, 10, 0, 50, fill=SOLID, eid="flat"),                # zero area
             _text("Unmapped title", 64, 40, 1152, 60, eid="raw"))         # authored data-placeholder="title"
    assert rules(workzone_findings(ir, ZONE, 1280, 720)) == ["workzone-overflow", "header-band-overlap"]
    assert workzone_findings(ir, ZONE, 1280, 720, raw_placeholders={"raw": "title"}) == []


def test_a_workzone_at_the_top_edge_has_no_header_band():
    top = Workzone(left=0.05, top=0.0, width=0.9, height=0.9)
    assert top.header_band_px(1280, 720) is None
    ir = _ir(_shape(64, 10, 400, 100, fill=SOLID, eid="high"))
    assert workzone_findings(ir, top, 1280, 720) == []


# ------------------------------------------------------------------------------------ static fallback


STATIC = """<!doctype html><html><head><style>html,body{margin:0}</style></head><body>
<h1 data-placeholder="title" style="position:absolute;left:64px;top:40px;width:1152px;height:60px">Title</h1>
<div style="position:absolute;left:64px;top:160px;width:560px;height:400px;background:#2F80ED">Inside</div>
<div data-name="Kicker" style="position:absolute;left:64px;top:100px;width:400px;height:30px">Key insight</div>
<div style="position:absolute;left:700px;top:160px;width:560px;height:200px;background:#EEE"></div>
<div style="position:absolute;left:64px;top:600px;width:200px">No height: not judged statically</div>
<aside class="notes">Notes are never judged.</aside>
</body></html>"""


def test_the_static_fallback_reads_declared_boxes():
    found = workzone_findings_static(STATIC, ZONE, 1280, 720)
    assert [(f["rule"], f["element"]) for f in found] == [
        ("workzone-overflow", "Kicker"), ("header-band-overlap", "Kicker"),
        ("workzone-overflow", "the div block at 700,160")]
    assert "right by 44 px" in found[2]["message"]


def test_the_static_fallback_on_a_clean_slide():
    clean = STATIC.split('<div data-name="Kicker"')[0] + "</body></html>"
    assert workzone_findings_static(clean, ZONE, 1280, 720) == []


# ------------------------------------------------------------------------------------ measured, end to end


SLIDE = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;width:1280px;height:720px;overflow:hidden;background:transparent;font-family:Arial}
</style></head><body>
<h1 data-placeholder="title" style="position:absolute;left:64px;top:28px;width:1152px;margin:0;
font:700 24px/1.2 Arial;color:#2B2B33">Revenue grew 12% in 2025, driven by pricing and mix</h1>
<div data-name="Overrun" style="position:absolute;left:64px;top:110px;width:1152px;height:120px;
background:#2F80ED"></div>
<p style="position:absolute;left:64px;top:300px;width:600px;margin:0;font:14px/1.4 Arial;color:#2B2B33">
Pricing drove half of the growth.</p>
</body></html>"""


def test_design_findings_html_adds_the_workzone_rules(master_dir: Path, tmp_path: Path):
    path = tmp_path / "slide.html"
    path.write_text(SLIDE, encoding="utf-8")
    kwargs: dict[str, Any] = {"assets_dir": master_dir / "assets", "layouts_dir": master_dir / "layouts"}
    try:
        with_zone = design_lint.design_findings_html(path, manifest(), LAYOUT, workzone=ZONE, **kwargs)
        without = design_lint.design_findings_html(path, manifest(), LAYOUT, **kwargs)
    except design_lint.DesignLintUnavailable as exc:
        pytest.skip(f"no measuring browser on this machine: {exc}")
    found = [f for f in with_zone if f["rule"] in ("workzone-overflow", "header-band-overlap")]
    assert sorted(f["rule"] for f in found) == ["header-band-overlap", "workzone-overflow"]
    assert all(f["level"] == "error" and f["element"] == "Overrun" for f in found)
    assert not any(f["rule"] in ("workzone-overflow", "header-band-overlap") for f in without)
    assert with_zone[:2] == sorted(with_zone[:2], key=lambda f: f["level"] != "error"), "errors rank first"


def test_a_run_callable_carries_the_browser_work(master_dir: Path, tmp_path: Path):
    path = tmp_path / "slide.html"
    path.write_text(SLIDE, encoding="utf-8")
    ran: list[bool] = []

    def run(work: Any) -> Any:
        ran.append(True)
        return work()

    try:
        design_lint.design_findings_html(path, manifest(), LAYOUT, assets_dir=master_dir / "assets",
                                         layouts_dir=master_dir / "layouts", workzone=ZONE, run=run)
    except design_lint.DesignLintUnavailable as exc:
        pytest.skip(f"no measuring browser on this machine: {exc}")
    assert ran == [True]
