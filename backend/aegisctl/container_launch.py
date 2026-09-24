"""Controlled Podman workload launches; raw inspection and cleanup remain pure."""

from __future__ import annotations

import collections
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from aegisctl.container_engine import (
    compose_command,
    compose_environment,
    container_command,
    selected_engine,
)
from aegisctl.podman_mask_compatibility import (
    MASK_OPTION,
    PodmanMaskError,
    _run,
    require_podman_mask_compatibility,
)

CANONICAL_SERVICES = frozenset(
    {
        "postgres",
        "migrate",
        "web",
        "operations",
        "indexer",
        "media",
        "gateway",
        "caddy",
        "caddy-local",
    }
)
_NATIVE_FLAGS = frozenset(
    {
        "--rm",
        "--read-only",
        "--init",
        "--interactive",
        "--detach",
        "--tty",
        "--privileged",
        "-i",
        "-t",
        "-d",
        "-it",
    }
)
_NATIVE_VALUES = frozenset(
    {
        "--name",
        "--label",
        "--cidfile",
        "--pull",
        "--network",
        "--network-alias",
        "--ip",
        "--user",
        "--group-add",
        "--cap-drop",
        "--cap-add",
        "--security-opt",
        "--memory",
        "--cpus",
        "--pids-limit",
        "--timeout",
        "--entrypoint",
        "--env",
        "--env-file",
        "--mount",
        "--volume",
        "--tmpfs",
        "--publish",
        "--workdir",
        "--add-host",
        "--hostname",
        "--ulimit",
        "-e",
        "-v",
        "-p",
        "-u",
        "-w",
    }
)
_COMPOSE_VALUES = frozenset(
    {
        "-f",
        "--file",
        "-p",
        "--project-name",
        "--project-directory",
        "--env-file",
        "--profile",
        "--parallel",
        "--progress",
        "--ansi",
    }
)
_COMPOSE_FLAGS = frozenset({"--compatibility", "--dry-run", "--all-resources"})
_RUNTIME = frozenset({"up", "run", "create", "start", "restart"})


def _native_inputs(
    arguments: Sequence[str],
    *,
    compose: bool = False,
    working_directory: Path | None = None,
) -> tuple[tuple[Path, ...], bool]:
    flags = _NATIVE_FLAGS | (
        {
            "--no-deps",
            "--no-TTY",
            "-T",
            "--build",
            "--use-aliases",
            "--service-ports",
            "--quiet-pull",
            "--remove-orphans",
        }
        if compose
        else set()
    )
    roots: list[Path] = []
    exact_options = 0
    index = 1
    while index < len(arguments):
        token = arguments[index]
        if token == "--":
            index += 1
            break
        if not token.startswith("-"):
            break
        name, separator, value = token.partition("=")
        if name == "--privileged":
            # Explicit false is also refused until its provider semantics are proved.
            raise PodmanMaskError("privileged options are not admitted by the mask policy")
        if name in flags:
            if separator and value not in ("true", "false"):
                raise PodmanMaskError("invalid controlled boolean option")
            index += 1
            continue
        if name not in _NATIVE_VALUES:
            raise PodmanMaskError("unsupported controlled native option")
        if not separator:
            index += 1
            if index >= len(arguments):
                raise PodmanMaskError("native option value missing")
            value = arguments[index]
        if name == "--security-opt":
            if value == MASK_OPTION:
                exact_options += 1
            elif value.startswith(("mask=", "mask:", "unmask=", "unmask:", "systempaths=")):
                raise PodmanMaskError("conflicting mask option")
        elif name in ("--volume", "-v"):
            source = value.split(":", 1)[0]
            if source.startswith(("/", ".")):
                path = Path(source)
                if not path.is_absolute():
                    if compose:
                        raise PodmanMaskError("relative Compose bind context is not proved")
                    directory = Path.cwd() if working_directory is None else working_directory
                    path = Path(os.path.abspath(directory / path))
                roots.append(path)
        elif name == "--mount":
            fields = dict(part.split("=", 1) for part in value.split(",") if "=" in part)
            if fields.get("type") == "bind":
                source = fields.get("source", fields.get("src", ""))
                if not source or not Path(source).is_absolute():
                    raise PodmanMaskError("controlled bind requires an absolute source")
                roots.append(Path(source))
        index += 1
    if index >= len(arguments) or exact_options > 1:
        raise PodmanMaskError("missing image or duplicate mask policy")
    return tuple(roots), bool(exact_options)


