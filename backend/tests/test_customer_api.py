"""
backend/tests/test_customer_api.py

HTTP-level smoke, validation, and authorization tests for the Customer
module (`app/api/v1/customer.py`), run against a real PostgreSQL
database (the same `settings.DATABASE_URL` used by the application --
never SQLite/in-memory).

Fixture layout mirrors `tests/test_booking_api.py`: a function-scoped
`db_session` bound to a single connection/outer transaction that is
rolled back after every test, an `app` fixture with `get_db` overridden
to yield that same session, and role-scoped HTTP clients built from
real, persisted `User` rows and real JWTs via
`app.core.security.create_access_token`.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import date, timedelta
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.deps import get_db
from app.core.config import settings
from app.core.security import create_access_token
from app.main import app as fastapi_app
from app.models.user import User, UserRole

pytestmark = pytest.mark.asyncio

CUSTOMERS_PREFIX = "/api/v1/customers"

_PHONE_COUNTER = itertools.count(9_100_000_001)
_EMAIL_COUNTER = itertools.count(1)


def _unique_phone() -> str:
    return f"+91{next(_PHONE_COUNTER)}"


def _unique_email() -> str:
    return f"customer.test.{next(_EMAIL_COUNTER)}@example.com"


# --------------------------------------------------------------------------
# Core fixtures (db_session / app / client)
# --------------------------------------------------------------------------
@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """
    Function-scoped `AsyncSession` bound to a single connection and a
    single outer transaction, rolled back after each test. Targets the
    real PostgreSQL database configured via `settings.DATABASE_URL`;
    never SQLite or any in-memory substitute.
    """
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)

    async with engine.connect() as connection:
        transaction = await connection.begin()

        session_factory = async_sessionmaker(
            bind=connection,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        session = session_factory()

        try:
            yield session
        finally:
            await session.close()
            if transaction.is_active:
                await transaction.rollback()

    await engine.dispose()


@pytest_asyncio.fixture
async def app(db_session: AsyncSession):
    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    fastapi_app.dependency_overrides[get_db] = _override_get_db
    yield fastapi_app
    fastapi_app.dependency_overrides.pop(get_db, None)


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    """Unauthenticated async HTTP client bound to the test app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# --------------------------------------------------------------------------
# User / auth fixtures
# --------------------------------------------------------------------------
USER_COLUMNS = set(User.__table__.columns.keys())


def _discover_password_field() -> str | None:
    for candidate in ("hashed_password", "password_hash", "password", "hashed_pw", "pw_hash", "hash"):
        if candidate in USER_COLUMNS:
            return candidate
    return None


PASSWORD_FIELD = _discover_password_field()


async def _make_user(db_session: AsyncSession, role: UserRole, email: str) -> User:
    kwargs = {
        "email": email,
        "full_name": f"{role.value.title()} Test User",
        "role": role,
        "is_active": True,
    }
    if PASSWORD_FIELD:
        kwargs[PASSWORD_FIELD] = "not-a-real-hash-$2b$12$test.value.only"
    if "phone" in USER_COLUMNS:
        kwargs["phone"] = _unique_phone()
    if "is_verified" in USER_COLUMNS:
        kwargs["is_verified"] = True
    if "uuid" in USER_COLUMNS:
        kwargs["uuid"] = str(uuid.uuid4())

    user = User(**kwargs)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bearer_client(app, user: User) -> AsyncClient:
    token = create_access_token(subject=str(user.id))
    transport = ASGITransport(app=app)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, UserRole.ADMIN, "admin@customertest.io")


@pytest_asyncio.fixture
async def sales_agent_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, UserRole.SALES_AGENT, "agent@customertest.io")


@pytest_asyncio.fixture
async def admin_client(app, admin_user: User) -> AsyncIterator[AsyncClient]:
    async with _bearer_client(app, admin_user) as ac:
        yield ac


@pytest_asyncio.fixture
async def sales_agent_client(app, sales_agent_user: User) -> AsyncIterator[AsyncClient]:
    async with _bearer_client(app, sales_agent_user) as ac:
        yield ac


# --------------------------------------------------------------------------
# Payload helper
# --------------------------------------------------------------------------
def _customer_payload(**overrides) -> dict:
    payload = {
        "first_name": "Rohan",
        "last_name": "Sharma",
        "email": _unique_email(),
        "phone": _unique_phone(),
        "customer_type": "BUYER",
        "customer_source": "WEBSITE",
        "status": "ACTIVE",
    }
    payload.update(overrides)
    return payload


