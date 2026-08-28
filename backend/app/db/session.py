"""
Database engine and session management (async).

This module is the single place where the SQLAlchemy async `Engine`
and session factory are constructed and configured for PostgreSQL. It
purposefully contains no ORM models or business logic -- only the
low-level plumbing required to open, pool, and yield database
connections/sessions to the rest of the application.

Provides:
  - `engine`:        the SQLAlchemy `AsyncEngine`, configured with an
                      enterprise-grade PostgreSQL connection pool.
  - `AsyncSessionLocal`: an `async_sessionmaker` factory used to
                      instantiate short-lived, per-request
                      `AsyncSession` objects.
  - `get_db`:         a FastAPI dependency that yields an
                      `AsyncSession` and guarantees it is closed
                      after the request completes, even if an
                      exception is raised.
"""

import logging
from typing import AsyncGenerator

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
# The `AsyncEngine` owns the underlying connection pool to PostgreSQL
# and is created exactly once per process at import time. It wraps an
# async DBAPI driver (asyncpg) and is safe to share across the entire
# application lifetime.
#
#   - `settings.DATABASE_URL`: PostgreSQL DSN assembled/validated in
#     `app.core.config` (e.g. "postgresql+asyncpg://user:pass@host:port/db").
#   - `echo=settings.DB_ECHO`: toggles SQL statement logging; should
#     only be enabled for local debugging, never in production.
#   - `pool_pre_ping=True`: issues a lightweight liveness check before
#     handing out a pooled connection, transparently detecting and
#     recycling connections that PostgreSQL, a firewall, or a load
#     balancer has silently dropped. Essential for long-running
#     production services.
#   - `pool_size` / `max_overflow`: the baseline pool size and the
#     number of additional temporary connections allowed under load.
#   - `pool_timeout`: seconds to wait for a free connection from the
#     pool before raising `TimeoutError`.
#   - `pool_recycle`: forcibly recycles connections older than this
#     many seconds, guarding against database-side idle-connection
#     timeouts (e.g. managed PostgreSQL services that close
#     connections after a fixed idle period).
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DB_ECHO,
    pool_pre_ping=True,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_recycle=settings.DB_POOL_RECYCLE,
)

# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------
# `AsyncSessionLocal` is a callable factory: invoking it
# (`AsyncSessionLocal()`) produces a new `AsyncSession` bound to
# `engine`. A fresh session is created per request/unit-of-work and is
# never shared across concurrent tasks or requests.
#
#   - `autoflush=False`: pending changes are only flushed to the
#     database on explicit `flush()`/`commit()`/query execution,
#     giving the service/repository layer full control over write
#     timing.
#   - `expire_on_commit=False`: ORM instances remain usable (their
#     attributes stay accessible) after a `commit()`, which avoids
#     unnecessary implicit re-fetch queries -- important for FastAPI
#     response serialization that happens after the transaction
#     commits (e.g. returning a `User` via `UserResponse` right after
#     `UserRepository.create()`).
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autoflush=False,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that provides a transactional async database
    session scoped to the lifetime of a single request.

    Usage in a route (via a repository/service layer):

        @router.get("/leads")
        async def list_leads(db: AsyncSession = Depends(get_db)):
            ...

    Behavior:
        - Yields a fresh `AsyncSession` obtained from
          `AsyncSessionLocal`.
        - On any unhandled exception raised while the session is in
          use, the transaction is rolled back before the exception
          is re-raised, preventing partially-applied changes from
          leaking into subsequent operations on the same connection.
        - The session is always closed in the `finally` block,
          releasing its connection back to the pool regardless of
          whether the request succeeded, failed, or was cancelled.

    Yields:
        AsyncSession: an active SQLAlchemy async ORM session bound to
        `engine`.
    """
    async with AsyncSessionLocal() as db:
        try:
            yield db
        except HTTPException:
            # Expected control flow (401/404/409/etc. raised deliberately
            # by the service/router layers). Still roll back any partial
            # writes so the connection is returned to the pool in a
            # clean state, but do NOT log this as an error -- doing so
            # would flood logs with full tracebacks for routine 4xx
            # responses, drowning out genuine failures.
            await db.rollback()
            raise
        except Exception:
            # Anything else here is unexpected (a real DB/driver error,
            # a bug in application code, etc.) and warrants a full
            # traceback at ERROR level for investigation.
            logger.exception("Database session error; rolling back transaction.")
            await db.rollback()
            raise
        finally:
            await db.close()


async def dispose_engine() -> None:
    """
    Dispose of the module-level `AsyncEngine` and its connection pool.

    Intended to be called once, during application shutdown (e.g. from
    the FastAPI `lifespan` context manager's teardown phase), so that
    all pooled connections are closed cleanly rather than left open
    until the process exits. Safe to call even if the engine has not
    been used yet.

    Usage:
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            yield
            await dispose_engine()
    """
    await engine.dispose()