"""
Repository layer for AI conversations and messages.
Contains database operations only. No business logic.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import select, func, and_, desc, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.conversation import Conversation
from app.models.message import Message
from app.schemas.ai import ConversationCreate, ConversationUpdate, MessageCreate


class ConversationRepository:
    """Handles all persistence operations for Conversation entities."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(self, user_id: int, payload: ConversationCreate) -> Conversation:
        entity = Conversation(
            id=uuid.uuid4(),
            user_id=user_id,
            title=payload.title,
            module=payload.module,
            meta_data=payload.metadata,
            is_archived=False,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        self._db.add(entity)
        await self._db.flush()
        await self._db.refresh(entity)
        return entity

    async def get_by_id(
        self, conversation_id: uuid.UUID, with_messages: bool = False
    ) -> Optional[Conversation]:
        query = select(Conversation).where(Conversation.id == conversation_id)
        if with_messages:
            query = query.options(selectinload(Conversation.messages))
        result = await self._db.execute(query)
        return result.scalar_one_or_none()

    async def list_paginated(
        self,
        *,
        user_id: int,
        page: int,
        page_size: int,
        module: Optional[str] = None,
        is_archived: Optional[bool] = None,
        search: Optional[str] = None,
    ) -> tuple[Sequence[Conversation], int]:
        conditions = [Conversation.user_id == user_id]
        if module is not None:
            conditions.append(Conversation.module == module)
        if is_archived is not None:
            conditions.append(Conversation.is_archived == is_archived)
        if search:
            conditions.append(Conversation.title.ilike(f"%{search}%"))

        count_query = select(func.count()).select_from(Conversation).where(and_(*conditions))
        total_result = await self._db.execute(count_query)
        total = total_result.scalar_one()

        query = (
            select(Conversation)
            .where(and_(*conditions))
            .order_by(desc(Conversation.updated_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await self._db.execute(query)
        items = result.scalars().all()
        return items, total

    async def update(
        self, entity: Conversation, payload: ConversationUpdate
    ) -> Conversation:
        update_data = payload.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(entity, field, value)
        entity.updated_at = datetime.utcnow()
        await self._db.flush()
        await self._db.refresh(entity)
        return entity

    async def touch(self, entity: Conversation) -> None:
        entity.updated_at = datetime.utcnow()
        await self._db.flush()

    async def delete(self, entity: Conversation) -> None:
        await self._db.delete(entity)
        await self._db.flush()


class MessageRepository:
    """Handles all persistence operations for Message entities."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(
        self, conversation_id: uuid.UUID, payload: MessageCreate
    ) -> Message:
        entity = Message(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            role=payload.role,
            content=payload.content,
            tokens_used=payload.tokens_used or 0,
            model_used=payload.model_used,
            created_at=datetime.utcnow(),
        )
        self._db.add(entity)
        await self._db.flush()
        await self._db.refresh(entity)
        return entity

    async def get_by_id(self, message_id: uuid.UUID) -> Optional[Message]:
        result = await self._db.execute(
            select(Message).where(Message.id == message_id)
        )
        return result.scalar_one_or_none()

    async def list_by_conversation(
        self,
        *,
        conversation_id: uuid.UUID,
        page: int,
        page_size: int,
        search: Optional[str] = None,
    ) -> tuple[Sequence[Message], int]:
        conditions = [Message.conversation_id == conversation_id]
        if search:
            conditions.append(Message.content.ilike(f"%{search}%"))

        count_query = select(func.count()).select_from(Message).where(and_(*conditions))
        total_result = await self._db.execute(count_query)
        total = total_result.scalar_one()

        query = (
            select(Message)
            .where(and_(*conditions))
            .order_by(Message.created_at.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await self._db.execute(query)
        items = result.scalars().all()
        return items, total

    async def get_recent_history(
        self, conversation_id: uuid.UUID, limit: int = 20
    ) -> Sequence[Message]:
        query = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(desc(Message.created_at))
            .limit(limit)
        )
        result = await self._db.execute(query)
        items = result.scalars().all()
        return list(reversed(items))

    async def count_by_conversation(self, conversation_id: uuid.UUID) -> int:
        result = await self._db.execute(
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation_id)
        )
        return result.scalar_one()