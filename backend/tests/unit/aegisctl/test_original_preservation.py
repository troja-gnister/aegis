from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest
from aegisctl.cli import main
from aegisctl.mounts import (
    ConfigError,
    local_identity,
    observe_mount_fingerprints,
    parse_config,
    preflight_slots,
    render_artifacts,
    write_manifest,
)


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "original"
    source.mkdir()
    (source / "keep").write_bytes(b"original fixture bytes")
    config = tmp_path / "mounts.toml"
    config.write_text(
        f'version = 1\n[[slots]]\nslot_id = "photos"\nsource = "{source}"\n'
        'container_path = "/srv/aegis/roots/photos"\nmode = "read_only"\n'
        f'expected_identity = "{local_identity(source)}"\n',
    )
    return source, config


def _destination(tmp_path: Path, source: Path, kind: str) -> Path:
    if kind == "parent-alias":
        alias = tmp_path / "alias"
        alias.symlink_to(source, target_is_directory=True)
        return alias / "new" / "artifact"
    if kind == "file-alias":
        alias = tmp_path / "alias"
        alias.symlink_to(source / "keep")
        return alias
    return source / ("keep" if kind == "existing" else "new/artifact")


def _unchanged(source: Path) -> None:
    assert (source / "keep").read_bytes() == b"original fixture bytes"
    assert sorted(path.name for path in source.iterdir()) == ["keep"]


@pytest.mark.parametrize("kind", ["existing", "new", "parent-alias", "file-alias"])
def test_manifest_rejects_original_destination_without_writes(tmp_path: Path, kind: str) -> None:
    source, config = _fixture(tmp_path)
    slots = tuple(replace(slot, mount_fingerprint="a" * 64)
                  for slot in preflight_slots(parse_config(config)))
    destination = _destination(tmp_path, source, kind)
    with pytest.raises(ConfigError, match="original"):
        write_manifest(destination, slots, uid=os.geteuid(), gid=os.getegid())
    _unchanged(source)


@pytest.mark.parametrize("artifact", ["compose", "attestation"])
@pytest.mark.parametrize("kind", ["existing", "new", "parent-alias", "file-alias"])
def test_render_rejects_every_original_destination_before_any_write(
    tmp_path: Path, artifact: str, kind: str,
) -> None:
    source, config = _fixture(tmp_path)
    slots = tuple(replace(slot, mount_fingerprint="a" * 64)
                  for slot in preflight_slots(parse_config(config)))
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, slots, uid=os.geteuid(), gid=os.getegid())
    safe = tmp_path / "safe-output"
    safe.write_bytes(b"existing generated artifact")
    unsafe = _destination(tmp_path, source, kind)
    compose, attestation = (unsafe, safe) if artifact == "compose" else (safe, unsafe)
    with pytest.raises(ConfigError, match="original"):
        render_artifacts(config, manifest, compose, attestation,
                         uid=os.geteuid(), gid=os.getegid())
    _unchanged(source)
    assert safe.read_bytes() == b"existing generated artifact"


def test_cli_rejects_original_output_before_launching_observer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, config = _fixture(tmp_path)

    def forbidden_observer(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("observer ran before original output rejection")

    monkeypatch.setattr("aegisctl.cli.observe_mount_fingerprints", forbidden_observer)
    assert main(["mounts", "preflight", "--config", str(config),
                 "--manifest", str(source / "keep")]) == 2
    _unchanged(source)


def test_observer_rejects_temporary_directory_below_original_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, config = _fixture(tmp_path)
    slots = preflight_slots(parse_config(config))
    monkeypatch.setattr("tempfile.tempdir", str(source))

    def forbidden_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("observer created artifacts below an original")

    monkeypatch.setattr("aegisctl.mounts.subprocess.run", forbidden_run)
    with pytest.raises(ConfigError, match="original"):
        observe_mount_fingerprints(slots)
    _unchanged(source)
