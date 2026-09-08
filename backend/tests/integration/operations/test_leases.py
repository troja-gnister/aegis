from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier

import pytest
from aegis_apps.identity.models import User
from aegis_apps.operations.enums import JobState, SafeErrorCode
from aegis_apps.operations.leases import (
    LeaseToken,
    claim_next_job,
    fail_job,
    finish_job,
    renew_lease,
    retry_job,
)
from aegis_apps.operations.models import Job
from aegis_apps.operations.services import (
    create_operation,
    enqueue_job,
    validate_authorization_snapshot,
)
from aegis_apps.roots.models import Root, RootGrant
from aegis_apps.roots.permissions import Permission
from django.db import close_old_connections
from django.utils import timezone

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]
REQUEST_ID = "task10_lease_request"


def _worker() -> str:
    return str(uuid.uuid4())


def _operation(*, username: str | None = None) -> tuple[User, Job]:
    actor = User.objects.create_user(username=username or f"lease-{uuid.uuid4()}")
    operation = create_operation(
        actor=actor,
        request_id=REQUEST_ID,
        kind="foundation.probe",
        intent={"roots": []},
    )
    return actor, operation.jobs.get()


def test_only_one_concurrent_worker_claims_a_job() -> None:
    _, job = _operation()
    now = timezone.now()
    workers = (_worker(), _worker())
    barrier = Barrier(2)

    def claim(worker_id: str) -> LeaseToken | None:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return claim_next_job("operations", worker_id, now)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, workers))

    leases = [lease for lease in claims if lease is not None]
    assert len(leases) == 1
    job.refresh_from_db()
    assert job.state == JobState.RUNNING
    assert job.attempts == job.attempt_token == 1
    assert job.lease_owner == leases[0].worker_id


def test_claim_orders_by_priority_availability_then_uuid_and_isolates_roles() -> None:
    now = timezone.now()
    jobs: list[Job] = []
    for priority, available_offset in ((2, -5), (7, -1), (7, -4)):
        _, initial = _operation()
        jobs.append(
            enqueue_job(
                operation=initial.operation,
                target_role="media",
                priority=priority,
                available_at=now + timedelta(seconds=available_offset),
            )
        )
    _, operations_job = _operation()

    first = claim_next_job("media", _worker(), now)
    second = claim_next_job("media", _worker(), now)
    third = claim_next_job("media", _worker(), now)

    assert first is not None and first.job_id == jobs[2].id
    assert second is not None and second.job_id == jobs[1].id
    assert third is not None and third.job_id == jobs[0].id
    operations_job.refresh_from_db()
    assert operations_job.state == JobState.QUEUED


def test_expired_takeover_advances_fence_and_stale_worker_cannot_finish() -> None:
    _, job = _operation()
    now = timezone.now()
    stale = claim_next_job("operations", _worker(), now)
    assert stale is not None

    takeover_time = stale.expires_at
    replacement = claim_next_job("operations", _worker(), takeover_time)

    assert replacement is not None
    assert replacement.job_id == stale.job_id == job.id
    assert replacement.attempt_token == stale.attempt_token + 1
    assert finish_job(stale, result={"ok": True}, now=takeover_time) is False
    assert finish_job(replacement, result={"ok": True}, now=takeover_time) is True
    job.refresh_from_db()
    assert job.state == JobState.SUCCEEDED
    assert job.result == {"ok": True}
    assert job.lease_owner is None
    assert job.lease_expires_at is None


def test_renew_finish_and_fail_require_exact_owner_token_and_a_strictly_live_lease() -> None:
    _, job = _operation()
    now = timezone.now()
    lease = claim_next_job("operations", _worker(), now)
    assert lease is not None
    wrong_owner = replace(lease, worker_id=_worker())
    wrong_token = replace(lease, attempt_token=lease.attempt_token + 1)

    assert renew_lease(wrong_owner, now=now) is False
    assert finish_job(wrong_token, result={"ok": True}, now=now) is False
    assert fail_job(wrong_owner, error_code="handler_failed", now=now) is False
    assert renew_lease(lease, now=now) is True
    assert finish_job(lease, result={"ok": True}, now=lease.expires_at) is False

    job.refresh_from_db()
    assert job.state == JobState.RUNNING
    assert job.result is None
    assert job.safe_error_code is None


def test_retry_uses_bounded_backoff_and_exhausts_at_max_attempts() -> None:
    actor = User.objects.create_user(username="retry-actor")
    operation = create_operation(
        actor=actor,
        request_id=REQUEST_ID,
        kind="foundation.probe",
        intent={"roots": []},
    )
    job = enqueue_job(
        operation=operation,
        target_role="media",
        max_attempts=2,
    )
    now = timezone.now()
    first = claim_next_job("media", _worker(), now)
    assert first is not None

    assert retry_job(
        first,
        error_code="retryable_failure",
        now=now,
        jitter_seconds=0.25,
    ) is True
    job.refresh_from_db()
    assert job.state == JobState.RETRY_WAIT
    assert job.available_at == now + timedelta(seconds=1.25)
    assert job.safe_error_code == SafeErrorCode.RETRYABLE_FAILURE
    assert job.safe_error_detail == "The job will be retried."

    second = claim_next_job("media", _worker(), job.available_at)
    assert second is not None
    assert retry_job(
        second,
        error_code="retryable_failure",
        detail="/private/secret-file token=credential",
        now=job.available_at,
        jitter_seconds=0,
    ) is True
    job.refresh_from_db()
    assert job.state == JobState.FAILED
    assert job.safe_error_code == SafeErrorCode.ATTEMPTS_EXHAUSTED
    assert job.safe_error_detail == "The job exhausted its attempts."
    assert "secret" not in job.safe_error_detail


