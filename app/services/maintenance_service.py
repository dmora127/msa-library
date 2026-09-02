"""Idle sweep + reaper.

Two distinct steps, invoked periodically (cron / scheduler hits the admin
maintenance endpoint, or a CLI calls this service):

1. mark_idle_stale: active entries past their source's idle_ttl_days WITHOUT a
   recent query hit flip to 'stale' (stale_since=now()). Stale entries are
   immediately invisible to queries (the lookup filters status='active').
2. reap: stale entries whose stale_since is older than the grace window have
   their OSDF object deleted and their row removed. The grace window prevents
   deleting an object a job is still mid-download after it just went stale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import SequenceHashMismatch, SequenceNotFound
from app.services.classifier import PENDING_CLASSIFICATION, UNCLASSIFIED, Classifier
from app.services.osdf_client import OsdfClient
from app.services.settings_service import SettingsService

log = logging.getLogger("af3cache.maintenance")


@dataclass(frozen=True)
class MaintenanceResult:
    marked_stale: int
    reaped: int


@dataclass(frozen=True)
class ReclassifyResult:
    scanned: int
    updated: int
    last_id: int | None     # max(id) of the batch (None if scanned == 0)
    exhausted: bool         # True iff scanned < limit -> no more rows past last_id


class MaintenanceService:
    def __init__(
        self,
        session: AsyncSession,
        osdf_client: OsdfClient,
        settings_service: SettingsService,
        classifier: Classifier,
    ):
        self.session = session
        self.osdf = osdf_client
        self.settings_service = settings_service
        self.classifier = classifier

    async def mark_idle_stale(self) -> int:
        rows = await self.session.execute(
            text(
                """
                update public.msa_cache_entries e
                   set status = 'stale', stale_since = now(), updated_at = now()
                  from public.msa_sources s
                 where e.source = s.name
                   and e.status = 'active'
                   and s.idle_ttl_days is not null
                   and e.last_accessed_at
                       < now() - make_interval(days => s.idle_ttl_days)
                returning e.id
                """
            )
        )
        n = len(rows.all())
        if n:
            log.info("maintenance: marked %d entries stale", n)
        return n

    async def reap(self) -> int:
        eff = await self.settings_service.effective()
        due = (
            await self.session.execute(
                text(
                    """
                    select id, osdf_uri from public.msa_cache_entries
                    where status = 'stale'
                      and stale_since
                          < now() - make_interval(hours => :grace)
                    """
                ),
                {"grace": eff.reap_grace_hours},
            )
        ).all()
        reaped = 0
        for row in due:
            await self.osdf.delete(row.osdf_uri)  # idempotent
            await self.session.execute(
                text("delete from public.msa_cache_entries where id = :id"),
                {"id": row.id},
            )
            reaped += 1
        if reaped:
            log.info("maintenance: reaped %d stale entries", reaped)
        return reaped

    async def reclassify_unclassified(
        self, limit: int = 100, after_id: int = 0
    ) -> ReclassifyResult:
        """Find msa_cache_entries still marked 'Unclassified' with id > after_id
        and re-run the Classifier on their sequence. Update species/query_name
        on a confident hit; leave alone otherwise. Returns scanned/updated/
        last_id/exhausted so a caller can drain the table by advancing a
        cursor (after_id := last_id) until exhausted=True. Per-row classifier
        failures are logged + skipped (never abort the batch)."""
        rows = (
            await self.session.execute(
                text(
                    """
                    select e.id, sq.sequence
                    from public.msa_cache_entries e
                    join public.msa_sequences sq on sq.id = e.sequence_id
                    where (e.species = 'Unclassified'
                           or e.query_name = 'Unclassified')
                      and e.id > :after_id
                    order by e.id
                    limit :limit
                    """
                ),
                {"limit": limit, "after_id": after_id},
            )
        ).all()
        scanned = len(rows)
        updated = 0
        for i, row in enumerate(rows, start=1):
            try:
                cls = await self.classifier.classify(row.sequence)
            except Exception as e:  # pragma: no cover - defensive
                log.warning(
                    "reclassify [%d/%d] id=%s FAILED: %s",
                    i, scanned, row.id, e,
                )
                continue
            if cls == UNCLASSIFIED:
                log.info(
                    "reclassify [%d/%d] id=%s -> Unclassified",
                    i, scanned, row.id,
                )
                continue
            await self.session.execute(
                text(
                    "update public.msa_cache_entries "
                    "set species = :sp, query_name = :qn, updated_at = now() "
                    "where id = :id"
                ),
                {"sp": cls.species, "qn": cls.query_name, "id": row.id},
            )
            updated += 1
            log.info(
                "reclassify [%d/%d] id=%s -> species=%r",
                i, scanned, row.id, cls.species,
            )
        last_id = rows[-1].id if rows else None
        exhausted = scanned < limit
        if scanned:
            log.info(
                "reclassify: scanned %d (after_id=%d), updated %d, "
                "last_id=%s, exhausted=%s",
                scanned, after_id, updated, last_id, exhausted,
            )
        return ReclassifyResult(
            scanned=scanned, updated=updated,
            last_id=last_id, exhausted=exhausted,
        )

    async def apply_external_classification(
        self, sequence_id: int, sequence_hash: str, species: str, query_name: str
    ) -> int:
        """Apply an externally computed classification (BLAST callback) to every
        cache entry of `sequence_id` still carrying the 'Pending Classification'
        sentinel in BOTH species and query_name. sequence_hash must match the
        stored seq_hash — guards against a stale/transposed id on the caller's
        side. Returns rows updated (0 = nothing pending; idempotent)."""
        row = (
            await self.session.execute(
                text("select seq_hash from public.msa_sequences where id = :id"),
                {"id": sequence_id},
            )
        ).first()
        if row is None:
            raise SequenceNotFound(f"msa_sequences.id={sequence_id} not found")
        if row.seq_hash != sequence_hash:
            raise SequenceHashMismatch(
                "sequence_hash does not match the stored seq_hash",
                extra={"sequence_id": sequence_id},
            )
        updated = len(
            (
                await self.session.execute(
                    text(
                        """
                        update public.msa_cache_entries
                           set species = :sp, query_name = :qn, updated_at = now()
                         where sequence_id = :sid
                           and species = :pending
                           and query_name = :pending
                        returning id
                        """
                    ),
                    {
                        "sp": species, "qn": query_name,
                        "sid": sequence_id, "pending": PENDING_CLASSIFICATION,
                    },
                )
            ).all()
        )
        log.info(
            "external reclassify: sequence_id=%s updated=%d species=%r",
            sequence_id, updated, species,
        )
        return updated

    async def run(self) -> MaintenanceResult:
        marked = await self.mark_idle_stale()
        reaped = await self.reap()
        return MaintenanceResult(marked_stale=marked, reaped=reaped)
