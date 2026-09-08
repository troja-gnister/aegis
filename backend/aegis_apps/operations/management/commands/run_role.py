from __future__ import annotations

import logging
import math
import secrets
import signal
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Event
from types import FrameType
from typing import Any, Final, Literal, cast

from aegisctl.mounts import attest_mounts
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from aegis_apps.operations.enums import HeartbeatStatus, JobState, SafeErrorCode, WorkerRole
from aegis_apps.operations.heartbeats import publish_heartbeat
from aegis_apps.operations.leases import LeaseToken, claim_next_job, fail_job, finish_job
from aegis_apps.operations.models import UNCONFIGURED_MANIFEST_IDENTITY, Job
from aegis_apps.operations.selectors import current_schema_identity
from aegis_apps.operations.serializers import canonical_worker_id
from aegis_apps.operations.services import validate_authorization_snapshot
from aegis_apps.roots.manifest import configured_manifest

logger = logging.getLogger(__name__)

WorkerHandler = Callable[[LeaseToken], Mapping[str, object]]
RoleName = Literal["operations", "indexer", "media"]
MAX_HEARTBEAT_SECONDS: Final = 15.0
MAX_POLL_SECONDS: Final = 30.0
MAX_POLL_JITTER_SECONDS: Final = 30.0

_shutdown = Event()


@dataclass(frozen=True, slots=True)
class WorkerIdentity:
    role: RoleName
    worker_id: str
    release_id: str
    schema_identity: str
    manifest_identity: str


def run_foundation_probe(lease: LeaseToken) -> dict[str, object]:
    del lease
    return {"ok": True}


ROLE_HANDLERS: dict[str, dict[str, WorkerHandler]] = {
    "operations": {"foundation.probe": run_foundation_probe},
    "indexer": {"foundation.probe": run_foundation_probe},
    "media": {"foundation.probe": run_foundation_probe},
}


def request_shutdown() -> None:
    _shutdown.set()


def _reset_shutdown() -> None:
    _shutdown.clear()


def _shutdown_requested() -> bool:
    return _shutdown.is_set()


def _wait_for_shutdown(timeout: float) -> None:
    _shutdown.wait(timeout)


def _handle_signal(signum: int, frame: FrameType | None) -> None:
    del signum, frame
    request_shutdown()


@contextmanager
def _installed_signal_handlers() -> Iterator[None]:
    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)


def _role(value: object) -> RoleName:
    if isinstance(value, WorkerRole):
        return cast(RoleName, value.value)
    if type(value) is str:
        try:
            return cast(RoleName, WorkerRole(value).value)
        except ValueError:
            pass
    raise ValueError("invalid worker role")


def _new_worker_id() -> str:
    return canonical_worker_id(str(uuid.uuid4()))


