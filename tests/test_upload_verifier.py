"""BackgroundVerifier unit tests: scheduling, dedup, drain, outcome mapping.

No database and no HTTP client: `_run` is replaced by a controllable
coroutine where only the task table matters, and a fake IngestionService +
AsyncMock session drive the outcome mapping.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.errors import OsdfUnavailable
from app.main import app
from app.services import upload_verifier as mod
from app.services.upload_verifier import BackgroundVerifier

IID = "11111111-1111-1111-1111-111111111111"


@asynccontextmanager
async def _dummy_session():
    yield AsyncMock()


def _verifier(**kw) -> BackgroundVerifier:
    return BackgroundVerifier(_dummy_session, **kw)


async def _counter(outcome: str) -> float:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        txt = (await c.get("/metrics")).text
    m = re.search(
        rf'^af3cache_upload_verify_total\{{outcome="{outcome}"\}} (\S+)', txt, re.M
    )
    return float(m.group(1)) if m else 0.0


# ------------------------------------------------------------ task table


async def test_submit_dedups_while_inflight(monkeypatch):
    v = _verifier()
    gate = asyncio.Event()

    async def held(ingestion_id, svc):
        await gate.wait()

    monkeypatch.setattr(v, "_run", held)
    assert await v.submit(IID, None) is True
    assert v.is_inflight(IID) and v.inflight_ids() == [IID]
    assert await v.submit(IID, None) is False  # still running -> refused
    gate.set()
    await v.drain(1.0)
    assert not v.is_inflight(IID)
    assert await v.submit(IID, None) is True  # finished -> a new one is fine
    await v.drain(1.0)


async def test_done_callback_removes_only_own_task(monkeypatch):
    """A finished task's done-callback may run one tick after a resubmit for
    the same id replaced it in the table; it must not pop the new entry."""
    v = _verifier()
    gate = asyncio.Event()

    async def held(ingestion_id, svc):
        await gate.wait()

    monkeypatch.setattr(v, "_run", held)
    await v.submit(IID, None)
    live = v._inflight[IID]

    async def noop():
        pass

    stale = asyncio.create_task(noop())
    await stale
    v._on_done(IID, stale)  # the stale callback firing late
    assert v._inflight.get(IID) is live
    gate.set()
    await v.drain(1.0)
    assert not v.is_inflight(IID)


async def test_finished_task_does_not_block_resubmit(monkeypatch):
    """Between a task finishing and its done-callback running (next loop
    tick) the table still holds the finished task; a resubmit in that window
    must be accepted, not refused as a duplicate."""
    v = _verifier()
    runs = 0

    async def quick(ingestion_id, svc):
        nonlocal runs
        runs += 1

    monkeypatch.setattr(v, "_run", quick)
    await v.submit(IID, None)
    first = v._inflight[IID]
    await first
    # callback may or may not have run yet; either way this must schedule
    assert await v.submit(IID, None) is True
    await v.drain(1.0)
    assert runs == 2


async def test_submit_returns_false_when_create_task_fails(monkeypatch, caplog):
    v = _verifier()

    def raiser(*a, **k):
        raise RuntimeError("no running event loop")

    monkeypatch.setattr(mod.asyncio, "create_task", raiser)
    before = await _counter("not_scheduled")
    with caplog.at_level(logging.WARNING, logger="af3cache.ingestion"):
        assert await v.submit(IID, None) is False
    assert not v.is_inflight(IID)
    assert any(f"ingestion={IID}" in m and "not scheduled" in m for m in caplog.messages)
    assert await _counter("not_scheduled") - before == 1


async def test_drain_times_out_and_cancels(monkeypatch, caplog):
    v = _verifier()

    async def slow(ingestion_id, svc):
        await asyncio.sleep(10)

    monkeypatch.setattr(v, "_run", slow)
    await v.submit(IID, None)
    task = v._inflight[IID]
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with caplog.at_level(logging.WARNING, logger="af3cache.ingestion"):
        await v.drain(0.05)
    assert loop.time() - t0 < 1.0
    assert task.cancelled()
    assert not v.is_inflight(IID)
    assert any("cancelling 1 still running" in m and IID in m for m in caplog.messages)


async def test_drain_without_tasks_is_a_noop():
    await _verifier().drain(0.0)


# ------------------------------------------------------------ outcome map


class _FakeSvc:
    """Stands in for IngestionService: with_session() returns an object whose
    complete_upload yields / raises whatever the test configured."""

    def __init__(self, result=None, exc=None):
        self.result, self.exc = result, exc
        self.session = None

    def with_session(self, session):
        self.session = session
        return self

    async def complete_upload(self, ingestion_id, requester=None):
        assert requester == "background"
        if self.exc is not None:
            raise self.exc
        return self.result


def _resp(status, reason=None):
    return SimpleNamespace(status=status, reject_reason=reason)


@pytest.mark.parametrize(
    "svc, outcome, committed, level, needle",
    [
        (_FakeSvc(_resp("available")), "published", True, None, None),
        (_FakeSvc(_resp("rejected", "sha256_mismatch")), "rejected", True,
         logging.INFO, "outcome=rejected reason=sha256_mismatch"),
        (_FakeSvc(_resp("uploaded", "simulated: awaiting OSDF")), "unverified", True,
         logging.WARNING, "UNVERIFIED left 'uploaded'"),
        (_FakeSvc(exc=OsdfUnavailable("origin down")), "unverified", False,
         logging.WARNING, "UNVERIFIED left 'uploaded' (sweeper retries): origin down"),
        (_FakeSvc(exc=RuntimeError("boom")), "error", False,
         logging.ERROR, "ERROR left 'uploaded'"),
    ],
    ids=["published", "rejected", "no-verdict", "osdf-unavailable", "exception"],
)
async def test_run_maps_outcomes(svc, outcome, committed, level, needle, caplog):
    session = AsyncMock()

    @asynccontextmanager
    async def factory():
        yield session

    v = BackgroundVerifier(factory)
    before = await _counter(outcome)
    with caplog.at_level(logging.INFO, logger="af3cache.ingestion"):
        await v._run(IID, svc)
    assert svc.session is session
    assert session.commit.await_count == (1 if committed else 0)
    if outcome == "error":
        session.rollback.assert_awaited_once()
    assert await _counter(outcome) - before == 1
    if needle:
        hits = [r for r in caplog.records if f"ingestion={IID}" in r.getMessage()
                and needle in r.getMessage()]
        assert hits and hits[0].levelno == level, caplog.messages
        if outcome == "error":
            assert hits[0].exc_info is not None


async def test_cancelled_run_logs_and_reraises(caplog):
    class _Hang(_FakeSvc):
        async def complete_upload(self, ingestion_id, requester=None):
            await asyncio.sleep(10)

    session = AsyncMock()

    @asynccontextmanager
    async def factory():
        yield session

    v = BackgroundVerifier(factory)
    before = await _counter("cancelled")
    with caplog.at_level(logging.WARNING, logger="af3cache.ingestion"):
        task = asyncio.create_task(v._run(IID, _Hang()))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    session.commit.assert_not_awaited()
    assert any("CANCELLED at shutdown" in m and IID in m for m in caplog.messages)
    assert await _counter("cancelled") - before == 1


async def test_inline_mode_awaits_run(monkeypatch):
    v = _verifier(inline=True)
    seen: list[str] = []

    async def record(ingestion_id, svc):
        seen.append(ingestion_id)

    monkeypatch.setattr(v, "_run", record)
    assert await v.submit(IID, None) is True
    assert seen == [IID] and not v.is_inflight(IID)
