"""Resource-free doubles for the Podman development helper tests."""
from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

Reply = tuple[Sequence[str], int, str]


class ScriptedPodman:
    """Answer each call with the next scripted reply after checking its command prefix."""

    def __init__(self, replies: Sequence[Reply]) -> None:
        self.replies = [(tuple(prefix), code, stdout) for prefix, code, stdout in replies]
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, arguments: Sequence[str], *, timeout: float = 120.0,
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        command = tuple(arguments)
        self.calls.append(command)
        assert self.replies, f"unexpected podman call {command}"
        prefix, code, stdout = self.replies.pop(0)
        assert command[: len(prefix)] == prefix, f"expected {prefix}, got {command}"
        return subprocess.CompletedProcess(list(command), code, stdout, "")

    def assert_done(self) -> None:
        assert not self.replies, f"unused scripted replies {self.replies}"


def write_proc_entry(
    proc: Path, pid: int, *, start_time: int, command: Sequence[str], state: str = "S",
) -> None:
    """A minimal /proc/<pid> with a command name that contains spaces and parentheses."""
    entry = proc / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    fields = " ".join([state, *(["0"] * 18), str(start_time), "0", "0"])
    (entry / "stat").write_text(f"{pid} (podman (svc) x) {fields}\n", encoding="ascii")
    (entry / "cmdline").write_bytes(b"\0".join(part.encode() for part in command) + b"\0")


def listing_replies(
    containers: str = "[]", volumes: str = "null", networks: str = "[]", images: str = "[]",
) -> list[Reply]:
    return [
        (("ps", "--all", "--format", "json"), 0, containers),
        (("volume", "ls", "--format", "json"), 0, volumes),
        (("network", "ls", "--format", "json"), 0, networks),
        (("images", "--all", "--format", "json"), 0, images),
    ]
