"""msa_sources.authorized_requesters (migration 0017): a community upload from
an authorized requester is published as the claiming source (OSG-Generated)
from the moment the cache entry exists — no nightly re-sourcing, so
GET /v1/stats never counts it as community.

Mechanics under test: BEFORE INSERT trigger on msa_cache_entries rewrites
source for the in-flight ingestion's requester; BEFORE UPDATE OF
cache_entry_id trigger on msa_ingestions copies the entry's source back onto
the audit row; both inside the publish transaction. The API is untouched —
jobs still request 'Community Contributed - Cached'.
"""

from __future__ import annotations

import hashlib
import itertools

import pytest
from sqlalchemy import text

from app.core.hashing import sequence_hash
from app.main import app
from app.schemas.ingestion import DEFAULT_SOURCE as COMMUNITY
from app.services.deps import get_osdf_client
from app.services.osdf_client import DryRunOsdfClient, StubOsdfClient
from tests.test_upload_complete import DV, KEY, _complete

OSG = "OSG-Generated"
CHTC = "pipeline-bot@site-a.example.edu"
OSPOOL = "batch.runner@ap.example.org"
OTHER = "colleague@example.edu"

# a fresh sequence per upload (each test rolls back, but keep them distinct
# within a test so entries never collide by accident)
_SEQS = (f"MKTAYIAKQR{'A' * n}" for n in itertools.count(1))


@pytest.fixture
def osdf_stub():
    stub = StubOsdfClient()
    app.dependency_overrides[get_osdf_client] = lambda: stub
    yield stub
    app.dependency_overrides.pop(get_osdf_client, None)


@pytest.fixture
def dryrun_osdf():
    c = DryRunOsdfClient()
    app.dependency_overrides[get_osdf_client] = lambda: c
    yield c
    app.dependency_overrides.pop(get_osdf_client, None)


def _a3m(seq: str) -> bytes:
    return f">query\n{seq}\n>UniRef100_h1\n{seq[:-1]}-\n".encode()


async def _request(client, *, sequence, requester):
    a3m = _a3m(sequence)
    body = {
        "sequence": sequence,
        "db_version": DV,
        "declared_size_bytes": len(a3m),
        "declared_sha256": hashlib.sha256(a3m).hexdigest(),
    }
    if requester is not None:
        body["requester"] = requester
    r = await client.post("/v1/uploads/request", json=body, headers=KEY)
    assert r.status_code == 200, r.text
    return r.json(), a3m


async def _upload(client, osdf_stub, *, requester, sequence=None):
    """request -> inject bytes -> complete (inline verify). Returns the request."""
    sequence = sequence or next(_SEQS)
    req, a3m = await _request(client, sequence=sequence, requester=requester)
    osdf_stub.objects[req["osdf_uri"]] = a3m
    r = await _complete(client, req["ingestion_id"])
    assert r.status_code == 200, r.text
    req["sequence"] = sequence
    return req


async def _rows(db_session, iid):
    return (
        await db_session.execute(
            text(
                """
                select i.status, i.source as ingestion_source, i.cache_entry_id,
                       e.source as entry_source, e.osdf_uri as entry_uri
                  from public.msa_ingestions i
                  left join public.msa_cache_entries e on e.id = i.cache_entry_id
                 where i.id = :id
                """
            ),
            {"id": iid},
        )
    ).one()


async def _entries_for(db_session, sequence):
    return (
        await db_session.execute(
            text(
                """
                select e.id, e.source, e.osdf_uri
                  from public.msa_cache_entries e
                  join public.msa_sequences sq on sq.id = e.sequence_id
                 where sq.seq_hash = :h and e.db_version = :dv
                 order by e.id
                """
            ),
            {"h": sequence_hash(sequence), "dv": DV},
        )
    ).all()


async def test_seed_lists_authorized_identities(db_session):
    pats = (
        await db_session.execute(
            text("select authorized_requesters from public.msa_sources where name = :n"),
            {"n": OSG},
        )
    ).scalar_one()
    assert "pipeline-bot%" in pats and OSPOOL in pats
    # nobody else claims requesters
    others = (
        await db_session.execute(
            text(
                "select name from public.msa_sources "
                "where cardinality(authorized_requesters) > 0 and name <> :n"
            ),
            {"n": OSG},
        )
    ).all()
    assert others == []


