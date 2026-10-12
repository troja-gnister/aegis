"""Recording results: probe metadata, retries and quarantine, publishing, recovery, eviction."""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from aegis_apps.media.discovery import discover_batch
from aegis_apps.media.dispatch import VerifiedOutput
from aegis_apps.media.enums import DerivativeState, Lane, SortSource, StageState
from aegis_apps.media.eviction import evict, evictable_bytes
from aegis_apps.media.leasing import AssetLease, ClaimedAsset, claim_assets, fenced
from aegis_apps.media.models import MediaAsset, MediaDerivative
from aegis_apps.media.pipeline import (
    backfill_ready_update,
    derivative_identities,
    record_failure,
    record_probe,
    record_unsupported,
    stage_ready_update,
)
from aegis_apps.media.publish import (
    DerivativeStore,
    FenceLost,
    Publication,
    PublishFailed,
    record_failed_request,
    record_publications,
)
from aegis_apps.media.recovery import RecoverySweep
from aegis_media_processor import protocol
from django.db.models import Q
from django.utils import timezone

from .worker_support import (
    PHOTO_METADATA,
    VIDEO_METADATA,
    Library,
    make_library,
    synthetic_webp,
)

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def library(browse_fixture: Any) -> Library:
    return make_library(browse_fixture)


@pytest.fixture
def store(tmp_path: Path) -> DerivativeStore:
    root = tmp_path / "derivatives"
    root.mkdir(mode=0o770)
    return DerivativeStore(str(root))


def _claim(
    library: Library, *, video: bool = False, probed: bool = False, **columns: Any,
) -> ClaimedAsset:
    entry = library.video() if video else library.photo()
    discover_batch(library.root.pk, None, limit=500, now=timezone.now())
    if probed:
        columns["probe_state"] = StageState.READY
    if columns:
        MediaAsset.objects.filter(pk=entry.pk).update(**columns)
    claimed = claim_assets(uuid.uuid4(), limit=32, root_ids=[library.root.pk],
                           now=timezone.now())
    return next(item for item in claimed if item.lease.entry_id == entry.pk)


def _asset(lease: AssetLease) -> MediaAsset:
    return MediaAsset.objects.get(pk=lease.entry_id)


def _output(tmp_path: Path, data: bytes, width: int, height: int,
            name: str = "thumbnail") -> VerifiedOutput:
    source = tmp_path / f"output-{uuid.uuid4().hex}"
    source.write_bytes(data)
    return VerifiedOutput(name=name, format="webp", bytes=len(data), width=width, height=height,
                          descriptor=os.open(source, os.O_RDONLY))


def _publications(
    store: DerivativeStore, tmp_path: Path, fingerprint: str, op: protocol.Op,
    *, padding: int = 64,
) -> list[Publication]:
    publications = []
    for identity in derivative_identities(fingerprint, op, protocol.DEFAULT_PARAMS[op]):
        size = (400, 300) if identity.kind == "thumbnail" else (2048, 1536)
        output = _output(tmp_path, synthetic_webp(*size, padding=padding), *size,
                         name=identity.kind)
        staged = store.stage(output, identity.key, identity.extension)
        output.close()
        publications.append(
            Publication(identity.kind, identity.key, identity.params_digest, staged),
        )
    return publications


def _final(store: DerivativeStore, key: str, extension: str = "webp") -> Path:
    return Path(store.root) / key[0:2] / key[2:4] / f"{key}.{extension}"


# -- probe, failure, quarantine -----------------------------------------------------------


def test_a_probe_records_metadata_and_the_wall_clock_capture_time(library: Library) -> None:
    claimed = _claim(library)
    now = timezone.now()
    assert record_probe(claimed.lease, PHOTO_METADATA, mtime_ns=claimed.row["mtime_ns"], now=now)
    asset = _asset(claimed.lease)
    assert asset.probe_state == StageState.READY
    assert asset.thumbnail_state == StageState.PENDING
    assert (asset.width, asset.height, asset.orientation) == (4032, 3024, 6)
    # EXIF keeps the camera's wall clock (10:30 at +02:00 is stored as 10:30Z).
    assert asset.captured_at == datetime(2024, 5, 1, 10, 30, tzinfo=UTC)
    assert asset.sort_at == asset.captured_at
    assert (asset.sort_source, asset.captured_at_source) == (SortSource.CAPTURED, "exif")
    assert (asset.camera_make, asset.camera_model, asset.has_location) == (
        "Synthetic", "Model One", True,
    )
    assert asset.processor_version == protocol.PROCESSOR_VERSION
    assert (asset.attempts, asset.safe_error_code, asset.lease_owner) == (0, None, None)
    assert asset.next_attempt_at == now


