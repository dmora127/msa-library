"""POST /v1/admin/ingest: admin-only bulk seed of pre-existing a3m files.

Bypasses the community-only upload gate, uses the provided osdf_uri directly,
runs the same verify+publish pipeline as a normal upload-complete.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import text

from app.core.hashing import sequence_hash
from app.main import app
from app.services.classifier import Classification, StubClassifier
from app.services.deps import get_classifier, get_osdf_client
from app.services.osdf_client import DryRunOsdfClient, StubOsdfClient

ADMIN = {"X-API-Key": "admin-test-key"}
USER = {"X-API-Key": "test-key"}

A3M = b">query\nMKTAYIAKQR\n>UniRef100_h1\nMKT-YIAKQR\n"
A3M_SHA = hashlib.sha256(A3M).hexdigest()
A3M_LEN = len(A3M)
SEQ = "MKTAYIAKQR"
URI = "osdf:///osg-public/data/dannyTesting/AF-X6R8R1-F1-msa_v1.a3m"


@pytest.fixture
def osdf_with_a3m():
    stub = StubOsdfClient({URI: A3M})
    app.dependency_overrides[get_osdf_client] = lambda: stub
    yield stub
    app.dependency_overrides.pop(get_osdf_client, None)


@pytest.fixture
def osdf_dryrun():
    stub = DryRunOsdfClient()
    app.dependency_overrides[get_osdf_client] = lambda: stub
    yield stub
    app.dependency_overrides.pop(get_osdf_client, None)


def _body(**over):
    base = {
        "sequence": SEQ, "molecule_type": "protein",
        "source": "GDM", "db_version": "uniref-2026-01",
        "osdf_uri": URI, "declared_size_bytes": A3M_LEN,
        "declared_sha256": A3M_SHA, "requester": "bulk-test",
    }
    base.update(over)
    return base


async def test_admin_ingest_requires_admin_key(client):
    assert (await client.post("/v1/admin/ingest", json=_body())).status_code == 401
    # a job key is not an admin key
    r = await client.post("/v1/admin/ingest", json=_body(), headers=USER)
    assert r.status_code == 401


async def test_admin_ingest_curated_source_publishes_and_queryable(
    client, db_session, osdf_with_a3m
):
    r = await client.post("/v1/admin/ingest", json=_body(), headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "available"
    assert body["osdf_uri"] == URI  # provided URI preserved (no template magic)

    q = await client.post("/v1/query", json={"sequence": SEQ}, headers=USER)
    hits = q.json()["results"][0]["hits"]
    gdm = next(h for h in hits if h["source"] == "GDM")
    assert gdm["osdf_uri"] == URI
    assert gdm["a3m_sha256"] == A3M_SHA
    assert gdm["n_sequences"] == 2  # came from real a3m validation
    # Default classifier in tests is Noop -> Unclassified.
    assert gdm["species"] == "Unclassified"
    assert gdm["query_name"] == "Unclassified"


async def test_admin_ingest_classifier_populates_species_and_query_name(
    client, db_session, osdf_with_a3m
):
    cls = Classification(species="Homo sapiens", query_name="Test Protein P12345")
    app.dependency_overrides[get_classifier] = lambda: StubClassifier(cls)
    try:
        r = await client.post("/v1/admin/ingest", json=_body(), headers=ADMIN)
        assert r.json()["status"] == "available"
        q = await client.post("/v1/query", json={"sequence": SEQ}, headers=USER)
        gdm = next(
            h for h in q.json()["results"][0]["hits"] if h["source"] == "GDM"
        )
        assert gdm["species"] == "Homo sapiens"
        assert gdm["query_name"] == "Test Protein P12345"
    finally:
        app.dependency_overrides.pop(get_classifier, None)


async def test_admin_ingest_unknown_source_rejected(client, osdf_with_a3m):
    r = await client.post(
        "/v1/admin/ingest", json=_body(source="Nonexistent"), headers=ADMIN
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "source_not_enabled"


async def test_admin_ingest_disabled_source_rejected(
    client, db_session, osdf_with_a3m
):
    await db_session.execute(
        text("update public.msa_sources set enabled=false where name='Nvidia'")
    )
    r = await client.post(
        "/v1/admin/ingest", json=_body(source="Nvidia"), headers=ADMIN
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "source_not_enabled"


async def test_admin_ingest_sha_mismatch_marked_rejected(
    client, db_session, osdf_with_a3m
):
    r = await client.post(
        "/v1/admin/ingest", json=_body(declared_sha256="b" * 64), headers=ADMIN
    )
    assert r.status_code == 200  # callback succeeded; verdict is rejected
    body = r.json()
    assert body["status"] == "rejected"
    assert body["reject_reason"] == "sha256_mismatch"


async def test_admin_ingest_reject_unless_stale(client, db_session, osdf_with_a3m):
    sid = (
        await db_session.execute(
            text(
                "insert into public.msa_sequences (seq_hash, sequence, seq_length) "
                "values (:h,:s,:l) returning id"
            ),
            {"h": sequence_hash(SEQ), "s": SEQ, "l": len(SEQ)},
        )
    ).scalar_one()
    await db_session.execute(
        text(
            "insert into public.msa_cache_entries "
            "(sequence_id, source, db_version, osdf_uri) "
            "values (:sid,'GDM','uniref-2026-01','osdf:///existing.a3m')"
        ),
        {"sid": sid},
    )
    r = await client.post("/v1/admin/ingest", json=_body(), headers=ADMIN)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "already_cached"
    assert err["osdf_uri"] == "osdf:///existing.a3m"


async def test_admin_ingest_metadata_overrides_win_over_classifier(
    client, db_session, osdf_with_a3m
):
    # classifier would say something else, but explicit overrides must win
    # (and the classifier should be skipped when both species+query_name given)
    cls = Classification(species="Should Not Appear", query_name="nope")
    app.dependency_overrides[get_classifier] = lambda: StubClassifier(cls)
    try:
        r = await client.post(
            "/v1/admin/ingest",
            json=_body(
                species="Homo sapiens",
                query_name="Q5VSL9 Some protein",
                n_sequences=137,
            ),
            headers=ADMIN,
        )
        assert r.json()["status"] == "available"
        q = await client.post("/v1/query", json={"sequence": SEQ}, headers=USER)
        gdm = next(
            h for h in q.json()["results"][0]["hits"] if h["source"] == "GDM"
        )
        assert gdm["species"] == "Homo sapiens"
        assert gdm["query_name"] == "Q5VSL9 Some protein"
        assert gdm["n_sequences"] == 137
    finally:
        app.dependency_overrides.pop(get_classifier, None)


@pytest.fixture
def osdf_real_empty():
    # A non-simulated client with NO objects: any fetch raises OsdfUnavailable.
    stub = StubOsdfClient({})
    app.dependency_overrides[get_osdf_client] = lambda: stub
    yield stub
    app.dependency_overrides.pop(get_osdf_client, None)


async def test_admin_ingest_skip_verify_publishes_without_fetch(
    client, db_session, osdf_real_empty
):
    # OSDF client is real (is_simulated False) and has no object -> a fetch
    # would fail. skip_verify must publish trusting declared metadata anyway.
    assert osdf_real_empty.is_simulated is False
    r = await client.post(
        "/v1/admin/ingest", json=_body(skip_verify=True), headers=ADMIN
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "available"
    q = await client.post("/v1/query", json={"sequence": SEQ}, headers=USER)
    gdm = next(h for h in q.json()["results"][0]["hits"] if h["source"] == "GDM")
    assert gdm["osdf_uri"] == URI


async def test_admin_ingest_without_skip_verify_fetches_real_osdf(
    client, db_session, osdf_real_empty
):
    # Same empty real client, no skip_verify -> the server tries to fetch the
    # (absent) object -> OsdfUnavailable -> 503, never published.
    r = await client.post("/v1/admin/ingest", json=_body(), headers=ADMIN)
    assert r.status_code == 503, r.text


async def test_admin_ingest_dryrun_publishes_trusting_declared(
    client, db_session, osdf_dryrun
):
    r = await client.post("/v1/admin/ingest", json=_body(), headers=ADMIN)
    assert r.json()["status"] == "available"  # dryrun trusts declared sha/size
    obs = (
        await db_session.execute(
            text(
                "select observed_sha256, observed_size_bytes, requester "
                "from public.msa_ingestions where osdf_uri=:u"
            ),
            {"u": URI},
        )
    ).one()
    assert obs.observed_sha256 == A3M_SHA
    assert obs.observed_size_bytes == A3M_LEN
    assert obs.requester == "bulk-test"  # audit row records who imported


async def test_admin_ingest_mints_uri_from_ingestion_id_when_omitted(
    client, db_session, osdf_real_empty
):
    # No osdf_uri in the body -> the server renders osdf_namespace_template
    # against the row id it just minted, exactly like the job upload path, and
    # hands the URI back so the caller knows where to put the object.
    body = _body(skip_verify=True)
    body.pop("osdf_uri")
    r = await client.post("/v1/admin/ingest", json=body, headers=ADMIN)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == "available"
    assert j["osdf_uri"] == (
        "osdf:///af3-msa-library/community-contributed/"
        f"{j['ingestion_id']}.a3m"
    )
    # the row, and the cache entry published from it, both point there
    row = (
        await db_session.execute(
            text("select osdf_uri from public.msa_ingestions where id = :id"),
            {"id": j["ingestion_id"]},
        )
    ).one()
    assert row.osdf_uri == j["osdf_uri"]
    q = await client.post("/v1/query", json={"sequence": SEQ}, headers=USER)
    gdm = next(h for h in q.json()["results"][0]["hits"] if h["source"] == "GDM")
    assert gdm["osdf_uri"] == j["osdf_uri"]


async def test_admin_ingest_minted_uris_are_unique_per_row(
    client, db_session, osdf_real_empty
):
    # Two ingests of DIFFERENT sequences each get their own row id -> their own
    # object path (no content-derived collisions).
    def _no_uri(**over):
        b = _body(skip_verify=True, **over)
        b.pop("osdf_uri")
        return b

    a = await client.post("/v1/admin/ingest", json=_no_uri(), headers=ADMIN)
    b = await client.post(
        "/v1/admin/ingest",
        json=_no_uri(sequence="MKTAYIAKQRQ", source="Nvidia"),
        headers=ADMIN,
    )
    assert a.json()["status"] == b.json()["status"] == "available"
    assert a.json()["osdf_uri"] != b.json()["osdf_uri"]


@pytest.mark.parametrize("payload", [
    {**{k: v for k, v in {
        "sequence": SEQ, "molecule_type": "protein", "source": "GDM",
        "db_version": "v1", "osdf_uri": URI,
        "declared_size_bytes": A3M_LEN, "declared_sha256": A3M_SHA,
    }.items() if k != omit}}
    # osdf_uri is NOT here: omitting it is the "let the server mint it" path
    for omit in ("source", "db_version",
                 "declared_size_bytes", "declared_sha256")
])
async def test_admin_ingest_required_fields(client, payload):
    r = await client.post("/v1/admin/ingest", json=payload, headers=ADMIN)
    assert r.status_code == 422


async def test_admin_ingest_prefers_room_but_never_refuses(
    client, db_session, osdf_real_empty, registry_override
):
    # server-minted URI path (_mint_osdf_uri) uses the same picker as job
    # uploads, but admin ingest describes an object that may already be on the
    # origin, so an all-full registry warns and proceeds instead of 507.
    from tests.conftest import NS_ISSUER

    registry_override([
        {"namespace": "af3-msa-library", "issuer": NS_ISSUER, "capacity_tb": 1},
    ])
    import uuid

    await db_session.execute(
        text(
            """
            insert into public.msa_ingestions
              (status, seq_hash, seq_length, source, db_version, declared_size_bytes,
               declared_sha256, osdf_uri, namespace, token_ref, token_expires_at)
            values ('pending', :h, 5, 'Community Contributed - Cached', 'v1',
                    950000000000, :sha, 'osdf:///af3-msa-library/x/full.a3m',
                    'af3-msa-library', :ref, now())
            """
        ),
        {"h": uuid.uuid4().hex * 2, "sha": "e" * 64, "ref": uuid.uuid4().hex},
    )
    body = _body(skip_verify=True)
    body.pop("osdf_uri")
    r = await client.post("/v1/admin/ingest", json=body, headers=ADMIN)
    assert r.status_code == 200, r.text
    ns = (
        await db_session.execute(
            text("select namespace from public.msa_ingestions where id = :id"),
            {"id": r.json()["ingestion_id"]},
        )
    ).scalar_one()
    assert ns == "af3-msa-library"  # column written on the admin path too


async def test_admin_ingest_authorized_requester_keeps_asserted_source(
    client, db_session, osdf_with_a3m
):
    """migration 0017: the requester->source flip applies only to COMMUNITY
    uploads. An admin ingest asserting GDM for a requester that OSG-Generated
    claims must stay GDM on both the entry and the audit row."""
    r = await client.post(
        "/v1/admin/ingest",
        json=_body(requester="pipeline-bot@site-a.example.edu"),
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text
    row = (
        await db_session.execute(
            text(
                """
                select i.source as ingestion_source, e.source as entry_source
                  from public.msa_ingestions i
                  join public.msa_cache_entries e on e.id = i.cache_entry_id
                 where i.id = :id
                """
            ),
            {"id": r.json()["ingestion_id"]},
        )
    ).one()
    assert row.entry_source == "GDM" and row.ingestion_source == "GDM"
