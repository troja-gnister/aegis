from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from threading import Event
from typing import TYPE_CHECKING

import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.indexing.runner import UnsupportedTraversal, source_components
from aegis_apps.roots.models import Root
from django.db import connection
from django.test import override_settings

from tests.support.database_roles import RoleDatabase

if TYPE_CHECKING:
    from conftest import ScanFixture

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def test_anchor_resolves_without_browser_or_logical_path(scan_fixture: ScanFixture) -> None:
    lease = scan_fixture.claim()
    with scan_fixture.database.as_django_role("aegis_indexer"):
        assert source_components(lease) == ()


def test_source_chain_uses_captured_marker_and_ignores_logical_ancestry(
    scan_fixture: ScanFixture, entry_factory: Callable[..., CatalogEntry],
) -> None:
    lease = scan_fixture.claim()
    root = scan_fixture.root
    parent = entry_factory(root=root, raw=b"physical", kind="directory", source_revision=7)
    logical = entry_factory(root=root, raw=b"logical", kind="directory")
    child = entry_factory(root=root, raw=b"raw\xff", parent=parent, kind="directory",
                          logical_parent=logical, logical_name="browser path", source_revision=3)
    CatalogEntry.objects.filter(pk=parent.pk).update(source_parent_revision=0)
    CatalogEntry.objects.filter(pk=child.pk).update(source_parent_revision=7)
    with scan_fixture.database.as_django_role("aegis_indexer"):
        assert source_components(replace(lease, directory_id=child.pk, parent_revision=3)) == (
            b"physical", b"raw\xff",
        )


@pytest.mark.parametrize("invalid", ["unknown", "replaced", "missing", "not_directory", "cycle"])
def test_unavailable_source_ancestry_cannot_authorize_traversal(
    scan_fixture: ScanFixture, entry_factory: Callable[..., CatalogEntry], invalid: str,
) -> None:
    lease = scan_fixture.claim()
    parent = entry_factory(root=scan_fixture.root, raw=b"parent", kind="directory")
    child = entry_factory(root=scan_fixture.root, raw=b"child", parent=parent, kind="directory")
    CatalogEntry.objects.filter(pk=parent.pk).update(source_parent_revision=0)
    CatalogEntry.objects.filter(pk=child.pk).update(source_parent_revision=0)
    if invalid == "unknown":
        CatalogEntry.objects.filter(pk=child.pk).update(source_parent_revision=None)
    elif invalid == "replaced":
        CatalogEntry.objects.filter(pk=parent.pk).update(source_revision=1)
    elif invalid == "missing":
        CatalogEntry.objects.filter(pk=parent.pk).update(source_state="missing")
    elif invalid == "not_directory":
        CatalogEntry.objects.filter(pk=parent.pk).update(kind="file")
    else:
        CatalogEntry.objects.filter(pk=parent.pk).update(source_parent=child)
    with scan_fixture.database.as_django_role("aegis_indexer"), pytest.raises(UnsupportedTraversal):
        source_components(replace(lease, directory_id=child.pk))


def test_foreign_root_directory_cannot_authorize_traversal(
    scan_fixture: ScanFixture, catalog_root: Root,
) -> None:
    lease = scan_fixture.claim()
    foreign = CatalogEntry.objects.get(root=catalog_root, source_parent=None)
    with scan_fixture.database.as_django_role("aegis_indexer"), pytest.raises(UnsupportedTraversal):
        source_components(replace(lease, directory_id=foreign.pk))


def test_component_bytes_are_bounded_without_truncation(
    scan_fixture: ScanFixture, entry_factory: Callable[..., CatalogEntry],
) -> None:
    lease = scan_fixture.claim()
    parent = CatalogEntry.objects.get(pk=lease.directory_id)
    for _ in range(17):
        parent = entry_factory(root=scan_fixture.root, raw=b"x" * 255, parent=parent,
                               kind="directory")
        CatalogEntry.objects.filter(pk=parent.pk).update(source_parent_revision=0)
    with scan_fixture.database.as_django_role("aegis_indexer"), pytest.raises(UnsupportedTraversal):
        source_components(replace(lease, directory_id=parent.pk))


def test_database_lane_is_bounded_and_has_actual_indexer_login(role_database: RoleDatabase) -> None:
    from aegis_apps.indexing.runner import DatabaseLane

    started, release = Event(), Event()

    def blocked() -> str:
        with connection.cursor() as cursor:
            cursor.execute("SELECT session_user")
            login = cursor.fetchone()[0]
        started.set()
        assert release.wait(5)
        return str(login)

    with role_database.as_django_role("aegis_indexer"):
        lane = DatabaseLane(1)
    try:
        future = lane.submit(blocked)
        assert started.wait(5)
        with pytest.raises(RuntimeError, match="capacity"):
            lane.submit(lambda: None)
        release.set()
        assert future.result(timeout=5) == "aegis_indexer"
    finally:
        release.set()
        lane.close()


