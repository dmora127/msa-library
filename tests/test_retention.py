"""Idle-based retention: source policy, idle sweep, reaper+grace, db_version
identity, coarsened access-bump, reject-unless-stale-then-allowed."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.hashing import sequence_hash
from app.main import app
from app.services.deps import get_osdf_client
from app.services.osdf_client import StubOsdfClient

KEY = {"X-API-Key": "test-key"}
ADMIN = {"X-API-Key": "admin-test-key"}


@pytest.fixture
def osdf_stub():
    stub = StubOsdfClient()
    app.dependency_overrides[get_osdf_client] = lambda: stub
    yield stub
    app.dependency_overrides.pop(get_osdf_client, None)


async def _seq(db_session, sequence):
    return (
        await db_session.execute(
            text(
                "insert into public.msa_sequences (seq_hash, sequence, seq_length) "
                "values (:h,:s,:l) on conflict (seq_hash) do update "
                "set sequence=excluded.sequence returning id"
            ),
            {"h": sequence_hash(sequence), "s": sequence, "l": len(sequence)},
        )
    ).scalar_one()


async def _entry(db_session, sid, source, dv, uri, *, last_access_sql="now()",
                 status="active", stale_since_sql="null"):
    await db_session.execute(
        text(
            f"insert into public.msa_cache_entries "
            f"(sequence_id, source, db_version, osdf_uri, status, "
            f" last_accessed_at, stale_since) "
            f"values (:sid,:src,:dv,:uri,:st,{last_access_sql},{stale_since_sql})"
        ),
        {"sid": sid, "src": source, "dv": dv, "uri": uri, "st": status},
    )


async def test_source_policy_columns(db_session):
    row = (
        await db_session.execute(
            text(
                "select idle_ttl_days, notes, metadata from public.msa_sources "
                "where name='Community Contributed - Cached'"
            )
        )
    ).one()
    assert row.idle_ttl_days == 90  # culls after 90 days without a query hit
    assert row.notes and row.metadata == {}
    gdm = (
        await db_session.execute(
            text("select idle_ttl_days from public.msa_sources where name='GDM'")
        )
    ).scalar_one()
    assert gdm is None  # curated set: never culled


async def test_idle_entry_hidden_then_sweep_marks_stale(client, db_session):
    sid = await _seq(db_session, "MKIDLESEQ")
    await _entry(
        db_session, sid, "Community Contributed - Cached", "v1",
        "osdf:///idle.a3m", last_access_sql="now() - interval '120 days'",
    )
    # defensively hidden from query even before the sweep runs
    q = await client.post("/v1/query", json={"sequence": "MKIDLESEQ"}, headers=KEY)
    assert q.json()["results"][0]["found"] is False

    m = await client.post("/v1/admin/maintenance/run", headers=ADMIN)
    assert m.status_code == 200 and m.json()["marked_stale"] >= 1
    st = (
        await db_session.execute(
            text("select status from public.msa_cache_entries where sequence_id=:s"),
            {"s": sid},
        )
    ).scalar_one()
    assert st == "stale"


async def test_reaper_respects_grace_then_deletes(client, db_session, osdf_stub):
    sid = await _seq(db_session, "MKREAPSEQ")
    osdf_stub.objects["osdf:///reap.a3m"] = b">q\nMK\n"
    # stale, but only 1h in -> within the 12h grace -> not reaped
    await _entry(
        db_session, sid, "Community Contributed - Cached", "v1",
        "osdf:///reap.a3m", status="stale",
        stale_since_sql="now() - interval '1 hour'",
    )
    r1 = await client.post("/v1/admin/maintenance/run", headers=ADMIN)
    assert r1.json()["reaped"] == 0
    assert "osdf:///reap.a3m" in osdf_stub.objects

    # age past the grace window -> reaped (object deleted, row gone)
    await db_session.execute(
        text(
            "update public.msa_cache_entries set stale_since=now()-interval '13 hours' "
            "where sequence_id=:s"
        ),
        {"s": sid},
    )
    r2 = await client.post("/v1/admin/maintenance/run", headers=ADMIN)
    assert r2.json()["reaped"] == 1
    assert "osdf:///reap.a3m" not in osdf_stub.objects
    n = (
        await db_session.execute(
            text("select count(*) from public.msa_cache_entries where sequence_id=:s"),
            {"s": sid},
        )
    ).scalar_one()
    assert n == 0


async def test_two_db_versions_both_served_with_version_in_hit(client, db_session):
    sid = await _seq(db_session, "MKTWOVER")
    await _entry(db_session, sid, "Community Contributed - Cached", "v1",
                 "osdf:///v1.a3m")
    await _entry(db_session, sid, "Community Contributed - Cached", "v2",
                 "osdf:///v2.a3m")
    q = await client.post("/v1/query", json={"sequence": "MKTWOVER"}, headers=KEY)
    hits = q.json()["results"][0]["hits"]
    assert {h["db_version"] for h in hits} == {"v1", "v2"}  # job decides which


async def test_coarsened_access_bump(client, db_session):
    sid = await _seq(db_session, "MKBUMPSEQ")
    # old but not idle (2d < 90d): returned, and older than 1d granularity -> bumped
    await _entry(db_session, sid, "Community Contributed - Cached", "old",
                 "osdf:///old.a3m", last_access_sql="now() - interval '2 days'")
    # recent: returned but within granularity -> NOT bumped
    await _entry(db_session, sid, "Community Contributed - Cached", "fresh",
                 "osdf:///fresh.a3m", last_access_sql="now() - interval '10 seconds'")
    await client.post("/v1/query", json={"sequence": "MKBUMPSEQ"}, headers=KEY)
    rows = dict(
        (r.db_version, r.age_s)
        for r in await db_session.execute(
            text(
                "select db_version, extract(epoch from (now()-last_accessed_at)) "
                "as age_s from public.msa_cache_entries where sequence_id=:s"
            ),
            {"s": sid},
        )
    )
    assert rows["old"] < 5            # bumped to ~now
    assert rows["fresh"] < 60         # untouched (was ~10s, still recent)


async def test_reject_unless_stale_then_allowed_after_stale(client, db_session):
    sid = await _seq(db_session, "MKSTALEFLOW")
    await _entry(db_session, sid, "Community Contributed - Cached", "v1",
                 "osdf:///cached.a3m")
    body = {
        "sequence": "MKSTALEFLOW", "db_version": "v1",
        "declared_size_bytes": 10, "declared_sha256": "a" * 64,
    }
    blocked = await client.post("/v1/uploads/request", json=body, headers=KEY)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["osdf_uri"] == "osdf:///cached.a3m"

    # entry goes stale -> the same (seq,source,db_version) is contributable again
    await db_session.execute(
        text(
            "update public.msa_cache_entries set status='stale', stale_since=now() "
            "where sequence_id=:s"
        ),
        {"s": sid},
    )
    allowed = await client.post("/v1/uploads/request", json=body, headers=KEY)
    assert allowed.status_code == 200
    assert allowed.json()["osdf_uri"].endswith(
        f"/{allowed.json()['ingestion_id']}.a3m"
    )
