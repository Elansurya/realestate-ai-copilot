"""
backend/alembic/env.py

Alembic migration environment configuration for the Real Estate AI
Copilot CRM.

Responsibilities:
    - Configure the Alembic runtime context for both offline (SQL
      script generation) and online (live database connection)
      migration modes.
    - Source the database connection URL from the application's own
      settings object (app.core.config.settings), so alembic.ini never
      needs to hardcode credentials.
    - Import Base and all ORM models so that `Base.metadata` is fully
      populated for `--autogenerate` to detect schema changes across
      every table in the project.

Design Notes:
    - The application uses an async SQLAlchemy engine/driver (e.g.
      asyncpg) for runtime request handling, but Alembic's core
      migration-running machinery is synchronous. This module bridges
      that gap using SQLAlchemy's `async_engine_from_config` combined
      with `connection.run_sync(...)`, which is the standard,
      recommended pattern for running Alembic against an async
      SQLAlchemy application without requiring a second, separate sync
      driver/URL.
    - Every model module must be imported here (even though none of
      their names are referenced directly) purely for their import-time
      side effect of registering their table on `Base.metadata`. Add
      new model imports to this block as new domains are introduced
      (Property, Booking, Customer, Payment, etc.) so autogenerate
      continues to see the full schema.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, async_engine_from_config

from app.core.config import settings

# --------------------------------------------------------------------------
# Model Imports (required for autogenerate to see all tables)
# --------------------------------------------------------------------------
# `app.models` itself imports every ORM model module (see its own
# docstring), which is what actually triggers SQLAlchemy's declarative
# registration of every table against `Base.metadata` before Alembic
# inspects it for autogeneration. Importing `Base` from `app.models`
# rather than `app.db.base` directly guarantees that side effect always
# runs first, regardless of import order elsewhere.
# --------------------------------------------------------------------------
from app.models import Base  # noqa: F401

# --------------------------------------------------------------------------
# Alembic Config Object
# --------------------------------------------------------------------------
# Provides access to the values within alembic.ini.
# --------------------------------------------------------------------------
config = context.config

# --------------------------------------------------------------------------
# Logging Configuration
# --------------------------------------------------------------------------
# Interprets the [loggers]/[handlers]/[formatters] sections of
# alembic.ini, wiring up Python's standard logging module accordingly.
# --------------------------------------------------------------------------
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# --------------------------------------------------------------------------
# Database URL Injection
# --------------------------------------------------------------------------
# The connection URL is sourced exclusively from the application's own
# settings (environment-driven, 12-factor compliant) rather than from a
# hardcoded value in alembic.ini. `settings.DATABASE_URL` is expected to
# be an async-driver URL (e.g. "postgresql+asyncpg://...").
# --------------------------------------------------------------------------
config.set_main_option(
    "sqlalchemy.url",
    settings.DATABASE_URL.replace("%", "%%")
)

# --------------------------------------------------------------------------
# Target Metadata
# --------------------------------------------------------------------------
# `target_metadata` is used by Alembic's `--autogenerate` support to
# compare the current database schema against the ORM's declared
# schema, producing the appropriate migration operations.
# --------------------------------------------------------------------------
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    Configures the context with just a URL and not an Engine, so no
    `DBAPI` connection is required. Calls to `context.execute()` emit
    the given string directly to the migration script output, making
    this suitable for generating SQL scripts without a live database
    connection.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """
    Configure and execute migrations against a live, already-established
    database connection.

    Args:
        connection: A synchronous-facing `Connection` object, obtained
            via `AsyncConnection.run_sync(...)` in `run_migrations_online`.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """
    Create an async Engine from the Alembic config, connect to the
    database, and run migrations via `run_sync`, bridging Alembic's
    synchronous migration-running API with the project's async
    SQLAlchemy engine.
    """
    connectable: AsyncEngine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode.

    Creates an async Engine and associates a connection with the
    Alembic migration context, then executes the migration scripts
    against that live connection.
    """
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()