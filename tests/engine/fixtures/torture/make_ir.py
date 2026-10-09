"""Author the hand-written torture IRs in `fixtures/torture/ir/`.

    python -m tests.engine.fixtures.torture.make_ir

These IRs are **hand-authored, not measured**: they exist so WP3 and WP4 can emit and test every
construct before WP1 and WP2 land a real extractor. Each one is a small, deliberately chosen set —
at least one element per construct in its family — not a transcript of the matching `.html`.

They are written through `engine.ir`'s own dataclasses rather than typed as JSON by hand for one
reason: a fixture that fails `IR.validate()` wastes the time of whoever picks it up, and going
through the dataclasses makes that impossible to ship by accident. The content is still authored here
in full; nothing is read out of a browser.

`image.src` and `raster.src` are written **repo-relative, POSIX style** (see `ir/README` in
`fixtures/torture/README.md`). The schema calls for absolute paths, but a committed fixture cannot
carry this machine's drive letter; consumers resolve them against `engine.config.PROJECT_ROOT`.

WP0b creates these; ownership passes to WP4 (every family except `charts`/`svg-charts`, which are
WP3a's and WP3c's).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.engine.ir import IR, Box, Canvas, Diagnostic, Element, Fonts, Group, Slide

HERE = Path(__file__).resolve().parent
IR_DIR = HERE / "ir"

#: Repo-relative, because a committed fixture cannot hold an absolute path (see the module docstring).
ASSETS = "tests/engine/fixtures/torture/assets"

CANVAS_16X9 = Canvas(1280, 720)
CANVAS_4X3 = Canvas(960, 720)
FONTS = Fonts(forced={"major": "Calibri Light", "minor": "Calibri"}, used=["Calibri", "Calibri Light"])

INK = "2E2E38"
MUTED = "747480"
BLUE = "1A9AFA"
DEEP = "0B5FA5"
GREEN = "2DB757"
AMBER = "F5A623"
RED = "E2445C"


# ------------------------------------------------------------------------------ text helpers


def run(text: str, **over: Any) -> dict[str, Any]:
    """One styled run with every field the schema names, so nothing is left to a default."""
    value: dict[str, Any] = {
        "text": text, "font": "Calibri", "sizePx": 15.0, "weight": 400, "italic": False,
        "color": INK, "alpha": 1.0, "underline": False, "strike": False, "letterSpacingPx": 0.0,
        "baseline": "normal", "baselineShiftPx": 0.0, "caps": False, "href": None,
    }
    value.update(over)
    return value


def line(x: float, y: float, w: float, h: float, runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {"box": {"x": x, "y": y, "w": w, "h": h}, "runs": runs}


def para(
    lines: list[dict[str, Any]], *, align: str = "left", lh: float = 20.0,
    before: float = 0.0, after: float = 0.0, bullet: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "align": align, "lineHeightPx": lh, "spaceBeforePx": before, "spaceAfterPx": after,
        "bullet": bullet, "lines": lines,
    }


def text(
    name: str, box: tuple[float, float, float, float], paragraphs: list[dict[str, Any]], **over: Any
) -> Element:
    x, y, w, h = box
    element = Element(
        kind="text", box=Box(x, y, w, h), name=name, paragraphs=paragraphs, anchor="top", wrap=True,
        writingMode="horizontal", source={"path": f"body > [data-name={name}]", "tag": "div", "svg": None},
    )
    for key, value in over.items():
        setattr(element, key, value)
    return element


def shape(
    name: str, box: tuple[float, float, float, float], geometry: dict[str, Any], **over: Any
) -> Element:
    x, y, w, h = box
    element = Element(
        kind="shape", box=Box(x, y, w, h), name=name, geometry=geometry,
        fill={"type": "solid", "color": BLUE, "alpha": 1.0}, stroke=None,
        source={"path": f"body > [data-name={name}]", "tag": "div", "svg": None},
    )
    for key, value in over.items():
        setattr(element, key, value)
    return element


def solid(color: str, alpha: float = 1.0) -> dict[str, Any]:
    return {"type": "solid", "color": color, "alpha": alpha}


def stroke(color: str, width: float, **over: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "color": color, "alpha": 1.0, "width": width, "dash": "solid",
        "cap": "butt", "join": "miter", "headEnd": None, "tailEnd": None,
    }
    value.update(over)
    return value


def finish(ir: IR) -> IR:
    """Number the elements and refuse to return an IR that does not validate."""
    for index, element in enumerate(ir.elements):
        element.id = f"e{index + 1}"
        element.z = index
    problems = ir.validate(check_files=False)
    if problems:
        raise SystemExit(f"{ir.slide.id}: hand-written IR is invalid:\n  " + "\n  ".join(problems))
    return ir


# ----------------------------------------------------------------------------------- families


def ir_text() -> IR:
    """Paragraphs, runs, bullets, spacing, line heights, rotation, case — plus the two chip shapes."""
    elements = [
        text("head", (40, 24, 620, 78), [
            para([line(40, 24, 232, 35, [run("Text torture", font="Calibri Light", sizePx=30.0)])],
                 lh=34.5, after=6.0),
            para([line(40, 65, 620, 20, [run("Two in-flow blocks under one parent are one element.")]),
                  line(40, 85, 300, 20, [run("Each block is a paragraph.")])], lh=20.0),
        ]),
        # The inline highlight's border box: a shape behind the text, never a property of the text.
        shape("mark-hl", (232.5, 128, 78, 19), {"type": "rect"}, fill=solid("FFE9A8")),
        text("runs", (40, 104, 620, 44), [
            para([
                line(40, 104, 620, 20, [
                    run("bold ", weight=700),
                    run("italic ", italic=True),
                    run("underline ", underline=True),
                    run("struck ", strike=True),
                    run("coloured ", color=BLUE),
                    run("link", color=BLUE, underline=True, href="https://example.com/report"),
                ]),
                line(40, 124, 620, 20, [
                    run("base"),
                    run("sup", sizePx=10.0, baseline="super", baselineShiftPx=-4.5),
                    run(" and "),
                    run("highlighted", color=INK),
                    run(" tracked", letterSpacingPx=1.6),
                ]),
            ], lh=20.0),
        ]),
        shape("chip-baseline", (170, 188, 74, 20), {"type": "roundRect",
                                                    "radius": {"tl": 10, "tr": 10, "br": 10, "bl": 10}},
              fill=solid("E8F4FE")),
        shape("chip-target", (252, 188, 62, 20), {"type": "roundRect",
                                                  "radius": {"tl": 10, "tr": 10, "br": 10, "bl": 10}},
              fill=solid("E6F6EC")),
        text("chips", (40, 186, 620, 24), [
            para([line(40, 186, 480, 24, [
                run("Inline-block chips "),
                run("Baseline", sizePx=12.0, color=DEEP),
                run(" "),
                run("Target", sizePx=12.0, color="12703A"),
                run(" sit in the line."),
            ])], lh=24.0),
        ]),
        text("align", (40, 236, 620, 100), [
            para([line(40, 236, 200, 20, [run("Left aligned.")])], align="left"),
            para([line(250, 256, 200, 20, [run("Centre aligned.")])], align="center"),
            para([line(460, 276, 200, 20, [run("Right aligned.")])], align="right"),
            para([line(40, 296, 620, 20, [run("Justified first line stretched to the full measure.")]),
                  line(40, 316, 300, 20, [run("Ragged last line.")])], align="justify"),
        ]),
        text("bullets-char", (700, 104, 440, 72), [
            para([line(726, 104, 414, 24, [run("Unordered item one")])], lh=24.0, after=4.0,
                 bullet={"type": "char", "char": "•", "level": 0, "indentPx": 18,
                         "color": INK, "start": 1}),
            para([line(726, 128, 414, 24, [run("Unordered item two")])], lh=24.0, after=4.0,
                 bullet={"type": "char", "char": "•", "level": 0, "indentPx": 18,
                         "color": INK, "start": 1}),
            para([line(726, 152, 414, 24, [run("Unordered item three")])], lh=24.0,
                 bullet={"type": "char", "char": "•", "level": 0, "indentPx": 18,
                         "color": INK, "start": 1}),
        ]),
        text("bullets-num", (700, 190, 440, 72), [
            para([line(726, 190, 414, 24, [run("Ordered item one")])], lh=24.0, after=4.0,
                 bullet={"type": "num", "char": "1.", "level": 0, "indentPx": 18,
                         "color": INK, "start": 1}),
            para([line(726, 214, 414, 24, [run("Ordered item two")])], lh=24.0, after=4.0,
                 bullet={"type": "num", "char": "2.", "level": 0, "indentPx": 18,
                         "color": INK, "start": 1}),
            para([line(726, 238, 414, 24, [run("Ordered item three")])], lh=24.0,
                 bullet={"type": "num", "char": "3.", "level": 0, "indentPx": 18,
                         "color": INK, "start": 1}),
        ]),
        # line-height 3.0 on a 20 px font: the case the emitter's vertical placement is calibrated on.
        text("line-height-3", (700, 452, 440, 120), [
            para([line(700, 472, 440, 24, [run("Line height 3.0, first line", sizePx=20.0)]),
                  line(700, 532, 440, 24, [run("Line height 3.0, second line", sizePx=20.0)])], lh=60.0),
        ]),
        text("caps", (40, 492, 620, 20), [
            para([line(40, 492, 420, 20, [run("TRANSFORMED TO UPPER CASE", sizePx=13.0, weight=700,
                                              letterSpacingPx=2.0, caps=True)])], lh=20.0),
        ], transformCase="upper"),
        text("rotated-90", (1120, 389, 160, 22), [
            para([line(1120, 389, 160, 22, [run("Rotated minus 90 degrees", sizePx=13.0, weight=700)])],
                 align="center", lh=22.0),
        ], rotation=-90.0),
        text("anchored-bottom", (700, 600, 300, 60), [
            para([line(700, 640, 300, 20, [run("Anchored to the bottom of its box.")])], lh=20.0),
        ], anchor="bottom"),
    ]
    return finish(IR(
        canvas=CANVAS_16X9, fonts=FONTS, elements=elements,
        slide=Slide(id="tor_text", title="torture · text", layoutId="layout-07",
                    notes="Speaker notes for the text torture slide."),
    ))


def ir_boxes() -> IR:
    """Fills, borders, radii, gradients, shadows, opacity, clip-path, clipping, rotation, a group, a raster."""
    groups = [Group(id="g1", parent=None, name="badges", box=Box(950, 530, 280, 110))]
    elements = [
        shape("solid-fill", (40, 50, 160, 110), {"type": "rect"}, fill=solid(BLUE)),
        shape("uniform-border", (235, 50, 160, 110), {"type": "rect"},
              fill=solid("E8F4FE"), stroke=stroke(DEEP, 2.0)),
        shape("radius-uniform", (430, 50, 160, 110),
              {"type": "roundRect", "radius": {"tl": 16, "tr": 16, "br": 16, "bl": 16}}, fill=solid(GREEN)),
        shape("radius-per-corner", (625, 50, 160, 110),
              {"type": "roundRect", "radius": {"tl": 24, "tr": 0, "br": 24, "bl": 0}}, fill=solid(AMBER)),
        shape("linear-135", (820, 50, 160, 110), {"type": "rect"},
              fill={"type": "gradient", "kind": "linear", "angle": 135,
                    "stops": [{"pos": 0.0, "color": BLUE, "alpha": 1.0},
                              {"pos": 0.55, "color": GREEN, "alpha": 1.0},
                              {"pos": 1.0, "color": AMBER, "alpha": 1.0}]}),
        shape("radial", (1015, 50, 160, 110), {"type": "rect"},
              fill={"type": "gradient", "kind": "radial", "angle": 0,
                    "stops": [{"pos": 0.0, "color": "FFFFFF", "alpha": 1.0},
                              {"pos": 1.0, "color": BLUE, "alpha": 1.0}]}),
        shape("shadow-one", (40, 210, 160, 110), {"type": "rect"},
              fill=solid("FFFFFF"), stroke=stroke("E6E6EC", 1.0),
              shadow={"color": INK, "alpha": 0.28, "blur": 10, "dx": 0, "dy": 4}),
        shape("opacity-45", (235, 210, 160, 110), {"type": "rect"}, fill=solid(BLUE), opacity=0.45),
        shape("rgba-fill", (430, 210, 160, 110), {"type": "rect"}, fill=solid(RED, 0.35)),
        # clip-path: polygon() — a chevron, absolute px, already closed.
        shape("clip-path-chevron", (625, 210, 160, 110),
              {"type": "custom", "fillRule": "nonzero", "path": [
                  ["M", 625, 210], ["L", 750, 210], ["L", 785, 265], ["L", 750, 320],
                  ["L", 625, 320], ["L", 660, 265], ["Z"]]}, fill=solid(DEEP)),
        shape("overflow-clip", (820, 210, 160, 110), {"type": "rect"}, fill=solid("F7F7FA")),
        shape("clipped-child", (790, 230, 200, 60), {"type": "rect"},
              fill=solid(BLUE), clip=Box(820, 230, 160, 60)),
        shape("rotated-18", (1015, 210, 160, 110), {"type": "rect"}, fill=solid(GREEN), rotation=18.0),
        shape("dashed-border", (40, 370, 160, 110), {"type": "rect"},
              fill=solid("FFFFFF"), stroke=stroke("516467", 2.0, dash=[6, 4])),
        shape("flipped", (235, 370, 160, 110),
              {"type": "custom", "fillRule": "nonzero",
               "path": [["M", 235, 370], ["C", 295, 370, 295, 480, 395, 480], ["L", 235, 480], ["Z"]]},
              fill=solid(MUTED), flipH=True),
        shape("badge-a", (950, 530, 130, 110),
              {"type": "roundRect", "radius": {"tl": 8, "tr": 8, "br": 8, "bl": 8}},
              fill=solid("E8F4FE"), group="g1"),
        shape("badge-b", (1100, 530, 130, 110),
              {"type": "roundRect", "radius": {"tl": 8, "tr": 8, "br": 8, "bl": 8}},
              fill=solid("E6F6EC"), group="g1"),
        Element(kind="raster", box=Box(430, 370, 160, 110), name="conic-gradient",
                src=f"{ASSETS}/photo-400x300.png", reason="conic-gradient: not expressible in DrawingML",
                source={"path": "body > [data-name=conic-gradient]", "tag": "div", "svg": None}),
    ]
    diagnostics = [
        Diagnostic(level="warn", source="body > [data-name=conic-gradient]",
                   message="conic-gradient rasterised", elementId="e18"),
    ]
    return finish(IR(
        canvas=CANVAS_16X9, fonts=FONTS, elements=elements, groups=groups, diagnostics=diagnostics,
        slide=Slide(id="tor_boxes", title="torture · boxes", layoutId="layout-07", notes=None),
    ))


def ir_images() -> IR:
    """Every image variant the emitter has to place: fit, crop, radius, circle, clip, opacity."""
    png = f"{ASSETS}/photo-400x300.png"
    jpg = f"{ASSETS}/photo-300x400.jpg"
    logo = f"{ASSETS}/logo-160x160.png"

    def image(name: str, box: tuple[float, float, float, float], src: str, **over: Any) -> Element:
        x, y, w, h = box
        element = Element(kind="image", box=Box(x, y, w, h), name=name, src=src, fit="cover",
                          crop={"l": 0.0, "t": 0.0, "r": 0.0, "b": 0.0}, radius=0.0, circle=False,
                          source={"path": f"body > [data-name={name}]", "tag": "img", "svg": None})
        for key, value in over.items():
            setattr(element, key, value)
        return element

    elements = [
        image("cover-landscape", (40, 70, 180, 140), png,
              crop={"l": 0.0179, "t": 0.0, "r": 0.0179, "b": 0.0}),
        image("contain-landscape", (245, 70, 180, 140), png, fit="contain"),
        image("fill-landscape", (450, 70, 180, 140), png, fit="fill"),
        image("cover-portrait", (655, 70, 180, 140), jpg,
              crop={"l": 0.0, "t": 0.2083, "r": 0.0, "b": 0.2083}),
        image("radius-14", (860, 70, 180, 140), png, radius=14.0,
              crop={"l": 0.0179, "t": 0.0, "r": 0.0179, "b": 0.0}),
        image("circle", (1065, 70, 140, 140), logo, circle=True, radius=70.0),
        image("opacity-50", (40, 300, 180, 140), png, opacity=0.5,
              crop={"l": 0.0179, "t": 0.0, "r": 0.0179, "b": 0.0}),
        image("clipped-image", (820, 270, 300, 225), png, fit="fill", clip=Box(860, 300, 180, 140)),
    ]
    return finish(IR(
        canvas=CANVAS_16X9, fonts=FONTS, elements=elements,
        slide=Slide(id="tor_images", title="torture · images", layoutId="layout-07", notes=None),
    ))


def ir_tables() -> IR:
    """One caption and one table with a colspan, a rowspan, per-cell borders, fills and alignments."""

    def border(width: float = 1.0, color: str = "E6E6EC", dash: str = "solid") -> dict[str, Any]:
        return {"width": width, "color": color, "dash": dash}

    def cell(
        r: int, c: int, runs: list[dict[str, Any]], *, row_span: int = 1, col_span: int = 1,
        fill: dict[str, Any] | None = None, valign: str = "top", align: str = "left",
        borders: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "r": r, "c": c, "rowSpan": row_span, "colSpan": col_span,
            "fill": fill,
            "borders": borders or {"top": None, "right": None, "bottom": border(), "left": None},
            "paddingPx": {"t": 7, "r": 10, "b": 7, "l": 10},
            "valign": valign,
            "paragraphs": [para([line(0, 0, 100, 20, runs)], align=align, lh=20.0)],
        }

    head = solid(INK)
    cells = [
        cell(0, 0, [run("Market", color="FFFFFF", weight=700)], col_span=2, fill=head,
             borders={"top": None, "right": None, "bottom": border(2.0, BLUE), "left": None}),
        cell(0, 2, [run("2023", color="FFFFFF", weight=700)], fill=head, align="right",
             borders={"top": None, "right": None, "bottom": border(2.0, BLUE), "left": None}),
        cell(0, 3, [run("FY27E", color="FFFFFF", weight=700)], fill=head, align="right",
             borders={"top": None, "right": None, "bottom": border(2.0, BLUE), "left": None}),
        cell(1, 0, [run("Partner", weight=700)], row_span=2, fill=solid("F2F8FE"), valign="middle",
             borders={"top": None, "right": border(1.0, "C9DCEB"), "bottom": border(), "left": None}),
        cell(1, 1, [run("Upsell and referred customers")]),
        cell(1, 2, [run("12.4")], align="right"),
        cell(1, 3, [run("31.0")], align="right"),
        cell(2, 1, [run("Business and events")]),
        cell(2, 2, [run("8.1")], align="right"),
        cell(2, 3, [run("17.5")], align="right"),
        cell(3, 0, [run("Internal", weight=700)], fill=solid("F2F8FE"), valign="middle",
             borders={"top": None, "right": border(1.0, "C9DCEB"), "bottom": border(), "left": None}),
        cell(3, 1, [run("Upsell")]),
        cell(3, 2, [run("51.2")], align="right"),
        cell(3, 3, [run("62.0")], align="right"),
        cell(4, 1, [run("Other purposes", weight=700)], valign="bottom",
             borders={"top": border(2.0, INK), "right": None, "bottom": None, "left": None}),
        cell(4, 2, [run("27.8", weight=700)], align="right", valign="bottom",
             borders={"top": border(2.0, INK), "right": None, "bottom": None, "left": None}),
        cell(4, 3, [run("18.0", weight=700)], align="right", valign="bottom",
             borders={"top": border(2.0, INK), "right": None, "bottom": None, "left": None}),
    ]
    elements = [
        text("caption", (48, 80, 560, 22), [
            para([line(48, 80, 300, 22, [run("Partners by segment, millions", sizePx=16.0, weight=700)])],
                 lh=22.0),
        ]),
        Element(
            kind="table", box=Box(48, 112, 560, 170), name="segments",
            rows=5, cols=4, colWidthsPx=[130.0, 170.0, 130.0, 130.0],
            rowHeightsPx=[34.0, 34.0, 34.0, 34.0, 34.0], cells=cells,
            source={"path": "body > div > table.t1", "tag": "table", "svg": None},
        ),
    ]
    return finish(IR(
        canvas=CANVAS_16X9, fonts=FONTS, elements=elements,
        slide=Slide(id="tor_tables", title="torture · tables", layoutId="layout-07", notes=None),
    ))


def ir_svg_shapes() -> IR:
    """The SVG primitives, as they arrive after `expand_svg` and the splice."""
    groups = [Group(id="svg#1-g-inherit", parent=None, name="g-inherit", box=Box(72, 224, 120, 96))]

    def svg_shape(name: str, box: tuple[float, float, float, float], geometry: dict[str, Any],
                  **over: Any) -> Element:
        element = shape(name, box, geometry, **over)
        element.source = {"path": "body > svg#grid", "tag": "svg", "svg": f"svg#1 > #{name}"}
        return element

    elements = [
        svg_shape("rect-plain", (72, 80, 112, 96), {"type": "rect"}, fill=solid(BLUE)),
        svg_shape("rect-rounded", (216, 80, 112, 96),
                  {"type": "roundRect", "radius": {"tl": 16, "tr": 16, "br": 16, "bl": 16}},
                  fill=solid(GREEN)),
        svg_shape("circle-plain", (512, 80, 96, 96), {"type": "ellipse"}, fill=solid(RED)),
        svg_shape("ellipse-plain", (648, 92, 112, 72), {"type": "ellipse"}, fill=solid(DEEP)),
        svg_shape("line-arrow", (792, 88, 112, 88),
                  {"type": "line", "points": [[792, 176], [904, 88]]},
                  fill={"type": "none"},
                  stroke=stroke(INK, 4.0, headEnd={"type": "triangle", "size": "med"})),
        svg_shape("polyline-open", (936, 80, 112, 88),
                  {"type": "polyline", "points": [[936, 168], [964, 96], [992, 144], [1020, 80], [1048, 128]]},
                  fill={"type": "none"}, stroke=stroke(BLUE, 4.0, join="round")),
        svg_shape("polygon-closed", (1088, 80, 96, 92),
                  {"type": "custom", "fillRule": "nonzero", "path": [
                      ["M", 1136, 80], ["L", 1184, 116], ["L", 1166, 172], ["L", 1106, 172],
                      ["L", 1088, 116], ["Z"]]},
                  fill=solid(MUTED)),
        svg_shape("inherit-a", (72, 224, 52, 96), {"type": "rect"},
                  fill=solid("E8F4FE"), stroke=stroke(DEEP, 4.0), group="svg#1-g-inherit"),
        svg_shape("inherit-b", (132, 224, 52, 96), {"type": "rect"},
                  fill=solid("E8F4FE"), stroke=stroke(DEEP, 4.0), group="svg#1-g-inherit"),
        svg_shape("grad-linear", (360, 224, 112, 96), {"type": "rect"},
                  fill={"type": "gradient", "kind": "linear", "angle": 90,
                        "stops": [{"pos": 0.0, "color": BLUE, "alpha": 1.0},
                                  {"pos": 1.0, "color": DEEP, "alpha": 1.0}]}),
        svg_shape("line-dashed", (648, 272, 112, 0),
                  {"type": "line", "points": [[648, 272], [760, 272]]},
                  fill={"type": "none"}, stroke=stroke("516467", 6.0, dash=[12, 6], cap="round")),
        svg_shape("alpha-rect", (936, 224, 112, 96), {"type": "rect"},
                  fill=solid(BLUE, 0.4), stroke=stroke(DEEP, 6.0, alpha=0.8), opacity=0.8),
        svg_shape("clipped-rect", (1080, 224, 112, 96), {"type": "rect"},
                  fill=solid(AMBER), clip=Box(1108, 224, 84, 68)),
        Element(kind="image", box=Box(504, 376, 112, 112), name="svg-image",
                src=f"{ASSETS}/logo-160x160.png", fit="cover",
                crop={"l": 0.0, "t": 0.0, "r": 0.0, "b": 0.0}, radius=0.0, circle=False,
                source={"path": "body > svg#grid", "tag": "image", "svg": "svg#1 > #svg-image"}),
        text("text-pt", (648, 360, 130, 30), [
            para([line(648, 360, 130, 30, [run("9 pt text", sizePx=24.0)])], lh=30.0),
        ]),
        text("text-tspans", (792, 352, 150, 30), [
            para([line(792, 352, 150, 30, [
                run("Base", sizePx=22.0),
                run("sup", sizePx=16.0, color=BLUE, baseline="super", baselineShiftPx=-10.0),
                run("bold", sizePx=22.0, weight=700),
            ])], lh=30.0),
        ]),
        text("text-rot", (900, 360, 120, 24), [
            para([line(900, 360, 120, 24, [run("Rotated", sizePx=20.0, color="516467")])],
                 align="center", lh=24.0),
        ], rotation=-90.0),
        shape("shadow-fedropshadow", (936, 516, 112, 88), {"type": "rect"},
              fill=solid("FFFFFF"), stroke=stroke("E6E6EC", 2.0),
              shadow={"color": INK, "alpha": 0.35, "blur": 6.4, "dx": 0, "dy": 4}),
        Element(kind="raster", box=Box(72, 512, 112, 96), name="raster-filter",
                src=f"{ASSETS}/photo-400x300.png", reason="svg filter: feGaussianBlur",
                source={"path": "body > svg#grid", "tag": "rect", "svg": "svg#1 > #raster-filter"}),
    ]
    diagnostics = [
        Diagnostic(level="warn", source="svg#1 > #raster-filter",
                   message="svg filter feGaussianBlur rasterised", elementId="e19"),
    ]
    return finish(IR(
        canvas=CANVAS_16X9, fonts=FONTS, elements=elements, groups=groups, diagnostics=diagnostics,
        slide=Slide(id="tor_svg_shapes", title="torture · svg-shapes", layoutId="layout-06", notes=None),
    ))


def ir_svg_paths() -> IR:
    """Curves, the four path presets, both ring arcs, a chord, even-odd and nested groups."""
    groups = [
        Group(id="svg#1-g-outer", parent=None, name="g-outer", box=Box(348, 486, 120, 152)),
        Group(id="svg#1-g-inner", parent="svg#1-g-outer", name="g-inner", box=Box(348, 486, 120, 152)),
    ]

    def path_shape(name: str, box: tuple[float, float, float, float], path: list[list[Any]],
                   **over: Any) -> Element:
        element = shape(name, box, {"type": "custom", "fillRule": "nonzero", "path": path}, **over)
        element.source = {"path": "body > svg#paths", "tag": "svg", "svg": f"svg#1 > #{name}"}
        return element

    def arc_shape(name: str, box: tuple[float, float, float, float], gtype: str,
                  arc: dict[str, float], **over: Any) -> Element:
        element = shape(name, box, {"type": gtype, "arc": arc}, **over)
        element.source = {"path": "body > svg#paths", "tag": "svg", "svg": f"svg#1 > #{name}"}
        return element

    elements = [
        path_shape("path-cubic", (72, 80, 112, 96),
                   [["M", 72, 176], ["C", 96, 80, 152, 80, 184, 176]],
                   fill={"type": "none"}, stroke=stroke(BLUE, 6.0, cap="round")),
        path_shape("path-quad", (216, 72, 112, 104),
                   [["M", 216, 176], ["Q", 272, 72, 328, 176], ["Z"]],
                   fill=solid("E8F4FE"), stroke=stroke(DEEP, 4.0)),
        path_shape("path-chevron", (504, 80, 112, 96),
                   [["M", 504, 80], ["L", 584, 80], ["L", 616, 128], ["L", 584, 176],
                    ["L", 504, 176], ["L", 536, 128], ["Z"]],
                   fill=solid(DEEP)),
        shape("path-as-rect", (648, 80, 112, 96), {"type": "rect"}, fill=solid(AMBER)),
        shape("path-as-circle", (800, 80, 96, 96), {"type": "ellipse"}, fill=solid(RED)),
        arc_shape("path-as-blockarc", (944, 80, 96, 96), "blockArc",
                  {"cx": 992, "cy": 128, "rOuter": 48, "rInner": 28, "startDeg": 180, "endDeg": 360},
                  fill=solid(MUTED)),
        arc_shape("path-as-pie", (1088, 80, 96, 96), "pie",
                  {"cx": 1136, "cy": 128, "rOuter": 48, "rInner": 0, "startDeg": 0, "endDeg": 90},
                  fill=solid(DEEP)),
        # A chord has no HTML source in this family; it is here so the emitter has one to write.
        arc_shape("chord-case", (72, 240, 96, 96), "chord",
                  {"cx": 120, "cy": 288, "rOuter": 48, "rInner": 0, "startDeg": 30, "endDeg": 210},
                  fill=solid(GREEN, 0.5), stroke=stroke("12703A", 2.0)),
        arc_shape("ring-dash-large", (70, 234, 196, 196), "blockArc",
                  {"cx": 168, "cy": 332, "rOuter": 98, "rInner": 78, "startDeg": 270, "endDeg": 2.19},
                  fill=solid(BLUE)),
        arc_shape("ring-dash-small", (340, 264, 136, 136), "blockArc",
                  {"cx": 408, "cy": 332, "rOuter": 68, "rInner": 52, "startDeg": 270, "endDeg": 50.38},
                  fill=solid(GREEN)),
        shape("polyline-markers", (528, 264, 180, 140),
              {"type": "polyline", "points": [[528, 404], [588, 284], [648, 384], [708, 264]]},
              fill={"type": "none"},
              stroke=stroke(BLUE, 4.0, headEnd={"type": "triangle", "size": "med"},
                            tailEnd={"type": "oval", "size": "med"})),
        path_shape("dash-scaled", (748, 264, 180, 150),
                   [["M", 748, 264], ["L", 928, 264], ["L", 928, 414]],
                   fill={"type": "none"}, stroke=stroke(RED, 6.0, dash=[12, 6])),
        path_shape("transformed-path", (96, 486, 176, 152),
                   [["M", 116, 486], ["L", 272, 543], ["L", 252, 638], ["L", 96, 581], ["Z"]],
                   fill=solid("C9DCEB"), stroke=stroke(DEEP, 3.0), rotation=0.0),
        path_shape("nested-path", (348, 486, 120, 152),
                   [["M", 348, 486], ["C", 408, 486, 408, 606, 468, 606], ["L", 468, 638],
                    ["L", 348, 638], ["Z"]],
                   fill=solid("E6F6EC"), stroke=stroke("12703A", 3.0), opacity=0.9,
                   group="svg#1-g-inner"),
        path_shape("path-evenodd", (548, 486, 180, 152),
                   [["M", 548, 486], ["L", 728, 486], ["L", 728, 638], ["L", 548, 638], ["Z"],
                    ["M", 584, 522], ["L", 692, 522], ["L", 692, 602], ["L", 584, 602], ["Z"]],
                   fill=solid(AMBER), stroke=stroke("8A5A00", 3.0)),
        path_shape("use-chevron-a", (788, 486, 112, 96),
                   [["M", 788, 486], ["L", 868, 486], ["L", 900, 534], ["L", 868, 582],
                    ["L", 788, 582], ["L", 820, 534], ["Z"]],
                   fill=solid("516467")),
    ]
    # path-evenodd: the hole is the whole point of the case, so set the rule by name, not by index.
    next(e for e in elements if e.name == "path-evenodd").geometry["fillRule"] = "evenodd"
    return finish(IR(
        canvas=CANVAS_16X9, fonts=FONTS, elements=elements, groups=groups,
        slide=Slide(id="tor_svg_paths", title="torture · svg-paths", layoutId="layout-06", notes=None),
    ))


def ir_placeholders() -> IR:
    """Explicit and implicit placeholder mapping on the Comparison layout, plus one refusal."""
    groups = [Group(id="g1", parent=None, name="badges", box=Box(950, 240, 280, 120))]
    elements = [
        text("deck-title", (48, 36, 864, 70), [
            para([line(48, 36, 780, 36, [run("Placeholder mapping on the Comparison layout",
                                             font="Calibri Light", sizePx=30.0)])], lh=36.0),
        ], placeholder={"type": "title", "idx": 0}),
        shape("rule", (48, 152, 864, 2), {"type": "rect"}, fill=solid(BLUE)),
        text("left-lede", (48, 166, 424, 56), [
            para([line(48, 166, 424, 22, [run("Left column heading, mapped by overlap alone",
                                              sizePx=15.0, weight=700, color=DEEP)])], lh=22.0),
        ], placeholder={"type": "body", "idx": 1}),
        text("right-lede", (488, 166, 424, 56), [
            para([line(488, 166, 424, 22, [run("Right column heading, mapped by attribute",
                                               sizePx=15.0, weight=700, color=DEEP)])], lh=22.0),
        ], placeholder={"type": "body", "idx": 3}),
        text("left-body", (48, 240, 424, 120), [
            para([line(48, 240, 424, 20, [run("This block carries no data-placeholder.", sizePx=14.0)]),
                  line(48, 260, 424, 20, [run("It lies inside the second zone.", sizePx=14.0)])], lh=20.0),
        ], placeholder={"type": "obj", "idx": 2}),
        text("right-body", (488, 240, 424, 160), [
            para([line(488, 240, 424, 20, [run("Two zones share the type obj on this layout,",
                                               sizePx=14.0)]),
                  line(488, 260, 424, 20, [run("so idx 4 is the only correct answer.", sizePx=14.0)])],
                 lh=20.0),
        ], placeholder={"type": "obj", "idx": 4}),
        text("below-threshold", (380, 600, 300, 100), [
            para([line(380, 600, 300, 18, [run("Only 13 per cent of this box overlaps the",
                                               sizePx=11.0, color=MUTED)]),
                  line(380, 618, 300, 18, [run("second zone, so it stays a text box.",
                                               sizePx=11.0, color=MUTED)])], lh=18.0),
        ]),
        text("source-note", (48, 660, 400, 28), [
            para([line(48, 660, 400, 18, [run("Source: torture fixture.", sizePx=11.0, color=MUTED)])],
                 lh=18.0),
        ]),
        shape("badge-a", (950, 240, 130, 120),
              {"type": "roundRect", "radius": {"tl": 8, "tr": 8, "br": 8, "bl": 8}},
              fill=solid("E8F4FE"), group="g1"),
        shape("badge-b", (1100, 240, 130, 120),
              {"type": "roundRect", "radius": {"tl": 8, "tr": 8, "br": 8, "bl": 8}},
              fill=solid("E6F6EC"), group="g1"),
    ]
    return finish(IR(
        canvas=CANVAS_16X9, fonts=FONTS, elements=elements, groups=groups,
        slide=Slide(id="tor_placeholders", title="torture · placeholders", layoutId="layout-05",
                    notes=None),
    ))


def ir_sizes_4x3() -> IR:
    """The same constructs on a 960 x 720 canvas: the size-generality check."""
    elements = [
        shape("corner-top-left", (0, 0, 24, 24), {"type": "rect"}, fill=solid(BLUE)),
        shape("corner-bottom-right", (936, 696, 24, 24), {"type": "rect"}, fill=solid(GREEN)),
        text("eyebrow", (40, 44, 360, 20), [
            para([line(40, 44, 300, 20, [run("SIZE GENERALITY · FOUR BY THREE", sizePx=12.0,
                                             weight=700, letterSpacingPx=1.5, color=MUTED, caps=True)])],
                 lh=20.0),
        ], transformCase="upper"),
        shape("swatch-a", (40, 80, 110, 60), {"type": "rect"}, fill=solid(BLUE)),
        shape("swatch-c", (280, 80, 110, 60),
              {"type": "roundRect", "radius": {"tl": 10, "tr": 10, "br": 10, "bl": 10}},
              fill=solid(AMBER)),
        # 400x300 into 300x150: cover scales by 0.75 to 300x225, so 37.5 px falls off top and bottom.
        Element(kind="image", box=Box(620, 40, 300, 150), name="hero-image",
                src=f"{ASSETS}/photo-400x300.png", fit="cover",
                crop={"l": 0.0, "t": 0.1667, "r": 0.0, "b": 0.1667}, radius=0.0, circle=False,
                source={"path": "body > [data-name=hero-image] > img", "tag": "img", "svg": None}),
        text("deck-title", (72, 232, 816, 80), [
            para([line(72, 232, 700, 40, [run("The same IR, emitted onto a narrower canvas",
                                              font="Calibri Light", sizePx=34.0)])], lh=40.0),
        ], placeholder={"type": "ctrTitle", "idx": 0}),
        text("deck-subtitle", (144, 416, 672, 60), [
            para([line(144, 416, 672, 23, [run("One CSS pixel is still one ninety-sixth of an inch.",
                                               sizePx=16.0, color="516467")])], lh=23.0),
        ], placeholder={"type": "subTitle", "idx": 1}),
        shape("diagram-box", (50, 526, 120, 70),
              {"type": "roundRect", "radius": {"tl": 8, "tr": 8, "br": 8, "bl": 8}},
              fill=solid("E8F4FE"), stroke=stroke(DEEP, 2.0)),
        shape("diagram-dot", (212, 523, 76, 76), {"type": "ellipse"},
              fill=solid(GREEN, 0.35), stroke=stroke("12703A", 2.0)),
        text("diagram-label", (50, 622, 120, 18), [
            para([line(50, 622, 120, 18, [run("Native on both", sizePx=13.0)])], align="center", lh=18.0),
        ]),
        Element(
            kind="table", box=Box(350, 496, 250, 90), name="mini-table",
            rows=3, cols=2, colWidthsPx=[130.0, 120.0], rowHeightsPx=[30.0, 30.0, 30.0],
            cells=[
                {"r": 0, "c": 0, "rowSpan": 1, "colSpan": 1, "fill": solid("E8F4FE"),
                 "borders": {"top": {"width": 1.0, "color": "D9D9E0", "dash": "solid"},
                             "right": {"width": 1.0, "color": "D9D9E0", "dash": "solid"},
                             "bottom": {"width": 1.0, "color": "D9D9E0", "dash": "solid"},
                             "left": {"width": 1.0, "color": "D9D9E0", "dash": "solid"}},
                 "paddingPx": {"t": 5, "r": 8, "b": 5, "l": 8}, "valign": "top",
                 "paragraphs": [para([line(0, 0, 100, 18, [run("Canvas", sizePx=12.0, weight=700,
                                                               color=DEEP)])], lh=18.0)]},
                {"r": 0, "c": 1, "rowSpan": 1, "colSpan": 1, "fill": solid("E8F4FE"),
                 "borders": {"top": {"width": 1.0, "color": "D9D9E0", "dash": "solid"},
                             "right": {"width": 1.0, "color": "D9D9E0", "dash": "solid"},
                             "bottom": {"width": 1.0, "color": "D9D9E0", "dash": "solid"},
                             "left": {"width": 1.0, "color": "D9D9E0", "dash": "solid"}},
                 "paddingPx": {"t": 5, "r": 8, "b": 5, "l": 8}, "valign": "top",
                 "paragraphs": [para([line(0, 0, 100, 18, [run("Width", sizePx=12.0, weight=700,
                                                               color=DEEP)])], align="right", lh=18.0)]},
                {"r": 1, "c": 0, "rowSpan": 1, "colSpan": 1, "fill": None,
                 "borders": {"top": None, "right": None,
                             "bottom": {"width": 1.0, "color": "D9D9E0", "dash": "solid"}, "left": None},
                 "paddingPx": {"t": 5, "r": 8, "b": 5, "l": 8}, "valign": "top",
                 "paragraphs": [para([line(0, 0, 100, 18, [run("16:9", sizePx=12.0)])], lh=18.0)]},
                {"r": 1, "c": 1, "rowSpan": 1, "colSpan": 1, "fill": None,
                 "borders": {"top": None, "right": None,
                             "bottom": {"width": 1.0, "color": "D9D9E0", "dash": "solid"}, "left": None},
                 "paddingPx": {"t": 5, "r": 8, "b": 5, "l": 8}, "valign": "top",
                 "paragraphs": [para([line(0, 0, 100, 18, [run("1280", sizePx=12.0)])], align="right",
                                     lh=18.0)]},
                {"r": 2, "c": 0, "rowSpan": 1, "colSpan": 1, "fill": None,
                 "borders": {"top": None, "right": None, "bottom": None, "left": None},
                 "paddingPx": {"t": 5, "r": 8, "b": 5, "l": 8}, "valign": "top",
                 "paragraphs": [para([line(0, 0, 100, 18, [run("4:3", sizePx=12.0)])], lh=18.0)]},
                {"r": 2, "c": 1, "rowSpan": 1, "colSpan": 1, "fill": None,
                 "borders": {"top": None, "right": None, "bottom": None, "left": None},
                 "paddingPx": {"t": 5, "r": 8, "b": 5, "l": 8}, "valign": "top",
                 "paragraphs": [para([line(0, 0, 100, 18, [run("960", sizePx=12.0)])], align="right",
                                     lh=18.0)]},
            ],
            source={"path": "body > table[data-name=mini-table]", "tag": "table", "svg": None},
        ),
        Element(
            kind="chart", box=Box(630, 496, 290, 180), name="mini-chart", origin="authored",
            confidence=1.0, plotRect=Box(653.2, 505, 261, 144), overlay=[],
            style={"font": "Calibri", "sizePx": 11.0, "color": "516467"},
            spec={
                "type": "column",
                "categories": ["16:9", "4:3"],
                "series": [{"name": "Canvas width", "values": [1280, 960]}],
                "colors": ["#1A9AFA"],
                "options": {"dataLabels": True, "labelPosition": "outEnd", "legend": False,
                            "gridlines": False,
                            "valueAxis": {"min": 0, "max": 1400, "majorUnit": 350}},
            },
            source={"path": "body > [data-name=mini-chart]", "tag": "div", "svg": None},
        ),
    ]
    return finish(IR(
        canvas=CANVAS_4X3, fonts=FONTS, elements=elements,
        slide=Slide(id="tor_sizes_4x3", title="torture · sizes-4x3", layoutId="layout-01",
                    notes="The 4:3 cross-section."),
    ))


# ------------------------------------------------------------ charts-hardening, charts-negative (WP-C)
#
# The two chart families of WP-C §8.1. Each chart's spec below is the one in the family's HTML,
# verbatim (`test_charts_emit.py` asserts the two files agree); the geometry is the HTML's own
# (`position:absolute` boxes). A chart without `options.plotArea` has no measured plot rect
# (`plotRect: None`), exactly as `page.js: emitChart` records it once WP-C C3 lands; one with a
# `plotArea` carries the rect the browser measures from it.

#: The families' chart text: `.chart { font-size: 11px; color: #333 }` in the HTML.
CHART_STYLE: dict[str, Any] = {"font": "Calibri", "sizePx": 11.0, "color": "333333"}
CAPTION = "516467"
TITLE_INK = "12233B"

_P05_CATEGORIES = ["Standalone", "Cost syn.", "Revenue syn.", "Subtotal", "Integration", "Tax", "Combined"]
_P05_VALUES = [8.1, 2.4, 1.6, 12.1, -0.9, 1.2, 12.4]
_P05_COLOURS = ["#1E9E5A", "#D64545", "#0B2545"]
_P05_OPTIONS = {"dataLabels": True, "numberFormat": "0.0", "gridlines": False, "legend": False}

#: (data-name, caption, box, spec) per chart, in document order.
HARDENING_CHARTS: list[tuple[str, str, tuple[float, float, float, float], dict[str, Any]]] = [
    ("A totals as indices", "A · p05-A: totals as indices, no plotArea", (40, 84, 285, 250), {
        "type": "waterfall", "categories": _P05_CATEGORIES,
        "series": [{"name": "Value", "values": _P05_VALUES}], "colors": _P05_COLOURS,
        "options": dict(_P05_OPTIONS, totals=[0, 3, 6])}),
    ("B totals as types", "B · p05-B: totals from series.types", (345, 84, 285, 250), {
        "type": "waterfall", "categories": _P05_CATEGORIES,
        "series": [{"name": "Value", "values": _P05_VALUES,
                    "types": ["total", "increase", "increase", "total", "decrease", "increase", "total"]}],
        "colors": _P05_COLOURS, "options": dict(_P05_OPTIONS)}),
    ("C totals as flags", "C · p05-C: totals as per-point booleans", (650, 84, 285, 250), {
        "type": "waterfall", "categories": _P05_CATEGORIES,
        "series": [{"name": "Value", "values": _P05_VALUES}], "colors": _P05_COLOURS,
        "options": dict(_P05_OPTIONS, totals=[True, False, False, True, False, False, True])}),
    ("E stacked waterfall", "E · two series stacked, no bar crosses zero", (955, 84, 285, 250), {
        "type": "waterfall", "categories": ["FY23", "Price", "Volume", "Mix", "FY24"],
        "series": [{"name": "Internal", "values": [36, 9, 14, -11, 48]},
                   {"name": "Partner", "values": [13, 5, 6, -4, 20]}],
        "colors": ["#0B2545", "#1A9AFA"],
        "options": {"dataLabels": True, "numberFormat": "0", "gridlines": False, "totals": [0, 4]}}),
    # prj_844d2914db (Sonnet 5), slide sld_124e333385 v001, copied verbatim on 2026-09-25 (read-only).
    ("G model-written waterfall", "G · a model's waterfall: list pointColors, plotArea", (40, 392, 590, 250), {
        "type": "waterfall",
        "categories": ["Baseline revenue value", "+ Partner growth", "+ Longer terms", "+ Higher spend",
                       "+ New marketplaces", "+ Private investment"],
        "series": [{"name": "Index", "values": [100, 30, 12, 18, 15, 20]}],
        "options": {"dataLabels": True, "numberFormat": "0", "gridlines": False, "legend": False,
                    "pointColors": ["#C4C4CD", "#1A9AFA", "#1A9AFA", "#1A9AFA", "#1A9AFA", "#1A9AFA"],
                    "valueAxis": {"min": 0, "max": 220, "majorUnit": 55, "visible": False},
                    "plotArea": {"x": 0.03, "y": 0.03, "w": 0.95, "h": 0.86}}}),
    ("H near 2750", "H · p11-A: a bridge near 2,750", (650, 392, 285, 250), {
        "type": "waterfall", "categories": ["FY23", "Services", "Products", "Lending", "FX & other", "FY24"],
        "series": [{"name": "Revenue", "values": [2750, 140, 95, 55, -70, 2970]}],
        "colors": ["#1E9E5A", "#D64545", "#0B3D2E"],
        "options": {"dataLabels": True, "numberFormat": "#,##0", "gridlines": False, "legend": False,
                    "totals": [0, 5]}}),
    ("I reference line", "I · a reference line at the third bar's value", (955, 392, 285, 250), {
        "type": "column", "categories": ["North", "South", "East", "West"],
        "series": [{"name": "Sales", "values": [42, 55, 61, 48]}], "colors": ["#1A9AFA"],
        "options": {"dataLabels": True, "numberFormat": "0", "gridlines": False, "legend": False,
                    "valueAxis": {"min": 0, "max": 80, "majorUnit": 20},
                    "referenceLines": [{"value": 61, "label": "target"}]}}),
]

NEGATIVE_CHARTS: list[tuple[str, str, tuple[float, float, float, float], dict[str, Any]]] = [
    ("D bridge crosses zero", "D · p05-D: a bridge that crosses zero", (40, 84, 590, 250), {
        "type": "waterfall", "categories": ["FY23", "Price", "Volume", "Mix", "Cost out", "FY24"],
        "series": [{"name": "EBITDA", "values": [20, -35, 10, -8, 25, 12]}], "colors": _P05_COLOURS,
        "options": {"dataLabels": True, "numberFormat": "0", "gridlines": False, "legend": False,
                    "totals": [0, 5]}}),
    ("F negative bar", "F · p11-B: horizontal bars, one negative", (650, 84, 590, 250), {
        "type": "bar", "categories": ["Bear case", "Base case", "Bull case"],
        "series": [{"name": "FY2 EPS impact", "values": [-2.0, 2.3, 6.2]}], "colors": ["#0B3D2E"],
        "options": {"dataLabels": True, "numberFormat": "0.0\"%\"", "gridlines": False, "legend": False}}),
    ("J stacked crossing", "J · stacked, a bar across zero", (40, 392, 386, 250), {
        "type": "waterfall", "categories": ["FY23", "Q1", "FY24"],
        "series": [{"name": "Internal", "values": [30, -25, -10]},
                   {"name": "Partner", "values": [0, -15, 0]}],
        "colors": ["#0B2545", "#1A9AFA"],
        "options": {"numberFormat": "0", "gridlines": False, "totals": [0, 2]}}),
    ("K below zero", "K · entirely below zero", (447, 392, 386, 250), {
        "type": "waterfall", "categories": ["FY23", "Opex", "FY24"],
        "series": [{"name": "EBIT", "values": [-20, -10, -30]}],
        "options": {"numberFormat": "0", "gridlines": False, "legend": False, "totals": [0, 2]}}),
    ("L zero step", "L · a zero step, then landing on zero", (854, 392, 386, 250), {
        "type": "waterfall", "categories": ["Start", "Flat", "Fall", "Rise"],
        "series": [{"name": "Cash", "values": [20, 0, -20, 5]}],
        "options": {"gridlines": False, "legend": False, "totals": [0]}}),
]


def _chart_family(slide_id: str, title: str, charts: list, notes: str) -> IR:
    elements: list[Element] = [
        text("title", (40, 24, 1200, 36), [
            para([line(40, 24, 1200, 36, [run(title, sizePx=24.0, weight=700, color=TITLE_INK)])], lh=36.0),
        ]),
    ]
    for index, (name, caption, box, spec) in enumerate(charts, start=1):
        x, y, w, h = box
        area = (spec.get("options") or {}).get("plotArea")
        plot = Box(x + w * area["x"], y + h * area["y"], w * area["w"], h * area["h"]) if area else None
        elements.append(Element(
            kind="chart", box=Box(x, y, w, h), name=name, spec=spec, style=dict(CHART_STYLE),
            origin="authored", confidence=1.0, plotRect=plot, overlay=[],
            source={"path": f"body > [data-name={name}]", "tag": "div", "svg": None},
        ))
        elements.append(text(f"cap{index}", (x, y + h + 6, w, 22), [
            para([line(x, y + h + 6, w, 22, [run(caption, sizePx=13.0, color=CAPTION)])], lh=22.0),
        ]))
    return finish(IR(
        canvas=CANVAS_16X9, fonts=FONTS, elements=elements,
        slide=Slide(id=slide_id, title=title, layoutId="layout-07", notes=notes),
    ))


def ir_charts_hardening() -> IR:
    """WP-C §8.1: every totals spelling, a stacked waterfall, a model-written spec, a reference line."""
    return _chart_family("tor_charts_hardening", "Charts — spellings the engine now reads one way",
                         HARDENING_CHARTS, "Seven authored charts; every spec lints clean.")


def ir_charts_negative() -> IR:
    """WP-C §8.1: everything below or across zero."""
    return _chart_family("tor_charts_negative", "Charts — below and across zero", NEGATIVE_CHARTS,
                         "Five authored charts: bars and bridges below and across zero.")


def ir_paint() -> IR:
    """WP-E: gradient text as a run fill, CSS border joins (a triangle and a trapezoid), an alpha tint."""
    brand, accent, red = "0B5CAD", "1E9E5A", "D64545"
    gradient = {"type": "gradient", "kind": "linear", "angle": 90,
                "stops": [{"pos": 0.0, "color": brand, "alpha": 1.0}, {"pos": 1.0, "color": accent, "alpha": 1.0}]}
    elements = [
        shape("rgba-tint", (40, 50, 160, 110), {"type": "rect"}, fill=solid(accent, 0.12)),
        # Gradient text: the run's own fill lives in the element's extras, keyed "paragraph/line/run";
        # the run's colour is the gradient's middle (0x0B..0x1E -> 0x15, 0x5C..0x9E -> 0x7D, 0xAD..0x5A -> 0x84).
        text("grad-text", (40, 210, 160, 65),
             [para([line(40, 210, 152, 64.8, [run("$12.4B", sizePx=54.0, weight=700, color="157D84")])], lh=64.8)],
             extras={"x-wpe-run-fills": {"0/0/0": gradient}}),
        # The zero-size-box triangle: border-left 14 px solid, top/bottom 8 px transparent.
        shape("tri-right left border", (40, 530, 14, 16),
              {"type": "custom", "fillRule": "nonzero",
               "path": [["M", 40, 546], ["L", 40, 530], ["L", 54, 538], ["Z"]]}, fill=solid(brand)),
        shape("two-sides-box", (820, 530, 160, 110), {"type": "rect"}, fill=solid("F7F7FA")),
        # Where two painted sides meet, each is a trapezoid cut on the corner diagonal.
        shape("two-sides-box top border", (820, 530, 160, 6),
              {"type": "custom", "fillRule": "nonzero",
               "path": [["M", 820, 530], ["L", 980, 530], ["L", 980, 536], ["L", 830, 536], ["Z"]]}, fill=solid(red)),
        shape("two-sides-box left border", (820, 530, 10, 110),
              {"type": "custom", "fillRule": "nonzero",
               "path": [["M", 820, 640], ["L", 820, 530], ["L", 830, 536], ["L", 830, 640], ["Z"]]}, fill=solid(brand)),
    ]
    return finish(IR(
        canvas=CANVAS_16X9, fonts=FONTS, elements=elements,
        slide=Slide(id="tor_paint", title="torture · paint", layoutId="layout-07", notes=None),
    ))


FAMILIES = {
    "text": ir_text,
    "boxes": ir_boxes,
    "images": ir_images,
    "tables": ir_tables,
    "svg-shapes": ir_svg_shapes,
    "svg-paths": ir_svg_paths,
    "placeholders": ir_placeholders,
    "sizes-4x3": ir_sizes_4x3,
    "charts-hardening": ir_charts_hardening,
    "charts-negative": ir_charts_negative,
    "paint": ir_paint,
}


def build() -> list[Path]:
    IR_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, factory in FAMILIES.items():
        ir = factory()
        path = ir.save(IR_DIR / f"{name}.json")
        written.append(path)
        print(f"{name:<14} {len(ir.elements):>2} elements, {len(ir.groups)} group(s), "
              f"{len(ir.diagnostics)} diagnostic(s) -> {path.name}")
    return written


if __name__ == "__main__":
    build()
