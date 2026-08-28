from __future__ import annotations

import itertools
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import AsyncIterator, Optional

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.security import create_access_token
from app.db.base import Base
from app.main import app as fastapi_app
from app.api.deps import get_db
from app.models.booking import Booking, BookingPaymentStatus, BookingStatus
from app.models.customer import Customer
from app.models.property import ListingType, Property
from app.models.user import User, UserRole

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Route / model introspection helpers
#
# These run once at import time. They never modify production code; they
# only read what is already registered/mapped so the tests target the
# real application instead of a hardcoded assumption.
# --------------------------------------------------------------------------
def _discover_bookings_prefix() -> str:
    """
    Scan the real FastAPI app's registered routes for the Booking
    collection endpoint (the route whose path ends in `/bookings`,
    ignoring any trailing slash) and return that exact path. Falls back
    to `/bookings` only if no such route can be found, so import never
    hard-fails.
    """
    candidates = set()
    for route in fastapi_app.routes:
        path = getattr(route, "path", None)
        if not path:
            continue
        normalized = path.rstrip("/")
        if normalized.endswith("/bookings"):
            candidates.add(normalized)

    if not candidates:
        return "/bookings"

    # Prefer the shortest match - that's the collection route itself
    # (e.g. "/api/v1/bookings") rather than a longer, unrelated route
    # that happens to contain "/bookings" as a substring.
    return sorted(candidates, key=len)[0]


BOOKINGS_PREFIX = _discover_bookings_prefix()


def _discover_columns(model) -> set:
    """
    Return the set of column names actually mapped on the real
    SQLAlchemy `model`'s table. Used throughout this module so test
    fixtures only ever set keyword arguments the model genuinely
    defines as a mapped column - never a computed/read-only property
    (e.g. a `full_name` hybrid property backed by separate
    `first_name`/`last_name` columns) and never a guessed name the
    model doesn't actually use.
    """
    try:
        return set(model.__table__.columns.keys())
    except AttributeError:
        return set()


def _discover_password_field() -> Optional[str]:
    """
    Inspect the real `User` table's mapped columns and return whichever
    password/credential column actually exists, trying the common
    naming conventions in order. Returns `None` if the model exposes no
    such column, in which case `_make_user` simply omits it.
    """
    for candidate in (
        "hashed_password",
        "password_hash",
        "password",
        "hashed_pw",
        "pw_hash",
        "hash",
    ):
        if candidate in USER_COLUMNS:
            return candidate
    return None


USER_COLUMNS = _discover_columns(User)
PASSWORD_FIELD = _discover_password_field()
CUSTOMER_COLUMNS = _discover_columns(Customer)
PROPERTY_COLUMNS = _discover_columns(Property)


def _uuid_value_for_column(column_name: str):
    """
    Build a value for a UUID-typed column based on the column's actual
    mapped Python type, rather than assuming either a native
    `uuid.UUID` object or a string is expected.

    Falls back to a string UUID if the column's Python type can't be
    determined (e.g. a dialect-specific type with no `python_type`
    implemented), which is the safer default since most non-native
    UUID columns in this codebase are plain `String`/`VARCHAR`.
    """
    try:
        column = User.__table__.columns[column_name]
        python_type = column.type.python_type
    except (KeyError, AttributeError, NotImplementedError):
        python_type = str

    generated = uuid.uuid4()
    if python_type is uuid.UUID:
        return generated
    return str(generated)


# Seeded well clear of the fixed phone numbers used by the `customer`
# and `second_customer` fixtures ("9840000001" / "9840000002") so
# there is no risk of collision if `users.phone` and `customers.phone`
# ever share a uniqueness scope, and so every `User` created via
# `_make_user` gets a distinct value even within the same test.
_PHONE_COUNTER = itertools.count(9_000_000_001)


async def _unique_test_phone(db_session: AsyncSession) -> str:
    """
    Generate a phone value that is unique in the actual test database.

    The API tests commit their seeded users, so a module-level counter alone
    is not sufficient: the counter resets when pytest starts a new process,
    while committed rows from earlier runs can still exist in the database.
    Check the real `users.phone` column before returning a candidate so the
    fixture remains safe across repeated pytest runs against the same DB.
    """
    while True:
        candidate = str(next(_PHONE_COUNTER))
        existing = await db_session.scalar(
            select(User.id).where(User.phone == candidate).limit(1)
        )
        if existing is None:
            return candidate


