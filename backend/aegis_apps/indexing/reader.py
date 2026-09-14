"""Read-only descriptor-relative enumeration; no database or worker loop."""

import os
import stat
import sys
from collections.abc import Iterator
from pathlib import PurePosixPath

from aegisctl.mounts import MAX_MOUNTINFO_BYTES, MountAttestationError, parse_mountinfo

from aegis_apps.catalog.domain import DirectoryIdentity, EntryKind, Observation, SourceState
from aegis_apps.catalog.names import source_name

from .protocol import (
    MAX_PAYLOAD_BYTES,
    FailureCode,
    ProtocolError,
    ReaderBatch,
    ReaderComplete,
    ReaderFailure,
    ReaderMessage,
    batch_frame_overhead,
    observation_encoded_size,
)

DEFAULT_BATCH_RECORDS = 500
MAX_COMPONENTS = 256
MAX_COMPONENT_BYTES = 4096


def _proc_read(path: str, limit: int) -> bytes:
    """Only fixed procfs metadata paths are opened for reading, never source files."""
    result = bytearray()
    with open(path, "rb", buffering=0) as handle:
        while len(result) <= limit:
            chunk = handle.read(min(65536, limit + 1 - len(result)))
            if not chunk:
                break
            result.extend(chunk)
    if len(result) > limit:
        raise MountAttestationError("mount metadata exceeds limit")
    return bytes(result)


def _mount_snapshot(descriptor: int) -> tuple[str, int, str]:
    if sys.platform != "linux":
        raise MountAttestationError("Linux mount identity required")
    root = os.readlink(f"/proc/self/fd/{descriptor}")
    if len(root) > MAX_COMPONENT_BYTES or not root.startswith("/"):
        raise MountAttestationError("invalid root identity")
    raw = _proc_read("/proc/self/mountinfo", MAX_MOUNTINFO_BYTES)
    records = parse_mountinfo(raw)
    record = records.get(root)
    if record is None or record.effective_mode != "read_only" or any(
        PurePosixPath(root) in PurePosixPath(path).parents for path in records
    ):
        raise MountAttestationError("root is not a read-only leaf mount")
    mount_id = _mount_id(descriptor)
    # The shared parser validates topology/escaping. Bind the fd's mount ID to
    # that exact parsed record using the same bounded mountinfo line order.
    lines = raw.splitlines()
    for path, line in zip(records, lines, strict=True):
        if path == root:
            observed = line.split(b" ", 1)[0]
            if not observed.isdigit() or len(observed) > 20 or int(observed) != mount_id:
                raise MountAttestationError("root mount identity changed")
            break
    return root, mount_id, record.mount_fingerprint


def _mount_id(descriptor: int) -> int:
    raw = _proc_read(f"/proc/self/fdinfo/{descriptor}", 4096)
    values = [line.partition(b":")[2].strip() for line in raw.splitlines()
              if line.startswith(b"mnt_id:")]
    if len(values) != 1 or not values[0].isdigit() or len(values[0]) > 20:
        raise MountAttestationError("mount identity unavailable")
    return int(values[0])


def directory_identity(descriptor: int) -> DirectoryIdentity:
    value = os.fstat(descriptor)
    if not stat.S_ISDIR(value.st_mode):
        raise MountAttestationError("directory identity unavailable")
    return DirectoryIdentity(value.st_dev, value.st_ino, value.st_mtime_ns, value.st_ctime_ns)


def open_child_directory(parent_fd: int, raw: bytes) -> int:
    source_name(raw)
    return os.open(raw, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                   dir_fd=parent_fd)


