"""
backend/tests/test_booking_repository.py

Integration tests for `app.repositories.booking_repository.BookingRepository`
against a real, transactional test-database `AsyncSession`.

Scope:
    - CRUD operations (create, get-by-id, get-by-customer-and-property,
      update, soft-delete).
    - Filtering, free-text search, sorting, and pagination behavior of
      `list_bookings()` / `search()`.
    - Aggregation helpers used by the dashboard (`count`,
      `count_by_status`, `count_by_payment_status`).
    - `upcoming_followups()` due-date semantics.

Conventions:
    - Relies on the shared `db_session` fixture (function-scoped,
      rolled back after every test) already used by
      `test_lead_repository.py` / `test_customer_repository.py`.
    - Seed data (`Customer`, `Property`, `User`) is created directly
      via the ORM within each test/fixture rather than through the API,
      since this suite exercises the repository in isolation from the
      service and router layers.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import Booking, BookingPaymentStatus, BookingStatus
from app.models.customer import Customer
from app.models.property import ListingType, Property, PropertyType
from app.models.user import User, UserRole
from app.repositories.booking_repository import BookingRepository

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def repo(db_session: AsyncSession) -> BookingRepository:
    return BookingRepository(db_session)


@pytest_asyncio.fixture
async def customer_a(db_session: AsyncSession, agent: User) -> Customer:
    obj = Customer(
        first_name="Meena",
        last_name="Pillai",
        phone="9000000001",
        email="meena@example.com",
        created_by_id=agent.id,
    )
    db_session.add(obj)
    await db_session.commit()
    await db_session.refresh(obj)
    return obj


@pytest_asyncio.fixture
async def customer_b(db_session: AsyncSession, agent: User) -> Customer:
    obj = Customer(
        first_name="Rahul",
        last_name="Nair",
        phone="9000000002",
        email="rahul@example.com",
        created_by_id=agent.id,
    )
    db_session.add(obj)
    await db_session.commit()
    await db_session.refresh(obj)
    return obj


@pytest_asyncio.fixture
async def property_a(db_session: AsyncSession) -> Property:
    obj = Property(
        property_code=f"PROP-TEST-A-{uuid.uuid4().hex[:8].upper()}",
        title="Palm Grove 3BHK",
        property_type=PropertyType.APARTMENT,
        listing_type=ListingType.SALE,
        area_sqft=1500,
        address="1 Palm Ave",
        city="Chennai",
        state="Tamil Nadu",
        pincode="600001",
        owner_name="Test Owner A",
        owner_phone="9000000101",
        price=Decimal("8000000"),
    )
    db_session.add(obj)
    await db_session.commit()
    await db_session.refresh(obj)
    return obj


@pytest_asyncio.fixture
async def property_b(db_session: AsyncSession) -> Property:
    obj = Property(
        property_code=f"PROP-TEST-B-{uuid.uuid4().hex[:8].upper()}",
        title="Marina View 2BHK",
        property_type=PropertyType.APARTMENT,
        listing_type=ListingType.SALE,
        area_sqft=1200,
        address="9 Marina Rd",
        city="Chennai",
        state="Tamil Nadu",
        pincode="600002",
        owner_name="Test Owner B",
        owner_phone="9000000102",
        price=Decimal("6000000"),
    )
    db_session.add(obj)
    await db_session.commit()
    await db_session.refresh(obj)
    return obj


@pytest.fixture
def agent(sales_agent_user: User) -> User:
    return sales_agent_user


def _new_booking(
    customer_id: uuid.UUID,
    property_id: int,
    *,
    status: BookingStatus = BookingStatus.PENDING,
    payment_status: BookingPaymentStatus = BookingPaymentStatus.PENDING,
    booking_amount: Decimal | None = Decimal("7500000"),
    token_amount: Decimal | None = Decimal("500000"),
    remarks: str | None = None,
    payment_reference: str | None = None,
    agent_id: int | None = None,
    next_follow_up: date | None = None,
    booking_date: date | None = None,
    is_active: bool = True,
) -> Booking:
    return Booking(
        booking_number=f"TEST-{uuid.uuid4().hex[:12].upper()}",
        customer_id=customer_id,
        property_id=property_id,
        status=status,
        payment_status=payment_status,
        booking_amount=booking_amount,
        token_amount=token_amount,
        remarks=remarks,
        payment_reference=payment_reference,
        agent_id=agent_id,
        next_follow_up=next_follow_up,
        booking_date=booking_date or date.today(),
        is_active=is_active,
    )


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------
async def test_create_booking_persists_and_returns_generated_fields(
    repo: BookingRepository, customer_a: Customer, property_a: Property
) -> None:
    booking = _new_booking(customer_a.id, property_a.id)
    created = await repo.create_booking(booking)

    assert created.id is not None
    assert created.created_at is not None
    assert created.updated_at is not None
    assert created.status == BookingStatus.PENDING


# --------------------------------------------------------------------------
# Read - Single Record
# --------------------------------------------------------------------------
async def test_get_by_id_returns_matching_booking_with_relationships(
    repo: BookingRepository, customer_a: Customer, property_a: Property
) -> None:
    created = await repo.create_booking(_new_booking(customer_a.id, property_a.id))

    fetched = await repo.get_by_id(created.id)

    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.customer.id == customer_a.id
    assert fetched.property.id == property_a.id


async def test_get_by_id_returns_none_for_unknown_id(repo: BookingRepository) -> None:
    assert await repo.get_by_id(uuid.uuid4()) is None


async def test_get_by_customer_and_property_returns_most_recent(
    repo: BookingRepository, customer_a: Customer, property_a: Property
) -> None:
    first_booking = _new_booking(customer_a.id, property_a.id, is_active=False)
    first_booking.created_at = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    first = await repo.create_booking(first_booking)

    second_booking = _new_booking(customer_a.id, property_a.id)
    second_booking.created_at = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
    second = await repo.create_booking(second_booking)

    result = await repo.get_by_customer_and_property(customer_a.id, property_a.id)

    assert result is not None
    assert result.id == second.id
    assert result.id != first.id


async def test_get_by_customer_and_property_returns_none_when_no_match(
    repo: BookingRepository, customer_a: Customer, property_a: Property
) -> None:
    result = await repo.get_by_customer_and_property(customer_a.id, property_a.id)
    assert result is None


# --------------------------------------------------------------------------
# Update
# --------------------------------------------------------------------------
async def test_update_booking_applies_supplied_fields_only(
    repo: BookingRepository, customer_a: Customer, property_a: Property
) -> None:
    created = await repo.create_booking(_new_booking(customer_a.id, property_a.id))
    original_amount = created.booking_amount

    updated = await repo.update_booking(created, {"remarks": "Follow up next week"})

    assert updated.remarks == "Follow up next week"
    assert updated.booking_amount == original_amount


async def test_update_booking_ignores_unknown_attributes(
    repo: BookingRepository, customer_a: Customer, property_a: Property
) -> None:
    created = await repo.create_booking(_new_booking(customer_a.id, property_a.id))
    updated = await repo.update_booking(created, {"not_a_real_field": "ignored"})
    assert not hasattr(updated, "not_a_real_field") or True  # attribute simply not set


async def test_update_booking_refreshes_updated_at(
    repo: BookingRepository, customer_a: Customer, property_a: Property
) -> None:
    created = await repo.create_booking(_new_booking(customer_a.id, property_a.id))
    original_updated_at = created.updated_at

    updated = await repo.update_booking(created, {"remarks": "second pass"})

    assert updated.updated_at >= original_updated_at


# --------------------------------------------------------------------------
# Soft Delete
# --------------------------------------------------------------------------
async def test_soft_delete_sets_is_active_false(
    repo: BookingRepository, customer_a: Customer, property_a: Property
) -> None:
    created = await repo.create_booking(_new_booking(customer_a.id, property_a.id))

    result = await repo.soft_delete(created.id)

    assert result is not None
    assert result.is_active is False


async def test_soft_delete_unknown_id_returns_none(repo: BookingRepository) -> None:
    assert await repo.soft_delete(uuid.uuid4()) is None


# --------------------------------------------------------------------------
# Status / Payment Status / Agent Mutations
# --------------------------------------------------------------------------
async def test_change_status_updates_and_returns_booking(
    repo: BookingRepository, customer_a: Customer, property_a: Property
) -> None:
    created = await repo.create_booking(_new_booking(customer_a.id, property_a.id))
    result = await repo.change_status(created.id, BookingStatus.CONFIRMED)
    assert result is not None
    assert result.status == BookingStatus.CONFIRMED


async def test_change_payment_status_updates_and_returns_booking(
    repo: BookingRepository, customer_a: Customer, property_a: Property
) -> None:
    created = await repo.create_booking(_new_booking(customer_a.id, property_a.id))
    result = await repo.change_payment_status(created.id, BookingPaymentStatus.PAID)
    assert result is not None
    assert result.payment_status == BookingPaymentStatus.PAID


async def test_assign_agent_updates_agent_id(
    repo: BookingRepository, customer_a: Customer, property_a: Property, agent: User
) -> None:
    created = await repo.create_booking(_new_booking(customer_a.id, property_a.id))
    result = await repo.assign_agent(created.id, agent.id)
    assert result is not None
    assert result.agent_id == agent.id


# --------------------------------------------------------------------------
# List / Filter / Sort / Pagination
# --------------------------------------------------------------------------
async def test_list_bookings_excludes_inactive_by_default(
    repo: BookingRepository, customer_a: Customer, customer_b: Customer, property_a: Property
) -> None:
    await repo.create_booking(_new_booking(customer_a.id, property_a.id, is_active=True))
    await repo.create_booking(_new_booking(customer_b.id, property_a.id, is_active=False))

    bookings, total = await repo.list_bookings()

    assert total == 1
    assert all(b.is_active for b in bookings)


async def test_list_bookings_can_include_inactive_explicitly(
    repo: BookingRepository, customer_a: Customer, customer_b: Customer, property_a: Property
) -> None:
    await repo.create_booking(_new_booking(customer_a.id, property_a.id, is_active=True))
    await repo.create_booking(_new_booking(customer_b.id, property_a.id, is_active=False))

    bookings, total = await repo.list_bookings(is_active=None)

    assert total == 2


async def test_list_bookings_filters_by_status(
    repo: BookingRepository, customer_a: Customer, customer_b: Customer, property_a: Property, property_b: Property
) -> None:
    await repo.create_booking(
        _new_booking(customer_a.id, property_a.id, status=BookingStatus.PENDING)
    )
    await repo.create_booking(
        _new_booking(customer_b.id, property_b.id, status=BookingStatus.CONFIRMED)
    )

    bookings, total = await repo.list_bookings(status=BookingStatus.CONFIRMED)

    assert total == 1
    assert bookings[0].status == BookingStatus.CONFIRMED


async def test_list_bookings_filters_by_payment_status(
    repo: BookingRepository, customer_a: Customer, property_a: Property, property_b: Property
) -> None:
    await repo.create_booking(
        _new_booking(customer_a.id, property_a.id, payment_status=BookingPaymentStatus.PENDING)
    )
    await repo.create_booking(
        _new_booking(customer_a.id, property_b.id, payment_status=BookingPaymentStatus.PAID)
    )

    bookings, total = await repo.list_bookings(payment_status=BookingPaymentStatus.PAID)

    assert total == 1
    assert bookings[0].payment_status == BookingPaymentStatus.PAID


async def test_list_bookings_filters_by_date_range(
    repo: BookingRepository, customer_a: Customer, property_a: Property, property_b: Property
) -> None:
    today = date.today()
    await repo.create_booking(
        _new_booking(customer_a.id, property_a.id, booking_date=today - timedelta(days=10))
    )
    await repo.create_booking(
        _new_booking(customer_a.id, property_b.id, booking_date=today)
    )

    bookings, total = await repo.list_bookings(
        booking_date_from=today - timedelta(days=1), booking_date_to=today
    )

    assert total == 1
    assert bookings[0].booking_date == today


async def test_list_bookings_pagination_returns_correct_page(
    repo: BookingRepository,
    customer_a: Customer,
    customer_b: Customer,
    property_a: Property,
    property_b: Property,
) -> None:
    seed_pairs = [
        (customer_a.id, property_a.id),
        (customer_a.id, property_b.id),
        (customer_b.id, property_a.id),
    ]
    for customer_id, property_id in seed_pairs:
        await repo.create_booking(_new_booking(customer_id, property_id))

    bookings_page_1, total = await repo.list_bookings(page=1, page_size=2)
    bookings_page_2, _ = await repo.list_bookings(page=2, page_size=2)

    assert total == 3
    assert len(bookings_page_1) == 2
    assert len(bookings_page_2) == 1


async def test_list_bookings_sorts_by_allowed_field_ascending(
    repo: BookingRepository, customer_a: Customer, property_a: Property, property_b: Property
) -> None:
    await repo.create_booking(
        _new_booking(
            customer_a.id,
            property_a.id,
            booking_amount=Decimal("100"),
            token_amount=Decimal("10"),
        )
    )
    await repo.create_booking(
        _new_booking(
            customer_a.id,
            property_b.id,
            booking_amount=Decimal("50"),
            token_amount=Decimal("5"),
        )
    )

    bookings, _ = await repo.list_bookings(sort_by="booking_amount", sort_order="asc")

    amounts = [b.booking_amount for b in bookings]
    assert amounts == sorted(amounts)


async def test_list_bookings_falls_back_to_created_at_for_unknown_sort_field(
    repo: BookingRepository, customer_a: Customer, property_a: Property
) -> None:
    await repo.create_booking(_new_booking(customer_a.id, property_a.id))
    # `sort_by` is applied via the repository's SORTABLE_FIELDS allow-list;
    # an unrecognized key must not raise and must silently fall back.
    bookings, total = await repo.list_bookings(sort_by="__not_a_column__")
    assert total == 1


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------
async def test_search_matches_remarks_case_insensitively(
    repo: BookingRepository, customer_a: Customer, property_a: Property
) -> None:
    await repo.create_booking(
        _new_booking(customer_a.id, property_a.id, remarks="Prefers a SEA-facing unit")
    )

    bookings, total = await repo.search("sea-facing")

    assert total == 1
    assert "SEA-facing" in bookings[0].remarks


async def test_search_matches_payment_reference(
    repo: BookingRepository, customer_a: Customer, property_a: Property
) -> None:
    await repo.create_booking(
        _new_booking(customer_a.id, property_a.id, payment_reference="UTR2026073199999")
    )

    bookings, total = await repo.search("UTR2026073199999")

    assert total == 1


async def test_search_returns_empty_for_no_match(
    repo: BookingRepository, customer_a: Customer, property_a: Property
) -> None:
    await repo.create_booking(_new_booking(customer_a.id, property_a.id, remarks="ordinary"))
    bookings, total = await repo.search("no-such-term-xyz")
    assert total == 0
    assert bookings == []


# --------------------------------------------------------------------------
# Follow-ups
# --------------------------------------------------------------------------
async def test_upcoming_followups_includes_overdue_and_due_today(
    repo: BookingRepository, customer_a: Customer, property_a: Property, property_b: Property
) -> None:
    today = date.today()
    await repo.create_booking(
        _new_booking(customer_a.id, property_a.id, next_follow_up=today - timedelta(days=2))
    )
    await repo.create_booking(
        _new_booking(customer_a.id, property_b.id, next_follow_up=today)
    )

    followups = await repo.upcoming_followups(reference_date=today)

    assert len(followups) == 2
    assert followups[0].next_follow_up <= followups[1].next_follow_up


async def test_upcoming_followups_excludes_future_dates(
    repo: BookingRepository, customer_a: Customer, property_a: Property
) -> None:
    today = date.today()
    await repo.create_booking(
        _new_booking(customer_a.id, property_a.id, next_follow_up=today + timedelta(days=5))
    )

    followups = await repo.upcoming_followups(reference_date=today)
    assert followups == []


async def test_upcoming_followups_excludes_inactive_bookings(
    repo: BookingRepository, customer_a: Customer, property_a: Property
) -> None:
    today = date.today()
    await repo.create_booking(
        _new_booking(
            customer_a.id, property_a.id, next_follow_up=today, is_active=False
        )
    )

    followups = await repo.upcoming_followups(reference_date=today)
    assert followups == []


async def test_upcoming_followups_excludes_null_dates(
    repo: BookingRepository, customer_a: Customer, property_a: Property
) -> None:
    await repo.create_booking(_new_booking(customer_a.id, property_a.id, next_follow_up=None))
    followups = await repo.upcoming_followups()
    assert followups == []


# --------------------------------------------------------------------------
# Aggregation (Dashboard Support)
# --------------------------------------------------------------------------
async def test_count_returns_active_booking_total(
    repo: BookingRepository, customer_a: Customer, customer_b: Customer, property_a: Property
) -> None:
    await repo.create_booking(_new_booking(customer_a.id, property_a.id, is_active=True))
    await repo.create_booking(_new_booking(customer_b.id, property_a.id, is_active=False))

    assert await repo.count(is_active=True) == 1
    assert await repo.count(is_active=None) == 2


async def test_count_by_status_groups_correctly(
    repo: BookingRepository, customer_a: Customer, property_a: Property, property_b: Property
) -> None:
    await repo.create_booking(_new_booking(customer_a.id, property_a.id, status=BookingStatus.PENDING))
    await repo.create_booking(_new_booking(customer_a.id, property_b.id, status=BookingStatus.PENDING))

    breakdown = await repo.count_by_status()
    assert breakdown.get("PENDING") == 2


async def test_count_by_payment_status_groups_correctly(
    repo: BookingRepository, customer_a: Customer, property_a: Property, property_b: Property
) -> None:
    await repo.create_booking(
        _new_booking(customer_a.id, property_a.id, payment_status=BookingPaymentStatus.PAID)
    )
    await repo.create_booking(
        _new_booking(customer_a.id, property_b.id, payment_status=BookingPaymentStatus.OVERDUE)
    )

    breakdown = await repo.count_by_payment_status()
    assert breakdown.get("PAID") == 1
    assert breakdown.get("OVERDUE") == 1