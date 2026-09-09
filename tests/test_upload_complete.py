"""Upload-complete verification matrix.

The route answers at once ("uploaded - pending verification", HTTP code from
UPLOAD_COMPLETE_ASYNC_STATUS_CODE) and verification runs in the background.
In tests the `client` fixture installs an INLINE BackgroundVerifier, so the
verify pipeline has already run on the test session by the time the response
comes back and the DB row carries the verdict.

Drives the StubOsdfClient by injecting bytes at the ingestion's osdf_uri:
sha-match -> available (+ cache entry + visible via the query view);
sha-mismatch / oversize / bad-format / seq-mismatch -> rejected (reason in
the row, logged); OSDF-down -> row stays 'uploaded', logged, no 503; dup
callback -> stored verdict; unknown id -> 404.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid

import httpx
import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.main import app
from app.schemas.ingestion import PENDING_VERIFICATION_STATUS as PENDING
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


async def _complete(client, iid):
    return await client.post(f"/v1/uploads/{iid}/complete", headers=KEY)


def _assert_pending(r, req, *, code=200):
    assert r.status_code == code, r.text
    body = r.json()
    assert body["status"] == PENDING
    assert body["osdf_uri"] == req["osdf_uri"]
    assert body["reject_reason"] is None
    assert body["ingestion_id"] == req["ingestion_id"]


async def test_complete_requires_api_key(client):
    r = await client.post(f"/v1/uploads/{uuid.uuid4()}/complete")
    assert r.status_code == 401


async def test_complete_unknown_id_404(client, osdf_stub):
    r = await client.post(f"/v1/uploads/{uuid.uuid4()}/complete", headers=KEY)
    assert r.status_code == 404


async def test_complete_success_available_and_queryable(client, db_session, osdf_stub):
    req = await _request(client)
    osdf_stub.objects[req["osdf_uri"]] = A3M

    r = await _complete(client, req["ingestion_id"])
    _assert_pending(r, req)

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
    on that sentinel and run classification asynchronously. The override is
    installed through dependency_overrides, which proves the background path
    inherits the request's resolved classifier (IngestionService.with_session)."""
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

        r = await _complete(client, req["ingestion_id"])
        _assert_pending(r, req)
        assert (await _status(db_session, req["ingestion_id"])).status == "available"

        q = await client.post("/v1/query", json={"sequence": SEQ}, headers=KEY)
        hit = q.json()["results"][0]["hits"][0]
        assert hit["species"] == "Pending Classification"
        assert hit["query_name"] == "Pending Classification"
        # ... and the poisoned classifier was demonstrably never used:
        assert hit["species"] != "should_not_appear"
        assert hit["query_name"] != "should_not_appear"
    finally:
        app.dependency_overrides.pop(get_classifier, None)


async def test_complete_sha_mismatch_rejected(client, db_session, osdf_stub, caplog):
    req = await _request(client, sha="b" * 64)  # declare wrong sha
    osdf_stub.objects[req["osdf_uri"]] = A3M
    with caplog.at_level(logging.WARNING, logger="af3cache.ingestion"):
        r = await _complete(client, req["ingestion_id"])
    _assert_pending(r, req)  # the verdict is NOT in the response any more
    st = await _status(db_session, req["ingestion_id"])
    assert st.status == "rejected" and st.cache_entry_id is None
    assert st.reject_reason == "sha256_mismatch"
    # the reason is logged, with the id, since no client sees it synchronously
    assert any(
        f"ingestion={req['ingestion_id']}" in m and "REJECTED reason=sha256_mismatch" in m
        for m in caplog.messages
    ), caplog.messages


async def test_complete_bad_format_rejected(client, db_session, osdf_stub):
    bad = b"not a fasta at all\n"
    req = await _request(client, size=len(bad), sha=hashlib.sha256(bad).hexdigest())
    osdf_stub.objects[req["osdf_uri"]] = bad
    r = await _complete(client, req["ingestion_id"])
    _assert_pending(r, req)
    st = await _status(db_session, req["ingestion_id"])
    assert st.status == "rejected"
    assert st.reject_reason == "bad_format:no_header"


async def test_complete_seq_hash_mismatch_rejected(client, db_session, osdf_stub):
    # a3m is well-formed but its query doesn't match the declared sequence
    req = await _request(client, sequence="AAAAAACCCC")
    osdf_stub.objects[req["osdf_uri"]] = A3M  # query line is MKTAYIAKQR
    r = await _complete(client, req["ingestion_id"])
    _assert_pending(r, req)
    st = await _status(db_session, req["ingestion_id"])
    assert st.status == "rejected" and st.reject_reason == "seq_hash_mismatch"


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
    r = await _complete(client, req["ingestion_id"])
    _assert_pending(r, req)
    st = await _status(db_session, req["ingestion_id"])
    assert st.status == "rejected" and st.reject_reason == "oversize"


