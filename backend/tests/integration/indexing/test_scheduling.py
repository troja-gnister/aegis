from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import psycopg
import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.identity.models import User
from aegis_apps.indexing.models import DirectoryWork, IndexDeployment, RootIndexState, ScanRun
from aegis_apps.roots.models import Root, RootGrant
from django.db import connection

from tests.deployment.test_database_roles import _create_scan_fixture, _scan_scalar
from tests.support.database_roles import RoleDatabase

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def test_schedule_coalesces_and_enqueues_anchor_only(role_database: RoleDatabase) -> None:
    from aegis_apps.indexing.scheduling import schedule_root_scan

    root, worker, digest = _create_scan_fixture()
    with role_database.as_django_role("aegis_indexer"):
        run_id = schedule_root_scan(root.pk, worker, digest)
        assert run_id is not None
        assert schedule_root_scan(root.pk, worker, digest) == run_id
    run = ScanRun.objects.get(pk=run_id)
    assert (run.generation, run.start_epoch, run.binding_epoch, run.policy_epoch) == (1, 0, 1, 1)
    work = DirectoryWork.objects.get(run=run)
    assert work.directory.source_parent_id is None
    assert (work.state, work.attempt) == ("pending", 0)
    assert CatalogEntry.objects.filter(root=root, source_parent=None).count() == 1


@pytest.mark.parametrize("invalid", ["digest", "worker", "expired", "future", "inactive", "slot"])
def test_schedule_requires_live_binding_and_heartbeat(
    role_database: RoleDatabase, invalid: str
) -> None:
    from aegis_apps.indexing.scheduling import schedule_root_scan

    root, worker, digest = _create_scan_fixture()
    if invalid == "digest":
        digest = "b" * 64
    elif invalid == "worker":
        worker = str(uuid.uuid4())
    elif invalid in ("expired", "future"):
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE operations_workerheartbeat SET last_seen_at = clock_timestamp() "
                "+ make_interval(secs => %s) WHERE worker_id = %s",
                [-121 if invalid == "expired" else 60, worker],
            )
    elif invalid == "inactive":
        Root.objects.filter(pk=root.pk).update(active=False)
    else:
        IndexDeployment.objects.filter(pk=1).update(slot_ids=[])
    with role_database.as_django_role("aegis_indexer"):
        assert schedule_root_scan(root.pk, worker, digest) is None
    assert not ScanRun.objects.exists()


def test_concurrent_scheduling_has_one_generation(role_database: RoleDatabase) -> None:
    root, worker, digest = _create_scan_fixture()
    barrier = Barrier(2)

    def schedule() -> object:
        with role_database.connect("aegis_indexer") as caller:
            barrier.wait(timeout=5)
            return _scan_scalar(
                caller.execute(
                    "SELECT public.aegis_schedule_root_scan(%s,%s,%s)", (root.pk, worker, digest)
                )
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: schedule(), range(2)))
    assert any(result is not None for result in results)
    assert ScanRun.objects.filter(root=root).count() == 1
    assert DirectoryWork.objects.count() == 1


def test_exhausted_run_settles_and_next_interval_starts_at_settlement(
    role_database: RoleDatabase,
) -> None:
    from aegis_apps.indexing.scheduling import schedule_root_scan

    root, worker, digest = _create_scan_fixture()
    with role_database.as_django_role("aegis_indexer"):
        run_id = schedule_root_scan(root.pk, worker, digest)
    DirectoryWork.objects.filter(run_id=run_id).update(state="degraded", attempt=3)
    assert run_id is not None
    with role_database.as_django_role("aegis_indexer"):
        assert schedule_root_scan(root.pk, worker, digest) is None
    run = ScanRun.objects.get(pk=run_id)
    state = RootIndexState.objects.get(root=root)
    assert run.state == "degraded"
    assert state.active_run_id is None
    assert run.settled_at is not None
    assert (state.due_at - run.settled_at).total_seconds() == 3600
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE indexing_rootindexstate SET due_at = clock_timestamp() WHERE root_id=%s",
            [root.pk],
        )
    with role_database.as_django_role("aegis_indexer"):
        next_id = schedule_root_scan(root.pk, worker, digest)
    assert next_id is not None
    assert ScanRun.objects.get(pk=next_id).generation == 2


