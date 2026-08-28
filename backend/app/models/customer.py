"""
backend/app/models/customer.py

SQLAlchemy 2.x ORM model representing a Customer within the Real Estate
AI Copilot CRM.

A Customer is a confirmed, engaged contact — converted from a Lead or
onboarded directly — with whom the agency maintains an active commercial
relationship (buying, selling, renting, letting, or investing). A Customer
MAY optionally trace its origin back to the Lead it was converted from.

Conventions (mirrors `app/models/lead.py` / `app/models/user.py`):
    - `Base` comes from `app.db.base`; timestamps are inline, timezone-aware
      UTC columns (no mixins — `app.models.mixins` does not exist in this
      project).
    - `User`/`Lead` are imported only under `TYPE_CHECKING` to avoid a
      runtime circular-import surface; relationships and `Mapped[]`
      annotations reference them by string, resolved by SQLAlchemy's mapper
      configuration (`Base.registry`) once every model module has been
      imported.
    - `id` is a server-generated PostgreSQL UUID via `func.gen_random_uuid()`,
      identical to `Lead.id` (same `pgcrypto` extension requirement).
    - `assigned_to_id` / `created_by_id` / `updated_by_id` are `Integer`
      FKs to `users.id`, matching `User.id`'s actual type — the same
      typing `Lead.assigned_agent_id` / `Lead.created_by` already use.
    - `is_active` is a plain boolean soft-disable flag, matching
      `Lead.is_active` / `Property.is_active`.
    - Single-column indexes are declared inline (`index=True`), exactly as
      `Lead.status` / `Lead.priority` / `Lead.lead_source` are. Only
      composite and partial (WHERE-clause) indexes live in `__table_args__`,
      so no column is ever indexed twice under the same generated name.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.lead import Lead
    from app.models.payment import Payment
    from app.models.user import User


class CustomerType(str, enum.Enum):
    """Commercial role a Customer plays in a transaction."""

    BUYER = "BUYER"
    SELLER = "SELLER"
    TENANT = "TENANT"
    LANDLORD = "LANDLORD"
    INVESTOR = "INVESTOR"


class CustomerStatus(str, enum.Enum):
    """Lifecycle status of a Customer record within the CRM."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    PROSPECT = "PROSPECT"
    BLACKLISTED = "BLACKLISTED"
    ARCHIVED = "ARCHIVED"


class CustomerSource(str, enum.Enum):
    """Acquisition channel through which a Customer was onboarded."""

    WEBSITE = "WEBSITE"
    REFERRAL = "REFERRAL"
    WALK_IN = "WALK_IN"
    SOCIAL_MEDIA = "SOCIAL_MEDIA"
    PROPERTY_PORTAL = "PROPERTY_PORTAL"
    COLD_CALL = "COLD_CALL"
    EVENT = "EVENT"
    PARTNER_AGENT = "PARTNER_AGENT"
    ADVERTISEMENT = "ADVERTISEMENT"
    LEAD_CONVERSION = "LEAD_CONVERSION"
    OTHER = "OTHER"


class Gender(str, enum.Enum):
    """Self-identified gender captured for a Customer."""

    MALE = "MALE"
    FEMALE = "FEMALE"
    OTHER = "OTHER"
    PREFER_NOT_TO_SAY = "PREFER_NOT_TO_SAY"


class MaritalStatus(str, enum.Enum):
    """Marital status captured for a Customer."""

    SINGLE = "SINGLE"
    MARRIED = "MARRIED"
    DIVORCED = "DIVORCED"
    WIDOWED = "WIDOWED"
    OTHER = "OTHER"


class PreferredPropertyType(str, enum.Enum):
    """Type of property a Customer is primarily interested in."""

    APARTMENT = "APARTMENT"
    VILLA = "VILLA"
    INDEPENDENT_HOUSE = "INDEPENDENT_HOUSE"
    PLOT = "PLOT"
    COMMERCIAL = "COMMERCIAL"
    OFFICE_SPACE = "OFFICE_SPACE"
    WAREHOUSE = "WAREHOUSE"
    OTHER = "OTHER"


