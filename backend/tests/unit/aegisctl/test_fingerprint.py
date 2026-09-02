from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn

import pytest
import yaml
from aegis_apps.roots.manifest import MountManifest
from aegisctl.mounts import (
    MAX_MOUNTINFO_BYTES,
    ConfigError,
    ValidatedSlot,
    attest_mounts,
    local_identity,
    observe_mount_fingerprints,
    parse_config,
    parse_mountinfo,
    preflight_slots,
    set_observer_output_limit,
)


def _observer_slots(tmp_path: Path) -> tuple[ValidatedSlot, ...]:
    source = tmp_path / "private-canary"
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
    return preflight_slots(parse_config(config))


def _write_observer_record(output: Any) -> None:
    output.write(
        b"643 631 0:50 /private/source /srv/aegis/roots/photos ro "
        b"- fakeowner /run/host_mark/private rw\n"
    )
    output.flush()


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

    def fake_run(arguments: list[str], **kwargs: Any) -> Result:
        calls.append(arguments)
        if "run" in arguments:
            compose_path = Path(arguments[arguments.index("-f") + 1])
            assert compose_path.stat().st_mode & 0o777 == 0o600
            compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
            service = compose["services"]["mount-observer"]
            assert service["volumes"][0]["source"] == str(source)
            assert service["volumes"][0]["type"] == "bind"
            assert service["volumes"][0]["read_only"] is True
            assert service["cpus"] == 0.5
            assert service["mem_limit"] == "64m"
            assert service["pids_limit"] == 64
            assert service["stop_grace_period"] == "3s"
            kwargs["stdout"].write(Result.stdout.encode("ascii"))
            kwargs["stdout"].flush()
        return Result()

    monkeypatch.setattr("aegisctl.mounts.subprocess.run", fake_run)

    observed = observe_mount_fingerprints(validated)

    assert len(observed[0].mount_fingerprint) == 64
    assert any("run" in call for call in calls)
    assert any("down" in call and "--timeout" in call for call in calls)


def test_observer_run_timeout_still_cleans_and_verifies_project_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slots = _observer_slots(tmp_path)
    cleanup_marker = tmp_path / "cleanup-attempted"
    inspected: list[str] = []

    def fake_run(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        if "run" in arguments:
            raise subprocess.TimeoutExpired(arguments, 30)
        if "down" in arguments:
            cleanup_marker.touch()
        for resource in ("ps", "network", "volume"):
            if resource in arguments:
                inspected.append(resource)
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr("aegisctl.mounts.subprocess.run", fake_run)

    with pytest.raises(ConfigError, match="observation") as caught:
        observe_mount_fingerprints(slots)

    assert cleanup_marker.exists()
    assert inspected == ["ps", "network", "volume"]
    assert str(slots[0].source) not in str(caught.value)


@pytest.mark.parametrize(
    "cleanup_failure",
    ["exception", "timeout", "nonzero", "residual"],
)
def test_observer_rejects_unconfirmed_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: str,
) -> None:
    slots = _observer_slots(tmp_path)
    inspected: list[str] = []

    def fake_run(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        if "run" in arguments:
            _write_observer_record(kwargs["stdout"])
            return subprocess.CompletedProcess(arguments, 0)
        if "down" in arguments:
            if cleanup_failure == "exception":
                raise subprocess.SubprocessError(str(slots[0].source))
            if cleanup_failure == "timeout":
                raise subprocess.TimeoutExpired(arguments, 3)
            return subprocess.CompletedProcess(
                arguments, 1 if cleanup_failure == "nonzero" else 0
            )
        for resource in ("ps", "network", "volume"):
            if resource in arguments:
                inspected.append(resource)
                if cleanup_failure == "residual" and resource == "ps":
                    kwargs["stdout"].write(b"observer-container-id\n")
                    kwargs["stdout"].flush()
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr("aegisctl.mounts.subprocess.run", fake_run)

    with pytest.raises(ConfigError, match="observation") as caught:
        observe_mount_fingerprints(slots)

    assert inspected == ["ps", "network", "volume"]
    assert str(slots[0].source) not in str(caught.value)


def test_real_observer_leaves_no_unique_project_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slots = _observer_slots(tmp_path)
    project_token = hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:16]
    real_token_hex = secrets.token_hex

    def fixed_project_token(length: int) -> str:
        if length == 8:
            return project_token
        return real_token_hex(length)

    monkeypatch.setattr("aegisctl.mounts.secrets.token_hex", fixed_project_token)

    observed = observe_mount_fingerprints(slots)

    assert len(observed[0].mount_fingerprint) == 64
    label = f"label=com.docker.compose.project=aegis-preflight-{project_token}"
    for command in (
        ["docker", "ps", "--all", "--quiet", "--filter", label],
        ["docker", "network", "ls", "--quiet", "--filter", label],
        ["docker", "volume", "ls", "--quiet", "--filter", label],
    ):
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        assert result.stdout == ""


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

    def fake_run(arguments: list[str], **kwargs: Any) -> Result:
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


def test_observer_child_has_a_hard_output_file_size_limit(tmp_path: Path) -> None:
    output = tmp_path / "observer-output"
    with output.open("wb") as stream:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os\nwhile True: os.write(1, b'x' * 65536)",
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.DEVNULL,
            preexec_fn=set_observer_output_limit,
        )

    assert result.returncode != 0
    assert output.stat().st_size <= MAX_MOUNTINFO_BYTES + 1


def test_observer_preexec_failure_is_translated_without_path_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "private-canary"
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

    def fail_run(*args: object, **kwargs: object) -> NoReturn:
        raise subprocess.SubprocessError(str(source))

    monkeypatch.setattr("aegisctl.mounts.subprocess.run", fail_run)

    with pytest.raises(ConfigError, match="observation") as caught:
        observe_mount_fingerprints(validated)

    assert str(source) not in str(caught.value)
