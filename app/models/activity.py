"""
backend/app/models/activity.py

SQLAlchemy 2.x (async) ORM model for the Activity Timeline module of
the Enterprise Real Estate AI Copilot CRM.

The Activity Timeline is a cross-cutting, append-mostly feed that
records notable events raised by every other domain module (Customer,
Lead, Property, Booking, Payment, Workflow, Notification, AI, Audit,
Document, Settings). A single ``Activity`` row answers: *what*
happened (``action`` / ``title`` / ``description``), *where* it
happened (``module`` / ``entity_type`` / ``entity_id``), *who* it
happened to/for (``performed_by_id`` / ``assigned_to_id``), and *how*
the state changed (``old_value`` / ``new_value`` / ``metadata``).

Conventions (mirrors `app/models/workflow.py` / `app/models/audit_log.py`):
    - `Base` comes from `app.db.base`.
    - Primary keys are server-generated PostgreSQL UUIDs via
      `func.gen_random_uuid()` (requires the `pgcrypto` extension,
      already enabled by earlier migrations in this project).
    - The module is polymorphically attached to the entity it
      describes via `entity_type` / `entity_id` (stored as text)
      rather than a hard foreign key, since those entities use
      different primary key types (UUID vs. Integer) across the
      project -- the same tradeoff already accepted in
      `app/models/workflow.py` and `app/models/audit_log.py`.
    - `performed_by_id` / `assigned_to_id` are `Integer` FKs to
      `users.id`, matching `User.id`'s actual type (see
      `app/models/user.py`).
    - Enums are native PostgreSQL ENUM types (via SQLAlchemy's
      `Enum`) for strong data-integrity at the database level.
    - Timestamps are timezone-aware (UTC).
    - `User` is imported only under `TYPE_CHECKING` to avoid a
      runtime circular-import surface. Relationships to `User` are
      intentionally one-directional (no `back_populates`) so this
      module does not require any change to `app/models/user.py`.
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
    String,
    Text,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

__all__ = [
    "ActivityType",
    "ActivityModule",
    "ActivityPriority",
    "ActivityStatus",
    "Activity",
]

if TYPE_CHECKING:
    from app.models.user import User


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class ActivityType(str, enum.Enum):
    """Enumerates the discrete action performed for a timeline entry.

    Attributes:
        CREATED: A new entity was created.
        UPDATED: An existing entity was modified.
        DELETED: An entity was deleted (soft or hard).
        RESTORED: A previously deleted entity was restored.
        ARCHIVED: An entity was archived.
        STATUS_CHANGED: An entity's status/lifecycle state changed.
        ASSIGNED: An entity was assigned to a user/team.
        UNASSIGNED: An entity was unassigned from a user/team.
        APPROVED: An entity or workflow step was approved.
        REJECTED: An entity or workflow step was rejected.
        UPLOADED: A file or document was uploaded.
        DOWNLOADED: A file or document was downloaded.
        VIEWED: An entity or document was viewed/opened.
        COMMENTED: A comment or note was added to an entity.
        SCHEDULED: A meeting, visit, or task was scheduled.
        CANCELLED: A scheduled item or process was cancelled.
        COMPLETED: A process or task reached completion.
        PAYMENT_RECEIVED: A payment was successfully received.
        PAYMENT_FAILED: A payment attempt failed.
        WORKFLOW_STARTED: A workflow instance was started.
        WORKFLOW_COMPLETED: A workflow instance completed.
        NOTIFICATION_SENT: A notification was dispatched.
        AI_GENERATED: An AI-derived artifact/output was generated.
        LOGIN: A user successfully authenticated.
        LOGOUT: A user ended their session.
        EXPORTED: Data was exported out of the system.
        IMPORTED: Data was imported into the system.
    """

    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"
    RESTORED = "restored"
    ARCHIVED = "archived"
    STATUS_CHANGED = "status_changed"
    ASSIGNED = "assigned"
    UNASSIGNED = "unassigned"
    APPROVED = "approved"
    REJECTED = "rejected"
    UPLOADED = "uploaded"
    DOWNLOADED = "downloaded"
    VIEWED = "viewed"
    COMMENTED = "commented"
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    PAYMENT_RECEIVED = "payment_received"
    PAYMENT_FAILED = "payment_failed"
    WORKFLOW_STARTED = "workflow_started"
    WORKFLOW_COMPLETED = "workflow_completed"
    NOTIFICATION_SENT = "notification_sent"
    AI_GENERATED = "ai_generated"
    LOGIN = "login"
    LOGOUT = "logout"
    EXPORTED = "exported"
    IMPORTED = "imported"


class ActivityModule(str, enum.Enum):
    """Enumerates the owning domain module that raised the activity.

    Attributes:
        CUSTOMER: Activity originated from the Customer module.
        LEAD: Activity originated from the Lead module.
        PROPERTY: Activity originated from the Property module.
        BOOKING: Activity originated from the Booking module.
        PAYMENT: Activity originated from the Payment module.
        WORKFLOW: Activity originated from the Workflow module.
        NOTIFICATION: Activity originated from the Notification module.
        AI: Activity originated from the AI module.
        AUDIT: Activity originated from the Audit module.
        DOCUMENT: Activity originated from the Document module.
        SETTINGS: Activity originated from the Settings module.
    """

    CUSTOMER = "customer"
    LEAD = "lead"
    PROPERTY = "property"
    BOOKING = "booking"
    PAYMENT = "payment"
    WORKFLOW = "workflow"
    NOTIFICATION = "notification"
    AI = "ai"
    AUDIT = "audit"
    DOCUMENT = "document"
    SETTINGS = "settings"


class ActivityPriority(str, enum.Enum):
    """Enumerates the priority classification of a timeline entry.

    Attributes:
        LOW: Routine, informational activity.
        NORMAL: Standard activity requiring no special attention.
        HIGH: Noteworthy activity that may warrant prompt review.
        URGENT: Time-sensitive activity requiring immediate attention.
    """

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class ActivityStatus(str, enum.Enum):
    """Enumerates the lifecycle status of a timeline entry.

    Attributes:
        PENDING: The activity has been recorded but not yet processed.
        ACTIVE: The activity is currently relevant/actionable.
        COMPLETED: The activity has been resolved/actioned.
        ARCHIVED: The activity has been archived out of the active feed.
        FAILED: The underlying action failed.
        CANCELLED: The underlying action was cancelled.
    """

    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------
class Activity(Base):
    """Represents a single entry in the cross-module Activity Timeline.

    An ``Activity`` row captures a human-readable, chronologically
    orderable event raised by any domain module in the platform. Rows
    are intended to be effectively immutable once created (the
    ``updated_at`` column and soft-delete columns are retained for
    architectural consistency with other models, moderation, and rare
    explicitly authorized corrections such as archiving).

    Attributes:
        id: Surrogate primary key (UUID v4).
        module: Owning domain module that raised the activity, see
            :class:`ActivityModule`.
        entity_type: Name of the entity/table the activity concerns
            (e.g. ``"Customer"``, ``"Booking"``).
        entity_id: Primary key of the affected entity, stored as text
            to remain agnostic to the referenced entity's key type
            (UUID or Integer) across the project.
        action: The action performed, see :class:`ActivityType`.
        title: Short, human-readable headline for the timeline entry.
        description: Optional longer free-text summary of the event.
        old_value: JSONB snapshot of the relevant state prior to the
            action, if applicable.
        new_value: JSONB snapshot of the relevant state after the
            action, if applicable.
        meta_data: Arbitrary JSONB payload for module-specific context
            (stored under the ``metadata`` column).
        priority: Priority classification, see :class:`ActivityPriority`.
        status: Current lifecycle status, see :class:`ActivityStatus`.
        performed_by_id: FK to the user who performed the action.
            Nullable to support system-initiated activities.
        assigned_to_id: FK to the user the activity is assigned to,
            if any (e.g. a follow-up task raised by the activity).
        ip_address: Origin IP address of the request that triggered
            the activity, if applicable.
        user_agent: User agent string of the client that triggered the
            activity, if applicable.
        source: Free-form origin of the activity (e.g. ``"web"``,
            ``"mobile"``, ``"api"``, ``"system"``, ``"webhook"``).
        is_deleted: Soft-delete flag.
        deleted_at: Timestamp of soft deletion, if any.
        created_at: Record creation timestamp.
        updated_at: Record last-update timestamp.
        performed_by: Relationship to the acting ``User`` (one-directional).
        assigned_to: Relationship to the assigned ``User`` (one-directional).
    """

    __tablename__ = "activities"
    __table_args__ = (
        Index("ix_activities_module", "module"),
        Index("ix_activities_entity_type", "entity_type"),
        Index("ix_activities_entity_id", "entity_id"),
        Index("ix_activities_action", "action"),
        Index("ix_activities_priority", "priority"),
        Index("ix_activities_status", "status"),
        Index("ix_activities_performed_by_id", "performed_by_id"),
        Index("ix_activities_assigned_to_id", "assigned_to_id"),
        Index("ix_activities_source", "source"),
        Index("ix_activities_created_at", "created_at"),
        # Composite indexes for common query patterns.
        Index("ix_activities_entity_type_entity_id", "entity_type", "entity_id"),
        Index("ix_activities_module_action", "module", "action"),
        Index("ix_activities_module_created_at", "module", "created_at"),
        Index(
            "ix_activities_performed_by_id_created_at",
            "performed_by_id",
            "created_at",
        ),
        Index(
            "ix_activities_assigned_to_id_status", "assigned_to_id", "status"
        ),
        # Data integrity check constraints.
        CheckConstraint(
            "btrim(title) <> ''", name="ck_activities_title_not_empty"
        ),
        CheckConstraint(
            "btrim(entity_type) <> ''", name="ck_activities_entity_type_not_empty"
        ),
        CheckConstraint(
            "btrim(entity_id) <> ''", name="ck_activities_entity_id_not_empty"
        ),
        CheckConstraint(
            "(is_deleted IS FALSE AND deleted_at IS NULL) "
            "OR (is_deleted IS TRUE AND deleted_at IS NOT NULL)",
            name="ck_activities_soft_delete_consistency",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )

    # NOTE: values_callable is required on every enum column below. Without
    # it, SQLAlchemy sends the Python enum MEMBER NAME (e.g. "CUSTOMER",
    # "NORMAL") to Postgres instead of the enum's .value (e.g. "customer",
    # "normal"). The real activity_*_enum Postgres types (created by the
    # Alembic migration) only contain the lowercase .value labels, so
    # without this fix every single INSERT into `activities` failed with
    # `invalid input value for enum activity_module_enum: "CUSTOMER"` (and
    # equivalent errors for action/priority/status) -- the Activity
    # Timeline module could not write a single row in production.
    module: Mapped[ActivityModule] = mapped_column(
        SAEnum(
            ActivityModule,
            name="activity_module_enum",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )

    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)

    action: Mapped[ActivityType] = mapped_column(
        SAEnum(
            ActivityType,
            name="activity_type_enum",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    old_value: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    meta_data: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONB, nullable=True
    )

    priority: Mapped[ActivityPriority] = mapped_column(
        SAEnum(
            ActivityPriority,
            name="activity_priority_enum",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=ActivityPriority.NORMAL,
        server_default=ActivityPriority.NORMAL.value,
    )

    status: Mapped[ActivityStatus] = mapped_column(
        SAEnum(
            ActivityStatus,
            name="activity_status_enum",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=ActivityStatus.ACTIVE,
        server_default=ActivityStatus.ACTIVE.value,
    )

    performed_by_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    assigned_to_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    ip_address: Mapped[Optional[str]] = mapped_column(INET, nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, default="system", server_default="system"
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

    performed_by: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[performed_by_id],
        lazy="selectin",
        viewonly=True,
    )
    assigned_to: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[assigned_to_id],
        lazy="selectin",
        viewonly=True,
    )

    def __repr__(self) -> str:
        """Returns an unambiguous, debug-friendly representation of the entry.

        Returns:
            str: A concise representation including id, module, entity, and action.
        """
        return (
            f"<Activity id={self.id} module={self.module!r} "
            f"entity_type={self.entity_type!r} entity_id={self.entity_id!r} "
            f"action={self.action!r} status={self.status!r}>"
        )