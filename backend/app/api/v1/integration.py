"""
backend/app/api/v1/integration.py

REST API router for the Integration Management module of the
Enterprise Real Estate AI Copilot CRM.

Mirrors the existing API layer conventions used throughout the
project (e.g. `app/api/v1/task.py`, `app/api/v1/search.py`):
    - JWT authentication via `get_current_user`.
    - RBAC via `require_roles`, scoped per-endpoint to the minimum
      role set that operation requires.
    - Dependency-injected `AsyncSession` / repository / service chain,
      with the router owning the commit boundary: every mutating
      endpoint calls `session.commit()` after a successful service
      call; read-only endpoints never commit.
    - All domain/business exceptions are raised by the service layer
      as `app.core.exceptions.AppException` subclasses and translated
      to HTTP responses by the project's centralized exception
      handlers -- this router does not catch or translate them itself.
    - Swagger documentation via `summary`/`description`/`response_model`
      on every route, and an explicit `responses=` map for common
      error statuses.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_roles
from app.db.session import get_db
from app.models.integration import (
    AuthenticationType,
    Integration,
    IntegrationProvider,
    IntegrationStatus,
    IntegrationType,
)
from app.models.user import User, UserRole
from app.repositories.integration_repository import IntegrationRepository
from app.schemas.integration import (
    IntegrationCreate,
    IntegrationFilter,
    IntegrationHealthCheck,
    IntegrationListResponse,
    IntegrationPaginationParams,
    IntegrationResponse,
    IntegrationSortingParams,
    IntegrationStatisticsResponse,
    IntegrationStatusUpdate,
    IntegrationUpdate,
)
from app.services.integration_service import IntegrationService

__all__ = ["router"]

router = APIRouter(prefix="/integrations", tags=["Integrations"])

#: Roles permitted to create, mutate, enable/disable, or health-check
#: integrations. Integration configuration touches credentials for
#: every external system the CRM talks to, so write access is
#: restricted to elevated roles. `require_roles` takes `UserRole`
#: members, not raw strings; `app.models.user.UserRole` only defines
#: ADMIN, SALES_MANAGER, and SALES_AGENT.
INTEGRATION_WRITE_ROLES: tuple[UserRole, ...] = (UserRole.ADMIN,)

#: Roles permitted to read integration configuration/status/statistics.
#: Slightly broader than write access, but still excludes low-privilege
#: roles since even non-secret configuration (bucket names, webhook
#: URLs, model names) is sensitive operational data.
INTEGRATION_READ_ROLES: tuple[UserRole, ...] = (UserRole.ADMIN, UserRole.SALES_MANAGER)

_COMMON_ERROR_RESPONSES: dict[int, dict] = {
    status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid credentials."},
    status.HTTP_403_FORBIDDEN: {"description": "Caller lacks the required role."},
}


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
async def get_integration_repository(
    session: AsyncSession = Depends(get_db),
) -> IntegrationRepository:
    """Builds an `IntegrationRepository` bound to the request-scoped session.

    Args:
        session: The request-scoped async SQLAlchemy session.

    Returns:
        IntegrationRepository: A repository instance bound to `session`.
    """
    return IntegrationRepository(session)


async def get_integration_service(
    repository: IntegrationRepository = Depends(get_integration_repository),
) -> IntegrationService:
    """Builds an `IntegrationService` bound to the request-scoped repository.

    Args:
        repository: The request-scoped `IntegrationRepository`.

    Returns:
        IntegrationService: A service instance bound to `repository`.
    """
    return IntegrationService(repository)


def _to_response(integration: Integration) -> IntegrationResponse:
    """Builds an `IntegrationResponse` from a persisted ORM instance.

    Explicitly derives `has_credentials` from the ORM instance, since
    `IntegrationResponse` deliberately has no `credentials` field and
    cannot compute this flag via `from_attributes` alone.

    Args:
        integration: The persisted `Integration` ORM instance.

    Returns:
        IntegrationResponse: The response representation, with
        credentials content never included.
    """
    response = IntegrationResponse.model_validate(integration)
    return response.model_copy(
        update={"has_credentials": integration.credentials is not None}
    )


# ---------------------------------------------------------------------------
# Statistics (registered ahead of the `{integration_id}` routes below so
# the literal path segment is never captured as a path parameter)
# ---------------------------------------------------------------------------
@router.get(
    "/statistics",
    response_model=IntegrationStatisticsResponse,
    summary="Get integration statistics",
    description=(
        "Returns aggregate statistics over all non-deleted integrations: "
        "counts by type, provider, status, and authentication type, plus "
        "active/failed/default counts and the most recent sync/health-check "
        "timestamps."
    ),
    responses=_COMMON_ERROR_RESPONSES,
)
async def get_integration_statistics(
    current_user: User = Depends(get_current_user),
    _roles: None = Depends(require_roles(*INTEGRATION_READ_ROLES)),
    service: IntegrationService = Depends(get_integration_service),
) -> IntegrationStatisticsResponse:
    """Retrieves aggregate integration statistics.

    Args:
        current_user: The authenticated caller.
        _roles: RBAC gate; unused beyond enforcing role membership.
        service: The injected `IntegrationService`.

    Returns:
        IntegrationStatisticsResponse: The computed aggregate statistics.
    """
    return await service.get_statistics()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@router.post(
    "",
    response_model=IntegrationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new integration",
    description=(
        "Creates a new external-system integration (email, SMS, WhatsApp, "
        "calendar, storage, notification, AI provider, payment gateway, "
        "webhook, or custom REST API). Validates the provider/type pairing, "
        "authentication/credentials requirement, provider-specific "
        "configuration completeness, and base/webhook URL requirements."
    ),
    responses={
        **_COMMON_ERROR_RESPONSES,
        status.HTTP_409_CONFLICT: {
            "description": "An integration with the same name already exists."
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Request failed schema or business-rule validation."
        },
    },
)
async def create_integration(
    payload: IntegrationCreate,
    current_user: User = Depends(get_current_user),
    _roles: None = Depends(require_roles(*INTEGRATION_WRITE_ROLES)),
    service: IntegrationService = Depends(get_integration_service),
    session: AsyncSession = Depends(get_db),
) -> IntegrationResponse:
    """Creates a new integration owned by the authenticated caller.

    Args:
        payload: The validated creation payload.
        current_user: The authenticated caller, recorded as `created_by`.
        _roles: RBAC gate; unused beyond enforcing role membership.
        service: The injected `IntegrationService`.
        session: The request-scoped session, committed on success.

    Returns:
        IntegrationResponse: The newly created integration.
    """
    integration = await service.create_integration(
        payload, created_by_id=current_user.id
    )
    await session.commit()
    return _to_response(integration)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
@router.get(
    "",
    response_model=IntegrationListResponse,
    summary="List integrations",
    description=(
        "Returns a filtered, sorted, paginated listing of integrations. "
        "Supports filtering by integration type, provider, status, "
        "authentication type, default flag, creation-date range, and "
        "free-text search against name/base URL."
    ),
    responses={
        **_COMMON_ERROR_RESPONSES,
        status.HTTP_404_NOT_FOUND: {
            "description": "The requested page is beyond the last available page."
        },
    },
)
async def list_integrations(
    current_user: User = Depends(get_current_user),
    _roles: None = Depends(require_roles(*INTEGRATION_READ_ROLES)),
    service: IntegrationService = Depends(get_integration_service),
    integration_type: Optional[IntegrationType] = Query(
        default=None, description="Restrict to a specific integration type."
    ),
    provider: Optional[IntegrationProvider] = Query(
        default=None, description="Restrict to a specific provider."
    ),
    status_: Optional[IntegrationStatus] = Query(
        default=None, alias="status", description="Restrict to a specific status."
    ),
    authentication_type: Optional[AuthenticationType] = Query(
        default=None, description="Restrict to a specific authentication type."
    ),
    is_default: Optional[bool] = Query(
        default=None, description="Restrict to default/non-default integrations."
    ),
    search: Optional[str] = Query(
        default=None,
        max_length=150,
        description="Free-text match against the integration name/base URL.",
    ),
    created_from: Optional[datetime] = Query(
        default=None, description="Inclusive lower bound on created_at."
    ),
    created_to: Optional[datetime] = Query(
        default=None, description="Inclusive upper bound on created_at."
    ),
    page: int = Query(default=1, ge=1, description="1-indexed page number."),
    page_size: int = Query(
        default=20, ge=1, le=200, description="Number of items per page."
    ),
    sort_by: str = Query(default="created_at", description="Column to sort by."),
    sort_order: str = Query(default="desc", description="'asc' or 'desc'."),
) -> IntegrationListResponse:
    """Lists integrations per the supplied filter/sort/pagination criteria.

    Args:
        current_user: The authenticated caller.
        _roles: RBAC gate; unused beyond enforcing role membership.
        service: The injected `IntegrationService`.
        integration_type: Optional integration type filter.
        provider: Optional provider filter.
        status_: Optional status filter (bound from the `status` query param).
        authentication_type: Optional authentication type filter.
        is_default: Optional default-flag filter.
        search: Optional free-text search term.
        created_from: Optional inclusive lower bound on `created_at`.
        created_to: Optional inclusive upper bound on `created_at`.
        page: 1-indexed page number.
        page_size: Number of items per page.
        sort_by: Column name to sort by.
        sort_order: Sort direction, `"asc"` or `"desc"`.

    Returns:
        IntegrationListResponse: The paginated, filtered, sorted listing.
    """
    filters = IntegrationFilter(
        integration_type=integration_type,
        provider=provider,
        status=status_,
        authentication_type=authentication_type,
        is_default=is_default,
        search=search,
        created_from=created_from,
        created_to=created_to,
    )
    pagination = IntegrationPaginationParams(page=page, page_size=page_size)
    sorting = IntegrationSortingParams(sort_by=sort_by, sort_order=sort_order)

    return await service.list_integrations(
        filters=filters, pagination=pagination, sorting=sorting
    )


# ---------------------------------------------------------------------------
# Retrieve
# ---------------------------------------------------------------------------
@router.get(
    "/{integration_id}",
    response_model=IntegrationResponse,
    summary="Get an integration by id",
    description="Returns a single, non-deleted integration by its id.",
    responses={
        **_COMMON_ERROR_RESPONSES,
        status.HTTP_404_NOT_FOUND: {"description": "Integration not found."},
    },
)
async def get_integration(
    integration_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    _roles: None = Depends(require_roles(*INTEGRATION_READ_ROLES)),
    service: IntegrationService = Depends(get_integration_service),
) -> IntegrationResponse:
    """Retrieves a single integration by id.

    Args:
        integration_id: Surrogate primary key of the integration.
        current_user: The authenticated caller.
        _roles: RBAC gate; unused beyond enforcing role membership.
        service: The injected `IntegrationService`.

    Returns:
        IntegrationResponse: The requested integration.
    """
    integration = await service.get_integration(integration_id)
    return _to_response(integration)


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------
@router.put(
    "/{integration_id}",
    response_model=IntegrationResponse,
    summary="Update an integration",
    description=(
        "Applies a partial update to an existing integration. Re-validates "
        "authentication/credentials, configuration, base/webhook URL, "
        "timeout, and retry-count rules against the resulting field values."
    ),
    responses={
        **_COMMON_ERROR_RESPONSES,
        status.HTTP_404_NOT_FOUND: {"description": "Integration not found."},
        status.HTTP_409_CONFLICT: {
            "description": "Renaming would collide with another integration's name."
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Request failed schema or business-rule validation."
        },
    },
)
async def update_integration(
    integration_id: uuid.UUID,
    payload: IntegrationUpdate,
    current_user: User = Depends(get_current_user),
    _roles: None = Depends(require_roles(*INTEGRATION_WRITE_ROLES)),
    service: IntegrationService = Depends(get_integration_service),
    session: AsyncSession = Depends(get_db),
) -> IntegrationResponse:
    """Applies a partial update to an existing integration.

    Args:
        integration_id: Surrogate primary key of the integration to update.
        payload: The validated partial-update payload.
        current_user: The authenticated caller.
        _roles: RBAC gate; unused beyond enforcing role membership.
        service: The injected `IntegrationService`.
        session: The request-scoped session, committed on success.

    Returns:
        IntegrationResponse: The updated integration.
    """
    integration = await service.update_integration(integration_id, payload)
    await session.commit()
    return _to_response(integration)


# ---------------------------------------------------------------------------
# Enable / disable
# ---------------------------------------------------------------------------
@router.patch(
    "/{integration_id}/enable",
    response_model=IntegrationResponse,
    summary="Enable an integration",
    description="Transitions an integration's status to `active`.",
    responses={
        **_COMMON_ERROR_RESPONSES,
        status.HTTP_404_NOT_FOUND: {"description": "Integration not found."},
        status.HTTP_409_CONFLICT: {
            "description": "Integration is already active."
        },
    },
)
async def enable_integration(
    integration_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    _roles: None = Depends(require_roles(*INTEGRATION_WRITE_ROLES)),
    service: IntegrationService = Depends(get_integration_service),
    session: AsyncSession = Depends(get_db),
) -> IntegrationResponse:
    """Transitions an integration to `ACTIVE`.

    Args:
        integration_id: Surrogate primary key of the integration to enable.
        current_user: The authenticated caller.
        _roles: RBAC gate; unused beyond enforcing role membership.
        service: The injected `IntegrationService`.
        session: The request-scoped session, committed on success.

    Returns:
        IntegrationResponse: The updated integration.
    """
    integration = await service.enable_integration(integration_id)
    await session.commit()
    return _to_response(integration)


@router.patch(
    "/{integration_id}/disable",
    response_model=IntegrationResponse,
    summary="Disable an integration",
    description="Transitions an integration's status to `inactive`.",
    responses={
        **_COMMON_ERROR_RESPONSES,
        status.HTTP_404_NOT_FOUND: {"description": "Integration not found."},
        status.HTTP_409_CONFLICT: {
            "description": "Integration is already inactive."
        },
    },
)
async def disable_integration(
    integration_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    _roles: None = Depends(require_roles(*INTEGRATION_WRITE_ROLES)),
    service: IntegrationService = Depends(get_integration_service),
    session: AsyncSession = Depends(get_db),
) -> IntegrationResponse:
    """Transitions an integration to `INACTIVE`.

    Args:
        integration_id: Surrogate primary key of the integration to disable.
        current_user: The authenticated caller.
        _roles: RBAC gate; unused beyond enforcing role membership.
        service: The injected `IntegrationService`.
        session: The request-scoped session, committed on success.

    Returns:
        IntegrationResponse: The updated integration.
    """
    integration = await service.disable_integration(integration_id)
    await session.commit()
    return _to_response(integration)


# ---------------------------------------------------------------------------
# Status transitions (arbitrary)
# ---------------------------------------------------------------------------
@router.patch(
    "/{integration_id}/status",
    response_model=IntegrationResponse,
    summary="Transition an integration's status",
    description="Applies an arbitrary status transition, with an optional audit reason.",
    responses={
        **_COMMON_ERROR_RESPONSES,
        status.HTTP_404_NOT_FOUND: {"description": "Integration not found."},
        status.HTTP_409_CONFLICT: {
            "description": "Integration is already in the requested status."
        },
    },
)
async def update_integration_status(
    integration_id: uuid.UUID,
    payload: IntegrationStatusUpdate,
    current_user: User = Depends(get_current_user),
    _roles: None = Depends(require_roles(*INTEGRATION_WRITE_ROLES)),
    service: IntegrationService = Depends(get_integration_service),
    session: AsyncSession = Depends(get_db),
) -> IntegrationResponse:
    """Applies an arbitrary status transition to an integration.

    Args:
        integration_id: Surrogate primary key of the integration.
        payload: The requested status transition and optional audit reason.
        current_user: The authenticated caller.
        _roles: RBAC gate; unused beyond enforcing role membership.
        service: The injected `IntegrationService`.
        session: The request-scoped session, committed on success.

    Returns:
        IntegrationResponse: The updated integration.
    """
    integration = await service.update_status(integration_id, payload)
    await session.commit()
    return _to_response(integration)


# ---------------------------------------------------------------------------
# Connection test / health check
# ---------------------------------------------------------------------------
@router.post(
    "/{integration_id}/test-connection",
    response_model=IntegrationHealthCheck,
    summary="Test an integration's connection readiness",
    description=(
        "Performs a structural readiness check: confirms the integration "
        "has everything it would need to attempt a live outbound call "
        "(required configuration keys, base/webhook URL, credentials). "
        "Does not persist the outcome and does not perform live network I/O."
    ),
    responses={
        **_COMMON_ERROR_RESPONSES,
        status.HTTP_404_NOT_FOUND: {"description": "Integration not found."},
    },
)
async def test_integration_connection(
    integration_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    _roles: None = Depends(require_roles(*INTEGRATION_WRITE_ROLES)),
    service: IntegrationService = Depends(get_integration_service),
) -> IntegrationHealthCheck:
    """Runs a structural connection-readiness test against an integration.

    Args:
        integration_id: Surrogate primary key of the integration to test.
        current_user: The authenticated caller.
        _roles: RBAC gate; unused beyond enforcing role membership.
        service: The injected `IntegrationService`.

    Returns:
        IntegrationHealthCheck: The (unpersisted) outcome of the test.
    """
    return await service.test_connection(integration_id)


@router.post(
    "/{integration_id}/health-check",
    response_model=IntegrationHealthCheck,
    summary="Run and persist an integration health check",
    description=(
        "Runs the same structural readiness check as `test-connection`, "
        "then persists the resulting status and health-check timestamp "
        "on the integration."
    ),
    responses={
        **_COMMON_ERROR_RESPONSES,
        status.HTTP_404_NOT_FOUND: {"description": "Integration not found."},
    },
)
async def health_check_integration(
    integration_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    _roles: None = Depends(require_roles(*INTEGRATION_WRITE_ROLES)),
    service: IntegrationService = Depends(get_integration_service),
    session: AsyncSession = Depends(get_db),
) -> IntegrationHealthCheck:
    """Runs a health check against an integration and persists the outcome.

    Args:
        integration_id: Surrogate primary key of the integration to check.
        current_user: The authenticated caller.
        _roles: RBAC gate; unused beyond enforcing role membership.
        service: The injected `IntegrationService`.
        session: The request-scoped session, committed on success.

    Returns:
        IntegrationHealthCheck: The persisted outcome of the health check.
    """
    outcome = await service.perform_health_check(integration_id)
    await session.commit()
    return outcome