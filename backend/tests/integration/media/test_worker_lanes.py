"""The media worker loop against a fake processor: lanes, requests, results and shutdown."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.media import worker as media_worker
from aegis_apps.media.discovery import discover_batch
from aegis_apps.media.enums import DerivativeState, MediaErrorCode, SortSource, StageState
from aegis_apps.media.models import MediaAsset, MediaDerivative
from aegis_apps.media.pipeline import derivative_identities
from aegis_apps.media.worker import MediaWorker
from aegis_apps.operations.enums import JobState, SafeErrorCode
from aegis_apps.operations.models import Job, WorkerHeartbeat
from aegis_media_processor import protocol
from django.utils import timezone

from .worker_support import (
    MTIME_NS,
    WORKER_ID,
    Library,
    drive,
    make_library,
    make_worker,
    publish_result,
)

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def library(browse_fixture: Any) -> Library:
    return make_library(browse_fixture)


@pytest.fixture(autouse=True)
def _clear_shutdown() -> Iterator[None]:
    """The interactive lane reads run_role's process-wide shutdown flag: start each test clear."""
    from aegis_apps.operations.management.commands import run_role

    run_role._reset_shutdown()
    yield
    run_role._reset_shutdown()


def _discover(library: Library) -> None:
    discover_batch(library.root.pk, None, limit=500, now=timezone.now())


def _ready(entry: CatalogEntry, *, video: bool = False) -> None:
    MediaAsset.objects.filter(pk=entry.pk).update(
        probe_state=StageState.READY, thumbnail_state=StageState.READY,
        poster_state=StageState.READY if video else None,
    )


def _job(operation_id: uuid.UUID) -> Job:
    return Job.objects.get(operation_id=operation_id)


def _inbox(worker: MediaWorker) -> list[protocol.Job]:
    inbox = Path(worker.scratch.inbox)
    return [
        protocol.decode_job((inbox / name).read_bytes())
        for name in sorted(os.listdir(inbox))
        if protocol.job_id_from_filename(name) is not None
    ]


# -- the background lane ------------------------------------------------------------------


def test_the_background_pipeline_backfills_each_asset_in_one_job(
    library: Library, tmp_path: Path,
) -> None:
    photo = library.photo()
    video = library.video()
    worker, processor, clock = make_worker(tmp_path)

    def finished() -> bool:
        return MediaAsset.objects.filter(thumbnail_state=StageState.READY).count() == 2

    drive(worker, processor, clock, finished)

    photo_asset = MediaAsset.objects.get(pk=photo.pk)
    video_asset = MediaAsset.objects.get(pk=video.pk)
    assert (photo_asset.probe_state, photo_asset.thumbnail_state) == (StageState.READY,) * 2
    assert (video_asset.probe_state, video_asset.thumbnail_state, video_asset.poster_state) == (
        StageState.READY,) * 3
    # The metadata and the timeline key came with the derivatives, from the one job.
    assert photo_asset.camera_make == "Synthetic"
    assert (photo_asset.sort_source, video_asset.sort_source) == (SortSource.CAPTURED,) * 2
    assert photo_asset.sort_at == photo_asset.captured_at
    assert video_asset.duration_ms == 12_345
    assert (photo_asset.attempts, photo_asset.lease_owner) == (0, None)
    assert sorted((job.op, job.kind) for job in processor.served) == [
        ("backfill", "photo"), ("backfill", "video"),
    ]
    assert {job.lane for job in processor.served} == {"background"}
    kinds = set(MediaDerivative.objects.values_list("asset_id", "kind", "state"))
    assert kinds == {
        (photo.pk, "thumbnail", "ready"), (video.pk, "thumbnail", "ready"),
        (video.pk, "poster", "ready"),
    }
    # The keys are the thumbnail and poster jobs' keys.
    expected = {
        (photo.pk, item.kind): item.key for item in derivative_identities(
            photo_asset.fingerprint, "thumbnail", protocol.THUMBNAIL_PARAMS)
    } | {
        (video.pk, item.kind): item.key for item in derivative_identities(
            video_asset.fingerprint, "poster", protocol.POSTER_PARAMS)
    }
    assert {(row.asset_id, row.kind): row.key for row in MediaDerivative.objects.all()} == expected
    for row in MediaDerivative.objects.all():
        path = Path(worker.store.root, row.key[0:2], row.key[2:4], f"{row.key}.webp")
        assert stat.S_IMODE(path.stat().st_mode) == 0o640
        assert row.fingerprint == MediaAsset.objects.get(pk=row.asset_id).fingerprint
    # Every handled result directory is gone.
    assert os.listdir(worker.scratch.done) == []
    assert worker.dispatches == {}


def test_assets_already_probed_get_their_thumbnail_or_poster_job(
    library: Library, tmp_path: Path,
) -> None:
    photo = library.photo()
    video = library.video()
    _discover(library)
    # Probed by an earlier release: the probes are kept, only the derivatives are built.
    MediaAsset.objects.filter(pk__in=(photo.pk, video.pk)).update(
        probe_state=StageState.READY, camera_make="Earlier",
    )
    worker, processor, clock = make_worker(tmp_path)

    drive(worker, processor, clock,
          lambda: MediaAsset.objects.filter(thumbnail_state=StageState.READY).count() == 2)

    assert sorted(job.op for job in processor.served) == ["poster", "thumbnail"]
    assert set(MediaAsset.objects.values_list("camera_make", flat=True)) == {"Earlier"}
    assert MediaAsset.objects.get(pk=video.pk).poster_state == StageState.READY


