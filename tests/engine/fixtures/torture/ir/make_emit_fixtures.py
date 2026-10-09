"""Regenerate the WP4 emitter IR fixtures — `tests/engine/fixtures/torture/ir/emit-*.json`.

The JSON files are the fixtures; this is how they were built and how to rebuild them after an IR
schema migration (`python -m tests.engine.fixtures.torture.ir.make_emit_fixtures` from the repo root).
Line widths come from the same font metrics the browser would measure with, so the width lock is
exercised by the cases that mean to exercise it and not by arithmetic noise. Owner: WP4.
"""
from pathlib import Path

from app.engine.emit.text import text_width_px
from app.engine.ir import IR, Box, Canvas, Diagnostic, Element, Group, Slide, assign_ids_and_z

OUT = Path(__file__).resolve().parent
LOGO = "../assets/logo-160x160.png"
PHOTO = "../assets/photo-400x300.png"


def run(text, *, font="Arial", size=20.0, weight=400, color="2E2E38", **extra):
    base = {"text": text, "font": font, "sizePx": size, "weight": weight, "italic": False,
            "color": color, "alpha": 1, "underline": False, "strike": False,
            "letterSpacingPx": 0, "baseline": "normal", "baselineShiftPx": 0,
            "caps": False, "href": None}
    base.update(extra)
    return base


def line(runs, x, y, lh):
    width = sum(text_width_px(r["text"], r["font"], r["sizePx"], r["weight"], r["italic"])
                + r["letterSpacingPx"] * len(r["text"]) for r in runs)
    return {"box": {"x": x, "y": y, "w": round(width, 3), "h": lh}, "runs": runs}


def para(lines, *, align="left", lh=30.0, before=0.0, after=0.0, bullet=None):
    return {"align": align, "lineHeightPx": lh, "spaceBeforePx": before, "spaceAfterPx": after,
            "bullet": bullet, "lines": lines}


def text(x, y, w, h, paragraphs, **kw):
    return Element(kind="text", box=Box(x, y, w, h), paragraphs=paragraphs,
                   anchor=kw.pop("anchor", "top"), writingMode=kw.pop("writingMode", "horizontal"),
                   wrap=kw.pop("wrap", True), **kw)


def save(name, canvas, layout_id, elements, groups=(), diagnostics=(), notes=None):
    assign_ids_and_z(elements)
    ir = IR(canvas=canvas, slide=Slide(id=name, title=name, layoutId=layout_id, notes=notes),
            elements=elements, groups=list(groups), diagnostics=list(diagnostics))
    problems = ir.validate(check_files=False)
    assert not problems, (name, problems)
    path = OUT / (name + ".json")
    path.write_text(ir.dumps(), encoding="utf-8")
    print(f"{path.name:<28} {len(elements):3d} elements  {len(groups)} groups")


WIDE, TALL = Canvas(1280, 720), Canvas(960, 720)


def solid(colour, alpha=1.0):
    return {"type": "solid", "color": colour, "alpha": alpha}


def stroke(colour="516467", width=1.0, **kw):
    return {"color": colour, "alpha": 1, "width": width, "cap": "butt", "join": "miter", **kw}


