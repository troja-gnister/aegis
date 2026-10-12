"""Load the standalone Podman installer and read the Quadlet units it renders."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[2]
INSTALLER = REPOSITORY / "deploy" / "podman" / "aegis-podman"
MODULE_NAME = "aegis_podman_installer"
QUADLET = Path("/usr/libexec/podman/quadlet")

# Synthetic install values; none of them names a real host, address or registry.
SAMPLE_PREFIX = "aegis-"
SAMPLE_RELEASE = "sha-" + "0123456789abcdef" * 2 + "01234567"
SAMPLE_PORT = 9443
SAMPLE_NAMES = ("aegis.example.test", "192.0.2.10")
SAMPLE_HOST_GID = 4321
SAMPLE_READER_TYPE = "aegis_reader.process"
SAMPLE_SECRETS_DIR = "/home/aegis-operator/.local/share/aegis/secrets"
SAMPLE_BACKEND_SUBNET = "10.123.45.0/24"
SAMPLE_INSTALL_TOKEN = "5a17" * 8
SAMPLE_IMAGES = {
    role: f"registry.example.test/aegis-{role}@sha256:{digit * 64}"
    for role, digit in (
        ("backend", "a"), ("gateway", "b"), ("postgres", "c"), ("caddy", "d"), ("media", "f"),
    )
}
# A synthetic operator directory for the derivatives volume's bind form (spec M8).
SAMPLE_DERIVATIVES_DIR = "/srv/aegis-operator/derivatives"
# A synthetic install's shared SELinux MCS level (the installer draws one at random).
SAMPLE_LABEL_LEVEL = "s0:c17,c912"
# A synthetic install's scan interval: daily, unlike compose's hourly development default.
SAMPLE_SCAN_INTERVAL_SECONDS = 86400
# Where the fake Podman keeps the operator's named volumes (podman info: store.volumePath).
SAMPLE_VOLUME_STORAGE = "/home/aegis-operator/.local/share/containers/storage/volumes"


def load_installer() -> ModuleType:
    """Import the extension-less installer once, the way the host's python3 runs it."""
    loaded = sys.modules.get(MODULE_NAME)
    if loaded is not None:
        return loaded
    loader = SourceFileLoader(MODULE_NAME, str(INSTALLER))
    spec = importlib.util.spec_from_loader(MODULE_NAME, loader)
    if spec is None:
        raise ImportError(f"cannot load {INSTALLER}")
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve string annotations through sys.modules during class creation.
    sys.modules[MODULE_NAME] = module
    try:
        loader.exec_module(module)
    except BaseException:
        del sys.modules[MODULE_NAME]
        raise
    return module


def sample_config(**overrides: Any) -> Any:
    installer = load_installer()
    fields: dict[str, Any] = {
        "prefix": SAMPLE_PREFIX,
        "release": SAMPLE_RELEASE,
        "https_port": SAMPLE_PORT,
        "names": SAMPLE_NAMES,
        "host_gid": SAMPLE_HOST_GID,
        "reader_type": SAMPLE_READER_TYPE,
        "secrets_dir": SAMPLE_SECRETS_DIR,
        "backend_subnet": SAMPLE_BACKEND_SUBNET,
        "install_token": SAMPLE_INSTALL_TOKEN,
        "label_level": SAMPLE_LABEL_LEVEL,
        "scan_interval_seconds": SAMPLE_SCAN_INTERVAL_SECONDS,
        **{f"image_{role}": image for role, image in SAMPLE_IMAGES.items()},
    }
    return installer.UnitConfig(**(fields | overrides))


@dataclass(frozen=True)
class Unit:
    """One systemd unit file: each section keeps its keys in order, duplicates included."""

    sections: dict[str, list[tuple[str, str]]]

    def values(self, section: str, key: str) -> list[str]:
        return [value for name, value in self.sections.get(section, []) if name == key]

    def value(self, section: str, key: str) -> str | None:
        values = self.values(section, key)
        if len(values) > 1:
            raise AssertionError(f"[{section}] {key} is set {len(values)} times")
        return values[0] if values else None

    def keys(self, section: str) -> set[str]:
        return {name for name, _ in self.sections.get(section, [])}


