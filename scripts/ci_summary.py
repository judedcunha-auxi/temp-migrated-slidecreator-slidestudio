#!/usr/bin/env python3
"""Writes the page a person reads when they open a CI run: what ran, what it found, what it cost.

    python scripts/ci_summary.py backend >> "$GITHUB_STEP_SUMMARY"

The backend job calls it once, as its last step with `if: always()`, so a red run still says what
happened. It reads what the job left in `ci-reports/` and what GitHub puts in the environment:

    STEPS_JSON       the job's `steps` context: every step's outcome, including the ones that never ran
    RUNTIME_VERSION  the Python version the job used
    ci-reports/      pytest `--junitxml` files and `pip-audit -f json`

A missing or unreadable input is shown as such, never skipped quietly: an empty summary must not look
like a clean one. The script never fails the build, because the steps themselves already do. Standard
library only, so it runs before anything is installed. Anything that came from a report (a test name, a
failure message) is data: it is escaped before it reaches a table cell. Ported from Auxi_Connector,
minus its frontend half.
"""

from __future__ import annotations

import html
import json
import os
import sys
import xml.etree.ElementTree as ET  # noqa: S405 - parses our own pytest output  # nosec B405
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPORTS = Path("ci-reports")
SLOWEST = 10
MAX_LISTED = 30

ICON = {"success": "✅", "failure": "❌", "skipped": "⏭️", "cancelled": "⚪"}


@dataclass
class Case:
    suite: str
    name: str
    seconds: float
    status: str  # passed | failed | skipped
    why: str = ""


@dataclass
class Suite:
    name: str
    seconds: float = 0.0
    cases: list[Case] = field(default_factory=list)
    unreadable: bool = False

    def count(self, status: str) -> int:
        return sum(1 for c in self.cases if c.status == status)


# ── Escaping ─────────────────────────────────────────────────────────────────

def cell(text: object, limit: int = 160) -> str:
    """Report text in a table cell or list item: no markup, no pipe, no backtick."""
    clean = " ".join(str(text).split())[:limit]
    return html.escape(clean, quote=False).replace("|", "&#124;").replace("`", "&#96;")


def code(text: object, limit: int = 160) -> str:
    clean = " ".join(str(text).split())[:limit]
    return "`" + clean.replace("`", "'").replace("|", "\\|") + "`"


def seconds(value: float) -> str:
    return f"{value:.1f} s"


# ── Readers ──────────────────────────────────────────────────────────────────

def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_junit(path: Path) -> Suite:
    suite = Suite(name=path.stem)
    try:
        root = ET.parse(path).getroot()  # noqa: S314  # nosec B314 - our own CI output
    except (ET.ParseError, OSError):
        suite.unreadable = True
        return suite
    suite.seconds = sum(float(n.get("time", 0)) for n in root.iter("testsuite"))
    for node in root.iter("testcase"):
        bad = node.find("failure")
        if bad is None:
            bad = node.find("error")
        skip = node.find("skipped")
        node_why = bad if bad is not None else skip
        why = ""
        if node_why is not None:
            lines = (node_why.get("message") or node_why.text or "").strip().splitlines()
            why = lines[0] if lines else ""
        status = "failed" if bad is not None else "skipped" if skip is not None else "passed"
        name = f"{node.get('classname', '')}::{node.get('name', '')}".strip(":")
        suite.cases.append(Case(suite.name, name, float(node.get("time", 0)), status, why))
    return suite


def read_pip_audit(path: Path) -> tuple[int, list[tuple[str, str, str, str]]] | None:
    data = _json(path)
    if not isinstance(data, dict) or "dependencies" not in data:
        return None
    deps = data["dependencies"]
    found = [
        (d.get("name", "?"), d.get("version", "?"), v.get("id", "?"), ", ".join(v.get("fix_versions") or []))
        for d in deps for v in d.get("vulns") or []
    ]
    return len(deps), found


# ── Rendering ────────────────────────────────────────────────────────────────

def header(title: str, env: Mapping[str, str]) -> list[str]:
    sha = env.get("GITHUB_SHA", "")[:7]
    parts = [p for p in (
        f"commit `{sha}`" if sha else "",
        f"`{cell(env['GITHUB_REF_NAME'])}`" if env.get("GITHUB_REF_NAME") else "",
        cell(env.get("GITHUB_EVENT_NAME", "")),
        cell(env.get("RUNNER_OS", "")),
        cell(env.get("RUNTIME_VERSION", "")),
    ) if p]
    return [f"## {title}", "", " · ".join(parts), ""] if parts else [f"## {title}", ""]


WORDS = {"pip": "pip", "mypy": "mypy", "ruff": "ruff", "pytest": "pytest", "bandit": "bandit",
         "gitleaks": "gitleaks"}


