"""`design_refs.design_lint.design_findings_html` — the deterministic design review, on hand-written slides.

Each fixture is the clean slide with one defect added, and must trigger exactly the rule it was written
for; the clean slide itself must produce nothing. The master is the synthetic one in `conftest.py` (a
1280x720 "Title only" layout, title zone x 64-1216, footer furniture from y 669); nothing real is touched.

Ported from Slide Studio `server/tests/test_design_lint.py`, which ran the same slides on a client
master with the same geometry. Browser tests are skipped (not failed) when this machine has no usable
Playwright browser. `_rank` and the colour helpers are pure and always run.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.core.design_refs import design_lint
from tests.core.design_refs.conftest import LAYOUT, manifest

_MASTER: dict[str, Path] = {}


@pytest.fixture(autouse=True)
def _master(master_dir: Path, tmp_path: Path) -> None:
    _MASTER["root"] = master_dir
    _MASTER["tmp"] = tmp_path


# ------------------------------------------------------------------------------------ fixtures


HEAD = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;width:1280px;height:720px;overflow:hidden;background:transparent;font-family:Arial}
*{box-sizing:border-box}
.t{position:absolute;margin:0;font:700 24px/1.2 Arial;color:#2B2B33}
.card{position:absolute;top:140px;width:368px;height:300px;background:#F3F3F5}
.card h3{position:absolute;left:24px;top:24px;margin:0;font:700 16px/1.25 Arial;color:#2B2B33}
.card p{position:absolute;left:24px;top:64px;width:320px;margin:0;font:14px/1.4 Arial;color:#6F6F7A}
.rule{position:absolute;left:64px;top:470px;width:1152px;height:3px;background:#2F80ED}
.note{position:absolute;left:64px;top:490px;margin:0;font:12px/1.3 Arial;color:#6F6F7A}
</style></head><body>
"""
TITLE = '<h1 class="t" data-placeholder="title" style="left:64px;top:28px;width:1152px">{title}</h1>\n'
CARD = ('<div class="card" style="left:{x}px;{style}"><h3>{head}</h3>'
        '<p>{body}</p></div>\n')
TAIL = '<aside class="notes">Speaker notes are not reviewed.</aside>\n</body></html>'

GOOD_TITLE = "Revenue grew 12% in 2025, driven by pricing and mix"
#: Each card's body fills its card (a hollow card is a finding): about nine lines at 14 px in 320 px. The
#: prose carries only three figures: four or more with nothing drawn is `exhibit-plain`.
CARDS = [("Pricing", "List prices rose 4% across the core range in January, the first rise in three years. "
                     "Retailers passed most of it through to shelf prices without losing distribution, and "
                     "promotional depth fell by about a fifth of volume as the calendar was cut back. Net price "
                     "realisation after rebates came in well ahead of the level set in the budget, and it "
                     "contributed about half of the year's revenue growth on its own."),
         ("Mix", "Premium lines grew to 38% of volume, up by a quarter on a year earlier, as the two launches of the "
                 "spring season reached full distribution in the second half. Premium units carry a margin "
                 "around nine points above the core range, so the shift added roughly a third of the growth. "
                 "Entry lines were trimmed by fourteen items with little loss of sales, which freed shelf "
                 "space and working capital for the ranges that earn more."),
         ("Volume", "Units were flat against a soft market that fell by about 2% over the year, so the business "
                    "gained close to two points of share in its core categories. Gains came mainly from the "
                    "grocery channel and from online orders, which offset a weaker convenience channel and the "
                    "planned exit from two low-margin private-label contracts in the autumn. Volume guidance for "
                    "next year assumes a flat market and stable share.")]


def slide(*, title: str = GOOD_TITLE, title_style: str = "", cards: list[dict[str, Any]] | None = None,
          rule: str = "", note: str = "Figures are for the fiscal year.", extra: str = "") -> str:
    """The clean slide: an action title, three equal cards 24 px apart, a rule and a note."""
    positions = cards or [{}, {}, {}]
    html = HEAD + TITLE.format(title=title).replace('width:1152px"', f'width:1152px;{title_style}"')
    for i, (head, body) in enumerate(CARDS):
        spec = {"x": 64 + i * 392, "style": "", "head": head, "body": body, **positions[i]}
        html += CARD.format(**spec)
    html += f'<div class="rule" style="{rule}"></div>\n'
    if note:
        html += f'<p class="note">{note}</p>\n'
    return html + extra + TAIL


_counter = [0]


