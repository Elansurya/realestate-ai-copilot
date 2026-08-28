# backend/tests/test_payment_repository.py

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payment import Payment, PaymentStatus, PaymentMode, PaymentType
from app.repositories.payment_repository import PaymentRepository
from app.schemas.payment import PaymentCreate, PaymentUpdate, PaymentSearchFilter


pytestmark = pytest.mark.asyncio


@pytest.fixture
def payment_repo(db_session: AsyncSession) -> PaymentRepository:
    return PaymentRepository(db_session)


@pytest.fixture
def base_payment_payload(booking_fixture, customer_fixture, property_fixture, user_fixture):
    return PaymentCreate(
        booking_id=booking_fixture.id,
        customer_id=customer_fixture.id,
        property_id=property_fixture.id,
        received_by=user_fixture.id,
        payment_date=date.today(),
        payment_amount=Decimal("100000.00"),
        payment_mode=PaymentMode.BANK_TRANSFER,
        transaction_reference=f"TXN-{uuid.uuid4().hex[:8]}",
        payment_type=PaymentType.TOKEN,
        bank_name="Test Bank",
    )


class TestPaymentRepositoryCRUD:

    async def test_create_payment(self, payment_repo, base_payment_payload):
        payment = await payment_repo.create(base_payment_payload, "PAY-2026-000001")
        assert payment.id is not None
        assert payment.payment_number == "PAY-2026-000001"
        assert payment.payment_status == PaymentStatus.PENDING
        assert payment.is_active is True

    async def test_get_by_id_returns_active_payment(self, payment_repo, base_payment_payload):
        payment = await payment_repo.create(base_payment_payload, "PAY-2026-000002")
        fetched = await payment_repo.get_by_id(payment.id)
        assert fetched is not None
        assert fetched.id == payment.id

    async def test_get_by_id_excludes_inactive(self, payment_repo, base_payment_payload):
        payment = await payment_repo.create(base_payment_payload, "PAY-2026-000003")
        await payment_repo.soft_delete(payment.id)
        fetched = await payment_repo.get_by_id(payment.id)
        assert fetched is None

    async def test_get_by_id_any_status(self, payment_repo, base_payment_payload):
        payment = await payment_repo.create(base_payment_payload, "PAY-2026-000004")
        await payment_repo.soft_delete(payment.id)
        fetched = await payment_repo.get_by_id_any_status(payment.id)
        assert fetched is not None
        assert fetched.is_active is False

    async def test_get_by_payment_number(self, payment_repo, base_payment_payload):
        await payment_repo.create(base_payment_payload, "PAY-2026-000005")
        fetched = await payment_repo.get_by_payment_number("PAY-2026-000005")
        assert fetched is not None
        assert fetched.payment_number == "PAY-2026-000005"

    async def test_get_last_payment_number(self, payment_repo, base_payment_payload):
        year = datetime.utcnow().year
        await payment_repo.create(base_payment_payload, f"PAY-{year}-000010")
        last_number = await payment_repo.get_last_payment_number(year)
        assert last_number == f"PAY-{year}-000010"

    async def test_update_payment(self, payment_repo, base_payment_payload):
        payment = await payment_repo.create(base_payment_payload, "PAY-2026-000006")
        update = PaymentUpdate(remarks="Updated remark")
        updated = await payment_repo.update(payment.id, update)
        assert updated.remarks == "Updated remark"

    async def test_update_status(self, payment_repo, base_payment_payload):
        payment = await payment_repo.create(base_payment_payload, "PAY-2026-000007")
        updated = await payment_repo.update_status(
            payment.id, PaymentStatus.SUCCESS, "Confirmed"
        )
        assert updated.payment_status == PaymentStatus.SUCCESS
        assert updated.remarks == "Confirmed"

    async def test_soft_delete(self, payment_repo, base_payment_payload):
        payment = await payment_repo.create(base_payment_payload, "PAY-2026-000008")
        result = await payment_repo.soft_delete(payment.id)
        assert result is True
        fetched = await payment_repo.get_by_id(payment.id)
        assert fetched is None

    async def test_restore(self, payment_repo, base_payment_payload):
        payment = await payment_repo.create(base_payment_payload, "PAY-2026-000009")
        await payment_repo.soft_delete(payment.id)
        restored = await payment_repo.restore(payment.id)
        assert restored is True
        fetched = await payment_repo.get_by_id(payment.id)
        assert fetched is not None


