"""The route-controls checklist: every route, who may call it, and what protects it.

THE CHECKLIST (`RULES`): one row per entry into the service, saying who may call
it, where its protection lives, which rate class limits abuse, how licensing is
checked, and which error format it uses (decision D32). `docs/route-controls.md`
is generated from it, so the documentation cannot drift from what is enforced.

THE ROBOTS (the tests) fail the build when:
  * a served route has no row, or more than one;
  * a row covers no route (a stale row is a false promise);
  * a `dependency` row's route does not declare the dependencies the row promises;
  * a `route` row's handler does not call the protections the row promises
    (transitively, within its module);
  * a `public` row's route takes credentials or declares any dependency;
  * a row's error format disagrees with the route (legacy routes must use
    `LegacyErrorRoute`, everything else must not);
  * a row's TODO-P5 markers are unknown, or an "ownership" marker has no
    `TODO-P5 ownership` comment in the handler's module (where the check will go);
  * an expensive row neither enforces nor marks (TODO-P5) its rate limit and
    in-flight cap;
  * the generated documentation is out of date.

TODO-P5 markers. Phase 5 adds Redis rate limits, in-flight caps, licensing and
ownership checks behind decisions. Until then a row says what it still lacks in
`todo_p5`, and the generated table shows it: a promise that is visibly open, never
one that silently looks kept.

To add a route: write its handler, add or extend a row in RULES, then run
    python tests/test_route_controls.py --write
to regenerate the docs table. Change a protection by changing the row and the code
together.
"""

from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute, Route

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:  # when run as a script for --write
    sys.path.insert(0, str(REPO))

from app.core.errors import LegacyErrorRoute  # noqa: E402

DOC = REPO / "docs" / "route-controls.md"
BEGIN = "<!-- BEGIN GENERATED: route controls (python tests/test_route_controls.py --write) -->"
END = "<!-- END GENERATED: route controls -->"

GUARDS = ("public", "dependency", "route")
# What a row may say Phase 5 still owes it (docs/route-controls.md, "TODO-P5").
TODO_P5 = ("rate-limit", "in-flight", "licence", "ownership", "over-the-limit-test")
RATE_CLASSES = ("none", "standard", "expensive")
ERROR_FORMATS = ("problem", "legacy")


@dataclass(frozen=True)
class Rule:
    name: str
    who: str
    guard: str              # "public" | "dependency" (FastAPI Depends) | "route" (the handler itself)
    rate_class: str         # "none" | "standard" (120/min) | "expensive" (10/min, 3 in flight)
    budget: str             # what limits abuse, in words
    licence: str            # licensing check, and fail mode (D10)
    errors: str             # "problem" (RFC 9457) | "legacy" ({"error"}; D32)
    routes: tuple[str, ...]  # "METHOD /path"
    must_depend: tuple[str, ...] = ()  # guard == "dependency": names of required dependencies
    must_call: tuple[str, ...] = ()    # guard == "route": names the handler must (transitively) call
    notes: str = ""
    todo_p5: tuple[str, ...] = ()      # what Phase 5 still owes this row (TODO_P5)


def _darwin(path: str) -> tuple[str, ...]:
    """Every method of a Darwin route: each is registered for all of them, because Darwin's
    handlers answer an unexpected method themselves (app/api/legacy.py, darwin_route)."""
    from app.api.legacy import ALL_METHODS

    return tuple(f"{method} /api/{path}" for method in sorted(ALL_METHODS))


_DARWIN_USER = "any signed-in user (bearer JWT, verified in-service: app/core/auth.py)"
_DARWIN_TODO = ("rate-limit", "in-flight", "licence", "over-the-limit-test")


