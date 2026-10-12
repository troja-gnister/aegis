from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.indexing.models import DirectoryWork

if TYPE_CHECKING:
    from aegis_apps.indexing.database import ScanLease

    from conftest import ScanFixture

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def test_failed_directory_keeps_unseen_locations_present(
    scan_fixture: ScanFixture, entry_factory: Callable[..., CatalogEntry],
) -> None:
    lease = scan_fixture.claim()
    original = entry_factory(root=scan_fixture.root, raw=b"unseen.jpg")
    assert scan_fixture.fail(lease, "permission_denied") is True
    original.refresh_from_db()
    assert original.source_state == "present"
    assert scan_fixture.finalize(lease).complete is False


def test_failure_publishes_root_degradation_without_scheduler_settlement(
    scan_fixture: ScanFixture,
) -> None:
    from aegis_apps.indexing.models import RootIndexState

    lease = scan_fixture.claim()
    assert scan_fixture.fail(lease, "reader_timeout")
    state = RootIndexState.objects.get(root=scan_fixture.root)
    assert state.status == "degraded"
    assert state.active_run_id == lease.run_id
    assert state.degraded_directories == 1


def test_successful_empty_directory_requires_seal(
    scan_fixture: ScanFixture, entry_factory: Callable[..., CatalogEntry],
) -> None:
    lease = scan_fixture.claim()
    original = entry_factory(root=scan_fixture.root, raw=b"unseen.jpg")
    assert scan_fixture.finalize(lease).complete is False
    original.refresh_from_db()
    assert original.source_state == "present"
    assert scan_fixture.seal(lease)
    result = scan_fixture.finalize(lease)
    assert (result.affected, result.complete) == (1, True)
    original.refresh_from_db()
    assert original.source_state == "missing"
    assert DirectoryWork.objects.get(pk=lease.work_id).state == "complete"


def test_bounded_finalization_resumes_and_preserves_newer_epoch(
    scan_fixture: ScanFixture, entry_factory: Callable[..., CatalogEntry],
) -> None:
    lease = scan_fixture.claim()
    for index in range(503):
        entry_factory(root=scan_fixture.root, raw=f"old-{index}".encode())
    survivor = entry_factory(root=scan_fixture.root, raw=b"newer", observation_epoch=1)
    assert scan_fixture.seal(lease)
    first = scan_fixture.finalize(lease)
    assert (first.affected, first.complete) == (500, False)
    missing = CatalogEntry.objects.filter(root=scan_fixture.root, source_state="missing")
    assert missing.count() == 500
    second = scan_fixture.finalize(lease)
    assert (second.affected, second.complete) == (3, True)
    survivor.refresh_from_db()
    assert survivor.source_state == "present"


def test_interrupted_finalization_requires_new_attempt_eof(
    scan_fixture: ScanFixture, entry_factory: Callable[..., CatalogEntry],
) -> None:
    lease = scan_fixture.claim()
    for index in range(3):
        entry_factory(root=scan_fixture.root, raw=f"old-{index}".encode())
    assert scan_fixture.seal(lease)
    assert scan_fixture.finalize(lease, 1).affected == 1
    scan_fixture.expire(lease)
    retry = scan_fixture.claim()
    assert retry.attempt == lease.attempt + 1
    assert scan_fixture.finalize(retry).complete is False
    assert scan_fixture.finalize(lease).complete is False
    assert CatalogEntry.objects.filter(root=scan_fixture.root, source_state="missing").count() == 1
    assert scan_fixture.seal(retry)
    result = scan_fixture.finalize(retry)
    assert (result.affected, result.complete) == (2, True)