# --------------------------------------------------------------------------
# Customer field-mapping helpers
#
# `full_name` raised `AttributeError: can't set attribute` on the real
# model, which is what a read-only hybrid property (no setter) looks
# like from a declarative constructor - meaning the class defines
# `full_name` as computed, not as a mapped column. The real columns
# are discovered instead of guessed, with a first/last-name split as
# fallback if no single "full name" column exists.
# --------------------------------------------------------------------------
def _customer_name_kwargs(full_name: str) -> dict:
    for candidate in ("full_name", "name", "customer_name", "display_name"):
        if candidate in CUSTOMER_COLUMNS:
            return {candidate: full_name}

    first, _, last = full_name.partition(" ")
    kwargs = {}
    for candidate in ("first_name", "given_name"):
        if candidate in CUSTOMER_COLUMNS:
            kwargs[candidate] = first
            break
    for candidate in ("last_name", "surname", "family_name"):
        if candidate in CUSTOMER_COLUMNS:
            kwargs[candidate] = last or first
            break
    return kwargs


def _customer_phone_kwargs(phone: str) -> dict:
    for candidate in ("phone", "phone_number", "mobile", "contact_number"):
        if candidate in CUSTOMER_COLUMNS:
            return {candidate: phone}
    return {}


def _customer_email_kwargs(email: str) -> dict:
    if "email" in CUSTOMER_COLUMNS:
        return {"email": email}
    return {}


def _customer_creator_kwargs(created_by_id: Optional[int]) -> dict:
    """
    Populate the `created_by_id` (and, if present, `updated_by_id`)
    audit columns on `Customer`.

    Unlike the other `_customer_*_kwargs` helpers, this isn't a
    "guess the right column name" problem - `created_by_id` is a NOT
    NULL foreign key into `users.id` on the real schema, so it cannot
    be synthesized as a bare literal the way a name/phone/email string
    can. Callers must supply an actual persisted user id (see the
    `_customer_creator` fixture). If no id is supplied, both columns
    are left unset so callers that don't need this (e.g. tests of the
    column-discovery helpers themselves) aren't forced to thread one
    through.
    """
    kwargs: dict = {}
    if created_by_id is None:
        return kwargs
    if "created_by_id" in CUSTOMER_COLUMNS:
        kwargs["created_by_id"] = created_by_id
    if "updated_by_id" in CUSTOMER_COLUMNS:
        kwargs["updated_by_id"] = created_by_id
    return kwargs


async def _make_customer(
    db_session: AsyncSession,
    full_name: str,
    phone: str,
    email: str,
    created_by_id: Optional[int] = None,
) -> Customer:
    """
    Persist and return a `Customer`, building kwargs strictly from the
    real model's mapped columns (see module-level helpers above) so
    the fixture never assumes a specific schema shape.

    `created_by_id` should be the id of an already-persisted `User`
    when the real schema's `customers.created_by_id` column is NOT
    NULL (see `_customer_creator_kwargs`); omitting it will raise an
    `IntegrityError` on schemas where that constraint applies.
    """
    kwargs: dict = {}
    kwargs.update(_customer_name_kwargs(full_name))
    kwargs.update(_customer_phone_kwargs(phone))
    kwargs.update(_customer_email_kwargs(email))
    kwargs.update(_customer_creator_kwargs(created_by_id))

    obj = Customer(**kwargs)
    db_session.add(obj)
    await db_session.commit()
    await db_session.refresh(obj)
    return obj


# --------------------------------------------------------------------------
# Property field-mapping helpers
#
# `address_line1` raised `TypeError: invalid keyword argument` on the
# real model, meaning the class has no such attribute at all (unlike
# the Customer case, this isn't a read-only property - the name is
# simply wrong). The real columns are discovered instead of guessed.
# --------------------------------------------------------------------------
def _property_title_kwargs(title: str) -> dict:
    for candidate in ("title", "name", "property_name"):
        if candidate in PROPERTY_COLUMNS:
            return {candidate: title}
    return {}


# Monotonic counter backing `_unique_test_property_code`, mirroring the
# `_PHONE_COUNTER` pattern above: every call returns a distinct value,
# satisfying any uniqueness constraint on `properties.property_code`
# deterministically.
_PROPERTY_CODE_COUNTER = itertools.count(1)


def _unique_test_property_code() -> str:
    """
    Generate a unique value for `properties.property_code`.

    This column is NOT NULL on the real schema but has no counterpart
    among a test's existing inputs (title/address/city/price) the way
    e.g. `customers.phone` does - there's no "obvious" value to reuse,
    so one is synthesized instead.
    """
    return f"TESTPROP{next(_PROPERTY_CODE_COUNTER):06d}"


def _property_code_kwargs() -> dict:
    for candidate in ("property_code", "code", "listing_code"):
        if candidate in PROPERTY_COLUMNS:
            return {candidate: _unique_test_property_code()}
    return {}