# --------------------------------------------------------------------------------- emit-shapes
shapes = [
    Element(kind="shape", box=Box(40, 40, 200, 120), name="rect-solid",
            geometry={"type": "rect"}, fill=solid("1A9AFB", 0.8), stroke=None),
    Element(kind="shape", box=Box(270, 40, 200, 120), name="roundrect-equal",
            geometry={"type": "roundRect", "radius": {"tl": 16, "tr": 16, "br": 16, "bl": 16}},
            fill=solid("F3F3F5"), stroke=stroke("1A9AFB", 1.0, dash=[4, 3])),
    Element(kind="shape", box=Box(500, 40, 200, 120), name="roundrect-unequal",
            geometry={"type": "roundRect", "radius": {"tl": 32, "tr": 0, "br": 32, "bl": 0}},
            fill=solid("B9C7C9"), stroke=stroke("2E2E38", 1.0, dash=[3, 2])),
    Element(kind="shape", box=Box(730, 40, 200, 120), name="ellipse-gradient",
            geometry={"type": "ellipse"},
            fill={"type": "gradient", "kind": "linear", "angle": 90,
                  "stops": [{"pos": 0, "color": "1A9AFB", "alpha": 1},
                            {"pos": 1, "color": "7FC6FF", "alpha": 0.4}]},
            stroke=None),
    Element(kind="shape", box=Box(960, 40, 200, 120), name="rect-radial",
            geometry={"type": "rect"},
            fill={"type": "gradient", "kind": "radial", "angle": 0,
                  "stops": [{"pos": 0, "color": "FFFFFF", "alpha": 1},
                            {"pos": 1, "color": "516467", "alpha": 1}]},
            stroke=None, shadow={"color": "000000", "alpha": 0.25, "blur": 12, "dx": 4, "dy": 6}),

    Element(kind="shape", box=Box(40, 200, 140, 140), name="donut",
            geometry={"type": "blockArc",
                      "arc": {"cx": 110, "cy": 270, "rOuter": 70, "rInner": 42,
                              "startDeg": 270, "endDeg": 90}},
            fill=solid("1A9AFB"), stroke=None),
    Element(kind="shape", box=Box(210, 200, 140, 140), name="pie",
            geometry={"type": "pie", "arc": {"cx": 280, "cy": 270, "rOuter": 70, "rInner": 0,
                                             "startDeg": 0, "endDeg": 120}},
            fill=solid("FFB600"), stroke=None),
    Element(kind="shape", box=Box(380, 200, 140, 140), name="chord",
            geometry={"type": "chord", "arc": {"cx": 450, "cy": 270, "rOuter": 70, "rInner": 0,
                                               "startDeg": 30, "endDeg": 200}},
            fill=solid("C981B2"), stroke=None),
    Element(kind="shape", box=Box(550, 200, 200, 140), name="arrow",
            geometry={"type": "line", "points": [[550, 340], [750, 200]]},
            fill={"type": "none"},
            stroke=stroke("2E2E38", 2.0, cap="round",
                          headEnd={"type": "oval", "size": "med"},
                          tailEnd={"type": "triangle", "size": "med"})),
    Element(kind="shape", box=Box(780, 200, 200, 140), name="polyline",
            geometry={"type": "polyline",
                      "points": [[780, 340], [830, 240], [880, 300], [930, 210], [980, 260]]},
            fill={"type": "none"}, stroke=stroke("1A9AFB", 3.0, join="round", cap="round")),
    Element(kind="shape", box=Box(1010, 200, 150, 140), name="ring-evenodd",
            geometry={"type": "custom", "fillRule": "evenodd",
                      "path": [["M", 1010, 200], ["L", 1160, 200], ["L", 1160, 340],
                               ["L", 1010, 340], ["Z"],
                               ["M", 1050, 240], ["L", 1120, 240], ["L", 1120, 300],
                               ["L", 1050, 300], ["Z"]]},
            fill=solid("2E2E38"), stroke=None),

    Element(kind="shape", box=Box(40, 380, 200, 120), name="rect-clipped",
            geometry={"type": "rect"}, fill=solid("FF6D00"), stroke=None,
            clip=Box(40, 380, 120, 80)),
    Element(kind="shape", box=Box(270, 380, 200, 120), name="ellipse-clipped",
            geometry={"type": "ellipse"}, fill=solid("36BFAF"), stroke=None,
            clip=Box(270, 380, 140, 120)),
    Element(kind="shape", box=Box(500, 380, 200, 120), name="rect-rotated",
            geometry={"type": "rect"}, fill=solid("FF4136"), stroke=None,
            rotation=15.0, flipH=True),
    Element(kind="shape", box=Box(730, 380, 200, 120), name="dash-sysdot",
            geometry={"type": "rect"}, fill={"type": "none"},
            stroke=stroke("2E2E38", 1.0, dash=[1, 1])),
    Element(kind="shape", box=Box(960, 380, 200, 120), name="dash-custom",
            geometry={"type": "rect"}, fill={"type": "none"},
            stroke=stroke("2E2E38", 1.0, dash=[30, 20])),

    Element(kind="shape", box=Box(40, 540, 120, 120), name="g-child-1", group="g1",
            geometry={"type": "rect"}, fill=solid("1A9AFB"), stroke=None),
    Element(kind="shape", box=Box(180, 540, 120, 120), name="g-child-2", group="g1",
            geometry={"type": "rect"}, fill=solid("7FC6FF"), stroke=None),
    Element(kind="shape", box=Box(320, 560, 80, 80), name="g-nested-child", group="g2",
            geometry={"type": "ellipse"}, fill=solid("FFB600"), stroke=None),
]
save("emit-shapes", WIDE, "layout-07", shapes,
     groups=[Group(id="g1", parent=None, name="value-chain", box=Box(40, 540, 360, 120)),
             Group(id="g2", parent="g1", name="badge", box=Box(320, 560, 80, 80))])

