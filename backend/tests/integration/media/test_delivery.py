from __future__ import annotations

import builtins
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.identity.models import User
from aegis_apps.media.delivery import record_derivative_access
from aegis_apps.media.models import MediaAsset, MediaDerivative
from aegis_apps.media.paths import derivative_redirect
from aegis_apps.roots.models import Root, RootGrant
from aegis_apps.roots.permissions import Permission
from django.contrib.sessions.models import Session
from django.db.models import F
from django.test import Client
from django.utils import timezone

from .conftest import ALL_MEDIA, MediaCatalog, other_root

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

NOT_FOUND = b"Not found\n"


def _assert_not_found(response: Any) -> None:
    assert response.status_code == 404
    assert response.content == NOT_FOUND
    assert response.headers["Cache-Control"] == "private, no-store"
    assert "X-Accel-Redirect" not in response.headers
    assert "ETag" not in response.headers


def _photo(media: MediaCatalog, name: bytes = b"IMG_0001.jpg") -> tuple[CatalogEntry, MediaAsset]:
    entry = media.entry(name)
    return entry, media.asset(entry)


def _etag(derivative: MediaDerivative) -> str:
    derivative.refresh_from_db()
    created = derivative.created_at - datetime(1970, 1, 1, tzinfo=UTC)
    microseconds = (created.days * 86_400 + created.seconds) * 1_000_000 + created.microseconds
    return f'"{derivative.key}.{derivative.bytes}.{microseconds}"'


# Derivatives


def test_ready_derivative_redirects_with_private_revalidation_headers(
    media_catalog: MediaCatalog,
) -> None:
    entry, asset = _photo(media_catalog)
    thumbnail = media_catalog.derivative(asset)

    response = media_catalog.get(media_catalog.derivative_path(entry))

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["X-Accel-Redirect"] == derivative_redirect(thumbnail.key, "webp")
    assert response.headers["X-Accel-Redirect"] == (
        f"/__aegis_derivatives/{thumbnail.key[0:2]}/{thumbnail.key[2:4]}/{thumbnail.key}.webp"
    )
    assert response.headers["Content-Type"] == "image/webp"
    assert response.headers["Cache-Control"] == "private, no-cache"
    assert response.headers["ETag"] == _etag(thumbnail)
    assert response.headers["ETag"].startswith(f'"{thumbnail.key}.2048.')
    assert "Cookie" in [value.strip() for value in response.headers["Vary"].split(",")]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Disposition" not in response.headers


def test_transcode_derivative_is_served_as_mp4(media_catalog: MediaCatalog) -> None:
    entry = media_catalog.entry(b"clip.mov")
    asset = media_catalog.asset(entry, kind="video")
    transcode = media_catalog.derivative(asset, "transcode")

    response = media_catalog.get(media_catalog.derivative_path(entry, "transcode"))

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "video/mp4"
    assert response.headers["X-Accel-Redirect"].endswith(f"/{transcode.key}.mp4")


@pytest.mark.parametrize(
    "header",
    ["{etag}", "W/{etag}", '"older-version", {etag}', "*"],
)
def test_matching_if_none_match_is_answered_304_without_a_redirect(
    media_catalog: MediaCatalog, header: str,
) -> None:
    entry, asset = _photo(media_catalog)
    thumbnail = media_catalog.derivative(asset)
    etag = _etag(thumbnail)

    response = media_catalog.get(
        media_catalog.derivative_path(entry),
        headers={"If-None-Match": header.format(etag=etag)},
    )

    assert response.status_code == 304
    assert response.content == b""
    assert "X-Accel-Redirect" not in response.headers
    assert "Content-Type" not in response.headers
    assert response.headers["ETag"] == etag
    assert response.headers["Cache-Control"] == "private, no-cache"
    assert "Cookie" in response.headers["Vary"]


