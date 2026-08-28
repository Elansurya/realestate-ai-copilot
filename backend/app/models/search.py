"""
backend/app/models/search.py

SQLAlchemy 2.x (async) ORM model for the Global Search module of the
Enterprise Real Estate AI Copilot CRM.

Every executed search (across Customers, Leads, Properties, Bookings,
Payments, Tasks, Documents, Workflow, Activity Timeline, Audit Logs,
Notifications, and Reports) is recorded as a ``SearchHistory`` row.
This gives the CRM a per-user search history/audit trail and a data
source for search analytics (most-searched modules, average latency,
etc.), without this module owning or duplicating any of the searched
entities themselves -- it only records *that* a search happened, with
what parameters, and how many results it returned.

Conventions (mirrors `app/models/task.py` / `app/models/activity.py`):
    - `Base` comes from `app.db.base`.
    - The primary key is a server-generated PostgreSQL UUID via
      `func.gen_random_uuid()` (requires the `pgcrypto` extension,
      already enabled by earlier migrations in this project).
    - `user_id` is an `Integer` FK to `users.id`, matching
      `User.id`'s actual type (see `app/models/user.py`), consistent
      with `Task.assigned_to_id` / `Task.created_by_id` in
      `app/models/task.py`.
    - Enums are native PostgreSQL ENUM types (via SQLAlchemy's
      `Enum`) for strong data-integrity at the database level.
    - Timestamps are timezone-aware (UTC).
    - `User` is imported only under `TYPE_CHECKING` to avoid a
      runtime circular-import surface. The relationship to `User` is
      intentionally one-directional (no `back_populates`) so this
      module does not require any change to `app/models/user.py`.
    - This model deliberately does NOT hold a hard foreign key to the
      searched entity (e.g. a specific Customer/Lead/Property row):
      a single search can span multiple modules and match many
      entities across them, so there is no single row for a FK to
      point at. Per-result detail belongs to the (non-persisted)
      `SearchResult` response schema built at query time by the
      service layer, not to this history record.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

__all__ = [
    "SearchModule",
    "SearchType",
    "SearchHistory",
]

if TYPE_CHECKING:
    from app.models.user import User


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class SearchModule(str, enum.Enum):
    """Enumerates the domain modules the Global Search module can query.

    Attributes:
        CUSTOMER: The Customers module.
        LEAD: The Leads module.
        PROPERTY: The Properties module.
        BOOKING: The Bookings module.
        PAYMENT: The Payments module.
        TASK: The Task Management module.
        DOCUMENT: The Documents module.
        WORKFLOW: The Workflow module.
        ACTIVITY: The Activity Timeline module.
        AUDIT_LOG: The Audit Logs module.
        NOTIFICATION: The Notifications module.
        REPORT: The Reports module.
    """

    CUSTOMER = "customer"
    LEAD = "lead"
    PROPERTY = "property"
    BOOKING = "booking"
    PAYMENT = "payment"
    TASK = "task"
    DOCUMENT = "document"
    WORKFLOW = "workflow"
    ACTIVITY = "activity"
    AUDIT_LOG = "audit_log"
    NOTIFICATION = "notification"
    REPORT = "report"


class SearchType(str, enum.Enum):
    """Enumerates the kind of search operation that was executed.

    Attributes:
        QUICK: A lightweight, minimal-filter search (e.g. an
            omnibox-style keyword lookup).
        ADVANCED: A search using multiple structured filter criteria.
        FILTERED: A search scoped to one or more specific
            `SearchModule` values via `SearchHistory.module`/filters.
        GLOBAL: An unscoped search executed across every searchable
            module at once.
        SAVED: A previously saved/re-run search.
    """

    QUICK = "quick"
    ADVANCED = "advanced"
    FILTERED = "filtered"
    GLOBAL = "global"
    SAVED = "saved"


# ---------------------------------------------------------------------------
# SearchHistory
# ---------------------------------------------------------------------------
class SearchHistory(Base):
    """Represents a single executed search recorded for a user.

    Attributes:
        id: Surrogate primary key (UUID v4).
        user_id: FK to the user who performed the search.
        search_query: The raw free-text query string the user searched for.
        module: The single module the search was scoped to, if any.
            ``NULL`` when the search was executed across multiple/all
            modules at once (see :attr:`SearchType.GLOBAL`).
        search_type: The kind of search operation performed, see
            :class:`SearchType`.
        filters: Arbitrary JSONB payload capturing the structured
            filter criteria supplied with the search (e.g. date
            ranges, status, priority), for reproducibility and
            analytics.
        result_count: The number of results the search returned.
        execution_time_ms: How long the search took to execute, in
            milliseconds.
        is_deleted: Soft-delete flag (e.g. a user clearing their
            search history).
        deleted_at: Timestamp of soft deletion, if any.
        created_at: Record creation timestamp (i.e. when the search
            was executed).
        updated_at: Record last-update timestamp.
        user: Relationship to the searching ``User`` (one-directional).
    """

    __tablename__ = "search_history"
    __table_args__ = (
        Index("ix_search_history_user_id", "user_id"),
        Index("ix_search_history_module", "module"),
        Index("ix_search_history_search_type", "search_type"),
        Index("ix_search_history_created_at", "created_at"),
        Index("ix_search_history_is_deleted", "is_deleted"),
        # Composite indexes for common query patterns.
        Index(
            "ix_search_history_user_id_created_at", "user_id", "created_at"
        ),
        Index("ix_search_history_user_id_module", "user_id", "module"),
        Index("ix_search_history_module_created_at", "module", "created_at"),
        Index(
            "ix_search_history_search_type_created_at",
            "search_type",
            "created_at",
        ),
        # Data integrity check constraints.
        CheckConstraint(
            "btrim(search_query) <> ''",
            name="ck_search_history_query_not_empty",
        ),
        CheckConstraint(
            "result_count >= 0",
            name="ck_search_history_result_count_non_negative",
        ),
        CheckConstraint(
            "execution_time_ms >= 0",
            name="ck_search_history_execution_time_non_negative",
        ),
        CheckConstraint(
            "(is_deleted IS FALSE AND deleted_at IS NULL) "
            "OR (is_deleted IS TRUE AND deleted_at IS NOT NULL)",
            name="ck_search_history_soft_delete_consistency",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    search_query: Mapped[str] = mapped_column(String(500), nullable=False)

    module: Mapped[Optional[SearchModule]] = mapped_column(
        SAEnum(SearchModule, name="search_module_enum", native_enum=True),
        nullable=True,
    )

    search_type: Mapped[SearchType] = mapped_column(
        SAEnum(SearchType, name="search_type_enum", native_enum=True),
        nullable=False,
        default=SearchType.GLOBAL,
        server_default=SearchType.GLOBAL.name,
    )

    filters: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    result_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    execution_time_ms: Mapped[float] = mapped_column(
        Numeric(10, 3), nullable=False, default=0, server_default="0"
    )

    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    user: Mapped["User"] = relationship(
        "User",
        foreign_keys=[user_id],
        lazy="selectin",
        viewonly=True,
    )

    def __repr__(self) -> str:
        """Returns an unambiguous, debug-friendly representation of the record.

        Returns:
            str: A concise representation including id, user_id,
            module, search_type, and result_count.
        """
        return (
            f"<SearchHistory id={self.id} user_id={self.user_id!r} "
            f"module={self.module!r} search_type={self.search_type!r} "
            f"result_count={self.result_count!r}>"
        )