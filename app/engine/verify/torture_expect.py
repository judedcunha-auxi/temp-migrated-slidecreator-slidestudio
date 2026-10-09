"""What a torture family's `expect.json` accounts for beyond `judge_family`'s own rules (WP-G, G-2).

`chart-specs` (fidelity probe p08) keeps two `data-chart` specs the validator rejects on purpose: an
unknown type and a radar, which are what the charts package (C) made fail visibly. Each rejection is
**one** finding reported twice — by lint (`chart-spec`, "data-chart: <problem>") and by `IR.validate()`
on the chart element ("element[i]/eN: <problem>"). 16-WPG §6's p08 row expects the rejection through
`expectedLint`; `judge_family` (G-1's) runs `IR.validate()` unconditionally, so it reaches these two
functions through one call-site line (plan §8: a new function plus one call-site line). Nothing else
lists `chart-spec`, so no other family is affected.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.engine.reports import LintReport

#: How lint words a rejected spec: the validator's message after this prefix (`lint._lint_chart`).
SPEC_PREFIX = "data-chart: "


def expected_spec_problems(expect: dict[str, Any], lint_report: LintReport | None) -> list[str]:
    """The validator messages a family expects, from the `chart-spec` lint findings it lists.

    No more are returned than `expectedLint` counts for `chart-spec`, at the listed level(s).
    """
    if lint_report is None:
        return []
    listed = [item for item in expect.get("expectedLint") or [] if item.get("rule") == "chart-spec"]
    wanted = sum(int(item.get("count") or 1) for item in listed)
    levels = {item.get("level") for item in listed}
    messages = [finding.message[len(SPEC_PREFIX):] for finding in lint_report.findings
                if finding.rule == "chart-spec" and finding.message.startswith(SPEC_PREFIX)
                and (None in levels or finding.level in levels)]
    return messages[:wanted]


def unexcused(problems: Sequence[str], excused: Sequence[str]) -> list[str]:
    """`IR.validate()` problems less those an expected lint finding accounts for, one for one."""
    left = list(excused)
    kept: list[str] = []
    for problem in problems:
        message = problem.split(": ", 1)[-1]           # "element[4]/e5: <message>"
        if message in left:
            left.remove(message)
            continue
        kept.append(problem)
    return kept
