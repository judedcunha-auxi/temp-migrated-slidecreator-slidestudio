"""scripts/ is not a package (each script runs as `python scripts/x.py`), so put it on
sys.path for the tests, the same way running a script does."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
