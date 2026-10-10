"""The Quadlet units mirror compose.yaml; drift between them fails here (spec D10)."""
from __future__ import annotations

import ipaddress
import json
import re
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from aegisctl.container_engine import selected_engine
from aegisctl.podman_mask_compatibility import MASK_OPTION

from tests.support.compose_render import rendered_compose
from tests.support.podman_deploy import (
    SAMPLE_BACKEND_SUBNET,
    SAMPLE_HOST_GID,
    SAMPLE_IMAGES,
    SAMPLE_PREFIX,
    SAMPLE_READER_TYPE,
    SAMPLE_SECRETS_DIR,
    Unit,
    environment_assignments,
    exec_start_arguments,
    generated_services,
    load_installer,
    network_line,
    parse_unit,
    quadlet_dryrun,
    sample_config,
    systemd_words,
)

REPOSITORY = Path(__file__).resolve().parents[2]
CONTAINERS = ("postgres", "migrate", "web", "operations", "indexer", "media", "gateway", "caddy")
COMPOSE_VARIABLE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)")
PRODUCTION_KEYS = frozenset({
    "AEGIS_ENV", "DJANGO_SETTINGS_MODULE", "AEGIS_RELEASE_ID", "AEGIS_PUBLIC_URL",
    "AEGIS_ALLOWED_HOSTS",
})
PRODUCTION_VALUE = "<production value>"

ALL = frozenset(CONTAINERS)
CADDY = frozenset({"caddy"})
HEALTH_GATED = frozenset({"postgres", "web", "operations", "indexer", "media"})
GROUP_MAPPED = frozenset({"gateway", "operations", "indexer", "media", "web"})
READER_LABELED = frozenset({"gateway", "operations", "indexer", "media"})

# Every way the units may differ from compose: the exact services it applies to, and why.
# Nothing else may differ, and every listed difference must occur (see the exactness test).
ALLOWED_DIFFERENCES: dict[str, tuple[frozenset[str], str]] = {
    "image": (ALL, "units run the published image of the same role, pinned by digest (D3)"),
    "build": (ALL, "the server never builds; CI publishes the images (D3)"),
    "profiles": (CADDY, "this deployment always includes the tls edge"),
    "depends_on": (
        frozenset({"migrate", "web", "operations", "indexer", "media", "caddy"}),
        "systemd ordering replaces compose conditions; see the render tests",
    ),
    "ports": (
        frozenset({"gateway", "caddy"}),
        "the only listener is the systemd-owned Caddy socket (D2, D7)",
    ),
    "restart": (ALL, "systemd restarts units; containers carry no restart policy (D1)"),
    "ContainerName": (ALL, "Podman object names carry the install prefix"),
    "Pull": (ALL, "the installer pulls the release images; a boot never reaches a registry"),
    "Notify": (HEALTH_GATED, "systemd orders startup by container health (D1)"),
    "GIDMap": (GROUP_MAPPED, "group-only access to originals and the volumes they share (D5)"),
    "SecurityLabelType": (READER_LABELED, "originals readers run as the reader type (D6)"),
    "Unmask": (ALL, "the powercap unmask compose.podman.yaml adds under Podman"),
    "production-environment-values": (
        frozenset({"migrate", "web", "operations", "indexer", "media", "gateway"}),
        "production values replace development defaults",
    ),
    "static-backend-addresses": (
        frozenset({"web", "gateway"}),
        "Quadlet recreates containers on restart; nginx and web's proxy trust pin each "
        "other's address at start.",
    ),
    "command": (CADDY, "Caddy serves Caddyfile.lan on the systemd socket (D2, D8)"),
    "environment": (CADDY, "Caddyfile.lan reads AEGIS_LAN_SITES, not AEGIS_TLS_HOST (D8)"),
    "caddyfile-bind": (CADDY, "Caddyfile.lan ships in the Caddy image, not as a bind (D4)"),
    "sysctls": (CADDY, "Podman's low-port sysctl serves in-container binds; Caddy binds none"),
    "tmpfs-ownership": (
        CADDY,
        "Podman refuses uid/gid tmpfs options; U,notmpcopyup gives the same owner "
        "(compose.podman.yaml)",
    ),
}
# Differences that only one engine's compose rendering contains.
ENGINE_ONLY = {("sysctls", "caddy"): "podman", ("tmpfs-ownership", "caddy"): "docker"}
COMPOSE_FIELDS = frozenset({
    "image", "user", "read_only", "tmpfs", "cap_drop", "cap_add", "security_opt", "init",
    "networks", "volumes", "secrets", "environment", "command", "entrypoint", "healthcheck",
})
UNIT_FIELDS = frozenset({
    "Image", "User", "Group", "ReadOnly", "Tmpfs", "DropCapability", "AddCapability",
    "NoNewPrivileges", "RunInit", "Network", "Volume", "Environment", "Exec",
    "Entrypoint", "HealthCmd", "HealthInterval", "HealthTimeout", "HealthRetries",
    "HealthStartPeriod",
})


