"""
backend/app/api/v1/workflow.py

HTTP API layer for the Workflow module (FastAPI).

- Authentication: JWT bearer tokens, verified via `get_current_user`
  (assumed to already exist in `app.core.security`, consistent with
  the rest of the project).
- Authorization: role-based access control via `require_roles(...)`
  (assumed to already exist alongside `get_current_user`).
- Swagger: every route declares `response_model`, `status_code`,
  `summary`, `description` and an explicit `tags` grouping so the
  generated OpenAPI docs are complete and readable.
- Business logic lives entirely in `WorkflowEngine` / `ApprovalEngine`
  (`app.utils.workflow_engine`); this module only wires HTTP <->
  engine calls. Domain exceptions raised by the engines are expected
  to be translated to HTTP responses by a global exception handler
  registered on the FastAPI app (not part of this file).

NOTE: `get_db` is assumed to already exist at `app.db.session` as an
async-session FastAPI dependency, consistent with the rest of the
project.
"""

from __future__ import annotations

import uuid
from typing import Any, List, Optional

from typing_extensions import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_roles
from app.core.exceptions import ConflictException, NotFoundException
from app.db.session import get_db
from app.models.user import User, UserRole
from app.repositories.workflow_repository import (
    PageResult,
    PaginationParams,
    SortParams,
    WorkflowFilterParams,
    WorkflowRepository,
)
from app.schemas.workflow import (
    ApprovalStatus,
    WorkflowApprovalRead,
    WorkflowCreate,
    WorkflowPriority,
    WorkflowRead,
    WorkflowStatus,
    WorkflowStepCreate,
    WorkflowStepRead,
    WorkflowStepStatus,
    WorkflowStepUpdate,
    WorkflowStepWithApprovals,
    WorkflowUpdate,
    WorkflowWithSteps,
)
from app.utils.workflow_engine import ApprovalEngine, WorkflowEngine
from app.utils.workflow_validator import WorkflowValidator

router = APIRouter(prefix="/workflows", tags=["Workflows"])

#: `get_current_user`-authenticated principal, declared once and reused
#: as a parameter annotation throughout this module (mirrors the local
#: `CurrentUser` alias already used by `app.api.v1.notification`).
CurrentUser = Annotated[User, Depends(get_current_user)]

# ---------------------------------------------------------------------------
# RBAC roles used by this module
# ---------------------------------------------------------------------------
# `require_roles` takes `UserRole` members, not raw strings; `app.models.
# user.UserRole` only defines ADMIN, SALES_MANAGER, and SALES_AGENT -- there
# is no separate approver/member role, so those are folded into the closest
# existing role.
ROLE_ADMIN = UserRole.ADMIN
ROLE_MANAGER = UserRole.SALES_MANAGER
ROLE_APPROVER = UserRole.SALES_MANAGER
ROLE_MEMBER = UserRole.SALES_AGENT

_CAN_WRITE = require_roles(ROLE_ADMIN, ROLE_MANAGER)
_CAN_DELETE = require_roles(ROLE_ADMIN)
_CAN_EXECUTE = require_roles(ROLE_ADMIN, ROLE_MANAGER)
_CAN_DECIDE_APPROVAL = require_roles(ROLE_ADMIN, ROLE_MANAGER, ROLE_APPROVER)
_CAN_READ = require_roles(ROLE_ADMIN, ROLE_MANAGER, ROLE_APPROVER, ROLE_MEMBER)


# ---------------------------------------------------------------------------
# Local request/response models (not covered by app.schemas.workflow)
# ---------------------------------------------------------------------------
class CommentCreate(BaseModel):
    message: str = Field(..., min_length=1, max_length=5_000)


class CommentRead(BaseModel):
    id: str
    author_id: int
    message: str
    created_at: str


class AssignmentRequest(BaseModel):
    assignee_id: int = Field(..., gt=0)


class CancelWorkflowRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=10_000)


class StepOutputRequest(BaseModel):
    output_data: Optional[dict[str, Any]] = None


class StepFailureRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=10_000)


class ApprovalRequestCreate(BaseModel):
    approver_id: int = Field(..., gt=0)


