"""
backend/tests/test_ai_router_integration.py

FINAL AI API / ROUTER INTEGRATION AUDIT.

HTTP-level integration tests for `app/api/v1/ai.py`, exercising the full
stack (router -> service -> repository -> real PostgreSQL + real pgvector)
through the actual FastAPI application via `httpx.AsyncClient`.

Scope / what is real vs mocked:
    - Real: FastAPI app (`app.main.app`), routing, dependency injection,
      RBAC (`RoleChecker`), JWT auth (`create_access_token` / `decode_token`),
      Pydantic request/response validation, every repository, every
      service's business logic, and a real PostgreSQL 16 + pgvector 0.6.0
      database (via a function-scoped transactional session that is rolled
      back after each test, matching the existing convention in
      `tests/test_booking_api.py`).
    - Mocked: ONLY the external network boundary -- `AIProviderClient.complete`
      (wraps the Anthropic SDK) and `EmbeddingService._call_embedding_api`
      (raw httpx call to the embeddings provider). No repository, service,
      schema, or database operation is mocked.

Run with:
    POSTGRES_DB=crm_ai_audit pytest backend/tests/test_ai_router_integration.py -v
"""

from __future__ import annotations

import io
import uuid
from typing import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.security import create_access_token
from app.db.session import get_db
from app.main import app as fastapi_app
from app.models.user import User, UserRole

AI_PREFIX = "/api/v1/ai"

# --------------------------------------------------------------------------
# Fixtures (mirrors tests/test_booking_api.py's real-DB pattern)
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session_factory = async_sessionmaker(
            bind=connection, class_=AsyncSession, expire_on_commit=False
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
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def _make_user(db_session: AsyncSession, role: UserRole) -> User:
    suffix = uuid.uuid4().hex[:10]
    user = User(
        uuid=str(uuid.uuid4()),
        full_name=f"{role.value.title()} Test User",
        email=f"{role.value.lower()}.{suffix}@aiaudit.io",
        phone=f"9{suffix[:9]}",
        password_hash="not-a-real-hash-$2b$12$test.value.only",
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


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, UserRole.ADMIN)


@pytest_asyncio.fixture
async def agent_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, UserRole.SALES_AGENT)


@pytest_asyncio.fixture
async def admin_client(app, admin_user: User) -> AsyncIterator[AsyncClient]:
    async with _bearer_client(app, admin_user) as ac:
        yield ac


@pytest_asyncio.fixture
async def agent_client(app, agent_user: User) -> AsyncIterator[AsyncClient]:
    async with _bearer_client(app, agent_user) as ac:
        yield ac


# --------------------------------------------------------------------------
# Provider-boundary mocks (ONLY the external LLM / embedding HTTP calls)
# --------------------------------------------------------------------------


def _fake_completion(content: str = "Hello from the AI provider.") -> dict:
    return {
        "content": content,
        "prompt_tokens": 12,
        "completion_tokens": 8,
        "model": "claude-sonnet-4-6",
    }


@pytest.fixture(autouse=True)
def mock_provider_boundary():
    """Patch the AI provider + embedding provider boundary for every test.

    Nothing else (repositories, services, schemas, the DB) is mocked.
    """
    with patch(
        "app.services.ai_service.AIProviderClient.complete",
        new=AsyncMock(return_value=_fake_completion()),
    ) as mock_complete, patch(
        "app.services.embedding_service.EmbeddingService._call_embedding_api",
        new=AsyncMock(
            side_effect=lambda batch: [[0.01 * i] * 1536 for i in range(1, len(batch) + 1)]
        ),
    ) as mock_embed:
        yield {"complete": mock_complete, "embed": mock_embed}


# --------------------------------------------------------------------------
# Auth / RBAC
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_conversations_without_token_returns_401(client: AsyncClient) -> None:
    resp = await client.get(f"{AI_PREFIX}/conversations")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_usage_logs_forbidden_for_non_admin(agent_client: AsyncClient) -> None:
    resp = await agent_client.get(f"{AI_PREFIX}/usage-logs")
    assert resp.status_code == 403


