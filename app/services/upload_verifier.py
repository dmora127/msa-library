"""Background verification of upload-complete callbacks.

POST /v1/uploads/{id}/complete used to run the whole verify+publish pipeline
inline: a `pelican object get ?directread` subprocess (tens of seconds on a
slow origin), the sha/size/format/seq-hash checks, then the publish. OSPool
jobs sat on the socket the entire time. Now the route parks the row in
'uploaded', answers immediately, and hands the id here.

Contract:
- exactly one task per ingestion id at a time (a job retrying its callback
  must not start a second fetch);
- the task runs IngestionService.complete_upload -- the same synchronous
  pipeline the abandoned-ingestion sweeper runs -- on its own DB session;
- anything that prevents a verdict (OSDF unavailable, fetch-slot queue
  timeout, an unexpected exception, shutdown) is logged with `ingestion=<id>`
  and the row is LEFT 'uploaded'. Nothing retries here: the cron sweeper
  (POST /v1/admin/maintenance/sweep-ingestions) re-attempts every 'uploaded'
  row older than sweep_min_age_minutes and expires the unverifiable past
  sweep_max_age_hours. That is the retry path, by design;
- on shutdown the lifespan hook drains: wait upload_verify_drain_seconds,
  then cancel what is still running.

Single uvicorn worker (see the Dockerfile and the note at the top of
app/core/metrics.py), so the in-memory in-flight table is authoritative.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import OsdfUnavailable
from app.core.metrics import record_upload_verify, record_upload_verify_inflight
from app.services.ingestion_service import IngestionService

log = logging.getLogger("af3cache.ingestion")

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class BackgroundVerifier:
    def __init__(self, session_factory: SessionFactory, *, inline: bool = False):
        """`session_factory()` must yield a fresh AsyncSession per call (the
        production wiring passes `lambda: get_sessionmaker()()`).

        `inline=True` makes submit() await the verification instead of
        scheduling it. Tests use it so a POST's database outcome is observable
        right after the response, on the test's own rolled-back session; a
        real task there would share one AsyncSession between two coroutines,
        which AsyncSession forbids."""
        self._session_factory = session_factory
        self._inline = inline
        self._inflight: dict[str, asyncio.Task] = {}

    # ----------------------------------------------------------------- public
    def is_inflight(self, ingestion_id: str) -> bool:
        t = self._inflight.get(ingestion_id)
        return t is not None and not t.done()

    def inflight_ids(self) -> list[str]:
        return [i for i, t in self._inflight.items() if not t.done()]

    async def submit(self, ingestion_id: str, svc: IngestionService) -> bool:
        """Schedule verification of `ingestion_id`. `svc` is the request-scoped
        IngestionService; only its session is replaced (see
        IngestionService.with_session), so whatever OSDF client / classifier
        the request resolved is what the task uses.

        Returns False when nothing was scheduled: a verification for this id
        is already running, or the event loop refused the task. Either way the
        caller has already answered the job; the row is 'uploaded' and the
        sweeper will get to it."""
        live = self._inflight.get(ingestion_id)
        if live is not None and not live.done():
            log.info(
                "verify ingestion=%s already in flight; callback not resubmitted",
                ingestion_id,
            )
            return False
        if self._inline:
            await self._run(ingestion_id, svc)
            return True
        coro = self._run(ingestion_id, svc)
        try:
            task = asyncio.create_task(coro, name=f"verify:{ingestion_id}")
        except RuntimeError as e:  # no running loop / loop closing
            coro.close()  # never awaited; silence the "never awaited" warning
            log.warning(
                "verify ingestion=%s not scheduled (%s); left 'uploaded' for the sweeper",
                ingestion_id, e,
            )
            record_upload_verify("not_scheduled")
            return False
        self._inflight[ingestion_id] = task
        record_upload_verify_inflight(+1)
        task.add_done_callback(lambda t, _id=ingestion_id: self._on_done(_id, t))
        return True

    async def drain(self, wait_seconds: float) -> None:
        """Shutdown: give running verifications `wait_seconds`, then cancel
        the rest. Cancelled rows stay 'uploaded' (the pipeline commits that
        flip before the fetch) and are picked up by the sweeper."""
        tasks = [t for t in self._inflight.values() if not t.done()]
        if not tasks:
            return
        log.info(
            "verify drain: %d in flight, waiting up to %.1fs", len(tasks), wait_seconds
        )
        _done, pending = await asyncio.wait(tasks, timeout=wait_seconds)
        if pending:
            names = sorted(_id for _id, t in self._inflight.items() if t in pending)
            log.warning(
                "verify drain: cancelling %d still running after %.1fs: %s",
                len(pending), wait_seconds, " ".join(names),
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        log.info("verify drain: done")

    # ---------------------------------------------------------------- private
    def _on_done(self, ingestion_id: str, task: asyncio.Task) -> None:
        # Pop only if the table still points at THIS task: a resubmit that
        # raced the callback must not lose its own entry.
        if self._inflight.get(ingestion_id) is task:
            del self._inflight[ingestion_id]
        record_upload_verify_inflight(-1)

    async def _run(self, ingestion_id: str, svc: IngestionService) -> None:
        t0 = time.perf_counter()
        outcome = "error"
        try:
            async with self._session_factory() as session:
                bound = svc.with_session(session)
                try:
                    out = await bound.complete_upload(ingestion_id, requester="background")
                    await session.commit()
                except OsdfUnavailable as e:
                    # The pipeline committed the 'uploaded' flip before the
                    # fetch, so there is nothing to roll back; the sweeper
                    # re-attempts this row once it is old enough.
                    outcome = "unverified"
                    log.warning(
                        "verify ingestion=%s UNVERIFIED left 'uploaded' (sweeper retries): %s",
                        ingestion_id, str(e)[:200],
                    )
                    return
                except asyncio.CancelledError:
                    outcome = "cancelled"
                    log.warning(
                        "verify ingestion=%s CANCELLED at shutdown; "
                        "left 'uploaded' (sweeper retries)",
                        ingestion_id,
                    )
                    raise
                except Exception:
                    await session.rollback()
                    log.exception(
                        "verify ingestion=%s ERROR left 'uploaded' (sweeper retries)",
                        ingestion_id,
                    )
                    return
                if out.status == "available":
                    outcome = "published"
                elif out.status == "rejected":
                    # _reject already logged "REJECTED reason=..."; one more
                    # line so the reason is visible next to the timing.
                    outcome = "rejected"
                    log.info(
                        "verify ingestion=%s done outcome=rejected reason=%s",
                        ingestion_id, out.reject_reason,
                    )
                else:
                    # dryrun OSDF impl without the sequence text, or anything
                    # else that returned without a verdict
                    outcome = "unverified"
                    log.warning(
                        "verify ingestion=%s UNVERIFIED left '%s' (sweeper retries): %s",
                        ingestion_id, out.status, out.reject_reason or "no verdict",
                    )
        finally:
            record_upload_verify(outcome, time.perf_counter() - t0)
