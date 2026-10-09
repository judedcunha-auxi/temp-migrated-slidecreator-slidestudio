"""The export lint and the design review of a slide, as the tail of a tool result.

Ported from Slide Studio `server/chat/slide_lint.py` (migration plan §4.1, K). The measurements come
from the context's services (`DesignContext.svc`), so a test can stub them; the design review now
includes the workzone and header-band rules when the deck has a workzone.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.design.context import DesignContext

_log = logging.getLogger(__name__)

JSON = dict[str, Any]

#: How many findings a tool result lists before it says "and N more".
MAX_FINDINGS = 8


def _line(f: JSON, where: str) -> str:
    message = " ".join(str(f.get("message") or "").split())
    if len(message) > 220:
        message = message[:217].rstrip() + "…"
    level = "error" if f.get("level") == "error" else "warning"
    return f"- {level} {f.get('rule')}{where}: {message}"


def lint_lines(ctx: DesignContext, sid: str) -> list[str] | None:
    """The slide's lint errors and warnings as short lines, or None when the linter could not run."""
    try:
        report = ctx.svc.lint(ctx.deck, sid)
    except Exception:  # noqa: BLE001 - the save stands whatever the linter does
        _log.warning("export lint failed after a save", exc_info=True)
        return None
    findings = [f for f in report.get("findings") or [] if f.get("level") in ("error", "warn")]
    findings.sort(key=lambda f: f.get("level") != "error")
    lines = [_line(f, f" (line {f['line']})" if f.get("line") else "") for f in findings[:MAX_FINDINGS]]
    if len(findings) > MAX_FINDINGS:
        lines.append(f"- … and {len(findings) - MAX_FINDINGS} more")
    return lines


def lint_text(ctx: DesignContext, sid: str) -> str:
    """What the linter says about a slide's current version, as the tail of a tool result."""
    lines = lint_lines(ctx, sid)
    if lines is None:
        return "Export lint: not checked (the linter is unavailable)."
    if not lines:
        return "Export lint: clean."
    errors = any(line.startswith("- error") for line in lines)
    head = ("Export lint found problems the export cannot rebuild; fix them and save again:" if errors
            else "Export lint warnings (the slide exports, but check these):")
    return "\n".join([head, *lines])


def design_findings(ctx: DesignContext, sid: str) -> list[JSON] | None:
    """The design review's findings (warn/error), or None when there is no reviewer."""
    review = ctx.svc.review
    if review is None:
        return None
    return [f for f in review(ctx.deck, sid) or [] if isinstance(f, dict) and f.get("level") in ("error", "warn")]


def design_text(ctx: DesignContext, sid: str) -> str | None:
    """The design review of a slide's current version as the tail of a tool result, or None when
    there is no design reviewer. It measures in a browser (about a second), so callers ask once."""
    try:
        findings = design_findings(ctx, sid)
    except Exception as exc:  # noqa: BLE001 - a reviewer that throws never costs the save
        _log.warning("design review raised; skipped for this save", exc_info=True)
        reason = " ".join(str(exc).split())[:160] or type(exc).__name__
        return f"Design review: not checked ({reason})."
    if findings is None:
        return None
    if not findings:
        return "Design review: clean."
    findings.sort(key=lambda f: f.get("level") != "error")
    lines = [_line(f, f" [{' '.join(str(f['element']).split())[:80]}]" if f.get("element") else "")
             for f in findings[:MAX_FINDINGS]]
    if len(findings) > MAX_FINDINGS:
        lines.append(f"- … and {len(findings) - MAX_FINDINGS} more")
    head = "Design review (measured layout and style; the slide exports either way, but a partner would notice):"
    return "\n".join([head, *lines])
