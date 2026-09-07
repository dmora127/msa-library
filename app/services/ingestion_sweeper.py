"""Abandoned-ingestion sweeper.

Upload requests get abandoned (API outage mid-job, job interrupted, token
expired before the put). Their msa_ingestions rows sit in `pending` (token
minted, object never confirmed) or `uploaded` (complete called, verify never
succeeded) forever — and, since the capacity gate counts in-flight rows at
their declared size, they also reserve phantom bytes.

Policy (POST /v1/admin/maintenance/sweep-ingestions, cron every 15 min):
- rows younger than `sweep_min_age_minutes` are never touched;
- EVERY selected row is re-attempted on EVERY run via the normal
  IngestionService.complete_upload pipeline (fetch from OSDF, verify, publish
  or reject) — the sweeper adds no verification logic of its own;
- a row whose attempt did not complete it (object not fetchable) AND that is
  older than `sweep_max_age_hours` is rejected with EXPIRED_REASON;
- circuit breaker: if every attempt in a run failed with the retryable OSDF
  error (and at least 3 were attempted), OSDF itself is probably down, so
  expiries are held for that run rather than converting real uploads into
  rejections.

Nothing in msa_cache_entries is touched: an in-flight row has no cache entry,
and the only matchable one would be a stale/held entry that must not be demoted.
Rows are processed one at a time, so the sweeper uses at most one of the
`verify_concurrency` OSDF fetch slots shared with live job callbacks.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import OsdfUnavailable
from app.core.metrics import record_ingestion_sweep, record_ingestion_sweep_run
from app.services.ingestion_service import IngestionService

log = logging.getLogger("af3cache.sweeper")

EXPIRED_REASON = "Expired - Upload unverifiable"
BREAKER_MIN_ATTEMPTS = 3


def _clock() -> float:  # indirection so tests can drive the budget deterministically
    return time.monotonic()

SELECT_SQL = """
select id::text as id, status::text as status, osdf_uri,
       extract(epoch from now() - created_at)::bigint as age_seconds
  from public.msa_ingestions
 where status in ('pending', 'uploaded')
   and created_at < now() - make_interval(mins => :min_age)
 order by created_at
 limit :lim
