from __future__ import annotations

import signal
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest
from aegis_apps.identity.models import User
from aegis_apps.operations.enums import HeartbeatStatus, JobState, SafeErrorCode
from aegis_apps.operations.leases import (
    LeaseToken,
    claim_next_job,
    start_job_execution,
)
from aegis_apps.operations.management.commands import run_role as worker_command
from aegis_apps.operations.models import Job, Operation, WorkerHeartbeat
from aegis_apps.operations.selectors import SchemaCompatibilityError
from aegis_apps.operations.services import create_operation, enqueue_job
from aegis_apps.roots.models import Root, RootGrant
from aegis_apps.roots.permissions import Permission
from aegisctl.mounts import MountAttestationError
from django.core.management import CommandError, call_command
from django.test import override_settings

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

WORKER_ID = "10000000-0000-4000-8000-000000000001"
SCHEMA_ID = "sha256:" + "a" * 64
MANIFEST_ID = "b" * 64


def _operation(*, role: str = "operations") -> tuple[User, Operation, Job]:
    actor = User.objects.create_user(username=f"worker-{uuid.uuid4()}")
    operation = create_operation(
        actor=actor,
        request_id="task10_worker_request",
        kind="foundation.probe",
        intent={"roots": []},
    )
    if role == "operations":
        return actor, operation, operation.jobs.get()
    return actor, operation, enqueue_job(operation=operation, target_role=role)


def _call_worker(role: str, *, once: bool = True) -> None:
    with patch.object(worker_command, "_new_worker_id", return_value=WORKER_ID):
        call_command("run_role", role=role, once=once)


def _identity(role: worker_command.RoleName = "operations") -> worker_command.WorkerIdentity:
    return worker_command.WorkerIdentity(
        role=role,
        worker_id=WORKER_ID,
        release_id="release-10",
        schema_identity=SCHEMA_ID,
        manifest_identity="unconfigured:v1",
    )


def _claimed_operation_job() -> tuple[LeaseToken, Job]:
    _actor, _operation_record, job = _operation(role="operations")
    lease = claim_next_job("operations", WORKER_ID, job.available_at)
    assert lease is not None
    return lease, job


def _assert_relinquished_before_execution(job: Job) -> None:
    job.refresh_from_db()
    assert job.state == JobState.RETRY_WAIT
    assert job.execution_started_at is None
    assert job.lease_owner is None
    assert job.lease_expires_at is None
    assert job.safe_error_code == SafeErrorCode.CLAIM_RELINQUISHED


@override_settings(
    AEGIS_ENVIRONMENT="production",
    AEGIS_PROCESS_ROLE="indexer",
    AEGIS_RELEASE_ID="release-10",
)
def test_startup_orders_role_validation_attestation_schema_heartbeat_then_claim() -> None:
    events: list[str] = []
    manifest = SimpleNamespace(digest=MANIFEST_ID)

    def load_manifest() -> SimpleNamespace:
        events.append("manifest")
        return manifest

    def verify_database_login(role: str) -> None:
        events.append(f"database:{role}")

    def attest(candidate: object, role: str) -> None:
        assert candidate is manifest
        events.append(f"attest:{role}")

    def schema() -> str:
        events.append("schema")
        return SCHEMA_ID

    def heartbeat(**kwargs: object) -> None:
        events.append(f"heartbeat:{kwargs['status']}")

    def claim_job(role: str, worker_id: str, now: object) -> None:
        del now
        assert worker_id == WORKER_ID
        events.append(f"claim:{role}")
        return None

    with (
        patch.object(
            worker_command,
            "require_runtime_database_login",
            side_effect=verify_database_login,
            create=True,
        ),
        patch.object(worker_command, "configured_manifest", side_effect=load_manifest),
        patch.object(worker_command, "attest_mounts", side_effect=attest),
        patch.object(worker_command, "current_schema_identity", side_effect=schema),
        patch.object(worker_command, "publish_heartbeat", side_effect=heartbeat),
        patch.object(worker_command, "claim_next_job", side_effect=claim_job),
    ):
        _call_worker("indexer")

    assert events == [
        "database:indexer",
        "manifest",
        "attest:indexer",
        "schema",
        f"heartbeat:{HeartbeatStatus.IDLE}",
        "claim:indexer",
        f"heartbeat:{HeartbeatStatus.STOPPING}",
    ]


