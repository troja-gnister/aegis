from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from aegis_apps.catalog.domain import EntryKind
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.indexing.checkpoints import BatchResult, FinalizeResult
from aegis_apps.indexing.database import ScanLease
from aegis_apps.indexing.protocol import ReaderBatch
from django.db import connection, transaction

from tests.support.database_roles import RoleDatabase

if TYPE_CHECKING:
    from conftest import ScanFixture

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def test_retry_cannot_accept_the_old_attempt(scan_fixture: ScanFixture) -> None:
    old = scan_fixture.claim()
    assert scan_fixture.observe(old, b"committed.txt") is not None
    scan_fixture.expire(old)
    current = scan_fixture.claim()
    assert current.attempt > old.attempt
    assert scan_fixture.observe(old, b"old.txt", sequence=2) is None
    assert scan_fixture.observe(current, b"current.txt") is not None
    assert CatalogEntry.objects.filter(raw_name=b"committed.txt").exists()


def test_parent_replacement_fences_observation_and_seal(scan_fixture: ScanFixture) -> None:
    lease = scan_fixture.claim()
    CatalogEntry.objects.filter(pk=lease.directory_id).update(source_revision=1)
    assert scan_fixture.observe(lease, b"stale.txt") is None
    assert scan_fixture.seal(lease) is False
    assert scan_fixture.finalize(lease).complete is False


def test_commit_after_expiry_rolls_back_every_observation(scan_fixture: ScanFixture) -> None:
    from aegis_apps.indexing.checkpoints import _lease_payload
    from aegis_apps.indexing.payloads import checkpoint_payload
    from psycopg.types.json import Jsonb

    lease = scan_fixture.claim()
    with scan_fixture.database.connect("aegis_migrator") as observer:
        observer.execute(
            "UPDATE public.indexing_directorywork SET lease_expires_at="
            "clock_timestamp()+interval '2 seconds' WHERE id=%s", [lease.work_id],
        )
        with (scan_fixture.database.connect("aegis_indexer") as caller,
              pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState), caller.transaction()):
            row = caller.execute("SELECT public.aegis_record_scan_batch(%s,%s)", [
                Jsonb(_lease_payload(lease)),
                Jsonb(checkpoint_payload(scan_fixture.batch(b"uncommitted"))),
            ]).fetchone()
            assert row is not None
            result = row[0]
            assert result["inserted"] == 1
            assert observer.execute(
                "SELECT count(*) FROM public.catalog_catalogentry WHERE raw_name=%s",
                [b"uncommitted"],
            ).fetchone() == (0,)
            caller.execute("SELECT pg_catalog.pg_sleep(2.1)")
        assert observer.execute(
            "SELECT count(*) FROM public.catalog_catalogentry WHERE raw_name=%s",
            [b"uncommitted"],
        ).fetchone() == (0,)


def test_recent_request_lookup_has_actor_root_time_index(scan_fixture: ScanFixture) -> None:
    from aegis_apps.identity.models import User

    actor = User.objects.create(username="history-index-test")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexdef FROM pg_catalog.pg_indexes WHERE schemaname='public' "
            "AND tablename='indexing_scanrequest' AND indexname='indexing_request_recent_idx'"
        )
        row = cursor.fetchone()
    assert row is not None
    assert "(actor_id, root_id, created_at)" in row[0]
    with scan_fixture.database.connect("aegis_migrator") as caller:
        caller.execute(
            "INSERT INTO public.indexing_scanrequest "
            "(id,actor_id,root_id,run_id,client_request_id,created_at) "
            "SELECT gen_random_uuid(),%s,%s,%s,'historical_'||n, "
            "clock_timestamp()-interval '1 day' FROM generate_series(1,10000) AS n",
            [actor.pk, scan_fixture.root.pk, scan_fixture.run_id],
        )
        caller.execute("ANALYZE public.indexing_scanrequest")
        row = caller.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON) SELECT id FROM public.indexing_scanrequest "
            "WHERE actor_id=%s AND root_id=%s AND created_at>%s-interval '60 seconds' LIMIT 20",
            [actor.pk, scan_fixture.root.pk, actor.date_joined],
        ).fetchone()
        assert row is not None
        plan = row[0][0]["Plan"]
    scan = plan["Plans"][0]
    assert scan["Index Name"] == "indexing_request_recent_idx"
    assert scan["Actual Rows"] == 0
    assert scan.get("Rows Removed by Filter", 0) == 0