class Allowances:
    """Grants listed differences and records each (difference, service) actually used."""

    def __init__(self) -> None:
        self.used: set[tuple[str, str]] = set()

    def take(self, service: str, field: str) -> bool:
        services, _ = ALLOWED_DIFFERENCES.get(field, (frozenset(), ""))
        if service not in services:
            return False
        self.used.add((field, service))
        return True


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    """Compose's tls rendering without any inherited value for the variables it reads.

    Compose still reads an untracked .env in the repository root, as for test_compose.py.
    """
    variables = {
        name
        for file in ("compose.yaml", "compose.podman.yaml")
        for name in COMPOSE_VARIABLE.findall((REPOSITORY / file).read_text(encoding="utf-8"))
    }
    with pytest.MonkeyPatch.context() as patch:
        for name in variables:
            patch.delenv(name, raising=False)
        return rendered_compose("tls")


@pytest.fixture(scope="module")
def units() -> dict[str, Unit]:
    return {
        name: parse_unit(text)
        for name, text in load_installer().render_units(sample_config()).items()
    }


def container(units: dict[str, Unit], name: str) -> Unit:
    return units[f"{SAMPLE_PREFIX}{name}.container"]


def compose_view(name: str, service: dict[str, Any], allowances: Allowances) -> dict[str, Any]:
    unknown = {
        key for key in service if key not in COMPOSE_FIELDS and not allowances.take(name, key)
    }
    assert not unknown, f"{name}: compose fields neither compared nor allowed: {unknown}"
    uid, _, gid = service["user"].partition(":")
    no_new_privileges = False
    for option in service.get("security_opt", []):
        if option == "no-new-privileges:true":
            no_new_privileges = True
        else:
            assert option == MASK_OPTION and allowances.take(name, "Unmask"), (name, option)
    tmpfs = list(service.get("tmpfs", []))
    owned = [entry.replace(f"uid={uid},gid={gid}", "U,notmpcopyup") for entry in tmpfs]
    if owned != tmpfs and allowances.take(name, "tmpfs-ownership"):
        tmpfs = owned
    for options in service.get("networks", {}).values():
        assert set(options or {}) <= {"aliases"}, (name, options)
    volumes = []
    for mount in service.get("volumes", []):
        if mount["type"] == "bind" and mount["target"] == "/etc/caddy/Caddyfile":
            assert allowances.take(name, "caddyfile-bind"), name
            continue
        assert mount["type"] == "volume", (name, mount)
        assert set(mount) <= {"type", "source", "target", "read_only", "volume"}, (name, mount)
        assert not mount.get("volume"), (name, mount)
        volumes.append((mount["source"], mount["target"], bool(mount.get("read_only"))))
    # Compose mounts each file secret read-only at its target; short targets sit in
    # /run/secrets. The units bind the installer's file of the same name the same way.
    secrets = []
    for secret in service.get("secrets", []):
        assert set(secret) == {"source", "target"}, (name, secret)
        target = secret["target"]
        target = target if target.startswith("/") else f"/run/secrets/{target}"
        secrets.append((secret["source"], target))
    environment = None
    if not allowances.take(name, "environment"):
        environment = production_view(name, service.get("environment", {}), allowances)
    image = service["image"]
    assert allowances.take(name, "image") and image.startswith("aegis-"), (name, image)
    return {
        "image": SAMPLE_IMAGES[image.removeprefix("aegis-")],
        "user": service["user"],
        "read_only": service.get("read_only") is True,
        "tmpfs": sorted(tmpfs),
        "cap_drop": sorted(service.get("cap_drop", [])),
        "cap_add": sorted(service.get("cap_add", [])),
        "no_new_privileges": no_new_privileges,
        "init": service.get("init") is True,
        "networks": {
            network: sorted({name, *((options or {}).get("aliases") or [])})
            for network, options in service.get("networks", {}).items()
        },
        "volumes": sorted(volumes),
        "secrets": sorted(secrets),
        "environment": environment,
        "command": None if allowances.take(name, "command") else service.get("command"),
        "entrypoint": service.get("entrypoint"),
        "healthcheck": service.get("healthcheck"),
    }


