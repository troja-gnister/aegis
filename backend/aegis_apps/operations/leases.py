from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .enums import JobKind, JobState, SafeErrorCode, WorkerRole
from .models import Job, Operation, _allow_job_execution_updates
from .serializers import canonical_worker_id, validate_probe_result
from .services import validate_authorization_snapshot

DEFAULT_LEASE_SECONDS: Final = 30.0
MAX_LEASE_SECONDS: Final = 300.0
DEFAULT_RETRY_BASE_SECONDS: Final = 1.0
DEFAULT_RETRY_MAX_SECONDS: Final = 300.0
MAX_RETRY_JITTER_SECONDS: Final = 5.0

_SAFE_DETAILS: Final[dict[SafeErrorCode, str]] = {
    SafeErrorCode.AUTHORIZATION_STALE: "Authorization changed before execution completed.",
    SafeErrorCode.ATTEMPTS_EXHAUSTED: "The job exhausted its attempts.",
    SafeErrorCode.HANDLER_FAILED: "The job handler failed safely.",
    SafeErrorCode.RETRYABLE_FAILURE: "The job will be retried.",
}


@dataclass(frozen=True, slots=True)
class LeaseToken:
    job_id: uuid.UUID
    kind: str
    attempt_token: int
    worker_id: str
    expires_at: datetime


def _role(value: object) -> WorkerRole:
    if isinstance(value, WorkerRole):
        return value
    if type(value) is str:
        try:
            return WorkerRole(value)
        except ValueError:
            pass
    raise ValueError("invalid worker role")


def _now(value: object) -> datetime:
    if not isinstance(value, datetime) or not timezone.is_aware(value):
        raise ValueError("invalid lease time")
    return value