def test_a_failed_backfill_falls_back_to_the_separate_stages(
    library: Library, tmp_path: Path,
) -> None:
    entry = library.photo()
    _discover(library)
    worker, processor, clock = make_worker(tmp_path)
    # The header is readable, the pixels are not: the probe works, the thumbnail never does.
    processor.behaviour = lambda job: (
        {} if job.op == "probe" else {"status": "failed", "code": "decode_failed"}
    )

    worker.tick()
    processor.serve()
    worker.tick()
    asset = MediaAsset.objects.get(pk=entry.pk)
    assert (asset.probe_state, asset.thumbnail_state, asset.attempts) == (
        StageState.FAILED, StageState.PENDING, 1,
    )
    for _ in range(10):
        if MediaAsset.objects.get(pk=entry.pk).thumbnail_state == StageState.QUARANTINED:
            break
        # Skip the retry backoff; a leased asset keeps its lease.
        MediaAsset.objects.filter(pk=entry.pk, lease_owner__isnull=True).update(
            next_attempt_at=timezone.now(),
        )
        clock.advance(media_worker.BACKGROUND_RETRY_SECONDS)
        worker.tick()
        processor.serve()
        worker.tick()

    asset = MediaAsset.objects.get(pk=entry.pk)
    assert [job.op for job in processor.served] == [
        "backfill", "probe", "thumbnail", "thumbnail", "thumbnail",
    ]
    # The metadata is kept and only the stage that fails is quarantined.
    assert (asset.probe_state, asset.thumbnail_state, asset.attempts) == (
        StageState.READY, StageState.QUARANTINED, 3,
    )
    assert asset.camera_make == "Synthetic"
    assert not MediaDerivative.objects.exists()


def test_backfill_fills_the_newest_files_first(library: Library, tmp_path: Path) -> None:
    entries = [library.photo(mtime_ns=MTIME_NS + index * 10**9) for index in range(12)]
    _discover(library)
    worker, _, _ = make_worker(tmp_path, environ={"AEGIS_MEDIA_PROCESSOR_CONCURRENCY": "2"})

    worker.tick()

    queued = {job.input.inode for job in _inbox(worker)}
    newest = sorted(entries, key=lambda entry: entry.mtime_ns or 0)[-4:]
    assert queued == {entry.inode for entry in newest}


def test_a_short_discovery_interval_finds_files_indexed_after_a_pass(
    library: Library, tmp_path: Path,
) -> None:
    # A test stack starts the worker before the indexer has catalogued its library: with the
    # production hour between passes, files indexed after the first pass would wait that long.
    worker, _, clock = make_worker(tmp_path, environ={
        "AEGIS_MEDIA_DISCOVERY_INTERVAL_SECONDS": "5",
        "AEGIS_MEDIA_ROOTS_REFRESH_SECONDS": "5",
    })

    def finish_pass() -> None:
        worker.tick()
        for _ in range(20):
            if not worker.discovery.active:
                return
            clock.advance(0.5)
            worker.tick()
        raise AssertionError("the discovery pass did not finish")

    finish_pass()
    assert worker.discovery.next_pass_at == pytest.approx(clock() + 5.0)
    entry = library.photo()
    clock.advance(4.0)
    worker.tick()
    assert not MediaAsset.objects.filter(pk=entry.pk).exists()
    clock.advance(1.0)
    finish_pass()
    assert MediaAsset.objects.filter(pk=entry.pk).exists()


def test_jobs_name_the_file_by_slot_and_raw_relative_path(
    library: Library, tmp_path: Path,
) -> None:
    raw = b"caf\xe9 \xff%.jpg"
    entry = library.photo(raw)
    _discover(library)
    worker, _, _ = make_worker(tmp_path)
    worker.tick()
    (job,) = _inbox(worker)
    assert job.op == "backfill"
    assert job.input.slot == library.root.slot_id
    assert job.input.path == b"album/" + raw
    entry.refresh_from_db()
    expected = (entry.device, entry.inode, entry.size, entry.mtime_ns)
    assert (job.input.device, job.input.inode, job.input.size, job.input.mtime_ns) == tuple(
        int(value) for value in expected if value is not None
    )


@pytest.mark.parametrize(("environ", "background"), [
    ({}, 8), ({"AEGIS_MEDIA_PROCESSOR_CONCURRENCY": "1"}, 2),
])
def test_at_most_twice_the_concurrency_plus_one_jobs_are_in_flight(
    library: Library, tmp_path: Path, environ: dict[str, str], background: int,
) -> None:
    for _ in range(12):
        library.photo()
    _discover(library)
    worker, _, clock = make_worker(tmp_path, environ=environ)
    for _ in range(3):
        worker.tick()
        clock.advance(1.0)
    # One job running and one queued behind it per processor child (four by default); the
    # last slot waits for interactive jobs.
    assert worker.capacity == background + 1
    assert len(_inbox(worker)) == background
    assert len(worker.dispatches) == background


