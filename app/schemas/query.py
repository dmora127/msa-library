"""Query request/response schemas.

A job sends either one raw `sequence` (server normalizes + hashes) or a batch
of pre-computed `seq_hashes`. Exactly one must be provided. "No hits" is an
explicit, machine-checkable signal: HTTP 200 with `found=false` and an empty
`hits` list — never a 404.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from app.core.hashing import is_valid_seq_hash


class QueryRequest(BaseModel):
    sequence: str | None = Field(default=None, description="Raw amino-acid sequence")
    seq_hashes: list[str] | None = Field(
        default=None, description="Pre-normalized SHA256 hex hashes"
    )
    sources: list[str] | None = Field(
        default=None, description="Restrict to these sources; None = all enabled"
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> QueryRequest:
        has_seq = bool(self.sequence and self.sequence.strip())
        has_hashes = bool(self.seq_hashes)
        if has_seq == has_hashes:
            raise ValueError("provide exactly one of 'sequence' or 'seq_hashes'")
        if has_hashes:
            bad = [h for h in self.seq_hashes or [] if not is_valid_seq_hash(h)]
            if bad:
                raise ValueError(f"invalid seq_hash(es): {bad}")
        return self


class QueryHit(BaseModel):
    seq_hash: str
    source: str
    osdf_uri: str
    db_version: str
    species: str = "Unclassified"
    query_name: str = "Unclassified"
    a3m_size_bytes: int | None = None
    a3m_sha256: str | None = None
    n_sequences: int | None = None
    seq_length: int | None = None


class QuerySeqResult(BaseModel):
    seq_hash: str
    found: bool
    hits: list[QueryHit]


class QueryResponse(BaseModel):
    results: list[QuerySeqResult]
