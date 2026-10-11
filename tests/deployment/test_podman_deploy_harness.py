"""The rootless Podman deployment harness, scripts/podman_deploy_check.py (spec D12, D13).

Nothing here runs Podman, systemd, the installer, chcon or ip, or opens a connection: every
program goes to FakeHost, which refuses any command it does not know, HTTPS goes to a fake
gateway with nginx's limit_req arithmetic, and every path is under tmp_path.
"""
from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import re
import signal
import ssl
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from scripts import podman_deploy_check as harness

TOKEN = "0a1b2c3d"
RUN = f"t{TOKEN}"
PREFIX = f"aegis-{RUN}-"
RELEASE = f"test-{TOKEN}"
# Documentation addresses (RFC 5737) and a made-up port: no host value.
HOST_ADDRESS = "198.51.100.20"
TEST_PORT = 23456
PYTHON = "/opt/test/python3"
PEM = "-----BEGIN CERTIFICATE-----\nMIIBsynthetic\n-----END CERTIFICATE-----\n"
RUN_LABEL = harness.RUN_LABEL
ROLES = ("backend", "gateway", "postgres", "caddy")
CHECKS = harness.CHECKS
SETUP = ("preflight", "build_images", "install")
FINAL = ("cleanup", "preexisting_unchanged")
PREEXISTING_IMAGE = "1" * 64


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeGateway:
    """nginx limit_req, nodelay, 10 requests a minute with a burst of 10, per key."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.buckets: dict[str, tuple[float, float]] = {}

    def login(self, key: str) -> int:
        now = self.clock.now
        if key not in self.buckets:
            self.buckets[key] = (0.0, now)
            return 403
        excess, last = self.buckets[key]
        excess = max(excess - (now - last) * 10 / 60, 0.0) + 1
        if excess > 10:
            return 503
        self.buckets[key] = (excess, now)
        return 403  # Django refuses a POST without a CSRF token


def completed(
    arguments: Sequence[str], code: int = 0, stdout: str = "", stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(arguments), code, stdout, stderr)


class FakeHost:
    """The programs, systemd user manager, installer and Podman storage the harness sees.

    ``faults`` switch on one misbehaviour each. The installer is simulated: init writes its
    record, install writes the unit files (never an [Install] section unless a fault says
    so), roots add the drop-ins, start brings the stack up, and uninstall removes it.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.config = self.home / ".config"
        self.runtime = tmp_path / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.runtime.chmod(0o700)
        # ${XDG_STATE_HOME:-~/.local/state}: where the record and the run directory live.
        self.state_base = self.home / ".local" / "state"
        self.graph_root = tmp_path / "graph"
        self.graph_root.mkdir()
        self.enforce = tmp_path / "enforce"
        self.enforce.write_text("1\n", encoding="ascii")
        self.clock = FakeClock()
        self.gateway = FakeGateway(self.clock)
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.calls: list[tuple[str, ...]] = []
        self.environments: dict[tuple[str, ...], dict[str, str] | None] = {}
        self.intercept: Callable[[tuple[str, ...]], subprocess.CompletedProcess[str] | None] \
            | None = None
        self.faults: set[str] = set()
        self.rootless = True
        self.selinux = True
        self.systemd = "systemd 256 (256-test)"
        self.free = 100 * 2**30
        self.route: list[dict[str, Any]] = [{
            "dst": "192.0.2.1", "gateway": "198.51.100.1", "dev": "test0",
            "prefsrc": HOST_ADDRESS, "flags": [], "uid": 1000, "cache": [],
        }]
        self.listeners: set[int] = {23000}
        self.layout = harness.layout_for(PREFIX, self.config)
        self.counter = 0
        self.requests: list[tuple[str, str, str]] = []
        self.labelled: list[str] = []
        self.units: dict[str, dict[str, str]] = {}
        # A --no-block restart of PostgreSQL takes effect once this much time has passed.
        self.restart_due: float | None = None
        # Objects that existed before the run; the harness must leave them as they are.
        self.containers: dict[str, dict[str, Any]] = {"home-service": {
            "Name": "home-service", "Id": "c" * 64, "Names": ["home-service"],
            "ImageID": PREEXISTING_IMAGE, "Created": 100,
            "Ports": [{"host_port": 8443, "container_port": 443, "range": 1}],
        }}
        self.volumes: dict[str, dict[str, Any]] = {
            "home-data": {"Name": "home-data", "CreatedAt": "2026-01-01T00:00:00Z"},
        }
        self.networks: dict[str, dict[str, Any]] = {
            "home-net": {"name": "home-net", "id": "9" * 64},
        }
        self.images: dict[str, dict[str, Any]] = {PREEXISTING_IMAGE: {
            "tags": ["registry.example.test/home:1"], "labels": {},
            "created": "2026-01-01T00:00:00Z",
        }}

    @property
    def state_directory(self) -> Path:
        return self.state_base / "aegis-podman-deploy-check"

    @property
    def run_directory(self) -> Path:
        return self.state_directory / RUN

    @property
    def state_home(self) -> Path:
        return self.run_directory / "state"

    @property
    def root(self) -> Path:
        return self.run_directory / "root"

    def reboot(self) -> None:
        """What a reboot does to the run: no container or unit keeps running, and the
        socket no longer listens. Unit files, volumes, networks, images and the run's own
        directory stay."""
        for name in [name for name in self.containers if name.startswith(PREFIX)]:
            del self.containers[name]
        for unit, info in self.units.items():
            self.units[unit] = info | {"ActiveState": "inactive", "SubState": "dead"}
        self.listeners.discard(TEST_PORT)

    # -- plumbing --------------------------------------------------------------------------
    def __call__(
        self, arguments: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        assert timeout > 0
        command = tuple(arguments)
        assert all(isinstance(word, str) for word in command), command
        self.calls.append(command)
        self.environments[command] = None if env is None else dict(env)
        if self.intercept is not None and (answer := self.intercept(command)) is not None:
            return answer
        if command[: len(harness.GENTLE)] == harness.GENTLE:
            assert command[len(harness.GENTLE):][2:3] == ("build",), command
            command = command[len(harness.GENTLE):]
        if command[:2] == (PYTHON, str(harness.INSTALLER)):
            assert env is not None
            return self.installer(command, command[2:], dict(env))
        assert env is None, command
        if command[:2] == harness.PODMAN:
            return self.podman(command, command[2:])
        if command[:2] == harness.SYSTEMCTL:
            return self.systemctl(command, command[2:])
        if command == ("systemctl", "--version"):
            return completed(command, 0, f"{self.systemd}\n+PAM +SELINUX\n")
        if command == ("ip", "-j", "route", "get", "192.0.2.1"):
            return completed(command, 0, json.dumps(self.route))
        if command[:4] == ("chcon", "-h", "-t", "container_file_t") and len(command) == 5:
            assert Path(command[4]).exists() and not Path(command[4]).is_symlink(), command
            self.labelled.append(command[4])
            return completed(command)
        raise AssertionError(f"unexpected command {command}")

    def installer_calls(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[:2] == (PYTHON, str(harness.INSTALLER))]

    def commands(self, *prefix: str) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[: len(prefix)] == prefix]

    def new_id(self) -> str:
        self.counter += 1
        return f"{self.counter:064x}"

    def run_image(self, role: str) -> str | None:
        tag = harness.image_tag(role, RUN)
        return next((identifier for identifier, image in self.images.items()
                     if tag in image["tags"]), None)

    # -- the installer ---------------------------------------------------------------------
    def installer(
        self, command: tuple[str, ...], words: tuple[str, ...], env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        verb = " ".join(words[:2]) if words[0] == "roots" else words[0]
        rest = words[len(verb.split()):]
        assert rest[:4] == ("--prefix", PREFIX, "--state-home", str(self.state_home)), words
        assert env["TMPDIR"] == str(self.run_directory / "tmp"), env
        assert not {"CONTAINER_HOST", "PYTHONPATH"} & set(env), env
        assert "--install-sections" not in words, words
        if f"{verb}-fails" in self.faults:
            return completed(command, 1, "", f"aegis-podman: error: {verb} refused (fake)")
        if verb == "init":
            self.state_home.mkdir(mode=0o700)
            state = self.state_home / "state.json"
            state.write_text(json.dumps({"prefix": PREFIX}), encoding="utf-8")
            state.chmod(0o600)
            return completed(command, 0, "aegis-podman: initialized\n")
        if verb == "install":
            for path in map(Path, self.layout.unit_files):
                path.parent.mkdir(parents=True, exist_ok=True)
                text = "[Unit]\nDescription=fake\n"
                if "install-section" in self.faults and path.suffix == ".socket":
                    text += "[Install]\nWantedBy=sockets.target\n"
                path.write_text(text, encoding="utf-8")
            for unit in self.layout.units:
                self.units[unit] = {"LoadState": "loaded", "ActiveState": "inactive",
                                    "SubState": "dead", "Result": "success", "NRestarts": "0"}
            return completed(command)
        if verb == "roots add":
            assert rest[4:] == ("--slot", "deploy-check", "--source", str(self.root),
                                "--timeout", "600"), rest
            for directory in map(Path, self.layout.drop_in_directories):
                directory.mkdir()
                (directory / "50-aegis-roots.conf").write_text(
                    f"[Container]\nVolume={self.root}:/srv/aegis/roots/deploy-check:ro\n",
                    encoding="utf-8",
                )
            return completed(command)
        if verb == "start":
            self.start_stack()
            return completed(command, 0, "aegis-podman: every unit is active\n")
        if verb == "ca-cert":
            return completed(command, 0, PEM)
        if verb == "stop":
            self.stop_units(self.layout.units)
            return completed(command)
        if verb == "uninstall":
            assert "--purge-data" in rest, rest
            if "uninstall-refuses" in self.faults:
                return completed(command, 1, "", "aegis-podman: error: a unit changed (fake)")
            self.stop_units(self.layout.units)
            for path in map(Path, self.layout.unit_files):
                path.unlink(missing_ok=True)
            for directory in map(Path, self.layout.drop_in_directories):
                if directory.is_dir():
                    for entry in directory.iterdir():
                        entry.unlink()
                    directory.rmdir()
            for unit in self.layout.units:
                self.units.pop(unit, None)
            for name in self.layout.volumes:
                self.volumes.pop(name, None)
            for name in self.layout.networks:
                self.networks.pop(name, None)
            for path in sorted(self.state_home.rglob("*"), reverse=True):
                path.rmdir() if path.is_dir() else path.unlink()
            self.state_home.rmdir()
            if "unremovable-file" in self.faults:
                # A file the container user owns, as podman unshare chown leaves one.
                sealed = self.run_directory / "sealed"
                sealed.mkdir()
                (sealed / "owned-by-a-subordinate-uid").write_text("x", encoding="ascii")
                sealed.chmod(0o500)
            if "lose-preexisting-volume" in self.faults:
                del self.volumes["home-data"]
            return completed(command)
        raise AssertionError(f"unexpected installer call {words}")

    # -- the stack -------------------------------------------------------------------------
    def role(self, service: str) -> str:
        for role in ("postgres", "gateway", "caddy"):
            if service == f"{PREFIX}{role}.service":
                return role
        return "backend"

    def new_container(self, service: str) -> None:
        name = self.layout.containers[service]
        health = "healthy"
        if "postgres-stuck" in self.faults and self.role(service) == "postgres" \
                and name in self.containers:
            health = "starting"
        policy = "always" if "restart-policy" in self.faults and service.endswith("web.service") \
            else ""
        bindings = {"8443/tcp": [{"HostIp": "", "HostPort": "8443"}]} if (
            "published-gateway" in self.faults and service.endswith("gateway.service")
        ) else {}
        self.containers[name] = {
            "Name": name, "Id": self.new_id(), "Names": [name],
            "ImageID": self.run_image(self.role(service)), "Created": self.clock.now,
            # Quadlet labels each container with the unit that runs it.
            "Config": {"Labels": {"PODMAN_SYSTEMD_UNIT": service}},
            "Ports": [],
            "State": {"Status": "running", **({"Health": {"Status": health}}
                                              if service in self.layout.health_checked else {})},
            "HostConfig": {"RestartPolicy": {"Name": policy, "MaximumRetryCount": 0},
                           "PortBindings": bindings},
            "NetworkSettings": {"Ports": {"8080/tcp": None}},
        }

    def activate(self, unit: str, sub: str) -> None:
        self.units[unit] = self.units.get(unit, {}) | {
            "LoadState": "loaded", "ActiveState": "active", "SubState": sub,
        }

    def start_stack(self) -> None:
        for unit in self.layout.generated:
            self.activate(unit, "exited")
        for name in self.layout.volumes:
            self.volumes.setdefault(name, {"Name": name, "CreatedAt": "2026-10-10T00:00:00Z"})
        for name in self.layout.networks:
            self.networks.setdefault(name, {"name": name, "id": self.new_id()})
        # systemd reports the socket as running once the service it feeds runs.
        self.activate(self.layout.socket, "running")
        self.listeners.add(TEST_PORT)
        for unit in self.layout.oneshots:
            self.activate(unit, "exited")
            self.units[unit] |= {"Result": "success", "NRestarts": "0"}
        for unit in self.layout.services:
            self.activate(unit, "running")
            if self.layout.containers[unit] not in self.containers:
                self.new_container(unit)

    def stop_units(self, units: Sequence[str]) -> None:
        for unit in units:
            if unit in self.units:
                self.units[unit] |= {"ActiveState": "inactive", "SubState": "dead"}
            self.containers.pop(self.layout.containers.get(unit, ""), None)
            if unit == self.layout.socket:
                self.listeners.discard(TEST_PORT)

    # -- systemctl --user ------------------------------------------------------------------
    def systemctl(
        self, command: tuple[str, ...], words: tuple[str, ...],
    ) -> subprocess.CompletedProcess[str]:
        assert "enable" not in words and "--runtime" not in words, words
        postgres = f"{PREFIX}postgres.service"
        migrate = f"{PREFIX}migrate.service"
        match words:
            case ("show", properties, *units) if properties.startswith("--property="):
                blocks = []
                for unit in units:
                    values = {"Id": unit, "LoadState": "not-found", "ActiveState": "inactive",
                              "SubState": "dead", "Result": "success", "NRestarts": "0"}
                    values |= self.units.get(unit, {})
                    blocks.append("\n".join(f"{key}={value}" for key, value in values.items()
                                            if value))
                return completed(command, 0, "\n\n".join(blocks) + "\n")
            case ("is-enabled", *units):
                states = []
                for unit in units:
                    if unit == self.layout.socket and "socket-enabled" in self.faults:
                        states.append("enabled")
                    elif unit == self.layout.socket:
                        socket_file = next(Path(path) for path in self.layout.unit_files
                                           if path.endswith(".socket"))
                        text = socket_file.read_text(encoding="utf-8")
                        states.append("enabled" if "[Install]" in text else "static")
                    else:
                        states.append("generated")
                return completed(command, 0, "\n".join(states) + "\n")
            case ("--no-block", "restart", unit) if unit == postgres:
                # Queued only: the old container answers until the job runs.
                self.restart_due = self.clock.now + 5
                return completed(command)
            case ("--no-block", "stop", unit) if unit == postgres:
                dependents = [migrate, *(f"{PREFIX}{role}.service"
                                         for role in ("web", "operations", "indexer", "media"))]
                self.stop_units([postgres, *dependents])
                return completed(command)
            case ("--no-block", "--job-mode=ignore-requirements", "start", unit) if unit == migrate:
                if "migrate-succeeds" in self.faults:
                    self.activate(migrate, "exited")
                else:
                    # The first attempt failed; systemd counts the restart only once
                    # RestartSec= has passed.
                    self.units[migrate] |= {"ActiveState": "activating",
                                            "SubState": "auto-restart",
                                            "Result": "exit-code", "NRestarts": "0"}
                return completed(command)
            case ("reset-failed", *_units):
                return completed(command)
        raise AssertionError(f"unexpected systemctl call {words}")

    # -- podman ----------------------------------------------------------------------------
    def image_entry(self, reference: str) -> tuple[str, dict[str, Any]] | None:
        if reference in self.images:
            return reference, self.images[reference]
        for identifier, image in self.images.items():
            if reference in image["tags"]:
                return identifier, image
        return None

    def podman(
        self, command: tuple[str, ...], words: tuple[str, ...],
    ) -> subprocess.CompletedProcess[str]:
        match words:
            case ("info", "--format", "json"):
                return completed(command, 0, json.dumps({
                    "host": {"security": {"rootless": self.rootless,
                                          "selinuxEnabled": self.selinux}},
                    "store": {"graphRoot": str(self.graph_root)},
                }))
            case ("ps", "-a", "--format", "json"):
                return completed(command, 0, json.dumps(list(self.containers.values())))
            case ("ps", "--all", "--format", "json"):
                return completed(command, 0, json.dumps([
                    {"Id": entry["Id"], "Names": entry["Names"], "ImageID": entry["ImageID"],
                     "Created": entry["Created"]} for entry in self.containers.values()
                ]))
            case ("volume", "ls", "--format", "json"):
                return completed(command, 0, json.dumps(list(self.volumes.values())))
            case ("network", "ls", "--format", "json"):
                return completed(command, 0, json.dumps(list(self.networks.values())))
            case ("images", "--all", "--format", "json"):
                return completed(command, 0, json.dumps([
                    {"Id": identifier, "Created": image["created"]}
                    for identifier, image in self.images.items()
                ]))
            case ("build", "--file", dockerfile, "--label", label, "--tag", tag, context):
                role = Path(dockerfile).name.removesuffix(".Dockerfile")
                assert dockerfile == str(harness.DOCKERFILES / f"{role}.Dockerfile"), words
                assert context == str(harness.REPOSITORY) and tag == harness.image_tag(role, RUN)
                assert label == f"{RUN_LABEL}={RUN}", label
                if f"build-{role}-fails" in self.faults:
                    return completed(command, 1, "", "Error: building at STEP 3: failed (fake)")
                if "preexisting-image" in self.faults and role == "caddy":
                    # The whole build came from cache: an image ID that existed before.
                    self.images[PREEXISTING_IMAGE]["tags"].append(tag)
                    self.images[PREEXISTING_IMAGE]["labels"][RUN_LABEL] = RUN
                    return completed(command)
                identifier = hashlib.sha256(tag.encode()).hexdigest()
                self.images[identifier] = {"tags": [tag], "labels": {RUN_LABEL: RUN},
                                           "created": "2026-10-10T00:00:00Z"}
                # Layer caching leaves an untagged intermediate image behind.
                self.images[hashlib.sha256(f"cache:{tag}".encode()).hexdigest()] = {
                    "tags": [], "labels": {}, "created": "2026-10-10T00:00:00Z",
                }
                if "foreign-tag" in self.faults and role == "gateway":
                    # Something else took the tag after the build.
                    self.images[identifier]["tags"].remove(tag)
                    self.images["f" * 64] = {"tags": [tag], "labels": {},
                                             "created": "2026-10-10T00:00:01Z"}
                return completed(command)
            case ("image", "inspect", reference):
                found = self.image_entry(reference)
                if found is None:
                    return completed(command, 125, "", "Error: image not known")
                identifier, image = found
                return completed(command, 0, json.dumps([{
                    "Id": identifier, "RepoTags": list(image["tags"]),
                    "Labels": dict(image["labels"]), "Created": image["created"],
                }]))
            case ("image", "exists", reference):
                return completed(command, 0 if self.image_entry(reference) else 1)
            case ("untag", identifier, tag):
                assert tag in self.images[identifier]["tags"], words
                self.images[identifier]["tags"].remove(tag)
                return completed(command)
            case ("image", "rm", "--no-prune", identifier):
                removed = self.images.get(identifier)
                assert removed is not None, words
                if removed["tags"]:
                    return completed(command, 2, "", "Error: image is referenced in multiple "
                                                     "repositories")
                if any(entry["ImageID"] == identifier for entry in self.containers.values()):
                    return completed(command, 2, "", "Error: image is in use by a container")
                del self.images[identifier]
                return completed(command)
            case ("container", "inspect", name):
                postgres = self.layout.containers[f"{PREFIX}postgres.service"]
                if self.restart_due is not None and self.clock.now >= self.restart_due:
                    self.restart_due = None
                    self.containers[postgres] = {}  # marks a restart for the stuck fault
                    self.new_container(f"{PREFIX}postgres.service")
                container = self.containers.get(name)
                if not container:
                    return completed(command, 125, "", f"Error: no such container {name}")
                return completed(command, 0, json.dumps([container]))
            case ("container" | "volume" | "network" as kind, "exists", name):
                objects = {"container": self.containers, "volume": self.volumes,
                           "network": self.networks}[kind]
                return completed(command, 0 if name in objects else 1)
            case ("exec", name, "python3", "-I", "-c", script, target, file, probe):
                assert name == f"{PREFIX}indexer" and script == harness.ROOT_PROBE
                assert target == "/srv/aegis/roots/deploy-check", target
                seen = {"entries": sorted(os.listdir(self.root)),
                        "read": (self.root / file).read_text(encoding="ascii"),
                        "write": "created" if "writable-root" in self.faults else "EROFS"}
                assert probe == ".aegis-deploy-check-write"
                return completed(command, 0, json.dumps(seen) + "\n")
            case ("unshare", "rm", "-rf", "--one-file-system", "--", path):
                assert path == str(self.run_directory), path
                for directory, _, _ in os.walk(path):
                    os.chmod(directory, 0o700)
                for child in sorted(Path(path).rglob("*"), reverse=True):
                    child.rmdir() if child.is_dir() else child.unlink()
                Path(path).rmdir()
                return completed(command)
        raise AssertionError(f"unexpected podman call {words}")

    # -- HTTPS -----------------------------------------------------------------------------
    def request(
        self, address: str, port: int, method: str, path: str, cafile: Path, *,
        body: bytes | None = None, timeout: float = 15.0,
    ) -> harness.Response:
        self.clock.now += 0.05
        assert port == TEST_PORT and cafile.read_text(encoding="ascii") == PEM
        assert stat.S_IMODE(cafile.stat().st_mode) == 0o600
        self.requests.append((address, method, path))
        if "tls" in self.faults:
            raise ssl.SSLCertVerificationError("certificate verify failed (fake)")
        source = "203.0.113.9" if "source-mismatch" in self.faults \
            and address == HOST_ADDRESS else address
        if (method, path) == ("GET", "/api/v1/auth/csrf"):
            return harness.Response(502 if "csrf-502" in self.faults else 200, source)
        assert (method, path, body) == ("POST", "/api/v1/auth/login", b"{}")
        key = "one bucket" if "shared-bucket" in self.faults else source
        return harness.Response(self.gateway.login(key), source)


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.fake = FakeHost(tmp_path)
        self.uid = os.getuid()
        self.environ = {
            "XDG_RUNTIME_DIR": str(self.fake.runtime), "PATH": "/usr/bin",
            # Neither may reach the installer.
            "CONTAINER_HOST": "unix:///elsewhere.sock", "PYTHONPATH": "/elsewhere",
        }
        self.token = TOKEN
        self.candidates = [18080, 8443, 23000, TEST_PORT]

    def machine(self) -> harness.Machine:
        fake = self.fake

        def free(path: str) -> int:
            assert path == str(fake.graph_root), path
            return fake.free

        return harness.Machine(
            run=fake, request=fake.request, out=fake.stdout, err=fake.stderr,
            environ=self.environ, home=fake.home, uid=self.uid,
            monotonic=fake.clock.monotonic, sleep=fake.clock.sleep,
            port_in_use=lambda port: port in fake.listeners, free_bytes=free,
            port_candidates=lambda: list(self.candidates), token=lambda size: self.token,
            selinux_enforce=fake.enforce, python=PYTHON,
        )

    def __call__(self, *argv: str) -> int:
        return harness.main(list(argv), self.machine())

    @property
    def record_path(self) -> Path:
        return self.fake.state_directory / f"{RUN}.json"

    def output(self) -> str:
        return self.fake.stdout.getvalue() + self.fake.stderr.getvalue()

    def checks(self) -> dict[str, str]:
        found = {}
        for line in self.fake.stdout.getvalue().splitlines():
            match = re.fullmatch(r"AEGIS_PODMAN_DEPLOY check=(\w+) status=(passed|failed)"
                                 r"(?: (.*))?", line)
            if match:
                found[match.group(1)] = match.group(2)
        return found

    def detail(self, name: str) -> str:
        """The last line for the check ``name``."""
        for line in reversed(self.fake.stdout.getvalue().splitlines()):
            if line.startswith(f"AEGIS_PODMAN_DEPLOY check={name} "):
                return line
        raise AssertionError(f"no line for {name}")


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def nothing_of_the_run_is_left(rig: Rig) -> None:
    fake = rig.fake
    assert not rig.record_path.exists()
    assert not fake.run_directory.exists()
    assert not [path for path in (*fake.layout.unit_files, *fake.layout.drop_in_directories)
                if os.path.lexists(path)]
    assert not [name for name in fake.containers if name.startswith(PREFIX)]
    assert not [name for name in (*fake.volumes, *fake.networks) if name.startswith(PREFIX)]
    assert not [identifier for identifier, image in fake.images.items()
                if image["labels"].get(RUN_LABEL) == RUN and identifier != PREEXISTING_IMAGE]
    assert not [tag for image in fake.images.values() for tag in image["tags"]
                if tag.endswith(f":deploy-check-{RUN}")]
    # Only the lock and the run's log stay: no record and no run directory.
    assert sorted(path.name for path in fake.state_directory.iterdir()) == ["lock", f"{RUN}.log"]
    # What existed before the run is still there.
    assert set(fake.containers) >= {"home-service"} and "home-data" in fake.volumes
    assert "home-net" in fake.networks and PREEXISTING_IMAGE in fake.images


def never_boot_enabled(rig: Rig) -> None:
    for call in rig.fake.calls:
        assert "--install-sections" not in call, call
        if call[:2] == harness.SYSTEMCTL:
            assert not {"enable", "reenable", "preset", "link", "--runtime"} & set(call), call


# -- a whole run -------------------------------------------------------------------------------


def test_a_passing_run_checks_everything_and_leaves_nothing(rig: Rig) -> None:
    assert rig() == 0, rig.output()

    assert rig.checks() == dict.fromkeys((*SETUP, *CHECKS, *FINAL), "passed")
    assert rig.fake.stdout.getvalue().splitlines()[-1] == (
        f"AEGIS_PODMAN_DEPLOY result=passed passed={len(SETUP) + len(CHECKS) + len(FINAL)} "
        "failed=0"
    )
    nothing_of_the_run_is_left(rig)
    never_boot_enabled(rig)
    assert not rig.fake.config.exists()  # the run created every unit directory, so all go
    # No secret, and no installer output, reaches the check lines.
    assert "csrfToken" not in rig.output() and "BEGIN CERTIFICATE" not in rig.output()


def test_the_installer_runs_with_exactly_these_arguments(rig: Rig) -> None:
    assert rig() == 0, rig.output()
    fake = rig.fake
    ids = {role: hashlib.sha256(harness.image_tag(role, RUN).encode()).hexdigest()
           for role in ROLES}
    common = ("--prefix", PREFIX, "--state-home", str(fake.state_home))
    base = (PYTHON, str(harness.INSTALLER))

    assert fake.installer_calls() == [
        (*base, "init", *common, "--release", RELEASE, "--https-port", str(TEST_PORT),
         "--name", "127.0.0.1", "--name", HOST_ADDRESS, "--local-images",
         ",".join(f"{role}={ids[role]}" for role in ROLES), "--reader-type", "container_t"),
        (*base, "install", *common, "--no-install-sections"),
        (*base, "roots", "add", *common, "--slot", "deploy-check", "--source", str(fake.root),
         "--timeout", "600"),
        (*base, "start", *common, "--timeout", "900"),
        (*base, "ca-cert", *common),
        # The migrate check queues PostgreSQL and the dependents through start.
        (*base, "start", *common, "--timeout", "900"),
        (*base, "stop", *common, "--timeout", "300"),
        (*base, "uninstall", *common, "--purge-data", "--timeout", "300"),
    ]
    for call in fake.installer_calls():
        environment = fake.environments[call]
        assert environment is not None
        assert environment["TMPDIR"] == str(fake.run_directory / "tmp")
        assert "CONTAINER_HOST" not in environment and "PYTHONPATH" not in environment
        assert environment["PATH"] == "/usr/bin"


def test_the_images_are_built_from_the_working_tree_and_labelled_for_the_run(rig: Rig) -> None:
    assert rig() == 0, rig.output()
    builds = rig.fake.commands(*harness.GENTLE, *harness.PODMAN, "build")
    assert harness.GENTLE == ("nice", "-n", "10", "ionice", "-c", "2", "-n", "7")
    # At a low CPU and I/O priority: the host serves production meanwhile.
    assert builds == [
        (*harness.GENTLE, *harness.PODMAN, "build", "--file",
         str(harness.REPOSITORY / "docker" / f"{role}.Dockerfile"),
         "--label", f"{RUN_LABEL}={RUN}", "--tag", f"localhost/aegis-{role}:deploy-check-{RUN}",
         str(harness.REPOSITORY))
        for role in ROLES
    ]
    # Only exact recorded identities are removed: each tag from its ID, then that ID.
    for role in ROLES:
        identifier = hashlib.sha256(harness.image_tag(role, RUN).encode()).hexdigest()
        assert (*harness.PODMAN, "untag", identifier, harness.image_tag(role, RUN)) \
            in rig.fake.calls
        assert (*harness.PODMAN, "image", "rm", "--no-prune", identifier) in rig.fake.calls


def test_the_synthetic_root_is_group_only_labelled_and_never_an_original(rig: Rig) -> None:
    seen: dict[str, Any] = {}

    def look(command: tuple[str, ...]) -> subprocess.CompletedProcess[str] | None:
        if command[2:4] == ("roots", "add"):
            root = rig.fake.root
            seen["modes"] = {path.relative_to(root.parent).as_posix():
                             stat.S_IMODE(path.lstat().st_mode)
                             for path in (root, *sorted(root.rglob("*")))}
            seen["owners"] = {path.lstat().st_uid for path in (root, *root.rglob("*"))}
        return None

    rig.fake.intercept = look
    assert rig() == 0, rig.output()
    assert seen["modes"] == {"root": 0o750, "root/album": 0o750,
                             "root/album/photo.txt": 0o640, "root/notes.txt": 0o640}
    assert seen["owners"] == {os.getuid()}
    root = rig.fake.root
    assert sorted(rig.fake.labelled) == sorted(
        str(path) for path in (root, root / "album", root / "notes.txt",
                               root / "album" / "photo.txt")
    )


def test_the_record_names_every_identity_before_it_exists(rig: Rig) -> None:
    seen: dict[str, Any] = {}

    def look(command: tuple[str, ...]) -> subprocess.CompletedProcess[str] | None:
        if command[:len(harness.GENTLE)] == harness.GENTLE and "record" not in seen:
            seen["mode"] = stat.S_IMODE(rig.record_path.stat().st_mode)
            seen["record"] = json.loads(rig.record_path.read_text(encoding="utf-8"))
        return None

    rig.fake.intercept = look
    assert rig() == 0, rig.output()
    record, layout = seen["record"], rig.fake.layout
    assert seen["mode"] == 0o600
    assert record["prefix"] == PREFIX and record["release"] == RELEASE
    assert record["run_directory"] == str(rig.fake.run_directory)
    assert record["port"] == TEST_PORT
    assert record["unit_files"] == list(layout.unit_files)
    assert record["drop_in_directories"] == list(layout.drop_in_directories)
    assert sorted(record["containers"]) == sorted(layout.containers.values())
    assert record["volumes"] == list(layout.volumes)
    assert record["networks"] == list(layout.networks)
    assert {role: entry["tag"] for role, entry in record["images"].items()} == {
        role: f"localhost/aegis-{role}:deploy-check-{RUN}" for role in ROLES
    }
    assert record["inventory"]["volumes"] == {"home-data": ["2026-01-01T00:00:00Z"]}
    assert HOST_ADDRESS not in json.dumps(record)


def test_the_test_port_skips_reserved_mapped_and_busy_ports(rig: Rig) -> None:
    # 18080 is reserved, 8443 is mapped by a container, 23000 has a listener.
    assert rig() == 0, rig.output()
    assert f"test port {TEST_PORT}" in rig.detail("preflight")


# -- preflight ----------------------------------------------------------------------------------


READ_ONLY = (
    (*harness.PODMAN, "info"), (*harness.PODMAN, "ps"), (*harness.PODMAN, "volume", "ls"),
    (*harness.PODMAN, "network", "ls"), (*harness.PODMAN, "images"),
    ("systemctl", "--version"), ("ip", "-j", "route", "get"),
)


def _set(attribute: str, value: object) -> Callable[[Rig], None]:
    return lambda rig: setattr(rig.fake, attribute, value)


def _existing(kind: str) -> Callable[[Rig], None]:
    def create(rig: Rig) -> None:
        layout = rig.fake.layout
        path = Path(layout.unit_files[0] if kind == "unit" else layout.drop_in_directories[2])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir() if kind == "drop-in" else path.write_text("", encoding="utf-8")
    return create


@pytest.mark.parametrize(("change", "message"), [
    (_set("rootless", False), "rootless"),
    (_set("selinux", False), "SELinux must be enabled"),
    (lambda rig: rig.fake.enforce.write_text("0\n", encoding="ascii"), "enforcing"),
    (_set("systemd", "systemd 253 (253.1)"), "systemd 254 or newer"),
    (_set("systemd", "not systemd"), "systemd 254 or newer"),
    (lambda rig: rig.fake.listeners.add(18080), "port 18080 has a listener"),
    (lambda rig: rig.fake.listeners.add(55432), "port 55432 has a listener"),
    (lambda rig: setattr(rig, "candidates", [18080, 8443, 23000]), "no free test port"),
    (_set("free", 40 * 2**30 - 1), "at least 40 GiB"),
    (_set("route", []), "no primary non-loopback IPv4"),
    (lambda rig: rig.fake.route[0].update(prefsrc="127.0.0.1"), "no primary non-loopback"),
    (lambda rig: rig.fake.route[0].update(prefsrc="169.254.1.2"), "no primary non-loopback"),
    (_existing("unit"), "files the run would write already exist"),
    (_existing("drop-in"), "files the run would write already exist"),
    (lambda rig: (rig.fake.state_directory.mkdir(parents=True, mode=0o700),
                  rig.fake.run_directory.mkdir()), "already exists"),
])
def test_preflight_refusals_create_nothing(
    rig: Rig, change: Callable[[Rig], None], message: str,
) -> None:
    change(rig)

    assert rig() == 1, rig.output()
    assert rig.checks() == {"preflight": "failed"}
    assert message in rig.detail("preflight")
    for call in rig.fake.calls:
        assert any(call[: len(prefix)] == prefix for prefix in READ_ONLY), call
    assert not rig.record_path.exists()
    assert sorted(path.name for path in rig.fake.state_directory.iterdir()) in (
        ["lock", f"{RUN}.log"], ["lock", RUN, f"{RUN}.log"],
    )
    assert "result=failed" in rig.fake.stdout.getvalue()


def test_a_run_as_root_is_refused_before_anything(rig: Rig) -> None:
    rig.uid = 0
    assert rig() == 1
    assert "root" in rig.fake.stderr.getvalue()
    assert not rig.fake.calls


def test_a_relative_state_home_is_ignored_as_the_spec_says(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A relative XDG value is invalid and ignored: the default ~/.local/state is used, and
    # nothing is ever written beside the working directory.
    working = tmp_path / "working"
    working.mkdir()
    monkeypatch.chdir(working)
    rig.environ["XDG_STATE_HOME"] = "relative/state"

    assert rig() == 0, rig.output()
    init = rig.fake.installer_calls()[0]
    assert init[init.index("--state-home") + 1] == str(
        rig.fake.home / ".local" / "state" / "aegis-podman-deploy-check" / RUN / "state")
    assert list(working.iterdir()) == []


def test_a_shared_state_directory_is_refused(rig: Rig) -> None:
    rig.fake.state_directory.mkdir(parents=True, mode=0o755)
    rig.fake.state_directory.chmod(0o755)
    assert rig() == 1
    assert "must be a private (0700) directory you own" in rig.fake.stderr.getvalue()
    assert not rig.fake.calls


def test_the_record_and_run_directory_persist_under_the_state_home(
    rig: Rig, tmp_path: Path,
) -> None:
    # Not under $XDG_RUNTIME_DIR or /tmp, which a reboot empties.
    rig.fake.state_base = tmp_path / "xdg-state"
    rig.environ["XDG_STATE_HOME"] = str(rig.fake.state_base)
    seen: dict[str, Any] = {}

    def look(command: tuple[str, ...]) -> subprocess.CompletedProcess[str] | None:
        if command[2:3] == ("start",) and "modes" not in seen:
            fake = rig.fake
            seen["modes"] = {path.name: stat.S_IMODE(path.lstat().st_mode) for path in (
                fake.state_directory, rig.record_path, fake.run_directory, fake.state_home,
            )}
            seen["record"] = json.loads(rig.record_path.read_text(encoding="utf-8"))
        return None

    rig.fake.intercept = look
    assert rig() == 0, rig.output()
    assert seen["modes"] == {"aegis-podman-deploy-check": 0o700, f"{RUN}.json": 0o600,
                             RUN: 0o700, "state": 0o700}
    record = seen["record"]
    assert record["run_directory"] == str(tmp_path / "xdg-state" / "aegis-podman-deploy-check"
                                          / RUN)
    assert not any(str(rig.fake.runtime) in json.dumps(value) for value in record.values()
                   if not isinstance(value, dict))
    init = rig.fake.installer_calls()[0]
    assert init[init.index("--state-home") + 1] == str(rig.fake.state_home)
    assert sorted(path.name for path in rig.fake.state_directory.iterdir()) == [
        "lock", f"{RUN}.log",
    ]
    assert not (rig.fake.home / ".local").exists()


def test_an_earlier_record_must_be_cleaned_up_first(rig: Rig) -> None:
    directory = rig.fake.state_directory
    directory.mkdir(parents=True, mode=0o700)
    (directory / "t99999999.json").write_text("{}", encoding="utf-8")

    assert rig() == 1
    assert f"--cleanup {directory / 't99999999.json'}" in rig.fake.stderr.getvalue()
    assert not rig.fake.calls


def test_a_second_harness_is_refused_while_one_runs(rig: Rig) -> None:
    results = []

    def nested(command: tuple[str, ...]) -> subprocess.CompletedProcess[str] | None:
        if command[2:3] == ("init",):
            results.append(harness.main([], rig.machine()))
        return None

    rig.fake.intercept = nested
    assert rig() == 0, rig.output()
    assert results == [1]
    assert "another podman deploy check is running" in rig.fake.stderr.getvalue()


# -- the prefix ---------------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["aegis-", "aegis-t0a1b2c3-", "aegis-T0A1B2C3D-", "other-"])
def test_a_prefix_other_than_a_runs_own_is_refused(prefix: str) -> None:
    with pytest.raises(harness.HarnessError, match="never the production aegis-"):
        harness.checked_prefix(prefix)
    assert harness.checked_prefix(PREFIX) == PREFIX


@pytest.mark.parametrize("token", ["", "0a1b2c3", "0A1B2C3D", "0a1b2c3d-"])
def test_a_malformed_run_id_is_refused_before_anything(rig: Rig, token: str) -> None:
    rig.token = token
    assert rig() == 1
    assert not rig.fake.calls


# -- checks, one fault at a time --------------------------------------------------------------


@pytest.mark.parametrize(("fault", "failing", "message"), [
    ("install-section", {"not_boot_enabled"}, "has an [Install] section"),
    ("socket-enabled", {"not_boot_enabled"}, f"{PREFIX}caddy.socket is enabled"),
    ("restart-policy", {"no_restart_policy"}, "restart policy always"),
    ("published-gateway", {"gateway_unpublished"}, "publishes 8443/tcp"),
    ("tls", {"tls_local_ca", "throttle_per_address"}, "SSLCertVerificationError"),
    ("csrf-502", {"tls_local_ca"}, f"GET {harness.CSRF_PATH} answered 502"),
    ("shared-bucket", {"throttle_per_address"}, "one bucket for both"),
    ("source-mismatch", {"throttle_per_address"}, "left from"),
    ("writable-root", {"root_read_only"}, "gave created, not EROFS"),
    ("postgres-stuck", {"postgres_restart_recovers"}, "health is starting"),
    ("migrate-succeeds", {"migrate_failure_recovers"}, "completed without PostgreSQL"),
])
def test_each_check_fails_on_its_own_fault_and_cleanup_still_runs(
    rig: Rig, fault: str, failing: set[str], message: str,
) -> None:
    rig.fake.faults.add(fault)

    assert rig() == 1, rig.output()
    checks = rig.checks()
    assert {name for name, status in checks.items() if status == "failed"} == failing
    assert set(checks) == {*SETUP, *CHECKS, *FINAL}
    assert any(message in rig.detail(name) for name in failing)
    nothing_of_the_run_is_left(rig)
    never_boot_enabled(rig)


def test_the_throttle_check_drains_each_address_and_asks_the_other(rig: Rig) -> None:
    assert rig() == 0, rig.output()
    posts = [address for address, method, _ in rig.fake.requests if method == "POST"]
    # Loopback until limited (11 admitted, the 12th refused), then one from the host address.
    assert posts[:13] == ["127.0.0.1"] * 12 + [HOST_ADDRESS]
    # After a cooldown, the reverse.
    assert harness.THROTTLE_COOLDOWN in rig.fake.clock.slept
    reverse = posts[13:]
    assert reverse[-1] == "127.0.0.1" and set(reverse[:-1]) == {HOST_ADDRESS}
    detail = rig.detail("throttle_per_address")
    assert "loopback limited (503) after 12 POSTs, then the host address answered 403" in detail
    assert "the host address limited (503)" in detail and "then loopback answered 403" in detail
    assert HOST_ADDRESS not in detail


def test_the_postgres_check_waits_for_the_restart_to_happen(rig: Rig) -> None:
    assert rig() == 0, rig.output()
    # The restart is queued with --no-block; the old, healthy container must not count.
    assert "every unit active and healthy again after 5s" in rig.detail(
        "postgres_restart_recovers")


def test_the_migrate_check_restarts_migrate_without_its_requirements(rig: Rig) -> None:
    assert rig() == 0, rig.output()
    systemctl = rig.fake.commands(*harness.SYSTEMCTL, "--no-block")
    assert systemctl == [
        (*harness.SYSTEMCTL, "--no-block", "restart", f"{PREFIX}postgres.service"),
        (*harness.SYSTEMCTL, "--no-block", "stop", f"{PREFIX}postgres.service"),
        (*harness.SYSTEMCTL, "--no-block", "--job-mode=ignore-requirements", "start",
         f"{PREFIX}migrate.service"),
    ]


# -- check evaluation ---------------------------------------------------------------------------


def _states(layout: harness.Layout, **changes: tuple[str, str]) -> dict[str, dict[str, str]]:
    states = {unit: {"ActiveState": "active", "SubState": "running"} for unit in layout.services}
    states |= {unit: {"ActiveState": "active", "SubState": "exited"} for unit in layout.oneshots}
    states[layout.socket] = {"ActiveState": "active", "SubState": "listening"}
    for unit, (active, sub) in changes.items():
        states[f"{PREFIX}{unit}"] = {"ActiveState": active, "SubState": sub}
    return states


def test_units_are_healthy_only_when_every_state_and_health_check_says_so(rig: Rig) -> None:
    layout = rig.fake.layout
    healthy = {unit: {"State": {"Health": {"Status": "healthy"}}}
               for unit in layout.health_checked}

    assert harness.evaluate_units(layout, _states(layout), healthy) == []
    assert harness.evaluate_units(
        layout, _states(layout, **{"web.service": ("activating", "start")}), healthy,
    ) == [f"{PREFIX}web.service is activating/start"]
    assert harness.evaluate_units(
        layout, _states(layout, **{"migrate.service": ("activating", "auto-restart")}), healthy,
    ) == [f"{PREFIX}migrate.service is activating/auto-restart"]
    # While its service runs, systemd reports the socket as running.
    assert harness.evaluate_units(
        layout, _states(layout, **{"caddy.socket": ("active", "running")}), healthy,
    ) == []
    assert harness.evaluate_units(
        layout, _states(layout, **{"caddy.socket": ("failed", "failed")}), healthy,
    ) == [f"{PREFIX}caddy.socket is failed/failed"]
    starting = healthy | {f"{PREFIX}indexer.service": {"State": {"Health": {"Status": "starting"}}}}
    assert harness.evaluate_units(layout, _states(layout), starting) == [
        f"{PREFIX}indexer.service health is starting",
    ]
    missing = healthy | {f"{PREFIX}postgres.service": None}
    assert harness.evaluate_units(layout, _states(layout), missing) == [
        f"{PREFIX}postgres.service health is absent",
    ]


def test_the_layout_follows_the_templates(rig: Rig) -> None:
    layout = rig.fake.layout
    quadlet = rig.fake.config / "containers" / "systemd"
    assert layout.socket == f"{PREFIX}caddy.socket"
    assert f"{rig.fake.config}/systemd/user/{PREFIX}caddy.socket" in layout.unit_files
    assert str(quadlet / f"{PREFIX}web.container") in layout.unit_files
    assert layout.oneshots == (f"{PREFIX}migrate.service",)
    assert f"{PREFIX}caddy.service" in layout.services
    assert f"{PREFIX}caddy.service" not in layout.health_checked
    assert f"{PREFIX}gateway.service" in layout.health_checked
    assert layout.containers[f"{PREFIX}postgres.service"] == f"{PREFIX}postgres"
    assert f"{PREFIX}postgres-data" in layout.volumes and f"{PREFIX}tls-hop" in layout.networks
    assert f"{PREFIX}backend-network.service" in layout.generated
    assert str(quadlet / f"{PREFIX}indexer.container.d") in layout.drop_in_directories
    assert all(unit.startswith(PREFIX) for unit in layout.units)


@pytest.mark.parametrize(("entry", "published"), [
    ({}, []),
    ({"HostConfig": {"PortBindings": None}, "NetworkSettings": {"Ports": None}}, []),
    ({"HostConfig": {"PortBindings": {}}, "NetworkSettings": {"Ports": {"8080/tcp": None}}}, []),
    ({"HostConfig": {"PortBindings": {"8081/tcp": [{"HostPort": "18081"}]}}}, ["8081/tcp"]),
    ({"NetworkSettings": {"Ports": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "1"}]}}},
     ["8080/tcp"]),
])
def test_published_ports_read_both_bindings_and_settings(
    entry: dict[str, Any], published: list[str],
) -> None:
    assert harness.published_ports(entry) == published