def parse_unit(text: str) -> Unit:
    """Parse the unit-file subset the templates use; anything else fails loudly."""
    sections: dict[str, list[tuple[str, str]]] = {}
    current: list[tuple[str, str]] | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if stripped.endswith("\\"):
            raise AssertionError(f"line {number}: continuation lines are not used")
        if stripped.startswith("[") and stripped.endswith("]"):
            name = stripped[1:-1]
            if name in sections:
                raise AssertionError(f"line {number}: section [{name}] repeats")
            current = sections[name] = []
            continue
        key, separator, value = stripped.partition("=")
        if current is None or not separator or not key.strip():
            raise AssertionError(f"line {number}: not a key=value line in a section")
        current.append((key.strip(), value.strip()))
    return Unit(sections)


def systemd_words(command: str) -> list[str]:
    """Split a systemd command line the way systemd does for ExecStart= and Exec=."""
    escapes = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
               "\\": "\\", '"': '"', "'": "'", "s": " "}
    words: list[str] = []
    word: list[str] = []
    quote = ""
    started = False
    index = 0
    while index < len(command):
        character = command[index]
        if character == "\\" and index + 1 < len(command):
            following = command[index + 1]
            if following == "x":
                word.append(chr(int(command[index + 2:index + 4], 16)))
                index += 4
            else:
                word.append(escapes[following])
                index += 2
            started = True
            continue
        if quote:
            if character == quote:
                quote = ""
            else:
                word.append(character)
        elif character in "\"'":
            quote = character
            started = True
        elif character.isspace():
            if started:
                words.append("".join(word))
                word, started = [], False
        else:
            word.append(character)
            started = True
        index += 1
    if quote:
        raise AssertionError(f"unterminated quote in {command!r}")
    if started:
        words.append("".join(word))
    return words


def network_line(line: str) -> tuple[str, list[str], dict[str, str]]:
    """Split Network=<unit>.network:alias=a,alias=b,ip=x into reference, aliases, options."""
    reference, _, joined = line.partition(":")
    aliases: list[str] = []
    options: dict[str, str] = {}
    for option in joined.split(",") if joined else []:
        key, separator, value = option.partition("=")
        if not separator or (key != "alias" and key in options):
            raise AssertionError(f"Network={line} has a malformed or repeated option")
        if key == "alias":
            aliases.append(value)
        else:
            options[key] = value
    return reference, aliases, options


def environment_assignments(unit: Unit) -> dict[str, str]:
    """Read Environment= lines; each template line carries exactly one assignment."""
    assignments: dict[str, str] = {}
    for raw in unit.values("Container", "Environment"):
        words = systemd_words(raw)
        if len(words) != 1:
            raise AssertionError(f"Environment={raw} must hold one assignment")
        key, separator, value = words[0].partition("=")
        if not separator or key in assignments:
            raise AssertionError(f"Environment={raw} is malformed or repeats {key}")
        assignments[key] = value
    return assignments


def quadlet_dryrun(units: Mapping[str, str], directory: Path) -> subprocess.CompletedProcess[str]:
    """Run Podman's Quadlet generator over only these units; it writes nothing."""
    for name, text in units.items():
        (directory / name).write_text(text, encoding="utf-8")
    environment = dict(os.environ) | {"QUADLET_UNIT_DIRS": str(directory)}
    return subprocess.run(
        [str(QUADLET), "-user", "-dryrun"], env=environment, check=False,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
    )


def generated_services(output: str) -> dict[str, Unit]:
    """Split Quadlet's dry-run output into its generated services."""
    services: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in output.splitlines():
        if line.startswith("---") and line.endswith("---") and len(line) > 6:
            current = services[line[3:-3]] = []
        elif current is not None:
            current.append(line)
    return {name: parse_unit("\n".join(lines)) for name, lines in services.items()}


def exec_start_arguments(service: Unit) -> list[str]:
    """ExecStart= as argv, with each --flag=value split into two words."""
    command = service.value("Service", "ExecStart")
    if command is None:
        raise AssertionError("generated service has no ExecStart")
    arguments: list[str] = []
    for word in systemd_words(command):
        flag, separator, value = word.partition("=")
        if word.startswith("--") and separator:
            arguments.extend((flag, value))
        else:
            arguments.append(word)
    return arguments


