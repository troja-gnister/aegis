from __future__ import annotations

import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier
from typing import Any

import pytest
from aegis_apps.identity.models import User
from aegis_apps.operations.models import Job, Operation
from aegis_apps.operations.services import create_operation
from aegis_apps.roots.models import Root, RootGrant
from aegis_apps.roots.permissions import Permission
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

BEFORE = [("operations", "0001_initial")]
COMPATIBILITY_PREDECESSOR = [("operations", "0004_job_claim_relinquished_error")]
NULL_ACTOR_PREDECESSOR = [("operations", "0006_job_execution_started_at")]


def _request_hash(*, actor_id: uuid.UUID, intent: dict[str, object]) -> bytes:
    canonical = json.dumps(
        intent,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(
        b"\x00".join((str(actor_id).encode(), b"foundation.probe", canonical))
    ).digest()


def _legacy_operation(
    *,
    apps: Any,
    actor_id: uuid.UUID,
    request_id: str,
    intent: dict[str, object],
    now: datetime,
) -> tuple[uuid.UUID, uuid.UUID]:
    operation_model = apps.get_model("operations", "Operation")
    job_model = apps.get_model("operations", "Job")
    operation = operation_model.objects.create(
        actor_id=actor_id,
        request_id=request_id,
        kind="foundation.probe",
        request_hash=_request_hash(actor_id=actor_id, intent=intent),
        intent=intent,
        authorization_snapshot={"userEpoch": 0, "rootEpochs": {}},
    )
    job = job_model.objects.create(
        operation_id=operation.pk,
        target_role="operations",
        kind="foundation.probe",
        payload=intent,
        priority=0,
        state="queued",
        available_at=now,
        attempts=0,
        attempt_token=0,
        max_attempts=5,
    )
    return operation.pk, job.pk


def _restore_after_failed_legacy_upgrade(
    *,
    duplicate_operation_id: uuid.UUID,
    current_leaf_nodes: list[tuple[str, str]],
) -> None:
    executor = MigrationExecutor(connection)
    old_apps = executor.loader.project_state(BEFORE).apps
    old_apps.get_model("operations", "Job").objects.filter(
        operation_id=duplicate_operation_id
    ).delete()
    old_apps.get_model("operations", "Operation").objects.filter(
        pk=duplicate_operation_id
    ).delete()
    MigrationExecutor(connection).migrate(current_leaf_nodes)


def test_upgrade_preserves_ambiguous_legacy_keys_and_maps_unambiguous_keys() -> None:
    executor = MigrationExecutor(connection)
    current_leaf_nodes = executor.loader.graph.leaf_nodes()
    operations_leaf = executor.loader.graph.leaf_nodes("operations")
    assert len(operations_leaf) == 1
    after = [operations_leaf[0]]
    upgraded = False
    duplicate_operation_id: uuid.UUID | None = None

    try:
        executor.migrate(BEFORE)
        old_apps = executor.loader.project_state(BEFORE).apps
        actor = User.objects.create_user(username="legacy-idempotency-actor")
        root = Root.objects.create(
            slot_id="legacy-idempotency-root",
            display_name="Legacy idempotency root",
            mode=Root.Mode.READ_ONLY,
            active=True,
        )
        RootGrant.objects.create(
            root=root,
            user=actor,
            permissions=int(Permission.BROWSE | Permission.PREVIEW),
        )
        now = timezone.now()
        browse_intent: dict[str, object] = {
            "roots": [{"id": str(root.pk), "permissions": int(Permission.BROWSE)}]
        }
        preview_intent: dict[str, object] = {
            "roots": [{"id": str(root.pk), "permissions": int(Permission.PREVIEW)}]
        }
        first_id, first_job_id = _legacy_operation(
            apps=old_apps,
            actor_id=actor.pk,
            request_id="legacy_duplicate_key",
            intent=browse_intent,
            now=now,
        )
        duplicate_operation_id, second_job_id = _legacy_operation(
            apps=old_apps,
            actor_id=actor.pk,
            request_id="legacy_duplicate_key",
            intent=preview_intent,
            now=now,
        )
        single_id, single_job_id = _legacy_operation(
            apps=old_apps,
            actor_id=actor.pk,
            request_id="legacy_single_key",
            intent={"roots": []},
            now=now,
        )

        MigrationExecutor(connection).migrate(after)
        upgraded = True

        assert set(Operation.objects.values_list("pk", flat=True)) == {
            first_id,
            duplicate_operation_id,
            single_id,
        }
        assert set(Job.objects.values_list("pk", flat=True)) == {
            first_job_id,
            second_job_id,
            single_job_id,
        }
        assert set(
            Operation.objects.filter(
                request_id="legacy_duplicate_key"
            ).values_list("idempotency_namespace", flat=True)
        ) == {None}
        assert Operation.objects.get(pk=single_id).idempotency_namespace == "v1"

        current_actor = User.objects.get(pk=actor.pk)
        with pytest.raises(ValueError, match="idempotency conflict"):
            create_operation(
                actor=current_actor,
                request_id="legacy_duplicate_key",
                kind="foundation.probe",
                intent=browse_intent,
            )
        mapped = create_operation(
            actor=current_actor,
            request_id="legacy_single_key",
            kind="foundation.probe",
            intent={"roots": []},
        )
        assert mapped.pk == single_id

        barrier = Barrier(2)

        def create_v1(_index: int) -> uuid.UUID:
            close_old_connections()
            try:
                thread_actor = User.objects.get(pk=actor.pk)
                barrier.wait(timeout=10)
                return create_operation(
                    actor=thread_actor,
                    request_id="fresh_v1_key",
                    kind="foundation.probe",
                    intent={"roots": []},
                ).pk
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            created_ids = list(pool.map(create_v1, range(2)))

        assert created_ids[0] == created_ids[1]
        assert Operation.objects.get(pk=created_ids[0]).idempotency_namespace == "v1"
        assert Operation.objects.count() == 4
        assert Job.objects.count() == 4
    finally:
        if not upgraded and duplicate_operation_id is not None:
            _restore_after_failed_legacy_upgrade(
                duplicate_operation_id=duplicate_operation_id,
                current_leaf_nodes=current_leaf_nodes,
            )
        else:
            MigrationExecutor(connection).migrate(current_leaf_nodes)


def test_upgrade_drops_the_already_applied_actor_request_constraint() -> None:
    executor = MigrationExecutor(connection)
    current_leaf_nodes = executor.loader.graph.leaf_nodes()
    operations_leaf = executor.loader.graph.leaf_nodes("operations")
    assert len(operations_leaf) == 1
    after = [operations_leaf[0]]

    try:
        assert after != COMPATIBILITY_PREDECESSOR
        executor.migrate(COMPATIBILITY_PREDECESSOR)
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE operations_operation "
                "DROP CONSTRAINT IF EXISTS operations_operation_actor_request_uniq"
            )
            cursor.execute(
                "ALTER TABLE operations_operation "
                "ADD CONSTRAINT operations_operation_actor_request_uniq "
                "UNIQUE (actor_id, request_id)"
            )

        MigrationExecutor(connection).migrate(after)

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM pg_constraint "
                "WHERE conrelid = 'operations_operation'::regclass "
                "AND conname = 'operations_operation_actor_request_uniq'"
            )
            assert cursor.fetchone() == (0,)
    finally:
        MigrationExecutor(connection).migrate(current_leaf_nodes)