def test_completed_directory_survives_reconnect_without_claim_or_double_progress(
    scan_fixture: ScanFixture,
) -> None:
    from aegis_apps.indexing.database import claim_directory
    from aegis_apps.indexing.models import RootIndexState

    lease = scan_fixture.claim()
    assert scan_fixture.seal(lease)
    assert scan_fixture.finalize(lease).complete
    with scan_fixture.database.as_django_role("aegis_indexer"):
        assert claim_directory(lease.run_id, lease.worker_id) is None
    assert scan_fixture.finalize(lease).complete is False
    assert RootIndexState.objects.get(root=scan_fixture.root).completed_directories == 1


def test_changed_eof_identity_degrades_child_without_missing_unseen_rows(
    scan_fixture: ScanFixture, entry_factory: Callable[..., CatalogEntry],
) -> None:
    from dataclasses import replace

    from aegis_apps.catalog.domain import DirectoryIdentity, EntryKind
    from aegis_apps.indexing.checkpoints import seal_directory
    from aegis_apps.indexing.protocol import ReaderBatch, ReaderComplete

    root_lease = scan_fixture.claim()
    item = replace(scan_fixture.batch(b"child").observations[0], kind=EntryKind.DIRECTORY)
    scan_fixture.record(root_lease, ReaderBatch(1, (item,)))
    assert scan_fixture.seal(root_lease)
    assert scan_fixture.finalize(root_lease).complete
    lease = scan_fixture.claim()
    child = CatalogEntry.objects.get(pk=lease.directory_id)
    unseen = entry_factory(root=scan_fixture.root, parent=child, raw=b"unseen")
    with scan_fixture.database.as_django_role("aegis_indexer"):
        assert seal_directory(lease, ReaderComplete(DirectoryIdentity(1, 2, 101, 100))) is False
    assert scan_fixture.finalize(lease).complete is False
    unseen.refresh_from_db()
    assert unseen.source_state == "present"
    assert DirectoryWork.objects.get(pk=lease.work_id).error_code == "identity_changed"
    from aegis_apps.indexing.models import RootIndexState

    assert RootIndexState.objects.get(root=scan_fixture.root).status == "degraded"


def test_a_missing_folder_takes_its_whole_subtree_in_bounded_batches(
    scan_fixture: ScanFixture,
    entry_factory: Callable[..., CatalogEntry],
) -> None:
    from aegis_apps.indexing.checkpoints import FinalizeResult
    from aegis_apps.indexing.models import RootIndexState

    root = scan_fixture.root
    lease = scan_fixture.claim()
    folder = entry_factory(root=root, raw=b"2019", kind="directory")
    album = entry_factory(root=root, raw=b"trip", parent=folder, kind="directory")
    nested = [
        entry_factory(root=root, raw=f"IMG_{index}.jpg".encode(), parent=album)
        for index in range(5)
    ]
    loose = entry_factory(root=root, raw=b"loose.jpg", parent=folder)
    tombstone = entry_factory(root=root, raw=b"gone.jpg", parent=folder, state="missing")
    newer = entry_factory(root=root, raw=b"newer.jpg", parent=album, observation_epoch=1)
    assert scan_fixture.observe(lease, b"kept.jpg") is not None
    assert scan_fixture.seal(lease)

    results: list[FinalizeResult] = []
    while not (results and results[-1].complete):
        results.append(scan_fixture.finalize(lease, 3))
        assert len(results) < 10
    # The unseen folder, then its children, then theirs, and finally the cleared flags:
    # never more than one batch of changes at once.
    assert [result.affected for result in results] == [3, 3, 3, 1]
    for entry in (folder, album, loose, *nested):
        entry.refresh_from_db()
        assert entry.source_state == "missing", entry.raw_name
    tombstone.refresh_from_db()
    newer.refresh_from_db()
    assert (tombstone.source_state, tombstone.catalog_version) == ("missing", 0)
    assert newer.source_state == "present"  # Observed in a newer epoch: not this run's to retire.
    assert CatalogEntry.objects.get(root=root, raw_name=b"kept.jpg").source_state == "present"
    assert RootIndexState.objects.get(root=root).completed_directories == 1


