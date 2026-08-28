# backend/tests/test_payment_api.py

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient


pytestmark = pytest.mark.asyncio


BASE_URL = "/api/v1/payments"


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestPaymentAuthentication:

    async def test_create_payment_without_token_returns_401(
        self, async_client: AsyncClient, payment_payload
    ):
        response = await async_client.post(BASE_URL, json=payment_payload)
        assert response.status_code == 401

    async def test_list_payments_without_token_returns_401(self, async_client: AsyncClient):
        response = await async_client.get(BASE_URL)
        assert response.status_code == 401

    async def test_invalid_token_returns_401(self, async_client: AsyncClient):
        response = await async_client.get(BASE_URL, headers=auth_headers("invalid.token.value"))
        assert response.status_code == 401


class TestPaymentAuthorization:

    async def test_sales_agent_cannot_delete_payment(
        self, async_client: AsyncClient, sales_agent_token, payment_id_fixture
    ):
        response = await async_client.delete(
            f"{BASE_URL}/{payment_id_fixture}", headers=auth_headers(sales_agent_token)
        )
        assert response.status_code == 403

    async def test_sales_agent_cannot_update_status(
        self, async_client: AsyncClient, sales_agent_token, payment_id_fixture
    ):
        response = await async_client.patch(
            f"{BASE_URL}/{payment_id_fixture}/status",
            json={"payment_status": "SUCCESS"},
            headers=auth_headers(sales_agent_token),
        )
        assert response.status_code == 403

    async def test_admin_can_delete_payment(
        self, async_client: AsyncClient, admin_token, pending_payment_id
    ):
        response = await async_client.delete(
            f"{BASE_URL}/{pending_payment_id}", headers=auth_headers(admin_token)
        )
        assert response.status_code == 204

    async def test_sales_agent_cannot_view_dashboard(
        self, async_client: AsyncClient, sales_agent_token
    ):
        response = await async_client.get(
            f"{BASE_URL}/dashboard/summary", headers=auth_headers(sales_agent_token)
        )
        assert response.status_code == 403


class TestCreatePaymentAPI:

    async def test_create_payment_success_201(
        self, async_client: AsyncClient, admin_token, payment_payload
    ):
        response = await async_client.post(
            BASE_URL, json=payment_payload, headers=auth_headers(admin_token)
        )
        assert response.status_code == 201
        body = response.json()
        assert body["payment_status"] == "PENDING"
        assert body["payment_number"].startswith("PAY-")
        assert body["receipt_number"].startswith("RCPT-")

    async def test_create_payment_invalid_amount_422(
        self, async_client: AsyncClient, admin_token, payment_payload
    ):
        payload = {**payment_payload, "payment_amount": -100}
        response = await async_client.post(
            BASE_URL, json=payload, headers=auth_headers(admin_token)
        )
        assert response.status_code == 422

    async def test_create_payment_missing_booking_404(
        self, async_client: AsyncClient, admin_token, payment_payload
    ):
        payload = {**payment_payload, "booking_id": str(uuid.uuid4())}
        response = await async_client.post(
            BASE_URL, json=payload, headers=auth_headers(admin_token)
        )
        assert response.status_code == 404

    async def test_create_payment_inactive_booking_400(
        self, async_client: AsyncClient, admin_token, inactive_booking_payload
    ):
        response = await async_client.post(
            BASE_URL, json=inactive_booking_payload, headers=auth_headers(admin_token)
        )
        assert response.status_code == 400

    async def test_create_payment_cheque_without_number_422(
        self, async_client: AsyncClient, admin_token, payment_payload
    ):
        payload = {**payment_payload, "payment_mode": "CHEQUE", "cheque_number": None}
        response = await async_client.post(
            BASE_URL, json=payload, headers=auth_headers(admin_token)
        )
        assert response.status_code == 422

    async def test_create_payment_duplicate_transaction_reference_409(
        self, async_client: AsyncClient, admin_token, success_payment_payload
    ):
        first = await async_client.post(
            BASE_URL, json=success_payment_payload, headers=auth_headers(admin_token)
        )
        assert first.status_code == 201

        second_payload = {**success_payment_payload}
        response = await async_client.post(
            BASE_URL, json=second_payload, headers=auth_headers(admin_token)
        )
        assert response.status_code == 409


class TestListPaymentsAPI:

    async def test_list_payments_200(self, async_client: AsyncClient, admin_token):
        response = await async_client.get(
            BASE_URL, headers=auth_headers(admin_token)
        )
        assert response.status_code == 200
        body = response.json()
        assert "items" in body
        assert "total" in body

    async def test_list_payments_pagination(self, async_client: AsyncClient, admin_token):
        response = await async_client.get(
            f"{BASE_URL}?page=1&page_size=5", headers=auth_headers(admin_token)
        )
        assert response.status_code == 200
        body = response.json()
        assert body["page"] == 1
        assert body["page_size"] == 5

    async def test_list_payments_filter_by_status(self, async_client: AsyncClient, admin_token):
        response = await async_client.get(
            f"{BASE_URL}?payment_status=PENDING", headers=auth_headers(admin_token)
        )
        assert response.status_code == 200

    async def test_list_payments_invalid_sort_order_422(
        self, async_client: AsyncClient, admin_token
    ):
        response = await async_client.get(
            f"{BASE_URL}?sort_order=invalid", headers=auth_headers(admin_token)
        )
        assert response.status_code == 422


