"""A second bound root granted only to another user (review M7).

The attacker has every media permission on their own root and knows the victim entry's
ID, the derivative's fingerprint token and the victim root's epoch; only their own
session namespace differs from the owner's. Every surface answers exactly like an
unknown entry or root.
"""

from __future__ import annotations

import uuid

import pytest
from aegis_apps.operations.models import Operation

from .conftest import MediaCatalog, SecondRoot

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def _derivative_path(media: MediaCatalog, second: SecondRoot, entry_id: uuid.UUID) -> str:
    path = media.derivative_path(second.entry, epoch=second.root.authorization_epoch)
    return path.replace(str(second.entry.pk), str(entry_id))


def test_the_second_root_is_bound_and_serves_its_owner(
    media_catalog: MediaCatalog, second_root: SecondRoot,
) -> None:
    client = second_root.owner_client
    namespace = media_catalog.namespace(client)
    derivative = media_catalog.derivative_path(
        second_root.entry, namespace=namespace, epoch=second_root.root.authorization_epoch,
    )

    assert media_catalog.get(derivative, client=client).status_code == 200
    details = media_catalog.get(f"/api/v1/entries/{second_root.entry.pk}/media", client=client)
    assert details.status_code == 200
    timeline = media_catalog.get(f"/api/v1/roots/{second_root.root.pk}/timeline", client=client)
    assert [item["id"] for item in timeline.json()["items"]] == [str(second_root.entry.pk)]
    # The attacker's own root is still bound after the rebinding.
    own_entry = media_catalog.entry(b"own.jpg")
    media_catalog.derivative(media_catalog.asset(own_entry))
    assert media_catalog.get(media_catalog.derivative_path(own_entry)).status_code == 200


def test_victim_derivatives_and_originals_are_unknown_to_the_attacker(
    media_catalog: MediaCatalog, second_root: SecondRoot,
) -> None:
    epoch = second_root.root.authorization_epoch
    victim = _derivative_path(media_catalog, second_root, second_root.entry.pk)
    unknown = _derivative_path(media_catalog, second_root, uuid.uuid4())
    originals = [
        media_catalog.original_path(second_root.entry, disposition, epoch=epoch)
        for disposition in ("inline", "attachment")
    ]

    expected = media_catalog.get(unknown)
    assert expected.status_code == 404
    for path in (victim, *originals):
        response = media_catalog.get(path)
        assert response.status_code == 404
        assert response.content == expected.content
        assert response.headers["Cache-Control"] == expected.headers["Cache-Control"]
        assert "X-Accel-Redirect" not in response.headers
        assert "ETag" not in response.headers


def test_victim_details_requests_and_timeline_are_unknown_to_the_attacker(
    media_catalog: MediaCatalog, second_root: SecondRoot,
) -> None:
    entry_id = second_root.entry.pk
    details = media_catalog.get(f"/api/v1/entries/{entry_id}/media")
    unknown_details = media_catalog.get(f"/api/v1/entries/{uuid.uuid4()}/media")
    request = media_catalog.post(f"/api/v1/entries/{entry_id}/media/requests", {"kind": "preview"})
    unknown_request = media_catalog.post(
        f"/api/v1/entries/{uuid.uuid4()}/media/requests", {"kind": "preview"},
    )
    timeline = media_catalog.get(f"/api/v1/roots/{second_root.root.pk}/timeline")
    unknown_timeline = media_catalog.get(f"/api/v1/roots/{uuid.uuid4()}/timeline")

    for response, expected in (
        (details, unknown_details), (request, unknown_request), (timeline, unknown_timeline),
    ):
        assert response.status_code == expected.status_code == 404
        assert response.content == expected.content
    assert not Operation.objects.exists()
