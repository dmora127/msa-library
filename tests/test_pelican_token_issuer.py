"""PelicanCliTokenIssuer: argv (incl. --scope-path from the namespace
registry), per-namespace credentials, stdout token, JWT exp/jti, failure
modes, and the stderr logging that a bare 503 used to hide."""

from __future__ import annotations

import logging
import time

import jwt
import pytest

from app.core.config import Settings
from app.core.errors import TokenIssuanceError
from app.core.namespaces import NamespaceConfigError
from app.services.token_issuer import (
    PelicanCliTokenIssuer,
    _redacted,
    build_token_issuer,
)
from tests.conftest import NS_ISSUER as ISSUER

SCOPE = "osdf:///af3-msa-library/community-contributed/iid.a3m"
SCOPE_PATH = "/community-contributed/iid.a3m"
LOGGER = "af3cache.tokens"


def _fake_pelican(tmp_path, *, stdout="", stderr="", rc=0, argv_log=None):
    script = tmp_path / "pelican"
    log = f'echo "$@" >> {argv_log}\n' if argv_log else ""
    err = f"printf %s '{stderr}' >&2\n" if stderr else ""
    script.write_text(f"#!/bin/sh\n{log}{err}printf %s '{stdout}'\nexit {rc}\n")
    script.chmod(0o755)
    return str(script)


def _pelican_settings(path, **over) -> Settings:
    return Settings(
        token_issuer_impl="pelican", pelican_namespaces_file=str(path), **over
    )


def _issuer(namespaces_yaml, tmp_path, entries=None, **fake):
    argv_log = tmp_path / "argv.txt"
    binary = _fake_pelican(tmp_path, argv_log=argv_log, **fake)
    issuer = PelicanCliTokenIssuer(
        _pelican_settings(namespaces_yaml(entries), pelican_token_binary=binary)
    )
    return issuer, argv_log


def test_build_dispatch_pelican(namespaces_yaml):
    assert isinstance(
        build_token_issuer(_pelican_settings(namespaces_yaml())),
        PelicanCliTokenIssuer,
    )


def test_build_dispatch_pelican_missing_file_raises(tmp_path):
    # the startup gate: constructing the pelican issuer validates the registry
    with pytest.raises(NamespaceConfigError):
        build_token_issuer(_pelican_settings(tmp_path / "absent.yaml"))


async def test_issue_runs_expected_command_and_returns_token(namespaces_yaml, tmp_path):
    exp = int(time.time()) + 1200
    token = jwt.encode(
        {"exp": exp, "jti": "jti-xyz", "scope": "storage.create"},
        "x" * 32, algorithm="HS256",
    )
    issuer, argv_log = _issuer(namespaces_yaml, tmp_path, stdout=token)

    res = await issuer.issue(subject="job", object_uri=SCOPE, ttl_seconds=1200)

    assert res.token == token
    assert res.claim_jti == "jti-xyz"               # parsed from the JWT
    assert abs(res.expires_at.timestamp() - exp) < 2
    assert res.token_ref and res.token_ref != res.claim_jti  # app correlation id

    key = namespaces_yaml.key("af3-msa-library")
    assert argv_log.read_text().strip() == (
        f"token create {SCOPE} --write --issuer {ISSUER} -k {key} "
        f"--scope-path {SCOPE_PATH} --lifetime 1200"
    )


async def test_issue_picks_credentials_by_namespace(namespaces_yaml, tmp_path):
    other_key = tmp_path / "west.pem"
    other_key.write_text("fake")
    entries = [
        {"namespace": "af3-msa-library", "issuer": ISSUER, "capacity_tb": 1},
        {"namespace": "af3-msa-west", "issuer": "https://west.test/ns/w",
         "private_key": str(other_key), "capacity_tb": 1},
    ]
    issuer, argv_log = _issuer(namespaces_yaml, tmp_path, entries, stdout="tok")
    await issuer.issue(subject="j", object_uri=SCOPE, ttl_seconds=60)
    await issuer.issue(
        subject="j", object_uri="osdf:///af3-msa-west/community-contributed/z.a3m",
        ttl_seconds=60,
    )
    east, west = argv_log.read_text().strip().splitlines()
    assert f"--issuer {ISSUER} -k {namespaces_yaml.key('af3-msa-library')}" in east
    assert f"--scope-path {SCOPE_PATH}" in east
    assert f"--issuer https://west.test/ns/w -k {other_key}" in west
    assert "--scope-path /community-contributed/z.a3m" in west


async def test_issue_multi_segment_namespace_scope_path(namespaces_yaml, tmp_path):
    entries = [{"namespace": "osg/af3", "issuer": ISSUER, "capacity_tb": 1}]
    issuer, argv_log = _issuer(namespaces_yaml, tmp_path, entries, stdout="tok")
    await issuer.issue(subject="j", object_uri="osdf:///osg/af3/x.a3m", ttl_seconds=60)
    assert "--scope-path /x.a3m" in argv_log.read_text()


async def test_issue_disabled_namespace_still_mints(namespaces_yaml, tmp_path):
    # re-request of a row that already lives in a drained store
    entries = [
        {"namespace": "live", "issuer": ISSUER, "capacity_tb": 1},
        {"namespace": "af3-msa-library", "issuer": ISSUER, "capacity_tb": 1,
         "enabled": False},
    ]
    issuer, argv_log = _issuer(namespaces_yaml, tmp_path, entries, stdout="tok")
    res = await issuer.issue(subject="j", object_uri=SCOPE, ttl_seconds=60)
    assert res.token == "tok"
    assert f"--scope-path {SCOPE_PATH}" in argv_log.read_text()


