# backend/tests/test_settings_api.py

"""
Settings Module - Phase 4
HTTP-level integration tests for the Settings FastAPI router
(`app/api/v1/settings.py`).

Scope:
    - Exercises the full stack (router -> service -> repository -> DB)
      against a real, isolated test database using an async HTTP
      client, mirroring the conventions established in
      `test_audit_api.py` / `test_booking_api.py`.
    - Verifies authentication (401), authorization/RBAC (403), request
      validation (422), business-rule violations (400/409/422), and
      not-found handling (404) in addition to the happy paths
      (200/201/204).

Endpoints covered:
    POST   /settings
    GET    /settings
    GET    /settings/statistics
    GET    /settings/public
    GET    /settings/category/{category}
    POST   /settings/cache/reload
    GET    /settings/cache/status
    POST   /settings/bulk-update
    DELETE /settings/bulk-delete
    GET    /settings/{setting_id}
    PUT    /settings/{setting_id}
    PATCH  /settings/{setting_id}
    DELETE /settings/{setting_id}
"""

from __future__ import annotations

import uuid
from typing import AsyncIterator
from urllib.parse import quote_plus

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.security import create_access_token
from app.db.base import Base
from app.db.session import get_db
from app.main import app as fastapi_app
from app.models.settings import SettingCategory, SettingDataType, Settings
from app.models.user import User, UserRole
from app.utils import settings_cache

pytestmark = pytest.mark.asyncio(loop_scope="function")

BASE_URL = f"{settings.API_V1_PREFIX}/settings"


def _default_test_database_url() -> str:
    """
    Build the fallback test-DB DSN from the same POSTGRES_* credentials
    that DATABASE_URL is built from (see app/core/config.py), instead of
    a hardcoded password. This keeps it in sync with the real local
    Postgres credentials and URL-encodes the password so special
    characters (e.g. '@') don't break the DSN.
    """
    password = settings.POSTGRES_PASSWORD
    if isinstance(password, SecretStr):
        password = password.get_secret_value()
    return (
        f"postgresql+asyncpg://{quote_plus(settings.POSTGRES_USER)}:"
        f"{quote_plus(password)}@{settings.POSTGRES_SERVER}:"
        f"{settings.POSTGRES_PORT}/test_settings_api_db"
    )


TEST_DATABASE_URL = getattr(settings, "TEST_DATABASE_URL", None) or _default_test_database_url()


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------
# Fixtures
#
# All async fixtures below (and the module's tests, via `pytestmark`
# above) are pinned to loop_scope="session" so that everything -
# the session-scoped async_engine/connection pool, every db_session,
# and every test coroutine - runs on the SAME event loop for the whole
# module. Without this, pytest-asyncio's default per-test event loop
# would spin up a new loop per test function while the DB connections
# opened via the session-scoped async_engine stay bound to whichever
# loop first created them, producing
# "RuntimeError: Task ... got Future ... attached to a different loop"
# and "InterfaceError: cannot perform operation: another operation is
# in progress" the moment more than one test touches the DB.
#
# NOTE: previously this file defined its own session-scoped
# `event_loop` fixture to try to force this. That pattern is
# deprecated in modern pytest-asyncio (it emits a DeprecationWarning
# and is not honored reliably by the plugin's own test-loop
# machinery) - `loop_scope="session"` on the mark/fixtures is the
# supported replacement, so the old `event_loop` fixture has been
# removed entirely.
# --------------------------------------------------------------------------
@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def async_engine():
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="function")
async def db_session(async_engine) -> AsyncSession:
    connection = await async_engine.connect()
    transaction = await connection.begin()
    session_factory = async_sessionmaker(
        bind=connection, expire_on_commit=False, class_=AsyncSession
    )
    session = session_factory()

    yield session

    await session.close()
    await transaction.rollback()
    await connection.close()


@pytest_asyncio.fixture(loop_scope="function", autouse=True)
async def _clear_settings_cache():
    # The settings cache is a process-local module-level store; clear it
    # before and after every test so cache state never leaks between tests.
    await settings_cache.clear_cache()
    yield
    await settings_cache.clear_cache()


@pytest_asyncio.fixture(loop_scope="function")
async def app(db_session: AsyncSession):
    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    fastapi_app.dependency_overrides[get_db] = _override_get_db
    yield fastapi_app
    fastapi_app.dependency_overrides.pop(get_db, None)