def test_a_video_probe_keeps_its_container_time_as_utc(library: Library) -> None:
    claimed = _claim(library, video=True)
    record_probe(claimed.lease, VIDEO_METADATA, mtime_ns=claimed.row["mtime_ns"],
                 now=timezone.now())
    asset = _asset(claimed.lease)
    assert asset.captured_at == datetime(2024, 5, 1, 8, 30, tzinfo=UTC)
    assert (asset.duration_ms, asset.video_codec, asset.container) == (12_345, "h264", "mp4")
    assert asset.poster_state == StageState.PENDING


def test_without_a_capture_time_the_timeline_uses_the_utc_mtime(library: Library) -> None:
    claimed = _claim(library)
    metadata = protocol.Metadata(
        width=10, height=10, orientation=None, captured_at=None, captured_at_source=None,
        camera_make=None, camera_model=None, duration_ms=None, video_codec=None,
        audio_codec=None, container=None, has_location=False,
    )
    record_probe(claimed.lease, metadata, mtime_ns=claimed.row["mtime_ns"], now=timezone.now())
    asset = _asset(claimed.lease)
    assert (asset.captured_at, asset.sort_source) == (None, SortSource.MODIFIED)
    assert asset.sort_at == datetime(2024, 5, 1, 10, 30, tzinfo=UTC)


def test_failures_back_off_then_quarantine_at_three(library: Library) -> None:
    claimed = _claim(library)
    now = timezone.now()
    outcome = record_failure(claimed.lease, "probe", "photo", "decode_failed", attempts=0,
                             now=now, uniform=lambda: 1.0)
    asset = _asset(claimed.lease)
    assert outcome == "failed"
    assert (asset.probe_state, asset.attempts, asset.safe_error_code) == (
        StageState.FAILED, 1, "decode_failed",
    )
    assert asset.next_attempt_at == now + timedelta(seconds=60)
    assert asset.lease_owner is None

    second = claim_assets(uuid.uuid4(), limit=1, root_ids=[library.root.pk],
                          now=now + timedelta(minutes=2))[0]
    record_failure(second.lease, "probe", "photo", "crashed", attempts=1,
                   now=now + timedelta(minutes=2), uniform=lambda: 1.0)
    asset = _asset(claimed.lease)
    assert asset.attempts == 2
    assert asset.next_attempt_at == now + timedelta(minutes=2, seconds=120)

    third = claim_assets(uuid.uuid4(), limit=1, root_ids=[library.root.pk],
                         now=now + timedelta(minutes=10))[0]
    outcome = record_failure(third.lease, "probe", "photo", "timeout", attempts=2,
                             now=now + timedelta(minutes=10))
    asset = _asset(claimed.lease)
    assert outcome == "quarantined"
    assert (asset.probe_state, asset.thumbnail_state, asset.poster_state) == (
        StageState.QUARANTINED, StageState.QUARANTINED, None,
    )
    assert (asset.attempts, asset.safe_error_code) == (3, "timeout")
    # Quarantine is terminal for this fingerprint: no retry loop.
    assert claim_assets(uuid.uuid4(), limit=8, root_ids=[library.root.pk],
                        now=now + timedelta(days=30)) == []


def test_a_poster_quarantine_ends_both_video_frames(library: Library) -> None:
    # Two counted poster failures already, each under its own claim: the third claim
    # carries attempts == 2 and an attempt_token of at least 3.
    claimed = _claim(
        library, video=True, probed=True, attempts=2, attempt_token=2,
        thumbnail_state=StageState.FAILED, poster_state=StageState.FAILED,
        safe_error_code="decode_failed",
    )
    assert (claimed.row["attempts"], claimed.lease.attempt_token) == (2, 3)
    outcome = record_failure(claimed.lease, "poster", "video", "decode_failed",
                             attempts=claimed.row["attempts"], now=timezone.now())
    assert outcome == "quarantined"
    asset = _asset(claimed.lease)
    assert (asset.probe_state, asset.thumbnail_state, asset.poster_state) == (
        StageState.READY, StageState.QUARANTINED, StageState.QUARANTINED,
    )


