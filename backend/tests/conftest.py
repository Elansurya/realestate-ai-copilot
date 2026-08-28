import pytest
"""
Additions for backend/tests/conftest.py

Paste everything below the existing imports into your conftest.py
(or append this whole block to the end of the existing file). Nothing
here redefines db_session / app / async_client — those keep coming
from test_booking_api.py exactly as already wired.

Every fixture below is built strictly from the real, uploaded source:
    - app/models/payment.py            (Payment, PaymentStatus, PaymentMode, PaymentType)
    - app/schemas/payment.py           (PaymentCreate field names/validators)
    - app/services/payment_service.py  (what create_payment/status-update validate)
    - tests/test_booking_api.py        (admin_client, sales_agent_client,
                                         customer, property_obj, _create_booking_via_api,
                                         BOOKINGS_PREFIX, create_access_token usage)

No model fields, enum values, or auth mechanisms are invented.
"""

import itertools
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from datetime import date
from decimal import Decimal

import pytest_asyncio

from app.core.security import create_access_token
from app.models.payment import Payment, PaymentMode, PaymentStatus, PaymentType

from tests.test_booking_api import (
    db_session,
    app,
    client as async_client,
)

from tests.test_booking_api import (
    BOOKINGS_PREFIX,
    _create_booking_via_api,
    _customer_creator,
    admin_client,
    admin_user,
    customer,
    property_obj,
    sales_agent_client,
    sales_agent_user,
)


# --------------------------------------------------------------------------
# Auth token fixtures
#
# test_payment_api.py builds its own Authorization headers via
# auth_headers(token), unlike test_booking_api.py's admin_client /
# sales_agent_client (which bake the header into an AsyncClient). So
# these fixtures return the bare JWT string, minted the same way
# _bearer_client() in test_booking_api.py already does — same
# create_access_token call, same subject convention (str(user.id)) —
# just without wrapping it in a client.
# --------------------------------------------------------------------------
@pytest_asyncio.fixture
async def admin_token(admin_user) -> str:
    return create_access_token(subject=str(admin_user.id))


@pytest_asyncio.fixture
async def sales_agent_token(sales_agent_user) -> str:
    return create_access_token(subject=str(sales_agent_user.id))


# --------------------------------------------------------------------------
# Booking helper
#
# Payment creation (via the API) requires an existing, active Booking
# to reference by booking_id — PaymentService._validate_booking() 404s
# / 400s otherwise. This reuses the exact same helper test_booking_api.py
# uses for its own booking-creation tests, through admin_client, so no
# booking-creation logic is duplicated here.
# --------------------------------------------------------------------------
@pytest_asyncio.fixture
async def _active_booking(admin_client, customer, property_obj) -> dict:
    return await _create_booking_via_api(admin_client, customer.id, property_obj.id)


@pytest_asyncio.fixture
async def booking_fixture(admin_client, customer, property_obj):
    """Create a real booking row for repository/service tests."""
    data = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    booking_id = data["id"]
    # The API helper returns JSON; repository tests need an object exposing .id.
    return SimpleNamespace(id=uuid.UUID(str(booking_id)))


@pytest_asyncio.fixture
async def async_session(db_session):
    """Backward-compatible alias for repository suites using async_session."""
    return db_session


@pytest_asyncio.fixture
async def customer_fixture(customer):
    return customer


@pytest_asyncio.fixture
async def property_fixture(property_obj):
    return property_obj


@pytest_asyncio.fixture
async def user_fixture(admin_user):
    return admin_user


# --------------------------------------------------------------------------
# Payload fixtures (dicts posted as JSON to POST /api/v1/payments)
#
# Field set matches PaymentCreate exactly (app/schemas/payment.py):
# booking_id, customer_id, property_id, received_by(optional),
# payment_date, payment_amount, payment_mode, transaction_reference
# (optional), payment_type, bank_name(optional), cheque_number
# (optional), remarks(optional), payment_status(optional, default
# PENDING), receipt_number(optional). payment_mode="UPI" and
# payment_type="TOKEN" are real PaymentMode/PaymentType members.
# --------------------------------------------------------------------------
_TXN_REF_COUNTER = itertools.count(1)


