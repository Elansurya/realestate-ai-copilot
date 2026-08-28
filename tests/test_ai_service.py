"""Service-layer tests for the current AI Copilot architecture."""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import AuthorizationException, NotFoundException, ValidationException
from app.models.message import MessageRole
from app.schemas.ai import (
    AnalyticsQueryRequest,
    ChatRequest,
    ConversationCreate,
    ConversationUpdate,
    PromptCreate,
    PromptRenderRequest,
    RAGQueryRequest,
    SQLQueryRequest,
)
from app.services.chat_service import ChatService
from app.services.embedding_service import EmbeddingService
from app.services.prompt_service import PromptService
from app.services.rag_service import RAGService
from app.services.sql_ai_service import SQLAIService
from app.services.analytics_ai_service import AnalyticsAIService


def conversation_entity(user_id=None, conversation_id=None):
    # user_id mirrors the real Conversation.user_id column (Integer FK to
    # users.id -- see app/models/conversation.py and app/models/user.py),
    # not a UUID. `user_id or 9001` intentionally treats 0 the same as
    # None here, matching the "falsy means unset" convention already used
    # for conversation_id above; no caller in this file passes user_id=0.
    now = __import__("datetime").datetime.utcnow()
    return SimpleNamespace(
        id=conversation_id or uuid.uuid4(),
        user_id=user_id or 9001,
        title="Test chat",
        module="chat",
        is_archived=False,
        created_at=now,
        updated_at=now,
        messages=[],
    )


def message_entity(conversation_id, role, content):
    return SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        role=role,
        content=content,
        tokens_used=0,
        model_used=None,
        created_at=__import__("datetime").datetime.utcnow(),
        usage=None,
    )


@pytest.mark.asyncio
async def test_chat_create_conversation():
    user_id = 101
    entity = conversation_entity(user_id)
    db = MagicMock()
    db.commit = AsyncMock()
    with patch("app.services.chat_service.ConversationRepository") as Repo:
        Repo.return_value.create = AsyncMock(return_value=entity)
        result = await ChatService(db).create_conversation(
            user_id, ConversationCreate(title="Chat", module="chat")
        )
    assert result.id == entity.id
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_list_conversations():
    user_id = 102
    db = MagicMock()
    with patch("app.services.chat_service.ConversationRepository") as Repo:
        Repo.return_value.list_paginated = AsyncMock(return_value=([], 0))
        result = await ChatService(db).list_conversations(
            user_id=user_id, page=1, page_size=20
        )
    assert result.total == 0
    assert result.items == []


@pytest.mark.asyncio
async def test_chat_missing_conversation_raises_not_found():
    db = MagicMock()
    with patch("app.services.chat_service.ConversationRepository") as Repo:
        Repo.return_value.get_by_id = AsyncMock(return_value=None)
        with pytest.raises(NotFoundException):
            await ChatService(db).get_conversation(103, uuid.uuid4())


@pytest.mark.asyncio
async def test_chat_cross_user_access_raises_forbidden():
    user_id = 104
    entity = conversation_entity(999104)  # a different user's conversation
    db = MagicMock()
    with patch("app.services.chat_service.ConversationRepository") as Repo:
        Repo.return_value.get_by_id = AsyncMock(return_value=entity)
        with pytest.raises(AuthorizationException):
            await ChatService(db).get_conversation(user_id, entity.id)


@pytest.mark.asyncio
async def test_chat_update_conversation():
    user_id = 105
    entity = conversation_entity(user_id)
    db = MagicMock()
    db.commit = AsyncMock()
    with patch("app.services.chat_service.ConversationRepository") as Repo:
        Repo.return_value.get_by_id = AsyncMock(return_value=entity)
        Repo.return_value.update = AsyncMock(return_value=entity)
        result = await ChatService(db).update_conversation(
            user_id, entity.id, ConversationUpdate(title="Renamed")
        )
    assert result.title == "Test chat"
    Repo.return_value.update.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_delete_conversation():
    user_id = 106
    entity = conversation_entity(user_id)
    db = MagicMock()
    db.commit = AsyncMock()
    with patch("app.services.chat_service.ConversationRepository") as Repo:
        Repo.return_value.get_by_id = AsyncMock(return_value=entity)
        Repo.return_value.delete = AsyncMock()
        await ChatService(db).delete_conversation(user_id, entity.id)
    Repo.return_value.delete.assert_awaited_once_with(entity)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_prompt_render_success():
    db = MagicMock()
    prompt = SimpleNamespace(
        id=uuid.uuid4(),
        name="Greeting",
        description=None,
        category="chat",
        template_text="Hello {{name}}",
        variables=["name"],
        is_active=True,
        created_at=__import__("datetime").datetime.utcnow(),
        updated_at=__import__("datetime").datetime.utcnow(),
    )
    with patch("app.services.prompt_service.PromptRepository") as Repo:
        Repo.return_value.get_by_id = AsyncMock(return_value=prompt)
        result = await PromptService(db).render_prompt(
            prompt.id, PromptRenderRequest(variables={"name": "Surya"})
        )
    assert result.rendered_text == "Hello Surya"


