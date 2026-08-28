"""
backend/app/api/v1/search.py

REST API layer for the Global Search module of the Enterprise Real
Estate AI Copilot CRM.

Endpoints:
    * ``POST   /api/v1/search``                 -- execute a global/filtered search (JSON body).
    * ``GET    /api/v1/search/advanced``        -- the same, via query parameters ("Advanced Filters").
    * ``GET    /api/v1/search/suggestions``     -- ranked autocomplete suggestions.
    * ``GET    /api/v1/search/history``         -- list the caller's search history.
    * ``GET    /api/v1/search/history/recent``  -- the caller's recent (de-duplicated) searches.
    * ``GET    /api/v1/search/history/{id}``    -- fetch a single history record.
    * ``DELETE /api/v1/search/history/{id}``    -- soft-delete a single history record.
    * ``DELETE /api/v1/search/history``         -- clear all of the caller's search history.
    * ``GET    /api/v1/search/statistics``      -- aggregate search statistics.

Every endpoint requires a valid JWT (via ``get_current_user``) and
enforces role-based access control where relevant (restricted modules,
tenant-wide statistics). All business/data errors surface as the
project's domain exceptions (``app.core.exceptions``) -- this router
never raises ``HTTPException`` directly; a global exception-handler
(registered on the FastAPI app, outside this file's scope) is assumed
to translate those into the appropriate HTTP status codes, e.g.:

    NotFoundError      -> 404
    ValidationException    -> 422
    BusinessRuleError  -> 400
    AuthorizationException     -> 403

.. note::
    This file assumes the following already exist elsewhere in the
    project, following the same conventions used by the rest of the
    codebase (mirrored here rather than re-declared):
        * ``app.db.session.get_db``            -- async session dependency.
        * ``app.core.security.get_current_user``-- JWT-authenticated user dependency.
        * ``app.models.user.User``              -- user ORM model with an
          ``id`` and a ``role`` attribute (string or string-backed enum).
        * ``app.core.exceptions.{NotFoundError, ValidationException,
          BusinessRuleError, AuthorizationException}``.
    If any of these differ in name/shape from the project's actual
    modules, only the small dependency-wiring section below needs to
    change -- the endpoint bodies are otherwise decoupled from it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, status
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.exceptions import AuthorizationException, ValidationException
from app.db.session import get_db
from app.models.search import SearchModule, SearchType
from app.models.user import User
from app.repositories.search_repository import SearchRepository
from app.schemas.search import (
    SearchFilter,
    SearchHistoryListResponse,
    SearchHistoryResponse,
    SearchPaginationParams,
    SearchRequest,
    SearchResponse,
    SearchSortingParams,
    SearchStatisticsResponse,
)
from app.services.search_service import SearchService
from app.utils.search_engine import (
    build_filter_from_query_params,
    get_suggestions,
    parse_date_param,
    parse_modules_param,
)

__all__ = ["router"]

router = APIRouter(prefix="/search", tags=["Global Search"])

# ---------------------------------------------------------------------------
# RBAC configuration
# ---------------------------------------------------------------------------
#: Roles permitted to search modules that carry sensitive data, and to
#: view tenant-wide (as opposed to per-user) search statistics.
_ELEVATED_ROLES: frozenset[str] = frozenset({"admin", "manager"})

#: Modules that require an elevated role to search at all.
_RESTRICTED_MODULES: frozenset[SearchModule] = frozenset(
    {SearchModule.AUDIT_LOG, SearchModule.PAYMENT}
)

_DEFAULT_RECENT_LIMIT: int = 10
_MAX_SUGGESTION_CANDIDATE_POOL: int = 50


def _role_value(user: User) -> Optional[str]:
    """Extracts a plain string role from a `User`, tolerating an enum role.

    Args:
        user: The authenticated user.

    Returns:
        Optional[str]: The lower-cased role value, or `None` if the
        user has no role attribute set.
    """
    role = getattr(user, "role", None)
    value = getattr(role, "value", role)
    return value.lower() if isinstance(value, str) else value


def _enforce_module_rbac(request: SearchRequest, current_user: User) -> None:
    """Blocks searches that target restricted modules without an elevated role.

    Args:
        request: The search request about to be executed.
        current_user: The authenticated caller.

    Raises:
        AuthorizationException: If `request.filters.modules` explicitly names
            one or more restricted modules and the caller's role is
            not in `_ELEVATED_ROLES`. Unscoped (global) searches are
            never blocked here -- callers without an elevated role
            simply won't find restricted-module data via this route,
            since only elevated roles ever pass a restricted module in
            explicitly, and unscoped `GLOBAL` searches are handled by
            the service/repository, which apply their own module
            allow-lists independent of RBAC.
    """
    if _role_value(current_user) in _ELEVATED_ROLES:
        return

    requested_modules = request.filters.modules if request.filters else None
    if not requested_modules:
        return

    blocked = [m for m in requested_modules if m in _RESTRICTED_MODULES]
    if blocked:
        names = ", ".join(m.value for m in blocked)
        raise AuthorizationException(
            f"Your role is not permitted to search the following "
            f"module(s): {names}."
        )


# ---------------------------------------------------------------------------
# Dependency wiring
# ---------------------------------------------------------------------------
async def get_search_service(
    session: AsyncSession = Depends(get_db),
) -> SearchService:
    """Builds a `SearchService` wired to a request-scoped async session.

    Args:
        session: The async SQLAlchemy session for this request.

    Returns:
        SearchService: A service instance backed by a fresh
        `SearchRepository` for this request.
    """
    return SearchService(SearchRepository(session))


# ---------------------------------------------------------------------------
# Search execution
# ---------------------------------------------------------------------------
@router.post(
    "",
    response_model=SearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Execute a global or filtered search",
    description=(
        "Runs a free-text search across one or more CRM modules "
        "(Customers, Leads, Properties, Bookings, Payments, Tasks, "
        "Documents, Workflow, Activity, Audit Logs, Notifications), "
        "ranks the combined results by relevance, paginates them, and "
        "records the search in the caller's search history."
    ),
    responses={
        400: {"description": "Business rule violation (e.g. unsupported module)."},
        403: {"description": "Not permitted to search the requested module(s)."},
        404: {"description": "The requested result page does not exist."},
        422: {"description": "Invalid query, filters, or sort field."},
    },
)
async def search(
    payload: SearchRequest,
    current_user: User = Depends(get_current_user),
    service: SearchService = Depends(get_search_service),
) -> SearchResponse:
    """Executes a global/filtered search on behalf of the authenticated user.

    Args:
        payload: The search request body.
        current_user: The authenticated caller (JWT-derived).
        service: The injected `SearchService`.

    Returns:
        SearchResponse: The ranked, paginated search results.
    """
    _enforce_module_rbac(payload, current_user)
    return await service.execute_search(user_id=current_user.id, request=payload)


@router.get(
    "/advanced",
    response_model=SearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Execute an advanced search via query parameters",
    description=(
        "Query-parameter equivalent of `POST /search`, for bookmarkable "
        "or link-shareable advanced/filtered searches. Supports "
        "restricting to specific modules, a creation-date range, and "
        "arbitrary module-specific filter criteria via a JSON-encoded "
        "`extra` parameter."
    ),
    responses={
        400: {"description": "Business rule violation (e.g. unsupported module)."},
        403: {"description": "Not permitted to search the requested module(s)."},
        404: {"description": "The requested result page does not exist."},
        422: {"description": "Invalid query, filters, or sort field."},
    },
)
async def advanced_search(
    q: str = Query(..., min_length=2, max_length=500, description="Free-text query."),
    search_type: SearchType = Query(
        SearchType.GLOBAL, description="Kind of search operation."
    ),
    modules: Optional[str] = Query(
        None, description="Comma-separated module list, e.g. 'customer,lead'."
    ),
    date_from: Optional[str] = Query(
        None, description="Inclusive lower bound, ISO-8601 date/datetime."
    ),
    date_to: Optional[str] = Query(
        None, description="Inclusive upper bound, ISO-8601 date/datetime."
    ),
    extra: Optional[str] = Query(
        None, description="JSON-encoded object of module-specific filter criteria."
    ),
    page: int = Query(1, ge=1, description="1-indexed page number."),
    page_size: int = Query(20, ge=1, le=200, description="Items per page."),
    sort_by: str = Query(
        "relevance", description="One of: relevance, created_at, updated_at, title."
    ),
    sort_order: str = Query("desc", description="'asc' or 'desc'."),
    current_user: User = Depends(get_current_user),
    service: SearchService = Depends(get_search_service),
) -> SearchResponse:
    """Executes an advanced search built from individual query parameters.

    Args:
        q: The free-text query.
        search_type: The kind of search operation to perform.
        modules: Raw comma-separated module list, if restricting scope.
        date_from: Raw ISO-8601 lower date bound, if any.
        date_to: Raw ISO-8601 upper date bound, if any.
        extra: Raw JSON-encoded extra filter criteria, if any.
        page: 1-indexed page number.
        page_size: Items per page.
        sort_by: Result sort field.
        sort_order: Result sort direction.
        current_user: The authenticated caller (JWT-derived).
        service: The injected `SearchService`.

    Returns:
        SearchResponse: The ranked, paginated search results.

    Raises:
        ValidationException: If any query parameter fails parsing/validation.
    """
    parsed_modules = parse_modules_param(modules)
    parsed_date_from = parse_date_param(date_from, "date_from")
    parsed_date_to = parse_date_param(date_to, "date_to")
    filters: Optional[SearchFilter] = build_filter_from_query_params(
        modules=parsed_modules,
        date_from=parsed_date_from,
        date_to=parsed_date_to,
        extra_json=extra,
    )

    try:
        payload = SearchRequest(
            query=q,
            search_type=search_type,
            filters=filters,
            pagination=SearchPaginationParams(page=page, page_size=page_size),
            sorting=SearchSortingParams(sort_by=sort_by, sort_order=sort_order),
        )
    except PydanticValidationError as exc:
        raise ValidationException(str(exc)) from exc

    _enforce_module_rbac(payload, current_user)
    return await service.execute_search(user_id=current_user.id, request=payload)


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------
@router.get(
    "/suggestions",
    response_model=list[str],
    status_code=status.HTTP_200_OK,
    summary="Get autocomplete suggestions for a partial query",
    description=(
        "Returns ranked search-query suggestions drawn from the "
        "caller's own recent searches and tenant-wide top queries, "
        "matched against the supplied partial query text."
    ),
)
async def suggestions(
    q: str = Query(..., min_length=1, max_length=100, description="Partial query text."),
    limit: int = Query(10, ge=1, le=25, description="Maximum suggestions to return."),
    current_user: User = Depends(get_current_user),
    service: SearchService = Depends(get_search_service),
) -> list[str]:
    """Builds ranked autocomplete suggestions for a partial query.

    Args:
        q: The user's partial/in-progress query text.
        limit: Maximum number of suggestions to return.
        current_user: The authenticated caller (JWT-derived).
        service: The injected `SearchService`.

    Returns:
        list[str]: The ranked suggestions, best match first.
    """
    recent = await service.get_recent_searches(
        user_id=current_user.id, limit=_MAX_SUGGESTION_CANDIDATE_POOL
    )
    candidates = [record.search_query for record in recent]

    try:
        stats = await service.get_statistics(user_id=current_user.id)
        candidates.extend(stats.top_queries.keys())
    except ValidationException:
        # Statistics are a "nice to have" here; a validation hiccup
        # (e.g. no history yet) shouldn't break suggestions.
        pass

    return get_suggestions(q, candidates, limit=limit)


# ---------------------------------------------------------------------------
# Search history
# ---------------------------------------------------------------------------
@router.get(
    "/history",
    response_model=SearchHistoryListResponse,
    status_code=status.HTTP_200_OK,
    summary="List the caller's search history",
    responses={404: {"description": "The requested page does not exist."}},
)
async def list_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    sort_by: str = Query("created_at"),
    sort_order: str = Query("desc"),
    module: Optional[SearchModule] = Query(None, description="Filter by module."),
    search_type: Optional[SearchType] = Query(None, description="Filter by search type."),
    current_user: User = Depends(get_current_user),
    service: SearchService = Depends(get_search_service),
) -> SearchHistoryListResponse:
    """Lists the authenticated caller's own search history.

    Args:
        page: 1-indexed page number.
        page_size: Items per page.
        sort_by: History column to sort by.
        sort_order: Sort direction.
        module: Optional module filter.
        search_type: Optional search-type filter.
        current_user: The authenticated caller (JWT-derived).
        service: The injected `SearchService`.

    Returns:
        SearchHistoryListResponse: The paginated history listing.
    """
    pagination = SearchPaginationParams(page=page, page_size=page_size)
    try:
        sorting = SearchSortingParams(sort_by=sort_by, sort_order=sort_order)
    except PydanticValidationError as exc:
        raise ValidationException(str(exc)) from exc

    return await service.list_history(
        user_id=current_user.id,
        pagination=pagination,
        sorting=sorting,
        module=module,
        search_type=search_type,
    )


@router.get(
    "/history/recent",
    response_model=list[SearchHistoryResponse],
    status_code=status.HTTP_200_OK,
    summary="Get the caller's recent, de-duplicated searches",
)
async def recent_history(
    limit: int = Query(_DEFAULT_RECENT_LIMIT, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    service: SearchService = Depends(get_search_service),
) -> list[SearchHistoryResponse]:
    """Returns the caller's most recent, de-duplicated search queries.

    Args:
        limit: Maximum number of recent searches to return.
        current_user: The authenticated caller (JWT-derived).
        service: The injected `SearchService`.

    Returns:
        list[SearchHistoryResponse]: The most recent distinct searches,
        newest first.
    """
    return await service.get_recent_searches(user_id=current_user.id, limit=limit)


@router.get(
    "/history/{history_id}",
    response_model=SearchHistoryResponse,
    status_code=status.HTTP_200_OK,
    summary="Get a single search-history record",
    responses={404: {"description": "No such search history record for this user."}},
)
async def get_history(
    history_id: UUID = Path(..., description="Search history record id."),
    current_user: User = Depends(get_current_user),
    service: SearchService = Depends(get_search_service),
) -> SearchHistoryResponse:
    """Fetches a single search-history record owned by the caller.

    Args:
        history_id: Surrogate primary key of the record.
        current_user: The authenticated caller (JWT-derived).
        service: The injected `SearchService`.

    Returns:
        SearchHistoryResponse: The requested record.
    """
    return await service.get_history(user_id=current_user.id, history_id=history_id)


@router.delete(
    "/history/{history_id}",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a single search-history record",
    responses={404: {"description": "No such search history record for this user."}},
)
async def delete_history(
    history_id: UUID = Path(..., description="Search history record id."),
    current_user: User = Depends(get_current_user),
    service: SearchService = Depends(get_search_service),
) -> None:
    """Soft-deletes a single search-history record owned by the caller.

    Args:
        history_id: Surrogate primary key of the record to delete.
        current_user: The authenticated caller (JWT-derived).
        service: The injected `SearchService`.
    """
    await service.delete_history(user_id=current_user.id, history_id=history_id)


@router.delete(
    "/history",
    status_code=status.HTTP_200_OK,
    summary="Clear all of the caller's search history",
)
async def clear_history(
    current_user: User = Depends(get_current_user),
    service: SearchService = Depends(get_search_service),
) -> dict[str, int]:
    """Soft-deletes every active search-history record for the caller.

    Args:
        current_user: The authenticated caller (JWT-derived).
        service: The injected `SearchService`.

    Returns:
        dict[str, int]: The number of records deleted, under the
        ``deleted_count`` key.
    """
    deleted_count = await service.clear_history(user_id=current_user.id)
    return {"deleted_count": deleted_count}


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
@router.get(
    "/statistics",
    response_model=SearchStatisticsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get aggregate search statistics",
    description=(
        "Returns aggregate search statistics for the caller's own "
        "activity by default. Tenant-wide statistics (`scope=all`) are "
        "restricted to admin/manager roles."
    ),
    responses={
        403: {"description": "Tenant-wide statistics require an elevated role."},
        422: {"description": "Invalid date range."},
    },
)
async def statistics(
    date_from: Optional[datetime] = Query(None, description="Inclusive lower bound."),
    date_to: Optional[datetime] = Query(None, description="Inclusive upper bound."),
    scope: str = Query(
        "self",
        pattern="^(self|all)$",
        description="'self' for the caller's own statistics, 'all' for tenant-wide.",
    ),
    current_user: User = Depends(get_current_user),
    service: SearchService = Depends(get_search_service),
) -> SearchStatisticsResponse:
    """Computes aggregate search statistics, scoped per-caller or tenant-wide.

    Args:
        date_from: Optional inclusive lower bound on `created_at`.
        date_to: Optional inclusive upper bound on `created_at`.
        scope: ``"self"`` (default) or ``"all"`` (elevated roles only).
        current_user: The authenticated caller (JWT-derived).
        service: The injected `SearchService`.

    Returns:
        SearchStatisticsResponse: The computed aggregate statistics.

    Raises:
        AuthorizationException: If `scope="all"` is requested by a caller
            without an elevated role.
    """
    if scope == "all":
        if _role_value(current_user) not in _ELEVATED_ROLES:
            raise AuthorizationException(
                "Only admin/manager roles may view tenant-wide search statistics."
            )
        return await service.get_statistics(
            user_id=None, date_from=date_from, date_to=date_to
        )

    return await service.get_statistics(
        user_id=current_user.id, date_from=date_from, date_to=date_to
    )