def test_stale_validator_gets_the_current_bytes(media_catalog: MediaCatalog) -> None:
    entry, asset = _photo(media_catalog)
    thumbnail = media_catalog.derivative(asset)

    response = media_catalog.get(
        media_catalog.derivative_path(entry), headers={"If-None-Match": '"' + "0" * 64 + '"'},
    )

    assert response.status_code == 200
    assert response.headers["ETag"] == _etag(thumbnail)
    assert "X-Accel-Redirect" in response.headers


@pytest.mark.parametrize(
    "permissions",
    [
        Permission.BROWSE,
        Permission.BROWSE | Permission.EXPORT,
        # PREVIEW never replaces BROWSE.
        Permission.PREVIEW,
        Permission.PREVIEW | Permission.EXPORT,
        Permission(0),
    ],
)
def test_derivatives_require_browse_and_preview(
    media_catalog: MediaCatalog, permissions: Permission,
) -> None:
    entry, asset = _photo(media_catalog)
    media_catalog.derivative(asset)
    path = media_catalog.derivative_path(entry)
    media_catalog.grant(permissions)

    _assert_not_found(media_catalog.get(path))


def test_group_and_admin_only_principals_get_404(media_catalog: MediaCatalog) -> None:
    entry, asset = _photo(media_catalog)
    media_catalog.derivative(asset)

    for client in (media_catalog.api.group_client, media_catalog.api.admin_client):
        path = media_catalog.derivative_path(entry, namespace=media_catalog.namespace(client))
        _assert_not_found(media_catalog.get(path, client=client))


def test_anonymous_requests_get_404(media_catalog: MediaCatalog) -> None:
    entry, asset = _photo(media_catalog)
    media_catalog.derivative(asset)
    path = media_catalog.derivative_path(entry)

    _assert_not_found(media_catalog.get(path, client=Client()))


def test_another_sessions_namespace_never_serves(media_catalog: MediaCatalog) -> None:
    entry, asset = _photo(media_catalog)
    media_catalog.derivative(asset)
    other = media_catalog.api.group_client
    RootGrant.objects.create(
        root_id=media_catalog.api.root.pk, user=media_catalog.api.group_user,
        permissions=int(ALL_MEDIA),
    )
    stolen = media_catalog.derivative_path(entry)
    own = media_catalog.derivative_path(entry, namespace=media_catalog.namespace(other))

    assert stolen != own
    _assert_not_found(media_catalog.get(stolen, client=other))
    assert media_catalog.get(own, client=other).status_code == 200


@pytest.mark.parametrize(
    "mutate",
    [
        lambda path: path.replace("&e=0", "&e=1"),
        lambda path: path.replace("&e=0", "&e=00"),
        lambda path: path.replace("&e=0", "&e=-0"),
        lambda path: path.replace("&e=0", f"&e={2**63}"),
        lambda path: path.replace("?f=", "?f=0").replace("&n=", "0&n="),
        lambda path: path.replace("?f=", "?f=A"),
        lambda path: path.replace("&n=", "&n=0"),
        lambda path: path.replace("&n=", "&n=%2F"),
        lambda path: path + "&e=0",
        lambda path: path + "&x=1",
        lambda path: path.split("&e=")[0],
        lambda path: path.replace("/thumbnail?", "/original?"),
        lambda path: path.replace("/thumbnail?", "/preview?"),
        lambda path: path + "&pad=" + "x" * 600,
    ],
)
def test_wrong_or_malformed_parameters_are_404(
    media_catalog: MediaCatalog, mutate: Any,
) -> None:
    entry, asset = _photo(media_catalog)
    media_catalog.derivative(asset)
    path = media_catalog.derivative_path(entry, epoch=0)
    assert media_catalog.get(path).status_code == 200

    _assert_not_found(media_catalog.get(mutate(path)))


