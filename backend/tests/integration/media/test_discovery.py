"""Discovery: idempotent upserts, the fingerprint-change transaction, parking and unparking."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.media.discovery import (
    Discovery,
    active_roots,
    discover_batch,
    park_batch,
    rediscover_entry,
    reset_asset,
)
from aegis_apps.media.enums import DerivativeState, Lane, SortSource, StageState
from aegis_apps.media.keys import derivative_key, params_digest
from aegis_apps.media.leasing import PARKED_UNTIL, claim_assets
from aegis_apps.media.models import MediaAsset, MediaDerivative
from aegis_apps.roots.manifest import configured_manifest
from django.utils import timezone

from .worker_support import MTIME_NS, Library, make_library

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def library(browse_fixture: Any) -> Library:
    return make_library(browse_fixture)


def _pass(library: Library, *, batch: int = 500) -> Discovery:
    discovery = Discovery(batch=batch)
    discovery.start([library.root.pk])
    for _ in range(1_000):
        if not discovery.active:
            break
        discovery.step(now=timezone.now())
    assert not discovery.active
    return discovery


def _processed(entry: CatalogEntry, *, video: bool = False) -> MediaAsset:
    """Mark an entry's asset as fully processed, with metadata, a lease and failures."""
    asset = MediaAsset.objects.get(pk=entry.pk)
    captured = datetime(2024, 5, 1, 10, 30, tzinfo=UTC)
    MediaAsset.objects.filter(pk=entry.pk).update(
        probe_state=StageState.READY, thumbnail_state=StageState.READY,
        poster_state=StageState.READY if video else None, attempts=2, attempt_token=7,
        environment_failures=5,
        lease_owner=uuid.uuid4(), lease_expires_at=timezone.now() + timedelta(minutes=5),
        lane=Lane.INTERACTIVE, processor_version="aegis-media-processor/1",
        safe_error_code="decode_failed", width=4032, height=3024, orientation=6,
        captured_at=captured, captured_at_source="exif", camera_make="Synthetic",
        camera_model="Model One", has_location=True, sort_at=captured,
        sort_source=SortSource.CAPTURED,
    )
    for kind in ("thumbnail", "preview"):
        digest = params_digest({"op": kind})
        MediaDerivative.objects.create(
            asset_id=entry.pk, kind=kind, fingerprint=asset.fingerprint, params_digest=digest,
            key=derivative_key(asset.fingerprint, "aegis-media-processor/1", kind, digest),
            extension="webp", bytes=100, width=400, height=300, state=DerivativeState.READY,
            evictable=kind == "preview",
        )
    return MediaAsset.objects.get(pk=entry.pk)


def test_active_roots_are_the_manifest_roots_by_slot(library: Library) -> None:
    assert active_roots(configured_manifest()) == {library.root.pk: library.root.slot_id}
    assert active_roots(None) == {}


def test_discovery_creates_one_asset_per_present_media_file(library: Library) -> None:
    photo = library.photo()
    video = library.video()
    library.file(b"notes.txt")
    library.file(b"noext")
    library.photo(state="missing")
    library.photo(state="inaccessible")
    library.photo(kind="symlink")
    library.photo(device=None)
    nested = library.factory(root=library.root, raw=b"nested", parent=library.album,
                             kind="directory")
    deep = library.photo(parent=nested)

    discovery = _pass(library)

    assets = {asset.pk: asset for asset in MediaAsset.objects.all()}
    assert set(assets) == {photo.pk, video.pk, deep.pk}
    assert discovery.totals.created == 3
    photo_asset, video_asset = assets[photo.pk], assets[video.pk]
    assert (photo_asset.kind, photo_asset.poster_state) == ("photo", None)
    assert (video_asset.kind, video_asset.poster_state) == ("video", StageState.PENDING)
    for asset, entry in ((photo_asset, photo), (video_asset, video)):
        assert asset.root_id == library.root.pk
        assert asset.fingerprint == library.fingerprint(entry)
        assert (asset.probe_state, asset.thumbnail_state) == (StageState.PENDING,) * 2
        assert asset.sort_source == SortSource.MODIFIED
        assert asset.sort_at == datetime(2024, 5, 1, 10, 30, tzinfo=UTC)
        assert asset.captured_at is None
        assert (asset.attempts, asset.attempt_token, asset.lease_owner) == (0, 0, None)
        assert asset.next_attempt_at <= timezone.now()


