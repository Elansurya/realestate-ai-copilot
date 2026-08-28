"""
backend/app/api/v1/users.py

API router for User Management endpoints (Milestone 4).

Responsibilities:
    - Expose HTTP endpoints for self-service profile management
      (view/update own profile, change own password) and admin-driven
      user administration (list, view, update, deactivate, reactivate).
    - Perform request/response schema validation and authorization
      wiring only.
    - Delegate all business logic to `UserService`.

Design Notes:
    - This router contains NO direct database queries, ORM models, or
      password/JWT handling. It is a thin transport layer: parse the
      request, resolve dependencies, call `UserService`, return the
      result.
    - `UserService` is instantiated per-request via a small dependency
      provider (`get_user_service`) so it can be swapped/mocked in
      tests without modifying route signatures, mirroring the pattern
      already established by `app.api.v1.auth.get_auth_service`.
    - Self-service routes (`/users/me`, `/users/me/change-password`)
      only require an authenticated caller (`get_current_user`) and
      always operate on that caller's own record.
    - Admin routes (list, get-by-uuid, update, deactivate, reactivate)
      additionally require the caller to hold the ADMIN role via
      `require_roles(UserRole.ADMIN)`.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.rbac import require_roles
from app.core.database import get_async_session
from app.api.dependencies.auth_dependency import get_current_user
from app.models.user import User, UserRole
from app.repositories.user_repository import UserRepository
from app.schemas.user import (
    ChangePasswordRequest,
    PaginatedUserResponse,
    UserAdminUpdate,
    UserResponse,
    UserUpdate,
)
from app.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["Users"])


# --------------------------------------------------------------------------
# Service Dependency Provider
# --------------------------------------------------------------------------
def get_user_service(
    db: AsyncSession = Depends(get_async_session),
) -> UserService:
    """
    Provide a request-scoped `UserService` instance bound to the
    current database session.

    Args:
        db: An active `AsyncSession`, injected via `get_async_session`.

    Returns:
        A `UserService` wired to a `UserRepository` bound to `db`.
    """
    user_repository = UserRepository(db)
    return UserService(user_repository)


# ==========================================================================
# Self-Service Endpoints
# ==========================================================================
@router.get(
    "/me",
    response_model=UserResponse,
    summary="Get my profile",
    description="Retrieve the authenticated caller's own profile.",
)
async def get_my_profile(
    current_user: User = Depends(get_current_user),
    user_service: UserService = Depends(get_user_service),
):
    return await user_service.get_profile(current_user)


@router.patch(
    "/me",
    response_model=UserResponse,
    summary="Update my profile",
    description=(
        "Update the authenticated caller's own profile (full name "
        "and/or phone). Cannot be used to change role or email."
    ),
)
async def update_my_profile(
    payload: UserUpdate,
    current_user: User = Depends(get_current_user),
    user_service: UserService = Depends(get_user_service),
):
    return await user_service.update_profile(current_user, payload)


@router.post(
    "/me/change-password",
    response_model=UserResponse,
    summary="Change my password",
    description=(
        "Change the authenticated caller's own password, after "
        "verifying their current password."
    ),
)
async def change_my_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    user_service: UserService = Depends(get_user_service),
):
    return await user_service.change_password(current_user, payload)


# ==========================================================================
# Admin Endpoints
# ==========================================================================
@router.get(
    "",
    response_model=PaginatedUserResponse,
    summary="List users",
    description=(
        "Retrieve a paginated, optionally filtered list of all users. "
        "Admin only."
    ),
)
async def list_users(
    page: int = Query(1, ge=1, description="1-indexed page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    role: Optional[UserRole] = Query(None, description="Filter by role"),
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
    search: Optional[str] = Query(
        None, description="Case-insensitive substring match on full name or email"
    ),
    user_service: UserService = Depends(get_user_service),
    _: User = Depends(require_roles(UserRole.ADMIN)),
):
    skip = (page - 1) * page_size
    items, total = await user_service.list_users(
        skip=skip,
        limit=page_size,
        role=role,
        is_active=is_active,
        search=search,
    )
    return PaginatedUserResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{user_uuid}",
    response_model=UserResponse,
    summary="Get a user by UUID",
    description="Retrieve a single user by their public UUID. Admin only.",
)
async def get_user(
    user_uuid: str,
    user_service: UserService = Depends(get_user_service),
    _: User = Depends(require_roles(UserRole.ADMIN)),
):
    return await user_service.get_user_by_uuid(user_uuid)


@router.patch(
    "/{user_uuid}",
    response_model=UserResponse,
    summary="Update a user (admin)",
    description=(
        "Apply an admin-driven update (role and/or account status) to "
        "a target user's record. Admin only."
    ),
)
async def update_user(
    user_uuid: str,
    payload: UserAdminUpdate,
    user_service: UserService = Depends(get_user_service),
    _: User = Depends(require_roles(UserRole.ADMIN)),
):
    return await user_service.update_user(user_uuid, payload)


@router.post(
    "/{user_uuid}/deactivate",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    summary="Deactivate a user",
    description=(
        "Deactivate a target user's account, revoking their ability to "
        "authenticate. Admin only."
    ),
)
async def deactivate_user(
    user_uuid: str,
    user_service: UserService = Depends(get_user_service),
    _: User = Depends(require_roles(UserRole.ADMIN)),
):
    return await user_service.deactivate_user(user_uuid)


@router.post(
    "/{user_uuid}/reactivate",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    summary="Reactivate a user",
    description=(
        "Reactivate a previously deactivated target user's account, "
        "restoring their ability to authenticate. Admin only."
    ),
)
async def reactivate_user(
    user_uuid: str,
    user_service: UserService = Depends(get_user_service),
    _: User = Depends(require_roles(UserRole.ADMIN)),
):
    return await user_service.reactivate_user(user_uuid)


__all__ = ["router"]