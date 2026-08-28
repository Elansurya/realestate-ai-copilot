"""
backend/app/models/document.py

SQLAlchemy 2.x ORM model representing a Document within the Real Estate
AI Copilot CRM.

A Document is any file (KYC paper, agreement, sale deed, payment receipt,
brochure, floor plan, etc.) uploaded to the CRM and optionally linked back
to the Lead, Customer, Property, or Booking it pertains to. Every Document
tracks where its binary content physically lives (storage provider +
path), its verification state, its version lineage, and a full audit
trail, while never storing the binary content itself.

Conventions (mirrors `app/models/customer.py` / `app/models/booking.py` /
`app/models/property.py`):
    - `Base` comes from `app.db.base`; timestamps are inline,
      timezone-aware UTC columns (no mixins -- `app.models.mixins` does
      not exist in this project).
    - `id` is a server-generated PostgreSQL UUID via
      `func.gen_random_uuid()`, identical to `Lead.id` / `Customer.id` /
      `Booking.id` (same `pgcrypto` extension requirement).
    - `customer_id` / `booking_id` / `lead_id` / `parent_document_id` are
      `PG_UUID` FKs, matching the actual UUID primary keys of
      `Customer` / `Booking` / `Lead` / `Document` itself.
    - `property_id` is an `Integer` FK to `properties.id` (matching
      `Property.id`'s actual type -- `Property` uses an integer
      surrogate PK, per `app/models/property.py`).
    - `uploaded_by_id` / `verified_by_id` / `deleted_by_id` /
      `created_by_id` / `updated_by_id` are `Integer` FKs to `users.id`,
      matching `User.id`'s actual type -- the same typing
      `Customer.assigned_to_id` / `Booking.agent_id` already use.
    - `Customer`/`Property`/`Booking`/`Lead`/`User` are imported only
      under `TYPE_CHECKING` to avoid a runtime circular-import surface;
      relationships and `Mapped[]` annotations reference them by string,
      resolved by SQLAlchemy's mapper configuration once every model
      module has been imported.
    - `file_type`, `category`, and `storage_provider` use native
      PostgreSQL ENUM types (mirroring `Customer.status` /
      `Booking.status`) for strong data integrity at the database level.
    - `is_active` is a plain boolean soft-disable flag, matching
      `Customer.is_active` / `Booking.is_active` / `Property.is_active`.
    - `is_deleted` / `deleted_at` / `deleted_by_id` implement soft
      delete as a *distinct* concept from `is_active`: `is_active`
      toggles visibility in normal business views, while `is_deleted`
      marks a record as logically removed (excluded everywhere) without
      a hard `DELETE`, preserving it for audit/compliance purposes.
    - Single-column indexes are declared inline (`index=True`); only
      composite indexes and CHECK constraints live in `__table_args__`,
      exactly as `Customer` / `Booking` already do.

NOTE (scope of this phase):
    This module intentionally contains ONLY the ORM model. No
    repository, service, router, tests, or documentation are declared
    here -- those belong to a later phase.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.booking import Booking
    from app.models.customer import Customer
    from app.models.lead import Lead
    from app.models.property import Property
    from app.models.user import User


# --------------------------------------------------------------------------
# Document File Type Enumeration
# --------------------------------------------------------------------------
class DocumentFileType(str, enum.Enum):
    """Defines the physical file format of a Document's stored content."""

    PDF = "PDF"
    DOC = "DOC"
    DOCX = "DOCX"
    XLS = "XLS"
    XLSX = "XLSX"
    PPT = "PPT"
    PPTX = "PPTX"
    JPG = "JPG"
    JPEG = "JPEG"
    PNG = "PNG"
    GIF = "GIF"
    TXT = "TXT"
    CSV = "CSV"
    ZIP = "ZIP"
    OTHER = "OTHER"


# --------------------------------------------------------------------------
# Document Category Enumeration
# --------------------------------------------------------------------------
class DocumentCategory(str, enum.Enum):
    """
    Defines the business classification of a Document, driving
    compliance workflows, retention policy, and UI grouping.
    """

    KYC = "KYC"
    IDENTITY_PROOF = "IDENTITY_PROOF"
    ADDRESS_PROOF = "ADDRESS_PROOF"
    INCOME_PROOF = "INCOME_PROOF"
    AGREEMENT = "AGREEMENT"
    SALE_DEED = "SALE_DEED"
    NOC = "NOC"
    PROPERTY_PAPER = "PROPERTY_PAPER"
    FLOOR_PLAN = "FLOOR_PLAN"
    BROCHURE = "BROCHURE"
    PAYMENT_RECEIPT = "PAYMENT_RECEIPT"
    BOOKING_FORM = "BOOKING_FORM"
    LEGAL_DOCUMENT = "LEGAL_DOCUMENT"
    TAX_DOCUMENT = "TAX_DOCUMENT"
    PHOTO = "PHOTO"
    CONTRACT = "CONTRACT"
    OTHER = "OTHER"