class ApprovalDecisionRequest(BaseModel):
    decision_notes: Optional[str] = Field(None, max_length=10_000)


class ApprovalEscalationRequest(BaseModel):
    escalated_to_id: int = Field(..., gt=0)
    decision_notes: Optional[str] = Field(None, max_length=10_000)


class PaginatedWorkflows(BaseModel):
    items: List[WorkflowRead]
    total: int
    page: int
    page_size: int
    total_pages: int


class PaginatedApprovals(BaseModel):
    items: List[WorkflowApprovalRead]
    total: int
    page: int
    page_size: int
    total_pages: int


class WorkflowStatistics(BaseModel):
    total: int
    by_status: dict[str, int]
    by_priority: dict[str, int]
    overdue_count: int
    average_completion_seconds: Optional[float]
    average_completion_hours: Optional[float]


class HistoryEntry(BaseModel):
    type: str
    id: uuid.UUID
    status: str
    timestamp: Optional[str]


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
def get_workflow_repository(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRepository:
    return WorkflowRepository(db)


def get_workflow_engine(
    repository: Annotated[WorkflowRepository, Depends(get_workflow_repository)],
) -> WorkflowEngine:
    return WorkflowEngine(repository, WorkflowValidator())


def get_approval_engine(
    repository: Annotated[WorkflowRepository, Depends(get_workflow_repository)],
    workflow_engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
) -> ApprovalEngine:
    return ApprovalEngine(repository, WorkflowValidator(), workflow_engine)


def _to_page_response(page: PageResult) -> dict[str, Any]:
    total_pages = (
        (page.total + page.page_size - 1) // page.page_size
        if page.page_size > 0
        else 0
    )
    return {
        "items": page.items,
        "total": page.total,
        "page": page.page,
        "page_size": page.page_size,
        "total_pages": total_pages,
    }


# ---------------------------------------------------------------------------
# Workflow: CRUD
# ---------------------------------------------------------------------------
@router.post(
    "",
    response_model=WorkflowWithSteps,
    status_code=status.HTTP_201_CREATED,
    summary="Create a workflow",
    description="Create a new workflow, optionally with an initial set of steps.",
    dependencies=[Depends(_CAN_WRITE)],
)
async def create_workflow(
    payload: WorkflowCreate,
    repository: Annotated[WorkflowRepository, Depends(get_workflow_repository)],
    current_user: CurrentUser,
) -> Any:
    WorkflowValidator.validate_workflow_name(payload.name)
    WorkflowValidator.validate_entity_reference(payload.entity_type, payload.entity_id)
    WorkflowValidator.validate_priority(payload.priority.value)
    if payload.steps:
        WorkflowValidator.validate_step_order_sequence(
            [step.step_order for step in payload.steps]
        )

    values = payload.model_dump(exclude={"steps", "meta_data"})
    values["meta_data"] = payload.meta_data
    values["priority"] = payload.priority.value
    values["created_by_id"] = current_user.id
    values["updated_by_id"] = current_user.id

    workflow = await repository.create_workflow(values)

    if payload.steps:
        step_values = []
        for step_payload in payload.steps:
            step_dict = step_payload.model_dump(exclude={"workflow_id"})
            step_dict["workflow_id"] = workflow.id
            step_dict["created_by_id"] = current_user.id
            step_dict["updated_by_id"] = current_user.id
            step_values.append(step_dict)
        await repository.bulk_create_steps(step_values)

    return await repository.get_workflow_with_steps(workflow.id)


@router.get(
    "",
    response_model=PaginatedWorkflows,
    summary="List / search / filter workflows",
    description=(
        "Paginated, sortable, filterable listing of workflows. Pass "
        "`search` to full-text match against name/description/type/entity_id."
    ),
    dependencies=[Depends(_CAN_READ)],
)
async def list_workflows(
    repository: Annotated[WorkflowRepository, Depends(get_workflow_repository)],
    search: Optional[str] = Query(None, description="Free-text search term."),
    status_filter: Optional[List[WorkflowStatus]] = Query(None, alias="status"),
    priority: Optional[List[WorkflowPriority]] = Query(None),
    workflow_type: Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None),
    entity_id: Optional[str] = Query(None),
    assigned_to_id: Optional[int] = Query(None),
    initiated_by_id: Optional[int] = Query(None),
    is_overdue: Optional[bool] = Query(None),
    include_deleted: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    sort_field: str = Query("created_at"),
    sort_direction: str = Query("desc"),
) -> Any:
    WorkflowValidator.validate_pagination(page, page_size)
    WorkflowValidator.validate_sort_field(sort_field)
    WorkflowValidator.validate_sort_direction(sort_direction)

    filters = WorkflowFilterParams(
        status=[s.value for s in status_filter] if status_filter else None,
        priority=[p.value for p in priority] if priority else None,
        workflow_type=workflow_type,
        entity_type=entity_type,
        entity_id=entity_id,
        initiated_by_id=initiated_by_id,
        assigned_to_id=assigned_to_id,
        is_overdue=is_overdue,
        include_deleted=include_deleted,
        search_term=search,
    )
    pagination = PaginationParams(page=page, page_size=page_size)
    sort = SortParams(field=sort_field, direction=sort_direction)

    if search:
        page_result = await repository.search_workflows(search, filters, pagination, sort)
    else:
        page_result = await repository.list_workflows(filters, pagination, sort)

    return _to_page_response(page_result)


