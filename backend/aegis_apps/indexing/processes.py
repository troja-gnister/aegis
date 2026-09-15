"""Linux reader transport and local lock ownership; no Django imports."""

from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import stat
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import BinaryIO, cast
from uuid import UUID

from aegisctl.mounts import MAX_MOUNTINFO_BYTES, parse_mountinfo

from .protocol import ProtocolError, ReaderBatch, ReaderMessage, read_message

COORDINATION_PATH = Path("/srv/aegis/indexer-coordination")
_LOCK_NAME = re.compile(r"(?:deployment|root-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.lock")


class CoordinationError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("scan coordination unavailable")


class CoordinationBusy(CoordinationError):
    pass


@dataclass(slots=True)
class ScanLock:
    fd: int

    @classmethod
    def acquire(cls, directory: int, name: str) -> ScanLock:
        if not _LOCK_NAME.fullmatch(name):
            raise CoordinationError
        descriptor = -1
        try:
            descriptor = os.open(
                name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=directory,
            )
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_dev != os.fstat(directory).st_dev):
                raise CoordinationError
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CoordinationBusy from None
            return cls(descriptor)
        except (OSError, CoordinationError) as error:
            if descriptor >= 0:
                os.close(descriptor)
            if isinstance(error, CoordinationBusy):
                raise
            raise CoordinationError from None

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        # Never LOCK_UN: an inherited open file description may still own source IO.


class Coordination:
    def __init__(self) -> None:
        self.directory = -1
        try:
            with open("/proc/self/mountinfo", "rb") as mountinfo:
                record = parse_mountinfo(mountinfo.read(MAX_MOUNTINFO_BYTES + 1)).get(
                    str(COORDINATION_PATH),
                )
            if record is None or record.effective_mode != "read_write":
                raise CoordinationError
            if record.filesystem_identity[1] not in {"ext4", "xfs", "btrfs", "tmpfs", "zfs"}:
                raise CoordinationError
            self.directory = os.open(
                COORDINATION_PATH, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            if os.fstat(self.directory).st_uid != os.geteuid():
                raise CoordinationError
            self.deployment = ScanLock.acquire(self.directory, "deployment.lock")
        except (OSError, ValueError, CoordinationError):
            if self.directory >= 0:
                os.close(self.directory)
            raise CoordinationError from None

    def root(self, root_id: UUID) -> ScanLock:
        if type(root_id) is not UUID:
            raise CoordinationError
        return ScanLock.acquire(self.directory, f"root-{root_id}.lock")

    def close(self) -> None:
        self.deployment.close()
        if self.directory >= 0:
            os.close(self.directory)
            self.directory = -1


@dataclass(frozen=True, slots=True)
class ReaderSource:
    path: str
    fingerprint: str
    components: tuple[bytes, ...]
    batch_records: int


class ProcessReader:
    """Two sender credits bound pipe + decoding + queue, including partial frames."""

    def __init__(self, source: ReaderSource, coordination: Coordination, root_id: UUID) -> None:
        self._lock = coordination.root(root_id)
        self._closed = False
        self._stopped = Event()
        self._queue: Queue[ReaderMessage | ProtocolError] = Queue(maxsize=2)
        command = [
            sys.executable, "-m", "aegis_apps.indexing.reader",
            "--root", source.path, "--fingerprint", source.fingerprint,
            "--parent", str(os.getpid()), "--batch", str(source.batch_records),
            "--components", json.dumps([base64.b64encode(raw).decode("ascii")
                                         for raw in source.components]),
        ]
        environment = {
            "PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        }
        try:
            self.process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                close_fds=True, pass_fds=(coordination.deployment.fd, self._lock.fd),
                env=environment, bufsize=0,
            )
        except BaseException:
            self._lock.close()
            raise
        assert self.process.stdin is not None and self.process.stdout is not None
        os.set_blocking(self.process.stdin.fileno(), False)
        self._receiver = Thread(target=self._receive, name="scan-results", daemon=True)
        self._receiver.start()

    def _receive(self) -> None:
        assert self.process.stdout is not None

        def enqueue(message: ReaderMessage | ProtocolError) -> None:
            while not self._stopped.is_set():
                try:
                    self._queue.put(message, timeout=.1)
                    return
                except Full:
                    continue

        try:
            while True:
                message = read_message(cast(BinaryIO, self.process.stdout))
                enqueue(message)
                if self._stopped.is_set() or not isinstance(message, ReaderBatch):
                    return
        except (ProtocolError, OSError):
            enqueue(ProtocolError())
        finally:
            self.process.stdout.close()

    def receive(self) -> ReaderMessage | None:
        try:
            message = self._queue.get_nowait()
        except Empty:
            return None
        if isinstance(message, ProtocolError):
            raise message
        return message

    def acknowledge(self, sequence: int) -> None:
        assert self.process.stdin is not None
        if self._closed or self.process.stdin.write(struct.pack("!Q", sequence)) != 8:
            raise ProtocolError

    def close_credits(self) -> None:
        if not self._closed:
            self._closed = True
            self._stopped.set()
            assert self.process.stdin is not None
            self.process.stdin.close()

    def terminate(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()

    def kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()

    def reaped(self) -> bool:
        if self.process.poll() is None:
            return False
        self._lock.close()
        return True
