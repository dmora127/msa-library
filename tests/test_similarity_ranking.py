"""Pure ranking/filter/dedup rules for similarity hits."""

from __future__ import annotations

from app.services.similarity.models import Thresholds
from app.services.similarity.ranking import (
    dedup_by_hash,
    passes_thresholds,
    rank_key,
    select_hits,
)
from tests.similarity_fakes import hit

T = Thresholds(
    min_percent_identity=90.0, min_query_coverage=0.9, min_target_coverage=0.9, max_hits=5
)
H = "a" * 64


def test_threshold_boundaries_inclusive():
    assert passes_thresholds(hit(H, percent_identity=90.0, query_coverage=0.9,
                                 target_coverage=0.9), T)
    assert not passes_thresholds(hit(H, percent_identity=89.99), T)
    assert not passes_thresholds(hit(H, query_coverage=0.899), T)
    assert not passes_thresholds(hit(H, target_coverage=0.899), T)


def test_high_identity_short_alignment_is_rejected():
    # 100 % identity over 20 % of the protein is NOT a match: coverage is a
    # separate gate, never folded into identity.
    fragment = hit(H, percent_identity=100.0, query_coverage=0.2, target_coverage=1.0,
                   alignment_length=24)
    assert not passes_thresholds(fragment, T)
    assert select_hits([fragment], T) == []


def test_rank_order_uses_the_five_keys_in_order():
    a = hit("a" * 64, percent_identity=99.0, query_coverage=0.95, target_coverage=0.95)
    b = hit("b" * 64, percent_identity=99.0, query_coverage=0.99, target_coverage=0.92)  # min .92
    c = hit("c" * 64, percent_identity=99.0, query_coverage=0.95, target_coverage=0.95,
            bitscore=250.0)
    d = hit("d" * 64, percent_identity=99.0, query_coverage=0.95, target_coverage=0.95,
            bitscore=250.0, evalue=1e-80)
    e = hit("0" * 64, percent_identity=99.0, query_coverage=0.95, target_coverage=0.95,
            bitscore=250.0, evalue=1e-80)  # identical stats to d: hash breaks the tie
    top = hit("f" * 64, percent_identity=99.5, query_coverage=0.90, target_coverage=0.90)
    out = select_hits([a, b, c, d, e, top], Thresholds(90, 0.9, 0.9, 10))
    assert [h.seq_hash[0] for h in out] == ["f", "0", "d", "c", "a", "b"]
    assert rank_key(top) < rank_key(e) < rank_key(d)


def test_dedup_keeps_better_ranked_duplicate():
    worse = hit(H, percent_identity=93.0, source_database="delta")
    better = hit(H, percent_identity=97.0, source_database="nightly")
    out = dedup_by_hash([worse, better])
    assert out == [better]
    out = dedup_by_hash([better, worse])
    assert out == [better]


def test_cap_and_empty():
    hits = [hit(f"{i:064x}", percent_identity=90 + i) for i in range(8)]
    out = select_hits(hits, T)
    assert len(out) == 5
    assert out[0].percent_identity == 97
    assert select_hits([], T) == []
