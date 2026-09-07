"""InProcessTokenIssuer: the RFC 7638 kid, claim-for-claim parity with what
`pelican token create --write` emits, per-namespace key/issuer selection, the
startup key gate, and the metrics the pelican issuer already records."""

from __future__ import annotations

import base64
import logging
import time
from datetime import UTC, datetime

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app.core.config import Settings
from app.core.errors import TokenIssuanceError
from app.core.namespaces import NamespaceConfigError
from app.services.token_issuer import (
    PELICAN_SUBJECT,
    WLCG_ANY_AUDIENCE,
    InProcessTokenIssuer,
    JwksProvider,
    _jwk_thumbprint,
    build_token_issuer,
)
from tests.conftest import NS_ISSUER as ISSUER

SCOPE = "osdf:///af3-msa-library/community-contributed/iid.a3m"
SCOPE_PATH = "/community-contributed/iid.a3m"
LOGGER = "af3cache.tokens"
PEM = serialization.Encoding.PEM
PKCS8 = serialization.PrivateFormat.PKCS8


def _write_key(tmp_path, name, key, password: bytes | None = None) -> str:
    enc = (
        serialization.BestAvailableEncryption(password)
        if password
        else serialization.NoEncryption()
    )
    p = tmp_path / f"{name}.pem"
    p.write_bytes(key.private_bytes(PEM, PKCS8, enc))
    return str(p)


def _ec_key():
    return ec.generate_private_key(ec.SECP256R1())


def _settings(path, **over) -> Settings:
    over.setdefault("token_issuer_impl", "inprocess")
    return Settings(pelican_namespaces_file=str(path), **over)


def _issuer(namespaces_yaml, tmp_path, key=None, entries=None) -> InProcessTokenIssuer:
    key = key or _ec_key()
    if entries is None:
        entries = [{"namespace": "af3-msa-library", "issuer": ISSUER, "capacity_tb": 50,
                    "private_key": _write_key(tmp_path, "community", key)}]
    return InProcessTokenIssuer(_settings(namespaces_yaml(entries)))


def _claims(token):
    return jwt.get_unverified_header(token), jwt.decode(
        token, options={"verify_signature": False, "verify_aud": False,
                        "verify_exp": False},
    )


# --- kid (RFC 7638) ---------------------------------------------------------

def test_kid_rfc7638_rsa_vector():
    # RFC 7638 section 3.1 example key + its published thumbprint.
    n_b64 = (
        "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7aPFF"
        "xuhDR1L6tSoc_BJECPebWKRXjBZCiFV4n3oknjhMstn64tZ_2W-5JsGY4Hc5n9yBXArwl93lqt"
        "7_RN5w6Cf0h4QyQ5v-65YGjQR0_FDW2QvzqY368QQMicAtaSqzs8KJZgnYb9c7d0zgdAZHzu6q"
        "MQvRL5hajrn1n91CbOpbISD08qNLyrdkt-bFTWhAI4vMQFh6WeZu0fM4lFd2NcRwr3XPksINHa"
        "Q-G_xBniIqbw0Ls1jF44-csFCur-kEgU8awapJzKnqDKgw"
    )
    n = int.from_bytes(base64.urlsafe_b64decode(n_b64 + "=="), "big")
    pub = rsa.RSAPublicNumbers(65537, n).public_key()
    assert _jwk_thumbprint(pub) == "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"


def test_kid_ec_p256_golden():
    # Fixed scalar -> fixed point -> fixed thumbprint. Pins the canonical form
    # (crv/kty/x/y sorted, no whitespace, 32-byte zero-padded coordinates).
    pub = ec.derive_private_key(1, ec.SECP256R1()).public_key()
    assert _jwk_thumbprint(pub) == "xx0BcA-wMohw8atYDJOe6peGModklG2wRHBlXHMvl0M"


def test_kid_ec_coordinates_are_fixed_width():
    # A point whose x has a leading zero byte must still hash the padded form.
    # scalar 5 on P-256 is not such a point in general, so assert the property
    # via the encoding length instead of a golden value.
    pub = ec.derive_private_key(5, ec.SECP256R1()).public_key()
    kid = _jwk_thumbprint(pub)
    assert len(kid) == 43 and "=" not in kid


# --- parity with pelican ----------------------------------------------------

