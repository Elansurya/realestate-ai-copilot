"""
backend/app/repositories/integration_repository.py

Data access layer for the Integration Management module.

This repository is intentionally free of business logic and domain
validation. It is responsible solely for translating well-formed
requests into SQLAlchemy 2.x async queries against the
``integrations`` table and returning ORM instances or primitive
aggregation results. All domain validation and exception raising
lives in ``app.services.integration_service.IntegrationService``.

Mirrors: app/repositories/task_repository.py (naming/style/transaction
conventions: `self.session`, flush + refresh on writes, no commit --
the commit/rollback boundary belongs to the caller).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integration import (
    AuthenticationType,
    Integration,
    IntegrationProvider,
    IntegrationStatus,
    IntegrationType,
)

__all__ = ["IntegrationRepository"]


class IntegrationRepository:
    """Provides raw persistence operations for :class:`Integration` entities.

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
    async def create(self, data: dict[str, Any]) -> Integration:
        """Persists a new integration.

        Args:
            data: Mapping of column names to values for the new row.

        Returns:
            Integration: The newly created, refreshed ORM instance.
        """
        entry = Integration(**data)
        self.session.add(entry)
        await self.session.flush()
        await self.session.refresh(entry)
        return entry

    async def get_by_id(
        self, integration_id: uuid.UUID, *, include_deleted: bool = False
    ) -> Optional[Integration]:
        """Fetches a single integration by its primary key.

        Args:
            integration_id: The UUID primary key of the integration.
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            Optional[Integration]: The matching integration, or ``None``
            if not found.
        """
        stmt = select(Integration).where(Integration.id == integration_id)
        if not include_deleted:
            stmt = stmt.where(Integration.is_deleted.is_(False))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_name(
        self, name: str, *, include_deleted: bool = False
    ) -> Optional[Integration]:
        """Fetches a single integration by its unique name.

        Args:
            name: The exact integration name to look up.
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            Optional[Integration]: The matching integration, or ``None``
            if not found.
        """
        stmt = select(Integration).where(Integration.name == name)
        if not include_deleted:
            stmt = stmt.where(Integration.is_deleted.is_(False))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update(self, integration: Integration, data: dict[str, Any]) -> Integration:
        """Applies a partial set of column updates to an existing integration.

        Args:
            integration: The ORM instance to mutate, previously loaded via
                :meth:`get_by_id`.
            data: Mapping of column names to their new values.

        Returns:
            Integration: The updated, refreshed ORM instance.
        """
        for key, value in data.items():
            setattr(integration, key, value)
        await self.session.flush()
        await self.session.refresh(integration)
        return integration

    async def soft_delete(self, integration: Integration) -> Integration:
        """Marks an integration as soft-deleted.

        Args:
            integration: The ORM instance to soft-delete.

        Returns:
            Integration: The updated, refreshed ORM instance.
        """
        integration.is_deleted = True
        integration.deleted_at = datetime.now(timezone.utc)
        await self.session.flush()
        await self.session.refresh(integration)
        return integration

    async def restore(self, integration: Integration) -> Integration:
        """Reverses a soft-delete on an integration.

        Args:
            integration: The ORM instance to restore.

        Returns:
            Integration: The updated, refreshed ORM instance.
        """
        integration.is_deleted = False
        integration.deleted_at = None
        await self.session.flush()
        await self.session.refresh(integration)
        return integration

    # ------------------------------------------------------------------
    # Status / lifecycle operations
    # ------------------------------------------------------------------
    async def set_status(
        self, integration: Integration, status: IntegrationStatus
    ) -> Integration:
        """Sets the operational status of an integration.

        Args:
            integration: The ORM instance to mutate.
            status: The new status to apply.

        Returns:
            Integration: The updated, refreshed ORM instance.
        """
        integration.status = status
        await self.session.flush()
        await self.session.refresh(integration)
        return integration

    async def enable(self, integration: Integration) -> Integration:
        """Transitions an integration to ``ACTIVE``.

        Args:
            integration: The ORM instance to mutate.

        Returns:
            Integration: The updated, refreshed ORM instance.
        """
        return await self.set_status(integration, IntegrationStatus.ACTIVE)

    async def disable(self, integration: Integration) -> Integration:
        """Transitions an integration to ``INACTIVE``.

        Args:
            integration: The ORM instance to mutate.

        Returns:
            Integration: The updated, refreshed ORM instance.
        """
        return await self.set_status(integration, IntegrationStatus.INACTIVE)

    async def update_health_check_status(
        self,
        integration: Integration,
        *,
        status: IntegrationStatus,
        checked_at: Optional[datetime] = None,
    ) -> Integration:
        """Records the outcome of a health check against an integration.

        Args:
            integration: The ORM instance to mutate.
            status: The resulting status to apply (e.g. ``ACTIVE`` on
                success, ``FAILED`` on failure).
            checked_at: Timestamp the health check was performed;
                defaults to the current UTC time.

        Returns:
            Integration: The updated, refreshed ORM instance.
        """
        integration.status = status
        integration.last_health_check_at = checked_at or datetime.now(timezone.utc)
        await self.session.flush()
        await self.session.refresh(integration)
        return integration

    async def update_last_sync(
        self, integration: Integration, *, synced_at: Optional[datetime] = None
    ) -> Integration:
        """Records a successful data sync/exchange for an integration.

        Args:
            integration: The ORM instance to mutate.
            synced_at: Timestamp of the sync; defaults to the current
                UTC time.

        Returns:
            Integration: The updated, refreshed ORM instance.
        """
        integration.last_sync_at = synced_at or datetime.now(timezone.utc)
        await self.session.flush()
        await self.session.refresh(integration)
        return integration

    async def update_last_health_check(
        self, integration: Integration, *, checked_at: Optional[datetime] = None
    ) -> Integration:
        """Records a health-check attempt timestamp without changing status.

        Args:
            integration: The ORM instance to mutate.
            checked_at: Timestamp of the check; defaults to the current
                UTC time.

        Returns:
            Integration: The updated, refreshed ORM instance.
        """
        integration.last_health_check_at = checked_at or datetime.now(timezone.utc)
        await self.session.flush()
        await self.session.refresh(integration)
        return integration

    async def clear_default_for_type(
        self, integration_type: IntegrationType, *, exclude_id: Optional[uuid.UUID] = None
    ) -> int:
        """Clears ``is_default`` on every other integration of a given type.

        Used to enforce a single default integration per
        `integration_type` when a new one is being promoted to default.

        Args:
            integration_type: The integration type to scope the clear to.
            exclude_id: Optional id to exclude from the clear (typically
                the integration being newly promoted to default).

        Returns:
            int: The number of rows affected.
        """
        conditions = [
            Integration.integration_type == integration_type,
            Integration.is_default.is_(True),
            Integration.is_deleted.is_(False),
        ]
        if exclude_id is not None:
            conditions.append(Integration.id != exclude_id)

        stmt = select(Integration).where(and_(*conditions))
        result = await self.session.execute(stmt)
        entries = list(result.scalars().all())
        for entry in entries:
            entry.is_default = False
        await self.session.flush()
        return len(entries)

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------
    async def bulk_enable(self, ids: Sequence[uuid.UUID]) -> int:
        """Sets status to ``ACTIVE`` for a bounded set of integrations.

        Args:
            ids: The primary keys of the integrations to enable.

        Returns:
            int: The number of rows affected.
        """
        return await self._bulk_set_status(ids, IntegrationStatus.ACTIVE)

    async def bulk_disable(self, ids: Sequence[uuid.UUID]) -> int:
        """Sets status to ``INACTIVE`` for a bounded set of integrations.

        Args:
            ids: The primary keys of the integrations to disable.

        Returns:
            int: The number of rows affected.
        """
        return await self._bulk_set_status(ids, IntegrationStatus.INACTIVE)

    async def _bulk_set_status(
        self, ids: Sequence[uuid.UUID], status: IntegrationStatus
    ) -> int:
        """Applies the same status to a bounded set of non-deleted integrations.

        Args:
            ids: The primary keys of the integrations to update.
            status: The status to apply to every matched row.

        Returns:
            int: The number of rows affected.
        """
        if not ids:
            return 0
        stmt = select(Integration).where(
            Integration.id.in_(ids), Integration.is_deleted.is_(False)
        )
        result = await self.session.execute(stmt)
        entries = list(result.scalars().all())
        for entry in entries:
            entry.status = status
        await self.session.flush()
        return len(entries)

    async def bulk_delete(self, ids: Sequence[uuid.UUID]) -> int:
        """Soft-deletes a specific set of integrations by id.

        Args:
            ids: The primary keys of the integrations to soft-delete.

        Returns:
            int: The number of rows affected.
        """
        if not ids:
            return 0
        stmt = select(Integration).where(
            Integration.id.in_(ids), Integration.is_deleted.is_(False)
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
        integration_type: Optional[IntegrationType] = None,
        provider: Optional[IntegrationProvider] = None,
        status: Optional[IntegrationStatus] = None,
        authentication_type: Optional[AuthenticationType] = None,
        is_default: Optional[bool] = None,
        search: Optional[str] = None,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
        include_deleted: bool = False,
    ) -> Select:
        """Applies the supplied filter predicates to a base select statement.

        Args:
            stmt: The base SQLAlchemy select statement to constrain.
            integration_type: Restrict to this integration type.
            provider: Restrict to this provider.
            status: Restrict to this status.
            authentication_type: Restrict to this authentication type.
            is_default: Restrict to default (or non-default) integrations.
            search: Case-insensitive substring match against ``name``.
            created_from: Inclusive lower bound on ``created_at``.
            created_to: Inclusive upper bound on ``created_at``.
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            Select: The statement with all applicable predicates applied.
        """
        conditions = []

        if not include_deleted:
            conditions.append(Integration.is_deleted.is_(False))
        if integration_type is not None:
            conditions.append(Integration.integration_type == integration_type)
        if provider is not None:
            conditions.append(Integration.provider == provider)
        if status is not None:
            conditions.append(Integration.status == status)
        if authentication_type is not None:
            conditions.append(Integration.authentication_type == authentication_type)
        if is_default is not None:
            conditions.append(Integration.is_default.is_(is_default))
        if search:
            term = f"%{search}%"
            conditions.append(
                or_(
                    Integration.name.ilike(term),
                    Integration.base_url.ilike(term),
                )
            )
        if created_from is not None:
            conditions.append(Integration.created_at >= created_from)
        if created_to is not None:
            conditions.append(Integration.created_at <= created_to)

        if conditions:
            stmt = stmt.where(and_(*conditions))
        return stmt

    # ------------------------------------------------------------------
    # Listing / searching
    # ------------------------------------------------------------------
    async def list_integrations(
        self,
        *,
        integration_type: Optional[IntegrationType] = None,
        provider: Optional[IntegrationProvider] = None,
        status: Optional[IntegrationStatus] = None,
        authentication_type: Optional[AuthenticationType] = None,
        is_default: Optional[bool] = None,
        search: Optional[str] = None,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
        include_deleted: bool = False,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[Integration], int]:
        """Retrieves a filtered, sorted, paginated page of integrations.

        Args:
            integration_type: Optional integration type filter.
            provider: Optional provider filter.
            status: Optional status filter.
            authentication_type: Optional authentication type filter.
            is_default: Optional default-flag filter.
            search: Optional free-text search on name/base_url.
            created_from: Optional inclusive lower bound on ``created_at``.
            created_to: Optional inclusive upper bound on ``created_at``.
            include_deleted: Whether soft-deleted rows should be considered.
            page: 1-indexed page number.
            page_size: Number of rows per page.
            sort_by: Column name to order by.
            sort_order: ``"asc"`` or ``"desc"``.

        Returns:
            tuple[list[Integration], int]: The page of matching
            integrations and the total count matching the filters
            (ignoring pagination).
        """
        filter_kwargs: dict[str, Any] = dict(
            integration_type=integration_type,
            provider=provider,
            status=status,
            authentication_type=authentication_type,
            is_default=is_default,
            search=search,
            created_from=created_from,
            created_to=created_to,
            include_deleted=include_deleted,
        )

        base_stmt = self._apply_filters(select(Integration), **filter_kwargs)
        count_stmt = self._apply_filters(
            select(func.count()).select_from(Integration), **filter_kwargs
        )

        sort_column = getattr(Integration, sort_by, Integration.created_at)
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

    async def search_integrations(
        self, term: str, *, limit: int = 20, include_deleted: bool = False
    ) -> list[Integration]:
        """Performs a lightweight free-text search over integration names.

        Args:
            term: The free-text search term.
            limit: Maximum number of rows to return.
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            list[Integration]: The matching integrations, most recently
            created first.
        """
        stmt = select(Integration).where(
            or_(
                Integration.name.ilike(f"%{term}%"),
                Integration.base_url.ilike(f"%{term}%"),
            )
        )
        if not include_deleted:
            stmt = stmt.where(Integration.is_deleted.is_(False))
        stmt = stmt.order_by(Integration.created_at.desc()).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_default_for_type(
        self, integration_type: IntegrationType
    ) -> Optional[Integration]:
        """Fetches the default, non-deleted integration for a given type.

        Args:
            integration_type: The integration type to look up.

        Returns:
            Optional[Integration]: The default integration for this
            type, or ``None`` if none is configured.
        """
        stmt = select(Integration).where(
            Integration.integration_type == integration_type,
            Integration.is_default.is_(True),
            Integration.is_deleted.is_(False),
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Aggregation / statistics
    # ------------------------------------------------------------------
    async def get_total_count(self, *, include_deleted: bool = False) -> int:
        """Returns the total number of integrations.

        Args:
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            int: The total row count.
        """
        stmt = select(func.count()).select_from(Integration)
        if not include_deleted:
            stmt = stmt.where(Integration.is_deleted.is_(False))
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def count_by_provider(
        self, *, include_deleted: bool = False
    ) -> dict[str, int]:
        """Counts integrations grouped by provider.

        Args:
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            dict[str, int]: Mapping of provider value to row count.
        """
        stmt = select(Integration.provider, func.count()).group_by(
            Integration.provider
        )
        if not include_deleted:
            stmt = stmt.where(Integration.is_deleted.is_(False))
        rows = (await self.session.execute(stmt)).all()
        return {provider.value: count for provider, count in rows}

    async def count_by_status(
        self, *, include_deleted: bool = False
    ) -> dict[str, int]:
        """Counts integrations grouped by status.

        Args:
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            dict[str, int]: Mapping of status value to row count.
        """
        stmt = select(Integration.status, func.count()).group_by(Integration.status)
        if not include_deleted:
            stmt = stmt.where(Integration.is_deleted.is_(False))
        rows = (await self.session.execute(stmt)).all()
        return {status.value: count for status, count in rows}

    async def count_by_type(
        self, *, include_deleted: bool = False
    ) -> dict[str, int]:
        """Counts integrations grouped by integration type.

        Args:
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            dict[str, int]: Mapping of integration type value to row count.
        """
        stmt = select(Integration.integration_type, func.count()).group_by(
            Integration.integration_type
        )
        if not include_deleted:
            stmt = stmt.where(Integration.is_deleted.is_(False))
        rows = (await self.session.execute(stmt)).all()
        return {integration_type.value: count for integration_type, count in rows}

    async def count_by_authentication_type(
        self, *, include_deleted: bool = False
    ) -> dict[str, int]:
        """Counts integrations grouped by authentication type.

        Args:
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            dict[str, int]: Mapping of authentication type value to row count.
        """
        stmt = select(
            Integration.authentication_type, func.count()
        ).group_by(Integration.authentication_type)
        if not include_deleted:
            stmt = stmt.where(Integration.is_deleted.is_(False))
        rows = (await self.session.execute(stmt)).all()
        return {auth_type.value: count for auth_type, count in rows}

    async def get_statistics(self) -> dict[str, Any]:
        """Computes aggregate statistics over all non-deleted integrations.

        Returns:
            dict[str, Any]: Raw aggregate values keyed by
            ``total_integrations``, ``by_type``, ``by_provider``,
            ``by_status``, ``by_authentication_type``, ``active_count``,
            ``failed_count``, ``default_count``, ``last_sync_at``, and
            ``last_health_check_at``, ready to be assembled by the
            service layer into an ``IntegrationStatisticsResponse``.
        """
        base_condition = Integration.is_deleted.is_(False)

        total = await self.get_total_count()
        by_type = await self.count_by_type()
        by_provider = await self.count_by_provider()
        by_status = await self.count_by_status()
        by_authentication_type = await self.count_by_authentication_type()

        active_count = by_status.get(IntegrationStatus.ACTIVE.value, 0)
        failed_count = by_status.get(IntegrationStatus.FAILED.value, 0)

        default_count_stmt = (
            select(func.count())
            .select_from(Integration)
            .where(base_condition, Integration.is_default.is_(True))
        )
        default_count = (
            await self.session.execute(default_count_stmt)
        ).scalar_one()

        last_sync_stmt = select(func.max(Integration.last_sync_at)).where(
            base_condition
        )
        last_sync_at = (await self.session.execute(last_sync_stmt)).scalar_one()

        last_health_check_stmt = select(
            func.max(Integration.last_health_check_at)
        ).where(base_condition)
        last_health_check_at = (
            await self.session.execute(last_health_check_stmt)
        ).scalar_one()

        return {
            "total_integrations": total,
            "by_type": by_type,
            "by_provider": by_provider,
            "by_status": by_status,
            "by_authentication_type": by_authentication_type,
            "active_count": active_count,
            "failed_count": failed_count,
            "default_count": default_count,
            "last_sync_at": last_sync_at,
            "last_health_check_at": last_health_check_at,
        }