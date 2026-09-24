from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import pytest
import yaml
from aegis_apps.roots.manifest import ManifestSlot, MountManifest
from aegisctl.mounts import (
    ConfigError,
    MountAttestationError,
    SlotSpec,
    attest_mounts,
    local_identity,
    observe_mount_fingerprints,
    preflight_slots,
    render_artifacts,
    write_manifest,
)

from tests.support.fake_container_engine import select_fake_engine
from tests.support.observer_engine import ObserverEngine, assert_observer_engine_cleaned


def _slot(path: Path, name: str = "photos") -> SlotSpec:
    return SlotSpec(name, path, f"/srv/aegis/roots/{name}", "read_only", local_identity(path))


def _host_records(monkeypatch: pytest.MonkeyPatch, raw: bytes) -> None:
    real_open = Path.open

    def open_mountinfo(path: Path, *args: Any, **kwargs: Any) -> Any:
        return (
            io.BytesIO(raw)
            if str(path) == "/proc/self/mountinfo"
            else real_open(path, *args, **kwargs)
        )

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "open", open_mountinfo)


def _snapshot(*directories: Path) -> tuple[object, ...]:
    # Enumerate only these deliberately disposable fixtures to detect writes.
    return tuple(
        (
            path,
            path.stat().st_ino,
            path.stat().st_mtime_ns,
            path.read_bytes() if path.is_file() else sorted(p.name for p in path.iterdir()),
        )
        for directory in directories
        for path in (directory, *sorted(directory.iterdir()))
    )


@pytest.mark.parametrize("descendant", ["nested", "deep/nested"])
@pytest.mark.parametrize("mode", ["ro", "rw"])
@pytest.mark.parametrize("declare_alias", [False, True])
def test_preflight_rejects_nested_bind_alias_even_when_declared_separately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    descendant: str,
    mode: str,
    declare_alias: bool,
) -> None:
    original, external = tmp_path / "original", tmp_path / "external"
    (original / descendant).mkdir(parents=True)
    external.mkdir()
    (external / "keep").write_bytes(b"synthetic original")
    raw = (
        "1 0 8:1 / / rw - ext4 /dev/root rw\n"
        f"2 1 8:2 /external {original.resolve() / descendant} {mode} - ext4 /dev/other rw\n"
        f"3 1 8:2 /external {external.resolve()} rw - ext4 /dev/other rw\n"
    ).encode()
    _host_records(monkeypatch, raw)
    before = _snapshot(original, external)
    slots = [_slot(original)] + ([_slot(external, "alias")] if declare_alias else [])
    error = None
    try:
        preflight_slots(slots)
    except ConfigError as caught:
        error = caught
    assert _snapshot(original, external) == before
    assert error is not None
    assert "nested mount" in str(error)
    assert str(original) not in str(error)


