"""Nightly + delta generation builders: promotion, failure isolation, manifest
high-water marks, full-rebuild delta semantics, retention."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.errors import SimilarityBuildFailed
from app.services.similarity.builders import (
    DELTA_LINK,
    MANIFEST,
    NIGHTLY_LINK,
    TARGET_DB,
    DeltaBuilder,
    NightlyBuilder,
    cleanup_generations,
    generation_name,
    parse_generation_ts,
    resolve_generation,
)
from app.services.similarity.mmseqs import MmseqsRunner
from app.services.similarity.models import read_manifest
from tests.similarity_fakes import fake_mmseqs, make_config
from tests.test_query import _seed_entry, _seed_sequence

T0 = datetime(2026, 9, 3, 2, 0, tzinfo=UTC)


def _clock(*times: datetime):
    it = iter(times)
    last = times[-1]
    return lambda: next(it, last)


async def _seed_active(db_session, seq: str) -> tuple[str, int]:
    h, sid = await _seed_sequence(db_session, seq)
    await _seed_entry(db_session, sid, "OSG-Generated", f"osdf:///{h[:8]}.a3m")
    return h, sid


async def _stats(db_session) -> tuple[int, int]:
    row = (await db_session.execute(text(
        "select coalesce(max(id), 0), "
        "(select count(*) from public.msa_sequences s where exists ("
        " select 1 from public.msa_cache_entries e where e.sequence_id = s.id "
        " and e.status = 'active')) from public.msa_sequences"
    ))).one()
    return int(row[0]), int(row[1])


def _headers(fasta: Path) -> set[str]:
    return {line[1:] for line in fasta.read_text().splitlines() if line.startswith(">")}


def test_generation_names_round_trip():
    n = generation_name("nightly", T0)
    d = generation_name("delta", T0 + timedelta(minutes=30))
    assert n == "nightly-build-20260903T020000Z-DB" and d == "delta-20260903T023000Z-DB"
    assert parse_generation_ts(n) == T0 and parse_generation_ts("junk") is None


async def test_nightly_build_promotes_and_writes_manifest(tmp_path, db_session):
    hashes = {(await _seed_active(db_session, s))[0] for s in ("MKTAYIAKQR", "MKTAYIAKQK")}
    hs, sid = await _seed_sequence(db_session, "MKTAYIAKQD")  # no entry -> excluded
    copies = tmp_path / "copies"
    binary, log = fake_mmseqs(tmp_path, fasta_copy_dir=copies)
    cfg = make_config(binary, tmp_path)
    runner = MmseqsRunner(binary)
    manifest, retired = await NightlyBuilder(db_session, runner, cfg, now=_clock(T0)).build()

    gen = cfg.db_root / generation_name("nightly", T0)
    assert manifest.generation == gen.name and retired == []
    assert os.readlink(cfg.db_root / NIGHTLY_LINK) == gen.name  # relative target
    assert resolve_generation(cfg.db_root, NIGHTLY_LINK) == gen.resolve()
    assert (gen / TARGET_DB).exists() and (gen / MANIFEST).exists()
    assert not (gen / "tmp").exists() and not (gen / "target-sequences.fa").exists()
    on_disk = read_manifest(gen / MANIFEST)
    max_id, active = await _stats(db_session)
    assert on_disk == manifest
    assert manifest.postgres_high_water_mark == max_id
    assert manifest.sequence_count == active == 2 and manifest.indexed is True
    assert manifest.mmseqs_version == "18-8cc5c" and manifest.build_type == "nightly"
    datetime.fromisoformat(manifest.snapshot_taken_at)  # parseable
    assert _headers(copies / "targetDB.fa") == hashes
    assert [r[0] for r in argv_lines_of(log)] == ["createdb", "createindex", "version"]


def argv_lines_of(log):
    from tests.similarity_fakes import argv_lines

    return argv_lines(log)


async def test_failed_nightly_leaves_active_generation_untouched(tmp_path, db_session):
    await _seed_active(db_session, "MKTAYIAKQR")
    good, _ = fake_mmseqs(tmp_path, name="good")
    bad, _ = fake_mmseqs(tmp_path, name="bad", fail_step="createindex")
    cfg = make_config(good, tmp_path)
    m1, _ = await NightlyBuilder(db_session, MmseqsRunner(good), cfg, now=_clock(T0)).build()
    t1 = T0 + timedelta(days=1)
    with pytest.raises(SimilarityBuildFailed) as ei:
        await NightlyBuilder(db_session, MmseqsRunner(bad), cfg, now=_clock(t1)).build()
    assert "createindex" in ei.value.message
    assert os.readlink(cfg.db_root / NIGHTLY_LINK) == m1.generation
    assert not (cfg.db_root / generation_name("nightly", t1)).exists()  # partial dir removed
    assert (cfg.db_root / m1.generation / TARGET_DB).exists()


async def test_nightly_with_no_active_sequences_fails_without_promoting(tmp_path, db_session):
    binary, _ = fake_mmseqs(tmp_path)
    cfg = make_config(binary, tmp_path)
    with pytest.raises(SimilarityBuildFailed):
        await NightlyBuilder(db_session, MmseqsRunner(binary), cfg, now=_clock(T0)).build()
    assert not (cfg.db_root / NIGHTLY_LINK).exists()


async def test_delta_is_a_full_rebuild_from_the_nightly_high_water_mark(tmp_path, db_session):
    nightly_hashes = {(await _seed_active(db_session, s))[0]
                      for s in ("MKTAYIAKQR", "MKTAYIAKQK", "MKTAYIAKQL")}
    copies = tmp_path / "copies"
    binary, _ = fake_mmseqs(tmp_path, fasta_copy_dir=copies)
    cfg = make_config(binary, tmp_path)
    runner = MmseqsRunner(binary)
    nightly, _ = await NightlyBuilder(db_session, runner, cfg, now=_clock(T0)).build()
    # "101-120"
    first = {(await _seed_active(db_session, s))[0] for s in ("MKTAYIAKQA", "MKTAYIAKQC")}
    d1, _ = await DeltaBuilder(db_session, runner, cfg,
                               now=_clock(T0 + timedelta(minutes=5))).build()
    assert d1.sequence_count == 2 and _headers(copies / "targetDB.fa") == first
    assert d1.base_nightly_generation == nightly.generation
    assert d1.base_high_water_mark == nightly.postgres_high_water_mark
    assert d1.postgres_high_water_mark > nightly.postgres_high_water_mark
    # "121-130": the NEW delta must contain 101-130, not just 121-130
    second = {(await _seed_active(db_session, s))[0] for s in ("MKTAYIAKQE",)}
    d2, _ = await DeltaBuilder(db_session, runner, cfg,
                               now=_clock(T0 + timedelta(minutes=10))).build()
    assert d2.sequence_count == 3
    assert _headers(copies / "targetDB.fa") == first | second
    assert not (_headers(copies / "targetDB.fa") & nightly_hashes)
    assert os.readlink(cfg.db_root / DELTA_LINK) == d2.generation
    assert d2.indexed is False


async def test_delta_catches_old_sequence_that_newly_gained_an_active_entry(
    tmp_path, db_session
):
    await _seed_active(db_session, "MKTAYIAKQR")
    old_hash, old_id = await _seed_sequence(db_session, "MKTAYIAKQK")  # no entry yet
    copies = tmp_path / "copies"
    binary, _ = fake_mmseqs(tmp_path, fasta_copy_dir=copies)
    cfg = make_config(binary, tmp_path)
    runner = MmseqsRunner(binary)
    n, _ = await NightlyBuilder(db_session, runner, cfg, now=_clock(T0)).build()
    assert n.sequence_count == 1
    # publish an entry for the OLD sequence id (<= high-water mark) after the
    # nightly; the harness's now() is frozen, so stamp it explicitly
    await _seed_entry(db_session, old_id, "GDM", "osdf:///late.a3m")
    await db_session.execute(text(
        "update public.msa_cache_entries set created_at = now() + interval '1 second', "
        "updated_at = now() + interval '1 second' where sequence_id = :sid"
    ), {"sid": old_id})
    later = _clock(T0 + timedelta(minutes=5))
    d, _ = await DeltaBuilder(db_session, runner, cfg, now=later).build()
    assert d.sequence_count == 1 and _headers(copies / "targetDB.fa") == {old_hash}


async def test_empty_delta_is_published_without_a_target_db(tmp_path, db_session):
    await _seed_active(db_session, "MKTAYIAKQR")
    binary, _ = fake_mmseqs(tmp_path)
    cfg = make_config(binary, tmp_path)
    runner = MmseqsRunner(binary)
    await NightlyBuilder(db_session, runner, cfg, now=_clock(T0)).build()
    later = _clock(T0 + timedelta(minutes=5))
    d, _ = await DeltaBuilder(db_session, runner, cfg, now=later).build()
    gen = cfg.db_root / d.generation
    assert d.sequence_count == 0 and d.indexed is False
    assert (gen / MANIFEST).exists() and not (gen / TARGET_DB).exists()
    assert os.readlink(cfg.db_root / DELTA_LINK) == d.generation


async def test_delta_without_nightly_fails_cleanly(tmp_path, db_session):
    binary, _ = fake_mmseqs(tmp_path)
    cfg = make_config(binary, tmp_path)
    with pytest.raises(SimilarityBuildFailed) as ei:
        await DeltaBuilder(db_session, MmseqsRunner(binary), cfg, now=_clock(T0)).build()
    assert "nightly" in ei.value.message
    assert not (cfg.db_root / DELTA_LINK).exists()
    assert list(cfg.db_root.glob("delta-*")) == []


def test_retention_never_deletes_active_and_keeps_recent(tmp_path):
    root = tmp_path / "db"
    root.mkdir()
    now = T0 + timedelta(hours=5)
    old_active = root / generation_name("nightly", T0)  # 5h old but ACTIVE
    old_dead = root / generation_name("nightly", T0 + timedelta(hours=1))  # 4h old
    recent = root / generation_name("nightly", now - timedelta(minutes=30))
    other_type = root / generation_name("delta", T0)  # old, but a delta: not ours
    for d in (old_active, old_dead, recent, other_type):
        d.mkdir()
    (root / NIGHTLY_LINK).symlink_to(old_active.name)
    stale_tmp = root / ".nightly-buildDB.tmp-deadbeef"
    stale_tmp.symlink_to(old_dead.name)
    os.utime(stale_tmp, (0, 0), follow_symlinks=False)
    removed = cleanup_generations(
        root, "nightly", keep={root / NIGHTLY_LINK}, older_than=timedelta(minutes=60), now=now
    )
    assert removed == [old_dead]
    assert old_active.exists() and recent.exists() and other_type.exists()
    assert not stale_tmp.exists()


async def test_nightly_retention_runs_after_promotion(tmp_path, db_session):
    await _seed_active(db_session, "MKTAYIAKQR")
    binary, _ = fake_mmseqs(tmp_path)
    cfg = make_config(binary, tmp_path)
    runner = MmseqsRunner(binary)
    m1, _ = await NightlyBuilder(db_session, runner, cfg, now=_clock(T0)).build()
    t1 = T0 + timedelta(hours=2)
    m2, retired = await NightlyBuilder(db_session, runner, cfg, now=_clock(t1)).build()
    assert [p.name for p in retired] == [m1.generation]
    assert os.readlink(cfg.db_root / NIGHTLY_LINK) == m2.generation
    assert not (cfg.db_root / m1.generation).exists()
    # within retention: previous generation survives for in-flight requests
    t2 = t1 + timedelta(minutes=10)
    m3, retired = await NightlyBuilder(db_session, runner, cfg, now=_clock(t2)).build()
    assert retired == [] and (cfg.db_root / m2.generation).exists()
    assert os.readlink(cfg.db_root / NIGHTLY_LINK) == m3.generation
