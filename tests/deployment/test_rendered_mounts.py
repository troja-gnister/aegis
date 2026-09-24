from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from aegisctl.container_engine import (
    compose_environment,
    container_command,
)
from aegisctl.container_resources import (
    ProjectInventory,
    ProjectResource,
    ProjectResourceRule,
    admit_project_transition,
    capture_project_inventory,
    cleanup_project_inventory,
    require_empty_project,
)
from aegisctl.mounts import (
    ConfigError,
    SlotSpec,
    local_identity,
    observe_mount_fingerprints,
    parse_config,
    preflight_slots,
    render_artifacts,
    write_manifest,
)

from tests.support.container_runtime import (
    FreshTestTree,
    copy_bind_inputs,
    map_inventory_files_to_container_user,
    prepare_owned_test_inventory,
    record_created_test_path,
    record_fresh_test_tree,
    record_test_tree_inventory,
)
from tests.support.fake_container_engine import ProjectEngine, select_fake_engine

REPOSITORY = Path(__file__).resolve().parents[2]
CONTAINER_COMMAND = container_command()
GATEWAY_ATTEST = REPOSITORY / "deploy/nginx/entrypoint/10-aegis-mount-attestation.sh"
NGINX_IMAGE = (
    "docker.io/nginxinc/nginx-unprivileged:1.30.4-alpine@"
    "sha256:45ce1e2e699234253d1def7baa96218a5d00b498d1ba0cbb1a17b6bdf73d1351"
)


