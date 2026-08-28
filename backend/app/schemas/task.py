"""
backend/app/schemas/task.py

Pydantic v2 schemas for the Task Management module of the Enterprise
Real Estate AI Copilot CRM.

Mirrors the shape of `app/models/task.py` and follows the same naming
convention already established in `app/schemas/activity.py`:
    - `*Create`       -> payload accepted on creation.
    - `*Update`       -> payload accepted on partial update (PATCH-style,
      all fields optional; excludes fields covered by their own
      dedicated schema, e.g. status/assignment).
    - `*Assign`       -> dedicated payload for assignment/reassignment.
    - `*StatusUpdate` -> dedicated payload for lifecycle status
      transitions (start, hold, complete, cancel).
    - `*Response`     -> full representation returned by the API,
      including server-generated/audit fields.
    - `*ListResponse` -> paginated collection wrapper.
    - `*Filter`       -> query/filter/sort/pagination parameters.
    - `*StatisticsResponse` -> aggregate counts over a set of entries.

These schemas define the request/response contracts for creating,
assigning, updating, filtering, listing, and summarizing tasks. They
are consumed by the (separately implemented) service and router
layers.
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

from app.models.task import TaskPriority, TaskStatus, TaskType

__all__ = [
    "TaskCreate",
    "TaskUpdate",
    "TaskAssign",
    "TaskStatusUpdate",
    "TaskResponse",
    "TaskListResponse",
    "TaskStatisticsResponse",
    "TaskFilter",
]


class TaskBase(BaseModel):
    """Shared base fields common to task creation and representation.

    Attributes:
        title: Short, human-readable headline for the task.
        description: Optional longer free-text description of the work.
        task_type: Category of work the task represents.
        priority: Priority classification of the task.
        status: Current lifecycle status of the task.
        due_date: Optional target completion timestamp.
        reminder_time: Optional timestamp at which a reminder should
            be raised.
        related_module: Owning domain module this task concerns, if any.
        related_entity_id: Primary key of the related entity within
            `related_module`, if any.
        metadata: Arbitrary module-specific context payload.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(
        ..., min_length=1, max_length=255, description="Task headline."
    )
    description: Optional[str] = Field(
        default=None, description="Longer free-text description of the work."
    )
    task_type: TaskType = Field(
        default=TaskType.GENERAL, description="Category of work."
    )
    priority: TaskPriority = Field(
        default=TaskPriority.NORMAL, description="Priority classification."
    )
    status: TaskStatus = Field(
        default=TaskStatus.PENDING, description="Current lifecycle status."
    )
    due_date: Optional[datetime] = Field(
        default=None, description="Target completion timestamp."
    )
    reminder_time: Optional[datetime] = Field(
        default=None, description="Timestamp at which a reminder should fire."
    )
    related_module: Optional[str] = Field(
        default=None,
        max_length=50,
        description="Owning domain module this task concerns (e.g. 'lead').",
    )
    related_entity_id: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Primary key of the related entity within related_module.",
    )
    metadata: Optional[dict[str, Any]] = Field(
        default=None, description="Arbitrary module-specific context payload."
    )

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        """Ensures the title is not blank after stripping whitespace.

        Args:
            value: The raw title text.

        Returns:
            str: The validated, stripped title.

        Raises:
            ValueError: If the stripped value is empty.
        """
        if not value or not value.strip():
            raise ValueError("title must not be empty or whitespace only.")
        return value

    @model_validator(mode="after")
    def _validate_related_entity_pair(self) -> "TaskBase":
        """Ensures `related_module`/`related_entity_id` are both set or both unset.

        Returns:
            TaskBase: The validated model instance.

        Raises:
            ValueError: If exactly one of the pair is supplied.
        """
        has_module = self.related_module is not None
        has_entity_id = self.related_entity_id is not None
        if has_module != has_entity_id:
            raise ValueError(
                "related_module and related_entity_id must both be supplied "
                "together, or both omitted."
            )
        return self

    @model_validator(mode="after")
    def _validate_reminder_before_due(self) -> "TaskBase":
        """Ensures the reminder time does not fall after the due date.

        Returns:
            TaskBase: The validated model instance.

        Raises:
            ValueError: If `reminder_time` is after `due_date`.
        """
        if (
            self.reminder_time is not None
            and self.due_date is not None
            and self.reminder_time > self.due_date
        ):
            raise ValueError("reminder_time must not be after due_date.")
        return self


class TaskCreate(TaskBase):
    """Schema used to create a new task.

    Attributes:
        assigned_to_id: Identifier of the user the task is assigned to
            at creation time, if any.
        created_by_id: Identifier of the creating user. ``None`` for
            system-initiated tasks (e.g. raised by a Workflow step).
    """

    assigned_to_id: Optional[int] = Field(
        default=None, gt=0, description="Identifier of the assigned user."
    )
    created_by_id: Optional[int] = Field(
        default=None, gt=0, description="Identifier of the creating user."
    )


