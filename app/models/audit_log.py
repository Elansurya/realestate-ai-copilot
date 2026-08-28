"""Audit Log SQLAlchemy models.

This module defines the ``AuditLog`` ORM model used to persist a tamper
evident, append-only trail of system and user activity across every
domain module in the platform (Customer, Lead, Property, Booking,
Payment, Dashboard, Report, AI, Notification, etc.).

The model is intentionally decoupled from any specific domain module.
Any module can write an audit entry by referencing its own name in
``module`` and the affected record in ``entity_type`` / ``entity_id``.
"""

import enum
import uuid
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.notification import TimestampMixin

__all__ = [
    "AuditModule",
    "AuditAction",
    "AuditSeverity",
    "AuditStatus",
    "AuditLog",
]


class AuditModule(str, enum.Enum):
    """Enumerates the domain modules that may originate an audit entry.

    Values mirror the owning domain modules referenced throughout the
    platform (see the module-level docstring above). This enum exists
    purely as an application-level convenience/validation aid for the
    free-form ``AuditLog.module`` column; it is intentionally not bound
    to a Postgres native enum type so that new modules can be added
    without a schema migration.

    Attributes:
        CUSTOMER: Customer domain module.
        LEAD: Lead domain module.
        PROPERTY: Property domain module.
        BOOKING: Booking domain module.
        PAYMENT: Payment domain module.
        DASHBOARD: Dashboard domain module.
        REPORT: Report domain module.
        AI: AI/assistant domain module.
        NOTIFICATION: Notification domain module.
        USER: User/authentication domain module (e.g. LOGIN/LOGOUT events).
    """

    CUSTOMER = "CUSTOMER"
    LEAD = "LEAD"
    PROPERTY = "PROPERTY"
    BOOKING = "BOOKING"
    PAYMENT = "PAYMENT"
    DASHBOARD = "DASHBOARD"
    REPORT = "REPORT"
    AI = "AI"
    NOTIFICATION = "NOTIFICATION"
    USER = "USER"


class AuditAction(str, enum.Enum):
    """Enumerates the discrete actions that can be recorded in the audit trail.

    Attributes:
        CREATE: A new entity was created.
        UPDATE: An existing entity was modified.
        DELETE: An entity was deleted (soft or hard).
        LOGIN: A user successfully authenticated.
        LOGOUT: A user ended their session.
        EXPORT: Data was exported out of the system.
        IMPORT: Data was imported into the system.
        APPROVE: An entity or workflow step was approved.
        REJECT: An entity or workflow step was rejected.
        ASSIGN: An entity was assigned to a user/team.
        UNASSIGN: An entity was unassigned from a user/team.
        UPLOAD: A file or document was uploaded.
        DOWNLOAD: A file or document was downloaded.
        SEND: A message, notification, or communication was sent.
        GENERATE: A derived artifact (report, AI output, etc.) was generated.
    """

    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    LOGIN = "LOGIN"
    LOGOUT = "LOGOUT"
    EXPORT = "EXPORT"
    IMPORT = "IMPORT"
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    ASSIGN = "ASSIGN"
    UNASSIGN = "UNASSIGN"
    UPLOAD = "UPLOAD"
    DOWNLOAD = "DOWNLOAD"
    SEND = "SEND"
    GENERATE = "GENERATE"


