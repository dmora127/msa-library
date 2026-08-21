"""POST /v1/query — sequence/hash -> cached MSA hits.

Always 200. "No hits" = `found=false` + empty `hits` (never 404). When the
caller supplies a raw sequence we additionally enforce plan.md's defensive
check: a returned row whose stored `sequence` != the requested normalized
sequence is dropped and logged (hash-collision / corruption guard).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.schemas.query import QueryHit, QueryRequest, QueryResponse, QuerySeqResult
from app.services.deps import get_sequence_service
from app.services.sequence_service import SequenceService

log = logging.getLogger("af3cache.query")
# Public read — no auth. Anyone may query the cache; only /v1/uploads/* is gated.
router = APIRouter(prefix="/v1", tags=["query"])

# Per-day counters for /v1/stats. `count` is +1 per request; `hit_count` is
# +N where N = sequences in this request that came back with >=1 hit (so the
# two are different units and hit_count may exceed count on batch traffic).
# Hot-row contention is fine at modest QPS; if it ever becomes a bottleneck
# switch to bucketed counters or an in-process flush.
_QUERY_METRIC_BUMP_SQL = (
    "insert into public.query_metrics (day, count, hit_count) "
    "values (current_date, 1, :hits) "
    "on conflict (day) do update set count = query_metrics.count + 1, "
    "hit_count = query_metrics.hit_count + excluded.hit_count"
)


@router.post("/query", response_model=QueryResponse)
async def query(
    req: QueryRequest,
    svc: SequenceService = Depends(get_sequence_service),
) -> QueryResponse:
    expected_seq: str | None = None
    if req.sequence:
        normalized, h = svc.normalize_and_hash(req.sequence)
        expected_seq = normalized
        order = [h]
    else:
        order = list(dict.fromkeys(req.seq_hashes or []))

    rows = await svc.lookup(order, req.sources)

    grouped: dict[str, list[QueryHit]] = {h: [] for h in order}
    for r in rows:
        if expected_seq is not None and r.sequence != expected_seq:
            log.warning(
                "query: dropping hit for %s source=%s — stored sequence != requested "
                "(hash collision / corruption guard)",
                r.seq_hash,
                r.source,
            )
            continue
        grouped.setdefault(r.seq_hash, []).append(
            QueryHit(
                seq_hash=r.seq_hash,
                source=r.source,
                osdf_uri=r.osdf_uri,
                db_version=r.db_version,
                species=r.species,
                query_name=r.query_name,
                a3m_size_bytes=r.a3m_size_bytes,
                a3m_sha256=r.a3m_sha256,
                n_sequences=r.n_sequences,
                seq_length=r.seq_length,
            )
        )

    # Best-effort per-day metrics. Bumped *after* grouping so hit_count sees
    # the post-collision-guard picture. Wrapped in a SAVEPOINT so a write
    # failure here (table missing, lock timeout, etc.) doesn't invalidate the
    # outer read transaction and 500 the query.
    hits_served = sum(1 for h in order if grouped[h])
    try:
        async with svc.session.begin_nested():
            await svc.session.execute(
                text(_QUERY_METRIC_BUMP_SQL), {"hits": hits_served}
            )
    except Exception:
        log.warning("query_metrics increment failed", exc_info=True)

    return QueryResponse(
        results=[
            QuerySeqResult(seq_hash=h, found=bool(grouped[h]), hits=grouped[h])
            for h in order
        ]
    )