def production_view(
    name: str, environment: dict[str, str], allowances: Allowances,
) -> dict[str, str]:
    """Environment values, with production keys compared by presence only."""
    if PRODUCTION_KEYS & set(environment):
        assert allowances.take(name, "production-environment-values"), name
    return {
        key: PRODUCTION_VALUE if key in PRODUCTION_KEYS else value
        for key, value in environment.items()
    }


def unit_health(unit: Unit) -> dict[str, Any] | None:
    command = unit.value("Container", "HealthCmd")
    if command is None:
        return None
    # Podman reads a JSON array as exec form and any other string as a shell command.
    test = json.loads(command) if command.startswith("[") else ["CMD-SHELL", command]
    if test[0] not in ("CMD", "CMD-SHELL"):
        test = ["CMD", *test]
    health: dict[str, Any] = {"test": test}
    for key, field in (("HealthTimeout", "timeout"), ("HealthInterval", "interval"),
                       ("HealthRetries", "retries"), ("HealthStartPeriod", "start_period")):
        value = unit.value("Container", key)
        if value is not None:
            health[field] = int(value) if field == "retries" else value
    return health


def unit_view(name: str, unit: Unit, allowances: Allowances) -> dict[str, Any]:
    unknown = {key for key in unit.keys("Container")
               if key not in UNIT_FIELDS and not allowances.take(name, key)}
    assert not unknown, f"{name}: unit keys neither compared nor allowed: {unknown}"

    def capabilities(key: str) -> list[str]:
        return sorted(
            word.upper().removeprefix("CAP_")
            for value in unit.values("Container", key) for word in value.split()
        )

    networks = {}
    for line in unit.values("Container", "Network"):
        reference, aliases, network_options = network_line(line)
        assert reference.startswith(SAMPLE_PREFIX) and reference.endswith(".network"), line
        network = reference.removeprefix(SAMPLE_PREFIX).removesuffix(".network")
        if network_options:
            assert set(network_options) == {"ip"} and network == "backend", line
            assert allowances.take(name, "static-backend-addresses"), line
        networks[network] = sorted(aliases)
    volumes = []
    secrets = []
    for line in unit.values("Container", "Volume"):
        source, target, *mount_options = line.split(":")
        if source.startswith("/"):
            directory, _, secret = source.rpartition("/")
            assert directory == SAMPLE_SECRETS_DIR and mount_options == ["ro"], line
            secrets.append((secret, target))
            continue
        assert source.startswith(SAMPLE_PREFIX) and source.endswith(".volume"), line
        assert mount_options in ([], ["ro"]), line
        volume = source.removeprefix(SAMPLE_PREFIX).removesuffix(".volume")
        volumes.append((volume, target, mount_options == ["ro"]))
    environment = None
    if not allowances.take(name, "environment"):
        environment = production_view(name, environment_assignments(unit), allowances)
    command = unit.value("Container", "Exec")
    entrypoint = unit.value("Container", "Entrypoint")
    return {
        "image": unit.value("Container", "Image"),
        "user": f"{unit.value('Container', 'User')}:{unit.value('Container', 'Group')}",
        "read_only": unit.value("Container", "ReadOnly") == "true",
        "tmpfs": sorted(unit.values("Container", "Tmpfs")),
        "cap_drop": capabilities("DropCapability"),
        "cap_add": capabilities("AddCapability"),
        "no_new_privileges": unit.value("Container", "NoNewPrivileges") == "true",
        "init": unit.value("Container", "RunInit") == "true",
        "networks": networks,
        "volumes": sorted(volumes),
        "secrets": sorted(secrets),
        "environment": environment,
        "command": (
            None if allowances.take(name, "command") or command is None
            else systemd_words(command)
        ),
        "entrypoint": (
            None if entrypoint is None
            else json.loads(entrypoint) if entrypoint.startswith("[") else [entrypoint]
        ),
        "healthcheck": unit_health(unit),
    }


