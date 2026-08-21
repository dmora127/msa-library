"""Async SQLAlchemy engine, session factory, declarative base."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None) -> AsyncEngine:
    # Pool tuned for OSDF-verify load: /complete used to hold a connection
    # idle across the long `pelican object get` subprocess; verify now releases
    # the session pre-fetch, but headroom + a longer wait still buffer bursts.
    return create_async_engine(
        url or get_settings().database_url,
        pool_pre_ping=True, future=True,
        pool_size=20, max_overflow=40, pool_timeout=60,
    )


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _sessionmaker
