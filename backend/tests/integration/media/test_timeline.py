from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.media.links import fingerprint_token
from aegis_apps.media.models import MediaAsset
from aegis_apps.roots.models import Root
from aegis_apps.roots.permissions import Permission
from django.db import connection, transaction
from django.db.models import F
from django.test import Client
from django.test.utils import CaptureQueriesContext

from .conftest import MediaCatalog

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

BASE = datetime(2024, 6, 15, 12, 0, tzinfo=UTC)


def _timeline(media: MediaCatalog, **params: Any) -> Any:
    return media.get(f"/api/v1/roots/{media.api.root.pk}/timeline", data=params)


def _media(
    media: MediaCatalog, name: bytes, sort_at: datetime, *, kind: str = "photo",
    entry_id: uuid.UUID | None = None, parent: CatalogEntry | None = None,
) -> CatalogEntry:
    extra: dict[str, Any] = {} if entry_id is None else {"id": entry_id}
    entry = media.entry(name, parent=parent, **extra)
    media.asset(entry, kind=kind, sort_at=sort_at)
    return entry


def _ids(payload: dict[str, Any]) -> list[str]:
    return [item["id"] for item in payload["items"]]


def test_items_are_newest_first_with_entry_id_tiebreak(media_catalog: MediaCatalog) -> None:
    low = uuid.UUID("00000000-0000-4000-8000-000000000001")
    high = uuid.UUID("ffffffff-ffff-4fff-bfff-ffffffffffff")
    older = _media(media_catalog, b"older.jpg", BASE - timedelta(days=30))
    tie_low = _media(media_catalog, b"tie-low.jpg", BASE, entry_id=low)
    tie_high = _media(media_catalog, b"tie-high.mp4", BASE, kind="video", entry_id=high)
    newest = _media(media_catalog, b"newest.jpg", BASE + timedelta(seconds=1))
    media_catalog.entry(b"notes.txt")

    response = _timeline(media_catalog)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    payload = response.json()
    assert set(payload) == {"items", "nextCursor", "contractVersion"}
    assert payload["contractVersion"] == 1
    assert payload["nextCursor"] is None
    assert _ids(payload) == [str(e.pk) for e in (newest, tie_high, tie_low, older)]


def test_items_are_entry_summaries_with_parent_and_media(media_catalog: MediaCatalog) -> None:
    folder = media_catalog.entry(b"albums", kind="directory")
    entry = _media(media_catalog, b"IMG_0001.jpg", BASE, parent=folder)
    asset = MediaAsset.objects.get(pk=entry.pk)
    media_catalog.derivative(asset)
    namespace = media_catalog.namespace()

    item = _timeline(media_catalog).json()["items"][0]

    assert item == {
        "id": str(entry.pk),
        "rootId": str(media_catalog.api.root.pk),
        "displayName": "IMG_0001.jpg",
        "kind": "file",
        "typeHint": "jpg",
        "size": "4096",
        "modifiedNs": "1700000000000000000",
        "sourceState": "present",
        "version": "0",
        "parentId": str(folder.pk),
        "media": {
            "kind": "photo",
            "thumbnail": "ready",
            "sortDate": "2024-06-15T12:00:00Z",
            "sortDateSource": "modified",
            "width": 4032,
            "height": 3024,
            "durationMs": None,
            "thumbnailUrl": (
                f"/media/v1/derivatives/{entry.pk}/thumbnail"
                f"?f={fingerprint_token(asset.fingerprint)}&n={namespace}&e=0"
            ),
        },
    }


def test_keyset_pages_walk_every_item_once(media_catalog: MediaCatalog) -> None:
    expected = [
        _media(media_catalog, f"IMG_{index:04d}.jpg".encode(), BASE - timedelta(hours=index % 4))
        for index in range(11)
    ]
    ordered = sorted(
        expected,
        key=lambda e: (MediaAsset.objects.get(pk=e.pk).sort_at, e.pk),
        reverse=True,
    )
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params: dict[str, Any] = {"limit": "3"}
        if cursor is not None:
            params["cursor"] = cursor
        payload = _timeline(media_catalog, **params).json()
        assert len(payload["items"]) <= 3
        seen.extend(_ids(payload))
        cursor = payload["nextCursor"]
        if cursor is None:
            break

    assert seen == [str(e.pk) for e in ordered]


