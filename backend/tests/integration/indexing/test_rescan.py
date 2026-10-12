from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from aegis_apps.audit.models import AuditEvent
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.identity.models import User
from aegis_apps.indexing.models import DirectoryWork, RootIndexState, ScanRequest, ScanRun
from aegis_apps.roots.models import Root, RootGrant
from django.contrib.auth.models import Group
from django.db import connection
from django.utils import timezone

from tests.deployment.test_database_roles import _create_scan_fixture
from tests.support.database_roles import RoleDatabase

if TYPE_CHECKING:
    from conftest import ScanFixture

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


@pytest.mark.parametrize("grant", ["direct", "group", "browse", "superuser", "inactive", "epoch"])
def test_manual_request_requires_live_root_admin(role_database: RoleDatabase, grant: str) -> None:
    from aegis_apps.indexing.services import request_root_scan

    root, _, _ = _create_scan_fixture()
    actor = User.objects.create_user(username=f"manual-{uuid.uuid4().hex}")
    if grant == "group":
        group = Group.objects.create(name=f"group-{uuid.uuid4().hex}")
        actor.groups.add(group)
        actor.refresh_from_db()  # Membership changes advance the account epoch.
        RootGrant.objects.create(root=root, group=group, permissions=128)
        RootGrant.objects.create(root=root, user=actor, permissions=1)
    elif grant != "superuser":
        RootGrant.objects.create(root=root, user=actor, permissions=1 if grant == "browse" else 128)
    if grant == "superuser":
        User.objects.filter(pk=actor.pk).update(is_superuser=True)
    elif grant == "inactive":
        User.objects.filter(pk=actor.pk).update(is_active=False)
    elif grant == "epoch":
        User.objects.filter(pk=actor.pk).update(authorization_epoch=actor.authorization_epoch + 1)
    with role_database.as_django_role("aegis_web"):
        if grant in ("direct", "group"):
            run_id = request_root_scan(actor, root.pk, "request_123")
        else:
            with pytest.raises(PermissionError):
                request_root_scan(actor, root.pk, "request_123")
            assert not ScanRequest.objects.exists()
            return
    event = AuditEvent.objects.get(event_type="index.scan.requested")
    assert (event.actor_id, event.root_id, event.object_id, event.request_id) == (
        actor.pk,
        root.pk,
        run_id,
        "request_123",
    )
    assert event.metadata == {}


def test_idempotency_rechecks_authority_and_root(role_database: RoleDatabase) -> None:
    from aegis_apps.indexing.services import request_root_scan

    root, _, _ = _create_scan_fixture()
    other, _, _ = _create_scan_fixture()
    actor = User.objects.create_user(username=f"replay-{uuid.uuid4().hex}")
    grant = RootGrant.objects.create(root=root, user=actor, permissions=128)
    RootGrant.objects.create(root=other, user=actor, permissions=128)
    with role_database.as_django_role("aegis_web"):
        run_id = request_root_scan(actor, root.pk, "replay_123")
        assert request_root_scan(actor, root.pk, "replay_123") == run_id
        with pytest.raises(ValueError):
            request_root_scan(actor, other.pk, "replay_123")
    assert (
        ScanRequest.objects.count()
        == AuditEvent.objects.filter(event_type="index.scan.requested").count()
        == 1
    )
    grant.permissions = 1
    grant.save(update_fields=("permissions",))
    with role_database.as_django_role("aegis_web"), pytest.raises(PermissionError):
        request_root_scan(actor, root.pk, "replay_123")


def test_manual_rate_limit_does_not_stop_periodic_maintenance(role_database: RoleDatabase) -> None:
    from aegis_apps.indexing.scheduling import schedule_root_scan
    from aegis_apps.indexing.services import ScanRequestThrottled, request_root_scan

    root, worker, digest = _create_scan_fixture()
    actor = User.objects.create_user(username=f"limit-{uuid.uuid4().hex}")
    RootGrant.objects.create(root=root, user=actor, permissions=128)
    with role_database.as_django_role("aegis_web"):
        for index in range(20):
            run_id = request_root_scan(actor, root.pk, f"request_{index:04}")
        with pytest.raises(ScanRequestThrottled) as caught:
            request_root_scan(actor, root.pk, "request_0020")
        assert 1 <= caught.value.retry_after <= 60
        assert request_root_scan(actor, root.pk, "request_0000") == run_id
    User.objects.filter(pk=actor.pk).update(authorization_epoch=2, is_active=False)
    with role_database.as_django_role("aegis_indexer"):
        assert schedule_root_scan(root.pk, worker, digest) == run_id
    assert ScanRequest.objects.count() == 20
    assert ScanRun.objects.count() == 1