@pytest.mark.parametrize("damage", ["unknown", "revision", "missing", "inaccessible", "cycle"])
def test_ancestry_fence_rejects_unavailable_descendant(
    scan_fixture: ScanFixture, damage: str,
) -> None:
    root_lease = scan_fixture.claim()
    item = replace(scan_fixture.batch(b"child").observations[0], kind=EntryKind.DIRECTORY)
    scan_fixture.record(root_lease, ReaderBatch(1, (item,)))
    assert scan_fixture.seal(root_lease)
    assert scan_fixture.finalize(root_lease).complete
    child_lease = scan_fixture.claim()
    child = CatalogEntry.objects.get(pk=child_lease.directory_id)
    if damage == "unknown":
        CatalogEntry.objects.filter(pk=child.pk).update(source_parent_revision=None)
    elif damage == "revision":
        CatalogEntry.objects.filter(pk=root_lease.directory_id).update(source_revision=1)
    elif damage in ("missing", "inaccessible"):
        CatalogEntry.objects.filter(pk=root_lease.directory_id).update(source_state=damage)
    else:
        sibling = CatalogEntry.objects.create(
            root=scan_fixture.root, source_parent=child, source_parent_revision=0,
            raw_name=b"cycle", display_name="cycle", name_key=b"cycle", kind="directory",
        )
        CatalogEntry.objects.filter(pk=child.pk).update(source_parent=sibling)
    assert scan_fixture.observe(child_lease, b"stale-descendant") is None
    assert scan_fixture.seal(child_lease) is False
    assert scan_fixture.finalize(child_lease).complete is False


def test_anchor_eof_keeps_newly_observed_children_available(scan_fixture: ScanFixture) -> None:
    lease = scan_fixture.claim()
    item = replace(scan_fixture.batch(b"child").observations[0], kind=EntryKind.DIRECTORY)
    scan_fixture.record(lease, ReaderBatch(1, (item,)))
    assert scan_fixture.seal(lease)
    assert scan_fixture.finalize(lease).complete
    child_lease = scan_fixture.claim()
    assert scan_fixture.observe(child_lease, b"nested") is not None


def test_documented_limit_deliberate_constraint_timing_can_publish_after_expiry(
    scan_fixture: ScanFixture,
) -> None:
    # User-approved fault-model exclusion: this deliberately bypasses the owned
    # checkpoint wrappers. The historical strict failure remains in task-6-report.md.
    from aegis_apps.indexing.checkpoints import _lease_payload
    from aegis_apps.indexing.payloads import checkpoint_payload
    from psycopg.types.json import Jsonb

    lease = scan_fixture.claim()
    with (scan_fixture.database.connect("aegis_migrator") as observer,
          scan_fixture.database.connect("aegis_indexer") as caller):
        observer.execute(
            "UPDATE public.indexing_directorywork SET lease_expires_at="
            "clock_timestamp()+interval '2 seconds' WHERE id=%s", [lease.work_id],
        )
        rejected = False
        try:
            with caller.transaction():
                caller.execute("SELECT public.aegis_record_scan_batch(%s,%s)", [
                    Jsonb(_lease_payload(lease)),
                    Jsonb(checkpoint_payload(scan_fixture.batch(b"forced-early"))),
                ])
                caller.execute("SET CONSTRAINTS aegis_guard_directory_commit IMMEDIATE")
                assert observer.execute(
                    "SELECT count(*) FROM public.catalog_catalogentry WHERE raw_name=%s",
                    [b"forced-early"],
                ).fetchone() == (0,)
                caller.execute("SELECT pg_catalog.pg_sleep(2.1)")
        except psycopg.errors.ObjectNotInPrerequisiteState:
            rejected = True
        row = observer.execute(
            "SELECT count(*) FROM public.catalog_catalogentry WHERE raw_name=%s", [b"forced-early"],
        ).fetchone()
        assert row is not None
        published = row[0]
        assert (rejected, published) == (False, 1)