RULES: tuple[Rule, ...] = (
    Rule(
        name="health probes",
        who="anyone (the platform's health check, verify scripts)",
        guard="public",
        rate_class="none",
        budget="none; not routed through the gateway, so not rate limited by it",
        licence="no",
        errors="problem",
        routes=("GET /healthz", "GET /readyz"),
        notes="No credentials by necessity; bodies reveal no hosts, variables or exceptions.",
    ),
    Rule(
        name="OpenAPI spec",
        who="anyone who can reach the service (behind the gateway once it exists)",
        guard="public",
        rate_class="none",
        budget="none; static document generated from the code",
        licence="no",
        errors="problem",
        routes=("GET /openapi.json", "HEAD /openapi.json"),
        notes="Interactive /docs and /redoc pages are disabled.",
    ),
    Rule(
        name="Darwin storyline + intake",
        who=_DARWIN_USER,
        guard="route",
        rate_class="expensive",
        budget="paid model calls: storyline (one structured call per job), intake (one call per turn); "
               "intake text <= 30000 chars, 12-reply wrap-up; per-user intake cap wired, OFF (D12)",
        licence="not checked yet (D10)",
        errors="legacy",
        routes=_darwin("storyline") + _darwin("intake"),
        must_call=("require_user",),
        todo_p5=_DARWIN_TODO,
        notes="Method not checked (any method acts as POST). Malformed JSON: 500 on storyline, 400 on intake.",
    ),
    Rule(
        name="Darwin storyline status",
        who=_DARWIN_USER + "; the job's owner (403 'Not your job'); an unknown id is 200 pending",
        guard="route",
        rate_class="standard",
        budget="a read of one job record",
        licence="no",
        errors="legacy",
        routes=_darwin("storyline-status"),
        must_call=("require_user", "read_owned_job"),
        todo_p5=("rate-limit", "over-the-limit-test"),
    ),
    Rule(
        name="Darwin PPTX submit",
        who=_DARWIN_USER + "; the deck's owner (403 'Not your deck')",
        guard="route",
        rate_class="expensive",
        budget="one design_and_export / stitch job per call (paid design turn + export)",
        licence="not checked yet (D10)",
        errors="legacy",
        routes=_darwin("pptx-submit") + _darwin("pptx-deck-submit"),
        must_call=("check_method", "require_user", "owned_deck"),
        todo_p5=_DARWIN_TODO,
        notes="405 is checked BEFORE auth (Darwin's order). Success is 200, not 202.",
    ),
    Rule(
        name="Darwin PPTX status + result",
        who=_DARWIN_USER + "; NO ownership check today (any job id), kept for the Connector",
        guard="route",
        rate_class="standard",
        budget="a read of one job record (and one .pptx)",
        licence="no",
        errors="legacy",
        routes=_darwin("pptx-status") + _darwin("pptx-result") + _darwin("pptx-deck-status")
        + _darwin("pptx-deck-result"),
        must_call=("require_user", "read_any_job"),
        todo_p5=("rate-limit", "ownership", "over-the-limit-test"),
        notes="Unknown or unfinished id: 502 (the Connector reads it as still running). Adding the ownership "
              "check is a D33 behaviour change; someone else's job must then answer the same 502.",
    ),
    Rule(
        name="Darwin image-to-slide",
        who=_DARWIN_USER + " (the Auxi Connector)",
        guard="route",
        rate_class="expensive",
        budget="multipart image <= 4 MiB; one design_and_export job (paid design turn) per call",
        licence="not checked yet (D10)",
        errors="legacy",
        routes=_darwin("image-to-slide"),
        must_call=("require_user", "check_method"),
        todo_p5=_DARWIN_TODO,
        notes="405 'POST only' AFTER auth. JSON bodies are a 400.",
    ),
)

# --- feature/darwin-brands: brand, admin, analytics and identity routes ------------------------------
_BRAND_ACL = ("; brand access is Darwin's getBrandAccess: owner, admins for org brands, read-only members of "
              "a mapped VERIFIED email domain; an unreachable brand is 404")
_ADMIN = "an admin only (profiles.is_admin, server-only): 401, then 403 'Admin access required'"

