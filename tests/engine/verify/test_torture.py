"""Every torture family meets its own `expect.json` — `judge_family`, without the gate leg.

The runner's rules live in `engine/verify/torture.py`; `python -m engine torture` applies them with
the pixel gate as well (the CLI only: it renders twice per family). Here every family in
`test_fixtures.FAMILIES` is extracted once on one measuring page, classified, emitted and judged:
kinds, rasters, charts, `expectedDiagnostics`, `expectedLint`, fit and the text row. This replaces
`test_text_layout::test_no_torture_family_reports_overflow_or_overlap`, which could not honour a
family's expected diagnostics.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.config import engine as config
from app.engine.classify.charts import recognise_charts
from app.engine.classify.placeholders import map_placeholders
from app.engine.emit import pptx as emitter
from app.engine.extract.html import measuring_page
from app.engine.ir import IR, Canvas
from app.engine.reports import EmitOptions
from app.engine.verify.fit import fit
from app.engine.verify.fixtures import context_for
from app.engine.verify.lint import lint
from app.engine.verify.text_deck import text_report
from app.engine.verify.text_layout import annotate_text_layout
from app.engine.verify.torture import judge_family
from tests.engine.helpers import extract_html, require_faces
from tests.engine.test_fixtures import FAMILIES

#: Families whose run the repaired instruments found short of their expectations, with the finding.
#: Strict: the day the engine is fixed the case XPASSes, fails, and this entry must go. Empty since
#: plan §16 #19 (`boxes`: the dropped second shadow is now warned by `reportDroppedShadows`).
KNOWN_FINDINGS: dict[str, str] = {}


@pytest.fixture(scope="module")
def runs(tmp_path_factory) -> dict[str, dict[str, Any]]:
    """Each family extracted, classified, emitted, fitted, linted and text-checked — once."""
    out = tmp_path_factory.mktemp("torture")
    results: dict[str, dict[str, Any]] = {}
    contexts = {family: context_for(config.TORTURE_FIXTURE / f"{family}.html") for family in FAMILIES}
    with measuring_page(Canvas(1280, 720)) as page:
        for family, context in contexts.items():
            canvas = Canvas(context.manifest.canvas_w, context.manifest.canvas_h)
            with measuring_page(canvas, page=page) as sized:
                extracted = extract_html(context.html, context.manifest, context.layout_id,
                                         context.assets_dir, slide_id=family, title=family, page=sized)
            ir = IR.from_json(extracted.to_json())
            ir = annotate_text_layout(map_placeholders(recognise_charts(ir), context.layout))
            deck = out / f"{family}.pptx"
            emitter.emit([ir], context.manifest, context.master, deck, EmitOptions())
            results[family] = {
                "context": context, "extracted": extracted, "ir": ir, "deck": deck,
                "fit": fit(deck, [ir]),
                "lint": lint(context.html.read_text(encoding="utf-8"), context.manifest),
                "text": text_report(deck, [ir]),
            }
    return results


#: Families whose expectations are about particular installed faces, not just any font: `weights`
#: measures Calibri's own static faces at 300 (Calibri Light) to 700, and its expected lint findings
#: and clip slack are those faces' numbers. Calibri and Calibri Light ship only with Windows/Office,
#: so on Linux (Carlito has 400 and 700 only) the family is skipped with that reason.
FACES_MEASURED: dict[str, tuple[str, ...]] = {"weights": ("Calibri", "Calibri Light")}


@pytest.mark.parametrize("family", [
    pytest.param(family, marks=pytest.mark.xfail(reason=KNOWN_FINDINGS[family], strict=True))
    if family in KNOWN_FINDINGS else family
    for family in FAMILIES
])
def test_family_meets_its_expectations(family: str, runs: dict[str, dict[str, Any]]) -> None:
    require_faces(*FACES_MEASURED.get(family, ()))
    run = runs[family]
    problems = judge_family(run["context"], run["extracted"], run["ir"], run["deck"], run["fit"],
                            run["lint"], text=run["text"])
    assert problems == [], f"{family}: {problems}"


def test_dropped_shadow_warns_only_where_a_family_expects_one(runs: dict[str, dict[str, Any]]) -> None:
    """`judge_family` holds unlisted *errors* against a family, not warns, so the dropped-shadow warn
    (plan §16 #19) is held both ways here: exactly the listed ones, and on the element that has them."""
    for family, run in runs.items():
        said = [d for d in run["ir"].diagnostics if d.message.startswith("box-shadow:")]
        listed = [item for item in (run["context"].expect or {}).get("expectedDiagnostics") or []
                  if str(item.get("match") or "").startswith("box-shadow:")]
        assert len(said) == len(listed), f"{family}: {[d.message for d in said]}"
    boxes = runs["boxes"]["ir"]
    warn, = [d for d in boxes.diagnostics if d.message.startswith("box-shadow:")]
    named = {e.id: e.name for e in boxes.elements}
    assert named[warn.elementId] == "shadow-two"


def test_judge_family_enforces_each_expectation(runs: dict[str, dict[str, Any]]) -> None:
    """The rules bite: a wrong count, an unlisted diagnostic, lint, a text finding, a gate ceiling."""
    from dataclasses import replace

    from app.engine.ir import Diagnostic
    from app.engine.reports import LintFinding, LintReport

    run = runs["text"]
    context = run["context"]

    def judged(expect: dict[str, Any], *, ir: IR | None = None, lint_report: LintReport | None = None,
               gate_px2: float | None = None, text: dict[str, Any] | None = None) -> list[str]:
        patched = replace(context, expect={**(context.expect or {}), **expect})
        return judge_family(patched, run["extracted"], ir or run["ir"], run["deck"], run["fit"],
                            lint_report if lint_report is not None else run["lint"],
                            gate_px2=gate_px2, text=text if text is not None else run["text"])

    assert judged({}) == []
    assert any("text: expected 1" in p for p in judged({"kinds": {"text": 1}}))

    noisy = IR.from_json(run["ir"].to_json())
    noisy.diagnostics.append(Diagnostic(level="warn", source="text-overlap", message="a over b by 9 px"))
    assert any("unexpected diagnostic" in p for p in judged({}, ir=noisy))
    assert judged({"expectedDiagnostics": [{"level": "warn", "match": "a over b"}]}, ir=noisy) == []
    assert any("expected diagnostic missing" in p
               for p in judged({"expectedDiagnostics": [{"level": "error", "match": "nowhere"}]}))

    linted = LintReport(findings=[LintFinding(level="error", rule="script", message="no js")])
    assert any("lint script" in p for p in judged({}, lint_report=linted))
    assert judged({"expectedLint": [{"level": "error", "rule": "script", "count": 1}]},
                  lint_report=linted) == []

    collision = {"slide": 1, "shape": "chips", "paragraph": 0, "line": 0,
                 "joins": [{"left": "partner", "right": "2023"}], "gapPx": 6.0, "method": "boxes"}
    assert any("text collisions" in p for p in judged({}, text={**run["text"], "collisions": [collision]}))
    assert any("text rows" in p for p in judged({}, text={**run["text"], "rows": [{"slide": 1}]}))

    # gateMaxPx2 is read with `is not None`: 0 means pixel-clean, absent means the canvas target
    assert any("gateMaxPx2 0" in p for p in judged({"gateMaxPx2": 0}, gate_px2=12.0))
    assert judged({"gateMaxPx2": 12}, gate_px2=12.0) == []
    # absent (None) means the canvas target; the text family itself now carries a ceiling (plan §11)
    assert any("the gate target 6,000" in p for p in judged({"gateMaxPx2": None}, gate_px2=6_001.0))
    assert judged({"gateMaxPx2": None}, gate_px2=5_999.0) == []


def test_an_expected_chart_spec_error_is_one_finding_not_two(runs: dict[str, dict[str, Any]]) -> None:
    """`chart-specs` keeps two specs the validator rejects on purpose (G-2). Each is reported by lint
    (`chart-spec`) and again by `IR.validate()` on its chart element; `expectedLint` accounts for both
    reports of the same finding, one for one, and for nothing else."""
    from dataclasses import replace

    run = runs["chart-specs"]
    context = run["context"]
    invalid = run["ir"].validate()
    assert len(invalid) == 2 and any("grouped_bar" in p for p in invalid) and any("radar" in p for p in invalid)

    def judged(expect: dict[str, Any]) -> list[str]:
        patched = replace(context, expect={**(context.expect or {}), **expect})
        return judge_family(patched, run["extracted"], run["ir"], run["deck"], run["fit"], run["lint"],
                            text=run["text"])

    assert judged({}) == []
    unlisted = judged({"expectedLint": [{"level": "warn", "rule": "chart-advisory", "count": 5}]})
    assert sum(p.startswith("invalid IR:") for p in unlisted) == 2
    assert sum(p.startswith("lint chart-spec:") for p in unlisted) == 2
    one = judged({"expectedLint": [{"level": "error", "rule": "chart-spec", "count": 1},
                                   {"level": "warn", "rule": "chart-advisory", "count": 5}]})
    assert sum(p.startswith("invalid IR:") for p in one) == 1 and "expected 1 lint error 'chart-spec', got 2" in one


def _text_of(element: Any) -> str:
    return "".join(run.get("text", "") for paragraph in element.paragraphs or []
                   for line in paragraph.get("lines") or [] for run in line.get("runs") or [])


def test_every_family_has_only_its_listed_kinds_and_groups(runs: dict[str, dict[str, Any]]) -> None:
    """`kinds` is a family's whole inventory and `groups` its group count, both on the extracted IR (G-2
    review m4). `judge_family` compares only the kinds a family lists and never reads `groups`, so an
    unexpected image, table or group would pass it; here a kind a family does not list must be absent.
    Measured 2026-09-25: every family already holds both."""
    problems = []
    for family, run in runs.items():
        expect = run["context"].expect or {}
        listed = expect.get("kinds") or {}
        unlisted = {kind: count for kind, count in run["extracted"].counts().items() if kind not in listed and count}
        if unlisted:
            problems.append(f"{family}: kinds it does not list: {unlisted}")
        groups = len(run["extracted"].groups)
        if expect.get("groups") is not None and groups != expect["groups"]:
            problems.append(f"{family}: groups expected {expect['groups']}, got {groups}")
    assert problems == []


def test_the_listed_placeholders_are_the_mapped_ones(runs: dict[str, dict[str, Any]]) -> None:
    """An `elements[]` entry that names a placeholder, or says `null`, is asserted, not only documented (G-2
    review m5): a family that lists any gets exactly those placeholders mapped after classification, and an
    entry's `startsWith` names the text that took it. Since render-check R3 the extractor records
    `data-placeholder`, so `chart-specs`' heading and `layout-misc`'s 1232 px title are the title by their
    attribute although neither is 60 % inside the zone (before, caption A took it and the title mapped to none)."""
    from collections import Counter

    checked, problems = [], []
    for family, run in runs.items():
        listed = [entry for entry in (run["context"].expect or {}).get("elements") or [] if "placeholder" in entry]
        if not listed:
            continue
        checked.append(family)
        mapped = [((e.placeholder.get("type"), e.placeholder.get("idx")), e) for e in run["ir"].elements
                  if e.placeholder]
        wanted = Counter((entry["placeholder"]["type"], entry["placeholder"]["idx"])
                         for entry in listed if entry["placeholder"])
        got = Counter(key for key, _ in mapped)
        if got != wanted:
            problems.append(f"{family}: mapped {dict(got)}, listed {dict(wanted)}")
        for entry in listed:
            wanted_text = entry.get("startsWith")
            if entry["placeholder"] and wanted_text:
                key = (entry["placeholder"]["type"], entry["placeholder"]["idx"])
                texts = [_text_of(e) for k, e in mapped if k == key]
                if not any(text.startswith(wanted_text) for text in texts):
                    problems.append(f"{family}: {key} took {texts}, not {wanted_text!r}…")
    assert problems == []
    assert {"placeholders", "sizes-4x3", "chart-specs", "layout-misc", "weights"} <= set(checked)


def test_weights_tight_card_reaches_its_clip_edge_unreported(runs: dict[str, dict[str, Any]]) -> None:
    """`weights`' card 3, 'Tight fit', ends its last line box within 4 px of its card's clip edge (G-2 review
    m2): `overflow:hidden` clips at the padding box, the card has no border, so the edge is the card's bottom.
    Nothing may be reported for it and the export must fit; card 2's hidden line is the only clip."""
    require_faces(*FACES_MEASURED["weights"])
    run = runs["weights"]
    ir = run["extracted"]
    cards = sorted((e for e in ir.elements if e.kind == "shape" and abs(e.box.w - 360) < 0.5), key=lambda e: e.box.x)
    assert len(cards) == 3

    def text_in(card: Any) -> Any:
        found = [e for e in ir.elements if e.kind == "text" and card.box.x <= e.box.x
                 and e.box.x + e.box.w <= card.box.x + card.box.w and card.box.y <= e.box.y]
        assert len(found) == 1
        return found[0]

    clipped_card, tight = text_in(cards[1]), text_in(cards[2])
    last = tight.paragraphs[-1]["lines"][-1]["box"]
    slack = cards[2].box.y + cards[2].box.h - (last["y"] + last["h"])
    assert 0 <= slack <= 4, f"card 3's last line box ends {slack:.1f} px above its clip edge"
    clips = [d for d in ir.diagnostics if "text is clipped" in d.message]
    assert [d.elementId for d in clips] == [clipped_card.id]
    assert not [d for d in ir.diagnostics if d.source == "text-overflow"]
    assert run["fit"].issues == []