def review(html: str, *, master: bool = True, **kw: Any) -> list[dict[str, Any]]:
    """Write the slide beside the synthetic master's assets and review it."""
    _counter[0] += 1
    path = _MASTER["tmp"] / f"slide-{_counter[0]}.html"
    path.write_text(html, encoding="utf-8")
    root = _MASTER["root"]
    try:
        return design_lint.design_findings_html(
            path, manifest() if master else None, LAYOUT if master else None,
            assets_dir=root / "assets", layouts_dir=root / "layouts" if master else None, **kw)
    except design_lint.DesignLintUnavailable as exc:
        pytest.skip(f"no measuring browser on this machine: {exc}")


def rules(findings: list[dict[str, Any]]) -> list[str]:
    return [f["rule"] for f in findings]


# ------------------------------------------------------------------------------------ the clean slide


def test_clean_slide_has_no_findings():
    assert review(slide()) == []


def test_finding_shape():
    found = review(slide(cards=[{}, {"style": "top:143px"}, {}]))
    assert found, "the near miss should be reported"
    for f in found:
        assert set(f) == {"level", "rule", "message", "element"}
        assert f["level"] in ("warn", "error")
        assert isinstance(f["message"], str) and f["message"]


# ------------------------------------------------------------------------------------ one rule each


def test_near_miss_alignment():
    found = review(slide(cards=[{}, {"style": "top:143px"}, {}]))
    assert rules(found) == ["alignment"]
    f = found[0]
    assert f["level"] == "warn"
    assert "top edge" in f["message"] and "y 143 → 140" in f["message"] and "3 px up" in f["message"]
    assert "Mix" in f["message"]


    assert "Mix" in f["message"]


def test_unequal_gaps():
    # Gaps 24 and 30 px: the third card starts 6 px late (and is 6 px narrower, so its right edge holds).
    found = review(slide(cards=[{}, {}, {"x": 854, "style": "width:362px"}]))
    assert rules(found) == ["gaps"]
    assert "24, 30 px" in found[0]["message"] and "x 854 → 848" in found[0]["message"]


def test_margin_intrusion():
    found = review(slide(extra='<p class="note" style="left:40px;top:520px">Pricing drove most of the gain.</p>\n'))
    assert rules(found) == ["margin"]
    assert "x 40" in found[0]["message"] and "64 px left margin" in found[0]["message"]
    assert "24 px right" in found[0]["message"]


def test_footer_band_intrusion():
    found = review(slide(extra='<p class="note" style="top:676px">Prepared for the board.</p>\n'))
    assert rules(found) == ["margin"]
    assert "footer" in found[0]["message"] and "layout's footer furniture" in found[0]["message"]


def test_text_spilling_out_of_its_card_is_an_error():
    long_body = " ".join(["Prices rose across every channel and region this year."] * 15)
    found = review(slide(cards=[{"body": long_body}, {}, {}], note=""))
    assert rules(found) == ["text-spill"]
    assert found[0]["level"] == "error"
    assert "below its bottom" in found[0]["message"] and "the card at y 440" in found[0]["message"]


def test_overlapping_text_is_an_error():
    found = review(slide(extra='<p class="note" style="left:64px;top:492px">Units are thousands.</p>\n'))
    assert rules(found) == ["text-overlap"]
    assert found[0]["level"] == "error"


def test_off_canvas_text_is_an_error():
    found = review(slide(extra='<p class="note" style="left:1150px;top:520px;white-space:nowrap">'
                               'This sentence runs off the right edge of the slide.</p>\n'))
    assert "text-off-canvas" in rules(found)
    assert all(f["level"] == "error" for f in found if f["rule"] == "text-off-canvas")


def test_text_below_8pt_is_an_error():
    found = review(slide(note="").replace('<aside', '<p class="note" style="font-size:9px">Tiny print.</p>\n<aside'))
    assert rules(found) == ["text-too-small"]
    assert found[0]["level"] == "error" and "9 px (6.8 pt)" in found[0]["message"]


def test_text_just_under_8pt_is_a_warn():
    found = review(slide(note="").replace('<aside', '<p class="note" style="font-size:10.5px">Small print.</p><aside'))
    assert rules(found) == ["text-too-small"]
    assert found[0]["level"] == "warn" and "10.5 px (7.9 pt)" in found[0]["message"]


def test_sizes_within_one_pt_count_once():
    # 14 px and 14.5 px are both 11 pt: still four sizes, no finding.
    extra = '<p class="note" style="left:64px;top:520px;font-size:14.5px">Nearly body.</p>'
    assert review(slide(extra=extra)) == []


