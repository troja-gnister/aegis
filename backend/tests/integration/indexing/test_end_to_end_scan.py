"""A complete real-reader scan of an owned synthetic tree, with restart, on real PostgreSQL."""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.indexing.protocol import ReaderBatch, ReaderComplete

if TYPE_CHECKING:
    from conftest import ScanFixture

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

TIED_MTIME_NS = 1_577_836_800_000_000_000
MAX_DIRECTORY_CLAIMS = 32


def _write(target: Path, content: bytes, mtime_ns: int = TIED_MTIME_NS) -> None:
    with target.open("xb") as handle:
        handle.write(content)
    os.utime(target, ns=(mtime_ns, mtime_ns))


def _tree(base: Path) -> Path:
    """Owned synthetic source plus an unmounted sibling the symlink names."""
    sibling = base / "sibling"
    sibling.mkdir()
    _write(sibling / "secret.txt", b"never followed\n")
    source = base / "source"
    source.mkdir()
    for number in range(250):
        _write(source / f"bulk-{number:04}.txt", f"bulk {number}\n".encode())
    _write(source / "Tie.txt", b"upper\n")
    _write(source / "tie.txt", b"lower\n")
    _write(source / "README", b"no extension\n", 978_523_200_000_000_000)
    _write(source / "data.unknown", b"literal unknown extension\n")
    (source / "link-outside").symlink_to("../sibling/secret.txt")
    (source / "empty").mkdir()
    deep = source / "albums" / "2024" / "summer"
    deep.mkdir(parents=True)
    _write(deep / "deep.txt", b"nested\n")
    _write(source / "albums" / "cover.png", b"\x89PNG synthetic\n")
    return source


def _snapshot(base: Path) -> dict[str, tuple[Any, ...]]:
    """Descriptor-relative, no-follow view of names, kinds, bytes, sizes and mtimes."""
    result: dict[str, tuple[Any, ...]] = {}

    def walk(descriptor: int, prefix: str) -> None:
        for name in sorted(os.listdir(descriptor)):
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            key = f"{prefix}{name}"
            if stat.S_ISDIR(info.st_mode):
                result[key] = ("directory", info.st_mtime_ns)
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor)
                try:
                    walk(child, f"{key}/")
                finally:
                    os.close(child)
            elif stat.S_ISLNK(info.st_mode):
                result[key] = ("symlink", os.readlink(name, dir_fd=descriptor))
            else:
                assert stat.S_ISREG(info.st_mode)
                handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
                try:
                    hasher = hashlib.sha256()
                    while chunk := os.read(handle, 1 << 16):
                        hasher.update(chunk)
                    digest = hasher.hexdigest()
                finally:
                    os.close(handle)
                result[key] = ("file", info.st_size, info.st_mtime_ns, digest)

    root = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        walk(root, "")
    finally:
        os.close(root)
    return result


def _scan(scan_fixture: ScanFixture, source: Path, *, interrupt: bool) -> int:
    """Drive every claimed directory through the real reader and checkpoint wrappers."""
    from aegis_apps.indexing import reader
    from aegis_apps.indexing.checkpoints import finalize_directory, seal_directory
    from aegis_apps.indexing.database import claim_directory
    from aegis_apps.indexing.runner import source_components

    database = scan_fixture.database
    descriptor = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    restarted = 0
    try:
        for _ in range(MAX_DIRECTORY_CLAIMS):
            with database.as_django_role("aegis_indexer"):
                lease = claim_directory(scan_fixture.run_id, scan_fixture.worker)
                if lease is None:
                    return restarted
                components = source_components(lease)
            messages = list(reader.read_directory(descriptor, components, 100))
            terminal = messages[-1]
            assert isinstance(terminal, ReaderComplete)
            batches = [message for message in messages[:-1] if isinstance(message, ReaderBatch)]
            if interrupt and not restarted and len(batches) > 1:
                # A crash after one committed batch: the unfinished directory restarts
                # from the beginning under a fresh attempt and idempotent upserts.
                scan_fixture.record(lease, batches[0])
                scan_fixture.expire(lease)
                restarted += 1
                continue
            for batch in batches:
                assert scan_fixture.record(lease, batch).observed == len(batch.observations)
            with database.as_django_role("aegis_indexer"):
                assert seal_directory(lease, terminal)
                for _ in range(8):
                    if finalize_directory(lease, 500).complete:
                        break
                else:
                    pytest.fail("directory finalization did not complete")
        pytest.fail("scan claimed more directories than the synthetic tree holds")
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("interrupt", [False, True], ids=["complete", "restart"])
def test_real_reader_scan_catalogs_tree_and_preserves_every_source(
    scan_fixture: ScanFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    interrupt: bool,
) -> None:
    from aegis_apps.indexing import reader
    from aegis_apps.indexing.models import RootIndexState
    from aegis_apps.indexing.scheduling import schedule_root_scan

    source = _tree(tmp_path)
    before = _snapshot(tmp_path)
    # Portable topology only: the real procfs mount check is covered by deployment tests.
    monkeypatch.setattr(reader, "_mount_snapshot", lambda fd: ("synthetic", 10, "fixed"))
    monkeypatch.setattr(reader, "_mount_id", lambda fd: 10)

    restarted = _scan(scan_fixture, source, interrupt=interrupt)
    assert restarted == (1 if interrupt else 0)
    with scan_fixture.database.as_django_role("aegis_indexer"):
        schedule_root_scan(scan_fixture.root.pk, scan_fixture.worker, "a" * 64)
    state = RootIndexState.objects.get(root=scan_fixture.root)
    assert state.status == "ready"
    assert state.active_run_id is None
    assert state.degraded_directories == 0

    rows = {
        bytes(row["raw_name"]): row for row in CatalogEntry.objects.filter(
            root=scan_fixture.root, source_parent__isnull=False,
        ).values("raw_name", "kind", "source_state", "size", "mtime_ns", "type_hint")
    }
    expected = {key.rsplit("/", 1)[-1].encode() for key in before if key.startswith("source/")}
    assert set(rows) == expected
    assert b"secret.txt" not in rows  # The symlink target is never entered.
    assert rows[b"link-outside"]["kind"] == "symlink"
    assert rows[b"empty"]["kind"] == rows[b"summer"]["kind"] == "directory"
    assert rows[b"README"]["type_hint"] is None
    assert rows[b"data.unknown"]["type_hint"] == "unknown"
    assert rows[b"README"]["mtime_ns"] == 978_523_200_000_000_000
    assert {rows[f"bulk-{n:04}.txt".encode()]["mtime_ns"] for n in range(250)} == {TIED_MTIME_NS}
    assert all(row["source_state"] == "present" for row in rows.values())
    for key, value in before.items():
        if key.startswith("source/") and value[0] == "file":
            row = rows[key.rsplit("/", 1)[-1].encode()]
            assert (row["size"], row["mtime_ns"]) == (value[1], value[2])

    assert _snapshot(tmp_path) == before