def test_audit_failure_rolls_back_request_and_scan(role_database: RoleDatabase) -> None:
    import psycopg

    root, _, _ = _create_scan_fixture()
    actor = User.objects.create_user(username=f"audit-{uuid.uuid4().hex}")
    RootGrant.objects.create(root=root, user=actor, permissions=128)
    with role_database.connect("aegis_migrator") as migrator:
        migrator.execute(
            "ALTER TABLE public.audit_auditevent ADD CONSTRAINT "
            "task4_reject_scan_audit CHECK (event_type <> 'index.scan.requested') NOT VALID"
        )
        try:
            with (
                role_database.connect("aegis_web") as web,
                pytest.raises(psycopg.errors.CheckViolation),
            ):
                web.execute(
                    "SELECT public.aegis_request_root_scan(%s,%s,%s,%s)",
                    [root.pk, actor.pk, actor.authorization_epoch, "audit_rollback"],
                )
            assert not ScanRequest.objects.exists()
            assert not ScanRun.objects.exists()
            assert not AuditEvent.objects.filter(event_type="index.scan.requested").exists()
        finally:
            migrator.execute(
                "ALTER TABLE public.audit_auditevent DROP CONSTRAINT task4_reject_scan_audit"
            )


def test_manual_start_is_immediate_and_captures_nonzero_state(role_database: RoleDatabase) -> None:
    from aegis_apps.indexing.services import request_root_scan

    root, _, _ = _create_scan_fixture()
    actor = User.objects.create_user(username=f"manual-state-{uuid.uuid4().hex}")
    RootGrant.objects.create(root=root, user=actor, permissions=128)
    Root.objects.filter(pk=root.pk).update(authorization_epoch=7)
    CatalogEntry.objects.filter(root=root).update(source_revision=19)
    with connection.cursor() as cursor:
        cursor.execute("SELECT clock_timestamp() + interval '1 hour'")
        due_at = cursor.fetchone()[0]
    RootIndexState.objects.create(
        root=root,
        binding_epoch=1,
        policy_epoch=1,
        reconciliation_epoch=11,
        next_generation=5,
        due_at=due_at,
    )
    with role_database.as_django_role("aegis_web"):
        run_id = request_root_scan(actor, root.pk, "manual_state")
    run = ScanRun.objects.get(pk=run_id)
    work = DirectoryWork.objects.get(run=run)
    state = RootIndexState.objects.get(root=root)
    assert (
        run.generation,
        run.start_epoch,
        run.root_epoch,
        run.binding_epoch,
        run.policy_epoch,
    ) == (5, 11, 7, 1, 1)
    assert (
        work.parent_revision,
        work.state,
        work.attempt,
        work.last_batch_sequence,
        work.observed_count,
        work.eof_identity,
        work.lease_owner,
        work.lease_expires_at,
    ) == (19, "pending", 0, 0, 0, None, None, None)
    assert state.due_at == run.started_at < due_at
    assert state.next_generation == 6


