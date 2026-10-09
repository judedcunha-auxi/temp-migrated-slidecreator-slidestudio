"""WP-F — the page is measured as the reader sees it (`docs/archive/engine/fidelity/15-WPF-as-seen.md`).

Five things a browser shows differently from what a naive walk reads, each with its fixture:

* **Animations** (F-A): measured at rest — finished, a looping one at its base state — never
  mid-flight, and said out loud (`settle.js`).
* **SVG `foreignObject`** (F-B): a picture of its text, not a blank PNG.
* **CSS columns** (F-C): one text element per column box, in reading order, never interleaved.
* **Clipped and clamped text** (F-D): only what is visible, plus the browser's own ellipsis, and an
  error that says what was hidden; a block that is entirely hidden is still reported.
* **Remote resources** (F-E): blocked by policy at export, or rasterised from the browser's own
  rendering when `SLIDE_ENGINE_REMOTE_RESOURCES=raster` — an explained absence either way.

The torture family `as-seen` carries A–D; `tests/engine/fixtures/wpf/edges.html` the edge cases, and
`tests/engine/fixtures/wpf/remote.html` the remote cases, which break the authoring contract on purpose and
so cannot be a torture family (its invariants forbid remote URLs). This file is the permanent home of
probe p07's remote-image case once `probes/` is retired (plan D8).
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

import pytest
from PIL import Image

from app.config import engine as config
from app.engine.extract.html import measuring_page, prepare_page, render_reference
from app.engine.ir import IR, Canvas, Element
from app.engine.manifest import Manifest
from app.engine.verify.fixtures import FixtureContext, context_for
from tests.engine.helpers import extract_html

AS_SEEN = config.TORTURE_FIXTURE / "as-seen.html"
WPF = Path(__file__).resolve().parents[1] / "fixtures" / "wpf"
EDGES = WPF / "edges.html"
REMOTE = WPF / "remote.html"


# ------------------------------------------------------------------------------------- helpers


def _context(html: Path) -> FixtureContext:
    """A torture slide names its master; the package-private `wpf/` slides sit on `test-16x9`."""
    if html.parent == WPF:
        stem = "test-16x9"
        return FixtureContext(
            html=html, manifest=Manifest.load(config.MASTERS_FIXTURE / f"{stem}.manifest.json"),
            layout_id="layout-07", master=config.MASTERS_FIXTURE / f"{stem}.pptx",
            assets_dir=config.TORTURE_FIXTURE / "assets", layouts_dir=config.MASTERS_FIXTURE / stem / "layouts",
        )
    return context_for(html)


def _extract(html: Path, slide_id: str, workspace: Path | None = None) -> IR:
    context = _context(html)
    canvas = Canvas(context.manifest.canvas_w, context.manifest.canvas_h)
    with measuring_page(canvas) as page:
        return extract_html(html, context.manifest, context.layout_id, context.assets_dir,
                            slide_id=slide_id, title=slide_id, page=page, workspace=workspace)


@pytest.fixture(scope="module")
def as_seen(browser) -> IR:
    return _extract(AS_SEEN, "wpf-as-seen")


@pytest.fixture(scope="module")
def edges(browser) -> IR:
    return _extract(EDGES, "wpf-edges")


def _in_browser(html: Path, script: str, arg=None):
    """Evaluate `script` on the slide exactly as extraction measures it (fonts forced, settled)."""
    context = _context(html)
    with measuring_page(Canvas(context.manifest.canvas_w, context.manifest.canvas_h)) as page:
        prepare_page(page, html, context.manifest, context.assets_dir)
        return page.evaluate(script, arg)


#: Per-character right edges and x-centres of one element's text, the ellipsis width in its style,
#: and its content box — what the ellipsis and x-centre rules are judged against.
_GEOMETRY = """(sel) => {
    const el = document.querySelector(sel), cs = getComputedStyle(el), range = document.createRange();
    const chars = [], walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode()))
        for (let i = 0; i < node.data.length; i++) {
            range.setStart(node, i); range.setEnd(node, i + 1);
            const r = Array.from(range.getClientRects()).find(r => r.width > 0);
            if (r) chars.push({c: node.data[i], left: r.left, right: r.right, cx: r.left + r.width / 2});
        }
    const probe = document.createElement('span');
    probe.style.cssText = 'position:absolute;left:-10000px;visibility:hidden;white-space:pre';
    for (const k of ['fontStyle', 'fontWeight', 'fontSize', 'fontFamily', 'letterSpacing']) probe.style[k] = cs[k];
    probe.textContent = '\\u2026';
    document.body.appendChild(probe);
    const ellipsis = probe.getBoundingClientRect().width;
    probe.remove();
    const b = el.getBoundingClientRect();
    const left = b.left + parseFloat(cs.borderLeftWidth) + parseFloat(cs.paddingLeft);
    const right = b.right - parseFloat(cs.borderRightWidth) - parseFloat(cs.paddingRight);
    return {chars, ellipsis, left, right};
}"""


def _by_source(ir: IR, needle: str, kind: str | None = None) -> list[Element]:
    return [e for e in ir.elements
            if needle in (e.source.get("path") or "") and (kind is None or e.kind == kind)]


def _named(ir: IR, name: str) -> Element:
    found = [e for e in ir.elements if e.name == name]
    assert found, f"no element named {name!r}; have {[e.name for e in ir.elements]}"
    return found[0]


def _text_of(element: Element) -> str:
    return " ".join("".join(run["text"] for run in line["runs"])
                    for paragraph in element.paragraphs or [] for line in paragraph["lines"])


def _messages(ir: IR, level: str | None = None) -> list[str]:
    return [d.message for d in ir.diagnostics if level is None or d.level == level]


def _lab(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    """sRGB → CIELAB (D65), for a ΔE a person would recognise."""
    def linear(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (linear(c) for c in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116
    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


def _delta_e(a: tuple[float, float, float], hex_colour: str) -> float:
    b = tuple(int(hex_colour[i:i + 2], 16) for i in (0, 2, 4))
    la, lb = _lab(a), _lab(b)  # type: ignore[arg-type]
    return sum((p - q) ** 2 for p, q in zip(la, lb, strict=False)) ** 0.5


def _patch_median(image: Image.Image, x0: int, y0: int, size: int = 10) -> tuple[float, float, float]:
    pixels = [image.getpixel((x, y)) for x in range(x0, x0 + size) for y in range(y0, y0 + size)]
    return tuple(statistics.median(p[i] for p in pixels) for i in range(3))  # type: ignore[return-value]


def _ink(path: str | Path, alpha_min: int = 9) -> int:
    return sum(1 for a in Image.open(path).convert("RGBA").getchannel("A").tobytes() if a >= alpha_min)


def _pixels_like(path: str | Path, hex_colour: str, tolerance: int = 8) -> int:
    """Opaque-ish pixels within `tolerance` of a colour, per channel."""
    want = [int(hex_colour[i:i + 2], 16) for i in (0, 2, 4)]
    raw = Image.open(path).convert("RGBA").tobytes()
    return sum(1 for i in range(0, len(raw), 4)
               if raw[i + 3] > 128 and all(abs(raw[i + c] - want[c]) < tolerance for c in range(3)))


def _reference(html: Path, out: Path) -> Path:
    """`render_reference` for a torture slide, in the master's fonts (G-1's F6, D5)."""
    context = context_for(html)
    return render_reference(html, context.layout_png, out, assets_dir=context.assets_dir,
                            fonts=dict(context.manifest.fonts))


# ---------------------------------------------------------------------------- F-A animations


def test_animations_are_measured_at_rest(as_seen: IR):
    """p09 B exported its cards at opacity 0.91 / 0.16 / 0 / 0 and 1–16 px low, differently each run."""
    cards = [e for e in as_seen.elements if e.name in {"card1", "card2", "card3", "card4"}]
    shapes = [c for c in cards if c.kind == "shape"]
    texts = [c for c in cards if c.kind == "text"]
    assert len(shapes) == 4 and len(texts) == 4
    for shape in shapes:
        assert shape.box.y == pytest.approx(130.0, abs=0.01), "a fade-up measured mid-flight sits low"
        assert shape.opacity == pytest.approx(1.0)
    for text in texts:
        assert text.box.y == pytest.approx(144.0, abs=0.5)
        assert text.opacity == pytest.approx(1.0)
    assert _named(as_seen, "reveal").opacity == pytest.approx(1.0), "a forwards-only reveal ends visible"
    assert _named(as_seen, "dot").opacity == pytest.approx(1.0), "a cancelled loop is at its base style"


def test_settle_reports_what_it_did(browser):
    context = context_for(AS_SEEN)
    canvas = Canvas(context.manifest.canvas_w, context.manifest.canvas_h)
    with measuring_page(canvas) as page:
        report = prepare_page(page, AS_SEEN, context.manifest, context.assets_dir)["settle"]
        again = page.evaluate("() => window.__engineSettle()")
    assert report["animations"] == 6
    assert report["finished"] == 5, "four cards and the reveal are at their end state"
    assert [entry["name"] for entry in report["cancelled"]] == ["pulse"]
    assert report["cancelled"][0]["path"].endswith("div#dot")
    assert report["stillRunning"] == 0 and report["transitions"] == 0 and report["paused"] == 0
    assert again["repeat"] is True and again["finished"] == 0, "settling twice does nothing the second time"


def test_settle_diagnostics_say_what_moved(as_seen: IR):
    infos = [m for m in _messages(as_seen, "info") if "settled" in m]
    assert infos == ["settled 5 CSS animation(s) and 0 transition(s) before measuring; "
                     "the export shows their end state"]
    loops = [m for m in _messages(as_seen, "warn") if "looping animation" in m]
    assert len(loops) == 1 and "pulse" in loops[0] and "base (un-animated) state" in loops[0]
    assert not [m for m in _messages(as_seen) if "still running" in m]


def test_the_walk_trips_on_a_page_nobody_settled(browser):
    """A caller that skips `prepare_page` must not get a quiet IR from a moving page."""
    from app.engine.extract.html import COLOR_JS, PAGE_JS

    with measuring_page(Canvas(1280, 720)) as page:
        page.goto(AS_SEEN.as_uri(), wait_until="domcontentloaded")
        page.add_script_tag(path=str(COLOR_JS))   # WP-E: page.js reads every colour through it
        page.add_script_tag(path=str(PAGE_JS))
        result = page.evaluate("() => window.__engineExtract({canvasW: 1280, canvasH: 720})")
    tripped = [d for d in result["diagnostics"] if "still running" in d["message"]]
    assert tripped and tripped[0]["level"] == "error"
    assert "not settled" in tripped[0]["message"]


def test_extraction_at_rest_is_deterministic(tmp_path: Path, browser):
    """Two extractions into the same workspace give the same IR, derived image paths included."""
    first = _extract(AS_SEEN, "wpf-as-seen", tmp_path)
    again = _extract(AS_SEEN, "wpf-as-seen", tmp_path)
    assert json.dumps(again.to_json(), sort_keys=True) == json.dumps(first.to_json(), sort_keys=True)


def test_the_reference_is_rendered_at_rest(tmp_path: Path, browser):
    """The gate's reference used to sleep 50 ms and photograph the fade mid-flight."""
    out = _reference(AS_SEEN, tmp_path / "as-seen.png")
    image = Image.open(out).convert("RGB")
    # 6 px inside the fourth card's bottom-right corner (card at 460,130, 130 x 100): the last card
    # to animate, with a 0.9 s delay — at 50 ms it was not there at all.
    assert _delta_e(_patch_median(image, 590 - 6 - 10, 230 - 6 - 10), "0B2545") < 5
    # The reveal (600,170, 160 x 60) has a base opacity of 0 and ends at 1.
    assert _delta_e(_patch_median(image, 760 - 6 - 10, 230 - 6 - 10), "1A9AFA") < 5


