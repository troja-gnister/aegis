from __future__ import annotations

import pytest
from aegis_apps.media.enums import DerivativeExtension, DerivativeKind, MediaKind
from aegis_apps.media.formats import (
    DERIVATIVE_EXTENSION_BY_KIND,
    DERIVATIVE_KINDS_BY_MEDIA_KIND,
    EVICTABLE_DERIVATIVE_KINDS,
    INLINE_ORIGINAL_CONTENT_TYPES,
    MEDIA_EXTENSIONS,
    PHOTO_EXTENSIONS,
    VIDEO_EXTENSIONS,
    derivative_content_type,
    media_kind,
    original_content_type,
)


def test_extension_allowlists_match_m4_exactly() -> None:
    assert frozenset(
        {"jpg", "jpeg", "png", "webp", "gif", "heic", "heif", "avif", "bmp", "tif", "tiff"}
    ) == PHOTO_EXTENSIONS
    assert frozenset(
        {"mp4", "m4v", "mov", "webm", "mkv", "avi", "3gp", "mts", "m2ts"}
    ) == VIDEO_EXTENSIONS
    assert not PHOTO_EXTENSIONS & VIDEO_EXTENSIONS
    assert tuple(sorted(PHOTO_EXTENSIONS | VIDEO_EXTENSIONS)) == MEDIA_EXTENSIONS
    for extension in MEDIA_EXTENSIONS:
        assert extension == extension.lower()
        assert extension.isascii() and extension.isalnum()
        assert 1 <= len(extension) <= 16


@pytest.mark.parametrize(
    ("type_hint", "expected"),
    [
        ("jpg", MediaKind.PHOTO),
        ("heic", MediaKind.PHOTO),
        ("tiff", MediaKind.PHOTO),
        ("mp4", MediaKind.VIDEO),
        ("m2ts", MediaKind.VIDEO),
        ("3gp", MediaKind.VIDEO),
        ("JPG", None),
        ("raw", None),
        ("cr2", None),
        ("txt", None),
        ("", None),
        (None, None),
        (1, None),
    ],
)
def test_media_kind_uses_the_lowercase_catalog_extension(
    type_hint: object, expected: MediaKind | None
) -> None:
    assert media_kind(type_hint) == expected


def test_inline_originals_are_exactly_the_browser_native_allowlist() -> None:
    assert set(INLINE_ORIGINAL_CONTENT_TYPES) == {
        "jpg", "jpeg", "png", "webp", "gif", "avif", "mp4", "m4v", "webm", "mov",
    }
    assert set(INLINE_ORIGINAL_CONTENT_TYPES) <= set(MEDIA_EXTENSIONS)
    for content_type in INLINE_ORIGINAL_CONTENT_TYPES.values():
        assert content_type.split("/")[0] in {"image", "video"}
        assert ";" not in content_type


@pytest.mark.parametrize(
    ("type_hint", "inline", "attachment"),
    [
        ("jpg", "image/jpeg", "image/jpeg"),
        ("jpeg", "image/jpeg", "image/jpeg"),
        ("png", "image/png", "image/png"),
        ("mov", "video/quicktime", "video/quicktime"),
        ("m4v", "video/mp4", "video/mp4"),
        ("heic", None, "application/octet-stream"),
        ("mkv", None, "application/octet-stream"),
        ("html", None, "application/octet-stream"),
        ("svg", None, "application/octet-stream"),
        (None, None, "application/octet-stream"),
    ],
)
def test_original_content_types_never_widen_inline_delivery(
    type_hint: str | None, inline: str | None, attachment: str
) -> None:
    assert original_content_type(type_hint, inline=True) == inline
    assert original_content_type(type_hint, inline=False) == attachment


def test_derivative_kinds_extensions_and_eviction_are_consistent() -> None:
    assert dict(DERIVATIVE_EXTENSION_BY_KIND) == {
        "thumbnail": DerivativeExtension.WEBP,
        "preview": DerivativeExtension.WEBP,
        "poster": DerivativeExtension.WEBP,
        "transcode": DerivativeExtension.MP4,
    }
    assert set(DERIVATIVE_EXTENSION_BY_KIND) == set(DerivativeKind.values)
    assert frozenset({"preview", "transcode"}) == EVICTABLE_DERIVATIVE_KINDS
    assert DERIVATIVE_KINDS_BY_MEDIA_KIND["photo"] == ("thumbnail", "preview")
    assert DERIVATIVE_KINDS_BY_MEDIA_KIND["video"] == ("thumbnail", "poster", "transcode")
    assert derivative_content_type("webp") == "image/webp"
    assert derivative_content_type("mp4") == "video/mp4"
    with pytest.raises(ValueError, match="invalid derivative extension"):
        derivative_content_type("jpg")