class TaskUpdate(BaseModel):
    """Schema used to partially update an existing task's descriptive fields.

    All fields are optional; only supplied fields are applied. Lifecycle
    status transitions are handled exclusively via :class:`TaskStatusUpdate`
    and assignment/reassignment exclusively via :class:`TaskAssign`, so
    neither `status` nor `assigned_to_id` is exposed here.

    Attributes:
        title: Updated headline for the task.
        description: Updated free-text description.
        task_type: Updated category of work.
        priority: Updated priority classification.
        due_date: Updated target completion timestamp.
        reminder_time: Updated reminder timestamp.
        metadata: Updated arbitrary context payload.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    title: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    task_type: Optional[TaskType] = None
    priority: Optional[TaskPriority] = None
    due_date: Optional[datetime] = None
    reminder_time: Optional[datetime] = None
    metadata: Optional[dict[str, Any]] = None

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

    @model_validator(mode="after")
    def _validate_reminder_before_due(self) -> "TaskUpdate":
        """Ensures a supplied reminder time does not fall after a supplied due date.

        Returns:
            TaskUpdate: The validated model instance.

        Raises:
            ValueError: If both fields are supplied and `reminder_time`
                is after `due_date`.
        """
        if (
            self.reminder_time is not None
            and self.due_date is not None
            and self.reminder_time > self.due_date
        ):
            raise ValueError("reminder_time must not be after due_date.")
        return self


class TaskAssign(BaseModel):
    """Schema used to assign or reassign a task to a user.

    Used for both the initial assignment and any subsequent
    reassignment; supplying ``assigned_to_id=None`` unassigns the task.

    Attributes:
        assigned_to_id: Identifier of the user to assign the task to,
            or ``None`` to unassign.
        note: Optional free-text note explaining the assignment/
            reassignment (e.g. surfaced via Activity Timeline/Comments
            integration).
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    assigned_to_id: Optional[int] = Field(
        default=None, gt=0, description="Identifier of the assigned user."
    )
    note: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="Optional note explaining the (re)assignment.",
    )


