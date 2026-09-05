"""ORM models.

The read-path tables/view mirror tutorial-CHTC-AF3/plan.md exactly. The DB
objects themselves are created by Alembic migrations (incl. enum type, RLS,
view, grants) — these models map them for the app's writes/reads.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IngestionStatus(enum.StrEnum):
    pending = "pending"
    uploaded = "uploaded"
    verified = "verified"
    rejected = "rejected"
    available = "available"


class MsaSequence(Base):
    __tablename__ = "msa_sequences"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    seq_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    sequence: Mapped[str] = mapped_column(Text, nullable=False)
    seq_length: Mapped[int] = mapped_column(Integer, nullable=False)
    molecule_type: Mapped[str] = mapped_column(
        Text, nullable=False, default="protein", server_default="protein"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MsaCacheEntry(Base):
    __tablename__ = "msa_cache_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sequence_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("msa_sequences.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    osdf_uri: Mapped[str] = mapped_column(Text, nullable=False)
    a3m_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    a3m_sha256: Mapped[str | None] = mapped_column(Text)
    n_sequences: Mapped[int | None] = mapped_column(Integer)
    # The reference-DB bundle version this MSA was computed against. Part of
    # the entry identity: same seq+source, different db_version = distinct rows.
    db_version: Mapped[str] = mapped_column(Text, nullable=False)
    # Access-based eviction: bumped (coarsely) on query hits; a sweep flips
    # status active->stale; the reaper deletes the object after the grace window.
    last_accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="active", server_default="active"
    )
    stale_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Populated by the verification pipeline's Classifier (e.g. diamond blastp
    # against swissprot); 'Unclassified' when no confident top hit.
    species: Mapped[str] = mapped_column(
        Text, nullable=False, default="Unclassified", server_default="Unclassified"
    )
    query_name: Mapped[str] = mapped_column(
        Text, nullable=False, default="Unclassified", server_default="Unclassified"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MsaSource(Base):
    __tablename__ = "msa_sources"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    notes: Mapped[str | None] = mapped_column(Text)
    homepage_url: Mapped[str | None] = mapped_column(Text)
    license: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str | None] = mapped_column(Text)
    # NULL => never cull. Else a cache entry from this source goes stale after
    # N days WITHOUT a query hit (popularity-based, not time-since-upload).
    idle_ttl_days: Mapped[int | None] = mapped_column(Integer)
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MsaIngestion(Base):
    __tablename__ = "msa_ingestions"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    status: Mapped[IngestionStatus] = mapped_column(
        Enum(
            IngestionStatus,
            name="ingestion_status",
            create_type=False,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=IngestionStatus.pending,
    )
    seq_hash: Mapped[str] = mapped_column(Text, nullable=False)
    seq_length: Mapped[int] = mapped_column(Integer, nullable=False)
    molecule_type: Mapped[str] = mapped_column(
        Text, nullable=False, default="protein", server_default="protein"
    )
    source: Mapped[str] = mapped_column(
        Text, ForeignKey("msa_sources.name"), nullable=False
    )
    db_version: Mapped[str] = mapped_column(Text, nullable=False)
    # Normalized sequence the job declared at request time, persisted only when
    # the request supplied `sequence` (audit; also lets simulation publish a
    # queryable entry without an a3m fetch). NULL for seq_hash-only requests.
    requested_sequence: Mapped[str | None] = mapped_column(Text)
    declared_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    declared_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    observed_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    observed_sha256: Mapped[str | None] = mapped_column(Text)
    observed_n_sequences: Mapped[int | None] = mapped_column(Integer)
    osdf_uri: Mapped[str] = mapped_column(Text, nullable=False)
    token_ref: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    token_claim_jti: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    requester: Mapped[str | None] = mapped_column(Text)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    cache_entry_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("msa_cache_entries.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReclassJob(Base):
    """One external NR BLAST reclassification job (remote id as PK). Written
    by the admin endpoints under /v1/admin/classification/jobs; see 0015."""

    __tablename__ = "reclass_jobs"

    job_id: Mapped[str] = mapped_column(Text, primary_key=True)
    input_name: Mapped[str] = mapped_column(Text, nullable=False)
    sequence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    residue_count: Mapped[int | None] = mapped_column(BigInteger)
    input_bytes: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="QUEUED", server_default="QUEUED"
    )
    remote_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remote_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remote_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    posted_so_far: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[dict | None] = mapped_column(JSONB)
    last_status: Mapped[dict | None] = mapped_column(JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)
    poll_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_by: Mapped[str | None] = mapped_column(String)
