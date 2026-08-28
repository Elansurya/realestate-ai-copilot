"""
backend/app/schemas/booking.py

Pydantic v2 schemas for the Booking module.

Responsibilities:
    - Define request/response contracts for booking creation, partial
      updates, single-record retrieval, paginated listing, and
      search/filter query parameters.

Design Notes:
    - These schemas are transport/validation contracts only; they
      contain no business logic or database access.
    - `BookingStatus`, `BookingPaymentStatus`, and `BookingPaymentMode`
      are imported directly from `app.models.booking` and reused as-is,
      so API-facing enum values always stay in lockstep with the
      ORM/DB enum definitions with zero duplication (mirrors
      `app/schemas/lead.py`'s handling of `LeadStatus` etc.).
    - `ConfigDict(from_attributes=True)` is used on all response
      schemas so they can be constructed directly from `Booking` ORM
      instances without manual dict conversion.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.booking import (
    BookingPaymentMode,
    BookingPaymentStatus,
    BookingStatus,
)

# --------------------------------------------------------------------------
# Shared Validation Constants
# --------------------------------------------------------------------------
_ALLOWED_SORT_FIELDS = frozenset(
    {
        "booking_date",
        "booking_amount",
        "token_amount",
        "status",
        "payment_status",
        "next_follow_up",
        "created_at",
        "updated_at",
    }
)


# --------------------------------------------------------------------------
# Booking Base Schema
# --------------------------------------------------------------------------
class BookingBase(BaseModel):
    """
    Common fields shared across booking creation, update, and response
    schemas. Contains no identity or audit fields.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    customer_id: uuid.UUID = Field(
        ...,
        description="The Customer who is booking the property.",
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    )
    property_id: int = Field(
        ...,
        description="Internal integer ID of the Property being booked.",
        examples=[101],
    )
    lead_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Optional reference to the originating Lead record.",
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    )
    agent_id: Optional[int] = Field(
        default=None,
        description="Internal ID of the User (sales agent) handling this booking.",
        examples=[12],
    )
    booking_date: date = Field(
        default_factory=date.today,
        description="Date on which the booking was made.",
        examples=["2026-07-31"],
    )
    booking_amount: Optional[Decimal] = Field(
        default=None,
        description="Total agreed booking value for the property (must be >= 0).",
        examples=[7500000],
    )
    token_amount: Optional[Decimal] = Field(
        default=None,
        description="Token/advance amount collected (must be >= 0).",
        examples=[500000],
    )
    payment_mode: Optional[BookingPaymentMode] = Field(
        default=None,
        description="Channel through which the token/booking amount was collected.",
        examples=["UPI"],
    )
    payment_reference: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Transaction/cheque/UTR reference number for the payment, if any.",
        examples=["UTR2026073112345"],
    )
    status: BookingStatus = Field(
        default=BookingStatus.PENDING,
        description="Current lifecycle stage of the booking.",
        examples=["PENDING"],
    )
    payment_status: BookingPaymentStatus = Field(
        default=BookingPaymentStatus.PENDING,
        description="Current payment state of the booking's token/booking amount.",
        examples=["PENDING"],
    )
    site_visit_date: Optional[date] = Field(
        default=None,
        description="Date of the site visit that led to (or is tied to) this booking.",
        examples=["2026-07-20"],
    )
    next_follow_up: Optional[date] = Field(
        default=None,
        description="Scheduled date for the next follow-up action on this booking.",
        examples=["2026-08-05"],
    )
    remarks: Optional[str] = Field(
        default=None,
        description="Free-form notes/remarks recorded by agents about this booking.",
        examples=["Customer requested a payment plan for the balance amount."],
    )
    cancellation_reason: Optional[str] = Field(
        default=None,
        description="Reason recorded when a booking's status is set to CANCELLED.",
    )
    is_active: bool = Field(
        default=True,
        description="Soft-disable flag; inactive bookings are excluded from active views.",
    )


# --------------------------------------------------------------------------
# Booking Create Schema
# --------------------------------------------------------------------------
class BookingCreate(BookingBase):
    """
    Request payload for creating a new booking.

    Overrides `booking_amount` and `token_amount` from `BookingBase` to
    enforce stricter creation-time constraints (non-negative values).
    """

    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "customer_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "property_id": 101,
                "lead_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "agent_id": 12,
                "booking_date": "2026-07-31",
                "booking_amount": 7500000,
                "token_amount": 500000,
                "payment_mode": "UPI",
                "payment_reference": "UTR2026073112345",
                "status": "PENDING",
                "payment_status": "PENDING",
                "site_visit_date": "2026-07-20",
                "next_follow_up": "2026-08-05",
                "remarks": "Customer requested a payment plan for the balance amount.",
                "is_active": True,
            }
        },
    )

    booking_amount: Optional[Decimal] = Field(
        default=None,
        ge=0,
        description="Total agreed booking value; must be >= 0 if provided.",
        examples=[7500000],
    )
    token_amount: Optional[Decimal] = Field(
        default=None,
        ge=0,
        description="Token/advance amount collected; must be >= 0 if provided.",
        examples=[500000],
    )

    @field_validator("token_amount")
    @classmethod
    def validate_token_amount(
        cls, value: Optional[Decimal], info
    ) -> Optional[Decimal]:
        """
        Validate that `token_amount`, when supplied alongside a
        `booking_amount`, does not exceed the total booking amount.

        Args:
            value: The raw token amount, or None if not supplied.
            info: Pydantic validation context, used to read the
                already-validated `booking_amount` sibling field.

        Returns:
            The validated token amount, unchanged.

        Raises:
            ValueError: If `token_amount` exceeds `booking_amount`.
        """
        booking_amount = info.data.get("booking_amount")
        if value is not None and booking_amount is not None and value > booking_amount:
            raise ValueError("token_amount must not exceed booking_amount.")
        return value