def step_name(step_id: str) -> str:
    first, *rest = step_id.split("-")
    words = [WORDS.get(first) or first.capitalize(), *(WORDS.get(w, w) for w in rest)]
    return " ".join(words)


def steps_section(env: Mapping[str, str]) -> list[str]:
    raw = env.get("STEPS_JSON")
    try:
        steps = json.loads(raw) if raw else {}
    except ValueError:
        steps = {}
    if not steps:
        return ["No step outcomes were passed in (STEPS_JSON).", ""]
    outcomes = {k: (v or {}).get("outcome", "?") for k, v in steps.items()}
    failed = [k for k, o in outcomes.items() if o == "failure"]
    skipped = [k for k, o in outcomes.items() if o == "skipped"]
    if failed:
        verdict = f"❌ **{len(failed)} of {len(outcomes)} steps failed**: " + ", ".join(cell(k) for k in failed)
        if skipped:
            verdict += f". {len(skipped)} did not run."
    else:
        verdict = f"✅ **All {len(outcomes)} steps passed**"
    rows = ["| Step | Result |", "|---|---|"]
    rows += [f"| {cell(step_name(k))} | {ICON.get(o, '❔')} {cell(o)} |" for k, o in outcomes.items()]
    return [verdict, "", "<details><summary>Every step</summary>", "", *rows, "", "</details>", ""]


def tests_section(suites: list[Suite]) -> list[str]:
    if not suites:
        return ["### Tests", "", "No test report was written.", ""]
    rows = ["| Suite | Tests | Passed | Failed | Skipped | Time |", "|---|--:|--:|--:|--:|--:|"]
    for s in suites:
        if s.unreadable:
            rows.append(f"| {cell(s.name)} | report could not be read | | | | |")
            continue
        failed = s.count("failed")
        rows.append(
            f"| {'❌ ' if failed else ''}{cell(s.name)} | {len(s.cases)} | {s.count('passed')} | {failed} "
            f"| {s.count('skipped')} | {seconds(s.seconds)} |"
        )
    ok = [s for s in suites if not s.unreadable]
    cases = [c for s in ok for c in s.cases]
    total = (
        f"**{len(cases)} tests** · {sum(c.status == 'passed' for c in cases)} passed · "
        f"{sum(c.status == 'failed' for c in cases)} failed · {sum(c.status == 'skipped' for c in cases)} skipped · "
        f"{seconds(sum(s.seconds for s in ok))}"
    )
    out = ["### Tests", "", total, "", *rows, ""]
    for status, title in (("failed", "Failing tests"), ("skipped", "Skipped tests")):
        picked = [c for c in cases if c.status == status]
        if picked:
            out += [f"#### {title} ({len(picked)})", ""]
            for c in picked[:MAX_LISTED]:
                out.append(f"- **{cell(c.suite)}** {code(c.name)}" + (f" — {cell(c.why)}" if c.why else ""))
            if len(picked) > MAX_LISTED:
                out.append(f"- …and {len(picked) - MAX_LISTED} more; the step log has them all.")
            out.append("")
    slow = sorted((c for c in cases if c.status == "passed"), key=lambda c: -c.seconds)[:SLOWEST]
    if slow and slow[0].seconds > 0:
        out += [f"<details><summary>Slowest {len(slow)} tests</summary>", "", "| Test | Time |", "|---|--:|"]
        out += [f"| {code(c.name, 110)} | {seconds(c.seconds)} |" for c in slow]
        out += ["", "</details>", ""]
    return out


def pip_audit_section(result: tuple[int, list[tuple[str, str, str, str]]] | None) -> list[str]:
    if result is None:
        return ["### Dependencies", "", "No pip-audit report was written.", ""]
    audited, found = result
    if not found:
        return ["### Dependencies", "", f"✅ {audited} Python packages audited, no known vulnerabilities.", ""]
    rows = ["| Package | Version | Advisory | Fixed in |", "|---|---|---|---|"]
    rows += [f"| {cell(n)} | {cell(v)} | {cell(i)} | {cell(f or 'no fix yet')} |" for n, v, i, f in found]
    return ["### Dependencies", "", f"❌ {len(found)} known vulnerabilities in {audited} Python packages.", "",
            *rows, ""]


def backend(env: Mapping[str, str], reports: Path) -> str:
    suites = [read_junit(p) for p in sorted(reports.glob("*.xml"))] if reports.is_dir() else []
    return "\n".join([
        *header("Backend", env), *steps_section(env), *tests_section(suites),
        *pip_audit_section(read_pip_audit(reports / "pip-audit.json")),
    ])


def main(argv: list[str], env: Mapping[str, str] = os.environ) -> int:
    kind = argv[1] if len(argv) > 1 else ""
    if kind != "backend":
        sys.stderr.write("usage: ci_summary.py backend\n")
        return 2
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # the summary file is UTF-8
    sys.stdout.write(backend(env, REPORTS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