def test_too_many_font_sizes():
    extra = ('<p class="note" style="left:64px;top:520px;font-size:18px">Eighteen.</p>\n'
             '<p class="note" style="left:64px;top:560px;font-size:20px">Twenty.</p>\n')
    found = review(slide(extra=extra))
    assert rules(found) == ["font-sizes"]
    assert "6 different font sizes" in found[0]["message"]


def test_off_palette_colour():
    found = review(slide(rule="background:#8A2BE2"))
    assert rules(found) == ["palette"]
    assert "#8A2BE2" in found[0]["message"]


def test_competing_accents():
    # Blue (the theme's accent1) as the structure, then teal (dk2) and purple (accent5) for emphasis.
    extra = ('<div style="position:absolute;left:64px;top:520px;width:200px;height:40px;background:#00A3A1"></div>\n'
             '<div style="position:absolute;left:288px;top:520px;width:200px;height:40px;background:#8E44AD"></div>\n')
    found = review(slide(rule="background:#2F80ED;height:8px", extra=extra))
    assert rules(found) == ["accents"]
    assert "#00A3A1" in found[0]["message"] and "#8E44AD" in found[0]["message"]


def test_title_wrapping_to_three_lines():
    title = ("Revenue grew 12% in 2025 as pricing, premium mix and procurement savings "
             "more than offset flat volumes in every region and channel we serve")
    found = review(slide(title=title, title_style="width:560px"))
    assert rules(found) == ["title-lines"]
    assert "3 lines" in found[0]["message"] or "4 lines" in found[0]["message"]


def test_topic_label_title():
    found = review(slide(title="Market overview"))
    assert rules(found) == ["title-topic"]
    assert found[0]["level"] == "warn"


CHART = ('<div data-name="Revenue chart" style="position:absolute;left:64px;top:500px;width:560px;height:140px" '
         "data-chart='{\"type\":\"column\",\"categories\":[\"2023\",\"2024\",\"2025\"],"
         "\"series\":[{\"name\":\"Revenue\",\"values\":[3,5,8]}],\"colors\":[\"#2F80ED\"]}'></div>\n")


def test_data_without_source():
    found = review(slide(note="", extra=CHART))
    assert rules(found) == ["source"]
    assert "Revenue chart" in found[0]["message"] and "Source:" in found[0]["message"]


def test_data_with_source_is_fine():
    assert review(slide(note="Source: company filings", extra=CHART)) == []


def test_zero_value_data_label():
    chart = CHART.replace("[3,5,8]", "[0,5,8]").replace('"colors"', '"options":{"dataLabels":true},"colors"')
    found = review(slide(note="Source: company filings", extra=chart))
    assert rules(found) == ["chart-zero-label"]
    assert "2023" in found[0]["message"]


def table(rows: list[list[str]], *, num_align: str = "right") -> str:
    head = "".join(f"<th>{c}</th>" for c in rows[0])
    body = "".join("<tr>" + "".join(
        f'<td style="text-align:{"left" if i == 0 else num_align}">{c}</td>' for i, c in enumerate(r)) + "</tr>"
        for r in rows[1:])
    return ('<table data-name="P&amp;L" style="position:absolute;left:64px;top:500px;width:560px;'
            'border-collapse:collapse;font:12px Arial;color:#2B2B33">'
            f"<tr>{head}</tr>{body}</table>\n")


ROWS = [["Item", "FY24", "FY25"], ["Revenue", "120.4", "132.9"], ["Costs", "(80.1)", "(84.6)"],
        ["EBITDA", "40.3", "48.3"]]


def table_slide(rows=ROWS, **kw) -> str:
    return slide(note="Source: company filings", cards=[{}, {}, {}],
                 extra=table(rows, **kw)).replace('top:490px;', 'top:640px;')


def test_banking_table_clean():
    assert review(table_slide()) == []


def test_table_numbers_not_right_aligned():
    found = review(table_slide(num_align="left"))
    assert rules(found) == ["table-align"]
    assert "column 2" in found[0]["message"] and "FY24" in found[0]["message"]