RULES += (
    Rule(
        name="Darwin brands",
        who=_DARWIN_USER + _BRAND_ACL + " (403 for a member's PATCH)",
        guard="route",
        rate_class="standard",
        budget="kit JSON <= 256K characters; 20 personal brands (the store's cap: 409, new); list/patch/delete",
        licence="no",
        errors="legacy",
        routes=_darwin("brands"),
        must_call=("require_user", "get_brand_access"),
        todo_p5=("rate-limit", "over-the-limit-test"),
        notes="Other methods 405 AFTER auth. DELETE never removes an org brand (admin-org-brands does).",
    ),
    Rule(
        name="Darwin brand assets",
        who=_DARWIN_USER + _BRAND_ACL + "; uploads need edit (403 for a member)",
        guard="route",
        rate_class="standard",
        budget="one PNG <= 4 MiB decoded per upload; reads of one asset",
        licence="no",
        errors="legacy",
        routes=_darwin("brand-asset"),
        must_call=("require_user", "require_brand"),
        todo_p5=("rate-limit", "over-the-limit-test"),
        notes="Every non-POST method acts as GET. style-default needs no brand.",
    ),
    Rule(
        name="Darwin brand template import + guidelines extraction",
        who=_DARWIN_USER + "; a named brand must be editable (404 otherwise, members included)",
        guard="route",
        rate_class="expensive",
        budget="brand-pptx: multipart .pptx <= 5 MiB, one extract + layout render job; brand-extract: PDF <= 4 MiB, "
               "one paid model call per job; per-user daily extraction cap wired, OFF (D12)",
        licence="not checked yet (D10)",
        errors="legacy",
        routes=_darwin("brand-pptx") + _darwin("brand-extract"),
        must_call=("require_user", "check_method", "editable_or_404", "submit_job"),
        todo_p5=_DARWIN_TODO,
        notes="405 'POST only' AFTER auth. The -background functions they triggered are internal jobs (D31).",
    ),
    Rule(
        name="Darwin brand preview",
        who=_DARWIN_USER + "; the brand must be editable (404 otherwise, members included)",
        guard="route",
        rate_class="expensive",
        budget="one gpt-image call per job unless the content-hash cache hits; global daily image cap (enforced)",
        licence="not checked yet (D10)",
        errors="legacy",
        routes=_darwin("brand-preview"),
        must_call=("require_user", "editable_or_404", "submit_job"),
        todo_p5=_DARWIN_TODO,
        notes="No method check; a bodyless request is 500 (uncaught req.json()).",
    ),
    Rule(
        name="Darwin brand job status",
        who=_DARWIN_USER + "; the job's owner (403 'Not your job'); an unknown id is 200 pending",
        guard="route",
        rate_class="standard",
        budget="a read of one job record (brand-preview-status: and one PNG)",
        licence="no",
        errors="legacy",
        routes=_darwin("brand-pptx-status") + _darwin("brand-extract-status") + _darwin("brand-preview-status"),
        must_call=("require_user", "read_owned_job"),
        todo_p5=("rate-limit", "over-the-limit-test"),
    ),
    Rule(
        name="Darwin brand archetypes + heading",
        who=_DARWIN_USER + _BRAND_ACL + "; needs edit (403 for a member)",
        guard="route",
        rate_class="standard",
        budget="copies three stored layout PNGs / rewrites one furniture JSON and the kit",
        licence="no",
        errors="legacy",
        routes=_darwin("brand-archetypes") + _darwin("brand-heading"),
        must_call=("check_method", "require_user", "get_brand_access"),
        todo_p5=("rate-limit", "over-the-limit-test"),
        notes="405 'POST only' BEFORE auth. Kept for the contract; deprecation candidates (D33).",
    ),
    Rule(
        name="Darwin admin",
        who=_ADMIN,
        guard="route",
        rate_class="standard",
        budget="admin-metrics: one overview + one activity read (<= 2000 events); admin-org-brands: one write",
        licence="no",
        errors="legacy",
        routes=_darwin("admin-metrics") + _darwin("admin-org-brands"),
        must_call=("require_admin",),
        todo_p5=("rate-limit", "over-the-limit-test"),
        notes="admin-org-brands: other methods 405 AFTER the admin check.",
    ),
    Rule(
        name="Darwin analytics events",
        who=_DARWIN_USER,
        guard="route",
        rate_class="standard",
        budget="<= 50 events per call (the rest dropped silently); writes swallowed",
        licence="no",
        errors="legacy",
        routes=_darwin("analytics-event"),
        must_call=("require_user", "record_batch"),
        todo_p5=("rate-limit", "over-the-limit-test"),
        notes="A malformed body is a silent 200 {ok:true}.",
    ),
    Rule(
        name="Darwin userinfo (OIDC shim)",
        who="anyone presenting a JWT-shaped bearer: it is DECODED, NOT VERIFIED (C8; Supabase Auth's "
            "custom:auxi provider calls it with a fresh IdP token)",
        guard="public",
        rate_class="standard",
        budget="decodes one header; no storage, no model",
        licence="no",
        errors="legacy",
        routes=_darwin("userinfo"),
        todo_p5=("rate-limit", "over-the-limit-test"),
        notes="Reveals only what the presented token says. Kept while the identity flow needs it (D25); "
              "errors are text/plain (Fetch default), as Darwin.",
    ),
)


