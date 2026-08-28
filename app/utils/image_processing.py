"""
backend/app/utils/image_processing.py

Image processing utility for the Document module.

Provides resize, thumbnail generation, compression, and dimension
inspection for raster images (`DocumentFileType.JPG` / `.JPEG` /
`.PNG` / `.GIF`), used by the preview / thumbnail endpoints and by the
upload endpoint's automatic compression step.

Built on Pillow. Contains no HTTP, database, or storage concerns --
every function takes raw bytes in and returns raw bytes (or metadata)
out, and raises only `app.core.exceptions.FileUploadException` on
failure.
"""

from __future__ import annotations

import io
from typing import Optional

from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.exceptions import FileUploadException

DEFAULT_THUMBNAIL_SIZE = (200, 200)
DEFAULT_PREVIEW_MAX_SIZE = (1600, 1600)
DEFAULT_JPEG_QUALITY = 75
DEFAULT_PNG_COMPRESS_LEVEL = 7

# Pillow save() only accepts a handful of format names; map our
# lowercase extensions onto them.
_PIL_FORMAT_BY_EXTENSION = {
    "jpg": "JPEG",
    "jpeg": "JPEG",
    "png": "PNG",
    "gif": "GIF",
}


def _open_image(content: bytes) -> Image.Image:
    """
    Opens raw image bytes as a Pillow `Image`, fully loading pixel
    data so the source buffer can be safely discarded afterward.

    Args:
        content: The raw image bytes.

    Returns:
        A loaded Pillow `Image`.

    Raises:
        FileUploadException: If `content` is not a decodable image.
    """
    try:
        image = Image.open(io.BytesIO(content))
        image.load()
        return image
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise FileUploadException(
            f"Unable to decode image content: {exc}",
            error_code="IMAGE_DECODE_FAILED",
        ) from exc


def _flatten_for_jpeg(image: Image.Image) -> Image.Image:
    """Flattens transparency onto a white background for JPEG encoding, which has no alpha channel."""
    if image.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", image.size, (255, 255, 255))
        rgba = image.convert("RGBA")
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def _encode(image: Image.Image, *, pil_format: str, quality: int) -> bytes:
    """Encodes a Pillow `Image` back to bytes in the given format."""
    buffer = io.BytesIO()
    save_kwargs: dict[str, object] = {}
    if pil_format == "JPEG":
        image = _flatten_for_jpeg(image)
        save_kwargs.update(quality=quality, optimize=True, progressive=True)
    elif pil_format == "PNG":
        save_kwargs.update(optimize=True, compress_level=DEFAULT_PNG_COMPRESS_LEVEL)
    try:
        image.save(buffer, format=pil_format, **save_kwargs)
    except (OSError, ValueError) as exc:
        raise FileUploadException(
            f"Unable to encode image as {pil_format}: {exc}",
            error_code="IMAGE_ENCODE_FAILED",
        ) from exc
    return buffer.getvalue()


def resolve_pil_format(extension: Optional[str], *, fallback: str = "JPEG") -> str:
    """
    Maps a file extension to the Pillow format name to encode with.

    Args:
        extension: The lowercase file extension (without dot), or
            None.
        fallback: The format to use when `extension` is unrecognized.

    Returns:
        A Pillow-compatible format name (e.g. "JPEG", "PNG", "GIF").
    """
    if extension is None:
        return fallback
    return _PIL_FORMAT_BY_EXTENSION.get(extension.lower(), fallback)


def get_image_dimensions(content: bytes) -> tuple[int, int]:
    """
    Reads an image's pixel dimensions without fully decoding it.

    Args:
        content: The raw image bytes.

    Returns:
        A (width, height) tuple, in pixels.

    Raises:
        FileUploadException: If `content` is not a decodable image.
    """
    image = _open_image(content)
    return image.size


def resize_image(
    content: bytes,
    *,
    max_width: int,
    max_height: int,
    extension: Optional[str] = None,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> bytes:
    """
    Resizes an image so it fits within `max_width` x `max_height`,
    preserving aspect ratio. Images already within bounds are
    returned re-encoded but not upscaled.

    Args:
        content: The raw source image bytes.
        max_width: The maximum output width, in pixels.
        max_height: The maximum output height, in pixels.
        extension: The source file's extension, used to pick the
            output encoding; defaults to JPEG if unrecognized.
        quality: JPEG quality (1-95) to use if encoding as JPEG.

    Returns:
        The resized image, encoded as bytes.

    Raises:
        FileUploadException: If the image cannot be decoded or
            re-encoded.
    """
    image = _open_image(content)
    image = ImageOps.exif_transpose(image) or image
    image.thumbnail((max_width, max_height), Image.LANCZOS)
    pil_format = resolve_pil_format(extension)
    return _encode(image, pil_format=pil_format, quality=quality)


def generate_thumbnail(
    content: bytes,
    *,
    size: tuple[int, int] = DEFAULT_THUMBNAIL_SIZE,
    extension: Optional[str] = None,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> bytes:
    """
    Generates a small preview thumbnail for an image, preserving
    aspect ratio within `size`.

    Args:
        content: The raw source image bytes.
        size: The (max_width, max_height) bounding box for the
            thumbnail.
        extension: The source file's extension, used to pick the
            output encoding; defaults to JPEG if unrecognized.
        quality: JPEG quality (1-95) to use if encoding as JPEG.

    Returns:
        The thumbnail image, encoded as bytes.

    Raises:
        FileUploadException: If the image cannot be decoded or
            re-encoded.
    """
    image = _open_image(content)
    image = ImageOps.exif_transpose(image) or image
    image.thumbnail(size, Image.LANCZOS)
    pil_format = resolve_pil_format(extension)
    return _encode(image, pil_format=pil_format, quality=quality)


def compress_image(
    content: bytes,
    *,
    quality: int = DEFAULT_JPEG_QUALITY,
    extension: Optional[str] = None,
    max_dimension: Optional[int] = None,
) -> bytes:
    """
    Re-encodes an image at reduced quality/size to shrink its storage
    footprint, without materially changing its visible dimensions
    unless `max_dimension` is supplied.

    Args:
        content: The raw source image bytes.
        quality: JPEG quality (1-95) to encode at; ignored for PNG
            (PNG compression is lossless and controlled separately).
        extension: The source file's extension, used to pick the
            output encoding; defaults to JPEG if unrecognized.
        max_dimension: If supplied, the image is additionally
            downscaled so neither side exceeds this many pixels.

    Returns:
        The compressed image, encoded as bytes. If the "compressed"
        result is not actually smaller than the input, the original
        bytes are returned instead.

    Raises:
        FileUploadException: If the image cannot be decoded or
            re-encoded.
    """
    image = _open_image(content)
    image = ImageOps.exif_transpose(image) or image
    if max_dimension is not None:
        image.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
    pil_format = resolve_pil_format(extension)
    compressed = _encode(image, pil_format=pil_format, quality=quality)
    return compressed if len(compressed) < len(content) else content