def test_retiring_reads_stay_bounded_and_never_revisit_retired_subtrees(
    scan_fixture: ScanFixture, entry_factory: Callable[..., CatalogEntry],
) -> None:
    """Bounded retirement reads; afterwards a fully missing subtree is never read again."""
    from aegis_apps.indexing.checkpoints import _lease_payload
    from aegis_apps.indexing.models import RootIndexState
    from aegis_apps.indexing.scheduling import schedule_root_scan
    from django.db import connection, transaction
    from django.db.models.functions import Now
    from psycopg.types.json import Jsonb

    root, database = scan_fixture.root, scan_fixture.database
    photos = entry_factory(root=root, raw=b"Photos", kind="directory")

    def add(parents: list[CatalogEntry], count: int, kind: str, state: str) -> list[CatalogEntry]:
        return CatalogEntry.objects.bulk_create([
            CatalogEntry(root=root, source_parent=parent, logical_parent=parent,
                         raw_name=f"{kind}-{index}".encode(), display_name=f"{kind}-{index}",
                         name_key=f"{kind}-{index}".encode(), kind=kind,
                         source_state=state, size=1, mtime_ns=1)
            for parent in parents for index in range(count)
        ])

    def finalize_buffers(lease: ScanLease, limit: int) -> int:
        """Every buffer one finalize call touched, its nested statements included."""
        parameters: list[Any] = [Jsonb(_lease_payload(lease)), limit]
        with (
            database.as_django_role("aegis_indexer"),
            transaction.atomic(durable=True),
            connection.cursor() as cursor,
        ):
            cursor.execute("SET CONSTRAINTS public.aegis_guard_directory_commit DEFERRED")
            cursor.execute(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
                "SELECT public.aegis_finalize_scan_directory(%s, %s)", parameters,
            )
            plan = cursor.fetchone()[0][0]["Plan"]
        return int(plan["Shared Hit Blocks"]) + int(plan["Shared Read Blocks"])

    def finalize_run(limit: int) -> list[int]:
        """The anchor's pass of one run (Photos is never observed), call by call."""
        lease = scan_fixture.claim()
        assert scan_fixture.observe(lease, b"kept.jpg") is not None
        assert scan_fixture.seal(lease)
        costs = []
        while DirectoryWork.objects.get(pk=lease.work_id).state != "complete":
            costs.append(finalize_buffers(lease, limit))
            assert len(costs) < 100
        with database.as_django_role("aegis_indexer"):
            assert schedule_root_scan(root.pk, scan_fixture.worker, "a" * 64) is None
        RootIndexState.objects.filter(root=root).update(due_at=Now())
        with database.as_django_role("aegis_indexer"):
            run_id = schedule_root_scan(root.pk, scan_fixture.worker, "a" * 64)
        assert run_id is not None
        scan_fixture.run_id = run_id
        return costs

    folders = add([photos], 200, "directory", "present")
    add(folders, 10, "file", "present")
    # The two reads of the retirement loop (finalize.sql), mid-retirement: the pending
    # entries, then one pending entry's live children, each from its partial index in ID
    # order and stopped by the batch limit; nothing is sorted or read in full.
    CatalogEntry.objects.filter(pk=photos.pk).update(source_state="missing", retire_children=True)
    CatalogEntry.objects.filter(pk__in=[folder.pk for folder in folders[:150]]).update(
        source_state="missing", retire_children=True,
    )
    for statement, index, rows in (
        (f"SELECT entry.id FROM catalog_catalogentry AS entry WHERE entry.root_id='{root.pk}' "
         "AND entry.retire_children LIMIT 100", "catalog_retiring", 100),
        (f"SELECT child.id FROM catalog_catalogentry AS child WHERE child.root_id='{root.pk}' "
         f"AND child.source_parent_id='{photos.pk}' AND child.source_state<>'missing' "
         "AND child.observation_epoch<=0 ORDER BY child.id LIMIT 100", "catalog_live_children", 50),
    ):
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("ANALYZE catalog_catalogentry")
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + statement)
            nodes, pending = [], [cursor.fetchone()[0][0]["Plan"]]
        while pending:
            nodes.append(pending.pop())
            pending.extend(nodes[-1].get("Plans", []))
        scans = [node for node in nodes if node.get("Relation Name") == "catalog_catalogentry"]
        # An index scan, or a bitmap heap scan that the limit stops early.
        used = {node.get("Index Name") for node in nodes} - {None}
        assert len(scans) == 1 and used == {index}, nodes
        assert scans[0]["Actual Rows"] == rows, scans
        assert not any(node["Node Type"] in ("Sort", "Seq Scan") for node in nodes), nodes
    CatalogEntry.objects.filter(pk__in=[folder.pk for folder in folders[:150]]).update(
        source_state="present", retire_children=False,
    )
    CatalogEntry.objects.filter(pk=photos.pk).update(source_state="present", retire_children=False)

    first = finalize_run(100)  # Photos vanished: 2,201 entries to retire, 201 flags to clear.
    # Linear: 2,402 changes in batches of 100, never a call without progress.
    assert 24 <= len(first) <= 27, first
    assert not CatalogEntry.objects.filter(
        root=root, source_state="present", source_parent__isnull=False,
    ).exclude(raw_name=b"kept.jpg").exists()

    (small,) = finalize_run(500)
    old = add(folders, 2, "directory", "missing")  # 400 more retired folders below,
    add(old, 5, "file", "missing")  # and 2,000 more retired files.
    (large,) = finalize_run(500)
    # A later run never reads a retired subtree, however many folders or entries it holds:
    # a walk over 400 more missing folders alone would cost about a thousand more buffers.
    assert large <= small + 200, (small, large)