# --------------------------------------------------------------------------- #
# What the app actually serves
# --------------------------------------------------------------------------- #

def served_routes(app: FastAPI, routers: Iterable[APIRouter]) -> dict[str, BaseRoute]:
    """"METHOD /path" -> route object, for every route the app serves.

    Routers come from the ROUTERS registry (their route objects carry the full
    path by convention, see tests/test_main.py); routes the app registers itself
    (the OpenAPI document) come from app.routes. Cross-checked against the
    OpenAPI paths so a route served some other way cannot hide.
    """
    table: dict[str, BaseRoute] = {}
    for router in routers:
        for route in router.routes:
            assert isinstance(route, APIRoute), f"unexpected route type {type(route).__name__} in a router"
            for method in sorted(route.methods or ()):
                table[f"{method} {route.path}"] = route
    for route in app.routes:
        if isinstance(route, (APIRoute, Route)):
            for method in sorted(route.methods or ()):
                table[f"{method} {route.path}"] = route
        elif type(route).__name__ != "_IncludedRouter":
            raise AssertionError(f"unrecognised route kind {type(route).__name__}: give it a row and a scan")
    served_paths = {key.split(" ", 1)[1] for key in table}
    missing = set(app.openapi()["paths"]) - served_paths
    assert not missing, f"served but not found by the scan: {sorted(missing)}"
    return table


def _app_routes() -> dict[str, BaseRoute]:
    from app.main import ROUTERS, create_app
    from tests.conftest import build_settings

    return served_routes(create_app(build_settings()), ROUTERS)


def _matching(key: str, rules: Iterable[Rule]) -> list[Rule]:
    return [rule for rule in rules if key in rule.routes]


# --------------------------------------------------------------------------- #
# Static analysis helpers
# --------------------------------------------------------------------------- #

def _dependency_names(route: APIRoute) -> set[str]:
    names: set[str] = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            names.add(getattr(dep.call, "__name__", type(dep.call).__name__))
        stack.extend(dep.dependencies)
    return names


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def closure_calls(fn: Callable[..., Any]) -> set[str]:
    """Names called by `fn`, followed through other functions of the same module."""
    module = inspect.getmodule(fn)
    tree = ast.parse(textwrap.dedent(inspect.getsource(module))) if module else None
    functions: dict[str, ast.AST] = {}
    if tree is not None:
        functions = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    start = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    seen: set[str] = set()
    called: set[str] = set()
    queue: list[ast.AST] = [start]
    while queue:
        node = queue.pop()
        for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
            name = _call_name(call)
            if not name:
                continue
            called.add(name)
            if name in functions and name not in seen:
                seen.add(name)
                queue.append(functions[name])
    return called


def _takes_credentials(route: APIRoute) -> bool:
    params = [*route.dependant.header_params, *route.dependant.cookie_params]
    return any(p.name.lower() in ("authorization", "cookie", "x-api-key") for p in params) or bool(
        route.dependant.dependencies
    )


