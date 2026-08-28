"""
backend/app/api/v1/settings.py

Settings Module API Endpoints.

Router layer only -- all business logic lives in `SettingsService`, all
persistence lives in `SettingsRepository`, and hot-path configuration
reads are served through `app.utils.settings_cache`. Follows the same
structure/style as `app/api/v1/property.py` / `app/api/v1/users.py`:

    Router -> Service -> Repository -> Database
                     \\-> Cache (utils/settings_cache.py)

RBAC summary:
    - Read endpoints (list/get/statistics/public/category) are open to
      any authenticated user.
    - Mutating endpoints (create/update/patch/delete/bulk-update/
      bulk-delete/cache-reload) are restricted to `UserRole.ADMIN` --
      the sole administrative role defined on `User` in this project.
"""

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth_dependency import get_current_user
from app.api.dependencies.rbac import require_roles
from app.db.session import get_db
from app.models.settings import SettingCategory, SettingDataType
from app.models.user import User, UserRole
from app.repositories.settings_repository import SettingsRepository
from app.schemas.settings import (
    SettingsCreate,
    SettingsFilter,
    SettingsListResponse,
    SettingsResponse,
    SettingsStatisticsResponse,
    SettingsUpdate,
)
from app.services.settings_service import SettingsService
from app.utils import settings_cache

router = APIRouter(prefix="/settings", tags=["Settings"])


# ---------------------------------------------------------------------------
# Service factory
# ---------------------------------------------------------------------------
def get_settings_service(db: AsyncSession = Depends(get_db)) -> SettingsService:
    """Builds a request-scoped `SettingsService` wired to its repository.

    Args:
        db: The active asynchronous SQLAlchemy session, injected via `get_db`.

    Returns:
        SettingsService: A service instance ready to handle the current request.
    """
    repository = SettingsRepository(db)
    return SettingsService(repository)


# ---------------------------------------------------------------------------
# Local request/response payload models
# (No dedicated schema module for these composite/bulk shapes; mirrors the
#  same convention already used for small inline models in
#  `app/api/v1/booking.py` and `app/api/v1/lead.py`.)
# ---------------------------------------------------------------------------
class SettingsBulkUpdateItem(BaseModel):
    """A single (id, partial-update) pair within a bulk-update request."""

    setting_id: uuid.UUID = Field(
        ..., description="UUID of the setting entry to update.", examples=["b3f1c2a0-1234-4a5b-9abc-1234567890ab"]
    )
    payload: SettingsUpdate = Field(..., description="Partial update to apply.")


class SettingsBulkUpdateRequest(BaseModel):
    """Request payload for updating multiple setting entries in one call."""

    updates: list[SettingsBulkUpdateItem] = Field(
        ..., min_length=1, description="Batch of (setting_id, payload) pairs to update."
    )


class SettingsBulkDeleteRequest(BaseModel):
    """Request payload for deleting multiple setting entries in one call."""

    ids: list[uuid.UUID] = Field(
        ..., min_length=1, description="UUIDs of the setting entries to delete."
    )


class SettingsBulkDeleteResponse(BaseModel):
    """Response payload reporting the outcome of a bulk-delete request."""

    deleted_count: int = Field(..., ge=0, examples=[3])


class CacheReloadResponse(BaseModel):
    """Response payload reporting the outcome of a cache reload."""

    reloaded: bool = Field(..., examples=[True])
    category: Optional[SettingCategory] = Field(
        default=None, description="Category reloaded, or null if the entire cache was reloaded."
    )
    total_entries: int = Field(..., ge=0, examples=[128])
    reloaded_at: datetime