def test_install_sections_and_enablement_links_are_found(tmp_path: Path, rig: Rig) -> None:
    unit = tmp_path / "x.socket"
    unit.write_text("[Socket]\nListenStream=1\n\n[Install]\nWantedBy=sockets.target\n")
    drop_ins = tmp_path / "x.container.d"
    drop_ins.mkdir()
    (drop_ins / "50-aegis-roots.conf").write_text("[Container]\nVolume=/a:/b:ro\n")
    clean = tmp_path / "y.container"
    clean.write_text("[Container]\nImage=x\n# [Install] only in a comment\n")
    assert harness.install_sections([str(unit), str(drop_ins), str(clean)]) == ["x.socket"]

    wants = rig.fake.runtime / "systemd" / "generator" / "default.target.wants"
    wants.mkdir(parents=True)
    (wants / f"{PREFIX}web.service").symlink_to("/nonexistent")
    (wants / "other.service").symlink_to("/nonexistent")
    links = harness.enablement_links(rig.machine(), rig.fake.config, rig.fake.layout.units)
    assert links == [f"default.target.wants/{PREFIX}web.service"]


def test_a_wants_link_fails_the_boot_check(rig: Rig) -> None:
    wants = rig.fake.config / "systemd" / "user" / "sockets.target.wants"

    def link(command: tuple[str, ...]) -> subprocess.CompletedProcess[str] | None:
        if command[2:3] == ("start",) and not wants.exists():  # the first start only
            wants.mkdir(parents=True)
            (wants / f"{PREFIX}caddy.socket").symlink_to("/nonexistent")
        return None

    rig.fake.intercept = link
    assert rig() == 1, rig.output()
    assert rig.checks()["not_boot_enabled"] == "failed"
    assert f"link sockets.target.wants/{PREFIX}caddy.socket" in rig.detail("not_boot_enabled")


