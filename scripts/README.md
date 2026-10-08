# scripts/

Python, not bash: shell quoting of JSON and queries fails silently, Git Bash rewrites
leading-slash Azure resource ids, and the Windows console needs an encoding fix. Every script
here is **standard library plus the Azure CLI**, so none needs a virtual environment, except
`gate.py`, which deliberately runs under the interpreter whose packages it is checking. Shared
plumbing (HTTP, git, `az`, pass/fail reporting, targets) is in `_common.py`.

| Script | What it answers |
|---|---|
| `gate.py` | Does the working tree pass the same checks CI runs? |
| `ci_summary.py` | What did a CI run do and find? (the page on the run) |
| `package_deploy.py` | Build the deploy package, stamped with its commit. |
| `verify_deploy.py` | Is the target healthy, and running the commit we expect? |
| `whats_deployed.py` | What commit is actually running on a target? |
| `_common.py` | Shared plumbing. |

## Targets

Hosting is not decided (D2), so hosts come from the environment until it is:

| Target | Host from | Default |
|---|---|---|
| `staging` | `SLIDEFORGE_STAGING_URL` | none |
| `production` | `SLIDEFORGE_PRODUCTION_URL` | none |
| `local` | `SLIDEFORGE_LOCAL_URL` | `http://127.0.0.1:8000` |

## `gate.py`: the full local gate

```bash
.venv/Scripts/python.exe scripts/gate.py             # everything
.venv/Scripts/python.exe scripts/gate.py --offline   # skip pip-audit (needs the network)
```

It runs, in CI's order: `pip check`, the pin check, the import smoke test, the tests, ruff, mypy,
pip-audit, gitleaks and bandit.

**Run it with the interpreter you mean.** Every Python step uses `sys.executable`, so the
project's 3.11 `.venv` is what gets tested. The banner prints the versions first, and warns when
the interpreter is not 3.11.

Three things it encodes that are easy to get wrong by hand:

1. **Installed versions are checked against `requirements.txt`.** `pip check` proves the
   installed set is consistent with itself; it says nothing about whether it is what we pinned.
   In Auxi_Connector both dev interpreters had drifted past the redis bound in opposite
   directions, so a green gate was exercising a redis-py that would never be deployed.
2. **`pip-audit` is part of the gate.** A green test suite says nothing about the dependency graph.
3. **A check that did not run says SKIP, not PASS.** gitleaks runs when it is on `PATH`
   (install it from the gitleaks releases page); CI always runs it. `--offline` skips pip-audit
   the same way. The summary lists what did not run.

## `ci_summary.py`: the CI run page

```bash
python scripts/ci_summary.py backend >> "$GITHUB_STEP_SUMMARY"
```

The last step of the backend job, with `if: always()`, so a red run still reports. It reads the
step outcomes, the pytest JUnit report and the pip-audit JSON from `ci-reports/`. A missing
report is shown as missing, never as clean. Report text is escaped before it reaches the page.

## `package_deploy.py`: build the deploy package

```bash
python scripts/package_deploy.py                 # build staging-deploy.zip
python scripts/package_deploy.py --allow-dirty   # from an uncommitted tree, knowingly
```

Writes `build_info.json` (`sha`, `branch`, `dirty`, `builtAt`) and zips it with `app/`,
`requirements.txt` and `startup.txt`. The include list is explicit, because zipping the working
tree would carry `.git`, `.env`, caches and tests. The sanity check aborts when the package lacks
`build_info.json` or any must-have file, or contains a secrets file or tests. It refuses a dirty
tree by default: those bytes would match no commit. `--deploy` is not available until hosting is
decided (D2, D18, D19); CI's deploy jobs will deploy the artifact this builds.

## `verify_deploy.py`: the post-deploy report

```bash
python scripts/verify_deploy.py staging
python scripts/verify_deploy.py production --sha <commit>
```

Passes only when `/readyz` is 200 with Redis and config ok, `/healthz` reports the expected
commit (`--sha`, else `GITHUB_SHA`, else the tip of the target's branch) from a clean tree, and
`/healthz` is responsive. Exits non-zero on any failure. Platform checks (health-check path,
settings that travel with a slot swap) are added once D2 is settled.

## `whats_deployed.py`: what is actually running

```bash
python scripts/whats_deployed.py production --against production
python scripts/whats_deployed.py staging --against <tag or sha>
```

Reads the commit from `/healthz` and compares it with a git ref. Exits 0 only when the target
runs exactly that ref from a clean build. In Auxi_Connector a hotfix once reached production
from an unmerged branch and went unnoticed for six days; with the commit stamped into the build,
this is one request.