class PreferredBHK(str, enum.Enum):
    """Room configuration (BHK) a Customer prefers."""

    STUDIO = "STUDIO"
    ONE_BHK = "ONE_BHK"
    TWO_BHK = "TWO_BHK"
    THREE_BHK = "THREE_BHK"
    FOUR_BHK = "FOUR_BHK"
    FIVE_PLUS_BHK = "FIVE_PLUS_BHK"


class Customer(Base):
    """
    Represents a Customer entity: an individual with an active commercial
    relationship with the agency, together with KYC, professional profile,
    preferences, and a full audit trail.

    Table: customers
    """

    __tablename__ = "customers"

    __table_args__ = (
        # Composite index — cannot be expressed as a single inline index=True.
        Index("ix_customers_name", "last_name", "first_name"),
        # Partial unique indexes: Postgres partial uniqueness requires a
        # WHERE clause, which only Index (not UniqueConstraint) supports in
        # this SQLAlchemy version. Named with the "uq_" prefix to signal
        # uniqueness semantics, consistent with the project's naming
        # convention vocabulary defined in app/db/base.py.
        Index(
            "uq_customers_pan_number",
            "pan_number",
            unique=True,
            postgresql_where=text("pan_number IS NOT NULL"),
        ),
        Index(
            "uq_customers_passport_number",
            "passport_number",
            unique=True,
            postgresql_where=text("passport_number IS NOT NULL"),
        ),
        CheckConstraint(
            "annual_income IS NULL OR annual_income >= 0",
            name="ck_customers_annual_income_non_negative",
        ),
        CheckConstraint(
            "budget_min IS NULL OR budget_min >= 0",
            name="ck_customers_budget_min_non_negative",
        ),
        CheckConstraint(
            "budget_max IS NULL OR budget_max >= 0",
            name="ck_customers_budget_max_non_negative",
        ),
        CheckConstraint(
            "budget_min IS NULL OR budget_max IS NULL OR budget_max >= budget_min",
            name="ck_customers_budget_max_gte_min",
        ),
        CheckConstraint(
            "pan_number IS NULL OR pan_number ~ '^[A-Z]{5}[0-9]{4}[A-Z]{1}$'",
            name="ck_customers_pan_number_format",
        ),
        CheckConstraint(
            "aadhaar_number IS NULL OR aadhaar_number ~ '^[Xx]{4}-[Xx]{4}-[0-9]{4}$'",
            name="ck_customers_aadhaar_number_masked_format",
        ),
        CheckConstraint(
            "postal_code IS NULL OR length(trim(postal_code)) > 0",
            name="ck_customers_postal_code_not_blank",
        ),
        CheckConstraint(
            "date_of_birth IS NULL OR date_of_birth <= created_at::date",
            name="ck_customers_dob_not_after_creation",
        ),
    )

    # ------------------------------------------------------------------
    # Primary Key
    # ------------------------------------------------------------------
    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        doc="Primary key; server-generated via gen_random_uuid(), matching Lead.id.",
    )

    # ------------------------------------------------------------------
    # Personal Information
    # ------------------------------------------------------------------
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    middle_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    date_of_birth: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    gender: Mapped[Optional[Gender]] = mapped_column(
        SAEnum(Gender, name="customer_gender_enum", native_enum=True, validate_strings=True),
        nullable=True,
    )

    marital_status: Mapped[Optional[MaritalStatus]] = mapped_column(
        SAEnum(
            MaritalStatus,
            name="customer_marital_status_enum",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=True,
    )

    nationality: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # ------------------------------------------------------------------
    # Contact Information
    # ------------------------------------------------------------------
    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
        doc="Unique contact email address for the customer.",
    )

    phone: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        index=True,
        doc="Primary contact phone number.",
    )

    alternate_phone: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # ------------------------------------------------------------------
    # Professional Information
    # ------------------------------------------------------------------
    occupation: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    company_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    annual_income: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(precision=14, scale=2),
        nullable=True,
        doc="Declared annual income, used for financial qualification.",
    )

    # ------------------------------------------------------------------
    # KYC (Know Your Customer)
    # ------------------------------------------------------------------
    pan_number: Mapped[Optional[str]] = mapped_column(
        String(10),
        nullable=True,
        doc="Government-issued PAN. Stored uppercase; format enforced via CHECK constraint.",
    )

    aadhaar_number: Mapped[Optional[str]] = mapped_column(
        String(14),
        nullable=True,
        doc=(
            "Masked Aadhaar representation only ('XXXX-XXXX-1234'). The full "
            "number is never persisted here; unmasking is handled exclusively "
            "by an audited, encrypted vault at the service layer."
        ),
    )

    passport_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # ------------------------------------------------------------------
    # Address
    # ------------------------------------------------------------------
    address_line_1: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    address_line_2: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    landmark: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    state: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    postal_code: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # ------------------------------------------------------------------
    # Customer Preferences
    # ------------------------------------------------------------------
    budget_min: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=16, scale=2), nullable=True)
    budget_max: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=16, scale=2), nullable=True)
    preferred_city: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    preferred_area: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)

    preferred_property_type: Mapped[Optional[PreferredPropertyType]] = mapped_column(
        SAEnum(
            PreferredPropertyType,
            name="customer_preferred_property_type_enum",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=True,
        index=True,
    )

    preferred_bhk: Mapped[Optional[PreferredBHK]] = mapped_column(
        SAEnum(PreferredBHK, name="customer_preferred_bhk_enum", native_enum=True, validate_strings=True),
        nullable=True,
    )

    # ------------------------------------------------------------------
    # Classification / Lifecycle
    # ------------------------------------------------------------------
    customer_type: Mapped[CustomerType] = mapped_column(
        SAEnum(CustomerType, name="customer_type_enum", native_enum=True, validate_strings=True),
        nullable=False,
        default=CustomerType.BUYER,
        server_default=CustomerType.BUYER.value,
        index=True,
    )

    customer_source: Mapped[CustomerSource] = mapped_column(
        SAEnum(CustomerSource, name="customer_source_enum", native_enum=True, validate_strings=True),
        nullable=False,
        default=CustomerSource.OTHER,
        server_default=CustomerSource.OTHER.value,
        index=True,
    )

    status: Mapped[CustomerStatus] = mapped_column(
        SAEnum(CustomerStatus, name="customer_status_enum", native_enum=True, validate_strings=True),
        nullable=False,
        default=CustomerStatus.ACTIVE,
        server_default=CustomerStatus.ACTIVE.value,
        index=True,
    )

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        doc="Soft-disable flag; inactive customers are excluded from active views.",
    )

    # ------------------------------------------------------------------
    # Relationship Foreign Keys
    # ------------------------------------------------------------------
    lead_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Optional reference to the originating Lead record.",
    )

    assigned_to_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="User (agent) responsible for this customer.",
    )

    created_by_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        doc="User who created this customer record.",
    )

    updated_by_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="User who last modified this customer record.",
    )

    # ------------------------------------------------------------------
    # Audit / Engagement Tracking
    # ------------------------------------------------------------------
    last_contacted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    next_followup_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
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
    lead: Mapped[Optional["Lead"]] = relationship(
        "Lead",
        foreign_keys=[lead_id],
        lazy="selectin",
        doc="The Lead this customer was converted from, if any.",
    )

    assigned_to: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[assigned_to_id],
        lazy="selectin",
        doc="The User (agent) currently responsible for this customer.",
    )

    created_by: Mapped["User"] = relationship(
        "User",
        foreign_keys=[created_by_id],
        lazy="selectin",
        doc="The User who originally created this customer record.",
    )

    updated_by: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[updated_by_id],
        lazy="selectin",
        doc="The User who last modified this customer record.",
    )

    payments: Mapped[list["Payment"]] = relationship(
        "Payment",
        back_populates="customer",
        lazy="selectin",
        doc="All Payment records made by this customer.",
    )

    # ------------------------------------------------------------------
    # Developer Ergonomics
    # ------------------------------------------------------------------
    @property
    def full_name(self) -> str:
        """Full display name, including the middle name when present."""
        parts = [self.first_name, self.middle_name, self.last_name]
        return " ".join(part for part in parts if part)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return (
            f"<Customer id={self.id} name={self.full_name!r} "
            f"type={self.customer_type.value} status={self.status.value}>"
        )