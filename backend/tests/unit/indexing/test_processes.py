from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
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


@pytest.mark.parametrize("failure", ["nonblocking", "thread_start"])
def test_post_spawn_failure_retains_owned_handle_and_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    deployment = processes.ScanLock.acquire(directory, "deployment.lock")
    root_id = uuid4()
    root_lock = processes.ScanLock.acquire(directory, f"root-{root_id}.lock")
    coordination = cast(processes.Coordination, SimpleNamespace(
        deployment=deployment, root=lambda root: root_lock,
    ))
    original_spawn = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = original_spawn([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    def broken(*args: object) -> None:
        raise RuntimeError("synthetic post-spawn failure")

    monkeypatch.setattr("aegis_apps.indexing.processes.subprocess.Popen", spawn)
    if failure == "nonblocking":
        monkeypatch.setattr("aegis_apps.indexing.processes.os.set_blocking", broken)
    else:
        monkeypatch.setattr("aegis_apps.indexing.processes.Thread.start", broken)
    try:
        with pytest.raises(RuntimeError) as caught:
            processes.ProcessReader(
                processes.ReaderSource("/srv/aegis/roots/synthetic", "a" * 64, (), 500),
                coordination, root_id,
            )
        reader = getattr(caught.value, "reader", None)
        assert reader is not None, "owned child was lost by exceptional construction"
        assert reader.process is children[0] and root_lock.fd >= 0
        with pytest.raises(processes.CoordinationBusy):
            processes.ScanLock.acquire(directory, f"root-{root_id}.lock")
        reader.kill()
        reader.process.wait(timeout=5)
        assert reader.reaped()
        assert root_lock.fd == -1
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            assert child.stdin is not None and child.stdout is not None
            child.stdin.close()
            child.stdout.close()
        root_lock.close()
        deployment.close()
        os.close(directory)
