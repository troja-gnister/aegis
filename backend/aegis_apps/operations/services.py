from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Final

from django.contrib.postgres.aggregates import BitOr
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from aegis_apps.common.middleware import REQUEST_ID
from aegis_apps.identity.models import User
from aegis_apps.roots.models import Root, RootGrant

from .enums import JobKind, JobState, WorkerRole
from .models import MAX_JOB_ATTEMPTS, MAX_JOB_PRIORITY, MIN_JOB_PRIORITY, Job, Operation
from .serializers import canonical_json_bytes, normalize_probe_intent

DEFAULT_MAX_ATTEMPTS: Final = 5
MAX_JOB_SCHEDULE_DELTA: Final = timedelta(days=366)


class _Unset:
    pass


_UNSET = _Unset()

_ALLOWED_TRANSITIONS = frozenset(
    {
        (JobState.QUEUED, JobState.RUNNING),
        (JobState.RUNNING, JobState.SUCCEEDED),
        (JobState.RUNNING, JobState.RETRY_WAIT),
        (JobState.RETRY_WAIT, JobState.RUNNING),
        (JobState.RUNNING, JobState.FAILED),
    }
)


def _job_state(value: object) -> JobState:
    if isinstance(value, JobState):
        return value
    if type(value) is str:
        try:
            return JobState(value)
        except ValueError:
            pass
    raise ValueError("invalid job state")


def assert_transition(source: object, target: object) -> None:
    transition = (_job_state(source), _job_state(target))
    if transition not in _ALLOWED_TRANSITIONS:
        raise ValueError("invalid job state transition")


def _validate_request_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 8 <= len(value) <= 64
        or REQUEST_ID.fullmatch(value) is None
    ):
        raise ValueError("invalid operation request ID")
    return value


def _validate_kind(value: object) -> JobKind:
    if isinstance(value, JobKind):
        return value
    if type(value) is not str:
        raise ValueError("invalid operation kind")
    try:
        return JobKind(value)
    except ValueError:
        raise ValueError("invalid operation kind") from None


def _validate_role(value: object) -> WorkerRole:
    if isinstance(value, WorkerRole):
        return value
    if type(value) is not str:
        raise ValueError("invalid worker role")
    try:
        return WorkerRole(value)
    except ValueError:
        raise ValueError("invalid worker role") from None


def _validate_priority(value: object) -> int:
    if type(value) is not int or not MIN_JOB_PRIORITY <= value <= MAX_JOB_PRIORITY:
        raise ValueError("invalid job priority")
    return value