# ----------------------------------------------------------------------------------- emit-text
texts = [
    text(64, 40, 560, 100, [
        para([line([run("A paragraph the browser broke across")], 64, 40, 30),
              line([run("exactly two lines.")], 64, 70, 30)], lh=30, after=12),
        para([line([run("A bulleted point.")], 82, 112, 30)], lh=30, before=12,
             bullet={"type": "char", "char": "•", "level": 0, "indentPx": 18,
                     "color": "1A9AFB"}),
    ], name="paragraphs"),
    text(660, 40, 560, 60,
         [para([line([run("Centred on a 3.0 line-height")], 810, 40, 60)], align="center", lh=60)],
         name="line-height-3"),
    text(660, 120, 560, 20,
         [para([line([run("Right, line-height 1.0")], 1050, 120, 20)], align="right", lh=20)],
         name="line-height-1"),
    text(64, 200, 900, 40, [para([line([
        run("Mixed "),
        run("bold", weight=700), run(" "),
        run("italic", italic=True), run(" "),
        run("underline", underline=True), run(" "),
        run("strike", strike=True), run(" "),
        run("wide", letterSpacingPx=2.5), run(" x"),
        run("2", baseline="super", size=12.0), run(" and H"),
        run("2", baseline="sub", size=12.0), run("O "),
        run("link", color="1A9AFB", href="https://example.com/a"),
    ], 64, 200, 40)], lh=40)], name="mixed-runs"),
    text(64, 280, 500, 100, [
        para([line([run("First numbered item.")], 82, 280, 30)], lh=30,
             bullet={"type": "num", "char": "1.", "level": 0, "indentPx": 18,
                     "color": "2E2E38", "start": 1}),
        para([line([run("Second numbered item.")], 82, 310, 30)], lh=30,
             bullet={"type": "num", "char": "2.", "level": 0, "indentPx": 18,
                     "color": "2E2E38", "start": 1}),
    ], name="numbered"),
    # A vertical label's measured boxes are tall and narrow: the text runs along y, the line
    # thickness along x. `line()` measures along x, so this one is transposed by hand.
    text(700, 280, 24, 160,
         [{"align": "left", "lineHeightPx": 24, "spaceBeforePx": 0, "spaceAfterPx": 0,
           "bullet": None,
           "lines": [{"box": {"x": 700, "y": 280, "w": 24,
                              "h": round(text_width_px("Vertical axis label", "Arial", 16.0,
                                                       400, False), 3)},
                      "runs": [run("Vertical axis label", size=16.0)]}]}],
         writingMode="vertical", name="vertical"),
    text(64, 420, 400, 30,
         [para([line([run("rotated banner", size=18.0)], 64, 420, 30)], lh=30)],
         rotation=-6.0, name="rotated"),
    text(64, 500, 600, 40,
         [para([line([run("shouty small print", size=14.0)], 64, 500, 40)], lh=40)],
         transformCase="upper", name="uppercase"),
    text(64, 580, 600, 34,
         [para([line([run("Anchored middle in its own box", size=22.0)], 64, 580, 34)], lh=34)],
         anchor="middle", name="anchored"),
]
save("emit-text", WIDE, "layout-07", texts,
     notes="Speaker notes ride along with the slide.")

