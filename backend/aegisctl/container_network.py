"""Strict normalization of Docker and native Podman network inspection JSON."""
from __future__ import annotations

import ipaddress
from typing import Any

from aegisctl.container_engine import ContainerEngineError


def _require_shape(info: dict[str, Any], engine: str) -> None:
    # Podman 5 native network inspect uses lower-case keys; Docker's API uses
    # title-case keys (with Id/ID varying by API). Never merge the two shapes.
    foreign = ({"Id", "ID", "Name", "Created", "Driver", "Labels", "Internal", "IPAM"}
               if engine == "podman" else
               {"id", "name", "created", "driver", "labels", "internal", "subnets"})
    if engine not in {"docker", "podman"} or foreign.intersection(info):
        raise ContainerEngineError("network inspection shape is invalid")


def network_identity_fields(info: dict[str, Any], engine: str) -> dict[str, Any]:
    """Return canonical identity/ownership fields, refusing missing or mixed keys."""
    _require_shape(info, engine)
    identity_keys = ("id",) if engine == "podman" else ("Id", "ID")
    identities = [info[key] for key in identity_keys if key in info]
    if len(identities) != 1:
        raise ContainerEngineError("network inspection identity is ambiguous")
    fields = {"Id": identities[0]}
    for key in ("Created", "Name", "Driver", "Labels"):
        fields[key] = info.get(key.lower() if engine == "podman" else key)
    if any(not isinstance(fields[key], str) or not fields[key]
           for key in ("Id", "Created", "Name", "Driver")):
        raise ContainerEngineError("network inspection identity is invalid")
    labels = fields["Labels"]
    if not isinstance(labels, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items()
    ):
        raise ContainerEngineError("network inspection labels are invalid")
    internal_key = "internal" if engine == "podman" else "Internal"
    if internal_key in info:
        if not isinstance(info[internal_key], bool):
            raise ContainerEngineError("network inspection isolation is invalid")
        fields["Internal"] = info[internal_key]
    return fields


def network_subnets(info: dict[str, Any], engine: str) -> list[str]:
    """Read only the selected engine's subnet shape, including Docker null IPAM."""
    _require_shape(info, engine)
    if engine == "podman":
        entries = info.get("subnets")
        subnet_key = "subnet"
    else:
        if "IPAM" not in info:
            raise ContainerEngineError("network inspection IPAM is missing")
        ipam = info["IPAM"]
        if ipam is None:
            return []
        if not isinstance(ipam, dict):
            raise ContainerEngineError("network inspection IPAM is invalid")
        entries = ipam.get("Config")
        if entries is None:
            return []
        subnet_key = "Subnet"
    if not isinstance(entries, list):
        raise ContainerEngineError("network inspection subnets are invalid")
    result: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ContainerEngineError("network inspection subnet is invalid")
        foreign_key = "Subnet" if engine == "podman" else "subnet"
        if foreign_key in entry:
            raise ContainerEngineError("network inspection subnet is ambiguous")
        subnet = entry.get(subnet_key)
        if engine == "docker" and subnet is None:
            continue
        if not isinstance(subnet, str) or not subnet:
            raise ContainerEngineError("network inspection subnet is invalid")
        try:
            ipaddress.ip_network(subnet)
        except ValueError as exc:
            raise ContainerEngineError("network inspection subnet is invalid") from exc
        result.append(subnet)
    return result
