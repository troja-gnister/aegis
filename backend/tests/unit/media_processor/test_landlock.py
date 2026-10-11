"""Landlock and seccomp confinement of the decoding child (C1, R1-R3).

These run on the host and again inside the aegis-media image at phase end
(tests/deployment/test_media_image.py), where the container's own seccomp profile must also
allow the Landlock syscalls. They need x86-64 and Landlock ABI 3 or newer; without them they
fail, as the processor refuses to start.
"""

from __future__ import annotations

import json
import os
import platform
import socket
import sys
from pathlib import Path
from typing import Any

import pytest
from aegis_media_processor import landlock, loop, seccomp
from aegis_media_processor.sandbox import ChildLimits, run, scrubbed_environment

from .support import make_roots, write_source

LIMITS = ChildLimits(memory_bytes=1 << 30, cpu_seconds=30, wall_seconds=30, output_bytes=1 << 20)
JOB_ID = "a" * 32

PROBE = r'''
import ctypes, errno, json, os, socket, sys
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
def attempt(action):
    try:
        action()
        return "ok"
    except OSError as error:
        return errno.errorcode.get(error.errno, str(error.errno))
def native(result):
    if result == -1:
        raise OSError(ctypes.get_errno(), "native call failed")
def spawn(path):
    from aegis_media_processor.sandbox import ChildLimits, run
    completed = run([path], ChildLimits(1 << 29, 5, 10, 1 << 20, nice=0), cwd=os.getcwd())
    if completed.returncode != 0:
        raise OSError(errno.EIO, "tool failed")
def unix(path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(path)
def write(path):
    with open(path, "wb") as handle:
        handle.write(b"x")
def read(path):
    with open(path, "rb") as handle:
        handle.read(1)
checks = json.loads(sys.argv[1])
report = {}
for name, (kind, *values) in checks.items():
    if kind == "read":
        report[name] = attempt(lambda: read(values[0]))
    elif kind == "write":
        report[name] = attempt(lambda: write(values[0]))
    elif kind == "append":
        report[name] = attempt(lambda: open(values[0], "ab").close())
    elif kind == "list":
        report[name] = attempt(lambda: os.listdir(values[0]))
    elif kind == "mkdir":
        report[name] = attempt(lambda: os.mkdir(values[0]))
    elif kind == "symlink":
        report[name] = attempt(lambda: os.symlink("/etc/hostname", values[0]))
    elif kind == "fd":
        report[name] = attempt(lambda: os.pread(int(values[0]), 2, 0))
    elif kind == "connect":
        report[name] = attempt(lambda: socket.create_connection(("127.0.0.1", values[0]), 5))
    elif kind == "bind":
        report[name] = attempt(lambda: socket.socket().bind(("127.0.0.1", 0)))
    elif kind == "signal":
        report[name] = attempt(lambda: os.kill(values[0], 0))
    elif kind == "udp":
        report[name] = attempt(
            lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("127.0.0.1", 9)))
    elif kind == "unix":
        report[name] = attempt(lambda: unix(values[0]))
    elif kind == "spawn":
        report[name] = attempt(lambda: spawn(values[0]))
    elif kind == "chmod":
        report[name] = attempt(lambda: os.chmod(values[0], 0o700))
    elif kind == "utime":
        report[name] = attempt(lambda: os.utime(values[0]))
    elif kind == "setxattr":
        report[name] = attempt(lambda: os.setxattr(values[0], "user.aegis", b"x"))
    elif kind == "unshare":
        report[name] = attempt(lambda: os.unshare(os.CLONE_NEWUSER))
    elif kind == "ptrace":
        report[name] = attempt(lambda: native(libc.ptrace(0, 0, None, None)))
    elif kind == "clone3":
        report[name] = attempt(lambda: native(libc.syscall(ctypes.c_long(435), None,
                                                           ctypes.c_size_t(0))))
print(json.dumps(report))
'''


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    roots = make_roots(tmp_path)
    write_source(roots, b"album/photo.jpg", b"\xff\xd8\xff\xe0 synthetic")
    write_source(roots, b"private/other.jpg", b"\xff\xd8\xff\xe0 another root's file")
    scratch = tmp_path / "scratch"
    directory = scratch / "work" / JOB_ID
    (directory / "out").mkdir(parents=True)
    (scratch / "inbox").mkdir()
    (directory / "job.json").write_text("{}", encoding="utf-8")
    return {"roots": roots, "scratch": scratch, "directory": directory,
            "out": directory / "out", "tmp": tmp_path}


