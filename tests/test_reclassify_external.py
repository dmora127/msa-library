"""External BLAST classification callback:
POST /v1/admin/reclassify -> MaintenanceService.apply_external_classification.

Distinct from the batch sweep (POST /v1/admin/maintenance/reclassify,
tests/test_reclassify.py): this endpoint takes one externally computed result
and applies it only to entries still carrying the 'Pending Classification'
sentinel in BOTH species and query_name.
"""

from __future__ import annotations

from sqlalchemy import text

from app.core.hashing import sequence_hash
from app.services.classifier import PENDING_CLASSIFICATION

ADMIN = {"X-API-Key": "admin-test-key"}
USER = {"X-API-Key": "test-key"}

PENDING = PENDING_CLASSIFICATION


async def _seed_sequence(db_session, sequence: str) -> tuple[int, str]:
    """Insert an msa_sequences row; return (id, seq_hash)."""
    h = sequence_hash(sequence)
    sid = (
        await db_session.execute(
            text(
                "insert into public.msa_sequences (seq_hash, sequence, seq_length) "
                "values (:h, :s, :l) returning id"
            ),
            {"h": h, "s": sequence, "l": len(sequence)},
        )
    ).scalar_one()
    return sid, h


async def _seed_entry(
    db_session, sid: int, species: str, query_name: str, source: str = "GDM",
    db_version: str = "v1",
) -> int:
    return (
        await db_session.execute(
            text(
                "insert into public.msa_cache_entries "
                "(sequence_id, source, db_version, osdf_uri, species, query_name) "
                "values (:sid, :src, :dbv, 'osdf:///x.a3m', :sp, :qn) returning id"
            ),
            {"sid": sid, "src": source, "dbv": db_version, "sp": species, "qn": query_name},
        )
    ).scalar_one()


async def _entry_rows(db_session, ids: list[int]) -> dict:
    return {
        row.id: (row.species, row.query_name)
        for row in await db_session.execute(
            text(
                "select id, species, query_name from public.msa_cache_entries "
                "where id = any(:ids)"
            ),
            {"ids": ids},
        )
    }


def _payload(sid: int, h: str, **over) -> dict:
    return {
        "sequence_id": sid,
        "sequence_hash": h,
        "species": "Homo sapiens",
        "query_name": "P53_HUMAN Cellular tumor antigen p53",
        **over,
    }


async def test_requires_admin_key(client, db_session):
    sid, h = await _seed_sequence(db_session, "MKAUTH")
    assert (await client.post(
        "/v1/admin/reclassify", json=_payload(sid, h))).status_code == 401
    assert (await client.post(
        "/v1/admin/reclassify", json=_payload(sid, h), headers=USER)).status_code == 401


async def test_updates_all_pending_entries_only(client, db_session):
    sid, h = await _seed_sequence(db_session, "MKPEND")
    e1 = await _seed_entry(db_session, sid, PENDING, PENDING, source="GDM")
    e2 = await _seed_entry(db_session, sid, PENDING, PENDING, source="OSG-Generated")
    e3 = await _seed_entry(db_session, sid, "E. coli", "Known Protein", source="Nvidia")

    r = await client.post("/v1/admin/reclassify", json=_payload(sid, h), headers=ADMIN)
    assert r.status_code == 200
    assert r.json() == {"sequence_id": sid, "updated": 2}

    rows = await _entry_rows(db_session, [e1, e2, e3])
    expect = ("Homo sapiens", "P53_HUMAN Cellular tumor antigen p53")
    assert rows[e1] == expect
    assert rows[e2] == expect
    assert rows[e3] == ("E. coli", "Known Protein")  # untouched

    bumped = (
        await db_session.execute(
            text(
                "select count(*) from public.msa_cache_entries "
                "where id = any(:ids) and updated_at is not null"
            ),
            {"ids": [e1, e2]},
        )
    ).scalar_one()
    assert bumped == 2


