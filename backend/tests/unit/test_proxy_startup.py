from __future__ import annotations

import socket

import pytest
from aegis.proxy import ProxyTrustError, main, resolve_trusted_proxy_ips


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


def test_proxy_start_execs_uvicorn_with_only_resolved_and_loopback_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def capture(executable: str, arguments: list[str]) -> None:
        captured["executable"] = executable
        captured["arguments"] = arguments
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(
        "aegis.proxy.resolve_trusted_proxy_ips",
        lambda: ("127.0.0.1", "172.28.0.9"),
    )
    monkeypatch.setattr("aegis.proxy.os.execvp", capture)

    with pytest.raises(RuntimeError, match="exec intercepted"):
        main()

    assert captured["executable"] == "uvicorn"
    assert captured["arguments"] == [
        "uvicorn",
        "aegis.asgi:application",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--proxy-headers",
        "--forwarded-allow-ips",
        "127.0.0.1,172.28.0.9",
        "--log-config",
        "/app/backend/aegis/uvicorn_logging.json",
    ]