def confined(layout: dict[str, Path], checks: dict[str, list[Any]],
             pass_fds: tuple[int, ...] = ()) -> dict[str, str]:
    """Run the probe under exactly the policy the loop builds for a job."""
    policy = landlock.child_policy(
        str(layout["directory"] / "job.json"), str(layout["out"]),
        (str(layout["roots"]), str(layout["scratch"])),
    )
    ruleset = landlock.create_ruleset(policy, landlock.require())
    try:
        completed = run(
            [sys.executable, "-I", "-c", PROBE, json.dumps(checks)], LIMITS,
            cwd=str(layout["out"]),
            environment=scrubbed_environment({"TMPDIR": str(layout["out"])}),
            pass_fds=pass_fds, landlock_ruleset=ruleset, seccomp_filter=seccomp.Filter(),
        )
    finally:
        os.close(ruleset)
    assert completed.returncode == 0, completed
    report: dict[str, str] = json.loads(completed.stdout)
    return report


def test_landlock_is_available_and_handles_every_known_right() -> None:
    abi = landlock.require()

    assert abi >= landlock.MINIMUM_ABI == 3
    assert landlock.HIGHEST_KNOWN_ABI == 10
    abi1 = (1 << 13) - 1
    assert landlock.filesystem_rights(3) == abi1 | 1 << 13 | 1 << 14
    assert landlock.filesystem_rights(5) == abi1 | 1 << 13 | 1 << 14 | 1 << 15
    assert landlock.filesystem_rights(8) == landlock.filesystem_rights(5)
    assert landlock.filesystem_rights(9) == (1 << 17) - 1
    assert landlock.filesystem_rights(10) == landlock.filesystem_rights(9)
    assert [landlock.network_rights(version) for version in (3, 4, 9, 10)] == [0, 3, 3, 15]
    assert [landlock.scopes(version) for version in (5, 6, 10)] == [0, 3, 3]


def test_the_child_reads_its_descriptor_but_no_root_path(layout: dict[str, Path]) -> None:
    roots = layout["roots"]
    descriptor = os.open(roots / "synthetic" / "album" / "photo.jpg", os.O_RDONLY)
    try:
        report = confined(layout, {
            "descriptor": ["fd", descriptor],
            "reopen": ["read", f"/proc/self/fd/{descriptor}"],
            "same_root": ["read", str(roots / "synthetic" / "album" / "photo.jpg")],
            "other_root": ["read", str(roots / "synthetic" / "private" / "other.jpg")],
            "list_roots": ["list", str(roots)],
            "job": ["read", str(layout["directory"] / "job.json")],
            "system": ["read", "/etc/ld.so.cache" if os.path.exists("/etc/ld.so.cache")
                       else "/dev/null"],
        }, pass_fds=(descriptor,))
    finally:
        os.close(descriptor)

    assert report == {
        "descriptor": "ok",
        # A magic-link reopen is checked against the original's path: this is why ffmpeg reads
        # the descriptor through fd: rather than /proc/self/fd/<n>.
        "reopen": "EACCES",
        "same_root": "EACCES",
        "other_root": "EACCES",
        "list_roots": "EACCES",
        "job": "ok",
        "system": "ok",
    }


def test_the_child_writes_only_beneath_its_output_directory(layout: dict[str, Path]) -> None:
    out, directory, scratch = layout["out"], layout["directory"], layout["scratch"]

    report = confined(layout, {
        "output": ["write", str(out / "thumbnail.webp")],
        "job_rewrite": ["append", str(directory / "job.json")],
        "job_dir": ["write", str(directory / "planted")],
        "inbox": ["write", str(scratch / "inbox" / ("b" * 32 + ".json"))],
        "elsewhere": ["write", str(layout["tmp"] / "escaped")],
        "subdirectory": ["mkdir", str(out / "nested")],
        "symlink": ["symlink", str(out / "link")],
    })

    assert report == {
        "output": "ok", "job_rewrite": "EACCES", "job_dir": "EACCES", "inbox": "EACCES",
        "elsewhere": "EACCES", "subdirectory": "EACCES", "symlink": "EACCES",
    }
    assert (out / "thumbnail.webp").read_bytes() == b"x"
    assert sorted(os.listdir(directory)) == ["job.json", "out"]
    assert (directory / "job.json").read_text(encoding="utf-8") == "{}"


