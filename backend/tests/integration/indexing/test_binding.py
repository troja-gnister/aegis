from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.identity.models import User
from aegis_apps.indexing.binding import install_index_binding
from aegis_apps.indexing.config import ScanPolicy
from aegis_apps.indexing.models import DirectoryWork, IndexDeployment, RootIndexState, ScanRun
from aegis_apps.operations.services import create_operation
from aegis_apps.roots.manifest import MountManifest
from aegis_apps.roots.models import Root, RootGrant
from django.contrib.auth.models import Group
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone

from tests.support.database_roles import RoleDatabase

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def _manifest(path: Path, *slot_ids: str) -> MountManifest:
    slots = [
        {
            "slotId": slot_id,
            "containerPath": f"/srv/aegis/roots/{slot_id}",
            "mode": "read_only",
            "filesystemId": index + 100,
            "rootInode": index + 200,
            "expectedIdentity": f"remote:nas.invalid:/{slot_id}",
            "mountFingerprint": f"{index + 1:064x}",
        }
        for index, slot_id in enumerate(slot_ids)
    ]
    raw = (
        json.dumps(
            {"version": 1, "generatedAt": "2026-09-14T12:00:00Z", "slots": slots},
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    os.chown(path, os.geteuid(), os.getegid())
    return MountManifest.load(path, hashlib.sha256(raw).hexdigest())


def _root(slot_id: str) -> Root:
    root = Root.objects.create(
        slot_id=slot_id,
        display_name=slot_id.title(),
        mode=Root.Mode.READ_ONLY,
        active=True,
    )
    CatalogEntry.objects.create(
        root=root,
        raw_name=b"",
        display_name="",
        name_key=b"",
        kind="directory",
    )
    return root


def test_binding_is_idempotent_and_changes_fence_work_and_authorization(
    tmp_path: Path,
    role_database: RoleDatabase,
) -> None:
    root = _root("photos")
    direct = User.objects.create_user(username="binding-direct")
    member = User.objects.create_user(username="binding-member")
    unrelated = User.objects.create_user(username="binding-unrelated")
    group = Group.objects.create(name="binding-group")
    member.groups.add(group)
    RootGrant.objects.create(root=root, user=direct, permissions=1)
    direct_grant = RootGrant.objects.get(root=root, user=direct)
    group_grant = RootGrant.objects.create(root=root, group=group, permissions=1)
    operation = create_operation(
        actor=direct,
        request_id="binding_upgrade_job",
        kind="foundation.probe",
        intent={"roots": []},
    )
    job = operation.jobs.get()
    anchor = CatalogEntry.objects.get(root=root)
    manifest = _manifest(tmp_path / "manifest.json", "photos")
    policy = ScanPolicy.from_environment({})

    with role_database.as_django_role("aegis_migrator"):
        install_index_binding(manifest, policy)
        initial = IndexDeployment.objects.get(pk=1)
        assert not RootIndexState.objects.filter(root=root).exists()
        initial_root_epoch = Root.objects.get(pk=root.pk).authorization_epoch
        initial_direct_epoch = User.objects.get(pk=direct.pk).authorization_epoch
        initial_member_epoch = User.objects.get(pk=member.pk).authorization_epoch
        install_index_binding(manifest, policy)

    repeated = IndexDeployment.objects.get(pk=1)
    assert repeated.epoch == initial.epoch == 1
    assert repeated.slot_ids == ["photos"]
    assert Root.objects.get(pk=root.pk).authorization_epoch == initial_root_epoch
    assert User.objects.get(pk=direct.pk).authorization_epoch == initial_direct_epoch
    assert User.objects.get(pk=member.pk).authorization_epoch == initial_member_epoch

    run = ScanRun.objects.create(
        root=root,
        binding_epoch=initial.epoch,
        policy_epoch=initial.epoch,
        root_epoch=initial_root_epoch,
        manifest_identity=manifest.digest,
        generation=1,
        start_epoch=0,
        state=ScanRun.State.RUNNING,
        started_at=timezone.now(),
    )
    RootIndexState.objects.create(
        root=root,
        binding_epoch=initial.epoch,
        policy_epoch=initial.epoch,
        reconciliation_epoch=0,
        next_generation=2,
        due_at=timezone.now(),
        active_run=run,
        status="scanning",
    )
    changed = ScanPolicy(7200, 120, 500, 2)
    changed_manifest = _manifest(tmp_path / "changed.json", "archive", "photos")
    with patch("aegis_apps.indexing.binding.invalidate_authorization_cache") as invalidate:
        with role_database.as_django_role("aegis_migrator"):
            install_index_binding(changed_manifest, changed)
        invalidate.assert_called_once_with(
            user_ids=frozenset((direct.id, member.id)), root_ids=frozenset((root.id,))
        )

    deployment = IndexDeployment.objects.get(pk=1)
    run.refresh_from_db()
    state = RootIndexState.objects.get(pk=root.pk)
    root.refresh_from_db()
    direct.refresh_from_db()
    member.refresh_from_db()
    unrelated.refresh_from_db()
    assert deployment.epoch == 2
    assert deployment.manifest_identity == changed_manifest.digest
    assert deployment.slot_ids == ["archive", "photos"]
    assert deployment.interval_seconds == 7200
    assert (run.state, run.settled_at is not None) == (ScanRun.State.FENCED, True)
    assert state.active_run_id is None
    assert state.binding_epoch == state.policy_epoch == deployment.epoch
    assert root.authorization_epoch == initial_root_epoch + 1
    assert direct.authorization_epoch == initial_direct_epoch + 1
    assert member.authorization_epoch == initial_member_epoch + 1
    assert unrelated.authorization_epoch == 0
    assert Root.objects.filter(pk=root.pk, slot_id="photos", active=True).exists()
    assert User.objects.filter(pk=direct.pk, username="binding-direct").exists()
    assert RootGrant.objects.filter(
        pk=direct_grant.pk, root=root, user=direct, permissions=1
    ).exists()
    assert RootGrant.objects.filter(
        pk=group_grant.pk, root=root, group=group, permissions=1
    ).exists()
    assert operation.jobs.filter(pk=job.pk, state="queued").exists()
    assert CatalogEntry.objects.filter(pk=anchor.pk, root=root, raw_name=b"").exists()


def test_absent_manifest_disables_slots_without_erasing_catalog(
    tmp_path: Path,
    role_database: RoleDatabase,
) -> None:
    root = _root("archive")
    manifest = _manifest(tmp_path / "archive.json", "archive")
    policy = ScanPolicy.from_environment({})
    with role_database.as_django_role("aegis_migrator"):
        install_index_binding(manifest, policy)
        install_index_binding(None, policy)

    deployment = IndexDeployment.objects.get(pk=1)
    assert deployment.slot_ids == []
    assert CatalogEntry.objects.filter(root=root).count() == 1


@pytest.mark.parametrize("role", ("aegis_web", "aegis_indexer"))
def test_runtime_roles_can_read_but_cannot_replace_binding(
    role_database: RoleDatabase,
    role: str,
) -> None:
    with role_database.as_django_role("aegis_migrator"):
        install_index_binding(None, ScanPolicy.from_environment({}))
    with role_database.as_django_role(role), connection.cursor() as cursor:
        cursor.execute("SELECT epoch FROM indexing_indexdeployment WHERE id = 1")
        assert cursor.fetchone() == (1,)
        with pytest.raises(DatabaseError):
            cursor.execute("UPDATE indexing_indexdeployment SET epoch = epoch + 1 WHERE id = 1")


def test_binding_change_rolls_back_epochs_and_invalidation(
    tmp_path: Path,
    role_database: RoleDatabase,
) -> None:
    root = _root("rollback")
    user = User.objects.create_user(username="binding-rollback")
    RootGrant.objects.create(root=root, user=user, permissions=1)
    manifest = _manifest(tmp_path / "rollback.json", "rollback")
    with role_database.as_django_role("aegis_migrator"):
        install_index_binding(manifest, ScanPolicy.from_environment({}))
    with patch("aegis_apps.indexing.binding.invalidate_authorization_cache") as invalidate:
        with (
            pytest.raises(RuntimeError, match="rollback"),
            role_database.as_django_role("aegis_migrator"),
            transaction.atomic(),
        ):
            install_index_binding(manifest, ScanPolicy(7200, 120, 500, 2))
            invalidate.assert_not_called()
            raise RuntimeError("rollback")
        invalidate.assert_not_called()
    assert IndexDeployment.objects.get(pk=1).epoch == 1
    assert Root.objects.get(pk=root.pk).authorization_epoch == 0
    assert User.objects.get(pk=user.pk).authorization_epoch == 0


@pytest.mark.parametrize(
    ("attempt", "sequence", "allowed"),
    ((1, 10, False), (2, 9, False), (2, 10, True), (2, 11, True), (3, 10, True), (3, 0, True)),
)
def test_directory_counters_fence_same_attempt_but_allow_retry_reset(
    role_database: RoleDatabase, attempt: int, sequence: int, allowed: bool
) -> None:
    root = _root("counter-root")
    with role_database.as_django_role("aegis_migrator"):
        run = ScanRun.objects.create(
            root=root,
            binding_epoch=1,
            policy_epoch=1,
            root_epoch=0,
            manifest_identity="unconfigured:v1",
            generation=1,
            start_epoch=0,
            state="queued",
        )
        work = DirectoryWork.objects.create(
            run=run,
            directory=CatalogEntry.objects.get(root=root),
            parent_revision=0,
            attempt=2,
            last_batch_sequence=10,
        )
        if allowed:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE indexing_directorywork SET attempt = %s, "
                    "last_batch_sequence = %s WHERE id = %s",
                    [attempt, sequence, work.pk],
                )
            work.refresh_from_db()
            assert (work.attempt, work.last_batch_sequence) == (attempt, sequence)
        else:
            with (
                pytest.raises(DatabaseError) as caught,
                transaction.atomic(),
                connection.cursor() as cursor,
            ):
                cursor.execute(
                    "UPDATE indexing_directorywork SET attempt = %s, "
                    "last_batch_sequence = %s WHERE id = %s",
                    [attempt, sequence, work.pk],
                )
            assert getattr(caught.value.__cause__, "sqlstate", None) == "55000"
            work.refresh_from_db()
            assert (work.attempt, work.last_batch_sequence) == (2, 10)


def test_active_run_must_belong_to_state_root(role_database: RoleDatabase) -> None:
    first = _root("state-first")
    second = _root("state-second")
    with role_database.as_django_role("aegis_migrator"):
        install_index_binding(None, ScanPolicy.from_environment({}))
    run = ScanRun.objects.create(
        root=second,
        binding_epoch=1,
        policy_epoch=1,
        root_epoch=0,
        manifest_identity="unconfigured:v1",
        generation=1,
        start_epoch=0,
        state=ScanRun.State.RUNNING,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        RootIndexState.objects.create(
            root=first,
            binding_epoch=1,
            policy_epoch=1,
            due_at=timezone.now(),
            active_run=run,
        )


def test_runtime_indexing_table_access_is_exact(role_database: RoleDatabase) -> None:
    shared_tables = (
        "indexing_directorywork",
        "indexing_indexdeployment",
        "indexing_rootindexstate",
        "indexing_scanrun",
    )
    for role in ("aegis_web", "aegis_indexer", "aegis_operations", "aegis_media"):
        readable = (
            (*shared_tables, "indexing_scanrequest")
            if role == "aegis_web"
            else shared_tables
            if role == "aegis_indexer"
            else ()
        )
        for table in (*shared_tables, "indexing_scanrequest"):
            with role_database.connect(role) as role_connection, role_connection.cursor() as cursor:
                if table in readable:
                    cursor.execute(f"SELECT count(*) FROM public.{table}")
                    assert cursor.fetchone() is not None
                else:
                    with pytest.raises(psycopg.Error):
                        cursor.execute(f"SELECT count(*) FROM public.{table}")
            with (
                role_database.connect(role) as role_connection,
                role_connection.cursor() as cursor,
                pytest.raises(psycopg.Error),
            ):
                cursor.execute(f"DELETE FROM public.{table} WHERE false")
