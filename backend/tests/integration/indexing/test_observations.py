from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from aegis_apps.catalog.domain import EntryKind, SourceState
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.indexing.models import DirectoryWork, RootIndexState
from aegis_apps.indexing.protocol import ReaderBatch
from aegis_apps.roots.models import Root

if TYPE_CHECKING:
    from conftest import ScanFixture

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def test_observation_replay_is_atomic_and_does_not_double_progress(
    scan_fixture: ScanFixture,
) -> None:
    lease = scan_fixture.claim()
    batch = scan_fixture.batch(b"photo.jpg")
    first = scan_fixture.record(lease, batch)
    assert (first.observed, first.inserted, first.changed) == (1, 1, 0)
    replay = scan_fixture.record(lease, batch)
    assert (replay.observed, replay.inserted, replay.changed) == (0, 0, 0)
    assert CatalogEntry.objects.filter(root=scan_fixture.root, raw_name=b"photo.jpg").count() == 1
    assert DirectoryWork.objects.get(pk=lease.work_id).observed_count == 1
    assert RootIndexState.objects.get(root=scan_fixture.root).observed_entries == 1
    with pytest.raises(ValueError):
        scan_fixture.observe(lease, b"forged.jpg")
    with pytest.raises(ValueError):
        scan_fixture.observe(lease, b"gap.jpg", sequence=3)
    assert not CatalogEntry.objects.filter(raw_name__in=[b"forged.jpg", b"gap.jpg"]).exists()


def test_source_replacement_keeps_id_and_logical_overrides(
    scan_fixture: ScanFixture, entry_factory: Callable[..., CatalogEntry],
) -> None:
    lease = scan_fixture.claim()
    logical_folder = entry_factory(root=scan_fixture.root, raw=b"logical-folder", kind="directory")
    original = entry_factory(root=scan_fixture.root, raw=b"photo.jpg", size=3,
                             logical_name="My photograph", logical_parent=logical_folder,
                             source_revision=7, catalog_version=9)
    result = scan_fixture.observe(lease, b"photo.jpg")
    assert result is not None
    assert result.changed == 1 and result.inserted == 0
    original.refresh_from_db()
    assert original.logical_name == "My photograph"
    assert original.logical_parent_id == logical_folder.pk
    assert (original.size, original.source_revision, original.catalog_version) == (
        Decimal(12), 8, 10,
    )
    scan_fixture.observe(lease, b"photo.jpg", sequence=2)
    original.refresh_from_db()
    assert (original.source_revision, original.catalog_version) == (8, 10)


def test_newer_epoch_observation_wins(
    scan_fixture: ScanFixture, entry_factory: Callable[..., CatalogEntry],
) -> None:
    lease = scan_fixture.claim()
    original = entry_factory(root=scan_fixture.root, raw=b"newer.jpg", size=99,
                             observation_epoch=lease.start_epoch + 1)
    scan_fixture.observe(lease, b"newer.jpg")
    original.refresh_from_db()
    assert original.size == 99 and original.observation_epoch == lease.start_epoch + 1


def test_hard_links_are_separate_locations(scan_fixture: ScanFixture) -> None:
    lease = scan_fixture.claim()
    scan_fixture.observe(lease, b"first")
    scan_fixture.observe(lease, b"second", sequence=2)
    rows = list(CatalogEntry.objects.filter(root=scan_fixture.root, inode=2))
    assert len(rows) == 2 and rows[0].pk != rows[1].pk


def test_foreign_root_lease_cannot_write(scan_fixture: ScanFixture, catalog_root: Root) -> None:
    lease = replace(scan_fixture.claim(), root_id=catalog_root.pk)
    assert scan_fixture.observe(lease, b"foreign") is None
    assert not CatalogEntry.objects.filter(raw_name=b"foreign").exists()


def test_inaccessible_metadata_retains_last_known_identity_and_prevents_seal(
    scan_fixture: ScanFixture, entry_factory: Callable[..., CatalogEntry],
) -> None:
    lease = scan_fixture.claim()
    original = entry_factory(root=scan_fixture.root, raw=b"unreadable", size=47,
                             device=9, inode=10, mtime_ns=11, ctime_ns=12)
    item = replace(scan_fixture.batch(b"unreadable").observations[0],
                   kind=EntryKind.SPECIAL, state=SourceState.INACCESSIBLE,
                   size=None, device=None, inode=None, mtime_ns=None, ctime_ns=None)
    scan_fixture.record(lease, ReaderBatch(1, (item,)))
    original.refresh_from_db()
    assert original.source_state == "inaccessible"
    assert (original.size, original.device, original.inode,
            original.mtime_ns, original.ctime_ns) == tuple(map(Decimal, (47, 9, 10, 11, 12)))
    assert scan_fixture.seal(lease) is False
    assert scan_fixture.finalize(lease).complete is False