def test_centred_score_column_is_exempt():
    rows = [["Criterion", "Score", "FY25"], ["Growth", "4/5", "132.9"], ["Margin", "High", "(84.6)"],
            ["Risk", "✓", "48.3"]]
    html = table_slide(rows).replace("<th>Score</th>", '<th style="text-align:center">Score</th>')
    html = html.replace('text-align:right">4/5', 'text-align:center">4/5').replace(
        'text-align:right">High', 'text-align:center">High').replace('text-align:right">✓', 'text-align:center">✓')
    # (scores set as text are also `exhibit-plain` — draw them as Harvey balls — which is not this test's point)
    assert [f for f in review(html) if f["rule"] != "exhibit-plain"] == []
    # The same short cells under a centred header, but financial figures left-aligned: still flagged.
    money = table_slide(num_align="left").replace("<th>FY24</th>", '<th style="text-align:center">FY24</th>')
    found = review(money)
    assert rules(found) == ["table-align"] and "FY24" in found[0]["message"]


def test_table_mixed_negatives():
    rows = [r[:] for r in ROWS]
    rows[2][2] = "-84.6"
    found = review(table_slide(rows))
    assert rules(found) == ["table-negatives"]
    assert "(84.6)" in found[0]["message"]


def test_table_mixed_decimals():
    rows = [r[:] for r in ROWS]
    rows[1][1] = "120"
    found = review(table_slide(rows))
    assert rules(found) == ["table-decimals"]
    assert "120" in found[0]["message"]


# ------------------------------------------------------------------------------------ the AI-generated look


#: The card the saved slides kept producing: a thick accent stripe, a small radius and a soft shadow.
STRIPE_CARD = "border-left:6px solid #2F80ED;border-radius:3px;box-shadow:0 2px 10px rgba(0,0,0,0.09)"


def test_rounded_side_stripe_card_is_flagged_with_the_notch():
    found = review(slide(cards=[{"style": STRIPE_CARD}, {}, {}]))
    assert sorted(rules(found)) == ["rounded-shadow", "side-stripe"]
    stripe = next(f for f in found if f["rule"] == "side-stripe")
    assert stripe["level"] == "warn"
    assert "6 px, rounded box" in stripe["message"] and "pokes out past the corners" in stripe["message"]
    assert "Pricing" in stripe["message"] and "full 1px border" in stripe["message"]
    assert "3 px radius" in next(f for f in found if f["rule"] == "rounded-shadow")["message"]


def test_square_side_stripe_is_flagged_without_the_notch():
    found = review(slide(cards=[{"style": "border-left:4px solid #2F80ED"}, {}, {}]))
    assert rules(found) == ["side-stripe"]
    assert "4 px" in found[0]["message"] and "pokes out" not in found[0]["message"]


def test_thin_side_rules_and_full_borders_are_fine():
    assert review(slide(cards=[{"style": "border-left:2px solid #2F80ED"}, {}, {}])) == []
    assert review(slide(cards=[{"style": "border:1px solid #2F80ED"}, {}, {}])) == []


def test_emoji_in_text():
    found = review(slide(extra='<p class="note" style="left:64px;top:520px">\U0001F680 Growth ahead</p>\n'))
    assert rules(found) == ["emoji"]
    assert "Growth ahead" in found[0]["message"]


def test_gradient_text():
    extra = ('<p class="note" style="left:64px;top:520px;background:linear-gradient(90deg,#2F80ED,#000000);'
             '-webkit-background-clip:text;background-clip:text;color:transparent">Gradient words</p>\n')
    found = review(slide(extra=extra))
    assert rules(found) == ["gradient-text"]


def test_italic_accent_word_in_the_title():
    found = review(slide(title="Revenue grew 12% in 2025, driven by <i>pricing</i> and mix"))
    assert rules(found) == ["title-italic"]
    assert "pricing" in found[0]["message"]


def test_emoji_pattern_spares_check_marks():
    for mark in ("✓", "✔", "✗", "✘", "●", "→"):
        assert not design_lint._EMOJI.search(mark), mark
    for emoji in ("\U0001F680", "✅", "✨", "⚡", "⭐", "❌", "\U0001F4A1", "⚠️"):
        assert design_lint._EMOJI.search(emoji), emoji


# ------------------------------------------------------------------------------------ plumbing


def test_missing_file_is_a_design_lint_error(tmp_path):
    with pytest.raises(design_lint.DesignLintError):
        design_lint.design_findings_html(tmp_path / "nowhere.html", manifest(), LAYOUT,
                                         assets_dir=tmp_path, layouts_dir=None)


def test_no_master_still_reviews():
    found = review(slide(note="").replace('<aside', '<p class="note" style="font-size:9px">Tiny.</p>\n<aside'),
                   master=False)
    assert "text-too-small" in rules(found)
    assert "palette" not in rules(found)    # no theme, no palette to judge against