def test_unit_states_parse_blocks_and_default_to_not_found() -> None:
    text = ("Id=a.service\nLoadState=loaded\nActiveState=active\nSubState=running\n\n"
            "Id=b.service\nLoadState=loaded\nActiveState=activating\nJob=17\n")
    states = harness.parse_unit_states(text, ["a.service", "b.service", "c.service"])
    assert states["a.service"]["Job"] == "" and states["a.service"]["SubState"] == "running"
    assert states["b.service"]["Job"] == "17"
    assert states["c.service"]["LoadState"] == "not-found"
    assert states["c.service"]["ActiveState"] == "inactive"


@pytest.mark.parametrize(("output", "address"), [
    (json.dumps([{"dst": "192.0.2.1", "prefsrc": "198.51.100.7"}]), "198.51.100.7"),
    (json.dumps([{"dst": "192.0.2.1", "prefsrc": "127.0.0.1"}]), None),
    (json.dumps([{"dst": "192.0.2.1", "prefsrc": "169.254.0.9"}]), None),
    (json.dumps([{"dst": "192.0.2.1", "prefsrc": "0.0.0.0"}]), None),
    (json.dumps([{"dst": "192.0.2.1", "prefsrc": "2001:db8::1"}]), None),
    (json.dumps([{"dst": "192.0.2.1"}]), None),
    (json.dumps([]), None),
    ("not json", None),
])
def test_the_host_address_is_the_routes_preferred_ipv4_source(
    output: str, address: str | None,
) -> None:
    assert harness.preferred_source(output) == address