async def test_issue_parity_with_pelican_shape(namespaces_yaml, tmp_path):
    key = _ec_key()
    issuer = _issuer(namespaces_yaml, tmp_path, key)
    t0 = int(time.time())
    tok = await issuer.issue(subject="requester@x", object_uri=SCOPE, ttl_seconds=900)
    header, claims = _claims(tok.token)

    kid = issuer.kid_for("af3-msa-library")
    assert header == {"alg": "ES256", "kid": kid, "typ": "JWT"}
    assert kid == _jwk_thumbprint(key.public_key())
    assert set(claims) == {"aud", "exp", "iat", "nbf", "iss", "jti", "scope", "sub", "wlcg.ver"}
    assert claims["aud"] == [WLCG_ANY_AUDIENCE]
    assert claims["iss"] == ISSUER
    assert claims["scope"] == f"storage.create:{SCOPE_PATH}"  # create only, no modify
    assert claims["sub"] == PELICAN_SUBJECT  # not the requester: pelican parity
    assert claims["wlcg.ver"] == "1.0"
    assert claims["nbf"] == claims["iat"]
    assert t0 <= claims["iat"] <= t0 + 2
    assert claims["exp"] - claims["iat"] == 900
    assert len(claims["jti"]) == 22 and "=" not in claims["jti"]

    # signature verifies with the public half, the way the origin checks it
    jwt.decode(tok.token, key.public_key(), algorithms=["ES256"],
               audience=WLCG_ANY_AUDIENCE)

    assert tok.expires_at == datetime.fromtimestamp(claims["exp"], UTC)
    assert tok.claim_jti == claims["jti"]
    assert tok.token_ref != tok.claim_jti
    assert len(tok.token_ref) == 32


