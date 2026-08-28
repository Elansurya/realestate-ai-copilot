"""
backend/tests/test_booking_service.py

Unit tests for `app.services.booking_service.BookingService`.

Scope:
    - Exercises the service layer in isolation: `BookingRepository`
      and the `AsyncSession` are both replaced with `AsyncMock`/`Mock`
      doubles, so these tests never touch a real database or event
      loop-bound connection.
    - Focuses on business rules the service is responsible for that
      the repository and Pydantic schema layers intentionally do not
      enforce on their own: cross-entity existence checks, duplicate-
      booking prevention, amount validation, status/payment-status
      transition state machines, and the active/inactive freeze rule.

Conventions:
    - `BookingService._repo` is monkey-patched with an `AsyncMock`
      after construction so the constructor's real
      `BookingRepository(db)` wiring is bypassed entirely, mirroring
      the existing `test_lead_service.py` pattern.
    - `db.execute` is an `AsyncMock` whose return value is configured
      per-test via a small `_scalar_result` helper that mimics
      SQLAlchemy's `Result.scalar_one_or_none()` / `.one()` contracts.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import (
    BadRequestException,
    ConflictException,
    NotFoundException,
)
from app.models.booking import Booking, BookingPaymentStatus, BookingStatus
from app.models.customer import Customer
from app.models.property import Property
from app.models.user import User
from app.schemas.booking import BookingCreate, BookingFilter, BookingUpdate
from app.services.booking_service import BookingService

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _scalar_result(value):
    """Build a `MagicMock` mimicking `Result.scalar_one_or_none()`."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    result.scalar_one.return_value = value
    return result


def _make_booking(
    *,
    booking_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    property_id: int = 101,
    status: BookingStatus = BookingStatus.PENDING,
    payment_status: BookingPaymentStatus = BookingPaymentStatus.PENDING,
    is_active: bool = True,
    booking_amount: Decimal | None = Decimal("7500000"),
    token_amount: Decimal | None = Decimal("500000"),
) -> Booking:
    return Booking(
        id=booking_id or uuid.uuid4(),
        customer_id=customer_id or uuid.uuid4(),
        property_id=property_id,
        status=status,
        payment_status=payment_status,
        is_active=is_active,
        booking_amount=booking_amount,
        token_amount=token_amount,
    )


@pytest.fixture
def mock_db() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def service(mock_db: AsyncMock) -> BookingService:
    svc = BookingService(mock_db)
    svc._repo = AsyncMock()
    return svc


@pytest.fixture
def current_user() -> User:
    user = MagicMock(spec=User)
    user.id = 1
    return user


# --------------------------------------------------------------------------
# create_booking - Cross-Entity Existence
# --------------------------------------------------------------------------
async def test_create_booking_raises_when_customer_missing(
    service: BookingService, mock_db: AsyncMock, current_user: User
) -> None:
    mock_db.execute.return_value = _scalar_result(None)
    payload = BookingCreate(customer_id=uuid.uuid4(), property_id=101)

    with pytest.raises(NotFoundException):
        await service.create_booking(payload, current_user)


async def test_create_booking_raises_when_property_missing(
    service: BookingService, mock_db: AsyncMock, current_user: User
) -> None:
    customer = Customer(id=uuid.uuid4())
    mock_db.execute.side_effect = [_scalar_result(customer), _scalar_result(None)]
    payload = BookingCreate(customer_id=customer.id, property_id=999999)

    with pytest.raises(NotFoundException):
        await service.create_booking(payload, current_user)


async def test_create_booking_raises_when_lead_missing(
    service: BookingService, mock_db: AsyncMock, current_user: User
) -> None:
    customer = Customer(id=uuid.uuid4())
    prop = Property(id=101)
    mock_db.execute.side_effect = [
        _scalar_result(customer),
        _scalar_result(prop),
        _scalar_result(None),
    ]
    payload = BookingCreate(customer_id=customer.id, property_id=101, lead_id=uuid.uuid4())

    with pytest.raises(NotFoundException):
        await service.create_booking(payload, current_user)


async def test_create_booking_raises_when_agent_missing(
    service: BookingService, mock_db: AsyncMock, current_user: User
) -> None:
    customer = Customer(id=uuid.uuid4())
    prop = Property(id=101)
    mock_db.execute.side_effect = [
        _scalar_result(customer),
        _scalar_result(prop),
        _scalar_result(None),
    ]
    payload = BookingCreate(customer_id=customer.id, property_id=101, agent_id=999999)

    with pytest.raises(NotFoundException):
        await service.create_booking(payload, current_user)


