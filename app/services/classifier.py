"""Sequence classification (species + query_name) for the verify pipeline.

DiamondClassifier runs:

    printf '>query\\n%s\\n' "$SEQUENCE" \\
      | diamond blastp -d <db> -q - --fast \\
          --id <ID_THRESHOLD> --query-cover <QUERY_COVER> \\
          --max-target-seqs 1 \\
          -f 6 qseqid sseqid pident length mismatch sscinames stitle evalue

Identity + coverage are enforced by diamond (`--id` / `--query-cover`), so any
row that comes out already meets those gates. The parser only re-checks
`evalue < EVALUE_THRESHOLD` and, on pass, sets species=sscinames + query_name=
stitle; otherwise -> 'Unclassified'.

Classification is metadata enrichment, never integrity-critical: any failure
(binary missing, db missing, timeout, no hits, non-zero exit) -> 'Unclassified'
(the upload still publishes). NoopClassifier is the default impl so deploys
without diamond keep working (everything publishes as 'Unclassified' until
CLASSIFIER_IMPL=diamond is set).
"""

from __future__ import annotations

import abc
import asyncio
import logging
from dataclasses import dataclass

from app.core.config import Settings

log = logging.getLogger("af3cache.classifier")

DEFAULT_ID_THRESHOLD = 99.99
DEFAULT_QUERY_COVER = 100.0
DEFAULT_EVALUE_THRESHOLD = 0.0005

_UNCLASSIFIED_LABEL = "Unclassified"


@dataclass(frozen=True)
class Classification:
    species: str
    query_name: str


UNCLASSIFIED = Classification(
    species=_UNCLASSIFIED_LABEL, query_name=_UNCLASSIFIED_LABEL
)


class Classifier(abc.ABC):
    @abc.abstractmethod
    async def classify(self, sequence: str) -> Classification: ...


class NoopClassifier(Classifier):
    """Always returns Unclassified. Default — keeps the pipeline working when
    diamond / swissprot aren't installed on the host."""

    async def classify(self, sequence: str) -> Classification:
        return UNCLASSIFIED


class StubClassifier(Classifier):
    """Returns a fixed Classification — for tests."""

    def __init__(self, result: Classification = UNCLASSIFIED):
        self.result = result

    async def classify(self, sequence: str) -> Classification:
        return self.result


class DiamondClassifier(Classifier):
    """Runs `diamond blastp` against a swissprot DB. Reads the query FASTA via
    stdin (no temp file). Output format -f 6 with columns:
    qseqid sseqid pident length mismatch sscinames stitle evalue.
    Identity + query coverage are enforced by diamond's own flags; the parser
    only re-checks evalue."""

    def __init__(
        self,
        *,
        binary: str,
        db: str,
        timeout: float = 60.0,
        id_threshold: float = DEFAULT_ID_THRESHOLD,
        query_cover: float = DEFAULT_QUERY_COVER,
        evalue_threshold: float = DEFAULT_EVALUE_THRESHOLD,
    ):
        self.binary = binary
        self.db = db
        self.timeout = timeout
        self.id_threshold = id_threshold
        self.query_cover = query_cover
        self.evalue_threshold = evalue_threshold

    async def classify(self, sequence: str) -> Classification:
        if not sequence:
            return UNCLASSIFIED
        fasta = f">query\n{sequence}\n".encode()
        argv = [
            self.binary, "blastp",
            "-d", self.db,
            "-q", "-",
            "--fast",
            "--id", str(self.id_threshold),
            "--query-cover", str(self.query_cover),
            "--max-target-seqs", "1",
            "-f", "6", "qseqid", "sseqid", "pident", "length", "mismatch",
            "sscinames", "stitle", "evalue",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            log.warning("classify: diamond binary not found: %s", self.binary)
            return UNCLASSIFIED
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(input=fasta), timeout=self.timeout
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("classify: diamond timed out after %ss", self.timeout)
            return UNCLASSIFIED
        if proc.returncode != 0:
            log.warning(
                "classify: diamond exit %d: %s",
                proc.returncode,
                err.decode(errors="replace").strip()[:200],
            )
            return UNCLASSIFIED
        return self._parse(out.decode(errors="replace"))

    def _parse(self, output: str) -> Classification:
        # Any row reaching here already passed diamond's --id + --query-cover
        # gates; we only re-check evalue strictly less than the threshold.
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            cols = line.split("\t")
            if len(cols) < 8:
                continue
            try:
                evalue = float(cols[7])
            except ValueError:
                continue
            if evalue < self.evalue_threshold:
                return Classification(species=cols[5], query_name=cols[6])
            return UNCLASSIFIED
        return UNCLASSIFIED


def build_classifier(settings: Settings) -> Classifier:
    impl = settings.classifier_impl.lower()
    if impl == "noop":
        return NoopClassifier()
    if impl == "stub":
        return StubClassifier()
    if impl == "diamond":
        if not settings.diamond_db:
            raise ValueError(
                "CLASSIFIER_IMPL=diamond requires DIAMOND_DB to be set"
            )
        return DiamondClassifier(
            binary=settings.diamond_binary,
            db=settings.diamond_db,
            timeout=settings.diamond_timeout_seconds,
            id_threshold=settings.diamond_id_threshold,
            query_cover=settings.diamond_query_cover,
            evalue_threshold=settings.diamond_evalue_threshold,
        )
    raise ValueError(f"unknown CLASSIFIER_IMPL: {settings.classifier_impl!r}")
