"""
SlideForge service: core/storage/paths. Every file path the service writes is built here.

The local adapter and the job workspaces write only under a configured root
(Phase 4 exit criterion: no writes outside the scratch root). `safe_join` is the
one way to turn names into a path below that root: each part must be a single
plain segment, and the joined path, with symlinks resolved, must still be inside
the root. Anything else raises `PathEscape` before a byte is written.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

# One path segment: letters, digits, dot, underscore, hyphen. No separators, no
# drive letters, no "..", no NUL, no leading dot (no hidden files, no "."/"..").
_SEGMENT = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,199}$")
# Names Windows refuses or maps to devices, whatever the extension.
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)

DEFAULT_SCRATCH_DIRNAME = "slideforge-scratch"


class PathEscape(ValueError):
    """A name that is not a single safe segment, or a path that leaves its root."""


def check_segment(part: str) -> str:
    if not isinstance(part, str) or not _SEGMENT.match(part):
        raise PathEscape("not a safe path segment")
    if part.split(".")[0].lower() in _WINDOWS_RESERVED:
        raise PathEscape("not a safe path segment")
    return part


def resolve_root(root: str | os.PathLike[str]) -> Path:
    """The root as an absolute, symlink-resolved path, created if missing."""
    path = Path(root).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve(strict=True)


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def safe_join(root: Path, *parts: str) -> Path:
    """`root/part/part/...`, refusing anything that could land outside `root`.

    `root` must already be resolved (resolve_root). The result is resolved too, so a
    symlink planted inside the root that points outside it is caught here."""
    if not parts:
        raise PathEscape("no path given")
    for part in parts:
        check_segment(part)
    candidate = root.joinpath(*parts)
    # strict=False: the leaf (and its parents) may not exist yet.
    resolved = candidate.resolve(strict=False)
    if not is_within(resolved, root):
        raise PathEscape("path leaves the storage root")
    return resolved


def default_scratch_root() -> Path:
    """Where scratch goes when SCRATCH_ROOT is unset: the system temp directory."""
    return Path(tempfile.gettempdir()) / DEFAULT_SCRATCH_DIRNAME
