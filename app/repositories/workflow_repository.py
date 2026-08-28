"""
backend/app/repositories/workflow_repository.py

Data access layer for the Workflow module.

This repository is intentionally "dumb": it knows how to talk to the
database (SQLAlchemy 2.x async) and how to shape query results, but it
holds no business rules, no state-machine/transition logic, and raises
no domain exceptions. All of that lives in `workflow_service.py`. The
repository only raises `LookupError` internally to signal "no row was
found for an in-place mutation", which the service layer translates
into a domain exception where relevant.

Mirrors:
    app/models/workflow.py -> Workflow, WorkflowStep, WorkflowApproval
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.workflow import (
    ApprovalStatus,
    Workflow,
    WorkflowApproval,
    WorkflowStep,
    WorkflowStepStatus,
)


# ---------------------------------------------------------------------------
# Query helper value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WorkflowFilterParams:
    """Filter criteria for listing/searching workflows."""

    status: Optional[Sequence[str]] = None
    priority: Optional[Sequence[str]] = None
    workflow_type: Optional[str] = None
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    initiated_by_id: Optional[int] = None
    assigned_to_id: Optional[int] = None
    due_before: Optional[datetime] = None
    due_after: Optional[datetime] = None
    is_overdue: Optional[bool] = None
    include_deleted: bool = False
    search_term: Optional[str] = None


@dataclass(frozen=True)
class PaginationParams:
    """Offset-based pagination parameters."""

    page: int = 1
    page_size: int = 20

    @property
    def offset(self) -> int:
        return max(self.page - 1, 0) * self.page_size


@dataclass(frozen=True)
class SortParams:
    """Sorting parameters for a single sortable field."""

    field: str = "created_at"
    direction: str = "desc"  # "asc" | "desc"


@dataclass(frozen=True)
class PageResult:
    """Generic container for a page of results plus total row count."""

    items: List[Any] = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20

    @property
    def total_pages(self) -> int:
        if self.page_size <= 0:
            return 0
        return (self.total + self.page_size - 1) // self.page_size


_SORTABLE_WORKFLOW_FIELDS = {
    "created_at": Workflow.created_at,
    "updated_at": Workflow.updated_at,
    "due_date": Workflow.due_date,
    "priority": Workflow.priority,
    "status": Workflow.status,
    "name": Workflow.name,
    "started_at": Workflow.started_at,
    "completed_at": Workflow.completed_at,
}


class WorkflowRepository:
    """Async repository encapsulating all persistence operations for
    Workflow, WorkflowStep, and WorkflowApproval rows."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Workflow: CRUD
    # ------------------------------------------------------------------
    async def create_workflow(self, values: dict[str, Any]) -> Workflow:
        workflow = Workflow(**values)
        self.session.add(workflow)
        await self.session.commit()
        await self.session.refresh(workflow)
        return workflow

    def _base_workflow_stmt(self, include_deleted: bool = False) -> Select:
        stmt = select(Workflow)
        if not include_deleted:
            stmt = stmt.where(Workflow.is_deleted.is_(False))
        return stmt

    async def get_workflow_by_id(
        self, workflow_id: uuid.UUID, include_deleted: bool = False
    ) -> Optional[Workflow]:
        stmt = self._base_workflow_stmt(include_deleted).where(Workflow.id == workflow_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_workflow_with_steps(
        self, workflow_id: uuid.UUID, include_deleted: bool = False
    ) -> Optional[Workflow]:
        stmt = (
            self._base_workflow_stmt(include_deleted)
            .where(Workflow.id == workflow_id)
            .options(
                selectinload(Workflow.steps).selectinload(WorkflowStep.approvals)
            )
        )
        result = await self.session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def update_workflow(
        self, workflow: Workflow, values: dict[str, Any]
    ) -> Workflow:
        for key, val in values.items():
            setattr(workflow, key, val)

        # PostgreSQL enforces completed_at/status consistency. When a caller
        # records a completion timestamp, transition the workflow to the
        # matching terminal state in the same unit of work.
        if values.get("completed_at") is not None:
            from app.models.workflow import WorkflowStatus
            workflow.status = WorkflowStatus.COMPLETED
        elif values.get("status") is not None:
            from app.models.workflow import WorkflowStatus
            try:
                requested_status = (
                    WorkflowStatus(values["status"])
                    if isinstance(values["status"], str)
                    else values["status"]
                )
            except ValueError:
                requested_status = values["status"]
            if requested_status != WorkflowStatus.COMPLETED:
                workflow.completed_at = None
        await self.session.commit()
        await self.session.refresh(workflow)
        return workflow

    async def soft_delete_workflow(self, workflow: Workflow) -> Workflow:
        workflow.is_deleted = True
        workflow.deleted_at = datetime.now(timezone.utc)
        await self.session.commit()
        await self.session.refresh(workflow)
        return workflow

    async def restore_workflow(self, workflow: Workflow) -> Workflow:
        workflow.is_deleted = False
        workflow.deleted_at = None
        await self.session.commit()
        await self.session.refresh(workflow)
        return workflow

    # ------------------------------------------------------------------
    # Workflow: list / search / pagination / sorting / filtering
    # ------------------------------------------------------------------
    def _apply_filters(self, stmt: Select, filters: WorkflowFilterParams) -> Select:
        if not filters.include_deleted:
            stmt = stmt.where(Workflow.is_deleted.is_(False))
        if filters.status:
            stmt = stmt.where(Workflow.status.in_(list(filters.status)))
        if filters.priority:
            stmt = stmt.where(Workflow.priority.in_(list(filters.priority)))
        if filters.workflow_type:
            stmt = stmt.where(Workflow.workflow_type == filters.workflow_type)
        if filters.entity_type:
            stmt = stmt.where(Workflow.entity_type == filters.entity_type)
        if filters.entity_id:
            stmt = stmt.where(Workflow.entity_id == filters.entity_id)
        if filters.initiated_by_id:
            stmt = stmt.where(Workflow.initiated_by_id == filters.initiated_by_id)
        if filters.assigned_to_id:
            stmt = stmt.where(Workflow.assigned_to_id == filters.assigned_to_id)
        if filters.due_before:
            stmt = stmt.where(Workflow.due_date <= filters.due_before)
        if filters.due_after:
            stmt = stmt.where(Workflow.due_date >= filters.due_after)
        if filters.is_overdue:
            now = datetime.now(timezone.utc)
            stmt = stmt.where(
                and_(
                    Workflow.due_date.is_not(None),
                    Workflow.due_date < now,
                    Workflow.status.notin_(["completed", "cancelled"]),
                )
            )
        if filters.search_term:
            term = f"%{filters.search_term.strip()}%"
            stmt = stmt.where(
                or_(
                    Workflow.name.ilike(term),
                    Workflow.description.ilike(term),
                    Workflow.workflow_type.ilike(term),
                    Workflow.entity_id.ilike(term),
                )
            )
        return stmt

    def _apply_sort(self, stmt: Select, sort: SortParams) -> Select:
        column = _SORTABLE_WORKFLOW_FIELDS.get(sort.field, Workflow.created_at)
        if sort.direction.lower() == "asc":
            return stmt.order_by(column.asc())
        return stmt.order_by(column.desc())

    async def list_workflows(
        self,
        filters: WorkflowFilterParams,
        pagination: PaginationParams,
        sort: SortParams,
    ) -> PageResult:
        base_stmt = self._apply_filters(select(Workflow), filters)

        count_stmt = select(func.count()).select_from(base_stmt.subquery())
        total = (await self.session.execute(count_stmt)).scalar_one()

        page_stmt = self._apply_sort(base_stmt, sort).offset(pagination.offset).limit(
            pagination.page_size
        )
        items = (await self.session.execute(page_stmt)).scalars().all()

        return PageResult(
            items=list(items),
            total=total,
            page=pagination.page,
            page_size=pagination.page_size,
        )

    async def search_workflows(
        self,
        search_term: str,
        filters: WorkflowFilterParams,
        pagination: PaginationParams,
        sort: SortParams,
    ) -> PageResult:
        merged = WorkflowFilterParams(
            status=filters.status,
            priority=filters.priority,
            workflow_type=filters.workflow_type,
            entity_type=filters.entity_type,
            entity_id=filters.entity_id,
            initiated_by_id=filters.initiated_by_id,
            assigned_to_id=filters.assigned_to_id,
            due_before=filters.due_before,
            due_after=filters.due_after,
            is_overdue=filters.is_overdue,
            include_deleted=filters.include_deleted,
            search_term=search_term,
        )
        return await self.list_workflows(merged, pagination, sort)

    # ------------------------------------------------------------------
    # Workflow: statistics
    # ------------------------------------------------------------------
    async def get_status_counts(self, filters: WorkflowFilterParams) -> dict[str, int]:
        stmt = self._apply_filters(
            select(Workflow.status, func.count(Workflow.id)), filters
        ).group_by(Workflow.status)
        rows = (await self.session.execute(stmt)).all()
        return {status.value: count for status, count in rows}

    async def get_priority_counts(self, filters: WorkflowFilterParams) -> dict[str, int]:
        stmt = self._apply_filters(
            select(Workflow.priority, func.count(Workflow.id)), filters
        ).group_by(Workflow.priority)
        rows = (await self.session.execute(stmt)).all()
        return {str(priority): count for priority, count in rows}

    async def get_overdue_count(self, filters: WorkflowFilterParams) -> int:
        overdue_filters = WorkflowFilterParams(
            status=filters.status,
            priority=filters.priority,
            workflow_type=filters.workflow_type,
            entity_type=filters.entity_type,
            entity_id=filters.entity_id,
            initiated_by_id=filters.initiated_by_id,
            assigned_to_id=filters.assigned_to_id,
            include_deleted=filters.include_deleted,
            is_overdue=True,
        )
        stmt = self._apply_filters(select(func.count(Workflow.id)), overdue_filters)
        return (await self.session.execute(stmt)).scalar_one()

    async def get_average_completion_seconds(
        self, filters: WorkflowFilterParams
    ) -> Optional[float]:
        stmt = self._apply_filters(
            select(
                func.avg(
                    func.extract("epoch", Workflow.completed_at - Workflow.started_at)
                )
            ),
            filters,
        ).where(Workflow.completed_at.is_not(None), Workflow.started_at.is_not(None))
        value = (await self.session.execute(stmt)).scalar_one()
        return float(value) if value is not None else None

    async def get_total_count(self, filters: WorkflowFilterParams) -> int:
        stmt = self._apply_filters(select(func.count(Workflow.id)), filters)
        return (await self.session.execute(stmt)).scalar_one()

    # ------------------------------------------------------------------
    # WorkflowStep: CRUD
    # ------------------------------------------------------------------
    async def create_step(self, values: dict[str, Any]) -> WorkflowStep:
        step = WorkflowStep(**values)
        self.session.add(step)
        await self.session.commit()
        await self.session.refresh(step)
        return step

    async def bulk_create_steps(
        self, values_list: Iterable[dict[str, Any]]
    ) -> List[WorkflowStep]:
        steps = [WorkflowStep(**values) for values in values_list]
        self.session.add_all(steps)
        await self.session.commit()
        for step in steps:
            await self.session.refresh(step)
        return steps

    async def get_step_by_id(self, step_id: uuid.UUID) -> Optional[WorkflowStep]:
        stmt = select(WorkflowStep).where(WorkflowStep.id == step_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_step_with_approvals(
        self, step_id: uuid.UUID
    ) -> Optional[WorkflowStep]:
        stmt = (
            select(WorkflowStep)
            .where(WorkflowStep.id == step_id)
            .options(selectinload(WorkflowStep.approvals))
        )
        result = await self.session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def list_steps_by_workflow(
        self, workflow_id: uuid.UUID
    ) -> List[WorkflowStep]:
        stmt = (
            select(WorkflowStep)
            .where(WorkflowStep.workflow_id == workflow_id)
            .order_by(WorkflowStep.step_order.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_step_by_order(
        self, workflow_id: uuid.UUID, step_order: int
    ) -> Optional[WorkflowStep]:
        stmt = select(WorkflowStep).where(
            WorkflowStep.workflow_id == workflow_id,
            WorkflowStep.step_order == step_order,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_next_pending_step(
        self, workflow_id: uuid.UUID
    ) -> Optional[WorkflowStep]:
        stmt = (
            select(WorkflowStep)
            .where(
                WorkflowStep.workflow_id == workflow_id,
                WorkflowStep.status == WorkflowStepStatus.PENDING,
            )
            .order_by(WorkflowStep.step_order.asc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_step(
        self, step: WorkflowStep, values: dict[str, Any]
    ) -> WorkflowStep:
        for key, val in values.items():
            setattr(step, key, val)
        await self.session.commit()
        await self.session.refresh(step)
        return step

    async def delete_step(self, step: WorkflowStep) -> None:
        await self.session.delete(step)
        await self.session.commit()

    async def count_steps_by_status(
        self, workflow_id: uuid.UUID
    ) -> dict[str, int]:
        stmt = (
            select(WorkflowStep.status, func.count(WorkflowStep.id))
            .where(WorkflowStep.workflow_id == workflow_id)
            .group_by(WorkflowStep.status)
        )
        rows = (await self.session.execute(stmt)).all()
        return {status.value: count for status, count in rows}

    # ------------------------------------------------------------------
    # WorkflowApproval: CRUD
    # ------------------------------------------------------------------
    async def create_approval(self, values: dict[str, Any]) -> WorkflowApproval:
        approval = WorkflowApproval(**values)
        self.session.add(approval)
        await self.session.commit()
        await self.session.refresh(approval)
        return approval

    async def get_approval_by_id(
        self, approval_id: uuid.UUID
    ) -> Optional[WorkflowApproval]:
        stmt = select(WorkflowApproval).where(WorkflowApproval.id == approval_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_approvals_by_step(
        self, step_id: uuid.UUID
    ) -> List[WorkflowApproval]:
        stmt = (
            select(WorkflowApproval)
            .where(WorkflowApproval.workflow_step_id == step_id)
            .order_by(WorkflowApproval.requested_at.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_approvals_by_workflow(
        self, workflow_id: uuid.UUID
    ) -> List[WorkflowApproval]:
        stmt = (
            select(WorkflowApproval)
            .where(WorkflowApproval.workflow_id == workflow_id)
            .order_by(WorkflowApproval.requested_at.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_pending_approvals_for_approver(
        self,
        approver_id: int,
        pagination: PaginationParams,
    ) -> PageResult:
        base_stmt = select(WorkflowApproval).where(
            WorkflowApproval.approver_id == approver_id,
            WorkflowApproval.status.in_(
                [ApprovalStatus.PENDING, ApprovalStatus.ESCALATED]
            ),
        )
        count_stmt = select(func.count()).select_from(base_stmt.subquery())
        total = (await self.session.execute(count_stmt)).scalar_one()

        page_stmt = (
            base_stmt.order_by(WorkflowApproval.requested_at.asc())
            .offset(pagination.offset)
            .limit(pagination.page_size)
        )
        items = (await self.session.execute(page_stmt)).scalars().all()
        return PageResult(
            items=list(items),
            total=total,
            page=pagination.page,
            page_size=pagination.page_size,
        )

    async def has_pending_approvals(self, step_id: uuid.UUID) -> bool:
        stmt = select(func.count(WorkflowApproval.id)).where(
            WorkflowApproval.workflow_step_id == step_id,
            WorkflowApproval.status.in_(
                [ApprovalStatus.PENDING, ApprovalStatus.ESCALATED]
            ),
        )
        count = (await self.session.execute(stmt)).scalar_one()
        return count > 0

    async def update_approval(
        self, approval: WorkflowApproval, values: dict[str, Any]
    ) -> WorkflowApproval:
        for key, val in values.items():
            setattr(approval, key, val)
        await self.session.commit()
        await self.session.refresh(approval)
        return approval

    async def get_approval_counts_by_status(
        self, workflow_id: Optional[uuid.UUID] = None
    ) -> dict[str, int]:
        stmt = select(WorkflowApproval.status, func.count(WorkflowApproval.id))
        if workflow_id is not None:
            stmt = stmt.where(WorkflowApproval.workflow_id == workflow_id)
        stmt = stmt.group_by(WorkflowApproval.status)
        rows = (await self.session.execute(stmt)).all()
        return {status.value: count for status, count in rows}

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------
    async def get_workflow_history(
        self, workflow_id: uuid.UUID
    ) -> List[dict[str, Any]]:
        """Build a chronological timeline of a workflow's execution by
        merging its steps and approvals into a single ordered list of
        history entries."""
        steps = await self.list_steps_by_workflow(workflow_id)
        approvals = await self.list_approvals_by_workflow(workflow_id)

        entries: List[dict[str, Any]] = []
        for step in steps:
            entries.append(
                {
                    "type": "step",
                    "id": step.id,
                    "step_order": step.step_order,
                    "step_name": step.step_name,
                    "status": step.status.value,
                    "timestamp": step.completed_at
                    or step.started_at
                    or step.created_at,
                    "assigned_to_id": step.assigned_to_id,
                }
            )
        for approval in approvals:
            entries.append(
                {
                    "type": "approval",
                    "id": approval.id,
                    "workflow_step_id": approval.workflow_step_id,
                    "status": approval.status.value,
                    "timestamp": approval.decided_at or approval.requested_at,
                    "approver_id": approval.approver_id,
                    "escalated": approval.escalated,
                }
            )

        entries.sort(key=lambda entry: entry["timestamp"] or datetime.min)
        return entries