# ------------------------------------------------------------------------- F-B foreignObject


def test_foreign_object_is_a_picture_of_its_text(as_seen: IR):
    rasters = [e for e in as_seen.elements if e.kind == "raster"]
    assert [r.reason for r in rasters] == ["svg foreignObject"]
    png = rasters[0].src
    assert _ink(png) > 200, "the capture of a foreignObject under a hidden svg root was blank"
    assert _pixels_like(png, "FF4136") == 0, "the sibling rect leaked into the isolated capture"


# ------------------------------------------------------------------------------ F-C columns


def _squash(text: str) -> str:
    """Whitespace-free, so a hyphen at a column break compares equal to the source's."""
    return "".join(text.split())


def _source_text(html: Path, element_id: str) -> str:
    return _in_browser(html, "(id) => document.getElementById(id).textContent", element_id)


def _columns(ir: IR, name: str) -> list[Element]:
    found = [e for e in ir.elements if e.kind == "text" and (e.name or "").startswith(name + " col ")]
    return sorted(found, key=lambda e: e.extras["x-wpf-column"]["index"])


def test_columns_export_one_text_box_per_column_in_reading_order(as_seen: IR):
    """p10 B: every exported line was a left-column line glued to a right-column one."""
    left, right = _columns(as_seen, "cols")
    assert [c.extras["x-wpf-column"] for c in (left, right)] == [{"index": 0, "count": 2}, {"index": 1, "count": 2}]
    assert left.box.x == pytest.approx(40.0, abs=0.5) and right.box.x == pytest.approx(312.0, abs=0.5)
    assert left.box.w == pytest.approx(248.0, abs=0.5) and right.box.w == pytest.approx(248.0, abs=0.5)
    assert [len(c.paragraphs[0]["lines"]) for c in (left, right)] == [6, 5]
    assert _squash(_text_of(left) + _text_of(right)) == _squash(_source_text(AS_SEEN, "cols"))


