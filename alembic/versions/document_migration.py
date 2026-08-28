"""document module

Revision ID: 20260802_0002
Revises: a1b2c3d4e5f6
Create Date: 2026-08-02 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# NOTE: Replace the placeholder below with the actual revision id of the
# current migration head before running. Determine it via
# `alembic heads` or by inspecting backend/alembic/versions/ -- do not
# guess this value.
revision = "20260802_0002"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # NOTE: create_type=False is intentional on all three ENUM
    # definitions below. The types are created explicitly via the
    # `.create(bind, checkfirst=True)` calls immediately following
    # these definitions. Without create_type=False, SQLAlchemy/Alembic
    # will *also* attempt to create the type as a side effect of
    # op.create_table(), which raises a duplicate-object error
    # ("type ... already exists") once the explicit creation above has
    # already run — or on any database where the type was already
    # created by a prior partial run.
    document_category_enum = postgresql.ENUM(
        "KYC",
        "IDENTITY_PROOF",
        "ADDRESS_PROOF",
        "INCOME_PROOF",
        "AGREEMENT",
        "SALE_DEED",
        "NOC",
        "PROPERTY_PAPER",
        "FLOOR_PLAN",
        "BROCHURE",
        "PAYMENT_RECEIPT",
        "BOOKING_FORM",
        "LEGAL_DOCUMENT",
        "TAX_DOCUMENT",
        "PHOTO",
        "CONTRACT",
        "OTHER",
        name="document_category_enum",
        create_type=False,
    )
    document_file_type_enum = postgresql.ENUM(
        "PDF",
        "DOC",
        "DOCX",
        "XLS",
        "XLSX",
        "PPT",
        "PPTX",
        "JPG",
        "JPEG",
        "PNG",
        "GIF",
        "TXT",
        "CSV",
        "ZIP",
        "OTHER",
        name="document_file_type_enum",
        create_type=False,
    )
    document_storage_provider_enum = postgresql.ENUM(
        "LOCAL",
        "AWS_S3",
        "AZURE_BLOB",
        "GCP_STORAGE",
        "CLOUDINARY",
        "OTHER",
        name="document_storage_provider_enum",
        create_type=False,
    )

    bind = op.get_bind()
    document_category_enum.create(bind, checkfirst=True)
    document_file_type_enum.create(bind, checkfirst=True)
    document_storage_provider_enum.create(bind, checkfirst=True)

    op.create_table(
        "documents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("property_id", sa.Integer(), nullable=True),
        sa.Column("booking_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("parent_document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "is_latest_version",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "category",
            document_category_enum,
            nullable=False,
            server_default="OTHER",
        ),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("original_file_name", sa.String(length=255), nullable=False),
        sa.Column("file_extension", sa.String(length=20), nullable=True),
        sa.Column("file_type", document_file_type_enum, nullable=False),
        sa.Column("mime_type", sa.String(length=150), nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "storage_provider",
            document_storage_provider_enum,
            nullable=False,
            server_default="LOCAL",
        ),
        sa.Column("storage_bucket", sa.String(length=255), nullable=True),
        sa.Column("storage_path", sa.String(length=1024), nullable=False),
        sa.Column("storage_url", sa.String(length=2048), nullable=True),
        sa.Column(
            "is_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("verified_by_id", sa.Integer(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("uploaded_by_id", sa.Integer(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by_id", sa.Integer(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
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
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["property_id"], ["properties.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["parent_document_id"], ["documents.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["verified_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["uploaded_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["deleted_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "file_size_bytes >= 0",
            name="ck_documents_file_size_bytes_non_negative",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_documents_version_positive",
        ),
        sa.CheckConstraint(
            "length(trim(storage_path)) > 0",
            name="ck_documents_storage_path_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(title)) > 0",
            name="ck_documents_title_not_blank",
        ),
        sa.CheckConstraint(
            "(is_deleted = false AND deleted_at IS NULL AND deleted_by_id IS NULL) "
            "OR (is_deleted = true AND deleted_at IS NOT NULL)",
            name="ck_documents_soft_delete_consistency",
        ),
        sa.CheckConstraint(
            "(is_verified = false AND verified_at IS NULL AND verified_by_id IS NULL) "
            "OR (is_verified = true AND verified_at IS NOT NULL AND verified_by_id IS NOT NULL)",
            name="ck_documents_verification_consistency",
        ),
        sa.CheckConstraint(
            "parent_document_id IS NULL OR parent_document_id != id",
            name="ck_documents_parent_not_self",
        ),
    )

    # ----------------------------------------------------------------
    # Single-column FK / lookup indexes
    # ----------------------------------------------------------------
    op.create_index("ix_documents_customer_id", "documents", ["customer_id"])
    op.create_index("ix_documents_property_id", "documents", ["property_id"])
    op.create_index("ix_documents_booking_id", "documents", ["booking_id"])
    op.create_index("ix_documents_lead_id", "documents", ["lead_id"])
    op.create_index("ix_documents_parent_document_id", "documents", ["parent_document_id"])
    op.create_index("ix_documents_is_latest_version", "documents", ["is_latest_version"])
    op.create_index("ix_documents_category", "documents", ["category"])
    op.create_index("ix_documents_file_type", "documents", ["file_type"])
    op.create_index("ix_documents_checksum_sha256", "documents", ["checksum_sha256"])
    op.create_index("ix_documents_storage_provider", "documents", ["storage_provider"])
    op.create_index("ix_documents_is_verified", "documents", ["is_verified"])
    op.create_index("ix_documents_verified_by_id", "documents", ["verified_by_id"])
    op.create_index("ix_documents_expiry_date", "documents", ["expiry_date"])
    op.create_index("ix_documents_uploaded_by_id", "documents", ["uploaded_by_id"])
    op.create_index("ix_documents_is_deleted", "documents", ["is_deleted"])
    op.create_index("ix_documents_deleted_by_id", "documents", ["deleted_by_id"])
    op.create_index("ix_documents_created_by_id", "documents", ["created_by_id"])
    op.create_index("ix_documents_updated_by_id", "documents", ["updated_by_id"])

    # ----------------------------------------------------------------
    # Composite indexes -- optimize common per-entity document lookups
    # scoped by category.
    # ----------------------------------------------------------------
    op.create_index(
        "ix_documents_customer_category", "documents", ["customer_id", "category"]
    )
    op.create_index(
        "ix_documents_property_category", "documents", ["property_id", "category"]
    )
    op.create_index(
        "ix_documents_booking_category", "documents", ["booking_id", "category"]
    )
    op.create_index(
        "ix_documents_active_deleted", "documents", ["is_active", "is_deleted"]
    )

    # ----------------------------------------------------------------
    # updated_at auto-maintenance trigger -- guarantees correctness for
    # any write path (ORM, raw SQL, bulk `update()` statements), not
    # just ORM `onupdate=func.now()` (which only fires on session
    # flush).
    # ----------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_documents_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_documents_set_updated_at
        BEFORE UPDATE ON documents
        FOR EACH ROW
        EXECUTE FUNCTION set_documents_updated_at();
        """
    )


