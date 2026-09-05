"""POST /v2/query: v1 behaviour preserved, plus the similarity opt-in.

The similarity service is stubbed; the metadata resolution for neighbour hashes
goes through the real SequenceService against seeded rows.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.errors import SimilarityDisabled, SimilarityUnavailable
from app.core.hashing import normalize_sequence, sequence_hash
from app.services.deps import get_similarity_service
from app.services.similarity.service import StubSimilarityService
from tests.similarity_fakes import hit
from tests.test_query import _seed_entry, _seed_sequence

QUERY = "MKTAYIAKQRQISFVKSHFSRQ"
NEIGHBOUR = "MKTAYIAKQRQISFVKSHFSRK"
STATS_KEYS = {
    "percent_identity", "query_coverage", "target_coverage", "alignment_length",
    "query_length", "target_length", "bitscore", "evalue", "source_database",
}


@pytest.fixture
def stub():
    from app.main import app

    s = StubSimilarityService()
    app.dependency_overrides[get_similarity_service] = lambda: s
    yield s
    app.dependency_overrides.pop(get_similarity_service, None)


async def _seed_neighbour(db_session, source="OSG-Generated", uri="osdf:///n.a3m"):
    h, sid = await _seed_sequence(db_session, NEIGHBOUR)
    await _seed_entry(db_session, sid, source, uri, size=321, sha="b" * 64, n=7)
    return h, sid


async def test_v2_is_public_and_miss_is_byte_identical_to_v1(client, stub):
    r1 = await client.post("/v1/query", json={"sequence": QUERY})
    r2 = await client.post("/v2/query", json={"sequence": QUERY})
    assert r1.status_code == r2.status_code == 200
    assert r2.content == r1.content
    assert r2.json() == {"results": [{
        "seq_hash": sequence_hash(QUERY), "found": False, "hits": []}]}
    assert stub.calls == []


async def test_v2_exact_hit_matches_v1_plus_match_type(client, db_session, stub):
    h, sid = await _seed_sequence(db_session, QUERY)
    await _seed_entry(db_session, sid, "OSG-Generated", "osdf:///x.a3m", size=5, sha="a" * 64, n=1)
    stub.hits = [hit("c" * 64)]  # would be returned if similarity were consulted
    r1 = await client.post("/v1/query", json={"sequence": QUERY})
    r2 = await client.post("/v2/query", json={"sequence": QUERY, "similarity_search": True})
    v1, v2 = r1.json()["results"][0], r2.json()["results"][0]
    assert v2.pop("match_type") == "exact"
    assert v2 == v1  # everything else identical, no "similarity" key anywhere
    assert "similarity" not in r2.json()["results"][0]["hits"][0]
    assert stub.calls == []


async def test_v2_miss_without_opt_in_never_calls_similarity(client, db_session, stub):
    await _seed_neighbour(db_session)
    stub.hits = [hit(sequence_hash(NEIGHBOUR))]
    r = await client.post("/v2/query", json={"sequence": QUERY})
    assert r.json()["results"][0] == {"seq_hash": sequence_hash(QUERY), "found": False,
                                      "hits": []}
    assert stub.calls == []


async def test_v1_ignores_the_flag(client, db_session, stub):
    await _seed_neighbour(db_session)
    stub.hits = [hit(sequence_hash(NEIGHBOUR))]
    r = await client.post("/v1/query", json={"sequence": QUERY, "similarity_search": True})
    assert r.status_code == 200
    assert r.json()["results"][0]["found"] is False and stub.calls == []


async def test_similarity_with_seq_hashes_is_422(client, stub):
    r = await client.post("/v2/query", json={"seq_hashes": ["a" * 64], "similarity_search": True})
    assert r.status_code == 422
    assert stub.calls == []


async def test_similar_hit_shape(client, db_session, stub):
    nh, _ = await _seed_neighbour(db_session)
    stub.hits = [hit(nh, percent_identity=95.45, query_coverage=0.982, target_coverage=0.967)]
    r = await client.post("/v2/query", json={"sequence": QUERY, "similarity_search": True})
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["found"] is True and res["match_type"] == "similar"
    assert res["seq_hash"] == sequence_hash(QUERY)  # the QUERY's hash
    assert len(res["hits"]) == 1
    h = res["hits"][0]
    assert h["seq_hash"] == nh  # the NEIGHBOUR's hash
    assert h["osdf_uri"] == "osdf:///n.a3m" and h["a3m_size_bytes"] == 321
    assert h["source"] == "OSG-Generated" and h["n_sequences"] == 7
    assert set(h["similarity"]) == STATS_KEYS
    assert h["similarity"]["percent_identity"] == 95.45
    assert h["similarity"]["query_coverage"] == 0.982
    assert h["similarity"]["source_database"] == "ephemeral"
    assert stub.calls == [normalize_sequence(QUERY)]


async def test_similar_hits_keep_ranking_and_honour_source_filters(client, db_session, stub):
    # neighbour A: two sources; neighbour B: GDM only; C: disabled source; D: not in DB
    ha, ia = await _seed_neighbour(db_session)
    await _seed_entry(db_session, ia, "GDM", "osdf:///a-gdm.a3m")
    hb, ib = await _seed_sequence(db_session, "MKTAYIAKQRQISFVKSHFSRR")
    await _seed_entry(db_session, ib, "GDM", "osdf:///b.a3m")
    hc, ic = await _seed_sequence(db_session, "MKTAYIAKQRQISFVKSHFSRD")
    await _seed_entry(db_session, ic, "Nvidia", "osdf:///c.a3m")
    await db_session.execute(
        text("update public.msa_sources set enabled=false where name='Nvidia'")
    )
    hd = "d" * 64
    stub.hits = [hit(hb, percent_identity=99), hit(ha, percent_identity=95),
                 hit(hc, percent_identity=94), hit(hd, percent_identity=93)]

    r = await client.post("/v2/query", json={"sequence": QUERY, "similarity_search": True})
    hits = r.json()["results"][0]["hits"]
    assert [(h["seq_hash"], h["source"]) for h in hits] == [
        (hb, "GDM"), (ha, "OSG-Generated"), (ha, "GDM"),
    ]
    assert [h["similarity"]["percent_identity"] for h in hits] == [99, 95, 95]

    r = await client.post("/v2/query", json={
        "sequence": QUERY, "similarity_search": True, "sources": ["OSG-Generated"]})
    hits = r.json()["results"][0]["hits"]
    assert [(h["seq_hash"], h["source"]) for h in hits] == [(ha, "OSG-Generated")]


async def test_similarity_no_neighbours_is_a_plain_miss(client, db_session, stub):
    r = await client.post("/v2/query", json={"sequence": QUERY, "similarity_search": True})
    assert r.json()["results"][0] == {"seq_hash": sequence_hash(QUERY), "found": False,
                                      "hits": []}
    assert stub.calls == [QUERY]


async def test_similarity_errors_use_the_app_error_envelope(client, stub):
    stub.error = SimilarityUnavailable("similarity search failed at mmseqs search (timeout)")
    r = await client.post("/v2/query", json={"sequence": QUERY, "similarity_search": True})
    assert r.status_code == 503
    assert r.json() == {"error": {"code": "similarity_unavailable",
                                  "message": "similarity search failed at mmseqs search (timeout)"}}
    stub.error = SimilarityDisabled("similarity search is disabled on this deployment")
    r = await client.post("/v2/query", json={"sequence": QUERY, "similarity_search": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "similarity_disabled"


async def test_openapi_documents_v2(client):
    spec = (await client.get("/openapi.json")).json()
    body = spec["paths"]["/v2/query"]["post"]["requestBody"]["content"]["application/json"]
    ref = body["schema"]["$ref"].rsplit("/", 1)[1]
    assert "similarity_search" in spec["components"]["schemas"][ref]["properties"]
    assert "similarity_search" not in spec["components"]["schemas"]["QueryRequest"]["properties"]