def _compose_split(arguments: Sequence[str]) -> tuple[list[str], list[str]]:
    index = 0
    while index < len(arguments):
        name, separator, _ = arguments[index].partition("=")
        if name in _COMPOSE_FLAGS:
            index += 1
        elif name in _COMPOSE_VALUES:
            if not separator and index + 1 >= len(arguments):
                raise PodmanMaskError("Compose option value missing")
            index += 1 if separator else 2
        elif arguments[index].startswith("-"):
            raise PodmanMaskError("unsupported controlled Compose option")
        else:
            return list(arguments[:index]), list(arguments[index:])
    return list(arguments), []


def _compose_files(options: Sequence[str], directory: Path) -> list[tuple[int, int, Path | None]]:
    """Return exact option spans and resolved file identities, skipping other values."""
    files: list[tuple[int, int, Path | None]] = []
    index = 0
    while index < len(options):
        name, separator, value = options[index].partition("=")
        count = 2 if name in _COMPOSE_VALUES and not separator else 1
        if name in ("-f", "--file"):
            if not separator:
                value = options[index + 1]
            path = (directory / value).resolve() if value and value != "-" else None
            files.append((index, index + count, path))
        index += count
    return files


def canonical_compose_arguments(
    arguments: Sequence[str],
    canonical_base: Path,
    environment: Mapping[str, str] | None = None,
    *,
    working_directory: Path | None = None,
) -> list[str]:
    """Pure explicit canonical-stack file selection, after existing overlays."""
    source = os.environ if environment is None else environment
    if selected_engine(source) == "docker":
        return list(arguments)
    options, command = _compose_split(arguments)
    directory = Path.cwd() if working_directory is None else Path(working_directory)
    entries = _compose_files(options, directory)
    files = [path for _, _, path in entries]
    base = (directory / canonical_base).resolve()
    if base not in files:
        return list(arguments)
    if None in files:
        raise PodmanMaskError("ambiguous Compose file identity")
    if len(set(files)) != len(files):
        raise PodmanMaskError("duplicate Compose file identity")
    normalized: list[str] = []
    cursor = 0
    for start, end, path in entries:
        normalized.extend((*options[cursor:start], "-f", str(path)))
        cursor = end
    normalized.extend(options[cursor:])
    override = base.with_name("compose.podman.yaml")
    if override in files:
        if files[-1] != override:
            raise PodmanMaskError("Podman policy overlay must follow known overlays")
        return [*normalized, *command]
    return [*normalized, "-f", str(override), *command]


def _render(
    options: Sequence[str],
    environment: Mapping[str, str],
    working_directory: Path | None = None,
) -> dict[str, Any]:
    result = _run(
        compose_command(*options, "config", "--format", "json", environment=environment),
        compose_environment(environment),
        cwd=working_directory,
    )
    if result.returncode:
        raise PodmanMaskError("controlled Compose render failed")
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or not isinstance(value.get("services"), dict):
        raise PodmanMaskError("invalid controlled Compose render")
    return value