def test_prompt_validation_rejects_undeclared_variable():
    with pytest.raises(ValidationException):
        PromptService._validate_template("Hello {{name}}", [])


def test_prompt_interpolation_rejects_missing_value():
    with pytest.raises(ValidationException):
        PromptService._interpolate("Hello {{name}}", {})


@pytest.mark.asyncio
async def test_prompt_missing_record_raises_not_found():
    db = MagicMock()
    with patch("app.services.prompt_service.PromptRepository") as Repo:
        Repo.return_value.get_by_id = AsyncMock(return_value=None)
        with pytest.raises(NotFoundException):
            await PromptService(db).get_prompt(uuid.uuid4())


@pytest.mark.asyncio
async def test_rag_empty_question_is_rejected():
    db = MagicMock()
    payload = RAGQueryRequest(question=" ")
    with pytest.raises(ValidationException):
        await RAGService(db).query(uuid.uuid4(), payload)


@pytest.mark.asyncio
async def test_rag_no_matches_returns_safe_fallback():
    db = MagicMock()
    usage = MagicMock()
    usage.log_usage = AsyncMock()
    with patch("app.services.rag_service.EmbeddingService") as Emb, \
         patch("app.services.rag_service.KnowledgeChunkRepository") as Chunks, \
         patch("app.services.rag_service.AIUsageService", return_value=usage):
        Emb.return_value.generate_embedding = AsyncMock(return_value=[0.1, 0.2])
        Chunks.return_value.similarity_search = AsyncMock(return_value=[])
        result = await RAGService(db).query(
            uuid.uuid4(), RAGQueryRequest(question="unknown", top_k=3)
        )
    assert result.sources == []
    assert "couldn't find" in result.answer.lower()
    usage.log_usage.assert_awaited_once()


def test_sql_sanitize_removes_code_fences():
    assert SQLAIService._sanitize_sql("```sql\nSELECT 1;\n```") == "SELECT 1"


def test_sql_validate_rejects_mutation():
    with pytest.raises(ValidationException):
        SQLAIService._validate_sql("DROP TABLE users")


def test_sql_validate_adds_limit():
    assert SQLAIService._validate_sql("SELECT * FROM properties") == "SELECT * FROM properties LIMIT 500"


@pytest.mark.asyncio
async def test_sql_query_success():
    db = MagicMock()
    usage = MagicMock()
    usage.log_usage = AsyncMock()
    provider = MagicMock()
    provider.complete = AsyncMock(return_value={
        "content": "SELECT COUNT(*) FROM properties",
        "prompt_tokens": 5,
        "completion_tokens": 4,
        "model": "test-model",
    })
    with patch("app.services.sql_ai_service.AIProviderClient", return_value=provider), \
         patch("app.services.sql_ai_service.AIUsageService", return_value=usage):
        result = await SQLAIService(db).query(
            uuid.uuid4(), SQLQueryRequest(question="How many properties?")
        )
    assert result.sql.endswith("LIMIT 500")
    usage.log_usage.assert_awaited_once()


@pytest.mark.asyncio
async def test_analytics_empty_dataset_rejected():
    db = MagicMock()
    with pytest.raises(Exception):
        AnalyticsQueryRequest(question="trend", dataset=[])


@pytest.mark.asyncio
async def test_analytics_empty_question_rejected():
    db = MagicMock()
    payload = AnalyticsQueryRequest(question=" ", dataset=[{"x": 1}])
    with pytest.raises(ValidationException):
        await AnalyticsAIService(db).analyze(uuid.uuid4(), payload)


@pytest.mark.asyncio
async def test_analytics_success():
    db = MagicMock()
    usage = MagicMock()
    usage.log_usage = AsyncMock()
    provider = MagicMock()
    provider.complete = AsyncMock(return_value={
        "content": "Sales increased.",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "model": "test-model",
    })
    with patch("app.services.analytics_ai_service.AIProviderClient", return_value=provider), \
         patch("app.services.analytics_ai_service.AIUsageService", return_value=usage):
        result = await AnalyticsAIService(db).analyze(
            uuid.uuid4(), AnalyticsQueryRequest(question="trend?", dataset=[{"sales": 10}])
        )
    assert result.insights == "Sales increased."
    assert result.usage.total_tokens == 15


@pytest.mark.asyncio
async def test_embedding_rejects_blank_text():
    service = EmbeddingService()
    with pytest.raises(ValidationException):
        await service.generate_embedding(" ")


@pytest.mark.asyncio
async def test_embedding_batches_large_input():
    service = EmbeddingService()
    with patch.object(service, "_call_embedding_api", new=AsyncMock(side_effect=lambda batch: [[float(i)] for i in range(len(batch))])) as call:
        values = await service.generate_embeddings_batch([f"text-{i}" for i in range(65)])
    assert len(values) == 65
    assert call.await_count == 2