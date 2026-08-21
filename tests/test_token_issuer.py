"""TokenIssuer unit tests: claims, TTL math, kid, ref/jti, dispatch."""

from __future__ import annotations

from datetime import UTC, datetime

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import Settings
from app.services.token_issuer import (
    JwksProvider,
    LocalSigningTokenIssuer,
    StubTokenIssuer,
    build_token_issuer,
)


def _rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _local_settings(**over) -> Settings:
    return Settings(
        token_issuer_impl="local",
        token_signing_key=_rsa_pem(),
        token_issuer_url="https://af3-cache.test/",
        token_audience="https://origin.test/",
        token_kid="kid-test",
        **over,
    )


async def test_local_issuer_claims_and_ttl():
    issuer = LocalSigningTokenIssuer(_local_settings())
    tok = await issuer.issue(
        subject="job-123", object_uri="/chtc/msa-cache/abc/src.a3m", ttl_seconds=900
    )
    pub = jwt.algorithms.RSAAlgorithm.from_jwk(  # type: ignore[attr-defined]
        __import__("json").dumps(issuer.jwks()["keys"][0])
    )
    claims = jwt.decode(
        tok.token, pub, algorithms=["RS256"], audience="https://origin.test/"
    )
    assert claims["iss"] == "https://af3-cache.test/"
    assert claims["sub"] == "job-123"
    assert claims["scope"] == (
        "storage.create:/chtc/msa-cache/abc/src.a3m "
        "storage.modify:/chtc/msa-cache/abc/src.a3m"
    )
    assert claims["wlcg.ver"] == "1.0"
    assert claims["exp"] - claims["iat"] == 900
    assert claims["nbf"] == claims["iat"]
    # header carries the kid that JWKS advertises
    assert jwt.get_unverified_header(tok.token)["kid"] == "kid-test"
    # token_ref is an independent correlation id; claim_jti is the token's jti
    assert tok.claim_jti == claims["jti"]
    assert tok.token_ref != tok.claim_jti
    assert tok.expires_at > datetime.now(UTC)


async def test_stub_issuer_has_no_jwks():
    issuer = StubTokenIssuer()
    tok = await issuer.issue(subject="s", object_uri="/x", ttl_seconds=60)
    assert tok.token.startswith("stub.")
    assert tok.token_ref and tok.claim_jti is None
    assert not isinstance(issuer, JwksProvider)


def test_local_issuer_is_jwks_provider():
    assert isinstance(LocalSigningTokenIssuer(_local_settings()), JwksProvider)


def test_build_dispatch():
    assert isinstance(build_token_issuer(Settings(token_issuer_impl="stub")), StubTokenIssuer)
    assert isinstance(build_token_issuer(_local_settings()), LocalSigningTokenIssuer)
    with pytest.raises(NotImplementedError):
        build_token_issuer(Settings(token_issuer_impl="oauth2"))
    with pytest.raises(ValueError):
        build_token_issuer(Settings(token_issuer_impl="bogus"))


async def test_local_issuer_ephemeral_key_when_unconfigured():
    # No key configured -> ephemeral RSA, still fully functional offline.
    issuer = LocalSigningTokenIssuer(Settings(token_issuer_impl="local"))
    tok = await issuer.issue(subject="s", object_uri="/p", ttl_seconds=30)
    assert tok.token and issuer.jwks()["keys"]
