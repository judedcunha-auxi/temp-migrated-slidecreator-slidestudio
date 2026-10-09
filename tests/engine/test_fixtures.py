"""The torture fixtures and the generated test masters are themselves under test.

Every other package's tests are written *against* these files, so a fixture that is silently wrong
costs more than a bug in the engine: it makes a correct implementation look broken. This module
checks the things that can be checked without an extractor —

* every family has an HTML slide, an `expect.json` and (for the families WP0b owns) a hand-written IR;
* every `expect.json` names a master and a layout that actually exist, at the canvas the HTML declares;
* every hand-written IR passes `IR.validate()` once its repo-relative asset paths are resolved,
  and agrees with its `expect.json` about the canvas and the layout;
* every `data-chart` in the HTML is real JSON that `charts_spec.validate` accepts;
* every asset the HTML references exists in `fixtures/torture/assets/`;
* both generated masters validate and their layout PNGs are exactly canvas-sized;
* the fixtures lint clean (`engine.verify.lint` is a WP5 stub today — this test starts asserting
  something the moment WP5 lands, which is the point of writing it now), less the findings a family
  declares in `expectedLint` because it keeps a contract error on purpose (G-2).

Missing inputs **fail**; nothing here skips (master brief §10.7).
"""
from __future__ import annotations

import html as html_module
import json
import re
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from app.config import engine as config
from app.engine import charts_spec
from app.engine.ir import IR
from app.engine.manifest import Manifest
from app.engine.verify.lint import lint

#: Every family named in master brief §11, with the packages that take ownership after WP0b.
FAMILIES: tuple[str, ...] = (
    "text", "boxes", "images", "tables",
    "svg-shapes", "svg-paths", "svg-charts", "charts",
    "placeholders", "sizes-4x3",
    "charts-hardening", "charts-negative",          # WP-C §8.1
    "charts-rendercheck",                           # render check 2026-09-29, defects 3-6 (R2)
    "charts-labels",                                # render check §9 (G24): label styles, breaks, subtotals
    "paint",   # WP-E: modern colours, gradient text, ::before/::after, CSS border joins
    "rows",
    "as-seen",
    # WP-G (G-2): the fidelity probes promoted (p05 and p11 live on in charts-hardening/-negative)
    "flexgrid", "tables-cells", "pseudo", "colours", "weights", "media", "chart-specs",
    "layout-misc", "text-misc",
    "inline-boxes",   # render check R1: icons, images and dots in inline runs (rendercheck/00-PLAN.md)
    "tables-spacing",   # border-spacing gaps as spacer rows/columns of the native table (rendercheck §9)
)

#: The hand-written IRs WP0b authors. `charts` and `svg-charts` are WP3a's and WP3c's, written in
#: the same wave, so their absence here is correct rather than an omission.
IR_FAMILIES: tuple[str, ...] = (
    "text", "boxes", "images", "tables", "svg-shapes", "svg-paths", "placeholders", "sizes-4x3",
    "charts-hardening", "charts-negative",          # WP-C, written by make_ir.py
    "paint",
)

TEST_MASTERS: tuple[str, ...] = ("test-16x9", "test-4x3")

REQUIRED_EXPECT_KEYS: tuple[str, ...] = ("master", "layoutId", "canvas", "kinds", "rasterAllowed", "charts")

#: `data-chart='{…}'` as the authoring contract writes it: single-quoted attribute, JSON inside.
DATA_CHART = re.compile(r"data-chart='(\{.*?\})'", re.S)

#: The app's asset URL shape; the extractor routes these at `assets_dir`.
ASSET_URL = re.compile(r"/api/projects/[^/\"')]+/assets/([^\"')\s]+)")


# ------------------------------------------------------------------------------------- helpers


def _torture() -> Path:
    directory = config.TORTURE_FIXTURE
    assert directory.is_dir(), f"torture fixtures missing at {directory} (WP0b deliverable)"
    return directory