def test_another_valid_fingerprint_never_serves_a_different_derivative(
    media_catalog: MediaCatalog,
) -> None:
    entry, asset = _photo(media_catalog)
    media_catalog.derivative(asset)
    other_entry, other_asset = _photo(media_catalog, b"IMG_0002.jpg")
    media_catalog.derivative(other_asset)

    _assert_not_found(media_catalog.get(
        media_catalog.derivative_path(entry, fingerprint=other_asset.fingerprint),
    ))
    _assert_not_found(media_catalog.get(
        media_catalog.derivative_path(other_entry, fingerprint=asset.fingerprint),
    ))


def test_root_epoch_change_invalidates_issued_urls(media_catalog: MediaCatalog) -> None:
    entry, asset = _photo(media_catalog)
    media_catalog.derivative(asset)
    issued = media_catalog.derivative_path(entry)
    Root.objects.filter(pk=media_catalog.api.root.pk).update(
        authorization_epoch=F("authorization_epoch") + 1,
    )

    _assert_not_found(media_catalog.get(issued))
    assert media_catalog.get(media_catalog.derivative_path(entry)).status_code == 200


@pytest.mark.parametrize("state", ["pending", "failed", "stale"])
def test_derivatives_that_are_not_ready_are_404(
    media_catalog: MediaCatalog, state: str,
) -> None:
    entry, asset = _photo(media_catalog)
    media_catalog.derivative(asset, state=state)

    _assert_not_found(media_catalog.get(media_catalog.derivative_path(entry)))


def test_a_derivative_of_an_older_fingerprint_is_never_served(
    media_catalog: MediaCatalog,
) -> None:
    entry, asset = _photo(media_catalog)
    old = asset.fingerprint
    media_catalog.derivative(asset, fingerprint=old)
    MediaAsset.objects.filter(pk=asset.pk).update(fingerprint="e" * 64)

    _assert_not_found(media_catalog.get(media_catalog.derivative_path(entry, fingerprint=old)))
    _assert_not_found(media_catalog.get(
        media_catalog.derivative_path(entry, fingerprint="e" * 64),
    ))


def test_kind_must_belong_to_the_asset_kind(media_catalog: MediaCatalog) -> None:
    entry = media_catalog.entry(b"clip.mp4")
    asset = media_catalog.asset(entry, kind="video")
    media_catalog.derivative(asset, "preview")

    _assert_not_found(media_catalog.get(media_catalog.derivative_path(entry, "preview")))


@pytest.mark.parametrize("mutation", ["missing", "inaccessible", "stale_parent"])
def test_unavailable_entries_are_404(media_catalog: MediaCatalog, mutation: str) -> None:
    folder = media_catalog.entry(b"albums", kind="directory")
    entry = media_catalog.entry(b"IMG_0003.jpg", parent=folder)
    media_catalog.derivative(media_catalog.asset(entry))
    path = media_catalog.derivative_path(entry)
    assert media_catalog.get(path).status_code == 200
    if mutation == "stale_parent":
        CatalogEntry.objects.filter(pk=folder.pk).update(source_revision=F("source_revision") + 1)
    else:
        CatalogEntry.objects.filter(pk=entry.pk).update(source_state=mutation)

    _assert_not_found(media_catalog.get(path))


def test_foreign_and_unknown_entries_are_indistinguishable(media_catalog: MediaCatalog) -> None:
    entry, asset = _photo(media_catalog)
    media_catalog.derivative(asset)
    issued = media_catalog.derivative_path(entry)
    unknown = issued.replace(str(entry.pk), str(uuid.uuid4()))
    foreign = CatalogEntry.objects.create(
        root=other_root(), raw_name=b"", display_name="", name_key=b"", kind="directory",
    )
    foreign_path = issued.replace(str(entry.pk), str(foreign.pk))

    responses = [media_catalog.get(unknown), media_catalog.get(foreign_path)]
    for response in responses:
        _assert_not_found(response)
    assert responses[0].content == responses[1].content


