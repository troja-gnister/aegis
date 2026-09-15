from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.indexing.models import DirectoryWork

if TYPE_CHECKING:
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
