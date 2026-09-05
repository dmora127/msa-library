"""query_similarity(): mode dispatch, concurrency gate, per-request workspace.

Nothing here knows which HTTP route called it. The route hands in a normalized
sequence and gets back filtered, deduplicated, ranked SimilarityHit objects
keyed by seq_hash, which it resolves to metadata through the SAME lookup the
exact-match path uses.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    SimilarityBuildDisabled,
    SimilarityBuildInProgress,
    SimilarityDisabled,
    SimilarityUnavailable,
)
from app.core.hashing import sequence_hash
from app.core.metrics import (
    SIMILARITY_INFLIGHT,
    SIMILARITY_WAITING,
    record_similarity,
    record_similarity_build,
)
from app.services.similarity.builders import (
    DELTA_LINK,
    MANIFEST,
    NIGHTLY_LINK,
    TARGET_DB,
    DeltaBuilder,
    EphemeralTargetBuilder,
    NightlyBuilder,
    resolve_generation,
)
from app.services.similarity.mmseqs import MmseqsError, MmseqsRunner, parse_results
from app.services.similarity.models import (
    Manifest,
    SimilarityConfig,
    SimilarityHit,
    SourceDatabase,
    read_manifest,
)
from app.services.similarity.ranking import select_hits

log = logging.getLogger("af3cache.similarity")

_WORKDIR_PREFIX = "query_similarity_"


class SimilarityRuntime:
    """Process singleton (lru_cache'd in deps): frozen config, the mmseqs
    runner, the request semaphore and the build lock."""

    def __init__(self, cfg: SimilarityConfig):
        self.cfg = cfg
        self.runner = MmseqsRunner(
            binary=cfg.mmseqs_binary,
            threads=cfg.mmseqs_threads,
            prefilter_mode=cfg.mmseqs_prefilter_mode,
            alignment_mode=cfg.mmseqs_alignment_mode,
            max_candidates=cfg.mmseqs_max_candidates,
            split_memory_limit=cfg.mmseqs_split_memory_limit,
            # Hand mmseqs slightly looser cut-offs than ours so it discards the
            # bulk cheaply while ranking.py stays the authoritative gate.
            min_seq_id=max(0.0, cfg.min_percent_identity / 100.0 - 0.01),
            min_cov=max(0.0, min(cfg.min_query_coverage, cfg.min_target_coverage) - 0.01),
            timeout=cfg.mmseqs_timeout_seconds,
        )
        self.build_runner = self.runner.with_timeout(cfg.build_timeout_seconds)
        self.thresholds = cfg.thresholds
        self.semaphore: asyncio.Semaphore | None = (
            asyncio.Semaphore(cfg.max_concurrent) if cfg.max_concurrent > 0 else None
        )
        self.build_lock = asyncio.Lock()

    @property
    def build_in_progress(self) -> bool:
        return self.build_lock.locked()


class SimilarityServiceBase(abc.ABC):
    @abc.abstractmethod
    async def query_similarity(self, sequence: str) -> list[SimilarityHit]: ...


class StubSimilarityService(SimilarityServiceBase):
    """Tests: canned hits or a canned error; records every sequence it saw so
    a test can assert the exact-hit path never invoked it."""

    def __init__(
        self, hits: list[SimilarityHit] | None = None, error: Exception | None = None
    ):
        self.hits = list(hits or [])
        self.error = error
        self.calls: list[str] = []

    async def query_similarity(self, sequence: str) -> list[SimilarityHit]:
        self.calls.append(sequence)
        if self.error is not None:
            raise self.error
        return list(self.hits)


@dataclass(frozen=True)
class BuildOutcome:
    manifest: Manifest
    retired: list[Path]
    elapsed_seconds: float


@dataclass(frozen=True)
class _SearchResult:
    hits: list[SimilarityHit]
    raw_count: int
    label: str  # metrics result label when hits is empty


class SimilarityService(SimilarityServiceBase):
    def __init__(self, session: AsyncSession, runtime: SimilarityRuntime):
        self.session = session
        self.rt = runtime

    # --- query path ----------------------------------------------------------

    async def query_similarity(self, sequence: str) -> list[SimilarityHit]:
        cfg = self.rt.cfg
        if not cfg.search_enabled:
            record_similarity("disabled")
            raise SimilarityDisabled("similarity search is disabled on this deployment")
        t0 = time.monotonic()
        await self._acquire_slot()
        SIMILARITY_INFLIGHT.inc()
        try:
            if cfg.db_mode == "snapshot":
                res = await self._search_snapshot(sequence)
            else:
                res = await self._search_ephemeral(sequence)
        except MmseqsError as e:
            elapsed = time.monotonic() - t0
            record_similarity(e.reason, elapsed)
            log.warning("similarity: %s (%.2fs)", e, elapsed)
            raise SimilarityUnavailable(
                f"similarity search failed at mmseqs {e.step} ({e.reason})"
            ) from e
        finally:
            SIMILARITY_INFLIGHT.dec()
            if self.rt.semaphore is not None:
                self.rt.semaphore.release()
        elapsed = time.monotonic() - t0
        record_similarity("hit" if res.hits else res.label, elapsed)
        log.info(
            "similarity: mode=%s qlen=%d qhash=%s raw=%d returned=%d %.2fs",
            cfg.db_mode, len(sequence), sequence_hash(sequence, already_normalized=True)[:12],
            res.raw_count, len(res.hits), elapsed,
        )
        return res.hits

    async def _acquire_slot(self) -> None:
        sem = self.rt.semaphore
        if sem is None:
            return
        SIMILARITY_WAITING.inc()
        try:
            async with asyncio.timeout(self.rt.cfg.queue_timeout_seconds):
                await sem.acquire()
        except TimeoutError as e:
            record_similarity("queue_timeout")
            raise SimilarityUnavailable("similarity queue full (retryable)") from e
        finally:
            SIMILARITY_WAITING.dec()

    async def _workdir(self) -> tempfile.TemporaryDirectory:
        cfg = self.rt.cfg
        return await asyncio.to_thread(
            tempfile.TemporaryDirectory,
            prefix=_WORKDIR_PREFIX,
            dir=str(cfg.tmp_root) if cfg.tmp_root else None,
        )

    async def _search_ephemeral(self, sequence: str) -> _SearchResult:
        cfg = self.rt.cfg
        tmp = await self._workdir()
        work = Path(tmp.name)
        try:
            builder = EphemeralTargetBuilder(self.rt.runner, index=cfg.ephemeral_index)
            target_db, stats = await builder.build(self.session, work)
            log.info(
                "similarity ephemeral: targets=%d residues=%d indexed=%s workdir=%s",
                stats.sequence_count, stats.residue_count, cfg.ephemeral_index, work.name,
            )
            if target_db is None:
                return _SearchResult([], 0, "empty_targets")
            raw = await self._search_against(work, sequence, target_db, "ephemeral")
            return _SearchResult(select_hits(raw, self.rt.thresholds), len(raw), "no_hit")
        finally:
            # Runs on success, no hits, Postgres error, mmseqs error, parse
            # error and cancellation alike.
            await asyncio.to_thread(tmp.cleanup)
            log.debug("similarity ephemeral: removed workdir %s", work.name)

    async def _search_snapshot(self, sequence: str) -> _SearchResult:
        root = self.rt.cfg.db_root
        # Resolve BOTH links once, before any mmseqs call, and use the concrete
        # generation dirs for the rest of the request.
        nightly = resolve_generation(root, NIGHTLY_LINK)
        if nightly is None or not (nightly / TARGET_DB).exists():
            record_similarity("db_missing")
            raise SimilarityUnavailable(
                "no active nightly similarity database (run the nightly build first)"
            )
        delta = resolve_generation(root, DELTA_LINK)
        delta_manifest = read_manifest(delta / MANIFEST) if delta is not None else None
        use_delta = (
            delta is not None
            and delta_manifest is not None
            and delta_manifest.sequence_count > 0
            and (delta / TARGET_DB).exists()
        )
        if delta_manifest is not None and delta_manifest.base_nightly_generation != nightly.name:
            log.warning(
                "similarity snapshot: delta %s was built against %s, active nightly is %s "
                "(harmless until the next delta refresh)",
                delta.name if delta else "?", delta_manifest.base_nightly_generation,
                nightly.name,
            )
        tmp = await self._workdir()
        work = Path(tmp.name)
        try:
            raw = await self._search_against(work, sequence, nightly / TARGET_DB, "nightly")
            n_nightly = len(raw)
            n_delta = 0
            if use_delta and delta is not None:
                delta_hits = await self._search_against(work, sequence, delta / TARGET_DB, "delta")
                n_delta = len(delta_hits)
                raw += delta_hits
            log.info(
                "similarity snapshot: nightly=%s (%d raw) delta=%s (%d raw)",
                nightly.name, n_nightly, delta.name if use_delta and delta else "none", n_delta,
            )
            return _SearchResult(select_hits(raw, self.rt.thresholds), len(raw), "no_hit")
        finally:
            await asyncio.to_thread(tmp.cleanup)

    async def _search_against(
        self, work: Path, sequence: str, target_db: Path, source: SourceDatabase
    ) -> list[SimilarityHit]:
        runner = self.rt.runner
        query_fa = work / f"{source}.query.fa"
        query_db = work / f"{source}.queryDB"
        result_db = work / f"{source}.resultDB"
        out = work / f"{source}.m8"
        await asyncio.to_thread(query_fa.write_text, f">query\n{sequence}\n")
        await runner.createdb(query_fa, query_db)
        await runner.search(query_db, target_db, result_db, work / f"tmp_{source}")
        await runner.convertalis(query_db, target_db, result_db, out)
        text = await asyncio.to_thread(out.read_text)
        return parse_results(text, source_database=source)

    # --- admin / builds -------------------------------------------------------

    async def status(self) -> dict:
        cfg = self.rt.cfg
        root = cfg.db_root
        out: dict = {
            "search_enabled": cfg.search_enabled,
            "db_mode": cfg.db_mode,
            "db_root": str(root),
            "nightly_build_enabled": cfg.nightly_build_enabled,
            "delta_build_enabled": cfg.delta_build_enabled,
            "delta_refresh_minutes": cfg.delta_refresh_minutes,
            "mmseqs_binary": cfg.mmseqs_binary,
            "mmseqs_version": None,
            "thresholds": {
                "min_percent_identity": cfg.min_percent_identity,
                "min_query_coverage": cfg.min_query_coverage,
                "min_target_coverage": cfg.min_target_coverage,
                "max_hits": cfg.max_hits,
                "max_candidates": cfg.mmseqs_max_candidates,
            },
            "nightly": None,
            "delta": None,
            "build_in_progress": self.rt.build_in_progress,
        }
        try:
            out["mmseqs_version"] = await self.rt.runner.version()
        except MmseqsError as e:
            log.warning("similarity status: mmseqs version unavailable: %s", e)
        for key, link in (("nightly", NIGHTLY_LINK), ("delta", DELTA_LINK)):
            gen = resolve_generation(root, link)
            if gen is None:
                continue
            m = await asyncio.to_thread(read_manifest, gen / MANIFEST)
            if m is not None:
                out[key] = {"generation_path": str(gen), **m.__dict__}
        return out

    async def build_nightly(self) -> BuildOutcome:
        if not self.rt.cfg.nightly_build_enabled:
            record_similarity_build("nightly", "disabled")
            raise SimilarityBuildDisabled("SIMILARITY_NIGHTLY_BUILD_ENABLED is false")
        builder = NightlyBuilder(self.session, self.rt.build_runner, self.rt.cfg)
        return await self._run_build("nightly", builder.build)

    async def refresh_delta(self) -> BuildOutcome:
        if not self.rt.cfg.delta_build_enabled:
            record_similarity_build("delta", "disabled")
            raise SimilarityBuildDisabled("SIMILARITY_DELTA_BUILD_ENABLED is false")
        builder = DeltaBuilder(self.session, self.rt.build_runner, self.rt.cfg)
        return await self._run_build("delta", builder.build)

    async def _run_build(self, build_type: str, fn) -> BuildOutcome:
        lock = self.rt.build_lock
        if lock.locked():
            record_similarity_build(build_type, "in_progress")
            raise SimilarityBuildInProgress("another similarity build is running")
        async with lock:
            t0 = time.monotonic()
            try:
                manifest, retired = await fn()
            except Exception:
                record_similarity_build(build_type, "failed", time.monotonic() - t0)
                raise
            elapsed = time.monotonic() - t0
            record_similarity_build(build_type, "ok", elapsed)
            return BuildOutcome(manifest=manifest, retired=retired, elapsed_seconds=elapsed)
