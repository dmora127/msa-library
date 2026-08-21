"""Effective runtime config: env (floor/default) overlaid by app_settings rows.

API keys are NEVER read from the DB — only from env Settings. Caching is added
in the admin-config phase; v0.1 reads the small settings table per request.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings

_KEYS = (
    "max_upload_size_bytes",
    "token_ttl_seconds",
    "osdf_namespace_template",
    "reap_grace_hours",
)


@dataclass(frozen=True)
class EffectiveSettings:
    max_upload_size_bytes: int
    token_ttl_seconds: int
    osdf_namespace_template: str
    reap_grace_hours: int


@dataclass(frozen=True)
class SourceRow:
    name: str
    enabled: bool
    idle_ttl_days: int | None
    notes: str | None


class SettingsService:
    def __init__(self, session: AsyncSession, settings: Settings):
        self.session = session
        self.settings = settings

    async def _overrides(self) -> dict[str, str]:
        rows = await self.session.execute(
            text("select key, value from public.app_settings where key = any(:k)"),
            {"k": list(_KEYS)},
        )
        return {k: v for k, v in rows.all()}

    async def effective(self) -> EffectiveSettings:
        o = await self._overrides()
        s = self.settings
        return EffectiveSettings(
            max_upload_size_bytes=int(
                o.get("max_upload_size_bytes", s.max_upload_size_bytes)
            ),
            token_ttl_seconds=int(o.get("token_ttl_seconds", s.token_ttl_seconds)),
            osdf_namespace_template=o.get(
                "osdf_namespace_template", s.osdf_namespace_template
            ),
            reap_grace_hours=int(o.get("reap_grace_hours", s.reap_grace_hours)),
        )

    async def list_sources(self) -> list[SourceRow]:
        rows = await self.session.execute(
            text(
                "select name, enabled, idle_ttl_days, notes "
                "from public.msa_sources order by name"
            )
        )
        return [SourceRow(**dict(r._mapping)) for r in rows]

    async def set_source_enabled(self, name: str, enabled: bool) -> bool:
        """Toggle msa_sources.enabled. Returns False if the source is unknown."""
        res = await self.session.execute(
            text(
                "update public.msa_sources set enabled = :e where name = :n"
            ),
            {"e": enabled, "n": name},
        )
        return res.rowcount > 0

    async def source_enabled(self, name: str) -> bool | None:
        """True/False, or None if the source does not exist."""
        return (
            await self.session.execute(
                text("select enabled from public.msa_sources where name = :n"),
                {"n": name},
            )
        ).scalar_one_or_none()

    async def set(self, key: str, value: str, updated_by: str | None = None) -> None:
        if key not in _KEYS:
            raise ValueError(f"unknown setting: {key!r}")
        await self.session.execute(
            text(
                """
                insert into public.app_settings(key, value, updated_by)
                values (:k, :v, :by)
                on conflict (key) do update
                  set value = excluded.value,
                      updated_by = excluded.updated_by,
                      updated_at = now()
                """
            ),
            {"k": key, "v": value, "by": updated_by},
        )
