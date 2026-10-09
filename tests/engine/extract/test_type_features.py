"""Figure styles and OpenType features PowerPoint cannot ask for (render check, 2026-09-29).

DrawingML has no run property for `lnum`/`onum`/`tnum`, fractions or any other requested OpenType
feature, so PowerPoint draws the font's default forms: Georgia's digits are old-style. A slide that asks
for lining digits looked right in the browser and wrong in the .pptx. The measurement, the reference and
the app's preview now draw the default forms too, and lint says why (`type-features`).
"""
from __future__ import annotations

from app.engine.extract.html import TYPE_FEATURES_CSS, force_theme_fonts, measuring_page
from app.engine.ir import Canvas
from app.engine.verify.lint import lint

PAGE = ("<!doctype html><html><head><meta charset='utf-8'><style>"
        "html,body{margin:0;width:1280px;height:720px}"
        ".n{position:absolute;left:40px;top:40px;font-family:Georgia,serif;font-size:96px;"
        "font-variant-numeric:lining-nums tabular-nums}"
        ".f{position:absolute;left:40px;top:300px;font-family:Georgia,serif;font-size:96px;"
        "font-feature-settings:'lnum' 1}"
        ".n::after{content:' 34';font-variant-numeric:lining-nums}"
        "</style></head><body><div class='n'>1234</div><div class='f'>5678</div></body></html>")


def test_measurement_draws_the_default_figures_powerpoint_will():
    with measuring_page(Canvas(w=1280, h=720)) as page:
        page.set_content(PAGE)
        asked = page.evaluate("() => getComputedStyle(document.querySelector('.n')).fontVariantNumeric")
        assert asked != "normal"                      # the slide really asks for them
        force_theme_fonts(page, {"major": "Georgia", "minor": "Arial"})
        styles = page.evaluate("""() => ['.n', '.f'].map(s => {
            const cs = getComputedStyle(document.querySelector(s));
            return [cs.fontVariantNumeric, cs.fontFeatureSettings];
        }).concat([[getComputedStyle(document.querySelector('.n'), '::after').fontVariantNumeric, '']])""")
    assert styles == [["normal", "normal"], ["normal", "normal"], ["normal", ""]], styles


def test_lint_says_powerpoint_keeps_the_default_figures():
    findings = [f for f in lint(PAGE).findings if f.rule == "type-features"]
    assert findings and all(f.level == "warn" for f in findings)
    said = " ".join(f.message for f in findings)
    assert "font-variant-numeric" in said and "font-feature-settings" in said


def test_normal_figures_are_not_a_finding():
    page = PAGE.replace("lining-nums tabular-nums", "normal").replace("lining-nums", "normal") \
               .replace("font-feature-settings:'lnum' 1", "font-feature-settings:normal")
    assert not [f for f in lint(page).findings if f.rule == "type-features"]


def test_the_rule_names_both_properties():
    assert "font-variant-numeric: normal !important" in TYPE_FEATURES_CSS
    assert "font-feature-settings: normal !important" in TYPE_FEATURES_CSS
