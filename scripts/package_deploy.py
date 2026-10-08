#!/usr/bin/env python3
"""Build the deploy package, stamped with the commit it was built from.

    python scripts/package_deploy.py               # build staging-deploy.zip
    python scripts/package_deploy.py --allow-dirty # build from an uncommitted tree, knowingly

Writes build_info.json (sha, branch, dirty, builtAt) and zips it with the app, so
/healthz can say which commit is running: "what is deployed?" becomes one request.
CI builds this from the exact commit it tested (the `Deploy artifact (SHA-addressed)`
job) and uploads it as `deploy-<sha>`.

The include list is explicit: a naive zip of the working tree carries .git, .env,
caches and tests. The sanity check ABORTS (not warns) when the package is missing
what it must contain or carries what it must not.

There is no --deploy yet. Where the package goes depends on decision D2 (App Service
for Containers, Container Apps, or zip deploy), D18 (secrets) and D19 (GitHub to
Azure credentials); the deploy step is added when they are settled. If D2 picks a
container, this script still stamps build_info.json, and the image copies it in.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Any

from _common import ROOT, git

ZIP_PATH = ROOT / "staging-deploy.zip"
BUILD_INFO = ROOT / "build_info.json"

INCLUDE = ["app", "requirements.txt", "startup.txt", "build_info.json"]
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo"}

# Present in a package means something went badly wrong.
FORBIDDEN_NAMES = {".env", ".env.local", ".env.deploy"}
MUST_HAVE = ("app/main.py", "requirements.txt", "startup.txt", "build_info.json")


def build_info() -> dict[str, Any]:
    """The commit identity of the working tree."""
    return {
        "sha": git("rev-parse", "HEAD"),
        "branch": os.environ.get("GITHUB_REF_NAME") or git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
        "builtAt": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
    }


def write_build_info(info: dict[str, Any], path: Path = BUILD_INFO) -> None:
    path.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    print(f"  build_info  : {str(info['sha'])[:12]} ({info['branch']}"
          f"{', DIRTY TREE' if info['dirty'] else ''})")


def package(root: Path = ROOT, zip_path: Path = ZIP_PATH) -> list[str]:
    """Zip INCLUDE into zip_path. Returns the archive names."""
    names: list[str] = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in INCLUDE:
            path = root / item
            if path.is_file():
                zf.write(path, item)
                names.append(item)
            elif path.is_dir():
                for current, dirs, files in os.walk(path):
                    dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
                    for name in sorted(files):
                        full = Path(current) / name
                        if full.suffix in SKIP_SUFFIXES:
                            continue
                        arc = full.relative_to(root).as_posix()
                        zf.write(full, arc)
                        names.append(arc)
            else:
                print(f"  (!) {item} not found; continuing without it")
    return names


def package_problems(names: list[str]) -> list[str]:
    """Why this package must not ship. Pure, so it is testable without a build."""
    problems = [f"missing {m}" for m in MUST_HAVE if m not in names]
    for name in names:
        base = name.rsplit("/", 1)[-1]
        if base in FORBIDDEN_NAMES:
            problems.append(f"contains a secrets file: {name}")
        if name.startswith(("tests/", ".git/")):
            problems.append(f"contains {name}")
    return problems


def dirty_refusal(argv: list[str], info: dict[str, Any]) -> str | None:
    """The reason to refuse building a package from a dirty tree, or None."""
    if not info.get("dirty") or "--allow-dirty" in argv:
        return None
    return ("  (!) refusing to package a DIRTY tree: the bytes would match no commit, so\n"
            "      /healthz's sha could not attribute them. Commit first, or pass\n"
            "      --allow-dirty; /healthz will then report dirty: true.")


def main(argv: list[str]) -> int:
    if "--deploy" in argv:
        print("  --deploy is not available yet: the target depends on decisions D2, D18 and D19.")
        print("  See docs/deployment.md.")
        return 2
    print("── packaging")
    info = build_info()
    if not info["sha"]:
        sys.exit("  (!) not a git checkout with a commit; cannot stamp build_info.json.")
    refusal = dirty_refusal(argv, info)
    if refusal:
        sys.exit(refusal)
    write_build_info(info)
    names = package()
    print(f"  {len(names)} files -> {ZIP_PATH.name} ({ZIP_PATH.stat().st_size / 1e6:.2f} MB)")
    print(f"  app modules : {sum(1 for n in names if n.startswith('app/') and n.endswith('.py'))}")
    problems = package_problems(names)
    if problems:
        sys.exit("  (!) package refused:\n      " + "\n      ".join(problems))
    print("  sanity      : ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
