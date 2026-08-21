"""GET /v1/stats — public counts snapshot."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text

from app.core.hashing import sequence_hash


async def test_stats_is_public_and_shape_is_exactly_the_spec(client):
    # no headers — true "empty curl"
    r = await client.get("/v1/stats")
    assert r.status_code == 200
    j = r.json()
    assert set(j.keys()) == {"data"}
    d = j["data"]
    assert set(d.keys()) == {
        "date",
        "alignmentsCached",
        "uniqueProteins",
        "species",
        "communityContributed",
        "queriesToday",
        "queriesTotal",
        "queriesWithHitsToday",
        "queriesWithHitsTotal",
    }
    assert d["date"] == datetime.now(UTC).date().isoformat()
    # types are ints (Pydantic enforces); just sanity-check
    for k in ("alignmentsCached", "uniqueProteins", "species",
              "communityContributed", "queriesToday", "queriesTotal",
              "queriesWithHitsToday", "queriesWithHitsTotal"):
        assert isinstance(d[k], int)


async def test_stats_queries_today_counts_v1_query_calls(client, db_session):
    # ensure today's bucket starts at a known value
    await db_session.execute(
        text("delete from public.query_metrics where day = current_date")
    )
    await db_session.commit()

    r0 = await client.get("/v1/stats")
    assert r0.json()["data"]["queriesToday"] == 0

    KEY = {"X-API-Key": "test-key"}
    for _ in range(3):
        await client.post("/v1/query", json={"sequence": "MKTAYIAKQR"}, headers=KEY)

    r1 = await client.get("/v1/stats")
    assert r1.json()["data"]["queriesToday"] == 3, r1.text

    # a batch query (multiple seq_hashes in one request) is still ONE call
    await client.post(
        "/v1/query",
        json={"seq_hashes": [sequence_hash("MKAB"), sequence_hash("MKCD")]},
        headers=KEY,
    )
    r2 = await client.get("/v1/stats")
    assert r2.json()["data"]["queriesToday"] == 4, r2.text


async def test_stats_queries_total_sums_all_days(client, db_session):
    """queriesTotal aggregates every row in query_metrics, not just today's."""
    await db_session.execute(text("delete from public.query_metrics"))
    # seed three historical days that don't overlap with `current_date`
    await db_session.execute(
        text(
            "insert into public.query_metrics (day, count, hit_count) values "
            "(current_date - 1, 7, 3), (current_date - 2, 11, 5), "
            "(current_date - 10, 4, 0)"
        )
    )
    await db_session.commit()

    # no requests yet today
    d0 = (await client.get("/v1/stats")).json()["data"]
    assert d0["queriesToday"] == 0
    assert d0["queriesTotal"] == 22       # 7 + 11 + 4
    assert d0["queriesWithHitsToday"] == 0
    assert d0["queriesWithHitsTotal"] == 8  # 3 + 5 + 0

    # fire two query calls -> today=2, total = 22 + 2 = 24
    KEY = {"X-API-Key": "test-key"}
    for _ in range(2):
        await client.post("/v1/query", json={"sequence": "MKTAYIAKQR"}, headers=KEY)

    d1 = (await client.get("/v1/stats")).json()["data"]
    assert d1["queriesToday"] == 2
    assert d1["queriesTotal"] == 24
    # both calls missed the cache -> the hit counters don't move
    assert d1["queriesWithHitsToday"] == 0
    assert d1["queriesWithHitsTotal"] == 8


async def test_stats_queries_with_hits_counts_sequences_not_requests(
    client, db_session
):
    """hit_count is per *sequence* served, while queriesToday is per request."""
    await db_session.execute(text("delete from public.query_metrics"))

    # two cached sequences (one with hits from two sources) + one uncached
    hashes = []
    for s, sources in (("MKHITONE", ("GDM",)), ("MKHITTWO", ("GDM", "OSG-Generated"))):
        h = sequence_hash(s)
        sid = (
            await db_session.execute(
                text(
                    "insert into public.msa_sequences (seq_hash, sequence, seq_length) "
                    "values (:h,:s,:l) returning id"
                ),
                {"h": h, "s": s, "l": len(s)},
            )
        ).scalar_one()
        for src in sources:
            await db_session.execute(
                text(
                    "insert into public.msa_cache_entries "
                    "(sequence_id, source, db_version, osdf_uri, species, query_name) "
                    "values (:sid,:src,'v1',:uri,'Homo sapiens','whatever')"
                ),
                {"sid": sid, "src": src, "uri": f"osdf:///{h}-{src}.a3m"},
            )
        hashes.append(h)

    KEY = {"X-API-Key": "test-key"}
    # one request, three sequences: 2 hit (one of them twice over), 1 misses
    r = await client.post(
        "/v1/query",
        json={"seq_hashes": [*hashes, sequence_hash("MKNOTHERE")]},
        headers=KEY,
    )
    assert r.status_code == 200, r.text

    d = (await client.get("/v1/stats")).json()["data"]
    assert d["queriesToday"] == 1            # one POST /v1/query
    assert d["queriesWithHitsToday"] == 2    # two sequences served, not 3 and not 4
    assert d["queriesWithHitsTotal"] == 2

    # a miss-only request bumps queriesToday but not the hit counters
    await client.post(
        "/v1/query", json={"seq_hashes": [sequence_hash("MKNOTHERE")]}, headers=KEY
    )
    d2 = (await client.get("/v1/stats")).json()["data"]
    assert d2["queriesToday"] == 2
    assert d2["queriesWithHitsToday"] == 2


async def test_stats_counts_reflect_seeded_rows(client, db_session):
    # baseline (the rolled-back test DB starts empty)
    base = (await client.get("/v1/stats")).json()["data"]

    # 2 unique sequences; 3 cache entries; species = {Homo sapiens, Mus musculus};
    # 1 community-contributed row
    seq_ids = []
    for s in ("MKABCDEF", "QRSTVWYI"):
        sid = (
            await db_session.execute(
                text(
                    "insert into public.msa_sequences "
                    "(seq_hash, sequence, seq_length) "
                    "values (:h,:s,:l) returning id"
                ),
                {"h": sequence_hash(s), "s": s, "l": len(s)},
            )
        ).scalar_one()
        seq_ids.append(sid)
    rows = [
        (seq_ids[0], "GDM",                              "Homo sapiens",
         "osdf:///a.a3m"),
        (seq_ids[0], "Community Contributed - Cached",  "Mus musculus",
         "osdf:///b.a3m"),
        (seq_ids[1], "OSG-Generated",                    "Homo sapiens",
         "osdf:///c.a3m"),
    ]
    for sid, src, sp, uri in rows:
        await db_session.execute(
            text(
                "insert into public.msa_cache_entries "
                "(sequence_id, source, db_version, osdf_uri, species, query_name) "
                "values (:s,:src,'v1',:uri,:sp,'whatever')"
            ),
            {"s": sid, "src": src, "uri": uri, "sp": sp},
        )

    d = (await client.get("/v1/stats")).json()["data"]
    assert d["alignmentsCached"]      == base["alignmentsCached"] + 3
    assert d["uniqueProteins"]        == base["uniqueProteins"] + 2
    # species += {Homo sapiens, Mus musculus} - whatever was already there
    assert d["species"]                >= base["species"]
    assert d["communityContributed"]  == base["communityContributed"] + 1
