from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

UVICORN_LOG_CONFIG = Path(__file__).parents[3] / "aegis" / "uvicorn_logging.json"
BACKEND_DIR = Path(__file__).parents[3]


def _unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_uvicorn_log_config_routes_access_and_error_before_app_start() -> None:
    value = json.loads(UVICORN_LOG_CONFIG.read_text(encoding="utf-8"))

    assert value["formatters"]["aegis_json"]["()"] == (
        "aegis_apps.common.logging.BoundedJSONFormatter"
    )
    assert value["loggers"]["uvicorn.access"] == {
        "handlers": ["stdout"],
        "level": "INFO",
        "propagate": False,
    }
    assert value["loggers"]["uvicorn.error"] == {
        "handlers": ["stderr"],
        "level": "INFO",
        "propagate": False,
    }


def test_real_uvicorn_cli_never_logs_request_canary() -> None:
    port = _unused_port()
    canary = "runtime-credential-canary"
    environment = {
        **os.environ,
        "AEGIS_ENV": "test",
        "AEGIS_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "DJANGO_SETTINGS_MODULE": "aegis.settings.test",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "aegis.asgi:application",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-config",
            str(UVICORN_LOG_CONFIG),
        ],
        cwd=BACKEND_DIR,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health/live?credential={canary}", timeout=1
                ) as response:
                    assert response.status == 200
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
    finally:
        process.terminate()
        stdout, stderr = process.communicate(timeout=10)

    rendered = [line for line in f"{stdout}\n{stderr}".splitlines() if line]
    assert rendered
    assert all(isinstance(json.loads(line), dict) for line in rendered)
    assert all(len(line.encode("utf-8")) <= 64 * 1024 for line in rendered)
    assert canary not in "\n".join(rendered)
