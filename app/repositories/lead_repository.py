"""
backend/app/repositories/lead_repository.py

Data-access layer for the Lead entity.

Responsibilities:
    - Encapsulate all direct database interactions (SELECT/INSERT/UPDATE)
      for the `Lead` model behind a clean, testable interface.
    - Provide a single point of change if the persistence mechanism or
      query strategy evolves (e.g., adding caching, read replicas).

Design Notes:
    - This repository is strictly a data-access abstraction. It contains
      NO business rules, permission checks, or HTTP-layer concerns
      (those belong in the service and API layers). It never raises
      `HTTPException` and never imports FastAPI.
    - All methods are async and expect an `AsyncSession` injected via
      the constructor.
    - Deletion is soft-only: `soft_delete()` flips `is_active` to
      `False` rather than issuing a `DELETE` statement, preserving
      historical/audit data.
    - Filtering/sorting parameters are accepted as plain, typed
      primitives (not Pydantic schemas), keeping this module decoupled
      from the API/schema layer per Clean Architecture boundaries.

Fix Note (2026-08-20):
    - `LeadService.list_leads()` has always accepted and forwarded
      `phone` and `email` as exact-match filter kwargs (mirroring
      `LeadFilter.phone` / `LeadFilter.email` in `schemas/lead.py`),
      but `LeadRepository.list_leads()` and its internal
      `_apply_filters()` helper never declared matching parameters.
      Any call to `GET /api/v1/leads/` therefore raised
      `TypeError: LeadRepository.list_leads() got an unexpected
      keyword argument 'phone'` inside the service call, which is not
      caught by the router's exception translation (that only wraps
      known domain exceptions), and surfaced as an uncaught HTTP 500.
      `phone` and `email` are now accepted and applied as exact-match
      filters in both `_apply_filters()` and `list_leads()`, restoring
      signature parity between the service and repository layers.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Optional, Sequence

from sqlalchemy import Select, and_, asc, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.lead import Lead, LeadPriority, LeadSource, LeadStatus


class LeadRepository:
    """
    Repository encapsulating CRUD, search, and aggregation operations
    for the `Lead` model.

    Consumed by higher-level services (e.g., LeadService) which
    orchestrate business logic on top of these primitive persistence
    operations.
    """

    def __init__(self, db: AsyncSession) -> None:
        """
        Args:
            db: An active SQLAlchemy AsyncSession, typically supplied
                via a FastAPI dependency (e.g., `get_db`).
        """
        self._db = db

    # ----------------------------------------------------------------
    # Internal Query Helpers
    # ----------------------------------------------------------------
    def _base_select(self, *, with_relationships: bool = False) -> Select[tuple[Lead]]:
        """
        Build the base `SELECT` statement for `Lead`, optionally eager-
        loading its `assigned_agent`/`creator` relationships to avoid
        N+1 queries when callers will access those attributes.

        Args:
            with_relationships: If True, eager-loads `assigned_agent`
                and `creator` via `selectinload()`.

        Returns:
            A `Select` statement targeting the `Lead` model.
        """
        stmt = select(Lead)
        if with_relationships:
            stmt = stmt.options(
                selectinload(Lead.assigned_agent),
                selectinload(Lead.creator),
            )
        return stmt

    def _apply_filters(
        self,
        stmt: Select[tuple[Lead]],
        *,
        status: Optional[LeadStatus] = None,
        priority: Optional[LeadPriority] = None,
        lead_source: Optional[LeadSource] = None,
        assigned_agent_id: Optional[int] = None,
        property_type: Optional[str] = None,
        preferred_location: Optional[str] = None,
        phone: Optional[str] = None,
        email: Optional[str] = None,
        search: Optional[str] = None,
        is_active: Optional[bool] = True,
    ) -> Select[tuple[Lead]]:
        """
        Apply a common set of equality and search filters to a `Lead`
        select statement.

        Args:
            stmt: The base select statement to filter.
            status: Optional exact-match pipeline status filter.
            priority: Optional exact-match priority filter.
            lead_source: Optional exact-match acquisition channel filter.
            assigned_agent_id: Optional exact-match assigned agent filter.
            property_type: Optional exact-match property type filter.
            preferred_location: Optional exact-match location filter.
            phone: Optional exact-match contact phone number filter.
            email: Optional exact-match contact email address filter.
            search: Optional free-text term matched (case-insensitive)
                against `full_name`, `phone`, `email`, and `remarks`.
            is_active: Optional active-flag filter. Defaults to `True`
                so inactive (soft-deleted) leads are excluded unless
                explicitly requested otherwise via `None`.

        Returns:
            The filtered `Select` statement.
        """
        conditions: list[Any] = []

        if is_active is not None:
            conditions.append(Lead.is_active == is_active)
        if status is not None:
            conditions.append(Lead.status == status)
        if priority is not None:
            conditions.append(Lead.priority == priority)
        if lead_source is not None:
            conditions.append(Lead.lead_source == lead_source)
        if assigned_agent_id is not None:
            conditions.append(Lead.assigned_agent_id == assigned_agent_id)
        if property_type is not None:
            conditions.append(Lead.property_type == property_type)
        if preferred_location is not None:
            conditions.append(Lead.preferred_location == preferred_location)
        if phone is not None:
            conditions.append(Lead.phone == phone)
        if email is not None:
            conditions.append(Lead.email == email)

        if conditions:
            stmt = stmt.where(and_(*conditions))

        if search:
            term = f"%{search}%"
            stmt = stmt.where(
                or_(
                    Lead.full_name.ilike(term),
                    Lead.phone.ilike(term),
                    Lead.email.ilike(term),
                    Lead.remarks.ilike(term),
                )
            )

        return stmt

    def _apply_sorting(
        self,
        stmt: Select[tuple[Lead]],
        *,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> Select[tuple[Lead]]:
        """
        Apply sorting to a `Lead` select statement.

        Args:
            stmt: The select statement to sort.
            sort_by: Name of the `Lead` column to sort by. Falls back
                to `created_at` if the attribute does not exist.
            sort_order: Either "asc" or "desc" (case-insensitive).
                Falls back to descending for any unrecognized value.

        Returns:
            The sorted `Select` statement.
        """
        column = getattr(Lead, sort_by, Lead.created_at)
        direction = asc if sort_order.lower() == "asc" else desc
        return stmt.order_by(direction(column))

    # ----------------------------------------------------------------
    # Create
    # ----------------------------------------------------------------
    async def create_lead(self, lead: Lead) -> Lead:
        """
        Persist a new lead record.

        Args:
            lead: A transient `Lead` instance (not yet added to the
                  session) with all required fields populated by the
                  caller.

        Returns:
            The persisted `Lead` instance, refreshed with any
            server-generated values (id, created_at, updated_at, etc.).
        """
        self._db.add(lead)
        await self._db.commit()
        await self._db.refresh(lead)
        return lead

    # ----------------------------------------------------------------
    # Read - Single Record
    # ----------------------------------------------------------------
    async def get_by_id(self, lead_id: uuid.UUID) -> Optional[Lead]:
        """
        Retrieve a lead by its primary key.

        Args:
            lead_id: The UUID of the lead to retrieve.

        Returns:
            The matching `Lead` instance, or None if no match is found.
        """
        stmt = self._base_select(with_relationships=True).where(Lead.id == lead_id)
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_phone(self, phone: str) -> Optional[Lead]:
        """
        Retrieve a lead by its exact contact phone number.

        Args:
            phone: The phone number to search for.

        Returns:
            The matching `Lead` instance, or None if no match is found.
        """
        stmt = select(Lead).where(Lead.phone == phone)
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> Optional[Lead]:
        """
        Retrieve a lead by its exact contact email address.

        Args:
            email: The email address to search for.

        Returns:
            The matching `Lead` instance, or None if no match is found.
        """
        stmt = select(Lead).where(Lead.email == email)
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    # ----------------------------------------------------------------
    # Read - Listing / Search
    # ----------------------------------------------------------------
    async def list_leads(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: Optional[LeadStatus] = None,
        priority: Optional[LeadPriority] = None,
        lead_source: Optional[LeadSource] = None,
        assigned_agent_id: Optional[int] = None,
        property_type: Optional[str] = None,
        preferred_location: Optional[str] = None,
        phone: Optional[str] = None,
        email: Optional[str] = None,
        search: Optional[str] = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        is_active: Optional[bool] = True,
    ) -> tuple[Sequence[Lead], int]:
        """
        Retrieve a paginated, filtered, and sorted list of leads,
        alongside the total count of matching records.

        Args:
            page: 1-indexed page number to retrieve.
            page_size: Number of records to return per page.
            status: Optional pipeline status filter.
            priority: Optional priority filter.
            lead_source: Optional acquisition channel filter.
            assigned_agent_id: Optional assigned agent filter.
            property_type: Optional property type filter.
            preferred_location: Optional preferred location filter.
            phone: Optional exact-match contact phone number filter.
            email: Optional exact-match contact email address filter.
            search: Optional free-text search term (name/phone/email/remarks).
            sort_by: Column name to sort by.
            sort_order: "asc" or "desc".
            is_active: Optional active-flag filter; defaults to True.

        Returns:
            A tuple of (matching leads for the requested page, total
            count of matching records across all pages).
        """
        base_stmt = self._apply_filters(
            select(Lead),
            status=status,
            priority=priority,
            lead_source=lead_source,
            assigned_agent_id=assigned_agent_id,
            property_type=property_type,
            preferred_location=preferred_location,
            phone=phone,
            email=email,
            search=search,
            is_active=is_active,
        )

        count_stmt = select(func.count()).select_from(base_stmt.subquery())
        total_result = await self._db.execute(count_stmt)
        total = total_result.scalar_one()

        data_stmt = self._apply_filters(
            self._base_select(with_relationships=True),
            status=status,
            priority=priority,
            lead_source=lead_source,
            assigned_agent_id=assigned_agent_id,
            property_type=property_type,
            preferred_location=preferred_location,
            phone=phone,
            email=email,
            search=search,
            is_active=is_active,
        )
        data_stmt = self._apply_sorting(data_stmt, sort_by=sort_by, sort_order=sort_order)
        data_stmt = data_stmt.offset((page - 1) * page_size).limit(page_size)

        result = await self._db.execute(data_stmt)
        leads = result.scalars().all()

        return leads, total

    async def search(
        self,
        term: str,
        *,
        page: int = 1,
        page_size: int = 20,
        is_active: Optional[bool] = True,
    ) -> tuple[Sequence[Lead], int]:
        """
        Perform a free-text search for leads across name, phone, email,
        and remarks fields using case-insensitive partial matching.

        Args:
            term: The search term to match.
            page: 1-indexed page number to retrieve.
            page_size: Number of records to return per page.
            is_active: Optional active-flag filter; defaults to True.

        Returns:
            A tuple of (matching leads for the requested page, total
            count of matching records across all pages).
        """
        return await self.list_leads(
            page=page,
            page_size=page_size,
            search=term,
            is_active=is_active,
        )

    async def upcoming_followups(self, *, reference_date: Optional[date] = None) -> Sequence[Lead]:
        """
        Retrieve all active leads whose next follow-up date is due
        (i.e., on or before the reference date).

        Args:
            reference_date: The date to compare against. Defaults to
                today if not supplied.

        Returns:
            A sequence of matching `Lead` instances, ordered by
            `next_follow_up` ascending (most overdue first).
        """
        effective_date = reference_date or date.today()
        stmt = (
            self._base_select(with_relationships=True)
            .where(
                and_(
                    Lead.next_follow_up.is_not(None),
                    Lead.next_follow_up <= effective_date,
                    Lead.is_active.is_(True),
                )
            )
            .order_by(asc(Lead.next_follow_up))
        )
        result = await self._db.execute(stmt)
        return result.scalars().all()

    # ----------------------------------------------------------------
    # Update
    # ----------------------------------------------------------------
    async def update_lead(self, lead: Lead, update_data: dict[str, Any]) -> Lead:
        """
        Apply a set of attribute updates to an already-tracked `Lead`
        instance and persist the changes.

        Args:
            lead: A `Lead` instance retrieved from this session (e.g.,
                  via `get_by_id`) with updates to be applied.
            update_data: Mapping of attribute names to new values. Only
                keys present on the `Lead` model are applied.

        Returns:
            The updated `Lead` instance, refreshed with the latest
            database state (e.g., updated `updated_at` timestamp).
        """
        for field_name, value in update_data.items():
            if hasattr(lead, field_name):
                setattr(lead, field_name, value)

        self._db.add(lead)
        await self._db.commit()
        await self._db.refresh(lead)
        return lead

    async def soft_delete(self, lead_id: uuid.UUID) -> Optional[Lead]:
        """
        Soft-delete a lead by setting `is_active` to False. Does not
        issue a `DELETE` statement; the record is preserved for
        auditing/history purposes.

        Args:
            lead_id: The UUID of the lead to soft-delete.

        Returns:
            The updated `Lead` instance with `is_active=False`, or None
            if no lead with the given ID exists.
        """
        stmt = (
            update(Lead)
            .where(Lead.id == lead_id)
            .values(is_active=False)
            .returning(Lead)
            .execution_options(synchronize_session="fetch")
        )
        result = await self._db.execute(stmt)
        await self._db.commit()
        return result.scalar_one_or_none()

    async def assign_agent(self, lead_id: uuid.UUID, agent_id: int) -> Optional[Lead]:
        """
        Update the sales agent assigned to a lead.

        Args:
            lead_id: The UUID of the lead to update.
            agent_id: The internal User ID of the agent to assign.

        Returns:
            The updated `Lead` instance, or None if no lead with the
            given ID exists.
        """
        stmt = (
            update(Lead)
            .where(Lead.id == lead_id)
            .values(assigned_agent_id=agent_id)
            .returning(Lead)
            .execution_options(synchronize_session="fetch")
        )
        result = await self._db.execute(stmt)
        await self._db.commit()
        return result.scalar_one_or_none()

    async def change_status(self, lead_id: uuid.UUID, status: LeadStatus) -> Optional[Lead]:
        """
        Update the pipeline status of a lead.

        Args:
            lead_id: The UUID of the lead to update.
            status: The new `LeadStatus` value to set.

        Returns:
            The updated `Lead` instance, or None if no lead with the
            given ID exists.
        """
        stmt = (
            update(Lead)
            .where(Lead.id == lead_id)
            .values(status=status)
            .returning(Lead)
            .execution_options(synchronize_session="fetch")
        )
        result = await self._db.execute(stmt)
        await self._db.commit()
        return result.scalar_one_or_none()

    async def change_priority(self, lead_id: uuid.UUID, priority: LeadPriority) -> Optional[Lead]:
        """
        Update the priority level of a lead.

        Args:
            lead_id: The UUID of the lead to update.
            priority: The new `LeadPriority` value to set.

        Returns:
            The updated `Lead` instance, or None if no lead with the
            given ID exists.
        """
        stmt = (
            update(Lead)
            .where(Lead.id == lead_id)
            .values(priority=priority)
            .returning(Lead)
            .execution_options(synchronize_session="fetch")
        )
        result = await self._db.execute(stmt)
        await self._db.commit()
        return result.scalar_one_or_none()

    # ----------------------------------------------------------------
    # Aggregation / Counting
    # ----------------------------------------------------------------
    async def count(self, *, is_active: Optional[bool] = True) -> int:
        """
        Count total leads matching the given active-flag filter.

        Args:
            is_active: Optional active-flag filter; defaults to True
                (counts only active leads).

        Returns:
            The total number of matching leads.
        """
        stmt = select(func.count()).select_from(Lead)
        if is_active is not None:
            stmt = stmt.where(Lead.is_active == is_active)
        result = await self._db.execute(stmt)
        return result.scalar_one()

    async def count_by_status(self, *, is_active: Optional[bool] = True) -> dict[str, int]:
        """
        Count active leads grouped by pipeline status.

        Args:
            is_active: Optional active-flag filter; defaults to True.

        Returns:
            A mapping of status value (e.g., "NEW") to lead count.
        """
        stmt = select(Lead.status, func.count()).group_by(Lead.status)
        if is_active is not None:
            stmt = stmt.where(Lead.is_active == is_active)
        result = await self._db.execute(stmt)
        return {status.value: count for status, count in result.all()}

    async def count_by_source(self, *, is_active: Optional[bool] = True) -> dict[str, int]:
        """
        Count active leads grouped by acquisition source.

        Args:
            is_active: Optional active-flag filter; defaults to True.

        Returns:
            A mapping of lead source value (e.g., "WEBSITE") to lead
            count.
        """
        stmt = select(Lead.lead_source, func.count()).group_by(Lead.lead_source)
        if is_active is not None:
            stmt = stmt.where(Lead.is_active == is_active)
        result = await self._db.execute(stmt)
        return {source.value: count for source, count in result.all()}


__all__ = ["LeadRepository"]