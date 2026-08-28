"""Pydantic schemas for the Retrieval-Augmented Generation (RAG) subsystem."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.knowledge_document import DocumentSourceType, DocumentStatus


class KnowledgeDocumentCreate(BaseModel):
    """Payload for registering a new knowledge base document for ingestion.

    Attributes:
        title: Human readable title of the document.
        source_type: Type of originating source for the document.
        source_uri: URI, file path, or external reference to the source.
        meta_data: Optional arbitrary metadata payload.
    """

    model_config = ConfigDict(from_attributes=True)

    title: str = Field(min_length=1, max_length=255)
    source_type: DocumentSourceType
    source_uri: str = Field(min_length=1, max_length=2048)
    meta_data: Optional[dict] = None

    @field_validator("source_uri")
    @classmethod
    def validate_source_uri(cls, value: str) -> str:
        """Ensure the source URI is not blank after stripping whitespace.

        Args:
            value: Raw source URI supplied by the caller.

        Returns:
            The stripped source URI.

        Raises:
            ValueError: If the source URI is blank after stripping.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("source_uri must not be blank")
        return stripped


class KnowledgeDocumentRead(BaseModel):
    """Representation of a persisted knowledge document returned to API consumers.

    Attributes:
        id: Unique identifier of the document.
        title: Human readable title of the document.
        source_type: Type of originating source for the document.
        source_uri: URI, file path, or external reference to the source.
        status: Current ingestion/indexing status.
        size_bytes: Size of the raw source content in bytes.
        created_at: Timestamp of document creation.
        updated_at: Timestamp of last document update.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    source_type: DocumentSourceType
    source_uri: str
    status: DocumentStatus
    size_bytes: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class EmbeddingChunkCreate(BaseModel):
    """Payload representing a single chunk to be embedded and persisted.

    Attributes:
        chunk_index: Zero-based ordinal position of the chunk within the document.
        chunk_text: Raw text content of the chunk to be embedded.
        token_count: Number of tokens contained in the chunk.
    """

    model_config = ConfigDict(from_attributes=True)

    chunk_index: int = Field(ge=0)
    chunk_text: str = Field(min_length=1)
    token_count: int = Field(default=0, ge=0)


class RAGQueryRequest(BaseModel):
    """Request payload for performing a retrieval-augmented query.

    Attributes:
        query_text: Natural language question to answer using retrieved context.
        conversation_id: Optional identifier of an associated conversation.
        top_k: Maximum number of chunks to retrieve for context.
        similarity_threshold: Minimum cosine similarity score for a chunk to qualify.
        document_ids: Optional restriction to a specific subset of documents.
    """

    model_config = ConfigDict(from_attributes=True)

    query_text: str = Field(min_length=1, max_length=4096)
    conversation_id: Optional[UUID] = None
    top_k: int = Field(default=5, gt=0, le=50)
    similarity_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    document_ids: Optional[list[UUID]] = None

    @field_validator("query_text")
    @classmethod
    def validate_query_text(cls, value: str) -> str:
        """Ensure the query text is not blank after stripping whitespace.

        Args:
            value: Raw query text supplied by the caller.

        Returns:
            The stripped query text.

        Raises:
            ValueError: If the query text is blank after stripping.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("query_text must not be blank")
        return stripped


class RetrievedChunk(BaseModel):
    """A single retrieved knowledge chunk supporting a RAG response.

    Attributes:
        embedding_id: Identifier of the source embedding record.
        document_id: Identifier of the parent knowledge document.
        document_title: Human readable title of the parent document.
        chunk_text: Raw text content of the retrieved chunk.
        similarity_score: Cosine similarity score between query and chunk.
    """

    model_config = ConfigDict(from_attributes=True)

    embedding_id: UUID
    document_id: UUID
    document_title: str
    chunk_text: str
    similarity_score: float = Field(ge=0.0, le=1.0)


class RAGQueryResponse(BaseModel):
    """Response payload containing a generated answer and its supporting context.

    Attributes:
        answer: Generated natural language answer.
        retrieved_chunks: Ordered list of chunks used to ground the answer.
        model_name: Name of the AI model that generated the answer.
    """

    model_config = ConfigDict(from_attributes=True)

    answer: str
    retrieved_chunks: list[RetrievedChunk] = Field(default_factory=list)
    model_name: str

    @model_validator(mode="after")
    def validate_chunks_present_when_answer_grounded(self) -> "RAGQueryResponse":
        """Ensure a non-empty answer references at least one retrieved chunk.

        Returns:
            The validated RAGQueryResponse instance.

        Raises:
            ValueError: If the answer is non-empty but no chunks were retrieved.
        """
        if self.answer.strip() and len(self.retrieved_chunks) == 0:
            raise ValueError(
                "retrieved_chunks must not be empty when a grounded answer is returned"
            )
        return self