def test_a_short_background_claim_is_not_repeated_on_every_busy_tick(
    library: Library, tmp_path: Path,
) -> None:
    library.photo()
    _discover(library)
    worker, processor, clock = make_worker(tmp_path)
    from aegis_apps.media.leasing import claim_assets

    calls: list[int] = []

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs["limit"])
        return claim_assets(*args, **kwargs)

    with patch("aegis_apps.media.worker.claim_assets", side_effect=counted):
        worker.tick()
        assert len(_inbox(worker)) == 1 and calls == [8]
        for _ in range(5):
            clock.advance(media_worker.BUSY_TICK_SECONDS)
            worker.tick()
        assert calls == [8]
        # A result frees a slot (and may make its asset claimable again): ask at once.
        library.photo()
        _discover(library)
        processor.serve()
        worker.tick()
        assert len(calls) == 2
        clock.advance(media_worker.BACKGROUND_RETRY_SECONDS)
        worker.tick()
        assert len(calls) == 3


def test_a_rejected_result_counts_as_a_failure(library: Library, tmp_path: Path) -> None:
    entry = library.photo()
    _discover(library)
    worker, processor, _ = make_worker(tmp_path)
    processor.behaviour = lambda job: {"raw": b'{"forged": true}'}
    worker.tick()
    processor.serve()
    worker.tick()
    asset = MediaAsset.objects.get(pk=entry.pk)
    assert (asset.probe_state, asset.attempts) == (StageState.FAILED, 1)
    assert asset.safe_error_code == MediaErrorCode.OUTPUT_INVALID
    assert worker.last_error == MediaErrorCode.OUTPUT_INVALID


def test_a_processor_failure_counts_and_the_third_quarantines(
    library: Library, tmp_path: Path,
) -> None:
    entry = library.photo()
    _discover(library)
    worker, processor, clock = make_worker(tmp_path)
    processor.behaviour = lambda job: {"status": "failed", "code": "decode_failed"}
    for _ in range(3):
        MediaAsset.objects.filter(pk=entry.pk).update(next_attempt_at=timezone.now())
        worker.tick()
        processor.serve()
        worker.tick()
        clock.advance(1.0)
    asset = MediaAsset.objects.get(pk=entry.pk)
    assert asset.probe_state == StageState.QUARANTINED
    assert asset.thumbnail_state == StageState.QUARANTINED
    assert asset.attempts == 3
    served = len(processor.served)
    MediaAsset.objects.filter(pk=entry.pk).update(next_attempt_at=timezone.now())
    worker.tick()
    assert _inbox(worker) == [] and len(processor.served) == served


def test_changed_with_the_same_fingerprint_counts_toward_quarantine(
    library: Library, tmp_path: Path,
) -> None:
    entry = library.photo()
    _discover(library)
    worker, processor, _ = make_worker(tmp_path)
    processor.behaviour = lambda job: {"status": "changed", "code": "source_changed"}
    worker.tick()
    processor.serve()
    worker.tick()
    asset = MediaAsset.objects.get(pk=entry.pk)
    assert (asset.probe_state, asset.attempts, asset.safe_error_code) == (
        StageState.FAILED, 1, "source_changed",
    )


def test_changed_with_a_new_fingerprint_rediscovers_at_once(
    library: Library, tmp_path: Path,
) -> None:
    entry = library.photo()
    _discover(library)
    before = MediaAsset.objects.get(pk=entry.pk)
    worker, processor, _ = make_worker(tmp_path)

    def edited(job: protocol.Job) -> dict[str, object]:
        CatalogEntry.objects.filter(pk=entry.pk).update(mtime_ns=MTIME_NS + 1)
        return {"status": "changed", "code": "source_changed"}

    processor.behaviour = edited
    worker.tick()
    processor.serve()
    worker.tick()
    asset = MediaAsset.objects.get(pk=entry.pk)
    assert asset.fingerprint == library.fingerprint(entry) != before.fingerprint
    assert (asset.probe_state, asset.attempts) == (StageState.PENDING, 0)


def test_unsupported_files_end_their_pipeline(library: Library, tmp_path: Path) -> None:
    entry = library.video()
    _discover(library)
    worker, processor, _ = make_worker(tmp_path)
    processor.behaviour = lambda job: {"status": "unsupported", "code": "no_video_stream"}
    worker.tick()
    processor.serve()
    worker.tick()
    asset = MediaAsset.objects.get(pk=entry.pk)
    assert (asset.probe_state, asset.thumbnail_state, asset.poster_state) == (
        StageState.UNSUPPORTED,) * 3


