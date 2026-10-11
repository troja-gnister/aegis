"""Bounded tool children: rlimits, Landlock, a wall-clock deadline, a scrubbed environment (M2).

Every child is started from an argument array (never a shell) in its own session, so a
deadline kills its process group. A process that leaves the group (``setsid``) is caught by the
loop instead: it is a child subreaper and kills every stray descendant after each job (see
``loop.py``). Before ``exec`` the child:

- lowers its priority with ``os.nice`` (10 for the processor's job children);
- sets ``RLIMIT_AS`` (address space), ``RLIMIT_CPU`` (SIGXCPU at the soft limit, SIGKILL two
  seconds later), ``RLIMIT_FSIZE`` (no file it writes can exceed the output limit),
  ``RLIMIT_NOFILE`` and ``RLIMIT_CORE`` (no core dumps of hostile input);
- asks for SIGKILL if its parent dies (``PR_SET_PDEATHSIG``) and exits at once if the parent
  already died before that request took effect;
- sets ``no_new_privs`` and, for the processor's job child, enforces the job's Landlock ruleset
  (``landlock.py``) and then the seccomp filter (``seccomp.py``), which ffmpeg and ffprobe
  inherit. Either failing to apply aborts the spawn: nothing runs unconfined.

``RLIMIT_NPROC`` is deliberately not set: it counts every process and thread of the UID,
including the processor itself and any other container sharing the UID mapping, and ffmpeg
and libheif decode with threads. The container's pids limit bounds process creation instead.

File descriptors are closed except stdout (a pipe the parent reads, bounded) and any
descriptor passed explicitly (the job's input); stdin and stderr are ``/dev/null``. The
environment holds only ``PATH``, the C UTF-8 locale, ``MALLOC_ARENA_MAX=2`` and, where a
caller adds it, ``TMPDIR``.

Why ``MALLOC_ARENA_MAX`` and bounded thread counts: ``RLIMIT_AS`` limits address space, not
resident memory, and threads reserve a lot of it. Each thread reserves a stack (``RLIMIT_STACK``,
8 MiB by default), and glibc gives threads their own malloc arenas, each reserving up to 64 MiB
on 64-bit systems (up to eight per core by default). ffmpeg starts one thread per core by
default (libx264 about one and a half per core), so on a many-core host an unbounded child could
reach its limit without using the memory. Every child therefore gets ``MALLOC_ARENA_MAX=2``,
ffmpeg runs with ``-threads 2`` for posters and ``-threads 4`` for transcodes (decoder, filters
and encoder), and pillow-heif decodes with two threads.
"""

from __future__ import annotations

import ctypes
import errno
import os
import resource
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from . import landlock
from .seccomp import Filter

SAFE_PATH: Final = "/usr/local/bin:/usr/bin:/bin"
DEFAULT_OPEN_FILES: Final = 64
DEFAULT_STDOUT_BYTES: Final = 64 * 1024
CPU_GRACE_SECONDS: Final = 2
_PR_SET_PDEATHSIG: Final = 1
_PR_SET_NO_NEW_PRIVS: Final = 38
_READ_CHUNK: Final = 65_536

# Loaded and bound before any fork: the pre-exec hook must not load libraries in the child.
_LIBC: Final = ctypes.CDLL(None, use_errno=True)
_PRCTL: Final = _LIBC.prctl
_PARENT_DIED: Final = 70


@dataclass(frozen=True, slots=True)
class ChildLimits:
    memory_bytes: int
    cpu_seconds: int
    wall_seconds: float
    output_bytes: int
    open_files: int = DEFAULT_OPEN_FILES
    nice: int = 10
    stdout_bytes: int = DEFAULT_STDOUT_BYTES

    def __post_init__(self) -> None:
        for value in (self.memory_bytes, self.cpu_seconds, self.output_bytes, self.open_files,
                      self.stdout_bytes):
            if type(value) is not int or value < 1:
                raise ValueError("child limits must be positive integers")
        if type(self.nice) is not int or not 0 <= self.nice <= 19:
            raise ValueError("child niceness increment must be within 0..19")
        if not isinstance(self.wall_seconds, int | float) or not 0 < self.wall_seconds <= 86_400:
            raise ValueError("child wall clock limit is out of bounds")


