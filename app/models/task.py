"""
backend/app/models/task.py

SQLAlchemy 2.x (async) ORM model for the Task Management module of the
Enterprise Real Estate AI Copilot CRM.

A ``Task`` is a discrete, assignable unit of work raised either
directly by a user or indirectly by another domain module (Lead,
Customer, Property, Booking, Payment, Workflow, Document, etc.) via
``related_module`` / ``related_entity_id``. Tasks support assignment
and reassignment, due dates and reminders, lifecycle status tracking,
completion/cancellation, and lightweight denormalized counters for
comments and attachments (the comment/attachment records themselves
are owned by their own, separately-scoped tables/modules and are not
part of this model).

Conventions (mirrors `app/models/activity.py` / `app/models/workflow.py`):
    - `Base` comes from `app.db.base`.
    - Primary keys are server-generated PostgreSQL UUIDs via
      `func.gen_random_uuid()` (requires the `pgcrypto` extension,
      already enabled by earlier migrations in this project).
    - The module is polymorphically attached to the entity it concerns
      via `related_module` / `related_entity_id` (stored as text)
      rather than a hard foreign key, since those entities use
      different primary key types (UUID vs. Integer) across the
      project -- the same tradeoff already accepted in
      `app/models/activity.py` and `app/models/workflow.py`.
    - `assigned_to_id` / `created_by_id` / `completed_by_id` are
      `Integer` FKs to `users.id`, matching `User.id`'s actual type
      (see `app/models/user.py`).
    - Enums are native PostgreSQL ENUM types (via SQLAlchemy's
      `Enum`) for strong data-integrity at the database level.
    - Timestamps are timezone-aware (UTC).
    - `User` is imported only under `TYPE_CHECKING` to avoid a
      runtime circular-import surface. Relationships to `User` are
      intentionally one-directional (no `back_populates`) so this
      module does not require any change to `app/models/user.py`.
    - `is_overdue` is intentionally NOT a persisted column/status: it
      is a point-in-time derived fact (`due_date < now()` while the
      task is not yet in a terminal state), so it is exposed only as
      a read-only, non-persisted Python property to avoid the schema
      ever drifting from the current time.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

__all__ = [
    "TaskStatus",
    "TaskPriority",
    "TaskType",
    "Task",
]

if TYPE_CHECKING:
    from app.models.user import User


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class TaskStatus(str, enum.Enum):
    """Enumerates the lifecycle status of a Task.

    Attributes:
        PENDING: The task has been created but not yet started.
        IN_PROGRESS: The task is actively being worked on.
        ON_HOLD: The task is temporarily paused.
        COMPLETED: The task has been completed.
        CANCELLED: The task was cancelled before completion.

    Note:
        "Overdue" is deliberately not a member of this enum. Whether a
        task is overdue is a function of `due_date` versus the current
        time and is exposed via the non-persisted `Task.is_overdue`
        property rather than stored as a status, to avoid the two
        drifting out of sync.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    ON_HOLD = "on_hold"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TaskPriority(str, enum.Enum):
    """Enumerates the priority classification of a Task.

    Attributes:
        LOW: Routine, low-urgency task.
        NORMAL: Standard priority requiring no special attention.
        HIGH: Noteworthy task that may warrant prompt attention.
        URGENT: Time-sensitive task requiring immediate attention.
    """

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class TaskType(str, enum.Enum):
    """Enumerates the category of work a Task represents.

    Attributes:
        GENERAL: Unclassified/general-purpose task.
        FOLLOW_UP: A follow-up with a lead, customer, or partner.
        CALL: A phone call to be made.
        EMAIL: An email to be sent.
        MEETING: A meeting to be held or attended.
        SITE_VISIT: A physical property/site visit.
        DOCUMENT_REVIEW: Review or verification of a document.
        PAYMENT_FOLLOW_UP: Follow-up related to a pending/failed payment.
        APPROVAL: A task representing a pending approval decision.
        OTHER: A task that does not fit any other category.
    """

    GENERAL = "general"
    FOLLOW_UP = "follow_up"
    CALL = "call"
    EMAIL = "email"
    MEETING = "meeting"
    SITE_VISIT = "site_visit"
    DOCUMENT_REVIEW = "document_review"
    PAYMENT_FOLLOW_UP = "payment_follow_up"
    APPROVAL = "approval"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------