class TestPaymentRepositorySearch:

    async def test_pagination(self, payment_repo, base_payment_payload):
        for i in range(15):
            base_payment_payload.transaction_reference = f"TXN-PAG-{i}"
            await payment_repo.create(base_payment_payload, f"PAY-2026-1{i:05d}")

        filters = PaymentSearchFilter(page=1, page_size=10)
        items, total = await payment_repo.search(filters)
        assert total >= 15
        assert len(items) == 10

    async def test_filter_by_status(self, payment_repo, base_payment_payload):
        payment = await payment_repo.create(base_payment_payload, "PAY-2026-200001")
        await payment_repo.update_status(payment.id, PaymentStatus.SUCCESS)

        filters = PaymentSearchFilter(payment_status=PaymentStatus.SUCCESS)
        items, total = await payment_repo.search(filters)
        assert total >= 1
        assert all(i.payment_status == PaymentStatus.SUCCESS for i in items)

    async def test_filter_by_amount_range(self, payment_repo, base_payment_payload):
        base_payment_payload.payment_amount = Decimal("50000.00")
        base_payment_payload.transaction_reference = "TXN-RANGE-1"
        await payment_repo.create(base_payment_payload, "PAY-2026-300001")

        filters = PaymentSearchFilter(min_amount=Decimal("10000"), max_amount=Decimal("60000"))
        items, total = await payment_repo.search(filters)
        assert total >= 1

    async def test_search_by_text(self, payment_repo, base_payment_payload):
        base_payment_payload.transaction_reference = "UNIQUE-SEARCH-TERM"
        await payment_repo.create(base_payment_payload, "PAY-2026-400001")

        filters = PaymentSearchFilter(search="UNIQUE-SEARCH-TERM")
        items, total = await payment_repo.search(filters)
        assert total == 1
        assert items[0].transaction_reference == "UNIQUE-SEARCH-TERM"

    async def test_sorting_asc_desc(self, payment_repo, base_payment_payload):
        base_payment_payload.payment_amount = Decimal("10.00")
        await payment_repo.create(base_payment_payload, "PAY-2026-500001")
        base_payment_payload.payment_amount = Decimal("999999.00")
        await payment_repo.create(base_payment_payload, "PAY-2026-500002")

        filters = PaymentSearchFilter(sort_by="payment_amount", sort_order="asc", page_size=100)
        items, _ = await payment_repo.search(filters)
        amounts = [i.payment_amount for i in items]
        assert amounts == sorted(amounts)

    async def test_is_active_filter_default_true(self, payment_repo, base_payment_payload):
        payment = await payment_repo.create(base_payment_payload, "PAY-2026-600001")
        await payment_repo.soft_delete(payment.id)

        filters = PaymentSearchFilter(is_active=True)
        items, _ = await payment_repo.search(filters)
        assert all(i.is_active for i in items)


class TestPaymentRepositoryAggregation:

    async def test_get_by_booking_id(self, payment_repo, base_payment_payload, booking_fixture):
        await payment_repo.create(base_payment_payload, "PAY-2026-700001")
        results = await payment_repo.get_by_booking_id(booking_fixture.id)
        assert len(results) >= 1

    async def test_get_by_customer_id(self, payment_repo, base_payment_payload, customer_fixture):
        await payment_repo.create(base_payment_payload, "PAY-2026-700002")
        results = await payment_repo.get_by_customer_id(customer_fixture.id)
        assert len(results) >= 1

    async def test_get_by_property_id(self, payment_repo, base_payment_payload, property_fixture):
        await payment_repo.create(base_payment_payload, "PAY-2026-700003")
        results = await payment_repo.get_by_property_id(property_fixture.id)
        assert len(results) >= 1

    async def test_get_total_paid_for_booking(
        self, payment_repo, base_payment_payload, booking_fixture
    ):
        payment = await payment_repo.create(base_payment_payload, "PAY-2026-800001")
        await payment_repo.update_status(payment.id, PaymentStatus.SUCCESS)
        total = await payment_repo.get_total_paid_for_booking(booking_fixture.id)
        assert total >= base_payment_payload.payment_amount

    async def test_get_today_payments(self, payment_repo, base_payment_payload):
        await payment_repo.create(base_payment_payload, "PAY-2026-900001")
        results = await payment_repo.get_today_payments()
        assert all(r.payment_date == date.today() for r in results)

    async def test_get_today_summary(self, payment_repo, base_payment_payload):
        payment = await payment_repo.create(base_payment_payload, "PAY-2026-900002")
        await payment_repo.update_status(payment.id, PaymentStatus.SUCCESS)
        count, total = await payment_repo.get_today_summary()
        assert count >= 1
        assert total >= base_payment_payload.payment_amount

    async def test_get_monthly_revenue(self, payment_repo, base_payment_payload):
        payment = await payment_repo.create(base_payment_payload, "PAY-2026-900003")
        await payment_repo.update_status(payment.id, PaymentStatus.SUCCESS)
        today = date.today()
        revenue = await payment_repo.get_monthly_revenue(today.year, today.month)
        assert revenue >= base_payment_payload.payment_amount

    async def test_get_dashboard_summary(self, payment_repo, base_payment_payload):
        payment = await payment_repo.create(base_payment_payload, "PAY-2026-900004")
        await payment_repo.update_status(payment.id, PaymentStatus.SUCCESS)
        summary = await payment_repo.get_dashboard_summary()
        assert summary["total_payments_count"] >= 1
        assert summary["success_amount"] >= base_payment_payload.payment_amount


class TestPaymentRepositoryConstraints:

    async def test_unique_payment_number_constraint(self, payment_repo, base_payment_payload):
        await payment_repo.create(base_payment_payload, "PAY-2026-UNIQUE-001")
        base_payment_payload.transaction_reference = "TXN-DIFFERENT"
        with pytest.raises(Exception):
            await payment_repo.create(base_payment_payload, "PAY-2026-UNIQUE-001")

    async def test_check_constraint_amount_positive(self, payment_repo, base_payment_payload):
        base_payment_payload.payment_amount = Decimal("-1.00")
        with pytest.raises(Exception):
            await payment_repo.create(base_payment_payload, "PAY-2026-NEG-001")