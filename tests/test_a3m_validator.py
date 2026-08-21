"""A3mValidator sniff: valid (gaps + lowercase insertions), and each reject."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.a3m_validator import A3mInvalid, A3mValidator

FIX = Path(__file__).parent / "fixtures"


def _v() -> A3mValidator:
    return A3mValidator()


def test_valid_sample_with_gap_and_insertion():
    info = _v().validate((FIX / "sample.a3m").read_bytes())
    assert info.n_sequences == 3
    assert info.match_columns == 10  # gaps count, lowercase insertions do not


def test_bad_format_no_header():
    with pytest.raises(A3mInvalid) as e:
        _v().validate((FIX / "bad_format.a3m").read_bytes())
    assert e.value.reason == "bad_format:no_header"


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"\xff\xfe\x00bad", "bad_format:non_utf8"),
        (b"   \n\n  \n", "bad_format:empty"),
        (b">only a header\n", "bad_format:dangling_header"),
        (b">q\nMKTAY\n>h\nMKT\n", "bad_format:ragged_alignment"),
        (b">q\nMKT12Y\n", "bad_format:bad_alphabet"),
    ],
)
def test_rejects(data, reason):
    with pytest.raises(A3mInvalid) as e:
        _v().validate(data)
    assert e.value.reason == reason


def test_single_record_ok():
    info = _v().validate(b">q\nMKTAYIAKQR\n")
    assert info.n_sequences == 1 and info.match_columns == 10
