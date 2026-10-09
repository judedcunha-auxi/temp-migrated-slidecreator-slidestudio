"""SlideForge service: api/deps. Who is calling, and the app's shared services, for route handlers.

Darwin's routes authenticate INSIDE the handler, not with a FastAPI dependency, because the order
of the method check, the auth check and the body parse is part of their contract (some answer 405
before 401, some after; app/api/legacy.py). So these are plain async functions taking the request:

    user = await require_user(request)     # 401 "Missing bearer token" / "Invalid or expired session"
    admin = await require_admin(request)   # ... and 403 "Admin access required"

Both also work as `Depends(require_user)` for a new route that has no ordering contract.

What they do (Darwin `_shared/auth.ts` + `_shared/http.ts: bearerToken`):

1. The Authorization header must match `^Bearer\\s+(.+)$` (scheme case-insensitive); else 401
   "Missing bearer token".
2. The token is verified (app/core/auth.py). Any problem with it: 401 "Invalid or expired session".
   The issuer unreachable or auth not configured: 500 "Internal error" (as Darwin, when Supabase
   was unreachable).
3. The subject is mapped to a user through the storage port: `users.get_or_create` (email and
   email_verified refreshed from the token every call, as 0011 reads auth.users).
4. `require_admin`: `profile.is_admin` (server-only; the port never lets a user set it), else 403.

The bodies come from `_common.json` (`auth.requireUser`, `auth.requireAdmin`) and are pinned by
the contract harness (tests/contract/).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Request

from app.core.auth import AuthUnavailable, InvalidToken, TokenVerifier, VerifiedToken
from app.core.errors import ApiError
from app.core.request_id import get_request_id
from app.core.storage.models import CallerContext, Profile
from app.core.storage.ports import Storage

if TYPE_CHECKING:
    from app.core.darwin.runtime import DarwinRuntime

_log = logging.getLogger(__name__)

MISSING_BEARER = "Missing bearer token"
INVALID_SESSION = "Invalid or expired session"
ADMIN_REQUIRED = "Admin access required"

# `^Bearer\s+(.+)$` with the `i` flag, as `_shared/http.ts`. `.` stops at a newline in both.
_BEARER = re.compile(r"^Bearer\s+(.+)$", re.IGNORECASE)


class ServiceNotReady(RuntimeError):
    """A shared service (storage, Redis) is missing on app.state. Unhandled on purpose: the route
    answers its 500 ("Internal error" on a legacy route), and the cause is logged."""


@dataclass(frozen=True)
class AuthedUser:
    """The caller of a request: Darwin's `{userId, email}` plus what the ports need.

    `ctx` is the CallerContext every storage call on the user's behalf takes (subject + request id).
    """

    user_id: str
    subject: str
    email: str | None
    is_admin: bool
    ctx: CallerContext
    profile: Profile
    token: VerifiedToken


def bearer_token(request: Request) -> str:
    match = _BEARER.match(request.headers.get("authorization") or "")
    if not match:
        raise ApiError(401, MISSING_BEARER)
    return match.group(1)


def get_storage(request: Request) -> Storage:
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        raise ServiceNotReady("no storage backend (see the startup log)")
    return storage  # type: ignore[no-any-return]


def get_verifier(request: Request) -> TokenVerifier:
    verifier = getattr(request.app.state, "token_verifier", None)
    if verifier is None:
        verifier = TokenVerifier.from_settings(request.app.state.settings)
        request.app.state.token_verifier = verifier
    return verifier  # type: ignore[no-any-return]


async def require_user(request: Request) -> AuthedUser:
    """The signed-in caller, or the route's 401 (see the module docstring). Cached per request, so
    calling it twice costs one verification."""
    cached = request.scope.get("sf.user")
    if isinstance(cached, AuthedUser):
        return cached
    token = bearer_token(request)
    try:
        verified = await get_verifier(request).verify(token)
    except InvalidToken as exc:
        _log.info("auth: token rejected: %s", exc)
        raise ApiError(401, INVALID_SESSION) from exc
    except AuthUnavailable as exc:
        _log.error("auth: cannot verify tokens: %s", exc)
        raise
    ctx = CallerContext(subject=verified.subject, request_id=get_request_id(request))
    profile = await get_storage(request).users.get_or_create(ctx, verified.identity())
    user = AuthedUser(user_id=profile.id, subject=verified.subject, email=profile.email,
                      is_admin=profile.is_admin, ctx=ctx, profile=profile, token=verified)
    request.scope["sf.user"] = user
    return user


async def require_admin(request: Request) -> AuthedUser:
    user = await require_user(request)
    if not user.is_admin:
        raise ApiError(403, ADMIN_REQUIRED)
    return user


def get_runtime(request: Request) -> DarwinRuntime:
    """The Darwin runtime (queue, models, image generator): built at startup, or on first use when a
    test or the lifespan did not provide one."""
    runtime = getattr(request.app.state, "darwin", None)
    if runtime is None:
        redis = getattr(request.app.state, "redis", None)
        if redis is None:
            raise ServiceNotReady("no Redis client")
        from app.core.darwin.runtime import build_runtime

        runtime = build_runtime(request.app.state.settings, redis, get_storage(request))
        request.app.state.darwin = runtime
    return runtime  # type: ignore[no-any-return]
