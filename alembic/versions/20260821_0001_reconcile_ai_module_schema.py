"""
Reconcile legacy AI-module tables with the current ORM schema.

Revision ID: 20260821_0001
Revises: 20260820_0002

This migration safely reconciles an older organization-scoped AI schema
with the current user-scoped AI schema.

Business tables are NEVER modified:
    users
    customers
    properties
    leads
    bookings
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector


# ---------------------------------------------------------------------------
# Alembic identifiers
# ---------------------------------------------------------------------------

revision = "20260821_0001"
down_revision = "20260820_0002"
branch_labels = None
depends_on = None


EMBEDDING_DIMENSION = 1536

LEGACY_TABLES = (
    "conversations",
    "messages",
    "prompt_templates",
    "knowledge_documents",
    "embeddings",
    "ai_usage_logs",
)


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
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    ]


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_names(inspector, table_name: str) -> set[str]:
    if not _table_exists(inspector, table_name):
        return set()

    return {
        column["name"]
        for column in inspector.get_columns(table_name)
    }


def _create_updated_at_function(bind) -> None:
    bind.exec_driver_sql(
        """
        CREATE OR REPLACE FUNCTION ai_module_set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def _create_updated_at_trigger(table_name: str) -> None:
    # FIX: make idempotent. A retried/partially-applied migration (or a
    # table that already had this trigger from a prior successful run)
    # must not fail with "trigger already exists".
    op.execute(
        f"""
        DROP TRIGGER IF EXISTS trg_{table_name}_updated_at
        ON {table_name};
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_{table_name}_updated_at
        BEFORE UPDATE ON {table_name}
        FOR EACH ROW
        EXECUTE FUNCTION ai_module_set_updated_at();
        """
    )


def _drop_updated_at_trigger(table_name: str) -> None:
    op.execute(
        f"""
        DROP TRIGGER IF EXISTS trg_{table_name}_updated_at
        ON {table_name};
        """
    )


# ---------------------------------------------------------------------------
# Legacy schema detection
# ---------------------------------------------------------------------------

def _is_legacy_conversations(inspector) -> bool:
    columns = _column_names(inspector, "conversations")

    return (
        "organization_id" in columns
        and "conversation_type" in columns
        and "status" in columns
        and "module" not in columns
    )


def _is_legacy_messages(inspector) -> bool:
    columns = _column_names(inspector, "messages")

    if not columns:
        return False

    return (
        "metadata_json" in columns
        or "sequence_number" in columns
    )


def _is_legacy_prompt_templates(inspector) -> bool:
    columns = _column_names(inspector, "prompt_templates")

    return (
        "organization_id" in columns
        and "template_body" in columns
        and "template_text" not in columns
    )


def _is_legacy_knowledge_documents(inspector) -> bool:
    columns = _column_names(inspector, "knowledge_documents")

    return (
        "organization_id" in columns
        and "uploaded_by" in columns
        and "file_path" in columns
        and "file_name" not in columns
    )


def _is_legacy_embeddings(inspector) -> bool:
    columns = _column_names(inspector, "embeddings")

    return (
        "embedding_vector" in columns
        and "dimensions" in columns
    )


def _is_legacy_ai_usage_logs(inspector) -> bool:
    return _table_exists(inspector, "ai_usage_logs")


# ---------------------------------------------------------------------------
# Safe legacy table rename
# ---------------------------------------------------------------------------