def test_jobs_the_processor_never_answers_expire_without_counting(
    library: Library, tmp_path: Path,
) -> None:
    entry = library.photo()
    _discover(library)
    worker, processor, clock = make_worker(tmp_path)
    worker.tick()
    (job,) = processor.claim_only()
    worker.tick()  # the worker sees the claim: the job left the inbox
    clock.advance(job.limits.wall_seconds + media_worker.RESULT_GRACE_SECONDS + 1)
    worker.tick()
    asset = MediaAsset.objects.get(pk=entry.pk)
    assert (asset.probe_state, asset.attempts, asset.environment_failures) == (
        StageState.PENDING, 0, 1,
    )
    assert asset.safe_error_code == MediaErrorCode.PROCESSOR_UNAVAILABLE
    assert asset.next_attempt_at > timezone.now()
    assert worker.dispatches == {}
    assert worker.processor_failures == 1
    # Dispatching pauses while the processor is unavailable.
    library.photo()
    _discover(library)
    worker.tick()
    assert _inbox(worker) == []
    # A late answer is discarded: nobody is waiting for it any more.
    publish_result(Path(worker.scratch.done), job)
    worker.tick()
    assert os.listdir(worker.scratch.done) == []
    assert MediaAsset.objects.get(pk=entry.pk).probe_state == StageState.PENDING


def test_unclaimed_jobs_are_withdrawn_after_the_queue_grace(
    library: Library, tmp_path: Path,
) -> None:
    library.photo()
    _discover(library)
    worker, _, clock = make_worker(tmp_path)
    worker.tick()
    assert len(_inbox(worker)) == 1
    clock.advance(600 + media_worker.QUEUE_GRACE_SECONDS)
    worker.tick()
    assert _inbox(worker) == []
    assert worker.last_error == MediaErrorCode.PROCESSOR_UNAVAILABLE


def test_unknown_results_are_discarded(library: Library, tmp_path: Path) -> None:
    worker, _, _ = make_worker(tmp_path)
    worker.tick()
    forged = Path(worker.scratch.done) / ("e" * 32)
    forged.mkdir()
    (forged / "result.json").write_bytes(b"{}")
    (Path(worker.scratch.done) / "junk").symlink_to(tmp_path)
    worker.tick()
    assert os.listdir(worker.scratch.done) == []
    assert tmp_path.exists()


def test_an_unavailable_scratch_dispatches_nothing(library: Library, tmp_path: Path) -> None:
    library.photo()
    _discover(library)
    worker, _, _ = make_worker(tmp_path)
    os.rmdir(worker.scratch.root)
    worker.tick()
    assert worker.dispatches == {}
    assert worker.last_error == MediaErrorCode.PROCESSOR_UNAVAILABLE
    assert MediaAsset.objects.get().lease_owner is None


# -- the interactive lane -----------------------------------------------------------------


def test_the_operation_queue_is_checked_at_most_every_quarter_second_while_busy(
    library: Library, tmp_path: Path,
) -> None:
    library.photo()
    entry = library.photo()
    _discover(library)
    _ready(entry)
    worker, _, clock = make_worker(tmp_path)
    from aegis_apps.operations.leases import claim_next_job as claim_job

    checks: list[float] = []

    def counted(*args: Any, **kwargs: Any) -> Any:
        checks.append(clock.now)
        return claim_job(*args, **kwargs)

    assert media_worker.INTERACTIVE_POLL_SECONDS == 0.25
    assert media_worker.BUSY_TICK_SECONDS == 0.05
    with patch("aegis_apps.media.worker.claim_next_job", side_effect=counted):
        worker.tick()
        assert worker.dispatches and checks == [clock.now]
        for _ in range(4):
            clock.advance(media_worker.BUSY_TICK_SECONDS)
            worker.tick()
        assert len(checks) == 1
        # A request queued now is claimed on the first tick at the interval; the queue is
        # drained in that tick, then left alone for another interval.
        operation = library.request(library.requester(), entry)
        clock.now = checks[0] + media_worker.INTERACTIVE_POLL_SECONDS
        worker.tick()
        assert checks[1:] == [clock.now, clock.now]
        assert _job(operation.pk).state == JobState.RUNNING
        clock.advance(media_worker.BUSY_TICK_SECONDS)
        worker.tick()
        assert len(checks) == 3


def test_recovery_steps_are_paced_apart_from_the_busy_tick(
    library: Library, tmp_path: Path,
) -> None:
    library.photo()
    _discover(library)
    worker, _, clock = make_worker(tmp_path)
    steps: list[float] = []
    worker.recovery.start()
    with patch.object(type(worker.recovery), "step",
                      side_effect=lambda **_: steps.append(clock.now)):
        worker.tick()
        assert worker.dispatches and worker.tick_seconds() == media_worker.BUSY_TICK_SECONDS
        for _ in range(9):
            clock.advance(media_worker.BUSY_TICK_SECONDS)
            worker.tick()
    # Ten busy ticks in 0.45 s, but recovery steps at most every 0.25 s (its earlier rate).
    assert media_worker.RECOVERY_PACE_SECONDS == 0.25
    assert len(steps) == 2
    assert steps[1] - steps[0] >= media_worker.RECOVERY_PACE_SECONDS
    # With nothing in flight, maintenance alone ticks at its own pace; idle, once a second.
    worker.dispatches.clear()
    assert worker.tick_seconds() == media_worker.MAINTENANCE_TICK_SECONDS == 0.25
    worker.recovery.phase = "idle"
    worker.discovery.roots = []
    assert worker.tick_seconds() == media_worker.IDLE_TICK_SECONDS


