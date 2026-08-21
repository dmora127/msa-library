"""Golden-value contract test.

GOLDEN_HASH was computed with plan.md's exact recipe against the Toy_Dataset
Job4 actin sequence (XP_001659963.1_Aedes_aegypti_Actin):

    printf '%s' "$SEQ" | tr -d '[:space:]' | tr 'a-z' 'A-Z' | shasum -a 256

If this test ever fails, server hashing has drifted from the client and every
cache hit silently becomes a miss.
"""

from __future__ import annotations

from app.core.hashing import is_valid_seq_hash, normalize_sequence, sequence_hash

ACTIN = (
    "MCDDDVAALVVDNGSGMCKAGFAGDDAPRAVFPSIVGRPRHQGVMVGMGQKDSYVGDEAQSKRGILTLKYPIEHG"
    "IITNWDDMEKIWHHTFYNELRVAPEEHPVLLTEAPLNPKANREKMTQIMFETFNSPAMYVAIQAVLSLYASGRTT"
    "GIVLDSGDGVSHTVPIYEGYALPHAILRLDLAGRDLTDYLMKILTERGYSFTTTAEREIVRDIKEKLCYVALDFE"
    "QEMATAAASTSLEKSYELPDGQVITIGNERFRCPEALFQPSFLGMESCGIHETVYNSIMKCDVDIRKDLYANTVM"
    "SGGTTMYPGIADRMQKEITALAPSTIKIKIIAPPERKYSVWIGGSILASLSTFQQMWISKEEYDESGPGIVHRKCF"
)
GOLDEN_HASH = "ebe47344e84c6b7564bd96135a602eb528e86ab55081a240ae7686821e78e287"


def test_actin_golden_hash():
    assert sequence_hash(ACTIN) == GOLDEN_HASH


def test_normalization_strips_whitespace_and_uppercases():
    messy = "  mcd dd\nVAAL\tvvd ngsgm "
    assert normalize_sequence(messy) == "MCDDDVAALVVDNGSGM"
    # whitespace/case must not change the hash
    assert sequence_hash(f"  {ACTIN.lower()}\n") == GOLDEN_HASH


def test_already_normalized_skips_renormalization():
    norm = normalize_sequence(ACTIN)
    assert sequence_hash(norm, already_normalized=True) == GOLDEN_HASH


def test_is_valid_seq_hash():
    assert is_valid_seq_hash(GOLDEN_HASH)
    assert not is_valid_seq_hash(GOLDEN_HASH.upper())  # must be lowercase
    assert not is_valid_seq_hash("abc")
    assert not is_valid_seq_hash("z" * 64)
