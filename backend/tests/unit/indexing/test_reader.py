"""Portable descriptor tests inject Linux topology only; deployment tests use real procfs."""

import io
import os
import sys
from collections.abc import Generator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from aegis_apps.catalog.domain import DirectoryIdentity
from aegis_apps.indexing import reader
from aegis_apps.indexing.protocol import (
    ReaderBatch,
    ReaderComplete,
    ReaderFailure,
    ReaderMessage,
    encode_message,
)


@pytest.fixture
def topology(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader, "_mount_snapshot", lambda fd: ("synthetic", 10, "fixed"))
    monkeypatch.setattr(reader, "_mount_id", lambda fd: 10)


def scan(root: Path, components: tuple[bytes, ...] = (), count: int = 100) -> list[ReaderMessage]:
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return list(reader.read_directory(descriptor, components, count))
    finally:
        os.close(descriptor)


def test_descriptor_relative_directory_and_raw_names(tmp_path: Path, topology: None) -> None:
    (tmp_path / "child").mkdir()
    (tmp_path / "child" / "keep.txt").write_bytes(b"preserve exactly")
    (tmp_path / "child" / "link").symlink_to("../../outside")
    messages = scan(tmp_path, (b"child",))
    assert isinstance(messages[-1], ReaderComplete)
    entries = {entry.name.raw: entry for batch in messages if isinstance(batch, ReaderBatch)
               for entry in batch.observations}
    assert set(entries) == {b"keep.txt", b"link"}
    assert entries[b"keep.txt"].size == 16
    assert entries[b"link"].kind == "symlink"


@pytest.mark.parametrize("components", [(b"..",), (b"/etc",), (b"a/b",), (b"x",) * 257])
def test_rejects_invalid_or_unbounded_component_chain(
    tmp_path: Path, topology: None, components: tuple[bytes, ...],
) -> None:
    assert scan(tmp_path, components) == [ReaderFailure("reader_protocol_error")]


def test_4096_raw_component_bytes_pass_validation(tmp_path: Path, topology: None) -> None:
    # Exactly 17 components and 4,096 raw bytes; separators are not input bytes.
    components = (b"x" * 255,) * 16 + (b"y" * 16,)
    # The first child does not exist. Reaching traversal gives source_unavailable,
    # while incorrect byte validation would return reader_protocol_error.
    assert scan(tmp_path, components) == [ReaderFailure("source_unavailable")]


def test_4097_raw_component_bytes_fail_validation(tmp_path: Path, topology: None) -> None:
    components = (b"x" * 255,) * 16 + (b"y" * 17,)
    assert scan(tmp_path, components) == [ReaderFailure("reader_protocol_error")]


@pytest.mark.parametrize("count", [0, 99, 2001, True])
def test_rejects_out_of_range_batch_size(tmp_path: Path, topology: None, count: int) -> None:
    assert scan(tmp_path, count=count) == [ReaderFailure("reader_protocol_error")]


def test_never_follows_ancestor_symlink(tmp_path: Path, topology: None) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to("real")
    messages = scan(tmp_path, (b"link",))
    assert messages == [ReaderFailure("source_unavailable")]


