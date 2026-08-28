"""
backend/app/services/activity_service.py

Business logic / orchestration layer for the Activity Timeline module.

The :class:`ActivityService` owns all domain validation and business
rules for activity timeline entries raised by every other module
(Customer, Lead, Property, Booking, Payment, Workflow, Notification,
AI, Audit, Document, Settings). It orchestrates
:class:`~app.repositories.activity_repository.ActivityRepository` for
persistence and never performs raw SQL or ORM queries itself, aside
from the lightweight existence check in ``_validate_user_exists``
(mirroring the same pattern already used in
``app.services.audit_log_service.AuditLogService``).

Only project domain exceptions (from ``app.core.exceptions``) are
raised from this layer -- never ``fastapi.HTTPException`` and never a
raw SQLAlchemy or framework exception. HTTP concerns are the sole
responsibility of the router layer.

NOTE ON EXCEPTION NAMING: `app.services.workflow_service` and
`app.services.audit_log_service` both import a
`BusinessRuleViolationException` from `app.core.exceptions`, but that
module only defines `BusinessRuleException` (same 422 semantics, same
`AppException` base, single-message constructor). This file raises the
real, importable `BusinessRuleException` instead of repeating that
undefined name, so that this module does not fail on import.

Mirrors: app/services/audit_log_service.py / app/services/workflow_service.py

ROUTER-COMPATIBILITY NOTE (added during Activity Timeline POST/PUT/
DELETE/RESTORE/timeline 500 audit):
    ``app/api/v1/activity.py`` calls this service using a CRUD-style
    surface (``create_activity``, ``update_activity``,
    ``delete_activity``, ``restore_activity``, ``get_entity_timeline``,
    ``get_module_timeline``, ``get_user_timeline``) that did not exist
    on this class -- only the lower-level ``*_timeline_event`` /
    ``get_timeline_by_*`` methods did, causing
    ``AttributeError: 'ActivityService' object has no attribute
    'create_activity'`` (and the equivalent for the other five
    endpoints) at request time, surfaced to clients as a generic 500 by
    the catch-all exception handler. The thin wrapper methods in the
    "Router-facing aliases" section below close that gap without
    altering the pre-existing lower-level methods, so any other code
    already depending on those lower-level names is unaffected.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional, Sequence

from sqlalchemy import select

from app.core.exceptions import (
    BusinessRuleException,
    ConflictException,
    NotFoundException,
    ValidationException,
)
from app.models.activity import (
    Activity,
    ActivityModule,
    ActivityPriority,
    ActivityStatus,
    ActivityType,
)
from app.models.user import User
from app.repositories.activity_repository import ActivityRepository
from app.schemas.activity import (
    ActivityCreate,
    ActivityFilter,
    ActivityListResponse,
    ActivityResponse,
    ActivityUpdate,
    StatisticsResponse,
    TimelineResponse,
)

__all__ = ["ActivityService"]


class ActivityService:
    """Encapsulates business rules and orchestration for the Activity
    Timeline module.

    Attributes:
        repository: Data-access layer used for all persistence operations.
    """

    #: Maximum number of entries accepted in a single bulk-create call.
    MAX_BULK_CREATE_SIZE: int = 500

    #: Maximum number of ids accepted in a single bulk-delete call.
    MAX_BULK_DELETE_SIZE: int = 1000

    #: Maximum number of rows returned by an entity timeline in one page.
    MAX_TIMELINE_PAGE_SIZE: int = 500

    #: Maximum number of rows accepted for the "recent activities" feed.
    MAX_RECENT_LIMIT: int = 200

    def __init__(self, repository: ActivityRepository) -> None:
        """Initializes the service with its repository dependency.

        Args:
            repository: The activity repository used for persistence.
        """
        self.repository = repository

    # ------------------------------------------------------------------
    # Validation helpers (business rules)
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_module(module: Any) -> ActivityModule:
        """Validates that the supplied module is a recognized enum member.

        Args:
            module: The module value to validate.

        Returns:
            ActivityModule: The validated module.

        Raises:
            ValidationException: If the module is not a member of
                :class:`ActivityModule`.
        """
        try:
            return ActivityModule(module)
        except ValueError as exc:
            raise ValidationException(f"Invalid activity module: {module!r}.") from exc

    @staticmethod
    def _validate_action(action: Any) -> ActivityType:
        """Validates that the supplied action is a recognized enum member.

        Args:
            action: The action value to validate.

        Returns:
            ActivityType: The validated action.

        Raises:
            ValidationException: If the action is not a member of
                :class:`ActivityType`.
        """
        try:
            return ActivityType(action)
        except ValueError as exc:
            raise ValidationException(f"Invalid activity action: {action!r}.") from exc

    @staticmethod
    def _validate_priority(priority: Any) -> ActivityPriority:
        """Validates that the supplied priority is a recognized enum member.

        Args:
            priority: The priority value to validate.

        Returns:
            ActivityPriority: The validated priority.

        Raises:
            ValidationException: If the priority is not a member of
                :class:`ActivityPriority`.
        """
        try:
            return ActivityPriority(priority)
        except ValueError as exc:
            raise ValidationException(
                f"Invalid activity priority: {priority!r}."
            ) from exc

    @staticmethod
    def _validate_status(status: Any) -> ActivityStatus:
        """Validates that the supplied status is a recognized enum member.

        Args:
            status: The status value to validate.

        Returns:
            ActivityStatus: The validated status.

        Raises:
            ValidationException: If the status is not a member of
                :class:`ActivityStatus`.
        """
        try:
            return ActivityStatus(status)
        except ValueError as exc:
            raise ValidationException(f"Invalid activity status: {status!r}.") from exc

    @staticmethod
    def _validate_title(title: str) -> str:
        """Validates that a title is present and meaningful.

        Args:
            title: The raw title text.

        Returns:
            str: The trimmed, validated title.

        Raises:
            ValidationException: If the title is empty or whitespace.
        """
        if not title or not title.strip():
            raise ValidationException("Activity title must not be empty.")
        return title.strip()

    @staticmethod
    def _validate_entity(entity_type: str, entity_id: str) -> tuple[str, str]:
        """Validates that the entity type/id pair is present and well-formed.

        Args:
            entity_type: Name of the entity/table the activity concerns.
            entity_id: Primary key of the affected entity.

        Returns:
            tuple[str, str]: The trimmed, validated ``(entity_type, entity_id)``.

        Raises:
            ValidationException: If either value is empty or whitespace.
        """
        if not entity_type or not entity_type.strip():
            raise ValidationException("Activity entity_type must not be empty.")
        if not entity_id or not entity_id.strip():
            raise ValidationException("Activity entity_id must not be empty.")
        return entity_type.strip(), entity_id.strip()

    async def _validate_user_exists(self, user_id: Optional[int]) -> None:
        """Validates that the referenced user exists, when one is supplied.

        Args:
            user_id: The user's identifier, or ``None`` for system-initiated
                activities / unassigned activities.

        Raises:
            NotFoundException: If ``user_id`` is supplied but no matching
                user record exists.
        """
        if user_id is None:
            return
        result = await self.repository.session.execute(
            select(User.id).where(User.id == user_id)
        )
        if result.scalar_one_or_none() is None:
            raise NotFoundException(f"User with id {user_id} does not exist.")

    async def _validate_and_normalize_create(
        self, payload: ActivityCreate
    ) -> dict[str, Any]:
        """Runs full validation on a creation payload and returns ORM-ready data.

        Args:
            payload: The incoming activity creation schema.

        Returns:
            dict[str, Any]: A mapping of column names to validated values,
            ready to be passed to the repository's ``create``/``bulk_create``.

        Raises:
            ValidationException: If any field fails validation.
            NotFoundException: If a referenced user does not exist.
        """
        module = self._validate_module(payload.module)
        action = self._validate_action(payload.action)
        priority = self._validate_priority(payload.priority)
        status = self._validate_status(payload.status)
        title = self._validate_title(payload.title)
        entity_type, entity_id = self._validate_entity(
            payload.entity_type, payload.entity_id
        )
        await self._validate_user_exists(payload.performed_by_id)
        await self._validate_user_exists(payload.assigned_to_id)

        return {
            "module": module,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "action": action,
            "title": title,
            "description": payload.description,
            "old_value": payload.old_value,
            "new_value": payload.new_value,
            "meta_data": payload.metadata,
            "priority": priority,
            "status": status,
            "performed_by_id": payload.performed_by_id,
            "assigned_to_id": payload.assigned_to_id,
            "ip_address": payload.ip_address,
            "user_agent": payload.user_agent,
            "source": payload.source,
        }

    # ------------------------------------------------------------------
    # Creation (timeline event creation)
    # ------------------------------------------------------------------

    async def create_timeline_event(self, payload: ActivityCreate) -> ActivityResponse:
        """Validates and persists a single activity timeline entry.

        Args:
            payload: The activity creation request.

        Returns:
            ActivityResponse: The persisted activity entry.

        Raises:
            ValidationException: If any field fails validation.
            NotFoundException: If a referenced user does not exist.
        """
        data = await self._validate_and_normalize_create(payload)
        entry = await self.repository.create(data)
        return ActivityResponse.model_validate(entry)

    async def bulk_create_timeline_events(
        self, payloads: Sequence[ActivityCreate]
    ) -> list[ActivityResponse]:
        """Validates and persists multiple activity entries at once.

        Args:
            payloads: The activity creation requests.

        Returns:
            list[ActivityResponse]: The persisted entries, in the same order
            as the input sequence.

        Raises:
            ValidationException: If the batch is empty, exceeds the maximum
                allowed size, or any individual entry fails validation.
            NotFoundException: If any referenced user does not exist.
        """
        if not payloads:
            raise ValidationException(
                "Bulk activity creation requires at least one entry."
            )
        if len(payloads) > self.MAX_BULK_CREATE_SIZE:
            raise ValidationException(
                "Bulk activity creation exceeds the maximum batch size of "
                f"{self.MAX_BULK_CREATE_SIZE}."
            )

        normalized_rows = [
            await self._validate_and_normalize_create(payload) for payload in payloads
        ]
        entries = await self.repository.bulk_create(normalized_rows)
        return [ActivityResponse.model_validate(entry) for entry in entries]

    # ------------------------------------------------------------------
    # Router-facing aliases
    #
    # `app/api/v1/activity.py` calls the service using these CRUD-style
    # names. They are thin wrappers around the pre-existing
    # `*_timeline_event` / `get_timeline_by_*` methods above/below (which
    # remain unchanged, in case other callers already depend on them),
    # so there is exactly one implementation of every business rule.
    # ------------------------------------------------------------------

    async def create_activity(
        self, payload: ActivityCreate, *, actor_id: Optional[int] = None
    ) -> ActivityResponse:
        """Validates and persists a new activity timeline entry.

        Router-facing entry point for ``POST /activities``. Identical to
        :meth:`create_timeline_event`, except that when the payload does
        not explicitly supply ``performed_by_id``, it is defaulted to
        ``actor_id`` (the authenticated caller), so activities created
        through the API are attributed to the acting user by default.

        Args:
            payload: The activity creation request.
            actor_id: The authenticated caller's user id, used as the
                default ``performed_by_id`` when the payload omits it.
                ``None`` is valid for system-initiated calls.

        Returns:
            ActivityResponse: The persisted activity entry.

        Raises:
            ValidationException: If any field fails validation.
            NotFoundException: If a referenced user does not exist.
        """
        data = await self._validate_and_normalize_create(payload)

        if data.get("performed_by_id") is None and actor_id is not None:
            await self._validate_user_exists(actor_id)
            data["performed_by_id"] = actor_id

        entry = await self.repository.create(data)
        return ActivityResponse.model_validate(entry)

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    async def get_activity(
        self, activity_id: uuid.UUID, *, include_deleted: bool = False
    ) -> ActivityResponse:
        """Retrieves a single activity entry by id.

        Args:
            activity_id: The UUID primary key of the entry.
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            ActivityResponse: The matching activity entry.

        Raises:
            NotFoundException: If no entry with the given id exists.
        """
        entry = await self.repository.get_by_id(
            activity_id, include_deleted=include_deleted
        )
        if entry is None:
            raise NotFoundException(f"Activity with id {activity_id} was not found.")
        return ActivityResponse.model_validate(entry)

    async def list_activities(self, filters: ActivityFilter) -> ActivityListResponse:
        """Retrieves a filtered, sorted, paginated page of activity entries.

        Args:
            filters: The combined filter, sort, and pagination parameters.

        Returns:
            ActivityListResponse: The requested page of entries plus
            pagination metadata.
        """
        items, total = await self.repository.list_activities(
            module=filters.module.value if filters.module else None,
            entity_type=filters.entity_type,
            entity_id=filters.entity_id,
            action=filters.action.value if filters.action else None,
            priority=filters.priority.value if filters.priority else None,
            status=filters.status.value if filters.status else None,
            performed_by_id=filters.performed_by_id,
            assigned_to_id=filters.assigned_to_id,
            source=filters.source,
            search=filters.search,
            date_from=filters.date_from,
            date_to=filters.date_to,
            page=filters.page,
            page_size=filters.page_size,
            sort_by=filters.sort_by,
            sort_order=filters.sort_order,
        )
        total_pages = (
            (total + filters.page_size - 1) // filters.page_size
            if filters.page_size
            else 0
        )
        return ActivityListResponse(
            items=[ActivityResponse.model_validate(item) for item in items],
            total=total,
            page=filters.page,
            page_size=filters.page_size,
            total_pages=total_pages,
        )

    async def search_activities(
        self,
        search_term: str,
        *,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> ActivityListResponse:
        """Performs a validated free-text search over activity title/description.

        Args:
            search_term: The text to search for.
            page: 1-indexed page number.
            page_size: Number of rows per page.
            sort_by: Column name to order by.
            sort_order: ``"asc"`` or ``"desc"``.

        Returns:
            ActivityListResponse: The matching page of entries plus
            pagination metadata.

        Raises:
            ValidationException: If the search term is empty.
        """
        if not search_term or not search_term.strip():
            raise ValidationException("Search term must not be empty.")

        items, total = await self.repository.search_activities(
            search_term.strip(),
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        total_pages = (total + page_size - 1) // page_size if page_size else 0
        return ActivityListResponse(
            items=[ActivityResponse.model_validate(item) for item in items],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )

    # ------------------------------------------------------------------
    # Timeline builders
    # ------------------------------------------------------------------

    async def get_timeline_by_entity(
        self,
        entity_type: str,
        entity_id: str,
        *,
        page: int = 1,
        page_size: int = 200,
        sort_order: str = "asc",
    ) -> TimelineResponse:
        """Builds the chronological activity timeline for a single entity.

        Args:
            entity_type: The name of the entity type (e.g. ``"Customer"``).
            entity_id: The primary key of the entity.
            page: 1-indexed page number.
            page_size: Number of rows per page.
            sort_order: ``"asc"`` (chronological) or ``"desc"`` (newest first).

        Returns:
            TimelineResponse: The entity's activity feed plus summary metadata.

        Raises:
            ValidationException: If ``entity_type``/``entity_id`` is empty or
                ``page_size`` exceeds the allowed maximum.
        """
        entity_type, entity_id = self._validate_entity(entity_type, entity_id)
        if page_size > self.MAX_TIMELINE_PAGE_SIZE:
            raise ValidationException(
                "page_size exceeds the maximum timeline page size of "
                f"{self.MAX_TIMELINE_PAGE_SIZE}."
            )

        items, total = await self.repository.get_timeline_by_entity(
            entity_type,
            entity_id,
            page=page,
            page_size=page_size,
            sort_order=sort_order,
        )

        timestamps = [item.created_at for item in items]
        return TimelineResponse(
            entity_type=entity_type,
            entity_id=entity_id,
            items=[ActivityResponse.model_validate(item) for item in items],
            total_count=total,
            first_activity_at=min(timestamps) if timestamps else None,
            last_activity_at=max(timestamps) if timestamps else None,
        )

    async def get_timeline_by_module(
        self,
        module: Any,
        *,
        page: int = 1,
        page_size: int = 50,
        sort_order: str = "desc",
    ) -> ActivityListResponse:
        """Builds the activity feed scoped to a single owning module.

        Args:
            module: The owning module to scope the feed to.
            page: 1-indexed page number.
            page_size: Number of rows per page.
            sort_order: ``"asc"`` or ``"desc"``.

        Returns:
            ActivityListResponse: The module's activity feed plus pagination
            metadata.

        Raises:
            ValidationException: If ``module`` is not a recognized value.
        """
        validated_module = self._validate_module(module)
        items, total = await self.repository.get_timeline_by_module(
            validated_module.value,
            page=page,
            page_size=page_size,
            sort_order=sort_order,
        )
        total_pages = (total + page_size - 1) // page_size if page_size else 0
        return ActivityListResponse(
            items=[ActivityResponse.model_validate(item) for item in items],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )

    async def get_timeline_by_user(
        self,
        user_id: int,
        *,
        page: int = 1,
        page_size: int = 50,
        sort_order: str = "desc",
    ) -> ActivityListResponse:
        """Builds the activity feed involving a specific user (as performer
        or assignee).

        Args:
            user_id: The user id to scope the feed to.
            page: 1-indexed page number.
            page_size: Number of rows per page.
            sort_order: ``"asc"`` or ``"desc"``.

        Returns:
            ActivityListResponse: The user's activity feed plus pagination
            metadata.

        Raises:
            ValidationException: If ``user_id`` is not a positive integer.
            NotFoundException: If no user with that id exists.
        """
        if user_id is None or user_id <= 0:
            raise ValidationException("user_id must be a positive integer.")
        await self._validate_user_exists(user_id)

        items, total = await self.repository.get_timeline_by_user(
            user_id,
            page=page,
            page_size=page_size,
            sort_order=sort_order,
        )
        total_pages = (total + page_size - 1) // page_size if page_size else 0
        return ActivityListResponse(
            items=[ActivityResponse.model_validate(item) for item in items],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )

    # ------------------------------------------------------------------
    # Router-facing timeline aliases
    #
    # `app/api/v1/activity.py`'s `/timeline/{entity_type}/{entity_id}`,
    # `/module/{module}`, and `/user/{user_id}` routes call these three
    # names. `get_entity_timeline` returns the same `TimelineResponse`
    # shape as `get_timeline_by_entity`, so it is a direct pass-through.
    # `get_module_timeline` / `get_user_timeline`, however, are consumed
    # by router code that builds its own `ActivityListResponse` from a
    # raw `(items, total)` pair -- the same shape the repository layer
    # already returns -- rather than accepting an already-built
    # `ActivityListResponse`, so these two return that raw pair instead
    # of delegating to `get_timeline_by_module`/`get_timeline_by_user`.
    # ------------------------------------------------------------------

    async def get_entity_timeline(
        self,
        entity_type: str,
        entity_id: str,
        *,
        page: int = 1,
        page_size: int = 200,
        sort_order: str = "asc",
    ) -> TimelineResponse:
        """Router-facing alias of :meth:`get_timeline_by_entity`.

        Args:
            entity_type: The entity/table the timeline belongs to.
            entity_id: The primary key of the entity.
            page: 1-indexed page number.
            page_size: Number of rows per page.
            sort_order: ``"asc"`` or ``"desc"``.

        Returns:
            TimelineResponse: The entity's activity timeline.
        """
        return await self.get_timeline_by_entity(
            entity_type,
            entity_id,
            page=page,
            page_size=page_size,
            sort_order=sort_order,
        )

    async def get_module_timeline(
        self,
        module: Any,
        *,
        page: int = 1,
        page_size: int = 50,
        sort_order: str = "desc",
    ) -> tuple[list[Activity], int]:
        """Retrieves the raw activity feed for an entire owning module.

        Unlike :meth:`get_timeline_by_module`, this returns the raw
        ``(items, total)`` pair of ORM instances (matching what the
        router's ``/module/{module}`` route expects, so it can build its
        own ``ActivityListResponse``), rather than an already-built
        ``ActivityListResponse``.

        Args:
            module: The owning module to scope the feed to.
            page: 1-indexed page number.
            page_size: Number of rows per page.
            sort_order: ``"asc"`` or ``"desc"``.

        Returns:
            tuple[list[Activity], int]: The page of matching ORM entries
            and the total count of entries for the module.

        Raises:
            ValidationException: If ``module`` is not a recognized value.
        """
        validated_module = self._validate_module(module)
        return await self.repository.get_timeline_by_module(
            validated_module.value,
            page=page,
            page_size=page_size,
            sort_order=sort_order,
        )

    async def get_user_timeline(
        self,
        user_id: int,
        *,
        page: int = 1,
        page_size: int = 50,
        sort_order: str = "desc",
    ) -> tuple[list[Activity], int]:
        """Retrieves the raw activity feed involving a specific user.

        Unlike :meth:`get_timeline_by_user`, this returns the raw
        ``(items, total)`` pair of ORM instances (matching what the
        router's ``/user/{user_id}`` route expects), rather than an
        already-built ``ActivityListResponse``.

        Args:
            user_id: The user id to scope the feed to.
            page: 1-indexed page number.
            page_size: Number of rows per page.
            sort_order: ``"asc"`` or ``"desc"``.

        Returns:
            tuple[list[Activity], int]: The page of matching ORM entries
            and the total count of entries involving the user.

        Raises:
            ValidationException: If ``user_id`` is not a positive integer.
            NotFoundException: If no user with that id exists.
        """
        if user_id is None or user_id <= 0:
            raise ValidationException("user_id must be a positive integer.")
        await self._validate_user_exists(user_id)

        return await self.repository.get_timeline_by_user(
            user_id,
            page=page,
            page_size=page_size,
            sort_order=sort_order,
        )

    # ------------------------------------------------------------------
    # Update / soft delete / restore
    # ------------------------------------------------------------------

    async def update_timeline_event(
        self, activity_id: uuid.UUID, payload: ActivityUpdate
    ) -> ActivityResponse:
        """Applies a partial update to an existing activity entry.

        Args:
            activity_id: The UUID primary key of the entry to update.
            payload: The partial update payload; unset fields are ignored.

        Returns:
            ActivityResponse: The updated activity entry.

        Raises:
            NotFoundException: If no entry with the given id exists.
            ConflictException: If the entry has been soft-deleted.
            ValidationException: If any supplied field fails validation.
            NotFoundException: If a newly assigned user does not exist.
        """
        entry = await self.repository.get_by_id(activity_id, include_deleted=False)
        if entry is None:
            raise NotFoundException(f"Activity with id {activity_id} was not found.")
        if entry.is_deleted:
            raise ConflictException(
                f"Activity '{activity_id}' is deleted and cannot be updated."
            )

        updates = payload.model_dump(exclude_unset=True)
        data: dict[str, Any] = {}

        if "title" in updates:
            data["title"] = self._validate_title(updates["title"])
        if "description" in updates:
            data["description"] = updates["description"]
        if "new_value" in updates:
            data["new_value"] = updates["new_value"]
        if "metadata" in updates:
            data["meta_data"] = updates["metadata"]
        if "priority" in updates:
            data["priority"] = self._validate_priority(updates["priority"])
        if "status" in updates:
            data["status"] = self._validate_status(updates["status"])
        if "assigned_to_id" in updates:
            await self._validate_user_exists(updates["assigned_to_id"])
            data["assigned_to_id"] = updates["assigned_to_id"]

        if not data:
            return ActivityResponse.model_validate(entry)

        updated = await self.repository.update(entry, data)
        return ActivityResponse.model_validate(updated)

    async def delete_timeline_event(self, activity_id: uuid.UUID) -> ActivityResponse:
        """Soft-deletes an activity entry.

        Args:
            activity_id: The UUID primary key of the entry to delete.

        Returns:
            ActivityResponse: The soft-deleted activity entry.

        Raises:
            NotFoundException: If no active entry with the given id exists.
            ConflictException: If the entry is already soft-deleted.
        """
        entry = await self.repository.get_by_id(activity_id, include_deleted=False)
        if entry is None:
            raise NotFoundException(f"Activity with id {activity_id} was not found.")
        if entry.is_deleted:
            raise ConflictException(f"Activity '{activity_id}' is already deleted.")

        deleted = await self.repository.soft_delete(entry)
        return ActivityResponse.model_validate(deleted)

    async def restore_timeline_event(self, activity_id: uuid.UUID) -> ActivityResponse:
        """Restores a previously soft-deleted activity entry.

        Args:
            activity_id: The UUID primary key of the entry to restore.

        Returns:
            ActivityResponse: The restored activity entry.

        Raises:
            NotFoundException: If no entry with the given id exists at all.
            ConflictException: If the entry is not currently deleted.
        """
        entry = await self.repository.get_by_id(activity_id, include_deleted=True)
        if entry is None:
            raise NotFoundException(f"Activity with id {activity_id} was not found.")
        if not entry.is_deleted:
            raise ConflictException(f"Activity '{activity_id}' is not deleted.")

        restored = await self.repository.restore(entry)
        return ActivityResponse.model_validate(restored)

    async def bulk_delete_timeline_events(self, ids: Sequence[uuid.UUID]) -> int:
        """Soft-deletes a specific, bounded set of activity entries.

        Args:
            ids: The primary keys of the entries to soft-delete.

        Returns:
            int: The number of entries soft-deleted.

        Raises:
            ValidationException: If ``ids`` is empty or exceeds the maximum
                allowed batch size.
        """
        if not ids:
            raise ValidationException(
                "At least one id must be supplied for bulk delete."
            )
        if len(ids) > self.MAX_BULK_DELETE_SIZE:
            raise ValidationException(
                "Bulk delete exceeds the maximum batch size of "
                f"{self.MAX_BULK_DELETE_SIZE}."
            )
        return await self.repository.bulk_soft_delete(ids)

    # ------------------------------------------------------------------
    # Router-facing mutation aliases
    #
    # Thin wrappers so `app/api/v1/activity.py`'s PUT/DELETE/PATCH
    # routes (`update_activity`, `delete_activity`, `restore_activity`)
    # resolve to a real method. The pre-existing `*_timeline_event`
    # methods above retain all the actual business logic and remain
    # unchanged.
    # ------------------------------------------------------------------

    async def update_activity(
        self, activity_id: uuid.UUID, payload: ActivityUpdate
    ) -> ActivityResponse:
        """Router-facing alias of :meth:`update_timeline_event`.

        Args:
            activity_id: The UUID primary key of the entry to update.
            payload: The partial update payload; unset fields are ignored.

        Returns:
            ActivityResponse: The updated activity entry.

        Raises:
            NotFoundException: If no entry with the given id exists.
            ConflictException: If the entry has been soft-deleted.
            ValidationException: If any supplied field fails validation.
            NotFoundException: If a newly assigned user does not exist.
        """
        return await self.update_timeline_event(activity_id, payload)

    async def delete_activity(self, activity_id: uuid.UUID) -> ActivityResponse:
        """Router-facing alias of :meth:`delete_timeline_event`.

        Args:
            activity_id: The UUID primary key of the entry to delete.

        Returns:
            ActivityResponse: The soft-deleted activity entry.

        Raises:
            NotFoundException: If no active entry with the given id exists.
            ConflictException: If the entry is already soft-deleted.
        """
        return await self.delete_timeline_event(activity_id)

    async def restore_activity(self, activity_id: uuid.UUID) -> ActivityResponse:
        """Router-facing alias of :meth:`restore_timeline_event`.

        Args:
            activity_id: The UUID primary key of the entry to restore.

        Returns:
            ActivityResponse: The restored activity entry.

        Raises:
            NotFoundException: If no entry with the given id exists at all.
            ConflictException: If the entry is not currently deleted.
        """
        return await self.restore_timeline_event(activity_id)

    # ------------------------------------------------------------------
    # Statistics / recent activity
    # ------------------------------------------------------------------

    async def get_statistics(
        self,
        *,
        module: Optional[Any] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> StatisticsResponse:
        """Computes aggregate activity statistics over an optional scope.

        Args:
            module: Optional owning-module filter for the total count.
            entity_type: Optional entity type filter for the total count.
            entity_id: Optional entity id filter for the total count.
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.

        Returns:
            StatisticsResponse: The computed aggregate statistics.

        Raises:
            ValidationException: If ``date_from`` is after ``date_to`` or
                ``module`` is not a recognized value.
        """
        if date_from and date_to and date_from > date_to:
            raise ValidationException("date_from must not be after date_to.")

        validated_module = self._validate_module(module) if module else None

        total = await self.repository.get_total_count(
            module=validated_module.value if validated_module else None,
            entity_type=entity_type,
            entity_id=entity_id,
            date_from=date_from,
            date_to=date_to,
        )
        by_module = await self.repository.count_by_module(
            date_from=date_from, date_to=date_to
        )
        by_action = await self.repository.count_by_action(
            date_from=date_from, date_to=date_to
        )
        by_priority = await self.repository.count_by_priority(
            date_from=date_from, date_to=date_to
        )
        by_status = await self.repository.count_by_status(
            date_from=date_from, date_to=date_to
        )

        return StatisticsResponse(
            total_activities=total,
            by_module=by_module,
            by_action=by_action,
            by_priority=by_priority,
            by_status=by_status,
            date_from=date_from,
            date_to=date_to,
        )

    async def get_recent_activities(
        self,
        *,
        limit: int = 20,
        module: Optional[Any] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
    ) -> list[ActivityResponse]:
        """Retrieves the most recent activity entries, optionally scoped.

        Args:
            limit: Maximum number of entries to return.
            module: Optional owning-module filter.
            entity_type: Optional entity type filter.
            entity_id: Optional entity id filter.

        Returns:
            list[ActivityResponse]: The most recent entries, newest first.

        Raises:
            ValidationException: If ``limit`` is not a positive integer
                within the allowed maximum, or ``module`` is not recognized.
        """
        if limit <= 0:
            raise ValidationException("limit must be a positive integer.")
        if limit > self.MAX_RECENT_LIMIT:
            raise ValidationException(
                f"limit must not exceed {self.MAX_RECENT_LIMIT}."
            )

        validated_module = self._validate_module(module) if module else None

        entries = await self.repository.get_recent_activities(
            limit,
            module=validated_module.value if validated_module else None,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        return [ActivityResponse.model_validate(entry) for entry in entries]