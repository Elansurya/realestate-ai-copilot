# backend/tests/test_payment_service.py

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import (
    NotFoundException,
    ConflictException,
    BadRequestException,
    ValidationException,
)
from app.models.payment import PaymentStatus, PaymentMode, PaymentType
from app.models.booking import BookingStatus, BookingPaymentStatus
from app.schemas.payment import PaymentCreate, PaymentUpdate, PaymentStatusUpdate
from app.services.payment_service import PaymentService


pytestmark = pytest.mark.asyncio


def make_booking(
    total_amount=Decimal("1000000.00"),
    paid_amount=Decimal("0.00"),
    is_active=True,
    payment_status=BookingPaymentStatus.PENDING,
    status=BookingStatus.CONFIRMED,
):
    booking = MagicMock()
    booking.id = uuid.uuid4()
    booking.total_amount = total_amount
    booking.paid_amount = paid_amount
    booking.is_active = is_active
    booking.payment_status = payment_status
    booking.status = status
    return booking


def make_customer(is_active=True):
    customer = MagicMock()
    customer.id = uuid.uuid4()
    customer.is_active = is_active
    return customer


def make_property(is_active=True):
    property_ = MagicMock()
    property_.id = 101
    property_.is_active = is_active
    return property_


def make_user(is_active=True):
    user = MagicMock()
    user.id = 1
    user.is_active = is_active
    return user


def make_payment(
    payment_status=PaymentStatus.PENDING,
    payment_amount=Decimal("100000.00"),
    transaction_reference="TXN-001",
    booking_id=None,
):
    payment = MagicMock()
    payment.id = uuid.uuid4()
    payment.booking_id = booking_id or uuid.uuid4()
    payment.payment_status = payment_status
    payment.payment_amount = payment_amount
    payment.transaction_reference = transaction_reference
    payment.payment_number = "PAY-2026-000001"
    payment.receipt_number = "RCPT-2026-000001"
    return payment


@pytest.fixture
def service(db_session_mock):
    svc = PaymentService(db_session_mock)
    svc.payment_repo = AsyncMock()
    svc.booking_repo = AsyncMock()
    svc.customer_repo = AsyncMock()
    svc.property_repo = AsyncMock()
    svc.user_repo = AsyncMock()
    return svc


@pytest.fixture
def valid_payload():
    return PaymentCreate(
        booking_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        property_id=101,
        received_by=1,
        payment_date=date.today(),
        payment_amount=Decimal("100000.00"),
        payment_mode=PaymentMode.BANK_TRANSFER,
        transaction_reference="TXN-VALID-001",
        payment_type=PaymentType.TOKEN,
        bank_name="Test Bank",
    )