def _checkpoint_call(
    scan_fixture: ScanFixture, lease: ScanLease, operation: str,
) -> BatchResult | FinalizeResult | bool:
    from aegis_apps.catalog.domain import DirectoryIdentity
    from aegis_apps.indexing import checkpoints
    from aegis_apps.indexing.protocol import ReaderComplete

    if operation == "record":
        return checkpoints.record_batch(lease, scan_fixture.batch(b"wrapper-owned"))
    if operation in ("seal", "seal_replay"):
        return checkpoints.seal_directory(lease, ReaderComplete(DirectoryIdentity(1, 1, 100, 100)))
    if operation == "finalize":
        return checkpoints.finalize_directory(lease)
    return checkpoints.fail_directory(lease, "permission_denied")


@pytest.mark.parametrize("operation", ["record", "seal", "finalize", "fail"])
@pytest.mark.parametrize("outer", ["atomic", "autocommit_disabled", "raw_begin"])
@pytest.mark.parametrize("supervised", [False, True])
def test_checkpoint_wrappers_refuse_caller_owned_transactions(
    scan_fixture: ScanFixture, operation: str, outer: str, supervised: bool,
) -> None:
    from aegis_apps.indexing.checkpoints import CheckpointCancellation, checkpoint_cancellation

    lease = scan_fixture.claim()
    with (scan_fixture.database.as_django_role("aegis_indexer"),
          checkpoint_cancellation(CheckpointCancellation() if supervised else None)):
        if outer == "atomic":
            with transaction.atomic(), pytest.raises(RuntimeError, match="top-level"):
                _checkpoint_call(scan_fixture, lease, operation)
        else:
            if outer == "raw_begin":
                connection.connection.execute("BEGIN")
            else:
                transaction.set_autocommit(False)
            try:
                with pytest.raises(RuntimeError, match="top-level"):
                    _checkpoint_call(scan_fixture, lease, operation)
            finally:
                transaction.rollback()
                transaction.set_autocommit(True)


@pytest.mark.parametrize("depth", [256, 257])
def test_checkpoint_ancestry_matches_reader_component_depth_bound(
    scan_fixture: ScanFixture, depth: int,
) -> None:
    from aegis_apps.indexing.models import DirectoryWork

    lease = scan_fixture.claim()
    parent_id = lease.directory_id
    for number in range(depth):
        parent_id = CatalogEntry.objects.create(
            root=scan_fixture.root, source_parent_id=parent_id, source_parent_revision=0,
            raw_name=f"node-{number}".encode(), display_name=f"node-{number}",
            name_key=f"node-{number}".encode(), kind="directory",
        ).pk
    with scan_fixture.database.as_django_role("aegis_migrator"):
        DirectoryWork.objects.filter(pk=lease.work_id).update(directory_id=parent_id)
    leaf_lease = replace(lease, directory_id=parent_id)
    result = scan_fixture.observe(leaf_lease, b"bounded-leaf")
    assert (result is not None) is (depth == 256)


def test_checkpoint_rejects_non_directory_ancestor(scan_fixture: ScanFixture) -> None:
    from aegis_apps.indexing.models import DirectoryWork

    lease = scan_fixture.claim()
    parent = CatalogEntry.objects.create(
        root=scan_fixture.root, source_parent_id=lease.directory_id, source_parent_revision=0,
        raw_name=b"file-ancestor", display_name="file-ancestor", name_key=b"file-ancestor",
        kind="file",
    )
    child = CatalogEntry.objects.create(
        root=scan_fixture.root, source_parent=parent, source_parent_revision=0,
        raw_name=b"child", display_name="child", name_key=b"child", kind="directory",
    )
    with scan_fixture.database.as_django_role("aegis_migrator"):
        DirectoryWork.objects.filter(pk=lease.work_id).update(directory=child)
    assert scan_fixture.observe(replace(lease, directory_id=child.pk), b"unavailable") is None