def test_units_cover_exactly_the_compose_tls_services(
    compose: dict[str, Any], units: dict[str, Unit],
) -> None:
    containers = {
        name.removeprefix(SAMPLE_PREFIX).removesuffix(".container")
        for name in units if name.endswith(".container")
    }

    assert set(compose["services"]) == containers == set(CONTAINERS)


@pytest.mark.parametrize("name", CONTAINERS)
def test_each_unit_matches_its_compose_service(
    compose: dict[str, Any], units: dict[str, Unit], name: str,
) -> None:
    expected = compose_view(name, compose["services"][name], Allowances())

    assert unit_view(name, container(units, name), Allowances()) == expected


def test_each_allowance_covers_exactly_the_differences_this_engine_renders(
    compose: dict[str, Any], units: dict[str, Unit],
) -> None:
    allowances = Allowances()
    for name in CONTAINERS:
        compose_view(name, compose["services"][name], allowances)
        unit_view(name, container(units, name), allowances)
    other_engine = "docker" if selected_engine() == "podman" else "podman"
    declared = {
        (field, service)
        for field, (services, _) in ALLOWED_DIFFERENCES.items() for service in services
    }

    assert allowances.used == declared - {
        pair for pair, engine in ENGINE_ONLY.items() if engine == other_engine
    }


def test_networks_and_volumes_match_compose_exactly(
    compose: dict[str, Any], units: dict[str, Unit],
) -> None:
    networks = {}
    volumes = set()
    for name, unit in units.items():
        short = name.removeprefix(SAMPLE_PREFIX).rpartition(".")[0]
        if name.endswith(".network"):
            assert set(unit.sections) == {"Network"}, name
            fixed = {"Subnet", "IPRange"} if short == "backend" else set()
            assert unit.keys("Network") - {"Internal"} == {"NetworkName"} | fixed, name
            assert not fixed or "static-backend-addresses" in ALLOWED_DIFFERENCES
            networks[short] = unit.value("Network", "Internal") == "true"
        elif name.endswith(".volume"):
            assert set(unit.sections) == {"Volume"}, name
            assert unit.sections["Volume"] == [("VolumeName", f"{SAMPLE_PREFIX}{short}")], name
            volumes.add(short)
    # Compose renders an empty ipam for a network that sets none.
    for name, network in compose["networks"].items():
        assert set(network) <= {"name", "internal", "ipam"} and not network.get("ipam"), name
    for name, volume in compose["volumes"].items():
        assert set(volume) <= {"name"}, name

    assert networks == {
        name: network.get("internal") is True for name, network in compose["networks"].items()
    }
    assert volumes == set(compose["volumes"])


def test_every_container_unmasks_exactly_the_compose_podman_path(units: dict[str, Unit]) -> None:
    for name in CONTAINERS:
        assert container(units, name).values("Container", "Unmask") == [
            MASK_OPTION.removeprefix("unmask=")
        ], name


