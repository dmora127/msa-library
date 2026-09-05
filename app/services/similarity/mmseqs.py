"""Thin async wrapper around the `mmseqs` CLI plus the result-TSV parser.

Every call is an argv list through asyncio.create_subprocess_exec (never a
shell string), with the same discipline as DiamondClassifier / the pelican
client: bounded by a timeout, killed on timeout or cancellation, stderr captured
and truncated in logs. Errors surface as MmseqsError; the service layer maps
them to HTTP.

Identity is read from `fident` (a 0-1 fraction in every MMseqs2 release we
could run) and scaled to 0-100 here, so the parser never depends on which
scale `pident` happens to use in a given build.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from pathlib import Path

from app.core.hashing import is_valid_seq_hash
from app.core.metrics import record_similarity_stage
from app.services.similarity.models import SimilarityHit, SourceDatabase

log = logging.getLogger("af3cache.similarity.mmseqs")

# Column order the parser depends on. Keep in sync with parse_results.
FORMAT_OUTPUT = "target,fident,qcov,tcov,alnlen,qlen,tlen,bits,evalue"
_N_COLS = 9
_STDERR_TAIL = 200


class MmseqsError(Exception):
    """A mmseqs step failed. `reason` is a closed set used as a metrics label:
    binary_missing | timeout | nonzero_exit | parse_error."""

    def __init__(
        self,
        step: str,
        *,
        reason: str,
        returncode: int | None = None,
        stderr_tail: str = "",
    ):
        self.step = step
        self.reason = reason
        self.returncode = returncode
        self.stderr_tail = stderr_tail
        detail = f"mmseqs {step}: {reason}"
        if returncode is not None:
            detail += f" (exit {returncode})"
        if stderr_tail:
            detail += f": {stderr_tail}"
        super().__init__(detail)


@dataclasses.dataclass(frozen=True)
class MmseqsRunner:
    binary: str
    threads: int = 4
    prefilter_mode: int = 1
    alignment_mode: int = 3
    max_candidates: int = 300
    split_memory_limit: str | None = None
    # Fraction-unit prefilters handed to `mmseqs search` so it discards the bulk
    # cheaply; the Python thresholds remain the authoritative decision.
    min_seq_id: float = 0.0
    min_cov: float = 0.0
    timeout: float = 300.0

    def with_timeout(self, timeout: float) -> MmseqsRunner:
        return dataclasses.replace(self, timeout=timeout)

    # --- commands ------------------------------------------------------------

    async def version(self) -> str:
        out = await self._run("version", [self.binary, "version"])
        return out.strip().splitlines()[0] if out.strip() else "unknown"

    async def createdb(self, fasta: Path, db: Path) -> None:
        # --dbtype 1 = amino acid; never let mmseqs guess from a tiny FASTA.
        await self._run(
            "createdb",
            [self.binary, "createdb", str(fasta), str(db), "--dbtype", "1", "-v", "1"],
        )

    async def createindex(self, db: Path, tmp: Path) -> None:
        argv = [
            self.binary, "createindex", str(db), str(tmp),
            "--threads", str(self.threads), "-v", "1",
        ]
        argv += self._mem_args()
        await self._run("createindex", argv)

    async def search(self, query_db: Path, target_db: Path, result_db: Path, tmp: Path) -> None:
        argv = [
            self.binary, "search", str(query_db), str(target_db), str(result_db), str(tmp),
            "--threads", str(self.threads),
            "--prefilter-mode", str(self.prefilter_mode),
            "--alignment-mode", str(self.alignment_mode),
            "--max-seqs", str(self.max_candidates),
            "--min-seq-id", f"{self.min_seq_id:.3f}",
            "-c", f"{self.min_cov:.3f}",
            # cov-mode 0: coverage of BOTH query and target must reach -c,
            # matching the two-sided rule enforced again in ranking.py.
            "--cov-mode", "0",
            "-v", "1",
        ]
        argv += self._mem_args()
        await self._run("search", argv)

    async def convertalis(
        self, query_db: Path, target_db: Path, result_db: Path, out: Path
    ) -> None:
        await self._run(
            "convertalis",
            [
                self.binary, "convertalis",
                str(query_db), str(target_db), str(result_db), str(out),
                "--format-output", FORMAT_OUTPUT,
                "--threads", str(self.threads), "-v", "1",
            ],
        )

    def _mem_args(self) -> list[str]:
        if self.split_memory_limit:
            return ["--split-memory-limit", self.split_memory_limit]
        return []

    # --- subprocess ----------------------------------------------------------

    async def _run(self, step: str, argv: list[str]) -> str:
        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            log.warning("mmseqs %s: binary not found: %s", step, self.binary)
            raise MmseqsError(step, reason="binary_missing") from e
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except TimeoutError as e:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            elapsed = time.monotonic() - t0
            record_similarity_stage(step, elapsed)
            log.warning("mmseqs %s: timed out after %.0fs", step, self.timeout)
            raise MmseqsError(step, reason="timeout") from e
        except asyncio.CancelledError:
            # Client went away mid-search: don't leave a CPU-bound orphan.
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raise
        elapsed = time.monotonic() - t0
        record_similarity_stage(step, elapsed)
        if proc.returncode != 0:
            tail = err.decode(errors="replace").strip()[-_STDERR_TAIL:]
            log.warning("mmseqs %s: exit %d in %.2fs: %s", step, proc.returncode, elapsed, tail)
            raise MmseqsError(
                step, reason="nonzero_exit", returncode=proc.returncode, stderr_tail=tail
            )
        log.info("mmseqs %s: ok in %.2fs", step, elapsed)
        return out.decode(errors="replace")


# --- results -----------------------------------------------------------------


def parse_results(text: str, *, source_database: SourceDatabase) -> list[SimilarityHit]:
    """Parse convertalis output produced with FORMAT_OUTPUT.

    Rows with the wrong column count or a non-seq_hash target are skipped (and
    counted in a warning); a row whose numbers are outside their documented
    ranges is a hard parse_error, because it means the binary is not speaking
    the units we assume and silently continuing would mis-rank everything.
    """
    hits: list[SimilarityHit] = []
    skipped = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        cols = line.split("\t")
        if len(cols) != _N_COLS:
            skipped += 1
            continue
        target = cols[0]
        if not is_valid_seq_hash(target):
            skipped += 1
            continue
        try:
            fident = float(cols[1])
            qcov = float(cols[2])
            tcov = float(cols[3])
            alnlen = int(float(cols[4]))
            qlen = int(float(cols[5]))
            tlen = int(float(cols[6]))
            bits = float(cols[7])
            evalue = float(cols[8])
        except ValueError:
            skipped += 1
            continue
        if not (0.0 <= fident <= 1.0 and 0.0 <= qcov <= 1.0 and 0.0 <= tcov <= 1.0):
            raise MmseqsError(
                "convertalis",
                reason="parse_error",
                stderr_tail=f"out-of-range fident/qcov/tcov for {target[:12]}…",
            )
        hits.append(
            SimilarityHit(
                seq_hash=target,
                percent_identity=fident * 100.0,
                query_coverage=qcov,
                target_coverage=tcov,
                alignment_length=alnlen,
                query_length=qlen,
                target_length=tlen,
                bitscore=bits,
                evalue=evalue,
                source_database=source_database,
            )
        )
    if skipped:
        log.warning("mmseqs convertalis: skipped %d malformed/foreign rows", skipped)
    return hits
