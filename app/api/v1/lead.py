"""
backend/app/api/v1/lead.py

API router for Lead management endpoints.

Responsibilities:
    - Expose HTTP endpoints for lead creation, retrieval, listing,
      updates, status/priority transitions, agent assignment, soft
      deletion, dashboard summary, follow-ups, and search.
    - Perform request/response schema validation only.
    - Delegate all business logic to `LeadService`, and all persistence
      access (via the service) to `LeadRepository`.

Design Notes:
    - This router contains NO direct database queries, business rules,
      or SQL — those concerns are fully encapsulated in
      `app.services.lead_service.LeadService` and
      `app.repositories.lead_repository.LeadRepository`, keeping this
      layer thin and testable.
    - `LeadService` is instantiated per-request via a small dependency
      provider (`get_lead_service`) so it can be swapped/mocked in
      tests without modifying route signatures.
    - Every endpoint requires authentication via `get_current_user`;
      this router does not implement authentication logic itself, only
      consumes the existing dependency.
    - Domain exceptions raised by `LeadService` are caught at the
      boundary of each endpoint and translated into the appropriate
      `HTTPException`. No domain exception is allowed to propagate past
      this layer unconverted.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth_dependency import get_current_user
from app.db.session import get_db
from app.models.lead import LeadPriority, LeadSource, LeadStatus
from app.models.user import User
from app.repositories.lead_repository import LeadRepository
from app.schemas.lead import (
    LeadCreate,
    LeadListResponse,
    LeadResponse,
    LeadUpdate,
)
from app.services.lead_service import (
    DuplicateEmailError,
    DuplicatePhoneError,
    InactiveLeadError,
    InvalidAgentAssignmentError,
    InvalidStatusTransitionError,
    LeadNotFoundError,
    LeadService,
    TerminalLeadStatusError,
)

router = APIRouter(prefix="/leads", tags=["Lead CRM"])


# --------------------------------------------------------------------------
# Local Request Schemas
# --------------------------------------------------------------------------
# These small, single-purpose request bodies are scoped to this router
# because they map directly to individual PATCH endpoint parameters and
# do not represent standalone Lead domain concepts belonging in
# app/schemas/lead.py.
# --------------------------------------------------------------------------
class LeadStatusChangeRequest(BaseModel):
    """Request payload for transitioning a lead's pipeline status."""

    model_config = ConfigDict(json_schema_extra={"example": {"status": "CONTACTED"}})

    status: LeadStatus = Field(
        ...,
        description="The target pipeline status to transition the lead to.",
        examples=["CONTACTED"],
    )


class LeadPriorityChangeRequest(BaseModel):
    """Request payload for changing a lead's priority level."""

    model_config = ConfigDict(json_schema_extra={"example": {"priority": "HIGH"}})

    priority: LeadPriority = Field(
        ...,
        description="The new priority level to assign to the lead.",
        examples=["HIGH"],
    )


class LeadAssignAgentRequest(BaseModel):
    """Request payload for assigning a sales agent to a lead."""

    model_config = ConfigDict(json_schema_extra={"example": {"agent_id": 12}})

    agent_id: int = Field(
        ...,
        gt=0,
        description="Internal User ID of the sales agent to assign to the lead.",
        examples=[12],
    )


# --------------------------------------------------------------------------
# Service Dependency Provider
# --------------------------------------------------------------------------
def get_lead_service(db: AsyncSession = Depends(get_db)) -> LeadService:
    """
    Provide a request-scoped `LeadService` instance, wired to a
    `LeadRepository` bound to the current database session.

    Args:
        db: An active `AsyncSession`, injected via `get_db`.

    Returns:
        A fully constructed `LeadService` ready to handle the request.
    """
    repository = LeadRepository(db)
    return LeadService(repository)


