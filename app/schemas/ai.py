"""Pydantic schemas for the AI Copilot module.

This module defines two layers of schemas:

1. Low-level chat completion primitives (TokenUsage, AIModelConfig,
   ChatMessageInput, ChatCompletionRequest/Response, AIErrorResponse) used
   internally when talking to underlying LLM providers.
2. Application-facing schemas (conversations, messages, prompts, knowledge
   documents, RAG/SQL/analytics queries, usage logs) consumed by the
   `/ai` API router.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.message import MessageRole
from app.models.prompt_template import PromptCategory


# --------------------------------------------------------------------------
# Low-level chat completion primitives
# --------------------------------------------------------------------------


class TokenUsage(BaseModel):
    """Token accounting for a single AI model invocation.

    Attributes:
        prompt_tokens: Number of tokens consumed by the prompt/input.
        completion_tokens: Number of tokens generated in the completion/output.
        total_tokens: Sum of prompt and completion tokens.
    """

    model_config = ConfigDict(from_attributes=True)

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_total_tokens(self) -> "TokenUsage":
        """Ensure total_tokens is consistent with prompt and completion tokens.

        Returns:
            The validated TokenUsage instance.

        Raises:
            ValueError: If total_tokens does not equal the sum of its parts.
        """
        expected_total = self.prompt_tokens + self.completion_tokens
        if self.total_tokens != expected_total:
            raise ValueError(
                f"total_tokens ({self.total_tokens}) must equal "
                f"prompt_tokens + completion_tokens ({expected_total})"
            )
        return self


class AIModelConfig(BaseModel):
    """Configuration parameters controlling an AI model invocation.

    Attributes:
        model_name: Identifier of the AI model to invoke.
        temperature: Sampling temperature controlling response randomness.
        max_tokens: Maximum number of tokens to generate.
        top_p: Nucleus sampling probability mass.
    """

    model_config = ConfigDict(from_attributes=True)

    model_name: str = Field(min_length=1, max_length=100)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, gt=0, le=32768)
    top_p: float = Field(default=1.0, gt=0.0, le=1.0)

    @field_validator("model_name")
    @classmethod
    def validate_model_name(cls, value: str) -> str:
        """Normalize and validate the AI model name.

        Args:
            value: Raw model name provided by the caller.

        Returns:
            The stripped model name.

        Raises:
            ValueError: If the model name is blank after stripping.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("model_name must not be blank")
        return stripped


class ChatMessageInput(BaseModel):
    """A single message supplied as input to a chat completion request.

    Attributes:
        role: Role of the message author.
        content: Textual content of the message.
    """

    model_config = ConfigDict(from_attributes=True)

    role: MessageRole
    content: str = Field(min_length=1, max_length=32768)


class ChatCompletionRequest(BaseModel):
    """Request payload for generating a chat completion.

    Attributes:
        conversation_id: Optional identifier of an existing conversation.
        messages: Ordered list of messages forming the conversation context.
        model_config_options: AI model configuration parameters.
        stream: Whether the response should be streamed incrementally.
    """

    model_config = ConfigDict(from_attributes=True)

    conversation_id: Optional[str] = None
    messages: list[ChatMessageInput] = Field(min_length=1)
    model_config_options: AIModelConfig
    stream: bool = False

    @field_validator("messages")
    @classmethod
    def validate_messages_not_empty(
        cls, value: list[ChatMessageInput]
    ) -> list[ChatMessageInput]:
        """Ensure at least one message is present in the request.

        Args:
            value: List of chat messages supplied by the caller.

        Returns:
            The validated list of messages.

        Raises:
            ValueError: If the message list is empty.
        """
        if len(value) == 0:
            raise ValueError("messages must contain at least one entry")
        return value


class ChatCompletionResponse(BaseModel):
    """Response payload returned from a chat completion invocation.

    Attributes:
        content: Generated textual content of the completion.
        role: Role associated with the generated message (typically assistant).
        usage: Token usage accounting for the invocation.
        model_name: Name of the AI model that produced the completion.
        finish_reason: Provider-reported reason generation stopped.
    """

    model_config = ConfigDict(from_attributes=True)

    content: str
    role: MessageRole = MessageRole.ASSISTANT
    usage: TokenUsage
    model_name: str
    finish_reason: str


