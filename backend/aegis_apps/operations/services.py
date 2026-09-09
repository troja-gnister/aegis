from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Final

from django.conf import settings
from django.contrib.postgres.aggregates import BitOr
from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.utils import timezone

from aegis_apps.common.database_privileges import (
    WORKER_DATABASE_ROLE_MAP,
    current_database_login,
)
from aegis_apps.common.middleware import REQUEST_ID
from aegis_apps.identity.models import User
from aegis_apps.roots.locking import AuthorizationLocks
from aegis_apps.roots.models import Root, RootGrant

from .enums import JobKind, JobState, WorkerRole
from .models import (
    MAX_JOB_ATTEMPTS,
    MAX_JOB_PRIORITY,
    MIN_JOB_PRIORITY,
    OPERATION_IDEMPOTENCY_NAMESPACE_V1,
    Job,
    Operation,
)
from .serializers import (
    canonical_json_bytes,
    normalize_probe_intent,
)
from .serializers import validate_authorization_snapshot as validate_snapshot_schema

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


def _validate_actor(actor: User) -> uuid.UUID:
    if not isinstance(actor, User) or not isinstance(actor.pk, uuid.UUID):
        raise ValueError("invalid operation actor")
    return actor.pk


def _active_actor(
    actor_id: uuid.UUID, *, locked_users: Mapping[uuid.UUID, User]
) -> User:
    locked = locked_users.get(actor_id)
    if locked is None or not locked.is_active:
        raise ValueError("invalid or inactive operation actor")
    return locked


def _authorization_requirements(intent: Mapping[str, object]) -> dict[uuid.UUID, int]:
    requirements: dict[uuid.UUID, int] = {}
    for item in _normalized_roots(intent):
        root_id = uuid.UUID(str(item["id"]))
        permissions = item["permissions"]
        if type(permissions) is not int:
            raise ValueError("invalid operation intent permission mask")
        requirements[root_id] = permissions
    return requirements


def _capture_authorization_snapshot(
    *,
    actor: User,
    requirements: Mapping[uuid.UUID, int],
    locked_roots: Mapping[uuid.UUID, Root],
) -> dict[str, object]:
    if len(locked_roots) != len(requirements) or any(
        not root.active for root in locked_roots.values()
    ):
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
        "rootEpochs": {
            str(root_id): locked_roots[root_id].authorization_epoch
            for root_id in sorted(requirements)
        },
    }