def test_replaced_directory_cannot_complete_from_old_descriptor(
    tmp_path: Path, topology: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = tmp_path / "child"
    child.mkdir()
    for number in range(101):
        (child / str(number)).touch()
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        iterator = reader.read_directory(fd, (b"child",), 100)
        assert isinstance(next(iterator), ReaderBatch)
        original_identity = reader.directory_identity
        before = child.stat()
        child.rename(tmp_path / "moved")
        child.mkdir()
        # Model a filesystem where the held directory's timestamps do not expose
        # rename. Only reopening the physical component chain catches replacement.
        def stable_old_identity(descriptor: int) -> DirectoryIdentity:
            identity = original_identity(descriptor)
            if identity.inode == before.st_ino:
                return DirectoryIdentity(before.st_dev, before.st_ino,
                                         before.st_mtime_ns, before.st_ctime_ns)
            return identity
        monkeypatch.setattr(reader, "directory_identity", stable_old_identity)
        assert list(iterator)[-1] == ReaderFailure("identity_changed")
    finally:
        os.close(fd)


def test_same_device_different_mount_id_fails_closed(
    tmp_path: Path, topology: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "child").mkdir()
    child_inode = (tmp_path / "child").stat().st_ino
    monkeypatch.setattr(reader, "_mount_id", lambda fd: (
        11 if os.fstat(fd).st_ino == child_inode else 10
    ))
    assert scan(tmp_path, (b"child",)) == [ReaderFailure("source_unavailable")]


def test_metadata_failure_emits_inaccessible_and_never_completes(
    tmp_path: Path, topology: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Entry:
        name = "denied"

        def stat(self, *, follow_symlinks: bool) -> os.stat_result:
            assert follow_symlinks is False
            raise PermissionError("private /original/path")

    class Entries:
        def __enter__(self) -> Iterator[Entry]:
            return iter([Entry()])

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr(os, "scandir", lambda fd: Entries())
    messages = scan(tmp_path)
    assert messages[-1] == ReaderFailure("permission_denied")
    assert isinstance(messages[0], ReaderBatch)
    entry = messages[0].observations[0]
    assert entry.name.raw == b"denied"
    assert entry.state == "inaccessible"
    assert (entry.size, entry.mtime_ns, entry.ctime_ns, entry.device, entry.inode) == (None,) * 5


def test_50000_lazy_entries_respect_byte_record_and_memory_bounds(
    tmp_path: Path, topology: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tracemalloc

    consumed = 0
    # Max-width, valid integer metadata makes 2,000 records exceed the byte cap.
    metadata = cast(os.stat_result, SimpleNamespace(
        st_mode=tmp_path.stat().st_mode, st_size=9223372036854775807,
        st_mtime_ns=-9223372036854775808, st_ctime_ns=9223372036854775807,
        st_dev=18446744073709551615, st_ino=18446744073709551615,
    ))

    class Entry:
        name = "x" * 250

        def stat(self, *, follow_symlinks: bool) -> os.stat_result:
            return metadata

    class Entries:
        def __enter__(self) -> Iterator[Entry]:
            def generate() -> Iterator[Entry]:
                nonlocal consumed
                for number in range(50000):
                    consumed += 1
                    entry = Entry()
                    entry.name = f"{number:05}" + "x" * 250
                    yield entry
            return generate()

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr(os, "scandir", lambda fd: Entries())
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    tracemalloc.start()
    total = batches = 0
    try:
        for message in reader.read_directory(fd, (), 2000):
            if isinstance(message, ReaderBatch):
                batches += 1
                total += len(message.observations)
                assert consumed <= total + 1
                assert message.sequence == batches
                assert len(message.observations) <= 2000
                assert len(encode_message(message)) <= 1048576
            else:
                assert isinstance(message, ReaderComplete)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        os.close(fd)
    assert total == 50000
    assert batches > 25  # The byte cap must flush before the 2,000-record cap.
    assert peak < 16 * 1024 * 1024
    print(f"50K observations: batches={batches}, tracemalloc_peak={peak} bytes")


def test_non_linux_fails_closed_without_topology_substitute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert scan(tmp_path) == [ReaderFailure("source_unavailable")]


def test_reusing_caller_fd_after_completion_and_cancel_restarts_directory(
    tmp_path: Path, topology: None,
) -> None:
    for number in range(105):
        (tmp_path / str(number)).touch()
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(3):
            messages = list(reader.read_directory(fd, (), 100))
            assert isinstance(messages[-1], ReaderComplete)
            assert {item.name.raw for message in messages if isinstance(message, ReaderBatch)
                    for item in message.observations} == {str(n).encode() for n in range(105)}
            iterator = cast(Generator[ReaderMessage], reader.read_directory(fd, (), 100))
            assert isinstance(next(iterator), ReaderBatch)
            iterator.close()
            os.fstat(fd)
    finally:
        os.close(fd)


@pytest.mark.parametrize("failure", ["invalid_name", "oversized", "missing"])
def test_invalid_or_unavailable_entry_degrades_terminal(
    tmp_path: Path, topology: None, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    class Entry:
        name = "x" * 256 if failure == "invalid_name" else "x"

        def stat(self, *, follow_symlinks: bool) -> os.stat_result:
            if failure == "missing":
                raise FileNotFoundError("private source")
            return tmp_path.stat()

    class Entries:
        def __enter__(self) -> Iterator[Entry]:
            return iter([Entry()])

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr(os, "scandir", lambda fd: Entries())
    if failure == "oversized":
        monkeypatch.setattr(reader, "observation_encoded_size", lambda item: 1048577)
    messages = scan(tmp_path)
    assert messages[-1] == ReaderFailure(
        "source_unavailable" if failure == "missing" else "unsupported_entry",
    )
    if failure == "missing":
        assert isinstance(messages[0], ReaderBatch)
        assert messages[0].observations[0].state == "inaccessible"


@pytest.mark.parametrize("raw", [b"", b"mnt_id: x\n", b"mnt_id: 1\nmnt_id: 2\n",
                                  b"mnt_id: 111111111111111111111\n"])
def test_missing_ambiguous_or_unbounded_mount_id_is_rejected(
    monkeypatch: pytest.MonkeyPatch, raw: bytes,
) -> None:
    monkeypatch.setattr(reader, "_proc_read", lambda path, limit: raw)
    with pytest.raises(ValueError):
        reader._mount_id(10)


def test_proc_metadata_read_is_bounded(tmp_path: Path) -> None:
    fixture = tmp_path / "owned-mount-metadata"
    fixture.write_bytes(b"x" * 4097)
    with pytest.raises(ValueError, match="exceeds limit"):
        reader._proc_read(str(fixture), 4096)


def test_short_proc_reads_cannot_hide_tail_of_mount_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Fragmented(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            assert size is not None
            return super().read(min(size, 2))

    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: Fragmented(b"x" * 4097))
    with pytest.raises(ValueError, match="exceeds limit"):
        reader._proc_read("/proc/self/fdinfo/10", 4096)


def test_reader_preserves_byte_irregular_names_through_protocol(
    tmp_path: Path, topology: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aegis_apps.indexing.protocol import decode_message

    class Entry:
        name = os.fsdecode(b"odd\xff\\line\n")

        def stat(self, *, follow_symlinks: bool) -> os.stat_result:
            return tmp_path.stat()

    class Entries:
        def __enter__(self) -> Iterator[Entry]:
            return iter([Entry()])

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr(os, "scandir", lambda fd: Entries())
    messages = scan(tmp_path)
    assert isinstance(messages[-1], ReaderComplete)
    decoded = decode_message(encode_message(messages[0]))
    assert isinstance(decoded, ReaderBatch)
    assert decoded.observations[0].name.raw == b"odd\xff\\line\n"
    assert decoded.observations[0].name.display == r"odd\xff\\line\u000a"
