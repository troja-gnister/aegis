"""The private foreground Podman API service and its exact process identity."""
from __future__ import annotations

import contextlib
import http.client
import os
import signal
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scripts.podman_dev.records import ApiRecord, PathIdentity, SessionError
from scripts.podman_dev.session import LOG_NAME

PROC = Path("/proc")
SOCKET_NAME = "podman.sock"
LOG_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW


@dataclass(frozen=True)
class ProcessIdentity:
    start_time: int
    command: tuple[str, ...]


def read_process_identity(pid: int, proc: Path = PROC) -> ProcessIdentity | None:
    """Start time and command of a live process, or None once it has exited."""
    try:
        status = (proc / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
        raw_command = (proc / str(pid) / "cmdline").read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return None
    # The command name may contain spaces or parentheses; fields resume after the
    # last ")". Field 3 (state) is index 0 and field 22 (start time) is index 19.
    fields = status[status.rindex(")") + 2 :].split()
    if fields[0] in ("Z", "X"):
        return None
    command = tuple(
        part.decode("utf-8", "surrogateescape") for part in raw_command.split(b"\0") if part
    )
    return ProcessIdentity(int(fields[19]), command)


def recorded_identity(record: ApiRecord) -> ProcessIdentity:
    return ProcessIdentity(record.start_time, tuple(record.command))


def process_matches(record: ApiRecord, proc: Path = PROC) -> bool:
    return read_process_identity(record.pid, proc) == recorded_identity(record)


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = str(path)

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(self._socket_path)
        self.sock = connection


def ping(path: Path, timeout: float = 2.0) -> bool:
    connection = _UnixHTTPConnection(path, timeout)
    try:
        connection.request("GET", "/_ping")
        response = connection.getresponse()
        return response.status == 200 and response.read().strip() == b"OK"
    except OSError:
        return False
    finally:
        connection.close()


def launch_api(
    state: Path, *, popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    proc: Path = PROC,
) -> ApiRecord:
    """Start the service and return its identity at once, before it is ready."""
    directory = Path(tempfile.mkdtemp(prefix="api-", dir=state))
    socket_path = directory / SOCKET_NAME
    command = [
        "podman", "--remote=false", "system", "service", "--time=0", f"unix://{socket_path}",
    ]
    log = os.open(state / LOG_NAME, LOG_FLAGS, 0o600)
    try:
        process = popen(
            command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, close_fds=True,
        )
    except OSError as error:
        directory.rmdir()
        raise SessionError(f"could not start the Podman API service: {error}") from error
    finally:
        os.close(log)
    identity = read_process_identity(process.pid, proc)
    if identity is None:
        directory.rmdir()
        raise SessionError(f"the Podman API service exited at once; see {state / LOG_NAME}")
    return ApiRecord(
        pid=process.pid, start_time=identity.start_time, command=list(identity.command),
        socket=str(socket_path), directory_identity=PathIdentity.of(directory),
    )


def wait_until_ready(
    record: ApiRecord, *, deadline_seconds: float = 30.0, proc: Path = PROC,
    probe: Callable[[Path], bool] = ping, sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> PathIdentity:
    socket_path = Path(record.socket)
    end = now() + deadline_seconds
    while True:
        if not process_matches(record, proc):
            raise SessionError(
                "the Podman API service exited during startup; see its log, then run stop"
            )
        if socket_path.exists() and probe(socket_path):
            return PathIdentity.of(socket_path)
        if now() >= end:
            raise SessionError(
                f"the Podman API service was not ready within {deadline_seconds:g} s; run stop"
            )
        sleep(0.2)


def stop_api(
    record: ApiRecord, *, deadline_seconds: float = 20.0, proc: Path = PROC,
    kill: Callable[[int, int], None] = os.kill, sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> None:
    expected = recorded_identity(record)
    current = read_process_identity(record.pid, proc)
    if current is not None:
        if current != expected:
            raise SessionError(
                f"process {record.pid} no longer matches the recorded Podman API service; "
                "nothing was signalled"
            )
        with contextlib.suppress(ProcessLookupError):
            kill(record.pid, signal.SIGTERM)
        end = now() + deadline_seconds
        while read_process_identity(record.pid, proc) == expected:
            if now() >= end:
                raise SessionError(
                    f"the Podman API service ignored SIGTERM for {deadline_seconds:g} s; "
                    "it was left running"
                )
            sleep(0.2)
    _remove_socket_entries(record)


def _remove_socket_entries(record: ApiRecord) -> None:
    socket_path = Path(record.socket)
    directory = socket_path.parent
    if os.path.lexists(socket_path):
        recorded = record.socket_identity
        if recorded is None or PathIdentity.of(socket_path) != recorded:
            raise SessionError(f"{socket_path} is not the recorded socket; left in place")
        socket_path.unlink()
    if os.path.lexists(directory):
        if PathIdentity.of(directory) != record.directory_identity:
            raise SessionError(
                f"{directory} is not the recorded socket directory; left in place"
            )
        if any(directory.iterdir()):
            raise SessionError(f"{directory} has unexpected contents; left in place")
        directory.rmdir()
