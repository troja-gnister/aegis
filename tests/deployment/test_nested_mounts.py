from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from aegisctl.container_engine import container_command
from aegisctl.mounts import (
    ConfigError,
    SlotSpec,
    local_identity,
    observe_mount_fingerprints,
    preflight_slots,
    render_artifacts,
    write_manifest,
)

from tests.support.container_runtime import (
    copy_bind_inputs,
    map_inventory_files_to_container_user,
    prepare_owned_test_inventory,
    record_created_test_path,
    record_fresh_test_tree,
    record_test_tree_inventory,
)

REPOSITORY = Path(__file__).resolve().parents[2]
CONTAINER_COMMAND = container_command()
GATEWAY_ATTEST = REPOSITORY / "deploy/nginx/entrypoint/10-aegis-mount-attestation.sh"
NGINX_IMAGE = (
    "docker.io/nginxinc/nginx-unprivileged:1.30.4-alpine@"
    "sha256:45ce1e2e699234253d1def7baa96218a5d00b498d1ba0cbb1a17b6bdf73d1351"
)


@pytest.fixture(scope="module")
def leaf_fixture(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    workspace = tmp_path_factory.mktemp("nested-mounts")
    tree = record_fresh_test_tree(workspace)
    source, external = workspace / "original", workspace / "external"
    source.mkdir()
    record_created_test_path(tree, source)
    (source / "nested").mkdir()
    record_created_test_path(tree, source / "nested")
    (source / "deep").mkdir()
    record_created_test_path(tree, source / "deep")
    (source / "deep/nested").mkdir()
    record_created_test_path(tree, source / "deep/nested")
    external.mkdir()
    record_created_test_path(tree, external)
    sentinel = external / "keep"
    sentinel.write_bytes(b"synthetic original descendant")
    record_created_test_path(tree, sentinel)
    before = sentinel.stat().st_ino
    config = workspace / "mounts.toml"
    config.write_text(
        f'version = 1\n[[slots]]\nslot_id = "photos"\nsource = "{source}"\n'
        'container_path = "/srv/aegis/roots/photos"\nmode = "read_only"\n'
        f'expected_identity = "{local_identity(source)}"\n',
    )
    record_created_test_path(tree, config)
    bind_inputs = copy_bind_inputs(workspace, {
        "gateway-attest": GATEWAY_ATTEST,
        "aegisctl": REPOSITORY / "backend/aegisctl",
    }, parent_tree=tree)
    prepare_owned_test_inventory(record_test_tree_inventory(tree))
    slots = preflight_slots([
        SlotSpec("photos", source, "/srv/aegis/roots/photos", "read_only", local_identity(source))
    ])
    observed = observe_mount_fingerprints(slots)
    manifest, attestation = workspace / "manifest.json", workspace / "gateway.attestation"
    digest = write_manifest(manifest, observed, uid=os.geteuid(), gid=os.getegid())
    record_created_test_path(tree, manifest)
    rendered = render_artifacts(
        config, manifest, workspace / "compose.yaml", attestation,
        uid=os.geteuid(), gid=os.getegid(),
    )
    record_created_test_path(tree, workspace / "compose.yaml")
    record_created_test_path(tree, attestation)
    inventory = record_test_tree_inventory(tree)
    prepare_owned_test_inventory(inventory)
    map_inventory_files_to_container_user(
        inventory, (manifest,), uid=os.geteuid(), gid=os.getegid(),
    )
    yield {
        "source": source, "external": external, "slots": slots, "manifest": manifest,
        "attestation": attestation, "digest": digest, "gateway_digest": rendered.gateway_digest,
        "gateway_script": bind_inputs["gateway-attest"],
        "aegisctl": bind_inputs["aegisctl"],
    }
    assert sentinel.read_bytes() == b"synthetic original descendant"
    assert sentinel.stat().st_ino == before
    assert sorted(path.relative_to(source).as_posix() for path in source.rglob("*")) == [
        "deep", "deep/nested", "nested",
    ]  # Enumerate only this test's disposable synthetic tree.


def _runtime_arguments(fixture: dict[str, Any], role: str) -> list[str]:
    arguments = [
        *CONTAINER_COMMAND, "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--mount",
        f"type=bind,src={fixture['source']},dst=/srv/aegis/roots/photos,readonly",
    ]
    if role == "gateway":
        return [
            *arguments,
            "--user", "101:101", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=2m",
            "--env", "AEGIS_GATEWAY_MOUNT_ATTESTATION=/run/aegis/mounts.gateway.attestation",
            "--env", f"AEGIS_GATEWAY_MOUNT_ATTESTATION_SHA256={fixture['gateway_digest']}",
            "--mount",
            f"type=bind,src={fixture['attestation']},dst=/run/aegis/mounts.gateway.attestation,readonly",
            "--mount",
            f"type=bind,src={fixture['gateway_script']},"
            "dst=/usr/local/bin/aegis-mount-attest,readonly",
            "--entrypoint", "/bin/sh", NGINX_IMAGE, "/usr/local/bin/aegis-mount-attest",
        ]
    return [
        *arguments,
        "--user", f"{os.geteuid()}:{os.getegid()}",
        "--env", f"AEGIS_MOUNT_MANIFEST_SHA256={fixture['digest']}", "--mount",
        f"type=bind,src={fixture['manifest']},dst=/run/aegis/mounts.manifest.json,readonly",
        "--mount",
        f"type=bind,src={fixture['aegisctl']},dst=/app/backend/aegisctl,readonly",
        "--entrypoint", "python", "aegis-backend", "-m", "aegisctl", "mounts", "attest",
        "--manifest", "/run/aegis/mounts.manifest.json", "--role", role,
    ]


@pytest.mark.parametrize("role", ["gateway", "operations", "indexer", "media"])
def test_real_runtime_accepts_leaf_root(leaf_fixture: dict[str, Any], role: str) -> None:
    result = subprocess.run(
        _runtime_arguments(leaf_fixture, role), capture_output=True,
        text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("role", ["gateway", "operations", "indexer", "media"])
@pytest.mark.parametrize("mode", ["ro", "rw"])
@pytest.mark.parametrize("descendant", ["nested", "deep/nested"])
def test_real_runtime_rejects_descendant_mount_introduced_after_preflight(
    leaf_fixture: dict[str, Any], role: str, mode: str, descendant: str,
) -> None:
    arguments = _runtime_arguments(leaf_fixture, role)
    child_mount = (
        f"type=bind,src={leaf_fixture['external']},dst=/srv/aegis/roots/photos/{descendant}"
        + (",readonly" if mode == "ro" else "")
    )
    arguments[2:2] = ["--mount", child_mount]
    result = subprocess.run(
        arguments, capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode != 0
    failure = json.loads(result.stderr)
    assert "attestation failed" in failure["message"].lower()
    assert str(leaf_fixture["source"]) not in result.stderr
    assert str(leaf_fixture["external"]) not in result.stderr


def test_real_observer_rejects_descendant_mount_visible_only_in_container(
    leaf_fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_run = subprocess.run

    def inject_child(arguments: list[str], **kwargs: Any) -> Any:
        if arguments[:2] == [*CONTAINER_COMMAND, "compose"] and "run" in arguments:
            path = Path(arguments[arguments.index("-f") + 1])
            document = yaml.safe_load(path.read_text())
            document["services"]["mount-observer"]["volumes"].append({
                "type": "bind", "source": str(leaf_fixture["external"]),
                "target": "/srv/aegis/roots/photos/deep/nested", "read_only": True,
                "bind": {"create_host_path": False},
            })
            # Only the observer's disposable, test-owned Compose artifact is altered.
            path.write_text(yaml.safe_dump(document))
        return real_run(arguments, **kwargs)

    monkeypatch.setattr(subprocess, "run", inject_child)
    with pytest.raises(ConfigError, match="nested mount"):
        observe_mount_fingerprints(leaf_fixture["slots"])
