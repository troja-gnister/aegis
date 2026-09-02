from __future__ import annotations

import ipaddress
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from email.message import Message
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
NGINX_CONFIG = REPOSITORY / "deploy" / "nginx" / "nginx.conf"
NGINX_SERVER_CONFIG = REPOSITORY / "deploy" / "nginx" / "aegis-server.conf"
UPSTREAM_FIXTURE = Path(__file__).parent / "fixtures" / "gateway_upstream.py"
PYTHON_IMAGE = (
    "python:3.13.15-slim-trixie@"
    "sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2"
)
NGINX_IMAGE = (
    "nginxinc/nginx-unprivileged:1.30.4-alpine@"
    "sha256:45ce1e2e699234253d1def7baa96218a5d00b498d1ba0cbb1a17b6bdf73d1351"
)
SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{8,64}\Z")
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Message
    body: bytes

    def json(self) -> dict[str, object]:
        return json.loads(self.body)


@dataclass(frozen=True)
class GatewayHarness:
    base_url: str
    internal_base_url: str
    resource_prefix: str
    container_name: str

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        internal: bool = False,
    ) -> HttpResponse:
        request = urllib.request.Request(
            f"{self.internal_base_url if internal else self.base_url}{path}",
            method=method,
            headers=dict(headers or {}),
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return HttpResponse(response.status, response.headers, response.read())
        except urllib.error.HTTPError as error:
            return HttpResponse(error.code, error.headers, error.read())


def docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments],
        check=check,
        capture_output=True,
        text=True,
    )


def remove_container(name: str) -> None:
    docker("rm", "--force", name, check=False)


def remove_network(name: str) -> None:
    docker("network", "rm", name, check=False)


def wait_for_gateway(base_url: str, container_name: str) -> None:
    deadline = time.monotonic() + 15
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/request-id", timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as error:
            last_error = error
        time.sleep(0.1)

    logs = docker("logs", container_name, check=False)
    raise AssertionError(
        f"gateway did not become ready: {last_error}\n{logs.stdout}\n{logs.stderr}"
    )


@pytest.fixture(scope="module")
def gateway(tmp_path_factory: pytest.TempPathFactory) -> Iterator[GatewayHarness]:
    suffix = uuid.uuid4().hex[:12]
    prefix = f"aegis-gateway-http-{suffix}"
    network_name = f"{prefix}-network"
    upstream_name = f"{prefix}-upstream"
    gateway_name = f"{prefix}-nginx"
    static_root = tmp_path_factory.mktemp("gateway-static")
    assets = static_root / "assets"
    admin_css = static_root / "admin-static" / "admin" / "css"
    assets.mkdir()
    admin_css.mkdir(parents=True)
    static_root.chmod(0o755)
    assets.chmod(0o755)
    admin_css.chmod(0o755)
    (static_root / "index.html").write_text("spa-shell", encoding="utf-8")
    (assets / "app-abcdefgh.js").write_text("asset-body", encoding="utf-8")
    (admin_css / "base.css").write_text("admin-css", encoding="utf-8")

    try:
        docker("network", "create", "--label", f"aegis.test.scope={suffix}", network_name)
        docker(
            "run",
            "--detach",
            "--name",
            upstream_name,
            "--label",
            f"aegis.test.scope={suffix}",
            "--network",
            network_name,
            "--network-alias",
            "web",
            "--user",
            "10001:10001",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--mount",
            f"type=bind,src={UPSTREAM_FIXTURE},dst=/fixture/gateway_upstream.py,readonly",
            PYTHON_IMAGE,
            "python",
            "/fixture/gateway_upstream.py",
        )
        docker(
            "run",
            "--detach",
            "--name",
            gateway_name,
            "--label",
            f"aegis.test.scope={suffix}",
            "--network",
            network_name,
            "--network-alias",
            "tls-gateway",
            "--user",
            "101:101",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=32m",
            "--tmpfs",
            "/var/cache/nginx:rw,noexec,nosuid,nodev,size=64m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--env",
            "NGINX_ENTRYPOINT_QUIET_LOGS=1",
            "--publish",
            "127.0.0.1::8080",
            "--publish",
            "127.0.0.1::8081",
            "--mount",
            f"type=bind,src={NGINX_CONFIG},dst=/etc/nginx/nginx.conf,readonly",
            "--mount",
            (
                f"type=bind,src={NGINX_SERVER_CONFIG},"
                "dst=/etc/nginx/aegis-server.conf,readonly"
            ),
            "--mount",
            f"type=bind,src={static_root},dst=/usr/share/nginx/html,readonly",
            NGINX_IMAGE,
        )
        binding = docker("port", gateway_name, "8080/tcp").stdout.strip()
        host, port = binding.rsplit(":", 1)
        assert host == "127.0.0.1"
        assert port.isdecimal() and int(port) > 0
        base_url = f"http://127.0.0.1:{port}"
        internal_binding = docker("port", gateway_name, "8081/tcp").stdout.strip()
        internal_host, internal_port = internal_binding.rsplit(":", 1)
        assert internal_host == "127.0.0.1"
        assert internal_port.isdecimal() and int(internal_port) > 0
        internal_base_url = f"http://127.0.0.1:{internal_port}"
        wait_for_gateway(base_url, gateway_name)
        yield GatewayHarness(base_url, internal_base_url, prefix, gateway_name)
    finally:
        remove_container(gateway_name)
        remove_container(upstream_name)
        remove_network(network_name)