def test_a_job_queued_behind_a_long_job_that_just_finished_is_not_withdrawn(
    library: Library, tmp_path: Path,
) -> None:
    long_video = library.video()
    library.photo()
    _discover(library)
    _ready(long_video, video=True)
    # An hour of video: the transcode's wall clock is the 7200 s cap.
    MediaAsset.objects.filter(pk=long_video.pk).update(duration_ms=3_600_000)
    operation = library.request(library.requester(), long_video, "transcode")
    worker, _, clock = make_worker(tmp_path, environ={"AEGIS_MEDIA_PROCESSOR_CONCURRENCY": "1"})
    worker.tick()
    queued = {job.op: job for job in _inbox(worker)}
    assert set(queued) == {"transcode", "backfill"}
    assert queued["transcode"].limits.wall_seconds == 7200
    # The single child runs the transcode; the backfill waits behind it in the inbox.
    inbox = Path(worker.scratch.inbox)
    (inbox / protocol.job_filename(queued["transcode"].id)).unlink()
    worker.tick()
    clock.advance(600)
    worker.tick()
    # The transcode finishes; the backfill has waited far longer than its own 90 s + 120 s.
    publish_result(Path(worker.scratch.done), queued["transcode"])
    worker.tick()
    assert _job(operation.pk).state == JobState.SUCCEEDED
    assert queued["backfill"].id in worker.dispatches
    assert [job.op for job in _inbox(worker)] == ["backfill"]
    assert (worker.processor_failures, worker._paused_until) == (0, 0.0)
    # It still expires once even the longest job it waited behind is overdue.
    clock.advance(7200 + media_worker.QUEUE_GRACE_SECONDS)
    worker.tick()
    assert _inbox(worker) == [] and worker.processor_failures == 1


def test_a_backfill_result_without_metadata_is_never_recorded_as_probed(
    library: Library, tmp_path: Path,
) -> None:
    from aegis_apps.media.dispatch import CollectedResult

    entry = library.photo()
    _discover(library)
    worker, _, _ = make_worker(tmp_path)
    worker.tick()
    (job,) = _inbox(worker)
    (Path(worker.scratch.done) / job.id).mkdir()
    forged = CollectedResult(protocol.Result(job.id, "ok", None, None, ()), ())
    with patch.object(worker.scratch, "collect", return_value=forged):
        worker.tick()
    asset = MediaAsset.objects.get(pk=entry.pk)
    assert (asset.probe_state, asset.safe_error_code, asset.width) == (
        StageState.FAILED, MediaErrorCode.OUTPUT_INVALID, None,
    )
    assert not MediaDerivative.objects.exists()


def test_background_claims_never_exceed_the_claim_batch(
    library: Library, tmp_path: Path,
) -> None:
    from aegis_apps.media.leasing import MAX_CLAIM_BATCH, claim_assets

    library.photo()
    _discover(library)
    worker, _, _ = make_worker(tmp_path, environ={"AEGIS_MEDIA_PROCESSOR_CONCURRENCY": "16"})
    worker.background_capacity = worker.capacity = MAX_CLAIM_BATCH + 8
    limits: list[int] = []

    def counted(*args: Any, **kwargs: Any) -> Any:
        limits.append(kwargs["limit"])
        return claim_assets(*args, **kwargs)

    with patch("aegis_apps.media.worker.claim_assets", side_effect=counted):
        worker.tick()
    assert limits == [MAX_CLAIM_BATCH]


def test_interactive_jobs_are_claimed_before_background_work(
    library: Library, tmp_path: Path,
) -> None:
    target = library.photo()
    for _ in range(4):
        library.photo()
    _discover(library)
    _ready(target)
    operation = library.request(library.requester(), target)
    worker, _, _ = make_worker(tmp_path, environ={"AEGIS_MEDIA_PROCESSOR_CONCURRENCY": "1"})
    calls: list[str] = []
    from aegis_apps.media.leasing import claim_assets
    from aegis_apps.operations.leases import claim_next_job as claim_job


    def job_first(*args: Any, **kwargs: Any) -> Any:
        calls.append("job")
        return claim_job(*args, **kwargs)

    def assets_after(*args: Any, **kwargs: Any) -> Any:
        calls.append("assets")
        return claim_assets(*args, **kwargs)

    with (
        patch("aegis_apps.media.worker.claim_next_job", side_effect=job_first),
        patch("aegis_apps.media.worker.claim_assets", side_effect=assets_after),
    ):
        worker.tick()
    assert calls[0] == "job" and "assets" in calls
    assert calls.index("assets") > max(i for i, call in enumerate(calls) if call == "job")
    queued = _inbox(worker)
    lanes = sorted(job.lane for job in queued)
    # Twice the concurrency for background work, plus the interactive slot.
    assert lanes == ["background", "background", "interactive"]
    (preview,) = [job for job in queued if job.lane == "interactive"]
    assert (preview.op, preview.kind) == ("preview", "photo")
    assert _job(operation.pk).state == JobState.RUNNING
    assert _job(operation.pk).execution_started_at is not None


