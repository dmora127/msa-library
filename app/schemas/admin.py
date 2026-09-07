"""Admin config + source schemas. Never includes API keys (those are env-only).

Source enablement is no longer an app_settings value — it lives on
msa_sources.enabled and is managed via the /v1/admin/sources endpoints.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AdminConfig(BaseModel):
    max_upload_size_bytes: int
    token_ttl_seconds: int
    osdf_namespace_template: str
    reap_grace_hours: int


class AdminConfigUpdate(BaseModel):
    max_upload_size_bytes: int | None = Field(default=None, gt=0)
    token_ttl_seconds: int | None = Field(default=None, gt=0)
    osdf_namespace_template: str | None = None
    reap_grace_hours: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check(self) -> AdminConfigUpdate:
        if all(
            v is None
            for v in (
                self.max_upload_size_bytes,
                self.token_ttl_seconds,
                self.osdf_namespace_template,
                self.reap_grace_hours,
            )
        ):
            raise ValueError("provide at least one field to update")
        if self.osdf_namespace_template is not None and any(
            ph not in self.osdf_namespace_template
            for ph in ("{namespace}", "{seq_hash}", "{source_slug}", "{ingestion_id}")
        ):
            raise ValueError(
                "osdf_namespace_template must contain {namespace}, {seq_hash}, "
                "{source_slug} and {ingestion_id}"
            )
        return self


class SourceInfo(BaseModel):
    name: str
    enabled: bool
    idle_ttl_days: int | None
    notes: str | None


class SourceUpdate(BaseModel):
    """Partial update for a source row.

    extra="forbid" is deliberate. This model previously accepted only `enabled`
    and Pydantic silently DISCARDED everything else, so a request carrying
    idle_ttl_days got HTTP 200 and a response echoing the UNCHANGED value --
    indistinguishable from success. Unknown fields now fail loudly with a 422.

    Every field is optional and only those actually PRESENT in the request body
    are written (see model_fields_set in the route). That matters for
    idle_ttl_days, where null is a meaningful value ("never cull", which is what
    the other seven sources use) and must be distinguishable from "not supplied".

    `name` is accepted only for convenience so a GET response can be edited and
    PUT straight back; it must match the path, and it is never used to rename.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    enabled: bool | None = None
    idle_ttl_days: int | None = Field(default=None, gt=0)
    notes: str | None = None


