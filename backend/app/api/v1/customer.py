"""
backend/app/api/v1/customers.py

API (presentation) layer for the Customer domain.

Responsibilities (and only these):
    - Receive and validate HTTP requests (via Pydantic schemas).
    - Resolve dependencies (database session, service layer, current
      user) through FastAPI's dependency-injection system.
    - Enforce authentication (JWT) and authorization (RBAC).
    - Delegate every piece of business logic to `CustomerService`.
    - Translate domain/service exceptions into `HTTPException`s.
    - Shape responses using `response_model` and document them for
      OpenAPI/Swagger.

Explicitly NOT here: business rules, SQL, ORM session/query
construction, or direct repository calls. Those live in
`CustomerService` / `CustomerRepository` respectively.

Design Notes:
    - `CustomerService`'s approved constructor is
      ``CustomerService(customer_repository, lead_repository,
      user_repository, session, *, logger=None)``. `get_customer_service`
      below wires exactly that shape from a request-scoped
      `AsyncSession`, using only the approved repository/service
      classes -- no repository or service method is ever called
      directly from a route handler.
    - Authentication and authorization reuse the approved
      `app.core.security` primitives (`get_current_user`,
      `require_roles`) unmodified. Role checks are expressed with the
      approved `UserRole` enum members (not raw strings), so they stay
      correct regardless of the exact string values `UserRole` is
      backed by.
    - `CustomerSearchFilters`, `CustomerExportRequest`, and every other
      body/response schema are imported as-is from `app.schemas.customer`
      and never re-declared. `POST /search` and `POST /export` accept
      full filter/export objects; the small number of endpoint-specific
      action payloads that have no equivalent in the schema layer
      (assign / status / follow-up updates) are declared as minimal,
      clearly-scoped request models in this module -- they carry no
      business logic and mirror the exact parameters the corresponding
      `CustomerService` methods accept.
    - Every domain exception raised by `CustomerService` is mapped to a
      specific HTTP status via `_EXCEPTION_STATUS_MAP` /
      `_run_service_call`, so no SQLAlchemy error, traceback, or other
      internal detail is ever exposed to a client.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Awaitable, Callable, Optional, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.api.dependencies.auth_dependency import get_current_user
from app.api.dependencies.rbac import require_roles
from app.exceptions import (
    CustomerNotFoundError,
    CustomerServiceError,
    DuplicateCustomerError,
    InvalidCustomerStateError,
    LeadNotFoundError,
    UserNotFoundError,
)
from app.models.customer import CustomerStatus
from app.models.user import User, UserRole
from app.repositories.customer_repository import CustomerRepository
from app.repositories.lead_repository import LeadRepository
from app.repositories.user_repository import UserRepository
from app.schemas.customer import (
    CustomerCreate,
    CustomerExportRequest,
    CustomerListResponse,
    CustomerResponse,
    CustomerSearchFilters,
    CustomerStatisticsResponse,
    CustomerUpdate,
)
from app.services.customer_service import CustomerService

router = APIRouter(prefix="/customers", tags=["Customers"])

_T = TypeVar("_T")

# --------------------------------------------------------------------------
# Role Groups
#
# The approved `UserRole` enum defines exactly three roles: ADMIN,
# SALES_MANAGER, SALES_AGENT. There is no dedicated read-only role in
# the approved model, so "read" access is simply "any authenticated
# staff member" (`get_current_user`) and these groups exist to express
# *write*-tier and *destructive*-tier authorization explicitly and
# extensibly, rather than to gate read access.
# --------------------------------------------------------------------------
_WRITE_ROLES: tuple[UserRole, ...] = (
    UserRole.ADMIN,
    UserRole.SALES_MANAGER,
    UserRole.SALES_AGENT,
)
_MANAGE_ROLES: tuple[UserRole, ...] = (UserRole.ADMIN, UserRole.SALES_MANAGER)
_ADMIN_ROLES: tuple[UserRole, ...] = (UserRole.ADMIN,)


# --------------------------------------------------------------------------
# Dependency Injection
# --------------------------------------------------------------------------
def get_customer_service(
    session: AsyncSession = Depends(get_async_session),
) -> CustomerService:
    """
    Build a request-scoped `CustomerService`, fully wired from the
    approved repository and service constructors.

    Wiring: `CustomerRepository(session)`, `LeadRepository(session)`,
    `UserRepository(session)` -> `CustomerService(customer_repository,
    lead_repository, user_repository, session)`.

    Args:
        session: The request-scoped async database session, injected
            via `get_async_session`.

    Returns:
        A `CustomerService` instance ready to handle this request's
        Customer-domain business logic.
    """
    return CustomerService(
        customer_repository=CustomerRepository(session),
        lead_repository=LeadRepository(session),
        user_repository=UserRepository(session),
        session=session,
    )


# --------------------------------------------------------------------------
# Endpoint-Specific Request Schemas
#
# Thin, single-purpose request DTOs for the handful of actions that
# have no corresponding schema in `app.schemas.customer`. Each mirrors
# exactly the parameters the matching `CustomerService` method accepts
# -- no additional fields, no business logic.
# --------------------------------------------------------------------------
class CustomerAssignRequest(BaseModel):
    """Request body for assigning a customer to a sales agent."""

    user_id: int = Field(
        ...,
        description="Internal ID of the User (sales agent) to assign this customer to.",
        examples=[7],
    )


class CustomerStatusUpdateRequest(BaseModel):
    """Request body for updating a customer's lifecycle status."""

    status: CustomerStatus = Field(
        ...,
        description="The new lifecycle status to apply to the customer.",
        examples=["ACTIVE"],
    )


