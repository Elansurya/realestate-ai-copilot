"""
backend/app/api/v1/task.py

REST API layer for the Task Management module of the Enterprise Real
Estate AI Copilot CRM.

This router is a thin HTTP adapter: it authenticates the caller,
enforces RBAC, translates query/path/body parameters into calls
against `TaskService`, and translates the service's return values /
exceptions into HTTP responses. It contains no persistence logic
(that is `TaskRepository`) and no business-rule logic of its own
beyond what is delegated to `app.utils.task_validator` and
`TaskService` -- see those modules' docstrings for where each rule
actually lives.

Assumed collaborators (already implemented elsewhere in the project,
per the task brief -- listed here so the interface this router
depends on is explicit and can be reconciled against the real
implementations):

    * ``app.api.deps``:
        - ``get_db_session() -> AsyncSession`` -- FastAPI dependency
          yielding a request-scoped async SQLAlchemy session.
        - ``get_current_user() -> CurrentUser`` -- FastAPI dependency
          that validates the caller's JWT bearer token (via the
          project's existing OAuth2/JWT security scheme) and returns
          the authenticated principal. Raises ``401`` on a missing/
          invalid/expired token.
        - ``require_roles(*roles: str) -> Callable`` -- dependency
          factory returning a FastAPI dependency that raises ``403``
          unless ``current_user.role`` (or ``current_user.roles``) is
          one of ``roles``. Used for RBAC on top of authentication.
    * ``app.services.task_service``:
        - ``TaskService`` -- the class this router delegates to for
          every operation; constructed per-request with the
          request-scoped session (``TaskService(session)``).
        - ``TaskNotFoundError`` -- raised by the service when a
          referenced task id does not exist (or is soft-deleted and
          the operation requires it not to be); mapped to ``404``.
        - ``TaskConflictError`` -- raised by the service for
          state-conflict failures the validator/repository can't
          catch alone (e.g. a concurrent modification); mapped to
          ``409``.

RBAC model assumed (mirrors the project's existing role vocabulary):
    - ``admin``    -- full access, including hard/bulk operations.
    - ``manager``  -- can assign/reassign, complete, cancel, delete,
      restore, and view statistics for their scope.
    - ``agent``    -- can view, create, update, complete, and cancel
      tasks; cannot delete, restore, reassign to arbitrary users, or
      run bulk operations.
    - ``viewer``   -- read-only access (list/get/search/statistics).

Swagger/OpenAPI:
    All routes are tagged ``"Tasks"`` and documented with
    ``summary``/``description`` so they render with meaningful entries
    under FastAPI's auto-generated ``/docs`` and ``/redoc``. Response
    models are declared per-route so the generated schema is precise.

Mirrors: app/api/v1/activity.py (naming/style/error-handling conventions).
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_roles
from app.api.deps import get_db as get_db_session
from app.models.task import TaskPriority, TaskStatus, TaskType
from app.models.user import UserRole
from app.schemas.task import (
    TaskAssign,
    TaskCreate,
    TaskFilter,
    TaskListResponse,
    TaskResponse,
    TaskStatisticsResponse,
    TaskStatusUpdate,
    TaskUpdate,
)
from app.services.task_service import (
    TaskConflictError,
    TaskNotFoundError,
    TaskService,
)
from app.utils.task_validator import TaskValidationError

__all__ = ["router"]

router = APIRouter(prefix="/tasks", tags=["Tasks"])

# ---------------------------------------------------------------------------
# RBAC role groups, named by capability rather than repeated inline so
# the intent of each endpoint's access policy reads clearly at the
# call site.
#
# `require_roles` compares `current_user.role` (a `UserRole` member,
# e.g. `UserRole.SALES_AGENT`) against this tuple with `in`, so the
# tuple must contain `UserRole` members -- not raw strings. `app.
# models.user.UserRole` only defines ADMIN, SALES_MANAGER, and
# SALES_AGENT (no separate "viewer" role), matching the convention
# already established in `app/api/v1/activity.py`.
# ---------------------------------------------------------------------------
_CAN_VIEW = (UserRole.ADMIN, UserRole.SALES_MANAGER, UserRole.SALES_AGENT, "viewer")
_CAN_WRITE = (UserRole.ADMIN, UserRole.SALES_MANAGER, UserRole.SALES_AGENT)
_CAN_ASSIGN = (UserRole.ADMIN, UserRole.SALES_MANAGER)
_CAN_DELETE = (UserRole.ADMIN, UserRole.SALES_MANAGER)
_CAN_BULK = (UserRole.ADMIN, UserRole.SALES_MANAGER)
_CAN_RESTORE = (UserRole.ADMIN,)


# ---------------------------------------------------------------------------
# Shared error translation
# ---------------------------------------------------------------------------
def _raise_for_validation_error(exc: TaskValidationError) -> None:
    """Translates a :class:`TaskValidationError` into a `422` HTTP error.

    Args:
        exc: The validation error raised by `app.utils.task_validator`
            or propagated from `TaskService`.

    Raises:
        HTTPException: Always; status `422` with a structured detail
            payload of `{"code", "field", "message"}`.
    """
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"code": exc.code, "field": exc.field, "message": exc.message},
    ) from exc


def _raise_not_found(task_id: uuid.UUID) -> None:
    """Raises a standardized `404` for a missing/inaccessible task.

    Args:
        task_id: The task id that could not be located.

    Raises:
        HTTPException: Always; status `404`.
    """
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Task '{task_id}' was not found.",
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@router.post(
    "",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a task",
    description=(
        "Creates a new task. `created_by_id` is taken from the "
        "authenticated caller unless explicitly overridden by an "
        "`admin`/`manager` on behalf of a system integration."
    ),
    dependencies=[Depends(require_roles(*_CAN_WRITE))],
)
async def create_task(
    payload: TaskCreate,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
) -> TaskResponse:
    """Creates a new task from a validated `TaskCreate` payload.

    Args:
        payload: The task creation payload.
        session: Request-scoped async database session.
        current_user: The authenticated caller.

    Returns:
        TaskResponse: The newly created task.

    Raises:
        HTTPException: `422` if a business validation rule is
            violated (e.g. an unrecognized `related_module`).
    """
    service = TaskService(session)
    if payload.created_by_id is None:
        payload = payload.model_copy(update={"created_by_id": current_user.id})
    try:
        task = await service.create_task(payload, requester=current_user)
    except TaskValidationError as exc:
        _raise_for_validation_error(exc)
    if session is not None:
        await session.commit()
    return TaskResponse.model_validate(task)


# ---------------------------------------------------------------------------
# List / filter / sort / paginate
# ---------------------------------------------------------------------------
@router.get(
    "",
    response_model=TaskListResponse,
    summary="List tasks",
    description=(
        "Returns a filtered, sorted, paginated page of tasks. Supports "
        "filtering by status/priority/type/assignee/creator/related "
        "entity/due-date range/overdue-only, free-text search over "
        "title and description, and sorting on any indexed column."
    ),
    dependencies=[Depends(require_roles(*_CAN_VIEW))],
)
async def list_tasks(
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
    status_: Optional[TaskStatus] = Query(default=None, alias="status"),
    priority: Optional[TaskPriority] = Query(default=None),
    task_type: Optional[TaskType] = Query(default=None),
    assigned_to_id: Optional[int] = Query(default=None, gt=0),
    created_by_id: Optional[int] = Query(default=None, gt=0),
    related_module: Optional[str] = Query(default=None, max_length=50),
    related_entity_id: Optional[str] = Query(default=None, max_length=64),
    search: Optional[str] = Query(default=None, max_length=255),
    due_from: Optional[str] = Query(default=None),
    due_to: Optional[str] = Query(default=None),
    only_overdue: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    sort_by: str = Query(default="created_at"),
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
) -> TaskListResponse:
    """Lists tasks matching the supplied filters.

    Args:
        session: Request-scoped async database session.
        current_user: The authenticated caller.
        status_: Optional lifecycle status filter (query alias `status`).
        priority: Optional priority filter.
        task_type: Optional task type filter.
        assigned_to_id: Optional assignee filter.
        created_by_id: Optional creator filter.
        related_module: Optional owning-module filter.
        related_entity_id: Optional related-entity filter.
        search: Optional free-text search on title/description.
        due_from: Optional inclusive lower bound on `due_date` (ISO 8601).
        due_to: Optional inclusive upper bound on `due_date` (ISO 8601).
        only_overdue: Restrict to currently overdue tasks.
        page: 1-indexed page number.
        page_size: Number of items per page.
        sort_by: Column name to sort by.
        sort_order: `"asc"` or `"desc"`.

    Returns:
        TaskListResponse: The matching page of tasks plus pagination
        metadata.

    Raises:
        HTTPException: `422` if the filter/sort/pagination parameters
            fail validation (e.g. an unrecognized `sort_by`).
    """
    try:
        task_filter = TaskFilter(
            status=status_,
            priority=priority,
            task_type=task_type,
            assigned_to_id=assigned_to_id,
            created_by_id=created_by_id,
            related_module=related_module,
            related_entity_id=related_entity_id,
            search=search,
            due_from=due_from,
            due_to=due_to,
            only_overdue=only_overdue,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    service = TaskService(session)
    try:
        items, total = await service.list_tasks(task_filter, requester=current_user)
    except TaskValidationError as exc:
        _raise_for_validation_error(exc)

    total_pages = (total + page_size - 1) // page_size if page_size else 0
    return TaskListResponse(
        items=[TaskResponse.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


# ---------------------------------------------------------------------------
# Search (dedicated endpoint, in addition to `search` as a list filter,
# for clients that want a simple "just search" contract)
# ---------------------------------------------------------------------------
@router.get(
    "/search",
    response_model=TaskListResponse,
    summary="Search tasks by free text",
    description=(
        "Case-insensitive substring search over task title and "
        "description. Equivalent to `GET /tasks?search=...` but "
        "exposed as a dedicated endpoint for search-first clients."
    ),
    dependencies=[Depends(require_roles(*_CAN_VIEW))],
)
async def search_tasks(
    q: str = Query(..., min_length=2, max_length=255, description="Search term."),
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    sort_by: str = Query(default="created_at"),
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
) -> TaskListResponse:
    """Searches tasks by a free-text term against title/description.

    Args:
        q: The search term.
        session: Request-scoped async database session.
        current_user: The authenticated caller.
        page: 1-indexed page number.
        page_size: Number of items per page.
        sort_by: Column name to sort by.
        sort_order: `"asc"` or `"desc"`.

    Returns:
        TaskListResponse: The matching page of tasks plus pagination
        metadata.

    Raises:
        HTTPException: `422` if `sort_by` is not an allowed column.
    """
    service = TaskService(session)
    try:
        items, total = await service.search_tasks(
            q,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
            requester=current_user,
        )
    except TaskValidationError as exc:
        _raise_for_validation_error(exc)

    total_pages = (total + page_size - 1) // page_size if page_size else 0
    return TaskListResponse(
        items=[TaskResponse.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
@router.get(
    "/statistics",
    response_model=TaskStatisticsResponse,
    summary="Get task statistics",
    description=(
        "Returns aggregate task counts (by status, priority, type, "
        "overdue, completed, cancelled) optionally scoped to a due-date "
        "window."
    ),
    dependencies=[Depends(require_roles(*_CAN_VIEW))],
)
async def get_task_statistics(
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
    due_from: Optional[str] = Query(default=None),
    due_to: Optional[str] = Query(default=None),
) -> TaskStatisticsResponse:
    """Computes aggregate statistics over the tasks in scope.

    Args:
        session: Request-scoped async database session.
        current_user: The authenticated caller.
        due_from: Optional inclusive lower bound on `due_date` (ISO 8601).
        due_to: Optional inclusive upper bound on `due_date` (ISO 8601).

    Returns:
        TaskStatisticsResponse: The computed aggregate statistics.
    """
    service = TaskService(session)
    return await service.get_statistics(
        due_from=due_from, due_to=due_to, requester=current_user
    )


# ---------------------------------------------------------------------------
# Recent / overdue / reminders convenience listings
# ---------------------------------------------------------------------------
@router.get(
    "/recent",
    response_model=list[TaskResponse],
    summary="List recently created tasks",
    description="Returns the most recently created tasks, newest first.",
    dependencies=[Depends(require_roles(*_CAN_VIEW))],
)
async def get_recent_tasks(
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
    limit: int = Query(default=20, ge=1, le=200),
    assigned_to_id: Optional[int] = Query(default=None, gt=0),
) -> list[TaskResponse]:
    """Fetches the most recently created tasks.

    Args:
        session: Request-scoped async database session.
        current_user: The authenticated caller.
        limit: Maximum number of tasks to return.
        assigned_to_id: Optional assignee filter.

    Returns:
        list[TaskResponse]: The most recent tasks, newest first.
    """
    service = TaskService(session)
    items = await service.get_recent_tasks(
        limit=limit, assigned_to_id=assigned_to_id, requester=current_user
    )
    return [TaskResponse.model_validate(item) for item in items]


@router.get(
    "/overdue",
    response_model=TaskListResponse,
    summary="List overdue tasks",
    description=(
        "Returns tasks past their `due_date` that are not yet in a "
        "terminal status (completed/cancelled)."
    ),
    dependencies=[Depends(require_roles(*_CAN_VIEW))],
)
async def get_overdue_tasks(
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
    assigned_to_id: Optional[int] = Query(default=None, gt=0),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
) -> TaskListResponse:
    """Lists currently overdue tasks.

    Args:
        session: Request-scoped async database session.
        current_user: The authenticated caller.
        assigned_to_id: Optional assignee filter.
        page: 1-indexed page number.
        page_size: Number of items per page.

    Returns:
        TaskListResponse: The matching page of overdue tasks.
    """
    task_filter = TaskFilter(
        assigned_to_id=assigned_to_id,
        only_overdue=True,
        page=page,
        page_size=page_size,
        sort_by="due_date",
        sort_order="asc",
    )
    service = TaskService(session)
    items, total = await service.list_tasks(task_filter, requester=current_user)
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    return TaskListResponse(
        items=[TaskResponse.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get(
    "/reminders/due",
    response_model=list[TaskResponse],
    summary="List tasks with an elapsed reminder",
    description=(
        "Returns non-terminal tasks whose `reminder_time` has elapsed. "
        "Primarily intended for operational/debugging visibility into "
        "the reminder engine (`app.utils.task_scheduler`); the "
        "scheduler itself polls the repository directly rather than "
        "this endpoint."
    ),
    dependencies=[Depends(require_roles(*_CAN_ASSIGN))],
)
async def get_due_reminders(
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[TaskResponse]:
    """Fetches tasks whose reminder time has elapsed.

    Args:
        session: Request-scoped async database session.
        current_user: The authenticated caller.
        limit: Maximum number of tasks to return.

    Returns:
        list[TaskResponse]: Tasks with an elapsed reminder, ordered by
        `reminder_time` ascending.
    """
    service = TaskService(session)
    items = await service.get_due_reminders(limit=limit, requester=current_user)
    return [TaskResponse.model_validate(item) for item in items]


# ---------------------------------------------------------------------------
# Bulk operations (declared before "/{task_id}" so the literal path
# segments below are not shadowed by the UUID path parameter route)
# ---------------------------------------------------------------------------
@router.patch(
    "/bulk",
    response_model=dict,
    summary="Bulk-update a set of tasks",
    description=(
        "Applies the same set of descriptive-field updates to a bounded "
        "set of tasks. Status, assignment, and soft-delete fields are "
        "excluded -- use the dedicated single-task endpoints for those, "
        "since they carry their own transition/permission rules."
    ),
    dependencies=[Depends(require_roles(*_CAN_BULK))],
)
async def bulk_update_tasks(
    ids: list[uuid.UUID],
    data: TaskUpdate,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
) -> dict:
    """Bulk-updates descriptive fields on a set of tasks.

    Args:
        ids: The primary keys of the tasks to update.
        data: The descriptive-field update payload, applied identically
            to every matched row.
        session: Request-scoped async database session.
        current_user: The authenticated caller.

    Returns:
        dict: `{"updated_count": int}`.

    Raises:
        HTTPException: `422` if `ids` is empty/oversized or `data`
            contains a disallowed field.
    """
    service = TaskService(session)
    try:
        updated_count = await service.bulk_update_tasks(
            ids, data, requester=current_user
        )
    except TaskValidationError as exc:
        _raise_for_validation_error(exc)
    if session is not None:
        await session.commit()
    return {"updated_count": updated_count}


@router.post(
    "/bulk/delete",
    response_model=dict,
    summary="Bulk soft-delete a set of tasks",
    description="Soft-deletes a bounded set of tasks by id.",
    dependencies=[Depends(require_roles(*_CAN_BULK))],
)
async def bulk_delete_tasks(
    ids: list[uuid.UUID],
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
) -> dict:
    """Bulk soft-deletes a set of tasks.

    Args:
        ids: The primary keys of the tasks to soft-delete.
        session: Request-scoped async database session.
        current_user: The authenticated caller.

    Returns:
        dict: `{"deleted_count": int}`.

    Raises:
        HTTPException: `422` if `ids` is empty or oversized.
    """
    service = TaskService(session)
    try:
        deleted_count = await service.bulk_soft_delete_tasks(
            ids, requester=current_user
        )
    except TaskValidationError as exc:
        _raise_for_validation_error(exc)
    if session is not None:
        await session.commit()
    return {"deleted_count": deleted_count}


# ---------------------------------------------------------------------------
# Single-task retrieval / update
# ---------------------------------------------------------------------------
@router.get(
    "/{task_id}",
    response_model=TaskResponse,
    summary="Get a task by id",
    description="Fetches a single task by its primary key.",
    dependencies=[Depends(require_roles(*_CAN_VIEW))],
)
async def get_task(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
) -> TaskResponse:
    """Fetches a single task by id.

    Args:
        task_id: The task's primary key.
        session: Request-scoped async database session.
        current_user: The authenticated caller.

    Returns:
        TaskResponse: The matching task.

    Raises:
        HTTPException: `404` if no matching, non-deleted task exists.
    """
    service = TaskService(session)
    task = await service.get_task(task_id, requester=current_user)
    if task is None:
        _raise_not_found(task_id)
    return TaskResponse.model_validate(task)


@router.patch(
    "/{task_id}",
    response_model=TaskResponse,
    summary="Update a task's descriptive fields",
    description=(
        "Partially updates a task's descriptive fields (title, "
        "description, type, priority, due date, reminder time, "
        "metadata). Status transitions and assignment are handled by "
        "their own dedicated endpoints."
    ),
    dependencies=[Depends(require_roles(*_CAN_WRITE))],
)
async def update_task(
    task_id: uuid.UUID,
    payload: TaskUpdate,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
) -> TaskResponse:
    """Applies a partial update to a task's descriptive fields.

    Args:
        task_id: The task's primary key.
        payload: The fields to update.
        session: Request-scoped async database session.
        current_user: The authenticated caller.

    Returns:
        TaskResponse: The updated task.

    Raises:
        HTTPException: `404` if the task does not exist; `422` if a
            business validation rule is violated.
    """
    service = TaskService(session)
    try:
        task = await service.update_task(
            task_id, payload, requester=current_user
        )
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except TaskValidationError as exc:
        _raise_for_validation_error(exc)
    if session is not None:
        await session.commit()
    return TaskResponse.model_validate(task)


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------
@router.post(
    "/{task_id}/assign",
    response_model=TaskResponse,
    summary="Assign or reassign a task",
    description=(
        "Sets (or clears, via `assigned_to_id: null`) the assignee of a "
        "task. Used for both the initial assignment and any subsequent "
        "reassignment."
    ),
    dependencies=[Depends(require_roles(*_CAN_ASSIGN))],
)
async def assign_task(
    task_id: uuid.UUID,
    payload: TaskAssign,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
) -> TaskResponse:
    """Assigns or reassigns a task.

    Args:
        task_id: The task's primary key.
        payload: The assignment payload.
        session: Request-scoped async database session.
        current_user: The authenticated caller.

    Returns:
        TaskResponse: The updated task.

    Raises:
        HTTPException: `404` if the task does not exist; `422` if the
            assignment target is invalid or not permitted.
    """
    service = TaskService(session)
    try:
        task = await service.assign_task(
            task_id, payload, requester=current_user
        )
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except TaskValidationError as exc:
        _raise_for_validation_error(exc)
    if session is not None:
        await session.commit()
    return TaskResponse.model_validate(task)


# ---------------------------------------------------------------------------
# Lifecycle transitions
# ---------------------------------------------------------------------------
@router.post(
    "/{task_id}/status",
    response_model=TaskResponse,
    summary="Transition a task's lifecycle status",
    description=(
        "Applies a non-terminal lifecycle transition (e.g. start, "
        "hold, resume). Completion and cancellation are handled by "
        "their own dedicated endpoints, since each carries additional "
        "side effects (`completed_at`/`completed_by_id` bookkeeping)."
    ),
    dependencies=[Depends(require_roles(*_CAN_WRITE))],
)
async def update_task_status(
    task_id: uuid.UUID,
    payload: TaskStatusUpdate,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
) -> TaskResponse:
    """Applies a lifecycle status transition to a task.

    Args:
        task_id: The task's primary key.
        payload: The status transition payload.
        session: Request-scoped async database session.
        current_user: The authenticated caller.

    Returns:
        TaskResponse: The updated task.

    Raises:
        HTTPException: `404` if the task does not exist; `422` if the
            requested transition is not legal from the task's current
            status (e.g. targeting `completed`/`cancelled` here rather
            than via the dedicated endpoints, or the task is already
            terminal).
    """
    service = TaskService(session)
    try:
        task = await service.update_status(
            task_id, payload, requester=current_user
        )
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except TaskValidationError as exc:
        _raise_for_validation_error(exc)
    if session is not None:
        await session.commit()
    return TaskResponse.model_validate(task)


@router.post(
    "/{task_id}/complete",
    response_model=TaskResponse,
    summary="Complete a task",
    description=(
        "Marks a task as completed, stamping `completed_at` and "
        "`completed_by_id` (the authenticated caller)."
    ),
    dependencies=[Depends(require_roles(*_CAN_WRITE))],
)
async def complete_task(
    task_id: uuid.UUID,
    payload: Optional[TaskStatusUpdate] = None,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
) -> TaskResponse:
    """Marks a task as completed.

    Args:
        task_id: The task's primary key.
        payload: Optional accompanying comment; `status` is ignored
            and forced to `completed` regardless of what is supplied.
        session: Request-scoped async database session.
        current_user: The authenticated caller.

    Returns:
        TaskResponse: The completed task.

    Raises:
        HTTPException: `404` if the task does not exist; `422` if the
            task is already in a terminal status.
    """
    service = TaskService(session)
    comment = payload.comment if payload else None
    try:
        task = await service.complete_task(
            task_id,
            completed_by_id=current_user.id,
            comment=comment,
            requester=current_user,
        )
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except TaskValidationError as exc:
        _raise_for_validation_error(exc)
    if session is not None:
        await session.commit()
    return TaskResponse.model_validate(task)


@router.post(
    "/{task_id}/cancel",
    response_model=TaskResponse,
    summary="Cancel a task",
    description="Marks a task as cancelled.",
    dependencies=[Depends(require_roles(*_CAN_WRITE))],
)
async def cancel_task(
    task_id: uuid.UUID,
    payload: Optional[TaskStatusUpdate] = None,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
) -> TaskResponse:
    """Marks a task as cancelled.

    Args:
        task_id: The task's primary key.
        payload: Optional accompanying cancellation comment/reason.
        session: Request-scoped async database session.
        current_user: The authenticated caller.

    Returns:
        TaskResponse: The cancelled task.

    Raises:
        HTTPException: `404` if the task does not exist; `422` if the
            task is already completed or already cancelled.
    """
    service = TaskService(session)
    comment = payload.comment if payload else None
    try:
        task = await service.cancel_task(
            task_id, comment=comment, requester=current_user
        )
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except TaskValidationError as exc:
        _raise_for_validation_error(exc)
    if session is not None:
        await session.commit()
    return TaskResponse.model_validate(task)


# ---------------------------------------------------------------------------
# Soft delete / restore
# ---------------------------------------------------------------------------
@router.delete(
    "/{task_id}",
    response_model=TaskResponse,
    summary="Soft-delete a task",
    description="Marks a task as soft-deleted; does not remove the row.",
    dependencies=[Depends(require_roles(*_CAN_DELETE))],
)
async def delete_task(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
) -> TaskResponse:
    """Soft-deletes a task.

    Args:
        task_id: The task's primary key.
        session: Request-scoped async database session.
        current_user: The authenticated caller.

    Returns:
        TaskResponse: The soft-deleted task.

    Raises:
        HTTPException: `404` if no matching, non-deleted task exists.
    """
    service = TaskService(session)
    try:
        task = await service.soft_delete_task(task_id, requester=current_user)
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    if session is not None:
        await session.commit()
    return TaskResponse.model_validate(task)


@router.post(
    "/{task_id}/restore",
    response_model=TaskResponse,
    summary="Restore a soft-deleted task",
    description="Reverses a soft-delete on a task.",
    dependencies=[Depends(require_roles(*_CAN_RESTORE))],
)
async def restore_task(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
) -> TaskResponse:
    """Restores a soft-deleted task.

    Args:
        task_id: The task's primary key.
        session: Request-scoped async database session.
        current_user: The authenticated caller.

    Returns:
        TaskResponse: The restored task.

    Raises:
        HTTPException: `404` if no task with that id exists at all;
            `422` if the task is not currently soft-deleted.
    """
    service = TaskService(session)
    try:
        task = await service.restore_task(task_id, requester=current_user)
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except TaskValidationError as exc:
        _raise_for_validation_error(exc)
    if session is not None:
        await session.commit()
    return TaskResponse.model_validate(task)


# ---------------------------------------------------------------------------
# Conflict handling shared by any route above that may surface a
# concurrent-modification failure from the service layer. FastAPI
# resolves exception handlers at the application level, but routers
# may also be defensive at the call site; where a route above does
# not already catch `TaskConflictError` explicitly, it propagates to
# the app-level handler registered for it (assumed configured in
# `app.main`, mirroring how `TaskNotFoundError`/`TaskValidationError`
# are otherwise handled inline in this file for per-route detail
# messages).
# ---------------------------------------------------------------------------