"""
API router for the AI Copilot module.
Contains HTTP endpoint definitions only. All business logic lives in the
service layer; all persistence lives in the repository layer.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    AuthorizationException,
    ConflictException,
    ExternalServiceException,
    NotFoundException,
    ValidationException,
)
from app.core.dependencies import (
    get_db,
    RoleChecker,
    UserRole,
    get_current_user,
)
from app.models.user import User
from app.schemas.ai import (
    AIUsageLogResponse,
    AnalyticsQueryRequest,
    AnalyticsQueryResponse,
    ChatRequest,
    ChatResponse,
    ConversationCreate,
    ConversationDetailResponse,
    ConversationResponse,
    ConversationUpdate,
    KnowledgeDocumentResponse,
    MessageResponse,
    PromptCreate,
    PromptRenderRequest,
    PromptRenderResponse,
    PromptResponse,
    PromptUpdate,
    RAGQueryRequest,
    RAGQueryResponse,
    SQLQueryRequest,
    SQLQueryResponse,
)
from app.schemas.common import PaginatedResponse
from app.services.ai_service import AIUsageService
from app.services.analytics_ai_service import AnalyticsAIService
from app.services.chat_service import ChatService
from app.services.document_ai_service import DocumentAIService
from app.services.prompt_service import PromptService
from app.services.rag_service import RAGService
from app.services.sql_ai_service import SQLAIService

router = APIRouter(prefix="/ai", tags=["AI Copilot"])

require_admin = RoleChecker([UserRole.ADMIN])
require_admin_or_manager = RoleChecker([UserRole.ADMIN, UserRole.MANAGER])
require_any_role = RoleChecker([UserRole.ADMIN, UserRole.MANAGER, UserRole.SALES_AGENT])


@contextmanager
def _translate_domain_exceptions():
    """Maps domain/service-layer exceptions to appropriate HTTP responses."""
    try:
        yield
    except NotFoundException as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except AuthorizationException as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ConflictException as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValidationException as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except ExternalServiceException as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc


# --------------------------------------------------------------------------
# Conversation Management
# --------------------------------------------------------------------------


@router.post(
    "/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new AI conversation",
)
async def create_conversation(
    payload: ConversationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_any_role),
) -> ConversationResponse:
    with _translate_domain_exceptions():
        return await ChatService(db).create_conversation(current_user.id, payload)


@router.get(
    "/conversations",
    response_model=PaginatedResponse[ConversationResponse],
    summary="List AI conversations for the current user",
)
async def list_conversations(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    module: Optional[str] = Query(None, description="Filter by module: chat, rag, sql, analytics"),
    is_archived: Optional[bool] = Query(None, description="Filter by archived status"),
    search: Optional[str] = Query(None, description="Search conversations by title"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_any_role),
) -> PaginatedResponse[ConversationResponse]:
    with _translate_domain_exceptions():
        return await ChatService(db).list_conversations(
            user_id=current_user.id,
            page=page,
            page_size=page_size,
            module=module,
            is_archived=is_archived,
            search=search,
        )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
    summary="Get a single AI conversation with its message history",
)
async def get_conversation(
    conversation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_any_role),
) -> ConversationDetailResponse:
    with _translate_domain_exceptions():
        return await ChatService(db).get_conversation(current_user.id, conversation_id)


@router.patch(
    "/conversations/{conversation_id}",
    response_model=ConversationResponse,
    summary="Update an AI conversation (rename, archive, etc.)",
)
async def update_conversation(
    conversation_id: uuid.UUID,
    payload: ConversationUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_any_role),
) -> ConversationResponse:
    with _translate_domain_exceptions():
        return await ChatService(db).update_conversation(
            current_user.id, conversation_id, payload
        )


@router.delete(
    "/conversations/{conversation_id}",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an AI conversation",
)
async def delete_conversation(
    conversation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_any_role),
) -> None:
    with _translate_domain_exceptions():
        await ChatService(db).delete_conversation(current_user.id, conversation_id)


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=PaginatedResponse[MessageResponse],
    summary="List messages within a conversation",
)
async def list_messages(
    conversation_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None, description="Search messages by content"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_any_role),
) -> PaginatedResponse[MessageResponse]:
    with _translate_domain_exceptions():
        return await ChatService(db).list_messages(
            user_id=current_user.id,
            conversation_id=conversation_id,
            page=page,
            page_size=page_size,
            search=search,
        )


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=ChatResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Send a message in a conversation and receive an AI reply",
)
async def send_message(
    conversation_id: uuid.UUID,
    payload: ChatRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_any_role),
) -> ChatResponse:
    with _translate_domain_exceptions():
        return await ChatService(db).send_message(current_user.id, conversation_id, payload)


# --------------------------------------------------------------------------
# Prompt Management
# --------------------------------------------------------------------------


@router.post(
    "/prompts",
    response_model=PromptResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a reusable AI prompt template",
)
async def create_prompt(
    payload: PromptCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_admin_or_manager),
) -> PromptResponse:
    with _translate_domain_exceptions():
        return await PromptService(db).create_prompt(current_user.id, payload)


@router.get(
    "/prompts",
    response_model=PaginatedResponse[PromptResponse],
    summary="List AI prompt templates",
)
async def list_prompts(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    category: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    search: Optional[str] = Query(None, description="Search by name or description"),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_any_role),
) -> PaginatedResponse[PromptResponse]:
    with _translate_domain_exceptions():
        return await PromptService(db).list_prompts(
            page=page,
            page_size=page_size,
            category=category,
            is_active=is_active,
            search=search,
        )


@router.get(
    "/prompts/{prompt_id}",
    response_model=PromptResponse,
    summary="Get a single AI prompt template",
)
async def get_prompt(
    prompt_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_any_role),
) -> PromptResponse:
    with _translate_domain_exceptions():
        return await PromptService(db).get_prompt(prompt_id)


@router.patch(
    "/prompts/{prompt_id}",
    response_model=PromptResponse,
    summary="Update an AI prompt template",
)
async def update_prompt(
    prompt_id: uuid.UUID,
    payload: PromptUpdate,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_admin_or_manager),
) -> PromptResponse:
    with _translate_domain_exceptions():
        return await PromptService(db).update_prompt(prompt_id, payload)


@router.delete(
    "/prompts/{prompt_id}",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an AI prompt template",
)
async def delete_prompt(
    prompt_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_admin_or_manager),
) -> None:
    with _translate_domain_exceptions():
        await PromptService(db).delete_prompt(prompt_id)


@router.post(
    "/prompts/{prompt_id}/render",
    response_model=PromptRenderResponse,
    summary="Render an AI prompt template with variable substitution",
)
async def render_prompt(
    prompt_id: uuid.UUID,
    payload: PromptRenderRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_any_role),
) -> PromptRenderResponse:
    with _translate_domain_exceptions():
        return await PromptService(db).render_prompt(prompt_id, payload)


# --------------------------------------------------------------------------
# Document Upload Management (Knowledge Base / RAG source documents)
# --------------------------------------------------------------------------


@router.post(
    "/documents/upload",
    response_model=KnowledgeDocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document into the AI knowledge base",
)
async def upload_document(
    title: str = Query(..., min_length=1, max_length=255),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_admin_or_manager),
) -> KnowledgeDocumentResponse:
    with _translate_domain_exceptions():
        return await DocumentAIService(db).upload_document(current_user.id, title, file)


@router.get(
    "/documents",
    response_model=PaginatedResponse[KnowledgeDocumentResponse],
    summary="List knowledge base documents",
)
async def list_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(
        None, alias="status", description="Filter by status: pending, processing, completed, failed"
    ),
    file_type: Optional[str] = Query(None),
    search: Optional[str] = Query(None, description="Search by document title"),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_any_role),
) -> PaginatedResponse[KnowledgeDocumentResponse]:
    with _translate_domain_exceptions():
        return await DocumentAIService(db).list_documents(
            page=page,
            page_size=page_size,
            status=status_filter,
            file_type=file_type,
            search=search,
        )


@router.get(
    "/documents/{document_id}",
    response_model=KnowledgeDocumentResponse,
    summary="Get a single knowledge base document",
)
async def get_document(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_any_role),
) -> KnowledgeDocumentResponse:
    with _translate_domain_exceptions():
        return await DocumentAIService(db).get_document(document_id)


@router.delete(
    "/documents/{document_id}",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a knowledge base document and its indexed chunks",
)
async def delete_document(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_admin_or_manager),
) -> None:
    with _translate_domain_exceptions():
        await DocumentAIService(db).delete_document(document_id)


# --------------------------------------------------------------------------
# RAG Query Flow
# --------------------------------------------------------------------------


@router.post(
    "/rag/query",
    response_model=RAGQueryResponse,
    summary="Ask a question answered via Retrieval-Augmented Generation over the knowledge base",
)
async def rag_query(
    payload: RAGQueryRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_any_role),
) -> RAGQueryResponse:
    with _translate_domain_exceptions():
        return await RAGService(db).query(current_user.id, payload)


# --------------------------------------------------------------------------
# SQL AI Endpoint
# --------------------------------------------------------------------------


@router.post(
    "/sql/query",
    response_model=SQLQueryResponse,
    summary="Translate a natural language question into a safe, read-only SQL query",
)
async def sql_ai_query(
    payload: SQLQueryRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_admin_or_manager),
) -> SQLQueryResponse:
    with _translate_domain_exceptions():
        return await SQLAIService(db).query(current_user.id, payload)


# --------------------------------------------------------------------------
# Analytics AI Endpoint
# --------------------------------------------------------------------------


@router.post(
    "/analytics/query",
    response_model=AnalyticsQueryResponse,
    summary="Generate AI-powered analytical insights over a supplied dataset",
)
async def analytics_ai_query(
    payload: AnalyticsQueryRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_any_role),
) -> AnalyticsQueryResponse:
    with _translate_domain_exceptions():
        return await AnalyticsAIService(db).analyze(current_user.id, payload)


# --------------------------------------------------------------------------
# AI Usage Logging
# --------------------------------------------------------------------------


@router.get(
    "/usage-logs",
    response_model=PaginatedResponse[AIUsageLogResponse],
    summary="List AI usage logs (admin only)",
)
async def list_usage_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user_id: Optional[uuid.UUID] = Query(None),
    module: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    search: Optional[str] = Query(None, description="Search by action name"),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_admin),
) -> PaginatedResponse[AIUsageLogResponse]:
    with _translate_domain_exceptions():
        return await AIUsageService(db).list_usage_logs(
            page=page,
            page_size=page_size,
            user_id=user_id,
            module=module,
            status=status_filter,
            search=search,
            date_from=date_from,
            date_to=date_to,
        )


@router.get(
    "/usage-logs/summary",
    summary="Get aggregated AI usage totals (tokens, cost, requests)",
)
async def get_usage_summary(
    user_id: Optional[uuid.UUID] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_admin_or_manager),
) -> dict:
    with _translate_domain_exceptions():
        return await AIUsageService(db).get_usage_summary(
            user_id=user_id, date_from=date_from, date_to=date_to
        )