class CustomerFollowupUpdateRequest(BaseModel):
    """Request body for updating a customer's follow-up schedule."""

    next_followup_date: Optional[date] = Field(
        default=None,
        description="New next-follow-up date, or omit/null to clear a scheduled follow-up.",
        examples=["2026-08-01"],
    )
    last_contacted_at: Optional[datetime] = Field(
        default=None,
        description="Optional new last-contacted UTC timestamp, typically 'now'.",
        examples=["2026-07-27T10:00:00Z"],
    )


# --------------------------------------------------------------------------
# Exception Translation
#
# Centralizes conversion of `CustomerService` domain exceptions into
# `HTTPException`s so no route handler needs a bespoke try/except
# block, and so no SQLAlchemy error, traceback, or other internal
# detail ever reaches a client.
# --------------------------------------------------------------------------
_EXCEPTION_STATUS_MAP: dict[type[Exception], int] = {
    CustomerNotFoundError: status.HTTP_404_NOT_FOUND,
    LeadNotFoundError: status.HTTP_422_UNPROCESSABLE_ENTITY,
    UserNotFoundError: status.HTTP_422_UNPROCESSABLE_ENTITY,
    DuplicateCustomerError: status.HTTP_409_CONFLICT,
    InvalidCustomerStateError: status.HTTP_409_CONFLICT,
    CustomerServiceError: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


async def _run_service_call(call: Callable[[], Awaitable[_T]]) -> _T:
    """
    Execute a `CustomerService` coroutine, translating any known domain
    exception into the matching `HTTPException`.

    Args:
        call: A zero-argument callable returning the awaitable service
            call to execute (e.g. ``lambda: service.get_customer(id)``).

    Returns:
        The awaited result of `call`, unchanged, on success.

    Raises:
        HTTPException: With a status code resolved from
            `_EXCEPTION_STATUS_MAP` and the exception's own
            client-safe message, for any mapped exception type.
            Re-raises anything unmapped unchanged (FastAPI's default
            handling still prevents traceback leakage).
    """
    try:
        return await call()
    except tuple(_EXCEPTION_STATUS_MAP.keys()) as exc:
        status_code = _EXCEPTION_STATUS_MAP[type(exc)]
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------
@router.post(
    "/",
    response_model=CustomerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a customer",
    description="Create a new customer record. Requires staff (admin, sales manager, or sales agent) authorization.",
    tags=["Customers"],
    responses={
        401: {"description": "Not authenticated."},
        403: {"description": "Not authorized to create customers."},
        409: {"description": "A customer with this email already exists."},
        422: {
            "description": "Request payload failed validation, or referenced lead/assigned user does not exist."
        },
    },
)
async def create_customer(
    payload: CustomerCreate,
    current_user: User = Depends(require_roles(*_WRITE_ROLES)),
    service: CustomerService = Depends(get_customer_service),
) -> CustomerResponse:
    """Create a new customer, stamping audit fields from the caller."""
    customer = await _run_service_call(
        lambda: service.create_customer(payload, created_by_id=current_user.id)
    )
    return CustomerResponse.model_validate(customer)


# --------------------------------------------------------------------------
# Read (collection / search / static-path routes declared before {customer_id})
# --------------------------------------------------------------------------
@router.get(
    "/",
    response_model=CustomerListResponse,
    status_code=status.HTTP_200_OK,
    summary="List customers",
    description="Retrieve a paginated, sortable list of customers, optionally filtered by active status.",
    tags=["Customers"],
    responses={401: {"description": "Not authenticated."}},
)
async def list_customers(
    page: int = Query(1, ge=1, description="1-indexed page number."),
    page_size: int = Query(20, ge=1, le=200, description="Number of records per page (max 200)."),
    sort_by: str = Query(
        "created_at",
        description="Field to sort by. Unrecognized values safely fall back to 'created_at'.",
    ),
    sort_order: str = Query("desc", pattern="^(asc|desc)$", description="Sort direction."),
    is_active: Optional[bool] = Query(
        True, description="Filter by active flag. Omit/null to include both active and inactive."
    ),
    current_user: User = Depends(get_current_user),
    service: CustomerService = Depends(get_customer_service),
) -> CustomerListResponse:
    """List customers with pagination and sorting, without free-text search."""
    customers, total = await _run_service_call(
        lambda: service.list_customers(
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
            is_active=is_active,
        )
    )
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    return CustomerListResponse(
        items=[CustomerResponse.model_validate(c) for c in customers],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.post(
    "/search",
    response_model=CustomerListResponse,
    status_code=status.HTTP_200_OK,
    summary="Search customers",
    description="Search and filter customers using the full enterprise filter set, with pagination and sorting.",
    tags=["Customers"],
    responses={401: {"description": "Not authenticated."}, 422: {"description": "Invalid filter payload."}},
)
async def search_customers(
    filters: CustomerSearchFilters,
    current_user: User = Depends(get_current_user),
    service: CustomerService = Depends(get_customer_service),
) -> CustomerListResponse:
    """Search customers against the full filter/pagination/sort contract."""
    customers, total = await _run_service_call(lambda: service.search_customers(filters))
    total_pages = (total + filters.page_size - 1) // filters.page_size if filters.page_size else 0
    return CustomerListResponse(
        items=[CustomerResponse.model_validate(c) for c in customers],
        total=total,
        page=filters.page,
        page_size=filters.page_size,
        total_pages=total_pages,
    )


@router.post(
    "/export",
    status_code=status.HTTP_200_OK,
    summary="Export customers",
    description=(
        "Export a filtered customer list as row data ready for CSV/XLSX "
        "serialization. Returns 200 (not 201) since this is a read/reporting "
        "operation expressed as POST solely to accept a structured filter body."
    ),
    tags=["Customers"],
    responses={401: {"description": "Not authenticated."}, 422: {"description": "Invalid export request."}},
)
async def export_customers(
    request: CustomerExportRequest,
    current_user: User = Depends(get_current_user),
    service: CustomerService = Depends(get_customer_service),
) -> list[dict[str, Any]]:
    """Export customer rows matching the given filters and column selection."""
    return await _run_service_call(
        lambda: service.export_customers(request, exported_by_id=current_user.id)
    )


@router.get(
    "/statistics",
    response_model=CustomerStatisticsResponse,
    status_code=status.HTTP_200_OK,
    summary="Customer statistics",
    description="Retrieve aggregate customer analytics for dashboards and reporting.",
    tags=["Customers"],
    responses={401: {"description": "Not authenticated."}},
)
async def get_customer_statistics(
    period_start: Optional[date] = Query(None, description="Inclusive lower bound on created_at."),
    period_end: Optional[date] = Query(None, description="Inclusive upper bound on created_at."),
    top_cities_limit: int = Query(10, ge=1, le=100, description="Maximum number of cities to include."),
    is_active: Optional[bool] = Query(True, description="Filter by active flag."),
    current_user: User = Depends(get_current_user),
    service: CustomerService = Depends(get_customer_service),
) -> CustomerStatisticsResponse:
    """Retrieve aggregate statistics across the (optionally scoped) customer set."""
    return await _run_service_call(
        lambda: service.get_customer_statistics(
            period_start=period_start,
            period_end=period_end,
            top_cities_limit=top_cities_limit,
            is_active=is_active,
        )
    )


@router.get(
    "/email/{email}",
    response_model=CustomerResponse,
    status_code=status.HTTP_200_OK,
    summary="Get customer by email",
    description="Retrieve a single customer by exact email address.",
    tags=["Customers"],
    responses={401: {"description": "Not authenticated."}, 404: {"description": "Customer not found."}},
)
async def get_customer_by_email(
    email: str,
    current_user: User = Depends(get_current_user),
    service: CustomerService = Depends(get_customer_service),
) -> CustomerResponse:
    """Retrieve a customer by their unique email address."""
    customer = await _run_service_call(lambda: service.get_customer_by_email(email))
    return CustomerResponse.model_validate(customer)


@router.get(
    "/phone/{phone}",
    response_model=CustomerResponse,
    status_code=status.HTTP_200_OK,
    summary="Get customer by phone",
    description=(
        "Retrieve a customer by phone number. Phone is not unique on the "
        "approved model, so the most recently created match is returned."
    ),
    tags=["Customers"],
    responses={401: {"description": "Not authenticated."}, 404: {"description": "Customer not found."}},
)
async def get_customer_by_phone(
    phone: str,
    current_user: User = Depends(get_current_user),
    service: CustomerService = Depends(get_customer_service),
) -> CustomerResponse:
    """Retrieve the most recently created customer matching a phone number."""
    customer = await _run_service_call(lambda: service.get_customer_by_phone(phone))
    return CustomerResponse.model_validate(customer)


@router.get(
    "/{customer_id}",
    response_model=CustomerResponse,
    status_code=status.HTTP_200_OK,
    summary="Get customer details",
    description="Retrieve a single customer by ID.",
    tags=["Customers"],
    responses={
        401: {"description": "Not authenticated."},
        404: {"description": "Customer not found."},
        422: {"description": "Invalid UUID supplied for customer_id."},
    },
)
async def get_customer(
    customer_id: UUID,
    is_active: Optional[bool] = Query(
        None, description="Optional active-flag filter. Omit to return regardless of active state."
    ),
    current_user: User = Depends(get_current_user),
    service: CustomerService = Depends(get_customer_service),
) -> CustomerResponse:
    """Retrieve a single customer by its unique identifier."""
    customer = await _run_service_call(
        lambda: service.get_customer(customer_id, is_active=is_active)
    )
    return CustomerResponse.model_validate(customer)


# --------------------------------------------------------------------------
# Update
# --------------------------------------------------------------------------
@router.put(
    "/{customer_id}",
    response_model=CustomerResponse,
    status_code=status.HTTP_200_OK,
    summary="Update customer",
    description="Update an existing customer record. Only fields explicitly set on the payload are applied.",
    tags=["Customers"],
    responses={
        401: {"description": "Not authenticated."},
        403: {"description": "Not authorized to update customers."},
        404: {"description": "Customer not found."},
        409: {"description": "The new email conflicts with another customer."},
        422: {
            "description": "Request payload failed validation, or referenced lead/assigned user does not exist."
        },
    },
)
async def update_customer(
    customer_id: UUID,
    payload: CustomerUpdate,
    current_user: User = Depends(require_roles(*_WRITE_ROLES)),
    service: CustomerService = Depends(get_customer_service),
) -> CustomerResponse:
    """Apply a partial update to an existing customer."""
    customer = await _run_service_call(
        lambda: service.update_customer(customer_id, payload, updated_by_id=current_user.id)
    )
    return CustomerResponse.model_validate(customer)


# --------------------------------------------------------------------------
# Delete / Soft-Delete / Restore
# --------------------------------------------------------------------------
@router.delete(
    "/{customer_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Permanently delete customer",
    description=(
        "Permanently delete a customer record. Provided for completeness "
        "(e.g. GDPR erasure); routine deactivation should use the "
        "soft-delete endpoint instead. Restricted to administrators."
    ),
    tags=["Customers"],
    responses={
        401: {"description": "Not authenticated."},
        403: {"description": "Not authorized to permanently delete customers."},
        404: {"description": "Customer not found."},
    },
)
async def delete_customer(
    customer_id: UUID,
    current_user: User = Depends(require_roles(*_ADMIN_ROLES)),
    service: CustomerService = Depends(get_customer_service),
) -> Response:
    """Permanently delete a customer record (hard delete)."""
    await _run_service_call(
        lambda: service.delete_customer(customer_id, deleted_by_id=current_user.id)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/{customer_id}/soft-delete",
    response_model=CustomerResponse,
    status_code=status.HTTP_200_OK,
    summary="Soft delete customer",
    description="Mark a customer as inactive without removing the underlying record.",
    tags=["Customers"],
    responses={
        401: {"description": "Not authenticated."},
        403: {"description": "Not authorized to deactivate customers."},
        404: {"description": "Customer not found."},
        409: {"description": "Customer is already inactive."},
    },
)
async def soft_delete_customer(
    customer_id: UUID,
    current_user: User = Depends(require_roles(*_MANAGE_ROLES)),
    service: CustomerService = Depends(get_customer_service),
) -> CustomerResponse:
    """Deactivate a customer record without deleting it."""
    customer = await _run_service_call(
        lambda: service.soft_delete_customer(customer_id, deleted_by_id=current_user.id)
    )
    return CustomerResponse.model_validate(customer)


@router.patch(
    "/{customer_id}/restore",
    response_model=CustomerResponse,
    status_code=status.HTTP_200_OK,
    summary="Restore customer",
    description="Restore a previously soft-deleted customer, marking it active again.",
    tags=["Customers"],
    responses={
        401: {"description": "Not authenticated."},
        403: {"description": "Not authorized to restore customers."},
        404: {"description": "Customer not found."},
        409: {"description": "Customer is already active."},
    },
)
async def restore_customer(
    customer_id: UUID,
    current_user: User = Depends(require_roles(*_MANAGE_ROLES)),
    service: CustomerService = Depends(get_customer_service),
) -> CustomerResponse:
    """Restore a soft-deleted customer to active status."""
    customer = await _run_service_call(
        lambda: service.restore_customer(customer_id, restored_by_id=current_user.id)
    )
    return CustomerResponse.model_validate(customer)


# --------------------------------------------------------------------------
# Assignment
# --------------------------------------------------------------------------
@router.patch(
    "/{customer_id}/assign",
    response_model=CustomerResponse,
    status_code=status.HTTP_200_OK,
    summary="Assign customer to an agent",
    description="Assign a customer to a sales agent (or any existing user).",
    tags=["Customers"],
    responses={
        401: {"description": "Not authenticated."},
        403: {"description": "Not authorized to assign customers."},
        404: {"description": "Customer not found."},
        422: {"description": "The target user does not exist."},
    },
)
async def assign_customer(
    customer_id: UUID,
    payload: CustomerAssignRequest,
    current_user: User = Depends(require_roles(*_WRITE_ROLES)),
    service: CustomerService = Depends(get_customer_service),
) -> CustomerResponse:
    """Assign a customer to the given user."""
    customer = await _run_service_call(
        lambda: service.assign_customer(
            customer_id, payload.user_id, assigned_by_id=current_user.id
        )
    )
    return CustomerResponse.model_validate(customer)


@router.patch(
    "/{customer_id}/unassign",
    response_model=CustomerResponse,
    status_code=status.HTTP_200_OK,
    summary="Unassign customer",
    description="Remove the current agent assignment from a customer.",
    tags=["Customers"],
    responses={
        401: {"description": "Not authenticated."},
        403: {"description": "Not authorized to unassign customers."},
        404: {"description": "Customer not found."},
    },
)
async def unassign_customer(
    customer_id: UUID,
    current_user: User = Depends(require_roles(*_WRITE_ROLES)),
    service: CustomerService = Depends(get_customer_service),
) -> CustomerResponse:
    """Clear the current agent assignment on a customer."""
    customer = await _run_service_call(
        lambda: service.unassign_customer(customer_id, unassigned_by_id=current_user.id)
    )
    return CustomerResponse.model_validate(customer)


# --------------------------------------------------------------------------
# Status & Follow-up
# --------------------------------------------------------------------------
@router.patch(
    "/{customer_id}/status",
    response_model=CustomerResponse,
    status_code=status.HTTP_200_OK,
    summary="Update customer status",
    description="Update a customer's lifecycle status.",
    tags=["Customers"],
    responses={
        401: {"description": "Not authenticated."},
        403: {"description": "Not authorized to update customer status."},
        404: {"description": "Customer not found."},
        422: {"description": "Invalid status value."},
    },
)
async def update_customer_status(
    customer_id: UUID,
    payload: CustomerStatusUpdateRequest,
    current_user: User = Depends(require_roles(*_WRITE_ROLES)),
    service: CustomerService = Depends(get_customer_service),
) -> CustomerResponse:
    """Update the lifecycle status of a customer."""
    customer = await _run_service_call(
        lambda: service.update_customer_status(
            customer_id, payload.status, updated_by_id=current_user.id
        )
    )
    return CustomerResponse.model_validate(customer)


@router.patch(
    "/{customer_id}/followup",
    response_model=CustomerResponse,
    status_code=status.HTTP_200_OK,
    summary="Update customer follow-up",
    description="Update a customer's next follow-up date and/or last-contacted timestamp.",
    tags=["Customers"],
    responses={
        401: {"description": "Not authenticated."},
        403: {"description": "Not authorized to update follow-up details."},
        404: {"description": "Customer not found."},
        409: {"description": "The supplied follow-up date is in the past."},
    },
)
async def update_customer_followup(
    customer_id: UUID,
    payload: CustomerFollowupUpdateRequest,
    current_user: User = Depends(require_roles(*_WRITE_ROLES)),
    service: CustomerService = Depends(get_customer_service),
) -> CustomerResponse:
    """Update a customer's scheduled follow-up date and/or last-contacted time."""
    customer = await _run_service_call(
        lambda: service.update_followup(
            customer_id,
            next_followup_date=payload.next_followup_date,
            last_contacted_at=payload.last_contacted_at,
            updated_by_id=current_user.id,
        )
    )
    return CustomerResponse.model_validate(customer)


__all__ = ["router"]