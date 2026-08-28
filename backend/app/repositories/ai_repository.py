# app/repositories/ai_repository.py
"""
Repository layer for AI usage logging.
Contains database operations only. No business logic.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_usage import AIFeature, AIUsage, AIUsageStatus


class AIUsageRepository:
    """Handles all persistence operations for AIUsage entities.

    All parameters that correspond to enum-backed columns on `AIUsage`
    (`feature`, `status`) are accepted here as their actual enum types,
    not raw strings, so this layer stays a thin, unambiguous mirror of
    the ORM model. String <-> enum translation for caller convenience
    belongs in the service layer.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(
        self,
        *,
        user_id: int,
        feature: AIFeature,
        model_name: str,
        status: AIUsageStatus,
        conversation_id: Optional[uuid.UUID] = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: Optional[int] = None,
        cost_usd: float = 0,
        latency_ms: Optional[int] = None,
        error_message: Optional[str] = None,
        request_metadata: Optional[dict] = None,
    ) -> AIUsage:
        """Insert a single AI usage record.

        Args:
            user_id: User who triggered the AI invocation.
            feature: The AI feature this record belongs to.
            model_name: Name of the underlying model that was invoked.
            status: Outcome status of the invocation.
            conversation_id: Optional related conversation identifier.
            prompt_tokens: Number of prompt/input tokens consumed.
            completion_tokens: Number of completion/output tokens consumed.
            total_tokens: Total tokens consumed; computed from
                prompt_tokens + completion_tokens when omitted.
            cost_usd: Estimated cost of the invocation in USD.
            latency_ms: Round-trip latency of the invocation, if measured.
            error_message: Error detail when status is FAILURE.
            request_metadata: Free-form JSON context for this record.

        Returns:
            The persisted AIUsage row.
        """
        entity = AIUsage(
            id=uuid.uuid4(),
            user_id=user_id,
            feature=feature,
            model_name=model_name,
            conversation_id=conversation_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=(
                total_tokens if total_tokens is not None else prompt_tokens + completion_tokens
            ),
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            status=status,
            error_message=error_message,
            request_metadata=request_metadata,
            created_at=datetime.utcnow(),
        )
        self._db.add(entity)
        await self._db.flush()
        await self._db.refresh(entity)
        return entity

    async def get_by_id(self, log_id: uuid.UUID) -> Optional[AIUsage]:
        """Fetch a single AIUsage row by its primary key.

        Args:
            log_id: Primary key of the record to fetch.

        Returns:
            The matching AIUsage row, or None if not found.
        """
        result = await self._db.execute(select(AIUsage).where(AIUsage.id == log_id))
        return result.scalar_one_or_none()

    async def list_paginated(
        self,
        *,
        page: int,
        page_size: int,
        user_id: Optional[uuid.UUID] = None,
        feature: Optional[AIFeature] = None,
        status: Optional[AIUsageStatus] = None,
        search: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> tuple[Sequence[AIUsage], int]:
        """List AIUsage rows with filtering and pagination.

        Args:
            page: 1-indexed page number.
            page_size: Maximum number of rows per page.
            user_id: Optional filter to a specific user.
            feature: Optional filter to a specific AI feature.
            status: Optional filter to a specific outcome status.
            search: Optional case-insensitive substring match against
                `model_name`.
            date_from: Optional inclusive lower bound on created_at.
            date_to: Optional inclusive upper bound on created_at.

        Returns:
            A tuple of (rows for the requested page, total matching rows).
        """
        conditions = []
        if user_id is not None:
            conditions.append(AIUsage.user_id == user_id)
        if feature is not None:
            conditions.append(AIUsage.feature == feature)
        if status is not None:
            conditions.append(AIUsage.status == status)
        if date_from is not None:
            conditions.append(AIUsage.created_at >= date_from)
        if date_to is not None:
            conditions.append(AIUsage.created_at <= date_to)
        if search:
            conditions.append(AIUsage.model_name.ilike(f"%{search}%"))

        base_query = select(AIUsage)
        count_query = select(func.count()).select_from(AIUsage)
        if conditions:
            base_query = base_query.where(and_(*conditions))
            count_query = count_query.where(and_(*conditions))

        total_result = await self._db.execute(count_query)
        total = total_result.scalar_one()

        result = await self._db.execute(
            base_query.order_by(desc(AIUsage.created_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = result.scalars().all()
        return items, total

    async def get_usage_summary(
        self,
        *,
        user_id: Optional[uuid.UUID] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> dict:
        """Compute aggregated usage totals over matching AIUsage rows.

        Args:
            user_id: Optional filter to a specific user.
            date_from: Optional inclusive lower bound on created_at.
            date_to: Optional inclusive upper bound on created_at.

        Returns:
            A dict with total_requests, successful_requests,
            failed_requests, total_prompt_tokens, total_completion_tokens,
            total_tokens, and total_cost.
        """
        conditions = []
        if user_id is not None:
            conditions.append(AIUsage.user_id == user_id)
        if date_from is not None:
            conditions.append(AIUsage.created_at >= date_from)
        if date_to is not None:
            conditions.append(AIUsage.created_at <= date_to)

        totals_query = select(
            func.count().label("total_requests"),
            func.coalesce(func.sum(AIUsage.prompt_tokens), 0).label("total_prompt_tokens"),
            func.coalesce(func.sum(AIUsage.completion_tokens), 0).label(
                "total_completion_tokens"
            ),
            func.coalesce(func.sum(AIUsage.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(AIUsage.cost_usd), 0).label("total_cost"),
        )
        failed_query = select(func.count()).select_from(AIUsage).where(
            AIUsage.status == AIUsageStatus.FAILURE
        )
        if conditions:
            totals_query = totals_query.where(and_(*conditions))
            failed_query = failed_query.where(and_(*conditions))

        totals = (await self._db.execute(totals_query)).one()
        failed_requests = (await self._db.execute(failed_query)).scalar_one()
        total_requests = totals.total_requests or 0

        return {
            "total_requests": total_requests,
            "successful_requests": total_requests - failed_requests,
            "failed_requests": failed_requests,
            "total_prompt_tokens": int(totals.total_prompt_tokens or 0),
            "total_completion_tokens": int(totals.total_completion_tokens or 0),
            "total_tokens": int(totals.total_tokens or 0),
            "total_cost": float(totals.total_cost or 0.0),
        }