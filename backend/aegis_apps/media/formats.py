"""Media formats, derivative shapes and content types (spec M4, M6).

The catalog's ``type_hint`` is the lowercase ASCII extension of a file name, so every
mapping here is keyed by a lowercase extension. Content types come from these maps
only; files are never sniffed.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from .enums import DerivativeExtension, DerivativeKind, MediaKind

PHOTO_EXTENSIONS: Final = frozenset(
    {"jpg", "jpeg", "png", "webp", "gif", "heic", "heif", "avif", "bmp", "tif", "tiff"}
)
VIDEO_EXTENSIONS: Final = frozenset(
    {"mp4", "m4v", "mov", "webm", "mkv", "avi", "3gp", "mts", "m2ts"}
)
MEDIA_KIND_BY_EXTENSION: Final[Mapping[str, MediaKind]] = MappingProxyType(
    {
        **dict.fromkeys(sorted(PHOTO_EXTENSIONS), MediaKind.PHOTO),
        **dict.fromkeys(sorted(VIDEO_EXTENSIONS), MediaKind.VIDEO),
    }
)
MEDIA_EXTENSIONS: Final = tuple(sorted(MEDIA_KIND_BY_EXTENSION))

# Browser-native originals that may be served inline (with PREVIEW). Attachments of
# these types (with EXPORT) use the same content type; every other attachment is
# application/octet-stream.
INLINE_ORIGINAL_CONTENT_TYPES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "gif": "image/gif",
        "avif": "image/avif",
        "mp4": "video/mp4",
        "m4v": "video/mp4",
        "webm": "video/webm",
        "mov": "video/quicktime",
    }
)
ATTACHMENT_FALLBACK_CONTENT_TYPE: Final = "application/octet-stream"

DERIVATIVE_CONTENT_TYPES: Final[Mapping[str, str]] = MappingProxyType(
    {
        DerivativeExtension.WEBP.value: "image/webp",
        DerivativeExtension.MP4.value: "video/mp4",
    }
)
DERIVATIVE_EXTENSION_BY_KIND: Final[Mapping[str, DerivativeExtension]] = MappingProxyType(
    {
        DerivativeKind.THUMBNAIL.value: DerivativeExtension.WEBP,
        DerivativeKind.PREVIEW.value: DerivativeExtension.WEBP,
        DerivativeKind.POSTER.value: DerivativeExtension.WEBP,
        DerivativeKind.TRANSCODE.value: DerivativeExtension.MP4,
    }
)
DERIVATIVE_KINDS_BY_MEDIA_KIND: Final[Mapping[str, tuple[DerivativeKind, ...]]] = (
    MappingProxyType(
        {
            MediaKind.PHOTO.value: (DerivativeKind.THUMBNAIL, DerivativeKind.PREVIEW),
            MediaKind.VIDEO.value: (
                DerivativeKind.THUMBNAIL,
                DerivativeKind.POSTER,
                DerivativeKind.TRANSCODE,
            ),
        }
    )
)
# Thumbnails and posters are kept; previews and transcodes are evicted least recently
# used above the cache budget.
EVICTABLE_DERIVATIVE_KINDS: Final = frozenset(
    {DerivativeKind.PREVIEW.value, DerivativeKind.TRANSCODE.value}
)

# M4 defaults. Posters are built at both sizes: the thumbnail-size poster is the
# video's ``thumbnail`` derivative and the preview-size poster is its ``poster``.
THUMBNAIL_MAX_EDGE: Final = 400
THUMBNAIL_WEBP_QUALITY: Final = 75
PREVIEW_MAX_EDGE: Final = 2048
PREVIEW_WEBP_QUALITY: Final = 82
POSTER_FRAME_MS: Final = 1000
TRANSCODE_MAX_HEIGHT: Final = 1080


def media_kind(type_hint: object) -> MediaKind | None:
    """Return the media kind of a catalog extension, or ``None`` if not media."""
    if not isinstance(type_hint, str):
        return None
    return MEDIA_KIND_BY_EXTENSION.get(type_hint)


def original_content_type(type_hint: object, *, inline: bool) -> str | None:
    """Return the content type for serving an original.

    For inline delivery this is ``None`` unless the extension is browser-native;
    attachments always have a content type.
    """
    content_type = (
        INLINE_ORIGINAL_CONTENT_TYPES.get(type_hint) if isinstance(type_hint, str) else None
    )
    if inline:
        return content_type
    return content_type or ATTACHMENT_FALLBACK_CONTENT_TYPE


def derivative_content_type(extension: object) -> str:
    if not isinstance(extension, str) or extension not in DERIVATIVE_CONTENT_TYPES:
        raise ValueError("invalid derivative extension")
    return DERIVATIVE_CONTENT_TYPES[extension]
