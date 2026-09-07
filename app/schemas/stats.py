"""Public stats envelope.

camelCase fields are deliberate — they're part of the externally-documented
response contract.
"""

from __future__ import annotations

from pydantic import BaseModel


class StatsData(BaseModel):
    date: str                  # ISO YYYY-MM-DD (UTC)
    alignmentsCached: int      # noqa: N815  total rows in msa_cache_entries
    uniqueProteins: int        # noqa: N815  distinct msa_sequences.seq_hash
    species: int               # distinct msa_cache_entries.species
    communityContributed: int  # noqa: N815  rows where source = community
    osgGenerated: int          # noqa: N815  rows where source = OSG-Generated
    queriesToday: int          # noqa: N815  POST /v1/query calls so far today (UTC)
    queriesTotal: int          # noqa: N815  POST /v1/query calls since metrics began (sum over all days)
    queriesWithHitsToday: int  # noqa: N815  sequences served >=1 cached alignment today (UTC)
    queriesWithHitsTotal: int  # noqa: N815  same, summed over all days; 0 for days before the counter existed


class StatsResponse(BaseModel):
    data: StatsData
