from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

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
        mount
        for mount in gateway["volumes"]
        if mount["target"] == "/srv/aegis/derivatives"
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


def test_postgres_stages_fixed_source_secrets_into_uid_70_private_tmpfs() -> None:
    postgres = rendered_compose()["services"]["postgres"]

    assert postgres["image"] == "aegis-postgres"
    assert postgres["build"] == {
        "context": str(REPOSITORY),
        "dockerfile": "docker/postgres.Dockerfile",
    }
    assert postgres["entrypoint"] == ["/usr/local/bin/aegis-postgres-entrypoint"]
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
