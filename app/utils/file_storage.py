"""
backend/app/utils/file_storage.py

Low-level file storage utility for the Document module.

Provides a storage-provider abstraction (`StorageBackend`) plus a
`LocalFileStorage` implementation, and the pure helper functions
(path generation, checksum, filename sanitization, file-type
inference) that the document API and service layers rely on to turn
an uploaded file into a `Document` row.

This module knows nothing about HTTP, the database, or business
rules -- it only reads/writes bytes to a configured storage location
and raises `app.core.exceptions.DatabaseException` /
`app.core.exceptions.FileUploadException` on failure.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from app.core.config import settings
from app.core.exceptions import DatabaseException, FileUploadException
from app.models.document import DocumentFileType, DocumentStorageProvider

# --------------------------------------------------------------------------
# File-Type Inference
# --------------------------------------------------------------------------
_EXTENSION_TO_FILE_TYPE: dict[str, DocumentFileType] = {
    "pdf": DocumentFileType.PDF,
    "doc": DocumentFileType.DOC,
    "docx": DocumentFileType.DOCX,
    "xls": DocumentFileType.XLS,
    "xlsx": DocumentFileType.XLSX,
    "ppt": DocumentFileType.PPT,
    "pptx": DocumentFileType.PPTX,
    "jpg": DocumentFileType.JPG,
    "jpeg": DocumentFileType.JPEG,
    "png": DocumentFileType.PNG,
    "gif": DocumentFileType.GIF,
    "txt": DocumentFileType.TXT,
    "csv": DocumentFileType.CSV,
    "zip": DocumentFileType.ZIP,
}

IMAGE_FILE_TYPES = frozenset(
    {
        DocumentFileType.JPG,
        DocumentFileType.JPEG,
        DocumentFileType.PNG,
        DocumentFileType.GIF,
    }
)

PREVIEWABLE_FILE_TYPES = IMAGE_FILE_TYPES | {DocumentFileType.PDF}

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")

# Default 25 MiB ceiling; overridable via `settings.MAX_UPLOAD_SIZE_BYTES`.
_DEFAULT_MAX_UPLOAD_SIZE_BYTES = 25 * 1024 * 1024


def infer_file_type(filename: str) -> DocumentFileType:
    """
    Infers a `DocumentFileType` from a file name's extension.

    Args:
        filename: The original file name (e.g. "aadhaar_card.pdf").

    Returns:
        The matching `DocumentFileType`, or `DocumentFileType.OTHER`
        if the extension is unrecognized.
    """
    ext = Path(filename).suffix.lstrip(".").lower()
    return _EXTENSION_TO_FILE_TYPE.get(ext, DocumentFileType.OTHER)


def infer_extension(filename: str) -> Optional[str]:
    """
    Extracts the lowercase extension (without the leading dot) from a
    file name.

    Args:
        filename: The original file name.

    Returns:
        The lowercase extension, or None if the file name has none.
    """
    ext = Path(filename).suffix.lstrip(".").lower()
    return ext or None


def is_image_file_type(file_type: DocumentFileType) -> bool:
    """Returns True if `file_type` is one of the supported raster image types."""
    return file_type in IMAGE_FILE_TYPES


def is_previewable_file_type(file_type: DocumentFileType) -> bool:
    """Returns True if `file_type` can be rendered inline by a browser."""
    return file_type in PREVIEWABLE_FILE_TYPES


# --------------------------------------------------------------------------
# Naming / Hashing
# --------------------------------------------------------------------------
def sanitize_filename(filename: str) -> str:
    """
    Strips any directory components and replaces characters outside
    `[A-Za-z0-9_.-]` with underscores, to produce a filesystem- and
    URL-safe file name.

    Args:
        filename: The raw, possibly attacker-controlled file name.

    Returns:
        A sanitized file name; falls back to "file" if the input
        sanitizes to an empty string.
    """
    base = Path(filename).name
    cleaned = _FILENAME_SAFE_RE.sub("_", base)
    return cleaned or "file"


def compute_sha256(content: bytes) -> str:
    """
    Computes the SHA-256 digest of file content, for integrity
    checking and duplicate-detection purposes.

    Args:
        content: The raw file bytes.

    Returns:
        The lowercase hex-encoded SHA-256 digest.
    """
    return hashlib.sha256(content).hexdigest()


def validate_file_size(size_bytes: int) -> None:
    """
    Validates that a file's size does not exceed the configured
    maximum upload size.

    Args:
        size_bytes: The size of the file content, in bytes.

    Raises:
        FileUploadException: If `size_bytes` exceeds the configured
            limit.
    """
    max_size = getattr(settings, "MAX_UPLOAD_SIZE_BYTES", _DEFAULT_MAX_UPLOAD_SIZE_BYTES)
    if size_bytes > max_size:
        raise FileUploadException(
            f"File size {size_bytes} bytes exceeds the maximum allowed size of {max_size} bytes.",
            details={"size_bytes": size_bytes, "max_size_bytes": max_size},
        )


def generate_storage_path(
    *,
    category: str,
    original_filename: str,
    scope_id: Optional[str] = None,
) -> tuple[str, str]:
    """
    Generates a collision-resistant storage key for a newly uploaded
    file.

    Args:
        category: The document's business category (used as a
            top-level folder for organization).
        original_filename: The file name as supplied by the uploader.
        scope_id: An optional owning-entity id (customer/property/
            booking/lead) used as a second-level folder; defaults to
            "unscoped" when not supplied.

    Returns:
        A tuple of (system-generated file name, full storage path).
    """
    safe_name = sanitize_filename(original_filename)
    system_file_name = f"{uuid.uuid4().hex}_{safe_name}"
    scope_segment = scope_id or "unscoped"
    storage_path = f"{category.lower()}/{scope_segment}/{system_file_name}"
    return system_file_name, storage_path


# --------------------------------------------------------------------------
# Storage Backend Abstraction
# --------------------------------------------------------------------------
class StorageBackend(ABC):
    """Common interface every storage provider implementation must satisfy."""

    @abstractmethod
    def save(self, content: bytes, storage_path: str) -> str:
        """Persists `content` at `storage_path`; returns the stored path."""

    @abstractmethod
    def read(self, storage_path: str) -> bytes:
        """Reads and returns the raw bytes stored at `storage_path`."""

    @abstractmethod
    def delete(self, storage_path: str) -> bool:
        """Deletes the object at `storage_path`; returns True if it existed."""

    @abstractmethod
    def exists(self, storage_path: str) -> bool:
        """Returns True if an object exists at `storage_path`."""

    def get_url(self, storage_path: str) -> Optional[str]:
        """Returns a public/servable URL for `storage_path`, if applicable."""
        return None


class LocalFileStorage(StorageBackend):
    """Filesystem-backed `StorageBackend`, rooted at a configured directory."""

    def __init__(self, base_dir: Optional[str] = None) -> None:
        root = base_dir or getattr(settings, "UPLOAD_ROOT_DIR", "./uploads")
        self._base_dir = Path(root).resolve()
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _resolve(self, storage_path: str) -> Path:
        """Resolves `storage_path` beneath the storage root, rejecting escapes."""
        resolved = (self._base_dir / storage_path).resolve()
        if resolved != self._base_dir and self._base_dir not in resolved.parents:
            raise DatabaseException(
                f"Resolved path escapes storage root: {storage_path!r}",
                error_code="STORAGE_PATH_ESCAPE",
            )
        return resolved

    def save(self, content: bytes, storage_path: str) -> str:
        try:
            target = self._resolve(storage_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        except OSError as exc:
            raise DatabaseException(
                f"Failed to write file at {storage_path!r}: {exc}",
                error_code="STORAGE_WRITE_FAILED",
            ) from exc
        return storage_path

    def read(self, storage_path: str) -> bytes:
        target = self._resolve(storage_path)
        if not target.is_file():
            raise DatabaseException(
                f"File not found at {storage_path!r}",
                error_code="STORAGE_FILE_NOT_FOUND",
            )
        try:
            return target.read_bytes()
        except OSError as exc:
            raise DatabaseException(
                f"Failed to read file at {storage_path!r}: {exc}",
                error_code="STORAGE_READ_FAILED",
            ) from exc

    def delete(self, storage_path: str) -> bool:
        target = self._resolve(storage_path)
        if not target.is_file():
            return False
        try:
            target.unlink()
            return True
        except OSError as exc:
            raise DatabaseException(
                f"Failed to delete file at {storage_path!r}: {exc}",
                error_code="STORAGE_DELETE_FAILED",
            ) from exc

    def exists(self, storage_path: str) -> bool:
        try:
            return self._resolve(storage_path).is_file()
        except DatabaseException:
            return False

    def get_url(self, storage_path: str) -> Optional[str]:
        base_url = getattr(settings, "UPLOAD_PUBLIC_BASE_URL", None)
        if not base_url:
            return None
        return f"{str(base_url).rstrip('/')}/{storage_path}"


# --------------------------------------------------------------------------
# Backend Factory
# --------------------------------------------------------------------------
_backend_registry: dict[DocumentStorageProvider, StorageBackend] = {}


def get_storage_backend(provider: DocumentStorageProvider) -> StorageBackend:
    """
    Resolves (and memoizes) the `StorageBackend` implementation for a
    given `DocumentStorageProvider`.

    Args:
        provider: The storage provider requested by the document row.

    Returns:
        A `StorageBackend` instance.

    Raises:
        DatabaseException: If the requested provider has no configured
            implementation.
    """
    if provider not in _backend_registry:
        if provider == DocumentStorageProvider.LOCAL:
            _backend_registry[provider] = LocalFileStorage()
        else:
            raise DatabaseException(
                f"Storage provider '{provider.value}' is not configured.",
                error_code="STORAGE_PROVIDER_NOT_CONFIGURED",
            )
    return _backend_registry[provider]