def _expect(family: str) -> dict[str, Any]:
    path = _torture() / f"{family}.expect.json"
    assert path.exists(), f"{path} is missing"
    return json.loads(path.read_text(encoding="utf-8"))


def _master_manifest(name: str) -> Manifest:
    path = config.MASTERS_FIXTURE / f"{name}.manifest.json"
    assert path.exists(), f"{path} is missing (run tests/engine/fixtures/masters/make_test_masters.py)"
    return Manifest.load(path)


def _resolved_ir(path: Path) -> IR:
    """Load a hand-written IR with its repo-relative `src` paths made absolute (see the README)."""
    ir = IR.load(path)
    for element in ir.elements:
        if element.src and not Path(element.src).is_absolute():
            resolved = (config.PROJECT_ROOT / element.src).resolve()
            assert resolved.exists(), f"{path.name}: asset {element.src} does not exist at {resolved}"
            element.src = str(resolved)
    return ir


# --------------------------------------------------------------------------- the family set


def test_every_family_has_a_slide_and_an_expectation() -> None:
    torture = _torture()
    missing = [f for f in FAMILIES if not (torture / f"{f}.html").exists()]
    assert not missing, f"no HTML for: {missing}"
    missing = [f for f in FAMILIES if not (torture / f"{f}.expect.json").exists()]
    assert not missing, f"no expect.json for: {missing}"


def test_no_stray_fixtures() -> None:
    """A slide with no expectation is a slide nobody is checking."""
    torture = _torture()
    stems = {p.stem for p in torture.glob("*.html")}
    assert stems == set(FAMILIES), f"unexpected or missing fixtures: {stems ^ set(FAMILIES)}"


def test_torture_references_are_fresh() -> None:
    """Every family's committed reference was rendered from today's HTML in its master's fonts.

    `reference/manifest.json` records the HTML's sha256 and the fonts per family
    (`python -m engine reference tests/engine/fixtures/torture` writes both). A family whose HTML changed
    without a re-render is scored against yesterday's picture — the torture gate refuses it, and so
    does this test.
    """
    from app.engine.verify.fixtures import context_for
    from app.engine.verify.torture import load_reference_manifest, reference_problem

    torture = _torture()
    manifest = load_reference_manifest(torture)
    assert manifest, f"{torture / 'reference' / 'manifest.json'} is missing"
    stale = {}
    for family in FAMILIES:
        context = context_for(torture / f"{family}.html")
        problem = reference_problem(torture / f"{family}.html", context.manifest.fonts)
        if problem:
            stale[family] = problem
    assert not stale, stale
    assert set(manifest) >= set(FAMILIES), f"no manifest entry for {set(FAMILIES) - set(manifest)}"


# ------------------------------------------------------------------------------- expect.json


@pytest.mark.parametrize("family", FAMILIES)
def test_expect_json_is_well_formed(family: str) -> None:
    expect = _expect(family)
    missing = [key for key in REQUIRED_EXPECT_KEYS if key not in expect]
    assert not missing, f"{family}.expect.json is missing {missing}"

    assert expect["master"] in TEST_MASTERS, f"{family}: unknown master {expect['master']!r}"
    assert isinstance(expect["kinds"], dict) and expect["kinds"], f"{family}: kinds must be non-empty"
    assert set(expect["kinds"]) <= {"shape", "text", "image", "table", "chart", "raster"}, (
        f"{family}: kinds names a kind the IR does not have: {sorted(expect['kinds'])}"
    )
    assert all(isinstance(v, int) and v >= 0 for v in expect["kinds"].values()), f"{family}: kind counts"
    assert isinstance(expect["rasterAllowed"], list), f"{family}: rasterAllowed must be a list"
    if not expect["rasterAllowed"]:
        assert expect["kinds"].get("raster", 0) == 0, (
            f"{family}: rasterAllowed is empty but {expect['kinds']['raster']} rasters are expected"
        )
    assert set(expect["charts"]) == {"authored", "recognised"}, f"{family}: charts block"


