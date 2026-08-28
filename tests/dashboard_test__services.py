from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import BadRequestException, ValidationException
from app.models.user import UserRole
from app.schemas.dashboard import (
    AgentPerformance,
    BookingSummary,
    CustomerSummary,
    LeadSummary,
    PropertySummary,
    RevenueSummary,
)
from app.services.dashboard_service import DashboardService

pytestmark = pytest.mark.asyncio


def make_user(role=UserRole.ADMIN, user_id=1):
    return SimpleNamespace(id=user_id, role=role)


@pytest.fixture
def service():
    instance = DashboardService(db=AsyncMock())
    instance.repository = AsyncMock()
    return instance


class TestAccessValidation:
    async def test_validate_access_allows_admin(self, service):
        service._validate_access(make_user(role=UserRole.ADMIN))

    async def test_validate_access_allows_sales_manager(self, service):
        service._validate_access(make_user(role=UserRole.SALES_MANAGER))

    async def test_validate_access_allows_sales_agent(self, service):
        service._validate_access(make_user(role=UserRole.SALES_AGENT))

    async def test_validate_access_raises_for_none_user(self, service):
        with pytest.raises(ValidationException):
            service._validate_access(None)

    async def test_validate_access_raises_for_unauthorized_role(self, service):
        with pytest.raises(ValidationException):
            service._validate_access(make_user(role="GUEST"))


class TestRevenueOverview:
    async def test_get_revenue_overview_maps_repository_stats(self, service):
        service.repository.get_payment_statistics.return_value = {
            "total_revenue": Decimal("8400000.00"),
            "collected_revenue": Decimal("7200000.00"),
            "pending_revenue": Decimal("1200000.00"),
            "refunded_revenue": Decimal("100000.00"),
            "total_transactions": 412,
            "average_transaction_value": Decimal("17475.73"),
        }

        result = await service.get_revenue_overview(make_user())

        assert isinstance(result, RevenueSummary)
        assert result.collected_revenue == Decimal("7200000.00")

    async def test_get_revenue_overview_raises_for_unauthorized_user(self, service):
        with pytest.raises(ValidationException):
            await service.get_revenue_overview(None)


class TestLeadOverview:
    async def test_get_lead_overview_includes_conversion_rate(self, service):
        service.repository.get_lead_statistics.return_value = {
            "total_leads": 1000,
            "new_leads": 210,
            "contacted_leads": 180,
            "qualified_leads": 150,
            "negotiation_leads": 90,
            "converted_leads": 260,
            "lost_leads": 90,
        }
        service.repository.get_lead_conversion_rate.return_value = 26.0

        result = await service.get_lead_overview(make_user())

        assert isinstance(result, LeadSummary)
        assert result.conversion_rate == 26.0
        assert result.total_leads == 1000


class TestBookingOverview:
    async def test_get_booking_overview_maps_repository_stats(self, service):
        service.repository.get_booking_statistics.return_value = {
            "total_bookings": 340,
            "pending_bookings": 40,
            "confirmed_bookings": 120,
            "cancelled_bookings": 20,
            "completed_bookings": 160,
            "total_booking_value": Decimal("7900000.00"),
        }

        result = await service.get_booking_overview(make_user())

        assert isinstance(result, BookingSummary)
        assert result.total_bookings == 340


class TestPropertyOverview:
    async def test_get_property_overview_maps_repository_stats(self, service):
        service.repository.get_property_statistics.return_value = {
            "total_properties": 520,
            "available_properties": 300,
            "reserved_properties": 60,
            "sold_properties": 110,
            "rented_properties": 40,
            "inactive_properties": 10,
            "average_property_price": Decimal("4250000.00"),
        }

        result = await service.get_property_overview(make_user())

        assert isinstance(result, PropertySummary)
        assert result.available_properties == 300


class TestCustomerOverview:
    async def test_get_customer_overview_maps_repository_stats(self, service):
        service.repository.get_customer_statistics.return_value = {
            "total_customers": 1240,
            "new_customers_this_month": 85,
            "distinct_cities": 34,
        }

        result = await service.get_customer_overview(make_user())

        assert isinstance(result, CustomerSummary)
        assert result.total_customers == 1240