@router.get(
    "/statistics",
    response_model=WorkflowStatistics,
    summary="Workflow statistics",
    description="Aggregate counts by status/priority, overdue count, and average completion time.",
    dependencies=[Depends(_CAN_READ)],
)
async def get_statistics(
    repository: Annotated[WorkflowRepository, Depends(get_workflow_repository)],
    status_filter: Optional[List[WorkflowStatus]] = Query(None, alias="status"),
    workflow_type: Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None),
) -> Any:
    filters = WorkflowFilterParams(
        status=[s.value for s in status_filter] if status_filter else None,
        workflow_type=workflow_type,
        entity_type=entity_type,
    )
    total = await repository.get_total_count(filters)
    by_status = await repository.get_status_counts(filters)
    by_priority = await repository.get_priority_counts(filters)
    overdue = await repository.get_overdue_count(filters)
    avg_seconds = await repository.get_average_completion_seconds(filters)

    return {
        "total": total,
        "by_status": by_status,
        "by_priority": by_priority,
        "overdue_count": overdue,
        "average_completion_seconds": avg_seconds,
        "average_completion_hours": (
            round(avg_seconds / 3600, 2) if avg_seconds is not None else None
        ),
    }


@router.get(
    "/{workflow_id}",
    response_model=WorkflowWithSteps,
    summary="Get a workflow",
    description="Fetch a single workflow with its ordered steps and each step's approvals.",
    dependencies=[Depends(_CAN_READ)],
)
async def get_workflow(
    workflow_id: uuid.UUID,
    engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
) -> Any:
    await engine._get_workflow_or_raise(workflow_id)
    return await engine.repository.get_workflow_with_steps(workflow_id)


@router.patch(
    "/{workflow_id}",
    response_model=WorkflowRead,
    summary="Update a workflow",
    description="Partially update a workflow. Status changes are validated against the state machine.",
    dependencies=[Depends(_CAN_WRITE)],
)
async def update_workflow(
    workflow_id: uuid.UUID,
    payload: WorkflowUpdate,
    repository: Annotated[WorkflowRepository, Depends(get_workflow_repository)],
    current_user: CurrentUser,
) -> Any:
    workflow = await repository.get_workflow_by_id(workflow_id)
    if workflow is None:
        raise NotFoundException(f"Workflow '{workflow_id}' was not found.")

    update_data = payload.model_dump(exclude_unset=True)

    if "status" in update_data and update_data["status"] is not None:
        target_status = WorkflowStatus(update_data["status"])
        WorkflowValidator.validate_workflow_transition(workflow.status, target_status)

    if "priority" in update_data and update_data["priority"] is not None:
        update_data["priority"] = update_data["priority"].value

    update_data["updated_by_id"] = current_user.id
    return await repository.update_workflow(workflow, update_data)


