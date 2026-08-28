"""Unit tests for the AI repository layer against the current backend architecture.

These tests intentionally use an AsyncMock session.  They verify repository
behavior and generated SQL expressions without requiring a second test DB.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.ai_usage import AIFeature, AIUsageStatus
from app.models.message import MessageRole
from app.repositories.ai_repository import AIUsageRepository
from app.repositories.conversation_repository import ConversationRepository, MessageRepository
from app.repositories.knowledge_repository import KnowledgeChunkRepository, KnowledgeDocumentRepository
from app.repositories.prompt_repository import PromptRepository
from app.schemas.ai import (
    ConversationCreate,
    ConversationUpdate,
    KnowledgeDocumentCreate,
    MessageCreate,
    PromptCreate,
    PromptUpdate,
)


@pytest.fixture
def db():
    session = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.delete = AsyncMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock()
    session.add = MagicMock()
    session.add_all = MagicMock()
    return session


def result(*, scalar=None, items=None, one=None):
    value = MagicMock()
    value.scalar_one_or_none.return_value = scalar
    value.scalar_one.return_value = scalar if scalar is not None else 0
    value.scalars.return_value.all.return_value = list(items or [])
    value.one.return_value = one
    value.__iter__.return_value = iter([])
    return value


@pytest.mark.asyncio
async def test_conversation_create_persists_entity(db):
    repo = ConversationRepository(db)
    payload = ConversationCreate(title="Test chat", module="chat")
    entity = await repo.create(uuid.uuid4(), payload)
    assert entity.title == "Test chat"
    assert entity.module == "chat"
    db.add.assert_called_once()
    db.flush.assert_awaited_once()
    db.refresh.assert_awaited_once_with(entity)


@pytest.mark.asyncio
async def test_conversation_get_by_id_returns_entity(db):
    conversation_id = uuid.uuid4()
    expected = SimpleNamespace(id=conversation_id)
    db.execute.return_value = result(scalar=expected)
    found = await ConversationRepository(db).get_by_id(conversation_id)
    assert found is expected


@pytest.mark.asyncio
async def test_conversation_get_by_id_missing_returns_none(db):
    db.execute.return_value = result(scalar=None)
    assert await ConversationRepository(db).get_by_id(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_conversation_list_paginated(db):
    items = [SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4())]
    db.execute.side_effect = [result(scalar=2), result(items=items)]
    found, total = await ConversationRepository(db).list_paginated(
        user_id=uuid.uuid4(), page=1, page_size=20
    )
    assert list(found) == items
    assert total == 2


@pytest.mark.asyncio
async def test_conversation_update_uses_only_supplied_fields(db):
    entity = SimpleNamespace(title="Old", is_archived=False, updated_at=datetime.min)
    updated = await ConversationRepository(db).update(
        entity, ConversationUpdate(title="New")
    )
    assert updated.title == "New"
    assert updated.is_archived is False
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_conversation_delete_calls_session(db):
    entity = SimpleNamespace(id=uuid.uuid4())
    await ConversationRepository(db).delete(entity)
    db.delete.assert_awaited_once_with(entity)
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_message_create(db):
    payload = MessageCreate(role=MessageRole.USER, content="hello")
    entity = await MessageRepository(db).create(uuid.uuid4(), payload)
    assert entity.role == MessageRole.USER
    assert entity.content == "hello"
    assert entity.tokens_used == 0


@pytest.mark.asyncio
async def test_message_list_and_count(db):
    messages = [SimpleNamespace(content="a")]
    db.execute.side_effect = [result(scalar=1), result(items=messages)]
    found, total = await MessageRepository(db).list_by_conversation(
        conversation_id=uuid.uuid4(), page=1, page_size=10
    )
    assert list(found) == messages
    assert total == 1


@pytest.mark.asyncio
async def test_message_recent_history_reverses_db_order(db):
    items = [SimpleNamespace(content="new"), SimpleNamespace(content="old")]
    db.execute.return_value = result(items=items)
    history = await MessageRepository(db).get_recent_history(uuid.uuid4(), limit=2)
    assert history == [items[1], items[0]]


@pytest.mark.asyncio
async def test_prompt_create(db):
    payload = PromptCreate(
        name="Greeting", template_text="Hello {{name}}", variables=["name"]
    )
    entity = await PromptRepository(db).create(42, payload)
    assert entity.name == "Greeting"
    assert entity.template_text == "Hello {{name}}"
    assert entity.created_by == 42


@pytest.mark.asyncio
async def test_prompt_get_by_id(db):
    expected = SimpleNamespace(id=uuid.uuid4())
    db.execute.return_value = result(scalar=expected)
    assert await PromptRepository(db).get_by_id(expected.id) is expected


@pytest.mark.asyncio
async def test_prompt_list_paginated(db):
    items = [SimpleNamespace(name="A")]
    db.execute.side_effect = [result(scalar=1), result(items=items)]
    found, total = await PromptRepository(db).list_paginated(page=1, page_size=20)
    assert list(found) == items
    assert total == 1


@pytest.mark.asyncio
async def test_prompt_update(db):
    entity = SimpleNamespace(name="Old", updated_at=datetime.min)
    payload = PromptUpdate(name="New")
    updated = await PromptRepository(db).update(entity, payload)
    assert updated.name == "New"
    db.refresh.assert_awaited_once_with(entity)


@pytest.mark.asyncio
async def test_prompt_delete(db):
    entity = SimpleNamespace(id=uuid.uuid4())
    await PromptRepository(db).delete(entity)
    db.delete.assert_awaited_once_with(entity)
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_knowledge_document_create(db):
    payload = KnowledgeDocumentCreate(
        title="Lease.pdf",
        file_name="lease.pdf",
        file_path="/tmp/lease.pdf",
        file_type="pdf",
        file_size=1234,
    )
    entity = await KnowledgeDocumentRepository(db).create(7, payload)
    assert entity.title == "Lease.pdf"
    assert entity.status == "pending"
    assert entity.uploaded_by == 7


@pytest.mark.asyncio
async def test_knowledge_document_get_missing(db):
    db.execute.return_value = result(scalar=None)
    assert await KnowledgeDocumentRepository(db).get_by_id(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_knowledge_document_list(db):
    items = [SimpleNamespace(title="Lease")]
    db.execute.side_effect = [result(scalar=1), result(items=items)]
    found, total = await KnowledgeDocumentRepository(db).list_paginated(
        page=1, page_size=10, status="pending", search="Lease"
    )
    assert list(found) == items
    assert total == 1


@pytest.mark.asyncio
async def test_knowledge_document_update_status(db):
    entity = SimpleNamespace(status="pending", doc_metadata={}, chunk_count=0, updated_at=datetime.min)
    updated = await KnowledgeDocumentRepository(db).update_status(
        entity, "failed", error_message="bad file", chunk_count=3
    )
    assert updated.status == "failed"
    assert updated.chunk_count == 3
    assert updated.doc_metadata["error"] == "bad file"


@pytest.mark.asyncio
async def test_knowledge_chunk_bulk_create(db):
    repo = KnowledgeChunkRepository(db)
    document_id = uuid.uuid4()
    chunks = [
        {"content": "one", "embedding": [0.1, 0.2], "chunk_index": 0, "token_count": 2},
        {"content": "two", "embedding": [0.2, 0.3], "chunk_index": 1},
    ]
    created = await repo.bulk_create(document_id, chunks)
    assert len(created) == 2
    assert created[0].document_id == document_id
    assert created[1].token_count == 0
    db.add_all.assert_called_once()
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_knowledge_chunk_count(db):
    db.execute.return_value = result(scalar=4)
    assert await KnowledgeChunkRepository(db).count_by_document(uuid.uuid4()) == 4


@pytest.mark.asyncio
async def test_ai_usage_create_computes_total_tokens(db):
    repo = AIUsageRepository(db)
    entity = await repo.create(
        user_id=uuid.uuid4(),
        feature=AIFeature.CHAT,
        model_name="test-model",
        status=AIUsageStatus.SUCCESS,
        prompt_tokens=10,
        completion_tokens=5,
    )
    assert entity.total_tokens == 15
    assert entity.model_name == "test-model"


@pytest.mark.asyncio
async def test_ai_usage_get_by_id(db):
    expected = SimpleNamespace(id=uuid.uuid4())
    db.execute.return_value = result(scalar=expected)
    assert await AIUsageRepository(db).get_by_id(expected.id) is expected


@pytest.mark.asyncio
async def test_ai_usage_list_paginated(db):
    items = [SimpleNamespace(model_name="test-model")]
    db.execute.side_effect = [result(scalar=1), result(items=items)]
    found, total = await AIUsageRepository(db).list_paginated(page=1, page_size=20)
    assert list(found) == items
    assert total == 1


@pytest.mark.asyncio
async def test_ai_usage_summary(db):
    totals = SimpleNamespace(
        total_requests=3,
        total_prompt_tokens=10,
        total_completion_tokens=20,
        total_tokens=30,
        total_cost=1.25,
    )
    db.execute.side_effect = [result(one=totals), result(scalar=1)]
    summary = await AIUsageRepository(db).get_usage_summary()
    assert summary == {
        "total_requests": 3,
        "successful_requests": 2,
        "failed_requests": 1,
        "total_prompt_tokens": 10,
        "total_completion_tokens": 20,
        "total_tokens": 30,
        "total_cost": 1.25,
    }