def test_an_interactive_preview_is_published_and_its_job_succeeds(
    library: Library, tmp_path: Path,
) -> None:
    entry = library.photo()
    _discover(library)
    _ready(entry)
    operation = library.request(library.requester(), entry)
    worker, processor, clock = make_worker(tmp_path)
    drive(worker, processor, clock, lambda: _job(operation.pk).state == JobState.SUCCEEDED)
    job = _job(operation.pk)
    assert job.result == {"ok": True}
    row = MediaDerivative.objects.get(asset_id=entry.pk, kind="preview")
    assert (row.state, row.evictable, row.width) == (DerivativeState.READY, True, 2048)
    assert row.fingerprint == MediaAsset.objects.get(pk=entry.pk).fingerprint
    assert Path(worker.store.root, row.key[0:2], row.key[2:4], f"{row.key}.webp").is_file()


def test_an_interactive_transcode_is_an_mp4(library: Library, tmp_path: Path) -> None:
    entry = library.video()
    _discover(library)
    _ready(entry, video=True)
    operation = library.request(library.requester(), entry, "transcode")
    worker, processor, clock = make_worker(tmp_path)
    drive(worker, processor, clock, lambda: _job(operation.pk).state == JobState.SUCCEEDED)
    row = MediaDerivative.objects.get(asset_id=entry.pk, kind="transcode")
    assert (row.extension, row.width, row.height) == ("mp4", 1920, 1080)
    (served,) = [job for job in processor.served if job.op == "transcode"]
    assert served.lane == "interactive"


def test_an_interactive_failure_fails_its_job_and_leaves_a_failed_derivative(
    library: Library, tmp_path: Path,
) -> None:
    entry = library.photo()
    _discover(library)
    _ready(entry)
    operation = library.request(library.requester(), entry)
    worker, processor, clock = make_worker(tmp_path)
    processor.behaviour = lambda job: {"status": "failed", "code": "memory_limit"}
    drive(worker, processor, clock, lambda: _job(operation.pk).state == JobState.FAILED)
    job = _job(operation.pk)
    assert job.safe_error_code == SafeErrorCode.HANDLER_FAILED
    row = MediaDerivative.objects.get(asset_id=entry.pk, kind="preview")
    assert row.state == DerivativeState.FAILED
    assert row.fingerprint == MediaAsset.objects.get(pk=entry.pk).fingerprint
    # Interactive failures never quarantine the asset's own stages.
    assert MediaAsset.objects.get(pk=entry.pk).thumbnail_state == StageState.READY


def test_stale_authorization_fails_the_job_before_anything_runs(
    library: Library, tmp_path: Path,
) -> None:
    entry = library.photo()
    _discover(library)
    _ready(entry)
    user = library.requester()
    operation = library.request(user, entry)
    user.authorization_epoch += 1
    user.save(update_fields=("authorization_epoch",))
    worker, _, _ = make_worker(tmp_path)
    worker.tick()
    job = _job(operation.pk)
    assert (job.state, job.safe_error_code) == (JobState.FAILED, SafeErrorCode.AUTHORIZATION_STALE)
    assert job.execution_started_at is None
    assert _inbox(worker) == []
    assert not MediaDerivative.objects.exists()


def test_a_request_for_a_file_that_changed_fails_and_rediscovers(
    library: Library, tmp_path: Path,
) -> None:
    entry = library.photo()
    _discover(library)
    _ready(entry)
    operation = library.request(library.requester(), entry)
    CatalogEntry.objects.filter(pk=entry.pk).update(mtime_ns=MTIME_NS + 9)
    worker, _, _ = make_worker(tmp_path)
    with patch("aegis_apps.media.worker.claim_assets", return_value=[]):
        worker.tick()
    job = _job(operation.pk)
    assert (job.state, job.safe_error_code) == (JobState.FAILED, SafeErrorCode.HANDLER_FAILED)
    assert job.execution_started_at is None
    assert MediaAsset.objects.get(pk=entry.pk).fingerprint == library.fingerprint(entry)
    assert _inbox(worker) == []


@pytest.mark.parametrize("state", ["missing", "inaccessible"])
def test_a_request_for_a_gone_file_fails(library: Library, tmp_path: Path, state: str) -> None:
    entry = library.photo()
    _discover(library)
    _ready(entry)
    operation = library.request(library.requester(), entry)
    CatalogEntry.objects.filter(pk=entry.pk).update(source_state=state)
    worker, _, _ = make_worker(tmp_path)
    worker.tick()
    assert _job(operation.pk).safe_error_code == SafeErrorCode.HANDLER_FAILED
    assert _inbox(worker) == []


def test_a_request_for_a_quarantined_asset_fails(library: Library, tmp_path: Path) -> None:
    entry = library.photo()
    _discover(library)
    _ready(entry)
    operation = library.request(library.requester(), entry)
    MediaAsset.objects.filter(pk=entry.pk).update(
        thumbnail_state=StageState.QUARANTINED, safe_error_code="decode_failed",
    )
    worker, _, _ = make_worker(tmp_path)
    worker.tick()
    assert _job(operation.pk).safe_error_code == SafeErrorCode.HANDLER_FAILED
    assert not MediaDerivative.objects.exists()


def test_a_ready_derivative_completes_a_request_without_a_processor_job(
    library: Library, tmp_path: Path,
) -> None:
    entry = library.photo()
    _discover(library)
    _ready(entry)
    worker, processor, clock = make_worker(tmp_path)
    first = library.request(library.requester(), entry)
    drive(worker, processor, clock, lambda: _job(first.pk).state == JobState.SUCCEEDED)
    served = len(processor.served)
    second = library.request(library.requester(), entry)
    clock.advance(media_worker.INTERACTIVE_POLL_SECONDS)
    worker.tick()
    assert _job(second.pk).state == JobState.SUCCEEDED
    assert len(processor.served) == served and _inbox(worker) == []


