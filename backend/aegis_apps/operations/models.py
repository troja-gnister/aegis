from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, ClassVar

from django.conf import settings
from django.db import models
from django.db.models.expressions import RawSQL

from .enums import HeartbeatStatus, JobKind, JobState, SafeErrorCode, WorkerRole

MIN_JOB_PRIORITY = -32_768
MAX_JOB_PRIORITY = 32_767
MAX_JOB_ATTEMPTS = 100
UNCONFIGURED_MANIFEST_IDENTITY = "unconfigured:v1"

IMMUTABLE_JOB_FIELDS = frozenset(
    {"operation", "operation_id", "target_role", "kind", "payload", "priority", "max_attempts"}
)
MUTABLE_JOB_FIELDS = frozenset(
    {
        "state",
        "available_at",
        "attempts",
        "attempt_token",
        "lease_owner",
        "lease_expires_at",
        "safe_error_code",
        "safe_error_detail",
        "result",
        "updated_at",
    }
)

_JOB_EXECUTION_CAPABILITY = object()
_job_execution_capability: ContextVar[object | None] = ContextVar(
    "aegis_job_execution_capability", default=None
)


def _operation_immutable() -> PermissionError:
    return PermissionError("operation records are immutable after insertion")


def _operation_deletion_disabled() -> PermissionError:
    return PermissionError("operation deletion is disabled")


def _bulk_conflict_updates_disabled(record_type: str) -> PermissionError:
    return PermissionError(f"{record_type} bulk conflict updates are disabled")


def _job_immutable() -> PermissionError:
    return PermissionError("job immutable fields cannot be changed")


def _job_execution_guarded() -> PermissionError:
    return PermissionError("job execution fields require the fenced lease service")


def _job_deletion_disabled() -> PermissionError:
    return PermissionError("durable job deletion is disabled")


@contextmanager
def _allow_job_execution_updates() -> Iterator[None]:
    token = _job_execution_capability.set(_JOB_EXECUTION_CAPABILITY)
    try:
        yield
    finally:
        _job_execution_capability.reset(token)


class OperationQuerySet(models.QuerySet["Operation"]):
    def update(self, **kwargs: Any) -> int:
        del kwargs
        raise _operation_immutable()

    def delete(self) -> tuple[int, dict[str, int]]:
        raise _operation_deletion_disabled()

    def bulk_create(
        self,
        objs: Iterable[Operation],
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_conflicts: bool = False,
        update_fields: Collection[str] | None = None,
        unique_fields: Collection[str] | None = None,
    ) -> list[Operation]:
        if update_conflicts:
            raise _bulk_conflict_updates_disabled("operation")
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=False,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )

    async def abulk_create(
        self,
        objs: Iterable[Operation],
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_conflicts: bool = False,
        update_fields: Collection[str] | None = None,
        unique_fields: Collection[str] | None = None,
    ) -> list[Operation]:
        if update_conflicts:
            raise _bulk_conflict_updates_disabled("operation")
        return await super().abulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=False,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )

    def bulk_update(
        self,
        objs: Iterable[Operation],
        fields: Iterable[str],
        batch_size: int | None = None,
    ) -> int:
        del objs, fields, batch_size
        raise _operation_immutable()

    async def abulk_update(
        self,
        objs: Iterable[Operation],
        fields: Iterable[str],
        batch_size: int | None = None,
    ) -> int:
        del objs, fields, batch_size
        raise _operation_immutable()


