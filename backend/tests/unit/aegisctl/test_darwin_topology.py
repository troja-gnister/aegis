from __future__ import annotations

import ctypes
import fcntl
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from aegisctl.mounts import ConfigError, SlotSpec, local_identity, preflight_slots, write_manifest


def _native_fixture(
    monkeypatch: pytest.MonkeyPatch,
    paths: dict[Path, tuple[Path, Path]],
    mountpoints: tuple[str, ...],
    *,
    failure: str = "",
) -> None:
    # Independently encoded Darwin SDK statfs64 ABI: size 2168, mntonname offset 88.
    payload = bytearray(2168 * len(mountpoints))
    for index, mountpoint in enumerate(mountpoints):
        encoded = mountpoint.encode() + b"\0"
        payload[index * 2168 + 88 : index * 2168 + 88 + len(encoded)] = encoded

    class Getfsstat:
        def __call__(self, buffer: object, size: int, flags: int) -> int:
            assert flags == 2  # cached, non-writing MNT_NOWAIT
            if failure == "error":
                return -1
            if not buffer:
                return 9000 if failure == "over-limit" else len(mountpoints)
            if failure == "full-buffer":
                return size // 2168
            if failure == "empty":
                return 0
            if failure == "changed-table":
                return len(mountpoints) - 1
            ctypes.memmove(buffer, bytes(payload), len(payload))  # type: ignore[arg-type]
            return len(mountpoints)

    class Library:
        def __getattr__(self, _name: str) -> Any:
            return Getfsstat()

    forms = {(path.stat().st_dev, path.stat().st_ino): values for path, values in paths.items()}
    original_fcntl = fcntl.fcntl

    def getpath(descriptor: int, command: int, argument: Any = 0) -> Any:
        if command not in (50, 102):
            return original_fcntl(descriptor, command, argument)
        if failure == "path-error":
            raise OSError("synthetic unavailable path identity")
        info = os.fstat(descriptor)
        if failure == "path-unterminated":
            return b"/" * 1024
        if failure == "path-relative":
            return b"relative\0".ljust(1024, b"\0")
        value = str(forms[(info.st_dev, info.st_ino)][command == 102]).encode() + b"\0"
        return value.ljust(1024, b"\0")

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: Library())
    monkeypatch.setattr(fcntl, "fcntl", getpath)


@pytest.mark.parametrize("spelling", [0, 1])
def test_native_darwin_mount_table_rejects_nested_roots_in_both_firmlink_spellings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spelling: int,
) -> None:
    source = tmp_path / "original"
    source.mkdir()
    forms = (source.resolve(), Path("/System/Volumes/Data/synthetic-original"))
    _native_fixture(monkeypatch, {source: forms}, ("/", str(forms[spelling] / "nested")))
    spec = SlotSpec(
        "photos", source, "/srv/aegis/roots/photos", "read_only", local_identity(source)
    )
    with pytest.raises(ConfigError, match="nested mount"):
        preflight_slots([spec])


@pytest.mark.parametrize("failure", [
    "error", "over-limit", "full-buffer", "empty", "changed-table", "path-error",
    "path-unterminated", "path-relative",
])
def test_darwin_unavailable_or_truncated_topology_rejects_before_manifest_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    source = tmp_path / "original"
    source.mkdir()
    spec = SlotSpec(
        "photos", source, "/srv/aegis/roots/photos", "read_only", local_identity(source)
    )
    slots = tuple(replace(slot, mount_fingerprint="a" * 64) for slot in preflight_slots([spec]))
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"keep")
    before = artifact.stat().st_ino
    _native_fixture(
        monkeypatch, {source: (source.resolve(), source.resolve())},
        ("/", "/unrelated"), failure=failure,
    )
    error = None
    try:
        write_manifest(artifact, slots, uid=os.geteuid(), gid=os.getegid())
    except ConfigError as caught:
        error = caught
    assert artifact.read_bytes() == b"keep"
    assert artifact.stat().st_ino == before
    assert error is not None


def test_darwin_manifest_rejects_firmlink_alias_into_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, alias = tmp_path / "original", tmp_path / "alias"
    source.mkdir()
    alias.mkdir()
    artifact = alias / "keep"
    artifact.write_bytes(b"synthetic original")
    spec = SlotSpec(
        "photos", source, "/srv/aegis/roots/photos", "read_only", local_identity(source)
    )
    slots = tuple(replace(slot, mount_fingerprint="a" * 64) for slot in preflight_slots([spec]))
    _native_fixture(
        monkeypatch,
        {
            source: (source.resolve(), Path("/System/Volumes/Data/synthetic-original")),
            alias: (source.resolve(), Path("/System/Volumes/Data/synthetic-original")),
        },
        ("/",),
    )
    before = artifact.stat().st_ino
    error = None
    try:
        write_manifest(artifact, slots, uid=os.geteuid(), gid=os.getegid())
    except ConfigError as caught:
        error = caught
    assert artifact.read_bytes() == b"synthetic original"
    assert artifact.stat().st_ino == before
    assert error is not None


def test_unsupported_host_cannot_write_manifest_without_topology(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "original"
    source.mkdir()
    spec = SlotSpec(
        "photos", source, "/srv/aegis/roots/photos", "read_only", local_identity(source)
    )
    slots = tuple(replace(slot, mount_fingerprint="a" * 64) for slot in preflight_slots([spec]))
    monkeypatch.setattr(sys, "platform", "unsupported")
    with pytest.raises(ConfigError, match=r"host.*topology"):
        write_manifest(tmp_path / "manifest", slots, uid=os.geteuid(), gid=os.getegid())


@pytest.mark.parametrize("mountpoint", ["relative", "/../opaque", "", "/" * 1024])
def test_uninterpretable_native_mountpoint_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mountpoint: str,
) -> None:
    source = tmp_path / "original"
    source.mkdir()
    _native_fixture(monkeypatch, {source: (source, source)}, (mountpoint,))
    spec = SlotSpec(
        "photos", source, "/srv/aegis/roots/photos", "read_only", local_identity(source)
    )
    with pytest.raises(ConfigError, match=r"host.*topology"):
        preflight_slots([spec])


@pytest.mark.parametrize("suffix", ["", "-sibling", "-archive/child"])
def test_darwin_allows_root_mount_and_component_siblings_without_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str,
) -> None:
    source = tmp_path / "original"
    (source / "ordinary").mkdir(parents=True)
    _native_fixture(monkeypatch, {source: (source, source)}, ("/", str(source) + suffix))

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("host mount detection must never enumerate original contents")

    monkeypatch.setattr(os, "scandir", forbidden)
    monkeypatch.setattr(os, "listdir", forbidden)
    spec = SlotSpec(
        "photos", source, "/srv/aegis/roots/photos", "read_only", local_identity(source)
    )
    assert preflight_slots([spec])[0].slot_id == "photos"
