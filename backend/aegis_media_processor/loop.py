"""The processor loop: claim jobs, run one confined decoding child each, publish results (M2).

The loop never decodes media and never imports Pillow. For each job it:

1. claims ``inbox/<id>.json`` atomically by renaming it to ``work/<id>/job.json`` (interactive
   jobs first, then the oldest). That file stays the loop's: the child may read it, never write;
2. validates the job and opens the original itself (``source.py``: no-follow components, then the
   fingerprint on the descriptor). An input that changed or cannot be opened is published at
   once, without a child;
3. creates ``work/<id>/out/`` and starts ``python -I -B -m aegis_media_processor.child <op>
   <job dir> <fd>`` with only that descriptor, under the job's rlimits, at nice 10, with a
   scrubbed environment, a Landlock ruleset that allows writes only beneath ``out/``, and a
   seccomp filter that denies metadata changes, ptrace and namespace calls;
4. after the child ends (or its deadline kills its process group), kills and reaps every stray
   descendant: the loop is a child subreaper, so processes that left the group come back to it.
   If one survives, that pass publishes its finished jobs as failures, without outputs;
5. validates the child's result against the in-memory job, checks each declared output in
   ``out/`` (``lstat``: a regular file with one link, of the declared size, with its format's
   signature), moves the outputs into a fresh directory with ``result.json``, publishes it as
   ``done/<id>`` by one rename, and discards ``work/<id>``.

Nothing a child leaves behind can stop the loop: removal makes directories writable again, and
whatever still cannot be removed is moved to ``rejected/``. At startup, claims left in ``work/``
are published as ``failed`` with ``interrupted``, so a job that kills the processor counts as an
attempt instead of looping. On SIGTERM or SIGINT the loop stops claiming, kills its children
and re-enqueues their jobs from memory. Polling sleeps at most :data:`POLL_SECONDS` (0.1 s: a
job queued into an idle processor starts within it, for a few cheap system calls per idle
pass); a signal or a child's output wakes it at once, and the next pass refills a finished
child's slot from the inbox without waiting. ``AEGIS_MEDIA_PROCESSOR_CONCURRENCY`` (1 to 16,
default 4) bounds the number of children. Each pass also restores mode 0770 on the scratch
directories and the running jobs' directories. Without Landlock ABI 3 or newer, or without the
seccomp filter (x86-64 only), the processor does not start.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Final

from . import landlock, seccomp
from .child import EXIT_INVALID_JOB, OUTPUT_DIR, failure, open_failure
from .protocol import (
    DEFAULT_ROOTS_DIR,
    DEFAULT_SCRATCH_DIR,
    DONE,
    INBOX,
    JOB_FILE,
    JOB_ID_RE,
    MAX_JOB_BYTES,
    MAX_RESULT_BYTES,
    RESULT_FILE,
    WORK,
    Job,
    ProtocolError,
    Result,
    decode_job,
    decode_result,
    encode_result,
    job_filename,
    job_id_from_filename,
    output_magic_matches,
    read_regular_file,
    write_file_atomically,
    write_job,
)
from .sandbox import Child, ChildLimits, Completed, scrubbed_environment
from .source import InputRefused, SourceChanged, SourceUnavailable, open_source

CONCURRENCY_ENVIRONMENT: Final = "AEGIS_MEDIA_PROCESSOR_CONCURRENCY"
DEFAULT_CONCURRENCY: Final = 4
MAX_CONCURRENCY: Final = 16
POLL_SECONDS: Final = 0.1
MAX_INBOX_SCAN: Final = 1024
CHILD_NICENESS: Final = 10
DIRECTORY_MODE: Final = 0o770
REJECTED: Final = "rejected"
EXIT_SCRATCH: Final = 1
EXIT_CONFIG: Final = 2
EXIT_LANDLOCK: Final = 3
_STRAY_ROUNDS: Final = 100
_UNLOCK_DEPTH: Final = 64
_PR_SET_CHILD_SUBREAPER: Final = 36
_PR_GET_CHILD_SUBREAPER: Final = 37
_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOCTTY
_PRCTL: Final = ctypes.CDLL(None, use_errno=True).prctl

LOGGER: Final = logging.getLogger("aegis.media.processor")


class ConfigError(ValueError):
    """The processor's configuration is invalid; the message names only the setting."""


