"""Similarity-search data types: hits, thresholds, generation manifests, config.

Units are fixed here and nowhere else: `percent_identity` is 0-100,
`query_coverage` / `target_coverage` are 0-1 fractions. The API schema
(app/schemas/query_v2.py) copies these fields verbatim.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from datetime import timedelta
from pathlib import Path
from typing import Literal

from app.core.config import Settings

log = logging.getLogger("af3cache.similarity")

SourceDatabase = Literal["ephemeral", "nightly", "delta"]
BuildType = Literal["nightly", "delta"]

DB_MODES = ("ephemeral", "snapshot")


@dataclass(frozen=True, slots=True)
class SimilarityHit:
    """One MMseqs alignment against a cached sequence, keyed by its seq_hash."""

    seq_hash: str
    percent_identity: float  # 0-100
    query_coverage: float  # 0-1: fraction of the QUERY covered by the alignment
    target_coverage: float  # 0-1: fraction of the TARGET covered by the alignment
    alignment_length: int
    query_length: int
    target_length: int
    bitscore: float
    evalue: float
    source_database: SourceDatabase


@dataclass(frozen=True)
class Thresholds:
    min_percent_identity: float
    min_query_coverage: float
    min_target_coverage: float
    max_hits: int


@dataclass(frozen=True)
class Manifest:
    """manifest.json inside every published nightly/delta generation dir.

    `postgres_high_water_mark` is max(msa_sequences.id) and `snapshot_taken_at`
    is Postgres now(), both captured in the SAME transaction as the dump. The
    delta builder reads them from the ACTIVE nightly's manifest to decide which
    rows are "newer than the nightly".
    """

    build_type: BuildType
    generation: str
    created_at: str  # ISO-8601 UTC
    snapshot_taken_at: str  # ISO-8601 UTC, from Postgres now()
    postgres_high_water_mark: int
    sequence_count: int
    residue_count: int
    indexed: bool
    mmseqs_version: str
    app_git_commit: str | None = None
    target_filter: str = "active_cache_entries"
    # delta only: the nightly this delta was computed against
    base_nightly_generation: str | None = None
    base_high_water_mark: int | None = None
    base_snapshot_taken_at: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        data = json.loads(text)
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def write_manifest(path: Path, manifest: Manifest) -> None:
    """Sync. Write-then-rename so a reader never sees a torn manifest."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(manifest.to_json())
    os.replace(tmp, path)


def read_manifest(path: Path) -> Manifest | None:
    """Sync. None when missing or unparseable (logged) -- callers treat that
    generation as unusable rather than failing the whole request."""
    try:
        return Manifest.from_json(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as e:
        log.warning("similarity: unreadable manifest %s: %s", path, e)
        return None


def app_git_commit() -> str | None:
    """Best effort; the image carries no .git so this is env-provided or None."""
    return os.environ.get("APP_GIT_COMMIT") or None


@dataclass(frozen=True)
class SimilarityConfig:
    """Snapshot of every similarity_* setting, plus two knobs tests override."""

    search_enabled: bool
    db_mode: str
    db_root: Path
    tmp_root: Path | None
    ephemeral_index: bool
    mmseqs_binary: str
    mmseqs_threads: int
    mmseqs_prefilter_mode: int
    mmseqs_alignment_mode: int
    mmseqs_max_candidates: int
    mmseqs_split_memory_limit: str | None
    mmseqs_timeout_seconds: float
    build_timeout_seconds: float
    min_percent_identity: float
    min_query_coverage: float
    min_target_coverage: float
    max_hits: int
    max_concurrent: int
    queue_timeout_seconds: float
    nightly_build_enabled: bool
    nightly_retention: timedelta
    delta_build_enabled: bool
    delta_refresh_minutes: int
    delta_retention: timedelta
    # "REPEATABLE READ" in production so the high-water mark and the dump share
    # one Postgres snapshot. Tests pass None: the harness binds the session to
    # an already-open transaction, where SET TRANSACTION is refused.
    build_isolation_level: str | None = "REPEATABLE READ"
    # Extra window the delta reaches back before the nightly snapshot. bigserial
    # ids are allocated at INSERT, not commit, so a row can commit after the
    # snapshot with an id below the high-water mark; overlap is harmless
    # because results are deduplicated by seq_hash.
    delta_overlap: timedelta = timedelta(minutes=15)

    @property
    def thresholds(self) -> Thresholds:
        return Thresholds(
            min_percent_identity=self.min_percent_identity,
            min_query_coverage=self.min_query_coverage,
            min_target_coverage=self.min_target_coverage,
            max_hits=self.max_hits,
        )


def build_similarity_config(settings: Settings) -> SimilarityConfig:
    """Validate + freeze the similarity settings (same late-validation style as
    build_classifier: bad values fail at first use, not at import)."""
    mode = settings.similarity_db_mode.lower()
    if mode not in DB_MODES:
        raise ValueError(
            f"unknown SIMILARITY_DB_MODE: {settings.similarity_db_mode!r} "
            f"(expected one of {', '.join(DB_MODES)})"
        )
    if not 0 <= settings.similarity_min_percent_identity <= 100:
        raise ValueError("SIMILARITY_MIN_PERCENT_IDENTITY must be within 0-100")
    for name in ("similarity_min_query_coverage", "similarity_min_target_coverage"):
        if not 0 <= getattr(settings, name) <= 1:
            raise ValueError(f"{name.upper()} must be a fraction within 0-1")
    if settings.similarity_max_hits < 1:
        raise ValueError("SIMILARITY_MAX_HITS must be >= 1")
    return SimilarityConfig(
        search_enabled=settings.similarity_search_enabled,
        db_mode=mode,
        db_root=Path(settings.similarity_db_root),
        tmp_root=Path(settings.similarity_tmp_root) if settings.similarity_tmp_root else None,
        ephemeral_index=settings.similarity_ephemeral_index,
        mmseqs_binary=settings.similarity_mmseqs_binary,
        mmseqs_threads=settings.similarity_mmseqs_threads,
        mmseqs_prefilter_mode=settings.similarity_mmseqs_prefilter_mode,
        mmseqs_alignment_mode=settings.similarity_mmseqs_alignment_mode,
        mmseqs_max_candidates=settings.similarity_mmseqs_max_candidates,
        mmseqs_split_memory_limit=settings.similarity_mmseqs_split_memory_limit,
        mmseqs_timeout_seconds=settings.similarity_mmseqs_timeout_seconds,
        build_timeout_seconds=settings.similarity_build_timeout_seconds,
        min_percent_identity=settings.similarity_min_percent_identity,
        min_query_coverage=settings.similarity_min_query_coverage,
        min_target_coverage=settings.similarity_min_target_coverage,
        max_hits=settings.similarity_max_hits,
        max_concurrent=settings.similarity_max_concurrent,
        queue_timeout_seconds=settings.similarity_queue_timeout_seconds,
        nightly_build_enabled=settings.similarity_nightly_build_enabled,
        nightly_retention=timedelta(minutes=settings.similarity_nightly_retention_minutes),
        delta_build_enabled=settings.similarity_delta_build_enabled,
        delta_refresh_minutes=settings.similarity_delta_refresh_minutes,
        delta_retention=timedelta(minutes=settings.similarity_delta_retention_minutes),
    )