class TestGetPaymentAPI:

    async def test_get_payment_200(
        self, async_client: AsyncClient, admin_token, pending_payment_id
    ):
        response = await async_client.get(
            f"{BASE_URL}/{pending_payment_id}", headers=auth_headers(admin_token)
        )
        assert response.status_code == 200

    async def test_get_payment_404(self, async_client: AsyncClient, admin_token):
        response = await async_client.get(
            f"{BASE_URL}/{uuid.uuid4()}", headers=auth_headers(admin_token)
        )
        assert response.status_code == 404


class TestUpdatePaymentAPI:

    async def test_update_payment_200(
        self, async_client: AsyncClient, admin_token, pending_payment_id
    ):
        response = await async_client.put(
            f"{BASE_URL}/{pending_payment_id}",
            json={"remarks": "Updated via test"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        assert response.json()["remarks"] == "Updated via test"

    async def test_update_payment_404(self, async_client: AsyncClient, admin_token):
        response = await async_client.put(
            f"{BASE_URL}/{uuid.uuid4()}",
            json={"remarks": "No such payment"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 404

    async def test_update_success_payment_restricted_field_400(
        self, async_client: AsyncClient, admin_token, success_payment_id
    ):
        response = await async_client.put(
            f"{BASE_URL}/{success_payment_id}",
            json={"payment_amount": 999999.00},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400


class TestPaymentStatusUpdateAPI:

    async def test_status_update_pending_to_success_200(
        self, async_client: AsyncClient, admin_token, pending_payment_id
    ):
        response = await async_client.patch(
            f"{BASE_URL}/{pending_payment_id}/status",
            json={"payment_status": "SUCCESS", "remarks": "Bank confirmed"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        assert response.json()["payment_status"] == "SUCCESS"

    async def test_status_update_failed_to_success_400(
        self, async_client: AsyncClient, admin_token, failed_payment_id
    ):
        response = await async_client.patch(
            f"{BASE_URL}/{failed_payment_id}/status",
            json={"payment_status": "SUCCESS"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400

    async def test_status_update_invalid_enum_422(
        self, async_client: AsyncClient, admin_token, pending_payment_id
    ):
        response = await async_client.patch(
            f"{BASE_URL}/{pending_payment_id}/status",
            json={"payment_status": "NOT_A_STATUS"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 422

    async def test_status_update_404(self, async_client: AsyncClient, admin_token):
        response = await async_client.patch(
            f"{BASE_URL}/{uuid.uuid4()}/status",
            json={"payment_status": "SUCCESS"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 404


class TestDeletePaymentAPI:

    async def test_delete_payment_204(
        self, async_client: AsyncClient, admin_token, pending_payment_id
    ):
        response = await async_client.delete(
            f"{BASE_URL}/{pending_payment_id}", headers=auth_headers(admin_token)
        )
        assert response.status_code == 204

    async def test_delete_success_payment_409(
        self, async_client: AsyncClient, admin_token, success_payment_id
    ):
        response = await async_client.delete(
            f"{BASE_URL}/{success_payment_id}", headers=auth_headers(admin_token)
        )
        assert response.status_code == 409

    async def test_delete_nonexistent_payment_404(
        self, async_client: AsyncClient, admin_token
    ):
        response = await async_client.delete(
            f"{BASE_URL}/{uuid.uuid4()}", headers=auth_headers(admin_token)
        )
        assert response.status_code == 404


class TestDashboardAndRevenueAPI:

    async def test_dashboard_summary_200(self, async_client: AsyncClient, admin_token):
        response = await async_client.get(
            f"{BASE_URL}/dashboard/summary", headers=auth_headers(admin_token)
        )
        assert response.status_code == 200
        body = response.json()
        assert "total_revenue" in body
        assert "monthly_revenue" in body

    async def test_today_payments_200(self, async_client: AsyncClient, admin_token):
        response = await async_client.get(
            f"{BASE_URL}/today", headers=auth_headers(admin_token)
        )
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    async def test_monthly_revenue_200(self, async_client: AsyncClient, admin_token):
        response = await async_client.get(
            f"{BASE_URL}/monthly-revenue?year=2026&month=8",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200

    async def test_monthly_revenue_invalid_month_422(
        self, async_client: AsyncClient, admin_token
    ):
        response = await async_client.get(
            f"{BASE_URL}/monthly-revenue?year=2026&month=13",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 422


class TestSwaggerSchemaExposure:

    async def test_openapi_contains_payment_paths(self, async_client: AsyncClient):
        response = await async_client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert "/api/v1/payments" in schema["paths"]
        assert "/api/v1/payments/{payment_id}" in schema["paths"]
        assert "/api/v1/payments/{payment_id}/status" in schema["paths"]
        assert "/api/v1/payments/dashboard/summary" in schema["paths"]
        assert "/api/v1/payments/today" in schema["paths"]
        assert "/api/v1/payments/monthly-revenue" in schema["paths"]