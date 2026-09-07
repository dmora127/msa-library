"""Pending-classification export for the external NR BLAST pipeline.

FASTA of every protein sequence with at least one ACTIVE msa_cache_entries row
carrying the 'Pending Classification' sentinel in BOTH species and query_name.
The header is `>{sequence_id}|{seq_hash}`: exactly what the remote reclass.sh
needs to call POST /v1/admin/reclassify (which cross-checks id against hash).

Rows are streamed through a server-side cursor in _CHUNK_ROWS partitions, so
the ~40 MB export never sits in memory. The count and the stream share one
predicate builder so `X-Sequence-Count` and the body agree (modulo writes
landing between the two statements; the count is advisory).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.classifier import PENDING_CLASSIFICATION

log = logging.getLogger("af3cache.classification_export")

_CHUNK_ROWS = 5000


@dataclass(frozen=True)
class ExportStats:
    sequence_count: int
    residue_count: int
    bytes: int

_PENDING_PREDICATE = """
    s.molecule_type = 'protein'
    and s.sequence <> ''
    and s.seq_length >= :min_length
    {max_clause}
    and exists (select 1
                  from public.msa_cache_entries e
                 where e.sequence_id = s.id
                   and e.status = 'active'
                   and e.species = :pending
                   and e.query_name = :pending)
"""

_COUNT_SQL = "select count(*) from public.msa_sequences s where {where}"
_TARGETS_SQL = (
    "select s.id, s.seq_hash, s.sequence from public.msa_sequences s "
    "where {where} order by s.id{limit_clause}"
)


def _build(
    template: str, *, min_length: int, max_length: int | None, limit: int | None
) -> tuple[str, dict]:
    where = _PENDING_PREDICATE.format(
        max_clause="and s.seq_length <= :max_length" if max_length is not None else ""
    )
    sql = template.format(where=where, limit_clause=" limit :limit" if limit is not None else "")
    params: dict = {"pending": PENDING_CLASSIFICATION, "min_length": min_length}
    if max_length is not None:
        params["max_length"] = max_length
    if limit is not None:
        params["limit"] = limit
    return sql, params


async def count_pending(
    session: AsyncSession,
    *,
    min_length: int,
    max_length: int | None = None,
    limit: int | None = None,
) -> int:
    sql, params = _build(_COUNT_SQL, min_length=min_length, max_length=max_length, limit=None)
    n = int((await session.execute(text(sql), params)).scalar_one())
    return min(n, limit) if limit is not None else n


async def iter_pending_fasta(
    session: AsyncSession,
    *,
    min_length: int,
    max_length: int | None = None,
    limit: int | None = None,
) -> AsyncIterator[bytes]:
    """Yield the FASTA in ASCII chunks of up to _CHUNK_ROWS records each."""
    sql, params = _build(_TARGETS_SQL, min_length=min_length, max_length=max_length, limit=limit)
    stmt = text(sql).execution_options(yield_per=_CHUNK_ROWS)
    result = await session.stream(stmt, params)
    count = 0
    residues = 0
    try:
        async for partition in result.partitions(_CHUNK_ROWS):
            buf = []
            for seq_id, seq_hash, sequence in partition:
                buf.append(f">{seq_id}|{seq_hash}\n{sequence}\n")
                count += 1
                residues += len(sequence)
            yield "".join(buf).encode("ascii")
    finally:
        await result.close()
    log.info(
        "classification export: %d sequences, %d residues (min_length=%d max_length=%s limit=%s)",
        count, residues, min_length, max_length, limit,
    )


def _append_bytes(path: Path, chunk: bytes) -> None:
    with path.open("ab") as fh:
        fh.write(chunk)


async def write_pending_fasta(
    session: AsyncSession,
    path: Path,
    *,
    min_length: int,
    max_length: int | None = None,
    limit: int | None = None,
) -> ExportStats:
    """Same stream as iter_pending_fasta, written to a file (for the
    API-driven submit, which scps it to the BLAST box). Counts are taken from
    the bytes actually written, so they match the file exactly."""
    await asyncio.to_thread(path.write_bytes, b"")
    count = residues = nbytes = 0
    async for chunk in iter_pending_fasta(
        session, min_length=min_length, max_length=max_length, limit=limit
    ):
        await asyncio.to_thread(_append_bytes, path, chunk)
        nbytes += len(chunk)
        for line in chunk.split(b"\n"):
            if not line:
                continue
            if line.startswith(b">"):
                count += 1
            else:
                residues += len(line)
    return ExportStats(sequence_count=count, residue_count=residues, bytes=nbytes)
