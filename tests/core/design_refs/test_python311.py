"""Every design_refs module parses with the Python 3.11 grammar (decision D2: the service runs 3.11).

Slide Studio targeted 3.12; its `design_lint.py` had a backslash inside an f-string expression (PEP 701,
3.12-only syntax). The port moved it into a helper (`design_lint._unsigned`); this keeps it that way.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[3] / "app" / "core" / "design_refs"


@pytest.mark.parametrize("path", sorted(PACKAGE.glob("*.py")), ids=lambda p: p.name)
def test_parses_with_the_311_grammar(path: Path) -> None:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 11))


def test_the_negative_figure_helper_strips_the_sign() -> None:
    from app.core.design_refs.design_lint import _unsigned

    assert _unsigned("-1.2") == "1.2" and _unsigned("– 3%") == "3%" and _unsigned("4") == "4"
