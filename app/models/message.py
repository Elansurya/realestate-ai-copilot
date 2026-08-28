"""SQLAlchemy model for individual AI conversation messages.

This module defines the Message model, representing a single turn (from a
user, the assistant, the system, or a tool) within an AI Conversation.
"""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.conversation import Conversation


class MessageRole(str, enum.Enum):
    """Enumeration of the originating role of a conversation message."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class Message(Base):
    """Represents a single message exchanged within an AI conversation.

    Attributes:
        id: Primary key UUID identifier.
        conversation_id: Foreign key referencing the parent conversation.
        role: Role of the message author (user, assistant, system, tool).
        content: Full textual content of the message.
        tokens_used: Number of tokens consumed generating/storing this message.
        model_used: Name of the underlying AI model that generated this
            message (assistant messages only; null for user/system/tool
            messages).
        meta_data: Arbitrary JSONB metadata (tool calls, citations, latency).
        is_deleted: Soft delete flag.
        deleted_at: Timestamp of soft deletion, if any.
        created_at: Timestamp of record creation.
        updated_at: Timestamp of last record update.
        conversation: The parent Conversation record.
    """

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[MessageRole] = mapped_column(
        SAEnum(
            MessageRole,
            name="message_role",
            native_enum=False,
            length=20,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            validate_strings=True,
        ),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    model_used: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    meta_data: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, nullable=True)

    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    conversation: Mapped["Conversation"] = relationship(
        "Conversation", back_populates="messages", lazy="joined"
    )

    def __repr__(self) -> str:
        """Return a debug-friendly representation of the message."""
        return f"<Message id={self.id} role={self.role} conversation_id={self.conversation_id}>"