"""
backend/app/schemas/search.py

Pydantic v2 schemas for the Global Search module of the Enterprise
Real Estate AI Copilot CRM.

Mirrors the shape of `app/models/search.py` and follows the same
naming/style conventions already established in `app/schemas/task.py`:
    - `*Request`   -> payload accepted to execute an operation.
    - `*Response`  -> representation returned by the API.
    - `*Filter`    -> structured filter/criteria payload.
    - `*Result`    -> a single item within a larger response.
    - Reusable `SearchPaginationParams` / `SearchSortingParams`
      building blocks, embedded wherever pagination/sorting is
      accepted, mirroring the inline pagination/sort fields already
      used by `app/schemas/task.py`'s `TaskFilter`.

These schemas define the request/response contracts for executing a
global search, listing/inspecting a user's search history, and
retrieving search analytics. They are consumed by the (separately
implemented, out of scope for this file) service and router layers.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, ClassVar, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.search import SearchModule, SearchType

__all__ = [
    "SearchPaginationParams",
    "SearchSortingParams",
    "SearchFilter",
    "SearchRequest",
    "SearchResult",
    "SearchResponse",
    "SearchHistoryResponse",
    "SearchHistoryListResponse",
    "SearchStatisticsResponse",
]

#: Columns callers may sort search-history listings by. Mirrors the
#: allow-list pattern used by `app/schemas/task.py`'s
#: `TaskFilter._ALLOWED_SORT_FIELDS`.
_ALLOWED_HISTORY_SORT_FIELDS: frozenset[str] = frozenset(
    {
        "created_at",
        "updated_at",
        "result_count",
        "execution_time_ms",
        "search_query",
        "module",
        "search_type",
    }
)

#: Minimum non-blank length of a free-text search query. Mirrors the
#: rationale in `app/utils/task_validator.py`'s
#: `MIN_SEARCH_TERM_LENGTH`: a single-character query against a
#: cross-module search is expensive and rarely useful.
MIN_QUERY_LENGTH: int = 2
MAX_QUERY_LENGTH: int = 500


# ---------------------------------------------------------------------------
# Reusable pagination / sorting building blocks
# ---------------------------------------------------------------------------
class SearchPaginationParams(BaseModel):
    """Reusable pagination parameters shared by search-related requests.

    Attributes:
        page: 1-indexed page number to retrieve.
        page_size: Number of items to retrieve per page.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    page: int = Field(default=1, ge=1, description="1-indexed page number.")
    page_size: int = Field(
        default=20, ge=1, le=200, description="Number of items per page."
    )


class SearchSortingParams(BaseModel):
    """Reusable sort parameters shared by search-related requests.

    Attributes:
        sort_by: Column/field name to sort results by.
        sort_order: Sort direction, either ``"asc"`` or ``"desc"``.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    sort_by: str = Field(
        default="created_at", description="Field name to sort results by."
    )
    sort_order: str = Field(
        default="desc", description="Sort direction: 'asc' or 'desc'."
    )

    @field_validator("sort_order")
    @classmethod
    def _validate_sort_order(cls, value: str) -> str:
        """Validates that the sort order is one of the supported directions.

        Args:
            value: The requested sort order.

        Returns:
            str: The normalized (lowercased) sort order.

        Raises:
            ValueError: If the value is not ``"asc"`` or ``"desc"``.
        """
        normalized = value.strip().lower()
        if normalized not in {"asc", "desc"}:
            raise ValueError("sort_order must be either 'asc' or 'desc'.")
        return normalized


class _HistorySortingParams(SearchSortingParams):
    """Sort parameters restricted to `SearchHistory`'s allowed columns.

    Kept private/internal to this module; used only by
    :class:`SearchHistoryFilter`-style consumers that must validate
    `sort_by` against the persisted `SearchHistory` schema, unlike
    the generic :class:`SearchSortingParams` used by
    :class:`SearchRequest` (which sorts live, cross-module results
    rather than a single table).
    """

    _ALLOWED_SORT_FIELDS: ClassVar[frozenset] = _ALLOWED_HISTORY_SORT_FIELDS

    @field_validator("sort_by")
    @classmethod
    def _validate_sort_by(cls, value: str) -> str:
        """Validates that the sort field is an allowed, indexed column.

        Args:
            value: The requested sort column name.

        Returns:
            str: The validated sort column name.

        Raises:
            ValueError: If the column is not in the allow-list.
        """
        if value not in _ALLOWED_HISTORY_SORT_FIELDS:
            raise ValueError(
                f"sort_by must be one of: {sorted(_ALLOWED_HISTORY_SORT_FIELDS)}"
            )
        return value


# ---------------------------------------------------------------------------
# Search filter / request
# ---------------------------------------------------------------------------
class SearchFilter(BaseModel):
    """Schema encapsulating structured filter criteria for a search.

    Attributes:
        modules: Restrict the search to one or more modules. If empty
            or omitted, the search spans every searchable module.
        date_from: Inclusive lower bound on the searched entities'
            relevant timestamp (e.g. `created_at`), if applicable to
            the target module(s).
        date_to: Inclusive upper bound on the searched entities'
            relevant timestamp, if applicable.
        extra: Arbitrary module-specific filter criteria (e.g.
            `{"status": "pending"}` for Tasks, `{"priority": "high"}`),
            passed through to the module-specific search
            implementation without validation at this layer.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    modules: Optional[list[SearchModule]] = Field(
        default=None,
        description="Restrict the search to these modules; omit/empty for all.",
    )
    date_from: Optional[datetime] = Field(
        default=None, description="Inclusive lower bound on relevant timestamp."
    )
    date_to: Optional[datetime] = Field(
        default=None, description="Inclusive upper bound on relevant timestamp."
    )
    extra: Optional[dict[str, Any]] = Field(
        default=None,
        description="Arbitrary module-specific filter criteria.",
    )

    @field_validator("modules")
    @classmethod
    def _dedupe_modules(
        cls, value: Optional[list[SearchModule]]
    ) -> Optional[list[SearchModule]]:
        """Deduplicates the requested module list while preserving order.

        Args:
            value: The raw list of modules, if supplied.

        Returns:
            Optional[list[SearchModule]]: The deduplicated list, or
            ``None`` unchanged.
        """
        if value is None:
            return None
        seen: set[SearchModule] = set()
        deduped: list[SearchModule] = []
        for module in value:
            if module not in seen:
                seen.add(module)
                deduped.append(module)
        return deduped

    @model_validator(mode="after")
    def _validate_date_range(self) -> "SearchFilter":
        """Ensures the provided date range is chronologically valid.

        Returns:
            SearchFilter: The validated model instance.

        Raises:
            ValueError: If `date_from` is after `date_to`.
        """
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be after date_to.")
        return self


