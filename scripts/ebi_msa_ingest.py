#!/usr/bin/env python3
"""Phase 1 of EBI MSA ingest: stage a3m files + create DB entries (NO pelican).

Decoupled from the upload because the pelican origin is intermittently flaky.
This phase only touches EBI + the af3-cache API; pelican_upload_worker.py
(phase 2) drains the staged files to the origin when it's healthy.

For each row of a TSV with columns:
    Entry  Entry Name  Organism  Gene Names (primary)  Sequence

it:
  1. GET https://alphafold.ebi.ac.uk/api/prediction/<Entry>  (accept: json)
  2. appends the response into an aggregating swissprot_ebi.json
  3. for each prediction with msaUrl + providerId:
       a. wget the msaUrl -> <staging>/<providerId>/<file>.a3m  (kept on disk)
       b. POST <base>/v1/admin/ingest (admin key) with sequence + metadata
          (species, query_name, db_version, n_sequences, sha256, size).
          osdf_uri = osdf:///af3-msa-library/alignments/<providerId>/<file>
       c. on success, append a record to upload_queue.jsonl carrying the FULL
          osdf_uri + the staged local path. That file IS the upload queue +
          phase 1's resume marker; phase 2 reads the records directly (it never
          reconstructs the uri/path) and uploads any whose osdf_uri isn't yet in
          upload_done.txt.

Resumable: upload_queue.jsonl is consulted on startup; already-queued
alignments are skipped (not re-downloaded/re-POSTed). 409 already_cached counts
as done. ingest_missing.txt records failures. A record is written only AFTER
the POST succeeds.

Backpressure: staging shares the volume with Postgres, so phase 1 pauses when
the pending (ingested-but-not-uploaded) count reaches --max-pending, letting
phase 2 drain. WATCH `df` — if phase 2 isn't running, phase 1 will stall here.

Admin key precedence: --admin-key  >  $AF3_ADMIN_KEY  >  interactive prompt.

Example:
  python3 scripts/ebi_msa_ingest.py \\
      --tsv swissprot.tsv \\
      --base-url https://af3-cache.example.org \\
      --requester danny \\
      --staging /media/volume/alignments_temp \\
      --workdir /media/volume/ebi_ingest \\
      --insecure          # only while the API uses Caddy's tls internal
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import getpass
import hashlib
import json
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit

EBI_PREDICTION_URL = "https://alphafold.ebi.ac.uk/api/prediction/{entry}"
OSDF_PREFIX = "osdf:///af3-msa-library/alignments"


# ---------------------------------------------------------------- helpers ----
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def count_a3m_sequences(path: Path) -> int:
    n = 0
    with open(path, "rb") as fh:
        for line in fh:
            if line.startswith(b">"):
                n += 1
    return n


def a3m_filename(msa_url: str) -> str:
    return os.path.basename(urlsplit(msa_url).path) or "alignment.a3m"


def upload_done_count(workdir: Path) -> int:
    """Lines in phase 2's upload_done.txt (0 if it doesn't exist yet)."""
    p = workdir / "upload_done.txt"
    if not p.exists():
        return 0
    with open(p) as fh:
        return sum(1 for ln in fh if ln.strip())


def progress(done: int, total: int, extra: str = "") -> None:
    width = 36
    frac = done / total if total else 1.0
    filled = int(width * frac)
    bar = "#" * filled + "-" * (width - filled)
    sys.stderr.write(f"\r[{bar}] {done}/{total} ({frac*100:5.1f}%) {extra}   ")
    sys.stderr.flush()


def ssl_ctx(insecure: bool, cacert: str | None) -> ssl.SSLContext | None:
    if insecure:
        c = ssl.create_default_context()
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        return c
    if cacert:
        return ssl.create_default_context(cafile=cacert)
    return None


def http_get_json(url: str, *, timeout: float, retries: int = 3):
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 * attempt)
                continue
            return e.code, None
        except Exception:
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
            return 0, None
    return 0, None


def http_post_json(url, body, *, admin_key, ctx, timeout):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "X-API-Key": admin_key},
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except Exception:
            return e.code, {"raw": raw.decode(errors="replace")[:300]}
    except Exception as e:
        return 0, {"transport": str(e)}


# ---------------------------------------------------------- state files ----
class State:
    """Thread-safe: `enqueue`, `log_missing`, and `aggregate_set` serialize file
    writes through a lock so worker threads (--concurrency > 1) can call them.
    Reads of `self.queued` are lock-free — set membership is atomic enough; the
    rare two-workers-pick-same-uri race is caught by the server's `409
    already_cached` and both workers still enqueue idempotently."""

    _AGGREGATE_SAVE_EVERY = 25       # cheapen restarts without thrashing disk

    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.agg_path = workdir / "swissprot_ebi.json"
        # the upload queue: one JSON record per successfully-ingested alignment,
        # carrying the FULL osdf_uri + the staged local path so phase 2 needs no
        # reconstruction. Doubles as phase 1's resume marker.
        self.queue_path = workdir / "upload_queue.jsonl"
        self.missing_path = workdir / "ingest_missing.txt"
        self.aggregate: dict = {}
        self._lock = threading.Lock()
        self._agg_dirty_count = 0
        if self.agg_path.exists():
            try:
                self.aggregate = json.loads(self.agg_path.read_text())
            except Exception:
                self.aggregate = {}
        self.queued: set[str] = set()   # osdf_uris already enqueued
        if self.queue_path.exists():
            with open(self.queue_path) as fh:
                for ln in fh:
                    ln = ln.strip()
                    if not ln:
                        continue
                    with contextlib.suppress(Exception):
                        self.queued.add(json.loads(ln)["osdf_uri"])
        # long-lived append logs (closed in .close()); not a with-block fit
        self._queue_fh = open(self.queue_path, "a", buffering=1)  # noqa: SIM115
        self._missing_fh = open(self.missing_path, "a", buffering=1)  # noqa: SIM115

    def enqueue(self, osdf_uri: str, local: Path, source: str,
                db_version: str) -> None:
        with self._lock:
            if osdf_uri in self.queued:
                return
            self.queued.add(osdf_uri)
            rec = {"osdf_uri": osdf_uri, "local": str(local),
                   "source": source, "db_version": db_version}
            self._queue_fh.write(json.dumps(rec) + "\n")
            self._queue_fh.flush()
            os.fsync(self._queue_fh.fileno())

    def log_missing(self, key: str, reason: str) -> None:
        with self._lock:
            self._missing_fh.write(f"{key}\t{reason}\n")
            self._missing_fh.flush()

    def aggregate_set(self, entry: str, preds: list) -> None:
        with self._lock:
            self.aggregate[entry] = preds
            self._agg_dirty_count += 1
            if self._agg_dirty_count >= self._AGGREGATE_SAVE_EVERY:
                self._save_aggregate_locked()
                self._agg_dirty_count = 0

    def save_aggregate(self) -> None:
        with self._lock:
            self._save_aggregate_locked()

    def _save_aggregate_locked(self) -> None:
        # Caller must hold self._lock.
        tmp = self.agg_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.aggregate))
        tmp.replace(self.agg_path)

    def close(self) -> None:
        self.save_aggregate()
        self._queue_fh.close()
        self._missing_fh.close()


# ----------------------------------------------------------- per-row work ----
def process_one(row, args, ingest_url, ctx, admin_key, st) -> tuple[int, int, int, str]:
    """One TSV row -> (ingested, skipped, failed, entry). A single row can
    contribute multiple counts because one EBI entry may yield multiple
    predictions, each of which is its own POST. Safe to call concurrently."""
    entry = (row.get("Entry") or "").strip()
    sequence = (row.get("Sequence") or "").strip().upper()
    if not entry or not sequence:
        st.log_missing(entry or "<blank>", "missing Entry/Sequence in TSV")
        return 0, 0, 1, entry or "<blank>"

    # 1. EBI prediction (cached in aggregate to make restarts cheap)
    preds = st.aggregate.get(entry)            # lock-free read; dict get is atomic
    if preds is None:
        code, preds = http_get_json(
            EBI_PREDICTION_URL.format(entry=entry), timeout=args.ebi_timeout,
        )
        if code != 200 or not isinstance(preds, list):
            st.log_missing(entry, f"ebi GET status={code}")
            return 0, 0, 1, entry
        st.aggregate_set(entry, preds)         # locked + periodic save
    if not preds:
        st.log_missing(entry, "ebi returned no predictions")
        return 0, 0, 1, entry

    ingested = skipped = failed = 0
    any_msa = False
    for pred in preds:
        msa_url = pred.get("msaUrl")
        provider = pred.get("providerId")
        if not msa_url or not provider:
            continue
        any_msa = True
        fname = a3m_filename(msa_url)
        osdf_uri = f"{OSDF_PREFIX}/{provider}/{fname}"
        if osdf_uri in st.queued:
            skipped += 1
            continue

        # disk backpressure: each worker waits independently. Overshoot is
        # bounded by args.concurrency files past --max-pending; acceptable.
        if args.max_pending:
            while (len(st.queued) - upload_done_count(args.workdir)) \
                    >= args.max_pending:
                time.sleep(30)

        # stage to <staging>/<provider>/<file>
        staged = args.staging / provider / fname
        staged.parent.mkdir(parents=True, exist_ok=True)
        rc = subprocess.run(
            ["wget", "-q", "-O", str(staged), msa_url], capture_output=True,
        )
        if rc.returncode != 0 or not staged.exists() \
                or staged.stat().st_size == 0:
            st.log_missing(
                osdf_uri,
                f"wget failed rc={rc.returncode} "
                f"{rc.stderr.decode(errors='replace')[:120]}",
            )
            if staged.exists():
                staged.unlink()
            failed += 1
            continue

        size = staged.stat().st_size
        sha = sha256_file(staged)
        n_seq = count_a3m_sequences(staged)
        db_version = " ".join(
            x for x in (pred.get("toolUsed"),
                        pred.get("modelCreatedDate")) if x
        ) or "unknown"
        species = pred.get("organismScientificName") or None
        qname = " ".join(
            x for x in (pred.get("uniprotAccession"),
                        pred.get("uniprotDescription")) if x
        ) or None

        payload = {
            "sequence": sequence,
            "molecule_type": args.molecule_type,
            "source": provider,
            "db_version": db_version,
            "osdf_uri": osdf_uri,
            "declared_size_bytes": size,
            "declared_sha256": sha,
            "requester": args.requester,
            "species": species,
            "query_name": qname,
            "n_sequences": n_seq,
            "skip_verify": args.skip_verify,
        }
        pcode, resp = http_post_json(
            ingest_url, payload, admin_key=admin_key, ctx=ctx,
            timeout=args.post_timeout,
        )
        status = (resp or {}).get("status")
        err = (resp or {}).get("error", {}) or {}
        if pcode == 200 and status == "available":
            st.enqueue(osdf_uri, staged, provider, db_version)
            ingested += 1
        elif pcode == 409 and err.get("code") == "already_cached":
            st.enqueue(osdf_uri, staged, provider, db_version)
            skipped += 1
        else:
            if staged.exists():
                staged.unlink()
            st.log_missing(
                osdf_uri,
                f"POST code={pcode} status={status} "
                f"{err.get('message') or str(resp)[:160]}",
            )
            failed += 1

    if not any_msa:
        st.log_missing(entry, "no prediction had msaUrl/providerId")
        failed += 1

    return ingested, skipped, failed, entry


# ----------------------------------------------------------------- main ----
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--tsv", required=True, type=Path)
    ap.add_argument("--base-url", required=True, help="af3-cache API base URL")
    ap.add_argument("--requester", required=True)
    ap.add_argument("--admin-key", default=None,
                    help="overrides $AF3_ADMIN_KEY; omit to be prompted")
    ap.add_argument("--workdir", type=Path, default=Path.cwd(),
                    help="where the state/log files live")
    ap.add_argument("--staging", type=Path,
                    default=Path("/media/volume/alignments_temp"),
                    help="where downloaded a3m files are staged for phase 2")
    ap.add_argument("--max-pending", type=int, default=500,
                    help="pause when this many alignments are ingested but not "
                         "yet uploaded (disk backpressure; 0 = no limit)")
    ap.add_argument("--molecule-type", default="protein",
                    choices=("protein", "rna", "dna"))
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS verify (Caddy tls internal)")
    ap.add_argument("--cacert", default=None, help="CA pem for TLS verify")
    ap.add_argument("--ebi-timeout", type=float, default=60.0)
    ap.add_argument("--post-timeout", type=float, default=120.0)
    ap.add_argument("--skip-verify", action="store_true",
                    help="ask the server to publish trusting the declared "
                         "sha/size WITHOUT fetching from OSDF (needed when the "
                         "API runs OSDF_CLIENT_IMPL=pelican but the object isn't "
                         "uploaded yet — POST-before-upload). Admin-key gated.")
    ap.add_argument("--limit", type=int, default=0,
                    help="process only the first N TSV rows (0 = all)")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="number of concurrent TSV-row workers (default 4). "
                         "1 = serial. The API's DB pool can comfortably take "
                         "4–8; going higher risks EBI rate-limiting and won't "
                         "speed past the phase-2 worker's upload throughput.")
    args = ap.parse_args()
    args.concurrency = max(1, args.concurrency)

    admin_key = (
        args.admin_key
        or os.environ.get("AF3_ADMIN_KEY")
        or getpass.getpass("Admin API key: ")
    )
    if not admin_key:
        print("error: no admin key", file=sys.stderr)
        return 2
    if not args.tsv.is_file():
        print(f"error: TSV not found: {args.tsv}", file=sys.stderr)
        return 2

    args.workdir.mkdir(parents=True, exist_ok=True)
    args.staging.mkdir(parents=True, exist_ok=True)
    ctx = ssl_ctx(args.insecure, args.cacert)
    ingest_url = args.base_url.rstrip("/") + "/v1/admin/ingest"

    with open(args.tsv, newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    if args.limit:
        rows = rows[: args.limit]
    total = len(rows)
    if total == 0:
        print("error: no rows in TSV", file=sys.stderr)
        return 2

    st = State(args.workdir)
    ingested = skipped = failed = 0
    print(f"# {total} entries | base={args.base_url} | workdir={args.workdir}"
          f" | concurrency={args.concurrency}", file=sys.stderr)

    def _tally(ing: int, sk: int, fl: int, key: str, done: int) -> None:
        nonlocal ingested, skipped, failed
        ingested += ing
        skipped += sk
        failed += fl
        progress(done, total,
                 f"ingested={ingested} skip={skipped} fail={failed} :: {key}")

    try:
        if args.concurrency <= 1:
            for i, row in enumerate(rows, start=1):
                ing, sk, fl, key = process_one(
                    row, args, ingest_url, ctx, admin_key, st
                )
                _tally(ing, sk, fl, key, i)
        else:
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                futs = {
                    pool.submit(
                        process_one, row, args, ingest_url, ctx, admin_key, st
                    ): row
                    for row in rows
                }
                for done, fut in enumerate(as_completed(futs), start=1):
                    row = futs[fut]
                    try:
                        ing, sk, fl, key = fut.result()
                    except Exception as e:
                        entry = (row.get("Entry") or "<blank>")
                        st.log_missing(entry, f"worker exception: {e}")
                        _tally(0, 0, 1, entry, done)
                    else:
                        _tally(ing, sk, fl, key, done)
        sys.stderr.write("\n")
    finally:
        st.close()

    print(f"# DONE: entries={total} ingested={ingested} skipped={skipped} "
          f"failed/missing={failed}", file=sys.stderr)
    print(f"# upload queue: {st.queue_path}", file=sys.stderr)
    print(f"# missing-log: {st.missing_path}", file=sys.stderr)
    print(f"# ebi cache:   {st.agg_path}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
