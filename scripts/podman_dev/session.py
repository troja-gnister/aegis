"""State paths, preflight and shell exports for one Podman development session."""
from __future__ import annotations

import os
import shlex
import stat
from collections.abc import Mapping
from pathlib import Path

from scripts.podman_dev.records import SessionError, SessionRecord

STATE_NAME = "aegis-podman-dev"
BASELINE_NAME = "inventory-before.json"
LOG_NAME = "podman-api.log"
REMOTE_VARIABLES = ("DOCKER_HOST", "DOCKER_CONTEXT", "CONTAINER_HOST", "CONTAINER_CONNECTION")


def state_directory(environ: Mapping[str, str]) -> Path:
    runtime = environ.get("XDG_RUNTIME_DIR", "")
    if not runtime or not Path(runtime).is_absolute():
        raise SessionError("XDG_RUNTIME_DIR must be set to an absolute path")
    return Path(runtime) / STATE_NAME


def preflight_problems(
    environ: Mapping[str, str], *, euid: int, rootless: bool, selinux_enforce: str,
) -> list[str]:
    problems: list[str] = []
    if euid == 0:
        problems.append("refusing to run as root")
    if not rootless:
        problems.append("Podman must run rootless")
    if selinux_enforce.strip() != "1":
        problems.append("SELinux must be enforcing")
    remote = [name for name in REMOTE_VARIABLES if name in environ]
    if remote:
        problems.append("remote engine variables are set: " + ", ".join(remote))
    runtime = environ.get("XDG_RUNTIME_DIR", "")
    try:
        metadata = os.lstat(runtime) if runtime and Path(runtime).is_absolute() else None
    except OSError:
        metadata = None
    if (
        metadata is None
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != euid
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        problems.append("XDG_RUNTIME_DIR must be an absolute private (0700) directory you own")
    elif os.path.lexists(Path(runtime) / STATE_NAME):
        problems.append(f"a previous session exists in {Path(runtime) / STATE_NAME}; "
                        "run stop first")
    return problems


def export_lines(record: SessionRecord, *, node_bin: Path, compose_provider: Path) -> str:
    if record.api is None or record.webkit is None:
        raise SessionError("the session is incomplete; run stop, then start")
    values = [
        ("AEGIS_CONTAINER_ENGINE", "podman"),
        ("PODMAN_COMPOSE_PROVIDER", str(compose_provider)),
        ("AEGIS_PODMAN_SOCKET", record.api.socket),
        ("E2E_WEBKIT_WS_ENDPOINT", record.webkit.endpoint),
    ]
    lines = [f"export {name}={shlex.quote(value)}" for name, value in values]
    lines.append(f'export PATH={shlex.quote(str(node_bin))}:"$PATH"')
    return "\n".join(lines) + "\n"
