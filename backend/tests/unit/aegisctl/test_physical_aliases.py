from __future__ import annotations

import hashlib
import io
import sys
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import pytest
from aegis_apps.roots.manifest import ManifestSlot, MountManifest
from aegisctl.mounts import (
    ConfigError,
    MountAttestationError,
    SlotSpec,
    attest_mounts,
    local_identity,
    observe_mount_fingerprints,
    parse_mountinfo,
    preflight_slots,
    write_manifest,
)

from tests.support.fake_container_engine import select_fake_engine
from tests.support.observer_engine import ObserverEngine, assert_observer_engine_cleaned


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
@pytest.mark.parametrize("engine", ("docker", "podman"))
def test_observer_checks_structured_physical_ancestry_before_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child_root: str,
    device: str,
    rejected: bool,
    engine: str,
) -> None:
    select_fake_engine(engine, tmp_path, monkeypatch)
    parent, alias = tmp_path / "tree", tmp_path / "private-alias"
    parent.mkdir()
    alias.mkdir()
    slots = preflight_slots([_slot(parent, "parent"), _slot(alias, "alias")])
    records = (
        "2 1 8:1 /tree /srv/aegis/roots/parent ro - ext4 /dev/sda rw\n"
        f"3 1 {device} {child_root} /srv/aegis/roots/alias ro - ext4 /dev/sda rw\n"
    ).encode()

    def fixture_output(command: list[str], kwargs: dict[str, Any]) -> None:
        kwargs["stdout"].write(records)
        kwargs["stdout"].flush()

    fake = ObserverEngine(slots[0].source, output=fixture_output)
    monkeypatch.setattr("aegisctl.mounts.subprocess.run", fake)
    if rejected:
        with pytest.raises(
            ConfigError, match="observation failed; diagnostics retained at",
        ) as caught:
            observe_mount_fingerprints(slots)
        cause = caught.value.__cause__
        assert isinstance(cause, ConfigError)
        assert "overlap" in str(cause) or "inconsistent" in str(cause)
        assert fake.diagnostics is not None
        assert str(caught.value).endswith(str(fake.diagnostics))
        assert (fake.diagnostics / "mountinfo.out").read_bytes() == records
    else:
        assert len(observe_mount_fingerprints(slots)) == 2
    assert_observer_engine_cleaned(fake)


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ambiguous_target: str,
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


@pytest.mark.parametrize("kernel_root", ["/..", "relative-root"])
@pytest.mark.parametrize("opaque_target", ["unrelated", "source", "ancestor"])
def test_host_preserves_opaque_mount_roots_and_checks_only_relevant_ancestry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kernel_root: str,
    opaque_target: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = {
        "unrelated": "/sys/fs/cgroup/freezer",
        "source": str(source.resolve()),
        "ancestor": str(source.resolve().parent),
    }[opaque_target]
    # cgroup_namespaces(7) documents /.. as an inherited cgroup mount root.
    records = (
        "1 0 8:1 / / rw - ext4 /dev/sda rw\n"
        f"2 1 0:32 {kernel_root} {target} ro - cgroup cgroup rw\n"
    ).encode()
    real_open = Path.open

    def fixture_mountinfo(path: Path, *args: Any, **kwargs: Any) -> Any:
        if str(path) == "/proc/self/mountinfo":
            return io.BytesIO(records)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "open", fixture_mountinfo)
    if opaque_target == "unrelated":
        validated = preflight_slots([_slot(source, "photos")])
        assert validated[0].source == source.resolve()
        # Runtime parsing must likewise preserve unrelated kernel records.
        assert target in parse_mountinfo(records)
    else:
        with pytest.raises(ConfigError, match="host mount identity"):
            preflight_slots([_slot(source, "photos")])


@pytest.mark.parametrize("role", ["operations", "indexer", "media"])
def test_runtime_rejects_opaque_selected_root_even_with_matching_fingerprint(
    tmp_path: Path,
    role: Literal["operations", "indexer", "media"],
) -> None:
    source = tmp_path.resolve() / "source"
    source.mkdir()
    raw = f"2 1 0:32 /.. {source} ro - cgroup cgroup rw\n".encode()
    fingerprint = hashlib.sha256(
        b"aegis.mount-fingerprint.v1\0" + b"0:32\0/..\0cgroup\0cgroup\0"
    ).hexdigest()
    manifest = MountManifest(
        "a" * 64,
        {
            "photos": ManifestSlot(
                "photos",
                PurePosixPath(source),
                "read_only",
                "local:1:2",
                1,
                2,
                fingerprint,
            )
        },
    )
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_bytes(raw)
    with pytest.raises(MountAttestationError, match="photos"):
        attest_mounts(manifest, role, mountinfo_path=mountinfo)


@pytest.mark.parametrize("engine", ("docker", "podman"))
def test_observer_rejects_opaque_selected_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
) -> None:
    select_fake_engine(engine, tmp_path, monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    slots = preflight_slots([_slot(source, "photos")])

    def fixture_output(command: list[str], kwargs: dict[str, Any]) -> None:
        kwargs["stdout"].write(b"2 1 0:32 /.. /srv/aegis/roots/photos ro - cgroup cgroup rw\n")
        kwargs["stdout"].flush()

    fake = ObserverEngine(slots[0].source, output=fixture_output)
    monkeypatch.setattr("aegisctl.mounts.subprocess.run", fake)
    with pytest.raises(ConfigError, match="observation failed"):
        observe_mount_fingerprints(slots)
    assert_observer_engine_cleaned(fake)
