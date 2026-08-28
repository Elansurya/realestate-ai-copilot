"""Data access layer for the Audit Log module.

This repository is intentionally free of business logic and domain
validation. It is responsible solely for translating well-formed
requests into SQLAlchemy 2.x async queries against the ``audit_logs``
table and returning ORM instances or primitive aggregation results.
"""

import uuid
from datetime import datetime
from typing import Any, Optional, Sequence

from sqlalchemy import Select, and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import (
    AuditAction,
    AuditLog,
    AuditSeverity,
    AuditStatus,
)

__all__ = ["AuditLogRepository"]


class AuditLogRepository:
    """Provides raw persistence operations for :class:`AuditLog` entities.

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

    async def create(self, data: dict[str, Any]) -> AuditLog:
        """Persists a new audit log entry.

        Args:
            data: Mapping of column names to values for the new row.

        Returns:
            AuditLog: The newly created, refreshed ORM instance.
        """
        entry = AuditLog(**data)
        self.session.add(entry)
        await self.session.flush()
        await self.session.refresh(entry)
        return entry

    async def bulk_create(self, rows: Sequence[dict[str, Any]]) -> list[AuditLog]:
        """Persists multiple audit log entries in a single flush.

        Args:
            rows: Sequence of column-name-to-value mappings for each new row.

        Returns:
            list[AuditLog]: The newly created, refreshed ORM instances in
            the same order as the input sequence.
        """
        entries = [AuditLog(**row) for row in rows]
        self.session.add_all(entries)
        await self.session.flush()
        for entry in entries:
            await self.session.refresh(entry)
        return entries

    async def get_by_id(self, audit_log_id: uuid.UUID) -> Optional[AuditLog]:
        """Fetches a single audit log entry by its primary key.

        Args:
            audit_log_id: The UUID primary key of the entry.

        Returns:
            Optional[AuditLog]: The matching entry, or ``None`` if not found.
        """
        result = await self.session.execute(
            select(AuditLog).where(AuditLog.id == audit_log_id)
        )
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Query building helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_filters(
        stmt: Select,
        *,
        user_id: Optional[int] = None,
        module: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        action: Optional[AuditAction] = None,
        severity: Optional[AuditSeverity] = None,
        status: Optional[AuditStatus] = None,
        request_id: Optional[str] = None,
        search: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> Select:
        """Applies the supplied filter predicates to a base select statement.

        Args:
            stmt: The base SQLAlchemy select statement to constrain.
            user_id: Restrict to entries authored by this user.
            module: Restrict to entries in this module.
            entity_type: Restrict to entries for this entity type.
            entity_id: Restrict to entries for this entity id.
            action: Restrict to entries with this action.
            severity: Restrict to entries with this severity.
            status: Restrict to entries with this status.
            request_id: Restrict to entries with this correlation id.
            search: Case-insensitive substring match against description.
            date_from: Inclusive lower bound on ``created_at``.
            date_to: Inclusive upper bound on ``created_at``.

        Returns:
            Select: The statement with all applicable predicates applied.
        """
        conditions = []

        if user_id is not None:
            conditions.append(AuditLog.user_id == user_id)
        if module is not None:
            conditions.append(AuditLog.module == module)
        if entity_type is not None:
            conditions.append(AuditLog.entity_type == entity_type)
        if entity_id is not None:
            conditions.append(AuditLog.entity_id == entity_id)
        if action is not None:
            conditions.append(AuditLog.action == action)
        if severity is not None:
            conditions.append(AuditLog.severity == severity)
        if status is not None:
            conditions.append(AuditLog.status == status)
        if request_id is not None:
            conditions.append(AuditLog.request_id == request_id)
        if search:
            conditions.append(AuditLog.description.ilike(f"%{search}%"))
        if date_from is not None:
            conditions.append(AuditLog.created_at >= date_from)
        if date_to is not None:
            conditions.append(AuditLog.created_at <= date_to)

        if conditions:
            stmt = stmt.where(and_(*conditions))
        return stmt

    # ------------------------------------------------------------------
    # Listing / searching
    # ------------------------------------------------------------------

    async def list_logs(
        self,
        *,
        user_id: Optional[int] = None,
        module: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        action: Optional[AuditAction] = None,
        severity: Optional[AuditSeverity] = None,
        status: Optional[AuditStatus] = None,
        request_id: Optional[str] = None,
        search: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[AuditLog], int]:
        """Retrieves a filtered, sorted, paginated page of audit log entries.

        Args:
            user_id: Optional acting-user filter.
            module: Optional module filter.
            entity_type: Optional entity type filter.
            entity_id: Optional entity id filter.
            action: Optional action filter.
            severity: Optional severity filter.
            status: Optional status filter.
            request_id: Optional correlation id filter.
            search: Optional free-text search on description.
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.
            page: 1-indexed page number.
            page_size: Number of rows per page.
            sort_by: Column name to order by.
            sort_order: ``"asc"`` or ``"desc"``.

        Returns:
            tuple[list[AuditLog], int]: The page of matching entries and the
            total count of entries matching the filters (ignoring pagination).
        """
        base_stmt = self._apply_filters(
            select(AuditLog),
            user_id=user_id,
            module=module,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            severity=severity,
            status=status,
            request_id=request_id,
            search=search,
            date_from=date_from,
            date_to=date_to,
        )

        count_stmt = self._apply_filters(
            select(func.count()).select_from(AuditLog),
            user_id=user_id,
            module=module,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            severity=severity,
            status=status,
            request_id=request_id,
            search=search,
            date_from=date_from,
            date_to=date_to,
        )

        sort_column = getattr(AuditLog, sort_by, AuditLog.created_at)
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

    async def search_logs(
        self,
        search_term: str,
        *,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[AuditLog], int]:
        """Performs a free-text search over audit log descriptions.

        Args:
            search_term: Case-insensitive substring to match against
                the description field.
            page: 1-indexed page number.
            page_size: Number of rows per page.
            sort_by: Column name to order by.
            sort_order: ``"asc"`` or ``"desc"``.

        Returns:
            tuple[list[AuditLog], int]: The page of matching entries and the
            total count of matching entries.
        """
        return await self.list_logs(
            search=search_term,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
        )

    # ------------------------------------------------------------------
    # Dashboard / statistics aggregations
    # ------------------------------------------------------------------

    async def count_by_module(
        self,
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> dict[str, int]:
        """Counts audit log entries grouped by module.

        Args:
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.

        Returns:
            dict[str, int]: Mapping of module name to entry count.
        """
        stmt = self._apply_filters(
            select(AuditLog.module, func.count().label("count")),
            date_from=date_from,
            date_to=date_to,
        ).group_by(AuditLog.module)
        result = await self.session.execute(stmt)
        return {row.module: row.count for row in result.all()}

    async def count_by_action(
        self,
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> dict[str, int]:
        """Counts audit log entries grouped by action.

        Args:
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.

        Returns:
            dict[str, int]: Mapping of action value to entry count.
        """
        stmt = self._apply_filters(
            select(AuditLog.action, func.count().label("count")),
            date_from=date_from,
            date_to=date_to,
        ).group_by(AuditLog.action)
        result = await self.session.execute(stmt)
        return {row.action.value: row.count for row in result.all()}

    async def count_by_user(
        self,
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        limit: int = 20,
    ) -> dict[int, int]:
        """Counts audit log entries grouped by acting user.

        Args:
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.
            limit: Maximum number of distinct users to return, ordered by
                descending entry count.

        Returns:
            dict[int, int]: Mapping of user id to entry count.
        """
        stmt = (
            self._apply_filters(
                select(AuditLog.user_id, func.count().label("count")),
                date_from=date_from,
                date_to=date_to,
            )
            .where(AuditLog.user_id.is_not(None))
            .group_by(AuditLog.user_id)
            .order_by(func.count().desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return {row.user_id: row.count for row in result.all()}

    async def count_by_severity(
        self,
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> dict[str, int]:
        """Counts audit log entries grouped by severity.

        Args:
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.

        Returns:
            dict[str, int]: Mapping of severity value to entry count.
        """
        stmt = self._apply_filters(
            select(AuditLog.severity, func.count().label("count")),
            date_from=date_from,
            date_to=date_to,
        ).group_by(AuditLog.severity)
        result = await self.session.execute(stmt)
        return {row.severity.value: row.count for row in result.all()}

    async def count_by_status(
        self,
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> dict[str, int]:
        """Counts audit log entries grouped by outcome status.

        Args:
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.

        Returns:
            dict[str, int]: Mapping of status value to entry count.
        """
        stmt = self._apply_filters(
            select(AuditLog.status, func.count().label("count")),
            date_from=date_from,
            date_to=date_to,
        ).group_by(AuditLog.status)
        result = await self.session.execute(stmt)
        return {row.status.value: row.count for row in result.all()}

    async def get_total_count(
        self,
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> int:
        """Counts the total number of audit log entries in scope.

        Args:
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.

        Returns:
            int: The total matching entry count.
        """
        stmt = self._apply_filters(
            select(func.count()).select_from(AuditLog),
            date_from=date_from,
            date_to=date_to,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    # ------------------------------------------------------------------
    # Recent activity feeds
    # ------------------------------------------------------------------

    async def get_latest_activities(self, limit: int = 20) -> list[AuditLog]:
        """Fetches the most recently created audit log entries.

        Args:
            limit: Maximum number of entries to return.

        Returns:
            list[AuditLog]: The most recent entries, newest first.
        """
        result = await self.session.execute(
            select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def get_recent_failed_logs(self, limit: int = 20) -> list[AuditLog]:
        """Fetches the most recent failed audit log entries.

        Args:
            limit: Maximum number of entries to return.

        Returns:
            list[AuditLog]: The most recent entries with ``status == FAILED``.
        """
        result = await self.session.execute(
            select(AuditLog)
            .where(AuditLog.status == AuditStatus.FAILED)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_recent_critical_logs(self, limit: int = 20) -> list[AuditLog]:
        """Fetches the most recent critical-severity audit log entries.

        Args:
            limit: Maximum number of entries to return.

        Returns:
            list[AuditLog]: The most recent entries with
            ``severity == CRITICAL``.
        """
        result = await self.session.execute(
            select(AuditLog)
            .where(AuditLog.severity == AuditSeverity.CRITICAL)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Retention / cleanup
    # ------------------------------------------------------------------

    async def delete_old_logs(self, before: datetime) -> int:
        """Deletes all audit log entries created before a given timestamp.

        Args:
            before: The exclusive upper bound; entries with
                ``created_at < before`` are deleted.

        Returns:
            int: The number of rows deleted.
        """
        stmt = delete(AuditLog).where(AuditLog.created_at < before)
        result = await self.session.execute(stmt)
        return result.rowcount or 0

    async def bulk_delete(self, ids: Sequence[uuid.UUID]) -> int:
        """Deletes a specific set of audit log entries by id.

        Args:
            ids: The primary keys of the entries to delete.

        Returns:
            int: The number of rows deleted.
        """
        if not ids:
            return 0
        stmt = delete(AuditLog).where(AuditLog.id.in_(ids))
        result = await self.session.execute(stmt)
        return result.rowcount or 0