"""
backend/app/repositories/customer_repository.py

Data-access layer for the Customer entity.

Responsibilities:
    - Encapsulate all direct database interactions (SELECT/INSERT/
      UPDATE/DELETE) for the `Customer` model behind a clean, testable
      interface.
    - Provide a single point of change if the persistence mechanism or
      query strategy evolves (e.g., adding caching, read replicas).

Design Notes:
    - This repository is strictly a data-access abstraction. It contains
      NO business rules, permission checks, or HTTP-layer concerns
      (those belong in the service and API layers). It never raises
      `HTTPException` and never imports FastAPI. Not-found conditions
      are represented as `None` return values (for single-record
      lookups) or `False`/`0` (for existence/count-style operations),
      exactly as `LeadRepository` already does — the calling service
      decides whether that should become a 404, a no-op, or something
      else.
    - All methods are async and expect an `AsyncSession` injected via
      the constructor, stored as `self._db` (matches `LeadRepository`).
    - Deletion has two tiers, matching the approved model:
        * `soft_delete()` / `restore()` flip `is_active`
          (`Customer` has no `is_deleted` column — only `is_active`,
          exactly like `Lead`/`Property`).
        * `delete()` issues a real `DELETE` and is provided for
          completeness (e.g., GDPR erasure requests); routine
          deactivation should go through `soft_delete()` instead.
    - Filtering/sorting parameters are accepted as plain, typed
      primitives (not Pydantic schemas), keeping this module decoupled
      from the API/schema layer per Clean Architecture boundaries —
      identical to `LeadRepository`.
    - `Customer.lead` / `Customer.assigned_to` / `Customer.created_by` /
      `Customer.updated_by` are declared `lazy="selectin"` on the model
      itself, so single-record reads (`get_by_id`, `get_by_email`,
      `get_by_phone`) eager-load them automatically with no explicit
      loader option required. Bulk/reporting paths (`list`, `search`,
      `export`, and the internal paginated-query helper) instead pass
      `load_relationships=False`, which attaches `noload()` for all
      four relationships — `CustomerResponse`/`CustomerListResponse`
      only ever serialize the scalar `*_id` foreign keys, never the
      related `User`/`Lead` objects, so eager-loading four extra
      relationships on every row of a paginated/exported result set
      would be pure overhead with zero payload benefit.
    - `SORTABLE_FIELDS` is an explicit allow-list mapping API-facing
      sort keys to real `InstrumentedAttribute`s. `sort_by` is looked
      up with `.get(sort_by, Customer.created_at)`, so a value outside
      this allow-list can never reach `ORDER BY` — it silently falls
      back to `created_at` instead of raising, matching the schema
      layer's documented behavior (`app/schemas/customer.py`'s
      `CustomerSearchFilters.sort_by` no longer duplicates this
      whitelist for exactly this reason).
    - `update()` mass-assignment protection: `update_data` is filtered
      against `_MUTABLE_COLUMNS`, a whitelist derived from the model's
      actual mapped columns minus `_PROTECTED_COLUMNS` (`id`,
      `created_at`, `updated_at`, `created_by_id`). This is stricter
      than `LeadRepository.update_lead()`'s plain `hasattr()` check:
      `hasattr` would also be `True` for relationship attributes
      (`lead`, `assigned_to`, ...) and the `full_name` property, so a
      caller passing those through `update_data` could otherwise
      overwrite a relationship or attempt to assign a read-only
      property. Restricting to real column names closes that gap.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional, Sequence

from sqlalchemy import Select, and_, asc, delete as sa_delete, desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, noload

from app.models.customer import (
    Customer,
    CustomerSource,
    CustomerStatus,
    CustomerType,
)


class CustomerRepository:
    """
    Repository encapsulating CRUD, search, and aggregation operations
    for the `Customer` model.

    Consumed by higher-level services (e.g., a `CustomerService`) which
    orchestrate business logic on top of these primitive persistence
    operations.
    """

    #: Explicit allow-list of client-sortable columns. Never resolve
    #: `sort_by` via `getattr(Customer, sort_by)` or any other mechanism
    #: that could reach an arbitrary attribute — only names present here
    #: can ever influence `ORDER BY`.
    SORTABLE_FIELDS: dict[str, InstrumentedAttribute] = {
        "first_name": Customer.first_name,
        "last_name": Customer.last_name,
        "email": Customer.email,
        "phone": Customer.phone,
        "customer_type": Customer.customer_type,
        "customer_source": Customer.customer_source,
        "status": Customer.status,
        "city": Customer.city,
        "preferred_city": Customer.preferred_city,
        "budget_min": Customer.budget_min,
        "budget_max": Customer.budget_max,
        "annual_income": Customer.annual_income,
        "next_followup_date": Customer.next_followup_date,
        "last_contacted_at": Customer.last_contacted_at,
        "created_at": Customer.created_at,
        "updated_at": Customer.updated_at,
    }

    #: Identity/audit columns `update()` must never mass-assign, even if
    #: present in a caller-supplied `update_data` dict.
    _PROTECTED_COLUMNS: frozenset[str] = frozenset({"id", "created_at", "updated_at", "created_by_id"})

    #: Every other real mapped column on `Customer` — the only keys
    #: `update()` will ever apply.
    _MUTABLE_COLUMNS: frozenset[str] = frozenset(Customer.__table__.columns.keys()) - _PROTECTED_COLUMNS

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
    def _base_select(self, *, load_relationships: bool = True) -> Select[tuple[Customer]]:
        """
        Build the base `SELECT` statement for `Customer`.

        Args:
            load_relationships: When `True` (default), relationships
                load via the model's own `lazy="selectin"` mapping with
                no extra options needed. When `False`, attaches
                `noload()` for `lead`, `assigned_to`, `created_by`, and
                `updated_by` to skip the four extra `selectin` queries
                entirely — intended for bulk/reporting paths whose
                response schemas never serialize those relationships.

        Returns:
            A `Select` statement targeting the `Customer` model.
        """
        stmt = select(Customer)
        if not load_relationships:
            stmt = stmt.options(
                noload(Customer.lead),
                noload(Customer.assigned_to),
                noload(Customer.created_by),
                noload(Customer.updated_by),
            )
        return stmt

    def _apply_filters(
        self,
        stmt: Select[tuple[Customer]],
        *,
        search: Optional[str] = None,
        customer_type: Optional[CustomerType] = None,
        status: Optional[CustomerStatus] = None,
        customer_source: Optional[CustomerSource] = None,
        city: Optional[str] = None,
        preferred_city: Optional[str] = None,
        lead_id: Optional[uuid.UUID] = None,
        assigned_to_id: Optional[int] = None,
        created_by_id: Optional[int] = None,
        updated_by_id: Optional[int] = None,
        budget_min: Optional[Decimal] = None,
        budget_max: Optional[Decimal] = None,
        annual_income_min: Optional[Decimal] = None,
        annual_income_max: Optional[Decimal] = None,
        date_of_birth: Optional[date] = None,
        next_followup_date: Optional[date] = None,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
        is_active: Optional[bool] = True,
    ) -> Select[tuple[Customer]]:
        """
        Apply the full enterprise search/filter set to a `Customer`
        select statement.

        Args:
            stmt: The base select statement to filter.
            search: Free-text term matched (case-insensitive) against
                `first_name`, `last_name`, `email`, `phone`, and `notes`.
            customer_type: Optional exact-match commercial-role filter.
            status: Optional exact-match lifecycle-status filter.
            customer_source: Optional exact-match acquisition-channel
                filter.
            city: Optional exact-match city filter.
            preferred_city: Optional exact-match preferred-city filter.
            lead_id: Optional exact-match originating-Lead filter.
            assigned_to_id: Optional exact-match assigned-agent filter.
            created_by_id: Optional exact-match creator filter.
            updated_by_id: Optional exact-match last-updater filter.
            budget_min: Lower bound on the customer's own `budget_min`
                column (i.e., `Customer.budget_min >= budget_min`).
            budget_max: Upper bound on the customer's own `budget_max`
                column (i.e., `Customer.budget_max <= budget_max`).
            annual_income_min: Lower bound on `annual_income`.
            annual_income_max: Upper bound on `annual_income`.
            date_of_birth: Optional exact-match date-of-birth filter.
            next_followup_date: Optional exact-match next-follow-up-date
                filter.
            created_from: Lower bound (inclusive) on `created_at`.
            created_to: Upper bound (inclusive) on `created_at`.
            is_active: Optional active-flag filter. Defaults to `True`
                so soft-deleted customers are excluded unless explicitly
                requested otherwise via `None` (include both) or
                `False` (inactive only).

        Returns:
            The filtered `Select` statement.
        """
        conditions: list[Any] = []

        if is_active is not None:
            conditions.append(Customer.is_active == is_active)
        if customer_type is not None:
            conditions.append(Customer.customer_type == customer_type)
        if status is not None:
            conditions.append(Customer.status == status)
        if customer_source is not None:
            conditions.append(Customer.customer_source == customer_source)
        if city is not None:
            conditions.append(Customer.city == city)
        if preferred_city is not None:
            conditions.append(Customer.preferred_city == preferred_city)
        if lead_id is not None:
            conditions.append(Customer.lead_id == lead_id)
        if assigned_to_id is not None:
            conditions.append(Customer.assigned_to_id == assigned_to_id)
        if created_by_id is not None:
            conditions.append(Customer.created_by_id == created_by_id)
        if updated_by_id is not None:
            conditions.append(Customer.updated_by_id == updated_by_id)
        if budget_min is not None:
            conditions.append(Customer.budget_min >= budget_min)
        if budget_max is not None:
            conditions.append(Customer.budget_max <= budget_max)
        if annual_income_min is not None:
            conditions.append(Customer.annual_income >= annual_income_min)
        if annual_income_max is not None:
            conditions.append(Customer.annual_income <= annual_income_max)
        if date_of_birth is not None:
            conditions.append(Customer.date_of_birth == date_of_birth)
        if next_followup_date is not None:
            conditions.append(Customer.next_followup_date == next_followup_date)
        if created_from is not None:
            conditions.append(Customer.created_at >= created_from)
        if created_to is not None:
            conditions.append(Customer.created_at <= created_to)

        if conditions:
            stmt = stmt.where(and_(*conditions))

        if search:
            term = f"%{search}%"
            stmt = stmt.where(
                Customer.first_name.ilike(term)
                | Customer.last_name.ilike(term)
                | Customer.email.ilike(term)
                | Customer.phone.ilike(term)
                | Customer.notes.ilike(term)
            )

        return stmt

    def _apply_sorting(
        self,
        stmt: Select[tuple[Customer]],
        *,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> Select[tuple[Customer]]:
        """
        Apply sorting to a `Customer` select statement.

        Args:
            stmt: The select statement to sort.
            sort_by: API-facing sort key, resolved exclusively against
                `SORTABLE_FIELDS`. Any key not present in that
                allow-list falls back to `created_at` — this can never
                result in an arbitrary `ORDER BY` expression.
            sort_order: `"asc"` or `"desc"` (case-insensitive). Falls
                back to descending for any unrecognized value.

        Returns:
            The sorted `Select` statement.
        """
        column = self.SORTABLE_FIELDS.get(sort_by, Customer.created_at)
        direction = asc if sort_order.lower() == "asc" else desc
        return stmt.order_by(direction(column))

    async def _paginated_query(
        self,
        *,
        page: int,
        page_size: int,
        sort_by: str,
        sort_order: str,
        load_relationships: bool,
        filters: dict[str, Any],
    ) -> tuple[Sequence[Customer], int]:
        """
        Shared count + fetch + sort + paginate implementation used by
        both `list()` and `search()`, so the two never duplicate this
        logic.

        Args:
            page: 1-indexed page number.
            page_size: Number of records per page.
            sort_by: API-facing sort key (see `_apply_sorting`).
            sort_order: `"asc"` or `"desc"`.
            load_relationships: Forwarded to `_base_select()`.
            filters: Keyword arguments forwarded to `_apply_filters()`.

        Returns:
            A tuple of `(items for the requested page, total matching
            count across all pages)`.
        """
        count_stmt = self._apply_filters(select(func.count()).select_from(Customer), **filters)
        total = (await self._db.execute(count_stmt)).scalar_one()

        stmt = self._apply_filters(self._base_select(load_relationships=load_relationships), **filters)
        stmt = self._apply_sorting(stmt, sort_by=sort_by, sort_order=sort_order)
        offset = (page - 1) * page_size
        stmt = stmt.offset(offset).limit(page_size)

        result = await self._db.execute(stmt)
        items = result.scalars().all()
        return items, total

    # ----------------------------------------------------------------
    # Create
    # ----------------------------------------------------------------
    async def create(self, customer: Customer) -> Customer:
        """
        Persist a new customer record.

        Args:
            customer: A transient `Customer` instance (not yet added to
                the session) with all required fields populated by the
                caller.

        Returns:
            The persisted `Customer` instance, refreshed with any
            server-generated values (`id`, `created_at`, `updated_at`,
            enum defaults, etc.).
        """
        self._db.add(customer)
        await self._db.commit()
        await self._db.refresh(customer)
        return customer

    # ----------------------------------------------------------------
    # Read - Single Record
    # ----------------------------------------------------------------
    async def get_by_id(
        self,
        customer_id: uuid.UUID,
        *,
        load_relationships: bool = True,
        is_active: Optional[bool] = None,
    ) -> Optional[Customer]:
        """
        Retrieve a customer by primary key.

        Args:
            customer_id: The UUID of the customer to retrieve.
            load_relationships: See `_base_select()`.
            is_active: Optional active-flag filter. Defaults to `None`
                (returns the record regardless of `is_active`), since
                primary-key lookups are also used to fetch soft-deleted
                records (e.g., for `restore()` flows).

        Returns:
            The matching `Customer` instance, or `None` if no match is
            found.
        """
        stmt = self._base_select(load_relationships=load_relationships).where(Customer.id == customer_id)
        if is_active is not None:
            stmt = stmt.where(Customer.is_active == is_active)
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_email(
        self, email: str, *, is_active: Optional[bool] = None
    ) -> Optional[Customer]:
        """
        Retrieve a customer by exact (case-sensitive) email address.

        Args:
            email: The email address to search for.
            is_active: Optional active-flag filter; defaults to `None`.

        Returns:
            The matching `Customer` instance, or `None` if no match is
            found.
        """
        stmt = self._base_select().where(Customer.email == email)
        if is_active is not None:
            stmt = stmt.where(Customer.is_active == is_active)
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_phone(
        self, phone: str, *, is_active: Optional[bool] = None
    ) -> Optional[Customer]:
        """
        Retrieve a customer by primary phone number.

        Unlike `email` (unique at the database level), `phone` has no
        unique constraint on the approved model — two customers may
        legitimately share a phone number (e.g., a shared household or
        office line). This method therefore does not assume at most
        one match: it deterministically returns the most recently
        created matching customer via `ORDER BY created_at DESC LIMIT
        1`, instead of using `scalar_one_or_none()`, which would raise
        `MultipleResultsFound` the moment two customers share a phone
        number.

        Args:
            phone: The phone number to search for.
            is_active: Optional active-flag filter; defaults to `None`.

        Returns:
            The most recently created matching `Customer` instance, or
            `None` if no match is found.
        """
        stmt = self._base_select().where(Customer.phone == phone)
        if is_active is not None:
            stmt = stmt.where(Customer.is_active == is_active)
        stmt = stmt.order_by(desc(Customer.created_at)).limit(1)
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def exists(self, customer_id: uuid.UUID) -> bool:
        """
        Check whether a customer with the given primary key exists,
        regardless of `is_active`.

        Args:
            customer_id: The UUID to check.

        Returns:
            `True` if a matching row exists, `False` otherwise.
        """
        stmt = select(select(Customer.id).where(Customer.id == customer_id).exists())
        result = await self._db.execute(stmt)
        return bool(result.scalar())

    # ----------------------------------------------------------------
    # Read - Collections
    # ----------------------------------------------------------------
    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        is_active: Optional[bool] = True,
        load_relationships: bool = False,
    ) -> tuple[Sequence[Customer], int]:
        """
        Retrieve an unfiltered, paginated page of customers.

        Args:
            page: 1-indexed page number.
            page_size: Number of records per page.
            sort_by: API-facing sort key (see `SORTABLE_FIELDS`).
            sort_order: `"asc"` or `"desc"`.
            is_active: Optional active-flag filter; defaults to `True`.
            load_relationships: See `_base_select()`. Defaults to
                `False` for this bulk path.

        Returns:
            A tuple of `(customers for the requested page, total
            matching count across all pages)`.
        """
        return await self._paginated_query(
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
            load_relationships=load_relationships,
            filters={"is_active": is_active},
        )

    async def search(
        self,
        *,
        search: Optional[str] = None,
        customer_type: Optional[CustomerType] = None,
        status: Optional[CustomerStatus] = None,
        customer_source: Optional[CustomerSource] = None,
        city: Optional[str] = None,
        preferred_city: Optional[str] = None,
        lead_id: Optional[uuid.UUID] = None,
        assigned_to_id: Optional[int] = None,
        created_by_id: Optional[int] = None,
        updated_by_id: Optional[int] = None,
        budget_min: Optional[Decimal] = None,
        budget_max: Optional[Decimal] = None,
        annual_income_min: Optional[Decimal] = None,
        annual_income_max: Optional[Decimal] = None,
        date_of_birth: Optional[date] = None,
        next_followup_date: Optional[date] = None,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
        is_active: Optional[bool] = True,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        load_relationships: bool = False,
    ) -> tuple[Sequence[Customer], int]:
        """
        Retrieve a filtered, paginated page of customers matching the
        full enterprise search/filter set (see `_apply_filters()` for
        the meaning of each parameter).

        Returns:
            A tuple of `(customers for the requested page, total
            matching count across all pages)`.
        """
        filters: dict[str, Any] = {
            "search": search,
            "customer_type": customer_type,
            "status": status,
            "customer_source": customer_source,
            "city": city,
            "preferred_city": preferred_city,
            "lead_id": lead_id,
            "assigned_to_id": assigned_to_id,
            "created_by_id": created_by_id,
            "updated_by_id": updated_by_id,
            "budget_min": budget_min,
            "budget_max": budget_max,
            "annual_income_min": annual_income_min,
            "annual_income_max": annual_income_max,
            "date_of_birth": date_of_birth,
            "next_followup_date": next_followup_date,
            "created_from": created_from,
            "created_to": created_to,
            "is_active": is_active,
        }
        return await self._paginated_query(
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
            load_relationships=load_relationships,
            filters=filters,
        )

    # ----------------------------------------------------------------
    # Update
    # ----------------------------------------------------------------
    async def update(self, customer: Customer, update_data: dict[str, Any]) -> Customer:
        """
        Apply a set of attribute updates to an already-tracked
        `Customer` instance and persist the changes.

        Args:
            customer: A `Customer` instance retrieved from this session
                (e.g., via `get_by_id`) with updates to be applied.
            update_data: Mapping of attribute names to new values. Only
                keys present in `_MUTABLE_COLUMNS` are applied — any
                other key (including `id`, `created_at`, `updated_at`,
                `created_by_id`, or a relationship/property name) is
                silently ignored, preventing mass assignment.

        Returns:
            The updated `Customer` instance, refreshed with the latest
            database state (e.g., the new `updated_at` timestamp).
        """
        for field_name, value in update_data.items():
            if field_name in self._MUTABLE_COLUMNS:
                setattr(customer, field_name, value)

        self._db.add(customer)
        await self._db.commit()
        await self._db.refresh(customer)
        return customer

    async def assign_customer(self, customer_id: uuid.UUID, assigned_to_id: int) -> Optional[Customer]:
        """
        Assign a customer to a sales agent.

        Args:
            customer_id: The UUID of the customer to update.
            assigned_to_id: The internal User ID of the agent to assign.

        Returns:
            The updated `Customer` instance, or `None` if no customer
            with the given ID exists.
        """
        stmt = (
            update(Customer)
            .where(Customer.id == customer_id)
            .values(assigned_to_id=assigned_to_id)
            .returning(Customer)
            .execution_options(synchronize_session="fetch")
        )
        result = await self._db.execute(stmt)
        await self._db.commit()
        return result.scalar_one_or_none()

    async def unassign_customer(self, customer_id: uuid.UUID) -> Optional[Customer]:
        """
        Clear the assigned agent on a customer.

        Args:
            customer_id: The UUID of the customer to update.

        Returns:
            The updated `Customer` instance, or `None` if no customer
            with the given ID exists.
        """
        stmt = (
            update(Customer)
            .where(Customer.id == customer_id)
            .values(assigned_to_id=None)
            .returning(Customer)
            .execution_options(synchronize_session="fetch")
        )
        result = await self._db.execute(stmt)
        await self._db.commit()
        return result.scalar_one_or_none()

    async def update_status(self, customer_id: uuid.UUID, status: CustomerStatus) -> Optional[Customer]:
        """
        Update the lifecycle status of a customer.

        Args:
            customer_id: The UUID of the customer to update.
            status: The new `CustomerStatus` value to set.

        Returns:
            The updated `Customer` instance, or `None` if no customer
            with the given ID exists.
        """
        stmt = (
            update(Customer)
            .where(Customer.id == customer_id)
            .values(status=status)
            .returning(Customer)
            .execution_options(synchronize_session="fetch")
        )
        result = await self._db.execute(stmt)
        await self._db.commit()
        return result.scalar_one_or_none()

    async def update_followup(
        self,
        customer_id: uuid.UUID,
        *,
        next_followup_date: Optional[date],
        last_contacted_at: Optional[datetime] = None,
    ) -> Optional[Customer]:
        """
        Update a customer's next scheduled follow-up date and,
        optionally, the timestamp of the most recent contact.

        Args:
            customer_id: The UUID of the customer to update.
            next_followup_date: The new next-follow-up date. Pass
                `None` to clear a previously scheduled follow-up.
            last_contacted_at: Optional new last-contacted timestamp,
                typically set to "now" by the caller when this
                follow-up update represents an interaction that just
                occurred.

        Returns:
            The updated `Customer` instance, or `None` if no customer
            with the given ID exists.
        """
        values: dict[str, Any] = {"next_followup_date": next_followup_date}
        if last_contacted_at is not None:
            values["last_contacted_at"] = last_contacted_at

        stmt = (
            update(Customer)
            .where(Customer.id == customer_id)
            .values(**values)
            .returning(Customer)
            .execution_options(synchronize_session="fetch")
        )
        result = await self._db.execute(stmt)
        await self._db.commit()
        return result.scalar_one_or_none()

    # ----------------------------------------------------------------
    # Delete
    # ----------------------------------------------------------------
    async def soft_delete(self, customer_id: uuid.UUID) -> Optional[Customer]:
        """
        Soft-delete a customer by setting `is_active` to `False`. Does
        not issue a `DELETE` statement; the record is preserved for
        auditing/history purposes.

        Args:
            customer_id: The UUID of the customer to soft-delete.

        Returns:
            The updated `Customer` instance with `is_active=False`, or
            `None` if no customer with the given ID exists.
        """
        stmt = (
            update(Customer)
            .where(Customer.id == customer_id)
            .values(is_active=False)
            .returning(Customer)
            .execution_options(synchronize_session="fetch")
        )
        result = await self._db.execute(stmt)
        await self._db.commit()
        return result.scalar_one_or_none()

    async def restore(self, customer_id: uuid.UUID) -> Optional[Customer]:
        """
        Restore a previously soft-deleted customer by setting
        `is_active` back to `True`.

        Args:
            customer_id: The UUID of the customer to restore.

        Returns:
            The updated `Customer` instance with `is_active=True`, or
            `None` if no customer with the given ID exists.
        """
        stmt = (
            update(Customer)
            .where(Customer.id == customer_id)
            .values(is_active=True)
            .returning(Customer)
            .execution_options(synchronize_session="fetch")
        )
        result = await self._db.execute(stmt)
        await self._db.commit()
        return result.scalar_one_or_none()

    async def delete(self, customer_id: uuid.UUID) -> bool:
        """
        Permanently delete a customer row. Provided for completeness
        (e.g., a GDPR/data-erasure request); routine deactivation
        should use `soft_delete()` instead, which preserves history.

        Args:
            customer_id: The UUID of the customer to delete.

        Returns:
            `True` if a row was deleted, `False` if no customer with
            the given ID existed.
        """
        stmt = sa_delete(Customer).where(Customer.id == customer_id)
        result = await self._db.execute(stmt)
        await self._db.commit()
        return result.rowcount > 0

    # ----------------------------------------------------------------
    # Aggregation / Statistics
    # ----------------------------------------------------------------
    async def get_statistics(
        self,
        *,
        period_start: Optional[date] = None,
        period_end: Optional[date] = None,
        top_cities_limit: int = 10,
        is_active: Optional[bool] = True,
    ) -> dict[str, Any]:
        """
        Compute aggregate Customer analytics entirely via SQL
        aggregation (no rows are ever loaded into Python memory).

        Args:
            period_start: Optional inclusive lower bound on
                `created_at` (compared by date).
            period_end: Optional inclusive upper bound on `created_at`
                (compared by date).
            top_cities_limit: Maximum number of cities to include in
                `customers_by_city`, ordered by descending count.
            is_active: Optional active-flag filter; defaults to `True`
                so statistics reflect only active customers unless
                explicitly requested otherwise.

        Returns:
            A dict with keys matching
            `CustomerStatisticsResponse`: `total_customers`,
            `customers_by_status`, `customers_by_type`,
            `customers_by_source`, `customers_by_city`,
            `average_annual_income`, `average_budget_max`,
            `conversion_rate_from_leads`, `period_start`, `period_end`.
        """
        conditions: list[Any] = []
        if is_active is not None:
            conditions.append(Customer.is_active == is_active)
        if period_start is not None:
            conditions.append(func.date(Customer.created_at) >= period_start)
        if period_end is not None:
            conditions.append(func.date(Customer.created_at) <= period_end)

        def _scope(stmt: Select[Any]) -> Select[Any]:
            return stmt.where(and_(*conditions)) if conditions else stmt

        total_customers = (
            await self._db.execute(_scope(select(func.count()).select_from(Customer)))
        ).scalar_one()

        status_rows = (
            await self._db.execute(
                _scope(select(Customer.status, func.count()).group_by(Customer.status))
            )
        ).all()
        customers_by_status = {row_status.value: count for row_status, count in status_rows}

        type_rows = (
            await self._db.execute(
                _scope(select(Customer.customer_type, func.count()).group_by(Customer.customer_type))
            )
        ).all()
        customers_by_type = {row_type.value: count for row_type, count in type_rows}

        source_rows = (
            await self._db.execute(
                _scope(select(Customer.customer_source, func.count()).group_by(Customer.customer_source))
            )
        ).all()
        customers_by_source = {row_source.value: count for row_source, count in source_rows}

        city_stmt = _scope(
            select(Customer.city, func.count().label("city_count")).where(Customer.city.is_not(None))
        )
        city_stmt = city_stmt.group_by(Customer.city).order_by(desc("city_count")).limit(top_cities_limit)
        city_rows = (await self._db.execute(city_stmt)).all()
        customers_by_city = {city_name: count for city_name, count in city_rows}

        averages_row = (
            await self._db.execute(
                _scope(select(func.avg(Customer.annual_income), func.avg(Customer.budget_max)))
            )
        ).one()
        average_annual_income, average_budget_max = averages_row

        conversion_row = (
            await self._db.execute(_scope(select(func.count(Customer.lead_id), func.count())))
        ).one()
        customers_from_leads, conversion_total = conversion_row
        conversion_rate_from_leads = (
            round((customers_from_leads / conversion_total) * 100, 2) if conversion_total else None
        )

        return {
            "total_customers": total_customers,
            "customers_by_status": customers_by_status,
            "customers_by_type": customers_by_type,
            "customers_by_source": customers_by_source,
            "customers_by_city": customers_by_city,
            "average_annual_income": average_annual_income,
            "average_budget_max": average_budget_max,
            "conversion_rate_from_leads": conversion_rate_from_leads,
            "period_start": period_start,
            "period_end": period_end,
        }

    # ----------------------------------------------------------------
    # Export
    # ----------------------------------------------------------------
    async def export(
        self,
        *,
        search: Optional[str] = None,
        customer_type: Optional[CustomerType] = None,
        status: Optional[CustomerStatus] = None,
        customer_source: Optional[CustomerSource] = None,
        city: Optional[str] = None,
        preferred_city: Optional[str] = None,
        lead_id: Optional[uuid.UUID] = None,
        assigned_to_id: Optional[int] = None,
        created_by_id: Optional[int] = None,
        updated_by_id: Optional[int] = None,
        budget_min: Optional[Decimal] = None,
        budget_max: Optional[Decimal] = None,
        annual_income_min: Optional[Decimal] = None,
        annual_income_max: Optional[Decimal] = None,
        date_of_birth: Optional[date] = None,
        next_followup_date: Optional[date] = None,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
        is_active: Optional[bool] = True,
        max_rows: Optional[int] = 50_000,
    ) -> Sequence[Customer]:
        """
        Fetch every customer matching the given filters, for a caller
        (the service layer) to serialize into a downloadable file.

        This method deliberately returns fully-loaded `Customer`
        domain objects rather than file bytes: building CSV/XLSX output
        is response formatting, which belongs in the service layer, not
        the repository (see module docstring / Clean Architecture
        boundary). Whether CSV, XLSX, or both are exposed as export
        formats is entirely a service/schema-layer decision — this
        method has no notion of file format at all.

        Args:
            (see `_apply_filters()` for the meaning of each filter
            parameter.)
            max_rows: Safety cap on the number of rows returned in a
                single export, to bound memory/response size. Pass
                `None` for no cap.

        Returns:
            A sequence of matching `Customer` instances, ordered
            oldest-first, with relationships skipped via
            `load_relationships=False` (export columns are drawn from
            scalar attributes only).
        """
        stmt = self._apply_filters(
            self._base_select(load_relationships=False),
            search=search,
            customer_type=customer_type,
            status=status,
            customer_source=customer_source,
            city=city,
            preferred_city=preferred_city,
            lead_id=lead_id,
            assigned_to_id=assigned_to_id,
            created_by_id=created_by_id,
            updated_by_id=updated_by_id,
            budget_min=budget_min,
            budget_max=budget_max,
            annual_income_min=annual_income_min,
            annual_income_max=annual_income_max,
            date_of_birth=date_of_birth,
            next_followup_date=next_followup_date,
            created_from=created_from,
            created_to=created_to,
            is_active=is_active,
        ).order_by(asc(Customer.created_at))

        if max_rows is not None:
            stmt = stmt.limit(max_rows)

        result = await self._db.execute(stmt)
        return result.scalars().all()


__all__ = ["CustomerRepository"]