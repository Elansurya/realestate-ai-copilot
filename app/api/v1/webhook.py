"""
backend/app/api/v1/webhook.py

FastAPI v1 REST API router for the Enterprise Webhook module of the
Enterprise Real Estate AI Copilot CRM.

Follows the project's API-layer conventions:
    - JWT authentication via `get_current_user` (from `app.api.deps`).
    - RBAC via a `require_roles(...)` dependency (from `app.api.deps`),
      applied per-route according to mutating vs. read-only access.
    - Async SQLAlchemy session injected via `get_db` (from
      `app.api.deps`).
    - All business logic delegated to `app.services.webhook_service.
      WebhookService`; this module contains no query building,
      persistence, or business-rule validation of its own.
    - Domain exceptions raised by the Service layer (`NotFoundException`,
      `ConflictException`, `ValidationException`, `BusinessRuleException`)
      are NOT caught here -- they propagate to the project's global
      exception handlers (registered on the FastAPI app instance),
      matching the convention already used by sibling routers.
    - Pagination / filtering / sorting / search are exposed uniformly
      via the `WebhookFilter` / `WebhookLogFilter` schemas, used as
      `Depends()` dependencies so each field becomes its own OpenAPI
      query parameter.
    - Full Swagger/OpenAPI documentation via `summary`, `description`,
      `response_model`, and explicit `status_code` on every route.

Dependency import note:
    `get_db`, `get_current_user`, and `require_roles` are imported from
    `app.api.deps`, matching the shared dependency module already used
    by sibling v1 routers. If any of these names differ slightly in
    this project's actual `app.api.deps` module, only the import line
    below needs to be adjusted -- every route below uses them only as
    ordinary FastAPI dependencies.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, require_roles
from app.core.exceptions import AppException, NotFoundException
from app.models.user import User, UserRole
from app.models.webhook import DeliveryStatus
from app.schemas.webhook import (
    WebhookCreate,
    WebhookFilter,
    WebhookListResponse,
    WebhookLogFilter,
    WebhookLogListResponse,
    WebhookLogResponse,
    WebhookResponse,
    WebhookStatisticsResponse,
    WebhookUpdate,
)
from app.services.webhook_service import WebhookService

__all__ = ["router"]

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])

#: Roles permitted to create/modify/delete/enable/disable/test/retry
#: webhooks. Read-only endpoints only require an authenticated user
#: (see `get_current_user` below). `require_roles` takes `UserRole`
#: members, not raw strings; `app.models.user.UserRole` only defines
#: ADMIN, SALES_MANAGER, and SALES_AGENT -- there is no separate
#: webhook_manager/integration_manager role.
_WEBHOOK_MANAGE_ROLES = (UserRole.ADMIN, UserRole.SALES_MANAGER)


def get_webhook_service(session: AsyncSession = Depends(get_db)) -> WebhookService:
    """Builds a request-scoped `WebhookService` bound to the injected
    async session.

    Args:
        session: The request-scoped `AsyncSession`, injected via `get_db`.

    Returns:
        WebhookService: A service instance ready to handle this request.
    """
    return WebhookService(session)


# ---------------------------------------------------------------------------
# Statistics (registered ahead of `/{webhook_id}` to avoid path collision)
# ---------------------------------------------------------------------------
@router.get(
    "/statistics",
    response_model=WebhookStatisticsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get webhook delivery statistics",
    description=(
        "Returns aggregate webhook and delivery statistics: counts by "
        "lifecycle status and event, delivery success rate, average "
        "delivery duration, and most recent delivery timestamp. Pass "
        "`webhook_id` to scope statistics to a single webhook."
    ),
)
async def get_statistics(
    webhook_id: uuid.UUID | None = Query(
        default=None,
        description="Optional webhook id to scope statistics to a single webhook.",
    ),
    current_user: User = Depends(get_current_user),
    service: WebhookService = Depends(get_webhook_service),
) -> WebhookStatisticsResponse:
    """Retrieves aggregate delivery statistics.

    Args:
        webhook_id: Optional webhook id to scope statistics to.
        current_user: The authenticated caller (any authenticated role).
        service: The injected `WebhookService`.

    Returns:
        WebhookStatisticsResponse: The computed statistics.
    """
    return await service.get_statistics(webhook_id=webhook_id)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@router.post(
    "",
    response_model=WebhookResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new webhook",
    description=(
        "Registers a new outbound webhook subscription for a domain "
        "event. Validates the target URL (including a baseline SSRF "
        "guard), authentication/secret pairing, custom headers, and "
        "payload template before persisting."
    ),
    dependencies=[Depends(require_roles(*_WEBHOOK_MANAGE_ROLES))],
)
async def create_webhook(
    payload: WebhookCreate,
    current_user: User = Depends(get_current_user),
    service: WebhookService = Depends(get_webhook_service),
) -> WebhookResponse:
    """Registers a new webhook subscription.

    Args:
        payload: The validated creation payload.
        current_user: The authenticated caller; recorded as `created_by`.
        service: The injected `WebhookService`.

    Returns:
        WebhookResponse: The newly created webhook.
    """
    return await service.create_webhook(payload, created_by_id=current_user.id)


# ---------------------------------------------------------------------------
# List / Filter / Search / Paginate / Sort
# ---------------------------------------------------------------------------
@router.get(
    "",
    response_model=WebhookListResponse,
    status_code=status.HTTP_200_OK,
    summary="List webhooks",
    description=(
        "Lists registered webhooks with filtering (event, status, "
        "authentication type, enabled flag, soft-delete state, "
        "creation-date range), free-text search over `name` and "
        "`target_url` via the `search` parameter, pagination, and "
        "sorting."
    ),
)
async def list_webhooks(
    filter_: WebhookFilter = Depends(),
    current_user: User = Depends(get_current_user),
    service: WebhookService = Depends(get_webhook_service),
) -> WebhookListResponse:
    """Lists webhooks matching filter/search/pagination/sort criteria.

    Args:
        filter_: Combined filter, search, pagination, and sort criteria.
        current_user: The authenticated caller (any authenticated role).
        service: The injected `WebhookService`.

    Returns:
        WebhookListResponse: The paginated, matching webhooks.
    """
    return await service.list_webhooks(filter_)


# ---------------------------------------------------------------------------
# Get by id
# ---------------------------------------------------------------------------
@router.get(
    "/{webhook_id}",
    response_model=WebhookResponse,
    status_code=status.HTTP_200_OK,
    summary="Get a webhook by id",
    description="Fetches a single, non-deleted webhook by its unique id.",
)
async def get_webhook(
    webhook_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    service: WebhookService = Depends(get_webhook_service),
) -> WebhookResponse:
    """Fetches a single webhook.

    Args:
        webhook_id: Identifier of the webhook to fetch.
        current_user: The authenticated caller (any authenticated role).
        service: The injected `WebhookService`.

    Returns:
        WebhookResponse: The matching webhook.

    Raises:
        NotFoundException: If no matching, non-deleted webhook exists
            (translated to HTTP 404 by the global exception handler).
    """
    try:
        return await service.get_webhook(webhook_id)
    except AppException as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------
@router.put(
    "/{webhook_id}",
    response_model=WebhookResponse,
    status_code=status.HTTP_200_OK,
    summary="Update a webhook",
    description=(
        "Partially updates an existing webhook (PATCH semantics -- "
        "only supplied fields are applied). Re-validates every "
        "changed field against the module's business rules."
    ),
    dependencies=[Depends(require_roles(*_WEBHOOK_MANAGE_ROLES))],
)
async def update_webhook(
    webhook_id: uuid.UUID,
    payload: WebhookUpdate,
    current_user: User = Depends(get_current_user),
    service: WebhookService = Depends(get_webhook_service),
) -> WebhookResponse:
    """Updates an existing webhook.

    Args:
        webhook_id: Identifier of the webhook to update.
        payload: The validated partial update payload.
        current_user: The authenticated caller.
        service: The injected `WebhookService`.

    Returns:
        WebhookResponse: The updated webhook.

    Raises:
        NotFoundException: If no matching, non-deleted webhook exists.
        ConflictException: If renaming to a `name` already in use.
        ValidationException: If any business rule fails.
    """
    return await service.update_webhook(webhook_id, payload)


# ---------------------------------------------------------------------------
# Delete (soft delete)
# ---------------------------------------------------------------------------
@router.delete(
    "/{webhook_id}",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a webhook",
    description="Soft-deletes a webhook, excluding it from delivery and default listings.",
    dependencies=[Depends(require_roles(*_WEBHOOK_MANAGE_ROLES))],
)
async def delete_webhook(
    webhook_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    service: WebhookService = Depends(get_webhook_service),
) -> None:
    """Soft-deletes a webhook.

    Args:
        webhook_id: Identifier of the webhook to delete.
        current_user: The authenticated caller.
        service: The injected `WebhookService`.

    Raises:
        NotFoundException: If no matching, non-deleted webhook exists.
    """
    await service.delete_webhook(webhook_id)


# ---------------------------------------------------------------------------
# Enable / Disable
# ---------------------------------------------------------------------------
@router.patch(
    "/{webhook_id}/enable",
    response_model=WebhookResponse,
    status_code=status.HTTP_200_OK,
    summary="Enable a webhook",
    description="Enables a webhook for delivery. Blocked while the webhook's status is 'suspended' or 'failed'.",
    dependencies=[Depends(require_roles(*_WEBHOOK_MANAGE_ROLES))],
)
async def enable_webhook(
    webhook_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    service: WebhookService = Depends(get_webhook_service),
) -> WebhookResponse:
    """Enables a webhook for delivery.

    Args:
        webhook_id: Identifier of the webhook to enable.
        current_user: The authenticated caller.
        service: The injected `WebhookService`.

    Returns:
        WebhookResponse: The updated webhook.

    Raises:
        NotFoundException: If no matching, non-deleted webhook exists.
        BusinessRuleException: If the webhook's status is `suspended`
            or `failed`.
    """
    try:
        return await service.enable_webhook(webhook_id)
    except AppException as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=str(exc),
        ) from exc


@router.patch(
    "/{webhook_id}/disable",
    response_model=WebhookResponse,
    status_code=status.HTTP_200_OK,
    summary="Disable a webhook",
    description="Disables a webhook, excluding it from delivery until re-enabled.",
    dependencies=[Depends(require_roles(*_WEBHOOK_MANAGE_ROLES))],
)
async def disable_webhook(
    webhook_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    service: WebhookService = Depends(get_webhook_service),
) -> WebhookResponse:
    """Disables a webhook.

    Args:
        webhook_id: Identifier of the webhook to disable.
        current_user: The authenticated caller.
        service: The injected `WebhookService`.

    Returns:
        WebhookResponse: The updated webhook.

    Raises:
        NotFoundException: If no matching, non-deleted webhook exists.
    """
    return await service.disable_webhook(webhook_id)


# ---------------------------------------------------------------------------
# Test Delivery
# ---------------------------------------------------------------------------
@router.post(
    "/{webhook_id}/test",
    response_model=WebhookLogResponse,
    status_code=status.HTTP_200_OK,
    summary="Send a test delivery",
    description=(
        "Sends a synthetic test payload to the webhook's target URL "
        "and logs the attempt like any other delivery, so the result "
        "is visible in delivery history and statistics."
    ),
    dependencies=[Depends(require_roles(*_WEBHOOK_MANAGE_ROLES))],
)
async def test_webhook(
    webhook_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    service: WebhookService = Depends(get_webhook_service),
) -> WebhookLogResponse:
    """Sends a synthetic test delivery to a webhook.

    Args:
        webhook_id: Identifier of the webhook to test.
        current_user: The authenticated caller.
        service: The injected `WebhookService`.

    Returns:
        WebhookLogResponse: The test delivery attempt's outcome.

    Raises:
        NotFoundException: If no matching, non-deleted webhook exists.
    """
    return await service.test_webhook(webhook_id)


# ---------------------------------------------------------------------------
# Retry Failed Delivery
# ---------------------------------------------------------------------------
@router.post(
    "/{webhook_id}/retry",
    response_model=WebhookLogResponse,
    status_code=status.HTTP_200_OK,
    summary="Retry a failed delivery",
    description=(
        "Retries a delivery for this webhook. Pass `log_id` to retry a "
        "specific delivery attempt; when omitted, the webhook's most "
        "recent `failed` delivery log is retried."
    ),
    dependencies=[Depends(require_roles(*_WEBHOOK_MANAGE_ROLES))],
)
async def retry_delivery(
    webhook_id: uuid.UUID,
    log_id: uuid.UUID | None = Query(
        default=None,
        description="Specific delivery log id to retry. Defaults to the most recent failed delivery for this webhook.",
    ),
    current_user: User = Depends(get_current_user),
    service: WebhookService = Depends(get_webhook_service),
) -> WebhookLogResponse:
    """Retries a failed delivery attempt for a webhook.

    Args:
        webhook_id: Identifier of the parent webhook.
        log_id: Specific delivery log id to retry; when omitted, the
            most recent `failed` log for this webhook is used.
        current_user: The authenticated caller.
        service: The injected `WebhookService`.

    Returns:
        WebhookLogResponse: The newly created retry attempt's outcome.

    Raises:
        NotFoundException: If `webhook_id` does not match a
            non-deleted webhook, or if no eligible failed delivery
            log exists to retry (whether explicitly via `log_id` or
            implicitly, when omitted).
        BusinessRuleException: If the webhook is disabled or has
            exhausted its configured `retry_count` for that delivery.
    """
    try:
        target_log_id = log_id
        if target_log_id is None:
            recent_failed = await service.get_delivery_logs(
                WebhookLogFilter(
                    webhook_id=webhook_id,
                    delivery_status=DeliveryStatus.FAILED,
                    page=1,
                    page_size=1,
                    sort_by="delivered_at",
                    sort_order="desc",
                )
            )
            if not recent_failed.items:
                raise NotFoundException(
                    f"No failed delivery log was found for webhook '{webhook_id}' to retry."
                )
            target_log_id = recent_failed.items[0].id

        return await service.retry_delivery(target_log_id)
    except AppException as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# Delivery Logs
# ---------------------------------------------------------------------------
@router.get(
    "/{webhook_id}/logs",
    response_model=WebhookLogListResponse,
    status_code=status.HTTP_200_OK,
    summary="List delivery logs for a webhook",
    description=(
        "Lists this webhook's delivery attempt history, with filtering "
        "by delivery status and delivered-at date range, pagination, "
        "and sorting -- the audit trail backing DLQ triage."
    ),
)
async def get_delivery_logs(
    webhook_id: uuid.UUID,
    log_filter: WebhookLogFilter = Depends(),
    current_user: User = Depends(get_current_user),
    service: WebhookService = Depends(get_webhook_service),
) -> WebhookLogListResponse:
    """Lists delivery log entries for a specific webhook.

    Args:
        webhook_id: Identifier of the parent webhook (taken from the
            path and merged into `log_filter`, overriding any
            `webhook_id` query value).
        log_filter: Combined filter, pagination, and sort criteria.
        current_user: The authenticated caller (any authenticated role).
        service: The injected `WebhookService`.

    Returns:
        WebhookLogListResponse: The paginated, matching log entries.

    Raises:
        NotFoundException: If `webhook_id` does not match a
            non-deleted webhook.
    """
    scoped_filter = log_filter.model_copy(update={"webhook_id": webhook_id})
    return await service.get_delivery_logs(scoped_filter)