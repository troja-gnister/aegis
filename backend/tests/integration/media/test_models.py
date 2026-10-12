from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import timedelta

import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.media.keys import derivative_key, fingerprint, params_digest
from aegis_apps.media.models import (
    ASSET_NEEDS_WORK,
    MAX_ASSET_ATTEMPTS,
    MAX_DERIVATIVE_BYTES,
    MAX_DURATION_MS,
    MAX_MEDIA_DIMENSION,
    MediaAsset,
    MediaDerivative,
)
from aegis_apps.roots.models import Root
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

EntryFactory = Callable[..., CatalogEntry]
NOW = timezone.now()
VIDEO = {"kind": "video", "poster_state": "pending"}


def _file(
    root: Root, entry_factory: EntryFactory, name: bytes = b"IMG_0001.jpg"
) -> CatalogEntry:
    return entry_factory(
        root=root, raw=name, size=4032, mtime_ns=1_700_000_000_000_000_000, device=7, inode=11
    )


def _asset_values(entry: CatalogEntry, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "entry": entry,
        "root_id": entry.root_id,
        "kind": "photo",
        "device": 7,
        "inode": 11,
        "size": 4032,
        "mtime_ns": 1_700_000_000_000_000_000,
        "fingerprint": fingerprint(
            entry.root_id, entry.id, 7, 11, 4032, 1_700_000_000_000_000_000
        ),
        "sort_at": NOW,
    }
    values.update(overrides)
    return values


def _asset(entry: CatalogEntry, **overrides: object) -> MediaAsset:
    return MediaAsset.objects.create(**_asset_values(entry, **overrides))


def _derivative_values(asset: MediaAsset, **overrides: object) -> dict[str, object]:
    digest = params_digest({"format": "webp", "maxEdge": 400, "quality": 75})
    values: dict[str, object] = {
        "asset": asset,
        "kind": "thumbnail",
        "fingerprint": asset.fingerprint,
        "params_digest": digest,
        "key": derivative_key(asset.fingerprint, "1", "thumbnail", digest),
        "extension": "webp",
        "bytes": 1234,
        "width": 400,
        "height": 300,
        "state": "ready",
        "evictable": False,
    }
    values.update(overrides)
    return values


