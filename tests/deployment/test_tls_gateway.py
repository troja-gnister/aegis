from __future__ import annotations

import http.cookiejar
import ipaddress
import json
import os
import secrets
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pytest
import yaml
from aegisctl.container_engine import (
    compose_environment,
    container_command,
    sanitized_environment,
    selected_engine,
)
from aegisctl.container_network import network_subnets
from aegisctl.container_resources import (
    ProjectInventory,
    ProjectResource,
    ProjectResourceRule,
    admit_project_transition,
    capture_project_inventory,
    cleanup_project_inventory,
    require_empty_project,
    require_project_inventory,
)

from tests.support.container_runtime import (
    OwnedDirectResource,
    OwnedDirectScope,
    cleanup_owned_resources,
    map_inventory_files_to_container_user,
    prepare_owned_test_inventory,
    read_optional_cidfile,
    record_created_test_path,
    record_fresh_test_tree,
    record_test_tree_inventory,
    recover_owned_resource,
    validate_owned_resources,
)
from tests.support.fake_container_engine import select_fake_engine

REPOSITORY = Path(__file__).resolve().parents[2]
CONTAINER_COMMAND = container_command()
PYTHON_IMAGE = (
    "docker.io/library/python:3.13.15-slim-trixie@"
    "sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2"
)
ADMIN_PASSWORD = "Tls-Probe-Anchor-934!"
COMMAND_TIMEOUT_SECONDS = 30
COMPOSE_TIMEOUT_SECONDS = 180
DIAGNOSTIC_LIMIT = 8 * 1024
CADDY_SERVICE = "caddy-local"


def _tls_compose_rules(arguments: Sequence[str]) -> tuple[ProjectResourceRule, ...]:
    if not arguments or arguments[0] not in {"up", "run"}:
        return ()
    oneoff = arguments[0] == "run"
    known_services = (
        "postgres", "migrate", "web", "operations", "indexer", "media", "gateway",
        CADDY_SERVICE,
    )
    services = tuple(service for service in known_services if service in arguments)
    rules: tuple[ProjectResourceRule, ...] = tuple(
        ProjectResourceRule("container", (
            ("com.docker.compose.service", service),
            ("com.docker.compose.oneoff", "True" if oneoff else "False"),
        )) for service in services
    )
    if oneoff:
        return rules
    return rules + tuple(
        ProjectResourceRule("network", (("com.docker.compose.network", network),))
        for network in ("backend", "edge", "tls-hop")
    ) + tuple(
        ProjectResourceRule("volume", (("com.docker.compose.volume", volume),))
        for volume in (
            "indexer-coordination", "postgres-data", "caddy-local-data", "staging",
            "derivatives", "model-cache", "quarantine", "frontier-outbox",
        )
    )


def bounded_tail(value: str) -> str:
    return value[-DIAGNOSTIC_LIMIT:]