def _checked_compose_inputs(
    original: list[str],
    selected: list[str],
    environment: Mapping[str, str],
    working_directory: Path | None = None,
) -> tuple[Path, ...]:
    before = _render(original, environment, working_directory)["services"]
    after = _render(selected, environment, working_directory)["services"]
    if set(before) != set(after) or not set(after) <= CANONICAL_SERVICES:
        raise PodmanMaskError("Podman overlay changed the canonical service set")
    roots: list[Path] = []
    for name, service in after.items():
        if service.get("privileged", False) is not False:
            raise PodmanMaskError("effective privileged service is not admitted")
        options = service.get("security_opt", [])
        original_options = before[name].get("security_opt", [])
        expected_options = [*original_options]
        if MASK_OPTION not in expected_options:
            expected_options.append(MASK_OPTION)
        if collections.Counter(options) != collections.Counter(expected_options):
            raise PodmanMaskError("Podman overlay changed existing security options")
        before_settings = {
            key: value for key, value in before[name].items() if key != "security_opt"
        }
        after_settings = {key: value for key, value in service.items() if key != "security_opt"}
        if before_settings != after_settings:
            raise PodmanMaskError("Podman overlay changed existing service settings")
        if options.count(MASK_OPTION) != 1 or not any(
            option in ("no-new-privileges:true", "no-new-privileges=true", "no-new-privileges")
            for option in options
        ):
            raise PodmanMaskError("effective Compose mask or NNP protection missing")
        if any(
            option != MASK_OPTION and option.startswith(("mask=", "unmask=", "systempaths="))
            for option in options
        ):
            raise PodmanMaskError("conflicting effective Compose mask policy")
        if service.get("profiles", []) != before[name].get("profiles", []):
            raise PodmanMaskError("Podman overlay changed service profiles")
        for mount in service.get("volumes", []):
            if mount.get("type") == "bind":
                roots.append(Path(mount["source"]))
    return tuple(roots)


def controlled_container_argv(
    command: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    canonical_base: Path | None = None,
    working_directory: Path | None = None,
) -> list[str]:
    """Gate complete native run/create or explicitly selected canonical Compose argv."""
    source = os.environ if environment is None else environment
    if list(command[:2]) == ["podman", "--remote=false"]:
        raw_arguments = list(command[2:])
        if not raw_arguments or raw_arguments[0] not in ("run", "create", "compose"):
            return list(command)
        if raw_arguments[0] == "compose":
            if canonical_base is None:
                return list(command)
            _, raw_action = _compose_split(raw_arguments[1:])
            if raw_action and raw_action[0] not in _RUNTIME | {"config", "build"}:
                return list(command)
    engine = selected_engine(source)
    if engine == "docker":
        if command and command[0] == "podman":
            raise PodmanMaskError("controlled launch engine selection differs")
        return list(command)
    prefix = container_command(environment=source)
    if list(command[: len(prefix)]) != prefix:
        raise PodmanMaskError("controlled launch engine prefix differs")
    arguments = list(command[len(prefix) :])
    if not arguments:
        return list(command)
    if arguments[0] in ("run", "create"):
        roots, supplied = _native_inputs(arguments, working_directory=working_directory)
        options = require_podman_mask_compatibility(source, originals=roots)
        insertion = (
            [] if supplied else [item for option in options for item in ("--security-opt", option)]
        )
        return [*prefix, arguments[0], *insertion, *arguments[1:]]
    if arguments[0] == "compose" and canonical_base is not None:
        original, action = _compose_split(arguments[1:])
        selected = canonical_compose_arguments(
            original,
            canonical_base,
            source,
            working_directory=working_directory,
        )
        directory = Path.cwd() if working_directory is None else working_directory
        base = (directory / canonical_base).resolve()
        files = _compose_files(selected, directory)
        overlay = [span for span in files if span[2] == base.with_name("compose.podman.yaml")]
        canonical = any(path == base for _, _, path in files)
        if action and action[0] in _RUNTIME and canonical:
            if len(overlay) != 1:
                raise PodmanMaskError("ambiguous Podman policy overlay")
            start, end, _ = overlay[0]
            without = [*selected[:start], *selected[end:]]
            extra_roots: tuple[Path, ...] = ()
            if action[0] == "run":
                extra_roots, _ = _native_inputs(action, compose=True)
            roots = _checked_compose_inputs(without, selected, source, working_directory)
            roots = (*roots, *extra_roots)
            require_podman_mask_compatibility(source, originals=roots)
        return [*prefix, "compose", *selected, *action]
    return list(command)