def test_a_paragraph_across_a_column_break_is_one_paragraph_in_each(as_seen: IR):
    three = _columns(as_seen, "cols3")
    assert [c.extras["x-wpf-column"]["index"] for c in three] == [0, 1, 2]
    assert [len(c.paragraphs) for c in three] == [1, 1, 1]
    first = _squash(_in_browser(AS_SEEN, "() => document.querySelector('#cols3 p').textContent"))
    assert _squash(_text_of(three[0]) + _text_of(three[1])) == first, "the straddling paragraph, split once"
    assert _squash("".join(_text_of(c) for c in three)) == _squash(_source_text(AS_SEEN, "cols3"))
    assert [m for m in _messages(as_seen, "info") if m.startswith("CSS columns")] == [
        "CSS columns: div#cols exported as 2 text boxes, one per column",
        "CSS columns: div#cols3 exported as 3 text boxes, one per column",
    ]


def test_one_line_per_column_on_one_top_is_two_lines(edges: IR):
    """Both halves of a balanced sentence share a top: only the column tells them apart."""
    halves = _columns(edges, "g")
    assert len(halves) == 2 and all(len(h.paragraphs[0]["lines"]) == 1 for h in halves)
    assert halves[0].box.y == pytest.approx(halves[1].box.y, abs=0.5)
    assert halves[0].box.x + halves[0].box.w <= halves[1].box.x, "the halves overlap"
    assert _squash(_text_of(halves[0]) + _text_of(halves[1])) == _squash(_source_text(EDGES, "g"))


