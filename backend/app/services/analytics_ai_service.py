# app/services/analytics_ai_service.py
"""
Service layer for the AI-powered analytics insights endpoint.
Contains business logic only.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationException
from app.schemas.ai import (
    AnalyticsQueryRequest,
    AnalyticsQueryResponse,
    ChatMessageInput,
    TokenUsage,
)
from app.services.ai_service import AIProviderClient, AIUsageService

ANALYTICS_SYSTEM_PROMPT = (
    "You are a data analyst for an enterprise real estate CRM. Given a "
    "dataset (as JSON) and a business question, produce a clear, concise "
    "analysis with concrete numbers drawn only from the supplied dataset. "
    "Highlight trends, anomalies, and actionable recommendations. Never "
    "fabricate figures that are not derivable from the dataset provided."
)
MAX_DATASET_ROWS = 500
MAX_DATASET_CHARS = 40000
ANALYTICS_MODULE = "analytics"
ANALYTICS_ACTION = "analyze"


class AnalyticsAIService:
    """Business logic for generating natural-language analytics insights."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._provider = AIProviderClient()
        self._usage = AIUsageService(db)

    async def analyze(
        self, user_id: int, payload: AnalyticsQueryRequest
    ) -> AnalyticsQueryResponse:
        """Run an AI-generated analysis over a caller-supplied dataset.

        Args:
            user_id: Identifier of the user requesting the analysis.
            payload: The analytics request, containing the dataset and
                the business question to answer against it.

        Returns:
            The generated analytics insight along with row count metadata.

        Raises:
            ValidationException: If the question or dataset is missing,
                or the dataset exceeds size limits.
        """
        if not payload.question or not payload.question.strip():
            raise ValidationException("A question is required for analytics AI queries.")
        if not payload.dataset:
            raise ValidationException("A non-empty dataset is required for analysis.")
        if len(payload.dataset) > MAX_DATASET_ROWS:
            raise ValidationException(
                f"Dataset exceeds the maximum of {MAX_DATASET_ROWS} rows per request."
            )

        serialized_dataset = json.dumps(payload.dataset, default=str)
        if len(serialized_dataset) > MAX_DATASET_CHARS:
            raise ValidationException(
                "Dataset payload is too large. Please aggregate or reduce it before analysis."
            )

        user_prompt = (
            f"Dataset (JSON array of records):\n{serialized_dataset}\n\n"
            f"Business question: {payload.question}"
        )

        try:
            completion = await self._provider.complete(
                [ChatMessageInput(role="user", content=user_prompt)],
                system_prompt=ANALYTICS_SYSTEM_PROMPT,
                temperature=0.2,
                max_tokens=2500,
            )
        except Exception as exc:  # noqa: BLE001
            await self._usage.log_usage(
                user_id=user_id,
                module=ANALYTICS_MODULE,
                action=ANALYTICS_ACTION,
                status="failure",
                error_message=str(exc),
                request_metadata={"dataset_rows": len(payload.dataset)},
            )
            raise

        usage = TokenUsage(
            prompt_tokens=completion["prompt_tokens"],
            completion_tokens=completion["completion_tokens"],
            total_tokens=completion["prompt_tokens"] + completion["completion_tokens"],
        )

        await self._usage.log_usage(
            user_id=user_id,
            module=ANALYTICS_MODULE,
            action=ANALYTICS_ACTION,
            status="success",
            usage=usage,
            model_used=completion["model"],
            conversation_id=payload.conversation_id,
            request_metadata={"dataset_rows": len(payload.dataset)},
        )

        return AnalyticsQueryResponse(
            conversation_id=payload.conversation_id,
            insights=completion["content"],
            usage=usage,
        )

    async def get_platform_usage_summary(
        self, user_id: Optional[uuid.UUID] = None
    ) -> dict:
        """Return aggregated AI usage totals, optionally scoped to a user.

        Args:
            user_id: Optional user to scope the summary to. When omitted,
                the summary covers all users.

        Returns:
            A dict with total_requests, successful_requests,
            failed_requests, total_prompt_tokens, total_completion_tokens,
            total_tokens, and total_cost.
        """
        return await self._usage.get_usage_summary(user_id=user_id)