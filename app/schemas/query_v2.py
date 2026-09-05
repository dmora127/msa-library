"""POST /v2/query schemas: v1 plus the opt-in similarity search.

v1 (app/schemas/query.py) is untouched; these subclass it so the two stay
diffable until v2 is merged back. Wire-format rules:

  * a v2 MISS is byte-identical to a v1 miss (`match_type` is omitted when
    None, so `{"seq_hash", "found": false, "hits": []}` is all there is);
  * a v2 EXACT hit gains exactly one key, `"match_type": "exact"`, and no
    `similarity` key on its hits;
  * a v2 SIMILAR result has `found: true`, `match_type: "similar"`, and every
    hit carries `similarity` with the MMseqs statistics. `result.seq_hash` is
    still the QUERY's hash; `hit.seq_hash` is the neighbour's.

Units: percent_identity 0-100, coverages 0-1 (never mixed).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.query import QueryHit, QueryRequest
from app.services.similarity.models import SimilarityHit


class QueryRequestV2(QueryRequest):
    similarity_search: bool = Field(
        default=False,
        description=(
            "On an exact miss, run an MMseqs2 similarity search over cached "
            "sequences and return near-identical neighbours. Requires 'sequence'."
        ),
    )

    @model_validator(mode="after")
    def _similarity_needs_sequence(self) -> QueryRequestV2:
        if self.similarity_search and self.seq_hashes:
            raise ValueError("similarity_search requires 'sequence' (not 'seq_hashes')")
        return self


class SimilarityStats(BaseModel):
    percent_identity: float  # 0-100
    query_coverage: float  # 0-1, fraction of the query aligned
    target_coverage: float  # 0-1, fraction of the cached sequence aligned
    alignment_length: int
    query_length: int
    target_length: int
    bitscore: float
    evalue: float
    source_database: Literal["ephemeral", "nightly", "delta"]

    @classmethod
    def from_hit(cls, hit: SimilarityHit) -> SimilarityStats:
        data = asdict(hit)
        data.pop("seq_hash")
        return cls(**data)


class QueryHitV2(QueryHit):
    # Omitted from JSON on exact hits so v1 clients reading v2 see no new key.
    similarity: SimilarityStats | None = Field(default=None, exclude_if=lambda v: v is None)


class QuerySeqResultV2(BaseModel):
    seq_hash: str
    found: bool
    # None on a miss (omitted), "exact" or "similar" otherwise.
    match_type: Literal["exact", "similar"] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    hits: list[QueryHitV2]


class QueryResponseV2(BaseModel):
    results: list[QuerySeqResultV2]
