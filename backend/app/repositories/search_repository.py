"""
backend/app/repositories/search_repository.py

Repository (data-access) layer for the Global Search module of the
Enterprise Real Estate AI Copilot CRM.

This repository is responsible for two distinct concerns, kept in a
single class because they share the same session/lifecycle and are
always used together by the service layer:

    1. **Cross-module search execution** -- running the actual
       ILIKE-based lookups against each searchable domain module
       (Customer, Lead, Property, Booking, Payment, Task, Document,
       Workflow, Activity, Audit Log, Notification) and returning a
       uniform, module-agnostic row shape (:class:`RawSearchHit`).
    2. **Search history / statistics persistence** -- CRUD-style
       access to the ``search_history`` table defined in
       ``app/models/search.py``.

Design notes:
    - This repository never raises HTTP-layer exceptions. It only
      raises the project's domain exceptions (see
      ``app.core.exceptions``), consistent with the rest of the
      codebase (mirrors the repository conventions already
      established for ``app/repositories/task_repository.py``).
    - Domain models for the individual searchable modules (Customer,
      Lead, Property, ...) are NOT part of this file's scope and are
      resolved lazily/defensively at call time. If a given module's
      model cannot be imported (e.g. it has not been implemented yet
      in this environment), that module is simply skipped rather than
      failing the whole cross-module search -- this keeps Global
      Search resilient as new modules are added to the CRM over time.
    - No hard FK/relationship is held to the searched entities
      themselves (see ``app/models/search.py`` docstring); this
      repository only ever reads from those tables.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BusinessRuleException,
    NotFoundException,
    ValidationException,
)
from app.models.search import SearchHistory, SearchModule, SearchType

__all__ = [
    "RawSearchHit",
    "SearchRepository",
]


# ---------------------------------------------------------------------------
# Uniform cross-module search hit
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class RawSearchHit:
    """Uniform, module-agnostic representation of a single matched entity.

    The service layer maps instances of this dataclass onto
    ``app.schemas.search.SearchResult`` once ranking/sorting has been
    applied.

    Attributes:
        module: The module the matched entity belongs to.
        entity_id: Primary key of the matched entity, as text.
        title: Best-effort human-readable headline for the entity.
        snippet: Best-effort short excerpt built from the matched
            text fields.
        matched_fields: Names of the columns the query matched against.
        created_at: Creation timestamp of the entity, if available.
        updated_at: Last-update timestamp of the entity, if available.
        extra: Arbitrary additional fields useful for rendering.
    """

    module: SearchModule
    entity_id: str
    title: str
    snippet: Optional[str]
    matched_fields: list[str]
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Per-module resolution tables
# ---------------------------------------------------------------------------
#: Dotted module path + class name for each searchable domain module.
#: ``SearchModule.REPORT`` is intentionally omitted -- Report Search is
#: out of scope for this iteration of Global Search.
_MODULE_MODEL_LOCATIONS: dict[SearchModule, tuple[str, str]] = {
    SearchModule.CUSTOMER: ("app.models.customer", "Customer"),
    SearchModule.LEAD: ("app.models.lead", "Lead"),
    SearchModule.PROPERTY: ("app.models.property", "Property"),
    SearchModule.BOOKING: ("app.models.booking", "Booking"),
    SearchModule.PAYMENT: ("app.models.payment", "Payment"),
    SearchModule.TASK: ("app.models.task", "Task"),
    SearchModule.DOCUMENT: ("app.models.document", "Document"),
    SearchModule.WORKFLOW: ("app.models.workflow", "Workflow"),
    SearchModule.ACTIVITY: ("app.models.activity", "Activity"),
    SearchModule.AUDIT_LOG: ("app.models.audit_log", "AuditLog"),
    SearchModule.NOTIFICATION: ("app.models.notification", "Notification"),
}

#: Text columns searched (in priority order) for each module. The
#: first available column is treated as the result ``title``; the
#: remainder contribute to the ``snippet``.
_MODULE_SEARCH_FIELDS: dict[SearchModule, tuple[str, ...]] = {
    SearchModule.CUSTOMER: ("first_name", "last_name", "email", "phone"),
    SearchModule.LEAD: ("name", "email", "phone", "source"),
    SearchModule.PROPERTY: ("title", "address", "city", "description"),
    SearchModule.BOOKING: ("reference_code", "status"),
    SearchModule.PAYMENT: ("reference_code", "status"),
    SearchModule.TASK: ("title", "description"),
    SearchModule.DOCUMENT: ("file_name", "description"),
    SearchModule.WORKFLOW: ("name", "description"),
    SearchModule.ACTIVITY: ("description", "activity_type"),
    SearchModule.AUDIT_LOG: ("action", "entity_type"),
    SearchModule.NOTIFICATION: ("title", "message"),
}

#: Per-process cache of resolved ORM classes, keyed by module.
_MODEL_CACHE: dict[SearchModule, Optional[type]] = {}

#: Default cap on rows fetched from a single module during a
#: cross-module Global Search, before ranking/pagination is applied
#: by the service layer.
_PER_MODULE_FETCH_LIMIT: int = 100


def _resolve_model(module: SearchModule) -> Optional[type]:
    """Lazily imports and caches the ORM class backing a search module.

    Args:
        module: The module whose backing ORM class should be resolved.

    Returns:
        Optional[type]: The resolved ORM class, or ``None`` if the
        module has no configured location or the import fails (e.g.
        the module has not been implemented yet).
    """
    if module in _MODEL_CACHE:
        return _MODEL_CACHE[module]

    location = _MODULE_MODEL_LOCATIONS.get(module)
    if location is None:
        _MODEL_CACHE[module] = None
        return None

    dotted_path, class_name = location
    try:
        mod = importlib.import_module(dotted_path)
        model_cls = getattr(mod, class_name)
    except (ImportError, AttributeError):
        _MODEL_CACHE[module] = None
        return None

    _MODEL_CACHE[module] = model_cls
    return model_cls


class SearchRepository:
    """Data-access layer for Global Search and search-history persistence.

    Attributes:
        _session: The active async SQLAlchemy session used for all
            queries issued by this repository instance.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Initializes the repository with an active async session.

        Args:
            session: The async SQLAlchemy session to issue queries on.
        """
        self._session = session

    # ------------------------------------------------------------------
    # Global / per-module search
    # ------------------------------------------------------------------
    def supported_modules(self) -> tuple[SearchModule, ...]:
        """Returns the modules Global Search currently knows how to query.

        Returns:
            tuple[SearchModule, ...]: All modules with a configured
            model location (i.e. everything except ``REPORT``).
        """
        return tuple(_MODULE_MODEL_LOCATIONS.keys())

    async def search_module(
        self,
        module: SearchModule,
        query: str,
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        limit: int = _PER_MODULE_FETCH_LIMIT,
    ) -> list[RawSearchHit]:
        """Searches a single module's table for rows matching ``query``.

        Args:
            module: The module to search.
            query: The (already-sanitized) free-text query.
            date_from: Optional inclusive lower bound on the entity's
                ``created_at`` column, if present on the model.
            date_to: Optional inclusive upper bound on the entity's
                ``created_at`` column, if present on the model.
            limit: Maximum number of rows to fetch from this module.

        Returns:
            list[RawSearchHit]: The matched rows, uniformly shaped.
            An empty list is returned (rather than raising) if the
            module's backing model cannot be resolved or exposes none
            of its expected searchable columns -- this keeps a single
            unavailable module from failing the entire Global Search.
        """
        model_cls = _resolve_model(module)
        if model_cls is None:
            return []

        fields = _MODULE_SEARCH_FIELDS.get(module, ())
        available_fields = [f for f in fields if hasattr(model_cls, f)]
        if not available_fields:
            return []

        like_pattern = f"%{query}%"
        conditions = [
            getattr(model_cls, f).ilike(like_pattern, escape="\\")
            for f in available_fields
        ]

        stmt: Select = select(model_cls).where(or_(*conditions))

        if date_from is not None and hasattr(model_cls, "created_at"):
            stmt = stmt.where(model_cls.created_at >= date_from)
        if date_to is not None and hasattr(model_cls, "created_at"):
            stmt = stmt.where(model_cls.created_at <= date_to)

        # Soft-deleted rows should never surface in search results, if
        # the target module supports soft deletion.
        if hasattr(model_cls, "is_deleted"):
            stmt = stmt.where(model_cls.is_deleted.is_(False))

        stmt = stmt.limit(limit)

        result = await self._session.execute(stmt)
        rows = result.scalars().all()
        return [
            self._to_raw_hit(module, row, available_fields) for row in rows
        ]

    @staticmethod
    def _to_raw_hit(
        module: SearchModule, row: Any, matched_fields: list[str]
    ) -> RawSearchHit:
        """Converts a raw ORM row into a uniform :class:`RawSearchHit`.

        Args:
            module: The module the row belongs to.
            row: The ORM instance returned by the module's query.
            matched_fields: The columns the search condition was built
                from for this module.

        Returns:
            RawSearchHit: The normalized representation of ``row``.
        """
        values = [
            str(getattr(row, f)) for f in matched_fields if getattr(row, f, None)
        ]
        title = values[0] if values else f"{module.value}:{getattr(row, 'id', '')}"
        snippet = " · ".join(values[1:]) if len(values) > 1 else None

        return RawSearchHit(
            module=module,
            entity_id=str(getattr(row, "id", "")),
            title=title,
            snippet=snippet,
            matched_fields=matched_fields,
            created_at=getattr(row, "created_at", None),
            updated_at=getattr(row, "updated_at", None),
            extra={},
        )

    # ------------------------------------------------------------------
    # Search history persistence
    # ------------------------------------------------------------------
    async def create_history(
        self,
        *,
        user_id: int,
        search_query: str,
        module: Optional[SearchModule],
        search_type: SearchType,
        filters: Optional[dict[str, Any]],
        result_count: int,
        execution_time_ms: float,
    ) -> SearchHistory:
        """Persists a single executed search as a ``search_history`` row.

        Args:
            user_id: Identifier of the user who performed the search.
            search_query: The raw free-text query that was searched for.
            module: The single module the search was scoped to, if any.
            search_type: The kind of search operation performed.
            filters: The structured filter criteria supplied, if any.
            result_count: The number of results the search returned.
            execution_time_ms: How long the search took, in milliseconds.

        Returns:
            SearchHistory: The newly persisted record.
        """
        record = SearchHistory(
            user_id=user_id,
            search_query=search_query,
            module=module,
            search_type=search_type,
            filters=filters,
            result_count=result_count,
            execution_time_ms=execution_time_ms,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)
        return record

    async def get_history_by_id(
        self, history_id: Any, user_id: int
    ) -> Optional[SearchHistory]:
        """Fetches a single, non-deleted search-history record for a user.

        Args:
            history_id: Surrogate primary key of the record.
            user_id: Identifier of the owning user (enforces per-user
                isolation of history records).

        Returns:
            Optional[SearchHistory]: The record, or ``None`` if it does
            not exist, is soft-deleted, or does not belong to the user.
        """
        stmt = select(SearchHistory).where(
            SearchHistory.id == history_id,
            SearchHistory.user_id == user_id,
            SearchHistory.is_deleted.is_(False),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_history(
        self,
        *,
        user_id: int,
        page: int,
        page_size: int,
        sort_by: str,
        sort_order: str,
        module: Optional[SearchModule] = None,
        search_type: Optional[SearchType] = None,
        include_deleted: bool = False,
    ) -> tuple[list[SearchHistory], int]:
        """Lists a user's search history, paginated, sorted, and filtered.

        Args:
            user_id: Identifier of the owning user.
            page: 1-indexed page number to retrieve.
            page_size: Number of records per page.
            sort_by: Column name to sort by. Must be a real
                ``SearchHistory`` column (the service layer is
                responsible for validating this against the allowed
                sort-field list before calling this method).
            sort_order: ``"asc"`` or ``"desc"``.
            module: Optional module to filter by.
            search_type: Optional search type to filter by.
            include_deleted: Whether to include soft-deleted records.

        Returns:
            tuple[list[SearchHistory], int]: The page of records and
            the total count of matching records across all pages.

        Raises:
            ValidationError: If ``sort_by`` does not correspond to a
                real column on ``SearchHistory``.
        """
        sort_column = getattr(SearchHistory, sort_by, None)
        if sort_column is None:
            raise ValidationException(
                f"Cannot sort search history by unknown field '{sort_by}'."
            )

        conditions = [SearchHistory.user_id == user_id]
        if not include_deleted:
            conditions.append(SearchHistory.is_deleted.is_(False))
        if module is not None:
            conditions.append(SearchHistory.module == module)
        if search_type is not None:
            conditions.append(SearchHistory.search_type == search_type)

        count_stmt = select(func.count()).select_from(SearchHistory).where(
            *conditions
        )
        total = (await self._session.execute(count_stmt)).scalar_one()

        order_clause = sort_column.asc() if sort_order == "asc" else sort_column.desc()
        offset = (page - 1) * page_size

        list_stmt = (
            select(SearchHistory)
            .where(*conditions)
            .order_by(order_clause)
            .offset(offset)
            .limit(page_size)
        )
        result = await self._session.execute(list_stmt)
        items = list(result.scalars().all())
        return items, total

    async def get_recent_searches(
        self, *, user_id: int, limit: int
    ) -> list[SearchHistory]:
        """Fetches the most recent search-history records for a user.

        Returns raw (non-deduplicated) rows; de-duplication of
        repeated query strings is a business-rule concern handled by
        the service layer.

        Args:
            user_id: Identifier of the owning user.
            limit: Maximum number of rows to fetch.

        Returns:
            list[SearchHistory]: The most recent records, newest first.
        """
        stmt = (
            select(SearchHistory)
            .where(
                SearchHistory.user_id == user_id,
                SearchHistory.is_deleted.is_(False),
            )
            .order_by(SearchHistory.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def soft_delete_history(self, history_id: Any, user_id: int) -> bool:
        """Soft-deletes a single search-history record owned by ``user_id``.

        Args:
            history_id: Surrogate primary key of the record to delete.
            user_id: Identifier of the owning user.

        Returns:
            bool: ``True`` if a record was found and soft-deleted.

        Raises:
            NotFoundError: If no matching, non-deleted record exists.
        """
        record = await self.get_history_by_id(history_id, user_id)
        if record is None:
            raise NotFoundException(
                f"Search history record '{history_id}' was not found."
            )
        record.is_deleted = True
        record.deleted_at = func.now()
        await self._session.flush()
        return True

    async def clear_history(self, *, user_id: int) -> int:
        """Soft-deletes every active search-history record for a user.

        Args:
            user_id: Identifier of the owning user.

        Returns:
            int: The number of records that were soft-deleted.
        """
        stmt = select(SearchHistory).where(
            SearchHistory.user_id == user_id,
            SearchHistory.is_deleted.is_(False),
        )
        result = await self._session.execute(stmt)
        records = list(result.scalars().all())
        for record in records:
            record.is_deleted = True
            record.deleted_at = func.now()
        await self._session.flush()
        return len(records)

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------
    async def get_statistics_raw(
        self,
        *,
        user_id: Optional[int],
        date_from: Optional[datetime],
        date_to: Optional[datetime],
        top_queries_limit: int = 10,
    ) -> dict[str, Any]:
        """Computes aggregate search statistics from ``search_history``.

        Args:
            user_id: If provided, scopes statistics to a single user;
                otherwise statistics are computed tenant/system-wide.
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.
            top_queries_limit: Maximum number of distinct query strings
                to include in the ``top_queries`` breakdown.

        Returns:
            dict[str, Any]: Raw aggregate values keyed by
            ``total_searches``, ``by_module``, ``by_search_type``,
            ``avg_execution_time_ms``, ``avg_result_count``, and
            ``top_queries``, ready to be assembled by the service layer
            into a ``SearchStatisticsResponse``.
        """
        conditions = [SearchHistory.is_deleted.is_(False)]
        if user_id is not None:
            conditions.append(SearchHistory.user_id == user_id)
        if date_from is not None:
            conditions.append(SearchHistory.created_at >= date_from)
        if date_to is not None:
            conditions.append(SearchHistory.created_at <= date_to)

        total_stmt = select(func.count()).select_from(SearchHistory).where(
            *conditions
        )
        total_searches = (await self._session.execute(total_stmt)).scalar_one()

        by_module_stmt = (
            select(SearchHistory.module, func.count())
            .where(*conditions)
            .group_by(SearchHistory.module)
        )
        by_module_rows = (await self._session.execute(by_module_stmt)).all()
        by_module = {
            (module.value if module is not None else "global"): count
            for module, count in by_module_rows
        }

        by_type_stmt = (
            select(SearchHistory.search_type, func.count())
            .where(*conditions)
            .group_by(SearchHistory.search_type)
        )
        by_type_rows = (await self._session.execute(by_type_stmt)).all()
        by_search_type = {
            search_type.value: count for search_type, count in by_type_rows
        }

        avg_stmt = select(
            func.coalesce(func.avg(SearchHistory.execution_time_ms), 0),
            func.coalesce(func.avg(SearchHistory.result_count), 0),
        ).where(*conditions)
        avg_execution_time_ms, avg_result_count = (
            await self._session.execute(avg_stmt)
        ).one()

        top_queries_stmt = (
            select(SearchHistory.search_query, func.count().label("occurrences"))
            .where(*conditions)
            .group_by(SearchHistory.search_query)
            .order_by(func.count().desc())
            .limit(top_queries_limit)
        )
        top_queries_rows = (await self._session.execute(top_queries_stmt)).all()
        top_queries = {query: count for query, count in top_queries_rows}

        return {
            "total_searches": total_searches,
            "by_module": by_module,
            "by_search_type": by_search_type,
            "avg_execution_time_ms": float(avg_execution_time_ms),
            "avg_result_count": float(avg_result_count),
            "top_queries": top_queries,
        }