class AIErrorResponse(BaseModel):
    """Standardized error payload for failed AI operations.

    Attributes:
        error_code: Machine-readable error classification code.
        message: Human readable error description.
        details: Optional structured detail payload.
    """

    model_config = ConfigDict(from_attributes=True)

    error_code: str
    message: str
    details: Optional[dict] = None


# --------------------------------------------------------------------------
# Conversation schemas
# --------------------------------------------------------------------------


class ConversationCreate(BaseModel):
    """Payload for creating a new AI conversation.

    Attributes:
        title: Display title for the conversation.
        module: Which AI module the conversation belongs to
            (chat, rag, sql, analytics).
        metadata: Optional free-form metadata associated with the
            conversation (e.g. seed context, tags).
    """

    model_config = ConfigDict(from_attributes=True)

    title: str = Field(min_length=1, max_length=255)
    module: str = Field(min_length=1, max_length=50)
    metadata: Optional[dict] = None

    @field_validator("module")
    @classmethod
    def validate_module(cls, value: str) -> str:
        """Validate the conversation belongs to a known AI module.

        Args:
            value: Raw module name.

        Returns:
            The normalized (lowercased, stripped) module name.

        Raises:
            ValueError: If the module is not one of the supported modules.
        """
        normalized = value.strip().lower()
        allowed = {"chat", "rag", "sql", "analytics"}
        if normalized not in allowed:
            raise ValueError(f"module must be one of {sorted(allowed)}")
        return normalized


class ConversationUpdate(BaseModel):
    """Payload for updating an existing AI conversation.

    Attributes:
        title: New title for the conversation, if renaming.
        is_archived: New archived status, if archiving/unarchiving.
    """

    model_config = ConfigDict(from_attributes=True)

    title: Optional[str] = Field(default=None, min_length=1, max_length=255)
    is_archived: Optional[bool] = None


class ConversationResponse(BaseModel):
    """Summary representation of an AI conversation.

    Attributes:
        id: Unique identifier of the conversation.
        user_id: Identifier of the user who owns the conversation.
        title: Display title of the conversation.
        module: AI module the conversation belongs to.
        is_archived: Whether the conversation has been archived.
        created_at: Timestamp the conversation was created.
        updated_at: Timestamp the conversation was last updated.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: int
    title: str
    module: str
    is_archived: bool = False
    created_at: datetime
    updated_at: datetime


class MessageCreate(BaseModel):
    """Payload for creating a new message within a conversation.

    Attributes:
        role: Role of the message author.
        content: Textual content of the message.
        tokens_used: Number of tokens consumed by this message.
        model_used: Name of the AI model that generated this message,
            if it was AI-generated.
    """

    model_config = ConfigDict(from_attributes=True)

    role: MessageRole
    content: str = Field(min_length=1, max_length=32768)
    tokens_used: int = Field(default=0, ge=0)
    model_used: Optional[str] = Field(default=None, max_length=100)


class MessageResponse(BaseModel):
    """Representation of a single message within a conversation.

    Attributes:
        id: Unique identifier of the message.
        conversation_id: Identifier of the parent conversation.
        role: Role of the message author.
        content: Textual content of the message.
        usage: Token usage for this message, if it was AI-generated.
        created_at: Timestamp the message was created.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    role: MessageRole
    content: str
    usage: Optional[TokenUsage] = None
    created_at: datetime


class ConversationDetailResponse(ConversationResponse):
    """Detailed representation of an AI conversation including its messages.

    Attributes:
        messages: Ordered list of messages belonging to the conversation.
    """

    model_config = ConfigDict(from_attributes=True)

    messages: list[MessageResponse] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Chat send-message schemas
# --------------------------------------------------------------------------


