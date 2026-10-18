"""Downloads for every file: entry details' ``downloadUrl`` and its delivery (FILE-005).

Entry details issue the attachment URL of an original only with EXPORT, and only for a
regular file that is available (present, with current source and navigation chains). The
URL is the one media details issue, and delivery authorizes it again on every request:
a resumed download is a new request of the same session, so it ends with the session.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.media.paths import original_redirect
from aegis_apps.roots.models import RootGrant
from aegis_apps.roots.permissions import Permission
from django.db.models import F
from django.test import Client

from .conftest import ALL_MEDIA, MediaCatalog, SecondRoot, other_root

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

NOT_FOUND = b"Not found\n"
# The real login password of api_catalog's users (tests/conftest.py).
API_CATALOG_PASSWORD = "task10-real-login-password"
# What a browser resuming an interrupted download sends: a byte range plus the validator
# the gateway gave it. web never evaluates either; the gateway answers them.
RESUME_HEADERS = {"Range": "bytes=10-", "If-Range": '"5f5e1000-1000"'}


def _assert_not_found(response: Any) -> None:
    assert response.status_code == 404
    assert response.content == NOT_FOUND
    assert response.headers["Cache-Control"] == "private, no-store"
    assert "X-Accel-Redirect" not in response.headers


def _details(media: MediaCatalog, entry: CatalogEntry, client: Client | None = None) -> Any:
    response = media.get(f"/api/v1/entries/{entry.pk}", client=client)
    assert response.status_code == 200
    return response.json()


def _download_url(media: MediaCatalog, entry: CatalogEntry, client: Client | None = None) -> Any:
    return _details(media, entry, client)["downloadUrl"]


def _attachment(media: MediaCatalog, entry: CatalogEntry, client: Client | None = None) -> str:
    return (
        f"/media/v1/originals/{entry.pk}?d=attachment"
        f"&n={media.namespace(client)}&e={media.epoch()}"
    )


def _login(media: MediaCatalog) -> Client:
    client = Client(enforce_csrf_checks=True)
    with media.api.database.as_django_role("aegis_web"):
        csrf = client.get("/api/v1/auth/csrf")
        response = client.post(
            "/api/v1/auth/login",
            {"username": media.api.user.username, "password": API_CATALOG_PASSWORD},
            content_type="application/json",
            headers={"X-CSRFToken": csrf.json()["csrfToken"]},
        )
    assert response.status_code == 200
    return client


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        (b"notes.txt", "application/octet-stream"),
        (b"report.pdf", "application/octet-stream"),
        (b"page.html", "application/octet-stream"),
        (b"archive.tar.gz", "application/octet-stream"),
        (b"no-extension", "application/octet-stream"),
        # A photo the media catalog has not described yet is still a downloadable file.
        (b"IMG_0001.jpg", "image/jpeg"),
    ],
)
def test_every_available_regular_file_gets_a_download_url_that_delivers(
    media_catalog: MediaCatalog, name: bytes, content_type: str,
) -> None:
    folder = media_catalog.entry(b"Reports 2027", kind="directory")
    entry = media_catalog.entry(name, parent=folder)

    url = _download_url(media_catalog, entry)
    response = media_catalog.get(url)

    assert url == _attachment(media_catalog, entry)
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["X-Accel-Redirect"] == original_redirect(
        media_catalog.root.slot_id, (b"Reports 2027", name),
    )
    assert response.headers["Content-Type"] == content_type
    assert response.headers["Content-Disposition"].startswith('attachment; filename="')
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize(
    ("permissions", "exported"),
    [
        (Permission.BROWSE, False),
        (Permission.BROWSE | Permission.PREVIEW, False),
        (Permission.BROWSE | Permission.ROOT_ADMIN, False),
        (Permission.BROWSE | Permission.EXPORT, True),
        (ALL_MEDIA, True),
    ],
)
def test_download_urls_need_export(
    media_catalog: MediaCatalog, permissions: Permission, exported: bool,
) -> None:
    entry = media_catalog.entry(b"notes.txt")
    media_catalog.grant(permissions)

    url = _download_url(media_catalog, entry)

    assert (url == _attachment(media_catalog, entry)) is exported
    assert (url is None) is not exported
    # Delivery agrees with the details: the same attachment URL is refused without EXPORT.
    response = media_catalog.get(_attachment(media_catalog, entry))
    assert response.status_code == (200 if exported else 404)


def test_export_through_a_group_grant_counts(media_catalog: MediaCatalog) -> None:
    entry = media_catalog.entry(b"notes.txt")
    client = media_catalog.api.group_client
    assert _download_url(media_catalog, entry, client) is None

    # Grants are additive; the group's grant is the group user's only one.
    RootGrant.objects.filter(root_id=media_catalog.root.pk, group__isnull=False).update(
        permissions=int(Permission.BROWSE | Permission.EXPORT),
    )

    url = _download_url(media_catalog, entry, client)
    assert url == _attachment(media_catalog, entry, client)
    assert media_catalog.get(url, client=client).status_code == 200


@pytest.mark.parametrize("kind", ["directory", "symlink", "special"])
def test_only_regular_files_get_download_urls(media_catalog: MediaCatalog, kind: str) -> None:
    entry = media_catalog.entry(f"not-a-file.{kind}".encode(), kind=kind)

    details = _details(media_catalog, entry)

    assert details["kind"] == kind
    assert details["downloadUrl"] is None
    _assert_not_found(media_catalog.get(_attachment(media_catalog, entry)))


@pytest.mark.parametrize(
    ("mutation", "state"),
    [
        ("missing", "missing"),
        ("inaccessible", "inaccessible"),
        ("unsupported", "unsupported"),
        # A changed source parent or a missing logical parent: the chain is not current.
        ("stale_source_parent", "inaccessible"),
        ("missing_logical_parent", "inaccessible"),
    ],
)
def test_unavailable_files_lose_the_download_url(
    media_catalog: MediaCatalog, mutation: str, state: str,
) -> None:
    folder = media_catalog.entry(b"albums", kind="directory")
    logical = media_catalog.entry(b"collection", kind="directory")
    entry = media_catalog.entry(b"notes.txt", parent=folder, logical_parent=logical)
    issued = _download_url(media_catalog, entry)
    assert media_catalog.get(issued).status_code == 200
    if mutation == "stale_source_parent":
        CatalogEntry.objects.filter(pk=folder.pk).update(source_revision=F("source_revision") + 1)
    elif mutation == "missing_logical_parent":
        CatalogEntry.objects.filter(pk=logical.pk).update(source_state="missing")
    else:
        CatalogEntry.objects.filter(pk=entry.pk).update(source_state=mutation)

    details = _details(media_catalog, entry)

    assert details["sourceState"] == state
    assert details["downloadUrl"] is None
    _assert_not_found(media_catalog.get(issued))


def test_roots_without_a_grant_never_issue_or_serve_downloads(
    media_catalog: MediaCatalog, second_root: SecondRoot,
) -> None:
    # A root in the manifest, granted only to its owner.
    owner = second_root.owner_client
    owner_url = _download_url(media_catalog, second_root.entry, owner)
    assert owner_url is not None
    assert media_catalog.get(owner_url, client=owner).status_code == 200
    own_namespace = owner_url.replace(
        f"&n={media_catalog.namespace(owner)}&", f"&n={media_catalog.namespace()}&",
    )
    for path in (f"/api/v1/entries/{second_root.entry.pk}", owner_url, own_namespace):
        response = media_catalog.get(path)
        assert response.status_code == 404
        assert b"/media/v1/" not in response.content
        assert b"downloadUrl" not in response.content
    # A root outside the manifest, without any grant.
    foreign = CatalogEntry.objects.create(
        root=other_root(), raw_name=b"", display_name="", name_key=b"", kind="directory",
    )
    assert media_catalog.get(f"/api/v1/entries/{foreign.pk}").status_code == 404
    _assert_not_found(media_catalog.get(_attachment(media_catalog, foreign)))
    # A grant that lost every permission.
    entry = media_catalog.entry(b"notes.txt")
    issued = _download_url(media_catalog, entry)
    media_catalog.grant(Permission(0))
    assert media_catalog.get(f"/api/v1/entries/{entry.pk}").status_code == 404
    _assert_not_found(media_catalog.get(issued))


@pytest.mark.parametrize(
    "asset_state",
    [
        {},
        # Download never depends on media processing: an unprocessable file still downloads.
        {"probe_state": "unsupported", "thumbnail_state": "unsupported",
         "width": None, "height": None},
        {"probe_state": "quarantined", "thumbnail_state": "quarantined",
         "safe_error_code": "timeout", "width": None, "height": None},
    ],
)
def test_media_entries_issue_one_download_url_in_both_details(
    media_catalog: MediaCatalog, asset_state: dict[str, object],
) -> None:
    entry = media_catalog.entry(b"IMG_0002.jpg")
    media_catalog.asset(entry, **asset_state)

    catalog_url = _download_url(media_catalog, entry)
    media = media_catalog.get(f"/api/v1/entries/{entry.pk}/media").json()

    assert catalog_url == _attachment(media_catalog, entry)
    assert media["original"]["downloadUrl"] == catalog_url
    assert media_catalog.get(catalog_url).status_code == 200


def test_resumes_are_authorized_per_request_and_end_with_the_session(
    media_catalog: MediaCatalog,
) -> None:
    entry = media_catalog.entry(b"large.bin")
    url = _download_url(media_catalog, entry)
    first = media_catalog.get(url)
    assert first.status_code == 200

    # Within the session a resume is answered like the first request: web authorizes it
    # and redirects; the gateway alone answers the range and its validator.
    resumed = media_catalog.get(url, headers=RESUME_HEADERS)
    assert resumed.status_code == 200
    assert resumed.content == b""
    assert resumed.headers["X-Accel-Redirect"] == first.headers["X-Accel-Redirect"]
    assert "Content-Range" not in resumed.headers
    assert "ETag" not in resumed.headers

    # After logout the URL is refused, for a resume as for a fresh request.
    assert media_catalog.post("/api/v1/auth/logout").status_code == 204
    _assert_not_found(media_catalog.get(url, headers=RESUME_HEADERS))
    _assert_not_found(media_catalog.get(url))

    # The next session of the same user gets a new URL; the old one stays refused.
    client = _login(media_catalog)
    renewed = _download_url(media_catalog, entry, client)
    assert renewed is not None and renewed != url
    _assert_not_found(media_catalog.get(url, client=client, headers=RESUME_HEADERS))
    assert media_catalog.get(renewed, client=client, headers=RESUME_HEADERS).status_code == 200


def test_unknown_entries_and_details_of_other_users_never_leak_a_url(
    media_catalog: MediaCatalog,
) -> None:
    entry = media_catalog.entry(b"notes.txt")
    issued = _download_url(media_catalog, entry)
    unknown = issued.replace(str(entry.pk), str(uuid.uuid4()))
    # The group user can browse the root but has no EXPORT, and another session's
    # namespace: neither the details nor the stolen URL give it the file.
    group = media_catalog.api.group_client

    _assert_not_found(media_catalog.get(unknown))
    assert _download_url(media_catalog, entry, group) is None
    _assert_not_found(media_catalog.get(issued, client=group))