# --------------------------------------------------------------------------- emit-placeholders
placeholders = [
    Element(kind="shape", box=Box(0, 0, 1280, 20), name="rule",
            geometry={"type": "rect"}, fill=solid("1A9AFB"), stroke=None),
    text(48, 29, 864, 62,
         [para([line([run("Explicitly the title", size=40.0, weight=700)], 48, 29, 62)], lh=62)],
         name="title", extras={"x-wp4-placeholder": "title"}),
    text(60, 200, 700, 60,
         [para([line([run("Body text that overlaps the content zone")], 60, 200, 30)], lh=30),
          para([line([run("by more than sixty per cent.")], 60, 230, 30)], lh=30)],
         name="body-by-overlap"),
    text(980, 660, 260, 24,
         [para([line([run("free caption", size=14.0)], 980, 660, 24)], lh=24)],
         name="free-caption"),
]
save("emit-placeholders", WIDE, "layout-02", placeholders)

# --------------------------------------------------------------------------------- emit-images
images = [
    Element(kind="image", box=Box(40, 40, 300, 200), name="photo-cover", src=PHOTO, fit="cover"),
    Element(kind="image", box=Box(380, 40, 300, 200), name="photo-contain", src=PHOTO,
            fit="contain"),
    Element(kind="image", box=Box(720, 40, 200, 200), name="logo-circle", src=LOGO,
            fit="fill", circle=True),
    Element(kind="image", box=Box(960, 40, 240, 100), name="logo-rounded", src=LOGO,
            fit="fill", radius=16.0, opacity=0.6),
    Element(kind="image", box=Box(40, 300, 314, 102), name="logo-cropped", src=LOGO,
            fit="fill", crop={"l": 0.1, "t": 0.0, "r": 0.1, "b": 0.0}),
    Element(kind="image", box=Box(400, 300, 314, 102), name="logo-clipped", src=LOGO,
            fit="fill", clip=Box(400, 300, 160, 102)),
    Element(kind="raster", box=Box(760, 300, 200, 120), name="filtered-badge", src=LOGO,
            reason="svg filter: feGaussianBlur"),
]
save("emit-images", WIDE, "layout-07", images,
     diagnostics=[Diagnostic(level="warn", source="svg#1 > filter",
                             message="filter rasterised", elementId="e7")])


# --------------------------------------------------------------------------------- emit-tables
def cell(r, c, label, head=False, span=1, fill=None):
    return {"r": r, "c": c, "rowSpan": 1, "colSpan": span,
            "fill": {"type": "solid", "color": fill, "alpha": 1} if fill else None,
            "borders": {"top": None, "right": None, "left": None,
                        "bottom": {"width": 1, "color": "B9C7C9", "dash": "solid"}},
            "paddingPx": {"t": 6, "r": 8, "b": 6, "l": 8}, "valign": "middle",
            "paragraphs": [para([line([run(label, size=16.0, weight=700 if head else 400)],
                                      0, 0, 22)], lh=22)]}


table = Element(kind="table", box=Box(64, 80, 800, 240), name="summary", rows=3, cols=3,
                colWidthsPx=[320.0, 240.0, 240.0], rowHeightsPx=[60.0, 90.0, 90.0],
                cells=[cell(0, 0, "Market", head=True, fill="F3F3F5"),
                       cell(0, 1, "2023", head=True, fill="F3F3F5"),
                       cell(0, 2, "FY27E", head=True, fill="F3F3F5"),
                       cell(1, 0, "Internal spans two columns", span=2),
                       cell(1, 2, "80"),
                       cell(2, 0, "Partner"), cell(2, 1, "26"), cell(2, 2, "70")])
save("emit-tables", WIDE, "layout-07", [table])

# --------------------------------------------------------------------------------- emit-charts
chart = Element(kind="chart", box=Box(96, 120, 560, 320), name="growth",
                spec={"type": "column_stacked",
                      "categories": ["2019", "2021", "2022", "2023"],
                      "series": [{"name": "Internal", "values": [40, 52, 57, 71]},
                                 {"name": "Partner", "values": [12, 9, 21, 18]}],
                      "colors": ["#B9C7C9", "#1A9AFB"],
                      "options": {"dataLabels": True, "legend": False, "gapWidth": 60}},
                style={"font": "Arial", "sizePx": 11, "color": "516467"},
                origin="authored", confidence=1.0,
                plotRect=Box(140, 136, 470, 250), overlay=["e2"])