class Operation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.PROTECT,
        related_name="operations",
    )
    request_id = models.CharField(max_length=64, db_index=True)
    kind = models.CharField(max_length=64, choices=JobKind.choices)
    request_hash = models.BinaryField(max_length=32)
    intent = models.JSONField()
    authorization_snapshot = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    objects = OperationQuerySet.as_manager()

    class Meta:
        base_manager_name = "objects"
        default_manager_name = "objects"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("actor", "request_id"),
                name="operations_operation_actor_request_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=JobKind.values),
                name="operations_operation_kind_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(request_id__regex=r"^[A-Za-z0-9_-]{8,64}$"),
                name="operations_operation_request_id_valid",
            ),
            models.CheckConstraint(
                condition=RawSQL(
                    "octet_length(request_hash) = 32",
                    (),
                    output_field=models.BooleanField(),
                ),
                name="operations_operation_hash_32_bytes",
            ),
            models.CheckConstraint(
                condition=RawSQL(
                    "jsonb_typeof(intent) = 'object' AND "
                    "jsonb_typeof(authorization_snapshot) = 'object'",
                    (),
                    output_field=models.BooleanField(),
                ),
                name="operations_operation_json_objects",
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise _operation_immutable()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        del args, kwargs
        raise _operation_deletion_disabled()


class JobQuerySet(models.QuerySet["Job"]):
    def update(self, **kwargs: Any) -> int:
        fields = frozenset(kwargs)
        if fields & IMMUTABLE_JOB_FIELDS or fields - MUTABLE_JOB_FIELDS:
            raise _job_immutable()
        if _job_execution_capability.get() is not _JOB_EXECUTION_CAPABILITY:
            raise _job_execution_guarded()
        return super().update(**kwargs)

    def delete(self) -> tuple[int, dict[str, int]]:
        raise _job_deletion_disabled()

    def bulk_create(
        self,
        objs: Iterable[Job],
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_conflicts: bool = False,
        update_fields: Collection[str] | None = None,
        unique_fields: Collection[str] | None = None,
    ) -> list[Job]:
        if update_conflicts:
            raise _bulk_conflict_updates_disabled("job")
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=False,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )

    async def abulk_create(
        self,
        objs: Iterable[Job],
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_conflicts: bool = False,
        update_fields: Collection[str] | None = None,
        unique_fields: Collection[str] | None = None,
    ) -> list[Job]:
        if update_conflicts:
            raise _bulk_conflict_updates_disabled("job")
        return await super().abulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=False,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )

    def bulk_update(
        self,
        objs: Iterable[Job],
        fields: Iterable[str],
        batch_size: int | None = None,
    ) -> int:
        field_names = frozenset(fields)
        if field_names & IMMUTABLE_JOB_FIELDS or field_names - MUTABLE_JOB_FIELDS:
            raise _job_immutable()
        if _job_execution_capability.get() is not _JOB_EXECUTION_CAPABILITY:
            raise _job_execution_guarded()
        return super().bulk_update(objs, field_names, batch_size=batch_size)

    async def abulk_update(
        self,
        objs: Iterable[Job],
        fields: Iterable[str],
        batch_size: int | None = None,
    ) -> int:
        field_names = frozenset(fields)
        if field_names & IMMUTABLE_JOB_FIELDS or field_names - MUTABLE_JOB_FIELDS:
            raise _job_immutable()
        if _job_execution_capability.get() is not _JOB_EXECUTION_CAPABILITY:
            raise _job_execution_guarded()
        return await super().abulk_update(objs, field_names, batch_size=batch_size)


