"""The entry ``media`` field: one LEFT JOIN, within the catalog's statement budgets."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.media.keys import derivative_key, fingerprint, params_digest
from aegis_apps.media.links import fingerprint_token
from aegis_apps.media.models import MediaAsset, MediaDerivative
from aegis_apps.roots.models import RootGrant
from aegis_apps.roots.permissions import Permission
from django.db import connection
from django.test.utils import CaptureQueriesContext

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

SORT_AT = datetime(2024, 5, 1, 10, 30, tzinfo=UTC)


def _media_entry(
    browse_fixture: Any, raw: bytes, *, kind: str = "photo", thumbnail: str = "ready",
) -> tuple[CatalogEntry, MediaAsset]:
    entry: CatalogEntry = browse_fixture.entry(
        raw, size=10, mtime_ns=20, device=30, inode=40,
    )
    asset = MediaAsset.objects.create(
        entry=entry,
        root_id=entry.root_id,
        kind=kind,
        device=30,
        inode=40,
        size=10,
        mtime_ns=20,
        fingerprint=fingerprint(entry.root_id, entry.id, 30, 40, 10, 20),
        probe_state="ready",
        thumbnail_state=thumbnail,
        poster_state="pending" if kind == "video" else None,
        safe_error_code="decode_failed" if thumbnail in ("failed", "quarantined") else None,
        width=640,
        height=480,
        duration_ms=1500 if kind == "video" else None,
        sort_at=SORT_AT,
    )
    if thumbnail == "ready":
        digest = params_digest({"kind": "thumbnail"})
        MediaDerivative.objects.create(
            asset=asset, kind="thumbnail", fingerprint=asset.fingerprint,
            params_digest=digest,
            key=derivative_key(asset.fingerprint, "aegis-media-processor/1", "thumbnail", digest),
            extension="webp", bytes=100, width=400, height=300, state="ready",
            evictable=False,
        )
    return entry, asset


def _grant(browse_fixture: Any, permissions: Permission) -> None:
    RootGrant.objects.filter(root=browse_fixture.root, user=browse_fixture.user).update(
        permissions=int(permissions),
    )


def test_page_entries_carry_media_through_one_join(browse_fixture: Any) -> None:
    _grant(browse_fixture, Permission.BROWSE | Permission.PREVIEW)
    photo, photo_asset = _media_entry(browse_fixture, b"a-photo.jpg")
    video, _ = _media_entry(browse_fixture, b"b-video.mp4", kind="video", thumbnail="pending")
    plain = browse_fixture.entry(b"c-notes.txt")
    folder = browse_fixture.entry(b"d-folder", kind="directory")

    page = browse_fixture.page()
    entries = {entry["id"]: entry for entry in page["entries"]}
    assert photo_asset.fingerprint not in json.dumps(page)

    assert entries[str(photo.pk)]["media"] == {
        "kind": "photo",
        "thumbnail": "ready",
        "sortDate": "2024-05-01T10:30:00Z",
        "sortDateSource": "modified",
        "width": 640,
        "height": 480,
        "durationMs": None,
        "thumbnailUrl": (
            f"/media/v1/derivatives/{photo.pk}/thumbnail"
            f"?f={fingerprint_token(photo_asset.fingerprint)}&n={browse_fixture.namespace}&e=0"
        ),
    }
    assert entries[str(video.pk)]["media"]["thumbnail"] == "pending"
    assert entries[str(video.pk)]["media"]["thumbnailUrl"] is None
    assert entries[str(video.pk)]["media"]["durationMs"] == 1500
    assert entries[str(plain.pk)]["media"] is None
    assert entries[str(folder.pk)]["media"] is None


def test_thumbnail_urls_need_preview(browse_fixture: Any) -> None:
    photo, _ = _media_entry(browse_fixture, b"photo.jpg")

    entry = browse_fixture.page()["entries"][0]

    assert entry["id"] == str(photo.pk)
    assert entry["media"]["thumbnail"] == "ready"
    assert entry["media"]["thumbnailUrl"] is None


def test_unavailable_entries_never_get_thumbnail_urls(browse_fixture: Any) -> None:
    _grant(browse_fixture, Permission.BROWSE | Permission.PREVIEW)
    photo, _ = _media_entry(browse_fixture, b"photo.jpg")
    CatalogEntry.objects.filter(pk=photo.pk).update(source_state="inaccessible")

    entry = browse_fixture.page()["entries"][0]

    assert entry["sourceState"] == "inaccessible"
    assert entry["media"]["thumbnailUrl"] is None


def test_media_pages_keep_the_eight_data_statement_budget(browse_fixture: Any) -> None:
    _grant(browse_fixture, Permission.BROWSE | Permission.PREVIEW)
    for index in range(40):
        _media_entry(browse_fixture, f"IMG_{index:04d}.jpg".encode())
    browse_fixture.seed_ties(10)

    browse_fixture.statements.clear()
    page = browse_fixture.page(limit=50)
    sql = [" ".join(value.upper().split()) for value in browse_fixture.statements]
    data = [value for value in sql if value.startswith(("SELECT", "WITH"))]

    assert len(page["entries"]) == 50
    assert sum(entry["media"] is not None for entry in page["entries"]) == 40
    assert len(data) <= 8, data
    assert len(sql) <= 16
    assert sum("MEDIA_MEDIAASSET" in value for value in data) == 2
    assert any('LEFT OUTER JOIN "MEDIA_MEDIAASSET"' in value for value in data)
    assert not any("MEDIA_MEDIADERIVATIVE" in value for value in data)
    assert not any("RAW_NAME" in value or "INODE" in value for value in data)


def test_details_carry_media(browse_fixture: Any) -> None:
    _grant(browse_fixture, Permission.BROWSE | Permission.PREVIEW)
    photo, asset = _media_entry(browse_fixture, b"photo.jpg")
    plain = browse_fixture.entry(b"notes.txt")

    details = browse_fixture.details(photo.pk)

    assert details["media"]["thumbnailUrl"].endswith(
        f"?f={fingerprint_token(asset.fingerprint)}&n={browse_fixture.namespace}&e=0"
    )
    assert browse_fixture.details(plain.pk)["media"] is None


def test_http_list_with_media_stays_within_the_middleware_inclusive_budget(
    api_catalog: Any, browse_fixture: Any,
) -> None:
    _grant(browse_fixture, Permission.BROWSE | Permission.PREVIEW)
    for index in range(20):
        _media_entry(browse_fixture, f"IMG_{index:04d}.jpg".encode())

    with (
        api_catalog.database.as_django_role("aegis_web"),
        CaptureQueriesContext(connection) as captured,
    ):
        response = api_catalog.client.get(f"/api/v1/roots/{api_catalog.root.pk}/entries")

    assert response.status_code == 200
    statements = [query["sql"] for query in captured]
    assert len(statements) <= 16
    data = [
        sql for sql in statements
        if not sql.startswith(("BEGIN", "COMMIT", "SET LOCAL")) and "django_session" not in sql
    ]
    assert len(data) <= 8
    media = [entry["media"] for entry in response.json()["entries"] if entry["media"]]
    assert len(media) == 20
    assert all(item["thumbnailUrl"].startswith("/media/v1/derivatives/") for item in media)
