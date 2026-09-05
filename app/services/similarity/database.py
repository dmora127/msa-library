"""Postgres -> FASTA streaming for the MMseqs target databases.

`msa_sequences` is the authoritative source, restricted to sequences that have
at least one ACTIVE `msa_cache_entries` row, so every similarity hit resolves
to metadata the query endpoint can actually serve. Rows are streamed through a
server-side cursor and appended to the FASTA in chunks; the full table is never
materialised in memory.

FASTA header is the bare seq_hash, so `mmseqs convertalis`'s `target` column
round-trips it verbatim.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("af3cache.similarity.db")

_ACTIVE_PREDICATE = """
    s.molecule_type = 'protein'
    and exists (select 1
                  from public.msa_cache_entries e
                 where e.sequence_id = s.id
                   and e.status = 'active')
"""

# Ephemeral and nightly dumps. No ORDER BY: mmseqs does not care and a 500k-row
# sort buys nothing.
ACTIVE_TARGETS_SQL = f"""
select s.seq_hash, s.sequence
  from public.msa_sequences s
 where {_ACTIVE_PREDICATE}
"""

# Delta dump: everything "newer than the active nightly". Three ways a
# sequence qualifies, all relative to the nightly manifest (never the previous
# delta, so each refresh is a full rebuild):
#   * its id is past the nightly high-water mark;
#   * it was inserted after the snapshot minus the overlap (bigserial ids are
#     handed out at INSERT, so a slow transaction can commit a LOW id late);
#   * an old sequence newly gained an active entry (publish/upsert touches
#     created_at/updated_at on msa_cache_entries).
DELTA_TARGETS_SQL = f"""
select s.seq_hash, s.sequence
  from public.msa_sequences s
 where {_ACTIVE_PREDICATE}
   and (   s.id > :hwm
        or s.created_at > :since
        or exists (select 1
                     from public.msa_cache_entries e2
                    where e2.sequence_id = s.id
                      and e2.status = 'active'
                      and (e2.created_at > :since or e2.updated_at > :since)))
"""

# Captured in the SAME transaction as the nightly dump, immediately before it.
SNAPSHOT_SQL = """
select coalesce(max(id), 0) as high_water_mark, now() as taken_at
  from public.msa_sequences
"""

_ISOLATION_LEVELS = {"READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"}
_CHUNK_ROWS = 5000


@dataclass(frozen=True)
class DumpStats:
    sequence_count: int
    residue_count: int


@dataclass(frozen=True)
class Snapshot:
    high_water_mark: int
    taken_at: datetime  # tz-aware, Postgres now() = transaction start


async def set_isolation(session: AsyncSession, level: str | None) -> None:
    """Issue SET TRANSACTION ISOLATION LEVEL as the FIRST statement of the
    session's transaction. Postgres refuses it after any query, which is the
    loud failure we want if a caller sneaks a query in first. None = skip
    (tests: the harness binds the session to an already-open transaction)."""
    if level is None:
        return
    lvl = level.upper()
    if lvl not in _ISOLATION_LEVELS:
        raise ValueError(f"unsupported isolation level {level!r}")
    await session.execute(text(f"set transaction isolation level {lvl}"))


async def capture_snapshot(session: AsyncSession) -> Snapshot:
    row = (await session.execute(text(SNAPSHOT_SQL))).one()
    return Snapshot(high_water_mark=int(row.high_water_mark), taken_at=row.taken_at)


async def dump_active_targets(session: AsyncSession, fasta_path: Path) -> DumpStats:
    return await _stream_fasta(session, ACTIVE_TARGETS_SQL, {}, fasta_path)


async def dump_delta_targets(
    session: AsyncSession, fasta_path: Path, *, hwm: int, since: datetime
) -> DumpStats:
    return await _stream_fasta(
        session, DELTA_TARGETS_SQL, {"hwm": hwm, "since": since}, fasta_path
    )


def _append(path: Path, chunk: str) -> None:
    with path.open("a", encoding="ascii") as fh:
        fh.write(chunk)


def _touch(path: Path) -> None:
    path.write_text("")


async def _stream_fasta(
    session: AsyncSession, sql: str, params: dict, fasta_path: Path
) -> DumpStats:
    # Always create the file, even for zero rows, so callers can rely on it.
    await asyncio.to_thread(_touch, fasta_path)
    stmt = text(sql).execution_options(yield_per=_CHUNK_ROWS)
    result = await session.stream(stmt, params)
    count = 0
    residues = 0
    skipped = 0
    try:
        async for partition in result.partitions(_CHUNK_ROWS):
            buf: list[str] = []
            for seq_hash, sequence in partition:
                if not sequence:
                    # The schema says NOT NULL, but an empty string would make
                    # mmseqs choke on the whole file; drop and identify the row.
                    skipped += 1
                    log.warning("similarity dump: empty sequence for %s, skipped", seq_hash)
                    continue
                buf.append(f">{seq_hash}\n{sequence}\n")
                count += 1
                residues += len(sequence)
            if buf:
                await asyncio.to_thread(_append, fasta_path, "".join(buf))
    finally:
        await result.close()
    if skipped:
        log.warning("similarity dump: %d rows skipped", skipped)
    return DumpStats(sequence_count=count, residue_count=residues)
