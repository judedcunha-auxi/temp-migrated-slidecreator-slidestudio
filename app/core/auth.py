"""SlideForge service: core/auth. Who is calling: a bearer JWT, verified against the issuer's JWKS.

The identity provider is not decided yet (decisions D7/D8; D9 says verify the IdP's JWT in the
service). So this verifies a plain OIDC-style access token by configuration alone:

* the signature, against the keys published at `AUTH_JWKS_URL` (cached `AUTH_JWKS_CACHE_S`; an
  unknown `kid` re-reads the set once, at most every `MIN_REFRESH_S`, so a key rotation is picked
  up without letting a stream of junk tokens hammer the issuer);
* `exp` (required), `nbf` and `iat` when present, with `AUTH_LEEWAY_S` of clock skew;
* `iss` == `AUTH_ISSUER` and `aud` contains `AUTH_AUDIENCE` (both required claims);
* `sub` (required): the identity subject the storage port keys the user by;
* the algorithm, from `AUTH_ALGORITHMS` (asymmetric only; `check_config` refuses HS* and none).

Swapping in the real issuer later is a configuration change. Tests mint tokens with a local key
(`tests/fakes/identity.py`). There is no `DEV_SECRET_KEY` bypass here (plan §4.3): Darwin's dev key
matched a static string and impersonated a fixed user; development gets its own issuer instead.

What a failure means to a caller (Darwin's `_shared/auth.ts`, the routes' contract):

* any token problem -> `InvalidToken` -> 401 "Invalid or expired session";
* the issuer cannot be reached, or auth is not configured -> `AuthUnavailable` -> 500
  "Internal error" (Darwin: Supabase unreachable or its env missing gave the same 500).

The HTTP side (the bearer header, the 401/403 bodies, the profile) is `app/api/deps.py`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx2 as httpx
import jwt

from app.config.settings import Settings
from app.core.storage.models import IdentityClaims

_log = logging.getLogger(__name__)

#: An unknown `kid` re-reads the JWKS at most this often (seconds).
MIN_REFRESH_S = 30.0
#: The JWKS document is small; anything bigger is not a key set.
MAX_JWKS_BYTES = 256 * 1024
JWKS_TIMEOUT_S = 5.0


class InvalidToken(Exception):
    """The token is malformed, expired, for another audience or issuer, or badly signed. The
    message is for logs only; the caller always gets Darwin's fixed 401 text."""


class AuthUnavailable(Exception):
    """The token could not be checked: no issuer is configured, or its keys could not be read."""


@dataclass(frozen=True)
class VerifiedToken:
    """A token that passed every check. `claims` is the whole verified payload."""

    subject: str
    claims: dict[str, Any] = field(default_factory=dict)

    def identity(self) -> IdentityClaims:
        """What the storage port's get-or-create needs. Email comes from `email`, else the first of
        B2C's `emails` (the shape Darwin's /api/userinfo reads); it counts as verified only when the
        token says `email_verified: true` (org-brand membership depends on it, 0011)."""
        email = self.claims.get("email")
        if not isinstance(email, str) or not email:
            emails = self.claims.get("emails")
            email = emails[0] if isinstance(emails, list) and emails and isinstance(emails[0], str) else None
        if email is not None and len(email) > 320:
            email = None
        return IdentityClaims(email=email, email_verified=self.claims.get("email_verified") is True)


class JwksSource(Protocol):
    """Where the signing keys come from: the issuer's JWKS document, as a dict."""

    async def fetch(self) -> dict[str, Any]: ...