@pytest.mark.parametrize("requester", [CHTC, "pipeline-bot@site-a.example.edu", OSPOOL])
async def test_authorized_requester_publishes_as_osg(
    client, db_session, osdf_stub, requester
):
    req = await _upload(client, osdf_stub, requester=requester)
    row = await _rows(db_session, req["ingestion_id"])
    assert row.status == "available" and row.cache_entry_id is not None
    assert row.entry_source == OSG
    assert row.ingestion_source == OSG  # audit row synced in the same txn
    assert row.entry_uri == req["osdf_uri"]

    # and that is what the lookup view / clients see
    q = await client.post("/v1/query", json={"sequence": req["sequence"]}, headers=KEY)
    hits = q.json()["results"][0]["hits"]
    assert [h["source"] for h in hits] == [OSG]


@pytest.mark.parametrize("requester", [OTHER, None])
async def test_other_requesters_stay_community(client, db_session, osdf_stub, requester):
    req = await _upload(client, osdf_stub, requester=requester)
    row = await _rows(db_session, req["ingestion_id"])
    assert row.status == "available"
    assert row.entry_source == COMMUNITY and row.ingestion_source == COMMUNITY


async def test_reupload_of_promoted_sequence_upserts_osg_row(
    client, db_session, osdf_stub
):
    """The BEFORE INSERT flip happens before ON CONFLICT arbitration, so a
    re-upload lands on the existing OSG-Generated row instead of creating the
    colliding community duplicate the old nightly job had to skip."""
    first = await _upload(client, osdf_stub, requester=CHTC)
    seq = first["sequence"]
    before = await _entries_for(db_session, seq)
    assert [e.source for e in before] == [OSG]

    # request_upload's AlreadyCached check looks for a live COMMUNITY entry
    # (API unchanged), so the re-upload goes through and must upsert the OSG row
    second = await _upload(client, osdf_stub, requester=CHTC, sequence=seq)
    after = await _entries_for(db_session, seq)
    assert [e.source for e in after] == [OSG]
    assert after[0].id == before[0].id  # same row, refreshed
    assert after[0].osdf_uri == second["osdf_uri"]
    row = await _rows(db_session, second["ingestion_id"])
    assert row.cache_entry_id == before[0].id and row.ingestion_source == OSG


async def test_empty_list_disables_the_flip(client, db_session, osdf_stub):
    await db_session.execute(
        text("update public.msa_sources set authorized_requesters = '{}' where name = :n"),
        {"n": OSG},
    )
    req = await _upload(client, osdf_stub, requester=CHTC)
    row = await _rows(db_session, req["ingestion_id"])
    assert row.entry_source == COMMUNITY and row.ingestion_source == COMMUNITY


async def test_disabled_target_source_does_not_claim(client, db_session, osdf_stub):
    await db_session.execute(
        text("update public.msa_sources set enabled = false where name = :n"), {"n": OSG}
    )
    req = await _upload(client, osdf_stub, requester=CHTC)
    row = await _rows(db_session, req["ingestion_id"])
    assert row.entry_source == COMMUNITY


async def test_simulate_path_also_flips(client, db_session, dryrun_osdf):
    """Dryrun publish (declared metadata trusted) goes through the same
    msa_cache_entries insert, so the trigger applies there too."""
    seq = next(_SEQS)
    req, _ = await _request(client, sequence=seq, requester=OSPOOL)
    r = await _complete(client, req["ingestion_id"])
    assert r.status_code == 200, r.text
    row = await _rows(db_session, req["ingestion_id"])
    assert row.status == "available"
    assert row.entry_source == OSG and row.ingestion_source == OSG


async def test_stats_counts_authorized_upload_as_osg_immediately(
    client, db_session, osdf_stub
):
    s0 = (await client.get("/v1/stats")).json()["data"]
    await _upload(client, osdf_stub, requester=CHTC)
    s1 = (await client.get("/v1/stats")).json()["data"]
    assert s1["osgGenerated"] == s0["osgGenerated"] + 1
    assert s1["communityContributed"] == s0["communityContributed"]
    assert s1["alignmentsCached"] == s0["alignmentsCached"] + 1
