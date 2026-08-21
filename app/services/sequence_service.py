"""Sequence lookup + idempotent get-or-create.

Reads go through the `msa_cache_lookup` VIEW (exactly as the existing client
does), joined to `msa_sources` only to honor the `enabled` flag, so community-
contributed entries surface identically to OSG/GDM/etc.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.hashing import normalize_sequence, sequence_hash


@dataclass(frozen=True)
class LookupRow:
    seq_hash: str
    sequence: str
    seq_length: int
    molecule_type: str
    source: str
    osdf_uri: str
    db_version: str
    a3m_size_bytes: int | None
    a3m_sha256: str | None
    n_sequences: int | None
    species: str
    query_name: str


# Returns ALL enabled sources for the sequence(s) that are active and not idle.
# The server does NO source preference/selection — the calling job decides which
# hit to pull (and which db_version it wants). Idle filtering is applied here
# defensively even between sweeps so an idle entry is never served.
_LOOKUP_SQL = """
    select v.seq_hash, v.sequence, v.seq_length, v.molecule_type,
           v.source, v.osdf_uri, v.db_version,
           v.a3m_size_bytes, v.a3m_sha256, v.n_sequences,
           v.species, v.query_name
    from public.msa_cache_lookup v
    join public.msa_sources s on s.name = v.source
    where v.seq_hash in :hashes
      and v.status = 'active'
      and (s.idle_ttl_days is null
           or v.last_accessed_at > now() - make_interval(days => s.idle_ttl_days))
      {enabled_clause}
      {source_clause}
"""

# Coarsened access-bump: only refresh entries whose last_accessed_at is older
# than the configured granularity, bounding write amplification on hot reads.
_BUMP_SQL = """
    update public.msa_cache_entries e
       set last_accessed_at = now()
      from public.msa_sequences sq
     where e.sequence_id = sq.id
       and sq.seq_hash in :hashes
       and e.status = 'active'
       and e.last_accessed_at < now() - make_interval(secs => :gran)
       {source_clause}
"""


class SequenceService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def lookup(
        self,
        seq_hashes: list[str],
        sources: list[str] | None = None,
        *,
        enabled_only: bool = True,
    ) -> list[LookupRow]:
        if not seq_hashes:
            return []
        enabled_clause = "and s.enabled = true" if enabled_only else ""
        source_clause = "and v.source in :sources" if sources else ""
        stmt = text(
            _LOOKUP_SQL.format(
                enabled_clause=enabled_clause, source_clause=source_clause
            )
        ).bindparams(bindparam("hashes", expanding=True))
        params: dict[str, object] = {"hashes": list(set(seq_hashes))}
        if sources:
            stmt = stmt.bindparams(bindparam("sources", expanding=True))
            params["sources"] = sources
        result = await self.session.execute(stmt, params)
        rows = [LookupRow(**dict(r._mapping)) for r in result]
        if rows:
            await self._bump_access(params["hashes"], sources)
        return rows

    async def _bump_access(
        self, seq_hashes: list[str], sources: list[str] | None
    ) -> None:
        gran = get_settings().access_bump_granularity_seconds
        source_clause = "and e.source in :sources" if sources else ""
        stmt = text(
            _BUMP_SQL.format(source_clause=source_clause)
        ).bindparams(bindparam("hashes", expanding=True))
        params: dict[str, object] = {"hashes": seq_hashes, "gran": gran}
        if sources:
            stmt = stmt.bindparams(bindparam("sources", expanding=True))
            params["sources"] = sources
        await self.session.execute(stmt, params)

    async def get_or_create_sequence_id(
        self, normalized: str, *, molecule_type: str = "protein"
    ) -> int:
        """Idempotent INSERT ... ON CONFLICT then SELECT. Returns msa_sequences.id."""
        seq_hash = sequence_hash(normalized, already_normalized=True)
        await self.session.execute(
            text(
                """
                insert into public.msa_sequences
                    (seq_hash, sequence, seq_length, molecule_type)
                values (:h, :s, :len, :mt)
                on conflict (seq_hash) do nothing
                """
            ),
            {
                "h": seq_hash,
                "s": normalized,
                "len": len(normalized),
                "mt": molecule_type,
            },
        )
        row = await self.session.execute(
            text("select id from public.msa_sequences where seq_hash = :h"),
            {"h": seq_hash},
        )
        return int(row.scalar_one())

    @staticmethod
    def normalize_and_hash(raw: str) -> tuple[str, str]:
        norm = normalize_sequence(raw)
        return norm, sequence_hash(norm, already_normalized=True)
