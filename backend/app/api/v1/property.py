"""
Property Management API Endpoints.

Router layer only — all business logic lives in PropertyService.
Follows the same structure/style as app/api/v1/users.py:
    Router -> Service -> Repository -> Database
"""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.db.session import get_db
from app.api.dependencies.auth_dependency import get_current_user
from app.api.dependencies.rbac import require_roles
from app.models.user import User, UserRole
from app.models.property import PropertyType, ListingType, PropertyStatus
from app.schemas.property import (
    PropertyCreate,
    PropertyUpdate,
    PropertyResponse,
    PropertyStatusUpdate,
    PropertyAssignment,
    PaginatedPropertyResponse,
)
from app.services.property_service import PropertyService

router = APIRouter(prefix="/properties")


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@router.post(
    "",
    response_model=PropertyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new property",
    description="Create a new property listing. Accessible to Admin and Agent roles.",
)
async def create_property(
    payload: PropertyCreate,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_roles(UserRole.ADMIN, UserRole.SALES_AGENT)),
):
    service = PropertyService(db)
    return await service.create_property(payload)


# ---------------------------------------------------------------------------
# List / Search / Filter / Paginate
# ---------------------------------------------------------------------------
@router.get(
    "",
    response_model=PaginatedPropertyResponse,
    summary="List / search properties",
    description=(
        "Retrieve a paginated list of properties. Supports search by "
        "property code, title, city, state, or owner name, plus filters "
        "for property type, status, listing type, assigned agent, "
        "featured, and active flag."
    ),
)
async def list_properties(
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
    search: Optional[str] = Query(None, description="Search term (code, title, city, state, owner name)"),
    property_type: Optional[PropertyType] = Query(None, description="Filter by property type"),
    listing_type: Optional[ListingType] = Query(None, description="Filter by listing type"),
    property_status: Optional[PropertyStatus] = Query(None, alias="status", description="Filter by property status"),
    assigned_agent_id: Optional[int] = Query(None, description="Filter by assigned agent"),
    is_featured: Optional[bool] = Query(None, description="Filter featured properties"),
    is_active: Optional[bool] = Query(
        True,
        description=(
            "Filter by active flag. Defaults to True so soft-deleted "
            "properties are excluded unless explicitly set to false or "
            "omitted via null to see all."
        ),
    ),
    sort_by: Optional[str] = Query("created_at", description="Field to sort by"),
    sort_order: Optional[str] = Query("desc", description="Sort order: asc or desc"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(10, ge=1, le=100, description="Items per page"),
):
    service = PropertyService(db)
    return await service.list_properties(
        search=search,
        property_type=property_type,
        listing_type=listing_type,
        property_status=property_status,
        assigned_agent_id=assigned_agent_id,
        is_featured=is_featured,
        is_active=is_active,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        page_size=page_size,
    )


# ---------------------------------------------------------------------------
# Retrieve single
# ---------------------------------------------------------------------------
@router.get(
    "/{property_id}",
    response_model=PropertyResponse,
    summary="Get property by ID",
    description="Retrieve a single property by its UUID.",
)
async def get_property(
    property_id: UUID,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = PropertyService(db)
    return await service.get_property(property_id)


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------
@router.put(
    "/{property_id}",
    response_model=PropertyResponse,
    summary="Update a property",
    description="Update an existing property. Accessible to Admin and Agent roles.",
)
async def update_property(
    property_id: UUID,
    payload: PropertyUpdate,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_roles(UserRole.ADMIN, UserRole.SALES_AGENT)),
):
    service = PropertyService(db)
    return await service.update_property(property_id, payload)


# ---------------------------------------------------------------------------
# Status Update
# ---------------------------------------------------------------------------
@router.patch(
    "/{property_id}/status",
    response_model=PropertyResponse,
    summary="Update property status",
    description="Update the status of a property (e.g. Available, Sold, Rented).",
)
async def update_property_status(
    property_id: UUID,
    payload: PropertyStatusUpdate,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_roles(UserRole.ADMIN, UserRole.SALES_AGENT)),
):
    service = PropertyService(db)
    return await service.update_status(property_id, payload)


# ---------------------------------------------------------------------------
# Assign Agent
# ---------------------------------------------------------------------------
@router.patch(
    "/{property_id}/assign-agent",
    response_model=PropertyResponse,
    summary="Assign agent to property",
    description="Assign or reassign an agent to a property. Admin only.",
)
async def assign_agent(
    property_id: UUID,
    payload: PropertyAssignment,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_roles(UserRole.ADMIN)),
):
    service = PropertyService(db)
    return await service.assign_agent(property_id, payload)


# ---------------------------------------------------------------------------
# Soft Delete
# ---------------------------------------------------------------------------
@router.delete(
    "/{property_id}",
    response_model=PropertyResponse,
    summary="Soft delete a property",
    description="Soft delete a property (marks inactive, does not remove from database). Admin only.",
)
async def delete_property(
    property_id: UUID,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_roles(UserRole.ADMIN)),
):
    service = PropertyService(db)
    return await service.soft_delete_property(property_id)


# ---------------------------------------------------------------------------
# Reactivate
# ---------------------------------------------------------------------------
@router.patch(
    "/{property_id}/reactivate",
    response_model=PropertyResponse,
    summary="Reactivate a soft-deleted property",
    description="Restore a previously soft-deleted property. Admin only.",
)
async def reactivate_property(
    property_id: UUID,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_roles(UserRole.ADMIN)),
):
    service = PropertyService(db)
    return await service.reactivate_property(property_id)