def run_command(
    arguments: Sequence[str],
    *,
    check: bool = True,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments,
            check=check,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        raise AssertionError(
            f"command timed out after {timeout}s\n"
            f"stdout:\n{error.stdout or ''}\n"
            f"stderr:\n{error.stderr or ''}"
        ) from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> None:
        del request, fp, code, msg, headers, newurl
        return None


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def compose_project_resources(project: str) -> dict[str, list[str]]:
    label = f"label=com.docker.compose.project={project}"
    resources: dict[str, list[str]] = {}
    for kind, arguments in {
        "container": ("container", "ls", "--all", "--quiet", "--filter", label),
        "network": ("network", "ls", "--quiet", "--filter", label),
        "volume": ("volume", "ls", "--quiet", "--filter", label),
    }.items():
        resources[kind] = run_command([*CONTAINER_COMMAND, *arguments]).stdout.split()
    return resources


@dataclass
class TlsStack:
    project: str
    override: Path
    http_port: int
    https_port: int
    client_names: list[OwnedDirectResource]
    resources: ProjectInventory | None = None

    @property
    def compose_arguments(self) -> list[str]:
        return [
            "compose",
            "--env-file",
            "/dev/null",
            "--project-name",
            self.project,
            "--project-directory",
            str(REPOSITORY),
            "-f",
            str(REPOSITORY / "compose.yaml"),
            "-f",
            str(self.override),
            "--profile",
            "tls-local",
        ]

    def compose(
        self, arguments: Sequence[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        if arguments and arguments[0] == "down":
            raise AssertionError("TLS teardown requires exact recorded resource cleanup")
        environment = sanitized_environment(os.environ)
        environment.update({
            "AEGIS_RELEASE_ID": "phase1-tls-test",
            "AEGIS_UID": str(os.geteuid()),
            "AEGIS_GID": str(os.getegid()),
            "AEGIS_HTTP_PORT": f"127.0.0.1:{self.http_port}",
            "AEGIS_LOCAL_HTTPS_PORT": f"127.0.0.1:{self.https_port}",
        })
        if self.resources is None:
            self.resources = require_empty_project(self.project)
        else:
            require_project_inventory(self.resources)
        mutation = bool(arguments) and arguments[0] in {"up", "run", "restart", "stop", "rm"}
        try:
            result = run_command(
                [*CONTAINER_COMMAND, *self.compose_arguments, *arguments],
                cwd=REPOSITORY,
                env=compose_environment(environment),
                check=False,
                timeout=COMPOSE_TIMEOUT_SECONDS,
            )
        finally:
            if mutation:
                assert self.resources is not None
                observed = capture_project_inventory(self.project)
                self.resources = admit_project_transition(
                    self.resources, observed, _tls_compose_rules(arguments),
                )
        if check and result.returncode:
            raise AssertionError("TLS Compose command failed")
        return result

    def service_container(self, service: str) -> str:
        return self.compose(["ps", "--quiet", service]).stdout.strip()

    def remove_compose_service(self, service: str) -> None:
        if self.resources is None:
            raise AssertionError("TLS project inventory was not initialized")
        require_project_inventory(self.resources)
        matches = []
        for resource in self.resources.resources:
            if resource.kind != "container":
                continue
            try:
                labels = json.loads(resource.fingerprint)["Labels"]
            except (KeyError, TypeError, ValueError) as exc:
                raise AssertionError("TLS project resource fingerprint is invalid") from exc
            if (
                labels.get("com.docker.compose.service") == service
                and labels.get("com.docker.compose.oneoff") == "False"
            ):
                matches.append(resource)
        if len(matches) != 1:
            raise AssertionError("TLS Compose service identity is ambiguous")
        target = matches[0]
        result: subprocess.CompletedProcess[str] | None = None
        error: Exception | None = None
        try:
            result = run_command(
                [*CONTAINER_COMMAND, "rm", "--force", target.immutable_id], check=False,
            )
        except Exception as exc:
            error = exc
        observed = capture_project_inventory(self.project)
        self.resources = admit_project_transition(
            self.resources, observed, (), allowed_removed=(target,),
        )
        if error is not None:
            raise error
        if result is None or result.returncode or target in self.resources.resources:
            raise AssertionError("failed exact TLS Compose service removal")

    def request(
        self,
        path: str,
        *,
        tls: bool,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        data: bytes | None = None,
        cookies: http.cookiejar.CookieJar | None = None,
        follow_redirects: bool = True,
    ) -> tuple[int, Mapping[str, str], bytes]:
        scheme = "https" if tls else "http"
        port = self.https_port if tls else self.http_port
        handlers: list[urllib.request.BaseHandler] = []
        if tls:
            handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        if cookies is not None:
            handlers.append(urllib.request.HTTPCookieProcessor(cookies))
        if not follow_redirects:
            handlers.append(NoRedirect())
        opener = urllib.request.build_opener(*handlers)
        request = urllib.request.Request(
            f"{scheme}://localhost:{port}{path}",
            method=method,
            headers=dict(headers or {}),
            data=data,
        )
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def start_client(
        self,
        suffix: str,
        *,
        network: str | None = None,
        ip_address: str | None = None,
    ) -> str:
        name = f"{self.project}-client-{suffix}"
        cidfile = self.override.parent / f"client-{suffix}.cid"
        selected_network = network or f"{self.project}_edge"
        network_arguments = ["--network", selected_network]
        if ip_address is not None:
            network_arguments += ["--ip", ip_address]
        created: subprocess.CompletedProcess[str] | None = None
        creation_error: Exception | None = None
        interruption: KeyboardInterrupt | SystemExit | None = None
        try:
            created = run_command([
                *CONTAINER_COMMAND,
                "create",
                "--cidfile",
                str(cidfile),
                "--name",
                name,
                "--label",
                f"aegis.tls.owner={self.project}",
                "--label",
                f"aegis.tls.resource={name}",
                *network_arguments,
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=8m",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                PYTHON_IMAGE,
                "sleep",
                "120",
            ], check=False)
        except (KeyboardInterrupt, SystemExit) as exc:
            interruption = exc
        except Exception as exc:
            creation_error = exc
        identity = read_optional_cidfile(cidfile)
        try:
            resource = recover_owned_resource(
                "container", identity, "aegis.tls.owner", self.project,
                "aegis.tls.resource", name,
                lambda *arguments: run_command(
                    [*CONTAINER_COMMAND, *arguments], check=False,
                ),
            )
        except Exception as exc:
            if interruption is not None:
                raise interruption from exc
            raise
        self.client_names.append(resource)
        if interruption is not None:
            raise interruption
        if creation_error is not None:
            raise creation_error
        if created is None or created.returncode:
            raise AssertionError("TLS client create failed after identity recovery")
        run_command([*CONTAINER_COMMAND, "start", resource.immutable_id])
        return name

    def remove_owned_client(self, name: str) -> None:
        match = [resource for resource in self.client_names if resource.immutable_id == name]
        if len(match) != 1:
            raise AssertionError("TLS client identity is not recorded")
        cleanup_owned_resources(
            tuple(self.client_names),
            lambda *arguments: run_command([*CONTAINER_COMMAND, *arguments], check=False),
            scopes=(OwnedDirectScope(
                "container", "aegis.tls.owner", self.project,
            ),),
            remove=(match[0],),
        )
        self.client_names.remove(match[0])
    def client_statuses(
        self, name: str, count: int, *, spoofed_forwarded_for: str | None = None
    ) -> list[int]:
        request_headers = {"Host": "localhost"}
        if spoofed_forwarded_for is not None:
            request_headers["X-Forwarded-For"] = spoofed_forwarded_for
        script = (
            "import http.client,json,socket,ssl;"
            "ctx=ssl._create_unverified_context();statuses=[];"
            f"requests={count};"
            f"headers={request_headers!r};"
            "\nclass CaddyConnection(http.client.HTTPSConnection):\n"
            " def connect(self):\n"
            f"  raw=socket.create_connection(('{CADDY_SERVICE}',8443),self.timeout);"
            "self.sock=self._context.wrap_socket(raw,server_hostname='localhost')\n"
            "\nfor _ in range(requests):\n"
            " connection=CaddyConnection('localhost',8443,context=ctx,timeout=3);"
            "connection.request('POST','/admin/login/',body=b'',headers=headers);"
            "statuses.append(connection.getresponse().status);connection.close()\n"
            "print(json.dumps(statuses))"
        )
        result = run_command(
            [*CONTAINER_COMMAND, "exec", name, "python", "-c", script],
            timeout=90,
        )
        return json.loads(result.stdout)

    def client_login_status(
        self, name: str, username: str, *, spoofed_forwarded_for: str | None = None
    ) -> int:
        extra_headers = {}
        if spoofed_forwarded_for is not None:
            extra_headers["X-Forwarded-For"] = spoofed_forwarded_for
        script = (
            "import http.client,http.cookies,json,socket,ssl;"
            "ctx=ssl._create_unverified_context();"
            "\nclass CaddyConnection(http.client.HTTPSConnection):\n"
            " def connect(self):\n"
            f"  raw=socket.create_connection(('{CADDY_SERVICE}',8443),self.timeout);"
            "self.sock=self._context.wrap_socket(raw,server_hostname='localhost')\n"
            "\nconnection=CaddyConnection('localhost',8443,context=ctx,timeout=3);"
            "connection.request('GET','/api/v1/auth/csrf',headers={'Host':'localhost'});"
            "response=connection.getresponse();token=json.loads(response.read())['csrfToken'];"
            "cookies=http.cookies.SimpleCookie();cookies.load(response.getheader('Set-Cookie'));"
            "csrf_cookie=cookies['csrftoken'].value;connection.close();"
            f"body=json.dumps({{'username':{username!r},'password':'invalid-password'}}).encode();"
            "headers={'Host':'localhost','Content-Type':'application/json',"
            "'X-CSRFToken':token,'Cookie':'csrftoken='+csrf_cookie,"
            "'Origin':'https://localhost'};"
            f"headers.update({extra_headers!r});"
            "connection=CaddyConnection('localhost',8443,context=ctx,timeout=3);"
            "connection.request('POST','/api/v1/auth/login',body=body,headers=headers);"
            "response=connection.getresponse();response.read();print(response.status);"
            "connection.close()"
        )
        result = run_command([*CONTAINER_COMMAND, "exec", name, "python", "-c", script])
        return int(result.stdout.strip())

    def ip_throttle_bucket_count(self) -> int:
        result = self.compose(
            [
                "exec",
                "--no-TTY",
                "web",
                "python",
                "manage.py",
                "shell",
                "--command",
                (
                    "from aegis_apps.identity.models import LoginThrottleBucket;"
                    "print(LoginThrottleBucket.objects.filter(kind='ip').count())"
                ),
            ]
        )
        return int(result.stdout.strip().splitlines()[-1])

    def wait_until_ready(self, *, timeout_seconds: int = 30) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_state = "not attempted"
        while time.monotonic() < deadline:
            try:
                status, _, _ = self.request(
                    "/api/v1/auth/csrf", tls=True, follow_redirects=False
                )
                if status == 200:
                    return
                last_state = f"HTTP {status}"
            except (OSError, urllib.error.URLError) as error:
                last_state = type(error).__name__
            time.sleep(0.2)

        processes = self.compose(["ps", "--all"], check=False)
        logs = self.compose(
            [
                "logs",
                "--no-color",
                "--tail",
                "80",
                "web",
                "gateway",
                CADDY_SERVICE,
            ],
            check=False,
        )
        process_output = bounded_tail(processes.stdout + processes.stderr)
        log_output = bounded_tail(logs.stdout + logs.stderr)
        raise AssertionError(
            f"TLS stack did not become ready ({last_state})\n"
            f"processes:\n{process_output}\nlogs:\n{log_output}"
        )

    def wait_until_service_healthy(
        self, service: str, *, timeout_seconds: int = 20
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_state = "container unavailable"
        while time.monotonic() < deadline:
            container = self.service_container(service)
            if container:
                inspected = run_command(
                    [*CONTAINER_COMMAND, "inspect", container], check=False
                )
                if inspected.returncode == 0:
                    info = json.loads(inspected.stdout)[0]
                    last_state = info["State"].get("Health", {}).get(
                        "Status", "health unavailable"
                    )
                    if last_state == "healthy":
                        return
            time.sleep(0.2)

        processes = self.compose(["ps", "--all", service], check=False)
        logs = self.compose(
            ["logs", "--no-color", "--tail", "40", service], check=False
        )
        process_output = bounded_tail(processes.stdout + processes.stderr)
        log_output = bounded_tail(logs.stdout + logs.stderr)
        raise AssertionError(
            f"{service} did not become healthy ({last_state})\n"
            f"processes:\n{process_output}\nlogs:\n{log_output}"
        )


def cleanup_tls_stack(stack: TlsStack) -> None:
    if stack.resources is None:
        raise AssertionError("TLS project inventory was not initialized")

    def runner(*arguments: str) -> subprocess.CompletedProcess[str]:
        return run_command([*CONTAINER_COMMAND, *arguments], check=False)

    client_scope = OwnedDirectScope("container", "aegis.tls.owner", stack.project)
    validate_owned_resources(
        tuple(stack.client_names), runner, scopes=(client_scope,),
    )
    require_project_inventory(stack.resources)
    cleanup_owned_resources(
        tuple(stack.client_names), runner, scopes=(client_scope,),
    )
    stack.client_names.clear()
    cleanup_project_inventory(stack.resources)
    stack.resources = ProjectInventory(stack.project, ())


class CaddyRecreator(Protocol):
    def remove_compose_service(self, service: str) -> None: ...
    def compose(self, arguments: Sequence[str]) -> object: ...


def recreate_caddy(stack: CaddyRecreator) -> None:
    stack.remove_compose_service(CADDY_SERVICE)
    stack.compose(["up", "--detach", "--no-deps", CADDY_SERVICE])


def test_tls_compose_preserves_validated_selection_and_strips_remote_operator_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    override = tmp_path / "override.yaml"
    override.write_text("services: {}\n", encoding="ascii")
    selected = {
        "AEGIS_CONTAINER_ENGINE": "podman",
        "PODMAN_COMPOSE_PROVIDER": "/owned/compose",
        "AEGIS_PODMAN_SOCKET": "/owned/private/podman.sock",
    }
    monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "podman")
    monkeypatch.setenv("PODMAN_COMPOSE_PROVIDER", selected["PODMAN_COMPOSE_PROVIDER"])
    monkeypatch.setenv("AEGIS_PODMAN_SOCKET", selected["AEGIS_PODMAN_SOCKET"])
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.invalid:2375")
    monkeypatch.setenv("COMPOSE_FILE", "/operator/compose.yaml")
    monkeypatch.setenv("AEGIS_DB_PASSWORD", "operator-secret")
    captured: list[dict[str, str]] = []

    def sanitize(source: Mapping[str, str]) -> dict[str, str]:
        assert source["DOCKER_HOST"].startswith("tcp://")
        return dict(selected)

    def compose_env(source: Mapping[str, str]) -> dict[str, str]:
        assert all(source[key] == value for key, value in selected.items())
        assert "COMPOSE_FILE" not in source
        assert "AEGIS_DB_PASSWORD" not in source
        return dict(source) | {"DOCKER_HOST": "unix:///owned/private/podman.sock"}

    def completed(
        arguments: Sequence[str], **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert arguments[:3] == ["podman", "--remote=false", "compose"]
        captured.append(dict(kwargs["env"]))  # type: ignore[arg-type]
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(f"{__name__}.CONTAINER_COMMAND", ["podman", "--remote=false"])
    monkeypatch.setattr(f"{__name__}.sanitized_environment", sanitize)
    monkeypatch.setattr(f"{__name__}.compose_environment", compose_env)
    monkeypatch.setattr(f"{__name__}.run_command", completed)
    monkeypatch.setattr(
        f"{__name__}.capture_project_inventory",
        lambda project: ProjectInventory(project, ()),
    )
    monkeypatch.setattr(f"{__name__}.require_project_inventory", lambda inventory: None)
    monkeypatch.setattr(
        f"{__name__}.require_empty_project", lambda project: ProjectInventory(project, ()),
    )
    stack = TlsStack("owned-project", override, 18080, 18443, [])

    for command in (["up", "--wait"], ["ps"], ["restart", "web"]):
        stack.compose(command)

    with pytest.raises(AssertionError, match="exact recorded resource cleanup"):
        stack.compose(["down"])
    assert len(captured) == 3
    assert all(env["DOCKER_HOST"] == "unix:///owned/private/podman.sock" for env in captured)


def test_tls_failed_compose_refuses_unknown_transition_and_retains_inventory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    override = tmp_path / "override.yaml"
    override.write_text("services: {}\n", encoding="ascii")
    project = "owned"
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
    stack = TlsStack(project, override, 18080, 18443, [], before)
    monkeypatch.setattr(f"{__name__}.require_project_inventory", lambda inventory: None)
    monkeypatch.setattr(
        f"{__name__}.run_command",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 125, "", "failed"),
    )
    monkeypatch.setattr(
        f"{__name__}.capture_project_inventory",
        lambda name: ProjectInventory(name, (unknown,)),
    )

    with pytest.raises(RuntimeError, match="unexpected"):
        stack.compose(["up", "gateway"], check=False)
    assert stack.resources == before


def test_tls_gateway_removal_records_transition_before_admitting_recreation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    override = tmp_path / "override.yaml"
    override.write_text("services: {}\n", encoding="ascii")
    project = "owned"

    def compose_resource(identity: str) -> ProjectResource:
        return ProjectResource(
            "container", identity, identity,
            json.dumps({
                "Id": identity, "Created": identity, "Name": f"{project}-gateway-1",
                "Image": "image", "Labels": {
                    "com.docker.compose.project": project,
                    "com.docker.compose.service": "gateway",
                    "com.docker.compose.oneoff": "False",
                },
            }, sort_keys=True, separators=(",", ":")),
        )

    original = compose_resource("original")
    recreated = compose_resource("recreated")
    retained = ProjectResource(
        "network", "network", "network",
        json.dumps({
            "Id": "network", "Created": "one", "Name": f"{project}_backend",
            "Driver": "bridge", "Labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.network": "backend",
            },
        }, sort_keys=True, separators=(",", ":")),
    )
    stack = TlsStack(
        project, override, 18080, 18443, [],
        ProjectInventory(project, (original, retained)),
    )
    inventories = iter((
        ProjectInventory(project, (retained,)),
        ProjectInventory(project, (recreated, retained)),
    ))
    monkeypatch.setattr(f"{__name__}.require_project_inventory", lambda inventory: None)
    monkeypatch.setattr(
        f"{__name__}.capture_project_inventory", lambda name: next(inventories),
    )
    commands: list[Sequence[str]] = []

    def completed(
        arguments: Sequence[str], **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        commands.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(f"{__name__}.run_command", completed)

    stack.remove_compose_service("gateway")
    assert stack.resources == ProjectInventory(project, (retained,))
    stack.compose(["up", "--detach", "--no-deps", "gateway"])
    assert stack.resources == ProjectInventory(project, (recreated, retained))
    assert [*CONTAINER_COMMAND, "rm", "--force", "original"] in commands


@pytest.mark.parametrize("engine", ("docker", "podman"))
def test_tls_cleanup_prevalidates_every_client_before_any_resource_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine: str,
) -> None:
    prefix = select_fake_engine(engine, tmp_path, monkeypatch)
    monkeypatch.setattr(f"{__name__}.CONTAINER_COMMAND", prefix)
    override = tmp_path / "override.yaml"
    override.write_text("services: {}\n", encoding="ascii")
    stack = TlsStack("owned", override, 18080, 18443, [
        OwnedDirectResource("container", "first", "aegis.tls.owner", "owned"),
        OwnedDirectResource("container", "second", "aegis.tls.owner", "owned"),
    ], ProjectInventory("owned", ()))
    commands: list[Sequence[str]] = []
    compose_cleanup: list[ProjectInventory] = []

    def completed(
        arguments: Sequence[str], **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        assert list(arguments[:len(prefix)]) == prefix
        arguments = list(arguments[len(prefix):])
        commands.append(arguments)
        if arguments[:2] == ["container", "ls"]:
            return subprocess.CompletedProcess(arguments, 0, "first\nsecond\n", "")
        identity = arguments[-1]
        payload = [{"Id": identity if identity == "first" else "replacement", "Config": {
            "Labels": {"aegis.tls.owner": "owned"},
        }}]
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    monkeypatch.setattr(f"{__name__}.run_command", completed)
    monkeypatch.setattr(f"{__name__}.require_project_inventory", lambda inventory: None)
    monkeypatch.setattr(f"{__name__}.cleanup_project_inventory", compose_cleanup.append)

    with pytest.raises(ValueError, match="changed"):
        cleanup_tls_stack(stack)
    assert not any(arguments[:2] == ["rm", "--force"] for arguments in commands)
    assert compose_cleanup == []


def test_caddy_recreation_records_exact_removal_before_normal_up() -> None:
    events: list[object] = []

    class Stack:
        def remove_compose_service(self, service: str) -> None:
            events.append(("remove", service))

        def compose(self, arguments: Sequence[str]) -> None:
            events.append(("compose", list(arguments)))

    recreate_caddy(Stack())
    assert events == [
        ("remove", CADDY_SERVICE),
        ("compose", ["up", "--detach", "--no-deps", CADDY_SERVICE]),
    ]


@pytest.mark.parametrize("engine", ("docker", "podman"))
def test_tls_client_identity_is_recorded_before_failed_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine: str,
) -> None:
    prefix = select_fake_engine(engine, tmp_path, monkeypatch)
    monkeypatch.setattr(f"{__name__}.CONTAINER_COMMAND", prefix)
    override = tmp_path / "override.yaml"
    override.write_text("services: {}\n", encoding="ascii")
    stack = TlsStack("owned", override, 18080, 18443, [])

    def completed(
        arguments: Sequence[str], **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        assert list(arguments[:len(prefix)]) == prefix
        arguments = list(arguments[len(prefix):])
        if arguments[0] == "create":
            Path(arguments[arguments.index("--cidfile") + 1]).write_text(
                "client-id\n", encoding="ascii",
            )
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if arguments[:2] == ["container", "ls"]:
            return subprocess.CompletedProcess(arguments, 0, "client-id\n", "")
        if arguments[:3] == ["container", "inspect", "client-id"]:
            payload = [{
                "Id": "client-id", "Config": {"Labels": {
                    "aegis.tls.owner": "owned",
                    "aegis.tls.resource": "owned-client-partial",
                }},
            }]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        if arguments[:2] == ["start", "client-id"]:
            raise RuntimeError("start failed")
        raise AssertionError(arguments)

    monkeypatch.setattr(f"{__name__}.run_command", completed)
    with pytest.raises(RuntimeError, match="start failed"):
        stack.start_client("partial")
    assert stack.client_names == [OwnedDirectResource(
        "container", "client-id", "aegis.tls.owner", "owned",
    )]


@pytest.mark.parametrize("interruption", (SystemExit(143), KeyboardInterrupt()))
@pytest.mark.parametrize("engine", ("docker", "podman"))
def test_tls_client_interruption_recovers_identity_then_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, interruption: BaseException, engine: str,
) -> None:
    prefix = select_fake_engine(engine, tmp_path, monkeypatch)
    monkeypatch.setattr(f"{__name__}.CONTAINER_COMMAND", prefix)
    override = tmp_path / "override.yaml"
    override.write_text("services: {}\n", encoding="ascii")
    stack = TlsStack("owned", override, 18080, 18443, [])

    def completed(
        arguments: Sequence[str], **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        assert list(arguments[:len(prefix)]) == prefix
        arguments = list(arguments[len(prefix):])
        if arguments[0] == "create":
            Path(arguments[arguments.index("--cidfile") + 1]).write_text(
                "client-id\n", encoding="ascii",
            )
            raise interruption
        if arguments[:2] == ["container", "ls"]:
            return subprocess.CompletedProcess(arguments, 0, "client-id\n", "")
        if arguments[:3] == ["container", "inspect", "client-id"]:
            payload = [{"Id": "client-id", "Config": {"Labels": {
                "aegis.tls.owner": "owned",
                "aegis.tls.resource": "owned-client-interrupted",
            }}}]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        raise AssertionError(arguments)

    monkeypatch.setattr(f"{__name__}.run_command", completed)
    with pytest.raises(type(interruption)):
        stack.start_client("interrupted")
    assert stack.client_names == [OwnedDirectResource(
        "container", "client-id", "aegis.tls.owner", "owned",
    )]


@pytest.mark.parametrize("engine", ("docker", "podman"))
def test_tls_client_unsafe_cid_still_recovers_and_preserves_interrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine: str,
) -> None:
    prefix = select_fake_engine(engine, tmp_path, monkeypatch)
    monkeypatch.setattr(f"{__name__}.CONTAINER_COMMAND", prefix)
    override = tmp_path / "override.yaml"
    override.write_text("services: {}\n", encoding="ascii")
    stack = TlsStack("owned", override, 18080, 18443, [])
    outside = tmp_path / "outside.cid"
    outside.write_text("client-id\n", encoding="ascii")

    def completed(
        arguments: Sequence[str], **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        assert list(arguments[:len(prefix)]) == prefix
        arguments = list(arguments[len(prefix):])
        if arguments[0] == "create":
            Path(arguments[arguments.index("--cidfile") + 1]).symlink_to(outside)
            raise KeyboardInterrupt
        if arguments[:2] == ["container", "ls"]:
            return subprocess.CompletedProcess(arguments, 0, "client-id\n", "")
        if arguments[:3] == ["container", "inspect", "client-id"]:
            payload = [{"Id": "client-id", "Config": {"Labels": {
                "aegis.tls.owner": "owned",
                "aegis.tls.resource": "owned-client-unsafe-cid",
            }}}]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        raise AssertionError(arguments)

    monkeypatch.setattr(f"{__name__}.run_command", completed)
    with pytest.raises(KeyboardInterrupt):
        stack.start_client("unsafe-cid")
    assert stack.client_names == [OwnedDirectResource(
        "container", "client-id", "aegis.tls.owner", "owned",
    )]


@pytest.mark.parametrize(
    "creation_error",
    (subprocess.TimeoutExpired(["create"], 30), OSError("transport failed")),
)
@pytest.mark.parametrize("engine", ("docker", "podman"))
def test_tls_address_occupant_exception_recovers_before_propagating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, creation_error: Exception, engine: str,
) -> None:
    prefix = select_fake_engine(engine, tmp_path, monkeypatch)
    monkeypatch.setattr(f"{__name__}.CONTAINER_COMMAND", prefix)
    override = tmp_path / "override.yaml"
    override.write_text("services: {}\n", encoding="ascii")
    stack = TlsStack("owned", override, 18080, 18443, [])
    create_arguments: list[str] = []

    def completed(
        arguments: Sequence[str], **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        assert list(arguments[:len(prefix)]) == prefix
        arguments = list(arguments[len(prefix):])
        if arguments[0] == "create":
            create_arguments.extend(arguments)
            raise creation_error
        if arguments[:2] == ["container", "ls"]:
            return subprocess.CompletedProcess(arguments, 0, "occupant-id\n", "")
        if arguments[:3] == ["container", "inspect", "occupant-id"]:
            payload = [{"Id": "occupant-id", "Config": {"Labels": {
                "aegis.tls.owner": "owned",
                "aegis.tls.resource": "owned-client-old-gateway-address",
            }}}]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        raise AssertionError(arguments)

    monkeypatch.setattr(f"{__name__}.run_command", completed)
    with pytest.raises(type(creation_error)):
        stack.start_client(
            "old-gateway-address", network="owned_backend", ip_address="10.0.0.8",
        )
    assert create_arguments[
        create_arguments.index("--network"):create_arguments.index("--network") + 4
    ] == ["--network", "owned_backend", "--ip", "10.0.0.8"]
    assert stack.client_names == [OwnedDirectResource(
        "container", "occupant-id", "aegis.tls.owner", "owned",
    )]


@pytest.mark.parametrize("engine", ("docker", "podman"))
def test_tls_client_nonzero_create_without_cidfile_recovers_unique_canonical_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine: str,
) -> None:
    prefix = select_fake_engine(engine, tmp_path, monkeypatch)
    monkeypatch.setattr(f"{__name__}.CONTAINER_COMMAND", prefix)
    override = tmp_path / "override.yaml"
    override.write_text("services: {}\n", encoding="ascii")
    stack = TlsStack("owned", override, 18080, 18443, [])
    identity = "c" * 64

    def completed(
        arguments: Sequence[str], **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        assert list(arguments[:len(prefix)]) == prefix
        arguments = list(arguments[len(prefix):])
        if arguments[0] == "create":
            return subprocess.CompletedProcess(arguments, 125, "", "failed")
        if arguments[:2] == ["container", "ls"]:
            return subprocess.CompletedProcess(arguments, 0, identity[:12] + "\n", "")
        if arguments[:3] == ["container", "inspect", identity[:12]]:
            payload = [{"Id": identity, "Config": {"Labels": {
                "aegis.tls.owner": "owned",
                "aegis.tls.resource": "owned-client-partial",
            }}}]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        raise AssertionError(arguments)

    monkeypatch.setattr(f"{__name__}.run_command", completed)
    with pytest.raises(AssertionError, match="create failed"):
        stack.start_client("partial")
    assert stack.client_names == [OwnedDirectResource(
        "container", identity, "aegis.tls.owner", "owned",
    )]


def test_tls_readiness_retries_transient_connection_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = TlsStack("readiness-retry", tmp_path / "override.yaml", 1, 2, [])
    responses: Iterator[tuple[int, Mapping[str, str], bytes] | BaseException] = iter(
        [urllib.error.URLError("not ready"), (503, {}, b""), (200, {}, b"")]
    )

    def request(*_args: object, **_kwargs: object) -> tuple[int, Mapping[str, str], bytes]:
        response = next(responses)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(stack, "request", request)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    stack.wait_until_ready(timeout_seconds=1)


def test_tls_readiness_timeout_has_bounded_process_and_log_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = TlsStack("readiness-timeout", tmp_path / "override.yaml", 1, 2, [])
    monotonic = iter([0.0, 0.0, 2.0])

    def unavailable(
        *_args: object, **_kwargs: object
    ) -> tuple[int, Mapping[str, str], bytes]:
        raise urllib.error.URLError("request-secret-canary")

    def compose(
        arguments: Sequence[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        del check
        if arguments[:2] == ["ps", "--all"]:
            return subprocess.CompletedProcess([], 0, "process-state-canary", "")
        return subprocess.CompletedProcess([], 0, "x" * 20_000 + "log-tail-canary", "")

    monkeypatch.setattr(stack, "request", unavailable)
    monkeypatch.setattr(stack, "compose", compose)
    monkeypatch.setattr(time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with pytest.raises(AssertionError) as caught:
        stack.wait_until_ready(timeout_seconds=1)

    rendered = str(caught.value)
    assert "process-state-canary" in rendered
    assert "log-tail-canary" in rendered
    assert "request-secret-canary" not in rendered
    assert len(rendered) < 20_000


def test_tls_readiness_does_not_consume_admin_login_rate_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = TlsStack("readiness-budget", tmp_path / "override.yaml", 1, 2, [])
    budget = 1

    def request(path: str, **_kwargs: object) -> tuple[int, Mapping[str, str], bytes]:
        nonlocal budget
        if path == "/admin/login/":
            budget -= 1
            return (200 if budget >= 0 else 503), {}, b""
        return 200, {}, b""

    monkeypatch.setattr(stack, "request", request)
    stack.wait_until_ready(timeout_seconds=1)
    status, _, _ = stack.request("/admin/login/", tls=True)
    assert status == 200, "Readiness polling must not spend the next admin request's budget"


def test_client_rate_probe_dials_local_caddy_with_localhost_sni(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = TlsStack("client-probe", tmp_path / "override.yaml", 1, 2, [])
    captured: dict[str, str] = {}

    def capture(
        arguments: Sequence[str], *, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        script = arguments[-1]
        compile(script, "<client-rate-probe>", "exec")
        captured["script"] = script
        return subprocess.CompletedProcess(arguments, 0, "[403]", "")

    monkeypatch.setattr(f"{__name__}.run_command", capture)

    statuses = stack.client_statuses(
        "client-container", 1, spoofed_forwarded_for="198.51.100.42"
    )

    assert statuses == [403]
    assert "('caddy-local',8443)" in captured["script"]
    assert "server_hostname='localhost'" in captured["script"]
    assert "198.51.100.42" in captured["script"]


def isolated_backend_subnet(existing: Sequence[str], *, start: int) -> str:
    networks = [ipaddress.ip_network(value) for value in existing]
    for offset in range(256):
        candidate = ipaddress.ip_network(f"10.253.{(start + offset) % 256}.0/24")
        if not any(candidate.overlaps(network) for network in networks if network.version == 4):
            return str(candidate)
    raise AssertionError("No non-overlapping isolated TLS test subnet is available")


def configured_subnets(networks: Sequence[dict]) -> list[str]:
    engine = selected_engine()
    return [subnet for network in networks for subnet in network_subnets(network, engine)]


@pytest.mark.parametrize("engine", ("docker", "podman"))
def test_tls_network_inventory_handles_null_default_network_ipam(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine: str,
) -> None:
    prefix = select_fake_engine(engine, tmp_path, monkeypatch)
    monkeypatch.setattr(f"{__name__}.CONTAINER_COMMAND", prefix)
    networks = [
        {"IPAM": {"Config": None}}, {"IPAM": None}, {"IPAM": {}},
        {"IPAM": {"Config": [{"Subnet": "172.17.0.0/16"}, {}, {"Subnet": None}]}},
    ] if engine == "docker" else [
        {"subnets": []}, {"subnets": [{"subnet": "172.17.0.0/16"}]},
    ]
    assert configured_subnets(networks) == ["172.17.0.0/16"]


def test_isolated_tls_subnet_avoids_existing_networks_and_fails_closed() -> None:
    assert isolated_backend_subnet(["10.253.0.0/24", "fd00::/64"], start=0) == "10.253.1.0/24"
    assert isolated_backend_subnet(["10.253.255.0/24"], start=255) == "10.253.0.0/24"
    with pytest.raises(AssertionError, match="non-overlapping"):
        isolated_backend_subnet(["10.253.0.0/16"], start=0)


@pytest.fixture(scope="module")
def tls_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TlsStack]:
    root = tmp_path_factory.mktemp("tls-stack")
    tree = record_fresh_test_tree(root)
    password_file = root / "admin-password"
    password_file.write_text(ADMIN_PASSWORD, encoding="utf-8")
    password_file.chmod(0o600)
    record_created_test_path(tree, password_file)
    throttle_hmac_file = root / "auth-throttle-hmac-key"
    throttle_hmac_file.write_text("a" * 64, encoding="utf-8")
    throttle_hmac_file.chmod(0o600)
    record_created_test_path(tree, throttle_hmac_file)
    container_secret_files = [password_file, throttle_hmac_file]
    http_port = free_port()
    https_port = free_port()
    project = f"aegis-tls-{uuid.uuid4().hex[:10]}"
    override = root / "compose.override.yaml"
    production_environment = f"""
      AEGIS_ENV: production
      AEGIS_PUBLIC_URL: https://localhost:{https_port}
      AEGIS_ALLOWED_HOSTS: localhost
      AEGIS_TRUST_PROXY_HEADERS: "true"
      DJANGO_SETTINGS_MODULE: aegis.settings.production
"""
    override.write_text(
        f"""
services:
  migrate:
    environment:
{production_environment}
    secrets:
      - source: admin-password
        target: admin_password
  web:
    environment:
{production_environment}
  gateway:
    environment:
      AEGIS_PUBLIC_URL: https://localhost:{https_port}
secrets:
  admin-password:
    file: {password_file}
  auth-throttle-hmac-key:
    file: {throttle_hmac_file}
""".lstrip(),
        encoding="utf-8",
    )
    record_created_test_path(tree, override)
    stack = TlsStack(project, override, http_port, https_port, [])

    # Use only per-test inputs; never depend on or read operator/dev credentials.
    configuration = yaml.safe_load(override.read_text())
    network_ids = run_command([*CONTAINER_COMMAND, "network", "ls", "--quiet"]).stdout.split()
    network_info = json.loads(
        run_command([*CONTAINER_COMMAND, "network", "inspect", *network_ids]).stdout
    )
    configuration["networks"] = {"backend": {"ipam": {"config": [{
        "subnet": isolated_backend_subnet(configured_subnets(network_info),
                                         start=secrets.randbelow(256)),
    }]}}}
    base = yaml.safe_load((REPOSITORY / "compose.yaml").read_text())
    for name in base["secrets"]:
        if name in configuration["secrets"]:
            continue
        secret = root / name
        secret.write_text(secrets.token_hex(32) + "\n", encoding="ascii")
        secret.chmod(0o600)
        record_created_test_path(tree, secret)
        configuration["secrets"][name] = {"file": str(secret)}
        container_secret_files.append(secret)
    override.write_text(yaml.safe_dump(configuration), encoding="utf-8")
    inventory = record_test_tree_inventory(tree)
    prepare_owned_test_inventory(inventory)
    map_inventory_files_to_container_user(
        inventory,
        tuple(container_secret_files),
        uid=os.geteuid(),
        gid=os.getegid(),
    )

    stack.resources = require_empty_project(project)

    try:
        started = stack.compose(
            [
                "up",
                "--build",
                "--detach",
                "postgres",
                "migrate",
                "web",
                "gateway",
                CADDY_SERVICE,
            ],
            check=False,
        )
        assert started.returncode == 0, started.stdout + started.stderr
        stack.compose(
            [
                "run",
                "--rm",
                "migrate",
                "python",
                "manage.py",
                "bootstrap_admin",
                "--username",
                "tls-admin",
                "--email",
                "tls-admin@example.invalid",
                "--password-file",
                "/run/secrets/admin_password",
            ]
        )

        stack.wait_until_ready()
        yield stack
    finally:
        cleanup_tls_stack(stack)


def test_tls_listener_alias_is_bound_only_to_tls_hop(tls_stack: TlsStack) -> None:
    gateway = tls_stack.service_container("gateway")
    caddy = tls_stack.service_container(CADDY_SERVICE)
    gateway_info = json.loads(
        run_command(
            [*CONTAINER_COMMAND, "inspect", gateway],
        ).stdout
    )[0]
    caddy_info = json.loads(
        run_command(
            [*CONTAINER_COMMAND, "inspect", caddy],
        ).stdout
    )[0]
    tls_network = f"{tls_stack.project}_tls-hop"
    tls_address = gateway_info["NetworkSettings"]["Networks"][tls_network]["IPAddress"]
    aliases = gateway_info["NetworkSettings"]["Networks"][tls_network]["Aliases"]

    resolved = run_command(
        [*CONTAINER_COMMAND, "exec", gateway, "getent", "hosts", "tls-gateway"],
    ).stdout.split()[0]

    assert {"tls-gateway", "gateway"} <= set(aliases)
    assert resolved == tls_address
    assert set(caddy_info["NetworkSettings"]["Networks"]) == {
        f"{tls_stack.project}_edge",
        tls_network,
    }
    assert run_command(
        [
            *CONTAINER_COMMAND,
            "run",
            "--rm",
            "--network",
            f"{tls_stack.project}_backend",
            PYTHON_IMAGE,
            "python",
            "-c",
            "import socket,sys;sys.exit(socket.socket().connect_ex(('gateway',8081)) == 0)",
        ],
        check=False,
    ).returncode == 0


def test_production_admin_login_has_secure_csrf_and_session_flow(
    tls_stack: TlsStack,
) -> None:
    cookies = http.cookiejar.CookieJar()
    status, _, body = tls_stack.request("/admin/login/", tls=True, cookies=cookies)
    csrf = next(cookie for cookie in cookies if cookie.name == "csrftoken")

    assert status == 200
    assert csrf.secure is True
    assert csrf.get_nonstandard_attr("SameSite") == "Lax"
    assert b"csrfmiddlewaretoken" in body

    denied, _, _ = tls_stack.request(
        "/admin/login/", tls=True, method="POST", data=b"username=tls-admin"
    )
    assert denied == 403

    form = urllib.parse.urlencode(
        {
            "username": "tls-admin",
            "password": ADMIN_PASSWORD,
            "csrfmiddlewaretoken": csrf.value,
            "next": "/admin/",
        }
    ).encode()
    authenticated, _, _ = tls_stack.request(
        "/admin/login/",
        tls=True,
        method="POST",
        headers={"Referer": f"https://localhost:{tls_stack.https_port}/admin/login/"},
        data=form,
        cookies=cookies,
    )
    session = next(cookie for cookie in cookies if cookie.name == "sessionid")

    assert authenticated == 200
    assert session.secure is True
    assert session.has_nonstandard_attr("HttpOnly")
    assert session.get_nonstandard_attr("SameSite") == "Lax"


def test_caddy_uses_distinct_actual_client_peer_rate_buckets(tls_stack: TlsStack) -> None:
    first = tls_stack.start_client("first")
    second = tls_stack.start_client("second")
    first_address = json.loads(
        run_command([*CONTAINER_COMMAND, "inspect", first]).stdout
    )[0]["NetworkSettings"]["Networks"][f"{tls_stack.project}_edge"]["IPAddress"]
    second_address = json.loads(
        run_command([*CONTAINER_COMMAND, "inspect", second]).stdout
    )[0]["NetworkSettings"]["Networks"][f"{tls_stack.project}_edge"]["IPAddress"]

    first_statuses = tls_stack.client_statuses(
        first, 25, spoofed_forwarded_for=second_address
    )
    second_statuses = tls_stack.client_statuses(second, 1)

    assert first_address != second_address
    assert 503 in first_statuses
    assert second_statuses == [403]


def test_gateway_forwards_distinct_unspoofable_client_ips_to_auth_throttle(
    tls_stack: TlsStack,
) -> None:
    web_info = json.loads(
        run_command([*CONTAINER_COMMAND, "inspect", tls_stack.service_container("web")]).stdout
    )[0]
    gateway_info = json.loads(
        run_command([*CONTAINER_COMMAND, "inspect", tls_stack.service_container("gateway")]).stdout
    )[0]
    caddy_info = json.loads(
        run_command(
            [*CONTAINER_COMMAND, "inspect", tls_stack.service_container(CADDY_SERVICE)]
        ).stdout
    )[0]
    assert web_info["State"]["Health"]["Status"] == "healthy"
    assert gateway_info["State"]["Health"]["Status"] == "healthy"
    assert caddy_info["State"]["Status"] == "running"

    first = tls_stack.start_client("auth-first")
    second = tls_stack.start_client("auth-second")
    second_address = json.loads(run_command([*CONTAINER_COMMAND, "inspect", second]).stdout)[0][
        "NetworkSettings"
    ]["Networks"][f"{tls_stack.project}_edge"]["IPAddress"]
    before = tls_stack.ip_throttle_bucket_count()

    first_status = tls_stack.client_login_status(first, f"first-{uuid.uuid4().hex}")
    second_status = tls_stack.client_login_status(second, f"second-{uuid.uuid4().hex}")

    assert first_status == second_status == 401
    assert tls_stack.ip_throttle_bucket_count() == before + 2

    spoofed_status = tls_stack.client_login_status(
        first,
        f"spoofed-{uuid.uuid4().hex}",
        spoofed_forwarded_for=second_address,
    )

    assert spoofed_status == 401
    assert tls_stack.ip_throttle_bucket_count() == before + 2


def test_public_gateway_cannot_spoof_proxy_attestation(tls_stack: TlsStack) -> None:
    status, headers, body = tls_stack.request(
        "/health/proxy-attestation",
        tls=True,
        headers={
            "X-Aegis-Proxy-Attestation": "startup-v1",
            "X-Forwarded-For": "192.0.2.254",
        },
    )

    assert status == 404
    assert body == b""
    assert headers["Cache-Control"] == "private, no-store"


def test_gateway_ip_drift_fails_closed_until_web_restarts(
    tls_stack: TlsStack,
) -> None:
    backend_network = f"{tls_stack.project}_backend"
    original_gateway = tls_stack.service_container("gateway")
    original_gateway_info = json.loads(
        run_command([*CONTAINER_COMMAND, "inspect", original_gateway]).stdout
    )[0]
    if original_gateway_info["Config"]["Labels"].get(
        "com.docker.compose.project"
    ) != tls_stack.project:
        raise AssertionError("refusing to replace an unowned gateway container")
    original_address = original_gateway_info["NetworkSettings"]["Networks"][
        backend_network
    ]["IPAddress"]
    tls_stack.remove_compose_service("gateway")
    occupant = tls_stack.start_client(
        "old-gateway-address",
        network=backend_network,
        ip_address=original_address,
    )

    try:
        recreated = tls_stack.compose(
            ["up", "--detach", "--no-deps", "gateway"], check=False
        )
        assert recreated.returncode == 0, recreated.stdout + recreated.stderr
        gateway = tls_stack.service_container("gateway")
        gateway_info = json.loads(
            run_command([*CONTAINER_COMMAND, "inspect", gateway]).stdout
        )[0]
        current_address = gateway_info["NetworkSettings"]["Networks"][
            backend_network
        ]["IPAddress"]

        assert current_address != original_address
        time.sleep(5)
        gateway_info = json.loads(
            run_command([*CONTAINER_COMMAND, "inspect", gateway]).stdout
        )[0]
        assert gateway_info["State"]["Health"]["Status"] != "healthy"
        listener_result = run_command(
            [
                *CONTAINER_COMMAND,
                "exec",
                occupant,
                "python",
                "-c",
                (
                    "import socket;"
                    "peer=socket.gethostbyname('gateway');"
                    "connection=socket.socket();connection.settimeout(2);"
                    "print(connection.connect_ex((peer,8080)))"
                ),
            ]
        )
        assert listener_result.stdout.strip() != "0"

        try:
            public_status, _, _ = tls_stack.request("/api/v1/auth/csrf", tls=True)
        except (OSError, urllib.error.URLError):
            public_status = None
        assert public_status != 200
    finally:
        tls_stack.compose(["up", "--detach", "--no-deps", "gateway"])
        tls_stack.compose(["restart", "web"])
        tls_stack.wait_until_ready(timeout_seconds=60)

    tls_stack.wait_until_service_healthy("gateway")
    recovered_gateway_info = json.loads(
        run_command(
            [*CONTAINER_COMMAND, "inspect", tls_stack.service_container("gateway")]
        ).stdout
    )[0]
    assert recovered_gateway_info["State"]["Health"]["Status"] == "healthy"

    first = tls_stack.start_client("drift-auth-first")
    second = tls_stack.start_client("drift-auth-second")
    second_address = json.loads(run_command([*CONTAINER_COMMAND, "inspect", second]).stdout)[0][
        "NetworkSettings"
    ]["Networks"][f"{tls_stack.project}_edge"]["IPAddress"]
    before = tls_stack.ip_throttle_bucket_count()

    first_status = tls_stack.client_login_status(first, f"first-{uuid.uuid4().hex}")
    second_status = tls_stack.client_login_status(second, f"second-{uuid.uuid4().hex}")
    spoofed_status = tls_stack.client_login_status(
        first,
        f"spoofed-{uuid.uuid4().hex}",
        spoofed_forwarded_for=second_address,
    )

    assert first_status == second_status == spoofed_status == 401
    assert tls_stack.ip_throttle_bucket_count() == before + 2


def test_public_http_ignores_spoofed_forwarding_scheme(tls_stack: TlsStack) -> None:
    status, headers, _ = tls_stack.request(
        "/admin/login/",
        tls=False,
        headers={
            "X-Forwarded-For": "203.0.113.99",
            "X-Forwarded-Proto": "https",
        },
        follow_redirects=False,
    )

    assert status in {301, 302}
    assert headers["Location"] == f"https://localhost:{tls_stack.https_port}/admin/login/"


def test_caddy_starts_unprivileged_and_output_omits_canaries(tls_stack: TlsStack) -> None:
    caddy = tls_stack.service_container(CADDY_SERVICE)
    request_canary = "caddy-request-credential-canary"
    header_canary = "caddy-header-credential-canary"

    status, _, _ = tls_stack.request(
        f"/missing/{request_canary}?credential={request_canary}",
        tls=True,
        headers={"X-Request-ID": header_canary},
    )
    info = json.loads(
        run_command([*CONTAINER_COMMAND, "inspect", caddy]).stdout
    )[0]
    capabilities = run_command(
        [*CONTAINER_COMMAND, "exec", caddy, "getcap", "/usr/bin/caddy"]
    ).stdout.strip()
    logs = tls_stack.compose(["logs", "--no-color", CADDY_SERVICE], check=False)
    rendered = logs.stdout + logs.stderr

    assert status == 200
    assert info["Config"]["User"] == "10001:10001"
    assert capabilities == ""
    assert info["HostConfig"]["CapDrop"] == ["ALL"]
    assert info["HostConfig"]["SecurityOpt"] == ["no-new-privileges:true"]
    assert run_command(
        [*CONTAINER_COMMAND, "exec", caddy, "test", "-f",
         "/data/caddy/pki/authorities/local/root.crt"],
        check=False,
    ).returncode == 0
    assert run_command(
        [*CONTAINER_COMMAND, "exec", caddy, "test", "-f", "/config/caddy/autosave.json"],
        check=False,
    ).returncode == 0
    assert request_canary not in rendered
    assert header_canary not in rendered


def test_local_caddy_preserves_ca_and_certificate_across_recreation(
    tls_stack: TlsStack,
) -> None:
    certificate_paths = [
        "/data/caddy/pki/authorities/local/root.crt",
        "/data/caddy/certificates/local/localhost/localhost.crt",
    ]
    original = tls_stack.service_container(CADDY_SERVICE)
    before = run_command(
        [*CONTAINER_COMMAND, "exec", original, "sha256sum", *certificate_paths]
    ).stdout

    recreate_caddy(tls_stack)
    tls_stack.wait_until_ready()

    recreated = tls_stack.service_container(CADDY_SERVICE)
    after = run_command(
        [*CONTAINER_COMMAND, "exec", recreated, "sha256sum", *certificate_paths]
    ).stdout
    status, _, body = tls_stack.request("/admin/login/", tls=True)

    assert recreated != original
    assert after == before
    assert status == 200
    assert b"csrfmiddlewaretoken" in body


@pytest.mark.parametrize("engine", ["docker", "podman"])
def test_native_network_subnets_prevent_tls_overlap(
    monkeypatch: pytest.MonkeyPatch, engine: str,
) -> None:
    monkeypatch.setattr(f"{__name__}.selected_engine", lambda: engine)
    network = ({"subnets": [{"subnet": "10.253.0.0/24"}, {"subnet": "fd00::/64"}]}
               if engine == "podman" else
               {"IPAM": {"Config": [{"Subnet": "10.253.0.0/24"}, {"Subnet": "fd00::/64"}]}})
    assert configured_subnets([network]) == ["10.253.0.0/24", "fd00::/64"]
    assert isolated_backend_subnet(configured_subnets([network]), start=0) == "10.253.1.0/24"


@pytest.mark.parametrize("engine, network", [
    ("docker", {}), ("docker", {"IPAM": []}),
    ("docker", {"IPAM": {"Config": {}}}),
    ("docker", {"IPAM": {"Config": ["bad"]}}),
    ("docker", {"IPAM": {"Config": [{"Subnet": 42}]}}),
    ("docker", {"IPAM": None, "subnets": []}),
    ("podman", {"subnets": None}), ("podman", {"subnets": {}}),
    ("podman", {"subnets": ["bad"]}), ("podman", {"subnets": [{}]}),
    ("podman", {"subnets": [{"subnet": None}]}),
    ("podman", {"subnets": [{"subnet": "not-a-network"}]}),
    ("podman", {"IPAM": None}),
])
def test_native_network_subnets_refuse_unknown_shapes(
    monkeypatch: pytest.MonkeyPatch, engine: str, network: dict,
) -> None:
    monkeypatch.setattr(f"{__name__}.selected_engine", lambda: engine)
    with pytest.raises(ValueError, match="network"):
        configured_subnets([network])