def _docker(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*CONTAINER_COMMAND, *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _run_permission_probe(
    arguments: list[str], source: Path, mode: int,
) -> subprocess.CompletedProcess[str]:
    readable = _docker(*arguments)
    if readable.returncode:
        raise AssertionError("readable synthetic attestation precondition failed")
    source.chmod(mode)
    try:
        return _docker(*arguments)
    finally:
        source.chmod(0o755)


def _create_literal_dollar_inputs(
    parent: Path, tree: FreshTestTree,
) -> tuple[Path, Path]:
    literal_parent = parent / "$AEGIS_INTERP_CANARY"
    source = literal_parent / "root"
    literal_parent.mkdir()
    record_created_test_path(tree, literal_parent)
    source.mkdir()
    record_created_test_path(tree, source)
    config = literal_parent / "mounts.toml"
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
    record_created_test_path(tree, config)
    return source, config


def _cleanup_rendered_oneoff(project: str, expected: ProjectInventory) -> None:
    created = admit_project_transition(
        expected, capture_project_inventory(project), (
            ProjectResourceRule("container", (
                ("com.docker.compose.service", "indexer"),
                ("com.docker.compose.oneoff", "True"),
            )),
            ProjectResourceRule("network", (
                ("com.docker.compose.network", "backend"),
            )),
            ProjectResourceRule("volume", (
                ("com.docker.compose.volume", "indexer-coordination"),
            )),
            ProjectResourceRule("volume", (
                ("com.docker.compose.volume", "postgres-data"),
            )),
            ProjectResourceRule("volume", (
                ("com.docker.compose.volume", "model-cache"),
            )),
        ),
    )
    cleanup_project_inventory(created)


def test_literal_dollar_fixture_records_each_created_path_once(tmp_path: Path) -> None:
    tree = record_fresh_test_tree(tmp_path)

    source, config = _create_literal_dollar_inputs(tmp_path, tree)

    assert source == tmp_path / "$AEGIS_INTERP_CANARY/root"
    assert config == tmp_path / "$AEGIS_INTERP_CANARY/mounts.toml"
    assert record_test_tree_inventory(tree).entries == tree.entries


def test_rendered_oneoff_refuses_unknown_transition_before_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = ProjectInventory("rendered", ())
    unknown = ProjectResource(
        "container", "unknown", "unknown",
        json.dumps({
            "Id": "unknown", "Created": "now", "Name": "unknown", "Image": "image",
            "Labels": {
                "com.docker.compose.project": "rendered",
                "com.docker.compose.service": "unknown",
                "com.docker.compose.oneoff": "True",
            },
        }, sort_keys=True, separators=(",", ":")),
    )
    cleaned: list[ProjectInventory] = []
    monkeypatch.setattr(
        f"{__name__}.capture_project_inventory",
        lambda project: ProjectInventory(project, (unknown,)),
    )
    monkeypatch.setattr(f"{__name__}.cleanup_project_inventory", cleaned.append)

    with pytest.raises(RuntimeError, match="unexpected"):
        _cleanup_rendered_oneoff("rendered", before)
    assert cleaned == []


def test_permission_probe_requires_readable_success_before_mode_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir(mode=0o755)
    events: list[object] = []

    def completed(*arguments: str) -> subprocess.CompletedProcess[str]:
        events.append(("run", source.stat().st_mode & 0o777, arguments))
        return subprocess.CompletedProcess(arguments, 0 if len(events) == 1 else 1, "", "")

    monkeypatch.setattr(f"{__name__}._docker", completed)

    result = _run_permission_probe(["run", "image"], source, 0o000)
    assert result.returncode == 1
    assert events == [
        ("run", 0o755, ("run", "image")),
        ("run", 0o000, ("run", "image")),
    ]
    assert source.stat().st_mode & 0o777 == 0o755


def test_real_observer_rejects_physical_parent_child_bind_roots(tmp_path: Path) -> None:
    tree = record_fresh_test_tree(tmp_path)
    parent = tmp_path / "tree"
    child = parent / "private"
    child.mkdir(parents=True)
    record_created_test_path(tree, parent)
    record_created_test_path(tree, child)
    # Validate independently to reach the observer's own physical-alias boundary.
    # A host bind alias can have unrelated realpaths with these same mount roots.
    slots = tuple(
        preflight_slots(
            [
                SlotSpec(
                    slot_id,
                    source,
                    f"/srv/aegis/roots/{slot_id}",
                    "read_only",
                    local_identity(source),
                )
            ]
        )[0]
        for source, slot_id in ((parent, "parent"), (child, "alias"))
    )
    prepare_owned_test_inventory(record_test_tree_inventory(tree))
    with pytest.raises(ConfigError, match="overlap"):
        observe_mount_fingerprints(slots)


def test_linux_preflight_rejects_real_bind_alias_ancestry(tmp_path: Path) -> None:
    tree = record_fresh_test_tree(tmp_path)
    parent = tmp_path / "tree"
    child = parent / "private"
    child.mkdir(parents=True)
    record_created_test_path(tree, parent)
    record_created_test_path(tree, child)
    script = """
from pathlib import Path
from aegisctl.mounts import ConfigError, SlotSpec, local_identity, preflight_slots
slots = [SlotSpec(name, Path(path), '/srv/aegis/roots/' + name, 'read_only',
                  local_identity(Path(path)))
         for name, path in [('parent', '/fixture/tree'), ('alias', '/fixture/private-alias')]]
try:
    preflight_slots(slots)
except ConfigError as error:
    if 'overlap' not in str(error):
        raise SystemExit(2)
    print('physical overlap rejected')
else:
    raise SystemExit(3)
"""
    bind_inputs = copy_bind_inputs(
        tmp_path, {"aegisctl": REPOSITORY / "backend/aegisctl"}, parent_tree=tree,
    )
    prepare_owned_test_inventory(record_test_tree_inventory(tree))
    result = _docker(
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
        "10001:10001",
        "--mount",
        f"type=bind,src={parent},dst=/fixture/tree,readonly",
        "--mount",
        f"type=bind,src={child},dst=/fixture/private-alias,readonly",
        "--mount",
        f"type=bind,src={bind_inputs['aegisctl']},dst=/app/backend/aegisctl,readonly",
        "--entrypoint",
        "python",
        "aegis-backend",
        "-c",
        script,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "physical overlap rejected\n"


@pytest.mark.parametrize("role", ["gateway", "operations", "indexer", "media"])
@pytest.mark.parametrize("mode", [0o000, 0o444, 0o111])
def test_runtime_attestation_rejects_effective_permission_denial(
    tmp_path: Path,
    role: str,
    mode: int,
) -> None:
    tree = record_fresh_test_tree(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    sentinel = source / "keep"
    sentinel.write_bytes(b"synthetic original")
    config = tmp_path / "mounts.toml"
    config.write_text(
        f'version = 1\n[[slots]]\nslot_id = "photos"\nsource = "{source}"\n'
        'container_path = "/srv/aegis/roots/photos"\nmode = "read_only"\n'
        f'expected_identity = "{local_identity(source)}"\n',
    )
    record_created_test_path(tree, source, recursive=True)
    record_created_test_path(tree, config)
    prepare_owned_test_inventory(record_test_tree_inventory(tree))
    observed = observe_mount_fingerprints(preflight_slots(parse_config(config)))
    manifest, attestation = tmp_path / "manifest.json", tmp_path / "gateway.attestation"
    digest = write_manifest(manifest, observed, uid=os.geteuid(), gid=os.getegid())
    rendered = render_artifacts(
        config, manifest, tmp_path / "compose.yaml", attestation, uid=os.geteuid(), gid=os.getegid()
    )
    record_created_test_path(tree, manifest)
    record_created_test_path(tree, tmp_path / "compose.yaml")
    record_created_test_path(tree, attestation)
    bind_inputs = copy_bind_inputs(tmp_path, {
        "gateway-attest": GATEWAY_ATTEST,
        "aegisctl": REPOSITORY / "backend/aegisctl",
    }, parent_tree=tree)
    inventory = record_test_tree_inventory(tree)
    prepare_owned_test_inventory(inventory)
    if role != "gateway":
        map_inventory_files_to_container_user(
            inventory, (manifest,), uid=os.geteuid(), gid=os.getegid(),
        )
    arguments = [
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--mount",
        f"type=bind,src={source},dst=/srv/aegis/roots/photos,readonly",
    ]
    if role == "gateway":
        arguments += [
            "--user",
            "101:101",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=2m",
            "--env",
            "AEGIS_GATEWAY_MOUNT_ATTESTATION=/run/aegis/mounts.gateway.attestation",
            "--env",
            f"AEGIS_GATEWAY_MOUNT_ATTESTATION_SHA256={rendered.gateway_digest}",
            "--mount",
            f"type=bind,src={attestation},dst=/run/aegis/mounts.gateway.attestation,readonly",
            "--mount",
            f"type=bind,src={bind_inputs['gateway-attest']},"
            "dst=/usr/local/bin/aegis-mount-attest,readonly",
            "--entrypoint",
            "/bin/sh",
            NGINX_IMAGE,
            "/usr/local/bin/aegis-mount-attest",
        ]
    else:
        arguments += [
            "--user",
            f"{os.geteuid()}:{os.getegid()}",
            "--env",
            f"AEGIS_MOUNT_MANIFEST_SHA256={digest}",
            "--mount",
            f"type=bind,src={manifest},dst=/run/aegis/mounts.manifest.json,readonly",
            "--mount",
            f"type=bind,src={bind_inputs['aegisctl']},dst=/app/backend/aegisctl,readonly",
            "--entrypoint",
            "python",
            "aegis-backend",
            "-m",
            "aegisctl",
            "mounts",
            "attest",
            "--manifest",
            "/run/aegis/mounts.manifest.json",
            "--role",
            role,
        ]
    result = _run_permission_probe(arguments, source, mode)
    if (sys.platform != "linux" and result.returncode == 126
            and "invalid mount config" in result.stderr and "permission denied" in result.stderr):
        pytest.skip("Docker Desktop denied the unreadable host bind before the attester ran")
    assert result.returncode != 0
    failure = json.loads(result.stderr)
    assert "attestation failed" in failure["message"].lower()
    assert str(source) not in result.stderr
    assert sentinel.read_bytes() == b"synthetic original"


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


def test_real_observer_leaves_no_unique_project_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = record_fresh_test_tree(tmp_path)
    source = tmp_path / "private-canary"
    source.mkdir()
    config = tmp_path / "mounts.toml"
    config.write_text(
        f'version = 1\n[[slots]]\nslot_id = "photos"\nsource = "{source}"\n'
        'container_path = "/srv/aegis/roots/photos"\nmode = "read_only"\n'
        f'expected_identity = "{local_identity(source)}"\n',
        encoding="utf-8",
    )
    record_created_test_path(tree, source)
    record_created_test_path(tree, config)
    slots = preflight_slots(parse_config(config))
    project_token = hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:16]
    real_token_hex = secrets.token_hex

    def fixed_project_token(length: int) -> str:
        return project_token if length == 8 else real_token_hex(length)

    monkeypatch.setattr("aegisctl.mounts.secrets.token_hex", fixed_project_token)
    prepare_owned_test_inventory(record_test_tree_inventory(tree))
    observed = observe_mount_fingerprints(slots)
    assert len(observed[0].mount_fingerprint) == 64
    label = f"label=com.docker.compose.project=aegis-preflight-{project_token}"
    for command in (
        ["ps", "--all", "--quiet", "--filter", label],
        ["network", "ls", "--quiet", "--filter", label],
        ["volume", "ls", "--quiet", "--filter", label],
    ):
        result = _docker(*command)
        assert result.returncode == 0
        assert result.stdout == ""


def test_gateway_shell_attests_real_ro_bind_by_fingerprint(tmp_path: Path) -> None:
    tree = record_fresh_test_tree(tmp_path)
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
mode = "read_write"
expected_identity = "{local_identity(source)}"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    record_created_test_path(tree, source)
    record_created_test_path(tree, config)
    prepare_owned_test_inventory(record_test_tree_inventory(tree))
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
    record_created_test_path(tree, manifest)
    record_created_test_path(tree, compose)
    record_created_test_path(tree, attestation)
    attestation.chmod(0o644)
    gateway_script = copy_bind_inputs(
        tmp_path, {"gateway-attest": GATEWAY_ATTEST}, parent_tree=tree,
    )["gateway-attest"]
    prepare_owned_test_inventory(record_test_tree_inventory(tree))

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
        f"type=bind,src={gateway_script},dst=/usr/local/bin/aegis-mount-attest,readonly",
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
        f"type=bind,src={gateway_script},dst=/usr/local/bin/aegis-mount-attest,readonly",
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
    assert json.loads(malformed_result.stderr)["message"] == ("Gateway mount attestation failed")
    assert str(source) not in malformed_result.stderr


def test_generated_compose_survives_compose_config_and_has_worker_attest_commands(
    tmp_path: Path,
) -> None:
    tree = record_fresh_test_tree(tmp_path)
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
mode = "read_write"
expected_identity = "{local_identity(source)}"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    record_created_test_path(tree, source)
    record_created_test_path(tree, config)
    prepare_owned_test_inventory(record_test_tree_inventory(tree))
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
            *CONTAINER_COMMAND,
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
        env=compose_environment(os.environ | {
            "AEGIS_UID": str(os.geteuid()),
            "AEGIS_GID": str(os.getegid()),
            "AEGIS_RELEASE_ID": "mount-test-release",
        }),
    )
    rendered = json.loads(result.stdout)
    services = rendered["services"]

    assert "/srv/aegis/roots/photos" not in {
        mount["target"] for mount in services["web"].get("volumes", [])
    }
    assert "/srv/aegis/roots/photos" not in {
        mount["target"] for mount in services["migrate"].get("volumes", [])
    }
    migrate_mounts = services["migrate"].get("volumes", [])
    assert {
        (mount["target"], mount["read_only"]) for mount in migrate_mounts
    } == {("/run/aegis/mounts.manifest.json", True)}
    assert services["migrate"]["environment"]["AEGIS_MOUNT_MANIFEST_SHA256"] == (
        hashlib.sha256(manifest.read_bytes()).hexdigest()
    )
    for role in ("operations", "indexer", "media"):
        command = " ".join(services[role]["command"])
        assert "aegisctl mounts attest" in command
        assert f"--role {role}" in command
        root_mount = next(
            mount
            for mount in services[role]["volumes"]
            if mount["target"] == "/srv/aegis/roots/photos"
        )
        assert root_mount["read_only"] is True
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
    tree = record_fresh_test_tree(tmp_path)
    if interpolation_value is None:
        monkeypatch.delenv("AEGIS_INTERP_CANARY", raising=False)
    else:
        monkeypatch.setenv("AEGIS_INTERP_CANARY", interpolation_value)
    source, config = _create_literal_dollar_inputs(tmp_path, tree)
    literal_parent = source.parent
    config = literal_parent / "mounts.toml"
    manifest = literal_parent / "manifest.json"
    output = literal_parent / "compose.generated.yaml"
    attestation = literal_parent / "gateway.attestation"
    prepare_owned_test_inventory(record_test_tree_inventory(tree))
    observed = observe_mount_fingerprints(preflight_slots(parse_config(config)))
    write_manifest(manifest, observed, uid=os.geteuid(), gid=os.getegid())
    record_created_test_path(tree, manifest)
    render_artifacts(
        config,
        manifest,
        output,
        attestation,
        uid=os.geteuid(),
        gid=os.getegid(),
    )
    record_created_test_path(tree, output)
    record_created_test_path(tree, attestation)
    configured = subprocess.run(
        [
            *CONTAINER_COMMAND,
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
        env=compose_environment(os.environ | {
            "AEGIS_UID": str(os.geteuid()),
            "AEGIS_GID": str(os.getegid()),
            "AEGIS_RELEASE_ID": "mount-test-release",
        }),
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

    project = "aegis-dollar-" + hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:12]
    # Even an attestation-only one-off container needs its declared secret binds.
    # Give every declaration private synthetic input, never operator/dev files.
    secret_sources = {}
    secret_paths = []
    for name in yaml.safe_load((REPOSITORY / "compose.yaml").read_text())["secrets"]:
        secret = tmp_path / name
        secret.write_text(secrets.token_hex(32) + "\n", encoding="ascii")
        secret.chmod(0o600)
        record_created_test_path(tree, secret)
        secret_paths.append(secret)
        secret_sources[name] = {"file": str(secret).replace("$", "$$")}
    secret_override = tmp_path / "compose.secrets.yaml"
    secret_override.write_text(yaml.safe_dump({"secrets": secret_sources}), encoding="utf-8")
    record_created_test_path(tree, secret_override)
    inventory = record_test_tree_inventory(tree)
    prepare_owned_test_inventory(inventory)
    map_inventory_files_to_container_user(
        inventory, (*secret_paths, manifest), uid=os.geteuid(), gid=os.getegid(),
    )
    compose_command = [
        *CONTAINER_COMMAND,
        "compose",
        "--env-file",
        "/dev/null",
        "--project-name",
        project,
        "-f",
        str(REPOSITORY / "compose.yaml"),
        "-f",
        str(output),
        "-f",
        str(secret_override),
    ]
    expected_resources = require_empty_project(project)
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
            env=compose_environment(os.environ | {
                "AEGIS_UID": str(os.geteuid()),
                "AEGIS_GID": str(os.getegid()),
                "AEGIS_RELEASE_ID": "mount-test-release",
            }),
        )
    finally:
        _cleanup_rendered_oneoff(project, expected_resources)

    assert runtime.returncode == 0, runtime.stderr
    assert json.loads(runtime.stdout)["status"] == "attested"


def test_gateway_attestation_uses_one_private_snapshot_when_source_mutates(
    tmp_path: Path,
) -> None:
    tree = record_fresh_test_tree(tmp_path)
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
    record_created_test_path(tree, source)
    record_created_test_path(tree, commands)
    record_created_test_path(tree, config)
    prepare_owned_test_inventory(record_test_tree_inventory(tree))
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
    record_created_test_path(tree, manifest)
    record_created_test_path(tree, compose)
    record_created_test_path(tree, attestation)
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
    record_created_test_path(tree, sha256sum)
    gateway_script = copy_bind_inputs(
        tmp_path, {"gateway-attest": GATEWAY_ATTEST}, parent_tree=tree,
    )["gateway-attest"]
    prepare_owned_test_inventory(record_test_tree_inventory(tree))

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
        f"type=bind,src={gateway_script},dst=/usr/local/bin/aegis-mount-attest,readonly",
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
    tree = record_fresh_test_tree(tmp_path)
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
    record_created_test_path(tree, source)
    record_created_test_path(tree, config)
    host_slots = preflight_slots(parse_config(config))
    prepare_owned_test_inventory(record_test_tree_inventory(tree))
    first = observe_mount_fingerprints(host_slots)
    second = observe_mount_fingerprints(host_slots)
    assert first[0].mount_fingerprint == second[0].mount_fingerprint
    digest = write_manifest(manifest, first, uid=os.geteuid(), gid=os.getegid())
    record_created_test_path(tree, manifest)
    inventory = record_test_tree_inventory(tree)
    prepare_owned_test_inventory(inventory)
    map_inventory_files_to_container_user(
        inventory, (manifest,), uid=os.geteuid(), gid=os.getegid(),
    )

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
    operations = run("operations", readonly=True)
    operations_writable = run("operations", readonly=False)
    wrong_digest = run("indexer", readonly=True, expected_digest="0" * 64)

    assert indexer.returncode == 0, indexer.stderr
    assert operations.returncode == 0, operations.stderr
    for rejected in (operations_writable, wrong_digest):
        assert rejected.returncode != 0
        error = json.loads(rejected.stderr)
        assert error["status"] == "error"
        assert str(source) not in rejected.stderr
        assert local_identity(source) not in rejected.stderr


@pytest.mark.parametrize("engine", ("docker", "podman"))
@pytest.mark.parametrize("unknown", (False, True))
def test_rendered_complete_observed_inventory_exact_cleanup_or_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine: str, unknown: bool,
) -> None:
    select_fake_engine(engine, tmp_path, monkeypatch)
    declared = (
        ("network", "backend"), ("volume", "postgres-data"), ("volume", "indexer-coordination"),
    )
    if unknown:
        declared += (("volume", "unknown"),)
    fake = ProjectEngine(engine, "probe", declared)
    monkeypatch.setattr(subprocess, "run", fake)
    before = require_empty_project("probe")
    fake.create()
    if unknown:
        with pytest.raises(RuntimeError, match="unexpected project resource transition"):
            _cleanup_rendered_oneoff("probe", before)
        assert fake.removals == []
        assert len(fake.resources) == len(declared)
    else:
        _cleanup_rendered_oneoff("probe", before)
        assert fake.removals == [
            ("network", "rm", "immutable-backend"),
            ("volume", "rm", "probe_indexer-coordination"), ("volume", "rm", "probe_postgres-data"),
        ]
        assert fake.resources == {}
        assert fake.queries[-3:] == ["container", "network", "volume"]
