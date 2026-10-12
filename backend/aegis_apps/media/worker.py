"""The media worker: ``run_role --role media`` (spec M2, M3).

One single-threaded loop, with one database connection, drives the whole pipeline. Each pass:

1. **collects** results from ``done/`` (``dispatch.py`` validates them as untrusted) and
   records them (``pipeline.py``, ``publish.py``); then deletes the job directory;
2. **expires** jobs the processor never claimed or never answered (the processor is
   unavailable: not counted against the file) and renews the leases of jobs in flight;
3. **fills** free processor slots. At most ``AEGIS_MEDIA_PROCESSOR_CONCURRENCY + 1`` jobs are
   in flight, and background work may use only ``concurrency`` of them, so an interactive job
   always finds room in the inbox (where the processor claims interactive jobs first).
   - The **interactive lane** comes first: ``media.preview`` and ``media.transcode`` jobs from
     the operation queue, claimed with ``claim_next_job``; each claim's authorization
     snapshot is re-checked before anything runs (``validate_authorization_snapshot``), then
     the payload is parsed (``requests.parse_media_request_job``) and checked against the
     catalog and the asset (same root, present, same fingerprint, the right kind, not
     unsupported or quarantined). A ready derivative completes the job at once; a job for a
     derivative already in flight joins it. Other job kinds of the media role (the foundation
     probe) run through the generic ``_execute_claim``.
   - The **background lane**: fenced asset leases (``leasing.py``), probe first, then
     thumbnail or poster.
4. runs one bounded **discovery** batch (``discovery.py``; a pass every
   ``AEGIS_MEDIA_DISCOVERY_INTERVAL_SECONDS``, an hour by default, over the active roots it
   re-reads every ``AEGIS_MEDIA_ROOTS_REFRESH_SECONDS``), **eviction** above the budget
   (``eviction.py``) and one **recovery** step (``recovery.py``);
5. publishes a **heartbeat** whose metrics are counts and one error code, never a name or a
   path.

Interactive jobs end as ``succeeded`` (result ``{"ok": true}``; the client polls the media
details for the URL); as ``failed`` with ``handler_failed`` and a ``failed`` derivative row
(which feeds the web side's request backoff) when the content cannot be processed or the
request no longer matches the asset; as ``failed`` with ``authorization_stale``; or back in
the queue with ``retryable_failure`` and no derivative row when the failure is environmental
(``pipeline.INTERACTIVE_RETRY_CODES``: the processor, the sandbox, the scratch or derivatives
volume, the source volume, or the processor's own shutdown).

**Circuit breaker.** Processor-wide failures (``pipeline.PROCESSOR_WIDE_CODES``, and a scratch
volume that is missing, unreadable or replaced) pause all dispatching for ``30 s * 2**(n - 1)``
(capped at an hour) after the n-th consecutive one; any result the processor produces resets
it. The heartbeat reports the consecutive count (``mediaProcessorFailures``) and the code.

The worker never opens an original and never decodes media: it builds each job from catalog
columns (slot and raw relative path) and the processor opens the file. On SIGTERM it stops
claiming and dispatching, waits up to :data:`SHUTDOWN_WAIT_SECONDS` for jobs in flight, then
gives every remaining lease back (asset leases unchanged; interactive jobs back to the queue)
and withdraws its unclaimed inbox files.
"""

from __future__ import annotations

import logging
import math
import random
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from aegis_media_processor import protocol
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from aegis_apps.operations.enums import JobKind, JobState, SafeErrorCode, WorkerRole
from aegis_apps.operations.leases import (
    LeaseToken,
    claim_next_job,
    fail_job,
    finish_job,
    relinquish_job,
    renew_lease,
    retry_job,
)
from aegis_apps.operations.models import UNCONFIGURED_MANIFEST_IDENTITY, Job
from aegis_apps.operations.services import validate_authorization_snapshot

from .config import ROOTS_REFRESH, MediaPolicy
from .discovery import Discovery, active_roots, catalog_file, is_parked, rediscover_entry
from .dispatch import CollectedResult, ResultRejected, Scratch, ScratchUnavailable
from .enums import DerivativeState, MediaErrorCode, MediaKind, StageState
from .eviction import evict
from .leasing import (
    PARKED_UNTIL,
    AssetLease,
    ClaimedAsset,
    bump_to_interactive,
    claim_assets,
    fenced,
    release_asset_lease,
    renew_asset_lease,
)
from .models import ASSET_NEEDS_WORK, MediaAsset, MediaDerivative
from .pipeline import (
    INTERACTIVE_RETRY_CODES,
    PROCESSOR_WIDE_CODES,
    REQUEST_OPERATIONS,
    build_job,
    derivative_identities,
    next_operation,
    record_failure,
    record_probe,
    record_unsupported,
    source_path,
    stage_ready_update,
    terminal,
)
from .publish import (
    DerivativeStore,
    FenceLost,
    OutputRejected,
    Publication,
    PublishFailed,
    StoreUnavailable,
    record_failed_request,
    record_publications,
    stage_outputs,
)
from .recovery import RecoverySweep
from .requests import MediaRequestJob, parse_media_request_job