class TaskStatusUpdate(BaseModel):
    """Schema used to transition a task's lifecycle status.

    Attributes:
        status: The target lifecycle status.
        comment: Optional free-text note accompanying the transition
            (e.g. a cancellation reason or completion note).
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    status: TaskStatus = Field(..., description="Target lifecycle status.")
    comment: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="Optional note accompanying the status transition.",
    )


class TaskResponse(TaskBase):
    """Schema representing a persisted task returned to clients.

    Attributes:
        id: Surrogate primary key of the task.
        assigned_to_id: Identifier of the assigned user, if any.
        created_by_id: Identifier of the creating user, if any.
        completed_by_id: Identifier of the completing user, if any.
        comments_count: Denormalized count of comments on the task.
        attachments_count: Denormalized count of attachment metadata
            entries on the task.
        completed_at: Timestamp the task was completed, if any.
        is_overdue: Derived flag indicating whether the task is
            currently overdue (past `due_date` and not in a terminal
            status). Computed at read time, not persisted.
        is_deleted: Soft-delete flag.
        deleted_at: Timestamp of soft deletion, if any.
        created_at: Timestamp the task was created.
        updated_at: Timestamp the task was last updated.
    """

    # `populate_by_name=True` is required alongside the `metadata`
    # field's `validation_alias` below: it lets that field be
    # populated either by its declared name ("metadata", used by
    # plain dict/JSON input) or by its alias ("meta_data", needed for
    # `from_attributes=True` ORM reads -- see the field's docstring).
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    # Overrides `TaskBase.metadata` to fix attribute resolution when
    # validating from an ORM `Task` instance (`from_attributes=True`).
    # `Task`'s JSON payload column is mapped to the Python attribute
    # `meta_data`, not `metadata`, specifically because `metadata` is
    # already a reserved attribute on every SQLAlchemy declarative
    # model (`Base.metadata`, the schema `MetaData` registry) -- see
    # `app/models/task.py`. Without this alias, `getattr(task,
    # "metadata")` silently returns that unrelated `MetaData` object
    # instead of the task's JSON payload (or `None`), and validation
    # fails with "Input should be a valid dictionary" for every real
    # (non-dict) `Task` row.
    metadata: Optional[dict[str, Any]] = Field(
        default=None,
        # Order matters: every SQLAlchemy declarative instance (e.g.
        # `Task`) has *both* attributes, so `AliasChoices` picks
        # whichever is listed first that is *present* on the source
        # object, not whichever actually resolves to a dict. Listing
        # "meta_data" first ensures ORM reads pick the real JSONB
        # column; "metadata" remains second so plain dict/JSON input
        # (which has no "meta_data" key) still populates correctly.
        validation_alias=AliasChoices("meta_data", "metadata"),
        description="Arbitrary module-specific context payload.",
    )
    assigned_to_id: Optional[int] = None
    created_by_id: Optional[int] = None
    completed_by_id: Optional[int] = None
    comments_count: int = 0
    attachments_count: int = 0
    completed_at: Optional[datetime] = None
    is_overdue: bool = False
    is_deleted: bool = False
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class TaskListResponse(BaseModel):
    """Schema representing a paginated collection of tasks.

    Attributes:
        items: The tasks for the current page.
        total: Total number of tasks matching the query, across all pages.
        page: Current page number (1-indexed).
        page_size: Number of items requested per page.
        total_pages: Total number of pages available.
    """

    model_config = ConfigDict(from_attributes=True)

    items: list[TaskResponse] = Field(default_factory=list)
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1)
    total_pages: int = Field(..., ge=0)

    @model_validator(mode="after")
    def _compute_total_pages_if_needed(self) -> "TaskListResponse":
        """Recomputes ``total_pages`` defensively when it appears inconsistent.

        Returns:
            TaskListResponse: The validated model instance.
        """
        expected_pages = (
            (self.total + self.page_size - 1) // self.page_size
            if self.page_size
            else 0
        )
        if self.total_pages != expected_pages:
            self.total_pages = expected_pages
        return self


class TaskFilter(BaseModel):
    """Schema encapsulating filter, sort, and pagination parameters for queries.

    Attributes:
        status: Restrict results to a specific lifecycle status.
        priority: Restrict results to a specific priority.
        task_type: Restrict results to a specific task type.
        assigned_to_id: Restrict results to a specific assignee.
        created_by_id: Restrict results to a specific creator.
        related_module: Restrict results to a specific owning module.
        related_entity_id: Restrict results to a specific related entity.
        search: Free-text search applied to the title/description fields.
        due_from: Lower bound (inclusive) on ``due_date``.
        due_to: Upper bound (inclusive) on ``due_date``.
        only_overdue: If ``True``, restrict results to currently overdue
            tasks (due in the past and not in a terminal status).
        page: Page number to retrieve (1-indexed).
        page_size: Number of items to retrieve per page.
        sort_by: Column name to sort by.
        sort_order: Sort direction, either ``"asc"`` or ``"desc"``.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    status: Optional[TaskStatus] = None
    priority: Optional[TaskPriority] = None
    task_type: Optional[TaskType] = None
    assigned_to_id: Optional[int] = Field(default=None, gt=0)
    created_by_id: Optional[int] = Field(default=None, gt=0)
    related_module: Optional[str] = Field(default=None, max_length=50)
    related_entity_id: Optional[str] = Field(default=None, max_length=64)
    search: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Free-text search on title/description.",
    )
    due_from: Optional[datetime] = Field(
        default=None, description="Inclusive lower bound on due_date."
    )
    due_to: Optional[datetime] = Field(
        default=None, description="Inclusive upper bound on due_date."
    )
    only_overdue: bool = Field(
        default=False, description="Restrict results to currently overdue tasks."
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
            "due_date",
            "priority",
            "status",
            "task_type",
            "title",
            "completed_at",
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
    def _validate_date_range(self) -> "TaskFilter":
        """Ensures the provided due-date range is chronologically valid.

        Returns:
            TaskFilter: The validated model instance.

        Raises:
            ValueError: If ``due_from`` is after ``due_to``.
        """
        if self.due_from and self.due_to and self.due_from > self.due_to:
            raise ValueError("due_from must not be after due_to.")
        return self


class TaskStatisticsResponse(BaseModel):
    """Schema representing aggregate statistics over a set of tasks.

    Attributes:
        total_tasks: Total number of tasks in scope.
        by_status: Count of tasks grouped by lifecycle status.
        by_priority: Count of tasks grouped by priority.
        by_type: Count of tasks grouped by task type.
        overdue_count: Number of tasks currently overdue.
        completed_count: Number of tasks with status ``completed``.
        cancelled_count: Number of tasks with status ``cancelled``.
        date_from: Inclusive lower bound of the statistics window, if scoped.
        date_to: Inclusive upper bound of the statistics window, if scoped.
    """

    model_config = ConfigDict(from_attributes=True)

    total_tasks: int = Field(..., ge=0)
    by_status: dict[str, int] = Field(default_factory=dict)
    by_priority: dict[str, int] = Field(default_factory=dict)
    by_type: dict[str, int] = Field(default_factory=dict)
    overdue_count: int = Field(default=0, ge=0)
    completed_count: int = Field(default=0, ge=0)
    cancelled_count: int = Field(default=0, ge=0)
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None