"""
backend/app/models/booking.py

SQLAlchemy 2.x ORM model representing a Booking within the Real Estate
AI Copilot CRM.

A Booking is the formal record of a Customer committing to a Property
(via token/booking payment), created and tracked by an assigned agent.
A Booking always references an existing Customer and Property, and MAY
optionally trace its origin back to the Lead that was converted.

Conventions (mirrors `app/models/lead.py` / `app/models/customer.py` /
`app/models/property.py`):
    - `Base` comes from `app.db.base`; timestamps are inline,
      timezone-aware UTC columns (no mixins).
    - `id` is a server-generated PostgreSQL UUID via
      `func.gen_random_uuid()`, identical to `Lead.id` / `Customer.id`
      (same `pgcrypto` extension requirement).
    - `customer_id` is a `PG_UUID` FK to `customers.id` (matching
      `Customer.id`'s actual type).
    - `property_id` is an `Integer` FK to `properties.id` (matching
      `Property.id`'s actual type — `Property` uses an integer surrogate
      PK with a separate public-facing `uuid` column, per
      `app/models/property.py`).
    - `lead_id` is an optional `PG_UUID` FK to `leads.id`, matching the
      same optional-provenance pattern as `Customer.lead_id`.
    - `agent_id` / `created_by` are `Integer` FKs to `users.id`,
      matching `User.id`'s actual type — the same typing
      `Lead.assigned_agent_id` / `Customer.assigned_to_id` already use.
    - `status` and `payment_status` use native PostgreSQL ENUM types
      (mirroring `Lead.status` / `Customer.status`) for strong data
      integrity at the database level.
    - `User`/`Customer`/`Property`/`Lead` are imported only under
      `TYPE_CHECKING` to avoid a runtime circular-import surface;
      relationships and `Mapped[]` annotations reference them by
      string, resolved by SQLAlchemy's mapper configuration once every
      model module has been imported.
    - `is_active` is a plain boolean soft-disable flag, matching
      `Lead.is_active` / `Customer.is_active` / `Property.is_active`.
    - Single-column indexes are declared inline (`index=True`); only
      composite indexes and CHECK constraints live in `__table_args__`,
      exactly as `Lead` / `Customer` already do.
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
    Sequence,
    String,
    Text,
    text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.customer import Customer
    from app.models.lead import Lead
    from app.models.payment import Payment
    from app.models.property import Property
    from app.models.user import User


# --------------------------------------------------------------------------
# Booking Status Enumeration
# --------------------------------------------------------------------------
class BookingStatus(str, enum.Enum):
    """
    Defines the lifecycle stage of a booking, from initial creation
    through to completion, cancellation, or refund.
    """

    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    REFUNDED = "REFUNDED"


# --------------------------------------------------------------------------
# Booking Payment Status Enumeration
# --------------------------------------------------------------------------
class BookingPaymentStatus(str, enum.Enum):
    """
    Defines the payment state of a booking's token/booking amount,
    used to drive collections follow-up and finance reporting.
    """

    PENDING = "PENDING"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    # Backward-compatible Python alias used by older service/test callers.
    PARTIAL = "PARTIALLY_PAID"
    PAID = "PAID"
    OVERDUE = "OVERDUE"
    REFUNDED = "REFUNDED"


# --------------------------------------------------------------------------
# Booking Payment Mode Enumeration
# --------------------------------------------------------------------------
class BookingPaymentMode(str, enum.Enum):
    """
    Defines the channel through which a booking's token/booking amount
    was (or is expected to be) collected.
    """

    CASH = "CASH"
    CHEQUE = "CHEQUE"
    BANK_TRANSFER = "BANK_TRANSFER"
    UPI = "UPI"
    CARD = "CARD"
    OTHER = "OTHER"


# --------------------------------------------------------------------------
# Booking Model
# --------------------------------------------------------------------------
class Booking(Base):
    """
    Represents a Booking entity: the formal record of a Customer
    committing to a Property, tracked from initial reservation through
    to confirmation, completion, cancellation, or refund.

    Table: bookings
    """

    __tablename__ = "bookings"

    __table_args__ = (
        Index("ix_bookings_status_payment_status", "status", "payment_status"),
        Index("ix_bookings_agent_id_status", "agent_id", "status"),
        Index("ix_bookings_customer_id_property_id", "customer_id", "property_id"),
        CheckConstraint(
            "booking_amount IS NULL OR booking_amount >= 0",
            name="ck_bookings_booking_amount_non_negative",
        ),
        CheckConstraint(
            "token_amount IS NULL OR token_amount >= 0",
            name="ck_bookings_token_amount_non_negative",
        ),
        CheckConstraint(
            "token_amount IS NULL OR booking_amount IS NULL OR token_amount <= booking_amount",
            name="ck_bookings_token_amount_lte_booking_amount",
        ),
    )

    # ----------------------------------------------------------------
    # Human-readable booking reference
    # ----------------------------------------------------------------
    booking_number: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        unique=True,
        index=True,
        server_default=Sequence(
            "bookings_booking_number_seq",
            metadata=Base.metadata,
        ).next_value(),
        doc="Unique human-readable booking reference.",
    )

    # ----------------------------------------------------------------
    # Primary Key
    # ----------------------------------------------------------------
    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        doc=(
            "Globally unique primary key for the booking record. "
            "Requires the PostgreSQL `pgcrypto` extension to be "
            "enabled for `gen_random_uuid()` to be available."
        ),
    )

    # ----------------------------------------------------------------
    # Relationship Foreign Keys
    # ----------------------------------------------------------------
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        doc="The Customer who is booking the property.",
    )

    property_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("properties.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        doc="Internal integer ID of the Property being booked.",
    )

    lead_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Optional reference to the originating Lead record.",
    )

    agent_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Internal ID of the User (sales agent) handling this booking.",
    )

    created_by: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Internal ID of the User who created this booking record.",
    )

    # ----------------------------------------------------------------
    # Booking Details
    # ----------------------------------------------------------------
    booking_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        server_default=func.current_date(),
        index=True,
        doc="Date on which the booking was made.",
    )

    booking_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(14, 2),
        nullable=True,
        doc="Total agreed booking value for the property, in local currency units.",
    )

    token_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(14, 2),
        nullable=True,
        doc="Token/advance amount collected against the total booking amount.",
    )

    payment_mode: Mapped[Optional[BookingPaymentMode]] = mapped_column(
        SAEnum(
            BookingPaymentMode,
            name="booking_payment_mode",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=True,
        doc="Channel through which the token/booking amount was collected.",
    )

    payment_reference: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        doc="Transaction/cheque/UTR reference number for the payment, if any.",
    )

    # ----------------------------------------------------------------
    # Pipeline Classification Fields
    # ----------------------------------------------------------------
    status: Mapped[BookingStatus] = mapped_column(
        SAEnum(
            BookingStatus,
            name="booking_status",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
        default=BookingStatus.PENDING,
        server_default=BookingStatus.PENDING.value,
        index=True,
        doc="Current lifecycle stage of the booking.",
    )

    payment_status: Mapped[BookingPaymentStatus] = mapped_column(
        SAEnum(
            BookingPaymentStatus,
            name="booking_payment_status",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
        default=BookingPaymentStatus.PENDING,
        server_default=BookingPaymentStatus.PENDING.value,
        index=True,
        doc="Current payment state of the booking's token/booking amount.",
    )

    # ----------------------------------------------------------------
    # Site Visit / Follow-up Fields
    # ----------------------------------------------------------------
    site_visit_date: Mapped[Optional[date]] = mapped_column(
        Date,
        nullable=True,
        doc="Date of the site visit that led to (or is tied to) this booking.",
    )

    next_follow_up: Mapped[Optional[date]] = mapped_column(
        Date,
        nullable=True,
        index=True,
        doc="Scheduled date for the next follow-up action on this booking.",
    )

    # ----------------------------------------------------------------
    # Notes
    # ----------------------------------------------------------------
    remarks: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Free-form notes/remarks recorded by agents about this booking.",
    )

    cancellation_reason: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Reason recorded when a booking's status is set to CANCELLED.",
    )

    # ----------------------------------------------------------------
    # Status Flags
    # ----------------------------------------------------------------
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        doc="Soft-disable flag; inactive bookings are excluded from active views.",
    )

    # ----------------------------------------------------------------
    # Audit Timestamps (Timezone-Aware, UTC)
    # ----------------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="UTC timestamp when the booking record was created.",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        doc="UTC timestamp when the booking record was last updated.",
    )

    # ----------------------------------------------------------------
    # Relationships
    # ----------------------------------------------------------------
    # `lazy="raise_on_sql"` (not "selectin"): matches `Lead.assigned_agent`
    # / `Lead.creator` — the repository's own `selectinload()` calls are
    # the sole, correctly-conditional loading path via its
    # `with_relationships` flag, so the model stays loading-neutral by
    # default instead of silently eager-loading on every query.
    customer: Mapped["Customer"] = relationship(
        "Customer",
        foreign_keys=[customer_id],
        lazy="raise_on_sql",
        doc="The Customer who is booking the property.",
    )

    property: Mapped["Property"] = relationship(
        "Property",
        foreign_keys=[property_id],
        lazy="raise_on_sql",
        doc="The Property being booked.",
    )

    lead: Mapped[Optional["Lead"]] = relationship(
        "Lead",
        foreign_keys=[lead_id],
        lazy="raise_on_sql",
        doc="The Lead this booking originated from, if any.",
    )

    agent: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[agent_id],
        lazy="raise_on_sql",
        doc="The User (sales agent) currently handling this booking.",
    )

    creator: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[created_by],
        lazy="raise_on_sql",
        doc="The User who originally created this booking record.",
    )

    payments: Mapped[list["Payment"]] = relationship(
        "Payment",
        back_populates="booking",
        lazy="raise_on_sql",
        doc="All Payment records associated with this booking.",
    )

    # ----------------------------------------------------------------
    # Developer Ergonomics
    # ----------------------------------------------------------------
    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return (
            f"<Booking id={self.id} customer_id={self.customer_id} "
            f"property_id={self.property_id} status={self.status.value}>"
        )


__all__ = [
    "Booking",
    "BookingStatus",
    "BookingPaymentStatus",
    "BookingPaymentMode",
]