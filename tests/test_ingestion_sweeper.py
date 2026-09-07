"""Abandoned-ingestion sweeper: POST /v1/admin/maintenance/sweep-ingestions.

Rows are seeded through the real upload-request endpoint (so they look exactly
like a job's), aged with SQL on created_at, and swept through the real
verify+publish pipeline against a StubOsdfClient that may or may not hold the
object bytes."""

from __future__ import annotations

import hashlib
import pathlib
import re
import uuid

import httpx
import pytest
import yaml
from sqlalchemy import text

from app.main import app
from app.services.deps import get_osdf_client
from app.services.ingestion_service import IngestionService
from app.services.ingestion_sweeper import EXPIRED_REASON
from app.services.osdf_client import StubOsdfClient

KEY = {"X-API-Key": "test-key"}
ADMIN = {"X-API-Key": "admin-test-key"}
URL = "/v1/admin/maintenance/sweep-ingestions"
A3M = b">query\nMKTAYIAKQR\n>UniRef100_h1\nMKT-YIAKQR\n"
A3M_SHA = hashlib.sha256(A3M).hexdigest()
SEQ = "MKTAYIAKQR"
DV = "uniref-2026-01"


@pytest.fixture
def osdf_stub():
    stub = StubOsdfClient()
    app.dependency_overrides[get_osdf_client] = lambda: stub
    yield stub
    app.dependency_overrides.pop(get_osdf_client, None)


async def _seed(client, db_session, *, age: str, sha: str = A3M_SHA) -> tuple[str, str]:
    """Create a pending row via the real endpoint and age it. Returns (id, uri).

    The sequence is always the fixture a3m's query (publish verifies that the
    object's query hashes to the declared sequence); rows are kept distinct via
    a unique db_version, which is part of the in-flight identity."""
    r = await client.post(
        "/v1/uploads/request",
        json={"sequence": SEQ, "db_version": f"{DV}-{uuid.uuid4().hex[:8]}",
              "declared_size_bytes": len(A3M), "declared_sha256": sha},
        headers=KEY,
    )
    assert r.status_code == 200, r.text
    j = r.json()
    # literal interval (trusted test input): asyncpg wants a timedelta for a
    # bound interval, and the retention tests use the same idiom
    await db_session.execute(
        text(f"update public.msa_ingestions set created_at = now() - interval '{age}' "
             "where id = :id"),
        {"id": j["ingestion_id"]},
    )
    return j["ingestion_id"], j["osdf_uri"]


async def _row(db_session, iid: str):
    return (
        await db_session.execute(
            text("select status, reject_reason, cache_entry_id "
                 "from public.msa_ingestions where id = :id"),
            {"id": iid},
        )
    ).one()


