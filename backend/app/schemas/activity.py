"""
backend/app/schemas/activity.py

Pydantic v2 schemas for the Activity Timeline module of the Enterprise
Real Estate AI Copilot CRM.

Mirrors the shape of `app/models/activity.py` and follows the same
naming convention already established in `app/schemas/audit_log.py`:
    - `*Create`   -> payload accepted on creation.
    - `*Update`   -> payload accepted on partial update (PATCH-style,
      all fields optional).
    - `*Response` -> full representation returned by the API,
      including server-generated/audit fields.
    - `*ListResponse` -> paginated collection wrapper.
    - `*Filter`   -> query/filter/sort/pagination parameters.
    - `TimelineResponse` -> chronological feed scoped to a single
      entity (e.g. all activities for one Customer or Booking).
    - `StatisticsResponse` -> aggregate counts over a set of entries.

These schemas define the request/response contracts for creating,
updating, filtering, listing, and summarizing activity timeline
entries. They are consumed by the (separately implemented) service
and router layers.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, ClassVar, Optional

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.models.activity import (
    ActivityModule,
    ActivityPriority,
    ActivityStatus,
    ActivityType,
)

__all__ = [
    "ActivityCreate",
    "ActivityUpdate",
    "ActivityResponse",
    "ActivityListResponse",
    "ActivityFilter",
    "TimelineResponse",
    "StatisticsResponse",
]


class ActivityBase(BaseModel):
    """Shared base fields common to activity creation and representation.

    Attributes:
        module: Owning domain module that raised the activity.
        entity_type: Name of the entity/table the activity concerns.
        entity_id: Primary key of the affected entity.
        action: The action performed.
        title: Short, human-readable headline for the timeline entry.
        description: Optional longer free-text summary of the event.
        old_value: Snapshot of the relevant state prior to the action.
        new_value: Snapshot of the relevant state after the action.
        metadata: Arbitrary module-specific context payload.
        priority: Priority classification of the timeline entry.
        status: Current lifecycle status of the timeline entry.
        ip_address: Origin IP address of the triggering request.
        user_agent: User agent string of the triggering client.
        source: Free-form origin of the activity (e.g. ``"web"``).
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    module: ActivityModule = Field(..., description="Owning domain module.")
    entity_type: str = Field(
        ..., min_length=1, max_length=100, description="Affected entity/table name."
    )
    entity_id: str = Field(
        ..., min_length=1, max_length=64, description="Affected entity primary key."
    )
    action: ActivityType = Field(..., description="Action performed.")
    title: str = Field(
        ..., min_length=1, max_length=255, description="Timeline entry headline."
    )
    description: Optional[str] = Field(
        default=None, description="Longer free-text summary of the event."
    )
    old_value: Optional[dict[str, Any]] = Field(
        default=None, description="Entity state prior to the action."
    )
    new_value: Optional[dict[str, Any]] = Field(
        default=None, description="Entity state after the action."
    )
    metadata: Optional[dict[str, Any]] = Field(
        default=None, description="Arbitrary module-specific context payload."
    )
    priority: ActivityPriority = Field(
        default=ActivityPriority.NORMAL, description="Priority classification."
    )
    status: ActivityStatus = Field(
        default=ActivityStatus.ACTIVE, description="Current lifecycle status."
    )
    ip_address: Optional[str] = Field(
        default=None, max_length=45, description="Origin IP address."
    )
    user_agent: Optional[str] = Field(
        default=None, description="Client user agent string."
    )
    source: Optional[str] = Field(
        default="system", max_length=50, description="Origin of the activity."
    )

    @field_validator("title", "entity_type", "entity_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        """Ensures required text fields are not blank after stripping whitespace.

        Args:
            value: The raw field value.

        Returns:
            str: The validated, stripped value.

        Raises:
            ValueError: If the stripped value is empty.
        """
        if not value or not value.strip():
            raise ValueError("Field must not be empty or whitespace only.")
        return value


class ActivityCreate(ActivityBase):
    """Schema used to create a new activity timeline entry.

    Attributes:
        performed_by_id: Identifier of the acting user. ``None`` for
            system-initiated activities.
        assigned_to_id: Identifier of the user this activity is
            assigned to, if any.
    """

    performed_by_id: Optional[int] = Field(
        default=None, gt=0, description="Identifier of the acting user."
    )
    assigned_to_id: Optional[int] = Field(
        default=None, gt=0, description="Identifier of the assigned user."
    )


class ActivityUpdate(BaseModel):
    """Schema used to partially update an existing activity entry.

    All fields are optional; only supplied fields are applied. Fields
    that identify *what happened* (``module``, ``entity_type``,
    ``entity_id``, ``action``) are intentionally immutable and not
    exposed here.

    Attributes:
        title: Updated headline for the timeline entry.
        description: Updated free-text summary.
        new_value: Updated snapshot of state after the action.
        metadata: Updated arbitrary context payload.
        priority: Updated priority classification.
        status: Updated lifecycle status.
        assigned_to_id: Updated assignee, or ``None`` to unassign.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    title: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    new_value: Optional[dict[str, Any]] = None
    metadata: Optional[dict[str, Any]] = None
    priority: Optional[ActivityPriority] = None
    status: Optional[ActivityStatus] = None
    assigned_to_id: Optional[int] = Field(default=None, gt=0)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: Optional[str]) -> Optional[str]:
        """Ensures a supplied title is not blank after stripping whitespace.

        Args:
            value: The raw field value, if supplied.

        Returns:
            Optional[str]: The validated, stripped value, or ``None``.

        Raises:
            ValueError: If a supplied value is empty after stripping.
        """
        if value is not None and not value.strip():
            raise ValueError("title must not be empty or whitespace only.")
        return value


