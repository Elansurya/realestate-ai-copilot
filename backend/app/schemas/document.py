"""
backend/app/schemas/document.py

Pydantic v2 schemas for the Document module.

Generated exclusively from the approved `app.models.document.Document`
ORM model. No fields, types, or defaults are assumed or carried over
from any other module's schemas.

Responsibilities:
    - Define request/response contracts for document creation, partial
      updates, single-record retrieval, paginated listing, and
      search/filter query parameters.

Design Notes:
    - These schemas are transport/validation contracts only; they
      contain no business logic or database access.
    - `DocumentFileType`, `DocumentCategory`, and
      `DocumentStorageProvider` are imported directly from
      `app.models.document` and reused as-is, so API-facing enum
      values always stay in lockstep with the ORM/DB enum definitions
      with zero duplication (mirrors `app/schemas/booking.py`'s reuse
      of `BookingStatus`/`BookingPaymentStatus`/`BookingPaymentMode`).
    - `ConfigDict(from_attributes=True)` is used on all response
      schemas so they can be constructed directly from `Document` ORM
      instances without manual dict conversion.
    - Validation constraints (`file_size_bytes >= 0`, `version >= 1`,
      non-blank `title`/`storage_path`) mirror the `CheckConstraint`s
      declared in `Document.__table_args__` exactly, so a payload that
      passes schema validation will never be rejected by the
      database's own constraints.
    - Whitespace-only or empty-string input on optional free-text
      fields is normalized to `None` at the schema boundary, so the
      service/repository layers never have to special-case empty
      strings vs. NULL (mirrors `app/schemas/customer.py`).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.document import (
    DocumentCategory,
    DocumentFileType,
    DocumentStorageProvider,
)

# --------------------------------------------------------------------------
# Shared Validation Constants
# --------------------------------------------------------------------------
_ALLOWED_SORT_FIELDS = frozenset(
    {
        "title",
        "category",
        "file_type",
        "storage_provider",
        "file_size_bytes",
        "version",
        "expiry_date",
        "is_verified",
        "created_at",
        "updated_at",
    }
)


def _blank_to_none(value: Optional[str]) -> Optional[str]:
    """Strips surrounding whitespace and converts a blank string to `None`."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


