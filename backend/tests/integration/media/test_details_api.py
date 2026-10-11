from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.media.links import fingerprint_token
from aegis_apps.roots.permissions import Permission
from django.test import Client

from .conftest import MediaCatalog, other_root

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def _details(media: MediaCatalog, entry: CatalogEntry, **kwargs: Any) -> Any:
    return media.get(f"/api/v1/entries/{entry.pk}/media", **kwargs)


def test_photo_details_match_the_contract(media_catalog: MediaCatalog) -> None:
    entry = media_catalog.entry(b"IMG_0001.jpg")
    asset = media_catalog.asset(
        entry,
        orientation=6,
        captured_at=datetime(2024, 5, 1, 10, 30, tzinfo=UTC),
        captured_at_source="exif",
        sort_source="captured",
        camera_make="Synthetic",
        camera_model="Camera 1",
        has_location=True,
    )
    media_catalog.derivative(asset)
    media_catalog.derivative(asset, "preview")
    namespace = media_catalog.namespace()
    token = fingerprint_token(asset.fingerprint)
    query = f"f={token}&n={namespace}&e=0"

    response = _details(media_catalog, entry)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.json() == {
        "entryId": str(entry.pk),
        "kind": "photo",
        "fingerprint": token,
        "metadata": {
            "width": 4032, "height": 3024, "orientation": 6,
            "capturedAt": "2024-05-01T10:30:00Z", "capturedAtSource": "exif",
            "cameraMake": "Synthetic", "cameraModel": "Camera 1", "durationMs": None,
            "videoCodec": None, "audioCodec": None, "container": None, "hasLocation": True,
        },
        "derivatives": {
            "thumbnail": {
                "state": "ready",
                "url": f"/media/v1/derivatives/{entry.pk}/thumbnail?{query}",
            },
            "preview": {
                "state": "ready",
                "url": f"/media/v1/derivatives/{entry.pk}/preview?{query}",
            },
        },
        "original": {
            "contentType": "image/jpeg",
            "inlineUrl": f"/media/v1/originals/{entry.pk}?d=inline&n={namespace}&e=0",
            "downloadUrl": f"/media/v1/originals/{entry.pk}?d=attachment&n={namespace}&e=0",
            "playbackType": None,
        },
        "permissions": {"preview": True, "export": True},
    }


def test_video_details_offer_playback_type_and_video_derivatives(
    media_catalog: MediaCatalog,
) -> None:
    entry = media_catalog.entry(b"clip.mp4")
    asset = media_catalog.asset(entry, kind="video")
    media_catalog.derivative(asset)
    media_catalog.derivative(asset, "poster")

    payload = _details(media_catalog, entry).json()

    assert payload["kind"] == "video"
    assert set(payload["derivatives"]) == {"thumbnail", "poster", "transcode"}
    assert payload["derivatives"]["poster"]["state"] == "ready"
    assert payload["derivatives"]["transcode"] == {"state": "missing", "url": None}
    assert payload["original"]["contentType"] == "video/mp4"
    assert payload["original"]["playbackType"] == 'video/mp4; codecs="avc1.64002A, mp4a.40.2"'
    assert payload["metadata"]["durationMs"] == 12_345
    assert payload["metadata"]["videoCodec"] == "h264"
    assert payload["metadata"]["container"] == "mp4"


def test_unplayable_video_has_no_playback_type(media_catalog: MediaCatalog) -> None:
    entry = media_catalog.entry(b"clip.mkv")
    media_catalog.asset(entry, kind="video", container="matroska", video_codec="hevc")

    payload = _details(media_catalog, entry).json()

    assert payload["original"]["inlineUrl"] is None
    assert payload["original"]["playbackType"] is None
    assert payload["original"]["contentType"] == "application/octet-stream"


@pytest.mark.parametrize(
    ("permissions", "preview", "export"),
    [
        (Permission.BROWSE, False, False),
        (Permission.BROWSE | Permission.PREVIEW, True, False),
        (Permission.BROWSE | Permission.EXPORT, False, True),
    ],
)
def test_urls_follow_the_callers_permissions(
    media_catalog: MediaCatalog, permissions: Permission, preview: bool, export: bool,
) -> None:
    entry = media_catalog.entry(b"IMG_0002.jpg")
    asset = media_catalog.asset(entry)
    media_catalog.derivative(asset)
    media_catalog.grant(permissions)

    payload = _details(media_catalog, entry).json()

    assert payload["permissions"] == {"preview": preview, "export": export}
    assert (payload["derivatives"]["thumbnail"]["url"] is not None) is preview
    assert (payload["original"]["inlineUrl"] is not None) is preview
    assert (payload["original"]["downloadUrl"] is not None) is export
    assert payload["derivatives"]["thumbnail"]["state"] == "ready"