def test_new_anchor_and_busy_root_isolation(role_database: RoleDatabase) -> None:
    from aegis_apps.indexing.scheduling import schedule_root_scan

    busy, worker, digest = _create_scan_fixture()
    fresh = Root.objects.create(
        slot_id=f"fresh-{uuid.uuid4().hex}",
        display_name="Fresh synthetic",
        active=True,
        mode=Root.Mode.READ_ONLY,
    )
    IndexDeployment.objects.filter(pk=1).update(slot_ids=sorted([busy.slot_id, fresh.slot_id]))
    with role_database.connect("aegis_web") as locker, locker.transaction():
        locker.execute("SELECT id FROM public.roots_root WHERE id=%s FOR UPDATE", [busy.pk])
        with role_database.as_django_role("aegis_indexer"):
            assert schedule_root_scan(busy.pk, worker, digest) is None
            run_id = schedule_root_scan(fresh.pk, worker, digest)
    assert run_id is not None
    anchor = CatalogEntry.objects.get(root=fresh)
    assert (anchor.raw_name, anchor.source_parent_id, anchor.kind) == (b"", None, "directory")
    assert DirectoryWork.objects.get(run_id=run_id).directory_id == anchor.pk


def test_root_epoch_change_fences_active_run_and_allocates_next_generation(
    role_database: RoleDatabase,
) -> None:
    from aegis_apps.indexing.scheduling import schedule_root_scan

    root, worker, digest = _create_scan_fixture()
    with role_database.as_django_role("aegis_indexer"):
        old = schedule_root_scan(root.pk, worker, digest)
    assert old is not None
    Root.objects.filter(pk=root.pk).update(authorization_epoch=1)
    with role_database.as_django_role("aegis_indexer"):
        new = schedule_root_scan(root.pk, worker, digest)
    assert new is not None and new != old
    assert ScanRun.objects.get(pk=old).state == "fenced"
    run = ScanRun.objects.get(pk=new)
    assert (run.generation, run.root_epoch) == (2, 1)


@pytest.mark.parametrize("path", ["periodic", "manual"])
def test_both_installed_paths_execute_the_shared_initialization_fragment(
    role_database: RoleDatabase,
    tmp_path: Path,
    path: str,
) -> None:
    root, worker, digest = _create_scan_fixture()
    actor = User.objects.create_user(username=f"fragment-{uuid.uuid4().hex}")
    RootGrant.objects.create(root=root, user=actor, permissions=128)
    resource_dir = tmp_path / "sql"
    resource_dir.mkdir()
    packaged = files("aegis_apps.indexing").joinpath("sql")
    for name in ("schedule.sql", "lease.sql", "start_run.sql"):
        (resource_dir / name).write_text(packaged.joinpath(name).read_text())
    # Inject a fault into the trusted installation resource, not into any runtime input.
    fragment = resource_dir / "start_run.sql"
    fragment.write_text(
        fragment.read_text() + "\nRAISE EXCEPTION 'synthetic fragment failure' "
        "USING ERRCODE = '55000';\n"
    )
    try:
        with patch("aegis_apps.common.database_privileges.files", return_value=tmp_path):
            role_database.synchronize()
        role = "aegis_indexer" if path == "periodic" else "aegis_web"
        with (
            role_database.connect(role) as caller,
            pytest.raises(
                psycopg.errors.ObjectNotInPrerequisiteState,
            ),
        ):
            if path == "periodic":
                caller.execute(
                    "SELECT public.aegis_schedule_root_scan(%s,%s,%s)", [root.pk, worker, digest]
                )
            else:
                caller.execute(
                    "SELECT public.aegis_request_root_scan(%s,%s,%s,%s)",
                    [root.pk, actor.pk, actor.authorization_epoch, "fragment_request"],
                )
        assert not ScanRun.objects.filter(root=root).exists()
        assert not DirectoryWork.objects.exists()
    finally:
        role_database.synchronize()


def test_periodic_start_preserves_due_time_and_captures_nonzero_state(
    role_database: RoleDatabase,
) -> None:
    from aegis_apps.indexing.scheduling import schedule_root_scan

    root, worker, digest = _create_scan_fixture()
    Root.objects.filter(pk=root.pk).update(authorization_epoch=7)
    anchor = CatalogEntry.objects.get(root=root)
    CatalogEntry.objects.filter(pk=anchor.pk).update(source_revision=19)
    with connection.cursor() as cursor:
        cursor.execute("SELECT clock_timestamp() - interval '1 hour'")
        due_at = cursor.fetchone()[0]
    RootIndexState.objects.create(
        root=root,
        binding_epoch=1,
        policy_epoch=1,
        reconciliation_epoch=11,
        next_generation=5,
        due_at=due_at,
    )
    with role_database.as_django_role("aegis_indexer"):
        run_id = schedule_root_scan(root.pk, worker, digest)
    assert run_id is not None
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
    assert state.due_at == due_at
    assert state.next_generation == 6
