from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

START_SCRIPT = Path(__file__).resolve().parents[2] / "deploy/nginx/start-gateway.sh"


def _write_command(directory: Path, name: str, body: str) -> None:
    command = directory / name
    command.write_text(f"#!/bin/sh\nset -eu\n{body}\n", encoding="utf-8")
    command.chmod(0o755)


def test_gateway_waits_for_upstream_health_before_starting_nginx(tmp_path: Path) -> None:
    commands = tmp_path / "bin"
    commands.mkdir()
    attempts = tmp_path / "attempts"
    wget_call = tmp_path / "wget-call"
    nginx_call = tmp_path / "nginx-call"
    _write_command(
        commands,
        "wget",
        (
            f"count=$(test -f {attempts} && cat {attempts} || echo 0)\n"
            "count=$((count + 1))\n"
            f"printf '%s' \"$count\" > {attempts}\n"
            f"printf '%s' \"$*\" > {wget_call}\n"
            "test \"$count\" -ge 3"
        ),
    )
    _write_command(commands, "sleep", "exit 0")
    _write_command(commands, "nginx", f"printf '%s\\n' \"$*\" > {nginx_call}")

    result = subprocess.run(
        ["/bin/sh", START_SCRIPT],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ
        | {
            "PATH": f"{commands}:/bin:/usr/bin",
            "AEGIS_PUBLIC_URL": "https://public.example.test",
            "AEGIS_GATEWAY_READY_FILE": str(tmp_path / "ready"),
        },
    )

    assert result.returncode == 0
    assert attempts.read_text(encoding="utf-8") == "3"
    wget_arguments = wget_call.read_text(encoding="utf-8")
    assert "X-Aegis-Proxy-Attestation: startup-v1" in wget_arguments
    assert "X-Forwarded-For: 192.0.2.254" in wget_arguments
    assert "http://web:8000/health/proxy-attestation" in wget_arguments
    assert nginx_call.read_text(encoding="utf-8").strip() == "-g daemon off;"


def test_gateway_wait_is_bounded_and_failure_log_is_safe_json(tmp_path: Path) -> None:
    commands = tmp_path / "bin"
    commands.mkdir()
    nginx_call = tmp_path / "nginx-call"
    attempts = tmp_path / "attempts"
    ready_file = tmp_path / "ready"
    ready_file.touch()
    _write_command(
        commands,
        "wget",
        (
            f"count=$(test -f {attempts} && cat {attempts} || echo 0)\n"
            "count=$((count + 1))\n"
            f"printf '%s' \"$count\" > {attempts}\n"
            "exit 1"
        ),
    )
    _write_command(commands, "sleep", "exit 0")
    _write_command(commands, "nginx", f"touch {nginx_call}")

    result = subprocess.run(
        ["/bin/sh", START_SCRIPT],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ
        | {
            "PATH": f"{commands}:/bin:/usr/bin",
            "AEGIS_PUBLIC_URL": "https://sensitive-host-canary.example",
            "AEGIS_GATEWAY_READY_FILE": str(ready_file),
            "AEGIS_GATEWAY_ATTESTATION_ATTEMPTS": "3",
        },
    )

    assert result.returncode == 1
    assert not nginx_call.exists()
    assert not ready_file.exists()
    assert attempts.read_text(encoding="utf-8") == "3"
    log = json.loads(result.stderr)
    timestamp = log.pop("timestamp")
    assert isinstance(timestamp, str)
    assert log == {
        "level": "ERROR",
        "logger": "gateway.startup",
        "message": "Upstream health check failed",
    }
    assert "sensitive-host-canary" not in result.stderr


def test_gateway_rejects_invalid_or_excessive_attempt_override(tmp_path: Path) -> None:
    commands = tmp_path / "bin"
    commands.mkdir()
    wget_call = tmp_path / "wget-call"
    _write_command(commands, "wget", f"touch {wget_call}")
    _write_command(commands, "sleep", "exit 0")
    _write_command(commands, "nginx", "exit 0")

    for value in ("0", "41", "not-a-number"):
        result = subprocess.run(
            ["/bin/sh", START_SCRIPT],
            check=False,
            capture_output=True,
            text=True,
            env=os.environ
            | {
                "PATH": f"{commands}:/bin:/usr/bin",
                "AEGIS_PUBLIC_URL": "https://public.example.test",
                "AEGIS_GATEWAY_READY_FILE": str(tmp_path / "ready"),
                "AEGIS_GATEWAY_ATTESTATION_ATTEMPTS": value,
            },
        )

        assert result.returncode == 1
        assert not wget_call.exists()
        log = json.loads(result.stderr)
        assert log["message"] == "Gateway startup configuration invalid"
