"""
backend/app/models/webhook.py

SQLAlchemy 2.x (async) ORM models for the Enterprise Webhook module of
the Enterprise Real Estate AI Copilot CRM.

This module supports outbound webhook registration and delivery:
    - ``Webhook``    -- a registered outbound webhook subscription: which
      event it fires on, where to deliver it, how to authenticate/sign
      the request, and its operational policy (timeout, retry count,
      rate limiting, custom headers, payload template).
    - ``WebhookLog``  -- an immutable, append-only delivery attempt
      record for a given ``Webhook``: HTTP response, timing, attempt
      number, and error detail. This is the audit trail backing
      delivery statistics, retry/backoff decisions, and Dead Letter
      Queue (DLQ) triage.

Conventions (mirrors `app/models/integration.py` / `app/models/task.py` /
`app/models/monitoring.py`):
    - `Base` comes from `app.db.base`.
    - Primary keys are server-generated PostgreSQL UUIDs via
      `func.gen_random_uuid()` (requires the `pgcrypto` extension,
      already enabled by earlier migrations in this project).
    - Enums are native PostgreSQL ENUM types (via SQLAlchemy's `Enum`)
      for strong data-integrity at the database level.
    - `created_by_id` is an `Integer` FK to `users.id`, matching
      `User.id`'s actual type, and is nullable to support
      system-provisioned/seeded webhooks not created interactively by
      a user.
    - Timestamps are timezone-aware (UTC).
    - `User` is imported only under `TYPE_CHECKING` to avoid a runtime
      circular-import surface; the relationship to `User` is
      intentionally one-directional (no `back_populates`), consistent
      with `Integration.created_by`.
    - `secret_key` stores only the raw HMAC signing secret value as
      configured; encryption-at-rest (if required) is an
      application/service-layer concern, not this model's, matching
      the same division of responsibility documented on
      `Integration.credentials`.
    - Soft delete (`is_deleted` / `deleted_at`) is implemented on
      `Webhook` only. `WebhookLog` rows are treated as an immutable
      audit trail and are not soft-deletable.
    - `WebhookLog.webhook_id` is a FK to `webhooks.id` with
      `ondelete="CASCADE"`, since delivery logs have no meaning once
      their parent webhook registration is permanently removed.

NOTE (scope of this phase):
    This module intentionally contains ONLY the ORM models (and their
    supporting enums). No repository, service, router, utils, tests,
    or documentation are declared here -- those belong to a later
    phase, per this phase's explicit scope.
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
    "WebhookStatus",
    "WebhookEvent",
    "DeliveryStatus",
    "AuthenticationType",
    "Webhook",
    "WebhookLog",
]


# ---------------------------------------------------------------------------
# Webhook Status Enumeration
# ---------------------------------------------------------------------------
class WebhookStatus(str, enum.Enum):
    """Defines the operational lifecycle status of a registered webhook.

    Attributes:
        ACTIVE: The webhook is enabled and eligible for delivery.
        INACTIVE: The webhook is configured but intentionally disabled
            by the owner (distinct from `enabled` -- see `Webhook.enabled`
            for the quick on/off toggle; `status` captures the broader
            lifecycle state).
        SUSPENDED: The webhook has been administratively suspended
            (e.g. after repeated delivery failures) and is excluded
            from delivery attempts until reactivated.
        FAILED: The webhook's most recent delivery attempt(s) failed
            and it has exceeded its retry policy, routing further
            events to the Dead Letter Queue.
    """

    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Webhook Event Enumeration
# ---------------------------------------------------------------------------
class WebhookEvent(str, enum.Enum):
    """Defines the domain event a webhook subscription fires on.

    Attributes:
        LEAD_CREATED: A new lead was created.
        LEAD_UPDATED: An existing lead was updated.
        LEAD_CONVERTED: A lead was converted to a customer/deal.
        DEAL_CREATED: A new deal/opportunity was created.
        DEAL_UPDATED: An existing deal/opportunity was updated.
        DEAL_CLOSED: A deal/opportunity was closed (won or lost).
        TASK_CREATED: A new task was created.
        TASK_COMPLETED: A task was marked complete.
        DOCUMENT_UPLOADED: A document was uploaded/attached.
        PAYMENT_RECEIVED: A payment was successfully received.
        BOOKING_CREATED: A new booking/appointment was created.
        BOOKING_CANCELLED: A booking/appointment was cancelled.
        USER_CREATED: A new user account was created.
        CUSTOM: A custom/application-defined event not covered by a
            more specific value.
    """

    LEAD_CREATED = "lead_created"
    LEAD_UPDATED = "lead_updated"
    LEAD_CONVERTED = "lead_converted"
    DEAL_CREATED = "deal_created"
    DEAL_UPDATED = "deal_updated"
    DEAL_CLOSED = "deal_closed"
    TASK_CREATED = "task_created"
    TASK_COMPLETED = "task_completed"
    DOCUMENT_UPLOADED = "document_uploaded"
    PAYMENT_RECEIVED = "payment_received"
    BOOKING_CREATED = "booking_created"
    BOOKING_CANCELLED = "booking_cancelled"
    USER_CREATED = "user_created"
    CUSTOM = "custom"


# ---------------------------------------------------------------------------
# Delivery Status Enumeration
# ---------------------------------------------------------------------------
class DeliveryStatus(str, enum.Enum):
    """Defines the outcome of a single webhook delivery attempt.

    Attributes:
        PENDING: The delivery attempt has been queued but not yet sent.
        SUCCESS: The delivery attempt received a successful (2xx)
            response.
        FAILED: The delivery attempt failed (non-2xx response,
            connection error, or timeout) and may be eligible for
            retry per the webhook's retry policy.
        RETRYING: The delivery attempt failed and a retry has been
            scheduled/is in progress.
        DEAD_LETTERED: The delivery permanently failed after
            exhausting the configured retry policy and has been routed
            to the Dead Letter Queue for manual triage.
    """

    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    RETRYING = "retrying"
    DEAD_LETTERED = "dead_lettered"


# ---------------------------------------------------------------------------
# Authentication Type Enumeration
# ---------------------------------------------------------------------------
class AuthenticationType(str, enum.Enum):
    """Defines the authentication mechanism used to sign/authenticate
    an outbound webhook delivery request.

    Attributes:
        NONE: No authentication; the request is sent unauthenticated.
        HMAC_SIGNATURE: The request body is signed with `secret_key`
            using HMAC and the signature is sent in a custom header
            (e.g. `X-Webhook-Signature`).
        BEARER_TOKEN: A static bearer token (`secret_key`) sent via the
            `Authorization` header.
        API_KEY: A static API key (`secret_key`) sent via a custom
            header.
        BASIC_AUTH: HTTP Basic authentication using `secret_key` as the
            credential payload.
    """

    NONE = "none"
    HMAC_SIGNATURE = "hmac_signature"
    BEARER_TOKEN = "bearer_token"
    API_KEY = "api_key"
    BASIC_AUTH = "basic_auth"


# ---------------------------------------------------------------------------
# Webhook Model
# ---------------------------------------------------------------------------
class Webhook(Base):
    """Represents a single registered outbound webhook subscription.

    Table: webhooks

    Attributes:
        id: Globally unique primary key (server-generated UUID).
        name: Human-readable, unique name for the webhook subscription.
        event: The domain event this webhook fires on, see
            :class:`WebhookEvent`.
        target_url: The destination URL events are delivered to.
        http_method: The HTTP method used for delivery (e.g. `POST`,
            `PUT`, `PATCH`).
        status: Current operational lifecycle status, see
            :class:`WebhookStatus`.
        authentication_type: The authentication/signing mechanism used
            for delivery, see :class:`AuthenticationType`.
        secret_key: The raw secret used for HMAC signing or as a
            bearer/API-key/basic-auth credential, depending on
            `authentication_type`. `NULL` when `authentication_type`
            is `NONE`.
        custom_headers: Additional static HTTP headers (JSONB object of
            string key/value pairs) sent with every delivery attempt.
        payload_template: Optional JSONB template describing how the
            outbound request body should be shaped from the source
            event payload (e.g. a field-mapping/templating spec
            interpreted by the service layer). `NULL` means the raw
            event payload is sent as-is.
        retry_count: Maximum number of delivery retries to attempt on
            failure, before the delivery is dead-lettered.
        timeout_seconds: Per-request delivery timeout, in seconds.
        rate_limit_per_minute: Maximum number of delivery attempts
            permitted for this webhook per minute, if enforced.
        enabled: Quick on/off toggle for delivery eligibility,
            independent of the broader `status` lifecycle field.
        last_delivery_at: Timestamp of the most recent delivery attempt
            (successful or not), regardless of outcome.
        last_success_at: Timestamp of the most recent successful
            delivery.
        last_failure_at: Timestamp of the most recent failed delivery.
        created_by_id: FK to the user who registered this webhook.
            Nullable to support system-provisioned/seeded webhooks.
        is_deleted: Soft-delete flag.
        deleted_at: Timestamp of soft deletion, if any.
        created_at: Record creation timestamp.
        updated_at: Record last-update timestamp.
        created_by: Relationship to the creating `User` (one-directional).
        logs: Relationship to this webhook's `WebhookLog` delivery
            history.
    """

    __tablename__ = "webhooks"

    __table_args__ = (
        # Single-column indexes for common query/filter patterns.
        # NOTE: no explicit Index("ix_webhooks_event", "event") here --
        # the `event` column below is declared with `index=True`, which
        # already creates that exact index (same name, via SQLAlchemy's
        # ix_<table>_<column> convention). Declaring it again here caused
        # a duplicate "ix_webhooks_event already exists" failure the
        # moment this table's DDL ran.
        Index("ix_webhooks_status", "status"),
        Index("ix_webhooks_authentication_type", "authentication_type"),
        Index("ix_webhooks_enabled", "enabled"),
        Index("ix_webhooks_created_by_id", "created_by_id"),
        Index("ix_webhooks_is_deleted", "is_deleted"),
        Index("ix_webhooks_created_at", "created_at"),
        Index("ix_webhooks_last_delivery_at", "last_delivery_at"),
        # Composite indexes for common dashboard/dispatch query patterns.
        Index("ix_webhooks_event_status", "event", "status"),
        Index("ix_webhooks_event_enabled", "event", "enabled"),
        Index("ix_webhooks_status_enabled", "status", "enabled"),
        # Uniqueness constraints.
        UniqueConstraint("name", name="uq_webhooks_name"),
        # Data integrity check constraints.
        CheckConstraint("btrim(name) <> ''", name="ck_webhooks_name_not_empty"),
        CheckConstraint("btrim(target_url) <> ''", name="ck_webhooks_target_url_not_empty"),
        CheckConstraint(
            "http_method IN ('GET','POST','PUT','PATCH','DELETE')",
            name="ck_webhooks_http_method_valid",
        ),
        CheckConstraint(
            "retry_count >= 0", name="ck_webhooks_retry_count_non_negative"
        ),
        CheckConstraint(
            "timeout_seconds > 0", name="ck_webhooks_timeout_seconds_positive"
        ),
        CheckConstraint(
            "rate_limit_per_minute IS NULL OR rate_limit_per_minute > 0",
            name="ck_webhooks_rate_limit_positive",
        ),
        CheckConstraint(
            "(is_deleted IS FALSE AND deleted_at IS NULL) "
            "OR (is_deleted IS TRUE AND deleted_at IS NOT NULL)",
            name="ck_webhooks_soft_delete_consistency",
        ),
        CheckConstraint(
            "(authentication_type = 'none' AND secret_key IS NULL) "
            "OR (authentication_type <> 'none' AND secret_key IS NOT NULL)",
            name="ck_webhooks_secret_key_required_when_authenticated",
        ),
    )

    # ------------------------------------------------------------------
    # Primary Key
    # ------------------------------------------------------------------
    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )

    # ------------------------------------------------------------------
    # Identity / Subscription
    # ------------------------------------------------------------------
    name: Mapped[str] = mapped_column(String(150), nullable=False)

    event: Mapped[WebhookEvent] = mapped_column(
        SAEnum(
            WebhookEvent,
            name="webhook_event_enum",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        index=True,
    )

    target_url: Mapped[str] = mapped_column(String(2048), nullable=False)

    http_method: Mapped[str] = mapped_column(
        String(10), nullable=False, default="POST", server_default="POST"
    )

    status: Mapped[WebhookStatus] = mapped_column(
        SAEnum(
            WebhookStatus,
            name="webhook_status_enum",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=WebhookStatus.ACTIVE,
        server_default=WebhookStatus.ACTIVE.value,
    )

    # ------------------------------------------------------------------
    # Authentication / Signing
    # ------------------------------------------------------------------
    authentication_type: Mapped[AuthenticationType] = mapped_column(
        SAEnum(
            AuthenticationType,
            name="webhook_authentication_type_enum",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=AuthenticationType.HMAC_SIGNATURE,
        server_default=AuthenticationType.HMAC_SIGNATURE.value,
    )

    secret_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # ------------------------------------------------------------------
    # Request Shaping
    # ------------------------------------------------------------------
    custom_headers: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    payload_template: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # ------------------------------------------------------------------
    # Delivery / Retry / Rate-Limit Policy
    # ------------------------------------------------------------------
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )

    timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )

    rate_limit_per_minute: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # ------------------------------------------------------------------
    # Delivery State
    # ------------------------------------------------------------------
    last_delivery_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_success_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failure_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ------------------------------------------------------------------
    # Audit / Soft Delete
    # ------------------------------------------------------------------
    created_by_id: Mapped[Optional[int]] = mapped_column(
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

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    created_by: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[created_by_id],
        lazy="selectin",
        viewonly=True,
    )

    logs: Mapped[list["WebhookLog"]] = relationship(
        "WebhookLog",
        back_populates="webhook",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        """Returns an unambiguous, debug-friendly representation of the record.

        Returns:
            str: A concise representation including id, name, event,
            and status.
        """
        return (
            f"<Webhook id={self.id} name={self.name!r} "
            f"event={self.event!r} status={self.status!r}>"
        )


# ---------------------------------------------------------------------------
# WebhookLog Model
# ---------------------------------------------------------------------------
class WebhookLog(Base):
    """Represents a single delivery attempt record for a `Webhook`.

    This is an immutable, append-only audit trail: one row is created
    per delivery attempt (including retries), so a single logical
    "event delivery" may be represented by multiple `WebhookLog` rows
    sharing the same `webhook_id`, distinguished by `attempt_count`.

    Table: webhook_logs

    Attributes:
        id: Globally unique primary key (server-generated UUID).
        webhook_id: FK to the parent `Webhook` this delivery attempt
            belongs to.
        delivery_status: Outcome of this delivery attempt, see
            :class:`DeliveryStatus`.
        response_code: HTTP status code returned by the target, if a
            response was received.
        response_body: Raw response body returned by the target,
            truncated/stored as-is by the service layer, if any.
        attempt_count: The 1-indexed attempt number this row
            represents (`1` for the initial attempt, `2+` for
            subsequent retries).
        duration_ms: Wall-clock duration of this delivery attempt, in
            milliseconds.
        error_message: Human-readable error detail (e.g. timeout,
            connection error, non-2xx response summary), if the
            attempt failed.
        delivered_at: Timestamp this delivery attempt was made.
        created_at: Record creation timestamp.
        webhook: Relationship back to the parent `Webhook`.
    """

    __tablename__ = "webhook_logs"

    __table_args__ = (
        Index("ix_webhook_logs_webhook_id", "webhook_id"),
        Index("ix_webhook_logs_delivery_status", "delivery_status"),
        Index("ix_webhook_logs_delivered_at", "delivered_at"),
        Index("ix_webhook_logs_created_at", "created_at"),
        # Composite indexes for common dashboard/history/DLQ query patterns.
        Index("ix_webhook_logs_webhook_id_delivered_at", "webhook_id", "delivered_at"),
        Index("ix_webhook_logs_webhook_id_delivery_status", "webhook_id", "delivery_status"),
        Index("ix_webhook_logs_delivery_status_delivered_at", "delivery_status", "delivered_at"),
        # Data integrity check constraints.
        CheckConstraint(
            "attempt_count > 0", name="ck_webhook_logs_attempt_count_positive"
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_webhook_logs_duration_ms_non_negative",
        ),
        CheckConstraint(
            "response_code IS NULL OR (response_code >= 100 AND response_code < 600)",
            name="ck_webhook_logs_response_code_valid_range",
        ),
    )

    # ------------------------------------------------------------------
    # Primary Key
    # ------------------------------------------------------------------
    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )

    # ------------------------------------------------------------------
    # Parent Reference
    # ------------------------------------------------------------------
    webhook_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("webhooks.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ------------------------------------------------------------------
    # Delivery Outcome
    # ------------------------------------------------------------------
    delivery_status: Mapped[DeliveryStatus] = mapped_column(
        SAEnum(
            DeliveryStatus,
            name="webhook_delivery_status_enum",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=DeliveryStatus.PENDING,
        server_default=DeliveryStatus.PENDING.value,
    )

    response_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    response_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    duration_ms: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 3), nullable=True
    )

    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    webhook: Mapped["Webhook"] = relationship(
        "Webhook",
        back_populates="logs",
        foreign_keys=[webhook_id],
        lazy="selectin",
    )

    def __repr__(self) -> str:
        """Returns an unambiguous, debug-friendly representation of the record.

        Returns:
            str: A concise representation including id, webhook_id,
            delivery_status, attempt_count, and response_code.
        """
        return (
            f"<WebhookLog id={self.id} webhook_id={self.webhook_id} "
            f"delivery_status={self.delivery_status!r} "
            f"attempt_count={self.attempt_count} response_code={self.response_code}>"
        )