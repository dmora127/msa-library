"""POST /v2/query — POST /v1/query plus an opt-in MMseqs2 similarity search.

EXPERIMENTAL COPY. The body below is routes_query.py verbatim (same public,
no-auth router; same inflight gauge, hit-ratio counters, SAVEPOINT-wrapped
query_metrics bump, and hash-collision guard) followed by ONE extra block: if
the exact lookup missed AND the caller set `similarity_search: true`, ask
SimilarityService for neighbours and resolve them through the SAME
SequenceService.lookup() the exact path uses. Once proven, the extra block and
the v2 schemas fold back into v1 and this module goes away.

Similarity is never consulted for an exact hit, for batch `seq_hashes`
requests, or when the flag is absent -- those behave exactly like v1.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.api.routes_query import _QUERY_METRIC_BUMP_SQL
from app.core.metrics import QUERY_INFLIGHT, record_query
from app.schemas.query_v2 import (
    QueryHitV2,
    QueryRequestV2,
    QueryResponseV2,
    QuerySeqResultV2,
    SimilarityStats,
)
from app.services.deps import get_sequence_service, get_similarity_service
from app.services.sequence_service import LookupRow, SequenceService
from app.services.similarity import SimilarityServiceBase

log = logging.getLogger("af3cache.query")
# Public read — no auth, same as /v1/query.
router = APIRouter(prefix="/v2", tags=["query"])


def _hit_from_row(r: LookupRow, similarity: SimilarityStats | None = None) -> QueryHitV2:
    # Field-for-field the v1 QueryHit construction, plus the optional
    # similarity block (omitted from JSON when None).
    return QueryHitV2(
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
        similarity=similarity,
    )


@router.post("/query", response_model=QueryResponseV2)
async def query_v2(
    req: QueryRequestV2,
    svc: SequenceService = Depends(get_sequence_service),
    similarity: SimilarityServiceBase = Depends(get_similarity_service),
) -> QueryResponseV2:
    QUERY_INFLIGHT.inc()
    try:
        # ---- verbatim from /v1/query -------------------------------------
        expected_seq: str | None = None
        if req.sequence:
            normalized, h = svc.normalize_and_hash(req.sequence)
            expected_seq = normalized
            order = [h]
        else:
            order = list(dict.fromkeys(req.seq_hashes or []))

        rows = await svc.lookup(order, req.sources)

        grouped: dict[str, list[QueryHitV2]] = {h: [] for h in order}
        for r in rows:
            if expected_seq is not None and r.sequence != expected_seq:
                log.warning(
                    "query: dropping hit for %s source=%s — stored sequence != requested "
                    "(hash collision / corruption guard)",
                    r.seq_hash,
                    r.source,
                )
                continue
            grouped.setdefault(r.seq_hash, []).append(_hit_from_row(r))

        # Exact hits only: the cache hit ratio keeps its meaning. Similarity
        # outcomes are counted separately in af3cache_similarity_total.
        hits_served = sum(1 for h in order if grouped[h])
        record_query(len(order), hits_served)
        try:
            async with svc.session.begin_nested():
                await svc.session.execute(
                    text(_QUERY_METRIC_BUMP_SQL), {"hits": hits_served}
                )
        except Exception:
            log.warning("query_metrics increment failed", exc_info=True)
        # ---- end verbatim --------------------------------------------------

        match_type: dict[str, str | None] = {
            h: ("exact" if grouped[h] else None) for h in order
        }
        if req.similarity_search and expected_seq is not None and not grouped[order[0]]:
            h = order[0]
            sim_hits = await similarity.query_similarity(expected_seq)
            if sim_hits:
                # Same metadata path as exact hits: active/enabled/idle/source
                # filters and the access bump all apply to the neighbours.
                neighbour_rows = await svc.lookup([s.seq_hash for s in sim_hits], req.sources)
                by_hash: dict[str, list[LookupRow]] = defaultdict(list)
                for r in neighbour_rows:
                    by_hash[r.seq_hash].append(r)
                for s in sim_hits:  # preserve MMseqs ranking
                    stats = SimilarityStats.from_hit(s)
                    for r in by_hash.get(s.seq_hash, []):
                        grouped[h].append(_hit_from_row(r, stats))
                if grouped[h]:
                    match_type[h] = "similar"

        return QueryResponseV2(
            results=[
                QuerySeqResultV2(
                    seq_hash=h,
                    found=bool(grouped[h]),
                    match_type=match_type[h],
                    hits=grouped[h],
                )
                for h in order
            ]
        )
    finally:
        QUERY_INFLIGHT.dec()
