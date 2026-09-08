from __future__ import annotations

import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any, cast
from unittest.mock import patch

import pytest
from aegis_apps.identity.models import User
from aegis_apps.operations.models import Job, Operation
from aegis_apps.operations.services import create_operation, enqueue_job
from aegis_apps.roots.models import Root, RootGrant
from aegis_apps.roots.permissions import Permission
from django.db import close_old_connections

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]
REQUEST_ID = "task10_service_request"


def _actor(*, username: str = "operation-actor", is_active: bool = True) -> User:
    return User.objects.create_user(username=username, is_active=is_active)


def _root(*, active: bool = True, epoch: int = 0) -> Root:
    return Root.objects.create(
        slot_id=f"slot-{uuid.uuid4().hex[:16]}",
        display_name="Opaque root",
        mode=Root.Mode.READ_ONLY,
        active=active,
        authorization_epoch=epoch,
    )


def test_create_operation_normalizes_hashes_snapshots_and_enqueues_atomically() -> None:
    actor = _actor()
    root_b = _root(epoch=4)
    root_a = _root(epoch=2)
    actor.authorization_epoch = 9
    actor.save(update_fields=("authorization_epoch",))
    RootGrant.objects.create(root=root_a, user=actor, permissions=int(Permission.BROWSE))
    RootGrant.objects.create(
        root=root_b,
        user=actor,
        permissions=int(Permission.BROWSE | Permission.PREVIEW),
    )
    supplied = {
        "roots": [
            {
                "permissions": int(Permission.BROWSE | Permission.PREVIEW),
                "id": str(root_b.id).upper(),
            },
            {"permissions": int(Permission.BROWSE), "id": str(root_a.id)},
        ]
    }

    operation = create_operation(
        actor=actor,
        request_id=REQUEST_ID,
        kind="foundation.probe",
        intent=supplied,
    )

    expected_intent = {
        "roots": [
            {"id": str(root_id), "permissions": permissions}
            for root_id, permissions in sorted(
                (
                    (root_a.id, int(Permission.BROWSE)),
                    (root_b.id, int(Permission.BROWSE | Permission.PREVIEW)),
                )
            )
        ]
    }
    canonical = json.dumps(
        expected_intent,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    assert operation.intent == expected_intent
    assert operation.authorization_snapshot == {
        "userEpoch": 9,
        "rootEpochs": {str(root_a.id): 2, str(root_b.id): 4},
    }
    assert bytes(operation.request_hash) == hashlib.sha256(
        b"\x00".join((str(actor.id).encode(), b"foundation.probe", canonical))
    ).digest()
    job = operation.jobs.get()
    assert job.target_role == "operations"
    assert job.kind == operation.kind
    assert job.payload == expected_intent


def test_create_operation_is_idempotent_for_the_exact_normalized_request() -> None:
    actor = _actor()
    first = create_operation(
        actor=actor,
        request_id=REQUEST_ID,
        kind="foundation.probe",
        intent={"roots": []},
    )
    repeated = create_operation(
        actor=actor,
        request_id=REQUEST_ID,
        kind="foundation.probe",
        intent={"roots": []},
    )

    assert repeated.id == first.id
    assert Operation.objects.count() == 1
    assert Job.objects.count() == 1


def test_new_request_key_for_identical_intent_creates_a_fresh_operation_and_job() -> None:
    actor = _actor()
    first = create_operation(
        actor=actor,
        request_id=REQUEST_ID,
        kind="foundation.probe",
        intent={"roots": []},
    )
    User.objects.filter(pk=actor.pk).update(authorization_epoch=7)

    second = create_operation(
        actor=actor,
        request_id="task10_fresh_request",
        kind="foundation.probe",
        intent={"roots": []},
    )

    assert second.id != first.id
    assert second.request_hash == first.request_hash
    assert second.authorization_snapshot == {"userEpoch": 7, "rootEpochs": {}}
    assert Operation.objects.count() == 2
    assert Job.objects.count() == 2


def test_reusing_one_actor_request_key_for_a_different_request_is_a_conflict() -> None:
    actor = _actor()
    root = _root()
    RootGrant.objects.create(root=root, user=actor, permissions=int(Permission.BROWSE))
    original = create_operation(
        actor=actor,
        request_id=REQUEST_ID,
        kind="foundation.probe",
        intent={"roots": []},
    )

    with pytest.raises(ValueError, match="idempotency conflict"):
        create_operation(
            actor=actor,
            request_id=REQUEST_ID,
            kind="foundation.probe",
            intent={
                "roots": [
                    {"id": str(root.id), "permissions": int(Permission.BROWSE)}
                ]
            },
        )

    assert list(Operation.objects.values_list("id", flat=True)) == [original.id]
    assert Job.objects.count() == 1


def test_new_key_after_permission_restoration_recaptures_current_epochs() -> None:
    actor = _actor()
    root = _root(epoch=3)
    grant = RootGrant.objects.create(
        root=root,
        user=actor,
        permissions=int(Permission.BROWSE),
    )
    intent = {
        "roots": [{"id": str(root.id), "permissions": int(Permission.BROWSE)}]
    }
    first = create_operation(
        actor=actor,
        request_id=REQUEST_ID,
        kind="foundation.probe",
        intent=intent,
    )

    RootGrant.objects.filter(pk=grant.pk).update(permissions=int(Permission.PREVIEW))
    RootGrant.objects.filter(pk=grant.pk).update(permissions=int(Permission.BROWSE))
    Root.objects.filter(pk=root.pk).update(authorization_epoch=8)
    User.objects.filter(pk=actor.pk).update(authorization_epoch=11)
    second = create_operation(
        actor=actor,
        request_id="task10_restored_request",
        kind="foundation.probe",
        intent=intent,
    )

    assert second.id != first.id
    assert second.authorization_snapshot == {
        "userEpoch": 11,
        "rootEpochs": {str(root.id): 8},
    }
    assert second.jobs.count() == 1


def test_concurrent_different_keys_for_identical_intent_create_fresh_operations() -> None:
    actor = _actor()
    barrier = Barrier(2)

    def create(index: int) -> uuid.UUID:
        close_old_connections()
        try:
            thread_actor = User.objects.get(pk=actor.pk)
            barrier.wait(timeout=10)
            return create_operation(
                actor=thread_actor,
                request_id=f"task10_parallel_{index}",
                kind="foundation.probe",
                intent={"roots": []},
            ).id
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        operation_ids = list(executor.map(create, range(2)))

    assert operation_ids[0] != operation_ids[1]
    assert Operation.objects.count() == 2
    assert Job.objects.count() == 2


def test_concurrent_operation_creation_and_enqueue_are_idempotent() -> None:
    actor = _actor()
    barrier = Barrier(2)

    def create(_index: int) -> uuid.UUID:
        close_old_connections()
        try:
            thread_actor = User.objects.get(pk=actor.pk)
            barrier.wait(timeout=10)
            return create_operation(
                actor=thread_actor,
                request_id=REQUEST_ID,
                kind="foundation.probe",
                intent={"roots": []},
            ).id
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        operation_ids = list(executor.map(create, range(2)))

    assert operation_ids[0] == operation_ids[1]
    operation = Operation.objects.get()
    assert operation.jobs.count() == 1


def test_create_operation_rolls_back_when_initial_enqueue_fails() -> None:
    actor = _actor()
    with (
        patch(
            "aegis_apps.operations.services.enqueue_job",
            side_effect=RuntimeError("synthetic enqueue failure"),
        ),
        pytest.raises(RuntimeError, match="synthetic"),
    ):
        create_operation(
            actor=actor,
            request_id=REQUEST_ID,
            kind="foundation.probe",
            intent={"roots": []},
        )

    assert Operation.objects.count() == 0
    assert Job.objects.count() == 0


@pytest.mark.parametrize(
    "intent",
    [
        {},
        {"roots": [], "other": 1},
        {"roots": "not-a-list"},
        {"roots": [{}]},
        {"roots": [{"id": str(uuid.uuid4()), "permissions": True}]},
        {"roots": [{"id": str(uuid.uuid4()), "permissions": 0}]},
        {"roots": [{"id": str(uuid.uuid4()), "permissions": 256}]},
        {"roots": [{"id": "not-a-uuid", "permissions": 1}]},
        {
            "roots": [
                {"id": "00000000-0000-0000-0000-000000000001", "permissions": 1},
                {"id": "00000000-0000-0000-0000-000000000001", "permissions": 1},
            ]
        },
        {
            "roots": [
                {"id": str(uuid.UUID(int=index + 1)), "permissions": 1}
                for index in range(129)
            ]
        },
    ],
)
def test_create_operation_rejects_non_exact_probe_intents(intent: object) -> None:
    actor = _actor(username=f"invalid-{uuid.uuid4()}")
    with pytest.raises(ValueError, match="intent"):
        create_operation(
            actor=actor,
            request_id=REQUEST_ID,
            kind="foundation.probe",
            intent=intent,
        )
    assert Operation.objects.count() == 0


def test_create_operation_rejects_inactive_missing_or_unauthorized_principals() -> None:
    inactive = _actor(username="inactive-actor", is_active=False)
    with pytest.raises(ValueError, match="actor"):
        create_operation(
            actor=inactive,
            request_id=REQUEST_ID,
            kind="foundation.probe",
            intent={"roots": []},
        )

    actor = _actor(username="ordinary-actor")
    actor.is_staff = True
    actor.is_superuser = True
    actor.save(update_fields=("is_staff", "is_superuser"))
    root = _root()
    with pytest.raises(ValueError, match="authorization"):
        create_operation(
            actor=actor,
            request_id=REQUEST_ID,
            kind="foundation.probe",
            intent={"roots": [{"id": str(root.id), "permissions": 1}]},
        )

    root.active = False
    root.save(update_fields=("active",))
    RootGrant.objects.create(root=root, user=actor, permissions=1)
    with pytest.raises(ValueError, match="authorization"):
        create_operation(
            actor=actor,
            request_id=REQUEST_ID,
            kind="foundation.probe",
            intent={"roots": [{"id": str(root.id), "permissions": 1}]},
        )

    assert Operation.objects.count() == 0


def test_enqueue_job_supports_each_role_but_never_substitutes_kind_or_payload() -> None:
    actor = _actor()
    operation = create_operation(
        actor=actor,
        request_id=REQUEST_ID,
        kind="foundation.probe",
        intent={"roots": []},
    )

    indexer = enqueue_job(operation=operation, target_role="indexer")
    media = enqueue_job(operation=operation, target_role="media")
    assert {job.target_role for job in operation.jobs.all()} == {
        "operations",
        "indexer",
        "media",
    }
    assert enqueue_job(operation=operation, target_role="indexer").id == indexer.id
    assert enqueue_job(operation=operation, target_role="media").id == media.id

    with pytest.raises(ValueError, match="kind"):
        enqueue_job(operation=operation, target_role="indexer", kind="other")
    with pytest.raises(ValueError, match="payload"):
        enqueue_job(operation=operation, target_role="indexer", payload={"roots": [1]})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"target_role": "unknown"},
        {"target_role": "media", "priority": True},
        {"target_role": "media", "priority": 32768},
        {"target_role": "media", "max_attempts": 0},
        {"target_role": "media", "max_attempts": 101},
    ],
)
def test_enqueue_job_rejects_unbounded_values(kwargs: dict[str, object]) -> None:
    actor = _actor(username=f"enqueue-{uuid.uuid4()}")
    operation = create_operation(
        actor=actor,
        request_id=REQUEST_ID,
        kind="foundation.probe",
        intent={"roots": []},
    )
    with pytest.raises(ValueError):
        enqueue_job(operation=operation, **cast(dict[str, Any], kwargs))