def test_stale_session_epoch_is_revoked_and_answered_404(media_catalog: MediaCatalog) -> None:
    entry, asset = _photo(media_catalog)
    media_catalog.derivative(asset)
    path = media_catalog.derivative_path(entry)
    session_key = media_catalog.client.cookies["sessionid"].value
    User.objects.filter(pk=media_catalog.api.user.pk).update(
        authorization_epoch=F("authorization_epoch") + 1,
    )

    _assert_not_found(media_catalog.get(path))
    assert not Session.objects.filter(session_key=session_key).exists()


def test_delivery_records_lru_access_through_the_web_boundary(
    media_catalog: MediaCatalog,
) -> None:
    entry, asset = _photo(media_catalog)
    old = timezone.now() - timedelta(hours=2)
    preview = media_catalog.derivative(asset, "preview", last_access_at=old)

    response = media_catalog.get(media_catalog.derivative_path(entry, "preview"))

    assert response.status_code == 200
    preview.refresh_from_db()
    assert preview.last_access_at > old + timedelta(hours=1)


@pytest.mark.parametrize(
    ("kind", "age", "touched"),
    [
        ("preview", timedelta(hours=2), True),
        ("preview", timedelta(minutes=10), False),
        ("thumbnail", timedelta(hours=2), False),
    ],
)
def test_owner_login_test_databases_use_the_equivalent_touch(
    media_catalog: MediaCatalog, kind: str, age: timedelta, touched: bool,
) -> None:
    _, asset = _photo(media_catalog)
    before = timezone.now() - age
    derivative = media_catalog.derivative(asset, kind, last_access_at=before)

    record_derivative_access(derivative.key)

    derivative.refresh_from_db()
    assert (derivative.last_access_at != before) is touched


def test_unknown_keys_are_a_harmless_touch(media_catalog: MediaCatalog) -> None:
    record_derivative_access("0" * 64)
    assert not MediaDerivative.objects.exists()


# Originals


def test_inline_original_redirects_to_the_encoded_root_path(media_catalog: MediaCatalog) -> None:
    folder = media_catalog.entry(b"Trip 2024 #1", kind="directory")
    nested = media_catalog.entry(b"50%? done", parent=folder, kind="directory")
    entry = media_catalog.entry(b"caf\xc3\xa9 \xe9.jpg", parent=nested)
    slot = media_catalog.root.slot_id

    response = media_catalog.get(media_catalog.original_path(entry, "inline"))

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["X-Accel-Redirect"] == (
        f"/__aegis_roots/{slot}/Trip%202024%20%231/50%25%3F%20done/caf%C3%A9%20%E9.jpg"
    )
    assert response.headers["Content-Type"] == "image/jpeg"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Disposition"] == (
        "inline; filename=\"caf_ _.jpg\"; filename*=UTF-8''caf%C3%A9%20%EF%BF%BD.jpg"
    )
    assert "ETag" not in response.headers


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        (b"IMG_0004.jpg", "image/jpeg"),
        (b"clip.m4v", "video/mp4"),
        (b"clip.mov", "video/quicktime"),
        (b"photo.heic", "application/octet-stream"),
        (b"notes.txt", "application/octet-stream"),
        (b"page.html", "application/octet-stream"),
        (b"no-extension", "application/octet-stream"),
    ],
)
def test_attachments_need_export_and_use_the_allowlisted_type(
    media_catalog: MediaCatalog, name: bytes, content_type: str,
) -> None:
    entry = media_catalog.entry(name)

    response = media_catalog.get(media_catalog.original_path(entry, "attachment"))

    assert response.status_code == 200
    assert response.headers["Content-Type"] == content_type
    assert response.headers["Content-Disposition"].startswith("attachment; filename=\"")
    assert response.headers["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize(
    ("name", "allowed"),
    [
        (b"a.jpg", True), (b"a.jpeg", True), (b"a.png", True), (b"a.webp", True),
        (b"a.gif", True), (b"a.avif", True), (b"a.mp4", True), (b"a.m4v", True),
        (b"a.webm", True), (b"a.mov", True), (b"a.heic", False), (b"a.tiff", False),
        (b"a.mkv", False), (b"a.svg", False), (b"a.html", False), (b"a.txt", False),
    ],
)
def test_inline_needs_an_allowlisted_type(
    media_catalog: MediaCatalog, name: bytes, allowed: bool,
) -> None:
    entry = media_catalog.entry(name)

    response = media_catalog.get(media_catalog.original_path(entry, "inline"))

    if allowed:
        assert response.status_code == 200
    else:
        _assert_not_found(response)


