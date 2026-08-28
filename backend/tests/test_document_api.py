"""
backend/tests/test_document_api.py

Integration-style tests for `app.api.v1.document.router`.

Scope and approach:
    - JWT validation and RBAC enforcement live in `app.api.deps`
      (`get_current_user`, `require_roles`), which is outside this
      module's scope. Rather than guess at their internals, these
      tests build a standalone FastAPI app containing only the
      Document router and override every dependency the router
      declares -- including each per-route `require_roles(...)`
      instance, discovered dynamically off `route.dependencies` -- so
      routing, request parsing, and response shaping are verified
      independently of the real auth implementation.
    - `DocumentService` is replaced with a fully mocked double via a
      dependency override on `get_document_service`, so no database is
      required.
    - `get_storage_backend` (imported into the router module) is
      monkeypatched to an in-memory fake, so upload / download /
      preview / thumbnail tests don't touch the filesystem.
    - A minimal domain-exception -> HTTP-status exception handler is
      registered on the *test* app only, to approximate what the real
      application's global handler is expected to do. This is a test
      fixture concern, not part of the Document module itself.

Run with:
    pytest backend/tests/test_document_api.py -v
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError as PydanticValidationError

from app.api.v1 import document as document_module
from app.core.exceptions import (
    BusinessRuleViolationError,
    DocumentAlreadyDeletedError,
    DocumentNotDeletedError,
    DocumentNotFoundError,
    DuplicateDocumentError,
)
from app.models.document import Document, DocumentCategory, DocumentFileType, DocumentStorageProvider
from app.schemas.document import DocumentListResponse


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
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


def make_image_document(**overrides) -> Document:
    overrides.setdefault("file_type", DocumentFileType.PNG)
    overrides.setdefault("file_extension", "png")
    overrides.setdefault("mime_type", "image/png")
    overrides.setdefault("original_file_name", "photo.png")
    return make_document(**overrides)


def png_bytes(size: tuple[int, int] = (64, 64)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color=(120, 200, 80)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeStorageBackend:
    """In-memory stand-in for `app.utils.file_storage.StorageBackend`."""

    def __init__(self, content_by_path: dict[str, bytes] | None = None) -> None:
        self.saved: dict[str, bytes] = dict(content_by_path or {})
        self.deleted: list[str] = []

    def save(self, content: bytes, storage_path: str) -> str:
        self.saved[storage_path] = content
        return storage_path

    def read(self, storage_path: str) -> bytes:
        return self.saved[storage_path]

    def delete(self, storage_path: str) -> bool:
        self.deleted.append(storage_path)
        return self.saved.pop(storage_path, None) is not None

    def exists(self, storage_path: str) -> bool:
        return storage_path in self.saved

    def get_url(self, storage_path: str) -> str | None:
        return None


_EXCEPTION_STATUS_MAP: dict[type[Exception], int] = {
    DocumentNotFoundError: 404,
    DuplicateDocumentError: 409,
    DocumentAlreadyDeletedError: 409,
    DocumentNotDeletedError: 409,
    BusinessRuleViolationError: 422,
}


@pytest.fixture
def fake_service() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def fake_backend() -> FakeStorageBackend:
    return FakeStorageBackend()


@pytest.fixture
def client(fake_service: AsyncMock, fake_backend: FakeStorageBackend, monkeypatch) -> TestClient:
    app = FastAPI()
    app.include_router(document_module.router)

    # Approximate the project's global domain-exception handler for
    # this isolated test app only.
    for exc_type, status_code in _EXCEPTION_STATUS_MAP.items():

        def _make_handler(code: int):
            async def _handler(request: Request, exc: Exception) -> JSONResponse:
                return JSONResponse(status_code=code, content={"detail": str(exc)})

            return _handler

        app.add_exception_handler(exc_type, _make_handler(status_code))

    async def _pydantic_validation_handler(
        request: Request, exc: PydanticValidationError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    app.add_exception_handler(PydanticValidationError, _pydantic_validation_handler)

    # Override the service dependency.
    app.dependency_overrides[document_module.get_document_service] = lambda: fake_service

    # Override auth/current-user.
    fake_user = type("FakeUser", (), {"id": 42, "role": "ADMIN"})()
    app.dependency_overrides[document_module.get_current_user] = lambda: fake_user

    # Override every RBAC dependency declared per-route, without
    # assuming anything about `require_roles`'s internals.
    for route in document_module.router.routes:
        for dep in getattr(route, "dependencies", []):
            app.dependency_overrides[dep.dependency] = lambda: None

    # Avoid real filesystem access for storage.
    monkeypatch.setattr(document_module, "get_storage_backend", lambda provider: fake_backend)

    return TestClient(app)


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------
class TestUploadEndpoint:
    def test_upload_success_returns_201(self, client, fake_service, fake_backend):
        created = make_document()
        fake_service.upload_document_metadata.return_value = created

        response = client.post(
            "/documents/upload",
            data={"title": "Aadhaar Card", "category": "KYC"},
            files={"file": ("aadhaar.pdf", b"%PDF-1.4 fake content", "application/pdf")},
        )

        assert response.status_code == 201
        assert response.json()["id"] == str(created.id)
        assert fake_service.upload_document_metadata.await_count == 1

    def test_upload_invalid_tags_json_returns_422(self, client, fake_service):
        response = client.post(
            "/documents/upload",
            data={"title": "Aadhaar Card", "tags": "{not-json}"},
            files={"file": ("aadhaar.pdf", b"content", "application/pdf")},
        )

        assert response.status_code == 422
        fake_service.upload_document_metadata.assert_not_awaited()

    def test_upload_duplicate_checksum_returns_409_and_cleans_up_storage(
        self, client, fake_service, fake_backend
    ):
        fake_service.upload_document_metadata.side_effect = DuplicateDocumentError(
            f"A document with checksum {'a' * 64} already exists "
            f"(document id: {uuid.uuid4()})."
        )

        response = client.post(
            "/documents/upload",
            data={"title": "Aadhaar Card"},
            files={"file": ("aadhaar.pdf", b"content", "application/pdf")},
        )

        assert response.status_code == 409
        assert len(fake_backend.deleted) == 1

    def test_upload_missing_file_returns_422(self, client):
        response = client.post("/documents/upload", data={"title": "Aadhaar Card"})

        assert response.status_code == 422


# --------------------------------------------------------------------------
# Search / List
# --------------------------------------------------------------------------
class TestSearchEndpoint:
    def test_search_returns_paginated_list(self, client, fake_service):
        docs = [make_document(), make_document()]
        fake_service.search_documents.return_value = DocumentListResponse(
            items=docs, total=2, page=1, page_size=20, total_pages=1
        )

        response = client.get("/documents", params={"page": 1, "page_size": 20})

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        assert len(body["items"]) == 2

    def test_search_rejects_invalid_sort_by(self, client, fake_service):
        response = client.get("/documents", params={"sort_by": "not_a_real_column"})

        assert response.status_code == 422
        fake_service.search_documents.assert_not_awaited()


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------
class TestStatisticsEndpoint:
    def test_statistics_returns_expected_shape(self, client, fake_service):
        fake_service.get_statistics.return_value = {
            "total_documents": 10,
            "active_documents": 9,
            "deleted_documents": 1,
            "verified_documents": 4,
            "unverified_documents": 6,
            "expired_documents": 0,
            "total_storage_bytes": 4096,
            "by_category": {DocumentCategory.KYC: 10},
            "by_file_type": {DocumentFileType.PDF: 10},
            "by_storage_provider": {DocumentStorageProvider.LOCAL: 10},
        }

        response = client.get("/documents/statistics")

        assert response.status_code == 200
        assert response.json()["total_documents"] == 10


# --------------------------------------------------------------------------
# Get / Update / Delete / Restore
# --------------------------------------------------------------------------
class TestSingleDocumentLifecycle:
    def test_get_document_not_found_returns_404(self, client, fake_service):
        fake_service.get_document.side_effect = DocumentNotFoundError("Document x was not found.")

        response = client.get(f"/documents/{uuid.uuid4()}")

        assert response.status_code == 404

    def test_get_document_found_returns_200(self, client, fake_service):
        doc = make_document()
        fake_service.get_document.return_value = doc

        response = client.get(f"/documents/{doc.id}")

        assert response.status_code == 200
        assert response.json()["title"] == doc.title

    def test_update_document_returns_200(self, client, fake_service):
        doc = make_document(title="Updated Title")
        fake_service.update_document.return_value = doc

        response = client.patch(f"/documents/{doc.id}", json={"title": "Updated Title"})

        assert response.status_code == 200
        assert response.json()["title"] == "Updated Title"

    def test_update_document_business_rule_violation_returns_422(self, client, fake_service):
        fake_service.update_document.side_effect = BusinessRuleViolationError("bad state")

        response = client.patch(f"/documents/{uuid.uuid4()}", json={"is_verified": False, "verified_by_id": 1})

        assert response.status_code == 422

    def test_delete_document_already_deleted_returns_409(self, client, fake_service):
        fake_service.delete_document.side_effect = DocumentAlreadyDeletedError(
            "Document x is already deleted."
        )

        response = client.delete(f"/documents/{uuid.uuid4()}")

        assert response.status_code == 409

    def test_delete_document_returns_200(self, client, fake_service):
        deleted = make_document(is_deleted=True)
        fake_service.delete_document.return_value = deleted

        response = client.delete(f"/documents/{deleted.id}")

        assert response.status_code == 200
        assert response.json()["is_deleted"] is True

    def test_restore_document_not_deleted_returns_409(self, client, fake_service):
        fake_service.restore_document.side_effect = DocumentNotDeletedError(
            "Document x is not deleted."
        )

        response = client.post(f"/documents/{uuid.uuid4()}/restore")

        assert response.status_code == 409

    def test_restore_document_returns_200(self, client, fake_service):
        restored = make_document(is_deleted=False)
        fake_service.restore_document.return_value = restored

        response = client.post(f"/documents/{restored.id}/restore")

        assert response.status_code == 200
        assert response.json()["is_deleted"] is False


# --------------------------------------------------------------------------
# Bulk Delete
# --------------------------------------------------------------------------
class TestBulkDeleteEndpoint:
    def test_bulk_delete_returns_summary(self, client, fake_service):
        ids = [uuid.uuid4(), uuid.uuid4()]
        fake_service.bulk_delete_documents.return_value = {
            "deleted_count": 2,
            "requested_count": 2,
            "not_found_ids": [],
        }

        response = client.post(
            "/documents/bulk-delete", json={"document_ids": [str(i) for i in ids]}
        )

        assert response.status_code == 200
        assert response.json()["deleted_count"] == 2

    def test_bulk_delete_requires_at_least_one_id(self, client, fake_service):
        response = client.post("/documents/bulk-delete", json={"document_ids": []})

        assert response.status_code == 422
        fake_service.bulk_delete_documents.assert_not_awaited()


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------
class TestDownloadEndpoint:
    def test_download_streams_original_content(self, client, fake_service, fake_backend):
        doc = make_document()
        fake_backend.saved[doc.storage_path] = b"%PDF-1.4 original bytes"
        fake_service.get_document.return_value = doc

        response = client.get(f"/documents/{doc.id}/download")

        assert response.status_code == 200
        assert response.content == b"%PDF-1.4 original bytes"
        assert "attachment" in response.headers["content-disposition"]
        assert doc.original_file_name in response.headers["content-disposition"]


# --------------------------------------------------------------------------
# Preview
# --------------------------------------------------------------------------
class TestPreviewEndpoint:
    def test_preview_unsupported_type_returns_415(self, client, fake_service):
        doc = make_document(file_type=DocumentFileType.ZIP, file_extension="zip")
        fake_service.get_document.return_value = doc

        response = client.get(f"/documents/{doc.id}/preview")

        assert response.status_code == 415

    def test_preview_image_returns_resized_content(self, client, fake_service, fake_backend):
        doc = make_image_document()
        fake_backend.saved[doc.storage_path] = png_bytes((3000, 3000))
        fake_service.get_document.return_value = doc

        response = client.get(f"/documents/{doc.id}/preview")

        assert response.status_code == 200
        preview_image = Image.open(io.BytesIO(response.content))
        assert max(preview_image.size) <= 1600

    def test_preview_pdf_returns_inline_content(self, client, fake_service, fake_backend):
        doc = make_document()
        fake_backend.saved[doc.storage_path] = b"%PDF-1.4 content"
        fake_service.get_document.return_value = doc

        response = client.get(f"/documents/{doc.id}/preview")

        assert response.status_code == 200
        assert "inline" in response.headers["content-disposition"]


# --------------------------------------------------------------------------
# Thumbnail
# --------------------------------------------------------------------------
class TestThumbnailEndpoint:
    def test_thumbnail_non_image_returns_415(self, client, fake_service):
        doc = make_document()  # PDF
        fake_service.get_document.return_value = doc

        response = client.get(f"/documents/{doc.id}/thumbnail")

        assert response.status_code == 415

    def test_thumbnail_image_returns_small_image(self, client, fake_service, fake_backend):
        doc = make_image_document()
        fake_backend.saved[doc.storage_path] = png_bytes((3000, 3000))
        fake_service.get_document.return_value = doc

        response = client.get(f"/documents/{doc.id}/thumbnail")

        assert response.status_code == 200
        thumb = Image.open(io.BytesIO(response.content))
        assert max(thumb.size) <= 200