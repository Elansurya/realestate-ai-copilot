"""
backend/app/api/v1/booking.py

FastAPI router exposing the Booking module's HTTP endpoints.

Responsibilities:
    - Wire authenticated, role-restricted HTTP endpoints to
      `BookingService`.
    - Translate query/path/body parameters into service calls and
      service results into `BookingResponse` / `BookingListResponse`
      schemas.
    - Own all HTTP-facing concerns (status codes, Swagger metadata);
      contains no business logic itself (that lives in
      `BookingService`) and no direct database access (that lives in
      `BookingRepository`).

Design Notes:
    - Domain exceptions raised by `BookingService`
      (`NotFoundException`, `ConflictException`, `BadRequestException`)
      are translated to HTTP responses by the application's global
      exception handlers (the same handlers already registered for the
      Lead/Customer modules); this router does not catch them
      individually.
    - Static-path routes (`/dashboard/summary`, `/followups/today`)
      are declared BEFORE the dynamic `/{booking_id}` routes so
      FastAPI's path matching does not attempt to parse them as a
      booking UUID.
    - Access is restricted to Admin, Sales Manager, and Sales Agent
      roles via a router-level `require_roles` dependency; JWT
      authentication is enforced by `get_current_user`.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, require_roles
from app.models.booking import BookingPaymentStatus, BookingStatus
from app.models.user import User, UserRole
from app.schemas.booking import (
    BookingCreate,
    BookingFilter,
    BookingListResponse,
    BookingResponse,
    BookingUpdate,
)
from app.services.booking_service import BookingService

router = APIRouter(
    prefix="/bookings",
    tags=["Bookings"],
    dependencies=[
        Depends(
            require_roles(
                UserRole.ADMIN, UserRole.SALES_MANAGER, UserRole.SALES_AGENT
            )
        )
    ],
    responses={
        401: {"description": "Missing or invalid authentication credentials."},
        403: {"description": "Authenticated user lacks a permitted role."},
        422: {"description": "Request payload failed schema validation."},
        500: {"description": "Unexpected internal server error."},
    },
)


def get_booking_service(db: AsyncSession = Depends(get_db)) -> BookingService:
    """
    FastAPI dependency that constructs a request-scoped `BookingService`
    bound to the injected database session.

    Args:
        db: An active SQLAlchemy AsyncSession, injected via `get_db`.

    Returns:
        A `BookingService` instance ready to handle the current request.
    """
    return BookingService(db)


# ----------------------------------------------------------------
# Simple response payload schemas for status/payment-status/agent
# ----------------------------------------------------------------
from pydantic import BaseModel, Field  # noqa: E402


class BookingStatusUpdate(BaseModel):
    """Request payload for transitioning a booking's lifecycle status."""

    status: BookingStatus = Field(
        ..., description="The new lifecycle status to transition to.", examples=["CONFIRMED"]
    )


class BookingPaymentStatusUpdate(BaseModel):
    """Request payload for transitioning a booking's payment status."""

    payment_status: BookingPaymentStatus = Field(
        ..., description="The new payment status to transition to.", examples=["PARTIALLY_PAID"]
    )


class BookingAgentAssignment(BaseModel):
    """Request payload for assigning/reassigning a booking's sales agent."""

    agent_id: int = Field(
        ..., description="Internal User ID of the agent to assign.", examples=[12]
    )


class BookingDashboardSummary(BaseModel):
    """Aggregate dashboard summary of active bookings."""

    total_active_bookings: int = Field(..., examples=[142])
    status_breakdown: dict[str, int] = Field(
        ..., examples=[{"PENDING": 40, "CONFIRMED": 60, "COMPLETED": 30, "CANCELLED": 10, "REFUNDED": 2}]
    )
    payment_status_breakdown: dict[str, int] = Field(
        ..., examples=[{"PENDING": 20, "PARTIALLY_PAID": 50, "PAID": 60, "OVERDUE": 10, "REFUNDED": 2}]
    )
    total_booking_value: float = Field(..., examples=[750000000])
    total_token_collected: float = Field(..., examples=[50000000])
    pending_followups: int = Field(..., examples=[8])


# ----------------------------------------------------------------
# Create
# ----------------------------------------------------------------
@router.post(
    "",
    response_model=BookingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new booking",
    description=(
        "Creates a new booking for a customer against a property. "
        "Validates that the referenced customer, property, optional "
        "lead, and optional agent all exist, that the booking/token "
        "amounts are non-negative and consistent, and that no other "
        "active booking already exists for the same customer/property "
        "pair."
    ),
    responses={
        201: {"description": "Booking created successfully."},
        400: {"description": "Invalid booking/token amounts."},
        404: {"description": "Customer, property, lead, or agent not found."},
        409: {"description": "An active booking already exists for this customer/property."},
    },
)
async def create_booking(
    payload: BookingCreate,
    current_user: User = Depends(get_current_user),
    service: BookingService = Depends(get_booking_service),
) -> BookingResponse:
    booking = await service.create_booking(payload, current_user)
    return BookingResponse.model_validate(booking)


