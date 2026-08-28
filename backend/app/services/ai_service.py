# app/services/ai_service.py
"""Service layer for AI usage tracking/logging and provider invocation.

Provides:
- AIProviderClient: a thin async wrapper around the underlying AI
  provider invocation, used by ChatService, RAGService, SQLAIService,
  and AnalyticsAIService.
- AIUsageService: records individual AI invocations (chat, rag, sql,
  analytics) for auditing/billing purposes and exposes query/aggregation
  endpoints used by the admin usage-log API.

AIUsageService accepts caller-friendly strings (module/status) at its
public boundary and translates them to the AIFeature/AIUsageStatus enums
that `AIUsage` (via AIUsageRepository) actually stores, so callers don't
need to import the model's enum types directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationException
from app.models.ai_usage import AIFeature, AIUsage, AIUsageStatus
from app.repositories.ai_repository import AIUsageRepository
from app.schemas.ai import AIUsageLogResponse, TokenUsage
from app.schemas.common import PaginatedResponse

DEFAULT_MODEL_NAME = "unknown"


class AIProviderClient:
    """Thin async wrapper around the underlying AI provider invocation.

    Centralizes how the various AI services issue provider calls so that
    provider-facing behaviour lives in one place instead of being
    duplicated across each service. The underlying SDK client is created
    lazily on first use, and only imported at call time, so importing
    this module (or constructing AIProviderClient) never fails just
    because the provider SDK isn't installed or configured.

    Attributes:
        client: The underlying provider SDK client instance, if supplied
            explicitly; otherwise created lazily on first `complete` call.
    """

    def __init__(self, client: Optional[Any] = None) -> None:
        """Initialize the provider client wrapper.

        Args:
            client: Optional pre-constructed provider SDK client. If not
                supplied, a client is created lazily the first time
                `complete` is called.
        """
        self.client = client

    def _get_client(self) -> Any:
        """Return the underlying provider client, creating it if needed.

        Returns:
            The underlying provider SDK client instance.
        """
        if self.client is None:
            from anthropic import AsyncAnthropic  # local import: optional dependency

            self.client = AsyncAnthropic()
        return self.client

    async def complete(
        self,
        messages: list,
        *,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1000,
        model: str = "claude-sonnet-4-6",
    ) -> dict:
        """Invoke the underlying AI provider and return a normalized result.

        Args:
            messages: Sequence of chat-message-like objects/dicts, each
                exposing `role` and `content` (either attribute or key
                access is supported).
            system_prompt: Optional system prompt to steer the model.
            temperature: Sampling temperature for the completion.
            max_tokens: Maximum number of tokens to generate.
            model: Name of the underlying model to invoke.

        Returns:
            A dict with keys: content, prompt_tokens, completion_tokens,
            model.
        """
        client = self._get_client()

        def _as_message(m: Any) -> dict:
            role = getattr(m, "role", None) if not isinstance(m, dict) else m.get("role")
            content = (
                getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
            )
            return {"role": role, "content": content}

        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [_as_message(m) for m in messages],
        }
        if system_prompt:
            kwargs["system"] = system_prompt

        response = await client.messages.create(**kwargs)

        content = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        usage = getattr(response, "usage", None)
        return {
            "content": content,
            "prompt_tokens": getattr(usage, "input_tokens", 0) if usage else 0,
            "completion_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
            "model": getattr(response, "model", model),
        }


class AIUsageService:
    """Records and reports on AI invocation usage across all AI modules.

    Attributes:
        db: Active async database session used for all queries in this
            service instance.
    """

    def __init__(self, db: AsyncSession) -> None:
        """Initialize the service with a database session.

        Args:
            db: Active async SQLAlchemy session.
        """
        self.db = db
        self._repository = AIUsageRepository(db)

    @staticmethod
    def _to_feature(module: str) -> AIFeature:
        """Translate a caller-supplied module string into an AIFeature.

        Args:
            module: Caller-supplied module name (e.g. "chat", "analytics").

        Returns:
            The corresponding AIFeature enum member.

        Raises:
            ValidationException: If module is blank or not a recognized
                AIFeature value.
        """
        normalized = (module or "").strip().lower()
        if not normalized:
            raise ValidationException("module must not be blank")
        try:
            return AIFeature(normalized)
        except ValueError as exc:
            valid = ", ".join(f.value for f in AIFeature)
            raise ValidationException(
                f"Unknown module '{module}'. Expected one of: {valid}"
            ) from exc

    @staticmethod
    def _to_status(status: str) -> AIUsageStatus:
        """Translate a caller-supplied status string into an AIUsageStatus.

        Args:
            status: Caller-supplied status (e.g. "success", "failure").

        Returns:
            The corresponding AIUsageStatus enum member.

        Raises:
            ValidationException: If status is blank or not a recognized
                AIUsageStatus value.
        """
        normalized = (status or "").strip().lower()
        if not normalized:
            raise ValidationException("status must not be blank")
        try:
            return AIUsageStatus(normalized)
        except ValueError as exc:
            valid = ", ".join(s.value for s in AIUsageStatus)
            raise ValidationException(
                f"Unknown status '{status}'. Expected one of: {valid}"
            ) from exc

    async def record_usage(
        self,
        *,
        user_id: int,
        module: str,
        status: str,
        action: Optional[str] = None,
        usage: Optional[TokenUsage] = None,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        model_used: Optional[str] = None,
        cost: Optional[float] = None,
        latency_ms: Optional[int] = None,
        conversation_id: Optional[uuid.UUID] = None,
        error_message: Optional[str] = None,
        request_metadata: Optional[dict] = None,
    ) -> AIUsageLogResponse:
        """Persist a single AI usage log entry.

        Intended to be called by the other AI services (ChatService,
        RAGService, SQLAIService, AnalyticsAIService) immediately after
        each provider invocation, so every AI call is auditable.

        Args:
            user_id: Identifier of the user who triggered the invocation.
            module: AI module the invocation belongs to. Must match one
                of the AIFeature values (chat, rag, sql_generation,
                analytics, embedding).
            status: Outcome status of the invocation. Must match one of
                the AIUsageStatus values (success, failure).
            action: Optional name of the action/endpoint invoked; stored
                inside `request_metadata` since it has no dedicated column.
            usage: Token usage accounting for the invocation. If given,
                takes precedence over `prompt_tokens`/`completion_tokens`.
            prompt_tokens: Prompt/input token count, used when `usage` is
                not supplied.
            completion_tokens: Completion/output token count, used when
                `usage` is not supplied.
            model_used: Name of the underlying model that was invoked.
            cost: Estimated monetary cost of the invocation, in USD.
            latency_ms: Round-trip latency of the invocation, if measured.
            conversation_id: Optional related conversation identifier.
            error_message: Error detail if the invocation failed.
            request_metadata: Additional free-form JSON context. Merged
                with `action` (if provided) under the "action" key.

        Returns:
            The persisted usage log entry.

        Raises:
            ValidationException: If module or status is blank or unknown.
        """
        feature = self._to_feature(module)
        status_enum = self._to_status(status)

        if usage is not None:
            resolved_prompt_tokens = usage.prompt_tokens
            resolved_completion_tokens = usage.completion_tokens
        else:
            resolved_prompt_tokens = prompt_tokens or 0
            resolved_completion_tokens = completion_tokens or 0

        metadata = dict(request_metadata) if request_metadata else {}
        if action:
            metadata["action"] = action

        entry = await self._repository.create(
            user_id=user_id,
            feature=feature,
            model_name=model_used or DEFAULT_MODEL_NAME,
            status=status_enum,
            conversation_id=conversation_id,
            prompt_tokens=resolved_prompt_tokens,
            completion_tokens=resolved_completion_tokens,
            cost_usd=cost or 0,
            latency_ms=latency_ms,
            error_message=error_message,
            request_metadata=metadata or None,
        )
        await self.db.commit()
        await self.db.refresh(entry)
        return self._to_response(entry)

    async def log_usage(
        self,
        *,
        user_id: int,
        module: str,
        status: str,
        action: Optional[str] = None,
        usage: Optional[TokenUsage] = None,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        model_used: Optional[str] = None,
        cost: Optional[float] = None,
        latency_ms: Optional[int] = None,
        conversation_id: Optional[uuid.UUID] = None,
        error_message: Optional[str] = None,
        request_metadata: Optional[dict] = None,
    ) -> AIUsageLogResponse:
        """Record a single AI usage log entry.

        Thin alias over `record_usage`, kept as a separate method because
        callers such as `AnalyticsAIService` invoke usage logging via
        `log_usage(...)`. Delegates entirely to `record_usage` so there is
        a single source of truth for how usage entries are persisted.

        Args: same as `record_usage`.

        Returns:
            The persisted usage log entry.

        Raises:
            ValidationException: If module or status is blank or unknown.
        """
        return await self.record_usage(
            user_id=user_id,
            module=module,
            status=status,
            action=action,
            usage=usage,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model_used=model_used,
            cost=cost,
            latency_ms=latency_ms,
            conversation_id=conversation_id,
            error_message=error_message,
            request_metadata=request_metadata,
        )

    async def list_usage_logs(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        user_id: Optional[uuid.UUID] = None,
        module: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> PaginatedResponse[AIUsageLogResponse]:
        """List AI usage log entries with filtering and pagination.

        Args:
            page: 1-indexed page number.
            page_size: Maximum number of entries per page.
            user_id: Optional filter to a specific user.
            module: Optional filter to a specific AI module.
            status: Optional filter to a specific outcome status.
            search: Optional case-insensitive substring match on the
                underlying model name.
            date_from: Optional inclusive lower bound on created_at.
            date_to: Optional inclusive upper bound on created_at.

        Returns:
            A paginated collection of usage log entries, most recent first.
        """
        feature = self._to_feature(module) if module is not None else None
        status_enum = self._to_status(status) if status is not None else None

        rows, total = await self._repository.list_paginated(
            page=page,
            page_size=page_size,
            user_id=user_id,
            feature=feature,
            status=status_enum,
            search=search,
            date_from=date_from,
            date_to=date_to,
        )

        import math

        return PaginatedResponse[AIUsageLogResponse](
            items=[self._to_response(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=math.ceil(total / page_size) if total else 0,
        )

    async def get_usage_summary(
        self,
        *,
        user_id: Optional[uuid.UUID] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> dict:
        """Compute aggregated usage totals (tokens, cost, request counts).

        Args:
            user_id: Optional filter to a specific user.
            date_from: Optional inclusive lower bound on created_at.
            date_to: Optional inclusive upper bound on created_at.

        Returns:
            A dict with total_requests, successful_requests, failed_requests,
            total_prompt_tokens, total_completion_tokens, total_tokens, and
            total_cost.
        """
        return await self._repository.get_usage_summary(
            user_id=user_id, date_from=date_from, date_to=date_to
        )

    @staticmethod
    def _to_response(entry: AIUsage) -> AIUsageLogResponse:
        """Convert an AIUsage ORM row into its response schema.

        Args:
            entry: The persisted AIUsage row.

        Returns:
            The corresponding AIUsageLogResponse.
        """
        usage = None
        if entry.prompt_tokens is not None and entry.completion_tokens is not None:
            usage = TokenUsage(
                prompt_tokens=entry.prompt_tokens,
                completion_tokens=entry.completion_tokens,
                total_tokens=(
                    entry.total_tokens
                    if entry.total_tokens is not None
                    else entry.prompt_tokens + entry.completion_tokens
                ),
            )
        action = (entry.request_metadata or {}).get("action") if entry.request_metadata else None
        return AIUsageLogResponse(
            id=entry.id,
            user_id=entry.user_id,
            module=entry.feature.value,
            action=action,
            status=entry.status.value,
            usage=usage,
            cost=float(entry.cost_usd) if entry.cost_usd is not None else None,
            error_message=entry.error_message,
            created_at=entry.created_at,
        )