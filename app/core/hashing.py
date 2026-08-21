"""The single shared sequence-normalization + hashing contract.

This MUST stay byte-identical to the client (tutorial-CHTC-AF3/plan.md §2):

    seq = "".join(entry["protein"]["sequence"].split()).upper()
    h   = hashlib.sha256(seq.encode()).hexdigest()

Any drift here silently turns every cache hit into a miss, so this is the only
place normalization is defined and it is pinned by a golden-value test
(tests/test_hashing.py) computed via plan.md's exact shell recipe.
"""

from __future__ import annotations

import hashlib

_HASH_LEN = 64


def normalize_sequence(raw: str) -> str:
    """Strip all whitespace, uppercase. Matches `"".join(s.split()).upper()`."""
    return "".join(raw.split()).upper()


def sequence_hash(raw_or_normalized: str, *, already_normalized: bool = False) -> str:
    """SHA256 hex of the normalized sequence."""
    norm = raw_or_normalized if already_normalized else normalize_sequence(raw_or_normalized)
    return hashlib.sha256(norm.encode()).hexdigest()


def is_valid_seq_hash(value: str) -> bool:
    """True iff `value` is a 64-char lowercase hex SHA256 (the DB CHECK regex)."""
    if len(value) != _HASH_LEN:
        return False
    return all(c in "0123456789abcdef" for c in value)
