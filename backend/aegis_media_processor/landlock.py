"""Landlock confinement for the decoding child (C1), through the raw syscalls 444-446.

The processor refuses to start without Landlock ABI 3 or newer (:data:`MINIMUM_ABI`), so
``truncate`` is always handled; there is no unconfined fallback. The loop prepares one ruleset
per job and the child applies it in its pre-exec hook, after ``PR_SET_NO_NEW_PRIVS`` and
before the seccomp filter (``seccomp.py``), so the Python child and every ffmpeg or ffprobe it
starts are confined from their first instruction.

Handled rights, by the ABI that introduced them (values from ``linux/landlock.h``); every right
the negotiated ABI knows is handled, up to :data:`HIGHEST_KNOWN_ABI` (10):

======  ==========================================  =========================================
ABI     filesystem (``handled_access_fs``)          network and scopes
======  ==========================================  =========================================
1       EXECUTE 1<<0, WRITE_FILE 1<<1,
        READ_FILE 1<<2, READ_DIR 1<<3,
        REMOVE_DIR 1<<4, REMOVE_FILE 1<<5,
        MAKE_CHAR 1<<6, MAKE_DIR 1<<7, MAKE_REG 1<<8,
        MAKE_SOCK 1<<9, MAKE_FIFO 1<<10,
        MAKE_BLOCK 1<<11, MAKE_SYM 1<<12
2       REFER 1<<13
3       TRUNCATE 1<<14
4                                                   net: BIND_TCP 1<<0, CONNECT_TCP 1<<1
5       IOCTL_DEV 1<<15
6                                                   scoped: ABSTRACT_UNIX_SOCKET 1<<0,
                                                    SIGNAL 1<<1
7, 8    (no rights: ``landlock_restrict_self`` logging and thread flags)
9       RESOLVE_UNIX 1<<16 (pathname Unix sockets)
10                                                  net: BIND_UDP 1<<2, CONNECT_SEND_UDP 1<<3
======  ==========================================  =========================================

No network right is ever granted (TCP from ABI 4, UDP from ABI 10), no pathname Unix socket
outside the child's domain can be reached (ABI 9), and with ABI 6 both scopes are set:
abstract Unix sockets and signals stay inside the child's domain. A kernel with an ABI above
10 still gets exactly these rights; newer rights stay unrestricted until this module learns
them (the kernel's compatibility model). The 24-byte ``landlock_ruleset_attr`` prefix is used;
ABI 10's quiet-logging fields are left out.

Path rules allow:

- read and execute beneath ``/usr``, ``/lib``, ``/lib64`` and the Python installation (the base
  interpreter, the virtual environment, and the directory holding this package);
- read beneath ``/etc`` (the dynamic loader's cache and configuration, GnuTLS's configuration);
- ``/dev/null`` read and write: the child opens it ``O_RDWR`` as stdin and stderr for ffmpeg and
  ffprobe (``subprocess.DEVNULL``). ``/dev/urandom`` read: the random-number fallbacks of
  Python, GnuTLS and the codecs when ``getrandom`` is unavailable. No other device is opened:
  stdio is inherited, memory is anonymous (no ``/dev/zero``), and no codec opens a hardware
  device. No ``IOCTL_DEV``: an ``isatty`` probe on ``/dev/null`` simply answers "not a tty";
- read of the job's own ``job.json``;
- read, write, create, truncate and remove regular files beneath the job's ``out/`` directory.

Everything else is denied: no other root path (the input arrives as an already-open
descriptor), no other scratch path, no directories, links, devices, FIFOs or sockets created
anywhere. ``child_policy`` also refuses a readable directory that contains, or lies inside,
the roots or the scratch volume.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

_SYS_CREATE_RULESET: Final = 444
_SYS_ADD_RULE: Final = 445
_SYS_RESTRICT_SELF: Final = 446
_CREATE_RULESET_VERSION: Final = 1 << 0
_RULE_PATH_BENEATH: Final = 1
_PR_SET_NO_NEW_PRIVS: Final = 38

MINIMUM_ABI: Final = 3
HIGHEST_KNOWN_ABI: Final = 10

ACCESS_FS_EXECUTE: Final = 1 << 0
ACCESS_FS_WRITE_FILE: Final = 1 << 1
ACCESS_FS_READ_FILE: Final = 1 << 2
ACCESS_FS_READ_DIR: Final = 1 << 3
ACCESS_FS_REMOVE_DIR: Final = 1 << 4
ACCESS_FS_REMOVE_FILE: Final = 1 << 5
ACCESS_FS_MAKE_CHAR: Final = 1 << 6
ACCESS_FS_MAKE_DIR: Final = 1 << 7
ACCESS_FS_MAKE_REG: Final = 1 << 8
ACCESS_FS_MAKE_SOCK: Final = 1 << 9
ACCESS_FS_MAKE_FIFO: Final = 1 << 10
ACCESS_FS_MAKE_BLOCK: Final = 1 << 11
ACCESS_FS_MAKE_SYM: Final = 1 << 12
ACCESS_FS_REFER: Final = 1 << 13  # ABI 2
ACCESS_FS_TRUNCATE: Final = 1 << 14  # ABI 3
ACCESS_FS_IOCTL_DEV: Final = 1 << 15  # ABI 5
ACCESS_FS_RESOLVE_UNIX: Final = 1 << 16  # ABI 9
ACCESS_NET_BIND_TCP: Final = 1 << 0  # ABI 4
ACCESS_NET_CONNECT_TCP: Final = 1 << 1  # ABI 4
ACCESS_NET_BIND_UDP: Final = 1 << 2  # ABI 10
ACCESS_NET_CONNECT_SEND_UDP: Final = 1 << 3  # ABI 10
SCOPE_ABSTRACT_UNIX_SOCKET: Final = 1 << 0  # ABI 6
SCOPE_SIGNAL: Final = 1 << 1  # ABI 6

_ABI1_FS: Final = (1 << 13) - 1
READ_EXECUTE: Final = ACCESS_FS_EXECUTE | ACCESS_FS_READ_FILE | ACCESS_FS_READ_DIR
READ_ONLY: Final = ACCESS_FS_READ_FILE | ACCESS_FS_READ_DIR
FILE_READ: Final = ACCESS_FS_READ_FILE
FILE_READ_WRITE: Final = ACCESS_FS_READ_FILE | ACCESS_FS_WRITE_FILE
OUTPUT_DIRECTORY: Final = (
    ACCESS_FS_READ_FILE | ACCESS_FS_READ_DIR | ACCESS_FS_WRITE_FILE | ACCESS_FS_MAKE_REG
    | ACCESS_FS_REMOVE_FILE | ACCESS_FS_TRUNCATE
)
SYSTEM_EXECUTABLE: Final = ("/usr", "/lib", "/lib64")
SYSTEM_READABLE: Final = ("/etc",)
# Exactly the devices a confined child opens, with the least rights each needs.
DEVICES: Final = (("/dev/null", FILE_READ_WRITE), ("/dev/urandom", FILE_READ))

_LIBC: Final = ctypes.CDLL(None, use_errno=True)
_SYSCALL: Final = _LIBC.syscall
_SYSCALL.restype = ctypes.c_long
_PRCTL: Final = _LIBC.prctl


class LandlockUnavailable(RuntimeError):
    """The kernel (or the container's seccomp profile) offers no Landlock ABI 3 or newer."""


class _RulesetAttr(ctypes.Structure):
    _fields_ = (
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    )


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = (("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32))


def abi_version() -> int:
    """The Landlock ABI the kernel offers, or 0 when it offers none."""
    version = _SYSCALL(
        ctypes.c_long(_SYS_CREATE_RULESET), None, ctypes.c_size_t(0),
        ctypes.c_uint32(_CREATE_RULESET_VERSION),
    )
    return int(version) if version > 0 else 0


def require(minimum: int = MINIMUM_ABI) -> int:
    abi = abi_version()
    if abi < minimum:
        raise LandlockUnavailable(
            f"Landlock ABI {minimum} or newer is unavailable: the media processor will not start"
        )
    return abi


def filesystem_rights(abi: int) -> int:
    rights = _ABI1_FS
    if abi >= 2:
        rights |= ACCESS_FS_REFER
    if abi >= 3:
        rights |= ACCESS_FS_TRUNCATE
    if abi >= 5:
        rights |= ACCESS_FS_IOCTL_DEV
    if abi >= 9:
        rights |= ACCESS_FS_RESOLVE_UNIX
    return rights


def network_rights(abi: int) -> int:
    rights = 0
    if abi >= 4:
        rights |= ACCESS_NET_BIND_TCP | ACCESS_NET_CONNECT_TCP
    if abi >= 10:
        rights |= ACCESS_NET_BIND_UDP | ACCESS_NET_CONNECT_SEND_UDP
    return rights


def scopes(abi: int) -> int:
    return SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL if abi >= 6 else 0


@dataclass(frozen=True, slots=True)
class Policy:
    """What one confined child may reach (see the module docstring)."""

    executable_dirs: tuple[str, ...]
    readable_dirs: tuple[str, ...]
    files: tuple[tuple[str, int], ...]
    output_dir: str


def _python_dirs() -> tuple[str, ...]:
    package_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = {sys.base_prefix, sys.base_exec_prefix, sys.prefix, sys.exec_prefix,
                  package_parent}
    return tuple(sorted(os.path.realpath(path) for path in candidates if path))


def _beneath(path: str, directory: str) -> bool:
    directory = directory.rstrip("/") or "/"
    return path == directory or path.startswith(directory + "/") or directory == "/"


def child_policy(job_file: str, output_dir: str, forbidden: Sequence[str]) -> Policy:
    """The job child's policy; refuses a readable path that contains or lies in ``forbidden``."""
    executable = tuple(dict.fromkeys((*SYSTEM_EXECUTABLE, *_python_dirs())))
    readable = SYSTEM_READABLE
    for path in (*executable, *readable):
        real = os.path.realpath(path)
        for denied in forbidden:
            hidden = os.path.realpath(denied)
            if _beneath(hidden, real) or _beneath(real, hidden):
                raise LandlockUnavailable("a readable path would expose a forbidden directory")
    return Policy(
        executable_dirs=executable,
        readable_dirs=readable,
        files=(*DEVICES, (job_file, FILE_READ)),
        output_dir=output_dir,
    )


def _check(result: int, what: str) -> int:
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, f"{what} failed: {os.strerror(error)}")
    return result


def _add_rule(ruleset: int, path: str, access: int, flags: int) -> bool:
    try:
        descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC | flags)
    except FileNotFoundError:
        return False
    try:
        attribute = _PathBeneathAttr(allowed_access=access, parent_fd=descriptor)
        _check(_SYSCALL(
            ctypes.c_long(_SYS_ADD_RULE), ctypes.c_int(ruleset),
            ctypes.c_int(_RULE_PATH_BENEATH), ctypes.byref(attribute), ctypes.c_uint32(0),
        ), "landlock_add_rule")
    finally:
        os.close(descriptor)
    return True


def create_ruleset(policy: Policy, abi: int) -> int:
    """A ruleset descriptor (close-on-exec) for ``policy``; the caller closes it."""
    if abi < MINIMUM_ABI:
        raise LandlockUnavailable(f"Landlock ABI {MINIMUM_ABI} or newer is unavailable")
    handled = filesystem_rights(abi)
    attribute = _RulesetAttr(
        handled_access_fs=handled, handled_access_net=network_rights(abi), scoped=scopes(abi),
    )
    size = 24 if abi >= 6 else 16 if abi >= 4 else 8
    ruleset = _check(int(_SYSCALL(
        ctypes.c_long(_SYS_CREATE_RULESET), ctypes.byref(attribute), ctypes.c_size_t(size),
        ctypes.c_uint32(0),
    )), "landlock_create_ruleset")
    try:
        for path in policy.executable_dirs:
            _add_rule(ruleset, path, READ_EXECUTE & handled, 0)
        for path in policy.readable_dirs:
            _add_rule(ruleset, path, READ_ONLY & handled, 0)
        for path, access in policy.files:
            if not _add_rule(ruleset, path, access & handled, os.O_NOFOLLOW):
                raise FileNotFoundError(path)
        if not _add_rule(ruleset, policy.output_dir, OUTPUT_DIRECTORY & handled,
                         os.O_DIRECTORY | os.O_NOFOLLOW):
            raise FileNotFoundError(policy.output_dir)
    except BaseException:
        os.close(ruleset)
        raise
    return ruleset


def restrict_self(ruleset: int) -> None:
    """Set ``no_new_privs`` and enforce ``ruleset`` on the calling process."""
    _check(_PRCTL(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "PR_SET_NO_NEW_PRIVS")
    _check(_SYSCALL(ctypes.c_long(_SYS_RESTRICT_SELF), ctypes.c_int(ruleset),
                    ctypes.c_uint32(0)), "landlock_restrict_self")