def test_checkpoint_wrapper_owns_transaction_and_returns_after_visibility(
    scan_fixture: ScanFixture,
) -> None:
    lease = scan_fixture.claim()
    with scan_fixture.database.connect("aegis_indexer") as observer:
        def inspect_transaction(
            execute: Callable[..., Any], sql: str, params: Any, many: bool, context: Any,
        ) -> Any:
            result = execute(sql, params, many, context)
            if sql.startswith("SELECT public.aegis_record_scan_batch"):
                assert connection.in_atomic_block and not connection.get_autocommit()
                assert observer.execute(
                    "SELECT count(*) FROM public.catalog_catalogentry WHERE raw_name=%s",
                    [b"wrapper-owned"],
                ).fetchone() == (0,)
            return result

        with (scan_fixture.database.as_django_role("aegis_indexer"),
              connection.execute_wrapper(inspect_transaction)):
            result = _checkpoint_call(scan_fixture, lease, "record")
            assert isinstance(result, BatchResult)
            assert result.inserted == 1
            assert connection.get_autocommit() and not connection.in_atomic_block
            assert observer.execute(
                "SELECT count(*) FROM public.catalog_catalogentry WHERE raw_name=%s",
                [b"wrapper-owned"],
            ).fetchone() == (1,)


@pytest.mark.parametrize("operation", ["record", "seal", "seal_replay", "finalize", "fail"])
@pytest.mark.parametrize("supervised", [False, True])
def test_wrapper_expiry_rejects_commit_including_terminal_transitions(
    scan_fixture: ScanFixture, entry_factory: Callable[..., CatalogEntry], operation: str,
    supervised: bool,
) -> None:
    from aegis_apps.indexing.checkpoints import CheckpointCancellation, checkpoint_cancellation

    with checkpoint_cancellation(CheckpointCancellation() if supervised else None):
        _assert_wrapper_expiry_rollback(scan_fixture, entry_factory, operation)


def _assert_wrapper_expiry_rollback(
    scan_fixture: ScanFixture, entry_factory: Callable[..., CatalogEntry], operation: str,
    lease: ScanLease | None = None,
) -> None:
    from aegis_apps.indexing.models import DirectoryWork

    lease = lease or scan_fixture.claim()
    unseen = entry_factory(root=scan_fixture.root, raw=b"unseen")
    if operation in ("finalize", "seal_replay"):
        assert scan_fixture.seal(lease)
    previous = DirectoryWork.objects.get(pk=lease.work_id)
    with scan_fixture.database.connect("aegis_migrator") as observer:
        observer.execute(
            "UPDATE public.indexing_directorywork SET lease_expires_at="
            "clock_timestamp()+interval '2 seconds' WHERE id=%s", [lease.work_id],
        )
        def pause_after_mutation(
            execute: Callable[..., Any], sql: str, params: Any, many: bool, context: Any,
        ) -> Any:
            result = execute(sql, params, many, context)
            if sql.startswith("SELECT public.aegis_"):
                # Real SQL latency between mutation and wrapper-owned COMMIT.
                connection.connection.execute("SELECT pg_catalog.pg_sleep(2.1)")
            return result

        with (scan_fixture.database.as_django_role("aegis_indexer"),
              connection.execute_wrapper(pause_after_mutation),
              pytest.raises(RuntimeError, match="scan checkpoint rejected")):
            _checkpoint_call(scan_fixture, lease, operation)
    current = DirectoryWork.objects.get(pk=lease.work_id)
    current_values = (
        current.state, current.eof_identity, current.error_code, current.last_batch_sequence,
    )
    assert current_values == (
        previous.state, previous.eof_identity, previous.error_code, previous.last_batch_sequence,
    )
    unseen.refresh_from_db()
    assert unseen.source_state == "present"
    assert not CatalogEntry.objects.filter(raw_name=b"wrapper-owned").exists()