def test_environment_failures_back_off_per_asset_up_to_an_hour(library: Library) -> None:
    claimed = _claim(library)
    now = timezone.now()
    outcome = record_failure(claimed.lease, "probe", "photo", "source_unavailable",
                             attempts=0, environment_failures=0, now=now, uniform=lambda: 1.0)
    asset = _asset(claimed.lease)
    assert outcome == "deferred"
    assert (asset.probe_state, asset.attempts, asset.environment_failures) == (
        StageState.PENDING, 0, 1,
    )
    assert asset.safe_error_code == "source_unavailable"
    assert asset.next_attempt_at == now + timedelta(seconds=30)

    # Each consecutive environmental failure doubles the wait...
    later = now + timedelta(minutes=1)
    second = claim_assets(uuid.uuid4(), limit=1, root_ids=[library.root.pk], now=later)[0]
    assert second.row["environment_failures"] == 1
    record_failure(second.lease, "probe", "photo", "source_unavailable", attempts=0,
                   environment_failures=1, now=later, uniform=lambda: 1.0)
    asset = _asset(claimed.lease)
    assert asset.environment_failures == 2
    assert asset.next_attempt_at == later + timedelta(seconds=60)

    # ...up to an hour, and never toward quarantine.
    MediaAsset.objects.filter(pk=claimed.lease.entry_id).update(environment_failures=20)
    far = later + timedelta(hours=2)
    third = claim_assets(uuid.uuid4(), limit=1, root_ids=[library.root.pk], now=far)[0]
    record_failure(third.lease, "probe", "photo", "tool_missing", attempts=0,
                   environment_failures=20, now=far, uniform=lambda: 1.0)
    asset = _asset(claimed.lease)
    assert asset.environment_failures == 21
    assert asset.next_attempt_at == far + timedelta(hours=1)
    assert (asset.attempts, asset.probe_state) == (0, StageState.PENDING)

    # A result that is the file's own (here a counted failure) resets the count.
    farther = far + timedelta(hours=2)
    fourth = claim_assets(uuid.uuid4(), limit=1, root_ids=[library.root.pk], now=farther)[0]
    record_failure(fourth.lease, "probe", "photo", "decode_failed", attempts=0,
                   environment_failures=21, now=farther)
    asset = _asset(claimed.lease)
    assert (asset.environment_failures, asset.attempts) == (0, 1)


def test_the_environmental_count_saturates_below_its_bound(library: Library) -> None:
    from aegis_apps.media.models import MAX_ENVIRONMENT_FAILURES

    claimed = _claim(library)
    record_failure(claimed.lease, "probe", "photo", "processor_unavailable", attempts=0,
                   environment_failures=MAX_ENVIRONMENT_FAILURES, now=timezone.now())
    assert _asset(claimed.lease).environment_failures == MAX_ENVIRONMENT_FAILURES


def test_successes_reset_the_environmental_count(library: Library) -> None:
    claimed = _claim(library)
    MediaAsset.objects.filter(pk=claimed.lease.entry_id).update(environment_failures=4)
    record_probe(claimed.lease, PHOTO_METADATA, mtime_ns=claimed.row["mtime_ns"],
                 now=timezone.now())
    assert _asset(claimed.lease).environment_failures == 0


def test_a_lost_lease_records_nothing(library: Library) -> None:
    claimed = _claim(library)
    lost = AssetLease(claimed.lease.entry_id, claimed.lease.attempt_token, uuid.uuid4(),
                      claimed.lease.expires_at)
    assert record_failure(lost, "probe", "photo", "decode_failed", attempts=0,
                          now=timezone.now()) == "lost"
    assert not record_probe(lost, PHOTO_METADATA, mtime_ns=0, now=timezone.now())
    assert not record_unsupported(lost, "probe", "photo", "too_large", now=timezone.now())
    assert _asset(claimed.lease).probe_state == StageState.PENDING


def test_unsupported_probes_end_every_stage(library: Library) -> None:
    claimed = _claim(library, video=True)
    record_unsupported(claimed.lease, "probe", "video", "no_video_stream", now=timezone.now())
    asset = _asset(claimed.lease)
    assert (asset.probe_state, asset.thumbnail_state, asset.poster_state) == (
        StageState.UNSUPPORTED,) * 3
    assert (asset.safe_error_code, asset.attempts) == ("no_video_stream", 0)


def test_a_failed_backfill_fails_only_the_probe_and_its_third_failure_ends_every_stage(
    library: Library,
) -> None:
    claimed = _claim(library, video=True)
    now = timezone.now()
    assert record_failure(claimed.lease, "backfill", "video", "decode_failed", attempts=0,
                          now=now) == "failed"
    asset = _asset(claimed.lease)
    # The next attempt runs the probe on its own (pipeline.next_operation).
    assert (asset.probe_state, asset.thumbnail_state, asset.poster_state) == (
        StageState.FAILED, StageState.PENDING, StageState.PENDING,
    )
    assert (asset.attempts, asset.safe_error_code) == (1, "decode_failed")

    # Not reachable in the pipeline (a counted failure leaves the probe failed), but bounded.
    other = _claim(library, video=True, attempts=2, attempt_token=2)
    assert record_failure(other.lease, "backfill", "video", "timeout",
                          attempts=other.row["attempts"], now=now) == "quarantined"
    assert (_asset(other.lease).probe_state, _asset(other.lease).thumbnail_state,
            _asset(other.lease).poster_state) == (StageState.QUARANTINED,) * 3

    third = _claim(library)
    assert record_failure(third.lease, "backfill", "photo", "source_unavailable", attempts=0,
                          now=now) == "deferred"
    assert (_asset(third.lease).probe_state, _asset(third.lease).environment_failures) == (
        StageState.PENDING, 1,
    )


