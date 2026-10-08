#!/usr/bin/env python3
"""What is actually running on a target, proved against git.

    python scripts/whats_deployed.py production
    python scripts/whats_deployed.py staging --against staging
    python scripts/whats_deployed.py production --against prod-2026-12-01-cutover

The deploy package is stamped with its commit (package_deploy.py writes
build_info.json, /healthz serves it), so this is one request rather than the
package forensics Auxi_Connector needed before it had a stamp. An unmerged hotfix
once sat on that repo's production for six days because another branch looked like
the source of truth; this answers "is production really `production`?" directly.

Exit codes: 0 when the target runs exactly the --against ref (or no ref was given
and the target reports a clean sha), 1 otherwise.
"""

from __future__ import annotations

import sys
from typing import Any

from _common import banner, git, http_json, target_host


def verdict(body: dict[str, Any], ref_sha: str | None) -> tuple[bool, str]:
    """(matches, explanation). Pure, for tests."""
    sha = str(body.get("sha", ""))
    if not sha:
        return False, "the target reports no sha: it was not deployed from a stamped package"
    if body.get("dirty"):
        return False, f"{sha[:12]} was built from a DIRTY tree: the bytes match no commit exactly"
    if ref_sha is None:
        return True, f"running {sha[:12]} (pass --against <ref> to compare)"
    if not ref_sha:
        return False, "the --against ref does not resolve in this checkout (git fetch?)"
    if sha == ref_sha:
        return True, f"running {sha[:12]}, which IS the ref"
    return False, f"running {sha[:12]}, but the ref is {ref_sha[:12]}"


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1].startswith("-"):
        print(__doc__)
        return 2
    target = argv[1]
    against = argv[argv.index("--against") + 1] if "--against" in argv else None
    host, _ref = target_host(target)
    banner(f"whats_deployed: {target} ({host})")

    status, body = http_json(f"{host}/healthz", retries=2)
    if status != 200:
        print(f"  /healthz answered {status}; cannot tell what is running.")
        return 1
    sha = str(body.get("sha", ""))
    print(f"  sha     : {sha or '<none>'}")
    print(f"  builtAt : {body.get('builtAt', '<none>')}")
    print(f"  dirty   : {body.get('dirty', '<not reported>')}")
    if sha:
        subject = git("log", "-1", "--format=%h %s (%an, %cs)", sha)
        print(f"  commit  : {subject or '<not in this checkout; git fetch?>'}")

    ref_sha = git("rev-parse", "--verify", "--quiet", f"{against}^{{commit}}") if against else None
    ok, message = verdict(body, ref_sha)
    print()
    print(f"  => {message}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
