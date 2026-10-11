"""Open one original by validated components, then prove its fingerprint (M2).

The processor loop (never the decoding child) calls this. The input is
``<roots>/<slot>/<relative path>``. The slot must match the manifest grammar and the path must
split into plain components (no ``..``, no absolute path, no NUL). Each directory is opened
relative to its parent with ``O_NOFOLLOW | O_DIRECTORY``, and the file as a path only
(``O_PATH | O_NOFOLLOW``), so no symlink is ever followed and a device node, FIFO or socket is
never opened for reading. The fingerprint (device, inode, size, mtime) is checked with
``fstat`` on that descriptor, the file is reopened read-only through the loop's own
``/proc/self/fd/<n>``, and the reopened descriptor is checked again.

Directories are not compared by device: btrfs subvolumes report their own ``st_dev`` while
staying on the same mount, so such a check would answer "changed" forever for files the
indexer cataloged there. Mount topology is attested when the roots are bound, and the final
descriptor must match the cataloged device, inode, size and mtime exactly.

The loop passes only that descriptor to the child. The child, confined by Landlock, cannot
resolve any root path: Pillow reads the descriptor directly, and ffmpeg and ffprobe read it
through ffmpeg's ``fd:`` protocol (``-fd <n> -i fd:``), which is seekable for regular files and
involves no path. A rename or swap after the check cannot substitute another file.
"""

from __future__ import annotations

import errno
import os
import stat
from typing import Final

from .protocol import DEFAULT_ROOTS_DIR, SLOT_ID_RE, ProtocolError, SourceInput, split_relative_path

_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# The final component is first opened as a path only: a device, FIFO or socket is never
# opened for reading, and a symlink is seen as itself.
_PATH_FLAGS: Final = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_NOCTTY
# The path no longer names a fingerprinted regular file: gone, a symlink, not a directory.
_CHANGED_ERRORS: Final = frozenset({
    errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EISDIR, errno.ENXIO, errno.EOPNOTSUPP,
})


class InputRefused(Exception):
    """The input is malformed; nothing was opened."""


class SourceChanged(Exception):
    """The path no longer names the fingerprinted regular file."""


class SourceUnavailable(Exception):
    """The root or a directory on the path cannot be read."""


def _open_error(error: OSError) -> Exception:
    if error.errno in _CHANGED_ERRORS:
        return SourceChanged()
    return SourceUnavailable()


def open_source(source: SourceInput, roots_dir: str = DEFAULT_ROOTS_DIR) -> int:
    """Return a read-only descriptor of the fingerprinted original; the caller closes it."""
    if type(source) is not SourceInput or type(source.slot) is not str \
            or SLOT_ID_RE.fullmatch(source.slot) is None:
        raise InputRefused
    try:
        components = split_relative_path(source.path)
    except ProtocolError:
        raise InputRefused from None
    if not isinstance(roots_dir, str) or not os.path.isabs(roots_dir) or "\0" in roots_dir:
        raise InputRefused
    try:
        directory = os.open(os.path.join(roots_dir, source.slot), _DIRECTORY_FLAGS)
    except OSError:
        raise SourceUnavailable from None
    try:
        for component in components[:-1]:
            try:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=directory)
            except OSError as error:
                raise _open_error(error) from None
            os.close(directory)
            directory = child
        try:
            located = os.open(components[-1], _PATH_FLAGS, dir_fd=directory)
        except OSError as error:
            raise _open_error(error) from None
    finally:
        os.close(directory)
    try:
        expected = (source.device, source.inode, source.size, source.mtime_ns)
        if _identity(located) != (stat.S_IFREG, *expected):
            raise SourceChanged
        try:
            descriptor = os.open(descriptor_path(located), _FILE_FLAGS)
        except OSError as error:
            raise _open_error(error) from None
    finally:
        os.close(located)
    try:
        # The reopened descriptor is the one decoders read; prove it is the same file.
        if _identity(descriptor) != (stat.S_IFREG, *expected):
            raise SourceChanged
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def matches(descriptor: int, source: SourceInput) -> bool:
    """Whether an open descriptor is still the fingerprinted regular file."""
    try:
        return _identity(descriptor) == (
            stat.S_IFREG, source.device, source.inode, source.size, source.mtime_ns,
        )
    except OSError:
        return False


def _identity(descriptor: int) -> tuple[int, int, int, int, int]:
    observed = os.fstat(descriptor)
    return (stat.S_IFMT(observed.st_mode), observed.st_dev, observed.st_ino, observed.st_size,
            observed.st_mtime_ns)


def descriptor_path(descriptor: int) -> str:
    """The path a tool opens to read an inherited descriptor, never the original path."""
    if type(descriptor) is not int or descriptor < 0:
        raise ValueError("invalid descriptor")
    return f"/proc/self/fd/{descriptor}"