annotation = text(700, 140, 400, 30,
                  [para([line([run("Annotation drawn over the chart", size=16.0)], 700, 140, 24)],
                        lh=24)], name="callout")
save("emit-charts", WIDE, "layout-07", [chart, annotation])

# ------------------------------------------------------------------------------ emit-sizes-4x3
small = [
    Element(kind="shape", box=Box(48, 180, 400, 160), name="panel",
            geometry={"type": "roundRect", "radius": {"tl": 12, "tr": 12, "br": 12, "bl": 12}},
            fill=solid("F3F3F5"), stroke=stroke("B9C7C9", 1.0)),
    Element(kind="shape", box=Box(520, 180, 160, 160), name="donut-4x3",
            geometry={"type": "blockArc", "arc": {"cx": 600, "cy": 260, "rOuter": 80, "rInner": 48,
                                                  "startDeg": 0, "endDeg": 270}},
            fill=solid("1A9AFB"), stroke=None),
    text(48, 29, 864, 62,
         [para([line([run("Four by three, same rules", size=36.0, weight=700)], 48, 29, 62)],
               lh=62)],
         name="title-4x3", extras={"x-wp4-placeholder": "title"}),
    text(72, 210, 350, 90,
         [para([line([run("The canvas is 960 x 720; nothing")], 72, 210, 30),
                line([run("else about the rules changes.")], 72, 240, 30)], lh=30)],
         name="copy-4x3"),
]
save("emit-sizes-4x3", TALL, "layout-06", small)

# ----------------------------------------------------------------------------------- emit-wrap
# Fidelity WP-D (F4): a line the browser broke, and the emitter locked with `a:br`, must never be
# broken a second time by PowerPoint or PptxRender. That only happens when the export cannot see
# the font the renderer draws, so this fixture means something only under **blind fonts**
# (`config.FONT_DIRS` → an empty directory: `test_emit._blind_fonts`, or
# `SLIDE_ENGINE_FONT_DIRS=<empty dir>`).
# There the emitter has no face at all, keeps `predicted = measured` and adds no tracking, while
# both renderers draw real Arial Bold.
#
# It is deliberately NOT in test_emit's ALL_FIXTURES: under the normal fonts the title would be
# tracked to the -0.12 em cap with two "tracking capped" warnings, and
# `test_every_fixture_emits_without_warnings` requires none.
#
# `title-edge` is benchmark slide 51's title as the browser broke it (01-FINDINGS F4), in Arial Bold
# 31 px on p09's geometry (x 24, w 1232), with line height 40 so line 1's descenders never merge
# with line 2's ascenders. The browser widths are 0.72 x the real ones; any factor below
# 1232 / 1316 = 0.936 gives the same box (max(1232, 948) x 1.02, right growth clipped at the canvas:
# 1256 px from x 24 to the edge), so the number is not tuned. The renderers draw line 1 at 1316 px:
# with wrap="square" it re-wraps at "versus" and "balance-" gets a line of its own, as on slide 51.
WRAP_SQUEEZE = 0.72


def squeezed(runs, x, y, lh, factor=WRAP_SQUEEZE):
    measured = line(runs, x, y, lh)
    measured["box"]["w"] = round(factor * sum(
        text_width_px(r["text"], r["font"], r["sizePx"], r["weight"], r["italic"]) for r in runs), 3)
    return measured


def centred(runs, left, width, y, lh):
    measured = line(runs, left, y, lh)
    measured["box"]["x"] = round(left + (width - measured["box"]["w"]) / 2.0, 3)
    return measured


TITLE_EDGE = ("Independence and cross-border reach create a differentiated proposition versus balance-",
              "sheet banks")
BODY_COLUMN = ("Balance-sheet lenders carry higher", "capital charges on mid-market loans,",
               "while private-credit funds can hold", "concentrated positions and price for",
               "certainty of execution rather than", "for the tightest possible spread.")