class AuditSeverity(str, enum.Enum):
    """Enumerates the severity classification of an audit event.

    Attributes:
        LOW: Routine, informational event with no risk implications.
        MEDIUM: Noteworthy event that may warrant periodic review.
        HIGH: Sensitive event that should be reviewed promptly.
        CRITICAL: Severe event requiring immediate attention (e.g. security).
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AuditStatus(str, enum.Enum):
    """Enumerates the outcome status of the audited operation.

    Attributes:
        SUCCESS: The operation completed successfully.
        FAILED: The operation failed or was rejected.
    """

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class AuditLog(TimestampMixin, Base):
    """Represents a single immutable audit trail entry.

    An ``AuditLog`` row captures who did what, to which entity, in which
    module, when, from where, and with what before/after state. Rows are
    intended to be write-once and should never be mutated after creation
    (the ``updated_at`` column is retained for architectural consistency
    with other models and for rare, explicitly authorized corrections).

    Attributes:
        id: Surrogate primary key (UUID v4).
        user_id: Foreign key to the acting ``User``. Nullable to support
            system-initiated or anonymous/unauthenticated events (e.g. a
            failed login attempt with an unknown user).
        module: Name of the owning domain module (e.g. ``"customer"``,
            ``"lead"``, ``"property"``, ``"booking"``, ``"payment"``,
            ``"dashboard"``, ``"report"``, ``"ai"``, ``"notification"``).
            Stored as free-form text; values should conform to
            :class:`AuditModule` where possible.
        entity_type: Name of the entity/table affected (e.g. ``"Customer"``).
        entity_id: Primary key of the affected entity, stored as text to
            remain agnostic to the affected entity's key type.
        action: The action performed, see :class:`AuditAction`.
        description: Human-readable summary of the event.
        old_data: JSONB snapshot of the entity state prior to the action.
        new_data: JSONB snapshot of the entity state after the action.
        ip_address: Origin IP address of the request that triggered the event.
        user_agent: User agent string of the client that triggered the event.
        request_id: Correlation/trace identifier linking related log lines
            across services (e.g. propagated from an ``X-Request-ID`` header).
        status: Outcome of the operation, see :class:`AuditStatus`.
        severity: Severity classification, see :class:`AuditSeverity`.
        created_at: Timestamp the audit entry was created (inherited).
        updated_at: Timestamp the audit entry was last updated (inherited).
        user: Relationship to the acting ``User``.
    """

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        # Must be a SQL expression (`text(...)`), not a plain Python string --
        # a plain string here is bound as a literal default value, and
        # Postgres then tries to parse the literal text "gen_random_uuid()"
        # as a UUID rather than invoking the function, raising
        # `invalid input syntax for type uuid`.
        server_default=text("gen_random_uuid()"),
        nullable=False,
    )

    # NOTE: users.id is INTEGER in this database, not UUID (confirmed against
    # the live schema and the original audit_log migration, which already
    # carried this exact warning). This column was previously typed as UUID,
    # which caused every audit log write referencing a real user to fail
    # with psycopg.errors.DatatypeMismatchError. Fixed to Integer to match
    # users.id and the actual Postgres column type.
    user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    module: Mapped[str] = mapped_column(String(100), nullable=False)

    entity_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    entity_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    action: Mapped[AuditAction] = mapped_column(
        # NOTE: create_type intentionally left at its default (True), unlike
        # this repo's Alembic migration for this column (which sets
        # create_type=False because it creates the `audit_action_enum` type
        # itself via an explicit `.create(bind, checkfirst=True)` call
        # before the table DDL runs). That explicit-create step only exists
        # in the migration, not in this ORM model, so mirroring
        # create_type=False here left nothing to ever create the type when
        # the schema is built directly from this metadata (e.g.
        # `Base.metadata.create_all()`, as several repository-layer test
        # suites do) -- Alembic-driven schema creation is unaffected either
        # way, since it derives its own DDL from the migration file, not
        # from this model.
        PGEnum(
            AuditAction,
            name="audit_action_enum",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )

    description: Mapped[str] = mapped_column(Text, nullable=False)

    old_data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    new_data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    ip_address: Mapped[Optional[str]] = mapped_column(INET, nullable=True)

    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    request_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    status: Mapped[AuditStatus] = mapped_column(
        # create_type left at default (True) -- see note on `action` above;
        # this repo's Alembic migration creates the type separately via an
        # explicit `.create(bind, checkfirst=True)` call, which this model
        # has no equivalent of, so create_type=False here left nothing to
        # create the type for schema built directly via this metadata.
        PGEnum(
            AuditStatus,
            name="audit_status_enum",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=AuditStatus.SUCCESS,
        server_default=AuditStatus.SUCCESS.value,
    )

    severity: Mapped[AuditSeverity] = mapped_column(
        # create_type left at default (True) -- see note on `action` above.
        PGEnum(
            AuditSeverity,
            name="audit_severity_enum",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=AuditSeverity.LOW,
        server_default=AuditSeverity.LOW.value,
    )

    user: Mapped[Optional["User"]] = relationship(
        "User",
        back_populates="audit_logs",
        lazy="selectin",
        foreign_keys=[user_id],
    )

    __table_args__ = (
        # Single-column indexes.
        Index("ix_audit_logs_user_id", "user_id"),
        Index("ix_audit_logs_module", "module"),
        Index("ix_audit_logs_entity_type", "entity_type"),
        Index("ix_audit_logs_entity_id", "entity_id"),
        Index("ix_audit_logs_action", "action"),
        Index("ix_audit_logs_severity", "severity"),
        Index("ix_audit_logs_status", "status"),
        Index("ix_audit_logs_created_at", "created_at"),
        # Composite indexes for common query patterns.
        Index("ix_audit_logs_module_action", "module", "action"),
        Index("ix_audit_logs_entity_type_entity_id", "entity_type", "entity_id"),
        Index("ix_audit_logs_user_id_created_at", "user_id", "created_at"),
        # Data integrity check constraints.
        CheckConstraint("btrim(module) <> ''", name="ck_audit_logs_module_not_empty"),
        CheckConstraint(
            "action::text <> ''", name="ck_audit_logs_action_not_empty"
        ),
        CheckConstraint(
            "btrim(description) <> ''", name="ck_audit_logs_description_not_empty"
        ),
    )

    def __repr__(self) -> str:
        """Returns an unambiguous, debug-friendly representation of the entry.

        Returns:
            str: A concise representation including id, module, entity, and action.
        """
        return (
            f"<AuditLog id={self.id} module={self.module!r} "
            f"entity_type={self.entity_type!r} entity_id={self.entity_id!r} "
            f"action={self.action!r} status={self.status!r}>"
        )