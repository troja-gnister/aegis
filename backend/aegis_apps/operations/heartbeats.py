from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from typing import Final

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from .enums import HeartbeatStatus, JobState, WorkerRole
from .models import UNCONFIGURED_MANIFEST_IDENTITY, Job, WorkerHeartbeat
from .selectors import (
    authoritative_database_time,
    current_manifest_identity,
    current_schema_identity,
)
from .serializers import (
    canonical_worker_id,
    validate_heartbeat_metrics,
    validate_safe_identity,
)

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOCATION_LOCK_NAMESPACE: Final = 0x41454749
_ALLOCATION_LOCK_KEYS: Final = {
    WorkerRole.OPERATIONS: 1,
    WorkerRole.INDEXER: 2,
    WorkerRole.MEDIA: 3,
}


class HeartbeatCapacityError(RuntimeError):
    """No expired slot is available for a new worker in this role."""


def _role(value: object) -> WorkerRole:
    if isinstance(value, WorkerRole):
        return value
    if type(value) is str:
        try:
            return WorkerRole(value)
        except ValueError:
            pass
    raise ValueError("invalid worker role")


def _status(value: object) -> HeartbeatStatus:
    if isinstance(value, HeartbeatStatus):
        return value
    if type(value) is str:
        try:
            return HeartbeatStatus(value)
        except ValueError:
            pass
    raise ValueError("invalid heartbeat status")


def _manifest_identity(value: object) -> str:
    if value == UNCONFIGURED_MANIFEST_IDENTITY:
        return UNCONFIGURED_MANIFEST_IDENTITY
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError("invalid manifest identity")
    return value


def _heartbeat_time(value: object) -> datetime:
    if not isinstance(value, datetime) or not timezone.is_aware(value):
        raise ValueError("invalid heartbeat time")
    return value


def _retention_seconds() -> float:
    value = settings.AEGIS_WORKER_HEARTBEAT_RETENTION_SECONDS
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid worker heartbeat retention")
    seconds = float(value)
    if not 0 < seconds <= 604_800:
        raise ValueError("invalid worker heartbeat retention")
    return seconds


def _slot_limit() -> int:
    value = settings.AEGIS_WORKER_HEARTBEAT_SLOTS_PER_ROLE
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_024:
        raise ValueError("invalid worker heartbeat slot limit")
    return value


def _lock_role_allocation(role: WorkerRole) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (_ALLOCATION_LOCK_NAMESPACE, _ALLOCATION_LOCK_KEYS[role]),
        )


def _current_job_id(value: object, *, role: WorkerRole, worker_id: str) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        job_id = value
    elif isinstance(value, str):
        try:
            job_id = uuid.UUID(canonical_worker_id(value))
        except ValueError:
            raise ValueError("invalid current job ID") from None
    else:
        raise ValueError("invalid current job ID")
    if not Job.objects.filter(
        pk=job_id,
        target_role=role,
        state=JobState.RUNNING,
        lease_owner=worker_id,
    ).exists():
        raise ValueError("invalid current job ID")
    return job_id


def _publish_heartbeat(
    *,
    role: str,
    worker_id: str,
    status: str = HeartbeatStatus.IDLE,
    metrics: object = None,
    current_job_id: uuid.UUID | str | None = None,
    release_id: str | None = None,
    schema_identity: str | None = None,
    manifest_identity: str | None = None,
    observed_at: datetime | None,
) -> WorkerHeartbeat:
    worker_role = _role(role)
    canonical_id = canonical_worker_id(worker_id)
    heartbeat_status = _status(status)
    normalized_metrics = validate_heartbeat_metrics({} if metrics is None else metrics)
    bounded_release = validate_safe_identity(
        settings.AEGIS_RELEASE_ID if release_id is None else release_id,
        field_name="release ID",
    )
    bounded_schema = validate_safe_identity(
        current_schema_identity() if schema_identity is None else schema_identity,
        field_name="schema identity",
    )
    bounded_manifest = _manifest_identity(
        current_manifest_identity() if manifest_identity is None else manifest_identity
    )
    job_id = _current_job_id(
        current_job_id,
        role=worker_role,
        worker_id=canonical_id,
    )

    with transaction.atomic():
        heartbeat_time = _heartbeat_time(
            authoritative_database_time() if observed_at is None else observed_at
        )
        values = {
            "release_id": bounded_release,
            "schema_identity": bounded_schema,
            "manifest_identity": bounded_manifest,
            "last_seen_at": heartbeat_time,
            "current_job_id": job_id,
            "status": heartbeat_status,
            "metrics": normalized_metrics,
        }
        heartbeat_rows = WorkerHeartbeat.objects.filter(
            role=worker_role,
            worker_id=canonical_id,
        )
        heartbeat_rows.filter(last_seen_at__lt=heartbeat_time).update(**values)
        heartbeat = heartbeat_rows.first()
        if heartbeat is not None:
            return heartbeat

        _lock_role_allocation(worker_role)
        heartbeat_rows.filter(last_seen_at__lt=heartbeat_time).update(**values)
        heartbeat = heartbeat_rows.first()
        if heartbeat is not None:
            return heartbeat

        retention_cutoff = heartbeat_time - timedelta(seconds=_retention_seconds())
        role_rows = WorkerHeartbeat.objects.filter(role=worker_role)
        reusable = (
            role_rows.select_for_update()
            .filter(last_seen_at__lt=retention_cutoff)
            .order_by("last_seen_at", "id")
            .first()
        )
        if reusable is not None:
            WorkerHeartbeat.objects.filter(pk=reusable.pk).update(
                worker_id=canonical_id,
                **values,
            )
            reusable.refresh_from_db()
            return reusable

        slot_limit = _slot_limit()
        occupied = list(role_rows.values_list("pk", flat=True)[:slot_limit])
        if len(occupied) >= slot_limit:
            raise HeartbeatCapacityError("worker heartbeat capacity exhausted")
        return WorkerHeartbeat.objects.create(
            role=worker_role,
            worker_id=canonical_id,
            **values,
        )


def publish_heartbeat(
    *,
    role: str,
    worker_id: str,
    status: str = HeartbeatStatus.IDLE,
    metrics: object = None,
    current_job_id: uuid.UUID | str | None = None,
    release_id: str | None = None,
    schema_identity: str | None = None,
    manifest_identity: str | None = None,
) -> WorkerHeartbeat:
    return _publish_heartbeat(
        role=role,
        worker_id=worker_id,
        status=status,
        metrics=metrics,
        current_job_id=current_job_id,
        release_id=release_id,
        schema_identity=schema_identity,
        manifest_identity=manifest_identity,
        observed_at=None,
    )


def publish_heartbeat_for_test(
    *,
    role: str,
    worker_id: str,
    observed_at: datetime,
    status: str = HeartbeatStatus.IDLE,
    metrics: object = None,
    current_job_id: uuid.UUID | str | None = None,
    release_id: str | None = None,
    schema_identity: str | None = None,
    manifest_identity: str | None = None,
) -> WorkerHeartbeat:
    if settings.AEGIS_ENVIRONMENT != "test":
        raise PermissionError("heartbeat time injection is test-only")
    return _publish_heartbeat(
        role=role,
        worker_id=worker_id,
        status=status,
        metrics=metrics,
        current_job_id=current_job_id,
        release_id=release_id,
        schema_identity=schema_identity,
        manifest_identity=manifest_identity,
        observed_at=observed_at,
    )