def _positive_bounded_seconds(value: object, *, field_name: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid {field_name}")
    seconds = float(value)
    if not math.isfinite(seconds) or not 0 < seconds <= maximum:
        raise ValueError(f"invalid {field_name}")
    return seconds


def _lease_delta() -> timedelta:
    seconds = _positive_bounded_seconds(
        getattr(settings, "AEGIS_JOB_LEASE_SECONDS", DEFAULT_LEASE_SECONDS),
        field_name="lease duration",
        maximum=MAX_LEASE_SECONDS,
    )
    return timedelta(seconds=seconds)


def _retry_parameters() -> tuple[float, float]:
    base = _positive_bounded_seconds(
        getattr(settings, "AEGIS_JOB_RETRY_BASE_SECONDS", DEFAULT_RETRY_BASE_SECONDS),
        field_name="retry base",
        maximum=DEFAULT_RETRY_MAX_SECONDS,
    )
    maximum = _positive_bounded_seconds(
        getattr(settings, "AEGIS_JOB_RETRY_MAX_SECONDS", DEFAULT_RETRY_MAX_SECONDS),
        field_name="retry maximum",
        maximum=86_400,
    )
    if base > maximum:
        raise ValueError("invalid retry bounds")
    return base, maximum


def _error_code(value: object) -> SafeErrorCode:
    if isinstance(value, SafeErrorCode):
        return value
    if type(value) is str:
        try:
            return SafeErrorCode(value)
        except ValueError:
            pass
    raise ValueError("invalid safe error code")


def _validated_lease(value: object) -> LeaseToken:
    if not isinstance(value, LeaseToken):
        raise ValueError("invalid lease token")
    if not isinstance(value.job_id, uuid.UUID):
        raise ValueError("invalid lease token")
    if type(value.attempt_token) is not int or value.attempt_token <= 0:
        raise ValueError("invalid lease token")
    canonical_worker_id(value.worker_id)
    try:
        JobKind(value.kind)
    except ValueError:
        raise ValueError("invalid lease token") from None
    _now(value.expires_at)
    return value


def _cas_filter(lease: LeaseToken, *, now: datetime) -> Q:
    return Q(
        pk=lease.job_id,
        kind=lease.kind,
        attempt_token=lease.attempt_token,
        lease_owner=lease.worker_id,
        state=JobState.RUNNING,
        lease_expires_at__gt=now,
    )


def _guarded_update(query: Q, **values: object) -> int:
    with _allow_job_execution_updates():
        return Job.objects.filter(query).update(**values)


def _expire_exhausted(job: Job, *, now: datetime) -> None:
    _guarded_update(
        Q(pk=job.pk, state=job.state, attempt_token=job.attempt_token),
        state=JobState.FAILED,
        attempt_token=job.attempt_token + 1,
        lease_owner=None,
        lease_expires_at=None,
        safe_error_code=SafeErrorCode.ATTEMPTS_EXHAUSTED,
        safe_error_detail=_SAFE_DETAILS[SafeErrorCode.ATTEMPTS_EXHAUSTED],
        result=None,
        updated_at=now,
    )


def claim_next_job(role: str, worker_id: str, now: datetime) -> LeaseToken | None:
    worker_role = _role(role)
    owner = canonical_worker_id(worker_id)
    claim_time = _now(now)
    expires_at = claim_time + _lease_delta()
    eligible = (
        Q(
            state__in=(JobState.QUEUED, JobState.RETRY_WAIT),
            available_at__lte=claim_time,
        )
        | Q(state=JobState.RUNNING, lease_expires_at__lte=claim_time)
    )

    with transaction.atomic():
        while True:
            job = (
                Job.objects.select_for_update(skip_locked=True)
                .filter(target_role=worker_role)
                .filter(eligible)
                .order_by("-priority", "available_at", "id")
                .first()
            )
            if job is None:
                return None
            if job.attempts >= job.max_attempts:
                _expire_exhausted(job, now=claim_time)
                continue
            if job.state not in (JobState.QUEUED, JobState.RETRY_WAIT, JobState.RUNNING):
                raise ValueError("invalid claim state")
            next_attempt = job.attempts + 1
            next_token = job.attempt_token + 1
            updated = _guarded_update(
                Q(pk=job.pk, state=job.state, attempt_token=job.attempt_token),
                state=JobState.RUNNING,
                attempts=next_attempt,
                attempt_token=next_token,
                lease_owner=owner,
                lease_expires_at=expires_at,
                safe_error_code=None,
                safe_error_detail=None,
                result=None,
                updated_at=claim_time,
            )
            if updated != 1:
                continue
            return LeaseToken(
                job_id=job.id,
                kind=job.kind,
                attempt_token=next_token,
                worker_id=owner,
                expires_at=expires_at,
            )


def renew_lease(lease: LeaseToken, *, now: datetime) -> bool:
    token = _validated_lease(lease)
    renewal_time = _now(now)
    return (
        _guarded_update(
            _cas_filter(token, now=renewal_time),
            lease_expires_at=renewal_time + _lease_delta(),
            updated_at=renewal_time,
        )
        == 1
    )


def _fail_with_code(
    lease: LeaseToken,
    *,
    code: SafeErrorCode,
    now: datetime,
) -> bool:
    return (
        _guarded_update(
            _cas_filter(lease, now=now),
            state=JobState.FAILED,
            lease_owner=None,
            lease_expires_at=None,
            safe_error_code=code,
            safe_error_detail=_SAFE_DETAILS[code],
            result=None,
            updated_at=now,
        )
        == 1
    )


def finish_job(lease: LeaseToken, *, result: object, now: datetime) -> bool:
    token = _validated_lease(lease)
    completion_time = _now(now)
    normalized_result = validate_probe_result(result)
    with transaction.atomic():
        operation_id = (
            Job.objects.filter(_cas_filter(token, now=completion_time))
            .values_list("operation_id", flat=True)
            .first()
        )
        if operation_id is None:
            return False
        operation = Operation.objects.filter(pk=operation_id).first()
        if operation is None or not validate_authorization_snapshot(operation):
            _fail_with_code(
                token,
                code=SafeErrorCode.AUTHORIZATION_STALE,
                now=completion_time,
            )
            return False
        return (
            _guarded_update(
                _cas_filter(token, now=completion_time),
                state=JobState.SUCCEEDED,
                lease_owner=None,
                lease_expires_at=None,
                safe_error_code=None,
                safe_error_detail=None,
                result=normalized_result,
                updated_at=completion_time,
            )
            == 1
        )


def fail_job(
    lease: LeaseToken,
    *,
    error_code: str,
    now: datetime,
    detail: str | None = None,
) -> bool:
    del detail
    token = _validated_lease(lease)
    failure_time = _now(now)
    code = _error_code(error_code)
    return _fail_with_code(token, code=code, now=failure_time)


def retry_job(
    lease: LeaseToken,
    *,
    error_code: str,
    now: datetime,
    detail: str | None = None,
    jitter_seconds: float = 0,
) -> bool:
    del detail
    token = _validated_lease(lease)
    retry_time = _now(now)
    code = _error_code(error_code)
    if code is not SafeErrorCode.RETRYABLE_FAILURE:
        raise ValueError("invalid retry error code")
    if isinstance(jitter_seconds, bool) or not isinstance(jitter_seconds, (int, float)):
        raise ValueError("invalid retry jitter")
    jitter = float(jitter_seconds)
    if not math.isfinite(jitter) or not 0 <= jitter <= MAX_RETRY_JITTER_SECONDS:
        raise ValueError("invalid retry jitter")
    base, maximum = _retry_parameters()

    with transaction.atomic():
        row = (
            Job.objects.filter(_cas_filter(token, now=retry_time))
            .values("attempts", "max_attempts")
            .first()
        )
        if row is None:
            return False
        attempts = row["attempts"]
        max_attempts = row["max_attempts"]
        if attempts >= max_attempts:
            return _fail_with_code(
                token,
                code=SafeErrorCode.ATTEMPTS_EXHAUSTED,
                now=retry_time,
            )
        delay = min(maximum, base * (2 ** max(attempts - 1, 0))) + jitter
        delay = min(delay, maximum + MAX_RETRY_JITTER_SECONDS)
        return (
            _guarded_update(
                _cas_filter(token, now=retry_time),
                state=JobState.RETRY_WAIT,
                available_at=retry_time + timedelta(seconds=delay),
                lease_owner=None,
                lease_expires_at=None,
                safe_error_code=code,
                safe_error_detail=_SAFE_DETAILS[code],
                result=None,
                updated_at=retry_time,
            )
            == 1
        )
