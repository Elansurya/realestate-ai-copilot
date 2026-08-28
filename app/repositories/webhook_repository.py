"""
backend/app/repositories/webhook_repository.py

Repository layer for the Enterprise Webhook module of the Enterprise
Real Estate AI Copilot CRM.

Follows the project's Repository Pattern conventions:
    - Constructed with an injected `AsyncSession` (no session
      creation/ownership here -- that belongs to the request-scoped
      dependency).
    - Every mutating method calls `session.flush()` (never `commit()`)
      so the calling Service layer retains control of the transaction
      boundary (commit/rollback), matching the project's Unit of Work
      convention.
    - Read methods use SQLAlchemy 2.x `select()` construct patterns,
      never raw string SQL.
    - This layer does NOT raise domain/business exceptions -- it
      returns `None` / empty collections for "not found" and lets the
      Service layer decide what that means. The one exception is that
      integrity/database errors are allowed to propagate as-is;
      translating those into domain exceptions is also a Service
      concern.
    - Soft-deleted `Webhook` rows are excluded from all read/list
      methods by default via an `include_deleted` flag, mirroring the
      `Webhook.is_deleted` soft-delete convention documented on the
      model.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.webhook import (
    DeliveryStatus,
    Webhook,
    WebhookEvent,
    WebhookLog,
    WebhookStatus,
)
from app.schemas.webhook import WebhookFilter, WebhookLogFilter

__all__ = ["WebhookRepository"]


class WebhookRepository:
    """Data-access layer for `Webhook` and `WebhookLog` records.

    Attributes:
        session: The request-scoped async SQLAlchemy session used for
            all reads/writes performed by this repository instance.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Initializes the repository with an injected async session.

        Args:
            session: The active `AsyncSession` for this unit of work.
        """
        self.session = session

    # ------------------------------------------------------------------
    # Create / Update / Soft-Delete / Restore
    # ------------------------------------------------------------------
    async def create(self, data: dict[str, Any]) -> Webhook:
        """Persists a new `Webhook` record.

        Args:
            data: Column values for the new webhook, typically sourced
                from a validated `WebhookCreate` schema (`.model_dump()`).

        Returns:
            Webhook: The newly created, flushed (not committed) webhook,
            with its server-generated `id` populated.
        """
        webhook = Webhook(**data)
        self.session.add(webhook)
        await self.session.flush()
        await self.session.refresh(webhook)
        return webhook

    async def update(self, webhook: Webhook, data: dict[str, Any]) -> Webhook:
        """Applies a partial set of column updates to an existing webhook.

        Args:
            webhook: The already-loaded `Webhook` instance to mutate.
            data: Mapping of column name -> new value. Only keys
                present are applied (PATCH semantics); callers are
                expected to have already filtered out unset fields
                (e.g. via `WebhookUpdate.model_dump(exclude_unset=True)`).

        Returns:
            Webhook: The updated, flushed webhook instance.
        """
        for field, value in data.items():
            setattr(webhook, field, value)
        await self.session.flush()
        await self.session.refresh(webhook)
        return webhook

    async def soft_delete(self, webhook: Webhook, *, deleted_at: Optional[datetime] = None) -> Webhook:
        """Marks a webhook as soft-deleted.

        Args:
            webhook: The webhook to soft-delete.
            deleted_at: Explicit deletion timestamp. Defaults to
                `datetime.now(timezone.utc)` when omitted.

        Returns:
            Webhook: The soft-deleted webhook instance.
        """
        from datetime import timezone

        webhook.is_deleted = True
        webhook.deleted_at = deleted_at or datetime.now(timezone.utc)
        webhook.enabled = False
        await self.session.flush()
        await self.session.refresh(webhook)
        return webhook

    async def restore(self, webhook: Webhook) -> Webhook:
        """Reverses a soft-delete, restoring a webhook to active use.

        Args:
            webhook: The soft-deleted webhook to restore.

        Returns:
            Webhook: The restored webhook instance (still respects
            whatever `enabled`/`status` the Service layer sets
            separately; this only clears the soft-delete markers).
        """
        webhook.is_deleted = False
        webhook.deleted_at = None
        await self.session.flush()
        await self.session.refresh(webhook)
        return webhook

    # ------------------------------------------------------------------
    # Enable / Disable
    # ------------------------------------------------------------------
    async def enable(self, webhook: Webhook) -> Webhook:
        """Sets the quick on/off `enabled` toggle to `True`.

        Args:
            webhook: The webhook to enable.

        Returns:
            Webhook: The updated webhook instance.
        """
        webhook.enabled = True
        await self.session.flush()
        await self.session.refresh(webhook)
        return webhook

    async def disable(self, webhook: Webhook) -> Webhook:
        """Sets the quick on/off `enabled` toggle to `False`.

        Args:
            webhook: The webhook to disable.

        Returns:
            Webhook: The updated webhook instance.
        """
        webhook.enabled = False
        await self.session.flush()
        await self.session.refresh(webhook)
        return webhook

    async def bulk_enable(self, webhook_ids: list[uuid.UUID]) -> int:
        """Enables a set of webhooks in a single statement.

        Args:
            webhook_ids: Identifiers of the webhooks to enable.

        Returns:
            int: The number of rows updated.
        """
        if not webhook_ids:
            return 0
        stmt = (
            update(Webhook)
            .where(Webhook.id.in_(webhook_ids), Webhook.is_deleted.is_(False))
            .values(enabled=True)
            .execution_options(synchronize_session="fetch")
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount or 0

    async def bulk_disable(self, webhook_ids: list[uuid.UUID]) -> int:
        """Disables a set of webhooks in a single statement.

        Args:
            webhook_ids: Identifiers of the webhooks to disable.

        Returns:
            int: The number of rows updated.
        """
        if not webhook_ids:
            return 0
        stmt = (
            update(Webhook)
            .where(Webhook.id.in_(webhook_ids), Webhook.is_deleted.is_(False))
            .values(enabled=False)
            .execution_options(synchronize_session="fetch")
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount or 0

    # ------------------------------------------------------------------
    # Single-record retrieval
    # ------------------------------------------------------------------
    async def get_by_id(
        self, webhook_id: uuid.UUID, *, include_deleted: bool = False
    ) -> Optional[Webhook]:
        """Fetches a single webhook by its primary key.

        Args:
            webhook_id: The webhook's UUID primary key.
            include_deleted: When `False` (default), soft-deleted
                webhooks are excluded and `None` is returned for them.

        Returns:
            Optional[Webhook]: The matching webhook, or `None`.
        """
        stmt = select(Webhook).where(Webhook.id == webhook_id)
        if not include_deleted:
            stmt = stmt.where(Webhook.is_deleted.is_(False))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_name(
        self, name: str, *, include_deleted: bool = False
    ) -> Optional[Webhook]:
        """Fetches a single webhook by its unique `name`.

        Args:
            name: The exact webhook name.
            include_deleted: When `False` (default), soft-deleted
                webhooks are excluded.

        Returns:
            Optional[Webhook]: The matching webhook, or `None`.
        """
        stmt = select(Webhook).where(Webhook.name == name)
        if not include_deleted:
            stmt = stmt.where(Webhook.is_deleted.is_(False))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Listing / Filtering / Searching / Pagination / Sorting
    # ------------------------------------------------------------------
    def _apply_filters(self, stmt: Select, filter_: WebhookFilter) -> Select:
        """Applies `WebhookFilter` predicates to a base `Webhook` select.

        Args:
            stmt: The base select statement to constrain.
            filter_: Filter criteria supplied by the caller.

        Returns:
            Select: The constrained select statement.
        """
        conditions = []

        if filter_.is_deleted is not None:
            conditions.append(Webhook.is_deleted.is_(filter_.is_deleted))
        if filter_.event is not None:
            conditions.append(Webhook.event == filter_.event)
        if filter_.status is not None:
            conditions.append(Webhook.status == filter_.status)
        if filter_.authentication_type is not None:
            conditions.append(Webhook.authentication_type == filter_.authentication_type)
        if filter_.enabled is not None:
            conditions.append(Webhook.enabled.is_(filter_.enabled))
        if filter_.search:
            like_term = f"%{filter_.search.strip()}%"
            conditions.append(
                or_(Webhook.name.ilike(like_term), Webhook.target_url.ilike(like_term))
            )
        if filter_.created_from is not None:
            conditions.append(Webhook.created_at >= filter_.created_from)
        if filter_.created_to is not None:
            conditions.append(Webhook.created_at <= filter_.created_to)

        if conditions:
            stmt = stmt.where(and_(*conditions))
        return stmt

    @staticmethod
    def _apply_sort(stmt: Select, model: type, sort_by: str, sort_order: str) -> Select:
        """Applies an `ORDER BY` clause to a select statement.

        Args:
            stmt: The select statement to order.
            model: The ORM model whose column is used for sorting
                (`Webhook` or `WebhookLog`).
            sort_by: The already-allow-listed column name to sort by.
            sort_order: Either `"asc"` or `"desc"`.

        Returns:
            Select: The ordered select statement.
        """
        column = getattr(model, sort_by)
        return stmt.order_by(column.desc() if sort_order == "desc" else column.asc())

    async def list_webhooks(
        self, filter_: WebhookFilter
    ) -> tuple[list[Webhook], int]:
        """Lists webhooks matching the given filter, paginated and sorted.

        This method also serves the module's "Search Webhooks"
        requirement via `WebhookFilter.search`, which matches against
        `name` and `target_url`.

        Args:
            filter_: Combined filter, pagination, and sort criteria.

        Returns:
            tuple[list[Webhook], int]: The page of matching webhooks
            (with `created_by` eagerly loaded) and the total count of
            matching rows across all pages.
        """
        base_stmt = select(Webhook)
        base_stmt = self._apply_filters(base_stmt, filter_)

        count_stmt = select(func.count()).select_from(base_stmt.subquery())
        total = (await self.session.execute(count_stmt)).scalar_one()

        page_stmt = self._apply_sort(base_stmt, Webhook, filter_.sort_by, filter_.sort_order)
        page_stmt = page_stmt.options(selectinload(Webhook.created_by))
        page_stmt = page_stmt.offset((filter_.page - 1) * filter_.page_size).limit(
            filter_.page_size
        )

        result = await self.session.execute(page_stmt)
        items = list(result.scalars().all())
        return items, total

    # ------------------------------------------------------------------
    # Aggregate Statistics
    # ------------------------------------------------------------------
    async def count_by_status(self, *, include_deleted: bool = False) -> dict[str, int]:
        """Counts webhooks grouped by `status`.

        Args:
            include_deleted: When `False` (default), soft-deleted
                webhooks are excluded from the counts.

        Returns:
            dict[str, int]: Mapping of `WebhookStatus` value -> count.
        """
        stmt = select(Webhook.status, func.count(Webhook.id)).group_by(Webhook.status)
        if not include_deleted:
            stmt = stmt.where(Webhook.is_deleted.is_(False))
        result = await self.session.execute(stmt)
        return {status.value: count for status, count in result.all()}

    async def count_by_event(self, *, include_deleted: bool = False) -> dict[str, int]:
        """Counts webhooks grouped by `event`.

        Args:
            include_deleted: When `False` (default), soft-deleted
                webhooks are excluded from the counts.

        Returns:
            dict[str, int]: Mapping of `WebhookEvent` value -> count.
        """
        stmt = select(Webhook.event, func.count(Webhook.id)).group_by(Webhook.event)
        if not include_deleted:
            stmt = stmt.where(Webhook.is_deleted.is_(False))
        result = await self.session.execute(stmt)
        return {event.value: count for event, count in result.all()}

    async def get_statistics(
        self, *, webhook_id: Optional[uuid.UUID] = None
    ) -> dict[str, Any]:
        """Computes aggregate webhook + delivery statistics.

        Args:
            webhook_id: When supplied, statistics are scoped to a
                single webhook's delivery logs; otherwise statistics
                are computed across all (non-deleted) webhooks.

        Returns:
            dict[str, Any]: Raw aggregate values suitable for building
            a `WebhookStatisticsResponse` in the Service layer:
            `total_webhooks`, `active_count`, `suspended_count`,
            `failed_count`, `by_event`, `by_status`,
            `total_deliveries`, `successful_deliveries`,
            `failed_deliveries`, `dead_lettered_deliveries`,
            `success_rate_percentage`, `average_duration_ms`,
            `last_delivery_at`.
        """
        webhook_stmt = select(
            func.count(Webhook.id),
            func.count(Webhook.id).filter(Webhook.status == WebhookStatus.ACTIVE),
            func.count(Webhook.id).filter(Webhook.status == WebhookStatus.SUSPENDED),
            func.count(Webhook.id).filter(Webhook.status == WebhookStatus.FAILED),
        ).where(Webhook.is_deleted.is_(False))
        if webhook_id is not None:
            webhook_stmt = webhook_stmt.where(Webhook.id == webhook_id)

        total_webhooks, active_count, suspended_count, failed_count = (
            await self.session.execute(webhook_stmt)
        ).one()

        by_event = await self.count_by_event()
        by_status = await self.count_by_status()

        log_stmt = select(
            func.count(WebhookLog.id),
            func.count(WebhookLog.id).filter(
                WebhookLog.delivery_status == DeliveryStatus.SUCCESS
            ),
            func.count(WebhookLog.id).filter(
                WebhookLog.delivery_status == DeliveryStatus.FAILED
            ),
            func.count(WebhookLog.id).filter(
                WebhookLog.delivery_status == DeliveryStatus.DEAD_LETTERED
            ),
            func.avg(WebhookLog.duration_ms),
            func.max(WebhookLog.delivered_at),
        )
        if webhook_id is not None:
            log_stmt = log_stmt.where(WebhookLog.webhook_id == webhook_id)

        (
            total_deliveries,
            successful_deliveries,
            failed_deliveries,
            dead_lettered_deliveries,
            average_duration_ms,
            last_delivery_at,
        ) = (await self.session.execute(log_stmt)).one()

        success_rate_percentage = (
            round((successful_deliveries / total_deliveries) * 100, 2)
            if total_deliveries
            else None
        )

        return {
            "total_webhooks": total_webhooks,
            "active_count": active_count,
            "suspended_count": suspended_count,
            "failed_count": failed_count,
            "by_event": by_event,
            "by_status": by_status,
            "total_deliveries": total_deliveries,
            "successful_deliveries": successful_deliveries,
            "failed_deliveries": failed_deliveries,
            "dead_lettered_deliveries": dead_lettered_deliveries,
            "success_rate_percentage": success_rate_percentage,
            "average_duration_ms": (
                float(average_duration_ms) if average_duration_ms is not None else None
            ),
            "last_delivery_at": last_delivery_at,
        }

    # ------------------------------------------------------------------
    # Delivery Logs
    # ------------------------------------------------------------------
    async def create_log(self, data: dict[str, Any]) -> WebhookLog:
        """Persists a new `WebhookLog` delivery-attempt record.

        Args:
            data: Column values for the new log row (e.g. `webhook_id`,
                `delivery_status`, `attempt_count`, `response_code`,
                `response_body`, `duration_ms`, `error_message`).

        Returns:
            WebhookLog: The newly created, flushed log entry.
        """
        log = WebhookLog(**data)
        self.session.add(log)
        await self.session.flush()
        await self.session.refresh(log)
        return log

    async def get_log_by_id(self, log_id: uuid.UUID) -> Optional[WebhookLog]:
        """Fetches a single delivery log entry by its primary key.

        Args:
            log_id: The `WebhookLog` UUID primary key.

        Returns:
            Optional[WebhookLog]: The matching log entry, or `None`.
        """
        stmt = select(WebhookLog).where(WebhookLog.id == log_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_latest_log(self, webhook_id: uuid.UUID) -> Optional[WebhookLog]:
        """Fetches the most recent delivery log entry for a webhook.

        Args:
            webhook_id: The parent webhook's UUID.

        Returns:
            Optional[WebhookLog]: The most recent log entry by
            `delivered_at`, or `None` if no deliveries have been logged.
        """
        stmt = (
            select(WebhookLog)
            .where(WebhookLog.webhook_id == webhook_id)
            .order_by(WebhookLog.delivered_at.desc(), WebhookLog.attempt_count.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_delivery_logs(
        self, filter_: WebhookLogFilter
    ) -> tuple[list[WebhookLog], int]:
        """Lists delivery log entries matching the given filter.

        Args:
            filter_: Combined filter, pagination, and sort criteria.

        Returns:
            tuple[list[WebhookLog], int]: The page of matching log
            entries and the total count of matching rows across all
            pages.
        """
        base_stmt = select(WebhookLog)
        conditions = []
        if filter_.webhook_id is not None:
            conditions.append(WebhookLog.webhook_id == filter_.webhook_id)
        if filter_.delivery_status is not None:
            conditions.append(WebhookLog.delivery_status == filter_.delivery_status)
        if filter_.delivered_from is not None:
            conditions.append(WebhookLog.delivered_at >= filter_.delivered_from)
        if filter_.delivered_to is not None:
            conditions.append(WebhookLog.delivered_at <= filter_.delivered_to)
        if conditions:
            base_stmt = base_stmt.where(and_(*conditions))

        count_stmt = select(func.count()).select_from(base_stmt.subquery())
        total = (await self.session.execute(count_stmt)).scalar_one()

        page_stmt = self._apply_sort(base_stmt, WebhookLog, filter_.sort_by, filter_.sort_order)
        page_stmt = page_stmt.offset((filter_.page - 1) * filter_.page_size).limit(
            filter_.page_size
        )

        result = await self.session.execute(page_stmt)
        items = list(result.scalars().all())
        return items, total

    async def get_failed_log_for_retry(
        self, log_id: uuid.UUID
    ) -> Optional[WebhookLog]:
        """Fetches a delivery log entry only if it is eligible for retry.

        Args:
            log_id: The `WebhookLog` UUID primary key.

        Returns:
            Optional[WebhookLog]: The log entry if its
            `delivery_status` is `FAILED` or `RETRYING`, otherwise
            `None`.
        """
        stmt = select(WebhookLog).where(
            WebhookLog.id == log_id,
            WebhookLog.delivery_status.in_(
                [DeliveryStatus.FAILED, DeliveryStatus.RETRYING]
            ),
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Webhook delivery-state bookkeeping (used after a delivery attempt)
    # ------------------------------------------------------------------
    async def record_delivery_outcome(
        self,
        webhook: Webhook,
        *,
        succeeded: bool,
        occurred_at: datetime,
    ) -> Webhook:
        """Updates a webhook's rollup delivery-state timestamps.

        Args:
            webhook: The webhook a delivery attempt was just made for.
            succeeded: Whether the delivery attempt succeeded.
            occurred_at: The timestamp the delivery attempt completed at.

        Returns:
            Webhook: The updated webhook instance.
        """
        webhook.last_delivery_at = occurred_at
        if succeeded:
            webhook.last_success_at = occurred_at
        else:
            webhook.last_failure_at = occurred_at
        await self.session.flush()
        await self.session.refresh(webhook)
        return webhook