def _validate_max_attempts(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_JOB_ATTEMPTS:
        raise ValueError("invalid maximum attempts")
    return value


def _validate_available_at(value: object, *, reference: datetime) -> datetime:
    if not isinstance(value, datetime) or not timezone.is_aware(value):
        raise ValueError("invalid job availability")
    if not reference - MAX_JOB_SCHEDULE_DELTA <= value <= reference + MAX_JOB_SCHEDULE_DELTA:
        raise ValueError("invalid job availability")
    return value


def _normalized_roots(intent: Mapping[str, object]) -> list[dict[str, object]]:
    roots = intent.get("roots")
    if not isinstance(roots, list):
        raise ValueError("invalid operation intent roots")
    result: list[dict[str, object]] = []
    for item in roots:
        if not isinstance(item, dict):
            raise ValueError("invalid operation intent root schema")
        result.append(item)
    return result


def _locked_active_actor(actor: User) -> User:
    if not isinstance(actor, User) or not isinstance(actor.pk, uuid.UUID):
        raise ValueError("invalid operation actor")
    locked = User.objects.select_for_update().filter(pk=actor.pk, is_active=True).first()
    if locked is None:
        raise ValueError("invalid or inactive operation actor")
    return locked


def _capture_authorization_snapshot(
    *,
    actor: User,
    intent: Mapping[str, object],
) -> dict[str, object]:
    requirements: dict[uuid.UUID, int] = {}
    for item in _normalized_roots(intent):
        root_id = uuid.UUID(str(item["id"]))
        permissions = item["permissions"]
        if type(permissions) is not int:
            raise ValueError("invalid operation intent permission mask")
        requirements[root_id] = permissions

    root_rows = list(
        Root.objects.select_for_update()
        .filter(pk__in=requirements, active=True)
        .order_by("id")
        .values_list("id", "authorization_epoch")
    )
    if len(root_rows) != len(requirements):
        raise ValueError("operation authorization failed")

    effective_masks = {
        root_id: mask
        for root_id, mask in (
            RootGrant.objects.filter(root_id__in=requirements, root__active=True)
            .filter(Q(user_id=actor.id) | Q(group__user=actor.id))
            .values_list("root_id")
            .annotate(mask=BitOr("permissions"))
        )
    }
    for root_id, required_mask in requirements.items():
        effective = effective_masks.get(root_id, 0)
        if effective & required_mask != required_mask:
            raise ValueError("operation authorization failed")

    return {
        "userEpoch": actor.authorization_epoch,
        "rootEpochs": {str(root_id): epoch for root_id, epoch in root_rows},
    }


def _verify_operation_identity(
    operation: Operation,
    *,
    actor_id: uuid.UUID,
    kind: JobKind,
    intent: Mapping[str, object],
) -> None:
    if (
        operation.actor_id != actor_id
        or operation.kind != kind
        or operation.intent != intent
    ):
        raise ValueError("operation idempotency conflict")


def _verify_existing_job(
    job: Job,
    *,
    role: WorkerRole,
    kind: JobKind,
    payload: Mapping[str, object],
    priority: int,
    max_attempts: int,
    explicit_available_at: datetime | None,
) -> None:
    if job.target_role != role:
        raise ValueError("job enqueue conflict")
    if job.kind != kind:
        raise ValueError("job kind conflict")
    if job.payload != payload:
        raise ValueError("job payload conflict")
    if job.priority != priority or job.max_attempts != max_attempts:
        raise ValueError("job enqueue conflict")
    if explicit_available_at is not None and job.available_at != explicit_available_at:
        raise ValueError("job availability conflict")


def enqueue_job(
    *,
    operation: Operation,
    target_role: str,
    kind: str | None = None,
    payload: Mapping[str, object] | None = None,
    priority: int = 0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    available_at: datetime | _Unset = _UNSET,
) -> Job:
    role = _validate_role(target_role)
    bounded_priority = _validate_priority(priority)
    bounded_attempts = _validate_max_attempts(max_attempts)
    if not isinstance(operation, Operation) or not isinstance(operation.pk, uuid.UUID):
        raise ValueError("invalid operation")
    supplied_kind = _validate_kind(kind) if kind is not None else None
    try:
        supplied_payload = normalize_probe_intent(payload) if payload is not None else None
    except ValueError as exc:
        raise ValueError("invalid job payload") from exc
    reference = timezone.now()
    explicit_available = (
        None
        if isinstance(available_at, _Unset)
        else _validate_available_at(available_at, reference=reference)
    )

    with transaction.atomic():
        stored = Operation.objects.select_for_update().filter(pk=operation.pk).first()
        if stored is None:
            raise ValueError("invalid operation")
        stored_kind = _validate_kind(stored.kind)
        stored_payload = normalize_probe_intent(stored.intent)
        if supplied_kind is not None and supplied_kind != stored_kind:
            raise ValueError("job kind does not match operation")
        if supplied_payload is not None and supplied_payload != stored_payload:
            raise ValueError("job payload does not match operation")
        existing = Job.objects.filter(operation=stored, target_role=role).first()
        if existing is not None:
            _verify_existing_job(
                existing,
                role=role,
                kind=stored_kind,
                payload=stored_payload,
                priority=bounded_priority,
                max_attempts=bounded_attempts,
                explicit_available_at=explicit_available,
            )
            return existing
        scheduled = reference if explicit_available is None else explicit_available
        return Job.objects.create(
            operation=stored,
            target_role=role,
            kind=stored_kind,
            payload=stored_payload,
            priority=bounded_priority,
            state=JobState.QUEUED,
            available_at=scheduled,
            max_attempts=bounded_attempts,
        )


def create_operation(
    *,
    actor: User,
    request_id: str,
    kind: str,
    intent: object,
) -> Operation:
    bounded_request_id = _validate_request_id(request_id)
    operation_kind = _validate_kind(kind)
    normalized_intent = normalize_probe_intent(intent)
    canonical = canonical_json_bytes(normalized_intent, maximum_bytes=16_384)
    if not isinstance(actor, User) or not isinstance(actor.pk, uuid.UUID):
        raise ValueError("invalid operation actor")
    request_hash = hashlib.sha256(
        b"\x00".join((str(actor.pk).encode(), operation_kind.encode(), canonical))
    ).digest()

    with transaction.atomic():
        locked_actor = _locked_active_actor(actor)
        existing = (
            Operation.objects.select_for_update().filter(request_hash=request_hash).first()
        )
        if existing is not None:
            _verify_operation_identity(
                existing,
                actor_id=locked_actor.id,
                kind=operation_kind,
                intent=normalized_intent,
            )
            initial = Job.objects.filter(
                operation=existing,
                target_role=WorkerRole.OPERATIONS,
            ).first()
            if initial is None:
                raise ValueError("operation initial job is missing")
            _verify_existing_job(
                initial,
                role=WorkerRole.OPERATIONS,
                kind=operation_kind,
                payload=normalized_intent,
                priority=0,
                max_attempts=DEFAULT_MAX_ATTEMPTS,
                explicit_available_at=None,
            )
            return existing

        snapshot = _capture_authorization_snapshot(
            actor=locked_actor,
            intent=normalized_intent,
        )
        try:
            with transaction.atomic():
                operation = Operation.objects.create(
                    actor=locked_actor,
                    request_id=bounded_request_id,
                    kind=operation_kind,
                    request_hash=request_hash,
                    intent=normalized_intent,
                    authorization_snapshot=snapshot,
                )
                enqueue_job(
                    operation=operation,
                    target_role=WorkerRole.OPERATIONS,
                )
                return operation
        except IntegrityError:
            conflicting = (
                Operation.objects.select_for_update().filter(request_hash=request_hash).first()
            )
            if conflicting is None:
                raise
            _verify_operation_identity(
                conflicting,
                actor_id=locked_actor.id,
                kind=operation_kind,
                intent=normalized_intent,
            )
            initial = Job.objects.filter(
                operation=conflicting,
                target_role=WorkerRole.OPERATIONS,
            ).first()
            if initial is None:
                raise ValueError("operation initial job is missing") from None
            _verify_existing_job(
                initial,
                role=WorkerRole.OPERATIONS,
                kind=operation_kind,
                payload=normalized_intent,
                priority=0,
                max_attempts=DEFAULT_MAX_ATTEMPTS,
                explicit_available_at=None,
            )
            return conflicting
