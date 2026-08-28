"""
Service layer for AI conversation and chat message management.
Contains business logic only.
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthorizationException, NotFoundException
from app.repositories.conversation_repository import (
    ConversationRepository,
    MessageRepository,
)
from app.schemas.ai import (
    ChatMessageInput,
    ConversationCreate,
    ConversationDetailResponse,
    ConversationResponse,
    ConversationUpdate,
    MessageCreate,
    MessageResponse,
    ChatRequest,
    ChatResponse,
    TokenUsage,
)
from app.schemas.common import PaginatedResponse
from app.services.ai_service import AIProviderClient, AIUsageService

DEFAULT_SYSTEM_PROMPT = (
    "You are the AI Copilot for an enterprise real estate CRM. "
    "Answer questions helpfully, concisely, and accurately based on the "
    "conversation context. If you are unsure, say so rather than guessing."
)
MAX_HISTORY_MESSAGES = 20


class ChatService:
    """Business logic for managing conversations and generating chat replies."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._conversations = ConversationRepository(db)
        self._messages = MessageRepository(db)
        self._provider = AIProviderClient()
        self._usage = AIUsageService(db)

    async def create_conversation(
        self, user_id: int, payload: ConversationCreate
    ) -> ConversationResponse:
        entity = await self._conversations.create(user_id, payload)
        await self._db.commit()
        return ConversationResponse.model_validate(entity)

    async def get_conversation(
        self, user_id: int, conversation_id: uuid.UUID
    ) -> ConversationDetailResponse:
        entity = await self._conversations.get_by_id(conversation_id, with_messages=True)
        entity = self._authorize_owner(entity, user_id, conversation_id)
        return ConversationDetailResponse.model_validate(entity)

    async def list_conversations(
        self,
        *,
        user_id: int,
        page: int,
        page_size: int,
        module: Optional[str] = None,
        is_archived: Optional[bool] = None,
        search: Optional[str] = None,
    ) -> PaginatedResponse[ConversationResponse]:
        items, total = await self._conversations.list_paginated(
            user_id=user_id,
            page=page,
            page_size=page_size,
            module=module,
            is_archived=is_archived,
            search=search,
        )
        return PaginatedResponse[ConversationResponse](
            items=[ConversationResponse.model_validate(item) for item in items],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=(total + page_size - 1) // page_size if page_size else 0,
        )

    async def update_conversation(
        self,
        user_id: int,
        conversation_id: uuid.UUID,
        payload: ConversationUpdate,
    ) -> ConversationResponse:
        entity = await self._conversations.get_by_id(conversation_id)
        entity = self._authorize_owner(entity, user_id, conversation_id)
        updated = await self._conversations.update(entity, payload)
        await self._db.commit()
        return ConversationResponse.model_validate(updated)

    async def delete_conversation(
        self, user_id: int, conversation_id: uuid.UUID
    ) -> None:
        entity = await self._conversations.get_by_id(conversation_id)
        entity = self._authorize_owner(entity, user_id, conversation_id)
        await self._conversations.delete(entity)
        await self._db.commit()

    async def list_messages(
        self,
        *,
        user_id: int,
        conversation_id: uuid.UUID,
        page: int,
        page_size: int,
        search: Optional[str] = None,
    ) -> PaginatedResponse[MessageResponse]:
        conversation = await self._conversations.get_by_id(conversation_id)
        self._authorize_owner(conversation, user_id, conversation_id)

        items, total = await self._messages.list_by_conversation(
            conversation_id=conversation_id,
            page=page,
            page_size=page_size,
            search=search,
        )
        return PaginatedResponse[MessageResponse](
            items=[MessageResponse.model_validate(item) for item in items],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=(total + page_size - 1) // page_size if page_size else 0,
        )

    async def send_message(
        self,
        user_id: int,
        conversation_id: uuid.UUID,
        payload: ChatRequest,
    ) -> ChatResponse:
        conversation = await self._conversations.get_by_id(conversation_id)
        conversation = self._authorize_owner(conversation, user_id, conversation_id)

        user_message = await self._messages.create(
            conversation_id,
            MessageCreate(role="user", content=payload.message),
        )

        history = await self._messages.get_recent_history(
            conversation_id, limit=MAX_HISTORY_MESSAGES
        )
        chat_history = [
            ChatMessageInput(role=m.role, content=m.content) for m in history
        ]

        try:
            completion = await self._provider.complete(
                chat_history,
                system_prompt=payload.system_prompt or DEFAULT_SYSTEM_PROMPT,
                temperature=payload.temperature or 0.3,
            )
            status = "success"
            error_message = None
        except Exception as exc:  # noqa: BLE001
            status = "failure"
            error_message = str(exc)
            await self._usage.log_usage(
                user_id=user_id,
                module="chat",
                action="send_message",
                conversation_id=conversation_id,
                status=status,
                error_message=error_message,
            )
            raise

        assistant_message = await self._messages.create(
            conversation_id,
            MessageCreate(
                role="assistant",
                content=completion["content"],
                tokens_used=completion["completion_tokens"],
                model_used=completion["model"],
            ),
        )
        await self._conversations.touch(conversation)
        await self._db.commit()

        await self._usage.log_usage(
            user_id=user_id,
            module="chat",
            action="send_message",
            conversation_id=conversation_id,
            prompt_tokens=completion["prompt_tokens"],
            completion_tokens=completion["completion_tokens"],
            model_used=completion["model"],
            status=status,
        )

        usage = TokenUsage(
            prompt_tokens=completion["prompt_tokens"],
            completion_tokens=completion["completion_tokens"],
            total_tokens=completion["prompt_tokens"] + completion["completion_tokens"],
        )

        return ChatResponse(
            conversation_id=conversation_id,
            user_message=MessageResponse.model_validate(user_message),
            assistant_message=MessageResponse.model_validate(assistant_message),
            usage=usage,
        )

    @staticmethod
    def _authorize_owner(entity, user_id: int, conversation_id: uuid.UUID):
        if entity is None:
            raise NotFoundException(f"Conversation {conversation_id} was not found.")
        if entity.user_id != user_id:
            raise AuthorizationException(
                "You do not have access to this conversation."
            )
        return entity