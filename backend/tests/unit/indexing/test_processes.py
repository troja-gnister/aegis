from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from aegis_apps.indexing import processes


def test_inherited_lock_excludes_replacement_after_parent_closes(tmp_path: Path) -> None:
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        lock = processes.ScanLock.acquire(directory, "deployment.lock")
        with subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            pass_fds=(lock.fd,), close_fds=True,
        ) as child:
            try:
                lock.close()
                with pytest.raises(processes.CoordinationBusy):
                    processes.ScanLock.acquire(directory, "deployment.lock")
                assert child.stdin is not None
                child.stdin.close()
                child.wait(timeout=5)
                replacement = processes.ScanLock.acquire(directory, "deployment.lock")
                replacement.close()
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
    finally:
        os.close(directory)


def test_lock_rejects_symlink_without_following_it(tmp_path: Path) -> None:
    original = tmp_path / "original"
    original.write_bytes(b"preserve")
    (tmp_path / "deployment.lock").symlink_to(original)
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(processes.CoordinationError):
            processes.ScanLock.acquire(directory, "deployment.lock")
        assert original.read_bytes() == b"preserve"
    finally:
        os.close(directory)


def test_root_locks_exclude_only_the_exact_root(tmp_path: Path) -> None:
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    first, second = f"root-{uuid4()}.lock", f"root-{uuid4()}.lock"
    try:
        locked = processes.ScanLock.acquire(directory, first)
        try:
            with pytest.raises(processes.CoordinationBusy):
                processes.ScanLock.acquire(directory, first)
            other = processes.ScanLock.acquire(directory, second)
            other.close()
        finally:
            locked.close()
    finally:
        os.close(directory)


@pytest.mark.parametrize("name", [
    "../original", "root-name.lock", "/original", "deployment.lock/x",
])
def test_coordination_rejects_nonopaque_lock_names(tmp_path: Path, name: str) -> None:
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(processes.CoordinationError):
            processes.ScanLock.acquire(directory, name)
        assert list(tmp_path.iterdir()) == []
    finally:
        os.close(directory)
