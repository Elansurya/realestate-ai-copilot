"""SQLAlchemy model for vector embeddings of knowledge document chunks.

Requires the ``pgvector`` PostgreSQL extension and the ``pgvector`` Python
package (``pgvector.sqlalchemy``) to persist and index dense vector data.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.knowledge_document import KnowledgeDocument

EMBEDDING_DIMENSION = 1536
"""Dimensionality of the embedding vectors, matching the configured embedding model."""


class Embedding(Base):
    """Represents a vector embedding for a single chunk of a knowledge document.

    Attributes:
        id: Primary key UUID identifier.
        document_id: Foreign key referencing the parent knowledge document.
        chunk_index: Zero-based ordinal position of the chunk within the document.
        chunk_text: Raw text content of the chunk that was embedded.
        embedding_vector: Dense vector representation of the chunk text.
        token_count: Number of tokens contained in the chunk.
        meta_data: Arbitrary JSONB metadata (page number, section heading).
        created_at: Timestamp of record creation.
        updated_at: Timestamp of last record update.
        document: The parent KnowledgeDocument record.
    """

    __tablename__ = "embeddings"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "chunk_index", name="uq_embeddings_document_id_chunk_index"
        ),
        Index("ix_embeddings_document_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_vector: Mapped[list[float]] = mapped_column(
        Vector(EMBEDDING_DIMENSION), nullable=False
    )
    token_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
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

    document: Mapped["KnowledgeDocument"] = relationship(
        "KnowledgeDocument", back_populates="embeddings", lazy="joined"
    )

    def __repr__(self) -> str:
        """Return a debug-friendly representation of the embedding."""
        return (
            f"<Embedding id={self.id} document_id={self.document_id} "
            f"chunk_index={self.chunk_index}>"
        )