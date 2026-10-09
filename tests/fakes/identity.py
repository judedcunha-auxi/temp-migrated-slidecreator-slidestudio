"""A fake token issuer: the test-only identity provider (plan §4.3: no DEV_SECRET_KEY bypass).

`FakeIssuer` holds a local RSA key, publishes it as a JWKS, and mints tokens the service's
`TokenVerifier` accepts, so every authenticated test is a real signature check, not a stub:

    issuer = FakeIssuer()
    app.state.token_verifier = issuer.verifier()
    headers = issuer.headers("user-1", email="a@example.com")

and the bad tokens the negative tests need:

    issuer.token("u", expired=True)              # exp in the past (beyond the leeway)
    issuer.token("u", audience="someone-else")   # wrong aud
    issuer.token("u", issuer="https://evil/")    # wrong iss
    issuer.token("u", signed_by_stranger=True)   # signed with a key not in the JWKS (same kid)
"""

from __future__ import annotations

import time
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.auth import StaticJwksSource, TokenVerifier

ISSUER = "https://issuer.test/"
AUDIENCE = "slideforge-test"
KID = "test-key-1"


def _new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


# Key generation is slow-ish; one pair per test session is plenty.
_KEY = _new_key()
_STRANGER = _new_key()


class FakeIssuer:
    def __init__(self, *, issuer: str = ISSUER, audience: str = AUDIENCE, kid: str = KID) -> None:
        self.issuer = issuer
        self.audience = audience
        self.kid = kid
        self.key = _KEY
        self.source = StaticJwksSource(self.jwks)

    def jwks(self) -> dict[str, Any]:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        jwk.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}

    def verifier(self, **overrides: Any) -> TokenVerifier:
        values: dict[str, Any] = {"issuer": self.issuer, "audience": self.audience, "source": self.source,
                                  "algorithms": ["RS256"], "cache_s": 600.0, "leeway_s": 30.0}
        values.update(overrides)
        return TokenVerifier(**values)

    def token(
        self,
        subject: str,
        *,
        email: str | None = None,
        email_verified: bool = True,
        expired: bool = False,
        audience: str | None = None,
        issuer: str | None = None,
        signed_by_stranger: bool = False,
        lifetime_s: int = 3600,
        extra: dict[str, Any] | None = None,
        drop: tuple[str, ...] = (),
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": subject,
            "iss": issuer or self.issuer,
            "aud": audience or self.audience,
            "iat": now - (7200 if expired else 0),
            "exp": now - 3600 if expired else now + lifetime_s,
        }
        if email is not None:
            claims["email"] = email
            claims["email_verified"] = email_verified
        claims.update(extra or {})
        for name in drop:
            claims.pop(name, None)
        key = _STRANGER if signed_by_stranger else self.key
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": self.kid})

    def headers(self, subject: str, **kwargs: Any) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(subject, **kwargs)}"}
