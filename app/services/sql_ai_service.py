"""
Service layer for the natural-language-to-SQL AI endpoint.
Contains business logic only.
"""
from __future__ import annotations

import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ExternalServiceException, ValidationException
from app.schemas.ai import ChatMessageInput, SQLQueryRequest, SQLQueryResponse, TokenUsage
from app.services.ai_service import AIProviderClient, AIUsageService

SQL_SYSTEM_PROMPT = (
    "You are a PostgreSQL expert generating READ-ONLY analytical queries for "
    "an enterprise real estate CRM. You must only ever produce a single SELECT "
    "statement. Never generate INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, "
    "GRANT, or any statement that mutates data or schema. Do not use multiple "
    "statements separated by semicolons. Always add a LIMIT clause capped at "
    "{max_rows} rows if the user does not specify one. Respond with ONLY the "
    "raw SQL query and nothing else \u2014 no explanations, no markdown fences."
)

FORBIDDEN_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "truncate",
    "grant",
    "revoke",
    "create",
    "attach",
    "copy",
    "call",
    "execute",
    "merge",
    "--",
    ";",
)
MAX_ROWS = 500
SQL_AI_MODULE = "sql_generation"
SQL_AI_ACTION = "query"


class SQLAIService:
    """Business logic for translating natural language into safe, read-only SQL."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._provider = AIProviderClient()
        self._usage = AIUsageService(db)

    async def query(
        self, user_id: int, payload: SQLQueryRequest
    ) -> SQLQueryResponse:
        if not payload.question or not payload.question.strip():
            raise ValidationException("A question is required for SQL AI queries.")

        system_prompt = SQL_SYSTEM_PROMPT.format(max_rows=MAX_ROWS)
        user_prompt = f"Question: {payload.question}"

        try:
            completion = await self._provider.complete(
                [ChatMessageInput(role="user", content=user_prompt)],
                system_prompt=system_prompt,
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001
            await self._usage.log_usage(
                user_id=user_id,
                module=SQL_AI_MODULE,
                action=SQL_AI_ACTION,
                status="failure",
                conversation_id=payload.conversation_id,
                error_message=str(exc),
            )
            raise

        generated_sql = self._sanitize_sql(completion["content"])
        generated_sql = self._validate_sql(generated_sql)

        usage = TokenUsage(
            prompt_tokens=completion["prompt_tokens"],
            completion_tokens=completion["completion_tokens"],
            total_tokens=completion["prompt_tokens"] + completion["completion_tokens"],
        )

        await self._usage.log_usage(
            user_id=user_id,
            module=SQL_AI_MODULE,
            action=SQL_AI_ACTION,
            status="success",
            usage=usage,
            model_used=completion["model"],
            conversation_id=payload.conversation_id,
        )

        return SQLQueryResponse(
            conversation_id=payload.conversation_id,
            sql=generated_sql,
            explanation=None,
            usage=usage,
        )

    @staticmethod
    def _sanitize_sql(raw_content: str) -> str:
        cleaned = raw_content.strip()
        cleaned = re.sub(r"^```(sql)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
        cleaned = cleaned.rstrip(";").strip()
        if not cleaned:
            raise ExternalServiceException("The AI provider returned an empty SQL query.")
        return cleaned

    @staticmethod
    def _validate_sql(sql: str) -> str:
        normalized = sql.lower()

        if not normalized.lstrip().startswith(("select", "with")):
            raise ValidationException(
                "Only SELECT/WITH statements are permitted for AI-generated SQL queries."
            )

        for keyword in FORBIDDEN_KEYWORDS:
            pattern = rf"\b{re.escape(keyword)}\b" if keyword.isalpha() else re.escape(keyword)
            if re.search(pattern, normalized):
                raise ValidationException(
                    f"Generated SQL contains a forbidden operation: '{keyword}'."
                )

        if "limit" not in normalized:
            return f"{sql} LIMIT {MAX_ROWS}"
        return sql