"""Upload-complete verification matrix.

Drives the StubOsdfClient by injecting bytes at the ingestion's osdf_uri:
sha-match -> available (+ cache entry + visible via the query view);
sha-mismatch / oversize / bad-format / seq-mismatch -> rejected;
OSDF-down -> 503 (row stays 'uploaded', retryable); dup callback -> no-op;
unknown id -> 404.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import text

from app.services.deps import get_osdf_client
from app.services.osdf_client import StubOsdfClient

KEY = {"X-API-Key": "test-key"}
A3M = b">query\nMKTAYIAKQR\n>UniRef100_h1\nMKT-YIAKQR\n"
A3M_SHA = hashlib.sha256(A3M).hexdigest()
A3M_LEN = len(A3M)
SEQ = "MKTAYIAKQR"
DV = "uniref-2026-01"


@pytest.fixture
def osdf_stub():
    from app.main import app

    stub = StubOsdfClient()
    app.dependency_overrides[get_osdf_client] = lambda: stub
    yield stub
    app.dependency_overrides.pop(get_osdf_client, None)


async def _request(client, *, sequence=SEQ, size=A3M_LEN, sha=A3M_SHA):
    r = await client.post(
        "/v1/uploads/request",
        json={
            "sequence": sequence,
            "db_version": DV,
            "declared_size_bytes": size,
            "declared_sha256": sha,
        },
        headers=KEY,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _status(db_session, iid):
    return (
        await db_session.execute(
            text(
                "select status, reject_reason, cache_entry_id, observed_sha256 "
                "from public.msa_ingestions where id=:id"
            ),
            {"id": iid},
        )
    ).one()


async def test_complete_requires_api_key(client):
    r = await client.post(f"/v1/uploads/{uuid.uuid4()}/complete")
    assert r.status_code == 401


async def test_complete_unknown_id_404(client, osdf_stub):
    r = await client.post(f"/v1/uploads/{uuid.uuid4()}/complete", headers=KEY)
    assert r.status_code == 404


async def test_complete_success_available_and_queryable(client, db_session, osdf_stub):
    req = await _request(client)
    osdf_stub.objects[req["osdf_uri"]] = A3M

    r = await client.post(f"/v1/uploads/{req['ingestion_id']}/complete", headers=KEY)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "available"
    assert body["osdf_uri"] == req["osdf_uri"]

    st = await _status(db_session, req["ingestion_id"])
    assert st.status == "available" and st.cache_entry_id is not None
    assert st.observed_sha256 == A3M_SHA

    # now visible through the unchanged msa_cache_lookup view
    q = await client.post("/v1/query", json={"sequence": SEQ}, headers=KEY)
    res = q.json()["results"][0]
    assert res["found"] is True
    hit = res["hits"][0]
    assert hit["source"] == "Community Contributed - Cached"
    assert hit["osdf_uri"] == req["osdf_uri"]
    assert hit["a3m_sha256"] == A3M_SHA and hit["n_sequences"] == 2


async def test_complete_skips_classifier_uses_pending_classification(
    client, db_session, osdf_stub
):
    """The job upload path (/v1/uploads/{id}/complete) must NOT invoke the
    classifier — Diamond is too slow on the hot path. Species/query_name
    default to 'Pending Classification' so a separate cron/sweep can filter
    on that sentinel and run classification asynchronously."""
    from app.main import app
    from app.services.classifier import Classification, StubClassifier
    from app.services.deps import get_classifier

    # If the classifier IS invoked, its values would leak into the cache row.
    poisoned = Classification(
        species="should_not_appear", query_name="should_not_appear"
    )
    app.dependency_overrides[get_classifier] = lambda: StubClassifier(poisoned)
    try:
        req = await _request(client)
        osdf_stub.objects[req["osdf_uri"]] = A3M

        r = await client.post(
            f"/v1/uploads/{req['ingestion_id']}/complete", headers=KEY
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "available"

        q = await client.post("/v1/query", json={"sequence": SEQ}, headers=KEY)
        hit = q.json()["results"][0]["hits"][0]
        assert hit["species"] == "Pending Classification"
        assert hit["query_name"] == "Pending Classification"
        # ... and the poisoned classifier was demonstrably never used:
        assert hit["species"] != "should_not_appear"
        assert hit["query_name"] != "should_not_appear"
    finally:
        app.dependency_overrides.pop(get_classifier, None)


async def test_complete_sha_mismatch_rejected(client, db_session, osdf_stub):
    req = await _request(client, sha="b" * 64)  # declare wrong sha
    osdf_stub.objects[req["osdf_uri"]] = A3M
    r = await client.post(f"/v1/uploads/{req['ingestion_id']}/complete", headers=KEY)
    assert r.json()["status"] == "rejected"
    assert r.json()["reject_reason"] == "sha256_mismatch"
    st = await _status(db_session, req["ingestion_id"])
    assert st.status == "rejected" and st.cache_entry_id is None


async def test_complete_bad_format_rejected(client, db_session, osdf_stub):
    bad = b"not a fasta at all\n"
    req = await _request(client, size=len(bad), sha=hashlib.sha256(bad).hexdigest())
    osdf_stub.objects[req["osdf_uri"]] = bad
    r = await client.post(f"/v1/uploads/{req['ingestion_id']}/complete", headers=KEY)
    assert r.json()["status"] == "rejected"
    assert r.json()["reject_reason"] == "bad_format:no_header"


async def test_complete_seq_hash_mismatch_rejected(client, db_session, osdf_stub):
    # a3m is well-formed but its query doesn't match the declared sequence
    req = await _request(client, sequence="AAAAAACCCC")
    osdf_stub.objects[req["osdf_uri"]] = A3M  # query line is MKTAYIAKQR
    r = await client.post(f"/v1/uploads/{req['ingestion_id']}/complete", headers=KEY)
    assert r.json()["reject_reason"] == "seq_hash_mismatch"


async def test_complete_oversize_rejected(client, db_session, osdf_stub):
    # shrink the limit via app_settings; object then exceeds it on fetch
    await db_session.execute(
        text(
            "update public.app_settings set value='5' "
            "where key='max_upload_size_bytes'"
        )
    )
    req = await _request(client, size=1)  # declared tiny, passes pre-token gate
    osdf_stub.objects[req["osdf_uri"]] = A3M  # 44 bytes > 5
    r = await client.post(f"/v1/uploads/{req['ingestion_id']}/complete", headers=KEY)
    assert r.json()["status"] == "rejected"
    assert r.json()["reject_reason"] == "oversize"


async def test_complete_osdf_unavailable_503_keeps_uploaded(
    client, db_session, osdf_stub
):
    req = await _request(client)
    # object intentionally NOT placed in the stub -> OsdfUnavailable
    r = await client.post(f"/v1/uploads/{req['ingestion_id']}/complete", headers=KEY)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "osdf_unavailable"
    st = await _status(db_session, req["ingestion_id"])
    assert st.status == "uploaded"  # retryable, not corrupted


async def test_complete_idempotent_duplicate_callback(client, db_session, osdf_stub):
    req = await _request(client)
    osdf_stub.objects[req["osdf_uri"]] = A3M
    r1 = await client.post(f"/v1/uploads/{req['ingestion_id']}/complete", headers=KEY)
    r2 = await client.post(f"/v1/uploads/{req['ingestion_id']}/complete", headers=KEY)
    assert r1.json()["status"] == r2.json()["status"] == "available"
    n = (
        await db_session.execute(
            text("select count(*) from public.msa_cache_entries")
        )
    ).scalar_one()
    assert n == 1  # no duplicate publish