class TestCreatePaymentValidation:

    async def test_booking_not_found_raises_not_found(self, service, valid_payload):
        service.booking_repo.get_by_id.return_value = None
        with pytest.raises(NotFoundException):
            await service.create_payment(valid_payload)

    async def test_inactive_booking_rejected(self, service, valid_payload):
        service.booking_repo.get_by_id.return_value = make_booking(is_active=False)
        with pytest.raises(BadRequestException):
            await service.create_payment(valid_payload)

    async def test_customer_not_found_raises_not_found(self, service, valid_payload):
        service.booking_repo.get_by_id.return_value = make_booking()
        service.customer_repo.get_by_id.return_value = None
        with pytest.raises(NotFoundException):
            await service.create_payment(valid_payload)

    async def test_inactive_customer_rejected(self, service, valid_payload):
        service.booking_repo.get_by_id.return_value = make_booking()
        service.customer_repo.get_by_id.return_value = make_customer(is_active=False)
        with pytest.raises(BadRequestException):
            await service.create_payment(valid_payload)

    async def test_property_not_found_raises_not_found(self, service, valid_payload):
        service.booking_repo.get_by_id.return_value = make_booking()
        service.customer_repo.get_by_id.return_value = make_customer()
        service.property_repo.get_by_id.return_value = None
        with pytest.raises(NotFoundException):
            await service.create_payment(valid_payload)

    async def test_inactive_property_rejected(self, service, valid_payload):
        service.booking_repo.get_by_id.return_value = make_booking()
        service.customer_repo.get_by_id.return_value = make_customer()
        service.property_repo.get_by_id.return_value = make_property(is_active=False)
        with pytest.raises(BadRequestException):
            await service.create_payment(valid_payload)

    async def test_received_by_user_not_found(self, service, valid_payload):
        service.booking_repo.get_by_id.return_value = make_booking()
        service.customer_repo.get_by_id.return_value = make_customer()
        service.property_repo.get_by_id.return_value = make_property()
        service.user_repo.get_by_id.return_value = None
        with pytest.raises(NotFoundException):
            await service.create_payment(valid_payload)

    async def test_inactive_received_by_user_rejected(self, service, valid_payload):
        service.booking_repo.get_by_id.return_value = make_booking()
        service.customer_repo.get_by_id.return_value = make_customer()
        service.property_repo.get_by_id.return_value = make_property()
        service.user_repo.get_by_id.return_value = make_user(is_active=False)
        with pytest.raises(BadRequestException):
            await service.create_payment(valid_payload)

    async def test_amount_exceeds_pending_rejected(self, service, valid_payload):
        booking = make_booking(total_amount=Decimal("50000.00"), paid_amount=Decimal("0.00"))
        service.booking_repo.get_by_id.return_value = booking
        service.customer_repo.get_by_id.return_value = make_customer()
        service.property_repo.get_by_id.return_value = make_property()
        service.user_repo.get_by_id.return_value = make_user()
        valid_payload.payment_amount = Decimal("100000.00")
        with pytest.raises(ValidationException):
            await service.create_payment(valid_payload)

    async def test_no_pending_amount_rejected(self, service, valid_payload):
        booking = make_booking(total_amount=Decimal("100000.00"), paid_amount=Decimal("100000.00"))
        service.booking_repo.get_by_id.return_value = booking
        service.customer_repo.get_by_id.return_value = make_customer()
        service.property_repo.get_by_id.return_value = make_property()
        service.user_repo.get_by_id.return_value = make_user()
        with pytest.raises(BadRequestException):
            await service.create_payment(valid_payload)

    async def test_duplicate_transaction_reference_success_rejected(
        self, service, valid_payload
    ):
        service.booking_repo.get_by_id.return_value = make_booking()
        service.customer_repo.get_by_id.return_value = make_customer()
        service.property_repo.get_by_id.return_value = make_property()
        service.user_repo.get_by_id.return_value = make_user()

        existing = make_payment(payment_status=PaymentStatus.SUCCESS)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        service.payment_repo.db.execute = AsyncMock(return_value=result_mock)

        valid_payload.payment_status = PaymentStatus.SUCCESS
        with pytest.raises(ConflictException):
            await service.create_payment(valid_payload)


class TestPaymentTypeRules:

    async def test_token_requires_pending_booking(self, service, valid_payload):
        booking = make_booking(payment_status=BookingPaymentStatus.PARTIAL)
        service.booking_repo.get_by_id.return_value = booking
        service.customer_repo.get_by_id.return_value = make_customer()
        service.property_repo.get_by_id.return_value = make_property()
        service.user_repo.get_by_id.return_value = make_user()

        valid_payload.payment_type = PaymentType.TOKEN
        with pytest.raises(BadRequestException):
            await service.create_payment(valid_payload)

    async def test_installment_requires_pending_or_partial(self, service, valid_payload):
        booking = make_booking(
            total_amount=Decimal("1000000.00"),
            paid_amount=Decimal("500000.00"),
            payment_status=BookingPaymentStatus.PARTIAL,
        )
        service.booking_repo.get_by_id.return_value = booking
        service.customer_repo.get_by_id.return_value = make_customer()
        service.property_repo.get_by_id.return_value = make_property()
        service.user_repo.get_by_id.return_value = make_user()
        service.payment_repo.create.return_value = make_payment(booking_id=booking.id)

        valid_payload.payment_type = PaymentType.INSTALLMENT
        valid_payload.payment_amount = Decimal("100000.00")
        result = await service.create_payment(valid_payload)
        assert result is not None

    async def test_full_payment_must_match_pending_exactly(self, service, valid_payload):
        booking = make_booking(total_amount=Decimal("500000.00"), paid_amount=Decimal("0.00"))
        service.booking_repo.get_by_id.return_value = booking
        service.customer_repo.get_by_id.return_value = make_customer()
        service.property_repo.get_by_id.return_value = make_property()
        service.user_repo.get_by_id.return_value = make_user()

        valid_payload.payment_type = PaymentType.FULL_PAYMENT
        valid_payload.payment_amount = Decimal("400000.00")
        with pytest.raises(ValidationException):
            await service.create_payment(valid_payload)

    async def test_refund_requires_prior_payment(self, service, valid_payload):
        booking = make_booking(paid_amount=Decimal("0.00"))
        service.booking_repo.get_by_id.return_value = booking
        service.customer_repo.get_by_id.return_value = make_customer()
        service.property_repo.get_by_id.return_value = make_property()
        service.user_repo.get_by_id.return_value = make_user()

        valid_payload.payment_type = PaymentType.REFUND
        with pytest.raises(BadRequestException):
            await service.create_payment(valid_payload)

    async def test_refund_allowed_with_prior_payment(self, service, valid_payload):
        booking = make_booking(paid_amount=Decimal("200000.00"))
        service.booking_repo.get_by_id.return_value = booking
        service.customer_repo.get_by_id.return_value = make_customer()
        service.property_repo.get_by_id.return_value = make_property()
        service.user_repo.get_by_id.return_value = make_user()
        service.payment_repo.create.return_value = make_payment(booking_id=booking.id)

        valid_payload.payment_type = PaymentType.REFUND
        valid_payload.payment_amount = Decimal("50000.00")
        result = await service.create_payment(valid_payload)
        assert result is not None