# Docker CI has no Podman, so this test exists only when Podman is the selected engine.
if selected_engine() == "podman":

    def test_quadlet_accepts_the_units_and_generates_the_hardened_run_options(
        tmp_path: Path,
    ) -> None:
        group_mapped = {"gateway": 101, "operations": 10001, "indexer": 10001, "media": 10001,
                        "web": 10001}
        static_hosts = {"web": 10, "gateway": 11}
        secret_binds = {"postgres": 6, "migrate": 2, "web": 3, "operations": 2, "indexer": 2,
                        "media": 2, "gateway": 0, "caddy": 0}
        health_commands = {
            "web": '["python","manage.py","check_runtime","--role","web","--require-http"]',
            "postgres": 'pg_isready -h 127.0.0.1 -p 5432 -U postgres -d "aegis"',
        }
        subnet = ipaddress.ip_network(SAMPLE_BACKEND_SUBNET)
        pool = ipaddress.ip_network(f"{subnet[128]}/25")
        result = quadlet_dryrun(load_installer().render_units(sample_config()), tmp_path)

        assert result.returncode == 0, result.stderr
        services = generated_services(result.stdout)
        assert set(services) == {
            *(f"{SAMPLE_PREFIX}{name}.service" for name in CONTAINERS),
            *(f"{SAMPLE_PREFIX}{name}-network.service"
              for name in ("backend", "edge", "tls-hop")),
            *(f"{SAMPLE_PREFIX}{name}-volume.service" for name in (
                "postgres-data", "indexer-coordination", "staging", "derivatives",
                "quarantine", "caddy-data",
            )),
        }
        for name in CONTAINERS:
            arguments = exec_start_arguments(services[f"{SAMPLE_PREFIX}{name}.service"])
            pairs = set(pairwise(arguments))
            assert arguments[:2] == ["/usr/bin/podman", "run"], name
            assert "--read-only" in arguments, name
            assert ("--cap-drop", "all") in pairs, name
            assert ("--security-opt", "no-new-privileges") in pairs, name
            assert ("--security-opt", MASK_OPTION) in pairs, name
            gid_maps = [value for flag, value in pairs if flag == "--gidmap"]
            assert gid_maps == (
                [f"+g{group_mapped[name]}:@{SAMPLE_HOST_GID}:1"] if name in group_mapped else []
            ), name
            labels = [value for flag, value in pairs
                      if flag == "--security-opt" and value.startswith("label=")]
            assert labels == (
                [f"label=type:{SAMPLE_READER_TYPE}"] if name in READER_LABELED else []
            ), name
            assert ("--pull", "never") in pairs, name
            assert ("--init" in arguments) is (name == "indexer"), name
            assert (("--sdnotify", "healthy") in pairs) is (name in HEALTH_GATED), name
            assert ("-d" in arguments) is (name != "migrate"), name
            if name in health_commands:
                assert [value for flag, value in pairs if flag == "--health-cmd"] == [
                    health_commands[name]
                ], name
            assert not {"--publish", "-p", "--restart", "--secret"} & set(arguments), name
            networks = [value for flag, value in pairs if flag == "--network"]
            backend = f"{SAMPLE_PREFIX}backend:alias={name}"
            if name in static_hosts:
                backend += f",ip={subnet[static_hosts[name]]}"
            assert (backend in networks) is (name not in ("caddy",)), (name, networks)
            assert not any("ip=" in network for network in networks if network != backend), name
            binds = [value for flag, value in pairwise(arguments)
                     if flag == "-v" and value.startswith(f"{SAMPLE_SECRETS_DIR}/")]
            assert len(binds) == secret_binds[name], name
            assert all(value.endswith(":ro") for value in binds), name
        for name in ("backend", "edge", "tls-hop"):
            arguments = exec_start_arguments(services[f"{SAMPLE_PREFIX}{name}-network.service"])
            pairs = set(pairwise(arguments))
            assert arguments[-1] == f"{SAMPLE_PREFIX}{name}", name
            assert ("--internal" in arguments) is (name != "edge"), name
            assert (("--subnet", SAMPLE_BACKEND_SUBNET) in pairs) is (name == "backend"), name
            assert (("--ip-range", str(pool)) in pairs) is (name == "backend"), name
