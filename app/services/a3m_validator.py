"""Lenient .a3m sniff-test.

a3m semantics: uppercase letters and '-' are MATCH-state columns; lowercase
letters and '.' are INSERTION states (variable per row). A well-formed a3m has
the same number of match columns in every record. This check is intentionally
lenient — it accepts real jackhmmer/MMseqs2 a3m (lowercase insertions, gaps)
but rejects non-FASTA / truncated / garbage uploads. v0.1 heuristic, tunable.

Reasons: bad_format:non_utf8 | empty | no_header | dangling_header |
bad_alphabet | ragged_alignment
"""

from __future__ import annotations

from dataclasses import dataclass

_MATCH_EXTRA = {"-", "*"}
_INSERTION_EXTRA = {"."}


class A3mInvalid(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = f"bad_format:{reason}"


@dataclass(frozen=True)
class A3mInfo:
    n_sequences: int
    match_columns: int


def _match_columns(seq: str) -> int:
    return sum(1 for c in seq if c == "-" or c == "*" or (c.isalpha() and c.isupper()))


def _valid_alphabet(seq: str) -> bool:
    return all(
        c.isalpha() or c in _MATCH_EXTRA or c in _INSERTION_EXTRA for c in seq
    )


class A3mValidator:
    def validate(self, data: bytes) -> A3mInfo:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise A3mInvalid("non_utf8") from e

        lines = text.splitlines()
        if not any(ln.strip() for ln in lines):
            raise A3mInvalid("empty")

        # first non-empty line must be a header
        first = next(ln for ln in lines if ln.strip())
        if not first.startswith(">"):
            raise A3mInvalid("no_header")

        records: list[str] = []
        cur: list[str] | None = None
        for ln in lines:
            if ln.startswith(">"):
                if cur is not None:
                    records.append("".join(cur))
                cur = []
            elif ln.strip():
                if cur is None:  # pragma: no cover - guarded by no_header
                    raise A3mInvalid("no_header")
                cur.append(ln.strip())
        if cur is None or not cur:
            raise A3mInvalid("dangling_header")
        records.append("".join(cur))

        if not records:
            raise A3mInvalid("empty")

        match_counts = set()
        for seq in records:
            if not seq or not _valid_alphabet(seq):
                raise A3mInvalid("bad_alphabet")
            match_counts.add(_match_columns(seq))

        if len(match_counts) != 1:
            raise A3mInvalid("ragged_alignment")

        return A3mInfo(n_sequences=len(records), match_columns=match_counts.pop())