@pytest_asyncio.fixture(loop_scope="function")
async def client(app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _make_user(db_session: AsyncSession, role: UserRole, email: str) -> User:
    unique_suffix = uuid.uuid4().hex[:10]
    user = User(
        uuid=str(uuid.uuid4()),
        email=email,
        phone=f"+1555{unique_suffix[:7]}",
        password_hash="not-a-real-hash",
        full_name=f"{role.value.title()} Test User",
        role=role,
        is_active=True,
    )
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


@pytest_asyncio.fixture(loop_scope="function")
async def admin_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, UserRole.ADMIN, "admin@settingstest.io")


@pytest_asyncio.fixture(loop_scope="function")
async def sales_manager_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, UserRole.SALES_MANAGER, "manager@settingstest.io")


@pytest_asyncio.fixture(loop_scope="function")
async def sales_agent_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, UserRole.SALES_AGENT, "agent@settingstest.io")


@pytest_asyncio.fixture(loop_scope="function")
async def admin_client(app, admin_user: User) -> AsyncIterator[AsyncClient]:
    async with _bearer_client(app, admin_user) as ac:
        yield ac


@pytest_asyncio.fixture(loop_scope="function")
async def sales_agent_client(app, sales_agent_user: User) -> AsyncIterator[AsyncClient]:
    async with _bearer_client(app, sales_agent_user) as ac:
        yield ac


@pytest_asyncio.fixture(loop_scope="function")
async def sales_manager_client(app, sales_manager_user: User) -> AsyncIterator[AsyncClient]:
    async with _bearer_client(app, sales_manager_user) as ac:
        yield ac


async def _create_setting(
    db_session: AsyncSession,
    *,
    category: SettingCategory = SettingCategory.EMAIL,
    setting_key: str = "SMTP_HOST",
    setting_value=None,
    description: str = "SMTP server hostname.",
    data_type: SettingDataType = SettingDataType.STRING,
    is_public: bool = False,
    is_editable: bool = True,
    is_encrypted: bool = False,
    validation_rules=None,
    created_by: int | None = None,
) -> Settings:
    entry = Settings(
        category=category,
        setting_key=setting_key,
        setting_value="smtp.example.com" if setting_value is None else setting_value,
        description=description,
        data_type=data_type,
        is_public=is_public,
        is_editable=is_editable,
        is_encrypted=is_encrypted,
        validation_rules=validation_rules,
        created_by=created_by,
        updated_by=created_by,
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)
    return entry


@pytest_asyncio.fixture(loop_scope="function")
async def setting_obj(db_session: AsyncSession) -> Settings:
    return await _create_setting(db_session)


@pytest_asyncio.fixture(loop_scope="function")
async def public_setting_obj(db_session: AsyncSession) -> Settings:
    return await _create_setting(
        db_session,
        category=SettingCategory.THEME,
        setting_key="PRIMARY_COLOR",
        setting_value="#123456",
        is_public=True,
    )


@pytest_asyncio.fixture(loop_scope="function")
async def non_editable_setting_obj(db_session: AsyncSession) -> Settings:
    return await _create_setting(
        db_session,
        category=SettingCategory.GENERAL,
        setting_key="LOCKED_SETTING",
        setting_value="locked",
        is_editable=False,
    )


@pytest_asyncio.fixture(loop_scope="function")
async def protected_setting_obj(db_session: AsyncSession) -> Settings:
    return await _create_setting(
        db_session,
        category=SettingCategory.SYSTEM,
        setting_key="SYSTEM_VERSION",
        setting_value="1.0.0",
    )


# --------------------------------------------------------------------------
# Authentication (401)
# --------------------------------------------------------------------------
class TestSettingsAuthentication:

    async def test_list_settings_without_token_returns_401(self, client: AsyncClient):
        response = await client.get(BASE_URL)
        assert response.status_code == 401

    async def test_get_setting_without_token_returns_401(self, client: AsyncClient):
        response = await client.get(f"{BASE_URL}/{uuid.uuid4()}")
        assert response.status_code == 401

    async def test_invalid_token_returns_401(self, client: AsyncClient):
        response = await client.get(BASE_URL, headers=auth_headers("invalid.token.value"))
        assert response.status_code == 401

    async def test_create_setting_without_token_returns_401(self, client: AsyncClient):
        response = await client.post(BASE_URL, json={})
        assert response.status_code == 401

    async def test_delete_setting_without_token_returns_401(self, client: AsyncClient):
        response = await client.delete(f"{BASE_URL}/{uuid.uuid4()}")
        assert response.status_code == 401