def test_the_child_cannot_reach_the_network(layout: dict[str, Path]) -> None:
    assert landlock.require() >= 4, "Landlock ABI 4 (TCP rules) is required"
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        report = confined(layout, {
            "connect": ["connect", listener.getsockname()[1]],
            "bind": ["bind"],
        })
    finally:
        listener.close()

    assert report == {"connect": "EACCES", "bind": "EACCES"}


def test_signals_leave_the_childs_domain_only_where_the_abi_scopes_them(
    layout: dict[str, Path],
) -> None:
    report = confined(layout, {"parent": ["signal", os.getpid()]})

    # Signal 0 only checks permission. ABI 6 scopes signals to the child's own domain.
    assert report == {"parent": "EPERM" if landlock.require() >= 6 else "ok"}


def test_a_policy_never_makes_a_root_or_scratch_readable(layout: dict[str, Path]) -> None:
    with pytest.raises(landlock.LandlockUnavailable):
        landlock.child_policy(str(layout["directory"] / "job.json"), str(layout["out"]),
                              ("/usr/share",))
    policy = landlock.child_policy(str(layout["directory"] / "job.json"), str(layout["out"]),
                                   (str(layout["roots"]), str(layout["scratch"])))
    assert policy.output_dir == str(layout["out"])
    assert "/usr" in policy.executable_dirs and "/etc" in policy.readable_dirs
    assert set(policy.files) == {
        ("/dev/null", landlock.ACCESS_FS_READ_FILE | landlock.ACCESS_FS_WRITE_FILE),
        ("/dev/urandom", landlock.ACCESS_FS_READ_FILE),
        (str(layout["directory"] / "job.json"), landlock.ACCESS_FS_READ_FILE),
    }
    # The reverse direction too: nothing readable may lie inside the roots or scratch.
    with pytest.raises(landlock.LandlockUnavailable):
        landlock.child_policy(str(layout["directory"] / "job.json"), str(layout["out"]),
                              (os.path.dirname(os.path.realpath(sys.prefix)),))


@pytest.mark.parametrize("abi", [0, 1, 2])
def test_without_landlock_abi_3_the_processor_fails_closed(
    layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch, abi: int,
) -> None:
    monkeypatch.setattr(landlock, "abi_version", lambda: abi)
    scratch = layout["tmp"] / "fresh-scratch"
    scratch.mkdir()

    with pytest.raises(landlock.LandlockUnavailable):
        landlock.require()
    with pytest.raises(landlock.LandlockUnavailable):
        landlock.create_ruleset(landlock.child_policy(
            str(layout["directory"] / "job.json"), str(layout["out"]), ()), abi)
    with loop.Processor(str(scratch), str(layout["roots"])) as processor, pytest.raises(
        landlock.LandlockUnavailable,
    ):
        processor.run(until_idle=True)
    assert loop.main(["--scratch", str(scratch), "--roots", str(layout["roots"])]) == 3
    assert os.listdir(scratch) == []


