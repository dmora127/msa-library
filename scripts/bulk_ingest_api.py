#!/usr/bin/env python3
"""Bulk-import a directory of .a3m files through the af3-cache admin API.

For each file in --dir, the script computes locally:
  - file sha256 + file size
  - query sequence (first FASTA record; gaps + lowercase insertions stripped;
    uppercase) -> seq_length + seq_hash (server also recomputes from `sequence`)
  - osdf_uri = <prefix>/<filename>

Then POSTs to {base_url}/v1/admin/ingest (admin-key gated). The server runs the
real verify+publish pipeline:
  - pelican mode: fetches the object from OSDF, checks sha/size/.a3m
    format/seq-match, publishes (status='available') or rejects with reason.
  - dryrun mode: logs the would-be `pelican object get` and publishes
    trusting the declared sha/size.

Per-file result is printed; exits non-zero if anything failed (HTTP error,
rejected, or 409 already_cached). Re-running is safe: already-cached entries
return 409 with the existing osdf_uri (no duplicates).

Admin key precedence: $AF3_ADMIN_KEY (preferred — keeps key out of argv) ->
--admin-key arg.

Usage:
  AF3_ADMIN_KEY=$(grep '^ADMIN_API_KEY=' deploy/jetstream2/.env.api | cut -d= -f2) \\
  python3 scripts/bulk_ingest_api.py \\
      --base-url https://<api-host-ip>.sslip.io \\
      --dir ./alignments \\
      --uri-prefix osdf:///osg-public/data/dannyTesting \\
      --molecule-type protein \\
      --source GDM \\
      --db-version uniref-2026-01 \\
      --requester danny \\
      --insecure                              # tls internal on the VM
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ALLOWED_MOL = ("protein", "rna", "dna")


def extract_query_sequence(data: bytes) -> str:
    """First FASTA record's plain residues. Drop gaps/insertions/whitespace,
    uppercase. Matches app/services/ingestion_service.py::_query_sequence."""
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


@dataclass
class Result:
    name: str
    http: int
    status: str        # available | rejected | already_cached | error
    reason: str | None
    osdf_uri: str | None


def _ssl_ctx(insecure: bool, cacert: Path | None) -> ssl.SSLContext | None:
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if cacert:
        return ssl.create_default_context(cafile=str(cacert))
    return None  # system default trust


def _post(url: str, body: dict, *, admin_key: str,
          ctx: ssl.SSLContext | None, timeout: float) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-API-Key": admin_key,
        },
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {"raw": raw.decode(errors="replace")[:500]}
        return e.code, body


def _process_file(
    path: Path, *, base_url: str, prefix: str, molecule_type: str,
    source: str, db_version: str, requester: str, admin_key: str,
    ctx: ssl.SSLContext | None, timeout: float, dry_run: bool,
) -> Result:
    name = path.name
    data = path.read_bytes()
    try:
        seq = extract_query_sequence(data)
    except UnicodeDecodeError:
        return Result(name, 0, "error", "not utf-8", None)
    if not seq:
        return Result(name, 0, "error", "no query sequence", None)

    body = {
        "sequence": seq,
        "molecule_type": molecule_type,
        "source": source,
        "db_version": db_version,
        "osdf_uri": f"{prefix}/{name}",
        "declared_size_bytes": len(data),
        "declared_sha256": hashlib.sha256(data).hexdigest(),
        "requester": requester,
    }
    if dry_run:
        print(f"DRY {name}: would POST {json.dumps(body)}")
        return Result(name, 0, "available", None, body["osdf_uri"])

    try:
        code, payload = _post(
            f"{base_url.rstrip('/')}/v1/admin/ingest",
            body, admin_key=admin_key, ctx=ctx, timeout=timeout,
        )
    except Exception as e:  # network/timeout/etc.
        return Result(name, 0, "error", f"transport: {e}", None)

    err = payload.get("error") or {}
    if code == 200:
        status = payload.get("status", "?")
        reason = payload.get("reject_reason")
        uri = payload.get("osdf_uri")
        return Result(name, 200, status, reason, uri)
    if code == 409 and err.get("code") == "already_cached":
        return Result(name, 409, "already_cached", err.get("message"),
                      err.get("osdf_uri"))
    return Result(name, code, "error", err.get("message") or str(payload)[:200],
                  err.get("osdf_uri"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--base-url", required=True,
                    help="e.g. https://<api-host-ip>.sslip.io")
    ap.add_argument("--admin-key", default=None,
                    help="overrides $AF3_ADMIN_KEY (prefer the env var)")
    ap.add_argument("--dir", required=True, type=Path)
    ap.add_argument("--uri-prefix", required=True,
                    help='e.g. "osdf:///osg-public/data/dannyTesting"')
    ap.add_argument("--molecule-type", required=True, choices=ALLOWED_MOL)
    ap.add_argument("--source", required=True)
    ap.add_argument("--db-version", required=True)
    ap.add_argument("--requester", required=True)
    ap.add_argument("--pattern", default="*.a3m")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS verification (use with tls internal)")
    ap.add_argument("--cacert", type=Path, default=None,
                    help="path to a CA pem (e.g. Caddy local root.crt)")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute + print the payloads; don't POST")
    args = ap.parse_args()

    admin_key = args.admin_key or os.environ.get("AF3_ADMIN_KEY", "")
    if not admin_key and not args.dry_run:
        return _die("admin key not set (pass --admin-key or AF3_ADMIN_KEY env)")
    if not args.dir.is_dir():
        return _die(f"not a directory: {args.dir}")
    files = sorted(p for p in args.dir.glob(args.pattern) if p.is_file())
    if not files:
        return _die(f"no files matching {args.pattern!r} in {args.dir}")

    ctx = _ssl_ctx(args.insecure, args.cacert)
    prefix = args.uri_prefix.rstrip("/")

    counts = {"available": 0, "rejected": 0, "already_cached": 0, "error": 0,
              "uploaded": 0}
    print(f"# {len(files)} file(s) -> {args.base_url}/v1/admin/ingest "
          f"(source={args.source} db_version={args.db_version})")
    for path in files:
        r = _process_file(
            path, base_url=args.base_url, prefix=prefix,
            molecule_type=args.molecule_type, source=args.source,
            db_version=args.db_version, requester=args.requester,
            admin_key=admin_key, ctx=ctx, timeout=args.timeout,
            dry_run=args.dry_run,
        )
        counts[r.status] = counts.get(r.status, 0) + 1
        flag = {"available": "OK ", "rejected": "REJ", "already_cached": "DUP",
                "uploaded": "UPL", "error": "ERR"}.get(r.status, "?  ")
        line = f"{flag} {r.name} [{r.http}] {r.status}"
        if r.reason:
            line += f"  reason={r.reason}"
        if r.osdf_uri:
            line += f"  osdf_uri={r.osdf_uri}"
        print(line)

    summary = "  ".join(f"{k}={v}" for k, v in counts.items() if v)
    print(f"# summary: {summary or 'no results'}")
    # exit non-zero if anything wasn't a clean publish (or 409 dup is benign)
    bad = counts["rejected"] + counts["error"] + counts["uploaded"]
    return 1 if bad else 0


def _die(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