@override_settings(
    AEGIS_ENVIRONMENT="production",
    AEGIS_PROCESS_ROLE="operations",
    AEGIS_RELEASE_ID="release-10",
)
def test_database_login_mismatch_fails_before_manifest_or_worker_work() -> None:
    with (
        patch.object(
            worker_command,
            "require_runtime_database_login",
            side_effect=ValueError("private database identity"),
            create=True,
        ),
        patch.object(worker_command, "configured_manifest") as manifest,
        patch.object(worker_command, "current_schema_identity") as schema,
        patch.object(worker_command, "publish_heartbeat") as heartbeat,
        patch.object(worker_command, "claim_next_job") as claim_job,
        pytest.raises(CommandError, match=r"^worker startup failed$") as caught,
    ):
        _call_worker("operations")

    assert "identity" not in str(caught.value)
    manifest.assert_not_called()
    schema.assert_not_called()
    heartbeat.assert_not_called()
    claim_job.assert_not_called()


@override_settings(AEGIS_PROCESS_ROLE="media")
def test_process_role_mismatch_fails_before_manifest_or_database_work() -> None:
    with (
        patch.object(worker_command, "configured_manifest") as manifest,
        patch.object(worker_command, "current_schema_identity") as schema,
        patch.object(worker_command, "publish_heartbeat") as heartbeat,
        patch.object(worker_command, "claim_next_job") as claim_job,
        pytest.raises(CommandError, match=r"^worker startup failed$") as caught,
    ):
        _call_worker("operations")

    assert "media" not in str(caught.value)
    manifest.assert_not_called()
    schema.assert_not_called()
    heartbeat.assert_not_called()
    claim_job.assert_not_called()


@pytest.mark.parametrize("release_id", ["", "development"])
@override_settings(AEGIS_ENVIRONMENT="production", AEGIS_PROCESS_ROLE="operations")
def test_production_startup_rejects_placeholder_release_before_attestation(
    release_id: str,
) -> None:
    with (
        override_settings(AEGIS_RELEASE_ID=release_id),
        patch.object(worker_command, "configured_manifest") as manifest,
        patch.object(worker_command, "current_schema_identity") as schema,
        patch.object(worker_command, "publish_heartbeat") as heartbeat,
        patch.object(worker_command, "claim_next_job") as claim_job,
        pytest.raises(CommandError, match=r"^worker startup failed$"),
    ):
        _call_worker("operations")

    manifest.assert_not_called()
    schema.assert_not_called()
    heartbeat.assert_not_called()
    claim_job.assert_not_called()


@override_settings(AEGIS_PROCESS_ROLE="operations")
def test_attestation_failure_is_generic_and_precedes_schema_heartbeat_and_claim() -> None:
    manifest = SimpleNamespace(digest=MANIFEST_ID)
    with (
        patch.object(worker_command, "configured_manifest", return_value=manifest),
        patch.object(
            worker_command,
            "attest_mounts",
            side_effect=MountAttestationError("/private/root must never escape"),
        ),
        patch.object(worker_command, "current_schema_identity") as schema,
        patch.object(worker_command, "publish_heartbeat") as heartbeat,
        patch.object(worker_command, "claim_next_job") as claim_job,
        pytest.raises(CommandError, match=r"^worker startup failed$") as caught,
    ):
        _call_worker("operations")

    assert "/private/root" not in str(caught.value)
    schema.assert_not_called()
    heartbeat.assert_not_called()
    claim_job.assert_not_called()


@override_settings(AEGIS_PROCESS_ROLE="operations")
def test_schema_failure_precedes_heartbeat_and_claim() -> None:
    with (
        patch.object(worker_command, "configured_manifest", return_value=None),
        patch.object(
            worker_command,
            "current_schema_identity",
            side_effect=SchemaCompatibilityError("pending private migration"),
        ),
        patch.object(worker_command, "publish_heartbeat") as heartbeat,
        patch.object(worker_command, "claim_next_job") as claim_job,
        pytest.raises(CommandError, match=r"^worker startup failed$") as caught,
    ):
        _call_worker("operations")

    assert "migration" not in str(caught.value)
    heartbeat.assert_not_called()
    claim_job.assert_not_called()


