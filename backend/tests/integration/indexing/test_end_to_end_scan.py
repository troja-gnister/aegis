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