# --------------------------------------------------------------------------
# Authorization / RBAC (403)
# --------------------------------------------------------------------------
class TestSettingsAuthorization:

    async def test_sales_agent_cannot_create_setting(self, sales_agent_client: AsyncClient):
        response = await sales_agent_client.post(
            BASE_URL,
            json={
                "category": "GENERAL",
                "setting_key": "SHOULD_FAIL",
                "setting_value": "x",
                "data_type": "STRING",
            },
        )
        assert response.status_code == 403

    async def test_sales_agent_cannot_update_setting(
        self, sales_agent_client: AsyncClient, setting_obj: Settings
    ):
        response = await sales_agent_client.put(
            f"{BASE_URL}/{setting_obj.id}", json={"description": "hacked"}
        )
        assert response.status_code == 403

    async def test_sales_agent_cannot_delete_setting(
        self, sales_agent_client: AsyncClient, setting_obj: Settings
    ):
        response = await sales_agent_client.delete(f"{BASE_URL}/{setting_obj.id}")
        assert response.status_code == 403

    async def test_sales_agent_cannot_reload_cache(self, sales_agent_client: AsyncClient):
        response = await sales_agent_client.post(f"{BASE_URL}/cache/reload")
        assert response.status_code == 403

    async def test_sales_agent_cannot_bulk_update(self, sales_agent_client: AsyncClient):
        response = await sales_agent_client.post(
            f"{BASE_URL}/bulk-update",
            json={"updates": [{"setting_id": str(uuid.uuid4()), "payload": {}}]},
        )
        assert response.status_code == 403

    async def test_sales_agent_cannot_bulk_delete(self, sales_agent_client: AsyncClient):
        response = await sales_agent_client.request(
            "DELETE", f"{BASE_URL}/bulk-delete", json={"ids": [str(uuid.uuid4())]}
        )
        assert response.status_code == 403

    async def test_sales_manager_can_view_settings(self, sales_manager_client: AsyncClient):
        response = await sales_manager_client.get(BASE_URL)
        assert response.status_code == 200

    async def test_admin_can_create_setting(self, admin_client: AsyncClient):
        response = await admin_client.post(
            BASE_URL,
            json={
                "category": "GENERAL",
                "setting_key": "ADMIN_CREATED_KEY",
                "setting_value": "x",
                "data_type": "STRING",
            },
        )
        assert response.status_code == 201