@router.delete(
    "/{workflow_id}",
    response_model=WorkflowRead,
    summary="Soft-delete a workflow",
    description="Marks the workflow as deleted without removing it from the database.",
    dependencies=[Depends(_CAN_DELETE)],
)
async def delete_workflow(
    workflow_id: uuid.UUID,
    engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
) -> Any:
    workflow = await engine._get_workflow_or_raise(workflow_id, include_deleted=True)
    if workflow.is_deleted:
        raise ConflictException(f"Workflow '{workflow_id}' is already deleted.")
    return await engine.repository.soft_delete_workflow(workflow)


@router.post(
    "/{workflow_id}/restore",
    response_model=WorkflowRead,
    summary="Restore a soft-deleted workflow",
    dependencies=[Depends(_CAN_DELETE)],
)
async def restore_workflow(
    workflow_id: uuid.UUID,
    repository: Annotated[WorkflowRepository, Depends(get_workflow_repository)],
) -> Any:
    workflow = await repository.get_workflow_by_id(workflow_id, include_deleted=True)
    if workflow is None:
        raise NotFoundException(f"Workflow '{workflow_id}' was not found.")
    if not workflow.is_deleted:
        raise ConflictException(f"Workflow '{workflow_id}' is not deleted.")
    return await repository.restore_workflow(workflow)


# ---------------------------------------------------------------------------
# Workflow: history
# ---------------------------------------------------------------------------
@router.get(
    "/{workflow_id}/history",
    response_model=List[dict],
    summary="Workflow execution history",
    description="Chronological timeline merging this workflow's step transitions and approval decisions.",
    dependencies=[Depends(_CAN_READ)],
)
async def get_workflow_history(
    workflow_id: uuid.UUID,
    engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
) -> Any:
    return await engine.get_history(workflow_id)


# ---------------------------------------------------------------------------
# Workflow: execution
# ---------------------------------------------------------------------------
@router.post(
    "/{workflow_id}/start",
    response_model=WorkflowRead,
    summary="Start a workflow",
    description="Transitions a DRAFT workflow to ACTIVE/IN_PROGRESS and starts its first step.",
    dependencies=[Depends(_CAN_EXECUTE)],
)
async def start_workflow(
    workflow_id: uuid.UUID,
    engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
    current_user: CurrentUser,
) -> Any:
    return await engine.start_workflow(workflow_id, actor_id=current_user.id)


@router.post(
    "/{workflow_id}/hold",
    response_model=WorkflowRead,
    summary="Put a workflow on hold",
    dependencies=[Depends(_CAN_EXECUTE)],
)
async def hold_workflow(
    workflow_id: uuid.UUID,
    engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
    current_user: CurrentUser,
) -> Any:
    return await engine.hold_workflow(workflow_id, actor_id=current_user.id)


@router.post(
    "/{workflow_id}/resume",
    response_model=WorkflowRead,
    summary="Resume a held workflow",
    dependencies=[Depends(_CAN_EXECUTE)],
)
async def resume_workflow(
    workflow_id: uuid.UUID,
    engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
    current_user: CurrentUser,
) -> Any:
    return await engine.resume_workflow(workflow_id, actor_id=current_user.id)


@router.post(
    "/{workflow_id}/cancel",
    response_model=WorkflowRead,
    summary="Cancel a workflow",
    dependencies=[Depends(_CAN_EXECUTE)],
)
async def cancel_workflow(
    workflow_id: uuid.UUID,
    payload: CancelWorkflowRequest,
    engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
    current_user: CurrentUser,
) -> Any:
    return await engine.cancel_workflow(
        workflow_id, payload.reason, actor_id=current_user.id
    )


@router.post(
    "/{workflow_id}/assign",
    response_model=WorkflowRead,
    summary="Assign a workflow to a user",
    dependencies=[Depends(_CAN_WRITE)],
)
async def assign_workflow(
    workflow_id: uuid.UUID,
    payload: AssignmentRequest,
    engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
    current_user: CurrentUser,
) -> Any:
    return await engine.assign_workflow(
        workflow_id, payload.assignee_id, actor_id=current_user.id
    )


