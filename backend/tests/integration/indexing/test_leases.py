from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.indexing.database import ScanLease
from aegis_apps.indexing.models import DirectoryWork, IndexDeployment
from aegis_apps.roots.models import Root
from django.db import connection
from psycopg.types.json import Jsonb

from tests.deployment.test_database_roles import _create_scan_fixture, _scan_scalar
from tests.support.database_roles import RoleDatabase

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def _claim(role_database: RoleDatabase) -> tuple[Root, ScanLease]:
    from aegis_apps.indexing.database import claim_directory
    from aegis_apps.indexing.scheduling import schedule_root_scan

    root, worker, digest = _create_scan_fixture()
    with role_database.as_django_role("aegis_indexer"):
        run_id = schedule_root_scan(root.pk, worker, digest)
        assert run_id is not None
        lease = claim_directory(run_id, worker)
    assert lease is not None
    return root, lease


def test_typed_lease_round_trip_and_one_reader_per_root(role_database: RoleDatabase) -> None:
    from aegis_apps.indexing.database import claim_directory, renew_directory

    root, lease = _claim(role_database)
    child = CatalogEntry.objects.create(
        root=root,
        source_parent_id=lease.directory_id,
        raw_name=b"child",
        display_name="child",
        name_key=b"child",
        kind="directory",
    )
    DirectoryWork.objects.create(run_id=lease.run_id, directory=child, parent_revision=0)
    with role_database.as_django_role("aegis_indexer"):
        assert renew_directory(lease)
        assert claim_directory(lease.run_id, lease.worker_id) is None
    work = DirectoryWork.objects.get(pk=lease.work_id)
    assert work.attempt == lease.attempt == 1
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXTRACT(EPOCH FROM (lease_expires_at-clock_timestamp())) "
            "FROM indexing_directorywork WHERE id=%s",
            [lease.work_id],
        )
        assert 55 < cursor.fetchone()[0] <= 60


@pytest.mark.parametrize(
    "invalid",
    [
        "expired",
        "future",
        "worker",
        "attempt",
        "binding",
        "policy",
        "root",
        "inactive",
        "slot",
        "revision",
    ],
)
def test_renew_rejects_stale_authority(role_database: RoleDatabase, invalid: str) -> None:
    from aegis_apps.indexing.database import renew_directory

    root, lease = _claim(role_database)
    if invalid in ("expired", "future"):
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE indexing_directorywork SET lease_expires_at=clock_timestamp() "
                "+ make_interval(secs=>%s) WHERE id=%s",
                [-1 if invalid == "expired" else 120, lease.work_id],
            )
    elif invalid == "worker":
        lease = replace(lease, worker_id=str(uuid.uuid4()))
    elif invalid == "attempt":
        lease = replace(lease, attempt=lease.attempt + 1)
    elif invalid == "binding":
        lease = replace(lease, binding_epoch=2)
    elif invalid == "policy":
        lease = replace(lease, policy_epoch=2)
    elif invalid == "root":
        Root.objects.filter(pk=root.pk).update(authorization_epoch=2)
    elif invalid == "inactive":
        Root.objects.filter(pk=root.pk).update(active=False)
    elif invalid == "slot":
        IndexDeployment.objects.filter(pk=1).update(slot_ids=[])
    else:
        CatalogEntry.objects.filter(pk=lease.directory_id).update(source_revision=1)
    with role_database.as_django_role("aegis_indexer"):
        assert renew_directory(lease) is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("attempt", True),
        ("attempt", -1),
        ("attempt", 2**63),
        ("attempt", 1.5),
        ("attempt", "1"),
        ("run_id", "invalid"),
        ("worker_id", "X" * 9000),
        ("manifest_identity", "A" * 64),
        ("extra", 1),
    ],
    ids=[
        "boolean",
        "negative",
        "overflow",
        "fraction",
        "string",
        "uuid",
        "oversized",
        "digest",
        "extra",
    ],
)
def test_lease_payload_is_strict_at_python_and_database_boundaries(
    role_database: RoleDatabase,
    field: str,
    value: object,
) -> None:
    from aegis_apps.indexing.database import parse_scan_lease, vars_from_lease

    _, lease = _claim(role_database)
    payload = {
        key: str(item) if isinstance(item, uuid.UUID) else item
        for key, item in vars_from_lease(lease).items()
    }
    payload[field] = value
    with pytest.raises(ValueError):
        parse_scan_lease(payload)
    with (
        role_database.connect("aegis_indexer") as caller,
        pytest.raises(psycopg.errors.InvalidParameterValue),
    ):
        caller.execute("SELECT public.aegis_renew_scan_directory(%s)", [Jsonb(payload)])


