from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.repositories.dashboard_repository import DashboardRepository

pytestmark = pytest.mark.asyncio


def _result_with_one(row):
    result = MagicMock()
    result.one.return_value = row
    return result


def _result_with_scalar(value):
    result = MagicMock()
    result.scalar_one.return_value = value
    return result


def _result_with_all(rows):
    result = MagicMock()
    result.all.return_value = rows
    return result


def _result_with_scalars(items):
    result = MagicMock()
    result.scalars.return_value.all.return_value = items
    return result


@pytest.fixture
def mock_session():
    return AsyncMock()


@pytest.fixture
def repository(mock_session):
    return DashboardRepository(mock_session)


class TestDashboardSummary:
    async def test_get_dashboard_summary_returns_aggregated_counts(self, repository, mock_session):
        summary_row = SimpleNamespace(
            total_customers=1240,
            total_leads=980,
            total_properties=520,
            total_bookings=340,
        )
        mock_session.execute.side_effect = [
            _result_with_one(summary_row),
            _result_with_scalar(Decimal("8500000.00")),
        ]

        result = await repository.get_dashboard_summary()

        assert result["total_customers"] == 1240
        assert result["total_leads"] == 980
        assert result["total_properties"] == 520
        assert result["total_bookings"] == 340
        assert result["total_revenue"] == Decimal("8500000.00")
        assert mock_session.execute.call_count == 2

    async def test_get_dashboard_summary_handles_null_aggregates(self, repository, mock_session):
        summary_row = SimpleNamespace(
            total_customers=None,
            total_leads=None,
            total_properties=None,
            total_bookings=None,
        )
        mock_session.execute.side_effect = [
            _result_with_one(summary_row),
            _result_with_scalar(None),
        ]

        result = await repository.get_dashboard_summary()

        assert result["total_customers"] == 0
        assert result["total_revenue"] == Decimal("0")


class TestTodaySummary:
    async def test_get_today_summary_returns_daily_counts(self, repository, mock_session):
        mock_session.execute.side_effect = [
            _result_with_scalar(12),
            _result_with_scalar(5),
            _result_with_scalar(3),
            _result_with_scalar(Decimal("21000.00")),
        ]

        result = await repository.get_today_summary()

        assert result["new_customers"] == 12
        assert result["new_leads"] == 5
        assert result["new_bookings"] == 3
        assert result["revenue_today"] == Decimal("21000.00")
        assert result["date"] == date.today()
        assert mock_session.execute.call_count == 4


