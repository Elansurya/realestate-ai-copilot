"""
backend/app/models/lead.py

SQLAlchemy 2.x ORM model representing a real-estate sales lead within
the Real Estate AI Copilot CRM.

Design Notes:
    - `id` uses a native PostgreSQL UUID primary key (server-generated
      via `gen_random_uuid()`, which requires the `pgcrypto` extension
      to be enabled on the target database) to avoid sequential ID
      enumeration and align with distributed-system-friendly identifier
      design for this entity.
    - `assigned_agent_id` and `created_by` are typed as `Integer`
      foreign keys referencing `users.id`. This is confirmed against
      the existing `User` model (`app/models/user.py`), which declares
      `id: Mapped[int] = mapped_column(Integer, primary_key=True, ...)`
      — i.e., `User.id` is a surrogate integer key, NOT a UUID, in this
      codebase. If `User.id`'s type is ever changed, these two columns
      (and their `ForeignKey` targets) must be updated to match exactly.
    - `status`, `priority`, and `lead_source` use native PostgreSQL ENUM
      types (mirroring the `role` column pattern on `User`) for strong
      data integrity at the database level.
    - All timestamps are timezone-aware (UTC), consistent with the
      `User` model's audit trail convention.
    - `assigned_agent` and `creator` are both relationships to `User`,
      distinguished via explicit `foreign_keys` to resolve the
      ambiguity of having two FKs to the same target table.
    - Composite indexes are defined via `__table_args__` to optimize
      common CRM query patterns: filtering the pipeline by
      (status, priority), and filtering an individual agent's queue by
      (assigned_agent_id, status).
    - This module imports `app.models.user.User` directly (not via a
      relationship string reference) since both models live in the same
      package with no reverse dependency from `user.py` back to
      `lead.py`, so no circular import is introduced.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
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
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.user import User


# --------------------------------------------------------------------------
# Lead Status Enumeration
# --------------------------------------------------------------------------
class LeadStatus(str, enum.Enum):
    """
    Defines the pipeline stage of a lead within the sales funnel.

    Inherits from `str` so that:
        - Values serialize cleanly to JSON without custom encoders.
        - Comparisons (e.g., `lead.status == LeadStatus.BOOKED`) work
          naturally.
        - The underlying database ENUM stores human-readable labels.
    """

    NEW = "NEW"
    CONTACTED = "CONTACTED"
    QUALIFIED = "QUALIFIED"
    SITE_VISIT = "SITE_VISIT"
    NEGOTIATION = "NEGOTIATION"
    BOOKED = "BOOKED"
    LOST = "LOST"


# --------------------------------------------------------------------------
# Lead Priority Enumeration
# --------------------------------------------------------------------------
class LeadPriority(str, enum.Enum):
    """
    Defines the urgency/priority level assigned to a lead, typically
    used to drive agent task ordering and follow-up SLAs.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"


# --------------------------------------------------------------------------
# Lead Source Enumeration
# --------------------------------------------------------------------------
class LeadSource(str, enum.Enum):
    """
    Defines the acquisition channel through which a lead entered the
    CRM, used for marketing attribution and channel performance
    reporting.
    """

    WEBSITE = "WEBSITE"
    WALK_IN = "WALK_IN"
    FACEBOOK = "FACEBOOK"
    INSTAGRAM = "INSTAGRAM"
    MAGICBRICKS = "MAGICBRICKS"
    NOBROKER = "NOBROKER"
    REFERRAL = "REFERRAL"
    PHONE = "PHONE"
    OTHER = "OTHER"