def test_an_unsupported_backfill_ends_every_stage(library: Library) -> None:
    claimed = _claim(library, video=True)
    record_unsupported(claimed.lease, "backfill", "video", "no_video_stream",
                       now=timezone.now())
    asset = _asset(claimed.lease)
    assert (asset.probe_state, asset.thumbnail_state, asset.poster_state) == (
        StageState.UNSUPPORTED,) * 3
    assert asset.processor_version == protocol.PROCESSOR_VERSION


# -- publishing ---------------------------------------------------------------------------


@pytest.mark.parametrize("video", [False, True])
def test_a_backfill_records_metadata_timeline_key_and_derivatives_together(
    library: Library, store: DerivativeStore, tmp_path: Path, video: bool,
) -> None:
    claimed = _claim(library, video=video)
    fingerprint = claimed.row["fingerprint"]
    kind = "video" if video else "photo"
    rendition: protocol.Op = "poster" if video else "thumbnail"
    now = timezone.now()
    publications = _publications(store, tmp_path, fingerprint, rendition)
    record_publications(
        store, entry_id=claimed.lease.entry_id, fingerprint=fingerprint,
        fence=fenced(claimed.lease, now=now), publications=publications,
        asset_update=backfill_ready_update(
            VIDEO_METADATA if video else PHOTO_METADATA, kind,
            mtime_ns=claimed.row["mtime_ns"], now=now,
        ),
        now=now,
    )
    asset = _asset(claimed.lease)
    assert asset.probe_state == asset.thumbnail_state == StageState.READY
    assert asset.poster_state == (StageState.READY if video else None)
    assert asset.processor_version == protocol.PROCESSOR_VERSION
    assert asset.sort_source == SortSource.CAPTURED
    assert asset.sort_at == asset.captured_at
    assert (asset.width, asset.height) == ((1920, 1080) if video else (4032, 3024))
    assert (asset.lease_owner, asset.attempts, asset.safe_error_code, asset.lane) == (
        None, 0, None, Lane.BACKGROUND,
    )
    assert asset.next_attempt_at == now
    rows = MediaDerivative.objects.filter(asset_id=claimed.lease.entry_id)
    assert {(row.kind, row.key, row.state) for row in rows} == {
        (item.kind, item.key, DerivativeState.READY) for item in publications
    }
    for row in rows:
        assert _final(store, row.key).stat().st_size == row.bytes


def test_a_backfill_that_loses_its_fence_records_no_metadata_and_no_derivative(
    library: Library, store: DerivativeStore, tmp_path: Path,
) -> None:
    claimed = _claim(library)
    fingerprint = claimed.row["fingerprint"]
    publications = _publications(store, tmp_path, fingerprint, "thumbnail")
    lost = AssetLease(claimed.lease.entry_id, claimed.lease.attempt_token + 1,
                      claimed.lease.owner, claimed.lease.expires_at)
    now = timezone.now()
    with pytest.raises(FenceLost):
        record_publications(
            store, entry_id=lost.entry_id, fingerprint=fingerprint,
            fence=fenced(lost, now=now), publications=publications,
            asset_update=backfill_ready_update(PHOTO_METADATA, "photo",
                                               mtime_ns=claimed.row["mtime_ns"], now=now),
            now=now,
        )
    asset = _asset(claimed.lease)
    assert (asset.probe_state, asset.thumbnail_state) == (StageState.PENDING,) * 2
    assert (asset.width, asset.camera_make, asset.sort_source) == (
        None, None, SortSource.MODIFIED,
    )
    assert not MediaDerivative.objects.exists()
    assert os.listdir(_final(store, publications[0].key).parent) == []


def test_a_backfill_whose_placement_fails_rolls_its_metadata_back(
    library: Library, store: DerivativeStore, tmp_path: Path,
) -> None:
    claimed = _claim(library)
    fingerprint = claimed.row["fingerprint"]
    publications = _publications(store, tmp_path, fingerprint, "thumbnail")
    now = timezone.now()
    with (
        patch.object(DerivativeStore, "place", side_effect=PublishFailed()),
        pytest.raises(PublishFailed),
    ):
        record_publications(
            store, entry_id=claimed.lease.entry_id, fingerprint=fingerprint,
            fence=fenced(claimed.lease, now=now), publications=publications,
            asset_update=backfill_ready_update(PHOTO_METADATA, "photo",
                                               mtime_ns=claimed.row["mtime_ns"], now=now),
            now=now,
        )
    asset = _asset(claimed.lease)
    assert (asset.probe_state, asset.width, asset.captured_at) == (
        StageState.PENDING, None, None,
    )
    assert not MediaDerivative.objects.exists()


