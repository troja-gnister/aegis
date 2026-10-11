"""A seccomp filter for the decoding child: what Landlock cannot restrict (R3).

Landlock does not mediate ``chmod``, ``chown``, ``utime`` or extended attributes, so a
compromised child could change the modes of scratch directories it can reach by name. This
classic BPF filter, installed in the pre-exec hook after Landlock (``no_new_privs`` is already
set), makes the child and everything it starts unable to:

- change file metadata: ``chmod``, ``fchmod``, ``fchmodat``, ``fchmodat2``, ``chown``,
  ``fchown``, ``lchown``, ``fchownat``, ``utime``, ``utimes``, ``utimensat``, ``futimesat``,
  ``setxattr``, ``lsetxattr``, ``fsetxattr``, ``setxattrat``, ``removexattr``,
  ``lremovexattr``, ``fremovexattr``, ``removexattrat``;
- reach other processes or the kernel's wider surface: ``ptrace``, ``process_vm_readv``,
  ``process_vm_writev``, ``kexec_load``, ``kexec_file_load``, ``bpf``, ``perf_event_open``,
  ``keyctl``, ``add_key``, ``request_key``, ``personality``;
- change namespaces or mounts: ``unshare``, ``setns``, ``mount``, ``umount2``, ``pivot_root``,
  ``chroot``, and ``clone`` with any ``CLONE_NEW*`` flag. ``clone3`` answers ``ENOSYS``
  because its flags sit in memory a classic filter cannot read; glibc then falls back to
  ``clone`` for threads and ``posix_spawn`` (the same approach as Docker's default profile).

Denied calls fail with ``EPERM``; everything else is allowed. A syscall from another
architecture, or from the x32 ABI, kills the process. The filter is for x86-64 only; elsewhere
the processor refuses to start.

Neither the decoders nor the tools need a denied call: Pillow and pillow-heif write outputs
through an already-open file object, ffmpeg's ``file`` protocol creates outputs with ``open``
and a mode and never changes metadata afterwards, ffprobe writes nothing, and Python's startup
and ``subprocess`` (fork, exec, pipes) use none of them.
"""

from __future__ import annotations

import ctypes
import platform
from typing import Final

_PR_SET_SECCOMP: Final = 22
_SECCOMP_MODE_FILTER: Final = 2
AUDIT_ARCH_X86_64: Final = 0xC000003E
X32_SYSCALL_BIT: Final = 0x40000000
RET_KILL_PROCESS: Final = 0x80000000
RET_ERRNO: Final = 0x00050000
RET_ALLOW: Final = 0x7FFF0000
EPERM: Final = 1
ENOSYS: Final = 38
# clone's CLONE_NEW* flags (CLONE_NEWTIME is valid only for clone3 and unshare).
CLONE_NAMESPACE_FLAGS: Final = 0x7E020000

_LD_W_ABS: Final = 0x20
_JEQ_K: Final = 0x15
_JGE_K: Final = 0x35
_JSET_K: Final = 0x45
_RET_K: Final = 0x06
_OFFSET_NR: Final = 0
_OFFSET_ARCH: Final = 4
_OFFSET_ARG0_LOW: Final = 16

# x86-64 syscall numbers.
DENIED: Final[dict[str, int]] = {
    "chmod": 90, "fchmod": 91, "fchmodat": 268, "fchmodat2": 452,
    "chown": 92, "fchown": 93, "lchown": 94, "fchownat": 260,
    "utime": 132, "utimes": 235, "utimensat": 280, "futimesat": 261,
    "setxattr": 188, "lsetxattr": 189, "fsetxattr": 190, "setxattrat": 463,
    "removexattr": 197, "lremovexattr": 198, "fremovexattr": 199, "removexattrat": 466,
    "ptrace": 101, "process_vm_readv": 310, "process_vm_writev": 311,
    "kexec_load": 246, "kexec_file_load": 320, "bpf": 321, "perf_event_open": 298,
    "keyctl": 250, "add_key": 248, "request_key": 249, "personality": 135,
    "unshare": 272, "setns": 308, "mount": 165, "umount2": 166, "pivot_root": 155,
    "chroot": 161,
}
SYS_CLONE: Final = 56
SYS_CLONE3: Final = 435


class SeccompUnavailable(RuntimeError):
    """This platform has no filter (not x86-64), so the processor will not start."""


class _SockFilter(ctypes.Structure):
    _fields_ = (
        ("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    )


class _SockFprog(ctypes.Structure):
    _fields_ = (("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter)))


def require() -> None:
    if platform.machine() != "x86_64":
        raise SeccompUnavailable("the seccomp filter exists for x86-64 only: not starting")


def program() -> list[tuple[int, int, int, int]]:
    """The filter as (code, jt, jf, k) instructions.

    Classic BPF jumps only forward, so the checks come first and the shared returns last:
    arch, x32, one comparison per denied call, clone3, clone and its flags, then allow, deny
    (EPERM), ENOSYS and kill.
    """
    denied = sorted(set(DENIED.values()))
    count = len(denied)
    clone_flags_at = 6 + count
    allow_at = clone_flags_at + 2
    deny_at = allow_at + 1
    enosys_at = deny_at + 1
    kill_at = enosys_at + 1

    def jump(source: int, target: int) -> int:
        offset = target - source - 1
        if not 0 <= offset <= 255:
            raise ValueError("seccomp jump out of range")
        return offset

    body: list[tuple[int, int, int, int]] = [
        (_LD_W_ABS, 0, 0, _OFFSET_ARCH),
        (_JEQ_K, 0, jump(1, kill_at), AUDIT_ARCH_X86_64),
        (_LD_W_ABS, 0, 0, _OFFSET_NR),
        (_JGE_K, jump(3, kill_at), 0, X32_SYSCALL_BIT),
    ]
    for index, number in enumerate(denied):
        body.append((_JEQ_K, jump(4 + index, deny_at), 0, number))
    body.append((_JEQ_K, jump(4 + count, enosys_at), 0, SYS_CLONE3))
    body.append((_JEQ_K, 0, jump(5 + count, allow_at), SYS_CLONE))
    body.append((_LD_W_ABS, 0, 0, _OFFSET_ARG0_LOW))
    body.append((_JSET_K, jump(clone_flags_at + 1, deny_at), 0, CLONE_NAMESPACE_FLAGS))
    body.append((_RET_K, 0, 0, RET_ALLOW))
    body.append((_RET_K, 0, 0, RET_ERRNO | EPERM))
    body.append((_RET_K, 0, 0, RET_ERRNO | ENOSYS))
    body.append((_RET_K, 0, 0, RET_KILL_PROCESS))
    if len(body) != kill_at + 1 or body[allow_at][3] != RET_ALLOW:
        raise AssertionError("seccomp program layout changed")
    return body


class Filter:
    """A built filter, kept alive in the parent so the pre-exec hook allocates nothing."""

    def __init__(self) -> None:
        require()
        instructions = program()
        self._array = (_SockFilter * len(instructions))(
            *(_SockFilter(code, jt, jf, k) for code, jt, jf, k in instructions)
        )
        self._program = _SockFprog(
            len(instructions), ctypes.cast(self._array, ctypes.POINTER(_SockFilter)),
        )
        self._prctl = ctypes.CDLL(None, use_errno=True).prctl

    def install(self) -> None:
        """Apply to the calling process (``no_new_privs`` must already be set)."""
        if self._prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER,
                       ctypes.byref(self._program), 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, "seccomp filter installation failed")