class ScratchError(RuntimeError):
    """The scratch directories are missing or not plain directories."""


def concurrency_from_environment(environ: Mapping[str, str]) -> int:
    """``AEGIS_MEDIA_PROCESSOR_CONCURRENCY``: decimal digits only, 1 to 16; default 4."""
    raw = environ.get(CONCURRENCY_ENVIRONMENT)
    if raw is None:
        return DEFAULT_CONCURRENCY
    if (not isinstance(raw, str) or not 1 <= len(raw) <= 2 or not raw.isascii()
            or not raw.isdecimal() or not 1 <= int(raw) <= MAX_CONCURRENCY):
        raise ConfigError(f"{CONCURRENCY_ENVIRONMENT} must be an integer from 1 to 16")
    return int(raw)


def subreaper() -> bool:
    value = ctypes.c_int(0)
    if _PRCTL(_PR_GET_CHILD_SUBREAPER, ctypes.byref(value), 0, 0, 0) != 0:
        return False
    return bool(value.value)


def set_subreaper(enabled: bool) -> None:
    if _PRCTL(_PR_SET_CHILD_SUBREAPER, 1 if enabled else 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER failed")


@dataclass(slots=True)
class Running:
    job: Job
    child: Child
    directory: str


def _unlock(path: str, depth: int = 0) -> None:
    """Give the owner full access to every directory beneath ``path``; never follows links."""
    if depth > _UNLOCK_DEPTH:
        return
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            return
        os.chmod(path, 0o700)
        names = os.listdir(path)
    except OSError:
        return
    for name in names:
        _unlock(os.path.join(path, name), depth + 1)


def _force_remove(path: str) -> None:
    """Remove a file, symlink or tree without following symlinks; raises if it cannot."""
    for attempt in range(2):
        try:
            observed = os.lstat(path)
        except FileNotFoundError:
            return
        try:
            if stat.S_ISDIR(observed.st_mode):
                shutil.rmtree(path)
            else:
                os.unlink(path)
            return
        except OSError:
            if attempt:
                raise
            # A child may have left read-only or inaccessible directories behind.
            if stat.S_ISDIR(observed.st_mode):
                _unlock(path)
            else:
                with suppress(OSError):
                    os.chmod(os.path.dirname(path), 0o700)


def verify_outputs(directory: str, job: Job, result: Result) -> Result:
    """``result`` only if every declared output in ``directory`` is a plain, matching file."""
    for output in result.outputs:
        path = os.path.join(directory, output.filename)
        try:
            observed = os.lstat(path)
            if (not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1
                    or observed.st_size != output.bytes):
                return failure(job, "failed", "invalid_result")
            descriptor = os.open(path, _READ_FLAGS)
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                    observed.st_dev, observed.st_ino,
                ):
                    return failure(job, "failed", "invalid_result")
                head = os.read(descriptor, 12)
            finally:
                os.close(descriptor)
        except OSError:
            return failure(job, "failed", "invalid_result")
        if not output_magic_matches(output.format, head):
            return failure(job, "failed", "invalid_result")
    return result


def _child_failure(job: Job, completed: Completed) -> Result | None:
    code = completed.failure_code
    if code is None:
        return None
    if code == "timeout":
        return failure(job, "timeout", "timeout")
    if code == "tool_failed":
        code = "invalid_job" if completed.returncode == EXIT_INVALID_JOB else "crashed"
    return failure(job, "failed", code)