def test_the_upgrade_flags_stale_subtrees_and_the_next_finalize_retires_them(
    scan_fixture: ScanFixture, entry_factory: Callable[..., CatalogEntry],
) -> None:
    """Earlier releases left a vanished or replaced folder's descendants present."""
    import importlib

    upgrade = importlib.import_module("aegis_apps.catalog.migrations.0005_cascade_indexes")
    root = scan_fixture.root
    vanished = entry_factory(root=root, raw=b"vanished", kind="directory", state="missing")
    stale = entry_factory(root=root, raw=b"stale.jpg", parent=vanished)
    replaced = entry_factory(root=root, raw=b"replaced", kind="file")
    orphan = entry_factory(root=root, raw=b"orphan.jpg", parent=replaced)
    deleted = entry_factory(root=root, raw=b"deleted", kind="file", state="missing")
    relic = entry_factory(root=root, raw=b"relic.jpg", parent=deleted)  # replaced, then deleted
    settled = entry_factory(root=root, raw=b"settled", kind="directory", state="missing")
    entry_factory(root=root, raw=b"gone.jpg", parent=settled, state="missing")
    plain = entry_factory(root=root, raw=b"plain.txt")
    with scan_fixture.database.connect("aegis_migrator") as migrator:
        migrator.execute(upgrade.FLAG_STALE_SUBTREES)
    flagged = set(CatalogEntry.objects.filter(root=root, retire_children=True)
                  .values_list("raw_name", flat=True))
    assert {bytes(raw) for raw in flagged} == {b"vanished", b"replaced", b"deleted"}

    lease = scan_fixture.claim()
    for sequence, raw in enumerate((b"replaced", b"plain.txt"), start=1):
        assert scan_fixture.observe(lease, raw, sequence) is not None
    assert scan_fixture.seal(lease)
    assert any(scan_fixture.finalize(lease).complete for _ in range(5))
    for entry, state in ((stale, "missing"), (orphan, "missing"), (relic, "missing"),
                         (replaced, "present"), (plain, "present")):
        entry.refresh_from_db()
        assert (entry.source_state, entry.retire_children) == (state, False), entry.raw_name
