from __future__ import annotations

import http.cookiejar
import json
import os
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

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
PYTHON_IMAGE = (
    "python:3.13.15-slim-trixie@"
    "sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2"
)
ADMIN_PASSWORD = "Tls-Probe-Anchor-934!"
COMMAND_TIMEOUT_SECONDS = 30
COMPOSE_TIMEOUT_SECONDS = 180
DIAGNOSTIC_LIMIT = 8 * 1024


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


@dataclass
class TlsStack:
    project: str
    override: Path
    http_port: int
    https_port: int
    client_names: list[str]

    @property
    def compose_arguments(self) -> list[str]:
        return [
            "compose",
            "--project-name",
            self.project,
            "--project-directory",
            str(REPOSITORY),
            "-f",
            str(REPOSITORY / "compose.yaml"),
            "-f",
            str(self.override),
            "--profile",
            "tls",
        ]

    def compose(
        self, arguments: Sequence[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return run_command(
            ["docker", *self.compose_arguments, *arguments],
            cwd=REPOSITORY,
            env=os.environ
            | {
                "AEGIS_HTTP_PORT": str(self.http_port),
                "AEGIS_HTTPS_PORT": str(self.https_port),
            },
            check=check,
            timeout=COMPOSE_TIMEOUT_SECONDS,
        )

    def service_container(self, service: str) -> str:
        return self.compose(["ps", "--quiet", service]).stdout.strip()

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

    def start_client(self, suffix: str) -> str:
        name = f"{self.project}-client-{suffix}"
        run_command(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                name,
                "--network",
                f"{self.project}_edge",
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
            ],
        )
        self.client_names.append(name)
        return name

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
            "  raw=socket.create_connection(('caddy',8443),self.timeout);"
            "self.sock=self._context.wrap_socket(raw,server_hostname='localhost')\n"
            "\nfor _ in range(requests):\n"
            " connection=CaddyConnection('localhost',8443,context=ctx,timeout=3);"
            "connection.request('POST','/admin/login/',body=b'',headers=headers);"
            "statuses.append(connection.getresponse().status);connection.close()\n"
            "print(json.dumps(statuses))"
        )
        result = run_command(
            ["docker", "exec", name, "python", "-c", script],
            timeout=90,
        )
        return json.loads(result.stdout)

    def wait_until_ready(self, *, timeout_seconds: int = 30) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_state = "not attempted"
        while time.monotonic() < deadline:
            try:
                status, _, _ = self.request(
                    "/admin/login/", tls=True, follow_redirects=False
                )
                if status == 200:
                    return
                last_state = f"HTTP {status}"
            except (OSError, urllib.error.URLError) as error:
                last_state = type(error).__name__
            time.sleep(0.2)

        processes = self.compose(["ps", "--all"], check=False)
        logs = self.compose(
            ["logs", "--no-color", "--tail", "80", "web", "gateway", "caddy"],
            check=False,
        )
        process_output = bounded_tail(processes.stdout + processes.stderr)
        log_output = bounded_tail(logs.stdout + logs.stderr)
        raise AssertionError(
            f"TLS stack did not become ready ({last_state})\n"
            f"processes:\n{process_output}\nlogs:\n{log_output}"
        )


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


def test_client_rate_probe_dials_caddy_with_localhost_sni(
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
    assert "('caddy',8443)" in captured["script"]
    assert "server_hostname='localhost'" in captured["script"]
    assert "198.51.100.42" in captured["script"]


@pytest.fixture(scope="module")
def tls_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TlsStack]:
    root = tmp_path_factory.mktemp("tls-stack")
    password_file = root / "admin-password"
    password_file.write_text(ADMIN_PASSWORD, encoding="utf-8")
    password_file.chmod(0o600)
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
  caddy:
    environment:
      AEGIS_TLS_HOST: localhost
secrets:
  admin-password:
    file: {password_file}
""".lstrip(),
        encoding="utf-8",
    )
    stack = TlsStack(project, override, http_port, https_port, [])

    try:
        started = stack.compose(
            ["up", "--build", "--detach", "postgres", "migrate", "web", "gateway", "caddy"],
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
        for name in stack.client_names:
            run_command(
                ["docker", "rm", "--force", name],
                check=False,
            )
        stack.compose(["down", "--volumes", "--remove-orphans"], check=False)


def test_tls_listener_alias_is_bound_only_to_tls_hop(tls_stack: TlsStack) -> None:
    gateway = tls_stack.service_container("gateway")
    caddy = tls_stack.service_container("caddy")
    gateway_info = json.loads(
        run_command(
            ["docker", "inspect", gateway],
        ).stdout
    )[0]
    caddy_info = json.loads(
        run_command(
            ["docker", "inspect", caddy],
        ).stdout
    )[0]
    tls_network = f"{tls_stack.project}_tls-hop"
    tls_address = gateway_info["NetworkSettings"]["Networks"][tls_network]["IPAddress"]
    aliases = gateway_info["NetworkSettings"]["Networks"][tls_network]["Aliases"]

    resolved = run_command(
        ["docker", "exec", gateway, "getent", "hosts", "tls-gateway"],
    ).stdout.split()[0]

    assert {"tls-gateway", "gateway"} <= set(aliases)
    assert resolved == tls_address
    assert set(caddy_info["NetworkSettings"]["Networks"]) == {
        f"{tls_stack.project}_edge",
        tls_network,
    }
    assert run_command(
        [
            "docker",
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
        run_command(["docker", "inspect", first]).stdout
    )[0]["NetworkSettings"]["Networks"][f"{tls_stack.project}_edge"]["IPAddress"]
    second_address = json.loads(
        run_command(["docker", "inspect", second]).stdout
    )[0]["NetworkSettings"]["Networks"][f"{tls_stack.project}_edge"]["IPAddress"]

    first_statuses = tls_stack.client_statuses(
        first, 25, spoofed_forwarded_for=second_address
    )
    second_statuses = tls_stack.client_statuses(second, 1)

    assert first_address != second_address
    assert 503 in first_statuses
    assert second_statuses == [403]


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
    caddy = tls_stack.service_container("caddy")
    request_canary = "caddy-request-credential-canary"
    header_canary = "caddy-header-credential-canary"

    status, _, _ = tls_stack.request(
        f"/missing/{request_canary}?credential={request_canary}",
        tls=True,
        headers={"X-Request-ID": header_canary},
    )
    info = json.loads(
        run_command(["docker", "inspect", caddy]).stdout
    )[0]
    capabilities = run_command(
        ["docker", "exec", caddy, "getcap", "/usr/bin/caddy"]
    ).stdout.strip()
    logs = tls_stack.compose(["logs", "--no-color", "caddy"], check=False)
    rendered = logs.stdout + logs.stderr

    assert status == 200
    assert info["Config"]["User"] == "10001:10001"
    assert capabilities == ""
    assert info["HostConfig"]["CapDrop"] == ["ALL"]
    assert info["HostConfig"]["SecurityOpt"] == ["no-new-privileges:true"]
    assert run_command(
        ["docker", "exec", caddy, "test", "-f", "/data/caddy/pki/authorities/local/root.crt"],
        check=False,
    ).returncode == 0
    assert run_command(
        ["docker", "exec", caddy, "test", "-f", "/config/caddy/autosave.json"],
        check=False,
    ).returncode == 0
    assert request_canary not in rendered
    assert header_canary not in rendered