def test_rank_caps_and_counts_the_rest():
    findings = ([design_lint._f("warn", "alignment", f"a{i}", None) for i in range(8)]
                + [design_lint._f("warn", "palette", "p", None), design_lint._f("error", "text-spill", "s", None)]
                + [design_lint._f("warn", "margin", f"m{i}", None) for i in range(4)])
    ranked = design_lint._rank(findings, 5)
    assert [f["rule"] for f in ranked[:5]] == ["text-spill", "margin", "alignment", "palette", "margin"]
    assert ranked[-1]["rule"] == "more-findings" and "9 more" in ranked[-1]["message"]
    assert len(ranked) == 6


def test_colour_helpers():
    assert design_lint.delta_e("FFFFFF", "FFFFFF") == 0
    assert design_lint._on_palette("F3F3F5", {"1A9AFA": "accent1"})           # a grey
    assert design_lint._on_palette("8CCDFC", {"1A9AFA": "accent1"})           # a 50 % tint of accent1
    assert not design_lint._on_palette("8A2BE2", {"1A9AFA": "accent1"})
    assert design_lint._on_palette("00B050", {"1A9AFA": "accent1"})           # RAG green


# ------------------------------------------------------------------------------------ dead space and devices


def test_a_third_of_the_body_left_blank_is_an_empty_band():
    # Cards 120 px tall instead of 300 (one line each): nothing between y 260 and the rule at 470.
    found = review(slide(cards=[{"style": "height:120px", "body": "Held flat on the year."}] * 3))
    assert rules(found) == ["empty-band"]
    f = found[0]
    assert f["level"] == "warn" and "band across the slide from y 260 to 470" in f["message"]
    assert "closing takeaway strip" in f["message"] and "deliberate single statement" in f["message"]


def test_status_pills_numbered_chips_and_check_bullets_are_not_flagged():
    """Meaning-carrying devices on a composed slide: a fully rounded status pill, a numbered chip and an
    SVG check bullet. None of them is an AI-look tell."""
    pill = ('<span style="position:absolute;left:88px;top:380px;display:inline-block;padding:2px 8px;'
            'border-radius:10px;background:#D6ECFE;color:#2B2B33;font:700 12px/1.3 Arial">On track</span>\n')
    chip = ('<div style="position:absolute;left:480px;top:380px;width:24px;height:24px;border-radius:12px;'
            'background:#2F80ED;color:#FFFFFF;font:700 12px/24px Arial;text-align:center">2</div>\n')
    check = ('<svg style="position:absolute;left:872px;top:382px" width="16" height="16" viewBox="0 0 16 16">'
             '<polyline points="3,8 7,12 13,4" fill="none" stroke="#2F80ED" stroke-width="2"/></svg>\n')
    found = review(slide(extra=pill + chip + check))
    assert not {"side-stripe", "rounded-shadow", "emoji", "gradient-text", "empty-band"} & set(rules(found)), found


def _box_item(x, y, w, h):
    box = design_lint.B(x, y, w, h)
    return design_lint.Item(el=None, kind="shape", box=box, ink=box)


def _body_grid(**kw):
    return design_lint.Context(grid=design_lint.Grid(left=64, right=1216, bottom=660,
                                                     title_zone=(64, 28, 1152, 62), **kw))


def test_largest_gap_counts_the_edges():
    assert design_lint._largest_gap([(100, 200), (150, 300)], 0, 1000) == (300, 1000)
    assert design_lint._largest_gap([(0, 400), (700, 1000)], 0, 1000) == (400, 700)
    assert design_lint._largest_gap([(250, 900)], 0, 1000) == (0, 250)


def test_a_dead_strip_down_the_slide_and_the_exemptions():
    left_half = [_box_item(64, 110, 520, 160), _box_item(64, 290, 520, 160), _box_item(64, 470, 520, 170)]
    found = design_lint._empty_band(left_half, _body_grid())
    assert [f["rule"] for f in found] == ["empty-band"]
    assert "strip down the slide from x 584 to 1216" in found[0]["message"]
    # a full body is fine
    full = left_half + [_box_item(608, 110, 608, 530)]
    assert design_lint._empty_band(full, _body_grid()) == []
    # a cover or divider is meant to be spacious; a lone statement is a choice
    assert design_lint._empty_band(left_half, _body_grid(top_title=False)) == []
    assert design_lint._empty_band(left_half[:2], _body_grid()) == []


