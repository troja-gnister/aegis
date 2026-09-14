"""Owned synthetic files only; guards are installed after fixture creation."""

import builtins
import os
import socket
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast

import pytest
from aegis_apps.indexing import reader
from aegis_apps.indexing.protocol import ReaderBatch, ReaderComplete, ReaderFailure, ReaderMessage


@pytest.mark.parametrize("outcome", ["success", "cancel", "error", "mount_loss"])
def test_bytes_locations_names_size_mtime_survive_reader_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    root = tmp_path / "original"
    root.mkdir()
    for number in range(105):
        (root / f"keep{number}").write_bytes(b"preserve exactly")
    (root / "dangling").symlink_to("missing")
    (root / "ancestor").symlink_to("..")
    os.mkfifo(root / "fifo")
    sock = socket.socket(socket.AF_UNIX)
    # AF_UNIX paths are bounded to about 100 bytes. Bind relative to the newly
    # owned fixture directory, then restore cwd before installing reader guards.
    original_cwd = os.getcwd()
    try:
        os.chdir(root)
        sock.bind("socket")
    finally:
        os.chdir(original_cwd)
    before = {p.name: (p.lstat().st_size, p.lstat().st_mtime_ns) for p in root.iterdir()}
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    baseline_fds = len(os.listdir("/dev/fd"))
    real_open = os.open

    def directory_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        assert flags & os.O_DIRECTORY, "file content opened"
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        return real_open(path, flags, *args, **kwargs)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("original mutation or content open")

    try:
        with monkeypatch.context() as guards:
            guards.setattr(reader, "_mount_snapshot", lambda fd: ("synthetic", 10, "fixed"))
            guards.setattr(reader, "_mount_id", lambda fd: 10)
            guards.setattr(os, "open", directory_open)
            guards.setattr(builtins, "open", forbidden)
            for operation in ("unlink", "rename", "replace", "remove", "chmod", "write"):
                guards.setattr(os, operation, forbidden)
            iterator = cast(Generator[ReaderMessage], reader.read_directory(fd, (), 100))
            assert isinstance(next(iterator), ReaderBatch)
            if outcome == "cancel":
                iterator.close()
            else:
                if outcome == "error":
                    guards.setattr(reader, "directory_identity", lambda fd: (_ for _ in ()).throw(
                        OSError("private failure")
                    ))
                elif outcome == "mount_loss":
                    guards.setattr(reader, "_mount_snapshot", lambda fd: (_ for _ in ()).throw(
                        OSError("mount lost")
                    ))
                messages = list(iterator)
                if outcome == "success":
                    assert isinstance(messages[-1], ReaderComplete)
                else:
                    assert isinstance(messages[-1], ReaderFailure)
            os.fstat(fd)  # Ownership stays with caller.
            assert len(os.listdir("/dev/fd")) == baseline_fds
        assert {p.name: (p.lstat().st_size, p.lstat().st_mtime_ns)
                for p in root.iterdir()} == before
        assert all((root / f"keep{number}").read_bytes() == b"preserve exactly"
                   for number in range(105))
        assert os.readlink(root / "dangling") == "missing"
        assert os.readlink(root / "ancestor") == ".."
    finally:
        os.close(fd)
        sock.close()