class CacheStatusResponse(BaseModel):
    """Response payload describing the current state of the settings cache."""

    total_entries: int = Field(..., ge=0)
    categories_cached: list[str] = Field(default_factory=list)
    hit_count: int = Field(..., ge=0)
    miss_count: int = Field(..., ge=0)
    hit_rate: float = Field(..., ge=0.0, le=1.0)
    reload_count: int = Field(..., ge=0)
    last_loaded_at: Optional[datetime] = None
    is_loaded: bool


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@router.post(
    "",
    response_model=SettingsResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new setting",
    description=(
        "Creates a new system configuration entry. Validates the "
        "setting_key format, the value against its declared data_type, "
        "any validation_rules, and the encrypted/public flag "
        "combination. Restricted to Admin. On success, the settings "
        "cache is incrementally updated with the new entry."
    ),
    responses={
        400: {"description": "Invalid value/data_type/flag combination"},
        403: {"description": "Caller is not an Admin"},
        409: {"description": "A setting with the same (category, setting_key) already exists"},
        422: {"description": "Request body failed schema validation"},
    },
)
async def create_setting(
    payload: SettingsCreate,
    service: SettingsService = Depends(get_settings_service),
    current_user: User = Depends(require_roles(UserRole.ADMIN)),
) -> SettingsResponse:
    created = await service.create_setting(payload)
    await settings_cache.update_cache_entry(
        created.category,
        created.setting_key,
        created.setting_value,
        data_type=created.data_type,
        is_encrypted=created.is_encrypted,
    )
    return created


