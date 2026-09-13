from __future__ import annotations

import io
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from aegisctl.mounts import (
    ConfigError,
    MountAttestationError,
    SlotSpec,
    local_identity,
    observe_mount_fingerprints,
    parse_mountinfo,
    preflight_slots,
    write_manifest,
)


def _slot(path: Path, slot_id: str) -> SlotSpec:
    return SlotSpec(slot_id, path, f"/srv/aegis/roots/{slot_id}", "read_only", local_identity(path))


@pytest.mark.parametrize("child_root", ["/tree/private", "/tree/archive\\040files"])
def test_preflight_rejects_physical_ancestor_through_bind_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child_root: str,
) -> None:
    parent, alias = tmp_path / "tree", tmp_path / "private-alias"
    parent.mkdir()
    alias.mkdir()
    records = (
        f"1 0 0:1 / / rw - tmpfs tmpfs rw\n"
        f"2 1 8:1 /tree {parent.resolve()} ro - ext4 /dev/sda rw\n"
        f"3 1 8:1 {child_root} {alias.resolve()} ro - ext4 /dev/sda rw\n"
    ).encode()
    real_open = Path.open

    def fixture_mountinfo(path: Path, *args: Any, **kwargs: Any) -> Any:
        return (
            io.BytesIO(records)
            if str(path) == "/proc/self/mountinfo"
            else real_open(path, *args, **kwargs)
        )

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "open", fixture_mountinfo)
    with pytest.raises(ConfigError, match="overlap"):
        preflight_slots([_slot(parent, "parent"), _slot(alias, "alias")])


@pytest.mark.parametrize(
    "child_root,device,rejected",
    [
        ("/tree/private", "8:1", True),
        ("/tree", "8:1", True),
        ("/treehouse", "8:1", False),
        ("/tree/private", "8:2", False),
    ],
)
def test_observer_checks_structured_physical_ancestry_before_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child_root: str,
    device: str,
    rejected: bool,
) -> None:
    parent, alias = tmp_path / "tree", tmp_path / "private-alias"
    parent.mkdir()
    alias.mkdir()
    slots = preflight_slots([_slot(parent, "parent"), _slot(alias, "alias")])
    records = (
        "2 1 8:1 /tree /srv/aegis/roots/parent ro - ext4 /dev/sda rw\n"
        f"3 1 {device} {child_root} /srv/aegis/roots/alias ro - ext4 /dev/sda rw\n"
    ).encode()

    def fixture_observer(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if "run" in command:
            kwargs["stdout"].write(records)
            kwargs["stdout"].flush()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("aegisctl.mounts.subprocess.run", fixture_observer)
    if rejected:
        with pytest.raises(ConfigError, match=r"overlap|inconsistent"):
            observe_mount_fingerprints(slots)
    else:
        assert len(observe_mount_fingerprints(slots)) == 2


def test_output_guard_rejects_original_through_physical_bind_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, alias = tmp_path / "original", tmp_path / "alias"
    source.mkdir()
    alias.mkdir()
    slots = tuple(
        replace(slot, mount_fingerprint="a" * 64)
        for slot in preflight_slots([_slot(source, "photos")])
    )
    destination = alias / "keep"
    destination.write_bytes(b"synthetic original")
    records = (
        "1 0 0:1 / / rw - tmpfs tmpfs rw\n"
        f"2 1 8:1 /tree {source.resolve()} rw - ext4 /dev/sda rw\n"
        f"3 1 8:1 /tree/private {alias.resolve()} rw - ext4 /dev/sda rw\n"
    ).encode()
    real_open = Path.open

    def fixture_mountinfo(path: Path, *args: Any, **kwargs: Any) -> Any:
        return (
            io.BytesIO(records)
            if str(path) == "/proc/self/mountinfo"
            else real_open(path, *args, **kwargs)
        )

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "open", fixture_mountinfo)
    import os

    with pytest.raises(ConfigError, match="original"):
        write_manifest(destination, slots, uid=os.geteuid(), gid=os.getegid())
    assert destination.read_bytes() == b"synthetic original"


@pytest.mark.parametrize("ambiguous_target", ["unrelated", "source", "ancestor"])
def test_host_lookup_only_rejects_ambiguity_on_the_selected_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ambiguous_target: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = {
        "unrelated": "/unrelated/stacked-mount",
        "source": str(source.resolve()),
        "ancestor": str(source.resolve().parent),
    }[ambiguous_target]
    records = (
        "1 0 0:1 / / rw - tmpfs tmpfs rw\n"
        f"2 1 8:1 /first {target} ro - ext4 /dev/sda rw\n"
        f"3 1 8:2 /second {target} ro - ext4 /dev/sdb rw\n"
    ).encode()
    real_open = Path.open

    def fixture_mountinfo(path: Path, *args: Any, **kwargs: Any) -> Any:
        if str(path) == "/proc/self/mountinfo":
            return io.BytesIO(records)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "open", fixture_mountinfo)
    if ambiguous_target == "unrelated":
        validated = preflight_slots([_slot(source, "photos")])
        assert validated[0].source == source.resolve()
    else:
        with pytest.raises(ConfigError, match="host mount identity"):
            preflight_slots([_slot(source, "photos")])
    # Runtime and constrained observer parsing remain globally strict.
    with pytest.raises(MountAttestationError, match="ambiguous mountpoint"):
        parse_mountinfo(records)