class TestPartialAndFullPayment:

    async def test_partial_payment_success_updates_booking(self, service, valid_payload):
        booking = make_booking(total_amount=Decimal("1000000.00"), paid_amount=Decimal("0.00"))
        service.booking_repo.get_by_id.return_value = booking
        service.customer_repo.get_by_id.return_value = make_customer()
        service.property_repo.get_by_id.return_value = make_property()
        service.user_repo.get_by_id.return_value = make_user()

        created_payment = make_payment(
            payment_status=PaymentStatus.SUCCESS, booking_id=booking.id
        )
        service.payment_repo.create.return_value = created_payment
        service.payment_repo.get_total_paid_for_booking.return_value = Decimal("400000.00")

        valid_payload.payment_amount = Decimal("400000.00")
        valid_payload.payment_status = PaymentStatus.SUCCESS
        valid_payload.payment_type = PaymentType.ADVANCE

        await service.create_payment(valid_payload)
        assert booking.payment_status == BookingPaymentStatus.PARTIAL

    async def test_full_payment_completes_booking(self, service, valid_payload):
        booking = make_booking(total_amount=Decimal("1000000.00"), paid_amount=Decimal("0.00"))
        service.booking_repo.get_by_id.return_value = booking
        service.customer_repo.get_by_id.return_value = make_customer()
        service.property_repo.get_by_id.return_value = make_property()
        service.user_repo.get_by_id.return_value = make_user()

        created_payment = make_payment(
            payment_status=PaymentStatus.SUCCESS, booking_id=booking.id
        )
        service.payment_repo.create.return_value = created_payment
        service.payment_repo.get_total_paid_for_booking.return_value = Decimal("1000000.00")

        valid_payload.payment_amount = Decimal("1000000.00")
        valid_payload.payment_status = PaymentStatus.SUCCESS
        valid_payload.payment_type = PaymentType.FULL_PAYMENT

        await service.create_payment(valid_payload)
        assert booking.payment_status == BookingPaymentStatus.PAID
        assert booking.status == BookingStatus.COMPLETED