if TYPE_CHECKING:
    from aegis_apps.operations.management.commands.run_role import WorkerIdentity

SCRATCH_DIR: Final = protocol.DEFAULT_SCRATCH_DIR
DERIVATIVES_DIR: Final = "/srv/aegis/derivatives"
BUSY_TICK_SECONDS: Final = 0.25
IDLE_TICK_SECONDS: Final = 1.0
QUEUE_GRACE_SECONDS: Final = 120.0
RESULT_GRACE_SECONDS: Final = 60.0
ASSET_RENEW_SECONDS: Final = 60.0
# The default of AEGIS_MEDIA_ROOTS_REFRESH_SECONDS; the worker uses its policy's value.
ROOTS_REFRESH_SECONDS: Final = float(ROOTS_REFRESH[1])
METRICS_SECONDS: Final = 60.0
EVICTION_SECONDS: Final = 60.0
DISCOVERY_PACE_SECONDS: Final = 0.1
BREAKER_BASE_SECONDS: Final = 30.0
BREAKER_MAX_SECONDS: Final = 3_600.0
SHUTDOWN_WAIT_SECONDS: Final = 8.0
MAX_INTERACTIVE_CLAIMS_PER_TICK: Final = 4
MEDIA_JOB_KINDS: Final = frozenset({JobKind.MEDIA_PREVIEW.value, JobKind.MEDIA_TRANSCODE.value})
REQUEST_MEDIA_KIND: Final = {"preview": MediaKind.PHOTO, "transcode": MediaKind.VIDEO}

LOGGER: Final = logging.getLogger("aegis.media.worker")


@dataclass(slots=True)
class InteractiveClaim:
    lease: LeaseToken
    renew_at: float


@dataclass(slots=True)
class Dispatch:
    """One job in flight; the in-memory ``job`` is the only authority for its result."""

    job: protocol.Job
    entry_id: uuid.UUID
    fingerprint: str
    kind: str
    mtime_ns: Any
    attempts: int
    dispatched_at: float
    asset_lease: AssetLease | None = None
    environment_failures: int = 0
    claims: list[InteractiveClaim] = field(default_factory=list)
    claimed_at: float | None = None
    renew_at: float = 0.0
    lease_lost: bool = False

    @property
    def interactive(self) -> bool:
        return self.asset_lease is None


@dataclass(frozen=True, slots=True)
class RequestTarget:
    slot: str
    path: bytes
    identity: tuple[int, int, int, int]
    kind: str
    duration_ms: int | None
    mtime_ns: Any


def media_metrics(
    *, in_flight: int, last_error: str | None, now: datetime, processor_failures: int = 0,
) -> dict[str, int | float | str]:
    """Heartbeat metrics: counts and one safe error code; never a name or a path."""
    queued = Job.objects.filter(
        target_role=WorkerRole.MEDIA, state__in=(JobState.QUEUED, JobState.RETRY_WAIT),
    )
    interactive = queued.count()
    oldest = queued.order_by("available_at").values_list("available_at", flat=True).first()
    total = MediaAsset.objects.count()
    parked = MediaAsset.objects.filter(ASSET_NEEDS_WORK, next_attempt_at__gte=PARKED_UNTIL).count()
    pending = MediaAsset.objects.filter(ASSET_NEEDS_WORK, next_attempt_at__lt=PARKED_UNTIL).count()
    current = max(total - parked, 0)
    complete = max(current - pending, 0)
    metrics: dict[str, int | float | str] = {
        "queueAgeSeconds": 0 if oldest is None else max(0, int((now - oldest).total_seconds())),
        "scanProgress": 1.0 if current == 0 else round(complete / current, 4),
        "mediaInteractiveQueued": interactive,
        "mediaBackgroundPending": pending,
        "mediaInFlight": max(in_flight, 0),
        "mediaAssets": current,
        "mediaAssetsComplete": complete,
        "mediaProcessorFailures": max(processor_failures, 0),
    }
    if last_error is not None and last_error in MediaErrorCode.values:
        metrics["mediaLastErrorCode"] = last_error
    return metrics