def test_publishing_places_files_then_rows_then_updates_the_asset(
    library: Library, store: DerivativeStore, tmp_path: Path,
) -> None:
    claimed = _claim(library, video=True, probed=True)
    fingerprint = claimed.row["fingerprint"]
    now = timezone.now()
    publications = _publications(store, tmp_path, fingerprint, "poster")
    record_publications(
        store, entry_id=claimed.lease.entry_id, fingerprint=fingerprint,
        fence=fenced(claimed.lease, now=now), publications=publications,
        asset_update=stage_ready_update("poster", "video", now=now), now=now,
    )
    rows = {
        row.kind: row
        for row in MediaDerivative.objects.filter(asset_id=claimed.lease.entry_id)
    }
    assert set(rows) == {"thumbnail", "poster"}
    for publication in publications:
        row = rows[publication.kind]
        assert (row.state, row.fingerprint, row.key) == (
            DerivativeState.READY, fingerprint, publication.key,
        )
        assert row.evictable is False
        assert row.bytes == _final(store, row.key).stat().st_size
        assert not (_final(store, row.key).parent / publication.staged.temporary).exists()
    asset = _asset(claimed.lease)
    assert (asset.thumbnail_state, asset.poster_state) == (StageState.READY,) * 2
    assert (asset.lease_owner, asset.attempts, asset.lane) == (None, 0, Lane.BACKGROUND)


def test_a_lost_fence_places_nothing_and_records_nothing(
    library: Library, store: DerivativeStore, tmp_path: Path,
) -> None:
    claimed = _claim(library, probed=True)
    fingerprint = claimed.row["fingerprint"]
    publications = _publications(store, tmp_path, fingerprint, "thumbnail")
    lost = AssetLease(claimed.lease.entry_id, claimed.lease.attempt_token + 1,
                      claimed.lease.owner, claimed.lease.expires_at)
    now = timezone.now()
    with pytest.raises(FenceLost):
        record_publications(
            store, entry_id=lost.entry_id, fingerprint=fingerprint,
            fence=fenced(lost, now=now), publications=publications,
            asset_update=stage_ready_update("thumbnail", "photo", now=now), now=now,
        )
    key = publications[0].key
    assert not _final(store, key).exists()
    assert os.listdir(_final(store, key).parent) == []
    assert not MediaDerivative.objects.exists()
    assert _asset(claimed.lease).thumbnail_state == StageState.PENDING


def test_a_crash_after_placement_leaves_only_an_orphan_file_for_recovery(
    library: Library, store: DerivativeStore, tmp_path: Path,
) -> None:
    claimed = _claim(library, probed=True)
    fingerprint = claimed.row["fingerprint"]
    publications = _publications(store, tmp_path, fingerprint, "thumbnail")
    key = publications[0].key
    now = timezone.now()
    with (
        patch("aegis_apps.media.publish._record", side_effect=_place_then_fail(store)),
        pytest.raises(RuntimeError),
    ):
        record_publications(
            store, entry_id=claimed.lease.entry_id, fingerprint=fingerprint,
            fence=fenced(claimed.lease, now=now), publications=publications,
            asset_update=stage_ready_update("thumbnail", "photo", now=now), now=now,
        )
    assert _final(store, key).exists()
    assert not MediaDerivative.objects.exists()
    assert _asset(claimed.lease).thumbnail_state == StageState.PENDING

    sweep = RecoverySweep(store, grace_seconds=0, wall_clock=lambda: time.time() + 1)
    sweep.start()
    while sweep.active:
        sweep.step(now=timezone.now())
    assert not _final(store, key).exists()
    assert sweep.totals.files_removed == 1


def _place_then_fail(store: DerivativeStore) -> Any:
    def record(_store: DerivativeStore, entry_id: uuid.UUID, fingerprint: str,
               publication: Publication, *, now: datetime) -> None:
        store.place(publication.staged)
        raise RuntimeError("simulated crash before the row commits")

    return record


def test_republishing_the_same_key_is_idempotent(
    library: Library, store: DerivativeStore, tmp_path: Path,
) -> None:
    claimed = _claim(library, probed=True)
    fingerprint = claimed.row["fingerprint"]
    entry_id = claimed.lease.entry_id
    now = timezone.now()
    for padding in (64, 128):
        record_publications(
            store, entry_id=entry_id, fingerprint=fingerprint, fence=Q(entry_id=entry_id),
            publications=_publications(store, tmp_path, fingerprint, "thumbnail",
                                       padding=padding),
            asset_update=None, now=now,
        )
    (row,) = MediaDerivative.objects.filter(asset_id=entry_id)
    # The first file and row stay; the second copy was discarded.
    assert row.bytes == len(synthetic_webp(400, 300, padding=64))
    assert _final(store, row.key).stat().st_size == row.bytes
    assert len(os.listdir(_final(store, row.key).parent)) == 1