def violations(table: dict[str, BaseRoute], rules: tuple[Rule, ...]) -> list[str]:
    """Every way the routes and rows disagree. Empty means the checklist holds."""
    problems: list[str] = []
    for rule in rules:
        if rule.guard not in GUARDS:
            problems.append(f"{rule.name}: unknown guard {rule.guard!r}")
        if rule.rate_class not in RATE_CLASSES:
            problems.append(f"{rule.name}: unknown rate class {rule.rate_class!r}")
        if rule.errors not in ERROR_FORMATS:
            problems.append(f"{rule.name}: unknown error format {rule.errors!r}")
        if not any(key in table for key in rule.routes):
            problems.append(f"{rule.name}: covers no served route")
        unknown_todo = set(rule.todo_p5) - set(TODO_P5)
        if unknown_todo:
            problems.append(f"{rule.name}: unknown TODO-P5 markers {sorted(unknown_todo)}")
        if rule.rate_class == "expensive" and not {"rate-limit", "in-flight"} <= set(rule.todo_p5):
            problems.append(f"{rule.name}: expensive, but its rate limit and in-flight cap are not marked TODO-P5 "
                            "(remove the markers only when Phase 5 enforces them, with must_depend/must_call)")
        for key in rule.routes:
            if key not in table:
                problems.append(f"{rule.name}: lists {key}, which is not served")
    for key, route in sorted(table.items()):
        matched = _matching(key, rules)
        if len(matched) != 1:
            problems.append(f"{key}: covered by {len(matched)} rows (needs exactly one)")
            continue
        rule = matched[0]
        is_legacy = isinstance(route, LegacyErrorRoute)
        if (rule.errors == "legacy") != is_legacy:
            problems.append(f"{key}: row says {rule.errors!r} errors but the route class says otherwise")
        if not isinstance(route, APIRoute):
            if rule.guard != "public":
                problems.append(f"{key}: a framework route can only be public")
            continue
        if rule.guard == "public" and _takes_credentials(route):
            problems.append(f"{key}: public row, but the route takes credentials or dependencies")
        if rule.guard == "dependency":
            missing = set(rule.must_depend) - _dependency_names(route)
            if not rule.must_depend or missing:
                problems.append(f"{key}: missing dependencies {sorted(missing) or 'none promised'}")
        if rule.guard == "route":
            missing = set(rule.must_call) - closure_calls(route.endpoint)
            if not rule.must_call or missing:
                problems.append(f"{key}: handler does not call {sorted(missing) or 'anything promised'}")
        if "ownership" in rule.todo_p5:
            if "TODO-P5 ownership" not in inspect.getsource(route.endpoint):
                problems.append(f"{key}: TODO-P5 ownership, but no 'TODO-P5 ownership' marker in its handler")
    return problems


# --------------------------------------------------------------------------- #
# The tests
# --------------------------------------------------------------------------- #

def test_the_scan_finds_the_routes_it_should():
    assert {"GET /healthz", "GET /readyz", "GET /openapi.json"} <= set(_app_routes())


def test_every_route_has_exactly_one_row_and_every_row_holds():
    problems = violations(_app_routes(), RULES)
    assert not problems, "route controls do not hold:\n  " + "\n  ".join(problems)


