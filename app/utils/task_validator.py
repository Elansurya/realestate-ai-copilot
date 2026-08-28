"""
backend/app/utils/task_validator.py

Validation Engine for the Task Management module.

Pydantic schemas (`app.schemas.task`) already enforce structural/field
level validation (types, lengths, simple cross-field rules such as
"reminder_time <= due_date"). This module is the next layer up: it
enforces *business* rules that require knowledge of the task's
*current* persisted state, the caller's identity/role, or
cross-cutting invariants that are awkward to express in a schema
(e.g. "you cannot complete a task that is already cancelled").

Design:
    - Every check either returns silently (valid) or raises
      :class:`TaskValidationError`. It never returns a boolean, so
      call sites cannot accidentally ignore a failed check.
    - This module raises framework-agnostic exceptions only. It has
      no dependency on FastAPI. The API layer
      (`app.api.v1.task`) is responsible for catching
      :class:`TaskValidationError` and translating it into an
      HTTP response (see the ``task_validation_exception_handler``
      pattern used there).
    - Functions are pure / side-effect free: they inspect the values
      handed to them and raise or return; they never touch the
      database themselves. Callers (typically ``TaskService``) are
      responsible for loading whatever state is needed.

Mirrors: app/utils/activity_validator.py (naming/style conventions).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence

from app.models.task import TaskPriority, TaskStatus, TaskType

__all__ = [
    "TaskValidationError",
    "ALLOWED_RELATED_MODULES",
    "MAX_BULK_OPERATION_SIZE",
    "MAX_TITLE_LENGTH",
    "validate_status_transition",
    "validate_assignment_target",
    "validate_self_assignment_policy",
    "validate_due_date_not_absurdly_past",
    "validate_reminder_lead_time",
    "validate_related_entity_reference",
    "validate_bulk_ids",
    "validate_bulk_update_payload",
    "validate_pagination_bounds",
    "validate_sort_field",
    "validate_search_term",
    "validate_completion_allowed",
    "validate_cancellation_allowed",
    "validate_restore_allowed",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Owning modules a Task is permitted to be linked to via
#: ``related_module`` / ``related_entity_id``. Kept as an explicit
#: allow-list (rather than trusting free text) so a typo or a
#: not-yet-integrated module cannot silently create orphaned links.
ALLOWED_RELATED_MODULES: frozenset[str] = frozenset(
    {
        "lead",
        "customer",
        "property",
        "booking",
        "payment",
        "workflow",
        "document",
    }
)

#: Maximum number of ids accepted by any bulk operation in a single
#: request, to bound worst-case query/row-lock time.
MAX_BULK_OPERATION_SIZE: int = 500

#: Maximum accepted page size (mirrors `TaskFilter.page_size` le=200,
#: re-asserted here so this module has no hidden coupling to the
#: schema's own bound).
MAX_PAGE_SIZE: int = 200

#: Mirrors `TaskBase.title` max_length, re-asserted for defense in depth.
MAX_TITLE_LENGTH: int = 255

#: Minimum non-blank length of a free-text search term. A single
#: character search against `ILIKE '%x%'` is expensive and rarely
#: useful, so it is rejected rather than silently executed.
MIN_SEARCH_TERM_LENGTH: int = 2

#: Columns callers may sort listings by. Mirrors
#: `TaskFilter._ALLOWED_SORT_FIELDS`; re-checked here because this
#: module is also used by call sites that bypass `TaskFilter`
#: (e.g. `get_recent_tasks`, statistics endpoints with sort params).
ALLOWED_SORT_FIELDS: frozenset[str] = frozenset(
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

#: Legal lifecycle transitions, keyed by current status. A status is
#: always a legal "transition" to itself (no-op update) except for
#: the two terminal statuses, which are locked once reached.
_TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.CANCELLED}
)

_ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {
            TaskStatus.PENDING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.ON_HOLD,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.IN_PROGRESS: frozenset(
        {
            TaskStatus.IN_PROGRESS,
            TaskStatus.ON_HOLD,
            TaskStatus.COMPLETED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.ON_HOLD: frozenset(
        {
            TaskStatus.ON_HOLD,
            TaskStatus.IN_PROGRESS,
            TaskStatus.CANCELLED,
        }
    ),
    # Terminal: no outbound transitions via TaskStatusUpdate. Reopening
    # a completed/cancelled task is intentionally not modeled as a
    # status transition (it would violate
    # ck_tasks_completed_at_consistency semantics); it must go through
    # an explicit "restore"-style flow at the service layer instead.
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class TaskValidationError(Exception):
    """Raised when a Task-related business rule is violated.

    Attributes:
        message: Human-readable description of the violation.
        field: Optional name of the offending field/parameter, useful
            for building structured API error responses (e.g.
            ``{"field": "status", "message": "..."}``).
        code: Optional short machine-readable error code, useful for
            client-side branching without string-matching `message`.
    """

    def __init__(
        self,
        message: str,
        *,
        field: Optional[str] = None,
        code: Optional[str] = None,
    ) -> None:
        """Initializes the validation error.

        Args:
            message: Human-readable description of the violation.
            field: Optional offending field/parameter name.
            code: Optional short machine-readable error code.
        """
        super().__init__(message)
        self.message = message
        self.field = field
        self.code = code


# ---------------------------------------------------------------------------
# Lifecycle transition validation
# ---------------------------------------------------------------------------
def validate_status_transition(
    current_status: TaskStatus, target_status: TaskStatus
) -> None:
    """Validates that moving from `current_status` to `target_status` is legal.

    Args:
        current_status: The task's current lifecycle status.
        target_status: The requested target lifecycle status.

    Raises:
        TaskValidationError: If the transition is not permitted (e.g.
            attempting to move a completed or cancelled task to any
            other status via a generic status update).
    """
    allowed = _ALLOWED_TRANSITIONS.get(current_status, frozenset())
    if target_status not in allowed:
        if current_status in _TERMINAL_STATUSES:
            raise TaskValidationError(
                f"Task is already '{current_status.value}' and its status "
                "is locked; terminal tasks cannot be transitioned further.",
                field="status",
                code="terminal_status_locked",
            )
        raise TaskValidationError(
            f"Cannot transition task from '{current_status.value}' to "
            f"'{target_status.value}'.",
            field="status",
            code="illegal_status_transition",
        )


def validate_completion_allowed(current_status: TaskStatus) -> None:
    """Validates that a task in `current_status` may be marked completed.

    Args:
        current_status: The task's current lifecycle status.

    Raises:
        TaskValidationError: If the task is already in a terminal status.
    """
    if current_status in _TERMINAL_STATUSES:
        raise TaskValidationError(
            f"Task is already '{current_status.value}' and cannot be "
            "completed.",
            field="status",
            code="terminal_status_locked",
        )


def validate_cancellation_allowed(current_status: TaskStatus) -> None:
    """Validates that a task in `current_status` may be cancelled.

    Args:
        current_status: The task's current lifecycle status.

    Raises:
        TaskValidationError: If the task is already completed or
            already cancelled.
    """
    if current_status == TaskStatus.COMPLETED:
        raise TaskValidationError(
            "A completed task cannot be cancelled.",
            field="status",
            code="terminal_status_locked",
        )
    if current_status == TaskStatus.CANCELLED:
        raise TaskValidationError(
            "Task is already cancelled.",
            field="status",
            code="terminal_status_locked",
        )


def validate_restore_allowed(is_deleted: bool) -> None:
    """Validates that a task may be restored from a soft-deleted state.

    Args:
        is_deleted: The task's current `is_deleted` flag.

    Raises:
        TaskValidationError: If the task is not currently soft-deleted.
    """
    if not is_deleted:
        raise TaskValidationError(
            "Task is not deleted; nothing to restore.",
            field="is_deleted",
            code="not_deleted",
        )


# ---------------------------------------------------------------------------
# Assignment validation
# ---------------------------------------------------------------------------
def validate_assignment_target(
    assigned_to_id: Optional[int],
    *,
    assignable_user_ids: Optional[Iterable[int]] = None,
) -> None:
    """Validates the target of an assignment/reassignment operation.

    Args:
        assigned_to_id: The user id being assigned, or ``None`` to
            unassign the task.
        assignable_user_ids: Optional explicit set of user ids the
            caller is permitted to assign this task to (e.g. members
            of the requester's team, computed by the service layer
            from an RBAC/org-chart lookup). If ``None``, no membership
            restriction is enforced here (the caller has none, or has
            already enforced it upstream).

    Raises:
        TaskValidationError: If `assigned_to_id` is not a positive
            integer, or if an `assignable_user_ids` allow-list is
            supplied and `assigned_to_id` is not a member of it.
    """
    if assigned_to_id is None:
        return
    if assigned_to_id <= 0:
        raise TaskValidationError(
            "assigned_to_id must be a positive integer.",
            field="assigned_to_id",
            code="invalid_assignee",
        )
    if assignable_user_ids is not None and assigned_to_id not in set(
        assignable_user_ids
    ):
        raise TaskValidationError(
            "You are not permitted to assign this task to the requested "
            "user.",
            field="assigned_to_id",
            code="assignee_not_permitted",
        )


def validate_self_assignment_policy(
    *,
    assigned_to_id: Optional[int],
    requester_id: int,
    allow_self_assign: bool,
) -> None:
    """Validates a requester's ability to assign a task to themself.

    Some organizations restrict self-assignment (e.g. junior agents
    must be assigned by a manager) via an RBAC policy flag resolved
    upstream by the service layer.

    Args:
        assigned_to_id: The user id being assigned, or ``None``.
        requester_id: The id of the user performing the assignment.
        allow_self_assign: Whether the requester's role is permitted
            to self-assign tasks.

    Raises:
        TaskValidationError: If the requester is assigning the task to
            themself and self-assignment is not permitted.
    """
    if (
        assigned_to_id is not None
        and assigned_to_id == requester_id
        and not allow_self_assign
    ):
        raise TaskValidationError(
            "Self-assignment is not permitted for your role.",
            field="assigned_to_id",
            code="self_assignment_forbidden",
        )


# ---------------------------------------------------------------------------
# Date / time validation
# ---------------------------------------------------------------------------
def validate_due_date_not_absurdly_past(
    due_date: Optional[datetime], *, grace_seconds: int = 60
) -> None:
    """Validates that a *newly supplied* due date is not obviously stale.

    This is intentionally lenient (a small grace window) since clock
    skew between client and server, or a request that was queued
    briefly, should not cause spurious rejections. It exists only to
    catch clearly wrong input (e.g. a date typo years in the past),
    not to police legitimately overdue tasks created via backfill.

    Args:
        due_date: The due date being set, if any.
        grace_seconds: Allowed clock-skew tolerance, in seconds.

    Raises:
        TaskValidationError: If `due_date` is timezone-naive, or if it
            falls further in the past than `grace_seconds` allows.
    """
    if due_date is None:
        return
    if due_date.tzinfo is None:
        raise TaskValidationError(
            "due_date must be timezone-aware.",
            field="due_date",
            code="naive_datetime",
        )
    now = datetime.now(timezone.utc)
    if (now - due_date).total_seconds() > grace_seconds and due_date < now:
        raise TaskValidationError(
            "due_date must not be set in the past.",
            field="due_date",
            code="due_date_in_past",
        )


def validate_reminder_lead_time(
    due_date: Optional[datetime],
    reminder_time: Optional[datetime],
    *,
    minimum_lead: Optional[int] = None,
) -> None:
    """Validates the gap between a reminder and its due date, if both are set.

    The schema layer already guarantees `reminder_time <= due_date`.
    This adds an optional business rule: a reminder that fires too
    close to (or exactly at) the deadline is rarely actionable.

    Args:
        due_date: The task's due date, if any.
        reminder_time: The task's reminder time, if any.
        minimum_lead: Minimum required lead time, in seconds, between
            `reminder_time` and `due_date`. If ``None``, no minimum is
            enforced (caller/deployment has not opted into this rule).

    Raises:
        TaskValidationError: If both timestamps are set, a minimum
            lead time is configured, and the actual gap is smaller.
    """
    if minimum_lead is None or due_date is None or reminder_time is None:
        return
    gap = (due_date - reminder_time).total_seconds()
    if gap < minimum_lead:
        raise TaskValidationError(
            f"reminder_time must be at least {minimum_lead} seconds before "
            "due_date.",
            field="reminder_time",
            code="reminder_lead_too_short",
        )


# ---------------------------------------------------------------------------
# Related-entity validation
# ---------------------------------------------------------------------------
def validate_related_entity_reference(
    related_module: Optional[str], related_entity_id: Optional[str]
) -> None:
    """Validates a `related_module` / `related_entity_id` pair.

    The schema layer already guarantees the pair is either both-set
    or both-unset. This adds the allow-list check on `related_module`,
    since the column is free text at the database level.

    Args:
        related_module: The owning module name, if any.
        related_entity_id: The related entity's id (as text), if any.

    Raises:
        TaskValidationError: If `related_module` is set but is not a
            recognized module name.
    """
    if related_module is None:
        return
    normalized = related_module.strip().lower()
    if normalized not in ALLOWED_RELATED_MODULES:
        raise TaskValidationError(
            f"related_module '{related_module}' is not a recognized "
            f"module. Allowed values: {sorted(ALLOWED_RELATED_MODULES)}.",
            field="related_module",
            code="unknown_related_module",
        )


# ---------------------------------------------------------------------------
# Bulk-operation validation
# ---------------------------------------------------------------------------
def validate_bulk_ids(ids: Sequence[object]) -> None:
    """Validates the size and shape of an id list for a bulk operation.

    Args:
        ids: The primary keys supplied for a bulk operation.

    Raises:
        TaskValidationError: If `ids` is empty or exceeds
            :data:`MAX_BULK_OPERATION_SIZE`.
    """
    if not ids:
        raise TaskValidationError(
            "At least one task id must be supplied.",
            field="ids",
            code="empty_id_list",
        )
    if len(ids) > MAX_BULK_OPERATION_SIZE:
        raise TaskValidationError(
            f"A maximum of {MAX_BULK_OPERATION_SIZE} task ids may be "
            f"supplied per bulk operation (received {len(ids)}).",
            field="ids",
            code="bulk_size_exceeded",
        )


def validate_bulk_update_payload(data: dict) -> None:
    """Validates the update payload used by a bulk-update operation.

    Bulk updates intentionally do not accept status or assignment
    changes (those carry their own transition/permission rules and
    must go through the dedicated single-task flows), nor primary-key
    or audit columns.

    Args:
        data: Mapping of column names to new values, as would be
            passed to `TaskRepository.bulk_update`.

    Raises:
        TaskValidationError: If `data` is empty, or contains any
            disallowed key.
    """
    if not data:
        raise TaskValidationError(
            "Bulk update payload must not be empty.",
            field="data",
            code="empty_update_payload",
        )
    disallowed = {
        "id",
        "status",
        "assigned_to_id",
        "completed_at",
        "completed_by_id",
        "is_deleted",
        "deleted_at",
        "created_at",
        "created_by_id",
    }
    offending = disallowed.intersection(data.keys())
    if offending:
        raise TaskValidationError(
            f"Fields {sorted(offending)} cannot be changed via bulk "
            "update; use the dedicated assignment/status/delete "
            "endpoints instead.",
            field="data",
            code="disallowed_bulk_field",
        )


# ---------------------------------------------------------------------------
# Listing / search validation
# ---------------------------------------------------------------------------
def validate_pagination_bounds(page: int, page_size: int) -> None:
    """Validates pagination parameters.

    Args:
        page: The requested 1-indexed page number.
        page_size: The requested number of items per page.

    Raises:
        TaskValidationError: If `page` is less than 1, `page_size` is
            less than 1, or `page_size` exceeds :data:`MAX_PAGE_SIZE`.
    """
    if page < 1:
        raise TaskValidationError(
            "page must be >= 1.", field="page", code="invalid_page"
        )
    if page_size < 1:
        raise TaskValidationError(
            "page_size must be >= 1.",
            field="page_size",
            code="invalid_page_size",
        )
    if page_size > MAX_PAGE_SIZE:
        raise TaskValidationError(
            f"page_size must not exceed {MAX_PAGE_SIZE}.",
            field="page_size",
            code="page_size_exceeded",
        )


def validate_sort_field(sort_by: str) -> None:
    """Validates that a requested sort column is on the allow-list.

    Args:
        sort_by: The requested sort column name.

    Raises:
        TaskValidationError: If `sort_by` is not an allowed column.
            Rejecting rather than silently falling back prevents a
            caller from believing a sort was applied when it was not.
    """
    if sort_by not in ALLOWED_SORT_FIELDS:
        raise TaskValidationError(
            f"sort_by must be one of {sorted(ALLOWED_SORT_FIELDS)}.",
            field="sort_by",
            code="invalid_sort_field",
        )


def validate_search_term(search: Optional[str]) -> None:
    """Validates a free-text search term.

    Args:
        search: The raw search term, if any.

    Raises:
        TaskValidationError: If `search` is supplied but, after
            stripping, is shorter than :data:`MIN_SEARCH_TERM_LENGTH`.
    """
    if search is None:
        return
    if len(search.strip()) < MIN_SEARCH_TERM_LENGTH:
        raise TaskValidationError(
            f"search must be at least {MIN_SEARCH_TERM_LENGTH} characters.",
            field="search",
            code="search_term_too_short",
        )