# --------------------------------------------------------------------------
# Document Base Schema
# --------------------------------------------------------------------------
class DocumentBase(BaseModel):
    """
    Common fields shared across document creation, update, and response
    schemas. Contains no identity or audit fields.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    customer_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Optional reference to the Customer this document belongs to.",
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    )
    property_id: Optional[int] = Field(
        default=None,
        description="Optional internal integer ID of the Property this document belongs to.",
        examples=[101],
    )
    booking_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Optional reference to the Booking this document belongs to.",
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    )
    lead_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Optional reference to the originating Lead record.",
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    )
    parent_document_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Optional reference to the prior version of this document.",
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    )
    version: int = Field(
        default=1,
        ge=1,
        description="Monotonically increasing version number within a document lineage.",
        examples=[1],
    )
    is_latest_version: bool = Field(
        default=True,
        description="True if this row is the most recent version in its document lineage.",
    )
    title: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Human-readable title of the document.",
        examples=["Aadhaar Card - Front & Back"],
    )
    description: Optional[str] = Field(
        default=None,
        description="Free-form description of the document.",
    )
    category: DocumentCategory = Field(
        default=DocumentCategory.OTHER,
        description="Business classification of the document.",
        examples=["KYC"],
    )
    tags: Optional[dict] = Field(
        default=None,
        description="Arbitrary JSON tag/metadata payload for flexible categorization.",
        examples=[{"priority": "high"}],
    )
    file_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Stored (system-generated) file name.",
        examples=["a1b2c3d4-aadhaar.pdf"],
    )
    original_file_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Original file name as supplied by the uploader.",
        examples=["aadhaar_card.pdf"],
    )
    file_extension: Optional[str] = Field(
        default=None,
        max_length=20,
        description="File extension of the stored content.",
        examples=["pdf"],
    )
    file_type: DocumentFileType = Field(
        ...,
        description="Physical file format of the stored content.",
        examples=["PDF"],
    )
    mime_type: Optional[str] = Field(
        default=None,
        max_length=150,
        description="MIME type of the stored content.",
        examples=["application/pdf"],
    )
    file_size_bytes: int = Field(
        ...,
        ge=0,
        description="Size of the stored file content, in bytes (must be >= 0).",
        examples=[204800],
    )
    checksum_sha256: Optional[str] = Field(
        default=None,
        min_length=64,
        max_length=64,
        description="SHA-256 checksum of the file content.",
        examples=["9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"],
    )
    storage_provider: DocumentStorageProvider = Field(
        default=DocumentStorageProvider.LOCAL,
        description="Physical storage backend hosting the file content.",
        examples=["AWS_S3"],
    )
    storage_bucket: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Storage bucket/container name, if applicable.",
        examples=["crm-documents-prod"],
    )
    storage_path: str = Field(
        ...,
        min_length=1,
        max_length=1024,
        description="Path/key of the file content within the storage provider.",
        examples=["customers/3fa85f64/kyc/aadhaar_card.pdf"],
    )
    storage_url: Optional[str] = Field(
        default=None,
        max_length=2048,
        description="Public or signed URL for retrieving the file content, if applicable.",
        examples=["https://crm-documents-prod.s3.amazonaws.com/customers/3fa85f64/kyc/aadhaar_card.pdf"],
    )
    is_verified: bool = Field(
        default=False,
        description="True once an authorized user has verified the document's authenticity.",
    )
    verified_by_id: Optional[int] = Field(
        default=None,
        description="Internal ID of the User who verified this document, if verified.",
        examples=[7],
    )
    verified_at: Optional[datetime] = Field(
        default=None,
        description="UTC timestamp when the document was verified, if verified.",
        examples=["2026-08-02T10:30:00Z"],
    )
    expiry_date: Optional[date] = Field(
        default=None,
        description="Date after which the document is considered expired.",
        examples=["2030-08-02"],
    )
    uploaded_by_id: int = Field(
        ...,
        description="Internal ID of the User who uploaded this document.",
        examples=[5],
    )
    is_active: bool = Field(
        default=True,
        description="Soft-disable flag; inactive documents are excluded from active views.",
    )

    @field_validator("description", "file_extension", "mime_type", "storage_bucket", "storage_url")
    @classmethod
    def normalize_blank_optional_strings(cls, value: Optional[str]) -> Optional[str]:
        """Normalizes blank optional free-text fields to `None`."""
        return _blank_to_none(value)

    @field_validator("checksum_sha256")
    @classmethod
    def validate_checksum_sha256(cls, value: Optional[str]) -> Optional[str]:
        """
        Validates that a supplied checksum is a well-formed lowercase
        hex-encoded SHA-256 digest.

        Args:
            value: The raw checksum string, or None if not supplied.

        Returns:
            The validated, lowercased checksum, or None.

        Raises:
            ValueError: If the value is not a 64-character hex string.
        """
        if value is None:
            return None
        normalized = value.strip().lower()
        if not normalized:
            return None
        if len(normalized) != 64 or any(c not in "0123456789abcdef" for c in normalized):
            raise ValueError("checksum_sha256 must be a 64-character hex-encoded SHA-256 digest.")
        return normalized

    @model_validator(mode="after")
    def validate_verification_consistency(self) -> "DocumentBase":
        """
        Validates that `is_verified`, `verified_at`, and
        `verified_by_id` are all set together or all unset, mirroring
        `ck_documents_verification_consistency`.

        Returns:
            The validated model instance.

        Raises:
            ValueError: If the verification fields are inconsistent.
        """
        if self.is_verified:
            if self.verified_at is None or self.verified_by_id is None:
                raise ValueError(
                    "verified_at and verified_by_id are required when is_verified is True."
                )
        else:
            if self.verified_at is not None or self.verified_by_id is not None:
                raise ValueError(
                    "verified_at and verified_by_id must be unset when is_verified is False."
                )
        return self


# --------------------------------------------------------------------------
# Document Create Schema
# --------------------------------------------------------------------------
class DocumentCreate(DocumentBase):
    """Request payload for creating a new document."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "customer_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "property_id": None,
                "booking_id": None,
                "lead_id": None,
                "parent_document_id": None,
                "version": 1,
                "is_latest_version": True,
                "title": "Aadhaar Card - Front & Back",
                "description": "KYC identity proof submitted at onboarding.",
                "category": "KYC",
                "tags": {"priority": "high"},
                "file_name": "a1b2c3d4-aadhaar.pdf",
                "original_file_name": "aadhaar_card.pdf",
                "file_extension": "pdf",
                "file_type": "PDF",
                "mime_type": "application/pdf",
                "file_size_bytes": 204800,
                "checksum_sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
                "storage_provider": "AWS_S3",
                "storage_bucket": "crm-documents-prod",
                "storage_path": "customers/3fa85f64/kyc/aadhaar_card.pdf",
                "storage_url": None,
                "is_verified": False,
                "verified_by_id": None,
                "verified_at": None,
                "expiry_date": None,
                "uploaded_by_id": 5,
                "is_active": True,
            }
        },
    )