# The installer's host side, faked: every program it runs answers from in-memory state here.
SAMPLE_REGISTRY = "registry.example.test/aegis"
SAMPLE_OTHER_RELEASE = "sha-" + "fedcba9876543210" * 2 + "fedcba98"
SAMPLE_THIRD_RELEASE = "sha-" + "0f1e2d3c4b5a6978" * 2 + "0f1e2d3c"
# Commit times, in release order: SAMPLE, then OTHER, then THIRD.
SAMPLE_CREATED = {
    SAMPLE_RELEASE: "2026-10-01T09:00:00Z",
    SAMPLE_OTHER_RELEASE: "2026-10-05T12:30:00+02:00",
    SAMPLE_THIRD_RELEASE: "2026-10-08T08:15:42.123456789Z",
}
RELEASE_SOURCE_TREE = REPOSITORY / "deploy" / "podman"
# The backend image carries the aegisctl package the installer runs on the host (roots).
LIBRARY_SOURCE_TREE = REPOSITORY / "backend" / "aegisctl"
PODMAN = ("podman", "--remote=false")
# How the installer runs the release's aegisctl on the host Python.
AEGISCTL = (sys.executable, "-B", "-P", "-S", "-m", "aegisctl")
SYSTEMCTL = ("systemctl", "--user")
# How the installer labels what the containers read: container_file_t at level s0.
CONTAINER_FILE_LABEL = ("chcon", "-h", "-t", "container_file_t", "-l", "s0")
# A structurally valid stand-in for Caddy's root certificate: one DER SEQUENCE, PEM-armored.
SAMPLE_CERTIFICATE_DER = bytes([0x30, 0x82, 0x01, 0x00]) + bytes(range(256))


def sample_certificate() -> str:
    body = base64.b64encode(SAMPLE_CERTIFICATE_DER).decode("ascii")
    lines = "\n".join(textwrap.wrap(body, 64))
    return f"-----BEGIN CERTIFICATE-----\n{lines}\n-----END CERTIFICATE-----\n"


def image_digest(role: str, release: str) -> str:
    return "sha256:" + hashlib.sha256(f"{role}:{release}".encode()).hexdigest()


