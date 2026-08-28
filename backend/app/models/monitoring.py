"""
backend/app/models/monitoring.py

SQLAlchemy 2.x (async) ORM model for the Enterprise Monitoring & Health
module of the Enterprise Real Estate AI Copilot CRM.

A ``SystemHealth`` row is a point-in-time (continuously upserted-in-place)
health snapshot for a single monitored component of the platform --
Application, Database, Storage, AI Providers, External Integrations,
Notification Services, Document Storage, Workflow Engine, and Search
Engine (see :class:`ComponentType`). Each row tracks resource utilization
(CPU/memory/disk), latency, error/warning counters, the last time the
component was checked, and the last known success/failure timestamps,
so operational dashboards and alerting can be built directly on top of
this table without any additional aggregation service.

Conventions (mirrors `app/models/document.py` / `app/models/task.py` /
`app/models/integration.py`):
    - `Base` comes from `app.db.base`; timestamps are inline,
      timezone-aware UTC columns (no mixins -- `app.models.mixins` does
      not exist in this project).
    - `id` is a server-generated PostgreSQL UUID via
      `func.gen_random_uuid()` (requires the `pgcrypto` extension,
      already enabled by earlier migrations in this project).
    - `component_type` and `status` use native PostgreSQL ENUM types
      (mirroring `Document.category` / `Document.file_type`) for strong
      data integrity at the database level.
    - `parent_component_id` is a self-referential, nullable FK to
      `system_health.id`, allowing a component to be grouped under a
      logical parent (e.g. a specific AI provider instance rolling up
      into an "AI Providers" aggregate row), mirroring the
      self-referential versioning relationship on
      `Document.parent_document_id`.
    - `created_by_id` / `updated_by_id` / `deleted_by_id` are `Integer`
      FKs to `users.id`, matching `User.id`'s actual type (see
      `app/models/user.py`). They are nullable because most
      `SystemHealth` rows are written by the scheduler/health-check
      worker rather than interactively by a user.
    - `is_active` is a plain boolean soft-disable flag, matching
      `Document.is_active` / `Customer.is_active`.
    - `is_deleted` / `deleted_at` / `deleted_by_id` implement soft
      delete as a *distinct* concept from `is_active`, exactly as
      `Document` already does.
    - Single-column indexes are declared inline (`index=True`); only
      composite indexes, the uniqueness constraint, and CHECK
      constraints live in `__table_args__`, exactly as `Document`
      already does.

NOTE (scope of this phase):
    This module intentionally contains ONLY the ORM model (and its
    supporting enums). No repository, service, router, tests, or
    documentation are declared here -- those belong to a later phase.
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
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User

__all__ = [
    "HealthStatus",
    "ComponentType",
    "MetricType",
    "SystemHealth",
]


# ---------------------------------------------------------------------------
# Health Status Enumeration
# ---------------------------------------------------------------------------
class HealthStatus(str, enum.Enum):
    """Defines the discrete operational health state of a monitored component.

    Attributes:
        HEALTHY: The component is fully operational within normal
            thresholds.
        DEGRADED: The component is operational but exhibiting elevated
            latency, errors, or resource usage.
        UNHEALTHY: The component is failing health checks or breaching
            critical thresholds but is not fully down.
        DOWN: The component is unreachable or has failed outright.
        MAINTENANCE: The component is intentionally offline for planned
            maintenance and should be excluded from alerting.
        UNKNOWN: No health check result has been recorded yet, or the
            last check could not determine a definitive status.
    """

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    DOWN = "DOWN"
    MAINTENANCE = "MAINTENANCE"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Component Type Enumeration
# ---------------------------------------------------------------------------
class ComponentType(str, enum.Enum):
    """Defines the category of system component being monitored.

    Attributes:
        APPLICATION: The core backend application/API process.
        DATABASE: The primary relational database (PostgreSQL).
        STORAGE: General-purpose object/file storage (e.g. AWS S3, Azure
            Blob, GCP Storage) used by the platform.
        AI_PROVIDER: An external AI/LLM provider (e.g. OpenAI, Anthropic).
        EXTERNAL_INTEGRATION: Any other configured third-party
            integration (payment gateway, calendar, CRM webhook, etc.).
        NOTIFICATION_SERVICE: Outbound notification channels (email,
            SMS, WhatsApp, push).
        DOCUMENT_STORAGE: The document management subsystem's storage
            backend (distinct from general-purpose `STORAGE`).
        WORKFLOW_ENGINE: The workflow/automation execution subsystem.
        SEARCH_ENGINE: The search/indexing subsystem.
    """

    APPLICATION = "APPLICATION"
    DATABASE = "DATABASE"
    STORAGE = "STORAGE"
    AI_PROVIDER = "AI_PROVIDER"
    EXTERNAL_INTEGRATION = "EXTERNAL_INTEGRATION"
    NOTIFICATION_SERVICE = "NOTIFICATION_SERVICE"
    DOCUMENT_STORAGE = "DOCUMENT_STORAGE"
    WORKFLOW_ENGINE = "WORKFLOW_ENGINE"
    SEARCH_ENGINE = "SEARCH_ENGINE"


# ---------------------------------------------------------------------------
# Metric Type Enumeration
# ---------------------------------------------------------------------------
class MetricType(str, enum.Enum):
    """Defines the kind of quantitative metric captured for a component.

    This enum is primarily used at the API/schema layer (e.g. to label
    a single metric sample) rather than as a persisted column on
    `SystemHealth`, whose metric columns are already discretely typed.

    Attributes:
        CPU_USAGE: Percentage of CPU utilization.
        MEMORY_USAGE: Percentage of memory utilization.
        DISK_USAGE: Percentage of disk utilization.
        RESPONSE_TIME: Latency of the component's health check, in
            milliseconds.
        ERROR_COUNT: Count of errors observed since the last reset.
        WARNING_COUNT: Count of warnings observed since the last reset.
        UPTIME: Percentage of time the component has been available.
        THROUGHPUT: Volume of requests/operations processed per unit time.
        AVAILABILITY: Point-in-time availability indicator.
    """

    CPU_USAGE = "CPU_USAGE"
    MEMORY_USAGE = "MEMORY_USAGE"
    DISK_USAGE = "DISK_USAGE"
    RESPONSE_TIME = "RESPONSE_TIME"
    ERROR_COUNT = "ERROR_COUNT"
    WARNING_COUNT = "WARNING_COUNT"
    UPTIME = "UPTIME"
    THROUGHPUT = "THROUGHPUT"
    AVAILABILITY = "AVAILABILITY"


# ---------------------------------------------------------------------------
# SystemHealth Model
# ---------------------------------------------------------------------------
class SystemHealth(Base):
    """Represents the latest health snapshot of a single monitored component.

    Table: system_health
    """

    __tablename__ = "system_health"

    __table_args__ = (
        # Prevents duplicate live rows for the same named component of the
        # same type (e.g. two "primary-postgres" / DATABASE rows).
        UniqueConstraint(
            "component_name",
            "component_type",
            name="uq_system_health_component_name_component_type",
        ),
        # Composite indexes -- cannot be expressed as single inline
        # index=True columns.
        Index("ix_system_health_type_status", "component_type", "status"),
        Index(
            "ix_system_health_active_deleted", "is_active", "is_deleted"
        ),
        Index(
            "ix_system_health_status_last_check",
            "status",
            "last_health_check_at",
        ),
        CheckConstraint(
            "length(trim(component_name)) > 0",
            name="ck_system_health_component_name_not_blank",
        ),
        CheckConstraint(
            "cpu_usage_percent IS NULL OR "
            "(cpu_usage_percent >= 0 AND cpu_usage_percent <= 100)",
            name="ck_system_health_cpu_usage_range",
        ),
        CheckConstraint(
            "memory_usage_percent IS NULL OR "
            "(memory_usage_percent >= 0 AND memory_usage_percent <= 100)",
            name="ck_system_health_memory_usage_range",
        ),
        CheckConstraint(
            "disk_usage_percent IS NULL OR "
            "(disk_usage_percent >= 0 AND disk_usage_percent <= 100)",
            name="ck_system_health_disk_usage_range",
        ),
        CheckConstraint(
            "response_time_ms IS NULL OR response_time_ms >= 0",
            name="ck_system_health_response_time_non_negative",
        ),
        CheckConstraint(
            "error_count >= 0", name="ck_system_health_error_count_non_negative"
        ),
        CheckConstraint(
            "warning_count >= 0",
            name="ck_system_health_warning_count_non_negative",
        ),
        CheckConstraint(
            "(is_deleted = false AND deleted_at IS NULL AND deleted_by_id IS NULL) "
            "OR (is_deleted = true AND deleted_at IS NOT NULL)",
            name="ck_system_health_soft_delete_consistency",
        ),
        CheckConstraint(
            "parent_component_id IS NULL OR parent_component_id != id",
            name="ck_system_health_parent_not_self",
        ),
    )

    # ------------------------------------------------------------------
    # Primary Key
    # ------------------------------------------------------------------
    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        doc=(
            "Globally unique primary key for the health snapshot record. "
            "Requires the PostgreSQL `pgcrypto` extension to be enabled "
            "for `gen_random_uuid()` to be available."
        ),
    )

    # ------------------------------------------------------------------
    # Hierarchical Grouping (Self-Referential)
    # ------------------------------------------------------------------
    parent_component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("system_health.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc=(
            "Optional reference to a parent SystemHealth row this "
            "component rolls up into (e.g. a specific AI provider "
            "instance grouped under an 'AI Providers' aggregate row)."
        ),
    )

    # ------------------------------------------------------------------
    # Component Identity
    # ------------------------------------------------------------------
    component_name: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
        index=True,
        doc="Human-readable, unique-per-type name of the monitored component.",
    )

    component_type: Mapped[ComponentType] = mapped_column(
        SAEnum(
            ComponentType,
            name="component_type_enum",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
        index=True,
        doc="Category of system component being monitored.",
    )

    # ------------------------------------------------------------------
    # Health State
    # ------------------------------------------------------------------
    status: Mapped[HealthStatus] = mapped_column(
        SAEnum(
            HealthStatus,
            name="health_status_enum",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
        default=HealthStatus.UNKNOWN,
        server_default=HealthStatus.UNKNOWN.value,
        index=True,
        doc="Current operational health state of the component.",
    )

    # ------------------------------------------------------------------
    # Resource Utilization Metrics
    # ------------------------------------------------------------------
    cpu_usage_percent: Mapped[Optional[float]] = mapped_column(
        Numeric(5, 2),
        nullable=True,
        doc="CPU utilization of the component, as a percentage (0-100).",
    )

    memory_usage_percent: Mapped[Optional[float]] = mapped_column(
        Numeric(5, 2),
        nullable=True,
        doc="Memory utilization of the component, as a percentage (0-100).",
    )

    disk_usage_percent: Mapped[Optional[float]] = mapped_column(
        Numeric(5, 2),
        nullable=True,
        doc="Disk utilization of the component, as a percentage (0-100).",
    )

    response_time_ms: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 3),
        nullable=True,
        index=True,
        doc="Latency of the most recent health check, in milliseconds.",
    )

    # ------------------------------------------------------------------
    # Error / Warning Counters
    # ------------------------------------------------------------------
    error_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        doc="Count of errors observed since the last counter reset.",
    )

    warning_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        doc="Count of warnings observed since the last counter reset.",
    )

    # ------------------------------------------------------------------
    # Check / Success / Failure Timestamps
    # ------------------------------------------------------------------
    last_health_check_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
        doc="UTC timestamp of the most recent health check attempt.",
    )

    last_success_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="UTC timestamp of the most recent successful health check.",
    )

    last_failure_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="UTC timestamp of the most recent failed health check.",
    )

    # ------------------------------------------------------------------
    # Diagnostic Metadata
    # ------------------------------------------------------------------
    status_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Free-form human-readable detail about the current status (e.g. last error message).",
    )

    meta_data: Mapped[Optional[dict]] = mapped_column(
        "metadata",
        JSONB,
        nullable=True,
        doc=(
            "Arbitrary JSONB payload for provider-specific diagnostics "
            "(e.g. raw probe response, connection pool stats, queue depth)."
        ),
    )

    # ------------------------------------------------------------------
    # Status Flags
    # ------------------------------------------------------------------
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        doc="Soft-disable flag; inactive components are excluded from active monitoring views.",
    )

    # ------------------------------------------------------------------
    # Soft Delete
    # ------------------------------------------------------------------
    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        index=True,
        doc="Soft delete flag; deleted health records are excluded everywhere.",
    )

    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="UTC timestamp when the health record was soft-deleted, if deleted.",
    )

    deleted_by_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="User who soft-deleted this health record, if deleted.",
    )

    # ------------------------------------------------------------------
    # Audit Fields
    # ------------------------------------------------------------------
    created_by_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc=(
            "User who created this health record, if created interactively. "
            "Nullable because most rows are written by the automated "
            "health-check scheduler/worker."
        ),
    )

    updated_by_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="User who last modified this health record, if modified interactively.",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="UTC timestamp when the health record was created.",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        doc="UTC timestamp when the health record was last updated.",
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    parent_component: Mapped[Optional["SystemHealth"]] = relationship(
        "SystemHealth",
        remote_side=[id],
        foreign_keys=[parent_component_id],
        back_populates="child_components",
        lazy="selectin",
        doc="The parent SystemHealth row this component rolls up into, if any.",
    )

    child_components: Mapped[list["SystemHealth"]] = relationship(
        "SystemHealth",
        back_populates="parent_component",
        foreign_keys=[parent_component_id],
        doc="Child SystemHealth rows that roll up into this component, if any.",
    )

    deleted_by: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[deleted_by_id],
        lazy="selectin",
        doc="The User who soft-deleted this health record, if deleted.",
    )

    created_by: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[created_by_id],
        lazy="selectin",
        doc="The User who created this health record, if created interactively.",
    )

    updated_by: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[updated_by_id],
        lazy="selectin",
        doc="The User who last modified this health record, if modified interactively.",
    )

    # ------------------------------------------------------------------
    # Developer Ergonomics
    # ------------------------------------------------------------------
    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return (
            f"<SystemHealth id={self.id} component_name={self.component_name!r} "
            f"component_type={self.component_type.value} status={self.status.value}>"
        )