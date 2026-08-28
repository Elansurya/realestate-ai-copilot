"""SQLAlchemy model for RAG knowledge base source documents."""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.embedding import Embedding


class DocumentSourceType(str, enum.Enum):
    """Enumeration of supported knowledge document source types.

    Currently unused by the active upload flow (all documents are
    file uploads), retained for forward compatibility with URL/manual
    ingestion sources.
    """

    PDF = "pdf"
    URL = "url"
    MANUAL = "manual"
    LISTING = "listing"
    CONTRACT = "contract"


class DocumentStatus(str, enum.Enum):
    """Enumeration of ingestion and indexing lifecycle states.

    Matches the free-form status strings written by
    ``KnowledgeDocumentRepository``/``DocumentAIService``
    ("pending", "processing", "completed", "failed").
    """

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class KnowledgeDocument(Base):
    """Represents a source document ingested into the RAG knowledge base.

    Attributes:
        id: Primary key UUID identifier.
        title: Human readable title of the document.
        file_name: Original uploaded file name.
        file_path: Server-side storage path of the uploaded file.
        file_type: File type/extension of the uploaded document.
        file_size: Size of the uploaded file, in bytes.
        status: Current ingestion/indexing status (pending, processing,
            completed, failed).
        chunk_count: Number of indexed chunks produced from the document.
        doc_metadata: Arbitrary JSONB metadata (author, listing id, tags,
            and processing error details).
        uploaded_by: Foreign key referencing the CRM user who uploaded it.
        created_at: Timestamp of record creation.
        updated_at: Timestamp of last record update.
        embeddings: Related Embedding chunk records.
    """

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        Index("ix_knowledge_documents_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_type: Mapped[str] = mapped_column(String(30), nullable=False)
    file_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    doc_metadata: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONB, nullable=True
    )
    uploaded_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    embeddings: Mapped[list["Embedding"]] = relationship(
        "Embedding",
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="Embedding.chunk_index",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        """Return a debug-friendly representation of the knowledge document."""
        return f"<KnowledgeDocument id={self.id} title={self.title} status={self.status}>"