def downgrade() -> None:
    # Trigger/function must be dropped before the table.
    op.execute("DROP TRIGGER IF EXISTS trg_documents_set_updated_at ON documents")
    op.execute("DROP FUNCTION IF EXISTS set_documents_updated_at()")

    op.drop_index("ix_documents_active_deleted", table_name="documents")
    op.drop_index("ix_documents_booking_category", table_name="documents")
    op.drop_index("ix_documents_property_category", table_name="documents")
    op.drop_index("ix_documents_customer_category", table_name="documents")

    op.drop_index("ix_documents_updated_by_id", table_name="documents")
    op.drop_index("ix_documents_created_by_id", table_name="documents")
    op.drop_index("ix_documents_deleted_by_id", table_name="documents")
    op.drop_index("ix_documents_is_deleted", table_name="documents")
    op.drop_index("ix_documents_uploaded_by_id", table_name="documents")
    op.drop_index("ix_documents_expiry_date", table_name="documents")
    op.drop_index("ix_documents_verified_by_id", table_name="documents")
    op.drop_index("ix_documents_is_verified", table_name="documents")
    op.drop_index("ix_documents_storage_provider", table_name="documents")
    op.drop_index("ix_documents_checksum_sha256", table_name="documents")
    op.drop_index("ix_documents_file_type", table_name="documents")
    op.drop_index("ix_documents_category", table_name="documents")
    op.drop_index("ix_documents_is_latest_version", table_name="documents")
    op.drop_index("ix_documents_parent_document_id", table_name="documents")
    op.drop_index("ix_documents_lead_id", table_name="documents")
    op.drop_index("ix_documents_booking_id", table_name="documents")
    op.drop_index("ix_documents_property_id", table_name="documents")
    op.drop_index("ix_documents_customer_id", table_name="documents")

    op.drop_table("documents")

    bind = op.get_bind()
    postgresql.ENUM(name="document_storage_provider_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="document_file_type_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="document_category_enum").drop(bind, checkfirst=True)