async def test_requires_both_fields_pending(client, db_session):
    # species pending but query_name already set -> AND predicate skips it
    sid, h = await _seed_sequence(db_session, "MKMIXED")
    eid = await _seed_entry(db_session, sid, PENDING, "Something Else")

    r = await client.post("/v1/admin/reclassify", json=_payload(sid, h), headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["updated"] == 0
    assert (await _entry_rows(db_session, [eid]))[eid] == (PENDING, "Something Else")


async def test_does_not_touch_other_sequences(client, db_session):
    sid, h = await _seed_sequence(db_session, "MKMINE")
    await _seed_entry(db_session, sid, PENDING, PENDING)
    other_sid, _ = await _seed_sequence(db_session, "MKOTHER")
    other_eid = await _seed_entry(db_session, other_sid, PENDING, PENDING)

    r = await client.post("/v1/admin/reclassify", json=_payload(sid, h), headers=ADMIN)
    assert r.status_code == 200 and r.json()["updated"] == 1
    assert (await _entry_rows(db_session, [other_eid]))[other_eid] == (PENDING, PENDING)


async def test_unknown_sequence_id_404(client, db_session):
    r = await client.post(
        "/v1/admin/reclassify",
        json=_payload(999999999, "0" * 64),
        headers=ADMIN,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "sequence_not_found"


async def test_hash_mismatch_409_and_no_update(client, db_session):
    sid, h = await _seed_sequence(db_session, "MKHASH")
    eid = await _seed_entry(db_session, sid, PENDING, PENDING)
    wrong = ("0" * 64) if h != "0" * 64 else ("1" * 64)

    r = await client.post(
        "/v1/admin/reclassify", json=_payload(sid, wrong), headers=ADMIN
    )
    assert r.status_code == 409
    body = r.json()["error"]
    assert body["code"] == "sequence_hash_mismatch"
    assert body["sequence_id"] == sid
    assert h not in str(r.json())  # stored hash is never echoed
    assert (await _entry_rows(db_session, [eid]))[eid] == (PENDING, PENDING)


async def test_malformed_hash_422(client, db_session):
    sid, h = await _seed_sequence(db_session, "MKFMT")
    for bad in ("short", h.upper(), "z" * 64):
        r = await client.post(
            "/v1/admin/reclassify",
            json=_payload(sid, bad),
            headers=ADMIN,
        )
        assert r.status_code == 422, bad


async def test_sentinel_or_empty_values_422(client, db_session):
    sid, h = await _seed_sequence(db_session, "MKSENT")
    for over in (
        {"species": PENDING},
        {"query_name": PENDING},
        {"species": "Unclassified"},
        {"species": "   "},
        {"query_name": ""},
    ):
        r = await client.post(
            "/v1/admin/reclassify", json=_payload(sid, h, **over), headers=ADMIN
        )
        assert r.status_code == 422, over


async def test_extra_field_422(client, db_session):
    sid, h = await _seed_sequence(db_session, "MKEXTRA")
    r = await client.post(
        "/v1/admin/reclassify",
        json=_payload(sid, h, surprise="field"),
        headers=ADMIN,
    )
    assert r.status_code == 422


async def test_repeat_call_is_idempotent(client, db_session):
    sid, h = await _seed_sequence(db_session, "MKTWICE")
    eid = await _seed_entry(db_session, sid, PENDING, PENDING)
    payload = _payload(sid, h)

    r1 = await client.post("/v1/admin/reclassify", json=payload, headers=ADMIN)
    assert r1.status_code == 200 and r1.json()["updated"] == 1
    r2 = await client.post("/v1/admin/reclassify", json=payload, headers=ADMIN)
    assert r2.status_code == 200 and r2.json()["updated"] == 0
    assert (await _entry_rows(db_session, [eid]))[eid] == (
        "Homo sapiens", "P53_HUMAN Cellular tumor antigen p53"
    )