@override_settings(AEGIS_PROCESS_ROLE="indexer")
def test_blocked_checkpoint_does_not_block_independent_heartbeat_lane(
    scan_fixture: ScanFixture,
) -> None:
    from aegis_apps.indexing.checkpoints import record_batch
    from aegis_apps.indexing.runner import DatabaseLane
    from aegis_apps.operations.heartbeats import publish_heartbeat

    lease = scan_fixture.claim()
    with scan_fixture.database.as_django_role("aegis_indexer"):
        mutations, heartbeat = DatabaseLane(1), DatabaseLane(1)
    try:
        with scan_fixture.database.connect("aegis_migrator") as holder, holder.transaction():
            holder.execute("SELECT id FROM public.indexing_directorywork WHERE id=%s FOR UPDATE",
                           [lease.work_id])
            pending = mutations.submit(lambda: record_batch(lease, scan_fixture.batch(b"blocked")))
            started = time.monotonic()
            published = heartbeat.submit(lambda: publish_heartbeat(
                role="indexer", worker_id=lease.worker_id, release_id="task4-test",
                schema_identity="schema-test", manifest_identity=lease.manifest_identity,
                current_job_id=None, metrics={"scanObservedEntries": 0},
            ))
            assert published.result(timeout=3).current_job_id is None
            assert time.monotonic() - started < 3
            with pytest.raises(RuntimeError, match="checkpoint rejected"):
                pending.result(timeout=5)
        assert not CatalogEntry.objects.filter(root_id=lease.root_id, raw_name=b"blocked").exists()
    finally:
        mutations.close()
        heartbeat.close()


@override_settings(AEGIS_PROCESS_ROLE="indexer")
def test_runtime_checkpoint_future_is_committed_and_cancellation_closes_gate(
    scan_fixture: ScanFixture,
) -> None:
    from aegis_apps.indexing.config import ScanPolicy
    from aegis_apps.indexing.processes import ReaderSource
    from aegis_apps.indexing.runner import ScanRuntime
    from aegis_apps.indexing.supervisor import ReaderHandle
    from aegis_apps.operations.management.commands.run_role import WorkerIdentity

    lease = scan_fixture.claim()
    identity = WorkerIdentity("indexer", lease.worker_id, "task4-test", "schema-test",
                              lease.manifest_identity)

    def forbidden_spawn(source: ReaderSource, root: object) -> ReaderHandle:
        raise AssertionError("checkpoint test cannot open sources")

    with scan_fixture.database.as_django_role("aegis_indexer"):
        runtime = ScanRuntime(identity, ScanPolicy(3600, 120, 500, 2), forbidden_spawn)
    try:
        recorded = runtime.execute(lease, "record", scan_fixture.batch(b"committed"))
        assert recorded.result(timeout=5).observed == 1
        assert CatalogEntry.objects.filter(root_id=lease.root_id, raw_name=b"committed").exists()
        runtime.cancel(lease)
        late = runtime.execute(lease, "record", scan_fixture.batch(b"late", sequence=2))
        with pytest.raises(RuntimeError, match="cancelled"):
            late.result(timeout=5)
        assert not CatalogEntry.objects.filter(root_id=lease.root_id, raw_name=b"late").exists()
    finally:
        runtime.close()


