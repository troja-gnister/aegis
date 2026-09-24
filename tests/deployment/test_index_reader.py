"""Linux reader evidence on owned read-only binds with no capabilities or network."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import uuid
from pathlib import Path

import pytest
from aegisctl.container_engine import container_command

from tests.support.container_runtime import (
    FreshTestTree,
    prepare_owned_test_inventory,
    record_created_test_path,
    record_fresh_test_tree,
    record_test_tree_inventory,
)

REPOSITORY = Path(__file__).resolve().parents[2]
CONTAINER_COMMAND = container_command()

# Executed in the exact container carrying the test mounts, not a second probe.
PROBE = r'''
import builtins
import json
import os
import stat
import sys
from aegis_apps.indexing import reader
from aegis_apps.indexing.protocol import ReaderBatch, ReaderComplete, ReaderFailure, encode_message
from aegisctl.mounts import parse_mountinfo

root = "/srv/aegis/roots/synthetic"
mode = sys.argv[1]
records = parse_mountinfo(open("/proc/self/mountinfo", "rb").read(1048577))
assert root in records and records[root].effective_mode == "read_only", "SETUP: root bind absent"
assert os.geteuid() != 0
status = open("/proc/self/status").read()
assert "CapEff:\t0000000000000000" in status
assert "NoNewPrivs:\t1" in status
fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
baseline_fds = len(os.listdir("/proc/self/fd"))
try:
    if mode != "leaf":
        nested = root + "/" + mode
        # A missing injection is a setup failure, never successful rejection.
        assert nested in records, "SETUP: requested descendant mount absent from mountinfo"
        assert os.path.isfile(nested + "/nested-sentinel"), "SETUP: descendant sentinel absent"
        assert os.stat(root).st_dev == os.stat(nested).st_dev, "SETUP: bind is not same-device"
        nested_fd = os.open(nested, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            assert reader._mount_id(nested_fd) != reader._mount_id(fd), "SETUP: mount IDs equal"
        finally:
            os.close(nested_fd)
        messages = list(reader.read_directory(fd, (), 100))
        assert messages == [ReaderFailure("source_unavailable")], messages
        print(json.dumps({"setup": "same-device descendant confirmed", "result": "rejected"}))
    else:
        assert not any(path.startswith(root + "/") for path in records), "SETUP: not a leaf"
        original_open, original_builtin = os.open, builtins.open
        def guarded_open(path, flags, *args, **kwargs):
            assert flags & os.O_DIRECTORY, "reader opened source content"
            assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            return original_open(path, flags, *args, **kwargs)
        def guarded_builtin(path, *args, **kwargs):
            assert str(path).startswith(("/proc/self/mountinfo", "/proc/self/fdinfo/"))
            return original_builtin(path, *args, **kwargs)
        def forbidden(*args, **kwargs):
            raise AssertionError("reader attempted an original mutation")
        os.open, builtins.open = guarded_open, guarded_builtin
        for name in ("unlink", "rename", "replace", "remove", "chmod", "write"):
            setattr(os, name, forbidden)
        for pass_number in range(30):
            iterator = reader.read_directory(fd, (), 100)
            first = next(iterator)
            assert isinstance(first, ReaderBatch)
            if pass_number % 2:
                iterator.close()
            else:
                messages = [first, *iterator]
                assert isinstance(messages[-1], ReaderComplete), messages[-1]
                entries = {entry.name.raw: entry for message in messages
                           if isinstance(message, ReaderBatch) for entry in message.observations}
                assert len(entries) == 111, len(entries)
                assert entries[b"dangling"].kind == "symlink"
                assert entries[b"ancestor"].kind == "symlink"
                assert entries[b"fifo"].kind == "special"
                assert entries[b"socket"].kind == "special"
                assert entries[b"keep0"].size == 16
                assert entries[b"line\nbreak"].name.display == r"line\u000abreak"
                assert all(len(encode_message(message)) <= 1048576 for message in messages)
            assert len(os.listdir("/proc/self/fd")) == baseline_fds, "reader FD leak"
            os.fstat(fd)
        # Linux device metadata is classified without opening the device.
        class Device:
            name = "synthetic-device"
            def stat(self, *, follow_symlinks):
                assert follow_symlinks is False
                return os.stat("/dev/null", follow_symlinks=False)
        item, error = reader._observe(Device())
        assert error is None and item.kind == "special" and item.state == "unsupported"
        print(json.dumps({"passes": 30, "fd_baseline": baseline_fds,
                          "fd_final": len(os.listdir("/proc/self/fd")), "result": "preserved"}))
finally:
    os.close(fd)
'''


def run_probe(
    source: Path, external: Path, descendant: str, read_only: bool, tree: FreshTestTree,
) -> dict[str, object]:
    prepare_owned_test_inventory(record_test_tree_inventory(tree))
    name = f"aegis-reader-{uuid.uuid4().hex}"
    command = [
        *CONTAINER_COMMAND, "run", "--name", name, "--label", f"aegis.reader.owner={name}",
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--user", f"{os.geteuid()}:{os.getegid()}",
        "--memory", "128m", "--pids-limit", "32", "--mount",
        f"type=bind,src={source},dst=/srv/aegis/roots/synthetic,readonly",
    ]
    if descendant != "leaf":
        command += ["--mount", f"type=bind,src={external},"
                    f"dst=/srv/aegis/roots/synthetic/{descendant}"
                    + (",readonly" if read_only else "")]
    command += ["--entrypoint", "python", "aegis-backend", "-c", PROBE, descendant]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)
    finally:
        inspection = subprocess.run([*CONTAINER_COMMAND, "inspect", name], capture_output=True,
                                    text=True, timeout=20, check=False)
        if inspection.returncode == 0:
            owned = json.loads(inspection.stdout)[0]
            assert owned["Config"]["Labels"]["aegis.reader.owner"] == name
            subprocess.run([*CONTAINER_COMMAND, "rm", "--force", owned["Id"]], capture_output=True,
                           text=True, timeout=20, check=True)
            absent = subprocess.run(
                [*CONTAINER_COMMAND, "inspect", owned["Id"]], capture_output=True,
                timeout=20, check=False,
            )
            assert absent.returncode != 0, "owned reader container cleanup failed"


@pytest.mark.parametrize("descendant,read_only", [
    ("leaf", True), ("nested", True), ("nested", False),
    ("deep/nested", True), ("deep/nested", False),
])
def test_actual_read_only_reader_and_same_container_mount_preconditions(
    tmp_path: Path, descendant: str, read_only: bool,
) -> None:
    tree = record_fresh_test_tree(tmp_path)
    source, external = tmp_path / "source", tmp_path / "external"
    source.mkdir()
    external.mkdir()
    (source / "nested").mkdir()
    (source / "deep/nested").mkdir(parents=True)
    for number in range(104):
        (source / f"keep{number}").write_bytes(b"preserve exactly")
    (source / "line\nbreak").write_bytes(b"raw name")
    (source / "dangling").symlink_to("missing")
    (source / "ancestor").symlink_to("..")
    os.mkfifo(source / "fifo")
    sock = socket.socket(socket.AF_UNIX)
    original_cwd = os.getcwd()
    try:
        os.chdir(source)
        sock.bind("socket")
    finally:
        os.chdir(original_cwd)
    (external / "nested-sentinel").write_bytes(b"nested original preserved")
    record_created_test_path(tree, source, recursive=True)
    record_created_test_path(tree, external, recursive=True)
    before = {str(p.relative_to(tmp_path)): (p.lstat().st_size, p.lstat().st_mtime_ns)
              for p in tmp_path.rglob("*")}
    try:
        result = run_probe(source, external, descendant, read_only, tree)
        assert result["result"] == ("preserved" if descendant == "leaf" else "rejected")
        print(json.dumps(result, sort_keys=True))
    finally:
        sock.close()
        assert {str(p.relative_to(tmp_path)): (p.lstat().st_size, p.lstat().st_mtime_ns)
                for p in tmp_path.rglob("*")} == before
        assert all((source / f"keep{number}").read_bytes() == b"preserve exactly"
                   for number in range(104))
        assert (external / "nested-sentinel").read_bytes() == b"nested original preserved"
