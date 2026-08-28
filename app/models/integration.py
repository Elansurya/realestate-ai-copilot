"""
backend/app/models/integration.py

SQLAlchemy 2.x (async) ORM model for the Integration Management module
of the Enterprise Real Estate AI Copilot CRM.

An ``Integration`` represents a single configured connection to an
external system or third-party service the CRM talks to -- email/SMS/
WhatsApp providers, cloud storage, calendar, AI providers, payment
gateways, webhook targets, or arbitrary custom REST APIs. This model
is deliberately generic/polymorphic (one table for every integration
kind, differentiated by :class:`IntegrationType` / :class:`IntegrationProvider`)
rather than one table per provider, since every integration ultimately
needs the same shape of data: how to authenticate, where to send
requests, and how to behave operationally (timeouts, retries, rate
limits, health).

Conventions (mirrors `app/models/task.py` / `app/models/search.py`):
    - `Base` comes from `app.db.base`.
    - The primary key is a server-generated PostgreSQL UUID via
      `func.gen_random_uuid()` (requires the `pgcrypto` extension,
      already enabled by earlier migrations in this project).
    - `created_by_id` is an `Integer` FK to `users.id`, matching
      `User.id`'s actual type (see `app/models/user.py`), consistent
      with `Task.created_by_id` in `app/models/task.py`. It is
      nullable to support system-provisioned/seeded integrations that
      were not created interactively by a user.
    - Enums are native PostgreSQL ENUM types (via SQLAlchemy's
      `Enum`) for strong data-integrity at the database level.
    - Timestamps are timezone-aware (UTC).
    - `User` is imported only under `TYPE_CHECKING` to avoid a
      runtime circular-import surface. The relationship to `User` is
      intentionally one-directional (no `back_populates`) so this
      module does not require any change to `app/models/user.py`.
    - `credentials` is a JSONB column that holds only the *encrypted
      ciphertext envelope* (e.g. a KMS/Fernet-encrypted blob plus key
      metadata) produced by the application layer -- this model makes
      no attempt to perform encryption/decryption itself and never
      stores plaintext secrets. Application-level encrypt/decrypt
      logic belongs to the (out of scope) service layer, not this
      model.
    - `configuration` is a separate, non-secret JSONB column for
      provider-specific settings that are safe to display/log (e.g.
      a Google Drive root folder id, an S3 bucket name, a Stripe
      webhook signing header name) -- this split keeps secrets and
      configuration independently queryable/auditable.
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
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

__all__ = [
    "IntegrationType",
    "IntegrationProvider",
    "IntegrationStatus",
    "AuthenticationType",
    "Integration",
]

if TYPE_CHECKING:
    from app.models.user import User


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class IntegrationType(str, enum.Enum):
    """Enumerates the functional category of an external integration.

    Attributes:
        EMAIL: Outbound transactional/bulk email delivery (e.g. SMTP).
        SMS: Outbound SMS delivery.
        WHATSAPP: WhatsApp Business messaging.
        CALENDAR: Calendar scheduling/sync.
        STORAGE: Object/blob file storage.
        NOTIFICATION: Push/messaging notification delivery (e.g. Firebase).
        AI_PROVIDER: Large-language-model / AI inference provider.
        PAYMENT_GATEWAY: Payment collection/processing provider.
        WEBHOOK: An outbound webhook target the CRM delivers events to.
        CUSTOM_API: A generic, custom REST API integration not covered
            by a more specific type.
    """

    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    CALENDAR = "calendar"
    STORAGE = "storage"
    NOTIFICATION = "notification"
    AI_PROVIDER = "ai_provider"
    PAYMENT_GATEWAY = "payment_gateway"
    WEBHOOK = "webhook"
    CUSTOM_API = "custom_api"


class IntegrationProvider(str, enum.Enum):
    """Enumerates the specific third-party providers Integration supports.

    Attributes:
        SMTP: Generic SMTP email provider.
        SMS_PROVIDER: Generic SMS gateway provider.
        WHATSAPP_BUSINESS: WhatsApp Business API.
        GOOGLE_CALENDAR: Google Calendar.
        GOOGLE_DRIVE: Google Drive.
        AWS_S3: Amazon S3.
        AZURE_BLOB_STORAGE: Azure Blob Storage.
        FIREBASE: Firebase (push notifications / cloud messaging).
        OPENAI: OpenAI.
        ANTHROPIC: Anthropic.
        GEMINI: Google Gemini.
        HUGGING_FACE: Hugging Face.
        RAZORPAY: Razorpay payment gateway.
        STRIPE: Stripe payment gateway.
        WEBHOOK_TARGET: A generic outbound webhook target.
        CUSTOM_REST_API: A generic, custom REST API.
    """

    SMTP = "smtp"
    SMS_PROVIDER = "sms_provider"
    WHATSAPP_BUSINESS = "whatsapp_business"
    GOOGLE_CALENDAR = "google_calendar"
    GOOGLE_DRIVE = "google_drive"
    AWS_S3 = "aws_s3"
    AZURE_BLOB_STORAGE = "azure_blob_storage"
    FIREBASE = "firebase"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    HUGGING_FACE = "hugging_face"
    RAZORPAY = "razorpay"
    STRIPE = "stripe"
    WEBHOOK_TARGET = "webhook_target"
    CUSTOM_REST_API = "custom_rest_api"


class IntegrationStatus(str, enum.Enum):
    """Enumerates the operational lifecycle status of an Integration.

    Attributes:
        ACTIVE: The integration is configured and available for use.
        INACTIVE: The integration is configured but intentionally disabled.
        PENDING_VERIFICATION: The integration has been created but not
            yet successfully health-checked/verified.
        FAILED: The integration's most recent health check or usage
            attempt failed.
        DISABLED: The integration has been administratively disabled
            (e.g. suspended pending investigation), distinct from a
            user choosing to deactivate it via `INACTIVE`.
    """

    ACTIVE = "active"
    INACTIVE = "inactive"
    PENDING_VERIFICATION = "pending_verification"
    FAILED = "failed"
    DISABLED = "disabled"


class AuthenticationType(str, enum.Enum):
    """Enumerates the authentication mechanism an Integration uses.

    Attributes:
        API_KEY: A single static API key/token.
        OAUTH2: OAuth 2.0 (authorization code / refresh token flow).
        BASIC_AUTH: HTTP Basic authentication (username/password).
        BEARER_TOKEN: A static bearer token sent via the Authorization header.
        HMAC_SIGNATURE: Requests/webhooks signed with an HMAC secret.
        NONE: No authentication (e.g. an unauthenticated public webhook).
    """

    API_KEY = "api_key"
    OAUTH2 = "oauth2"
    BASIC_AUTH = "basic_auth"
    BEARER_TOKEN = "bearer_token"
    HMAC_SIGNATURE = "hmac_signature"
    NONE = "none"


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------
class Integration(Base):
    """Represents a single configured connection to an external system.

    Attributes:
        id: Surrogate primary key (UUID v4).
        name: Human-readable, per-tenant-unique name for this
            integration instance (e.g. "Primary SMTP", "S3 - Documents").
        provider: The specific third-party provider, see
            :class:`IntegrationProvider`.
        integration_type: The functional category of the integration,
            see :class:`IntegrationType`.
        status: Current operational status, see :class:`IntegrationStatus`.
        authentication_type: The authentication mechanism used, see
            :class:`AuthenticationType`.
        configuration: Non-secret, provider-specific settings JSONB
            payload (e.g. bucket name, calendar id, model name).
        credentials: Encrypted-at-rest credentials JSONB payload. This
            column is a placeholder for an application-layer-encrypted
            ciphertext envelope; plaintext secrets must never be
            written here directly.
        base_url: Base URL of the external API/service, if applicable.
        api_version: API version string/identifier requested against
            the provider, if applicable (e.g. ``"2024-06-20"``).
        webhook_url: Inbound/outbound webhook URL associated with this
            integration, if applicable (e.g. a payment gateway's
            callback URL, or the target URL for `WEBHOOK` type).
        timeout_seconds: Per-request timeout, in seconds, applied when
            calling out to this integration.
        retry_count: Number of retries to attempt on a failed request
            to this integration.
        rate_limit_per_minute: Maximum number of requests permitted to
            this integration per minute, if the provider/CRM enforces
            client-side rate limiting.
        is_default: Whether this integration is the default instance
            for its `integration_type` (e.g. the default outbound
            email provider). Enforced unique per (`integration_type`,
            `is_default`) when `True` -- see `__table_args__`.
        last_sync_at: Timestamp of the last successful data
            sync/exchange with this integration, if applicable.
        last_health_check_at: Timestamp of the last health check
            attempt against this integration, regardless of outcome.
        created_by_id: FK to the user who created this integration.
            Nullable to support system-provisioned/seeded integrations.
        is_deleted: Soft-delete flag.
        deleted_at: Timestamp of soft deletion, if any.
        created_at: Record creation timestamp.
        updated_at: Record last-update timestamp.
        created_by: Relationship to the creating ``User`` (one-directional).
    """

    __tablename__ = "integrations"
    __table_args__ = (
        Index("ix_integrations_provider", "provider"),
        Index("ix_integrations_integration_type", "integration_type"),
        Index("ix_integrations_status", "status"),
        Index("ix_integrations_authentication_type", "authentication_type"),
        Index("ix_integrations_created_by_id", "created_by_id"),
        Index("ix_integrations_is_default", "is_default"),
        Index("ix_integrations_is_deleted", "is_deleted"),
        Index("ix_integrations_created_at", "created_at"),
        Index("ix_integrations_last_sync_at", "last_sync_at"),
        Index("ix_integrations_last_health_check_at", "last_health_check_at"),
        # Composite indexes for common query patterns.
        Index(
            "ix_integrations_integration_type_status",
            "integration_type",
            "status",
        ),
        Index(
            "ix_integrations_integration_type_provider",
            "integration_type",
            "provider",
        ),
        Index(
            "ix_integrations_provider_status",
            "provider",
            "status",
        ),
        Index(
            "ix_integrations_integration_type_is_default",
            "integration_type",
            "is_default",
        ),
        # Uniqueness constraints.
        UniqueConstraint("name", name="uq_integrations_name"),
        # Data integrity check constraints.
        CheckConstraint("btrim(name) <> ''", name="ck_integrations_name_not_empty"),
        CheckConstraint(
            "timeout_seconds > 0", name="ck_integrations_timeout_seconds_positive"
        ),
        CheckConstraint(
            "retry_count >= 0", name="ck_integrations_retry_count_non_negative"
        ),
        CheckConstraint(
            "rate_limit_per_minute IS NULL OR rate_limit_per_minute > 0",
            name="ck_integrations_rate_limit_positive",
        ),
        CheckConstraint(
            "(is_deleted IS FALSE AND deleted_at IS NULL) "
            "OR (is_deleted IS TRUE AND deleted_at IS NOT NULL)",
            name="ck_integrations_soft_delete_consistency",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )

    name: Mapped[str] = mapped_column(String(150), nullable=False)

    provider: Mapped[IntegrationProvider] = mapped_column(
        SAEnum(
            IntegrationProvider,
            name="integration_provider_enum",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )

    integration_type: Mapped[IntegrationType] = mapped_column(
        SAEnum(
            IntegrationType,
            name="integration_type_enum",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )

    status: Mapped[IntegrationStatus] = mapped_column(
        SAEnum(
            IntegrationStatus,
            name="integration_status_enum",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=IntegrationStatus.PENDING_VERIFICATION,
        server_default=IntegrationStatus.PENDING_VERIFICATION.value,
    )

    authentication_type: Mapped[AuthenticationType] = mapped_column(
        SAEnum(
            AuthenticationType,
            name="authentication_type_enum",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=AuthenticationType.API_KEY,
        server_default=AuthenticationType.API_KEY.value,
    )

    configuration: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    credentials: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    base_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    api_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    webhook_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    rate_limit_per_minute: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )

    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    last_sync_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_health_check_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

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

    created_by: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[created_by_id],
        lazy="selectin",
        viewonly=True,
    )

    def __repr__(self) -> str:
        """Returns an unambiguous, debug-friendly representation of the record.

        Returns:
            str: A concise representation including id, name, provider,
            integration_type, and status.
        """
        return (
            f"<Integration id={self.id} name={self.name!r} "
            f"provider={self.provider!r} integration_type={self.integration_type!r} "
            f"status={self.status!r}>"
        )