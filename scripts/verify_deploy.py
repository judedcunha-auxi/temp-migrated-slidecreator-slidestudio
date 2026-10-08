#!/usr/bin/env python3
"""The post-deploy report: is the target healthy, and is it running the commit we think?

    python scripts/verify_deploy.py staging
    python scripts/verify_deploy.py production
    python scripts/verify_deploy.py staging --sha <commit>   # CI passes the commit it deployed
    python scripts/verify_deploy.py local                     # a server on 127.0.0.1:8000

Checks, each PASS or FAIL, and a non-zero exit on any failure so it can gate a deploy:

1. /readyz answers 200 with redis ok and config ok;
2. /healthz answers 200 and reports a sha, which matches the expected commit (--sha,
   else GITHUB_SHA, else the tip of the target's branch), and is not dirty;
3. responsiveness: three /healthz samples, averaging under MAX_AVG_SECONDS.

Platform checks (health-check path, startup command and settings that travel with a
slot swap, as in Auxi_Connector) are added when hosting is decided (D2). Hosts come
from SLIDEFORGE_STAGING_URL / SLIDEFORGE_PRODUCTION_URL until then (_common.py).
"""

from __future__ import annotations

import os
import sys
from typing import Any

from _common import Report, banner, git, http_json, section, target_host, timed

MAX_AVG_SECONDS = 2.0


def expected_sha(argv: list[str], ref: str) -> str:
    if "--sha" in argv:
        return argv[argv.index("--sha") + 1]
    return os.environ.get("GITHUB_SHA", "") or git("rev-parse", ref)


def readiness_findings(status: int, body: dict[str, Any]) -> list[tuple[bool, str]]:
    """(ok, message) for a /readyz answer. Pure, for tests."""
    if status != 200:
        return [(False, f"/readyz {status} {body}")]
    return [
        (True, "/readyz 200"),
        (body.get("redis") == "ok", f"redis {body.get('redis')!r}"),
        (body.get("config") == "ok", f"config {body.get('config')!r}"),
    ]


def identity_findings(status: int, body: dict[str, Any], want: str) -> list[tuple[bool, str]]:
    """(ok, message) for a /healthz answer against the expected commit. Pure, for tests."""
    if status != 200:
        return [(False, f"/healthz {status}")]
    sha = str(body.get("sha", ""))
    found = [(True, "/healthz 200")]
    if not sha:
        return [*found, (False, "/healthz reports no sha: the package was not stamped (package_deploy.py)")]
    if want:
        found.append((sha == want, f"running {sha[:12]}, expected {want[:12]}"))
    else:
        found.append((False, f"running {sha[:12]}, but there is no expected commit to compare with"))
    found.append((body.get("dirty") is False, f"dirty={body.get('dirty')!r} (built from a committed tree)"))
    return found


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1].startswith("-"):
        print(__doc__)
        return 2
    target = argv[1]
    host, ref = target_host(target)
    want = expected_sha(argv, ref)
    banner(f"verify_deploy: target={target}  host={host}")
    r = Report()

    section("1. Readiness")
    status, body = http_json(f"{host}/readyz", retries=8)
    for ok, message in readiness_findings(status, body):
        r.ok(message) if ok else r.bad(message)

    section("2. Commit identity")
    status, body = http_json(f"{host}/healthz", retries=2)
    if body.get("builtAt"):
        r.note(f"built at {body['builtAt']}")
    for ok, message in identity_findings(status, body, want):
        r.ok(message) if ok else r.bad(message)

    section("3. Responsiveness (3 samples)")
    samples = [timed(f"{host}/healthz") for _ in range(3)]
    if any(s < 0 for s in samples):
        r.bad("at least one probe got no response")
    else:
        average = sum(samples) / len(samples)
        message = f"average /healthz {average:.3f}s (limit {MAX_AVG_SECONDS}s)"
        r.ok(message) if average < MAX_AVG_SECONDS else r.bad(message)

    section("4. Platform configuration")
    r.note("not checked yet: depends on the hosting decision (D2). See docs/deployment.md.")
    return r.finish("RESULT")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
