"""Bounded, read-only Darwin mount metadata and firmlink-aware directory paths."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import platform
from pathlib import Path

MAX_HOST_MOUNTS = 8192
MAX_DARWIN_PATH = 1024


class HostTopologyError(ValueError):
    """Native metadata is unavailable or cannot safely establish boundaries."""


class _Statfs(ctypes.Structure):
    # Darwin's 64-bit statfs ABI (sys/mount.h), on supported arm64 and x86_64.
    _fields_ = [
        ("bsize", ctypes.c_uint32), ("iosize", ctypes.c_int32),
        ("blocks", ctypes.c_uint64), ("bfree", ctypes.c_uint64),
        ("bavail", ctypes.c_uint64), ("files", ctypes.c_uint64),
        ("ffree", ctypes.c_uint64), ("fsid", ctypes.c_int32 * 2),
        ("owner", ctypes.c_uint32), ("type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32), ("fssubtype", ctypes.c_uint32),
        ("fstypename", ctypes.c_char * 16),
        ("mntonname", ctypes.c_char * MAX_DARWIN_PATH),
        ("mntfromname", ctypes.c_char * MAX_DARWIN_PATH),
        ("flags_ext", ctypes.c_uint32), ("reserved", ctypes.c_uint32 * 7),
    ]


def _native_path(raw: bytes) -> Path:
    if not raw or len(raw) >= MAX_DARWIN_PATH:
        raise HostTopologyError("host mount topology cannot be checked")
    path = Path(os.fsdecode(raw))
    if not path.is_absolute() or ".." in path.parts:
        raise HostTopologyError("host mount topology cannot be checked")
    return path


def darwin_mountpoints() -> tuple[Path, ...]:
    """Read cached getfsstat metadata; a full buffer may have been truncated."""
    architecture = platform.machine()
    if (architecture not in {"arm64", "x86_64"} or ctypes.sizeof(_Statfs) != 2168
            or _Statfs.mntonname.offset != 88):
        raise HostTopologyError("host mount topology cannot be checked")
    try:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        symbol = "getfsstat" if architecture == "arm64" else "getfsstat$INODE64"
        getfsstat = getattr(library, symbol)
        getfsstat.argtypes = [ctypes.POINTER(_Statfs), ctypes.c_int, ctypes.c_int]
        getfsstat.restype = ctypes.c_int
        count = getfsstat(None, 0, 2)  # MNT_NOWAIT: no filesystem refresh/traversal.
        if not 1 <= count < MAX_HOST_MOUNTS:
            raise HostTopologyError("host mount topology cannot be checked")
        records = (_Statfs * (count + 1))()
        observed = getfsstat(records, ctypes.sizeof(records), 2)
        # Reject both truncation and a table changing between the two calls.
        if observed != count:
            raise HostTopologyError("host mount topology cannot be checked")
        return tuple(_native_path(records[index].mntonname) for index in range(observed))
    except (OSError, AttributeError) as exc:
        raise HostTopologyError("host mount topology cannot be checked") from exc


def darwin_path_forms(path: Path) -> frozenset[Path]:
    """Map an existing directory ancestor through both native firmlink forms.

    Only directory descriptors are opened. File contents and directory entries
    are never read; missing destination components are appended after lookup.
    """
    descriptor = -1
    suffix: tuple[str, ...] = ()
    try:
        ancestor = path.resolve(strict=False)
        while True:
            try:
                descriptor = os.open(
                    ancestor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                )
                break
            except OSError as exc:
                if exc.errno not in {errno.ENOENT, errno.ENOTDIR} or ancestor == ancestor.parent:
                    raise
                suffix = (ancestor.name, *suffix)
                ancestor = ancestor.parent
        forms: set[Path] = set()
        for command in (50, 102):  # F_GETPATH and F_GETPATH_NOFIRMLINK, sys/fcntl.h.
            raw = fcntl.fcntl(descriptor, command, bytes(MAX_DARWIN_PATH))
            if not isinstance(raw, bytes) or b"\0" not in raw:
                raise HostTopologyError("host mount topology cannot be checked")
            forms.add(_native_path(raw.split(b"\0", 1)[0]).joinpath(*suffix))
        return frozenset(forms)
    except OSError as exc:
        raise HostTopologyError("host mount topology cannot be checked") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
