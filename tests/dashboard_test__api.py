from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_user
from app.api.v1.dashboard import get_dashboard_service, router as dashboard_router
from app.core.exceptions import BadRequestException, NotFoundException, ValidationException
from app.models.user import UserRole
from app.schemas.dashboard import (
    AgentPerformance,
    BookingChart,
    BookingSummary,
    CustomerSummary,
    DashboardResponse,
    DashboardSummary,
    LeadChart,
    LeadSummary,
    MonthlyTrend,
    PaymentChart,
    PropertyChart,
    PropertySummary,
    RecentActivity,
    RevenueChart,
    RevenueSummary,
    WeeklyTrend,
)

pytestmark = pytest.mark.asyncio


def build_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(dashboard_router, prefix="/api/v1")

    @app.exception_handler(ValidationException)
    async def validation_exception_handler(request, exc):
        return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"detail": str(exc)})

    @app.exception_handler(BadRequestException)
    async def bad_request_exception_handler(request, exc):
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})

    @app.exception_handler(NotFoundException)
    async def not_found_exception_handler(request, exc):
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})

    return app


def make_user(role=UserRole.ADMIN, user_id=1):
    return SimpleNamespace(id=user_id, role=role)


@pytest.fixture
def app():
    return build_test_app()


@pytest.fixture
def mock_service():
    return AsyncMock()


@pytest_asyncio.fixture
async def client(app, mock_service):
    app.dependency_overrides[get_current_user] = lambda: make_user()
    app.dependency_overrides[get_dashboard_service] = lambda: mock_service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client

    app.dependency_overrides.clear()


def sample_dashboard_summary() -> DashboardSummary:
    return DashboardSummary(
        revenue=RevenueSummary(
            total_revenue=Decimal("8400000.00"),
            collected_revenue=Decimal("7200000.00"),
            pending_revenue=Decimal("1200000.00"),
            refunded_revenue=Decimal("100000.00"),
            total_transactions=412,
            average_transaction_value=Decimal("17475.73"),
        ),
        leads=LeadSummary(
            total_leads=980,
            new_leads=210,
            contacted_leads=180,
            qualified_leads=150,
            negotiation_leads=90,
            converted_leads=260,
            lost_leads=90,
            conversion_rate=26.53,
        ),
        bookings=BookingSummary(
            total_bookings=340,
            pending_bookings=40,
            confirmed_bookings=120,
            cancelled_bookings=20,
            completed_bookings=160,
            total_booking_value=Decimal("7900000.00"),
        ),
        properties=PropertySummary(
            total_properties=520,
            available_properties=300,
            reserved_properties=60,
            sold_properties=110,
            rented_properties=40,
            inactive_properties=10,
            average_property_price=Decimal("4250000.00"),
        ),
        customers=CustomerSummary(
            total_customers=1240,
            new_customers_this_month=85,
            active_customers=0,
            distinct_cities=34,
        ),
        generated_at=datetime(2026, 8, 1, 9, 30, 0),
    )


class TestAuthentication:
    async def test_missing_token_returns_401(self, app, mock_service):
        def raise_unauthenticated():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

        app.dependency_overrides[get_current_user] = raise_unauthenticated
        app.dependency_overrides[get_dashboard_service] = lambda: mock_service

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as async_client:
            response = await async_client.get("/api/v1/dashboard/summary")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        app.dependency_overrides.clear()

    async def test_invalid_token_returns_401(self, app, mock_service):
        def raise_invalid_token():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
            )

        app.dependency_overrides[get_current_user] = raise_invalid_token
        app.dependency_overrides[get_dashboard_service] = lambda: mock_service

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as async_client:
            response = await async_client.get(
                "/api/v1/dashboard/summary",
                headers={"Authorization": "Bearer invalid.token.value"},
            )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        app.dependency_overrides.clear()


class TestAuthorization:
    async def test_unauthorized_role_returns_403(self, app, mock_service):
        mock_service.get_dashboard_summary.side_effect = ValidationException(
            "You are not authorized to access the dashboard module."
        )
        app.dependency_overrides[get_current_user] = lambda: make_user(role="GUEST")
        app.dependency_overrides[get_dashboard_service] = lambda: mock_service

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as async_client:
            response = await async_client.get("/api/v1/dashboard/summary")

        assert response.status_code == status.HTTP_403_FORBIDDEN
        app.dependency_overrides.clear()

    async def test_admin_role_returns_200(self, client, mock_service):
        mock_service.get_dashboard_summary.return_value = sample_dashboard_summary()

        response = await client.get("/api/v1/dashboard/summary")

        assert response.status_code == status.HTTP_200_OK

    async def test_sales_agent_scoped_response_returns_200(self, app, mock_service):
        mock_service.get_top_agents.return_value = [
            AgentPerformance(
                agent_id=14,
                agent_name="Ananya Rao",
                total_leads_assigned=0,
                total_leads_converted=0,
                conversion_rate=0.0,
                total_bookings=21,
                total_revenue_generated=Decimal("512000.00"),
                average_deal_size=Decimal("24380.95"),
            )
        ]
        app.dependency_overrides[get_current_user] = lambda: make_user(
            role=UserRole.SALES_AGENT, user_id=14
        )
        app.dependency_overrides[get_dashboard_service] = lambda: mock_service

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as async_client:
            response = await async_client.get("/api/v1/dashboard/top-agents")

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert len(payload) == 1
        assert payload[0]["agent_id"] == 14
        app.dependency_overrides.clear()