def test_discovery_is_idempotent(library: Library) -> None:
    for _ in range(3):
        library.photo()
    library.video()
    _pass(library)
    before = list(MediaAsset.objects.order_by("pk").values())
    second = _pass(library)
    assert (second.totals.created, second.totals.reset, second.totals.parked) == (0, 0, 0)
    assert list(MediaAsset.objects.order_by("pk").values()) == before


def test_keyset_batches_cover_every_file_once(library: Library) -> None:
    entries = {library.photo().pk for _ in range(5)}
    seen: list[uuid.UUID] = []
    cursor = None
    for _ in range(10):
        cursor, _ = discover_batch(library.root.pk, cursor, limit=2, now=timezone.now())
        seen = list(MediaAsset.objects.values_list("pk", flat=True))
        if cursor is None:
            break
    assert cursor is None
    assert set(seen) == entries
    assert MediaAsset.objects.count() == 5


def test_a_fingerprint_change_resets_the_asset_and_stales_its_derivatives(
    library: Library,
) -> None:
    entry = library.photo()
    _pass(library)
    before = _processed(entry)
    CatalogEntry.objects.filter(pk=entry.pk).update(mtime_ns=MTIME_NS + 1, size=8192)

    discovery = _pass(library)

    asset = MediaAsset.objects.get(pk=entry.pk)
    assert discovery.totals.reset == 1
    assert asset.fingerprint == library.fingerprint(entry) != before.fingerprint
    assert (int(asset.size), int(asset.mtime_ns)) == (8192, MTIME_NS + 1)
    assert (asset.probe_state, asset.thumbnail_state, asset.poster_state) == (
        StageState.PENDING, StageState.PENDING, None,
    )
    assert (asset.attempts, asset.safe_error_code, asset.processor_version) == (0, None, None)
    assert asset.environment_failures == 0
    assert (asset.lease_owner, asset.lease_expires_at) == (None, None)
    assert asset.attempt_token == before.attempt_token + 1
    assert asset.lane == Lane.BACKGROUND
    assert (asset.width, asset.height, asset.orientation, asset.camera_make) == (None,) * 4
    assert (asset.captured_at, asset.has_location) == (None, False)
    assert asset.sort_source == SortSource.MODIFIED
    assert asset.next_attempt_at <= timezone.now()
    states = set(MediaDerivative.objects.filter(asset_id=entry.pk).values_list("state", flat=True))
    assert states == {DerivativeState.STALE}
    # Rows only: the stale derivatives keep their sizes for eviction to account for.
    assert set(MediaDerivative.objects.values_list("bytes", flat=True)) == {100}


def test_the_reset_is_one_transaction(library: Library) -> None:
    entry = library.photo()
    _pass(library)
    before = _processed(entry)
    CatalogEntry.objects.filter(pk=entry.pk).update(mtime_ns=MTIME_NS + 1)
    row = CatalogEntry.objects.filter(pk=entry.pk).values(
        "id", "root_id", "kind", "type_hint", "source_state", "size", "mtime_ns", "device",
        "inode",
    ).get()
    from aegis_apps.media.discovery import catalog_file

    source = catalog_file(row)
    assert source is not None

    class Exploding:
        class objects:
            @staticmethod
            def filter(**_: object) -> Any:
                raise RuntimeError("derivative update failed")

    with (
        patch("aegis_apps.media.discovery.MediaDerivative", Exploding),
        pytest.raises(RuntimeError),
    ):
        reset_asset(entry.pk, now=timezone.now(), source=source)

    after = MediaAsset.objects.get(pk=entry.pk)
    assert after.fingerprint == before.fingerprint
    assert after.attempt_token == before.attempt_token
    assert after.thumbnail_state == StageState.READY
    assert set(MediaDerivative.objects.values_list("state", flat=True)) == {DerivativeState.READY}