def test_takeover_fences_old_attempt_and_resets_sequence(role_database: RoleDatabase) -> None:
    from aegis_apps.indexing.database import claim_directory, vars_from_lease

    _, lease = _claim(role_database)
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE indexing_directorywork SET lease_expires_at=clock_timestamp() "
            "- interval '30 seconds', last_batch_sequence=9, eof_identity='{}'::jsonb "
            "WHERE id=%s",
            [lease.work_id],
        )
    with role_database.as_django_role("aegis_indexer"):
        newer = claim_directory(lease.run_id, lease.worker_id)
    assert newer is not None and newer.attempt == 2
    work = DirectoryWork.objects.get(pk=lease.work_id)
    assert work.last_batch_sequence == 0 and work.eof_identity is None
    payload = {
        key: str(item) if isinstance(item, uuid.UUID) else item
        for key, item in vars_from_lease(lease).items()
    }
    with role_database.connect("aegis_indexer") as old_caller:
        assert (
            _scan_scalar(
                old_caller.execute("SELECT public.aegis_renew_scan_directory(%s)", [Jsonb(payload)])
            )
            is False
        )


def _wait_blocked(observer: psycopg.Connection[Any], blocked_pid: int, blocking_pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = observer.execute(
            "SELECT %s = ANY(pg_catalog.pg_blocking_pids(%s))", [blocking_pid, blocked_pid]
        ).fetchone()
        if row is not None and row[0]:
            return
        time.sleep(0.01)
    pytest.fail("expected database lock barrier was not reached")


def _payload(lease: ScanLease) -> dict[str, object]:
    from aegis_apps.indexing.database import vars_from_lease

    return {
        key: str(item) if isinstance(item, uuid.UUID) else item
        for key, item in vars_from_lease(lease).items()
    }


def test_old_attempt_waiting_on_takeover_cannot_renew(role_database: RoleDatabase) -> None:
    _, lease = _claim(role_database)
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE indexing_directorywork SET lease_expires_at=clock_timestamp() "
            "- interval '30 seconds' WHERE id=%s",
            [lease.work_id],
        )
    with (
        role_database.connect("aegis_indexer") as takeover,
        role_database.connect("aegis_indexer") as old,
        role_database.connect("aegis_migrator") as observer,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        with takeover.transaction():
            newer = _scan_scalar(
                takeover.execute(
                    "SELECT public.aegis_claim_scan_directory(%s,%s)",
                    [lease.run_id, lease.worker_id],
                )
            )
            assert newer["attempt"] == 2
            waiting = pool.submit(
                old.execute,
                "SELECT public.aegis_renew_scan_directory(%s)",
                [Jsonb(_payload(lease))],
            )
            _wait_blocked(observer, old.info.backend_pid, takeover.info.backend_pid)
        assert _scan_scalar(waiting.result(timeout=5)) is False


@pytest.mark.parametrize("boundary", ["root", "binding"])
@pytest.mark.parametrize("first", ["revoke", "renew"])
def test_revocation_and_renewal_serialize_at_database_barrier(
    role_database: RoleDatabase,
    boundary: str,
    first: str,
) -> None:
    root, lease = _claim(role_database)
    role = "aegis_web" if boundary == "root" else "aegis_migrator"
    sql = (
        "UPDATE public.roots_root SET active=false WHERE id=%s"
        if boundary == "root"
        else "UPDATE public.indexing_indexdeployment SET epoch=epoch+1 WHERE id=1"
    )
    params = [root.pk] if boundary == "root" else []
    with (
        role_database.connect(role) as revoker,
        role_database.connect("aegis_indexer") as renewer,
        role_database.connect("aegis_migrator") as observer,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        holder = revoker if first == "revoke" else renewer
        waiter = renewer if first == "revoke" else revoker
        with holder.transaction():
            if first == "revoke":
                revoker.execute(sql, params)
                waiting = pool.submit(
                    renewer.execute,
                    "SELECT public.aegis_renew_scan_directory(%s)",
                    [Jsonb(_payload(lease))],
                )
            else:
                assert (
                    _scan_scalar(
                        renewer.execute(
                            "SELECT public.aegis_renew_scan_directory(%s)", [Jsonb(_payload(lease))]
                        )
                    )
                    is True
                )
                waiting = pool.submit(revoker.execute, sql, params)
            _wait_blocked(observer, waiter.info.backend_pid, holder.info.backend_pid)
        result = waiting.result(timeout=5)
        if first == "revoke":
            assert _scan_scalar(result) is False
        assert (
            _scan_scalar(
                renewer.execute(
                    "SELECT public.aegis_renew_scan_directory(%s)", [Jsonb(_payload(lease))]
                )
            )
            is False
        )


@pytest.mark.parametrize(
    "attempt,elapsed,claimed",
    [(1, 4, False), (1, 6, True), (2, 9, False), (2, 11, True), (3, 20, False)],
)
def test_database_clock_retry_backoff_and_exhaustion(
    role_database: RoleDatabase,
    attempt: int,
    elapsed: int,
    claimed: bool,
) -> None:
    from aegis_apps.indexing.database import claim_directory

    _, lease = _claim(role_database)
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE indexing_directorywork SET attempt=%s, "
            "lease_expires_at=clock_timestamp()-make_interval(secs=>%s) WHERE id=%s",
            [attempt, elapsed, lease.work_id],
        )
    with role_database.as_django_role("aegis_indexer"):
        result = claim_directory(lease.run_id, lease.worker_id)
    assert (result is not None) is claimed
    work = DirectoryWork.objects.get(pk=lease.work_id)
    if attempt == 3:
        assert work.state == "degraded"
        assert work.error_code == "scan_attempts_exhausted"
    else:
        assert work.attempt == attempt + int(claimed)


@pytest.mark.parametrize("value", [None, [], {}, 1, "invalid"])
def test_nonobject_and_missing_lease_payload_is_rejected(
    role_database: RoleDatabase, value: object
) -> None:
    from aegis_apps.indexing.database import parse_scan_lease

    with pytest.raises(ValueError):
        parse_scan_lease(value)
    with (
        role_database.connect("aegis_indexer") as caller,
        pytest.raises(psycopg.errors.InvalidParameterValue),
    ):
        caller.execute("SELECT public.aegis_renew_scan_directory(%s)", [Jsonb(value)])


def test_claim_samples_database_time_after_waiting_for_directory_lock(
    role_database: RoleDatabase,
) -> None:
    from aegis_apps.indexing.scheduling import schedule_root_scan

    root, worker, digest = _create_scan_fixture()
    with role_database.as_django_role("aegis_indexer"):
        run_id = schedule_root_scan(root.pk, worker, digest)
    directory = CatalogEntry.objects.get(root=root, source_parent=None)
    with (
        role_database.connect("aegis_migrator") as holder,
        role_database.connect("aegis_indexer") as claimer,
        role_database.connect("aegis_migrator") as observer,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        with holder.transaction():
            holder.execute(
                "SELECT id FROM public.catalog_catalogentry WHERE id=%s FOR UPDATE", [directory.pk]
            )
            waiting = pool.submit(
                claimer.execute, "SELECT public.aegis_claim_scan_directory(%s,%s)", [run_id, worker]
            )
            _wait_blocked(observer, claimer.info.backend_pid, holder.info.backend_pid)
            before_unlock = _scan_scalar(holder.execute("SELECT pg_catalog.clock_timestamp()"))
        lease = _scan_scalar(waiting.result(timeout=5))
    assert lease is not None
    work = DirectoryWork.objects.get(pk=lease["work_id"])
    assert work.lease_expires_at is not None
    assert work.lease_expires_at >= before_unlock + timedelta(seconds=60)


@pytest.mark.parametrize(
    "field",
    [
        "run_id",
        "work_id",
        "root_id",
        "directory_id",
        "generation",
        "start_epoch",
        "root_epoch",
        "parent_revision",
        "manifest_identity",
    ],
)
def test_every_lease_identity_field_is_fenced(role_database: RoleDatabase, field: str) -> None:
    _, lease = _claim(role_database)
    payload = _payload(lease)
    if field.endswith("_id"):
        payload[field] = str(uuid.uuid4())
    elif field == "manifest_identity":
        payload[field] = "b" * 64
    else:
        payload[field] = int(str(payload[field])) + 1
    with role_database.connect("aegis_indexer") as caller:
        assert (
            _scan_scalar(
                caller.execute("SELECT public.aegis_renew_scan_directory(%s)", [Jsonb(payload)])
            )
            is False
        )
