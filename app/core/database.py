"""
backend/app/core/database.py

Compatibility re-export layer for the async database engine/session
plumbing.

Context:
    The actual `AsyncEngine`, `async_sessionmaker`, declarative `Base`,
    and FastAPI session dependency are defined once in `app.db.session`
    (see that module for the full implementation and configuration
    rationale: connection pooling, `pool_pre_ping`, `pool_recycle`,
    transaction/rollback handling, etc.). This module exists ONLY
    because some modules in the codebase (e.g.
    `app.api.v1.endpoints.customer`, `app.api.v1.auth`) import a
    `get_async_session` symbol from `app.core.database`, which
    previously did not exist -- causing a `ModuleNotFoundError` at
    import time and preventing the application from starting.

    Additionally, some modules (typically SQLAlchemy models) import the
    declarative `Base` class from `app.core.database`. `Base` is
    defined once in `app.db.session` alongside the engine; it is
    re-exported here rather than re-declared, so that every model in
    the codebase -- regardless of which of these two paths it imports
    `Base` from -- registers against the exact same
    `declarative_base()` metadata object. Declaring a second, separate
    `Base` here would silently split the ORM's mapper registry/metadata
    in two, which would make Alembic autogenerate and
    `Base.metadata.create_all()` blind to whichever half of the models
    imported the "other" `Base` -- a difficult-to-diagnose class of bug.

Design Notes:
    - This module intentionally contains NO engine/session/Base
      construction logic of its own. `app.db.session` remains the
      single source of truth; duplicating it here would risk two
      independently-configured connection pools (or two divergent ORM
      metadata registries) targeting the same database, which is a
      common source of subtle production bugs (e.g., pool exhaustion,
      inconsistent `pool_recycle` settings, missing tables/migrations).
    - `get_async_session` is a direct alias of `get_db` (identical
      generator function object), not a re-implementation, so both
      names are guaranteed to always behave identically and can never
      drift apart.
    - Existing call sites using `Depends(get_async_session)` continue to
      work unmodified; new code should prefer importing `get_db`
      directly from `app.db.session` to avoid this indirection.
"""

from __future__ import annotations

from app.db.base import Base
from app.db.session import AsyncSessionLocal, dispose_engine, engine, get_db

# Alias preserved for backward compatibility with existing call sites
# (`app.api.v1.endpoints.customer`, `app.api.v1.auth`) that depend on
# this exact name.
get_async_session = get_db

# Alias preserved for backward compatibility with existing call sites
# (`app.workers.notification_worker`) that depend on this exact name
# for constructing standalone sessions outside of a FastAPI request.
async_session_factory = AsyncSessionLocal

__all__ = [
    "engine",
    "AsyncSessionLocal",
    "async_session_factory",
    "Base",
    "get_db",
    "get_async_session",
    "dispose_engine",
]