def test_the_robots_catch_what_they_should():
    """The checker itself, on a synthetic app, so it is proven before Phase 5 adds rows."""

    def require_user() -> str:
        return "user"

    def check_rate_limit() -> None:
        return None

    router = APIRouter(tags=["synthetic"])

    @router.get("/dep-ok", dependencies=[Depends(require_user)])
    async def dep_ok() -> None:
        return None

    @router.get("/dep-missing")
    async def dep_missing() -> None:
        return None

    @router.get("/route-ok")
    async def route_ok() -> None:
        _helper()

    def _helper() -> None:
        check_rate_limit()

    @router.get("/public-with-auth", dependencies=[Depends(require_user)])
    async def public_with_auth() -> None:
        return None

    legacy = APIRouter(route_class=LegacyErrorRoute, tags=["synthetic-legacy"])

    @legacy.get("/legacy")
    async def legacy_route() -> None:
        return None

    app = FastAPI(openapi_url=None)
    app.include_router(router)
    app.include_router(legacy)
    table = served_routes(app, [router, legacy])
    good = (
        Rule("a", "w", "dependency", "standard", "b", "l", "problem", ("GET /dep-ok",), must_depend=("require_user",)),
        Rule("b", "w", "dependency", "standard", "b", "l", "problem", ("GET /dep-missing",), must_depend=("require_user",)),
        Rule("c", "w", "route", "standard", "b", "l", "problem", ("GET /route-ok",), must_call=("check_rate_limit",)),
        Rule("d", "w", "public", "none", "b", "l", "problem", ("GET /public-with-auth",)),
        Rule("e", "w", "public", "none", "b", "l", "problem", ("GET /legacy",)),
        Rule("f", "w", "public", "none", "b", "l", "problem", ("GET /gone",)),
    )
    found = "\n".join(violations(table, good))
    todo = (
        Rule("g", "w", "public", "none", "b", "l", "problem", ("GET /dep-ok",), todo_p5=("someday",)),
        Rule("h", "w", "public", "expensive", "b", "l", "problem", ("GET /route-ok",)),
        Rule("i", "w", "public", "none", "b", "l", "legacy", ("GET /legacy",), todo_p5=("ownership",)),
    )
    found_todo = "\n".join(violations(table, todo))
    assert "g: unknown TODO-P5 markers ['someday']" in found_todo
    assert "h: expensive, but its rate limit and in-flight cap are not marked TODO-P5" in found_todo
    assert "GET /legacy: TODO-P5 ownership, but no 'TODO-P5 ownership' marker" in found_todo
    assert "GET /dep-ok" not in found
    assert "GET /route-ok" not in found
    assert "GET /dep-missing: missing dependencies ['require_user']" in found
    assert "GET /public-with-auth: public row, but the route takes credentials" in found
    assert "GET /legacy: row says 'problem' errors" in found
    assert "f: covers no served route" in found
    assert "covered by 0 rows" in "\n".join(violations(table, good[:1]))


# --------------------------------------------------------------------------- #
# The documentation is the checklist
# --------------------------------------------------------------------------- #

def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def render(table: dict[str, BaseRoute] | None = None) -> str:
    table = table if table is not None else _app_routes()
    lines = [
        "### The checklist",
        "",
        "| Entry | Who may call it | Enforced in | Rate class | Abuse budget | Licence | Errors | TODO-P5 | Notes |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for rule in RULES:
        paths = list(dict.fromkeys(r.split(" ", 1)[1] for r in rule.routes))
        if len(rule.routes) > 2 and all(sum(1 for r in rule.routes if r.endswith(" " + p)) > 2 for p in paths):
            entry = "<br>".join(f"`{p}` (every method)" for p in paths)
        else:
            entry = "<br>".join(f"`{r}`" for r in rule.routes)
        lines.append("| " + " | ".join(_cell(c) for c in (
            f"**{rule.name}**<br>{entry}", rule.who, rule.guard, rule.rate_class, rule.budget,
            rule.licence, rule.errors, ", ".join(rule.todo_p5) or "-", rule.notes)) + " |")
    lines += ["", "### Every served route, and the row that covers it", "", "| Route | Row |", "|---|---|"]
    for key in sorted(table, key=lambda k: (k.split(" ", 1)[1], k)):
        rows = _matching(key, RULES)
        lines.append(f"| `{key}` | {rows[0].name if len(rows) == 1 else '**NO SINGLE ROW**'} |")
    return "\n".join(lines)


def _doc_block() -> str:
    text = DOC.read_text(encoding="utf-8").replace("\r\n", "\n")
    return text[text.index(BEGIN) + len(BEGIN):text.index(END)].strip("\n")


def test_the_generated_documentation_matches_the_checklist():
    assert _doc_block() == render(), (
        "docs/route-controls.md is out of date: run python tests/test_route_controls.py --write"
    )


def write_doc() -> None:
    text = DOC.read_text(encoding="utf-8").replace("\r\n", "\n")
    head, rest = text.split(BEGIN)
    tail = rest.split(END)[1]
    DOC.write_text(head + BEGIN + "\n\n" + render() + "\n\n" + END + tail, encoding="utf-8", newline="\n")
    print(f"wrote {DOC}")


if __name__ == "__main__":
    if "--write" in sys.argv:
        write_doc()
    else:
        print(render())
