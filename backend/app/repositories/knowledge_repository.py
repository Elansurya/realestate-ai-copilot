"""
Repository layer for AI knowledge base documents and chunks (RAG).
Contains database operations only. No business logic.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import select, func, and_, desc, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.embedding import Embedding
from app.models.knowledge_document import KnowledgeDocument
from app.schemas.ai import KnowledgeDocumentCreate


class KnowledgeDocumentRepository:
    """Handles all persistence operations for KnowledgeDocument entities."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(
        self, uploaded_by: Optional[int], payload: KnowledgeDocumentCreate
    ) -> KnowledgeDocument:
        entity = KnowledgeDocument(
            id=uuid.uuid4(),
            title=payload.title,
            file_name=payload.file_name,
            file_path=payload.file_path,
            file_type=payload.file_type,
            file_size=payload.file_size,
            status="pending",
            uploaded_by=uploaded_by,
            doc_metadata=payload.doc_metadata or {},
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        self._db.add(entity)
        await self._db.flush()
        await self._db.refresh(entity)
        return entity

    async def get_by_id(self, document_id: uuid.UUID) -> Optional[KnowledgeDocument]:
        result = await self._db.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.id == document_id)
        )
        return result.scalar_one_or_none()

    async def list_paginated(
        self,
        *,
        page: int,
        page_size: int,
        status: Optional[str] = None,
        file_type: Optional[str] = None,
        search: Optional[str] = None,
        uploaded_by: Optional[int] = None,
    ) -> tuple[Sequence[KnowledgeDocument], int]:
        conditions = []
        if status is not None:
            conditions.append(KnowledgeDocument.status == status)
        if file_type is not None:
            conditions.append(KnowledgeDocument.file_type == file_type)
        if uploaded_by is not None:
            conditions.append(KnowledgeDocument.uploaded_by == uploaded_by)
        if search:
            conditions.append(KnowledgeDocument.title.ilike(f"%{search}%"))

        count_query = select(func.count()).select_from(KnowledgeDocument)
        base_query = select(KnowledgeDocument)
        if conditions:
            count_query = count_query.where(and_(*conditions))
            base_query = base_query.where(and_(*conditions))

        total_result = await self._db.execute(count_query)
        total = total_result.scalar_one()

        result = await self._db.execute(
            base_query.order_by(desc(KnowledgeDocument.created_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = result.scalars().all()
        return items, total

    async def update_status(
        self,
        entity: KnowledgeDocument,
        status: str,
        error_message: Optional[str] = None,
        chunk_count: Optional[int] = None,
    ) -> KnowledgeDocument:
        entity.status = status
        if error_message is not None:
            entity.doc_metadata = {**(entity.doc_metadata or {}), "error": error_message}
        if chunk_count is not None:
            entity.chunk_count = chunk_count
        entity.updated_at = datetime.utcnow()
        await self._db.flush()
        await self._db.refresh(entity)
        return entity

    async def delete(self, entity: KnowledgeDocument) -> None:
        await self._db.delete(entity)
        await self._db.flush()


class KnowledgeChunkRepository:
    """Handles all persistence operations for Embedding (document chunk) entities."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def bulk_create(
        self,
        document_id: uuid.UUID,
        chunks: Sequence[dict],
    ) -> Sequence[Embedding]:
        entities = [
            Embedding(
                id=uuid.uuid4(),
                document_id=document_id,
                chunk_text=chunk["content"],
                embedding_vector=chunk["embedding"],
                chunk_index=chunk["chunk_index"],
                token_count=chunk.get("token_count", 0),
                created_at=datetime.utcnow(),
            )
            for chunk in chunks
        ]
        self._db.add_all(entities)
        await self._db.flush()
        return entities

    async def delete_by_document(self, document_id: uuid.UUID) -> None:
        result = await self._db.execute(
            select(Embedding).where(
                Embedding.document_id == document_id
            )
        )
        for chunk in result.scalars().all():
            await self._db.delete(chunk)
        await self._db.flush()

    async def similarity_search(
        self,
        query_embedding: Sequence[float],
        top_k: int = 5,
        document_ids: Optional[Sequence[uuid.UUID]] = None,
    ) -> Sequence[tuple[Embedding, float]]:
        """Return the top-k most similar chunks along with their cosine distance.

        Returns:
            A sequence of (Embedding, cosine_distance) pairs, ordered by
            ascending distance (most similar first). Cosine distance is in
            the range [0, 2]; callers can derive a similarity score via
            ``1 - distance``.
        """
        distance = Embedding.embedding_vector.cosine_distance(query_embedding)
        query = (
            select(Embedding, distance.label("distance"))
            .order_by(distance)
        )
        if document_ids:
            query = query.where(Embedding.document_id.in_(document_ids))
        query = query.limit(top_k)
        result = await self._db.execute(query)
        return [(row.Embedding, row.distance) for row in result]

    async def count_by_document(self, document_id: uuid.UUID) -> int:
        result = await self._db.execute(
            select(func.count())
            .select_from(Embedding)
            .where(Embedding.document_id == document_id)
        )
        return result.scalar_one()