@override_settings(AEGIS_PROCESS_ROLE="indexer")
def test_once_claims_only_its_role_and_finishes_foundation_probe() -> None:
    _actor, operation, indexer_job = _operation(role="indexer")
    operations_job = operation.jobs.get(target_role="operations")

    _call_worker("indexer")

    indexer_job.refresh_from_db()
    operations_job.refresh_from_db()
    assert indexer_job.state == JobState.SUCCEEDED
    assert indexer_job.result == {"ok": True}
    assert operations_job.state == JobState.QUEUED
    heartbeat = WorkerHeartbeat.objects.get(role="indexer", worker_id=WORKER_ID)
    assert heartbeat.status == HeartbeatStatus.STOPPING
    assert heartbeat.current_job_id is None


@override_settings(AEGIS_PROCESS_ROLE="operations")
def test_dispatch_revalidates_authorization_before_handler_work() -> None:
    actor = User.objects.create_user(username="worker-authorization-stale")
    root = Root.objects.create(
        slot_id="worker-authorization-root",
        display_name="Opaque root",
        mode=Root.Mode.READ_ONLY,
        active=True,
    )
    RootGrant.objects.create(root=root, user=actor, permissions=int(Permission.BROWSE))
    operation = create_operation(
        actor=actor,
        request_id="task10_worker_stale",
        kind="foundation.probe",
        intent={"roots": [{"id": str(root.id), "permissions": int(Permission.BROWSE)}]},
    )
    actor.authorization_epoch += 1
    actor.save(update_fields=("authorization_epoch",))
    handler = Mock(return_value={"ok": True})

    with patch.dict(
        worker_command.ROLE_HANDLERS["operations"],
        {"foundation.probe": handler},
    ):
        _call_worker("operations")

    handler.assert_not_called()
    job = operation.jobs.get()
    assert job.state == JobState.FAILED
    assert job.safe_error_code == SafeErrorCode.AUTHORIZATION_STALE
    assert job.result is None


@override_settings(AEGIS_PROCESS_ROLE="media")
def test_shutdown_requested_during_handler_finishes_job_stops_claiming_and_heartbeats() -> None:
    _actor, _operation_record, media_job = _operation(role="media")
    handled: list[uuid.UUID] = []

    def handler(lease: LeaseToken) -> dict[str, bool]:
        handled.append(lease.job_id)
        worker_command.request_shutdown()
        return {"ok": True}

    with (
        patch.dict(
            worker_command.ROLE_HANDLERS["media"],
            {"foundation.probe": handler},
        ),
        patch.object(signal, "signal", wraps=signal.signal) as install_signal,
        patch(
            "aegis_apps.operations.management.commands.run_role.claim_next_job",
            wraps=claim_next_job,
        ) as claim,
    ):
        _call_worker("media", once=False)

    assert handled == [media_job.id]
    assert claim.call_count == 1
    media_job.refresh_from_db()
    assert media_job.state == JobState.SUCCEEDED
    heartbeat = WorkerHeartbeat.objects.get(role="media", worker_id=WORKER_ID)
    assert heartbeat.status == HeartbeatStatus.STOPPING
    installed = [item.args[:2] for item in install_signal.call_args_list[:2]]
    assert installed[0][0] == signal.SIGTERM
    assert installed[1][0] == signal.SIGINT


@override_settings(AEGIS_PROCESS_ROLE="operations")
def test_shutdown_after_loop_check_prevents_claim() -> None:
    identity = worker_command.WorkerIdentity(
        role="operations",
        worker_id=WORKER_ID,
        release_id="release-10",
        schema_identity=SCHEMA_ID,
        manifest_identity="unconfigured:v1",
    )
    monotonic_calls = 0

    def signal_between_loop_check_and_claim() -> float:
        nonlocal monotonic_calls
        monotonic_calls += 1
        if monotonic_calls == 2:
            worker_command.request_shutdown()
        return 0.0

    worker_command._reset_shutdown()
    try:
        with (
            patch.object(worker_command, "_startup", return_value=identity),
            patch.object(worker_command, "_publish"),
            patch.object(worker_command, "claim_next_job", return_value=None) as claim,
        ):
            worker_command.run_worker(
                role="operations",
                once=False,
                worker_id=WORKER_ID,
                monotonic=signal_between_loop_check_and_claim,
            )
    finally:
        worker_command._reset_shutdown()

    claim.assert_not_called()


