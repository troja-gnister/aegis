from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
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
    "postgres:18.6-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2"
)


def rendered_compose() -> dict[str, Any]:
    result = subprocess.run(
        [
            "docker",
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
        env=os.environ | {"AEGIS_RELEASE_ID": "container-boundary-test"},
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
        assert service["security_opt"] == ["no-new-privileges:true"]
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
        "indexer": set(),
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
    assert postgres["security_opt"] == ["no-new-privileges:true"]
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
    assert any(
        mount.startswith("/run/aegis-source-secrets:")
        and "uid=0" in mount
        and "gid=0" in mount
        and "mode=0700" in mount
        for mount in tmpfs
    )
    assert any(
        mount.startswith("/run/secrets:")
        and "uid=70" in mount
        and "gid=70" in mount
        and "mode=0700" in mount
        for mount in tmpfs
    )
    for mount in tmpfs:
        assert "size=" in mount
        assert "noexec" in mount
        assert "nosuid" in mount
        assert "nodev" in mount


def test_postgres_staging_wrapper_never_reads_secret_values_into_shell_state() -> None:
    wrapper = REPOSITORY / "deploy" / "postgres" / "entrypoint.sh"
    source = wrapper.read_text(encoding="utf-8")

    syntax = subprocess.run(
        ["sh", "-n", str(wrapper)],
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
    assert source.index("umask 0022") < source.index(
        'exec /usr/local/bin/docker-entrypoint.sh "$@"'
    )
    for name in POSTGRES_SECRET_SOURCES:
        assert name.replace("-", "_") in source


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
    return subprocess.run(
        [
            "docker",
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
        env=os.environ | {"AEGIS_RELEASE_ID": "container-runtime-test"},
    )


def protected_volume_created_at() -> str | None:
    result = subprocess.run(
        [
            "docker",
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


@pytest.mark.integration
def test_live_postgres_stages_secrets_and_drops_to_uid_70(tmp_path: Path) -> None:
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
                                "tmpfs": {"size": 268_435_456},
                            }
                        ],
                    }
                },
                "secrets": {name: {"file": path} for name, path in secret_files.items()},
            }
        ),
        encoding="utf-8",
    )

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

        started = docker_compose(project, override, "up", "--detach", "--build", "postgres")
        assert started.returncode == 0
        identity = docker_compose(project, override, "ps", "--quiet", "postgres")
        assert identity.returncode == 0
        container_id = identity.stdout.strip()
        assert container_id

        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            health = subprocess.run(
                [
                    "docker",
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
                break
            if health.stdout.strip() == "unhealthy":
                pytest.fail("disposable PostgreSQL became unhealthy", pytrace=False)
            time.sleep(0.25)
        else:
            pytest.fail("disposable PostgreSQL did not become healthy", pytrace=False)

        mounts = subprocess.run(
            ["docker", "inspect", container_id, "--format", "{{json .Mounts}}"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert all(
            mount.get("Name") != "aegis_postgres-data" for mount in json.loads(mounts.stdout)
        )
        assert all(mount.get("Type") != "volume" for mount in json.loads(mounts.stdout))

        process_status = subprocess.run(
            [
                "docker",
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
            metadata = subprocess.run(
                [
                    "docker",
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

        source_access = subprocess.run(
            [
                "docker",
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
            authentication = subprocess.run(
                [
                    "docker",
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

        logs = subprocess.run(
            ["docker", "logs", container_id],
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
            timeout=60,
        )
        assert stopped.returncode == 0
        if container_id:
            assert (
                subprocess.run(
                    ["docker", "inspect", container_id],
                    check=False,
                    capture_output=True,
                    text=True,
                ).returncode
                != 0
            )
        assert protected_volume_created_at() == protected_created_at
