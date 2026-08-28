"""
backend/app/repositories/booking_repository.py

Data-access layer for the Booking entity.

Responsibilities:
    - Encapsulate all direct database interactions (SELECT/INSERT/
      UPDATE) for the `Booking` model behind a clean, testable
      interface.
    - Provide a single point of change if the persistence mechanism or
      query strategy evolves (e.g., adding caching, read replicas).

Design Notes:
    - This repository is strictly a data-access abstraction. It contains
      NO business rules, permission checks, or HTTP-layer concerns
      (those belong in the service and API layers). It never raises
      `HTTPException` and never imports FastAPI, exactly as
      `LeadRepository` / `CustomerRepository` do.
    - All methods are async and expect an `AsyncSession` injected via
      the constructor, stored as `self._db` (matches `LeadRepository` /
      `CustomerRepository`).
    - Deletion is soft-only: `soft_delete()` flips `is_active` to
      `False` rather than issuing a `DELETE` statement, preserving
      historical/audit data — matching `Lead.is_active` semantics.
    - Filtering/sorting parameters are accepted as plain, typed
      primitives (not Pydantic schemas), keeping this module decoupled
      from the API/schema layer per Clean Architecture boundaries.
    - `SORTABLE_FIELDS` is an explicit allow-list mapping API-facing
      sort keys to real `InstrumentedAttribute`s, resolved via
      `.get(sort_by, Booking.created_at)` so a value outside this
      allow-list can never reach `ORDER BY` — mirrors
      `CustomerRepository.SORTABLE_FIELDS`.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Optional, Sequence

from sqlalchemy import Select, and_, asc, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, selectinload

from app.models.booking import (
    Booking,
    BookingPaymentStatus,
    BookingStatus,
)


class BookingRepository:
    """
    Repository encapsulating CRUD, search, and aggregation operations
    for the `Booking` model.

    Consumed by higher-level services (e.g., a `BookingService`) which
    orchestrate business logic on top of these primitive persistence
    operations.
    """

    #: Explicit allow-list of client-sortable columns. Never resolve
    #: `sort_by` via `getattr(Booking, sort_by)` or any other mechanism
    #: that could reach an arbitrary attribute — only names present here
    #: can ever influence `ORDER BY`.
    SORTABLE_FIELDS: dict[str, InstrumentedAttribute] = {
        "booking_date": Booking.booking_date,
        "booking_amount": Booking.booking_amount,
        "token_amount": Booking.token_amount,
        "status": Booking.status,
        "payment_status": Booking.payment_status,
        "next_follow_up": Booking.next_follow_up,
        "created_at": Booking.created_at,
        "updated_at": Booking.updated_at,
    }

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
    def _base_select(self, *, with_relationships: bool = False) -> Select[tuple[Booking]]:
        """
        Build the base `SELECT` statement for `Booking`, optionally
        eager-loading its `customer`/`property`/`lead`/`agent`/`creator`
        relationships to avoid N+1 queries when callers will access
        those attributes.

        Args:
            with_relationships: If True, eager-loads `customer`,
                `property`, `lead`, `agent`, and `creator` via
                `selectinload()`.

        Returns:
            A `Select` statement targeting the `Booking` model.
        """
        stmt = select(Booking)
        if with_relationships:
            stmt = stmt.options(
                selectinload(Booking.customer),
                selectinload(Booking.property),
                selectinload(Booking.lead),
                selectinload(Booking.agent),
                selectinload(Booking.creator),
            )
        return stmt

    def _apply_filters(
        self,
        stmt: Select[tuple[Booking]],
        *,
        status: Optional[BookingStatus] = None,
        payment_status: Optional[BookingPaymentStatus] = None,
        customer_id: Optional[uuid.UUID] = None,
        property_id: Optional[int] = None,
        lead_id: Optional[uuid.UUID] = None,
        agent_id: Optional[int] = None,
        booking_date_from: Optional[date] = None,
        booking_date_to: Optional[date] = None,
        search: Optional[str] = None,
        is_active: Optional[bool] = True,
    ) -> Select[tuple[Booking]]:
        """
        Apply a common set of equality, range, and search filters to a
        `Booking` select statement.

        Args:
            stmt: The base select statement to filter.
            status: Optional exact-match lifecycle status filter.
            payment_status: Optional exact-match payment status filter.
            customer_id: Optional exact-match Customer filter.
            property_id: Optional exact-match Property filter.
            lead_id: Optional exact-match originating-Lead filter.
            agent_id: Optional exact-match assigned agent filter.
            booking_date_from: Lower bound (inclusive) on `booking_date`.
            booking_date_to: Upper bound (inclusive) on `booking_date`.
            search: Optional free-text term matched (case-insensitive)
                against `remarks` and `payment_reference`.
            is_active: Optional active-flag filter. Defaults to `True`
                so inactive (soft-deleted) bookings are excluded unless
                explicitly requested otherwise via `None`.

        Returns:
            The filtered `Select` statement.
        """
        conditions: list[Any] = []

        if is_active is not None:
            conditions.append(Booking.is_active == is_active)
        if status is not None:
            conditions.append(Booking.status == status)
        if payment_status is not None:
            conditions.append(Booking.payment_status == payment_status)
        if customer_id is not None:
            conditions.append(Booking.customer_id == customer_id)
        if property_id is not None:
            conditions.append(Booking.property_id == property_id)
        if lead_id is not None:
            conditions.append(Booking.lead_id == lead_id)
        if agent_id is not None:
            conditions.append(Booking.agent_id == agent_id)
        if booking_date_from is not None:
            conditions.append(Booking.booking_date >= booking_date_from)
        if booking_date_to is not None:
            conditions.append(Booking.booking_date <= booking_date_to)

        if conditions:
            stmt = stmt.where(and_(*conditions))

        if search:
            term = f"%{search}%"
            stmt = stmt.where(
                or_(
                    Booking.remarks.ilike(term),
                    Booking.payment_reference.ilike(term),
                )
            )

        return stmt

    def _apply_sorting(
        self,
        stmt: Select[tuple[Booking]],
        *,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> Select[tuple[Booking]]:
        """
        Apply sorting to a `Booking` select statement.

        Args:
            stmt: The select statement to sort.
            sort_by: API-facing sort key, resolved exclusively against
                `SORTABLE_FIELDS`. Any key not present in that
                allow-list falls back to `created_at`.
            sort_order: "asc" or "desc" (case-insensitive). Falls back
                to descending for any unrecognized value.

        Returns:
            The sorted `Select` statement.
        """
        column = self.SORTABLE_FIELDS.get(sort_by, Booking.created_at)
        direction = asc if sort_order.lower() == "asc" else desc
        return stmt.order_by(direction(column))

    # ----------------------------------------------------------------
    # Create
    # ----------------------------------------------------------------
    async def create_booking(self, booking: Booking) -> Booking:
        """
        Persist a new booking record.

        Args:
            booking: A transient `Booking` instance (not yet added to
                the session) with all required fields populated by the
                caller.

        Returns:
            The persisted `Booking` instance, refreshed with any
            server-generated values (id, created_at, updated_at, etc.).
        """
        self._db.add(booking)
        await self._db.commit()
        await self._db.refresh(booking)
        return booking

    # ----------------------------------------------------------------
    # Read - Single Record
    # ----------------------------------------------------------------
    async def get_by_id(self, booking_id: uuid.UUID) -> Optional[Booking]:
        """
        Retrieve a booking by its primary key.

        Args:
            booking_id: The UUID of the booking to retrieve.

        Returns:
            The matching `Booking` instance, or None if no match is
            found.
        """
        stmt = self._base_select(with_relationships=True).where(Booking.id == booking_id)
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_customer_and_property(
        self, customer_id: uuid.UUID, property_id: int
    ) -> Optional[Booking]:
        """
        Retrieve the most recently created booking for a given
        Customer/Property pair.

        Args:
            customer_id: The UUID of the customer to search for.
            property_id: The integer ID of the property to search for.

        Returns:
            The most recently created matching `Booking` instance, or
            None if no match is found.
        """
        stmt = (
            select(Booking)
            .where(
                and_(
                    Booking.customer_id == customer_id,
                    Booking.property_id == property_id,
                )
            )
            .order_by(desc(Booking.created_at))
            .limit(1)
        )
        result = await self._db.execute(stmt)
        return result.scalars().first()

    # ----------------------------------------------------------------
    # Read - Listing / Search
    # ----------------------------------------------------------------
    async def list_bookings(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: Optional[BookingStatus] = None,
        payment_status: Optional[BookingPaymentStatus] = None,
        customer_id: Optional[uuid.UUID] = None,
        property_id: Optional[int] = None,
        lead_id: Optional[uuid.UUID] = None,
        agent_id: Optional[int] = None,
        booking_date_from: Optional[date] = None,
        booking_date_to: Optional[date] = None,
        search: Optional[str] = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        is_active: Optional[bool] = True,
    ) -> tuple[Sequence[Booking], int]:
        """
        Retrieve a paginated, filtered, and sorted list of bookings,
        alongside the total count of matching records.

        Args:
            page: 1-indexed page number to retrieve.
            page_size: Number of records to return per page.
            status: Optional lifecycle status filter.
            payment_status: Optional payment status filter.
            customer_id: Optional Customer filter.
            property_id: Optional Property filter.
            lead_id: Optional originating-Lead filter.
            agent_id: Optional assigned agent filter.
            booking_date_from: Lower bound (inclusive) on `booking_date`.
            booking_date_to: Upper bound (inclusive) on `booking_date`.
            search: Optional free-text search term (remarks/payment_reference).
            sort_by: Column name to sort by.
            sort_order: "asc" or "desc".
            is_active: Optional active-flag filter; defaults to True.

        Returns:
            A tuple of (matching bookings for the requested page, total
            count of matching records across all pages).
        """
        base_stmt = self._apply_filters(
            select(Booking),
            status=status,
            payment_status=payment_status,
            customer_id=customer_id,
            property_id=property_id,
            lead_id=lead_id,
            agent_id=agent_id,
            booking_date_from=booking_date_from,
            booking_date_to=booking_date_to,
            search=search,
            is_active=is_active,
        )

        count_stmt = select(func.count()).select_from(base_stmt.subquery())
        total_result = await self._db.execute(count_stmt)
        total = total_result.scalar_one()

        data_stmt = self._apply_filters(
            self._base_select(with_relationships=True),
            status=status,
            payment_status=payment_status,
            customer_id=customer_id,
            property_id=property_id,
            lead_id=lead_id,
            agent_id=agent_id,
            booking_date_from=booking_date_from,
            booking_date_to=booking_date_to,
            search=search,
            is_active=is_active,
        )
        data_stmt = self._apply_sorting(data_stmt, sort_by=sort_by, sort_order=sort_order)
        data_stmt = data_stmt.offset((page - 1) * page_size).limit(page_size)

        result = await self._db.execute(data_stmt)
        bookings = result.scalars().all()

        return bookings, total

    async def search(
        self,
        term: str,
        *,
        page: int = 1,
        page_size: int = 20,
        is_active: Optional[bool] = True,
    ) -> tuple[Sequence[Booking], int]:
        """
        Perform a free-text search for bookings across remarks and
        payment reference fields using case-insensitive partial
        matching.

        Args:
            term: The search term to match.
            page: 1-indexed page number to retrieve.
            page_size: Number of records to return per page.
            is_active: Optional active-flag filter; defaults to True.

        Returns:
            A tuple of (matching bookings for the requested page, total
            count of matching records across all pages).
        """
        return await self.list_bookings(
            page=page,
            page_size=page_size,
            search=term,
            is_active=is_active,
        )

    async def upcoming_followups(self, *, reference_date: Optional[date] = None) -> Sequence[Booking]:
        """
        Retrieve all active bookings whose next follow-up date is due
        (i.e., on or before the reference date).

        Args:
            reference_date: The date to compare against. Defaults to
                today if not supplied.

        Returns:
            A sequence of matching `Booking` instances, ordered by
            `next_follow_up` ascending (most overdue first).
        """
        effective_date = reference_date or date.today()
        stmt = (
            self._base_select(with_relationships=True)
            .where(
                and_(
                    Booking.next_follow_up.is_not(None),
                    Booking.next_follow_up <= effective_date,
                    Booking.is_active.is_(True),
                )
            )
            .order_by(asc(Booking.next_follow_up))
        )
        result = await self._db.execute(stmt)
        return result.scalars().all()

    # ----------------------------------------------------------------
    # Update
    # ----------------------------------------------------------------
    async def update_booking(self, booking: Booking, update_data: dict[str, Any]) -> Booking:
        """
        Apply a set of attribute updates to an already-tracked `Booking`
        instance and persist the changes.

        Args:
            booking: A `Booking` instance retrieved from this session
                (e.g., via `get_by_id`) with updates to be applied.
            update_data: Mapping of attribute names to new values. Only
                keys present on the `Booking` model are applied.

        Returns:
            The updated `Booking` instance, refreshed with the latest
            database state (e.g., updated `updated_at` timestamp).
        """
        for field_name, value in update_data.items():
            if hasattr(booking, field_name):
                setattr(booking, field_name, value)

        self._db.add(booking)
        await self._db.commit()
        await self._db.refresh(booking)
        return booking

    async def soft_delete(self, booking_id: uuid.UUID) -> Optional[Booking]:
        booking = await self.get_by_id(booking_id)
        if booking is None:
            return None
        booking.is_active = False
        await self._db.commit()
        await self._db.refresh(booking)
        return booking

    async def assign_agent(self, booking_id: uuid.UUID, agent_id: int) -> Optional[Booking]:
        booking = await self.get_by_id(booking_id)
        if booking is None:
            return None
        booking.agent_id = agent_id
        await self._db.commit()
        await self._db.refresh(booking)
        return booking

    async def change_status(self, booking_id: uuid.UUID, status: BookingStatus) -> Optional[Booking]:
        booking = await self.get_by_id(booking_id)
        if booking is None:
            return None
        booking.status = status
        await self._db.commit()
        await self._db.refresh(booking)
        return booking

    async def change_payment_status(self, booking_id: uuid.UUID, payment_status: BookingPaymentStatus) -> Optional[Booking]:
        booking = await self.get_by_id(booking_id)
        if booking is None:
            return None
        booking.payment_status = payment_status
        await self._db.commit()
        await self._db.refresh(booking)
        return booking

    # ----------------------------------------------------------------
    # Aggregation / Counting
    # ----------------------------------------------------------------
    async def count(self, *, is_active: Optional[bool] = True) -> int:
        """
        Count total bookings matching the given active-flag filter.

        Args:
            is_active: Optional active-flag filter; defaults to True
                (counts only active bookings).

        Returns:
            The total number of matching bookings.
        """
        stmt = select(func.count()).select_from(Booking)
        if is_active is not None:
            stmt = stmt.where(Booking.is_active == is_active)
        result = await self._db.execute(stmt)
        return result.scalar_one()

    async def count_by_status(self, *, is_active: Optional[bool] = True) -> dict[str, int]:
        """
        Count active bookings grouped by lifecycle status.

        Args:
            is_active: Optional active-flag filter; defaults to True.

        Returns:
            A mapping of status value (e.g., "PENDING") to booking
            count.
        """
        stmt = select(Booking.status, func.count()).group_by(Booking.status)
        if is_active is not None:
            stmt = stmt.where(Booking.is_active == is_active)
        result = await self._db.execute(stmt)
        return {status.value: count for status, count in result.all()}

    async def count_by_payment_status(self, *, is_active: Optional[bool] = True) -> dict[str, int]:
        """
        Count active bookings grouped by payment status.

        Args:
            is_active: Optional active-flag filter; defaults to True.

        Returns:
            A mapping of payment status value (e.g., "PAID") to booking
            count.
        """
        stmt = select(Booking.payment_status, func.count()).group_by(Booking.payment_status)
        if is_active is not None:
            stmt = stmt.where(Booking.is_active == is_active)
        result = await self._db.execute(stmt)
        return {payment_status.value: count for payment_status, count in result.all()}


__all__ = ["BookingRepository"]