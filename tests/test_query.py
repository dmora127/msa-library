"""Query endpoint: hit / multi-hit / no-hit / batch / auth / mismatch-defense.

Data is seeded through `db_session`; the app request runs on that same
rolled-back connection (client fixture binds get_session -> db_session), so
seeded rows are visible without committing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.hashing import normalize_sequence, sequence_hash

KEY = {"X-API-Key": "test-key"}


async def _seed_sequence(db_session, sequence: str, *, stored_sequence: str | None = None):
    norm = normalize_sequence(sequence)
    h = sequence_hash(norm, already_normalized=True)
    row = await db_session.execute(
        text(
            "insert into public.msa_sequences (seq_hash, sequence, seq_length) "
            "values (:h, :s, :l) returning id"
        ),
        {"h": h, "s": stored_sequence if stored_sequence is not None else norm,
         "l": len(norm)},
    )
    return h, int(row.scalar_one())


async def _seed_entry(db_session, seq_id: int, source: str, uri: str, **meta):
    await db_session.execute(
        text(
            "insert into public.msa_cache_entries "
            "(sequence_id, source, db_version, osdf_uri, a3m_size_bytes, "
            " a3m_sha256, n_sequences) "
            "values (:sid, :src, :dv, :uri, :sz, :sha, :n)"
        ),
        {"sid": seq_id, "src": source, "dv": meta.get("dv", "db1"),
         "uri": uri, "sz": meta.get("size"), "sha": meta.get("sha"),
         "n": meta.get("n")},
    )


async def test_query_is_public_no_auth(client):
    # /v1/query is public read — no credential required.
    r = await client.post("/v1/query", json={"sequence": "MKT"})
    assert r.status_code == 200, r.text


async def test_query_no_hit(client, db_session):
    r = await client.post("/v1/query", json={"sequence": "MKTAYIAK"}, headers=KEY)
    assert r.status_code == 200
    body = r.json()
    assert body["results"][0]["found"] is False
    assert body["results"][0]["hits"] == []


async def test_query_hit_by_sequence(client, db_session):
    h, sid = await _seed_sequence(db_session, "MKTAYIAKQR")
    await _seed_entry(
        db_session, sid, "OSG-Generated", "osdf:///chtc/msa-cache/x.a3m",
        size=500, sha="a" * 64, n=42,
    )
    r = await client.post("/v1/query", json={"sequence": "mkt ayi akqr"}, headers=KEY)
    assert r.status_code == 200
    res = r.json()["results"][0]
    assert res["found"] is True and res["seq_hash"] == h
    hit = res["hits"][0]
    assert hit["osdf_uri"] == "osdf:///chtc/msa-cache/x.a3m"
    assert hit["source"] == "OSG-Generated"
    assert hit["a3m_size_bytes"] == 500 and hit["n_sequences"] == 42


async def test_query_multi_hit_and_source_filter(client, db_session):
    h, sid = await _seed_sequence(db_session, "QRSTVWY")
    await _seed_entry(db_session, sid, "OSG-Generated", "osdf:///a.a3m")
    await _seed_entry(db_session, sid, "GDM", "osdf:///b.a3m")
    r = await client.post("/v1/query", json={"sequence": "QRSTVWY"}, headers=KEY)
    assert {hh["source"] for hh in r.json()["results"][0]["hits"]} == {
        "OSG-Generated", "GDM"
    }
    r2 = await client.post(
        "/v1/query", json={"sequence": "QRSTVWY", "sources": ["GDM"]}, headers=KEY
    )
    hits = r2.json()["results"][0]["hits"]
    assert len(hits) == 1 and hits[0]["source"] == "GDM"


async def test_query_disabled_source_excluded(client, db_session):
    h, sid = await _seed_sequence(db_session, "ACDEFGHIK")
    await _seed_entry(db_session, sid, "Nvidia", "osdf:///n.a3m")
    await db_session.execute(
        text("update public.msa_sources set enabled = false where name = 'Nvidia'")
    )
    r = await client.post("/v1/query", json={"sequence": "ACDEFGHIK"}, headers=KEY)
    assert r.json()["results"][0]["found"] is False


async def test_query_batch_seq_hashes(client, db_session):
    h1, sid1 = await _seed_sequence(db_session, "AAAAAA")
    await _seed_entry(db_session, sid1, "GDM", "osdf:///a.a3m")
    missing = sequence_hash("ZZZZZZ")
    r = await client.post(
        "/v1/query", json={"seq_hashes": [h1, missing]}, headers=KEY
    )
    by_hash = {x["seq_hash"]: x for x in r.json()["results"]}
    assert by_hash[h1]["found"] is True
    assert by_hash[missing]["found"] is False


async def test_query_mismatch_defense(client, db_session):
    # Corruption sim: a row whose stored sequence != what hashes to its seq_hash.
    h, sid = await _seed_sequence(
        db_session, "MKTAYIAKQR", stored_sequence="TOTALLYDIFFERENT"
    )
    await _seed_entry(db_session, sid, "GDM", "osdf:///x.a3m")
    r = await client.post("/v1/query", json={"sequence": "MKTAYIAKQR"}, headers=KEY)
    # hit exists by hash but is dropped because stored sequence differs
    assert r.json()["results"][0]["found"] is False


@pytest.mark.parametrize(
    "payload",
    [{}, {"sequence": "MK", "seq_hashes": ["a" * 64]}, {"seq_hashes": ["bad"]}],
)
async def test_query_validation(client, payload):
    r = await client.post("/v1/query", json=payload, headers=KEY)
    assert r.status_code == 422