# --------------------------------------------------------------------------
# Conversations + Messages (Chat)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conversation_and_chat_lifecycle(agent_client: AsyncClient) -> None:
    create_resp = await agent_client.post(
        f"{AI_PREFIX}/conversations",
        json={"title": "Lead follow-up", "module": "chat"},
    )
    assert create_resp.status_code == 201, create_resp.text
    conv = create_resp.json()
    conv_id = conv["id"]
    uuid.UUID(conv_id)
    assert conv["module"] == "chat"
    assert conv["is_archived"] is False

    list_resp = await agent_client.get(f"{AI_PREFIX}/conversations")
    assert list_resp.status_code == 200
    listing = list_resp.json()
    assert listing["total"] >= 1
    assert any(item["id"] == conv_id for item in listing["items"])

    send_resp = await agent_client.post(
        f"{AI_PREFIX}/conversations/{conv_id}/messages",
        json={"message": "What properties are available downtown?"},
    )
    assert send_resp.status_code == 201, send_resp.text
    chat = send_resp.json()
    assert chat["conversation_id"] == conv_id
    assert chat["user_message"]["role"] == "user"
    assert chat["assistant_message"]["role"] == "assistant"
    assert chat["assistant_message"]["content"] == "Hello from the AI provider."
    assert chat["usage"]["total_tokens"] == 20

    detail_resp = await agent_client.get(f"{AI_PREFIX}/conversations/{conv_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert len(detail["messages"]) == 2

    messages_resp = await agent_client.get(f"{AI_PREFIX}/conversations/{conv_id}/messages")
    assert messages_resp.status_code == 200
    assert messages_resp.json()["total"] == 2

    patch_resp = await agent_client.patch(
        f"{AI_PREFIX}/conversations/{conv_id}", json={"is_archived": True}
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["is_archived"] is True

    delete_resp = await agent_client.delete(f"{AI_PREFIX}/conversations/{conv_id}")
    assert delete_resp.status_code == 204

    get_after_delete = await agent_client.get(f"{AI_PREFIX}/conversations/{conv_id}")
    assert get_after_delete.status_code == 404


@pytest.mark.asyncio
async def test_conversation_create_invalid_module_returns_422(agent_client: AsyncClient) -> None:
    resp = await agent_client.post(
        f"{AI_PREFIX}/conversations", json={"title": "x", "module": "not-a-real-module"}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_conversation_not_owned_returns_403(
    agent_client: AsyncClient, admin_client: AsyncClient
) -> None:
    create_resp = await admin_client.post(
        f"{AI_PREFIX}/conversations", json={"title": "Admin's chat", "module": "chat"}
    )
    conv_id = create_resp.json()["id"]

    resp = await agent_client.get(f"{AI_PREFIX}/conversations/{conv_id}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_conversation_not_found_returns_404(agent_client: AsyncClient) -> None:
    resp = await agent_client.get(f"{AI_PREFIX}/conversations/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_send_message_provider_failure_logs_and_returns_500(
    agent_client: AsyncClient,
) -> None:
    create_resp = await agent_client.post(
        f"{AI_PREFIX}/conversations", json={"title": "Will fail", "module": "chat"}
    )
    conv_id = create_resp.json()["id"]

    failing_transport = ASGITransport(app=agent_client._transport.app, raise_app_exceptions=False)
    with patch(
        "app.services.ai_service.AIProviderClient.complete",
        new=AsyncMock(side_effect=RuntimeError("provider unavailable")),
    ):
        async with AsyncClient(
            transport=failing_transport,
            base_url="http://test",
            headers=agent_client.headers,
        ) as failing_client:
            resp = await failing_client.post(
                f"{AI_PREFIX}/conversations/{conv_id}/messages",
                json={"message": "hello"},
            )
    # Provider errors are not one of the domain exceptions translated by
    # _translate_domain_exceptions, so they surface as 500s. This is
    # asserted explicitly here so any change in that behavior is caught.
    assert resp.status_code == 500
    # Usage logging on the failure path must itself succeed (module="chat",
    # status="failure" -- both valid AIFeature/AIUsageStatus values); if it
    # didn't, this request would have failed with a masking 422 instead of
    # bubbling up the 500 asserted above.


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_crud_and_render(admin_client: AsyncClient, agent_client: AsyncClient) -> None:
    create_resp = await admin_client.post(
        f"{AI_PREFIX}/prompts",
        json={
            "name": _unique("Follow-up Prompt"),
            "description": "Sends a follow-up",
            "category": "chat",
            "template_text": "Hello {{name}}, following up on {{topic}}.",
            "variables": ["name", "topic"],
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    prompt = create_resp.json()
    prompt_id = prompt["id"]
    assert prompt["is_active"] is True

    list_resp = await agent_client.get(f"{AI_PREFIX}/prompts")
    assert list_resp.status_code == 200
    assert any(p["id"] == prompt_id for p in list_resp.json()["items"])

    get_resp = await agent_client.get(f"{AI_PREFIX}/prompts/{prompt_id}")
    assert get_resp.status_code == 200

    render_resp = await agent_client.post(
        f"{AI_PREFIX}/prompts/{prompt_id}/render",
        json={"variables": {"name": "Asha", "topic": "the downtown listing"}},
    )
    assert render_resp.status_code == 200, render_resp.text
    assert render_resp.json()["rendered_text"] == (
        "Hello Asha, following up on the downtown listing."
    )

    render_missing_var = await agent_client.post(
        f"{AI_PREFIX}/prompts/{prompt_id}/render", json={"variables": {"name": "Asha"}}
    )
    assert render_missing_var.status_code == 422

    update_resp = await admin_client.patch(
        f"{AI_PREFIX}/prompts/{prompt_id}", json={"is_active": False}
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["is_active"] is False

    delete_resp = await admin_client.delete(f"{AI_PREFIX}/prompts/{prompt_id}")
    assert delete_resp.status_code == 204

    get_after_delete = await agent_client.get(f"{AI_PREFIX}/prompts/{prompt_id}")
    assert get_after_delete.status_code == 404


@pytest.mark.asyncio
async def test_prompt_create_forbidden_for_agent(agent_client: AsyncClient) -> None:
    resp = await agent_client.post(
        f"{AI_PREFIX}/prompts",
        json={
            "name": _unique("Agent Prompt"),
            "category": "chat",
            "template_text": "Hi",
            "variables": [],
        },
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_prompt_duplicate_name_returns_409(admin_client: AsyncClient) -> None:
    name = _unique("Dup Prompt")
    payload = {
        "name": name,
        "category": "chat",
        "template_text": "Hi there",
        "variables": [],
    }
    first = await admin_client.post(f"{AI_PREFIX}/prompts", json=payload)
    assert first.status_code == 201
    second = await admin_client.post(f"{AI_PREFIX}/prompts", json=payload)
    assert second.status_code == 409


# --------------------------------------------------------------------------
# Documents (Knowledge base upload) + RAG
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_document_upload_list_get_delete(admin_client: AsyncClient) -> None:
    file_content = b"Downtown Metro Tower is a 40-story residential building in Sector 5."
    files = {"file": ("brochure.txt", io.BytesIO(file_content), "text/plain")}

    upload_resp = await admin_client.post(
        f"{AI_PREFIX}/documents/upload",
        params={"title": "Metro Tower Brochure"},
        files=files,
    )
    assert upload_resp.status_code == 201, upload_resp.text
    doc = upload_resp.json()
    doc_id = doc["id"]
    assert doc["status"] == "completed"
    assert doc["chunk_count"] == 1

    list_resp = await admin_client.get(f"{AI_PREFIX}/documents")
    assert list_resp.status_code == 200
    assert any(d["id"] == doc_id for d in list_resp.json()["items"])

    get_resp = await admin_client.get(f"{AI_PREFIX}/documents/{doc_id}")
    assert get_resp.status_code == 200

    delete_resp = await admin_client.delete(f"{AI_PREFIX}/documents/{doc_id}")
    assert delete_resp.status_code == 204

    get_after_delete = await admin_client.get(f"{AI_PREFIX}/documents/{doc_id}")
    assert get_after_delete.status_code == 404


@pytest.mark.asyncio
async def test_document_upload_rejects_unsupported_extension(admin_client: AsyncClient) -> None:
    files = {"file": ("virus.exe", io.BytesIO(b"MZ"), "application/octet-stream")}
    resp = await admin_client.post(
        f"{AI_PREFIX}/documents/upload", params={"title": "Bad file"}, files=files
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_document_upload_forbidden_for_agent(agent_client: AsyncClient) -> None:
    files = {"file": ("notes.txt", io.BytesIO(b"notes"), "text/plain")}
    resp = await agent_client.post(
        f"{AI_PREFIX}/documents/upload", params={"title": "Notes"}, files=files
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_rag_query_with_indexed_document_returns_grounded_answer(
    admin_client: AsyncClient, agent_client: AsyncClient
) -> None:
    file_content = (
        b"The Riverside Apartments complex offers 3-bedroom units starting "
        b"at 85 lakh rupees, with possession from December 2026."
    )
    files = {"file": ("riverside.txt", io.BytesIO(file_content), "text/plain")}
    upload_resp = await admin_client.post(
        f"{AI_PREFIX}/documents/upload",
        params={"title": "Riverside Apartments Listing"},
        files=files,
    )
    assert upload_resp.status_code == 201
    assert upload_resp.json()["status"] == "completed"

    rag_resp = await agent_client.post(
        f"{AI_PREFIX}/rag/query",
        json={"question": "What is the starting price for Riverside Apartments?", "top_k": 3},
    )
    assert rag_resp.status_code == 200, rag_resp.text
    body = rag_resp.json()
    assert body["answer"] == "Hello from the AI provider."
    assert len(body["sources"]) == 1
    assert body["sources"][0]["document_title"] == "Riverside Apartments Listing"
    assert body["usage"]["total_tokens"] == 20


@pytest.mark.asyncio
async def test_rag_query_with_empty_knowledge_base_returns_no_match_answer(
    agent_client: AsyncClient,
) -> None:
    resp = await agent_client.post(
        f"{AI_PREFIX}/rag/query", json={"question": "Anything about a nonexistent property?"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["sources"] == []
    assert "couldn't find" in body["answer"].lower()
    assert body["usage"]["total_tokens"] == 0


# --------------------------------------------------------------------------
# SQL AI
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sql_ai_query_success(admin_client: AsyncClient, mock_provider_boundary) -> None:
    mock_provider_boundary["complete"].return_value = _fake_completion(
        "SELECT id, title FROM properties WHERE city = 'Mumbai'"
    )
    resp = await admin_client.post(
        f"{AI_PREFIX}/sql/query",
        json={"question": "Show me all properties in Mumbai"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sql"].lower().startswith("select")
    assert "limit" in body["sql"].lower()
    assert body["usage"]["total_tokens"] == 20


@pytest.mark.asyncio
async def test_sql_ai_query_rejects_mutating_sql(
    admin_client: AsyncClient, mock_provider_boundary
) -> None:
    mock_provider_boundary["complete"].return_value = _fake_completion(
        "DROP TABLE properties"
    )
    resp = await admin_client.post(
        f"{AI_PREFIX}/sql/query", json={"question": "Delete everything"}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_sql_ai_query_forbidden_for_agent(agent_client: AsyncClient) -> None:
    resp = await agent_client.post(
        f"{AI_PREFIX}/sql/query", json={"question": "Show me all properties"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_sql_ai_query_blank_question_returns_422(admin_client: AsyncClient) -> None:
    resp = await admin_client.post(f"{AI_PREFIX}/sql/query", json={"question": ""})
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Analytics AI
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analytics_ai_query_success(agent_client: AsyncClient) -> None:
    resp = await agent_client.post(
        f"{AI_PREFIX}/analytics/query",
        json={
            "question": "What is the trend in bookings this quarter?",
            "dataset": [
                {"month": "June", "bookings": 12},
                {"month": "July", "bookings": 18},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["insights"] == "Hello from the AI provider."
    assert body["usage"]["total_tokens"] == 20


@pytest.mark.asyncio
async def test_analytics_ai_query_empty_dataset_returns_422(agent_client: AsyncClient) -> None:
    resp = await agent_client.post(
        f"{AI_PREFIX}/analytics/query",
        json={"question": "Any trend?", "dataset": []},
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Usage logs
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_logs_recorded_and_listable(
    admin_client: AsyncClient, agent_client: AsyncClient
) -> None:
    resp = await agent_client.post(
        f"{AI_PREFIX}/analytics/query",
        json={"question": "Trend?", "dataset": [{"a": 1}]},
    )
    assert resp.status_code == 200

    logs_resp = await admin_client.get(f"{AI_PREFIX}/usage-logs", params={"module": "analytics"})
    assert logs_resp.status_code == 200
    logs = logs_resp.json()
    assert logs["total"] >= 1
    assert all(item["module"] == "analytics" for item in logs["items"])

    summary_resp = await admin_client.get(f"{AI_PREFIX}/usage-logs/summary")
    assert summary_resp.status_code == 200
    summary = summary_resp.json()
    assert summary["total_requests"] >= 1


@pytest.mark.asyncio
async def test_usage_logs_summary_forbidden_for_agent(agent_client: AsyncClient) -> None:
    resp = await agent_client.get(f"{AI_PREFIX}/usage-logs/summary")
    assert resp.status_code == 403