def _derivative(asset: MediaAsset, **overrides: object) -> MediaDerivative:
    return MediaDerivative.objects.create(**_derivative_values(asset, **overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        VIDEO,
        {
            **VIDEO,
            "probe_state": "ready",
            "thumbnail_state": "ready",
            "poster_state": "ready",
            "width": 1920,
            "height": 1080,
            "orientation": 6,
            "duration_ms": 12_345,
            "video_codec": "avc1.640028",
            "audio_codec": "mp4a.40.2",
            "container": "mov,mp4,m4a,3gp,3g2,mj2",
            "processor_version": "aegis-media-processor/1",
        },
        {"processor_version": "2026.10.1+build.7"},
        {
            "probe_state": "ready",
            "thumbnail_state": "ready",
            "captured_at": NOW - timedelta(days=400),
            "captured_at_source": "exif",
            "sort_at": NOW - timedelta(days=400),
            "sort_source": "captured",
            "camera_make": "NIKON CORPORATION",
            "camera_model": "Über Cam 7",
            "has_location": True,
        },
        {"captured_at": NOW, "captured_at_source": "container"},
        {"probe_state": "failed", "safe_error_code": "decode_failed", "attempts": 1,
         "attempt_token": 1, "next_attempt_at": NOW + timedelta(minutes=5)},
        {"probe_state": "quarantined", "thumbnail_state": "quarantined",
         "safe_error_code": "timeout", "attempts": 3, "attempt_token": 4},
        {"probe_state": "unsupported", "thumbnail_state": "unsupported"},
        {**VIDEO, "probe_state": "unsupported", "thumbnail_state": "unsupported",
         "poster_state": "unsupported", "safe_error_code": "unsupported_format"},
        {**VIDEO, "probe_state": "ready", "thumbnail_state": "ready",
         "poster_state": "quarantined", "safe_error_code": "memory_limit"},
        {"lane": "interactive", "attempt_token": 2, "lease_owner": uuid.uuid4(),
         "lease_expires_at": NOW + timedelta(seconds=30)},
        {"attempts": MAX_ASSET_ATTEMPTS, "attempt_token": MAX_ASSET_ATTEMPTS},
        {"device": (1 << 64) - 1, "inode": (1 << 64) - 1, "size": 0, "mtime_ns": -1},
        {"width": MAX_MEDIA_DIMENSION, "height": 1},
        {**VIDEO, "duration_ms": MAX_DURATION_MS},
    ],
)
def test_valid_assets_are_accepted(
    catalog_root: Root, entry_factory: EntryFactory, overrides: dict[str, object]
) -> None:
    asset = _asset(_file(catalog_root, entry_factory), **overrides)
    asset.refresh_from_db()
    assert asset.pk == asset.entry_id


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "audio"},
        {"probe_state": "done"},
        {"thumbnail_state": "unknown"},
        {**VIDEO, "poster_state": "unknown"},
        {"lane": "urgent"},
        {"sort_source": "guessed"},
        {"captured_at": NOW, "captured_at_source": "filename"},
        {"probe_state": "failed", "safe_error_code": "boom"},
        {"fingerprint": "A" * 64},
        {"fingerprint": "a" * 63},
        {"fingerprint": "g" * 64},
        {"device": -1},
        {"inode": 1 << 64},
        {"size": 1 << 64},
        {"poster_state": "pending"},
        {"kind": "video"},
        {"duration_ms": 1000},
        {"video_codec": "h264"},
        {"audio_codec": "aac"},
        {"container": "mov"},
        {"thumbnail_state": "ready"},
        {**VIDEO, "poster_state": "ready"},
        {**VIDEO, "probe_state": "failed", "poster_state": "ready", "safe_error_code": "timeout"},
        {"probe_state": "failed"},
        {"probe_state": "ready", "thumbnail_state": "quarantined"},
        {**VIDEO, "probe_state": "ready", "poster_state": "failed"},
        {"probe_state": "unsupported"},
        {"probe_state": "quarantined", "safe_error_code": "timeout"},
        {**VIDEO, "probe_state": "unsupported", "thumbnail_state": "unsupported"},
        {**VIDEO, "probe_state": "quarantined", "thumbnail_state": "quarantined",
         "poster_state": "failed", "safe_error_code": "timeout"},
        {"attempts": MAX_ASSET_ATTEMPTS + 1, "attempt_token": MAX_ASSET_ATTEMPTS + 1},
        {"attempts": 2, "attempt_token": 1},
        {"lease_owner": uuid.uuid4()},
        {"lease_expires_at": NOW},
        {"processor_version": "bad version"},
        {"processor_version": "-1"},
        {"processor_version": "/1"},
        {"processor_version": ""},
        {"width": 0, "height": 10},
        {"width": 10},
        {"height": 10},
        {"width": MAX_MEDIA_DIMENSION + 1, "height": 1},
        {"orientation": 0},
        {"orientation": 9},
        {"captured_at": NOW},
        {"captured_at_source": "exif"},
        {"camera_make": ""},
        {"camera_make": " Canon"},
        {"camera_make": "Canon "},
        {"camera_model": "EOS\x01R5"},
        {"camera_model": "EOS\nR5"},
        {**VIDEO, "video_codec": "h264;rm"},
        {**VIDEO, "video_codec": ".h264"},
        {**VIDEO, "audio_codec": "aac/x"},
        {**VIDEO, "container": "mov mp4"},
        {**VIDEO, "duration_ms": MAX_DURATION_MS + 1},
        {"sort_source": "captured"},
        {
            "sort_source": "captured",
            "captured_at": NOW - timedelta(hours=1),
            "captured_at_source": "exif",
        },
    ],
)
def test_database_rejects_invalid_asset_shapes(
    catalog_root: Root, entry_factory: EntryFactory, overrides: dict[str, object]
) -> None:
    entry = _file(catalog_root, entry_factory)
    with pytest.raises(IntegrityError), transaction.atomic():
        _asset(entry, **overrides)
    assert not MediaAsset.objects.filter(pk=entry.pk).exists()


def test_asset_is_one_per_entry(catalog_root: Root, entry_factory: EntryFactory) -> None:
    first = _file(catalog_root, entry_factory)
    _asset(first)

    with pytest.raises(IntegrityError), transaction.atomic():
        MediaAsset.objects.bulk_create([MediaAsset(**_asset_values(first))])
    assert MediaAsset.objects.filter(pk=first.pk).count() == 1


