"""
backend/tests/test_document_service.py

Unit tests for `app.services.document_service.DocumentService`.

`DocumentRepository` is fully mocked (`AsyncMock`) so these tests
exercise only the service layer's business rules: duplicate
validation, version-lineage management, verification consistency,
soft-delete / restore state machines, bulk-delete accounting, and
statistics pass-through.

Run with:
    pytest backend/tests/test_document_service.py -v
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import (
    BusinessRuleException,
    BusinessRuleViolationError,
    ConflictException,
    DuplicateResourceException,
    NotFoundException,
)
from app.models.document import Document, DocumentCategory, DocumentFileType, DocumentStorageProvider
from app.schemas.document import DocumentCreate, DocumentFilter, DocumentUpdate
from app.services.document_service import DocumentService


ACTOR_ID = 42


def make_document(**overrides) -> Document:
    defaults = dict(
        id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        property_id=None,
        booking_id=None,
        lead_id=None,
        parent_document_id=None,
        version=1,
        is_latest_version=True,
        title="Aadhaar Card",
        description=None,
        category=DocumentCategory.KYC,
        tags=None,
        file_name="abc123_aadhaar.pdf",
        original_file_name="aadhaar.pdf",
        file_extension="pdf",
        file_type=DocumentFileType.PDF,
        mime_type="application/pdf",
        file_size_bytes=1024,
        checksum_sha256="a" * 64,
        storage_provider=DocumentStorageProvider.LOCAL,
        storage_bucket=None,
        storage_path="kyc/customer/abc123_aadhaar.pdf",
        storage_url=None,
        is_verified=False,
        verified_by_id=None,
        verified_at=None,
        expiry_date=None,
        uploaded_by_id=5,
        is_active=True,
        is_deleted=False,
        deleted_at=None,
        deleted_by_id=None,
        created_by_id=5,
        updated_by_id=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return Document(**defaults)


def make_create_payload(**overrides) -> DocumentCreate:
    defaults = dict(
        title="Aadhaar Card",
        file_name="abc123_aadhaar.pdf",
        original_file_name="aadhaar.pdf",
        file_type=DocumentFileType.PDF,
        file_size_bytes=1024,
        storage_path="kyc/customer/abc123_aadhaar.pdf",
        uploaded_by_id=5,
        checksum_sha256="a" * 64,
    )
    defaults.update(overrides)
    return DocumentCreate(**defaults)


@pytest.fixture
def mock_repo() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def service(mock_repo: AsyncMock) -> DocumentService:
    return DocumentService(mock_repo)


# --------------------------------------------------------------------------
# upload_document_metadata() / create_document()
# --------------------------------------------------------------------------
class TestUpload:
    @pytest.mark.asyncio
    async def test_upload_rejects_duplicate_checksum(self, service, mock_repo):
        mock_repo.find_duplicate.return_value = make_document()
        payload = make_create_payload()

        with pytest.raises(DuplicateResourceException):
            await service.upload_document_metadata(payload, actor_id=ACTOR_ID)

        mock_repo.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_upload_succeeds_when_no_duplicate(self, service, mock_repo):
        mock_repo.find_duplicate.return_value = None
        created = make_document()
        mock_repo.create.return_value = created
        payload = make_create_payload()

        result = await service.upload_document_metadata(payload, actor_id=ACTOR_ID)

        assert result is created
        create_call_kwargs = mock_repo.create.await_args.args[0]
        assert create_call_kwargs["created_by_id"] == ACTOR_ID

    @pytest.mark.asyncio
    async def test_upload_with_missing_parent_raises_not_found(self, service, mock_repo):
        mock_repo.find_duplicate.return_value = None
        mock_repo.get_by_id.return_value = None
        payload = make_create_payload(parent_document_id=uuid.uuid4())

        with pytest.raises(NotFoundException):
            await service.upload_document_metadata(payload, actor_id=ACTOR_ID)

    @pytest.mark.asyncio
    async def test_upload_with_deleted_parent_raises_invalid_state(self, service, mock_repo):
        mock_repo.find_duplicate.return_value = None
        mock_repo.get_by_id.return_value = make_document(is_deleted=True)
        payload = make_create_payload(parent_document_id=uuid.uuid4())

        with pytest.raises(BusinessRuleException):
            await service.upload_document_metadata(payload, actor_id=ACTOR_ID)

    @pytest.mark.asyncio
    async def test_upload_new_version_demotes_previous_latest(self, service, mock_repo):
        mock_repo.find_duplicate.return_value = None
        parent_id = uuid.uuid4()
        lineage_root_id = uuid.uuid4()
        parent = make_document(id=parent_id, parent_document_id=lineage_root_id)
        mock_repo.get_by_id.return_value = parent
        new_version = make_document(parent_document_id=parent_id, version=2)
        mock_repo.create.return_value = new_version
        mock_repo.update.return_value = new_version
        payload = make_create_payload(parent_document_id=parent_id)

        result = await service.upload_document_metadata(payload, actor_id=ACTOR_ID)

        mock_repo.unset_latest_version.assert_awaited_once_with(lineage_root_id)
        mock_repo.update.assert_awaited_once()
        assert result is new_version

    @pytest.mark.asyncio
    async def test_create_document_is_alias_for_upload(self, service, mock_repo):
        mock_repo.find_duplicate.return_value = None
        created = make_document()
        mock_repo.create.return_value = created
        payload = make_create_payload()

        result = await service.create_document(payload, actor_id=ACTOR_ID)

        assert result is created


# --------------------------------------------------------------------------
# get_document() / get_latest_version()
# --------------------------------------------------------------------------
class TestRead:
    @pytest.mark.asyncio
    async def test_get_document_raises_when_missing(self, service, mock_repo):
        mock_repo.get_by_id_or_raise.side_effect = NotFoundException("Document x was not found.")

        with pytest.raises(NotFoundException):
            await service.get_document(uuid.uuid4())

    @pytest.mark.asyncio
    async def test_get_latest_version_raises_when_lineage_empty(self, service, mock_repo):
        mock_repo.get_latest_version.return_value = None

        with pytest.raises(NotFoundException):
            await service.get_latest_version(uuid.uuid4())

    @pytest.mark.asyncio
    async def test_get_latest_version_returns_match(self, service, mock_repo):
        latest = make_document()
        mock_repo.get_latest_version.return_value = latest

        result = await service.get_latest_version(uuid.uuid4())

        assert result is latest


# --------------------------------------------------------------------------
# search_documents()
# --------------------------------------------------------------------------
class TestSearch:
    @pytest.mark.asyncio
    async def test_search_documents_builds_pagination_envelope(self, service, mock_repo):
        docs = [make_document() for _ in range(3)]
        mock_repo.search.return_value = (docs, 25)
        filters = DocumentFilter(page=2, page_size=10)

        response = await service.search_documents(filters)

        assert len(response.items) == 3
        assert [item.id for item in response.items] == [doc.id for doc in docs]
        assert [item.title for item in response.items] == [doc.title for doc in docs]
        assert [item.customer_id for item in response.items] == [doc.customer_id for doc in docs]
        assert [item.version for item in response.items] == [doc.version for doc in docs]
        assert response.total == 25
        assert response.page == 2
        assert response.page_size == 10
        assert response.total_pages == 3

    @pytest.mark.asyncio
    async def test_search_documents_zero_results_has_zero_pages(self, service, mock_repo):
        mock_repo.search.return_value = ([], 0)
        filters = DocumentFilter(page=1, page_size=20)

        response = await service.search_documents(filters)

        assert response.total_pages == 0


# --------------------------------------------------------------------------
# update_document()
# --------------------------------------------------------------------------
class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_raises_when_missing(self, service, mock_repo):
        mock_repo.get_by_id_or_raise.side_effect = NotFoundException("Document x was not found.")

        with pytest.raises(NotFoundException):
            await service.update_document(uuid.uuid4(), DocumentUpdate(title="New"), actor_id=ACTOR_ID)

    @pytest.mark.asyncio
    async def test_update_raises_when_already_deleted(self, service, mock_repo):
        mock_repo.get_by_id_or_raise.return_value = make_document(is_deleted=True)

        with pytest.raises(ConflictException):
            await service.update_document(uuid.uuid4(), DocumentUpdate(title="New"), actor_id=ACTOR_ID)

    @pytest.mark.asyncio
    async def test_update_stamps_updated_by_id(self, service, mock_repo):
        document = make_document()
        mock_repo.get_by_id_or_raise.return_value = document
        mock_repo.update.return_value = document

        await service.update_document(document.id, DocumentUpdate(title="New Title"), actor_id=ACTOR_ID)

        update_data = mock_repo.update.await_args.args[1]
        assert update_data["updated_by_id"] == ACTOR_ID
        assert update_data["title"] == "New Title"

    @pytest.mark.asyncio
    async def test_update_turning_on_verification_fills_defaults(self, service, mock_repo):
        document = make_document()
        mock_repo.get_by_id_or_raise.return_value = document
        mock_repo.update.return_value = document

        await service.update_document(
            document.id, DocumentUpdate(is_verified=True), actor_id=ACTOR_ID
        )

        update_data = mock_repo.update.await_args.args[1]
        assert update_data["verified_by_id"] == ACTOR_ID
        assert update_data["verified_at"] is not None

    @pytest.mark.asyncio
    async def test_update_setting_verified_by_without_flag_raises(self, service, mock_repo):
        document = make_document()
        mock_repo.get_by_id_or_raise.return_value = document

        with pytest.raises(BusinessRuleViolationError):
            await service.update_document(
                document.id,
                DocumentUpdate(verified_by_id=99),
                actor_id=ACTOR_ID,
            )


# --------------------------------------------------------------------------
# delete_document() / bulk_delete_documents()
# --------------------------------------------------------------------------
class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_raises_when_already_deleted(self, service, mock_repo):
        mock_repo.get_by_id_or_raise.return_value = make_document(is_deleted=True)

        with pytest.raises(ConflictException):
            await service.delete_document(uuid.uuid4(), actor_id=ACTOR_ID)

    @pytest.mark.asyncio
    async def test_delete_soft_deletes_active_document(self, service, mock_repo):
        document = make_document()
        mock_repo.get_by_id_or_raise.return_value = document
        deleted = make_document(id=document.id, is_deleted=True)
        mock_repo.soft_delete.return_value = deleted

        result = await service.delete_document(document.id, actor_id=ACTOR_ID)

        mock_repo.soft_delete.assert_awaited_once_with(document, deleted_by_id=ACTOR_ID)
        assert result is deleted

    @pytest.mark.asyncio
    async def test_bulk_delete_requires_at_least_one_id(self, service, mock_repo):
        with pytest.raises(BusinessRuleViolationError):
            await service.bulk_delete_documents([], actor_id=ACTOR_ID)

    @pytest.mark.asyncio
    async def test_bulk_delete_reports_not_found_ids(self, service, mock_repo):
        existing = make_document()
        missing_id = uuid.uuid4()
        mock_repo.get_by_ids.return_value = [existing]
        mock_repo.bulk_soft_delete.return_value = 1

        result = await service.bulk_delete_documents([existing.id, missing_id], actor_id=ACTOR_ID)

        assert result["deleted_count"] == 1
        assert result["requested_count"] == 2
        assert str(missing_id) in result["not_found_ids"]

    @pytest.mark.asyncio
    async def test_bulk_delete_deduplicates_ids(self, service, mock_repo):
        existing = make_document()
        mock_repo.get_by_ids.return_value = [existing]
        mock_repo.bulk_soft_delete.return_value = 1

        result = await service.bulk_delete_documents([existing.id, existing.id], actor_id=ACTOR_ID)

        assert result["requested_count"] == 1


# --------------------------------------------------------------------------
# restore_document()
# --------------------------------------------------------------------------
class TestRestore:
    @pytest.mark.asyncio
    async def test_restore_raises_when_not_deleted(self, service, mock_repo):
        mock_repo.get_by_id_or_raise.return_value = make_document(is_deleted=False)

        with pytest.raises(ConflictException):
            await service.restore_document(uuid.uuid4(), actor_id=ACTOR_ID)

    @pytest.mark.asyncio
    async def test_restore_succeeds_and_stamps_updated_by(self, service, mock_repo):
        document = make_document(is_deleted=True)
        mock_repo.get_by_id_or_raise.return_value = document
        restored = make_document(id=document.id, is_deleted=False)
        mock_repo.restore.return_value = restored
        mock_repo.update.return_value = restored

        result = await service.restore_document(document.id, actor_id=ACTOR_ID)

        mock_repo.restore.assert_awaited_once_with(document)
        update_args = mock_repo.update.await_args.args
        assert update_args[1] == {"updated_by_id": ACTOR_ID}
        assert result is restored


# --------------------------------------------------------------------------
# get_statistics()
# --------------------------------------------------------------------------
class TestStatistics:
    @pytest.mark.asyncio
    async def test_get_statistics_passes_through_scope(self, service, mock_repo):
        mock_repo.get_statistics.return_value = {"total_documents": 5}
        customer_id = uuid.uuid4()

        result = await service.get_statistics(customer_id=customer_id)

        mock_repo.get_statistics.assert_awaited_once_with(
            customer_id=customer_id, property_id=None, booking_id=None, lead_id=None
        )
        assert result == {"total_documents": 5}


# --------------------------------------------------------------------------
# validate_duplicate()
# --------------------------------------------------------------------------
class TestValidateDuplicate:
    @pytest.mark.asyncio
    async def test_validate_duplicate_returns_true_when_clear(self, service, mock_repo):
        mock_repo.find_duplicate.return_value = None

        result = await service.validate_duplicate(checksum_sha256="a" * 64)

        assert result is True

    @pytest.mark.asyncio
    async def test_validate_duplicate_raises_when_conflicting(self, service, mock_repo):
        mock_repo.find_duplicate.return_value = make_document()

        with pytest.raises(DuplicateResourceException):
            await service.validate_duplicate(checksum_sha256="a" * 64)