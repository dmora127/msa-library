"""Target-database builders: per-request ephemeral, nightly, and delta.

Snapshot-mode layout under SIMILARITY_DB_ROOT:

    nightly-build-20260903T020000Z-DB/   targetDB* + manifest.json  (indexed)
    delta-20260903T030000Z-DB/           targetDB* + manifest.json  (unindexed)
    nightly-buildDB  -> nightly-build-20260903T020000Z-DB      (symlink)
    deltaDB          -> delta-20260903T030000Z-DB              (symlink)

Every generation is immutable once published. Publication is an atomic symlink
swap (temp link + os.replace), so there is never a moment without an active
generation, and a failed build leaves the active one untouched. Old generations
are removed only after a retention window, never the active target.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import SimilarityBuildFailed
from app.core.metrics import record_similarity_stage
from app.services.similarity.database import (
    DumpStats,
    capture_snapshot,
    dump_active_targets,
    dump_delta_targets,
    set_isolation,
)
from app.services.similarity.mmseqs import MmseqsError, MmseqsRunner
from app.services.similarity.models import (
    BuildType,
    Manifest,
    SimilarityConfig,
    app_git_commit,
    read_manifest,
    write_manifest,
)

log = logging.getLogger("af3cache.similarity.build")

NIGHTLY_LINK = "nightly-buildDB"
DELTA_LINK = "deltaDB"
TARGET_DB = "targetDB"
MANIFEST = "manifest.json"
TARGET_FASTA = "target-sequences.fa"

_PREFIX: dict[str, str] = {"nightly": "nightly-build", "delta": "delta"}
_GEN_RE = re.compile(r"^(nightly-build|delta)-(\d{8}T\d{6}Z)-DB$")
_TS_FMT = "%Y%m%dT%H%M%SZ"


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- generation naming / symlinks --------------------------------------------


def generation_name(build_type: BuildType, ts: datetime) -> str:
    return f"{_PREFIX[build_type]}-{ts.astimezone(UTC).strftime(_TS_FMT)}-DB"


def parse_generation_ts(name: str) -> datetime | None:
    m = _GEN_RE.match(name)
    if not m:
        return None
    return datetime.strptime(m.group(2), _TS_FMT).replace(tzinfo=UTC)


def resolve_generation(root: Path, link: str) -> Path | None:
    """Dereference the active symlink ONCE. Callers keep the returned concrete
    path for the whole request so a promotion mid-request cannot mix files
    from two generations. None if the link is missing or dangling."""
    try:
        return (root / link).resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def promote(root: Path, link: str, gen_dir: Path) -> None:
    """Atomically point `root/link` at `gen_dir`: create a temp symlink in the
    same directory, then os.replace() it over the live name. Never
    unlink-then-create (that leaves a window with no active generation)."""
    tmp = root / f".{link}.tmp-{uuid.uuid4().hex}"
    os.symlink(gen_dir.name, tmp)  # relative target: root can be moved/mounted elsewhere
    try:
        os.replace(tmp, root / link)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def cleanup_generations(
    root: Path,
    build_type: BuildType,
    *,
    keep: set[Path],
    older_than: timedelta,
    now: datetime,
) -> list[Path]:
    """Delete generation dirs of `build_type` whose timestamp is older than the
    retention window, except anything in `keep` (resolved paths). Also sweeps
    orphaned temp symlinks. Returns the removed paths."""
    prefix = _PREFIX[build_type]
    removed: list[Path] = []
    keep_resolved = {p.resolve() for p in keep}
    for child in sorted(root.iterdir()):
        if child.is_symlink():
            if child.name.startswith(".") and ".tmp-" in child.name:
                try:
                    age = now.timestamp() - child.lstat().st_mtime
                except OSError:
                    continue
                if age > older_than.total_seconds():
                    with contextlib.suppress(OSError):
                        child.unlink()
            continue
        m = _GEN_RE.match(child.name)
        if not m or m.group(1) != prefix or not child.is_dir():
            continue
        ts = parse_generation_ts(child.name)
        if ts is None or now - ts <= older_than:
            continue
        if child.resolve() in keep_resolved:
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed.append(child)
    return removed


def _validate_target_db(db: Path) -> None:
    for suffix in ("", ".index", ".dbtype"):
        p = Path(str(db) + suffix)
        if not p.exists():
            raise SimilarityBuildFailed(f"mmseqs createdb produced no {p.name}")


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _mkdirs(*paths: Path) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


# --- ephemeral ---------------------------------------------------------------


class EphemeralTargetBuilder:
    """Build a complete target DB for ONE request inside its private workdir.
    The caller owns (and deletes) the workdir."""

    def __init__(self, runner: MmseqsRunner, *, index: bool = True):
        self.runner = runner
        self.index = index

    async def build(self, session: AsyncSession, workdir: Path) -> tuple[Path | None, DumpStats]:
        fasta = workdir / TARGET_FASTA
        t0 = time.monotonic()
        stats = await dump_active_targets(session, fasta)
        record_similarity_stage("dump", time.monotonic() - t0)
        if stats.sequence_count == 0:
            return None, stats
        db = workdir / TARGET_DB
        await self.runner.createdb(fasta, db)
        if self.index:
            await self.runner.createindex(db, workdir / "tmp_index")
        return db, stats


# --- nightly / delta ---------------------------------------------------------


class _GenerationBuilder:
    build_type: BuildType
    link: str

    def __init__(
        self,
        session: AsyncSession,
        runner: MmseqsRunner,
        cfg: SimilarityConfig,
        *,
        now: Callable[[], datetime] = _utcnow,
    ):
        self.session = session
        self.runner = runner
        self.cfg = cfg
        self.now = now

    async def build(self) -> tuple[Manifest, list[Path]]:
        root = self.cfg.db_root
        await asyncio.to_thread(_mkdirs, root)
        ts = self.now()
        gen = root / generation_name(self.build_type, ts)
        if gen.exists():
            raise SimilarityBuildFailed(f"generation {gen.name} already exists")
        await asyncio.to_thread(_mkdirs, gen, gen / "tmp")
        try:
            manifest = await self._populate(gen, ts)
        except MmseqsError as e:
            await asyncio.to_thread(_rmtree, gen)
            raise SimilarityBuildFailed(
                f"{self.build_type} build failed at mmseqs {e.step} ({e.reason})"
            ) from e
        except SimilarityBuildFailed:
            await asyncio.to_thread(_rmtree, gen)
            raise
        except Exception as e:
            await asyncio.to_thread(_rmtree, gen)
            raise SimilarityBuildFailed(
                f"{self.build_type} build failed: {type(e).__name__}"
            ) from e
        except asyncio.CancelledError:
            await asyncio.to_thread(_rmtree, gen)
            raise
        # Only after everything above succeeded does the active link move.
        await asyncio.to_thread(promote, root, self.link, gen)
        log.info(
            "similarity %s build: promoted %s (%d sequences, %d residues, indexed=%s)",
            self.build_type, gen.name, manifest.sequence_count,
            manifest.residue_count, manifest.indexed,
        )
        retention = (
            self.cfg.nightly_retention if self.build_type == "nightly" else self.cfg.delta_retention
        )
        removed = await asyncio.to_thread(
            cleanup_generations, root, self.build_type,
            keep={gen}, older_than=retention, now=self.now(),
        )
        for r in removed:
            log.info("similarity %s build: retired %s", self.build_type, r.name)
        return manifest, removed

    async def _populate(self, gen: Path, ts: datetime) -> Manifest:  # pragma: no cover
        raise NotImplementedError

    async def _finish_files(self, gen: Path) -> None:
        # Drop the dump + mmseqs scratch; only targetDB* and the manifest stay.
        await asyncio.to_thread(_rmtree, gen / "tmp")
        with contextlib.suppress(OSError):
            await asyncio.to_thread(os.unlink, gen / TARGET_FASTA)


class NightlyBuilder(_GenerationBuilder):
    build_type: BuildType = "nightly"
    link = NIGHTLY_LINK

    async def _populate(self, gen: Path, ts: datetime) -> Manifest:
        await set_isolation(self.session, self.cfg.build_isolation_level)
        snap = await capture_snapshot(self.session)
        fasta = gen / TARGET_FASTA
        t0 = time.monotonic()
        stats = await dump_active_targets(self.session, fasta)
        record_similarity_stage("dump", time.monotonic() - t0)
        if stats.sequence_count == 0:
            raise SimilarityBuildFailed("no active sequences to build a nightly from")
        db = gen / TARGET_DB
        await self.runner.createdb(fasta, db)
        _validate_target_db(db)
        await self.runner.createindex(db, gen / "tmp")
        await self._finish_files(gen)
        version = await self.runner.version()
        manifest = Manifest(
            build_type="nightly",
            generation=gen.name,
            created_at=ts.isoformat(),
            snapshot_taken_at=snap.taken_at.isoformat(),
            postgres_high_water_mark=snap.high_water_mark,
            sequence_count=stats.sequence_count,
            residue_count=stats.residue_count,
            indexed=True,
            mmseqs_version=version,
            app_git_commit=app_git_commit(),
        )
        await asyncio.to_thread(write_manifest, gen / MANIFEST, manifest)
        return manifest


class DeltaBuilder(_GenerationBuilder):
    build_type: BuildType = "delta"
    link = DELTA_LINK

    async def _populate(self, gen: Path, ts: datetime) -> Manifest:
        root = self.cfg.db_root
        base_dir = resolve_generation(root, NIGHTLY_LINK)
        if base_dir is None:
            raise SimilarityBuildFailed(
                "no active nightly generation; run the nightly build first"
            )
        base = await asyncio.to_thread(read_manifest, base_dir / MANIFEST)
        if base is None:
            raise SimilarityBuildFailed(f"active nightly {base_dir.name} has no readable manifest")
        await set_isolation(self.session, self.cfg.build_isolation_level)
        snap = await capture_snapshot(self.session)
        since = datetime.fromisoformat(base.snapshot_taken_at) - self.cfg.delta_overlap
        fasta = gen / TARGET_FASTA
        t0 = time.monotonic()
        stats = await dump_delta_targets(
            self.session, fasta, hwm=base.postgres_high_water_mark, since=since
        )
        record_similarity_stage("dump", time.monotonic() - t0)
        if stats.sequence_count > 0:
            db = gen / TARGET_DB
            await self.runner.createdb(fasta, db)
            _validate_target_db(db)
        # An empty delta is valid (e.g. right after a nightly): publish a
        # generation with a manifest but no targetDB; searches skip it.
        await self._finish_files(gen)
        version = await self.runner.version()
        manifest = Manifest(
            build_type="delta",
            generation=gen.name,
            created_at=ts.isoformat(),
            snapshot_taken_at=snap.taken_at.isoformat(),
            postgres_high_water_mark=snap.high_water_mark,
            sequence_count=stats.sequence_count,
            residue_count=stats.residue_count,
            indexed=False,
            mmseqs_version=version,
            app_git_commit=app_git_commit(),
            base_nightly_generation=base_dir.name,
            base_high_water_mark=base.postgres_high_water_mark,
            base_snapshot_taken_at=base.snapshot_taken_at,
        )
        await asyncio.to_thread(write_manifest, gen / MANIFEST, manifest)
        return manifest


# Spec-named entry points.


async def build_nightly_similarity_database(
    session: AsyncSession, runner: MmseqsRunner, cfg: SimilarityConfig
) -> tuple[Manifest, list[Path]]:
    return await NightlyBuilder(session, runner, cfg).build()


async def refresh_delta_similarity_database(
    session: AsyncSession, runner: MmseqsRunner, cfg: SimilarityConfig
) -> tuple[Manifest, list[Path]]:
    return await DeltaBuilder(session, runner, cfg).build()
