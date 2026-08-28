# backend/app/services/document_service.py
"""
backend/app/services/document_service.py

Service (business-rules) layer for the Document module.

Generated exclusively from the approved `app.models.document.Document`
ORM model, `app.schemas.document` schemas, and
`app.repositories.document_repository.DocumentRepository`. This layer
owns every business rule for documents: duplicate validation, version
lineage management, verification consistency, soft delete / restore
semantics, and statistics assembly. It contains no direct database
access -- all persistence goes through `DocumentRepository`.

Conventions:
    - Raises only `app.core.exceptions` domain exceptions -- never
      raw SQLAlchemy, Pydantic, or generic exceptions.
    - Every mutating method accepts the acting user's id explicitly
      (`actor_id`) for audit-trail (`created_by_id` / `updated_by_id`
      / `deleted_by_id`) purposes -- it is never inferred here.
    - Returns ORM `Document` instances (or plain dicts for
      statistics); translation to `DocumentResponse` /
      `DocumentListResponse` is the router's responsibility.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from app.core.exceptions import (
    BusinessRuleException,
    ConflictException,
    DuplicateResourceException,
    NotFoundException,
)
from app.models.document import Document
from app.repositories.document_repository import DocumentRepository
from app.schemas.document import (
    DocumentCreate,
    DocumentFilter,
    DocumentListResponse,
    DocumentUpdate,
)


class DocumentService:
    """Business-rule orchestration for the `Document` entity."""

    def __init__(self, repository: DocumentRepository) -> None:
        self._repository = repository

    # ------------------------------------------------------------------
    # Create / Upload Metadata
    # ------------------------------------------------------------------
    async def upload_document_metadata(
        self, payload: DocumentCreate, *, actor_id: int
    ) -> Document:
        """
        Registers metadata for a newly uploaded file.

        Business rules enforced:
            - The file content must not already exist for the same
              owning-entity scope (duplicate checksum rejection).
            - If `parent_document_id` is supplied, the parent must
              exist, must not itself be deleted, and every prior
              version in the lineage is demoted so the new row is the
              sole `is_latest_version = True` record.
            - `created_by_id` is always set to `uploaded_by_id` on
              first creation.

        Args:
            payload: Validated document creation payload.
            actor_id: The id of the user performing the upload.

        Returns:
            The newly created `Document`.

        Raises:
            DuplicateResourceException: If a non-deleted document with
                the same checksum already exists in the same scope.
            NotFoundException: If `parent_document_id` is supplied but
                does not reference an existing document.
            BusinessRuleException: If `parent_document_id` references
                a soft-deleted document.
        """
        data = payload.model_dump(exclude={"verified_by_id", "verified_at"} if not payload.is_verified else set())

        if payload.checksum_sha256:
            await self._assert_no_duplicate(
                checksum_sha256=payload.checksum_sha256,
                customer_id=payload.customer_id,
                property_id=payload.property_id,
                booking_id=payload.booking_id,
                lead_id=payload.lead_id,
                exclude_id=None,
            )

        parent: Optional[Document] = None
        if payload.parent_document_id is not None:
            parent = await self._repository.get_by_id(
                payload.parent_document_id, include_deleted=True
            )
            if parent is None:
                raise NotFoundException(
                    f"Document {payload.parent_document_id} was not found."
                )
            if parent.is_deleted:
                raise BusinessRuleException(
                    "Cannot version a soft-deleted document."
                )

        data["created_by_id"] = actor_id
        data["updated_by_id"] = None
        document = await self._repository.create(data)

        if parent is not None:
            lineage_root_id = parent.parent_document_id or parent.id
            await self._repository.unset_latest_version(lineage_root_id)
            await self._repository.update(document, {"is_latest_version": True})

        return document

    # Alias matching common CRUD naming -- identical behavior.
    async def create_document(
        self, payload: DocumentCreate, *, actor_id: int
    ) -> Document:
        """Alias of `upload_document_metadata` for CRUD-style callers."""
        return await self.upload_document_metadata(payload, actor_id=actor_id)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    async def get_document(
        self, document_id: uuid.UUID, *, include_deleted: bool = False
    ) -> Document:
        """
        Retrieves a single document by id.

        Args:
            document_id: The document's UUID.
            include_deleted: When True, soft-deleted documents are
                also eligible to be returned.

        Returns:
            The matching `Document`.

        Raises:
            NotFoundException: If no matching document exists.
        """
        return await self._repository.get_by_id_or_raise(
            document_id, include_deleted=include_deleted
        )

    async def get_latest_version(self, document_id: uuid.UUID) -> Document:
        """
        Resolves the latest version within a document's lineage.

        Args:
            document_id: Any document id within the lineage.

        Returns:
            The latest `Document` in that lineage.

        Raises:
            NotFoundException: If the lineage cannot be resolved.
        """
        latest = await self._repository.get_latest_version(document_id)
        if latest is None:
            raise NotFoundException(f"Document {document_id} was not found.")
        return latest

    # ------------------------------------------------------------------
    # Search / Pagination / Sorting / Filtering
    # ------------------------------------------------------------------
    async def search_documents(self, filters: DocumentFilter) -> DocumentListResponse:
        """
        Executes a filtered, sorted, paginated document search and
        assembles the paginated response envelope.

        Args:
            filters: Validated `DocumentFilter` query parameters.

        Returns:
            A `DocumentListResponse` with items, total count, and
            pagination metadata.
        """
        items, total = await self._repository.search(filters)
        total_pages = math.ceil(total / filters.page_size) if total else 0
        return DocumentListResponse(
            items=items,
            total=total,
            page=filters.page,
            page_size=filters.page_size,
            total_pages=total_pages,
        )

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------
    async def update_document(
        self,
        document_id: uuid.UUID,
        payload: DocumentUpdate,
        *,
        actor_id: int,
    ) -> Document:
        """
        Applies a partial update to an existing document.

        Business rules enforced:
            - Cannot update a soft-deleted document (must be restored
              first).
            - Verification fields (`is_verified`, `verified_by_id`,
              `verified_at`) must remain mutually consistent after the
              patch is applied.
            - `updated_by_id` is always stamped with `actor_id`.

        Args:
            document_id: The document's UUID.
            payload: Validated partial update payload.
            actor_id: The id of the user performing the update.

        Returns:
            The updated `Document`.

        Raises:
            NotFoundException: If no matching document exists.
            ConflictException: If the document is soft-deleted.
            BusinessRuleException: If the resulting verification
                fields would be inconsistent.
        """
        document = await self._repository.get_by_id_or_raise(document_id)
        if document.is_deleted:
            raise ConflictException(f"Document {document_id} is already deleted.")

        data = payload.model_dump(exclude_unset=True, exclude={"is_deleted", "deleted_by_id"})

        resulting_is_verified = data.get("is_verified", document.is_verified)
        resulting_verified_by_id = data.get("verified_by_id", document.verified_by_id)
        resulting_verified_at = data.get("verified_at", document.verified_at)

        if resulting_is_verified:
            if resulting_verified_by_id is None or resulting_verified_at is None:
                if "verified_at" not in data and resulting_verified_at is None:
                    data["verified_at"] = datetime.now(timezone.utc)
                if "verified_by_id" not in data and resulting_verified_by_id is None:
                    data["verified_by_id"] = actor_id
        else:
            if resulting_verified_by_id is not None or resulting_verified_at is not None:
                raise BusinessRuleException(
                    "verified_by_id and verified_at must be unset when is_verified is False."
                )

        data["updated_by_id"] = actor_id
        return await self._repository.update(document, data)

    # ------------------------------------------------------------------
    # Soft Delete
    # ------------------------------------------------------------------
    async def delete_document(self, document_id: uuid.UUID, *, actor_id: int) -> Document:
        """
        Soft-deletes a single document.

        Args:
            document_id: The document's UUID.
            actor_id: The id of the user performing the deletion.

        Returns:
            The soft-deleted `Document`.

        Raises:
            NotFoundException: If no matching document exists.
            ConflictException: If the document is already
                soft-deleted.
        """
        document = await self._repository.get_by_id_or_raise(
            document_id, include_deleted=True
        )
        if document.is_deleted:
            raise ConflictException(f"Document {document_id} is already deleted.")
        return await self._repository.soft_delete(document, deleted_by_id=actor_id)

    async def bulk_delete_documents(
        self, document_ids: Sequence[uuid.UUID], *, actor_id: int
    ) -> dict[str, Any]:
        """
        Soft-deletes multiple documents in bulk.

        Business rules enforced:
            - Ids that do not resolve to an existing, non-deleted
              document are reported back rather than silently ignored.

        Args:
            document_ids: The ids of the documents to soft-delete.
            actor_id: The id of the user performing the deletion.

        Returns:
            A dict with `deleted_count`, `requested_count`, and
            `not_found_ids` (ids that could not be soft-deleted).
        """
        unique_ids = list(dict.fromkeys(document_ids))
        if not unique_ids:
            raise BusinessRuleException("No document ids were provided.")

        existing = await self._repository.get_by_ids(unique_ids, include_deleted=False)
        existing_ids = {doc.id for doc in existing}
        not_found_ids = [str(doc_id) for doc_id in unique_ids if doc_id not in existing_ids]

        deleted_count = await self._repository.bulk_soft_delete(
            list(existing_ids), deleted_by_id=actor_id
        )
        return {
            "deleted_count": deleted_count,
            "requested_count": len(unique_ids),
            "not_found_ids": not_found_ids,
        }

    # ------------------------------------------------------------------
    # Restore
    # ------------------------------------------------------------------
    async def restore_document(self, document_id: uuid.UUID, *, actor_id: int) -> Document:
        """
        Reverses a soft delete on a single document.

        Args:
            document_id: The document's UUID.
            actor_id: The id of the user performing the restore
                (stamped onto `updated_by_id`).

        Returns:
            The restored `Document`.

        Raises:
            NotFoundException: If no matching document exists at all
                (including soft-deleted rows).
            ConflictException: If the document is not currently
                soft-deleted.
        """
        document = await self._repository.get_by_id_or_raise(
            document_id, include_deleted=True
        )
        if not document.is_deleted:
            raise ConflictException(f"Document {document_id} is not deleted.")
        restored = await self._repository.restore(document)
        return await self._repository.update(restored, {"updated_by_id": actor_id})

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------
    async def get_statistics(
        self,
        *,
        customer_id: Optional[uuid.UUID] = None,
        property_id: Optional[int] = None,
        booking_id: Optional[uuid.UUID] = None,
        lead_id: Optional[uuid.UUID] = None,
    ) -> dict[str, Any]:
        """
        Assembles document statistics, optionally scoped to a single
        owning entity.

        Args:
            customer_id: Restrict statistics to this customer, if set.
            property_id: Restrict statistics to this property, if set.
            booking_id: Restrict statistics to this booking, if set.
            lead_id: Restrict statistics to this lead, if set.

        Returns:
            A statistics dict (see
            `DocumentRepository.get_statistics` for shape).
        """
        return await self._repository.get_statistics(
            customer_id=customer_id,
            property_id=property_id,
            booking_id=booking_id,
            lead_id=lead_id,
        )

    # ------------------------------------------------------------------
    # Duplicate Validation
    # ------------------------------------------------------------------
    async def _assert_no_duplicate(
        self,
        *,
        checksum_sha256: str,
        customer_id: Optional[uuid.UUID],
        property_id: Optional[int],
        booking_id: Optional[uuid.UUID],
        lead_id: Optional[uuid.UUID],
        exclude_id: Optional[uuid.UUID],
    ) -> None:
        """
        Raises if a non-deleted document with the same checksum
        already exists within the same owning-entity scope.

        Raises:
            DuplicateResourceException: If a duplicate is found.
        """
        duplicate = await self._repository.find_duplicate(
            checksum_sha256=checksum_sha256,
            customer_id=customer_id,
            property_id=property_id,
            booking_id=booking_id,
            lead_id=lead_id,
            exclude_id=exclude_id,
        )
        if duplicate is not None:
            raise DuplicateResourceException(
                f"A document with checksum {checksum_sha256} already exists "
                f"(document id: {duplicate.id})."
            )

    async def validate_duplicate(
        self,
        *,
        checksum_sha256: str,
        customer_id: Optional[uuid.UUID] = None,
        property_id: Optional[int] = None,
        booking_id: Optional[uuid.UUID] = None,
        lead_id: Optional[uuid.UUID] = None,
    ) -> bool:
        """
        Public pre-flight duplicate check (e.g. before initiating an
        upload), without mutating any state.

        Args:
            checksum_sha256: The SHA-256 digest to check.
            customer_id: Owning customer scope (if any).
            property_id: Owning property scope (if any).
            booking_id: Owning booking scope (if any).
            lead_id: Owning lead scope (if any).

        Returns:
            True if no duplicate exists (i.e. it is safe to upload).

        Raises:
            DuplicateResourceException: If a duplicate already exists.
        """
        await self._assert_no_duplicate(
            checksum_sha256=checksum_sha256,
            customer_id=customer_id,
            property_id=property_id,
            booking_id=booking_id,
            lead_id=lead_id,
            exclude_id=None,
        )
        return True