class Task(Base):
    """Represents a single, assignable unit of work in the Task module.

    Attributes:
        id: Surrogate primary key (UUID v4).
        title: Short, human-readable headline for the task.
        description: Optional longer free-text description of the work.
        task_type: Category of work, see :class:`TaskType`.
        status: Current lifecycle status, see :class:`TaskStatus`.
        priority: Priority classification, see :class:`TaskPriority`.
        due_date: Optional target completion timestamp.
        reminder_time: Optional timestamp at which a reminder should be
            raised (e.g. consumed by the Notification module).
        assigned_to_id: FK to the user currently responsible for the
            task. Nullable to support unassigned tasks.
        created_by_id: FK to the user (or system actor) who created the
            task. Nullable to support system-initiated tasks raised by
            other modules (e.g. Workflow).
        related_module: Name of the owning domain module this task
            concerns (e.g. ``"lead"``, ``"booking"``), if any. Stored
            as free text rather than a hard foreign key, mirroring the
            same tradeoff already accepted in `app/models/activity.py`
            and `app/models/workflow.py`.
        related_entity_id: Primary key of the related entity within
            `related_module`, stored as text to remain agnostic to the
            referenced entity's key type (UUID or Integer).
        comments_count: Denormalized count of comments attached to this
            task (comment records are owned by another module/table).
        attachments_count: Denormalized count of attachment metadata
            entries attached to this task (attachment records are
            owned by another module/table).
        meta_data: Arbitrary JSONB payload for task-specific context
            (stored under the ``metadata`` column), e.g. Workflow or
            Notification integration details.
        completed_at: Timestamp the task was marked completed, if any.
        completed_by_id: FK to the user who completed the task, if any.
        is_deleted: Soft-delete flag.
        deleted_at: Timestamp of soft deletion, if any.
        created_at: Record creation timestamp.
        updated_at: Record last-update timestamp.
        assigned_to: Relationship to the assigned ``User`` (one-directional).
        created_by: Relationship to the creating ``User`` (one-directional).
        completed_by: Relationship to the completing ``User`` (one-directional).
    """

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_status", "status"),
        Index("ix_tasks_priority", "priority"),
        Index("ix_tasks_task_type", "task_type"),
        Index("ix_tasks_assigned_to_id", "assigned_to_id"),
        Index("ix_tasks_created_by_id", "created_by_id"),
        Index("ix_tasks_completed_by_id", "completed_by_id"),
        Index("ix_tasks_due_date", "due_date"),
        Index("ix_tasks_reminder_time", "reminder_time"),
        Index("ix_tasks_related_module", "related_module"),
        Index("ix_tasks_related_entity_id", "related_entity_id"),
        Index("ix_tasks_created_at", "created_at"),
        # Composite indexes for common query patterns.
        Index(
            "ix_tasks_related_module_related_entity_id",
            "related_module",
            "related_entity_id",
        ),
        Index("ix_tasks_assigned_to_id_status", "assigned_to_id", "status"),
        Index("ix_tasks_assigned_to_id_due_date", "assigned_to_id", "due_date"),
        Index("ix_tasks_status_due_date", "status", "due_date"),
        Index("ix_tasks_status_priority", "status", "priority"),
        # Data integrity check constraints.
        CheckConstraint("btrim(title) <> ''", name="ck_tasks_title_not_empty"),
        CheckConstraint(
            "comments_count >= 0", name="ck_tasks_comments_count_non_negative"
        ),
        CheckConstraint(
            "attachments_count >= 0", name="ck_tasks_attachments_count_non_negative"
        ),
        CheckConstraint(
            "(related_module IS NULL AND related_entity_id IS NULL) "
            "OR (related_module IS NOT NULL AND related_entity_id IS NOT NULL)",
            name="ck_tasks_related_entity_pair_consistency",
        ),
        CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL) "
            "OR (status <> 'completed' AND completed_at IS NULL)",
            name="ck_tasks_completed_at_consistency",
        ),
        CheckConstraint(
            "completed_by_id IS NULL OR status = 'completed'",
            name="ck_tasks_completed_by_consistency",
        ),
        CheckConstraint(
            "reminder_time IS NULL OR due_date IS NULL OR reminder_time <= due_date",
            name="ck_tasks_reminder_before_due_date",
        ),
        CheckConstraint(
            "(is_deleted IS FALSE AND deleted_at IS NULL) "
            "OR (is_deleted IS TRUE AND deleted_at IS NOT NULL)",
            name="ck_tasks_soft_delete_consistency",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    task_type: Mapped[TaskType] = mapped_column(
        SAEnum(
            TaskType, name="task_type_enum", native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=TaskType.GENERAL,
        server_default=TaskType.GENERAL.value,
    )

    status: Mapped[TaskStatus] = mapped_column(
        SAEnum(
            TaskStatus, name="task_status_enum", native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=TaskStatus.PENDING,
        server_default=TaskStatus.PENDING.value,
    )

    priority: Mapped[TaskPriority] = mapped_column(
        SAEnum(
            TaskPriority, name="task_priority_enum", native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=TaskPriority.NORMAL,
        server_default=TaskPriority.NORMAL.value,
    )

    due_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reminder_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    assigned_to_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    related_module: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    related_entity_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )

    comments_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    attachments_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    meta_data: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONB, nullable=True
    )

    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_by_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
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

    assigned_to: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[assigned_to_id],
        lazy="selectin",
        viewonly=True,
    )
    created_by: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[created_by_id],
        lazy="selectin",
        viewonly=True,
    )
    completed_by: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[completed_by_id],
        lazy="selectin",
        viewonly=True,
    )

    # ------------------------------------------------------------------
    # Derived, non-persisted properties
    # ------------------------------------------------------------------
    @property
    def is_overdue(self) -> bool:
        """Indicates whether this task is currently overdue.

        A task is overdue when it has a `due_date` in the past and has
        not reached a terminal status (`completed` or `cancelled`).
        This is computed on read rather than persisted, since it is a
        function of the current time and would otherwise drift stale.

        Returns:
            bool: ``True`` if the task is overdue, ``False`` otherwise.
        """
        if self.due_date is None:
            return False
        if self.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
            return False
        now = datetime.now(timezone.utc)
        due_date = self.due_date
        if due_date.tzinfo is None:
            due_date = due_date.replace(tzinfo=timezone.utc)
        return due_date < now

    def __repr__(self) -> str:
        """Returns an unambiguous, debug-friendly representation of the task.

        Returns:
            str: A concise representation including id, title, status,
            priority, and assignee.
        """
        return (
            f"<Task id={self.id} title={self.title!r} status={self.status!r} "
            f"priority={self.priority!r} assigned_to_id={self.assigned_to_id!r}>"
        )