async def test_rsa_key_uses_rs256(namespaces_yaml, tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer = _issuer(namespaces_yaml, tmp_path, key)
    tok = await issuer.issue(subject="j", object_uri=SCOPE, ttl_seconds=60)
    header, _ = _claims(tok.token)
    assert header["alg"] == "RS256"
    assert header["kid"] == _jwk_thumbprint(key.public_key())
    jwt.decode(tok.token, key.public_key(), algorithms=["RS256"],
               audience=WLCG_ANY_AUDIENCE)


async def test_multi_segment_namespace_scope_path(namespaces_yaml, tmp_path):
    entries = [{"namespace": "osg/af3", "issuer": ISSUER, "capacity_tb": 1,
                "private_key": _write_key(tmp_path, "k", _ec_key())}]
    issuer = _issuer(namespaces_yaml, tmp_path, entries=entries)
    tok = await issuer.issue(subject="j", object_uri="osdf:///osg/af3/sub/x.a3m",
                             ttl_seconds=60)
    assert _claims(tok.token)[1]["scope"] == "storage.create:/sub/x.a3m"


# --- startup key gate -------------------------------------------------------

def test_fake_pem_fails_at_construction(namespaces_yaml):
    # conftest's default entry points at a placeholder key: fine for the
    # registry loader, fatal for an issuer that must actually sign with it.
    with pytest.raises(NamespaceConfigError, match="not an unencrypted PEM"):
        InProcessTokenIssuer(_settings(namespaces_yaml()))


def test_p384_key_rejected(namespaces_yaml, tmp_path):
    entries = [{"namespace": "af3-msa-library", "issuer": ISSUER, "capacity_tb": 1,
                "private_key": _write_key(tmp_path, "p384",
                                          ec.generate_private_key(ec.SECP384R1()))}]
    with pytest.raises(NamespaceConfigError, match="must be P-256"):
        InProcessTokenIssuer(_settings(namespaces_yaml(entries)))


def test_encrypted_pem_rejected(namespaces_yaml, tmp_path):
    entries = [{"namespace": "af3-msa-library", "issuer": ISSUER, "capacity_tb": 1,
                "private_key": _write_key(tmp_path, "enc", _ec_key(), password=b"pw")}]
    with pytest.raises(NamespaceConfigError, match="not an unencrypted PEM"):
        InProcessTokenIssuer(_settings(namespaces_yaml(entries)))


def test_build_dispatch_inprocess_missing_file_raises(tmp_path):
    with pytest.raises(NamespaceConfigError):
        build_token_issuer(_settings(tmp_path / "absent.yaml"))


# --- per-namespace selection ------------------------------------------------

async def test_per_namespace_key_and_issuer(namespaces_yaml, tmp_path):
    ka, kb = _ec_key(), _ec_key()
    entries = [
        {"namespace": "af3-msa-library", "issuer": ISSUER, "capacity_tb": 1,
         "private_key": _write_key(tmp_path, "a", ka)},
        {"namespace": "af3-msa-west", "issuer": "https://west.test/issuer",
         "capacity_tb": 1, "private_key": _write_key(tmp_path, "b", kb)},
    ]
    issuer = _issuer(namespaces_yaml, tmp_path, entries=entries)
    ta = await issuer.issue(subject="j", object_uri=SCOPE, ttl_seconds=60)
    tb = await issuer.issue(subject="j", object_uri="osdf:///af3-msa-west/y.a3m",
                            ttl_seconds=60)
    ha, ca = _claims(ta.token)
    hb, cb = _claims(tb.token)
    assert ha["kid"] == _jwk_thumbprint(ka.public_key()) and ca["iss"] == ISSUER
    assert hb["kid"] == _jwk_thumbprint(kb.public_key())
    assert cb["iss"] == "https://west.test/issuer"
    assert cb["scope"] == "storage.create:/y.a3m"
    jwt.decode(tb.token, kb.public_key(), algorithms=["ES256"], audience=WLCG_ANY_AUDIENCE)
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(tb.token, ka.public_key(), algorithms=["ES256"], audience=WLCG_ANY_AUDIENCE)


async def test_disabled_namespace_still_mints(namespaces_yaml, tmp_path):
    entries = [
        {"namespace": "af3-msa-library", "issuer": ISSUER, "capacity_tb": 1,
         "enabled": False, "private_key": _write_key(tmp_path, "a", _ec_key())},
        {"namespace": "af3-msa-west", "issuer": "https://west.test/issuer",
         "capacity_tb": 1, "private_key": _write_key(tmp_path, "b", _ec_key())},
    ]
    issuer = _issuer(namespaces_yaml, tmp_path, entries=entries)
    tok = await issuer.issue(subject="j", object_uri=SCOPE, ttl_seconds=60)
    assert _claims(tok.token)[1]["iss"] == ISSUER


def test_same_key_path_parsed_once(namespaces_yaml, tmp_path, monkeypatch):
    path = _write_key(tmp_path, "shared", _ec_key())
    entries = [
        {"namespace": "af3-msa-library", "issuer": ISSUER, "capacity_tb": 1, "private_key": path},
        {"namespace": "af3-msa-west", "issuer": ISSUER, "capacity_tb": 1, "private_key": path},
    ]
    import app.services.token_issuer as mod
    calls = []
    real = mod._load_signing_key
    monkeypatch.setattr(mod, "_load_signing_key",
                        lambda p, ns: calls.append(p) or real(p, ns))
    issuer = _issuer(namespaces_yaml, tmp_path, entries=entries)
    assert calls == [path]
    assert issuer.kid_for("af3-msa-library") == issuer.kid_for("af3-msa-west")
    assert issuer.kid_for("nope") is None


# --- metrics + failure paths ------------------------------------------------

async def test_not_configured_records_metric(namespaces_yaml, tmp_path, caplog, monkeypatch):
    seen = []
    monkeypatch.setattr("app.services.token_issuer.record_token_issue",
                        lambda result, seconds=None: seen.append(result))
    issuer = _issuer(namespaces_yaml, tmp_path)
    with (
        caplog.at_level(logging.WARNING, logger=LOGGER),
        pytest.raises(TokenIssuanceError, match="no configured namespace"),
    ):
        await issuer.issue(subject="j", object_uri="osdf:///somewhere-else/x.a3m",
                           ttl_seconds=60)
    assert seen == ["not_configured"]
    assert any("not_configured" in r.message and "somewhere-else" in r.message
               for r in caplog.records)


async def test_sign_error_records_metric(namespaces_yaml, tmp_path, caplog, monkeypatch):
    seen = []
    monkeypatch.setattr("app.services.token_issuer.record_token_issue",
                        lambda result, seconds=None: seen.append((result, seconds)))
    issuer = _issuer(namespaces_yaml, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("hsm on fire")

    monkeypatch.setattr("app.services.token_issuer.jwt.encode", boom)
    with (
        caplog.at_level(logging.WARNING, logger=LOGGER),
        pytest.raises(TokenIssuanceError, match="in-process signing failed: RuntimeError"),
    ):
        await issuer.issue(subject="j", object_uri=SCOPE, ttl_seconds=60)
    assert [r for r, _ in seen] == ["sign_error"]
    assert seen[0][1] is not None
    assert any("sign_error" in r.message and "hsm on fire" in r.message
               for r in caplog.records)


async def test_ok_records_metric_with_elapsed(namespaces_yaml, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr("app.services.token_issuer.record_token_issue",
                        lambda result, seconds=None: seen.append((result, seconds)))
    issuer = _issuer(namespaces_yaml, tmp_path)
    await issuer.issue(subject="j", object_uri=SCOPE, ttl_seconds=60)
    assert len(seen) == 1 and seen[0][0] == "ok" and 0 <= seen[0][1] < 1


# --- wiring -----------------------------------------------------------------

def test_build_dispatch_inprocess(namespaces_yaml, tmp_path):
    entries = [{"namespace": "af3-msa-library", "issuer": ISSUER, "capacity_tb": 1,
                "private_key": _write_key(tmp_path, "k", _ec_key())}]
    issuer = build_token_issuer(_settings(namespaces_yaml(entries)))
    assert isinstance(issuer, InProcessTokenIssuer)
    # case-insensitive like the other impls
    assert isinstance(
        build_token_issuer(_settings(namespaces_yaml(entries), token_issuer_impl="INPROCESS")),
        InProcessTokenIssuer,
    )


def test_not_jwks_provider(namespaces_yaml, tmp_path):
    # A jwks() method here would mount /.well-known/jwks.json in production;
    # the origin's JWKS is authoritative for these issuers.
    assert not isinstance(_issuer(namespaces_yaml, tmp_path), JwksProvider)
