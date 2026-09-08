from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from aegis_apps.identity.models import User
from aegis_apps.operations.enums import JobState
from aegis_apps.operations.models import Job, Operation, WorkerHeartbeat
from aegis_apps.operations.services import create_operation
from django.db import IntegrityError, transaction
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
        Operation.objects.bulk_update([operation], ["intent"])
    with pytest.raises(PermissionError, match="deletion"):
        operation.delete()
    with pytest.raises(PermissionError, match="deletion"):
        Operation.objects.filter(pk=operation.pk).delete()

    operation.refresh_from_db()
    assert operation.intent == original


def test_job_intent_is_immutable_and_execution_fields_require_the_lease_boundary() -> None:
    operation = _operation()
    job = operation.jobs.get()

    job.priority = 7
    with pytest.raises(PermissionError, match="immutable"):
        job.save()
    with pytest.raises(PermissionError, match="lease"):
        Job.objects.filter(pk=job.pk).update(state=JobState.RUNNING)
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
