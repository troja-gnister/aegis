from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from aegis_apps.media.delivery import if_none_match_hits, strong_etag
from aegis_apps.media.links import (
    MediaLinks,
    derivative_url,
    download_url,
    entry_media,
    fingerprint_token,
    original_url,
    rfc3339_utc,
)
from aegis_apps.media.selectors import ReadyDerivative
from django.test import override_settings

ENTRY = uuid.UUID("0b8e5e4c-7f3a-4c1e-9a55-3c7d2f1e0a11")
FINGERPRINT = "f" * 64
NAMESPACE = "a" * 40
LINKS = MediaLinks(namespace=NAMESPACE, root_epoch=7, preview=True, export=False)


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": ENTRY,
        "_media_kind": "photo",
        "_media_thumbnail": "ready",
        "_media_sort_at": datetime(2024, 5, 1, 10, 30, tzinfo=UTC),
        "_media_sort_source": "captured",
        "_media_width": 4032,
        "_media_height": 3024,
        "_media_duration_ms": None,
        "_media_fingerprint": FINGERPRINT,
    }
    row.update(overrides)
    return row


def test_entry_media_matches_the_contract_shape() -> None:
    assert entry_media(_row(), available=True, links=LINKS) == {
        "kind": "photo",
        "thumbnail": "ready",
        "sortDate": "2024-05-01T10:30:00Z",
        "sortDateSource": "captured",
        "width": 4032,
        "height": 3024,
        "durationMs": None,
        "thumbnailUrl": (
            f"/media/v1/derivatives/{ENTRY}/thumbnail"
            f"?f={fingerprint_token(FINGERPRINT)}&n={NAMESPACE}&e=7"
        ),
    }


def test_entry_without_an_asset_has_no_media() -> None:
    assert entry_media(_row(_media_kind=None), available=True, links=LINKS) is None


@pytest.mark.parametrize(
    ("overrides", "available", "links"),
    [
        ({"_media_thumbnail": "pending"}, True, LINKS),
        ({"_media_thumbnail": "failed"}, True, LINKS),
        ({"_media_thumbnail": "unsupported"}, True, LINKS),
        ({"_media_thumbnail": "quarantined"}, True, LINKS),
        ({}, False, LINKS),
        ({}, True, MediaLinks(namespace=NAMESPACE, root_epoch=7, preview=False, export=True)),
    ],
)
def test_thumbnail_url_needs_ready_available_and_preview(
    overrides: dict[str, object], available: bool, links: MediaLinks,
) -> None:
    media = entry_media(_row(**overrides), available=available, links=links)

    assert media is not None
    assert media["thumbnailUrl"] is None
    assert media["thumbnail"] == overrides.get("_media_thumbnail", "ready")


def test_video_media_carries_duration_and_modified_fallback() -> None:
    media = entry_media(
        _row(
            _media_kind="video", _media_sort_source="modified", _media_duration_ms=12_345,
            _media_sort_at=datetime(2023, 1, 2, 3, 4, 5, 600, tzinfo=timezone(timedelta(hours=2))),
        ),
        available=True,
        links=LINKS,
    )

    assert media is not None
    assert media["kind"] == "video"
    assert media["sortDateSource"] == "modified"
    assert media["sortDate"] == "2023-01-02T01:04:05.000600Z"
    assert media["durationMs"] == 12_345