@dataclass(frozen=True, slots=True)
class Completed:
    """How a child ended. ``returncode`` is negative for a signal, as in ``subprocess``."""

    returncode: int
    timed_out: bool
    stdout: bytes
    stdout_overflow: bool

    @property
    def failure_code(self) -> str | None:
        """A protocol error code for an abnormal end, or ``None`` for a clean exit 0."""
        if self.stdout_overflow:
            return "invalid_result"
        if self.timed_out:
            return "timeout"
        if self.returncode == -signal.SIGXCPU:
            return "cpu_limit"
        if self.returncode == -signal.SIGXFSZ:
            return "output_limit"
        if self.returncode < 0:
            return "crashed"
        if self.returncode > 0:
            return "tool_failed"
        return None


def scrubbed_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The only environment a tool child receives."""
    environment = {
        "PATH": SAFE_PATH,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "MALLOC_ARENA_MAX": "2",
    }
    for key, value in (extra or {}).items():
        if not key.isidentifier() or not key.isupper() or "\0" in value or key in environment:
            raise ValueError("invalid child environment entry")
        environment[key] = value
    return environment


def _bounded(limit: int, value: int) -> tuple[int, int]:
    """(soft, hard), never above the inherited hard limit, which a child cannot raise."""
    _, hard = resource.getrlimit(limit)
    if hard != resource.RLIM_INFINITY:
        value = min(value, hard)
    return value, value


def limit_resources(
    limits: ChildLimits, landlock_ruleset: int | None = None,
    seccomp_filter: Filter | None = None,
) -> Callable[[], None]:
    """The pre-exec hook: ``limits``, then Landlock, then seccomp, in the forked child."""
    cpu_soft = limits.cpu_seconds
    cpu_hard = limits.cpu_seconds + CPU_GRACE_SECONDS
    parent = os.getpid()

    def apply() -> None:
        if limits.nice:
            os.nice(limits.nice)
        resource.setrlimit(resource.RLIMIT_AS, _bounded(resource.RLIMIT_AS, limits.memory_bytes))
        _, inherited_cpu = resource.getrlimit(resource.RLIMIT_CPU)
        hard = cpu_hard if inherited_cpu == resource.RLIM_INFINITY else min(cpu_hard, inherited_cpu)
        resource.setrlimit(resource.RLIMIT_CPU, (min(cpu_soft, hard), hard))
        resource.setrlimit(
            resource.RLIMIT_FSIZE, _bounded(resource.RLIMIT_FSIZE, limits.output_bytes),
        )
        resource.setrlimit(
            resource.RLIMIT_NOFILE, _bounded(resource.RLIMIT_NOFILE, limits.open_files),
        )
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if _PRCTL(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_SET_PDEATHSIG failed")
        if os.getppid() != parent:
            # The parent died between fork and the request: no signal will ever come.
            os._exit(_PARENT_DIED)
        if _PRCTL(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
        if landlock_ruleset is not None:
            landlock.restrict_self(landlock_ruleset)
        if seccomp_filter is not None:
            seccomp_filter.install()

    return apply


class Child:
    """A started tool child in its own session. Its pipe and process group belong to it."""

    def __init__(
        self,
        argv: Sequence[str],
        limits: ChildLimits,
        *,
        cwd: str,
        environment: Mapping[str, str],
        pass_fds: tuple[int, ...] = (),
        landlock_ruleset: int | None = None,
        seccomp_filter: Filter | None = None,
    ) -> None:
        if isinstance(argv, str | bytes) or not argv or not all(
            isinstance(argument, str) and "\0" not in argument for argument in argv
        ):
            raise TypeError("a child command is a non-empty array of strings")
        self.limits = limits
        self.started = time.monotonic()
        self.deadline = self.started + limits.wall_seconds
        self._stdout = bytearray()
        self._overflow = False
        self._eof = False
        self._timed_out = False
        self._result: Completed | None = None
        self.process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=cwd,
            env=dict(environment),
            close_fds=True,
            pass_fds=pass_fds,
            start_new_session=True,
            preexec_fn=limit_resources(limits, landlock_ruleset, seccomp_filter),
        )
        assert self.process.stdout is not None
        self._pipe = self.process.stdout
        os.set_blocking(self._pipe.fileno(), False)

    def fileno(self) -> int:
        return self._pipe.fileno()

    @property
    def eof(self) -> bool:
        return self._eof

    def pump(self) -> None:
        """Read what stdout has; past the bound, kill the child and stop reading."""
        if self._eof:
            return
        while True:
            try:
                chunk = os.read(self._pipe.fileno(), _READ_CHUNK)
            except BlockingIOError:
                return
            except OSError:
                self._eof = True
                return
            if not chunk:
                self._eof = True
                return
            room = self.limits.stdout_bytes - len(self._stdout)
            if len(chunk) > room:
                self._stdout += chunk[: max(room, 0)]
                self._overflow = True
                self._eof = True
                self.kill()
                return
            self._stdout += chunk

    def exited(self) -> bool:
        """Whether the child has exited, without reaping it (its group ID stays reserved)."""
        if self.process.returncode is not None:
            return True
        try:
            observed = os.waitid(
                os.P_PID, self.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except ChildProcessError:
            return True
        return observed is not None

    def expired(self, now: float | None = None) -> bool:
        return (time.monotonic() if now is None else now) >= self.deadline

    def kill(self) -> None:
        """SIGKILL the whole process group; safe until reaped, since the leader holds its ID."""
        if self.process.returncode is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            self.process.kill()

    def time_out(self) -> None:
        self._timed_out = True
        self.kill()

    def reap(self) -> Completed:
        """Kill anything left in the group, wait for the leader, and close the pipe."""
        if self._result is not None:
            return self._result
        self.kill()
        returncode = self.process.wait()
        self.pump()
        self._pipe.close()
        self._result = Completed(
            returncode=returncode,
            timed_out=self._timed_out,
            stdout=bytes(self._stdout),
            stdout_overflow=self._overflow,
        )
        return self._result


def run(
    argv: Sequence[str],
    limits: ChildLimits,
    *,
    cwd: str,
    environment: Mapping[str, str] | None = None,
    pass_fds: tuple[int, ...] = (),
    landlock_ruleset: int | None = None,
    seccomp_filter: Filter | None = None,
) -> Completed:
    """Run one child to completion under ``limits``; the deadline kills its process group."""
    child = Child(
        argv, limits, cwd=cwd,
        environment=scrubbed_environment() if environment is None else environment,
        pass_fds=pass_fds, landlock_ruleset=landlock_ruleset, seccomp_filter=seccomp_filter,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.fileno(), selectors.EVENT_READ)
            while True:
                if not child.eof:
                    child.pump()
                if child.eof:
                    if child.exited():
                        break
                    if child.expired():
                        child.time_out()
                        break
                    time.sleep(min(0.05, max(child.deadline - time.monotonic(), 0.0)))
                    continue
                remaining = child.deadline - time.monotonic()
                if remaining <= 0:
                    child.time_out()
                    break
                selector.select(min(remaining, 1.0))
    except BaseException:
        child.kill()
        child.reap()
        raise
    return child.reap()


def output_limit_error(error: BaseException) -> bool:
    """Whether an error is the file-size limit (Python ignores SIGXFSZ, so writes get EFBIG)."""
    return isinstance(error, OSError) and error.errno == errno.EFBIG