def test_versions_and_port_mappings_are_read() -> None:
    assert harness.systemd_version("systemd 257 (257.9-1.fc44)\n+PAM") == 257
    assert harness.systemd_version("systemd") is None
    assert harness.mapped_ports([
        {"Ports": [{"host_port": 8000, "range": 3}, {"host_port": "bad"}]}, {"Ports": None},
    ]) == {8000, 8001, 8002}


def test_a_check_line_has_one_line_and_a_fixed_form() -> None:
    out = io.StringIO()
    report = harness.Report(harness.Machine(out=out, err=io.StringIO()))
    report.check("tls_local_ca", True, "first\nsecond   third")
    report.check("root_read_only", False)
    assert out.getvalue().splitlines() == [
        "AEGIS_PODMAN_DEPLOY check=tls_local_ca status=passed first second third",
        "AEGIS_PODMAN_DEPLOY check=root_read_only status=failed",
    ]
    assert not report.passed


# -- cleanup on every exit path ---------------------------------------------------------------


def test_a_failed_start_still_stops_and_purges_everything(rig: Rig) -> None:
    rig.fake.faults.add("start-fails")

    assert rig() == 1, rig.output()
    checks = rig.checks()
    assert checks["install"] == "failed" and "start: aegis-podman: error" in rig.detail("install")
    assert all(checks[name] == "failed" for name in CHECKS)
    assert "not run" in rig.detail("units_healthy")
    assert checks["cleanup"] == checks["preexisting_unchanged"] == "passed"
    verbs = [call[2] for call in rig.fake.installer_calls()]
    assert verbs[-2:] == ["stop", "uninstall"]
    nothing_of_the_run_is_left(rig)
    # The installer's own output goes to the operator.
    assert "| aegis-podman: error: start refused (fake)" in rig.fake.stderr.getvalue()


