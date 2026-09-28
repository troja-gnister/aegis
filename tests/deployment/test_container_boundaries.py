from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from aegisctl.container_engine import (
    compose_environment,
    container_command,
    podman_compose_provider,
    selected_engine,
)
from aegisctl.container_resources import (
    ProjectInventory,
    ProjectResource,
    ProjectResourceRule,
    admit_project_transition,
    capture_project_inventory,
    cleanup_project_resource_kinds,
    require_empty_project,
    require_project_inventory,
)
from aegisctl.podman_mask_compatibility import MASK_OPTION

from tests.support.container_runtime import (
    FreshTestTree,
    prepare_owned_test_inventory,
    record_created_test_path,
    record_fresh_test_tree,
    record_test_tree_inventory,
)
from tests.support.container_runtime import (
    run_deployment_process as run_container,
)
from tests.support.postgres_tmpfs import (
    POSTGRES_PRIVATE_TMPFS,
    assert_private_tmpfs_provenance,
    source_metadata_snapshot,
)

REPOSITORY = Path(__file__).resolve().parents[2]
CONTAINER_COMMAND = container_command()
APPLICATION_SERVICES = ("gateway", "migrate", "web", "operations", "indexer", "media")
BACKEND_SERVICES = ("migrate", "web", "operations", "indexer", "media")
WORKER_SERVICES = ("operations", "indexer", "media")
POSTGRES_SECRET_SOURCES = (
    "postgres-superuser-password",
    "db-migrator-password",
    "db-web-password",
    "db-operations-password",
    "db-indexer-password",
    "db-media-password",
)
POSTGRES_BASE_IMAGE = (
    "docker.io/library/postgres:18.6-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2"
)
PROJECT_INVENTORIES: dict[str, ProjectInventory] = {}
# Installed only in the test's overridden bootstrap entrypoint, after sourcing
# the unchanged helper. It never surrounds staging or the normal entrypoint.
# The ERR handler only reports; errexit then exits with the failing command's
# own status. It must not `return`: bash 5.2 prints "pop_var_context: head of
# shell_variables not a function context" for a return from an ERR-trap handler.
POSTGRES_BOOTSTRAP_DIAGNOSTIC = r"""
aegis_pg_refuse() {
    local frame target_id=unavailable
    printf '%s\n' 'database secret staging refused' >&2
    case "${target-}" in
        /|/var|/run|/var/run|/run/aegis-source-secrets|/run/secrets|/run/postgresql|/tmp|final|\
        postgres_superuser_password|db_migrator_password|db_web_password|\
        db_operations_password|db_indexer_password|db_media_password|\
        /run/aegis-source-secrets/postgres_superuser_password|\
        /run/aegis-source-secrets/db_migrator_password|/run/aegis-source-secrets/db_web_password|\
        /run/aegis-source-secrets/db_operations_password|/run/aegis-source-secrets/db_indexer_password|\
        /run/aegis-source-secrets/db_media_password) target_id=$target ;;
    esac
    printf 'bootstrap-refusal target=%.64s\n' "$target_id" >&2
    for ((frame=1; frame<${#FUNCNAME[@]} && frame<=6; frame++)); do
        printf 'bootstrap-refusal frame=%d function=%.64s line=%.10s\n' \
            "$frame" "${FUNCNAME[frame]}" "${BASH_LINENO[frame-1]}" >&2
    done
    exit 1
}
aegis_pg_bootstrap_error() {
    local status=$1
    printf 'bootstrap-error line=%.10s status=%.3s\n' "$2" "$status" >&2
}
trap 'aegis_pg_bootstrap_error "$?" "$LINENO"' ERR
"""
# The complete overridden bootstrap program and its literal process arguments.
POSTGRES_BOOTSTRAP_PROGRAM = (
    "set -Eeuo pipefail; "
    "source /usr/local/libexec/aegis-postgres-private-tmpfs.sh; "
    + POSTGRES_BOOTSTRAP_DIAGNOSTIC
    + "\naegis_prepare_postgres_tmpfs; "
    "stat -c '%u:%g:%a:%n' /run/aegis-source-secrets /run/secrets "
    "/run/postgresql /tmp"
)
POSTGRES_BOOTSTRAP_ENTRYPOINT = ("bash", "-c", POSTGRES_BOOTSTRAP_PROGRAM)


def compose_encoded(arguments: tuple[str, ...]) -> list[str]:
    """Encode every literal dollar exactly once, so Compose interpolates nothing."""
    return [argument.replace("$", "$$") for argument in arguments]


# Written to the override: Compose renders this form; the process receives the literal.
POSTGRES_BOOTSTRAP_COMPOSE_ENTRYPOINT = compose_encoded(POSTGRES_BOOTSTRAP_ENTRYPOINT)


def expected_security_options() -> list[str]:
    return ["no-new-privileges:true", *([MASK_OPTION] if selected_engine() == "podman" else [])]


def _boundary_up_rules(arguments: tuple[str, ...]) -> tuple[ProjectResourceRule, ...]:
    services = tuple(
        name for name in ("postgres", *APPLICATION_SERVICES) if name in arguments
    )
    return (
        *(ProjectResourceRule("container", (
            ("com.docker.compose.service", service),
            ("com.docker.compose.oneoff", "False"),
        )) for service in services),
        ProjectResourceRule("network", (("com.docker.compose.network", "backend"),)),
        *(ProjectResourceRule("volume", (("com.docker.compose.volume", volume),))
          for volume in (
              "indexer-coordination", "postgres-data", "staging", "derivatives",
              "model-cache", "quarantine", "frontier-outbox",
          )),
    )