def test_two_requests_for_one_derivative_share_one_processor_job(
    library: Library, tmp_path: Path,
) -> None:
    entry = library.photo()
    _discover(library)
    _ready(entry)
    first = library.request(library.requester(), entry)
    second = library.request(library.requester(), entry)
    worker, processor, clock = make_worker(tmp_path)
    worker.tick()
    assert len(_inbox(worker)) == 1
    drive(worker, processor, clock, lambda: _job(second.pk).state == JobState.SUCCEEDED)
    assert _job(first.pk).state == JobState.SUCCEEDED
    assert [job.op for job in processor.served] == ["preview"]


def test_the_foundation_probe_still_runs_in_the_media_loop(
    library: Library, tmp_path: Path,
) -> None:
    from aegis_apps.identity.models import User
    from aegis_apps.operations.services import create_operation, enqueue_job

    actor = User.objects.create_user(username=f"probe-{uuid.uuid4().hex}")
    operation = create_operation(actor=actor, request_id="media_probe_request",
                                 kind="foundation.probe", intent={"roots": []})
    job = enqueue_job(operation=operation, target_role="media")
    worker, _, _ = make_worker(tmp_path)
    worker.tick()
    job.refresh_from_db()
    assert (job.state, job.result) == (JobState.SUCCEEDED, {"ok": True})


# -- heartbeat and shutdown ---------------------------------------------------------------


def test_heartbeat_metrics_are_counts_and_codes_without_names(
    library: Library, tmp_path: Path,
) -> None:
    names = [library.photo().raw_name, library.video().raw_name]
    worker, processor, clock = make_worker(tmp_path)
    processor.behaviour = lambda job: (
        {"status": "failed", "code": "decode_failed"} if job.kind == "video" else {}
    )
    drive(worker, processor, clock,
           lambda: MediaAsset.objects.filter(probe_state=StageState.READY).count() == 1)
    clock.advance(media_worker.METRICS_SECONDS + 1)
    worker.tick()
    heartbeat = WorkerHeartbeat.objects.get(role="media", worker_id=WORKER_ID)
    metrics = heartbeat.metrics
    assert set(metrics) <= {
        "queueAgeSeconds", "scanProgress", "mediaInteractiveQueued", "mediaBackgroundPending",
        "mediaInFlight", "mediaAssets", "mediaAssetsComplete", "mediaThumbnailsReady",
        "mediaPostersReady", "mediaProcessorFailures", "mediaLastErrorCode",
    }
    assert metrics["mediaProcessorFailures"] == 0
    assert metrics["mediaAssets"] == 2
    # Ready derivatives, for the backfill rate: the photo's thumbnail, no poster yet.
    assert (metrics["mediaThumbnailsReady"], metrics["mediaPostersReady"]) == (1, 0)
    assert metrics["mediaLastErrorCode"] in MediaErrorCode.values
    assert 0 <= metrics["scanProgress"] <= 1
    for key, value in metrics.items():
        if key != "mediaLastErrorCode":
            assert type(value) in (int, float)
    text = json.dumps(metrics)
    for name in names:
        assert bytes(name).decode("ascii", "ignore") not in text
    for private in (library.root.slot_id, "album", "/", str(library.root.pk)):
        assert private not in text


def test_shutdown_returns_leases_and_withdraws_unclaimed_jobs(
    library: Library, tmp_path: Path,
) -> None:
    background = library.photo()
    requested = library.photo()
    _discover(library)
    _ready(requested)
    operation = library.request(library.requester(), requested)
    worker, _, _ = make_worker(tmp_path)
    worker.tick()
    assert len(_inbox(worker)) == 2
    worker.shutdown(wait_seconds=0)
    assert _inbox(worker) == []
    assert worker.dispatches == {}
    asset = MediaAsset.objects.get(pk=background.pk)
    assert (asset.lease_owner, asset.lease_expires_at, asset.attempts) == (None, None, 0)
    assert asset.next_attempt_at <= timezone.now()
    job = _job(operation.pk)
    assert (job.state, job.safe_error_code) == (
        JobState.RETRY_WAIT, SafeErrorCode.RETRYABLE_FAILURE,
    )


def test_shutdown_waits_for_jobs_in_flight(library: Library, tmp_path: Path) -> None:
    entry = library.photo()
    _discover(library)
    stopping = [False]
    worker, processor, _ = make_worker(tmp_path, stop=lambda: stopping[0])
    worker.tick()
    processor.serve()
    stopping[0] = True
    worker.shutdown(wait_seconds=5)
    assert MediaAsset.objects.get(pk=entry.pk).probe_state == StageState.READY
    assert worker.dispatches == {}