def test_a_failed_build_removes_the_images_already_built(rig: Rig) -> None:
    rig.fake.faults.add("build-gateway-fails")

    assert rig() == 1, rig.output()
    assert rig.checks()["build_images"] == "failed"
    assert not rig.fake.installer_calls()
    assert rig.checks()["cleanup"] == "passed"
    nothing_of_the_run_is_left(rig)


def test_an_interrupt_cleans_up_and_exits_130(rig: Rig) -> None:
    def interrupt(command: tuple[str, ...]) -> subprocess.CompletedProcess[str] | None:
        if command[2:3] == ("start",):
            raise KeyboardInterrupt
        return None

    rig.fake.intercept = interrupt
    assert rig() == 130, rig.output()
    assert rig.checks()["cleanup"] == "passed"
    assert "result=interrupted" in rig.fake.stdout.getvalue()
    assert [call[2] for call in rig.fake.installer_calls()][-2:] == ["stop", "uninstall"]
    nothing_of_the_run_is_left(rig)


def test_sigterm_cleans_up_exits_143_and_restores_the_handlers(rig: Rig) -> None:
    before = {signum: signal.getsignal(signum)
              for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    seen: dict[str, Any] = {}

    def terminate(command: tuple[str, ...]) -> subprocess.CompletedProcess[str] | None:
        if command[2:4] == ("roots", "add"):
            signal.raise_signal(signal.SIGTERM)
        if command[2:3] == ("uninstall",):
            # Cleanup is not cut short, and what it runs inherits that.
            seen["handlers"] = {signal.getsignal(signum)
                                for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        return None

    rig.fake.intercept = terminate
    assert rig() == 143, rig.output()
    assert seen["handlers"] == {signal.SIG_IGN}
    assert rig.checks()["cleanup"] == "passed"
    nothing_of_the_run_is_left(rig)
    assert {signum: signal.getsignal(signum) for signum in before} == before


def test_an_unexpected_error_still_cleans_up(rig: Rig) -> None:
    def explode(command: tuple[str, ...]) -> subprocess.CompletedProcess[str] | None:
        if command[2:3] == ("install",):
            raise RuntimeError("a harness bug")
        return None

    rig.fake.intercept = explode
    assert rig() == 1, rig.output()
    assert "RuntimeError: a harness bug" in rig.detail("harness")
    assert rig.checks()["cleanup"] == "passed"
    nothing_of_the_run_is_left(rig)


def test_a_listener_left_on_the_test_port_is_a_leftover(rig: Rig) -> None:
    def keep_listening(command: tuple[str, ...]) -> subprocess.CompletedProcess[str] | None:
        if command[2:3] == ("uninstall",):
            result = rig.fake.installer(command, command[2:], dict(rig.fake.environments[command]
                                                                  or {}))
            rig.fake.listeners.add(TEST_PORT)
            return result
        return None

    rig.fake.intercept = keep_listening
    assert rig() == 1, rig.output()
    assert f"left: port {TEST_PORT} still has a listener" in rig.detail("cleanup")
    assert rig.record_path.exists()


def test_unit_directories_the_run_caused_go_again_and_others_stay(rig: Rig) -> None:
    config = rig.fake.config
    (config / "containers").mkdir(parents=True)
    (config / "systemd" / "user").mkdir(parents=True)
    (config / "systemd" / "user" / "existing.service").write_text("[Unit]\n", encoding="utf-8")

    assert rig() == 0, rig.output()
    # containers/ was there, empty: it stays; containers/systemd/ came with the run: it goes.
    assert (config / "containers").is_dir() and not (config / "containers" / "systemd").exists()
    assert sorted(path.name for path in (config / "systemd" / "user").iterdir()) == [
        "existing.service",
    ]


def test_failed_units_are_cleared_by_their_exact_names(rig: Rig) -> None:
    assert rig() == 0, rig.output()
    assert rig.fake.commands(*harness.SYSTEMCTL, "reset-failed") == [
        (*harness.SYSTEMCTL, "reset-failed", *rig.fake.layout.units),
    ]


class DeadTerminal(io.StringIO):
    """A terminal that hung up (EIO) or a reader that went away (EPIPE)."""

    def __init__(self, code: int) -> None:
        super().__init__()
        self.code = code

    def write(self, text: str) -> int:
        raise OSError(self.code, os.strerror(self.code))

    def flush(self) -> None:
        raise OSError(self.code, os.strerror(self.code))


def log_lines(rig: Rig) -> list[str]:
    log = rig.fake.state_directory / f"{RUN}.log"
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    return log.read_text(encoding="utf-8").splitlines()


def test_every_line_also_goes_to_the_runs_log(rig: Rig) -> None:
    assert rig() == 0, rig.output()
    lines = log_lines(rig)
    printed = rig.fake.stdout.getvalue().splitlines() + rig.fake.stderr.getvalue().splitlines()
    assert sorted(lines) == sorted(printed)
    assert lines[-1].startswith("AEGIS_PODMAN_DEPLOY result=passed")


@pytest.mark.parametrize("code", [errno.EIO, errno.EPIPE])
def test_output_that_cannot_be_written_never_stops_the_cleanup(rig: Rig, code: int) -> None:
    rig.fake.stdout, rig.fake.stderr = DeadTerminal(code), DeadTerminal(code)

    assert rig() == 0
    nothing_of_the_run_is_left(rig)
    assert "AEGIS_PODMAN_DEPLOY check=cleanup status=passed nothing of the run is left" \
        in log_lines(rig)


def test_a_hang_up_with_a_dead_terminal_still_cleans_up(rig: Rig) -> None:
    rig.fake.stdout, rig.fake.stderr = DeadTerminal(errno.EIO), DeadTerminal(errno.EIO)

    def hang_up(command: tuple[str, ...]) -> subprocess.CompletedProcess[str] | None:
        if command[2:3] == ("start",):
            signal.raise_signal(signal.SIGHUP)
        return None

    rig.fake.intercept = hang_up
    assert rig() == 129
    assert [call[2] for call in rig.fake.installer_calls()][-2:] == ["stop", "uninstall"]
    nothing_of_the_run_is_left(rig)
    assert log_lines(rig)[-1].startswith("AEGIS_PODMAN_DEPLOY result=interrupted")


def test_signals_go_straight_from_ending_the_run_to_ignored(
    rig: Rig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No moment between the run and its cleanup in which SIGTERM or SIGHUP would kill the
    # harness outright.
    settings: list[tuple[int, Any]] = []
    original = signal.signal

    def recording(signum: int, handler: Any) -> Any:
        settings.append((signum, handler))
        return original(signum, handler)

    monkeypatch.setattr(signal, "signal", recording)
    before = signal.getsignal(signal.SIGTERM)
    assert rig() == 0, rig.output()
    for signum in (signal.SIGTERM, signal.SIGHUP):
        handlers = [handler for number, handler in settings if number == signum]
        assert handlers[:-1] == [harness._raise_interrupted, signal.SIG_IGN], handlers
    assert signal.getsignal(signal.SIGTERM) == before


def test_what_the_builds_leave_is_reported_not_removed(rig: Rig) -> None:
    assert rig() == 0, rig.output()
    caches = [identifier for identifier, image in rig.fake.images.items()
              if not image["tags"] and not image["labels"]]
    assert len(caches) == 4
    assert "kept, as any build's: 4 new images (build cache)" in rig.fake.stderr.getvalue()


def test_unit_files_of_earlier_runs_are_reported_and_left_alone(rig: Rig) -> None:
    quadlet = rig.fake.config / "containers" / "systemd"
    quadlet.mkdir(parents=True)
    earlier = quadlet / "aegis-t99999999-web.container"
    earlier.write_text("[Container]\nImage=x\n", encoding="utf-8")

    assert rig() == 0, rig.output()
    assert ("warning: unit files of another deploy-check run, never removed by this one: "
            f"{earlier}") in rig.fake.stderr.getvalue()
    assert earlier.read_text(encoding="utf-8") == "[Container]\nImage=x\n"


def test_files_the_container_users_own_go_through_podman_unshare(rig: Rig) -> None:
    rig.fake.faults.add("unremovable-file")

    assert rig() == 0, rig.output()
    assert rig.fake.commands(*harness.PODMAN, "unshare") == [
        (*harness.PODMAN, "unshare", "rm", "-rf", "--one-file-system", "--",
         str(rig.fake.run_directory)),
    ]
    nothing_of_the_run_is_left(rig)


def test_a_changed_preexisting_object_fails_the_run_but_never_blocks_the_next(
    rig: Rig,
) -> None:
    rig.fake.faults.add("lose-preexisting-volume")

    assert rig() == 1, rig.output()
    assert rig.checks()["preexisting_unchanged"] == "failed"
    assert rig.checks()["cleanup"] == "passed"
    assert "pre-existing volumes home-data disappeared" in rig.detail("preexisting_unchanged")
    # Nothing of the run is left, so its record goes: the change is reported, not held.
    assert not rig.record_path.exists()
    assert "the run left nothing behind" in rig.fake.stderr.getvalue()


def test_an_image_id_that_existed_before_is_only_untagged(rig: Rig) -> None:
    rig.fake.faults.add("preexisting-image")

    assert rig() == 0, rig.output()
    tag = harness.image_tag("caddy", RUN)
    assert (*harness.PODMAN, "untag", PREEXISTING_IMAGE, tag) in rig.fake.calls
    assert not [call for call in rig.fake.commands(*harness.PODMAN, "image", "rm")
                if PREEXISTING_IMAGE in call]
    assert rig.fake.images[PREEXISTING_IMAGE]["tags"] == ["registry.example.test/home:1"]


def test_a_tag_without_the_runs_label_is_never_removed(rig: Rig) -> None:
    rig.fake.faults.add("foreign-tag")

    assert rig() == 1, rig.output()
    assert rig.checks()["build_images"] == "failed"
    foreign = rig.fake.images["f" * 64]
    assert foreign["tags"] == [harness.image_tag("gateway", RUN)]
    assert not [call for call in rig.fake.calls if "f" * 64 in call]
    assert rig.checks()["cleanup"] == "failed"
    assert "left: image localhost/aegis-gateway" in rig.detail("cleanup")


# -- leftovers and --cleanup --------------------------------------------------------------------


def test_leftovers_fail_the_run_keep_the_record_and_cleanup_finishes_later(rig: Rig) -> None:
    rig.fake.faults.add("uninstall-refuses")

    assert rig() == 1, rig.output()
    detail = rig.detail("cleanup")
    assert "aegis-podman uninstall --purge-data: aegis-podman: error" in detail
    assert f"left: file {PREFIX}backend.network" in detail
    assert f"left: file {PREFIX}indexer.container.d" in detail
    assert f"left: volume {PREFIX}postgres-data" in detail
    assert f"left: network {PREFIX}tls-hop" in detail
    assert f"left: unit {PREFIX}postgres.service is loaded/inactive" in detail
    assert f"kept {rig.fake.run_directory}: the installer's record is still in it" in detail
    assert rig.record_path.exists()
    assert f"--cleanup {rig.record_path}" in rig.fake.stderr.getvalue()
    assert rig.fake.state_home.exists()

    rig.fake.faults.clear()
    rig.fake.stdout, rig.fake.stderr = io.StringIO(), io.StringIO()
    assert rig("--cleanup", str(rig.record_path)) == 0, rig.output()
    assert rig.checks() == {"cleanup": "passed", "preexisting_unchanged": "passed"}
    nothing_of_the_run_is_left(rig)
    never_boot_enabled(rig)


def test_cleanup_of_a_record_is_not_cut_short_by_signals(rig: Rig) -> None:
    interrupted_record(rig)
    seen: dict[str, Any] = {}

    def look(command: tuple[str, ...]) -> subprocess.CompletedProcess[str] | None:
        if command[2:3] == ("uninstall",):
            seen["handlers"] = {signal.getsignal(signum)
                                for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        return None

    rig.fake.intercept = look
    before = signal.getsignal(signal.SIGTERM)
    assert rig("--cleanup", str(rig.record_path)) == 0, rig.output()
    assert seen["handlers"] == {signal.SIG_IGN}
    assert signal.getsignal(signal.SIGTERM) == before


def interrupted_record(rig: Rig) -> dict[str, Any]:
    """A run interrupted before cleanup could finish: its record stays behind."""
    def interrupt(command: tuple[str, ...]) -> subprocess.CompletedProcess[str] | None:
        if command[2:3] == ("start",):
            raise KeyboardInterrupt
        if command[2:3] == ("uninstall",):
            return completed(command, 1, "", "aegis-podman: error: interrupted (fake)")
        return None

    rig.fake.intercept = interrupt
    assert rig() == 130
    rig.fake.intercept = None
    rig.fake.calls.clear()
    rig.fake.stdout, rig.fake.stderr = io.StringIO(), io.StringIO()
    loaded: dict[str, Any] = json.loads(rig.record_path.read_text(encoding="utf-8"))
    return loaded


@pytest.mark.parametrize(("damage", "message"), [
    ({"prefix": "aegis-"}, "never the production aegis-"),
    ({"prefix": "aegis-t99999999-"}, "does not belong to the run"),
    ({"run_directory": "/tmp/aegis-podman-deploy.t0a1b2c3d"}, "not the run's own"),
    ({"directory_identity": [1, 2]}, "malformed directory identity"),
    ({"unit_files": ["/etc/systemd/user/aegis-t0a1b2c3d-x.service"]}, "malformed unit_files"),
    ({"containers": ["home-service"]}, "malformed containers"),
    ({"volumes": ["home-data"]}, "malformed volumes"),
    ({"networks": ["aegis-backend"]}, "malformed networks"),
    ({"images": {}}, "malformed images"),
    ({"created_directories": ["/home"]}, "malformed created directories"),
    ({"extra": 1}, "unknown fields"),
])
def test_cleanup_refuses_a_record_that_names_anything_but_the_runs_own(
    rig: Rig, damage: dict[str, Any], message: str,
) -> None:
    record = interrupted_record(rig)
    rig.record_path.write_text(json.dumps(record | damage), encoding="utf-8")

    assert rig("--cleanup", str(rig.record_path)) == 1
    assert message in rig.fake.stderr.getvalue()
    assert not rig.fake.calls


def test_cleanup_refuses_a_record_outside_its_directory_or_shared(
    rig: Rig, tmp_path: Path,
) -> None:
    record = interrupted_record(rig)
    elsewhere = tmp_path / f"{RUN}.json"
    elsewhere.write_text(json.dumps(record), encoding="utf-8")
    elsewhere.chmod(0o600)
    assert rig("--cleanup", str(elsewhere)) == 1
    rig.record_path.chmod(0o644)
    assert rig("--cleanup", str(rig.record_path)) == 1
    assert "is not a record in" in rig.fake.stderr.getvalue()
    assert "must be a private (0600) regular file" in rig.fake.stderr.getvalue()
    assert not rig.fake.calls


def test_cleanup_finishes_after_a_reboot(rig: Rig) -> None:
    interrupted_record(rig)
    rig.fake.reboot()

    assert rig("--cleanup", str(rig.record_path)) == 0, rig.output()
    assert rig.checks() == {"cleanup": "passed", "preexisting_unchanged": "passed"}
    nothing_of_the_run_is_left(rig)
    never_boot_enabled(rig)


def test_cleanup_accepts_pieces_that_are_already_gone(rig: Rig) -> None:
    def interrupted(command: tuple[str, ...]) -> subprocess.CompletedProcess[str] | None:
        if command[2:3] == ("ca-cert",):
            raise KeyboardInterrupt
        if command[2:3] == ("uninstall",):
            return completed(command, 1, "", "aegis-podman: error: interrupted (fake)")
        if command[2:5] == ("image", "rm", "--no-prune"):
            return completed(command, 2, "", "Error: image is in use by a container")
        return None

    rig.fake.intercept = interrupted
    assert rig() == 130
    rig.fake.intercept = None
    rig.fake.stdout, rig.fake.stderr = io.StringIO(), io.StringIO()
    fake = rig.fake
    # Removed by hand meanwhile: a unit file, a container, a volume and an image.
    Path(fake.layout.unit_files[0]).unlink()
    fake.containers.pop(f"{PREFIX}web", None)
    fake.volumes.pop(f"{PREFIX}staging")
    backend = hashlib.sha256(harness.image_tag("backend", RUN).encode()).hexdigest()
    del fake.images[backend]
    fake.calls.clear()

    assert rig("--cleanup", str(rig.record_path)) == 0, rig.output()
    nothing_of_the_run_is_left(rig)
    assert (*harness.PODMAN, "image", "rm", "--no-prune", backend) not in fake.calls


def _foreign_tag(rig: Rig) -> None:
    rig.fake.images["e" * 64] = {"tags": [harness.image_tag("gateway", RUN)], "labels": {},
                                 "created": "2026-10-11T00:00:00Z"}


def _foreign_container(rig: Rig) -> None:
    name = f"{PREFIX}web"
    rig.fake.containers[name] = {
        "Name": name, "Id": "d" * 64, "Names": [name], "ImageID": PREEXISTING_IMAGE,
        "Created": 5, "Config": {"Labels": {"PODMAN_SYSTEMD_UNIT": "other.service"}},
    }


def _other_token(rig: Rig) -> None:
    (rig.fake.run_directory / ".aegis-deploy-check-run").write_text("0" * 32 + "\n")


def _other_installer_record(rig: Rig) -> None:
    (rig.fake.state_home / "state.json").write_text(json.dumps({"prefix": "aegis-"}))


def _other_directory(rig: Rig) -> None:
    moved = rig.fake.run_directory.with_name("moved")
    rig.fake.run_directory.rename(moved)
    rig.fake.run_directory.mkdir(mode=0o700)
    (moved / ".aegis-deploy-check-run").rename(
        rig.fake.run_directory / ".aegis-deploy-check-run")


@pytest.mark.parametrize(("damage", "message"), [
    (_foreign_tag, "names an image without this run's label"),
    (_foreign_container, f"the container {PREFIX}web does not belong to the unit"),
    (_other_token, "is not the directory this run created"),
    (_other_directory, "is not the directory this run created"),
    (_other_installer_record, f"is not the installer's record for {PREFIX}"),
])
def test_cleanup_refuses_an_identity_mismatch_before_changing_anything(
    rig: Rig, damage: Callable[[Rig], None], message: str,
) -> None:
    interrupted_record(rig)
    rig.fake.reboot()
    damage(rig)

    assert rig("--cleanup", str(rig.record_path)) == 1
    detail = rig.detail("cleanup")
    assert "refused, nothing was changed" in detail and message in detail
    assert not rig.fake.installer_calls()
    for verb in ("untag", "unshare"):
        assert not rig.fake.commands(*harness.PODMAN, verb)
    assert not rig.fake.commands(*harness.PODMAN, "image", "rm")
    assert not rig.fake.commands(*harness.SYSTEMCTL, "reset-failed")
    assert rig.record_path.exists() and rig.fake.run_directory.exists()


def test_cleanup_never_removes_a_directory_the_run_did_not_create(rig: Rig) -> None:
    record = interrupted_record(rig)
    record["directory_identity"] = {"inode": 1, "token": "0" * 32}
    rig.record_path.write_text(json.dumps(record), encoding="utf-8")

    assert rig("--cleanup", str(rig.record_path)) == 1
    assert "is not the directory this run created; left in place" in rig.detail("cleanup")
    assert rig.fake.run_directory.exists() and rig.record_path.exists()
    assert not rig.fake.commands(*harness.PODMAN, "unshare")


def test_the_makefile_target_runs_the_harness_with_the_locked_environment() -> None:
    makefile = (harness.REPOSITORY / "Makefile").read_text(encoding="utf-8")
    assert "verify-podman-deploy" in makefile.splitlines()[0].split()
    assert re.search(r"^verify-podman-deploy:\n\tuv run --locked python "
                     r"scripts/podman_deploy_check\.py\n", makefile, re.MULTILINE)