@override_settings(AEGIS_PROCESS_ROLE="indexer")
def test_unconfigured_manifest_keeps_foundation_indexer_idle(
    role_database: RoleDatabase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aegis_apps.indexing.config import ScanPolicy
    from aegis_apps.indexing.processes import ReaderSource
    from aegis_apps.indexing.runner import ScanRuntime
    from aegis_apps.indexing.supervisor import ReaderHandle
    from aegis_apps.operations.management.commands.run_role import WorkerIdentity
    from aegis_apps.roots import manifest

    monkeypatch.setattr(manifest, "configured_manifest", lambda: None)

    def forbidden_spawn(source: ReaderSource, root: object) -> ReaderHandle:
        raise AssertionError("unconfigured worker cannot open sources")

    identity = WorkerIdentity("indexer", "10000000-0000-4000-8000-000000000001",
                              "task7-test", "schema-test", "unconfigured:v1")
    with role_database.as_django_role("aegis_indexer"):
        runtime = ScanRuntime(identity, ScanPolicy(3600, 120, 500, 2), forbidden_spawn)
    try:
        admitted = runtime.admissions.submit(lambda: runtime.claim_next(set()))
        assert admitted.result(timeout=5) is None
    finally:
        runtime.close()


def test_source_preparation_uses_only_granted_root_columns(
    scan_fixture: ScanFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import PurePosixPath
    from unittest.mock import Mock

    from aegis_apps.indexing.config import ScanPolicy
    from aegis_apps.indexing.processes import ReaderSource
    from aegis_apps.indexing.runner import ScanRuntime
    from aegis_apps.indexing.supervisor import ReaderHandle
    from aegis_apps.operations.management.commands.run_role import WorkerIdentity
    from aegis_apps.roots import manifest

    lease = scan_fixture.claim()
    slot = manifest.ManifestSlot(
        scan_fixture.root.slot_id, PurePosixPath(f"/srv/aegis/roots/{scan_fixture.root.slot_id}"),
        "read_only", "local:1:1", 1, 1, "b" * 64,
    )
    current = manifest.MountManifest(lease.manifest_identity, {slot.slot_id: slot})
    monkeypatch.setattr(manifest, "configured_manifest", lambda: current)
    identity = WorkerIdentity(
        "indexer", lease.worker_id, "task4-test", "schema-test", current.digest,
    )
    captured: list[tuple[ReaderSource, object]] = []

    def capture(source: ReaderSource, root: object) -> ReaderHandle:
        captured.append((source, root))
        return Mock(spec=ReaderHandle)

    with scan_fixture.database.as_django_role("aegis_indexer"):
        runtime = ScanRuntime(identity, ScanPolicy(3600, 120, 500, 2), capture)
    try:
        runtime.spawn(lease).result(timeout=5)
        assert captured[0][0].path == str(slot.container_path)
        assert captured[0][0].components == ()
        assert captured[0][1] == lease.root_id
    finally:
        runtime.close()


@override_settings(AEGIS_PROCESS_ROLE="indexer")
@pytest.mark.parametrize("operation", ["record", "finalize"])
def test_cancel_idle_registered_connection_prevents_checkpoint_transaction(
    scan_fixture: ScanFixture, entry_factory: Callable[..., CatalogEntry],
    monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    from unittest.mock import Mock

    from aegis_apps.indexing import checkpoints
    from aegis_apps.indexing.config import ScanPolicy
    from aegis_apps.indexing.runner import ScanRuntime
    from aegis_apps.operations.management.commands.run_role import WorkerIdentity
    from psycopg.pq import TransactionStatus

    lease = scan_fixture.claim()
    unseen = entry_factory(root=scan_fixture.root, raw=b"unseen")
    if operation == "finalize":
        assert scan_fixture.seal(lease)
    name = "record_batch" if operation == "record" else "finalize_directory"
    original = getattr(checkpoints, name)
    entered, release = Event(), Event()

    def barrier(*args: object) -> object:
        assert connection.connection.info.transaction_status == TransactionStatus.IDLE
        entered.set()
        assert release.wait(5), "test barrier not released"
        return original(*args)

    monkeypatch.setattr(checkpoints, name, barrier)
    identity = WorkerIdentity("indexer", lease.worker_id, "task4-test", "schema-test",
                              lease.manifest_identity)
    with scan_fixture.database.as_django_role("aegis_indexer"):
        runtime = ScanRuntime(identity, ScanPolicy(3600, 120, 500, 2), Mock())
    try:
        payload = scan_fixture.batch(b"late") if operation == "record" else 500
        pending = runtime.execute(lease, operation, payload)
        assert entered.wait(5)
        runtime.cancel(lease)
        # Prove an actual SQL cancel delivered while idle is insufficient by itself.
        with runtime._mutex:
            active = runtime._connections[lease.work_id]
        active.cancel_safe(timeout=1)
        release.set()
        with pytest.raises(RuntimeError, match="cancelled"):
            pending.result(timeout=5)
        assert not CatalogEntry.objects.filter(root=scan_fixture.root, raw_name=b"late").exists()
        unseen.refresh_from_db()
        assert unseen.source_state == "present"
    finally:
        release.set()
        runtime.close()


@override_settings(AEGIS_PROCESS_ROLE="indexer")
def test_unreaped_reader_persists_degraded_root_and_excludes_replacement(
    scan_fixture: ScanFixture,
) -> None:
    from concurrent.futures import Future
    from unittest.mock import Mock

    from aegis_apps.indexing.config import ScanPolicy
    from aegis_apps.indexing.models import DirectoryWork, RootIndexState
    from aegis_apps.indexing.runner import ScanRuntime
    from aegis_apps.indexing.supervisor import ReaderHandle, ReaderState, ScanSupervisor
    from aegis_apps.operations.management.commands.run_role import WorkerIdentity

    lease = scan_fixture.claim()
    reader = Mock(spec=ReaderHandle)
    reader.receive.return_value = None
    reader.reaped.return_value = False
    launched: Future[ReaderHandle] = Future()
    launched.set_result(reader)
    published: Future[None] = Future()
    published.set_result(None)
    identity = WorkerIdentity("indexer", lease.worker_id, "task4-test", "schema-test",
                              lease.manifest_identity)
    with scan_fixture.database.as_django_role("aegis_indexer"):
        runtime = ScanRuntime(identity, ScanPolicy(3600, 120, 500, 2), Mock())
    now = [0.0]
    supervisor = ScanSupervisor(
        spawn=lambda candidate: launched, execute=runtime.execute, renew=runtime.renew,
        heartbeat=lambda metrics: published, cancel=runtime.cancel, monotonic=lambda: now[0],
    )
    try:
        supervisor.start(lease)
        supervisor.tick()
        supervisor.stop()
        now[0] = 6
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            supervisor.tick()
            if DirectoryWork.objects.get(pk=lease.work_id).state == "degraded":
                break
            time.sleep(.01)
        state = RootIndexState.objects.get(root=scan_fixture.root)
        assert state.status == "degraded"
        assert state.active_run_id == lease.run_id
        assert supervisor.states[lease.root_id] == ReaderState.UNREAPED
        assert supervisor.has_unreaped_reader(lease.root_id)
        with pytest.raises(RuntimeError, match="slot unavailable"):
            supervisor.start(replace(lease, attempt=2))
    finally:
        reader.reaped.return_value = True
        runtime.close()


@override_settings(AEGIS_PROCESS_ROLE="indexer")
@pytest.mark.parametrize("operation", ["record", "finalize"])
@pytest.mark.parametrize("boundary", ["after_setup", "after_mutation", "commit_admitted"])
def test_cancel_and_final_commit_admission_have_explicit_order(
    scan_fixture: ScanFixture, entry_factory: Callable[..., CatalogEntry],
    monkeypatch: pytest.MonkeyPatch, operation: str, boundary: str,
) -> None:
    from unittest.mock import Mock, patch

    from aegis_apps.indexing import checkpoints
    from aegis_apps.indexing.config import ScanPolicy
    from aegis_apps.indexing.runner import ScanRuntime
    from aegis_apps.operations.management.commands.run_role import WorkerIdentity
    from django.db import connections

    lease = scan_fixture.claim()
    unseen = entry_factory(root=scan_fixture.root, raw=b"unseen")
    if operation == "finalize":
        assert scan_fixture.seal(lease)
    name = "record_batch" if operation == "record" else "finalize_directory"
    original = getattr(checkpoints, name)
    entered, release = Event(), Event()

    def wait_at_boundary() -> None:
        entered.set()
        assert release.wait(5), "test barrier not released"

    def wrapped(*args: object) -> object:
        actual_commit = connection.commit

        def commit() -> None:
            wait_at_boundary()
            actual_commit()

        def statement(execute: Callable[..., object], sql: str, params: object,
                      many: bool, context: object) -> object:
            result = execute(sql, params, many, context)
            if ((boundary == "after_setup" and sql.startswith("SET CONSTRAINTS"))
                    or (boundary == "after_mutation" and sql.startswith("SELECT public."))):
                wait_at_boundary()
            return result

        if boundary == "commit_admitted":
            with patch.object(connections["default"], "commit", side_effect=commit):
                return original(*args)
        with connection.execute_wrapper(statement):
            return original(*args)

    monkeypatch.setattr(checkpoints, name, wrapped)
    identity = WorkerIdentity("indexer", lease.worker_id, "task4-test", "schema-test",
                              lease.manifest_identity)
    with scan_fixture.database.as_django_role("aegis_indexer"):
        runtime = ScanRuntime(identity, ScanPolicy(3600, 120, 500, 2), Mock())

    def control_probe() -> int:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            return int(cursor.fetchone()[0])

    try:
        pending = runtime.execute(
            lease, operation, scan_fixture.batch(b"late") if operation == "record" else 500,
        )
        assert entered.wait(5)
        started = time.monotonic()
        runtime.cancel(lease)
        assert time.monotonic() - started < .2, "cancel blocked on transaction I/O"
        assert runtime.controls.submit(control_probe).result(timeout=2) == 1
        assert not pending.done(), "stop must wait for the in-flight outcome"
        release.set()
        if boundary == "commit_admitted":
            pending.result(timeout=5)
        else:
            with pytest.raises(RuntimeError, match="cancelled"):
                pending.result(timeout=5)
        published = boundary == "commit_admitted"
        assert CatalogEntry.objects.filter(root=scan_fixture.root, raw_name=b"late").exists() == (
            published and operation == "record"
        )
        unseen.refresh_from_db()
        assert unseen.source_state == ("missing" if published and operation == "finalize"
                                       else "present")
        with pytest.raises(RuntimeError, match="cancelled"):
            runtime.execute(lease, operation, 500).result(timeout=5)
    finally:
        release.set()
        runtime.close()
