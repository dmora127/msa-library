"""MmseqsRunner argv construction, subprocess failure modes, and the parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.similarity.mmseqs import (
    FORMAT_OUTPUT,
    MmseqsError,
    MmseqsRunner,
    parse_results,
)
from tests.similarity_fakes import FAKE_VERSION, argv_lines, fake_mmseqs, m8_row

H1 = "1" * 64
H2 = "2" * 64


def _runner(binary: str, **kw) -> MmseqsRunner:
    base = dict(threads=3, prefilter_mode=1, alignment_mode=3, max_candidates=77,
                split_memory_limit=None, min_seq_id=0.89, min_cov=0.89, timeout=5.0)
    base.update(kw)
    return MmseqsRunner(binary, **base)


async def test_argv_per_step(tmp_path: Path):
    binary, log = fake_mmseqs(tmp_path)
    r = _runner(binary, split_memory_limit="1G")
    fa = tmp_path / "q.fa"
    fa.write_text(">q\nMKT\n")
    await r.createdb(fa, tmp_path / "qDB")
    await r.createindex(tmp_path / "qDB", tmp_path / "tmpi")
    await r.search(tmp_path / "qDB", tmp_path / "tDB", tmp_path / "rDB", tmp_path / "tmps")
    await r.convertalis(tmp_path / "qDB", tmp_path / "tDB", tmp_path / "rDB", tmp_path / "o.m8")
    assert await r.version() == FAKE_VERSION

    (createdb,) = argv_lines(log, "createdb")
    assert createdb[1:3] == [str(fa), str(tmp_path / "qDB")] and "--dbtype" in createdb
    assert createdb[createdb.index("--dbtype") + 1] == "1"
    (createindex,) = argv_lines(log, "createindex")
    assert createindex[1:3] == [str(tmp_path / "qDB"), str(tmp_path / "tmpi")]
    assert createindex[createindex.index("--split-memory-limit") + 1] == "1G"
    (search,) = argv_lines(log, "search")
    assert search[1:5] == [str(tmp_path / p) for p in ("qDB", "tDB", "rDB", "tmps")]
    flags = dict(zip(search[5::2], search[6::2], strict=False))
    assert flags["--prefilter-mode"] == "1" and flags["--alignment-mode"] == "3"
    assert flags["--max-seqs"] == "77" and flags["--threads"] == "3"
    assert flags["--min-seq-id"] == "0.890" and flags["-c"] == "0.890"
    assert flags["--cov-mode"] == "0" and flags["--split-memory-limit"] == "1G"
    (conv,) = argv_lines(log, "convertalis")
    assert conv[conv.index("--format-output") + 1] == FORMAT_OUTPUT
    assert conv[4] == str(tmp_path / "o.m8")
    # no split-memory flag unless configured
    binary2, log2 = fake_mmseqs(tmp_path, name="mmseqs2")
    await _runner(binary2).search(tmp_path / "a", tmp_path / "b", tmp_path / "c", tmp_path / "d")
    assert "--split-memory-limit" not in argv_lines(log2, "search")[0]


async def test_nonzero_exit_raises_with_stderr_tail(tmp_path: Path):
    binary, _ = fake_mmseqs(tmp_path, fail_step="search", rc=3)
    with pytest.raises(MmseqsError) as ei:
        await _runner(binary).search(tmp_path / "a", tmp_path / "b", tmp_path / "c", tmp_path / "d")
    e = ei.value
    assert e.step == "search" and e.reason == "nonzero_exit" and e.returncode == 3
    assert "fake failure in search" in e.stderr_tail


async def test_missing_binary(tmp_path: Path):
    with pytest.raises(MmseqsError) as ei:
        await _runner(str(tmp_path / "nope")).version()
    assert ei.value.reason == "binary_missing"


async def test_timeout_kills_child(tmp_path: Path):
    binary, _ = fake_mmseqs(tmp_path, sleep_step="search", sleep_seconds=5)
    with pytest.raises(MmseqsError) as ei:
        await _runner(binary, timeout=0.3).search(
            tmp_path / "a", tmp_path / "b", tmp_path / "c", tmp_path / "d"
        )
    assert ei.value.reason == "timeout"


def test_parse_results_scales_fident_to_percent():
    text = "\n".join([m8_row(H1, fident=0.985, qcov=0.99, tcov=0.97), m8_row(H2, fident=1.0)])
    hits = parse_results(text + "\n\n", source_database="nightly")
    assert [h.seq_hash for h in hits] == [H1, H2]
    assert hits[0].percent_identity == pytest.approx(98.5)
    assert hits[0].query_coverage == 0.99 and hits[0].target_coverage == 0.97
    assert hits[0].alignment_length == 118 and hits[0].query_length == 120
    assert hits[0].target_length == 121 and hits[0].bitscore == 200.0
    assert hits[0].evalue == 1e-50 and hits[0].source_database == "nightly"
    assert hits[1].percent_identity == 100.0


def test_parse_results_skips_malformed_and_foreign_rows():
    text = "\n".join([
        "not\tenough\tcolumns",
        m8_row("query_not_a_hash"),  # foreign header
        m8_row(H1).replace("118", "abc"),  # non-numeric
        m8_row(H2),
    ])
    hits = parse_results(text, source_database="ephemeral")
    assert [h.seq_hash for h in hits] == [H2]


@pytest.mark.parametrize("bad", [
    m8_row(H1, fident=1.5),  # would be 150 % identity
    m8_row(H1, qcov=1.2),
    m8_row(H1, tcov=-0.1),
])
def test_parse_results_out_of_range_is_hard_error(bad):
    with pytest.raises(MmseqsError) as ei:
        parse_results(bad, source_database="ephemeral")
    assert ei.value.reason == "parse_error"