class SearchRequest(BaseModel):
    """Schema used to execute a global (cross-module) search.

    Attributes:
        query: The free-text search query.
        search_type: The kind of search operation to perform.
        filters: Optional structured filter criteria.
        pagination: Pagination parameters for the result set.
        sorting: Sort parameters for the result set.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    query: str = Field(
        ...,
        min_length=MIN_QUERY_LENGTH,
        max_length=MAX_QUERY_LENGTH,
        description="Free-text search query.",
    )
    search_type: SearchType = Field(
        default=SearchType.GLOBAL, description="Kind of search operation."
    )
    filters: Optional[SearchFilter] = Field(
        default=None, description="Structured filter criteria."
    )
    pagination: SearchPaginationParams = Field(
        default_factory=SearchPaginationParams,
        description="Pagination parameters for the result set.",
    )
    sorting: SearchSortingParams = Field(
        default_factory=SearchSortingParams,
        description="Sort parameters for the result set.",
    )

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, value: str) -> str:
        """Ensures the query is not blank after stripping whitespace.

        Args:
            value: The raw query text.

        Returns:
            str: The validated, stripped query.

        Raises:
            ValueError: If the stripped value is empty or shorter than
                :data:`MIN_QUERY_LENGTH`.
        """
        stripped = value.strip()
        if len(stripped) < MIN_QUERY_LENGTH:
            raise ValueError(
                f"query must be at least {MIN_QUERY_LENGTH} characters."
            )
        return stripped

    @model_validator(mode="after")
    def _validate_type_module_consistency(self) -> "SearchRequest":
        """Ensures `search_type` and any requested module scope are consistent.

        Returns:
            SearchRequest: The validated model instance.

        Raises:
            ValueError: If `search_type` is `GLOBAL` while `filters`
                explicitly restricts to modules, or if `search_type`
                is `FILTERED` without any module restriction supplied.
        """
        modules = self.filters.modules if self.filters else None
        if self.search_type == SearchType.GLOBAL and modules:
            raise ValueError(
                "search_type 'global' must not be combined with a "
                "restricted module list; use search_type 'filtered' "
                "instead."
            )
        if self.search_type == SearchType.FILTERED and not modules:
            raise ValueError(
                "search_type 'filtered' requires at least one module "
                "in filters.modules."
            )
        return self


# ---------------------------------------------------------------------------
# Search results / response
# ---------------------------------------------------------------------------
class SearchResult(BaseModel):
    """Schema representing a single matched item within a search response.

    Attributes:
        module: The module this result belongs to.
        entity_id: Primary key of the matched entity within `module`,
            as text (entities use different PK types across modules;
            mirrors `Task.related_entity_id` in `app/models/task.py`).
        title: Short, human-readable headline for the result.
        snippet: Optional short excerpt highlighting the match.
        score: Optional relevance score for this result, if the
            underlying search implementation computes one.
        matched_fields: Optional list of field names the query matched
            against (e.g. `["title", "description"]`).
        created_at: Creation timestamp of the matched entity, if known.
        updated_at: Last-update timestamp of the matched entity, if known.
        extra: Arbitrary module-specific fields useful for rendering
            the result (e.g. a status badge, an assignee name).
    """

    model_config = ConfigDict(from_attributes=True)

    module: SearchModule
    entity_id: str = Field(..., max_length=64)
    title: str = Field(..., min_length=1, max_length=255)
    snippet: Optional[str] = Field(default=None, max_length=1000)
    score: Optional[float] = Field(default=None, ge=0)
    matched_fields: Optional[list[str]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    extra: Optional[dict[str, Any]] = None


class SearchResponse(BaseModel):
    """Schema representing the full result of an executed search.

    Attributes:
        query: The search query that was executed.
        search_type: The kind of search operation performed.
        modules_searched: The modules actually covered by this search.
        results: The matched results for the current page.
        total: Total number of results matching the query, across all pages.
        pagination: The pagination parameters that produced this page.
        sorting: The sort parameters applied to `results`.
        total_pages: Total number of pages available.
        execution_time_ms: How long the search took to execute, in
            milliseconds.
    """

    model_config = ConfigDict(from_attributes=True)

    query: str
    search_type: SearchType
    modules_searched: list[SearchModule] = Field(default_factory=list)
    results: list[SearchResult] = Field(default_factory=list)
    total: int = Field(..., ge=0)
    pagination: SearchPaginationParams
    sorting: SearchSortingParams
    total_pages: int = Field(..., ge=0)
    execution_time_ms: float = Field(..., ge=0)

    @model_validator(mode="after")
    def _compute_total_pages_if_needed(self) -> "SearchResponse":
        """Recomputes ``total_pages`` defensively when it appears inconsistent.

        Returns:
            SearchResponse: The validated model instance.
        """
        page_size = self.pagination.page_size
        expected_pages = (
            (self.total + page_size - 1) // page_size if page_size else 0
        )
        if self.total_pages != expected_pages:
            self.total_pages = expected_pages
        return self


# ---------------------------------------------------------------------------
# Search history
# ---------------------------------------------------------------------------
class SearchHistoryResponse(BaseModel):
    """Schema representing a persisted `SearchHistory` record.

    Attributes:
        id: Surrogate primary key of the search-history record.
        user_id: Identifier of the user who performed the search.
        search_query: The raw free-text query that was searched for.
        module: The single module the search was scoped to, if any.
        search_type: The kind of search operation performed.
        filters: The structured filter criteria supplied with the search.
        result_count: The number of results the search returned.
        execution_time_ms: How long the search took to execute, in
            milliseconds.
        is_deleted: Soft-delete flag.
        deleted_at: Timestamp of soft deletion, if any.
        created_at: Timestamp the search was executed.
        updated_at: Timestamp the record was last updated.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: int
    search_query: str
    module: Optional[SearchModule] = None
    search_type: SearchType
    filters: Optional[dict[str, Any]] = None
    result_count: int = Field(default=0, ge=0)
    execution_time_ms: float = Field(default=0, ge=0)
    is_deleted: bool = False
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class SearchHistoryListResponse(BaseModel):
    """Schema representing a paginated collection of search-history records.

    Attributes:
        items: The search-history records for the current page.
        total: Total number of records matching the query, across all pages.
        page: Current page number (1-indexed).
        page_size: Number of items requested per page.
        total_pages: Total number of pages available.
    """

    model_config = ConfigDict(from_attributes=True)

    items: list[SearchHistoryResponse] = Field(default_factory=list)
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1)
    total_pages: int = Field(..., ge=0)

    @model_validator(mode="after")
    def _compute_total_pages_if_needed(self) -> "SearchHistoryListResponse":
        """Recomputes ``total_pages`` defensively when it appears inconsistent.

        Returns:
            SearchHistoryListResponse: The validated model instance.
        """
        expected_pages = (
            (self.total + self.page_size - 1) // self.page_size
            if self.page_size
            else 0
        )
        if self.total_pages != expected_pages:
            self.total_pages = expected_pages
        return self


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
class SearchStatisticsResponse(BaseModel):
    """Schema representing aggregate statistics over a set of searches.

    Attributes:
        total_searches: Total number of searches in scope.
        by_module: Count of searches grouped by scoped module (searches
            with a `NULL` module, i.e. true global searches, are
            reported under the `"global"` key rather than omitted).
        by_search_type: Count of searches grouped by search type.
        avg_execution_time_ms: Average execution time across searches
            in scope, in milliseconds.
        avg_result_count: Average number of results returned across
            searches in scope.
        top_queries: The most frequently executed query strings in
            scope, mapped to their occurrence count.
        date_from: Inclusive lower bound of the statistics window, if scoped.
        date_to: Inclusive upper bound of the statistics window, if scoped.
    """

    model_config = ConfigDict(from_attributes=True)

    total_searches: int = Field(..., ge=0)
    by_module: dict[str, int] = Field(default_factory=dict)
    by_search_type: dict[str, int] = Field(default_factory=dict)
    avg_execution_time_ms: float = Field(default=0, ge=0)
    avg_result_count: float = Field(default=0, ge=0)
    top_queries: dict[str, int] = Field(default_factory=dict)
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None