def _bounded_seconds(
    value: object,
    *,
    field_name: str,
    maximum: float,
    allow_zero: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid {field_name}")
    seconds = float(value)
    lower_valid = seconds >= 0 if allow_zero else seconds > 0
    if not math.isfinite(seconds) or not lower_valid or seconds > maximum:
        raise ValueError(f"invalid {field_name}")
    return seconds


def _publish(
    identity: WorkerIdentity,
    *,
    status: HeartbeatStatus,
    job_id: uuid.UUID | None,
) -> None:
    publish_heartbeat(
        role=identity.role,
        worker_id=identity.worker_id,
        status=status,
        current_job_id=job_id,
        release_id=identity.release_id,
        schema_identity=identity.schema_identity,
        manifest_identity=identity.manifest_identity,
    )


def _startup(*, role: object, worker_id: str) -> WorkerIdentity:
    worker_role = _role(role)
    configured_role = settings.AEGIS_PROCESS_ROLE
    if configured_role is not None and configured_role != worker_role:
        raise ValueError("configured process role does not match command role")

    manifest = configured_manifest()
    if manifest is None:
        manifest_identity = UNCONFIGURED_MANIFEST_IDENTITY
    else:
        attest_mounts(manifest, worker_role)
        manifest_identity = manifest.digest

    schema_identity = current_schema_identity()
    identity = WorkerIdentity(
        role=worker_role,
        worker_id=worker_id,
        release_id=settings.AEGIS_RELEASE_ID,
        schema_identity=schema_identity,
        manifest_identity=manifest_identity,
    )
    _publish(identity, status=HeartbeatStatus.IDLE, job_id=None)
    return identity


def _live_claim_job(lease: LeaseToken, *, role: RoleName) -> Job | None:
    now = timezone.now()
    return (
        Job.objects.select_related("operation")
        .filter(
            pk=lease.job_id,
            target_role=role,
            kind=lease.kind,
            state=JobState.RUNNING,
            attempt_token=lease.attempt_token,
            lease_owner=lease.worker_id,
            lease_expires_at__gt=now,
        )
        .first()
    )


def _execute_claim(identity: WorkerIdentity, lease: LeaseToken) -> None:
    job = _live_claim_job(lease, role=identity.role)
    if job is None:
        return
    if not validate_authorization_snapshot(job.operation):
        fail_job(
            lease,
            error_code=SafeErrorCode.AUTHORIZATION_STALE,
            now=timezone.now(),
        )
        return

    handler = ROLE_HANDLERS[identity.role].get(lease.kind)
    if handler is None:
        fail_job(
            lease,
            error_code=SafeErrorCode.HANDLER_FAILED,
            now=timezone.now(),
        )
        return

    _publish(identity, status=HeartbeatStatus.RUNNING, job_id=lease.job_id)
    try:
        result = handler(lease)
    except Exception:
        logger.error(
            "Worker handler failed safely",
            extra={"event": "operations.worker.handler", "error_code": "HANDLER_FAILED"},
        )
        fail_job(
            lease,
            error_code=SafeErrorCode.HANDLER_FAILED,
            now=timezone.now(),
        )
        return
    finish_job(lease, result=result, now=timezone.now())


def run_worker(
    *,
    role: object,
    once: bool,
    worker_id: str,
    sleep: Callable[[float], None] = _wait_for_shutdown,
    monotonic: Callable[[], float] = time.monotonic,
    jitter: Callable[[float, float], float] | None = None,
) -> None:
    heartbeat_seconds = _bounded_seconds(
        settings.AEGIS_WORKER_HEARTBEAT_SECONDS,
        field_name="heartbeat interval",
        maximum=MAX_HEARTBEAT_SECONDS,
    )
    poll_seconds = _bounded_seconds(
        settings.AEGIS_QUEUE_POLL_SECONDS,
        field_name="poll interval",
        maximum=MAX_POLL_SECONDS,
    )
    poll_jitter_seconds = _bounded_seconds(
        settings.AEGIS_QUEUE_POLL_JITTER_SECONDS,
        field_name="poll jitter",
        maximum=MAX_POLL_JITTER_SECONDS,
        allow_zero=True,
    )
    if poll_jitter_seconds > poll_seconds:
        raise ValueError("invalid poll jitter")
    identity = _startup(role=role, worker_id=worker_id)
    random_jitter = secrets.SystemRandom().uniform if jitter is None else jitter
    next_heartbeat = monotonic() + heartbeat_seconds

    try:
        while not _shutdown_requested():
            monotonic_now = monotonic()
            if monotonic_now >= next_heartbeat:
                _publish(identity, status=HeartbeatStatus.IDLE, job_id=None)
                next_heartbeat = monotonic_now + heartbeat_seconds

            lease = claim_next_job(identity.role, identity.worker_id, timezone.now())
            if lease is not None:
                _execute_claim(identity, lease)
                _publish(identity, status=HeartbeatStatus.IDLE, job_id=None)
                next_heartbeat = monotonic() + heartbeat_seconds

            if once or _shutdown_requested():
                break

            delay = poll_seconds + random_jitter(0.0, poll_jitter_seconds)
            until_heartbeat = max(0.0, next_heartbeat - monotonic())
            if until_heartbeat == 0:
                continue
            sleep(min(delay, until_heartbeat))
    finally:
        _publish(identity, status=HeartbeatStatus.STOPPING, job_id=None)


class Command(BaseCommand):
    help = "Run one fixed-role durable operations worker."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--role", required=True, choices=tuple(ROLE_HANDLERS))
        parser.add_argument("--once", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        _reset_shutdown()
        try:
            worker_id = _new_worker_id()
            with _installed_signal_handlers():
                run_worker(
                    role=options.get("role"),
                    once=options.get("once") is True,
                    worker_id=worker_id,
                )
        except Exception:
            raise CommandError("worker startup failed") from None
