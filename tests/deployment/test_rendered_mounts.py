from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from aegisctl.mounts import (
    local_identity,
    observe_mount_fingerprints,
    parse_config,
    preflight_slots,
    render_artifacts,
    write_manifest,
)

REPOSITORY = Path(__file__).resolve().parents[2]
GATEWAY_ATTEST = REPOSITORY / "deploy/nginx/entrypoint/10-aegis-mount-attestation.sh"
NGINX_IMAGE = (
    "nginxinc/nginx-unprivileged:1.30.4-alpine@"
    "sha256:45ce1e2e699234253d1def7baa96218a5d00b498d1ba0cbb1a17b6bdf73d1351"
)


def _docker(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"AEGIS_GATEWAY_MOUNT_ATTESTATION": ""},
        {"AEGIS_GATEWAY_MOUNT_ATTESTATION_SHA256": ""},
        {
            "AEGIS_GATEWAY_MOUNT_ATTESTATION": "",
            "AEGIS_GATEWAY_MOUNT_ATTESTATION_SHA256": "",
        },
        {"AEGIS_GATEWAY_MOUNT_ATTESTATION": "/private-canary"},
        {"AEGIS_GATEWAY_MOUNT_ATTESTATION_SHA256": "0" * 64},
        {
            "AEGIS_GATEWAY_MOUNT_ATTESTATION": "/private-canary",
            "AEGIS_GATEWAY_MOUNT_ATTESTATION_SHA256": "",
        },
        {
            "AEGIS_GATEWAY_MOUNT_ATTESTATION": "",
            "AEGIS_GATEWAY_MOUNT_ATTESTATION_SHA256": "0" * 64,
        },
        {
            "AEGIS_GATEWAY_MOUNT_ATTESTATION": "/private-canary",
            "AEGIS_GATEWAY_MOUNT_ATTESTATION_SHA256": "0" * 64,
        },
    ],
    ids=(
        "both-unset",
        "attestation-empty-digest-unset",
        "attestation-unset-digest-empty",
        "both-empty",
        "attestation-value-digest-unset",
        "attestation-unset-digest-value",
        "attestation-value-digest-empty",
        "attestation-empty-digest-value",
        "both-values",
    ),
)
def test_gateway_mount_attestation_is_noop_only_when_both_settings_are_unset(
    settings: dict[str, str],
) -> None:
    result = subprocess.run(
        ["/bin/sh", GATEWAY_ATTEST],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]} | settings,
    )

    if not settings:
        assert result.returncode == 0
        assert result.stdout == result.stderr == ""
        return
    assert result.returncode != 0
    failure = json.loads(result.stderr)
    assert failure["message"] == "Gateway mount attestation failed"
    assert "private-canary" not in result.stderr


def test_gateway_shell_attests_real_ro_bind_by_fingerprint(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    config = tmp_path / "mounts.toml"
    manifest = tmp_path / "manifest.json"
    compose = tmp_path / "compose.yaml"
    attestation = tmp_path / "gateway.attestation"
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
    observed = observe_mount_fingerprints(preflight_slots(parse_config(config)))
    write_manifest(manifest, observed, uid=os.geteuid(), gid=os.getegid())
    rendered = render_artifacts(
        config,
        manifest,
        compose,
        attestation,
        uid=os.geteuid(),
        gid=os.getegid(),
    )
    attestation.chmod(0o644)

    result = _docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=2m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        "101:101",
        "--env",
        "AEGIS_GATEWAY_MOUNT_ATTESTATION=/run/aegis/mounts.gateway.attestation",
        "--env",
        f"AEGIS_GATEWAY_MOUNT_ATTESTATION_SHA256={rendered.gateway_digest}",
        "--mount",
        f"type=bind,src={GATEWAY_ATTEST},dst=/usr/local/bin/aegis-mount-attest,readonly",
        "--mount",
        f"type=bind,src={attestation},dst=/run/aegis/mounts.gateway.attestation,readonly",
        "--mount",
        f"type=bind,src={source},dst=/srv/aegis/roots/photos,readonly",
        "--entrypoint",
        "/bin/sh",
        NGINX_IMAGE,
        "/usr/local/bin/aegis-mount-attest",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""

    fields = attestation.read_text(encoding="ascii").rstrip("\n").split("|")
    fields[2] = "12abc"
    malformed = "|".join(fields) + "\n"
    attestation.write_text(malformed, encoding="ascii")
    malformed_digest = hashlib.sha256(malformed.encode("ascii")).hexdigest()
    malformed_result = _docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=2m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        "101:101",
        "--env",
        "AEGIS_GATEWAY_MOUNT_ATTESTATION=/run/aegis/mounts.gateway.attestation",
        "--env",
        f"AEGIS_GATEWAY_MOUNT_ATTESTATION_SHA256={malformed_digest}",
        "--mount",
        f"type=bind,src={GATEWAY_ATTEST},dst=/usr/local/bin/aegis-mount-attest,readonly",
        "--mount",
        f"type=bind,src={attestation},dst=/run/aegis/mounts.gateway.attestation,readonly",
        "--mount",
        f"type=bind,src={source},dst=/srv/aegis/roots/photos,readonly",
        "--entrypoint",
        "/bin/sh",
        NGINX_IMAGE,
        "/usr/local/bin/aegis-mount-attest",
    )

    assert malformed_result.returncode != 0
    assert json.loads(malformed_result.stderr)["message"] == (
        "Gateway mount attestation failed"
    )
    assert str(source) not in malformed_result.stderr