def _first_enum_value(column):
    """
    Return a valid value for an Enum-typed column.

    Prefers the column's mapped Python `Enum` class (so the ORM
    receives an actual enum member, matching how the rest of the
    codebase constructs these models) and falls back to the raw
    string values declared on the underlying SQL enum type if no
    Python Enum class is mapped - covering both `sqlalchemy.Enum`
    usages and any custom/dialect-specific enum type.

    Returns `None` if neither source yields a usable value, in which
    case the caller simply omits the column (matching the "no
    plausible guess" fallback pattern used elsewhere in this file).
    """
    enum_class = getattr(column.type, "enum_class", None)
    if enum_class is not None:
        return next(iter(enum_class))

    enums = getattr(column.type, "enums", None)
    if enums:
        return enums[0]

    return None


def _property_type_kwargs() -> dict:
    """
    Populate `properties.property_type`, a NOT NULL enum column on
    the real schema with no plausible source value among a test's
    existing inputs (title/address/city/price) - unlike
    `property_code`, this can't be synthesized as a bare string
    without risking a value the enum doesn't actually define, so it's
    read directly off the mapped column's enum type instead (see
    `_first_enum_value`).
    """
    if "property_type" not in PROPERTY_COLUMNS:
        return {}

    value = _first_enum_value(Property.__table__.columns["property_type"])
    if value is None:
        return {}

    return {"property_type": value}


def _property_listing_type_kwargs() -> dict:
    """
    Populate `properties.listing_type`, a NOT NULL enum column
    (`listing_type_enum` in Postgres) on the real schema, backed by
    the concrete `app.models.property.ListingType` enum (`SALE` /
    `RENT`).

    Unlike `property_type` above - where this module has no prior
    knowledge of the enum's members and therefore reads a value
    generically off the mapped column via `_first_enum_value` -
    `ListingType` is already imported directly by this test module, so
    its `SALE` member is used explicitly. This is a real, existing
    value defined on the production model, not an invented one.
    Omitting this column previously caused every fixture using
    `property_obj` to fail at insert time with
    `NotNullViolationError: null value in column "listing_type"`.
    """
    if "listing_type" not in PROPERTY_COLUMNS:
        return {}

    return {"listing_type": ListingType.SALE}


def _property_area_kwargs() -> dict:
    """
    Populate `properties.area_sqft` (or whatever the real schema's
    equivalent column is actually named), a NOT NULL numeric column
    with no plausible source value among a test's existing inputs
    (title/address/city/price) - the same "nothing to guess from"
    situation as `property_code` above, not the enum situation of
    `property_type`/`listing_type` (there's no finite enum to read a
    valid member from here; it's a plain scalar column that just needs
    an arbitrary-but-valid number).

    A fixed, reasonable `Decimal` value is synthesized rather than an
    invented edge case, since no test in this module is exercising
    area-based business rules - the value only needs to satisfy the
    NOT NULL constraint. Omitting this column previously caused every
    fixture using `property_obj` to fail at insert time with
    `NotNullViolationError: null value in column "area_sqft"`.
    """
    for candidate in ("area_sqft", "area", "square_feet", "sqft", "carpet_area"):
        if candidate in PROPERTY_COLUMNS:
            return {candidate: Decimal("1500")}
    return {}


def _property_address_kwargs(address: str) -> dict:
    for candidate in (
        "address_line1",
        "address_line_1",
        "address",
        "street_address",
        "line1",
    ):
        if candidate in PROPERTY_COLUMNS:
            return {candidate: address}
    return {}


def _property_city_kwargs(city: str) -> dict:
    if "city" in PROPERTY_COLUMNS:
        return {"city": city}
    return {}


def _property_state_kwargs() -> dict:
    """
    Populate `properties.state`.

    Confirmed via `app/models/property.py`:

        state: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    This is a plain NOT NULL `String(100)` column - not wrapped in
    `Enum(...)` (unlike `property_type`/`property_status`/
    `listing_type`/`furnishing` on this same model) and not a
    `ForeignKey(...)` (unlike `assigned_agent_id`). It also has no
    `default`/`server_default`, so nothing supplies a value
    automatically. That puts it in the same category as `address`/
    `city` immediately above it in the model: free-text with no closed
    set of valid values, so - exactly like `property_code` and
    `area_sqft` above - a fixed value is synthesized rather than read
    off an enum or requiring a persisted related row.

    `"Tamil Nadu"` is used because every fixture in this module
    already hardcodes `city="Chennai"` (see `property_obj` /
    `second_property`), so this keeps the synthesized value
    geographically consistent with the existing test data rather than
    being an arbitrary unrelated string.

    Omitting this column previously caused every fixture using
    `property_obj` to fail at insert time with
    `NotNullViolationError: null value in column "state"`.
    """
    if "state" not in PROPERTY_COLUMNS:
        return {}
    return {"state": "Tamil Nadu"}