def test_reviving_a_stale_key_gives_a_fresh_created_at_and_supersedes_older_rows(
    library: Library, store: DerivativeStore, tmp_path: Path,
) -> None:
    claimed = _claim(library, probed=True)
    fingerprint = claimed.row["fingerprint"]
    entry_id = claimed.lease.entry_id
    (identity,) = derivative_identities(fingerprint, "preview", protocol.PREVIEW_PARAMS)
    old_created = timezone.now() - timedelta(days=3)
    MediaDerivative.objects.create(
        asset_id=entry_id, kind="preview", fingerprint=fingerprint,
        params_digest=identity.params_digest, key=identity.key, extension="webp",
        bytes=10, width=1, height=1, state=DerivativeState.STALE, evictable=True,
    )
    MediaDerivative.objects.filter(key=identity.key).update(created_at=old_created)
    older = MediaDerivative.objects.create(
        asset_id=entry_id, kind="preview", fingerprint=fingerprint,
        params_digest="0" * 64, key="0" * 64, extension="webp", state=DerivativeState.FAILED,
        evictable=True,
    )
    now = timezone.now()
    record_publications(
        store, entry_id=entry_id, fingerprint=fingerprint, fence=Q(entry_id=entry_id),
        publications=_publications(store, tmp_path, fingerprint, "preview"),
        asset_update=None, now=now,
    )
    revived = MediaDerivative.objects.get(key=identity.key)
    assert revived.state == DerivativeState.READY
    assert revived.created_at == now > old_created
    assert (revived.width, revived.height) == (2048, 1536)
    assert MediaDerivative.objects.get(pk=older.pk).state == DerivativeState.STALE


def test_failed_requests_leave_a_failed_row_until_a_success_revives_it(
    library: Library, store: DerivativeStore, tmp_path: Path,
) -> None:
    claimed = _claim(library, probed=True)
    fingerprint = claimed.row["fingerprint"]
    entry_id = claimed.lease.entry_id
    (identity,) = derivative_identities(fingerprint, "preview", protocol.PREVIEW_PARAMS)
    fence = Q(entry_id=entry_id)
    assert record_failed_request(
        entry_id=entry_id, fingerprint=fingerprint, kind="preview", key=identity.key,
        params_digest=identity.params_digest, extension="webp", fence=fence,
    )
    failed = MediaDerivative.objects.get(key=identity.key)
    assert (failed.state, failed.bytes) == (DerivativeState.FAILED, None)
    record_publications(
        store, entry_id=entry_id, fingerprint=fingerprint, fence=fence,
        publications=_publications(store, tmp_path, fingerprint, "preview"),
        asset_update=None, now=timezone.now(),
    )
    assert MediaDerivative.objects.get(key=identity.key).state == DerivativeState.READY
    # A ready derivative is never downgraded by a later failure.
    assert not record_failed_request(
        entry_id=entry_id, fingerprint=fingerprint, kind="preview", key=identity.key,
        params_digest=identity.params_digest, extension="webp", fence=fence,
    )
    # And nothing is recorded against another fingerprint.
    assert not record_failed_request(
        entry_id=entry_id, fingerprint="e" * 64, kind="preview", key=identity.key,
        params_digest=identity.params_digest, extension="webp", fence=fence,
    )


def test_a_publish_error_rolls_back_rows_and_the_asset(
    library: Library, store: DerivativeStore, tmp_path: Path,
) -> None:
    claimed = _claim(library, probed=True)
    fingerprint = claimed.row["fingerprint"]
    publications = _publications(store, tmp_path, fingerprint, "thumbnail")
    now = timezone.now()
    with (
        patch.object(DerivativeStore, "place", side_effect=PublishFailed()),
        pytest.raises(PublishFailed),
    ):
        record_publications(
            store, entry_id=claimed.lease.entry_id, fingerprint=fingerprint,
            fence=fenced(claimed.lease, now=now), publications=publications,
            asset_update=stage_ready_update("thumbnail", "photo", now=now), now=now,
        )
    assert not MediaDerivative.objects.exists()
    assert _asset(claimed.lease).thumbnail_state == StageState.PENDING
    assert os.listdir(_final(store, publications[0].key).parent) == []


# -- recovery -----------------------------------------------------------------------------


