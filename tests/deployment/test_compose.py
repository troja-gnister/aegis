import json
import os
import subprocess
import urllib.request
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
UV_IMAGE = (
    "ghcr.io/astral-sh/uv:0.12.8@"
    "sha256:d1cbaeadc234fe19c0d93daabcf5e98738cd93c6d1dd4918ef6aa30735feb23a"
)
PYTHON_IMAGE = (
    "python:3.13.15-slim-trixie@"
    "sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2"
)


def rendered_compose(
    *profiles: str, environment: dict[str, str] | None = None
) -> dict:
    profile_arguments = [argument for profile in profiles for argument in ("--profile", profile)]
    result = subprocess.run(
        [
            "docker",
            "compose",
            *profile_arguments,
            "-f",
            "compose.yaml",
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=os.environ | (environment or {}),
    )
    return json.loads(result.stdout)


def test_nginx_configuration_parses_with_pinned_runtime() -> None:
    config = REPOSITORY / "deploy" / "nginx" / "nginx.conf"
    server_config = REPOSITORY / "deploy" / "nginx" / "aegis-server.conf"
    image = (
        "nginxinc/nginx-unprivileged:1.30.4-alpine@"
        "sha256:45ce1e2e699234253d1def7baa96218a5d00b498d1ba0cbb1a17b6bdf73d1351"
    )

    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--add-host",
            "web:127.0.0.1",
            "--add-host",
            "tls-gateway:127.0.0.1",
            "--volume",
            f"{config}:/etc/nginx/nginx.conf:ro",
            "--volume",
            f"{server_config}:/etc/nginx/aegis-server.conf:ro",
            "--entrypoint",
            "nginx",
            image,
            "-t",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_nginx_configuration_failure_remains_visible_to_ci(tmp_path: Path) -> None:
    config = (REPOSITORY / "deploy" / "nginx" / "nginx.conf").read_text(
        encoding="utf-8"
    )
    invalid = tmp_path / "invalid-nginx.conf"
    invalid.write_text(
        config.replace("worker_processes auto;", "invalid_directive;", 1),
        encoding="utf-8",
    )
    server_config = REPOSITORY / "deploy" / "nginx" / "aegis-server.conf"
    image = (
        "nginxinc/nginx-unprivileged:1.30.4-alpine@"
        "sha256:45ce1e2e699234253d1def7baa96218a5d00b498d1ba0cbb1a17b6bdf73d1351"
    )

    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--add-host",
            "web:127.0.0.1",
            "--add-host",
            "tls-gateway:127.0.0.1",
            "--volume",
            f"{invalid}:/etc/nginx/nginx.conf:ro",
            "--volume",
            f"{server_config}:/etc/nginx/aegis-server.conf:ro",
            "--entrypoint",
            "nginx",
            image,
            "-t",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "invalid_directive" in result.stderr


def test_gateway_build_collects_only_admin_static_with_pinned_python_stage() -> None:
    dockerfile = (REPOSITORY / "docker" / "gateway.Dockerfile").read_text(encoding="utf-8")
    final_stage = dockerfile.split(
        "FROM nginxinc/nginx-unprivileged:1.30.4-alpine@", maxsplit=1
    )[1]

    assert f"FROM {UV_IMAGE} AS admin-static-uv" in dockerfile
    assert f"FROM {PYTHON_IMAGE} AS admin-static-build" in dockerfile
    assert "python manage.py collectstatic --noinput" in dockerfile
    assert (
        "COPY --from=admin-static-build /app/backend/staticfiles/admin/ "
        "/usr/share/nginx/html/admin-static/admin/"
    ) in final_stage
    assert "/app/backend/staticfiles/ /usr/share/nginx/html/admin-static/" not in final_stage
    assert "COPY --from=admin-static-build /app/.venv" not in final_stage

def test_core_services_are_unprivileged_and_postgres_is_private() -> None:
    config = rendered_compose()
    services = config["services"]
    assert {"gateway", "web", "migrate", "operations", "indexer", "media", "postgres"} <= set(
        services
    )
    assert "ports" not in services["postgres"]
    for name in ("gateway", "web", "operations", "indexer", "media"):
        assert services[name]["read_only"] is True
        assert services[name]["cap_drop"] == ["ALL"]
        assert services[name]["security_opt"] == ["no-new-privileges:true"]


def test_web_uses_bounded_log_config_from_process_start() -> None:
    command = rendered_compose()["services"]["web"]["command"]

    assert command[-2:] == ["--log-config", "/app/backend/aegis/uvicorn_logging.json"]


def test_caddy_overwrites_forwarding_headers_without_deleting_replacements() -> None:
    caddyfile = (REPOSITORY / "deploy" / "caddy" / "Caddyfile").read_text(
        encoding="utf-8"
    )

    assert "header_up X-Forwarded-For {remote_host}" in caddyfile
    assert "header_up X-Forwarded-Proto https" in caddyfile
    assert "header_up -X-Forwarded-For" not in caddyfile
    assert "header_up -X-Forwarded-Proto" not in caddyfile


def test_web_healthcheck_connects_loopback_with_public_authority_and_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = rendered_compose()["services"]["web"]["healthcheck"]["test"][-1]
    captured: dict[str, object] = {}

    def capture(request: urllib.request.Request, *, timeout: int) -> None:
        captured["url"] = request.full_url
        captured["host"] = request.get_header("Host")
        captured["forwarded_proto"] = request.get_header("X-forwarded-proto")
        captured["timeout"] = timeout

    monkeypatch.setenv("AEGIS_PUBLIC_URL", "https://public.example.test:9443")
    monkeypatch.setattr(urllib.request, "urlopen", capture)

    exec(command, {})

    assert captured == {
        "url": "http://127.0.0.1:8000/health/live",
        "host": "public.example.test:9443",
        "forwarded_proto": "https",
        "timeout": 2,
    }


def test_backend_network_is_internal_and_gateway_is_the_only_published_service() -> None:
    config = rendered_compose()
    services = config["services"]

    assert config["networks"]["backend"]["internal"] is True
    assert services["gateway"]["ports"] == [
        {
            "mode": "ingress",
            "target": 8080,
            "published": "8080",
            "protocol": "tcp",
        }
    ]
    assert all(
        "ports" not in service
        for name, service in services.items()
        if name != "gateway" and not service.get("profiles")
    )


def test_tls_hop_is_private_alias_bound_and_trusted_by_web() -> None:
    config = rendered_compose("tls")
    services = config["services"]

    assert config["networks"]["tls-hop"]["internal"] is True
    assert set(services["gateway"]["networks"]) == {"backend", "edge", "tls-hop"}
    assert services["gateway"]["networks"]["tls-hop"]["aliases"] == ["tls-gateway"]
    assert set(services["caddy"]["networks"]) == {"edge", "tls-hop"}
    assert {
        name
        for name, service in services.items()
        if "tls-hop" in service.get("networks", {})
    } == {"gateway", "caddy"}
    assert [publisher["target"] for publisher in services["gateway"]["ports"]] == [8080]
    assert services["web"]["environment"]["AEGIS_TRUST_PROXY_HEADERS"] == "true"


def test_caddy_tmpfs_ownership_follows_nondefault_runtime_ids() -> None:
    caddy = rendered_compose(
        "tls", environment={"AEGIS_UID": "21001", "AEGIS_GID": "21002"}
    )["services"]["caddy"]

    assert caddy["user"] == "21001:21002"
    assert all("uid=21001" in item and "gid=21002" in item for item in caddy["tmpfs"])


def test_caddy_build_removes_unneeded_file_capability_without_weakening_policy() -> None:
    caddy = rendered_compose("tls")["services"]["caddy"]
    dockerfile_path = REPOSITORY / "docker" / "caddy.Dockerfile"

    assert dockerfile_path.is_file()
    dockerfile = dockerfile_path.read_text(encoding="utf-8")
    assert caddy["image"] == "aegis-caddy"
    assert caddy["build"] == {
        "context": str(REPOSITORY),
        "dockerfile": "docker/caddy.Dockerfile",
    }
    assert caddy["cap_drop"] == ["ALL"]
    assert caddy["security_opt"] == ["no-new-privileges:true"]
    assert (
        "FROM caddy:2.11.4-alpine@"
        "sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648"
    ) in dockerfile
    assert "setcap -r /usr/bin/caddy" in dockerfile
    assert "test -z \"$(getcap /usr/bin/caddy)\"" in dockerfile


def test_only_core_gateway_joins_non_internal_edge_network_for_host_ingress() -> None:
    config = rendered_compose()
    services = config["services"]

    assert config["networks"]["edge"].get("internal", False) is False
    assert set(services["gateway"]["networks"]) == {"backend", "edge", "tls-hop"}
    for name, service in services.items():
        if name != "gateway" and not service.get("profiles"):
            assert "edge" not in service["networks"]


def test_base_compose_has_no_original_or_docker_socket_mounts() -> None:
    config = rendered_compose()

    for service in config["services"].values():
        for mount in service.get("volumes", []):
            source = mount.get("source", "")
            target = mount["target"]
            assert "docker.sock" not in source
            assert "docker.sock" not in target
            assert "/srv/aegis/roots" not in target


def test_postgres_18_data_volume_uses_major_version_parent_mount() -> None:
    postgres = rendered_compose()["services"]["postgres"]
    data_mount = next(
        mount for mount in postgres["volumes"] if mount["source"] == "postgres-data"
    )

    assert data_mount["target"] == "/var/lib/postgresql"
    assert data_mount["target"] != "/var/lib/postgresql/data"


def test_worker_commands_and_volumes_are_role_scoped() -> None:
    services = rendered_compose()["services"]
    expected = {
        "operations": {"staging"},
        "indexer": set(),
        "media": {"derivatives", "quarantine"},
    }

    for role, volume_sources in expected.items():
        assert services[role]["command"] == ["python", "manage.py", "run_role", "--role", role]
        assert {mount["source"] for mount in services[role].get("volumes", [])} == volume_sources
        assert [secret["source"] for secret in services[role]["secrets"]] == [
            "django-secret-key",
            f"db-{role}-password",
        ]