def test_without_the_seccomp_filter_the_processor_fails_closed(
    layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    scratch = layout["tmp"] / "fresh-scratch"
    scratch.mkdir()

    with pytest.raises(seccomp.SeccompUnavailable):
        seccomp.Filter()
    assert loop.main(["--scratch", str(scratch), "--roots", str(layout["roots"])]) == 3
    assert os.listdir(scratch) == []


def test_the_child_starts_tools_with_dev_null_as_their_stdio(layout: dict[str, Path]) -> None:
    """R1: subprocess opens /dev/null read-write for ffmpeg's stdin and stderr."""
    report = confined(layout, {
        "tool": ["spawn", "/bin/true"],
        "dev_null": ["write", "/dev/null"],
        "urandom": ["read", "/dev/urandom"],
        "zero": ["read", "/dev/zero"],
    })

    assert report == {"tool": "ok", "dev_null": "ok", "urandom": "ok", "zero": "EACCES"}


def test_the_child_cannot_change_metadata_trace_or_unshare(layout: dict[str, Path]) -> None:
    out, scratch = layout["out"], layout["scratch"]
    (out / "made-by-the-loop.webp").write_bytes(b"x")
    modes = {path: path.stat().st_mode for path in (out, scratch / "inbox")}

    report = confined(layout, {
        "write": ["write", str(out / "thumbnail.webp")],
        "chmod_output": ["chmod", str(out / "made-by-the-loop.webp")],
        "chmod_inbox": ["chmod", str(scratch / "inbox")],
        "chmod_out": ["chmod", str(out)],
        "utime": ["utime", str(out / "made-by-the-loop.webp")],
        "setxattr": ["setxattr", str(out / "made-by-the-loop.webp")],
        "unshare": ["unshare"],
        "ptrace": ["ptrace"],
        "clone3": ["clone3"],
        "tool": ["spawn", "/bin/true"],
    })

    assert report == {
        "write": "ok", "chmod_output": "EPERM", "chmod_inbox": "EPERM", "chmod_out": "EPERM",
        "utime": "EPERM", "setxattr": "EPERM", "unshare": "EPERM", "ptrace": "EPERM",
        # Threads and posix_spawn fall back to clone, whose namespace flags are checked.
        "clone3": "ENOSYS", "tool": "ok",
    }
    assert {path: path.stat().st_mode for path in modes} == modes


def test_udp_and_pathname_unix_sockets_are_denied_where_the_abi_handles_them(
    layout: dict[str, Path],
) -> None:
    abi = landlock.require()
    path = layout["tmp"] / "host.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    try:
        report = confined(layout, {"udp": ["udp"], "unix": ["unix", str(path)]})
    finally:
        server.close()

    assert report == {
        "udp": "EACCES" if abi >= 10 else "ok",
        "unix": "EACCES" if abi >= 9 else "ok",
    }


def test_the_seccomp_program_checks_the_architecture_first() -> None:
    instructions = seccomp.program()

    assert instructions[0] == (0x20, 0, 0, 4)
    assert instructions[1][3] == seccomp.AUDIT_ARCH_X86_64
    assert instructions[-1] == (0x06, 0, 0, seccomp.RET_KILL_PROCESS)
    denied = {k for code, _, _, k in instructions if code == 0x15}
    assert set(seccomp.DENIED.values()) <= denied
    assert {seccomp.SYS_CLONE, seccomp.SYS_CLONE3} <= denied
    assert len(instructions) == len(set(seccomp.DENIED.values())) + 12


def test_every_seccomp_jump_is_forward_and_lands_inside_the_program() -> None:
    instructions = seccomp.program()
    jumps = {0x15, 0x35, 0x45}

    for index, (code, jt, jf, _) in enumerate(instructions):
        if code in jumps:
            for offset in (jt, jf):
                assert 0 <= offset <= 255
                assert index + 1 + offset < len(instructions)
        else:
            assert (jt, jf) == (0, 0)
    # Every path ends in a return: the last four instructions are the shared returns.
    assert [code for code, _, _, _ in instructions[-4:]] == [0x06] * 4
    assert [k for _, _, _, k in instructions[-4:]] == [
        seccomp.RET_ALLOW, seccomp.RET_ERRNO | seccomp.EPERM, seccomp.RET_ERRNO | seccomp.ENOSYS,
        seccomp.RET_KILL_PROCESS,
    ]


def test_a_confinement_build_error_is_a_refusal_not_a_traceback(
    layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken() -> list[tuple[int, int, int, int]]:
        raise ValueError("seccomp jump out of range")

    monkeypatch.setattr(seccomp, "program", broken)
    scratch = layout["tmp"] / "fresh-scratch"
    scratch.mkdir()

    assert loop.main(["--scratch", str(scratch), "--roots", str(layout["roots"])]) == 3
    assert os.listdir(scratch) == []
