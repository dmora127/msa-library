"""Capacity gate (app/services/namespace_capacity.py): the usage formula over
real rows, the TTL cache, the accepting threshold, the exporter-SQL pin, and
GET /v1/admin/namespaces."""

from __future__ import annotations

import pathlib
import re
import uuid

import pytest
import yaml
from sqlalchemy import text

from app.core.errors import NamespaceFull
from app.core.namespaces import load_namespaces
from app.services.namespace_capacity import (
    NAMESPACE_USAGE_SQL,
    NamespaceCapacityGate,
)
from tests.conftest import NS_ISSUER

ADMIN = {"X-API-Key": "admin-test-key"}
SRC = "Community Contributed - Cached"
NS = "af3-msa-library"


async def _cache_entry(db_session) -> int:
    sid = (
        await db_session.execute(
            text(
                "insert into public.msa_sequences (seq_hash, sequence, seq_length) "
                "values (:h, :s, 5) returning id"
            ),
            {"h": uuid.uuid4().hex * 2, "s": "MKTAY"},
        )
    ).scalar_one()
    return (
        await db_session.execute(
            text(
                "insert into public.msa_cache_entries "
                "(sequence_id, source, db_version, osdf_uri) "
                "values (:sid, :src, 'v1', :u) returning id"
            ),
            {"sid": sid, "src": SRC, "u": f"osdf:///{NS}/e/{uuid.uuid4().hex}.a3m"},
        )
    ).scalar_one()


async def _ingestion(db_session, *, status, declared, observed=None, ns=NS,
                     cache_entry_id=None):
    await db_session.execute(
        text(
            """
            insert into public.msa_ingestions
              (status, seq_hash, seq_length, source, db_version,
               declared_size_bytes, declared_sha256, observed_size_bytes,
               osdf_uri, namespace, token_ref, token_expires_at, cache_entry_id)
            values
              (:st, :h, 5, :src, 'v1', :d, :sha, :o, :u, :ns, :ref, now(), :ce)
            """
        ),
        {
            "st": status, "h": uuid.uuid4().hex * 2, "src": SRC, "d": declared,
            "sha": "b" * 64, "o": observed,
            "u": f"osdf:///{ns}/community-contributed/{uuid.uuid4()}.a3m",
            "ns": ns, "ref": uuid.uuid4().hex, "ce": cache_entry_id,
        },
    )


def _gate(namespaces_yaml, entries=None, *, ttl=60, min_headroom_pct=None):
    return NamespaceCapacityGate(
        load_namespaces(
            namespaces_yaml(entries, min_headroom_pct=min_headroom_pct), required=True
        ),
        ttl,
    )


async def test_usage_formula(db_session, namespaces_yaml):
    ce = await _cache_entry(db_session)
    await _ingestion(db_session, status="available", declared=1, observed=1000,
                     cache_entry_id=ce)                      # counts observed
    await _ingestion(db_session, status="available", declared=1, observed=5000)
    #                                                  ^ reaped: no cache entry, ignored
    await _ingestion(db_session, status="pending", declared=200)    # declared reserved
    await _ingestion(db_session, status="uploaded", declared=30)    # declared reserved
    await _ingestion(db_session, status="rejected", declared=7000, observed=7000)
    await _ingestion(db_session, status="pending", declared=999, ns="elsewhere")

    snap = await _gate(namespaces_yaml).snapshot(db_session)
    u = snap[NS]
    assert u.used_bytes == 1000 + 200 + 30
    assert u.available_count == 1
    assert u.inflight_count == 2
    assert u.capacity_bytes == 50_000_000_000_000
    assert u.headroom_bytes == u.capacity_bytes - 1230
    assert u.accepting and u.enabled
    assert "elsewhere" not in snap  # rows outside the registry are ignored


async def test_empty_namespace_reads_zero(db_session, namespaces_yaml):
    snap = await _gate(namespaces_yaml).snapshot(db_session)
    assert snap[NS].used_bytes == 0 and snap[NS].headroom_pct == 100.0


async def test_accepting_flips_at_min_headroom(db_session, namespaces_yaml):
    # capacity 1 TB, headroom threshold 10% -> full at >= 900 GB used
    entries = [{"namespace": NS, "issuer": NS_ISSUER, "capacity_tb": 1}]
    await _ingestion(db_session, status="pending", declared=899_999_999_999)
    gate = _gate(namespaces_yaml, entries)
    assert (await gate.snapshot(db_session))[NS].accepting is True
    await _ingestion(db_session, status="pending", declared=1)  # exactly 10% left
    snap = await gate.snapshot(db_session, fresh=True)
    assert snap[NS].headroom_pct == pytest.approx(10.0)
    assert snap[NS].accepting is False
    assert await gate.accepting(db_session) == []
    with pytest.raises(NamespaceFull) as ei:
        await gate.check(db_session, NS)
    assert ei.value.extra["namespaces"][NS]["used_bytes"] == 900_000_000_000


async def test_per_entry_headroom_override(db_session, namespaces_yaml):
    entries = [
        {"namespace": NS, "issuer": NS_ISSUER, "capacity_tb": 1, "min_headroom_pct": 50},
        {"namespace": "roomy", "issuer": NS_ISSUER, "capacity_tb": 1},
    ]
    await _ingestion(db_session, status="pending", declared=600_000_000_000)
    gate = _gate(namespaces_yaml, entries, min_headroom_pct=10)
    assert await gate.accepting(db_session) == ["roomy"]


