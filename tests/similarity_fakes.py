"""Shared helpers for the similarity tests: a fake `mmseqs` shell script and a
SimilarityConfig factory.

The fake dispatches on the subcommand, appends every argv line to a log file,
fabricates the companion files `createdb` would produce, and writes a prepared
.m8 on `convertalis` (optionally a different one per target-DB path substring,
for nightly+delta merge tests). It runs through the REAL
asyncio.create_subprocess_exec path, like tests/test_classifier.py's fake
diamond.
"""

from __future__ import annotations

import dataclasses
import shlex
from datetime import timedelta
from pathlib import Path

from app.core.config import Settings
from app.services.similarity.models import SimilarityConfig, SimilarityHit, build_similarity_config

FAKE_VERSION = "18-8cc5c"


def fake_mmseqs(
    tmp_path: Path,
    *,
    m8: str = "",
    m8_by_target: dict[str, str] | None = None,
    fail_step: str | None = None,
    rc: int = 1,
    sleep_step: str | None = None,
    sleep_seconds: float = 0.0,
    fasta_copy_dir: Path | None = None,
    search_hook: str = "",
    name: str = "mmseqs",
) -> tuple[str, Path]:
    """Write the fake binary under tmp_path; return (binary_path, argv_log)."""
    argv_log = tmp_path / f"{name}.argv.log"
    m8_dir = tmp_path / f"{name}.m8"
    m8_dir.mkdir(exist_ok=True)
    default_m8 = m8_dir / "default.m8"
    default_m8.write_text(m8)
    case_lines = []
    for i, (needle, text) in enumerate((m8_by_target or {}).items()):
        p = m8_dir / f"t{i}.m8"
        p.write_text(text)
        case_lines.append(f'    *{shlex.quote(needle)}*) cat {shlex.quote(str(p))} > "$5" ;;')
    case_lines.append(f'    *) cat {shlex.quote(str(default_m8))} > "$5" ;;')
    copy_cmd = ""
    if fasta_copy_dir is not None:
        fasta_copy_dir.mkdir(parents=True, exist_ok=True)
        copy_cmd = f'cp "$2" {shlex.quote(str(fasta_copy_dir))}/"$(basename "$3")".fa'
    script = f"""#!/bin/sh
printf '%s\\n' "$*" >> {shlex.quote(str(argv_log))}
cmd="$1"
if [ "$cmd" = "{sleep_step or ''}" ]; then exec sleep {sleep_seconds}; fi
if [ "$cmd" = "{fail_step or ''}" ]; then echo "fake failure in $cmd" >&2; exit {rc}; fi
case "$cmd" in
  version)
    echo {FAKE_VERSION} ;;
  createdb)
    cp "$2" "$3"; : > "$3.index"; : > "$3.dbtype"
    {copy_cmd} ;;
  createindex)
    : > "$2.idx.0" ;;
  search)
    {search_hook}
    : > "$4"; : > "$4.index" ;;
  convertalis)
    case "$3" in
{chr(10).join(case_lines)}
    esac ;;
  *)
    echo "unknown subcommand $cmd" >&2; exit 2 ;;
esac
exit 0
"""
    p = tmp_path / name
    p.write_text(script)
    p.chmod(0o755)
    return str(p), argv_log


def argv_lines(argv_log: Path, step: str | None = None) -> list[list[str]]:
    if not argv_log.exists():
        return []
    rows = [line.split(" ") for line in argv_log.read_text().splitlines() if line]
    return [r for r in rows if step is None or r[0] == step]


def make_config(binary: str, tmp_path: Path, **overrides) -> SimilarityConfig:
    """Real build_similarity_config over a Settings, then test-friendly tweaks:
    no isolation switch (the harness session is already in a transaction),
    zero delta overlap (every seeded row shares the transaction's now()),
    private tmp/db roots."""
    settings_kwargs = {
        "similarity_mmseqs_binary": binary,
        "similarity_tmp_root": str(tmp_path / "tmp"),
        "similarity_db_root": str(tmp_path / "db"),
        "similarity_mmseqs_threads": 1,
        "similarity_mmseqs_timeout_seconds": 10.0,
        "similarity_build_timeout_seconds": 10.0,
    }
    cfg_overrides = {}
    for k, v in overrides.items():
        if hasattr(Settings, "model_fields") and f"similarity_{k}" in Settings.model_fields:
            settings_kwargs[f"similarity_{k}"] = v
        else:
            cfg_overrides[k] = v
    (tmp_path / "tmp").mkdir(exist_ok=True)
    cfg = build_similarity_config(Settings(**settings_kwargs))
    return dataclasses.replace(
        cfg,
        build_isolation_level=None,
        delta_overlap=timedelta(0),
        **cfg_overrides,
    )


def m8_row(
    seq_hash: str,
    *,
    fident: float = 0.95,
    qcov: float = 0.98,
    tcov: float = 0.97,
    alnlen: int = 118,
    qlen: int = 120,
    tlen: int = 121,
    bits: float = 200.0,
    evalue: float = 1e-50,
) -> str:
    return "\t".join(
        [seq_hash, f"{fident}", f"{qcov}", f"{tcov}", str(alnlen), str(qlen), str(tlen),
         f"{bits}", f"{evalue}"]
    )


def hit(seq_hash: str, **kw) -> SimilarityHit:
    base = dict(
        seq_hash=seq_hash,
        percent_identity=95.0,
        query_coverage=0.98,
        target_coverage=0.97,
        alignment_length=118,
        query_length=120,
        target_length=121,
        bitscore=200.0,
        evalue=1e-50,
        source_database="ephemeral",
    )
    base.update(kw)
    return SimilarityHit(**base)