"""


@dataclass(frozen=True)
class SweepItem:
    id: str
    status_before: str
    age_seconds: int
    outcome: str  # published | rejected_verify | unverified | expired | error
    #             | would_attempt | would_expire (dry_run) | skipped_budget
    detail: str | None = None


@dataclass
class SweepResult:
    dry_run: bool
    scanned: int = 0
    attempted: int = 0
    published: int = 0
    rejected_verify: int = 0
    unverified: int = 0
    expired: int = 0
    expiries_held: int = 0
    errors: int = 0
    skipped_budget: int = 0
    exhausted: bool = True
    elapsed_seconds: float = 0.0
    items: list[SweepItem] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["items"] = [asdict(i) for i in self.items]
        return d


class IngestionSweeper:
    def __init__(
        self, session: AsyncSession, ingestion: IngestionService, settings: Settings
    ):
        self.session = session
        self.ingestion = ingestion
        self.settings = settings

    async def run(
        self,
        *,
        limit: int | None = None,
        dry_run: bool = False,
        min_age_minutes: int | None = None,
        max_age_hours: int | None = None,
        budget_seconds: int | None = None,
    ) -> SweepResult:
        s = self.settings
        limit = limit or s.sweep_default_limit
        min_age = s.sweep_min_age_minutes if min_age_minutes is None else min_age_minutes
        max_age = s.sweep_max_age_hours if max_age_hours is None else max_age_hours
        budget = s.sweep_budget_seconds if budget_seconds is None else budget_seconds
        max_age_s = max_age * 3600
        t0 = _clock()
        res = SweepResult(dry_run=dry_run)

        rows = (
            await self.session.execute(
                text(SELECT_SQL), {"min_age": min_age, "lim": limit + 1}
            )
        ).all()
        res.exhausted = len(rows) <= limit
        rows = rows[:limit]
        res.scanned = len(rows)
        if not rows:
            res.elapsed_seconds = round(_clock() - t0, 3)
            return res

        log.info(
            "sweep start: %d row(s) older than %dm (expire past %dh, limit=%d, dry_run=%s)",
            len(rows), min_age, max_age, limit, dry_run,
        )
        candidates: list[tuple] = []  # (row, detail): unverified AND over max age
        for row in rows:
            over_max = row.age_seconds > max_age_s
            if _clock() - t0 > budget:
                res.skipped_budget += 1
                res.exhausted = False
                res.items.append(SweepItem(
                    row.id, row.status, row.age_seconds, "skipped_budget",
                ))
                continue
            if dry_run:
                outcome = "would_expire" if over_max else "would_attempt"
                res.items.append(SweepItem(row.id, row.status, row.age_seconds, outcome))
                continue

            res.attempted += 1
            detail: str | None = None
            try:
                out = await self.ingestion.complete_upload(row.id, requester="sweeper")
                if out.status == "available":
                    outcome = "published"
                elif out.status == "rejected":
                    outcome = "rejected_verify"
                    detail = out.reject_reason
                else:  # 'uploaded': simulate impl without sequence text
                    outcome = "unverified"
                    detail = f"status={out.status} {out.reject_reason or ''}".strip()
            except OsdfUnavailable as e:
                outcome = "unverified"
                detail = str(e)[:200]
            except Exception as e:  # noqa: BLE001 - one bad row must not stop the sweep
                # discard only THIS row's partial work: every earlier row was
                # committed below, so the rollback cannot undo its publish
                await self.session.rollback()
                outcome = "error"
                detail = f"{type(e).__name__}: {str(e)[:200]}"
                log.exception("sweep ingestion=%s ERROR", row.id)
            else:
                # the pipeline commits the 'uploaded' flip itself but leaves
                # the publish/reject for the request session; make each row's
                # result durable before touching the next one
                await self.session.commit()

            if outcome == "published":
                res.published += 1
            elif outcome == "rejected_verify":
                res.rejected_verify += 1
            elif outcome == "unverified":
                res.unverified += 1
            else:
                res.errors += 1
            record_ingestion_sweep(outcome)
            log.log(
                logging.WARNING if outcome == "error" else logging.INFO,
                "sweep ingestion=%s before=%s age=%ds outcome=%s detail=%s",
                row.id, row.status, row.age_seconds, outcome, detail,
            )
            if outcome == "unverified" and over_max:
                # item appended below once we know whether it expires or is held
                candidates.append((row, detail))
            else:
                res.items.append(SweepItem(
                    row.id, row.status, row.age_seconds, outcome, detail,
                ))

        if candidates and (
            res.attempted >= BREAKER_MIN_ATTEMPTS and res.unverified == res.attempted
        ):
            res.expiries_held = len(candidates)
            record_ingestion_sweep("expiries_held", len(candidates))
            log.warning(
                "sweep: every one of %d attempts failed with OSDF unavailable; "
                "holding %d expiries this run (OSDF outage?)",
                res.attempted, len(candidates),
            )
            for row, detail in candidates:
                res.items.append(SweepItem(
                    row.id, row.status, row.age_seconds, "unverified",
                    f"expiry held: {detail}",
                ))
        else:
            for row, detail in candidates:
                await self.ingestion.reject_ingestion(row.id, EXPIRED_REASON)
                res.expired += 1
                record_ingestion_sweep("expired")
                log.warning(
                    "sweep ingestion=%s EXPIRED age=%ds (%s): %s",
                    row.id, row.age_seconds, EXPIRED_REASON, detail,
                )
                res.items.append(SweepItem(
                    row.id, row.status, row.age_seconds, "expired", EXPIRED_REASON,
                ))
            if candidates:
                await self.session.commit()

        res.elapsed_seconds = round(_clock() - t0, 3)
        record_ingestion_sweep_run(res.elapsed_seconds)
        log.info(
            "sweep done: scanned=%d attempted=%d published=%d rejected=%d "
            "unverified=%d expired=%d held=%d errors=%d skipped=%d in %.1fs",
            res.scanned, res.attempted, res.published, res.rejected_verify,
            res.unverified, res.expired, res.expiries_held, res.errors,
            res.skipped_budget, res.elapsed_seconds,
        )
        return res
