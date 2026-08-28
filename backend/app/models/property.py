"""
Property Model
===============

SQLAlchemy 2.x ORM model representing a real estate property listing.

Part of Milestone 5 — Property Management Module.

Follows the same conventions established in previous milestones:
- Async SQLAlchemy 2.x declarative mapping (Mapped / mapped_column)
- UUID as public-facing identifier, integer PK for internal FK performance
- Enum-backed status/type columns for data integrity
- Indexes on frequently filtered/sorted columns
- Server-side timestamps for created_at / updated_at
"""

import enum
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from uuid import UUID as PyUUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
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

if TYPE_CHECKING:
    from app.models.payment import Payment
    from app.models.user import User


class PropertyType(str, enum.Enum):
    """Type/category of the property."""

    APARTMENT = "apartment"
    VILLA = "villa"
    INDEPENDENT_HOUSE = "independent_house"
    PLOT = "plot"
    COMMERCIAL_OFFICE = "commercial_office"
    COMMERCIAL_SHOP = "commercial_shop"
    WAREHOUSE = "warehouse"
    AGRICULTURAL_LAND = "agricultural_land"
    PENTHOUSE = "penthouse"
    STUDIO = "studio"


class PropertyStatus(str, enum.Enum):
    """Current lifecycle status of the property listing."""

    AVAILABLE = "available"
    UNDER_NEGOTIATION = "under_negotiation"
    SOLD = "sold"
    RENTED = "rented"
    ON_HOLD = "on_hold"
    WITHDRAWN = "withdrawn"


class ListingType(str, enum.Enum):
    """Whether the property is listed for sale or for rent."""

    SALE = "sale"
    RENT = "rent"


class FurnishingType(str, enum.Enum):
    """Furnishing state of the property."""

    FURNISHED = "furnished"
    SEMI_FURNISHED = "semi_furnished"
    UNFURNISHED = "unfurnished"


class Property(Base):
    """
    Represents a real estate property listing managed within the CRM.

    Ownership / contact details (owner_name, owner_phone, owner_email) are
    stored directly on the property record since, in this domain, a property
    owner is typically an external party (not a system User) supplied by the
    listing agent — distinct from `assigned_agent_id`, which references the
    internal User responsible for managing the listing.
    """

    __tablename__ = "properties"

    # --- Identifiers ---------------------------------------------------
    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    uuid: Mapped[PyUUID] = mapped_column(
        PG_UUID(as_uuid=True),
        unique=True,
        nullable=False,
        default=uuid4,
        index=True,
    )
    property_code: Mapped[str] = mapped_column(
        String(32),
        unique=True,
        nullable=False,
        index=True,
        comment="Human-readable unique code, e.g. PROP-2026-00001",
    )

    # --- Core details ----------------------------------------------------
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    property_type: Mapped[PropertyType] = mapped_column(
        Enum(
            PropertyType,
            name="property_type_enum",
            native_enum=True,
            validate_strings=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        index=True,
    )
    property_status: Mapped[PropertyStatus] = mapped_column(
        Enum(
            PropertyStatus,
            name="property_status_enum",
            native_enum=True,
            validate_strings=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=PropertyStatus.AVAILABLE,
        server_default=PropertyStatus.AVAILABLE.value,
        index=True,
    )
    listing_type: Mapped[ListingType] = mapped_column(
        Enum(
            ListingType,
            name="listing_type_enum",
            native_enum=True,
            validate_strings=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        index=True,
    )
    furnishing: Mapped[FurnishingType | None] = mapped_column(
        Enum(
            FurnishingType,
            name="furnishing_type_enum",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            native_enum=True,
            validate_strings=True,
        ),
        nullable=True,
    )

    # --- Pricing & area ----------------------------------------------------
    price: Mapped[float] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        comment="Sale price or monthly/annual rent depending on listing_type",
    )
    area_sqft: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)

    # --- Specifications ---------------------------------------------------
    bedrooms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bathrooms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parking: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=0, server_default="0"
    )

    # --- Location ----------------------------------------------------------
    address: Mapped[str] = mapped_column(String(500), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    pincode: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    latitude: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    longitude: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)

    # --- Assignment ----------------------------------------------------
    assigned_agent_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # --- Owner (external party, not a system user) --------------------
    owner_name: Mapped[str] = mapped_column(String(150), nullable=False)
    owner_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    owner_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- Flags --------------------------------------------------------
    is_featured: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true", index=True
    )

    # --- Timestamps ----------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # --- Relationships ---------------------------------------------------
    # `Mapped[Optional["User"]]` matches `assigned_agent_id` being
    # nullable (a Property can be unassigned). `foreign_keys` is pinned
    # explicitly even though only one FK to `users.id` exists today, so
    # this relationship doesn't silently become ambiguous (and fail at
    # mapper-configuration time) if a second FK to `users` is ever added
    # to this table later (e.g. `created_by_id`).
    assigned_agent: Mapped[Optional["User"]] = relationship(
        "User",
        back_populates="assigned_properties",
        foreign_keys=[assigned_agent_id],
        lazy="selectin",
        doc=(
            "The User (agent) currently responsible for this listing. "
            "Eagerly loaded via selectin on every read, matching "
            "PropertyRepository's unconditional "
            "`selectinload(Property.assigned_agent)` — there is no "
            "'skip relationships' read path for Property, unlike Lead/"
            "Customer, so eager-by-default here introduces no redundant "
            "or wasted query relative to actual usage."
        ),
    )

    payments: Mapped[list["Payment"]] = relationship(
        "Payment",
        back_populates="property",
        lazy="selectin",
        doc="All Payment records associated with this property.",
    )

    # --- Table-level constraints & indexes ---------------------------------
    __table_args__ = (
        CheckConstraint("price >= 0", name="ck_properties_price_non_negative"),
        CheckConstraint(
            "area_sqft > 0", name="ck_properties_area_sqft_positive"
        ),
        CheckConstraint(
            "bedrooms IS NULL OR bedrooms >= 0",
            name="ck_properties_bedrooms_non_negative",
        ),
        CheckConstraint(
            "bathrooms IS NULL OR bathrooms >= 0",
            name="ck_properties_bathrooms_non_negative",
        ),
        CheckConstraint(
            "parking IS NULL OR parking >= 0",
            name="ck_properties_parking_non_negative",
        ),
        CheckConstraint(
            "latitude IS NULL OR (latitude >= -90 AND latitude <= 90)",
            name="ck_properties_latitude_range",
        ),
        CheckConstraint(
            "longitude IS NULL OR (longitude >= -180 AND longitude <= 180)",
            name="ck_properties_longitude_range",
        ),
        Index("ix_properties_city_state", "city", "state"),
        Index(
            "ix_properties_type_status_listing",
            "property_type",
            "property_status",
            "listing_type",
        ),
        Index("ix_properties_price", "price"),
        Index(
            "ix_properties_active_featured",
            "is_active",
            "is_featured",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Property id={self.id} code={self.property_code!r} "
            f"title={self.title!r} status={self.property_status.value}>"
        )