class TestDashboardSummary:
    async def test_get_dashboard_summary_aggregates_all_sections(self, service):
        service.repository.get_payment_statistics.return_value = {
            "total_revenue": Decimal("8400000.00"),
            "collected_revenue": Decimal("7200000.00"),
            "pending_revenue": Decimal("1200000.00"),
            "refunded_revenue": Decimal("100000.00"),
            "total_transactions": 412,
            "average_transaction_value": Decimal("17475.73"),
        }
        service.repository.get_lead_statistics.return_value = {
            "total_leads": 1000,
            "new_leads": 210,
            "contacted_leads": 180,
            "qualified_leads": 150,
            "negotiation_leads": 90,
            "converted_leads": 260,
            "lost_leads": 90,
        }
        service.repository.get_lead_conversion_rate.return_value = 26.0
        service.repository.get_booking_statistics.return_value = {
            "total_bookings": 340,
            "pending_bookings": 40,
            "confirmed_bookings": 120,
            "cancelled_bookings": 20,
            "completed_bookings": 160,
            "total_booking_value": Decimal("7900000.00"),
        }
        service.repository.get_property_statistics.return_value = {
            "total_properties": 520,
            "available_properties": 300,
            "reserved_properties": 60,
            "sold_properties": 110,
            "rented_properties": 40,
            "inactive_properties": 10,
            "average_property_price": Decimal("4250000.00"),
        }
        service.repository.get_customer_statistics.return_value = {
            "total_customers": 1240,
            "new_customers_this_month": 85,
            "distinct_cities": 34,
        }

        result = await service.get_dashboard_summary(make_user())

        assert result.revenue.collected_revenue == Decimal("7200000.00")
        assert result.leads.conversion_rate == 26.0
        assert result.bookings.total_bookings == 340
        assert result.properties.total_properties == 520
        assert result.customers.total_customers == 1240
        assert isinstance(result.generated_at, datetime)


class TestMonthlyRevenueTrend:
    async def test_get_monthly_revenue_trend_reverses_to_chronological_order(self, service):
        service.repository.get_revenue_trend.return_value = [
            SimpleNamespace(year=2026, month=7, total_count=38, total_amount=Decimal("645000.00")),
            SimpleNamespace(year=2026, month=6, total_count=41, total_amount=Decimal("712000.00")),
        ]

        result = await service.get_monthly_revenue_trend(make_user(), months=12)

        assert result[0].month == 6
        assert result[1].month == 7
        assert result[0].label == "Jun 2026"

    async def test_get_monthly_revenue_trend_rejects_out_of_range_months(self, service):
        with pytest.raises(BadRequestException):
            await service.get_monthly_revenue_trend(make_user(), months=48)

    async def test_get_monthly_revenue_trend_rejects_zero_months(self, service):
        with pytest.raises(BadRequestException):
            await service.get_monthly_revenue_trend(make_user(), months=0)


class TestWeeklyRevenueTrend:
    async def test_get_weekly_revenue_trend_returns_requested_number_of_weeks(self, service):
        service.repository.get_weekly_revenue.return_value = Decimal("158000.00")

        result = await service.get_weekly_revenue_trend(make_user(), weeks=4)

        assert len(result) == 4
        assert service.repository.get_weekly_revenue.await_count == 4

    async def test_get_weekly_revenue_trend_rejects_out_of_range_weeks(self, service):
        with pytest.raises(BadRequestException):
            await service.get_weekly_revenue_trend(make_user(), weeks=60)


class TestDailyRevenueOverviewAndGrowth:
    async def test_get_daily_revenue_overview_computes_growth_percentages(self, service):
        service.repository.get_weekly_revenue.side_effect = [
            Decimal("21000.00"),
            Decimal("18500.00"),
            Decimal("158000.00"),
            Decimal("142000.00"),
        ]
        service.repository.get_monthly_revenue.side_effect = [
            Decimal("645000.00"),
            Decimal("712000.00"),
        ]
        service.repository.get_booking_statistics.return_value = {
            "total_bookings": 340,
            "confirmed_bookings": 120,
            "completed_bookings": 160,
        }
        service.repository.get_property_statistics.return_value = {
            "total_properties": 520,
            "available_properties": 300,
        }
        service.repository.get_outstanding_revenue.return_value = Decimal("1200000.00")

        result = await service.get_daily_revenue_overview(make_user())

        assert result["today_revenue"] == Decimal("21000.00")
        assert result["daily_growth_percentage"] == pytest.approx(13.51, rel=1e-2)
        assert result["booking_conversion_rate"] == pytest.approx(82.35, rel=1e-2)
        assert result["property_availability_rate"] == pytest.approx(57.69, rel=1e-2)
        assert result["outstanding_amount"] == Decimal("1200000.00")

    async def test_calculate_growth_with_zero_previous_and_positive_current(self, service):
        assert service._calculate_growth(Decimal("500.00"), Decimal("0")) == 100.0

    async def test_calculate_growth_with_zero_previous_and_zero_current(self, service):
        assert service._calculate_growth(Decimal("0"), Decimal("0")) == 0.0

    async def test_calculate_growth_with_positive_change(self, service):
        assert service._calculate_growth(Decimal("120"), Decimal("100")) == 20.0

    async def test_calculate_growth_with_negative_change(self, service):
        assert service._calculate_growth(Decimal("80"), Decimal("100")) == -20.0

    async def test_calculate_booking_conversion_rate_zero_total(self, service):
        assert service._calculate_booking_conversion_rate({"total_bookings": 0}) == 0.0

    async def test_calculate_property_availability_rate_zero_total(self, service):
        assert service._calculate_property_availability_rate({"total_properties": 0}) == 0.0


