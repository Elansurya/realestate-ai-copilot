"""
Declarative Base for all SQLAlchemy ORM models.

This module defines the single, shared `Base` class that every ORM
model in the application (created under `app/models/` in a later
phase) MUST inherit from. Centralizing the declarative base here
(rather than in `session.py` or inside individual model files) is a
deliberate architectural decision for the following reasons:

  - Avoids circular imports: `session.py` (engine/session wiring)
    never needs to import model classes, and model modules only
    need to import `Base` from this single, lightweight module.
  - Single source of truth for `Base.metadata`, which Alembic will
    use (in the migrations phase) to autogenerate schema diffs by
    comparing it against the live PostgreSQL database.
  - Enforces a consistent, enterprise-grade constraint naming
    convention across every table, so that indexes, unique
    constraints, foreign keys, and check constraints are named
    deterministically instead of relying on database-driver
    defaults. This is critical for reliable Alembic autogeneration
    and for constraints to be identifiable/droppable by name in
    production incident response.

NOTE (scope of this phase):
  This module intentionally contains ONLY the declarative base
  infrastructure. No ORM models, mixins, or table definitions are
  declared here -- those belong to `app/models/` and are introduced
  in a subsequent phase.
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# ---------------------------------------------------------------------------
# Naming convention for schema-level constraints and indexes
# ---------------------------------------------------------------------------
# PostgreSQL (and SQLAlchemy) will otherwise assign auto-generated,
# non-deterministic names to unnamed constraints/indexes. Explicitly
# fixing a naming convention here ensures:
#   - Alembic can reliably detect and generate ALTER/DROP statements
#     for constraints across environments (dev, staging, production).
#   - Constraint names are predictable and greppable in the database
#     (e.g. `ix_leads_email`, `uq_users_email`, `fk_leads_user_id_users`).
#
# Convention keys map to SQLAlchemy constraint/index types:
#   ix  -> Index
#   uq  -> UniqueConstraint
#   ck  -> CheckConstraint
#   fk  -> ForeignKeyConstraint
#   pk  -> PrimaryKeyConstraint
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Shared MetaData instance carrying the naming convention above.
# Every model's table will be registered against this single
# MetaData object via `Base.metadata`, giving Alembic one complete,
# authoritative view of the target schema.
metadata_obj = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    """
    Shared declarative base class for all ORM models.

    Uses SQLAlchemy 2.0's typed `DeclarativeBase` (rather than the
    legacy `declarative_base()` factory function) to gain:
      - Full static type-checker support (mypy/pyright) for
        `Mapped[...]` attribute annotations declared on subclasses.
      - A single, explicit, importable base class instead of a
        dynamically generated one.
      - Forward compatibility with SQLAlchemy's ongoing typed-ORM
        direction.

    Every model class defined under `app/models/` must inherit from
    this `Base`, e.g.:

        class Lead(Base):
            __tablename__ = "leads"
            id: Mapped[int] = mapped_column(primary_key=True)
            ...

    Attributes:
        metadata: Bound to `metadata_obj`, so all tables declared by
            subclasses share the enterprise naming convention and a
            single `MetaData` registry usable by Alembic for
            autogenerating migrations.
    """

    metadata = metadata_obj