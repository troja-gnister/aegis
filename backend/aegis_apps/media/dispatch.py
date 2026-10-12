"""The worker's side of the scratch volume: job files out, untrusted results in (spec M2).

The media worker and the processor share only the scratch volume (``protocol.py`` describes
the layout), and the worker treats all of it as hostile, directories included: a compromised
processor can rename, chmod or replace anything there.

- **Held directories.** :meth:`Scratch.prepare` opens the scratch root, ``inbox/`` and
  ``done/`` once with ``O_DIRECTORY|O_NOFOLLOW`` and keeps the descriptors. Every operation is
  relative to them (``dir_fd=`` for open, link, unlink, rmdir and stat; ``scandir`` on a
  descriptor), so a swapped name never redirects the worker. :meth:`Scratch.verify`, run on
  every pass, requires each name to still resolve (without following links) to the same
  device and inode as the held descriptor; a swap makes the scratch unavailable until it is
  prepared again. The worker never touches ``work/`` or ``rejected/``, so it holds neither.
- **Jobs** are written into ``inbox/`` as an ``O_EXCL`` temporary file, then linked to their
  final name, which never replaces a queued job. Job ids are 128 random bits, never reused.
- **Results** in ``done/`` are bound to the worker's own in-memory job, never to anything
  read back from scratch. :meth:`Scratch.collect`:

  - opens ``done/<id>`` with ``O_DIRECTORY|O_NOFOLLOW|O_NONBLOCK`` (a symlink, FIFO or file is
    refused without blocking) and lists at most a few names: exactly ``result.json`` plus the
    outputs ``protocol.job_outputs`` derives from the dispatched operation and kind (a
    backfill's are its rendition's, and its ``ok`` result must carry metadata, like a probe's);
  - reads ``result.json`` from a descriptor (regular file, one link, at most
    ``MAX_RESULT_BYTES``) and decodes it with ``protocol.decode_result`` against the
    dispatched job, then checks the id, the output names and the metadata again here;
  - for every output, by the derived name only: ``lstat`` (a regular file, not a symlink or
    FIFO, ``st_nlink == 1``, the declared size, within the job's output bound), then an
    ``O_NOFOLLOW|O_NONBLOCK`` open whose ``fstat`` must show the same inode, then the
    signature (``RIFF....WEBP`` with a RIFF size equal to the file size, or an MP4 ``ftyp``
    box at offset 4) and the dimensions: the declared ones must fit the kind's bounds and, for
    WebP, equal the canvas size in the ``VP8X``/``VP8 ``/``VP8L`` header. Reading those few
    header bytes is not decoding; the worker never decodes media.

  The verified descriptors are handed to publishing, which copies from them into its own
  ``O_EXCL`` file in the derivatives tree (never renaming or linking a scratch inode).
- **Cleanup** removes every ``done/`` entry it has handled, and every entry it does not know
  (late, forged or left by an earlier worker). It never follows a symlink: it unlinks
  anything that is not a directory, and descends only into directories it can open with
  ``O_NOFOLLOW`` whose inode matches what it saw, giving itself access with ``fchmod`` on
  that descriptor. Depth and entry counts are bounded.

One worker owns a scratch volume: at startup it empties ``inbox/`` (its predecessor's jobs)
and ``done/`` (results nobody is waiting for).
"""

from __future__ import annotations

import itertools
import os
import secrets
import stat
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Final

from aegis_media_processor import protocol

from .models import MAX_DERIVATIVE_BYTES

SCRATCH_DIRECTORY_MODE: Final = 0o770
MAX_DONE_SCAN: Final = 1_024
HEADER_BYTES: Final = 32
# The smallest file whose header the checks below can read.
MIN_OUTPUT_BYTES: Final[Mapping[str, int]] = {"webp": 30, "mp4": 16}
# The most names a result directory may hold: result.json plus two outputs.
MAX_RESULT_NAMES: Final = 3
_DIRECTORY_FLAGS: Final = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
)
_PATH_FLAGS: Final = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOCTTY
_CREATE_FLAGS: Final = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOCTTY
)
JOB_FILE_MODE: Final = 0o660
_REMOVE_DEPTH: Final = 16
_REMOVE_BUDGET: Final = 4_096
_VP8_START_CODE: Final = b"\x9d\x01\x2a"