async def _create_customer_via_api(client: AsyncClient, **overrides) -> dict:
    resp = await client.post(f"{CUSTOMERS_PREFIX}/", json=_customer_payload(**overrides))
    assert resp.status_code == 201, resp.text
    return resp.json()


# ==========================================================================
# Smoke tests: full CRUD lifecycle
# ==========================================================================
class TestCustomerSmoke:
    async def test_create_customer(self, admin_client: AsyncClient):
        body = await _create_customer_via_api(admin_client)
        assert body["first_name"] == "Rohan"
        assert body["status"] == "ACTIVE"
        assert body["is_active"] is True
        assert "id" in body

    async def test_get_customer_by_id(self, admin_client: AsyncClient):
        created = await _create_customer_via_api(admin_client)
        resp = await admin_client.get(f"{CUSTOMERS_PREFIX}/{created['id']}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == created["id"]

    async def test_list_customers(self, admin_client: AsyncClient):
        await _create_customer_via_api(admin_client)
        await _create_customer_via_api(admin_client)
        resp = await admin_client.get(f"{CUSTOMERS_PREFIX}/")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] >= 2
        assert len(body["items"]) >= 2

    async def test_update_customer(self, admin_client: AsyncClient):
        created = await _create_customer_via_api(admin_client)
        resp = await admin_client.put(
            f"{CUSTOMERS_PREFIX}/{created['id']}", json={"city": "Bengaluru"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["city"] == "Bengaluru"

    async def test_soft_delete_and_restore(self, admin_client: AsyncClient):
        created = await _create_customer_via_api(admin_client)
        resp = await admin_client.patch(f"{CUSTOMERS_PREFIX}/{created['id']}/soft-delete")
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_active"] is False

        resp = await admin_client.patch(f"{CUSTOMERS_PREFIX}/{created['id']}/restore")
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_active"] is True

    async def test_hard_delete(self, admin_client: AsyncClient):
        created = await _create_customer_via_api(admin_client)
        resp = await admin_client.delete(f"{CUSTOMERS_PREFIX}/{created['id']}")
        assert resp.status_code == 204, resp.text

        resp = await admin_client.get(f"{CUSTOMERS_PREFIX}/{created['id']}")
        assert resp.status_code == 404

    async def test_assign_and_unassign(self, admin_client: AsyncClient, sales_agent_user: User):
        created = await _create_customer_via_api(admin_client)
        resp = await admin_client.patch(
            f"{CUSTOMERS_PREFIX}/{created['id']}/assign",
            json={"user_id": sales_agent_user.id},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["assigned_to_id"] == sales_agent_user.id

        resp = await admin_client.patch(f"{CUSTOMERS_PREFIX}/{created['id']}/unassign")
        assert resp.status_code == 200, resp.text
        assert resp.json()["assigned_to_id"] is None

    async def test_update_status(self, admin_client: AsyncClient):
        created = await _create_customer_via_api(admin_client)
        resp = await admin_client.patch(
            f"{CUSTOMERS_PREFIX}/{created['id']}/status", json={"status": "PROSPECT"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "PROSPECT"

    async def test_update_followup(self, admin_client: AsyncClient):
        created = await _create_customer_via_api(admin_client)
        future_date = (date.today() + timedelta(days=7)).isoformat()
        resp = await admin_client.patch(
            f"{CUSTOMERS_PREFIX}/{created['id']}/followup",
            json={"next_followup_date": future_date},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["next_followup_date"] == future_date

    async def test_search_customers(self, admin_client: AsyncClient):
        await _create_customer_via_api(admin_client, first_name="Zenith")
        resp = await admin_client.post(
            f"{CUSTOMERS_PREFIX}/search", json={"search": "Zenith", "page": 1, "page_size": 20}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] >= 1
        assert any(item["first_name"] == "Zenith" for item in body["items"])

    async def test_statistics(self, admin_client: AsyncClient):
        await _create_customer_via_api(admin_client)
        resp = await admin_client.get(f"{CUSTOMERS_PREFIX}/statistics")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "total_customers" in body
        assert "customers_by_status" in body

    async def test_export_customers(self, admin_client: AsyncClient):
        await _create_customer_via_api(admin_client)
        resp = await admin_client.post(f"{CUSTOMERS_PREFIX}/export", json={"export_format": "csv"})
        assert resp.status_code == 200, resp.text


# ==========================================================================
# Validation / error cases
# ==========================================================================
class TestCustomerValidation:
    async def test_create_duplicate_email_conflict(self, admin_client: AsyncClient):
        email = _unique_email()
        await _create_customer_via_api(admin_client, email=email)
        resp = await admin_client.post(
            f"{CUSTOMERS_PREFIX}/", json=_customer_payload(email=email)
        )
        assert resp.status_code == 409, resp.text

    async def test_create_invalid_pan_number(self, admin_client: AsyncClient):
        resp = await admin_client.post(
            f"{CUSTOMERS_PREFIX}/", json=_customer_payload(pan_number="INVALID123")
        )
        assert resp.status_code == 422, resp.text

    async def test_create_budget_max_less_than_min(self, admin_client: AsyncClient):
        resp = await admin_client.post(
            f"{CUSTOMERS_PREFIX}/",
            json=_customer_payload(budget_min=5_000_000, budget_max=1_000_000),
        )
        assert resp.status_code == 422, resp.text

    async def test_create_missing_required_field(self, admin_client: AsyncClient):
        payload = _customer_payload()
        del payload["email"]
        resp = await admin_client.post(f"{CUSTOMERS_PREFIX}/", json=payload)
        assert resp.status_code == 422, resp.text

    async def test_create_nonexistent_lead_id(self, admin_client: AsyncClient):
        resp = await admin_client.post(
            f"{CUSTOMERS_PREFIX}/",
            json=_customer_payload(lead_id=str(uuid.uuid4())),
        )
        assert resp.status_code == 422, resp.text

    async def test_create_nonexistent_assigned_to_id(self, admin_client: AsyncClient):
        resp = await admin_client.post(
            f"{CUSTOMERS_PREFIX}/", json=_customer_payload(assigned_to_id=999_999)
        )
        assert resp.status_code == 422, resp.text

    async def test_get_nonexistent_customer_404(self, admin_client: AsyncClient):
        resp = await admin_client.get(f"{CUSTOMERS_PREFIX}/{uuid.uuid4()}")
        assert resp.status_code == 404, resp.text

    async def test_update_empty_payload_rejected(self, admin_client: AsyncClient):
        created = await _create_customer_via_api(admin_client)
        resp = await admin_client.put(f"{CUSTOMERS_PREFIX}/{created['id']}", json={})
        assert resp.status_code == 422, resp.text

    async def test_followup_date_in_past_rejected(self, admin_client: AsyncClient):
        created = await _create_customer_via_api(admin_client)
        past_date = (date.today() - timedelta(days=1)).isoformat()
        resp = await admin_client.patch(
            f"{CUSTOMERS_PREFIX}/{created['id']}/followup",
            json={"next_followup_date": past_date},
        )
        assert resp.status_code == 409, resp.text


# ==========================================================================
# Authentication / authorization
# ==========================================================================
class TestCustomerAuth:
    async def test_create_requires_authentication(self, client: AsyncClient):
        resp = await client.post(f"{CUSTOMERS_PREFIX}/", json=_customer_payload())
        assert resp.status_code == 401, resp.text

    async def test_list_requires_authentication(self, client: AsyncClient):
        resp = await client.get(f"{CUSTOMERS_PREFIX}/")
        assert resp.status_code == 401, resp.text

    async def test_sales_agent_can_create(self, sales_agent_client: AsyncClient):
        resp = await sales_agent_client.post(
            f"{CUSTOMERS_PREFIX}/", json=_customer_payload()
        )
        assert resp.status_code == 201, resp.text

    async def test_sales_agent_forbidden_from_hard_delete(
        self, admin_client: AsyncClient, sales_agent_client: AsyncClient
    ):
        created = await _create_customer_via_api(admin_client)
        resp = await sales_agent_client.delete(f"{CUSTOMERS_PREFIX}/{created['id']}")
        assert resp.status_code == 403, resp.text

    async def test_sales_agent_forbidden_from_soft_delete(
        self, admin_client: AsyncClient, sales_agent_client: AsyncClient
    ):
        created = await _create_customer_via_api(admin_client)
        resp = await sales_agent_client.patch(f"{CUSTOMERS_PREFIX}/{created['id']}/soft-delete")
        assert resp.status_code == 403, resp.text
