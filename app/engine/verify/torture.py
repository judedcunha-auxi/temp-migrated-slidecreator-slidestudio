"""The torture runner's rules, in one place: what a family must produce, and whether it did.

The torture run takes every family in `fixtures/torture/` through extract → classify →
emit → fit → the pixel gate → the text row, on its own test master, and `judge_family` holds every
per-family expectation of `<family>.expect.json` (16-WPG §5, plan D7):

* `kinds` — element counts per kind on the **extracted** IR (the recogniser consumes shapes into
  charts, so counting after classification would report a healthy recogniser as a regression);
* `rasterAllowed` — substrings of the raster reasons the contract allows here (empty = none);
* `charts` — `{authored, recognised}` after classification; `IR.validate()` of the classified IR;
* `expectedDiagnostics` — `[{level, match}]`: every listed one must appear, and any diagnostic of
  level `error` (except a `rasterised:` one `rasterAllowed` covers) or with source `text-overflow` /
  `text-overlap` that is *not* listed is a problem;
* `expectedLint` — `[{level, rule, count}]`: exactly that many findings; any other lint error is a
  problem;
* fit issues; the gate ceiling when the gate leg ran — top-level `gateMaxPx2` with **`is not None`**
  semantics (`0` means pixel-clean; absent → `config.gate_target_px2` for the canvas); and the text
  row's lists when given (every list-valued key, so a check added to `text_deck` later counts too).

The committed references are checked for freshness: `reference/manifest.json` records, per family,
the sha256 of the HTML and the fonts each PNG was rendered from. A missing or stale reference is a
problem, never a skip — a gate number against yesterday's picture is not evidence.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from app.config import engine as config
from app.engine.ir import IR
from app.engine.renderer import Renderer
from app.engine.reports import FitReport, LintReport
from app.engine.verify.torture_expect import expected_spec_problems, unexcused

#: `reference/manifest.json` beside the reference PNGs of a fixture directory.
MANIFEST_NAME = "manifest.json"

#: Diagnostic sources that are findings about the text layout itself (`page.js`, `text_layout`).
TEXT_LAYOUT_SOURCES = ("text-overflow", "text-overlap")


# ----------------------------------------------------------------------------------- references


def html_sha256(html: Path) -> str:
    """The HTML's content hash, line endings normalised: a CRLF checkout is the same slide."""
    return hashlib.sha256(Path(html).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def reference_dir(fixture_dir: Path) -> Path:
    return Path(fixture_dir) / "reference"


def load_reference_manifest(fixture_dir: Path) -> dict[str, Any]:
    path = reference_dir(fixture_dir) / MANIFEST_NAME
    if not path.exists():
        return {}
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def reference_problem(html: Path, fonts: dict[str, str]) -> str | None:
    """Why the committed reference of this slide cannot be scored against — or None when it can."""
    html = Path(html)
    png = reference_dir(html.parent) / f"{html.stem}.png"
    if not png.exists():
        return f"reference missing ({png.name}) — regenerate with `app.engine.verify.torture.write_references`"
    entry = load_reference_manifest(html.parent).get(html.stem)
    if not entry:
        return (f"reference not in {MANIFEST_NAME} — regenerate with `app.engine.verify.torture.write_references`")
    if entry.get("htmlSha256") != html_sha256(html):
        return "reference stale — regenerate with `app.engine.verify.torture.write_references`"
    if dict(entry.get("fonts") or {}) != dict(fonts or {}):
        return (f"reference stale — rendered in {entry.get('fonts')}, the master's fonts are {fonts}; "
                f"regenerate with `app.engine.verify.torture.write_references`")
    return None


def write_references(fixture_dir: Path, stems: Sequence[str] | None = None) -> tuple[int, list[str]]:
    """Render the browser composites of a fixture directory in each slide's master fonts.

    Writes `reference/<stem>.png` and records `{htmlSha256, fonts, renderedAt}` per stem in
    `reference/manifest.json` (other stems' entries are kept). Returns `(written, problems)`.
    """
    from app.engine.extract.html import render_reference, title_zone_of
    from app.engine.verify.fixtures import FixtureError, context_for

    fixture_dir = Path(fixture_dir)
    slides = sorted(fixture_dir.glob("*.html"))
    if stems:
        wanted = set(stems)
        slides = [html for html in slides if html.stem in wanted]
    out_dir = reference_dir(fixture_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_reference_manifest(fixture_dir)
    scripts = (config.CHART_PREVIEW_JS,) if config.CHART_PREVIEW_JS.exists() else ()
    written, problems = 0, []
    today = _dt.date.today().isoformat()
    for html in slides:
        try:
            context = context_for(html)
            layout_png = context.layout_png
        except FixtureError as error:
            problems.append(f"{html.name}: {error}")
            continue
        if not layout_png.exists():
            problems.append(f"{html.name}: no layout background at {layout_png}")
            continue
        fonts = dict(context.manifest.fonts)
        render_reference(html, layout_png, out_dir / f"{html.stem}.png",
                         assets_dir=context.assets_dir, fonts=fonts, scripts=scripts,
                         title_zone=title_zone_of(context.manifest, context.layout_id))
        manifest[html.stem] = {"htmlSha256": html_sha256(html), "fonts": fonts, "renderedAt": today}
        written += 1
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(dict(sorted(manifest.items())), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return written, problems


# ------------------------------------------------------------------------------------- the gate leg


def gate_row(
    html: Path, context: Any, ir: IR, deck: Path, out_dir: Path, *, renderer: Renderer
) -> tuple[dict[str, Any], float | None]:
    """Score one family's export against its committed reference, masked as the suite masks.

    Returns the `torture.json` gate fields (`referenceFresh`, `gateMaxPx2`, and when it ran
    `nonChartArea`, `defectArea`, `components`, or `problems` when it could not) and the masked area
    `judge_family` compares with the ceiling — None when the gate did not run.
    """
    from app.engine.verify.gate import gate
    from app.engine.verify.suite import masks_for

    manifest = context.manifest
    row: dict[str, Any] = {
        "gateMaxPx2": gate_ceiling(context.expect or {}, manifest.canvas_w, manifest.canvas_h),
        "problems": [],
    }
    stale = reference_problem(html, manifest.fonts)
    row["referenceFresh"] = stale is None
    if stale:
        row["problems"].append(stale)
        return row, None
    reference = reference_dir(Path(html).parent) / f"{Path(html).stem}.png"
    report = gate(deck, [reference], [masks_for(ir, manifest)], out_dir, renderer=renderer)
    if not report.slides:
        row["problems"].append("gate: the renderer scored no slide")
        return row, None
    slide = report.slides[0]
    row.update({"nonChartArea": slide.non_chart_area, "defectArea": slide.defect_area,
                "components": len(slide.components)})
    return row, float(slide.non_chart_area or 0)


# ---------------------------------------------------------------------------------------- judging


def gate_ceiling(expect: dict[str, Any], canvas_w: int, canvas_h: int) -> float | None:
    """`gateMaxPx2` when the family sets one (`0` is a real ceiling), else the canvas's target."""
    ceiling = expect.get("gateMaxPx2")
    if ceiling is not None:
        return float(ceiling)
    return config.gate_target_px2(canvas_w, canvas_h)


def _matches(expected: dict[str, Any], diagnostic: Any) -> bool:
    if expected.get("level") and expected["level"] != diagnostic.level:
        return False
    needle = str(expected.get("match") or "").lower()
    return needle in f"{diagnostic.source} {diagnostic.message}".lower()


def _raster_allowed(reason: str, allowed: Sequence[str]) -> bool:
    """Substring match, blind to quote characters: `data-pptx=raster` covers `data-pptx="raster"`."""
    def bare(text: str) -> str:
        return text.replace('"', "").replace("'", "")

    return any(bare(str(item)) in bare(reason) for item in allowed)


def judge_family(
    context: Any,
    ir_extracted: IR,
    ir_classified: IR,
    deck: Path | None,
    fit_report: FitReport | None,
    lint_report: LintReport | None,
    *,
    gate_px2: float | None = None,
    text: dict[str, Any] | None = None,
) -> list[str]:
    """Every way this family's run falls short of its `expect.json`, as sentences. Empty = clean."""
    expect = dict(getattr(context, "expect", None) or {})
    problems: list[str] = []

    counts = ir_extracted.counts()
    for kind, wanted in (expect.get("kinds") or {}).items():
        got = counts.get(kind, 0)
        if got != wanted:
            problems.append(f"{kind}: expected {wanted}, got {got}")

    allowed = [str(item) for item in expect.get("rasterAllowed") or []]
    for element in ir_extracted.elements:
        if element.kind != "raster":
            continue
        reason = str(element.reason or "")
        if not _raster_allowed(reason, allowed):
            problems.append(f"raster {element.id} ({reason or 'no reason'}) is not in rasterAllowed")

    charts = [e for e in ir_classified.elements if e.kind == "chart"]
    for origin, want in (expect.get("charts") or {}).items():
        got = sum(1 for chart in charts if chart.origin == origin)
        if want is not None and got != want:
            problems.append(f"charts {origin}: expected {want}, got {got}")

    problems.extend(f"invalid IR: {problem}" for problem in
                    unexcused(ir_classified.validate(), expected_spec_problems(expect, lint_report))[:5])

    listed = [dict(item) for item in expect.get("expectedDiagnostics") or []]
    diagnostics = list(ir_classified.diagnostics)
    for item in listed:
        if not any(_matches(item, d) for d in diagnostics):
            problems.append(f"expected diagnostic missing: {item.get('level')} {item.get('match')!r}")
    for diagnostic in diagnostics:
        judged = diagnostic.level == "error" or diagnostic.source in TEXT_LAYOUT_SOURCES
        if not judged or any(_matches(item, diagnostic) for item in listed):
            continue
        if diagnostic.message.startswith("rasterised:") and _raster_allowed(diagnostic.message, allowed):
            continue
        problems.append(f"unexpected diagnostic: {diagnostic.level} {diagnostic.source}: "
                        f"{diagnostic.message[:120]}")

    if lint_report is not None:
        findings = list(lint_report.findings)
        for item in expect.get("expectedLint") or []:
            level, rule, count = item.get("level"), item.get("rule"), int(item.get("count") or 1)
            matching = [f for f in findings if f.rule == rule and (not level or f.level == level)]
            if len(matching) != count:
                problems.append(f"expected {count} lint {level} {rule!r}, got {len(matching)}")
            for finding in matching[:count]:
                findings.remove(finding)
        problems.extend(f"lint {f.rule}: {f.message[:120]}" for f in findings if f.level == "error")

    if fit_report is not None:
        problems.extend(f"fit: {issue.kind} {issue.shape}" for issue in fit_report.issues)

    if gate_px2 is not None:
        manifest = context.manifest
        ceiling = gate_ceiling(expect, manifest.canvas_w, manifest.canvas_h)
        if ceiling is not None and gate_px2 > ceiling:
            source = "gateMaxPx2" if expect.get("gateMaxPx2") is not None else "the gate target"
            problems.append(f"gate: {gate_px2:,.0f} px² masked exceeds {source} {ceiling:,.0f} px²")

    for key, entries in (text or {}).items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            problems.append(f"text {key}: {_describe(key, entry)}")
    return problems


def _describe(key: str, entry: Any) -> str:
    if isinstance(entry, dict) and key == "collisions":
        joins = ", ".join(f"{j.get('left')!r}+{j.get('right')!r}" for j in entry.get("joins") or [])
        return f"{entry.get('shape')}: {joins} ({entry.get('gapPx')} px, {entry.get('method')})"
    if isinstance(entry, dict) and key == "exportOverlaps":
        return f"{entry.get('a')!r} over {entry.get('b')!r} by {entry.get('sharedPx')} px"
    return str(entry)[:160]


def text_columns(text: dict[str, Any]) -> dict[str, int]:
    """`torture.json` counts per text check: `textCollisions`, `exportOverlaps`, and any key added."""
    names = {"collisions": "textCollisions", "exportOverlaps": "exportOverlaps"}
    return {names.get(key, f"text{key[:1].upper()}{key[1:]}"): len(value)
            for key, value in (text or {}).items() if isinstance(value, list)}