def test_work_predicate_selects_exactly_pending_and_failed_stages(
    catalog_root: Root, entry_factory: EntryFactory
) -> None:
    cases: list[tuple[dict[str, object], bool]] = [
        ({}, True),
        ({"probe_state": "failed", "safe_error_code": "decode_failed"}, True),
        ({"probe_state": "ready", "thumbnail_state": "failed",
          "safe_error_code": "timeout"}, True),
        ({**VIDEO, "probe_state": "ready", "thumbnail_state": "ready"}, True),
        ({"probe_state": "ready", "thumbnail_state": "ready"}, False),
        ({"probe_state": "unsupported", "thumbnail_state": "unsupported"}, False),
        ({"probe_state": "quarantined", "thumbnail_state": "quarantined",
          "safe_error_code": "timeout"}, False),
        ({**VIDEO, "probe_state": "ready", "thumbnail_state": "ready",
          "poster_state": "quarantined", "safe_error_code": "memory_limit"}, False),
        ({**VIDEO, "probe_state": "ready", "thumbnail_state": "ready",
          "poster_state": "ready"}, False),
    ]
    expected: set[object] = set()
    for index, (overrides, needs_work) in enumerate(cases):
        asset = _asset(
            _file(catalog_root, entry_factory, f"IMG_{index:04d}.jpg".encode()), **overrides
        )
        if needs_work:
            expected.add(asset.pk)

    assert set(
        MediaAsset.objects.filter(ASSET_NEEDS_WORK).values_list("pk", flat=True)
    ) == expected


def test_asset_root_must_be_its_entrys_root(
    catalog_root: Root, entry_factory: EntryFactory
) -> None:
    other = Root.objects.create(
        slot_id=f"synthetic-{uuid.uuid4().hex}",
        display_name="Synthetic other",
        mode=Root.Mode.READ_ONLY,
        active=True,
    )
    entry = _file(catalog_root, entry_factory)

    with pytest.raises(IntegrityError), transaction.atomic():
        _asset(entry, root_id=other.pk)
    assert not MediaAsset.objects.filter(pk=entry.pk).exists()


def test_asset_entry_is_protected_and_derivatives_protect_their_asset(
    catalog_root: Root, entry_factory: EntryFactory
) -> None:
    asset = _asset(_file(catalog_root, entry_factory))
    derivative = _derivative(asset)

    assert MediaAsset._meta.get_field("entry").remote_field.on_delete.__name__ == "PROTECT"
    assert MediaAsset._meta.get_field("root").remote_field.on_delete.__name__ == "PROTECT"
    with pytest.raises(ProtectedError):
        asset.delete()
    derivative.delete()
    assert not MediaDerivative.objects.filter(pk=derivative.pk).exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"state": "pending", "bytes": None, "width": None, "height": None},
        {"state": "failed", "bytes": None, "width": None, "height": None},
        {"state": "stale"},
        {"state": "stale", "bytes": None, "width": None, "height": None},
        {"state": "stale", "fingerprint": "0" * 64},
        {"kind": "preview", "evictable": True, "width": 2048, "height": 1536},
        {"kind": "poster", "width": 2048, "height": 1152},
        {"kind": "transcode", "extension": "mp4", "evictable": True,
         "bytes": MAX_DERIVATIVE_BYTES, "width": 1920, "height": 1080},
    ],
)
def test_valid_derivatives_are_accepted(
    catalog_root: Root, entry_factory: EntryFactory, overrides: dict[str, object]
) -> None:
    asset = _asset(_file(catalog_root, entry_factory))
    derivative = _derivative(asset, **overrides)
    derivative.refresh_from_db()
    assert derivative.asset_id == asset.pk


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "original"},
        {"state": "done"},
        {"extension": "jpg"},
        {"kind": "transcode", "evictable": True},
        {"extension": "mp4"},
        {"evictable": True},
        {"kind": "preview"},
        {"kind": "poster", "evictable": True},
        {"key": "A" * 64},
        {"key": "a" * 63},
        {"fingerprint": "A" * 64},
        {"fingerprint": "a" * 63},
        {"fingerprint": "g" * 64},
        {"params_digest": "a" * 63},
        {"params_digest": "z" * 64},
        {"bytes": 0},
        {"bytes": MAX_DERIVATIVE_BYTES + 1},
        {"width": None},
        {"height": None},
        {"width": 0},
        {"height": MAX_MEDIA_DIMENSION + 1},
        {"bytes": None},
        {"state": "pending"},
        {"state": "failed", "bytes": None},
        {"state": "failed", "bytes": None, "width": None},
        {"state": "stale", "width": None},
        {"state": "stale", "height": None},
    ],
)
def test_database_rejects_invalid_derivative_shapes(
    catalog_root: Root, entry_factory: EntryFactory, overrides: dict[str, object]
) -> None:
    asset = _asset(_file(catalog_root, entry_factory))
    with pytest.raises(IntegrityError), transaction.atomic():
        _derivative(asset, **overrides)
    assert not MediaDerivative.objects.filter(asset=asset).exists()