def _unique_txn_reference() -> str:
    return f"TESTTXN{next(_TXN_REF_COUNTER):08d}"


@pytest_asyncio.fixture
async def payment_payload(_active_booking, customer, property_obj) -> dict:
    return {
        "booking_id": _active_booking["id"],
        "customer_id": str(customer.id),
        "property_id": property_obj.id,
        "payment_date": date.today().isoformat(),
        "payment_amount": 100000,
        "payment_mode": PaymentMode.UPI.value,
        "transaction_reference": _unique_txn_reference(),
        "payment_type": PaymentType.TOKEN.value,
        "remarks": "Test payment via fixture",
    }


@pytest_asyncio.fixture
async def inactive_booking_payload(admin_client, customer, property_obj) -> dict:
    """
    A payload referencing a booking that has been soft-deleted (is_active
    is False), for the 400 "inactive booking" case in
    PaymentService._validate_booking().
    """
    booking = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    resp = await admin_client.delete(f"{BOOKINGS_PREFIX}/{booking['id']}")
    assert resp.status_code == 200, resp.text

    return {
        "booking_id": booking["id"],
        "customer_id": str(customer.id),
        "property_id": property_obj.id,
        "payment_date": date.today().isoformat(),
        "payment_amount": 50000,
        "payment_mode": PaymentMode.UPI.value,
        "transaction_reference": _unique_txn_reference(),
        "payment_type": PaymentType.TOKEN.value,
        "remarks": "Payment attempted against an inactive booking",
    }


@pytest_asyncio.fixture
async def success_payment_payload(_active_booking, customer, property_obj) -> dict:
    """
    payment_status is explicitly SUCCESS: PaymentService only enforces
    transaction_reference uniqueness when payment_status == SUCCESS
    (see _validate_transaction_reference_uniqueness), so the duplicate-
    reference 409 test needs this, not the PENDING default.
    """
    return {
        "booking_id": _active_booking["id"],
        "customer_id": str(customer.id),
        "property_id": property_obj.id,
        "payment_date": date.today().isoformat(),
        "payment_amount": 100000,
        "payment_mode": PaymentMode.UPI.value,
        "transaction_reference": _unique_txn_reference(),
        "payment_type": PaymentType.TOKEN.value,
        "payment_status": PaymentStatus.SUCCESS.value,
        "remarks": "Test success payment via fixture",
    }


# --------------------------------------------------------------------------
# Direct-DB Payment row fixtures (for tests that need an existing
# payment_id, not a fresh POST)
#
# Inserted directly through db_session/Payment(...) rather than through
# PaymentService.create_payment(), because the service path (see the
# module docstring above) currently raises AttributeError on
# booking.total_amount / booking.paid_amount, which don't exist on the
# real Booking model. Bypassing the service for row setup keeps these
# fixtures usable regardless of that separate service-layer issue, and
# only sets columns that genuinely exist on Payment (app/models/payment.py).
# --------------------------------------------------------------------------
_PAYMENT_NUMBER_COUNTER = itertools.count(1)


def _unique_test_payment_number() -> str:
    return f"PAY-TEST-{next(_PAYMENT_NUMBER_COUNTER):06d}"


async def _make_payment(
    db_session,
    booking_id: uuid.UUID,
    customer_id: uuid.UUID,
    property_id: int,
    payment_status: PaymentStatus,
    payment_amount: Decimal = Decimal("100000"),
    payment_type: PaymentType = PaymentType.TOKEN,
    payment_mode: PaymentMode = PaymentMode.UPI,
    transaction_reference: str | None = None,
) -> Payment:
    payment = Payment(
        payment_number=_unique_test_payment_number(),
        booking_id=booking_id,
        customer_id=customer_id,
        property_id=property_id,
        payment_amount=payment_amount,
        payment_mode=payment_mode,
        payment_type=payment_type,
        payment_status=payment_status,
        transaction_reference=transaction_reference,
    )
    db_session.add(payment)
    await db_session.commit()
    await db_session.refresh(payment)
    return payment


