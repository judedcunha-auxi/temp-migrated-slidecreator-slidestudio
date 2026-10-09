"""Probes every authenticated route shares: the two 401s of `requireUser` (`_common.json` auth)."""

from __future__ import annotations

from typing import Any

from tests.contract.harness import Probe, RouteCases
from tests.fakes.darwin import DarwinEnv

DEV_BYPASS_NA = ("The DEV_SECRET_KEY bypass does not exist in this service (plan §4.3; decision 2026-10-09): "
                 "development and tests use their own token issuer.")


def request(env: DarwinEnv, method: str, path: str, headers: dict[str, str] | None = None, **kwargs: Any) -> Any:
    return env.client.request(method, path, headers=headers or {}, **kwargs)


def missing_bearer_probes(method: str, path: str, **kwargs: Any) -> list[Probe]:
    """401 "Missing bearer token": no header, another scheme, the scheme without a token."""
    return [
        Probe("no Authorization header", lambda env: request(env, method, path, **kwargs)),
        Probe("Basic scheme", lambda env: request(env, method, path, {"Authorization": "Basic dXNlcjpwYXNz"}, **kwargs)),
        Probe("Bearer with no token", lambda env: request(env, method, path, {"Authorization": "Bearer"}, **kwargs)),
    ]


def invalid_session_probes(method: str, path: str, **kwargs: Any) -> list[Probe]:
    """401 "Invalid or expired session": every way a token can be wrong (Darwin: Supabase rejected it)."""
    def bad(**token: Any) -> Any:
        return lambda env: request(env, method, path, env.auth("alice", **token), **kwargs)

    return [
        Probe("expired token", bad(expired=True)),
        Probe("wrong audience", bad(audience="another-service")),
        Probe("wrong issuer", bad(issuer="https://evil.example/")),
        Probe("signed by a key not in the JWKS", bad(signed_by_stranger=True)),
        Probe("no subject", bad(drop=("sub",))),
        Probe("not a JWT", lambda env: request(env, method, path, {"Authorization": "Bearer not-a-jwt"}, **kwargs)),
        Probe("scheme in lower case, bad token",
              lambda env: request(env, method, path, {"Authorization": "bearer x.y.z"}, **kwargs)),
    ]


def add_auth(rc: RouteCases, method: str, path: str, **kwargs: Any) -> None:
    rc.add("E401 Missing bearer token", *missing_bearer_probes(method, path, **kwargs))
    rc.add("E401 Invalid or expired session", *invalid_session_probes(method, path, **kwargs))
    if "E500 DEV_SECRET_KEY set but DEV_USER_ID missing" in {e for e in _error_ids(rc.route)}:
        rc.na("E500 DEV_SECRET_KEY set but DEV_USER_ID missing", DEV_BYPASS_NA)


def _error_ids(route: str) -> list[str]:
    from tests.contract.harness import entries

    return [e.id for e in entries(route) if e.is_error]


def broken(env: DarwinEnv, area: str, operation: str) -> None:
    """Make one storage operation fail as an unreachable General service would (a 500 path)."""
    from app.core.storage.ports import Unavailable

    async def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise Unavailable("general-service.internal:443 unreachable")

    setattr(getattr(env.store, area), operation, fail)
