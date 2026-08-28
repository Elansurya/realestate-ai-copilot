"""
Service layer for AI prompt template management.
Contains business logic only.
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictException, NotFoundException, ValidationException
from app.repositories.prompt_repository import PromptRepository
from app.schemas.ai import (
    PromptCreate,
    PromptRenderRequest,
    PromptRenderResponse,
    PromptResponse,
    PromptUpdate,
)
from app.schemas.common import PaginatedResponse

_VARIABLE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


class PromptService:
    """Business logic for creating, listing, updating, and rendering AI prompts."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._repository = PromptRepository(db)

    async def create_prompt(
        self, created_by: Optional[int], payload: PromptCreate
    ) -> PromptResponse:
        existing = await self._repository.get_by_name(payload.name)
        if existing is not None:
            raise ConflictException(
                f"A prompt with the name '{payload.name}' already exists."
            )
        self._validate_template(payload.template_text, payload.variables or [])
        entity = await self._repository.create(created_by, payload)
        await self._db.commit()
        return PromptResponse.model_validate(entity)

    async def get_prompt(self, prompt_id: uuid.UUID) -> PromptResponse:
        entity = await self._repository.get_by_id(prompt_id)
        if entity is None:
            raise NotFoundException(f"Prompt {prompt_id} was not found.")
        return PromptResponse.model_validate(entity)

    async def list_prompts(
        self,
        *,
        page: int,
        page_size: int,
        category: Optional[str] = None,
        is_active: Optional[bool] = None,
        search: Optional[str] = None,
    ) -> PaginatedResponse[PromptResponse]:
        items, total = await self._repository.list_paginated(
            page=page,
            page_size=page_size,
            category=category,
            is_active=is_active,
            search=search,
        )
        return PaginatedResponse[PromptResponse](
            items=[PromptResponse.model_validate(item) for item in items],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=(total + page_size - 1) // page_size if page_size else 0,
        )

    async def update_prompt(
        self, prompt_id: uuid.UUID, payload: PromptUpdate
    ) -> PromptResponse:
        entity = await self._repository.get_by_id(prompt_id)
        if entity is None:
            raise NotFoundException(f"Prompt {prompt_id} was not found.")

        if payload.name and payload.name != entity.name:
            existing = await self._repository.get_by_name(payload.name)
            if existing is not None:
                raise ConflictException(
                    f"A prompt with the name '{payload.name}' already exists."
                )

        template = payload.template_text or entity.template_text
        variables = payload.variables if payload.variables is not None else entity.variables
        self._validate_template(template, variables or [])

        updated = await self._repository.update(entity, payload)
        await self._db.commit()
        return PromptResponse.model_validate(updated)

    async def delete_prompt(self, prompt_id: uuid.UUID) -> None:
        entity = await self._repository.get_by_id(prompt_id)
        if entity is None:
            raise NotFoundException(f"Prompt {prompt_id} was not found.")
        await self._repository.delete(entity)
        await self._db.commit()

    async def render_prompt(
        self, prompt_id: uuid.UUID, payload: PromptRenderRequest
    ) -> PromptRenderResponse:
        entity = await self._repository.get_by_id(prompt_id)
        if entity is None:
            raise NotFoundException(f"Prompt {prompt_id} was not found.")
        if not entity.is_active:
            raise ValidationException("This prompt is inactive and cannot be rendered.")

        rendered = self._interpolate(entity.template_text, payload.variables)
        return PromptRenderResponse(prompt_id=entity.id, rendered_text=rendered)

    @staticmethod
    def _validate_template(template: str, declared_variables: list[str]) -> None:
        if not template or not template.strip():
            raise ValidationException("Prompt template cannot be empty.")
        used_variables = set(_VARIABLE_PATTERN.findall(template))
        missing = used_variables - set(declared_variables)
        if missing:
            raise ValidationException(
                f"Template references undeclared variables: {', '.join(sorted(missing))}"
            )

    @staticmethod
    def _interpolate(template: str, variables: dict[str, str]) -> str:
        def _replace(match: re.Match) -> str:
            key = match.group(1)
            if key not in variables:
                raise ValidationException(f"Missing value for template variable '{key}'.")
            return str(variables[key])

        return _VARIABLE_PATTERN.sub(_replace, template)