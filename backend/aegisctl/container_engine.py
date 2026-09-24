"""Validated local container-engine commands for deployment tooling."""
from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

ENGINE_VARIABLE = "AEGIS_CONTAINER_ENGINE"
PROVIDER_VARIABLE = "PODMAN_COMPOSE_PROVIDER"
SOCKET_VARIABLE = "AEGIS_PODMAN_SOCKET"
REMOTE_VARIABLES = frozenset({
    "DOCKER_HOST", "DOCKER_CONTEXT", "CONTAINER_HOST", "CONTAINER_CONNECTION",
})


class ContainerEngineError(ValueError):
    """The selected local engine or Compose provider is unsafe or invalid."""


def selected_engine(environment: Mapping[str, str] | None = None) -> str:
    source = os.environ if environment is None else environment
    value = source.get(ENGINE_VARIABLE, "docker")
    if value not in ("docker", "podman"):
        raise ContainerEngineError(
            f"{ENGINE_VARIABLE} must be exactly 'docker' or 'podman'"
        )
    if value == "podman":
        podman_compose_provider(source)
        podman_socket(source)
    return value


def podman_compose_provider(environment: Mapping[str, str] | None = None) -> Path:
    source = os.environ if environment is None else environment
    raw = source.get(PROVIDER_VARIABLE, "")
    path = Path(raw)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ContainerEngineError(
            f"{PROVIDER_VARIABLE} must name an owned absolute executable"
        ) from exc
    if (
        not path.is_absolute()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or not metadata.st_mode & stat.S_IXUSR
        or metadata.st_mode & 0o022
    ):
        raise ContainerEngineError(
            f"{PROVIDER_VARIABLE} must name an owned absolute executable"
        )
    return path


def podman_socket(environment: Mapping[str, str] | None = None) -> Path:
    source = os.environ if environment is None else environment
    raw = source.get(SOCKET_VARIABLE, "")
    path = Path(raw)
    error = f"{SOCKET_VARIABLE} must name an owned local socket in a private directory"
    if (
        os.geteuid() == 0
        or not path.is_absolute()
        or any(part in (".", "..") for part in path.parts)
    ):
        raise ContainerEngineError(error)
    try:
        metadata = path.lstat()
        parent_metadata = path.parent.lstat()
    except OSError as exc:
        raise ContainerEngineError(error) from exc
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
    ):
        raise ContainerEngineError(error)

    ancestor = path.parent.parent
    while True:
        try:
            ancestor_metadata = ancestor.lstat()
        except OSError as exc:
            raise ContainerEngineError(error) from exc
        if not stat.S_ISDIR(ancestor_metadata.st_mode):
            raise ContainerEngineError(error)
        writable_by_others = bool(ancestor_metadata.st_mode & 0o022)
        trusted_sticky_root = (
            ancestor_metadata.st_uid == 0
            and bool(ancestor_metadata.st_mode & stat.S_ISVTX)
        )
        if (
            writable_by_others
            and ancestor_metadata.st_uid != os.geteuid()
            and not trusted_sticky_root
        ):
            raise ContainerEngineError(error)
        if ancestor == ancestor.parent:
            break
        ancestor = ancestor.parent
    return path


def container_command(
    *arguments: str, environment: Mapping[str, str] | None = None,
) -> list[str]:
    engine = selected_engine(environment)
    prefix = [engine, "--remote=false"] if engine == "podman" else [engine]
    return [*prefix, *arguments]


def compose_command(
    *arguments: str, environment: Mapping[str, str] | None = None,
) -> list[str]:
    engine = selected_engine(environment)
    prefix = [engine, "--remote=false"] if engine == "podman" else [engine]
    return [*prefix, "compose", *arguments]


def sanitized_environment(environment: Mapping[str, str]) -> dict[str, str]:
    engine = selected_engine(environment)
    sanitized = {
        key: value
        for key, value in environment.items()
        if not key.startswith(("AEGIS_", "E2E_", "COMPOSE_", "DJANGO_", "PG", "PODMAN_"))
        and key not in REMOTE_VARIABLES
    }
    sanitized[ENGINE_VARIABLE] = engine
    if engine == "podman":
        sanitized[PROVIDER_VARIABLE] = str(podman_compose_provider(environment))
        sanitized[SOCKET_VARIABLE] = str(podman_socket(environment))
    return sanitized


def local_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Preserve command inputs while removing inherited remote-engine routing."""
    selected_engine(environment)
    return {key: value for key, value in environment.items() if key not in REMOTE_VARIABLES}


def compose_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Route Podman Compose only through its validated temporary Unix socket."""
    engine = selected_engine(environment)
    sanitized = local_environment(environment)
    if engine == "podman":
        sanitized["DOCKER_HOST"] = f"unix://{podman_socket(environment)}"
    return sanitized