class Job(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operation = models.ForeignKey(
        Operation,
        on_delete=models.PROTECT,
        related_name="jobs",
    )
    target_role = models.CharField(max_length=16, choices=WorkerRole.choices)
    kind = models.CharField(max_length=64, choices=JobKind.choices)
    payload = models.JSONField()
    priority = models.SmallIntegerField(default=0)
    state = models.CharField(max_length=16, choices=JobState.choices, default=JobState.QUEUED)
    available_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    attempt_token = models.PositiveBigIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=5)
    lease_owner = models.CharField(max_length=36, null=True)
    lease_expires_at = models.DateTimeField(null=True)
    safe_error_code = models.CharField(max_length=64, null=True)
    safe_error_detail = models.CharField(max_length=512, null=True)
    result = models.JSONField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = JobQuerySet.as_manager()

    class Meta:
        base_manager_name = "objects"
        default_manager_name = "objects"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("operation", "target_role"),
                name="operations_job_operation_role_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(target_role__in=WorkerRole.values),
                name="operations_job_role_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=JobKind.values),
                name="operations_job_kind_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=JobState.values),
                name="operations_job_state_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(priority__gte=MIN_JOB_PRIORITY, priority__lte=MAX_JOB_PRIORITY),
                name="operations_job_priority_bounded",
            ),
            models.CheckConstraint(
                condition=models.Q(max_attempts__gte=1, max_attempts__lte=MAX_JOB_ATTEMPTS),
                name="operations_job_max_attempts_bounded",
            ),
            models.CheckConstraint(
                condition=models.Q(attempts__gte=0)
                & models.Q(attempts__lte=models.F("max_attempts")),
                name="operations_job_attempts_bounded",
            ),
            models.CheckConstraint(
                condition=models.Q(attempt_token__gte=models.F("attempts")),
                name="operations_job_attempt_token_monotonic",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state=JobState.RUNNING,
                        lease_owner__isnull=False,
                        lease_expires_at__isnull=False,
                    )
                    | (
                        ~models.Q(state=JobState.RUNNING)
                        & models.Q(lease_owner__isnull=True, lease_expires_at__isnull=True)
                    )
                ),
                name="operations_job_running_lease_shape",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(lease_owner__isnull=True)
                    | models.Q(
                        lease_owner__regex=(
                            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                            r"[0-9a-f]{4}-[0-9a-f]{12}$"
                        )
                    )
                ),
                name="operations_job_lease_owner_uuid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(safe_error_code__isnull=True)
                    | models.Q(safe_error_code__in=SafeErrorCode.values)
                ),
                name="operations_job_error_code_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state__in=(JobState.FAILED, JobState.RETRY_WAIT),
                        safe_error_code__isnull=False,
                        safe_error_detail__isnull=False,
                    )
                    | models.Q(
                        state__in=(JobState.QUEUED, JobState.RUNNING, JobState.SUCCEEDED),
                        safe_error_code__isnull=True,
                        safe_error_detail__isnull=True,
                    )
                ),
                name="operations_job_error_state_shape",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(state=JobState.SUCCEEDED, result__isnull=False)
                    | (~models.Q(state=JobState.SUCCEEDED) & models.Q(result__isnull=True))
                ),
                name="operations_job_result_state_shape",
            ),
            models.CheckConstraint(
                condition=RawSQL(
                    "jsonb_typeof(payload) = 'object' AND "
                    "(result IS NULL OR jsonb_typeof(result) = 'object')",
                    (),
                    output_field=models.BooleanField(),
                ),
                name="operations_job_json_objects",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("target_role", "state", "-priority", "available_at", "id"),
                name="operations_job_claim_idx",
            ),
            models.Index(fields=("lease_expires_at",), name="operations_job_lease_idx"),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise PermissionError(
                "job immutable fields and execution state require the fenced lease service"
            )
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        del args, kwargs
        raise _job_deletion_disabled()


class WorkerHeartbeat(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.CharField(max_length=16, choices=WorkerRole.choices)
    worker_id = models.CharField(max_length=36)
    release_id = models.CharField(max_length=96)
    schema_identity = models.CharField(max_length=96)
    manifest_identity = models.CharField(max_length=64)
    last_seen_at = models.DateTimeField()
    current_job_id = models.UUIDField(null=True)
    status = models.CharField(max_length=16, choices=HeartbeatStatus.choices)
    metrics = models.JSONField(default=dict)

    class Meta:
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("role", "last_seen_at"),
                name="operations_hb_role_seen_idx",
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("role", "worker_id"),
                name="operations_heartbeat_role_worker_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(role__in=WorkerRole.values),
                name="operations_heartbeat_role_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=HeartbeatStatus.values),
                name="operations_heartbeat_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    worker_id__regex=(
                        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                        r"[0-9a-f]{4}-[0-9a-f]{12}$"
                    )
                ),
                name="operations_heartbeat_worker_uuid",
            ),
            models.CheckConstraint(
                condition=models.Q(release_id__regex=r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,95}$"),
                name="operations_heartbeat_release_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    schema_identity__regex=r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,95}$"
                ),
                name="operations_heartbeat_schema_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(manifest_identity=UNCONFIGURED_MANIFEST_IDENTITY)
                    | models.Q(manifest_identity__regex=r"^[0-9a-f]{64}$")
                ),
                name="operations_heartbeat_manifest_valid",
            ),
            models.CheckConstraint(
                condition=RawSQL(
                    "jsonb_typeof(metrics) = 'object'",
                    (),
                    output_field=models.BooleanField(),
                ),
                name="operations_heartbeat_metrics_object",
            ),
        ]
