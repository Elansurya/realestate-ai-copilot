"""
backend/app/models/settings.py

SQLAlchemy 2.x ORM model representing a system Setting within the Real
Estate AI Copilot CRM.

A Setting is a single, typed, categorized key/value configuration entry
(e.g. ``category="EMAIL"``, ``setting_key="SMTP_HOST"``) that drives
runtime behavior of the platform without requiring a code deployment.
Settings power the General, Company, Security, Email, SMS, WhatsApp, AI,
Dashboard, Reports, Notifications, Storage, Backup, Theme, Audit, and
System configuration surfaces of the application.

Conventions (mirrors `app/models/booking.py` / `app/models/lead.py` /
`app/models/customer.py` / `app/models/property.py`):
    - `Base` comes from `app.db.base`; timestamps are inline,
      timezone-aware UTC columns (no mixins).
    - `id` is a server-generated PostgreSQL UUID via
      `func.gen_random_uuid()`, identical to `Lead.id` / `Customer.id`
      (same `pgcrypto` extension requirement).
    - `created_by` / `updated_by` are `Integer` FKs to `users.id`,
      matching `User.id`'s actual type -- the same typing
      `Lead.assigned_agent_id` / `Booking.agent_id` already use.
    - `category` and `data_type` use native PostgreSQL ENUM types
      (mirroring `Lead.status` / `Booking.status`) for strong data
      integrity at the database level.
    - `setting_value` and `validation_rules` are `JSONB` columns,
      allowing each setting to store an arbitrarily shaped, strongly
      validated value while remaining queryable at the database level.
    - `User` is imported only under `TYPE_CHECKING` to avoid a runtime
      circular-import surface; relationships and `Mapped[]` annotations
      reference it by string, resolved by SQLAlchemy's mapper
      configuration once every model module has been imported.
    - A composite unique constraint on (`category`, `setting_key`)
      guarantees each configuration key is defined at most once per
      category, mirroring the uniqueness guarantees already enforced
      elsewhere in the schema (e.g. `Customer.email`).
    - Single-column indexes are declared inline (`index=True`); only
      composite indexes, the unique constraint, and CHECK constraints
      live in `__table_args__`, exactly as `Lead` / `Booking` already do.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User


# --------------------------------------------------------------------------
# Setting Category Enumeration
# --------------------------------------------------------------------------
class SettingCategory(str, enum.Enum):
    """
    Defines the functional area a setting belongs to, used to group and
    scope configuration entries across the admin Settings module.
    """

    GENERAL = "GENERAL"
    COMPANY = "COMPANY"
    SECURITY = "SECURITY"
    EMAIL = "EMAIL"
    SMS = "SMS"
    WHATSAPP = "WHATSAPP"
    AI = "AI"
    DASHBOARD = "DASHBOARD"
    REPORTS = "REPORTS"
    NOTIFICATIONS = "NOTIFICATIONS"
    STORAGE = "STORAGE"
    BACKUP = "BACKUP"
    THEME = "THEME"
    AUDIT = "AUDIT"
    SYSTEM = "SYSTEM"


# --------------------------------------------------------------------------
# Setting Data Type Enumeration
# --------------------------------------------------------------------------
class SettingDataType(str, enum.Enum):
    """
    Defines the logical data type stored inside a setting's
    ``setting_value`` JSONB payload, used to drive validation and
    rendering of the appropriate input control on the client.
    """

    STRING = "STRING"
    INTEGER = "INTEGER"
    FLOAT = "FLOAT"
    BOOLEAN = "BOOLEAN"
    JSON = "JSON"
    ARRAY = "ARRAY"
    DATE = "DATE"
    DATETIME = "DATETIME"
    EMAIL = "EMAIL"
    URL = "URL"
    PASSWORD = "PASSWORD"


# --------------------------------------------------------------------------
# Settings Model
# --------------------------------------------------------------------------
class Settings(Base):
    """
    Represents a Settings entity: a single, typed, categorized
    configuration key/value entry that governs runtime behavior of the
    platform (General, Company, Security, Email, SMS, WhatsApp, AI,
    Dashboard, Reports, Notifications, Storage, Backup, Theme, Audit,
    and System).

    Table: settings
    """

    __tablename__ = "settings"

    __table_args__ = (
        UniqueConstraint(
            "category", "setting_key", name="uq_settings_category_setting_key"
        ),
        Index("ix_settings_category_is_public", "category", "is_public"),
        Index("ix_settings_category_is_editable", "category", "is_editable"),
        CheckConstraint(
            "btrim(setting_key) <> ''", name="ck_settings_setting_key_not_empty"
        ),
        CheckConstraint(
            "NOT (is_encrypted AND is_public)",
            name="ck_settings_encrypted_not_public",
        ),
    )

    # ----------------------------------------------------------------
    # Primary Key
    # ----------------------------------------------------------------
    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        doc=(
            "Globally unique primary key for the setting record. "
            "Requires the PostgreSQL `pgcrypto` extension to be "
            "enabled for `gen_random_uuid()` to be available."
        ),
    )

    # ----------------------------------------------------------------
    # Classification Fields
    # ----------------------------------------------------------------
    category: Mapped[SettingCategory] = mapped_column(
        SAEnum(
            SettingCategory,
            name="setting_category",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
        index=True,
        doc="Functional area this setting belongs to (e.g. EMAIL, SECURITY).",
    )

    setting_key: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
        index=True,
        doc="Unique-per-category configuration key, e.g. 'SMTP_HOST'.",
    )

    # ----------------------------------------------------------------
    # Value Fields
    # ----------------------------------------------------------------
    setting_value: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=True,
        doc="The configured value, stored as JSONB to support any shape.",
    )

    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Human-readable explanation of what this setting controls.",
    )

    data_type: Mapped[SettingDataType] = mapped_column(
        SAEnum(
            SettingDataType,
            name="setting_data_type",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
        default=SettingDataType.STRING,
        server_default=SettingDataType.STRING.value,
        doc="Logical data type of `setting_value`, used to drive validation.",
    )

    validation_rules: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=True,
        doc=(
            "Optional JSONB ruleset (e.g. min/max, regex, allowed values) "
            "used to validate `setting_value` at the service layer."
        ),
    )

    # ----------------------------------------------------------------
    # Access Control Flags
    # ----------------------------------------------------------------
    is_public: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        index=True,
        doc="Whether this setting may be exposed to unauthenticated clients.",
    )

    is_editable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        index=True,
        doc="Whether this setting may be modified via the Settings UI/API.",
    )

    is_encrypted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        doc="Whether `setting_value` is stored/transmitted in encrypted form.",
    )

    # ----------------------------------------------------------------
    # Relationship Foreign Keys
    # ----------------------------------------------------------------
    created_by: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Internal ID of the User who created this setting record.",
    )

    updated_by: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Internal ID of the User who last updated this setting record.",
    )

    # ----------------------------------------------------------------
    # Audit Timestamps (Timezone-Aware, UTC)
    # ----------------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="UTC timestamp when the setting record was created.",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        doc="UTC timestamp when the setting record was last updated.",
    )

    # ----------------------------------------------------------------
    # Relationships
    # ----------------------------------------------------------------
    # `lazy="raise_on_sql"` (not "selectin"): matches `Booking.agent` /
    # `Booking.creator` -- the repository's own `selectinload()` calls are
    # the sole, correctly-conditional loading path via its
    # `with_relationships` flag, so the model stays loading-neutral by
    # default instead of silently eager-loading on every query.
    creator: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[created_by],
        lazy="raise_on_sql",
        doc="The User who originally created this setting record.",
    )

    updater: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[updated_by],
        lazy="raise_on_sql",
        doc="The User who last updated this setting record.",
    )

    # ----------------------------------------------------------------
    # Developer Ergonomics
    # ----------------------------------------------------------------
    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return (
            f"<Settings id={self.id} category={self.category.value} "
            f"setting_key={self.setting_key!r}>"
        )


__all__ = [
    "Settings",
    "SettingCategory",
    "SettingDataType",
]