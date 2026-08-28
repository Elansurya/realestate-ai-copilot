# backend/app/api/v1/payment.py

import uuid
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db, get_current_user, RoleChecker, UserRole
from app.models.user import User
from app.models.payment import PaymentStatus, PaymentMode, PaymentType
from app.schemas.payment import (
    PaymentCreate,
    PaymentUpdate,
    PaymentResponse,
    PaymentListResponse,
    PaymentStatusUpdate,
    PaymentSearchFilter,
    DashboardPaymentSummary,
)
from app.services.payment_service import PaymentService

router = APIRouter(prefix="/payments", tags=["Payments"])


def get_payment_service(db: AsyncSession = Depends(get_db)) -> PaymentService:
    return PaymentService(db)


@router.post(
    "",
    response_model=PaymentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new payment",
    description=(
        "Creates a new payment record for a booking. Validates booking, "
        "customer, property and receiver, enforces pending amount limits, "
        "generates payment/receipt numbers, and syncs booking payment state."
    ),
    responses={
        400: {"description": "Invalid payment or inactive related entity"},
        404: {"description": "Booking, customer, property or user not found"},
        409: {"description": "Duplicate successful transaction reference"},
        422: {"description": "Validation error"},
    },
)
async def create_payment(
    payment_data: PaymentCreate,
    service: PaymentService = Depends(get_payment_service),
    current_user: User = Depends(
        RoleChecker([UserRole.ADMIN, UserRole.MANAGER, UserRole.SALES_AGENT])
    ),
) -> PaymentResponse:
    return await service.create_payment(payment_data)


@router.get(
    "",
    response_model=PaymentListResponse,
    status_code=status.HTTP_200_OK,
    summary="List and search payments",
    description="Retrieves a paginated, filterable and sortable list of payments.",
)
async def list_payments(
    booking_id: Optional[uuid.UUID] = Query(None),
    customer_id: Optional[uuid.UUID] = Query(None),
    property_id: Optional[int] = Query(None),
    received_by: Optional[int] = Query(None),
    payment_status: Optional[PaymentStatus] = Query(None),
    payment_mode: Optional[PaymentMode] = Query(None),
    payment_type: Optional[PaymentType] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    min_amount: Optional[Decimal] = Query(None),
    max_amount: Optional[Decimal] = Query(None),
    search: Optional[str] = Query(None, max_length=100),
    is_active: Optional[bool] = Query(True),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort_by: str = Query("created_at"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    service: PaymentService = Depends(get_payment_service),
    current_user: User = Depends(
        RoleChecker([UserRole.ADMIN, UserRole.MANAGER, UserRole.SALES_AGENT])
    ),
) -> PaymentListResponse:
    filters = PaymentSearchFilter(
        booking_id=booking_id,
        customer_id=customer_id,
        property_id=property_id,
        received_by=received_by,
        payment_status=payment_status,
        payment_mode=payment_mode,
        payment_type=payment_type,
        date_from=date_from,
        date_to=date_to,
        min_amount=min_amount,
        max_amount=max_amount,
        search=search,
        is_active=is_active,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return await service.list_payments(filters)


@router.get(
    "/dashboard/summary",
    response_model=DashboardPaymentSummary,
    status_code=status.HTTP_200_OK,
    summary="Get payment dashboard summary",
    description="Aggregated payment metrics for dashboards and reporting.",
)
async def get_dashboard_summary(
    service: PaymentService = Depends(get_payment_service),
    current_user: User = Depends(
        RoleChecker([UserRole.ADMIN, UserRole.MANAGER])
    ),
) -> DashboardPaymentSummary:
    return await service.get_dashboard_summary()


@router.get(
    "/today",
    response_model=list[PaymentResponse],
    status_code=status.HTTP_200_OK,
    summary="Get today's payments",
    description="Retrieves all active payments recorded for the current date.",
)
async def get_today_payments(
    service: PaymentService = Depends(get_payment_service),
    current_user: User = Depends(
        RoleChecker([UserRole.ADMIN, UserRole.MANAGER, UserRole.SALES_AGENT])
    ),
) -> list[PaymentResponse]:
    return await service.get_today_payments()


@router.get(
    "/monthly-revenue",
    response_model=Decimal,
    status_code=status.HTTP_200_OK,
    summary="Get monthly revenue",
    description="Total successful revenue collected for a given year and month.",
)
async def get_monthly_revenue(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    service: PaymentService = Depends(get_payment_service),
    current_user: User = Depends(
        RoleChecker([UserRole.ADMIN, UserRole.MANAGER])
    ),
) -> Decimal:
    return await service.get_monthly_revenue(year, month)


@router.get(
    "/{payment_id}",
    response_model=PaymentResponse,
    status_code=status.HTTP_200_OK,
    summary="Get a payment by ID",
    responses={404: {"description": "Payment not found"}},
)
async def get_payment(
    payment_id: uuid.UUID,
    service: PaymentService = Depends(get_payment_service),
    current_user: User = Depends(
        RoleChecker([UserRole.ADMIN, UserRole.MANAGER, UserRole.SALES_AGENT])
    ),
) -> PaymentResponse:
    return await service.get_payment(payment_id)


@router.put(
    "/{payment_id}",
    response_model=PaymentResponse,
    status_code=status.HTTP_200_OK,
    summary="Update a payment",
    responses={
        400: {"description": "Invalid update or restricted field modification"},
        404: {"description": "Payment not found"},
        409: {"description": "Duplicate successful transaction reference"},
    },
)
async def update_payment(
    payment_id: uuid.UUID,
    payment_data: PaymentUpdate,
    service: PaymentService = Depends(get_payment_service),
    current_user: User = Depends(
        RoleChecker([UserRole.ADMIN, UserRole.MANAGER])
    ),
) -> PaymentResponse:
    return await service.update_payment(payment_id, payment_data)


@router.patch(
    "/{payment_id}/status",
    response_model=PaymentResponse,
    status_code=status.HTTP_200_OK,
    summary="Update payment status",
    description=(
        "Transitions a payment's status following the allowed state machine: "
        "PENDING/PARTIAL -> SUCCESS/FAILED, SUCCESS -> REFUNDED."
    ),
    responses={
        400: {"description": "Invalid status transition"},
        404: {"description": "Payment not found"},
        409: {"description": "Duplicate successful transaction reference"},
    },
)
async def update_payment_status(
    payment_id: uuid.UUID,
    status_data: PaymentStatusUpdate,
    service: PaymentService = Depends(get_payment_service),
    current_user: User = Depends(
        RoleChecker([UserRole.ADMIN, UserRole.MANAGER])
    ),
) -> PaymentResponse:
    return await service.update_payment_status(payment_id, status_data)


@router.delete(
    "/{payment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft delete a payment",
    responses={
        404: {"description": "Payment not found"},
        409: {"description": "Cannot delete a SUCCESS payment"},
    },
)
async def delete_payment(
    payment_id: uuid.UUID,
    service: PaymentService = Depends(get_payment_service),
    current_user: User = Depends(RoleChecker([UserRole.ADMIN])),
) -> None:
    await service.delete_payment(payment_id)