def _walk(root_fd: int, components: tuple[bytes, ...], mount_id: int) -> int:
    # Opening '.' creates an independent directory offset, unlike dup(root_fd).
    descriptor = os.open(b".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=root_fd)
    try:
        if _mount_id(descriptor) != mount_id:
            raise MountAttestationError("mount identity changed")
        for raw in components:
            child = open_child_directory(descriptor, raw)
            os.close(descriptor)
            descriptor = child
            if _mount_id(descriptor) != mount_id:
                raise MountAttestationError("descendant mount is forbidden")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _failure(error: OSError) -> FailureCode:
    return "permission_denied" if isinstance(error, PermissionError) else "source_unavailable"


def _observe(entry: os.DirEntry[str]) -> tuple[Observation, FailureCode | None]:
    name = source_name(os.fsencode(entry.name))
    try:
        value = entry.stat(follow_symlinks=False)
    except OSError as error:
        return (Observation(name, EntryKind.SPECIAL, SourceState.INACCESSIBLE,
                            None, None, None, None, None), _failure(error))
    kind = (EntryKind.DIRECTORY if stat.S_ISDIR(value.st_mode) else
            EntryKind.FILE if stat.S_ISREG(value.st_mode) else
            EntryKind.SYMLINK if stat.S_ISLNK(value.st_mode) else EntryKind.SPECIAL)
    state = SourceState.UNSUPPORTED if kind == EntryKind.SPECIAL else SourceState.PRESENT
    return (Observation(name, kind, state, value.st_size, value.st_mtime_ns,
                        value.st_ctime_ns, value.st_dev, value.st_ino), None)


def read_directory(
    root_fd: int, components: tuple[bytes, ...], batch_records: int,
) -> Iterator[ReaderMessage]:
    """Enumerate one directory beneath an attested, caller-owned Linux root fd.

    Caller must close an abandoned iterator to release its descriptors. EOF is
    eventually consistent under trusted external writers, not an atomic snapshot.
    This generator does not enforce transport credits or the supervisor timeout.
    """
    try:
        if (type(root_fd) is not int or root_fd < 0 or type(components) is not tuple
                or len(components) > MAX_COMPONENTS or type(batch_records) is not int
                or not 100 <= batch_records <= 2000):
            raise ValueError
        total = 0
        for raw in components:
            source_name(raw)
            total += len(raw) + 1
            if total > MAX_COMPONENT_BYTES:
                raise ValueError
    except ValueError:
        yield ReaderFailure("reader_protocol_error")
        return
    descriptor = -1
    terminal: ReaderComplete | ReaderFailure
    try:
        topology = _mount_snapshot(root_fd)
        descriptor = _walk(root_fd, components, topology[1])
        before = directory_identity(descriptor)
        sequence = 1
        observations: list[Observation] = []
        byte_count = batch_frame_overhead(sequence)
        degraded: FailureCode | None = None
        with os.scandir(descriptor) as entries:
            for entry in entries:
                try:
                    observation, error = _observe(entry)
                    degraded = degraded or error
                    size = observation_encoded_size(observation)
                except (ValueError, ProtocolError):
                    degraded = degraded or "unsupported_entry"
                    continue
                if size + batch_frame_overhead(sequence) > MAX_PAYLOAD_BYTES:
                    degraded = degraded or "unsupported_entry"
                    continue
                if observations and (
                    len(observations) == batch_records
                    or byte_count + 1 + size > MAX_PAYLOAD_BYTES
                ):
                    yield ReaderBatch(sequence, tuple(observations))
                    sequence += 1
                    observations.clear()
                    byte_count = batch_frame_overhead(sequence)
                byte_count += size + bool(observations)
                observations.append(observation)
        if observations:
            yield ReaderBatch(sequence, tuple(observations))
        after = directory_identity(descriptor)
        current = _walk(root_fd, components, topology[1])
        try:
            reopened = directory_identity(current)
        finally:
            os.close(current)
        if before != after or after != reopened:
            terminal = ReaderFailure("identity_changed")
        elif topology != _mount_snapshot(root_fd):
            terminal = ReaderFailure("source_unavailable")
        elif degraded:
            terminal = ReaderFailure(degraded)
        else:
            terminal = ReaderComplete(after)
    except OSError as error:
        terminal = ReaderFailure(_failure(error))
    except (MountAttestationError, ValueError):
        terminal = ReaderFailure("source_unavailable")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    yield terminal