def _rename_legacy_table(bind, table_name: str) -> None:
    """
    Rename a legacy table and all of its PostgreSQL indexes.

    PostgreSQL relation names are schema-wide. Therefore simply renaming
    the table is not enough: old indexes such as

        pk_conversations
        ix_conversations_user_id
        ix_conversations_is_deleted

    can still collide with the indexes created for the replacement table.

    This function:
        1. Finds the old primary key.
        2. Finds all indexes.
        3. Renames the table.
        4. Renames the old primary key.
        5. Renames every remaining old index.

    No data is deleted.
    """

    inspector = sa.inspect(bind)

    if not _table_exists(inspector, table_name):
        return

    backup_name = f"{table_name}_legacy_backup"

    if _table_exists(inspector, backup_name):
        return

    # -----------------------------------------------------------------------
    # Find primary-key constraint.
    # -----------------------------------------------------------------------

    pk_result = bind.execute(
        sa.text(
            """
            SELECT con.conname
            FROM pg_constraint con
            JOIN pg_class rel
                ON rel.oid = con.conrelid
            JOIN pg_namespace ns
                ON ns.oid = rel.relnamespace
            WHERE ns.nspname = 'public'
              AND rel.relname = :table_name
              AND con.contype = 'p'
            ORDER BY con.conname
            LIMIT 1
            """
        ),
        {
            "table_name": table_name,
        },
    )

    pk_row = pk_result.first()
    pk_name = pk_row[0] if pk_row else None

    # -----------------------------------------------------------------------
    # Find ALL indexes belonging to the table.
    # -----------------------------------------------------------------------

    index_result = bind.execute(
        sa.text(
            """
            SELECT indexrelname
            FROM pg_stat_all_indexes
            WHERE schemaname = 'public'
              AND relname = :table_name
            ORDER BY indexrelname
            """
        ),
        {
            "table_name": table_name,
        },
    )

    index_names = [
        row[0]
        for row in index_result.fetchall()
    ]

    # -----------------------------------------------------------------------
    # Rename the table.
    # -----------------------------------------------------------------------

    op.rename_table(
        table_name,
        backup_name,
    )

    # -----------------------------------------------------------------------
    # Rename primary-key constraint.
    # -----------------------------------------------------------------------

    if pk_name:
        new_pk_name = f"{pk_name}_legacy_backup"

        pk_exists_result = bind.execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_constraint con
                    JOIN pg_class rel
                        ON rel.oid = con.conrelid
                    JOIN pg_namespace ns
                        ON ns.oid = rel.relnamespace
                    WHERE ns.nspname = 'public'
                      AND rel.relname = :backup_name
                      AND con.conname = :pk_name
                )
                """
            ),
            {
                "backup_name": backup_name,
                "pk_name": pk_name,
            },
        )

        pk_exists = bool(
            pk_exists_result.scalar()
        )

        new_pk_exists_result = bind.execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_class c
                    JOIN pg_namespace ns
                        ON ns.oid = c.relnamespace
                    WHERE ns.nspname = 'public'
                      AND c.relname = :new_pk_name
                )
                """
            ),
            {
                "new_pk_name": new_pk_name,
            },
        )

        new_pk_exists = bool(
            new_pk_exists_result.scalar()
        )

        if pk_exists and not new_pk_exists:
            preparer = bind.dialect.identifier_preparer

            quoted_backup = preparer.quote(
                backup_name
            )
            quoted_old_pk = preparer.quote(
                pk_name
            )
            quoted_new_pk = preparer.quote(
                new_pk_name
            )

            bind.exec_driver_sql(
                f"""
                ALTER TABLE public.{quoted_backup}
                RENAME CONSTRAINT {quoted_old_pk}
                TO {quoted_new_pk}
                """
            )

    # -----------------------------------------------------------------------
    # Rename every remaining index.
    # -----------------------------------------------------------------------

    for index_name in index_names:

        # The primary-key index is handled through its constraint above.
        if pk_name and index_name == pk_name:
            continue

        new_index_name = f"{index_name}_legacy_backup"

        old_index_result = bind.execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_class c
                    JOIN pg_namespace ns
                        ON ns.oid = c.relnamespace
                    WHERE ns.nspname = 'public'
                      AND c.relname = :index_name
                      AND c.relkind IN ('i', 'I')
                )
                """
            ),
            {
                "index_name": index_name,
            },
        )

        old_index_exists = bool(
            old_index_result.scalar()
        )

        if not old_index_exists:
            continue

        new_index_result = bind.execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_class c
                    JOIN pg_namespace ns
                        ON ns.oid = c.relnamespace
                    WHERE ns.nspname = 'public'
                      AND c.relname = :new_index_name
                )
                """
            ),
            {
                "new_index_name": new_index_name,
            },
        )

        new_index_exists = bool(
            new_index_result.scalar()
        )

        if new_index_exists:
            continue

        preparer = bind.dialect.identifier_preparer

        quoted_old_index = preparer.quote(
            index_name
        )

        quoted_new_index = preparer.quote(
            new_index_name
        )

        bind.exec_driver_sql(
            f"""
            ALTER INDEX public.{quoted_old_index}
            RENAME TO {quoted_new_index}
            """
        )


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:
    bind = op.get_bind()

    # -----------------------------------------------------------------------
    # PostgreSQL extensions.
    # -----------------------------------------------------------------------

    op.execute(
        'CREATE EXTENSION IF NOT EXISTS "pgcrypto";'
    )

    op.execute(
        'CREATE EXTENSION IF NOT EXISTS "vector";'
    )

    inspector = sa.inspect(bind)

    # -----------------------------------------------------------------------
    # Detect legacy tables.
    # -----------------------------------------------------------------------

    legacy_tables: list[str] = []

    if _is_legacy_conversations(inspector):
        legacy_tables.append("conversations")

    if _is_legacy_messages(inspector):
        legacy_tables.append("messages")

    if _is_legacy_prompt_templates(inspector):
        legacy_tables.append("prompt_templates")

    if _is_legacy_knowledge_documents(inspector):
        legacy_tables.append("knowledge_documents")

    if _is_legacy_embeddings(inspector):
        legacy_tables.append("embeddings")

    if _is_legacy_ai_usage_logs(inspector):
        legacy_tables.append("ai_usage_logs")

    # -----------------------------------------------------------------------
    # No legacy schema to migrate away from. NOTE: this only skips the
    # legacy-table handling below; ai_usages reconciliation still runs,
    # since ai_usages existing/being current is orthogonal to whether the
    # legacy tables were ever present.
    # -----------------------------------------------------------------------

    if legacy_tables:
        # -------------------------------------------------------------------
        # Trigger function.
        # -------------------------------------------------------------------

        _create_updated_at_function(bind)

        # -------------------------------------------------------------------
        # Rename children first and parents last.
        # -------------------------------------------------------------------

        rename_order = [
            "ai_usage_logs",
            "embeddings",
            "messages",
            "knowledge_documents",
            "prompt_templates",
            "conversations",
        ]

        for table_name in rename_order:
            if table_name in legacy_tables:
                _rename_legacy_table(
                    bind,
                    table_name,
                )
    else:
        _create_updated_at_function(bind)

    # -----------------------------------------------------------------------
    # Refresh inspector.
    # -----------------------------------------------------------------------

    inspector = sa.inspect(bind)

    # -----------------------------------------------------------------------
    # conversations
    # -----------------------------------------------------------------------
    # FIX: guard every create_table below against a retried/partial run.
    # If a previous attempt renamed the legacy table away and created the
    # new one before failing later (e.g. at ai_usages), re-running this
    # migration must not try to create these tables a second time.

    if not _table_exists(inspector, "conversations"):
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
                sa.ForeignKey(
                    "users.id",
                    ondelete="CASCADE",
                ),
                nullable=False,
            ),
            sa.Column(
                "title",
                sa.String(length=255),
                nullable=False,
                server_default="New Conversation",
            ),
            sa.Column(
                "module",
                sa.String(length=50),
                nullable=False,
                server_default="chat",
            ),
            sa.Column(
                "metadata",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
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

        op.create_index(
            "ix_conversations_user_id",
            "conversations",
            ["user_id"],
        )

        op.create_index(
            "ix_conversations_is_deleted",
            "conversations",
            ["is_deleted"],
        )

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

    _create_updated_at_trigger(
        "conversations"
    )

    # -----------------------------------------------------------------------
    # messages
    # -----------------------------------------------------------------------

    if not _table_exists(inspector, "messages"):
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
                sa.ForeignKey(
                    "conversations.id",
                    ondelete="CASCADE",
                ),
                nullable=False,
            ),
            sa.Column(
                "role",
                sa.String(length=20),
                nullable=False,
            ),
            sa.Column(
                "content",
                sa.Text(),
                nullable=False,
            ),
            sa.Column(
                "tokens_used",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "model_used",
                sa.String(length=100),
                nullable=True,
            ),
            sa.Column(
                "metadata",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
            *_timestamp_columns(),
            *_soft_delete_columns(),
            sa.CheckConstraint(
                "role IN ('user','assistant','system','tool')",
                name="ck_messages_role",
            ),
            sa.CheckConstraint(
                "tokens_used >= 0",
                name="ck_messages_tokens_used_nonneg",
            ),
        )

        op.create_index(
            "ix_messages_conversation_id",
            "messages",
            ["conversation_id"],
        )

        op.create_index(
            "ix_messages_is_deleted",
            "messages",
            ["is_deleted"],
        )

        op.create_index(
            "ix_messages_conversation_created_at",
            "messages",
            ["conversation_id", "created_at"],
        )

    _create_updated_at_trigger(
        "messages"
    )

    # -----------------------------------------------------------------------
    # prompt_templates
    # -----------------------------------------------------------------------

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

    prompt_category_enum.create(
        bind,
        checkfirst=True,
    )

    if not _table_exists(inspector, "prompt_templates"):
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
                sa.ForeignKey(
                    "users.id",
                    ondelete="SET NULL",
                ),
                nullable=True,
            ),
            sa.Column(
                "name",
                sa.String(length=150),
                nullable=False,
            ),
            sa.Column(
                "description",
                sa.Text(),
                nullable=True,
            ),
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
            sa.Column(
                "template_text",
                sa.Text(),
                nullable=False,
            ),
            sa.Column(
                "variables",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
            sa.Column(
                "version",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            *_timestamp_columns(),
            *_soft_delete_columns(),
            sa.CheckConstraint(
                "version >= 1",
                name="ck_prompt_templates_version_positive",
            ),
            sa.UniqueConstraint(
                "name",
                "version",
                name="uq_prompt_templates_name_version",
            ),
        )

        op.create_index(
            "ix_prompt_templates_name",
            "prompt_templates",
            ["name"],
        )

        op.create_index(
            "ix_prompt_templates_is_deleted",
            "prompt_templates",
            ["is_deleted"],
        )

        op.create_index(
            "ix_prompt_templates_category_is_active",
            "prompt_templates",
            ["category", "is_active"],
        )

    _create_updated_at_trigger(
        "prompt_templates"
    )

    # -----------------------------------------------------------------------
    # knowledge_documents
    # -----------------------------------------------------------------------

    if not _table_exists(inspector, "knowledge_documents"):
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
                sa.ForeignKey(
                    "users.id",
                    ondelete="SET NULL",
                ),
                nullable=True,
            ),
            sa.Column(
                "title",
                sa.String(length=255),
                nullable=False,
            ),
            sa.Column(
                "file_name",
                sa.String(length=255),
                nullable=False,
            ),
            sa.Column(
                "file_path",
                sa.String(length=1024),
                nullable=False,
            ),
            sa.Column(
                "file_type",
                sa.String(length=30),
                nullable=False,
            ),
            sa.Column(
                "file_size",
                sa.BigInteger(),
                nullable=True,
            ),
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="pending",
            ),
            sa.Column(
                "chunk_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "metadata",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
            *_timestamp_columns(),
            sa.CheckConstraint(
                "status IN ('pending','processing','completed','failed')",
                name="ck_knowledge_documents_status",
            ),
            sa.CheckConstraint(
                "file_size IS NULL OR file_size >= 0",
                name="ck_knowledge_documents_file_size_nonneg",
            ),
            sa.CheckConstraint(
                "chunk_count >= 0",
                name="ck_knowledge_documents_chunk_count_nonneg",
            ),
        )

        op.create_index(
            "ix_knowledge_documents_status",
            "knowledge_documents",
            ["status"],
        )

    _create_updated_at_trigger(
        "knowledge_documents"
    )

    # -----------------------------------------------------------------------
    # embeddings
    # -----------------------------------------------------------------------

    if not _table_exists(inspector, "embeddings"):
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
                sa.ForeignKey(
                    "knowledge_documents.id",
                    ondelete="CASCADE",
                ),
                nullable=False,
            ),
            sa.Column(
                "chunk_index",
                sa.Integer(),
                nullable=False,
            ),
            sa.Column(
                "chunk_text",
                sa.Text(),
                nullable=False,
            ),
            sa.Column(
                "embedding_vector",
                Vector(EMBEDDING_DIMENSION),
                nullable=False,
            ),
            sa.Column(
                "token_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            *_timestamp_columns(),
            sa.CheckConstraint(
                "chunk_index >= 0",
                name="ck_embeddings_chunk_index_nonneg",
            ),
            sa.CheckConstraint(
                "token_count >= 0",
                name="ck_embeddings_token_count_nonneg",
            ),
            sa.UniqueConstraint(
                "document_id",
                "chunk_index",
                name="uq_embeddings_document_id_chunk_index",
            ),
        )

        op.create_index(
            "ix_embeddings_document_id",
            "embeddings",
            ["document_id"],
        )

    _create_updated_at_trigger(
        "embeddings"
    )

    # -----------------------------------------------------------------------
    # ai_usages
    # -----------------------------------------------------------------------
    # FIX (root cause of the migration failure): ai_usages ALREADY EXISTS
    # with the correct, current ORM schema per spec. The original migration
    # called op.create_table("ai_usages", ...) unconditionally, which threw
    # "relation ai_usages already exists" and rolled back the entire
    # transaction -- which is exactly why the DB was still showing legacy
    # table names despite the rename logic having "run". We must only
    # create ai_usages (and its enums/indexes/trigger) if it is genuinely
    # missing, and never touch it otherwise.

    ai_feature_enum = postgresql.ENUM(
        "CHAT",
        "RAG",
        "SQL_GENERATION",
        "ANALYTICS",
        "EMBEDDING",
        name="ai_feature",
    )

    ai_feature_enum.create(
        bind,
        checkfirst=True,
    )

    ai_usage_status_enum = postgresql.ENUM(
        "SUCCESS",
        "FAILURE",
        name="ai_usage_status",
    )

    ai_usage_status_enum.create(
        bind,
        checkfirst=True,
    )

    if not _table_exists(inspector, "ai_usages"):
        ai_feature_enum_ref = postgresql.ENUM(
            "CHAT",
            "RAG",
            "SQL_GENERATION",
            "ANALYTICS",
            "EMBEDDING",
            name="ai_feature",
            create_type=False,
        )

        ai_usage_status_enum_ref = postgresql.ENUM(
            "SUCCESS",
            "FAILURE",
            name="ai_usage_status",
            create_type=False,
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
                sa.ForeignKey(
                    "users.id",
                    ondelete="CASCADE",
                ),
                nullable=False,
            ),
            sa.Column(
                "conversation_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(
                    "conversations.id",
                    ondelete="SET NULL",
                ),
                nullable=True,
            ),
            sa.Column(
                "feature",
                ai_feature_enum_ref,
                nullable=False,
            ),
            sa.Column(
                "model_name",
                sa.String(length=100),
                nullable=False,
            ),
            sa.Column(
                "prompt_tokens",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "completion_tokens",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "total_tokens",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "cost_usd",
                sa.Numeric(
                    precision=12,
                    scale=6,
                ),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "latency_ms",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "status",
                ai_usage_status_enum_ref,
                nullable=False,
                server_default="SUCCESS",
            ),
            sa.Column(
                "error_message",
                sa.Text(),
                nullable=True,
            ),
            sa.Column(
                "request_metadata",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
            *_timestamp_columns(),
            sa.CheckConstraint(
                "prompt_tokens >= 0",
                name="ck_ai_usages_prompt_tokens_nonneg",
            ),
            sa.CheckConstraint(
                "completion_tokens >= 0",
                name="ck_ai_usages_completion_tokens_nonneg",
            ),
            sa.CheckConstraint(
                "total_tokens >= 0",
                name="ck_ai_usages_total_tokens_nonneg",
            ),
            sa.CheckConstraint(
                "cost_usd >= 0",
                name="ck_ai_usages_cost_nonneg",
            ),
            sa.CheckConstraint(
                "latency_ms IS NULL OR latency_ms >= 0",
                name="ck_ai_usages_latency_nonneg",
            ),
        )

        op.create_index(
            "ix_ai_usages_feature",
            "ai_usages",
            ["feature"],
        )

        op.create_index(
            "ix_ai_usages_user_id_created_at",
            "ai_usages",
            ["user_id", "created_at"],
        )

        _create_updated_at_trigger(
            "ai_usages"
        )
    # else: ai_usages already exists and is current -- do not touch it,
    # not even to add the updated_at trigger, per the "must remain
    # untouched" requirement.


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------

