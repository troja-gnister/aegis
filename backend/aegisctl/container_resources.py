"""Exact identity tracking for disposable Compose project resources."""
from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from aegisctl.container_engine import (
    ContainerEngineError,
    container_command,
    local_environment,
    selected_engine,
)
from aegisctl.container_network import network_identity_fields

ResourceRunner = Callable[[list[str], Mapping[str, str]], subprocess.CompletedProcess[str]]


class ProjectResourceError(RuntimeError):
    """A disposable project resource could not be proved unchanged and owned."""


@dataclass(frozen=True, order=True)
class ProjectResource:
    kind: str
    handle: str
    immutable_id: str
    fingerprint: str


@dataclass(frozen=True)
class ProjectInventory:
    project: str
    resources: tuple[ProjectResource, ...]


@dataclass(frozen=True)
class ProjectResourceRule:
    """One resource that a bounded operation may create at most once."""

    kind: str
    required_labels: tuple[tuple[str, str], ...]
    name: str | None = None


def _run(arguments: list[str], environment: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments, env=local_environment(environment), check=False,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectResourceError("project resource query failed") from exc


def _inspect_fields(
    kind: str, info: dict[str, Any], engine: str,
) -> tuple[str, str, dict[str, Any]]:
    if kind == "container":
        labels = info.get("Config", {}).get("Labels") or {}
        immutable_id = info.get("Id")
        fields = {
            "Id": immutable_id, "Created": info.get("Created"),
            "Name": info.get("Name"), "Image": info.get("Image"), "Labels": labels,
        }
    elif kind == "network":
        try:
            fields = network_identity_fields(info, engine)
        except ContainerEngineError as exc:
            raise ProjectResourceError("project network inspection failed") from exc
        labels = fields["Labels"]
        immutable_id = fields["Id"]
    else:
        labels = info.get("Labels") or {}
        immutable_id = info.get("Name")
        fields = {
            "Name": immutable_id, "CreatedAt": info.get("CreatedAt"),
            "Driver": info.get("Driver"), "Mountpoint": info.get("Mountpoint"),
            "Options": info.get("Options") or {}, "Scope": info.get("Scope"),
            "Labels": labels,
        }
        if not isinstance(info.get("CreatedAt"), str) or not info["CreatedAt"]:
            raise ProjectResourceError("project resource identity is invalid")
    if not isinstance(immutable_id, str) or not immutable_id:
        raise ProjectResourceError("project resource identity is invalid")
    owned_project = labels.get("com.docker.compose.project")
    if not isinstance(owned_project, str):
        raise ProjectResourceError("project resource ownership changed")
    return immutable_id, owned_project, fields


def capture_project_inventory(
    project: str, environment: Mapping[str, str] | None = None,
    *, runner: ResourceRunner | None = None,
) -> ProjectInventory:
    source = os.environ if environment is None else environment
    execute = _run if runner is None else runner
    label = f"label=com.docker.compose.project={project}"
    resources: list[ProjectResource] = []
    queries = {
        "container": ("container", "ls", "--all", "--quiet", "--filter", label),
        "network": ("network", "ls", "--quiet", "--filter", label),
        "volume": ("volume", "ls", "--quiet", "--filter", label),
    }
    for kind, arguments in queries.items():
        listed = execute(container_command(*arguments, environment=source), source)
        if listed.returncode:
            raise ProjectResourceError("project resource query failed")
        handles = listed.stdout.split()
        if len(handles) != len(set(handles)):
            raise ProjectResourceError("project resource inventory is ambiguous")
        for handle in handles:
            inspected = execute(
                container_command(kind, "inspect", handle, environment=source), source,
            )
            if inspected.returncode:
                raise ProjectResourceError("project resource inspection failed")
            try:
                payload = json.loads(inspected.stdout)
                info = payload[0]
                if len(payload) != 1 or not isinstance(info, dict):
                    raise ValueError
            except (ValueError, TypeError, KeyError) as exc:
                raise ProjectResourceError("project resource inspection failed") from exc
            immutable_id, owned_project, fields = _inspect_fields(
                kind, info, selected_engine(source),
            )
            if owned_project != project:
                raise ProjectResourceError("project resource ownership changed")
            resources.append(ProjectResource(
                kind, handle, immutable_id,
                json.dumps(fields, sort_keys=True, separators=(",", ":")),
            ))
    return ProjectInventory(project, tuple(sorted(resources)))


def require_empty_project(
    project: str, environment: Mapping[str, str] | None = None,
    *, runner: ResourceRunner | None = None,
) -> ProjectInventory:
    inventory = capture_project_inventory(project, environment, runner=runner)
    if inventory.resources:
        raise ProjectResourceError("disposable project already has resources")
    return inventory


def require_project_inventory(
    expected: ProjectInventory, environment: Mapping[str, str] | None = None,
    *, runner: ResourceRunner | None = None,
) -> None:
    if capture_project_inventory(expected.project, environment, runner=runner) != expected:
        raise ProjectResourceError("project resources changed or contain unknown resources")


def _resource_fields(resource: ProjectResource) -> dict[str, Any]:
    try:
        fields = json.loads(resource.fingerprint)
    except (TypeError, ValueError) as exc:
        raise ProjectResourceError("project resource fingerprint is invalid") from exc
    if not isinstance(fields, dict) or not isinstance(fields.get("Labels"), dict):
        raise ProjectResourceError("project resource fingerprint is invalid")
    return fields


def _matches_rule(resource: ProjectResource, rule: ProjectResourceRule) -> bool:
    if resource.kind != rule.kind:
        return False
    fields = _resource_fields(resource)
    labels = fields["Labels"]
    if rule.name is not None and fields.get("Name") != rule.name:
        return False
    return all(labels.get(key) == value for key, value in rule.required_labels)


def admit_project_transition(
    expected: ProjectInventory,
    observed: ProjectInventory,
    allowed_new: tuple[ProjectResourceRule, ...],
    *,
    allowed_removed: tuple[ProjectResource, ...] = (),
    required_removed: tuple[ProjectResource, ...] = (),
) -> ProjectInventory:
    """Admit only retained identities plus explicitly described new resources."""
    if expected.project != observed.project:
        raise ProjectResourceError("project resource transition changed project")
    if len(observed.resources) != len(set(observed.resources)):
        raise ProjectResourceError("project resource transition is ambiguous")
    allowed = set(allowed_removed) | set(required_removed)
    required = set(required_removed)
    if (
        len(set(allowed_removed)) != len(allowed_removed)
        or len(required) != len(required_removed)
        or not allowed <= set(expected.resources)
    ):
        raise ProjectResourceError("project resource removal transition is invalid")
    current_by_identity = {
        (resource.kind, resource.immutable_id): resource
        for resource in observed.resources
    }
    if len(current_by_identity) != len(observed.resources):
        raise ProjectResourceError("project resource transition is ambiguous")
    expected_identities = {
        (resource.kind, resource.immutable_id): resource
        for resource in expected.resources
    }
    for identity, resource in expected_identities.items():
        current = current_by_identity.get(identity)
        if resource in required and current is not None:
            raise ProjectResourceError("required project resource was not removed")
        if current is None:
            if resource not in allowed:
                raise ProjectResourceError("recorded project resource was removed or replaced")
        elif current != resource:
            raise ProjectResourceError("recorded project resource identity changed")
    additions = [
        resource for identity, resource in current_by_identity.items()
        if identity not in expected_identities
    ]
    for addition in additions:
        matches = [rule for rule in allowed_new if _matches_rule(addition, rule)]
        if len(matches) != 1:
            raise ProjectResourceError("unexpected project resource transition")
    for rule in allowed_new:
        matching = [resource for resource in observed.resources if _matches_rule(resource, rule)]
        if len(matching) > 1:
            raise ProjectResourceError("project resource transition is ambiguous")
    return observed


def cleanup_project_inventory(
    expected: ProjectInventory, environment: Mapping[str, str] | None = None,
    *, runner: ResourceRunner | None = None,
) -> None:
    source = os.environ if environment is None else environment
    execute = _run if runner is None else runner
    require_project_inventory(expected, source, runner=runner)
    for resource in expected.resources:
        if resource.kind == "container":
            arguments = ("rm", "--force", resource.immutable_id)
        elif resource.kind == "network":
            arguments = ("network", "rm", resource.immutable_id)
        else:
            arguments = ("volume", "rm", resource.handle)
        removed = execute(container_command(*arguments, environment=source), source)
        if removed.returncode:
            raise ProjectResourceError("exact project resource cleanup failed")
    remaining = capture_project_inventory(expected.project, source, runner=runner)
    if remaining.resources:
        raise ProjectResourceError("exact project resource cleanup was incomplete")


def cleanup_project_resource_kinds(
    expected: ProjectInventory,
    kinds: frozenset[str],
    environment: Mapping[str, str] | None = None,
) -> ProjectInventory:
    """Remove selected exact resources and prove every retained identity."""
    if not kinds <= {"container", "network", "volume"}:
        raise ProjectResourceError("project cleanup resource kind is invalid")
    source = os.environ if environment is None else environment
    require_project_inventory(expected, source)
    for resource in expected.resources:
        if resource.kind not in kinds:
            continue
        if resource.kind == "container":
            arguments = ("rm", "--force", resource.immutable_id)
        elif resource.kind == "network":
            arguments = ("network", "rm", resource.immutable_id)
        else:
            arguments = ("volume", "rm", resource.handle)
        if _run(container_command(*arguments, environment=source), source).returncode:
            raise ProjectResourceError("exact project resource cleanup failed")
    remaining = capture_project_inventory(expected.project, source)
    retained = tuple(resource for resource in expected.resources if resource.kind not in kinds)
    if remaining != ProjectInventory(expected.project, retained):
        raise ProjectResourceError("exact project resource cleanup was incomplete")
    return remaining