class ActivityResponse(ActivityBase):
    """Schema representing a persisted activity entry returned to clients.

    Attributes:
        id: Surrogate primary key of the activity entry.
        performed_by_id: Identifier of the acting user, if any.
        assigned_to_id: Identifier of the assigned user, if any.
        is_deleted: Soft-delete flag.
        deleted_at: Timestamp of soft deletion, if any.
        created_at: Timestamp the entry was created.
        updated_at: Timestamp the entry was last updated.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # Overrides `ActivityBase.metadata` to fix a genuine attribute-name
    # collision when validating from an `Activity` ORM instance
    # (`from_attributes=True`): the ORM's mapped Python attribute for
    # this data is `meta_data` (see `app/models/activity.py`), not
    # `metadata` -- `metadata` is reserved on every SQLAlchemy
    # declarative model as the class-level `Base.metadata` (a
    # `sqlalchemy.MetaData` instance). Without this override,
    # `getattr(activity, "metadata")` silently resolves to that
    # `MetaData` object instead of the actual payload, and validation
    # fails with `Input should be a valid dictionary`. `AliasChoices`
    # tries `"meta_data"` first (the correct ORM attribute) and falls
    # back to `"metadata"` so validation from a plain dict (e.g.
    # `{"metadata": {...}}`) still works unchanged.
    metadata: Optional[dict[str, Any]] = Field(
        default=None,
        validation_alias=AliasChoices("meta_data", "metadata"),
        description="Arbitrary module-specific context payload.",
    )
    performed_by_id: Optional[int] = None
    assigned_to_id: Optional[int] = None
    is_deleted: bool = False
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class ActivityListResponse(BaseModel):
    """Schema representing a paginated collection of activity entries.

    Attributes:
        items: The activity entries for the current page.
        total: Total number of entries matching the query, across all pages.
        page: Current page number (1-indexed).
        page_size: Number of items requested per page.
        total_pages: Total number of pages available.
    """

    model_config = ConfigDict(from_attributes=True)

    items: list[ActivityResponse] = Field(default_factory=list)
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1)
    total_pages: int = Field(..., ge=0)

    @model_validator(mode="after")
    def _compute_total_pages_if_needed(self) -> "ActivityListResponse":
        """Recomputes ``total_pages`` defensively when it appears inconsistent.

        Returns:
            ActivityListResponse: The validated model instance.
        """
        expected_pages = (
            (self.total + self.page_size - 1) // self.page_size
            if self.page_size
            else 0
        )
        if self.total_pages != expected_pages:
            self.total_pages = expected_pages
        return self


class ActivityFilter(BaseModel):
    """Schema encapsulating filter, sort, and pagination parameters for queries.

    Attributes:
        module: Restrict results to a specific owning module.
        entity_type: Restrict results to a specific entity type.
        entity_id: Restrict results to a specific entity id.
        action: Restrict results to a specific action.
        priority: Restrict results to a specific priority.
        status: Restrict results to a specific lifecycle status.
        performed_by_id: Restrict results to a specific acting user.
        assigned_to_id: Restrict results to a specific assigned user.
        source: Restrict results to a specific origin source.
        search: Free-text search applied to the title/description fields.
        date_from: Lower bound (inclusive) on ``created_at``.
        date_to: Upper bound (inclusive) on ``created_at``.
        page: Page number to retrieve (1-indexed).
        page_size: Number of items to retrieve per page.
        sort_by: Column name to sort by.
        sort_order: Sort direction, either ``"asc"`` or ``"desc"``.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    module: Optional[ActivityModule] = None
    entity_type: Optional[str] = Field(default=None, max_length=100)
    entity_id: Optional[str] = Field(default=None, max_length=64)
    action: Optional[ActivityType] = None
    priority: Optional[ActivityPriority] = None
    status: Optional[ActivityStatus] = None
    performed_by_id: Optional[int] = Field(default=None, gt=0)
    assigned_to_id: Optional[int] = Field(default=None, gt=0)
    source: Optional[str] = Field(default=None, max_length=50)
    search: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Free-text search on title/description.",
    )
    date_from: Optional[datetime] = Field(
        default=None, description="Inclusive lower bound on created_at."
    )
    date_to: Optional[datetime] = Field(
        default=None, description="Inclusive upper bound on created_at."
    )

    page: int = Field(default=1, ge=1, description="1-indexed page number.")
    page_size: int = Field(
        default=20, ge=1, le=200, description="Number of items per page."
    )

    sort_by: str = Field(
        default="created_at", description="Column name to sort results by."
    )
    sort_order: str = Field(
        default="desc", description="Sort direction: 'asc' or 'desc'."
    )

    _ALLOWED_SORT_FIELDS: ClassVar[frozenset] = frozenset(
        {
            "created_at",
            "updated_at",
            "module",
            "entity_type",
            "action",
            "priority",
            "status",
            "title",
        }
    )

    @field_validator("sort_order")
    @classmethod
    def _validate_sort_order(cls, value: str) -> str:
        """Validates that the sort order is one of the supported directions.

        Args:
            value: The requested sort order.

        Returns:
            str: The normalized (lowercased) sort order.

        Raises:
            ValueError: If the value is not ``"asc"`` or ``"desc"``.
        """
        normalized = value.strip().lower()
        if normalized not in {"asc", "desc"}:
            raise ValueError("sort_order must be either 'asc' or 'desc'.")
        return normalized

    @field_validator("sort_by")
    @classmethod
    def _validate_sort_by(cls, value: str) -> str:
        """Validates that the sort field is an allowed, indexed column.

        Args:
            value: The requested sort column name.

        Returns:
            str: The validated sort column name.

        Raises:
            ValueError: If the column is not in the allow-list.
        """
        if value not in cls._ALLOWED_SORT_FIELDS:
            raise ValueError(
                f"sort_by must be one of: {sorted(cls._ALLOWED_SORT_FIELDS)}"
            )
        return value

    @model_validator(mode="after")
    def _validate_date_range(self) -> "ActivityFilter":
        """Ensures the provided date range is chronologically valid.

        Returns:
            ActivityFilter: The validated model instance.

        Raises:
            ValueError: If ``date_from`` is after ``date_to``.
        """
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be after date_to.")
        return self


