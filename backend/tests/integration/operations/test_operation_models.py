from __future__ import annotations

import asyncio
import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from aegis_apps.identity.models import User
from aegis_apps.operations.enums import JobState
from aegis_apps.operations.models import (
    OPERATION_IDEMPOTENCY_NAMESPACE_V1,
    Job,
    Operation,
    WorkerHeartbeat,
)
from aegis_apps.operations.services import create_operation
from django.db import IntegrityError, close_old_connections, transaction
from django.utils import timezone

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def _operation() -> Operation:
    actor = User.objects.create_user(username=f"actor-{uuid.uuid4()}")
    return create_operation(
        actor=actor,
        request_id="task10_model_request",
        kind="foundation.probe",
        intent={"roots": []},
    )


def _operation_candidate(
    *,
    request_id: str,
    actor: User | None = None,
    idempotency_namespace: str | None = OPERATION_IDEMPOTENCY_NAMESPACE_V1,
    discriminator: str = "default",
) -> Operation:
    return Operation(
        actor=actor,
        request_id=request_id,
        idempotency_namespace=idempotency_namespace,
        kind="foundation.probe",
        request_hash=hashlib.sha256(discriminator.encode("ascii")).digest(),
        intent={"roots": []},
        authorization_snapshot={"userEpoch": 0, "rootEpochs": {}},
    )


def test_operation_and_job_are_uuid_backed_and_protect_the_actor() -> None:
    operation = _operation()
    job = operation.jobs.get()

    assert operation.id.version == 4
    assert job.id.version == 4
    assert Operation._meta.get_field("actor").remote_field.on_delete.__name__ == "PROTECT"
    assert Job._meta.get_field("operation").remote_field.on_delete.__name__ == "PROTECT"


def test_operation_is_immutable_through_instance_queryset_bulk_and_delete_paths() -> None:
    operation = _operation()
    original = operation.intent

    operation.intent = {"roots": [{"id": str(uuid.uuid4()), "permissions": 1}]}
    with pytest.raises(PermissionError, match="immutable"):
        operation.save()
    with pytest.raises(PermissionError, match="immutable"):
        Operation.objects.filter(pk=operation.pk).update(intent={"roots": []})
    with pytest.raises(PermissionError, match="immutable"):
        Operation.objects.filter(pk=operation.pk).update(idempotency_namespace=None)
    with pytest.raises(PermissionError, match="immutable"):
        Operation.objects.bulk_update([operation], ["intent"])
    with pytest.raises(PermissionError, match="immutable"):
        Operation.objects.bulk_update([operation], ["idempotency_namespace"])
    with pytest.raises(PermissionError, match="deletion"):
        operation.delete()
    with pytest.raises(PermissionError, match="deletion"):
        Operation.objects.filter(pk=operation.pk).delete()

    operation.refresh_from_db()
    assert operation.intent == original


@pytest.mark.parametrize("insertion_path", ["save", "manager_create"])
def test_current_operation_insert_rejects_legacy_null_namespace(
    insertion_path: str,
) -> None:
    request_id = f"null_namespace_{insertion_path}"
    candidate = _operation_candidate(
        request_id=request_id,
        idempotency_namespace=None,
        discriminator=insertion_path,
    )

    with pytest.raises(ValueError, match="idempotency namespace"):
        if insertion_path == "save":
            candidate.save()
        else:
            Operation.objects.create(
                actor=candidate.actor,
                request_id=candidate.request_id,
                idempotency_namespace=candidate.idempotency_namespace,
                kind=candidate.kind,
                request_hash=candidate.request_hash,
                intent=candidate.intent,
                authorization_snapshot=candidate.authorization_snapshot,
            )

    assert not Operation.objects.filter(request_id=request_id).exists()


@pytest.mark.parametrize("ignore_conflicts", [False, True])
def test_operation_bulk_create_rejects_legacy_null_namespace_before_insert(
    ignore_conflicts: bool,
) -> None:
    request_id = f"null_namespace_bulk_{ignore_conflicts!s:.5}"
    candidate = _operation_candidate(
        request_id=request_id,
        idempotency_namespace=None,
        discriminator=request_id,
    )

    with pytest.raises(ValueError, match="idempotency namespace"):
        Operation.objects.bulk_create(
            [candidate],
            ignore_conflicts=ignore_conflicts,
        )

    assert not Operation.objects.filter(request_id=request_id).exists()


def test_operation_async_bulk_create_rejects_legacy_null_namespace() -> None:
    request_id = "null_namespace_async_bulk"
    candidate = _operation_candidate(
        request_id=request_id,
        idempotency_namespace=None,
        discriminator=request_id,
    )

    async def insert() -> None:
        await Operation.objects.abulk_create([candidate])

    with pytest.raises(ValueError, match="idempotency namespace"):
        asyncio.run(insert())

    assert not Operation.objects.filter(request_id=request_id).exists()