def test_recovery_removes_orphans_and_repairs_rows_without_files(
    library: Library, store: DerivativeStore, tmp_path: Path,
) -> None:
    claimed = _claim(library, probed=True)
    fingerprint = claimed.row["fingerprint"]
    entry_id = claimed.lease.entry_id
    now = timezone.now()
    record_publications(
        store, entry_id=entry_id, fingerprint=fingerprint, fence=fenced(claimed.lease, now=now),
        publications=_publications(store, tmp_path, fingerprint, "thumbnail"),
        asset_update=stage_ready_update("thumbnail", "photo", now=now), now=now,
    )
    record_publications(
        store, entry_id=entry_id, fingerprint=fingerprint, fence=Q(entry_id=entry_id),
        publications=_publications(store, tmp_path, fingerprint, "preview"),
        asset_update=None, now=now,
    )
    thumbnail = MediaDerivative.objects.get(asset_id=entry_id, kind="thumbnail")
    preview = MediaDerivative.objects.get(asset_id=entry_id, kind="preview")
    kept_token = _asset(claimed.lease).attempt_token
    # Orphans: a file without a row, a staging copy, and a file whose row is failed.
    orphan_key = "ab" * 32
    orphan = _final(store, orphan_key)
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")
    temporary = orphan.parent / f".{orphan_key}.0011223344556677.tmp"
    temporary.write_bytes(b"partial")
    failed_key = "ab" + "cd" * 31
    MediaDerivative.objects.create(
        asset_id=entry_id, kind="poster", fingerprint=fingerprint, params_digest="1" * 64,
        key=failed_key, extension="webp", state=DerivativeState.STALE, evictable=False,
    )
    MediaDerivative.objects.filter(key=failed_key).update(state=DerivativeState.FAILED)
    failed_file = _final(store, failed_key)
    failed_file.parent.mkdir(parents=True, exist_ok=True)
    failed_file.write_bytes(b"x")
    # Orphan rows: the files of a ready preview and of the current thumbnail vanish.
    _final(store, preview.key).unlink()
    _final(store, thumbnail.key).unlink()

    sweep = RecoverySweep(store, grace_seconds=0, wall_clock=lambda: time.time() + 1)
    sweep.start()
    while sweep.active:
        sweep.step(now=timezone.now())

    assert not orphan.exists() and not temporary.exists() and not failed_file.exists()
    assert not MediaDerivative.objects.filter(pk=preview.pk).exists()
    assert MediaDerivative.objects.get(pk=thumbnail.pk).state == DerivativeState.STALE
    asset = _asset(claimed.lease)
    assert asset.thumbnail_state == StageState.PENDING
    assert asset.attempt_token == kept_token + 1
    assert asset.next_attempt_at <= timezone.now()
    assert (sweep.totals.files_removed, sweep.totals.rows_deleted,
            sweep.totals.stages_reset) == (3, 1, 1)


def test_a_repair_that_cannot_lock_its_asset_is_rolled_back(
    library: Library, store: DerivativeStore, tmp_path: Path,
) -> None:
    claimed = _claim(library, probed=True)
    fingerprint = claimed.row["fingerprint"]
    now = timezone.now()
    publications = _publications(store, tmp_path, fingerprint, "thumbnail")
    record_publications(
        store, entry_id=claimed.lease.entry_id, fingerprint=fingerprint,
        fence=fenced(claimed.lease, now=now), publications=publications,
        asset_update=stage_ready_update("thumbnail", "photo", now=now), now=now,
    )
    _final(store, publications[0].key).unlink()
    sweep = RecoverySweep(store, grace_seconds=0, wall_clock=lambda: time.time() + 1)
    with patch("aegis_apps.media.recovery._reset_stage", return_value=None):
        sweep.start()
        while sweep.active:
            sweep.step(now=timezone.now())
    # Never half-applied: the row stays current, and the next sweep retries the repair.
    assert MediaDerivative.objects.get(key=publications[0].key).state == DerivativeState.READY
    assert _asset(claimed.lease).thumbnail_state == StageState.READY
    assert sweep.totals.stages_reset == 0


def test_recovery_keeps_recent_files_and_referenced_files(
    library: Library, store: DerivativeStore, tmp_path: Path,
) -> None:
    claimed = _claim(library, probed=True)
    fingerprint = claimed.row["fingerprint"]
    now = timezone.now()
    publications = _publications(store, tmp_path, fingerprint, "thumbnail")
    record_publications(
        store, entry_id=claimed.lease.entry_id, fingerprint=fingerprint,
        fence=fenced(claimed.lease, now=now), publications=publications,
        asset_update=stage_ready_update("thumbnail", "photo", now=now), now=now,
    )
    recent = _final(store, "ab" * 32)
    recent.parent.mkdir(parents=True, exist_ok=True)
    recent.write_bytes(b"in flight")
    sweep = RecoverySweep(store)  # one hour of grace
    sweep.start()
    while sweep.active:
        sweep.step(now=timezone.now())
    assert recent.exists()
    assert _final(store, publications[0].key).exists()
    assert MediaDerivative.objects.get(key=publications[0].key).state == DerivativeState.READY


# -- eviction -----------------------------------------------------------------------------