class TimelineResponse(BaseModel):
    """Schema representing a chronological activity feed for one entity.

    Used by endpoints that return the full history of activity for a
    single record (e.g. ``GET /customers/{id}/timeline``).

    Attributes:
        entity_type: The entity/table the timeline belongs to.
        entity_id: The primary key of the entity the timeline belongs to.
        items: Activity entries for the entity, ordered chronologically.
        total_count: Total number of activity entries for this entity.
        first_activity_at: Timestamp of the earliest recorded activity.
        last_activity_at: Timestamp of the most recent recorded activity.
    """

    model_config = ConfigDict(from_attributes=True)

    entity_type: str
    entity_id: str
    items: list[ActivityResponse] = Field(default_factory=list)
    total_count: int = Field(..., ge=0)
    first_activity_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None


class StatisticsResponse(BaseModel):
    """Schema representing aggregate statistics over a set of activity entries.

    Attributes:
        total_activities: Total number of activity entries in scope.
        by_module: Count of entries grouped by owning module.
        by_action: Count of entries grouped by action.
        by_priority: Count of entries grouped by priority.
        by_status: Count of entries grouped by status.
        date_from: Inclusive lower bound of the statistics window, if scoped.
        date_to: Inclusive upper bound of the statistics window, if scoped.
    """

    model_config = ConfigDict(from_attributes=True)

    total_activities: int = Field(..., ge=0)
    by_module: dict[str, int] = Field(default_factory=dict)
    by_action: dict[str, int] = Field(default_factory=dict)
    by_priority: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, int] = Field(default_factory=dict)
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None