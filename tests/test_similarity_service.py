"""SimilarityService: ephemeral lifecycle, concurrency gate, snapshot search.

Seeded rows live in db_session's rolled-back transaction; the service reads
through that same session, exactly as the request path does.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.errors import SimilarityDisabled, SimilarityUnavailable
from app.services.similarity import builders
from app.services.similarity.builders import (
    DELTA_LINK,
    MANIFEST,
    NIGHTLY_LINK,
    TARGET_DB,
    promote,
)
from app.services.similarity.database import DumpStats
from app.services.similarity.models import Manifest
from app.services.similarity.service import SimilarityRuntime, SimilarityService
from tests.similarity_fakes import argv_lines, fake_mmseqs, m8_row, make_config
from tests.test_query import _seed_entry, _seed_sequence

QUERY = "MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEKAVQVKVKALPDAQFEVVHSLAKWKR"


def _workdirs(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "tmp").glob("query_similarity_*"))


async def _set_status(db_session, seq_id: int, status: str) -> None:
    await db_session.execute(
        text("update public.msa_cache_entries set status = :st where sequence_id = :sid"),
        {"st": status, "sid": seq_id},
    )


async def _seed_four(db_session):
    """active / stale / held / no-entry sequences; returns their hashes."""
    ha, ia = await _seed_sequence(db_session, "MKTAYIAKQRQISFVKSHFSRQ")
    await _seed_entry(db_session, ia, "OSG-Generated", "osdf:///a.a3m")
    hs, i_s = await _seed_sequence(db_session, "MKTAYIAKQRQISFVKSHFSRR")
    await _seed_entry(db_session, i_s, "OSG-Generated", "osdf:///s.a3m")
    await _set_status(db_session, i_s, "stale")
    hh, ih = await _seed_sequence(db_session, "MKTAYIAKQRQISFVKSHFSRK")
    await _seed_entry(db_session, ih, "OSG-Generated", "osdf:///h.a3m")
    await _set_status(db_session, ih, "held")
    hn, _ = await _seed_sequence(db_session, "MKTAYIAKQRQISFVKSHFSRD")
    return ha, hs, hh, hn


# --- ephemeral ---------------------------------------------------------------


async def test_ephemeral_dumps_only_active_targets(tmp_path, db_session):
    ha, hs, hh, hn = await _seed_four(db_session)
    copies = tmp_path / "copies"
    binary, log = fake_mmseqs(tmp_path, m8=m8_row(ha), fasta_copy_dir=copies)
    svc = SimilarityService(db_session, SimilarityRuntime(make_config(binary, tmp_path)))
    hits = await svc.query_similarity(QUERY)
    assert [h.seq_hash for h in hits] == [ha]
    assert hits[0].source_database == "ephemeral"
    fasta = (copies / "targetDB.fa").read_text()
    headers = {line[1:] for line in fasta.splitlines() if line.startswith(">")}
    assert headers == {ha}
    # full ephemeral pipeline, in order, in one private workdir
    steps = [row[0] for row in argv_lines(log)]
    assert steps == ["createdb", "createindex", "createdb", "search", "convertalis"]
    dirs = {Path(row[2]).parent.name for row in argv_lines(log, "createdb")}
    assert len(dirs) == 1 and next(iter(dirs)).startswith("query_similarity_")
    assert _workdirs(tmp_path) == []


async def test_ephemeral_index_can_be_disabled(tmp_path, db_session):
    ha, *_ = await _seed_four(db_session)
    binary, log = fake_mmseqs(tmp_path, m8=m8_row(ha))
    cfg = make_config(binary, tmp_path, ephemeral_index=False)
    await SimilarityService(db_session, SimilarityRuntime(cfg)).query_similarity(QUERY)
    assert argv_lines(log, "createindex") == []


async def test_ephemeral_filters_and_ranks(tmp_path, db_session):
    ha, hs, hh, hn = await _seed_four(db_session)
    other = "f" * 64
    m8 = "\n".join([
        m8_row(ha, fident=0.93),
        m8_row(other, fident=0.99),  # better identity, and mmseqs may emit any hash
        m8_row(hn, fident=1.0, qcov=0.3),  # coverage fail
        m8_row(hh, fident=0.5),  # identity fail
    ])
    binary, _ = fake_mmseqs(tmp_path, m8=m8)
    svc = SimilarityService(db_session, SimilarityRuntime(make_config(binary, tmp_path)))
    hits = await svc.query_similarity(QUERY)
    assert [h.seq_hash for h in hits] == [other, ha]


async def test_ephemeral_zero_targets_short_circuits(tmp_path, db_session):
    # nothing active in this transaction -> no mmseqs at all, no error
    binary, log = fake_mmseqs(tmp_path, m8=m8_row("a" * 64))
    svc = SimilarityService(db_session, SimilarityRuntime(make_config(binary, tmp_path)))
    assert await svc.query_similarity(QUERY) == []
    assert argv_lines(log) == []
    assert _workdirs(tmp_path) == []


@pytest.mark.parametrize("scenario", ["success", "no_hit", "mmseqs_error", "parse_error"])
async def test_ephemeral_workdir_always_removed(tmp_path, db_session, scenario):
    ha, *_ = await _seed_four(db_session)
    kw = {"m8": m8_row(ha)}
    if scenario == "no_hit":
        kw["m8"] = ""
    elif scenario == "mmseqs_error":
        kw["fail_step"] = "search"
    elif scenario == "parse_error":
        kw["m8"] = m8_row(ha, fident=7.0)
    binary, _ = fake_mmseqs(tmp_path, **kw)
    svc = SimilarityService(db_session, SimilarityRuntime(make_config(binary, tmp_path)))
    if scenario in ("mmseqs_error", "parse_error"):
        with pytest.raises(SimilarityUnavailable) as ei:
            await svc.query_similarity(QUERY)
        assert ei.value.status_code == 503
        # public message names the step, never paths/stderr
        assert "mmseqs" in ei.value.message and str(tmp_path) not in ei.value.message
    else:
        hits = await svc.query_similarity(QUERY)
        assert bool(hits) == (scenario == "success")
    assert _workdirs(tmp_path) == []


async def test_ephemeral_workdir_removed_on_postgres_error(tmp_path, monkeypatch):
    binary, _ = fake_mmseqs(tmp_path)

    async def boom(session, fasta):
        raise RuntimeError("db down")

    monkeypatch.setattr(builders, "dump_active_targets", boom)
    svc = SimilarityService(None, SimilarityRuntime(make_config(binary, tmp_path)))
    with pytest.raises(RuntimeError):
        await svc.query_similarity(QUERY)
    assert _workdirs(tmp_path) == []


async def test_concurrent_requests_get_distinct_workdirs_and_run_in_parallel(
    tmp_path, monkeypatch
):
    seq = "a" * 64

    async def fake_dump(session, fasta):
        fasta.write_text(f">{seq}\nMKT\n")
        return DumpStats(1, 3)

    monkeypatch.setattr(builders, "dump_active_targets", fake_dump)
    binary, log = fake_mmseqs(tmp_path, m8=m8_row(seq), sleep_step="createindex",
                              sleep_seconds=0.4)
    rt = SimilarityRuntime(make_config(binary, tmp_path, max_concurrent=4))
    # the sleeping step is createindex; search/convertalis still need to run,
    # so use a second fake that does not sleep for those (same argv log)
    t0 = time.monotonic()
    results = await asyncio.gather(
        *(SimilarityService(None, rt).query_similarity(QUERY) for _ in range(3)),
        return_exceptions=True,
    )
    elapsed = time.monotonic() - t0
    # createindex "sleeps" via exec, so it never creates files; the rest of the
    # pipeline still ran (fake search/convertalis need no index).
    assert all(isinstance(r, list) for r in results), results
    dirs = {Path(row[2]).parent.name for row in argv_lines(log, "createdb")}
    assert len(dirs) == 3 and all(d.startswith("query_similarity_") for d in dirs)
    assert elapsed < 0.4 * 3 * 0.8, f"requests were serialised ({elapsed:.2f}s)"
    assert _workdirs(tmp_path) == []


async def test_queue_timeout_is_503(tmp_path, monkeypatch):
    seq = "a" * 64

    async def fake_dump(session, fasta):
        fasta.write_text(f">{seq}\nMKT\n")
        return DumpStats(1, 3)

    monkeypatch.setattr(builders, "dump_active_targets", fake_dump)
    binary, _ = fake_mmseqs(tmp_path, m8=m8_row(seq), sleep_step="search", sleep_seconds=1.5)
    rt = SimilarityRuntime(make_config(binary, tmp_path, max_concurrent=1,
                                       queue_timeout_seconds=0.2, mmseqs_timeout_seconds=5))
    first = asyncio.create_task(SimilarityService(None, rt).query_similarity(QUERY))
    await asyncio.sleep(0.1)
    with pytest.raises(SimilarityUnavailable) as ei:
        await SimilarityService(None, rt).query_similarity(QUERY)
    assert "queue" in ei.value.message
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first


async def test_disabled_raises_409(tmp_path):
    binary, log = fake_mmseqs(tmp_path)
    rt = SimilarityRuntime(make_config(binary, tmp_path, search_enabled=False))
    with pytest.raises(SimilarityDisabled) as ei:
        await SimilarityService(None, rt).query_similarity(QUERY)
    assert ei.value.status_code == 409 and argv_lines(log) == []


# --- snapshot ----------------------------------------------------------------


def _publish(root: Path, build_type: str, name: str, *, count: int, base: str | None = None,
             with_db: bool = True) -> Path:
    gen = root / name
    gen.mkdir(parents=True)
    if with_db:
        for suffix in ("", ".index", ".dbtype"):
            (gen / f"{TARGET_DB}{suffix}").write_text("")
    m = Manifest(
        build_type=build_type, generation=name,
        created_at=datetime.now(UTC).isoformat(),
        snapshot_taken_at=datetime.now(UTC).isoformat(),
        postgres_high_water_mark=100, sequence_count=count, residue_count=count * 100,
        indexed=build_type == "nightly", mmseqs_version="x",
        base_nightly_generation=base,
    )
    (gen / MANIFEST).write_text(m.to_json())
    promote(root, NIGHTLY_LINK if build_type == "nightly" else DELTA_LINK, gen)
    return gen


async def test_snapshot_missing_nightly_is_503(tmp_path):
    binary, log = fake_mmseqs(tmp_path)
    rt = SimilarityRuntime(make_config(binary, tmp_path, db_mode="snapshot"))
    with pytest.raises(SimilarityUnavailable):
        await SimilarityService(None, rt).query_similarity(QUERY)
    assert argv_lines(log) == []


async def test_snapshot_merges_nightly_and_delta(tmp_path):
    root = tmp_path / "db"
    n = _publish(root, "nightly", "nightly-build-20260901T020000Z-DB", count=10)
    _publish(root, "delta", "delta-20260901T030000Z-DB", count=2, base=n.name)
    shared, only_n, only_d = "1" * 64, "2" * 64, "3" * 64
    binary, log = fake_mmseqs(tmp_path, m8_by_target={
        "nightly-build-": "\n".join([m8_row(shared, fident=0.93), m8_row(only_n, fident=0.95)]),
        "delta-": "\n".join([m8_row(shared, fident=0.99), m8_row(only_d, fident=0.91)]),
    })
    rt = SimilarityRuntime(make_config(binary, tmp_path, db_mode="snapshot"))
    hits = await SimilarityService(None, rt).query_similarity(QUERY)
    assert [(h.seq_hash, h.source_database) for h in hits] == [
        (shared, "delta"), (only_n, "nightly"), (only_d, "delta"),
    ]
    assert hits[0].percent_identity == pytest.approx(99.0)  # better duplicate kept
    # two searches, no target build, workdir gone
    steps = [row[0] for row in argv_lines(log)]
    assert steps.count("search") == 2 and "createindex" not in steps
    assert _workdirs(tmp_path) == []


@pytest.mark.parametrize("delta_state", ["absent", "empty"])
async def test_snapshot_skips_absent_or_empty_delta(tmp_path, delta_state):
    root = tmp_path / "db"
    n = _publish(root, "nightly", "nightly-build-20260901T020000Z-DB", count=10)
    if delta_state == "empty":
        _publish(root, "delta", "delta-20260901T030000Z-DB", count=0, base=n.name,
                 with_db=False)
    binary, log = fake_mmseqs(tmp_path, m8=m8_row("1" * 64))
    rt = SimilarityRuntime(make_config(binary, tmp_path, db_mode="snapshot"))
    hits = await SimilarityService(None, rt).query_similarity(QUERY)
    assert len(hits) == 1 and hits[0].source_database == "nightly"
    searches = argv_lines(log, "search")
    assert len(searches) == 1 and "nightly-build-" in searches[0][2]


async def test_snapshot_resolves_symlink_once(tmp_path):
    root = tmp_path / "db"
    g1 = _publish(root, "nightly", "nightly-build-20260901T020000Z-DB", count=10)
    g2 = root / "nightly-build-20260902T020000Z-DB"
    g2.mkdir()
    for suffix in ("", ".index", ".dbtype"):
        (g2 / f"{TARGET_DB}{suffix}").write_text("")
    (g2 / MANIFEST).write_text((g1 / MANIFEST).read_text().replace(g1.name, g2.name))
    # the fake `search` re-points the active link mid-request
    hook = f"ln -sfn {g2.name} {root / NIGHTLY_LINK}"
    binary, log = fake_mmseqs(tmp_path, m8=m8_row("1" * 64), search_hook=hook)
    rt = SimilarityRuntime(make_config(binary, tmp_path, db_mode="snapshot"))
    hits = await SimilarityService(None, rt).query_similarity(QUERY)
    assert len(hits) == 1
    assert os.readlink(root / NIGHTLY_LINK) == g2.name  # link did move ...
    (conv,) = argv_lines(log, "convertalis")
    assert conv[2] == str(g1 / TARGET_DB)  # ... but the request stayed on g1


async def test_status_reports_manifests_and_version(tmp_path):
    root = tmp_path / "db"
    n = _publish(root, "nightly", "nightly-build-20260901T020000Z-DB", count=10)
    binary, _ = fake_mmseqs(tmp_path)
    rt = SimilarityRuntime(make_config(binary, tmp_path, db_mode="snapshot"))
    st = await SimilarityService(None, rt).status()
    assert st["db_mode"] == "snapshot" and st["mmseqs_version"] == "18-8cc5c"
    assert st["nightly"]["generation"] == n.name and st["delta"] is None
    assert st["nightly"]["generation_path"] == str(n)
    assert json.dumps(st)  # serialisable
    broken = SimilarityRuntime(dataclasses.replace(rt.cfg, mmseqs_binary=str(tmp_path / "no")))
    assert (await SimilarityService(None, broken).status())["mmseqs_version"] is None