def _row(entry_id: uuid.UUID, fingerprint: str, kind: str, *, state: str, size: int,
         accessed: datetime, created: datetime, store: DerivativeStore) -> MediaDerivative:
    key = uuid.uuid4().hex + uuid.uuid4().hex
    row = MediaDerivative.objects.create(
        asset_id=entry_id, kind=kind, fingerprint=fingerprint, params_digest="2" * 64,
        key=key, extension="mp4" if kind == "transcode" else "webp", bytes=size, width=1,
        height=1, state=DerivativeState.STALE, evictable=kind in ("preview", "transcode"),
    )
    MediaDerivative.objects.filter(pk=row.pk).update(
        state=state, last_access_at=accessed, created_at=created,
    )
    path = _final(store, key, row.extension)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * 16)
    row.refresh_from_db()
    return row


def test_eviction_removes_stale_then_least_recently_used_down_to_the_budget(
    library: Library, store: DerivativeStore,
) -> None:
    entries = [library.photo() for _ in range(4)]
    video = library.video()
    discover_batch(library.root.pk, None, limit=500, now=timezone.now())
    fingerprints = {asset.pk: asset.fingerprint for asset in MediaAsset.objects.all()}
    now = timezone.now()
    old_stale = _row(entries[0].pk, fingerprints[entries[0].pk], "thumbnail", state="stale",
                     size=300, accessed=now, created=now - timedelta(days=9), store=store)
    new_stale = _row(entries[1].pk, fingerprints[entries[1].pk], "preview", state="stale",
                     size=300, accessed=now, created=now - timedelta(days=1), store=store)
    lru = _row(entries[2].pk, fingerprints[entries[2].pk], "preview", state="ready", size=400,
               accessed=now - timedelta(days=5), created=now, store=store)
    mru = _row(entries[3].pk, fingerprints[entries[3].pk], "preview", state="ready", size=400,
               accessed=now - timedelta(hours=1), created=now, store=store)
    transcode = _row(video.pk, fingerprints[video.pk], "transcode", state="ready", size=400,
                     accessed=now - timedelta(days=2), created=now, store=store)
    thumbnail = _row(entries[3].pk, fingerprints[entries[3].pk], "thumbnail", state="ready",
                     size=10_000, accessed=now - timedelta(days=99), created=now, store=store)

    assert evictable_bytes() == 300 + 300 + 400 + 400 + 400
    report = evict(store, budget_bytes=1_000)

    remaining = set(MediaDerivative.objects.values_list("pk", flat=True))
    assert report.used_bytes == 1_800
    # 1,800 > 1,000: stale first (oldest first), then least recently used, down to 950.
    assert old_stale.pk not in remaining and new_stale.pk not in remaining
    assert lru.pk not in remaining
    assert {mru.pk, transcode.pk, thumbnail.pk} <= remaining
    assert report.deleted == 3 and report.freed_bytes == 1_000
    for row in (old_stale, new_stale, lru):
        assert not _final(store, row.key, row.extension).exists()
    for row in (mru, transcode, thumbnail):
        assert _final(store, row.key, row.extension).exists()


def test_nothing_is_evicted_within_the_budget_and_thumbnails_never(
    library: Library, store: DerivativeStore,
) -> None:
    entry = library.photo()
    discover_batch(library.root.pk, None, limit=500, now=timezone.now())
    fingerprint = MediaAsset.objects.get(pk=entry.pk).fingerprint
    now = timezone.now()
    thumbnail = _row(entry.pk, fingerprint, "thumbnail", state="ready", size=10**9,
                     accessed=now - timedelta(days=999), created=now, store=store)
    preview = _row(entry.pk, fingerprint, "preview", state="ready", size=100, accessed=now,
                   created=now, store=store)
    assert evict(store, budget_bytes=1_000).deleted == 0
    assert evict(store, budget_bytes=99).deleted == 1
    assert set(MediaDerivative.objects.values_list("pk", flat=True)) == {thumbnail.pk}
    assert not MediaDerivative.objects.filter(pk=preview.pk).exists()
    with pytest.raises(ValueError):
        evict(store, budget_bytes=0)


def test_a_row_touched_after_selection_is_not_evicted(
    library: Library, store: DerivativeStore,
) -> None:
    entry = library.photo()
    discover_batch(library.root.pk, None, limit=500, now=timezone.now())
    fingerprint = MediaAsset.objects.get(pk=entry.pk).fingerprint
    now = timezone.now()
    preview = _row(entry.pk, fingerprint, "preview", state="ready", size=100,
                   accessed=now - timedelta(days=1), created=now, store=store)
    from aegis_apps.media import eviction

    original = eviction._candidates

    def touched_after_selection(limit: int) -> list[dict[str, Any]]:
        selected = original(limit)
        MediaDerivative.objects.filter(pk=preview.pk).update(last_access_at=timezone.now())
        return selected

    with patch.object(eviction, "_candidates", side_effect=touched_after_selection):
        assert evict(store, budget_bytes=10).deleted == 0
    assert MediaDerivative.objects.filter(pk=preview.pk).exists()
    assert _final(store, preview.key).exists()