def _property_pincode_kwargs() -> dict:
    """
    Populate `properties.pincode`, a NOT NULL plain `String(10)`
    column on the real schema (see `Property.pincode` in
    `app/models/property.py`) - not an enum, not a foreign key, just
    free text with no server_default, the same situation as `state`
    above. A fixed 6-digit value consistent with the `city="Chennai"`
    / `state="Tamil Nadu"` values already hardcoded in this module's
    fixtures is synthesized rather than left unset. Omitting this
    column previously caused every fixture using `property_obj` to
    fail at insert time with `NotNullViolationError: null value in
    column "pincode"`.
    """
    if "pincode" not in PROPERTY_COLUMNS:
        return {}
    return {"pincode": "600001"}


def _property_owner_kwargs() -> dict:
    """
    Populate `properties.owner_name` and `properties.owner_phone`, two
    further NOT NULL plain `String` columns on the real schema (see
    `Property.owner_name` - `String(150), nullable=False` - and
    `Property.owner_phone` - `String(20), nullable=False` - in
    `app/models/property.py`). Neither is an enum or a foreign key,
    and neither has a default/server_default - the same "free text,
    nothing to guess from among title/address/city/price" situation as
    `state`/`pincode` above. (`owner_email` is deliberately excluded
    here: it's `Mapped[str | None]` / `nullable=True` on the real
    model, so it needs no synthesized value.)

    Fixed values are used since no test in this module exercises
    owner-identity business rules - they only need to satisfy the NOT
    NULL constraints. Omitting these columns previously caused every
    fixture using `property_obj` to fail at insert time with
    `NotNullViolationError: null value in column "owner_name"`.
    """
    kwargs: dict = {}
    if "owner_name" in PROPERTY_COLUMNS:
        kwargs["owner_name"] = "Test Property Owner"
    if "owner_phone" in PROPERTY_COLUMNS:
        kwargs["owner_phone"] = "9800000000"
    return kwargs


def _property_price_kwargs(price: Decimal) -> dict:
    for candidate in ("price", "amount", "listed_price", "value"):
        if candidate in PROPERTY_COLUMNS:
            return {candidate: price}
    return {}


