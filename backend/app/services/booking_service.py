"""
backend/app/services/booking_service.py

Business/service layer for the Booking module.

Responsibilities:
    - Orchestrate `BookingRepository` operations behind business rules
      (existence checks, duplicate-booking prevention, amount
      validation, status/payment-status transition rules).
    - Own all cross-entity validation (Customer/Property/Lead/Agent
      existence) that the repository layer intentionally does not
      perform.
    - Raise domain exceptions only (`app.core.exceptions`); this layer
      never imports FastAPI or raises `HTTPException` directly, exactly
      as `LeadService` / `CustomerService` do. The API layer is
      responsible for translating these into HTTP responses (via the
      global exception handlers already registered for the Lead/
      Customer modules).

Design Notes:
    - `_STATUS_TRANSITIONS` / `_PAYMENT_STATUS_TRANSITIONS` are
      explicit allow-lists of valid next-states, mirroring the
      allow-list pattern already used for `BookingRepository.
      SORTABLE_FIELDS` — an unlisted transition can never succeed.
    - All mutating operations re-check `Booking.is_active` before
      applying changes; inactive (soft-deleted) bookings are frozen.
    - `created_by` is always derived from the authenticated
      `current_user`, never accepted from the request payload.
    - `booking_number` is always derived server-side via
      `app.services.booking_number.generate_booking_number()`, never
      accepted from the request payload, mirroring the `created_by`
      pattern above.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BadRequestException,
    ConflictException,
    NotFoundException,
)
from app.models.booking import (
    Booking,
    BookingPaymentStatus,
    BookingStatus,
)
from app.models.customer import Customer
from app.models.lead import Lead
from app.models.property import Property
from app.models.user import User
from app.repositories.booking_repository import BookingRepository
from app.schemas.booking import BookingCreate, BookingFilter, BookingUpdate
from app.services.booking_number import generate_booking_number


class BookingService:
    """
    Encapsulates all business logic for creating, retrieving, updating,
    transitioning, and reporting on `Booking` records.

    Consumed exclusively by the API layer (`app/api/v1/booking.py`),
    which is responsible for authentication/authorization and HTTP
    concerns only.
    """

    #: Allow-listed lifecycle transitions. A `status` value that is not
    #: a key here has no valid outgoing transitions (terminal state).
    _STATUS_TRANSITIONS: dict[BookingStatus, frozenset[BookingStatus]] = {
        BookingStatus.PENDING: frozenset(
            {BookingStatus.CONFIRMED, BookingStatus.CANCELLED}
        ),
        BookingStatus.CONFIRMED: frozenset(
            {BookingStatus.COMPLETED, BookingStatus.CANCELLED}
        ),
        BookingStatus.CANCELLED: frozenset({BookingStatus.REFUNDED}),
        BookingStatus.COMPLETED: frozenset(),
        BookingStatus.REFUNDED: frozenset(),
    }

    #: Allow-listed payment-status transitions.
    _PAYMENT_STATUS_TRANSITIONS: dict[
        BookingPaymentStatus, frozenset[BookingPaymentStatus]
    ] = {
        BookingPaymentStatus.PENDING: frozenset(
            {
                BookingPaymentStatus.PARTIALLY_PAID,
                BookingPaymentStatus.PAID,
                BookingPaymentStatus.OVERDUE,
            }
        ),
        BookingPaymentStatus.PARTIALLY_PAID: frozenset(
            {
                BookingPaymentStatus.PAID,
                BookingPaymentStatus.OVERDUE,
                BookingPaymentStatus.REFUNDED,
            }
        ),
        BookingPaymentStatus.OVERDUE: frozenset(
            {
                BookingPaymentStatus.PARTIALLY_PAID,
                BookingPaymentStatus.PAID,
            }
        ),
        BookingPaymentStatus.PAID: frozenset({BookingPaymentStatus.REFUNDED}),
        BookingPaymentStatus.REFUNDED: frozenset(),
    }

    def __init__(self, db: AsyncSession) -> None:
        """
        Args:
            db: An active SQLAlchemy AsyncSession, typically supplied
                via a FastAPI dependency (e.g., `get_db`).
        """
        self._db = db
        self._repo = BookingRepository(db)

    # ----------------------------------------------------------------
    # Cross-Entity Existence Validation
    # ----------------------------------------------------------------
    async def _get_customer_or_404(self, customer_id: uuid.UUID) -> Customer:
        """
        Ensure a `Customer` with the given ID exists.

        Args:
            customer_id: The UUID of the customer to look up.

        Returns:
            The matching `Customer` instance.

        Raises:
            NotFoundException: If no such customer exists.
        """
        result = await self._db.execute(
            select(Customer).where(Customer.id == customer_id)
        )
        customer = result.scalar_one_or_none()
        if customer is None:
            raise NotFoundException(
                f"Customer with id '{customer_id}' was not found."
            )
        return customer

    async def _get_property_or_404(self, property_id: int) -> Property:
        """
        Ensure a `Property` with the given ID exists.

        Args:
            property_id: The integer ID of the property to look up.

        Returns:
            The matching `Property` instance.

        Raises:
            NotFoundException: If no such property exists.
        """
        result = await self._db.execute(
            select(Property).where(Property.id == property_id)
        )
        property_obj = result.scalar_one_or_none()
        if property_obj is None:
            raise NotFoundException(
                f"Property with id '{property_id}' was not found."
            )
        return property_obj

    async def _get_lead_or_404(self, lead_id: uuid.UUID) -> Lead:
        """
        Ensure a `Lead` with the given ID exists.

        Args:
            lead_id: The UUID of the lead to look up.

        Returns:
            The matching `Lead` instance.

        Raises:
            NotFoundException: If no such lead exists.
        """
        result = await self._db.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if lead is None:
            raise NotFoundException(f"Lead with id '{lead_id}' was not found.")
        return lead

    async def _get_user_or_404(self, user_id: int, *, role_label: str = "User") -> User:
        """
        Ensure a `User` with the given ID exists (used for both
        `agent_id` and `created_by` validation).

        Args:
            user_id: The integer ID of the user to look up.
            role_label: Human-readable label used in the error message
                (e.g., "Agent") to make 404s more actionable.

        Returns:
            The matching `User` instance.

        Raises:
            NotFoundException: If no such user exists.
        """
        result = await self._db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None:
            raise NotFoundException(
                f"{role_label} with id '{user_id}' was not found."
            )
        return user

    async def _ensure_no_duplicate_active_booking(
        self,
        customer_id: uuid.UUID,
        property_id: int,
        *,
        exclude_booking_id: Optional[uuid.UUID] = None,
    ) -> None:
        """
        Ensure there is no other ACTIVE booking for the same
        Customer/Property pair.

        Args:
            customer_id: The UUID of the customer being booked.
            property_id: The integer ID of the property being booked.
            exclude_booking_id: A booking ID to exclude from the check
                (used when re-validating an update on the same record).

        Raises:
            ConflictException: If an active booking already exists for
                this customer/property combination.
        """
        existing = await self._repo.get_by_customer_and_property(
            customer_id, property_id
        )
        if (
            existing is not None
            and existing.is_active
            and existing.id != exclude_booking_id
        ):
            raise ConflictException(
                "An active booking already exists for this customer and "
                "property."
            )

    # ----------------------------------------------------------------
    # Field-Level Business Validation
    # ----------------------------------------------------------------
    @staticmethod
    def _validate_amounts(
        booking_amount: Optional[Decimal], token_amount: Optional[Decimal]
    ) -> None:
        """
        Validate booking/token amount business rules beyond what the
        Pydantic schema already enforces (defense-in-depth for values
        that reach the service via partial updates).

        Args:
            booking_amount: The total agreed booking value, if any.
            token_amount: The token/advance amount collected, if any.

        Raises:
            BadRequestException: If either amount is negative, or if
                `token_amount` exceeds `booking_amount`.
        """
        if booking_amount is not None and booking_amount < 0:
            raise BadRequestException("booking_amount must be >= 0.")
        if token_amount is not None and token_amount < 0:
            raise BadRequestException("token_amount must be >= 0.")
        if (
            booking_amount is not None
            and token_amount is not None
            and token_amount > booking_amount
        ):
            raise BadRequestException(
                "token_amount must not exceed booking_amount."
            )

    @staticmethod
    def _ensure_active(booking: Booking) -> None:
        """
        Ensure a booking is active before allowing it to be mutated.

        Args:
            booking: The booking instance to check.

        Raises:
            ConflictException: If the booking has been soft-deleted
                (`is_active` is False).
        """
        if not booking.is_active:
            raise ConflictException(
                f"Booking '{booking.id}' is inactive and cannot be modified."
            )

    def _validate_status_transition(
        self, current_status: BookingStatus, new_status: BookingStatus
    ) -> None:
        """
        Validate a proposed booking-status transition against the
        allow-listed state machine.

        Args:
            current_status: The booking's current status.
            new_status: The requested next status.

        Raises:
            BadRequestException: If the transition is not permitted.
        """
        if new_status == current_status:
            return
        allowed = self._STATUS_TRANSITIONS.get(current_status, frozenset())
        if new_status not in allowed:
            raise BadRequestException(
                f"Invalid booking status transition: "
                f"'{current_status.value}' -> '{new_status.value}'."
            )

    def _validate_payment_status_transition(
        self,
        current_status: BookingPaymentStatus,
        new_status: BookingPaymentStatus,
    ) -> None:
        """
        Validate a proposed payment-status transition against the
        allow-listed state machine.

        Args:
            current_status: The booking's current payment status.
            new_status: The requested next payment status.

        Raises:
            BadRequestException: If the transition is not permitted.
        """
        if new_status == current_status:
            return
        allowed = self._PAYMENT_STATUS_TRANSITIONS.get(current_status, frozenset())
        if new_status not in allowed:
            raise BadRequestException(
                f"Invalid payment status transition: "
                f"'{current_status.value}' -> '{new_status.value}'."
            )

    # ----------------------------------------------------------------
    # Create
    # ----------------------------------------------------------------
    async def create_booking(
        self, payload: BookingCreate, current_user: User
    ) -> Booking:
        """
        Validate and create a new booking.

        Args:
            payload: The validated `BookingCreate` request payload.
            current_user: The authenticated user creating the booking;
                used to stamp `created_by`.

        Returns:
            The newly persisted `Booking` instance.

        Raises:
            NotFoundException: If the customer, property, lead, or
                agent referenced does not exist.
            BadRequestException: If the booking/token amounts are
                invalid.
            ConflictException: If an active booking already exists for
                this customer/property pair.
        """
        await self._get_customer_or_404(payload.customer_id)
        await self._get_property_or_404(payload.property_id)
        if payload.lead_id is not None:
            await self._get_lead_or_404(payload.lead_id)
        if payload.agent_id is not None:
            await self._get_user_or_404(payload.agent_id, role_label="Agent")

        self._validate_amounts(payload.booking_amount, payload.token_amount)
        await self._ensure_no_duplicate_active_booking(
            payload.customer_id, payload.property_id
        )

        booking_number = await generate_booking_number(self._db)

        booking = Booking(
            customer_id=payload.customer_id,
            property_id=payload.property_id,
            lead_id=payload.lead_id,
            agent_id=payload.agent_id,
            booking_number=booking_number,
            booking_date=payload.booking_date,
            booking_amount=payload.booking_amount,
            token_amount=payload.token_amount,
            payment_mode=payload.payment_mode,
            payment_reference=payload.payment_reference,
            status=payload.status,
            payment_status=payload.payment_status,
            site_visit_date=payload.site_visit_date,
            next_follow_up=payload.next_follow_up,
            remarks=payload.remarks,
            cancellation_reason=payload.cancellation_reason,
            is_active=payload.is_active,
            created_by=current_user.id,
        )
        return await self._repo.create_booking(booking)

    # ----------------------------------------------------------------
    # Read
    # ----------------------------------------------------------------
    async def get_booking(self, booking_id: uuid.UUID) -> Booking:
        """
        Retrieve a single booking by ID.

        Args:
            booking_id: The UUID of the booking to retrieve.

        Returns:
            The matching `Booking` instance.

        Raises:
            NotFoundException: If no booking with the given ID exists.
        """
        booking = await self._repo.get_by_id(booking_id)
        if booking is None:
            raise NotFoundException(f"Booking with id '{booking_id}' was not found.")
        return booking

    async def list_bookings(
        self, filters: BookingFilter
    ) -> tuple[Sequence[Booking], int]:
        """
        Retrieve a paginated, filtered, and sorted list of bookings.

        Args:
            filters: The validated `BookingFilter` query parameters.

        Returns:
            A tuple of (matching bookings for the requested page, total
            matching record count).
        """
        return await self._repo.list_bookings(
            page=filters.page,
            page_size=filters.page_size,
            status=filters.status,
            payment_status=filters.payment_status,
            customer_id=filters.customer_id,
            property_id=filters.property_id,
            lead_id=filters.lead_id,
            agent_id=filters.agent_id,
            booking_date_from=filters.booking_date_from,
            booking_date_to=filters.booking_date_to,
            search=filters.search,
            sort_by=filters.sort_by,
            sort_order=filters.sort_order,
        )

    async def search_bookings(
        self, term: str, *, page: int = 1, page_size: int = 20
    ) -> tuple[Sequence[Booking], int]:
        """
        Free-text search across booking remarks/payment reference.

        Args:
            term: The search term to match.
            page: 1-indexed page number to retrieve.
            page_size: Number of records to return per page.

        Returns:
            A tuple of (matching bookings for the requested page, total
            matching record count).

        Raises:
            BadRequestException: If the search term is blank.
        """
        if not term or not term.strip():
            raise BadRequestException("Search term must not be empty.")
        return await self._repo.search(term.strip(), page=page, page_size=page_size)

    # ----------------------------------------------------------------
    # Update
    # ----------------------------------------------------------------
    async def update_booking(
        self, booking_id: uuid.UUID, payload: BookingUpdate
    ) -> Booking:
        """
        Apply a partial update to an existing booking.

        Args:
            booking_id: The UUID of the booking to update.
            payload: The validated `BookingUpdate` request payload;
                only explicitly supplied fields are applied.

        Returns:
            The updated `Booking` instance.

        Raises:
            NotFoundException: If the booking, agent does not exist.
            ConflictException: If the booking is inactive.
            BadRequestException: If the resulting amounts are invalid,
                or an included status/payment_status transition is
                invalid.
        """
        booking = await self.get_booking(booking_id)
        self._ensure_active(booking)

        update_data = payload.model_dump(exclude_unset=True)

        if "agent_id" in update_data and update_data["agent_id"] is not None:
            await self._get_user_or_404(update_data["agent_id"], role_label="Agent")

        effective_booking_amount = update_data.get(
            "booking_amount", booking.booking_amount
        )
        effective_token_amount = update_data.get(
            "token_amount", booking.token_amount
        )
        self._validate_amounts(effective_booking_amount, effective_token_amount)

        if "status" in update_data and update_data["status"] is not None:
            self._validate_status_transition(booking.status, update_data["status"])

        if (
            "payment_status" in update_data
            and update_data["payment_status"] is not None
        ):
            self._validate_payment_status_transition(
                booking.payment_status, update_data["payment_status"]
            )

        return await self._repo.update_booking(booking, update_data)

    # ----------------------------------------------------------------
    # Delete (Soft)
    # ----------------------------------------------------------------
    async def soft_delete_booking(self, booking_id: uuid.UUID) -> Booking:
        """
        Soft-delete a booking (sets `is_active` to False).

        Args:
            booking_id: The UUID of the booking to soft-delete.

        Returns:
            The updated, inactive `Booking` instance.

        Raises:
            NotFoundException: If no booking with the given ID exists.
            ConflictException: If the booking is already inactive.
        """
        booking = await self.get_booking(booking_id)
        if not booking.is_active:
            raise ConflictException(
                f"Booking '{booking_id}' is already inactive."
            )
        deleted = await self._repo.soft_delete(booking_id)
        if deleted is None:
            raise NotFoundException(f"Booking with id '{booking_id}' was not found.")
        return deleted

    # ----------------------------------------------------------------
    # Status / Payment Status / Agent Transitions
    # ----------------------------------------------------------------
    async def change_status(
        self, booking_id: uuid.UUID, new_status: BookingStatus
    ) -> Booking:
        """
        Transition a booking's lifecycle status.

        Args:
            booking_id: The UUID of the booking to update.
            new_status: The requested new `BookingStatus`.

        Returns:
            The updated `Booking` instance.

        Raises:
            NotFoundException: If no booking with the given ID exists.
            ConflictException: If the booking is inactive.
            BadRequestException: If the transition is not permitted.
        """
        booking = await self.get_booking(booking_id)
        self._ensure_active(booking)
        self._validate_status_transition(booking.status, new_status)

        updated = await self._repo.change_status(booking_id, new_status)
        if updated is None:
            raise NotFoundException(f"Booking with id '{booking_id}' was not found.")
        return updated

    async def change_payment_status(
        self, booking_id: uuid.UUID, new_status: BookingPaymentStatus
    ) -> Booking:
        """
        Transition a booking's payment status.

        Args:
            booking_id: The UUID of the booking to update.
            new_status: The requested new `BookingPaymentStatus`.

        Returns:
            The updated `Booking` instance.

        Raises:
            NotFoundException: If no booking with the given ID exists.
            ConflictException: If the booking is inactive.
            BadRequestException: If the transition is not permitted.
        """
        booking = await self.get_booking(booking_id)
        self._ensure_active(booking)
        self._validate_payment_status_transition(booking.payment_status, new_status)

        updated = await self._repo.change_payment_status(booking_id, new_status)
        if updated is None:
            raise NotFoundException(f"Booking with id '{booking_id}' was not found.")
        return updated

    async def assign_agent(self, booking_id: uuid.UUID, agent_id: int) -> Booking:
        """
        Assign (or reassign) the sales agent handling a booking.

        Args:
            booking_id: The UUID of the booking to update.
            agent_id: The internal User ID of the agent to assign.

        Returns:
            The updated `Booking` instance.

        Raises:
            NotFoundException: If the booking or agent does not exist.
            ConflictException: If the booking is inactive.
        """
        booking = await self.get_booking(booking_id)
        self._ensure_active(booking)
        await self._get_user_or_404(agent_id, role_label="Agent")

        updated = await self._repo.assign_agent(booking_id, agent_id)
        if updated is None:
            raise NotFoundException(f"Booking with id '{booking_id}' was not found.")
        return updated

    # ----------------------------------------------------------------
    # Reporting
    # ----------------------------------------------------------------
    async def dashboard_summary(self) -> dict[str, Any]:
        """
        Compute an aggregate dashboard summary of active bookings.

        Returns:
            A dict containing:
                - total_active_bookings: int
                - status_breakdown: dict[str, int]
                - payment_status_breakdown: dict[str, int]
                - total_booking_value: Decimal
                - total_token_collected: Decimal
                - pending_followups: int
        """
        total_active = await self._repo.count(is_active=True)
        status_breakdown = await self._repo.count_by_status(is_active=True)
        payment_status_breakdown = await self._repo.count_by_payment_status(
            is_active=True
        )

        totals_stmt = select(
            func.coalesce(func.sum(Booking.booking_amount), 0),
            func.coalesce(func.sum(Booking.token_amount), 0),
        ).where(Booking.is_active.is_(True))
        totals_result = await self._db.execute(totals_stmt)
        total_booking_value, total_token_collected = totals_result.one()

        followups = await self._repo.upcoming_followups()

        return {
            "total_active_bookings": total_active,
            "status_breakdown": status_breakdown,
            "payment_status_breakdown": payment_status_breakdown,
            "total_booking_value": Decimal(total_booking_value),
            "total_token_collected": Decimal(total_token_collected),
            "pending_followups": len(followups),
        }

    async def todays_followups(
        self, *, reference_date: Optional[date] = None
    ) -> Sequence[Booking]:
        """
        Retrieve active bookings whose next follow-up is due today (or
        earlier).

        Args:
            reference_date: The date to compare against. Defaults to
                today if not supplied.

        Returns:
            A sequence of matching `Booking` instances, ordered by
            `next_follow_up` ascending (most overdue first).
        """
        return await self._repo.upcoming_followups(reference_date=reference_date)


__all__ = ["BookingService"]