def test_unavailable_entries_keep_metadata_but_lose_every_url(
    media_catalog: MediaCatalog,
) -> None:
    entry = media_catalog.entry(b"IMG_0003.jpg")
    media_catalog.derivative(media_catalog.asset(entry))
    CatalogEntry.objects.filter(pk=entry.pk).update(source_state="inaccessible")

    payload = _details(media_catalog, entry).json()

    assert payload["derivatives"]["thumbnail"] == {"state": "ready", "url": None}
    assert payload["original"]["inlineUrl"] is None
    assert payload["original"]["downloadUrl"] is None


def test_derivative_states_cover_pending_failed_stale_and_requested(
    media_catalog: MediaCatalog,
) -> None:
    entry = media_catalog.entry(b"IMG_0004.jpg")
    asset = media_catalog.asset(entry, thumbnail_state="failed", safe_error_code="decode_failed")
    media_catalog.derivative(asset, "preview", state="stale", variant=0)

    first = _details(media_catalog, entry).json()["derivatives"]
    assert first == {
        "thumbnail": {"state": "failed", "url": None},
        "preview": {"state": "missing", "url": None},
    }

    media_catalog.derivative(asset, "preview", state="failed")
    assert _details(media_catalog, entry).json()["derivatives"]["preview"]["state"] == "failed"

    requested = media_catalog.post(
        f"/api/v1/entries/{entry.pk}/media/requests", {"kind": "preview"},
    )
    assert requested.status_code == 202
    assert _details(media_catalog, entry).json()["derivatives"]["preview"]["state"] == "pending"


def test_a_thumbnail_marked_ready_without_a_current_file_is_missing(
    media_catalog: MediaCatalog,
) -> None:
    entry = media_catalog.entry(b"IMG_0005.jpg")
    asset = media_catalog.asset(entry)
    media_catalog.derivative(asset, fingerprint="d" * 64)

    assert _details(media_catalog, entry).json()["derivatives"]["thumbnail"] == {
        "state": "missing", "url": None,
    }


@pytest.mark.parametrize("state", ["unsupported", "quarantined"])
def test_terminal_assets_report_their_state_for_every_derivative(
    media_catalog: MediaCatalog, state: str,
) -> None:
    entry = media_catalog.entry(b"IMG_0006.jpg")
    media_catalog.asset(
        entry, probe_state=state, thumbnail_state=state,
        safe_error_code="timeout" if state == "quarantined" else None,
        width=None, height=None,
    )

    derivatives = _details(media_catalog, entry).json()["derivatives"]

    assert derivatives == {
        "thumbnail": {"state": state, "url": None},
        "preview": {"state": state, "url": None},
    }


def test_non_media_unknown_foreign_and_directory_entries_are_404(
    media_catalog: MediaCatalog,
) -> None:
    plain = media_catalog.entry(b"notes.txt")
    undiscovered = media_catalog.entry(b"IMG_0007.jpg")
    folder = media_catalog.entry(b"albums", kind="directory")
    foreign = CatalogEntry.objects.create(
        root=other_root(), raw_name=b"", display_name="", name_key=b"", kind="directory",
    )

    responses = [
        _details(media_catalog, entry) for entry in (plain, undiscovered, folder, foreign)
    ]
    responses.append(media_catalog.get(f"/api/v1/entries/{uuid.uuid4()}/media"))

    for response in responses:
        assert response.status_code == 404
        assert response.json() == {"type": "catalog_not_found", "title": "Catalog item not found"}
        assert response.headers["Cache-Control"] == "private, no-store"


def test_details_need_browse(media_catalog: MediaCatalog) -> None:
    entry = media_catalog.entry(b"IMG_0008.jpg")
    media_catalog.asset(entry)
    media_catalog.grant(Permission.PREVIEW | Permission.EXPORT)

    assert _details(media_catalog, entry).status_code == 404


def test_details_require_authentication_and_reject_query_parameters(
    media_catalog: MediaCatalog,
) -> None:
    entry = media_catalog.entry(b"IMG_0009.jpg")
    media_catalog.asset(entry)

    anonymous = Client().get(f"/api/v1/entries/{entry.pk}/media")
    with_query = media_catalog.get(f"/api/v1/entries/{entry.pk}/media?content=true")

    assert anonymous.status_code == 401
    assert anonymous.headers["Cache-Control"] == "private, no-store"
    assert with_query.status_code == 400
    assert with_query.json()["type"] == "invalid_catalog_query"


def test_details_never_serialize_source_identity(media_catalog: MediaCatalog) -> None:
    entry = media_catalog.entry(b"IMG_0010.jpg", device=98_765_432_109, inode=12_345_678_901)
    asset = media_catalog.asset(entry)
    media_catalog.derivative(asset)

    body = _details(media_catalog, entry).content.decode()

    # The fingerprint hashes device and inode, so only its keyed token is ever sent.
    assert asset.fingerprint not in body
    assert fingerprint_token(asset.fingerprint) in body

    for forbidden in ("98765432109", "12345678901", "/srv/aegis", "raw_name", "inode", "slot"):
        assert forbidden not in body
