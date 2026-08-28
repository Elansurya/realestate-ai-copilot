"""
Property Schemas
==================

Pydantic v2 schemas for the Property Management module.

Follows the same conventions established in `app/schemas/user.py`:
- `ConfigDict(from_attributes=True)` on response schemas for ORM compatibility
- UUID as the public-facing identifier (never expose internal integer `id`)
- Explicit field-level constraints via `Field(...)`
- `field_validator` / `model_validator` for cross-field and normalization rules
- Separate Create / Update / Response / action-specific schemas per resource
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from app.models.property import (
    FurnishingType,
    ListingType,
    PropertyStatus,
    PropertyType,
)

# --------------------------------------------------------------------------
# Shared constants
# --------------------------------------------------------------------------

PINCODE_LENGTH = 6  # India-specific 6-digit postal code


# --------------------------------------------------------------------------
# Base
# --------------------------------------------------------------------------


class PropertyBase(BaseModel):
    """Fields common to property create/update payloads."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., min_length=3, max_length=255)
    description: Optional[str] = Field(default=None, max_length=5000)

    property_type: PropertyType
    listing_type: ListingType
    furnishing: Optional[FurnishingType] = None

    price: Decimal = Field(..., ge=0, decimal_places=2)
    area_sqft: Decimal = Field(..., gt=0, decimal_places=2)

    bedrooms: Optional[int] = Field(default=None, ge=0, le=50)
    bathrooms: Optional[int] = Field(default=None, ge=0, le=50)
    parking: Optional[int] = Field(default=0, ge=0, le=50)

    address: str = Field(..., min_length=5, max_length=500)
    city: str = Field(..., min_length=2, max_length=100)
    state: str = Field(..., min_length=2, max_length=100)
    pincode: str = Field(..., min_length=PINCODE_LENGTH, max_length=10)
    latitude: Optional[Decimal] = Field(default=None, ge=-90, le=90)
    longitude: Optional[Decimal] = Field(default=None, ge=-180, le=180)

    owner_name: str = Field(..., min_length=2, max_length=150)
    owner_phone: str = Field(..., min_length=7, max_length=20)
    owner_email: Optional[EmailStr] = None

    is_featured: bool = False

    @field_validator("pincode")
    @classmethod
    def validate_pincode(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError("pincode must contain digits only")
        if len(value) != PINCODE_LENGTH:
            raise ValueError(f"pincode must be exactly {PINCODE_LENGTH} digits")
        return value

    @field_validator("owner_phone")
    @classmethod
    def validate_owner_phone(cls, value: str) -> str:
        cleaned = value.replace(" ", "").replace("-", "")
        digits = cleaned[1:] if cleaned.startswith("+") else cleaned
        if not digits.isdigit():
            raise ValueError("owner_phone must contain only digits (with optional leading +)")
        if not (7 <= len(digits) <= 15):
            raise ValueError("owner_phone must be between 7 and 15 digits")
        return cleaned

    @field_validator("city", "state")
    @classmethod
    def validate_alpha_location(cls, value: str) -> str:
        if not value.replace(" ", "").isalpha():
            raise ValueError("must contain alphabetic characters only")
        return value.title()


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------


class PropertyCreate(PropertyBase):
    """Payload for creating a new property listing."""

    property_code: Optional[str] = Field(
        default=None,
        max_length=32,
        description="Optional client-supplied code; auto-generated if omitted.",
    )
    assigned_agent_id: Optional[int] = Field(
        default=None,
        description="Internal user id of the agent to assign at creation time.",
    )
    property_status: PropertyStatus = PropertyStatus.AVAILABLE

    @model_validator(mode="after")
    def validate_residential_fields(self) -> "PropertyCreate":
        residential_types = {
            PropertyType.APARTMENT,
            PropertyType.VILLA,
            PropertyType.INDEPENDENT_HOUSE,
            PropertyType.PENTHOUSE,
            PropertyType.STUDIO,
        }
        if self.property_type in residential_types and self.bedrooms is None:
            raise ValueError("bedrooms is required for residential property types")
        return self


# --------------------------------------------------------------------------
# Update
# --------------------------------------------------------------------------


class PropertyUpdate(BaseModel):
    """Payload for partially updating an existing property. All fields optional."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: Optional[str] = Field(default=None, min_length=3, max_length=255)
    description: Optional[str] = Field(default=None, max_length=5000)

    property_type: Optional[PropertyType] = None
    listing_type: Optional[ListingType] = None
    furnishing: Optional[FurnishingType] = None

    price: Optional[Decimal] = Field(default=None, ge=0, decimal_places=2)
    area_sqft: Optional[Decimal] = Field(default=None, gt=0, decimal_places=2)

    bedrooms: Optional[int] = Field(default=None, ge=0, le=50)
    bathrooms: Optional[int] = Field(default=None, ge=0, le=50)
    parking: Optional[int] = Field(default=None, ge=0, le=50)

    address: Optional[str] = Field(default=None, min_length=5, max_length=500)
    city: Optional[str] = Field(default=None, min_length=2, max_length=100)
    state: Optional[str] = Field(default=None, min_length=2, max_length=100)
    pincode: Optional[str] = Field(default=None, min_length=PINCODE_LENGTH, max_length=10)
    latitude: Optional[Decimal] = Field(default=None, ge=-90, le=90)
    longitude: Optional[Decimal] = Field(default=None, ge=-180, le=180)

    owner_name: Optional[str] = Field(default=None, min_length=2, max_length=150)
    owner_phone: Optional[str] = Field(default=None, min_length=7, max_length=20)
    owner_email: Optional[EmailStr] = None

    is_featured: Optional[bool] = None
    is_active: Optional[bool] = None

    @field_validator("pincode")
    @classmethod
    def validate_pincode(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if not value.isdigit():
            raise ValueError("pincode must contain digits only")
        if len(value) != PINCODE_LENGTH:
            raise ValueError(f"pincode must be exactly {PINCODE_LENGTH} digits")
        return value

    @model_validator(mode="after")
    def validate_at_least_one_field(self) -> "PropertyUpdate":
        if not self.model_dump(exclude_unset=True):
            raise ValueError("at least one field must be provided for update")
        return self


# --------------------------------------------------------------------------
# Action-specific schemas
# --------------------------------------------------------------------------


class PropertyStatusUpdate(BaseModel):
    """Payload for updating only the property_status."""

    property_status: PropertyStatus
    reason: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Optional note explaining the status change (for audit trail).",
    )


class PropertyAssignment(BaseModel):
    """Payload for assigning (or reassigning) a property to an agent."""

    assigned_agent_id: int = Field(..., gt=0)
    reason: Optional[str] = Field(default=None, max_length=500)


# --------------------------------------------------------------------------
# Response
# --------------------------------------------------------------------------


class PropertyResponse(BaseModel):
    """Public representation of a property returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    uuid: uuid.UUID
    property_code: str

    title: str
    description: Optional[str] = None

    property_type: PropertyType
    property_status: PropertyStatus
    listing_type: ListingType
    furnishing: Optional[FurnishingType] = None

    price: Decimal
    area_sqft: Decimal

    bedrooms: Optional[int] = None
    bathrooms: Optional[int] = None
    parking: Optional[int] = None

    address: str
    city: str
    state: str
    pincode: str
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None

    assigned_agent_id: Optional[int] = None

    owner_name: str
    owner_phone: str
    owner_email: Optional[EmailStr] = None

    is_featured: bool
    is_active: bool

    created_at: datetime
    updated_at: datetime


class PaginatedPropertyResponse(BaseModel):
    """Paginated wrapper for property list endpoints."""

    model_config = ConfigDict(from_attributes=True)

    items: list[PropertyResponse]
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1)
    total_pages: int = Field(..., ge=0)

    @model_validator(mode="after")
    def validate_total_pages(self) -> "PaginatedPropertyResponse":
        expected = (self.total + self.page_size - 1) // self.page_size if self.total else 0
        if self.total_pages != expected:
            raise ValueError("total_pages does not match total/page_size calculation")
        return self