def rendered_compose() -> dict[str, Any]:
    result = run_container(
        [
            *CONTAINER_COMMAND,
            "compose",
            "--project-directory",
            str(REPOSITORY),
            "-f",
            str(REPOSITORY / "compose.yaml"),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=compose_environment(
            os.environ | {"AEGIS_RELEASE_ID": "container-boundary-test"}
        ),
    )
    return cast(dict[str, Any], json.loads(result.stdout))


def secret_sources(service: dict[str, Any]) -> set[str]:
    return {secret["source"] for secret in service.get("secrets", [])}


def volume_sources(service: dict[str, Any]) -> set[str]:
    return {mount.get("source", "") for mount in service.get("volumes", [])}


def test_application_services_have_fail_closed_container_isolation() -> None:
    config = rendered_compose()
    services = config["services"]

    for name in APPLICATION_SERVICES:
        service = services[name]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == expected_security_options()
        assert service["user"] not in ("", "0", "0:0", "root")
        assert service.get("privileged", False) is False
        assert "pid" not in service
        assert "ipc" not in service
        assert "network_mode" not in service
        assert service.get("tmpfs")
        for mount in service["tmpfs"]:
            assert "size=" in mount
            assert "noexec" in mount
            assert "nosuid" in mount
            assert "nodev" in mount
        for mount in service.get("volumes", []):
            assert "docker.sock" not in mount.get("source", "")
            assert "docker.sock" not in mount["target"]

    assert config["networks"]["backend"]["internal"] is True
    for name in BACKEND_SERVICES:
        assert set(services[name]["networks"]) == {"backend"}


def test_gateway_has_only_delivery_state_and_no_application_credentials() -> None:
    gateway = rendered_compose()["services"]["gateway"]

    assert "secrets" not in gateway
    assert volume_sources(gateway) == {"derivatives"}
    derivative = next(
        mount for mount in gateway["volumes"] if mount["target"] == "/srv/aegis/derivatives"
    )
    assert derivative["read_only"] is True
    forbidden_fragments = (
        "DB_",
        "DATABASE",
        "DJANGO",
        "PASSWORD",
        "SECRET",
        "WORKER",
    )
    assert not any(
        fragment in key
        for key in gateway.get("environment", {})
        for fragment in forbidden_fragments
    )


def test_database_credentials_commands_and_volumes_are_role_scoped() -> None:
    services = rendered_compose()["services"]

    expected_secrets = {
        "migrate": {"django-secret-key", "db-migrator-password"},
        "web": {
            "django-secret-key",
            "db-web-password",
            "auth-throttle-hmac-key",
        },
        "operations": {"django-secret-key", "db-operations-password"},
        "indexer": {"django-secret-key", "db-indexer-password"},
        "media": {"django-secret-key", "db-media-password"},
    }
    expected_volumes = {
        "migrate": set(),
        "web": {"staging"},
        "operations": {"staging"},
        "indexer": {"indexer-coordination"},
        "media": {"derivatives", "quarantine"},
    }
    expected_users = {
        "migrate": "aegis_migrator",
        "web": "aegis_web",
        "operations": "aegis_operations",
        "indexer": "aegis_indexer",
        "media": "aegis_media",
    }

    assert services["migrate"]["command"] == ["python", "manage.py", "deploy_database"]
    for role in WORKER_SERVICES:
        assert services[role]["command"] == [
            "python",
            "manage.py",
            "run_role",
            "--role",
            role,
        ]
        assert services[role]["environment"]["AEGIS_PROCESS_ROLE"] == role

    for name in BACKEND_SERVICES:
        service = services[name]
        assert service["environment"]["AEGIS_DB_USER"] == expected_users[name]
        assert secret_sources(service) == expected_secrets[name]
        assert volume_sources(service) == expected_volumes[name]
        for other_name in BACKEND_SERVICES:
            if other_name == name:
                continue
            assert expected_users[other_name] not in service["environment"].values()


def test_backend_healthchecks_verify_the_exact_runtime_boundary() -> None:
    services = rendered_compose()["services"]

    assert services["web"]["healthcheck"]["test"] == [
        "CMD",
        "python",
        "manage.py",
        "check_runtime",
        "--role",
        "web",
        "--require-http",
    ]
    for role in WORKER_SERVICES:
        assert services[role]["healthcheck"]["test"] == [
            "CMD",
            "python",
            "manage.py",
            "check_runtime",
            "--role",
            role,
        ]
    for name in ("web", *WORKER_SERVICES):
        healthcheck = services[name]["healthcheck"]
        assert healthcheck["interval"] == "5s"
        assert healthcheck["timeout"] == "5s"
        assert healthcheck["retries"] == 20
        assert healthcheck["start_period"] == "10s"


def test_postgres_stages_fixed_source_secrets_into_uid_70_private_tmpfs() -> None:
    postgres = rendered_compose()["services"]["postgres"]

    assert postgres["image"] == "aegis-postgres"
    assert postgres["build"] == {
        "context": str(REPOSITORY),
        "dockerfile": "docker/postgres.Dockerfile",
    }
    assert postgres["entrypoint"] == ["/usr/local/bin/aegis-postgres-entrypoint"]
    assert postgres["command"] == ["postgres"]
    assert postgres["user"] == "0:0"
    assert postgres["read_only"] is True
    assert postgres["security_opt"] == expected_security_options()
    assert postgres["environment"]["POSTGRES_PASSWORD_FILE"] == (
        "/run/secrets/postgres_superuser_password"
    )

    targets = {secret["source"]: secret["target"] for secret in postgres["secrets"]}
    assert set(targets) == set(POSTGRES_SECRET_SOURCES)
    assert targets == {
        source: f"/run/aegis-source-secrets/{source.replace('-', '_')}"
        for source in POSTGRES_SECRET_SOURCES
    }

    tmpfs = set(postgres["tmpfs"])
    assert tmpfs == {
        "/run/aegis-source-secrets:size=64k,noexec,nosuid,nodev,mode=0700",
        "/run/secrets:size=64k,noexec,nosuid,nodev,mode=0700",
        "/run/postgresql:size=16m,noexec,nosuid,nodev,mode=0775",
        "/tmp:size=16m,noexec,nosuid,nodev,mode=1777",
    }
    for mount in tmpfs:
        assert "size=" in mount
        assert "noexec" in mount
        assert "nosuid" in mount
        assert "nodev" in mount


def test_postgres_staging_wrapper_never_reads_secret_values_into_shell_state() -> None:
    wrapper = REPOSITORY / "deploy" / "postgres" / "entrypoint.sh"
    source = wrapper.read_text(encoding="utf-8")

    syntax = run_container(
        ["bash", "-n", str(wrapper)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0
    assert "set -x" not in source
    assert "`" not in source
    assert "PGPASSWORD=" not in source
    assert "POSTGRES_PASSWORD=" not in source
    assert "read " not in source
    assert "cat " not in source
    assert 'cp "$source_path" "$staged_path"' in source
    assert 'chown 70:70 "$staged_path"' in source
    assert 'chmod 0400 "$staged_path"' in source
    assert "--aegis-reconcile-existing-database" in source
    assert "source /usr/local/bin/docker-entrypoint.sh" in source
    assert 'exec gosu postgres "$0"' in source
    assert 'exec /usr/local/bin/docker-entrypoint.sh "$@"' in source
    for name in POSTGRES_SECRET_SOURCES:
        assert name.replace("-", "_") in source


def test_selected_provider_renders_bootstrap_program_without_interpolation(
    tmp_path: Path,
) -> None:
    """Resource-free: only the provider's config path; no engine, API, or socket."""
    project = tmp_path / "compose.bootstrap-render.json"
    project.write_text(json.dumps({"services": {"postgres": {
        "image": "aegis-bootstrap-render-only:unused",
        "entrypoint": POSTGRES_BOOTSTRAP_COMPOSE_ENTRYPOINT,
        "command": [],
    }}}), encoding="utf-8")
    provider = (
        [str(podman_compose_provider())] if selected_engine() == "podman"
        else ["docker", "compose"]
    )
    # Explicit environment without DOCKER_HOST/CONTAINER_HOST: a host value for
    # every Bash name in the program, none of which may reach the rendered model.
    canaries = {
        name: f"aegis-host-canary-{name}"
        for name in ("target", "target_id", "frame", "status", "FUNCNAME", "BASH_LINENO", "LINENO")
    }
    rendered = run_container(
        [
            *provider, "--env-file", "/dev/null", "--project-directory", str(tmp_path),
            "--project-name", "aegis-bootstrap-render", "-f", str(project),
            "config", "--format", "json",
        ],
        check=False, capture_output=True, text=True, timeout=60,
        env={"PATH": os.environ.get("PATH", os.defpath), **canaries},
    )
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stderr == "", "Compose reported interpolation warnings or errors"
    entrypoint = json.loads(rendered.stdout)["services"]["postgres"]["entrypoint"]
    assert entrypoint == POSTGRES_BOOTSTRAP_COMPOSE_ENTRYPOINT
    assert entrypoint != list(POSTGRES_BOOTSTRAP_ENTRYPOINT)
    assert not any(value in rendered.stdout for value in canaries.values())


def test_web_and_migrate_have_no_original_mount_and_every_root_consumer_is_read_only() -> None:
    services = rendered_compose()["services"]

    for name in ("web", "migrate"):
        assert all(
            not mount["target"].startswith("/srv/aegis/roots/")
            for mount in services[name].get("volumes", [])
        )
    for name in ("gateway", *WORKER_SERVICES):
        for mount in services[name].get("volumes", []):
            if mount["target"].startswith("/srv/aegis/roots/"):
                assert mount["read_only"] is True


def docker_compose(
    project: str,
    override: Path,
    *arguments: str,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    if project not in PROJECT_INVENTORIES:
        PROJECT_INVENTORIES[project] = require_empty_project(project)
    inventory = PROJECT_INVENTORIES[project]
    require_project_inventory(inventory)
    if arguments and arguments[0] == "down":
        kinds = {"container", "network"}
        if "--volumes" in arguments:
            kinds.add("volume")
        PROJECT_INVENTORIES[project] = cleanup_project_resource_kinds(
            inventory, frozenset(kinds),
        )
        return subprocess.CompletedProcess(arguments, 0, "", "")
    mutation = bool(arguments) and arguments[0] in {"up", "create"}
    try:
        result = run_container(
            [
                *CONTAINER_COMMAND,
                "compose",
                "--project-name",
                project,
                "--project-directory",
                str(REPOSITORY),
                "-f",
                str(REPOSITORY / "compose.yaml"),
                "-f",
                str(override),
                *arguments,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=compose_environment(
                os.environ | {"AEGIS_RELEASE_ID": "container-runtime-test"}
            ),
        )
    finally:
        if mutation:
            observed = capture_project_inventory(project)
            PROJECT_INVENTORIES[project] = admit_project_transition(
                inventory, observed, _boundary_up_rules(arguments),
            )
    return result


def test_docker_compose_failed_up_refuses_unknown_transition_and_retains_inventory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    project = "bounded-transition"
    before = ProjectInventory(project, ())
    unknown = ProjectResource(
        "container", "unknown", "unknown",
        json.dumps({
            "Id": "unknown", "Created": "now", "Name": "unknown", "Image": "image",
            "Labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": "unknown",
                "com.docker.compose.oneoff": "False",
            },
        }, sort_keys=True, separators=(",", ":")),
    )
    PROJECT_INVENTORIES[project] = before
    monkeypatch.setattr(f"{__name__}.require_project_inventory", lambda inventory: None)
    monkeypatch.setattr(
        f"{__name__}.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 125, "", "failed"),
    )
    monkeypatch.setattr(
        f"{__name__}.capture_project_inventory",
        lambda name: ProjectInventory(name, (unknown,)),
    )
    try:
        with pytest.raises(RuntimeError, match="unexpected"):
            docker_compose(project, tmp_path / "override.yaml", "up", "postgres")
        assert PROJECT_INVENTORIES[project] == before
    finally:
        PROJECT_INVENTORIES.pop(project, None)


def protected_volume_created_at() -> str | None:
    result = run_container(
        [
            *CONTAINER_COMMAND,
            "volume",
            "inspect",
            "aegis_postgres-data",
            "--format",
            "{{.CreatedAt}}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def wait_for_postgres_health(
    project: str,
    container_id: str,
    *,
    timeout: float = 90,
) -> str:
    inventory = PROJECT_INVENTORIES[project]
    require_project_inventory(inventory)
    assert any(
        resource.kind == "container" and resource.immutable_id == container_id
        for resource in inventory.resources
    ), "PostgreSQL health target was not recorded at creation"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        health = run_container(
            [
                *CONTAINER_COMMAND,
                "inspect",
                container_id,
                "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{end}}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if health.stdout.strip() == "healthy":
            return container_id
        if health.stdout.strip() == "unhealthy":
            pytest.fail("disposable PostgreSQL became unhealthy", pytrace=False)
        time.sleep(0.25)
    pytest.fail("disposable PostgreSQL did not become healthy", pytrace=False)


def run_database_probe(
    *,
    project: str,
    scratch: Path,
    label: str,
    role: str,
    password: str,
    statement: str,
    tree: FreshTestTree,
) -> subprocess.CompletedProcess[str]:
    escaped_password = password.replace("\\", "\\\\").replace(":", "\\:")
    pgpass = scratch / f"{label}.pgpass"
    pgpass.write_text(
        f"postgres:5432:aegis:{role}:{escaped_password}\n",
        encoding="utf-8",
    )
    pgpass.chmod(0o600)
    record_created_test_path(tree, pgpass)
    prepare_owned_test_inventory(record_test_tree_inventory(tree))
    return run_container(
        [
            *CONTAINER_COMMAND,
            "run",
            "--rm",
            "--network",
            f"{project}_backend",
            "--mount",
            f"type=bind,src={pgpass},dst=/run/probe.pgpass,readonly",
            "--env",
            "PGPASSFILE=/run/probe.pgpass",
            POSTGRES_BASE_IMAGE,
            "psql",
            "--no-psqlrc",
            "--no-password",
            "--host",
            "postgres",
            "--dbname",
            "aegis",
            "--username",
            role,
            "--tuples-only",
            "--no-align",
            "--command",
            statement,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def inspect_postgres_private_tmpfs(container_id: str) -> None:
    inspected = run_container(
        [*CONTAINER_COMMAND, "container", "inspect", container_id],
        check=True, capture_output=True, text=True,
    )
    payload = json.loads(inspected.stdout)
    assert isinstance(payload, list) and len(payload) == 1
    assert_private_tmpfs_provenance(payload[0], POSTGRES_PRIVATE_TMPFS)


def _postgres_image_identity(value: str) -> str:
    identity = value.removeprefix("sha256:")
    assert re.fullmatch(r"[0-9a-f]{64}", identity), "PostgreSQL image identity unavailable"
    return identity


def build_postgres_image(project: str, override: Path) -> str:
    """Record the built image before any canonical preparation container exists."""
    built = docker_compose(project, override, "build", "postgres")
    assert built.returncode == 0, "disposable PostgreSQL image build failed"
    inspected = run_container(
        [*CONTAINER_COMMAND, "image", "inspect", "aegis-postgres"],
        check=True, capture_output=True, text=True,
    )
    payload = json.loads(inspected.stdout)
    assert isinstance(payload, list) and len(payload) == 1
    return _postgres_image_identity(payload[0]["Id"])


def start_postgres_with_private_tmpfs(
    project: str, override: Path, image: str,
    *, process_entrypoint: tuple[str, ...] | None = None,
) -> str:
    """Admit an unstarted exact container; prove private targets before root bootstrap.

    Compose ps need not list created/stopped containers. Only the immutable
    identity recorded by the bounded create transition can become a start target.
    The same project inventory must survive start; it cannot admit new resources.
    A literal process entrypoint (the bootstrap) must render as its once-encoded
    Compose form and be inspected unchanged. Otherwise, the inspected entrypoint
    must equal the rendered one.
    """
    rendered = docker_compose(project, override, "config", "--format", "json")
    assert rendered.returncode == 0
    config = json.loads(rendered.stdout)
    service = config["services"]["postgres"]
    assert service["image"] == "aegis-postgres"
    assert service["user"] == "0:0" and service["read_only"] is True
    assert not any(
        resource.kind == "container" for resource in PROJECT_INVENTORIES[project].resources
    ), "PostgreSQL creation requires the previous owned container to be removed"
    expected_sources = {
        f"/run/aegis-source-secrets/{name.replace('-', '_')}": config["secrets"][name]["file"]
        for name in POSTGRES_SECRET_SOURCES
    }
    assert len(service["secrets"]) == len(expected_sources)
    assert {
        item["target"]: config["secrets"][item["source"]]["file"]
        for item in service["secrets"]
    } == expected_sources, "canonical PostgreSQL source configuration changed"

    # The finally in docker_compose records permitted partial creates too. A
    # failure never proceeds to start, but known owned resources remain cleanable.
    created = docker_compose(
        project, override, "create", "--no-build", "--pull", "never", "postgres",
    )
    assert created.returncode == 0, "disposable PostgreSQL creation failed"
    inventory = PROJECT_INVENTORIES[project]
    containers = [resource for resource in inventory.resources if resource.kind == "container"]
    assert len(containers) == 1, "PostgreSQL creation identity is missing or ambiguous"
    recorded = containers[0]
    assert re.fullmatch(r"[0-9a-f]{64}", recorded.immutable_id)
    require_project_inventory(inventory)
    inspected = run_container(
        [*CONTAINER_COMMAND, "container", "inspect", recorded.immutable_id],
        check=True, capture_output=True, text=True,
    )
    payload = json.loads(inspected.stdout)
    assert isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict)
    info = payload[0]
    fingerprint = {key: info.get(key) for key in ("Id", "Created", "Name", "Image")}
    fingerprint["Labels"] = info.get("Config", {}).get("Labels") or {}
    assert fingerprint == json.loads(recorded.fingerprint), "PostgreSQL creation identity changed"
    assert all(fingerprint["Labels"].get(key) == value for key, value in {
        "com.docker.compose.project": project,
        "com.docker.compose.service": "postgres",
        "com.docker.compose.oneoff": "False",
    }.items()), "PostgreSQL creation ownership changed"
    assert _postgres_image_identity(info["Image"]) == _postgres_image_identity(image), (
        "PostgreSQL image changed after build"
    )
    actual = info["Config"]
    assert actual.get("User") == service["user"], "PostgreSQL bootstrap user changed"
    if process_entrypoint is None:
        assert actual.get("Entrypoint") == service["entrypoint"], "PostgreSQL entrypoint changed"
    else:
        assert service["entrypoint"] == compose_encoded(process_entrypoint), (
            "PostgreSQL Compose entrypoint changed"
        )
        assert actual.get("Entrypoint") == list(process_entrypoint), "PostgreSQL entrypoint changed"
    assert (actual.get("Cmd") or []) == (service["command"] or []), "PostgreSQL command changed"
    assert info["HostConfig"].get("ReadonlyRootfs") is True
    assert info["HostConfig"].get("Privileged") is False
    assert_private_tmpfs_provenance(info, POSTGRES_PRIVATE_TMPFS)
    assert {
        mount["Destination"]: mount.get("Source")
        for mount in info["Mounts"] if mount["Destination"] in expected_sources
    } == expected_sources, "PostgreSQL source mount was substituted"
    require_project_inventory(inventory)
    try:
        started = run_container(
            [*CONTAINER_COMMAND, "start", recorded.immutable_id],
            check=True, capture_output=True, text=True, timeout=60,
        )
        assert started.returncode == 0
    finally:
        require_project_inventory(inventory)
    return recorded.immutable_id


@pytest.mark.integration
def test_live_postgres_stages_secrets_and_drops_to_uid_70(tmp_path: Path) -> None:
    tree = record_fresh_test_tree(tmp_path)
    protected_created_at = protected_volume_created_at()
    project = f"aegis-task11-boundary-{uuid.uuid4().hex[:10]}"
    secret_values = {
        "postgres-superuser-password": f"super-'\\$`; Ω {secrets.token_urlsafe(24)}",
        "db-migrator-password": f"migrator-'\\$`; Ω {secrets.token_urlsafe(24)}",
        "db-web-password": f"web-'\\$`; Ω {secrets.token_urlsafe(24)}",
        "db-operations-password": f"operations-'\\$`; Ω {secrets.token_urlsafe(24)}",
        "db-indexer-password": f"indexer-'\\$`; Ω {secrets.token_urlsafe(24)}",
        "db-media-password": f"media-'\\$`; Ω {secrets.token_urlsafe(24)}",
        "django-secret-key": secrets.token_urlsafe(48),
        "auth-throttle-hmac-key": secrets.token_urlsafe(48),
    }
    secret_files: dict[str, str] = {}
    for name, value in secret_values.items():
        path = tmp_path / name
        path.write_text(value + "\n", encoding="utf-8")
        path.chmod(0o600)
        record_created_test_path(tree, path)
        secret_files[name] = str(path)

    override = tmp_path / "compose.runtime.json"
    override.write_text(
        json.dumps(
            {
                "services": {
                    "postgres": {
                        "volumes": [
                            {
                                "type": "tmpfs",
                                "target": "/var/lib/postgresql",
                                # Explicit mode: Podman applies Compose's default mode=0
                                # literally, unlike Docker's documented 1777 default.
                                "tmpfs": {"size": 268_435_456, "mode": 0o1777},
                            }
                        ],
                    }
                },
                "secrets": {name: {"file": path} for name, path in secret_files.items()},
            }
        ),
        encoding="utf-8",
    )
    record_created_test_path(tree, override)
    bootstrap_override = tmp_path / "compose.bootstrap.json"
    bootstrap_config = json.loads(override.read_text(encoding="utf-8"))
    bootstrap_config["services"]["postgres"].update({
        "entrypoint": POSTGRES_BOOTSTRAP_COMPOSE_ENTRYPOINT,
        "command": [], "restart": "no", "healthcheck": {"disable": True},
    })
    bootstrap_override.write_text(json.dumps(bootstrap_config), encoding="utf-8")
    record_created_test_path(tree, bootstrap_override)
    prepare_owned_test_inventory(record_test_tree_inventory(tree))
    inputs = [Path(path) for path in secret_files.values()]
    source_snapshot = source_metadata_snapshot(inputs)

    container_id = ""
    try:
        rendered_result = docker_compose(project, override, "config", "--format", "json")
        assert rendered_result.returncode == 0
        rendered = json.loads(rendered_result.stdout)
        data_mount = next(
            mount
            for mount in rendered["services"]["postgres"]["volumes"]
            if mount["target"] == "/var/lib/postgresql"
        )
        assert data_mount["type"] == "tmpfs"
        assert data_mount.get("source") != "postgres-data"
        # Compose renders tmpfs mode as an integer (verified via the actual
        # provider's `config --format json` on a scratch project: mode=0o1777
        # comes back as 1023, not an octal string). The real provider renders
        # size as a string while the fake double preserves the Python int
        # given in the override, so compare it numerically.
        assert data_mount["tmpfs"]["mode"] == 0o1777
        assert int(data_mount["tmpfs"]["size"]) == 268_435_456

        # Same canonical mounts/image, with an owned diagnostic command that
        # stops before staging or the official entrypoint. This measures the
        # bootstrap 0775 socket mode separately from the live upstream 03775.
        image = build_postgres_image(project, override)
        bootstrap_id = start_postgres_with_private_tmpfs(
            project, bootstrap_override, image, process_entrypoint=POSTGRES_BOOTSTRAP_ENTRYPOINT,
        )
        waited = run_container(
            [*CONTAINER_COMMAND, "wait", bootstrap_id],
            check=True, capture_output=True, text=True, timeout=30,
        )
        # Capture the metadata-only diagnostic before any exit assertion and
        # before the outer finally removes this exact owned bootstrap container.
        bootstrap_metadata = run_container(
            [*CONTAINER_COMMAND, "logs", "--tail", "32", bootstrap_id],
            check=False, capture_output=True, text=True, timeout=30,
        )
        diagnostic = bootstrap_metadata.stdout + bootstrap_metadata.stderr
        if len(diagnostic.encode("utf-8")) > 8192:
            # An assertion expression would let pytest expand the rejected log.
            raise AssertionError("bootstrap diagnostic logs exceeded 8192 bytes")
        assert bootstrap_metadata.returncode == 0, "bootstrap diagnostic log collection failed"
        assert waited.stdout.strip() == "0", f"PostgreSQL bootstrap failed:\n{diagnostic}"
        inspect_postgres_private_tmpfs(bootstrap_id)
        assert bootstrap_metadata.stdout.splitlines() == [
            "0:0:700:/run/aegis-source-secrets", "70:70:700:/run/secrets",
            "70:70:775:/run/postgresql", "70:70:1777:/tmp",
        ]
        stopped_probe = docker_compose(project, bootstrap_override, "down", "--remove-orphans")
        assert stopped_probe.returncode == 0
        assert source_metadata_snapshot(inputs) == source_snapshot

        container_id = start_postgres_with_private_tmpfs(project, override, image)
        wait_for_postgres_health(project, container_id)

        mounts = run_container(
            [*CONTAINER_COMMAND, "inspect", container_id, "--format", "{{json .Mounts}}"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert all(
            mount.get("Name") != "aegis_postgres-data" for mount in json.loads(mounts.stdout)
        )
        assert all(mount.get("Type") != "volume" for mount in json.loads(mounts.stdout))
        inspect_postgres_private_tmpfs(container_id)
        directory_metadata = run_container(
            [*CONTAINER_COMMAND, "exec", container_id, "stat", "-c", "%u:%g:%a:%n",
             "/run/aegis-source-secrets", "/run/secrets", "/run/postgresql", "/tmp"],
            check=True, capture_output=True, text=True,
        )
        assert directory_metadata.stdout.splitlines() == [
            "0:0:700:/run/aegis-source-secrets", "70:70:700:/run/secrets",
            "70:70:3775:/run/postgresql", "70:70:1777:/tmp",
        ]

        process_status = run_container(
            [
                *CONTAINER_COMMAND,
                "exec",
                container_id,
                "sh",
                "-c",
                "sed -n '/^Uid:/p;/^Gid:/p;/^CapEff:/p' /proc/1/status",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert process_status.stdout.splitlines() == [
            "Uid:\t70\t70\t70\t70",
            "Gid:\t70\t70\t70\t70",
            "CapEff:\t0000000000000000",
        ]

        for name in POSTGRES_SECRET_SOURCES:
            staged_name = name.replace("-", "_")
            metadata = run_container(
                [
                    *CONTAINER_COMMAND,
                    "exec",
                    "--user",
                    "70:70",
                    container_id,
                    "stat",
                    "-c",
                    "%u:%g:%a:%s",
                    f"/run/secrets/{staged_name}",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            owner, group, mode, size = metadata.stdout.strip().split(":")
            assert (owner, group, mode) == ("70", "70", "400")
            assert 1 <= int(size) <= 4096

        source_access = run_container(
            [
                *CONTAINER_COMMAND,
                "exec",
                "--user",
                "70:70",
                container_id,
                "sh",
                "-c",
                "test -r /run/aegis-source-secrets/db_web_password",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert source_access.returncode != 0

        role_passwords = {
            "aegis_migrator": secret_values["db-migrator-password"],
            "aegis_web": secret_values["db-web-password"],
            "aegis_operations": secret_values["db-operations-password"],
            "aegis_indexer": secret_values["db-indexer-password"],
            "aegis_media": secret_values["db-media-password"],
        }
        for role, password in role_passwords.items():
            pgpass = tmp_path / f"{role}.pgpass"
            escaped_password = password.replace("\\", "\\\\").replace(":", "\\:")
            pgpass.write_text(
                f"postgres:5432:aegis:{role}:{escaped_password}\n",
                encoding="utf-8",
            )
            pgpass.chmod(0o600)
            record_created_test_path(tree, pgpass)
            prepare_owned_test_inventory(record_test_tree_inventory(tree))
            authentication = run_container(
                [
                    *CONTAINER_COMMAND,
                    "run",
                    "--rm",
                    "--network",
                    f"{project}_backend",
                    "--mount",
                    f"type=bind,src={pgpass},dst=/run/probe.pgpass,readonly",
                    "--env",
                    "PGPASSFILE=/run/probe.pgpass",
                    POSTGRES_BASE_IMAGE,
                    "psql",
                    "--no-psqlrc",
                    "--no-password",
                    "--host",
                    "postgres",
                    "--dbname",
                    "aegis",
                    "--username",
                    role,
                    "--tuples-only",
                    "--no-align",
                    "--command",
                    "SELECT session_user || '|' || current_user",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            if authentication.returncode != 0:
                pytest.fail(
                    f"database role authentication failed for {role}",
                    pytrace=False,
                )
            assert authentication.stdout.strip() == f"{role}|{role}"

        logs = run_container(
            [*CONTAINER_COMMAND, "logs", container_id],
            check=True,
            capture_output=True,
            text=True,
        )
        if any(value in logs.stdout + logs.stderr for value in secret_values.values()):
            pytest.fail("a staged secret appeared in PostgreSQL logs", pytrace=False)
    finally:
        stopped = docker_compose(
            project,
            override,
            "down",
            "--remove-orphans",
            "--volumes",
            timeout=60,
        )
        assert stopped.returncode == 0
        assert source_metadata_snapshot(inputs) == source_snapshot
        if container_id:
            assert (
                run_container(
                    [*CONTAINER_COMMAND, "inspect", container_id],
                    check=False,
                    capture_output=True,
                    text=True,
                ).returncode
                != 0
            )
        assert protected_volume_created_at() == protected_created_at


@pytest.mark.integration
def test_postgres_reconciles_populated_accepted_base_and_rotated_secrets(
    tmp_path: Path,
) -> None:
    tree = record_fresh_test_tree(tmp_path)
    protected_created_at = protected_volume_created_at()
    project = f"aegis-task11-upgrade-{uuid.uuid4().hex[:10]}"
    initial_values = {
        "postgres-superuser-password": f"initial-super-{secrets.token_urlsafe(24)}",
        "db-migrator-password": f"initial-migrator-{secrets.token_urlsafe(24)}",
        "db-web-password": f"initial-web-{secrets.token_urlsafe(24)}",
        "db-operations-password": f"initial-operations-{secrets.token_urlsafe(24)}",
        "db-indexer-password": f"initial-indexer-{secrets.token_urlsafe(24)}",
        "db-media-password": f"initial-media-{secrets.token_urlsafe(24)}",
        "django-secret-key": secrets.token_urlsafe(48),
        "auth-throttle-hmac-key": secrets.token_urlsafe(48),
    }
    rotated_values = {
        **initial_values,
        "db-migrator-password": f"rotated-migrator-{secrets.token_urlsafe(24)}",
        "db-web-password": f"rotated-web-{secrets.token_urlsafe(24)}",
        "db-operations-password": f"rotated-operations-{secrets.token_urlsafe(24)}",
        "db-indexer-password": f"rotated-indexer-{secrets.token_urlsafe(24)}",
        "db-media-password": f"rotated-media-{secrets.token_urlsafe(24)}",
    }
    secret_files: dict[str, Path] = {}
    for name, value in initial_values.items():
        path = tmp_path / name
        path.write_text(value + "\n", encoding="utf-8")
        path.chmod(0o600)
        record_created_test_path(tree, path)
        secret_files[name] = path

    override = tmp_path / "compose.upgrade.json"
    override.write_text(
        json.dumps(
            {
                "secrets": {
                    name: {"file": str(path)} for name, path in secret_files.items()
                }
            }
        ),
        encoding="utf-8",
    )
    record_created_test_path(tree, override)
    prepare_owned_test_inventory(record_test_tree_inventory(tree))
    all_sensitive_values = tuple(initial_values.values()) + tuple(rotated_values.values())
    inputs = list(secret_files.values())
    source_snapshot = source_metadata_snapshot(inputs)
    container_ids: list[str] = []
    try:
        image = build_postgres_image(project, override)
        container_ids.append(start_postgres_with_private_tmpfs(project, override, image))
        wait_for_postgres_health(project, container_ids[-1])
        inspect_postgres_private_tmpfs(container_ids[-1])

        accepted_base_state = docker_compose(
            project,
            override,
            "exec",
            "-T",
            "--user",
            "70:70",
            "postgres",
            "psql",
            "--no-psqlrc",
            "--no-password",
            "--username",
            "postgres",
            "--dbname",
            "aegis",
            "--set",
            "ON_ERROR_STOP=1",
            "--command",
            "SET ROLE aegis_migrator; "
            "CREATE TABLE public.aegis_accepted_base_probe "
            "(id integer PRIMARY KEY, marker text NOT NULL); "
            "INSERT INTO public.aegis_accepted_base_probe VALUES (1, 'preserved'); "
            "ALTER DEFAULT PRIVILEGES FOR ROLE aegis_migrator IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aegis_web; "
            "ALTER DEFAULT PRIVILEGES FOR ROLE aegis_migrator IN SCHEMA public "
            "GRANT USAGE, SELECT ON SEQUENCES TO aegis_web; "
            "RESET ROLE; "
            "ALTER ROLE aegis_migrator INHERIT; "
            "ALTER ROLE aegis_web INHERIT; "
            "ALTER ROLE aegis_operations INHERIT; "
            "ALTER ROLE aegis_indexer INHERIT; "
            "ALTER ROLE aegis_media INHERIT; "
            "ALTER SCHEMA public OWNER TO pg_database_owner;",
        )
        assert accepted_base_state.returncode == 0, accepted_base_state.stderr

        stopped = docker_compose(project, override, "down", "--remove-orphans")
        assert stopped.returncode == 0
        assert source_metadata_snapshot(inputs) == source_snapshot

        for name, value in rotated_values.items():
            secret_files[name].write_text(value + "\n", encoding="utf-8")
            secret_files[name].chmod(0o600)
        secret_files["db-web-password"].write_text("", encoding="utf-8")
        source_snapshot = source_metadata_snapshot(inputs)

        invalid_container_id = start_postgres_with_private_tmpfs(project, override, image)
        container_ids.append(invalid_container_id)
        time.sleep(1)
        invalid_logs = run_container(
            [*CONTAINER_COMMAND, "logs", invalid_container_id],
            check=True, capture_output=True, text=True,
        )
        assert "database secret staging refused" in (invalid_logs.stdout + invalid_logs.stderr)
        assert not any(
            value in invalid_logs.stdout + invalid_logs.stderr for value in all_sensitive_values
        )
        stopped = docker_compose(project, override, "down", "--remove-orphans")
        assert stopped.returncode == 0
        assert source_metadata_snapshot(inputs) == source_snapshot

        secret_files["db-web-password"].write_text(
            rotated_values["db-web-password"] + "\n",
            encoding="utf-8",
        )
        secret_files["db-web-password"].chmod(0o600)
        source_snapshot = source_metadata_snapshot(inputs)
        container_ids.append(start_postgres_with_private_tmpfs(project, override, image))
        wait_for_postgres_health(project, container_ids[-1])
        inspect_postgres_private_tmpfs(container_ids[-1])

        roles = ("migrator", "web", "operations", "indexer", "media")
        for role in roles:
            database_role = f"aegis_{role}"
            old_authentication = run_database_probe(
                project=project,
                scratch=tmp_path,
                label=f"old-{role}",
                role=database_role,
                password=initial_values[f"db-{role}-password"],
                statement="SELECT session_user",
                tree=tree,
            )
            assert old_authentication.returncode != 0
            new_authentication = run_database_probe(
                project=project,
                scratch=tmp_path,
                label=f"new-{role}",
                role=database_role,
                password=rotated_values[f"db-{role}-password"],
                statement="SELECT session_user || '|' || current_user",
                tree=tree,
            )
            assert new_authentication.returncode == 0
            assert new_authentication.stdout.strip() == f"{database_role}|{database_role}"

        preserved = run_database_probe(
            project=project,
            scratch=tmp_path,
            label="preserved-row",
            role="aegis_migrator",
            password=rotated_values["db-migrator-password"],
            statement=(
                "SELECT marker FROM public.aegis_accepted_base_probe WHERE id = 1; "
                "SELECT pg_get_userbyid(nspowner) FROM pg_namespace "
                "WHERE nspname = 'public'; "
                "SELECT count(*) FROM pg_roles WHERE rolname LIKE 'aegis_%' "
                "AND (rolinherit OR rolsuper OR rolcreatedb OR rolcreaterole "
                "OR rolreplication OR rolbypassrls); "
                "SELECT count(*) FROM pg_default_acl AS defaults "
                "CROSS JOIN LATERAL aclexplode(defaults.defaclacl) AS acl "
                "WHERE defaults.defaclrole = "
                "(SELECT oid FROM pg_roles WHERE rolname = 'aegis_migrator') "
                "AND acl.grantee = "
                "(SELECT oid FROM pg_roles WHERE rolname = 'aegis_web');"
            ),
            tree=tree,
        )
        assert preserved.returncode == 0, preserved.stderr
        assert preserved.stdout.splitlines() == ["preserved", "aegis_migrator", "0", "0"]

        for container_id in container_ids:
            logs = run_container(
                [*CONTAINER_COMMAND, "logs", container_id],
                check=False,
                capture_output=True,
                text=True,
            )
            assert not any(
                value in logs.stdout + logs.stderr for value in all_sensitive_values
            )
    finally:
        docker_compose(
            project, override, "down", "--remove-orphans", "--volumes", timeout=60,
        )
        assert source_metadata_snapshot(inputs) == source_snapshot
        assert protected_volume_created_at() == protected_created_at
