from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from aegisctl.cli import main
from aegisctl.mounts import (
    ConfigError,
    SlotSpec,
    local_identity,
    parse_config,
    preflight_slots,
    write_manifest,
)


def _slot(source: Path, slot_id: str, *, mode: str = "read_only") -> SlotSpec:
    return SlotSpec(
        slot_id=slot_id,
        source=source,
        container_path=f"/srv/aegis/roots/{slot_id}",
        mode=mode,  # type: ignore[arg-type]
        expected_identity=local_identity(source),
    )


def test_schema_validation_does_not_access_illustrative_source(tmp_path: Path) -> None:
    config = tmp_path / "mounts.toml"
    config.write_text(
        """
version = 1

[[slots]]
slot_id = "family-photos"
source = "/definitely/not/present/aegis-example"
container_path = "/srv/aegis/roots/family-photos"
mode = "read_only"
expected_identity = "remote:nas01:/volume1/photos"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    slots = parse_config(config)

    assert slots[0].slot_id == "family-photos"


@pytest.mark.parametrize(
    "replacement",
    [
        "unexpected = true\n",
        "version = 1\nextra = 1\nslots = []\n",
        'version = 1\n[[slots]]\nslot_id = "UPPER"\nsource = "/tmp"\n'
        'container_path = "/srv/aegis/roots/UPPER"\nmode = "read_only"\n'
        'expected_identity = "remote:nas:/share"\n',
    ],
)
def test_config_rejects_unknown_fields_and_invalid_slot_ids(
    tmp_path: Path, replacement: str
) -> None:
    config = tmp_path / "mounts.toml"
    config.write_text(replacement, encoding="utf-8")

    with pytest.raises(ConfigError):
        parse_config(config)


def test_nested_sources_are_rejected(tmp_path: Path) -> None:
    parent = tmp_path / "photos"
    child = parent / "private"
    child.mkdir(parents=True)

    with pytest.raises(ValueError, match="overlap"):
        preflight_slots([_slot(parent, "photos"), _slot(child, "private")])


def test_duplicate_real_paths_through_symlink_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)

    with pytest.raises(ValueError, match="duplicate"):
        preflight_slots([_slot(source, "source"), _slot(alias, "alias")])


def test_duplicate_remote_identities_are_rejected(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    remote = "remote:nas01:/exports/photos"
    slots = [
        SlotSpec("first", first, "/srv/aegis/roots/first", "read_only", remote),
        SlotSpec("second", second, "/srv/aegis/roots/second", "read_only", remote),
    ]

    with pytest.raises(ValueError, match="duplicate"):
        preflight_slots(slots)


def test_local_identity_mismatch_does_not_disclose_source(tmp_path: Path) -> None:
    source = tmp_path / "private-customer-name"
    source.mkdir()
    spec = SlotSpec(
        "photos",
        source,
        "/srv/aegis/roots/photos",
        "read_only",
        "local:1:2",
    )

    with pytest.raises(ValueError) as caught:
        preflight_slots([spec])

    assert "photos" in str(caught.value)
    assert str(source) not in str(caught.value)
    assert "local:1:2" not in str(caught.value)


def test_writable_probe_refuses_preexisting_nonempty_reserved_directory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    reserved = source / ".aegis-preflight"
    reserved.mkdir(parents=True)
    sentinel = reserved / "operator-owned"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="probe"):
        preflight_slots([_slot(source, "source", mode="read_write")])

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_writable_probe_leaves_preexisting_empty_reserved_directory(tmp_path: Path) -> None:
    source = tmp_path / "source"
    reserved = source / ".aegis-preflight"
    reserved.mkdir(parents=True)

    validated = preflight_slots([_slot(source, "source", mode="read_write")])

    assert validated[0].slot_id == "source"
    assert reserved.is_dir()
    assert list(reserved.iterdir()) == []


def test_manifest_write_is_atomic_sanitized_and_exactly_0600(tmp_path: Path) -> None:
    source = tmp_path / "customer-secret-path"
    source.mkdir()
    validated = preflight_slots([_slot(source, "photos")])
    manifest = tmp_path / "manifest.json"

    fingerprinted = tuple(
        replace(slot, mount_fingerprint="a" * 64) for slot in validated
    )
    digest = write_manifest(manifest, fingerprinted, uid=os.geteuid(), gid=os.getegid())

    raw = manifest.read_bytes()
    payload = json.loads(raw)
    assert len(digest) == 64
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert str(source).encode() not in raw
    assert payload["slots"][0]["slotId"] == "photos"
    assert payload["slots"][0]["filesystemId"] == source.stat().st_dev
    assert payload["slots"][0]["rootInode"] == source.stat().st_ino


def test_runtime_identity_requires_both_canonical_current_nonroot_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aegisctl.mounts import runtime_identity

    monkeypatch.setenv("AEGIS_UID", str(os.geteuid()))
    monkeypatch.delenv("AEGIS_GID", raising=False)
    with pytest.raises(ConfigError, match="both"):
        runtime_identity()

    monkeypatch.setenv("AEGIS_GID", str(os.getegid()))
    assert runtime_identity() == (os.geteuid(), os.getegid())

    monkeypatch.setenv("AEGIS_UID", f"0{os.geteuid()}")
    with pytest.raises(ConfigError):
        runtime_identity()


def test_cli_validate_is_schema_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "mounts.toml"
    config.write_text(
        """
version = 1
[[slots]]
slot_id = "photos"
source = "/missing/illustrative/source"
container_path = "/srv/aegis/roots/photos"
mode = "read_only"
expected_identity = "remote:nas01:/photos"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    assert main(["mounts", "validate", "--config", str(config)]) == 0
    assert capsys.readouterr().out == '{"status":"valid"}\n'


def test_cli_inspect_prints_only_local_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "private-name"
    source.mkdir()

    assert main(["mounts", "inspect", "--source", str(source.resolve())]) == 0
    captured = capsys.readouterr()
    assert captured.out == local_identity(source) + "\n"
    assert captured.err == ""


def test_cli_preflight_atomically_writes_sanitized_manifest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "private-name"
    source.mkdir()
    config = tmp_path / "mounts.toml"
    manifest = tmp_path / "manifest.json"
    config.write_text(
        f"""
version = 1
[[slots]]
slot_id = "photos"
source = "{source}"
container_path = "/srv/aegis/roots/photos"
mode = "read_only"
expected_identity = "{local_identity(source)}"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "aegisctl.cli.observe_mount_fingerprints",
        lambda slots: tuple(replace(slot, mount_fingerprint="a" * 64) for slot in slots),
    )

    assert (
        main(
            [
                "mounts",
                "preflight",
                "--config",
                str(config),
                "--manifest",
                str(manifest),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "preflighted"
    assert len(result["manifestSha256"]) == 64
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert str(source) not in manifest.read_text(encoding="utf-8")


def test_checked_in_example_validates_without_host_access(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["mounts", "validate", "--config", "deploy/mounts.example.toml"]) == 0
    assert capsys.readouterr().out == '{"status":"valid"}\n'


def test_config_is_read_from_a_bounded_nofollow_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "mounts.toml"
    config.write_text(
        """
version = 1
[[slots]]
slot_id = "photos"
source = "/missing/example"
container_path = "/srv/aegis/roots/photos"
mode = "read_only"
expected_identity = "remote:nas:/photos"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda self: (_ for _ in ()).throw(AssertionError("unbounded path read")),
    )

    assert parse_config(config)[0].slot_id == "photos"


def test_oversized_config_is_rejected_after_only_the_bounded_prefix(tmp_path: Path) -> None:
    config = tmp_path / "mounts.toml"
    config.write_bytes(b" " * (64 * 1024 + 1))

    with pytest.raises(ConfigError, match="size"):
        parse_config(config)


def test_manifest_atomic_write_forces_invoking_group_in_inherited_group_directory(
    tmp_path: Path,
) -> None:
    inherited_groups = [group for group in os.getgroups() if group != os.getegid()]
    if not inherited_groups:
        pytest.skip("requires a supplementary group to exercise inherited ownership")
    destination = tmp_path / "inherited-group"
    destination.mkdir()
    os.chown(destination, os.geteuid(), inherited_groups[0])
    destination.chmod(0o2770)
    source = tmp_path / "source"
    source.mkdir()
    slots = tuple(
        replace(slot, mount_fingerprint="0" * 64)
        for slot in preflight_slots([_slot(source, "photos")])
    )
    manifest = destination / "manifest.json"

    write_manifest(manifest, slots, uid=os.geteuid(), gid=os.getegid())

    info = manifest.stat()
    assert info.st_uid == os.geteuid()
    assert info.st_gid == os.getegid()
    assert info.st_mode & 0o777 == 0o600
