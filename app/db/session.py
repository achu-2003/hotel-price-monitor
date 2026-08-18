"""Database engines and session factories.

Two engines on purpose:

* **async** (asyncpg) for FastAPI — the API is I/O bound and benefits from
  async concurrency.
* **sync** (psycopg3) for Celery workers and Alembic — the sync Playwright API
  cannot run inside an event loop, and Celery prefork does not provide one.
  Trying to share one async engine across both is a well-known source of
  "attached to a different loop" failures.

Engines are created lazily so importing a model in a test or a script does not
open a connection.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache(maxsize=1)
def get_async_engine():
    settings = get_settings()
    return create_async_engine(
        settings.database_url_async,
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,      # survive a Postgres restart without 500s
        pool_recycle=1800,
        echo=False,              # never echo SQL: bind params can carry secrets
    )


@lru_cache(maxsize=1)
def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_async_engine(),
        class_=AsyncSession,
        expire_on_commit=False,  # let handlers read attributes after commit
        autoflush=False,
    )


@lru_cache(maxsize=1)
def get_sync_engine():
    settings = get_settings()
    return create_engine(
        settings.database_url_sync,
        # Workers are few and long-lived; a small pool per process is plenty
        # and keeps total connections well under Postgres max_connections when
        # worker processes are recycled.
        pool_size=5,
        max_overflow=2,
        pool_pre_ping=True,
        pool_recycle=1800,
        echo=False,
    )


@lru_cache(maxsize=1)
def get_sync_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_sync_engine(), expire_on_commit=False, autoflush=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency. Rolls back on any unhandled exception."""
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@contextmanager
def sync_session() -> Generator[Session, None, None]:
    """Session for Celery tasks.

    Commits on clean exit, rolls back on failure. Every task that touches the
    database should hold one of these for as short a time as possible: a
    browser fetch takes 20-40s and must NOT hold a transaction open while it
    runs, or connections pile up and Postgres starts refusing them.
    """
    factory = get_sync_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def dispose_engines() -> None:
    """Called from the FastAPI lifespan shutdown hook."""
    await get_async_engine().dispose()
