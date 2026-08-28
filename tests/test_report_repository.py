"""
test_report_repository.py

Repository-layer test suite for the Reports Module.
Mirrors the testing architecture established for:
Customer / Lead / Property / Booking / Payment / Dashboard repositories.

Scope:
- Revenue report queries
- Booking report queries
- Payment report queries
- Lead report queries
- Customer report queries
- Property report queries
- Dashboard aggregation queries
- Date filters
- Pagination
- Sorting
- Search
- Export queries (raw dataset fetch for PDF/Excel/CSV)
- Empty dataset behavior
- Large dataset behavior
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.report_repository import ReportRepository
from app.models.booking import Booking
from app.models.payment import Payment
from app.models.lead import Lead, LeadStatus
from app.models.customer import Customer
from app.models.property import Property
from app.models.user import User, UserRole


pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest_asyncio.fixture
async def report_repository(db_session: AsyncSession) -> ReportRepository:
    return ReportRepository(db_session)


@pytest_asyncio.fixture
async def seeded_agent(db_session: AsyncSession) -> User:
    agent = User(
        uuid=str(uuid.uuid4()),
        full_name="Agent Smith",
        email=f"agent_{uuid.uuid4().hex[:8]}@example.com",
        phone=f"+1555{uuid.uuid4().int % 10_000_000:07d}",
        password_hash="not-a-real-hash-$2b$12$test.value.only",
        role=UserRole.SALES_AGENT,
        is_active=True,
        is_verified=True,
    )
    db_session.add(agent)
    await db_session.commit()
    await db_session.refresh(agent)
    return agent


@pytest_asyncio.fixture
async def seeded_customer(db_session: AsyncSession, seeded_agent: User) -> Customer:
    customer = Customer(
        id=uuid.uuid4(),
        first_name="John",
        last_name="Buyer",
        email=f"cust_{uuid.uuid4().hex[:8]}@example.com",
        phone=f"9{uuid.uuid4().int % 10**9:09d}",
        created_by_id=seeded_agent.id,
    )
    db_session.add(customer)
    await db_session.commit()
    await db_session.refresh(customer)
    return customer


@pytest_asyncio.fixture
async def seeded_property(db_session: AsyncSession) -> Property:
    prop = Property(
        property_code=f"PROP-{uuid.uuid4().hex[:10].upper()}",
        title="Skyline Apartments 12B",
        property_type="apartment",
        listing_type="sale",
        price=Decimal("15000000.00"),
        area_sqft=1200,
        address="12B Skyline Apartments",
        city="Mumbai",
        state="Maharashtra",
        pincode="400001",
        owner_name="Property Owner",
        owner_phone=f"9{uuid.uuid4().int % 10**9:09d}",
        property_status="available",
    )
    db_session.add(prop)
    await db_session.commit()
    await db_session.refresh(prop)
    return prop


@pytest_asyncio.fixture
async def seeded_booking(
    db_session: AsyncSession, seeded_customer: Customer, seeded_property: Property, seeded_agent: User
) -> Booking:
    booking = Booking(
        id=uuid.uuid4(),
        booking_number=f"BOOK-{uuid.uuid4().hex[:12].upper()}",
        customer_id=seeded_customer.id,
        property_id=seeded_property.id,
        agent_id=seeded_agent.id,
        booking_amount=Decimal("500000.00"),
        status="CONFIRMED",
        booking_date=datetime.now(timezone.utc),
    )
    db_session.add(booking)
    await db_session.commit()
    await db_session.refresh(booking)
    return booking


@pytest_asyncio.fixture
async def seeded_payment(db_session: AsyncSession, seeded_booking: Booking) -> Payment:
    payment = Payment(
        id=uuid.uuid4(),
        payment_number=f"PAY-{uuid.uuid4().hex[:12].upper()}",
        booking_id=seeded_booking.id,
        customer_id=seeded_booking.customer_id,
        property_id=seeded_booking.property_id,
        payment_amount=Decimal("500000.00"),
        payment_status="SUCCESS",
        payment_date=date.today(),
        payment_mode="BANK_TRANSFER",
        payment_type="FULL_PAYMENT",
    )
    db_session.add(payment)
    await db_session.commit()
    await db_session.refresh(payment)
    return payment


@pytest_asyncio.fixture
async def seeded_lead(db_session: AsyncSession, seeded_agent: User) -> Lead:
    lead = Lead(
        id=uuid.uuid4(),
        full_name="Jane Prospect",
        phone=f"9{uuid.uuid4().int % 10**9:09d}",
        email=f"lead_{uuid.uuid4().hex[:8]}@example.com",
        status=LeadStatus.BOOKED,
        assigned_agent_id=seeded_agent.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(lead)
    await db_session.commit()
    await db_session.refresh(lead)
    return lead


async def _bulk_bookings(db_session, customer, prop, agent, count):
    bookings = []
    for i in range(count):
        b = Booking(
            id=uuid.uuid4(),
            booking_number=f"BOOK-{uuid.uuid4().hex[:12].upper()}",
            customer_id=customer.id,
            property_id=prop.id,
            agent_id=agent.id,
            booking_amount=Decimal(str(100000 + i * 1000)),
            status="CONFIRMED",
            # Report tests need many rows for the same customer/property pair.
            # The production schema correctly enforces at most one ACTIVE
            # booking for that pair, so these synthetic report rows are
            # intentionally inactive; the report repository does not filter
            # on is_active for these report queries.
            is_active=False,
            booking_date=datetime.now(timezone.utc) - timedelta(days=i),
        )
        bookings.append(b)
    db_session.add_all(bookings)
    await db_session.commit()
    return bookings


# --------------------------------------------------------------------------
# Revenue Report
# --------------------------------------------------------------------------

class TestRevenueReportRepository:
    async def test_get_revenue_report_returns_data(self, report_repository, seeded_payment):
        result = await report_repository.get_revenue_report(
            start_date=date.today() - timedelta(days=30), end_date=date.today() + timedelta(days=1)
        )
        assert result is not None
        assert len(result) >= 1

    async def test_get_revenue_report_sums_amounts_correctly(self, report_repository, seeded_payment):
        result = await report_repository.get_revenue_report(
            start_date=date.today() - timedelta(days=30), end_date=date.today() + timedelta(days=1)
        )
        total = sum(Decimal(str(r.amount)) for r in result)
        assert total >= Decimal("500000.00")

    async def test_get_revenue_report_empty_dataset(self, report_repository):
        result = await report_repository.get_revenue_report(
            start_date=date(2000, 1, 1), end_date=date(2000, 1, 31)
        )
        assert result == [] or len(result) == 0

    async def test_get_revenue_report_date_filter_excludes_out_of_range(
        self, report_repository, seeded_payment
    ):
        result = await report_repository.get_revenue_report(
            start_date=date.today() + timedelta(days=10),
            end_date=date.today() + timedelta(days=20),
        )
        assert len(result) == 0


# --------------------------------------------------------------------------
# Booking Report
# --------------------------------------------------------------------------

class TestBookingReportRepository:
    async def test_get_booking_report_returns_records(self, report_repository, seeded_booking):
        result = await report_repository.get_booking_report(page=1, page_size=10)
        assert result is not None
        assert result.total >= 1
        assert len(result.items) >= 1

    async def test_get_booking_report_pagination(
        self, report_repository, db_session, seeded_customer, seeded_property, seeded_agent
    ):
        await _bulk_bookings(db_session, seeded_customer, seeded_property, seeded_agent, 25)
        page_one = await report_repository.get_booking_report(page=1, page_size=10)
        page_two = await report_repository.get_booking_report(page=2, page_size=10)
        assert len(page_one.items) == 10
        assert len(page_two.items) == 10
        assert page_one.items[0].id != page_two.items[0].id

    async def test_get_booking_report_sorting_amount_desc(
        self, report_repository, db_session, seeded_customer, seeded_property, seeded_agent
    ):
        await _bulk_bookings(db_session, seeded_customer, seeded_property, seeded_agent, 5)
        result = await report_repository.get_booking_report(
            page=1, page_size=10, sort_by="amount", sort_order="desc"
        )
        amounts = [r.amount for r in result.items]
        assert amounts == sorted(amounts, reverse=True)

    async def test_get_booking_report_search_by_customer_name(
        self, report_repository, seeded_booking, seeded_customer
    ):
        result = await report_repository.get_booking_report(
            page=1, page_size=10, search=seeded_customer.full_name[:4]
        )
        assert len(result.items) >= 1

    async def test_get_booking_report_empty_dataset(self, report_repository):
        result = await report_repository.get_booking_report(
            page=1, page_size=10, start_date=date(1999, 1, 1), end_date=date(1999, 1, 2)
        )
        assert result.total == 0
        assert result.items == []

    async def test_get_booking_report_large_dataset(
        self, report_repository, db_session, seeded_customer, seeded_property, seeded_agent
    ):
        await _bulk_bookings(db_session, seeded_customer, seeded_property, seeded_agent, 200)
        result = await report_repository.get_booking_report(page=1, page_size=50)
        assert result.total >= 200
        assert len(result.items) == 50


# --------------------------------------------------------------------------
# Payment Report
# --------------------------------------------------------------------------

class TestPaymentReportRepository:
    async def test_get_payment_report_returns_records(self, report_repository, seeded_payment):
        result = await report_repository.get_payment_report(page=1, page_size=10)
        assert result.total >= 1

    async def test_get_payment_report_filters_by_status(self, report_repository, seeded_payment):
        result = await report_repository.get_payment_report(
            page=1, page_size=10, status="SUCCESS"
        )
        assert all(getattr(item, "payment_status", item.status) == "SUCCESS" for item in result.items)

    async def test_get_payment_report_filters_by_method(self, report_repository, seeded_payment):
        result = await report_repository.get_payment_report(
            page=1, page_size=10, payment_method="BANK_TRANSFER"
        )
        assert all(getattr(item, "payment_mode", item.payment_method) == "BANK_TRANSFER" for item in result.items)

    async def test_get_payment_report_date_range(self, report_repository, seeded_payment):
        result = await report_repository.get_payment_report(
            page=1,
            page_size=10,
            start_date=date.today() - timedelta(days=1),
            end_date=date.today() + timedelta(days=1),
        )
        assert result.total >= 1

    async def test_get_payment_report_empty_dataset(self, report_repository):
        result = await report_repository.get_payment_report(
            page=1, page_size=10, status="failed"
        )
        assert result.total == 0


# --------------------------------------------------------------------------
# Lead Report
# --------------------------------------------------------------------------

class TestLeadReportRepository:
    async def test_get_lead_report_returns_records(self, report_repository, seeded_lead):
        result = await report_repository.get_lead_report(page=1, page_size=10)
        assert result.total >= 1

    async def test_get_lead_report_filters_by_status(self, report_repository, seeded_lead):
        result = await report_repository.get_lead_report(
            page=1, page_size=10, status="BOOKED"
        )
        assert all(item.status == LeadStatus.BOOKED for item in result.items)

    async def test_get_lead_report_filters_by_agent(self, report_repository, seeded_lead, seeded_agent):
        result = await report_repository.get_lead_report(
            page=1, page_size=10, agent_id=seeded_agent.id
        )
        assert all(item.assigned_agent_id == seeded_agent.id for item in result.items)

    async def test_get_lead_conversion_stats(self, report_repository, seeded_lead):
        stats = await report_repository.get_lead_conversion_stats(
            start_date=date.today() - timedelta(days=30), end_date=date.today() + timedelta(days=1)
        )
        assert stats is not None
        assert "converted" in stats or hasattr(stats, "converted")

    async def test_get_lead_report_empty_dataset(self, report_repository):
        result = await report_repository.get_lead_report(page=1, page_size=10, status="lost")
        assert result.total == 0


# --------------------------------------------------------------------------
# Customer Report
# --------------------------------------------------------------------------

class TestCustomerReportRepository:
    async def test_get_customer_report_returns_records(self, report_repository, seeded_customer):
        result = await report_repository.get_customer_report(page=1, page_size=10)
        assert result.total >= 1

    async def test_get_customer_report_search(self, report_repository, seeded_customer):
        result = await report_repository.get_customer_report(
            page=1, page_size=10, search=seeded_customer.full_name[:4]
        )
        assert len(result.items) >= 1

    async def test_get_customer_analytics(self, report_repository, seeded_customer, seeded_booking):
        analytics = await report_repository.get_customer_analytics(customer_id=seeded_customer.id)
        assert analytics is not None

    async def test_get_customer_report_empty_dataset(self, report_repository):
        result = await report_repository.get_customer_report(
            page=1, page_size=10, search="nonexistent_customer_xyz"
        )
        assert result.total == 0


# --------------------------------------------------------------------------
# Property Report
# --------------------------------------------------------------------------

class TestPropertyReportRepository:
    async def test_get_property_report_returns_records(self, report_repository, seeded_property):
        result = await report_repository.get_property_report(page=1, page_size=10)
        assert result.total >= 1

    async def test_get_property_report_filters_by_status(self, report_repository, seeded_property):
        result = await report_repository.get_property_report(
            page=1, page_size=10, status="available"
        )
        assert all(item.property_status == "available" for item in result.items)

    async def test_get_top_performing_properties(self, report_repository, seeded_booking):
        result = await report_repository.get_top_properties(limit=5)
        assert result is not None
        assert len(result) <= 5

    async def test_get_property_report_empty_dataset(self, report_repository):
        result = await report_repository.get_property_report(
            page=1, page_size=10, city="NonexistentCity"
        )
        assert result.total == 0


# --------------------------------------------------------------------------
# Dashboard Aggregation
# --------------------------------------------------------------------------

class TestDashboardAggregationRepository:
    async def test_get_dashboard_summary(
        self, report_repository, seeded_booking, seeded_payment, seeded_lead
    ):
        summary = await report_repository.get_dashboard_summary()
        assert summary is not None

    async def test_get_dashboard_summary_with_date_range(
        self, report_repository, seeded_booking, seeded_payment
    ):
        summary = await report_repository.get_dashboard_summary(
            start_date=date.today() - timedelta(days=7), end_date=date.today()
        )
        assert summary is not None

    async def test_get_top_agents(self, report_repository, seeded_booking):
        result = await report_repository.get_top_agents(limit=5)
        assert result is not None
        assert len(result) <= 5

    async def test_get_dashboard_summary_empty_dataset(self, report_repository):
        summary = await report_repository.get_dashboard_summary(
            start_date=date(1990, 1, 1), end_date=date(1990, 1, 2)
        )
        assert summary is not None


# --------------------------------------------------------------------------
# Export Queries
# --------------------------------------------------------------------------

class TestExportQueriesRepository:
    async def test_get_export_dataset_revenue(self, report_repository, seeded_payment):
        rows = await report_repository.get_export_dataset(
            report_type="revenue",
            start_date=date.today() - timedelta(days=30),
            end_date=date.today() + timedelta(days=1),
        )
        assert rows is not None
        assert len(rows) >= 1

    async def test_get_export_dataset_bookings(self, report_repository, seeded_booking):
        rows = await report_repository.get_export_dataset(
            report_type="bookings",
            start_date=date.today() - timedelta(days=30),
            end_date=date.today() + timedelta(days=1),
        )
        assert rows is not None

    async def test_get_export_dataset_empty(self, report_repository):
        rows = await report_repository.get_export_dataset(
            report_type="revenue",
            start_date=date(1990, 1, 1),
            end_date=date(1990, 1, 2),
        )
        assert rows == [] or len(rows) == 0

    async def test_get_export_dataset_large(
        self, report_repository, db_session, seeded_customer, seeded_property, seeded_agent
    ):
        await _bulk_bookings(db_session, seeded_customer, seeded_property, seeded_agent, 150)
        rows = await report_repository.get_export_dataset(
            report_type="bookings",
            start_date=date.today() - timedelta(days=200),
            end_date=date.today() + timedelta(days=1),
        )
        assert len(rows) >= 150