# ---------------------------------------------------------------------------
# Workflow: comments
# ---------------------------------------------------------------------------
@router.post(
    "/{workflow_id}/comments",
    response_model=CommentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a comment to a workflow",
    dependencies=[Depends(_CAN_READ)],
)
async def add_workflow_comment(
    workflow_id: uuid.UUID,
    payload: CommentCreate,
    engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
    current_user: CurrentUser,
) -> Any:
    return await engine.add_workflow_comment(
        workflow_id, current_user.id, payload.message
    )


@router.get(
    "/{workflow_id}/comments",
    response_model=List[CommentRead],
    summary="List a workflow's comments",
    dependencies=[Depends(_CAN_READ)],
)
async def list_workflow_comments(
    workflow_id: uuid.UUID,
    engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
) -> Any:
    return await engine.list_workflow_comments(workflow_id)


# ---------------------------------------------------------------------------
# WorkflowStep: CRUD
# ---------------------------------------------------------------------------
@router.post(
    "/{workflow_id}/steps",
    response_model=WorkflowStepRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a step to a workflow",
    dependencies=[Depends(_CAN_WRITE)],
)
async def add_step(
    workflow_id: uuid.UUID,
    payload: WorkflowStepCreate,
    repository: Annotated[WorkflowRepository, Depends(get_workflow_repository)],
    current_user: CurrentUser,
) -> Any:
    workflow = await repository.get_workflow_by_id(workflow_id)
    if workflow is None:
        raise NotFoundException(f"Workflow '{workflow_id}' was not found.")

    WorkflowValidator.validate_step_name(payload.step_name)
    WorkflowValidator.validate_step_can_be_added(workflow.status)

    existing = await repository.get_step_by_order(workflow_id, payload.step_order)
    if existing is not None:
        raise ConflictException(
            f"A step with step_order={payload.step_order} already exists "
            f"for workflow '{workflow_id}'."
        )

    # The step's `workflow_id` is always taken from the URL path, never
    # trusted from the request body -- otherwise a caller could pass a
    # different `workflow_id` in the JSON payload than the one in the
    # URL and have the step silently created under (or FK-violate
    # against) an unrelated workflow, bypassing the status/order checks
    # just performed above against the path's workflow.
    values = payload.model_dump(exclude={"workflow_id"})
    values["workflow_id"] = workflow_id
    values["created_by_id"] = current_user.id
    values["updated_by_id"] = current_user.id
    return await repository.create_step(values)


@router.get(
    "/{workflow_id}/steps",
    response_model=List[WorkflowStepWithApprovals],
    summary="List a workflow's steps",
    dependencies=[Depends(_CAN_READ)],
)
async def list_steps(
    workflow_id: uuid.UUID,
    engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
) -> Any:
    await engine._get_workflow_or_raise(workflow_id)
    workflow = await engine.repository.get_workflow_with_steps(workflow_id)
    return workflow.steps if workflow else []


@router.get(
    "/{workflow_id}/steps/{step_id}",
    response_model=WorkflowStepWithApprovals,
    summary="Get a workflow step",
    dependencies=[Depends(_CAN_READ)],
)
async def get_step(
    workflow_id: uuid.UUID,
    step_id: uuid.UUID,
    engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
) -> Any:
    await engine._get_step_or_raise(workflow_id, step_id)
    return await engine.repository.get_step_with_approvals(step_id)


@router.patch(
    "/{workflow_id}/steps/{step_id}",
    response_model=WorkflowStepRead,
    summary="Update a workflow step",
    dependencies=[Depends(_CAN_WRITE)],
)
async def update_step(
    workflow_id: uuid.UUID,
    step_id: uuid.UUID,
    payload: WorkflowStepUpdate,
    engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
    current_user: CurrentUser,
) -> Any:
    step = await engine._get_step_or_raise(workflow_id, step_id)
    update_data = payload.model_dump(exclude_unset=True)

    if "status" in update_data and update_data["status"] is not None:
        target_status = WorkflowStepStatus(update_data["status"])
        WorkflowValidator.validate_step_transition(step.status, target_status)

    update_data["updated_by_id"] = current_user.id
    return await engine.repository.update_step(step, update_data)


