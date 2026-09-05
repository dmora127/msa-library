"""Threshold filtering, deduplication and deterministic ranking of hits.

Pure functions, no IO. Identity and the two coverages are checked SEPARATELY:
100 % identity over 20 % of the protein is not a match.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.services.similarity.models import SimilarityHit, Thresholds


def passes_thresholds(hit: SimilarityHit, t: Thresholds) -> bool:
    return (
        hit.percent_identity >= t.min_percent_identity
        and hit.query_coverage >= t.min_query_coverage
        and hit.target_coverage >= t.min_target_coverage
    )


def rank_key(hit: SimilarityHit) -> tuple:
    """Sort key: identity DESC, min(qcov, tcov) DESC, bitscore DESC, evalue ASC,
    seq_hash ASC. The final component exists only so output is deterministic."""
    return (
        -hit.percent_identity,
        -min(hit.query_coverage, hit.target_coverage),
        -hit.bitscore,
        hit.evalue,
        hit.seq_hash,
    )


def dedup_by_hash(hits: Iterable[SimilarityHit]) -> list[SimilarityHit]:
    """One hit per seq_hash, keeping the best-ranked (nightly vs delta overlap,
    or several alignments of the same pair)."""
    best: dict[str, SimilarityHit] = {}
    for h in hits:
        cur = best.get(h.seq_hash)
        if cur is None or rank_key(h) < rank_key(cur):
            best[h.seq_hash] = h
    return list(best.values())


def select_hits(hits: Iterable[SimilarityHit], t: Thresholds) -> list[SimilarityHit]:
    """filter -> dedup -> rank -> cap. The single entry point services use."""
    kept = dedup_by_hash(h for h in hits if passes_thresholds(h, t))
    kept.sort(key=rank_key)
    return kept[: t.max_hits]
