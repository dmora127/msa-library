"""End-to-end against a REAL mmseqs binary. Skipped unless one is reachable via
SIMILARITY_MMSEQS_BINARY or on PATH:

    SIMILARITY_MMSEQS_BINARY=/path/to/mmseqs AF3_TEST_PG_PORT=5433 uv run pytest -q \
        tests/test_similarity_integration.py
"""

from __future__ import annotations

import os
import shutil

import pytest
from sqlalchemy import text

from app.core.hashing import sequence_hash
from app.services.deps import get_similarity_runtime
from app.services.similarity.builders import DeltaBuilder, NightlyBuilder
from app.services.similarity.service import SimilarityRuntime, SimilarityService
from tests.similarity_fakes import make_config
from tests.test_query import _seed_entry, _seed_sequence

MMSEQS = shutil.which(os.environ.get("SIMILARITY_MMSEQS_BINARY", "mmseqs"))
pytestmark = pytest.mark.skipif(MMSEQS is None, reason="mmseqs binary not available")

# A real-looking 120-aa protein and controlled variants of it.
A = (
    "MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEKAVQVKVKALPDAQFEVVHSLAKWKR"
    "QTLGQHDFSAGEGLYTHMKALRPDEDRLSPLHSVYVDQWDWERVMGDGERQFSTLKSTVEAIWAGIKATEAAVSEEFGL"
)[:120]


def _mutate(seq: str, positions: list[int], to: str = "W") -> str:
    s = list(seq)
    for p in positions:
        s[p] = to if s[p] != to else "G"
    return "".join(s)


B = _mutate(A, [5, 20, 35, 50, 65, 80])  # ~95 % identical to A
FRAGMENT = A[:60]  # 100 % identical over 50 % of the query
STALE_ONLY = _mutate(A, [10, 30, 90])
QUERY = _mutate(A, [3, 100])  # 2 mismatches vs A, ~93 % vs B


async def _seed(db_session, seq: str, *, status: str = "active") -> str:
    h, sid = await _seed_sequence(db_session, seq)
    await _seed_entry(db_session, sid, "OSG-Generated", f"osdf:///{h[:10]}.a3m")
    if status != "active":
        await db_session.execute(
            text("update public.msa_cache_entries set status=:st where sequence_id=:sid"),
            {"st": status, "sid": sid},
        )
    return h


async def _seed_all(db_session) -> dict[str, str]:
    return {
        "A": await _seed(db_session, A),
        "B": await _seed(db_session, B),
        "F": await _seed(db_session, FRAGMENT),
        "S": await _seed(db_session, STALE_ONLY, status="stale"),
    }


def _rt(tmp_path, **kw) -> SimilarityRuntime:
    return SimilarityRuntime(make_config(MMSEQS, tmp_path, mmseqs_threads=2, **kw))


async def test_ephemeral_end_to_end(tmp_path, db_session):
    hashes = await _seed_all(db_session)
    svc = SimilarityService(db_session, _rt(tmp_path))
    hits = await svc.query_similarity(QUERY)
    assert [h.seq_hash for h in hits] == [hashes["A"], hashes["B"]], hits
    a, b = hits
    assert 97.0 <= a.percent_identity <= 99.0 and 90.0 <= b.percent_identity < a.percent_identity
    for h in (a, b):
        assert h.query_coverage >= 0.9 and h.target_coverage >= 0.9
        assert h.query_length == 120 and h.target_length == 120
        assert h.source_database == "ephemeral" and h.bitscore > 0
    assert hashes["F"] not in {h.seq_hash for h in hits}  # coverage gate
    assert hashes["S"] not in {h.seq_hash for h in hits}  # never in the dump
    assert list((tmp_path / "tmp").glob("query_similarity_*")) == []


async def test_snapshot_end_to_end(tmp_path, db_session):
    hashes = await _seed_all(db_session)
    rt = _rt(tmp_path, db_mode="snapshot", nightly_build_enabled=True, delta_build_enabled=True)
    svc = SimilarityService(db_session, rt)
    await NightlyBuilder(db_session, rt.build_runner, rt.cfg).build()
    # a neighbour that only exists in the delta
    late = await _seed(db_session, _mutate(A, [7, 70]))
    await DeltaBuilder(db_session, rt.build_runner, rt.cfg).build()
    hits = await svc.query_similarity(QUERY)
    by_hash = {h.seq_hash: h for h in hits}
    assert by_hash[hashes["A"]].source_database == "nightly"
    assert by_hash[late].source_database == "delta"
    assert set(by_hash) == {hashes["A"], hashes["B"], late}
    st = await svc.status()
    assert st["nightly"]["indexed"] is True and st["delta"]["sequence_count"] == 1
    assert st["mmseqs_version"]


async def test_v2_route_with_real_binary(client, db_session, tmp_path):
    from app.main import app

    hashes = await _seed_all(db_session)
    rt = _rt(tmp_path)
    app.dependency_overrides[get_similarity_runtime] = lambda: rt
    try:
        r = await client.post("/v2/query", json={"sequence": QUERY, "similarity_search": True})
        assert r.status_code == 200, r.text
        res = r.json()["results"][0]
        assert res["found"] is True and res["match_type"] == "similar"
        assert res["seq_hash"] == sequence_hash(QUERY)
        assert [h["seq_hash"] for h in res["hits"]] == [hashes["A"], hashes["B"]]
        assert res["hits"][0]["similarity"]["percent_identity"] > 97
        # exact hit on A itself: similarity not consulted, v1 shape + match_type
        r = await client.post("/v2/query", json={"sequence": A, "similarity_search": True})
        res = r.json()["results"][0]
        assert res["match_type"] == "exact" and "similarity" not in res["hits"][0]
    finally:
        app.dependency_overrides.pop(get_similarity_runtime, None)