def test_upgrade_preserves_and_demotes_ambiguous_v1_null_actor_history() -> None:
    executor = MigrationExecutor(connection)
    current_leaf_nodes = executor.loader.graph.leaf_nodes()
    operations_leaf = executor.loader.graph.leaf_nodes("operations")
    assert len(operations_leaf) == 1
    after = [operations_leaf[0]]

    try:
        assert after != NULL_ACTOR_PREDECESSOR
        executor.migrate(NULL_ACTOR_PREDECESSOR)
        old_apps = executor.loader.project_state(NULL_ACTOR_PREDECESSOR).apps
        operation_model = old_apps.get_model("operations", "Operation")
        job_model = old_apps.get_model("operations", "Job")
        now = timezone.now()
        operation_ids: set[uuid.UUID] = set()
        job_ids: set[uuid.UUID] = set()
        for index in range(2):
            intent: dict[str, object] = {
                "roots": [],
                "legacyVariant": index,
            }
            operation = operation_model.objects.create(
                actor_id=None,
                request_id="legacy_null_actor_key",
                idempotency_namespace="v1",
                kind="foundation.probe",
                request_hash=hashlib.sha256(
                    f"legacy-null-{index}".encode("ascii")
                ).digest(),
                intent=intent,
                authorization_snapshot={"userEpoch": 0, "rootEpochs": {}},
            )
            job = job_model.objects.create(
                operation_id=operation.pk,
                target_role="operations",
                kind="foundation.probe",
                payload=intent,
                priority=0,
                state="queued",
                available_at=now,
                attempts=0,
                attempt_token=0,
                max_attempts=5,
                execution_started_at=None,
            )
            operation_ids.add(operation.pk)
            job_ids.add(job.pk)
        single_operation = operation_model.objects.create(
            actor_id=None,
            request_id="legacy_single_null_actor_key",
            idempotency_namespace="v1",
            kind="foundation.probe",
            request_hash=hashlib.sha256(b"legacy-single-null").digest(),
            intent={"roots": []},
            authorization_snapshot={"userEpoch": 0, "rootEpochs": {}},
        )
        single_job = job_model.objects.create(
            operation_id=single_operation.pk,
            target_role="operations",
            kind="foundation.probe",
            payload={"roots": []},
            priority=0,
            state="queued",
            available_at=now,
            attempts=0,
            attempt_token=0,
            max_attempts=5,
            execution_started_at=None,
        )
        operation_ids.add(single_operation.pk)
        job_ids.add(single_job.pk)

        MigrationExecutor(connection).migrate(after)

        assert set(
            Operation.objects.filter(pk__in=operation_ids).values_list("pk", flat=True)
        ) == operation_ids
        assert set(
            Job.objects.filter(operation_id__in=operation_ids).values_list(
                "pk", flat=True
            )
        ) == job_ids
        assert set(
            Operation.objects.filter(request_id="legacy_null_actor_key").values_list(
                "idempotency_namespace", flat=True
            )
        ) == {None}
        assert (
            Operation.objects.get(pk=single_operation.pk).idempotency_namespace
            == "v1"
        )
        with (
            pytest.raises(DatabaseError) as caught,
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                INSERT INTO operations_operation (
                    id, actor_id, request_id, idempotency_namespace, kind,
                    request_hash, intent, authorization_snapshot, created_at
                ) VALUES (
                    %s, NULL, %s, NULL, 'foundation.probe', %s,
                    '{"roots": []}', '{"userEpoch": 0, "rootEpochs": {}}',
                    clock_timestamp()
                )
                """,
                [
                    uuid.uuid4(),
                    "post_upgrade_null_namespace",
                    hashlib.sha256(b"post-upgrade-null").digest(),
                ],
            )
        assert caught.value.__cause__ is not None
        assert getattr(caught.value.__cause__, "sqlstate", None) == "55000"
    finally:
        MigrationExecutor(connection).migrate(current_leaf_nodes)
