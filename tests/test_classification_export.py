"""GET /v1/admin/classification/pending.fa — the NR BLAST hand-off export.

Streams `>{sequence_id}|{seq_hash}` FASTA for every protein sequence with an
ACTIVE cache entry still carrying the 'Pending Classification' sentinel in
both fields. Seeding goes through db_session; the request streams through the
same rolled-back session, which also pins the "session outlives the handler"
assumption the StreamingResponse relies on.
"""

from __future__ import annotations

import re

from sqlalchemy import text

from app.services import classification_export
from tests.test_reclassify_external import ADMIN, PENDING, USER, _seed_entry, _seed_sequence

URL = "/v1/admin/classification/pending.fa"
_AA = "ACDEFGHIKLMNPQRSTVWY"
_HDR = re.compile(r"^>(\d+)\|([0-9a-f]{64})$")


def _seq(n: int, i: int) -> str:
    """Distinct protein-looking sequence of length n, unique per i."""
    body = (_AA * (n // 20 + 2))[: n - 3]
    return "M" + _AA[i % 20] + _AA[(i // 20) % 20] + body


async def _pending(db_session, seq: str, *, source: str = "GDM", status: str = "active"):
    sid, h = await _seed_sequence(db_session, seq)
    eid = await _seed_entry(db_session, sid, PENDING, PENDING, source=source)
    if status != "active":
        await db_session.execute(
            text("update public.msa_cache_entries set status=:st where id=:id"),
            {"st": status, "id": eid},
        )
    return sid, h


def _parse(body: str) -> list[tuple[int, str, str]]:
    lines = body.splitlines()
    assert len(lines) % 2 == 0, body[:200]
    out = []
    for hdr, seq in zip(lines[::2], lines[1::2], strict=True):
        m = _HDR.match(hdr)
        assert m, hdr
        out.append((int(m.group(1)), m.group(2), seq))
    return out


async def test_requires_admin_key(client):
    assert (await client.get(URL)).status_code == 401
    assert (await client.get(URL, headers=USER)).status_code == 401


async def test_exports_only_active_both_pending(client, db_session):
    a_id, a_hash = await _pending(db_session, _seq(40, 1))  # in
    await _pending(db_session, _seq(40, 2), status="held")  # held -> out
    sid, _ = await _seed_sequence(db_session, _seq(40, 3))  # Unclassified -> out
    await _seed_entry(db_session, sid, "Unclassified", "Unclassified")
    sid, _ = await _seed_sequence(db_session, _seq(40, 4))  # classified -> out
    await _seed_entry(db_session, sid, "Homo sapiens", "Some protein")
    sid, _ = await _seed_sequence(db_session, _seq(40, 5))  # only species pending -> out
    await _seed_entry(db_session, sid, PENDING, "Named already")
    await _seed_sequence(db_session, _seq(40, 6))  # no entry at all -> out

    r = await client.get(URL, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/x-fasta")
    recs = _parse(r.text)
    assert [(i, h) for i, h, _ in recs] == [(a_id, a_hash)]
    assert recs[0][2] == _seq(40, 1)
    assert r.headers["x-sequence-count"] == "1"


async def test_sequence_with_two_pending_sources_appears_once(client, db_session):
    sid, h = await _pending(db_session, _seq(40, 7), source="GDM")
    await _seed_entry(db_session, sid, PENDING, PENDING, source="OSG-Generated")
    r = await client.get(URL, headers=ADMIN)
    recs = _parse(r.text)
    assert [(i, hh) for i, hh, _ in recs] == [(sid, h)]
    assert r.headers["x-sequence-count"] == "1"


async def test_header_format_and_order(client, db_session):
    seeded = [await _pending(db_session, _seq(50, i)) for i in (12, 11, 10)]
    r = await client.get(URL, headers=ADMIN)
    recs = _parse(r.text)
    ids = [i for i, _, _ in recs]
    assert ids == sorted(ids) and set(ids) == {s for s, _ in seeded}
    by_id = {s: h for s, h in seeded}
    for i, h, _ in recs:
        assert by_id[i] == h
    assert r.headers["x-sequence-count"] == "3"


async def test_empty_export(client):
    r = await client.get(URL, headers=ADMIN)
    assert r.status_code == 200
    assert r.text == ""
    assert r.headers["x-sequence-count"] == "0"
    cd = r.headers["content-disposition"]
    assert cd.startswith('attachment; filename="pending-') and cd.endswith('.fa"')
    assert r.headers["x-min-length"] == "30"
    assert "x-max-length" not in r.headers


async def test_min_length_default_and_override(client, db_session):
    short_id, _ = await _pending(db_session, _seq(29, 20))
    long_id, _ = await _pending(db_session, _seq(30, 21))
    r = await client.get(URL, headers=ADMIN)
    assert [i for i, _, _ in _parse(r.text)] == [long_id]
    assert r.headers["x-min-length"] == "30" and r.headers["x-sequence-count"] == "1"
    r = await client.get(URL, headers=ADMIN, params={"min_length": 1})
    assert sorted(i for i, _, _ in _parse(r.text)) == sorted([short_id, long_id])
    assert r.headers["x-min-length"] == "1" and r.headers["x-sequence-count"] == "2"
    assert (await client.get(URL, headers=ADMIN, params={"min_length": 0})).status_code == 422


async def test_max_length_and_limit(client, db_session):
    ids = [(await _pending(db_session, _seq(n, 30 + k)))[0] for k, n in enumerate((35, 60, 90))]
    r = await client.get(URL, headers=ADMIN, params={"max_length": 60})
    assert sorted(i for i, _, _ in _parse(r.text)) == sorted(ids[:2])
    assert r.headers["x-max-length"] == "60" and r.headers["x-sequence-count"] == "2"
    r = await client.get(URL, headers=ADMIN, params={"limit": 2})
    assert [i for i, _, _ in _parse(r.text)] == sorted(ids)[:2]
    assert r.headers["x-sequence-count"] == "2"
    assert (await client.get(URL, headers=ADMIN, params={"limit": 0})).status_code == 422


async def test_non_protein_excluded(client, db_session):
    sid, _ = await _pending(db_session, _seq(40, 40))
    await db_session.execute(
        text("update public.msa_sequences set molecule_type='rna' where id=:id"), {"id": sid}
    )
    r = await client.get(URL, headers=ADMIN)
    assert r.text == "" and r.headers["x-sequence-count"] == "0"


async def test_round_trip_to_reclassify_callback(client, db_session):
    keep_id, _ = await _pending(db_session, _seq(40, 50))
    sid, h = await _pending(db_session, _seq(40, 51), source="GDM")
    await _seed_entry(db_session, sid, PENDING, PENDING, source="OSG-Generated")
    r = await client.get(URL, headers=ADMIN)
    rec = next(x for x in _parse(r.text) if x[0] == sid)
    # exactly what the remote reclass.sh does with a header
    cb = await client.post(
        "/v1/admin/reclassify",
        json={"sequence_id": rec[0], "sequence_hash": rec[1],
              "species": "Escherichia coli", "query_name": "Beta-lactamase"},
        headers=ADMIN,
    )
    assert cb.status_code == 200, cb.text
    assert cb.json()["updated"] == 2
    r = await client.get(URL, headers=ADMIN)
    assert [i for i, _, _ in _parse(r.text)] == [keep_id]
    assert r.headers["x-sequence-count"] == "1"


async def test_streams_in_multiple_chunks(client, db_session, monkeypatch):
    monkeypatch.setattr(classification_export, "_CHUNK_ROWS", 2)
    seeded = sorted([(await _pending(db_session, _seq(40, 60 + i)))[0] for i in range(5)])
    r = await client.get(URL, headers=ADMIN)
    assert r.status_code == 200
    assert [i for i, _, _ in _parse(r.text)] == seeded
    assert r.headers["x-sequence-count"] == "5"