class TestAgentPerformance:
    async def test_get_agents_performance_returns_all_for_admin(self, service):
        service.repository.get_agent_performance.return_value = [
            SimpleNamespace(
                agent_id=14,
                agent_name="Ananya Rao",
                total_leads_assigned=62,
                total_leads_converted=19,
                total_bookings=21,
                total_revenue_generated=Decimal("512000.00"),
            ),
            SimpleNamespace(
                agent_id=15,
                agent_name="Vikram Shah",
                total_leads_assigned=40,
                total_leads_converted=10,
                total_bookings=12,
                total_revenue_generated=Decimal("300000.00"),
            ),
        ]

        result = await service.get_agents_performance(make_user(role=UserRole.ADMIN))

        assert len(result) == 2
        assert all(isinstance(item, AgentPerformance) for item in result)

    async def test_get_agents_performance_scopes_sales_agent_to_self(self, service):
        service.repository.get_agent_performance.return_value = [
            SimpleNamespace(
                agent_id=14,
                agent_name="Ananya Rao",
                total_leads_assigned=62,
                total_leads_converted=19,
                total_bookings=21,
                total_revenue_generated=Decimal("512000.00"),
            ),
            SimpleNamespace(
                agent_id=15,
                agent_name="Vikram Shah",
                total_leads_assigned=40,
                total_leads_converted=10,
                total_bookings=12,
                total_revenue_generated=Decimal("300000.00"),
            ),
        ]

        result = await service.get_agents_performance(
            make_user(role=UserRole.SALES_AGENT, user_id=14)
        )

        assert len(result) == 1
        assert result[0].agent_id == 14

    async def test_get_agents_performance_rejects_invalid_pagination(self, service):
        with pytest.raises(BadRequestException):
            await service.get_agents_performance(make_user(), limit=0)

    async def test_get_agents_performance_computes_conversion_rate(self, service):
        service.repository.get_agent_performance.return_value = [
            SimpleNamespace(
                agent_id=14,
                agent_name="Ananya Rao",
                total_leads_assigned=50,
                total_leads_converted=10,
                total_bookings=10,
                total_revenue_generated=Decimal("100000.00"),
            )
        ]

        result = await service.get_agents_performance(make_user())

        assert result[0].conversion_rate == 20.0
        assert result[0].average_deal_size == Decimal("10000.00")

    async def test_get_top_agents_scopes_sales_agent_to_self(self, service):
        service.repository.get_top_agents.return_value = [
            SimpleNamespace(
                agent_id=14, agent_name="Ananya Rao", total_bookings=21,
                total_revenue_generated=Decimal("512000.00"),
            ),
            SimpleNamespace(
                agent_id=15, agent_name="Vikram Shah", total_bookings=12,
                total_revenue_generated=Decimal("300000.00"),
            ),
        ]

        result = await service.get_top_agents(
            make_user(role=UserRole.SALES_AGENT, user_id=15)
        )

        assert len(result) == 1
        assert result[0].agent_id == 15

    async def test_get_top_agents_rejects_invalid_limit(self, service):
        with pytest.raises(BadRequestException):
            await service.get_top_agents(make_user(), limit=0)


class TestRankings:
    async def test_get_top_properties_returns_formatted_dicts(self, service):
        service.repository.get_top_properties.return_value = [
            SimpleNamespace(
                property_id=88,
                title="Lakeview Residency, Tower B",
                city="Chennai",
                total_bookings=12,
                total_revenue=Decimal("610000.00"),
            )
        ]

        result = await service.get_top_properties(make_user())

        assert result[0]["property_id"] == 88
        assert result[0]["total_revenue"] == Decimal("610000.00")

    async def test_get_top_cities_returns_formatted_dicts(self, service):
        service.repository.get_top_cities.return_value = [
            SimpleNamespace(city="Chennai", total_properties=96, total_bookings=58)
        ]

        result = await service.get_top_cities(make_user())

        assert result[0]["city"] == "Chennai"

    async def test_get_top_properties_rejects_invalid_limit(self, service):
        with pytest.raises(BadRequestException):
            await service.get_top_properties(make_user(), limit=-1)