# ----------------------------------------------------------------
# Dashboard / Followups (static paths — declared before `/{booking_id}`)
# ----------------------------------------------------------------
@router.get(
    "/dashboard/summary",
    response_model=BookingDashboardSummary,
    status_code=status.HTTP_200_OK,
    summary="Get booking dashboard summary",
    description=(
        "Returns aggregate metrics across all active bookings: counts "
        "by lifecycle status and payment status, total booking value, "
        "total token amount collected, and the count of pending "
        "follow-ups."
    ),
    responses={200: {"description": "Dashboard summary computed successfully."}},
)
async def get_dashboard_summary(
    service: BookingService = Depends(get_booking_service),
) -> BookingDashboardSummary:
    summary = await service.dashboard_summary()
    return BookingDashboardSummary(**summary)


@router.get(
    "/followups/today",
    response_model=list[BookingResponse],
    status_code=status.HTTP_200_OK,
    summary="List today's due follow-ups",
    description=(
        "Returns all active bookings whose `next_follow_up` date is "
        "due today or earlier, ordered by `next_follow_up` ascending "
        "(most overdue first)."
    ),
    responses={200: {"description": "Follow-up list retrieved successfully."}},
)
async def get_todays_followups(
    service: BookingService = Depends(get_booking_service),
) -> list[BookingResponse]:
    bookings = await service.todays_followups()
    return [BookingResponse.model_validate(b) for b in bookings]


# ----------------------------------------------------------------
# List / Search
# ----------------------------------------------------------------
@router.get(
    "",
    response_model=BookingListResponse,
    status_code=status.HTTP_200_OK,
    summary="List and filter bookings",
    description=(
        "Returns a paginated, filterable, sortable list of bookings. "
        "Supports filtering by status, payment status, customer, "
        "property, lead, agent, booking-date range, and free-text "
        "search across remarks/payment reference."
    ),
    responses={200: {"description": "Bookings retrieved successfully."}},
)
async def list_bookings(
    filters: BookingFilter = Depends(),
    service: BookingService = Depends(get_booking_service),
) -> BookingListResponse:
    bookings, total = await service.list_bookings(filters)
    total_pages = (total + filters.page_size - 1) // filters.page_size if total else 0
    return BookingListResponse(
        items=[BookingResponse.model_validate(b) for b in bookings],
        total=total,
        page=filters.page,
        page_size=filters.page_size,
        total_pages=total_pages,
    )