@router.delete(
    "/{workflow_id}/steps/{step_id}",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a workflow step",
    description="Only permitted while the parent workflow is still in DRAFT status.",
    dependencies=[Depends(_CAN_DELETE)],
)
async def remove_step(
    workflow_id: uuid.UUID,
    step_id: uuid.UUID,
    engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
) -> None:
    workflow = await engine._get_workflow_or_raise(workflow_id)
    step = await engine._get_step_or_raise(workflow_id, step_id)
    WorkflowValidator.validate_step_can_be_removed(workflow.status)
    await engine.repository.delete_step(step)


# ---------------------------------------------------------------------------
# WorkflowStep: execution
# ---------------------------------------------------------------------------
@router.post(
    "/{workflow_id}/steps/{step_id}/complete",
    response_model=WorkflowStepRead,
    summary="Complete a step",
    dependencies=[Depends(_CAN_EXECUTE)],
)
async def complete_step(
    workflow_id: uuid.UUID,
    step_id: uuid.UUID,
    payload: StepOutputRequest,
    engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
    current_user: CurrentUser,
) -> Any:
    return await engine.complete_step(
        workflow_id, step_id, payload.output_data, actor_id=current_user.id
    )


@router.post(
    "/{workflow_id}/steps/{step_id}/fail",
    response_model=WorkflowStepRead,
    summary="Fail a step",
    dependencies=[Depends(_CAN_EXECUTE)],
)
async def fail_step(
    workflow_id: uuid.UUID,
    step_id: uuid.UUID,
    payload: StepFailureRequest,
    engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
    current_user: CurrentUser,
) -> Any:
    return await engine.fail_step(
        workflow_id, step_id, payload.reason, actor_id=current_user.id
    )


@router.post(
    "/{workflow_id}/steps/{step_id}/retry",
    response_model=WorkflowStepRead,
    summary="Retry a failed step",
    dependencies=[Depends(_CAN_EXECUTE)],
)
async def retry_step(
    workflow_id: uuid.UUID,
    step_id: uuid.UUID,
    engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
    current_user: CurrentUser,
) -> Any:
    return await engine.retry_step(workflow_id, step_id, actor_id=current_user.id)


@router.post(
    "/{workflow_id}/steps/{step_id}/skip",
    response_model=WorkflowStepRead,
    summary="Skip a step",
    dependencies=[Depends(_CAN_EXECUTE)],
)
async def skip_step(
    workflow_id: uuid.UUID,
    step_id: uuid.UUID,
    engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
    current_user: CurrentUser,
) -> Any:
    return await engine.skip_step(workflow_id, step_id, actor_id=current_user.id)


@router.post(
    "/{workflow_id}/steps/{step_id}/assign",
    response_model=WorkflowStepRead,
    summary="Assign a step to a user",
    dependencies=[Depends(_CAN_WRITE)],
)
async def assign_step(
    workflow_id: uuid.UUID,
    step_id: uuid.UUID,
    payload: AssignmentRequest,
    engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
    current_user: CurrentUser,
) -> Any:
    return await engine.assign_step(
        workflow_id, step_id, payload.assignee_id, actor_id=current_user.id
    )


# ---------------------------------------------------------------------------
# WorkflowStep: comments
# ---------------------------------------------------------------------------
@router.post(
    "/{workflow_id}/steps/{step_id}/comments",
    response_model=CommentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a comment to a step",
    dependencies=[Depends(_CAN_READ)],
)
async def add_step_comment(
    workflow_id: uuid.UUID,
    step_id: uuid.UUID,
    payload: CommentCreate,
    engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
    current_user: CurrentUser,
) -> Any:
    return await engine.add_step_comment(
        workflow_id, step_id, current_user.id, payload.message
    )


@router.get(
    "/{workflow_id}/steps/{step_id}/comments",
    response_model=List[CommentRead],
    summary="List a step's comments",
    dependencies=[Depends(_CAN_READ)],
)
async def list_step_comments(
    workflow_id: uuid.UUID,
    step_id: uuid.UUID,
    engine: Annotated[WorkflowEngine, Depends(get_workflow_engine)],
) -> Any:
    return await engine.list_step_comments(workflow_id, step_id)


