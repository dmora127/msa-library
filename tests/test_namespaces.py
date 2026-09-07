"""Namespace registry (app/core/namespaces.py): file validation, private-key
checks, normalization, and object-URI -> (namespace, scope_path) resolution.
No DB."""

from __future__ import annotations

import os

import pytest

from app.core.config import Settings
from app.core.namespaces import (
    EMPTY_REGISTRY,
    NamespaceConfigError,
    NamespaceRegistry,
    load_namespaces,
    namespaces_required,
)

ISS = "https://issuer.test/api/v1.0/issuer/ns/x"


def _entry(ns, **over):
    e = {"namespace": ns, "issuer": ISS, "capacity_tb": 10}
    e.update(over)
    return e


# --- loading + validation ---------------------------------------------------


def test_load_two_entries_normalizes_and_orders(namespaces_yaml):
    reg = load_namespaces(
        namespaces_yaml([_entry("/osg/af3/"), _entry("af3-msa-library")]),
        required=True,
    )
    assert set(reg.entries) == {"osg/af3", "af3-msa-library"}
    assert reg.enabled_namespaces() == ["af3-msa-library", "osg/af3"]
    assert reg.source and reg.default_headroom_pct == 10
    assert len(reg) == 2 and bool(reg)


def test_disabled_entry_excluded_from_enabled_but_keeps_credentials(namespaces_yaml):
    reg = load_namespaces(
        namespaces_yaml([_entry("a"), _entry("b", enabled=False)]), required=True
    )
    assert reg.enabled_namespaces() == ["a"]
    assert reg.credentials_for("b") is not None
    assert reg.credentials_for("b").enabled is False
    assert reg.credentials_for("zzz") is None


def test_capacity_and_headroom_fields(namespaces_yaml):
    reg = load_namespaces(
        namespaces_yaml(
            [_entry("a", capacity_tb=1.5), _entry("b", min_headroom_pct=25)],
            min_headroom_pct=15,
        ),
        required=True,
    )
    assert reg.entries["a"].capacity_bytes == 1_500_000_000_000
    assert reg.headroom_pct_for("a") == 15
    assert reg.headroom_pct_for("b") == 25
    assert reg.headroom_pct_for("unknown") == 15


def test_missing_file_required_raises(tmp_path):
    with pytest.raises(NamespaceConfigError, match="not found"):
        load_namespaces(tmp_path / "nope.yaml", required=True)


def test_missing_file_not_required_is_empty(tmp_path):
    reg = load_namespaces(tmp_path / "nope.yaml", required=False)
    assert reg is EMPTY_REGISTRY
    assert len(reg) == 0 and not reg
    assert reg.resolve("osdf:///af3-msa-library/x.a3m") is None


@pytest.mark.parametrize(
    "body, match",
    [
        ("namespaces: [", "cannot parse"),
        ("- a\n- b\n", "top level must be a mapping"),
        ("", "empty"),
        ("namespaces: []\n", "list is empty"),
    ],
)
def test_malformed_file_rejected(tmp_path, body, match):
    p = tmp_path / "ns.yaml"
    p.write_text(body)
    with pytest.raises(NamespaceConfigError, match=match):
        load_namespaces(p, required=False)  # present file must be valid regardless


def test_directory_at_path_rejected(tmp_path):
    with pytest.raises(NamespaceConfigError, match="directory"):
        load_namespaces(tmp_path, required=True)


@pytest.mark.parametrize(
    "entries",
    [
        [_entry("a", bogus=1)],                       # unknown key
        [{"namespace": "a", "capacity_tb": 1}],       # missing issuer/private_key
        [_entry("a", issuer="ftp://x")],              # not http(s)
        [_entry("a", issuer="https://x y")],          # whitespace in issuer
        [_entry("")],                                 # empty namespace
        [_entry("   /  ")],                           # slashes only
        [_entry("https://host/ns")],                  # scheme in namespace
        [_entry("a//b")],                             # empty segment
        [_entry("a/../b")],                           # traversal
        [_entry("a b")],                              # whitespace
        [_entry("a", capacity_tb=0)],
        [_entry("a", capacity_tb=-1)],
        [_entry("a", min_headroom_pct=101)],
        [_entry("a", private_key="")],
    ],
)
def test_invalid_entry_rejected(namespaces_yaml, entries):
    with pytest.raises(NamespaceConfigError, match="invalid"):
        load_namespaces(namespaces_yaml(entries), required=True)


def test_top_level_headroom_out_of_range(namespaces_yaml):
    with pytest.raises(NamespaceConfigError, match="invalid"):
        load_namespaces(namespaces_yaml(min_headroom_pct=200), required=True)