def test_a_hole_between_stacked_units_is_a_stack_gap():
    # A takeaway panel 60 px under the cards (bottom y 440): a hole, not a gutter.
    panel = ('<div style="position:absolute;left:64px;top:500px;width:1152px;height:56px;background:#F3F3F5">'
             '<p style="position:absolute;left:24px;top:18px;margin:0;font:700 16px/1.25 Arial;color:#2B2B33">'
             'Pricing and mix explain the whole gain.</p></div>\n')
    found = review(slide(rule="display:none", note="", extra=panel))
    assert rules(found) == ["stack-gap"]
    msg = found[0]["message"]
    assert "60 px between" in msg and "bottom y 440" in msg and "top y 500" in msg
    assert "share their top and bottom edges" in msg and "never filler" in msg
    # at a 24 px gutter it is fine
    assert review(slide(rule="display:none", note="", extra=panel.replace("top:500px", "top:464px"))) == []


def test_a_panel_with_blank_bottom_half_is_hollow():
    found = review(slide(cards=[{"body": "Held flat on the year."}, {}, {}]))
    assert rules(found) == ["hollow-panel"]
    msg = found[0]["message"]
    assert "Pricing" in msg and "box y 140–440" in msg and "blank below" in msg
    assert "Size each panel to its content" in msg and "evidence" in msg


def test_hollow_and_gap_rules_skip_parts_and_centred_content():
    # a panel whose content is vertically centred is not hollow; a pill is not a unit
    card = _box_item(64, 110, 400, 200)
    card.card = True
    from types import SimpleNamespace
    text = design_lint.Item(el=SimpleNamespace(id="t1"), kind="text", box=design_lint.B(88, 190, 300, 40),
                            ink=design_lint.B(88, 190, 300, 40), container=card, size_px=14.7)
    assert design_lint._hollow_panels([card, text], _body_grid()) == []
    text.box = text.ink = design_lint.B(88, 124, 300, 40)          # now in the top: 132 px blank below
    assert [f["rule"] for f in design_lint._hollow_panels([card, text], _body_grid())] == ["hollow-panel"]
    pill = _box_item(64, 400, 50, 18)                               # below the card, but a pill is no unit
    assert design_lint._stack_gaps([card, text, pill], _body_grid()) == []


# ------------------------------------------------------------------------------------ show, don't list

BENCH = [["Metric", "Us", "Peers", "Gap"], ["Revenue per head", "1.4", "1.9", "(0.5)"],
         ["Digital share", "34%", "61%", "(27) pts"], ["Cost ratio", "78%", "71%", "+7 pts"]]


def test_figures_set_only_as_text_are_an_exhibit_plain():
    found = review(table_slide(BENCH))
    assert "exhibit-plain" in rules(found)
    msg = next(f for f in found if f["rule"] == "exhibit-plain")["message"]
    assert "draws none of them" in msg and "one shared scale" in msg and "lower is better" in msg


def test_a_bar_or_a_financial_grid_is_not_plain():
    bar = '<div style="position:absolute;left:900px;top:600px;width:120px;height:10px;background:#2F80ED"></div>\n'
    assert "exhibit-plain" not in rules(review(table_slide(BENCH).replace("<aside", bar + "<aside")))
    assert "exhibit-plain" not in rules(review(table_slide())), "a statement across FY columns is read, not charted"


def test_figure_counting_skips_years_dates_and_numbering():
    assert design_lint._figures("34% vs 61%, a gap of 27 pts") == 3
    assert design_lint._figures("Day 5–10, Week 2–3, Q1 2025, Month 18, Phase 2") == 0


FLAT_CHART = ('<div style="position:absolute;left:64px;top:520px;width:600px;height:130px" data-chart=\'{{"type":"column",'
         '"categories":["A","B","C","D","E","F"],"series":[{{"name":"Rev","values":[5,7,6,8,9,12]}}],'
         '"colors":["#2F80ED"],"options":{{"dataLabels":true{options}}}}}\'></div>\n')


def test_a_one_colour_chart_without_a_highlight_has_no_focus():
    html = slide(note="Source: company filings", extra=FLAT_CHART.format(options=""))
    found = review(html)
    assert "chart-no-focus" in rules(found)
    assert "`pointColors`" in next(f for f in found if f["rule"] == "chart-no-focus")["message"]
    focused = FLAT_CHART.format(options=',"pointColors":{{"5":"#2B2B33"}}')
    html = slide(note="Source: company filings", extra=focused)
    assert "chart-no-focus" not in rules(review(html))
