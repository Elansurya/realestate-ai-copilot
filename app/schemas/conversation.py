"""Pydantic schemas for AI conversations and their constituent messages."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.conversation import ConversationStatus
from app.models.message import MessageRole
from app.schemas.ai import TokenUsage


class MessageCreate(BaseModel):
    """Payload for creating a new message within a conversation.

    Attributes:
        role: Role of the message author.
        content: Textual content of the message.
        tokens_used: Number of tokens consumed by this message.
        meta_data: Optional arbitrary metadata payload.
    """

    model_config = ConfigDict(from_attributes=True)

    role: MessageRole
    content: str = Field(min_length=1, max_length=32768)
    tokens_used: int = Field(default=0, ge=0)
    meta_data: Optional[dict] = None

    @field_validator("content")
    @classmethod
    def validate_content_not_blank(cls, value: str) -> str:
        """Ensure message content is not empty after whitespace stripping.

        Args:
            value: Raw message content.

        Returns:
            The stripped message content.

        Raises:
            ValueError: If content is blank after stripping.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("content must not be blank")
        return stripped


class MessageRead(BaseModel):
    """Representation of a persisted message returned to API consumers.

    Attributes:
        id: Unique identifier of the message.
        conversation_id: Identifier of the parent conversation.
        role: Role of the message author.
        content: Textual content of the message.
        tokens_used: Number of tokens consumed by this message.
        meta_data: Arbitrary metadata payload associated with the message.
        created_at: Timestamp of message creation.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    role: MessageRole
    content: str
    tokens_used: int
    meta_data: Optional[dict] = None
    created_at: datetime


class ConversationCreate(BaseModel):
    """Payload for creating a new AI conversation.

    Attributes:
        title: Human readable title for the conversation.
        model_name: Name of the AI model backing the conversation.
        context: Optional initial context payload.
    """

    model_config = ConfigDict(from_attributes=True)

    title: str = Field(default="New Conversation", min_length=1, max_length=255)
    model_name: str = Field(default="gpt-4o", min_length=1, max_length=100)
    context: Optional[dict] = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        """Ensure the conversation title is not blank after stripping.

        Args:
            value: Raw title supplied by the caller.

        Returns:
            The stripped conversation title.

        Raises:
            ValueError: If the title is blank after stripping.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped


class ConversationUpdate(BaseModel):
    """Payload for partially updating an existing conversation.

    Attributes:
        title: Updated human readable title, if provided.
        status: Updated lifecycle status, if provided.
        context: Updated context payload, if provided.
    """

    model_config = ConfigDict(from_attributes=True)

    title: Optional[str] = Field(default=None, min_length=1, max_length=255)
    status: Optional[ConversationStatus] = None
    context: Optional[dict] = None


class ConversationRead(BaseModel):
    """Representation of a persisted conversation returned to API consumers.

    Attributes:
        id: Unique identifier of the conversation.
        user_id: Identifier of the owning CRM user.
        title: Human readable title of the conversation.
        status: Current lifecycle status of the conversation.
        model_name: Name of the AI model backing the conversation.
        total_tokens: Cumulative token usage across all messages.
        context: Arbitrary context payload associated with the conversation.
        created_at: Timestamp of conversation creation.
        updated_at: Timestamp of last conversation update.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    title: str
    status: ConversationStatus
    model_name: str
    total_tokens: int
    context: Optional[dict] = None
    created_at: datetime
    updated_at: datetime


class ConversationWithMessages(ConversationRead):
    """Conversation representation enriched with its full message history.

    Attributes:
        messages: Ordered list of messages belonging to the conversation.
        usage: Aggregate token usage summary for the conversation.
    """

    messages: list[MessageRead] = Field(default_factory=list)
    usage: Optional[TokenUsage] = None