import json
import os
import subprocess
import tempfile
import urllib.request
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aegis_apps.common import runtime_checks
from aegisctl.container_engine import compose_environment as validated_compose_environment
from aegisctl.container_engine import container_command
from aegisctl.container_resources import (
    ProjectInventory,
    ProjectResource,
    ProjectResourceRule,
    admit_project_transition,
    capture_project_inventory,
    cleanup_project_inventory,
    require_empty_project,
)
from django.test import override_settings

from tests.support.container_runtime import copy_bind_inputs
from tests.support.fake_container_engine import ProjectEngine, select_fake_engine

REPOSITORY = Path(__file__).resolve().parents[2]
CONTAINER_COMMAND = container_command()
UV_IMAGE = (
    "ghcr.io/astral-sh/uv:0.12.8@"
    "sha256:d1cbaeadc234fe19c0d93daabcf5e98738cd93c6d1dd4918ef6aa30735feb23a"
)
PYTHON_IMAGE = (
    "docker.io/library/python:3.13.15-slim-trixie@"
    "sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2"
)
NODE_IMAGE = (
    "docker.io/library/node:24.20.0-bookworm-slim@"
    "sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e"
)
CADDY_IMAGE = (
    "docker.io/library/caddy:2.11.4-alpine@"
    "sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648"
)
PUBLIC_TLS_HOST = "files.operator-domain.dev"


INVALID_TLS_RULES = (
    ProjectResourceRule("container", (
        ("com.docker.compose.service", "caddy"),
        ("com.docker.compose.oneoff", "True"),
    )),
    ProjectResourceRule("network", (("com.docker.compose.network", "backend"),)),
    ProjectResourceRule("network", (("com.docker.compose.network", "edge"),)),
    ProjectResourceRule("network", (("com.docker.compose.network", "tls-hop"),)),
    ProjectResourceRule("volume", (("com.docker.compose.volume", "caddy-data"),)),
    ProjectResourceRule("volume", (("com.docker.compose.volume", "derivatives"),)),
)


def _run_invalid_tls_host_probe(
    project: str, command: list[str], environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    expected = require_empty_project(project, environment)
    result: subprocess.CompletedProcess[str] | None = None
    error: BaseException | None = None
    try:
        result = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=120,
            env=environment,
        )
    except BaseException as exc:
        error = exc
    try:
        observed = capture_project_inventory(project, environment)
        created = admit_project_transition(expected, observed, INVALID_TLS_RULES)
        cleanup_project_inventory(created, environment)
    except BaseException as recovery_error:
        if isinstance(error, (SystemExit, KeyboardInterrupt)):
            raise error from recovery_error
        raise
    if error is not None:
        raise error
    if result is None:
        raise AssertionError("invalid-host TLS probe produced no result")
    return result


def _project_resource(
    kind: str, identity: str, labels: dict[str, str],
) -> ProjectResource:
    fields = {"Id": identity, "Name": identity, "Labels": labels}
    return ProjectResource(
        kind, identity, identity,
        json.dumps(fields, sort_keys=True, separators=(",", ":")),
    )


def test_invalid_tls_probe_preserves_unknown_addition_without_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = ProjectInventory("probe", ())
    unknown = ProjectInventory("probe", (_project_resource(
        "container", "foreign", {
            "com.docker.compose.project": "probe",
            "com.docker.compose.service": "postgres",
            "com.docker.compose.oneoff": "True",
        },
    ),))
    cleaned: list[ProjectInventory] = []
    monkeypatch.setattr(f"{__name__}.require_empty_project", lambda *args: empty)
    monkeypatch.setattr(f"{__name__}.capture_project_inventory", lambda *args: unknown)
    monkeypatch.setattr(f"{__name__}.cleanup_project_inventory", cleaned.append)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 64, "", "invalid host",
    ))

    with pytest.raises(RuntimeError, match="unexpected project resource transition"):
        _run_invalid_tls_host_probe("probe", ["compose", "run"], {})
    assert cleaned == []