@pytest.mark.parametrize("raw,display,key,hint", [
    (b"bad\xff.JPG", "bad\\xff.JPG", b"bad\\xff.jpg", "jpg"),
    (b"A\x01.txt", "A\\u0001.txt", b"a\\u0001.txt", "txt"),
    ("e\u0301.TXT".encode(), "e\u0301.TXT", "é.txt".encode(), "txt"),
])
def test_lossless_names_keep_authoritative_display_and_order(
    scan_fixture: ScanFixture, raw: bytes, display: str, key: bytes, hint: str,
) -> None:
    scan_fixture.observe(scan_fixture.claim(), raw)
    entry = CatalogEntry.objects.get(root=scan_fixture.root, raw_name=raw)
    assert bytes(entry.raw_name) == raw
    assert (entry.display_name, bytes(entry.name_key), entry.type_hint) == (display, key, hint)


def test_duplicate_source_names_reject_the_entire_batch(scan_fixture: ScanFixture) -> None:
    lease = scan_fixture.claim()
    item = scan_fixture.batch(b"duplicate").observations[0]
    with pytest.raises(ValueError):
        scan_fixture.record(lease, ReaderBatch(1, (item, item)))
    assert not CatalogEntry.objects.filter(root=scan_fixture.root, raw_name=b"duplicate").exists()
    assert DirectoryWork.objects.get(pk=lease.work_id).last_batch_sequence == 0


def test_child_directory_work_is_inserted_atomically_and_once(scan_fixture: ScanFixture) -> None:
    lease = scan_fixture.claim()
    item = replace(scan_fixture.batch(b"directory").observations[0], kind=EntryKind.DIRECTORY)
    batch = ReaderBatch(1, (item,))
    scan_fixture.record(lease, batch)
    scan_fixture.record(lease, batch)
    child = CatalogEntry.objects.get(root=scan_fixture.root, raw_name=b"directory")
    assert DirectoryWork.objects.filter(run_id=lease.run_id, directory=child).count() == 1


@pytest.mark.parametrize("component", [b"\x01", b"\xff", b"x"])
def test_reader_batches_of_maximum_names_are_accepted_by_checkpoint(
    scan_fixture: ScanFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: bytes,
) -> None:
    import os
    import stat
    from types import SimpleNamespace

    from aegis_apps.indexing import reader
    from aegis_apps.indexing.models import IndexDeployment
    from aegis_apps.indexing.protocol import ReaderComplete, encode_message

    IndexDeployment.objects.filter(pk=1).update(batch_records=2000)
    lease = scan_fixture.claim()
    metadata = SimpleNamespace(st_mode=stat.S_IFREG, st_size=9223372036854775807,
                               st_mtime_ns=-9223372036854775808,
                               st_ctime_ns=9223372036854775807,
                               st_dev=18446744073709551615, st_ino=18446744073709551615)

    class Entry:
        def __init__(self, number: int) -> None:
            self.name = os.fsdecode(f"{number:05}".encode() + component * 250)

        def stat(self, *, follow_symlinks: bool) -> SimpleNamespace:
            return metadata

    class Entries:
        def __enter__(self) -> Iterator[Entry]:
            return (Entry(number) for number in range(1200))

        def __exit__(self, *args: object) -> None:
            pass

    with monkeypatch.context() as patch:
        patch.setattr(reader, "_mount_snapshot", lambda fd: ("synthetic", 10, "fixed"))
        patch.setattr(reader, "_mount_id", lambda fd: 10)
        patch.setattr(os, "scandir", lambda fd: Entries())
        descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            messages = list(reader.read_directory(descriptor, (), 2000))
        finally:
            os.close(descriptor)
    assert isinstance(messages[-1], ReaderComplete)
    total = 0
    for message in messages[:-1]:
        assert isinstance(message, ReaderBatch)
        assert len(encode_message(message)) <= 1048576
        result = scan_fixture.record(lease, message)
        total += result.inserted
    assert total == 1200