def test_derivative_keys_are_unique_and_one_derivative_per_kind_is_current(
    catalog_root: Root, entry_factory: EntryFactory
) -> None:
    asset = _asset(_file(catalog_root, entry_factory))
    current = _derivative(asset)

    with pytest.raises(IntegrityError), transaction.atomic():
        _derivative(asset, state="stale")
    with pytest.raises(IntegrityError), transaction.atomic():
        _derivative(asset, key="b" * 64)

    MediaDerivative.objects.filter(pk=current.pk).update(state="stale")
    _derivative(asset, key="b" * 64)
    _derivative(asset, key="c" * 64, state="stale")
    _derivative(asset, key="d" * 64, state="stale")
    _derivative(
        asset,
        kind="preview",
        key="e" * 64,
        evictable=True,
        state="pending",
        bytes=None,
        width=None,
        height=None,
    )
    assert MediaDerivative.objects.filter(asset=asset).exclude(state="stale").count() == 2


def test_media_indexes_exist_with_the_planned_shapes() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT indexname, indexdef
              FROM pg_catalog.pg_indexes
             WHERE schemaname = 'public'
               AND tablename IN ('media_mediaasset', 'media_mediaderivative')
            """
        )
        definitions = dict(cursor.fetchall())

    assert definitions["media_asset_timeline_idx"].endswith(
        "USING btree (root_id, sort_at DESC, entry_id DESC)"
    )
    assert definitions["media_asset_timeline_kind_idx"].endswith(
        "USING btree (root_id, kind, sort_at DESC, entry_id DESC)"
    )
    work = definitions["media_asset_work_idx"]
    assert "USING btree (lane, next_attempt_at, entry_id) WHERE" in work
    for column in ("probe_state", "thumbnail_state", "poster_state"):
        assert column in work
    assert "'pending'" in work
    assert "'failed'" in work
    for finished in ("'ready'", "'unsupported'", "'quarantined'"):
        assert finished not in work
    # Newest files first, per root: background assets that need work and are not parked.
    backfill = definitions["media_asset_backfill_idx"]
    assert "USING btree (root_id, mtime_ns DESC, entry_id DESC) WHERE" in backfill
    for term in ("probe_state", "thumbnail_state", "poster_state", "'pending'", "'failed'",
                 "lane", "'background'", "next_attempt_at", "9999-01-01"):
        assert term in backfill
    for finished in ("'ready'", "'unsupported'", "'quarantined'", "'interactive'"):
        assert finished not in backfill
    assert definitions["media_derivative_identity_idx"].endswith(
        "USING btree (asset_id, kind, fingerprint)"
    )
    lru = definitions["media_derivative_lru_idx"]
    assert "USING btree (last_access_at, id) INCLUDE (bytes) WHERE" in lru
    assert "evictable" in lru
    assert "'ready'" in lru
    assert "UNIQUE" in definitions["media_derivative_one_current_per_kind"]
    assert "media_asset_fingerprint_uniq" not in definitions
    for redundant in ("USING btree (root_id)", "USING btree (asset_id)"):
        assert not any(definition.endswith(redundant) for definition in definitions.values())


def test_the_asset_table_is_vacuumed_in_proportion_to_its_churn() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT reloptions FROM pg_catalog.pg_class WHERE oid = 'media_mediaasset'::regclass"
        )
        (options,) = cursor.fetchone()

    assert options == ["autovacuum_vacuum_scale_factor=0.02"]


def test_asset_composite_root_foreign_key_is_installed() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT pg_catalog.pg_get_constraintdef(constraint_row.oid)
              FROM pg_catalog.pg_constraint AS constraint_row
             WHERE constraint_row.conname = 'media_asset_entry_same_root'
            """
        )
        row = cursor.fetchone()

    assert row is not None
    assert row[0].startswith(
        "FOREIGN KEY (root_id, entry_id) REFERENCES catalog_catalogentry(root_id, id)"
    )
