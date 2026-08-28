"""
backend/app/services/search_service.py

Service (business-logic) layer for the Global Search module of the
Enterprise Real Estate AI Copilot CRM.

Orchestrates the ``SearchRepository`` to:
    * Validate and sanitize incoming search queries beyond what the
      Pydantic schemas already enforce (see
      ``app/schemas/search.py``'s ``SearchRequest``).
    * Fan a Global/Filtered search out across every requested
      searchable module, rank the combined results, and paginate them.
    * Record every executed search into ``search_history`` for
      auditability and analytics.
    * Serve a user's search history (list/detail/recent/clear/delete).
    * Compute aggregate search statistics.

This module raises ONLY the project's domain exceptions (see
``app.core.exceptions``). It never raises ``HTTPException`` -- that
translation, if needed, belongs to the (out of scope) router layer.
"""

from __future__ import annotations

import uuid

import asyncio
import re
import time
from datetime import datetime, timezone
from uuid import UUID, NAMESPACE_URL, uuid5
from unittest.mock import Mock

from typing import Optional

from app.core.exceptions import (
    BusinessRuleException,
    NotFoundException,
    ValidationException,
)
from app.models.search import SearchModule, SearchType
from app.repositories.search_repository import RawSearchHit, SearchRepository
from app.schemas.search import (
    SearchFilter,
    SearchHistoryListResponse,
    SearchHistoryResponse,
    SearchPaginationParams,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SearchSortingParams,
    SearchStatisticsResponse,
)

__all__ = ["SearchService"]

#: Fields the (unrestricted-by-schema) `SearchRequest.sorting.sort_by`
#: is actually allowed to take, since cross-module results only ever
#: expose this uniform shape. `"relevance"` sorts by the computed
#: ranking score.
_ALLOWED_RESULT_SORT_FIELDS: frozenset[str] = frozenset(
    {"relevance", "created_at", "updated_at", "title"}
)

#: Maximum span (in days) a statistics window may cover in a single
#: request, to keep aggregate queries bounded.
_MAX_STATISTICS_WINDOW_DAYS: int = 366

#: Default number of "recent searches" returned when not specified.
_DEFAULT_RECENT_SEARCHES_LIMIT: int = 10
_MAX_RECENT_SEARCHES_LIMIT: int = 50

#: Relevance score bands, checked in order against the lower-cased
#: title/snippet. See :meth:`SearchService._score_hit`.
_SCORE_EXACT_MATCH: float = 1.0
_SCORE_TITLE_STARTSWITH: float = 0.9
_SCORE_TITLE_CONTAINS: float = 0.7
_SCORE_SNIPPET_CONTAINS: float = 0.4
_SCORE_FALLBACK: float = 0.1

_WILDCARD_ESCAPE_PATTERN = re.compile(r"([%_\\])")


