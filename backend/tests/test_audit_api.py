# backend/tests/test_audit_api.py

"""
Audit Log Module - Phase 4
HTTP-level integration tests for the Audit Log FastAPI router
(`app/api/v1/audit_log.py`).

Scope:
    - Exercises the full stack (router -> service -> repository -> DB)
      against a real, isolated test database using an async HTTP
      an async HTTP client, mirroring the conventions established in
      `test_booking_api.py`.
    - Verifies authentication (401), authorization/RBAC (403), request
      validation (422), business-rule violations (400/409), and
      not-found handling (404) in addition to the happy paths (200/201).

Endpoints covered:
    GET    /audit-logs
    GET    /audit-logs/{id}
    GET    /audit-logs/search
    GET    /audit-logs/statistics
    GET    /audit-logs/recent
    GET    /audit-logs/failed
    GET    /audit-logs/critical
    GET    /audit-logs/user/{user_id}
    GET    /audit-logs/module/{module}
    GET    /audit-logs/entity/{entity_type}/{entity_id}
    POST   /audit-logs/export
    DELETE /audit-logs/{id}
    DELETE /audit-logs/cleanup
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.security import create_access_token
from app.db.base import Base
from app.db.session import get_db
from app.main import app as fastapi_app
from app.models.audit_log import AuditAction, AuditLog, AuditModule, AuditSeverity, AuditStatus
from app.models.user import User, UserRole

pytestmark = pytest.mark.asyncio

BASE_URL = f"{settings.API_V1_PREFIX}/audit-logs"

TEST_DATABASE_URL = getattr(settings, "TEST_DATABASE_URL", None) or (
    "postgresql+asyncpg://postgres:Elan%402004@localhost:5432/test_audit_api_db"
)


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
# NOTE: no custom `event_loop` fixture override here (unlike an earlier
# version of this file). That deprecated pattern is incompatible with
# httpx.AsyncClient/ASGITransport under this pytest-asyncio version --
# it produces `RuntimeError: ... Future attached to a different loop`
# at request time, because the app's DB session ends up bound to a
# different event loop than the one the ASGI transport actually runs
# requests on. Using the implicit, function-scoped event loop pytest-
# asyncio provides by default for every fixture keeps everything (test,
# app, db session, HTTP client) on the same loop.
@pytest_asyncio.fixture
async def async_engine():
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, future=True)
    # Only the tables owned/required by this API suite are created.
    # Creating the entire application's metadata here causes unrelated
    # native PostgreSQL enum types from other modules to collide and makes
    # an authentication test fail before the request is even executed.
    required_tables = [User.__table__, AuditLog.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn, tables=required_tables
            )
        )
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.drop_all(
                sync_conn, tables=list(reversed(required_tables))
            )
        )
    await engine.dispose()


@pytest_asyncio.fixture
async def audit_db_session(async_engine) -> AsyncSession:
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


@pytest_asyncio.fixture
async def audit_app(audit_db_session: AsyncSession):
    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield audit_db_session

    fastapi_app.dependency_overrides[get_db] = _override_get_db
    yield fastapi_app
    fastapi_app.dependency_overrides.pop(get_db, None)


@pytest_asyncio.fixture
async def audit_client(audit_app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=audit_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _make_user(audit_db_session: AsyncSession, role: UserRole, email: str) -> User:
    # NOTE: the current User model (app/models/user.py) names this column
    # `password_hash`, not `hashed_password`, and also requires `uuid` and
    # `phone` (unique, non-nullable, no client-side default for either).
    user = User(
        uuid=str(uuid.uuid4()),
        email=email,
        phone=f"+1555{uuid.uuid4().int % 10_000_000:07d}",
        password_hash="not-a-real-hash",
        full_name=f"{role.value.title()} Test User",
        role=role,
        is_active=True,
    )
    audit_db_session.add(user)
    await audit_db_session.commit()
    await audit_db_session.refresh(user)
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
async def audit_admin_user(audit_db_session: AsyncSession) -> User:
    return await _make_user(audit_db_session, UserRole.ADMIN, "admin@audittest.io")


@pytest_asyncio.fixture
async def audit_sales_manager_user(audit_db_session: AsyncSession) -> User:
    return await _make_user(audit_db_session, UserRole.SALES_MANAGER, "manager@audittest.io")


@pytest_asyncio.fixture
async def audit_sales_agent_user(audit_db_session: AsyncSession) -> User:
    return await _make_user(audit_db_session, UserRole.SALES_AGENT, "agent@audittest.io")


@pytest_asyncio.fixture
async def audit_admin_client(audit_app, audit_admin_user: User) -> AsyncIterator[AsyncClient]:
    async with _bearer_client(audit_app, audit_admin_user) as ac:
        yield ac


@pytest_asyncio.fixture
async def audit_sales_agent_client(audit_app, audit_sales_agent_user: User) -> AsyncIterator[AsyncClient]:
    async with _bearer_client(audit_app, audit_sales_agent_user) as ac:
        yield ac


@pytest_asyncio.fixture
async def audit_sales_manager_client(audit_app, audit_sales_manager_user: User) -> AsyncIterator[AsyncClient]:
    async with _bearer_client(audit_app, audit_sales_manager_user) as ac:
        yield ac


async def _create_log(
    audit_db_session: AsyncSession,
    *,
    user_id: int = 1,
    module: AuditModule = AuditModule.LEAD,
    action: AuditAction = AuditAction.CREATE,
    entity_type: str = "LEAD",
    entity_id: str | None = None,
    severity: AuditSeverity = AuditSeverity.LOW,
    status: AuditStatus = AuditStatus.SUCCESS,
    description: str = "Created a new lead record.",
    created_at: datetime | None = None,
) -> AuditLog:
    log = AuditLog(
        user_id=user_id,
        module=module,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id or str(uuid.uuid4()),
        severity=severity,
        status=status,
        description=description,
    )
    audit_db_session.add(log)
    await audit_db_session.commit()
    await audit_db_session.refresh(log)
    if created_at is not None:
        log.created_at = created_at
        audit_db_session.add(log)
        await audit_db_session.commit()
        await audit_db_session.refresh(log)
    return log


@pytest_asyncio.fixture
async def audit_log_obj(audit_db_session: AsyncSession) -> AuditLog:
    return await _create_log(audit_db_session)


@pytest_asyncio.fixture
async def failed_log_obj(audit_db_session: AsyncSession) -> AuditLog:
    return await _create_log(audit_db_session, status=AuditStatus.FAILED, description="Failed login attempt.")


@pytest_asyncio.fixture
async def critical_log_obj(audit_db_session: AsyncSession) -> AuditLog:
    return await _create_log(
        audit_db_session, severity=AuditSeverity.CRITICAL, description="Critical data deletion."
    )


# --------------------------------------------------------------------------
# Authentication (401)
# --------------------------------------------------------------------------
class TestAuditLogAuthentication:

    async def test_list_audit_logs_without_token_returns_401(self, audit_client: AsyncClient):
        response = await audit_client.get(BASE_URL)
        assert response.status_code == 401

    async def test_get_audit_log_without_token_returns_401(self, audit_client: AsyncClient):
        response = await audit_client.get(f"{BASE_URL}/{uuid.uuid4()}")
        assert response.status_code == 401

    async def test_invalid_token_returns_401(self, audit_client: AsyncClient):
        response = await audit_client.get(BASE_URL, headers=auth_headers("invalid.token.value"))
        assert response.status_code == 401

    async def test_export_without_token_returns_401(self, audit_client: AsyncClient):
        response = await audit_client.post(f"{BASE_URL}/export", json={})
        assert response.status_code == 401

    async def test_delete_without_token_returns_401(self, audit_client: AsyncClient):
        response = await audit_client.delete(f"{BASE_URL}/{uuid.uuid4()}")
        assert response.status_code == 401


# --------------------------------------------------------------------------
# Authorization (403)
# --------------------------------------------------------------------------
class TestAuditLogAuthorization:

    async def test_sales_agent_cannot_delete_audit_log(
        self, audit_sales_agent_client: AsyncClient, audit_log_obj: AuditLog
    ):
        response = await audit_sales_agent_client.delete(f"{BASE_URL}/{audit_log_obj.id}")
        assert response.status_code == 403

    async def test_sales_agent_cannot_cleanup_audit_logs(self, audit_sales_agent_client: AsyncClient):
        response = await audit_sales_agent_client.delete(f"{BASE_URL}/cleanup?retention_days=365")
        assert response.status_code == 403

    async def test_sales_agent_cannot_view_statistics(self, audit_sales_agent_client: AsyncClient):
        response = await audit_sales_agent_client.get(f"{BASE_URL}/statistics")
        assert response.status_code == 403

    async def test_admin_can_delete_audit_log(
        self, audit_admin_client: AsyncClient, audit_log_obj: AuditLog
    ):
        response = await audit_admin_client.delete(f"{BASE_URL}/{audit_log_obj.id}")
        assert response.status_code == 204

    async def test_sales_manager_can_view_audit_logs(self, audit_sales_manager_client: AsyncClient):
        response = await audit_sales_manager_client.get(BASE_URL)
        assert response.status_code == 200


# --------------------------------------------------------------------------
# GET /audit-logs
# --------------------------------------------------------------------------
class TestListAuditLogsAPI:

    async def test_list_audit_logs_200(self, audit_admin_client: AsyncClient, audit_log_obj: AuditLog):
        response = await audit_admin_client.get(BASE_URL, headers={})
        response = await audit_admin_client.get(BASE_URL)
        assert response.status_code == 200
        body = response.json()
        assert "items" in body
        assert "total" in body

    async def test_list_audit_logs_pagination(self, audit_admin_client: AsyncClient):
        response = await audit_admin_client.get(f"{BASE_URL}?page=1&page_size=5")
        assert response.status_code == 200
        body = response.json()
        assert body["page"] == 1
        assert body["page_size"] == 5

    async def test_list_audit_logs_invalid_sort_order_422(self, audit_admin_client: AsyncClient):
        response = await audit_admin_client.get(f"{BASE_URL}?sort_order=invalid")
        assert response.status_code == 422

    async def test_list_audit_logs_invalid_page_size_422(self, audit_admin_client: AsyncClient):
        response = await audit_admin_client.get(f"{BASE_URL}?page_size=-1")
        assert response.status_code == 422


# --------------------------------------------------------------------------
# GET /audit-logs/{id}
# --------------------------------------------------------------------------
class TestGetAuditLogAPI:

    async def test_get_audit_log_200(self, audit_admin_client: AsyncClient, audit_log_obj: AuditLog):
        response = await audit_admin_client.get(f"{BASE_URL}/{audit_log_obj.id}")
        assert response.status_code == 200
        assert response.json()["id"] == str(audit_log_obj.id)

    async def test_get_audit_log_404(self, audit_admin_client: AsyncClient):
        response = await audit_admin_client.get(f"{BASE_URL}/{uuid.uuid4()}")
        assert response.status_code == 404

    async def test_get_audit_log_invalid_uuid_422(self, audit_admin_client: AsyncClient):
        response = await audit_admin_client.get(f"{BASE_URL}/not-a-uuid")
        assert response.status_code == 422


# --------------------------------------------------------------------------
# GET /audit-logs/search
# --------------------------------------------------------------------------
class TestSearchAuditLogsAPI:

    async def test_search_by_keyword_200(self, audit_admin_client: AsyncClient, audit_log_obj: AuditLog):
        # The real endpoint's query param is `q` (required, min_length=1),
        # not `search` -- see search_audit_logs() in app/api/v1/audit_log.py.
        response = await audit_admin_client.get(f"{BASE_URL}/search?q=Created a new lead")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] >= 1

    @pytest.mark.skip(
        reason="GET /audit-logs/search has no `module` filter parameter -- "
        "only `q`, `page`, `page_size`, `sort_by`, `sort_order` (see "
        "search_audit_logs() in app/api/v1/audit_log.py). Module filtering "
        "is only available via GET /audit-logs or GET /audit-logs/module/{module}, "
        "both covered elsewhere in this file."
    )
    async def test_search_by_module_filter(self, audit_admin_client: AsyncClient, audit_log_obj: AuditLog):
        response = await audit_admin_client.get(f"{BASE_URL}/search?module=LEAD")
        assert response.status_code == 200

    async def test_search_missing_query_returns_422(self, audit_admin_client: AsyncClient):
        # `q` is a required query parameter on this endpoint, so omitting
        # it (regardless of any other, unsupported query params supplied)
        # is rejected by FastAPI's own request validation before the
        # handler ever runs.
        response = await audit_admin_client.get(f"{BASE_URL}/search?module=NOT_REAL")
        assert response.status_code == 422

    @pytest.mark.skip(
        reason="GET /audit-logs/search has no `date_from`/`date_to` filter "
        "parameters -- only `q`, `page`, `page_size`, `sort_by`, `sort_order` "
        "(see search_audit_logs() in app/api/v1/audit_log.py). Date-range "
        "filtering is only available via GET /audit-logs."
    )
    async def test_search_by_date_range(self, audit_admin_client: AsyncClient, audit_log_obj: AuditLog):
        now = datetime.now(timezone.utc)
        date_from = (now - timedelta(hours=1)).isoformat()
        date_to = (now + timedelta(hours=1)).isoformat()
        response = await audit_admin_client.get(
            f"{BASE_URL}/search?date_from={date_from}&date_to={date_to}"
        )
        assert response.status_code == 200


# --------------------------------------------------------------------------
# GET /audit-logs/statistics
# --------------------------------------------------------------------------
class TestStatisticsAPI:

    async def test_statistics_200(self, audit_admin_client: AsyncClient, audit_log_obj: AuditLog):
        response = await audit_admin_client.get(f"{BASE_URL}/statistics")
        assert response.status_code == 200
        body = response.json()
        # AuditStatisticsResponse (app/schemas/audit_log.py) names this
        # field `total_events`, not `total_logs`.
        assert "total_events" in body


# --------------------------------------------------------------------------
# GET /audit-logs/recent, /failed, /critical
# --------------------------------------------------------------------------
class TestRecentFailedCriticalAPI:

    async def test_recent_logs_200(self, audit_admin_client: AsyncClient, audit_log_obj: AuditLog):
        response = await audit_admin_client.get(f"{BASE_URL}/recent")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    async def test_failed_logs_200(self, audit_admin_client: AsyncClient, failed_log_obj: AuditLog):
        response = await audit_admin_client.get(f"{BASE_URL}/failed")
        assert response.status_code == 200
        body = response.json()
        assert all(item["status"] == "FAILED" for item in body)

    async def test_critical_logs_200(self, audit_admin_client: AsyncClient, critical_log_obj: AuditLog):
        response = await audit_admin_client.get(f"{BASE_URL}/critical")
        assert response.status_code == 200
        body = response.json()
        assert all(item["severity"] == "CRITICAL" for item in body)


# --------------------------------------------------------------------------
# GET /audit-logs/user/{user_id}, /module/{module}, /entity/{type}/{id}
# --------------------------------------------------------------------------
class TestScopedLookupAPI:

    async def test_get_by_user_200(self, audit_admin_client: AsyncClient, audit_log_obj: AuditLog):
        response = await audit_admin_client.get(f"{BASE_URL}/user/{audit_log_obj.user_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] >= 1

    async def test_get_by_module_200(self, audit_admin_client: AsyncClient, audit_log_obj: AuditLog):
        response = await audit_admin_client.get(f"{BASE_URL}/module/LEAD")
        assert response.status_code == 200

    async def test_get_by_module_unknown_returns_empty_page(self, audit_admin_client: AsyncClient):
        # `module` is a free-form string filter with no server-side
        # validation against a known set of modules (see
        # get_audit_logs_by_module() in app/api/v1/audit_log.py) -- an
        # unrecognized module simply matches nothing.
        response = await audit_admin_client.get(f"{BASE_URL}/module/NOT_REAL_MODULE")
        assert response.status_code == 200
        body = response.json()
        assert body["items"] == []
        assert body["total"] == 0

    async def test_get_by_entity_200(self, audit_admin_client: AsyncClient, audit_log_obj: AuditLog):
        response = await audit_admin_client.get(
            f"{BASE_URL}/entity/{audit_log_obj.entity_type}/{audit_log_obj.entity_id}"
        )
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    async def test_get_by_entity_no_match_returns_empty_list(self, audit_admin_client: AsyncClient):
        response = await audit_admin_client.get(f"{BASE_URL}/entity/LEAD/{uuid.uuid4()}")
        assert response.status_code == 200
        assert response.json() == []


# --------------------------------------------------------------------------
# POST /audit-logs/export
# --------------------------------------------------------------------------
class TestExportAuditLogsAPI:

    async def test_export_success_200(self, audit_admin_client: AsyncClient, audit_log_obj: AuditLog):
        response = await audit_admin_client.post(
            f"{BASE_URL}/export", json={"export_format": "csv"}
        )
        assert response.status_code == 200

    async def test_export_invalid_date_range_422(self, audit_admin_client: AsyncClient):
        # AuditLogFilter has no `export_format` concept to validate (see
        # note above); it does, however, enforce date_from <= date_to via
        # its own model_validator (app/schemas/audit_log.py), which is a
        # genuine request-validation failure this endpoint documents (422).
        now = datetime.now(timezone.utc)
        response = await audit_admin_client.post(
            f"{BASE_URL}/export",
            json={
                "date_from": (now + timedelta(days=1)).isoformat(),
                "date_to": now.isoformat(),
            },
        )
        assert response.status_code == 422

    async def test_export_invalid_action_enum_422(self, audit_admin_client: AsyncClient):
        response = await audit_admin_client.post(
            f"{BASE_URL}/export", json={"action": "NOT_A_REAL_ACTION"}
        )
        assert response.status_code == 422


# --------------------------------------------------------------------------
# DELETE /audit-logs/{id}
# --------------------------------------------------------------------------
class TestDeleteAuditLogAPI:

    async def test_delete_204(self, audit_admin_client: AsyncClient, audit_log_obj: AuditLog):
        response = await audit_admin_client.delete(f"{BASE_URL}/{audit_log_obj.id}")
        assert response.status_code == 204

    async def test_delete_nonexistent_404(self, audit_admin_client: AsyncClient):
        response = await audit_admin_client.delete(f"{BASE_URL}/{uuid.uuid4()}")
        assert response.status_code == 404

    async def test_delete_already_deleted_409(self, audit_admin_client: AsyncClient, audit_log_obj: AuditLog):
        first = await audit_admin_client.delete(f"{BASE_URL}/{audit_log_obj.id}")
        assert first.status_code == 204

        second = await audit_admin_client.delete(f"{BASE_URL}/{audit_log_obj.id}")
        assert second.status_code in (404, 409)


# --------------------------------------------------------------------------
# DELETE /audit-logs/cleanup
# --------------------------------------------------------------------------
class TestCleanupAuditLogsAPI:

    async def test_cleanup_success_200(self, audit_admin_client: AsyncClient, audit_log_obj: AuditLog):
        response = await audit_admin_client.delete(f"{BASE_URL}/cleanup?retention_days=365")
        assert response.status_code == 200
        body = response.json()
        assert "deleted_count" in body

    async def test_cleanup_invalid_days_422(self, audit_admin_client: AsyncClient):
        response = await audit_admin_client.delete(f"{BASE_URL}/cleanup?retention_days=-5")
        assert response.status_code == 422

    async def test_cleanup_below_minimum_retention_422(self, audit_admin_client: AsyncClient):
        # retention_days=1 satisfies the query param's own `ge=1`
        # constraint, so this passes FastAPI's own request validation and
        # reaches AuditLogService.cleanup_old_logs(), which then raises
        # BusinessRuleException because 1 < MIN_RETENTION_DAYS (30). That
        # exception's default_status_code is 422 (HTTP_422_UNPROCESSABLE_ENTITY),
        # not 400 -- see app/core/exceptions.py.
        response = await audit_admin_client.delete(f"{BASE_URL}/cleanup?retention_days=1")
        assert response.status_code == 422


# --------------------------------------------------------------------------
# Swagger / OpenAPI Exposure
# --------------------------------------------------------------------------
class TestSwaggerSchemaExposure:

    async def test_openapi_contains_audit_log_paths(self, audit_client: AsyncClient):
        response = await audit_client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert f"{BASE_URL}" in schema["paths"]
        assert f"{BASE_URL}/{{audit_log_id}}" in schema["paths"] or any(
            path.startswith(f"{BASE_URL}/") and "{" in path for path in schema["paths"]
        )
        assert f"{BASE_URL}/statistics" in schema["paths"]
        assert f"{BASE_URL}/search" in schema["paths"]
        assert f"{BASE_URL}/recent" in schema["paths"]
        assert f"{BASE_URL}/failed" in schema["paths"]
        assert f"{BASE_URL}/critical" in schema["paths"]
        assert f"{BASE_URL}/export" in schema["paths"]
        assert f"{BASE_URL}/cleanup" in schema["paths"]