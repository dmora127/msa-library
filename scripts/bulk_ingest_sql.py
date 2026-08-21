#!/usr/bin/env python3
"""Generate a single SQL blob for bulk-importing .a3m files into the af3-cache
database via pgweb (or psql). BYPASSES the API verification pipeline — the
server never sees a3m bytes or runs the sha/size/format/seq-match checks.

For each .a3m file the script computes locally:
  - the query sequence (first FASTA record's residues; gaps + lowercase
    insertions stripped; uppercase) -> seq_length + seq_hash
  - file sha256 + file size
  - osdf_uri = <prefix>/<filename>

Output is one transactional script that upserts msa_sequences +
msa_cache_entries (status='active', last_accessed_at=now() — immediately
query-visible). Re-runs are idempotent (ON CONFLICT clauses).

The --requester value is only recorded in a SQL header comment; with the API
path it would also persist on msa_ingestions, but that's skipped here.

Usage:
  python3 scripts/bulk_ingest_sql.py \\
      --dir ./alignments \\
      --uri-prefix osdf:///osg-public/data/dannyTesting \\
      --molecule-type protein \\
      --source GDM \\
      --db-version uniref-2026-01 \\
      --requester danny \\
      --out bulk_ingest.sql
  # then paste bulk_ingest.sql contents into pgweb's Query tab
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ALLOWED_MOL = ("protein", "rna", "dna")


def extract_query_sequence(data: bytes) -> str:
    """First FASTA record's plain residues. Drop gaps ('-', '.'), insertions
    (lowercase), and any non-alpha; uppercase. Matches the server's logic in
    app/services/ingestion_service.py::_query_sequence."""
    text = data.decode("utf-8")
    first: list[str] = []
    seen_header = False
    for ln in text.splitlines():
        if ln.startswith(">"):
            if seen_header:
                break
            seen_header = True
        elif seen_header and ln.strip():
            first.append(ln.strip())
    return "".join(c for c in "".join(first) if c.isalpha()).upper()


def q(s: str) -> str:
    """Single-quoted SQL string literal (doubles embedded quotes)."""
    return "'" + s.replace("'", "''") + "'"


def emit_block(
    *,
    seq_hash: str,
    sequence: str,
    seq_length: int,
    molecule_type: str,
    source: str,
    db_version: str,
    osdf_uri: str,
    size_bytes: int,
    file_sha256: str,
) -> str:
    return f"""\
with upserted as (
  insert into public.msa_sequences (seq_hash, sequence, seq_length, molecule_type)
  values ({q(seq_hash)}, {q(sequence)}, {seq_length}, {q(molecule_type)})
  on conflict (seq_hash) do nothing
  returning id
), seq_row as (
  select id from upserted
  union all
  select id from public.msa_sequences
   where seq_hash = {q(seq_hash)} and not exists (select 1 from upserted)
)
insert into public.msa_cache_entries
  (sequence_id, source, db_version, osdf_uri,
   a3m_size_bytes, a3m_sha256, n_sequences, status, last_accessed_at)
select id, {q(source)}, {q(db_version)},
       {q(osdf_uri)}, {size_bytes}, {q(file_sha256)}, null, 'active', now()
from seq_row
on conflict (sequence_id, source, db_version) do update set
  osdf_uri = excluded.osdf_uri,
  a3m_size_bytes = excluded.a3m_size_bytes,
  a3m_sha256 = excluded.a3m_sha256,
  status = 'active', stale_since = null,
  last_accessed_at = now(), updated_at = now();
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--dir", required=True, type=Path,
                    help="local directory containing .a3m files")
    ap.add_argument("--uri-prefix", required=True,
                    help='e.g. "osdf:///osg-public/data/dannyTesting"')
    ap.add_argument("--molecule-type", required=True, choices=ALLOWED_MOL)
    ap.add_argument("--source", required=True,
                    help="msa_sources.name (must exist + be enabled)")
    ap.add_argument("--db-version", required=True,
                    help="reference-DB bundle version, e.g. uniref-2026-01")
    ap.add_argument("--requester", required=True,
                    help="recorded as a SQL header comment for traceability")
    ap.add_argument("--pattern", default="*.a3m",
                    help='glob inside --dir (default: "*.a3m")')
    ap.add_argument("--out", type=Path, default=None,
                    help="write SQL to file (default: stdout)")
    args = ap.parse_args()

    if not args.dir.is_dir():
        return _die(f"not a directory: {args.dir}")
    files = sorted(p for p in args.dir.glob(args.pattern) if p.is_file())
    if not files:
        return _die(f"no files matching {args.pattern!r} in {args.dir}")
    prefix = args.uri_prefix.rstrip("/")

    out = [
        "-- af3-cache pgweb bulk import (BYPASSES app verification pipeline)",
        f"-- requester:     {args.requester}",
        f"-- source:        {args.source}",
        f"-- db_version:    {args.db_version}",
        f"-- molecule_type: {args.molecule_type}",
        f"-- uri_prefix:    {prefix}/",
        f"-- {len(files)} file(s) from {args.dir}",
        "",
        "begin;",
        "",
    ]

    for path in files:
        data = path.read_bytes()
        size = len(data)
        file_sha = hashlib.sha256(data).hexdigest()
        try:
            seq = extract_query_sequence(data)
        except UnicodeDecodeError:
            return _die(f"{path}: not valid utf-8")
        if not seq:
            return _die(f"{path}: empty/missing first FASTA record")
        seq_hash = hashlib.sha256(seq.encode()).hexdigest()
        uri = f"{prefix}/{path.name}"
        out.append(
            f"-- {path.name}  seq_len={len(seq)}  size={size}  "
            f"file_sha={file_sha[:12]}…"
        )
        out.append(emit_block(
            seq_hash=seq_hash, sequence=seq, seq_length=len(seq),
            molecule_type=args.molecule_type, source=args.source,
            db_version=args.db_version, osdf_uri=uri,
            size_bytes=size, file_sha256=file_sha,
        ))

    out.append("commit;")
    output = "\n".join(out) + "\n"

    if args.out:
        args.out.write_text(output)
        print(
            f"wrote {args.out} — {len(files)} entries, "
            f"{output.count(chr(10))} lines",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(output)
    return 0


def _die(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
