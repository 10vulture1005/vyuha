# db/session.py
"""Database engine and session factories for VYUHA.

Provides:
    - Synchronous engine + session for CLI/Cron/Backtest workloads
    - Async engine + session for the FastAPI service layer (Phase 7)
    - Context managers: get_session() and get_async_session()
"""
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from config.settings import settings


def _build_sync_engine():
    """Create the synchronous SQLAlchemy engine based on DATABASE_URL."""
    connect_args = {}
    # SQLite needs check_same_thread=False for multi-threaded use
    if settings.DATABASE_URL.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    return create_engine(
        settings.DATABASE_URL,
        pool_pre_ping=True,
        echo=False,
        connect_args=connect_args,
    )


sync_engine = _build_sync_engine()
SyncSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Synchronous session context manager with automatic rollback on exception.

    Usage:
        with get_session() as session:
            session.add(some_row)
            # auto-committed on clean exit, auto-rolled-back on exception
    """
    session = SyncSessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session_dependency() -> Generator[Session, None, None]:
    """Synchronous session generator for FastAPI Depends()."""
    session = SyncSessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ─── Async Engine (activated when PostgreSQL + asyncpg is available) ───────────
# Lazy-initialized to avoid ImportError when asyncpg isn't installed (dev/SQLite)

_async_engine = None
_AsyncSessionLocal = None


def _init_async():
    """Initialize async engine and session factory (requires asyncpg)."""
    global _async_engine, _AsyncSessionLocal
    if _async_engine is not None:
        return

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

    async_db_url = (
        settings.DATABASE_URL
        .replace("postgresql://", "postgresql+asyncpg://")
        .replace("postgresql+psycopg2://", "postgresql+asyncpg://")
        .replace("sqlite:///", "sqlite+aiosqlite:///")
    )
    _async_engine = create_async_engine(
        async_db_url, pool_size=10, max_overflow=20, pool_pre_ping=True, echo=False
    )
    _AsyncSessionLocal = async_sessionmaker(
        autocommit=False, autoflush=False, bind=_async_engine, class_=AsyncSession
    )


async def get_async_session():
    """Asynchronous session context manager for FastAPI.

    Lazily initializes the async engine on first call.
    Only works with PostgreSQL + asyncpg.
    """
    _init_async()
    async with _AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
