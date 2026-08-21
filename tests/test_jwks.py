"""JWKS route: conditional mount + token<->JWKS verification roundtrip."""

from __future__ import annotations

import json

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

from app.api import routes_jwks
from app.core.config import Settings
from app.services.deps import get_token_issuer
from app.services.token_issuer import LocalSigningTokenIssuer


def _local_issuer() -> LocalSigningTokenIssuer:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return LocalSigningTokenIssuer(
        Settings(
            token_issuer_impl="local",
            token_signing_key=pem,
            token_audience="https://origin.test/",
            token_kid="kid-roundtrip",
        )
    )


async def test_jwks_not_mounted_with_stub_issuer(client):
    # conftest sets TOKEN_ISSUER_IMPL=stub -> endpoint must not exist.
    r = await client.get("/.well-known/jwks.json")
    assert r.status_code == 404


async def test_jwks_served_and_token_verifies():
    issuer = _local_issuer()
    app = FastAPI()
    app.include_router(routes_jwks.router)
    app.dependency_overrides[get_token_issuer] = lambda: issuer

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/.well-known/jwks.json")
    assert r.status_code == 200
    jwks = r.json()
    jwk = jwks["keys"][0]
    assert jwk["kid"] == "kid-roundtrip" and jwk["use"] == "sig"

    tok = await issuer.issue(subject="job", object_uri="/ns/a.a3m", ttl_seconds=120)
    pub = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))  # type: ignore[attr-defined]
    claims = jwt.decode(
        tok.token, pub, algorithms=["RS256"], audience="https://origin.test/"
    )
    assert claims["sub"] == "job"
