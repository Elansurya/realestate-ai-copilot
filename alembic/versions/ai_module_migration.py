"""AI module: conversations, messages, prompt_templates, knowledge_documents,
embeddings, ai_usage_logs.

Revision ID: ai_module_migration
Revises: payment_module_migration
Create Date: 2026-08-01 00:00:00.000000

This migration is PostgreSQL specific. It uses:
  - UUID primary keys (uuid-ossp / gen_random_uuid via pgcrypto)
  - JSONB columns for flexible metadata
  - Foreign keys with ON DELETE behavior
  - Single-column and composite indexes
  - CHECK constraints for enum-like fields and numeric bounds
  - Soft delete support (deleted_at / is_deleted)
  - created_at / updated_at audit columns with server defaults + triggers
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

EMBEDDING_DIMENSION = 1536

# revision identifiers, used by Alembic.
revision = "ai_module_migration"
down_revision = "payment_module_migration"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _timestamp_columns():
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    ]


def _soft_delete_columns():
    return [
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    ]


UPDATED_AT_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION ai_module_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

DROP_UPDATED_AT_TRIGGER_FN = "DROP FUNCTION IF EXISTS ai_module_set_updated_at();"


def _create_updated_at_trigger(table_name: str) -> None:
    op.execute(
        f"""
        CREATE TRIGGER trg_{table_name}_updated_at
        BEFORE UPDATE ON {table_name}
        FOR EACH ROW
        EXECUTE FUNCTION ai_module_set_updated_at();
        """
    )


def _drop_updated_at_trigger(table_name: str) -> None:
    op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_updated_at ON {table_name};")


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------
def upgrade() -> None:
    bind = op.get_bind()

    # Required for gen_random_uuid()
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto";')
    # Required for the pgvector column type used by `embeddings.embedding_vector`
    op.execute('CREATE EXTENSION IF NOT EXISTS "vector";')
    op.execute(UPDATED_AT_TRIGGER_FN)

    # -----------------------------------------------------------------
    # conversations
    # -----------------------------------------------------------------
    op.create_table(
        "conversations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=255), nullable=False, server_default="New Conversation"),
        sa.Column(
            "module",
            sa.String(length=50),
            nullable=False,
            server_default="chat",
        ),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "is_archived",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        *_timestamp_columns(),
        *_soft_delete_columns(),
        sa.CheckConstraint(
            "module IN ('chat','rag','sql','analytics')",
            name="ck_conversations_module",
        ),
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    op.create_index("ix_conversations_is_deleted", "conversations", ["is_deleted"])
    op.create_index(
        "ix_conversations_user_id_module",
        "conversations",
        ["user_id", "module"],
    )
    op.create_index(
        "ix_conversations_created_at",
        "conversations",
        ["created_at"],
    )
    _create_updated_at_trigger("conversations")

    # -----------------------------------------------------------------
    # messages
    # -----------------------------------------------------------------
    op.create_table(
        "messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "tokens_used",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("model_used", sa.String(length=100), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        *_timestamp_columns(),
        *_soft_delete_columns(),
        sa.CheckConstraint(
            "role IN ('user','assistant','system','tool')",
            name="ck_messages_role",
        ),
        sa.CheckConstraint(
            "tokens_used >= 0", name="ck_messages_tokens_used_nonneg"
        ),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_index("ix_messages_is_deleted", "messages", ["is_deleted"])
    op.create_index(
        "ix_messages_conversation_created_at",
        "messages",
        ["conversation_id", "created_at"],
    )
    _create_updated_at_trigger("messages")

    # -----------------------------------------------------------------
    # prompt_templates
    # -----------------------------------------------------------------
    prompt_category_enum = postgresql.ENUM(
        "CHAT",
        "RAG",
        "SQL_GENERATION",
        "ANALYTICS",
        "LEAD_SCORING",
        "PROPERTY_DESCRIPTION",
        "EMAIL_DRAFTING",
        name="prompt_category",
    )
    prompt_category_enum.create(bind, checkfirst=True)

    op.create_table(
        "prompt_templates",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "category",
            postgresql.ENUM(
                "CHAT",
                "RAG",
                "SQL_GENERATION",
                "ANALYTICS",
                "LEAD_SCORING",
                "PROPERTY_DESCRIPTION",
                "EMAIL_DRAFTING",
                name="prompt_category",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("template_text", sa.Text(), nullable=False),
        sa.Column("variables", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_timestamp_columns(),
        *_soft_delete_columns(),
        sa.CheckConstraint("version >= 1", name="ck_prompt_templates_version_positive"),
        sa.UniqueConstraint(
            "name", "version", name="uq_prompt_templates_name_version"
        ),
    )
    op.create_index("ix_prompt_templates_name", "prompt_templates", ["name"])
    op.create_index("ix_prompt_templates_is_deleted", "prompt_templates", ["is_deleted"])
    op.create_index(
        "ix_prompt_templates_category_is_active",
        "prompt_templates",
        ["category", "is_active"],
    )
    _create_updated_at_trigger("prompt_templates")

    # -----------------------------------------------------------------
    # knowledge_documents
    # -----------------------------------------------------------------
    op.create_table(
        "knowledge_documents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "uploaded_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("file_type", sa.String(length=30), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "status IN ('pending','processing','completed','failed')",
            name="ck_knowledge_documents_status",
        ),
        sa.CheckConstraint(
            "file_size IS NULL OR file_size >= 0",
            name="ck_knowledge_documents_file_size_nonneg",
        ),
        sa.CheckConstraint("chunk_count >= 0", name="ck_knowledge_documents_chunk_count_nonneg"),
    )
    op.create_index("ix_knowledge_documents_status", "knowledge_documents", ["status"])
    op.create_index(
        "ix_knowledge_documents_title_trgm",
        "knowledge_documents",
        ["title"],
    )
    _create_updated_at_trigger("knowledge_documents")

    # -----------------------------------------------------------------
    # embeddings
    # -----------------------------------------------------------------
    op.create_table(
        "embeddings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("embedding_vector", Vector(EMBEDDING_DIMENSION), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        *_timestamp_columns(),
        sa.CheckConstraint("chunk_index >= 0", name="ck_embeddings_chunk_index_nonneg"),
        sa.CheckConstraint(
            "token_count >= 0", name="ck_embeddings_token_count_nonneg"
        ),
        sa.UniqueConstraint(
            "document_id", "chunk_index", name="uq_embeddings_document_id_chunk_index"
        ),
    )
    op.create_index("ix_embeddings_document_id", "embeddings", ["document_id"])
    _create_updated_at_trigger("embeddings")

    # -----------------------------------------------------------------
    # ai_usages
    # -----------------------------------------------------------------
    ai_feature_enum = postgresql.ENUM(
        "CHAT", "RAG", "SQL_GENERATION", "ANALYTICS", "EMBEDDING",
        name="ai_feature",
    )
    ai_feature_enum.create(bind, checkfirst=True)
    ai_usage_status_enum = postgresql.ENUM(
        "SUCCESS", "FAILURE", name="ai_usage_status"
    )
    ai_usage_status_enum.create(bind, checkfirst=True)
    ai_feature_enum_ref = postgresql.ENUM(
        "CHAT", "RAG", "SQL_GENERATION", "ANALYTICS", "EMBEDDING",
        name="ai_feature",
        create_type=False,
    )
    ai_usage_status_enum_ref = postgresql.ENUM(
        "SUCCESS", "FAILURE", name="ai_usage_status", create_type=False
    )

    op.create_table(
        "ai_usages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("feature", ai_feature_enum_ref, nullable=False),
        sa.Column("model_name", sa.String(length=100), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "status", ai_usage_status_enum_ref, nullable=False, server_default="SUCCESS"
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("request_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint("prompt_tokens >= 0", name="ck_ai_usages_prompt_tokens_nonneg"),
        sa.CheckConstraint(
            "completion_tokens >= 0", name="ck_ai_usages_completion_tokens_nonneg"
        ),
        sa.CheckConstraint("total_tokens >= 0", name="ck_ai_usages_total_tokens_nonneg"),
        sa.CheckConstraint("cost_usd >= 0", name="ck_ai_usages_cost_nonneg"),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0", name="ck_ai_usages_latency_nonneg"
        ),
    )
    op.create_index("ix_ai_usages_feature", "ai_usages", ["feature"])
    op.create_index(
        "ix_ai_usages_user_id_created_at",
        "ai_usages",
        ["user_id", "created_at"],
    )
    _create_updated_at_trigger("ai_usages")

    del bind  # unused, kept for clarity/extension


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------
def downgrade() -> None:
    for table in (
        "ai_usages",
        "embeddings",
        "knowledge_documents",
        "prompt_templates",
        "messages",
        "conversations",
    ):
        _drop_updated_at_trigger(table)

    op.drop_table("ai_usages")
    op.drop_table("embeddings")
    op.drop_table("knowledge_documents")
    op.drop_table("prompt_templates")
    op.drop_table("messages")
    op.drop_table("conversations")

    bind = op.get_bind()
    postgresql.ENUM(name="ai_usage_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="ai_feature").drop(bind, checkfirst=True)
    postgresql.ENUM(name="prompt_category").drop(bind, checkfirst=True)

    op.execute(DROP_UPDATED_AT_TRIGGER_FN)