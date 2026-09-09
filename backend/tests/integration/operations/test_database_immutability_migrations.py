from __future__ import annotations

import importlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pytest
from aegis_apps.audit.models import AuditEvent
from aegis_apps.audit.services import record_event
from aegis_apps.identity.models import User
from aegis_apps.operations.models import Job, Operation
from aegis_apps.operations.services import create_operation, enqueue_job
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

WORKER_DATABASE_ROLES = (
    "aegis_operations",
    "aegis_indexer",
    "aegis_media",
    "aegis_web",
)


@pytest.fixture(scope="module", autouse=True)
def _runtime_roles(
    django_db_setup: object,
    django_db_blocker: Any,
) -> Iterator[None]:
    del django_db_setup
    created: list[str] = []
    with django_db_blocker.unblock(), connection.cursor() as cursor:
        for role in WORKER_DATABASE_ROLES:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s)",
                [role],
            )
            if cursor.fetchone() != (False,):
                pytest.fail(
                    "database immutability tests require a disposable cluster "
                    "without pre-existing Aegis runtime roles"
                )
            cursor.execute(f'CREATE ROLE "{role}" NOLOGIN')
            created.append(role)
        cursor.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE "
            "ON public.audit_auditevent, public.operations_operation, "
            "public.operations_job TO aegis_operations"
        )
        cursor.execute(
            "GRANT SELECT, UPDATE ON public.operations_job "
            "TO aegis_indexer, aegis_media, aegis_web"
        )
    try:
        yield
    finally:
        with django_db_blocker.unblock(), connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
            for role in created:
                cursor.execute(f'DROP OWNED BY "{role}"')
                cursor.execute(f'DROP ROLE "{role}"')


@contextmanager
def _as_runtime(role: str) -> Iterator[None]:
    if role not in WORKER_DATABASE_ROLES:
        raise ValueError("invalid test role")
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(f'SET LOCAL ROLE "{role}"')
        try:
            yield
        finally:
            cursor.execute("RESET ROLE")


def _execute_as_runtime(
    role: str,
    sql: str,
    params: Any = None,
) -> None:
    with _as_runtime(role), connection.cursor() as cursor:
        cursor.execute(sql, params)