@override_settings(AEGIS_PROCESS_ROLE="operations")
def test_shutdown_during_claim_relinquishes_without_dispatch() -> None:
    _actor, _operation_record, job = _operation(role="operations")
    identity = worker_command.WorkerIdentity(
        role="operations",
        worker_id=WORKER_ID,
        release_id="release-10",
        schema_identity=SCHEMA_ID,
        manifest_identity="unconfigured:v1",
    )
    handler = Mock(return_value={"ok": True})

    def claim_then_signal(role: str, worker_id: str, now: object) -> LeaseToken | None:
        assert isinstance(now, type(job.available_at))
        lease = claim_next_job(role, worker_id, now)
        worker_command.request_shutdown()
        return lease

    worker_command._reset_shutdown()
    try:
        with (
            patch.object(worker_command, "_startup", return_value=identity),
            patch.object(worker_command, "_publish"),
            patch.object(worker_command, "claim_next_job", side_effect=claim_then_signal),
            patch.dict(
                worker_command.ROLE_HANDLERS["operations"],
                {"foundation.probe": handler},
            ),
        ):
            worker_command.run_worker(
                role="operations",
                once=False,
                worker_id=WORKER_ID,
            )
    finally:
        worker_command._reset_shutdown()

    handler.assert_not_called()
    job.refresh_from_db()
    assert job.state == JobState.RETRY_WAIT
    assert job.lease_owner is None
    assert job.lease_expires_at is None
    replacement = claim_next_job("operations", str(uuid.uuid4()), job.available_at)
    assert replacement is not None
    assert replacement.job_id == job.pk


@override_settings(AEGIS_PROCESS_ROLE="operations")
def test_shutdown_during_live_claim_load_relinquishes_before_dispatch() -> None:
    lease, job = _claimed_operation_job()
    handler = Mock(return_value={"ok": True})
    load_live_claim = worker_command._live_claim_job

    def load_then_signal(candidate: LeaseToken, *, role: worker_command.RoleName) -> Job | None:
        live_job = load_live_claim(candidate, role=role)
        worker_command.request_shutdown()
        return live_job

    worker_command._reset_shutdown()
    try:
        with (
            patch.object(worker_command, "_live_claim_job", side_effect=load_then_signal),
            patch.object(worker_command, "_publish"),
            patch.dict(
                worker_command.ROLE_HANDLERS["operations"],
                {"foundation.probe": handler},
            ),
        ):
            worker_command._execute_claim(_identity(), lease)
    finally:
        worker_command._reset_shutdown()

    handler.assert_not_called()
    _assert_relinquished_before_execution(job)


@override_settings(AEGIS_PROCESS_ROLE="operations")
def test_shutdown_during_authorization_validation_relinquishes_before_dispatch() -> None:
    lease, job = _claimed_operation_job()
    handler = Mock(return_value={"ok": True})

    def authorize_then_signal(_operation_record: Operation) -> bool:
        worker_command.request_shutdown()
        return True

    worker_command._reset_shutdown()
    try:
        with (
            patch.object(
                worker_command,
                "validate_authorization_snapshot",
                side_effect=authorize_then_signal,
            ),
            patch.object(worker_command, "_publish"),
            patch.dict(
                worker_command.ROLE_HANDLERS["operations"],
                {"foundation.probe": handler},
            ),
        ):
            worker_command._execute_claim(_identity(), lease)
    finally:
        worker_command._reset_shutdown()

    handler.assert_not_called()
    _assert_relinquished_before_execution(job)


@override_settings(AEGIS_PROCESS_ROLE="operations")
def test_shutdown_during_running_publication_relinquishes_before_dispatch() -> None:
    lease, job = _claimed_operation_job()
    handler = Mock(return_value={"ok": True})

    def publish_then_signal(
        _candidate: worker_command.WorkerIdentity,
        *,
        status: HeartbeatStatus,
        job_id: uuid.UUID | None,
    ) -> None:
        assert status == HeartbeatStatus.RUNNING
        assert job_id == job.pk
        worker_command.request_shutdown()

    worker_command._reset_shutdown()
    try:
        with (
            patch.object(worker_command, "_publish", side_effect=publish_then_signal),
            patch.dict(
                worker_command.ROLE_HANDLERS["operations"],
                {"foundation.probe": handler},
            ),
        ):
            worker_command._execute_claim(_identity(), lease)
    finally:
        worker_command._reset_shutdown()

    handler.assert_not_called()
    _assert_relinquished_before_execution(job)


