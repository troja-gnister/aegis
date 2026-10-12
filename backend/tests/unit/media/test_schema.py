from __future__ import annotations

from datetime import UTC, datetime

from aegis_apps.common.database_privileges import MANAGED_TABLE_COLUMNS
from aegis_apps.media.enums import (
    CapturedAtSource,
    DerivativeExtension,
    DerivativeKind,
    DerivativeState,
    Lane,
    MediaKind,
    SortSource,
    StageState,
)
from aegis_apps.media.models import (
    ASSET_NEEDS_WORK,
    BACKFILL_CANDIDATE,
    PARKED_UNTIL,
    MediaAsset,
    MediaDerivative,
)
from django.db import models
from django.db.migrations.loader import MigrationLoader


def _migrated_columns(model_name: str) -> tuple[str, ...]:
    state = MigrationLoader(None, ignore_no_migrations=True).project_state()
    model = state.apps.get_model("media", model_name)
    return tuple(field.column for field in model._meta.local_concrete_fields)


def test_privilege_manifest_matches_the_migrated_column_order() -> None:
    assert _migrated_columns("MediaAsset") == MANAGED_TABLE_COLUMNS["media_mediaasset"]
    assert _migrated_columns("MediaDerivative") == MANAGED_TABLE_COLUMNS[
        "media_mediaderivative"
    ]


def test_contract_enum_values_are_stable() -> None:
    assert MediaKind.values == ["photo", "video"]
    assert StageState.values == ["pending", "ready", "failed", "unsupported", "quarantined"]
    assert Lane.values == ["interactive", "background"]
    assert SortSource.values == ["captured", "modified"]
    assert CapturedAtSource.values == ["exif", "container"]
    assert DerivativeKind.values == ["thumbnail", "preview", "poster", "transcode"]
    assert DerivativeState.values == ["pending", "ready", "failed", "stale"]
    assert DerivativeExtension.values == ["webp", "mp4"]


def _index(model: type[models.Model], name: str) -> models.Index:
    for index in model._meta.indexes:
        if index.name == name:
            assert isinstance(index, models.Index)
            return index
    raise AssertionError(f"missing index {name}")


def test_timeline_and_leasing_indexes_have_the_planned_keys() -> None:
    assert _index(MediaAsset, "media_asset_timeline_idx").fields == [
        "root",
        "-sort_at",
        "-entry",
    ]
    assert _index(MediaAsset, "media_asset_timeline_kind_idx").fields == [
        "root",
        "kind",
        "-sort_at",
        "-entry",
    ]
    work = _index(MediaAsset, "media_asset_work_idx")
    assert work.fields == ["lane", "next_attempt_at", "entry"]
    literal_work_predicate = (
        models.Q(probe_state__in=("pending", "failed"))
        | models.Q(thumbnail_state__in=("pending", "failed"))
        | models.Q(poster_state__in=("pending", "failed"))
    )
    assert work.condition == literal_work_predicate
    assert literal_work_predicate == ASSET_NEEDS_WORK
    backfill = _index(MediaAsset, "media_asset_backfill_idx")
    assert backfill.fields == ["root", "-mtime_ns", "-entry"]
    assert backfill.condition == (
        literal_work_predicate
        & models.Q(lane="background")
        & models.Q(next_attempt_at__lt=PARKED_UNTIL)
    ) == BACKFILL_CANDIDATE
    assert datetime(9999, 1, 1, tzinfo=UTC) == PARKED_UNTIL
    lru = _index(MediaDerivative, "media_derivative_lru_idx")
    assert lru.fields == ["last_access_at", "id"]
    assert tuple(lru.include) == ("bytes",)
    assert lru.condition == models.Q(evictable=True, state="ready")
    identity = _index(MediaDerivative, "media_derivative_identity_idx")
    assert identity.fields == ["asset", "kind", "fingerprint"]
    assert identity.condition is None


def test_asset_is_keyed_by_its_catalog_entry() -> None:
    entry = MediaAsset._meta.get_field("entry")
    assert entry.primary_key
    assert entry.one_to_one
    assert MediaAsset._meta.pk is entry
    root = MediaAsset._meta.get_field("root")
    assert isinstance(root, models.ForeignKey)
    assert root.remote_field.on_delete is models.PROTECT