async def _sweep(client, **body) -> dict:
    r = await client.post(URL, json=body, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def _item(res: dict, iid: str) -> dict:
    return next(i for i in res["items"] if i["id"] == iid)


# --- selection ---------------------------------------------------------------


async def test_young_rows_are_not_scanned(client, db_session, osdf_stub):
    iid, _ = await _seed(client, db_session, age="10 minutes")
    res = await _sweep(client)
    assert res["scanned"] == 0 and res["items"] == []
    assert (await _row(db_session, iid)).status == "pending"


async def test_terminal_rows_never_selected(client, db_session, osdf_stub):
    iid, uri = await _seed(client, db_session, age="2 days")
    osdf_stub.objects[uri] = A3M
    first = await _sweep(client)
    assert first["published"] == 1
    again = await _sweep(client)
    assert again["scanned"] == 0
    assert (await _row(db_session, iid)).status == "available"


async def test_limit_and_exhausted_oldest_first(client, db_session, osdf_stub):
    old, _ = await _seed(client, db_session, age="3 hours")
    older, uri = await _seed(client, db_session, age="5 hours")
    osdf_stub.objects[uri] = A3M  # so it leaves the in-flight set after pass 1
    res = await _sweep(client, limit=1)
    assert res["scanned"] == 1 and res["exhausted"] is False
    assert res["items"][0]["id"] == older and res["published"] == 1
    res2 = await _sweep(client, limit=1)
    assert res2["items"][0]["id"] == old and res2["exhausted"] is True


# --- attempts within the window ----------------------------------------------


async def test_object_present_is_published(client, db_session, osdf_stub):
    iid, uri = await _seed(client, db_session, age="45 minutes")
    osdf_stub.objects[uri] = A3M
    res = await _sweep(client)
    assert res["attempted"] == 1 and res["published"] == 1 and res["expired"] == 0
    assert _item(res, iid)["outcome"] == "published"
    row = await _row(db_session, iid)
    assert row.status == "available" and row.cache_entry_id is not None


async def test_object_absent_is_unverified_and_left_uploaded(client, db_session, osdf_stub):
    iid, _ = await _seed(client, db_session, age="45 minutes")
    res = await _sweep(client)
    assert res["unverified"] == 1 and res["expired"] == 0
    it = _item(res, iid)
    assert it["outcome"] == "unverified" and it["status_before"] == "pending"
    row = await _row(db_session, iid)
    assert row.status == "uploaded" and row.reject_reason is None


async def test_bad_object_is_rejected_by_pipeline(client, db_session, osdf_stub):
    iid, uri = await _seed(client, db_session, age="45 minutes", sha="f" * 64)
    osdf_stub.objects[uri] = A3M
    res = await _sweep(client)
    assert res["rejected_verify"] == 1
    assert _item(res, iid)["detail"] == "sha256_mismatch"
    row = await _row(db_session, iid)
    assert row.status == "rejected" and row.reject_reason == "sha256_mismatch"


# --- past the expiry window --------------------------------------------------


async def test_old_unverifiable_row_is_expired(client, db_session, osdf_stub):
    iid, _ = await _seed(client, db_session, age="25 hours")
    res = await _sweep(client)
    assert res["attempted"] == 1 and res["unverified"] == 1 and res["expired"] == 1
    it = _item(res, iid)
    assert it["outcome"] == "expired" and it["detail"] == EXPIRED_REASON
    row = await _row(db_session, iid)
    assert row.status == "rejected"
    assert row.reject_reason == "Expired - Upload unverifiable"
    assert row.cache_entry_id is None
    # the (seq, source, db_version) slot is free again: no cache entry was made
    n = (
        await db_session.execute(text("select count(*) from public.msa_cache_entries"))
    ).scalar_one()
    assert n == 0


async def test_old_row_with_object_is_published_not_expired(client, db_session, osdf_stub):
    iid, uri = await _seed(client, db_session, age="3 days")
    osdf_stub.objects[uri] = A3M
    res = await _sweep(client)
    assert res["published"] == 1 and res["expired"] == 0
    assert (await _row(db_session, iid)).status == "available"


async def test_old_bad_object_gets_pipeline_reason_not_expiry(client, db_session, osdf_stub):
    iid, uri = await _seed(client, db_session, age="30 hours", sha="e" * 64)
    osdf_stub.objects[uri] = A3M
    res = await _sweep(client)
    assert res["rejected_verify"] == 1 and res["expired"] == 0
    assert (await _row(db_session, iid)).reject_reason == "sha256_mismatch"


async def test_custom_thresholds_in_body(client, db_session, osdf_stub):
    iid, _ = await _seed(client, db_session, age="2 hours")
    # default 24h window -> retried; with a 1h window -> expired
    res = await _sweep(client)
    assert res["unverified"] == 1 and res["expired"] == 0
    res = await _sweep(client, max_age_hours=1)
    assert res["expired"] == 1
    assert (await _row(db_session, iid)).reject_reason == EXPIRED_REASON


# --- circuit breaker ---------------------------------------------------------


async def test_breaker_holds_expiries_when_every_attempt_fails(client, db_session, osdf_stub):
    ids = [(await _seed(client, db_session, age="26 hours"))[0] for _ in range(3)]
    res = await _sweep(client)
    assert res["attempted"] == 3 and res["unverified"] == 3
    assert res["expired"] == 0 and res["expiries_held"] == 3
    for iid in ids:
        row = await _row(db_session, iid)
        assert row.status == "uploaded" and row.reject_reason is None
        assert _item(res, iid)["detail"].startswith("expiry held")


async def test_breaker_does_not_engage_when_one_fetch_succeeds(client, db_session, osdf_stub):
    good, uri = await _seed(client, db_session, age="26 hours")
    osdf_stub.objects[uri] = A3M
    bad = [(await _seed(client, db_session, age="26 hours"))[0] for _ in range(2)]
    res = await _sweep(client)
    assert res["published"] == 1 and res["expired"] == 2 and res["expiries_held"] == 0
    assert (await _row(db_session, good)).status == "available"
    for iid in bad:
        assert (await _row(db_session, iid)).reject_reason == EXPIRED_REASON


async def test_breaker_needs_three_attempts(client, db_session, osdf_stub):
    a, _ = await _seed(client, db_session, age="26 hours")
    b, _ = await _seed(client, db_session, age="26 hours")
    res = await _sweep(client)
    assert res["attempted"] == 2 and res["expired"] == 2 and res["expiries_held"] == 0


# --- dry run, errors, auth, validation --------------------------------------


async def test_dry_run_touches_nothing(client, db_session, osdf_stub):
    young, uri = await _seed(client, db_session, age="45 minutes")
    osdf_stub.objects[uri] = A3M
    old, _ = await _seed(client, db_session, age="25 hours")
    res = await _sweep(client, dry_run=True)
    assert res["dry_run"] is True and res["scanned"] == 2 and res["attempted"] == 0
    assert _item(res, young)["outcome"] == "would_attempt"
    assert _item(res, old)["outcome"] == "would_expire"
    assert (await _row(db_session, young)).status == "pending"
    assert (await _row(db_session, old)).status == "pending"


async def test_error_on_one_row_does_not_stop_the_sweep(client, db_session, osdf_stub, monkeypatch):
    boom, _ = await _seed(client, db_session, age="2 hours")
    ok, uri = await _seed(client, db_session, age="3 hours")
    osdf_stub.objects[uri] = A3M
    orig = IngestionService.complete_upload

    async def patched(self, ingestion_id, requester=None):
        if ingestion_id == boom:
            raise RuntimeError("synthetic failure")
        return await orig(self, ingestion_id, requester)

    monkeypatch.setattr(IngestionService, "complete_upload", patched)
    res = await _sweep(client)
    assert res["errors"] == 1 and res["published"] == 1
    assert _item(res, boom)["outcome"] == "error"
    assert "RuntimeError" in _item(res, boom)["detail"]
    assert (await _row(db_session, ok)).status == "available"


async def test_budget_skips_remaining_rows(client, db_session, osdf_stub, monkeypatch):
    for _ in range(2):
        await _seed(client, db_session, age="2 hours")
    import app.services.ingestion_sweeper as mod

    # clock reads: t0, row-1 budget check (in budget), row-2 budget check (over)
    ticks = iter([0.0, 0.0, 1000.0])
    monkeypatch.setattr(mod, "_clock", lambda: next(ticks, 1000.0))
    res = await _sweep(client, budget_seconds=1)
    assert res["attempted"] == 1 and res["skipped_budget"] == 1
    assert res["exhausted"] is False


async def test_requires_admin_key(client):
    assert (await client.post(URL, json={})).status_code == 401
    assert (await client.post(URL, json={}, headers=KEY)).status_code == 401


@pytest.mark.parametrize(
    "body",
    [{"max_age_hours": 1, "min_age_minutes": 60}, {"limit": 0}, {"limit": 5000},
     {"bogus": 1}, {"max_age_hours": 0}],
)
async def test_request_validation(client, body):
    assert (await client.post(URL, json=body, headers=ADMIN)).status_code == 422


async def test_metrics_count_outcomes(client, db_session, osdf_stub):
    def _counter(txt: str, outcome: str) -> float:
        m = re.search(rf'^af3cache_ingestion_sweep_total\{{outcome="{outcome}"\}} (\S+)', txt, re.M)
        return float(m.group(1)) if m else 0.0

    async def _metrics() -> str:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            return (await c.get("/metrics")).text

    before = await _metrics()
    pub, uri = await _seed(client, db_session, age="45 minutes")
    osdf_stub.objects[uri] = A3M
    await _seed(client, db_session, age="25 hours")
    await _sweep(client)
    after = await _metrics()
    assert _counter(after, "published") - _counter(before, "published") == 1
    assert _counter(after, "expired") - _counter(before, "expired") == 1


async def test_exporter_inflight_query_runs(db_session, client, osdf_stub):
    path = pathlib.Path(__file__).parent.parent / (
        "deploy/jetstream2/monitoring/postgres_queries.yaml"
    )
    q = yaml.safe_load(path.read_text())["af3cache_inflight_ingestions"]["query"]
    await _seed(client, db_session, age="1 minute")
    await _seed(client, db_session, age="2 hours")
    await _seed(client, db_session, age="26 hours")
    rows = {r.age: r.count for r in (await db_session.execute(text(q))).all()}
    assert rows == {"fresh": 1, "over_30m": 1, "over_24h": 1}