def test_counter_repair_recounts_the_latest_run_only(scan_fixture: ScanFixture) -> None:
    """The one-time repair replaces counters that earlier releases let accumulate."""
    import importlib

    from aegis_apps.indexing.scheduling import schedule_root_scan
    from django.db.models.functions import Now

    repair = importlib.import_module("aegis_apps.indexing.migrations.0004_current_run_counters")
    database, root = scan_fixture.database, scan_fixture.root
    lease = scan_fixture.claim()
    assert scan_fixture.observe(lease, b"first.txt") is not None
    assert scan_fixture.seal(lease)
    assert scan_fixture.finalize(lease).complete
    with database.as_django_role("aegis_indexer"):
        assert schedule_root_scan(root.pk, scan_fixture.worker, "a" * 64) is None
    RootIndexState.objects.filter(root=root).update(due_at=Now())
    with database.as_django_role("aegis_indexer"):
        second = schedule_root_scan(root.pk, scan_fixture.worker, "a" * 64)
    assert second is not None
    scan_fixture.run_id = second
    lease = scan_fixture.claim()
    for sequence, name in enumerate((b"a.txt", b"b.txt"), start=1):
        assert scan_fixture.observe(lease, name, sequence) is not None
    # Counters as a release without the run-start reset left them: summed over runs.
    RootIndexState.objects.filter(root=root).update(
        observed_entries=1_955_208,
        completed_directories=40,
        degraded_directories=3,
    )
    other, _, _ = _create_scan_fixture()
    RootIndexState.objects.create(
        root=other,
        binding_epoch=1,
        policy_epoch=1,
        due_at=timezone.now(),
        observed_entries=5,
        completed_directories=6,
        degraded_directories=7,
    )
    with database.connect("aegis_migrator") as migrator, migrator.transaction():
        for statement in repair.Migration.operations[0].sql:
            migrator.execute(statement)
    state = RootIndexState.objects.get(root=root)
    # The active second run has observed two entries and completed no directory yet.
    assert (state.observed_entries, state.completed_directories, state.degraded_directories) == (
        2,
        0,
        0,
    )
    untouched = RootIndexState.objects.get(root=other)
    assert (
        untouched.observed_entries,
        untouched.completed_directories,
        untouched.degraded_directories,
    ) == (5, 6, 7)


def _recount(root_id: uuid.UUID) -> tuple[int, int, int]:
    """The 0004 repair's definition, over the root's latest run."""
    run = ScanRun.objects.filter(root_id=root_id).order_by("-generation").first()
    assert run is not None
    work = DirectoryWork.objects.filter(run=run)
    return (
        sum(work.values_list("observed_count", flat=True)),
        work.filter(state="complete").count(),
        work.filter(state="degraded").count(),
    )


def _live(root_id: uuid.UUID) -> tuple[int, int, int]:
    state = RootIndexState.objects.get(root_id=root_id)
    return state.observed_entries, state.completed_directories, state.degraded_directories


@pytest.mark.parametrize("settled_by", ["claim", "schedule"])
def test_exhausted_attempts_count_as_degraded_directories(
    scan_fixture: ScanFixture,
    settled_by: str,
) -> None:
    from aegis_apps.indexing.database import claim_directory
    from aegis_apps.indexing.scheduling import schedule_root_scan

    for attempt in (1, 2, 3):
        lease = scan_fixture.claim()
        assert lease.attempt == attempt
        scan_fixture.expire(lease)
    with scan_fixture.database.as_django_role("aegis_indexer"):
        if settled_by == "claim":
            assert claim_directory(scan_fixture.run_id, scan_fixture.worker) is None
        assert schedule_root_scan(scan_fixture.root.pk, scan_fixture.worker, "a" * 64) is None
    work = DirectoryWork.objects.get(run_id=scan_fixture.run_id)
    assert (work.state, work.error_code) == ("degraded", "scan_attempts_exhausted")
    assert _live(scan_fixture.root.pk) == _recount(scan_fixture.root.pk) == (0, 0, 1)
    assert RootIndexState.objects.get(root=scan_fixture.root).status == "degraded"


