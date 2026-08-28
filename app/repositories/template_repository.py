# backend/app/repositories/template_repository.py
"""Repository for notification templates.

Contains only database access operations. No business rules, validation,
or orchestration logic belongs in this module.
"""

import uuid
from datetime import datetime
from typing import Optional, Sequence, Tuple

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import NotificationChannel
from app.models.notification_template import NotificationTemplate, TemplateLocale


class TemplateRepository:
    """Data access layer for `NotificationTemplate` entities.

    Attributes:
        session: Active async SQLAlchemy session bound to the current
            unit of work.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Initialize the repository.

        Args:
            session: Async SQLAlchemy session used for all operations.
        """
        self.session = session

    async def create(self, template: NotificationTemplate) -> NotificationTemplate:
        """Persist a new notification template.

        Args:
            template: Template entity to insert.

        Returns:
            The persisted template with generated fields populated.
        """
        self.session.add(template)
        await self.session.flush()
        await self.session.refresh(template)
        return template

    async def bulk_create(
        self, templates: Sequence[NotificationTemplate]
    ) -> Sequence[NotificationTemplate]:
        """Persist multiple templates in a single operation.

        Args:
            templates: Template entities to insert.

        Returns:
            The persisted templates.
        """
        self.session.add_all(templates)
        await self.session.flush()
        return templates

    async def get_by_id(
        self, template_id: uuid.UUID
    ) -> Optional[NotificationTemplate]:
        """Fetch a single template by primary key.

        Args:
            template_id: UUID of the template.

        Returns:
            The matching template, or None if not found or soft deleted.
        """
        stmt = select(NotificationTemplate).where(
            NotificationTemplate.id == template_id,
            NotificationTemplate.is_deleted.is_(False),
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_by_code(
        self,
        code: str,
        channel: NotificationChannel,
        locale: TemplateLocale = TemplateLocale.EN_US,
    ) -> Optional[NotificationTemplate]:
        """Fetch the active, highest-version template matching the given key.

        Args:
            code: Business identifier for the template family.
            channel: Delivery channel the template must render for.
            locale: Locale of the template content.

        Returns:
            The active template with the highest version, or None if no
            active template matches.
        """
        stmt = (
            select(NotificationTemplate)
            .where(
                NotificationTemplate.code == code,
                NotificationTemplate.channel == channel,
                NotificationTemplate.locale == locale,
                NotificationTemplate.is_active.is_(True),
                NotificationTemplate.is_deleted.is_(False),
            )
            .order_by(NotificationTemplate.version.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_templates(
        self,
        channel: Optional[NotificationChannel] = None,
        locale: Optional[TemplateLocale] = None,
        is_active: Optional[bool] = None,
        search_term: Optional[str] = None,
        sort_by: str = "created_at",
        sort_desc: bool = True,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[Sequence[NotificationTemplate], int]:
        """Fetch a filtered, sorted, paginated list of templates.

        Args:
            channel: Filter by delivery channel.
            locale: Filter by locale.
            is_active: Filter by active flag.
            search_term: Free text match against code and name.
            sort_by: Column name to sort by. Must be an attribute of
                `NotificationTemplate`.
            sort_desc: Whether to sort in descending order.
            page: 1-indexed page number.
            page_size: Number of records per page.

        Returns:
            A tuple of (matching templates for the page, total matching
            count across all pages).
        """
        conditions = [NotificationTemplate.is_deleted.is_(False)]
        if channel is not None:
            conditions.append(NotificationTemplate.channel == channel)
        if locale is not None:
            conditions.append(NotificationTemplate.locale == locale)
        if is_active is not None:
            conditions.append(NotificationTemplate.is_active.is_(is_active))
        if search_term:
            like_pattern = f"%{search_term}%"
            conditions.append(
                NotificationTemplate.code.ilike(like_pattern)
                | NotificationTemplate.name.ilike(like_pattern)
            )

        count_stmt = select(func.count(NotificationTemplate.id)).where(and_(*conditions))
        total_result = await self.session.execute(count_stmt)
        total = total_result.scalar_one()

        sort_column = getattr(NotificationTemplate, sort_by, NotificationTemplate.created_at)
        order_clause = sort_column.desc() if sort_desc else sort_column.asc()

        list_stmt = (
            select(NotificationTemplate)
            .where(and_(*conditions))
            .order_by(order_clause)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        list_result = await self.session.execute(list_stmt)
        return list_result.scalars().all(), total

    async def update_fields(
        self, template_id: uuid.UUID, values: dict
    ) -> Optional[NotificationTemplate]:
        """Update arbitrary column values on a template.

        Args:
            template_id: UUID of the template to update.
            values: Mapping of column name to new value.

        Returns:
            The updated template, or None if not found.
        """
        stmt = (
            update(NotificationTemplate)
            .where(
                NotificationTemplate.id == template_id,
                NotificationTemplate.is_deleted.is_(False),
            )
            .values(**values)
            .returning(NotificationTemplate)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.scalar_one_or_none()

    async def deactivate(self, template_id: uuid.UUID) -> Optional[NotificationTemplate]:
        """Deactivate a template so it can no longer be resolved for rendering.

        Args:
            template_id: UUID of the template to deactivate.

        Returns:
            The updated template, or None if not found.
        """
        return await self.update_fields(template_id, {"is_active": False})

    async def soft_delete(self, template_id: uuid.UUID, deleted_at: datetime) -> bool:
        """Soft delete a template.

        Args:
            template_id: UUID of the template to delete.
            deleted_at: Timestamp of the deletion.

        Returns:
            True if a row was updated, False otherwise.
        """
        stmt = (
            update(NotificationTemplate)
            .where(
                NotificationTemplate.id == template_id,
                NotificationTemplate.is_deleted.is_(False),
            )
            .values(is_deleted=True, deleted_at=deleted_at, is_active=False)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return (result.rowcount or 0) > 0

    async def get_by_code(self, code: str) -> Optional[NotificationTemplate]:
        """Return the highest active, non-deleted version for a template code."""
        stmt = (
            select(NotificationTemplate)
            .where(
                NotificationTemplate.code == code,
                NotificationTemplate.is_active.is_(True),
                NotificationTemplate.is_deleted.is_(False),
            )
            .order_by(NotificationTemplate.version.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def render(self, template: NotificationTemplate, variables: dict) -> dict:
        """Render a template using simple {{name}} placeholders."""
        import re
        pattern = re.compile(r"\{\{\s*(\w+)\s*\}\}")

        def render_text(value: Optional[str]) -> Optional[str]:
            if value is None:
                return None
            def replace(match):
                key = match.group(1)
                if key not in variables:
                    raise ValueError(f"missing value for placeholder '{key}'")
                return str(variables[key])
            return pattern.sub(replace, value)

        return {
            "template_id": template.id,
            "subject": render_text(template.subject_template),
            "body": render_text(template.body_template) or "",
        }