@pytest.mark.parametrize("family", FAMILIES)
def test_expect_json_matches_its_master(family: str) -> None:
    expect = _expect(family)
    manifest = _master_manifest(expect["master"])
    layout = manifest.layout(expect["layoutId"])          # raises if the id is unknown
    assert layout.partName, f"{family}: layout {layout.id} has no partName"

    assert expect["canvas"]["w"] == manifest.canvas_w and expect["canvas"]["h"] == manifest.canvas_h, (
        f"{family}: expect.json canvas {expect['canvas']} but {expect['master']} is "
        f"{manifest.canvas_w}x{manifest.canvas_h}"
    )
    body = (_torture() / f"{family}.html").read_text(encoding="utf-8")
    # Whitespace-insensitive: the contract fixes the canvas, not the CSS formatting, and fixtures
    # are written both minified (`width:1280px`) and expanded (`width: 1280px`).
    declared = {
        (prop, int(value))
        for prop, value in re.findall(r"\b(width|height)\s*:\s*(\d+)px", body)
    }
    assert ("width", manifest.canvas_w) in declared and ("height", manifest.canvas_h) in declared, (
        f"{family}.html does not declare the {manifest.canvas_w}x{manifest.canvas_h} canvas on html,body"
    )


@pytest.mark.parametrize("family", FAMILIES)
def test_expected_placeholders_exist_on_the_layout(family: str) -> None:
    """A fixture may only expect a placeholder the layout actually has, found by idx."""
    expect = _expect(family)
    layout = _master_manifest(expect["master"]).layout(expect["layoutId"])
    by_idx = {p.idx: p for p in layout.placeholders}
    for element in expect.get("elements", []):
        wanted = element.get("placeholder")
        if not wanted:
            continue
        placeholder = by_idx.get(wanted["idx"])
        assert placeholder is not None, (
            f"{family}: element {element.get('name')} expects idx {wanted['idx']}, "
            f"which {layout.id} does not have (has {sorted(by_idx)})"
        )
        assert placeholder.type == wanted["type"], (
            f"{family}: idx {wanted['idx']} on {layout.id} is a {placeholder.type}, "
            f"not a {wanted['type']}"
        )


# --------------------------------------------------------------------------------- the HTML


def _expected_lint(family: str, level: str | None = None, rule: str | None = None) -> dict[str, int]:
    """`expectedLint` as `{rule: count}`, optionally one level or one rule only (G-2)."""
    counts: dict[str, int] = {}
    for item in _expect(family).get("expectedLint") or []:
        if (level and item.get("level") != level) or (rule and item.get("rule") != rule):
            continue
        counts[item["rule"]] = counts.get(item["rule"], 0) + int(item.get("count") or 1)
    return counts


@pytest.mark.parametrize("family", FAMILIES)
def test_authored_chart_specs_are_emittable(family: str) -> None:
    """Every `data-chart` parses as JSON and passes the spec validator the linter will use.

    Except the specs a family keeps *because* the validator rejects them: exactly as many as its
    `expectedLint` counts `chart-spec` errors (`chart-specs`: an unknown type and a radar), and each
    rejection is the message the linter reports for that spec.
    """
    body = (_torture() / f"{family}.html").read_text(encoding="utf-8")
    specs = [json.loads(html_module.unescape(m)) for m in DATA_CHART.findall(body)]

    expected = _expect(family)["charts"]["authored"]
    assert len(specs) == expected, f"{family}.html has {len(specs)} data-chart elements, expected {expected}"
    rejected = {index: charts_spec.validate(spec) for index, spec in enumerate(specs)}
    rejected = {index: problems for index, problems in rejected.items() if problems}
    allowed = _expected_lint(family, rule="chart-spec").get("chart-spec", 0)
    assert len(rejected) == allowed, (
        f"{family}.html: {len(rejected)} rejected spec(s), expectedLint allows {allowed}: "
        + "; ".join(f"chart {i} ({specs[i].get('type')}): {p}" for i, p in rejected.items()))
    if rejected:
        linted = {f.message for f in lint(body, _master_manifest(_expect(family)["master"])).findings
                  if f.rule == "chart-spec"}
        for index, problems in rejected.items():
            assert all(f"data-chart: {p}" in linted for p in problems), (index, problems, linted)