class FakeClock:
    """Monotonic time that moves only when the installer sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []
        self.hook: Callable[[], None] | None = None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        if self.hook is not None:
            self.hook()


class FakeSystem:
    """Podman, systemd, loginctl, chcon, ip and aegisctl for the installer, as in-memory state.

    It records every argument vector and refuses any program it does not know. It reports
    only states real systemd reports: a unit whose start fails on a dependency is left
    inactive with Result=success and no job, and an empty Job property is left out. Starting
    units reads the installed unit files the way Quadlet does: volumes and networks appear
    with their labels, and a container runs the image its unit file names when it is
    (re)created. It writes nothing but the ``podman cp`` destination and ``--cidfile`` files,
    and what aegisctl writes.

    aegisctl runs against the extracted library: ``inspect`` and ``validate`` really run it on
    this Python with the installer's argument vector and environment. ``preflight`` and
    ``render`` parse their arguments with aegisctl's own parser and then call its library
    directly, with made-up observer fingerprints in place of the podman-run observer and the
    invoking group in place of the operator's (the record's group is a sample value).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.prefix = SAMPLE_PREFIX
        # Answers a call itself when it returns (exit code, stdout, stderr), else passes it
        # on. It may also raise, as the runner does on a timeout.
        self.intercept: Callable[[tuple[str, ...]], tuple[int, str, str] | None] | None = None
        self.rootless = True
        self.selinux_enabled = True
        self.systemd_version = "systemd 256 (256-test)"
        self.linger = "yes"
        self.manager_environment: dict[str, str] = {}
        self.listeners: set[int] = set()
        self.containers: list[dict[str, Any]] = []
        self.networks: dict[str, dict[str, Any]] = {
            "podman": {"name": "podman", "id": "9" * 64, "labels": {},
                       "subnets": [{"subnet": "10.88.0.0/16", "gateway": "10.88.0.1"}]},
        }
        self.volumes: dict[str, dict[str, Any]] = {}
        self.routes4: list[dict[str, Any]] = [
            {"dst": "default", "gateway": "192.0.2.1", "dev": "test0"},
            {"dst": "192.0.2.0/24", "dev": "test0"},
        ]
        self.routes6: list[dict[str, Any]] = [{"dst": "fe80::/64", "dev": "test0"}]
        self.published: set[str] = {SAMPLE_RELEASE, SAMPLE_OTHER_RELEASE, SAMPLE_THIRD_RELEASE}
        # The backend image's org.opencontainers.image.created label, per release.
        self.created_labels: dict[str, str | None] = dict(SAMPLE_CREATED)
        self.missing_images: set[str] = set()
        # Images a deployment test built locally, by ID: their role and test release.
        self.local_images: dict[str, tuple[str, str]] = {}
        self.release_tree = RELEASE_SOURCE_TREE
        self.library_tree = LIBRARY_SOURCE_TREE
        self.tree_hook: Any = None
        self.library_hook: Callable[[Path], None] | None = None
        self.release_hooks: dict[str, Callable[[Path], None]] = {}
        self.unit_directory: Path | None = None
        self.units: dict[str, dict[str, str]] = {}
        self.running_images: dict[str, str] = {}
        # The name Podman reports per container when it differs from the unit's reference.
        self.reported_image_names: dict[str, str] = {}
        self.after_start: dict[str, dict[str, str]] = {}
        self.unhealthy: set[str] = set()
        self.created: list[str] = []
        self.created_from: dict[str, str] = {}
        self.removed: list[str] = []
        self.failures: dict[tuple[str, ...], tuple[int, str, str]] = {}
        self.create_output: str | None = None
        self.write_cidfile = True
        self.ca_output = sample_certificate()
        self.run_output = "user_id=1 created=true\n"
        self.counter = 0
        # What chcon labelled and podman unshare chown handed over, in order.
        self.labelled: list[str] = []
        self.chowned: dict[str, str] = {}
        # Every aegisctl run: its argument vector and environment.
        self.aegisctl_runs: list[tuple[tuple[str, ...], dict[str, str]]] = []
        self.host_gid = SAMPLE_HOST_GID
        # What podman unshare chcon -R relabelled, in order: (mount point, level).
        self.relabelled: list[tuple[str, str]] = []
        self.volume_storage = SAMPLE_VOLUME_STORAGE
        # SELinux as the reader probe meets it: each source's label (shared with the test's
        # fake xattrs) and the types whose read rules the loaded module grants.
        self.labels: dict[str, bytes] = {}
        self.loaded_types: set[str] = set()
        # Probe containers that still exist, by name: an interrupted podman run leaves one.
        self.probe_containers: dict[str, dict[str, Any]] = {}
        self.probes: list[tuple[str, ...]] = []

    # -- plumbing --------------------------------------------------------------------------
    def __call__(
        self, arguments: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        assert timeout > 0
        command = tuple(arguments)
        assert all(isinstance(word, str) for word in command), command
        self.calls.append(command)
        if command[: len(AEGISCTL)] == AEGISCTL:
            assert env is not None, command
            self.aegisctl_runs.append((command, dict(env)))
        else:
            assert env is None, command
        if self.intercept is not None and (answer := self.intercept(command)) is not None:
            return subprocess.CompletedProcess(list(command), *answer)
        for prefix, (code, stdout, stderr) in self.failures.items():
            if command[: len(prefix)] == prefix:
                return subprocess.CompletedProcess(list(command), code, stdout, stderr)
        if command[: len(AEGISCTL)] == AEGISCTL:
            assert env is not None
            return subprocess.CompletedProcess(
                list(command), *self.aegisctl(command[len(AEGISCTL):], env),
            )
        if command[:2] == PODMAN:
            code, stdout = self.podman(command[2:])
        elif command[:2] == SYSTEMCTL:
            code, stdout = self.systemctl(command[2:])
        elif command == ("systemctl", "--version"):
            code, stdout = 0, f"{self.systemd_version}\n+PAM +AUDIT +SELINUX\n"
        elif command[:2] == ("loginctl", "show-user"):
            assert command[3:] == ("--property=Linger", "--value"), command
            code, stdout = 0, f"{self.linger}\n"
        elif command[:6] == CONTAINER_FILE_LABEL and len(command) == 7:
            # Files, and the derivatives directory, at level s0; never through a link.
            target = Path(command[6])
            assert (target.is_file() or target.is_dir()) and not target.is_symlink(), command
            self.labelled.append(command[6])
            code, stdout = 0, ""
        elif command[:2] == ("ip", "-j"):
            code, stdout = self.routes(command[2:])
        else:
            raise AssertionError(f"unexpected command {command}")
        return subprocess.CompletedProcess(list(command), code, stdout, "")

    def commands(self, *prefix: str) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[: len(prefix)] == prefix]

    def new_id(self) -> str:
        self.counter += 1
        return f"{self.counter:064x}"

    def release_of(self, image: str) -> str:
        if image in self.local_images:
            return self.local_images[image][1]
        for release in self.published:
            if image.endswith(image_digest("backend", release)):
                return release
        raise AssertionError(f"no published release has the image {image}")

    # -- programs --------------------------------------------------------------------------
    def routes(self, words: tuple[str, ...]) -> tuple[int, str]:
        family, rest = words[0], words[1:]
        assert rest == ("route", "show", "table", "all"), words
        return 0, json.dumps(self.routes4 if family == "-4" else self.routes6)

    def podman(self, words: tuple[str, ...]) -> tuple[int, str]:
        match words:
            case ("info", "--format", "json"):
                return 0, json.dumps({
                    "host": {"security": {
                        "rootless": self.rootless, "selinuxEnabled": self.selinux_enabled,
                    }},
                    "store": {"volumePath": self.volume_storage},
                })
            case ("ps", "-a", "--format", "json"):
                return 0, json.dumps(self.containers)
            case ("network", "ls", "--format", "json"):
                return 0, json.dumps(list(self.networks.values()))
            case ("network", "inspect", *names):
                if any(name not in self.networks for name in names):
                    return 125, ""
                return 0, json.dumps([self.networks[name] for name in names])
            case ("network", "exists", name):
                return (0 if name in self.networks else 1), ""
            case ("network", "rm", name):
                assert name in self.networks, name
                del self.networks[name]
                return 0, name
            case ("volume", "ls", "--format", "json"):
                return 0, json.dumps([{"Name": name} for name in self.volumes] or None)
            case ("volume", "exists", name):
                return (0 if name in self.volumes else 1), ""
            case ("volume", "inspect", name):
                if name not in self.volumes:
                    return 125, ""
                return 0, json.dumps([self.volumes[name]])
            case ("volume", "rm", name):
                assert name in self.volumes, name
                del self.volumes[name]
                return 0, name
            case ("pull", "--quiet", reference):
                repository, _, release = reference.rpartition(":")
                if release not in self.published:
                    return 125, ""
                return 0, image_digest(repository, release)[7:] + "\n"
            case ("image", "inspect", "--format", "{{.Id}}", reference):
                if reference in self.missing_images or (
                    re.fullmatch(r"[0-9a-f]{64}", reference) and reference not in self.local_images
                ):
                    return 125, ""
                return 0, self.image_id_for(reference) + "\n"
            case ("image", "inspect", reference) if re.fullmatch(r"[0-9a-f]{64}", reference):
                if reference not in self.local_images:
                    return 125, ""
                role, release = self.local_images[reference]
                created = self.created_labels.get(release)
                return 0, json.dumps([{
                    "Id": reference, "Digest": "sha256:" + "f" * 64, "RepoDigests": [],
                    "RepoTags": [f"localhost/aegis-{role}:{release}"],
                    "Labels": {} if created is None else {
                        "org.opencontainers.image.created": created,
                    },
                }])
            case ("image", "inspect", reference):
                repository, _, release = reference.rpartition(":")
                role = repository.rpartition("/aegis-")[2]
                digest = image_digest(role, release)
                created = self.created_labels.get(release)
                labels = {} if created is None else {"org.opencontainers.image.created": created}
                return 0, json.dumps([{
                    "Id": "e" * 64, "Digest": digest, "Labels": labels,
                    "RepoDigests": [f"{repository}@{digest}"],
                }])
            case ("image", "exists", reference):
                return (1 if reference in self.missing_images else 0), ""
            case ("create", "--pull=never", "--network=none", cidfile, image, "python3"):
                assert "@sha256:" in image or image in self.local_images, words
                assert cidfile.startswith("--cidfile="), words
                identifier = self.new_id()
                self.created.append(identifier)
                self.created_from[identifier] = image
                path = Path(cidfile.removeprefix("--cidfile="))
                if self.write_cidfile:
                    assert not path.exists(), path
                    path.write_text(identifier, encoding="ascii")
                if self.create_output is not None:
                    return 0, self.create_output
                return 0, identifier + "\n"
            case ("cp", source, destination):
                identifier, _, path = source.partition(":")
                assert identifier in self.created and identifier not in self.removed, source
                if path == "/app/backend/aegisctl":
                    self.copy_library(Path(destination), self.created_from[identifier])
                    return 0, ""
                assert path == "/usr/share/aegis/podman", source
                self.copy_release_tree(Path(destination), self.created_from[identifier])
                return 0, ""
            case ("rm", identifier):
                assert identifier in self.created, f"removal of a foreign container {identifier}"
                if identifier in self.removed:
                    return 1, ""
                self.removed.append(identifier)
                return 0, identifier + "\n"
            case ("unshare", "stat", "--format=%u:%g:%a", "--", path):
                # As the containers' user namespace shows it: a directory handed over looks
                # owned by 10001 and the operator's group (0 there).
                info = os.stat(path)
                owner = self.chowned.get(path, f"{info.st_uid}:{info.st_gid}")
                return 0, f"{owner}:{stat.S_IMODE(info.st_mode):o}\n"
            case ("unshare", "chcon", "-R", "-h", "-l", level, path):
                # Only ever a volume's own mount point inside the volume storage.
                assert path.startswith(f"{self.volume_storage}/"), path
                assert path.endswith("/_data") and path.count("/") == (
                    self.volume_storage.count("/") + 2
                ), path
                self.relabelled.append((path, level))
                return 0, ""
            case ("unshare", "chown", "-h", "10001:10001" | "10001:0" as owner, path):
                target = Path(path)
                assert (target.is_file() or target.is_dir()) and not target.is_symlink(), path
                self.chowned[path] = owner
                return 0, ""
            case ("container", "inspect", name) if name in self.probe_containers:
                return 0, json.dumps([self.probe_containers[name]])
            case ("container", "inspect", name):
                service = f"{name}.service"
                if self.units.get(service, {}).get("ActiveState") != "active":
                    return 125, ""
                status = "starting" if name in self.unhealthy else "healthy"
                reference = self.running_images.get(name, "")
                return 0, json.dumps([{
                    # Like Podman: the name it resolved (here a local tag stands in for a
                    # digest or ID), plus the image ID the container actually runs.
                    "Name": name, "ImageName": self.reported_image_names.get(name, reference),
                    "Image": self.image_id_for(reference) if reference else "",
                    "State": {"Health": {"Status": status}},
                }])
            case ("exec", _container, "cat", "/data/caddy/pki/authorities/local/root.crt"):
                return 0, self.ca_output
            case ("run", "--rm", "--pull", "never", "--network", "none", "--name", name,
                  "--label", label, *rest) if label.startswith("aegis.probe="):
                return self.probe(name, label, tuple(rest))
            case ("container", "exists", name):
                return (0 if name in self.probe_containers else 1), ""
            case ("rm", "--force", "--ignore", identifier):
                found = [name for name, entry in self.probe_containers.items()
                         if entry["Id"] == identifier]
                assert found, f"removal of a foreign container {identifier}"
                del self.probe_containers[found[0]]
                self.removed.append(identifier)
                return 0, identifier + "\n"
            case ("run", "--rm", *_rest):
                return 0, self.run_output
        raise AssertionError(f"unexpected podman call {words}")

    def copy_release_tree(self, destination: Path, image: str) -> None:
        """Copy a release tree the way the image ships it: read-only files and directories."""
        assert not destination.exists(), destination
        shutil.copytree(
            self.release_tree, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        if self.tree_hook is not None:
            self.tree_hook(destination)
        hook = self.release_hooks.get(self.release_of(image))
        if hook is not None:
            hook(destination)
        for directory, subdirectories, files in os.walk(destination, topdown=False):
            for name in files:
                path = Path(directory) / name
                if not path.is_symlink():
                    path.chmod(0o555 if name == "aegis-podman" else 0o444)
            for name in subdirectories:
                if not (Path(directory) / name).is_symlink():
                    (Path(directory) / name).chmod(0o555)
        destination.chmod(0o555)

    def probe(self, name: str, label: str, rest: tuple[str, ...]) -> tuple[int, str]:
        """The reader probe: SELinux as ``loaded_types`` says, then the probe's own script,
        run by this shell against the real source, so real permissions decide the rest."""
        self.probes.append((name, label, *rest))
        assert "label=type:aegis_reader.process" in rest, rest
        volume = rest[rest.index("-v") + 1]
        source = volume.split(":")[0]
        assert volume == f"{source}:/srv/aegis/probe:ro", volume  # never a relabel option
        selinux_type = self.labels[source].rstrip(b"\0").decode("ascii").split(":", 3)[2]
        if selinux_type not in self.loaded_types:
            return 1, ""
        script_at = rest.index("-c")
        script = rest[script_at + 1].replace("/srv/aegis/probe", source)
        result = subprocess.run(
            ["/bin/sh", "-c", script, *rest[script_at + 2:]], check=False,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
        )
        return result.returncode, ""

    def run_host_setup(self, module: Path) -> None:
        """What the operator's sudo run of the script does to SELinux: load this module."""
        self.loaded_types = set(re.findall(
            r"\(allow process ([a-z0-9_]+) \(dir ", module.read_text(encoding="utf-8"),
        ))

    def copy_library(self, destination: Path, image: str) -> None:
        """Copy the backend image's aegisctl package, read-only as the image ships it."""
        assert not destination.exists(), destination
        self.release_of(image)
        shutil.copytree(
            self.library_tree, destination,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        if self.library_hook is not None:
            self.library_hook(destination)
        for directory, subdirectories, files in os.walk(destination, topdown=False):
            for name in files:
                (Path(directory) / name).chmod(0o444)
            for name in subdirectories:
                (Path(directory) / name).chmod(0o555)
        destination.chmod(0o555)

    def aegisctl(self, words: tuple[str, ...], env: Mapping[str, str]) -> tuple[int, str, str]:
        """``python -m aegisctl`` against the installer's extracted copy of the package."""
        from aegisctl import cli, mounts

        library = Path(env["PYTHONPATH"])
        assert (library / "aegisctl" / "__main__.py").is_file(), library
        assert env.get("AEGIS_CONTAINER_ENGINE") == "podman", env
        assert not [key for key in env if key.startswith("PYTHON") and key != "PYTHONPATH"], env
        try:
            args = cli._parser().parse_args(list(words))
            cli._check_choice_options(args)
        except SystemExit:
            raise AssertionError(f"aegisctl refuses the arguments {words}") from None
        if args.mount_command in ("inspect", "validate"):
            result = subprocess.run(
                [*AEGISCTL, *words], env=dict(env), check=False, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=60,
            )
            return result.returncode, result.stdout, result.stderr
        try:
            if args.mount_command == "preflight":
                assert args.observer == "podman-run" and args.host_gid == self.host_gid, args
                assert not Path(args.manifest).exists(), "the manifest path is fresh"
                validated = tuple(
                    replace(slot, mount_fingerprint=hashlib.sha256(
                        f"{slot.slot_id}:{slot.source}".encode()).hexdigest())
                    for slot in mounts.preflight_slots(mounts.parse_config(args.config))
                )
                digest = mounts.write_manifest(
                    args.manifest, validated, uid=os.geteuid(), gid=os.getegid(),
                )
                return 0, json.dumps({"status": "preflighted", "manifestSha256": digest}) + "\n", ""
            assert args.mount_command == "render" and args.format == "quadlet", args
            assert args.host_gid == self.host_gid, args
            # aegisctl reads the manifest as the operator: it must not be handed over yet.
            assert str(args.manifest) not in self.chowned, "render needs a fresh manifest"
            result = mounts.render_quadlet_artifacts(
                args.config, args.manifest, args.output_dir, args.gateway_attestation,
                unit_prefix=args.unit_prefix, host_gid=os.getegid(),
            )
        except mounts.ConfigError as error:
            return 2, "", json.dumps({"status": "error", "message": str(error)}) + "\n"
        return 0, json.dumps({
            "status": "rendered", "manifestSha256": result.manifest_digest,
            "gatewayAttestationSha256": result.gateway_digest,
        }) + "\n", ""

    def systemctl(self, words: tuple[str, ...]) -> tuple[int, str]:
        match words:
            case ("daemon-reload",):
                return 0, ""
            case ("show-environment",):
                return 0, "".join(f"{key}={value}\n"
                                  for key, value in self.manager_environment.items())
            case ("enable" | "disable", _unit):
                return 0, ""
            case ("--no-block", "start" | "restart" as verb, *units):
                for unit in units:
                    self.activate(unit, restart=verb == "restart")
                self.quadlet_start(restart=verb == "restart")
                for unit, values in self.after_start.items():
                    self.units[unit] = self.unit(unit) | values
                return 0, ""
            case ("--no-block", "stop", *units):
                for unit in units:
                    self.units[unit] = self.unit(unit) | {
                        "ActiveState": "inactive", "SubState": "dead",
                    }
                return 0, ""
            case ("show", properties, *units) if properties.startswith("--property="):
                blocks = []
                for unit in units:
                    values = {key: value for key, value in self.unit(unit).items() if value}
                    blocks.append("\n".join(f"{key}={value}" for key, value in values.items()))
                return 0, "\n\n".join(blocks) + "\n"
        raise AssertionError(f"unexpected systemctl call {words}")

    @staticmethod
    def image_id_for(reference: str) -> str:
        """The image ID a reference resolves to: a local ID is itself, a digest its own hash."""
        if re.fullmatch(r"[0-9a-f]{64}", reference):
            return reference
        return hashlib.sha256(reference.encode("utf-8")).hexdigest()

    def unit(self, name: str) -> dict[str, str]:
        default = {"Id": name, "LoadState": "loaded" if self.has_source(name) else "not-found",
                   "ActiveState": "inactive", "SubState": "dead", "Result": "success",
                   "NRestarts": "0", "Job": ""}
        return default | self.units.get(name, {})

    def has_source(self, name: str) -> bool:
        """Whether a unit file defines ``name``; systemd reports any other unit not-found."""
        if self.unit_directory is None:
            return True
        if name.endswith(".socket"):
            return (self.unit_directory.parent.parent / "systemd" / "user" / name).is_file()
        stem = name.removesuffix(".service")
        for marker, suffix in (("-network", ".network"), ("-volume", ".volume")):
            if stem.endswith(marker) and (
                self.unit_directory / f"{stem.removesuffix(marker)}{suffix}"
            ).is_file():
                return True
        return (self.unit_directory / f"{stem}.container").is_file()

    def unit_file(self, name: str) -> str | None:
        if self.unit_directory is None or not (self.unit_directory / name).is_file():
            return None
        return (self.unit_directory / name).read_text(encoding="utf-8")

    def activate(self, unit: str, *, restart: bool = False) -> None:
        was_active = self.unit(unit)["ActiveState"] == "active"
        if unit.endswith(".socket"):
            sub = "listening"
        elif unit.endswith(("migrate.service", "-network.service", "-volume.service")):
            sub = "exited"
        else:
            sub = "running"
        self.units[unit] = self.unit(unit) | {"ActiveState": "active", "SubState": sub}
        stem = unit.removesuffix(".service")
        text = self.unit_file(f"{stem}.container")
        if text is not None and (restart or not was_active):
            # Quadlet creates the container afresh, from the image its unit names.
            self.running_images[stem] = parse_unit(text).value("Container", "Image") or ""

    def quadlet_start(self, *, restart: bool) -> None:
        """What a start pulls in: Quadlet's volume and network services, and migrate."""
        if self.unit_directory is None or not self.unit_directory.is_dir():
            return
        for path in sorted(self.unit_directory.iterdir()):
            # <unit>.container.d/ holds drop-ins, which extend a unit rather than define one.
            if not path.name.startswith(self.prefix) or not path.is_file():
                continue
            unit = parse_unit(path.read_text(encoding="utf-8"))
            if path.suffix == ".volume":
                name = unit.value("Volume", "VolumeName") or ""
                # Podman's local driver keeps the mount options it was created with.
                options = {
                    option: value for key, option in (
                        ("Device", "device"), ("Type", "type"), ("Options", "o"),
                    ) if (value := unit.value("Volume", key)) is not None
                }
                self.volumes.setdefault(name, {
                    "Name": name, "Labels": labels(unit, "Volume"), "Options": options,
                    "Mountpoint": f"{self.volume_storage}/{name}/_data",
                })
                self.activate(f"{path.stem}-volume.service")
            elif path.suffix == ".network":
                name = unit.value("Network", "NetworkName") or ""
                subnet = unit.value("Network", "Subnet")
                self.networks.setdefault(name, {
                    "name": name, "id": self.new_id(), "labels": labels(unit, "Network"),
                    "subnets": [{"subnet": subnet}] if subnet else [],
                })
                self.activate(f"{path.stem}-network.service")
            elif path.name == f"{self.prefix}migrate.container":
                self.activate(f"{self.prefix}migrate.service", restart=restart)


def labels(unit: Unit, section: str) -> dict[str, str]:
    return dict(value.partition("=")[::2] for value in unit.values(section, "Label"))