# --------------------------------------------------------------------------
# Domain Exception Translation Helper
# --------------------------------------------------------------------------
def _raise_http_exception(exc: Exception) -> None:
    """
    Translate a `LeadService` domain exception into the appropriate
    `HTTPException` and raise it.

    Args:
        exc: The caught domain exception instance.

    Raises:
        HTTPException: Always raised, with a status code and detail
            message determined by the exception type. Falls back to
            500 Internal Server Error for any unrecognized exception.
    """
    if isinstance(exc, LeadNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, (DuplicatePhoneError, DuplicateEmailError)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(
        exc,
        (
            InactiveLeadError,
            InvalidStatusTransitionError,
            InvalidAgentAssignmentError,
            TerminalLeadStatusError,
        ),
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Internal server error. Please try again later.",
    )


# --------------------------------------------------------------------------
# POST /leads
# --------------------------------------------------------------------------
@router.post(
    "/",
    response_model=LeadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new lead",
    description=(
        "Creates a new sales lead with the supplied contact and "
        "requirement details. Responds with 409 Conflict if the phone "
        "number or email address is already registered to another "
        "lead."
    ),
    responses={
        201: {"description": "Lead created successfully."},
        409: {"description": "Duplicate phone number or email address."},
        422: {"description": "Validation error in request payload."},
    },
)
async def create_lead(
    payload: LeadCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    lead_service: Annotated[LeadService, Depends(get_lead_service)],
) -> Any:
    """
    Create a new lead.

    Args:
        payload: Validated `LeadCreate` containing the new lead's
                 contact and requirement details.
        current_user: The authenticated user making the request.
        lead_service: Injected `LeadService` handling lead creation
                      business logic.

    Returns:
        The newly created lead, serialized via `LeadResponse`.

    Raises:
        HTTPException(409): If the phone or email is already registered
            to another lead.
    """
    data = payload.model_dump()
    data["created_by"] = current_user.id
    try:
        return await lead_service.create_lead(data)
    except (DuplicatePhoneError, DuplicateEmailError) as exc:
        _raise_http_exception(exc)


# --------------------------------------------------------------------------
# GET /leads
# --------------------------------------------------------------------------
@router.get(
    "/",
    response_model=LeadListResponse,
    status_code=status.HTTP_200_OK,
    summary="List leads",
    description=(
        "Retrieves a paginated, filterable, sortable list of leads. "
        "Supports filtering by status, priority, lead source, assigned "
        "agent, property type, preferred location, and a free-text "
        "search term."
    ),
    responses={200: {"description": "Paginated list of leads."}},
)
async def list_leads(
    current_user: Annotated[User, Depends(get_current_user)],
    lead_service: Annotated[LeadService, Depends(get_lead_service)],
    page: int = Query(default=1, ge=1, description="1-indexed page number."),
    page_size: int = Query(default=20, ge=1, le=100, description="Records per page (max 100)."),
    status_filter: Optional[LeadStatus] = Query(default=None, alias="status", description="Filter by pipeline status."),
    priority: Optional[LeadPriority] = Query(default=None, description="Filter by priority level."),
    lead_source: Optional[LeadSource] = Query(default=None, description="Filter by acquisition channel."),
    assigned_agent_id: Optional[int] = Query(default=None, description="Filter by assigned agent's User ID."),
    property_type: Optional[str] = Query(default=None, description="Filter by property type."),
    preferred_location: Optional[str] = Query(default=None, description="Filter by preferred location."),
    search: Optional[str] = Query(default=None, description="Free-text search term."),
    sort_by: str = Query(default="created_at", description="Field to sort results by."),
    sort_order: str = Query(default="desc", description="Sort direction: 'asc' or 'desc'."),
) -> Any:
    """
    List leads with pagination, filtering, sorting, and search.

    Args:
        current_user: The authenticated user making the request.
        lead_service: Injected `LeadService` handling listing logic.
        page: 1-indexed page number to retrieve.
        page_size: Number of records to return per page.
        status_filter: Optional pipeline status filter.
        priority: Optional priority filter.
        lead_source: Optional acquisition channel filter.
        assigned_agent_id: Optional assigned agent filter.
        property_type: Optional property type filter.
        preferred_location: Optional preferred location filter.
        search: Optional free-text search term.
        sort_by: Column name to sort by.
        sort_order: "asc" or "desc".

    Returns:
        A paginated collection of leads, serialized via
        `LeadListResponse`.
    """
    result = await lead_service.list_leads(
        page=page,
        page_size=page_size,
        status=status_filter,
        priority=priority,
        lead_source=lead_source,
        assigned_agent_id=assigned_agent_id,
        property_type=property_type,
        preferred_location=preferred_location,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return result


# --------------------------------------------------------------------------
# GET /leads/dashboard/summary
# --------------------------------------------------------------------------
@router.get(
    "/dashboard/summary",
    status_code=status.HTTP_200_OK,
    summary="Retrieve lead pipeline dashboard summary",
    description=(
        "Returns aggregate pipeline metrics: total active leads, counts "
        "by key statuses (new, contacted, booked, closed), and full "
        "breakdowns by status and acquisition source."
    ),
    responses={200: {"description": "Dashboard summary metrics."}},
)
async def dashboard_summary(
    current_user: Annotated[User, Depends(get_current_user)],
    lead_service: Annotated[LeadService, Depends(get_lead_service)],
) -> Any:
    """
    Retrieve aggregate lead pipeline metrics for dashboard display.

    Args:
        current_user: The authenticated user making the request.
        lead_service: Injected `LeadService` handling aggregation logic.

    Returns:
        A dict containing total/new/contacted/booked/closed counts and
        breakdowns by status and source.
    """
    return await lead_service.dashboard_summary()


# --------------------------------------------------------------------------
# GET /leads/followups/today
# --------------------------------------------------------------------------
@router.get(
    "/followups/today",
    response_model=list[LeadResponse],
    status_code=status.HTTP_200_OK,
    summary="Retrieve today's pending follow-ups",
    description=(
        "Returns all active leads whose next follow-up date is due "
        "today or earlier."
    ),
    responses={200: {"description": "List of leads due for follow-up."}},
)
async def upcoming_followups(
    current_user: Annotated[User, Depends(get_current_user)],
    lead_service: Annotated[LeadService, Depends(get_lead_service)],
) -> Any:
    """
    Retrieve active leads due for follow-up today or earlier.

    Args:
        current_user: The authenticated user making the request.
        lead_service: Injected `LeadService` handling follow-up lookup.

    Returns:
        A list of matching leads, serialized via `LeadResponse`.
    """
    return await lead_service.upcoming_followups()


# --------------------------------------------------------------------------
# GET /leads/search
# --------------------------------------------------------------------------
@router.get(
    "/search",
    response_model=LeadListResponse,
    status_code=status.HTTP_200_OK,
    summary="Search leads",
    description=(
        "Performs a free-text search across lead name, phone, email, "
        "and remarks fields, with pagination support."
    ),
    responses={200: {"description": "Paginated search results."}},
)
async def search_leads(
    current_user: Annotated[User, Depends(get_current_user)],
    lead_service: Annotated[LeadService, Depends(get_lead_service)],
    q: str = Query(..., min_length=1, description="Free-text search term."),
    page: int = Query(default=1, ge=1, description="1-indexed page number."),
    page_size: int = Query(default=20, ge=1, le=100, description="Records per page (max 100)."),
) -> Any:
    """
    Search leads by free-text term.

    Args:
        current_user: The authenticated user making the request.
        lead_service: Injected `LeadService` handling search logic.
        q: The search term to match against name/phone/email/remarks.
        page: 1-indexed page number to retrieve.
        page_size: Number of records to return per page.

    Returns:
        A paginated collection of matching leads, serialized via
        `LeadListResponse`.
    """
    return await lead_service.search(q, page=page, page_size=page_size)


# --------------------------------------------------------------------------
# GET /leads/{lead_id}
# --------------------------------------------------------------------------
@router.get(
    "/{lead_id}",
    response_model=LeadResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve a lead by ID",
    description="Returns the full details of a single lead by its UUID.",
    responses={
        200: {"description": "The requested lead."},
        404: {"description": "Lead not found."},
    },
)
async def get_lead(
    lead_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    lead_service: Annotated[LeadService, Depends(get_lead_service)],
) -> Any:
    """
    Retrieve a single lead by its identifier.

    Args:
        lead_id: The UUID of the lead to retrieve.
        current_user: The authenticated user making the request.
        lead_service: Injected `LeadService` handling retrieval logic.

    Returns:
        The matching lead, serialized via `LeadResponse`.

    Raises:
        HTTPException(404): If no lead with the given ID exists.
    """
    try:
        return await lead_service.get_lead(lead_id)
    except LeadNotFoundError as exc:
        _raise_http_exception(exc)


# --------------------------------------------------------------------------
# PUT /leads/{lead_id}
# --------------------------------------------------------------------------
@router.put(
    "/{lead_id}",
    response_model=LeadResponse,
    status_code=status.HTTP_200_OK,
    summary="Update a lead",
    description=(
        "Updates an existing lead's fields. Only supplied fields are "
        "applied. Responds with 409 Conflict if the updated phone or "
        "email collides with another lead, or 400 Bad Request if the "
        "lead is inactive."
    ),
    responses={
        200: {"description": "The updated lead."},
        400: {"description": "Lead is inactive and cannot be modified."},
        404: {"description": "Lead not found."},
        409: {"description": "Duplicate phone number or email address."},
    },
)
async def update_lead(
    lead_id: uuid.UUID,
    payload: LeadUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    lead_service: Annotated[LeadService, Depends(get_lead_service)],
) -> Any:
    """
    Update an existing lead's fields.

    Args:
        lead_id: The UUID of the lead to update.
        payload: Validated `LeadUpdate` containing the fields to change.
        current_user: The authenticated user making the request.
        lead_service: Injected `LeadService` handling update logic.

    Returns:
        The updated lead, serialized via `LeadResponse`.

    Raises:
        HTTPException(404): If no lead with the given ID exists.
        HTTPException(400): If the lead is inactive.
        HTTPException(409): If the updated phone or email collides with
            another lead.
    """
    data = payload.model_dump(exclude_unset=True)
    try:
        return await lead_service.update_lead(lead_id, data)
    except (
        LeadNotFoundError,
        InactiveLeadError,
        DuplicatePhoneError,
        DuplicateEmailError,
    ) as exc:
        _raise_http_exception(exc)


# --------------------------------------------------------------------------
# PATCH /leads/{lead_id}/status
# --------------------------------------------------------------------------
@router.patch(
    "/{lead_id}/status",
    response_model=LeadResponse,
    status_code=status.HTTP_200_OK,
    summary="Change a lead's pipeline status",
    description=(
        "Transitions a lead to a new pipeline status, enforcing the "
        "forward-only workflow ordering (NEW -> CONTACTED -> QUALIFIED "
        "-> SITE_VISIT -> NEGOTIATION -> BOOKED), with LOST reachable "
        "from any non-terminal status. BOOKED and LOST are terminal "
        "states; no further transitions are permitted once reached."
    ),
    responses={
        200: {"description": "The lead with its updated status."},
        400: {"description": "Invalid status transition, inactive lead, or terminal status."},
        404: {"description": "Lead not found."},
    },
)
async def change_lead_status(
    lead_id: uuid.UUID,
    payload: LeadStatusChangeRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    lead_service: Annotated[LeadService, Depends(get_lead_service)],
) -> Any:
    """
    Change a lead's pipeline status.

    Args:
        lead_id: The UUID of the lead to update.
        payload: Validated `LeadStatusChangeRequest` containing the
                 target status.
        current_user: The authenticated user making the request.
        lead_service: Injected `LeadService` handling status transition
                      logic.

    Returns:
        The updated lead, serialized via `LeadResponse`.

    Raises:
        HTTPException(404): If no lead with the given ID exists.
        HTTPException(400): If the lead is inactive, has reached a
            terminal status, or the transition violates the workflow.
    """
    try:
        return await lead_service.change_status(lead_id, payload.status)
    except (
        LeadNotFoundError,
        InactiveLeadError,
        TerminalLeadStatusError,
        InvalidStatusTransitionError,
    ) as exc:
        _raise_http_exception(exc)


# --------------------------------------------------------------------------
# PATCH /leads/{lead_id}/priority
# --------------------------------------------------------------------------
@router.patch(
    "/{lead_id}/priority",
    response_model=LeadResponse,
    status_code=status.HTTP_200_OK,
    summary="Change a lead's priority",
    description="Updates the priority level assigned to a lead.",
    responses={
        200: {"description": "The lead with its updated priority."},
        400: {"description": "Lead is inactive and cannot be modified."},
        404: {"description": "Lead not found."},
    },
)
async def change_lead_priority(
    lead_id: uuid.UUID,
    payload: LeadPriorityChangeRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    lead_service: Annotated[LeadService, Depends(get_lead_service)],
) -> Any:
    """
    Change a lead's priority level.

    Args:
        lead_id: The UUID of the lead to update.
        payload: Validated `LeadPriorityChangeRequest` containing the
                 new priority.
        current_user: The authenticated user making the request.
        lead_service: Injected `LeadService` handling priority update
                      logic.

    Returns:
        The updated lead, serialized via `LeadResponse`.

    Raises:
        HTTPException(404): If no lead with the given ID exists.
        HTTPException(400): If the lead is inactive.
    """
    try:
        return await lead_service.change_priority(lead_id, payload.priority)
    except (LeadNotFoundError, InactiveLeadError) as exc:
        _raise_http_exception(exc)


# --------------------------------------------------------------------------
# PATCH /leads/{lead_id}/assign-agent
# --------------------------------------------------------------------------
@router.patch(
    "/{lead_id}/assign-agent",
    response_model=LeadResponse,
    status_code=status.HTTP_200_OK,
    summary="Assign a sales agent to a lead",
    description=(
        "Assigns the specified sales agent to a lead. Responds with "
        "400 Bad Request if the lead is inactive or the agent "
        "identifier is invalid."
    ),
    responses={
        200: {"description": "The lead with its updated agent assignment."},
        400: {"description": "Lead is inactive or agent identifier is invalid."},
        404: {"description": "Lead not found."},
    },
)
async def assign_lead_agent(
    lead_id: uuid.UUID,
    payload: LeadAssignAgentRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    lead_service: Annotated[LeadService, Depends(get_lead_service)],
) -> Any:
    """
    Assign a sales agent to a lead.

    Args:
        lead_id: The UUID of the lead to update.
        payload: Validated `LeadAssignAgentRequest` containing the
                 target agent's User ID.
        current_user: The authenticated user making the request.
        lead_service: Injected `LeadService` handling assignment logic.

    Returns:
        The updated lead, serialized via `LeadResponse`.

    Raises:
        HTTPException(404): If no lead with the given ID exists.
        HTTPException(400): If the lead is inactive or the agent
            identifier is invalid.
    """
    try:
        return await lead_service.assign_agent(lead_id, payload.agent_id)
    except (LeadNotFoundError, InactiveLeadError, InvalidAgentAssignmentError) as exc:
        _raise_http_exception(exc)


# --------------------------------------------------------------------------
# DELETE /leads/{lead_id}
# --------------------------------------------------------------------------
@router.delete(
    "/{lead_id}",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a lead",
    description=(
        "Marks a lead as inactive (soft delete). The record is "
        "preserved for auditing/history purposes and is not physically "
        "removed from the database."
    ),
    responses={
        204: {"description": "Lead soft-deleted successfully."},
        404: {"description": "Lead not found."},
    },
)
async def delete_lead(
    lead_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    lead_service: Annotated[LeadService, Depends(get_lead_service)],
) -> Response:
    """
    Soft-delete a lead by marking it inactive.

    Args:
        lead_id: The UUID of the lead to soft-delete.
        current_user: The authenticated user making the request.
        lead_service: Injected `LeadService` handling soft deletion
                      logic.

    Returns:
        `Response` with status 204 and no body on success.

    Raises:
        HTTPException(404): If no lead with the given ID exists.
    """
    try:
        await lead_service.delete_lead(lead_id)
    except LeadNotFoundError as exc:
        _raise_http_exception(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]