@pytest.mark.parametrize("family", FAMILIES)
def test_referenced_assets_exist(family: str) -> None:
    body = (_torture() / f"{family}.html").read_text(encoding="utf-8")
    assets_dir = _torture() / "assets"
    for name in sorted(set(ASSET_URL.findall(body))):
        assert (assets_dir / name).is_file(), f"{family}.html references {name}, missing from {assets_dir}"


@pytest.mark.parametrize("family", FAMILIES)
def test_no_remote_resources_and_no_script(family: str) -> None:
    """The contract's two hard lint errors: a fetched remote resource, and JavaScript in the slide.

    An `<a href="https://…">` is a hyperlink the browser never fetches, so it is allowed and this
    test deliberately only looks at attributes that cause a request (`src`, `url()`, `@import`).
    """
    body = (_torture() / f"{family}.html").read_text(encoding="utf-8")
    fetched = [
        url for url in re.findall(r"""\bsrc\s*=\s*["']([^"']+)["']""", body)
        if url.startswith(("http://", "https://", "//"))
    ]
    assert not fetched, f"{family}.html fetches remote resources: {fetched}"
    assert "url(http" not in body and "url('http" not in body and 'url("http' not in body, (
        f"{family}.html has a remote url() resource"
    )
    assert "@import" not in body, f"{family}.html has an @import"
    assert "<script" not in body, f"{family}.html contains a script; the contract forbids it"
    assert "javascript:" not in body, f"{family}.html has a javascript: URL"


@pytest.mark.parametrize("family", FAMILIES)
def test_fixture_lints_without_errors(family: str) -> None:
    """Fixtures are written inside the contract, so the static linter must have nothing to say.

    The exception is a family that carries a contract error on purpose (`chart-specs`: a `data-chart`
    the validator rejects) and says so in `expectedLint`: exactly that many errors of that rule are
    subtracted, and anything else is still a failure (G-2).
    """
    expect = _expect(family)
    manifest = _master_manifest(expect["master"])
    report = lint((_torture() / f"{family}.html").read_text(encoding="utf-8"), manifest)
    errors = list(getattr(report, "errors", []))
    for rule, count in _expected_lint(family, level="error").items():
        matching = [f for f in errors if f.rule == rule]
        assert len(matching) == count, f"{family}.html: expected {count} {rule!r} lint error(s), got {matching}"
        errors = [f for f in errors if f.rule != rule]
    assert not errors, f"{family}.html: lint errors {errors}"


# ------------------------------------------------------------------------- the hand-written IRs


def test_wp0b_authored_every_ir_it_owns() -> None:
    ir_dir = _torture() / "ir"
    assert ir_dir.is_dir(), f"{ir_dir} is missing"
    missing = [f for f in IR_FAMILIES if not (ir_dir / f"{f}.json").exists()]
    assert not missing, f"no hand-written IR for: {missing}"


@pytest.mark.parametrize("family", IR_FAMILIES)
def test_hand_written_ir_validates(family: str) -> None:
    path = _torture() / "ir" / f"{family}.json"
    ir = _resolved_ir(path)
    problems = ir.validate(check_files=True)
    assert not problems, f"{family}.json: {problems}"