class ReclassifyRequest(BaseModel):
    """Single-item classification callback from the external BLAST pipeline.

    sequence_hash is the sha256 of the normalized sequence (the app/core/hashing
    contract) and must match msa_sequences.seq_hash for sequence_id — a safety
    cross-check so a stale or transposed id can't relabel the wrong sequence.
    Sentinel values ('Pending Classification', 'Unclassified') are rejected so
    a row can never be re-armed back to pending through this endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    sequence_id: int = Field(gt=0)
    sequence_hash: str
    species: str = Field(min_length=1, max_length=512)
    query_name: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def _check(self) -> ReclassifyRequest:
        from app.core.hashing import is_valid_seq_hash
        from app.services.classifier import PENDING_CLASSIFICATION, UNCLASSIFIED

        if not is_valid_seq_hash(self.sequence_hash):
            raise ValueError("sequence_hash must be 64-char lowercase hex")
        self.species = self.species.strip()
        self.query_name = self.query_name.strip()
        for v in (self.species, self.query_name):
            if not v or v in (PENDING_CLASSIFICATION, UNCLASSIFIED.species):
                raise ValueError(
                    "species/query_name must be a real classification, "
                    "not empty or a sentinel value"
                )
        return self


class ReclassifyResponse(BaseModel):
    sequence_id: int
    updated: int  # rows flipped from 'Pending Classification'; 0 is a valid no-op


class AdminIngestRequest(BaseModel):
    """Admin-only bulk ingest of an a3m at an osdf_uri the caller controls.

    Bypasses the community-only upload gate (admin asserts the source). The
    same verify+publish pipeline as upload-complete then runs (in pelican mode:
    fetch + sha/size/format/seq checks; in dryrun: trust declared metadata).

    osdf_uri is optional: omit it and the server mints one from
    osdf_namespace_template exactly like the job upload path does — i.e.
    osdf:///af3-msa-library/community-contributed/<msa_ingestions.id>.a3m —
    and returns it in the response, so bulk-seeded objects live at the same
    id-derived paths as normally uploaded ones. Pass it explicitly only when
    the object ALREADY sits at a path you don't control (e.g. mirrored EBI
    files), in which case it is used verbatim (no template/store pick).
    """

    sequence: str | None = None
    seq_hash: str | None = None
    seq_length: int | None = None
    molecule_type: str = "protein"
    source: str = Field(min_length=1)
    db_version: str = Field(min_length=1)
    osdf_uri: str | None = Field(default=None, min_length=1)
    declared_size_bytes: int = Field(gt=0)
    declared_sha256: str
    requester: str | None = None
    # Optional metadata the caller can supply directly (overrides what the
    # classifier / a3m would otherwise produce at publish). If both species and
    # query_name are given, the classifier is skipped entirely.
    species: str | None = None
    query_name: str | None = None
    n_sequences: int | None = None
    # Admin escape hatch: publish trusting the declared sha/size WITHOUT fetching
    # the object from OSDF (same as dryrun mode), even when OSDF_CLIENT_IMPL=pelican.
    # Lets the two-phase bulk pipeline POST before phase 2 has uploaded the object,
    # while the job upload flow keeps real fetch+verify. Admin-key gated.
    skip_verify: bool = False

    @model_validator(mode="after")
    def _check(self) -> AdminIngestRequest:
        from app.core.hashing import is_valid_seq_hash

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
        if self.molecule_type not in ("protein", "rna", "dna"):
            raise ValueError("molecule_type must be protein|rna|dna")
        return self


# --- similarity (mmseqs2) admin ---------------------------------------------


class SimilarityManifestInfo(BaseModel):
    """manifest.json of a published nightly/delta generation, plus where it
    lives on disk."""

    build_type: str
    generation: str
    generation_path: str
    created_at: str
    snapshot_taken_at: str
    postgres_high_water_mark: int
    sequence_count: int
    residue_count: int
    indexed: bool
    mmseqs_version: str
    app_git_commit: str | None = None
    target_filter: str = "active_cache_entries"
    base_nightly_generation: str | None = None
    base_high_water_mark: int | None = None
    base_snapshot_taken_at: str | None = None


class SimilarityStatusResponse(BaseModel):
    search_enabled: bool
    db_mode: str
    db_root: str
    nightly_build_enabled: bool
    delta_build_enabled: bool
    delta_refresh_minutes: int
    mmseqs_binary: str
    mmseqs_version: str | None  # None when the binary is missing/broken
    thresholds: dict[str, float | int]
    nightly: SimilarityManifestInfo | None
    delta: SimilarityManifestInfo | None
    build_in_progress: bool


class SimilarityBuildResponse(BaseModel):
    manifest: SimilarityManifestInfo
    elapsed_seconds: float
    retired: list[str]  # generation dirs removed by retention after promotion


# --- NR BLAST reclassification jobs -----------------------------------------


class ReclassJobCreate(BaseModel):
    """Recorded by helpers/reclassify_export.sh right after `reclass-submit`."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=128)
    input_name: str = Field(min_length=1, max_length=255)
    sequence_count: int = Field(ge=0)
    residue_count: int | None = Field(default=None, ge=0)
    input_bytes: int | None = Field(default=None, ge=0)


class ReclassRemoteStatus(BaseModel):
    """The raw JSON printed by `reclass-status <job>` on the BLAST box. Known
    fields are typed; anything else is kept (extra="allow") and stored verbatim
    in reclass_jobs.last_status."""

    model_config = ConfigDict(extra="allow")

    status: str
    job: str | None = None
    source: str | None = None
    bytes: int | None = None
    records: int | None = None
    submitted_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    results: str | None = None
    posted_so_far: int | None = None
    input_retained: bool | None = None
    summary: dict[str, Any] | None = None
    log_tail: list[str] | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _normalise_status(self) -> ReclassRemoteStatus:
        from app.services.reclass_jobs import STATES

        self.status = self.status.strip().upper()
        if self.status not in STATES:
            raise ValueError(f"status must be one of {', '.join(STATES)}")
        return self


class ReclassJobOut(BaseModel):
    job_id: str
    input_name: str
    sequence_count: int
    residue_count: int | None
    input_bytes: int | None
    status: str
    remote_submitted_at: datetime | None
    remote_started_at: datetime | None
    remote_finished_at: datetime | None
    exit_code: int | None
    posted_so_far: int | None
    summary: dict[str, Any] | None
    last_status: dict[str, Any] | None
    last_error: str | None
    poll_count: int
    last_polled_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ReclassSubmitRequest(BaseModel):
    """POST /v1/admin/classification/jobs/submit body (all optional)."""

    model_config = ConfigDict(extra="forbid")

    min_length: int | None = Field(default=None, ge=1)  # default: RECLASS_EXPORT_MIN_LENGTH
    max_length: int | None = Field(default=None, ge=1)
    limit: int | None = Field(default=None, ge=1)


class ReclassPolledJob(BaseModel):
    previous_status: str
    job: ReclassJobOut


class ReclassPollFailure(BaseModel):
    job_id: str
    error: str


class ReclassPollResponse(BaseModel):
    polled: list[ReclassPolledJob]
    failed: list[ReclassPollFailure]