def _expected_counts(before: dict[str, tuple[Any, ...]]) -> tuple[int, int]:
    """Entries below the source and directories including the source itself (the anchor)."""
    entries = [key for key in before if key.startswith("source/")]
    directories = [key for key in entries if before[key][0] == "directory"]
    return len(entries), len(directories) + 1


@pytest.mark.parametrize("start", ["periodic", "manual"])
@pytest.mark.parametrize("interrupt", [False, True], ids=["complete", "restart"])
def test_progress_counters_describe_the_current_run_only(
    scan_fixture: ScanFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start: str,
    interrupt: bool,
) -> None:
    """Regression: a new generation used to add its progress to every earlier run's."""
    import uuid

    from aegis_apps.identity.models import User
    from aegis_apps.indexing import reader
    from aegis_apps.indexing.models import RootIndexState, ScanRun
    from aegis_apps.indexing.scheduling import schedule_root_scan
    from aegis_apps.indexing.services import request_root_scan
    from aegis_apps.roots.models import RootGrant
    from django.db.models.functions import Now

    source = _tree(tmp_path)
    expected_entries, expected_directories = _expected_counts(_snapshot(tmp_path))
    monkeypatch.setattr(reader, "_mount_snapshot", lambda fd: ("synthetic", 10, "fixed"))
    monkeypatch.setattr(reader, "_mount_id", lambda fd: 10)
    database, root = scan_fixture.database, scan_fixture.root

    def counters() -> tuple[str, int, int, int]:
        state = RootIndexState.objects.get(root=root)
        return (
            state.status,
            state.observed_entries,
            state.completed_directories,
            state.degraded_directories,
        )

    def settle() -> None:
        with database.as_django_role("aegis_indexer"):
            assert schedule_root_scan(root.pk, scan_fixture.worker, "a" * 64) is None

    first_restarts = _scan(scan_fixture, source, interrupt=interrupt)
    assert first_restarts == (1 if interrupt else 0)
    settle()
    assert counters() == ("ready", expected_entries, expected_directories, 0)

    if start == "periodic":
        RootIndexState.objects.filter(root=root).update(due_at=Now())
        with database.as_django_role("aegis_indexer"):
            second = schedule_root_scan(root.pk, scan_fixture.worker, "a" * 64)
    else:
        actor = User.objects.create_user(username=f"rescan-{uuid.uuid4().hex}")
        RootGrant.objects.create(root=root, user=actor, permissions=128)
        with database.as_django_role("aegis_web"):
            second = request_root_scan(actor, root.pk, "rescan_counters")
    assert second is not None and second != scan_fixture.run_id
    assert ScanRun.objects.get(pk=second).generation == 2
    # The new run starts from zero, not from the completed run's totals.
    assert counters() == ("queued", 0, 0, 0)

    scan_fixture.run_id = second
    assert _scan(scan_fixture, source, interrupt=interrupt) == first_restarts
    settle()
    assert counters() == ("ready", expected_entries, expected_directories, 0)
    assert RootIndexState.objects.get(root=root).next_generation == 3