@pytest.mark.parametrize("operation", ["record", "finalize"])
@pytest.mark.parametrize("boundary", ["root", "binding"])
@pytest.mark.parametrize("first", ["revoker", "checkpoint"])
def test_checkpoint_and_revocation_keep_root_first_lock_order(
    scan_fixture: ScanFixture, operation: str, boundary: str, first: str,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from aegis_apps.indexing.checkpoints import _lease_payload
    from aegis_apps.indexing.payloads import checkpoint_payload
    from psycopg.types.json import Jsonb

    from .test_leases import _wait_blocked

    lease = scan_fixture.claim()
    if operation == "finalize":
        assert scan_fixture.seal(lease)
    mutation_sql = (
        "SELECT public.aegis_record_scan_batch(%s,%s)" if operation == "record"
        else "SELECT public.aegis_finalize_scan_directory(%s,%s)"
    )
    mutation_args = [Jsonb(_lease_payload(lease)),
                     Jsonb(checkpoint_payload(scan_fixture.batch(b"ordered")))
                     if operation == "record" else 500]
    revoke_sql = (
        "UPDATE public.roots_root SET active=false WHERE id=%s" if boundary == "root"
        else "UPDATE public.indexing_indexdeployment SET epoch=epoch+1 WHERE id=1"
    )
    revoke_args = [lease.root_id] if boundary == "root" else []
    role = "aegis_web" if boundary == "root" else "aegis_migrator"
    with (scan_fixture.database.connect(role) as revoker,
          scan_fixture.database.connect("aegis_indexer") as checkpoint,
          scan_fixture.database.connect("aegis_migrator") as observer,
          ThreadPoolExecutor(max_workers=1) as pool):
        holder, waiter = (revoker, checkpoint) if first == "revoker" else (checkpoint, revoker)
        with holder.transaction():
            if first == "revoker":
                revoker.execute(revoke_sql, revoke_args)
                pending = pool.submit(checkpoint.execute, mutation_sql, mutation_args)
            else:
                row = checkpoint.execute(mutation_sql, mutation_args).fetchone()
                assert row is not None and row[0] is not None
                pending = pool.submit(revoker.execute, revoke_sql, revoke_args)
            _wait_blocked(observer, waiter.info.backend_pid, holder.info.backend_pid)
        result = pending.result(timeout=5)
        if first == "revoker":
            assert result.fetchone() == (None,)
        assert checkpoint.execute(mutation_sql, mutation_args).fetchone() == (None,)


def test_wrapper_explicitly_defers_guard_even_with_immediate_database_default(
    scan_fixture: ScanFixture, entry_factory: Callable[..., CatalogEntry],
) -> None:
    lease = scan_fixture.claim()
    with scan_fixture.database.connect("aegis_migrator") as migrator:
        migrator.execute(
            "DROP TRIGGER aegis_guard_directory_commit ON public.indexing_directorywork"
        )
        migrator.execute(
            "CREATE CONSTRAINT TRIGGER aegis_guard_directory_commit AFTER INSERT OR UPDATE "
            "ON public.indexing_directorywork DEFERRABLE INITIALLY IMMEDIATE "
            "FOR EACH ROW EXECUTE FUNCTION public.aegis_guard_directory_commit()"
        )
        try:
            # The actual mutation/expiry/rollback assertions must still pass when
            # the wrapper cannot rely on the migration's initially-deferred default.
            _assert_wrapper_expiry_rollback(
                scan_fixture, entry_factory, "record", lease=lease,
            )
        finally:
            migrator.execute(
                "DROP TRIGGER aegis_guard_directory_commit ON public.indexing_directorywork"
            )
            migrator.execute(
                "CREATE CONSTRAINT TRIGGER aegis_guard_directory_commit AFTER INSERT OR UPDATE "
                "ON public.indexing_directorywork DEFERRABLE INITIALLY DEFERRED "
                "FOR EACH ROW EXECUTE FUNCTION public.aegis_guard_directory_commit()"
            )


def test_synthetic_scan_heartbeat_uses_database_clock(
    role_database: RoleDatabase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta

    from aegis_apps.indexing.scheduling import schedule_root_scan
    from django.utils import timezone

    from tests.deployment.test_database_roles import _create_scan_fixture

    host_ahead = timezone.now() + timedelta(seconds=30)
    monkeypatch.setattr(timezone, "now", lambda: host_ahead)
    root, worker, digest = _create_scan_fixture()
    with role_database.connect("aegis_migrator") as observer:
        heartbeat = observer.execute(
            "SELECT clock_timestamp(),last_seen_at FROM public.operations_workerheartbeat "
            "WHERE worker_id=%s", [worker],
        ).fetchone()
    with role_database.as_django_role("aegis_indexer"):
        run_id = schedule_root_scan(root.pk, worker, digest)
    assert run_id is not None, heartbeat