def test_a_spanner_is_its_own_full_width_element(edges: IR):
    spanner = [e for e in edges.elements if e.kind == "text" and e.name == "h"]
    assert len(spanner) == 1 and _text_of(spanner[0]) == "Spanner"
    assert spanner[0].box.w == pytest.approx(520.0, abs=0.5)
    columns = _columns(edges, "h")
    assert len(columns) == 2 and not any("Spanner" in _text_of(c) for c in columns)


def test_overflow_columns_are_hidden_by_a_clip_or_exported_beside_the_box(edges: IR):
    hidden = _columns(edges, "ihidden")
    assert [c.extras["x-wpf-column"]["index"] for c in hidden] == [0, 1], "nothing beyond band 1"
    assert any(d.message.startswith("text is clipped") and d.elementId == hidden[0].id for d in edges.diagnostics)
    visible = _columns(edges, "ivisible")
    assert [c.extras["x-wpf-column"]["index"] for c in visible] == [0, 1, 2]
    assert visible[2].box.x >= 400.0, "the overflow column sits to the right of the 360 px box"
    assert any(d.level == "warn" and "outside" in d.message and "ivisible" in d.message for d in edges.diagnostics)


def test_a_decorated_block_split_across_columns_is_reported(edges: IR):
    assert any(d.level == "warn" and "fragmented across" in d.message for d in edges.diagnostics)


def test_paragraphs_down_one_column_flow_are_one_element_per_column(edges: IR):
    """Rebased on WP-B: `stacked()` judges a paragraph continued into the next column by its union
    box, which spans the whole flow, and saw it *beside* the paragraph before — four elements for (l).
    Down the flow they stack (`sameColumnFlow`), so each column box is still one element."""
    halves = _columns(edges, "l")
    assert [c.extras["x-wpf-column"]["index"] for c in halves] == [0, 1]
    assert [e.name for e in edges.elements if e.kind == "text" and (e.name or "").startswith("l ")] == [
        "l col 1", "l col 2"]
    assert [len(c.paragraphs) for c in halves] == [2, 2], "the straddling paragraph is in both"
    assert _squash("".join(_text_of(c) for c in halves)) == _squash(_source_text(EDGES, "l"))
    for column in halves:   # WP-B's text row reads a column record's run boxes like any other
        boxes = column.extras["x-run-boxes"]
        assert [len(p) for p in boxes] == [len(p["lines"]) for p in column.paragraphs]
        for per_line, paragraph in zip(boxes, column.paragraphs, strict=False):
            for runs, line in zip(per_line, paragraph["lines"], strict=False):
                assert len(runs) == len(line["runs"])
                assert column.box.x - 0.5 <= runs[0][0] and runs[-1][1] <= column.box.x + column.box.w + 0.5


# ------------------------------------------------------------------------- F-D clipped text


def _only_error(ir: IR, element: Element) -> str:
    errors = [d.message for d in ir.diagnostics if d.level == "error" and d.elementId == element.id]
    assert len(errors) == 1, errors
    return errors[0]


def _runs(element: Element) -> list[dict]:
    return [run for p in element.paragraphs for line in p["lines"] for run in line["runs"]]