def _verify_operation_identity(
    operation: Operation,
    *,
    actor_id: uuid.UUID,
    request_id: str,
    kind: JobKind,
    request_hash: bytes,
    intent: Mapping[str, object],
) -> None:
    if (
        operation.actor_id != actor_id
        or operation.request_id != request_id
        or operation.kind != kind
        or bytes(operation.request_hash) != request_hash
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


def _existing_idempotent_operation(
    *,
    actor_id: uuid.UUID,
    request_id: str,
    kind: JobKind,
    request_hash: bytes,
    intent: Mapping[str, object],
) -> Operation | None:
    matching = Operation.objects.select_for_update().filter(
        actor_id=actor_id,
        request_id=request_id,
    )
    versioned = list(
        matching.filter(
            idempotency_namespace=OPERATION_IDEMPOTENCY_NAMESPACE_V1
        ).order_by("id")[:2]
    )
    legacy = list(
        matching.filter(idempotency_namespace__isnull=True).order_by("id")[:2]
    )
    if len(versioned) > 1 or len(legacy) > 1 or (versioned and legacy):
        raise ValueError("operation idempotency conflict")
    operation = versioned[0] if versioned else legacy[0] if legacy else None
    if operation is None:
        return None
    _verify_operation_identity(
        operation,
        actor_id=actor_id,
        request_id=request_id,
        kind=kind,
        request_hash=request_hash,
        intent=intent,
    )
    initial = Job.objects.filter(
        operation=operation,
        target_role=WorkerRole.OPERATIONS,
    ).first()
    if initial is None:
        raise ValueError("operation initial job is missing")
    _verify_existing_job(
        initial,
        role=WorkerRole.OPERATIONS,
        kind=kind,
        payload=intent,
        priority=0,
        max_attempts=DEFAULT_MAX_ATTEMPTS,
        explicit_available_at=None,
    )
    return operation


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
    actor_id = _validate_actor(actor)
    requirements = _authorization_requirements(normalized_intent)
    request_hash = hashlib.sha256(
        b"\x00".join((str(actor_id).encode(), operation_kind.encode(), canonical))
    ).digest()

    with transaction.atomic():
        authorization_locks = AuthorizationLocks()
        locked_roots = authorization_locks.roots(requirements)
        locked_actor = _active_actor(
            actor_id,
            locked_users=authorization_locks.users((actor_id,)),
        )
        existing = _existing_idempotent_operation(
            actor_id=locked_actor.id,
            request_id=bounded_request_id,
            kind=operation_kind,
            request_hash=request_hash,
            intent=normalized_intent,
        )
        if existing is not None:
            return existing

        snapshot = _capture_authorization_snapshot(
            actor=locked_actor,
            requirements=requirements,
            locked_roots=locked_roots,
        )
        try:
            with transaction.atomic():
                operation = Operation.objects.create(
                    actor=locked_actor,
                    request_id=bounded_request_id,
                    idempotency_namespace=OPERATION_IDEMPOTENCY_NAMESPACE_V1,
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
            conflicting = _existing_idempotent_operation(
                actor_id=locked_actor.id,
                request_id=bounded_request_id,
                kind=operation_kind,
                request_hash=request_hash,
                intent=normalized_intent,
            )
            if conflicting is None:
                raise
            return conflicting


def _validate_authorization_snapshot_via_database(operation_id: uuid.UUID) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT public.aegis_validate_operation_authorization(%s)",
            [operation_id],
        )
        row = cursor.fetchone()
    if row is None:
        return False
    return row[0] is True


def validate_authorization_snapshot(operation: Operation) -> bool:
    if not isinstance(operation, Operation) or not isinstance(operation.pk, uuid.UUID):
        return False
    configured_role = getattr(settings, "AEGIS_PROCESS_ROLE", None)
    if configured_role in WorkerRole.values:
        database_role = WORKER_DATABASE_ROLE_MAP.get(current_database_login())
        if database_role is not None:
            if database_role != configured_role:
                return False
            return _validate_authorization_snapshot_via_database(operation.pk)
    with transaction.atomic():
        stored = (
            Operation.objects.filter(pk=operation.pk)
            .values("actor_id", "intent", "authorization_snapshot")
            .first()
        )
        if stored is None or not isinstance(stored["actor_id"], uuid.UUID):
            return False
        try:
            normalized_intent = normalize_probe_intent(stored["intent"])
            if normalized_intent != stored["intent"]:
                return False
            snapshot = validate_snapshot_schema(
                stored["authorization_snapshot"],
                intent=normalized_intent,
            )
        except ValueError:
            return False

        requirements = _authorization_requirements(normalized_intent)
        authorization_locks = AuthorizationLocks()
        locked_roots = authorization_locks.roots(requirements)
        if len(locked_roots) != len(requirements) or any(
            not root.active for root in locked_roots.values()
        ):
            return False
        locked_users = authorization_locks.users((stored["actor_id"],))
        actor = locked_users.get(stored["actor_id"])
        if (
            actor is None
            or not actor.is_active
            or actor.authorization_epoch != snapshot["userEpoch"]
        ):
            return False

        root_epochs = snapshot["rootEpochs"]
        if not isinstance(root_epochs, Mapping):
            return False
        if any(
            root_epochs.get(str(root_id)) != root.authorization_epoch
            for root_id, root in locked_roots.items()
        ):
            return False

        effective_masks = {
            root_id: mask
            for root_id, mask in (
                RootGrant.objects.filter(root_id__in=requirements, root__active=True)
                .filter(Q(user_id=actor.id) | Q(group__user=actor.id))
                .values_list("root_id")
                .annotate(mask=BitOr("permissions"))
            )
        }
        return all(
            effective_masks.get(root_id, 0) & required_mask == required_mask
            for root_id, required_mask in requirements.items()
        )
