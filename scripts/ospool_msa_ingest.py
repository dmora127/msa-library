#!/usr/bin/env python3
"""Ingest OSPool-generated AF3 alignments into af3-cache (runs on the app host).

Independent of ebi_msa_ingest.py but mirrors it: it creates DB entries via the
admin API and stages the a3m + enqueues it into the SAME upload_queue.jsonl that
pelican_upload_worker.py (phase 2) drains. Point --workdir at the worker's
workdir so the uploads get picked up.

For every file matching <root>/<pattern> (default */inference_inputs/*.tar.gz):
  1. read its AF3 <name>_data.json straight out of the .tar.gz IN MEMORY (no
     extraction to disk, so processing can't fill the volume)
  2. parse the accession from the name: JobN_<accession>.data_pipeline.tar.gz
     -> <accession>  (e.g. Job9_A0A8I5KQE6.data_pipeline.tar.gz -> A0A8I5KQE6)
     and GET https://www.ebi.ac.uk/proteins/api/proteins/<accession> (JSON)
  3. from that JSON: query_name = protein.recommendedName.fullName.value;
        species = organism.names[] where type == "scientific"
     and from AF3's <name>_data.json in the tarball: extract the protein chain's
     unpairedMsa -> write an .a3m; compute sha256 / size / n_sequences; the
     query sequence comes from the same JSON entry.
  4. POST <base>/v1/admin/ingest (admin key) with sequence + that metadata.
        osdf_uri = osdf:///af3-msa-library/alignments/<source>/<accession>.a3m
  5. on success append a record to upload_queue.jsonl (phase 2 uploads it).

UniProt is best-effort: on a miss/5xx, species & query_name default to
"Pending Classification" so BOTH are sent and the server SKIPS its
synchronous diamond classifier (which otherwise runs per-ingest and serializes
throughput). The scheduled reclassify sweep backfills these rows with diamond
later, off the critical path.

Resumable (skips osdf_uris already in upload_queue.jsonl; 409 already_cached
counts as done). ingest_missing.txt records failures. --max-pending applies the
same disk backpressure as the EBI helper.

Admin key: --admin-key  >  $AF3_ADMIN_KEY  >  interactive prompt.

Example:
  python3 scripts/ospool_msa_ingest.py \\
      --root ~/ospool_runs --base-url https://af3-cache.example.org \\
      --requester danny --source OSG-Generated --db-version af3-dbs-2024-12 \\
      --staging /media/volume/alignments_temp --workdir /media/volume/ebi_ingest \\
      --insecure
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import json
import os
import re
import ssl
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

UNIPROT_URL = "https://www.ebi.ac.uk/proteins/api/proteins/{acc}"
OSDF_PREFIX = "osdf:///af3-msa-library/alignments"
# --as-community: publish at the same path real community contributions land at,
# with a uuid5 filename derived from the a3m's sha256 + db_version. That mirrors
# the server's cache identity (content + reference-DB bundle), is portable to
# any uploader (no OSPool-specific concept like accession), and means re-runs
# of the same input are idempotent.
COMMUNITY_OSDF_PREFIX = "osdf:///af3-msa-library/community-contributed"
COMMUNITY_SOURCE = "Community Contributed - Cached"
COMMUNITY_UUID_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # DNS


# ---------------------------------------------------------------- helpers ----
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def count_a3m_sequences(text: str) -> int:
    return sum(1 for ln in text.splitlines() if ln.startswith(">"))


def accession_from_name(name: str) -> str | None:
    """Job9_A0A8I5KQE6.data_pipeline.tar.gz -> A0A8I5KQE6 (after JobN_, before
    .data_pipeline)."""
    m = re.match(r"^Job[^_]*_(.+?)\.data_pipeline\b", name)
    return m.group(1) if m else None


def parse_uniprot_json(body: bytes) -> tuple[str | None, str | None]:
    """Return (query_name, species) from the EBI Proteins API JSON for one
    accession. We request JSON, not XML: EBI's XML serializer 500s on some
    entries (bad keyword in their data) while JSON returns fine.

      query_name = protein.recommendedName.fullName.value
                   (fallback: protein.submittedName[0].fullName.value, TrEMBL)
      species    = organism.names[] where type == 'scientific' -> value
    """
    try:
        d = json.loads(body)
    except Exception:
        return None, None
    if isinstance(d, list):                       # single-accession endpoint is
        d = d[0] if d else {}                     # an object, but be defensive
    if not isinstance(d, dict):
        return None, None
    prot = d.get("protein") or {}
    name = ((prot.get("recommendedName") or {}).get("fullName") or {}).get("value")
    if not name:
        sub = prot.get("submittedName") or []
        if sub:
            name = ((sub[0].get("fullName")) or {}).get("value")
    species = None
    for nm in (d.get("organism") or {}).get("names") or []:
        if isinstance(nm, dict) and nm.get("type") == "scientific" and nm.get("value"):
            species = nm["value"]
            break
    return (name or None), (species or None)


def parse_unpaired_msa(data_bytes: bytes) -> tuple[str | None, str | None]:
    """First protein chain's (unpairedMsa, normalized query sequence) from the
    bytes of an AF3 <name>_data.json. None if absent/empty."""
    try:
        data = json.loads(data_bytes)
    except Exception:
        return None, None
    for entry in data.get("sequences", []):
        prot = entry.get("protein") if isinstance(entry, dict) else None
        if not prot:
            continue
        msa = prot.get("unpairedMsa")
        if isinstance(msa, str) and msa.strip():
            seq = "".join((prot.get("sequence") or "").split()).upper()
            return msa, seq
    return None, None


def read_unpaired_msa_from_tar(
    tar_path: Path, accession: str
) -> tuple[str | None, str | None, str]:
    """Read the AF3 <name>_data.json directly out of the .tar.gz (in memory, no
    extraction to disk) and return (unpairedMsa, normalized query sequence,
    note). `note` is "" on success or a short failure reason for ingest_missing.

    Prefers a *_data.json member whose basename carries the accession; falls
    back to the first one. The whole tarball is never written to disk.
    """
    try:
        with tarfile.open(tar_path, "r:*") as tf:
            members = [m for m in tf.getmembers()
                       if m.isfile() and m.name.lower().endswith("_data.json")]
            if not members:
                return None, None, "no *_data.json member in tarball"
            member = next(
                (m for m in members
                 if accession.lower() in os.path.basename(m.name).lower()),
                members[0],
            )
            fh = tf.extractfile(member)
            if fh is None:
                return None, None, f"could not read member {member.name}"
            data_bytes = fh.read()
    except (tarfile.TarError, OSError, EOFError) as e:
        return None, None, f"tar read failed: {str(e)[:140]}"
    msa, seq = parse_unpaired_msa(data_bytes)
    if not msa or not seq:
        return None, None, "no protein unpairedMsa in data json"
    return msa, seq, ""


def progress(done: int, total: int, extra: str = "") -> None:
    width = 36
    frac = done / total if total else 1.0
    bar = "#" * int(width * frac) + "-" * (width - int(width * frac))
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


def http_get(url: str, *, accept: str, timeout: float, retries: int = 3):
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"Accept": accept})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
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


def upload_done_count(workdir: Path) -> int:
    p = workdir / "upload_done.txt"
    if not p.exists():
        return 0
    with open(p) as fh:
        return sum(1 for ln in fh if ln.strip())


# ----------------------------------------------------------- queue state ----
class State:
    """Same shared queue as ebi_msa_ingest.py (upload_queue.jsonl + missing).

    Thread-safe: `enqueue` and `log_missing` serialize file writes through a
    lock so multiple worker threads (--concurrency > 1) can call them. Reads
    of `self.queued` (set membership / len) are intentionally lock-free —
    Python set operations are atomic enough for the dedup check, and the
    worst case (two workers POSTing the same accession) is caught by the
    server's `409 already_cached`.
    """

    def __init__(self, workdir: Path):
        self.queue_path = workdir / "upload_queue.jsonl"
        self.missing_path = workdir / "ingest_missing.txt"
        self.queued: set[str] = set()
        self._lock = threading.Lock()
        if self.queue_path.exists():
            with open(self.queue_path) as fh:
                for ln in fh:
                    ln = ln.strip()
                    if not ln:
                        continue
                    with contextlib.suppress(Exception):
                        self.queued.add(json.loads(ln)["osdf_uri"])
        self._queue_fh = open(self.queue_path, "a", buffering=1)   # noqa: SIM115
        self._missing_fh = open(self.missing_path, "a", buffering=1)  # noqa: SIM115

    def enqueue(self, osdf_uri, local, source, db_version):
        with self._lock:
            if osdf_uri in self.queued:
                return
            self.queued.add(osdf_uri)
            rec = {"osdf_uri": osdf_uri, "local": str(local),
                   "source": source, "db_version": db_version}
            self._queue_fh.write(json.dumps(rec) + "\n")
            self._queue_fh.flush()
            os.fsync(self._queue_fh.fileno())

    def log_missing(self, key, reason):
        with self._lock:
            self._missing_fh.write(f"{key}\t{reason}\n")
            self._missing_fh.flush()

    def close(self):
        self._queue_fh.close()
        self._missing_fh.close()


# ------------------------------------------------------- per-tarball work ----
def process_one(tar, args, ingest_url, ctx, admin_key, st) -> tuple[str, str]:
    """Do one tarball end-to-end and return (outcome, key). outcome is one of
    'ingested' | 'skipped' | 'failed'; key is the osdf_uri (or the tar path
    on early failures). Safe to call from multiple threads; State serializes
    its own writes."""
    acc = accession_from_name(tar.name)
    if not acc:
        st.log_missing(str(tar), "could not parse accession from name")
        return "failed", str(tar)

    # Default (per-source) URI is accession-based and can be computed up front,
    # so we dedup against the queue before doing any I/O. For --as-community the
    # URI is content-derived (sha + db_version), so we have to compute the sha
    # first; dedup happens just below after we have it.
    if not args.as_community:
        osdf_uri = f"{OSDF_PREFIX}/{args.source}/{acc}.a3m"
        if osdf_uri in st.queued:
            return "skipped", osdf_uri

    # disk backpressure: each worker waits independently. Overshoot is at most
    # args.concurrency files beyond --max-pending, which is acceptable.
    if args.max_pending:
        while (len(st.queued) - upload_done_count(args.workdir)) \
                >= args.max_pending:
            time.sleep(30)

    # 1+2. read AF3 <name>_data.json out of the tarball in memory
    msa_text, sequence, note = read_unpaired_msa_from_tar(tar, acc)
    if note:
        # use the accession as the missing-key for community mode (no URI yet)
        st.log_missing(
            osdf_uri if not args.as_community else acc, note,
        )
        return "failed", acc

    # 3. compute sha/size/n_seq from the bytes (drives the URI in community mode)
    data = msa_text.encode("utf-8")
    sha = sha256_bytes(data)
    size = len(data)
    n_seq = count_a3m_sequences(msa_text)

    # In community mode, finalize the URI now that we have the file's sha.
    if args.as_community:
        ouid = uuid.uuid5(COMMUNITY_UUID_NS, f"{sha}:{args.db_version}")
        osdf_uri = f"{COMMUNITY_OSDF_PREFIX}/{ouid}.a3m"
        if osdf_uri in st.queued:
            return "skipped", osdf_uri
        staged_name = f"{ouid}.a3m"
        staged_dir = "community-contributed"
    else:
        staged_name = f"{acc}.a3m"
        staged_dir = args.source

    # 4. write the a3m to staging
    staged = args.staging / staged_dir / staged_name
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(data)

    # 4. UniProt metadata (best-effort). Default any miss/partial to
    # "Pending Classification" so BOTH fields are sent -> the server skips its
    # synchronous diamond classifier; the reclassify sweep backfills these
    # rows with diamond later, off the critical path.
    code, body = http_get(UNIPROT_URL.format(acc=acc),
                          accept="application/json",
                          timeout=args.uniprot_timeout,
                          retries=args.uniprot_retries)
    query_name = species = None
    if code == 200 and body:
        query_name, species = parse_uniprot_json(body)
    else:
        st.log_missing(osdf_uri, f"uniprot GET status={code} (continuing)")
    species = species or "Pending Classification"
    query_name = query_name or "Pending Classification"

    # 5. POST ingest
    payload = {
        "sequence": sequence,
        "molecule_type": args.molecule_type,
        "source": args.source,
        "db_version": args.db_version,
        "osdf_uri": osdf_uri,
        "declared_size_bytes": size,
        "declared_sha256": sha,
        "requester": args.requester,
        "species": species,
        "query_name": query_name,
        "n_sequences": n_seq,
        "skip_verify": args.skip_verify,
    }
    pcode, resp = http_post_json(ingest_url, payload, admin_key=admin_key,
                                 ctx=ctx, timeout=args.post_timeout)
    status = (resp or {}).get("status")
    err = (resp or {}).get("error", {}) or {}
    if pcode == 200 and status == "available":
        st.enqueue(osdf_uri, staged, args.source, args.db_version)
        return "ingested", osdf_uri
    if pcode == 409 and err.get("code") == "already_cached":
        # Server already has a live cache_entry for (seq, source, db_version).
        # If its osdf_uri matches ours (which it always will in --as-community
        # since we derive deterministically from sha+db_version), enqueue —
        # phase-2's `pelican object put` to the same path is idempotent for
        # identical bytes. If it doesn't match (e.g. legacy data with a
        # different URI scheme), don't enqueue — uploading ours would orphan
        # a file on the origin no cache_entry references.
        if err.get("osdf_uri") == osdf_uri:
            st.enqueue(osdf_uri, staged, args.source, args.db_version)
        else:
            staged.unlink(missing_ok=True)
        return "skipped", osdf_uri
    staged.unlink(missing_ok=True)
    st.log_missing(osdf_uri, f"POST code={pcode} status={status} "
                   f"{err.get('message') or str(resp)[:160]}")
    return "failed", osdf_uri


# ----------------------------------------------------------------- main ----
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--root", type=Path, default=Path.cwd(),
                    help="dir to scan with --pattern")
    ap.add_argument("--pattern", default="*/inference_inputs/*.tar.gz")
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--requester", required=True)
    ap.add_argument("--source", default="OSG-Generated",
                    help="msa_sources.name (must exist + be enabled)")
    ap.add_argument("--as-community", action="store_true",
                    help="publish as community contributions on the user's "
                         "behalf: forces --source to 'Community Contributed - "
                         "Cached' and writes to "
                         "osdf:///af3-msa-library/community-contributed/"
                         "<uuid>.a3m, where <uuid> is a deterministic uuid5 "
                         "derived from the a3m's sha256 + db_version (mirrors "
                         "the server's cache identity; re-runs are idempotent; "
                         "no OSPool-specific input).")
    ap.add_argument("--db-version", required=True,
                    help="reference-DB bundle version used for these MSAs")
    ap.add_argument("--admin-key", default=None)
    ap.add_argument("--staging", type=Path,
                    default=Path("/media/volume/alignments_temp"))
    ap.add_argument("--workdir", type=Path, default=Path.cwd(),
                    help="MUST match the upload worker's --workdir")
    ap.add_argument("--max-pending", type=int, default=500)
    ap.add_argument("--molecule-type", default="protein",
                    choices=("protein", "rna", "dna"))
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--cacert", default=None)
    ap.add_argument("--uniprot-timeout", type=float, default=60.0)
    ap.add_argument("--uniprot-retries", type=int, default=2,
                    help="attempts for the UniProt GET (1 = no retry). Lower it "
                         "during an EBI 5xx storm to avoid backoff stalls; "
                         "classification is deferred to the reclassify sweep "
                         "anyway")
    ap.add_argument("--post-timeout", type=float, default=120.0)
    ap.add_argument("--skip-verify",
                    action=argparse.BooleanOptionalAction, default=True,
                    help="ask the server to publish trusting the declared "
                         "sha/size WITHOUT fetching from OSDF — required when "
                         "the API runs OSDF_CLIENT_IMPL=pelican but the object "
                         "isn't uploaded yet (POST-before-upload). DEFAULT "
                         "TRUE for this two-phase pipeline; pass --no-skip-verify "
                         "only when the object is already on the origin and you "
                         "want server-side verification.")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=4,
                    help="number of concurrent POST/UniProt workers (default 4). "
                         "1 = serial. The API's DB pool can comfortably take 4–8; "
                         "going higher risks UniProt rate-limiting and won't "
                         "speed past the phase-2 worker's upload throughput.")
    args = ap.parse_args()
    args.concurrency = max(1, args.concurrency)
    if args.as_community:
        args.source = COMMUNITY_SOURCE

    admin_key = (args.admin_key or os.environ.get("AF3_ADMIN_KEY")
                 or getpass.getpass("Admin API key: "))
    if not admin_key:
        print("error: no admin key", file=sys.stderr)
        return 2

    # pre-flight: source must exist + be enabled, else every POST 422s
    ctx = ssl_ctx(args.insecure, args.cacert)
    src_url = args.base_url.rstrip("/") + "/v1/admin/sources"
    try:
        req = urllib.request.Request(
            src_url,
            headers={"X-API-Key": admin_key, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            sources = json.loads(r.read() or b"[]")
        match = next(
            (s for s in sources if s.get("name") == args.source), None
        )
        if match is None:
            print(f"error: source {args.source!r} not registered at "
                  f"{args.base_url} (got {sorted(s.get('name') for s in sources)})",
                  file=sys.stderr)
            return 2
        if not match.get("enabled"):
            print(f"error: source {args.source!r} is registered but disabled — "
                  f"flip it on with PUT /v1/admin/sources/{args.source}",
                  file=sys.stderr)
            return 2
    except Exception as e:
        print(f"error: source check failed against {src_url}: {e}",
              file=sys.stderr)
        return 2

    tarballs = sorted(args.root.glob(args.pattern))
    if args.limit:
        tarballs = tarballs[: args.limit]
    total = len(tarballs)
    if total == 0:
        print(f"error: no files matching {args.pattern!r} under {args.root}",
              file=sys.stderr)
        return 2

    args.workdir.mkdir(parents=True, exist_ok=True)
    args.staging.mkdir(parents=True, exist_ok=True)
    ingest_url = args.base_url.rstrip("/") + "/v1/admin/ingest"
    st = State(args.workdir)
    ingested = skipped = failed = 0
    print(f"# {total} tarball(s) under {args.root} | source={args.source} "
          f"db_version={args.db_version} | concurrency={args.concurrency}",
          file=sys.stderr)

    def _tally(outcome: str, key: str, done: int) -> None:
        nonlocal ingested, skipped, failed
        if outcome == "ingested":
            ingested += 1
        elif outcome == "skipped":
            skipped += 1
        else:
            failed += 1
        progress(done, total,
                 f"ok={ingested} skip={skipped} fail={failed} :: "
                 f"…{key[-40:]}")

    try:
        if args.concurrency <= 1:
            for i, tar in enumerate(tarballs, start=1):
                outcome, key = process_one(
                    tar, args, ingest_url, ctx, admin_key, st
                )
                _tally(outcome, key, i)
        else:
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                futs = {
                    pool.submit(
                        process_one, tar, args, ingest_url, ctx, admin_key, st
                    ): tar
                    for tar in tarballs
                }
                for done, fut in enumerate(as_completed(futs), start=1):
                    tar = futs[fut]
                    try:
                        outcome, key = fut.result()
                    except Exception as e:
                        st.log_missing(str(tar), f"worker exception: {e}")
                        _tally("failed", str(tar), done)
                    else:
                        _tally(outcome, key, done)
        sys.stderr.write("\n")
    finally:
        st.close()

    print(f"# DONE: tarballs={total} ingested={ingested} skipped={skipped} "
          f"failed={failed}", file=sys.stderr)
    print(f"# upload queue: {st.queue_path}  (phase 2 uploads it)",
          file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