@pytest.mark.parametrize(
    ("disposition", "permissions"),
    [
        ("inline", Permission.BROWSE | Permission.EXPORT),
        ("inline", Permission.PREVIEW),
        ("attachment", Permission.BROWSE | Permission.PREVIEW),
        ("attachment", Permission.EXPORT),
        ("attachment", Permission.EXPORT | Permission.PREVIEW),
    ],
)
def test_originals_require_browse_and_the_disposition_permission(
    media_catalog: MediaCatalog, disposition: str, permissions: Permission,
) -> None:
    entry = media_catalog.entry(b"IMG_0005.jpg")
    path = media_catalog.original_path(entry, disposition)
    media_catalog.grant(permissions)

    _assert_not_found(media_catalog.get(path))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda path: path.replace("?d=inline", "?d=download"),
        lambda path: path.replace("?d=inline", "?d=INLINE"),
        lambda path: path.replace("&e=0", "&e=1"),
        lambda path: path.replace("&n=", "&n=f"),
        lambda path: path + "&f=" + "0" * 64,
        lambda path: path.replace("?d=inline&", "?"),
    ],
)
def test_original_parameter_mismatches_are_404(
    media_catalog: MediaCatalog, mutate: Any,
) -> None:
    entry = media_catalog.entry(b"IMG_0006.jpg")
    path = media_catalog.original_path(entry, "inline", epoch=0)
    assert media_catalog.get(path).status_code == 200

    _assert_not_found(media_catalog.get(mutate(path)))


def test_directories_and_unavailable_originals_are_404(media_catalog: MediaCatalog) -> None:
    folder = media_catalog.entry(b"folder.jpg", kind="directory")
    gone = media_catalog.entry(b"gone.jpg")
    CatalogEntry.objects.filter(pk=gone.pk).update(source_state="missing")

    _assert_not_found(media_catalog.get(media_catalog.original_path(folder, "attachment")))
    _assert_not_found(media_catalog.get(media_catalog.original_path(gone, "attachment")))


@pytest.mark.parametrize("disposition", ["inline", "attachment"])
def test_originals_never_touch_the_filesystem(
    media_catalog: MediaCatalog, monkeypatch: pytest.MonkeyPatch, disposition: str,
) -> None:
    entry = media_catalog.entry(b"IMG_0007.jpg")
    touched: list[str] = []

    def guarded(original: Any) -> Any:
        def call(path: Any, *args: Any, **kwargs: Any) -> Any:
            name = os.fsdecode(path) if isinstance(path, (str, bytes, os.PathLike)) else ""
            if name.startswith("/srv/aegis") or "IMG_0007" in name:
                touched.append(name)
                raise AssertionError("web touched an original")
            return original(path, *args, **kwargs)
        return call

    for module, name in (
        (os, "open"), (os, "stat"), (os, "lstat"), (os, "scandir"), (os, "listdir"),
        (builtins, "open"),
    ):
        monkeypatch.setattr(module, name, guarded(getattr(module, name)))
    response = media_catalog.get(media_catalog.original_path(entry, disposition))

    assert response.status_code == 200
    assert response.headers["X-Accel-Redirect"].startswith("/__aegis_roots/")
    assert touched == []