@override_settings(AEGIS_PROCESS_ROLE="operations")
def test_signal_observed_immediately_before_durable_start_prevents_dispatch() -> None:
    lease, job = _claimed_operation_job()
    handler = Mock(return_value={"ok": True})
    serialized_start = worker_command._serialized_execution_start

    @contextmanager
    def signal_before_start() -> Iterator[None]:
        signal.raise_signal(signal.SIGTERM)
        with serialized_start():
            yield

    worker_command._reset_shutdown()
    try:
        with (
            worker_command._installed_signal_handlers(),
            patch.object(
                worker_command,
                "_serialized_execution_start",
                signal_before_start,
            ),
            patch.object(worker_command, "_publish"),
            patch.dict(
                worker_command.ROLE_HANDLERS["operations"],
                {"foundation.probe": handler},
            ),
        ):
            worker_command._execute_claim(_identity(), lease)
    finally:
        worker_command._reset_shutdown()

    handler.assert_not_called()
    _assert_relinquished_before_execution(job)


@override_settings(AEGIS_PROCESS_ROLE="operations")
def test_signal_observed_immediately_after_durable_start_allows_current_dispatch() -> None:
    lease, job = _claimed_operation_job()
    handler = Mock(return_value={"ok": True})
    durable_start = start_job_execution

    def start_then_signal(candidate: LeaseToken, *, now: datetime) -> bool:
        started = durable_start(candidate, now=now)
        signal.raise_signal(signal.SIGTERM)
        return started

    worker_command._reset_shutdown()
    try:
        with (
            worker_command._installed_signal_handlers(),
            patch.object(worker_command, "start_job_execution", side_effect=start_then_signal),
            patch.object(worker_command, "_publish"),
            patch.dict(
                worker_command.ROLE_HANDLERS["operations"],
                {"foundation.probe": handler},
            ),
        ):
            worker_command._execute_claim(_identity(), lease)
            assert worker_command._shutdown_requested()
    finally:
        worker_command._reset_shutdown()

    handler.assert_called_once_with(lease)
    job.refresh_from_db()
    assert job.state == JobState.SUCCEEDED
    assert job.execution_started_at is not None
    assert job.result == {"ok": True}


def test_handler_registry_is_closed_for_all_three_roles() -> None:
    assert tuple(worker_command.ROLE_HANDLERS) == ("operations", "indexer", "media")
    for handlers in worker_command.ROLE_HANDLERS.values():
        assert handlers == {"foundation.probe": worker_command.run_foundation_probe}


@override_settings(AEGIS_PROCESS_ROLE="operations")
def test_command_generates_one_opaque_uuid_and_reuses_it_for_heartbeat_and_claim() -> None:
    observed_worker_ids: list[str] = []

    def heartbeat(**kwargs: object) -> None:
        observed_worker_ids.append(str(kwargs["worker_id"]))

    def claim_job(role: str, worker_id: str, now: object) -> None:
        del role, now
        observed_worker_ids.append(worker_id)
        return None

    with (
        patch.object(worker_command, "configured_manifest", return_value=None),
        patch.object(worker_command, "current_schema_identity", return_value=SCHEMA_ID),
        patch.object(worker_command, "publish_heartbeat", side_effect=heartbeat),
        patch.object(worker_command, "claim_next_job", side_effect=claim_job),
    ):
        call_command("run_role", role="operations", once=True)

    assert len(observed_worker_ids) == 3
    assert len(set(observed_worker_ids)) == 1
    assert str(uuid.UUID(observed_worker_ids[0])) == observed_worker_ids[0]


@override_settings(AEGIS_PROCESS_ROLE="operations")
def test_once_claims_at_most_once_and_never_sleeps() -> None:
    identity = worker_command.WorkerIdentity(
        role="operations",
        worker_id=WORKER_ID,
        release_id="release-10",
        schema_identity=SCHEMA_ID,
        manifest_identity="unconfigured:v1",
    )
    sleeper = Mock()
    worker_command._reset_shutdown()
    with (
        patch.object(worker_command, "_startup", return_value=identity),
        patch.object(worker_command, "_publish"),
        patch.object(worker_command, "claim_next_job", return_value=None) as claim_job,
    ):
        worker_command.run_worker(
            role="operations",
            once=True,
            worker_id=WORKER_ID,
            sleep=sleeper,
        )

    claim_job.assert_called_once()
    sleeper.assert_not_called()