def test_a_changed_manifest_stops_the_worker(library: Library, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    worker, _, clock = make_worker(tmp_path)
    worker.tick()
    # A second, valid manifest: it loads, but it is not the one the worker started with.
    current = Path(os.environ["AEGIS_MOUNT_MANIFEST"])
    document = json.loads(current.read_bytes())
    document["generatedAt"] = "2026-10-11T12:00:00Z"
    raw = json.dumps(document).encode()
    replacement = tmp_path / "replacement-manifest.json"
    replacement.write_bytes(raw)
    replacement.chmod(0o600)
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST", str(replacement))
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST_SHA256", hashlib.sha256(raw).hexdigest())
    clock.advance(media_worker.ROOTS_REFRESH_SECONDS + 1)
    with pytest.raises(RuntimeError, match=r"^media manifest changed$"):
        worker.tick()


def test_derivative_keys_of_published_previews_are_the_pipeline_keys(
    library: Library, tmp_path: Path,
) -> None:
    entry = library.photo()
    _discover(library)
    _ready(entry)
    operation = library.request(library.requester(), entry)
    worker, processor, clock = make_worker(tmp_path)
    drive(worker, processor, clock, lambda: _job(operation.pk).state == JobState.SUCCEEDED)
    fingerprint = MediaAsset.objects.get(pk=entry.pk).fingerprint
    (identity,) = derivative_identities(fingerprint, "preview", protocol.PREVIEW_PARAMS)
    assert MediaDerivative.objects.get(kind="preview").key == identity.key


# -- environmental failures: re-queued, never a failed row --------------------------------


def test_an_unanswered_interactive_job_goes_back_to_the_queue_without_a_failed_row(
    library: Library, tmp_path: Path,
) -> None:
    entry = library.photo()
    _discover(library)
    _ready(entry)
    operation = library.request(library.requester(), entry)
    worker, processor, clock = make_worker(tmp_path)
    worker.tick()
    (job,) = processor.claim_only()
    worker.tick()
    clock.advance(job.limits.wall_seconds + media_worker.RESULT_GRACE_SECONDS + 1)
    worker.tick()
    queued = _job(operation.pk)
    assert (queued.state, queued.safe_error_code) == (
        JobState.RETRY_WAIT, SafeErrorCode.RETRYABLE_FAILURE,
    )
    assert not MediaDerivative.objects.exists()


@pytest.mark.parametrize(
    "code", ["spawn_failed", "tool_missing", "source_unavailable", "interrupted"],
)
def test_environmental_results_requeue_interactive_jobs(
    library: Library, tmp_path: Path, code: str,
) -> None:
    entry = library.photo()
    _discover(library)
    _ready(entry)
    operation = library.request(library.requester(), entry)
    worker, processor, _ = make_worker(tmp_path)
    processor.behaviour = lambda job: {"status": "failed", "code": code}
    worker.tick()
    processor.serve()
    worker.tick()
    queued = _job(operation.pk)
    assert (queued.state, queued.safe_error_code) == (
        JobState.RETRY_WAIT, SafeErrorCode.RETRYABLE_FAILURE,
    )
    assert not MediaDerivative.objects.exists()


def test_processor_wide_failures_pause_dispatching_with_a_growing_backoff(
    library: Library, tmp_path: Path,
) -> None:
    for _ in range(6):
        library.photo()
    _discover(library)
    worker, processor, clock = make_worker(
        tmp_path, environ={"AEGIS_MEDIA_PROCESSOR_CONCURRENCY": "1"},
    )
    processor.behaviour = lambda job: {"status": "failed", "code": "tool_missing"}
    worker.tick()
    assert len(_inbox(worker)) == 2
    processor.serve()
    worker.tick()
    # Both jobs met the same outage: it counts once.
    assert worker.processor_failures == 1
    assert worker._paused_until == clock.now + 30
    worker.tick()
    assert _inbox(worker) == []  # paused
    clock.advance(31)
    worker.tick()
    assert len(_inbox(worker)) == 2
    processor.serve()
    worker.tick()
    assert worker.processor_failures == 2
    assert worker._paused_until == clock.now + 60
    # The processor recovers: its first result closes the breaker.
    processor.behaviour = lambda job: {}
    clock.advance(61)
    worker.tick()
    processor.serve()
    worker.tick()
    assert worker.processor_failures == 0
    assert MediaAsset.objects.filter(probe_state=StageState.READY).count() == 2
    # Each asset that met tool_missing backed off on its own, without a counted attempt.
    assert set(MediaAsset.objects.values_list("attempts", flat=True)) == {0}


def test_a_swapped_done_directory_stops_dispatch_and_is_never_followed(
    library: Library, tmp_path: Path,
) -> None:
    library.photo()
    _discover(library)
    worker, _, clock = make_worker(tmp_path)
    precious = tmp_path / "precious"
    (precious / "aa").mkdir(parents=True)
    (precious / "aa" / "keep.webp").write_bytes(b"keep")
    worker.tick()
    assert worker.scratch_ready
    os.rename(worker.scratch.done, tmp_path / "done.moved")
    Path(worker.scratch.done).symlink_to(precious)
    worker.tick()
    assert not worker.scratch_ready
    assert worker.processor_failures == 1
    assert (precious / "aa" / "keep.webp").read_bytes() == b"keep"
    # Once the volume is sane again, the worker re-opens it after the pause.
    Path(worker.scratch.done).unlink()
    Path(worker.scratch.done).mkdir()
    clock.advance(31)
    worker.tick()
    assert worker.scratch_ready
