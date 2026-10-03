from __future__ import annotations

import os
import socket

import pytest
from aegis import proxy
from aegis.config import DEFAULT_WEB_WORKERS, ConfigurationError
from aegis.proxy import ProxyTrustError, main, resolve_trusted_proxy_ips
from django.test import override_settings


def _address(value: str) -> tuple[int, int, int, str, tuple[str, int]]:
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (value, 8000))


def test_proxy_trust_contains_only_loopback_and_exact_gateway_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [_address("172.28.0.9"), _address("172.28.0.9")],
    )

    assert resolve_trusted_proxy_ips() == ("127.0.0.1", "172.28.0.9")


@pytest.mark.parametrize(
    "answers",
    [
        [],
        [_address("172.28.0.9"), _address("172.28.0.10")],
        [_address("gateway")],
    ],
)
def test_proxy_trust_fails_closed_on_ambiguous_or_invalid_gateway_resolution(
    monkeypatch: pytest.MonkeyPatch,
    answers: list[tuple[int, int, int, str, tuple[str, int]]],
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: answers)

    with pytest.raises(ProxyTrustError, match="trusted gateway peer resolution failed"):
        resolve_trusted_proxy_ips()


def _capture_exec(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]
) -> None:
    def capture(executable: str, arguments: list[str], environment: dict[str, str]) -> None:
        captured["executable"] = executable
        captured["arguments"] = arguments
        captured["environment"] = environment
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(
        "aegis.proxy.resolve_trusted_proxy_ips",
        lambda: ("127.0.0.1", "172.28.0.9"),
    )
    monkeypatch.setattr("aegis.proxy.os.execvpe", capture)


def _uvicorn_arguments(workers: int) -> list[str]:
    return [
        "uvicorn",
        "aegis.asgi:application",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--proxy-headers",
        "--forwarded-allow-ips",
        "127.0.0.1,172.28.0.9",
        "--workers",
        str(workers),
        "--log-config",
        "/app/backend/aegis/uvicorn_logging.json",
    ]


def test_proxy_start_execs_uvicorn_with_only_resolved_and_loopback_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    _capture_exec(monkeypatch, captured)
    monkeypatch.delenv("AEGIS_WEB_WORKERS", raising=False)
    monkeypatch.setenv("WEB_CONCURRENCY", "11")

    with pytest.raises(RuntimeError, match="exec intercepted"):
        main()

    assert captured["executable"] == "uvicorn"
    # The worker count is always explicit, so uvicorn never falls back to an
    # inherited, unvalidated WEB_CONCURRENCY.
    assert captured["arguments"] == _uvicorn_arguments(DEFAULT_WEB_WORKERS)
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert environment["AEGIS_WEB_DATABASE_POOL"] == "enabled"
    assert "WEB_CONCURRENCY" not in environment


def test_proxy_start_uses_the_validated_configured_worker_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    _capture_exec(monkeypatch, captured)
    monkeypatch.setenv("AEGIS_WEB_WORKERS", "2")

    with pytest.raises(RuntimeError, match="exec intercepted"):
        main()

    assert captured["arguments"] == _uvicorn_arguments(2)


@override_settings(AEGIS_ENVIRONMENT="production")
def test_invalid_worker_count_prevents_database_dns_and_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*_arguments: object) -> None:
        raise AssertionError("startup must stop at configuration")

    monkeypatch.setenv("AEGIS_WEB_WORKERS", "1000")
    monkeypatch.setattr(proxy, "require_runtime_database_login", unexpected, raising=False)
    monkeypatch.setattr(proxy, "resolve_trusted_proxy_ips", unexpected)
    monkeypatch.setattr(os, "execvpe", unexpected)

    with pytest.raises(ConfigurationError, match="AEGIS_WEB_WORKERS"):
        main()


@override_settings(AEGIS_ENVIRONMENT="production")
def test_proxy_verifies_web_database_login_before_dns_and_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def verify(role: str) -> None:
        events.append(f"database:{role}")

    def resolve() -> tuple[str, str]:
        events.append("proxy")
        return ("127.0.0.1", "172.28.0.9")

    def capture(
        _executable: str, _arguments: list[str], _environment: dict[str, str]
    ) -> None:
        events.append("listener")
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(proxy, "require_runtime_database_login", verify, raising=False)
    monkeypatch.setattr(proxy, "resolve_trusted_proxy_ips", resolve)
    monkeypatch.setattr(os, "execvpe", capture)

    with pytest.raises(RuntimeError, match="exec intercepted"):
        main()

    assert events == ["database:web", "proxy", "listener"]


@override_settings(AEGIS_ENVIRONMENT="production")
def test_proxy_database_login_mismatch_prevents_dns_and_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(_role: str) -> None:
        raise RuntimeError("runtime database login does not match")

    def unexpected_resolve() -> tuple[str, str]:
        raise AssertionError("proxy DNS must not run")

    def unexpected_exec(
        _executable: str, _arguments: list[str], _environment: dict[str, str]
    ) -> None:
        raise AssertionError("listener must not start")

    monkeypatch.setattr(proxy, "require_runtime_database_login", reject, raising=False)
    monkeypatch.setattr(proxy, "resolve_trusted_proxy_ips", unexpected_resolve)
    monkeypatch.setattr(os, "execvpe", unexpected_exec)

    with pytest.raises(RuntimeError, match="runtime database login does not match"):
        main()