@pytest.mark.parametrize("family", IR_FAMILIES)
def test_hand_written_ir_agrees_with_its_family(family: str) -> None:
    """The IR is a smaller sample than the HTML, but it must sit on the same master and layout."""
    expect = _expect(family)
    manifest = _master_manifest(expect["master"])
    ir = _resolved_ir(_torture() / "ir" / f"{family}.json")

    assert ir.canvas.w == manifest.canvas_w and ir.canvas.h == manifest.canvas_h, (
        f"{family}.json canvas {ir.canvas.w}x{ir.canvas.h} != {expect['master']}'s "
        f"{manifest.canvas_w}x{manifest.canvas_h}"
    )
    assert ir.slide.layoutId == expect["layoutId"], (
        f"{family}.json is on {ir.slide.layoutId}, expect.json says {expect['layoutId']}"
    )
    manifest.layout(ir.slide.layoutId)  # raises if the layout is unknown
    assert ir.elements, f"{family}.json has no elements"


@pytest.mark.parametrize("family", IR_FAMILIES)
def test_hand_written_ir_round_trips(family: str) -> None:
    """`IR.load(x).dumps()` must equal the file: the fixtures are canonical JSON, byte for byte."""
    path = _torture() / "ir" / f"{family}.json"
    assert IR.load(path).dumps() == path.read_text(encoding="utf-8"), (
        f"{family}.json is not canonical — re-save it with IR.save (see make_ir.py)"
    )


@pytest.mark.parametrize("family", IR_FAMILIES)
def test_hand_written_ir_covers_the_expected_kinds(family: str) -> None:
    """Whatever kinds the family's HTML produces, the sample IR must exercise them too."""
    expect = _expect(family)
    ir = _resolved_ir(_torture() / "ir" / f"{family}.json")
    wanted = {kind for kind, count in expect["kinds"].items() if count}
    # `raster` is the exception: three families declare raster-only HTML constructs but the sample
    # IRs carry a raster only where the emitter needs one to work on.
    missing = wanted - set(ir.counts()) - {"raster"}
    assert not missing, f"{family}.json exercises no {sorted(missing)} element"


# ------------------------------------------------------------------------- the generated masters


@pytest.mark.parametrize("name", TEST_MASTERS)
def test_test_master_is_complete(name: str) -> None:
    deck = config.MASTERS_FIXTURE / f"{name}.pptx"
    assert deck.exists(), f"{deck} is missing"
    manifest = _master_manifest(name)
    problems = manifest.validate(project_dir=config.MASTERS_FIXTURE / name)
    assert not problems, f"{name}: {problems}"
    assert manifest.importer == "deterministic", f"{name}: importer is {manifest.importer!r}"
    assert all(layout.partName for layout in manifest.layouts), f"{name}: a layout has no partName"
    assert manifest.sourceHash, f"{name}: no sourceHash"


@pytest.mark.parametrize("name", TEST_MASTERS)
def test_layout_backgrounds_are_canvas_sized(name: str) -> None:
    """The gate composites the layout PNG with the slide, so they must share a pixel grid exactly."""
    manifest = _master_manifest(name)
    layouts_dir = config.MASTERS_FIXTURE / name / "layouts"
    expected = (manifest.canvas_w, manifest.canvas_h)
    for layout in manifest.layouts:
        png = layouts_dir / str(layout.background)
        assert png.exists(), f"{name}: {png} is missing"
        with Image.open(png) as image:
            assert image.size == expected, f"{name}/{png.name} is {image.size}, expected {expected}"


def test_the_two_masters_have_different_canvases() -> None:
    """Size generality is only proved if the two masters actually differ."""
    wide, narrow = _master_manifest("test-16x9"), _master_manifest("test-4x3")
    assert (wide.canvas_w, wide.canvas_h) == (1280, 720)
    assert (narrow.canvas_w, narrow.canvas_h) == (960, 720)
    assert wide.canvas_w != narrow.canvas_w


def test_a_family_runs_on_each_master() -> None:
    used = {_expect(family)["master"] for family in FAMILIES}
    assert used == set(TEST_MASTERS), f"no torture family runs on {set(TEST_MASTERS) - used}"
