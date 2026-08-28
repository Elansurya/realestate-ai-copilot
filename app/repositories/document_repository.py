# backend/app/repositories/document_repository.py
"""
backend/app/repositories/document_repository.py

Repository (data-access) layer for the Document module.

Generated exclusively from the approved `app.models.document.Document`
ORM model and `app.schemas.document` schemas. Contains no business
rules -- those belong to `app.services.document_service`. This layer
only knows how to talk to the database.

Conventions:
    - Async SQLAlchemy 2.x, `AsyncSession` injected by the caller
      (service layer owns the session/transaction boundary).
    - Every read excludes soft-deleted rows (`is_deleted = false`)
      unless the caller explicitly opts in via `include_deleted`.
    - Raises only `app.core.exceptions` domain exceptions -- never
      raw SQLAlchemy or generic exceptions.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import Select, asc, desc, func, or_, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException
from app.models.document import Document
from app.schemas.document import DocumentFilter


class DocumentRepository:
    """Persistence operations for the `Document` entity."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------
    async def create(self, data: dict[str, Any]) -> Document:
        """
        Inserts a new document row.

        Args:
            data: Column values for the new `Document`, already
                validated by the service/schema layer.

        Returns:
            The persisted `Document`, refreshed from the database.
        """
        document = Document(**data)
        self._session.add(document)
        await self._session.flush()
        await self._session.refresh(document)
        return document

    # ------------------------------------------------------------------
    # Read (single)
    # ------------------------------------------------------------------
    async def get_by_id(
        self,
        document_id: uuid.UUID,
        *,
        include_deleted: bool = False,
    ) -> Optional[Document]:
        """
        Fetches a single document by primary key.

        Args:
            document_id: The document's UUID.
            include_deleted: When True, soft-deleted rows are also
                eligible to be returned.

        Returns:
            The matching `Document`, or None if not found.
        """
        stmt = select(Document).where(Document.id == document_id)
        if not include_deleted:
            stmt = stmt.where(Document.is_deleted.is_(False))
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id_or_raise(
        self,
        document_id: uuid.UUID,
        *,
        include_deleted: bool = False,
    ) -> Document:
        """
        Fetches a single document by primary key or raises.

        Args:
            document_id: The document's UUID.
            include_deleted: When True, soft-deleted rows are also
                eligible to be returned.

        Returns:
            The matching `Document`.

        Raises:
            NotFoundException: If no matching document exists.
        """
        document = await self.get_by_id(document_id, include_deleted=include_deleted)
        if document is None:
            raise NotFoundException(f"Document {document_id} was not found.")
        return document

    async def get_by_ids(
        self,
        document_ids: Sequence[uuid.UUID],
        *,
        include_deleted: bool = False,
    ) -> list[Document]:
        """
        Fetches multiple documents by primary key.

        Args:
            document_ids: The UUIDs to fetch.
            include_deleted: When True, soft-deleted rows are also
                eligible to be returned.

        Returns:
            The matching `Document` rows (may be fewer than requested
            if some ids do not exist or are excluded by the deleted
            filter).
        """
        if not document_ids:
            return []
        stmt = select(Document).where(Document.id.in_(document_ids))
        if not include_deleted:
            stmt = stmt.where(Document.is_deleted.is_(False))
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_latest_version(
        self, document_id: uuid.UUID
    ) -> Optional[Document]:
        """
        Resolves the latest version within the lineage that
        `document_id` belongs to (walks via `parent_document_id`
        chains rooted at the same lineage).

        Args:
            document_id: Any document id within the lineage.

        Returns:
            The `Document` row flagged `is_latest_version = True`
            within that lineage, or None if the lineage is empty.
        """
        anchor = await self.get_by_id(document_id, include_deleted=True)
        if anchor is None:
            return None
        root_id = anchor.parent_document_id or anchor.id
        stmt = select(Document).where(
            or_(Document.id == root_id, Document.parent_document_id == root_id),
            Document.is_latest_version.is_(True),
            Document.is_deleted.is_(False),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Duplicate Validation
    # ------------------------------------------------------------------
    async def find_duplicate(
        self,
        *,
        checksum_sha256: str,
        customer_id: Optional[uuid.UUID],
        property_id: Optional[int],
        booking_id: Optional[uuid.UUID],
        lead_id: Optional[uuid.UUID],
        exclude_id: Optional[uuid.UUID] = None,
    ) -> Optional[Document]:
        """
        Looks for an existing, non-deleted document with the same
        checksum within the same owning-entity scope.

        Args:
            checksum_sha256: The SHA-256 digest to match.
            customer_id: Owning customer scope (if any).
            property_id: Owning property scope (if any).
            booking_id: Owning booking scope (if any).
            lead_id: Owning lead scope (if any).
            exclude_id: A document id to exclude from the match (used
                when validating an update rather than a create).

        Returns:
            The first matching `Document`, or None if no duplicate
            exists.
        """
        stmt = select(Document).where(
            Document.checksum_sha256 == checksum_sha256,
            Document.is_deleted.is_(False),
            Document.customer_id.is_(customer_id) if customer_id is None else Document.customer_id == customer_id,
            Document.property_id.is_(property_id) if property_id is None else Document.property_id == property_id,
            Document.booking_id.is_(booking_id) if booking_id is None else Document.booking_id == booking_id,
            Document.lead_id.is_(lead_id) if lead_id is None else Document.lead_id == lead_id,
        )
        if exclude_id is not None:
            stmt = stmt.where(Document.id != exclude_id)
        stmt = stmt.limit(1)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------
    async def update(self, document: Document, data: dict[str, Any]) -> Document:
        """
        Applies a partial set of column updates to an already-loaded
        document instance.

        Args:
            document: The `Document` instance to mutate (must be
                attached to the current session).
            data: Column values to apply.

        Returns:
            The updated `Document`, refreshed from the database.
        """
        for field, value in data.items():
            setattr(document, field, value)
        await self._session.flush()
        await self._session.refresh(document)
        return document

    async def unset_latest_version(self, lineage_root_id: uuid.UUID) -> None:
        """
        Clears `is_latest_version` for every row in a document lineage
        (the root document plus every document whose
        `parent_document_id` points at the root), ahead of a new
        version being promoted.

        Args:
            lineage_root_id: The id of the first document in the
                lineage (the row with `parent_document_id IS NULL`).
        """
        stmt = (
            sa_update(Document)
            .where(
                or_(
                    Document.id == lineage_root_id,
                    Document.parent_document_id == lineage_root_id,
                ),
                Document.is_latest_version.is_(True),
            )
            .values(is_latest_version=False)
        )
        await self._session.execute(stmt)
        await self._session.flush()

    # ------------------------------------------------------------------
    # Soft Delete / Restore
    # ------------------------------------------------------------------
    async def soft_delete(self, document: Document, *, deleted_by_id: int) -> Document:
        """
        Soft-deletes a single document.

        Args:
            document: The `Document` instance to soft-delete.
            deleted_by_id: The acting user's id.

        Returns:
            The soft-deleted `Document`.
        """
        document.is_deleted = True
        document.deleted_at = datetime.now(timezone.utc)
        document.deleted_by_id = deleted_by_id
        await self._session.flush()
        await self._session.refresh(document)
        return document

    async def bulk_soft_delete(
        self, document_ids: Sequence[uuid.UUID], *, deleted_by_id: int
    ) -> int:
        """
        Soft-deletes multiple documents in a single statement.

        Args:
            document_ids: The ids of the documents to soft-delete.
            deleted_by_id: The acting user's id.

        Returns:
            The number of rows affected.
        """
        if not document_ids:
            return 0
        stmt = (
            sa_update(Document)
            .where(Document.id.in_(document_ids), Document.is_deleted.is_(False))
            .values(
                is_deleted=True,
                deleted_at=datetime.now(timezone.utc),
                deleted_by_id=deleted_by_id,
            )
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount or 0

    async def restore(self, document: Document) -> Document:
        """
        Reverses a soft delete on a single document.

        Args:
            document: The `Document` instance to restore.

        Returns:
            The restored `Document`.
        """
        document.is_deleted = False
        document.deleted_at = None
        document.deleted_by_id = None
        await self._session.flush()
        await self._session.refresh(document)
        return document

    # ------------------------------------------------------------------
    # Search / Filter / Sort / Pagination
    # ------------------------------------------------------------------
    def _apply_filters(self, stmt: Select, filters: DocumentFilter) -> Select:
        """Applies `DocumentFilter` predicates to a base SELECT statement."""
        if filters.customer_id is not None:
            stmt = stmt.where(Document.customer_id == filters.customer_id)
        if filters.property_id is not None:
            stmt = stmt.where(Document.property_id == filters.property_id)
        if filters.booking_id is not None:
            stmt = stmt.where(Document.booking_id == filters.booking_id)
        if filters.lead_id is not None:
            stmt = stmt.where(Document.lead_id == filters.lead_id)
        if filters.category is not None:
            stmt = stmt.where(Document.category == filters.category)
        if filters.file_type is not None:
            stmt = stmt.where(Document.file_type == filters.file_type)
        if filters.storage_provider is not None:
            stmt = stmt.where(Document.storage_provider == filters.storage_provider)
        if filters.is_verified is not None:
            stmt = stmt.where(Document.is_verified == filters.is_verified)
        if filters.is_active is not None:
            stmt = stmt.where(Document.is_active == filters.is_active)
        if filters.is_deleted is not None:
            stmt = stmt.where(Document.is_deleted == filters.is_deleted)
        if filters.uploaded_by_id is not None:
            stmt = stmt.where(Document.uploaded_by_id == filters.uploaded_by_id)
        if filters.expiring_before is not None:
            stmt = stmt.where(
                Document.expiry_date.is_not(None),
                Document.expiry_date <= filters.expiring_before,
            )
        if filters.search:
            term = f"%{filters.search.strip().lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(Document.title).like(term),
                    func.lower(Document.original_file_name).like(term),
                )
            )
        return stmt

    async def search(self, filters: DocumentFilter) -> tuple[list[Document], int]:
        """
        Executes a filtered, sorted, paginated document search.

        Args:
            filters: Validated `DocumentFilter` query parameters.

        Returns:
            A tuple of (page of `Document` rows, total matching count
            across all pages).
        """
        base_stmt = self._apply_filters(select(Document), filters)

        count_stmt = self._apply_filters(
            select(func.count()).select_from(Document), filters
        )
        total_result = await self._session.execute(count_stmt)
        total = total_result.scalar_one()

        sort_column = getattr(Document, filters.sort_by)
        order_fn = desc if filters.sort_order == "desc" else asc
        page_stmt = (
            base_stmt.order_by(order_fn(sort_column))
            .offset((filters.page - 1) * filters.page_size)
            .limit(filters.page_size)
        )
        result = await self._session.execute(page_stmt)
        items = list(result.scalars().all())
        return items, total

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
        Aggregates document counts and storage usage, optionally
        scoped to a single owning entity.

        Args:
            customer_id: Restrict statistics to this customer, if set.
            property_id: Restrict statistics to this property, if set.
            booking_id: Restrict statistics to this booking, if set.
            lead_id: Restrict statistics to this lead, if set.

        Returns:
            A dict with total/active/deleted/verified counts, storage
            size totals, and breakdowns by category, file type, and
            storage provider.
        """

        def _scope(stmt: Select) -> Select:
            if customer_id is not None:
                stmt = stmt.where(Document.customer_id == customer_id)
            if property_id is not None:
                stmt = stmt.where(Document.property_id == property_id)
            if booking_id is not None:
                stmt = stmt.where(Document.booking_id == booking_id)
            if lead_id is not None:
                stmt = stmt.where(Document.lead_id == lead_id)
            return stmt

        not_deleted = _scope(select(Document).where(Document.is_deleted.is_(False)))

        total_stmt = _scope(
            select(func.count()).select_from(Document).where(Document.is_deleted.is_(False))
        )
        deleted_stmt = _scope(
            select(func.count()).select_from(Document).where(Document.is_deleted.is_(True))
        )
        verified_stmt = _scope(
            select(func.count())
            .select_from(Document)
            .where(Document.is_deleted.is_(False), Document.is_verified.is_(True))
        )
        active_stmt = _scope(
            select(func.count())
            .select_from(Document)
            .where(Document.is_deleted.is_(False), Document.is_active.is_(True))
        )
        expired_stmt = _scope(
            select(func.count())
            .select_from(Document)
            .where(
                Document.is_deleted.is_(False),
                Document.expiry_date.is_not(None),
                Document.expiry_date < date.today(),
            )
        )
        storage_size_stmt = _scope(
            select(func.coalesce(func.sum(Document.file_size_bytes), 0)).where(
                Document.is_deleted.is_(False)
            )
        )
        by_category_stmt = _scope(
            select(Document.category, func.count())
            .where(Document.is_deleted.is_(False))
            .group_by(Document.category)
        )
        by_file_type_stmt = _scope(
            select(Document.file_type, func.count())
            .where(Document.is_deleted.is_(False))
            .group_by(Document.file_type)
        )
        by_storage_provider_stmt = _scope(
            select(Document.storage_provider, func.count())
            .where(Document.is_deleted.is_(False))
            .group_by(Document.storage_provider)
        )

        total = (await self._session.execute(total_stmt)).scalar_one()
        deleted = (await self._session.execute(deleted_stmt)).scalar_one()
        verified = (await self._session.execute(verified_stmt)).scalar_one()
        active = (await self._session.execute(active_stmt)).scalar_one()
        expired = (await self._session.execute(expired_stmt)).scalar_one()
        total_storage_bytes = (await self._session.execute(storage_size_stmt)).scalar_one()
        by_category = dict(
            (row[0], row[1])
            for row in (await self._session.execute(by_category_stmt)).all()
        )
        by_file_type = dict(
            (row[0], row[1])
            for row in (await self._session.execute(by_file_type_stmt)).all()
        )
        by_storage_provider = dict(
            (row[0], row[1])
            for row in (await self._session.execute(by_storage_provider_stmt)).all()
        )

        return {
            "total_documents": total,
            "active_documents": active,
            "deleted_documents": deleted,
            "verified_documents": verified,
            "unverified_documents": total - verified,
            "expired_documents": expired,
            "total_storage_bytes": int(total_storage_bytes),
            "by_category": by_category,
            "by_file_type": by_file_type,
            "by_storage_provider": by_storage_provider,
        }

    # ------------------------------------------------------------------
    # Existence Helpers
    # ------------------------------------------------------------------
    async def exists(self, document_id: uuid.UUID) -> bool:
        """
        Checks whether a non-deleted document exists for the given id.

        Args:
            document_id: The document's UUID.

        Returns:
            True if a matching, non-deleted row exists.
        """
        stmt = select(func.count()).select_from(Document).where(
            Document.id == document_id, Document.is_deleted.is_(False)
        )
        result = await self._session.execute(stmt)
        return (result.scalar_one() or 0) > 0