def interactive_fence(entry_id: uuid.UUID, fingerprint: str, kind: str) -> Q:
    """What an interactive result may still be recorded against: the same asset version."""
    return (
        Q(entry_id=entry_id, fingerprint=fingerprint, kind=kind)
        & Q(next_attempt_at__lt=PARKED_UNTIL)
        & ~Q(probe_state__in=(StageState.UNSUPPORTED, StageState.QUARANTINED))
        & ~Q(thumbnail_state=StageState.QUARANTINED)
        & (Q(poster_state__isnull=True) | ~Q(poster_state=StageState.QUARANTINED))
    )


def record_request_failure(
    entry_id: uuid.UUID, fingerprint: str, kind: str, op: protocol.Op,
) -> None:
    """Leave a ``failed`` derivative state for (entry, kind, fingerprint) (Task 6's backoff)."""
    for item in derivative_identities(fingerprint, op, protocol.DEFAULT_PARAMS[op]):
        record_failed_request(
            entry_id=entry_id, fingerprint=fingerprint, kind=item.kind, key=item.key,
            params_digest=item.params_digest, extension=item.extension,
            fence=interactive_fence(entry_id, fingerprint, kind),
        )


def _default_sleep(seconds: float) -> None:
    from aegis_apps.operations.management.commands import run_role

    run_role._wait_for_shutdown(seconds)