@override_settings(
    AEGIS_PROCESS_ROLE="operations",
    AEGIS_WORKER_HEARTBEAT_SECONDS=10.0,
    AEGIS_QUEUE_POLL_SECONDS=1.0,
    AEGIS_QUEUE_POLL_JITTER_SECONDS=0.25,
)
def test_normal_polling_has_bounded_jitter_no_busy_wait_and_periodic_heartbeat() -> None:
    identity = worker_command.WorkerIdentity(
        role="operations",
        worker_id=WORKER_ID,
        release_id="release-10",
        schema_identity=SCHEMA_ID,
        manifest_identity="unconfigured:v1",
    )
    monotonic_now = 0.0
    sleeps: list[float] = []
    statuses: list[HeartbeatStatus] = []
    claims = 0

    def monotonic() -> float:
        return monotonic_now

    def sleep(delay: float) -> None:
        nonlocal monotonic_now
        sleeps.append(delay)
        monotonic_now += delay

    def claim_job(role: str, worker_id: str, now: object) -> None:
        nonlocal claims
        del role, worker_id, now
        claims += 1
        if claims == 9:
            worker_command.request_shutdown()
        return None

    def publish(
        candidate: worker_command.WorkerIdentity,
        *,
        status: HeartbeatStatus,
        job_id: uuid.UUID | None,
    ) -> None:
        del candidate, job_id
        statuses.append(status)

    worker_command._reset_shutdown()
    with (
        patch.object(worker_command, "_startup", return_value=identity),
        patch.object(worker_command, "_publish", side_effect=publish),
        patch.object(worker_command, "claim_next_job", side_effect=claim_job),
    ):
        worker_command.run_worker(
            role="operations",
            once=False,
            worker_id=WORKER_ID,
            sleep=sleep,
            monotonic=monotonic,
            jitter=lambda low, high: high,
        )

    assert sleeps
    assert all(0 < delay <= 1.25 for delay in sleeps)
    assert monotonic_now == 10.0
    assert statuses == [HeartbeatStatus.IDLE, HeartbeatStatus.STOPPING]


@override_settings(AEGIS_PROCESS_ROLE="operations")
def test_handler_exception_persists_only_the_fixed_safe_failure() -> None:
    _actor, operation, _job = _operation()
    handler = Mock(side_effect=RuntimeError("/private/root secret-token private-name.mov"))

    with patch.dict(
        worker_command.ROLE_HANDLERS["operations"],
        {"foundation.probe": handler},
    ):
        _call_worker("operations")

    job = operation.jobs.get()
    assert job.state == JobState.FAILED
    assert job.safe_error_code == SafeErrorCode.HANDLER_FAILED
    assert job.safe_error_detail == "The job handler failed safely."
    assert "private" not in job.safe_error_detail.lower()


@override_settings(AEGIS_PROCESS_ROLE="operations")
def test_management_command_does_not_accept_an_operator_worker_id() -> None:
    with (
        patch.object(worker_command, "configured_manifest") as manifest,
        pytest.raises(TypeError, match="Unknown option"),
    ):
        call_command("run_role", role="operations", once=True, worker_id=WORKER_ID)

    manifest.assert_not_called()


@override_settings(AEGIS_PROCESS_ROLE="operations")
def test_signal_handlers_are_restored_after_once_run() -> None:
    previous_term = Mock()
    previous_int = Mock()
    with (
        patch.object(worker_command, "configured_manifest", return_value=None),
        patch.object(worker_command, "current_schema_identity", return_value=SCHEMA_ID),
        patch.object(worker_command, "publish_heartbeat"),
        patch.object(worker_command, "claim_next_job", return_value=None),
        patch.object(
            signal,
            "getsignal",
            side_effect=(previous_term, previous_int),
        ),
        patch.object(signal, "signal") as set_signal,
    ):
        _call_worker("operations")

    assert set_signal.call_args_list[-2:] == [
        call(signal.SIGINT, previous_int),
        call(signal.SIGTERM, previous_term),
    ]