async def test_disabled_namespace_never_accepting_and_check_passes(db_session, namespaces_yaml):
    entries = [
        {"namespace": "live", "issuer": NS_ISSUER, "capacity_tb": 1},
        {"namespace": NS, "issuer": NS_ISSUER, "capacity_tb": 1, "enabled": False},
    ]
    gate = _gate(namespaces_yaml, entries)
    snap = await gate.snapshot(db_session)
    assert snap[NS].accepting is False and snap[NS].enabled is False
    await gate.check(db_session, NS)         # disabled is the picker's problem
    await gate.check(db_session, "unknown")  # unknown is the token issuer's problem


async def test_snapshot_is_cached_until_fresh(db_session, namespaces_yaml):
    gate = _gate(namespaces_yaml, ttl=3600)
    assert (await gate.snapshot(db_session))[NS].used_bytes == 0
    await _ingestion(db_session, status="pending", declared=123)
    assert (await gate.snapshot(db_session))[NS].used_bytes == 0       # stale
    assert (await gate.snapshot(db_session, fresh=True))[NS].used_bytes == 123
    gate0 = _gate(namespaces_yaml, ttl=0)
    assert (await gate0.snapshot(db_session))[NS].used_bytes == 123
    await _ingestion(db_session, status="pending", declared=1)
    assert (await gate0.snapshot(db_session))[NS].used_bytes == 124   # ttl 0 = always fresh


def test_exporter_query_matches_gate_sql():
    """postgres-exporter and the gate must agree on what 'used' means."""
    path = pathlib.Path(__file__).parent.parent / (
        "deploy/jetstream2/monitoring/postgres_queries.yaml"
    )
    doc = yaml.safe_load(path.read_text())
    norm = lambda q: re.sub(r"\s+", " ", q).strip()  # noqa: E731
    assert norm(doc["af3cache_namespace"]["query"]) == norm(NAMESPACE_USAGE_SQL)
    labels = {list(m)[0]: m[list(m)[0]]["usage"] for m in doc["af3cache_namespace"]["metrics"]}
    assert labels == {"namespace": "LABEL", "available_count": "GAUGE",
                      "inflight_count": "GAUGE", "used_bytes": "GAUGE"}


async def test_admin_namespaces_endpoint(client, db_session, registry_override):
    registry_override([
        {"namespace": NS, "issuer": NS_ISSUER, "capacity_tb": 1},
        {"namespace": "drained", "issuer": NS_ISSUER, "capacity_tb": 2, "enabled": False},
    ])
    await _ingestion(db_session, status="pending", declared=250_000_000_000)
    r = await client.get("/v1/admin/namespaces", headers=ADMIN)
    assert r.status_code == 200, r.text
    rows = {row["namespace"]: row for row in r.json()}
    assert set(rows) == {NS, "drained"}
    assert rows[NS]["used_bytes"] == 250_000_000_000
    assert rows[NS]["headroom_pct"] == pytest.approx(75.0)
    assert rows[NS]["accepting"] is True and rows[NS]["inflight_count"] == 1
    assert rows["drained"]["enabled"] is False and rows["drained"]["accepting"] is False
    assert rows["drained"]["capacity_bytes"] == 2_000_000_000_000
    job_key = {"X-API-Key": "test-key"}
    assert (await client.get("/v1/admin/namespaces", headers=job_key)).status_code == 401


async def test_migration_backfills_namespace_from_osdf_uri(db_session):
    # 0016's backfill is a one-shot UPDATE; re-run its expression on a fresh
    # row inserted WITHOUT namespace to pin the split_part semantics.
    await db_session.execute(
        text(
            """
            insert into public.msa_ingestions
              (status, seq_hash, seq_length, source, db_version,
               declared_size_bytes, declared_sha256, osdf_uri, token_ref, token_expires_at)
            values ('pending', :h, 5, :src, 'v1', 1, :sha,
                    'osdf:///af3-msa-library/community-contributed/bf.a3m', :ref, now()),
                   ('pending', :h2, 5, :src, 'v1', 1, :sha,
                    'pelican://osgaf3_store1/msa-cache/legacy.a3m', :ref2, now())
            """
        ),
        {"h": uuid.uuid4().hex * 2, "h2": uuid.uuid4().hex * 2, "src": SRC,
         "sha": "c" * 64, "ref": uuid.uuid4().hex, "ref2": uuid.uuid4().hex},
    )
    await db_session.execute(
        text(
            "update public.msa_ingestions set namespace = split_part(osdf_uri, '/', 4) "
            "where osdf_uri like 'osdf:///%' and split_part(osdf_uri, '/', 4) <> '' "
            "and namespace is null"
        )
    )
    rows = (
        await db_session.execute(
            text("select osdf_uri, namespace from public.msa_ingestions order by osdf_uri")
        )
    ).all()
    by_uri = {r.osdf_uri: r.namespace for r in rows}
    assert by_uri["osdf:///af3-msa-library/community-contributed/bf.a3m"] == NS
    assert by_uri["pelican://osgaf3_store1/msa-cache/legacy.a3m"] is None
    idx = (
        await db_session.execute(
            text("select indexname from pg_indexes where tablename = 'msa_ingestions'")
        )
    ).scalars().all()
    assert "msa_ingestions_namespace_status_idx" in idx