def test_duplicate_namespace_after_normalization_rejected(namespaces_yaml):
    with pytest.raises(NamespaceConfigError, match="duplicate"):
        load_namespaces(
            namespaces_yaml([_entry("af3-msa-library"), _entry("/af3-msa-library/")]),
            required=True,
        )


@pytest.mark.parametrize("required", [True, False])
def test_all_disabled_rejected(namespaces_yaml, required):
    with pytest.raises(NamespaceConfigError, match="disabled"):
        load_namespaces(
            namespaces_yaml([_entry("a", enabled=False)]), required=required
        )


# --- private key checks (every entry, disabled included) --------------------


def test_private_key_missing_rejected(namespaces_yaml, tmp_path):
    p = namespaces_yaml([_entry("a", private_key=str(tmp_path / "absent.pem"))])
    with pytest.raises(NamespaceConfigError, match="not found"):
        load_namespaces(p, required=True)


def test_private_key_is_directory_rejected(namespaces_yaml, tmp_path):
    d = tmp_path / "keydir"
    d.mkdir()
    p = namespaces_yaml([_entry("a", private_key=str(d))])
    with pytest.raises(NamespaceConfigError, match="bind mount"):
        load_namespaces(p, required=True)


def test_private_key_empty_rejected(namespaces_yaml, tmp_path):
    k = tmp_path / "empty.pem"
    k.write_text("")
    with pytest.raises(NamespaceConfigError, match="is empty"):
        load_namespaces(namespaces_yaml([_entry("a", private_key=str(k))]), required=True)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores mode bits")
def test_private_key_unreadable_rejected(namespaces_yaml, tmp_path):
    k = tmp_path / "locked.pem"
    k.write_text("secret")
    k.chmod(0)
    try:
        with pytest.raises(NamespaceConfigError, match="unreadable"):
            load_namespaces(
                namespaces_yaml([_entry("a", private_key=str(k))]), required=True
            )
    finally:
        k.chmod(0o600)


def test_disabled_entry_key_still_checked(namespaces_yaml, tmp_path):
    p = namespaces_yaml(
        [_entry("a"), _entry("b", enabled=False, private_key=str(tmp_path / "gone.pem"))]
    )
    with pytest.raises(NamespaceConfigError, match="'b'"):
        load_namespaces(p, required=True)


# --- resolve() ---------------------------------------------------------------


def _reg(*names):
    from app.core.namespaces import NamespaceEntry

    return NamespaceRegistry(
        entries={
            n: NamespaceEntry(namespace=n, issuer=ISS, private_key="/k", capacity_tb=1)
            for n in names
        }
    )


@pytest.mark.parametrize(
    "names, uri, expected",
    [
        (["af3-msa-library"], "osdf:///af3-msa-library/community-contributed/x.a3m",
         ("af3-msa-library", "/community-contributed/x.a3m")),
        (["af3-msa-library"], "pelican://osg-htc.org/af3-msa-library/a/b.a3m",
         ("af3-msa-library", "/a/b.a3m")),
        (["af3-msa-library"], "/af3-msa-library/x.a3m", ("af3-msa-library", "/x.a3m")),
        (["af3-msa-library"], "af3-msa-library/x.a3m", ("af3-msa-library", "/x.a3m")),
        # multi-segment + nested: longest prefix wins
        (["osg", "osg/af3"], "osdf:///osg/af3/x.a3m", ("osg/af3", "/x.a3m")),
        (["osg", "osg/af3"], "osdf:///osg/x.a3m", ("osg", "/x.a3m")),
        (["osg", "osg/af3"], "osdf:///osg/af3x/x.a3m", ("osg", "/af3x/x.a3m")),
        # prefix but not on a '/' boundary
        (["af3-msa-library"], "osdf:///af3-msa-library-2/x.a3m", None),
        # namespace only / trailing slash: no object -> never a root scope
        (["af3-msa-library"], "osdf:///af3-msa-library", None),
        (["af3-msa-library"], "osdf:///af3-msa-library/", None),
        (["af3-msa-library"], "osdf:///af3-msa-library/dir/", None),
        (["af3-msa-library"], "osdf:///other/x.a3m", None),
        # namespace in the authority is NOT a path
        (["af3-msa-library"], "osdf://af3-msa-library/x.a3m", None),
        (["af3-msa-library"], "osdf:///af3-msa-library/../x.a3m", None),
        (["af3-msa-library"], "", None),
    ],
)
def test_resolve(names, uri, expected):
    assert _reg(*names).resolve(uri) == expected


def test_namespaces_required_for_pelican_and_inprocess():
    for impl in ("pelican", "Pelican", "inprocess", "INPROCESS"):
        assert namespaces_required(Settings(token_issuer_impl=impl))
    for impl in ("stub", "dryrun", "local"):
        assert not namespaces_required(Settings(token_issuer_impl=impl))
