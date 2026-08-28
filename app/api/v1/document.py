"""
backend/app/api/v1/document.py

FastAPI v1 router for the Document module.

Wires HTTP concerns (JWT authentication, RBAC, request/response
schemas, Swagger metadata, multipart upload/download streaming) onto
`app.services.document_service.DocumentService`. Contains no business
rules -- those live in the service layer -- and no direct database or
storage access outside of the storage/image utilities it orchestrates.

Auth conventions assumed (mirrors the rest of the API surface):
    - `app.api.deps.get_current_user` validates the JWT bearer token
      and resolves the authenticated `app.models.user.User`.
    - `app.api.deps.get_db` yields the request-scoped `AsyncSession`.
    - `app.api.deps.require_roles(*roles)` is a dependency factory
      enforcing RBAC by comparing `current_user.role` against an
      allow-list.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, require_roles
from app.models.document import DocumentCategory, DocumentFileType, DocumentStorageProvider
from app.models.user import User
from app.repositories.document_repository import DocumentRepository
from app.schemas.document import (
    DocumentCreate,
    DocumentFilter,
    DocumentListResponse,
    DocumentResponse,
    DocumentUpdate,
)
from app.services.document_service import DocumentService
from app.utils.file_storage import (
    IMAGE_FILE_TYPES,
    compute_sha256,
    generate_storage_path,
    get_storage_backend,
    infer_extension,
    infer_file_type,
    is_previewable_file_type,
    validate_file_size,
)
from app.utils.image_processing import (
    DEFAULT_PREVIEW_MAX_SIZE,
    DEFAULT_THUMBNAIL_SIZE,
    compress_image,
    generate_thumbnail,
    resize_image,
)

# --------------------------------------------------------------------------
# RBAC Role Groups
# --------------------------------------------------------------------------
ROLE_ADMIN = "ADMIN"
ROLE_MANAGER = "MANAGER"
ROLE_AGENT = "AGENT"
ROLE_VIEWER = "VIEWER"

READ_ROLES = (ROLE_ADMIN, ROLE_MANAGER, ROLE_AGENT, ROLE_VIEWER)
WRITE_ROLES = (ROLE_ADMIN, ROLE_MANAGER, ROLE_AGENT)
DELETE_ROLES = (ROLE_ADMIN, ROLE_MANAGER)
RESTORE_ROLES = (ROLE_ADMIN,)

router = APIRouter(prefix="/documents", tags=["Documents"])


# --------------------------------------------------------------------------
# Transport-Only Request / Response Models
# (not domain schemas -- these exist purely to shape this router's I/O)
# --------------------------------------------------------------------------
class BulkDeleteRequest(BaseModel):
    """Request payload for bulk soft-deleting documents."""

    document_ids: list[uuid.UUID] = Field(
        ..., min_length=1, description="Ids of the documents to soft-delete."
    )


class BulkDeleteResponse(BaseModel):
    """Response payload summarizing a bulk soft-delete operation."""

    deleted_count: int
    requested_count: int
    not_found_ids: list[str]


class DocumentStatisticsResponse(BaseModel):
    """Response payload for the document statistics endpoint."""

    total_documents: int
    active_documents: int
    deleted_documents: int
    verified_documents: int
    unverified_documents: int
    expired_documents: int
    total_storage_bytes: int
    by_category: dict[DocumentCategory, int]
    by_file_type: dict[DocumentFileType, int]
    by_storage_provider: dict[DocumentStorageProvider, int]


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------
async def get_document_service(
    session: AsyncSession = Depends(get_db),
) -> DocumentService:
    """Builds a `DocumentService` wired to a request-scoped repository/session."""
    return DocumentService(DocumentRepository(session))


# --------------------------------------------------------------------------
# Create / Upload
# --------------------------------------------------------------------------
@router.post(
    "/upload",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a new document",
    description=(
        "Uploads a file and registers its metadata. Images are "
        "automatically compressed before being persisted to the "
        "configured storage backend."
    ),
    dependencies=[Depends(require_roles(*WRITE_ROLES))],
)
async def upload_document(
    file: UploadFile = File(..., description="The file content to upload."),
    title: str = Form(..., min_length=1, max_length=255),
    category: DocumentCategory = Form(default=DocumentCategory.OTHER),
    description: Optional[str] = Form(default=None),
    tags: Optional[str] = Form(default=None, description="Optional JSON object of tags."),
    customer_id: Optional[uuid.UUID] = Form(default=None),
    property_id: Optional[int] = Form(default=None),
    booking_id: Optional[uuid.UUID] = Form(default=None),
    lead_id: Optional[uuid.UUID] = Form(default=None),
    parent_document_id: Optional[uuid.UUID] = Form(default=None),
    expiry_date: Optional[date] = Form(default=None),
    storage_provider: DocumentStorageProvider = Form(default=DocumentStorageProvider.LOCAL),
    storage_bucket: Optional[str] = Form(default=None),
    current_user: User = Depends(get_current_user),
    service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    content = await file.read()
    validate_file_size(len(content))

    original_filename = file.filename or "upload.bin"
    file_type = infer_file_type(original_filename)
    extension = infer_extension(original_filename)

    if file_type in IMAGE_FILE_TYPES:
        content = compress_image(content, extension=extension)

    checksum = compute_sha256(content)
    scope_id = str(customer_id or property_id or booking_id or lead_id or "general")
    system_file_name, storage_path = generate_storage_path(
        category=category.value, original_filename=original_filename, scope_id=scope_id
    )

    parsed_tags: Optional[dict] = None
    if tags:
        try:
            parsed_tags = json.loads(tags)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"tags must be a valid JSON object: {exc}",
            ) from exc

    backend = get_storage_backend(storage_provider)
    backend.save(content, storage_path)

    try:
        payload = DocumentCreate(
            customer_id=customer_id,
            property_id=property_id,
            booking_id=booking_id,
            lead_id=lead_id,
            parent_document_id=parent_document_id,
            title=title,
            description=description,
            category=category,
            tags=parsed_tags,
            file_name=system_file_name,
            original_file_name=original_filename,
            file_extension=extension,
            file_type=file_type,
            mime_type=file.content_type,
            file_size_bytes=len(content),
            checksum_sha256=checksum,
            storage_provider=storage_provider,
            storage_bucket=storage_bucket,
            storage_path=storage_path,
            storage_url=backend.get_url(storage_path),
            expiry_date=expiry_date,
            uploaded_by_id=current_user.id,
        )
        document = await service.upload_document_metadata(payload, actor_id=current_user.id)
    except Exception:
        backend.delete(storage_path)
        raise

    return document


# --------------------------------------------------------------------------
# Search / List
# --------------------------------------------------------------------------
@router.get(
    "",
    response_model=DocumentListResponse,
    summary="Search documents",
    description="Filtered, sorted, paginated document search.",
    dependencies=[Depends(require_roles(*READ_ROLES))],
)
async def search_documents(
    filters: DocumentFilter = Depends(),
    service: DocumentService = Depends(get_document_service),
) -> DocumentListResponse:
    return await service.search_documents(filters)


# --------------------------------------------------------------------------
# Statistics (must be declared before "/{document_id}")
# --------------------------------------------------------------------------
@router.get(
    "/statistics",
    response_model=DocumentStatisticsResponse,
    summary="Get document statistics",
    description="Counts, verification rates, and storage usage, optionally scoped to one owning entity.",
    dependencies=[Depends(require_roles(*READ_ROLES))],
)
async def get_document_statistics(
    customer_id: Optional[uuid.UUID] = Query(default=None),
    property_id: Optional[int] = Query(default=None),
    booking_id: Optional[uuid.UUID] = Query(default=None),
    lead_id: Optional[uuid.UUID] = Query(default=None),
    service: DocumentService = Depends(get_document_service),
) -> DocumentStatisticsResponse:
    stats = await service.get_statistics(
        customer_id=customer_id,
        property_id=property_id,
        booking_id=booking_id,
        lead_id=lead_id,
    )
    return DocumentStatisticsResponse(**stats)


# --------------------------------------------------------------------------
# Bulk Delete (must be declared before "/{document_id}")
# --------------------------------------------------------------------------
@router.post(
    "/bulk-delete",
    response_model=BulkDeleteResponse,
    summary="Bulk soft-delete documents",
    dependencies=[Depends(require_roles(*DELETE_ROLES))],
)
async def bulk_delete_documents(
    payload: BulkDeleteRequest,
    current_user: User = Depends(get_current_user),
    service: DocumentService = Depends(get_document_service),
) -> BulkDeleteResponse:
    result = await service.bulk_delete_documents(payload.document_ids, actor_id=current_user.id)
    return BulkDeleteResponse(**result)


# --------------------------------------------------------------------------
# Read (single)
# --------------------------------------------------------------------------
@router.get(
    "/{document_id}",
    response_model=DocumentResponse,
    summary="Get a document's metadata",
    dependencies=[Depends(require_roles(*READ_ROLES))],
)
async def get_document(
    document_id: uuid.UUID,
    service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    return await service.get_document(document_id)


# --------------------------------------------------------------------------
# Update
# --------------------------------------------------------------------------
@router.patch(
    "/{document_id}",
    response_model=DocumentResponse,
    summary="Update a document's metadata",
    dependencies=[Depends(require_roles(*WRITE_ROLES))],
)
async def update_document(
    document_id: uuid.UUID,
    payload: DocumentUpdate,
    current_user: User = Depends(get_current_user),
    service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    return await service.update_document(document_id, payload, actor_id=current_user.id)


# --------------------------------------------------------------------------
# Delete / Restore
# --------------------------------------------------------------------------
@router.delete(
    "/{document_id}",
    response_model=DocumentResponse,
    summary="Soft-delete a document",
    dependencies=[Depends(require_roles(*DELETE_ROLES))],
)
async def delete_document(
    document_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    return await service.delete_document(document_id, actor_id=current_user.id)


@router.post(
    "/{document_id}/restore",
    response_model=DocumentResponse,
    summary="Restore a soft-deleted document",
    dependencies=[Depends(require_roles(*RESTORE_ROLES))],
)
async def restore_document(
    document_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    return await service.restore_document(document_id, actor_id=current_user.id)


# --------------------------------------------------------------------------
# Download / Preview / Thumbnail
# --------------------------------------------------------------------------
@router.get(
    "/{document_id}/download",
    summary="Download a document's original file content",
    response_class=StreamingResponse,
    dependencies=[Depends(require_roles(*READ_ROLES))],
)
async def download_document(
    document_id: uuid.UUID,
    service: DocumentService = Depends(get_document_service),
) -> StreamingResponse:
    document = await service.get_document(document_id)
    backend = get_storage_backend(document.storage_provider)
    content = backend.read(document.storage_path)
    media_type = document.mime_type or "application/octet-stream"
    headers = {"Content-Disposition": f'attachment; filename="{document.original_file_name}"'}
    return StreamingResponse(iter([content]), media_type=media_type, headers=headers)


@router.get(
    "/{document_id}/preview",
    summary="Preview a document inline (images and PDFs)",
    response_class=StreamingResponse,
    dependencies=[Depends(require_roles(*READ_ROLES))],
)
async def preview_document(
    document_id: uuid.UUID,
    service: DocumentService = Depends(get_document_service),
) -> StreamingResponse:
    document = await service.get_document(document_id)
    if not is_previewable_file_type(document.file_type):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Preview is not supported for file type '{document.file_type.value}'.",
        )

    backend = get_storage_backend(document.storage_provider)
    content = backend.read(document.storage_path)

    if document.file_type in IMAGE_FILE_TYPES:
        content = resize_image(
            content,
            max_width=DEFAULT_PREVIEW_MAX_SIZE[0],
            max_height=DEFAULT_PREVIEW_MAX_SIZE[1],
            extension=document.file_extension,
        )
        media_type = document.mime_type or f"image/{document.file_type.value.lower()}"
    else:
        media_type = document.mime_type or "application/pdf"

    return StreamingResponse(
        iter([content]), media_type=media_type, headers={"Content-Disposition": "inline"}
    )


@router.get(
    "/{document_id}/thumbnail",
    summary="Get a small thumbnail for an image document",
    response_class=StreamingResponse,
    dependencies=[Depends(require_roles(*READ_ROLES))],
)
async def get_document_thumbnail(
    document_id: uuid.UUID,
    service: DocumentService = Depends(get_document_service),
) -> StreamingResponse:
    document = await service.get_document(document_id)
    if document.file_type not in IMAGE_FILE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Thumbnails are only available for image documents.",
        )

    backend = get_storage_backend(document.storage_provider)
    content = backend.read(document.storage_path)
    thumbnail = generate_thumbnail(
        content, size=DEFAULT_THUMBNAIL_SIZE, extension=document.file_extension
    )
    media_type = document.mime_type or f"image/{document.file_type.value.lower()}"
    return StreamingResponse(
        iter([thumbnail]), media_type=media_type, headers={"Content-Disposition": "inline"}
    )