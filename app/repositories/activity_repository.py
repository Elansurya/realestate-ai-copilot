"""
backend/app/repositories/activity_repository.py

Data access layer for the Activity Timeline module.

This repository is intentionally free of business logic and domain
validation. It is responsible solely for translating well-formed
requests into SQLAlchemy 2.x async queries against the ``activities``
table and returning ORM instances or primitive aggregation results.
All domain validation and exception raising lives in
``app.services.activity_service.ActivityService``.

Mirrors: app/repositories/audit_log_repository.py
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activity import (
    Activity,
    ActivityModule,
    ActivityPriority,
    ActivityStatus,
    ActivityType,
)

__all__ = ["ActivityRepository"]


class ActivityRepository:
    """Provides raw persistence operations for :class:`Activity` entities.

    Attributes:
        session: The active asynchronous SQLAlchemy session used for all
            database operations issued by this repository.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Initializes the repository with an active database session.

        Args:
            session: The asynchronous SQLAlchemy session to use for queries.
        """
        self.session = session

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create(self, data: dict[str, Any]) -> Activity:
        """Persists a new activity timeline entry.

        Args:
            data: Mapping of column names to values for the new row.

        Returns:
            Activity: The newly created, refreshed ORM instance.
        """
        entry = Activity(**data)
        self.session.add(entry)
        await self.session.flush()
        await self.session.refresh(entry)
        return entry

    async def bulk_create(self, rows: Sequence[dict[str, Any]]) -> list[Activity]:
        """Persists multiple activity timeline entries in a single flush.

        Args:
            rows: Sequence of column-name-to-value mappings for each new row.

        Returns:
            list[Activity]: The newly created, refreshed ORM instances in
            the same order as the input sequence.
        """
        entries = [Activity(**row) for row in rows]
        self.session.add_all(entries)
        await self.session.flush()
        for entry in entries:
            await self.session.refresh(entry)
        return entries

    async def get_by_id(
        self, activity_id: uuid.UUID, *, include_deleted: bool = False
    ) -> Optional[Activity]:
        """Fetches a single activity entry by its primary key.

        Args:
            activity_id: The UUID primary key of the entry.
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            Optional[Activity]: The matching entry, or ``None`` if not found.
        """
        stmt = select(Activity).where(Activity.id == activity_id)
        if not include_deleted:
            stmt = stmt.where(Activity.is_deleted.is_(False))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update(self, activity: Activity, data: dict[str, Any]) -> Activity:
        """Applies a partial set of column updates to an existing entry.

        Args:
            activity: The ORM instance to mutate, previously loaded via
                :meth:`get_by_id`.
            data: Mapping of column names to their new values.

        Returns:
            Activity: The updated, refreshed ORM instance.
        """
        for key, value in data.items():
            setattr(activity, key, value)
        await self.session.flush()
        await self.session.refresh(activity)
        return activity

    async def soft_delete(self, activity: Activity) -> Activity:
        """Marks an activity entry as soft-deleted.

        Args:
            activity: The ORM instance to soft-delete.

        Returns:
            Activity: The updated, refreshed ORM instance.
        """
        activity.is_deleted = True
        activity.deleted_at = datetime.now(timezone.utc)
        await self.session.flush()
        await self.session.refresh(activity)
        return activity

    async def restore(self, activity: Activity) -> Activity:
        """Reverses a soft-delete on an activity entry.

        Args:
            activity: The ORM instance to restore.

        Returns:
            Activity: The updated, refreshed ORM instance.
        """
        activity.is_deleted = False
        activity.deleted_at = None
        await self.session.flush()
        await self.session.refresh(activity)
        return activity

    async def bulk_soft_delete(self, ids: Sequence[uuid.UUID]) -> int:
        """Soft-deletes a specific set of activity entries by id.

        Args:
            ids: The primary keys of the entries to soft-delete.

        Returns:
            int: The number of rows affected.
        """
        if not ids:
            return 0
        stmt = select(Activity).where(
            Activity.id.in_(ids), Activity.is_deleted.is_(False)
        )
        result = await self.session.execute(stmt)
        entries = list(result.scalars().all())
        now = datetime.now(timezone.utc)
        for entry in entries:
            entry.is_deleted = True
            entry.deleted_at = now
        await self.session.flush()
        return len(entries)

    # ------------------------------------------------------------------
    # Query building helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_filters(
        stmt: Select,
        *,
        module: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        action: Optional[str] = None,
        priority: Optional[str] = None,
        status: Optional[str] = None,
        performed_by_id: Optional[int] = None,
        assigned_to_id: Optional[int] = None,
        source: Optional[str] = None,
        search: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        include_deleted: bool = False,
    ) -> Select:
        """Applies the supplied filter predicates to a base select statement.

        Args:
            stmt: The base SQLAlchemy select statement to constrain.
            module: Restrict to entries in this owning module.
            entity_type: Restrict to entries for this entity type.
            entity_id: Restrict to entries for this entity id.
            action: Restrict to entries with this action.
            priority: Restrict to entries with this priority.
            status: Restrict to entries with this lifecycle status.
            performed_by_id: Restrict to entries performed by this user.
            assigned_to_id: Restrict to entries assigned to this user.
            source: Restrict to entries with this origin source.
            search: Case-insensitive substring match against title/description.
            date_from: Inclusive lower bound on ``created_at``.
            date_to: Inclusive upper bound on ``created_at``.
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            Select: The statement with all applicable predicates applied.
        """
        conditions = []

        if not include_deleted:
            conditions.append(Activity.is_deleted.is_(False))
        if module is not None:
            conditions.append(Activity.module == module)
        if entity_type is not None:
            conditions.append(Activity.entity_type == entity_type)
        if entity_id is not None:
            conditions.append(Activity.entity_id == entity_id)
        if action is not None:
            conditions.append(Activity.action == action)
        if priority is not None:
            conditions.append(Activity.priority == priority)
        if status is not None:
            conditions.append(Activity.status == status)
        if performed_by_id is not None:
            conditions.append(Activity.performed_by_id == performed_by_id)
        if assigned_to_id is not None:
            conditions.append(Activity.assigned_to_id == assigned_to_id)
        if source is not None:
            conditions.append(Activity.source == source)
        if search:
            term = f"%{search}%"
            conditions.append(
                or_(Activity.title.ilike(term), Activity.description.ilike(term))
            )
        if date_from is not None:
            conditions.append(Activity.created_at >= date_from)
        if date_to is not None:
            conditions.append(Activity.created_at <= date_to)

        if conditions:
            stmt = stmt.where(and_(*conditions))
        return stmt

    # ------------------------------------------------------------------
    # Listing / searching
    # ------------------------------------------------------------------

    async def list_activities(
        self,
        *,
        module: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        action: Optional[str] = None,
        priority: Optional[str] = None,
        status: Optional[str] = None,
        performed_by_id: Optional[int] = None,
        assigned_to_id: Optional[int] = None,
        source: Optional[str] = None,
        search: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        include_deleted: bool = False,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[Activity], int]:
        """Retrieves a filtered, sorted, paginated page of activity entries.

        Args:
            module: Optional owning-module filter.
            entity_type: Optional entity type filter.
            entity_id: Optional entity id filter.
            action: Optional action filter.
            priority: Optional priority filter.
            status: Optional lifecycle status filter.
            performed_by_id: Optional acting-user filter.
            assigned_to_id: Optional assigned-user filter.
            source: Optional origin source filter.
            search: Optional free-text search on title/description.
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.
            include_deleted: Whether soft-deleted rows should be considered.
            page: 1-indexed page number.
            page_size: Number of rows per page.
            sort_by: Column name to order by.
            sort_order: ``"asc"`` or ``"desc"``.

        Returns:
            tuple[list[Activity], int]: The page of matching entries and the
            total count of entries matching the filters (ignoring pagination).
        """
        filter_kwargs: dict[str, Any] = dict(
            module=module,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            priority=priority,
            status=status,
            performed_by_id=performed_by_id,
            assigned_to_id=assigned_to_id,
            source=source,
            search=search,
            date_from=date_from,
            date_to=date_to,
            include_deleted=include_deleted,
        )

        base_stmt = self._apply_filters(select(Activity), **filter_kwargs)
        count_stmt = self._apply_filters(
            select(func.count()).select_from(Activity), **filter_kwargs
        )

        sort_column = getattr(Activity, sort_by, Activity.created_at)
        order_expr = sort_column.asc() if sort_order == "asc" else sort_column.desc()

        page = max(page, 1)
        page_size = max(page_size, 1)
        offset = (page - 1) * page_size

        list_stmt = base_stmt.order_by(order_expr).offset(offset).limit(page_size)

        total_result = await self.session.execute(count_stmt)
        total = total_result.scalar_one()

        result = await self.session.execute(list_stmt)
        items = list(result.scalars().all())

        return items, total

    async def search_activities(
        self,
        search_term: str,
        *,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[Activity], int]:
        """Performs a free-text search over activity titles/descriptions.

        Args:
            search_term: Case-insensitive substring to match against the
                title and description fields.
            page: 1-indexed page number.
            page_size: Number of rows per page.
            sort_by: Column name to order by.
            sort_order: ``"asc"`` or ``"desc"``.

        Returns:
            tuple[list[Activity], int]: The page of matching entries and the
            total count of matching entries.
        """
        return await self.list_activities(
            search=search_term,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
        )

    # ------------------------------------------------------------------
    # Timeline feeds
    # ------------------------------------------------------------------

    async def get_timeline_by_entity(
        self,
        entity_type: str,
        entity_id: str,
        *,
        page: int = 1,
        page_size: int = 200,
        sort_order: str = "asc",
        include_deleted: bool = False,
    ) -> tuple[list[Activity], int]:
        """Retrieves the chronological activity feed for a single entity.

        Args:
            entity_type: The entity/table the timeline belongs to.
            entity_id: The primary key of the entity.
            page: 1-indexed page number.
            page_size: Number of rows per page.
            sort_order: ``"asc"`` (chronological) or ``"desc"`` (newest first).
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            tuple[list[Activity], int]: The page of matching entries and the
            total count of entries for the entity.
        """
        return await self.list_activities(
            entity_type=entity_type,
            entity_id=entity_id,
            include_deleted=include_deleted,
            page=page,
            page_size=page_size,
            sort_by="created_at",
            sort_order=sort_order,
        )

    async def get_timeline_by_module(
        self,
        module: str,
        *,
        page: int = 1,
        page_size: int = 50,
        sort_order: str = "desc",
        include_deleted: bool = False,
    ) -> tuple[list[Activity], int]:
        """Retrieves the activity feed for an entire owning module.

        Args:
            module: The owning module to scope the feed to.
            page: 1-indexed page number.
            page_size: Number of rows per page.
            sort_order: ``"asc"`` or ``"desc"``.
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            tuple[list[Activity], int]: The page of matching entries and the
            total count of entries for the module.
        """
        return await self.list_activities(
            module=module,
            include_deleted=include_deleted,
            page=page,
            page_size=page_size,
            sort_by="created_at",
            sort_order=sort_order,
        )

    async def get_timeline_by_user(
        self,
        user_id: int,
        *,
        page: int = 1,
        page_size: int = 50,
        sort_order: str = "desc",
        include_deleted: bool = False,
    ) -> tuple[list[Activity], int]:
        """Retrieves the activity feed involving a specific user, whether as
        the performer of the action or as the assignee.

        Args:
            user_id: The user id to scope the feed to.
            page: 1-indexed page number.
            page_size: Number of rows per page.
            sort_order: ``"asc"`` or ``"desc"``.
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            tuple[list[Activity], int]: The page of matching entries and the
            total count of entries involving the user.
        """
        base_stmt = select(Activity).where(
            or_(
                Activity.performed_by_id == user_id,
                Activity.assigned_to_id == user_id,
            )
        )
        if not include_deleted:
            base_stmt = base_stmt.where(Activity.is_deleted.is_(False))

        count_stmt = select(func.count()).select_from(base_stmt.subquery())

        sort_column = Activity.created_at
        order_expr = sort_column.asc() if sort_order == "asc" else sort_column.desc()

        page = max(page, 1)
        page_size = max(page_size, 1)
        offset = (page - 1) * page_size

        list_stmt = base_stmt.order_by(order_expr).offset(offset).limit(page_size)

        total = (await self.session.execute(count_stmt)).scalar_one()
        items = (await self.session.execute(list_stmt)).scalars().all()
        return list(items), total

    async def get_recent_activities(
        self,
        limit: int = 20,
        *,
        module: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
    ) -> list[Activity]:
        """Fetches the most recently created activity entries.

        Args:
            limit: Maximum number of entries to return.
            module: Optional owning-module filter.
            entity_type: Optional entity type filter.
            entity_id: Optional entity id filter.

        Returns:
            list[Activity]: The most recent entries, newest first.
        """
        stmt = self._apply_filters(
            select(Activity),
            module=module,
            entity_type=entity_type,
            entity_id=entity_id,
        ).order_by(Activity.created_at.desc()).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Statistics / aggregations
    # ------------------------------------------------------------------

    async def get_total_count(
        self,
        *,
        module: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        include_deleted: bool = False,
    ) -> int:
        """Counts the total number of activity entries in scope.

        Args:
            module: Optional owning-module filter.
            entity_type: Optional entity type filter.
            entity_id: Optional entity id filter.
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            int: The total matching entry count.
        """
        stmt = self._apply_filters(
            select(func.count()).select_from(Activity),
            module=module,
            entity_type=entity_type,
            entity_id=entity_id,
            date_from=date_from,
            date_to=date_to,
            include_deleted=include_deleted,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def count_by_module(
        self,
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        include_deleted: bool = False,
    ) -> dict[str, int]:
        """Counts activity entries grouped by owning module.

        Args:
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            dict[str, int]: Mapping of module value to entry count.
        """
        stmt = self._apply_filters(
            select(Activity.module, func.count().label("count")),
            date_from=date_from,
            date_to=date_to,
            include_deleted=include_deleted,
        ).group_by(Activity.module)
        result = await self.session.execute(stmt)
        return {row.module.value: row.count for row in result.all()}

    async def count_by_action(
        self,
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        include_deleted: bool = False,
    ) -> dict[str, int]:
        """Counts activity entries grouped by action.

        Args:
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            dict[str, int]: Mapping of action value to entry count.
        """
        stmt = self._apply_filters(
            select(Activity.action, func.count().label("count")),
            date_from=date_from,
            date_to=date_to,
            include_deleted=include_deleted,
        ).group_by(Activity.action)
        result = await self.session.execute(stmt)
        return {row.action.value: row.count for row in result.all()}

    async def count_by_user(
        self,
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        include_deleted: bool = False,
        limit: int = 20,
    ) -> dict[int, int]:
        """Counts activity entries grouped by the acting (performed-by) user.

        Args:
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.
            include_deleted: Whether soft-deleted rows should be considered.
            limit: Maximum number of distinct users to return, ordered by
                descending entry count.

        Returns:
            dict[int, int]: Mapping of user id to entry count.
        """
        stmt = (
            self._apply_filters(
                select(Activity.performed_by_id, func.count().label("count")),
                date_from=date_from,
                date_to=date_to,
                include_deleted=include_deleted,
            )
            .where(Activity.performed_by_id.is_not(None))
            .group_by(Activity.performed_by_id)
            .order_by(func.count().desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return {row.performed_by_id: row.count for row in result.all()}

    async def count_by_status(
        self,
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        include_deleted: bool = False,
    ) -> dict[str, int]:
        """Counts activity entries grouped by lifecycle status.

        Args:
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            dict[str, int]: Mapping of status value to entry count.
        """
        stmt = self._apply_filters(
            select(Activity.status, func.count().label("count")),
            date_from=date_from,
            date_to=date_to,
            include_deleted=include_deleted,
        ).group_by(Activity.status)
        result = await self.session.execute(stmt)
        return {row.status.value: row.count for row in result.all()}

    async def count_by_priority(
        self,
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        include_deleted: bool = False,
    ) -> dict[str, int]:
        """Counts activity entries grouped by priority.

        Args:
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            dict[str, int]: Mapping of priority value to entry count.
        """
        stmt = self._apply_filters(
            select(Activity.priority, func.count().label("count")),
            date_from=date_from,
            date_to=date_to,
            include_deleted=include_deleted,
        ).group_by(Activity.priority)
        result = await self.session.execute(stmt)
        return {row.priority.value: row.count for row in result.all()}