def test_a_file_changed_since_discovery_is_404_until_rediscovered(
    media_catalog: MediaCatalog,
) -> None:
    entry, asset = _photo(media_catalog)
    media_catalog.derivative(asset)
    path = media_catalog.derivative_path(entry)
    assert media_catalog.get(path).status_code == 200

    for change in ({"mtime_ns": F("mtime_ns") + 1}, {"inode": F("inode") + 1},
                   {"size": F("size") + 1}, {"device": None}):
        CatalogEntry.objects.filter(pk=entry.pk).update(**change)
        _assert_not_found(media_catalog.get(path))
        details = media_catalog.get(f"/api/v1/entries/{entry.pk}/media").json()
        assert details["derivatives"]["thumbnail"] == {"state": "pending", "url": None}
        CatalogEntry.objects.filter(pk=entry.pk).update(
            device=7, inode=1000, size=4096, mtime_ns=1_700_000_000_000_000_000,
        )
        assert media_catalog.get(path).status_code == 200


def test_urls_carry_a_keyed_token_never_the_fingerprint(media_catalog: MediaCatalog) -> None:
    entry, asset = _photo(media_catalog)
    media_catalog.derivative(asset)
    namespace = media_catalog.namespace()
    raw = (
        f"/media/v1/derivatives/{entry.pk}/thumbnail"
        f"?f={asset.fingerprint}&n={namespace}&e=0"
    )
    issued = media_catalog.derivative_path(entry)
    details = media_catalog.get(f"/api/v1/entries/{entry.pk}/media")

    _assert_not_found(media_catalog.get(raw))
    assert media_catalog.get(issued).status_code == 200
    assert asset.fingerprint not in issued
    assert asset.fingerprint not in details.content.decode()
    token = details.json()["fingerprint"]
    assert len(token) == 32 and all(c in "0123456789abcdef" for c in token)
    assert f"f={token}&" in issued


def test_a_regenerated_derivative_gets_a_new_validator(media_catalog: MediaCatalog) -> None:
    entry, asset = _photo(media_catalog)
    old = media_catalog.derivative(asset, "preview")
    old_etag = _etag(old)
    key = old.key
    MediaDerivative.objects.filter(pk=old.pk).delete()
    regenerated = media_catalog.derivative(asset, "preview", key=key, bytes=4096)
    path = media_catalog.derivative_path(entry, "preview")

    response = media_catalog.get(path, headers={"If-None-Match": old_etag})

    assert regenerated.key == key
    assert response.status_code == 200
    assert response.headers["ETag"] == _etag(regenerated) != old_etag
    assert "X-Accel-Redirect" in response.headers


def test_stale_navigation_makes_an_entry_unavailable(media_catalog: MediaCatalog) -> None:
    logical = media_catalog.entry(b"album", kind="directory")
    entry = media_catalog.entry(b"IMG_0008.jpg", logical_parent=logical)
    media_catalog.derivative(media_catalog.asset(entry))
    paths = [
        media_catalog.derivative_path(entry),
        media_catalog.original_path(entry, "attachment"),
    ]
    assert [media_catalog.get(path).status_code for path in paths] == [200, 200]
    CatalogEntry.objects.filter(pk=logical.pk).update(source_state="missing")

    for path in paths:
        _assert_not_found(media_catalog.get(path))
    details = media_catalog.get(f"/api/v1/entries/{entry.pk}/media").json()
    assert details["derivatives"]["thumbnail"]["url"] is None
    assert details["original"]["downloadUrl"] is None


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete", "options"])
def test_delivery_is_read_only(media_catalog: MediaCatalog, method: str) -> None:
    entry, asset = _photo(media_catalog)
    media_catalog.derivative(asset)
    path = media_catalog.derivative_path(entry)

    with media_catalog.api.database.as_django_role("aegis_web"):
        response = getattr(media_catalog.client, method)(
            path, headers={"X-CSRFToken": media_catalog.client.cookies["csrftoken"].value},
        )

    assert response.status_code == 405
    assert response.headers["Cache-Control"] == "private, no-store"


def test_resolver_errors_under_media_are_never_stored(media_catalog: MediaCatalog) -> None:
    response = media_catalog.get("/media/v1/derivatives/not-a-uuid/thumbnail")

    assert response.status_code == 404
    assert response.headers["Cache-Control"] == "private, no-store"
