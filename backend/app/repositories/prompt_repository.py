"""
Repository layer for AI prompt management.
Contains database operations only. No business logic.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import select, func, and_, desc, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.prompt_template import PromptTemplate
from app.schemas.ai import PromptCreate, PromptUpdate


class PromptRepository:
    """Handles all persistence operations for PromptTemplate entities."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(self, created_by: Optional[int], payload: PromptCreate) -> PromptTemplate:
        entity = PromptTemplate(
            id=uuid.uuid4(),
            name=payload.name,
            description=payload.description,
            template_text=payload.template_text,
            category=payload.category,
            variables=payload.variables,
            is_active=payload.is_active if payload.is_active is not None else True,
            created_by=created_by,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        self._db.add(entity)
        await self._db.flush()
        await self._db.refresh(entity)
        return entity

    async def get_by_id(self, prompt_id: uuid.UUID) -> Optional[PromptTemplate]:
        result = await self._db.execute(
            select(PromptTemplate).where(PromptTemplate.id == prompt_id)
        )
        return result.scalar_one_or_none()

    async def get_by_name(self, name: str) -> Optional[PromptTemplate]:
        result = await self._db.execute(
            select(PromptTemplate).where(PromptTemplate.name == name)
        )
        return result.scalar_one_or_none()

    async def list_paginated(
        self,
        *,
        page: int,
        page_size: int,
        category: Optional[str] = None,
        is_active: Optional[bool] = None,
        search: Optional[str] = None,
    ) -> tuple[Sequence[PromptTemplate], int]:
        conditions = []
        if category is not None:
            conditions.append(PromptTemplate.category == category)
        if is_active is not None:
            conditions.append(PromptTemplate.is_active == is_active)
        if search:
            conditions.append(
                or_(
                    PromptTemplate.name.ilike(f"%{search}%"),
                    PromptTemplate.description.ilike(f"%{search}%"),
                )
            )

        count_query = select(func.count()).select_from(PromptTemplate)
        base_query = select(PromptTemplate)
        if conditions:
            count_query = count_query.where(and_(*conditions))
            base_query = base_query.where(and_(*conditions))

        total_result = await self._db.execute(count_query)
        total = total_result.scalar_one()

        result = await self._db.execute(
            base_query.order_by(desc(PromptTemplate.updated_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = result.scalars().all()
        return items, total

    async def update(self, entity: PromptTemplate, payload: PromptUpdate) -> PromptTemplate:
        update_data = payload.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(entity, field, value)
        entity.updated_at = datetime.utcnow()
        await self._db.flush()
        await self._db.refresh(entity)
        return entity

    async def delete(self, entity: PromptTemplate) -> None:
        await self._db.delete(entity)
        await self._db.flush()