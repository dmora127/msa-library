#!/usr/bin/env python3
"""Phase 2 of EBI MSA ingest: drain staged a3m files to the pelican origin.

Pairs with ebi_msa_ingest.py (phase 1). Phase 1 appends one JSON record per
ingested alignment to <workdir>/upload_queue.jsonl, each carrying the FULL
osdf_uri + the staged local path. This worker reads those records directly --
it does NOT reconstruct the uri or path, and --namespace is used ONLY for the
health probe. This worker:

  * every --interval seconds, probes the origin with
        pelican object ls <namespace>
    If that fails, the origin is considered down -> wait + re-probe.
  * when the origin is up, uploads every queued record whose osdf_uri isn't yet
    in <workdir>/upload_done.txt:
        pelican object put <record.local> <record.osdf_uri>
    On success: record in upload_done.txt + delete the staged file.
  * if any `put` fails, assume the origin hiccupped: STOP this pass and wait for
    the next probe (don't hammer a struggling origin).
  * every --status-interval seconds (default 1s) a live dashboard is rewritten
    to <workdir>/upload_status.txt (queue depth, finished, remaining, in-flight
    file, progress bar) on a background thread, so it stays current even while
    the main loop sleeps between probes. View it with:
        watch -n1 cat <workdir>/upload_status.txt      (best)
        tail -F      <workdir>/upload_status.txt        (note: -F, not -f, since
                                                         the file is replaced)

Resumable + safe to run as a long-lived daemon (re-reads the queue each pass to
pick up new phase-1 additions). Phase 1 only queues a record after its POST
succeeds, so a queued upload always has a matching cache entry.

Example (run under nohup):
  nohup python3 scripts/pelican_upload_worker.py \\
      --workdir /media/volume/ebi_ingest \\
      --pelican /opt/pelican/pelican \\
      --namespace osdf:///af3-msa-library/ \\
      > /media/volume/ebi_ingest/upload_worker.log 2>&1 &
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path


def ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(msg: str) -> None:
    print(f"{ts()} {msg}", flush=True)


def read_queue(workdir: Path) -> list[dict]:
    """Ordered, de-duplicated records from phase 1's upload_queue.jsonl.
    Each record carries the full osdf_uri + staged local path."""
    p = workdir / "upload_queue.jsonl"
    if not p.exists():
        return []
    seen: set[str] = set()
    out: list[dict] = []
    with open(p) as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
                uri = rec["osdf_uri"]
            except Exception:
                continue
            if uri not in seen:
                seen.add(uri)
                out.append(rec)
    return out


def load_done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with open(path) as fh:
        return {ln.strip() for ln in fh if ln.strip()}


def origin_up(pelican: str, namespace: str, timeout: float) -> bool:
    try:
        r = subprocess.run(
            [pelican, "object", "ls", namespace],
            capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log(f"probe: `pelican object ls {namespace}` timed out ({timeout}s)")
        return False
    except FileNotFoundError:
        log(f"probe: pelican binary not found: {pelican}")
        return False
    if r.returncode != 0:
        log(f"probe: origin DOWN (ls rc={r.returncode}) "
            f"{r.stderr.decode(errors='replace').strip()[:160]}")
        return False
    return True


def count_lines(path: Path) -> int:
    """Cheap non-empty line count (queue/done/problem files are line-per-record)."""
    if not path.exists():
        return 0
    try:
        with open(path) as fh:
            return sum(1 for ln in fh if ln.strip())
    except OSError:
        return 0


class Status:
    """Live dashboard rewritten every `refresh` seconds to upload_status.txt.

    Runs on its own daemon thread so the file stays current even while the main
    loop is asleep between probes. The main loop just assigns plain attributes
    (single writer; the thread only reads them) — counts come from re-reading the
    queue/done/problem files each tick, so they track phase-1 additions live.
    The file is written atomically (tmp + os.replace), so a reader never sees a
    half-written snapshot. View with `watch -n1 cat …` or `tail -F …`.
    """

    def __init__(self, workdir: Path, namespace: str, interval: float,
                 refresh: float = 1.0):
        self.path = workdir / "upload_status.txt"
        self.tmp = workdir / "upload_status.txt.tmp"
        self.queue_path = workdir / "upload_queue.jsonl"
        self.done_path = workdir / "upload_done.txt"
        self.problem_path = workdir / "upload_problems.txt"
        self.namespace = namespace
        self.interval = interval
        self.refresh = refresh
        # mutable display state, set by the main loop
        self.phase = "starting"          # starting|idle|probing|uploading|holding
        self.origin_up = False
        self.current_file = ""
        self.pass_done = 0
        self.pass_total = 0
        self.total_uploaded = 0
        self.last_uploaded = ""
        self.next_probe_ts = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        with contextlib.suppress(Exception):
            self._write(self._render())   # one final snapshot on exit

    def _run(self) -> None:
        while not self._stop.wait(self.refresh):
            with contextlib.suppress(Exception):
                self._write(self._render())

    def _write(self, text: str) -> None:
        self.tmp.write_text(text)
        os.replace(self.tmp, self.path)   # atomic swap; readers never tear

    @staticmethod
    def _bar(frac: float, width: int = 40) -> str:
        frac = 0.0 if frac < 0 else 1.0 if frac > 1 else frac
        fill = int(width * frac)
        return "[" + "#" * fill + "-" * (width - fill) + f"] {frac * 100:5.1f}%"

    def _render(self) -> str:
        qtotal = count_lines(self.queue_path)
        done = count_lines(self.done_path)
        errs = count_lines(self.problem_path)
        pending = max(qtotal - done, 0)
        inflight = self.current_file if self.phase == "uploading" else ""
        probe = ""
        if self.phase in ("idle", "holding") and self.next_probe_ts:
            probe = f"next probe in {max(0, int(self.next_probe_ts - time.time()))}s"
        pass_line = (f"   ({self.pass_done}/{self.pass_total} this pass)"
                     if self.pass_total else "")
        return (
            f"af3 pelican upload worker                {ts()}\n"
            f"origin : {self.namespace}\n"
            f"state  : {self.phase:<9}  origin="
            f"{'UP' if self.origin_up else 'DOWN'}   {probe}\n"
            f"\n"
            f"  {self._bar(done / qtotal if qtotal else 0.0)}\n"
            f"\n"
            f"  queue total        : {qtotal}\n"
            f"  finished uploading : {done}\n"
            f"  remaining in queue : {pending}\n"
            f"  uploading now      : {1 if inflight else 0}   {inflight}\n"
            f"  uploaded this run  : {self.total_uploaded}{pass_line}\n"
            f"  last uploaded      : {self.last_uploaded}\n"
            f"  errors logged      : {errs}   (see upload_problems.txt)\n"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--workdir", type=Path, required=True,
                    help="dir holding upload_queue.jsonl (+ upload_done.txt)")
    ap.add_argument("--pelican", default="pelican")
    ap.add_argument("--namespace", default="osdf:///af3-msa-library/",
                    help="probed with `pelican object ls`")
    ap.add_argument("--interval", type=float, default=300.0,
                    help="seconds between probes / passes (default 300 = 5 min)")
    ap.add_argument("--ls-timeout", type=float, default=120.0)
    ap.add_argument("--put-timeout", type=float, default=600.0)
    ap.add_argument("--once", action="store_true",
                    help="single pass then exit (else run forever)")
    ap.add_argument("--status-interval", type=float, default=1.0,
                    help="seconds between upload_status.txt dashboard refreshes")
    args = ap.parse_args()

    done_path = args.workdir / "upload_done.txt"
    problem_path = args.workdir / "upload_problems.txt"
    args.workdir.mkdir(parents=True, exist_ok=True)

    done = load_done(done_path)
    done_fh = open(done_path, "a", buffering=1)          # noqa: SIM115 (daemon)
    prob_fh = open(problem_path, "a", buffering=1)       # noqa: SIM115 (daemon)
    total_uploaded = 0
    status = Status(args.workdir, args.namespace, args.interval,
                    args.status_interval)
    status.total_uploaded = 0
    status.start()
    log(f"worker start workdir={args.workdir} namespace={args.namespace} "
        f"interval={args.interval}s already_uploaded={len(done)} "
        f"status={status.path}")
    try:
        while True:
            queue = read_queue(args.workdir)
            pending = [r for r in queue if r["osdf_uri"] not in done]
            if not pending:
                status.phase = "idle"
                status.current_file = ""
                status.pass_total = 0
                status.next_probe_ts = time.time() + args.interval
                log(f"idle: nothing pending (queue={len(queue)} "
                    f"uploaded={len(done)})")
                if args.once:
                    break
                time.sleep(args.interval)
                continue

            status.phase = "probing"
            if not origin_up(args.pelican, args.namespace, args.ls_timeout):
                status.origin_up = False
                status.phase = "holding"
                status.next_probe_ts = time.time() + args.interval
                log(f"holding off: {len(pending)} pending; re-probe in "
                    f"{args.interval}s")
                if args.once:
                    break
                time.sleep(args.interval)
                continue

            status.origin_up = True
            status.phase = "uploading"
            status.pass_total = len(pending)
            status.pass_done = 0
            log(f"origin UP: draining {len(pending)} pending upload(s)")
            uploaded_this_pass = 0
            interrupted = False
            for rec in pending:
                uri = rec["osdf_uri"]
                local = Path(rec["local"])
                status.current_file = local.name
                if not local.exists():
                    prob_fh.write(f"{uri}\tstaged file missing: {local}\n")
                    prob_fh.flush()
                    log(f"  SKIP (staged file missing): {uri}")
                    continue
                try:
                    pp = subprocess.run(
                        [args.pelican, "object", "put", str(local), uri],
                        capture_output=True, timeout=args.put_timeout,
                    )
                except subprocess.TimeoutExpired:
                    status.phase = "holding"
                    status.current_file = ""
                    log(f"  put TIMEOUT ({args.put_timeout}s) -> origin "
                        f"likely degraded; holding off: {uri}")
                    interrupted = True
                    break
                if pp.returncode != 0:
                    status.phase = "holding"
                    status.current_file = ""
                    log(f"  put FAILED rc={pp.returncode} -> holding off: {uri} "
                        f"{pp.stderr.decode(errors='replace').strip()[:160]}")
                    interrupted = True
                    break
                done.add(uri)
                done_fh.write(uri + "\n")
                done_fh.flush()
                os.fsync(done_fh.fileno())
                local.unlink(missing_ok=True)
                uploaded_this_pass += 1
                total_uploaded += 1
                status.pass_done = uploaded_this_pass
                status.total_uploaded = total_uploaded
                status.last_uploaded = local.name
                status.current_file = ""

            log(f"pass done: uploaded={uploaded_this_pass} "
                f"total={total_uploaded} remaining≈{len(pending) - uploaded_this_pass}"
                + ("  (origin hiccup, paused)" if interrupted else ""))

            if args.once and not interrupted:
                break
            status.phase = "idle"
            status.next_probe_ts = time.time() + args.interval
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("interrupted; exiting")
    finally:
        status.phase = "stopped"
        status.current_file = ""
        status.stop()
        done_fh.close()
        prob_fh.close()

    log(f"worker exit total_uploaded={total_uploaded}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
