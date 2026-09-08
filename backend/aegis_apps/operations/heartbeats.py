from __future__ import annotations

import re
import uuid
from datetime import datetime

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .enums import HeartbeatStatus, JobState, WorkerRole
from .models import UNCONFIGURED_MANIFEST_IDENTITY, Job, WorkerHeartbeat
from .selectors import current_manifest_identity, current_schema_identity
from .serializers import (
    canonical_worker_id,
    validate_heartbeat_metrics,
    validate_safe_identity,
)

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


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
    now: datetime | None = None,
) -> WorkerHeartbeat:
    worker_role = _role(role)
    canonical_id = canonical_worker_id(worker_id)
    heartbeat_status = _status(status)
    normalized_metrics = validate_heartbeat_metrics({} if metrics is None else metrics)
    heartbeat_time = _heartbeat_time(timezone.now() if now is None else now)
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
        heartbeat, _created = WorkerHeartbeat.objects.update_or_create(
            role=worker_role,
            worker_id=canonical_id,
            defaults={
                "release_id": bounded_release,
                "schema_identity": bounded_schema,
                "manifest_identity": bounded_manifest,
                "last_seen_at": heartbeat_time,
                "current_job_id": job_id,
                "status": heartbeat_status,
                "metrics": normalized_metrics,
            },
        )
    return heartbeat
