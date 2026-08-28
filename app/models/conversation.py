"""SQLAlchemy model for AI conversation sessions.

This module defines the Conversation model, which represents a persistent
chat session between a CRM user and the AI Copilot. A conversation acts as
the aggregate root for a sequence of Message records exchanged with one or
more underlying AI models.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.message import Message


class Conversation(Base):
    """Represents a chat conversation between a user and the AI Copilot.

    Attributes:
        id: Primary key UUID identifier.
        user_id: Foreign key referencing the owning CRM user.
        title: Human readable title for the conversation.
        module: Which AI module the conversation belongs to
            (chat, rag, sql, analytics).
        meta_data: Arbitrary JSONB metadata supplied at creation time
            (e.g. seed context, tags).
        is_archived: Whether the conversation has been archived.
        is_deleted: Soft delete flag.
        deleted_at: Timestamp of soft deletion, if any.
        created_at: Timestamp of record creation.
        updated_at: Timestamp of last record update.
        messages: Related Message records ordered by creation.
    """

    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_user_id_module", "user_id", "module"),
        Index("ix_conversations_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(
        String(255), nullable=False, default="New Conversation"
    )
    module: Mapped[str] = mapped_column(
        String(50), nullable=False, default="chat", server_default="chat"
    )
    meta_data: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, nullable=True)
    is_archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

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

    messages: Mapped[list["Message"]] = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        """Return a debug-friendly representation of the conversation."""
        return f"<Conversation id={self.id} module={self.module} user_id={self.user_id}>"