# ---------------------------------------------------------------------------
# Approval flow
# ---------------------------------------------------------------------------
@router.post(
    "/{workflow_id}/steps/{step_id}/approvals",
    response_model=WorkflowApprovalRead,
    status_code=status.HTTP_201_CREATED,
    summary="Request approval for a step",
    dependencies=[Depends(_CAN_WRITE)],
)
async def request_approval(
    workflow_id: uuid.UUID,
    step_id: uuid.UUID,
    payload: ApprovalRequestCreate,
    approval_engine: Annotated[ApprovalEngine, Depends(get_approval_engine)],
    current_user: CurrentUser,
) -> Any:
    return await approval_engine.request_approval(
        workflow_id, step_id, payload.approver_id, actor_id=current_user.id
    )


@router.get(
    "/{workflow_id}/steps/{step_id}/approvals",
    response_model=List[WorkflowApprovalRead],
    summary="List a step's approvals",
    dependencies=[Depends(_CAN_READ)],
)
async def list_step_approvals(
    workflow_id: uuid.UUID,
    step_id: uuid.UUID,
    approval_engine: Annotated[ApprovalEngine, Depends(get_approval_engine)],
) -> Any:
    return await approval_engine.list_step_approvals(step_id)


@router.get(
    "/approvals/pending",
    response_model=PaginatedApprovals,
    summary="List my pending approvals",
    description="Paginated list of approvals awaiting a decision from the current user.",
    dependencies=[Depends(_CAN_DECIDE_APPROVAL)],
)
async def list_pending_approvals(
    approval_engine: Annotated[ApprovalEngine, Depends(get_approval_engine)],
    current_user: CurrentUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> Any:
    pagination = PaginationParams(page=page, page_size=page_size)
    page_result = await approval_engine.list_pending_for_approver(
        current_user.id, pagination
    )
    return _to_page_response(page_result)


@router.post(
    "/approvals/{approval_id}/approve",
    response_model=WorkflowApprovalRead,
    summary="Approve an approval request",
    dependencies=[Depends(_CAN_DECIDE_APPROVAL)],
)
async def approve_approval(
    approval_id: uuid.UUID,
    payload: ApprovalDecisionRequest,
    approval_engine: Annotated[ApprovalEngine, Depends(get_approval_engine)],
    current_user: CurrentUser,
) -> Any:
    return await approval_engine.approve(
        approval_id, payload.decision_notes, actor_id=current_user.id
    )


@router.post(
    "/approvals/{approval_id}/reject",
    response_model=WorkflowApprovalRead,
    summary="Reject an approval request",
    dependencies=[Depends(_CAN_DECIDE_APPROVAL)],
)
async def reject_approval(
    approval_id: uuid.UUID,
    payload: ApprovalDecisionRequest,
    approval_engine: Annotated[ApprovalEngine, Depends(get_approval_engine)],
    current_user: CurrentUser,
) -> Any:
    return await approval_engine.reject(
        approval_id, payload.decision_notes, actor_id=current_user.id
    )


@router.post(
    "/approvals/{approval_id}/escalate",
    response_model=WorkflowApprovalRead,
    summary="Escalate an approval request",
    dependencies=[Depends(_CAN_DECIDE_APPROVAL)],
)
async def escalate_approval(
    approval_id: uuid.UUID,
    payload: ApprovalEscalationRequest,
    approval_engine: Annotated[ApprovalEngine, Depends(get_approval_engine)],
    current_user: CurrentUser,
) -> Any:
    return await approval_engine.escalate(
        approval_id,
        payload.escalated_to_id,
        payload.decision_notes,
        actor_id=current_user.id,
    )


@router.post(
    "/approvals/{approval_id}/cancel",
    response_model=WorkflowApprovalRead,
    summary="Cancel an approval request",
    dependencies=[Depends(_CAN_WRITE)],
)
async def cancel_approval(
    approval_id: uuid.UUID,
    payload: ApprovalDecisionRequest,
    approval_engine: Annotated[ApprovalEngine, Depends(get_approval_engine)],
    current_user: CurrentUser,
) -> Any:
    return await approval_engine.cancel(
        approval_id, payload.decision_notes, actor_id=current_user.id
    )