# --------------------------------------------------------------------------
# Document Update Schema
# --------------------------------------------------------------------------
class DocumentUpdate(BaseModel):
    """
    Request payload for partially updating an existing document (PATCH
    semantics). Every field is optional; only supplied fields are
    intended to be applied by the service layer.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    customer_id: Optional[uuid.UUID] = Field(default=None)
    property_id: Optional[int] = Field(default=None)
    booking_id: Optional[uuid.UUID] = Field(default=None)
    lead_id: Optional[uuid.UUID] = Field(default=None)
    title: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = Field(default=None)
    category: Optional[DocumentCategory] = Field(default=None)
    tags: Optional[dict] = Field(default=None)
    mime_type: Optional[str] = Field(default=None, max_length=150)
    storage_url: Optional[str] = Field(default=None, max_length=2048)
    is_verified: Optional[bool] = Field(default=None)
    verified_by_id: Optional[int] = Field(default=None)
    verified_at: Optional[datetime] = Field(default=None)
    expiry_date: Optional[date] = Field(default=None)
    is_active: Optional[bool] = Field(
        default=None,
        description="Soft-disable flag; inactive documents are excluded from active views.",
    )
    is_deleted: Optional[bool] = Field(
        default=None,
        description="Soft delete flag; deleted documents are excluded everywhere.",
    )
    deleted_by_id: Optional[int] = Field(default=None)

    @field_validator("description", "mime_type", "storage_url")
    @classmethod
    def normalize_blank_optional_strings(cls, value: Optional[str]) -> Optional[str]:
        """Normalizes blank optional free-text fields to `None`."""
        return _blank_to_none(value)


# --------------------------------------------------------------------------
# Document Response Schema
# --------------------------------------------------------------------------
class DocumentResponse(DocumentBase):
    """
    Outward-facing representation of a document, returned by document
    retrieval and mutation endpoints. Extends `DocumentBase` with
    identity, soft-delete, and audit fields populated by the
    persistence layer.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(
        ...,
        description="Globally unique identifier of the document record.",
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    )
    is_deleted: bool = Field(
        default=False,
        description="Soft delete flag; deleted documents are excluded everywhere.",
    )
    deleted_at: Optional[datetime] = Field(
        default=None,
        description="UTC timestamp when the document was soft-deleted, if deleted.",
    )
    deleted_by_id: Optional[int] = Field(
        default=None,
        description="Internal ID of the User who soft-deleted this document, if deleted.",
    )
    created_by_id: int = Field(
        ...,
        description="Internal ID of the User who created this document record.",
        examples=[5],
    )
    updated_by_id: Optional[int] = Field(
        default=None,
        description="Internal ID of the User who last modified this document record.",
        examples=[5],
    )
    created_at: datetime = Field(
        ...,
        description="UTC timestamp when the document record was created.",
        examples=["2026-08-02T10:30:00Z"],
    )
    updated_at: datetime = Field(
        ...,
        description="UTC timestamp when the document record was last updated.",
        examples=["2026-08-02T10:30:00Z"],
    )


# --------------------------------------------------------------------------
# Document List Response Schema
# --------------------------------------------------------------------------
class DocumentListResponse(BaseModel):
    """Paginated collection response for document listing/search endpoints."""

    model_config = ConfigDict(from_attributes=True)

    items: list[DocumentResponse] = Field(
        ...,
        description="The page of document records matching the query.",
    )
    total: int = Field(
        ...,
        ge=0,
        description="Total number of document records matching the query, across all pages.",
        examples=[86],
    )
    page: int = Field(
        ...,
        ge=1,
        description="Current page number (1-indexed).",
        examples=[1],
    )
    page_size: int = Field(
        ...,
        ge=1,
        description="Number of records requested per page.",
        examples=[20],
    )
    total_pages: int = Field(
        ...,
        ge=0,
        description="Total number of pages available for the given page_size.",
        examples=[5],
    )


