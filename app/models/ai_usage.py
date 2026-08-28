# app/models/ai_usage.py
"""SQLAlchemy model for AI feature usage and cost tracking."""

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AIFeature(str, enum.Enum):
    """Enumeration of distinct AI-powered features that consume tokens."""

    CHAT = "chat"
    RAG = "rag"
    SQL_GENERATION = "sql_generation"
    ANALYTICS = "analytics"
    EMBEDDING = "embedding"


class AIUsageStatus(str, enum.Enum):
    """Enumeration of the outcome status of an AI API call."""

    SUCCESS = "success"
    FAILURE = "failure"


class AIUsage(Base):
    """Represents a single billable interaction with an underlying AI provider.

    Attributes:
        id: Primary key UUID identifier.
        user_id: Foreign key referencing the CRM user who triggered the call.
        conversation_id: Optional foreign key referencing the related conversation.
        feature: The AI feature that generated this usage record.
        model_name: Name of the underlying AI model invoked.
        prompt_tokens: Number of tokens in the prompt/input.
        completion_tokens: Number of tokens in the completion/output.
        total_tokens: Sum of prompt and completion tokens.
        cost_usd: Estimated cost of the call in US dollars.
        latency_ms: Round-trip latency of the call in milliseconds.
        status: Outcome status of the call.
        error_message: Error detail captured when status is FAILURE.
        request_metadata: Free-form JSON blob for caller-supplied context
            (e.g. the specific action/endpoint name, dataset size, etc.)
            that doesn't warrant its own column.
        created_at: Timestamp of record creation.
        updated_at: Timestamp of last record update.
    """

    __tablename__ = "ai_usages"
    __table_args__ = (
        Index("ix_ai_usages_user_id_created_at", "user_id", "created_at"),
        Index("ix_ai_usages_feature", "feature"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    feature: Mapped[AIFeature] = mapped_column(
        SAEnum(AIFeature, name="ai_feature", native_enum=True), nullable=False
    )
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    total_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cost_usd: Mapped[Numeric] = mapped_column(
        Numeric(12, 6), nullable=False, default=0, server_default="0"
    )
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[AIUsageStatus] = mapped_column(
        SAEnum(AIUsageStatus, name="ai_usage_status", native_enum=True),
        nullable=False,
        default=AIUsageStatus.SUCCESS,
        server_default=AIUsageStatus.SUCCESS.name,
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    request_metadata: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        """Return a debug-friendly representation of the AI usage record."""
        return f"<AIUsage id={self.id} feature={self.feature} total_tokens={self.total_tokens}>"