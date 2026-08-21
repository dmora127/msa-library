"""Classifier: dispatch + DiamondClassifier parse/threshold/failure modes.

The real diamond binary isn't installed; we substitute a tiny shell script that
prints prepared stdout / exits with a given code, exercising the parser and
failure paths.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.classifier import (
    UNCLASSIFIED,
    DiamondClassifier,
    NoopClassifier,
    StubClassifier,
    build_classifier,
)


def _fake_diamond(tmp_path, *, stdout: str = "", rc: int = 0) -> str:
    p = tmp_path / "diamond"
    p.write_text(f'#!/bin/sh\nprintf %s "{stdout}"\nexit {rc}\n')
    p.chmod(0o755)
    return str(p)


def test_build_dispatch():
    assert isinstance(
        build_classifier(Settings(classifier_impl="noop")), NoopClassifier
    )
    assert isinstance(
        build_classifier(Settings(classifier_impl="stub")), StubClassifier
    )
    # diamond requires DIAMOND_DB
    with pytest.raises(ValueError):
        build_classifier(Settings(classifier_impl="diamond"))
    assert isinstance(
        build_classifier(
            Settings(classifier_impl="diamond", diamond_db="/tmp/swissprot")
        ),
        DiamondClassifier,
    )
    with pytest.raises(ValueError):
        build_classifier(Settings(classifier_impl="bogus"))


async def test_noop_always_unclassified():
    assert (await NoopClassifier().classify("MKTAYIAK")) == UNCLASSIFIED


async def test_diamond_top_hit_meets_thresholds(tmp_path):
    # diamond -f 6 columns: qseqid sseqid pident length mismatch sscinames stitle evalue
    row = (
        "query\tsp|P12345|HUMAN_PROT\t100.0\t300\t0\t"
        "Homo sapiens\tProtein P12345 OS=Homo sapiens\t1e-50"
    )
    c = DiamondClassifier(
        binary=_fake_diamond(tmp_path, stdout=row + "\n"), db="/tmp/db"
    )
    r = await c.classify("MKTAYIAK")
    assert r.species == "Homo sapiens"
    assert r.query_name == "Protein P12345 OS=Homo sapiens"


@pytest.mark.parametrize("evalue", ["0.001", "0.0005"])
async def test_diamond_evalue_above_threshold_unclassified(tmp_path, evalue):
    # evalue 0.0005 is the boundary — must be strictly less than threshold.
    row = f"q\tsp|x\t100.0\t300\t0\tHomo sapiens\tProtein x\t{evalue}"
    c = DiamondClassifier(
        binary=_fake_diamond(tmp_path, stdout=row + "\n"), db="/tmp/db"
    )
    assert (await c.classify("MKT")) == UNCLASSIFIED


async def test_diamond_trusts_id_filter_low_pident_still_classifies(tmp_path):
    # Diamond's --id 99.99 gates identity at the source; if a row reaches us,
    # we don't re-check pident (only evalue). pident=99.99 + good evalue -> ok.
    row = (
        "q\tsp|p\t99.99\t300\t0\tHomo sapiens\tProtein P\t1e-40"
    )
    c = DiamondClassifier(
        binary=_fake_diamond(tmp_path, stdout=row + "\n"), db="/tmp/db"
    )
    r = await c.classify("MKT")
    assert r.species == "Homo sapiens" and r.query_name == "Protein P"


async def test_diamond_no_hits_unclassified(tmp_path):
    c = DiamondClassifier(
        binary=_fake_diamond(tmp_path, stdout=""), db="/tmp/db"
    )
    assert (await c.classify("MKT")) == UNCLASSIFIED


async def test_diamond_nonzero_exit_unclassified(tmp_path):
    c = DiamondClassifier(
        binary=_fake_diamond(tmp_path, stdout="", rc=1), db="/tmp/db"
    )
    assert (await c.classify("MKT")) == UNCLASSIFIED


async def test_diamond_missing_binary_unclassified():
    c = DiamondClassifier(
        binary="definitely-not-a-real-binary-xyz", db="/tmp/db"
    )
    assert (await c.classify("MKT")) == UNCLASSIFIED


async def test_diamond_empty_sequence_unclassified():
    c = DiamondClassifier(binary="diamond", db="/tmp/db")
    assert (await c.classify("")) == UNCLASSIFIED


async def test_diamond_malformed_row_unclassified(tmp_path):
    # fewer than 8 columns -> skip; non-numeric pident/evalue -> skip
    c = DiamondClassifier(
        binary=_fake_diamond(tmp_path, stdout="too\tfew\tcols\n"), db="/tmp/db"
    )
    assert (await c.classify("MKT")) == UNCLASSIFIED
