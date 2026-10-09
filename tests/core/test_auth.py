"""core/auth: the JWKS-verified bearer token, against a local issuer (tests/fakes/identity.py)."""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
import jwt
import pytest

from app.core.auth import AuthUnavailable, HttpJwksSource, InvalidToken, StaticJwksSource, TokenVerifier
from tests.conftest import build_settings
from tests.fakes.identity import AUDIENCE, ISSUER, FakeIssuer


@pytest.fixture
def issuer() -> FakeIssuer:
    return FakeIssuer()


@pytest.mark.asyncio
async def test_a_good_token_gives_its_subject_and_claims(issuer: FakeIssuer):
    verified = await issuer.verifier().verify(issuer.token("user-1", email="Jane@Example.com"))
    assert verified.subject == "user-1"
    assert verified.claims["iss"] == ISSUER and verified.claims["aud"] == AUDIENCE
    identity = verified.identity()
    assert (identity.email, identity.email_verified) == ("Jane@Example.com", True)


@pytest.mark.parametrize("bad", [
    {"expired": True},
    {"audience": "someone-else"},
    {"issuer": "https://evil.example/"},
    {"signed_by_stranger": True},
    {"drop": ("sub",)},
    {"drop": ("exp",)},
    {"drop": ("aud",)},
    {"extra": {"sub": ""}},
])
@pytest.mark.asyncio
async def test_every_bad_token_is_invalid(issuer: FakeIssuer, bad: dict[str, Any]):
    with pytest.raises(InvalidToken):
        await issuer.verifier().verify(issuer.token("user-1", **bad))


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c", "x" * 50])
@pytest.mark.asyncio
async def test_garbage_is_invalid(issuer: FakeIssuer, token: str):
    with pytest.raises(InvalidToken):
        await issuer.verifier().verify(token)


@pytest.mark.asyncio
async def test_hs256_and_none_are_refused_even_with_the_right_claims(issuer: FakeIssuer):
    claims = {"sub": "u", "iss": ISSUER, "aud": AUDIENCE, "exp": 4_000_000_000}
    hs = jwt.encode(claims, "a-shared-secret-of-sufficient-length-32b", algorithm="HS256", headers={"kid": issuer.kid})
    none = jwt.encode(claims, None, algorithm="none")  # type: ignore[arg-type]
    for token in (hs, none):
        with pytest.raises(InvalidToken):
            await issuer.verifier().verify(token)


@pytest.mark.asyncio
async def test_a_token_within_the_leeway_is_accepted(issuer: FakeIssuer):
    token = issuer.token("u", lifetime_s=-10)  # expired 10 s ago; leeway 30 s
    assert (await issuer.verifier().verify(token)).subject == "u"
    with pytest.raises(InvalidToken):
        await issuer.verifier(leeway_s=0).verify(token)


@pytest.mark.asyncio
async def test_keys_are_cached_and_an_unknown_kid_refetches_at_most_every_30s(issuer: FakeIssuer):
    clock = [1000.0]
    source = StaticJwksSource(issuer.jwks)
    verifier = issuer.verifier(source=source, clock=lambda: clock[0])
    for _ in range(3):
        await verifier.verify(issuer.token("u"))
    assert source.fetches == 1
    stranger = jwt.encode({"sub": "u", "iss": ISSUER, "aud": AUDIENCE, "exp": 4_000_000_000}, issuer.key,
                          algorithm="RS256", headers={"kid": "rotated-key"})
    with pytest.raises(InvalidToken):
        await verifier.verify(stranger)
    assert source.fetches == 1  # within 30 s of the last read: no refetch storm
    clock[0] += 31
    with pytest.raises(InvalidToken):
        await verifier.verify(stranger)
    assert source.fetches == 2  # one re-read, for a possible rotation
    clock[0] += 600
    await verifier.verify(issuer.token("u"))
    assert source.fetches == 3  # the cache expired


@pytest.mark.asyncio
async def test_a_rotated_key_is_picked_up(issuer: FakeIssuer):
    clock = [0.0]
    keys = {"current": issuer.jwks()}
    verifier = issuer.verifier(source=StaticJwksSource(lambda: keys["current"]), clock=lambda: clock[0])
    await verifier.verify(issuer.token("u"))
    rotated = FakeIssuer(kid="key-2")
    keys["current"] = rotated.jwks()
    clock[0] += 31
    assert (await verifier.verify(rotated.token("u"))).subject == "u"


@pytest.mark.asyncio
async def test_unconfigured_or_unreachable_is_unavailable_not_invalid(issuer: FakeIssuer):
    with pytest.raises(AuthUnavailable):
        await TokenVerifier(issuer="", audience="", source=None).verify(issuer.token("u"))

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    source = HttpJwksSource("https://issuer.test/jwks",
                            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(down)))
    with pytest.raises(AuthUnavailable):
        await issuer.verifier(source=source).verify(issuer.token("u"))


@pytest.mark.parametrize("answer", [httpx.Response(500), httpx.Response(200, content=b"not json"),
                                    httpx.Response(200, json=[1]), httpx.Response(200, json={"keys": []})])
@pytest.mark.asyncio
async def test_a_bad_jwks_is_unavailable(issuer: FakeIssuer, answer: httpx.Response):
    source = HttpJwksSource("https://issuer.test/jwks", client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: answer)))
    with pytest.raises(AuthUnavailable):
        await issuer.verifier(source=source).verify(issuer.token("u"))


@pytest.mark.asyncio
async def test_the_http_source_reads_the_published_set(issuer: FakeIssuer):
    seen: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=issuer.jwks())

    source = HttpJwksSource("https://issuer.test/.well-known/jwks.json", client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(serve)))
    assert (await issuer.verifier(source=source).verify(issuer.token("u"))).subject == "u"
    assert seen == ["https://issuer.test/.well-known/jwks.json"]


def test_from_settings():
    s = build_settings(auth_issuer=ISSUER, auth_audience=AUDIENCE, auth_jwks_url="https://issuer.test/jwks",
                       auth_algorithms="RS256,ES256", auth_leeway_s=5)
    verifier = TokenVerifier.from_settings(s)
    assert verifier.configured and verifier.algorithms == ["RS256", "ES256"] and verifier.leeway_s == 5
    assert not TokenVerifier.from_settings(build_settings()).configured


def test_identity_reads_b2c_emails_and_only_trusts_a_true_email_verified():
    from app.core.auth import VerifiedToken

    assert VerifiedToken("s", {"emails": ["b2c@example.com"]}).identity().email == "b2c@example.com"
    assert VerifiedToken("s", {"email": "a@example.com", "email_verified": "true"}).identity().email_verified is False
    assert VerifiedToken("s", {}).identity().email is None