class TestNotFound:
    async def test_empty_dataset_returns_404(self, client, mock_service):
        mock_service.get_top_properties.side_effect = NotFoundException(
            "No property records were found for the requested criteria."
        )

        response = await client.get("/api/v1/dashboard/top-properties")

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestFullDashboardEndpoint:
    async def test_get_dashboard_returns_200(self, client, mock_service):
        mock_service.get_full_dashboard.return_value = DashboardResponse(
            summary=sample_dashboard_summary(),
            top_agents=[],
            recent_activities=[],
            revenue_chart=RevenueChart(period="monthly", labels=[], values=[]),
            booking_chart=BookingChart(period="monthly", labels=[], values=[]),
            lead_chart=LeadChart(period="monthly", labels=[], new_leads=[], converted_leads=[]),
            property_chart=PropertyChart(labels=[], values=[]),
            payment_chart=PaymentChart(period="monthly", labels=[], collected=[], pending=[]),
        )

        response = await client.get("/api/v1/dashboard")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["summary"]["customers"]["total_customers"] == 1240


class TestSummaryEndpoint:
    async def test_get_summary_returns_200(self, client, mock_service):
        mock_service.get_dashboard_summary.return_value = sample_dashboard_summary()

        response = await client.get("/api/v1/dashboard/summary")

        assert response.status_code == status.HTTP_200_OK
        assert float(response.json()["revenue"]["collected_revenue"]) == 7200000.00


class TestRevenueEndpoints:
    async def test_get_revenue_returns_200(self, client, mock_service):
        mock_service.get_revenue_overview.return_value = RevenueSummary(
            total_revenue=Decimal("8400000.00"),
            collected_revenue=Decimal("7200000.00"),
            pending_revenue=Decimal("1200000.00"),
            refunded_revenue=Decimal("100000.00"),
            total_transactions=412,
            average_transaction_value=Decimal("17475.73"),
        )

        response = await client.get("/api/v1/dashboard/revenue")

        assert response.status_code == status.HTTP_200_OK

    async def test_get_monthly_revenue_returns_200(self, client, mock_service):
        mock_service.get_monthly_revenue_trend.return_value = [
            MonthlyTrend(year=2026, month=7, label="Jul 2026", total_count=38, total_amount=Decimal("645000.00"))
        ]

        response = await client.get("/api/v1/dashboard/revenue/monthly?months=6")

        assert response.status_code == status.HTTP_200_OK
        assert len(response.json()) == 1

    async def test_get_monthly_revenue_invalid_range_returns_422(self, client):
        response = await client.get("/api/v1/dashboard/revenue/monthly?months=100")

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    async def test_get_weekly_revenue_returns_200(self, client, mock_service):
        mock_service.get_weekly_revenue_trend.return_value = [
            WeeklyTrend(
                year=2026, week_number=31, week_start=date(2026, 7, 27),
                week_end=date(2026, 8, 2), total_count=0, total_amount=Decimal("158000.00"),
            )
        ]

        response = await client.get("/api/v1/dashboard/revenue/weekly?weeks=4")

        assert response.status_code == status.HTTP_200_OK

    async def test_get_daily_revenue_returns_200(self, client, mock_service):
        mock_service.get_daily_revenue_overview.return_value = {
            "date": date(2026, 8, 1),
            "today_revenue": Decimal("21000.00"),
            "yesterday_revenue": Decimal("18500.00"),
            "daily_growth_percentage": 13.51,
            "week_to_date_revenue": Decimal("158000.00"),
            "previous_week_revenue": Decimal("142000.00"),
            "weekly_growth_percentage": 11.27,
            "month_to_date_revenue": Decimal("645000.00"),
            "previous_month_revenue": Decimal("712000.00"),
            "monthly_growth_percentage": -9.41,
            "booking_conversion_rate": 82.35,
            "property_availability_rate": 57.69,
            "outstanding_amount": Decimal("1200000.00"),
        }

        response = await client.get("/api/v1/dashboard/revenue/daily")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["daily_growth_percentage"] == 13.51