def test_generated_compose_survives_compose_config_and_has_worker_attest_commands(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source,with:punctuation"
    source.mkdir()
    config = tmp_path / "mounts.toml"
    manifest = tmp_path / "manifest.json"
    output = tmp_path / "compose.generated.yaml"
    gateway = tmp_path / "gateway.attestation"
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
    observed = observe_mount_fingerprints(preflight_slots(parse_config(config)))
    write_manifest(manifest, observed, uid=os.geteuid(), gid=os.getegid())
    render_artifacts(
        config,
        manifest,
        output,
        gateway,
        uid=os.geteuid(),
        gid=os.getegid(),
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(REPOSITORY / "compose.yaml"),
            "-f",
            str(output),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=os.environ | {"AEGIS_UID": str(os.geteuid()), "AEGIS_GID": str(os.getegid())},
    )
    rendered = json.loads(result.stdout)
    services = rendered["services"]

    assert "/srv/aegis/roots/photos" not in {
        mount["target"] for mount in services["web"].get("volumes", [])
    }
    assert "/srv/aegis/roots/photos" not in {
        mount["target"] for mount in services["migrate"].get("volumes", [])
    }
    for role in ("operations", "indexer", "media"):
        command = " ".join(services[role]["command"])
        assert "aegisctl mounts attest" in command
        assert f"--role {role}" in command
    generated = yaml.safe_load(output.read_text(encoding="ascii"))
    for service in generated["services"].values():
        targets = [mount["target"] for mount in service.get("volumes", [])]
        assert len(targets) == len(set(targets))


@pytest.mark.parametrize("interpolation_value", [None, "rewritten"])
def test_compose_bind_sources_preserve_literal_dollars_for_preflight_and_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interpolation_value: str | None,
) -> None:
    if interpolation_value is None:
        monkeypatch.delenv("AEGIS_INTERP_CANARY", raising=False)
    else:
        monkeypatch.setenv("AEGIS_INTERP_CANARY", interpolation_value)
    literal_parent = tmp_path / "$AEGIS_INTERP_CANARY"
    source = literal_parent / "root"
    source.mkdir(parents=True)
    config = literal_parent / "mounts.toml"
    manifest = literal_parent / "manifest.json"
    output = literal_parent / "compose.generated.yaml"
    attestation = literal_parent / "gateway.attestation"
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

    observed = observe_mount_fingerprints(preflight_slots(parse_config(config)))
    write_manifest(manifest, observed, uid=os.geteuid(), gid=os.getegid())
    render_artifacts(
        config,
        manifest,
        output,
        attestation,
        uid=os.geteuid(),
        gid=os.getegid(),
    )
    configured = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(REPOSITORY / "compose.yaml"),
            "-f",
            str(output),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=os.environ | {"AEGIS_UID": str(os.geteuid()), "AEGIS_GID": str(os.getegid())},
    )
    services = json.loads(configured.stdout)["services"]
    expected_sources = {
        "/srv/aegis/roots/photos": str(source.resolve()),
        "/run/aegis/mounts.manifest.json": str(manifest.resolve()),
        "/run/aegis/mounts.gateway.attestation": str(attestation.resolve()),
    }
    for target, literal_source in expected_sources.items():
        configured_sources = {
            mount["source"]
            for service in services.values()
            for mount in service.get("volumes", [])
            if mount["target"] == target
        }
        assert configured_sources == {literal_source.replace("$", "$$")}

    project = "aegis-dollar-" + hashlib.sha256(
        str(tmp_path).encode("utf-8")
    ).hexdigest()[:12]
    compose_command = [
        "docker",
        "compose",
        "--project-name",
        project,
        "-f",
        str(REPOSITORY / "compose.yaml"),
        "-f",
        str(output),
    ]
    try:
        runtime = subprocess.run(
            [
                *compose_command,
                "run",
                "--rm",
                "--no-deps",
                "--pull",
                "never",
                "--no-TTY",
                "--entrypoint",
                "aegisctl",
                "indexer",
                "mounts",
                "attest",
                "--manifest",
                "/run/aegis/mounts.manifest.json",
                "--role",
                "indexer",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env=os.environ
            | {"AEGIS_UID": str(os.geteuid()), "AEGIS_GID": str(os.getegid())},
        )
    finally:
        subprocess.run(
            [*compose_command, "down", "--remove-orphans", "--volumes"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            env=os.environ
            | {"AEGIS_UID": str(os.geteuid()), "AEGIS_GID": str(os.getegid())},
        )

    assert runtime.returncode == 0, runtime.stderr
    assert json.loads(runtime.stdout)["status"] == "attested"


def test_gateway_attestation_uses_one_private_snapshot_when_source_mutates(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    config = tmp_path / "mounts.toml"
    manifest = tmp_path / "manifest.json"
    compose = tmp_path / "compose.yaml"
    attestation = tmp_path / "gateway.attestation"
    commands = tmp_path / "commands"
    commands.mkdir()
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
    observed = observe_mount_fingerprints(preflight_slots(parse_config(config)))
    write_manifest(manifest, observed, uid=os.geteuid(), gid=os.getegid())
    rendered = render_artifacts(
        config,
        manifest,
        compose,
        attestation,
        uid=os.geteuid(),
        gid=os.getegid(),
    )
    attestation.chmod(0o666)
    sha256sum = commands / "sha256sum"
    sha256sum.write_text(
        """#!/bin/sh
set -eu
digest="$(/bin/busybox sha256sum)"
printf 'mutated-after-hash\n' > "$AEGIS_GATEWAY_MOUNT_ATTESTATION"
printf '%s\n' "$digest"
""",
        encoding="ascii",
    )
    sha256sum.chmod(0o755)

    result = _docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=2m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        "101:101",
        "--env",
        "PATH=/commands:/usr/sbin:/usr/bin:/sbin:/bin",
        "--env",
        "AEGIS_GATEWAY_MOUNT_ATTESTATION=/run/aegis/mounts.gateway.attestation",
        "--env",
        f"AEGIS_GATEWAY_MOUNT_ATTESTATION_SHA256={rendered.gateway_digest}",
        "--mount",
        f"type=bind,src={commands},dst=/commands,readonly",
        "--mount",
        f"type=bind,src={GATEWAY_ATTEST},dst=/usr/local/bin/aegis-mount-attest,readonly",
        "--mount",
        f"type=bind,src={attestation},dst=/run/aegis/mounts.gateway.attestation",
        "--mount",
        f"type=bind,src={source},dst=/srv/aegis/roots/photos,readonly",
        "--entrypoint",
        "/bin/sh",
        NGINX_IMAGE,
        "/usr/local/bin/aegis-mount-attest",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


def test_backend_image_packages_cli() -> None:
    result = _docker(
        "run",
        "--rm",
        "--entrypoint",
        "aegisctl",
        "aegis-backend",
        "--help",
    )

    assert result.returncode == 0, result.stderr
    assert "mounts" in result.stdout


def test_backend_runtime_reuses_gateway_fingerprint_and_enforces_role_mode(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    config = tmp_path / "mounts.toml"
    manifest = tmp_path / "manifest.json"
    config.write_text(
        f"""
version = 1
[[slots]]
slot_id = "uploads"
source = "{source}"
container_path = "/srv/aegis/roots/uploads"
mode = "read_write"
expected_identity = "{local_identity(source)}"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    host_slots = preflight_slots(parse_config(config))
    first = observe_mount_fingerprints(host_slots)
    second = observe_mount_fingerprints(host_slots)
    assert first[0].mount_fingerprint == second[0].mount_fingerprint
    digest = write_manifest(manifest, first, uid=os.geteuid(), gid=os.getegid())

    def run(role: str, *, readonly: bool, expected_digest: str = digest):
        source_mount = f"type=bind,src={source},dst=/srv/aegis/roots/uploads"
        if readonly:
            source_mount += ",readonly"
        return _docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            f"{os.geteuid()}:{os.getegid()}",
            "--env",
            f"AEGIS_MOUNT_MANIFEST_SHA256={expected_digest}",
            "--mount",
            f"type=bind,src={manifest},dst=/run/aegis/mounts.manifest.json,readonly",
            "--mount",
            source_mount,
            "--entrypoint",
            "aegisctl",
            "aegis-backend",
            "mounts",
            "attest",
            "--manifest",
            "/run/aegis/mounts.manifest.json",
            "--role",
            role,
        )

    indexer = run("indexer", readonly=True)
    operations_wrong_mode = run("operations", readonly=True)
    operations = run("operations", readonly=False)
    wrong_digest = run("indexer", readonly=True, expected_digest="0" * 64)

    assert indexer.returncode == 0, indexer.stderr
    assert operations.returncode == 0, operations.stderr
    for rejected in (operations_wrong_mode, wrong_digest):
        assert rejected.returncode != 0
        error = json.loads(rejected.stderr)
        assert error["status"] == "error"
        assert str(source) not in rejected.stderr
        assert local_identity(source) not in rejected.stderr