def test_a_clamp_keeps_two_lines_and_the_browsers_ellipsis(as_seen: IR):
    """p09 D exported every line of a two-line clamp, with no ellipsis."""
    clamp = _named(as_seen, "clamp")
    lines = clamp.paragraphs[0]["lines"]
    assert len(lines) == 2
    assert lines[-1]["runs"][-1]["text"].endswith("…")
    source = _source_text(AS_SEEN, "clamp")
    kept = _text_of(clamp).rstrip("…")
    assert _squash(source).startswith(_squash(kept))
    assert not any(word in run["text"] for run in _runs(clamp) for word in ("never", "visible", "author"))
    extras = clamp.extras["x-wpf-clipped"]
    assert extras["hiddenLines"] == 3 and extras["hiddenChars"] > 0
    assert extras["ellipsis"] is True and extras["canvas"] is False
    message = _only_error(as_seen, clamp)
    assert message.startswith("text is clipped")
    assert "3 line(s)" in message and f"{extras['hiddenChars']} character(s)" in message
    assert "is never visible" in message and "ellipsis was kept" in message
    assert clamp.clip is not None


def test_a_nowrap_ellipsis_keeps_what_fits_before_the_ellipsis(as_seen: IR):
    ell = _named(as_seen, "ell")
    lines = ell.paragraphs[0]["lines"]
    assert len(lines) == 1 and lines[0]["runs"][-1]["text"].endswith("…")
    geometry = _in_browser(AS_SEEN, _GEOMETRY, "#ell")
    kept = len(_text_of(ell)) - 1
    chars, width, right = geometry["chars"], geometry["ellipsis"], geometry["right"]
    assert chars[kept - 1]["right"] + width <= right + 0.5, "the ellipsis would not fit"
    assert chars[kept]["right"] + width > right, "one more character would still have fitted"
    assert lines[0]["box"]["x"] + lines[0]["box"]["w"] == pytest.approx(chars[kept - 1]["right"] + width, abs=0.5)
    assert ell.clip is not None and ell.extras["x-wpf-clipped"]["ellipsis"] is True


def test_vertical_and_horizontal_clips_keep_only_what_is_seen(as_seen: IR):
    shows2 = _named(as_seen, "shows2")
    assert len(shows2.paragraphs[0]["lines"]) == 2, "2 of 5 lines are visible"
    assert shows2.extras["x-wpf-clipped"] == {"hiddenLines": 3, "hiddenChars": shows2.extras["x-wpf-clipped"]["hiddenChars"],
                                              "ellipsis": False, "canvas": False}
    narrow = _named(as_seen, "narrow")
    geometry = _in_browser(AS_SEEN, _GEOMETRY, "#narrow")
    kept = len(_text_of(narrow).rstrip())
    visible = [c for c in geometry["chars"] if c["cx"] <= 1100.0]
    assert _text_of(narrow) == "".join(c["c"] for c in visible).rstrip(), "the x-centre rule"
    assert not _text_of(narrow).endswith("…"), "no text-overflow, no ellipsis"
    assert kept < len(_source_text(AS_SEEN, "narrow"))
    for element in (shows2, narrow):
        assert element.clip is not None
        assert _only_error(as_seen, element).startswith("text is clipped")


@pytest.mark.parametrize(("fixture", "name", "selector"), [
    ("as_seen", "clamp", "#clamp"), ("as_seen", "ell", "#ell"), ("edges", "div#1", "#e"), ("edges", "d", "#d")])
def test_the_ellipsis_is_inside_its_runs_box(request, fixture: str, name: str, selector: str):
    """The "…" is a synthetic fragment with no `Range` rect, so WP-B's glyph-edge trim cut it out of
    its line box and its run's `x-run-boxes` entry; they are widened back to it (plan §16 #10)."""
    ir = request.getfixturevalue(fixture)
    geometry = _in_browser(AS_SEEN if fixture == "as_seen" else EDGES, _GEOMETRY, selector)
    width, glyphs = geometry["ellipsis"], [c for c in geometry["chars"] if not c["c"].isspace()]
    element = [e for e in ir.elements if e.kind == "text" and e.name == name][0]
    kept = len(_squash(_text_of(element).replace("…", "")))
    boxes, seen = element.extras["x-run-boxes"], 0
    for p, paragraph in enumerate(element.paragraphs):
        for index, line in enumerate(paragraph["lines"]):
            texts = [run["text"] for run in line["runs"]]
            if not any("…" in text for text in texts):
                continue
            seen += 1
            k = next(i for i, text in enumerate(texts) if "…" in text)
            left, right = boxes[p][index][k]
            box = line["box"]
            if texts[k].startswith("…") and k == 0:
                # RTL: the ellipsis leads, ending where the first kept character starts (the kept
                # text is the end of the source).
                assert left == pytest.approx(box["x"], abs=0.01)
                assert left == pytest.approx(glyphs[len(glyphs) - kept]["left"] - width, abs=0.05)
            else:
                # LTR: the kept text is the start of the source; the ellipsis follows its last glyph.
                assert texts[k].endswith("…") and k == len(texts) - 1
                assert right == pytest.approx(box["x"] + box["w"], abs=0.01)
                assert right == pytest.approx(glyphs[kept - 1]["right"] + width, abs=0.05)
    assert seen == 1