def test_kind_and_year_filters(media_catalog: MediaCatalog) -> None:
    photo_2024 = _media(media_catalog, b"a.jpg", datetime(2024, 1, 1, tzinfo=UTC))
    video_2024 = _media(
        media_catalog, b"b.mp4", datetime(2024, 12, 31, 23, 59, tzinfo=UTC), kind="video",
    )
    photo_2023 = _media(media_catalog, b"c.jpg", datetime(2023, 12, 31, 23, 59, tzinfo=UTC))

    assert _ids(_timeline(media_catalog, kind="photo").json()) == [
        str(photo_2024.pk), str(photo_2023.pk),
    ]
    assert _ids(_timeline(media_catalog, kind="video").json()) == [str(video_2024.pk)]
    assert _ids(_timeline(media_catalog, year="2024").json()) == [
        str(video_2024.pk), str(photo_2024.pk),
    ]
    assert _ids(_timeline(media_catalog, year="2023", kind="photo").json()) == [
        str(photo_2023.pk),
    ]
    assert _timeline(media_catalog, year="1999").json()["items"] == []


def test_missing_entries_are_hidden_and_stale_chains_are_inaccessible(
    media_catalog: MediaCatalog,
) -> None:
    folder = media_catalog.entry(b"albums", kind="directory")
    stale = _media(media_catalog, b"stale.jpg", BASE, parent=folder)
    media_catalog.derivative(MediaAsset.objects.get(pk=stale.pk))
    gone = _media(media_catalog, b"gone.jpg", BASE)
    CatalogEntry.objects.filter(pk=gone.pk).update(source_state="missing")
    CatalogEntry.objects.filter(pk=folder.pk).update(source_revision=F("source_revision") + 1)

    items = _timeline(media_catalog).json()["items"]

    assert [item["id"] for item in items] == [str(stale.pk)]
    assert items[0]["sourceState"] == "inaccessible"
    assert items[0]["media"]["thumbnail"] == "ready"
    assert items[0]["media"]["thumbnailUrl"] is None


@pytest.mark.parametrize(
    ("permissions", "status", "urls"),
    [
        (Permission.BROWSE | Permission.PREVIEW, 200, True),
        (Permission.BROWSE, 200, False),
        (Permission.PREVIEW | Permission.EXPORT, 404, False),
    ],
)
def test_timeline_needs_browse_and_urls_need_preview(
    media_catalog: MediaCatalog, permissions: Permission, status: int, urls: bool,
) -> None:
    entry = _media(media_catalog, b"IMG_0002.jpg", BASE)
    media_catalog.derivative(MediaAsset.objects.get(pk=entry.pk))
    media_catalog.grant(permissions)

    response = _timeline(media_catalog)

    assert response.status_code == status
    if status == 200:
        assert (response.json()["items"][0]["media"]["thumbnailUrl"] is not None) is urls
    else:
        assert response.json()["type"] == "catalog_not_found"


def test_unknown_and_foreign_roots_are_404(media_catalog: MediaCatalog) -> None:
    foreign = Root.objects.create(
        slot_id=f"foreign-{uuid.uuid4().hex[:12]}", display_name="Foreign",
        mode=Root.Mode.READ_ONLY, active=True,
    )
    for root_id in (uuid.uuid4(), foreign.pk):
        response = media_catalog.get(f"/api/v1/roots/{root_id}/timeline")
        assert response.status_code == 404
        assert response.json()["type"] == "catalog_not_found"


@pytest.mark.parametrize(
    "change",
    [
        {"limit": "4"},
        {"kind": "photo"},
        {"year": "2024"},
        "root_epoch",
        "namespace",
    ],
)
def test_cursors_are_bound_to_filters_limit_epoch_and_session(
    media_catalog: MediaCatalog, change: Any,
) -> None:
    for index in range(5):
        _media(media_catalog, f"IMG_1{index:03d}.jpg".encode(), BASE - timedelta(days=index))
    cursor = _timeline(media_catalog, limit="3").json()["nextCursor"]
    assert isinstance(cursor, str)
    params: dict[str, Any] = {"limit": "3", "cursor": cursor}
    client = None
    if change == "root_epoch":
        Root.objects.filter(pk=media_catalog.api.root.pk).update(
            authorization_epoch=F("authorization_epoch") + 1,
        )
    elif change == "namespace":
        client = media_catalog.api.group_client
    else:
        params.update(change)

    response = media_catalog.get(
        f"/api/v1/roots/{media_catalog.api.root.pk}/timeline", data=params, client=client,
    )

    assert response.status_code == 409
    assert response.json() == {
        "type": "cursor_restart_required", "title": "Catalog view changed",
    }