def test_a_repended_directory_is_counted_once_by_its_final_state(
    scan_fixture: ScanFixture,
) -> None:
    from aegis_apps.catalog.domain import DirectoryIdentity, EntryKind, Observation, SourceState
    from aegis_apps.catalog.names import source_name
    from aegis_apps.indexing.checkpoints import seal_directory
    from aegis_apps.indexing.protocol import ReaderBatch, ReaderComplete

    def child(mtime_ns: int) -> ReaderBatch:
        return ReaderBatch(
            1,
            (
                Observation(
                    source_name(b"child"),
                    EntryKind.DIRECTORY,
                    SourceState.PRESENT,
                    4096,
                    mtime_ns,
                    100,
                    1,
                    1,
                ),
            ),
        )

    def hold(work_id: uuid.UUID, interval: str) -> None:
        with scan_fixture.database.connect("aegis_migrator") as migrator:
            migrator.execute(
                "UPDATE public.indexing_directorywork SET available_at=clock_timestamp()"
                f"+interval '{interval}', lease_expires_at=LEAST(lease_expires_at, "
                "clock_timestamp()-interval '30 seconds') WHERE id=%s",
                [work_id],
            )

    anchor = scan_fixture.claim()
    assert scan_fixture.record(anchor, child(100)).observed == 1
    hold(anchor.work_id, "1 hour")  # The anchor's first attempt stops before its EOF.
    first = scan_fixture.claim()
    assert scan_fixture.seal(first)  # DirectoryIdentity(1, 1, 100, 100) matches.
    assert scan_fixture.finalize(first).complete
    assert _live(scan_fixture.root.pk)[1] == 1

    # The anchor's retry sees the child changed: the completed child is pending again.
    hold(anchor.work_id, "-1 second")
    retry = scan_fixture.claim()
    assert retry.work_id == anchor.work_id and retry.attempt == 2
    assert scan_fixture.record(retry, child(200)).observed == 1
    assert DirectoryWork.objects.get(pk=first.work_id).state == "pending"
    assert _live(scan_fixture.root.pk)[1] == 0
    assert scan_fixture.seal(retry)
    assert scan_fixture.finalize(retry).complete
    second = scan_fixture.claim()
    assert second.work_id == first.work_id and second.attempt == 2
    with scan_fixture.database.as_django_role("aegis_indexer"):
        assert seal_directory(second, ReaderComplete(DirectoryIdentity(1, 1, 200, 100)))
    assert scan_fixture.finalize(second).complete
    # Two directories, each complete once (not three completions); one entry observed.
    assert _live(scan_fixture.root.pk) == _recount(scan_fixture.root.pk) == (1, 2, 0)


def test_a_directory_out_of_attempts_is_not_pended_again_and_the_run_settles(
    scan_fixture: ScanFixture,
) -> None:
    """Regression: re-pending an exhausted row left it unclaimable and the run unsettled."""
    from aegis_apps.catalog.domain import EntryKind, Observation, SourceState
    from aegis_apps.catalog.names import source_name
    from aegis_apps.indexing.database import claim_directory
    from aegis_apps.indexing.protocol import ReaderBatch
    from aegis_apps.indexing.scheduling import schedule_root_scan

    def child(mtime_ns: int) -> ReaderBatch:
        return ReaderBatch(
            1,
            (
                Observation(
                    source_name(b"child"),
                    EntryKind.DIRECTORY,
                    SourceState.PRESENT,
                    4096,
                    mtime_ns,
                    100,
                    1,
                    1,
                ),
            ),
        )

    def hold(work_id: uuid.UUID, interval: str) -> None:
        with scan_fixture.database.connect("aegis_migrator") as migrator:
            migrator.execute(
                "UPDATE public.indexing_directorywork SET available_at=clock_timestamp()"
                f"+interval '{interval}', lease_expires_at=LEAST(lease_expires_at, "
                "clock_timestamp()-interval '30 seconds') WHERE id=%s",
                [work_id],
            )

    anchor = scan_fixture.claim()
    assert scan_fixture.record(anchor, child(100)).observed == 1
    hold(anchor.work_id, "1 hour")
    for attempt in (1, 2, 3):
        lease = scan_fixture.claim()
        assert lease.work_id != anchor.work_id and lease.attempt == attempt
        scan_fixture.expire(lease)
    with scan_fixture.database.as_django_role("aegis_indexer"):
        assert claim_directory(scan_fixture.run_id, scan_fixture.worker) is None
    exhausted = DirectoryWork.objects.get(pk=lease.work_id)
    assert (exhausted.state, exhausted.attempt) == ("degraded", 3)

    # The parent's retry sees the child changed; the exhausted row stays degraded.
    hold(anchor.work_id, "-1 second")
    retry = scan_fixture.claim()
    assert retry.work_id == anchor.work_id
    assert scan_fixture.record(retry, child(200)).observed == 1
    assert DirectoryWork.objects.get(pk=lease.work_id).state == "degraded"
    assert scan_fixture.seal(retry)
    assert scan_fixture.finalize(retry).complete
    with scan_fixture.database.as_django_role("aegis_indexer"):
        assert schedule_root_scan(scan_fixture.root.pk, scan_fixture.worker, "a" * 64) is None
    assert ScanRun.objects.get(pk=scan_fixture.run_id).state == "degraded"
    assert _live(scan_fixture.root.pk) == _recount(scan_fixture.root.pk) == (1, 1, 1)