@pytest.mark.parametrize("writer", ["manifest", "compose", "attestation", "observer"])
@pytest.mark.parametrize("destination", ["keep", "new/artifact"])
def test_every_direct_writer_rejects_nested_root_before_any_artifact_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
    destination: str,
) -> None:
    original, external = tmp_path / "original", tmp_path / "external"
    (original / "nested").mkdir(parents=True)
    external.mkdir()
    (external / "keep").write_bytes(b"synthetic original")
    slots = tuple(
        replace(slot, mount_fingerprint="a" * 64) for slot in preflight_slots([_slot(original)])
    )
    config, manifest = tmp_path / "mounts.toml", tmp_path / "manifest.json"
    config.write_text(
        f'version=1\n[[slots]]\nslot_id="photos"\nsource="{original}"\n'
        'container_path="/srv/aegis/roots/photos"\nmode="read_only"\n'
        f'expected_identity="{local_identity(original)}"\n',
    )
    write_manifest(manifest, slots, uid=os.geteuid(), gid=os.getegid())
    safe_output = tmp_path / "safe-artifact"
    safe_output.write_bytes(b"existing generated artifact")
    raw = (
        "1 0 8:1 / / rw - ext4 /dev/root rw\n"
        f"2 1 8:2 /external {original.resolve()}/nested ro - ext4 /dev/other rw\n"
        f"3 1 8:2 /external {external.resolve()} rw - ext4 /dev/other rw\n"
    ).encode()
    _host_records(monkeypatch, raw)

    def fake_docker(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if "run" in command:
            kwargs["stdout"].write(
                b"1 0 8:1 /original /srv/aegis/roots/photos ro - ext4 /dev/root rw\n"
            )
            kwargs["stdout"].flush()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("aegisctl.mounts.subprocess.run", fake_docker)
    monkeypatch.setattr("tempfile.tempdir", str(external))
    before = _snapshot(original, external)
    error = None
    try:
        output = external / destination
        if writer == "manifest":
            write_manifest(output, slots, uid=os.geteuid(), gid=os.getegid())
        elif writer == "observer":
            observe_mount_fingerprints(slots)
        else:
            compose, attestation = (
                (output, safe_output) if writer == "compose" else (safe_output, output)
            )
            render_artifacts(
                config, manifest, compose, attestation, uid=os.geteuid(), gid=os.getegid()
            )
    except ConfigError as caught:
        error = caught
    assert _snapshot(original, external) == before
    assert safe_output.read_bytes() == b"existing generated artifact"
    assert error is not None
    assert "nested mount" in str(error)


@pytest.mark.parametrize("mount_suffix", ["", "-archive", "-sibling/child"])
def test_root_mount_and_component_siblings_remain_usable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mount_suffix: str,
) -> None:
    source = tmp_path / "original"
    (source / "ordinary/subdirectory").mkdir(parents=True)
    raw = (
        "1 0 8:1 / / rw - ext4 /dev/root rw\n"
        f"2 1 8:2 /other {source.resolve()}{mount_suffix} ro - ext4 /dev/other rw\n"
    ).encode()
    _host_records(monkeypatch, raw)
    assert preflight_slots([_slot(source)])[0].source == source.resolve()


@pytest.mark.parametrize("role", ["operations", "indexer", "media"])
@pytest.mark.parametrize("suffix", ["/nested", "/deep/nested"])
def test_backend_rejects_descendant_mounts_introduced_after_preflight(
    tmp_path: Path,
    role: Literal["operations", "indexer", "media"],
    suffix: str,
) -> None:
    source = tmp_path.resolve() / "source"
    source.mkdir()
    raw = (
        f"1 0 8:1 /original {source} ro - ext4 /dev/root rw\n"
        f"2 1 8:2 /elsewhere {source}{suffix} ro - ext4 /dev/other rw\n"
    ).encode()
    fingerprint = hashlib.sha256(
        b"aegis.mount-fingerprint.v1\0" + b"8:1\0/original\0ext4\0/dev/root\0"
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
def test_observer_filter_retains_descendants_for_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
) -> None:
    select_fake_engine(engine, tmp_path, monkeypatch, checked_mask_policy=True)
    source = tmp_path / "source"
    source.mkdir()
    slots = preflight_slots([_slot(source)])
    mountinfo = tmp_path / "observer-mountinfo"
    mountinfo.write_bytes(
        b"1 0 8:1 /original /srv/aegis/roots/photos ro - ext4 /dev/root rw\n"
        b"2 1 8:2 /elsewhere /srv/aegis/roots/photos/deep/nested ro - ext4 /dev/other rw\n"
    )
    real_run = subprocess.run

    def fixture_output(command: list[str], kwargs: dict[str, Any]) -> None:
        compose = yaml.safe_load(Path(command[command.index("-f") + 1]).read_text())
        script = compose["services"]["mount-observer"]["command"][0]
        result = real_run(
            ["/bin/sh", "-ec", script.replace("/proc/self/mountinfo", str(mountinfo))], **kwargs
        )
        assert result.returncode == 0

    fake = ObserverEngine(slots[0].source, output=fixture_output)
    monkeypatch.setattr("aegisctl.mounts.subprocess.run", fake)
    with pytest.raises(ConfigError, match="observation failed; diagnostics retained at") as caught:
        observe_mount_fingerprints(slots)
    cause = caught.value.__cause__
    assert isinstance(cause, ConfigError)
    assert "nested mount" in str(cause)
    assert fake.diagnostics is not None
    assert str(caught.value).endswith(str(fake.diagnostics))
    assert (fake.diagnostics / "mountinfo.out").read_bytes() == mountinfo.read_bytes()
    assert_observer_engine_cleaned(fake)