# --------------------------------------------------------------------------
# Document Storage Provider Enumeration
# --------------------------------------------------------------------------
class DocumentStorageProvider(str, enum.Enum):
    """Defines the physical storage backend hosting a Document's content."""

    LOCAL = "LOCAL"
    AWS_S3 = "AWS_S3"
    AZURE_BLOB = "AZURE_BLOB"
    GCP_STORAGE = "GCP_STORAGE"
    CLOUDINARY = "CLOUDINARY"
    OTHER = "OTHER"


# --------------------------------------------------------------------------
# Document Model
# --------------------------------------------------------------------------
class Document(Base):
    """
    Represents a Document entity: metadata and storage location for a
    single uploaded file, optionally linked to a Lead, Customer,
    Property, and/or Booking, with verification, versioning, soft
    delete, and a full audit trail.

    Table: documents
    """

    __tablename__ = "documents"

    __table_args__ = (
        # Composite indexes -- cannot be expressed as single inline
        # index=True columns.
        Index("ix_documents_customer_category", "customer_id", "category"),
        Index("ix_documents_property_category", "property_id", "category"),
        Index("ix_documents_booking_category", "booking_id", "category"),
        Index("ix_documents_active_deleted", "is_active", "is_deleted"),
        CheckConstraint(
            "file_size_bytes >= 0",
            name="ck_documents_file_size_bytes_non_negative",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_documents_version_positive",
        ),
        CheckConstraint(
            "length(trim(storage_path)) > 0",
            name="ck_documents_storage_path_not_blank",
        ),
        CheckConstraint(
            "length(trim(title)) > 0",
            name="ck_documents_title_not_blank",
        ),
        CheckConstraint(
            "(is_deleted = false AND deleted_at IS NULL AND deleted_by_id IS NULL) "
            "OR (is_deleted = true AND deleted_at IS NOT NULL)",
            name="ck_documents_soft_delete_consistency",
        ),
        CheckConstraint(
            "(is_verified = false AND verified_at IS NULL AND verified_by_id IS NULL) "
            "OR (is_verified = true AND verified_at IS NOT NULL AND verified_by_id IS NOT NULL)",
            name="ck_documents_verification_consistency",
        ),
        CheckConstraint(
            "parent_document_id IS NULL OR parent_document_id != id",
            name="ck_documents_parent_not_self",
        ),
    )

    # ------------------------------------------------------------------
    # Primary Key
    # ------------------------------------------------------------------
    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        doc=(
            "Globally unique primary key for the document record. "
            "Requires the PostgreSQL `pgcrypto` extension to be "
            "enabled for `gen_random_uuid()` to be available."
        ),
    )

    # ------------------------------------------------------------------
    # Relationship Foreign Keys (Owning Entity)
    # ------------------------------------------------------------------
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Optional reference to the Customer this document belongs to.",
    )

    property_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("properties.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Optional internal integer ID of the Property this document belongs to.",
    )

    booking_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("bookings.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Optional reference to the Booking this document belongs to.",
    )

    lead_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Optional reference to the originating Lead record.",
    )

    # ------------------------------------------------------------------
    # Versioning (Self-Referential)
    # ------------------------------------------------------------------
    parent_document_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Optional reference to the prior version of this document, if any.",
    )

    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        doc="Monotonically increasing version number within a document lineage.",
    )

    is_latest_version: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        index=True,
        doc="True if this row is the most recent version in its document lineage.",
    )

    # ------------------------------------------------------------------
    # Descriptive Metadata
    # ------------------------------------------------------------------
    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc="Human-readable title of the document.",
    )

    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    category: Mapped[DocumentCategory] = mapped_column(
        SAEnum(
            DocumentCategory,
            name="document_category_enum",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
        default=DocumentCategory.OTHER,
        server_default=DocumentCategory.OTHER.value,
        index=True,
        doc="Business classification of the document.",
    )

    tags: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        doc="Arbitrary JSONB tag/metadata payload for flexible categorization.",
    )

    # ------------------------------------------------------------------
    # File Attributes
    # ------------------------------------------------------------------
    file_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc="Stored (system-generated) file name.",
    )

    original_file_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc="Original file name as supplied by the uploader.",
    )

    file_extension: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    file_type: Mapped[DocumentFileType] = mapped_column(
        SAEnum(
            DocumentFileType,
            name="document_file_type_enum",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
        index=True,
        doc="Physical file format of the stored content.",
    )

    mime_type: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)

    file_size_bytes: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        doc="Size of the stored file content, in bytes.",
    )

    checksum_sha256: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        doc="SHA-256 checksum of the file content, used for integrity checks/de-duplication.",
    )

    # ------------------------------------------------------------------
    # Storage Location
    # ------------------------------------------------------------------
    storage_provider: Mapped[DocumentStorageProvider] = mapped_column(
        SAEnum(
            DocumentStorageProvider,
            name="document_storage_provider_enum",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
        default=DocumentStorageProvider.LOCAL,
        server_default=DocumentStorageProvider.LOCAL.value,
        index=True,
        doc="Physical storage backend hosting the file content.",
    )

    storage_bucket: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    storage_path: Mapped[str] = mapped_column(
        String(1024),
        nullable=False,
        doc="Path/key of the file content within the storage provider.",
    )

    storage_url: Mapped[Optional[str]] = mapped_column(
        String(2048),
        nullable=True,
        doc="Public or signed URL for retrieving the file content, if applicable.",
    )

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------
    is_verified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        index=True,
        doc="True once an authorized user has verified the document's authenticity.",
    )

    verified_by_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="User who verified this document, if verified.",
    )

    verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="UTC timestamp when the document was verified, if verified.",
    )

    expiry_date: Mapped[Optional[date]] = mapped_column(
        Date,
        nullable=True,
        index=True,
        doc="Date after which the document is considered expired (e.g. ID proofs, NOCs).",
    )

    # ------------------------------------------------------------------
    # Ownership / Upload
    # ------------------------------------------------------------------
    uploaded_by_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        doc="User who uploaded this document.",
    )

    # ------------------------------------------------------------------
    # Status Flags
    # ------------------------------------------------------------------
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        doc="Soft-disable flag; inactive documents are excluded from active views.",
    )

    # ------------------------------------------------------------------
    # Soft Delete
    # ------------------------------------------------------------------
    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        index=True,
        doc="Soft delete flag; deleted documents are excluded everywhere.",
    )

    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="UTC timestamp when the document was soft-deleted, if deleted.",
    )

    deleted_by_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="User who soft-deleted this document, if deleted.",
    )

    # ------------------------------------------------------------------
    # Audit Fields
    # ------------------------------------------------------------------
    created_by_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        doc="User who created this document record.",
    )

    updated_by_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="User who last modified this document record.",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="UTC timestamp when the document record was created.",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        doc="UTC timestamp when the document record was last updated.",
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    customer: Mapped[Optional["Customer"]] = relationship(
        "Customer",
        foreign_keys=[customer_id],
        lazy="selectin",
        doc="The Customer this document belongs to, if any.",
    )

    property: Mapped[Optional["Property"]] = relationship(
        "Property",
        foreign_keys=[property_id],
        lazy="selectin",
        doc="The Property this document belongs to, if any.",
    )

    booking: Mapped[Optional["Booking"]] = relationship(
        "Booking",
        foreign_keys=[booking_id],
        lazy="selectin",
        doc="The Booking this document belongs to, if any.",
    )

    lead: Mapped[Optional["Lead"]] = relationship(
        "Lead",
        foreign_keys=[lead_id],
        lazy="selectin",
        doc="The Lead this document originated from, if any.",
    )

    parent_document: Mapped[Optional["Document"]] = relationship(
        "Document",
        remote_side=[id],
        foreign_keys=[parent_document_id],
        lazy="selectin",
        doc="The prior version of this document, if any.",
    )

    uploaded_by: Mapped["User"] = relationship(
        "User",
        foreign_keys=[uploaded_by_id],
        lazy="selectin",
        doc="The User who uploaded this document.",
    )

    verified_by: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[verified_by_id],
        lazy="selectin",
        doc="The User who verified this document, if verified.",
    )

    deleted_by: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[deleted_by_id],
        lazy="selectin",
        doc="The User who soft-deleted this document, if deleted.",
    )

    created_by: Mapped["User"] = relationship(
        "User",
        foreign_keys=[created_by_id],
        lazy="selectin",
        doc="The User who originally created this document record.",
    )

    updated_by: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[updated_by_id],
        lazy="selectin",
        doc="The User who last modified this document record.",
    )

    # ------------------------------------------------------------------
    # Developer Ergonomics
    # ------------------------------------------------------------------
    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return (
            f"<Document id={self.id} title={self.title!r} "
            f"category={self.category.value} version={self.version}>"
        )