class ScratchUnavailable(RuntimeError):
    """The scratch directories are missing or are not plain directories."""


class ResultRejected(ValueError):
    """A ``done/`` entry failed validation. ``reason`` is a fixed, non-identifying word."""

    def __init__(self, reason: str) -> None:
        super().__init__("media_result_rejected")
        self.reason = reason


@dataclass(slots=True)
class VerifiedOutput:
    """One validated output; ``descriptor`` is open read-only until :meth:`close`."""

    name: str
    format: protocol.OutputFormat
    bytes: int
    width: int
    height: int
    descriptor: int

    def close(self) -> None:
        if self.descriptor >= 0:
            with suppress(OSError):
                os.close(self.descriptor)
            self.descriptor = -1


@dataclass(slots=True)
class CollectedResult:
    result: protocol.Result
    outputs: tuple[VerifiedOutput, ...]

    def close(self) -> None:
        for output in self.outputs:
            output.close()

    def __enter__(self) -> CollectedResult:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def webp_dimensions(head: bytes) -> tuple[int, int] | None:
    """The canvas size in a WebP file's first 30 bytes, or ``None`` if it is not readable."""
    if len(head) < 30 or head[:4] != b"RIFF" or head[8:12] != b"WEBP":
        return None
    chunk = head[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(head[24:27], "little") + 1
        height = int.from_bytes(head[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 ":
        if head[23:26] != _VP8_START_CODE:
            return None
        width = int.from_bytes(head[26:28], "little") & 0x3FFF
        height = int.from_bytes(head[28:30], "little") & 0x3FFF
        return (width, height) if width and height else None
    if chunk == b"VP8L":
        if head[20] != 0x2F:
            return None
        bits = int.from_bytes(head[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def check_header(
    output_format: protocol.OutputFormat, head: bytes, *, size: int, width: int, height: int,
) -> bool:
    """Whether a file's first bytes match its format and declared size and dimensions."""
    if not protocol.output_magic_matches(output_format, head[:12]):
        return False
    if output_format == "webp":
        riff_size = int.from_bytes(head[4:8], "little")
        return riff_size + 8 == size and webp_dimensions(head) == (width, height)
    box = int.from_bytes(head[0:4], "big")
    return 16 <= box <= size


def _same(observed: os.stat_result, held: os.stat_result) -> bool:
    return (observed.st_dev, observed.st_ino) == (held.st_dev, held.st_ino)


def _open_directory_at(parent: int, name: str, observed: os.stat_result) -> int | None:
    """Open ``name`` (seen as ``observed``) as a directory, never through a symlink.

    A directory without owner read or search access gets it first, through an ``O_PATH``
    descriptor of that exact inode (``chmod`` on its ``/proc/self/fd`` link, which names the
    held inode and never resolves the name again); Linux has no ``fchmod`` on ``O_PATH``.
    """
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    except PermissionError:
        try:
            handle = os.open(name, _PATH_FLAGS, dir_fd=parent)
        except OSError:
            return None
        try:
            if not _same(os.fstat(handle), observed):
                return None
            os.chmod(f"/proc/self/fd/{handle}", 0o700)
        except OSError:
            return None
        finally:
            os.close(handle)
        try:
            descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
        except OSError:
            return None
    except OSError:
        return None
    try:
        if _same(os.fstat(descriptor), observed):
            return descriptor
    except OSError:
        pass
    os.close(descriptor)
    return None


def remove_at(parent: int, name: str, *, depth: int = 0, budget: list[int] | None = None) -> bool:
    """Remove ``name`` beneath the directory ``parent`` without following symlinks.

    Anything but a directory is unlinked; a directory is entered only through an
    ``O_NOFOLLOW`` descriptor of the inode that was seen, given owner access with ``fchmod``,
    emptied and removed. Depth and the number of entries are bounded; never raises.
    """
    remaining = [_REMOVE_BUDGET] if budget is None else budget
    if remaining[0] <= 0 or not name or "/" in name or name in (".", ".."):
        return False
    remaining[0] -= 1
    try:
        observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not stat.S_ISDIR(observed.st_mode):
        try:
            os.unlink(name, dir_fd=parent)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True
    if depth >= _REMOVE_DEPTH:
        return False
    directory = _open_directory_at(parent, name, observed)
    if directory is None:
        return False
    removed = True
    try:
        with suppress(OSError):
            os.fchmod(directory, 0o700)
        try:
            with os.scandir(directory) as entries:
                names = [entry.name for entry in itertools.islice(entries, _REMOVE_BUDGET)]
        except OSError:
            return False
        for child in names:
            removed = remove_at(directory, child, depth=depth + 1, budget=remaining) and removed
    finally:
        os.close(directory)
    if not removed:
        return False
    try:
        os.rmdir(name, dir_fd=parent)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


class Scratch:
    """The scratch volume as the worker sees it: held ``inbox/`` and ``done/`` descriptors.

    ``inbox`` and ``done`` are their paths, kept for diagnostics and tests only; no operation
    resolves them.
    """

    def __init__(self, root: str) -> None:
        if not isinstance(root, str) or not os.path.isabs(root) or "\0" in root:
            raise ValueError("the scratch directory must be an absolute path")
        self.root = os.path.normpath(root)
        self.inbox = os.path.join(self.root, protocol.INBOX)
        self.done = os.path.join(self.root, protocol.DONE)
        self._descriptors: dict[str, int] = {}

    @property
    def is_open(self) -> bool:
        return bool(self._descriptors)

    def close(self) -> None:
        for descriptor in self._descriptors.values():
            with suppress(OSError):
                os.close(descriptor)
        self._descriptors = {}

    def _held(self, name: str) -> int:
        descriptor = self._descriptors.get(name)
        if descriptor is None:
            raise ScratchUnavailable("the scratch directories are not open")
        return descriptor

    def prepare(self) -> None:
        """Open the scratch root, ``inbox/`` and ``done/`` (creating the two if missing)."""
        self.close()
        opened: dict[str, int] = {}
        try:
            opened["."] = os.open(self.root, _DIRECTORY_FLAGS)
            for name in (protocol.INBOX, protocol.DONE):
                try:
                    os.mkdir(name, SCRATCH_DIRECTORY_MODE, dir_fd=opened["."])
                    created = True
                except FileExistsError:
                    created = False
                opened[name] = os.open(name, _DIRECTORY_FLAGS, dir_fd=opened["."])
                if created:
                    # The processor shares these through the group, whatever our umask is.
                    os.fchmod(opened[name], SCRATCH_DIRECTORY_MODE)
        except OSError as error:
            for descriptor in opened.values():
                os.close(descriptor)
            raise ScratchUnavailable("the scratch directories are unavailable") from error
        self._descriptors = opened

    def verify(self) -> None:
        """Each name must still be the directory that is held (run on every pass)."""
        root = self._held(".")
        try:
            if not _same(os.stat(self.root, follow_symlinks=False), os.fstat(root)):
                raise ScratchUnavailable("the scratch root was replaced")
            for name in (protocol.INBOX, protocol.DONE):
                held = os.fstat(self._held(name))
                observed = os.stat(name, dir_fd=root, follow_symlinks=False)
                if not stat.S_ISDIR(observed.st_mode) or not _same(observed, held):
                    raise ScratchUnavailable("a scratch directory was replaced")
                if stat.S_IMODE(held.st_mode) & 0o700 != 0o700:
                    # Taken away by the processor: restore our own access, on the descriptor.
                    with suppress(OSError):
                        os.fchmod(self._held(name), SCRATCH_DIRECTORY_MODE)
        except OSError as error:
            self.close()
            raise ScratchUnavailable("the scratch directories are unavailable") from error
        except ScratchUnavailable:
            self.close()
            raise

    def clear_stale(self) -> int:
        """Remove an earlier worker's queued jobs and every unclaimed result (startup)."""
        removed = 0
        inbox = self._held(protocol.INBOX)
        try:
            with os.scandir(inbox) as entries:
                names = [entry.name for entry in itertools.islice(entries, _REMOVE_BUDGET)]
        except OSError:
            names = []
        for name in names:
            if protocol.job_id_from_filename(name) is not None or (
                name.startswith(".") and name.endswith(".tmp")
            ):
                with suppress(OSError):
                    os.unlink(name, dir_fd=inbox)
                    removed += 1
        for name in self.finished(_REMOVE_BUDGET):
            if self.discard(name):
                removed += 1
        return removed

    def submit(self, job: protocol.Job) -> None:
        """Queue a job: an ``O_EXCL`` temporary file, then a link that never replaces one.

        Raises ``OSError`` (``FileExistsError`` for a queued id), ``ProtocolError`` or
        ``ScratchUnavailable``.
        """
        data = protocol.encode_job(job)
        inbox = self._held(protocol.INBOX)
        filename = protocol.job_filename(job.id)
        temporary = f".{filename}.{secrets.token_hex(8)}.tmp"
        descriptor = os.open(temporary, _CREATE_FLAGS, JOB_FILE_MODE, dir_fd=inbox)
        try:
            try:
                os.fchmod(descriptor, JOB_FILE_MODE)
                view = memoryview(data)
                while view:
                    view = view[os.write(descriptor, view):]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.link(temporary, filename, src_dir_fd=inbox, dst_dir_fd=inbox,
                    follow_symlinks=False)
        finally:
            with suppress(OSError):
                os.unlink(temporary, dir_fd=inbox)

    def queued(self, job_id: str) -> bool:
        """Whether the processor has not claimed the job yet (``True`` when unknown)."""
        try:
            os.stat(protocol.job_filename(job_id), dir_fd=self._held(protocol.INBOX),
                    follow_symlinks=False)
        except FileNotFoundError:
            return False
        except (OSError, ScratchUnavailable):
            return True
        return True

    def withdraw(self, job_id: str) -> bool:
        """Remove a still-queued job; ``False`` if the processor already claimed it."""
        try:
            os.unlink(protocol.job_filename(job_id), dir_fd=self._held(protocol.INBOX))
        except (OSError, ScratchUnavailable):
            return False
        return True

    def finished(self, limit: int = MAX_DONE_SCAN) -> list[str]:
        """Names in ``done/`` (bounded); raises ``ScratchUnavailable`` if it is unreadable."""
        try:
            with os.scandir(self._held(protocol.DONE)) as entries:
                return [entry.name for entry in itertools.islice(entries, limit)]
        except OSError as error:
            raise ScratchUnavailable("the done directory is unreadable") from error

    def discard(self, name: str) -> bool:
        """Remove one ``done/`` entry without following symlinks; never raises."""
        try:
            done = self._held(protocol.DONE)
        except ScratchUnavailable:
            return False
        return remove_at(done, name)

    def collect(self, job: protocol.Job) -> CollectedResult:
        """Validate ``done/<job.id>`` against the dispatched job; raises ``ResultRejected``."""
        try:
            directory = os.open(job.id, _DIRECTORY_FLAGS, dir_fd=self._held(protocol.DONE))
        except (OSError, ScratchUnavailable):
            raise ResultRejected("unreadable") from None
        try:
            try:
                with os.scandir(directory) as entries:
                    listed = [
                        entry.name
                        for entry in itertools.islice(entries, MAX_RESULT_NAMES + 1)
                    ]
            except OSError:
                raise ResultRejected("unreadable") from None
            if len(listed) > MAX_RESULT_NAMES:
                raise ResultRejected("unexpected_files")
            names = set(listed)
            raw = _read_result(directory)
            try:
                result = protocol.decode_result(raw, job)
            except protocol.ProtocolError:
                raise ResultRejected("malformed") from None
            if result.id != job.id:
                raise ResultRejected("job_mismatch")
            specs = protocol.job_outputs(job.op, job.kind) if result.status == "ok" else ()
            if tuple(output.name for output in result.outputs) != tuple(
                spec.name for spec in specs
            ) or (result.metadata is not None) != (
                result.status == "ok" and job.op in protocol.METADATA_OPS
            ):
                raise ResultRejected("op_mismatch")
            if names != {protocol.RESULT_FILE, *(spec.filename for spec in specs)}:
                raise ResultRejected("unexpected_files")
            bounds = protocol.output_edge_bounds(job)
            verified: list[VerifiedOutput] = []
            try:
                for spec, output in zip(specs, result.outputs, strict=True):
                    verified.append(_verify_output(directory, spec, output, job, bounds))
            except BaseException:
                for item in verified:
                    item.close()
                raise
            return CollectedResult(result, tuple(verified))
        finally:
            os.close(directory)


def _read_result(directory: int) -> bytes:
    try:
        descriptor = os.open(protocol.RESULT_FILE, _READ_FLAGS, dir_fd=directory)
    except OSError:
        raise ResultRejected("unreadable") from None
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or not 2 <= observed.st_size <= protocol.MAX_RESULT_BYTES
        ):
            raise ResultRejected("malformed")
        data = bytearray()
        while len(data) <= protocol.MAX_RESULT_BYTES:
            chunk = os.pread(
                descriptor, protocol.MAX_RESULT_BYTES + 1 - len(data), len(data),
            )
            if not chunk:
                break
            data += chunk
    except OSError:
        raise ResultRejected("unreadable") from None
    finally:
        os.close(descriptor)
    if len(data) > protocol.MAX_RESULT_BYTES:
        raise ResultRejected("malformed")
    return bytes(data)


def _verify_output(
    directory: int,
    spec: protocol.OutputSpec,
    output: protocol.Output,
    job: protocol.Job,
    bounds: Mapping[str, tuple[int, int]],
) -> VerifiedOutput:
    if output.name != spec.name or output.format != spec.format:
        raise ResultRejected("op_mismatch")
    long_edge, short_edge = bounds[spec.name]
    if (
        min(output.width, output.height) < 1
        or max(output.width, output.height) > long_edge
        or min(output.width, output.height) > short_edge
    ):
        raise ResultRejected("dimensions")
    maximum = min(job.limits.output_bytes, MAX_DERIVATIVE_BYTES)
    try:
        observed = os.stat(spec.filename, dir_fd=directory, follow_symlinks=False)
    except OSError:
        raise ResultRejected("unreadable") from None
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
        raise ResultRejected("not_regular")
    if observed.st_nlink != 1:
        raise ResultRejected("hard_link")
    if observed.st_size != output.bytes:
        raise ResultRejected("size_mismatch")
    if not MIN_OUTPUT_BYTES[spec.format] <= observed.st_size <= maximum:
        raise ResultRejected("size_bounds")
    try:
        descriptor = os.open(spec.filename, _READ_FLAGS, dir_fd=directory)
    except OSError:
        raise ResultRejected("unreadable") from None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino)
            or opened.st_nlink != 1
            or opened.st_size != observed.st_size
        ):
            raise ResultRejected("not_regular")
        head = os.pread(descriptor, HEADER_BYTES, 0)
        if not check_header(
            spec.format, head, size=observed.st_size, width=output.width, height=output.height,
        ):
            raise ResultRejected("signature")
    except ResultRejected:
        os.close(descriptor)
        raise
    except OSError:
        os.close(descriptor)
        raise ResultRejected("unreadable") from None
    return VerifiedOutput(
        name=spec.name, format=spec.format, bytes=observed.st_size, width=output.width,
        height=output.height, descriptor=descriptor,
    )
