"""Admin config + source schemas. Never includes API keys (those are env-only).

Source enablement is no longer an app_settings value — it lives on
msa_sources.enabled and is managed via the /v1/admin/sources endpoints.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


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
    enabled: bool


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