class MediaWorker:
    def __init__(
        self,
        identity: WorkerIdentity,
        *,
        stop_requested: Callable[[], bool],
        policy: MediaPolicy,
        scratch: Scratch,
        store: DerivativeStore,
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        uniform: Callable[[], float] = random.random,
    ) -> None:
        self.identity = identity
        self.owner = uuid.UUID(identity.worker_id)
        self.stop_requested = stop_requested
        self.policy = policy
        self.scratch = scratch
        self.store = store
        self.sleep = sleep or _default_sleep
        self.monotonic = monotonic
        self.uniform = uniform
        self.capacity = policy.processor_concurrency + 1
        self.background_capacity = policy.processor_concurrency
        self.dispatches: dict[str, Dispatch] = {}
        self.discovery = Discovery(
            batch=policy.discovery_batch,
            interval_seconds=float(policy.discovery_interval_seconds),
            clock=monotonic,
        )
        self.recovery = RecoverySweep(store, clock=monotonic)
        self.roots: dict[uuid.UUID, str] = {}
        self.last_error: str | None = None
        self.scratch_ready = False
        self.store_ready = False
        self._cleared = False
        self._ready_check_at = 0.0
        self._paused_until = 0.0
        self._roots_at = 0.0
        self._discovery_at = 0.0
        self._eviction_at = 0.0
        self._metrics_at = 0.0
        self._heartbeat_at = 0.0
        self._metrics: dict[str, int | float | str] = {}
        self._breaker_failures = 0
        self._copy_deadline = math.inf
        self._job_lease_renew = max(1.0, float(settings.AEGIS_JOB_LEASE_SECONDS) / 3)
        self._heartbeat_seconds = float(settings.AEGIS_WORKER_HEARTBEAT_SECONDS)

    # -- the loop -----------------------------------------------------------------------------

    def run(self) -> None:
        try:
            while not self.stop_requested():
                self.tick()
                if self.stop_requested():
                    break
                self.sleep(BUSY_TICK_SECONDS if self._busy() else IDLE_TICK_SECONDS)
        finally:
            self.shutdown()

    def _busy(self) -> bool:
        return bool(self.dispatches) or self.discovery.active or self.recovery.active

    def tick(self) -> None:
        now_m = self.monotonic()
        self._ensure_ready(now_m)
        self._verify_scratch(now_m)
        self._refresh_roots(now_m)
        self._collect()
        self._expire(now_m)
        self._renew(now_m)
        if not self.stop_requested():
            self._fill(now_m)
        self._discover(now_m)
        self._maintain(now_m)
        self._heartbeat(now_m)

    def _error(self, code: str) -> None:
        self.last_error = code

    def _trip(self, code: str) -> None:
        """A processor-wide failure: pause all dispatching, longer each consecutive time."""
        self._error(code)
        self._breaker_failures += 1
        exponent = min(self._breaker_failures - 1, 30)
        pause = min(BREAKER_MAX_SECONDS, BREAKER_BASE_SECONDS * (2**exponent))
        self._paused_until = max(self._paused_until, self.monotonic() + pause)
        LOGGER.error(
            "media worker: dispatching paused for %ds (%s, %d in a row)",
            int(pause), code, self._breaker_failures,
        )

    def _healthy(self) -> None:
        """The processor produced a result: close the circuit breaker."""
        self._breaker_failures = 0

    @property
    def processor_failures(self) -> int:
        return self._breaker_failures

    def _scratch_lost(self) -> None:
        self.scratch_ready = False
        self.scratch.close()
        self._trip(MediaErrorCode.PROCESSOR_UNAVAILABLE)
        self._ready_check_at = self._paused_until

    def _ensure_ready(self, now_m: float) -> None:
        if (self.scratch_ready and self.store_ready) or now_m < self._ready_check_at:
            return
        if not self.scratch_ready:
            try:
                self.scratch.prepare()
                if not self._cleared:
                    self.scratch.clear_stale()
                    self._cleared = True
                self.scratch_ready = True
            except ScratchUnavailable:
                LOGGER.error("media worker: scratch unavailable (processor_unavailable)")
                self._scratch_lost()
        try:
            self.store.check()
            self.store_ready = True
        except StoreUnavailable:
            self.store_ready = False
            LOGGER.error("media worker: derivatives unavailable (publish_failed)")
            self._trip(MediaErrorCode.PUBLISH_FAILED)
            self._ready_check_at = self._paused_until

    def _verify_scratch(self, now_m: float) -> None:
        """Every pass: the held scratch directories must still be the ones in the tree."""
        if not self.scratch_ready:
            return
        try:
            self.scratch.verify()
        except ScratchUnavailable:
            LOGGER.error("media worker: scratch directories replaced or unreadable")
            self._scratch_lost()

    def _refresh_roots(self, now_m: float) -> None:
        if now_m < self._roots_at:
            return
        from aegis_apps.roots.manifest import configured_manifest

        manifest = configured_manifest()
        identity = UNCONFIGURED_MANIFEST_IDENTITY if manifest is None else manifest.digest
        if identity != self.identity.manifest_identity:
            raise RuntimeError("media manifest changed")
        self.roots = active_roots(manifest)
        self._roots_at = now_m + float(self.policy.roots_refresh_seconds)

    # -- results ------------------------------------------------------------------------------

    def _collect(self) -> None:
        if not self.scratch_ready:
            return
        try:
            names = self.scratch.finished()
        except ScratchUnavailable:
            self._scratch_lost()
            return
        for name in names:
            dispatch = self.dispatches.pop(name, None)
            if dispatch is None:
                # Late, forged or left by an earlier worker: nobody is waiting for it.
                self.scratch.discard(name)
                continue
            try:
                self._conclude(dispatch)
            finally:
                self.scratch.discard(name)

    def _conclude(self, dispatch: Dispatch) -> None:
        try:
            collected = self.scratch.collect(dispatch.job)
        except ResultRejected as rejection:
            LOGGER.warning("media job %s rejected (%s)", dispatch.job.id, rejection.reason)
            self._healthy()
            self._failed(dispatch, MediaErrorCode.OUTPUT_INVALID)
            return
        with collected:
            result = collected.result
            if result.safe_error_code not in PROCESSOR_WIDE_CODES:
                self._healthy()
            if result.status == "ok":
                self._succeeded(dispatch, collected)
            elif result.status == "unsupported":
                self._unsupported(dispatch, result.safe_error_code or "unsupported_format")
            elif result.status == "changed":
                self._changed(dispatch)
            else:
                self._failed(dispatch, result.safe_error_code or MediaErrorCode.CRASHED)

    def _interactive_fence(self, dispatch: Dispatch) -> Q:
        return interactive_fence(dispatch.entry_id, dispatch.fingerprint, dispatch.kind)

    def _succeeded(self, dispatch: Dispatch, collected: CollectedResult) -> None:
        job = dispatch.job
        now = timezone.now()
        if job.op == "probe":
            metadata = collected.result.metadata
            if dispatch.asset_lease is not None and metadata is not None:
                record_probe(dispatch.asset_lease, metadata, mtime_ns=dispatch.mtime_ns, now=now)
            return
        identities = derivative_identities(dispatch.fingerprint, job.op, job.params)
        try:
            staged = stage_outputs(self.store, [
                (output, identity.key)
                for output, identity in zip(collected.outputs, identities, strict=True)
            ], progress=lambda: self._keepalive(dispatch))
        except OutputRejected:
            self._failed(dispatch, MediaErrorCode.OUTPUT_INVALID)
            return
        except PublishFailed:
            self._failed(dispatch, MediaErrorCode.PUBLISH_FAILED)
            return
        publications = [
            Publication(identity.kind, identity.key, identity.params_digest, item)
            for identity, item in zip(identities, staged, strict=True)
        ]
        if dispatch.asset_lease is not None:
            fence = fenced(dispatch.asset_lease, now=now)
            update = stage_ready_update(job.op, dispatch.kind, now=now)
        else:
            fence = self._interactive_fence(dispatch)
            update = None
        try:
            record_publications(
                self.store, entry_id=dispatch.entry_id, fingerprint=dispatch.fingerprint,
                fence=fence, publications=publications, asset_update=update, now=now,
            )
        except FenceLost:
            # A background lease was lost (nothing to record), or the asset changed under an
            # interactive request.
            if dispatch.interactive:
                self._request_failed(dispatch)
            return
        except PublishFailed:
            self._failed(dispatch, MediaErrorCode.PUBLISH_FAILED)
            return
        for claim in dispatch.claims:
            finish_job(claim.lease, result={"ok": True}, now=timezone.now())
        dispatch.claims.clear()

    def _unsupported(self, dispatch: Dispatch, code: str) -> None:
        self._error(code)
        if dispatch.asset_lease is not None:
            record_unsupported(
                dispatch.asset_lease, dispatch.job.op, dispatch.kind, code, now=timezone.now(),
            )
            return
        self._request_failed(dispatch)

    def _changed(self, dispatch: Dispatch) -> None:
        self._error(MediaErrorCode.SOURCE_CHANGED)
        outcome = rediscover_entry(dispatch.entry_id, now=timezone.now())
        if dispatch.asset_lease is None:
            # A new fingerprint fences the failed row out; the client sees the new asset.
            self._request_failed(dispatch)
            return
        if outcome == "unchanged":
            # The catalog still says this is the file: a source the processor can never
            # match must count, or it would loop (Task 3 review, I3).
            self._failed(dispatch, MediaErrorCode.SOURCE_CHANGED)

    def _failed(self, dispatch: Dispatch, code: str) -> None:
        self._error(code)
        if code in PROCESSOR_WIDE_CODES:
            self._trip(code)
        if dispatch.asset_lease is not None:
            outcome = record_failure(
                dispatch.asset_lease, dispatch.job.op, dispatch.kind, code,
                attempts=dispatch.attempts, environment_failures=dispatch.environment_failures,
                now=timezone.now(), uniform=self.uniform,
            )
            if outcome == "quarantined":
                LOGGER.warning("media job %s: asset quarantined (%s)", dispatch.job.id, code)
            return
        if code in INTERACTIVE_RETRY_CODES:
            self._retry_claims(dispatch)
        else:
            self._request_failed(dispatch)

    def _retry_claims(self, dispatch: Dispatch) -> None:
        """An environmental failure: back to the queue, no failed row (no web backoff)."""
        for claim in dispatch.claims:
            retry_job(
                claim.lease, error_code=SafeErrorCode.RETRYABLE_FAILURE, now=timezone.now(),
                jitter_seconds=self.uniform() * 5,
            )
        dispatch.claims.clear()

    def _request_failed(self, dispatch: Dispatch) -> None:
        """Every interactive failure: a ``failed`` derivative row, and FAILED jobs."""
        record_request_failure(dispatch.entry_id, dispatch.fingerprint, dispatch.kind,
                               dispatch.job.op)
        for claim in dispatch.claims:
            fail_job(claim.lease, error_code=SafeErrorCode.HANDLER_FAILED, now=timezone.now())
        dispatch.claims.clear()

    # -- deadlines and leases -----------------------------------------------------------------

    def _expire(self, now_m: float) -> None:
        if not self.dispatches:
            return
        longest = max(d.job.limits.wall_seconds for d in self.dispatches.values())
        for job_id, dispatch in list(self.dispatches.items()):
            if dispatch.claimed_at is None:
                if not self.scratch.queued(job_id):
                    dispatch.claimed_at = now_m
                    continue
                if now_m - dispatch.dispatched_at <= longest + QUEUE_GRACE_SECONDS:
                    continue
                if not self.scratch.withdraw(job_id):
                    dispatch.claimed_at = now_m
                    continue
            elif now_m - dispatch.claimed_at <= dispatch.job.limits.wall_seconds + (
                RESULT_GRACE_SECONDS
            ):
                continue
            del self.dispatches[job_id]
            LOGGER.error("media job %s: no result (processor_unavailable)", job_id)
            self._failed(dispatch, MediaErrorCode.PROCESSOR_UNAVAILABLE)

    def _renew(self, now_m: float) -> None:
        for dispatch in self.dispatches.values():
            self._renew_one(dispatch, now_m)

    def _renew_one(self, dispatch: Dispatch, now_m: float) -> None:
        if dispatch.asset_lease is not None and not dispatch.lease_lost and (
            now_m >= dispatch.renew_at
        ):
            renewed = renew_asset_lease(dispatch.asset_lease, now=timezone.now())
            if renewed is None:
                # Reset by discovery, or expired: its result can no longer be recorded.
                dispatch.lease_lost = True
            else:
                dispatch.asset_lease = renewed
                dispatch.renew_at = now_m + ASSET_RENEW_SECONDS
        kept: list[InteractiveClaim] = []
        for claim in dispatch.claims:
            if now_m < claim.renew_at:
                kept.append(claim)
            elif renew_lease(claim.lease, now=timezone.now()):
                claim.renew_at = now_m + self._job_lease_renew
                kept.append(claim)
        dispatch.claims = kept

    def _keepalive(self, current: Dispatch) -> bool:
        """Between copy chunks: keep every lease alive; ``False`` abandons the copy."""
        now_m = self.monotonic()
        self._renew_one(current, now_m)
        self._renew(now_m)
        return time.monotonic() < self._copy_deadline

    # -- filling slots --------------------------------------------------------------------

    def _can_dispatch(self, now_m: float) -> bool:
        return (
            self.scratch_ready
            and self.store_ready
            and now_m >= self._paused_until
            and not self.stop_requested()
        )

    def _fill(self, now_m: float) -> None:
        claims = 0
        while (
            self._can_dispatch(now_m)
            and len(self.dispatches) < self.capacity
            and claims < MAX_INTERACTIVE_CLAIMS_PER_TICK
        ):
            lease = claim_next_job(
                WorkerRole.MEDIA.value, self.identity.worker_id, timezone.now(),
            )
            if lease is None:
                break
            claims += 1
            if self.stop_requested():
                relinquish_job(lease, now=timezone.now())
                break
            self._take_interactive(lease, now_m)
        background = sum(1 for d in self.dispatches.values() if not d.interactive)
        free = min(self.capacity - len(self.dispatches), self.background_capacity - background)
        if free <= 0 or not self.roots or not self._can_dispatch(now_m):
            return
        for claimed in claim_assets(
            self.owner, limit=free, root_ids=tuple(self.roots), now=timezone.now(),
        ):
            self._start_stage(claimed, now_m)

    def _take_interactive(self, lease: LeaseToken, now_m: float) -> None:
        from aegis_apps.operations.management.commands import run_role

        if lease.kind not in MEDIA_JOB_KINDS:
            run_role._execute_claim(self.identity, lease)
            return
        job = run_role._live_claim_job(lease, role="media")
        if job is None:
            return
        if not validate_authorization_snapshot(job.operation):
            run_role._settle_unstarted_failure(lease, error_code=SafeErrorCode.AUTHORIZATION_STALE)
            return
        try:
            request = parse_media_request_job(job)
        except ValueError:
            run_role._settle_unstarted_failure(lease, error_code=SafeErrorCode.HANDLER_FAILED)
            return
        op = REQUEST_OPERATIONS[request.kind]
        target = self._request_target(request)
        if target is None:
            # The request no longer matches a processable asset (changed, gone, terminal):
            # the job fails, but nothing was processed, so no failed derivative is recorded.
            run_role._settle_unstarted_failure(lease, error_code=SafeErrorCode.HANDLER_FAILED)
            return
        identity = derivative_identities(request.fingerprint, op, protocol.DEFAULT_PARAMS[op])[0]
        if self._derivative_ready(request.entry_id, identity.key, identity.extension):
            if run_role._start_or_relinquish(lease):
                finish_job(lease, result={"ok": True}, now=timezone.now())
            return
        claim = InteractiveClaim(lease, now_m + self._job_lease_renew)
        joined = next((
            dispatch for dispatch in self.dispatches.values()
            if dispatch.interactive and dispatch.entry_id == request.entry_id
            and dispatch.job.op == op and dispatch.fingerprint == request.fingerprint
        ), None)
        if joined is not None:
            if run_role._start_or_relinquish(lease):
                joined.claims.append(claim)
            return
        bump_to_interactive(request.entry_id, now=timezone.now())
        if not run_role._start_or_relinquish(lease):
            return
        try:
            processor_job = build_job(
                op, kind=target.kind, lane="interactive", slot=target.slot, path=target.path,
                identity=target.identity, policy=self.policy, duration_ms=target.duration_ms,
            )
            self.scratch.submit(processor_job)
        except protocol.ProtocolError:
            # The request cannot be expressed as a job (its path, for one): a content failure.
            record_request_failure(request.entry_id, request.fingerprint, target.kind, op)
            fail_job(lease, error_code=SafeErrorCode.HANDLER_FAILED, now=timezone.now())
            return
        except (OSError, ScratchUnavailable):
            self._scratch_lost()
            retry_job(
                lease, error_code=SafeErrorCode.RETRYABLE_FAILURE, now=timezone.now(),
                jitter_seconds=self.uniform() * 5,
            )
            return
        self.dispatches[processor_job.id] = Dispatch(
            job=processor_job, entry_id=request.entry_id, fingerprint=request.fingerprint,
            kind=target.kind, mtime_ns=target.mtime_ns, attempts=0, dispatched_at=now_m,
            claims=[claim],
        )

    def _request_target(self, request: MediaRequestJob) -> RequestTarget | None:
        """The request's file, if it still is the asset the request was made for."""
        from aegis_apps.catalog.models import CatalogEntry

        slot = self.roots.get(request.root_id)
        if slot is None:
            return None
        row = (
            CatalogEntry.objects.filter(id=request.entry_id, root_id=request.root_id)
            .order_by("id")
            .values(
                "id", "root_id", "kind", "type_hint", "source_state", "size", "mtime_ns",
                "device", "inode",
            )
            .first()
        )
        source = None if row is None else catalog_file(row)
        asset = (
            MediaAsset.objects.filter(entry_id=request.entry_id, root_id=request.root_id)
            .order_by("entry_id")
            .values(
                "kind", "fingerprint", "probe_state", "thumbnail_state", "poster_state",
                "duration_ms", "next_attempt_at",
            )
            .first()
        )
        if source is None or asset is None:
            return None
        if source.fingerprint != asset["fingerprint"]:
            # The file changed since the asset was recorded: re-discover it now.
            rediscover_entry(request.entry_id, now=timezone.now())
            return None
        if (
            asset["fingerprint"] != request.fingerprint
            or asset["kind"] != REQUEST_MEDIA_KIND[request.kind]
            or is_parked(asset["next_attempt_at"])
            or terminal(asset)
        ):
            return None
        path = source_path(request.root_id, request.entry_id)
        if path is None:
            return None
        return RequestTarget(
            slot=slot,
            path=path,
            identity=(int(source.device), int(source.inode), int(source.size),
                      int(source.mtime_ns)),
            kind=str(asset["kind"]),
            duration_ms=asset["duration_ms"],
            mtime_ns=source.mtime_ns,
        )

    def _derivative_ready(self, entry_id: uuid.UUID, key: str, extension: str) -> bool:
        ready = MediaDerivative.objects.filter(
            asset_id=entry_id, key=key, state=DerivativeState.READY,
        ).exists()
        return ready and self.store.exists(key, extension)

    def _start_stage(self, claimed: ClaimedAsset, now_m: float) -> None:
        row, lease = claimed.row, claimed.lease
        now = timezone.now()
        op = next_operation(row)
        if op is None:
            release_asset_lease(lease, now=now)
            return
        slot = self.roots.get(row["root_id"])
        path = None if slot is None else source_path(row["root_id"], row["entry_id"])
        if slot is None or path is None:
            outcome = rediscover_entry(row["entry_id"], now=now)
            if outcome == "unchanged":
                record_failure(
                    lease, op, row["kind"], MediaErrorCode.SOURCE_UNAVAILABLE,
                    attempts=row["attempts"], environment_failures=row["environment_failures"],
                    now=now, uniform=self.uniform,
                )
            return
        try:
            job = build_job(
                op, kind=row["kind"], lane=row["lane"], slot=slot, path=path,
                identity=(int(row["device"]), int(row["inode"]), int(row["size"]),
                          int(row["mtime_ns"])),
                policy=self.policy, duration_ms=row["duration_ms"],
            )
        except protocol.ProtocolError:
            record_unsupported(lease, op, row["kind"], MediaErrorCode.UNSUPPORTED_FORMAT, now=now)
            return
        try:
            self.scratch.submit(job)
        except (OSError, protocol.ProtocolError, ScratchUnavailable):
            self._scratch_lost()
            record_failure(
                lease, op, row["kind"], MediaErrorCode.PROCESSOR_UNAVAILABLE,
                attempts=row["attempts"], environment_failures=row["environment_failures"],
                now=now, uniform=self.uniform,
            )
            return
        self.dispatches[job.id] = Dispatch(
            job=job, entry_id=row["entry_id"], fingerprint=row["fingerprint"],
            kind=row["kind"], mtime_ns=row["mtime_ns"], attempts=row["attempts"],
            dispatched_at=now_m, asset_lease=lease, renew_at=now_m + ASSET_RENEW_SECONDS,
            environment_failures=row["environment_failures"],
        )

    # -- background maintenance -----------------------------------------------------------

    def _discover(self, now_m: float) -> None:
        if self.discovery.due():
            self.discovery.start(tuple(self.roots))
        if self.discovery.active and now_m >= self._discovery_at:
            self.discovery.step(now=timezone.now())
            self._discovery_at = now_m + DISCOVERY_PACE_SECONDS

    def _maintain(self, now_m: float) -> None:
        if not self.store_ready:
            return
        if now_m >= self._eviction_at:
            self._eviction_at = now_m + EVICTION_SECONDS
            report = evict(self.store, budget_bytes=self.policy.cache_budget_bytes)
            if report.deleted:
                LOGGER.info("media eviction: %d derivatives removed", report.deleted)
        if self.recovery.due():
            self.recovery.start()
        if self.recovery.active:
            self.recovery.step(now=timezone.now())

    def _heartbeat(self, now_m: float) -> None:
        if now_m < self._heartbeat_at:
            return
        from aegis_apps.operations.heartbeats import publish_heartbeat

        if now_m >= self._metrics_at:
            self._metrics = media_metrics(
                in_flight=len(self.dispatches), last_error=self.last_error, now=timezone.now(),
            )
            self._metrics_at = now_m + METRICS_SECONDS
        metrics = dict(self._metrics)
        metrics["mediaInFlight"] = len(self.dispatches)
        metrics["mediaProcessorFailures"] = self._breaker_failures
        if self.last_error is not None:
            metrics["mediaLastErrorCode"] = self.last_error
        publish_heartbeat(
            role=WorkerRole.MEDIA.value,
            worker_id=self.identity.worker_id,
            status="running" if self.dispatches else "idle",
            metrics=metrics,
            current_job_id=None,
            release_id=self.identity.release_id,
            schema_identity=self.identity.schema_identity,
            manifest_identity=self.identity.manifest_identity,
        )
        self._heartbeat_at = now_m + self._heartbeat_seconds

    # -- shutdown -------------------------------------------------------------------------

    def shutdown(self, *, wait_seconds: float = SHUTDOWN_WAIT_SECONDS) -> None:
        """Wait a bounded time for jobs in flight, then give every remaining lease back."""
        # Real time: the bound is the container's stop timeout, whatever clock the loop uses.
        # A copy still running at the deadline is abandoned between chunks (_keepalive).
        deadline = time.monotonic() + wait_seconds
        self._copy_deadline = deadline
        while self.dispatches and time.monotonic() < deadline:
            with _best_effort("collect"):
                self._collect()
            if self.dispatches:
                time.sleep(min(BUSY_TICK_SECONDS, max(deadline - time.monotonic(), 0.0)))
        for job_id, dispatch in list(self.dispatches.items()):
            with _best_effort("release"):
                self.scratch.withdraw(job_id)
                now = timezone.now()
                if dispatch.asset_lease is not None and not dispatch.lease_lost:
                    release_asset_lease(dispatch.asset_lease, now=now)
                for claim in dispatch.claims:
                    retry_job(
                        claim.lease, error_code=SafeErrorCode.RETRYABLE_FAILURE, now=now,
                        jitter_seconds=0,
                    )
                dispatch.claims.clear()
        self.dispatches.clear()
        self.scratch.close()
        self.store.close()


@contextmanager
def _best_effort(step: str) -> Iterator[None]:
    try:
        yield
    except Exception:
        LOGGER.error("media worker: shutdown step failed (%s)", step)


def run_media_worker(
    identity: WorkerIdentity,
    stop_requested: Callable[[], bool],
    *,
    scratch_dir: str | None = None,
    derivatives_dir: str | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """The ``run_role --role media`` loop, after the role's startup checks and heartbeat."""
    worker = MediaWorker(
        identity,
        stop_requested=stop_requested,
        policy=settings.AEGIS_MEDIA_POLICY,
        scratch=Scratch(scratch_dir or SCRATCH_DIR),
        store=DerivativeStore(derivatives_dir or DERIVATIVES_DIR),
        sleep=sleep,
    )
    worker.run()