def test_invalid_tls_probe_propagates_cleanup_query_failure_without_broad_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = ProjectInventory("probe", ())
    created = ProjectInventory("probe", (_project_resource(
        "container", "oneoff", {
            "com.docker.compose.project": "probe",
            "com.docker.compose.service": "caddy",
            "com.docker.compose.oneoff": "True",
        },
    ),))
    commands: list[list[str]] = []
    monkeypatch.setattr(f"{__name__}.require_empty_project", lambda *args: empty)
    monkeypatch.setattr(f"{__name__}.capture_project_inventory", lambda *args: created)

    def refuse(*args: object) -> None:
        del args
        raise RuntimeError("cleanup inventory query failed")

    monkeypatch.setattr(f"{__name__}.cleanup_project_inventory", refuse)

    def completed(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        commands.append(command)
        return subprocess.CompletedProcess(command, 64, "", "invalid host")

    monkeypatch.setattr(subprocess, "run", completed)
    with pytest.raises(RuntimeError, match="cleanup inventory query failed"):
        _run_invalid_tls_host_probe("probe", ["compose", "run"], {})
    assert commands == [["compose", "run"]]
    assert all("down" not in command for command in commands)


def rendered_compose(
    *profiles: str, environment: dict[str, str] | None = None
) -> dict[str, Any]:
    profile_arguments = [argument for profile in profiles for argument in ("--profile", profile)]
    environment_for_compose = (
        os.environ
        | {"AEGIS_RELEASE_ID": "test-release-identity"}
        | (environment or {})
    )
    result = subprocess.run(
        [
            *CONTAINER_COMMAND,
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
        env=validated_compose_environment(environment_for_compose),
    )
    return cast(dict[str, Any], json.loads(result.stdout))


def adapted_caddyfile(path: Path, *, tls_host: str | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="aegis-caddy-bind-", dir="/tmp") as temporary:
        copied = copy_bind_inputs(Path(temporary), {"Caddyfile": path})["Caddyfile"]
        arguments = [
            *CONTAINER_COMMAND, "run", "--rm", "--volume",
            f"{copied}:/etc/caddy/Caddyfile:ro",
        ]
        if tls_host is not None:
            arguments.extend(["--env", f"AEGIS_TLS_HOST={tls_host}"])
        result = subprocess.run(
            [*arguments, CADDY_IMAGE, "caddy", "adapt", "--config",
             "/etc/caddy/Caddyfile", "--adapter", "caddyfile"],
            check=True, capture_output=True, text=True, timeout=30,
        )
    return cast(dict[str, Any], json.loads(result.stdout))


def test_nginx_configuration_parses_with_pinned_runtime(tmp_path: Path) -> None:
    config = REPOSITORY / "deploy" / "nginx" / "nginx.conf"
    server_config = REPOSITORY / "deploy" / "nginx" / "aegis-server.conf"
    image = (
        "docker.io/nginxinc/nginx-unprivileged:1.30.4-alpine@"
        "sha256:45ce1e2e699234253d1def7baa96218a5d00b498d1ba0cbb1a17b6bdf73d1351"
    )
    inputs = copy_bind_inputs(tmp_path, {"nginx.conf": config, "server.conf": server_config})

    subprocess.run(
        [
            *CONTAINER_COMMAND,
            "run",
            "--rm",
            "--add-host",
            "web:127.0.0.1",
            "--add-host",
            "tls-gateway:127.0.0.1",
            "--volume",
            f"{inputs['nginx.conf']}:/etc/nginx/nginx.conf:ro",
            "--volume",
            f"{inputs['server.conf']}:/etc/nginx/aegis-server.conf:ro",
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
    inputs = copy_bind_inputs(
        tmp_path, {"invalid.conf": invalid, "server.conf": server_config}
    )
    image = (
        "docker.io/nginxinc/nginx-unprivileged:1.30.4-alpine@"
        "sha256:45ce1e2e699234253d1def7baa96218a5d00b498d1ba0cbb1a17b6bdf73d1351"
    )

    result = subprocess.run(
        [
            *CONTAINER_COMMAND,
            "run",
            "--rm",
            "--add-host",
            "web:127.0.0.1",
            "--add-host",
            "tls-gateway:127.0.0.1",
            "--volume",
            f"{inputs['invalid.conf']}:/etc/nginx/nginx.conf:ro",
            "--volume",
            f"{inputs['server.conf']}:/etc/nginx/aegis-server.conf:ro",
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
        "FROM docker.io/nginxinc/nginx-unprivileged:1.30.4-alpine@", maxsplit=1
    )[1]

    assert f"FROM {NODE_IMAGE} AS frontend-build" in dockerfile
    assert f"FROM {UV_IMAGE} AS admin-static-uv" in dockerfile
    assert f"FROM {PYTHON_IMAGE} AS admin-static-build" in dockerfile
    assert "python manage.py collectstatic --noinput" in dockerfile
    assert (
        "COPY --from=admin-static-build /app/backend/staticfiles/admin/ "
        "/usr/share/nginx/html/admin-static/admin/"
    ) in final_stage
    assert (
        "COPY --chmod=0555 deploy/nginx/start-gateway.sh "
        "/usr/local/bin/aegis-gateway-start"
    ) in final_stage
    assert 'CMD ["/usr/local/bin/aegis-gateway-start"]' in final_stage
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

    assert command == ["python", "-m", "aegis.proxy"]


def test_gateway_creation_does_not_wait_for_web_health() -> None:
    services = rendered_compose(
        "tls", "tls-local", environment={"AEGIS_TLS_HOST": PUBLIC_TLS_HOST}
    )["services"]

    assert "gateway" not in services["web"].get("depends_on", {})
    assert "web" not in services["gateway"].get("depends_on", {})
    assert services["gateway"]["healthcheck"]["test"] == [
        "CMD-SHELL",
        "test -f /tmp/aegis-upstream-ready && nc -z 127.0.0.1 8080",
    ]
    assert services["caddy"]["depends_on"]["gateway"]["condition"] == "service_healthy"
    assert services["caddy-local"]["depends_on"]["gateway"]["condition"] == (
        "service_healthy"
    )


def test_auth_throttle_hmac_secret_is_mounted_only_into_web() -> None:
    config = rendered_compose()
    services = config["services"]

    assert config["secrets"]["auth-throttle-hmac-key"]["file"].endswith(
        "/deploy/secrets/dev/auth-throttle-hmac-key"
    )
    assert services["web"]["environment"]["AEGIS_AUTH_THROTTLE_HMAC_KEY_FILE"] == (
        "/run/secrets/auth_throttle_hmac_key"
    )
    assert "auth-throttle-hmac-key" in {
        secret["source"] for secret in services["web"]["secrets"]
    }
    for name in ("migrate", "operations", "indexer", "media"):
        assert "AEGIS_AUTH_THROTTLE_HMAC_KEY_FILE" not in services[name]["environment"]
        assert "auth-throttle-hmac-key" not in {
            secret["source"] for secret in services[name]["secrets"]
        }


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
    captured: dict[str, object] = {}

    def capture(
        request: urllib.request.Request, *, timeout: int
    ) -> nullcontext[SimpleNamespace]:
        captured["url"] = request.full_url
        captured["host"] = request.get_header("Host")
        captured["forwarded_proto"] = request.get_header("X-forwarded-proto")
        captured["timeout"] = timeout
        return nullcontext(SimpleNamespace(status=200))

    monkeypatch.setattr(runtime_checks.urllib.request, "urlopen", capture)
    with override_settings(AEGIS_PUBLIC_URL="https://public.example.test:9443"):
        runtime_checks.probe_local_web()

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
    config = rendered_compose("tls", environment={"AEGIS_TLS_HOST": PUBLIC_TLS_HOST})
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
    assert services["caddy"]["environment"]["AEGIS_TLS_HOST"] == PUBLIC_TLS_HOST


def test_production_and_local_tls_modes_have_distinct_issuers() -> None:
    production_path = REPOSITORY / "deploy" / "caddy" / "Caddyfile"
    local_path = REPOSITORY / "deploy" / "caddy" / "Caddyfile.local"
    production = adapted_caddyfile(production_path, tls_host=PUBLIC_TLS_HOST)
    local = adapted_caddyfile(local_path)
    production_rendered = json.dumps(production, sort_keys=True)
    local_rendered = json.dumps(local, sort_keys=True)

    assert "internal" not in production_rendered
    assert PUBLIC_TLS_HOST in production_rendered
    assert '"module": "internal"' in local_rendered
    assert "localhost" in local_rendered


def test_production_tls_exposes_acme_port_and_local_tls_keeps_development_port() -> None:
    production = rendered_compose(
        "tls",
        environment={"AEGIS_TLS_HOST": PUBLIC_TLS_HOST, "AEGIS_HTTPS_PORT": ""},
    )["services"]["caddy"]
    local = rendered_compose(
        "tls-local",
        environment={"AEGIS_HTTPS_PORT": "", "AEGIS_LOCAL_HTTPS_PORT": ""},
    )["services"]["caddy-local"]
    example_environment = (REPOSITORY / ".env.example").read_text(encoding="utf-8")

    assert production["ports"] == [
        {"mode": "ingress", "target": 8443, "published": "443", "protocol": "tcp"}
    ]
    assert local["ports"] == [
        {"mode": "ingress", "target": 8443, "published": "8443", "protocol": "tcp"}
    ]
    assert "AEGIS_HTTPS_PORT=443\n" in example_environment
    assert "AEGIS_LOCAL_HTTPS_PORT=8443\n" in example_environment


def test_caddy_data_is_durable_and_runtime_identity_is_fixed() -> None:
    config = rendered_compose(
        "tls", "tls-local", environment={"AEGIS_TLS_HOST": PUBLIC_TLS_HOST}
    )
    services = config["services"]
    caddy = services["caddy"]
    local = services["caddy-local"]

    assert caddy["user"] == local["user"] == "10001:10001"
    for service in (caddy, local):
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert set(service["networks"]) == {"edge", "tls-hop"}
    assert {mount["target"]: mount["source"] for mount in caddy["volumes"]}["/data"] == (
        "caddy-data"
    )
    assert {mount["target"]: mount["source"] for mount in local["volumes"]}["/data"] == (
        "caddy-local-data"
    )
    assert all(not item.startswith("/data:") for item in caddy["tmpfs"])
    assert all(not item.startswith("/data:") for item in local["tmpfs"])
    assert {
        name
        for name, service in services.items()
        if any(
            mount.get("source") == "caddy-data"
            for mount in service.get("volumes", [])
        )
    } == {"caddy"}
    assert {
        name
        for name, service in services.items()
        if any(
            mount.get("source") == "caddy-local-data"
            for mount in service.get("volumes", [])
        )
    } == {"caddy-local"}


def test_base_profile_does_not_require_tls_host_but_production_startup_does() -> None:
    base = rendered_compose()
    production = rendered_compose("tls", environment={"AEGIS_TLS_HOST": ""})

    assert "caddy" not in base["services"]
    assert production["services"]["caddy"]["environment"]["AEGIS_TLS_HOST"] == ""
    assert production["services"]["caddy"]["command"][0].endswith(
        "aegis-caddy-start"
    )


@pytest.mark.parametrize(
    "tls_host",
    [
        None,
        "localhost",
        "127.0.0.1",
        "files",
        "files.example",
        "files.example.test",
        "files.example.invalid",
        "files.alt",
        "FILES.ALT",
        "files.arpa",
        "FILES.EXAMPLE.TEST",
        "files.example.com",
        "files.xn--kgbechtv",
        "files.xn--hgbk6aj7f53bba",
        "files.xn--0zwm56d",
        "files.xn--g6w251d",
        "files.xn--80akhbyknj4f",
        "files.xn--11b5bs3a9aj6g",
        "files.xn--jxalpdlp",
        "files.xn--9t4b11yi5a",
        "files.xn--deba0ad",
        "files.xn--zckzah",
        "files.xn--hlcj6aya9esc7a",
        "-files.public.dev",
        "files-.public.dev",
        f"{'a' * 64}.public.dev",
        "files.public.123",
        "files.public.dev\n",
        "files.public.dev\n\n",
        "files.public\ndev.operator-domain",
        "files.public.dev\t",
        "files.public.dev\r",
        "files.públic.dev",
    ],
)
def test_production_caddy_start_rejects_non_public_hosts(
    tls_host: str | None,
) -> None:
    environment = os.environ.copy()
    if tls_host is None:
        environment.pop("AEGIS_TLS_HOST", None)
    else:
        environment["AEGIS_TLS_HOST"] = tls_host

    result = subprocess.run(
        ["sh", str(REPOSITORY / "deploy" / "caddy" / "aegis-caddy-start")],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        env=environment,
    )

    assert result.returncode == 64
    assert result.stdout == ""
    assert result.stderr == (
        "AEGIS_TLS_HOST must be set to a public DNS hostname for the tls profile\n"
    )


def test_production_caddy_start_exports_only_normalized_valid_host(
    tmp_path: Path,
) -> None:
    original = (REPOSITORY / "deploy" / "caddy" / "aegis-caddy-start").read_text(
        encoding="utf-8"
    )
    probe = original.replace(
        "exec /usr/bin/caddy run --config /etc/caddy/Caddyfile --adapter caddyfile",
        "printf '%s' \"$AEGIS_TLS_HOST\"",
        1,
    )
    probe_path = tmp_path / "aegis-caddy-start-probe"
    probe_path.write_text(probe, encoding="utf-8")

    result = subprocess.run(
        ["sh", str(probe_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        env=os.environ | {"AEGIS_TLS_HOST": "Files.Operator-Domain.Dev"},
    )

    assert probe != original
    assert result.returncode == 0
    assert result.stdout == "files.operator-domain.dev"
    assert result.stderr == ""


@pytest.mark.parametrize(
    "rejected_host",
    [
        "files.example.test",
        "files.alt",
        "FILES.ALT",
        "files.arpa",
        pytest.param("files.public.dev\n", id="trailing-newline"),
        pytest.param("files.public\ndev.operator-domain", id="internal-newline"),
        pytest.param("files.public.dev\t", id="trailing-tab"),
    ],
)
def test_production_tls_profile_rejects_invalid_host_before_acme(
    rejected_host: str,
) -> None:
    project = f"aegis-production-tls-probe-{uuid.uuid4().hex[:10]}"
    compose = [
        *CONTAINER_COMMAND,
        "compose",
        "--project-name",
        project,
        "--project-directory",
        str(REPOSITORY),
        "-f",
        str(REPOSITORY / "compose.yaml"),
        "--profile",
        "tls",
    ]
    environment = validated_compose_environment(os.environ | {
        "AEGIS_RELEASE_ID": "tls-rejection-test",
        "AEGIS_TLS_HOST": rejected_host,
    })

    result = _run_invalid_tls_host_probe(
        project,
        [*compose, "run", "--build", "--rm", "--no-deps", "caddy"],
        environment,
    )

    rendered = (result.stdout + result.stderr).lower()
    assert result.returncode == 64
    assert (
        "aegis_tls_host must be set to a public dns hostname for the tls profile"
        in rendered
    )
    assert "obtaining certificate" not in rendered
    assert "acme" not in rendered


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
        "FROM docker.io/library/caddy:2.11.4-alpine@"
        "sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648"
    ) in dockerfile
    assert "setcap -r /usr/bin/caddy" in dockerfile
    assert "test -z \"$(getcap /usr/bin/caddy)\"" in dockerfile
    assert (
        "COPY --chmod=0755 deploy/caddy/aegis-caddy-start "
        "/usr/local/bin/aegis-caddy-start"
    ) in dockerfile


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
        "indexer": {"indexer-coordination"},
        "media": {"derivatives", "quarantine"},
    }

    for role, volume_sources in expected.items():
        assert services[role]["command"] == ["python", "manage.py", "run_role", "--role", role]
        assert services[role]["environment"]["AEGIS_PROCESS_ROLE"] == role
        assert {mount["source"] for mount in services[role].get("volumes", [])} == volume_sources
        assert [secret["source"] for secret in services[role]["secrets"]] == [
            "django-secret-key",
            f"db-{role}-password",
        ]
    assert services["indexer"]["init"] is True
    assert services["indexer"]["volumes"] == [{
        "type": "volume", "source": "indexer-coordination",
        "target": "/srv/aegis/indexer-coordination", "volume": {},
    }]


def test_coordination_image_ownership_uses_configured_uid_gid() -> None:
    services = rendered_compose(environment={"AEGIS_UID": "501", "AEGIS_GID": "20"})["services"]
    assert services["indexer"]["user"] == "501:20"
    assert services["indexer"]["build"]["args"] == {"AEGIS_UID": "501", "AEGIS_GID": "20"}


def test_backend_release_identity_is_required_and_propagated_to_web_and_workers() -> None:
    release_id = "release-2026.09.08+compose.1"
    services = rendered_compose(environment={"AEGIS_RELEASE_ID": release_id})["services"]

    for service_name in ("web", "operations", "indexer", "media"):
        assert services[service_name]["environment"]["AEGIS_RELEASE_ID"] == release_id

    environment = os.environ.copy()
    environment.pop("AEGIS_RELEASE_ID", None)
    environment = validated_compose_environment(environment)
    result = subprocess.run(
        [*CONTAINER_COMMAND, "compose", "-f", "compose.yaml", "config", "--quiet"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode != 0
    assert "AEGIS_RELEASE_ID" in result.stderr


@pytest.mark.parametrize("interruption", [SystemExit, KeyboardInterrupt])
@pytest.mark.parametrize("uncertainty", [None, "capture", "admission", "cleanup"])
def test_invalid_tls_probe_preserves_interruption_through_recovery(
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException], uncertainty: str | None,
) -> None:
    empty = ProjectInventory("probe", ())
    original = interruption(17)
    recovery_error = RuntimeError("recovery uncertain")
    calls: list[str] = []

    def run(*args: object, **kwargs: object) -> None:
        raise original

    def step(name: str, result: object) -> Any:
        def perform(*args: object) -> object:
            calls.append(name)
            if uncertainty == name:
                raise recovery_error
            return result
        return perform

    monkeypatch.setattr(f"{__name__}.require_empty_project", lambda *args: empty)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(f"{__name__}.capture_project_inventory", step("capture", empty))
    monkeypatch.setattr(f"{__name__}.admit_project_transition", step("admission", empty))
    monkeypatch.setattr(f"{__name__}.cleanup_project_inventory", step("cleanup", None))
    with pytest.raises(interruption) as caught:
        _run_invalid_tls_host_probe("probe", ["compose", "run"], {})
    assert caught.value is original
    assert original.__cause__ is (None if uncertainty is None else recovery_error)
    expected = ["capture", "admission", "cleanup"]
    expected_calls = expected if uncertainty is None else expected[:expected.index(uncertainty) + 1]
    assert calls == expected_calls


@pytest.mark.parametrize("engine", ("docker", "podman"))
@pytest.mark.parametrize("unknown", (False, True))
def test_tls_complete_observed_inventory_exact_cleanup_or_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine: str, unknown: bool,
) -> None:
    prefix = select_fake_engine(engine, tmp_path, monkeypatch)
    declared = (
        ("network", "backend"), ("network", "edge"), ("network", "tls-hop"),
        ("volume", "caddy-data"), ("volume", "derivatives"),
    )
    if unknown:
        declared += (("volume", "unknown"),)
    fake = ProjectEngine(engine, "probe", declared)
    monkeypatch.setattr(subprocess, "run", fake)
    if unknown:
        with pytest.raises(RuntimeError, match="unexpected project resource transition"):
            _run_invalid_tls_host_probe("probe", [*prefix, "compose", "run"], dict(os.environ))
        assert fake.removals == []
        assert len(fake.resources) == len(declared)
    else:
        result = _run_invalid_tls_host_probe("probe", [*prefix, "compose", "run"], dict(os.environ))
        assert result.returncode == 64
        assert fake.removals == [
            ("network", "rm", "immutable-backend"), ("network", "rm", "immutable-edge"),
            ("network", "rm", "immutable-tls-hop"), ("volume", "rm", "probe_caddy-data"),
            ("volume", "rm", "probe_derivatives"),
        ]
        assert fake.resources == {}
        assert fake.queries[-3:] == ["container", "network", "volume"]
