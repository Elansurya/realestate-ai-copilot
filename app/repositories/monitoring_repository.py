"""
backend/app/repositories/monitoring_repository.py

Async SQLAlchemy 2.x repository for the Enterprise Monitoring & Health
module of the Enterprise Real Estate AI Copilot CRM.

This repository is a pure data-access layer around the `SystemHealth`
ORM model (see `app.models.monitoring`). It contains no business rules,
no validation beyond what the database/schema layers already enforce,
and raises no domain exceptions -- it only queries, mutates, and returns
ORM instances (or `None` / aggregate primitives) so the service layer
can apply business rules and translate absence-of-data into the
appropriate domain exception.

Conventions:
    - Every method accepts an `AsyncSession` injected at construction
      time (mirrors the repository pattern already used for
      `DocumentRepository` / `TaskRepository` / `IntegrationRepository`).
    - Soft-deleted rows (`is_deleted = True`) are excluded by default
      everywhere except `restore()` and the explicit
      `include_deleted` / `is_deleted` filter toggles, mirroring
      `SystemHealth.__table_args__`'s soft-delete semantics.
    - Pagination, filtering, and sorting are driven entirely by the
      `HealthFilter` schema so the service layer never has to build
      raw SQLAlchemy clauses itself.
    - `SystemHealth` rows are continuously upserted-in-place
      (one live row per `component_name` + `component_type` pair, per
      the model's `UniqueConstraint`). Because there is no separate
      history/audit table in this phase, `get_health_history()` is
      implemented pragmatically over the current snapshot rows for a
      component and its child rollups (see method docstring for the
      exact semantics and its documented limitation).
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.monitoring import ComponentType, HealthStatus, SystemHealth
from app.schemas.monitoring import HealthFilter, SystemHealthCreate, SystemHealthUpdate

__all__ = ["MonitoringRepository"]


class MonitoringRepository:
    """Data-access layer for `SystemHealth` health snapshot records."""

    def __init__(self, session: AsyncSession) -> None:
        """
        Args:
            session: The async SQLAlchemy session this repository will
                use for all queries and mutations.
        """
        self._session = session

    # ------------------------------------------------------------------
    # Internal Query Helpers
    # ------------------------------------------------------------------
    def _base_select(self, include_deleted: bool = False) -> Select:
        """
        Builds the base SELECT statement for `SystemHealth`, with
        soft-deleted rows excluded by default.

        Args:
            include_deleted: If True, soft-deleted rows are included.

        Returns:
            A `Select` statement over `SystemHealth`.
        """
        stmt = select(SystemHealth)
        if not include_deleted:
            stmt = stmt.where(SystemHealth.is_deleted.is_(False))
        return stmt

    @staticmethod
    def _apply_filters(stmt: Select, filters: HealthFilter) -> Select:
        """
        Applies the filterable fields of `HealthFilter` to a `Select`
        statement as WHERE clauses.

        Args:
            stmt: The base statement to apply filters to.
            filters: The filter/search parameters supplied by the caller.

        Returns:
            The filtered `Select` statement.
        """
        conditions = []

        if filters.component_name is not None:
            conditions.append(SystemHealth.component_name == filters.component_name)

        if filters.component_type is not None:
            conditions.append(SystemHealth.component_type == filters.component_type)

        if filters.status is not None:
            conditions.append(SystemHealth.status == filters.status)

        if filters.parent_component_id is not None:
            conditions.append(
                SystemHealth.parent_component_id == filters.parent_component_id
            )

        if filters.is_active is not None:
            conditions.append(SystemHealth.is_active == filters.is_active)

        # `is_deleted` filter overrides the default soft-delete exclusion
        # applied by `_base_select`, so it is always applied explicitly.
        conditions.append(SystemHealth.is_deleted == bool(filters.is_deleted))

        if filters.search:
            like_term = f"%{filters.search.lower()}%"
            conditions.append(func.lower(SystemHealth.component_name).like(like_term))

        if conditions:
            stmt = stmt.where(and_(*conditions))

        return stmt

    @staticmethod
    def _apply_sorting(stmt: Select, filters: HealthFilter) -> Select:
        """
        Applies ORDER BY to a `Select` statement based on the validated
        `sort_by` / `sort_order` fields of `HealthFilter`.

        Args:
            stmt: The statement to apply sorting to.
            filters: The filter parameters containing sort directives.

        Returns:
            The sorted `Select` statement.
        """
        column = getattr(SystemHealth, filters.sort_by)
        return stmt.order_by(column.desc() if filters.sort_order == "desc" else column.asc())

    def _with_relationships(self, stmt: Select) -> Select:
        """
        Eagerly loads the relationships commonly needed by callers
        (parent/child rollups) to avoid N+1 lazy-load round trips.

        Args:
            stmt: The statement to augment with eager-loading options.

        Returns:
            The augmented `Select` statement.
        """
        return stmt.options(
            selectinload(SystemHealth.parent_component),
            selectinload(SystemHealth.child_components),
        )

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------
    async def create(
        self,
        data: SystemHealthCreate,
        created_by_id: Optional[int] = None,
    ) -> SystemHealth:
        """
        Inserts a new health snapshot record.

        Args:
            data: The validated creation payload.
            created_by_id: The internal user ID that created this
                record interactively, if any.

        Returns:
            The newly persisted `SystemHealth` instance (flushed, not
            yet committed).
        """
        record = SystemHealth(
            **data.model_dump(exclude_unset=False),
            created_by_id=created_by_id,
            updated_by_id=created_by_id,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)
        return record

    # ------------------------------------------------------------------
    # Retrieve
    # ------------------------------------------------------------------
    async def get_by_id(
        self,
        health_id: uuid.UUID,
        include_deleted: bool = False,
    ) -> Optional[SystemHealth]:
        """
        Retrieves a single health snapshot record by its primary key.

        Args:
            health_id: The UUID of the record to retrieve.
            include_deleted: If True, soft-deleted rows are eligible.

        Returns:
            The matching `SystemHealth` instance, or `None` if not found.
        """
        stmt = self._with_relationships(
            self._base_select(include_deleted=include_deleted)
        ).where(SystemHealth.id == health_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_name_and_type(
        self,
        component_name: str,
        component_type: ComponentType,
        include_deleted: bool = False,
    ) -> Optional[SystemHealth]:
        """
        Retrieves the live health snapshot for a given component
        identity (`component_name` + `component_type`), which is
        unique per the model's `UniqueConstraint`.

        Args:
            component_name: The exact component name to look up.
            component_type: The component's category.
            include_deleted: If True, soft-deleted rows are eligible.

        Returns:
            The matching `SystemHealth` instance, or `None` if not found.
        """
        stmt = self._with_relationships(
            self._base_select(include_deleted=include_deleted)
        ).where(
            SystemHealth.component_name == component_name,
            SystemHealth.component_type == component_type,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_component_status(
        self,
        component_name: str,
        component_type: ComponentType,
    ) -> Optional[SystemHealth]:
        """
        Retrieves the current status snapshot of a single monitored
        component. Thin, semantically-named wrapper over
        `get_by_name_and_type()` for dashboard/status-check call sites.

        Args:
            component_name: The exact component name to look up.
            component_type: The component's category.

        Returns:
            The matching `SystemHealth` instance, or `None` if not found
            or soft-deleted.
        """
        return await self.get_by_name_and_type(component_name, component_type)

    async def get_health_history(
        self,
        component_name: str,
        component_type: ComponentType,
        limit: int = 50,
    ) -> list[SystemHealth]:
        """
        Retrieves the health history available for a component.

        NOTE: `SystemHealth` rows are continuously upserted-in-place
        (one live row per component identity) rather than appended as
        discrete historical events, so there is at most one live row
        per component plus any of its child rollup rows. This method
        therefore returns the component's own live snapshot together
        with the live snapshots of any components rolled up under it
        (`parent_component_id`), ordered by most-recently-checked
        first, which is the closest true "history" available without a
        dedicated audit/history table.

        Args:
            component_name: The exact component name to look up.
            component_type: The component's category.
            limit: Maximum number of rows to return.

        Returns:
            A list of `SystemHealth` instances ordered by
            `last_health_check_at` descending (most recent first).
        """
        parent = await self.get_by_name_and_type(component_name, component_type)
        if parent is None:
            return []

        stmt = self._base_select().where(
            or_(
                SystemHealth.id == parent.id,
                SystemHealth.parent_component_id == parent.id,
            )
        )
        stmt = stmt.order_by(SystemHealth.last_health_check_at.desc()).limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # List / Search / Filter / Sort / Paginate
    # ------------------------------------------------------------------
    async def list_paginated(
        self,
        filters: HealthFilter,
    ) -> tuple[list[SystemHealth], int]:
        """
        Retrieves a filtered, sorted, paginated page of health snapshot
        records, along with the total matching count (pre-pagination).

        Supports free-text `search` (matched against `component_name`),
        exact-match filtering on component identity/status/parent/
        active/deleted, `sort_by` / `sort_order`, and `page` /
        `page_size` pagination -- all sourced from `HealthFilter`.

        Args:
            filters: The validated filter/sort/pagination parameters.

        Returns:
            A tuple of (page of `SystemHealth` instances, total count
            of records matching the filters across all pages).
        """
        base_stmt = self._apply_filters(select(SystemHealth), filters)

        count_stmt = select(func.count()).select_from(base_stmt.subquery())
        total = (await self._session.execute(count_stmt)).scalar_one()

        page_stmt = self._with_relationships(self._apply_filters(select(SystemHealth), filters))
        page_stmt = self._apply_sorting(page_stmt, filters)
        page_stmt = page_stmt.offset((filters.page - 1) * filters.page_size).limit(
            filters.page_size
        )

        result = await self._session.execute(page_stmt)
        items = list(result.scalars().all())
        return items, total

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------
    async def update(
        self,
        health_id: uuid.UUID,
        data: SystemHealthUpdate,
        updated_by_id: Optional[int] = None,
    ) -> Optional[SystemHealth]:
        """
        Applies a partial (PATCH-style) update to an existing health
        snapshot record.

        Args:
            health_id: The UUID of the record to update.
            data: The validated partial update payload; only fields
                explicitly set by the caller are applied.
            updated_by_id: The internal user ID performing the update
                interactively, if any.

        Returns:
            The updated `SystemHealth` instance, or `None` if no record
            with that ID exists (including soft-deleted records, which
            are excluded from update eligibility).
        """
        record = await self.get_by_id(health_id, include_deleted=False)
        if record is None:
            return None

        update_fields = data.model_dump(exclude_unset=True)
        for field_name, value in update_fields.items():
            setattr(record, field_name, value)

        record.updated_by_id = updated_by_id
        await self._session.flush()
        await self._session.refresh(record)
        return record

    async def upsert_health_check_result(
        self,
        component_name: str,
        component_type: ComponentType,
        data: SystemHealthCreate,
        actor_id: Optional[int] = None,
    ) -> SystemHealth:
        """
        Inserts a new snapshot row, or updates the existing live row in
        place, for the given component identity. This is the primary
        write path used by the automated health-check worker, matching
        the model's "continuously upserted-in-place" design.

        Args:
            component_name: The exact component name to upsert.
            component_type: The component's category.
            data: The full snapshot payload to write.
            actor_id: The internal user ID performing the write
                interactively, or `None` for scheduler/worker writes.

        Returns:
            The created or updated `SystemHealth` instance.
        """
        existing = await self.get_by_name_and_type(component_name, component_type)
        if existing is None:
            return await self.create(data, created_by_id=actor_id)

        update_payload = SystemHealthUpdate(**data.model_dump(exclude_unset=False))
        updated = await self.update(existing.id, update_payload, updated_by_id=actor_id)
        assert updated is not None  # existing was just fetched above
        return updated

    # ------------------------------------------------------------------
    # Delete / Restore
    # ------------------------------------------------------------------
    async def soft_delete(
        self,
        health_id: uuid.UUID,
        deleted_by_id: Optional[int] = None,
    ) -> Optional[SystemHealth]:
        """
        Soft-deletes a health snapshot record, setting `is_deleted`,
        `deleted_at`, and `deleted_by_id` in a single mutation, exactly
        as required by `ck_system_health_soft_delete_consistency`.

        Args:
            health_id: The UUID of the record to soft-delete.
            deleted_by_id: The internal user ID performing the
                soft-delete, if any.

        Returns:
            The soft-deleted `SystemHealth` instance, or `None` if no
            (non-deleted) record with that ID exists.
        """
        record = await self.get_by_id(health_id, include_deleted=False)
        if record is None:
            return None

        record.is_deleted = True
        record.deleted_at = func.now()
        record.deleted_by_id = deleted_by_id
        await self._session.flush()
        await self._session.refresh(record)
        return record

    async def restore(self, health_id: uuid.UUID) -> Optional[SystemHealth]:
        """
        Restores a previously soft-deleted health snapshot record,
        clearing `is_deleted`, `deleted_at`, and `deleted_by_id`.

        Args:
            health_id: The UUID of the record to restore.

        Returns:
            The restored `SystemHealth` instance, or `None` if no
            soft-deleted record with that ID exists.
        """
        record = await self.get_by_id(health_id, include_deleted=True)
        if record is None or not record.is_deleted:
            return None

        record.is_deleted = False
        record.deleted_at = None
        record.deleted_by_id = None
        await self._session.flush()
        await self._session.refresh(record)
        return record

    # ------------------------------------------------------------------
    # Statistics / Aggregation
    # ------------------------------------------------------------------
    async def count_by_status(self) -> dict[HealthStatus, int]:
        """
        Counts active, non-deleted health records grouped by `status`.

        Returns:
            A mapping of `HealthStatus` to the number of matching
            records. Statuses with zero matching records are omitted.
        """
        stmt = (
            select(SystemHealth.status, func.count())
            .where(SystemHealth.is_deleted.is_(False))
            .group_by(SystemHealth.status)
        )
        result = await self._session.execute(stmt)
        return {status: count for status, count in result.all()}

    async def count_by_component_type(self) -> dict[ComponentType, int]:
        """
        Counts active, non-deleted health records grouped by
        `component_type`.

        Returns:
            A mapping of `ComponentType` to the number of matching
            records. Component types with zero matching records are
            omitted.
        """
        stmt = (
            select(SystemHealth.component_type, func.count())
            .where(SystemHealth.is_deleted.is_(False))
            .group_by(SystemHealth.component_type)
        )
        result = await self._session.execute(stmt)
        return {component_type: count for component_type, count in result.all()}

    async def get_aggregate_metrics(self) -> dict[str, Optional[float] | int]:
        """
        Computes system-wide aggregate metrics over all active,
        non-deleted health records: total record count, average
        response time, and summed error/warning counters.

        Returns:
            A dict with keys `total_components`, `average_response_time_ms`,
            `total_error_count`, and `total_warning_count`.
        """
        stmt = select(
            func.count(),
            func.avg(SystemHealth.response_time_ms),
            func.coalesce(func.sum(SystemHealth.error_count), 0),
            func.coalesce(func.sum(SystemHealth.warning_count), 0),
        ).where(SystemHealth.is_deleted.is_(False))

        result = await self._session.execute(stmt)
        total, avg_response_time, total_errors, total_warnings = result.one()

        return {
            "total_components": total,
            "average_response_time_ms": (
                float(avg_response_time) if avg_response_time is not None else None
            ),
            "total_error_count": int(total_errors),
            "total_warning_count": int(total_warnings),
        }

    async def list_all_active(self) -> list[SystemHealth]:
        """
        Retrieves every active (`is_active = True`), non-deleted health
        snapshot record, for use in whole-system health aggregation
        (e.g. the top-level `/health` overview).

        Returns:
            The list of all active, non-deleted `SystemHealth` instances.
        """
        stmt = self._base_select().where(SystemHealth.is_active.is_(True))
        result = await self._session.execute(stmt)
        return list(result.scalars().all())