class TestStatusTransitions:

    async def test_pending_to_success_allowed(self, service):
        payment = make_payment(payment_status=PaymentStatus.PENDING)
        service.payment_repo.get_by_id.return_value = payment
        service.payment_repo.update_status.return_value = make_payment(
            payment_status=PaymentStatus.SUCCESS
        )
        service.payment_repo.get_total_paid_for_booking.return_value = Decimal("0.00")
        service.booking_repo.get_by_id.return_value = make_booking()

        status_data = PaymentStatusUpdate(payment_status=PaymentStatus.SUCCESS)
        result = await service.update_payment_status(payment.id, status_data)
        assert result is not None

    async def test_failed_to_success_rejected(self, service):
        payment = make_payment(payment_status=PaymentStatus.FAILED)
        service.payment_repo.get_by_id.return_value = payment

        status_data = PaymentStatusUpdate(payment_status=PaymentStatus.SUCCESS)
        with pytest.raises(BadRequestException):
            await service.update_payment_status(payment.id, status_data)

    async def test_refunded_to_success_rejected(self, service):
        payment = make_payment(payment_status=PaymentStatus.REFUNDED)
        service.payment_repo.get_by_id.return_value = payment

        status_data = PaymentStatusUpdate(payment_status=PaymentStatus.SUCCESS)
        with pytest.raises(BadRequestException):
            await service.update_payment_status(payment.id, status_data)

    async def test_success_to_refunded_allowed(self, service):
        payment = make_payment(payment_status=PaymentStatus.SUCCESS)
        service.payment_repo.get_by_id.return_value = payment
        service.payment_repo.update_status.return_value = make_payment(
            payment_status=PaymentStatus.REFUNDED
        )
        service.payment_repo.get_total_paid_for_booking.return_value = Decimal("0.00")
        service.booking_repo.get_by_id.return_value = make_booking()

        status_data = PaymentStatusUpdate(payment_status=PaymentStatus.REFUNDED)
        result = await service.update_payment_status(payment.id, status_data)
        assert result is not None

    async def test_partial_to_success_allowed(self, service):
        payment = make_payment(payment_status=PaymentStatus.PARTIAL)
        service.payment_repo.get_by_id.return_value = payment
        service.payment_repo.update_status.return_value = make_payment(
            payment_status=PaymentStatus.SUCCESS
        )
        service.payment_repo.get_total_paid_for_booking.return_value = Decimal("0.00")
        service.booking_repo.get_by_id.return_value = make_booking()

        status_data = PaymentStatusUpdate(payment_status=PaymentStatus.SUCCESS)
        result = await service.update_payment_status(payment.id, status_data)
        assert result is not None

    async def test_payment_not_found_raises_not_found(self, service):
        service.payment_repo.get_by_id.return_value = None
        status_data = PaymentStatusUpdate(payment_status=PaymentStatus.SUCCESS)
        with pytest.raises(NotFoundException):
            await service.update_payment_status(uuid.uuid4(), status_data)


class TestPaymentNumberAndReceiptGeneration:

    async def test_payment_number_increments_sequence(self, service):
        service.payment_repo.get_last_payment_number.return_value = "PAY-2026-000042"
        number = await service._generate_payment_number()
        assert number == "PAY-2026-000043"

    async def test_payment_number_starts_at_one(self, service):
        service.payment_repo.get_last_payment_number.return_value = None
        number = await service._generate_payment_number()
        assert number.endswith("000001")

    def test_receipt_number_generation(self, service):
        receipt = service._generate_receipt_number("PAY-2026-000001")
        assert receipt == "RCPT-2026-000001"


class TestPaymentDeletion:

    async def test_delete_success_payment_rejected(self, service):
        payment = make_payment(payment_status=PaymentStatus.SUCCESS)
        service.payment_repo.get_by_id.return_value = payment
        with pytest.raises(ConflictException):
            await service.delete_payment(payment.id)

    async def test_delete_pending_payment_allowed(self, service):
        payment = make_payment(payment_status=PaymentStatus.PENDING)
        service.payment_repo.get_by_id.return_value = payment
        service.payment_repo.soft_delete.return_value = True
        await service.delete_payment(payment.id)
        service.payment_repo.soft_delete.assert_awaited_once_with(payment.id)

    async def test_delete_nonexistent_payment_raises_not_found(self, service):
        service.payment_repo.get_by_id.return_value = None
        with pytest.raises(NotFoundException):
            await service.delete_payment(uuid.uuid4())


class TestDashboardAndRevenue:

    async def test_get_dashboard_summary(self, service):
        service.payment_repo.get_dashboard_summary.return_value = {
            "total_payments_count": 10,
            "total_revenue": Decimal("1000000.00"),
            "today_payments_count": 2,
            "today_revenue": Decimal("50000.00"),
            "monthly_revenue": Decimal("300000.00"),
            "pending_amount": Decimal("100000.00"),
            "success_amount": Decimal("800000.00"),
            "failed_count": 1,
            "refunded_amount": Decimal("20000.00"),
            "partial_count": 3,
        }
        summary = await service.get_dashboard_summary()
        assert summary.total_payments_count == 10

    async def test_monthly_revenue_invalid_month_rejected(self, service):
        with pytest.raises(ValidationException):
            await service.get_monthly_revenue(2026, 13)

    async def test_monthly_revenue_valid(self, service):
        service.payment_repo.get_monthly_revenue.return_value = Decimal("500000.00")
        revenue = await service.get_monthly_revenue(2026, 8)
        assert revenue == Decimal("500000.00")