CARD = ("Certainty of execution", "beats price for borrowers")
wrapping = [
    text(24, 18, 1232, 80,
         [para([squeezed([run(TITLE_EDGE[0], size=31.0, weight=700)], 24, 18, 40),
                squeezed([run(TITLE_EDGE[1], size=31.0, weight=700)], 24, 58, 40)], lh=40)],
         name="title-edge", extras={"x-wp4-placeholder": "title"}),
    # The ordinary case: every line narrower than its box, so it renders the same wrapped or not.
    text(64, 200, 300, 120,
         [para([line([run(words, size=15.0)], 64, 200 + 20 * index, 20)
                for index, words in enumerate(BODY_COLUMN)], lh=20)],
         name="body-column"),
    text(700, 200, 280, 40,
         [para([centred([run(words, size=14.0, weight=700)], 700, 280, 200 + 20 * index, 20)
                for index, words in enumerate(CARD)], align="center", lh=20)],
         name="centred-card"),
    # bad2f9b's chip: a 0.2 in box holding a one-line "20%" 28 px wide — a wrap-off one-liner for
    # the fit check's overhang rule to look at.
    text(1000, 400, 19.2, 18,
         [para([line([run("20%", size=14.0)], 1000, 400, 18)], lh=18)],
         name="chip"),
]
save("emit-wrap", WIDE, "layout-06", wrapping)

# ---------------------------------------------------------------------------- emit-tables-rich
# WP-A (docs/archive/engine/fidelity/10-WPA-tables.md §8.3): one table whose `x-wpa` extras exercise every
# cell rule of the emitter — an overlay-only cell, a first-line indent, bullets, the width lock, a
# measured paragraph gap, a dashed rule, a translucent fill, a slot offset, a nowrap line, a measured
# left margin (`marginPx`: a grid column or a `margin-left`) and a bullet whose text starts after a
# chip — with a gradient shape behind it and a chip + chip text over it. Cells without `marginPx`
# keep the old reading (the bullet's own hanging indent, a first-line indent at `marL = 0`). Line
# boxes sit where the browser would have put them (slot top + slot.t + padding for top-anchored
# cells), so the margin model's measured correction is zero and Δ is the model alone.
RICH_X, RICH_Y = 64.0, 120.0
RICH_COLS, RICH_ROWS = [240.0, 240.0, 240.0, 240.0], [60.0, 70.0, 44.0]
RICH_PAD = {"t": 8, "r": 10, "b": 8, "l": 10}


def rich_para(texts, r, c, *, slot_t=0.0, slot_l=0.0, indent=0.0, before=0.0, top=0.0, narrower=0.0,
              bullet=None, lh=22.0, left=0.0, **run_kw):
    """One paragraph at its cell's content box (+ `left`), a line per text, the first line `top` px lower."""
    x = RICH_X + sum(RICH_COLS[:c]) + RICH_PAD["l"] + slot_l + left
    y = RICH_Y + sum(RICH_ROWS[:r]) + slot_t + RICH_PAD["t"] + top
    lines = []
    for index, text_ in enumerate(texts):
        drawn = line([run(text_, font="Calibri", size=14.667, **run_kw)], x + (indent if index == 0 else 0.0),
                     y + index * lh, lh)
        drawn["box"]["w"] = round(drawn["box"]["w"] - narrower, 3)
        lines.append(drawn)
    return para(lines, lh=lh, before=before, bullet=bullet)


def rich_cell(r, c, paragraphs, *, fill=None, bottom=None):
    return {"r": r, "c": c, "rowSpan": 1, "colSpan": 1, "fill": fill or {"type": "none"},
            "borders": {"top": None, "right": None, "left": None,
                        "bottom": bottom or {"width": 1, "color": "D8DDE6", "dash": "solid"}},
            "paddingPx": dict(RICH_PAD), "valign": "top", "paragraphs": paragraphs}


def rich_extras(flow="text", indent=(), slot=None, wrap=True, overlays=0, fill_from="none", margin=None):
    extras = {"fillFrom": fill_from, "flow": flow, "indentPx": list(indent),
              "slot": slot or {"t": 0, "r": 0, "b": 0, "l": 0}, "wrap": wrap, "overlays": overlays, "bands": 0}
    if margin is not None:
        extras["marginPx"] = list(margin)
    return extras