class TestUpcomingFollowups:
    async def test_get_upcoming_followups_scopes_sales_agent(self, service):
        service.repository.get_upcoming_followups.return_value = [
            SimpleNamespace(
                id=3320, customer_id=1188, status="negotiation",
                follow_up_date=date(2026, 8, 3), agent_id=14,
            ),
            SimpleNamespace(
                id=3321, customer_id=1189, status="new",
                follow_up_date=date(2026, 8, 4), agent_id=15,
            ),
        ]

        result = await service.get_upcoming_followups(
            make_user(role=UserRole.SALES_AGENT, user_id=14)
        )

        assert len(result) == 1
        assert result[0]["lead_id"] == 3320

    async def test_get_upcoming_followups_rejects_invalid_pagination(self, service):
        with pytest.raises(BadRequestException):
            await service.get_upcoming_followups(make_user(), offset=-1)


class TestRecentActivities:
    async def test_get_recent_activities_merges_and_sorts_by_recency(self, service):
        service.repository.get_recent_customers.return_value = [
            SimpleNamespace(
                id=1, full_name="Rahul Mehta", email="rahul@example.com",
                created_at=datetime(2026, 8, 1, 6, 0, 0),
            )
        ]
        service.repository.get_recent_leads.return_value = [
            SimpleNamespace(
                id=2, status="converted", created_at=datetime(2026, 8, 1, 7, 45, 0),
                updated_at=datetime(2026, 8, 1, 7, 45, 0),
            )
        ]
        service.repository.get_recent_bookings.return_value = [
            SimpleNamespace(
                id=3, status="confirmed", amount=Decimal("45000.00"),
                created_at=datetime(2026, 8, 1, 5, 0, 0),
            )
        ]
        service.repository.get_recent_payments.return_value = [
            SimpleNamespace(
                id=4, status="completed", amount=Decimal("45000.00"),
                created_at=datetime(2026, 8, 1, 8, 12, 0),
            )
        ]

        result = await service.get_recent_activities(make_user(), limit=10)

        assert len(result) == 4
        assert result[0].reference_id == 4
        assert result[0].activity_type.value == "payment_received"

    async def test_get_recent_activities_rejects_invalid_limit(self, service):
        with pytest.raises(BadRequestException):
            await service.get_recent_activities(make_user(), limit=0)


class TestFullDashboard:
    async def test_get_full_dashboard_assembles_all_sections(self, service):
        service.repository.get_payment_statistics.return_value = {
            "total_revenue": Decimal("8400000.00"),
            "collected_revenue": Decimal("7200000.00"),
            "pending_revenue": Decimal("1200000.00"),
            "refunded_revenue": Decimal("100000.00"),
            "total_transactions": 412,
            "average_transaction_value": Decimal("17475.73"),
        }
        service.repository.get_lead_statistics.return_value = {
            "total_leads": 1000,
            "new_leads": 210,
            "contacted_leads": 180,
            "qualified_leads": 150,
            "negotiation_leads": 90,
            "converted_leads": 260,
            "lost_leads": 90,
        }
        service.repository.get_lead_conversion_rate.return_value = 26.0
        service.repository.get_booking_statistics.return_value = {
            "total_bookings": 340,
            "pending_bookings": 40,
            "confirmed_bookings": 120,
            "cancelled_bookings": 20,
            "completed_bookings": 160,
            "total_booking_value": Decimal("7900000.00"),
        }
        service.repository.get_property_statistics.return_value = {
            "total_properties": 520,
            "available_properties": 300,
            "reserved_properties": 60,
            "sold_properties": 110,
            "rented_properties": 40,
            "inactive_properties": 10,
            "average_property_price": Decimal("4250000.00"),
        }
        service.repository.get_customer_statistics.return_value = {
            "total_customers": 1240,
            "new_customers_this_month": 85,
            "distinct_cities": 34,
        }
        service.repository.get_top_agents.return_value = []
        service.repository.get_recent_customers.return_value = []
        service.repository.get_recent_leads.return_value = []
        service.repository.get_recent_bookings.return_value = []
        service.repository.get_recent_payments.return_value = []
        service.repository.get_revenue_trend.return_value = []

        result = await service.get_full_dashboard(make_user())

        assert result.summary.revenue.collected_revenue == Decimal("7200000.00")
        assert result.top_agents == []
        assert result.recent_activities == []
        assert result.revenue_chart is not None
        assert result.booking_chart is not None
        assert result.lead_chart is not None
        assert result.property_chart is not None
        assert result.payment_chart is not None

    async def test_get_full_dashboard_raises_for_unauthenticated_user(self, service):
        with pytest.raises(ValidationException):
            await service.get_full_dashboard(None)