def test_a_hidden_paragraph_is_dropped_and_quoted(edges: IR):
    three = [e for e in edges.elements if e.kind == "text" and e.name == "a"]
    assert len(three) == 1 and [_text_of_paragraph(p) for p in three[0].paragraphs] == ["one", "two"]
    assert "“three”" in _only_error(edges, three[0])


def _text_of_paragraph(paragraph: dict) -> str:
    return " ".join("".join(run["text"] for run in line["runs"]) for line in paragraph["lines"])


def test_a_wholly_hidden_block_is_not_exported_but_is_reported(edges: IR):
    """Nothing may vanish without a diagnostic (master brief §10.9)."""
    assert not [e for e in edges.elements if "Card three" in (_text_of(e) if e.kind == "text" else "")]
    found = [d for d in edges.diagnostics if "whole block is hidden" in d.message]
    assert len(found) == 1 and found[0].elementId is None and "Card three is hidden" in found[0].message
    assert found[0].level == "error" and found[0].message.startswith("text is clipped")


def test_text_off_the_canvas_edge_is_dropped_as_outside_the_canvas(edges: IR):
    """Peter's decision #12: a line straddling the canvas edge by more than half is dropped."""
    title = [e for e in edges.elements if e.kind == "text" and e.name == "c"][0]
    assert _text_of(title) == "A title long enough to wrap onto a"
    message = _only_error(edges, title)
    assert "outside the canvas" in message and "text is clipped" not in message
    assert title.extras["x-wpf-clipped"]["canvas"] is True


def test_a_chip_fitted_to_its_text_is_not_clipped(edges: IR):
    """Glyph rects overhang their line boxes (`line-height: 1`): a fitted chip is not cut — as on
    fidelity-w0, no clip and no error; only a line box the clip cuts is reported."""
    chip = [e for e in edges.elements if e.kind == "text" and e.name == "k"][0]
    assert _text_of(chip) == "Tight chip gyq"
    assert chip.clip is None and "x-wpf-clipped" not in chip.extras
    assert not [d for d in edges.diagnostics if d.elementId == chip.id and d.level == "error"]


def test_rtl_ellipsis_sits_at_the_start_of_the_line(edges: IR):
    rtl = [e for e in edges.elements if e.kind == "text" and e.name == "d"][0]
    first = rtl.paragraphs[0]["lines"][0]["runs"][0]
    assert first["text"].startswith("…")
    assert "“This sentence is m" in _only_error(edges, rtl), "the quote is what is hidden"


@pytest.mark.parametrize(("name", "selector"), [("div#1", "#e"), ("div#2", "#f")])
def test_the_ellipsis_takes_the_blocks_style_and_fits(edges: IR, name: str, selector: str):
    """Measured in Edge: a bold last run is followed by a *regular* ellipsis — the block's style
    (CSS Overflow 3), not the run's; letter-spacing is inherited, so it is the block's too."""
    element = [e for e in edges.elements if e.kind == "text" and e.name == name][0]
    last = element.paragraphs[0]["lines"][-1]
    runs = last["runs"]
    ellipsis = runs[-1] if runs[-1]["text"] == "…" else None
    if selector == "#e":
        assert runs[-2]["weight"] == 700 and ellipsis is not None and ellipsis["weight"] == 400
    else:
        assert runs[-1]["text"].endswith("…") and runs[-1]["letterSpacingPx"] == pytest.approx(1.0)
    geometry = _in_browser(EDGES, _GEOMETRY, selector)
    assert last["box"]["x"] + last["box"]["w"] <= geometry["right"] + 0.5
    assert element.extras["x-wpf-clipped"]["ellipsis"] is True


def test_wp1_clipbox_is_cut_without_an_ellipsis(sample_manifest: Manifest, sample_dir: Path):
    """`test_clipped_text_is_an_error_diagnostic` holds unchanged (clip.w 120); the run has no '…'."""
    ir = extract_html(Path(__file__).resolve().parents[1] / "fixtures" / "wp1" / "text.html", sample_manifest, "layout-05",
                      sample_dir / "assets", slide_id="wpf-wp1-text")
    clipbox = _named(ir, "clipbox")
    assert clipbox.clip.w == pytest.approx(120.0, abs=0.01)
    assert "…" not in _text_of(clipbox)
    assert "This sentence is much wider".startswith(_text_of(clipbox)[:10])


# -------------------------------------------------------------------------- F-E remote resources
#
# p07 A's remote image lives here for good (plan D8): a torture family may not name a remote URL.

REMOTE_IMAGES = (
    "https://remote.test/map.png", "https://remote.test/bg.png", "file://remote.test/pr.png",
    "https://remote.test/logo.png", "https://10.0.0.1/x.png",
)