def test_expired_final_attempt_is_terminally_fenced_and_claim_loop_advances() -> None:
    actor = User.objects.create_user(username="expired-final-actor")
    exhausted_operation = create_operation(
        actor=actor,
        request_id=REQUEST_ID,
        kind="foundation.probe",
        intent={"roots": []},
    )
    exhausted = enqueue_job(
        operation=exhausted_operation,
        target_role="media",
        max_attempts=1,
        priority=10,
    )
    _, next_initial = _operation()
    next_job = enqueue_job(
        operation=next_initial.operation,
        target_role="media",
        max_attempts=2,
        priority=0,
    )
    now = timezone.now()
    first = claim_next_job("media", _worker(), now)
    assert first is not None and first.job_id == exhausted.id

    following = claim_next_job("media", _worker(), first.expires_at)

    assert following is not None and following.job_id == next_job.id
    exhausted.refresh_from_db()
    assert exhausted.state == JobState.FAILED
    assert exhausted.safe_error_code == SafeErrorCode.ATTEMPTS_EXHAUSTED
    assert exhausted.attempt_token == first.attempt_token + 1


def test_invalid_result_and_error_inputs_make_no_partial_change() -> None:
    _, job = _operation()
    now = timezone.now()
    lease = claim_next_job("operations", _worker(), now)
    assert lease is not None

    with pytest.raises(ValueError, match="result"):
        finish_job(lease, result={"path": "/sensitive/file"}, now=now)
    with pytest.raises(ValueError, match="error code"):
        fail_job(lease, error_code="/sensitive/error", now=now)
    with pytest.raises(ValueError, match="jitter"):
        retry_job(
            lease,
            error_code="retryable_failure",
            now=now,
            jitter_seconds=float("nan"),
        )

    job.refresh_from_db()
    assert job.state == JobState.RUNNING
    assert job.result is None
    assert job.safe_error_code is None


def test_authorization_is_revalidated_before_completion_without_staff_bypass() -> None:
    actor = User.objects.create_user(
        username="authorization-lease-actor",
        is_staff=True,
        is_superuser=True,
    )
    root = Root.objects.create(
        slot_id="authorization-lease-root",
        display_name="Authorization lease root",
        mode=Root.Mode.READ_ONLY,
        active=True,
    )
    RootGrant.objects.create(root=root, user=actor, permissions=int(Permission.BROWSE))
    operation = create_operation(
        actor=actor,
        request_id=REQUEST_ID,
        kind="foundation.probe",
        intent={"roots": [{"id": str(root.id), "permissions": int(Permission.BROWSE)}]},
    )
    assert validate_authorization_snapshot(operation) is True
    now = timezone.now()
    lease = claim_next_job("operations", _worker(), now)
    assert lease is not None

    RootGrant.objects.filter(root=root, user=actor).update(permissions=int(Permission.PREVIEW))
    assert validate_authorization_snapshot(operation) is False
    assert finish_job(lease, result={"ok": True}, now=now) is False

    job = operation.jobs.get()
    assert job.state == JobState.FAILED
    assert job.safe_error_code == SafeErrorCode.AUTHORIZATION_STALE
    assert job.safe_error_detail == "Authorization changed before execution completed."
    assert job.result is None


def test_actor_or_root_epoch_change_invalidates_authorization_snapshot() -> None:
    actor = User.objects.create_user(username="epoch-validation-actor")
    root = Root.objects.create(
        slot_id="epoch-validation-root",
        display_name="Epoch validation root",
        mode=Root.Mode.READ_ONLY,
        active=True,
    )
    RootGrant.objects.create(root=root, user=actor, permissions=int(Permission.BROWSE))
    operation = create_operation(
        actor=actor,
        request_id=REQUEST_ID,
        kind="foundation.probe",
        intent={"roots": [{"id": str(root.id), "permissions": 1}]},
    )

    actor.authorization_epoch += 1
    actor.save(update_fields=("authorization_epoch",))
    assert validate_authorization_snapshot(operation) is False
    actor.authorization_epoch -= 1
    actor.save(update_fields=("authorization_epoch",))
    root.authorization_epoch += 1
    root.save(update_fields=("authorization_epoch",))
    assert validate_authorization_snapshot(operation) is False


def test_invalid_role_worker_or_time_is_rejected_before_claiming() -> None:
    _, job = _operation()
    now = timezone.now()
    with pytest.raises(ValueError, match="role"):
        claim_next_job("unknown", _worker(), now)
    with pytest.raises(ValueError, match="worker"):
        claim_next_job("operations", "hostname-is-forbidden", now)
    with pytest.raises(ValueError, match="time"):
        claim_next_job("operations", _worker(), now.replace(tzinfo=None))
    job.refresh_from_db()
    assert job.state == JobState.QUEUED
