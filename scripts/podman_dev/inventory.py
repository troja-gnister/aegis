"""Identity inventory of pre-existing Podman objects; it reports and never deletes.

Runtime state (running, exited, restart counts) is deliberately excluded: other
services on the host may restart during a session without that being a change.
"""
from __future__ import annotations

import json
from typing import Any

from scripts.podman_dev import podman
from scripts.podman_dev.records import SessionError

KINDS = ("containers", "volumes", "networks", "images")
Inventory = dict[str, dict[str, list[Any]]]


def _listing(run: podman.Runner, arguments: tuple[str, ...]) -> list[dict[str, Any]]:
    command = f"podman {' '.join(arguments)}"
    result = run(list(arguments))
    if result.returncode != 0:
        raise SessionError(f"{command} failed: {result.stderr[-2000:]}")
    parsed = json.loads(result.stdout or "null")
    if parsed is None:
        return []
    if not isinstance(parsed, list):
        raise SessionError(f"{command} returned unexpected JSON: {type(parsed).__name__}")
    return parsed


def snapshot(run: podman.Runner = podman.run) -> Inventory:
    containers = _listing(run, ("ps", "--all", "--format", "json"))
    volumes = _listing(run, ("volume", "ls", "--format", "json"))
    networks = _listing(run, ("network", "ls", "--format", "json"))
    images = _listing(run, ("images", "--all", "--format", "json"))
    return {
        "containers": {
            item["Id"]: [sorted(item["Names"]), item["ImageID"], item["Created"]]
            for item in containers
        },
        "volumes": {item["Name"]: [item["CreatedAt"]] for item in volumes},
        "networks": {item["id"]: [item["name"], item["created"]] for item in networks},
        "images": {item["Id"]: [item["Created"]] for item in images},
    }


def compare(before: Inventory, after: Inventory) -> list[str]:
    missing = [kind for kind in KINDS if kind not in before]
    if missing:
        raise SessionError(f"inventory baseline is missing {', '.join(missing)}")
    problems: list[str] = []
    for kind in KINDS:
        current = after.get(kind, {})
        for key, identity in sorted(before[kind].items()):
            if key not in current:
                problems.append(f"pre-existing {kind} {key} disappeared (was {identity})")
            elif current[key] != identity:
                problems.append(
                    f"pre-existing {kind} {key} changed from {identity} to {current[key]}"
                )
    return problems