def test_a_gone_entry_parks_its_asset_and_keeps_the_catalog_row(library: Library) -> None:
    entry = library.photo()
    kept = library.photo()
    _pass(library)
    before = _processed(entry)
    CatalogEntry.objects.filter(pk=entry.pk).update(source_state="missing")
    catalog_rows = CatalogEntry.objects.count()

    discovery = _pass(library)

    asset = MediaAsset.objects.get(pk=entry.pk)
    assert discovery.totals.parked == 1
    assert asset.next_attempt_at == PARKED_UNTIL
    assert asset.fingerprint == before.fingerprint
    assert (asset.probe_state, asset.thumbnail_state) == (StageState.PENDING,) * 2
    assert (asset.camera_make, asset.captured_at, asset.has_location) == (None, None, False)
    assert asset.attempt_token == before.attempt_token + 1
    assert (asset.lease_owner, asset.attempts, asset.environment_failures) == (None, 0, 0)
    assert set(MediaDerivative.objects.filter(asset_id=entry.pk).values_list(
        "state", flat=True,
    )) == {DerivativeState.STALE}
    assert CatalogEntry.objects.count() == catalog_rows
    assert MediaAsset.objects.get(pk=kept.pk).next_attempt_at < PARKED_UNTIL
    # A parked asset is never leased.
    claimed = claim_assets(uuid.uuid4(), limit=8, root_ids=[library.root.pk],
                           now=timezone.now() + timedelta(days=3650))
    assert entry.pk not in {item.lease.entry_id for item in claimed}
    # Parking again changes nothing.
    assert _pass(library).totals.parked == 0
    assert MediaAsset.objects.get(pk=entry.pk).attempt_token == asset.attempt_token


@pytest.mark.parametrize(
    "change",
    [{"kind": "directory", "size": None}, {"type_hint": "txt"}, {"source_state": "unsupported"}],
)
def test_entries_that_stop_being_media_files_are_parked(
    library: Library, change: dict[str, object],
) -> None:
    entry = library.photo()
    _pass(library)
    CatalogEntry.objects.filter(pk=entry.pk).update(**change)
    park_batch(library.root.pk, None, limit=500, now=timezone.now())
    assert MediaAsset.objects.get(pk=entry.pk).next_attempt_at == PARKED_UNTIL


def test_a_returning_entry_is_unparked_with_its_current_fingerprint(library: Library) -> None:
    entry = library.photo()
    _pass(library)
    CatalogEntry.objects.filter(pk=entry.pk).update(source_state="missing")
    _pass(library)
    CatalogEntry.objects.filter(pk=entry.pk).update(source_state="present", inode=99_999)

    discovery = _pass(library)

    asset = MediaAsset.objects.get(pk=entry.pk)
    assert discovery.totals.reset == 1
    assert asset.next_attempt_at <= timezone.now()
    assert asset.fingerprint == library.fingerprint(entry)
    assert asset.inode == 99_999


def test_rediscovering_one_entry(library: Library) -> None:
    entry = library.photo()
    absent = library.file(b"notes.txt")
    assert rediscover_entry(entry.pk, now=timezone.now()) == "created"
    assert rediscover_entry(entry.pk, now=timezone.now()) == "unchanged"
    assert rediscover_entry(absent.pk, now=timezone.now()) == "absent"
    assert rediscover_entry(uuid.uuid4(), now=timezone.now()) == "absent"
    CatalogEntry.objects.filter(pk=entry.pk).update(mtime_ns=MTIME_NS + 5)
    assert rediscover_entry(entry.pk, now=timezone.now()) == "reset"
    assert MediaAsset.objects.get(pk=entry.pk).fingerprint == library.fingerprint(entry)
    assert rediscover_entry(entry.pk, now=timezone.now()) == "unchanged"
    CatalogEntry.objects.filter(pk=entry.pk).update(source_state="missing")
    assert rediscover_entry(entry.pk, now=timezone.now()) == "parked"
    assert rediscover_entry(entry.pk, now=timezone.now()) == "unchanged"


def test_discovery_passes_are_spaced_by_the_interval(library: Library) -> None:
    now = [100.0]
    discovery = Discovery(batch=500, interval_seconds=300, clock=lambda: now[0])
    assert discovery.due()
    discovery.start([library.root.pk])
    while discovery.active:
        discovery.step(now=timezone.now())
    assert discovery.passes == 1
    assert not discovery.due()
    now[0] += 301
    assert discovery.due()
    discovery.start([])
    assert discovery.passes == 2 and not discovery.active
