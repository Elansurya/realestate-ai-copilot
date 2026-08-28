"""
backend/app/schemas/lead.py

Pydantic v2 schemas for the Lead module.

Responsibilities:
    - Define request/response contracts for lead creation, partial
      updates, single-record retrieval, paginated listing, and
      search/filter query parameters.

Design Notes:
    - These schemas are transport/validation contracts only; they
      contain no business logic or database access.
    - `LeadStatus`, `LeadPriority`, and `LeadSource` are imported
      directly from `app.models.lead` and reused as-is, so API-facing
      enum values always stay in lockstep with the ORM/DB enum
      definitions with zero duplication.
    - `ConfigDict(from_attributes=True)` is used on all response
      schemas so they can be constructed directly from `Lead` ORM
      instances without manual dict conversion.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
)

from app.models.lead import LeadPriority, LeadSource, LeadStatus

# --------------------------------------------------------------------------
# Shared Validation Constants
# --------------------------------------------------------------------------
_INDIAN_MOBILE_REGEX = r"^(\+91[\-\s]?)?[6-9]\d{9}$"

_ALLOWED_SORT_FIELDS = frozenset(
    {
        "full_name",
        "budget",
        "status",
        "priority",
        "lead_source",
        "next_follow_up",
        "created_at",
        "updated_at",
    }
)


# --------------------------------------------------------------------------
# Lead Base Schema
# --------------------------------------------------------------------------
class LeadBase(BaseModel):
    """
    Common fields shared across lead creation, update, and response
    schemas. Contains no identity or audit fields.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    full_name: str = Field(
        ...,
        description="Full name of the prospective client.",
        examples=["Rahul Sharma"],
    )
    phone: str = Field(
        ...,
        description="Primary contact phone number for the lead.",
        examples=["+91-9876543210"],
    )
    email: Optional[EmailStr] = Field(
        default=None,
        description="Optional contact email address for the lead.",
        examples=["rahul.sharma@example.com"],
    )
    budget: Optional[Decimal] = Field(
        default=None,
        description="Prospective client's stated budget (must be >= 0).",
        examples=[7500000],
    )
    property_type: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Type of property the lead is interested in (e.g., Apartment, Villa, Plot).",
        examples=["Apartment"],
    )
    preferred_location: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Preferred locality/area for the property search.",
        examples=["Whitefield, Bangalore"],
    )
    bhk: Optional[str] = Field(
        default=None,
        max_length=20,
        description="Preferred configuration (e.g., '2BHK', '3BHK').",
        examples=["3BHK"],
    )
    lead_source: LeadSource = Field(
        default=LeadSource.OTHER,
        description="Acquisition channel through which the lead was captured.",
        examples=["WEBSITE"],
    )
    status: LeadStatus = Field(
        default=LeadStatus.NEW,
        description="Current stage of the lead within the sales pipeline.",
        examples=["NEW"],
    )
    priority: LeadPriority = Field(
        default=LeadPriority.MEDIUM,
        description="Urgency/priority level assigned to the lead.",
        examples=["MEDIUM"],
    )
    assigned_agent_id: Optional[int] = Field(
        default=None,
        description="Internal ID of the User (sales agent) assigned to this lead.",
        examples=[12],
    )
    next_follow_up: Optional[date] = Field(
        default=None,
        description="Scheduled date for the next follow-up action with this lead.",
        examples=["2026-08-01"],
    )
    remarks: Optional[str] = Field(
        default=None,
        description="Free-form notes/remarks recorded by agents about this lead.",
        examples=["Interested in a 3BHK, prefers ready-to-move properties."],
    )
    is_active: bool = Field(
        default=True,
        description="Soft-disable flag; inactive leads are excluded from active pipelines.",
    )


# --------------------------------------------------------------------------
# Lead Create Schema
# --------------------------------------------------------------------------
class LeadCreate(LeadBase):
    """
    Request payload for creating a new lead.

    Overrides `full_name` and `phone` from `LeadBase` to enforce
    stricter creation-time constraints (length bounds, phone format).
    """

    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "full_name": "Rahul Sharma",
                "phone": "+91-9876543210",
                "email": "rahul.sharma@example.com",
                "budget": 7500000,
                "property_type": "Apartment",
                "preferred_location": "Whitefield, Bangalore",
                "bhk": "3BHK",
                "lead_source": "WEBSITE",
                "status": "NEW",
                "priority": "MEDIUM",
                "assigned_agent_id": 12,
                "next_follow_up": "2026-08-01",
                "remarks": "Interested in a 3BHK, prefers ready-to-move properties.",
                "is_active": True,
            }
        },
    )

    full_name: Annotated[
        str,
        Field(
            ...,
            min_length=3,
            max_length=150,
            description="Full name of the prospective client (3-150 characters).",
            examples=["Rahul Sharma"],
        ),
    ]
    phone: Annotated[
        str,
        Field(
            ...,
            description="Indian mobile number, with or without a '+91' prefix.",
            examples=["+91-9876543210"],
        ),
    ]
    budget: Optional[Decimal] = Field(
        default=None,
        ge=0,
        description="Prospective client's stated budget; must be >= 0 if provided.",
        examples=[7500000],
    )

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        """
        Validate that `phone` matches a valid Indian mobile number,
        optionally prefixed with '+91', a hyphen, or a space, and
        beginning with a digit from 6-9 (standard Indian mobile range).

        Args:
            value: The raw phone number string supplied by the client.

        Returns:
            The validated phone number string, unchanged.

        Raises:
            ValueError: If the phone number does not match the expected
                Indian mobile number format.
        """
        import re

        if not re.match(_INDIAN_MOBILE_REGEX, value):
            raise ValueError(
                "Phone number must be a valid Indian mobile number "
                "(10 digits starting with 6-9, optionally prefixed with '+91')."
            )
        return value


