"""Per-namespace capacity gate.

A namespace whose free space is at or below its headroom threshold accepts no
NEW uploads: it is skipped when picking a store for a fresh upload request, and
a fresh request that would land in it (hardcoded template) is refused with 507
namespace_full. Re-requests for rows that already exist never come through
here — their bytes are already counted, and re-minting adds none.

Usage per namespace = observed bytes of `available` rows that still have a
cache entry (reaped objects drop out via the FK's ON DELETE SET NULL) plus the
DECLARED bytes of in-flight (`pending` / `uploaded`) rows, which reserve their
space before the object exists. The SQL below is the same text postgres-
exporter runs (deploy/jetstream2/monitoring/postgres_queries.yaml,
`af3cache_namespace`); tests/test_namespace_capacity.py pins them equal so the
gate and the dashboard can never disagree about what "used" means.

The snapshot is cached in-process for `namespace_usage_ttl_seconds` (60 s by
default): headroom at 10 % of a multi-TB store dwarfs a minute of uploads, and
this keeps one aggregate over ~750k index entries off the per-request path.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NamespaceFull
from app.core.metrics import record_namespace_state
from app.core.namespaces import NamespaceRegistry

log = logging.getLogger("af3cache.namespaces")

NAMESPACE_USAGE_SQL = """
select namespace,
       count(*) filter (where status = 'available' and cache_entry_id is not null)
         as available_count,
       count(*) filter (where status in ('pending','uploaded'))
         as inflight_count,
       coalesce(sum(observed_size_bytes)
                  filter (where status = 'available' and cache_entry_id is not null), 0)
     + coalesce(sum(declared_size_bytes)
                  filter (where status in ('pending','uploaded')), 0)
         as used_bytes
  from public.msa_ingestions
 where namespace is not null and status in ('pending','uploaded','available')
 group by namespace
"""


@dataclass(frozen=True)
class NamespaceUsage:
    namespace: str
    enabled: bool
    capacity_bytes: int
    used_bytes: int
    headroom_bytes: int
    headroom_pct: float
    min_headroom_pct: float
    accepting: bool
    available_count: int
    inflight_count: int


class NamespaceCapacityGate:
    def __init__(self, registry: NamespaceRegistry, ttl_seconds: int = 60):
        self.registry = registry
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, NamespaceUsage] | None = None
        self._cached_at = 0.0

    async def snapshot(
        self, session: AsyncSession, *, fresh: bool = False
    ) -> dict[str, NamespaceUsage]:
        """Usage for every configured namespace (rows for namespaces not in the
        registry are ignored; namespaces with no rows read as 0 used)."""
        now = time.monotonic()
        if (
            not fresh
            and self._cache is not None
            and now - self._cached_at < self.ttl_seconds
        ):
            return self._cache

        rows = (await session.execute(text(NAMESPACE_USAGE_SQL))).all()
        by_ns = {r.namespace: r for r in rows}
        out: dict[str, NamespaceUsage] = {}
        for ns, entry in self.registry.entries.items():
            r = by_ns.get(ns)
            used = int(r.used_bytes) if r is not None else 0
            cap = entry.capacity_bytes
            headroom = cap - used
            headroom_pct = (headroom / cap * 100.0) if cap > 0 else 0.0
            min_pct = self.registry.headroom_pct_for(ns)
            accepting = entry.enabled and headroom_pct > min_pct
            out[ns] = NamespaceUsage(
                namespace=ns,
                enabled=entry.enabled,
                capacity_bytes=cap,
                used_bytes=used,
                headroom_bytes=headroom,
                headroom_pct=headroom_pct,
                min_headroom_pct=min_pct,
                accepting=accepting,
                available_count=int(r.available_count) if r is not None else 0,
                inflight_count=int(r.inflight_count) if r is not None else 0,
            )
            record_namespace_state(
                ns, capacity_bytes=cap, min_headroom_pct=min_pct,
                enabled=entry.enabled, accepting=accepting,
            )
            if entry.enabled and not accepting and (
                self._cache is None or self._cache.get(ns, None) is None
                or self._cache[ns].accepting
            ):
                log.warning(
                    "namespace %s is within its headroom (%.1f%% free <= %.1f%%): "
                    "refusing new uploads (used=%d capacity=%d)",
                    ns, headroom_pct, min_pct, used, cap,
                )
        self._cache = out
        self._cached_at = now
        return out

    async def accepting(self, session: AsyncSession) -> list[str]:
        snap = await self.snapshot(session)
        return sorted(ns for ns, u in snap.items() if u.accepting)

    async def check(self, session: AsyncSession, namespace: str) -> None:
        """Refuse a NEW upload into `namespace` if it is enabled-but-full. An
        unknown namespace is not this gate's problem (the token issuer refuses
        it as not_configured); a disabled one is left to the picker."""
        snap = await self.snapshot(session)
        u = snap.get(namespace)
        if u is None or u.accepting or not u.enabled:
            return
        raise NamespaceFull(
            f"namespace {namespace} is within its capacity headroom "
            f"({u.headroom_pct:.1f}% free <= {u.min_headroom_pct:.1f}%)",
            extra=self._extra(snap),
        )

    def refuse_all(self, snap: dict[str, NamespaceUsage]) -> NamespaceFull:
        return NamespaceFull(
            "every enabled OSDF namespace is within its capacity headroom",
            extra=self._extra(snap),
        )

    @staticmethod
    def _extra(snap: dict[str, NamespaceUsage]) -> dict:
        return {
            "namespaces": {
                ns: {
                    "used_bytes": u.used_bytes,
                    "capacity_bytes": u.capacity_bytes,
                    "headroom_pct": round(u.headroom_pct, 2),
                    "min_headroom_pct": u.min_headroom_pct,
                }
                for ns, u in snap.items()
                if u.enabled
            }
        }