def _process_parents() -> dict[int, int]:
    """Every visible process and its parent, from ``/proc/<pid>/stat``."""
    parents: dict[int, int] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return parents
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as handle:
                data = handle.read(4096)
            parents[int(name)] = int(data.rsplit(b")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
    return parents


def _is_stray(pid: int, parents: Mapping[int, int], me: int, tracked: set[int]) -> bool:
    """A descendant of ``me`` that is not (beneath) a running job's child."""
    current, seen = pid, set()
    while current not in seen:
        if current in tracked:
            return False
        parent = parents.get(current)
        if parent is None or parent <= 0:
            return False
        if parent == me:
            return True
        seen.add(current)
        current = parent
    return False


def _kill_if_parent(pid: int, parent: int) -> None:
    """SIGKILL ``pid`` through a pidfd, if it is still the process seen with ``parent``."""
    try:
        handle = os.pidfd_open(pid)
    except OSError:
        return
    try:
        if _parent_of(pid) == parent:
            signal.pidfd_send_signal(handle, signal.SIGKILL)
    except OSError:
        pass
    finally:
        os.close(handle)


def _parent_of(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            return int(handle.read(4096).rsplit(b")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return None


class Processor:
    """One processor: the scratch volume, the roots, and up to ``concurrency`` children."""

    def __init__(
        self,
        scratch: str = DEFAULT_SCRATCH_DIR,
        roots: str = DEFAULT_ROOTS_DIR,
        concurrency: int = DEFAULT_CONCURRENCY,
        *,
        child_command: Sequence[str] | None = None,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        for path in (scratch, roots):
            if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
                raise ConfigError("scratch and roots must be absolute paths")
        if type(concurrency) is not int or not 1 <= concurrency <= MAX_CONCURRENCY:
            raise ConfigError("concurrency must be an integer from 1 to 16")
        if not 0 < poll_seconds <= 2:
            raise ConfigError("the poll interval must be at most two seconds")
        self.scratch = os.path.normpath(scratch)
        self.roots = os.path.normpath(roots)
        self.concurrency = concurrency
        self.poll_seconds = poll_seconds
        self.inbox = os.path.join(self.scratch, INBOX)
        self.work = os.path.join(self.scratch, WORK)
        self.done = os.path.join(self.scratch, DONE)
        self.rejected = os.path.join(self.scratch, REJECTED)
        self._command = tuple(child_command or (
            sys.executable, "-I", "-B", "-m", "aegis_media_processor.child",
        ))
        self._running: dict[str, Running] = {}
        self._stopping = False
        self._abi = 0
        self._filter: seccomp.Filter | None = None
        self._mode_failures: set[str] = set()
        self._wake_read, self._wake_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)

    def __enter__(self) -> Processor:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        for descriptor in (self._wake_read, self._wake_write):
            with suppress(OSError):
                os.close(descriptor)

    @property
    def running(self) -> int:
        return len(self._running)

    def request_stop(self) -> None:
        """Stop claiming and shut down; safe to call from a signal handler."""
        self._stopping = True
        with suppress(BlockingIOError, OSError):
            os.write(self._wake_write, b"\0")

    def landlock_abi(self) -> int:
        """The Landlock ABI, checked once; raises ``LandlockUnavailable`` (fail closed)."""
        if self._abi < landlock.MINIMUM_ABI:
            self._abi = landlock.require()
        return self._abi

    def seccomp_filter(self) -> seccomp.Filter:
        """The child's seccomp filter, built once; raises ``SeccompUnavailable`` (fail closed)."""
        if self._filter is None:
            self._filter = seccomp.Filter()
        return self._filter

    def restore_modes(self) -> None:
        """Put back mode 0770 on the scratch directories and each running job's directories.

        Landlock cannot restrict ``chmod``; the seccomp filter denies it to children, and this
        repairs anything else on every pass. Only the mode bits are touched (setgid is kept),
        never ownership, and a failure is logged with a safe code only.
        """
        paths = [self.scratch, self.inbox, self.work, self.done, self.rejected]
        for running in self._running.values():
            paths += [running.directory, os.path.join(running.directory, OUTPUT_DIR)]
        for path in paths:
            try:
                observed = os.lstat(path)
                if not stat.S_ISDIR(observed.st_mode):
                    continue
                wanted = DIRECTORY_MODE | (observed.st_mode & stat.S_ISGID)
                if stat.S_IMODE(observed.st_mode) != wanted:
                    os.chmod(path, wanted)
                    LOGGER.warning("media scratch: a directory mode was restored (mode_restored)")
            except OSError:
                if path not in self._mode_failures:
                    self._mode_failures.add(path)
                    LOGGER.error("media scratch: a directory mode cannot be restored (mode_failed)")
            else:
                self._mode_failures.discard(path)

    # -- scratch directories ------------------------------------------------------------------

    def prepare(self) -> None:
        """Create ``inbox``, ``work``, ``done`` and ``rejected``; each a real directory."""
        try:
            if not stat.S_ISDIR(os.lstat(self.scratch).st_mode):
                raise ScratchError("the scratch path is not a directory")
            for path in (self.inbox, self.work, self.done, self.rejected):
                with suppress(FileExistsError):
                    os.mkdir(path, DIRECTORY_MODE)
                if not stat.S_ISDIR(os.lstat(path).st_mode):
                    raise ScratchError("a scratch subdirectory is not a directory")
        except OSError as error:
            raise ScratchError("the scratch directories are unavailable") from error
        self.restore_modes()

    def discard(self, path: str) -> None:
        """Remove a scratch entry; if that fails, move it to ``rejected/``. Never raises."""
        try:
            _force_remove(path)
            return
        except OSError:
            pass
        try:
            os.rename(path, os.path.join(
                self.rejected, f"{os.path.basename(path).lstrip('.')}-{secrets.token_hex(6)}",
            ))
            LOGGER.error("media scratch: an entry was moved to rejected (discard_failed)")
        except OSError:
            LOGGER.error("media scratch: an entry could not be discarded (discard_failed)")

    def sweep(self) -> int:
        """Publish every stale claim in ``work/`` as interrupted; discard everything else."""
        with suppress(OSError):
            for name in os.listdir(self.rejected):
                with suppress(OSError):
                    _force_remove(os.path.join(self.rejected, name))
        try:
            names = os.listdir(self.work)
        except OSError as error:
            raise ScratchError("the work directory is unreadable") from error
        swept = 0
        for name in names:
            path = os.path.join(self.work, name)
            try:
                claim = JOB_ID_RE.fullmatch(name) is not None and stat.S_ISDIR(
                    os.lstat(path).st_mode
                )
                if (not claim or os.path.lexists(os.path.join(self.done, name))
                        or not os.path.lexists(os.path.join(path, JOB_FILE))):
                    # Junk, already published, or claimed no further than its directory (the
                    # inbox still holds the job if it was never renamed).
                    self.discard(path)
                    continue
                job: Job | None
                try:
                    job = decode_job(read_regular_file(os.path.join(path, JOB_FILE),
                                                       MAX_JOB_BYTES))
                    if job.id != name:
                        job = None
                except (OSError, ProtocolError):
                    job = None
            except OSError:
                self.discard(path)
                continue
            self._publish(name, Result(name, "failed", "interrupted", None, ()), job, None)
            self.discard(path)
            swept += 1
            LOGGER.warning("media job %s: stale claim published as interrupted", name)
        return swept

    # -- claiming -----------------------------------------------------------------------------

    def _candidates(self) -> list[str]:
        """Inbox job ids, interactive first, then oldest; unreadable jobs sort last."""
        found: list[tuple[int, int, str]] = []
        try:
            with os.scandir(self.inbox) as entries:
                for entry in entries:
                    job_id = job_id_from_filename(entry.name)
                    if job_id is None:
                        continue
                    try:
                        modified = entry.stat(follow_symlinks=False).st_mtime_ns
                        lane = decode_job(read_regular_file(entry.path, MAX_JOB_BYTES)).lane
                    except (OSError, ProtocolError):
                        found.append((2, 0, job_id))
                    else:
                        found.append((0 if lane == "interactive" else 1, modified, job_id))
                    if len(found) >= MAX_INBOX_SCAN:
                        break
        except OSError:
            LOGGER.error("media processor: the inbox is unreadable (inbox_unreadable)")
        return [job_id for _, _, job_id in sorted(found)]

    def claim(self, job_id: str) -> str | None:
        """Atomically move ``inbox/<id>.json`` to ``work/<id>/job.json``; the job directory."""
        source = os.path.join(self.inbox, job_filename(job_id))
        directory = os.path.join(self.work, job_id)
        try:
            if os.path.lexists(os.path.join(self.done, job_id)):
                LOGGER.error("media job %s: refused, its id is already published", job_id)
                with suppress(FileNotFoundError):
                    os.unlink(source)
                return None
            try:
                os.mkdir(directory, DIRECTORY_MODE)
            except FileExistsError:
                LOGGER.error("media job %s: refused, its id is already claimed", job_id)
                with suppress(FileNotFoundError):
                    os.unlink(source)
                return None
            try:
                os.rename(source, os.path.join(directory, JOB_FILE))
            except FileNotFoundError:
                os.rmdir(directory)
                return None
        except OSError:
            LOGGER.error("media job %s: could not be claimed (claim_failed)", job_id)
            if not os.path.lexists(os.path.join(directory, JOB_FILE)):
                self.discard(directory)
            return None
        return directory

    def _start(self, job_id: str, directory: str) -> None:
        try:
            job = decode_job(read_regular_file(os.path.join(directory, JOB_FILE), MAX_JOB_BYTES))
            if job.id != job_id:
                raise ProtocolError
        except (OSError, ProtocolError):
            self._conclude(job_id, Result(job_id, "failed", "invalid_job", None, ()), None,
                           directory)
            return
        try:
            descriptor = open_source(job.input, self.roots)
        except (InputRefused, SourceChanged, SourceUnavailable) as error:
            self._conclude(job_id, open_failure(job, error), job, directory)
            return
        ruleset: int | None = None
        try:
            output = os.path.join(directory, OUTPUT_DIR)
            os.mkdir(output, DIRECTORY_MODE)
            policy = landlock.child_policy(
                os.path.join(directory, JOB_FILE), output, (self.roots, self.scratch),
            )
            ruleset = landlock.create_ruleset(policy, self.landlock_abi())
            child = Child(
                [*self._command, job.op, directory, str(descriptor)],
                ChildLimits(
                    memory_bytes=job.limits.memory_bytes,
                    cpu_seconds=job.limits.cpu_seconds,
                    wall_seconds=job.limits.wall_seconds,
                    output_bytes=job.limits.output_bytes,
                    nice=CHILD_NICENESS,
                    stdout_bytes=MAX_RESULT_BYTES,
                ),
                cwd=output,
                environment=scrubbed_environment({"TMPDIR": output}),
                pass_fds=(descriptor,),
                landlock_ruleset=ruleset,
                seccomp_filter=self.seccomp_filter(),
            )
        except (OSError, subprocess.SubprocessError, ValueError, landlock.LandlockUnavailable,
                seccomp.SeccompUnavailable):
            self._conclude(job_id, failure(job, "failed", "spawn_failed"), job, directory)
            LOGGER.error("media job %s: failed spawn_failed", job_id)
            return
        finally:
            os.close(descriptor)
            if ruleset is not None:
                os.close(ruleset)
        self._running[job_id] = Running(job, child, directory)

    def fill(self) -> int:
        """Claim and start jobs up to the concurrency; returns how many were claimed."""
        claimed = 0
        if self._stopping or len(self._running) >= self.concurrency:
            return 0
        for job_id in self._candidates():
            if len(self._running) >= self.concurrency or self._stopping:
                break
            directory = self.claim(job_id)
            if directory is None:
                continue
            claimed += 1
            self._start(job_id, directory)
        return claimed

    # -- finishing ----------------------------------------------------------------------------

    def _publish(
        self, job_id: str, result: Result, job: Job | None, outputs_from: str | None,
    ) -> bool:
        """Build ``result.json`` plus its outputs in a fresh directory, rename it to ``done/``.

        Never raises. If an ``ok`` result cannot be published, an ``invalid_result`` failure is.
        """
        staging = os.path.join(self.work, f".publish-{job_id}")
        try:
            if os.path.lexists(staging):
                self.discard(staging)
            os.mkdir(staging, DIRECTORY_MODE)
            for output in result.outputs:
                if outputs_from is None:
                    raise OSError("outputs without a source directory")
                os.rename(os.path.join(outputs_from, output.filename),
                          os.path.join(staging, output.filename))
            descriptor = os.open(staging, _DIRECTORY_FLAGS)
            try:
                write_file_atomically(descriptor, RESULT_FILE, encode_result(result, job))
            finally:
                os.close(descriptor)
            os.rename(staging, os.path.join(self.done, job_id))
            return True
        except (OSError, ProtocolError):
            self.discard(staging)
        if result.status == "ok" and job is not None:
            LOGGER.error("media job %s: outputs could not be published (publish_failed)", job_id)
            return self._publish(job_id, failure(job, "failed", "invalid_result"), job, None)
        LOGGER.error("media job %s: no result could be published (publish_failed)", job_id)
        return False

    def _conclude(self, job_id: str, result: Result, job: Job | None, directory: str) -> None:
        self._publish(job_id, result, job, os.path.join(directory, OUTPUT_DIR))
        self.discard(directory)
        LOGGER.info("media job %s: %s %s", job_id, result.status, result.safe_error_code or "-")

    def _finish(self, running: Running, completed: Completed, *, trusted: bool = True) -> None:
        job = running.job
        output = os.path.join(running.directory, OUTPUT_DIR)
        result = _child_failure(job, completed)
        if result is None and not trusted:
            result = failure(job, "failed", "crashed")
        if result is None:
            try:
                result = verify_outputs(output, job, decode_result(completed.stdout, job))
            except ProtocolError:
                result = failure(job, "failed", "invalid_result")
        self._publish(job.id, result, job, output)
        self.discard(running.directory)
        elapsed = time.monotonic() - running.child.started
        LOGGER.info(
            "media job %s: %s %s %s in %.1fs", job.id, job.op, result.status,
            result.safe_error_code or "-", elapsed,
        )

    def kill_strays(self) -> bool:
        """SIGKILL and reap every descendant that is not beneath a running job's child.

        With the loop as child subreaper, processes that left a job's group (``setsid``) or
        outlived their parent come back to the loop, so they are found here. Returns whether
        none survived. In the processor's container the loop's only descendants are job
        processes; a process hosting the loop in-process (a test) becomes the subreaper while
        ``run()`` lasts, and any other untracked descendant it has is killed too.
        """
        me = os.getpid()
        for _ in range(_STRAY_ROUNDS):
            parents = _process_parents()
            tracked = {running.child.process.pid for running in self._running.values()}
            strays = [pid for pid in parents if _is_stray(pid, parents, me, tracked)]
            if not strays:
                return True
            for pid in strays:
                _kill_if_parent(pid, parents[pid])
            for pid in strays:
                if parents[pid] == me:
                    with suppress(ChildProcessError):
                        os.waitpid(pid, os.WNOHANG)
            time.sleep(0.01)
        LOGGER.error("media processor: stray processes survived (strays_remaining)")
        return False

    def collect(self) -> int:
        """Finish children that exited or ran past their deadline; returns how many."""
        finished: list[tuple[Running, Completed]] = []
        now = time.monotonic()
        for job_id, running in list(self._running.items()):
            child = running.child
            child.pump()
            if not child.exited():
                if not child.expired(now):
                    continue
                child.time_out()
            completed = child.reap()
            del self._running[job_id]
            finished.append((running, completed))
        # Before any output is read, nothing a finished job started may still be running. A
        # survivor could still hold an output open for writing, so then nothing is published.
        clean = self.kill_strays()
        for running, completed in finished:
            self._finish(running, completed, trusted=clean)
        return len(finished)

    def wait(self) -> None:
        """Sleep until a child writes or exits, a signal arrives, or the poll interval ends."""
        timeout = self.poll_seconds
        now = time.monotonic()
        with selectors.DefaultSelector() as selector:
            selector.register(self._wake_read, selectors.EVENT_READ)
            for running in self._running.values():
                timeout = min(timeout, max(running.child.deadline - now, 0.0))
                if running.child.eof:
                    timeout = min(timeout, 0.05)
                else:
                    selector.register(running.child.fileno(), selectors.EVENT_READ)
            selector.select(timeout)
        with suppress(BlockingIOError, OSError):
            while os.read(self._wake_read, 64):
                pass

    # -- the loop -----------------------------------------------------------------------------

    def shutdown(self) -> None:
        """Kill running children and re-enqueue their jobs from memory, never from disk."""
        stopped = list(self._running.values())
        for running in stopped:
            running.child.kill()
            running.child.reap()
        self._running.clear()
        self.kill_strays()
        for running in stopped:
            try:
                write_job(self.inbox, running.job)
            except (OSError, ProtocolError):
                self._publish(running.job.id, failure(running.job, "failed", "interrupted"),
                              running.job, None)
            else:
                LOGGER.info("media job %s: returned to the inbox at shutdown", running.job.id)
            self.discard(running.directory)

    def run(self, *, until_idle: bool = False) -> None:
        """Serve until stopped; ``until_idle`` returns once nothing is queued or running."""
        self.landlock_abi()
        self.seccomp_filter()
        self.prepare()
        was_subreaper = subreaper()
        set_subreaper(True)
        try:
            self.sweep()
            while not self._stopping:
                self.restore_modes()
                claimed = self.fill()
                if until_idle and not claimed and not self._running:
                    return
                self.wait()
                self.collect()
        finally:
            self.shutdown()
            if not was_subreaper:
                set_subreaper(False)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m aegis_media_processor")
    parser.add_argument("--scratch", default=DEFAULT_SCRATCH_DIR)
    parser.add_argument("--roots", default=DEFAULT_ROOTS_DIR)
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s",
                        stream=sys.stderr)
    try:
        concurrency = concurrency_from_environment(os.environ)
        processor = Processor(arguments.scratch, arguments.roots, concurrency)
    except ConfigError as error:
        LOGGER.error("configuration refused: %s", error)
        return EXIT_CONFIG
    try:
        abi = processor.landlock_abi()
        processor.seccomp_filter()
    except (landlock.LandlockUnavailable, seccomp.SeccompUnavailable) as error:
        LOGGER.error("%s (confinement_unavailable)", error)
        processor.close()
        return EXIT_LANDLOCK
    except Exception:
        # Any other failure to build the confinement is a refusal too, never a traceback.
        LOGGER.error("the child confinement could not be built (confinement_unavailable)")
        processor.close()
        return EXIT_LANDLOCK
    os.umask(0o007)

    def stop(_signal: int, _frame: object) -> None:
        processor.request_stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    LOGGER.info("media processor started with %d children, Landlock ABI %d", concurrency, abi)
    try:
        processor.run()
    except ScratchError as error:
        LOGGER.error("%s", error)
        return EXIT_SCRATCH
    finally:
        processor.close()
    LOGGER.info("media processor stopped")
    return 0
