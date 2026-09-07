"""OSDF namespace registry — the YAML file that says where uploads may land.

One entry per Pelican namespace (store): its token issuer URL, the private key
that mints write tokens for it, its storage capacity, and whether it accepts
NEW uploads. Loaded once per process (`deps.get_namespace_registry`) and
validated at app construction, so a broken file stops the container instead of
surfacing as a 503 storm on POST /v1/uploads/request.

Why a file and not env vars: each namespace is a record of several fields, and
the list grows. Three flat env vars (PELICAN_TOKEN_ISSUER, _PRIVATE_KEY,
PELICAN_STORE_NAMESPACES) could describe exactly one namespace; this replaces
them.

Format (deploy/jetstream2/namespaces.yaml.example):

    min_headroom_pct: 10            # optional, default 10
    namespaces:
      - namespace: af3-msa-library  # path prefix under osdf:///, may contain '/'
        issuer: https://<origin>:<port>/api/v1.0/issuer/ns/af3-msa-library
        private_key: /keys/community.pem     # in-container path
        capacity_tb: 50                       # decimal TB (1e12 bytes)
        min_headroom_pct: 15                  # optional per-entry override
        enabled: true                         # false = drain: no NEW uploads

`resolve()` is the one place that turns an object URI into (namespace,
scope_path). The inprocess issuer signs `storage.create:<scope_path>` directly;
the pelican issuer passes it as `--scope-path`, which does not skip pelican's
director lookup (10 s cap) but makes a director failure non-fatal instead of
the "Failed to get director info" exit that caused most nonzero exits.
Disabled entries keep their credentials: an ingestion row that already points
at a drained namespace must still be able to re-request its token.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

log = logging.getLogger("af3cache.namespaces")

TB = 1_000_000_000_000  # decimal terabyte, matching the 50e12 quota alerts


class NamespaceConfigError(ValueError):
    """The namespaces file is missing, unparseable, or invalid. Raised at
    startup (never on a request path)."""


def _normalize_namespace(raw: str) -> str:
    ns = raw.strip().strip("/")
    if not ns:
        raise ValueError("namespace must not be empty")
    if any(ch.isspace() for ch in ns):
        raise ValueError(f"namespace {raw!r} must not contain whitespace")
    if "://" in ns:
        raise ValueError(f"namespace {raw!r} must be a path prefix, not a URL")
    if "//" in ns or ".." in ns.split("/"):
        raise ValueError(f"namespace {raw!r} must not contain '//' or '..' segments")
    return ns


class NamespaceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    namespace: str
    # Plain str on purpose: pydantic's URL types append a trailing '/' to a bare
    # host, and this value is the token's `iss` claim byte-for-byte.
    issuer: str
    private_key: str
    capacity_tb: float = Field(gt=0)
    min_headroom_pct: float | None = Field(default=None, ge=0, le=100)
    enabled: bool = True

    @field_validator("namespace")
    @classmethod
    def _ns(cls, v: str) -> str:
        return _normalize_namespace(v)

    @field_validator("issuer")
    @classmethod
    def _issuer(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("https://") or v.startswith("http://")):
            raise ValueError("issuer must be an http(s) URL")
        if any(ch.isspace() for ch in v):
            raise ValueError("issuer must not contain whitespace")
        return v

    @field_validator("private_key")
    @classmethod
    def _key(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("private_key must not be empty")
        return v

    @property
    def capacity_bytes(self) -> int:
        return int(self.capacity_tb * TB)


class NamespacesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_headroom_pct: float = Field(default=10, ge=0, le=100)
    namespaces: list[NamespaceEntry]

    @model_validator(mode="after")
    def _unique(self) -> NamespacesFile:
        seen: set[str] = set()
        for e in self.namespaces:
            if e.namespace in seen:
                raise ValueError(f"duplicate namespace {e.namespace!r}")
            seen.add(e.namespace)
        return self


@dataclass(frozen=True)
class NamespaceRegistry:
    """Immutable view of the loaded file. `entries` holds EVERY entry (disabled
    included) so credentials stay resolvable for rows already pointing at a
    drained store; only `enabled_namespaces()` filters."""

    entries: Mapping[str, NamespaceEntry]
    default_headroom_pct: float = 10.0
    source: str | None = None

    def __post_init__(self) -> None:
        # longest first so 'osg/af3' wins over 'osg' for osdf:///osg/af3/x
        object.__setattr__(
            self, "_by_length",
            tuple(sorted(self.entries, key=lambda n: (-len(n), n))),
        )

    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)

    def enabled_namespaces(self) -> list[str]:
        return sorted(n for n, e in self.entries.items() if e.enabled)

    def credentials_for(self, namespace: str) -> NamespaceEntry | None:
        return self.entries.get(namespace)

    def headroom_pct_for(self, namespace: str) -> float:
        e = self.entries.get(namespace)
        if e is None or e.min_headroom_pct is None:
            return self.default_headroom_pct
        return e.min_headroom_pct

    def resolve(self, object_uri: str) -> tuple[str, str] | None:
        """(namespace, scope_path) for an object URI, or None.

        Only the URL *path* is consulted, so `osdf:///ns/obj`,
        `pelican://<federation>/ns/obj` and a bare `/ns/obj` all resolve, while
        `osdf://ns/obj` (namespace in the authority) does not. scope_path is
        the remainder after the namespace prefix and always starts with '/'.
        A URI naming only the namespace (no object) is refused: we never mint a
        namespace-root write scope.
        """
        parsed = urlparse(object_uri)
        path = parsed.path if (parsed.path or parsed.scheme) else object_uri
        if not path.startswith("/"):
            path = "/" + path
        if ".." in path.split("/"):
            return None
        for ns in self._by_length:  # type: ignore[attr-defined]
            prefix = "/" + ns
            if path == prefix or path.startswith(prefix + "/"):
                scope_path = path[len(prefix):]
                if scope_path in ("", "/") or scope_path.endswith("/"):
                    return None
                return ns, scope_path
        return None


EMPTY_REGISTRY = NamespaceRegistry(entries={})


def namespaces_required(settings) -> bool:
    """The pelican and inprocess issuers cannot mint anything without the
    file (issuer URL + private key per namespace live only here)."""
    return settings.token_issuer_impl.lower() in ("pelican", "inprocess")


def _check_private_key(entry: NamespaceEntry, path: str) -> None:
    p = entry.private_key
    if os.path.isdir(p):
        raise NamespaceConfigError(
            f"{path}: namespace {entry.namespace!r}: private_key {p!r} is a "
            "directory (bind mount of a missing host file?)"
        )
    if not os.path.isfile(p):
        raise NamespaceConfigError(
            f"{path}: namespace {entry.namespace!r}: private_key {p!r} not found"
        )
    try:
        with open(p, "rb") as fh:
            head = fh.read(64)
    except OSError as e:
        raise NamespaceConfigError(
            f"{path}: namespace {entry.namespace!r}: private_key {p!r} "
            f"unreadable: {e.strerror or e}"
        ) from e
    if not head:
        raise NamespaceConfigError(
            f"{path}: namespace {entry.namespace!r}: private_key {p!r} is empty"
        )


def load_namespaces(path: str | os.PathLike, *, required: bool) -> NamespaceRegistry:
    """Parse + validate the file. Missing file: raise when `required`, else an
    empty registry (dryrun/stub/local impls tolerate absence). A file that IS
    present must be valid regardless of `required`, so a broken file is caught
    in staging rather than after flipping TOKEN_ISSUER_IMPL to pelican."""
    path = os.fspath(path)
    if not os.path.exists(path):
        if required:
            raise NamespaceConfigError(
                f"namespaces file {path!r} not found (PELICAN_NAMESPACES_FILE); "
                "TOKEN_ISSUER_IMPL=pelican cannot mint without it"
            )
        log.info("namespaces: file %s not present; registry empty", path)
        return EMPTY_REGISTRY
    if os.path.isdir(path):
        raise NamespaceConfigError(
            f"namespaces file {path!r} is a directory (bind mount of a missing host file?)"
        )
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as e:
        raise NamespaceConfigError(f"{path}: cannot parse: {e}") from e
    if data is None:
        raise NamespaceConfigError(f"{path}: file is empty")
    if not isinstance(data, dict):
        raise NamespaceConfigError(
            f"{path}: top level must be a mapping with a 'namespaces:' list"
        )
    try:
        parsed = NamespacesFile.model_validate(data)
    except ValidationError as e:
        raise NamespaceConfigError(f"{path}: invalid: {e}") from e
    if not parsed.namespaces:
        raise NamespaceConfigError(f"{path}: 'namespaces:' list is empty")
    if not any(e.enabled for e in parsed.namespaces):
        raise NamespaceConfigError(
            f"{path}: every namespace is disabled; nothing could accept uploads"
        )
    for e in parsed.namespaces:  # disabled entries still mint for re-requests
        _check_private_key(e, path)

    reg = NamespaceRegistry(
        entries={e.namespace: e for e in parsed.namespaces},
        default_headroom_pct=parsed.min_headroom_pct,
        source=path,
    )
    log.info(
        "namespaces: loaded %d entries (%d enabled) from %s: %s",
        len(reg), len(reg.enabled_namespaces()), path, ", ".join(sorted(reg.entries)),
    )
    for e in parsed.namespaces:
        log.debug(
            "namespaces: %s issuer=%s key=%s capacity_tb=%s enabled=%s",
            e.namespace, e.issuer, e.private_key, e.capacity_tb, e.enabled,
        )
    return reg