class ChatRequest(BaseModel):
    """Payload for sending a new message within a conversation.

    Attributes:
        message: Textual content of the user's message.
        system_prompt: Optional override for the system prompt used to
            steer the assistant's reply.
        temperature: Optional override for the sampling temperature used
            to generate the reply.
    """

    model_config = ConfigDict(from_attributes=True)

    message: str = Field(min_length=1, max_length=32768)
    system_prompt: Optional[str] = Field(default=None, max_length=8192)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)


class ChatResponse(BaseModel):
    """Response payload after sending a message and receiving an AI reply.

    Attributes:
        conversation_id: Identifier of the conversation the message belongs to.
        user_message: The stored user message.
        assistant_message: The generated assistant reply.
        usage: Token usage accounting for the AI invocation.
    """

    model_config = ConfigDict(from_attributes=True)

    conversation_id: uuid.UUID
    user_message: MessageResponse
    assistant_message: MessageResponse
    usage: TokenUsage


# --------------------------------------------------------------------------
# Prompt template schemas
# --------------------------------------------------------------------------


class PromptCreate(BaseModel):
    """Payload for creating a reusable AI prompt template.

    Attributes:
        name: Human-readable name of the prompt template.
        description: Optional description of the prompt's purpose.
        category: Functional category the prompt belongs to.
        template_text: The prompt text, containing `{variable}` placeholders.
        variables: Names of the variables expected by the template.
        is_active: Whether the prompt is available for use.
    """

    model_config = ConfigDict(from_attributes=True)

    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=2000)
    category: PromptCategory = Field(default=PromptCategory.CHAT)
    template_text: str = Field(min_length=1, max_length=32768)
    variables: list[str] = Field(default_factory=list)
    is_active: bool = True


class PromptUpdate(BaseModel):
    """Payload for updating an existing AI prompt template.

    Attributes:
        name: New name for the prompt template.
        description: New description for the prompt template.
        category: New functional category for the prompt template.
        template_text: New prompt text.
        variables: New list of expected variable names.
        is_active: New active status.
    """

    model_config = ConfigDict(from_attributes=True)

    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=2000)
    category: Optional[PromptCategory] = None
    template_text: Optional[str] = Field(default=None, min_length=1, max_length=32768)
    variables: Optional[list[str]] = None
    is_active: Optional[bool] = None


class PromptResponse(BaseModel):
    """Representation of an AI prompt template.

    Attributes:
        id: Unique identifier of the prompt template.
        name: Human-readable name of the prompt template.
        description: Description of the prompt's purpose.
        category: Functional category the prompt belongs to.
        template_text: The prompt text, containing `{variable}` placeholders.
        variables: Names of the variables expected by the template.
        is_active: Whether the prompt is available for use.
        created_at: Timestamp the prompt was created.
        updated_at: Timestamp the prompt was last updated.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: Optional[str] = None
    category: PromptCategory
    template_text: str
    variables: list[str] = Field(default_factory=list)
    is_active: bool = True
    created_at: datetime
    updated_at: datetime


class PromptRenderRequest(BaseModel):
    """Payload for rendering a prompt template with variable substitution.

    Attributes:
        variables: Mapping of variable name to substitution value.
    """

    model_config = ConfigDict(from_attributes=True)

    variables: dict[str, str] = Field(default_factory=dict)


class PromptRenderResponse(BaseModel):
    """Response payload containing a rendered prompt.

    Attributes:
        prompt_id: Identifier of the prompt template that was rendered.
        rendered_text: The prompt text after variable substitution.
    """

    model_config = ConfigDict(from_attributes=True)

    prompt_id: uuid.UUID
    rendered_text: str


# --------------------------------------------------------------------------
# Knowledge document schemas
# --------------------------------------------------------------------------


class KnowledgeDocumentCreate(BaseModel):
    """Payload for registering a newly uploaded knowledge base document.

    Attributes:
        title: Display title of the document.
        file_name: Original uploaded file name.
        file_path: Server-side storage path of the uploaded file.
        file_type: File type/extension of the uploaded document.
        file_size: Size of the uploaded file, in bytes.
        doc_metadata: Arbitrary free-form metadata for the document.
    """

    model_config = ConfigDict(from_attributes=True)

    title: str = Field(min_length=1, max_length=255)
    file_name: str
    file_path: str
    file_type: str
    file_size: int = Field(ge=0)
    doc_metadata: Optional[dict] = None


class KnowledgeDocumentResponse(BaseModel):
    """Representation of a document uploaded to the AI knowledge base.

    Attributes:
        id: Unique identifier of the document.
        title: Display title of the document.
        file_name: Original uploaded file name.
        file_type: File type/extension of the uploaded document.
        status: Processing status (pending, processing, completed, failed).
        chunk_count: Number of indexed chunks produced from the document.
        uploaded_by: Identifier of the user who uploaded the document.
        error_message: Error detail if processing failed.
        created_at: Timestamp the document was uploaded.
        updated_at: Timestamp the document was last updated.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    file_name: str
    file_type: str
    status: str
    chunk_count: int = 0
    uploaded_by: Optional[int] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# RAG query schemas