def test_moved_and_deleted_folders_take_their_subtrees_and_come_back(
    scan_fixture: ScanFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a vanished folder's descendants used to stay present forever."""
    import shutil

    from aegis_apps.indexing import reader
    from aegis_apps.indexing.models import RootIndexState
    from aegis_apps.indexing.scheduling import schedule_root_scan
    from django.db.models.functions import Now

    source = _tree(tmp_path)
    monkeypatch.setattr(reader, "_mount_snapshot", lambda fd: ("synthetic", 10, "fixed"))
    monkeypatch.setattr(reader, "_mount_id", lambda fd: 10)
    database, root = scan_fixture.database, scan_fixture.root

    def scan() -> None:
        _scan(scan_fixture, source, interrupt=False)
        with database.as_django_role("aegis_indexer"):
            assert schedule_root_scan(root.pk, scan_fixture.worker, "a" * 64) is None
        assert RootIndexState.objects.get(root=root).status == "ready"

    def rescan() -> None:
        RootIndexState.objects.filter(root=root).update(due_at=Now())
        with database.as_django_role("aegis_indexer"):
            run_id = schedule_root_scan(root.pk, scan_fixture.worker, "a" * 64)
        assert run_id is not None
        scan_fixture.run_id = run_id
        scan()

    def states(*names: bytes) -> dict[bytes, list[str]]:
        rows = CatalogEntry.objects.filter(root=root, raw_name__in=names).order_by("source_state")
        found: dict[bytes, list[str]] = {}
        for raw, state in rows.values_list("raw_name", "source_state"):
            found.setdefault(bytes(raw), []).append(state)
        return found

    scan()
    subtree = (b"2024", b"summer", b"deep.txt")
    original = {
        bytes(raw): pk
        for raw, pk in CatalogEntry.objects.filter(root=root, raw_name__in=subtree).values_list(
            "raw_name", "id"
        )
    }
    assert states(*subtree) == {name: ["present"] for name in subtree}

    # A move: the old copy goes missing with its whole subtree, so nothing is listed twice.
    (source / "archive").mkdir()
    os.rename(source / "albums" / "2024", source / "archive" / "2024")
    rescan()
    assert states(*subtree) == {name: ["missing", "present"] for name in subtree}
    assert {CatalogEntry.objects.get(pk=pk).source_state for pk in original.values()} == {"missing"}

    # Moving it back brings the very same rows back once the folder is read again.
    os.rename(source / "archive" / "2024", source / "albums" / "2024")
    rescan()
    assert {CatalogEntry.objects.get(pk=pk).source_state for pk in original.values()} == {"present"}
    assert states(*subtree) == {name: ["missing", "present"] for name in subtree}

    # A delete: everything below goes; the parent folder's other entries stay.
    shutil.rmtree(source / "albums" / "2024")
    rescan()
    assert {CatalogEntry.objects.get(pk=pk).source_state for pk in original.values()} == {"missing"}
    assert states(*subtree) == {name: ["missing", "missing"] for name in subtree}
    assert states(b"cover.png", b"albums") == {b"cover.png": ["present"], b"albums": ["present"]}


def test_a_folder_replaced_by_a_file_retires_its_old_children(
    scan_fixture: ScanFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    from aegis_apps.indexing import reader
    from aegis_apps.indexing.models import RootIndexState
    from aegis_apps.indexing.scheduling import schedule_root_scan
    from django.db.models.functions import Now

    source = _tree(tmp_path)
    monkeypatch.setattr(reader, "_mount_snapshot", lambda fd: ("synthetic", 10, "fixed"))
    monkeypatch.setattr(reader, "_mount_id", lambda fd: 10)
    database, root = scan_fixture.database, scan_fixture.root

    def scan(first: bool = False) -> None:
        if not first:
            RootIndexState.objects.filter(root=root).update(due_at=Now())
            with database.as_django_role("aegis_indexer"):
                run_id = schedule_root_scan(root.pk, scan_fixture.worker, "a" * 64)
            assert run_id is not None
            scan_fixture.run_id = run_id
        _scan(scan_fixture, source, interrupt=False)
        with database.as_django_role("aegis_indexer"):
            assert schedule_root_scan(root.pk, scan_fixture.worker, "a" * 64) is None

    def state(raw: bytes) -> tuple[str, str]:
        row = CatalogEntry.objects.filter(root=root, raw_name=raw).values_list(
            "kind", "source_state",
        )
        assert row.count() == 1
        return row.get()

    scan(first=True)
    shutil.rmtree(source / "albums" / "2024")
    _write(source / "albums" / "2024", b"now a file\n")
    scan()
    # The same location is now a file; what was below the folder is not listed any more.
    assert state(b"2024") == ("file", "present")
    assert state(b"summer") == ("directory", "missing")
    assert state(b"deep.txt") == ("file", "missing")

    (source / "albums" / "2024").unlink()
    (source / "albums" / "2024" / "summer").mkdir(parents=True)
    _write(source / "albums" / "2024" / "summer" / "deep.txt", b"nested\n")
    scan()
    assert state(b"2024") == ("directory", "present")
    assert state(b"summer") == ("directory", "present")
    assert state(b"deep.txt") == ("file", "present")