def _assert_runtime_rejected(
    role: str,
    sql: str,
    params: Any = None,
    *,
    sqlstate: str = "55000",
) -> None:
    with (
        _as_runtime(role),
        pytest.raises(DatabaseError) as caught,
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(sql, params)
    assert caught.value.__cause__ is not None
    assert getattr(caught.value.__cause__, "sqlstate", None) == sqlstate


def _records() -> tuple[AuditEvent, Operation, Job]:
    actor = User.objects.create_user(username=f"immutability-{uuid.uuid4()}")
    operation = create_operation(
        actor=actor,
        request_id=f"immutable_{uuid.uuid4().hex}",
        kind="foundation.probe",
        intent={"roots": []},
    )
    job = operation.jobs.get()
    audit = record_event(
        event_type="operations.test",
        outcome="success",
        actor=actor,
        request_id=f"audit_{uuid.uuid4().hex}",
        metadata={},
    )
    return audit, operation, job


def _job_for_role(role: str) -> Job:
    _audit, operation, operations_job = _records()
    if role == "operations":
        return operations_job
    return enqueue_job(operation=operation, target_role=role)


def _claim_as_role(*, role: str, job: Job, worker_id: str) -> None:
    _execute_as_runtime(
        f"aegis_{role}",
        """
        UPDATE public.operations_job
           SET state = 'running',
               attempts = attempts + 1,
               attempt_token = attempt_token + 1,
               lease_owner = %s,
               lease_expires_at = pg_catalog.clock_timestamp() + interval '30 seconds',
               safe_error_code = NULL,
               safe_error_detail = NULL,
               result = NULL,
               updated_at = pg_catalog.clock_timestamp()
         WHERE id = %s
        """,
        [worker_id, job.pk],
    )


def test_database_immutability_migration_leaves_and_dependencies_are_explicit() -> None:
    audit = importlib.import_module(
        "aegis_apps.audit.migrations.0003_database_append_only"
    )
    operations = importlib.import_module(
        "aegis_apps.operations.migrations.0008_database_immutability"
    )

    assert audit.Migration.dependencies == [
        ("audit", "0002_auditevent_manager_names")
    ]
    assert set(operations.Migration.dependencies) == {
        ("operations", "0007_operation_idempotency_namespace_boundary"),
        ("roots", "0001_initial"),
        ("identity", "0004_login_throttle"),
    }


def test_runtime_role_cannot_rewrite_or_remove_append_only_records() -> None:
    audit, operation, job = _records()

    _assert_runtime_rejected(
        "aegis_operations",
        "UPDATE public.audit_auditevent SET outcome = 'failure' WHERE id = %s",
        [audit.pk],
    )
    _assert_runtime_rejected(
        "aegis_operations",
        "DELETE FROM public.audit_auditevent WHERE id = %s",
        [audit.pk],
    )
    _assert_runtime_rejected(
        "aegis_operations",
        "TRUNCATE TABLE public.audit_auditevent CASCADE",
    )
    _assert_runtime_rejected(
        "aegis_operations",
        "UPDATE public.operations_operation SET intent = '{}' WHERE id = %s",
        [operation.pk],
    )
    _assert_runtime_rejected(
        "aegis_operations",
        "DELETE FROM public.operations_operation WHERE id = %s",
        [operation.pk],
    )
    _assert_runtime_rejected(
        "aegis_operations",
        "TRUNCATE TABLE public.operations_operation CASCADE",
    )
    _assert_runtime_rejected(
        "aegis_operations",
        "UPDATE public.operations_job SET payload = '{}' WHERE id = %s",
        [job.pk],
    )
    _assert_runtime_rejected(
        "aegis_operations",
        "DELETE FROM public.operations_job WHERE id = %s",
        [job.pk],
    )
    _assert_runtime_rejected(
        "aegis_operations",
        "TRUNCATE TABLE public.operations_job",
    )


def test_new_null_namespace_insert_is_rejected_even_for_schema_owner() -> None:
    _audit, operation, _job = _records()

    with (
        pytest.raises(DatabaseError) as caught,
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            """
            INSERT INTO public.operations_operation (
                id, actor_id, request_id, idempotency_namespace, kind,
                request_hash, intent, authorization_snapshot, created_at
            )
            SELECT
                %s, actor_id, %s, NULL, kind, request_hash, intent,
                authorization_snapshot, clock_timestamp()
            FROM public.operations_operation
            WHERE id = %s
            """,
            [uuid.uuid4(), f"legacy_{uuid.uuid4().hex}", operation.pk],
        )
    assert caught.value.__cause__ is not None
    assert getattr(caught.value.__cause__, "sqlstate", None) == "55000"


def test_role_fenced_start_uses_database_time_and_preserves_terminal_marker() -> None:
    _audit, _operation, job = _records()
    worker_id = str(uuid.uuid4())
    _claim_as_role(role="operations", job=job, worker_id=worker_id)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_catalog.clock_timestamp()")
        lower = cursor.fetchone()[0]
    supplied = timezone.now() + timedelta(days=30)
    _execute_as_runtime(
        "aegis_operations",
        """
        UPDATE public.operations_job
           SET execution_started_at = %s,
               updated_at = pg_catalog.clock_timestamp()
         WHERE id = %s
        """,
        [supplied, job.pk],
    )
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_catalog.clock_timestamp()")
        upper = cursor.fetchone()[0]
        cursor.execute(
            "SELECT execution_started_at FROM public.operations_job WHERE id = %s",
            [job.pk],
        )
        started = cursor.fetchone()[0]
    assert lower <= started <= upper
    assert started != supplied

    _assert_runtime_rejected(
        "aegis_operations",
        """
        UPDATE public.operations_job
           SET execution_started_at = pg_catalog.clock_timestamp(),
               updated_at = pg_catalog.clock_timestamp()
         WHERE id = %s
        """,
        [job.pk],
    )
    _assert_runtime_rejected(
        "aegis_media",
        "UPDATE public.operations_job SET updated_at = clock_timestamp() WHERE id = %s",
        [job.pk],
        sqlstate="42501",
    )
    _assert_runtime_rejected(
        "aegis_web",
        "UPDATE public.operations_job SET updated_at = clock_timestamp() WHERE id = %s",
        [job.pk],
        sqlstate="42501",
    )

    _execute_as_runtime(
        "aegis_operations",
        """
        UPDATE public.operations_job
           SET state = 'succeeded',
               lease_owner = NULL,
               lease_expires_at = NULL,
               result = '{"ok": true}',
               updated_at = pg_catalog.clock_timestamp()
         WHERE id = %s
        """,
        [job.pk],
    )
    job.refresh_from_db()
    assert job.execution_started_at == started
    _assert_runtime_rejected(
        "aegis_operations",
        """
        UPDATE public.operations_job
           SET execution_started_at = NULL,
               updated_at = pg_catalog.clock_timestamp()
         WHERE id = %s
        """,
        [job.pk],
    )


@pytest.mark.parametrize("role", ["operations", "indexer", "media"])
def test_each_worker_role_can_start_only_its_assigned_job(role: str) -> None:
    job = _job_for_role(role)
    worker_id = str(uuid.uuid4())
    _claim_as_role(role=role, job=job, worker_id=worker_id)

    _execute_as_runtime(
        f"aegis_{role}",
        """
        UPDATE public.operations_job
           SET execution_started_at = '2000-01-01T00:00:00Z',
               updated_at = pg_catalog.clock_timestamp()
         WHERE id = %s
        """,
        [job.pk],
    )

    job.refresh_from_db()
    assert job.execution_started_at is not None
    assert job.execution_started_at.year != 2000


def test_stale_owner_token_and_expired_start_requests_are_rejected() -> None:
    job = _job_for_role("operations")
    worker_id = str(uuid.uuid4())
    _claim_as_role(role="operations", job=job, worker_id=worker_id)

    _assert_runtime_rejected(
        "aegis_operations",
        """
        UPDATE public.operations_job
           SET execution_started_at = pg_catalog.clock_timestamp(),
               attempt_token = attempt_token + 1,
               updated_at = pg_catalog.clock_timestamp()
         WHERE id = %s
        """,
        [job.pk],
    )
    _assert_runtime_rejected(
        "aegis_operations",
        """
        UPDATE public.operations_job
           SET execution_started_at = pg_catalog.clock_timestamp(),
               lease_owner = %s,
               updated_at = pg_catalog.clock_timestamp()
         WHERE id = %s
        """,
        [str(uuid.uuid4()), job.pk],
    )
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE public.operations_job
               SET lease_expires_at = pg_catalog.clock_timestamp() - interval '1 second'
             WHERE id = %s
            """,
            [job.pk],
        )
    _assert_runtime_rejected(
        "aegis_operations",
        """
        UPDATE public.operations_job
           SET execution_started_at = pg_catalog.clock_timestamp(),
               updated_at = pg_catalog.clock_timestamp()
         WHERE id = %s
        """,
        [job.pk],
    )


def test_unstarted_relinquishment_preserves_null_marker_for_immediate_reclaim() -> None:
    job = _job_for_role("operations")
    worker_id = str(uuid.uuid4())
    _claim_as_role(role="operations", job=job, worker_id=worker_id)

    _execute_as_runtime(
        "aegis_operations",
        """
        UPDATE public.operations_job
           SET state = 'retry_wait',
               available_at = pg_catalog.clock_timestamp(),
               lease_owner = NULL,
               lease_expires_at = NULL,
               safe_error_code = 'claim_relinquished',
               safe_error_detail = 'claim released',
               result = NULL,
               updated_at = pg_catalog.clock_timestamp()
         WHERE id = %s
        """,
        [job.pk],
    )

    job.refresh_from_db()
    assert job.state == "retry_wait"
    assert job.execution_started_at is None
    assert job.lease_owner is None
    assert job.lease_expires_at is None


def test_retry_marker_reset_is_bounded_and_expired_takeover_clears_marker() -> None:
    _audit, _operation, job = _records()
    worker_id = str(uuid.uuid4())
    now = timezone.now()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE public.operations_job
               SET state = 'running', attempts = 1, attempt_token = 1,
                   execution_started_at = %s, lease_owner = %s,
                   lease_expires_at = %s, updated_at = %s
             WHERE id = %s
            """,
            [now, worker_id, now + timedelta(minutes=5), now, job.pk],
        )

    _assert_runtime_rejected(
        "aegis_operations",
        """
        UPDATE public.operations_job
           SET state = 'retry_wait',
               available_at = pg_catalog.clock_timestamp() + interval '86406 seconds',
               execution_started_at = NULL,
               lease_owner = NULL,
               lease_expires_at = NULL,
               safe_error_code = 'retryable_failure',
               safe_error_detail = 'bounded retry',
               result = NULL,
               updated_at = pg_catalog.clock_timestamp()
         WHERE id = %s
        """,
        [job.pk],
    )
    _execute_as_runtime(
        "aegis_operations",
        """
        UPDATE public.operations_job
           SET state = 'retry_wait',
               available_at = pg_catalog.clock_timestamp() + interval '86405 seconds',
               execution_started_at = NULL,
               lease_owner = NULL,
               lease_expires_at = NULL,
               safe_error_code = 'retryable_failure',
               safe_error_detail = 'bounded retry',
               result = NULL,
               updated_at = pg_catalog.clock_timestamp()
         WHERE id = %s
        """,
        [job.pk],
    )
    job.refresh_from_db()
    assert job.state == "retry_wait"
    assert job.execution_started_at is None

    takeover_time = timezone.now()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE public.operations_job
               SET state = 'running', attempts = 1, attempt_token = 7,
                   execution_started_at = %s, lease_owner = %s,
                   lease_expires_at = %s, safe_error_code = NULL,
                   safe_error_detail = NULL, updated_at = %s
             WHERE id = %s
            """,
            [
                takeover_time - timedelta(minutes=1),
                worker_id,
                takeover_time - timedelta(seconds=1),
                takeover_time,
                job.pk,
            ],
        )
    replacement_id = str(uuid.uuid4())
    _execute_as_runtime(
        "aegis_operations",
        """
        UPDATE public.operations_job
           SET state = 'running',
               attempts = attempts + 1,
               attempt_token = attempt_token + 1,
               execution_started_at = NULL,
               lease_owner = %s,
               lease_expires_at = pg_catalog.clock_timestamp() + interval '30 seconds',
               safe_error_code = NULL,
               safe_error_detail = NULL,
               result = NULL,
               updated_at = pg_catalog.clock_timestamp()
         WHERE id = %s
        """,
        [replacement_id, job.pk],
    )
    job.refresh_from_db()
    assert job.attempt_token == 8
    assert job.attempts == 2
    assert job.lease_owner == replacement_id
    assert job.execution_started_at is None


def test_schema_owner_flush_and_reverse_migration_remain_available() -> None:
    audit, operation, job = _records()
    assert audit.pk and operation.pk and job.pk

    call_command("flush", interactive=False, verbosity=0)
    assert AuditEvent.objects.count() == 0
    assert Operation.objects.count() == 0
    assert Job.objects.count() == 0

    executor = MigrationExecutor(connection)
    current_leaves = executor.loader.graph.leaf_nodes()
    predecessors = [
        node
        for node in current_leaves
        if node[0] not in {"audit", "operations"}
    ] + [
        ("audit", "0002_auditevent_manager_names"),
        ("operations", "0007_operation_idempotency_namespace_boundary"),
    ]
    try:
        executor.migrate(predecessors)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT proname
                  FROM pg_catalog.pg_proc
                 WHERE proname = ANY(%s)
                """,
                [
                    [
                        "aegis_audit_event_append_only_guard",
                        "aegis_operation_namespace_insert_guard",
                        "aegis_operation_intent_append_only_guard",
                        "aegis_job_immutable_fields_guard",
                        "aegis_job_retention_guard",
                        "aegis_job_execution_marker_guard",
                    ]
                ],
            )
            assert cursor.fetchall() == []
    finally:
        MigrationExecutor(connection).migrate(current_leaves)