# --------------------------------------------------------------------------
# Lead Model
# --------------------------------------------------------------------------
class Lead(Base):
    """
    Represents a prospective client (sales lead) tracked within the CRM
    pipeline, from initial acquisition through to booking or loss.

    This model is the authoritative record used for:
        - Sales pipeline tracking (status, priority)
        - Agent assignment and accountability (assigned_agent, creator)
        - Follow-up scheduling (next_follow_up)
        - Marketing channel attribution (lead_source)
        - Auditing (created_at / updated_at trail)

    Table: leads
    """

    __tablename__ = "leads"

    __table_args__ = (
        Index("ix_leads_status_priority", "status", "priority"),
        Index("ix_leads_assigned_agent_id_status", "assigned_agent_id", "status"),
    )

    # ----------------------------------------------------------------
    # Primary Key
    # ----------------------------------------------------------------
    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        doc=(
            "Globally unique primary key for the lead record. "
            "Requires the PostgreSQL `pgcrypto` extension to be "
            "enabled for `gen_random_uuid()` to be available."
        ),
    )

    # ----------------------------------------------------------------
    # Contact / Profile Fields
    # ----------------------------------------------------------------
    full_name: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
        index=True,
        doc="Full name of the prospective client.",
    )

    phone: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        index=True,
        doc="Primary contact phone number for the lead.",
    )

    email: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        index=True,
        doc="Optional contact email address for the lead.",
    )

    # ----------------------------------------------------------------
    # Requirement / Interest Fields
    # ----------------------------------------------------------------
    budget: Mapped[Optional[float]] = mapped_column(
        Numeric(14, 2),
        nullable=True,
        doc="Prospective client's stated budget, in local currency units.",
    )

    property_type: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        doc="Type of property the lead is interested in (e.g., Apartment, Villa, Plot).",
    )

    preferred_location: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        doc="Preferred locality/area for the property search.",
    )

    bhk: Mapped[Optional[str]] = mapped_column(
        String(20),
        nullable=True,
        doc="Preferred configuration (e.g., '2BHK', '3BHK').",
    )

    # ----------------------------------------------------------------
    # Pipeline Classification Fields
    # ----------------------------------------------------------------
    lead_source: Mapped[LeadSource] = mapped_column(
        SAEnum(
            LeadSource,
            name="lead_source",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
        default=LeadSource.OTHER,
        server_default=LeadSource.OTHER.value,
        index=True,
        doc="Acquisition channel through which the lead was captured.",
    )

    status: Mapped[LeadStatus] = mapped_column(
        SAEnum(
            LeadStatus,
            name="lead_status",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
        default=LeadStatus.NEW,
        server_default=LeadStatus.NEW.value,
        index=True,
        doc="Current stage of the lead within the sales pipeline.",
    )

    priority: Mapped[LeadPriority] = mapped_column(
        SAEnum(
            LeadPriority,
            name="lead_priority",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
        default=LeadPriority.MEDIUM,
        server_default=LeadPriority.MEDIUM.value,
        index=True,
        doc="Urgency/priority level assigned to the lead.",
    )

    # ----------------------------------------------------------------
    # Ownership / Accountability Fields
    #
    # NOTE: `assigned_agent_id` and `created_by` are declared as
    # `Integer` to exactly match `User.id` as currently defined in
    # `app/models/user.py` (`id: Mapped[int] = mapped_column(Integer,
    # primary_key=True, autoincrement=True, ...)`). If `User.id` is
    # ever migrated to a UUID primary key, these two columns AND their
    # `ForeignKey("users.id")` targets must be updated in lockstep, or
    # the foreign key constraints will fail at migration time.
    # ----------------------------------------------------------------
    assigned_agent_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Internal ID of the User (sales agent) currently assigned to this lead.",
    )

    created_by: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Internal ID of the User who created this lead record.",
    )

    # ----------------------------------------------------------------
    # Follow-up / Notes Fields
    # ----------------------------------------------------------------
    next_follow_up: Mapped[Optional[date]] = mapped_column(
        Date,
        nullable=True,
        index=True,
        doc="Scheduled date for the next follow-up action with this lead.",
    )

    remarks: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Free-form notes/remarks recorded by agents about this lead.",
    )

    # ----------------------------------------------------------------
    # Status Flags
    # ----------------------------------------------------------------
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        doc="Soft-disable flag; inactive leads are excluded from active pipelines.",
    )

    # ----------------------------------------------------------------
    # Audit Timestamps (Timezone-Aware, UTC)
    # ----------------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="UTC timestamp when the lead record was created.",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        doc="UTC timestamp when the lead record was last updated.",
    )

    # ----------------------------------------------------------------
    # Relationships
    # ----------------------------------------------------------------
    # `lazy="raise_on_sql"` (not "selectin"): `LeadRepository._base_select()`
    # already documents and implements conditional eager loading via its
    # `with_relationships` flag — `selectinload(Lead.assigned_agent)` /
    # `selectinload(Lead.creator)` are only attached when the caller passes
    # `with_relationships=True`. With a "selectin" default on the model
    # itself, that flag was actually a no-op: the mapped default eagerly
    # loaded both relationships on every single Lead query regardless of
    # the flag, silently defeating the repository's own N+1-avoidance
    # design for bulk/list/search paths. `raise_on_sql` makes the model
    # loading-neutral by default so the repository's explicit
    # `selectinload()` calls become the sole, correctly-conditional
    # loading path; any access attempted without eager-loading raises an
    # immediate, clear error instead of either a silent extra query or an
    # async `MissingGreenlet` failure.
    assigned_agent: Mapped[Optional[User]] = relationship(
        User,
        foreign_keys=[assigned_agent_id],
        lazy="raise_on_sql",
        doc="The User (sales agent) currently assigned to this lead.",
    )

    creator: Mapped[Optional[User]] = relationship(
        User,
        foreign_keys=[created_by],
        lazy="raise_on_sql",
        doc="The User who originally created this lead record.",
    )

    # ----------------------------------------------------------------
    # Developer Ergonomics
    # ----------------------------------------------------------------
    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return (
            f"<Lead id={self.id} full_name={self.full_name!r} "
            f"status={self.status.value} priority={self.priority.value}>"
        )


__all__ = ["Lead", "LeadStatus", "LeadPriority", "LeadSource"]