@router.get(
    "/search",
    response_model=BookingListResponse,
    status_code=status.HTTP_200_OK,
    summary="Free-text search bookings",
    description=(
        "Performs a case-insensitive free-text search for bookings "
        "across `remarks` and `payment_reference`."
    ),
    responses={
        200: {"description": "Search results retrieved successfully."},
        400: {"description": "Search term is empty or whitespace only."},
    },
)
async def search_bookings(
    q: str = Query(
        ...,
        min_length=1,
        max_length=255,
        pattern=r".*\S.*",
        description="Search term. Must contain at least one non-whitespace character.",
    ),
    page: int = Query(1, ge=1, description="Page number (1-indexed)."),
    page_size: int = Query(20, ge=1, le=100, description="Records per page."),
    service: BookingService = Depends(get_booking_service),
) -> BookingListResponse:
    bookings, total = await service.search_bookings(q, page=page, page_size=page_size)
    total_pages = (total + page_size - 1) // page_size if total else 0
    return BookingListResponse(
        items=[BookingResponse.model_validate(b) for b in bookings],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


# ----------------------------------------------------------------
# Retrieve Single
# ----------------------------------------------------------------
@router.get(
    "/{booking_id}",
    response_model=BookingResponse,
    status_code=status.HTTP_200_OK,
    summary="Get a booking by ID",
    description="Retrieves a single booking record by its UUID.",
    responses={
        200: {"description": "Booking retrieved successfully."},
        404: {"description": "Booking not found."},
    },
)
async def get_booking(
    booking_id: uuid.UUID,
    service: BookingService = Depends(get_booking_service),
) -> BookingResponse:
    booking = await service.get_booking(booking_id)
    return BookingResponse.model_validate(booking)


# ----------------------------------------------------------------
# Update
# ----------------------------------------------------------------
@router.put(
    "/{booking_id}",
    response_model=BookingResponse,
    status_code=status.HTTP_200_OK,
    summary="Update a booking",
    description=(
        "Partially updates a booking. Only supplied fields are "
        "applied. Inactive bookings cannot be modified. If `status` "
        "or `payment_status` are included, they are validated against "
        "the allowed transition state machine."
    ),
    responses={
        200: {"description": "Booking updated successfully."},
        400: {"description": "Invalid amounts or invalid status transition."},
        404: {"description": "Booking or referenced agent not found."},
        409: {"description": "Booking is inactive and cannot be modified."},
    },
)
async def update_booking(
    booking_id: uuid.UUID,
    payload: BookingUpdate,
    service: BookingService = Depends(get_booking_service),
) -> BookingResponse:
    booking = await service.update_booking(booking_id, payload)
    return BookingResponse.model_validate(booking)


# ----------------------------------------------------------------
# Delete (Soft)
# ----------------------------------------------------------------
@router.delete(
    "/{booking_id}",
    response_model=BookingResponse,
    status_code=status.HTTP_200_OK,
    summary="Soft-delete a booking",
    description=(
        "Soft-deletes a booking by setting `is_active` to False. The "
        "record is preserved for audit/history purposes; no data is "
        "physically removed."
    ),
    responses={
        200: {"description": "Booking soft-deleted successfully."},
        404: {"description": "Booking not found."},
        409: {"description": "Booking is already inactive."},
    },
)
async def delete_booking(
    booking_id: uuid.UUID,
    service: BookingService = Depends(get_booking_service),
) -> BookingResponse:
    booking = await service.soft_delete_booking(booking_id)
    return BookingResponse.model_validate(booking)


# ----------------------------------------------------------------
# Status Transition
# ----------------------------------------------------------------
@router.patch(
    "/{booking_id}/status",
    response_model=BookingResponse,
    status_code=status.HTTP_200_OK,
    summary="Change a booking's lifecycle status",
    description=(
        "Transitions a booking's lifecycle status. Valid transitions: "
        "PENDING -> CONFIRMED -> COMPLETED (terminal), or "
        "PENDING -> CONFIRMED -> CANCELLED -> REFUNDED (terminal). "
        "Any other transition is rejected."
    ),
    responses={
        200: {"description": "Booking status updated successfully."},
        400: {"description": "Invalid status transition."},
        404: {"description": "Booking not found."},
        409: {"description": "Booking is inactive and cannot be modified."},
    },
)
async def change_booking_status(
    booking_id: uuid.UUID,
    payload: BookingStatusUpdate,
    service: BookingService = Depends(get_booking_service),
) -> BookingResponse:
    booking = await service.change_status(booking_id, payload.status)
    return BookingResponse.model_validate(booking)


# ----------------------------------------------------------------
# Payment Status Transition
# ----------------------------------------------------------------
@router.patch(
    "/{booking_id}/payment-status",
    response_model=BookingResponse,
    status_code=status.HTTP_200_OK,
    summary="Change a booking's payment status",
    description=(
        "Transitions a booking's payment status. Valid transitions: "
        "PENDING -> PARTIALLY_PAID -> PAID, with branches to OVERDUE "
        "or REFUNDED as applicable. Any other transition is rejected."
    ),
    responses={
        200: {"description": "Booking payment status updated successfully."},
        400: {"description": "Invalid payment status transition."},
        404: {"description": "Booking not found."},
        409: {"description": "Booking is inactive and cannot be modified."},
    },
)
async def change_booking_payment_status(
    booking_id: uuid.UUID,
    payload: BookingPaymentStatusUpdate,
    service: BookingService = Depends(get_booking_service),
) -> BookingResponse:
    booking = await service.change_payment_status(booking_id, payload.payment_status)
    return BookingResponse.model_validate(booking)


# ----------------------------------------------------------------
# Assign Agent
# ----------------------------------------------------------------
@router.patch(
    "/{booking_id}/assign-agent",
    response_model=BookingResponse,
    status_code=status.HTTP_200_OK,
    summary="Assign or reassign the sales agent on a booking",
    description=(
        "Assigns the given user as the booking's handling sales agent. "
        "Validates that the agent user exists."
    ),
    responses={
        200: {"description": "Agent assigned successfully."},
        404: {"description": "Booking or agent not found."},
        409: {"description": "Booking is inactive and cannot be modified."},
    },
)
async def assign_booking_agent(
    booking_id: uuid.UUID,
    payload: BookingAgentAssignment,
    service: BookingService = Depends(get_booking_service),
) -> BookingResponse:
    booking = await service.assign_agent(booking_id, payload.agent_id)
    return BookingResponse.model_validate(booking)


__all__ = ["router"]