async def _make_property(
    db_session: AsyncSession, title: str, address: str, city: str, price: Decimal
) -> Property:
    """
    Persist and return a `Property`, building kwargs strictly from the
    real model's mapped columns (see module-level helpers above) so
    the fixture never assumes a specific schema shape.
    """
    kwargs: dict = {}
    kwargs.update(_property_title_kwargs(title))
    kwargs.update(_property_code_kwargs())
    kwargs.update(_property_type_kwargs())
    kwargs.update(_property_listing_type_kwargs())
    kwargs.update(_property_area_kwargs())
    kwargs.update(_property_address_kwargs(address))
    kwargs.update(_property_city_kwargs(city))
    kwargs.update(_property_state_kwargs())
    kwargs.update(_property_pincode_kwargs())
    kwargs.update(_property_owner_kwargs())
    kwargs.update(_property_price_kwargs(price))

    obj = Property(**kwargs)
    db_session.add(obj)
    await db_session.commit()
    await db_session.refresh(obj)
    return obj


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """
    Provide a function-scoped `AsyncSession` bound to a single connection
    and a single outer transaction. The transaction is rolled back after
    each test so no test data is ever persisted, and the schema is never
    created, dropped, or otherwise modified by this fixture.
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
    """
    Yield the FastAPI application with `get_db` overridden to serve the
    per-test transactional session, restoring the original dependency
    once the test completes.
    """

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


async def _make_user(db_session: AsyncSession, role: UserRole, email: str) -> User:
    """
    Persist and return a `User` with the given role for auth tests.

    Only fields that genuinely exist on the real `User` model are set.
    The password/credential column name is resolved dynamically via
    `PASSWORD_FIELD` (see `_discover_password_field`) instead of being
    hardcoded, since the real model may not expose `hashed_password`.

    The model's `uuid` column is NOT NULL on the real schema, so a
    value is always supplied when that column exists. Its type is
    resolved dynamically via `_uuid_value_for_column` rather than
    assumed, so a native `uuid.UUID` is only used when the column is
    actually mapped as one; otherwise a string form is used.

    The model's `phone` column is likewise NOT NULL on the real
    schema, so a unique value is supplied via `_unique_test_phone`
    whenever that column exists, avoiding both the not-null violation
    and any collision if `phone` also carries a uniqueness constraint.
    """
    kwargs = {
        "email": email,
        "full_name": f"{role.value.title()} Test User",
        "role": role,
        "is_active": True,
    }
    if PASSWORD_FIELD:
        kwargs[PASSWORD_FIELD] = "not-a-real-hash-$2b$12$test.value.only"

    if "uuid" in USER_COLUMNS:
        kwargs["uuid"] = _uuid_value_for_column("uuid")

    if "phone" in USER_COLUMNS:
        kwargs["phone"] = await _unique_test_phone(db_session)

    if "is_verified" in USER_COLUMNS:
        kwargs["is_verified"] = True

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
    return await _make_user(db_session, UserRole.ADMIN, "admin@boookingtest.io")


@pytest_asyncio.fixture
async def sales_manager_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, UserRole.SALES_MANAGER, "manager@boookingtest.io")


@pytest_asyncio.fixture
async def sales_agent_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, UserRole.SALES_AGENT, "agent@boookingtest.io")


@pytest_asyncio.fixture
async def admin_client(app, admin_user: User) -> AsyncIterator[AsyncClient]:
    async with _bearer_client(app, admin_user) as ac:
        yield ac


@pytest_asyncio.fixture
async def sales_manager_client(app, sales_manager_user: User) -> AsyncIterator[AsyncClient]:
    async with _bearer_client(app, sales_manager_user) as ac:
        yield ac


@pytest_asyncio.fixture
async def sales_agent_client(app, sales_agent_user: User) -> AsyncIterator[AsyncClient]:
    async with _bearer_client(app, sales_agent_user) as ac:
        yield ac


@pytest_asyncio.fixture
async def _customer_creator(db_session: AsyncSession) -> User:
    """
    Throwaway ADMIN user that exists solely to satisfy
    `customers.created_by_id`, a NOT NULL foreign key into `users.id`
    on the real schema (see `_customer_creator_kwargs`).

    Deliberately a separate fixture/row from `admin_user` rather than
    reusing it: `admin_user` represents "the actor driving the test's
    HTTP requests" and is scoped to whichever role-specific test needs
    it, while this fixture represents an unrelated, incidental "who
    created this seed row" value. Conflating the two would make tests
    that assert on `admin_user`'s identity fragile to changes in how
    customer fixtures are seeded.
    """
    return await _make_user(db_session, UserRole.ADMIN, "system.creator@boookingtest.io")


@pytest_asyncio.fixture
async def customer(db_session: AsyncSession, _customer_creator: User) -> Customer:
    return await _make_customer(
        db_session,
        "Asha Rao",
        "9840000001",
        "asha@example.com",
        created_by_id=_customer_creator.id,
    )


@pytest_asyncio.fixture
async def second_customer(db_session: AsyncSession, _customer_creator: User) -> Customer:
    return await _make_customer(
        db_session,
        "Vikram Iyer",
        "9840000002",
        "vikram@example.com",
        created_by_id=_customer_creator.id,
    )


@pytest_asyncio.fixture
async def property_obj(db_session: AsyncSession) -> Property:
    return await _make_property(
        db_session, "Lakeview 3BHK", "12 Lake Road", "Chennai", Decimal("7500000")
    )


@pytest_asyncio.fixture
async def second_property(db_session: AsyncSession) -> Property:
    return await _make_property(
        db_session, "Hillcrest 2BHK", "4 Hill Street", "Chennai", Decimal("5200000")
    )


def _booking_payload(customer_id: uuid.UUID, property_id: int, **overrides) -> dict:
    payload = {
        "customer_id": str(customer_id),
        "property_id": property_id,
        "booking_amount": 7500000,
        "token_amount": 500000,
        "payment_mode": "UPI",
        "payment_reference": "UTR2026073112345",
        "status": "PENDING",
        "payment_status": "PENDING",
        "remarks": "Initial booking via test suite.",
    }
    payload.update(overrides)
    return payload


async def _create_booking_via_api(
    admin_client: AsyncClient, customer_id: uuid.UUID, property_id: int, **overrides
) -> dict:
    resp = await admin_client.post(
        BOOKINGS_PREFIX, json=_booking_payload(customer_id, property_id, **overrides)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------
# Authentication (401)
# --------------------------------------------------------------------------
async def test_list_bookings_without_token_returns_401(client: AsyncClient) -> None:
    resp = await client.get(BOOKINGS_PREFIX)
    assert resp.status_code == 401


async def test_create_booking_with_malformed_token_returns_401(client: AsyncClient) -> None:
    resp = await client.post(
        BOOKINGS_PREFIX,
        json={},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# Create - Success
# --------------------------------------------------------------------------
async def test_create_booking_success(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    resp = await admin_client.post(
        BOOKINGS_PREFIX, json=_booking_payload(customer.id, property_obj.id)
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["customer_id"] == str(customer.id)
    assert body["property_id"] == property_obj.id
    assert body["status"] == "PENDING"
    assert body["payment_status"] == "PENDING"
    assert Decimal(str(body["booking_amount"])) == Decimal("7500000")
    assert uuid.UUID(body["id"])


async def test_sales_agent_can_create_booking(
    sales_agent_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    resp = await sales_agent_client.post(
        BOOKINGS_PREFIX, json=_booking_payload(customer.id, property_obj.id)
    )
    assert resp.status_code == 201, resp.text


# --------------------------------------------------------------------------
# Create - Validation Errors (422)
# --------------------------------------------------------------------------
async def test_create_booking_missing_required_fields_returns_422(
    admin_client: AsyncClient,
) -> None:
    resp = await admin_client.post(BOOKINGS_PREFIX, json={})
    assert resp.status_code == 422


async def test_create_booking_negative_booking_amount_returns_422(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    resp = await admin_client.post(
        BOOKINGS_PREFIX,
        json=_booking_payload(customer.id, property_obj.id, booking_amount=-100),
    )
    assert resp.status_code == 422


async def test_create_booking_token_amount_exceeds_booking_amount_returns_422(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    resp = await admin_client.post(
        BOOKINGS_PREFIX,
        json=_booking_payload(
            customer.id, property_obj.id, booking_amount=100000, token_amount=200000
        ),
    )
    assert resp.status_code == 422


async def test_create_booking_invalid_status_enum_returns_422(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    resp = await admin_client.post(
        BOOKINGS_PREFIX,
        json=_booking_payload(customer.id, property_obj.id, status="NOT_A_STATUS"),
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Create - Not Found (404)
# --------------------------------------------------------------------------
async def test_create_booking_unknown_customer_returns_404(
    admin_client: AsyncClient, property_obj: Property
) -> None:
    resp = await admin_client.post(
        BOOKINGS_PREFIX, json=_booking_payload(uuid.uuid4(), property_obj.id)
    )
    assert resp.status_code == 404


async def test_create_booking_unknown_property_returns_404(
    admin_client: AsyncClient, customer: Customer
) -> None:
    resp = await admin_client.post(
        BOOKINGS_PREFIX, json=_booking_payload(customer.id, 999999)
    )
    assert resp.status_code == 404


async def test_create_booking_unknown_agent_returns_404(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    resp = await admin_client.post(
        BOOKINGS_PREFIX,
        json=_booking_payload(customer.id, property_obj.id, agent_id=999999),
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Create - Conflict (409)
# --------------------------------------------------------------------------
async def test_create_duplicate_active_booking_returns_409(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    resp = await admin_client.post(
        BOOKINGS_PREFIX, json=_booking_payload(customer.id, property_obj.id)
    )
    assert resp.status_code == 409


# --------------------------------------------------------------------------
# Retrieve Single
# --------------------------------------------------------------------------
async def test_get_booking_by_id_success(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    resp = await admin_client.get(f"{BOOKINGS_PREFIX}/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


async def test_get_booking_not_found_returns_404(admin_client: AsyncClient) -> None:
    resp = await admin_client.get(f"{BOOKINGS_PREFIX}/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_get_booking_invalid_uuid_returns_422(admin_client: AsyncClient) -> None:
    resp = await admin_client.get(f"{BOOKINGS_PREFIX}/not-a-uuid")
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# List / Filter / Pagination
# --------------------------------------------------------------------------
async def test_list_bookings_returns_paginated_results(
    admin_client: AsyncClient,
    customer: Customer,
    second_customer: Customer,
    property_obj: Property,
    second_property: Property,
) -> None:
    await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    await _create_booking_via_api(admin_client, second_customer.id, second_property.id)

    resp = await admin_client.get(BOOKINGS_PREFIX, params={"page": 1, "page_size": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 1
    assert body["total_pages"] == 2


async def test_list_bookings_filter_by_status(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    await admin_client.patch(
        f"{BOOKINGS_PREFIX}/{created['id']}/status", json={"status": "CONFIRMED"}
    )

    resp = await admin_client.get(BOOKINGS_PREFIX, params={"status": "CONFIRMED"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "CONFIRMED"


async def test_list_bookings_invalid_sort_by_returns_422(admin_client: AsyncClient) -> None:
    resp = await admin_client.get(
        BOOKINGS_PREFIX, params={"sort_by": "id; DROP TABLE bookings"}
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------
async def test_search_bookings_by_remarks(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    await _create_booking_via_api(
        admin_client, customer.id, property_obj.id, remarks="Needs a balcony-facing unit"
    )
    resp = await admin_client.get(f"{BOOKINGS_PREFIX}/search", params={"q": "balcony"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


async def test_search_bookings_blank_term_returns_422(admin_client: AsyncClient) -> None:
    resp = await admin_client.get(f"{BOOKINGS_PREFIX}/search", params={"q": " "})
    assert resp.status_code == 422


async def test_search_bookings_missing_term_returns_422(admin_client: AsyncClient) -> None:
    resp = await admin_client.get(f"{BOOKINGS_PREFIX}/search")
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Update
# --------------------------------------------------------------------------
async def test_update_booking_success(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    resp = await admin_client.put(
        f"{BOOKINGS_PREFIX}/{created['id']}", json={"remarks": "Updated remarks."}
    )
    assert resp.status_code == 200
    assert resp.json()["remarks"] == "Updated remarks."


async def test_update_booking_not_found_returns_404(admin_client: AsyncClient) -> None:
    resp = await admin_client.put(
        f"{BOOKINGS_PREFIX}/{uuid.uuid4()}", json={"remarks": "irrelevant"}
    )
    assert resp.status_code == 404


async def test_update_booking_invalid_amounts_returns_400(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    created = await _create_booking_via_api(
        admin_client, customer.id, property_obj.id, booking_amount=1000000, token_amount=100000
    )
    resp = await admin_client.put(
        f"{BOOKINGS_PREFIX}/{created['id']}", json={"token_amount": 5000000}
    )
    assert resp.status_code == 400


async def test_update_inactive_booking_returns_409(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    await admin_client.delete(f"{BOOKINGS_PREFIX}/{created['id']}")

    resp = await admin_client.put(
        f"{BOOKINGS_PREFIX}/{created['id']}", json={"remarks": "should fail"}
    )
    assert resp.status_code == 409


async def test_update_booking_unknown_agent_returns_404(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    resp = await admin_client.put(
        f"{BOOKINGS_PREFIX}/{created['id']}", json={"agent_id": 999999}
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Soft Delete
# --------------------------------------------------------------------------
async def test_soft_delete_booking_success(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    resp = await admin_client.delete(f"{BOOKINGS_PREFIX}/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


async def test_soft_delete_already_inactive_booking_returns_409(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    await admin_client.delete(f"{BOOKINGS_PREFIX}/{created['id']}")
    resp = await admin_client.delete(f"{BOOKINGS_PREFIX}/{created['id']}")
    assert resp.status_code == 409


async def test_soft_delete_unknown_booking_returns_404(admin_client: AsyncClient) -> None:
    resp = await admin_client.delete(f"{BOOKINGS_PREFIX}/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_soft_deleted_booking_frees_customer_property_pair(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    await admin_client.delete(f"{BOOKINGS_PREFIX}/{created['id']}")

    resp = await admin_client.post(
        BOOKINGS_PREFIX, json=_booking_payload(customer.id, property_obj.id)
    )
    assert resp.status_code == 201, resp.text


# --------------------------------------------------------------------------
# Status Transitions
# --------------------------------------------------------------------------
async def test_change_status_valid_transition_succeeds(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    resp = await admin_client.patch(
        f"{BOOKINGS_PREFIX}/{created['id']}/status", json={"status": "CONFIRMED"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "CONFIRMED"


async def test_change_status_invalid_transition_returns_400(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    resp = await admin_client.patch(
        f"{BOOKINGS_PREFIX}/{created['id']}/status", json={"status": "COMPLETED"}
    )
    assert resp.status_code == 400


async def test_change_status_from_terminal_state_returns_400(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    await admin_client.patch(
        f"{BOOKINGS_PREFIX}/{created['id']}/status", json={"status": "CONFIRMED"}
    )
    await admin_client.patch(
        f"{BOOKINGS_PREFIX}/{created['id']}/status", json={"status": "COMPLETED"}
    )

    resp = await admin_client.patch(
        f"{BOOKINGS_PREFIX}/{created['id']}/status", json={"status": "CONFIRMED"}
    )
    assert resp.status_code == 400


async def test_change_status_on_inactive_booking_returns_409(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    await admin_client.delete(f"{BOOKINGS_PREFIX}/{created['id']}")

    resp = await admin_client.patch(
        f"{BOOKINGS_PREFIX}/{created['id']}/status", json={"status": "CONFIRMED"}
    )
    assert resp.status_code == 409


async def test_change_status_unknown_booking_returns_404(admin_client: AsyncClient) -> None:
    resp = await admin_client.patch(
        f"{BOOKINGS_PREFIX}/{uuid.uuid4()}/status", json={"status": "CONFIRMED"}
    )
    assert resp.status_code == 404


async def test_change_status_missing_body_returns_422(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    resp = await admin_client.patch(f"{BOOKINGS_PREFIX}/{created['id']}/status", json={})
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Payment Status Transitions
# --------------------------------------------------------------------------
async def test_change_payment_status_valid_transition_succeeds(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    resp = await admin_client.patch(
        f"{BOOKINGS_PREFIX}/{created['id']}/payment-status",
        json={"payment_status": "PARTIALLY_PAID"},
    )
    assert resp.status_code == 200
    assert resp.json()["payment_status"] == "PARTIALLY_PAID"


async def test_change_payment_status_invalid_transition_returns_400(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    await admin_client.patch(
        f"{BOOKINGS_PREFIX}/{created['id']}/payment-status", json={"payment_status": "PAID"}
    )
    resp = await admin_client.patch(
        f"{BOOKINGS_PREFIX}/{created['id']}/payment-status", json={"payment_status": "PENDING"}
    )
    assert resp.status_code == 400


async def test_change_payment_status_from_refunded_returns_400(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    await admin_client.patch(
        f"{BOOKINGS_PREFIX}/{created['id']}/payment-status", json={"payment_status": "PAID"}
    )
    await admin_client.patch(
        f"{BOOKINGS_PREFIX}/{created['id']}/payment-status", json={"payment_status": "REFUNDED"}
    )
    resp = await admin_client.patch(
        f"{BOOKINGS_PREFIX}/{created['id']}/payment-status", json={"payment_status": "PAID"}
    )
    assert resp.status_code == 400


async def test_change_payment_status_on_inactive_booking_returns_409(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    await admin_client.delete(f"{BOOKINGS_PREFIX}/{created['id']}")
    resp = await admin_client.patch(
        f"{BOOKINGS_PREFIX}/{created['id']}/payment-status",
        json={"payment_status": "PARTIALLY_PAID"},
    )
    assert resp.status_code == 409


async def test_change_payment_status_unknown_booking_returns_404(
    admin_client: AsyncClient,
) -> None:
    resp = await admin_client.patch(
        f"{BOOKINGS_PREFIX}/{uuid.uuid4()}/payment-status", json={"payment_status": "PAID"}
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Agent Assignment
# --------------------------------------------------------------------------
async def test_assign_agent_success(
    admin_client: AsyncClient,
    customer: Customer,
    property_obj: Property,
    sales_agent_user: User,
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    resp = await admin_client.patch(
        f"{BOOKINGS_PREFIX}/{created['id']}/assign-agent",
        json={"agent_id": sales_agent_user.id},
    )
    assert resp.status_code == 200
    assert resp.json()["agent_id"] == sales_agent_user.id


async def test_assign_agent_unknown_agent_returns_404(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    resp = await admin_client.patch(
        f"{BOOKINGS_PREFIX}/{created['id']}/assign-agent", json={"agent_id": 999999}
    )
    assert resp.status_code == 404


async def test_assign_agent_on_inactive_booking_returns_409(
    admin_client: AsyncClient,
    customer: Customer,
    property_obj: Property,
    sales_agent_user: User,
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    await admin_client.delete(f"{BOOKINGS_PREFIX}/{created['id']}")
    resp = await admin_client.patch(
        f"{BOOKINGS_PREFIX}/{created['id']}/assign-agent",
        json={"agent_id": sales_agent_user.id},
    )
    assert resp.status_code == 409


async def test_assign_agent_unknown_booking_returns_404(
    admin_client: AsyncClient, sales_agent_user: User
) -> None:
    resp = await admin_client.patch(
        f"{BOOKINGS_PREFIX}/{uuid.uuid4()}/assign-agent",
        json={"agent_id": sales_agent_user.id},
    )
    assert resp.status_code == 404


async def test_assign_agent_missing_body_returns_422(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    resp = await admin_client.patch(f"{BOOKINGS_PREFIX}/{created['id']}/assign-agent", json={})
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Dashboard Summary / Follow-ups
# --------------------------------------------------------------------------
async def test_dashboard_summary_reflects_active_bookings(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    resp = await admin_client.get(f"{BOOKINGS_PREFIX}/dashboard/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_active_bookings"] >= 1
    assert body["status_breakdown"]["PENDING"] >= 1
    assert Decimal(str(body["total_booking_value"])) >= Decimal("7500000")


async def test_todays_followups_returns_due_bookings(
    admin_client: AsyncClient, customer: Customer, property_obj: Property
) -> None:
    created = await _create_booking_via_api(admin_client, customer.id, property_obj.id)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    await admin_client.put(
        f"{BOOKINGS_PREFIX}/{created['id']}", json={"next_follow_up": yesterday}
    )

    resp = await admin_client.get(f"{BOOKINGS_PREFIX}/followups/today")
    assert resp.status_code == 200
    ids = [item["id"] for item in resp.json()]
    assert created["id"] in ids


async def test_dashboard_route_is_not_shadowed_by_booking_id_route(
    admin_client: AsyncClient,
) -> None:
    """
    Regression guard: `/dashboard/summary` and `/followups/today` must
    resolve to their dedicated handlers, not attempt UUID parsing via
    `/{booking_id}` (which would 422 on a non-UUID path segment).
    """
    resp = await admin_client.get(f"{BOOKINGS_PREFIX}/dashboard/summary")
    assert resp.status_code == 200
    resp = await admin_client.get(f"{BOOKINGS_PREFIX}/followups/today")
    assert resp.status_code == 200