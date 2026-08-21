"""Upload-request: auth, happy path, oversize-before-token, source gate,
idempotency, reject-unless-stale, db_version identity, schema validation."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.hashing import sequence_hash

KEY = {"X-API-Key": "test-key"}
SHA = "a" * 64
DV = "uniref-2026-01"


async def _count(db_session) -> int:
    r = await db_session.execute(text("select count(*) from public.msa_ingestions"))
    return int(r.scalar_one())


async def test_request_requires_api_key(client):
    r = await client.post("/v1/uploads/request", json={"sequence": "MK"})
    assert r.status_code == 401


async def test_request_happy_path(client, db_session):
    body = {
        "sequence": "mkt ayiakqr",
        "db_version": DV,
        "declared_size_bytes": 500,
        "declared_sha256": SHA,
    }
    r = await client.post("/v1/uploads/request", json=body, headers=KEY)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["write_token"].startswith("stub.")
    assert j["token_type"] == "Bearer"
    assert j["max_upload_size_bytes"] == 104857600
    h = sequence_hash("MKTAYIAKQR")
    # immutable, unique object at the flat community-contributed namespace
    assert j["osdf_uri"] == (
        f"osdf:///af3-msa-library/community-contributed/{j['ingestion_id']}.a3m"
    )
    row = (
        await db_session.execute(
            text(
                "select status, seq_hash, source, db_version, declared_sha256, "
                "token_ref from public.msa_ingestions where id = :id"
            ),
            {"id": j["ingestion_id"]},
        )
    ).one()
    assert row.status == "pending"
    assert row.seq_hash == h
    assert row.source == "Community Contributed - Cached"
    assert row.db_version == DV
    assert row.declared_sha256 == SHA
    assert row.token_ref  # bound, but the JWT itself is never stored


async def test_request_oversize_rejected_before_token(client, db_session):
    body = {
        "sequence": "MKTAYIAK",
        "db_version": DV,
        "declared_size_bytes": 999_999_999_999,
        "declared_sha256": SHA,
    }
    r = await client.post("/v1/uploads/request", json=body, headers=KEY)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "upload_too_large"
    assert await _count(db_session) == 0  # no pending row, no token minted


async def test_request_disabled_source_rejected(client, db_session):
    body = {
        "sequence": "MKTAYIAK",
        "source": "GDM",  # jobs may only contribute to the community source
        "db_version": DV,
        "declared_size_bytes": 100,
        "declared_sha256": SHA,
    }
    r = await client.post("/v1/uploads/request", json=body, headers=KEY)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "source_not_enabled"
    assert await _count(db_session) == 0


async def test_request_idempotent_reuses_inflight_row(client, db_session):
    body = {
        "sequence": "QRSTVWY", "db_version": DV,
        "declared_size_bytes": 10, "declared_sha256": SHA,
    }
    r1 = await client.post("/v1/uploads/request", json=body, headers=KEY)
    r2 = await client.post("/v1/uploads/request", json=body, headers=KEY)
    assert r1.json()["ingestion_id"] == r2.json()["ingestion_id"]
    assert r1.json()["osdf_uri"] == r2.json()["osdf_uri"]  # same object
    assert await _count(db_session) == 1
    assert r1.json()["write_token"] != r2.json()["write_token"]  # token refreshed


async def test_request_distinct_db_versions_are_separate_ingestions(client, db_session):
    base = {"sequence": "MKDBVER", "declared_size_bytes": 10, "declared_sha256": SHA}
    a = await client.post(
        "/v1/uploads/request", json={**base, "db_version": "v1"}, headers=KEY
    )
    b = await client.post(
        "/v1/uploads/request", json={**base, "db_version": "v2"}, headers=KEY
    )
    assert a.json()["ingestion_id"] != b.json()["ingestion_id"]
    assert await _count(db_session) == 2  # different db_version = different upload


async def test_request_rejected_when_live_entry_exists(client, db_session):
    # an active, non-idle cache entry for this (seq,source,db_version)
    sid = (
        await db_session.execute(
            text(
                "insert into public.msa_sequences (seq_hash, sequence, seq_length) "
                "values (:h,:s,:l) returning id"
            ),
            {"h": sequence_hash("MKLIVECACHE"), "s": "MKLIVECACHE", "l": 11},
        )
    ).scalar_one()
    await db_session.execute(
        text(
            "insert into public.msa_cache_entries "
            "(sequence_id, source, db_version, osdf_uri) "
            "values (:sid,'Community Contributed - Cached',:dv,'osdf:///live.a3m')"
        ),
        {"sid": sid, "dv": DV},
    )
    r = await client.post(
        "/v1/uploads/request",
        json={"sequence": "MKLIVECACHE", "db_version": DV,
              "declared_size_bytes": 10, "declared_sha256": SHA},
        headers=KEY,
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "already_cached"
    assert err["osdf_uri"] == "osdf:///live.a3m"  # job uses the cache instead
    assert await _count(db_session) == 0  # no new ingestion


@pytest.mark.parametrize(
    "payload",
    [
        {"db_version": DV, "declared_size_bytes": 1, "declared_sha256": SHA},
        {"sequence": "MK", "seq_hash": "a" * 64, "db_version": DV,
         "declared_size_bytes": 1, "declared_sha256": SHA},
        {"seq_hash": "a" * 64, "db_version": DV, "declared_size_bytes": 1,
         "declared_sha256": SHA},  # seq_hash without seq_length
        {"sequence": "MK", "db_version": DV, "declared_size_bytes": 1,
         "declared_sha256": "nothex"},
        {"sequence": "MK", "db_version": DV, "declared_size_bytes": 0,
         "declared_sha256": SHA},  # gt=0
        {"sequence": "MK", "declared_size_bytes": 1,
         "declared_sha256": SHA},  # missing db_version
    ],
)
async def test_request_validation(client, payload):
    r = await client.post("/v1/uploads/request", json=payload, headers=KEY)
    assert r.status_code == 422
