"""Periodic reclassification sweep:
POST /v1/admin/maintenance/reclassify -> MaintenanceService.reclassify_unclassified."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.hashing import sequence_hash
from app.main import app
from app.services.classifier import UNCLASSIFIED, Classification, StubClassifier
from app.services.deps import get_classifier

ADMIN = {"X-API-Key": "admin-test-key"}
USER = {"X-API-Key": "test-key"}


async def _seed(db_session, sequence: str, species: str, query_name: str):
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
    eid = (
        await db_session.execute(
            text(
                "insert into public.msa_cache_entries "
                "(sequence_id, source, db_version, osdf_uri, species, query_name) "
                "values (:sid,'GDM','v1','osdf:///x.a3m',:sp,:qn) returning id"
            ),
            {"sid": sid, "sp": species, "qn": query_name},
        )
    ).scalar_one()
    return eid


@pytest.fixture
def stub_classifier():
    """Override get_classifier with a configurable StubClassifier."""
    stub = StubClassifier(UNCLASSIFIED)
    app.dependency_overrides[get_classifier] = lambda: stub
    yield stub
    app.dependency_overrides.pop(get_classifier, None)


async def test_reclassify_requires_admin_key(client):
    assert (await client.post(
        "/v1/admin/maintenance/reclassify")).status_code == 401
    assert (await client.post(
        "/v1/admin/maintenance/reclassify", headers=USER)).status_code == 401


async def test_reclassify_updates_unclassified_rows_on_confident_hit(
    client, db_session, stub_classifier
):
    eid_uncls = await _seed(db_session, "MKUNCLS", "Unclassified", "Unclassified")
    eid_already = await _seed(db_session, "MKKNOWN", "E. coli", "Known Protein")

    stub_classifier.result = Classification(
        species="Homo sapiens", query_name="Test Protein P12345"
    )
    r = await client.post(
        "/v1/admin/maintenance/reclassify", headers=ADMIN
    )
    assert r.status_code == 200
    body = r.json()
    # already-classified rows are excluded by the WHERE (no needless classify
    # calls), so only the Unclassified row is scanned + updated.
    assert body["scanned"] == 1 and body["updated"] == 1
    assert body["last_id"] == eid_uncls
    assert body["exhausted"] is True  # scanned < limit

    rows = {
        row.id: (row.species, row.query_name)
        for row in await db_session.execute(
            text(
                "select id, species, query_name from public.msa_cache_entries "
                "where id in (:a, :b)"
            ),
            {"a": eid_uncls, "b": eid_already},
        )
    }
    assert rows[eid_uncls] == ("Homo sapiens", "Test Protein P12345")
    assert rows[eid_already] == ("E. coli", "Known Protein")  # untouched


async def test_reclassify_skip_when_classifier_still_unclassified(
    client, db_session, stub_classifier
):
    # default stub_classifier result is UNCLASSIFIED
    eid = await _seed(db_session, "MKSTILLUN", "Unclassified", "Unclassified")
    r = await client.post(
        "/v1/admin/maintenance/reclassify", headers=ADMIN
    )
    body = r.json()
    assert body["scanned"] == 1 and body["updated"] == 0
    assert body["last_id"] == eid
    row = (
        await db_session.execute(
            text(
                "select species, query_name from public.msa_cache_entries "
                "where id = :id"
            ),
            {"id": eid},
        )
    ).one()
    assert row.species == "Unclassified" and row.query_name == "Unclassified"


async def test_reclassify_cursor_drains_full_backlog(
    client, db_session, stub_classifier
):
    # 5 unclassified rows; limit=2 + advancing after_id walks ALL of them
    # in 3 batches even though stub_classifier returns UNCLASSIFIED (so no
    # row would normally "drop out" of the head of the query).
    ids = []
    for s in ("MKA", "MKB", "MKC", "MKD", "MKE"):
        ids.append(await _seed(db_session, s, "Unclassified", "Unclassified"))

    cursor, total_scanned, batches = 0, 0, 0
    while True:
        batches += 1
        r = await client.post(
            f"/v1/admin/maintenance/reclassify?limit=2&after_id={cursor}",
            headers=ADMIN,
        )
        body = r.json()
        total_scanned += body["scanned"]
        if body["exhausted"]:
            break
        cursor = body["last_id"]

    assert total_scanned >= 5  # every row was visited at least once
    assert batches == 3        # 2 + 2 + 1 -> third batch is exhausted


async def test_reclassify_limit_caps_per_run_work(
    client, db_session, stub_classifier
):
    # 3 unclassified rows, limit=2 -> only 2 reclassified this pass
    seq_ids = []
    for s in ("MKA", "MKB", "MKC"):
        seq_ids.append(await _seed(db_session, s, "Unclassified", "Unclassified"))
    stub_classifier.result = Classification(
        species="X. test", query_name="Y protein"
    )
    r = await client.post(
        "/v1/admin/maintenance/reclassify?limit=2", headers=ADMIN
    )
    body = r.json()
    assert body["scanned"] == 2 and body["updated"] == 2
    assert body["exhausted"] is False  # scanned == limit -> more remain
    cnt = (
        await db_session.execute(
            text(
                "select count(*) from public.msa_cache_entries "
                "where id = any(:ids) and species='Unclassified'"
            ),
            {"ids": seq_ids},
        )
    ).scalar_one()
    assert cnt == 1