@pytest_asyncio.fixture
async def payment_id_fixture(db_session, _active_booking, customer, property_obj) -> str:
    """Any existing, active payment — used by the 403 authorization tests."""
    payment = await _make_payment(
        db_session,
        uuid.UUID(_active_booking["id"]),
        customer.id,
        property_obj.id,
        PaymentStatus.PENDING,
    )
    return str(payment.id)


@pytest_asyncio.fixture
async def pending_payment_id(db_session, _active_booking, customer, property_obj) -> str:
    payment = await _make_payment(
        db_session,
        uuid.UUID(_active_booking["id"]),
        customer.id,
        property_obj.id,
        PaymentStatus.PENDING,
    )
    return str(payment.id)


@pytest_asyncio.fixture
async def success_payment_id(db_session, _active_booking, customer, property_obj) -> str:
    payment = await _make_payment(
        db_session,
        uuid.UUID(_active_booking["id"]),
        customer.id,
        property_obj.id,
        PaymentStatus.SUCCESS,
    )
    return str(payment.id)


@pytest_asyncio.fixture
async def failed_payment_id(db_session, _active_booking, customer, property_obj) -> str:
    payment = await _make_payment(
        db_session,
        uuid.UUID(_active_booking["id"]),
        customer.id,
        property_obj.id,
        PaymentStatus.FAILED,
    )
    return str(payment.id)
# --------------------------------------------------------------------------
# Shared unit-test fixtures
# --------------------------------------------------------------------------
# Several service suites exercise orchestration with the repository fully
# mocked.  They require a session-shaped object but must not touch a real
# database.  Keep this fixture here so every service test gets the same
# lightweight async-session double.
@pytest.fixture
def mocker(monkeypatch):
    class _Patch:
        def object(self, target, attribute, new=None, **kwargs):
            if new is None:
                factory = kwargs.pop("new_callable", None)
                new = factory(**kwargs) if factory is not None else MagicMock(**kwargs)
            monkeypatch.setattr(target, attribute, new)
            return new
    class _Mocker:
        AsyncMock = AsyncMock
        MagicMock = MagicMock
        Mock = MagicMock
        patch = _Patch()
    return _Mocker()

@pytest.fixture
def db_session_mock():
    from unittest.mock import AsyncMock, MagicMock

    session = MagicMock(name="db_session_mock")
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.execute = AsyncMock()
    session.add = MagicMock()
    session.add_all = MagicMock()
    return session



@pytest_asyncio.fixture
async def create_user_id(db_session):
    from app.models.user import User, UserRole
    async def _create_user_id(**overrides):
        data = {
            "uuid": str(uuid.uuid4()),
            "full_name": "Repository Test User",
            "email": f"repo_user_{uuid.uuid4().hex[:12]}@example.com",
            "phone": f"+1555{uuid.uuid4().int % 10_000_000:07d}",
            "password_hash": "not-a-real-hash",
            "role": UserRole.ADMIN,
            "is_active": True,
            "is_verified": True,
        }
        data.update(overrides)
        user = User(**data)
        db_session.add(user)
        await db_session.flush()
        return user.id
    return _create_user_id


@pytest_asyncio.fixture
async def seed_users(db_session):
    """Create the four users required by workflow repository tests."""
    from app.models.user import User, UserRole

    users = {}
    specs = {
        "initiator": (UserRole.ADMIN, "workflow_initiator"),
        "assignee": (UserRole.SALES_MANAGER, "workflow_assignee"),
        "approver": (UserRole.SALES_MANAGER, "workflow_approver"),
        "escalation_target": (UserRole.ADMIN, "workflow_escalation"),
    }
    for key, (role, label) in specs.items():
        user = User(
            uuid=str(uuid.uuid4()),
            full_name=label.replace("_", " ").title(),
            email=f"{label}_{uuid.uuid4().hex[:10]}@example.com",
            phone=f"+1555{uuid.uuid4().int % 10_000_000:07d}",
            password_hash="not-a-real-hash",
            role=role,
            is_active=True,
        )
        db_session.add(user)
        await db_session.flush()
        users[key] = user.id
    await db_session.commit()
    return users
