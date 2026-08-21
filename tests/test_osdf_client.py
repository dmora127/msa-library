"""OsdfClient: stub fetch/missing/oversize, dispatch, pelican fetch/missing."""

from __future__ import annotations

import glob

import pytest

from app.core.errors import OsdfUnavailable
from app.services.osdf_client import (
    ObjectTooLarge,
    PelicanCliOsdfClient,
    StubOsdfClient,
    build_osdf_client,
)


def _fake_pelican_get(tmp_path, *, content="ABCDEF", rc=0, argv_log=None):
    """A stand-in `pelican` that writes `content` to its LAST arg (the
    destination file) — mirroring `pelican object get <src> <dest>`."""
    script = tmp_path / "pelican"
    log = f'echo "$@" >> {argv_log}\n' if argv_log else ""
    script.write_text(
        "#!/bin/sh\n"
        f"{log}"
        'for dest in "$@"; do :; done\n'      # dest = last positional arg
        f"printf %s '{content}' > \"$dest\"\n"
        f"exit {rc}\n"
    )
    script.chmod(0o755)
    return str(script)


async def test_stub_fetch_ok():
    c = StubOsdfClient({"osdf:///x/a.a3m": b">q\nMK\n"})
    obj = await c.fetch("osdf:///x/a.a3m", max_bytes=1000)
    assert obj.data == b">q\nMK\n" and obj.size == 6


async def test_stub_missing_is_unavailable():
    with pytest.raises(OsdfUnavailable):
        await StubOsdfClient().fetch("osdf:///nope", max_bytes=10)


async def test_stub_oversize():
    c = StubOsdfClient({"u": b"x" * 100})
    with pytest.raises(ObjectTooLarge):
        await c.fetch("u", max_bytes=10)


def test_dispatch():
    assert isinstance(build_osdf_client("stub"), StubOsdfClient)
    assert isinstance(build_osdf_client("pelican"), PelicanCliOsdfClient)
    with pytest.raises(ValueError):
        build_osdf_client("bogus")


async def test_pelican_missing_binary_is_unavailable():
    c = PelicanCliOsdfClient(binary="definitely-not-a-real-binary-xyz")
    with pytest.raises(OsdfUnavailable):
        await c.fetch("osdf:///x", max_bytes=10)


async def test_pelican_directread_downloads_to_tmp_reads_and_cleans_up(tmp_path):
    argv_log = tmp_path / "argv.txt"
    c = PelicanCliOsdfClient(
        binary=_fake_pelican_get(tmp_path, content="A3MDATA", argv_log=argv_log)
    )
    before = set(glob.glob("/tmp/osdf-verify-*"))
    obj = await c.fetch(
        "osdf:///af3-msa-library/community-contributed/x.a3m", max_bytes=1000
    )
    assert obj.data == b"A3MDATA" and obj.size == 7
    # cache-bypassing read, and the object went to a /tmp file (not stdout `-`)
    argv = argv_log.read_text()
    assert "?directread" in argv
    assert "/tmp/osdf-verify-" in argv
    # the temp file is deleted after the read
    assert not (set(glob.glob("/tmp/osdf-verify-*")) - before)


async def test_pelican_get_nonzero_exit_is_unavailable(tmp_path):
    c = PelicanCliOsdfClient(binary=_fake_pelican_get(tmp_path, rc=1))
    before = set(glob.glob("/tmp/osdf-verify-*"))
    with pytest.raises(OsdfUnavailable):
        await c.fetch("osdf:///x.a3m", max_bytes=1000)
    assert not (set(glob.glob("/tmp/osdf-verify-*")) - before)   # cleaned up on failure


async def test_pelican_get_oversize_rejected(tmp_path):
    c = PelicanCliOsdfClient(binary=_fake_pelican_get(tmp_path, content="x" * 100))
    with pytest.raises(ObjectTooLarge):
        await c.fetch("osdf:///x.a3m", max_bytes=10)


def _fake_pelican_get_sleeping(tmp_path, *, sleep=0.3, content="OK"):
    """Like _fake_pelican_get but sleeps before writing the dest — lets us
    exercise concurrency caps and queue timeouts deterministically."""
    script = tmp_path / "pelican"
    script.write_text(
        "#!/bin/sh\n"
        f"sleep {sleep}\n"
        'for dest in "$@"; do :; done\n'
        f"printf %s '{content}' > \"$dest\"\n"
    )
    script.chmod(0o755)
    return str(script)


async def test_pelican_fetch_caps_concurrency(tmp_path):
    # concurrency=2, 4 fetches that each take ~0.3s -> two batches -> ~0.6s,
    # not ~0.3s (no cap) and not ~1.2s (serialized).
    import asyncio as _asyncio
    import time as _time
    c = PelicanCliOsdfClient(
        binary=_fake_pelican_get_sleeping(tmp_path, sleep=0.3),
        concurrency=2, queue_timeout=10.0,
    )
    t0 = _time.monotonic()
    results = await _asyncio.gather(*[
        c.fetch(f"osdf:///x/{i}.a3m", max_bytes=1000) for i in range(4)
    ])
    elapsed = _time.monotonic() - t0
    assert all(r.data == b"OK" for r in results)
    assert 0.55 < elapsed < 1.1, f"expected ~0.6s with concurrency=2, got {elapsed:.2f}s"


async def test_pelican_fetch_queue_timeout_503s(tmp_path):
    # concurrency=1 + slow fetch + tiny queue_timeout -> second caller times out
    # waiting in the queue and gets OsdfUnavailable (503).
    import asyncio as _asyncio
    c = PelicanCliOsdfClient(
        binary=_fake_pelican_get_sleeping(tmp_path, sleep=1.0),
        concurrency=1, queue_timeout=0.2,
    )
    first = _asyncio.create_task(c.fetch("osdf:///a.a3m", max_bytes=1000))
    await _asyncio.sleep(0.05)        # let `first` enter the semaphore
    with pytest.raises(OsdfUnavailable, match="queue full"):
        await c.fetch("osdf:///b.a3m", max_bytes=1000)
    await first                        # let the first one finish + clean up