# --------------------------------------------------------------------------
# Lead Update Schema
# --------------------------------------------------------------------------
class LeadUpdate(BaseModel):
    """
    Request payload for partially updating an existing lead (PATCH
    semantics). Every field is optional; only supplied fields are
    intended to be applied by the service layer.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    full_name: Optional[str] = Field(
        default=None,
        min_length=3,
        max_length=150,
        description="Full name of the prospective client.",
    )
    phone: Optional[str] = Field(
        default=None,
        description="Indian mobile number, with or without a '+91' prefix.",
    )
    email: Optional[EmailStr] = Field(
        default=None,
        description="Optional contact email address for the lead.",
    )
    budget: Optional[Decimal] = Field(
        default=None,
        ge=0,
        description="Prospective client's stated budget; must be >= 0 if provided.",
    )
    property_type: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Type of property the lead is interested in.",
    )
    preferred_location: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Preferred locality/area for the property search.",
    )
    bhk: Optional[str] = Field(
        default=None,
        max_length=20,
        description="Preferred configuration (e.g., '2BHK', '3BHK').",
    )
    lead_source: Optional[LeadSource] = Field(
        default=None,
        description="Acquisition channel through which the lead was captured.",
    )
    status: Optional[LeadStatus] = Field(
        default=None,
        description="Current stage of the lead within the sales pipeline.",
    )
    priority: Optional[LeadPriority] = Field(
        default=None,
        description="Urgency/priority level assigned to the lead.",
    )
    assigned_agent_id: Optional[int] = Field(
        default=None,
        description="Internal ID of the User (sales agent) assigned to this lead.",
    )
    next_follow_up: Optional[date] = Field(
        default=None,
        description="Scheduled date for the next follow-up action with this lead.",
    )
    remarks: Optional[str] = Field(
        default=None,
        description="Free-form notes/remarks recorded by agents about this lead.",
    )
    is_active: Optional[bool] = Field(
        default=None,
        description="Soft-disable flag; inactive leads are excluded from active pipelines.",
    )

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: Optional[str]) -> Optional[str]:
        """
        Validate `phone` against the Indian mobile number format when
        provided; skipped entirely when the field is omitted (None),
        consistent with PATCH semantics.

        Args:
            value: The raw phone number string, or None if not supplied.

        Returns:
            The validated phone number string, or None if not supplied.

        Raises:
            ValueError: If a non-null phone number does not match the
                expected Indian mobile number format.
        """
        import re

        if value is None:
            return value
        if not re.match(_INDIAN_MOBILE_REGEX, value):
            raise ValueError(
                "Phone number must be a valid Indian mobile number "
                "(10 digits starting with 6-9, optionally prefixed with '+91')."
            )
        return value


# --------------------------------------------------------------------------
# Lead Response Schema
# --------------------------------------------------------------------------
class LeadResponse(LeadBase):
    """
    Outward-facing representation of a lead, returned by lead retrieval
    and mutation endpoints. Extends `LeadBase` with identity, ownership,
    and audit fields populated by the persistence layer.
    """

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "full_name": "Rahul Sharma",
                "phone": "+91-9876543210",
                "email": "rahul.sharma@example.com",
                "budget": 7500000,
                "property_type": "Apartment",
                "preferred_location": "Whitefield, Bangalore",
                "bhk": "3BHK",
                "lead_source": "WEBSITE",
                "status": "NEW",
                "priority": "MEDIUM",
                "assigned_agent_id": 12,
                "next_follow_up": "2026-08-01",
                "remarks": "Interested in a 3BHK, prefers ready-to-move properties.",
                "is_active": True,
                "created_by": 5,
                "created_at": "2026-07-01T10:30:00Z",
                "updated_at": "2026-07-05T14:15:00Z",
            }
        },
    )

    id: uuid.UUID = Field(
        ...,
        description="Globally unique identifier of the lead record.",
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    )
    created_by: Optional[int] = Field(
        default=None,
        description="Internal ID of the User who created this lead record.",
        examples=[5],
    )
    created_at: datetime = Field(
        ...,
        description="UTC timestamp when the lead record was created.",
        examples=["2026-07-01T10:30:00Z"],
    )
    updated_at: datetime = Field(
        ...,
        description="UTC timestamp when the lead record was last updated.",
        examples=["2026-07-05T14:15:00Z"],
    )


# --------------------------------------------------------------------------
# Lead List Response Schema
# --------------------------------------------------------------------------
class LeadListResponse(BaseModel):
    """
    Paginated collection response for lead listing/search endpoints.
    """

    model_config = ConfigDict(from_attributes=True)

    items: list[LeadResponse] = Field(
        ...,
        description="The page of lead records matching the query.",
    )
    total: int = Field(
        ...,
        ge=0,
        description="Total number of lead records matching the query, across all pages.",
        examples=[142],
    )
    page: int = Field(
        ...,
        ge=1,
        description="Current page number (1-indexed).",
        examples=[1],
    )
    page_size: int = Field(
        ...,
        ge=1,
        description="Number of records requested per page.",
        examples=[20],
    )
    total_pages: int = Field(
        ...,
        ge=0,
        description="Total number of pages available for the given page_size.",
        examples=[8],
    )


# --------------------------------------------------------------------------
# Lead Filter Schema
# --------------------------------------------------------------------------
class LeadFilter(BaseModel):
    """
    Query parameters for searching and filtering leads, including
    pagination and sorting controls.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    status: Optional[LeadStatus] = Field(
        default=None,
        description="Filter by current pipeline status.",
    )
    priority: Optional[LeadPriority] = Field(
        default=None,
        description="Filter by priority level.",
    )
    lead_source: Optional[LeadSource] = Field(
        default=None,
        description="Filter by acquisition channel.",
    )
    assigned_agent_id: Optional[int] = Field(
        default=None,
        description="Filter by the assigned sales agent's internal User ID.",
    )
    property_type: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Filter by property type.",
    )
    preferred_location: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Filter by preferred location.",
    )
    phone: Optional[str] = Field(
        default=None,
        description="Filter by exact contact phone number.",
    )
    email: Optional[EmailStr] = Field(
        default=None,
        description="Filter by exact contact email address.",
    )
    search: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Free-text search term matched against name/phone/email/remarks.",
        examples=["Rahul"],
    )
    page: int = Field(
        default=1,
        ge=1,
        description="Page number to retrieve (1-indexed).",
        examples=[1],
    )
    page_size: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Number of records to return per page (max 100).",
        examples=[20],
    )
    sort_by: str = Field(
        default="created_at",
        description=(
            "Field to sort results by. Allowed values: "
            "full_name, budget, status, priority, lead_source, "
            "next_follow_up, created_at, updated_at."
        ),
        examples=["created_at"],
    )
    sort_order: str = Field(
        default="desc",
        description="Sort direction: 'asc' or 'desc'.",
        examples=["desc"],
    )

    @field_validator("search")
    @classmethod
    def validate_search(cls, value: Optional[str]) -> Optional[str]:
        """
        Reject a search term that is empty after whitespace stripping,
        since an effectively-blank search string would otherwise match
        every record and defeat the purpose of filtering.

        Args:
            value: The raw search string, or None if not supplied.

        Returns:
            The validated, stripped search string, or None.

        Raises:
            ValueError: If the search string is empty after stripping.
        """
        if value is not None and not value.strip():
            raise ValueError("Search term must not be empty or whitespace only.")
        return value

    @field_validator("sort_by")
    @classmethod
    def validate_sort_by(cls, value: str) -> str:
        """
        Validate that `sort_by` references an allowed, indexable column
        to prevent arbitrary/unsafe field references reaching the query
        layer.

        Args:
            value: The requested sort field name.

        Returns:
            The validated sort field name.

        Raises:
            ValueError: If the field is not in the allowed sort fields.
        """
        if value not in _ALLOWED_SORT_FIELDS:
            allowed = ", ".join(sorted(_ALLOWED_SORT_FIELDS))
            raise ValueError(f"sort_by must be one of: {allowed}.")
        return value

    @field_validator("sort_order")
    @classmethod
    def validate_sort_order(cls, value: str) -> str:
        """
        Validate that `sort_order` is either 'asc' or 'desc'
        (case-insensitive), normalizing the result to lowercase.

        Args:
            value: The requested sort direction.

        Returns:
            The normalized sort direction ('asc' or 'desc').

        Raises:
            ValueError: If the value is not 'asc' or 'desc'.
        """
        normalized = value.strip().lower()
        if normalized not in {"asc", "desc"}:
            raise ValueError("sort_order must be either 'asc' or 'desc'.")
        return normalized


__all__ = [
    "LeadBase",
    "LeadCreate",
    "LeadUpdate",
    "LeadResponse",
    "LeadListResponse",
    "LeadFilter",
]