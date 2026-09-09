"""Upload-request / upload-complete schemas.

The job declares the .a3m's sha256 + size up front (so we can refuse an
oversize object *before* minting a write token), then calls back when the
upload finishes for authoritative re-verification.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.core.hashing import is_valid_seq_hash

DEFAULT_SOURCE = "Community Contributed - Cached"

# What POST /v1/uploads/{id}/complete answers on the first callback: the row is
# parked in DB status 'uploaded' and verification runs in the background. Only
# the response wording carries the "pending" part -- the status enum is not
# extended.
PENDING_VERIFICATION_STATUS = "uploaded - pending verification"


class UploadRequest(BaseModel):
    sequence: str | None = Field(default=None, description="Raw amino-acid sequence")
    seq_hash: str | None = Field(default=None, description="SHA256 hex (if no sequence)")
    seq_length: int | None = Field(
        default=None, description="Required only when seq_hash is given"
    )
    molecule_type: str = "protein"
    source: str = DEFAULT_SOURCE
    # The reference-DB bundle version the job used to compute this MSA. Same
    # seq+source with a different db_version is a distinct cached entry.
    db_version: str = Field(min_length=1)
    declared_size_bytes: int = Field(gt=0)
    declared_sha256: str
    requester: str | None = None

    @model_validator(mode="after")
    def _check(self) -> UploadRequest:
        has_seq = bool(self.sequence and self.sequence.strip())
        has_hash = bool(self.seq_hash)
        if has_seq == has_hash:
            raise ValueError("provide exactly one of 'sequence' or 'seq_hash'")
        if has_hash:
            if not is_valid_seq_hash(self.seq_hash or ""):
                raise ValueError("invalid seq_hash")
            if not self.seq_length or self.seq_length <= 0:
                raise ValueError("seq_length is required when seq_hash is given")
        if not is_valid_seq_hash(self.declared_sha256):
            raise ValueError("declared_sha256 must be 64-char lowercase hex")
        return self


class UploadRequestResponse(BaseModel):
    ingestion_id: str
    osdf_uri: str
    osdf_namespace: str
    write_token: str
    token_type: str = "Bearer"
    expires_at: datetime
    ttl_seconds: int
    max_upload_size_bytes: int


class UploadCompleteRequest(BaseModel):
    requester: str | None = None


class UploadCompleteResponse(BaseModel):
    ingestion_id: str
    # "uploaded - pending verification" on the first callback (verify runs in
    # the background); "available" / "rejected" (+ reject_reason) when a later
    # callback finds the row already verified; "uploaded" only from the dryrun
    # OSDF impl when it cannot publish without the sequence text.
    status: str
    reject_reason: str | None = None
    osdf_uri: str | None = None