# --------------------------------------------------------------------------
# Document Filter Schema
# --------------------------------------------------------------------------
class DocumentFilter(BaseModel):
    """
    Query parameters for searching and filtering documents, including
    pagination and sorting controls.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    customer_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Filter by the document's Customer ID.",
    )
    property_id: Optional[int] = Field(
        default=None,
        description="Filter by the document's Property ID.",
    )
    booking_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Filter by the document's Booking ID.",
    )
    lead_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Filter by the originating Lead ID.",
    )
    category: Optional[DocumentCategory] = Field(
        default=None,
        description="Filter by business classification.",
    )
    file_type: Optional[DocumentFileType] = Field(
        default=None,
        description="Filter by physical file format.",
    )
    storage_provider: Optional[DocumentStorageProvider] = Field(
        default=None,
        description="Filter by storage backend.",
    )
    is_verified: Optional[bool] = Field(
        default=None,
        description="Filter by verification status.",
    )
    is_active: Optional[bool] = Field(
        default=None,
        description="Filter by soft-disable status.",
    )
    is_deleted: Optional[bool] = Field(
        default=False,
        description="Filter by soft-delete status. Defaults to excluding deleted documents.",
    )
    uploaded_by_id: Optional[int] = Field(
        default=None,
        description="Filter by the internal User ID of the uploader.",
    )
    expiring_before: Optional[date] = Field(
        default=None,
        description="Filter to documents expiring on or before this date.",
    )
    search: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Free-text search term matched against title/original_file_name.",
        examples=["aadhaar"],
    )
    page: int = Field(
        default=1,
        ge=1,
        description="Page number to retrieve (1-indexed).",
        examples=[1],
    )
    page_size: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Number of records to return per page (max 100).",
        examples=[20],
    )
    sort_by: str = Field(
        default="created_at",
        description=(
            "Field to sort results by. Allowed values: "
            "title, category, file_type, storage_provider, file_size_bytes, "
            "version, expiry_date, is_verified, created_at, updated_at."
        ),
        examples=["created_at"],
    )
    sort_order: str = Field(
        default="desc",
        description="Sort direction: 'asc' or 'desc'.",
        examples=["desc"],
    )

    @field_validator("search")
    @classmethod
    def validate_search(cls, value: Optional[str]) -> Optional[str]:
        """
        Rejects a search term that is empty after whitespace stripping,
        since an effectively-blank search string would otherwise match
        every record and defeat the purpose of filtering.

        Args:
            value: The raw search string, or None if not supplied.

        Returns:
            The validated, stripped search string, or None.

        Raises:
            ValueError: If the search string is empty after stripping.
        """
        if value is not None and not value.strip():
            raise ValueError("Search term must not be empty or whitespace only.")
        return value

    @field_validator("sort_by")
    @classmethod
    def validate_sort_by(cls, value: str) -> str:
        """
        Validates that `sort_by` references an allowed, indexable
        column to prevent arbitrary/unsafe field references reaching
        the query layer.

        Args:
            value: The requested sort field name.

        Returns:
            The validated sort field name.

        Raises:
            ValueError: If the field is not in the allowed sort fields.
        """
        if value not in _ALLOWED_SORT_FIELDS:
            allowed = ", ".join(sorted(_ALLOWED_SORT_FIELDS))
            raise ValueError(f"sort_by must be one of: {allowed}.")
        return value

    @field_validator("sort_order")
    @classmethod
    def validate_sort_order(cls, value: str) -> str:
        """
        Validates that `sort_order` is either 'asc' or 'desc'
        (case-insensitive), normalizing the result to lowercase.

        Args:
            value: The requested sort direction.

        Returns:
            The normalized sort direction ('asc' or 'desc').

        Raises:
            ValueError: If the value is not 'asc' or 'desc'.
        """
        normalized = value.strip().lower()
        if normalized not in {"asc", "desc"}:
            raise ValueError("sort_order must be either 'asc' or 'desc'.")
        return normalized


__all__ = [
    "DocumentBase",
    "DocumentCreate",
    "DocumentUpdate",
    "DocumentResponse",
    "DocumentListResponse",
    "DocumentFilter",
]