# --------------------------------------------------------------------------


class RAGQueryRequest(BaseModel):
    """Payload for a Retrieval-Augmented Generation query.

    Attributes:
        question: Natural language question to answer.
        conversation_id: Optional existing conversation to attach the query to.
        document_ids: Optional subset of documents to restrict retrieval to.
        top_k: Number of chunks to retrieve for context.
    """

    model_config = ConfigDict(from_attributes=True)

    question: str = Field(min_length=1, max_length=8192)
    conversation_id: Optional[uuid.UUID] = None
    document_ids: Optional[list[uuid.UUID]] = None
    top_k: int = Field(default=5, ge=1, le=50)


class RAGSourceChunk(BaseModel):
    """A single retrieved source chunk backing a RAG answer.

    Attributes:
        document_id: Identifier of the source document.
        document_title: Display title of the source document.
        chunk_text: The retrieved chunk's text content.
        score: Relevance score of the chunk to the query.
    """

    model_config = ConfigDict(from_attributes=True)

    document_id: uuid.UUID
    document_title: str
    chunk_text: str
    score: float = Field(ge=0.0, le=1.0)


class RAGQueryResponse(BaseModel):
    """Response payload for a Retrieval-Augmented Generation query.

    Attributes:
        conversation_id: Conversation the query was recorded under, if any.
        answer: Generated answer text.
        sources: Source chunks used to ground the answer.
        usage: Token usage accounting for the invocation.
    """

    model_config = ConfigDict(from_attributes=True)

    conversation_id: Optional[uuid.UUID] = None
    answer: str
    sources: list[RAGSourceChunk] = Field(default_factory=list)
    usage: TokenUsage


# --------------------------------------------------------------------------
# SQL AI schemas
# --------------------------------------------------------------------------


