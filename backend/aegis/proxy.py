from __future__ import annotations

import ipaddress
import os
import socket
from typing import NoReturn


class ProxyTrustError(RuntimeError):
    """Raised when the sole trusted gateway peer cannot be identified safely."""


def resolve_trusted_proxy_ips() -> tuple[str, str]:
    try:
        answers = socket.getaddrinfo("gateway", 8000, type=socket.SOCK_STREAM)
        addresses = {ipaddress.ip_address(answer[4][0]) for answer in answers}
    except (OSError, ValueError, TypeError, IndexError):
        raise ProxyTrustError("trusted gateway peer resolution failed") from None
    if len(addresses) != 1:
        raise ProxyTrustError("trusted gateway peer resolution failed")
    gateway_address = next(iter(addresses))
    if (
        gateway_address.is_loopback
        or gateway_address.is_multicast
        or gateway_address.is_unspecified
    ):
        raise ProxyTrustError("trusted gateway peer resolution failed")
    return ("127.0.0.1", str(gateway_address))


def main() -> NoReturn:
    trusted_proxy_ips = ",".join(resolve_trusted_proxy_ips())
    arguments = [
        "uvicorn",
        "aegis.asgi:application",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--proxy-headers",
        "--forwarded-allow-ips",
        trusted_proxy_ips,
        "--log-config",
        "/app/backend/aegis/uvicorn_logging.json",
    ]
    os.execvp(arguments[0], arguments)
    raise RuntimeError("unreachable")


if __name__ == "__main__":
    main()