def test_operation_bulk_create_prevalidates_the_entire_mixed_batch() -> None:
    valid_request_id = "mixed_namespace_valid"
    invalid_request_id = "mixed_namespace_invalid"
    records = [
        _operation_candidate(
            request_id=valid_request_id,
            discriminator=valid_request_id,
        ),
        _operation_candidate(
            request_id=invalid_request_id,
            idempotency_namespace=None,
            discriminator=invalid_request_id,
        ),
    ]

    with pytest.raises(ValueError, match="idempotency namespace"):
        Operation.objects.bulk_create(records, batch_size=1)

    assert not Operation.objects.filter(
        request_id__in=(valid_request_id, invalid_request_id)
    ).exists()


def test_operation_bulk_create_retains_ignore_conflicts_for_valid_v1_rows() -> None:
    request_id = "valid_namespace_ignore_conflict"
    original = _operation_candidate(
        request_id=request_id,
        discriminator="original",
    )
    original.save()
    duplicate = _operation_candidate(
        request_id=request_id,
        discriminator="duplicate",
    )

    created = Operation.objects.bulk_create([duplicate], ignore_conflicts=True)

    assert created == [duplicate]
    assert Operation.objects.filter(request_id=request_id, actor=None).count() == 1
    stored = Operation.objects.get(request_id=request_id, actor=None)
    assert bytes(stored.request_hash) == bytes(original.request_hash)


def test_concurrent_v1_null_actor_request_ids_collide() -> None:
    request_id = "null_actor_collision"
    barrier = Barrier(2)

    def insert(index: int) -> uuid.UUID | None:
        close_old_connections()
        try:
            candidate = _operation_candidate(
                request_id=request_id,
                discriminator=f"null-actor-{index}",
            )
            barrier.wait(timeout=10)
            try:
                candidate.save()
            except IntegrityError:
                return None
            return candidate.pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        inserted_ids = list(executor.map(insert, range(2)))

    assert len([operation_id for operation_id in inserted_ids if operation_id]) == 1
    assert Operation.objects.filter(request_id=request_id, actor=None).count() == 1


def test_operation_bulk_create_cannot_update_an_existing_intent_on_conflict() -> None:
    operation = _operation()
    original = operation.intent
    replacement = Operation(
        id=operation.id,
        actor_id=operation.actor_id,
        request_id=operation.request_id,
        kind=operation.kind,
        request_hash=operation.request_hash,
        intent={"roots": [{"id": str(uuid.uuid4()), "permissions": 1}]},
        authorization_snapshot=operation.authorization_snapshot,
    )

    with pytest.raises(PermissionError, match="conflict"):
        Operation.objects.bulk_create(
            [replacement],
            update_conflicts=True,
            update_fields=("intent",),
            unique_fields=("pk",),
        )

    operation.refresh_from_db()
    assert operation.intent == original


def test_operation_async_bulk_create_cannot_update_an_existing_intent_on_conflict() -> None:
    operation = _operation()
    replacement = Operation(
        id=operation.id,
        actor_id=operation.actor_id,
        request_id=operation.request_id,
        kind=operation.kind,
        request_hash=operation.request_hash,
        intent={"roots": [{"id": str(uuid.uuid4()), "permissions": 1}]},
        authorization_snapshot=operation.authorization_snapshot,
    )

    async def attempt_conflict_update() -> None:
        await Operation.objects.abulk_create(
            [replacement],
            update_conflicts=True,
            update_fields=("intent",),
            unique_fields=("pk",),
        )

    with pytest.raises(PermissionError, match="conflict"):
        asyncio.run(attempt_conflict_update())


def test_job_intent_is_immutable_and_execution_fields_require_the_lease_boundary() -> None:
    operation = _operation()
    job = operation.jobs.get()

    job.priority = 7
    with pytest.raises(PermissionError, match="immutable"):
        job.save()
    with pytest.raises(PermissionError, match="lease"):
        Job.objects.filter(pk=job.pk).update(state=JobState.RUNNING)
    with pytest.raises(PermissionError, match="lease"):
        Job.objects.filter(pk=job.pk).update(execution_started_at=timezone.now())
    with pytest.raises(PermissionError, match="immutable"):
        Job.objects.filter(pk=job.pk).update(priority=7)
    with pytest.raises(PermissionError, match="immutable"):
        Job.objects.bulk_update([job], ["priority"])
    with pytest.raises(PermissionError, match="deletion"):
        job.delete()
    with pytest.raises(PermissionError, match="deletion"):
        Job.objects.filter(pk=job.pk).delete()

    job.refresh_from_db()
    assert job.priority == 0
    assert job.state == JobState.QUEUED