async def test_complete_osdf_unavailable_returns_pending_keeps_uploaded(
    client, db_session, osdf_stub, caplog
):
    """OSDF down is no longer a 503 to the job: the callback was accepted, the
    row stays 'uploaded' (the sweeper retries it) and the API log says so."""
    req = await _request(client)
    # object intentionally NOT placed in the stub -> OsdfUnavailable
    with caplog.at_level(logging.WARNING, logger="af3cache.ingestion"):
        r = await _complete(client, req["ingestion_id"])
    _assert_pending(r, req)
    st = await _status(db_session, req["ingestion_id"])
    assert st.status == "uploaded"  # retryable, not corrupted
    assert any(
        f"ingestion={req['ingestion_id']}" in m and "UNVERIFIED" in m
        and "left 'uploaded'" in m
        for m in caplog.messages
    ), caplog.messages


async def test_complete_background_exception_leaves_uploaded_and_logs(
    client, db_session, osdf_stub, caplog, monkeypatch
):
    """An unexpected failure inside the pipeline must not escape (the job has
    its answer) and must not leave the row in a terminal state."""
    req = await _request(client)

    async def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(osdf_stub, "fetch", boom)
    with caplog.at_level(logging.ERROR, logger="af3cache.ingestion"):
        r = await _complete(client, req["ingestion_id"])
    _assert_pending(r, req)
    st = await _status(db_session, req["ingestion_id"])
    assert st.status == "uploaded"
    rec = [
        x for x in caplog.records
        if f"ingestion={req['ingestion_id']}" in x.getMessage() and "ERROR" in x.getMessage()
    ]
    assert rec and rec[0].exc_info is not None  # traceback attached


async def test_complete_idempotent_duplicate_callback(client, db_session, osdf_stub):
    req = await _request(client)
    osdf_stub.objects[req["osdf_uri"]] = A3M
    r1 = await _complete(client, req["ingestion_id"])
    _assert_pending(r1, req)
    # second callback finds the verified row: stored verdict, HTTP 200, no
    # second verification -- this is the caller's "poll"
    r2 = await _complete(client, req["ingestion_id"])
    assert r2.status_code == 200
    assert r2.json()["status"] == "available"
    assert r2.json()["osdf_uri"] == req["osdf_uri"]
    n = (
        await db_session.execute(
            text("select count(*) from public.msa_cache_entries")
        )
    ).scalar_one()
    assert n == 1  # no duplicate publish


async def test_complete_status_code_follows_setting(client, db_session, osdf_stub):
    """UPLOAD_COMPLETE_ASYNC_STATUS_CODE=202 applies to the pending answer only;
    the terminal short-circuit stays 200."""
    tuned = get_settings().model_copy(update={"upload_complete_async_status_code": 202})
    app.dependency_overrides[get_settings] = lambda: tuned
    try:
        req = await _request(client)
        osdf_stub.objects[req["osdf_uri"]] = A3M
        r1 = await _complete(client, req["ingestion_id"])
        _assert_pending(r1, req, code=202)
        r2 = await _complete(client, req["ingestion_id"])
        assert r2.status_code == 200 and r2.json()["status"] == "available"
    finally:
        app.dependency_overrides.pop(get_settings, None)


async def test_repeat_post_while_inflight_does_not_resubmit(
    client, db_session, osdf_stub, inline_verifier, monkeypatch
):
    """A job retrying its callback while the first verification is still
    running gets the same pending answer; the route asks the verifier, which
    refuses the duplicate, and nothing touches the row."""
    calls: list[str] = []

    async def refuse(ingestion_id, svc):
        calls.append(ingestion_id)
        return False

    monkeypatch.setattr(inline_verifier, "submit", refuse)
    req = await _request(client)
    osdf_stub.objects[req["osdf_uri"]] = A3M
    r1 = await _complete(client, req["ingestion_id"])
    r2 = await _complete(client, req["ingestion_id"])
    _assert_pending(r1, req)
    _assert_pending(r2, req)
    assert calls == [req["ingestion_id"]] * 2
    assert (await _status(db_session, req["ingestion_id"])).status == "uploaded"


async def test_complete_metrics_outcome_counter(client, db_session, osdf_stub):
    def _counter(txt: str, outcome: str) -> float:
        m = re.search(
            rf'^af3cache_upload_verify_total\{{outcome="{outcome}"\}} (\S+)', txt, re.M
        )
        return float(m.group(1)) if m else 0.0

    async def _metrics() -> str:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            return (await c.get("/metrics")).text

    before = await _metrics()
    ok = await _request(client)
    osdf_stub.objects[ok["osdf_uri"]] = A3M
    await _complete(client, ok["ingestion_id"])
    bad = await _request(client, sequence="AAAAAACCCC", sha="b" * 64)
    osdf_stub.objects[bad["osdf_uri"]] = A3M
    await _complete(client, bad["ingestion_id"])
    missing = await _request(client, sequence="MKTAYIAKQRW")
    await _complete(client, missing["ingestion_id"])  # no object -> unverified
    after = await _metrics()
    assert _counter(after, "published") - _counter(before, "published") == 1
    assert _counter(after, "rejected") - _counter(before, "rejected") == 1
    assert _counter(after, "unverified") - _counter(before, "unverified") == 1
