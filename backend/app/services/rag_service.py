"""
Service layer for the Retrieval-Augmented Generation (RAG) query flow.
Contains business logic only.
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationException
from app.repositories.knowledge_repository import KnowledgeChunkRepository
from app.schemas.ai import (
    ChatMessageInput,
    RAGQueryRequest,
    RAGQueryResponse,
    RAGSourceChunk,
    TokenUsage,
)
from app.services.ai_service import AIProviderClient, AIUsageService
from app.services.embedding_service import EmbeddingService

RAG_SYSTEM_PROMPT = (
    "You are the AI Copilot for an enterprise real estate CRM. Answer the "
    "user's question using ONLY the provided context excerpts. If the "
    "context does not contain enough information to answer confidently, "
    "state that clearly instead of guessing. Cite which excerpt numbers "
    "you used when relevant."
)
DEFAULT_TOP_K = 5
MAX_TOP_K = 20


class RAGService:
    """Business logic for embedding queries, retrieving context, and generating answers."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._chunks = KnowledgeChunkRepository(db)
        self._embeddings = EmbeddingService()
        self._provider = AIProviderClient()
        self._usage = AIUsageService(db)

    async def query(
        self, user_id: int, payload: RAGQueryRequest
    ) -> RAGQueryResponse:
        if not payload.question or not payload.question.strip():
            raise ValidationException("A question is required for RAG queries.")

        top_k = min(payload.top_k or DEFAULT_TOP_K, MAX_TOP_K)

        query_embedding = await self._embeddings.generate_embedding(payload.question)

        document_ids: Optional[list[uuid.UUID]] = (
            payload.document_ids if payload.document_ids else None
        )
        matched_chunks = await self._chunks.similarity_search(
            query_embedding, top_k=top_k, document_ids=document_ids
        )

        if not matched_chunks:
            answer = (
                "I couldn't find any relevant information in the knowledge "
                "base to answer that question."
            )
            zero_usage = TokenUsage(
                prompt_tokens=0, completion_tokens=0, total_tokens=0
            )
            await self._usage.log_usage(
                user_id=user_id,
                module="rag",
                action="query",
                status="success",
                conversation_id=payload.conversation_id,
                usage=zero_usage,
                request_metadata={"matched_chunks": 0},
            )
            return RAGQueryResponse(
                conversation_id=payload.conversation_id,
                answer=answer,
                sources=[],
                usage=zero_usage,
            )

        context_blocks = "\n\n".join(
            f"[Excerpt {idx + 1}]\n{chunk.chunk_text}"
            for idx, (chunk, _distance) in enumerate(matched_chunks)
        )
        user_prompt = (
            f"Context excerpts:\n{context_blocks}\n\n"
            f"Question: {payload.question}"
        )

        try:
            completion = await self._provider.complete(
                [ChatMessageInput(role="user", content=user_prompt)],
                system_prompt=RAG_SYSTEM_PROMPT,
                temperature=0.2,
            )
            status = "success"
        except Exception as exc:  # noqa: BLE001
            await self._usage.log_usage(
                user_id=user_id,
                module="rag",
                action="query",
                status="failure",
                conversation_id=payload.conversation_id,
                error_message=str(exc),
            )
            raise

        usage = TokenUsage(
            prompt_tokens=completion["prompt_tokens"],
            completion_tokens=completion["completion_tokens"],
            total_tokens=completion["prompt_tokens"] + completion["completion_tokens"],
        )

        await self._usage.log_usage(
            user_id=user_id,
            module="rag",
            action="query",
            usage=usage,
            model_used=completion["model"],
            status=status,
            conversation_id=payload.conversation_id,
            request_metadata={"matched_chunks": len(matched_chunks)},
        )

        return RAGQueryResponse(
            conversation_id=payload.conversation_id,
            answer=completion["content"],
            sources=[
                RAGSourceChunk(
                    document_id=chunk.document_id,
                    document_title=chunk.document.title if chunk.document else "",
                    chunk_text=chunk.chunk_text,
                    score=max(0.0, min(1.0, 1.0 - float(distance))),
                )
                for chunk, distance in matched_chunks
            ],
            usage=usage,
        )