class TestRevenueQueries:
    async def test_get_monthly_revenue_returns_decimal(self, repository, mock_session):
        mock_session.execute.return_value = _result_with_scalar(Decimal("645000.00"))

        result = await repository.get_monthly_revenue(2026, 7)

        assert result == Decimal("645000.00")
        mock_session.execute.assert_awaited_once()

    async def test_get_monthly_revenue_defaults_to_zero(self, repository, mock_session):
        mock_session.execute.return_value = _result_with_scalar(None)

        result = await repository.get_monthly_revenue(2026, 7)

        assert result == Decimal("0")

    async def test_get_weekly_revenue_returns_decimal(self, repository, mock_session):
        mock_session.execute.return_value = _result_with_scalar(Decimal("158000.00"))

        result = await repository.get_weekly_revenue(date(2026, 7, 27), date(2026, 8, 2))

        assert result == Decimal("158000.00")

    async def test_get_revenue_trend_returns_grouped_rows(self, repository, mock_session):
        rows = [
            SimpleNamespace(year=2026, month=7, total_amount=Decimal("645000.00"), total_count=38),
            SimpleNamespace(year=2026, month=6, total_amount=Decimal("712000.00"), total_count=41),
        ]
        mock_session.execute.return_value = _result_with_all(rows)

        result = await repository.get_revenue_trend(months=12)

        assert len(result) == 2
        assert result[0].year == 2026

    async def test_get_revenue_trend_query_uses_group_by_and_limit(self, repository, mock_session):
        mock_session.execute.return_value = _result_with_all([])

        await repository.get_revenue_trend(months=6)

        stmt = mock_session.execute.call_args[0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "GROUP BY" in compiled.upper()
        assert "LIMIT 6" in compiled.upper()

    async def test_get_outstanding_revenue_returns_decimal(self, repository, mock_session):
        mock_session.execute.return_value = _result_with_scalar(Decimal("1200000.00"))

        result = await repository.get_outstanding_revenue()

        assert result == Decimal("1200000.00")


class TestLeadAnalytics:
    async def test_get_lead_statistics_returns_status_breakdown(self, repository, mock_session):
        row = SimpleNamespace(
            total_leads=980,
            new_leads=210,
            contacted_leads=180,
            qualified_leads=150,
            negotiation_leads=90,
            converted_leads=260,
            lost_leads=90,
        )
        mock_session.execute.return_value = _result_with_one(row)

        result = await repository.get_lead_statistics()

        assert result["total_leads"] == 980
        assert result["converted_leads"] == 260

    async def test_get_lead_conversion_rate_computes_percentage(self, repository, mock_session):
        row = SimpleNamespace(total_leads=1000, converted_leads=260)
        mock_session.execute.return_value = _result_with_one(row)

        result = await repository.get_lead_conversion_rate()

        assert result == 26.0

    async def test_get_lead_conversion_rate_zero_total_returns_zero(self, repository, mock_session):
        row = SimpleNamespace(total_leads=0, converted_leads=0)
        mock_session.execute.return_value = _result_with_one(row)

        result = await repository.get_lead_conversion_rate()

        assert result == 0.0


class TestBookingAnalytics:
    async def test_get_booking_statistics_returns_status_breakdown(self, repository, mock_session):
        row = SimpleNamespace(
            total_bookings=340,
            pending_bookings=40,
            confirmed_bookings=120,
            cancelled_bookings=20,
            completed_bookings=160,
            total_booking_value=Decimal("7900000.00"),
        )
        mock_session.execute.return_value = _result_with_one(row)

        result = await repository.get_booking_statistics()

        assert result["total_bookings"] == 340
        assert result["total_booking_value"] == Decimal("7900000.00")


class TestPropertyAnalytics:
    async def test_get_property_statistics_returns_status_breakdown(self, repository, mock_session):
        row = SimpleNamespace(
            total_properties=520,
            available_properties=300,
            reserved_properties=60,
            sold_properties=110,
            rented_properties=40,
            inactive_properties=10,
            average_property_price=Decimal("4250000.00"),
        )
        mock_session.execute.return_value = _result_with_one(row)

        result = await repository.get_property_statistics()

        assert result["total_properties"] == 520
        assert result["average_property_price"] == Decimal("4250000.00")


class TestCustomerAnalytics:
    async def test_get_customer_statistics_returns_counts(self, repository, mock_session):
        row = SimpleNamespace(
            total_customers=1240,
            new_customers_this_month=85,
            distinct_cities=34,
        )
        mock_session.execute.return_value = _result_with_one(row)

        result = await repository.get_customer_statistics()

        assert result["total_customers"] == 1240
        assert result["distinct_cities"] == 34


class TestPaymentAnalytics:
    async def test_get_payment_statistics_returns_breakdown(self, repository, mock_session):
        row = SimpleNamespace(
            collected_revenue=Decimal("7200000.00"),
            pending_revenue=Decimal("1200000.00"),
            refunded_revenue=Decimal("100000.00"),
            total_transactions=412,
            average_transaction_value=Decimal("17475.73"),
        )
        mock_session.execute.return_value = _result_with_one(row)

        result = await repository.get_payment_statistics()

        assert result["collected_revenue"] == Decimal("7200000.00")
        assert result["total_revenue"] == Decimal("8400000.00")
        assert result["total_transactions"] == 412

    async def test_get_pending_payments_applies_status_filter(self, repository, mock_session):
        mock_session.execute.return_value = _result_with_scalars([])

        await repository.get_pending_payments(limit=10, offset=0)

        stmt = mock_session.execute.call_args[0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
        assert "status" in compiled


class TestAgentPerformance:
    async def test_get_agent_performance_returns_rows(self, repository, mock_session):
        rows = [
            SimpleNamespace(
                agent_id=14,
                agent_name="Ananya Rao",
                total_leads_assigned=62,
                total_leads_converted=19,
                total_bookings=21,
                total_revenue_generated=Decimal("512000.00"),
            )
        ]
        mock_session.execute.return_value = _result_with_all(rows)

        result = await repository.get_agent_performance(limit=20, offset=0)

        assert len(result) == 1
        assert result[0].agent_name == "Ananya Rao"

    async def test_get_agent_performance_applies_pagination(self, repository, mock_session):
        mock_session.execute.return_value = _result_with_all([])

        await repository.get_agent_performance(limit=5, offset=10)

        stmt = mock_session.execute.call_args[0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).upper()
        assert "LIMIT 5" in compiled
        assert "OFFSET 10" in compiled


class TestRecentRecords:
    async def test_get_recent_customers_returns_scalars(self, repository, mock_session):
        customers = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        mock_session.execute.return_value = _result_with_scalars(customers)

        result = await repository.get_recent_customers(limit=10, offset=0)

        assert len(result) == 2

    async def test_get_recent_leads_applies_limit(self, repository, mock_session):
        mock_session.execute.return_value = _result_with_scalars([])

        await repository.get_recent_leads(limit=7, offset=0)

        stmt = mock_session.execute.call_args[0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).upper()
        assert "LIMIT 7" in compiled

    async def test_get_recent_bookings_returns_scalars(self, repository, mock_session):
        bookings = [SimpleNamespace(id=1)]
        mock_session.execute.return_value = _result_with_scalars(bookings)

        result = await repository.get_recent_bookings(limit=10, offset=0)

        assert len(result) == 1

    async def test_get_recent_payments_returns_scalars(self, repository, mock_session):
        payments = [SimpleNamespace(id=1)]
        mock_session.execute.return_value = _result_with_scalars(payments)

        result = await repository.get_recent_payments(limit=10, offset=0)

        assert len(result) == 1


class TestTopRankings:
    async def test_get_top_agents_orders_by_revenue(self, repository, mock_session):
        rows = [SimpleNamespace(agent_id=14, agent_name="Ananya Rao", total_bookings=21, total_revenue_generated=Decimal("512000.00"))]
        mock_session.execute.return_value = _result_with_all(rows)

        result = await repository.get_top_agents(limit=5)

        assert result[0].agent_id == 14

    async def test_get_top_cities_returns_grouped_rows(self, repository, mock_session):
        rows = [SimpleNamespace(city="Chennai", total_properties=96, total_bookings=58)]
        mock_session.execute.return_value = _result_with_all(rows)

        result = await repository.get_top_cities(limit=5)

        assert result[0].city == "Chennai"

    async def test_get_top_properties_returns_grouped_rows(self, repository, mock_session):
        rows = [
            SimpleNamespace(
                property_id=88,
                title="Lakeview Residency, Tower B",
                city="Chennai",
                total_bookings=12,
                total_revenue=Decimal("610000.00"),
            )
        ]
        mock_session.execute.return_value = _result_with_all(rows)

        result = await repository.get_top_properties(limit=5)

        assert result[0].property_id == 88


class TestUpcomingFollowups:
    async def test_get_upcoming_followups_returns_scalars(self, repository, mock_session):
        leads = [SimpleNamespace(id=3320, follow_up_date=date(2026, 8, 3))]
        mock_session.execute.return_value = _result_with_scalars(leads)

        result = await repository.get_upcoming_followups(limit=10, offset=0)

        assert len(result) == 1

    async def test_get_upcoming_followups_filters_by_status_and_date(self, repository, mock_session):
        mock_session.execute.return_value = _result_with_scalars([])

        await repository.get_upcoming_followups(limit=10, offset=0)

        stmt = mock_session.execute.call_args[0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
        assert "follow_up_date" in compiled
        assert "status" in compiled