class SQLQueryRequest(BaseModel):
    """Payload for translating a natural language question into SQL.

    Attributes:
        question: Natural language question describing the desired data.
        conversation_id: Optional existing conversation to attach the query to.
        schema_context: Optional free-text description of the relevant
            tables/columns to ground the generated SQL. When omitted, the
            model generates SQL using only the question.
        execute: Whether the generated, validated SELECT statement should
            also be executed against the database. Defaults to True.
    """

    model_config = ConfigDict(from_attributes=True)

    question: str = Field(min_length=1, max_length=8192)
    conversation_id: Optional[uuid.UUID] = None
    schema_context: Optional[str] = Field(default=None, max_length=8192)
    execute: bool = True

    @field_validator("question")
    @classmethod
    def validate_question(cls, value: str) -> str:
        """Ensure the natural language question is not blank after stripping.

        Args:
            value: Raw question text supplied by the caller.

        Returns:
            The stripped question text.

        Raises:
            ValueError: If the question is blank after stripping.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("question must not be blank")
        return stripped

    @field_validator("schema_context")
    @classmethod
    def validate_schema_context(cls, value: Optional[str]) -> Optional[str]:
        """Normalize schema_context, treating blank strings as unset.

        Args:
            value: Raw schema context text supplied by the caller.

        Returns:
            The stripped schema context, or None if blank/unset.
        """
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class SQLQueryResponse(BaseModel):
    """Response payload containing a generated read-only SQL query.

    Attributes:
        conversation_id: Conversation the query was recorded under, if any.
        sql: The generated, read-only SQL statement.
        explanation: Optional plain-language explanation of the query.
        usage: Token usage accounting for the invocation.
        executed: Whether the generated SQL was actually executed.
        columns: Ordered list of result column names, if executed.
        rows: Result rows as column-name -> value mappings, if executed.
        row_count: Number of rows returned, if executed.
        error: Execution error message, if execution was attempted and failed.
    """

    model_config = ConfigDict(from_attributes=True)

    conversation_id: Optional[uuid.UUID] = None
    sql: str
    explanation: Optional[str] = None
    usage: TokenUsage
    executed: bool = False
    columns: list[str] = Field(default_factory=list)
    rows: list[dict] = Field(default_factory=list)
    row_count: int = Field(default=0, ge=0)
    error: Optional[str] = None

    @field_validator("sql")
    @classmethod
    def validate_read_only(cls, value: str) -> str:
        """Ensure the generated SQL is a read-only statement.

        Args:
            value: Raw SQL text produced by the model.

        Returns:
            The validated SQL text.

        Raises:
            ValueError: If the statement does not start with SELECT/WITH.
        """
        normalized = value.strip().lower()
        if not (normalized.startswith("select") or normalized.startswith("with")):
            raise ValueError("Generated SQL must be a read-only SELECT/WITH statement")
        return value

    @model_validator(mode="after")
    def validate_row_count_matches_rows(self) -> "SQLQueryResponse":
        """Ensure row_count is consistent with the length of the returned rows.

        Returns:
            The validated SQLQueryResponse instance.

        Raises:
            ValueError: If row_count does not match the number of returned rows.
        """
        if self.row_count != len(self.rows):
            raise ValueError(
                f"row_count ({self.row_count}) must equal the number of rows "
                f"returned ({len(self.rows)})"
            )
        return self


# --------------------------------------------------------------------------
# Analytics AI schemas
# --------------------------------------------------------------------------


class AnalyticsQueryRequest(BaseModel):
    """Payload for generating AI-powered analytical insights.

    Attributes:
        question: Natural language analytical question to answer.
        dataset: Supplied tabular dataset to analyze, as a list of records.
        conversation_id: Optional existing conversation to attach the query to.
    """

    model_config = ConfigDict(from_attributes=True)

    question: str = Field(min_length=1, max_length=8192)
    dataset: list[dict] = Field(min_length=1)
    conversation_id: Optional[uuid.UUID] = None


class AnalyticsQueryResponse(BaseModel):
    """Response payload for an AI-powered analytics query.

    Attributes:
        conversation_id: Conversation the query was recorded under, if any.
        insights: Generated analytical insight text.
        usage: Token usage accounting for the invocation.
    """

    model_config = ConfigDict(from_attributes=True)

    conversation_id: Optional[uuid.UUID] = None
    insights: str
    usage: TokenUsage


# --------------------------------------------------------------------------
# AI usage logging schemas
# --------------------------------------------------------------------------


class AIUsageLogResponse(BaseModel):
    """Representation of a single AI usage log entry.

    Attributes:
        id: Unique identifier of the usage log entry.
        user_id: Identifier of the user who triggered the AI invocation.
        module: AI module the invocation belongs to (chat, rag, sql, analytics).
        action: Name of the action/endpoint that was invoked.
        status: Outcome status of the invocation (success, error, etc.).
        usage: Token usage accounting for the invocation.
        cost: Estimated monetary cost of the invocation, if tracked.
        error_message: Error detail if the invocation failed.
        created_at: Timestamp the usage log entry was recorded.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: int
    module: str
    action: str
    status: str
    usage: Optional[TokenUsage] = None
    cost: Optional[float] = Field(default=None, ge=0.0)
    error_message: Optional[str] = None
    created_at: datetime