BULLET = {"type": "char", "char": "•", "level": 0, "indentPx": 18, "color": "2E2E38", "start": 1}
SLOT2 = {"t": 2, "r": 2, "b": 2, "l": 2}
rich_cells = [
    rich_cell(0, 0, []),
    rich_cell(0, 1, [rich_para(["Singapore"], 0, 1, indent=22.5)]),
    rich_cell(0, 2, [rich_para(["Sandbox"], 0, 2, bullet=BULLET),
                     rich_para(["Talent visas"], 0, 2, top=22.0, bullet=BULLET)]),
    rich_cell(0, 3, [rich_para(["Width locked line"], 0, 3, narrower=6.0)]),
    rich_cell(1, 0, [rich_para(["First paragraph"], 1, 0),
                     rich_para(["After a 6 px gap"], 1, 0, top=28.0, before=6.0)]),
    rich_cell(1, 1, [rich_para(["Dashed rule below"], 1, 1)],
              bottom={"width": 1, "color": "516467", "dash": [4, 3]}),
    rich_cell(1, 2, [rich_para(["Translucent fill"], 1, 2)], fill=solid("1A9AFB", 0.2)),
    rich_cell(1, 3, [rich_para(["Slot offset"], 1, 3, slot_t=2.0, slot_l=2.0)]),
    rich_cell(2, 0, [rich_para(["One line, never wrapped"], 2, 0)]),
    rich_cell(2, 1, [rich_para(["In a column"], 2, 1, left=60.0)]),
    rich_cell(2, 2, [rich_para(["Over a gradient"], 2, 2)]),
    rich_cell(2, 3, [rich_para(["After a chip"], 2, 3, left=40.0, bullet=BULLET)]),
]
rich_wpa = {"v": 1, "collapse": True, "spacingPx": {"h": 0, "v": 0}, "decorated": True, "grouped": False,
            "cells": {"0:0": rich_extras(flow="none", overlays=2), "0:1": rich_extras(indent=[22.5]),
                      "0:2": rich_extras(indent=[0, 0]), "0:3": rich_extras(indent=[0]),
                      "1:0": rich_extras(indent=[0, 0]), "1:1": rich_extras(indent=[0]),
                      "1:2": rich_extras(indent=[0], fill_from="row"), "1:3": rich_extras(indent=[0], slot=SLOT2),
                      "2:0": rich_extras(indent=[0], wrap=False), "2:1": rich_extras(indent=[0], margin=[60.0]),
                      "2:2": rich_extras(indent=[0]), "2:3": rich_extras(indent=[-40.0], margin=[40.0])}}


def overlay(role, cell_rc):
    return {"x-wpa": {"cell": list(cell_rc) if cell_rc else None, "table": "rich-cells", "role": role}}


rich = [
    Element(kind="shape", box=Box(RICH_X + 480, RICH_Y + 130, 240, 44), name="rich-cells r2c2 cellGradient",
            geometry={"type": "rect"}, stroke=None,
            fill={"type": "gradient", "kind": "linear", "angle": 90,
                  "stops": [{"pos": 0, "color": "EAF2FF", "alpha": 1}, {"pos": 1, "color": "FFFFFF", "alpha": 1}]},
            extras=overlay("cellGradient", (2, 2))),
    Element(kind="table", box=Box(RICH_X, RICH_Y, sum(RICH_COLS), sum(RICH_ROWS)), name="rich-cells",
            rows=3, cols=4, colWidthsPx=RICH_COLS, rowHeightsPx=RICH_ROWS, cells=rich_cells, opacity=0.9,
            extras={"x-wpa": rich_wpa}),
    Element(kind="shape", box=Box(RICH_X + 10, RICH_Y + 8, 60, 18), name="rich-cells r0c0 chip",
            geometry={"type": "roundRect", "radius": {"tl": 9, "tr": 9, "br": 9, "bl": 9}},
            fill=solid("1E9E5A"), stroke=None, extras=overlay("chip", (0, 0))),
    text(RICH_X + 18, RICH_Y + 9, 44, 16,
         [para([line([run("Leader", font="Calibri", size=12.0, weight=700, color="FFFFFF")],
                      RICH_X + 18, RICH_Y + 9, 16)], lh=16)],
         wrap=False, name="rich-cells r0c0 chipText", extras=overlay("chipText", (0, 0))),
]
save("emit-tables-rich", WIDE, "layout-07", rich)