def assert_security_headers(response: HttpResponse) -> None:
    for name, expected in SECURITY_HEADERS.items():
        assert response.headers[name] == expected


def cache_control_directives(response: HttpResponse) -> set[str]:
    return {
        directive.strip().lower()
        for value in response.headers.get_all("Cache-Control", [])
        for directive in value.split(",")
        if directive.strip()
    }


def assert_private_no_store(response: HttpResponse) -> None:
    assert cache_control_directives(response) == {"private", "no-store"}


def test_valid_bounded_request_ids_are_preserved(gateway: GatewayHarness) -> None:
    for request_id in ("Client-123_ok", "A" + "b" * 63):
        response = gateway.request("/api/request-id", headers={"X-Request-ID": request_id})

        assert response.status == 200
        assert response.json()["request_id"] == request_id


@pytest.mark.parametrize(
    "request_id", [None, "short", "invalid request id", "client.dot", "a" * 65]
)
def test_missing_or_invalid_request_ids_are_replaced_safely(
    gateway: GatewayHarness, request_id: str | None
) -> None:
    headers = {} if request_id is None else {"X-Request-ID": request_id}

    response = gateway.request("/api/request-id", headers=headers)
    generated = response.json()["request_id"]

    assert response.status == 200
    assert isinstance(generated, str)
    assert SAFE_REQUEST_ID.fullmatch(generated)
    assert len(generated) <= 64
    assert generated != request_id


def test_hostile_forwarding_headers_are_overwritten(gateway: GatewayHarness) -> None:
    response = gateway.request(
        "/api/forwarding",
        headers={
            "X-Forwarded-For": "203.0.113.10, 127.0.0.1",
            "X-Forwarded-Proto": "https",
            "X-Aegis-Proxy-Attestation": "startup-v1",
        },
    )
    forwarded_for = response.json()["forwarded_for"]

    assert response.status == 200
    assert isinstance(forwarded_for, str)
    ipaddress.ip_address(forwarded_for)
    assert forwarded_for != "203.0.113.10, 127.0.0.1"
    assert response.json()["forwarded_proto"] == "http"
    assert response.json()["proxy_attestation"] is None


def test_internal_listener_accepts_only_bounded_caddy_identity(
    gateway: GatewayHarness,
) -> None:
    accepted = gateway.request(
        "/api/forwarding",
        headers={"X-Forwarded-For": "198.51.100.42", "X-Forwarded-Proto": "http"},
        internal=True,
    )

    assert accepted.status == 200
    assert accepted.json()["forwarded_for"] == "198.51.100.42"
    assert accepted.json()["forwarded_proto"] == "https"

    ipv6 = gateway.request(
        "/api/forwarding",
        headers={"X-Forwarded-For": "2001:db8::42"},
        internal=True,
    )
    assert ipv6.status == 200
    assert ipv6.json()["forwarded_for"] == "2001:db8::42"

    mapped = gateway.request(
        "/api/forwarding",
        headers={"X-Forwarded-For": "::ffff:198.51.100.42"},
        internal=True,
    )
    assert mapped.status == 200
    assert mapped.json()["forwarded_for"] == "::ffff:198.51.100.42"

    for value in (
        None,
        "198.51.100.1, 203.0.113.1",
        "bad identity",
        "198.51.100.42 203.0.113.1",
        "999.999.999.999",
        "::::",
        "2001:db8:0:0:0:0:0:0:42",
        "a" * 46,
    ):
        headers = {} if value is None else {"X-Forwarded-For": value}
        rejected = gateway.request("/api/forwarding", headers=headers, internal=True)
        assert rejected.status == 400