# --------------------------------------------------------------------------
# Booking Update Schema
# --------------------------------------------------------------------------
class BookingUpdate(BaseModel):
    """
    Request payload for partially updating an existing booking (PATCH
    semantics). Every field is optional; only supplied fields are
    intended to be applied by the service layer.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    agent_id: Optional[int] = Field(
        default=None,
        description="Internal ID of the User (sales agent) handling this booking.",
    )
    booking_date: Optional[date] = Field(
        default=None,
        description="Date on which the booking was made.",
    )
    booking_amount: Optional[Decimal] = Field(
        default=None,
        ge=0,
        description="Total agreed booking value; must be >= 0 if provided.",
    )
    token_amount: Optional[Decimal] = Field(
        default=None,
        ge=0,
        description="Token/advance amount collected; must be >= 0 if provided.",
    )
    payment_mode: Optional[BookingPaymentMode] = Field(
        default=None,
        description="Channel through which the token/booking amount was collected.",
    )
    payment_reference: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Transaction/cheque/UTR reference number for the payment, if any.",
    )
    status: Optional[BookingStatus] = Field(
        default=None,
        description="Current lifecycle stage of the booking.",
    )
    payment_status: Optional[BookingPaymentStatus] = Field(
        default=None,
        description="Current payment state of the booking's token/booking amount.",
    )
    site_visit_date: Optional[date] = Field(
        default=None,
        description="Date of the site visit that led to (or is tied to) this booking.",
    )
    next_follow_up: Optional[date] = Field(
        default=None,
        description="Scheduled date for the next follow-up action on this booking.",
    )
    remarks: Optional[str] = Field(
        default=None,
        description="Free-form notes/remarks recorded by agents about this booking.",
    )
    cancellation_reason: Optional[str] = Field(
        default=None,
        description="Reason recorded when a booking's status is set to CANCELLED.",
    )
    is_active: Optional[bool] = Field(
        default=None,
        description="Soft-disable flag; inactive bookings are excluded from active views.",
    )


# --------------------------------------------------------------------------
# Booking Response Schema
# --------------------------------------------------------------------------
class BookingResponse(BookingBase):
    """
    Outward-facing representation of a booking, returned by booking
    retrieval and mutation endpoints. Extends `BookingBase` with
    identity, ownership, and audit fields populated by the persistence
    layer.
    """

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "customer_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "property_id": 101,
                "lead_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "agent_id": 12,
                "booking_date": "2026-07-31",
                "booking_amount": 7500000,
                "token_amount": 500000,
                "payment_mode": "UPI",
                "payment_reference": "UTR2026073112345",
                "status": "PENDING",
                "payment_status": "PENDING",
                "site_visit_date": "2026-07-20",
                "next_follow_up": "2026-08-05",
                "remarks": "Customer requested a payment plan for the balance amount.",
                "cancellation_reason": None,
                "is_active": True,
                "created_by": 5,
                "created_at": "2026-07-31T10:30:00Z",
                "updated_at": "2026-07-31T10:30:00Z",
            }
        },
    )

    id: uuid.UUID = Field(
        ...,
        description="Globally unique identifier of the booking record.",
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    )
    created_by: Optional[int] = Field(
        default=None,
        description="Internal ID of the User who created this booking record.",
        examples=[5],
    )
    created_at: datetime = Field(
        ...,
        description="UTC timestamp when the booking record was created.",
        examples=["2026-07-31T10:30:00Z"],
    )
    updated_at: datetime = Field(
        ...,
        description="UTC timestamp when the booking record was last updated.",
        examples=["2026-07-31T10:30:00Z"],
    )


# --------------------------------------------------------------------------
# Booking List Response Schema
# --------------------------------------------------------------------------
class BookingListResponse(BaseModel):
    """
    Paginated collection response for booking listing/search endpoints.
    """

    model_config = ConfigDict(from_attributes=True)

    items: list[BookingResponse] = Field(
        ...,
        description="The page of booking records matching the query.",
    )
    total: int = Field(
        ...,
        ge=0,
        description="Total number of booking records matching the query, across all pages.",
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
# Booking Filter Schema
# --------------------------------------------------------------------------
class BookingFilter(BaseModel):
    """
    Query parameters for searching and filtering bookings, including
    pagination and sorting controls.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    status: Optional[BookingStatus] = Field(
        default=None,
        description="Filter by current lifecycle status.",
    )
    payment_status: Optional[BookingPaymentStatus] = Field(
        default=None,
        description="Filter by current payment status.",
    )
    customer_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Filter by the booking's Customer ID.",
    )
    property_id: Optional[int] = Field(
        default=None,
        description="Filter by the booking's Property ID.",
    )
    lead_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Filter by the originating Lead ID.",
    )
    agent_id: Optional[int] = Field(
        default=None,
        description="Filter by the assigned sales agent's internal User ID.",
    )
    booking_date_from: Optional[date] = Field(
        default=None,
        description="Lower bound (inclusive) on `booking_date`.",
    )
    booking_date_to: Optional[date] = Field(
        default=None,
        description="Upper bound (inclusive) on `booking_date`.",
    )
    search: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Free-text search term matched against remarks/payment_reference.",
        examples=["UTR2026"],
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
            "booking_date, booking_amount, token_amount, status, "
            "payment_status, next_follow_up, created_at, updated_at."
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
    "BookingBase",
    "BookingCreate",
    "BookingUpdate",
    "BookingResponse",
    "BookingListResponse",
    "BookingFilter",
]