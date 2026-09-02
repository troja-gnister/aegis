from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from aegis_apps.roots.manifest import MountManifest
from aegisctl.mounts import (
    MAX_MOUNTINFO_BYTES,
    ConfigError,
    attest_mounts,
    local_identity,
    observe_mount_fingerprints,
    parse_config,
    parse_mountinfo,
    preflight_slots,
)


def test_mount_fingerprint_excludes_volatile_ids_target_and_mode_options() -> None:
    first = parse_mountinfo(
        b"643 631 0:50 /host\\040root /srv/aegis/roots/photos ro,nosuid "
        b"- fakeowner /run/host_mark/private rw,fakeowner\n"
    )["/srv/aegis/roots/photos"]
    second = parse_mountinfo(
        b"999 888 0:50 /host\\040root /different/target rw,nodev "
        b"- fakeowner /run/host_mark/private ro,fakeowner\n"
    )["/different/target"]
    expected = hashlib.sha256(
        b"aegis.mount-fingerprint.v1\0"
        b"0:50\0"
        b"/host\\040root\0"
        b"fakeowner\0"
        b"/run/host_mark/private\0"
    ).hexdigest()

    assert first.mount_fingerprint == expected
    assert second.mount_fingerprint == expected
    assert first.effective_mode == "read_only"
    assert second.effective_mode == "read_only"


def test_backend_attestation_uses_mount_fingerprint_not_container_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    config = tmp_path / "mounts.toml"
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
    host_validated = preflight_slots(parse_config(config))
    raw_record = (
        b"643 631 0:50 /private/source /srv/aegis/roots/photos ro "
        b"- fakeowner /run/host_mark/private rw\n"
    )
    fingerprint = parse_mountinfo(raw_record)[
        "/srv/aegis/roots/photos"
    ].mount_fingerprint
    from aegisctl.mounts import write_manifest

    manifest_path = tmp_path / "manifest.json"
    write_manifest(
        manifest_path,
        tuple(replace(slot, mount_fingerprint=fingerprint) for slot in host_validated),
        uid=os.geteuid(),
        gid=os.getegid(),
    )
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    manifest = MountManifest.load(manifest_path, digest)
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_bytes(raw_record)
    monkeypatch.setattr(
        os,
        "stat",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError),
    )

    attest_mounts(manifest, "indexer", mountinfo_path=mountinfo)


def test_container_observer_returns_fingerprinted_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source,with:punctuation"
    source.mkdir()
    config = tmp_path / "mounts.toml"
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
    validated = preflight_slots(parse_config(config))
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = (
            "643 631 0:50 /private/source /srv/aegis/roots/photos ro "
            "- fakeowner /run/host_mark/private rw\n"
        )
        stderr = ""

    def fake_run(arguments, **kwargs):
        calls.append(arguments)
        if "run" in arguments:
            compose_path = Path(arguments[arguments.index("-f") + 1])
            assert compose_path.stat().st_mode & 0o777 == 0o600
            compose = compose_path.read_text(encoding="utf-8")
            assert str(source) in compose
            assert "type: bind" in compose
            assert "read_only: true" in compose
            kwargs["stdout"].write(Result.stdout.encode("ascii"))
            kwargs["stdout"].flush()
        return Result()

    monkeypatch.setattr("aegisctl.mounts.subprocess.run", fake_run)

    observed = observe_mount_fingerprints(validated)

    assert len(observed[0].mount_fingerprint) == 64
    assert any("run" in call for call in calls)
    assert any("down" in call for call in calls)


def test_container_observer_never_captures_unbounded_subprocess_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    config = tmp_path / "mounts.toml"
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
    validated = preflight_slots(parse_config(config))

    class Result:
        returncode = 0

    def fake_run(arguments, **kwargs):
        if "run" in arguments:
            assert kwargs.get("capture_output") is not True
            assert kwargs.get("stderr") is subprocess.DEVNULL
            output = kwargs["stdout"]
            output.write(b"x" * (MAX_MOUNTINFO_BYTES + 1))
            output.flush()
        return Result()

    monkeypatch.setattr("aegisctl.mounts.subprocess.run", fake_run)

    with pytest.raises(ConfigError, match="observation") as caught:
        observe_mount_fingerprints(validated)

    assert str(source) not in str(caught.value)