def _png(size: tuple[int, int], colour: str) -> bytes:
    import io

    buffer = io.BytesIO()
    Image.new("RGB", size, "#" + colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _fake_remote_host(route) -> None:
    """`remote.test` as a public host would answer — registered *before* the engine's router."""
    url = route.request.url
    bodies = {
        "map.png": (_png((56, 29), "3366CC"), "image/png"),
        "bg.png": (_png((40, 40), "FF8800"), "image/png"),
        "logo.png": (_png((12, 4), "22AA55"), "image/png"),
        "slide.css": (b"body{}", "text/css"),
        "embed": (b"<body style='margin:0;background:#AA3377'>embedded</body>", "text/html"),
    }
    for name, (body, kind) in bodies.items():
        if url.endswith("/" + name):
            route.fulfill(status=200, body=body, content_type=kind)
            return
    route.fulfill(status=404, body=b"", content_type="text/plain")


def test_remote_resources_are_blocked_by_default_and_said_once(browser):
    """Peter's decision #3: block every non-asset host at export, everywhere including dev."""
    assert config.REMOTE_RESOURCES == "block"
    context = _context(REMOTE)
    with measuring_page(Canvas(1280, 720)) as page:
        seen: list[str] = []
        page.on("request", lambda request: seen.append(request.url))
        report = prepare_page(page, REMOTE, context.manifest, context.assets_dir)
        page.evaluate("() => new Promise(r => setTimeout(r, 50))")
        blocked = {entry["url"]: entry for entry in report["blocked"]}
    outside = {url for url in seen if not url.startswith("file:///")}
    assert outside, "the slide asked for nothing remote — the fixture is broken"
    assert outside <= set(blocked), f"requests that were not refused: {outside - set(blocked)}"
    assert {entry["reason"] for entry in blocked.values()} == {"blocked by policy"}

    ir = _extract(REMOTE, "wpf-remote")
    assert not [e for e in ir.elements if e.kind in ("image", "raster")]
    for url in REMOTE_IMAGES:
        about = [d for d in ir.diagnostics if url in d.message]
        assert len(about) == 1, f"{url}: {[d.message for d in about]}"
        assert about[0].level == "error"
        assert "image did not load" in about[0].message and "blocked by policy" in about[0].message
    frames = [d for d in ir.diagnostics if "remote.test/embed" in d.message]
    assert len(frames) == 1 and frames[0].level == "error" and "blocked by policy" in frames[0].message
    for resource in ("slide.css", "f.woff2"):
        about = [d for d in ir.diagnostics if resource in d.message]
        assert len(about) == 1 and about[0].level == "warn" and "blocked remote resource" in about[0].message


def test_raster_policy_embeds_the_browsers_picture_of_a_public_remote_image(
    browser, monkeypatch: pytest.MonkeyPatch
):
    from app.engine.extract import derived_dir
    from app.engine.extract import html as html_module
    from tests.engine.helpers import current_workspace

    monkeypatch.setattr(config, "REMOTE_RESOURCES", "raster")
    monkeypatch.setattr(html_module, "_host_is_public", lambda host: host == "remote.test")
    context = _context(REMOTE)
    with measuring_page(Canvas(1280, 720)) as page:
        page.route("https://remote.test/**", _fake_remote_host)   # before the engine's router
        ir = extract_html(REMOTE, context.manifest, context.layout_id, context.assets_dir,
                          slide_id="wpf-remote-raster", title="remote", page=page)
    images = {e.extras.get("x-wpf-remote"): e for e in ir.elements if e.kind == "image"}

    photo = images["https://remote.test/map.png"]
    assert Path(photo.src).parent == derived_dir(current_workspace(), "wpf-remote-raster")
    assert Image.open(photo.src).size == (round(photo.box.w), round(photo.box.h)) == (560, 290)
    assert photo.fit == "fill" and photo.crop is None
    assert any(d.level == "warn" and d.message.startswith("rasterised: remote image https://remote.test/map.png")
               for d in ir.diagnostics)

    background = images["https://remote.test/bg.png"]
    assert _pixels_like(background.src, "FF8800") > 0, "the remote background itself is in the picture"
    assert _pixels_like(background.src, "E8F1FB") == 0, "its colour is drawn natively, not in the picture"
    assert _pixels_like(background.src, "0B2545", tolerance=60) == 0, "no glyph of the child text"

    rasters = [e for e in ir.elements if e.kind == "raster"]
    assert any("<iframe>" in r.reason for r in rasters), "a remote iframe on a public host: as before"
    logo = [r for r in rasters if "remote" in r.reason]
    assert len(logo) == 1 and _ink(logo[0].src) > 0

    private = [d for d in ir.diagnostics if "10.0.0.1" in d.message]
    assert len(private) == 1 and "image did not load" in private[0].message and "not public" in private[0].message
    assert "https://10.0.0.1/x.png" not in images


@pytest.mark.parametrize("host", [
    "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
    "::1", "[::1]", "::ffff:169.254.169.254", "fd00::1", "fe80::1", "localhost", "x.local", "x.internal",
    "x.localhost", "x.home.arpa", "",
])
def test_a_private_host_is_never_public(host: str):
    from app.engine.extract import html as html_module

    html_module.forget_host_checks()
    assert html_module._host_is_public(host) is False


@pytest.mark.parametrize("host", ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
def test_a_global_address_is_public(host: str):
    from app.engine.extract import html as html_module

    html_module.forget_host_checks()
    assert html_module._host_is_public(host) is True


@pytest.mark.parametrize(("answers", "public"), [
    (["10.1.2.3"], False),
    (["93.184.216.34", "192.168.0.7"], False),     # one private address is enough to refuse
    (["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"], True),
    ([], False),
    (None, False),                                 # the resolver fails
])
def test_a_name_is_public_only_when_every_address_is(monkeypatch: pytest.MonkeyPatch, answers, public: bool):
    import socket

    from app.engine.extract import html as html_module

    def fake(host, port, *args, **kwargs):
        if answers is None:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
                for a in answers]

    monkeypatch.setattr(html_module.socket, "getaddrinfo", fake)
    html_module.forget_host_checks()
    try:
        assert html_module._host_is_public("somewhere.example") is public
    finally:
        html_module.forget_host_checks()


def test_the_router_replaces_only_its_own_handler_and_serves_assets_first(browser, monkeypatch: pytest.MonkeyPatch):
    from app.engine.extract.html import route_assets

    assets = config.TORTURE_FIXTURE / "assets"
    body = ('<img id="a" src="http://localhost:8787/api/projects/x/assets/photo-400x300.png">'
            '<img id="r" src="https://remote.test/one.png">')
    for policy in ("block", "raster"):
        monkeypatch.setattr(config, "REMOTE_RESOURCES", policy)
        with measuring_page(Canvas(1280, 720)) as page:
            first = route_assets(page, assets)
            second = route_assets(page, assets)
            page.set_content(body, wait_until="networkidle")
            widths = page.evaluate("() => [document.getElementById('a').naturalWidth, "
                                   "document.getElementById('r').naturalWidth]")
        assert widths == [400, 0], f"{policy}: the asset is served from disk, the remote one is not"
        assert first.blocked == [], "the first handler was replaced, not stacked"
        assert [entry["url"] for entry in second.blocked] == ["https://remote.test/one.png"]

    monkeypatch.setattr(config, "REMOTE_RESOURCES", "block")
    images = _extract(config.TORTURE_FIXTURE / "images.html", "wpf-images")
    expect = json.loads((config.TORTURE_FIXTURE / "images.expect.json").read_text(encoding="utf-8"))
    assert images.counts() == expect["kinds"], "file:-resolved assets still route"


def test_the_linter_names_the_policy():
    from app.engine.verify.lint import lint

    report = lint('<!doctype html><html><body><img src="https://example.com/a.png"></body></html>')
    found = [f for f in report.findings if f.rule == "remote-resource"]
    assert found and found[0].level == "error"
    assert "policy" in found[0].message and "blocks remote resources" in found[0].message
    assert "SLIDE_ENGINE_REMOTE_RESOURCES=raster" in found[0].message


def test_the_reference_hides_what_the_export_blocks(tmp_path: Path, browser):
    """A blocked image or frame is absent from the export, so it is absent from the reference too:
    Chromium's broken-image icon and error page are not scored against the deck."""
    from app.engine.extract.html import render_reference

    context = _context(REMOTE)
    layout_png = context.layouts_dir / context.manifest.layout(context.layout_id).background
    out = render_reference(REMOTE, layout_png, tmp_path / "remote.png", assets_dir=context.assets_dir,
                           fonts=dict(context.manifest.fonts))
    rendered = Image.open(out).convert("RGB")
    layout = Image.open(layout_png).convert("RGB")
    for x, y in ((300, 180), (790, 270)):          # inside the blocked <img> and the blocked <iframe>
        assert rendered.getpixel((x, y)) == layout.getpixel((x, y))


# ----------------------------------------------------------------------------- the family


def test_every_expected_diagnostic_is_reported(as_seen: IR):
    expect = json.loads(AS_SEEN.with_suffix(".expect.json").read_text(encoding="utf-8"))
    for wanted in expect["expectedDiagnostics"]:
        found = [d for d in as_seen.diagnostics
                 if d.level == wanted["level"] and wanted["match"] in d.message]
        assert found, f"no {wanted['level']} diagnostic containing {wanted['match']!r}"
    assert as_seen.counts() == expect["kinds"]
