"""PelicanCliTokenIssuer: argv, stdout token, JWT exp/jti, failure modes."""

from __future__ import annotations

import time

import jwt
import pytest

from app.core.config import Settings
from app.core.errors import TokenIssuanceError
from app.services.token_issuer import (
    PelicanCliTokenIssuer,
    build_token_issuer,
)

SCOPE = "osdf:///af3-msa-library/community-contributed/iid.a3m"
ISSUER = "https://issuer.test/"
KEY_PATH = "/keys/community.pem"


def _fake_pelican(tmp_path, *, stdout="", rc=0, argv_log=None):
    script = tmp_path / "pelican"
    log = f'echo "$@" >> {argv_log}\n' if argv_log else ""
    script.write_text(f'#!/bin/sh\n{log}printf %s \'{stdout}\'\nexit {rc}\n')
    script.chmod(0o755)
    return str(script)


def _pelican_settings(**over) -> Settings:
    return Settings(
        token_issuer_impl="pelican",
        pelican_token_issuer=ISSUER,
        pelican_token_private_key=KEY_PATH,
        **over,
    )


def test_build_dispatch_pelican():
    assert isinstance(
        build_token_issuer(Settings(token_issuer_impl="pelican")),
        PelicanCliTokenIssuer,
    )


async def test_issue_runs_expected_command_and_returns_token(tmp_path):
    argv_log = tmp_path / "argv.txt"
    exp = int(time.time()) + 1200
    token = jwt.encode(
        {"exp": exp, "jti": "jti-xyz", "scope": "storage.create"},
        "x" * 32, algorithm="HS256",
    )
    binary = _fake_pelican(tmp_path, stdout=token, argv_log=argv_log)
    issuer = PelicanCliTokenIssuer(_pelican_settings(pelican_token_binary=binary))

    res = await issuer.issue(subject="job", object_uri=SCOPE, ttl_seconds=1200)

    assert res.token == token
    assert res.claim_jti == "jti-xyz"               # parsed from the JWT
    assert abs(res.expires_at.timestamp() - exp) < 2
    assert res.token_ref and res.token_ref != res.claim_jti  # app correlation id

    argv = argv_log.read_text().strip()
    assert argv == (
        f"token create {SCOPE} --write --issuer {ISSUER} "
        f"-k {KEY_PATH} --lifetime 1200"
    )


async def test_issue_non_jwt_token_falls_back_to_requested_ttl(tmp_path):
    binary = _fake_pelican(tmp_path, stdout="opaque-not-a-jwt")
    issuer = PelicanCliTokenIssuer(_pelican_settings(pelican_token_binary=binary))
    res = await issuer.issue(subject="j", object_uri=SCOPE, ttl_seconds=600)
    assert res.token == "opaque-not-a-jwt"
    assert res.claim_jti is None
    assert 595 <= (res.expires_at.timestamp() - time.time()) <= 605


async def test_issue_missing_issuer_or_key_raises():
    # not configured -> fail before ever shelling out
    with pytest.raises(TokenIssuanceError):
        await PelicanCliTokenIssuer(
            Settings(token_issuer_impl="pelican")
        ).issue(subject="j", object_uri=SCOPE, ttl_seconds=60)


async def test_issue_missing_binary_raises(tmp_path):
    issuer = PelicanCliTokenIssuer(
        _pelican_settings(pelican_token_binary="definitely-not-real-xyz")
    )
    with pytest.raises(TokenIssuanceError):
        await issuer.issue(subject="j", object_uri=SCOPE, ttl_seconds=60)


async def test_issue_nonzero_exit_raises(tmp_path):
    binary = _fake_pelican(tmp_path, stdout="", rc=1)
    issuer = PelicanCliTokenIssuer(_pelican_settings(pelican_token_binary=binary))
    with pytest.raises(TokenIssuanceError):
        await issuer.issue(subject="j", object_uri=SCOPE, ttl_seconds=60)


async def test_issue_empty_stdout_raises(tmp_path):
    binary = _fake_pelican(tmp_path, stdout="", rc=0)
    issuer = PelicanCliTokenIssuer(_pelican_settings(pelican_token_binary=binary))
    with pytest.raises(TokenIssuanceError):
        await issuer.issue(subject="j", object_uri=SCOPE, ttl_seconds=60)
