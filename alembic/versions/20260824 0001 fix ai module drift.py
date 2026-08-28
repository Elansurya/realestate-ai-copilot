"""Fix AI module column/type drift on already-applied databases.

Revision ID: 20260824_0001
Revises: 20260821_0001
Create Date: 2026-08-24

WHY THIS MIGRATION EXISTS
--------------------------
Editing an Alembic migration file after it has already been *run*
against a database does NOT retroactively change that database.
Alembic only tracks which revision IDs have been applied
(`alembic_version` table) -- it never re-diffs or re-runs a
revision's contents once its ID is marked as applied.

On at least one real environment, `ai_module_migration` was applied
while an earlier draft of that file was in place (missing
`conversations.module`, `prompt_templates.template_text`,
`knowledge_documents.file_name`, and creating `embeddings.embedding_vector`
as `jsonb` instead of `vector(1536)`). The file was later corrected in
this repo, but `alembic upgrade head` alone does nothing for a database
that already has `ai_module_migration`'s revision ID recorded --
Alembic has no way to know the file's contents changed underneath it.

This migration repairs that drift directly, additively, and
idempotently:
    - Inspects the live database (not the ORM/model definitions) for
      each of the 4 affected columns.
    - Adds any column that is missing, with the exact type/nullability
      the ORM (`app/models/*.py`) expects.
    - If `embeddings.embedding_vector` exists but is not a pgvector
      `vector` column, converts it -- but only if the table is
      confirmed empty; otherwise it raises with a clear message rather
      than silently discarding data.
    - Every step first checks whether the fix is already in place, so
      this migration is a safe no-op on databases that never had the
      drift (e.g. a fresh database built purely by running this
      repo's own migration chain, where `ai_module_migration` already
      created everything correctly).

DATABASE MODIFIED BY RUNNING THIS FILE: YES, but only for columns/types
that are actually missing/wrong; already-correct databases are untouched.
DATA MODIFIED: NO, except the embedding_vector type conversion, which
only proceeds automatically when the table is empty.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260824_0001"
down_revision = "20260821_0001"
branch_labels = None
depends_on = None


def _columns(inspector: sa.Inspector, table_name: str) -> dict:
    return {c["name"]: c for c in inspector.get_columns(table_name)}


def _index_names(inspector: sa.Inspector, table_name: str) -> set:
    return {ix["name"] for ix in inspector.get_indexes(table_name)}


def _check_constraint_names(inspector: sa.Inspector, table_name: str) -> set:
    return {cc["name"] for cc in inspector.get_check_constraints(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # -----------------------------------------------------------------
    # conversations.module
    # -----------------------------------------------------------------
    if "conversations" in existing_tables:
        cols = _columns(inspector, "conversations")
        if "module" not in cols:
            op.add_column(
                "conversations",
                sa.Column(
                    "module",
                    sa.String(length=50),
                    nullable=False,
                    server_default="chat",
                ),
            )
            if "ck_conversations_module" not in _check_constraint_names(
                sa.inspect(bind), "conversations"
            ):
                op.create_check_constraint(
                    "ck_conversations_module",
                    "conversations",
                    "module IN ('chat','rag','sql','analytics')",
                )
            if "ix_conversations_user_id_module" not in _index_names(
                sa.inspect(bind), "conversations"
            ):
                op.create_index(
                    "ix_conversations_user_id_module",
                    "conversations",
                    ["user_id", "module"],
                )

    # -----------------------------------------------------------------
    # prompt_templates.template_text
    # -----------------------------------------------------------------
    if "prompt_templates" in existing_tables:
        cols = _columns(inspector, "prompt_templates")
        if "template_text" not in cols:
            # Added NOT NULL with a temporary server_default so any
            # existing rows get a value, then the default is dropped so
            # future inserts must supply it explicitly (matching the
            # ORM, which has no default for this column).
            op.add_column(
                "prompt_templates",
                sa.Column(
                    "template_text", sa.Text(), nullable=False, server_default=""
                ),
            )
            op.alter_column("prompt_templates", "template_text", server_default=None)

    # -----------------------------------------------------------------
    # knowledge_documents.file_name
    # -----------------------------------------------------------------
    if "knowledge_documents" in existing_tables:
        cols = _columns(inspector, "knowledge_documents")
        if "file_name" not in cols:
            op.add_column(
                "knowledge_documents",
                sa.Column(
                    "file_name", sa.String(length=255), nullable=False, server_default=""
                ),
            )
            op.alter_column("knowledge_documents", "file_name", server_default=None)

    # -----------------------------------------------------------------
    # embeddings.embedding_vector: must be pgvector `vector(1536)`, not jsonb
    # -----------------------------------------------------------------
    if "embeddings" in existing_tables:
        cols = _columns(inspector, "embeddings")
        col = cols.get("embedding_vector")
        if col is not None:
            type_name = str(col["type"]).lower()
            if "vector" not in type_name:
                op.execute('CREATE EXTENSION IF NOT EXISTS "vector";')
                row_count = bind.execute(
                    sa.text("SELECT COUNT(*) FROM embeddings")
                ).scalar()
                if row_count and row_count > 0:
                    raise RuntimeError(
                        "embeddings.embedding_vector exists as "
                        f"'{type_name}' instead of pgvector's 'vector' type, "
                        f"and the table has {row_count} row(s). Refusing to "
                        "auto-convert and potentially lose data. Back up or "
                        "re-embed the affected rows, or TRUNCATE the table "
                        "if the data is disposable, then re-run "
                        "`alembic upgrade head`."
                    )
                op.execute("ALTER TABLE embeddings DROP COLUMN embedding_vector;")
                op.execute(
                    "ALTER TABLE embeddings ADD COLUMN embedding_vector "
                    "vector(1536) NOT NULL;"
                )


def downgrade() -> None:
    # Intentionally a no-op. This migration only repairs columns/types
    # that the target schema (ai_module_migration / the ORM) already
    # requires; downgrading it would mean re-introducing a known-bad,
    # already-fixed production bug. If a true rollback of the AI module
    # is ever needed, use `20260821_0001`'s downgrade (or
    # `ai_module_migration`'s), not this one.
    pass