@pytest.mark.parametrize(
    "overrides",
    [
        {"_media_kind": "audio"},
        {"_media_thumbnail": "missing"},
        {"_media_sort_source": "exif"},
        {"_media_sort_at": "2024-05-01"},
        {"_media_fingerprint": None},
    ],
)
def test_malformed_joined_rows_fail_closed(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        entry_media(_row(**overrides), available=True, links=LINKS)


def test_delivery_urls_bind_namespace_epoch_and_fingerprint() -> None:
    assert derivative_url(
        ENTRY, "preview", FINGERPRINT, namespace=NAMESPACE, root_epoch=0,
    ) == (
        f"/media/v1/derivatives/{ENTRY}/preview"
        f"?f={fingerprint_token(FINGERPRINT)}&n={NAMESPACE}&e=0"
    )
    assert original_url(ENTRY, "inline", namespace=NAMESPACE, root_epoch=2**63 - 1) == (
        f"/media/v1/originals/{ENTRY}?d=inline&n={NAMESPACE}&e={2**63 - 1}"
    )
    assert original_url(ENTRY, "attachment", namespace=NAMESPACE, root_epoch=3).endswith(
        f"?d=attachment&n={NAMESPACE}&e=3"
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"namespace": "", "root_epoch": 1},
        {"namespace": "a/b", "root_epoch": 1},
        {"namespace": "a&e=0", "root_epoch": 1},
        {"namespace": "a" * 129, "root_epoch": 1},
        {"namespace": NAMESPACE, "root_epoch": -1},
        {"namespace": NAMESPACE, "root_epoch": 2**63},
        {"namespace": NAMESPACE, "root_epoch": True},
    ],
)
def test_url_inputs_are_bounded(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        derivative_url(ENTRY, "thumbnail", FINGERPRINT, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        original_url(ENTRY, "inline", **kwargs)  # type: ignore[arg-type]


def test_derivative_url_refuses_unknown_kinds_and_fingerprints() -> None:
    with pytest.raises(ValueError):
        derivative_url(ENTRY, "original", FINGERPRINT, namespace=NAMESPACE, root_epoch=1)
    with pytest.raises(ValueError):
        derivative_url(ENTRY, "thumbnail", "F" * 64, namespace=NAMESPACE, root_epoch=1)
    with pytest.raises(ValueError):
        original_url(ENTRY, "download", namespace=NAMESPACE, root_epoch=1)  # type: ignore[arg-type]


EXPORT_LINKS = MediaLinks(namespace=NAMESPACE, root_epoch=7, preview=False, export=True)


def test_download_url_is_the_attachment_url_of_an_available_regular_file() -> None:
    assert download_url(ENTRY, kind="file", available=True, links=EXPORT_LINKS) == (
        f"/media/v1/originals/{ENTRY}?d=attachment&n={NAMESPACE}&e=7"
    )
    assert download_url(
        ENTRY, kind="file", available=True, links=EXPORT_LINKS,
    ) == original_url(ENTRY, "attachment", namespace=NAMESPACE, root_epoch=7)


@pytest.mark.parametrize(
    ("kind", "available", "links"),
    [
        # EXPORT is required; PREVIEW never stands in for it.
        ("file", True, LINKS),
        ("file", True, dataclasses.replace(EXPORT_LINKS, export=False, preview=True)),
        # Unavailable files: missing, inaccessible, unsupported or a stale chain.
        ("file", False, EXPORT_LINKS),
        # Only regular files have bytes to send.
        ("directory", True, EXPORT_LINKS),
        ("symlink", True, EXPORT_LINKS),
        ("special", True, EXPORT_LINKS),
        (None, True, EXPORT_LINKS),
        ("FILE", True, EXPORT_LINKS),
    ],
)
def test_download_url_needs_export_and_an_available_regular_file(
    kind: object, available: bool, links: MediaLinks,
) -> None:
    assert download_url(ENTRY, kind=kind, available=available, links=links) is None


def test_download_url_fails_closed_on_truthy_non_booleans_and_bad_inputs() -> None:
    assert download_url(
        ENTRY, kind="file", available=1, links=EXPORT_LINKS,  # type: ignore[arg-type]
    ) is None
    with pytest.raises(ValueError):
        download_url(
            str(ENTRY), kind="file", available=True, links=EXPORT_LINKS,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        download_url(
            ENTRY, kind="file", available=True,
            links=dataclasses.replace(EXPORT_LINKS, namespace="a&d=inline"),
        )


def test_rfc3339_requires_aware_timestamps() -> None:
    assert rfc3339_utc(datetime(1, 1, 1, tzinfo=UTC)) == "0001-01-01T00:00:00Z"
    with pytest.raises(ValueError):
        rfc3339_utc(datetime(2024, 1, 1))


DERIVATIVE = ReadyDerivative(
    key="a" * 64,
    extension="webp",
    size=2048,
    created_at=datetime(2026, 10, 11, 12, 0, 0, 123456, tzinfo=UTC),
    fingerprint=FINGERPRINT,
)
ETAG = '"' + "a" * 64 + '.2048.1791720000123456"'


def test_strong_etag_covers_the_key_size_and_publication_time() -> None:
    assert strong_etag(DERIVATIVE) == ETAG
    assert strong_etag(dataclasses.replace(DERIVATIVE, size=2049)) != ETAG
    assert strong_etag(dataclasses.replace(
        DERIVATIVE, created_at=DERIVATIVE.created_at + timedelta(microseconds=1),
    )) != ETAG


@pytest.mark.parametrize(
    ("header", "hit"),
    [
        (ETAG, True),
        (f"W/{ETAG}", True),
        (f'"other", {ETAG}', True),
        ("*", True),
        (" * ", True),
        ('"other"', False),
        ('"' + "a" * 64 + '"', False),
        (ETAG[:-1], False),
        ("", False),
        (None, False),
        ('"x", ' * 2000 + ETAG, False),
    ],
)
def test_if_none_match_uses_weak_comparison_against_the_validator(
    header: str | None, hit: bool,
) -> None:
    assert if_none_match_hits(header, strong_etag(DERIVATIVE)) is hit


def test_fingerprint_token_is_keyed_short_and_never_the_fingerprint() -> None:
    token = fingerprint_token(FINGERPRINT)

    assert len(token) == 32
    assert all(c in "0123456789abcdef" for c in token)
    assert token not in FINGERPRINT
    assert fingerprint_token(FINGERPRINT) == token
    assert fingerprint_token("e" * 64) != token
    with override_settings(SECRET_KEY="another-test-secret-key-for-media-tokens"):
        assert fingerprint_token(FINGERPRINT) != token
    for invalid in ("F" * 64, "f" * 63, "", "../" + "f" * 61):
        with pytest.raises(ValueError):
            fingerprint_token(invalid)