class HttpJwksSource:
    """The issuer's published JWKS, over HTTPS."""

    def __init__(self, url: str, *, timeout_s: float = JWKS_TIMEOUT_S,
                 client_factory: Callable[[], httpx.AsyncClient] | None = None) -> None:
        self.url = url
        self.timeout_s = timeout_s
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=timeout_s))

    async def fetch(self) -> dict[str, Any]:
        try:
            async with self._client_factory() as client:
                response = await client.get(self.url, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise AuthUnavailable(f"the JWKS could not be fetched ({type(exc).__name__})") from exc
        if response.status_code != 200 or len(response.content) > MAX_JWKS_BYTES:
            raise AuthUnavailable(f"the JWKS answered {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise AuthUnavailable("the JWKS is not JSON") from exc
        if not isinstance(data, dict):
            raise AuthUnavailable("the JWKS is not an object")
        return data


class StaticJwksSource:
    """A fixed key set (tests, or a pinned set of keys)."""

    def __init__(self, jwks: dict[str, Any] | Callable[[], dict[str, Any]]) -> None:
        self._jwks = jwks
        self.fetches = 0

    async def fetch(self) -> dict[str, Any]:
        self.fetches += 1
        return self._jwks() if callable(self._jwks) else self._jwks


class TokenVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        source: JwksSource | None,
        algorithms: list[str] | None = None,
        cache_s: float = 600.0,
        leeway_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.issuer = issuer
        self.audience = audience
        self.algorithms = list(algorithms or ["RS256"])
        self.source = source
        self.cache_s = cache_s
        self.leeway_s = leeway_s
        self._clock = clock
        self._keys: dict[str | None, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, s: Settings) -> TokenVerifier:
        source = HttpJwksSource(s.auth_jwks_url) if s.auth_jwks_url else None
        return cls(issuer=s.auth_issuer, audience=s.auth_audience, source=source,
                   algorithms=s.auth_algorithm_list, cache_s=s.auth_jwks_cache_s, leeway_s=s.auth_leeway_s)

    @property
    def configured(self) -> bool:
        return bool(self.issuer and self.audience and self.source is not None)

    # ------------------------------------------------------------------ keys
    async def _refresh(self) -> None:
        if self.source is None:
            raise AuthUnavailable("no JWKS source is configured")
        raw = await self.source.fetch()
        keys: dict[str | None, jwt.PyJWK] = {}
        for entry in raw.get("keys") or []:
            if not isinstance(entry, dict) or entry.get("use", "sig") != "sig":
                continue
            try:
                key = jwt.PyJWK.from_dict(entry)
            except jwt.PyJWTError:
                continue  # an algorithm we do not support, or a broken entry: skip it, keep the rest
            keys[key.key_id] = key
        if not keys:
            raise AuthUnavailable("the JWKS holds no usable signing key")
        self._keys = keys
        self._fetched_at = self._clock()

    async def _key_for(self, kid: str | None) -> jwt.PyJWK:
        async with self._lock:
            now = self._clock()
            stale = self._fetched_at is None or now - self._fetched_at >= self.cache_s
            if stale:
                await self._refresh()
            elif kid not in self._keys and now - (self._fetched_at or 0.0) >= MIN_REFRESH_S:
                await self._refresh()  # a rotation: the issuer may have published a new key
            key = self._keys.get(kid)
            if key is None and kid is None and len(self._keys) == 1:
                key = next(iter(self._keys.values()))
            if key is None:
                raise InvalidToken("no signing key with this kid")
            return key

    # ---------------------------------------------------------------- verify
    async def verify(self, token: str) -> VerifiedToken:
        if not self.configured:
            raise AuthUnavailable("auth is not configured (AUTH_ISSUER, AUTH_AUDIENCE, AUTH_JWKS_URL)")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidToken(f"malformed token ({type(exc).__name__})") from exc
        alg = header.get("alg")
        if alg not in self.algorithms:
            raise InvalidToken("algorithm not accepted")
        kid = header.get("kid")
        key = await self._key_for(kid if isinstance(kid, str) else None)
        try:
            claims = jwt.decode(
                token,
                key=key.key,
                algorithms=self.algorithms,
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway_s,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken(f"rejected ({type(exc).__name__})") from exc
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 256:
            raise InvalidToken("bad subject")
        return VerifiedToken(subject=subject, claims=claims)


#: The verifier as the routes see it: anything with this one method (tests may pass a stub).
Verify = Callable[[str], Awaitable[VerifiedToken]]
