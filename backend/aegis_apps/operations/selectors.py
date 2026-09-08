from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Final, TypedDict

from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Count, FloatField, Max, Min, Q
from django.db.models.functions import Cast
from django.utils import timezone

from aegis_apps.roots.manifest import configured_manifest

from .config import WORKER_ROLES
from .enums import HeartbeatStatus, JobState
from .models import UNCONFIGURED_MANIFEST_IDENTITY, Job, WorkerHeartbeat

MAX_PUBLIC_JOB_COUNT: Final = 2_147_483_647
MAX_PUBLIC_QUEUE_AGE_SECONDS: Final = 2_147_483_647


class SchemaCompatibilityError(RuntimeError):
    """The running code and applied migration graph do not match."""


class OperationRoleStatus(TypedDict):
    role: str
    state: str
    jobCount: int
    oldestQueueAgeSeconds: int | None
    scanProgress: float | None
    diskPressure: str


def _validated_roles(values: Sequence[str]) -> tuple[str, ...]:
    roles = tuple(values)
    if (
        isinstance(values, (str, bytes))
        or not roles
        or any(role not in WORKER_ROLES for role in roles)
        or len(set(roles)) != len(roles)
    ):
        raise ValueError("invalid required worker roles")
    return roles


def _aware_now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or not timezone.is_aware(value):
        raise ValueError("invalid worker status time")
    return value


def _freshness(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid worker heartbeat freshness")
    seconds = float(value)
    if not math.isfinite(seconds) or not 0 < seconds <= 300:
        raise ValueError("invalid worker heartbeat freshness")
    return seconds


def current_schema_identity() -> str:
    connection.ensure_connection()
    executor = MigrationExecutor(connection)
    targets = sorted(executor.loader.graph.leaf_nodes())
    if executor.migration_plan(targets):
        raise SchemaCompatibilityError("migrations pending")
    encoded = b"aegis.schema-identity.v1\x00" + b"".join(
        app.encode("ascii") + b":" + name.encode("ascii") + b"\x00"
        for app, name in targets
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def current_manifest_identity() -> str:
    manifest = configured_manifest()
    return UNCONFIGURED_MANIFEST_IDENTITY if manifest is None else manifest.digest


def worker_role_states(
    *,
    roles: Sequence[str],
    release_id: str,
    schema_identity: str,
    manifest_identity: str,
    now: datetime,
    freshness_seconds: float,
) -> dict[str, str]:
    required_roles = _validated_roles(roles)
    status_time = _aware_now(now)
    fresh_for = _freshness(freshness_seconds)
    cutoff = status_time - timedelta(seconds=fresh_for)
    rows = (
        WorkerHeartbeat.objects.filter(role__in=required_roles)
        .values("role")
        .annotate(
            total=Count("id"),
            healthy=Count(
                "id",
                filter=Q(
                    last_seen_at__gte=cutoff,
                    status__in=(HeartbeatStatus.IDLE, HeartbeatStatus.RUNNING),
                    release_id=release_id,
                    schema_identity=schema_identity,
                    manifest_identity=manifest_identity,
                ),
            ),
        )
    )
    counts = {row["role"]: (row["total"], row["healthy"]) for row in rows}
    return {
        role: (
            "missing"
            if role not in counts
            else "healthy"
            if counts[role][1] > 0
            else "stale"
        )
        for role in required_roles
    }


def _disk_pressure(value: object) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "unknown"
    pressure = float(value)
    if not math.isfinite(pressure) or not 0 <= pressure <= 1:
        return "unknown"
    if pressure >= 0.9:
        return "critical"
    if pressure >= 0.8:
        return "warning"
    return "ok"


def operations_status(*, now: datetime | None = None) -> list[OperationRoleStatus]:
    status_time = timezone.now() if now is None else _aware_now(now)
    roles = _validated_roles(settings.AEGIS_REQUIRED_WORKER_ROLES)
    release_id = settings.AEGIS_RELEASE_ID
    schema_identity = current_schema_identity()
    manifest_identity = current_manifest_identity()
    freshness_seconds = _freshness(settings.AEGIS_WORKER_HEARTBEAT_FRESH_SECONDS)
    states = worker_role_states(
        roles=roles,
        release_id=release_id,
        schema_identity=schema_identity,
        manifest_identity=manifest_identity,
        now=status_time,
        freshness_seconds=freshness_seconds,
    )

    active_states = (JobState.QUEUED, JobState.RETRY_WAIT, JobState.RUNNING)
    queued_states = (JobState.QUEUED, JobState.RETRY_WAIT)
    job_rows = (
        Job.objects.filter(target_role__in=roles, state__in=active_states)
        .values("target_role")
        .annotate(
            job_count=Count("id"),
            oldest_queued=Min("created_at", filter=Q(state__in=queued_states)),
        )
    )
    jobs = {row["target_role"]: row for row in job_rows}

    cutoff = status_time - timedelta(seconds=freshness_seconds)
    metric_rows = (
        WorkerHeartbeat.objects.filter(
            role__in=roles,
            last_seen_at__gte=cutoff,
            status__in=(HeartbeatStatus.IDLE, HeartbeatStatus.RUNNING),
            release_id=release_id,
            schema_identity=schema_identity,
            manifest_identity=manifest_identity,
        )
        .values("role")
        .annotate(
            scan_progress=Max(
                Cast("metrics__scanProgress", FloatField()),
                filter=Q(metrics__has_key="scanProgress"),
            ),
            disk_pressure=Max(
                Cast("metrics__diskPressure", FloatField()),
                filter=Q(metrics__has_key="diskPressure"),
            ),
        )
    )
    metrics = {row["role"]: row for row in metric_rows}

    result: list[OperationRoleStatus] = []
    for role in roles:
        job_data = jobs.get(role)
        count = 0 if job_data is None else min(job_data["job_count"], MAX_PUBLIC_JOB_COUNT)
        oldest = None if job_data is None else job_data["oldest_queued"]
        age = (
            None
            if oldest is None
            else min(
                max(0, int((status_time - oldest).total_seconds())),
                MAX_PUBLIC_QUEUE_AGE_SECONDS,
            )
        )
        metric_data = metrics.get(role, {})
        scan = metric_data.get("scan_progress")
        safe_scan = (
            float(scan)
            if isinstance(scan, (int, float))
            and not isinstance(scan, bool)
            and math.isfinite(float(scan))
            and 0 <= float(scan) <= 1
            else None
        )
        result.append(
            {
                "role": role,
                "state": states[role],
                "jobCount": count,
                "oldestQueueAgeSeconds": age,
                "scanProgress": safe_scan,
                "diskPressure": _disk_pressure(metric_data.get("disk_pressure")),
            }
        )
    return result
