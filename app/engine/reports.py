"""Every options and report dataclass the engine passes around — and the only place they live.

v1 had `GateReport` defined twice and `EmitReport` nowhere (`docs/archive/engine/90-CRITIQUE.md` #7). One module owns
them all: packages import from here and never declare a parallel type. Owned by WP0a, frozen after
WP0a — a package that needs another field describes it in its report instead of adding it.

Everything is JSON-able through `to_json()` so `report.json` is a faithful dump of the objects the
suite built, and `SuiteReport.to_markdown()` is what a human reads in the morning.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Literal, cast

from app.engine.ir import IR


def _jsonable(value: Any) -> Any:
    """Dataclasses → dicts, Paths → strings, tuples → lists; everything else untouched."""
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


@dataclass(slots=True)
class Box:
    """A rectangle in a report (gate components, masks). Kept separate from `ir.Box`: reports are
    plain records that survive a JSON round-trip, and a gate component's box is in *render* pixels."""

    x: float
    y: float
    w: float
    h: float


# ------------------------------------------------------------------------------------- export


@dataclass(slots=True)
class ExportOptions:
    """What `app.engine.pipeline.export_deck` was asked to do."""

    out_dir: Path
    slide_ids: list[str] | None = None
    verify: bool = False
    #: Called with `{"type": "status"|"usage"|"export"|"done", ...}` so the caller can stream progress.
    #: The engine never imports the API layer; this callback is the whole bridge.
    on_event: Callable[[dict[str, Any]], None] | None = None
    emit: EmitOptions | None = None
    #: The export's scratch folder: derived images are written under `workspace/derived/`. One per
    #: job, never shared; `None` means `out_dir / "work"`.
    workspace: Path | None = None

    @property
    def work_dir(self) -> Path:
        return Path(self.workspace) if self.workspace is not None else Path(self.out_dir) / "work"

    def event(self, **payload: Any) -> None:
        """Emit a progress event, ignoring the absence of a listener."""
        if self.on_event is not None:
            self.on_event(payload)


@dataclass(slots=True)
class EmitOptions:
    """Switches on the emitter (WP4). Defaults are what the acceptance gate is measured with."""

    line_lock: bool = True
    width_lock: bool = True
    theme_fonts: bool = True
    group_svg: bool = True
    name_shapes: bool = True
    finalize: bool = True


@dataclass(slots=True)
class EmitReport:
    """What the emitter actually produced — the input to coverage, and the honest record of drops."""

    shapes_by_kind: dict[str, int] = field(default_factory=dict)
    rasters: list[dict[str, Any]] = field(default_factory=list)          # [{id, reason}]
    charts: list[dict[str, Any]] = field(default_factory=list)           # [{id, origin, type}]
    placeholders_used: list[dict[str, Any]] = field(default_factory=list)  # [{slide, type, idx}]
    warnings: list[str] = field(default_factory=list)
    #: Constructs we could emit only approximately (e.g. custDash the renderer draws solid).
    renderer_gaps: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return cast(dict[str, Any], _jsonable(self))


@dataclass(slots=True)
class ExportResult:
    """The product of one export. Carries the IRs so the suite never has to re-extract."""

    pptx: Path
    irs: list[IR] = field(default_factory=list)
    emit_report: EmitReport | None = None
    slide_ids: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    per_slide_s: dict[str, float] = field(default_factory=dict)
    cost: float = 0.0
    renderer_warnings: list[str] = field(default_factory=list)
    #: Re-runs this exact export into a fresh directory. `run_suite(--determinism)` calls it twice
    #: and compares sha256; `None` when the caller cannot reproduce the run.
    rerun: Callable[[Path], ExportResult] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "pptx": str(self.pptx),
            "slideIds": list(self.slide_ids),
            "elapsedS": round(self.elapsed_s, 3),
            "perSlideS": {k: round(v, 3) for k, v in self.per_slide_s.items()},
            "cost": self.cost,
            "rendererWarnings": list(self.renderer_warnings),
            "emitReport": self.emit_report.to_json() if self.emit_report else None,
        }