def test_job_bulk_create_cannot_update_intent_or_execution_fields_on_conflict() -> None:
    operation = _operation()
    job = operation.jobs.get()
    replacement = Job(
        id=job.id,
        operation=operation,
        target_role=job.target_role,
        kind=job.kind,
        payload=job.payload,
        priority=123,
        state=JobState.SUCCEEDED,
        available_at=job.available_at,
        attempts=job.attempts,
        attempt_token=7,
        max_attempts=job.max_attempts,
        result={"probe": "forged"},
    )

    with pytest.raises(PermissionError, match="conflict"):
        Job.objects.bulk_create(
            [replacement],
            update_conflicts=True,
            update_fields=("priority", "state", "result", "attempt_token"),
            unique_fields=("pk",),
        )

    job.refresh_from_db()
    assert job.priority == 0
    assert job.state == JobState.QUEUED
    assert job.result is None
    assert job.attempt_token == 0


def test_job_async_bulk_create_cannot_update_execution_fields_on_conflict() -> None:
    operation = _operation()
    job = operation.jobs.get()
    replacement = Job(
        id=job.id,
        operation=operation,
        target_role=job.target_role,
        kind=job.kind,
        payload=job.payload,
        priority=job.priority,
        state=JobState.SUCCEEDED,
        available_at=job.available_at,
        attempts=job.attempts,
        attempt_token=7,
        max_attempts=job.max_attempts,
        result={"probe": "forged"},
    )

    async def attempt_conflict_update() -> None:
        await Job.objects.abulk_create(
            [replacement],
            update_conflicts=True,
            update_fields=("state", "result", "attempt_token"),
            unique_fields=("pk",),
        )

    with pytest.raises(PermissionError, match="conflict"):
        asyncio.run(attempt_conflict_update())


@pytest.mark.parametrize(
    "updates",
    [
        {"kind": "unknown"},
        {"request_hash": b"short"},
        {"request_id": "bad"},
    ],
)
def test_database_rejects_invalid_operation_fields(updates: dict[str, object]) -> None:
    operation = _operation()
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "actor_id": operation.actor_id,
        "request_id": "task10_valid_request",
        "kind": "foundation.probe",
        "request_hash": uuid.uuid4().bytes + uuid.uuid4().bytes,
        "intent": {"roots": []},
        "authorization_snapshot": {"userEpoch": 0, "rootEpochs": {}},
        "created_at": timezone.now(),
    }
    values.update(updates)
    with pytest.raises(IntegrityError), transaction.atomic():
        Operation.objects.bulk_create([Operation(**values)])


@pytest.mark.parametrize(
    "updates",
    [
        {"target_role": "unknown"},
        {"kind": "unknown"},
        {"state": "unknown"},
        {"max_attempts": 0},
        {"attempts": -1},
        {"attempts": 2, "max_attempts": 1},
        {"attempt_token": 0, "attempts": 1},
        {
            "state": JobState.RUNNING,
            "lease_owner": None,
            "lease_expires_at": timezone.now() + timedelta(seconds=30),
        },
        {
            "state": JobState.QUEUED,
            "lease_owner": str(uuid.uuid4()),
            "lease_expires_at": timezone.now() + timedelta(seconds=30),
        },
        {
            "state": JobState.QUEUED,
            "execution_started_at": timezone.now(),
        },
    ],
)
def test_database_rejects_invalid_job_cross_field_shapes(updates: dict[str, object]) -> None:
    operation = _operation()
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "operation_id": operation.id,
        "target_role": "operations",
        "kind": "foundation.probe",
        "payload": {"roots": []},
        "priority": 0,
        "state": JobState.QUEUED,
        "available_at": timezone.now(),
        "attempts": 0,
        "attempt_token": 0,
        "max_attempts": 5,
        "lease_owner": None,
        "lease_expires_at": None,
        "safe_error_code": None,
        "safe_error_detail": None,
        "result": None,
    }
    values.update(updates)
    with pytest.raises(IntegrityError), transaction.atomic():
        Job.objects.bulk_create([Job(**values)])


def test_database_rejects_duplicate_role_enqueue_for_one_operation() -> None:
    operation = _operation()
    original = operation.jobs.get()
    with pytest.raises(IntegrityError), transaction.atomic():
        Job.objects.bulk_create(
            [
                Job(
                    operation=operation,
                    target_role=original.target_role,
                    kind=original.kind,
                    payload=original.payload,
                    priority=original.priority,
                    state=JobState.QUEUED,
                    available_at=timezone.now(),
                    max_attempts=original.max_attempts,
                )
            ]
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("role", "unknown"),
        ("status", "unknown"),
        ("worker_id", "not-a-uuid"),
    ],
)
def test_database_rejects_invalid_heartbeat_identity_fields(field: str, value: str) -> None:
    values = {
        "role": "operations",
        "worker_id": str(uuid.uuid4()),
        "release_id": "release-1",
        "schema_identity": "schema-1",
        "manifest_identity": "unconfigured:v1",
        "last_seen_at": timezone.now(),
        "status": "idle",
        "metrics": {},
    }
    values[field] = value
    with pytest.raises(IntegrityError), transaction.atomic():
        WorkerHeartbeat.objects.bulk_create([WorkerHeartbeat(**values)])