async def test_issue_non_jwt_token_falls_back_to_requested_ttl(namespaces_yaml, tmp_path):
    issuer, _ = _issuer(namespaces_yaml, tmp_path, stdout="opaque-not-a-jwt")
    res = await issuer.issue(subject="j", object_uri=SCOPE, ttl_seconds=600)
    assert res.token == "opaque-not-a-jwt"
    assert res.claim_jti is None
    assert 595 <= (res.expires_at.timestamp() - time.time()) <= 605


async def test_issue_unconfigured_namespace_raises_not_configured(
    namespaces_yaml, tmp_path, caplog, monkeypatch
):
    seen = []
    monkeypatch.setattr(
        "app.services.token_issuer.record_token_issue",
        lambda result, seconds=None: seen.append(result),
    )
    issuer, argv_log = _issuer(namespaces_yaml, tmp_path, stdout="tok")
    with (
        caplog.at_level(logging.WARNING, logger=LOGGER),
        pytest.raises(TokenIssuanceError, match="no configured namespace"),
    ):
        await issuer.issue(
            subject="j", object_uri="osdf:///somewhere-else/x.a3m", ttl_seconds=60
        )
    assert seen == ["not_configured"]
    assert not argv_log.exists()  # never shelled out
    assert any("not_configured" in r.message and "somewhere-else" in r.message
               for r in caplog.records)


async def test_issue_missing_binary_logs_cli_missing(namespaces_yaml, tmp_path, caplog):
    issuer = PelicanCliTokenIssuer(
        _pelican_settings(namespaces_yaml(), pelican_token_binary="definitely-not-real-xyz")
    )
    with (
        caplog.at_level(logging.WARNING, logger=LOGGER),
        pytest.raises(TokenIssuanceError),
    ):
        await issuer.issue(subject="j", object_uri=SCOPE, ttl_seconds=60)
    rec = next(r for r in caplog.records if r.name == LOGGER and "cli_missing" in r.message)
    assert "namespace=af3-msa-library" in rec.message
    assert "definitely-not-real-xyz" in rec.message


async def test_issue_nonzero_exit_logs_stderr(namespaces_yaml, tmp_path, caplog):
    issuer, _ = _issuer(
        namespaces_yaml, tmp_path, rc=2,
        stderr="Transfer.DirectorTimeout Error: context deadline exceeded",
    )
    with (
        caplog.at_level(logging.WARNING, logger=LOGGER),
        pytest.raises(TokenIssuanceError, match="exit 2"),
    ):
        await issuer.issue(subject="j", object_uri=SCOPE, ttl_seconds=60)
    rec = next(
        r for r in caplog.records if r.name == LOGGER and "nonzero_exit" in r.message
    )
    assert "namespace=af3-msa-library" in rec.message
    assert "rc=2" in rec.message
    assert "DirectorTimeout" in rec.message
    assert "elapsed=" in rec.message


async def test_issue_stderr_truncated_to_500(namespaces_yaml, tmp_path, caplog):
    issuer, _ = _issuer(namespaces_yaml, tmp_path, rc=1, stderr="E" * 2000)
    with (
        caplog.at_level(logging.WARNING, logger=LOGGER),
        pytest.raises(TokenIssuanceError),
    ):
        await issuer.issue(subject="j", object_uri=SCOPE, ttl_seconds=60)
    rec = next(
        r for r in caplog.records if r.name == LOGGER and "nonzero_exit" in r.message
    )
    assert "E" * 500 in rec.message and "E" * 501 not in rec.message


async def test_issue_empty_stdout_logs_empty_output(namespaces_yaml, tmp_path, caplog):
    issuer, _ = _issuer(namespaces_yaml, tmp_path, stdout="", rc=0, stderr="nothing minted")
    with (
        caplog.at_level(logging.WARNING, logger=LOGGER),
        pytest.raises(TokenIssuanceError, match="no token"),
    ):
        await issuer.issue(subject="j", object_uri=SCOPE, ttl_seconds=60)
    rec = next(
        r for r in caplog.records if r.name == LOGGER and "empty_output" in r.message
    )
    assert "nothing minted" in rec.message


async def test_debug_log_redacts_key_path(namespaces_yaml, tmp_path, caplog):
    issuer, _ = _issuer(namespaces_yaml, tmp_path, stdout="tok")
    key = namespaces_yaml.key("af3-msa-library")
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await issuer.issue(subject="j", object_uri=SCOPE, ttl_seconds=60)
    argv_rec = next(r for r in caplog.records if "argv=" in r.message)
    assert "-k <redacted>" in argv_rec.message
    assert key not in argv_rec.message
    assert any("token issue ok namespace=af3-msa-library" in r.message
               for r in caplog.records)


def test_redacted_masks_only_the_key():
    assert _redacted(["p", "token", "create", "u", "-k", "/secret", "--lifetime", "5"]) == (
        "p token create u -k <redacted> --lifetime 5"
    )
    assert _redacted(["p", "-k"]) == "p -k"  # dangling flag, nothing to mask
