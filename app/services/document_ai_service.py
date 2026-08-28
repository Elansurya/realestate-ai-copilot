"""
Service layer for AI knowledge-base document upload and processing.
Contains business logic only.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Optional

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import NotFoundException, ValidationException
from app.repositories.knowledge_repository import (
    KnowledgeChunkRepository,
    KnowledgeDocumentRepository,
)
from app.schemas.ai import KnowledgeDocumentCreate, KnowledgeDocumentResponse
from app.schemas.common import PaginatedResponse
from app.services.embedding_service import EmbeddingService

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".csv"}
MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024
CHUNK_SIZE_CHARS = 1200
CHUNK_OVERLAP_CHARS = 150


class DocumentAIService:
    """Business logic for uploading, processing, and indexing knowledge documents."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._documents = KnowledgeDocumentRepository(db)
        self._chunks = KnowledgeChunkRepository(db)
        self._embeddings = EmbeddingService()
        self._settings = get_settings()

    async def upload_document(
        self, uploaded_by: int, title: str, file: UploadFile
    ) -> KnowledgeDocumentResponse:
        extension = os.path.splitext(file.filename or "")[1].lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise ValidationException(
                f"Unsupported file type '{extension}'. Allowed types: "
                f"{', '.join(sorted(ALLOWED_EXTENSIONS))}"
            )

        content = await file.read()
        if len(content) > MAX_FILE_SIZE_BYTES:
            raise ValidationException("File exceeds the maximum allowed size of 25MB.")
        if len(content) == 0:
            raise ValidationException("Uploaded file is empty.")

        storage_dir = self._settings.AI_DOCUMENT_STORAGE_PATH
        os.makedirs(storage_dir, exist_ok=True)
        stored_filename = f"{uuid.uuid4()}{extension}"
        stored_path = os.path.join(storage_dir, stored_filename)
        with open(stored_path, "wb") as destination:
            destination.write(content)

        document = await self._documents.create(
            uploaded_by,
            KnowledgeDocumentCreate(
                title=title,
                file_name=file.filename or stored_filename,
                file_path=stored_path,
                file_type=extension.lstrip("."),
                file_size=len(content),
                doc_metadata={},
            ),
        )
        await self._db.commit()

        try:
            await self._process_document(document.id, stored_path, extension)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to process document %s", document.id)
            document = await self._documents.update_status(
                document, status="failed", error_message=str(exc)
            )
            await self._db.commit()
            return KnowledgeDocumentResponse.model_validate(document)

        refreshed = await self._documents.get_by_id(document.id)
        return KnowledgeDocumentResponse.model_validate(refreshed)

    async def get_document(self, document_id: uuid.UUID) -> KnowledgeDocumentResponse:
        entity = await self._documents.get_by_id(document_id)
        if entity is None:
            raise NotFoundException(f"Document {document_id} was not found.")
        return KnowledgeDocumentResponse.model_validate(entity)

    async def list_documents(
        self,
        *,
        page: int,
        page_size: int,
        status: Optional[str] = None,
        file_type: Optional[str] = None,
        search: Optional[str] = None,
        uploaded_by: Optional[int] = None,
    ) -> PaginatedResponse[KnowledgeDocumentResponse]:
        items, total = await self._documents.list_paginated(
            page=page,
            page_size=page_size,
            status=status,
            file_type=file_type,
            search=search,
            uploaded_by=uploaded_by,
        )
        return PaginatedResponse[KnowledgeDocumentResponse](
            items=[KnowledgeDocumentResponse.model_validate(item) for item in items],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=(total + page_size - 1) // page_size if page_size else 0,
        )

    async def delete_document(self, document_id: uuid.UUID) -> None:
        entity = await self._documents.get_by_id(document_id)
        if entity is None:
            raise NotFoundException(f"Document {document_id} was not found.")

        await self._chunks.delete_by_document(document_id)
        if entity.file_path and os.path.exists(entity.file_path):
            try:
                os.remove(entity.file_path)
            except OSError:
                logger.warning("Could not remove file %s from disk.", entity.file_path)

        await self._documents.delete(entity)
        await self._db.commit()

    async def _process_document(
        self, document_id: uuid.UUID, file_path: str, extension: str
    ) -> None:
        document = await self._documents.get_by_id(document_id)
        await self._documents.update_status(document, status="processing")
        await self._db.commit()

        text = self._extract_text(file_path, extension)
        if not text.strip():
            raise ValidationException("No extractable text content was found in the document.")

        chunks = self._split_into_chunks(text)
        embeddings = await self._embeddings.generate_embeddings_batch(chunks)

        chunk_payloads = [
            {
                "content": chunk_text,
                "embedding": embedding,
                "chunk_index": idx,
                "token_count": len(chunk_text.split()),
            }
            for idx, (chunk_text, embedding) in enumerate(zip(chunks, embeddings))
        ]
        await self._chunks.bulk_create(document_id, chunk_payloads)

        document = await self._documents.get_by_id(document_id)
        await self._documents.update_status(
            document, status="completed", chunk_count=len(chunk_payloads)
        )
        await self._db.commit()

    @staticmethod
    def _extract_text(file_path: str, extension: str) -> str:
        if extension in (".txt", ".md", ".csv"):
            with open(file_path, "r", encoding="utf-8", errors="ignore") as handle:
                return handle.read()

        if extension == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(file_path)
            return "\n".join(page.extract_text() or "" for page in reader.pages)

        if extension == ".docx":
            import docx

            document = docx.Document(file_path)
            return "\n".join(paragraph.text for paragraph in document.paragraphs)

        raise ValidationException(f"Unsupported file type for extraction: {extension}")

    @staticmethod
    def _split_into_chunks(text: str) -> list[str]:
        normalized = " ".join(text.split())
        if len(normalized) <= CHUNK_SIZE_CHARS:
            return [normalized]

        chunks: list[str] = []
        start = 0
        while start < len(normalized):
            end = min(start + CHUNK_SIZE_CHARS, len(normalized))
            chunks.append(normalized[start:end])
            if end == len(normalized):
                break
            start = end - CHUNK_OVERLAP_CHARS
        return chunks