def downgrade() -> None:
    bind = op.get_bind()

    inspector = sa.inspect(bind)

    backup_exists = any(
        _table_exists(
            inspector,
            f"{table}_legacy_backup",
        )
        for table in LEGACY_TABLES
    )

    if not backup_exists:
        return

    # -----------------------------------------------------------------------
    # Never silently destroy newly-created AI data.
    # NOTE: ai_usages is intentionally excluded here -- this migration
    # never creates or owns ai_usages when it already existed, so downgrade
    # must not drop it either.
    # -----------------------------------------------------------------------

    for table in (
        "embeddings",
        "knowledge_documents",
        "prompt_templates",
        "messages",
        "conversations",
    ):
        inspector = sa.inspect(bind)

        if not _table_exists(inspector, table):
            continue

        count = bind.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {table}"
            )
        ).scalar_one()

        if count:
            raise RuntimeError(
                f"Refusing to downgrade: "
                f"'{table}' contains {count} row(s)."
            )

    # -----------------------------------------------------------------------
    # Drop triggers.
    # -----------------------------------------------------------------------

    for table in (
        "embeddings",
        "knowledge_documents",
        "prompt_templates",
        "messages",
        "conversations",
    ):
        inspector = sa.inspect(bind)

        if _table_exists(inspector, table):
            _drop_updated_at_trigger(
                table
            )

    # -----------------------------------------------------------------------
    # Drop tables in dependency-safe order.
    # -----------------------------------------------------------------------

    for table in (
        "embeddings",
        "knowledge_documents",
        "prompt_templates",
        "messages",
        "conversations",
    ):
        inspector = sa.inspect(bind)

        if _table_exists(inspector, table):
            op.drop_table(
                table
            )

    # -----------------------------------------------------------------------
    # Drop enums.
    # -----------------------------------------------------------------------
    # NOTE: ai_feature / ai_usage_status are NOT dropped here since
    # ai_usages (which this migration does not own when pre-existing)
    # still depends on them.

    postgresql.ENUM(
        name="prompt_category"
    ).drop(
        bind,
        checkfirst=True,
    )

    # -----------------------------------------------------------------------
    # Drop trigger function.
    # -----------------------------------------------------------------------

    op.execute(
        "DROP FUNCTION IF EXISTS ai_module_set_updated_at();"
    )

    # -----------------------------------------------------------------------
    # Restore legacy table names.
    # -----------------------------------------------------------------------

    for table_name in (
        "conversations",
        "messages",
        "prompt_templates",
        "knowledge_documents",
        "embeddings",
        "ai_usage_logs",
    ):
        inspector = sa.inspect(bind)

        backup_name = (
            f"{table_name}_legacy_backup"
        )

        if (
            _table_exists(
                inspector,
                backup_name,
            )
            and not _table_exists(
                inspector,
                table_name,
            )
        ):
            op.rename_table(
                backup_name,
                table_name,
            )