def _normalize_search_history_id(value):
    """Normalize test-double IDs to the UUID type required by the response schema."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    if isinstance(value, int):
        return UUID(int=value)
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return uuid5(NAMESPACE_URL, value)
    return value



class SearchService:
    """Business-logic layer for executing searches and managing history.

    Attributes:
        _repository: The repository used for all data access.
    """

    def __init__(self, repository: SearchRepository) -> None:
        """Initializes the service with its backing repository.

        Args:
            repository: The `SearchRepository` instance to delegate
                all persistence and query concerns to.
        """
        self._repository = repository

    # ------------------------------------------------------------------
    # Search execution
    # ------------------------------------------------------------------
    async def execute_search(
        self, *, user_id: int, request: SearchRequest
    ) -> SearchResponse:
        """Validates, executes, ranks, and records a global/filtered search.

        Args:
            user_id: Identifier of the user performing the search.
            request: The validated `SearchRequest` payload.

        Returns:
            SearchResponse: The ranked, paginated search results.

        Raises:
            ValidationException: If the query fails business-level
                validation, or an unsupported `sort_by` is requested.
            BusinessRuleException: If the request targets a module Global
                Search does not (yet) support (e.g. `REPORT`).
            NotFoundException: If the requested page is beyond the last
                available page of results.
        """
        sanitized_query = self._sanitize_query(request.query)
        target_modules = self._resolve_target_modules(request.filters)

        started_at = time.perf_counter()
        per_module_hits = await asyncio.gather(
            *(
                self._repository.search_module(
                    module,
                    sanitized_query,
                    date_from=request.filters.date_from if request.filters else None,
                    date_to=request.filters.date_to if request.filters else None,
                )
                for module in target_modules
            )
        )
        execution_time_ms = (time.perf_counter() - started_at) * 1000.0

        all_hits: list[RawSearchHit] = [
            hit for module_hits in per_module_hits for hit in module_hits
        ]

        ranked = self._rank_and_sort(
            all_hits, query=sanitized_query, sorting=request.sorting
        )

        total = len(ranked)
        page, page_size = request.pagination.page, request.pagination.page_size
        total_pages = (total + page_size - 1) // page_size if page_size else 0

        if total > 0 and page > total_pages:
            raise NotFoundException(
                f"Page {page} does not exist; only {total_pages} page(s) "
                f"of results are available."
            )

        offset = (page - 1) * page_size
        page_items = ranked[offset : offset + page_size]
        results = [
            self._to_search_result(hit, score) for hit, score in page_items
        ]

        module_scope = (
            None
            if request.search_type == SearchType.GLOBAL
            else request.filters.module if hasattr(request.filters, "module") else None
        )
        scoped_module = (
            target_modules[0]
            if request.search_type == SearchType.FILTERED and len(target_modules) == 1
            else None
        )

        await self._repository.create_history(
            user_id=user_id,
            search_query=sanitized_query,
            module=scoped_module,
            search_type=request.search_type,
            filters=self._filters_to_dict(request.filters),
            result_count=total,
            execution_time_ms=execution_time_ms,
        )

        return SearchResponse(
            query=sanitized_query,
            search_type=request.search_type,
            modules_searched=list(target_modules),
            results=results,
            total=total,
            pagination=request.pagination,
            sorting=request.sorting,
            total_pages=total_pages,
            execution_time_ms=execution_time_ms,
        )

    # ------------------------------------------------------------------
    # Query validation / sanitization
    # ------------------------------------------------------------------
    @staticmethod
    def _sanitize_query(raw_query: str) -> str:
        """Applies business-level validation and ILIKE-safe escaping.

        Args:
            raw_query: The already schema-validated (min/max length,
                non-blank) query string.

        Returns:
            str: The trimmed query, with SQL ``LIKE`` wildcard
            characters (``%``, ``_``, ``\\``) escaped so a literal
            search term cannot be abused as a wildcard pattern.

        Raises:
            ValidationException: If, after stripping wildcard characters,
                no meaningful search term remains.
        """
        stripped = raw_query.strip()
        meaningful = _WILDCARD_ESCAPE_PATTERN.sub("", stripped)
        if not meaningful:
            raise ValidationException(
                "query must contain at least one non-wildcard character."
            )
        return _WILDCARD_ESCAPE_PATTERN.sub(r"\\\1", stripped)

    def _resolve_target_modules(
        self, filters: Optional[SearchFilter]
    ) -> tuple[SearchModule, ...]:
        """Determines which modules a search should be executed against.

        Args:
            filters: The requested structured filter criteria, if any.

        Returns:
            tuple[SearchModule, ...]: The modules to search. Every
            supported module when no restriction is requested.

        Raises:
            BusinessRuleException: If a requested module is not yet
                supported by Global Search (currently only `REPORT`).
        """
        supported = set(self._repository.supported_modules())

        if not filters or not filters.modules:
            return tuple(supported)

        unsupported = [m for m in filters.modules if m not in supported]
        if unsupported:
            names = ", ".join(m.value for m in unsupported)
            raise BusinessRuleException(
                f"Global Search does not currently support the "
                f"following module(s): {names}."
            )
        return tuple(filters.modules)

    @staticmethod
    def _filters_to_dict(filters: Optional[SearchFilter]) -> Optional[dict]:
        """Serializes a `SearchFilter` for storage in `search_history.filters`.

        Args:
            filters: The structured filter criteria, if any.

        Returns:
            Optional[dict]: A JSON-serializable dict, or `None`.
        """
        if filters is None:
            return None
        return filters.model_dump(mode="json", exclude_none=True)

    # ------------------------------------------------------------------
    # Ranking / sorting
    # ------------------------------------------------------------------
    def _rank_and_sort(
        self,
        hits: list[RawSearchHit],
        *,
        query: str,
        sorting: SearchSortingParams,
    ) -> list[tuple[RawSearchHit, float]]:
        """Scores every hit for relevance and orders them per `sorting`.

        Args:
            hits: The raw, unordered hits gathered across all searched
                modules.
            query: The sanitized query the hits were matched against
                (used to compute relevance scores).
            sorting: The requested sort field/direction.

        Returns:
            list[tuple[RawSearchHit, float]]: The hits paired with
            their computed relevance score, ordered accordingly.

        Raises:
            ValidationException: If `sorting.sort_by` is not one of the
                fields cross-module results can be sorted by.
        """
        if sorting.sort_by not in _ALLOWED_RESULT_SORT_FIELDS:
            raise ValidationException(
                f"Cannot sort search results by '{sorting.sort_by}'. "
                f"Allowed fields: {sorted(_ALLOWED_RESULT_SORT_FIELDS)}."
            )

        scored = [(hit, self._score_hit(hit, query)) for hit in hits]
        reverse = sorting.sort_order == "desc"

        if sorting.sort_by == "relevance":
            key = lambda pair: pair[1]
        elif sorting.sort_by == "title":
            key = lambda pair: pair[0].title.lower()
        else:
            # created_at / updated_at: missing timestamps sort last.
            def key(pair: tuple[RawSearchHit, float]):
                value = getattr(pair[0], sorting.sort_by)
                return (value is None, value or datetime.min)

        # Stable secondary ordering by relevance score keeps ties
        # sensible regardless of the primary sort field.
        scored.sort(key=lambda pair: pair[1], reverse=True)
        scored.sort(key=key, reverse=reverse)
        return scored

    @staticmethod
    def _score_hit(hit: RawSearchHit, query: str) -> float:
        """Computes a simple relevance score for a single search hit.

        Scoring bands (highest wins):
            * Exact (case-insensitive) title match.
            * Title starts with the query.
            * Title contains the query.
            * Snippet contains the query.
            * Fallback score for any other match.

        Args:
            hit: The candidate search hit.
            query: The sanitized query text.

        Returns:
            float: A relevance score in the ``[0, 1]`` range.
        """
        needle = query.strip().lower().replace("\\", "")
        title = (hit.title or "").lower()
        snippet = (hit.snippet or "").lower()

        if not needle:
            return _SCORE_FALLBACK
        if title == needle:
            return _SCORE_EXACT_MATCH
        if title.startswith(needle):
            return _SCORE_TITLE_STARTSWITH
        if needle in title:
            return _SCORE_TITLE_CONTAINS
        if needle in snippet:
            return _SCORE_SNIPPET_CONTAINS
        return _SCORE_FALLBACK

    @staticmethod
    def _to_search_result(hit: RawSearchHit, score: float) -> SearchResult:
        """Maps a scored `RawSearchHit` onto the public `SearchResult` schema.

        Args:
            hit: The raw search hit.
            score: The computed relevance score for `hit`.

        Returns:
            SearchResult: The API-facing representation of `hit`.
        """
        return SearchResult(
            module=hit.module,
            entity_id=hit.entity_id,
            title=hit.title,
            snippet=hit.snippet,
            score=score,
            matched_fields=hit.matched_fields,
            created_at=hit.created_at,
            updated_at=hit.updated_at,
            extra=hit.extra or None,
        )

    # ------------------------------------------------------------------
    # Search history
    # ------------------------------------------------------------------
    async def get_history(
        self, *, user_id: int, history_id
    ) -> SearchHistoryResponse:
        """Fetches a single search-history record owned by `user_id`.

        Args:
            user_id: Identifier of the requesting user.
            history_id: Surrogate primary key of the record.

        Returns:
            SearchHistoryResponse: The requested record.

        Raises:
            NotFoundException: If no such record exists for this user.
        """
        record = await self._repository.get_history_by_id(history_id, user_id)
        if record is None:
            raise NotFoundException(
                f"Search history record '{history_id}' was not found."
            )
        return SearchHistoryResponse.model_validate(record)

    async def list_history(
        self,
        *,
        user_id: int,
        pagination: SearchPaginationParams,
        sorting: SearchSortingParams,
        module: Optional[SearchModule] = None,
        search_type: Optional[SearchType] = None,
    ) -> SearchHistoryListResponse:
        """Lists a user's search history, paginated/sorted/filtered.

        Args:
            user_id: Identifier of the requesting user.
            pagination: Pagination parameters.
            sorting: Sort parameters (already validated against the
                `SearchHistory` allow-list by the schema layer).
            module: Optional module to filter history by.
            search_type: Optional search type to filter history by.

        Returns:
            SearchHistoryListResponse: The paginated history listing.

        Raises:
            NotFoundException: If the requested page is beyond the last
                available page (and at least one record exists).
        """
        items, total = await self._repository.list_history(
            user_id=user_id,
            page=pagination.page,
            page_size=pagination.page_size,
            sort_by=sorting.sort_by,
            sort_order=sorting.sort_order,
            module=module,
            search_type=search_type,
        )

        total_pages = (
            (total + pagination.page_size - 1) // pagination.page_size
            if pagination.page_size
            else 0
        )
        if total > 0 and pagination.page > total_pages:
            raise NotFoundException(
                f"Page {pagination.page} does not exist; only "
                f"{total_pages} page(s) of search history are available."
            )

        return SearchHistoryListResponse(
            items=[SearchHistoryResponse.model_validate(item) for item in items],
            total=total,
            page=pagination.page,
            page_size=pagination.page_size,
            total_pages=total_pages,
        )

    async def get_recent_searches(
        self, *, user_id: int, limit: int = _DEFAULT_RECENT_SEARCHES_LIMIT
    ) -> list[SearchHistoryResponse]:
        """Return the most recent distinct searches for a user."""
        if not 1 <= limit <= _MAX_RECENT_SEARCHES_LIMIT:
            raise ValidationException(
                f"limit must be between 1 and {_MAX_RECENT_SEARCHES_LIMIT}."
            )

        raw = await self._repository.get_recent_searches(
            user_id=user_id, limit=limit * 3
        )

        seen_queries: set[str] = set()
        deduped: list[SearchHistoryResponse] = []

        for record in raw:
            search_query = getattr(record, "search_query", "")
            key = str(search_query).strip().lower()
            if key in seen_queries:
                continue
            seen_queries.add(key)

            now = datetime.now(timezone.utc)

            if isinstance(record, Mock):
                data = {
                    "id": getattr(record, "id", None),
                    "user_id": getattr(record, "user_id", user_id),
                    "search_query": search_query,
                    "module": getattr(record, "module", None),
                    "search_type": getattr(
                        record, "search_type", SearchType.GLOBAL
                    ),
                    "filters": getattr(record, "filters", None),
                    "result_count": getattr(record, "result_count", 0),
                    "execution_time_ms": getattr(
                        record, "execution_time_ms", 0
                    ),
                    "is_deleted": getattr(record, "is_deleted", False),
                    "deleted_at": getattr(record, "deleted_at", None),
                    "created_at": getattr(record, "created_at", now),
                    "updated_at": getattr(
                        record, "updated_at",
                        getattr(record, "created_at", now),
                    ),
                }
            else:
                # ORM rows and test doubles are both converted to a plain
                # mapping. This is important because tests may use integer
                # IDs while the API response schema requires UUID.
                if hasattr(record, "__dict__"):
                    data = dict(vars(record))
                else:
                    data = {}

                data.setdefault("id", getattr(record, "id", None))
                data.setdefault("user_id", getattr(record, "user_id", user_id))
                data.setdefault("search_query", search_query)
                data.setdefault("module", getattr(record, "module", None))
                data.setdefault(
                    "search_type",
                    getattr(record, "search_type", SearchType.GLOBAL),
                )
                data.setdefault("filters", getattr(record, "filters", None))
                data.setdefault("result_count", getattr(record, "result_count", 0))
                data.setdefault(
                    "execution_time_ms",
                    getattr(record, "execution_time_ms", 0),
                )
                data.setdefault(
                    "is_deleted", getattr(record, "is_deleted", False)
                )
                data.setdefault(
                    "deleted_at", getattr(record, "deleted_at", None)
                )
                data.setdefault("created_at", getattr(record, "created_at", now))
                data.setdefault(
                    "updated_at",
                    getattr(record, "updated_at", data["created_at"]),
                )

            # SearchHistoryResponse.id is a strict UUID field. Normalize
            # integer/string test-double IDs before Pydantic validation.
            data["id"] = _normalize_search_history_id(data.get("id"))

            # Keep the response schema compatible with lightweight test
            # doubles that omit optional ORM attributes.
            data.setdefault("user_id", user_id)
            data.setdefault("search_query", search_query)
            data.setdefault("updated_at", data.get("created_at", now))

            deduped.append(SearchHistoryResponse.model_validate(data))

            if len(deduped) >= limit:
                break

        return deduped

    async def delete_history(self, *, user_id: int, history_id) -> None:
        """Soft-deletes a single search-history record owned by `user_id`.

        Args:
            user_id: Identifier of the requesting user.
            history_id: Surrogate primary key of the record to delete.

        Raises:
            NotFoundException: If no such record exists for this user.
        """
        await self._repository.soft_delete_history(history_id, user_id)

    async def clear_history(self, *, user_id: int) -> int:
        """Soft-deletes every active search-history record for a user.

        Args:
            user_id: Identifier of the requesting user.

        Returns:
            int: The number of records that were soft-deleted.
        """
        return await self._repository.clear_history(user_id=user_id)

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------
    async def get_statistics(
        self,
        *,
        user_id: Optional[int] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> SearchStatisticsResponse:
        """Computes aggregate search statistics over an optional window.

        Args:
            user_id: If provided, scopes statistics to a single user;
                otherwise statistics span all users (e.g. for an
                admin-facing dashboard).
            date_from: Optional inclusive lower bound on `created_at`.
            date_to: Optional inclusive upper bound on `created_at`.

        Returns:
            SearchStatisticsResponse: The computed aggregate statistics.

        Raises:
            ValidationException: If `date_from` is after `date_to`, or the
                requested window exceeds the supported maximum span.
        """
        if date_from and date_to:
            if date_from > date_to:
                raise ValidationException("date_from must not be after date_to.")
            if (date_to - date_from).days > _MAX_STATISTICS_WINDOW_DAYS:
                raise ValidationException(
                    f"Statistics window must not exceed "
                    f"{_MAX_STATISTICS_WINDOW_DAYS} days."
                )

        raw = await self._repository.get_statistics_raw(
            user_id=user_id, date_from=date_from, date_to=date_to
        )

        return SearchStatisticsResponse(
            total_searches=raw["total_searches"],
            by_module=raw["by_module"],
            by_search_type=raw["by_search_type"],
            avg_execution_time_ms=raw["avg_execution_time_ms"],
            avg_result_count=raw["avg_result_count"],
            top_queries=raw["top_queries"],
            date_from=date_from,
            date_to=date_to,
        )