# ---------------------------------------------------------------------------
# List / Search / Filter / Paginate
# ---------------------------------------------------------------------------
@router.get(
    "",
    response_model=SettingsListResponse,
    status_code=status.HTTP_200_OK,
    summary="List and search settings",
    description=(
        "Retrieves a paginated, filterable, and sortable list of "
        "setting entries. Supports filtering by category, setting_key, "
        "data_type, is_public, is_editable, is_encrypted, free-text "
        "search, and created_at date range. Accessible to any "
        "authenticated user."
    ),
)
async def list_settings(
    category: Optional[SettingCategory] = Query(None, description="Filter by category"),
    setting_key: Optional[str] = Query(None, max_length=150, description="Filter by exact setting key"),
    data_type: Optional[SettingDataType] = Query(None, description="Filter by data type"),
    is_public: Optional[bool] = Query(None, description="Filter by public-exposure flag"),
    is_editable: Optional[bool] = Query(None, description="Filter by editability flag"),
    is_encrypted: Optional[bool] = Query(None, description="Filter by encryption flag"),
    search: Optional[str] = Query(None, max_length=255, description="Free-text search on setting_key/description"),
    date_from: Optional[datetime] = Query(None, description="Inclusive lower bound on created_at"),
    date_to: Optional[datetime] = Query(None, description="Inclusive upper bound on created_at"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    sort_by: str = Query("created_at"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    service: SettingsService = Depends(get_settings_service),
    current_user: User = Depends(get_current_user),
) -> SettingsListResponse:
    filters = SettingsFilter(
        category=category,
        setting_key=setting_key,
        data_type=data_type,
        is_public=is_public,
        is_editable=is_editable,
        is_encrypted=is_encrypted,
        search=search,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return await service.list_settings(filters)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
@router.get(
    "/statistics",
    response_model=SettingsStatisticsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get settings statistics",
    description=(
        "Computes aggregate statistics (totals, public/editable/"
        "encrypted counts, and breakdowns by category and data_type) "
        "over an optional created_at date range. Accessible to any "
        "authenticated user."
    ),
    responses={400: {"description": "date_from is after date_to"}},
)
async def get_settings_statistics(
    date_from: Optional[datetime] = Query(None, description="Inclusive lower bound on created_at"),
    date_to: Optional[datetime] = Query(None, description="Inclusive upper bound on created_at"),
    service: SettingsService = Depends(get_settings_service),
    current_user: User = Depends(get_current_user),
) -> SettingsStatisticsResponse:
    return await service.get_statistics(date_from=date_from, date_to=date_to)


# ---------------------------------------------------------------------------
# Public settings
# ---------------------------------------------------------------------------
@router.get(
    "/public",
    response_model=list[SettingsResponse],
    status_code=status.HTTP_200_OK,
    summary="Get publicly exposable settings",
    description=(
        "Retrieves every setting entry flagged as `is_public`. "
        "Accessible to any authenticated user, matching this "
        "project's existing pattern of gating all API access behind "
        "a valid JWT even for nominally 'public' configuration."
    ),
)
async def get_public_settings(
    service: SettingsService = Depends(get_settings_service),
    current_user: User = Depends(get_current_user),
) -> list[SettingsResponse]:
    return await service.get_public_settings()


# ---------------------------------------------------------------------------
# By category
# ---------------------------------------------------------------------------
@router.get(
    "/category/{category}",
    response_model=list[SettingsResponse],
    status_code=status.HTTP_200_OK,
    summary="Get settings by category",
    description="Retrieves every setting entry within a given functional category.",
    responses={422: {"description": "Invalid category value"}},
)
async def get_settings_by_category(
    category: SettingCategory,
    service: SettingsService = Depends(get_settings_service),
    current_user: User = Depends(get_current_user),
) -> list[SettingsResponse]:
    return await service.get_settings_by_category(category)


# ---------------------------------------------------------------------------
# Cache reload / status
# (Declared before the "/{setting_id}" routes below so the literal
#  "cache" path segment is never mistaken for a setting id.)
# ---------------------------------------------------------------------------
@router.post(
    "/cache/reload",
    response_model=CacheReloadResponse,
    status_code=status.HTTP_200_OK,
    summary="Reload the settings cache",
    description=(
        "Forces a clean rebuild of the in-process settings cache from "
        "the database, optionally scoped to a single category. "
        "Restricted to Admin."
    ),
    responses={403: {"description": "Caller is not an Admin"}},
)
async def reload_settings_cache(
    category: Optional[SettingCategory] = Query(
        None, description="Optional category to scope the reload to; omit to reload everything."
    ),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.ADMIN)),
) -> CacheReloadResponse:
    snapshot = await settings_cache.reload_cache(db, category=category)
    return CacheReloadResponse(
        reloaded=True,
        category=category,
        total_entries=len(snapshot),
        reloaded_at=datetime.utcnow(),
    )


@router.get(
    "/cache/status",
    response_model=CacheStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Get settings cache status",
    description=(
        "Reports the current state of the in-process settings cache: "
        "size, cached categories, cumulative hit/miss counters, and "
        "the timestamp of the most recent load or reload. Accessible "
        "to any authenticated user."
    ),
)
async def get_settings_cache_status(
    current_user: User = Depends(get_current_user),
) -> CacheStatusResponse:
    stats = await settings_cache.get_cache_statistics()
    return CacheStatusResponse(**stats)


# ---------------------------------------------------------------------------
# Bulk update / bulk delete
# (Declared before "/{setting_id}" so the literal "bulk-update" /
#  "bulk-delete" path segments are never mistaken for a setting id.)
# ---------------------------------------------------------------------------
@router.post(
    "/bulk-update",
    response_model=list[SettingsResponse],
    status_code=status.HTTP_200_OK,
    summary="Bulk update settings",
    description=(
        "Applies a bounded batch of partial updates to multiple "
        "setting entries in one call. Restricted to Admin. On "
        "success, the settings cache is incrementally updated for "
        "every entry in the batch."
    ),
    responses={
        400: {"description": "Empty batch, batch too large, or a value/flag validation failure"},
        403: {"description": "Caller is not an Admin"},
        404: {"description": "One or more referenced settings do not exist"},
    },
)
async def bulk_update_settings(
    payload: SettingsBulkUpdateRequest,
    service: SettingsService = Depends(get_settings_service),
    current_user: User = Depends(require_roles(UserRole.ADMIN)),
) -> list[SettingsResponse]:
    updates = [(item.setting_id, item.payload) for item in payload.updates]
    updated_entries = await service.bulk_update_settings(updates)
    for entry in updated_entries:
        await settings_cache.update_cache_entry(
            entry.category,
            entry.setting_key,
            entry.setting_value,
            data_type=entry.data_type,
            is_encrypted=entry.is_encrypted,
        )
    return updated_entries


@router.delete(
    "/bulk-delete",
    response_model=SettingsBulkDeleteResponse,
    status_code=status.HTTP_200_OK,
    summary="Bulk delete settings",
    description=(
        "Deletes a bounded batch of setting entries by id in one "
        "call. Protected system settings cannot be deleted. Restricted "
        "to Admin. On success, the settings cache is refreshed via a "
        "full reload."
    ),
    responses={
        400: {"description": "Empty batch or batch too large"},
        403: {"description": "Caller is not an Admin"},
        404: {"description": "One or more referenced settings do not exist"},
        409: {"description": "One or more referenced settings are protected system settings"},
    },
)
async def bulk_delete_settings(
    payload: SettingsBulkDeleteRequest,
    db: AsyncSession = Depends(get_db),
    service: SettingsService = Depends(get_settings_service),
    current_user: User = Depends(require_roles(UserRole.ADMIN)),
) -> SettingsBulkDeleteResponse:
    deleted_count = await service.bulk_delete_settings(payload.ids)
    await settings_cache.reload_cache(db)
    return SettingsBulkDeleteResponse(deleted_count=deleted_count)


# ---------------------------------------------------------------------------
# Get / Update / Patch / Delete by id
# ---------------------------------------------------------------------------
@router.get(
    "/{setting_id}",
    response_model=SettingsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get a setting by ID",
    responses={404: {"description": "Setting not found"}},
)
async def get_setting(
    setting_id: uuid.UUID,
    service: SettingsService = Depends(get_settings_service),
    current_user: User = Depends(get_current_user),
) -> SettingsResponse:
    return await service.get_setting(setting_id)


@router.put(
    "/{setting_id}",
    response_model=SettingsResponse,
    status_code=status.HTTP_200_OK,
    summary="Update a setting",
    description=(
        "Applies a partial update to an existing setting entry. "
        "`category` and `setting_key` are immutable once created. "
        "Restricted to Admin. On success, the settings cache is "
        "incrementally updated with the new value."
    ),
    responses={
        400: {"description": "Setting is not editable, or a value/flag validation failure"},
        403: {"description": "Caller is not an Admin"},
        404: {"description": "Setting not found"},
    },
)
async def update_setting(
    setting_id: uuid.UUID,
    payload: SettingsUpdate,
    service: SettingsService = Depends(get_settings_service),
    current_user: User = Depends(require_roles(UserRole.ADMIN)),
) -> SettingsResponse:
    updated = await service.update_setting(setting_id, payload)
    await settings_cache.update_cache_entry(
        updated.category,
        updated.setting_key,
        updated.setting_value,
        data_type=updated.data_type,
        is_encrypted=updated.is_encrypted,
    )
    return updated


@router.patch(
    "/{setting_id}",
    response_model=SettingsResponse,
    status_code=status.HTTP_200_OK,
    summary="Partially update a setting",
    description=(
        "Identical semantics to `PUT /settings/{setting_id}` -- "
        "`SettingsUpdate` fields are already all-optional, so PATCH is "
        "offered as an explicit, REST-conventional alias for partial "
        "modification. Restricted to Admin. On success, the settings "
        "cache is incrementally updated with the new value."
    ),
    responses={
        400: {"description": "Setting is not editable, or a value/flag validation failure"},
        403: {"description": "Caller is not an Admin"},
        404: {"description": "Setting not found"},
    },
)
async def patch_setting(
    setting_id: uuid.UUID,
    payload: SettingsUpdate,
    service: SettingsService = Depends(get_settings_service),
    current_user: User = Depends(require_roles(UserRole.ADMIN)),
) -> SettingsResponse:
    updated = await service.update_setting(setting_id, payload)
    await settings_cache.update_cache_entry(
        updated.category,
        updated.setting_key,
        updated.setting_value,
        data_type=updated.data_type,
        is_encrypted=updated.is_encrypted,
    )
    return updated


@router.delete(
    "/{setting_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a setting",
    description=(
        "Deletes a single setting entry. Protected system settings "
        "cannot be deleted. Restricted to Admin. On success, the "
        "entry is removed from the settings cache."
    ),
    responses={
        403: {"description": "Caller is not an Admin"},
        404: {"description": "Setting not found"},
        409: {"description": "Setting is a protected system setting"},
    },
)
async def delete_setting(
    setting_id: uuid.UUID,
    service: SettingsService = Depends(get_settings_service),
    current_user: User = Depends(require_roles(UserRole.ADMIN)),
) -> None:
    entry = await service.get_setting(setting_id)
    await service.delete_setting(setting_id)
    await settings_cache.remove_cache_entry(entry.category, entry.setting_key)