# --------------------------------------------------------------------------
# POST /settings
# --------------------------------------------------------------------------
class TestCreateSettingAPI:

    async def test_create_setting_201(
        self, admin_client: AsyncClient, admin_user: User
    ):
        response = await admin_client.post(
            BASE_URL,
            json={
                "category": "EMAIL",
                "setting_key": "SMTP_PORT",
                "setting_value": 587,
                "description": "SMTP port.",
                "data_type": "INTEGER",
                "is_public": False,
                "is_editable": True,
                "is_encrypted": False,
                "created_by": admin_user.id,
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["setting_key"] == "SMTP_PORT"
        assert body["category"] == "EMAIL"
        assert "id" in body

    async def test_create_setting_duplicate_409(
        self, admin_client: AsyncClient, setting_obj: Settings
    ):
        response = await admin_client.post(
            BASE_URL,
            json={
                "category": setting_obj.category.value,
                "setting_key": setting_obj.setting_key,
                "setting_value": "irrelevant",
                "data_type": "STRING",
            },
        )
        assert response.status_code == 409

    async def test_create_setting_encrypted_and_public_422(self, admin_client: AsyncClient):
        response = await admin_client.post(
            BASE_URL,
            json={
                "category": "SYSTEM",
                "setting_key": "BAD_FLAG_COMBO",
                "setting_value": "x",
                "data_type": "STRING",
                "is_encrypted": True,
                "is_public": True,
            },
        )
        assert response.status_code == 422

    async def test_create_setting_invalid_key_format_400(self, admin_client: AsyncClient):
        response = await admin_client.post(
            BASE_URL,
            json={
                "category": "GENERAL",
                "setting_key": "1_bad_key",
                "setting_value": "x",
                "data_type": "STRING",
            },
        )
        assert response.status_code == 400

    async def test_create_setting_value_type_mismatch_400(self, admin_client: AsyncClient):
        response = await admin_client.post(
            BASE_URL,
            json={
                "category": "GENERAL",
                "setting_key": "BAD_INT_VALUE",
                "setting_value": "not-an-integer",
                "data_type": "INTEGER",
            },
        )
        assert response.status_code == 400

    async def test_create_setting_missing_required_field_422(self, admin_client: AsyncClient):
        response = await admin_client.post(BASE_URL, json={"setting_value": "x"})
        assert response.status_code == 422


# --------------------------------------------------------------------------
# GET /settings
# --------------------------------------------------------------------------
class TestListSettingsAPI:

    async def test_list_settings_200(self, admin_client: AsyncClient, setting_obj: Settings):
        response = await admin_client.get(BASE_URL)
        assert response.status_code == 200
        body = response.json()
        assert "items" in body
        assert "total" in body
        assert body["total"] >= 1

    async def test_list_settings_pagination(self, admin_client: AsyncClient):
        response = await admin_client.get(f"{BASE_URL}?page=1&page_size=5")
        assert response.status_code == 200
        body = response.json()
        assert body["page"] == 1
        assert body["page_size"] == 5

    async def test_list_settings_filter_by_category(
        self, admin_client: AsyncClient, setting_obj: Settings
    ):
        response = await admin_client.get(f"{BASE_URL}?category=EMAIL")
        assert response.status_code == 200
        body = response.json()
        assert all(item["category"] == "EMAIL" for item in body["items"])

    async def test_list_settings_search(self, admin_client: AsyncClient, setting_obj: Settings):
        response = await admin_client.get(f"{BASE_URL}?search=SMTP")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] >= 1

    async def test_list_settings_invalid_sort_order_422(self, admin_client: AsyncClient):
        response = await admin_client.get(f"{BASE_URL}?sort_order=sideways")
        assert response.status_code == 422

    async def test_list_settings_invalid_page_size_422(self, admin_client: AsyncClient):
        response = await admin_client.get(f"{BASE_URL}?page_size=-1")
        assert response.status_code == 422


# --------------------------------------------------------------------------
# GET /settings/statistics
# --------------------------------------------------------------------------
class TestStatisticsAPI:

    async def test_statistics_200(self, admin_client: AsyncClient, setting_obj: Settings):
        response = await admin_client.get(f"{BASE_URL}/statistics")
        assert response.status_code == 200
        body = response.json()
        assert "total_settings" in body
        assert body["total_settings"] >= 1

    async def test_statistics_date_from_after_date_to_400(self, admin_client: AsyncClient):
        response = await admin_client.get(
            f"{BASE_URL}/statistics?date_from=2026-06-01T00:00:00Z&date_to=2026-01-01T00:00:00Z"
        )
        assert response.status_code == 400


# --------------------------------------------------------------------------
# GET /settings/public
# --------------------------------------------------------------------------
class TestPublicSettingsAPI:

    async def test_public_settings_only_returns_public(
        self,
        admin_client: AsyncClient,
        public_setting_obj: Settings,
        setting_obj: Settings,
    ):
        response = await admin_client.get(f"{BASE_URL}/public")
        assert response.status_code == 200
        body = response.json()
        assert all(item["is_public"] is True for item in body)
        keys = {item["setting_key"] for item in body}
        assert public_setting_obj.setting_key in keys
        assert setting_obj.setting_key not in keys


# --------------------------------------------------------------------------
# GET /settings/category/{category}
# --------------------------------------------------------------------------
class TestSettingsByCategoryAPI:

    async def test_get_by_category_200(
        self, admin_client: AsyncClient, setting_obj: Settings
    ):
        response = await admin_client.get(f"{BASE_URL}/category/EMAIL")
        assert response.status_code == 200
        body = response.json()
        assert all(item["category"] == "EMAIL" for item in body)

    async def test_get_by_category_invalid_value_422(self, admin_client: AsyncClient):
        response = await admin_client.get(f"{BASE_URL}/category/NOT_A_REAL_CATEGORY")
        assert response.status_code == 422


# --------------------------------------------------------------------------
# GET /settings/cache/status, POST /settings/cache/reload
# --------------------------------------------------------------------------
class TestCacheAPI:

    async def test_cache_status_200(self, admin_client: AsyncClient):
        response = await admin_client.get(f"{BASE_URL}/cache/status")
        assert response.status_code == 200
        body = response.json()
        assert "total_entries" in body
        assert "is_loaded" in body

    async def test_cache_reload_200(self, admin_client: AsyncClient, setting_obj: Settings):
        response = await admin_client.post(f"{BASE_URL}/cache/reload")
        assert response.status_code == 200
        body = response.json()
        assert body["reloaded"] is True
        assert body["total_entries"] >= 1

    async def test_cache_reload_scoped_to_category(
        self, admin_client: AsyncClient, setting_obj: Settings
    ):
        response = await admin_client.post(f"{BASE_URL}/cache/reload?category=EMAIL")
        assert response.status_code == 200
        body = response.json()
        assert body["category"] == "EMAIL"

    async def test_create_setting_updates_cache_status(self, admin_client: AsyncClient):
        before = await admin_client.get(f"{BASE_URL}/cache/status")
        before_total = before.json()["total_entries"]

        await admin_client.post(
            BASE_URL,
            json={
                "category": "GENERAL",
                "setting_key": "CACHE_TEST_KEY",
                "setting_value": "x",
                "data_type": "STRING",
            },
        )

        after = await admin_client.get(f"{BASE_URL}/cache/status")
        assert after.json()["total_entries"] == before_total + 1


# --------------------------------------------------------------------------
# GET /settings/{setting_id}
# --------------------------------------------------------------------------
class TestGetSettingAPI:

    async def test_get_setting_200(self, admin_client: AsyncClient, setting_obj: Settings):
        response = await admin_client.get(f"{BASE_URL}/{setting_obj.id}")
        assert response.status_code == 200
        assert response.json()["id"] == str(setting_obj.id)

    async def test_get_setting_404(self, admin_client: AsyncClient):
        response = await admin_client.get(f"{BASE_URL}/{uuid.uuid4()}")
        assert response.status_code == 404

    async def test_get_setting_invalid_uuid_422(self, admin_client: AsyncClient):
        response = await admin_client.get(f"{BASE_URL}/not-a-uuid")
        assert response.status_code == 422


# --------------------------------------------------------------------------
# PUT / PATCH /settings/{setting_id}
# --------------------------------------------------------------------------
class TestUpdateSettingAPI:

    async def test_update_setting_put_200(
        self, admin_client: AsyncClient, setting_obj: Settings, admin_user: User
    ):
        response = await admin_client.put(
            f"{BASE_URL}/{setting_obj.id}",
            json={"setting_value": "smtp2.example.com", "updated_by": admin_user.id},
        )
        assert response.status_code == 200
        assert response.json()["setting_value"] == "smtp2.example.com"

    async def test_update_setting_patch_200(
        self, admin_client: AsyncClient, setting_obj: Settings
    ):
        response = await admin_client.patch(
            f"{BASE_URL}/{setting_obj.id}", json={"description": "Patched description."}
        )
        assert response.status_code == 200
        assert response.json()["description"] == "Patched description."

    async def test_update_setting_404(self, admin_client: AsyncClient):
        response = await admin_client.put(
            f"{BASE_URL}/{uuid.uuid4()}", json={"description": "ghost"}
        )
        assert response.status_code == 404

    async def test_update_non_editable_setting_422(
        self, admin_client: AsyncClient, non_editable_setting_obj: Settings
    ):
        response = await admin_client.put(
            f"{BASE_URL}/{non_editable_setting_obj.id}", json={"description": "should fail"}
        )
        assert response.status_code == 422

    async def test_update_setting_encrypted_and_public_422(
        self, admin_client: AsyncClient, setting_obj: Settings
    ):
        response = await admin_client.put(
            f"{BASE_URL}/{setting_obj.id}",
            json={"is_encrypted": True, "is_public": True},
        )
        assert response.status_code == 422


# --------------------------------------------------------------------------
# DELETE /settings/{setting_id}
# --------------------------------------------------------------------------
class TestDeleteSettingAPI:

    async def test_delete_setting_204(self, admin_client: AsyncClient, setting_obj: Settings):
        response = await admin_client.delete(f"{BASE_URL}/{setting_obj.id}")
        assert response.status_code == 204

    async def test_delete_setting_404(self, admin_client: AsyncClient):
        response = await admin_client.delete(f"{BASE_URL}/{uuid.uuid4()}")
        assert response.status_code == 404

    async def test_delete_already_deleted_returns_404(
        self, admin_client: AsyncClient, setting_obj: Settings
    ):
        first = await admin_client.delete(f"{BASE_URL}/{setting_obj.id}")
        assert first.status_code == 204

        second = await admin_client.delete(f"{BASE_URL}/{setting_obj.id}")
        assert second.status_code == 404

    async def test_delete_protected_system_setting_422(
        self, admin_client: AsyncClient, protected_setting_obj: Settings
    ):
        response = await admin_client.delete(f"{BASE_URL}/{protected_setting_obj.id}")
        assert response.status_code == 422


# --------------------------------------------------------------------------
# POST /settings/bulk-update
# --------------------------------------------------------------------------
class TestBulkUpdateSettingsAPI:

    async def test_bulk_update_200(self, admin_client: AsyncClient, setting_obj: Settings):
        response = await admin_client.post(
            f"{BASE_URL}/bulk-update",
            json={
                "updates": [
                    {
                        "setting_id": str(setting_obj.id),
                        "payload": {"description": "Updated via bulk."},
                    }
                ]
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body[0]["description"] == "Updated via bulk."

    async def test_bulk_update_empty_batch_422(self, admin_client: AsyncClient):
        response = await admin_client.post(f"{BASE_URL}/bulk-update", json={"updates": []})
        assert response.status_code == 422

    async def test_bulk_update_nonexistent_id_404(self, admin_client: AsyncClient):
        response = await admin_client.post(
            f"{BASE_URL}/bulk-update",
            json={
                "updates": [
                    {"setting_id": str(uuid.uuid4()), "payload": {"description": "ghost"}}
                ]
            },
        )
        assert response.status_code == 404

    async def test_bulk_update_non_editable_entry_422(
        self, admin_client: AsyncClient, non_editable_setting_obj: Settings
    ):
        response = await admin_client.post(
            f"{BASE_URL}/bulk-update",
            json={
                "updates": [
                    {
                        "setting_id": str(non_editable_setting_obj.id),
                        "payload": {"description": "should fail"},
                    }
                ]
            },
        )
        assert response.status_code == 422


# --------------------------------------------------------------------------
# DELETE /settings/bulk-delete
# --------------------------------------------------------------------------
class TestBulkDeleteSettingsAPI:

    async def test_bulk_delete_200(
        self, admin_client: AsyncClient, db_session: AsyncSession
    ):
        entry_1 = await _create_setting(db_session, setting_key="BULK_DELETE_1")
        entry_2 = await _create_setting(db_session, setting_key="BULK_DELETE_2")

        response = await admin_client.request(
            "DELETE",
            f"{BASE_URL}/bulk-delete",
            json={"ids": [str(entry_1.id), str(entry_2.id)]},
        )
        assert response.status_code == 200
        assert response.json()["deleted_count"] == 2

    async def test_bulk_delete_empty_ids_422(self, admin_client: AsyncClient):
        response = await admin_client.request(
            "DELETE", f"{BASE_URL}/bulk-delete", json={"ids": []}
        )
        assert response.status_code == 422

    async def test_bulk_delete_nonexistent_id_404(self, admin_client: AsyncClient):
        response = await admin_client.request(
            "DELETE", f"{BASE_URL}/bulk-delete", json={"ids": [str(uuid.uuid4())]}
        )
        assert response.status_code == 404

    async def test_bulk_delete_protected_setting_422(
        self, admin_client: AsyncClient, protected_setting_obj: Settings
    ):
        response = await admin_client.request(
            "DELETE",
            f"{BASE_URL}/bulk-delete",
            json={"ids": [str(protected_setting_obj.id)]},
        )
        assert response.status_code == 422


# --------------------------------------------------------------------------
# Swagger / OpenAPI Exposure
# --------------------------------------------------------------------------
class TestSwaggerSchemaExposure:

    async def test_openapi_contains_settings_paths(self, client: AsyncClient):
        response = await client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert BASE_URL in schema["paths"]
        assert f"{BASE_URL}/statistics" in schema["paths"]
        assert f"{BASE_URL}/public" in schema["paths"]
        assert f"{BASE_URL}/cache/reload" in schema["paths"]
        assert f"{BASE_URL}/cache/status" in schema["paths"]
        assert f"{BASE_URL}/bulk-update" in schema["paths"]
        assert f"{BASE_URL}/bulk-delete" in schema["paths"]
        assert any(
            path.startswith(f"{BASE_URL}/category/") for path in schema["paths"]
        )
        assert any(
            path.startswith(f"{BASE_URL}/") and "{setting_id}" in path
            for path in schema["paths"]
        )