"""Inbound WLCG-bearer auth on /v1/uploads/* (dual-auth window).

Integration tests drive the real dependency through POST /v1/uploads/request;
the two unit tests cover the one-sided past-exp grace, which can't be reached
through the default (grace=0) app settings.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app.core.config import get_settings
from app.core.scitokens import ScopeError, TokenError, verify_bearer

BODY = {
    "sequence": "mkt ayiakqr",
    "db_version": "uniref-2026-01",
    "declared_size_bytes": 500,
    "declared_sha256": "a" * 64,
}


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _request(client, headers):
    return await client.post("/v1/uploads/request", json=BODY, headers=headers)


# ---- dual-auth: both credential types work -------------------------------


async def test_valid_bearer_accepted(client, mint_token):
    r = await _request(client, _bearer(mint_token()))
    assert r.status_code == 200, r.text


async def test_api_key_still_works(client):
    # Migration window: the legacy API key must keep functioning.
    r = await _request(client, {"X-API-Key": "test-key"})
    assert r.status_code == 200, r.text


async def test_no_credential_rejected(client):
    r = await _request(client, {})
    assert r.status_code == 401


async def test_aud_as_list_accepted(client, mint_token):
    # Real AP tokens carry aud as a JSON array, possibly with extra entries.
    tok = mint_token(aud=["https://wlcg.cern.ch/jwt/v1/any",
                          "https://af3-library.osg-htc.org"])
    r = await _request(client, _bearer(tok))
    assert r.status_code == 200, r.text


async def test_broader_scope_accepted(client, mint_token):
    # write:/ covers the /community-contributed namespace.
    r = await _request(client, _bearer(mint_token(scope="write:/")))
    assert r.status_code == 200, r.text


# ---- rejection paths -> 401 ----------------------------------------------


async def test_untrusted_issuer_rejected(client, mint_token):
    r = await _request(client, _bearer(mint_token(iss="https://evil.example")))
    assert r.status_code == 401


async def test_wrong_audience_rejected(client, mint_token):
    r = await _request(client, _bearer(mint_token(aud="https://someone-else")))
    assert r.status_code == 401


async def test_expired_rejected(client, mint_token):
    # 100s past exp; default grace 0 + 60s skew -> still expired.
    r = await _request(client, _bearer(mint_token(exp_delta=-100)))
    assert r.status_code == 401


async def test_bad_signature_rejected(client, mint_token):
    forged = ec.generate_private_key(ec.SECP256R1())
    r = await _request(client, _bearer(mint_token(signing_key=forged)))
    assert r.status_code == 401


async def test_alg_none_rejected(client, mint_token):
    r = await _request(client, _bearer(mint_token(alg="none")))
    assert r.status_code == 401


async def test_missing_wlcg_ver_rejected(client, mint_token):
    r = await _request(client, _bearer(mint_token(wlcg_ver=None)))
    assert r.status_code == 401


async def test_missing_iat_rejected(client, mint_token):
    r = await _request(client, _bearer(mint_token(include_iat=False)))
    assert r.status_code == 401


# ---- rejection paths -> 403 (authenticated, under-scoped) ----------------


async def test_wrong_scope_action_rejected(client, mint_token):
    r = await _request(client, _bearer(mint_token(scope="read:/community-contributed")))
    assert r.status_code == 403


async def test_narrower_scope_rejected(client, mint_token):
    # A grant strictly BELOW the namespace does not authorize the namespace.
    r = await _request(
        client, _bearer(mint_token(scope="write:/community-contributed/foo"))
    )
    assert r.status_code == 403


# ---- one-sided past-exp grace (unit, needs non-default settings) ---------


def test_past_exp_grace_accepts_recently_expired(mint_token):
    settings = get_settings().model_copy(update={"jwt_allowed_past_exp_seconds": 300})
    claims = verify_bearer(mint_token(exp_delta=-100), settings)  # 100s past exp
    assert claims["sub"] == "danny.morales"


def test_past_exp_grace_still_bounded(mint_token):
    settings = get_settings().model_copy(update={"jwt_allowed_past_exp_seconds": 30})
    # 100s past exp, grace 30 + skew 60 = 90 < 100 -> still expired.
    with pytest.raises(TokenError):
        verify_bearer(mint_token(exp_delta=-100), settings)


def test_scope_error_is_403_class(mint_token):
    with pytest.raises(ScopeError):
        verify_bearer(mint_token(scope="write:/user/danny"), get_settings())