@pytest.mark.parametrize(
    "query",
    [
        "limit=0", "limit=121", "limit=01", "limit=ten", "kind=audio", "kind=",
        "year=24", "year=0000", "year=20245", "year=%EF%BC%92%EF%BC%90%EF%BC%92%EF%BC%94",
        "limit=10&limit=20", "sort=name", "parent=x",
    ],
)
def test_bad_timeline_parameters_are_400(media_catalog: MediaCatalog, query: str) -> None:
    response = media_catalog.get(f"/api/v1/roots/{media_catalog.api.root.pk}/timeline?{query}")

    assert response.status_code == 400
    assert response.json()["type"] == "invalid_catalog_query"


def test_garbage_cursors_restart(media_catalog: MediaCatalog) -> None:
    for cursor in ("not-signed", "x" * 8193):
        response = _timeline(media_catalog, cursor=cursor)
        assert response.status_code == 409
        assert response.json()["type"] == "cursor_restart_required"


def test_timeline_requires_authentication() -> None:
    response = Client().get(f"/api/v1/roots/{uuid.uuid4()}/timeline")

    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "private, no-store"


def test_timeline_statements_do_not_grow_with_the_page(media_catalog: MediaCatalog) -> None:
    def statements(limit: int) -> list[str]:
        with (
            media_catalog.api.database.as_django_role("aegis_web"),
            CaptureQueriesContext(connection) as captured,
        ):
            response = media_catalog.client.get(
                f"/api/v1/roots/{media_catalog.api.root.pk}/timeline", data={"limit": limit},
            )
        assert response.status_code == 200
        return [query["sql"] for query in captured]

    for index in range(30):
        entry = _media(media_catalog, f"IMG_2{index:03d}.jpg".encode(), BASE)
        media_catalog.derivative(MediaAsset.objects.get(pk=entry.pk))

    small, large = statements(2), statements(30)

    assert len(small) == len(large) <= 18
    data = [
        sql for sql in large
        if sql.lstrip().upper().startswith(("SELECT", "WITH"))
        and "django_session" not in sql
    ]
    assert len(data) <= 8
    assert not any("raw_name" in sql or '"inode"' in sql for sql in data)


def test_deep_pages_seek_the_timeline_index_with_a_row_comparison(
    media_catalog: MediaCatalog,
) -> None:
    for index in range(40):
        _media(media_catalog, f"IMG_4{index:03d}.jpg".encode(), BASE - timedelta(minutes=index))
    cursor = _timeline(media_catalog, limit="5").json()["nextCursor"]
    with (
        media_catalog.api.database.as_django_role("aegis_web"),
        CaptureQueriesContext(connection) as captured,
    ):
        response = media_catalog.client.get(
            f"/api/v1/roots/{media_catalog.api.root.pk}/timeline",
            data={"limit": "5", "cursor": cursor},
        )
    assert response.status_code == 200
    statement = next(
        query["sql"] for query in captured if query["sql"].startswith("WITH candidates")
    )
    assert '("media_mediaasset"."sort_at", "media_mediaasset"."entry_id") <' in statement

    with transaction.atomic(), connection.cursor() as cursor_:
        # A small fixture may prefer a seqscan; this proves the index can seek to the
        # boundary (the row comparison is an index condition, not a filter).
        cursor_.execute("ANALYZE media_mediaasset")
        cursor_.execute("ANALYZE catalog_catalogentry")
        cursor_.execute("SET LOCAL enable_seqscan = off")
        cursor_.execute("EXPLAIN (FORMAT JSON) " + statement)
        document = cursor_.fetchone()[0]
    nodes = []
    pending = [document[0]["Plan"]]
    while pending:
        node = pending.pop()
        nodes.append(node)
        pending.extend(node.get("Plans", []))
    seeks = [
        node for node in nodes
        if node.get("Index Name") == "media_asset_timeline_idx"
        and "ROW(" in node.get("Index Cond", "")
    ]
    assert seeks, nodes
