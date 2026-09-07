"""GET /v1/stats — public snapshot counts. No auth (empty curl)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.schemas.stats import StatsData, StatsResponse

router = APIRouter(tags=["stats"])

_COMMUNITY = "Community Contributed - Cached"
_OSG = "OSG-Generated"

_STATS_SQL = """
select
  (select count(*)                from public.msa_cache_entries)         as alignments_cached,
  (select count(*)                from public.msa_sequences)             as unique_proteins,
  (select count(distinct species) from public.msa_cache_entries)         as species,
  (select count(*)                from public.msa_cache_entries
     where source = :community)                                          as community_contributed,
  (select count(*)                from public.msa_cache_entries
     where source = :osg)                                                as osg_generated,
  (select coalesce(count, 0)      from public.query_metrics
     where day = current_date)                                           as queries_today,
  (select coalesce(sum(count), 0) from public.query_metrics)             as queries_total,
  (select coalesce(hit_count, 0)  from public.query_metrics
     where day = current_date)                                          as queries_with_hits_today,
  (select coalesce(sum(hit_count), 0) from public.query_metrics)        as queries_with_hits_total
"""


@router.get("/v1/stats", response_model=StatsResponse)
async def get_stats(
    session: AsyncSession = Depends(get_session),
) -> StatsResponse:
    row = (
        await session.execute(text(_STATS_SQL), {"community": _COMMUNITY, "osg": _OSG})
    ).one()
    return StatsResponse(
        data=StatsData(
            date=datetime.now(UTC).date().isoformat(),
            alignmentsCached=row.alignments_cached,
            uniqueProteins=row.unique_proteins,
            species=row.species,
            communityContributed=row.community_contributed,
            osgGenerated=row.osg_generated,
            queriesToday=row.queries_today or 0,
            queriesTotal=row.queries_total or 0,
            queriesWithHitsToday=row.queries_with_hits_today or 0,
            queriesWithHitsTotal=row.queries_with_hits_total or 0,
        )
    )
