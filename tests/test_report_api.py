"""
test_report_api.py

API-layer test suite for the Reports Module.
Mirrors the testing architecture established for:
Customer / Lead / Property / Booking / Payment / Dashboard endpoints.

Scope:
- Authentication (401)
- Authorization (403)
- Not found (404)
- Validation errors (422)
- Pagination / Filtering / Sorting
- Revenue endpoints
- Booking endpoints
- Payment endpoints
- Lead endpoints
- Customer endpoints
- Property endpoints
- Dashboard endpoints
- Export endpoints
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.dependencies import get_current_user, get_db
from app.services.report_service import ReportService
from app.api.v1.report import get_report_service


pytestmark = pytest.mark.asyncio

BASE_URL = "/api/v1/reports"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def _make_user(role: str = "admin"):
    return MagicMock(id=uuid.uuid4(), role=role, is_active=True, email="user@example.com")


@pytest.fixture
def mock_report_service():
    return AsyncMock(spec=ReportService)


@pytest_asyncio.fixture
async def authorized_client(mock_report_service):
    app.dependency_overrides[get_current_user] = lambda: _make_user(role="admin")
    app.dependency_overrides[get_report_service] = lambda: mock_report_service
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def agent_client(mock_report_service):
    app.dependency_overrides[get_current_user] = lambda: _make_user(role="agent")
    app.dependency_overrides[get_report_service] = lambda: mock_report_service
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def unauthenticated_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


def _valid_range_params():
    return {
        "start_date": str(date.today() - timedelta(days=30)),
        "end_date": str(date.today() - timedelta(days=1)),
    }


# --------------------------------------------------------------------------
# Authentication & Authorization
# --------------------------------------------------------------------------

class TestAuthenticationAndAuthorization:
    async def test_revenue_endpoint_requires_authentication(self, unauthenticated_client):
        response = await unauthenticated_client.get(
            f"{BASE_URL}/revenue", params=_valid_range_params()
        )
        assert response.status_code == 401

    async def test_dashboard_endpoint_requires_authentication(self, unauthenticated_client):
        response = await unauthenticated_client.get(f"{BASE_URL}/dashboard")
        assert response.status_code == 401

    async def test_agent_forbidden_from_revenue_report(self, agent_client, mock_report_service):
        response = await agent_client.get(f"{BASE_URL}/revenue", params=_valid_range_params())
        assert response.status_code == 403

    async def test_agent_forbidden_from_export(self, agent_client):
        response = await agent_client.post(
            f"{BASE_URL}/export",
            json={
                "report_type": "revenue",
                "export_format": "pdf",
                **_valid_range_params(),
            },
        )
        assert response.status_code == 403

    async def test_admin_allowed_dashboard_access(self, authorized_client, mock_report_service):
        mock_report_service.get_dashboard_summary.return_value = MagicMock(
            total_revenue=Decimal("100000"), total_bookings=10
        )
        response = await authorized_client.get(f"{BASE_URL}/dashboard")
        assert response.status_code == 200


# --------------------------------------------------------------------------
# Not Found / Validation
# --------------------------------------------------------------------------

class TestNotFoundAndValidation:
    async def test_customer_analytics_404_when_missing(self, authorized_client, mock_report_service):
        from app.core.exceptions import NotFoundError

        mock_report_service.get_customer_analytics.side_effect = NotFoundError("Customer not found")
        response = await authorized_client.get(
            f"{BASE_URL}/customers/{uuid.uuid4()}/analytics"
        )
        assert response.status_code == 404

    async def test_revenue_endpoint_422_missing_dates(self, authorized_client):
        response = await authorized_client.get(f"{BASE_URL}/revenue")
        assert response.status_code == 422

    async def test_revenue_endpoint_422_invalid_date_format(self, authorized_client):
        response = await authorized_client.get(
            f"{BASE_URL}/revenue",
            params={"start_date": "not-a-date", "end_date": "also-not-a-date"},
        )
        assert response.status_code == 422

    async def test_booking_report_422_invalid_pagination(self, authorized_client):
        response = await authorized_client.get(
            f"{BASE_URL}/bookings",
            params={**_valid_range_params(), "page": 0, "page_size": -5},
        )
        assert response.status_code == 422

    async def test_export_422_invalid_format(self, authorized_client):
        response = await authorized_client.post(
            f"{BASE_URL}/export",
            json={
                "report_type": "revenue",
                "export_format": "doc",
                **_valid_range_params(),
            },
        )
        assert response.status_code == 422


# --------------------------------------------------------------------------
# Pagination / Filtering / Sorting
# --------------------------------------------------------------------------

class TestPaginationFilteringSorting:
    async def test_booking_report_pagination_params_passed(self, authorized_client, mock_report_service):
        mock_report_service.get_booking_report.return_value = MagicMock(items=[], total=0)
        response = await authorized_client.get(
            f"{BASE_URL}/bookings",
            params={**_valid_range_params(), "page": 2, "page_size": 20},
        )
        assert response.status_code == 200
        _, kwargs = mock_report_service.get_booking_report.call_args
        assert kwargs.get("page") == 2
        assert kwargs.get("page_size") == 20

    async def test_payment_report_filter_by_status(self, authorized_client, mock_report_service):
        mock_report_service.get_payment_report.return_value = MagicMock(items=[], total=0)
        response = await authorized_client.get(
            f"{BASE_URL}/payments",
            params={**_valid_range_params(), "status": "completed"},
        )
        assert response.status_code == 200
        _, kwargs = mock_report_service.get_payment_report.call_args
        assert kwargs.get("status") == "completed"

    async def test_booking_report_sorting(self, authorized_client, mock_report_service):
        mock_report_service.get_booking_report.return_value = MagicMock(items=[], total=0)
        response = await authorized_client.get(
            f"{BASE_URL}/bookings",
            params={**_valid_range_params(), "sort_by": "amount", "sort_order": "desc"},
        )
        assert response.status_code == 200
        _, kwargs = mock_report_service.get_booking_report.call_args
        assert kwargs.get("sort_by") == "amount"
        assert kwargs.get("sort_order") == "desc"

    async def test_customer_report_search(self, authorized_client, mock_report_service):
        mock_report_service.get_customer_report.return_value = MagicMock(items=[], total=0)
        response = await authorized_client.get(
            f"{BASE_URL}/customers", params={"search": "John"}
        )
        assert response.status_code == 200
        _, kwargs = mock_report_service.get_customer_report.call_args
        assert kwargs.get("search") == "John"


# --------------------------------------------------------------------------
# Revenue Endpoints
# --------------------------------------------------------------------------

class TestRevenueEndpoints:
    async def test_get_revenue_report_success(self, authorized_client, mock_report_service):
        mock_report_service.get_revenue_summary.return_value = MagicMock(
            total_revenue=Decimal("500000"), average_revenue=Decimal("50000")
        )
        response = await authorized_client.get(f"{BASE_URL}/revenue", params=_valid_range_params())
        assert response.status_code == 200
        body = response.json()
        assert "total_revenue" in body

    async def test_get_revenue_growth_success(self, authorized_client, mock_report_service):
        mock_report_service.get_revenue_growth.return_value = MagicMock(growth_percentage=15.5)
        response = await authorized_client.get(
            f"{BASE_URL}/revenue/growth",
            params={
                "current_start": str(date.today() - timedelta(days=30)),
                "current_end": str(date.today() - timedelta(days=1)),
                "previous_start": str(date.today() - timedelta(days=60)),
                "previous_end": str(date.today() - timedelta(days=31)),
            },
        )
        assert response.status_code == 200


# --------------------------------------------------------------------------
# Booking Endpoints
# --------------------------------------------------------------------------

class TestBookingEndpoints:
    async def test_get_booking_report_success(self, authorized_client, mock_report_service):
        mock_report_service.get_booking_report.return_value = MagicMock(items=[], total=0)
        response = await authorized_client.get(f"{BASE_URL}/bookings", params=_valid_range_params())
        assert response.status_code == 200

    async def test_get_booking_statistics_success(self, authorized_client, mock_report_service):
        mock_report_service.get_booking_statistics.return_value = MagicMock(
            total_bookings=10, confirmed_bookings=8, cancellation_rate=20.0
        )
        response = await authorized_client.get(
            f"{BASE_URL}/bookings/statistics", params=_valid_range_params()
        )
        assert response.status_code == 200


# --------------------------------------------------------------------------
# Payment Endpoints
# --------------------------------------------------------------------------

class TestPaymentEndpoints:
    async def test_get_payment_report_success(self, authorized_client, mock_report_service):
        mock_report_service.get_payment_report.return_value = MagicMock(items=[], total=0)
        response = await authorized_client.get(f"{BASE_URL}/payments", params=_valid_range_params())
        assert response.status_code == 200

    async def test_get_payment_analytics_success(self, authorized_client, mock_report_service):
        mock_report_service.get_payment_analytics.return_value = MagicMock(success_rate=95.0)
        response = await authorized_client.get(
            f"{BASE_URL}/payments/analytics", params=_valid_range_params()
        )
        assert response.status_code == 200


# --------------------------------------------------------------------------
# Lead Endpoints
# --------------------------------------------------------------------------

class TestLeadEndpoints:
    async def test_get_lead_report_success(self, authorized_client, mock_report_service):
        mock_report_service.get_lead_report.return_value = MagicMock(items=[], total=0)
        response = await authorized_client.get(f"{BASE_URL}/leads", params=_valid_range_params())
        assert response.status_code == 200

    async def test_get_lead_conversion_analytics_success(self, authorized_client, mock_report_service):
        mock_report_service.get_lead_conversion_analytics.return_value = MagicMock(
            conversion_rate=25.0
        )
        response = await authorized_client.get(
            f"{BASE_URL}/leads/conversion", params=_valid_range_params()
        )
        assert response.status_code == 200


# --------------------------------------------------------------------------
# Customer Endpoints
# --------------------------------------------------------------------------

class TestCustomerEndpoints:
    async def test_get_customer_report_success(self, authorized_client, mock_report_service):
        mock_report_service.get_customer_report.return_value = MagicMock(items=[], total=0)
        response = await authorized_client.get(f"{BASE_URL}/customers")
        assert response.status_code == 200

    async def test_get_customer_analytics_success(self, authorized_client, mock_report_service):
        customer_id = uuid.uuid4()
        mock_report_service.get_customer_analytics.return_value = MagicMock(
            customer_id=customer_id, total_bookings=3
        )
        response = await authorized_client.get(f"{BASE_URL}/customers/{customer_id}/analytics")
        assert response.status_code == 200


# --------------------------------------------------------------------------
# Property Endpoints
# --------------------------------------------------------------------------

class TestPropertyEndpoints:
    async def test_get_property_report_success(self, authorized_client, mock_report_service):
        mock_report_service.get_property_report.return_value = MagicMock(items=[], total=0)
        response = await authorized_client.get(f"{BASE_URL}/properties")
        assert response.status_code == 200

    async def test_get_top_properties_success(self, authorized_client, mock_report_service):
        mock_report_service.get_top_properties.return_value = []
        response = await authorized_client.get(f"{BASE_URL}/properties/top", params={"limit": 5})
        assert response.status_code == 200

    async def test_get_top_properties_invalid_limit_422(self, authorized_client, mock_report_service):
        from app.core.exceptions import ValidationError

        mock_report_service.get_top_properties.side_effect = ValidationError("Invalid limit")
        response = await authorized_client.get(f"{BASE_URL}/properties/top", params={"limit": 0})
        assert response.status_code in (400, 422)


# --------------------------------------------------------------------------
# Dashboard Endpoints
# --------------------------------------------------------------------------

class TestDashboardEndpoints:
    async def test_get_dashboard_summary_success(self, authorized_client, mock_report_service):
        mock_report_service.get_dashboard_summary.return_value = MagicMock(
            total_revenue=Decimal("1000000"), total_bookings=50
        )
        response = await authorized_client.get(f"{BASE_URL}/dashboard")
        assert response.status_code == 200

    async def test_get_top_agents_success(self, authorized_client, mock_report_service):
        mock_report_service.get_top_agents.return_value = []
        response = await authorized_client.get(f"{BASE_URL}/dashboard/top-agents", params={"limit": 5})
        assert response.status_code == 200


# --------------------------------------------------------------------------
# Export Endpoints
# --------------------------------------------------------------------------

class TestExportEndpoints:
    async def test_export_revenue_pdf_success(self, authorized_client, mock_report_service):
        mock_report_service.export_report.return_value = b"%PDF-1.4 mock"
        response = await authorized_client.post(
            f"{BASE_URL}/export",
            json={
                "report_type": "revenue",
                "export_format": "pdf",
                **_valid_range_params(),
            },
        )
        assert response.status_code == 200

    async def test_export_bookings_excel_success(self, authorized_client, mock_report_service):
        mock_report_service.export_report.return_value = b"mock-xlsx-bytes"
        response = await authorized_client.post(
            f"{BASE_URL}/export",
            json={
                "report_type": "bookings",
                "export_format": "excel",
                **_valid_range_params(),
            },
        )
        assert response.status_code == 200

    async def test_export_payments_csv_success(self, authorized_client, mock_report_service):
        mock_report_service.export_report.return_value = "col1,col2\n1,2"
        response = await authorized_client.post(
            f"{BASE_URL}/export",
            json={
                "report_type": "payments",
                "export_format": "csv",
                **_valid_range_params(),
            },
        )
        assert response.status_code == 200

    async def test_export_requires_authentication(self, unauthenticated_client):
        response = await unauthenticated_client.post(
            f"{BASE_URL}/export",
            json={
                "report_type": "revenue",
                "export_format": "pdf",
                **_valid_range_params(),
            },
        )
        assert response.status_code == 401

    async def test_export_invalid_report_type_422(self, authorized_client):
        response = await authorized_client.post(
            f"{BASE_URL}/export",
            json={
                "report_type": "unknown",
                "export_format": "csv",
                **_valid_range_params(),
            },
        )
        assert response.status_code == 422