# --------------------------------------------------------------------------
# create_booking - Duplicate Booking Prevention
# --------------------------------------------------------------------------
async def test_create_booking_raises_conflict_on_duplicate_active_booking(
    service: BookingService, mock_db: AsyncMock, current_user: User
) -> None:
    customer = Customer(id=uuid.uuid4())
    prop = Property(id=101)
    mock_db.execute.side_effect = [_scalar_result(customer), _scalar_result(prop)]

    existing = _make_booking(customer_id=customer.id, property_id=101, is_active=True)
    service._repo.get_by_customer_and_property.return_value = existing

    payload = BookingCreate(customer_id=customer.id, property_id=101)

    with pytest.raises(ConflictException):
        await service.create_booking(payload, current_user)
    service._repo.create_booking.assert_not_called()


async def test_create_booking_allows_new_booking_when_prior_is_inactive(
    service: BookingService, mock_db: AsyncMock, current_user: User
) -> None:
    customer = Customer(id=uuid.uuid4())
    prop = Property(id=101)
    mock_db.execute.side_effect = [
        _scalar_result(customer),
        _scalar_result(prop),
        _scalar_result(1),
    ]

    existing = _make_booking(customer_id=customer.id, property_id=101, is_active=False)
    service._repo.get_by_customer_and_property.return_value = existing
    service._repo.create_booking.return_value = _make_booking(
        customer_id=customer.id, property_id=101
    )

    payload = BookingCreate(customer_id=customer.id, property_id=101)
    result = await service.create_booking(payload, current_user)

    assert result.customer_id == customer.id
    service._repo.create_booking.assert_awaited_once()


async def test_create_booking_success_stamps_created_by(
    service: BookingService, mock_db: AsyncMock, current_user: User
) -> None:
    customer = Customer(id=uuid.uuid4())
    prop = Property(id=101)
    mock_db.execute.side_effect = [
        _scalar_result(customer),
        _scalar_result(prop),
        _scalar_result(1),
    ]
    service._repo.get_by_customer_and_property.return_value = None

    async def _echo_create(booking: Booking) -> Booking:
        return booking

    service._repo.create_booking.side_effect = _echo_create

    payload = BookingCreate(customer_id=customer.id, property_id=101)
    result = await service.create_booking(payload, current_user)

    assert result.created_by == current_user.id


# --------------------------------------------------------------------------
# Amount Validation
# --------------------------------------------------------------------------
def test_validate_amounts_rejects_negative_booking_amount() -> None:
    with pytest.raises(BadRequestException):
        BookingService._validate_amounts(Decimal("-1"), None)


def test_validate_amounts_rejects_negative_token_amount() -> None:
    with pytest.raises(BadRequestException):
        BookingService._validate_amounts(None, Decimal("-1"))


def test_validate_amounts_rejects_token_exceeding_booking() -> None:
    with pytest.raises(BadRequestException):
        BookingService._validate_amounts(Decimal("100"), Decimal("200"))


def test_validate_amounts_allows_equal_token_and_booking() -> None:
    BookingService._validate_amounts(Decimal("100"), Decimal("100"))


def test_validate_amounts_allows_none_values() -> None:
    BookingService._validate_amounts(None, None)


# --------------------------------------------------------------------------
# update_booking - Business Rules
# --------------------------------------------------------------------------
async def test_update_booking_raises_when_inactive(service: BookingService) -> None:
    booking = _make_booking(is_active=False)
    service._repo.get_by_id.return_value = booking

    with pytest.raises(ConflictException):
        await service.update_booking(booking.id, BookingUpdate(remarks="x"))


async def test_update_booking_raises_not_found(service: BookingService) -> None:
    service._repo.get_by_id.return_value = None

    with pytest.raises(NotFoundException):
        await service.update_booking(uuid.uuid4(), BookingUpdate(remarks="x"))


async def test_update_booking_validates_effective_amounts(
    service: BookingService, mock_db: AsyncMock
) -> None:
    booking = _make_booking(booking_amount=Decimal("1000000"), token_amount=Decimal("100000"))
    service._repo.get_by_id.return_value = booking

    with pytest.raises(BadRequestException):
        await service.update_booking(
            booking.id, BookingUpdate(token_amount=Decimal("5000000"))
        )


async def test_update_booking_validates_agent_existence(
    service: BookingService, mock_db: AsyncMock
) -> None:
    booking = _make_booking()
    service._repo.get_by_id.return_value = booking
    mock_db.execute.return_value = _scalar_result(None)

    with pytest.raises(NotFoundException):
        await service.update_booking(booking.id, BookingUpdate(agent_id=999999))


async def test_update_booking_applies_only_supplied_fields(
    service: BookingService, mock_db: AsyncMock
) -> None:
    booking = _make_booking()
    service._repo.get_by_id.return_value = booking
    service._repo.update_booking.return_value = booking

    await service.update_booking(booking.id, BookingUpdate(remarks="Updated"))

    _, update_data = service._repo.update_booking.call_args.args
    assert update_data == {"remarks": "Updated"}


# --------------------------------------------------------------------------
# soft_delete_booking
# --------------------------------------------------------------------------
async def test_soft_delete_booking_success(service: BookingService) -> None:
    booking = _make_booking(is_active=True)
    service._repo.get_by_id.return_value = booking
    service._repo.soft_delete.return_value = _make_booking(
        booking_id=booking.id, is_active=False
    )

    result = await service.soft_delete_booking(booking.id)
    assert result.is_active is False


async def test_soft_delete_already_inactive_raises_conflict(service: BookingService) -> None:
    booking = _make_booking(is_active=False)
    service._repo.get_by_id.return_value = booking

    with pytest.raises(ConflictException):
        await service.soft_delete_booking(booking.id)


async def test_soft_delete_not_found_raises(service: BookingService) -> None:
    service._repo.get_by_id.return_value = None

    with pytest.raises(NotFoundException):
        await service.soft_delete_booking(uuid.uuid4())


# --------------------------------------------------------------------------
# Status Transition State Machine
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "current,new",
    [
        (BookingStatus.PENDING, BookingStatus.CONFIRMED),
        (BookingStatus.PENDING, BookingStatus.CANCELLED),
        (BookingStatus.CONFIRMED, BookingStatus.COMPLETED),
        (BookingStatus.CONFIRMED, BookingStatus.CANCELLED),
        (BookingStatus.CANCELLED, BookingStatus.REFUNDED),
    ],
)
def test_status_transition_allowed(current: BookingStatus, new: BookingStatus) -> None:
    svc = BookingService(AsyncMock())
    svc._validate_status_transition(current, new)  # should not raise


@pytest.mark.parametrize(
    "current,new",
    [
        (BookingStatus.PENDING, BookingStatus.COMPLETED),
        (BookingStatus.PENDING, BookingStatus.REFUNDED),
        (BookingStatus.CONFIRMED, BookingStatus.PENDING),
        (BookingStatus.COMPLETED, BookingStatus.PENDING),
        (BookingStatus.COMPLETED, BookingStatus.CANCELLED),
        (BookingStatus.REFUNDED, BookingStatus.CONFIRMED),
    ],
)
def test_status_transition_rejected(current: BookingStatus, new: BookingStatus) -> None:
    svc = BookingService(AsyncMock())
    with pytest.raises(BadRequestException):
        svc._validate_status_transition(current, new)


def test_status_transition_same_state_is_noop() -> None:
    svc = BookingService(AsyncMock())
    svc._validate_status_transition(BookingStatus.PENDING, BookingStatus.PENDING)


async def test_change_status_raises_when_inactive(service: BookingService) -> None:
    booking = _make_booking(is_active=False)
    service._repo.get_by_id.return_value = booking

    with pytest.raises(ConflictException):
        await service.change_status(booking.id, BookingStatus.CONFIRMED)


async def test_change_status_success(service: BookingService) -> None:
    booking = _make_booking(status=BookingStatus.PENDING)
    service._repo.get_by_id.return_value = booking
    service._repo.change_status.return_value = _make_booking(
        booking_id=booking.id, status=BookingStatus.CONFIRMED
    )

    result = await service.change_status(booking.id, BookingStatus.CONFIRMED)
    assert result.status == BookingStatus.CONFIRMED


# --------------------------------------------------------------------------
# Payment Status Transition State Machine
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "current,new",
    [
        (BookingPaymentStatus.PENDING, BookingPaymentStatus.PARTIALLY_PAID),
        (BookingPaymentStatus.PENDING, BookingPaymentStatus.PAID),
        (BookingPaymentStatus.PENDING, BookingPaymentStatus.OVERDUE),
        (BookingPaymentStatus.PARTIALLY_PAID, BookingPaymentStatus.PAID),
        (BookingPaymentStatus.PARTIALLY_PAID, BookingPaymentStatus.OVERDUE),
        (BookingPaymentStatus.PARTIALLY_PAID, BookingPaymentStatus.REFUNDED),
        (BookingPaymentStatus.OVERDUE, BookingPaymentStatus.PARTIALLY_PAID),
        (BookingPaymentStatus.OVERDUE, BookingPaymentStatus.PAID),
        (BookingPaymentStatus.PAID, BookingPaymentStatus.REFUNDED),
    ],
)
def test_payment_status_transition_allowed(
    current: BookingPaymentStatus, new: BookingPaymentStatus
) -> None:
    svc = BookingService(AsyncMock())
    svc._validate_payment_status_transition(current, new)  # should not raise


@pytest.mark.parametrize(
    "current,new",
    [
        (BookingPaymentStatus.PENDING, BookingPaymentStatus.REFUNDED),
        (BookingPaymentStatus.PAID, BookingPaymentStatus.PENDING),
        (BookingPaymentStatus.PAID, BookingPaymentStatus.PARTIALLY_PAID),
        (BookingPaymentStatus.REFUNDED, BookingPaymentStatus.PAID),
    ],
)
def test_payment_status_transition_rejected(
    current: BookingPaymentStatus, new: BookingPaymentStatus
) -> None:
    svc = BookingService(AsyncMock())
    with pytest.raises(BadRequestException):
        svc._validate_payment_status_transition(current, new)


async def test_change_payment_status_raises_when_inactive(service: BookingService) -> None:
    booking = _make_booking(is_active=False)
    service._repo.get_by_id.return_value = booking

    with pytest.raises(ConflictException):
        await service.change_payment_status(booking.id, BookingPaymentStatus.PAID)


async def test_change_payment_status_success(service: BookingService) -> None:
    booking = _make_booking(payment_status=BookingPaymentStatus.PENDING)
    service._repo.get_by_id.return_value = booking
    service._repo.change_payment_status.return_value = _make_booking(
        booking_id=booking.id, payment_status=BookingPaymentStatus.PAID
    )

    result = await service.change_payment_status(booking.id, BookingPaymentStatus.PAID)
    assert result.payment_status == BookingPaymentStatus.PAID


# --------------------------------------------------------------------------
# Agent Assignment
# --------------------------------------------------------------------------
async def test_assign_agent_raises_when_agent_missing(
    service: BookingService, mock_db: AsyncMock
) -> None:
    booking = _make_booking()
    service._repo.get_by_id.return_value = booking
    mock_db.execute.return_value = _scalar_result(None)

    with pytest.raises(NotFoundException):
        await service.assign_agent(booking.id, 999999)


async def test_assign_agent_raises_when_inactive(
    service: BookingService, mock_db: AsyncMock
) -> None:
    booking = _make_booking(is_active=False)
    service._repo.get_by_id.return_value = booking

    with pytest.raises(ConflictException):
        await service.assign_agent(booking.id, 12)


async def test_assign_agent_success(service: BookingService, mock_db: AsyncMock) -> None:
    booking = _make_booking()
    agent = User(id=12, email="a@test.io")
    service._repo.get_by_id.return_value = booking
    mock_db.execute.return_value = _scalar_result(agent)
    service._repo.assign_agent.return_value = _make_booking(booking_id=booking.id)

    result = await service.assign_agent(booking.id, 12)
    service._repo.assign_agent.assert_awaited_once_with(booking.id, 12)
    assert result is not None


# --------------------------------------------------------------------------
# search_bookings
# --------------------------------------------------------------------------
async def test_search_bookings_rejects_blank_term(service: BookingService) -> None:
    with pytest.raises(BadRequestException):
        await service.search_bookings("   ")


async def test_search_bookings_strips_and_delegates(service: BookingService) -> None:
    service._repo.search.return_value = ([], 0)
    await service.search_bookings("  balcony  ")
    service._repo.search.assert_awaited_once_with("balcony", page=1, page_size=20)


# --------------------------------------------------------------------------
# list_bookings
# --------------------------------------------------------------------------
async def test_list_bookings_delegates_all_filters(service: BookingService) -> None:
    service._repo.list_bookings.return_value = ([], 0)
    filters = BookingFilter(status=BookingStatus.PENDING, page=2, page_size=10)

    await service.list_bookings(filters)

    service._repo.list_bookings.assert_awaited_once()
    _, kwargs = service._repo.list_bookings.call_args
    assert kwargs["status"] == BookingStatus.PENDING
    assert kwargs["page"] == 2
    assert kwargs["page_size"] == 10


# --------------------------------------------------------------------------
# dashboard_summary / todays_followups
# --------------------------------------------------------------------------
async def test_dashboard_summary_aggregates_repository_data(
    service: BookingService, mock_db: AsyncMock
) -> None:
    service._repo.count.return_value = 5
    service._repo.count_by_status.return_value = {"PENDING": 5}
    service._repo.count_by_payment_status.return_value = {"PENDING": 5}
    service._repo.upcoming_followups.return_value = [_make_booking(), _make_booking()]

    totals_result = MagicMock()
    totals_result.one.return_value = (Decimal("1000000"), Decimal("100000"))
    mock_db.execute.return_value = totals_result

    summary = await service.dashboard_summary()

    assert summary["total_active_bookings"] == 5
    assert summary["pending_followups"] == 2
    assert summary["total_booking_value"] == Decimal("1000000")
    assert summary["total_token_collected"] == Decimal("100000")


async def test_todays_followups_delegates_to_repository(service: BookingService) -> None:
    reference = date(2026, 8, 1)
    service._repo.upcoming_followups.return_value = []

    await service.todays_followups(reference_date=reference)

    service._repo.upcoming_followups.assert_awaited_once_with(reference_date=reference)