class TestEntitySummaryEndpoints:
    async def test_get_customers_returns_200(self, client, mock_service):
        mock_service.get_customer_overview.return_value = CustomerSummary(
            total_customers=1240, new_customers_this_month=85, active_customers=0, distinct_cities=34
        )

        response = await client.get("/api/v1/dashboard/customers")

        assert response.status_code == status.HTTP_200_OK

    async def test_get_leads_returns_200(self, client, mock_service):
        mock_service.get_lead_overview.return_value = LeadSummary(
            total_leads=980, new_leads=210, contacted_leads=180, qualified_leads=150,
            negotiation_leads=90, converted_leads=260, lost_leads=90, conversion_rate=26.53,
        )

        response = await client.get("/api/v1/dashboard/leads")

        assert response.status_code == status.HTTP_200_OK

    async def test_get_bookings_returns_200(self, client, mock_service):
        mock_service.get_booking_overview.return_value = BookingSummary(
            total_bookings=340, pending_bookings=40, confirmed_bookings=120,
            cancelled_bookings=20, completed_bookings=160, total_booking_value=Decimal("7900000.00"),
        )

        response = await client.get("/api/v1/dashboard/bookings")

        assert response.status_code == status.HTTP_200_OK

    async def test_get_payments_returns_200(self, client, mock_service):
        mock_service.get_payment_overview.return_value = RevenueSummary(
            total_revenue=Decimal("8400000.00"), collected_revenue=Decimal("7200000.00"),
            pending_revenue=Decimal("1200000.00"), refunded_revenue=Decimal("100000.00"),
            total_transactions=412, average_transaction_value=Decimal("17475.73"),
        )

        response = await client.get("/api/v1/dashboard/payments")

        assert response.status_code == status.HTTP_200_OK

    async def test_get_properties_returns_200(self, client, mock_service):
        mock_service.get_property_overview.return_value = PropertySummary(
            total_properties=520, available_properties=300, reserved_properties=60,
            sold_properties=110, rented_properties=40, inactive_properties=10,
            average_property_price=Decimal("4250000.00"),
        )

        response = await client.get("/api/v1/dashboard/properties")

        assert response.status_code == status.HTTP_200_OK


class TestAgentAndActivityEndpoints:
    async def test_get_agents_returns_200(self, client, mock_service):
        mock_service.get_agents_performance.return_value = [
            AgentPerformance(
                agent_id=14, agent_name="Ananya Rao", total_leads_assigned=62,
                total_leads_converted=19, conversion_rate=30.65, total_bookings=21,
                total_revenue_generated=Decimal("512000.00"), average_deal_size=Decimal("24380.95"),
            )
        ]

        response = await client.get("/api/v1/dashboard/agents?limit=20&offset=0")

        assert response.status_code == status.HTTP_200_OK

    async def test_get_recent_activities_returns_200(self, client, mock_service):
        mock_service.get_recent_activities.return_value = [
            RecentActivity(
                activity_type="payment_received", reference_id=4021, title="Payment received",
                description="Payment status: completed", actor_name=None,
                amount=Decimal("45000.00"), occurred_at=datetime(2026, 8, 1, 8, 12, 0),
            )
        ]

        response = await client.get("/api/v1/dashboard/recent-activities?limit=15")

        assert response.status_code == status.HTTP_200_OK

    async def test_get_top_agents_returns_200(self, client, mock_service):
        mock_service.get_top_agents.return_value = []

        response = await client.get("/api/v1/dashboard/top-agents?limit=5")

        assert response.status_code == status.HTTP_200_OK

    async def test_get_top_properties_returns_200(self, client, mock_service):
        mock_service.get_top_properties.return_value = [
            {
                "property_id": 88, "title": "Lakeview Residency, Tower B",
                "city": "Chennai", "total_bookings": 12, "total_revenue": Decimal("610000.00"),
            }
        ]

        response = await client.get("/api/v1/dashboard/top-properties?limit=5")

        assert response.status_code == status.HTTP_200_OK

    async def test_get_top_cities_returns_200(self, client, mock_service):
        mock_service.get_top_cities.return_value = [
            {"city": "Chennai", "total_properties": 96, "total_bookings": 58}
        ]

        response = await client.get("/api/v1/dashboard/top-cities?limit=5")

        assert response.status_code == status.HTTP_200_OK

    async def test_get_upcoming_followups_returns_200(self, client, mock_service):
        mock_service.get_upcoming_followups.return_value = [
            {
                "lead_id": 3320, "customer_id": 1188, "status": "negotiation",
                "follow_up_date": date(2026, 8, 3), "agent_id": 14,
            }
        ]

        response = await client.get("/api/v1/dashboard/upcoming-followups?limit=10&offset=0")

        assert response.status_code == status.HTTP_200_OK


class TestPaginationValidation:
    async def test_agents_endpoint_rejects_limit_above_maximum(self, client):
        response = await client.get("/api/v1/dashboard/agents?limit=500&offset=0")

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    async def test_upcoming_followups_rejects_negative_offset(self, client):
        response = await client.get("/api/v1/dashboard/upcoming-followups?limit=10&offset=-5")

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY