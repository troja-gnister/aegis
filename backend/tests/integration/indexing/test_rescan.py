from __future__ import annotations

import uuid

import pytest
from aegis_apps.audit.models import AuditEvent
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.identity.models import User
from aegis_apps.indexing.models import DirectoryWork, RootIndexState, ScanRequest, ScanRun
from aegis_apps.roots.models import Root, RootGrant
from django.contrib.auth.models import Group
from django.db import connection

from tests.deployment.test_database_roles import _create_scan_fixture
from tests.support.database_roles import RoleDatabase

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
