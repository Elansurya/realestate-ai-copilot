# backend/app/services/template_service.py
"""Business logic for notification template management and rendering."""

import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

from app.models.notification import NotificationChannel
from app.models.notification_template import NotificationTemplate, TemplateLocale
from app.repositories.template_repository import TemplateRepository
from app.schemas.template import TemplateCreate, TemplateUpdate
from app.services.notification_service import TemplateNotFoundError, TemplateRenderError

_PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@dataclass
class RenderedContent:
    """Result of rendering a template against a set of variables.

    Attributes:
        template_id: UUID of the template that was rendered.
        subject: Rendered subject line, if the template defines one.
        body: Rendered body content.
    """

    template_id: uuid.UUID
    subject: Optional[str]
    body: str


class TemplateService:
    """Business logic layer for creating, versioning, and rendering templates.

    Attributes:
        template_repo: Data access layer for notification templates.
    """

    def __init__(self, template_repo: TemplateRepository) -> None:
        """Initialize the service.

        Args:
            template_repo: Repository for `NotificationTemplate` entities.
        """
        self.template_repo = template_repo

    async def create_template(self, data: TemplateCreate) -> NotificationTemplate:
        """Create a new template, auto-incrementing the version if a prior
        active version with the same code/channel/locale exists.

        Args:
            data: Validated template creation payload.

        Returns:
            The persisted template.
        """
        existing = await self.template_repo.get_active_by_code(
            data.code, data.channel, data.locale
        )
        version = existing.version + 1 if existing is not None else data.version

        template = NotificationTemplate(
            code=data.code,
            name=data.name,
            channel=data.channel,
            locale=data.locale,
            version=version,
            subject_template=data.subject_template,
            body_template=data.body_template,
            variables=data.variables,
            is_active=data.is_active,
        )
        return await self.template_repo.create(template)

    async def get_template(self, template_id: uuid.UUID) -> NotificationTemplate:
        """Fetch a template by primary key.

        Args:
            template_id: UUID of the template.

        Returns:
            The matching template.

        Raises:
            TemplateNotFoundError: If no matching template exists.
        """
        template = await self.template_repo.get_by_id(template_id)
        if template is None:
            raise TemplateNotFoundError(f"template {template_id} was not found")
        return template

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
            sort_by: Column name to sort by.
            sort_desc: Whether to sort in descending order.
            page: 1-indexed page number.
            page_size: Number of records per page.

        Returns:
            A tuple of (matching templates for the page, total count).
        """
        return await self.template_repo.list_templates(
            channel=channel,
            locale=locale,
            is_active=is_active,
            search_term=search_term,
            sort_by=sort_by,
            sort_desc=sort_desc,
            page=page,
            page_size=page_size,
        )

    async def update_template(
        self, template_id: uuid.UUID, data: TemplateUpdate
    ) -> NotificationTemplate:
        """Update mutable fields on an existing template.

        Args:
            template_id: UUID of the template to update.
            data: Fields to update; unset fields are left untouched.

        Returns:
            The updated template.

        Raises:
            TemplateNotFoundError: If no matching template exists.
        """
        await self.get_template(template_id)
        values = data.model_dump(exclude_unset=True)
        if not values:
            return await self.get_template(template_id)

        updated = await self.template_repo.update_fields(template_id, values)
        if updated is None:
            raise TemplateNotFoundError(f"template {template_id} was not found")
        return updated

    async def deactivate_template(self, template_id: uuid.UUID) -> NotificationTemplate:
        """Deactivate a template so it can no longer be resolved for rendering.

        Args:
            template_id: UUID of the template to deactivate.

        Returns:
            The updated template.

        Raises:
            TemplateNotFoundError: If no matching template exists.
        """
        updated = await self.template_repo.deactivate(template_id)
        if updated is None:
            raise TemplateNotFoundError(f"template {template_id} was not found")
        return updated

    async def delete_template(self, template_id: uuid.UUID, deleted_at) -> bool:
        """Soft delete a template.

        Args:
            template_id: UUID of the template to delete.
            deleted_at: Timestamp of the deletion.

        Returns:
            True once the template has been soft deleted.

        Raises:
            TemplateNotFoundError: If no matching template exists.
        """
        deleted = await self.template_repo.soft_delete(template_id, deleted_at)
        if not deleted:
            raise TemplateNotFoundError(f"template {template_id} was not found")
        return deleted

    async def render(
        self,
        code: str,
        channel: NotificationChannel,
        variables: Dict[str, Any],
        locale: TemplateLocale = TemplateLocale.EN_US,
    ) -> RenderedContent:
        """Resolve the active template for a key and render it with variables.

        Args:
            code: Business identifier for the template family.
            channel: Delivery channel the template must render for.
            variables: Values to substitute into the template placeholders.
            locale: Locale of the template content.

        Returns:
            The rendered subject (if any) and body content.

        Raises:
            TemplateNotFoundError: If no active template matches the key.
            TemplateRenderError: If required variables are missing.
        """
        template = await self.template_repo.get_active_by_code(code, channel, locale)
        if template is None:
            raise TemplateNotFoundError(
                f"no active template found for code={code}, channel={channel.value}, locale={locale.value}"
            )

        required_variables = set((template.variables or {}).keys())
        missing = required_variables - set(variables.keys())
        if missing:
            raise TemplateRenderError(
                f"missing required template variables: {sorted(missing)}"
            )

        subject = (
            self._render_string(template.subject_template, variables)
            if template.subject_template
            else None
        )
        body = self._render_string(template.body_template, variables)
        return RenderedContent(template_id=template.id, subject=subject, body=body)

    def _render_string(self, raw_template: str, variables: Dict[str, Any]) -> str:
        """Substitute `{{variable}}` placeholders in a template string.

        Args:
            raw_template: Template content containing placeholder tokens.
            variables: Values to substitute into the placeholders.

        Returns:
            The rendered string.

        Raises:
            TemplateRenderError: If a placeholder has no corresponding value.
        """

        def _replace(match: re.Match) -> str:
            key = match.group(1)
            if key not in variables:
                raise TemplateRenderError(f"missing value for placeholder '{key}'")
            return str(variables[key])

        return _PLACEHOLDER_PATTERN.sub(_replace, raw_template)