def test_login_rate_limit_rejects_excess_without_limiting_other_api(
    gateway: GatewayHarness,
) -> None:
    login_responses = [
        gateway.request("/api/v1/auth/login", method="POST") for _ in range(25)
    ]
    rejected = next(
        response for response in login_responses if response.status in {429, 503}
    )

    assert any(response.status == 200 for response in login_responses)
    assert_security_headers(rejected)
    assert_private_no_store(rejected)
    assert gateway.request("/api/not-login", method="POST").status == 200


def test_admin_login_rate_limit_is_exact_and_other_admin_routes_are_unlimited(
    gateway: GatewayHarness,
) -> None:
    login_responses = [
        gateway.request("/admin/login/", method="POST") for _ in range(25)
    ]
    rejected = next(
        response for response in login_responses if response.status in {429, 503}
    )

    assert any(response.status == 200 for response in login_responses)
    assert_security_headers(rejected)
    assert_private_no_store(rejected)
    assert SAFE_REQUEST_ID.fullmatch(rejected.headers["X-Request-ID"])
    assert gateway.request("/admin/auth/group/", method="POST").status == 200


def test_admin_proxy_is_private_and_preserves_bounded_request_identity(
    gateway: GatewayHarness,
) -> None:
    request_id = "AdminRequest_1234"
    response = gateway.request("/admin/", headers={"X-Request-ID": request_id})

    assert response.status == 200
    assert response.json()["request_id"] == request_id
    assert response.headers["X-Request-ID"] == request_id
    assert_security_headers(response)
    assert_private_no_store(response)


def test_only_collected_admin_static_subtree_is_public(gateway: GatewayHarness) -> None:
    asset = gateway.request("/admin-static/admin/css/base.css")
    outside = gateway.request("/admin-static/not-admin.css")

    assert asset.status == 200
    assert asset.body == b"admin-css"
    assert_security_headers(asset)
    assert outside.status == 404
    assert_security_headers(outside)


@pytest.mark.parametrize("path", ["/api/headers", "/health/live"])
def test_proxied_responses_have_security_and_private_cache_headers(
    gateway: GatewayHarness, path: str
) -> None:
    response = gateway.request(path)

    assert response.status == 200
    assert response.json()["path"] == path
    assert_security_headers(response)
    assert_private_no_store(response)


def test_spa_shell_and_fallback_are_private_no_store(gateway: GatewayHarness) -> None:
    for path in ("/", "/client/route"):
        response = gateway.request(path)

        assert response.status == 200
        assert response.body == b"spa-shell"
        assert_security_headers(response)
        assert_private_no_store(response)


def test_hashed_asset_is_cached_immutably(gateway: GatewayHarness) -> None:
    response = gateway.request("/assets/app-abcdefgh.js")

    assert response.status == 200
    assert response.body == b"asset-body"
    assert_security_headers(response)
    assert cache_control_directives(response) == {
        "public",
        "max-age=31536000",
        "immutable",
    }


@pytest.mark.parametrize("path", ["/__aegis_roots/file", "/__aegis_derivatives/file"])
def test_protected_aliases_cannot_be_requested_directly(
    gateway: GatewayHarness, path: str
) -> None:
    response = gateway.request(path)

    assert response.status == 404
    assert_security_headers(response)


def test_every_gateway_log_line_is_bounded_json_without_canaries(
    gateway: GatewayHarness,
) -> None:
    request_canary = "nginx-credential-canary"
    header_canary = "invalid credential canary"
    filesystem_canary = "filesystem-secret-canary"

    response = gateway.request(
        f"/admin-static/admin/{filesystem_canary}/{request_canary}"
        f"?credentials={request_canary}",
        headers={"X-Request-ID": header_canary},
    )
    logs = docker("logs", gateway.container_name)
    rendered = f"{logs.stdout}\n{logs.stderr}"
    lines = [line for line in rendered.splitlines() if line]

    assert response.status == 404
    assert request_canary not in rendered
    assert header_canary not in rendered
    assert filesystem_canary not in rendered
    assert lines
    for line in lines:
        assert len(line.encode("utf-8")) < 512
        payload = json.loads(line)
        assert set(payload) == {
            "timestamp",
            "level",
            "logger",
            "message",
            "request_id",
            "status",
        }
        assert payload["level"] == "INFO"
        assert payload["logger"] == "nginx.access"
        assert payload["message"] == "HTTP request completed"