# ------------------------------------------------------------------------------------- verify


@dataclass(slots=True)
class LintFinding:
    level: Literal["error", "warn", "info"]
    rule: str
    message: str
    line: int | None = None
    snippet: str | None = None


@dataclass(slots=True)
class LintReport:
    """Static findings on one slide's HTML (no browser, < 50 ms). Owner: WP5."""

    slide_id: str | None = None
    findings: list[LintFinding] = field(default_factory=list)

    @property
    def errors(self) -> list[LintFinding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[LintFinding]:
        return [f for f in self.findings if f.level == "warn"]

    @property
    def clean(self) -> bool:
        return not self.errors

    def to_json(self) -> dict[str, Any]:
        return cast(dict[str, Any], _jsonable(self))


@dataclass(slots=True)
class FitIssue:
    slide: int
    shape: str
    kind: str          # "overflow" | "off-slide" | "zero-width" | "line-count" | "narrow-box" | "overhang"
    detail: str


@dataclass(slots=True)
class FitReport:
    """Predicted text problems in the emitted deck, before anyone opens it. Owner: WP5."""

    issues: list[FitIssue] = field(default_factory=list)
    slides_checked: int = 0
    #: Per slide: what the predictor made of it (lines predicted vs lines in the IR, per text box).
    details: list[dict[str, Any]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.issues

    def to_json(self) -> dict[str, Any]:
        return cast(dict[str, Any], _jsonable(self))


@dataclass(slots=True)
class GateSlide:
    index: int
    defect_area: int = 0
    clean: bool = True
    components: list[Box] = field(default_factory=list)
    diff_png: Path | None = None
    #: Area outside the masked regions — the number the acceptance gate reads.
    non_chart_area: int | None = None


@dataclass(slots=True)
class GateReport:
    """The pixel gate: visible defects between the browser composite and PptxRender's render."""

    slides: list[GateSlide] = field(default_factory=list)
    slides_checked: int = 0
    total_area: int = 0
    settings: dict[str, Any] = field(default_factory=dict)
    #: Which renderer produced the numbers ("pptxrender", or "none" when the gate did not run), so a
    #: number can be traced to a renderer.
    source: str = "none"
    warnings: list[str] = field(default_factory=list)

    @property
    def clean_slides(self) -> int:
        return sum(1 for s in self.slides if s.clean)

    def to_json(self) -> dict[str, Any]:
        return cast(dict[str, Any], _jsonable(self))


@dataclass(slots=True)
class CoverageReport:
    """What became native, what did not, and whether the template survived. Owner: WP5."""

    per_slide: list[dict[str, Any]] = field(default_factory=list)
    rasters: list[dict[str, Any]] = field(default_factory=list)       # [{slide, id, reason}]
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    charts: list[dict[str, Any]] = field(default_factory=list)        # structural chart checks
    template: dict[str, Any] = field(default_factory=dict)            # hash-equality + placeholder checks
    #: The text row (`engine.verify.text_deck.text_report`): `{"checked", "collisions",
    #: "exportOverlaps", "unmatched", "skipped"}`. Every list-valued key is a list of findings, and
    #: `clean`, `## Text`, the suite row and the torture columns iterate them all, so a check added
    #: to the dict later (B's `rows`, `positions`, `seams`) reaches them with no edit here.
    text: dict[str, Any] = field(default_factory=dict)
    renderer_warnings_ours: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)

    @property
    def raster_count(self) -> int:
        return len(self.rasters)

    @property
    def text_findings(self) -> dict[str, list[Any]]:
        """The text row's findings, per check: every list-valued key of `text`."""
        return {key: value for key, value in self.text.items() if isinstance(value, list)}

    @property
    def clean(self) -> bool:
        return (not self.rasters and not self.renderer_warnings_ours
                and bool(self.template.get("ok", True))
                and not any(self.text_findings.values()))

    def to_json(self) -> dict[str, Any]:
        return cast(dict[str, Any], _jsonable(self))


@dataclass(slots=True)
class ValidationReport:
    """`validate.py deck.pptx --original master.pptx` — the OOXML structural validator."""

    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    skipped: str | None = None

    def to_json(self) -> dict[str, Any]:
        return cast(dict[str, Any], _jsonable(self))


@dataclass(slots=True)
class DeterminismReport:
    """Two exports of the same input must be byte-identical (master brief §9)."""

    checked: bool = False
    identical: bool | None = None
    sha256: list[str] = field(default_factory=list)
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return cast(dict[str, Any], _jsonable(self))


@dataclass(slots=True)
class Timing:
    """Seconds per phase. `per_slide_export` is the number master brief §9 caps at 10 s."""

    extract: float = 0.0
    classify: float = 0.0
    emit: float = 0.0
    verify: float = 0.0
    per_slide_export: float = 0.0
    total: float = 0.0


@dataclass(slots=True)
class SuiteReport:
    """Everything `run_suite` learned about one deck."""

    project_id: str | None = None
    pptx: Path | None = None
    slide_ids: list[str] = field(default_factory=list)
    lint: list[LintReport] = field(default_factory=list)
    fit: FitReport | None = None
    gate: GateReport | None = None
    coverage: CoverageReport | None = None
    validation: ValidationReport | None = None
    determinism: DeterminismReport | None = None
    timing: Timing = field(default_factory=Timing)
    cost: float = 0.0
    #: Per acceptance row (master brief §9): {"coverage": True, "fit": False, …}. Empty until WP5.
    gate_results: dict[str, bool] = field(default_factory=dict)
    #: Per-slide deltas against a `baseline=` report, when one was given.
    baseline_delta: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True only when every acceptance row that ran, passed. No rows = not a pass."""
        return bool(self.gate_results) and all(self.gate_results.values())

    def to_json(self) -> dict[str, Any]:
        return cast(dict[str, Any], _jsonable(self))

    def to_markdown(self) -> str:
        """`report.md` — the one-screen summary a human reads first."""
        lines: list[str] = []
        title = "# Export report"
        if self.project_id:
            title += f" — {self.project_id}"
        lines += [title, ""]
        if self.pptx:
            lines += [f"`{self.pptx}`", ""]

        lines += ["| check | result |", "|---|---|"]
        for name, ok in self.gate_results.items():
            lines.append(f"| {name} | {'PASS' if ok else 'FAIL'} |")
        if not self.gate_results:
            lines.append("| (no checks ran) | — |")
        lines.append("")

        if self.gate and self.gate.slides:
            lines += ["## Gate (visible defect area)", "",
                      "| slide | area px² | non-chart px² | clean | components |", "|---|---|---|---|---|"]
            for slide in self.gate.slides:
                non_chart = "—" if slide.non_chart_area is None else f"{slide.non_chart_area:,}"
                lines.append(
                    f"| {slide.index} | {slide.defect_area:,} | {non_chart} | "
                    f"{'yes' if slide.clean else 'no'} | {len(slide.components)} |"
                )
            lines += ["", f"renderer: {self.gate.source}, slides checked: {self.gate.slides_checked}", ""]

        if self.fit:
            lines += [f"## Fit — {len(self.fit.issues)} issue(s) over {self.fit.slides_checked} slide(s)", ""]
            for issue in self.fit.issues[:20]:
                lines.append(f"- slide {issue.slide} · {issue.kind} · {issue.shape}: {issue.detail}")
            if len(self.fit.issues) > 20:
                lines.append(f"- … {len(self.fit.issues) - 20} more")
            lines.append("")

        if self.coverage:
            lines += [f"## Coverage — {self.coverage.raster_count} raster(s)", ""]
            for raster in self.coverage.rasters[:20]:
                lines.append(f"- {raster.get('id')}: {raster.get('reason')}")
            if self.coverage.renderer_warnings_ours:
                lines += ["", "Renderer warnings attributable to us:"]
                lines += [f"- {w}" for w in self.coverage.renderer_warnings_ours[:20]]
            lines.append("")
            if self.coverage.text.get("checked"):
                lines += _text_section(self.coverage.text)

        errors = [f for report in self.lint for f in report.errors]
        warnings = [f for report in self.lint for f in report.warnings]
        if errors or warnings:
            lines += [f"## Lint — {len(errors)} error(s), {len(warnings)} warning(s)", ""]
            for finding in (errors + warnings)[:20]:
                lines.append(f"- **{finding.level}** {finding.rule}: {finding.message}")
            lines.append("")

        if self.validation:
            state = "clean" if self.validation.ok else f"{len(self.validation.errors)} error(s)"
            lines += [f"## Validation — {state} ({self.validation.elapsed_s:.1f}s)", ""]
            for error in self.validation.errors[:20]:
                lines.append(f"- {error}")
            lines.append("")

        if self.determinism and self.determinism.checked:
            verdict = "byte-identical" if self.determinism.identical else "DIFFERENT"
            lines += [f"## Determinism — {verdict}", "", self.determinism.detail, ""]

        timing = self.timing
        lines += [
            "## Timing (s)", "",
            "| extract | classify | emit | verify | per slide export | total |",
            "|---|---|---|---|---|---|",
            f"| {timing.extract:.2f} | {timing.classify:.2f} | {timing.emit:.2f} | {timing.verify:.2f} "
            f"| {timing.per_slide_export:.2f} | {timing.total:.2f} |",
            "",
        ]
        if self.cost:
            lines += [f"Cost: ${self.cost:.2f}", ""]
        if self.baseline_delta:
            lines += ["## Against baseline", "", "```json", str(self.baseline_delta), "```", ""]
        if self.notes:
            lines += ["## Notes", ""] + [f"- {note}" for note in self.notes] + [""]
        return "\n".join(lines)


#: How `## Text` names the findings of each text check; a key it does not know is named by itself.
_TEXT_LABELS: dict[str, str] = {
    "collisions": "collision(s)",
    "exportOverlaps": "export-introduced overlap(s)",
}


def _text_finding(key: str, entry: Any) -> str:
    """One text finding, for a person: the glued joins, the overlapping pair, or the raw entry."""
    if not isinstance(entry, dict):
        return f"- {key}: {entry}"
    slide = entry.get("slide")
    if key == "collisions":
        joins = ", ".join(f"{j.get('left')!r} + {j.get('right')!r}" for j in entry.get("joins") or [])
        return (f"- slide {slide} · {entry.get('shape')} paragraph {entry.get('paragraph')} line "
                f"{entry.get('line')}: {joins} — browser gap {entry.get('gapPx')} px ({entry.get('method')})")
    if key == "exportOverlaps":
        return (f"- slide {slide}: {entry.get('a')!r} and {entry.get('b')!r} share "
                f"{entry.get('sharedPx')} px of ink (design: {entry.get('designSharedPx')} px)")
    detail = ", ".join(f"{k}={v}" for k, v in entry.items() if k != "slide")
    return f"- slide {slide} · {key}: {detail}"


def _text_section(text: dict[str, Any]) -> list[str]:
    """`## Text — N collision(s), M export-introduced overlap(s)[, …]` and the first 20 findings."""
    findings = {key: value for key, value in text.items() if isinstance(value, list)}
    counts = ", ".join(f"{len(value)} {_TEXT_LABELS.get(key, key)}" for key, value in findings.items())
    lines = [f"## Text — {counts or 'no checks'}", ""]
    shown = [(key, entry) for key, value in findings.items() for entry in value][:20]
    lines += [_text_finding(key, entry) for key, entry in shown]
    total = sum(len(value) for value in findings.values())
    if total > 20:
        lines.append(f"- … {total - 20} more")
    if text.get("unmatched") or text.get("skipped"):
        lines.append(f"({text.get('unmatched', 0)} text shape(s) without an IR element, "
                     f"{text.get('skipped', 0)} line(s) or shape(s) not judged)")
    lines.append("")
    return lines
