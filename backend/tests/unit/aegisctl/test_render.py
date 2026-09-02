from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from aegis_apps.roots.manifest import MountManifest
from aegisctl.mounts import (
    MountAttestationError,
    attest_mounts,
    local_identity,
    parse_config,
    parse_mountinfo,
    preflight_slots,
    render_artifacts,
    write_manifest,
)


def _fingerprint(fields: bytes) -> str:
    return hashlib.sha256(b"aegis.mount-fingerprint.v1\0" + fields).hexdigest()


def _preflight_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    readonly = tmp_path / "readonly-private"
    writable = tmp_path / "writable-private"
    readonly.mkdir()
    writable.mkdir()
    config = tmp_path / "mounts.toml"
    manifest = tmp_path / "manifest.json"
    config.write_text(
        f"""
version = 1

[[slots]]
slot_id = "photos"
source = "{readonly}"
container_path = "/srv/aegis/roots/photos"
mode = "read_only"
expected_identity = "{local_identity(readonly)}"

[[slots]]
slot_id = "uploads"
source = "{writable}"
container_path = "/srv/aegis/roots/uploads"
mode = "read_write"
expected_identity = "{local_identity(writable)}"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    validated = tuple(
        replace(
            slot,
            mount_fingerprint=_fingerprint(
                b"0:32\0/\0ext4\0/dev/sda\0"
                if slot.slot_id == "photos"
                else b"0:33\0/\0ext4\0/dev/sdb\0"
            ),
        )
        for slot in preflight_slots(parse_config(config))
    )
    write_manifest(manifest, validated, uid=os.geteuid(), gid=os.getegid())
    return config, manifest, readonly, writable


def _root_mount(service: dict[str, object], target: str) -> dict[str, object]:
    volumes = service["volumes"]
    assert isinstance(volumes, list)
    return next(volume for volume in volumes if volume["target"] == target)  # type: ignore[index,union-attr,no-any-return]


def test_render_is_deterministic_and_enforces_role_scoped_long_bind_mounts(
    tmp_path: Path,
) -> None:
    config, manifest, readonly, writable = _preflight_fixture(tmp_path)
    output = tmp_path / "compose.generated.yaml"
    gateway = tmp_path / "gateway.attestation"

    result = render_artifacts(
        config,
        manifest,
        output,
        gateway,
        uid=os.geteuid(),
        gid=os.getegid(),
    )
    first_compose = output.read_bytes()
    first_attestation = gateway.read_bytes()
    second = render_artifacts(
        config,
        manifest,
        output,
        gateway,
        uid=os.geteuid(),
        gid=os.getegid(),
    )

    assert result == second
    assert output.read_bytes() == first_compose
    assert gateway.read_bytes() == first_attestation
    assert result.manifest_digest == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert result.gateway_digest == hashlib.sha256(gateway.read_bytes()).hexdigest()
    photos_fingerprint = _fingerprint(b"0:32\0/\0ext4\0/dev/sda\0")
    uploads_fingerprint = _fingerprint(b"0:33\0/\0ext4\0/dev/sdb\0")
    assert gateway.read_text(encoding="ascii").splitlines() == [
        f"photos|/srv/aegis/roots/photos|{readonly.stat().st_dev}|"
        f"{readonly.stat().st_ino}|read_only|"
        f"{photos_fingerprint}",
        f"uploads|/srv/aegis/roots/uploads|{writable.stat().st_dev}|"
        f"{writable.stat().st_ino}|read_write|"
        f"{uploads_fingerprint}",
    ]

    rendered = yaml.safe_load(first_compose)
    services = rendered["services"]
    assert "migrate" not in services
    assert services["web"]["user"] == f"{os.geteuid()}:{os.getegid()}"
    assert services["operations"]["user"] == f"{os.geteuid()}:{os.getegid()}"
    assert services["indexer"]["user"] == f"{os.geteuid()}:{os.getegid()}"
    assert services["media"]["user"] == f"{os.geteuid()}:{os.getegid()}"
    assert "user" not in services["gateway"]

    root_targets = {"/srv/aegis/roots/photos", "/srv/aegis/roots/uploads"}
    web_targets = {mount["target"] for mount in services["web"]["volumes"]}
    assert web_targets.isdisjoint(root_targets)
    for role in ("gateway", "indexer", "media"):
        for target in root_targets:
            assert _root_mount(services[role], target)["read_only"] is True
    assert _root_mount(services["operations"], "/srv/aegis/roots/photos")["read_only"] is True
    assert _root_mount(services["operations"], "/srv/aegis/roots/uploads")["read_only"] is False
    for service in services.values():
        for mount in service.get("volumes", []):
            assert mount["type"] == "bind"
            assert mount["bind"] == {"create_host_path": False}


def test_render_rejects_output_aliases_before_overwrite(tmp_path: Path) -> None:
    config, manifest, _, _ = _preflight_fixture(tmp_path)
    gateway = tmp_path / "gateway.attestation"
    original = config.read_bytes()

    with pytest.raises(ValueError, match="alias"):
        render_artifacts(
            config,
            manifest,
            config,
            gateway,
            uid=os.geteuid(),
            gid=os.getegid(),
        )

    assert config.read_bytes() == original


def test_render_rejects_manifest_that_no_longer_matches_source(tmp_path: Path) -> None:
    config, manifest, _, writable = _preflight_fixture(tmp_path)
    replaced = tmp_path / "replacement"
    replaced.mkdir()
    writable.rename(tmp_path / "old")
    replaced.rename(writable)

    with pytest.raises(ValueError, match="uploads") as caught:
        render_artifacts(
            config,
            manifest,
            tmp_path / "compose.yaml",
            tmp_path / "gateway.txt",
            uid=os.geteuid(),
            gid=os.getegid(),
        )

    assert str(writable) not in str(caught.value)


def test_mountinfo_decodes_only_defined_escapes_and_computes_effective_mode() -> None:
    mountinfo = (
        b"36 25 0:32 / /srv/aegis/roots/photo\\040archive ro,nosuid - ext4 /dev/sda rw\n"
        b"37 25 0:33 / /srv/aegis/roots/uploads rw,nosuid - ext4 /dev/sdb rw\n"
    )

    records = parse_mountinfo(mountinfo)

    assert records["/srv/aegis/roots/photo archive"].effective_mode == "read_only"
    assert records["/srv/aegis/roots/uploads"].effective_mode == "read_write"

    with pytest.raises(MountAttestationError):
        parse_mountinfo(
            b"36 25 0:32 / /srv/aegis/roots/photo\\141 ro - ext4 /dev/sda rw\n"
        )


def test_mountinfo_rejects_duplicate_exact_target_and_beyond_bound_input() -> None:
    duplicate = (
        b"36 25 0:32 / /srv/aegis/roots/photos ro - ext4 /dev/sda rw\n"
        b"37 25 0:32 / /srv/aegis/roots/photos ro - ext4 /dev/sda rw\n"
    )
    with pytest.raises(MountAttestationError, match="ambiguous"):
        parse_mountinfo(duplicate)
    with pytest.raises(MountAttestationError, match="size"):
        parse_mountinfo(b"x" * (1024 * 1024 + 1))


def test_backend_attestation_checks_identity_exact_mountpoint_and_role_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, manifest_path, readonly, writable = _preflight_fixture(tmp_path)
    del config
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    manifest = MountManifest.load(manifest_path, digest)
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "\n".join(
            [
                "36 25 0:32 / /srv/aegis/roots/photos ro - ext4 /dev/sda rw",
                "37 25 0:33 / /srv/aegis/roots/uploads rw - ext4 /dev/sdb rw",
            ]
        )
        + "\n",
        encoding="ascii",
    )
    real_stat = os.stat

    def container_stat(
        path: os.PathLike[str] | str, *, follow_symlinks: bool = True
    ) -> os.stat_result:
        if str(path) == "/srv/aegis/roots/photos":
            return real_stat(readonly, follow_symlinks=follow_symlinks)
        if str(path) == "/srv/aegis/roots/uploads":
            return real_stat(writable, follow_symlinks=follow_symlinks)
        return real_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", container_stat)

    attest_mounts(manifest, "operations", mountinfo_path=mountinfo)
    with pytest.raises(MountAttestationError, match="uploads") as caught:
        attest_mounts(manifest, "indexer", mountinfo_path=mountinfo)
    assert str(writable) not in str(caught.value)
