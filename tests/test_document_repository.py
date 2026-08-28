"""
backend/tests/test_document_repository.py

Unit tests for `app.repositories.document_repository.DocumentRepository`.

The repository is exercised against a mocked `AsyncSession` (rather
than a live Postgres instance) so these tests run fast and without
external infrastructure. Each test configures the session mock's
`execute()` return value to mimic the SQLAlchemy `Result` shape the
repository method under test expects, then asserts on the repository's
return value and/or the statement-execution side effects.

Run with:
    pytest backend/tests/test_document_repository.py -v
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import NotFoundException
from app.models.document import (
    Document,
    DocumentCategory,
    DocumentFileType,
    DocumentStorageProvider,
)
from app.repositories.document_repository import DocumentRepository
from app.schemas.document import DocumentFilter


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture
def repository(mock_session: AsyncMock) -> DocumentRepository:
    return DocumentRepository(mock_session)


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


def result_with_scalar_one_or_none(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def result_with_scalars_all(values):
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = values
    result.scalars.return_value = scalars
    return result


def result_with_scalar_one(value):
    result = MagicMock()
    result.scalar_one.return_value = value
    return result


# --------------------------------------------------------------------------
# create()
# --------------------------------------------------------------------------
class TestCreate:
    @pytest.mark.asyncio
    async def test_create_adds_flushes_and_refreshes(self, repository, mock_session):
        data = {
            "title": "Aadhaar Card",
            "file_name": "abc.pdf",
            "original_file_name": "aadhaar.pdf",
            "file_type": DocumentFileType.PDF,
            "file_size_bytes": 1024,
            "storage_path": "kyc/abc.pdf",
            "uploaded_by_id": 5,
            "created_by_id": 5,
        }

        document = await repository.create(data)

        mock_session.add.assert_called_once()
        mock_session.flush.assert_awaited_once()
        mock_session.refresh.assert_awaited_once()
        assert isinstance(document, Document)
        assert document.title == "Aadhaar Card"


# --------------------------------------------------------------------------
# get_by_id() / get_by_id_or_raise()
# --------------------------------------------------------------------------
class TestGetById:
    @pytest.mark.asyncio
    async def test_get_by_id_returns_document_when_found(self, repository, mock_session):
        expected = make_document()
        mock_session.execute.return_value = result_with_scalar_one_or_none(expected)

        result = await repository.get_by_id(expected.id)

        assert result is expected

    @pytest.mark.asyncio
    async def test_get_by_id_returns_none_when_not_found(self, repository, mock_session):
        mock_session.execute.return_value = result_with_scalar_one_or_none(None)

        result = await repository.get_by_id(uuid.uuid4())

        assert result is None

    @pytest.mark.asyncio
    async def test_get_by_id_or_raise_raises_when_missing(self, repository, mock_session):
        mock_session.execute.return_value = result_with_scalar_one_or_none(None)
        missing_id = uuid.uuid4()

        with pytest.raises(NotFoundException):
            await repository.get_by_id_or_raise(missing_id)

    @pytest.mark.asyncio
    async def test_get_by_id_or_raise_returns_document_when_found(self, repository, mock_session):
        expected = make_document()
        mock_session.execute.return_value = result_with_scalar_one_or_none(expected)

        result = await repository.get_by_id_or_raise(expected.id)

        assert result is expected


# --------------------------------------------------------------------------
# get_by_ids()
# --------------------------------------------------------------------------
class TestGetByIds:
    @pytest.mark.asyncio
    async def test_get_by_ids_empty_input_short_circuits(self, repository, mock_session):
        result = await repository.get_by_ids([])

        assert result == []
        mock_session.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_by_ids_returns_matching_rows(self, repository, mock_session):
        docs = [make_document(), make_document()]
        mock_session.execute.return_value = result_with_scalars_all(docs)

        result = await repository.get_by_ids([d.id for d in docs])

        assert result == docs


# --------------------------------------------------------------------------
# find_duplicate()
# --------------------------------------------------------------------------
class TestFindDuplicate:
    @pytest.mark.asyncio
    async def test_find_duplicate_returns_match(self, repository, mock_session):
        existing = make_document()
        mock_session.execute.return_value = result_with_scalar_one_or_none(existing)

        result = await repository.find_duplicate(
            checksum_sha256="a" * 64,
            customer_id=existing.customer_id,
            property_id=None,
            booking_id=None,
            lead_id=None,
        )

        assert result is existing

    @pytest.mark.asyncio
    async def test_find_duplicate_returns_none_when_no_match(self, repository, mock_session):
        mock_session.execute.return_value = result_with_scalar_one_or_none(None)

        result = await repository.find_duplicate(
            checksum_sha256="b" * 64,
            customer_id=None,
            property_id=None,
            booking_id=None,
            lead_id=None,
        )

        assert result is None


# --------------------------------------------------------------------------
# update()
# --------------------------------------------------------------------------
class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_applies_fields_and_persists(self, repository, mock_session):
        document = make_document()

        updated = await repository.update(document, {"title": "New Title"})

        assert updated.title == "New Title"
        mock_session.flush.assert_awaited_once()
        mock_session.refresh.assert_awaited_once()


# --------------------------------------------------------------------------
# unset_latest_version()
# --------------------------------------------------------------------------
class TestUnsetLatestVersion:
    @pytest.mark.asyncio
    async def test_unset_latest_version_executes_and_flushes(self, repository, mock_session):
        mock_session.execute.return_value = MagicMock()

        await repository.unset_latest_version(uuid.uuid4())

        mock_session.execute.assert_awaited_once()
        mock_session.flush.assert_awaited_once()


# --------------------------------------------------------------------------
# soft_delete() / bulk_soft_delete() / restore()
# --------------------------------------------------------------------------
class TestSoftDeleteAndRestore:
    @pytest.mark.asyncio
    async def test_soft_delete_sets_flags(self, repository, mock_session):
        document = make_document()

        result = await repository.soft_delete(document, deleted_by_id=9)

        assert result.is_deleted is True
        assert result.deleted_by_id == 9
        assert result.deleted_at is not None

    @pytest.mark.asyncio
    async def test_bulk_soft_delete_empty_input_short_circuits(self, repository, mock_session):
        count = await repository.bulk_soft_delete([], deleted_by_id=9)

        assert count == 0
        mock_session.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bulk_soft_delete_returns_rowcount(self, repository, mock_session):
        exec_result = MagicMock()
        exec_result.rowcount = 3
        mock_session.execute.return_value = exec_result

        count = await repository.bulk_soft_delete([uuid.uuid4(), uuid.uuid4(), uuid.uuid4()], deleted_by_id=9)

        assert count == 3

    @pytest.mark.asyncio
    async def test_restore_clears_flags(self, repository, mock_session):
        document = make_document(
            is_deleted=True, deleted_at=datetime.now(timezone.utc), deleted_by_id=9
        )

        result = await repository.restore(document)

        assert result.is_deleted is False
        assert result.deleted_at is None
        assert result.deleted_by_id is None


# --------------------------------------------------------------------------
# search()
# --------------------------------------------------------------------------
class TestSearch:
    @pytest.mark.asyncio
    async def test_search_returns_items_and_total(self, repository, mock_session):
        docs = [make_document(), make_document()]
        mock_session.execute.side_effect = [
            result_with_scalar_one(2),
            result_with_scalars_all(docs),
        ]
        filters = DocumentFilter(page=1, page_size=20)

        items, total = await repository.search(filters)

        assert items == docs
        assert total == 2

    @pytest.mark.asyncio
    async def test_search_applies_pagination_math(self, repository, mock_session):
        mock_session.execute.side_effect = [
            result_with_scalar_one(0),
            result_with_scalars_all([]),
        ]
        filters = DocumentFilter(page=2, page_size=10, search="aadhaar")

        items, total = await repository.search(filters)

        assert items == []
        assert total == 0


# --------------------------------------------------------------------------
# get_statistics()
# --------------------------------------------------------------------------
class TestGetStatistics:
    @pytest.mark.asyncio
    async def test_get_statistics_assembles_expected_shape(self, repository, mock_session):
        mock_session.execute.side_effect = [
            result_with_scalar_one(10),  # total
            result_with_scalar_one(2),  # deleted
            result_with_scalar_one(6),  # verified
            result_with_scalar_one(9),  # active
            result_with_scalar_one(1),  # expired
            result_with_scalar_one(2048),  # storage bytes
            MagicMock(all=MagicMock(return_value=[(DocumentCategory.KYC, 5)])),
            MagicMock(all=MagicMock(return_value=[(DocumentFileType.PDF, 5)])),
            MagicMock(all=MagicMock(return_value=[(DocumentStorageProvider.LOCAL, 10)])),
        ]

        stats = await repository.get_statistics()

        assert stats["total_documents"] == 10
        assert stats["deleted_documents"] == 2
        assert stats["verified_documents"] == 6
        assert stats["unverified_documents"] == 4
        assert stats["active_documents"] == 9
        assert stats["expired_documents"] == 1
        assert stats["total_storage_bytes"] == 2048
        assert stats["by_category"] == {DocumentCategory.KYC: 5}
        assert stats["by_file_type"] == {DocumentFileType.PDF: 5}
        assert stats["by_storage_provider"] == {DocumentStorageProvider.LOCAL: 10}


# --------------------------------------------------------------------------
# exists()
# --------------------------------------------------------------------------
class TestExists:
    @pytest.mark.asyncio
    async def test_exists_true_when_count_positive(self, repository, mock_session):
        mock_session.execute.return_value = result_with_scalar_one(1)

        assert await repository.exists(uuid.uuid4()) is True

    @pytest.mark.asyncio
    async def test_exists_false_when_count_zero(self, repository, mock_session):
        mock_session.execute.return_value = result_with_scalar_one(0)

        assert await repository.exists(uuid.uuid4()) is False